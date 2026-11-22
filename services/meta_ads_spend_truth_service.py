"""PR-ADS-171A — the Meta Ads spend truth for a range of account days.

The Meta counterpart of ``services.revenue_spend_truth_service``: one state
machine that says whether Meta spend over ``[start, end]`` may be published,
and the figures when it may.

There may be several ad accounts. Each is judged ON ITS OWN — its own coverage
ledger, the settle rule in its own time zone, its own currency's rates — by
:func:`derive_meta_ads_spend_truth`; :func:`combine_account_truths` then
publishes a total only when EVERY relevant account passes.

States (of an account, and of the total)
----------------------------------------
``not_connected``        no account is relevant to the range: Meta is not set
                         up and no stored history reaches it. Every figure is
                         None. Not a zero and not an error.
``source_unavailable``   the stored spend could not be read, or the
                         configuration is broken. Every figure None.
``coverage_incomplete``  some required day of some account has no verified
                         read — or, with ``settled``, is not held completely.
                         The total is None.
``fx_incomplete``        coverage is verified; some account's currency could
                         not be converted for every spend day. USD is None;
                         the native total is published only when the accounts
                         share one currency. ``fx_reason`` says why.
``verified_zero_spend``  coverage and FX verified; the range spent nothing.
                         A published 0.
``verified``             coverage and FX verified; spend above zero.

Which accounts are relevant, and for which days
-----------------------------------------------
* A CONFIGURED account must be covered for every day of the range. One that
  was never read, or that Meta no longer lets us read, therefore withholds
  every range that reaches past its last verified day — until the owner
  removes it from the configuration.
* An account that is stored but NO LONGER CONFIGURED (removed by the owner
  when it was closed) keeps counting: it must be covered for every day of the
  range up to its last verified day, and after that day it contributes
  nothing and requires nothing. Removing an account is the owner's statement
  that it has stopped spending; the payload discloses it
  (``accounts[].configured`` is False, ``required_through`` is that day).
  Remove an account only once it has stopped spending.
* For all time (``start`` None) each account counts from its own first
  verified day.

Rules
-----
* USD is verified for a USD account (rate 1) or when every spend day has its
  own rate. A currency the FX provider does not price is never converted.
  Each account converts with its own currency's rates; the USD total is the
  sum.
* A currency is an account's, from ``meta_ads_account``. Unknown is None —
  never a default. Accounts in different currencies have no native total.
* ``settled`` applies the shared settle rule to each account in THAT
  account's zone (``meta_ads_account.time_zone_name``). An unknown zone
  settles nothing. Different zones are disclosed (``mixed_time_zones``): the
  same calendar date is a different 24 hours in each.

Read-only: SELECTs on the local database. No call to Meta.
"""
from __future__ import annotations

from datetime import date, datetime

STATE_VERIFIED = "verified"
STATE_VERIFIED_ZERO = "verified_zero_spend"
STATE_FX_INCOMPLETE = "fx_incomplete"
STATE_COVERAGE_INCOMPLETE = "coverage_incomplete"
STATE_SOURCE_UNAVAILABLE = "source_unavailable"
STATE_NOT_CONNECTED = "not_connected"

SPEND_SOURCE = "meta_campaign_daily_spend"


