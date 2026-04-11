"""
Avverros Doctrine Engine
Calls Claude API with structured campaign data + doctrine system prompt.
Returns plain-language recommendations with FIX/HOLD/SCALE/CUT classifications.
"""

import os
import json
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()
DOCTRINE_PATH = Path("docs/DOCTRINE.md")
MODEL = "claude-sonnet-4-6"


def load_doctrine() -> str:
    """Load the doctrine rules from DOCTRINE.md as the system prompt."""
    return DOCTRINE_PATH.read_text()


def run_weekly_analysis(
    campaigns: list,
    search_terms: list,
    crm_summary: dict,
    junk_leads: list,
    ngram_findings: dict,
    classifier_results: list,
    geo_data: list,
) -> dict:
    """
    Run the full weekly doctrine analysis via Claude API.
    Returns structured recommendations.
    """
    system_prompt = load_doctrine()

    # Build structured data payload for Claude
    analysis_payload = {
        "report_type": "weekly_doctrine_audit",
        "campaigns": classifier_results,  # Pre-classified by campaign_classifier.py
        "account_summary": {
            "total_spend_7d": sum(float(c.get("spend", 0) or 0) for c in campaigns),
            "total_conversions_7d": sum(float(c.get("conversions", 0) or 0) for c in campaigns),
        },
        "lead_quality": {
            "total_paid_leads": crm_summary.get("total", 0),
            "gclid_coverage_pct": crm_summary.get("gclid_coverage_pct", 0),
            "mql_status_breakdown": crm_summary.get("mql_status_breakdown", {}),
            "junk_count": len(junk_leads),
            "junk_examples": junk_leads[:5],  # Top 5 examples
        },
        "intent_mismatch": {
            "top_waste_terms": ngram_findings.get("top_waste_terms", [])[:20],
            "estimated_weekly_waste_usd": ngram_findings.get("total_estimated_waste", 0),
            "junk_pattern_breakdown": ngram_findings.get("pattern_breakdown", {}),
        },
        "geo_summary": {
            "by_tier": _aggregate_geo_by_tier(geo_data),
            "zero_conversion_countries": [
                g for g in geo_data
                if float(g.get("conversions", 0) or 0) == 0
                and float(g.get("spend", 0) or 0) > 50
            ][:10],
        },
    }

    user_message = f"""
Analyse the following Google Ads and CRM data for Logistaas and produce the weekly doctrine report.

DATA:
{json.dumps(analysis_payload, indent=2, default=str)}

Produce your analysis in this exact structure:

## Campaign State Classifications
[Table: Campaign | State | Rationale]

## Top 3 Priority Actions This Week
[Numbered list — specific, actionable, prioritised by revenue impact]

## Money Leak Report
[Search terms to add as negatives with estimated weekly waste each]

## Lead Quality Assessment
[MQL vs SQL vs junk breakdown, patterns, what's driving junk]

## Geo Intelligence
[Which markets are performing, which are burning budget, tier recommendations]

## Risk Flags
[Urgent issues before next spend cycle]

Be direct and specific. Name campaigns, keywords, and countries. Quantify waste where possible.
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    report_text = response.content[0].text

    return {
        "report_type": "weekly",
        "generated_at": _now_str(),
        "model": MODEL,
        "report": report_text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def run_daily_analysis(
    anomalies: list,
    crm_delta: dict,
    new_junk_terms: list,
    budget_pacing: list,
) -> dict:
    """
    Lightweight daily pulse analysis.
    Only runs full Claude call if anomalies are detected.
    """
    if not anomalies and not new_junk_terms:
        return {
            "report_type": "daily",
            "generated_at": _now_str(),
            "status": "clean",
            "message": "No anomalies detected. System healthy.",
        }

    system_prompt = load_doctrine()

    payload = {
        "report_type": "daily_pulse",
        "anomalies": anomalies,
        "crm_delta": crm_delta,
        "new_junk_terms": new_junk_terms[:10],
        "budget_pacing_alerts": [b for b in budget_pacing if b.get("alert")],
    }

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        temperature=0,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"Daily pulse alert. Review these anomalies and provide immediate action items:\n\n{json.dumps(payload, indent=2, default=str)}"
        }]
    )

    return {
        "report_type": "daily",
        "generated_at": _now_str(),
        "status": "alerts",
        "alert_count": len(anomalies) + len(new_junk_terms),
        "report": response.content[0].text,
    }


def run_monthly_strategy(
    monthly_data: dict,
    competitor_data: dict,
    geo_30d: list,
    deal_pipeline: list,
) -> dict:
    """
    Full monthly strategic advisory — extended context window.
    """
    system_prompt = load_doctrine()

    payload = {
        "report_type": "monthly_strategy",
        "period": monthly_data.get("period"),
        "performance_summary": monthly_data,
        "competitor_analysis": competitor_data,
        "geo_30d_breakdown": geo_30d,
        "pipeline_data": {
            "deals_by_stage": _aggregate_deals_by_stage(deal_pipeline),
            "deals_by_country": _aggregate_deals_by_country(deal_pipeline),
        },
    }

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        temperature=0,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"""
Produce the full monthly strategy report for Logistaas. This is the most important report of the month.

DATA:
{json.dumps(payload, indent=2, default=str)}

Structure:
## Executive Summary
## Campaign Portfolio Review
## Regional Market Analysis (MENA / SEA / LATAM / Other)
## Budget Reallocation Plan
## Competitor Conquesting Review
## Ad Copy & Landing Page Recommendations
## Smart Bidding Health Check
## 30-Day Action Plan (prioritised)
"""
        }]
    )

    return {
        "report_type": "monthly",
        "generated_at": _now_str(),
        "model": MODEL,
        "report": response.content[0].text,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now_str() -> str:
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def _aggregate_geo_by_tier(geo_data: list) -> dict:
    """Group geo performance by tier (defined in config)."""
    from connectors.hubspot_pull import DEAL_STAGE_MAP
    import yaml

    with open("config/logistaas_config.yaml") as f:
        config = yaml.safe_load(f)

    tiers = {"tier_1": [], "tier_2": [], "tier_3": [], "unassigned": []}

    t1 = [c.lower() for c in config["geo_tiers"]["tier_1"]["countries"]]
    t2 = [c.lower() for c in config["geo_tiers"]["tier_2"]["countries"]]
    t3 = [c.lower() for c in config["geo_tiers"]["tier_3"]["countries"]]

    for row in geo_data:
        country = (row.get("country") or "").lower()
        entry = {
            "country": country,
            "spend": float(row.get("spend", 0) or 0),
            "conversions": float(row.get("conversions", 0) or 0),
        }
        if country in t1:
            tiers["tier_1"].append(entry)
        elif country in t2:
            tiers["tier_2"].append(entry)
        elif country in t3:
            tiers["tier_3"].append(entry)
        else:
            tiers["unassigned"].append(entry)

    return tiers


def _aggregate_deals_by_stage(deals: list) -> dict:
    from connectors.hubspot_pull import DEAL_STAGE_MAP
    result = {}
    for d in deals:
        stage = d.get("properties", {}).get("dealstage", "unknown")
        label = DEAL_STAGE_MAP.get(stage, stage)
        result[label] = result.get(label, 0) + 1
    return result


def _aggregate_deals_by_country(deals: list) -> dict:
    result = {}
    for d in deals:
        country = d.get("contact_country", "unknown")
        result[country] = result.get(country, 0) + 1
    return result
