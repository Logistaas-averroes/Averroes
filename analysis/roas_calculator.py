"""
ROAS Calculator
Central backend truth layer for ROAS, CAC, LTV/CAC, payback, close rate,
true CPL, and verdict.

Responsibility:
  - Compute campaign-level and country-level ROAS.
  - Issue verdicts based on configurable thresholds.
  - All thresholds from config/thresholds.yaml.
  - Verdict logic lives here — not frontend.

What is NOT in this file:
  - No external API calls.
  - No frontend formatting.
  - No Google Ads ROAS metrics.
  - No Google Ads conversion value.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from analysis.attribution_matcher import attribute_deals, CAMPAIGN_TAGS
from analysis.unit_economics import (
    compute_cac,
    compute_ltv_from_mrr,
    compute_ltv_to_cac,
    compute_payback_months,
)
from connectors.hubspot_churn import get_monthly_churn

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"
DATA_DIR = Path(__file__).parent.parent / "data"
THRESHOLDS_FILE = CONFIG_DIR / "thresholds.yaml"


def _parse_iso_datetime(value) -> datetime:
    """Parse ISO datetime input to naive UTC-compatible datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    raise TypeError("Unsupported date value type")


def _load_thresholds() -> dict:
    """Load ROAS thresholds from config/thresholds.yaml."""
    if not THRESHOLDS_FILE.exists():
        logger.warning("Thresholds file not found, using defaults")
        return {
            "ltv_cac_scale_threshold": 3.0,
            "ltv_cac_fix_threshold": 1.5,
            "ltv_cac_cut_threshold": 1.0,
            "payback_months_scale_max": 18,
            "payback_months_fix_max": 36,
            "min_deals_for_verdict": 5,
            "default_monthly_churn": 0.03,
            "allow_scale_on_spend_weighted_attribution": False,
        }

    with open(THRESHOLDS_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    roas_config = config.get("roas", {})
    return {
        "ltv_cac_scale_threshold": roas_config.get("ltv_cac_scale_threshold", 3.0),
        "ltv_cac_fix_threshold": roas_config.get("ltv_cac_fix_threshold", 1.5),
        "ltv_cac_cut_threshold": roas_config.get("ltv_cac_cut_threshold", 1.0),
        "payback_months_scale_max": roas_config.get("payback_months_scale_max", 18),
        "payback_months_fix_max": roas_config.get("payback_months_fix_max", 36),
        "min_deals_for_verdict": roas_config.get("min_deals_for_verdict", 5),
        "default_monthly_churn": roas_config.get("default_monthly_churn", 0.03),
        "allow_scale_on_spend_weighted_attribution": roas_config.get(
            "allow_scale_on_spend_weighted_attribution", False
        ),
    }


def _load_windsor_spend() -> list:
    """Load Windsor campaign spend data."""
    spend_file = DATA_DIR / "campaign_performance.json"
    if not spend_file.exists():
        logger.warning("Windsor campaign performance file not found")
        return []
    with open(spend_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_campaign_spend(windsor_data: list, campaign: str, window_days: int) -> float:
    """Get total spend for a campaign within the time window."""
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    total = 0.0
    missing_date_rows = 0
    invalid_date_rows = 0

    for row in windsor_data:
        row_campaign = (row.get("campaign") or "").lower().strip()
        if row_campaign != campaign.lower().strip():
            continue

        row_date = row.get("date") or row.get("source_date")
        if not row_date:
            missing_date_rows += 1
            continue
        try:
            parsed = _parse_iso_datetime(row_date)
            if parsed < cutoff:
                continue
        except (ValueError, TypeError):
            invalid_date_rows += 1
            continue

        spend = row.get("spend", 0) or row.get("cost", 0) or 0
        try:
            total += float(spend)
        except (ValueError, TypeError):
            pass

    if missing_date_rows or invalid_date_rows:
        logger.warning(
            "Skipped %d Windsor spend rows for campaign '%s' due to missing date and %d due to invalid date",
            missing_date_rows,
            campaign,
            invalid_date_rows,
        )

    return total


def _get_country_spend(windsor_data: list, country: str, window_days: int) -> float:
    """Get total spend for a country within the time window."""
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    total = 0.0
    missing_date_rows = 0
    invalid_date_rows = 0

    for row in windsor_data:
        row_country = (row.get("country") or "").lower().strip()
        if row_country != country.lower().strip():
            continue

        row_date = row.get("date") or row.get("source_date")
        if not row_date:
            missing_date_rows += 1
            continue
        try:
            parsed = _parse_iso_datetime(row_date)
            if parsed < cutoff:
                continue
        except (ValueError, TypeError):
            invalid_date_rows += 1
            continue

        spend = row.get("spend", 0) or row.get("cost", 0) or 0
        try:
            total += float(spend)
        except (ValueError, TypeError):
            pass

    if missing_date_rows or invalid_date_rows:
        logger.warning(
            "Skipped %d Windsor spend rows for country '%s' due to missing date and %d due to invalid date",
            missing_date_rows,
            country,
            invalid_date_rows,
        )

    return total


def _get_total_spend(windsor_data: list, window_days: int) -> float:
    """Get total spend across all campaigns within the time window."""
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    total = 0.0
    missing_date_rows = 0
    invalid_date_rows = 0

    for row in windsor_data:
        row_date = row.get("date") or row.get("source_date")
        if not row_date:
            missing_date_rows += 1
            continue
        try:
            parsed = _parse_iso_datetime(row_date)
            if parsed < cutoff:
                continue
        except (ValueError, TypeError):
            invalid_date_rows += 1
            continue

        spend = row.get("spend", 0) or row.get("cost", 0) or 0
        try:
            total += float(spend)
        except (ValueError, TypeError):
            pass

    if missing_date_rows or invalid_date_rows:
        logger.warning(
            "Skipped %d Windsor spend rows due to missing date and %d due to invalid date",
            missing_date_rows,
            invalid_date_rows,
        )

    return total


def compute_verdict(
    ltv_to_cac: float | None,
    payback_months: float | None,
    deals_won: int,
    attribution_confidence: str,
) -> str:
    """Compute verdict based on unit economics and attribution quality.

    Verdict logic:
      - INSUFFICIENT_DATA if deals_won < configured min_deals_for_verdict
      - HOLD if attribution is tier_3_spend_weighted (never SCALE from estimates)
      - CUT if LTV/CAC < 1
      - FIX if LTV/CAC < 1.5 or payback > 36
      - HOLD if 1.5 <= LTV/CAC < 3
      - SCALE if LTV/CAC >= 3 and payback <= 18
      - Else HOLD

    All numeric thresholds from config/thresholds.yaml.
    """
    thresholds = _load_thresholds()
    min_deals = thresholds["min_deals_for_verdict"]
    allow_scale_tier3 = thresholds["allow_scale_on_spend_weighted_attribution"]

    if deals_won < min_deals:
        return "INSUFFICIENT_DATA"

    if attribution_confidence == "tier_3_spend_weighted" and not allow_scale_tier3:
        return "HOLD"

    if ltv_to_cac is None:
        return "INSUFFICIENT_DATA"

    cut_threshold = thresholds["ltv_cac_cut_threshold"]
    fix_threshold = thresholds["ltv_cac_fix_threshold"]
    scale_threshold = thresholds["ltv_cac_scale_threshold"]
    payback_fix_max = thresholds["payback_months_fix_max"]
    payback_scale_max = thresholds["payback_months_scale_max"]

    if ltv_to_cac < cut_threshold:
        return "CUT"

    if ltv_to_cac < fix_threshold:
        return "FIX"

    if payback_months is not None and payback_months > payback_fix_max:
        return "FIX"

    if ltv_to_cac >= scale_threshold:
        if payback_months is not None and payback_months <= payback_scale_max:
            return "SCALE"
        return "HOLD"

    # 1.5 <= ltv_to_cac < 3.0
    return "HOLD"


def _filter_deals_by_window(deals: list, window_days: int) -> list:
    """Filter attributed deals by closedate within the time window.

    Conservative fallback: include deals with missing/unparseable closedate, with warning.
    """
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    filtered = []
    missing_closedate = 0
    invalid_closedate = 0
    for deal in deals:
        closedate = deal.get("closedate")
        if closedate:
            try:
                parsed = _parse_iso_datetime(closedate)
                if parsed < cutoff:
                    continue
                filtered.append(deal)
                continue
            except (ValueError, TypeError):
                invalid_closedate += 1
                filtered.append(deal)
                continue
        missing_closedate += 1
        filtered.append(deal)

    if missing_closedate or invalid_closedate:
        logger.warning(
            "Included %d deals with missing closedate and %d with invalid closedate in %dd window fallback",
            missing_closedate,
            invalid_closedate,
            window_days,
        )
    return filtered


def _compute_roas_metrics(
    deals: list, spend: float, campaign_or_country: str, churn_rate: float
) -> dict:
    """Compute all ROAS metrics for a set of deals and spend."""
    warnings = []
    deals_won = len(deals)

    # Revenue aggregation
    acv_revenue = sum(d.get("hs_acv", 0) or d.get("amount", 0) for d in deals)
    arr_revenue = sum(d.get("hs_arr", 0) for d in deals)
    mrr_revenue = sum(d.get("hs_mrr", 0) for d in deals)

    # LTV from MRR
    ltv_revenue = 0.0
    for d in deals:
        deal_mrr = d.get("hs_mrr", 0)
        if deal_mrr and deal_mrr > 0:
            ltv_revenue += compute_ltv_from_mrr(deal_mrr, churn_rate)

    # ROAS calculations
    acv_roas = round(acv_revenue / spend, 2) if spend > 0 else None
    arr_roas = round(arr_revenue / spend, 2) if spend > 0 else None
    ltv_roas = round(ltv_revenue / spend, 2) if spend > 0 else None

    # Unit economics
    cac = compute_cac(spend, deals_won)
    avg_mrr = mrr_revenue / deals_won if deals_won > 0 else 0
    avg_ltv = ltv_revenue / deals_won if deals_won > 0 else 0
    ltv_cac = compute_ltv_to_cac(avg_ltv, cac) if cac else None
    payback = compute_payback_months(cac, avg_mrr) if cac and avg_mrr > 0 else None

    # True CPL — simplified: spend / deals as proxy when detailed lead data unavailable
    true_cpl = round(spend / deals_won, 2) if deals_won > 0 else None

    # Close rate — placeholder (needs Google conversions data)
    close_rate = None

    # Determine dominant attribution confidence
    confidence_counts = {}
    for d in deals:
        conf = d.get("attribution_confidence", "tier_3_spend_weighted")
        confidence_counts[conf] = confidence_counts.get(conf, 0) + 1

    dominant_confidence = max(confidence_counts, key=confidence_counts.get) if confidence_counts else "tier_3_spend_weighted"

    # Verdict
    verdict = compute_verdict(ltv_cac, payback, deals_won, dominant_confidence)

    if spend == 0:
        warnings.append("No spend data available for this period")
    if deals_won == 0:
        warnings.append("No won deals in this period")
    if acv_revenue == 0 and deals_won > 0:
        warnings.append("All deals have zero ACV — check amount fields")

    return {
        "spend": round(spend, 2),
        "deals_won": deals_won,
        "acv_revenue": round(acv_revenue, 2),
        "arr_revenue": round(arr_revenue, 2),
        "mrr_revenue": round(mrr_revenue, 2),
        "ltv_revenue": round(ltv_revenue, 2),
        "acv_roas": acv_roas,
        "arr_roas": arr_roas,
        "ltv_roas": ltv_roas,
        "cac": round(cac, 2) if cac else None,
        "ltv_to_cac": round(ltv_cac, 2) if ltv_cac else None,
        "payback_months": round(payback, 1) if payback else None,
        "true_cpl": true_cpl,
        "close_rate": close_rate,
        "attribution_confidence": dominant_confidence,
        "verdict": verdict,
        "warnings": warnings,
    }


def compute_campaign_roas(campaign: str, window_days: int = 60) -> dict:
    """Compute ROAS metrics for a single campaign.

    Args:
        campaign: Campaign name (case-insensitive).
        window_days: Lookback window in days.

    Returns:
        Dict with full ROAS metrics for the campaign.
    """
    attributed_deals = attribute_deals()
    windsor_data = _load_windsor_spend()

    # Filter deals for this campaign and window
    campaign_deals = [
        d for d in attributed_deals
        if (d.get("campaign") or "").lower().strip() == campaign.lower().strip()
    ]
    campaign_deals = _filter_deals_by_window(campaign_deals, window_days)

    # Get spend
    spend = _get_campaign_spend(windsor_data, campaign, window_days)

    # Get churn
    current_month = datetime.utcnow().strftime("%Y-%m")
    churn_info = get_monthly_churn(current_month, campaign)
    churn_rate = churn_info["monthly_churn_rate"]

    metrics = _compute_roas_metrics(campaign_deals, spend, campaign, churn_rate)
    metrics["campaign"] = campaign

    return metrics


def compute_country_roas(country: str, window_days: int = 60) -> dict:
    """Compute ROAS metrics for a single country.

    Country-level ROAS is always marked as estimate until GCLID is fully wired.

    Args:
        country: Country name (case-insensitive).
        window_days: Lookback window in days.

    Returns:
        Dict with full ROAS metrics for the country.
    """
    attributed_deals = attribute_deals()
    windsor_data = _load_windsor_spend()

    # Filter deals for this country and window
    country_deals = [
        d for d in attributed_deals
        if (d.get("country") or "").lower().strip() == country.lower().strip()
    ]
    country_deals = _filter_deals_by_window(country_deals, window_days)

    # Get spend
    spend = _get_country_spend(windsor_data, country, window_days)

    # Get churn
    current_month = datetime.utcnow().strftime("%Y-%m")
    churn_info = get_monthly_churn(current_month)
    churn_rate = churn_info["monthly_churn_rate"]

    metrics = _compute_roas_metrics(country_deals, spend, country, churn_rate)
    metrics["country"] = country

    # Hard rule: country-level is always estimate until GCLID is wired
    # Override attribution confidence unless tier_1 is actually dominant
    if metrics["attribution_confidence"] != "tier_1_gclid":
        metrics["attribution_confidence"] = "tier_3_spend_weighted"
    metrics["country_level_estimate"] = True

    return metrics


def compute_all_campaign_roas(window_days: int = 60) -> list[dict]:
    """Compute ROAS for all campaigns with attributed deals.

    Returns:
        List of campaign ROAS dicts.
    """
    attributed_deals = attribute_deals()
    windsor_data = _load_windsor_spend()

    # Get churn
    current_month = datetime.utcnow().strftime("%Y-%m")

    # Group deals by campaign
    campaigns = {}
    for deal in attributed_deals:
        campaign = (deal.get("campaign") or "unknown").lower().strip()
        if campaign not in campaigns:
            campaigns[campaign] = []
        campaigns[campaign].append(deal)

    results = []
    for campaign, deals in campaigns.items():
        filtered_deals = _filter_deals_by_window(deals, window_days)
        if not filtered_deals:
            continue

        spend = _get_campaign_spend(windsor_data, campaign, window_days)
        churn_info = get_monthly_churn(current_month, campaign)
        churn_rate = churn_info["monthly_churn_rate"]

        metrics = _compute_roas_metrics(filtered_deals, spend, campaign, churn_rate)
        metrics["campaign"] = campaign
        results.append(metrics)

    # Sort by spend descending
    results.sort(key=lambda x: x.get("spend", 0), reverse=True)
    return results


def compute_all_country_roas(window_days: int = 60) -> list[dict]:
    """Compute ROAS for all countries with attributed deals.

    Hard rule: All country rows marked as estimate until GCLID is wired.

    Returns:
        List of country ROAS dicts.
    """
    attributed_deals = attribute_deals()
    windsor_data = _load_windsor_spend()

    current_month = datetime.utcnow().strftime("%Y-%m")

    # Group deals by country
    countries = {}
    for deal in attributed_deals:
        country = (deal.get("country") or "unknown").lower().strip()
        if country not in countries:
            countries[country] = []
        countries[country].append(deal)

    results = []
    for country, deals in countries.items():
        filtered_deals = _filter_deals_by_window(deals, window_days)
        if not filtered_deals:
            continue

        spend = _get_country_spend(windsor_data, country, window_days)
        churn_info = get_monthly_churn(current_month)
        churn_rate = churn_info["monthly_churn_rate"]

        metrics = _compute_roas_metrics(filtered_deals, spend, country, churn_rate)
        metrics["country"] = country

        # Hard rule: country-level is always estimate until GCLID is wired
        if metrics["attribution_confidence"] != "tier_1_gclid":
            metrics["attribution_confidence"] = "tier_3_spend_weighted"
        metrics["country_level_estimate"] = True

        results.append(metrics)

    results.sort(key=lambda x: x.get("spend", 0), reverse=True)
    return results
