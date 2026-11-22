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
* Every account LISTED in any credential set — complete or broken — and every
  account that is stored is required for EVERY day of the range. One that was
  never read, that Meta no longer lets us read, whose credential set is
  broken, or that was simply dropped from the configuration therefore
  withholds every range that reaches past its last verified day. Being absent
  from the configuration means nothing.
* The one exception is the owner's explicit declaration
  (``META_AD_ACCOUNT_RETIRED=<id>:<last spend day>``) that an account stopped
  spending. It is honoured only for an account that is NOT being read (in no
  credential set, or refused by Meta) and has no stored spend after the
  declared day; see :func:`declaration_verdict`. A total that rests on a
  declaration says so at the top level (``owner_declared_retired`` — with the
  first day it rests on — and ``owner_declared_retired_disclosure``); a
  consumer must show it.
* For all time (``start`` None) each account counts from its own first
  verified day (``first_verified_days``).
* ``not_connected`` only when no account is listed and none is stored. When
  every stored account is validly declared retired before the range, the
  result is a zero that carries the declarations — never an unmarked one.

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

from datetime import date, datetime, timedelta

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


PUBLISHING_STATES = (STATE_VERIFIED, STATE_VERIFIED_ZERO, STATE_FX_INCOMPLETE)

#: A declaration names a day the account's verified coverage does not reach.
DECLARATION_NOT_COVERED = "retired_declaration_not_covered"
#: The account has stored spend AFTER the day it was declared to stop on.
DECLARATION_CONTRADICTED = "retired_declaration_contradicted"
#: A declaration names an account that was never read.
DECLARATION_UNKNOWN_ACCOUNT = "retired_declaration_unknown_account"
#: A declaration names an account that is configured and that Meta still
#: answers for. It keeps being measured; the declaration is not honoured.
DECLARATION_STILL_READ = "retired_declaration_account_still_read"

DISCLOSURE = ("This total assumes {accounts} — the owner's declaration "
              "(META_AD_ACCOUNT_RETIRED), not a reading from Meta.")


def declaration_verdict(*, day: date, covered_through: date | None,
                        last_spend: date | None, being_read: bool,
                        start_date: date | None, end_date: date) -> dict:
    """Pure. What one retirement declaration means for one range.

    ``{honoured, problem, required_end, relied_from}``:

    * ``problem`` set, ``honoured`` False — the account stays required through
      ``end_date`` like any other:
        - the account is still being read (configured, and Meta answers);
        - it has stored spend dated after the declared day;
        - the range touches days between its last verified day and the
          declared day — days it is said to have spent on that were never
          read.
    * ``honoured`` True — the range reaches past what is on record and rests
      on the declaration from ``relied_from`` on. ``required_end`` is the last
      day the account must still be verified for, or None when no day of the
      range is the account's.
    * neither — the range lies within the account's verified days: nothing
      rests on the declaration.

    A declared day LATER than the last verified day is allowed, so the owner
    never has to declare a false day: ranges up to the last verified day are
    published as measured, ranges touching the unread days in between are
    withheld, and ranges wholly after the declared day rest on the
    declaration.
    """
    out = {"honoured": False, "problem": None, "required_end": end_date,
           "relied_from": None}
    if being_read:
        return {**out, "problem": DECLARATION_STILL_READ}
    if last_spend is not None and last_spend > day:
        return {**out, "problem": DECLARATION_CONTRADICTED}
    if covered_through is not None and end_date <= covered_through:
        return out                         # measured; nothing is relied on
    settled_until = max(day, covered_through) if covered_through else day
    relied_from = settled_until + timedelta(days=1)
    if covered_through is not None and covered_through >= day:
        after = start_date is not None and start_date > covered_through
        return {**out, "honoured": True, "relied_from": relied_from,
                "required_end": None if after else covered_through}
    # The declared day is past the ledger: (covered_through, day] was never read.
    if start_date is not None and start_date > day:
        return {**out, "honoured": True, "relied_from": relied_from,
                "required_end": None}
    return {**out, "problem": DECLARATION_NOT_COVERED}


