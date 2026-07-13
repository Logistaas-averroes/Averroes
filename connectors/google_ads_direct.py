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
# Required and optional environment variables
# ---------------------------------------------------------------------------
_REQUIRED_ENV_VARS = [
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
]

# Optional: omit to use direct customer mode; set to enable manager mode.
_OPTIONAL_ENV_VARS = [
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
]


def get_access_mode() -> str:
    """Return the current access mode.

    Returns 'manager' when GOOGLE_ADS_LOGIN_CUSTOMER_ID is set and non-empty,
    'direct_customer' otherwise.
    """
    return "manager" if os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip() else "direct_customer"


def build_google_ads_client() -> GoogleAdsClient:
    """Build a GoogleAdsClient from environment variables.

    Supports two modes:
    - Manager mode: GOOGLE_ADS_LOGIN_CUSTOMER_ID is set → included in config.
    - Direct customer mode: GOOGLE_ADS_LOGIN_CUSTOMER_ID is absent/empty → omitted.

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
        "use_proto_plus": True,
    }

    login_customer_id = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip()
    if login_customer_id:
        config["login_customer_id"] = login_customer_id
        logger.info("Google Ads direct connector mode: manager")
    else:
        logger.info("Google Ads direct connector mode: direct_customer")

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


def _run_search_stream(
    client: GoogleAdsClient,
    customer_id: str,
    query: str,
    *,
    raise_on_error: bool = True,
) -> list:
    """Execute a GAQL query using SearchStream and return a list of row objects.

    Read-only. Uses SearchStream for efficient large result sets.

    Args:
        client: Authenticated GoogleAdsClient instance.
        customer_id: Google Ads customer ID (without dashes).
        query: GAQL query string.
        raise_on_error: When True (default) re-raises API and unexpected
            exceptions after logging so callers (e.g. smoke tests) detect
            failures instead of silently returning empty results.  Set False
            only for intentional fallback paths such as fetch_geo_performance.
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
        if raise_on_error:
            raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in search stream: %s", exc)
        if raise_on_error:
            raise
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
    """Fetch search term view performance with raw currency lineage.

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive.
        end_date:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of dicts with search term performance data.
        Each row includes raw ``cost_micros`` and ``currency_code`` from
        ``customer.currency_code`` so the downstream writer can persist
        the durable native-currency lineage required by PR-ADS-144.
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
          metrics.conversions,
          customer.currency_code
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date DESC
    """

    rows = _run_search_stream(client, customer_id, query)
    result = []
    for row in rows:
        cost_micros = row.metrics.cost_micros
        result.append({
            "date": row.segments.date,
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "ad_group_id": row.ad_group.id,
            "ad_group_name": row.ad_group.name,
            "search_term": row.search_term_view.search_term,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost_micros": cost_micros,
            "spend": round(cost_micros / 1_000_000, 6),
            "currency_code": row.customer.currency_code,
            "conversions": row.metrics.conversions,
        })
    logger.info(
        "fetch_search_terms: %d rows (%s → %s)", len(result), start_date, end_date
    )
    return result


# Latest-observed keyword quality diagnostics (PR-ADS-146). These live on
# ad_group_criterion.quality_info and are CURRENT attributes (not date-grained),
# so they repeat across a criterion's daily rows — the writer keeps the latest
# observation. Selectable on the installed Google Ads API version (v21–v24); if a
# future/locked-down API rejects them we FAIL CLOSED (retry without, quality
# unavailable) rather than inventing replacements.
_KEYWORD_QUALITY_FIELDS = (
    "ad_group_criterion.quality_info.quality_score",
    "ad_group_criterion.quality_info.search_predicted_ctr",
    "ad_group_criterion.quality_info.creative_quality_score",
    "ad_group_criterion.quality_info.post_click_quality_score",
)


