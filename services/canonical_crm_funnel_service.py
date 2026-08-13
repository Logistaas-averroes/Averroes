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
    EVENT_LEAD,
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
    stage_label,
)
from analysis.mql_status_taxonomy import ALL_CATEGORIES as ALL_MQL_CATEGORIES
from analysis.source_classification import (
    GROUP_GOOGLE_ADS,
    GROUP_LABELS,
    classify_source,
    classify_source_taxonomy,
)
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

# Scopes that cannot be computed without a successfully-consulted Google Ads
# campaign-identity contract. When it is unavailable these are UNAVAILABLE
# (null) — never False membership and never a zero count.
IDENTITY_DEPENDENT_SCOPES = (
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
REASON_CAMPAIGN_IDENTITY_UNAVAILABLE = "campaign_identity_unavailable"

# Why a contact row does (or does not) carry Google Ads campaign/keyword labels.
# ``hs_analytics_source_data_1`` / ``_data_2`` are HubSpot Original Source
# Drill-Down fields. They carry Google Ads campaign / keyword semantics ONLY for
# Paid Search contacts; for Organic, Paid Social, Email, Referral, Offline and
# Event contacts they hold entirely different drill-down text, so labelling them
# "Campaign" / "Keyword" would fabricate an attribution that does not exist.
CAMPAIGN_SEMANTICS_GOOGLE_ADS = "google_ads_campaign"
CAMPAIGN_SEMANTICS_NOT_APPLICABLE = REASON_NOT_GOOGLE_ADS
CAMPAIGN_SEMANTICS_UNAVAILABLE = REASON_CAMPAIGN_IDENTITY_UNAVAILABLE
CAMPAIGN_SEMANTICS_UNRESOLVED = "campaign_identity_unresolved"

# Acquisition groups accepted as a population filter (the PR-ADS-117 taxonomy).
ACQUISITION_GROUPS = tuple(GROUP_LABELS.keys())


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
def source_taxonomy(row: dict) -> dict:
    """The FULL canonical source classification for one canonical contact row.

    Reuses ``analysis.source_classification`` — the same group → channel →
    platform contract Revenue by Source uses — with the SAME evidence pair the
    durable ``contact_source_classification`` writer uses:

        primary = ``hs_analytics_source``            (Original Source)
        detail  = ``hs_analytics_source_data_1``     (Original Source Drill-Down)

    Passing the detail is required, not optional. Without it every
    ``Offline Sources`` contact collapses to the Offline group, when the
    drill-down is exactly what routes SalesNash / Events to Other Paid and
    reseller / referral / direct email to Organic. Dropping it made the Leads
    page classify the same contact differently from Revenue by Source.

    This is not a second taxonomy: no rule is defined here. Unlike the legacy
    ``leads`` table (which stores ``campaign_name``, not the drill-down, which is
    why PR-ADS-152 had to pass ``None``), the canonical contact store persists
    the real HubSpot drill-down field, so the full contract is available.
    Campaign name is still NEVER used to infer a source.
    """
    return classify_source_taxonomy(
        row.get("hs_analytics_source"), row.get("hs_analytics_source_data_1"))


def derive_acquisition_group(row: dict) -> str:
    """Acquisition group from the contact's own HubSpot source evidence."""
    return source_taxonomy(row)["source_group"]


def campaign_identity(row: dict, resolver, identity_available: bool) -> dict:
    """Google Ads campaign / keyword labels, or an explicit reason there are none.

    A label is published ONLY where Google Ads semantics are proven:

      * the contact's acquisition group is Google Ads (Paid Search), AND
      * the Google Ads campaign-identity contract was consulted, AND
      * the drill-down label resolved to a real Google Ads campaign.

    Anything else returns ``None`` with a semantics code. A non-Google contact is
    ``not_google_ads_source`` — its drill-down text is real evidence, but it is
    not a campaign, and it is surfaced through the source taxonomy instead.
    """
    if derive_acquisition_group(row) != GROUP_GOOGLE_ADS:
        return {"campaign": None, "keyword": None, "available": False,
                "semantics": CAMPAIGN_SEMANTICS_NOT_APPLICABLE}
    if not identity_available:
        return {"campaign": None, "keyword": None, "available": False,
                "semantics": CAMPAIGN_SEMANTICS_UNAVAILABLE}

    label = row.get("hs_analytics_source_data_1")
    resolved, _reason = resolver(label)
    if not resolved:
        return {"campaign": None, "keyword": None, "available": False,
                "semantics": CAMPAIGN_SEMANTICS_UNRESOLVED}

    keyword = (row.get("hs_analytics_source_data_2") or "").strip() or None
    return {"campaign": label, "keyword": keyword, "available": True,
            "semantics": CAMPAIGN_SEMANTICS_GOOGLE_ADS}


def default_campaign_resolver(campaign_name) -> tuple[bool, str | None]:
    """Fallback resolver used when the Google Ads identity contract is not
    consulted (pure tests). Delegates to the PR-ADS-152 safe-campaign rule."""
    from services.canonical_contact_outcome_service import (  # noqa: PLC0415
        campaign_disqualifier, is_safe_campaign,
    )
    if is_safe_campaign(campaign_name):
        return True, None
    return False, campaign_disqualifier(campaign_name)


def _contact_scopes(row: dict, resolver, identity_available: bool = True) -> dict:
    """Scope membership flags for ONE contact. Scope is a property of the
    contact's acquisition evidence and applies identically to every funnel event
    that contact produced — attribution creates subsets, it never redefines the
    underlying event (Minimum Viable Truth rule 6).

    When the Google Ads campaign-identity contract could not be consulted, the
    identity-dependent scopes are ``None`` (UNAVAILABLE), never ``False``.
    Collapsing "we could not check" into "not a member" would render an
    unknowable subset as a confident zero.
    """
    group = derive_acquisition_group(row)
    is_google_ads = group == GROUP_GOOGLE_ADS

    if not identity_available:
        return {
            "acquisition_group": group,
            SCOPE_ALL_SOURCE: True,
            SCOPE_GOOGLE_ADS_SOURCE: is_google_ads,
            SCOPE_CAMPAIGN_ATTRIBUTABLE: None,
            SCOPE_KEYWORD_ATTRIBUTABLE: None,
            "campaign_block_reason": REASON_CAMPAIGN_IDENTITY_UNAVAILABLE,
        }

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
    identity_available: bool = True,
    acquisition_group: str | None = None,
) -> dict:
    """Build canonical funnel event populations from raw contact rows. Pure.

    ``acquisition_group`` restricts the population to one acquisition group, so
    the funnel counts, the cohort-safe conversions, the coverage diagnostics and
    the contact rows all describe THE SAME population. The page must never show
    "Source = Organic" beside a headline that silently means all sources.

    ``identity_available`` reports whether the Google Ads campaign-identity
    contract was successfully consulted. When it is False the campaign- and
    keyword-attributable counts are ``None`` (unavailable) rather than 0, and
    the broader ``all_source`` / ``google_ads_source`` counts remain fully
    available — an attribution outage must not suppress CRM funnel truth.

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

        scopes = _contact_scopes(raw, resolver, identity_available)
        if acquisition_group and scopes["acquisition_group"] != acquisition_group:
            # Filtered out before any counting, so counts, conversions and
            # coverage all describe the selected population.
            continue
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
        event: _event_counts(events[event], identity_available)
        for event in FUNNEL_EVENTS
    }

    coverage = {
        "acquisition_group": acquisition_group,
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
        "identity_available": identity_available,
    }


def _event_counts(population: list[dict], identity_available: bool) -> dict:
    """Per-scope counts for one event population.

    An identity-dependent scope is ``None`` when the campaign-identity contract
    could not be consulted. Summing membership there would publish 0, which
    reads as "no campaign-attributable contacts" rather than "we could not tell".
    """
    counts = {}
    for scope in ORDERED_SCOPES:
        if not identity_available and scope in IDENTITY_DEPENDENT_SCOPES:
            counts[scope] = None
            continue
        counts[scope] = sum(1 for c in population if c["scopes"][scope])
    return counts


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
def scope_keys(population: list[dict], scope: str):
    """Contact-id set for a named scope within one event population.

    Returns ``None`` when the scope is UNAVAILABLE for this population (the
    campaign-identity contract could not be consulted) — never an empty set,
    which a caller could mistake for a proven zero.
    """
    if scope not in ORDERED_SCOPES:
        raise ValueError(f"Unknown scope '{scope}'")
    if any(c["scopes"].get(scope) is None for c in population):
        return None
    return {c["contact_id"] for c in population if c["scopes"][scope]}


def scopes_are_nested(population: list[dict]) -> bool:
    """Prove keyword ⊆ campaign ⊆ google_ads ⊆ all_source for one population.

    Unavailable scopes are skipped: an unknown subset cannot violate nesting,
    and asserting over it would turn an attribution outage into a false
    ``mismatch``.
    """
    sets = [scope_keys(population, scope) for scope in ORDERED_SCOPES]
    available = [s for s in sets if s is not None]
    return all(available[i + 1] <= available[i]
               for i in range(len(available) - 1))


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
    # An attribution outage never makes the funnel "reconciled": the narrow
    # scopes are unknown, and saying otherwise would imply they were checked.
    if populations.get("identity_available") is False:
        reasons.append(REASON_CAMPAIGN_IDENTITY_UNAVAILABLE)

    if reasons:
        return {"status": STATUS_PARTIAL, "reasons": reasons}
    return {"status": STATUS_RECONCILED, "reasons": []}


# ── Public entry point ───────────────────────────────────────────────────────
def build(
    window_type: str,
    window_key: str,
    *,
    scope: str = SCOPE_ALL_SOURCE,
    acquisition_group: str | None = None,
    event: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Canonical CRM funnel for one window. Read-only.

    ``window_type`` is 'business' or 'evidence' — the two vocabularies are
    resolved by the SHARED resolver (PR-ADS-152 §5) and never merged, and this
    service never invents a third rolling implementation of the same label.

    ``acquisition_group`` narrows the whole aggregate — counts, cohort-safe
    conversions and coverage — to one acquisition group, so a page that offers a
    Source selector reports ONE population rather than a filtered table beside an
    all-source headline.
    """
    if scope not in ORDERED_SCOPES:
        raise ValueError(f"Unknown scope '{scope}'")
    if acquisition_group is not None and acquisition_group not in ACQUISITION_GROUPS:
        raise ValueError(f"Unknown acquisition group '{acquisition_group}'")
    if event is not None and not is_valid_event(event):
        raise ValueError(f"Unknown funnel event '{event}'")

    window = resolve_window_contract(window_type, window_key, now=now)
    start, end = window["start"], window["end"]

    from db import crm_funnel_repository as repo  # noqa: PLC0415

    fetched = repo.fetch_funnel_contacts(start, end)
    available = bool(fetched.get("available"))
    rows = fetched.get("rows") or []

    resolver, identity_available = _build_campaign_resolver(start, end)
    populations = build_populations(
        rows, start, end, campaign_resolver=resolver,
        identity_available=identity_available,
        acquisition_group=acquisition_group)

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
        "acquisition_group": acquisition_group,
        "acquisition_group_label": (GROUP_LABELS[acquisition_group]
                                    if acquisition_group else "All sources"),
        "campaign_identity_available": identity_available,
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
        "notes": ([] if identity_available else [
            "Google Ads campaign identity could not be consulted: "
            "campaign-attributable and keyword-attributable counts are "
            "UNAVAILABLE (null), not zero. All-source and Google Ads-source "
            "counts are unaffected.",
        ]) + ([] if not acquisition_group else [
            f"Filtered to the {GROUP_LABELS[acquisition_group]} acquisition group: "
            "every count, conversion and coverage figure below describes that "
            "population only.",
        ]) + [
            "Funnel counts are stage-ENTRY events, not current lifecycle stage. "
            "A contact now at Customer still counts in its historical SQL cohort.",
            "A missing stage-entry timestamp is a coverage gap. Contact creation "
            "date is never substituted for a funnel event date.",
            "Lifecycle Customer is NOT revenue Customer — closed-won deal truth is "
            "defined separately (PR-ADS-153E).",
        ],
    }


