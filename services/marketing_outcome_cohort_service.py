"""
services/marketing_outcome_cohort_service.py

PR-ADS-161B — canonical marketing outcome cohorts.

Two questions that sound alike
------------------------------
1. **Acquisition cohort** — "of the contacts acquired during this period, how
   many have reached SQL as of now?" Membership is ``contact_created_at``. The
   outcome is whatever canonical lifecycle evidence proves *today*. This is the
   question a paid-marketing decision asks, and the one Campaign Evidence and
   its CPQL answer.
2. **Lifecycle event** — "how many contacts entered SQL during this period?"
   Membership is the SQL-entry date itself. It needs an exact timestamp for
   every contact, and remains governed by the lifecycle coverage gate
   (``analysis.sql_publication``, ``scripts/audit_sql_coverage_gate.py``).

They are different populations. A contact created in March that became an SQL
in June is in March's acquisition cohort and June's lifecycle events. Nothing in
this module answers question 2, and nothing here may be relabelled as it.

Why the cohort can count contacts the event metric cannot
---------------------------------------------------------
A contact whose current stage is ``customer`` with no recorded SQL-entry date
*did* reach SQL — the stage proves the transition — but *when* is unknown. The
event metric must withhold: it cannot place the contact in any window. The
cohort does not need to: its window is decided by ``created_at``, which is
known. The undated contact is therefore counted, once, in the cohort of the
period it was acquired in, and its missing timestamp is disclosed as a
lifecycle-event coverage gap. **No SQL-entry timestamp is ever produced,
estimated or substituted** — not from creation, not from the coverage boundary,
not from a sync time. The cohort simply never asks for one.

What proves SQL
---------------
A distinct contact has reached SQL when canonical evidence proves any of:

* a direct HubSpot SQL-entry timestamp (``hs_v2_date_entered_salesqualifiedlead``);
* a recovered SQL transition from HubSpot lifecycle-stage history;
* a current lifecycle stage that implies SQL —
  ``analysis.crm_lifecycle.stages_implying_event(EVENT_SQL)``, the repository's
  one stage-rank rule (``salesqualifiedlead``, ``opportunity``, ``customer``,
  ``evangelist``).

This is a UNION, and deliberately so. ``db.crm_funnel_repository.
fetch_sql_coverage_population`` counts only the third (current stage), because
its question is "whose SQL date is missing?". A contact whose SQL-entry date
HubSpot recorded and whose stage later moved backwards still reached SQL — the
event is proven by its timestamp, not by its current stage
(``analysis/crm_lifecycle.py``'s doctrine). The two counts are expected to
differ by exactly those contacts, and the audit reports the split.

Attribution
-----------
Every cohort SQL lands in exactly one bucket, and none is dropped:

* ``campaign``              — Google Ads sourced, mapped to one campaign through
                              Campaign Evidence's own identity resolver;
* ``unattributed_google_ads`` — Google Ads sourced, no campaign it can be placed
                              on (missing, pseudo, unmapped or conflicting label);
* ``excluded_non_google``   — not Google Ads sourced, or an approved mapping says
                              the label is not a Google Ads campaign.

``google_ads = campaign + unattributed_google_ads`` and
``all_sources = google_ads + excluded_non_google`` hold by construction, and the
audit re-proves both from raw rows.

Purity
------
Everything above ``build_window_outcomes`` is pure: plain dict rows in, plain
dicts out. Only ``build_window_outcomes`` and ``lifecycle_event_disclosure``
read the database, and only through read-only repositories. No HubSpot call,
no Google Ads call, no write of any kind.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from analysis.account_time import ACCOUNT_TZ
from analysis.crm_lifecycle import EVENT_SQL, normalize_lifecycle_stage, stages_implying_event
from analysis.revenue_scope import has_campaign, is_google_ads_attributed
from analysis.source_classification import GROUP_GOOGLE_ADS, GROUP_UNCLASSIFIED, classify_source
from services.canonical_contact_outcome_service import campaign_disqualifier

logger = logging.getLogger(__name__)

# ── Metric families (the API contract) ───────────────────────────────────────
METRIC_FAMILY_COHORT = "acquisition_cohort_outcomes"
METRIC_FAMILY_LIFECYCLE_EVENTS = "lifecycle_stage_events"

WINDOW_BASIS_COHORT = "contact_created_at"
WINDOW_BASIS_LIFECYCLE_EVENTS = "date_entered_sql"
WINDOW_BASIS_DEALS = "primary_contact.contact_created_at"

OUTCOME_BASIS_SQL = "latest_canonical_lifecycle_evidence"
OUTCOME_BASIS_DEALS = "canonical_deal_ledger.hs_is_closed_won"

DEDUP_KEY = "contact_id"
DEAL_DEDUP_KEY = "deal_id"
#: The documented fallback identity, used only when ``contact_id`` is blank.
#: Prefixed so it can never collide with a real HubSpot id or with the legacy
#: ``leads`` table's ``id:`` fallback.
FALLBACK_KEY_PREFIX = "funnel_row:"

COHORT_BASIS_LABEL = (
    "SQLs from contacts created during this period, measured as of the data "
    "watermark")

# ── SQL proof, strongest first ───────────────────────────────────────────────
PROOF_DIRECT = "direct_sql_entry_timestamp"
PROOF_RECOVERED = "recovered_lifecycle_history"
PROOF_STAGE = "lifecycle_stage_implies_sql"
PROOF_ORDER = (PROOF_DIRECT, PROOF_RECOVERED, PROOF_STAGE)
#: Proofs that carry an exact SQL-entry timestamp. A stage-only proof does not.
TIMESTAMPED_PROOFS = frozenset({PROOF_DIRECT, PROOF_RECOVERED})

_STAGES_IMPLYING_SQL = frozenset(stages_implying_event(EVENT_SQL))

# ── Attribution buckets ──────────────────────────────────────────────────────
BUCKET_CAMPAIGN = "campaign"
BUCKET_UNATTRIBUTED = "unattributed_google_ads"
BUCKET_EXCLUDED = "excluded_non_google"
BUCKET_AMBIGUOUS = "ambiguous"          # deals only: contacts disagree on source
CONTACT_BUCKETS = (BUCKET_CAMPAIGN, BUCKET_UNATTRIBUTED, BUCKET_EXCLUDED)
DEAL_BUCKETS = CONTACT_BUCKETS + (BUCKET_AMBIGUOUS,)

REASON_UNMAPPED_LABEL = "unmapped_campaign_label"
REASON_CONFLICTING_ROWS = "conflicting_attribution_across_rows"
REASON_NON_GOOGLE_SOURCE = "non_google_source"
#: Blank or unrecognised original source: excluded because nothing proves Google
#: Ads bought it, NOT because another channel is proven.
REASON_SOURCE_UNCLASSIFIED = "original_source_unclassified"
REASON_LABEL_NOT_GOOGLE_ADS = "label_mapped_not_google_ads"
REASON_DEAL_WITHOUT_CAMPAIGN = "deal_without_campaign"
REASON_DEAL_SOURCE_AMBIGUOUS = "deal_contacts_disagree_on_source"
REASON_DEAL_LABEL_CONTRADICTS_CLICK = "deal_gclid_but_label_mapped_not_google_ads"

UNPLACEABLE_NO_PRIMARY_CONTACT = "no_primary_contact"
UNPLACEABLE_CONTACT_NOT_IN_FUNNEL = "primary_contact_not_in_canonical_funnel"
UNPLACEABLE_CONTACT_NO_CREATED_AT = "primary_contact_has_no_created_at"
UNPLACEABLE_NO_DEAL_ID = "deal_without_deal_id"

# ── Publication vocabulary ───────────────────────────────────────────────────
STATUS_PUBLISHED = "published"
STATUS_WITHHELD = "withheld"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_APPLICABLE = "not_applicable"     # CPQL over zero SQLs

CPQL_REASON_SPEND_UNAVAILABLE = "spend_unavailable"
CPQL_REASON_FX_INCOMPLETE = "spend_usd_unavailable_fx_incomplete"
CPQL_REASON_COHORT_UNAVAILABLE = "cohort_unavailable"
CPQL_REASON_SOURCE_NOT_FRESH = "source_not_fresh"
CPQL_REASON_SOURCE_FRESHNESS_UNKNOWN = "source_freshness_unknown"
CPQL_REASON_ZERO_SQLS = "zero_cohort_sqls"
CPQL_REASON_ZERO_SPEND = "zero_window_spend"
CPQL_REASON_ATTRIBUTION_UNAVAILABLE = "campaign_attribution_unavailable"

# Why the cohort SQL count itself is not published. The freshness reasons that
# withhold it are passed through verbatim from analysis.sql_coverage_freshness
# (e.g. ``source_bootstrap_incomplete``), never collapsed into one "stale".
SQL_REASON_FUNNEL_UNREADABLE = "canonical_funnel_unreadable"
SQL_REASON_RECONCILIATION_FAILED = "cohort_reconciliation_failed"
SQL_REASON_WATERMARK_UNKNOWN = "data_watermark_unknown"

#: The only freshness verdicts under which the cohort's population is PROVEN
#: complete as of a known watermark. ``source_fresh``: current.
#: ``source_stale``: complete as of an older watermark, published WITH that
#: watermark. Every other verdict — no sync state, a bootstrap still arriving,
#: a failed or never-run incremental, a record without provenance — means the
#: population itself is not proven, so a count over it is partial, and partial
#: is not success. Listed, not derived: a new freshness reason is withheld until
#: someone decides otherwise.
PUBLISHABLE_FRESHNESS_REASONS = frozenset({"source_fresh", "source_stale"})

COVERAGE_COMPLETE = "cohort_complete"
COVERAGE_EVENT_GAPS = "cohort_complete_event_timestamps_incomplete"
COVERAGE_UNAVAILABLE = "unavailable"
#: Rows were read, but the population is not proven complete (see
#: ``sql_publication``) — a count exists and is not a total.
COVERAGE_NOT_PROVEN = "cohort_population_not_proven"


# ═════════════════════════════════════════════════════════════════════════════
# Window instants
# ═════════════════════════════════════════════════════════════════════════════
def window_instants(start: date | None, end: date) -> tuple[datetime | None, datetime]:
    """Resolve inclusive account-local calendar days to ``[start_at, end_before)``.

    Spend rows are Google Ads account-local days (``analysis.account_time``).
    Cohort membership is bounded on the SAME days, so a CPQL's numerator and
    denominator describe one window. The legacy lead read bounded on midnight
    in the database session's timezone instead, which drifts from spend by the
    UK's daylight-saving offset; this does not.
    """
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        tz = ZoneInfo(ACCOUNT_TZ)
    except Exception:  # noqa: BLE001 - tz database unavailable
        tz = timezone.utc
    start_at = (None if start is None
                else datetime.combine(start, time.min, tzinfo=tz).astimezone(timezone.utc))
    end_before = datetime.combine(end + timedelta(days=1), time.min,
                                  tzinfo=tz).astimezone(timezone.utc)
    return start_at, end_before


def _as_instant(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def in_window(created_at, start_at: datetime | None, end_before: datetime) -> bool:
    """Cohort membership. ``created_at`` alone decides; None is in no window."""
    instant = _as_instant(created_at)
    if instant is None:
        return False
    if start_at is not None and instant < start_at:
        return False
    return instant < end_before


# ═════════════════════════════════════════════════════════════════════════════
# Per-contact classification (pure)
# ═════════════════════════════════════════════════════════════════════════════
def sql_proof(row: dict) -> str | None:
    """The strongest canonical evidence that this contact reached SQL, or None.

    Reads three things and invents nothing. A stage-only proof says the
    transition happened; it carries no date, and none is attached.
    """
    if row.get("sql_entered_direct") is not None:
        return PROOF_DIRECT
    if row.get("sql_entered_recovered") is not None:
        return PROOF_RECOVERED
    if normalize_lifecycle_stage(row.get("lifecycle_stage")) in _STAGES_IMPLYING_SQL:
        return PROOF_STAGE
    return None


def contact_identity(row: dict) -> tuple[str, bool]:
    """``(dedup_key, used_fallback)``. ``contact_id`` unless genuinely blank."""
    cid = str(row.get("contact_id") or "").strip()
    if cid:
        return cid, False
    return f"{FALLBACK_KEY_PREFIX}{row.get('funnel_row_id')}", True


LabelResolver = Callable[[Any], tuple[str, Any]]


def contact_bucket(row: dict, resolve_label: LabelResolver) -> tuple[str, str | None, str | None]:
    """``(bucket, campaign_key, reason)`` for one contact.

    Google Ads sourcing is the contact's OWN HubSpot original source —
    ``classify_source``, as every canonical contact read decides it — never a
    campaign label. Campaign placement then goes through ``resolve_label``,
    which production binds to Campaign Evidence's ``_assign_lead`` so a cohort
    SQL lands on exactly the row the page's spend lands on.
    """
    group = classify_source(row.get("hs_analytics_source"), None)
    if group != GROUP_GOOGLE_ADS:
        # "Not proven Google Ads" is not "proven non-Google". A contact whose
        # original source is blank or unrecognised is excluded all the same —
        # nothing proves it was bought — but under its own reason, so the
        # exclusion is not read as evidence of another channel.
        return BUCKET_EXCLUDED, None, (REASON_SOURCE_UNCLASSIFIED
                                       if group == GROUP_UNCLASSIFIED
                                       else REASON_NON_GOOGLE_SOURCE)
    label = row.get("hs_analytics_source_data_1")
    disqualified = campaign_disqualifier(label)
    if disqualified is not None:
        return BUCKET_UNATTRIBUTED, None, disqualified
    kind, key = resolve_label(label)
    if kind == "google_ads":
        return BUCKET_CAMPAIGN, str(key), None
    if kind == "not_google_ads":
        return BUCKET_EXCLUDED, None, REASON_LABEL_NOT_GOOGLE_ADS
    return BUCKET_UNATTRIBUTED, f"unmatched:{key}", REASON_UNMAPPED_LABEL


# ═════════════════════════════════════════════════════════════════════════════
# The cohort (pure)
# ═════════════════════════════════════════════════════════════════════════════
def _new_slot(display=None) -> dict:
    return {"display_name": display, "contacts_acquired": 0, "sqls": 0,
            "sqls_missing_event_timestamp": 0}


def _bump(slot: dict, proof: str | None) -> None:
    slot["contacts_acquired"] += 1
    if proof is not None:
        slot["sqls"] += 1
        if proof not in TIMESTAMPED_PROOFS:
            slot["sqls_missing_event_timestamp"] += 1


def _merge_duplicates(rows: list[dict], resolve_label: LabelResolver) -> tuple[dict, str, str | None, str | None]:
    """Collapse several rows for one identity into one classified contact.

    The canonical table is UNIQUE on ``contact_id``, so this is defensive — but
    when it fires it must neither double-count nor guess:

    * SQL proof: the strongest across the rows. Any canonical row proving SQL
      proves the contact reached it.
    * attribution: kept only when every row agrees. Rows that disagree are
      ``unattributed`` with an explicit reason — never assigned to whichever
      row happened to come first, and never dropped.
    """
    proofs = [p for p in (sql_proof(r) for r in rows) if p is not None]
    proof = min(proofs, key=PROOF_ORDER.index) if proofs else None
    buckets = {contact_bucket(r, resolve_label) for r in rows}
    if len(buckets) == 1:
        bucket, key, reason = next(iter(buckets))
    elif all(b == BUCKET_EXCLUDED for b, _, _ in buckets):
        bucket, key, reason = BUCKET_EXCLUDED, None, REASON_CONFLICTING_ROWS
    else:
        bucket, key, reason = BUCKET_UNATTRIBUTED, None, REASON_CONFLICTING_ROWS
    return rows[0], proof, bucket, (key, reason)


def build_cohort(rows: list[dict], *, resolve_label: LabelResolver,
                 start_at: datetime | None, end_before: datetime) -> dict:
    """Classify a window's acquired contacts into proven outcomes and buckets.

    ``rows`` are ``fetch_acquisition_cohort_contacts`` rows. Membership is
    re-tested here rather than trusted from the query, so a repository that
    ever widened its predicate would show up as ``rows_outside_window`` instead
    of silently enlarging the cohort.
    """
    grouped: dict[str, list[dict]] = {}
    fallback_identities = 0
    rows_outside_window = 0
    for row in rows or []:
        if not in_window(row.get("created_at"), start_at, end_before):
            rows_outside_window += 1
            continue
        ident, used_fallback = contact_identity(row)
        if used_fallback and ident not in grouped:
            fallback_identities += 1
        grouped.setdefault(ident, []).append(row)

    duplicate_rows = sum(len(v) - 1 for v in grouped.values())

    by_campaign: dict[str, dict] = {}
    unattributed_by_label: dict[str, dict] = {}
    unattributed = _new_slot()
    excluded = _new_slot()
    google_ads = _new_slot()
    all_sources = _new_slot()
    unattributed_reasons: dict[str, int] = {}
    excluded_reasons: dict[str, int] = {}
    excluded_sqls_with_gclid = 0
    proof_counts = {p: 0 for p in PROOF_ORDER}
    sql_identities: dict[str, dict] = {}

    for ident in sorted(grouped):
        group = grouped[ident]
        if len(group) == 1:
            row = group[0]
            proof = sql_proof(row)
            bucket, key, reason = contact_bucket(row, resolve_label)
        else:
            row, proof, bucket, (key, reason) = _merge_duplicates(group, resolve_label)

        _bump(all_sources, proof)
        if proof is not None:
            proof_counts[proof] += 1
            sql_identities[ident] = {"proof": proof, "bucket": bucket,
                                     "campaign_key": key, "reason": reason}

        if bucket == BUCKET_EXCLUDED:
            _bump(excluded, proof)
            if proof is not None:
                excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
                if str(row.get("gclid") or "").strip():
                    excluded_sqls_with_gclid += 1
            continue

        _bump(google_ads, proof)
        if bucket == BUCKET_CAMPAIGN:
            _bump(by_campaign.setdefault(key, _new_slot()), proof)
        else:
            _bump(unattributed, proof)
            if proof is not None:
                unattributed_reasons[reason] = unattributed_reasons.get(reason, 0) + 1
            if key is not None:
                _bump(unattributed_by_label.setdefault(
                    key, _new_slot(row.get("hs_analytics_source_data_1"))), proof)

    unattributed_without_label_row = {
        field: unattributed[field] - sum(s[field] for s in unattributed_by_label.values())
        for field in ("contacts_acquired", "sqls", "sqls_missing_event_timestamp")
    }
    return {
        "all_sources": all_sources,
        "google_ads": google_ads,
        "by_campaign": by_campaign,
        "unattributed": {**unattributed, "by_reason": unattributed_reasons,
                         "by_label": unattributed_by_label,
                         "without_label_row": unattributed_without_label_row},
        "excluded_non_google": {**excluded, "by_reason": excluded_reasons,
                                "sqls_with_gclid": excluded_sqls_with_gclid},
        "proof_counts": proof_counts,
        "dedup": {"key": DEDUP_KEY, "duplicate_rows": duplicate_rows,
                  "fallback_identities": fallback_identities,
                  "fallback_key": f"{FALLBACK_KEY_PREFIX}<funnel_row_id>"},
        "rows_outside_window": rows_outside_window,
        "sql_identities": sql_identities,
    }


def reconcile_cohort(cohort: dict) -> list[str]:
    """Every identity the buckets must satisfy. Empty list = reconciled.

    Called by the service on every build and by the audit, so a bucket that
    starts leaking contacts fails loudly in both places.
    """
    problems: list[str] = []
    ga, un, ex, al = (cohort["google_ads"], cohort["unattributed"],
                      cohort["excluded_non_google"], cohort["all_sources"])
    mapped = {f: sum(s[f] for s in cohort["by_campaign"].values())
              for f in ("contacts_acquired", "sqls", "sqls_missing_event_timestamp")}
    for f in ("contacts_acquired", "sqls", "sqls_missing_event_timestamp"):
        if mapped[f] + un[f] != ga[f]:
            problems.append(f"google_ads.{f} {ga[f]} != campaigns {mapped[f]} "
                            f"+ unattributed {un[f]}")
        if ga[f] + ex[f] != al[f]:
            problems.append(f"all_sources.{f} {al[f]} != google_ads {ga[f]} "
                            f"+ excluded {ex[f]}")
        if un["without_label_row"][f] < 0:
            problems.append(f"unattributed label rows exceed the unattributed {f} total")
    if sum(cohort["proof_counts"].values()) != al["sqls"]:
        problems.append("proof counts do not sum to all-source SQLs")
    if len(cohort["sql_identities"]) != al["sqls"]:
        problems.append("distinct SQL identities != all-source SQL count")
    if sum(un["by_reason"].values()) != un["sqls"]:
        problems.append("unattributed reasons do not sum to unattributed SQLs")
    if sum(ex["by_reason"].values()) != ex["sqls"]:
        problems.append("excluded reasons do not sum to excluded SQLs")
    return problems


# ═════════════════════════════════════════════════════════════════════════════
# Closed-won deals (pure)
# ═════════════════════════════════════════════════════════════════════════════
def deal_bucket(deal: dict, resolve_label: LabelResolver) -> tuple[str, str | None, str | None]:
    """``(bucket, campaign_key, reason)`` for one canonical ledger row.

    Google Ads evidence is the scope lattice's own predicate
    (``analysis.revenue_scope.is_google_ads_attributed``: agreed source or a
    GCLID). Ambiguous contact evidence is NOT attribution and stays in its own
    bucket.
    """
    if is_google_ads_attributed(deal):
        if not has_campaign(deal):
            return BUCKET_UNATTRIBUTED, None, REASON_DEAL_WITHOUT_CAMPAIGN
        kind, key = resolve_label(deal.get("campaign_name_raw"))
        if kind == "google_ads":
            return BUCKET_CAMPAIGN, str(key), None
        if kind == "not_google_ads":
            # A recorded click says Google Ads; an approved mapping says the
            # label is not. Evidence that contradicts itself is not attribution.
            return BUCKET_AMBIGUOUS, None, REASON_DEAL_LABEL_CONTRADICTS_CLICK
        return BUCKET_UNATTRIBUTED, f"unmatched:{key}", REASON_UNMAPPED_LABEL
    if (deal.get("attribution_status") or "") == "ambiguous":
        return BUCKET_AMBIGUOUS, None, REASON_DEAL_SOURCE_AMBIGUOUS
    return BUCKET_EXCLUDED, None, REASON_NON_GOOGLE_SOURCE


def _new_deal_slot() -> dict:
    return {"deals": 0, "revenue_usd_known": 0.0, "revenue_usd_missing": 0}


def _bump_deal(slot: dict, deal: dict) -> None:
    slot["deals"] += 1
    revenue = deal.get("revenue_usd")
    if revenue is None:
        slot["revenue_usd_missing"] += 1
    else:
        slot["revenue_usd_known"] = round(slot["revenue_usd_known"] + float(revenue), 2)


def build_deal_outcomes(deals: list[dict], *, created_at_by_contact: dict,
                        resolve_label: LabelResolver,
                        start_at: datetime | None, end_before: datetime) -> dict:
    """Closed-won deals of the contacts acquired in the window, one per ``deal_id``.

    A deal joins a cohort through its ledger ``primary_contact_id`` — one
    contact per deal, so a deal associated with five contacts is still one
    deal. Placing it by ``deal_close_date`` instead would put an event-time
    metric on a cohort page, which is the definitional mix this module exists
    to prevent.

    A deal that cannot be placed in time is reported as unplaceable, by
    reason. It is never assigned a window from any other date.

    Limitation, disclosed rather than hidden: when several contacts share one
    deal with identical evidence, the ledger's ``primary_contact_id`` is the
    lowest contact id — a DISPLAY identity (``analysis.deal_truth`` rule 2),
    not the first contact acquired. Those contacts may have been created in
    different windows, so such a deal's window is the display contact's.
    ``placed_by_display_contact`` counts them.
    """
    seen: set[str] = set()
    placed_by_display_contact = 0
    duplicate_rows = 0
    unplaceable: dict[str, int] = {}
    buckets = {b: _new_deal_slot() for b in DEAL_BUCKETS}
    by_campaign: dict[str, dict] = {}
    reasons: dict[str, int] = {}

    for deal in deals or []:
        deal_id = str(deal.get("deal_id") or "").strip()
        if not deal_id:
            unplaceable[UNPLACEABLE_NO_DEAL_ID] = unplaceable.get(UNPLACEABLE_NO_DEAL_ID, 0) + 1
            continue
        if deal_id in seen:
            duplicate_rows += 1
            continue
        seen.add(deal_id)

        contact = str(deal.get("primary_contact_id") or "").strip()
        if not contact:
            why = UNPLACEABLE_NO_PRIMARY_CONTACT
        elif contact not in created_at_by_contact:
            why = UNPLACEABLE_CONTACT_NOT_IN_FUNNEL
        elif created_at_by_contact[contact] is None:
            why = UNPLACEABLE_CONTACT_NO_CREATED_AT
        else:
            why = None
        if why is not None:
            unplaceable[why] = unplaceable.get(why, 0) + 1
            continue
        if not in_window(created_at_by_contact[contact], start_at, end_before):
            continue

        bucket, key, reason = deal_bucket(deal, resolve_label)
        _bump_deal(buckets[bucket], deal)
        if (deal.get("association_count") or 0) > 1:
            placed_by_display_contact += 1
        if bucket == BUCKET_CAMPAIGN:
            _bump_deal(by_campaign.setdefault(key, _new_deal_slot()), deal)
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + 1

    google_ads = _new_deal_slot()
    for b in (BUCKET_CAMPAIGN, BUCKET_UNATTRIBUTED):
        google_ads["deals"] += buckets[b]["deals"]
        google_ads["revenue_usd_known"] = round(
            google_ads["revenue_usd_known"] + buckets[b]["revenue_usd_known"], 2)
        google_ads["revenue_usd_missing"] += buckets[b]["revenue_usd_missing"]

    return {
        "buckets": buckets,
        "google_ads": google_ads,
        "by_campaign": by_campaign,
        "reasons": reasons,
        "unplaceable": unplaceable,
        "unplaceable_total": sum(unplaceable.values()),
        "placed_by_display_contact": placed_by_display_contact,
        "dedup": {"key": DEAL_DEDUP_KEY, "duplicate_rows": duplicate_rows,
                  "distinct_deals_examined": len(seen)},
    }


# ═════════════════════════════════════════════════════════════════════════════
# Publication + metadata (pure)
# ═════════════════════════════════════════════════════════════════════════════
def sql_publication(*, cohort_available: bool, reconciliation_problems,
                    freshness: dict | None) -> tuple[str, str | None]:
    """``(status, reason)`` — may the cohort SQL count be published?

    The ONE verdict. The page's SQL count and every cohort CPQL derive from it,
    so a CPQL can never be published over a count that is not.

    * ``unavailable`` — the canonical funnel could not be read.
    * ``withheld`` — buckets do not reconcile; or freshness could not be read
      (``fresh is None``: we cannot say "as of when"); or the freshness verdict
      does not prove the population complete as of a known watermark — the
      freshness reason is passed through as the reason.
    * ``published`` — fresh, or stale WITH its watermark (the label says
      "measured as of"). Stale still withholds CPQL; see ``cpql_decision``.
    """
    if not cohort_available:
        return STATUS_UNAVAILABLE, SQL_REASON_FUNNEL_UNREADABLE
    if reconciliation_problems:
        return STATUS_WITHHELD, SQL_REASON_RECONCILIATION_FAILED
    freshness = freshness or {}
    if freshness.get("fresh") is None:
        return STATUS_WITHHELD, SQL_REASON_WATERMARK_UNKNOWN
    reason = freshness.get("reason")
    if reason not in PUBLISHABLE_FRESHNESS_REASONS:
        return STATUS_WITHHELD, reason or SQL_REASON_WATERMARK_UNKNOWN
    if not freshness.get("last_successful_incremental_at"):
        # Fresh/stale without the instant they are measured from cannot occur
        # from the real assessor; fail closed if it ever does.
        return STATUS_WITHHELD, SQL_REASON_WATERMARK_UNKNOWN
    return STATUS_PUBLISHED, None


def cpql_decision(*, publication: tuple[str, str | None], spend_available: bool,
                  spend_usd, cohort_sqls, source_fresh) -> tuple[str, str | None, float | None]:
    """``(status, reason, value)`` for a cohort CPQL.

    CPQL = window Google Ads spend ÷ cohort SQLs from contacts acquired in that
    SAME window. A missing SQL-entry date never blocks it — the cohort does not
    use one. What does block it:

    * the SQL count itself not being published (``publication``, from
      ``sql_publication``) — the CPQL inherits that status AND its reason, so
      an incomplete bootstrap is never explained as "stale";
    * no spend, or no USD spend because FX is incomplete;
    * zero window spend — "no cost recorded" is not "free SQLs", so it is never
      a $0 CPQL;
    * a stale source. Spend is current; a stale funnel's outcomes are not, so
      the ratio would divide today's money by yesterday's results.

    Zero SQLs is ``not_applicable`` with a ``None`` value — never 0, never ∞.
    """
    pub_status, pub_reason = publication
    if pub_status != STATUS_PUBLISHED:
        return pub_status, pub_reason, None
    if not spend_available:
        return STATUS_UNAVAILABLE, CPQL_REASON_SPEND_UNAVAILABLE, None
    if spend_usd is None:
        return STATUS_UNAVAILABLE, CPQL_REASON_FX_INCOMPLETE, None
    if source_fresh is not True:
        reason = (CPQL_REASON_SOURCE_FRESHNESS_UNKNOWN if source_fresh is None
                  else CPQL_REASON_SOURCE_NOT_FRESH)
        return STATUS_WITHHELD, reason, None
    if not float(spend_usd) > 0:
        return STATUS_NOT_APPLICABLE, CPQL_REASON_ZERO_SPEND, None
    if not cohort_sqls:
        return STATUS_NOT_APPLICABLE, CPQL_REASON_ZERO_SQLS, None
    return STATUS_PUBLISHED, None, round(float(spend_usd) / int(cohort_sqls), 2)


def coverage_status(cohort: dict | None, *, publication: tuple[str, str | None]) -> str:
    """Never ``cohort_complete`` over a population that is not proven complete."""
    if cohort is None:
        return COVERAGE_UNAVAILABLE
    if publication[0] != STATUS_PUBLISHED:
        return COVERAGE_NOT_PROVEN
    if cohort["all_sources"]["sqls_missing_event_timestamp"] > 0:
        return COVERAGE_EVENT_GAPS
    return COVERAGE_COMPLETE


def coverage_notes(cohort: dict | None, *, missing_created_at,
                   publication: tuple[str, str | None]) -> list[str]:
    if cohort is None:
        return ["the canonical contact funnel could not be read; no cohort "
                "outcome is published"]
    notes = []
    if publication[0] != STATUS_PUBLISHED:
        notes.append(
            f"the cohort SQL count is withheld ({publication[1]}): the canonical "
            f"contact population is not proven complete as of a known data "
            f"watermark, so any count read from it is partial, not a total.")
    gap = cohort["all_sources"]["sqls_missing_event_timestamp"]
    if gap:
        notes.append(
            f"{gap} cohort SQL contact(s) are proven by lifecycle stage and have "
            f"no exact SQL-entry timestamp. They are counted here, in the cohort "
            f"of the period they were created in. They remain lifecycle-event "
            f"coverage gaps: no timestamp was produced for them, and lifecycle-"
            f"event SQL totals stay governed by the coverage gate.")
    if missing_created_at:
        notes.append(
            f"{missing_created_at} canonical contact(s) have no created date and "
            f"belong to no acquisition window, All Time included. No other date "
            f"is substituted.")
    if cohort["dedup"]["fallback_identities"]:
        notes.append(
            f"{cohort['dedup']['fallback_identities']} contact(s) had a blank "
            f"contact_id and were deduplicated by funnel row id instead.")
    if cohort["dedup"]["duplicate_rows"]:
        notes.append(
            f"{cohort['dedup']['duplicate_rows']} duplicate row(s) were collapsed "
            f"to one contact each; the canonical table is expected to be unique "
            f"on contact_id.")
    if cohort["rows_outside_window"]:
        notes.append(
            f"{cohort['rows_outside_window']} row(s) returned outside the window "
            f"were excluded on re-test.")
    return notes


def sql_metric_metadata(*, cohort: dict | None, freshness: dict, attribution_status: str,
                        missing_created_at, publication: tuple[str, str | None]) -> dict:
    """The machine-readable contract every cohort-SQL response carries."""
    return {
        "metric_family": METRIC_FAMILY_COHORT,
        "window_basis": WINDOW_BASIS_COHORT,
        "outcome_basis": OUTCOME_BASIS_SQL,
        "dedup_key": DEDUP_KEY,
        "as_of": freshness.get("last_successful_incremental_at"),
        "source_freshness": {
            "fresh": freshness.get("fresh"),
            "reason": freshness.get("reason"),
            "age_hours": freshness.get("age_hours"),
            "detail": freshness.get("detail"),
        },
        "attribution_status": attribution_status,
        "mapped_count": (None if cohort is None
                         else sum(s["sqls"] for s in cohort["by_campaign"].values())),
        "unattributed_count": None if cohort is None else cohort["unattributed"]["sqls"],
        "excluded_non_google_count": (None if cohort is None
                                      else cohort["excluded_non_google"]["sqls"]),
        "coverage_status": coverage_status(cohort, publication=publication),
        "coverage_notes": coverage_notes(cohort, missing_created_at=missing_created_at,
                                         publication=publication),
        "basis_label": COHORT_BASIS_LABEL,
    }


#: Deals and SQL contacts are attributed on DIFFERENT evidence, by design — each
#: by the canonical rule for its own entity. Stated on every response so a
#: campaign with closed-won deals and no cohort SQLs is not read as a defect.
DEAL_ATTRIBUTION_BASIS = (
    "closed-won deals are attributed by the revenue scope lattice "
    "(analysis.revenue_scope: agreed Google Ads source OR a GCLID on the deal's "
    "evidence); cohort SQLs by the contact's own original source "
    "(hs_analytics_source). A deal "
    "with a GCLID whose contact's original source is not Paid Search counts as a "
    "Google Ads deal while that contact is excluded from Google Ads SQLs, so a "
    "campaign can show closed-won deals without a matching cohort SQL.")


def deal_metric_metadata(*, deals: dict | None, freshness: dict, attribution_status: str,
                         publication: tuple[str, str | None]) -> dict:
    """The same contract for closed-won deals, with their own bases named."""
    notes = []
    if deals is None:
        notes.append("the canonical deal ledger could not be read")
    else:
        if publication[0] != STATUS_PUBLISHED:
            notes.append(
                f"deals are placed in a window through their contact's created "
                f"date, and the contact population is not proven complete "
                f"({publication[1]}); these counts are partial.")
        if deals.get("placed_by_display_contact"):
            notes.append(
                f"{deals['placed_by_display_contact']} counted deal(s) have more "
                f"than one associated contact and are placed by the ledger's "
                f"primary contact — the lowest contact id, a display identity — "
                f"not by the first contact acquired.")
        if deals["unplaceable_total"]:
            notes.append(
                f"{deals['unplaceable_total']} closed-won deal(s) could not be "
                f"placed in any acquisition window "
                f"({', '.join(f'{k}: {v}' for k, v in sorted(deals['unplaceable'].items()))}); "
                f"they are in no window and are not counted as zero.")
        amb = deals["buckets"][BUCKET_AMBIGUOUS]["deals"]
        if amb:
            notes.append(f"{amb} closed-won deal(s) carry contradictory source "
                         f"evidence and are reported as ambiguous, not attributed.")
        missing = sum(b["revenue_usd_missing"] for b in deals["buckets"].values())
        if missing:
            notes.append(f"{missing} deal(s) have no USD revenue (currency "
                         f"incomplete); revenue sums are the known subset.")
    return {
        "metric_family": METRIC_FAMILY_COHORT,
        "window_basis": WINDOW_BASIS_DEALS,
        "outcome_basis": OUTCOME_BASIS_DEALS,
        "dedup_key": DEAL_DEDUP_KEY,
        "as_of": freshness.get("last_successful_incremental_at"),
        "source_freshness": {"fresh": freshness.get("fresh"),
                             "reason": freshness.get("reason")},
        "attribution_status": attribution_status,
        "mapped_count": None if deals is None else deals["buckets"][BUCKET_CAMPAIGN]["deals"],
        "unattributed_count": (None if deals is None
                               else deals["buckets"][BUCKET_UNATTRIBUTED]["deals"]),
        "ambiguous_count": None if deals is None else deals["buckets"][BUCKET_AMBIGUOUS]["deals"],
        "excluded_non_google_count": (None if deals is None
                                      else deals["buckets"][BUCKET_EXCLUDED]["deals"]),
        "coverage_status": (
            STATUS_UNAVAILABLE if deals is None
            else COVERAGE_NOT_PROVEN if publication[0] != STATUS_PUBLISHED
            else "complete" if not deals["unplaceable_total"]
            else "partial_unplaceable_disclosed"),
        "attribution_basis": DEAL_ATTRIBUTION_BASIS,
        "coverage_notes": notes,
        "label": "Closed-won deals (deduplicated by deal_id) — not unique customers",
    }


def _jsonable_cohort(cohort: dict) -> dict:
    """The cohort without its per-identity working set (contact ids stay server-side)."""
    return {k: v for k, v in cohort.items() if k != "sql_identities"}


# ═════════════════════════════════════════════════════════════════════════════
# Reads (the only I/O in this module)
# ═════════════════════════════════════════════════════════════════════════════
def read_freshness(now: datetime | None = None) -> dict:
    """The contact-funnel freshness verdict — the canonical assessor, unchanged."""
    from analysis import sql_coverage_freshness as freshness_mod  # noqa: PLC0415
    from db import crm_funnel_repository as repo  # noqa: PLC0415
    try:
        return freshness_mod.assess(repo.fetch_contact_funnel_sync_state(), now=now)
    except Exception as exc:  # noqa: BLE001
        logger.error("[cohort] freshness unreadable: %s", exc)
        return {"fresh": None, "reason": "source_freshness_unreadable",
                "detail": "the contact-funnel sync state could not be read",
                "last_successful_incremental_at": None, "age_hours": None}


def build_window_outcomes(start: date | None, end: date, *, resolve_label: LabelResolver,
                          freshness: dict | None = None,
                          now: datetime | None = None) -> dict:
    """Read and classify one window's acquisition cohort and its closed-won deals.

    Returns ``{"available", "cohort", "deals", "missing_created_at",
    "reconciliation_problems", "freshness", "sql_publication", "start_at",
    "end_before"}``.
    ``cohort`` / ``deals`` are None — never empty — when their source could
    not be read.
    """
    from db import crm_funnel_repository as funnel_repo  # noqa: PLC0415
    from db import deal_ledger_repository as ledger_repo  # noqa: PLC0415

    start_at, end_before = window_instants(start, end)
    freshness = freshness if freshness is not None else read_freshness(now)

    contacts = funnel_repo.fetch_acquisition_cohort_contacts(start_at, end_before)
    cohort = None
    problems: list[str] = []
    if contacts.get("available"):
        cohort = build_cohort(contacts.get("rows") or [], resolve_label=resolve_label,
                              start_at=start_at, end_before=end_before)
        problems = reconcile_cohort(cohort)

    deals = None
    won = ledger_repo.fetch_won_deals(None, None)
    if won.get("available"):
        rows = won.get("rows") or []
        created = funnel_repo.fetch_contacts_created_at(
            r.get("primary_contact_id") for r in rows)
        if created.get("available"):
            deals = build_deal_outcomes(
                rows, created_at_by_contact=created.get("created_at") or {},
                resolve_label=resolve_label, start_at=start_at, end_before=end_before)

    return {
        "available": cohort is not None,
        "cohort": cohort,
        "deals": deals,
        "missing_created_at": contacts.get("missing_created_at"),
        "reconciliation_problems": problems,
        "freshness": freshness,
        # The one publication verdict; the SQL count, every CPQL and both
        # metadata blocks read it from here.
        "sql_publication": sql_publication(
            cohort_available=cohort is not None,
            reconciliation_problems=problems, freshness=freshness),
        "start_at": start_at.isoformat() if start_at else None,
        "end_before": end_before.isoformat(),
    }


def lifecycle_event_disclosure() -> dict:
    """What this page does NOT publish, and why — the event-time SQL coverage.

    Read only from the sanctioned global inputs of the publication contract
    and the coverage-population split. It decides nothing: lifecycle-event SQL
    totals stay governed by ``analysis.sql_publication`` and the coverage gate,
    and this disclosure exists so a cohort number can never be mistaken for
    one of them.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415
    from services import canonical_sql_publication_service as pubsvc  # noqa: PLC0415

    try:
        inputs = pubsvc.publication_inputs()
    except Exception as exc:  # noqa: BLE001
        logger.error("[cohort] publication inputs unreadable: %s", exc)
        inputs = {}
    population = repo.fetch_sql_coverage_population()
    incidents = inputs.get("open_incidents")
    readable = population.get("available") is True
    return {
        "metric_family": METRIC_FAMILY_LIFECYCLE_EVENTS,
        "window_basis": WINDOW_BASIS_LIFECYCLE_EVENTS,
        "published_on_this_page": False,
        "governed_by": "analysis.sql_publication.publication_verdict "
                       "(enforced by scripts/audit_sql_coverage_gate.py)",
        "reached_sql_by_current_stage": population.get("candidates") if readable else None,
        "exact_direct_timestamp": population.get("direct") if readable else None,
        "recovered_timestamp": population.get("recovered") if readable else None,
        "missing_exact_timestamp": population.get("unresolved") if readable else None,
        "open_post_boundary_incidents": (len(incidents) if isinstance(incidents, list)
                                         else None),
        "boundary_readable": inputs.get("boundary_readable"),
        "boundary_id": inputs.get("boundary_id"),
        "boundary_observed_at": (str(inputs["boundary_observed_at"])
                                 if inputs.get("boundary_observed_at") else None),
        "explanation": (
            "Lifecycle-event SQLs (contacts that ENTERED SQL in a window) need "
            "an exact SQL-entry timestamp for every contact. Contacts without "
            "one, and open post-boundary incidents, keep those totals withheld. "
            "They are not shown on this page and are not replaced by the "
            "acquisition-cohort count."),
    }
