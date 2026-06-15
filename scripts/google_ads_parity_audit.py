"""
PR-ADS-099 — Google Ads API vs Windsor Dataset Parity Audit

Read-only audit comparing direct Google Ads API data against Windsor/current source.
No database writes. No Google Ads writes. No scheduler changes.
No production source switch.

Usage:
    python scripts/google_ads_parity_audit.py [--windows 7,30,60] [--datasets campaigns,search_terms,keywords,geo]
"""

import argparse
import logging
import sys
import os
from datetime import datetime, timedelta, timezone

# Ensure repo root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parity math utilities
# ---------------------------------------------------------------------------

def percent_delta(google_ads_value: float, windsor_value: float) -> float | None:
    """Calculate percent delta between Google Ads and Windsor values.

    Returns 0.0 if both values are zero.
    Returns the percentage difference relative to Windsor as baseline.
    Returns None when Windsor baseline is zero and Google Ads is non-zero.
    """
    if windsor_value == 0 and google_ads_value == 0:
        return 0.0
    if windsor_value == 0:
        return None  # Cannot calculate % delta against zero baseline
    return ((google_ads_value - windsor_value) / abs(windsor_value)) * 100.0


def classify_status(
    spend_delta_pct: float | None,
    clicks_delta_pct: float | None,
    impressions_delta_pct: float | None,
    row_count_google: int,
    row_count_windsor: int,
    source_error: bool = False,
) -> str:
    """Classify parity status based on metric deltas and thresholds.

    Returns: PASS, WARNING, FAIL, or NOT_AVAILABLE.

    Thresholds:
    - PASS: spend delta <= 3% AND click/impression delta <= 5%
    - WARNING: spend delta <= 10% OR row-count differs materially but spend is close
    - FAIL: spend/click/impression deltas are large or source errors
    - NOT_AVAILABLE: Windsor/current source is missing (None deltas)
    """
    if source_error:
        return "FAIL"

    # If we have no Windsor data at all
    if row_count_windsor == 0 and row_count_google > 0:
        return "NOT_AVAILABLE"

    if row_count_windsor == 0 and row_count_google == 0:
        return "PASS"

    # Cannot compute meaningful deltas
    if spend_delta_pct is None:
        return "NOT_AVAILABLE"
    if clicks_delta_pct is None or impressions_delta_pct is None:
        return "NOT_AVAILABLE"

    abs_spend = abs(spend_delta_pct)
    abs_clicks = abs(clicks_delta_pct)
    abs_impressions = abs(impressions_delta_pct)

    # PASS: spend <= 3% and clicks/impressions <= 5%
    if abs_spend <= 3.0 and abs_clicks <= 5.0 and abs_impressions <= 5.0:
        # Check for material row count difference
        if row_count_windsor > 0:
            row_ratio = abs(row_count_google - row_count_windsor) / row_count_windsor
            if row_ratio > 0.5:
                return "WARNING"
        return "PASS"

    # WARNING: spend <= 10% or row-count differs but spend is close
    if abs_spend <= 10.0:
        return "WARNING"

    # FAIL
    return "FAIL"


def format_delta(value: float | None) -> str:
    """Format a delta percentage for display."""
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


# ---------------------------------------------------------------------------
# Data aggregation helpers
# ---------------------------------------------------------------------------

def _row_spend(row: dict, spend_key: str) -> float:
    """Return normalized spend for a row.

    Google Ads API reports cost in micros (`cost_micros`); spend must be
    normalized to currency units as ``cost_micros / 1_000_000``. The direct
    connector already exposes a pre-normalized ``spend`` field, but this helper
    falls back to normalizing ``cost_micros`` itself so the audit is correct
    even if a raw row is passed in.
    """
    val = row.get(spend_key)
    if val in (None, ""):
        # Fall back to a generic spend field, then to cost_micros normalization.
        val = row.get("spend")
    if val in (None, ""):
        cost_micros = row.get("cost_micros")
        if cost_micros not in (None, ""):
            return float(cost_micros) / 1_000_000
        return 0.0
    return float(val or 0)


def _row_conversion_value(row: dict) -> float:
    """Return conversion value for a row, supporting both source field names.

    Google Ads API uses ``conversions_value``; some sources use
    ``conversion_value``. Windsor REST does not currently expose conversion
    value at all, so this returns 0.0 for those rows (reported as N/A in the
    parity delta — see docs/29).
    """
    for key in ("conversions_value", "conversion_value"):
        val = row.get(key)
        if val not in (None, ""):
            return float(val or 0)
    return 0.0