# ── PR-ADS-153C — server-side contact rows for the canonical Leads page ──────
def resolve_source_pair_allowlist(acquisition_group: str | None, source_pairs: list):
    """Which ``(Original Source, Drill-Down)`` PAIRS belong to an acquisition group.

    The classifier reads both fields — ``Offline Sources`` alone is ambiguous and
    only its drill-down decides between Other Paid (SalesNash / Events), Organic
    (reseller / referral / direct email) and Offline. So the allow-list must be
    over pairs, not over the primary source alone; collapsing it to the primary
    would re-introduce exactly the evidence loss this fixes.

    The classifier is pure Python, so it is applied ONCE to the small set of
    distinct pairs held in the store and the matches become a SQL allow-list.
    Filtering therefore stays server-side (so pagination is correct) without
    re-implementing the taxonomy in SQL.

    Returns ``None`` for "no constraint".
    """
    if not acquisition_group:
        return None
    return [(primary, detail) for primary, detail in source_pairs
            if classify_source(primary, detail) == acquisition_group]


def resolve_campaign_allowlist(raw_campaigns: list, resolver):
    """Which campaign labels resolve to a real Google Ads campaign identity.

    Campaign attribution depends only on the campaign LABEL, never on the
    individual contact, so the resolver runs once per distinct label.
    """
    return [label for label in raw_campaigns if resolver(label)[0]]


