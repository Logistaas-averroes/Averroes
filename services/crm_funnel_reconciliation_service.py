"""
services/crm_funnel_reconciliation_service.py

PR-ADS-153B §22–§23 — reconcile the LEGACY funnel doctrine against canonical
HubSpot lifecycle truth, contact by contact, and explain every SQL count change
before it reaches a page.

Why this exists
---------------
PR-ADS-153B materially changes what an "SQL" is:

    legacy      status_category = 'qualified'
                ⟸ mql_status ∈ {CLOSED - Sales Qualified, CLOSED - Deal Created}
                dated by the contact's CREATION date

    canonical   the contact entered lifecycle stage 'salesqualifiedlead'
                dated by hs_v2_date_entered_salesqualifiedlead

Those two populations differ for two independent reasons, and the difference must
never surprise anyone:

  * POPULATION — a contact can be qualified under one doctrine and not the other.
  * DATE — the same contact can be an SQL under both doctrines yet land in a
    different reporting window because the event date moved from acquisition to
    qualification.

This service separates those two causes rather than reporting one confusing
delta, so "the Dashboard SQL number changed" always has a contact-level answer.

Read-only. No emails are ever returned — HubSpot contact id and company only.
"""

from __future__ import annotations

import logging

from analysis.crm_lifecycle import (
    STAGE_CUSTOMER,
    STAGE_OPPORTUNITY,
    STAGE_SQL,
    normalize_lifecycle_stage,
    stage_rank,
)
from analysis.mql_status_taxonomy import (
    CATEGORY_DEAL_CREATED_SIGNAL,
    CATEGORY_NO_VERDICT,
    CATEGORY_SALES_QUALIFIED_SIGNAL,
    CATEGORY_UNMAPPED,
    classify_mql_status,
    looks_like_free_text,
)
from services import canonical_crm_funnel_service as funnel
from services.canonical_contact_outcome_service import (
    WINDOW_BUSINESS,
    resolve_window_contract,
)

logger = logging.getLogger(__name__)

LEGACY_QUALIFIED = "qualified"
LEGACY_IN_PROGRESS = "in_progress"

# ── Mismatch classes (PR-ADS-153B §22) ───────────────────────────────────────
MISMATCH_LIFECYCLE_SQL_LEGACY_NOT_QUALIFIED = "lifecycle_sql_legacy_not_qualified"
MISMATCH_LEGACY_QUALIFIED_NEVER_ENTERED_SQL = "legacy_qualified_never_entered_sql"
MISMATCH_LIFECYCLE_OPPORTUNITY_LEGACY_IN_PROGRESS = (
    "lifecycle_opportunity_legacy_in_progress")
MISMATCH_LIFECYCLE_CUSTOMER_NO_CUSTOMER_DATE = "lifecycle_customer_no_customer_date"
MISMATCH_DEAL_CREATED_STATUS_NOT_OPPORTUNITY = "deal_created_status_lifecycle_not_opportunity"
MISMATCH_SALES_QUALIFIED_STATUS_NOT_SQL = "sales_qualified_status_lifecycle_not_sql"
MISMATCH_UNMAPPED_MQL_STATUS = "unmapped_mql_status"
MISMATCH_NO_VERDICT = "no_verdict_mql_status"
MISMATCH_LEGACY_WITHOUT_HUBSPOT_IDENTITY = "legacy_without_hubspot_identity"
MISMATCH_FREE_TEXT_MQL_STATUS = "free_text_mql_status_pollution"

MISMATCH_CLASSES = (
    MISMATCH_LIFECYCLE_SQL_LEGACY_NOT_QUALIFIED,
    MISMATCH_LEGACY_QUALIFIED_NEVER_ENTERED_SQL,
    MISMATCH_LIFECYCLE_OPPORTUNITY_LEGACY_IN_PROGRESS,
    MISMATCH_LIFECYCLE_CUSTOMER_NO_CUSTOMER_DATE,
    MISMATCH_DEAL_CREATED_STATUS_NOT_OPPORTUNITY,
    MISMATCH_SALES_QUALIFIED_STATUS_NOT_SQL,
    MISMATCH_UNMAPPED_MQL_STATUS,
    MISMATCH_NO_VERDICT,
    MISMATCH_LEGACY_WITHOUT_HUBSPOT_IDENTITY,
    MISMATCH_FREE_TEXT_MQL_STATUS,
)

