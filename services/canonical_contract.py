"""
services/canonical_contract.py

PR-ADS-154C — the ONE window anchor and the ONE truth-contract block that every
production metric response carries.

Why this exists
---------------
Two pages could disagree about the same metric for two reasons, and both were
live:

**1. They asked about different date ranges.** Every consumer already called
``analysis.business_windows.resolve_window``, so the resolver was shared — but
the reference instant was not. Dashboards anchored on UTC, spend and geo on the
Google Ads account day read from the database (falling back to UTC when absent),
and the evidence services on a hardcoded ``Europe/London``. At 23:30 UTC on
30 June under BST, ``current_quarter`` is Q2 under one anchor and Q3 under
another. Every named window diverges at that instant.

**2. Nothing in the response said what it was.** A payload of numbers with no
statement of which source, scope, currency and window produced them cannot be
compared with another payload except by assuming they agree — which is the
assumption this programme exists to stop making.

So this module answers both, once:

  * :func:`resolve_canonical_window` — the account-day anchor, used everywhere.
  * :func:`truth_contract` — the metadata block published beside the numbers.

What ``fallback_used`` means
----------------------------
``False`` is a claim, not a default: the figures come from the canonical source
named in ``data_source``, and no legacy provider, cached legacy payload or
page-local recomputation contributed. A consumer that cannot make that claim
must publish ``fallback_used: True`` — or, better, fail closed with
:func:`unavailable_contract` and no numbers at all. Silently returning legacy
figures under a canonical label is the specific failure this forbids.

Read-only. This module computes and describes; it never writes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from analysis.business_windows import is_valid_window, resolve_window_in_zone

log = logging.getLogger(__name__)

#: Reporting currency for canonical business metrics. HubSpot revenue is USD, so
#: the ROAS denominator is converted per spend_date through the canonical FX
#: layer rather than at one spot rate.
REPORTING_CURRENCY = "USD"

#: Canonical source identifiers. A response names the authority it read, so a
#: reader can check the claim instead of inferring it from the endpoint's name.
SOURCE_CANONICAL_SPEND = "google_ads_api.canonical_campaign_spend"
SOURCE_CANONICAL_GEO = "google_ads_api.canonical_geo_spend"
SOURCE_CANONICAL_FUNNEL = "hubspot.canonical_contact_funnel"
SOURCE_CANONICAL_DEAL_LEDGER = "hubspot.canonical_deal_ledger"
SOURCE_REVENUE_DECISION_MART = "canonical.revenue_decision_mart"
SOURCE_REVENUE_BY_SOURCE = "canonical.revenue_by_source"

#: PR-ADS-168B review round 1. The label on every Dashboard link to Channels
#: from a business-window figure. Channels uses evidence windows (ending
#: yesterday) grouped by HubSpot original source; the Dashboard source figures
#: are business windows grouped by source group. The link must not promise
#: that Channels reproduces the figure.
CHANNELS_LINK_LABEL = "Open Channels (different windows and grouping)"
SOURCE_FX = "canonical.fx_rates"

CANONICAL_SOURCES = frozenset({
    SOURCE_CANONICAL_SPEND, SOURCE_CANONICAL_GEO, SOURCE_CANONICAL_FUNNEL,
    SOURCE_CANONICAL_DEAL_LEDGER, SOURCE_REVENUE_DECISION_MART,
    SOURCE_REVENUE_BY_SOURCE, SOURCE_FX,
})

#: Truth states a production metric may be published under.
TRUTH_READY = "ready"
TRUTH_NOT_READY = "not_ready"
TRUTH_UNAVAILABLE = "unavailable"


def configured_account_time_zone() -> str | None:
    """The Google Ads account time zone on record, or None.

    Isolated here so every caller reads it the same way and tests patch one
    seam. A failure to read it is not an error: the resolver falls back to the
    account default rather than to UTC.
    """
    try:
        from db import revenue_repository as repo  # noqa: PLC0415
        return repo.fetch_account_time_zone()
    except Exception as exc:  # noqa: BLE001
        log.warning("[canonical-contract] account time zone unreadable: %s", exc)
        return None


def resolve_canonical_window(window: str, now: datetime | None = None,
                             account_time_zone: str | None = None) -> dict:
    """THE window resolver for production business metrics.

    Anchored on the Google Ads account calendar day, because spend is
    denominated in it. Callers already holding the configured zone pass it;
    otherwise it is read once here.

    Returns the resolved window dict plus ``timezone``.
    """
    if not is_valid_window(window):
        raise ValueError(f"Invalid window '{window}'")
    zone = account_time_zone if account_time_zone is not None else \
        configured_account_time_zone()
    return resolve_window_in_zone(window, zone, now=now)


# ── Settled-day windows (PR-ADS-167A, owner decision 2026-11-20) ─────────────
#
# Country Revenue & Spend, its mart country view, Dashboard Countries and the
# geo reconciliation panel divide closed-won revenue by country spend. Country
# spend for a day is complete only once the day has ended in the account time
# zone and the morning sync has re-read it (the settle rule in
# ``google_ads_spend_service``), so a window that includes today can never hold
# complete country spend. The owner's decision: for THESE consumers only, a
# window that includes today ENDS AT YESTERDAY (account-local). Its deals are
# read over the same ACCOUNT-LOCAL days as its settled spend
# (:func:`settled_revenue_bounds`). Closed windows (Last Quarter) are unchanged
# — their deals keep the business windows' UTC-midnight bounds. Every other revenue page keeps its window, so these pages may
# differ from them by today's deals; the window block says so.

#: ``through_reason`` of a window that was ended at yesterday because today is
#: not settled. The only value; ``None`` means the window is unchanged.
THROUGH_TODAY_NOT_SETTLED = "today_not_settled"


def _fmt_day(d) -> str:
    """``19 Nov 2026`` — the page's wording, independent of the locale."""
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Oct", "Nov", "Dec")
    return f"{d.day} {months[d.month - 1]} {d.year}"