def aggregate_metrics(rows: list, spend_key: str = "spend") -> dict:
    """Aggregate common metrics from a list of row dicts."""
    total_spend = 0.0
    total_clicks = 0
    total_impressions = 0
    total_conversions = 0.0
    total_conversion_value = 0.0
    rows_with_conversion_value = 0
    campaigns = set()
    ad_groups = set()
    search_terms = set()
    keywords = set()

    for row in rows:
        total_spend += _row_spend(row, spend_key)
        total_clicks += int(row.get("clicks", 0) or 0)
        total_impressions += int(row.get("impressions", 0) or 0)
        total_conversions += float(row.get("conversions", 0) or 0)
        if row.get("conversions_value") not in (None, "") or row.get("conversion_value") not in (None, ""):
            rows_with_conversion_value += 1
        total_conversion_value += _row_conversion_value(row)

        # Collect unique counts
        campaign_key = row.get("campaign_id")
        if campaign_key in (None, ""):
            campaign_key = row.get("campaign_name") or row.get("campaign")
        if campaign_key not in (None, ""):
            campaigns.add(campaign_key)
        if row.get("ad_group_id"):
            ad_groups.add(row["ad_group_id"])
        elif row.get("ad_group"):
            ad_groups.add(row["ad_group"])

        if row.get("search_term"):
            search_terms.add(row["search_term"])

        if row.get("keyword_text"):
            keywords.add(row["keyword_text"])
        elif row.get("keyword"):
            keywords.add(row["keyword"])

    return {
        "row_count": len(rows),
        "spend": round(total_spend, 2),
        "clicks": total_clicks,
        "impressions": total_impressions,
        "conversions": round(total_conversions, 2),
        "conversion_value": round(total_conversion_value, 2),
        "has_conversion_value": rows_with_conversion_value > 0,
        "campaign_count": len(campaigns),
        "ad_group_count": len(ad_groups),
        "search_term_count": len(search_terms),
        "keyword_count": len(keywords),
    }


# ---------------------------------------------------------------------------
# Data fetching — Google Ads direct
# ---------------------------------------------------------------------------