def build_meta_ads_spend_truth_for_days(start_date, end_date, *,
                                        settled: bool = False,
                                        now: datetime | None = None) -> dict:
    """Meta spend truth over inclusive account-local days (``start_date`` None
    for all time), summed over every relevant ad account. A figure is
    returned only in a state that publishes it.

    ``accounts`` lists each relevant account: id, name, currency, time zone,
    whether it is configured and whether it syncs, its own state and figures,
    the last day it is required for, its first verified day, and what its
    latest sync said.

    ``owner_declared_retired`` is non-empty exactly when the published figure
    relies on the owner's declaration that an account stopped spending; a
    consumer must show ``owner_declared_retired_disclosure`` beside it.
    """
    from db import meta_ads_repository as repo  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415
    from services.google_ads_spend_service import (  # noqa: PLC0415
        analyze_coverage, analyze_settled_coverage, zone_or_none,
    )

    problems: list = []

    def finish(truth, accounts=(), relied=()):
        zones = sorted({a["time_zone"] for a in accounts if a.get("time_zone")})
        currencies = {a["currency"] for a in accounts if a.get("currency")}
        relied = list(relied) if truth["state"] in PUBLISHING_STATES else []
        disclosure = None
        if relied:
            disclosure = DISCLOSURE.format(accounts="; ".join(
                f"ad account {r['account_id']} spent nothing from "
                f"{r['relied_from']} on (declared last spend day "
                f"{r['last_spend_day']})" for r in relied))
        out = {**truth, **_frame(start_date, end_date, settled),
               "accounts": list(accounts),
               "account_ids": [a["account_id"] for a in accounts],
               "time_zones": zones, "mixed_time_zones": len(zones) > 1,
               "mixed_currencies": len(currencies) > 1,
               "first_verified_days": [
                   {"account_id": a["account_id"],
                    "first_verified_day": a.get("first_verified_day")}
                   for a in accounts],
               "owner_declared_retired": relied,
               "owner_declared_retired_disclosure": disclosure,
               "retired_declaration_problems": list(problems)}
        if "unsettled_reason" in truth:
            out["unsettled_reason"] = truth["unsettled_reason"]
        out["withheld_detail"] = (
            truth.get("withheld_detail")
            if truth["state"] == STATE_COVERAGE_INCOMPLETE else None)
        return out

    unavailable = derive_meta_ads_spend_truth({"connected": True, "available": False})
    cfg = meta.config_status()
    if cfg.get("misconfigured") or cfg.get("unparsed_account_entries"):
        # Set up, but wrongly — or an account list that cannot be read, so the
        # configured accounts are not known. Not "not connected".
        return finish(unavailable)
    # Configured = LISTED in any credential set, complete or broken. An
    # account in a broken set does not sync; it is not thereby removed.
    listed = list(cfg.get("listed_account_ids") or [])
    syncing = set(cfg.get("account_ids") or [])
    declared = {d["account_id"]: date.fromisoformat(d["last_spend_day"])
                for d in cfg.get("retired") or []}
    read = repo.fetch_meta_accounts()
    if not read.get("available"):
        # Whether history exists is unknown: that is not "not connected".
        return finish(unavailable)
    stored = {a["account_id"]: a for a in read.get("accounts") or []}
    states = (repo.fetch_meta_account_sync_states().get("states") or {})
    for account_id, day in declared.items():
        if account_id not in stored:
            problems.append({"account_id": account_id,
                             "reason": DECLARATION_UNKNOWN_ACCOUNT,
                             "detail": f"account {account_id} was never read; "
                                       "its declaration is ignored"})

    # Every listed account and every stored account is REQUIRED through the
    # last day asked for. The one exception is an account the owner has
    # declared retired, and only when the declaration is borne out by the
    # ledger: verified coverage reaching the declared day, and no stored
    # spend after it. Being absent from the configuration means nothing.
    relevant, relied = [], []          # (account_id, row|None, required_end)
    for account_id in listed + [a for a in stored if a not in listed]:
        row = stored.get(account_id)
        required_end = end_date
        day = declared.get(account_id)
        if row is not None and day is not None:
            last = (date.fromisoformat(row["covered_through"])
                    if row.get("covered_through") else None)
            spent = (date.fromisoformat(row["last_spend_date"])
                     if row.get("last_spend_date") else None)
            # "Being read": configured, and Meta has not started refusing it.
            being_read = (account_id in listed and not
                          (states.get(account_id) or {}).get("not_readable_since"))
            verdict = declaration_verdict(
                day=day, covered_through=last, last_spend=spent,
                being_read=being_read, start_date=start_date, end_date=end_date)
            if verdict["problem"] == DECLARATION_STILL_READ:
                problems.append({
                    "account_id": account_id, "reason": DECLARATION_STILL_READ,
                    "detail": f"account {account_id} is declared retired but "
                              "is configured and still read; the declaration "
                              "is ignored"})
            elif verdict["problem"] == DECLARATION_CONTRADICTED:
                problems.append({
                    "account_id": account_id, "reason": DECLARATION_CONTRADICTED,
                    "detail": f"account {account_id} is declared to have last "
                              f"spent on {day.isoformat()}, but has stored "
                              f"spend on {spent.isoformat()}"})
            elif verdict["problem"] == DECLARATION_NOT_COVERED:
                problems.append({
                    "account_id": account_id, "reason": DECLARATION_NOT_COVERED,
                    "detail": f"account {account_id} is declared to have last "
                              f"spent on {day.isoformat()}, but its verified "
                              "coverage ends "
                              + (last.isoformat() if last else "nowhere")
                              + "; the days in between were never read"})
            elif verdict["honoured"]:
                relied.append({"account_id": account_id,
                               "last_spend_day": day.isoformat(),
                               "relied_from": verdict["relied_from"].isoformat()})
                if verdict["required_end"] is None:
                    continue           # no day of this range is the account's
                required_end = verdict["required_end"]
        relevant.append((account_id, row, required_end))
    if not relevant and relied:
        # Every account is validly declared retired before this range: the
        # zero rests entirely on the declarations, and says so.
        return finish(derive_meta_ads_spend_truth({
            "connected": True, "available": True, "coverage_complete": True,
            "native_total": 0.0, "usd_total": 0.0, "fx_status": "verified"}),
            [], relied)
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
                 "configured": account_id in listed,
                 "syncing": account_id in syncing,
                 "owner_declared_retired": any(
                     r["account_id"] == account_id for r in relied),
                 "required_through": required_end.isoformat(),
                 "covered_through": (row or {}).get("covered_through"),
                 "first_verified_day": (row or {}).get("covered_from"),
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
        if settled and truth["state"] in PUBLISHING_STATES:
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
        short = [e for e in entries if e["state"] == STATE_COVERAGE_INCOMPLETE]
        named = {p["account_id"]: p["detail"] for p in problems}
        total["withheld_detail"] = "; ".join(
            named.get(e["account_id"])
            or (f"account {e['account_id']} has never been read"
                if e.get("never_read") else
                f"account {e['account_id']} is not verified for every day "
                f"through {e['required_through']} (verified through "
                f"{e.get('covered_through') or 'no day'})")
            for e in short)
    for entry in entries:
        entry.pop("meta_ads_spend_source", None)
        entry.pop("native_currency", None)
    return finish(total, entries, relied)
