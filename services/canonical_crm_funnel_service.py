"""
services/canonical_crm_funnel_service.py

PR-ADS-153B — ONE canonical CRM funnel contract. HubSpot Lifecycle Stage is the
funnel; Averroes ingests, deduplicates, attributes, windows, reconciles and
reports it, and never invents a parallel lifecycle.

Canonical definitions
---------------------
Every funnel metric is a STAGE-ENTRY EVENT proven by a HubSpot timestamp:

    Lead        contact entered `lead`                 → hs_v2_date_entered_lead
    MQL         contact entered `marketingqualifiedlead`→ hs_v2_date_entered_marketingqualifiedlead
    SQL         contact entered `salesqualifiedlead`   → hs_v2_date_entered_salesqualifiedlead
    Opportunity contact entered `opportunity`          → hs_v2_date_entered_opportunity
    Customer    contact entered `customer`             → hs_v2_date_entered_customer
                (LIFECYCLE customer — distinct from the REVENUE customer, which
                 PR-ADS-153E defines from closed-won deal truth)

What this replaces
------------------
The legacy doctrine defined "SQL" as ``status_category = 'qualified'``, derived
from ``mql_status ∈ {CLOSED - Sales Qualified, CLOSED - Deal Created}`` and dated
by the contact's CREATION date. That produced acquisition-cohort counts labelled
as qualification events. Here:

  * SQL is lifecycle-entry evidence, never ``mql_status``;
  * the event date is the stage-entry timestamp, never ``createdate``;
  * a missing stage-entry timestamp is a COVERAGE GAP (the contact is not counted
    in a bounded window and is reported as missing), never silently back-dated.

Non-exclusivity
---------------
Funnel counts are NOT mutually exclusive by current lifecycle stage. A contact
now at ``customer`` still entered ``salesqualifiedlead`` on some date and remains
in that historical SQL cohort.

Named scopes (the PR-ADS-152 algebra, applied to every funnel event)
--------------------------------------------------------------------
    keyword_attributable ≤ campaign_attributable ≤ google_ads_source ≤ all_source

``all_source`` is genuinely all-source here: the canonical contact store ingests
every HubSpot contact, not only paid-search ones (PR-ADS-153B §18).

Purity
------
``build_populations`` and every helper below operate on plain dict rows, so the
whole funnel — scopes, cohort conversions, coverage and reconciliation — is
unit-testable without a database. Only ``build`` touches the repository.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from analysis.crm_lifecycle import (
    EVENT_CUSTOMER,
    EVENT_DATE_COLUMN,
    EVENT_HUBSPOT_PROPERTY,
    EVENT_LABELS,
    EVENT_MQL,
    EVENT_OPPORTUNITY,
    EVENT_SQL,
    EVENT_STAGE,
    FUNNEL_EVENTS,
    FUNNEL_PROGRESSION,
    LIFECYCLE_RULE_VERSION,
    is_known_stage,
    is_valid_event,
    normalize_lifecycle_stage,
)
from analysis.source_classification import GROUP_GOOGLE_ADS, classify_source
from services.canonical_contact_outcome_service import (
    STATUS_MISMATCH,
    STATUS_PARTIAL,
    STATUS_RECONCILED,
    STATUS_UNAVAILABLE,
    WINDOW_BUSINESS,
    WINDOW_EVIDENCE,
    resolve_window_contract,
)

logger = logging.getLogger(__name__)

# ── Named scopes (event-agnostic; the SQL-specific names stay in PR-ADS-152) ──
SCOPE_ALL_SOURCE = "all_source"
SCOPE_GOOGLE_ADS_SOURCE = "google_ads_source"
SCOPE_CAMPAIGN_ATTRIBUTABLE = "campaign_attributable"
SCOPE_KEYWORD_ATTRIBUTABLE = "keyword_attributable"

# Ordered broadest → narrowest. The nesting invariant is asserted, not assumed.
ORDERED_SCOPES = (
    SCOPE_ALL_SOURCE,
    SCOPE_GOOGLE_ADS_SOURCE,
    SCOPE_CAMPAIGN_ATTRIBUTABLE,
    SCOPE_KEYWORD_ATTRIBUTABLE,
)

SCOPE_LABELS = {
    SCOPE_ALL_SOURCE: "All sources",
    SCOPE_GOOGLE_ADS_SOURCE: "Google Ads-source",
    SCOPE_CAMPAIGN_ATTRIBUTABLE: "Campaign-attributable",
    SCOPE_KEYWORD_ATTRIBUTABLE: "Keyword-attributable",
}

# Canonical contract constants, surfaced in every payload.
FUNNEL_SOURCE = "hubspot_lifecycle"
FUNNEL_TABLE = "hubspot_contact_funnel"
# The durable identity column on the canonical table. Named exactly as the
# schema names it so API consumers can map the payload to the DDL.
FUNNEL_DEDUP_KEY = "contact_id"

# Conversion basis vocabulary. A rate is only ever published on a cohort basis.
BASIS_COHORT = "cohort"
BASIS_UNAVAILABLE = "unavailable"

# Reason codes for contacts that cannot be counted in a bounded window.
REASON_MISSING_STAGE_DATE = "missing_stage_entry_date"
REASON_NOT_GOOGLE_ADS = "not_google_ads_source"
REASON_UNKNOWN_LIFECYCLE_STAGE = "unknown_lifecycle_stage"


def _norm(value) -> str:
    return (value or "").strip().lower()


def event_definition(event: str) -> dict:
    """The canonical, self-describing definition of one funnel event."""
    if not is_valid_event(event):
        raise ValueError(f"Unknown funnel event '{event}'")
    return {
        "event": event,
        "label": EVENT_LABELS[event],
        "definition": (
            f"HubSpot contact entered lifecycle stage '{EVENT_STAGE[event]}'"
        ),
        "canonical_source": FUNNEL_SOURCE,
        "table": FUNNEL_TABLE,
        "dedup_key": FUNNEL_DEDUP_KEY,
        "event_date_property": EVENT_HUBSPOT_PROPERTY[event],
        "event_date_column": EVENT_DATE_COLUMN[event],
        "rule_version": LIFECYCLE_RULE_VERSION,
    }


def funnel_definitions() -> dict:
    """Every canonical funnel definition, keyed by event."""
    return {event: event_definition(event) for event in FUNNEL_EVENTS}


# ── Window membership ────────────────────────────────────────────────────────
def _in_window(value, start: date | None, end: date | None) -> bool:
    """Is an event date inside ``[start, end]``?

    A None event date is NEVER in a bounded window — absence of stage evidence is
    a coverage gap, not a date. In an unbounded (All Time) window a None date is
    still not an event: no timestamp means no proven transition.
    """
    if value is None:
        return False
    if isinstance(value, datetime):
        value = value.date()
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


# ── Scope derivation ─────────────────────────────────────────────────────────
def derive_acquisition_group(row: dict) -> str:
    """Acquisition group from the contact's OWN HubSpot Original Source.

    The drill-down detail is not passed: it is not safely re-derivable for every
    contact, and the established doctrine (PR-ADS-152 §4) refuses to guess it.
    Campaign name is NEVER used to infer a source.
    """
    return classify_source(row.get("hs_analytics_source"), None)


def default_campaign_resolver(campaign_name) -> tuple[bool, str | None]:
    """Fallback resolver used when the Google Ads identity contract is not
    consulted (pure tests). Delegates to the PR-ADS-152 safe-campaign rule."""
    from services.canonical_contact_outcome_service import (  # noqa: PLC0415
        campaign_disqualifier, is_safe_campaign,
    )
    if is_safe_campaign(campaign_name):
        return True, None
    return False, campaign_disqualifier(campaign_name)


def _contact_scopes(row: dict, resolver) -> dict:
    """Scope membership flags for ONE contact. Scope is a property of the
    contact's acquisition evidence and applies identically to every funnel event
    that contact produced — attribution creates subsets, it never redefines the
    underlying event (Minimum Viable Truth rule 6)."""
    group = derive_acquisition_group(row)
    is_google_ads = group == GROUP_GOOGLE_ADS
    campaign_ok, campaign_reason = resolver(row.get("hs_analytics_source_data_1"))
    has_keyword = bool((row.get("hs_analytics_source_data_2") or "").strip())

    in_campaign = is_google_ads and campaign_ok
    return {
        "acquisition_group": group,
        SCOPE_ALL_SOURCE: True,
        SCOPE_GOOGLE_ADS_SOURCE: is_google_ads,
        SCOPE_CAMPAIGN_ATTRIBUTABLE: in_campaign,
        # Funnel-side keyword scope: a campaign-attributable contact that also
        # carries a HubSpot keyword label. This is NOT a Google Ads criterion-level
        # join (none exists — see the PR-ADS-153A audit); it is the narrowest
        # honest subset and is always named as such.
        SCOPE_KEYWORD_ATTRIBUTABLE: in_campaign and has_keyword,
        "campaign_block_reason": campaign_reason,
    }


def build_populations(
    rows: list[dict],
    start: date | None,
    end: date | None,
    *,
    campaign_resolver=None,
) -> dict:
    """Build canonical funnel event populations from raw contact rows. Pure.

    For every funnel event, a contact belongs to that event's population when its
    stage-entry date for that event falls inside ``[start, end]``. A contact can
    belong to several event populations in the same window (it may have entered
    MQL and SQL in the same quarter), and belongs to a historical cohort
    regardless of its CURRENT lifecycle stage.

    Returns ``{events, contacts, counts, coverage}``:
      ``events``   event → list of contact dicts in that event's window population
      ``contacts`` every input contact, normalised, with scope flags
      ``counts``   event → scope → count
      ``coverage`` stage-evidence coverage and unknown-stage diagnostics
    """
    resolver = campaign_resolver or default_campaign_resolver

    contacts: list[dict] = []
    events: dict[str, list[dict]] = {e: [] for e in FUNNEL_EVENTS}
    missing_stage_dates: dict[str, int] = {e: 0 for e in FUNNEL_EVENTS}
    unknown_stage_contacts = 0
    stage_reached_without_date: dict[str, int] = {e: 0 for e in FUNNEL_EVENTS}

    for raw in rows or []:
        contact_id = str(raw.get("contact_id") or "").strip()
        if not contact_id:
            # No durable HubSpot identity — never counted, never synthesised.
            continue

        scopes = _contact_scopes(raw, resolver)
        stage = normalize_lifecycle_stage(raw.get("lifecycle_stage"))
        if stage is not None and not is_known_stage(stage):
            unknown_stage_contacts += 1

        event_dates = {
            event: _as_date(raw.get(EVENT_DATE_COLUMN[event]))
            for event in FUNNEL_EVENTS
        }

        contact = {
            "contact_id": contact_id,
            "company": raw.get("company"),
            "lifecycle_stage": stage,
            "lifecycle_stage_known": stage is None or is_known_stage(stage),
            "mql_status": raw.get("mql_status"),
            "mql_status_category": raw.get("mql_status_category"),
            "created_at": _as_date(raw.get("created_at")),
            "acquisition_group": scopes["acquisition_group"],
            "campaign_name": raw.get("hs_analytics_source_data_1"),
            "keyword": raw.get("hs_analytics_source_data_2"),
            "country": raw.get("ip_country") or raw.get("country"),
            "has_gclid": bool(raw.get("has_gclid")),
            "event_dates": event_dates,
            "scopes": {s: scopes[s] for s in ORDERED_SCOPES},
            "campaign_block_reason": scopes["campaign_block_reason"],
        }
        contacts.append(contact)

        for event in FUNNEL_EVENTS:
            event_date = event_dates[event]
            if event_date is None:
                # The contact's current stage says it reached this stage, but no
                # entry timestamp exists — a coverage gap worth reporting, and
                # never repaired with createdate.
                if _stage_reached(stage, event):
                    stage_reached_without_date[event] += 1
                    missing_stage_dates[event] += 1
                continue
            if _in_window(event_date, start, end):
                events[event].append(contact)

    counts = {
        event: {scope: sum(1 for c in events[event] if c["scopes"][scope])
                for scope in ORDERED_SCOPES}
        for event in FUNNEL_EVENTS
    }

    coverage = {
        "contacts_considered": len(contacts),
        "missing_stage_entry_dates": missing_stage_dates,
        "stage_reached_without_entry_date": stage_reached_without_date,
        "unknown_lifecycle_stage_contacts": unknown_stage_contacts,
    }

    return {
        "events": events,
        "contacts": contacts,
        "counts": counts,
        "coverage": coverage,
    }


def _stage_reached(current_stage, event: str) -> bool:
    """Does the contact's CURRENT stage imply it must have entered ``event``?

    Used only to quantify missing stage-entry evidence. It never creates an
    event and never infers a date.
    """
    from analysis.crm_lifecycle import STAGE_RANK  # noqa: PLC0415

    current_rank = STAGE_RANK.get(current_stage)
    event_rank = STAGE_RANK.get(EVENT_STAGE[event])
    if current_rank is None or event_rank is None:
        return False
    return current_rank >= event_rank


# ── Scope key sets + nesting invariant ───────────────────────────────────────
def scope_keys(population: list[dict], scope: str) -> set:
    """Contact-id set for a named scope within one event population."""
    if scope not in ORDERED_SCOPES:
        raise ValueError(f"Unknown scope '{scope}'")
    return {c["contact_id"] for c in population if c["scopes"][scope]}


def scopes_are_nested(population: list[dict]) -> bool:
    """Prove keyword ⊆ campaign ⊆ google_ads ⊆ all_source for one population."""
    sets = [scope_keys(population, scope) for scope in ORDERED_SCOPES]
    return all(sets[i + 1] <= sets[i] for i in range(len(sets) - 1))


# ── Cohort-safe conversions ──────────────────────────────────────────────────
def cohort_conversion(
    populations: dict,
    from_event: str,
    to_event: str,
    scope: str = SCOPE_ALL_SOURCE,
) -> dict:
    """Cohort-safe conversion between two funnel events.

    The denominator is the set of contacts that entered ``from_event`` INSIDE the
    window. The numerator is the subset of THAT SAME cohort which also entered
    ``to_event`` at any later time (inside or outside the window). This is the
    only honest way to express progression: dividing two independent event-period
    totals compares different cohorts and is never done here.

    When the cohort is empty the rate is ``None`` with basis ``unavailable`` — a
    funnel rate is never fabricated.
    """
    cohort = [c for c in populations["events"][from_event] if c["scopes"][scope]]
    denominator = len(cohort)
    if denominator == 0:
        return {
            "from_event": from_event,
            "to_event": to_event,
            "scope": scope,
            "cohort_size": 0,
            "converted": None,
            "rate_pct": None,
            "basis": BASIS_UNAVAILABLE,
            "available": False,
            "reason": "empty_cohort",
        }

    converted = 0
    for contact in cohort:
        from_date = contact["event_dates"].get(from_event)
        to_date = contact["event_dates"].get(to_event)
        if to_date is None or from_date is None:
            continue
        # A later-or-same-day transition proves progression for this cohort.
        if to_date >= from_date:
            converted += 1

    return {
        "from_event": from_event,
        "to_event": to_event,
        "scope": scope,
        "cohort_size": denominator,
        "converted": converted,
        "rate_pct": round(converted * 100.0 / denominator, 2),
        "basis": BASIS_COHORT,
        "available": True,
        "reason": None,
    }


def build_conversions(populations: dict, scope: str = SCOPE_ALL_SOURCE) -> list[dict]:
    """Every adjacent funnel conversion, cohort-safe."""
    return [
        cohort_conversion(populations, from_event, to_event, scope)
        for from_event, to_event in FUNNEL_PROGRESSION
    ]


# ── Reconciliation status ────────────────────────────────────────────────────
def reconciliation_status(populations: dict, *, available: bool) -> dict:
    """Status of the canonical funnel for this window.

    ``mismatch`` means an invariant broke (scope nesting, or an impossible count)
    and the numbers must NOT render as normal values. ``partial`` means the funnel
    is readable but stage evidence is incomplete. ``unavailable`` means the truth
    could not be read at all — never rendered as zero.
    """
    if not available:
        return {
            "status": STATUS_UNAVAILABLE,
            "reasons": ["canonical_contact_store_unavailable"],
        }

    reasons: list[str] = []
    for event in FUNNEL_EVENTS:
        if not scopes_are_nested(populations["events"][event]):
            reasons.append(f"scope_nesting_broken:{event}")

    if reasons:
        return {"status": STATUS_MISMATCH, "reasons": reasons}

    coverage = populations.get("coverage", {})
    missing = coverage.get("stage_reached_without_entry_date", {}) or {}
    if any((missing.get(e) or 0) > 0 for e in FUNNEL_EVENTS):
        reasons.append(REASON_MISSING_STAGE_DATE)
    if (coverage.get("unknown_lifecycle_stage_contacts") or 0) > 0:
        reasons.append(REASON_UNKNOWN_LIFECYCLE_STAGE)

    if reasons:
        return {"status": STATUS_PARTIAL, "reasons": reasons}
    return {"status": STATUS_RECONCILED, "reasons": []}


# ── Public entry point ───────────────────────────────────────────────────────
def build(
    window_type: str,
    window_key: str,
    *,
    scope: str = SCOPE_ALL_SOURCE,
    event: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Canonical CRM funnel for one window. Read-only.

    ``window_type`` is 'business' or 'evidence' — the two vocabularies are
    resolved by the SHARED resolver (PR-ADS-152 §5) and never merged, and this
    service never invents a third rolling implementation of the same label.
    """
    if scope not in ORDERED_SCOPES:
        raise ValueError(f"Unknown scope '{scope}'")
    if event is not None and not is_valid_event(event):
        raise ValueError(f"Unknown funnel event '{event}'")

    window = resolve_window_contract(window_type, window_key, now=now)
    start, end = window["start"], window["end"]

    from db import crm_funnel_repository as repo  # noqa: PLC0415

    fetched = repo.fetch_funnel_contacts(start, end)
    available = bool(fetched.get("available"))
    rows = fetched.get("rows") or []

    resolver = _build_campaign_resolver(start, end)
    populations = build_populations(rows, start, end, campaign_resolver=resolver)

    counts = populations["counts"]
    status = reconciliation_status(populations, available=available)

    events_payload = {}
    for funnel_event in FUNNEL_EVENTS:
        if event is not None and funnel_event != event:
            continue
        definition = event_definition(funnel_event)
        events_payload[funnel_event] = {
            **definition,
            "scope": scope,
            "scope_label": SCOPE_LABELS[scope],
            "count": counts[funnel_event][scope] if available else None,
            "counts_by_scope": counts[funnel_event] if available else None,
            "available": available,
        }

    sync_state = _sync_state_block()

    return {
        "available": available,
        "window": {
            "window_type": window["window_type"],
            "window_key": window["window_key"],
            "start_date": window["start_date"],
            "end_date": window["end_date"],
        },
        "scope": scope,
        "scope_label": SCOPE_LABELS[scope],
        "canonical_source": FUNNEL_SOURCE,
        "table": FUNNEL_TABLE,
        "dedup_key": FUNNEL_DEDUP_KEY,
        "rule_version": LIFECYCLE_RULE_VERSION,
        "events": events_payload,
        "counts_by_scope": counts if available else None,
        "conversions": build_conversions(populations, scope) if available else None,
        "coverage": populations["coverage"] if available else None,
        "reconciliation": status,
        "sync": sync_state,
        "notes": [
            "Funnel counts are stage-ENTRY events, not current lifecycle stage. "
            "A contact now at Customer still counts in its historical SQL cohort.",
            "A missing stage-entry timestamp is a coverage gap. Contact creation "
            "date is never substituted for a funnel event date.",
            "Lifecycle Customer is NOT revenue Customer — closed-won deal truth is "
            "defined separately (PR-ADS-153E).",
        ],
    }


