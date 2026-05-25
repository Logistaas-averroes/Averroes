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


def _parse_windsor_mcp_response(raw_response: object) -> list:
    """
    Parse Windsor MCP get_data response.

    Confirmed shape (May 2026):
    [
      {
        "text": "[{...}, {...}]"
      }
    ]

    Returns row dicts.

    Also handles:
    - Already-parsed list of row dicts (passthrough).
    - Dict with "data" key (standard REST response shape).
    - Logs clear error if malformed.
    """
    if not raw_response:
        logger.error("_parse_windsor_mcp_response: empty response")
        return []

    # Case 1: list with text field (MCP double-parse shape)
    if (
        isinstance(raw_response, list)
        and len(raw_response) > 0
        and isinstance(raw_response[0], dict)
        and "text" in raw_response[0]
    ):
        text_payload = raw_response[0]["text"]
        try:
            parsed = json.loads(text_payload)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error(
                "_parse_windsor_mcp_response: failed to parse text payload: %s", exc
            )
            return []
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "data" in parsed:
            return parsed["data"]
        logger.error(
            "_parse_windsor_mcp_response: parsed text is neither list nor dict with data"
        )
        return []

    # Case 2: already a list of row dicts (e.g. search_term key present)
    if (
        isinstance(raw_response, list)
        and len(raw_response) > 0
        and isinstance(raw_response[0], dict)
        and "search_term" in raw_response[0]
    ):
        return raw_response

    # Case 3: dict with data key (standard REST)
    if isinstance(raw_response, dict) and "data" in raw_response:
        return raw_response["data"]

    logger.error(
        "_parse_windsor_mcp_response: unrecognised response shape: %s",
        type(raw_response).__name__,
    )
    return []


def _normalize_search_term_row(row: dict) -> dict:
    """
    Normalize a single search-term row from Windsor.

    Extracts ad_group_id from Google Ads resource path if present:
      customers/3059734490/adGroups/175677406221 → 175677406221
    """
    ad_group_raw = row.get("ad_group")
    ad_group_id = (
        ad_group_raw.split("/")[-1]
        if isinstance(ad_group_raw, str) and "/" in ad_group_raw
        else ad_group_raw
    )

    return {
        "search_term": row.get("search_term"),
        "campaign": row.get("campaign"),
        "ad_group": ad_group_raw,
        "ad_group_id": ad_group_id,
        "impressions": row.get("impressions"),
        "clicks": row.get("clicks"),
        "spend": row.get("spend"),
        "conversions": row.get("conversions"),
    }


def normalize_search_term_rows(rows: list[dict]) -> list[dict]:
    """Normalize raw Windsor REST search-term rows.

    Requirements (PR-ADS-065):
    - Preserve search_term (exact field name)
    - Preserve campaign
    - Preserve raw ad_group
    - Add ad_group_id if resource path exists
    - Preserve impressions/clicks/spend/conversions
    - Do NOT map query/search_query/search_term_text to search_term
    - Skip or flag rows with blank search_term

    Returns list of normalized dicts.
    """
    normalized = []
    skipped_blank = 0
    for row in rows or []:
        norm = _normalize_search_term_row(row)
        st = norm.get("search_term")
        if not st or (isinstance(st, str) and not st.strip()):
            skipped_blank += 1
            continue
        normalized.append(norm)
    if skipped_blank > 0:
        logger.warning(
            "normalize_search_term_rows: skipped %d rows with blank/missing search_term",
            skipped_blank,
        )
    return normalized