def fetch_google_ads_data(dataset: str, start_date: str, end_date: str) -> dict:
    """Fetch data from Google Ads direct connector.

    Returns dict with 'rows' and 'error' keys.
    """
    try:
        from connectors.google_ads_direct import (
            fetch_campaign_performance,
            fetch_search_terms,
            fetch_keyword_performance,
            fetch_geo_performance,
        )

        fetchers = {
            "campaigns": fetch_campaign_performance,
            "search_terms": fetch_search_terms,
            "keywords": fetch_keyword_performance,
            "geo": fetch_geo_performance,
        }

        fetcher = fetchers.get(dataset)
        if not fetcher:
            return {"rows": [], "error": f"Unknown dataset: {dataset}"}

        rows = fetcher(start_date, end_date)
        return {"rows": rows, "error": None}

    except Exception as exc:
        logger.error("Google Ads fetch error (%s): %s", dataset, exc)
        return {"rows": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# Data fetching — Windsor/current source
# ---------------------------------------------------------------------------

def fetch_windsor_data(
    dataset: str,
    start_date: str,
    end_date: str,
    days_back: int,
) -> dict:
    """Fetch data from Windsor connector (current source) for a closed window.

    For campaigns/keywords/geo, the explicit-range Windsor pulls
    (``pull_*_performance_range``) are used so both sources cover the **same**
    closed ``start_date → end_date`` window (yesterday-back, excluding today).

    Windsor search terms only support preset windows (``last_7d``/``last_30d``…)
    and have no explicit-range API, so they fall back to the preset
    ``pull_search_terms(days_back=...)`` path. This is a known/explained
    difference (see docs/29) — the preset window includes today, so search-term
    parity is treated as informational, not gating.

    Returns dict with 'rows', 'error', and 'window_mode' keys.
    """
    try:
        from connectors.windsor_pull import (
            pull_campaign_performance_range,
            pull_search_terms,
            pull_keyword_performance_range,
            pull_geo_performance_range,
        )

        range_fetchers = {
            "campaigns": lambda: pull_campaign_performance_range(start_date, end_date),
            "keywords": lambda: pull_keyword_performance_range(start_date, end_date),
            "geo": lambda: pull_geo_performance_range(start_date, end_date),
        }

        if dataset in range_fetchers:
            rows = range_fetchers[dataset]()
            return {"rows": rows, "error": None, "window_mode": "closed_range"}

        if dataset == "search_terms":
            # No explicit-range API for Windsor search terms — preset window only.
            rows = pull_search_terms(days_back=days_back)
            return {"rows": rows, "error": None, "window_mode": "preset"}

        return {"rows": [], "error": f"Unknown dataset: {dataset}", "window_mode": None}

    except Exception as exc:
        logger.error("Windsor fetch error (%s): %s", dataset, exc)
        return {"rows": [], "error": str(exc), "window_mode": None}


# ---------------------------------------------------------------------------
# Parity comparison
# ---------------------------------------------------------------------------

def compare_dataset(
    dataset: str,
    days_back: int,
    start_date: str,
    end_date: str,
) -> dict:
    """Compare a single dataset between Google Ads and Windsor.

    Returns a parity result dict.
    """
    # Fetch from both sources (closed window — yesterday-back, excludes today)
    gads_result = fetch_google_ads_data(dataset, start_date, end_date)
    windsor_result = fetch_windsor_data(dataset, start_date, end_date, days_back)

    source_error = bool(gads_result["error"] or windsor_result["error"])

    # Aggregate metrics
    gads_metrics = aggregate_metrics(gads_result["rows"])
    windsor_metrics = aggregate_metrics(windsor_result["rows"], spend_key="spend")

    # Calculate deltas
    spend_delta = percent_delta(gads_metrics["spend"], windsor_metrics["spend"])
    clicks_delta = percent_delta(gads_metrics["clicks"], windsor_metrics["clicks"])
    impressions_delta = percent_delta(gads_metrics["impressions"], windsor_metrics["impressions"])
    conversions_delta = percent_delta(gads_metrics["conversions"], windsor_metrics["conversions"])
    # Conversion value is only available where the source reports it. Windsor
    # REST does not currently expose conversion value, so this delta is usually
    # N/A — reported for information, never used to gate the PASS/WARN/FAIL verdict.
    conversion_value_delta = percent_delta(
        gads_metrics["conversion_value"], windsor_metrics["conversion_value"]
    )

    # Row delta
    row_delta = gads_metrics["row_count"] - windsor_metrics["row_count"]

    # Classify status
    status = classify_status(
        spend_delta_pct=spend_delta,
        clicks_delta_pct=clicks_delta,
        impressions_delta_pct=impressions_delta,
        row_count_google=gads_metrics["row_count"],
        row_count_windsor=windsor_metrics["row_count"],
        source_error=source_error,
    )

    # Build notes
    notes = []
    if gads_result["error"]:
        notes.append(f"Google Ads error: {gads_result['error']}")
    if windsor_result["error"]:
        notes.append(f"Windsor error: {windsor_result['error']}")
    if windsor_result.get("window_mode") == "preset":
        notes.append(
            "Windsor search terms use a preset window (includes today); "
            "Google Ads uses the closed yesterday-back window — small "
            "partial-day differences are expected"
        )
    if windsor_metrics["row_count"] > 0:
        row_ratio = abs(row_delta) / windsor_metrics["row_count"]
        if row_ratio > 0.5 and status != "FAIL":
            notes.append(
                "Row count differs materially — expected for search terms "
                "where direct API returns more granular rows"
            )
    if not windsor_metrics["has_conversion_value"] and gads_metrics["has_conversion_value"]:
        notes.append(
            "Conversion value reported by Google Ads only — Windsor REST does "
            "not expose it, so conversion-value parity is N/A (informational)"
        )

    return {
        "dataset": dataset,
        "window": f"{days_back}d",
        "window_start": start_date,
        "window_end": end_date,
        "windsor_window_mode": windsor_result.get("window_mode"),
        "google_ads_rows": gads_metrics["row_count"],
        "windsor_rows": windsor_metrics["row_count"],
        "row_delta": row_delta,
        "google_ads_spend": gads_metrics["spend"],
        "windsor_spend": windsor_metrics["spend"],
        "spend_delta_pct": spend_delta,
        "clicks_delta_pct": clicks_delta,
        "impressions_delta_pct": impressions_delta,
        "conversions_delta_pct": conversions_delta,
        "google_ads_conversion_value": gads_metrics["conversion_value"],
        "windsor_conversion_value": windsor_metrics["conversion_value"],
        "conversion_value_delta_pct": conversion_value_delta,
        "conversion_value_available": (
            gads_metrics["has_conversion_value"] and windsor_metrics["has_conversion_value"]
        ),
        "google_ads_campaign_count": gads_metrics["campaign_count"],
        "windsor_campaign_count": windsor_metrics["campaign_count"],
        "google_ads_ad_group_count": gads_metrics["ad_group_count"],
        "windsor_ad_group_count": windsor_metrics["ad_group_count"],
        "google_ads_search_term_count": gads_metrics["search_term_count"],
        "windsor_search_term_count": windsor_metrics["search_term_count"],
        "google_ads_keyword_count": gads_metrics["keyword_count"],
        "windsor_keyword_count": windsor_metrics["keyword_count"],
        "status": status,
        "notes": "; ".join(notes) if notes else "",
    }


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_report(results: list) -> str:
    """Format parity results into a human-readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append("Google Ads vs Windsor Parity Audit")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("Windows are closed (yesterday-back, excluding today) to avoid partial-day mismatch.")
    lines.append("=" * 70)

    current_window = None
    for r in results:
        if r["window"] != current_window:
            current_window = r["window"]
            lines.append("")
            window_range = ""
            if r.get("window_start") and r.get("window_end"):
                window_range = f"  ({r['window_start']} → {r['window_end']})"
            lines.append(f"Window: {current_window}{window_range}")
            lines.append("-" * 40)

        lines.append("")
        lines.append(f"  {r['dataset'].upper()}")
        lines.append(f"    Google Ads API rows: {r['google_ads_rows']}")
        lines.append(f"    Windsor rows:        {r['windsor_rows']}")
        lines.append(f"    Row delta:           {r['row_delta']:+d}")
        lines.append(f"    Google Ads spend:    ${r['google_ads_spend']:.2f}")
        lines.append(f"    Windsor spend:       ${r['windsor_spend']:.2f}")
        lines.append(f"    Spend delta:         {format_delta(r['spend_delta_pct'])}")
        lines.append(f"    Clicks delta:        {format_delta(r['clicks_delta_pct'])}")
        lines.append(f"    Impressions delta:   {format_delta(r['impressions_delta_pct'])}")
        lines.append(f"    Conversions delta:   {format_delta(r['conversions_delta_pct'])}")
        if r.get("conversion_value_available"):
            lines.append(f"    Conv. value delta:   {format_delta(r['conversion_value_delta_pct'])}")
        else:
            lines.append("    Conv. value delta:   N/A (not exposed by both sources)")
        lines.append(f"    Status:              {r['status']}")
        if r["notes"]:
            lines.append(f"    Notes:               {r['notes']}")

    # Summary
    lines.append("")
    lines.append("=" * 70)
    lines.append("SUMMARY")
    lines.append("=" * 70)
    statuses = [r["status"] for r in results]
    lines.append(f"  PASS:          {statuses.count('PASS')}")
    lines.append(f"  WARNING:       {statuses.count('WARNING')}")
    lines.append(f"  FAIL:          {statuses.count('FAIL')}")
    lines.append(f"  NOT_AVAILABLE: {statuses.count('NOT_AVAILABLE')}")
    lines.append("")

    if "FAIL" in statuses:
        lines.append("  RECOMMENDATION: Do NOT cut over to Google Ads direct API yet.")
        lines.append("                  Investigate FAIL items before proceeding.")
    elif "WARNING" in statuses:
        lines.append("  RECOMMENDATION: Review WARNING items. If row count differences")
        lines.append("                  are expected (e.g. search terms), proceed with caution.")
    else:
        lines.append("  RECOMMENDATION: Google Ads direct API appears ready to replace Windsor.")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

def run_audit(windows: list[int] = None, datasets: list[str] = None) -> list[dict]:
    """Run the full parity audit across specified windows and datasets.

    Returns list of result dicts.
    """
    if windows is None:
        windows = [7, 30, 60]
    if datasets is None:
        datasets = ["campaigns", "search_terms", "keywords", "geo"]

    results = []
    for days_back in windows:
        # Closed window: end on yesterday (UTC) and exclude today so neither
        # source compares a partial, still-accumulating current day.
        today = datetime.now(timezone.utc).date()
        end_date = today - timedelta(days=1)
        start_date = end_date - timedelta(days=days_back - 1)

        for dataset in datasets:
            logger.info("Comparing %s for %dd window (%s → %s)", dataset, days_back, start_date, end_date)
            result = compare_dataset(
                dataset=dataset,
                days_back=days_back,
                start_date=str(start_date),
                end_date=str(end_date),
            )
            results.append(result)

    return results


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="PR-ADS-099: Google Ads API vs Windsor Parity Audit"
    )
    parser.add_argument(
        "--windows",
        default="7,30,60",
        help="Comma-separated audit windows in days (default: 7,30,60)",
    )
    parser.add_argument(
        "--datasets",
        default="campaigns,search_terms,keywords,geo",
        help="Comma-separated datasets to audit (default: campaigns,search_terms,keywords,geo)",
    )

    args = parser.parse_args()
    windows = [int(w.strip()) for w in args.windows.split(",")]
    datasets = [d.strip() for d in args.datasets.split(",")]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info("Starting Google Ads vs Windsor Parity Audit")
    logger.info("Windows: %s", windows)
    logger.info("Datasets: %s", datasets)

    results = run_audit(windows=windows, datasets=datasets)
    report = format_report(results)
    print(report)

    # Return exit code based on results
    statuses = [r["status"] for r in results]
    if "FAIL" in statuses:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
