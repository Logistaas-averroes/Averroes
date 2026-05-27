"""Shared unit economics aggregation for live and snapshot ROAS reports."""

from datetime import datetime, timezone

from analysis.roas_calculator import compute_verdict
from analysis.unit_economics import compute_cac, compute_ltv_to_cac, compute_payback_months
from connectors.hubspot_churn import get_monthly_churn


def _dominant_attribution_from_campaigns(campaigns: list[dict]) -> str:
    """Derive overall attribution confidence weighted by deals_won."""
    weighted = {}
    for campaign in campaigns:
        confidence = campaign.get("attribution_confidence") or "tier_3_spend_weighted"
        weight = campaign.get("deals_won", 0)
        try:
            weight = int(weight)
        except (ValueError, TypeError):
            weight = 0
        if weight <= 0:
            continue
        weighted[confidence] = weighted.get(confidence, 0) + weight
    if not weighted:
        return "tier_3_spend_weighted"
    return max(weighted, key=weighted.get)


def compute_unit_economics_summary(campaigns: list[dict]) -> dict:
    """Aggregate overall unit-economics metrics from campaign ROAS rows."""
    total_spend = sum(c.get("spend", 0) for c in campaigns)
    total_deals = sum(c.get("deals_won", 0) for c in campaigns)
    total_acv = sum(c.get("acv_revenue", 0) for c in campaigns)
    total_mrr = sum(c.get("mrr_revenue", 0) for c in campaigns)
    total_ltv = sum(c.get("ltv_revenue", 0) for c in campaigns)

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    churn_info = get_monthly_churn(current_month)
    churn_rate = churn_info["monthly_churn_rate"]

    avg_deal_acv = round(total_acv / total_deals) if total_deals > 0 else 0
    avg_deal_mrr = round(total_mrr / total_deals) if total_deals > 0 else 0
    avg_ltv = total_ltv / total_deals if total_deals > 0 else 0

    cac = compute_cac(total_spend, total_deals)
    ltv_cac = compute_ltv_to_cac(avg_ltv, cac) if cac else None
    payback = compute_payback_months(cac, avg_deal_mrr) if cac and avg_deal_mrr > 0 else None

    overall_attribution = _dominant_attribution_from_campaigns(campaigns)
    overall_verdict = compute_verdict(ltv_cac, payback, total_deals, overall_attribution)

    return {
        "ltv_to_cac": round(ltv_cac, 2) if ltv_cac else None,
        "payback_months": round(payback, 1) if payback else None,
        "avg_deal_acv": avg_deal_acv,
        "avg_deal_mrr": avg_deal_mrr,
        "monthly_churn_rate_used": churn_rate,
        "total_spend": round(total_spend, 2),
        "total_deals_won": total_deals,
        "total_acv_revenue": round(total_acv, 2),
        "total_ltv_revenue": round(total_ltv, 2),
        "overall_attribution_confidence": overall_attribution,
        "overall_verdict": overall_verdict,
        "verdict": overall_verdict,
    }
