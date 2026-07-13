"""
Google Ads Canonical Source Adapter — PR-ADS-103

Exposes Windsor-compatible pull functions powered by the direct Google Ads API
connector (connectors/google_ads_direct.py).

Read-only adapter layer. Guarantees:
- No writes to Google Ads (no mutate operations).
- No writes to the database.
- No Windsor imports.

This adapter is the replacement interface for Windsor pull functions.
The production scheduler cutover (Windsor → Google Ads) is PR-ADS-104.

Public API:
    get_date_range(days_back)

    pull_campaign_performance(days_back)
    pull_search_terms(days_back)
    pull_keyword_performance(days_back)
    pull_geo_performance(days_back)

    pull_campaign_performance_range(date_from, date_to)
    pull_search_terms_range(date_from, date_to)
    pull_keyword_performance_range(date_from, date_to)
    pull_geo_performance_range(date_from, date_to)

    save_output(campaigns, search_terms, keywords, geos)

Internal helpers (pure, no I/O):
    safe_divide(numerator, denominator)
    normalize_campaign_row(row)
    normalize_search_term_row(row)
    normalize_keyword_row(row)
    normalize_geo_row(row)
"""

import json
import logging
import os
from datetime import timedelta, timezone, datetime

from connectors.google_ads_direct import (
    fetch_campaign_performance,
    fetch_keyword_performance,
    fetch_search_terms,
    fetch_geo_performance,
)

logger = logging.getLogger(__name__)

