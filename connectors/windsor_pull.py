"""
Windsor.ai Google Ads Connector
Pulls campaign performance, search terms, and keyword data.
Requires Windsor.ai Basic plan or above for search term data.
"""

import logging
import os
import json
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY")
WINDSOR_ACCOUNT_ID = os.getenv("WINDSOR_ACCOUNT_ID")
WINDSOR_BASE_URL = "https://connectors.windsor.ai/all"

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2


def get_date_range(days_back: int = 30) -> tuple:
    end = datetime.utcnow().date()
    start = end - timedelta(days=days_back)
    return str(start), str(end)


def _request_with_retry(params: dict, description: str) -> list:
    """
    Make a Windsor.ai API request with exponential backoff retry.
    Handles 429 (rate limit) and 401 (auth failure) explicitly.
    Returns the data list or an empty list on failure.
    """
    if not WINDSOR_API_KEY:
        logger.error("WINDSOR_API_KEY is not set — cannot pull data")
        return []

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(WINDSOR_BASE_URL, params=params, timeout=60)

            if response.status_code == 401:
                logger.error("Windsor.ai auth failure (401) — check WINDSOR_API_KEY")
                return []

            if response.status_code == 429:
                wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Windsor.ai rate limited (429) — retry %d/%d in %ds",
                    attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()
            records = data.get("data", [])
            logger.info("Pulled %d %s rows", len(records), description)
            return records

        except requests.exceptions.Timeout:
            logger.warning(
                "Windsor.ai timeout — retry %d/%d", attempt, MAX_RETRIES
            )
            time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))

        except requests.exceptions.RequestException as exc:
            logger.error("Windsor.ai request error (%s): %s", description, exc)
            if attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            else:
                return []

    logger.error(
        "Windsor.ai request failed after %d retries (%s)", MAX_RETRIES, description
    )
    return []


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

    return _request_with_retry(params, "campaign")


def pull_search_terms(days_back: int = 14) -> list:
    """
    Pull actual search terms that triggered ads.
    This is the raw material for N-gram analysis.
    Requires Windsor.ai paid plan.

    Uses date_preset (not date_from/date_to) — the confirmed working query pattern.
    The segment=search_term parameter is rejected by Windsor with 400 BAD REQUEST.
    """
    params = {
        "api_key": WINDSOR_API_KEY,
        "account_id": WINDSOR_ACCOUNT_ID,
        "date_preset": "last_14d",
        "data_source": "google_ads",
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
    }

    return _request_with_retry(params, "search term")


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

    return _request_with_retry(params, "keyword")


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

    return _request_with_retry(params, "geo")


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

    logger.info("Saved all Windsor.ai data to data/")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Pulling Google Ads data via Windsor.ai...")
    campaigns = pull_campaign_performance(days_back=30)
    search_terms = pull_search_terms(days_back=14)
    keywords = pull_keyword_performance(days_back=30)
    geos = pull_geo_performance(days_back=30)

    summary = get_account_summary(campaigns)
    logger.info("Account summary:")
    logger.info("  Total spend: $%.2f", summary["total_spend"])
    logger.info("  Total conversions: %s", summary["total_conversions"])
    logger.info("  Avg CPL: $%.2f", summary["avg_cpl"])
    logger.info("  Campaigns: %s", summary["campaign_count"])

    save_output(campaigns, search_terms, keywords, geos)
