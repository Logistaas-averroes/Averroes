"""PR-ADS-171C — Meta ad-set figures for a range of account days.

The read function a future page will publish from (PR-ADS-171B / 171D). No
page, no API route reads it yet.

:func:`build_meta_adset_breakdown_for_days` returns one row per account,
campaign and ad set — spend (account currency and USD), impressions, clicks
and ``meta_reported_leads`` — each as ``{value, status, reason}``.

When it publishes
-----------------
Which accounts a range needs, through which day, and what an owner's
retirement declaration means are NOT decided here: this calls
``meta_ads_spend_truth_service.build_meta_ads_spend_truth_for_days`` and uses
its ``accounts`` and each one's ``required_through``. On top of that:

* the campaign-level truth must itself publish the range — the ad-set rows
  are a detail of it, never a substitute;
* EVERY relevant account's AD-SET ledger must be verified for every one of
  its required days (and, with ``settled``, held completely in that account's
  zone) — otherwise nothing is returned for ANY account
  (``meta_adset_coverage_incomplete``): a breakdown with an account missing
  would not add up to the published campaign spend;
* the stored ad-set rows must still sum to the stored campaign rows, per day
  and campaign, decided by the database at read time
  (``meta_adset_reconciliation_mismatch``). The campaign level is re-read
  every day; an ad-set read that then fails leaves ad-set rows that describe
  yesterday's campaign figures, and the ledger alone cannot see that.

Withheld means absent: ``rows`` is empty unless ``status`` is ``published``.

What the figures are
--------------------
* Ids are the identity. ``adset_name`` / ``campaign_name`` are the newest
  labels on record and must never be used to join anything — a renamed
  campaign or ad set keeps its id.
* ``meta_reported_leads`` and ``meta_reported_lead_actions`` are Meta's own
  attributed counts under each ad set's attribution setting
  (``attribution_settings``). They are not contacts, not CRM leads, not MQLs
  and not SQLs, and must never be added to, subtracted from or divided by a
  HubSpot count. The action types overlap and are never summed.
* A missing figure is ``unavailable`` with a reason — never 0.

Read-only: SELECTs on the local database. No call to Meta.
"""
from __future__ import annotations

from datetime import date, datetime

STATUS_PUBLISHED = "published"
STATUS_WITHHELD = "withheld"
STATUS_UNAVAILABLE = "unavailable"

METRIC_FAMILY = "meta_reported_adset_delivery"
WINDOW_BASIS = "meta_account_local_spend_date"

#: The campaign-level truth does not publish this range.
REASON_CAMPAIGN_BASE = "meta_campaign_spend_not_published"
REASON_COVERAGE = "meta_adset_coverage_incomplete"
REASON_NOT_SETTLED = "meta_adset_days_not_settled"
REASON_SOURCE = "meta_adset_source_unavailable"
REASON_MISMATCH = "meta_adset_reconciliation_mismatch"
#: No action type has been confirmed as the native lead-form count.
REASON_LEAD_TYPE_UNVERIFIED = "meta_lead_action_type_unverified"
#: Some day's row carried no ``actions`` at all: the count is unknown there.
REASON_ACTIONS_MISSING = "meta_actions_missing_on_some_days"
REASON_COUNT_MISSING = "meta_count_missing_on_some_days"

LEADS_DISCLOSURE = (
    "Meta-reported leads are Meta's own attributed counts under each ad "
    "set's attribution setting. They are not contacts or CRM leads and are "
    "not comparable with HubSpot counts.")


def _metric(value, reason=None, **extra) -> dict:
    """``{value, status, reason}``. A None value is ``unavailable`` and names
    why; it is never rendered as zero."""
    published = value is not None
    return {"value": value,
            "status": STATUS_PUBLISHED if published else STATUS_UNAVAILABLE,
            "reason": None if published else reason, **extra}


