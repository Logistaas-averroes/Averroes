"""
Dashboard → Deals (PR-ADS-139; one builder since PR-ADS-178B, ``docs/61``).

The tab republishes the Pipeline builder's figures for the window —
``services.pipeline_service.build_pipeline`` — and adds one thing the Pipeline
page does not show: the previous-period comparison, built by the SAME builder
over the previous window. Nothing here computes a figure (the 177B precedent:
Dashboard → Campaigns reads the Campaigns page's view).

What PR-ADS-178B deleted with the composition that produced it:
  * the second, third and fourth reads of the same metrics — the mart deal
    summary beside ``load_won_deals`` / ``summarize_deals`` (three closed-won
    counts over one ledger), the page-side ``amount_unknown`` revenue check
    beside the reader contract, ``build_revenue_by_source``'s source groups;
  * the through-today campaign view's "Campaign → Pipeline" breakdown (the
    Campaigns page owns campaigns);
  * the legacy "Qualified (lead status) · no won deal" backlog
    (``fetch_sql_lead_details``, ``_build_sql_no_deal``) and the source
    breakdown's lead-status column (owner decision 4, 2026-11-25);
  * the always-Unavailable opportunity / open-pipeline / aging / rate cards
    and the decision cards — the ledger holds closed-won deals only, which
    the truth footer says once.

Read-only. NO writes to HubSpot, Google Ads, budgets, bids, campaigns,
keywords, negatives, or offline conversions.
"""

from __future__ import annotations

import logging
from datetime import datetime

from services import canonical_revenue_service as _canonical_snapshot  # PR-ADS-161E: one snapshot per page
from services.canonical_contract import resolve_canonical_window
from services.dashboard_overview_service import (
    _coerce_utc,
    _delta_metric,
    _previous_window_reference,
)
from services.pipeline_service import build_pipeline

logger = logging.getLogger(__name__)

#: The KPI keys a period comparison is computed for, and the pipeline KPI
#: each reads. Average deal value and SQLs carry no delta of their own here
#: (the SQL card's chip is gated client-side on a completed window).
_DELTA_KPIS = (("sqls", "sqls"),
               ("closed_won_deals", "closed_won_deals"),
               ("closed_won_customers", "customers"),
               ("closed_won_revenue_usd", "won_revenue_usd"))


def _value(kpis: dict, key: str):
    m = (kpis or {}).get(key) or {}
    return m.get("value") if m.get("status") == "published" else None


def _build_period_change(window: str, now: datetime, kpis: dict) -> dict:
    """Deltas against the previous window, read from the SAME builder."""
    ref = _previous_window_reference(window, now)
    if ref is None:
        return {"available": False, "label": None, "note": None, "metrics": {},
                "reason": "All Time has no previous period to compare against."}
    prev_key, prev_now, label, note = ref
    try:
        prev = build_pipeline(prev_key, now=prev_now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dashboard deals previous-period build failed: %s", exc)
        return {"available": False, "label": label, "note": note, "metrics": {},
                "reason": "Previous period unavailable."}
    prev_kpis = prev.get("kpis") or {}
    metrics = {key: _delta_metric(_value(kpis, src), _value(prev_kpis, src))
               for key, src in _DELTA_KPIS}
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label, "note": note, "metrics": metrics, "reason": None,
    }


@_canonical_snapshot.single_snapshot
def build_dashboard_deals(window: str = "current_quarter",
                          now: datetime | None = None) -> dict:
    """The Dashboard → Deals contract: the Pipeline builder's payload for the
    window, plus ``period_change`` (the same builder, previous window).

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolve_canonical_window(window, now=now)
    payload = build_pipeline(window, now=now)
    return {
        **payload,
        "surface": "dashboard_deals",
        "period_change": _build_period_change(window, _coerce_utc(now),
                                              payload.get("kpis") or {}),
    }