def end_at_settled_day(resolved: dict) -> dict:
    """``resolved`` (a business window resolved on the account day) ended at
    the last settled day: yesterday for an open window, unchanged when closed.

    Pure. The start never moves — an open window is the same window, minus
    today. ``window_range`` states what was measured: ``start``, ``through``
    (inclusive), ``through_reason`` and the disclosure the page shows. On the
    first day of a quarter or year the open window has no settled day yet:
    ``through`` is the day before ``start`` and ``empty`` is True; every
    consumer then publishes nothing (``NO_SETTLED_DAY_IN_WINDOW``).
    """
    from datetime import date, timedelta  # noqa: PLC0415

    out = dict(resolved)
    start = out.get("start_date")
    if out.get("is_closed_window"):
        out["through_reason"] = None
        out["window_range"] = {"start": start, "through": out.get("end_date"),
                               "through_reason": None, "empty": False,
                               "disclosure": None}
        return out
    today = date.fromisoformat(str(out["end_date"]))
    through = today - timedelta(days=1)
    empty = bool(start) and through < date.fromisoformat(str(start))
    disclosure = (
        (f"No completed day in this window yet — today ({_fmt_day(today)}) is "
         "not included until tomorrow morning's sync.") if empty else
        (f"Through {_fmt_day(through)} — today is not included until tomorrow "
         "morning's sync."))
    disclosure += (" Other revenue pages, such as Deals, already count deals "
                   "closed today.")
    out.update({
        "end_date": through.isoformat(),
        "account_today": today.isoformat(),
        "through_reason": THROUGH_TODAY_NOT_SETTLED,
        "window_range": {"start": start, "through": through.isoformat(),
                         "through_reason": THROUGH_TODAY_NOT_SETTLED,
                         "empty": empty, "disclosure": disclosure},
    })
    return out


def resolve_settled_window(window: str, now: datetime | None = None,
                           account_time_zone: str | None = None) -> dict:
    """THE window of the settled-day consumers: :func:`resolve_canonical_window`
    ended at yesterday when it includes today (:func:`end_at_settled_day`)."""
    return end_at_settled_day(resolve_canonical_window(
        window, now=now, account_time_zone=account_time_zone))


#: Every metric of a settled-day consumer over an EMPTY settled window (the
#: first day of a quarter or year: no day has ended yet) is unavailable with
#: this reason — never a "verified" zero over no day at all.
NO_SETTLED_DAY_IN_WINDOW = "no_settled_day_in_window"


