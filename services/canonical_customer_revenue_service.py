"""
services/canonical_customer_revenue_service.py

PR-ADS-161D — canonical closed-won deals, customers and closed-won revenue,
per evidence and business window, with attribution, coverage, freshness and an
explicit publication verdict for every metric.

Not a second source of revenue truth
------------------------------------
This module never reads the deal ledger itself. It reads the closed-won
universe through ``services.canonical_revenue_service`` — THE revenue read
contract (docs/35 §14) — so the won predicate (``hs_is_closed_won IS TRUE``),
deal identity (``deal_id``), the revenue value (``revenue_usd`` where the
currency is proven) and the coverage gate (``check_sync_coverage``) are the
ones every page already uses. What it adds is the layer those pages lack:

* a distinction between a deal and a customer (a HubSpot company) — and an
  honest refusal to count customers the repository cannot identify;
* a deal-level attribution PARTITION that always sums to the all-source total,
  with campaign placement through Campaign Evidence's own resolver (approved
  mapping, else an exact match to exactly one spend campaign; never fuzzy);
* the acquisition-cohort question, kept apart from the close-date question;
* a won-definition cross-check against the confirmed won stage;
* per-metric publication: ``published`` / ``withheld`` / ``unavailable`` /
  ``not_published``, with a value of ``None`` — never ``0`` — whenever the
  status is not ``published``.

Read-only: no HubSpot call, no Google Ads call, no write. It is consumed by no
production page in PR-ADS-161D; PR-ADS-161E migrates readers onto it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

from analysis import closed_won_truth as cwt

log = logging.getLogger(__name__)

WINDOW_EVIDENCE = "evidence"
WINDOW_BUSINESS = "business"

SOURCE = "hubspot_deal_ledger"
REPORTING_CURRENCY = "USD"

DEFINITIONS = {
    "closed_won": ("hs_is_closed_won IS TRUE (HubSpot's own won flag; "
                   "docs/35 §3), cross-checked against HubSpot deal stage "
                   f"{cwt.CONFIRMED_WON_STAGE_ID} "
                   f"({cwt.CONFIRMED_WON_STAGE_LABEL})"),
    "won_stage_cross_check": cwt.CONFIRMED_WON_STAGE_ID,
    "deal_dedup_key": "hubspot_deal_id",
    "customer_dedup_key": "hubspot_company_id",
    "revenue_source": "canonical deal ledger (hubspot_deal_ledger.revenue_usd "
                      "where currency_status proves USD)",
    "reporting_currency": REPORTING_CURRENCY,
    "event_membership_date_field": "deal_close_date",
    "acquisition_membership_date_field": "contact_created_at",
    "window_interval": "half-open [start, end), UTC instants",
}

#: The repository has no store of deal→company associations (the ledger holds
#: none, and no table does). Customer identity is therefore unavailable for
#: every deal — stated as data so the audit and the docs say the same thing.
COMPANY_ASSOCIATION_SOURCE = None
COMPANY_ASSOCIATION_GAP = (
    "the canonical deal ledger stores no HubSpot company association, and no "
    "other local table does; a customer count cannot be computed from local "
    "data until company associations are ingested")


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def window_bounds(window_type: str, window_key: str,
                  now: datetime | None = None) -> dict:
    """``{start, end, start_date, end_date, is_all_time, label}``, half-open.

    Evidence windows follow the repository's evidence convention: N account-local
    calendar dates ending today (``analysis.account_time``), so ``start`` is
    midnight of ``today - (N-1)`` and ``end`` is midnight after today. Business
    windows use ``analysis.business_windows.get_window_bounds`` unchanged. Both
    are UTC instants; neither ever uses an ingestion, sync or boundary time.
    """
    now = now or _utcnow()
    if window_type == WINDOW_EVIDENCE:
        from analysis.account_time import account_today  # noqa: PLC0415
        from analysis.evidence_windows import (  # noqa: PLC0415
            resolve_evidence_window,
        )
        resolved = resolve_evidence_window(window_key)
        today = account_today(now)
        days = resolved["days"]
        start_date = None if days is None else today - timedelta(days=days - 1)
        return {"start": None if start_date is None else _midnight(start_date),
                "end": _midnight(today + timedelta(days=1)),
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": (today + timedelta(days=1)).isoformat(),
                "is_all_time": days is None, "label": window_key}
    if window_type == WINDOW_BUSINESS:
        from analysis.business_windows import (  # noqa: PLC0415
            get_window_bounds, resolve_window,
        )
        resolved = resolve_window(window_key, now=now)
        start, end = get_window_bounds(window_key, now=now)
        return {"start": start, "end": end,
                "start_date": start.date().isoformat() if start else None,
                "end_date": end.date().isoformat(),
                "is_all_time": start is None,
                "label": resolved.get("label")}
    raise ValueError(f"unknown window type '{window_type}'")


def all_windows() -> list:
    """Every supported (window_type, window_key) pair, in display order."""
    from analysis.business_windows import WINDOW_KEYS  # noqa: PLC0415
    from analysis.evidence_windows import EVIDENCE_WINDOWS  # noqa: PLC0415
    return ([(WINDOW_EVIDENCE, k) for k in EVIDENCE_WINDOWS]
            + [(WINDOW_BUSINESS, k) for k in WINDOW_KEYS])


def campaign_resolver(now: datetime | None = None) -> tuple:
    """``(resolve_label | None, detail)`` — Campaign Evidence's own resolver.

    Bound to the approved durable identity mappings and the ALL-TIME canonical
    spend campaign names, so a deal's campaign placement does not depend on
    which window happens to be asked about. Never fuzzy. ``None`` when either
    input cannot be read: then no deal is placed on a campaign and campaign
    revenue is withheld — never guessed.
    """
    try:
        from analysis.account_time import account_today  # noqa: PLC0415
        from db import revenue_repository as repo  # noqa: PLC0415
        from services.campaign_evidence_service import (  # noqa: PLC0415
            _assign_lead, _identity_index, _spend_by_campaign_id,
        )
        spend = repo.fetch_canonical_campaign_spend(None, account_today(now))
        if not spend.get("available"):
            return None, "canonical campaign spend could not be read"
        identity = repo.fetch_campaign_identity(spend.get("customer_id"))
        if not identity.get("available"):
            return None, "campaign identity mappings could not be read"
        _, norm_to_ids = _spend_by_campaign_id(spend)
        by_label, _ = _identity_index(identity)
        return (lambda label: _assign_lead(label, by_label, norm_to_ids),
                "approved identity mappings, else exact normalized match to "
                "exactly one all-time canonical spend campaign; never fuzzy")
    except Exception as exc:  # noqa: BLE001
        log.warning("[closed_won_truth] campaign resolver unavailable: %s", exc)
        return None, "campaign resolver could not be built"


def _as_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def freshness_block(sync_state, findings, now: datetime) -> dict:
    """Ledger freshness from SUCCESSFUL SYNC COVERAGE — never the newest deal.

    ``latest_successful_incremental_at`` is reported only when the coverage
    gate proves the last run was a successful incremental; otherwise NULL, with
    the gate's findings saying why. No staleness threshold is configured for
    the deal ledger anywhere in the repository, so age is reported and NOT
    judged: inventing a threshold here would be a truth rule hidden in a number.
    """
    state = sync_state or {}
    proven = findings == [] and bool(state)
    successful = (state.get("last_sync_mode") == "incremental"
                  and state.get("last_status") == "success")
    last_ok = state.get("last_incremental_at") if (proven and successful) \
        else None
    last_ok_dt = _as_dt(last_ok)
    return {
        "signal": "canonical deal-ledger sync coverage (hubspot_deal_sync_state)",
        "coverage_proven": proven if findings is not None else None,
        "violation_codes": sorted({f.get("code") for f in findings or []}),
        "violations": [f.get("message") for f in findings or []],
        "bootstrap_status": state.get("bootstrap_status"),
        "bootstrap_complete": state.get("bootstrap_status") == "complete"
        if state else None,
        "bootstrap_completed_at": state.get("bootstrap_completed_at"),
        "incremental_succeeded_after_bootstrap": proven,
        "latest_successful_incremental_at": last_ok,
        "last_sync_mode": state.get("last_sync_mode"),
        "last_status": state.get("last_status"),
        "source_watermark": state.get("last_modified_watermark"),
        "coverage_through": state.get("last_modified_watermark")
        if proven else None,
        "latest_batch_id": state.get("last_batch_id"),
        "deals_seen": state.get("deals_seen"),
        "pages_fetched": state.get("pages_fetched"),
        "association_failures": state.get("association_failures"),
        "rows_fetched_prepared_written_rejected": None,
        "age_hours": (round((now - last_ok_dt).total_seconds() / 3600.0, 2)
                      if last_ok_dt else None),
        "staleness_threshold_hours": None,
        "staleness_assessed": False,
        "staleness_note": ("no staleness threshold is configured for the "
                           "canonical deal ledger; age is reported, not "
                           "judged"),
    }


def _contacts_by_deal(rows) -> dict:
    out: dict = {}
    for r in rows or []:
        out.setdefault(str(r.get("deal_id")), []).append(r)
    return out


def _unavailable_window(window_type, window_key, bounds, reason, detail,
                        freshness) -> dict:
    statuses = {k: {"status": cwt.UNAVAILABLE, "reason": reason}
                for k in ("closed_won_deals", "revenue_usd", "customers",
                          "campaign_revenue", "acquisition_cohort")}
    statuses["roas"] = {"status": cwt.NOT_PUBLISHED,
                        "reason": cwt.R_ROAS_NOT_CERTIFIED}
    statuses["cac"] = {"status": cwt.NOT_PUBLISHED,
                       "reason": cwt.R_CAC_NOT_CERTIFIED}
    return {
        "available": False, "reason": reason, "detail": detail,
        "window": _window_meta(window_type, window_key, bounds),
        "definitions": DEFINITIONS,
        "outcomes": {"closed_won_deals": None, "customers": None,
                     "confirmed_customer_lower_bound": None,
                     "revenue_usd": None},
        "attribution": None, "acquisition_cohort": None,
        "coverage": {"source_freshness": freshness},
        "publication": statuses,
    }


def _window_meta(window_type, window_key, bounds) -> dict:
    return {"window_type": window_type, "window_key": window_key,
            "label": (bounds or {}).get("label"),
            "start_date": (bounds or {}).get("start_date"),
            "end_date": (bounds or {}).get("end_date"),
            "end_exclusive": True,
            "membership_date_field": "deal_close_date"}


def get_closed_won_truth(windows=None, *, now: datetime | None = None,
                         universe: dict | None = None,
                         resolver=None, resolver_detail=None) -> dict:
    """The closed-won truth for every requested window, from ONE snapshot.

    ``windows`` is a list of ``(window_type, window_key)``; all supported windows
    when None. ``universe`` / ``resolver`` are injectable for tests; production
    reads them here.
    """
    from services import canonical_revenue_service as crs  # noqa: PLC0415

    now = now or _utcnow()
    windows = list(windows or all_windows())
    if universe is None:
        universe = crs.load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID)
    if resolver is None and resolver_detail is None:
        resolver, resolver_detail = campaign_resolver(now)

    base = {"source": SOURCE, "generated_at": now.isoformat(),
            "rule_version": cwt.CLOSED_WON_TRUTH_RULE_VERSION,
            "hubspot_calls_performed": False,
            "google_ads_calls_performed": False,
            "external_writes_performed": False,
            "database_writes_performed": False,
            "customer_identity_source": COMPANY_ASSOCIATION_SOURCE,
            "customer_identity_gap": COMPANY_ASSOCIATION_GAP,
            "campaign_resolver": resolver_detail,
            "consumed_by_production_pages": False}

    if not universe.get("available"):
        freshness = {"signal": "canonical deal-ledger sync coverage",
                     "coverage_proven": None}
        out = []
        for wtype, wkey in windows:
            bounds = window_bounds(wtype, wkey, now)
            out.append(_unavailable_window(
                wtype, wkey, bounds, cwt.R_SOURCE_UNREADABLE,
                "the canonical deal ledger could not be read", freshness))
        return {**base, "available": False, "windows": out,
                "freshness": freshness}

    findings = universe.get("coverage_findings") or []
    freshness = freshness_block(universe.get("sync_state"), findings, now)
    contacts = _contacts_by_deal(universe.get("acquisition_contacts"))
    out = []
    for wtype, wkey in windows:
        bounds = window_bounds(wtype, wkey, now)
        result = cwt.evaluate_window(
            won_rows=universe.get("won_rows") or [],
            definition_rows=universe.get("won_definition_rows") or [],
            contacts_by_deal=contacts, start=bounds["start"],
            end=bounds["end"], is_all_time=bounds["is_all_time"], now=now,
            coverage_findings=findings, resolve_label=resolver,
            company_ids_by_deal=None)
        result["coverage"]["source_freshness"] = freshness
        out.append({"available": True,
                    "window": _window_meta(wtype, wkey, bounds),
                    "definitions": DEFINITIONS, **result})
    return {**base, "available": True, "windows": out, "freshness": freshness}


def get_window_outcome(window_type: str, window_key: str, *,
                       now: datetime | None = None) -> dict:
    """One window's closed-won truth."""
    return get_closed_won_truth([(window_type, window_key)],
                                now=now)["windows"][0]
