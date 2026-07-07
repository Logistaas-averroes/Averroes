"""
Dashboard Deals, Opportunities & Pipeline Intelligence (PR-ADS-139)

The sixth and final Dashboard tab (Dashboard → Deals). It answers, in one screen:
what happens AFTER an SQL — how many became closed-won customers, how much
closed-won revenue that produced, which sources / campaigns create it, and which
qualified SQLs have not yet become customers.

Durable-data reality (see PR-ADS-139 investigation):
  The durable, read-only HubSpot deal ledger stores CLOSED-WON deals ONLY. Open
  opportunities, closed-lost deals, deal create dates and any non-won deal stage
  are NOT synced into any queryable durable table (open rows carry a NULL
  deal_id / deal_close_date; ``deal_created_at`` is never populated). So this tab
  presents the closed-won half of the pipeline as TRUTH and renders open pipeline,
  closed-lost, opportunity aging, opportunity-created counts, and the two
  opportunity-conversion rates as "Unavailable" — it never invents them.

Composition doctrine — this service computes NO new business math and adds NO new
attribution model. It composes canonical read-only contracts:
  - build_revenue_decision_mart(view="deal")    -> canonical SQLs / customers /
      closed-won revenue + readiness + the closed-won deal ledger rows + the
      ledger availability signal.
  - build_revenue_decision_mart(view="campaign") -> per-campaign SQLs / customers /
      closed-won revenue for the campaign → pipeline panel.
  - build_revenue_by_source                     -> per source-group SQLs /
      customers / closed-won revenue for the source → pipeline panel.
  - fetch_sql_lead_details                       -> per-SQL rows + whether that
      contact has a closed-won deal, for the "SQLs not yet closed-won" panel.

Truth rules (unchanged, never loosened):
  - HubSpot closed-won is revenue truth (USD). Open pipeline is NOT revenue; a
    closed-lost deal is NOT negative revenue; an opportunity is NOT a customer.
  - No ROAS on this tab (spend / ROAS live on the spend pages).
  - No fake $0 / 0.00x / 0%. A missing amount stays null; a rate whose denominator
    is null / zero / unsafe is null (Unavailable); a missing prior period yields
    "No comparison".
  - Open opportunity / closed-lost / aging / deal-create-date data does not exist
    durably → rendered "Unavailable", never invented.
  - Read-only. NO writes to HubSpot, Google Ads, budgets, bids, campaigns,
    keywords, negatives, or offline conversions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from analysis.business_windows import resolve_window
from analysis.source_classification import GROUP_LABELS
from db import revenue_repository as repo
from services.revenue_decision_mart import build_revenue_decision_mart
from services.source_attribution_service import build_revenue_by_source

# Reuse the Overview tab's pure window/delta helpers (PR-ADS-134/135/136/137/138).
from services.dashboard_overview_service import (
    _coerce_utc,
    _delta_metric,
    _parse_iso_date,
    _previous_window_reference,
    _round2,
)

logger = logging.getLogger(__name__)


# ── Business-facing status copy ──────────────────────────────────────────────
STATUS_REVENUE_PROVEN = "Revenue proven"
STATUS_SQL_NOT_CUSTOMER = "SQL — not yet a customer"
STATUS_SQL_AGING = "Aging SQL — needs sales review"
STATUS_AMOUNT_UNAVAILABLE = "Amount unavailable"
STATUS_ATTR_REVIEW = "Attribution needs review"
STATUS_STAGE_UNAVAILABLE = "Stage unavailable"
STATUS_SQL_PRODUCER = "SQL producer"
STATUS_NO_ACTIVITY = "No activity in window"

# The single, precise reason every open-pipeline / lost / aging field carries.
OPEN_PIPELINE_REASON = (
    "Open opportunity stage data is unavailable from the durable HubSpot deal ledger."
)
STAGE_REASON = (
    "Only closed-won deals are stored durably; open and closed-lost stages are not synced."
)

UNATTRIBUTED_LABEL = "Unattributed / Needs Review"

# SQL aging thresholds (days since the SQL business event date).
_SQL_AGING_DAYS = 30
_SQL_LIST_LIMIT = 40


def _rate(numerator, denominator):
    """Conversion rate as a fraction (0..1), or None when unsafe.

    None when the denominator is null / zero / negative or the numerator is null —
    never a fabricated 0% that would read as a real, measured conversion.
    """
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 4)


def _norm(label) -> str:
    if not label:
        return ""
    return " ".join(str(label).replace("_", " ").strip().lower().split())


# ── Closed-won deal rows (the durable proof) ─────────────────────────────────

def _deal_row(deal: dict) -> dict:
    """One closed-won deal for the deal table / drawer. Open-pipeline-only fields
    (created_date, age, days_to_close, source) are Unavailable — the durable ledger
    does not carry them. A missing amount stays None (never a fake $0)."""
    amount = deal.get("deal_amount_usd")
    amount_usd = _round2(amount) if amount is not None else None
    status = STATUS_REVENUE_PROVEN if amount_usd is not None else STATUS_AMOUNT_UNAVAILABLE
    match = _norm(deal.get("match_status"))
    attribution_status = ("attributed" if match not in ("", "unknown", "unmatched", "none")
                          else "needs_review")
    return {
        "deal_id": deal.get("deal_id"),
        "deal_name": None,          # deal title is not persisted → Unavailable
        "company": deal.get("company"),
        "company_id": None,         # not persisted → Unavailable
        "main_contact": None,       # not persisted → Unavailable
        "contact_id": None,         # not on the closed-won ledger fetch → Unavailable
        "amount_usd": amount_usd,
        "stage": deal.get("deal_stage_label"),
        "stage_group": "Closed Won",
        "status": status,
        "created_date": None,       # deal_created_at is never populated → Unavailable
        "close_date": deal.get("deal_close_date"),
        "age_days": None,           # requires a create date → Unavailable
        "days_to_close": None,      # requires a create date → Unavailable
        "source_group": None,       # deal rows carry no source group → Unavailable
        "source_detail": None,
        "campaign_name": deal.get("campaign_name"),
        "country": deal.get("country"),
        "attribution_status": attribution_status,
    }


def _build_deals(mart_rows: list, revenue_connected: bool, ledger_available: bool) -> list:
    if not revenue_connected or not ledger_available:
        return []
    return [_deal_row(d) for d in (mart_rows or [])]


# ── KPIs ─────────────────────────────────────────────────────────────────────

def _build_kpis(summary: dict, revenue_connected: bool, sqls_not_closed_won) -> dict:
    sqls = summary.get("sqls")
    customers = summary.get("customers") if revenue_connected else None
    won = _round2(summary.get("won_revenue_usd")) if revenue_connected else None
    return {
        "sqls": sqls,
        # Open pipeline / opportunity data is not durable → Unavailable, never faked.
        "opportunities_created": None,
        "open_opportunities": None,
        "open_pipeline_usd": None,
        "closed_won_customers": customers,
        "closed_won_revenue_usd": won,
        "closed_lost_deals": None,
        "lost_pipeline_usd": None,
        "sql_to_opportunity_rate": None,
        "opportunity_to_customer_rate": None,
        "avg_days_to_close": None,
        "stalled_opportunities": None,
        # Honest, durable extras: the full-funnel conversion we CAN prove, and the
        # count of qualified SQLs with no closed-won deal yet.
        "sql_to_customer_rate": _rate(customers, sqls),
        "sqls_not_closed_won": sqls_not_closed_won,
    }


# ── Pipeline funnel (SQL → closed-won; unavailable middle stages are honest) ──

def _build_funnel(kpis: dict) -> list:
    sqls = kpis.get("sqls")
    customers = kpis.get("closed_won_customers")
    won = kpis.get("closed_won_revenue_usd")
    return [
        {"stage": "SQLs", "count": sqls, "amount_usd": None,
         "rate_from_previous": None,
         "status": STATUS_SQL_PRODUCER if (sqls or 0) > 0 else STATUS_NO_ACTIVITY},
        {"stage": "Opportunities Created", "count": None, "amount_usd": None,
         "rate_from_previous": None, "status": STATUS_STAGE_UNAVAILABLE},
        {"stage": "Open Pipeline", "count": None, "amount_usd": None,
         "rate_from_previous": None, "status": STATUS_STAGE_UNAVAILABLE},
        {"stage": "Closed Won", "count": customers, "amount_usd": won,
         "rate_from_previous": _rate(customers, sqls),
         "status": STATUS_REVENUE_PROVEN if (customers or 0) > 0 else STATUS_NO_ACTIVITY},
        {"stage": "Closed Lost", "count": None, "amount_usd": None,
         "rate_from_previous": None, "status": STATUS_STAGE_UNAVAILABLE},
        {"stage": "SQLs Not Yet Closed-Won", "count": kpis.get("sqls_not_closed_won"),
         "amount_usd": None, "rate_from_previous": None,
         "status": STATUS_SQL_NOT_CUSTOMER if (kpis.get("sqls_not_closed_won") or 0) > 0
         else STATUS_NO_ACTIVITY},
    ]


def _build_stage_mix(kpis: dict) -> list:
    """Only the closed-won stage is durably known; open/lost stages are Unavailable."""
    return [
        {"stage": "Closed Won", "count": kpis.get("closed_won_customers"),
         "amount_usd": kpis.get("closed_won_revenue_usd"),
         "avg_age_days": None, "oldest_age_days": None,
         "status": STATUS_REVENUE_PROVEN},
        {"stage": "Open / Lost", "count": None, "amount_usd": None,
         "avg_age_days": None, "oldest_age_days": None,
         "status": STATUS_STAGE_UNAVAILABLE},
    ]


def _build_aging_buckets() -> list:
    """Opportunity aging requires open deals + create dates — neither is durable."""
    return [
        {"bucket": b, "open_opportunities": None, "open_pipeline_usd": None,
         "status": STATUS_STAGE_UNAVAILABLE}
        for b in ("0-14 days", "15-30 days", "31-60 days", "60+ days")
    ]


# ── Source → pipeline (real closed-won; open pipeline Unavailable) ────────────

def _source_status(sqls, customers, won) -> str:
    if (customers or 0) > 0 and (won or 0) > 0:
        return STATUS_REVENUE_PROVEN
    if (customers or 0) > 0:
        return STATUS_REVENUE_PROVEN
    if (sqls or 0) > 0:
        return STATUS_SQL_PRODUCER
    return STATUS_NO_ACTIVITY


def _build_source_breakdown(by_source: dict, revenue_connected: bool) -> list:
    out = []
    for g in (by_source.get("groups") or []):
        sqls = g.get("sqls")
        customers = g.get("customers") if revenue_connected else None
        won = _round2(g.get("won_revenue")) if revenue_connected else None
        out.append({
            "source_group": g.get("group"),
            "source_detail": g.get("label") or GROUP_LABELS.get(g.get("group"), "—"),
            "sqls": sqls,
            "opportunities": None,           # not durable → Unavailable
            "open_pipeline_usd": None,       # not durable → Unavailable
            "closed_won_customers": customers,
            "closed_won_revenue_usd": won,
            "sql_to_opportunity_rate": None,
            "opportunity_to_customer_rate": None,
            "sql_to_customer_rate": _rate(customers, sqls),
            "status": _source_status(sqls, customers, won),
        })
    out.sort(key=lambda r: (
        r.get("closed_won_revenue_usd") or 0.0,
        r.get("closed_won_customers") or 0,
        r.get("sqls") or 0,
    ), reverse=True)
    return out


def _build_campaign_breakdown(mart_campaign_rows: list, revenue_connected: bool) -> list:
    out = []
    for m in (mart_campaign_rows or []):
        name = m.get("campaign_name")
        if not name:
            continue
        sqls = m.get("sqls")
        customers = m.get("customers") if revenue_connected else None
        won = _round2(m.get("won_revenue")) if revenue_connected else None
        # Skip pure-noise rows with no SQLs and no customers.
        if (sqls or 0) == 0 and (customers or 0) == 0 and (won or 0) == 0:
            continue
        out.append({
            "campaign_name": name,
            "sqls": sqls,
            "opportunities": None,
            "open_pipeline_usd": None,
            "closed_won_customers": customers,
            "closed_won_revenue_usd": won,
            "sql_to_customer_rate": _rate(customers, sqls),
            "status": _source_status(sqls, customers, won),
        })
    out.sort(key=lambda r: (
        r.get("closed_won_revenue_usd") or 0.0,
        r.get("closed_won_customers") or 0,
        r.get("sqls") or 0,
    ), reverse=True)
    return out


# ── SQLs not yet closed-won (per-contact, honestly labelled) ──────────────────

def _days_since(ref_now: datetime, sql_date) -> int | None:
    d = _parse_iso_date(sql_date if isinstance(sql_date, str) else (sql_date.isoformat() if sql_date else None))
    if d is None or ref_now is None:
        return None
    delta = ref_now.date() - d
    return delta.days if delta.days >= 0 else 0


def _sql_no_deal_status(days, has_campaign) -> str:
    if not has_campaign:
        return STATUS_ATTR_REVIEW
    if days is not None and days >= _SQL_AGING_DAYS:
        return STATUS_SQL_AGING
    return STATUS_SQL_NOT_CUSTOMER


def _build_sql_no_deal(sql_result: dict, ref_now: datetime, revenue_connected: bool) -> tuple[list, int, bool]:
    """Per-SQL rows whose contact has NO closed-won deal yet. Returns (rows, total,
    available). Deliberately "not yet closed-won" — open / lost pipeline is not
    visible, so an SQL is never claimed to have "no deal"."""
    if not sql_result.get("available"):
        return [], 0, False
    pending = [r for r in (sql_result.get("rows") or []) if not r.get("has_closed_won_deal")]
    total = len(pending)
    rows = []
    for r in pending[:_SQL_LIST_LIMIT]:
        days = _days_since(ref_now, r.get("sql_date"))
        has_campaign = bool(r.get("campaign_name"))
        rows.append({
            "contact_id": r.get("contact_id"),
            "company": r.get("company"),
            "company_id": None,
            "main_contact": None,
            "sql_date": r.get("sql_date"),
            "days_since_sql": days,
            "source_group": None,           # per-lead source group not exposed here
            "source_detail": None,
            "campaign_name": r.get("campaign_name"),
            "country": r.get("country"),
            "status": _sql_no_deal_status(days, has_campaign),
        })
    return rows, total, True


# ── Decision cards ───────────────────────────────────────────────────────────

def _fmt_usd_short(value) -> str:
    if value is None:
        return "Unavailable"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "Unavailable"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:.0f}"


def _build_decision_cards(kpis: dict, source_rows: list, sql_no_deal_total: int,
                          sql_no_deal_available: bool) -> list:
    cards = []

    # Scale — the source producing the most closed-won revenue.
    proven = [s for s in source_rows if (s.get("closed_won_revenue_usd") or 0) > 0]
    if proven:
        top = max(proven, key=lambda s: (s.get("closed_won_revenue_usd") or 0.0,
                                         s.get("closed_won_customers") or 0))
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": f"{top['source_detail']} produces the most closed-won revenue",
            "body": (f"{top['source_detail']} generated {_fmt_usd_short(top['closed_won_revenue_usd'])} "
                     f"closed-won revenue from {top.get('closed_won_customers') or 0} customer"
                     f"{'s' if (top.get('closed_won_customers') or 0) != 1 else ''} this window."),
            "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
        })
    else:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "No source has closed-won revenue yet",
            "body": "No source produced closed-won customers in this window.",
            "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
        })

    # Watch — a source with SQL volume but weak customer conversion.
    weak = [s for s in source_rows if (s.get("sqls") or 0) >= 3 and (s.get("closed_won_customers") or 0) == 0]
    if weak:
        top = max(weak, key=lambda s: s.get("sqls") or 0)
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": f"{top['source_detail']} has SQL volume but no customers",
            "body": (f"{top['source_detail']} produced {top['sqls']} SQLs but no closed-won "
                     "customers in this window. Review lead quality and sales follow-up."),
            "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
        })
    else:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "No source with unconverted SQL volume",
            "body": "Every source with meaningful SQL volume produced at least one customer, "
                    "or there is not enough SQL volume to judge.",
            "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
        })

    # Investigate — qualified SQLs that have not become customers.
    if sql_no_deal_available and sql_no_deal_total > 0:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Qualified SQLs not yet closed-won",
            "body": (f"{sql_no_deal_total} qualified SQL{'s' if sql_no_deal_total != 1 else ''} "
                     "in this window have no closed-won deal yet. Open / lost pipeline is not "
                     "visible here — review these as a sales follow-up gap."),
            "target_page": "leads", "target_label": "Open Leads",
        })
    else:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "No unconverted SQL backlog surfaced",
            "body": "Either every qualified SQL has a closed-won deal, or per-SQL linkage is "
                    "unavailable for this window.",
            "target_page": "leads", "target_label": "Open Leads",
        })

    # Unavailable — the honest, central caveat of this tab.
    cards.append({
        "type": "unavailable", "title": "Unavailable",
        "headline": "Open pipeline & opportunity stages unavailable",
        "body": ("Open opportunities, closed-lost deals and opportunity aging are not stored "
                 "in the durable HubSpot deal ledger — only closed-won deals are. Those "
                 "sections show Unavailable, never a fabricated pipeline number."),
        "target_page": "revenue-health", "target_label": "Open Revenue Health",
    })
    return cards


# ── Truth status + unavailable ───────────────────────────────────────────────

def _build_truth_status(readiness: dict, deals: list, revenue_connected: bool,
                        ledger_available: bool, sql_no_deal_available: bool) -> dict:
    lead_ready = bool(readiness.get("lead_metrics_ready"))
    amounts_partial = any(d.get("amount_usd") is None for d in deals)
    attribution_partial = any(d.get("attribution_status") != "attributed" for d in deals)
    return {
        "sqls": "ready" if lead_ready else "partial",
        "deals": ("ready" if (revenue_connected and ledger_available)
                  else ("blocked" if not revenue_connected else "unavailable")),
        # Stage data is structurally partial: only closed-won is durable.
        "stages": "partial",
        "amounts": "partial" if amounts_partial else "ready",
        "attribution": "partial" if attribution_partial else "ready",
        "read_only": "ready",
    }


def _build_unavailable(kpis: dict, deals: list, revenue_connected: bool,
                       ledger_available: bool, sql_no_deal_available: bool,
                       period_change: dict) -> list:
    out = [
        {"metric": "open_pipeline_usd", "reason": OPEN_PIPELINE_REASON},
        {"metric": "opportunities_created", "reason": OPEN_PIPELINE_REASON},
        {"metric": "closed_lost_deals", "reason": STAGE_REASON},
        {"metric": "opportunity_aging", "reason": OPEN_PIPELINE_REASON},
        {"metric": "sql_to_opportunity_rate",
         "reason": "Opportunity-created counts are not durable, so the SQL → Opportunity rate is unavailable."},
        {"metric": "opportunity_to_customer_rate",
         "reason": "Opportunity counts are not durable, so the Opportunity → Customer rate is unavailable."},
        {"metric": "avg_days_to_close",
         "reason": "Deal create dates are not persisted (deal_created_at is always empty), so time-to-close is unavailable."},
    ]
    if not revenue_connected:
        out.append({"metric": "closed_won_revenue_usd",
                    "reason": "Closed-won customers and revenue are withheld — the revenue integration is disconnected."})
    elif not ledger_available:
        out.append({"metric": "deals",
                    "reason": "The closed-won deal ledger could not be read; deal proof is unavailable this window."})
    if kpis.get("sqls") is None:
        out.append({"metric": "sqls",
                    "reason": "SQLs withheld — lead business event dates are unavailable."})
    if not sql_no_deal_available:
        out.append({"metric": "sql_no_deal",
                    "reason": "Per-SQL linkage is unavailable; the qualified-lead source could not be read."})
    if not period_change.get("available"):
        out.append({"metric": "period_change",
                    "reason": period_change.get("reason") or "No comparison available."})
    return out


# ── Period change (real closed-won metrics only) ─────────────────────────────

def _build_period_change(window: str, now: datetime, kpis: dict) -> dict:
    ref = _previous_window_reference(window, now)
    if ref is None:
        return {"available": False, "label": None, "note": None, "metrics": {},
                "reason": "All Time has no previous period to compare against."}
    prev_key, prev_now, label, note = ref
    try:
        prev = build_revenue_decision_mart(view="deal", window=prev_key, now=prev_now)
        ps = prev.get("summary") or {}
        connected = bool((prev.get("readiness") or {}).get("revenue_integration_connected"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dashboard deals previous-period build failed: %s", exc)
        return {"available": False, "label": label, "note": note, "metrics": {},
                "reason": "Previous period unavailable."}
    prev_sqls = ps.get("sqls")
    prev_customers = ps.get("customers") if connected else None
    prev_won = _round2(ps.get("won_revenue_usd")) if connected else None
    metrics = {
        "sqls": _delta_metric(kpis.get("sqls"), prev_sqls),
        # Opportunity metrics are structurally unavailable → no comparison.
        "opportunities_created": _delta_metric(None, None),
        "open_opportunities": _delta_metric(None, None),
        "open_pipeline_usd": _delta_metric(None, None),
        "closed_won_customers": _delta_metric(kpis.get("closed_won_customers"), prev_customers),
        "closed_won_revenue_usd": _delta_metric(kpis.get("closed_won_revenue_usd"), prev_won),
        "sql_to_opportunity_rate": _delta_metric(None, None),
        "opportunity_to_customer_rate": _delta_metric(None, None),
        "sql_to_customer_rate": _delta_metric(kpis.get("sql_to_customer_rate"),
                                              _rate(prev_customers, prev_sqls)),
    }
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label, "note": note, "metrics": metrics, "reason": None,
    }


def build_dashboard_deals(window: str = "current_quarter",
                          now: datetime | None = None) -> dict:
    """Build the Dashboard Deals & Pipeline Intelligence contract for one window.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolve_window(window, now=now)
    ref_now = _coerce_utc(now)

    mart = build_revenue_decision_mart(view="deal", window=window, now=now)
    window_block = mart.get("window") or {}
    summary = mart.get("summary") or {}
    readiness = mart.get("readiness") or {}
    revenue_connected = bool(readiness.get("revenue_integration_connected"))

    start = _parse_iso_date(window_block.get("start_date"))
    end = _parse_iso_date(window_block.get("end_date"))

    # Read the RAW closed-won deal ledger (not the mart deal rows): the mart coerces
    # a null deal amount to $0 via _safe_float, which would fabricate a $0. The raw
    # fetch preserves a null amount so a missing amount renders "Unavailable".
    deals_ledger = repo.fetch_revenue_deals(start, end)
    ledger_available = bool(deals_ledger.get("available"))
    won_deal_rows = deals_ledger.get("rows") or []

    # Per-source + per-campaign closed-won pipeline (canonical, read-only).
    by_source = build_revenue_by_source(window, now=now)
    mart_campaign = build_revenue_decision_mart(view="campaign", window=window, now=now)
    mart_campaign_rows = mart_campaign.get("rows") or []

    # Qualified SQLs whose contact has no closed-won deal yet.
    sql_result = repo.fetch_sql_lead_details(start, end)
    sql_no_deal_rows, sql_no_deal_total, sql_no_deal_available = _build_sql_no_deal(
        sql_result, ref_now, revenue_connected)
    sqls_not_closed_won = sql_no_deal_total if sql_no_deal_available else None

    kpis = _build_kpis(summary, revenue_connected, sqls_not_closed_won)
    deals = _build_deals(won_deal_rows, revenue_connected, ledger_available)
    funnel = _build_funnel(kpis)
    stage_mix = _build_stage_mix(kpis)
    aging_buckets = _build_aging_buckets()
    source_breakdown = _build_source_breakdown(by_source, revenue_connected)
    campaign_breakdown = _build_campaign_breakdown(mart_campaign_rows, revenue_connected)
    period_change = _build_period_change(window, ref_now, kpis)
    decision_cards = _build_decision_cards(kpis, source_breakdown, sql_no_deal_total,
                                           sql_no_deal_available)
    truth_status = _build_truth_status(readiness, deals, revenue_connected,
                                       ledger_available, sql_no_deal_available)
    unavailable = _build_unavailable(kpis, deals, revenue_connected, ledger_available,
                                     sql_no_deal_available, period_change)

    return {
        "window": window_block,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        "source_truth": "hubspot_deal_pipeline_truth",
        "google_ads_conversion_value_used": False,
        # Structural truth: open opportunity / lost / aging stage data is NOT durable.
        "pipeline_stage_data_available": False,
        "pipeline_stage_reason": OPEN_PIPELINE_REASON,
        "deal_proof_available": bool(revenue_connected and ledger_available),
        "kpis": kpis,
        "period_change": period_change,
        "funnel": funnel,
        "stage_mix": stage_mix,
        "source_breakdown": source_breakdown,
        "campaign_breakdown": campaign_breakdown,
        "aging_buckets": aging_buckets,
        "deals": deals,
        "sql_no_deal": sql_no_deal_rows,
        "sql_no_deal_total": sql_no_deal_total,
        "sql_no_deal_available": sql_no_deal_available,
        "decision_cards": decision_cards,
        "truth_status": truth_status,
        "unavailable": unavailable,
    }