def _account_midnight_utc(day, zone_name) -> datetime:
    """Midnight starting ``day`` in the account zone, as a UTC instant."""
    from datetime import date  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from analysis.account_time import ACCOUNT_TZ  # noqa: PLC0415

    d = date.fromisoformat(str(day))
    try:
        tz = ZoneInfo(zone_name or ACCOUNT_TZ)
    except Exception:  # noqa: BLE001 — unknown zone: the account default
        tz = ZoneInfo(ACCOUNT_TZ)
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)


def settled_revenue_bounds(resolved: dict) -> tuple | None:
    """THE revenue bounds of a settled-day window: ``(start, end)`` as UTC
    instants, half-open, on the ACCOUNT calendar — the same days the settled
    spend covers.

    ``start`` is account-local midnight of ``start_date`` (None for All Time);
    ``end`` (exclusive) is account-local midnight of ``account_today``, i.e.
    the end of yesterday. On an empty window ``end == start``. None when the
    window was not cut (closed windows keep the business windows' own bounds).
    Every settled consumer and both audits read deals through this one
    function (``load_won_deals(..., bounds=...)``), so they compare the same
    instants.
    """
    if not resolved.get("through_reason"):
        return None
    zone = resolved.get("timezone")
    start = (_account_midnight_utc(resolved["start_date"], zone)
             if resolved.get("start_date") else None)
    end = _account_midnight_utc(resolved["account_today"], zone)
    if (resolved.get("window_range") or {}).get("empty") and start is not None:
        end = start
    return start, end


def resolve_settled_evidence_window(window_key: str,
                                    now: datetime | None = None) -> dict:
    """An EVIDENCE window (``30d`` … ``all_time``) ended at its last settled
    day — PR-ADS-168A, the Channels page's outcome and spend half.

    Evidence windows are N account-local days ending TODAY
    (``analysis.account_time``), so every one of them is open. The owner's
    PR-ADS-167A rule for revenue over spend applies unchanged
    (:func:`end_at_settled_day`): the start never moves, the window ends
    YESTERDAY, and ``window_range`` carries the disclosure. The result also
    feeds :func:`settled_revenue_bounds` (it carries ``timezone`` and
    ``account_today``), so the deals are read over exactly the days the spend
    covers. Raises ``EvidenceWindowError`` for an unknown key.
    """
    from datetime import timedelta  # noqa: PLC0415

    from analysis.account_time import ACCOUNT_TZ, account_today  # noqa: PLC0415
    from analysis.evidence_windows import resolve_evidence_window  # noqa: PLC0415

    resolved = resolve_evidence_window(window_key)
    today = account_today(now)
    days = resolved["days"]
    start = None if days is None else today - timedelta(days=days - 1)
    out = end_at_settled_day({
        "window_type": "evidence", "window_key": resolved["key"],
        "key": resolved["key"], "label": resolved["key"],
        "start_date": start.isoformat() if start else None,
        "end_date": today.isoformat(), "is_closed_window": False,
        "is_all_time": days is None, "timezone": ACCOUNT_TZ,
    })
    return out


def truth_contract(*, data_source: str, window: str,
                   truth_status: str = TRUTH_READY,
                   now: datetime | None = None,
                   account_time_zone: str | None = None,
                   customer_id: str | None = None,
                   currency: str = REPORTING_CURRENCY,
                   fallback_used: bool = False,
                   resolved: dict | None = None,
                   notes: str | None = None) -> dict:
    """The metadata block published beside every production metric.

    Identifies the truth contract the numbers were produced under, so two
    responses can be compared on stated facts rather than on the assumption that
    equally-named endpoints mean the same thing.

    ``fallback_used=True`` is permitted but never silent: it appears in the
    payload and the cross-page audit treats it as a violation.
    """
    block = resolved or resolve_canonical_window(
        window, now=now, account_time_zone=account_time_zone)
    return {
        "data_source": data_source,
        "truth_status": truth_status,
        "window": block.get("key", window),
        "window_label": block.get("label"),
        "window_start": block.get("start_date"),
        "window_end": block.get("end_date"),
        "timezone": block.get("timezone"),
        "customer_id": customer_id,
        "currency": currency,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        # A claim, not a default — see the module docstring.
        "fallback_used": bool(fallback_used),
        "notes": notes,
    }