def contacts(
    window_type: str,
    window_key: str,
    *,
    event: str = EVENT_SQL,
    scope: str = SCOPE_ALL_SOURCE,
    acquisition_group: str | None = None,
    operational_status: str | None = None,
    company_query: str | None = None,
    page: int = 1,
    page_size: int = 50,
    now: datetime | None = None,
) -> dict:
    """One bounded page of contacts for a funnel event. Read-only.

    Rows are the contacts whose stage-entry date for ``event`` falls inside the
    window — NOT the contacts currently at that lifecycle stage. A contact now at
    Customer is still returned by the SQL view for the window it entered Sales
    Qualified Lead.

    Never returns an email address.
    """
    if scope not in ORDERED_SCOPES:
        raise ValueError(f"Unknown scope '{scope}'")
    if acquisition_group is not None and acquisition_group not in ACQUISITION_GROUPS:
        raise ValueError(f"Unknown acquisition group '{acquisition_group}'")
    if not is_valid_event(event):
        raise ValueError(f"Unknown funnel event '{event}'")

    window = resolve_window_contract(window_type, window_key, now=now)
    start, end = window["start"], window["end"]

    from db import crm_funnel_repository as repo  # noqa: PLC0415

    resolver, identity_available = _build_campaign_resolver(start, end)

    # A narrow scope with no identity contract is UNAVAILABLE — never an empty
    # page, which would read as a proven zero.
    if scope in IDENTITY_DEPENDENT_SCOPES and not identity_available:
        return {
            "available": False,
            "reason": REASON_CAMPAIGN_IDENTITY_UNAVAILABLE,
            "window": _window_payload(window),
            "event": event,
            "scope": scope,
            "scope_label": SCOPE_LABELS[scope],
            "rows": [],
            "total": None,
            "page": page,
            "page_size": page_size,
            "campaign_identity_available": False,
        }

    facets = repo.fetch_distinct_facets()
    source_pairs = facets.get("source_pairs") or []
    raw_campaigns = facets.get("campaigns") or []

    pairs_in = resolve_source_pair_allowlist(acquisition_group, source_pairs)
    if scope == SCOPE_GOOGLE_ADS_SOURCE or scope in IDENTITY_DEPENDENT_SCOPES:
        google_pairs = resolve_source_pair_allowlist(GROUP_GOOGLE_ADS, source_pairs)
        pairs_in = (google_pairs if pairs_in is None
                    else [p for p in pairs_in if p in set(google_pairs)])

    campaigns_in = None
    if scope in IDENTITY_DEPENDENT_SCOPES:
        campaigns_in = resolve_campaign_allowlist(raw_campaigns, resolver)

    fetched = repo.fetch_funnel_contact_page(
        event, start, end,
        source_pairs_in=pairs_in,
        campaigns_in=campaigns_in,
        require_keyword=(scope == SCOPE_KEYWORD_ATTRIBUTABLE),
        operational_status=operational_status,
        company_query=company_query,
        page=page,
        page_size=page_size,
    )

    definition = event_definition(event)
    return {
        "available": bool(fetched.get("available")),
        "window": _window_payload(window),
        "event": event,
        "event_label": definition["label"],
        "event_date_property": definition["event_date_property"],
        "event_date_column": definition["event_date_column"],
        "scope": scope,
        "scope_label": SCOPE_LABELS[scope],
        "campaign_identity_available": identity_available,
        "acquisition_group": acquisition_group,
        "operational_status": operational_status,
        "acquisition_group_label": (GROUP_LABELS[acquisition_group]
                                    if acquisition_group else "All sources"),
        "rows": [_contact_row_payload(r, event, identity_available, resolver)
                 for r in (fetched.get("rows") or [])],
        "total": fetched.get("total") if fetched.get("available") else None,
        "page": fetched.get("page", page),
        "page_size": fetched.get("page_size", page_size),
        "has_more": fetched.get("has_more", False),
        "order_by": fetched.get("order_by"),
        "sync": _sync_state_block(),
    }


