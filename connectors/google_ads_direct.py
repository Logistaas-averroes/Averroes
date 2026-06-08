"""
Google Ads Direct Read-Only Connector — PR-ADS-098

Replaces Windsor.ai as the upstream Google Ads data source.
Read-only. No mutate operations. No writes to Google Ads.

Credentials are loaded exclusively from environment variables.
Never hardcode tokens, secrets, or credentials in this file.
"""

import logging
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required environment variables
# ---------------------------------------------------------------------------
_REQUIRED_ENV_VARS = [
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "GOOGLE_ADS_CUSTOMER_ID",
]


def build_google_ads_client() -> GoogleAdsClient:
    """Build a GoogleAdsClient from environment variables.

    Raises:
        EnvironmentError: If any required env var is missing.
    """
    missing = [v for v in _REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise EnvironmentError(
            f"Missing required Google Ads env vars: {', '.join(missing)}"
        )

    config = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        "use_proto_plus": True,
    }
    return GoogleAdsClient.load_from_dict(config)


def get_customer_id() -> str:
    """Return the customer account ID from env vars."""
    cid = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "")
    if not cid:
        raise EnvironmentError("GOOGLE_ADS_CUSTOMER_ID is not set")
    return cid


def test_connection() -> bool:
    """Validate credentials by issuing a minimal GAQL query.

    Returns True on success, False on failure (logs the error).
    Read-only. Does not mutate any Google Ads resource.
    """
    try:
        client = build_google_ads_client()
        customer_id = get_customer_id()
        ga_service = client.get_service("GoogleAdsService")

        query = """
            SELECT customer.id
            FROM customer
            LIMIT 1
        """
        request = client.get_type("SearchGoogleAdsRequest")
        request.customer_id = customer_id
        request.query = query
        ga_service.search(request=request)
        logger.info("Google Ads direct connection test: PASS (customer_id=%s)", customer_id)
        return True

    except EnvironmentError as exc:
        logger.error("Google Ads connection test failed — env error: %s", exc)
        return False
    except GoogleAdsException as exc:
        for error in exc.failure.errors:
            logger.error(
                "Google Ads connection test failed — API error: %s", error.message
            )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("Google Ads connection test failed: %s", exc)
        return False


def _run_search_stream(client: GoogleAdsClient, customer_id: str, query: str) -> list:
    """Execute a GAQL query using SearchStream and return a list of row objects.

    Read-only. Uses SearchStream for efficient large result sets.
    """
    ga_service = client.get_service("GoogleAdsService")
    request = client.get_type("SearchGoogleAdsStreamRequest")
    request.customer_id = customer_id
    request.query = query

    rows = []
    try:
        stream = ga_service.search_stream(request=request)
        for batch in stream:
            rows.extend(batch.results)
    except GoogleAdsException as exc:
        for error in exc.failure.errors:
            logger.error("Google Ads API error: %s", error.message)
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in search stream: %s", exc)
    return rows


def fetch_campaign_performance(start_date: str, end_date: str) -> list:
    """Fetch campaign-level performance metrics.

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive.
        end_date:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of dicts with campaign performance data.
        Read-only. Does not mutate any Google Ads resource.
    """
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          segments.date,
          campaign.id,
          campaign.name,
          campaign.status,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date DESC
    """

    rows = _run_search_stream(client, customer_id, query)
    result = []
    for row in rows:
        result.append({
            "date": row.segments.date,
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "campaign_status": row.campaign.status.name,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost_micros": row.metrics.cost_micros,
            "spend": round(row.metrics.cost_micros / 1_000_000, 6),
            "conversions": row.metrics.conversions,
            "conversions_value": row.metrics.conversions_value,
        })
    logger.info(
        "fetch_campaign_performance: %d rows (%s → %s)", len(result), start_date, end_date
    )
    return result


def fetch_search_terms(start_date: str, end_date: str) -> list:
    """Fetch search term view performance.

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive.
        end_date:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of dicts with search term performance data.
        Read-only. Does not mutate any Google Ads resource.
    """
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          segments.date,
          campaign.id,
          campaign.name,
          ad_group.id,
          ad_group.name,
          search_term_view.search_term,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date DESC
    """

    rows = _run_search_stream(client, customer_id, query)
    result = []
    for row in rows:
        result.append({
            "date": row.segments.date,
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "ad_group_id": row.ad_group.id,
            "ad_group_name": row.ad_group.name,
            "search_term": row.search_term_view.search_term,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost_micros": row.metrics.cost_micros,
            "spend": round(row.metrics.cost_micros / 1_000_000, 6),
            "conversions": row.metrics.conversions,
        })
    logger.info(
        "fetch_search_terms: %d rows (%s → %s)", len(result), start_date, end_date
    )
    return result


def fetch_keyword_performance(start_date: str, end_date: str) -> list:
    """Fetch keyword view performance.

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive.
        end_date:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of dicts with keyword performance data.
        Read-only. Does not mutate any Google Ads resource.
    """
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          segments.date,
          campaign.id,
          campaign.name,
          ad_group.id,
          ad_group.name,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM keyword_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date DESC
    """

    rows = _run_search_stream(client, customer_id, query)
    result = []
    for row in rows:
        result.append({
            "date": row.segments.date,
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "ad_group_id": row.ad_group.id,
            "ad_group_name": row.ad_group.name,
            "keyword_text": row.ad_group_criterion.keyword.text,
            "keyword_match_type": row.ad_group_criterion.keyword.match_type.name,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost_micros": row.metrics.cost_micros,
            "spend": round(row.metrics.cost_micros / 1_000_000, 6),
            "conversions": row.metrics.conversions,
        })
    logger.info(
        "fetch_keyword_performance: %d rows (%s → %s)", len(result), start_date, end_date
    )
    return result


def fetch_geo_performance(start_date: str, end_date: str) -> list:
    """Fetch geographic view performance.

    Queries geographic_view (country-level breakdown).
    If geographic_view is unavailable due to account access level,
    this function logs a warning and returns an empty list.
    user_location_view is the documented fallback (see PR-ADS-099).

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive.
        end_date:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of dicts with geo performance data.
        Read-only. Does not mutate any Google Ads resource.
    """
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          segments.date,
          campaign.id,
          campaign.name,
          geographic_view.country_criterion_id,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM geographic_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date DESC
    """

    rows = _run_search_stream(client, customer_id, query)
    result = []
    for row in rows:
        result.append({
            "date": row.segments.date,
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "country_criterion_id": row.geographic_view.country_criterion_id,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost_micros": row.metrics.cost_micros,
            "spend": round(row.metrics.cost_micros / 1_000_000, 6),
            "conversions": row.metrics.conversions,
        })
    if not result:
        logger.warning(
            "fetch_geo_performance: 0 rows returned for %s → %s. "
            "If geographic_view is incompatible with account access level, "
            "test user_location_view in PR-ADS-099.",
            start_date, end_date,
        )
    else:
        logger.info(
            "fetch_geo_performance: %d rows (%s → %s)", len(result), start_date, end_date
        )
    return result


def _get_default_date_range(days_back: int = 7) -> tuple:
    """Return (start_date, end_date) strings for the last N days."""
    end = datetime.utcnow().date()
    start = end - timedelta(days=days_back - 1)
    return str(start), str(end)