def lead_metrics(row: dict, lead_type: str | None) -> dict:
    """Pure. The two lead figures of one aggregated ad-set row.

    ``meta_reported_lead_actions`` — the per-type counts, published only when
    every day's row carried ``actions``; the types overlap
    (``overlapping_types``) and are never added together.

    ``meta_reported_leads`` — the count of the ONE configured action type
    (``lead_type``), or unavailable: no type is configured
    (``meta_lead_action_type_unverified``), or some day carried no
    ``actions`` (``meta_actions_missing_on_some_days``). With ``actions``
    present on every day and the type absent from all of them, Meta reported
    none: 0.
    """
    missing = int(row.get("rows_without_actions") or 0)
    counts = dict(row.get("meta_reported_lead_action_counts") or {})
    raw = _metric(None if missing else counts, REASON_ACTIONS_MISSING,
                  rows_without_actions=missing, overlapping_types=True)
    if not lead_type:
        leads = _metric(None, REASON_LEAD_TYPE_UNVERIFIED)
    elif missing:
        leads = _metric(None, REASON_ACTIONS_MISSING)
    else:
        leads = _metric(int(counts.get(lead_type, 0)))
    leads["action_type"] = lead_type
    return {"meta_reported_leads": leads, "meta_reported_lead_actions": raw}


def _frame(start_date, end_date, settled, base) -> dict:
    return {
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat(),
        "settled_rule_applied": bool(settled),
        "metric_family": METRIC_FAMILY, "window_basis": WINDOW_BASIS,
        "names_are_labels": True,
        "meta_reported_leads_disclosure": LEADS_DISCLOSURE,
        "campaign_spend_state": base.get("state"),
        "time_zones": base.get("time_zones") or [],
        "mixed_time_zones": bool(base.get("mixed_time_zones")),
        "mixed_currencies": bool(base.get("mixed_currencies")),
        # A figure that rests on the owner's retirement declaration says so.
        "owner_declared_retired": base.get("owner_declared_retired") or [],
        "owner_declared_retired_disclosure":
            base.get("owner_declared_retired_disclosure"),
    }