def _keyword_query(start_date: str, end_date: str, *, with_quality: bool) -> str:
    """Build the keyword_view GAQL. ``with_quality`` toggles the quality_info
    fields so we can retry without them if the API version rejects them."""
    quality_block = ""
    if with_quality:
        quality_block = "".join(f"          {f},\n" for f in _KEYWORD_QUALITY_FIELDS)
    return f"""
        SELECT
          segments.date,
          customer.id,
          customer.currency_code,
          campaign.id,
          campaign.name,
          ad_group.id,
          ad_group.name,
          ad_group_criterion.criterion_id,
          ad_group_criterion.status,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
{quality_block}          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM keyword_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date DESC
    """


def _is_unsupported_field_error(exc: Exception) -> bool:
    """True when a GoogleAdsException looks like an unrecognized/unsupported
    field error for the quality_info selection. Conservative: when we can't tell,
    return False so the original error re-raises (never silently swallowed)."""
    text = str(getattr(exc, "failure", "") or exc).lower()
    markers = (
        "quality_info", "unrecognized_field", "unrecognized field",
        "unknown field", "prohibited_field", "field_error",
        "not a valid field", "cannot be selected",
    )
    return any(m in text for m in markers)


def _quality_bucket(enum_val) -> str | None:
    """Map a QualityScoreBucket enum to a display string, or None when the API
    reports no bucket (UNSPECIFIED/UNKNOWN) — unavailable, never a fake value."""
    name = getattr(enum_val, "name", None) or str(enum_val or "")
    if name in ("UNSPECIFIED", "UNKNOWN", ""):
        return None
    return name  # BELOW_AVERAGE / AVERAGE / ABOVE_AVERAGE