def pull_search_terms(days_back: int = 60) -> list:
    """
    Pull search-term universe from Windsor.ai REST API.

    Confirmed Windsor MCP contract (May 2026):
        connector: google_ads
        account_id: 305-973-4490
        date_preset: last_60d
        fields: search_term, campaign, ad_group, impressions, clicks, spend, conversions

    Important:
    - `search_term` is exact field name.
    - `ad_group` may be a Google Ads resource path.
    - Do not use `search_term_text`, `query`, or `search_query`.

    Runtime note:
    - MCP get_data is not available at app runtime (no MCP client in deployment).
    - This function attempts Windsor REST `date_preset=last_60d` as runtime fallback.
    - REST parity with the confirmed MCP 60-day universe is not guaranteed unless
      validated in the current runtime/account.
    - For MCP-extracted payloads, use `_parse_windsor_mcp_response()` to parse.

    days_back mapping to Windsor presets:
        ≤1 → last_1d, ≤7 → last_7d, ≤14 → last_14d, else → last_60d
    """
    if days_back <= 1:
        date_preset = "last_1d"
    elif days_back <= 7:
        date_preset = "last_7d"
    elif days_back <= 14:
        date_preset = "last_14d"
    else:
        date_preset = "last_60d"

    logger.info(
        "Windsor search-term pull started: date_preset=%s, account_id=%s",
        date_preset, WINDSOR_ACCOUNT_ID,
    )

    params = {
        "api_key": WINDSOR_API_KEY,
        "account_id": WINDSOR_ACCOUNT_ID,
        "date_preset": date_preset,
        "data_source": "google_ads",
        "fields": ",".join([
            "search_term",
            "campaign",
            "ad_group",
            "impressions",
            "clicks",
            "spend",
            "conversions",
        ]),
    }

    rows = _request_with_retry(params, f"search term ({date_preset})")

    if rows:
        logger.info(
            "Windsor search-term pull returned %d rows", len(rows),
        )
        if rows and isinstance(rows[0], dict):
            sample_keys = sorted(rows[0].keys())
            logger.info("Windsor search-term sample keys: %s", sample_keys)
            has_search_term = "search_term" in rows[0]
            logger.info("Windsor search-term sample search_term present: %s", has_search_term)
            if not has_search_term:
                logger.error(
                    "Windsor search-term rows returned WITHOUT search_term field. "
                    "Keys found: %s. This is a contract violation.",
                    sample_keys,
                )
        return rows

    if date_preset == "last_60d":
        logger.warning(
            "Windsor search-term REST fallback last_60d returned no rows; "
            "falling back to legacy last_14d behavior"
        )
        legacy_params = dict(params)
        legacy_params["date_preset"] = "last_14d"
        legacy_rows = _request_with_retry(legacy_params, "search term (last_14d)")
        if not legacy_rows:
            logger.warning(
                "WARNING: Windsor search-term pull returned 0 rows for last_60d AND last_14d. "
                "This is suspicious for an active account and should not be treated as "
                "healthy evidence."
            )
        else:
            logger.info(
                "Windsor search-term pull (last_14d fallback) returned %d rows",
                len(legacy_rows),
            )
            if legacy_rows and isinstance(legacy_rows[0], dict):
                sample_keys = sorted(legacy_rows[0].keys())
                logger.info("Windsor search-term sample keys: %s", sample_keys)
                if "search_term" not in legacy_rows[0]:
                    logger.error(
                        "Windsor search-term rows returned WITHOUT search_term field. "
                        "Keys found: %s. This is a contract violation.",
                        sample_keys,
                    )
        return legacy_rows

    logger.warning(
        "WARNING: Windsor search-term pull returned 0 rows (date_preset=%s). "
        "This is suspicious for an active account and should not be treated as "
        "healthy evidence.",
        date_preset,
    )
    return []


def pull_search_terms_mcp_payload(raw_response: object) -> list:
    """
    Parse and normalize a Windsor MCP search-term payload.

    This is for operator/agent use when MCP extraction has been run externally
    and the raw response needs to be parsed into normalized rows.

    TODO: Integrate runtime MCP client when available in deployment.

    Usage:
        raw = get_data(...)  # MCP call outside this app
        rows = pull_search_terms_mcp_payload(raw)
    """
    parsed = _parse_windsor_mcp_response(raw_response)
    return [_normalize_search_term_row(row) for row in parsed]


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


def pull_campaign_performance_range(date_from: str, date_to: str) -> list:
    """Pull campaign-level performance for an explicit date range.

    Used by the historical backfill framework (PR-ADS-071).
    Accepts ISO date strings (YYYY-MM-DD).
    Reads all campaigns (active and paused) — Windsor returns all status by default.
    NEVER writes to Google Ads.
    """
    params = {
        "api_key": WINDSOR_API_KEY,
        "date_from": str(date_from),
        "date_to": str(date_to),
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
    return _request_with_retry(
        params, f"campaign range ({date_from}→{date_to})"
    )


def pull_keyword_performance_range(date_from: str, date_to: str) -> list:
    """Pull keyword-level performance for an explicit date range.

    Used by the historical backfill framework (PR-ADS-071).
    NEVER writes to Google Ads.
    """
    params = {
        "api_key": WINDSOR_API_KEY,
        "date_from": str(date_from),
        "date_to": str(date_to),
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
    return _request_with_retry(
        params, f"keyword range ({date_from}→{date_to})"
    )


def pull_geo_performance_range(date_from: str, date_to: str) -> list:
    """Pull geographic performance for an explicit date range.

    Used by the historical backfill framework (PR-ADS-071).
    NEVER writes to Google Ads.
    """
    params = {
        "api_key": WINDSOR_API_KEY,
        "date_from": str(date_from),
        "date_to": str(date_to),
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
    return _request_with_retry(
        params, f"geo range ({date_from}→{date_to})"
    )


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
    search_terms = pull_search_terms(days_back=60)
    keywords = pull_keyword_performance(days_back=30)
    geos = pull_geo_performance(days_back=30)

    summary = get_account_summary(campaigns)
    logger.info("Account summary:")
    logger.info("  Total spend: $%.2f", summary["total_spend"])
    logger.info("  Total conversions: %s", summary["total_conversions"])
    logger.info("  Avg CPL: $%.2f", summary["avg_cpl"])
    logger.info("  Campaigns: %s", summary["campaign_count"])

    save_output(campaigns, search_terms, keywords, geos)
