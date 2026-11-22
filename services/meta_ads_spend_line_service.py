"""PR-ADS-171B — the "Meta Ads (spend)" paid-media line.

One line, shown on the Channels page beside the "Google Ads (canonical
evidence)" line and on the Dashboard's Channels tab. It republishes what
``meta_ads_spend_truth_service.build_meta_ads_spend_truth_for_days`` — the ONLY
reader of Meta spend totals — decided for a page's days, and nothing else.

What the line is
----------------
* **Spend only.** Meta spend over the page's settled days, in USD, with the
  native total when every account shares one currency and each account's own
  figures when they do not. No HubSpot count sits on the line and no rate is
  derived from it: HubSpot's Paid Social contacts are not yet proven to be the
  population this spend paid for (Meta's native lead forms create the
  contacts, and a campaign renamed in Meta keeps its old name in HubSpot). The
  line carries that as ``comparison`` —
  ``incompatible_rate(channel_population_is_not_meta_spend_population)`` —
  the codebase's convention for a rate across two populations.
* **Withheld means absent.** Unless the truth publishes the total, no spend
  figure is on the line at all — not the total, not any account's. The word,
  the reason and the truth's own explanation (``withheld_detail``) are.
* **The owner's retirement declaration is disclosed wherever the figure is.**
  When the truth's ``owner_declared_retired`` is non-empty the line carries
  ``owner_declared_retired_disclosure`` beside the figure; a marker without
  its disclosure withholds the figure (``meta_retired_disclosure_missing``).

Days
----
The page's days are calendar dates. The truth reads each ad account on ITS
OWN calendar (``meta_ads_account.time_zone_name``) and settles each in its own
zone — the PR-ADS-171A rule, applied here unchanged: the line passes the
page's settled dates (``start_date`` … ``end_date``) with ``settled=True`` and
adds no date rule of its own. So a date is a different 24 hours in each
account, and none of them need be the page's own day zone; the line says so
(``day_basis_disclosure``). That is a measurement of what each account spent
on those dates, and it is not compared with anything counted on the page's
days, so the offset changes no figure the page computes. An account whose
zone is unknown settles nothing, so the total is withheld.

Read-only. SELECTs on the local database through the truth function. No call
to Meta.
"""
from __future__ import annotations

import logging

from analysis import closed_won_truth as cwt

log = logging.getLogger(__name__)

LINE_SOURCE = "__meta_ads_spend__"
LINE_LABEL = "Meta Ads (spend)"
LINE_RULE_VERSION = "meta_ads_spend_line_v1"
METRIC_FAMILY = "meta_ads_spend"
WINDOW_BASIS = "meta_account_local_spend_date"

#: Why the line's spend is not a number.
R_NOT_CONNECTED = "meta_ads_not_connected"
R_SOURCE_UNAVAILABLE = "meta_ads_spend_source_unavailable"
R_COVERAGE_INCOMPLETE = "meta_ads_spend_coverage_incomplete"
R_FX_MISSING = "fx_rates_missing"
R_FX_UNSUPPORTED = "fx_currency_not_supported"
R_MIXED_CURRENCIES = "meta_accounts_in_different_currencies"
R_DISCLOSURE_MISSING = "meta_retired_disclosure_missing"
R_LINE_FAILED = "meta_ads_spend_line_failed"
#: Every reason the line can give, so the UI and the docs list the same set.
REASONS = (R_NOT_CONNECTED, R_SOURCE_UNAVAILABLE, R_COVERAGE_INCOMPLETE,
           R_FX_MISSING, R_FX_UNSUPPORTED, R_MIXED_CURRENCIES,
           R_DISCLOSURE_MISSING, R_LINE_FAILED,
           cwt.R_CHANNEL_POPULATION_NOT_META_SPEND)

_PUBLISHING = ("verified", "verified_zero_spend")

NOTE = ("Meta Ads spend is read from Meta's own ad accounts and shown on its own "
        "line. It is not matched to HubSpot's Paid Social contacts, SQLs, deals "
        "or customers: HubSpot's Paid Social channel also holds LinkedIn, and "
        "HubSpot keeps the campaign name a contact arrived under after the "
        "campaign is renamed in Meta, so the two are not yet proven to be the "
        "same population. No rate is calculated between them.")