def build_meta_adset_breakdown_for_days(start_date, end_date, *,
                                        settled: bool = False,
                                        now: datetime | None = None) -> dict:
    """Meta ad-set figures over inclusive account-local days (``start_date``
    None for all time: each account from its first verified CAMPAIGN day).

    ``{status, reason, detail, rows, accounts, native_lead_action_type, …}``.
    ``rows`` is empty unless ``status`` is ``published``; then it holds one
    entry per account, campaign and ad set with ``spend``, ``spend_usd``,
    ``impressions``, ``clicks``, ``meta_reported_leads`` and
    ``meta_reported_lead_actions`` as ``{value, status, reason}``.
    """
    from db import meta_ads_repository as repo  # noqa: PLC0415
    from services import meta_ads_adset_service as adsets  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415
    from services import meta_ads_spend_truth_service as campaign  # noqa: PLC0415
    from services.google_ads_spend_service import (  # noqa: PLC0415
        analyze_coverage, analyze_settled_coverage, zone_or_none,
    )

    # Relevance, required days and retirement: the campaign truth's, verbatim.
    base = campaign.build_meta_ads_spend_truth_for_days(
        start_date, end_date, settled=settled, now=now)
    lead_type = adsets.native_lead_action_type()
    frame = {**_frame(start_date, end_date, settled, base),
             "native_lead_action_type": lead_type}

    def withheld(status, reason, detail=None, accounts=()):
        return {**frame, "status": status, "reason": reason, "detail": detail,
                "rows": [], "accounts": list(accounts)}

    if base.get("state") not in campaign.PUBLISHING_STATES:
        unreadable = base.get("state") in (campaign.STATE_NOT_CONNECTED,
                                           campaign.STATE_SOURCE_UNAVAILABLE)
        return withheld(STATUS_UNAVAILABLE if unreadable else STATUS_WITHHELD,
                        REASON_CAMPAIGN_BASE,
                        f"campaign-level Meta spend is {base.get('state')}"
                        + (f": {base['withheld_detail']}"
                           if base.get("withheld_detail") else ""))

    priced = meta.fx_provider_currencies()
    accounts, rows, blocked = [], [], None
    for entry in base.get("accounts") or []:
        account_id = entry["account_id"]
        summary = {"account_id": account_id,
                   "account_name": entry.get("account_name"),
                   "currency": entry.get("currency"),
                   "time_zone": entry.get("time_zone"),
                   "required_through": entry.get("required_through"),
                   "adset_coverage": "verified", "reason": None, "detail": None}
        accounts.append(summary)

        def block(status, reason, detail, coverage):
            nonlocal blocked
            summary.update(adset_coverage=coverage, reason=reason, detail=detail)
            if blocked is None or (blocked[0] == STATUS_WITHHELD
                                   and status == STATUS_UNAVAILABLE):
                blocked = (status, reason, detail)

        required_end = date.fromisoformat(entry["required_through"])
        # All time counts from the account's first verified CAMPAIGN day —
        # not from its first ad-set chunk, which would let an ad-set history
        # shorter than the campaign history pass as complete.
        first = start_date or (date.fromisoformat(entry["first_verified_day"])
                               if entry.get("first_verified_day") else None)
        if first is None:
            block(STATUS_WITHHELD, REASON_COVERAGE,
                  f"account {account_id} has no verified day", "incomplete")
            continue
        if first > required_end:
            continue                    # no day of the range is this account's
        coverage = repo.fetch_meta_adset_coverage(first, required_end, account_id)
        if not coverage.get("available"):
            block(STATUS_UNAVAILABLE, REASON_SOURCE,
                  f"account {account_id}: the ad-set ledger could not be read",
                  "unavailable")
            continue
        chunks = coverage.get("chunks") or []
        analysis = analyze_coverage(first, required_end, chunks)
        if analysis.get("complete") is not True:
            block(STATUS_WITHHELD, REASON_COVERAGE,
                  f"account {account_id}: ad-set detail is not verified for "
                  f"every day of {first.isoformat()}..{required_end.isoformat()}"
                  + (f" ({analysis.get('missing_days')} day(s) missing)"
                     if analysis.get("missing_days") else ""), "incomplete")
            continue
        if settled:
            settle = analyze_settled_coverage(
                first, required_end, chunks, zone_or_none(entry.get("time_zone")),
                now=now)
            if not settle.get("complete"):
                block(STATUS_WITHHELD, REASON_NOT_SETTLED,
                      f"account {account_id}: "
                      + str(settle.get("unsettled_reason")
                            or "spend_days_not_settled"), "not_settled")
                continue
        currency = (entry.get("currency") or "").upper() or None
        read = repo.fetch_meta_adset_spend(
            first, required_end, account_id,
            tolerance_micros=meta.reconciliation_tolerance_micros(currency))
        if not read.get("available"):
            block(STATUS_UNAVAILABLE, REASON_SOURCE,
                  f"account {account_id}: the ad-set rows could not be read",
                  "unavailable")
            continue
        if read.get("mismatches"):
            miss = read["mismatches"][0]
            block(STATUS_WITHHELD, REASON_MISMATCH,
                  f"account {account_id}: {len(read['mismatches'])} "
                  "campaign-day(s) of ad-set rows no longer sum to the "
                  f"campaign row (first {miss['spend_date']} campaign "
                  f"{miss['campaign_id']})", "mismatch")
            continue
        fx_reason = (meta.REASON_FX_UNSUPPORTED
                     if (currency or "") not in priced else meta.REASON_FX_MISSING)
        for r in read["rows"]:
            rows.append({
                "account_id": account_id,
                "account_name": entry.get("account_name"),
                "time_zone": entry.get("time_zone"),
                "campaign_id": r["campaign_id"],
                "campaign_name": r.get("campaign_name"),
                "adset_id": r["adset_id"],
                "adset_name": r.get("adset_name"),
                "first_day": r.get("first_day"), "last_day": r.get("last_day"),
                "spend": _metric(round(r["spend"], 2), currency=currency),
                "spend_usd": _metric(
                    round(r["spend_usd"], 2) if r.get("spend_usd") is not None
                    else None, fx_reason, currency=meta.REPORTING_CURRENCY),
                "impressions": _metric(r.get("impressions"), REASON_COUNT_MISSING),
                "clicks": _metric(r.get("clicks"), REASON_COUNT_MISSING),
                **lead_metrics(r, lead_type),
                "attribution_settings": r.get("attribution_settings") or [],
            })

    if blocked is not None:
        # One account short withholds every account: a breakdown that leaves
        # an account out does not add up to the published campaign spend.
        return withheld(*blocked, accounts=accounts)
    return {**frame, "status": STATUS_PUBLISHED, "reason": None, "detail": None,
            "rows": rows, "accounts": accounts}