#: The key under which a response publishes its PER-METRIC provenance.
#: Deliberately not `truth_status`, which several dashboards already use for
#: something else at the top level.
METRIC_TRUTH_KEY = "metric_truth"


def metric_contract(*, metric: str, data_source: str, scope: str,
                    resolved: dict, truth_status: str = TRUTH_READY,
                    customer_id: str | None = None,
                    currency: str = REPORTING_CURRENCY,
                    fallback_used: bool = False,
                    unavailable_reason: str | None = None,
                    violation_codes=None) -> dict:
    """The provenance block for ONE metric within a response.

    PR-ADS-154C-F1. A single response-level ``data_source`` cannot describe a
    page that combines Google Ads spend, HubSpot revenue and funnel counts: the
    Overview publishes all three, so one source name is wrong about at least two
    of them. Worse, it made the audit's registry self-certifying — the audit
    printed the ``canonical_source`` it *expected*, which proves nothing about
    where the number came from.

    Each metric now states its own lineage, and the audit checks the statement
    against its registry rather than echoing it.

    ``resolved`` is the window this response actually used, passed in rather than
    re-resolved, so a page cannot describe a different range from the one it
    computed over.
    """
    block = {
        "metric": metric,
        "data_source": data_source,
        "scope": scope,
        "truth_status": truth_status,
        "window": resolved.get("key"),
        "window_start": resolved.get("start_date"),
        "window_end": resolved.get("end_date"),
        "timezone": resolved.get("timezone"),
        "customer_id": customer_id,
        "currency": currency,
        "fallback_used": bool(fallback_used),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if unavailable_reason:
        block["unavailable_reason"] = unavailable_reason
    # PR-ADS-154C-F3-F1 §4. A withheld metric says WHY in machine-readable form,
    # from the canonical helper that made the decision — not from a string each
    # dashboard hand-assembles, which is how six pages come to describe one
    # refusal six different ways. Normalised to a sorted, de-duplicated list so
    # two contracts describing the same refusal compare equal.
    codes = _normalize_violation_codes(violation_codes)
    if codes:
        block["violation_codes"] = codes
    return block


def _normalize_violation_codes(codes) -> list:
    """Deterministic list from anything a caller reasonably hands over.

    A bare string is one code, not five characters. Order and duplicates are
    removed so a contract cannot differ from an identical one by accident.
    """
    if codes is None:
        return []
    if isinstance(codes, str):
        codes = [codes]
    return sorted({str(c) for c in codes if c})


def metric_truth_block(resolved: dict, entries: list[dict]) -> dict:
    """Assemble ``metric_truth`` from a list of :func:`metric_contract` specs.

    Each entry is a dict of :func:`metric_contract` keyword arguments minus
    ``resolved``, which is supplied once here so every metric in a response is
    described against the same window by construction.
    """
    return {e["metric"]: metric_contract(resolved=resolved, **e) for e in entries}


def unavailable_contract(*, data_source: str, window: str, reason: str,
                         now: datetime | None = None,
                         account_time_zone: str | None = None) -> dict:
    """Fail closed: canonical truth could not be established for this window.

    Returned INSTEAD of figures, never alongside legacy ones. A page that shows
    last-known-good numbers under an unavailable contract has told the reader
    the opposite of what happened.
    """
    contract = truth_contract(
        data_source=data_source, window=window,
        truth_status=TRUTH_UNAVAILABLE, now=now,
        account_time_zone=account_time_zone, fallback_used=False)
    contract["unavailable_reason"] = reason
    return contract


def is_canonical(contract: dict) -> bool:
    """True when this block is a usable canonical READING.

    Three conditions, and the third is easy to forget: naming a canonical source
    is not the same as having got anything from it. An ``unavailable`` block
    names the authority it could not read, so a check that looked only at
    ``data_source`` and ``fallback_used`` would call a failed read canonical.
    """
    return (contract.get("data_source") in CANONICAL_SOURCES
            and contract.get("fallback_used") is False
            and contract.get("truth_status") != TRUTH_UNAVAILABLE)