def _window_payload(window: dict) -> dict:
    return {
        "window_type": window["window_type"],
        "window_key": window["window_key"],
        "start_date": window["start_date"],
        "end_date": window["end_date"],
    }


def _contact_row_payload(row: dict, event: str, identity_available: bool,
                         resolver=None) -> dict:
    """Admin-safe contact row. Company + HubSpot contact id only — NEVER email.

    Campaign and keyword are published ONLY where Google Ads semantics are proven
    (see ``campaign_identity``). For every other acquisition group they are
    ``None`` with ``campaign_semantics = 'not_google_ads_source'``, and the row
    instead carries the canonical source taxonomy — group, channel, platform, and
    the neutral raw drill-down — which is what that contact's evidence actually
    means.
    """
    stage_dates = {
        e: _iso(row.get(EVENT_DATE_COLUMN[e])) for e in FUNNEL_EVENTS
    }
    taxonomy = source_taxonomy(row)
    identity = campaign_identity(
        row, resolver or default_campaign_resolver, identity_available)
    return {
        "contact_id": row.get("contact_id"),
        "company": row.get("company"),
        "lifecycle_stage": normalize_lifecycle_stage(row.get("lifecycle_stage")),
        "lifecycle_stage_label": stage_label(row.get("lifecycle_stage")),
        "event_date": stage_dates.get(event),
        "stage_dates": stage_dates,
        "mql_status": row.get("mql_status"),
        "mql_status_category": row.get("mql_status_category"),
        "acquisition_group": taxonomy["source_group"],
        "acquisition_group_label": taxonomy["source_group_label"],
        "source_channel": taxonomy["source_channel"],
        "source_channel_label": taxonomy["source_channel_label"],
        "source_platform": taxonomy["source_platform"],
        "source_platform_label": taxonomy["source_platform_label"],
        "attribution_quality": taxonomy["attribution_quality"],
        "acquisition_source_raw": row.get("hs_analytics_source"),
        # Neutral name: this is the HubSpot Original Source Drill-Down, which is
        # a Google Ads campaign only for Paid Search contacts.
        "source_detail_raw": row.get("hs_analytics_source_data_1"),
        "campaign": identity["campaign"],
        "campaign_available": identity["available"],
        "campaign_semantics": identity["semantics"],
        "keyword": identity["keyword"],
        "country": row.get("ip_country") or row.get("country"),
        "owner_id": row.get("owner_id"),
        "has_gclid": bool(row.get("has_gclid")),
    }