MISMATCH_LABELS = {
    MISMATCH_LIFECYCLE_SQL_LEGACY_NOT_QUALIFIED:
        "Entered Sales Qualified Lead in HubSpot, but legacy status is not qualified",
    MISMATCH_LEGACY_QUALIFIED_NEVER_ENTERED_SQL:
        "Legacy qualified, but HubSpot has no Sales Qualified Lead entry",
    MISMATCH_LIFECYCLE_OPPORTUNITY_LEGACY_IN_PROGRESS:
        "Lifecycle Opportunity, but legacy status still in progress",
    MISMATCH_LIFECYCLE_CUSTOMER_NO_CUSTOMER_DATE:
        "Lifecycle stage is Customer, but no customer stage-entry date exists",
    MISMATCH_DEAL_CREATED_STATUS_NOT_OPPORTUNITY:
        "MQL status 'CLOSED - Deal Created', but lifecycle never reached Opportunity",
    MISMATCH_SALES_QUALIFIED_STATUS_NOT_SQL:
        "MQL status 'CLOSED - Sales Qualified', but lifecycle never reached SQL",
    MISMATCH_UNMAPPED_MQL_STATUS:
        "MQL status value Averroes does not recognise (new production value)",
    MISMATCH_NO_VERDICT:
        "No MQL status recorded yet",
    MISMATCH_LEGACY_WITHOUT_HUBSPOT_IDENTITY:
        "Legacy lead row carries no HubSpot contact id and cannot be reconciled",
    MISMATCH_FREE_TEXT_MQL_STATUS:
        "Legacy mql_status holds MDR free text (pre-PR-ADS-153B pollution)",
}


def _funnel_index(funnel_rows: list[dict]) -> dict:
    return {
        str(r.get("contact_id")): r
        for r in (funnel_rows or [])
        if str(r.get("contact_id") or "").strip()
    }


def reconcile_contacts(
    funnel_rows: list[dict],
    legacy_rows: list[dict],
    exclusions: set | None = None,
) -> dict:
    """Contact-by-contact legacy-vs-lifecycle reconciliation. Pure.

    Returns ``{counts, contacts}`` where ``counts`` is mismatch class → count and
    ``contacts`` is the admin-safe drill-down (contact id + company + the classes
    it triggered). One contact may appear in several classes.
    """
    exclusions = exclusions or set()
    by_id = _funnel_index(funnel_rows)

    counts = {cls: 0 for cls in MISMATCH_CLASSES}
    drilldown: list[dict] = []

    # ── Legacy-anchored classes ──────────────────────────────────────────────
    for legacy in legacy_rows or []:
        contact_key = legacy.get("contact_key")
        if contact_key in exclusions:
            continue

        contact_id = str(legacy.get("contact_id") or "").strip()
        legacy_status = (legacy.get("status_category") or "").strip().lower()
        raw_mql = legacy.get("mql_status")
        classes: list[str] = []

        if not contact_id:
            counts[MISMATCH_LEGACY_WITHOUT_HUBSPOT_IDENTITY] += 1
            classes.append(MISMATCH_LEGACY_WITHOUT_HUBSPOT_IDENTITY)
            if classes:
                drilldown.append(_drill(legacy.get("company"), None, classes,
                                        legacy_status, raw_mql, None))
            continue

        if looks_like_free_text(raw_mql):
            counts[MISMATCH_FREE_TEXT_MQL_STATUS] += 1
            classes.append(MISMATCH_FREE_TEXT_MQL_STATUS)

        category = classify_mql_status(raw_mql)
        if category == CATEGORY_UNMAPPED:
            counts[MISMATCH_UNMAPPED_MQL_STATUS] += 1
            classes.append(MISMATCH_UNMAPPED_MQL_STATUS)
        elif category == CATEGORY_NO_VERDICT:
            counts[MISMATCH_NO_VERDICT] += 1
            classes.append(MISMATCH_NO_VERDICT)

        canonical = by_id.get(contact_id)
        stage = normalize_lifecycle_stage(
            (canonical or {}).get("lifecycle_stage"))
        entered_sql = (canonical or {}).get("date_entered_sql")
        entered_opportunity = (canonical or {}).get("date_entered_opportunity")

        if legacy_status == LEGACY_QUALIFIED and canonical is not None and not entered_sql:
            counts[MISMATCH_LEGACY_QUALIFIED_NEVER_ENTERED_SQL] += 1
            classes.append(MISMATCH_LEGACY_QUALIFIED_NEVER_ENTERED_SQL)

        if legacy_status == LEGACY_IN_PROGRESS and _reached(stage, STAGE_OPPORTUNITY):
            counts[MISMATCH_LIFECYCLE_OPPORTUNITY_LEGACY_IN_PROGRESS] += 1
            classes.append(MISMATCH_LIFECYCLE_OPPORTUNITY_LEGACY_IN_PROGRESS)

        if category == CATEGORY_SALES_QUALIFIED_SIGNAL and canonical is not None:
            if not entered_sql and not _reached(stage, STAGE_SQL):
                counts[MISMATCH_SALES_QUALIFIED_STATUS_NOT_SQL] += 1
                classes.append(MISMATCH_SALES_QUALIFIED_STATUS_NOT_SQL)

        if category == CATEGORY_DEAL_CREATED_SIGNAL and canonical is not None:
            if not entered_opportunity and not _reached(stage, STAGE_OPPORTUNITY):
                counts[MISMATCH_DEAL_CREATED_STATUS_NOT_OPPORTUNITY] += 1
                classes.append(MISMATCH_DEAL_CREATED_STATUS_NOT_OPPORTUNITY)

        if classes:
            drilldown.append(_drill(
                legacy.get("company") or (canonical or {}).get("company"),
                contact_id, classes, legacy_status, raw_mql, stage))

    # ── Lifecycle-anchored classes ───────────────────────────────────────────
    legacy_by_id = {
        str(r.get("contact_id")): r for r in (legacy_rows or [])
        if str(r.get("contact_id") or "").strip()
    }

    for contact_id, row in by_id.items():
        stage = normalize_lifecycle_stage(row.get("lifecycle_stage"))
        classes = []

        legacy = legacy_by_id.get(contact_id)
        legacy_status = ((legacy or {}).get("status_category") or "").strip().lower()

        if row.get("date_entered_sql") and legacy_status != LEGACY_QUALIFIED:
            counts[MISMATCH_LIFECYCLE_SQL_LEGACY_NOT_QUALIFIED] += 1
            classes.append(MISMATCH_LIFECYCLE_SQL_LEGACY_NOT_QUALIFIED)

        if stage == STAGE_CUSTOMER and not row.get("date_entered_customer"):
            counts[MISMATCH_LIFECYCLE_CUSTOMER_NO_CUSTOMER_DATE] += 1
            classes.append(MISMATCH_LIFECYCLE_CUSTOMER_NO_CUSTOMER_DATE)

        if classes:
            drilldown.append(_drill(
                row.get("company"), contact_id, classes, legacy_status,
                row.get("mql_status"), stage))

    return {
        "counts": counts,
        "labels": MISMATCH_LABELS,
        "contacts": drilldown,
        "total_flagged_contacts": len(drilldown),
    }