_SOURCE = "google_ads_api"

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def get_date_range(days_back: int = 30) -> tuple[str, str]:
    """Return (start_date, end_date) ISO strings for the last *days_back* days.

    Both dates are inclusive. The window is:
        end   = today (UTC)
        start = end − (days_back − 1)

    This produces a window of exactly *days_back* calendar days including today.

    Example: days_back=30 on 2026-06-08 → ('2026-05-10', '2026-06-08')

    Note on Windsor parity:
        Windsor used ``start = end - timedelta(days=days_back)`` which produced
        days_back + 1 calendar days.  The formula here corrects that off-by-one
        so that ``days_back=30`` genuinely covers 30 days.  If you need the old
        behaviour for backfill comparisons, use the explicit range functions.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back - 1)
    return str(start), str(end)


# ---------------------------------------------------------------------------
# Pure normalizers (no I/O, no side effects)
# ---------------------------------------------------------------------------


def safe_divide(numerator: float, denominator: float) -> float:
    """Return numerator / denominator, or 0.0 when denominator is zero."""
    if not denominator:
        return 0.0
    return numerator / denominator


def normalize_id(value) -> str:
    """Normalize identifier values to non-null string form."""
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.lower() == "none" else value


def normalize_campaign_row(row: dict) -> dict:
    """Convert a google_ads_direct campaign row to the internal contract shape.

    Input fields (from fetch_campaign_performance):
        date, campaign_id, campaign_name, spend, clicks, impressions,
        conversions, conversions_value

    Output fields:
        date, campaign, campaign_id (str), spend, clicks, impressions,
        conversions, conversions_value, cpc, ctr, conversion_rate,
        source="google_ads_api"
    """
    clicks = row.get("clicks", 0) or 0
    impressions = row.get("impressions", 0) or 0
    conversions = row.get("conversions", 0.0) or 0.0
    spend = row.get("spend", 0.0) or 0.0

    return {
        "date": row.get("date"),
        "campaign": row.get("campaign_name"),
        "campaign_id": normalize_id(row.get("campaign_id")),
        "spend": spend,
        "clicks": clicks,
        "impressions": impressions,
        "conversions": conversions,
        "conversions_value": row.get("conversions_value", 0.0) or 0.0,
        "cpc": safe_divide(spend, clicks),
        "ctr": safe_divide(clicks, impressions),
        "conversion_rate": safe_divide(conversions, clicks),
        "source": _SOURCE,
    }


def normalize_search_term_row(row: dict) -> dict | None:
    """Convert a google_ads_direct search term row to the internal contract.

    Returns None when search_term is blank or null — callers must skip these.

    Input fields (from fetch_search_terms):
        date, campaign_id, campaign_name, ad_group_id, ad_group_name,
        search_term, impressions, clicks, cost_micros, spend,
        currency_code, conversions

    Output fields:
        date, search_term, campaign, campaign_id (str), ad_group,
        ad_group_id (str), impressions, clicks, cost_micros, spend,
        currency_code, conversions, source="google_ads_api"

    Field discipline:
        The internal contract requires the field to be named exactly
        ``search_term``.  Do NOT rename to query / search_query /
        search_term_text.

    Currency lineage (PR-ADS-144):
        ``cost_micros`` and ``currency_code`` are preserved from the Google
        Ads response so the writer can store durable native-currency
        provenance. ``spend`` remains the legacy cost_micros/1e6 value; the
        writer stores it alongside cost_micros for backward compatibility.
    """
    search_term = row.get("search_term") or ""
    if not search_term.strip():
        return None

    return {
        "date": row.get("date"),
        "search_term": search_term,
        "campaign": row.get("campaign_name"),
        "campaign_id": normalize_id(row.get("campaign_id")),
        "ad_group": row.get("ad_group_name"),
        "ad_group_id": normalize_id(row.get("ad_group_id")),
        "impressions": row.get("impressions", 0) or 0,
        "clicks": row.get("clicks", 0) or 0,
        "cost_micros": row.get("cost_micros"),
        "spend": row.get("spend", 0.0) or 0.0,
        "currency_code": row.get("currency_code"),
        "conversions": row.get("conversions", 0.0) or 0.0,
        "source": _SOURCE,
    }


def normalize_keyword_row(row: dict) -> dict:
    """Convert a google_ads_direct keyword row to the internal contract shape.

    Input fields (from fetch_keyword_performance):
        date, currency_code, campaign_id, campaign_name, ad_group_id,
        ad_group_name, criterion_id, criterion_status, keyword_text,
        keyword_match_type, impressions, clicks, cost_micros, spend,
        conversions, quality_score, quality_available, expected_ctr,
        ad_relevance, landing_page_experience

    Output fields:
        date, campaign, campaign_id (str), ad_group, ad_group_id (str),
        criterion_id (str), criterion_status, keyword, match_type (uppercase),
        cost_micros, currency_code, impressions, clicks, spend, conversions,
        cpc, quality_score, quality_available, expected_ctr, ad_relevance,
        landing_page_experience, source="google_ads_api"

    match_type is normalised to uppercase Google Ads name: EXACT / PHRASE / BROAD

    Currency lineage (PR-ADS-146): ``cost_micros`` and ``currency_code`` are
    preserved so the durable writer can store native-currency provenance;
    ``spend`` stays the legacy cost_micros/1e6 value. Quality fields are
    latest-observed keyword attributes — passed through unchanged (None stays
    None; a genuine 0 is never invented, and unavailable stays distinct from 0).
    """
    clicks = row.get("clicks", 0) or 0
    spend = row.get("spend", 0.0) or 0.0

    raw_match_type = row.get("keyword_match_type", "") or ""
    match_type = raw_match_type.upper()

    return {
        "date": row.get("date"),
        "customer_id": normalize_id(row.get("customer_id")),
        "campaign": row.get("campaign_name"),
        "campaign_id": normalize_id(row.get("campaign_id")),
        "ad_group": row.get("ad_group_name"),
        "ad_group_id": normalize_id(row.get("ad_group_id")),
        "criterion_id": normalize_id(row.get("criterion_id")),
        "criterion_status": row.get("criterion_status"),
        "keyword": row.get("keyword_text"),
        "match_type": match_type,
        "cost_micros": row.get("cost_micros"),
        "currency_code": row.get("currency_code"),
        "spend": spend,
        "clicks": clicks,
        "impressions": row.get("impressions", 0) or 0,
        "conversions": row.get("conversions", 0.0) or 0.0,
        "cpc": safe_divide(spend, clicks),
        "quality_score": row.get("quality_score"),
        "quality_available": row.get("quality_available", False),
        "expected_ctr": row.get("expected_ctr"),
        "ad_relevance": row.get("ad_relevance"),
        "landing_page_experience": row.get("landing_page_experience"),
        "source": _SOURCE,
    }


def normalize_geo_row(row: dict) -> dict:
    """Convert a google_ads_direct geo row to the internal contract shape.

    Input fields (from fetch_geo_performance):
        date, campaign_id, campaign_name, country_criterion_id, impressions,
        clicks, spend, conversions

    Output fields:
        date, campaign, campaign_id (str), country_criterion_id (str),
        country (None — mapping not implemented), impressions, clicks, spend,
        conversions, source="google_ads_api"

    Note on country name mapping:
        country is set to None because no criterion-ID-to-name mapping is
        implemented in this PR.  A lookup table can be added in a future PR.
        Do not invent or hard-code country names.
    """
    return {
        "date": row.get("date"),
        "campaign": row.get("campaign_name"),
        "campaign_id": normalize_id(row.get("campaign_id")),
        "country_criterion_id": normalize_id(row.get("country_criterion_id")),
        "country": None,
        "spend": row.get("spend", 0.0) or 0.0,
        "clicks": row.get("clicks", 0) or 0,
        "impressions": row.get("impressions", 0) or 0,
        "conversions": row.get("conversions", 0.0) or 0.0,
        "source": _SOURCE,
    }


# ---------------------------------------------------------------------------
# Explicit date-range pull functions
# ---------------------------------------------------------------------------


def pull_campaign_performance_range(date_from: str, date_to: str) -> list[dict]:
    """Pull and normalise campaign performance for an explicit date range.

    Args:
        date_from: ISO date string (YYYY-MM-DD), inclusive.
        date_to:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of normalised campaign rows matching the internal contract.
        Read-only. No DB writes. No Google Ads writes.
    """
    raw_rows = fetch_campaign_performance(date_from, date_to)
    result = [normalize_campaign_row(r) for r in raw_rows]
    logger.info(
        "pull_campaign_performance_range: %d rows (%s → %s)",
        len(result), date_from, date_to,
    )
    return result


def pull_search_terms_range(date_from: str, date_to: str) -> list[dict]:
    """Pull and normalise search term performance for an explicit date range.

    Blank/null search_term rows are skipped.

    Args:
        date_from: ISO date string (YYYY-MM-DD), inclusive.
        date_to:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of normalised search term rows matching the internal contract.
        Read-only. No DB writes. No Google Ads writes.
    """
    raw_rows = fetch_search_terms(date_from, date_to)
    result = [n for r in raw_rows if (n := normalize_search_term_row(r)) is not None]
    logger.info(
        "pull_search_terms_range: %d rows (%s → %s)",
        len(result), date_from, date_to,
    )
    return result


def pull_keyword_performance_range(date_from: str, date_to: str) -> list[dict]:
    """Pull and normalise keyword performance for an explicit date range.

    Args:
        date_from: ISO date string (YYYY-MM-DD), inclusive.
        date_to:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of normalised keyword rows matching the internal contract.
        Read-only. No DB writes. No Google Ads writes.
    """
    raw_rows = fetch_keyword_performance(date_from, date_to)
    result = [normalize_keyword_row(r) for r in raw_rows]
    logger.info(
        "pull_keyword_performance_range: %d rows (%s → %s)",
        len(result), date_from, date_to,
    )
    return result


def pull_geo_performance_range(date_from: str, date_to: str) -> list[dict]:
    """Pull and normalise geo performance for an explicit date range.

    Args:
        date_from: ISO date string (YYYY-MM-DD), inclusive.
        date_to:   ISO date string (YYYY-MM-DD), inclusive.

    Returns:
        List of normalised geo rows matching the internal contract.
        Read-only. No DB writes. No Google Ads writes.
    """
    raw_rows = fetch_geo_performance(date_from, date_to)
    result = [normalize_geo_row(r) for r in raw_rows]
    logger.info(
        "pull_geo_performance_range: %d rows (%s → %s)",
        len(result), date_from, date_to,
    )
    return result


# ---------------------------------------------------------------------------
# days_back convenience functions
# ---------------------------------------------------------------------------


def pull_campaign_performance(days_back: int = 30) -> list[dict]:
    """Pull and normalise campaign performance for the last *days_back* days.

    Delegates to pull_campaign_performance_range using get_date_range().

    Returns:
        List of normalised campaign rows. Read-only.
    """
    start, end = get_date_range(days_back)
    return pull_campaign_performance_range(start, end)


def pull_search_terms(days_back: int = 60) -> list[dict]:
    """Pull and normalise search term performance for the last *days_back* days.

    Delegates to pull_search_terms_range using get_date_range().
    Blank/null search_term rows are skipped.

    Returns:
        List of normalised search term rows. Read-only.
    """
    start, end = get_date_range(days_back)
    return pull_search_terms_range(start, end)


def pull_keyword_performance(days_back: int = 30) -> list[dict]:
    """Pull and normalise keyword performance for the last *days_back* days.

    Delegates to pull_keyword_performance_range using get_date_range().

    Returns:
        List of normalised keyword rows. Read-only.
    """
    start, end = get_date_range(days_back)
    return pull_keyword_performance_range(start, end)


def pull_geo_performance(days_back: int = 30) -> list[dict]:
    """Pull and normalise geo performance for the last *days_back* days.

    Delegates to pull_geo_performance_range using get_date_range().

    Returns:
        List of normalised geo rows. Read-only.
    """
    start, end = get_date_range(days_back)
    return pull_geo_performance_range(start, end)


# ---------------------------------------------------------------------------
# Output persistence (local files only — no Google Ads writes)
# ---------------------------------------------------------------------------


def save_output(campaigns: list, search_terms: list, keywords: list, geos: list):
    """Save all Google Ads data to the data/ directory.

    Writes the same compatibility files expected by downstream analysis logic:
        data/ads_campaigns.json
        data/ads_search_terms.json
        data/ads_keywords.json
        data/ads_geos.json

    This is local app output only.  No writes to Google Ads.
    No database writes.
    """
    os.makedirs("data", exist_ok=True)

    with open("data/ads_campaigns.json", "w") as f:
        json.dump(campaigns, f, indent=2)

    with open("data/ads_search_terms.json", "w") as f:
        json.dump(search_terms, f, indent=2)

    with open("data/ads_keywords.json", "w") as f:
        json.dump(keywords, f, indent=2)

    with open("data/ads_geos.json", "w") as f:
        json.dump(geos, f, indent=2)

    logger.info("Saved Google Ads data to data/ (%d campaigns, %d search terms, %d keywords, %d geos)",
                len(campaigns or []), len(search_terms or []), len(keywords or []), len(geos or []))