def operational_status_breakdown(
    window_type: str, window_key: str, *, event: str = EVENT_LEAD,
    acquisition_group: str | None = None,
    now: datetime | None = None) -> dict:
    """Counts by operational ``mql_status_category`` for one event window.

    These are WORKING statuses, not funnel stages — the Disqualified / Other view
    is deliberately kept distinct from the five canonical stages.

    ``acquisition_group`` applies the SAME population filter as the funnel and
    the contact rows, using the same pre-resolved pair allow-list, so the Source
    selector means one thing everywhere on the page.
    """
    if not is_valid_event(event):
        raise ValueError(f"Unknown funnel event '{event}'")
    if acquisition_group is not None and acquisition_group not in ACQUISITION_GROUPS:
        raise ValueError(f"Unknown acquisition group '{acquisition_group}'")
    window = resolve_window_contract(window_type, window_key, now=now)

    from db import crm_funnel_repository as repo  # noqa: PLC0415

    pairs_in = None
    if acquisition_group:
        facets = repo.fetch_distinct_facets()
        pairs_in = resolve_source_pair_allowlist(
            acquisition_group, facets.get("source_pairs") or [])

    fetched = repo.fetch_operational_status_counts(
        event, window["start"], window["end"], source_pairs_in=pairs_in)
    return {
        "available": bool(fetched.get("available")),
        "window": _window_payload(window),
        "event": event,
        "acquisition_group": acquisition_group,
        "acquisition_group_label": (GROUP_LABELS[acquisition_group]
                                    if acquisition_group else "All sources"),
        "counts": fetched.get("counts") if fetched.get("available") else None,
        "categories": list(ALL_MQL_CATEGORIES),
    }


def _build_campaign_resolver(start: date | None, end: date | None):
    """Reuse the canonical Google Ads campaign-identity resolver (PR-ADS-152 §2).

    Returns ``(resolver, identity_available)``. Availability is propagated — not
    swallowed — so the caller can render the identity-dependent scopes as
    unavailable rather than zero.

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
        return unavailable_campaign_resolver, False

    if not identity_available:
        # Fail closed: an unconsultable identity contract means campaign
        # attribution is UNAVAILABLE, never guessed from a bare campaign label
        # and never rendered as a confident zero.
        logger.info("[crm_funnel] campaign identity contract unavailable — "
                    "campaign/keyword scopes withheld")
        return unavailable_campaign_resolver, False
    return resolver, True


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
    "contacts", "operational_status_breakdown",
    "source_taxonomy", "derive_acquisition_group", "campaign_identity",
    "resolve_source_pair_allowlist", "ACQUISITION_GROUPS",
    "funnel_definitions", "event_definition", "scope_keys", "scopes_are_nested",
    "reconciliation_status",
]