def _round2(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def derive_meta_ads_spend_truth(inputs: dict | None) -> dict:
    """Pure. ``inputs``: ``{connected, available, coverage_complete,
    native_currency, native_total, usd_total, fx_status, fx_reason}``.

    ``fx_status`` is ``verified`` | ``incomplete`` | anything else (read as
    unavailable). Nothing is defaulted: a missing input withholds.
    """
    data = inputs or {}
    currency = data.get("native_currency") or None
    native_total = _round2(data.get("native_total"))
    usd_total = _round2(data.get("usd_total"))
    fx_in = data.get("fx_status")
    fx_status = fx_in if fx_in in ("verified", "incomplete") else "unavailable"

    if data.get("connected") is not True:
        state = STATE_NOT_CONNECTED
    elif data.get("available") is not True or native_total is None:
        state = STATE_SOURCE_UNAVAILABLE
    elif data.get("coverage_complete") is not True:
        state = STATE_COVERAGE_INCOMPLETE
    elif fx_status != "verified" or usd_total is None:
        state = STATE_FX_INCOMPLETE
    elif usd_total <= 0 and native_total <= 0:
        state = STATE_VERIFIED_ZERO
    else:
        state = STATE_VERIFIED

    verified = state in (STATE_VERIFIED, STATE_VERIFIED_ZERO)
    if state == STATE_NOT_CONNECTED:
        coverage = "not_connected"
    elif state == STATE_SOURCE_UNAVAILABLE:
        coverage = "unavailable"
    elif state == STATE_COVERAGE_INCOMPLETE:
        coverage = "incomplete"
    else:
        coverage = "verified"
    connected = state != STATE_NOT_CONNECTED
    return {
        "state": state,
        "meta_ads_spend_source": (SPEND_SOURCE if state not in (
            STATE_NOT_CONNECTED, STATE_SOURCE_UNAVAILABLE) else "unavailable"),
        "native_currency": currency if connected else None,
        "native_spend": (native_total if verified
                         or state == STATE_FX_INCOMPLETE else None),
        "usd_spend": usd_total if verified else None,
        "fx_status": fx_status if connected else "unavailable",
        "fx_reason": (data.get("fx_reason")
                      if state == STATE_FX_INCOMPLETE else None),
        "spend_coverage_status": coverage,
    }


def combine_account_truths(entries: list) -> dict:
    """Pure. The total over per-account truths (each a
    :func:`derive_meta_ads_spend_truth` result plus ``account_id``,
    ``currency`` …). A figure is summed only when every account publishes it.
    """
    if not entries:
        return derive_meta_ads_spend_truth({"connected": False})
    states = [e["state"] for e in entries]
    currencies = {e.get("currency") for e in entries
                  if (e.get("native_spend") or 0) != 0}
    if not currencies:
        currencies = {e.get("currency") for e in entries}
    common = next(iter(currencies)) if len(currencies) == 1 else None

    def total(key):
        values = [e.get(key) for e in entries]
        return None if any(v is None for v in values) else round(sum(values), 2)

    if STATE_SOURCE_UNAVAILABLE in states or STATE_NOT_CONNECTED in states:
        state = STATE_SOURCE_UNAVAILABLE
    elif STATE_COVERAGE_INCOMPLETE in states:
        state = STATE_COVERAGE_INCOMPLETE
    elif STATE_FX_INCOMPLETE in states:
        state = STATE_FX_INCOMPLETE
    else:
        usd = total("usd_spend")
        state = (STATE_SOURCE_UNAVAILABLE if usd is None
                 else STATE_VERIFIED if usd > 0 else STATE_VERIFIED_ZERO)
    verified = state in (STATE_VERIFIED, STATE_VERIFIED_ZERO)
    native_ok = (verified or state == STATE_FX_INCOMPLETE) and common is not None
    return {
        "state": state,
        "meta_ads_spend_source": (SPEND_SOURCE if state != STATE_SOURCE_UNAVAILABLE
                                  else "unavailable"),
        "native_currency": common,
        "native_spend": total("native_spend") if native_ok else None,
        "usd_spend": total("usd_spend") if verified else None,
        "fx_status": ("verified" if verified
                      else "incomplete" if state == STATE_FX_INCOMPLETE
                      else "unavailable"),
        "fx_reason": (next((e.get("fx_reason") for e in entries
                            if e["state"] == STATE_FX_INCOMPLETE), None)
                      if state == STATE_FX_INCOMPLETE else None),
        "spend_coverage_status": ("verified" if verified
                                  or state == STATE_FX_INCOMPLETE
                                  else "incomplete"
                                  if state == STATE_COVERAGE_INCOMPLETE
                                  else "unavailable"),
    }


def _frame(start_date, end_date, settled) -> dict:
    return {"start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat(),
            "unsettled_reason": None, "settled_rule_applied": bool(settled)}


_NO_ACCOUNTS = {"accounts": [], "account_ids": [], "time_zones": [],
                "mixed_time_zones": False, "mixed_currencies": False}


def build_meta_ads_spend_truth_for_days(start_date, end_date, *,
                                        settled: bool = False,
                                        now: datetime | None = None) -> dict:
    """Meta spend truth over inclusive account-local days (``start_date`` None
    for all time), summed over every relevant ad account. A figure is
    returned only in a state that publishes it.

    ``accounts`` lists each relevant account: id, name, currency, time zone,
    whether it is still configured, its own state and figures, the last day it
    is required for, and what its latest sync said.
    """
    from db import meta_ads_repository as repo  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415
    from services.google_ads_spend_service import (  # noqa: PLC0415
        analyze_coverage, analyze_settled_coverage, zone_or_none,
    )

    def finish(truth, accounts=()):
        zones = sorted({a["time_zone"] for a in accounts if a.get("time_zone")})
        currencies = {a["currency"] for a in accounts if a.get("currency")}
        return {**truth, **_frame(start_date, end_date, settled),
                "accounts": list(accounts),
                "account_ids": [a["account_id"] for a in accounts],
                "time_zones": zones, "mixed_time_zones": len(zones) > 1,
                "mixed_currencies": len(currencies) > 1,
                **{k: truth[k] for k in ("unsettled_reason",) if k in truth}}

    unavailable = derive_meta_ads_spend_truth({"connected": True, "available": False})
    cfg = meta.config_status()
    if cfg.get("misconfigured"):
        # Set up, but wrongly. Not "not connected": something is broken.
        return finish(unavailable)
    configured = list(cfg.get("account_ids") or [])
    read = repo.fetch_meta_accounts()
    if not read.get("available"):
        # Whether history exists is unknown: that is not "not connected".
        return finish(unavailable)
    stored = {a["account_id"]: a for a in read.get("accounts") or []}
    states = (repo.fetch_meta_account_sync_states().get("states") or {})

    # (account_id, row|None, required_end)
    retired = []
    for account_id, row in stored.items():
        if account_id in configured or not row.get("covered_through"):
            continue
        last = date.fromisoformat(row["covered_through"])
        if start_date is None or start_date <= last:
            retired.append((account_id, row, min(end_date, last)))
    relevant = []
    if retired or any(a in stored for a in configured):
        # Once anything is on record, EVERY configured account is required —
        # including one that has never been read.
        relevant = [(a, stored.get(a), end_date) for a in configured] + retired
    if not relevant:
        return finish(derive_meta_ads_spend_truth({"connected": False}))

    priced = meta.fx_provider_currencies()
    entries = []
    for account_id, row, required_end in relevant:
        state = states.get(account_id) or {}
        entry = {"account_id": account_id,
                 "account_name": (row or {}).get("account_name"),
                 "currency": (row or {}).get("currency_code") or None,
                 "time_zone": (row or {}).get("time_zone_name") or None,
                 "configured": account_id in configured,
                 "required_through": required_end.isoformat(),
                 "covered_through": (row or {}).get("covered_through"),
                 "last_sync_status": state.get("last_status"),
                 "last_sync_reason": state.get("last_reason"),
                 "not_readable_since": (state["not_readable_since"].isoformat()
                                        if state.get("not_readable_since") else None),
                 "unsettled_reason": None}
        if row is None:
            # Configured, never read: nothing is known about its spend.
            entries.append({**entry, **derive_meta_ads_spend_truth({
                "connected": True, "available": True, "native_total": 0.0,
                "coverage_complete": False}), "never_read": True})
            continue
        coverage = repo.fetch_meta_spend_coverage(start_date, required_end, account_id)
        spend = repo.fetch_meta_campaign_spend(start_date, required_end, account_id)
        if not coverage.get("available") or not spend.get("available"):
            entries.append({**entry, **unavailable})
            continue
        chunks = coverage.get("chunks") or []
        complete = analyze_coverage(
            start_date, required_end, chunks).get("complete") is True
        currency = (entry["currency"] or "").upper()
        fx_ok = spend.get("fx_complete") is True
        fx_reason = None
        if not fx_ok:
            fx_reason = (meta.REASON_FX_UNSUPPORTED if currency not in priced
                         else meta.REASON_FX_MISSING)
        truth = derive_meta_ads_spend_truth({
            "connected": True, "available": True, "coverage_complete": complete,
            "native_currency": entry["currency"],
            "native_total": spend.get("total_spend"),
            "usd_total": spend.get("total_spend_usd") if fx_ok else None,
            "fx_status": "verified" if fx_ok else "incomplete",
            "fx_reason": fx_reason})
        entry["usd_identity"] = currency == repo.REPORTING_CURRENCY
        if settled and truth["state"] in (STATE_VERIFIED, STATE_VERIFIED_ZERO,
                                          STATE_FX_INCOMPLETE):
            settle = analyze_settled_coverage(
                start_date, required_end, chunks,
                zone_or_none(entry["time_zone"]), now=now)
            if not settle.get("complete"):
                truth.update({
                    "state": STATE_COVERAGE_INCOMPLETE, "usd_spend": None,
                    "native_spend": None, "spend_coverage_status": "incomplete",
                    "fx_reason": None})
                entry["unsettled_reason"] = (settle.get("unsettled_reason")
                                             or "spend_days_not_settled")
        entries.append({**entry, **truth})

    total = combine_account_truths(entries)
    if total["state"] == STATE_COVERAGE_INCOMPLETE:
        total["unsettled_reason"] = next(
            (e["unsettled_reason"] for e in entries if e.get("unsettled_reason")),
            None)
    for entry in entries:
        entry.pop("meta_ads_spend_source", None)
        entry.pop("native_currency", None)
    return finish(total, entries)
