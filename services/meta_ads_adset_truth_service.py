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
#: The configured type is not one the sync stores (only types containing
#: "lead" are kept): its count would read as 0 on every ad set, forever.
REASON_LEAD_TYPE_NOT_STORED = "meta_lead_action_type_not_stored"
#: The account's stored rows have never carried the configured type (exact
#: key, case and all): its absence is not evidence that Meta reported none.
REASON_LEAD_TYPE_NOT_OBSERVED = "meta_lead_action_type_not_observed"
#: Some day's row carried no ``actions`` at all: the count is unknown there.
REASON_ACTIONS_MISSING = "meta_actions_missing_on_some_days"
REASON_COUNT_MISSING = "meta_count_missing_on_some_days"
#: PR-ADS-171D tripwire. In this account's stored rows the configured type and
#: `lead` differ on some day: the premise of the choice (identical counts) no
#: longer holds, and the configured type could be counting a subset.
REASON_LEAD_TYPES_DIVERGED = "meta_lead_action_types_diverged"

LEADS_DISCLOSURE = (
    "Meta-reported leads are Meta's own attributed counts under each ad "
    "set's attribution setting. They are not contacts or CRM leads and are "
    "not comparable with HubSpot counts.")

#: Fixed text: the configured string itself is never echoed.
LEAD_TYPE_NOT_STORED_DETAIL = (
    "meta_ads.native_lead_action_type names an action type the ad-set sync "
    "does not store (only types containing \"lead\" are kept); "
    "Meta-reported leads are unavailable until it names one that is.")


def lead_type_stored(lead_type: str | None) -> bool:
    """Whether the ad-set sync stores ``lead_type`` at all
    (``connectors.meta_ads.is_lead_action_type``). Pure."""
    from connectors.meta_ads import is_lead_action_type  # noqa: PLC0415
    return bool(lead_type) and is_lead_action_type(lead_type)


def _metric(value, reason=None, **extra) -> dict:
    """``{value, status, reason}``. A None value is ``unavailable`` and names
    why; it is never rendered as zero."""
    published = value is not None
    return {"value": value,
            "status": STATUS_PUBLISHED if published else STATUS_UNAVAILABLE,
            "reason": None if published else reason, **extra}


def lead_metrics(row: dict, lead_type: str | None, *,
                 type_observed: bool | None = None) -> dict:
    """Pure. The two lead figures of one aggregated ad-set row.

    ``meta_reported_lead_actions`` — the per-type counts, published only when
    every day's row carried ``actions``; the types overlap
    (``overlapping_types``) and are never added together.

    ``meta_reported_leads`` — the count of the ONE configured action type
    (``lead_type``), or unavailable: no type is configured
    (``meta_lead_action_type_unverified``), the type is one the sync never
    stores (``meta_lead_action_type_not_stored``; it is not echoed), or some
    day carried no ``actions`` (``meta_actions_missing_on_some_days``).

    With ``actions`` present on every day and the type absent from all of
    them, 0 is published ONLY when ``type_observed`` is True — the account's
    stored rows carry that exact key on some day. Otherwise the absence may
    just mean the name is wrong or this account never emits it:
    ``meta_lead_action_type_not_observed`` (False) or
    ``meta_adset_source_unavailable`` (None, not known).
    """
    missing = int(row.get("rows_without_actions") or 0)
    counts = dict(row.get("meta_reported_lead_action_counts") or {})
    raw = _metric(None if missing else counts, REASON_ACTIONS_MISSING,
                  rows_without_actions=missing, overlapping_types=True)
    stored = lead_type_stored(lead_type)
    if not lead_type:
        leads = _metric(None, REASON_LEAD_TYPE_UNVERIFIED)
    elif not stored:
        leads = _metric(None, REASON_LEAD_TYPE_NOT_STORED)
    elif missing:
        leads = _metric(None, REASON_ACTIONS_MISSING)
    elif lead_type in counts:
        leads = _metric(int(counts[lead_type]))
    elif type_observed is True:
        leads = _metric(0)                  # a type this account does emit
    else:
        leads = _metric(None, REASON_LEAD_TYPE_NOT_OBSERVED
                        if type_observed is False else REASON_SOURCE)
    leads["action_type"] = lead_type if stored else None
    return {"meta_reported_leads": leads, "meta_reported_lead_actions": raw}


#: PR-ADS-171D. Whether the attribution setting the lead counts were
#: computed under is on record for the published rows.
ATTRIBUTION_STORED = "stored"
ATTRIBUTION_PARTLY_STORED = "partly_stored"
ATTRIBUTION_NOT_STORED = "not_stored"
ATTRIBUTION_NO_ROWS = "no_rows"