def _reached(stage, target) -> bool:
    """Has the contact's current lifecycle stage reached ``target``?"""
    current, wanted = stage_rank(stage), stage_rank(target)
    if current is None or wanted is None:
        return False
    return current >= wanted


def _drill(company, contact_id, classes, legacy_status, mql_status, stage) -> dict:
    """Admin-safe drill-down row. Contact id + company only — never an email."""
    return {
        "contact_id": contact_id,
        "company": company,
        "legacy_status_category": legacy_status or None,
        "mql_status": mql_status,
        "lifecycle_stage": stage,
        "mismatch_classes": classes,
    }


# ── Before/after SQL reconciliation (PR-ADS-153B §23) ────────────────────────
def compare_sql_counts(
    funnel_rows: list[dict],
    legacy_rows: list[dict],
    exclusions: set | None,
    start,
    end,
    *,
    campaign_resolver=None,
) -> dict:
    """Explain the SQL count change for ONE window. Pure.

    Splits the delta into its two independent causes:

      * DATE SHIFT — the contact is an SQL under both doctrines, but the event
        date moved from contact creation to Sales-Qualified entry, so it lands in
        a different window.
      * POPULATION — the contact qualifies under one doctrine only.

    Returns both counts and the contact-id sets that produced them.
    """
    exclusions = exclusions or set()

    legacy_qualified_any = set()
    legacy_in_window = set()
    for row in legacy_rows or []:
        if row.get("contact_key") in exclusions:
            continue
        contact_id = str(row.get("contact_id") or "").strip()
        if not contact_id:
            continue
        if (row.get("status_category") or "").strip().lower() != LEGACY_QUALIFIED:
            continue
        legacy_qualified_any.add(contact_id)
        if _in_window(row.get("contact_created_at"), start, end):
            legacy_in_window.add(contact_id)

    lifecycle_sql_any = set()
    lifecycle_in_window = set()
    for row in funnel_rows or []:
        contact_id = str(row.get("contact_id") or "").strip()
        if not contact_id:
            continue
        if not row.get("date_entered_sql"):
            continue
        lifecycle_sql_any.add(contact_id)
        if _in_window(row.get("date_entered_sql"), start, end):
            lifecycle_in_window.add(contact_id)

    overlap = legacy_in_window & lifecycle_in_window
    legacy_only = legacy_in_window - lifecycle_in_window
    lifecycle_only = lifecycle_in_window - legacy_in_window

    # SQL under BOTH doctrines overall, but not in the same window → pure date shift.
    both_doctrines = legacy_qualified_any & lifecycle_sql_any
    date_shifted = (legacy_only | lifecycle_only) & both_doctrines

    # Legacy-qualified contacts HubSpot never marked Sales Qualified → population.
    missing_sql_event_date = {
        cid for cid in legacy_only if cid not in lifecycle_sql_any
    }

    scope_counts = _scope_coverage(
        funnel_rows, lifecycle_in_window, campaign_resolver=campaign_resolver)

    return {
        "legacy_sql_count": len(legacy_in_window),
        "lifecycle_sql_count": len(lifecycle_in_window),
        "delta": len(lifecycle_in_window) - len(legacy_in_window),
        "overlap_contacts": len(overlap),
        "legacy_only_contacts": len(legacy_only),
        "lifecycle_only_contacts": len(lifecycle_only),
        "date_shifted_contacts": len(date_shifted),
        "missing_sql_event_date_contacts": len(missing_sql_event_date),
        "attribution_coverage": scope_counts,
        "legacy_definition": (
            "status_category = 'qualified' (mql_status CLOSED - Sales Qualified / "
            "CLOSED - Deal Created), windowed by contact_created_at"
        ),
        "canonical_definition": (
            "entered lifecycle stage 'salesqualifiedlead', windowed by "
            "hs_v2_date_entered_salesqualifiedlead"
        ),
        "sets": {
            "legacy_only": sorted(legacy_only),
            "lifecycle_only": sorted(lifecycle_only),
            "date_shifted": sorted(date_shifted),
            "missing_sql_event_date": sorted(missing_sql_event_date),
        },
    }


