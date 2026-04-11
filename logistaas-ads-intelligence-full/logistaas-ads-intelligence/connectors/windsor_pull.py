"""
Windsor.ai Google Ads Connector
Pulls campaign performance, search terms, and keyword data.
Requires Windsor.ai Basic plan or above for search term data.
"""

import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY")
WINDSOR_ACCOUNT_ID = os.getenv("WINDSOR_ACCOUNT_ID")
WINDSOR_BASE_URL = "https://connectors.windsor.ai/all"


def get_date_range(days_back: int = 30) -> tuple:
    end = datetime.utcnow().date()
    start = end - timedelta(days=days_back)
    return str(start), str(end)


def pull_campaign_performance(days_back: int = 30) -> list:
    """
    Pull campaign-level performance metrics.
    Fields: campaign, spend, clicks, impressions, conversions, cpc
    """
    start, end = get_date_range(days_back)

    params = {
        "api_key": WINDSOR_API_KEY,
        "date_from": start,
        "date_to": end,
        "fields": ",".join([
            "date",
            "campaign",
            "campaign_id",
            "spend",
            "clicks",
            "impressions",
            "conversions",
            "cpc",
            "cpm",
            "ctr",
            "conversion_rate",
        ]),
        "data_source": "google_ads",
        "account_id": WINDSOR_ACCOUNT_ID,
    }

    response = requests.get(WINDSOR_BASE_URL, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()

    records = data.get("data", [])
    print(f"Pulled {len(records)} campaign rows (last {days_back} days)")
    return records


def pull_search_terms(days_back: int = 14) -> list:
    """
    Pull actual search terms that triggered ads.
    This is the raw material for N-gram analysis.
    Requires Windsor.ai paid plan.
    """
    start, end = get_date_range(days_back)

    params = {
        "api_key": WINDSOR_API_KEY,
        "date_from": start,
        "date_to": end,
        "fields": ",".join([
            "date",
            "search_term",
            "campaign",
            "campaign_id",
            "ad_group",
            "keyword",
            "match_type",
            "spend",
            "clicks",
            "impressions",
            "conversions",
        ]),
        "data_source": "google_ads",
        "account_id": WINDSOR_ACCOUNT_ID,
        "segment": "search_term",
    }

    response = requests.get(WINDSOR_BASE_URL, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()

    records = data.get("data", [])
    print(f"Pulled {len(records)} search term rows (last {days_back} days)")
    return records


def pull_keyword_performance(days_back: int = 30) -> list:
    """
    Pull keyword-level performance.
    Used for match type analysis and keyword-level CPQL.
    """
    start, end = get_date_range(days_back)

    params = {
        "api_key": WINDSOR_API_KEY,
        "date_from": start,
        "date_to": end,
        "fields": ",".join([
            "date",
            "campaign",
            "campaign_id",
            "ad_group",
            "keyword",
            "match_type",
            "quality_score",
            "spend",
            "clicks",
            "impressions",
            "conversions",
            "cpc",
        ]),
        "data_source": "google_ads",
        "account_id": WINDSOR_ACCOUNT_ID,
        "segment": "keyword",
    }

    response = requests.get(WINDSOR_BASE_URL, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()

    records = data.get("data", [])
    print(f"Pulled {len(records)} keyword rows (last {days_back} days)")
    return records


def pull_geo_performance(days_back: int = 30) -> list:
    """
    Pull performance broken down by country.
    Critical for 80+ country geo tier analysis.
    """
    start, end = get_date_range(days_back)

    params = {
        "api_key": WINDSOR_API_KEY,
        "date_from": start,
        "date_to": end,
        "fields": ",".join([
            "date",
            "campaign",
            "country",
            "spend",
            "clicks",
            "impressions",
            "conversions",
        ]),
        "data_source": "google_ads",
        "account_id": WINDSOR_ACCOUNT_ID,
        "segment": "geo",
    }

    response = requests.get(WINDSOR_BASE_URL, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()

    records = data.get("data", [])
    print(f"Pulled {len(records)} geo rows (last {days_back} days)")
    return records


def get_account_summary(campaigns: list) -> dict:
    """Aggregate totals across all campaign data."""
    total_spend = sum(float(r.get("spend", 0) or 0) for r in campaigns)
    total_clicks = sum(int(r.get("clicks", 0) or 0) for r in campaigns)
    total_conversions = sum(float(r.get("conversions", 0) or 0) for r in campaigns)

    return {
        "total_spend": round(total_spend, 2),
        "total_clicks": total_clicks,
        "total_conversions": round(total_conversions, 1),
        "avg_cpc": round(total_spend / max(total_clicks, 1), 2),
        "avg_cpl": round(total_spend / max(total_conversions, 1), 2),
        "campaign_count": len(set(r.get("campaign") for r in campaigns)),
    }


def save_output(campaigns: list, search_terms: list, keywords: list, geos: list):
    """Save all Windsor data to data/ directory."""
    os.makedirs("data", exist_ok=True)

    with open("data/ads_campaigns.json", "w") as f:
        json.dump(campaigns, f, indent=2)

    with open("data/ads_search_terms.json", "w") as f:
        json.dump(search_terms, f, indent=2)

    with open("data/ads_keywords.json", "w") as f:
        json.dump(keywords, f, indent=2)

    with open("data/ads_geos.json", "w") as f:
        json.dump(geos, f, indent=2)

    print("Saved all Windsor.ai data to data/")


if __name__ == "__main__":
    print("Pulling Google Ads data via Windsor.ai...")
    campaigns = pull_campaign_performance(days_back=30)
    search_terms = pull_search_terms(days_back=14)
    keywords = pull_keyword_performance(days_back=30)
    geos = pull_geo_performance(days_back=30)

    summary = get_account_summary(campaigns)
    print(f"\nAccount summary:")
    print(f"  Total spend: ${summary['total_spend']:,.2f}")
    print(f"  Total conversions: {summary['total_conversions']}")
    print(f"  Avg CPL: ${summary['avg_cpl']:,.2f}")
    print(f"  Campaigns: {summary['campaign_count']}")

    save_output(campaigns, search_terms, keywords, geos)