def attribution_summary(rows: list) -> dict:
    """Pure. The attribution setting(s) behind ``meta_reported_leads``, read
    from the stored rows only — never assumed, never a default.

    ``settings``: the distinct stored values over ``rows``;
    ``rows_without_setting``: stored day-rows that carry none; ``status``:
    ``stored`` (every day-row carried one), ``partly_stored``, ``not_stored``
    (none did — the setting is not on record) or ``no_rows``.
    """
    settings = sorted({s for r in rows or [] for s in r.get("attribution_settings") or []})
    missing = sum(int(r.get("attribution_setting_missing_rows") or 0) for r in rows or [])
    if not rows:
        status = ATTRIBUTION_NO_ROWS
    elif not settings:
        status = ATTRIBUTION_NOT_STORED
    elif missing:
        status = ATTRIBUTION_PARTLY_STORED
    else:
        status = ATTRIBUTION_STORED
    return {"settings": settings, "rows_without_setting": missing,
            "status": status,
            "detail": ("not stored: no stored ad-set row of this range carries "
                       "an attribution setting" if status == ATTRIBUTION_NOT_STORED
                       else None)}


def _tripped(figures: dict, diverged_rows: int) -> dict:
    """``meta_reported_leads`` made unavailable when the lead-type tripwire
    fired for the account (``meta_lead_action_types_diverged``). The raw
    per-type counts are left as they are: they are Meta's, not our choice."""
    if diverged_rows:
        leads = figures["meta_reported_leads"]
        figures["meta_reported_leads"] = {
            **_metric(None, REASON_LEAD_TYPES_DIVERGED),
            "action_type": leads.get("action_type")}
    return figures


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

    ``{status, reason, detail, rows, accounts, native_lead_action_type,
    meta_reported_leads_attribution, …}``.
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
    stored_type = lead_type_stored(lead_type)
    frame = {**_frame(start_date, end_date, settled, base),
             # A refused type is reported by a fixed message, never echoed.
             "native_lead_action_type": lead_type if stored_type else None,
             "config_problems": [] if stored_type or not lead_type else [{
                 "key": "meta_ads.native_lead_action_type",
                 "reason": REASON_LEAD_TYPE_NOT_STORED,
                 "detail": LEAD_TYPE_NOT_STORED_DETAIL}]}

    def withheld(status, reason, detail=None, accounts=()):
        # Withheld means absent: no attribution is reported for rows that are
        # not published.
        return {**frame, "status": status, "reason": reason, "detail": detail,
                "rows": [], "accounts": list(accounts),
                "meta_reported_leads_attribution": None}

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
                   "adset_coverage": "verified", "reason": None, "detail": None,
                   # Has this account's stored detail ever carried the type?
                   "meta_reported_leads_type_observed": None,
                   # PR-ADS-171D tripwire: {rows, first_day} or None.
                   "meta_reported_lead_types_diverged": None}
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
            tolerance_micros=meta.reconciliation_tolerance_micros(currency),
            observed_action_type=lead_type if stored_type else None)
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
        observed = read.get("action_type_observed") if stored_type else None
        summary["meta_reported_leads_type_observed"] = observed
        # The tripwire speaks only for a type the account has emitted: an
        # unobserved type is already unavailable for its own reason.
        diverged = (read.get("lead_types_diverged")
                    if stored_type and observed is True else None)
        diverged_rows = int((diverged or {}).get("rows") or 0)
        summary["meta_reported_lead_types_diverged"] = diverged if diverged_rows else None
        if diverged_rows:
            frame["config_problems"].append({
                "key": "meta_ads.native_lead_action_type",
                "reason": REASON_LEAD_TYPES_DIVERGED,
                "account_id": account_id,
                "detail": (f"account {account_id}: on {diverged_rows} stored "
                           "day-row(s) (first "
                           f"{diverged.get('first_day')}) the configured type's "
                           "count differs from `lead`'s; it was chosen because "
                           "they were identical. Meta-reported leads are "
                           "unavailable for this account until the choice is "
                           "reviewed.")})
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
                **_tripped(lead_metrics(r, lead_type, type_observed=observed),
                           diverged_rows),
                "attribution_settings": r.get("attribution_settings") or [],
                "attribution_setting_missing_rows":
                    int(r.get("attribution_setting_missing_rows") or 0),
            })

    if blocked is not None:
        # One account short withholds every account: a breakdown that leaves
        # an account out does not add up to the published campaign spend.
        return withheld(*blocked, accounts=accounts)
    return {**frame, "status": STATUS_PUBLISHED, "reason": None, "detail": None,
            "rows": rows, "accounts": accounts,
            # PR-ADS-171D: the setting the published lead counts are under,
            # from the stored rows ("not stored" when none carries one).
            "meta_reported_leads_attribution": attribution_summary(rows)}