def fetch_keyword_performance(start_date: str, end_date: str) -> list:
    """Fetch keyword_view performance with durable lineage + latest quality.

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive.
        end_date:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of dicts with keyword performance data, preserving raw
        ``cost_micros``, native ``currency_code``, immutable Google Ads identity
        (campaign/ad_group/criterion ids), criterion status, and — when the API
        supports them — latest-observed quality diagnostics. When the quality
        fields are unsupported, retries WITHOUT them and marks quality
        unavailable (fail closed).
        Read-only. Does not mutate any Google Ads resource.
    """
    client = build_google_ads_client()
    customer_id = get_customer_id()

    with_quality = True
    try:
        rows = _run_search_stream(
            client, customer_id, _keyword_query(start_date, end_date, with_quality=True))
    except GoogleAdsException as exc:
        if _is_unsupported_field_error(exc):
            logger.warning(
                "fetch_keyword_performance: quality_info fields unsupported by the "
                "installed Google Ads API — retrying WITHOUT quality (fail closed)")
            with_quality = False
            rows = _run_search_stream(
                client, customer_id, _keyword_query(start_date, end_date, with_quality=False))
        else:
            raise

    result = []
    for row in rows:
        crit = row.ad_group_criterion
        if with_quality:
            qi = crit.quality_info
            # Google Ads quality_score is 1–10 when available; 0/unset means
            # unavailable — map that to None so "unavailable" stays distinct from
            # a genuine stored 0 downstream.
            qs_raw = int(getattr(qi, "quality_score", 0) or 0)
            quality_score = qs_raw if qs_raw > 0 else None
            expected_ctr = _quality_bucket(getattr(qi, "search_predicted_ctr", None))
            ad_relevance = _quality_bucket(getattr(qi, "creative_quality_score", None))
            landing_page_experience = _quality_bucket(getattr(qi, "post_click_quality_score", None))
        else:
            quality_score = expected_ctr = ad_relevance = landing_page_experience = None
        status_enum = getattr(crit, "status", None)
        result.append({
            "date": row.segments.date,
            "customer_id": row.customer.id,
            "currency_code": row.customer.currency_code or None,
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "ad_group_id": row.ad_group.id,
            "ad_group_name": row.ad_group.name,
            "criterion_id": crit.criterion_id,
            "criterion_status": getattr(status_enum, "name", None),
            "keyword_text": crit.keyword.text,
            "keyword_match_type": crit.keyword.match_type.name,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost_micros": row.metrics.cost_micros,
            "spend": round(row.metrics.cost_micros / 1_000_000, 6),
            "conversions": row.metrics.conversions,
            "quality_score": quality_score,
            "quality_available": with_quality,
            "expected_ctr": expected_ctr,
            "ad_relevance": ad_relevance,
            "landing_page_experience": landing_page_experience,
        })
    logger.info(
        "fetch_keyword_performance: %d rows (%s → %s), quality=%s",
        len(result), start_date, end_date, "on" if with_quality else "unavailable"
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

    # geographic_view may be unavailable for some account types; treat
    # API errors as an intentional fallback and return an empty list.
    rows = _run_search_stream(client, customer_id, query, raise_on_error=False)
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


# PR-ADS-118 — canonical campaign-daily spend. Bump when the query shape changes.
SPEND_QUERY_VERSION = "campaign_daily_v1"


def fetch_campaign_daily_spend(start_date: str, end_date: str) -> dict:
    """Read canonical Google Ads campaign-daily spend directly from the API.

    This is the spend-truth source for PR-ADS-118: a direct campaign-date query
    independent of the geo table. Raw micros are preserved (never rounded before
    aggregation). Read-only — a pure SELECT that never writes to Google Ads.

    Returns {customer_id, currency_code, source_query_version,
             rows:[{campaign_id, campaign_name, spend_date, cost_micros,
                    currency_code, customer_id}]}.
    """
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          segments.date,
          metrics.cost_micros,
          customer.currency_code
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date
    """

    rows = _run_search_stream(client, customer_id, query)
    currency_code = None
    out = []
    for row in rows:
        currency_code = row.customer.currency_code or currency_code
        out.append({
            "customer_id": str(customer_id),
            "currency_code": row.customer.currency_code,
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "spend_date": row.segments.date,
            "cost_micros": int(row.metrics.cost_micros),
        })
    logger.info(
        "fetch_campaign_daily_spend: %d rows (%s → %s)", len(out), start_date, end_date)
    return {
        "customer_id": str(customer_id),
        "currency_code": currency_code,
        "source_query_version": SPEND_QUERY_VERSION,
        "rows": out,
    }


# PR-ADS-120 — account-level daily spend, queried independently of campaigns so
# the campaign sum can be reconciled against the account total.
ACCOUNT_SPEND_QUERY_VERSION = "account_daily_v1"


def fetch_account_daily_spend(start_date: str, end_date: str) -> dict:
    """Read account-level Google Ads daily spend directly from the API.

    PR-ADS-120 reconciliation source: the account total per day, independent of
    the campaign breakdown, so the campaign sum can be checked against it. Also
    surfaces the account time zone so spend windows use the account's local day.
    Raw micros are preserved (never rounded before aggregation). Read-only — a
    pure SELECT that never writes to Google Ads.

    Returns {customer_id, currency_code, account_time_zone, source_query_version,
             rows:[{customer_id, currency_code, account_time_zone, spend_date,
                    cost_micros}]}.
    """
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          customer.id,
          customer.currency_code,
          customer.time_zone,
          segments.date,
          metrics.cost_micros
        FROM customer
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date
    """

    rows = _run_search_stream(client, customer_id, query)
    currency_code = None
    account_time_zone = None
    out = []
    for row in rows:
        currency_code = row.customer.currency_code or currency_code
        account_time_zone = row.customer.time_zone or account_time_zone
        out.append({
            "customer_id": str(customer_id),
            "currency_code": row.customer.currency_code,
            "account_time_zone": row.customer.time_zone,
            "spend_date": row.segments.date,
            "cost_micros": int(row.metrics.cost_micros),
        })
    logger.info(
        "fetch_account_daily_spend: %d rows (%s → %s)", len(out), start_date, end_date)
    return {
        "customer_id": str(customer_id),
        "currency_code": currency_code,
        "account_time_zone": account_time_zone,
        "source_query_version": ACCOUNT_SPEND_QUERY_VERSION,
        "rows": out,
    }


# PR-ADS-122 — single-campaign reconciliation. A FRESH campaign-level total for
# one campaign/date range, queried independently of the canonical backfill so a
# ROAS campaign row can prove its spend against the live Google Ads API.
CAMPAIGN_RECONCILE_QUERY_VERSION = "campaign_daily_by_id_v1"


def fetch_campaign_daily_spend_for_campaign(
    start_date: str, end_date: str, campaign_id: str
) -> dict:
    """Fresh Google Ads campaign-level daily spend for ONE campaign. Read-only.

    The reconciliation truth source for PR-ADS-122: a live campaign-level query
    filtered to a single campaign_id, fetched fresh (never the canonical table)
    so the local canonical total can be checked against the API right now. Raw
    micros are preserved (never rounded before aggregation). A pure SELECT that
    never writes to Google Ads.

    Returns {customer_id, currency_code, account_time_zone, campaign_id,
             campaign_name, source_query_version,
             rows:[{spend_date, cost_micros}]}.
    """
    safe_campaign_id = str(campaign_id).strip()
    if not safe_campaign_id.isdigit():
        raise ValueError("campaign_id must be numeric")
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          customer.currency_code,
          customer.time_zone,
          segments.date,
          metrics.cost_micros
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND campaign.id = {safe_campaign_id}
        ORDER BY segments.date
    """

    rows = _run_search_stream(client, customer_id, query)
    currency_code = None
    account_time_zone = None
    campaign_name = None
    out = []
    for row in rows:
        currency_code = row.customer.currency_code or currency_code
        account_time_zone = row.customer.time_zone or account_time_zone
        campaign_name = row.campaign.name or campaign_name
        out.append({
            "spend_date": row.segments.date,
            "cost_micros": int(row.metrics.cost_micros),
        })
    logger.info(
        "fetch_campaign_daily_spend_for_campaign: %d rows (campaign=%s, %s → %s)",
        len(out), safe_campaign_id, start_date, end_date)
    return {
        "customer_id": str(customer_id),
        "currency_code": currency_code,
        "account_time_zone": account_time_zone,
        "campaign_id": safe_campaign_id,
        "campaign_name": campaign_name,
        "source_query_version": CAMPAIGN_RECONCILE_QUERY_VERSION,
        "rows": out,
    }


# PR-ADS-122 — ad-group-level spend WITH status, so the reconciliation can prove
# whether a Google Ads UI "Ad group status: Enabled" filter explains a lower UI
# total than the campaign-level (all-status) canonical spend.
ADGROUP_RECONCILE_QUERY_VERSION = "adgroup_daily_v1"


def fetch_ad_group_daily_spend(
    start_date: str, end_date: str, campaign_id: str
) -> dict:
    """Ad-group-level daily spend with status for ONE campaign. Read-only.

    PR-ADS-122 reconciliation evidence: the same campaign spend broken out by
    ad group AND ad-group status (ENABLED / PAUSED / REMOVED). This proves
    whether the Google Ads UI screenshot is lower because it filters to enabled
    ad groups while the campaign-level total includes paused/removed ad-group
    spend. Raw micros preserved. A pure SELECT — never writes to Google Ads.

    Returns {customer_id, currency_code, campaign_id, source_query_version,
             rows:[{ad_group_id, ad_group_name, ad_group_status, spend_date,
                    cost_micros}]}.
    """
    safe_campaign_id = str(campaign_id).strip()
    if not safe_campaign_id.isdigit():
        raise ValueError("campaign_id must be numeric")
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          campaign.id,
          ad_group.id,
          ad_group.name,
          ad_group.status,
          customer.currency_code,
          segments.date,
          metrics.cost_micros
        FROM ad_group
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND campaign.id = {safe_campaign_id}
        ORDER BY segments.date
    """

    rows = _run_search_stream(client, customer_id, query, raise_on_error=False)
    currency_code = None
    out = []
    for row in rows:
        currency_code = row.customer.currency_code or currency_code
        out.append({
            "ad_group_id": str(row.ad_group.id),
            "ad_group_name": row.ad_group.name,
            "ad_group_status": row.ad_group.status.name,
            "spend_date": row.segments.date,
            "cost_micros": int(row.metrics.cost_micros),
        })
    logger.info(
        "fetch_ad_group_daily_spend: %d rows (campaign=%s, %s → %s)",
        len(out), safe_campaign_id, start_date, end_date)
    return {
        "customer_id": str(customer_id),
        "currency_code": currency_code,
        "campaign_id": safe_campaign_id,
        "source_query_version": ADGROUP_RECONCILE_QUERY_VERSION,
        "rows": out,
    }


# PR-ADS-124 — canonical Google Ads geo (country) daily spend. Bump when the
# query shape changes.
GEO_DAILY_SPEND_QUERY_VERSION = "geo_daily_v1"


def fetch_geo_daily_spend(start_date: str, end_date: str) -> dict:
    """Read canonical Google Ads geo (country) daily spend directly from the API.

    PR-ADS-124 geo spend-truth source: a direct geographic_view query, segmented
    by country criterion and campaign and date, independent of the legacy
    run-scoped `geo` table or any Windsor data. Raw micros are preserved (never
    rounded before aggregation). Read-only — a pure SELECT that never writes to
    Google Ads.

    Errors are RAISED (raise_on_error=True), not swallowed: the geo sync must be
    able to tell a genuinely empty-but-valid result (query succeeded, 0 rows)
    from an API failure / unsupported geographic_view (query errored). A failure
    becomes a FAILED chunk, never a confidently "verified empty" one.

    Returns {customer_id, currency_code, source_query_version,
             rows:[{customer_id, currency_code, country_criterion_id,
                    campaign_id, spend_date, cost_micros}]}.
    """
    client = build_google_ads_client()
    customer_id = get_customer_id()

    query = f"""
        SELECT
          campaign.id,
          geographic_view.country_criterion_id,
          customer.currency_code,
          segments.date,
          metrics.cost_micros
        FROM geographic_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date
    """

    # Raise on API error so the caller can mark the chunk failed/unavailable — a
    # valid query that returns zero rows is a real empty result, an error is not.
    rows = _run_search_stream(client, customer_id, query, raise_on_error=True)
    currency_code = None
    out = []
    for row in rows:
        currency_code = row.customer.currency_code or currency_code
        out.append({
            "customer_id": str(customer_id),
            "currency_code": row.customer.currency_code,
            "country_criterion_id": str(row.geographic_view.country_criterion_id),
            "campaign_id": str(row.campaign.id),
            "spend_date": row.segments.date,
            "cost_micros": int(row.metrics.cost_micros),
        })
    logger.info(
        "fetch_geo_daily_spend: %d rows (%s → %s)", len(out), start_date, end_date)
    return {
        "customer_id": str(customer_id),
        "currency_code": currency_code,
        "source_query_version": GEO_DAILY_SPEND_QUERY_VERSION,
        "rows": out,
    }


def fetch_geo_target_country_codes(criterion_ids) -> dict:
    """Resolve Google Ads geo target constant ids -> {country_code, name}.

    PR-ADS-124: geographic_view returns a numeric country_criterion_id; this
    resolves each to an ISO country code + canonical name via the
    geo_target_constant resource so the canonical geo table can present named
    ROAS by Country rows that join HubSpot deal countries. Read-only — a pure
    SELECT that never writes to Google Ads.

    Returns {criterion_id(str): {"country_code": str|None, "name": str|None}}.
    Only valid, numeric ids are queried; unknown/unresolved ids are simply absent.
    """
    ids = sorted({str(c).strip() for c in (criterion_ids or [])
                  if c is not None and str(c).strip().isdigit()})
    if not ids:
        return {}
    client = build_google_ads_client()
    customer_id = get_customer_id()
    id_list = ", ".join(ids)
    query = f"""
        SELECT
          geo_target_constant.id,
          geo_target_constant.country_code,
          geo_target_constant.canonical_name,
          geo_target_constant.name
        FROM geo_target_constant
        WHERE geo_target_constant.id IN ({id_list})
    """
    rows = _run_search_stream(client, customer_id, query, raise_on_error=False)
    out: dict = {}
    for row in rows:
        gtc = row.geo_target_constant
        out[str(gtc.id)] = {
            "country_code": (gtc.country_code or None),
            "name": (gtc.canonical_name or gtc.name or None),
        }
    logger.info("fetch_geo_target_country_codes: resolved %d/%d ids", len(out), len(ids))
    return out