def _scope_coverage(funnel_rows, contact_ids: set, *, campaign_resolver=None) -> dict:
    """Named-scope breakdown of a lifecycle SQL set (attribution creates subsets)."""
    resolver = campaign_resolver or funnel.default_campaign_resolver
    rows = [r for r in (funnel_rows or [])
            if str(r.get("contact_id") or "") in contact_ids]
    populations = funnel.build_populations(
        rows, None, None, campaign_resolver=resolver)
    contacts = populations["contacts"]
    return {
        scope: sum(1 for c in contacts if c["scopes"][scope])
        for scope in funnel.ORDERED_SCOPES
    }


def _in_window(value, start, end) -> bool:
    if value is None:
        return False
    from datetime import date as _date, datetime as _datetime

    if isinstance(value, _datetime):
        value = value.date()
    if not isinstance(value, _date):
        try:
            value = _date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return False
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True


# ── Public entry point ───────────────────────────────────────────────────────
def run(business_window: str = "current_quarter", *, now=None) -> dict:
    """Full reconciliation payload for the admin audit endpoint. Read-only."""
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    window = resolve_window_contract(WINDOW_BUSINESS, business_window, now=now)
    start, end = window["start"], window["end"]

    funnel_fetch = repo.fetch_all_funnel_contacts()
    legacy_fetch = repo.fetch_legacy_outcome_rows()
    pollution = repo.fetch_polluted_mql_status_rows()

    available = bool(funnel_fetch.get("available") and legacy_fetch.get("available"))
    funnel_rows = funnel_fetch.get("rows") or []
    legacy_rows = legacy_fetch.get("rows") or []
    exclusions = legacy_fetch.get("exclusions") or set()

    if not available:
        return {
            "available": False,
            "reason": "canonical_or_legacy_source_unavailable",
            "window": {
                "window_type": window["window_type"],
                "window_key": window["window_key"],
                "start_date": window["start_date"],
                "end_date": window["end_date"],
            },
        }

    resolver = funnel._build_campaign_resolver(start, end)  # noqa: SLF001

    return {
        "available": True,
        "window": {
            "window_type": window["window_type"],
            "window_key": window["window_key"],
            "start_date": window["start_date"],
            "end_date": window["end_date"],
        },
        "sql_comparison": compare_sql_counts(
            funnel_rows, legacy_rows, exclusions, start, end,
            campaign_resolver=resolver),
        "mismatches": reconcile_contacts(funnel_rows, legacy_rows, exclusions),
        "mql_status_pollution": {
            "available": bool(pollution.get("available")),
            "legacy_rows_with_unknown_status": pollution.get("total"),
            "distinct_values_sample": pollution.get("rows"),
            "note": (
                "Detection only. PR-ADS-153B removed the mql_status ← "
                "mql___mdr_comments fallback so new pollution cannot occur; "
                "historical evidence is never rewritten or deleted."
            ),
        },
        "doctrine": {
            "legacy_status_category": (
                "COMPATIBILITY ONLY — legacy operational classification, no "
                "longer canonical funnel truth. Retired for page counting in "
                "PR-ADS-153C."
            ),
            "canonical_funnel": "HubSpot lifecycle stage-entry evidence",
            "lifecycle_customer_vs_revenue_customer": (
                "Lifecycle Customer is a CRM stage event. Revenue Customer is "
                "closed-won deal truth and is defined separately in PR-ADS-153E."
            ),
        },
    }