def _disclose_days(accounts: list, page_zone: str | None) -> str | None:
    zones = [f"{a['account_id']} on {a['time_zone']}" if a.get("time_zone")
             else f"{a['account_id']} on an unknown time zone"
             for a in accounts]
    if not zones:
        return None
    text = ("Each ad account is read on its own calendar days: "
            + "; ".join(zones) + ".")
    if len({a.get("time_zone") for a in accounts}) > 1:
        text += " The same date is a different 24 hours in each account."
    if page_zone and any(a.get("time_zone") != page_zone for a in accounts):
        text += (f" These are not the {page_zone} days the rest of this page "
                 "counts on; nothing on the page is divided by this spend.")
    return text


def _metric(value, status, reason=None, **extra):
    return cwt.metric(value, status, reason, **extra)


def line_from_truth(truth: dict | None, *, start_date=None, end_date=None,
                    page_zone: str | None = None,
                    empty_reason: str | None = None) -> dict:
    """Pure. The line for one truth result (or for no settled day at all,
    ``empty_reason``). See the module docstring for what it publishes."""
    t = truth or {}
    state = t.get("state") if not empty_reason else None
    relied = list(t.get("owner_declared_retired") or []) if state in (
        *_PUBLISHING, "fx_incomplete") else []
    disclosure = t.get("owner_declared_retired_disclosure") if relied else None

    usd_value = t.get("usd_spend")
    if empty_reason:
        usd = _metric(None, cwt.UNAVAILABLE, empty_reason)
    elif state in _PUBLISHING and usd_value is not None:
        usd = _metric(round(float(usd_value), 2), cwt.PUBLISHED)
    elif state == "fx_incomplete":
        usd = _metric(None, cwt.WITHHELD, t.get("fx_reason") or R_FX_MISSING)
    elif state == "coverage_incomplete":
        usd = _metric(None, cwt.WITHHELD, R_COVERAGE_INCOMPLETE)
    elif state == "not_connected":
        usd = _metric(None, cwt.UNAVAILABLE, R_NOT_CONNECTED)
    else:
        # source_unavailable, a state this line does not know, a published
        # state without its figure: not evidence of a total.
        usd = _metric(None, cwt.UNAVAILABLE, R_SOURCE_UNAVAILABLE)
    if relied and not disclosure:
        # The figure rests on the owner's declaration and the words that say
        # so are missing: the figure is not shown without them.
        usd = _metric(None, cwt.WITHHELD, R_DISCLOSURE_MISSING)
    published = usd["status"] == cwt.PUBLISHED

    native_value = t.get("native_spend")
    currency = t.get("native_currency")
    native_ok = (not empty_reason and not (relied and not disclosure)
                 and state in (*_PUBLISHING, "fx_incomplete")
                 and native_value is not None and currency)
    if native_ok:
        native = _metric(round(float(native_value), 2), cwt.PUBLISHED,
                         currency=currency)
    elif published and t.get("mixed_currencies"):
        native = _metric(None, cwt.UNAVAILABLE, R_MIXED_CURRENCIES, currency=None)
    else:
        native = _metric(None, usd["status"] if not published else cwt.UNAVAILABLE,
                         usd["reason"] or R_SOURCE_UNAVAILABLE, currency=None)

    accounts = []
    for a in ([] if empty_reason else t.get("accounts") or []):
        # An account's figure is shown only when the TOTAL is published: one
        # account's spend beside a withheld total is a partial, not the total.
        a_native = a.get("native_spend")
        a_usd = a.get("usd_spend")
        accounts.append({
            "account_id": a.get("account_id"),
            "account_name": a.get("account_name"),
            "currency": a.get("currency"),
            "time_zone": a.get("time_zone"),
            "state": a.get("state"),
            "verified_through": a.get("covered_through"),
            "required_through": a.get("required_through"),
            "first_verified_day": a.get("first_verified_day"),
            "configured": a.get("configured"),
            "owner_declared_retired": bool(a.get("owner_declared_retired")),
            "not_readable_since": a.get("not_readable_since"),
            "never_read": bool(a.get("never_read")),
            "native_spend": (_metric(round(float(a_native), 2), cwt.PUBLISHED,
                                     currency=a.get("currency"))
                             if published and a_native is not None
                             else _metric(None, usd["status"] if not published
                                          else cwt.UNAVAILABLE,
                                          usd["reason"] or R_SOURCE_UNAVAILABLE,
                                          currency=a.get("currency"))),
            "usd_spend": (_metric(round(float(a_usd), 2), cwt.PUBLISHED)
                          if published and a_usd is not None
                          else _metric(None, usd["status"] if not published
                                       else cwt.UNAVAILABLE,
                                       usd["reason"] or R_SOURCE_UNAVAILABLE)),
        })

    # False and None are different claims: "Meta is not set up" vs "we could
    # not tell" (an unreadable store, a broken configuration, no day read).
    if state == "not_connected":
        connected = False
    elif state in (None, "source_unavailable"):
        connected = None
    else:
        connected = True
    return {
        "source": LINE_SOURCE,
        "label": LINE_LABEL,
        "platform": "meta",
        "rule_version": LINE_RULE_VERSION,
        "metric_family": METRIC_FAMILY,
        "window_basis": WINDOW_BASIS,
        "in_channel_partition": False,
        "added_to_totals": False,
        # Spend and nothing to divide it by: no HubSpot count is on this line.
        "compared_with_outcomes": False,
        "comparison": cwt.incompatible_rate(cwt.R_CHANNEL_POPULATION_NOT_META_SPEND),
        "state": state,
        "connected": connected,
        "spend": {**usd, "currency": "USD", "source": "meta_ads"},
        "native_spend": native,
        "accounts": accounts,
        "time_zones": [] if empty_reason else list(t.get("time_zones") or []),
        "mixed_time_zones": bool(t.get("mixed_time_zones")) and not empty_reason,
        "mixed_currencies": bool(t.get("mixed_currencies")) and not empty_reason,
        "day_basis_disclosure": _disclose_days(accounts, page_zone),
        "owner_declared_retired": relied,
        "owner_declared_retired_disclosure": disclosure,
        "retired_declaration_problems": ([] if empty_reason else list(
            t.get("retired_declaration_problems") or [])),
        "withheld_detail": (None if published or empty_reason
                            else t.get("withheld_detail")),
        "unsettled_reason": (None if published or empty_reason
                             else t.get("unsettled_reason")),
        "start_date": (start_date.isoformat() if hasattr(start_date, "isoformat")
                       else start_date),
        "end_date": (end_date.isoformat() if hasattr(end_date, "isoformat")
                     else end_date),
        "settled_rule_applied": bool(t.get("settled_rule_applied")),
        "note": NOTE,
        "meta_calls_performed": False,
    }