def _build_campaign_resolver(start: date | None, end: date | None):
    """Reuse the canonical Google Ads campaign-identity resolver (PR-ADS-152 §2).

    Campaign attribution must resolve a REAL Google Ads campaign identity, never a
    bare non-empty label. When the identity contract cannot be consulted the
    resolver fails closed: campaign attribution is unavailable, never guessed.
    """
    from services.canonical_contact_outcome_service import (  # noqa: PLC0415
        _build_identity_resolver, unavailable_campaign_resolver,
    )

    try:
        resolver, identity_available = _build_identity_resolver(start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[crm_funnel] campaign identity resolver unavailable: %s", exc)
        return unavailable_campaign_resolver

    if not identity_available:
        # Fail closed: an unconsultable identity contract means campaign
        # attribution is UNAVAILABLE, never guessed from a bare campaign label.
        logger.info("[crm_funnel] campaign identity contract unavailable — "
                    "campaign/keyword scopes withheld")
        return unavailable_campaign_resolver
    return resolver


def _sync_state_block() -> dict:
    """Durable ingestion state for the canonical contact store (never in-memory)."""
    try:
        from db import writers as db_writers  # noqa: PLC0415

        state = db_writers.get_contact_funnel_sync_state("contacts")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[crm_funnel] sync state unavailable: %s", exc)
        state = None

    if not state:
        return {
            "available": False,
            "bootstrap_status": "unavailable",
            "last_modified_watermark": None,
            "last_incremental_at": None,
        }
    return {
        "available": True,
        "bootstrap_status": state.get("bootstrap_status"),
        "bootstrap_started_at": _iso(state.get("bootstrap_started_at")),
        "bootstrap_completed_at": _iso(state.get("bootstrap_completed_at")),
        "last_modified_watermark": _iso(state.get("last_modified_watermark")),
        "last_incremental_at": _iso(state.get("last_incremental_at")),
        "contacts_seen": state.get("contacts_seen"),
        "last_error": state.get("last_error"),
    }


def _iso(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Re-exported so consumers name the window vocabulary explicitly.
__all__ = [
    "WINDOW_BUSINESS", "WINDOW_EVIDENCE",
    "SCOPE_ALL_SOURCE", "SCOPE_GOOGLE_ADS_SOURCE",
    "SCOPE_CAMPAIGN_ATTRIBUTABLE", "SCOPE_KEYWORD_ATTRIBUTABLE",
    "ORDERED_SCOPES", "FUNNEL_EVENTS",
    "EVENT_MQL", "EVENT_SQL", "EVENT_OPPORTUNITY", "EVENT_CUSTOMER",
    "build", "build_populations", "build_conversions", "cohort_conversion",
    "funnel_definitions", "event_definition", "scope_keys", "scopes_are_nested",
    "reconciliation_status",
]
