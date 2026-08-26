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
                    unavailable_reason: str | None = None) -> dict:
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
    return block


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