def build_meta_ads_spend_line(start_date, end_date, *, now=None,
                              page_zone: str | None = None,
                              empty_reason: str | None = None) -> dict:
    """The line over the page's settled dates ``[start_date, end_date]``
    (``start_date`` None for all time). ``empty_reason``: the page has no
    settled day, so no truth is read and nothing is published."""
    if empty_reason:
        return line_from_truth(None, start_date=start_date, end_date=end_date,
                               page_zone=page_zone, empty_reason=empty_reason)
    from services.meta_ads_spend_truth_service import (  # noqa: PLC0415
        build_meta_ads_spend_truth_for_days,
    )
    try:
        truth = build_meta_ads_spend_truth_for_days(
            start_date, end_date, settled=True, now=now)
    except Exception as exc:  # noqa: BLE001 — fail closed, the page still renders
        log.error("[meta-spend-line] truth failed: %s", exc, exc_info=True)
        line = line_from_truth({"state": "source_unavailable"},
                               start_date=start_date, end_date=end_date,
                               page_zone=page_zone)
        line["spend"] = {**_metric(None, cwt.UNAVAILABLE, R_LINE_FAILED),
                         "currency": "USD", "source": "meta_ads"}
        line["native_spend"] = _metric(None, cwt.UNAVAILABLE, R_LINE_FAILED,
                                       currency=None)
        return line
    return line_from_truth(truth, start_date=start_date, end_date=end_date,
                           page_zone=page_zone)


def paid_social_spend(line: dict | None) -> tuple:
    """``(spend metric, ratio gate)`` for the HubSpot Paid Social row.

    Meta not set up: the row has no spend source, as before
    (``channel_spend_not_connected``, no ratio gate). Otherwise — whatever
    the line publishes — the row's spend cell points at the line and its
    period ratios are a rate across two populations."""
    if line is None or line.get("connected") is False:
        return cwt.metric(None, cwt.UNAVAILABLE, cwt.R_CHANNEL_SPEND_NOT_CONNECTED), None
    return (cwt.metric(None, cwt.UNAVAILABLE, cwt.R_SPEND_ON_META_ADS_LINE),
            cwt.incompatible_rate(cwt.R_CHANNEL_POPULATION_NOT_META_SPEND))
