"""
Google Ads Direct API Smoke Test — PR-ADS-098

Validates end-to-end connectivity against the live Google Ads account.
Queries the last 7 days of campaigns, search terms, and keywords.
Prints row counts and a sample of redacted rows.
Writes nothing to the database.

Usage:
  python scripts/google_ads_smoke_test.py

Prerequisites:
  All Google Ads env vars must be configured:
    GOOGLE_ADS_DEVELOPER_TOKEN
    GOOGLE_ADS_CLIENT_ID
    GOOGLE_ADS_CLIENT_SECRET
    GOOGLE_ADS_REFRESH_TOKEN
    GOOGLE_ADS_CUSTOMER_ID
  Optional:
    GOOGLE_ADS_LOGIN_CUSTOMER_ID  (omit for direct customer mode)
"""

import os
import sys
from datetime import datetime, timedelta

# Ensure repo root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors.google_ads_direct import (
    build_google_ads_client,
    fetch_campaign_performance,
    fetch_keyword_performance,
    fetch_search_terms,
    get_access_mode,
    get_customer_id,
)

_REQUIRED_VARS = [
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
]

SAMPLE_ROWS = 3


def _check_env() -> bool:
    missing = [v for v in _REQUIRED_VARS if not os.getenv(v)]
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
        print(
            "Run 'python scripts/google_ads_readiness_check.py' for details.",
            file=sys.stderr,
        )
        return False
    return True


def _redact_row(row: dict) -> dict:
    """Return a copy of the row with sensitive string values partially redacted."""
    safe = {}
    sensitive_keys = {"keyword_text", "search_term"}
    for k, v in row.items():
        if k in sensitive_keys and isinstance(v, str) and len(v) > 4:
            safe[k] = v[:2] + "****"
        else:
            safe[k] = v
    return safe


def _print_sample(rows: list, label: str) -> None:
    if not rows:
        return
    print(f"  Sample ({label}, up to {SAMPLE_ROWS} rows):")
    for row in rows[:SAMPLE_ROWS]:
        safe = _redact_row(row)
        print(f"    {safe}")


def main() -> None:
    print()
    print("Google Ads direct API smoke test")
    print("=" * 40)

    if not _check_env():
        sys.exit(1)

    customer_id = get_customer_id()
    mode = get_access_mode()
    print(f"Mode: {mode}")
    print(f"Customer ID: {customer_id}")

    end = datetime.utcnow().date()
    start = end - timedelta(days=6)
    start_str = str(start)
    end_str = str(end)
    print(f"Date range:  {start_str} → {end_str}")
    print()

    # ── Build client ───────────────────────────────────────────────────────
    try:
        build_google_ads_client()
        print("Client build: OK")
    except Exception as exc:  # noqa: BLE001
        print(f"Client build: FAILED — {exc}", file=sys.stderr)
        sys.exit(1)

    # ── Campaign performance ───────────────────────────────────────────────
    campaign_ok = True
    try:
        campaigns = fetch_campaign_performance(start_str, end_str)
        print(f"Campaign rows: {len(campaigns)}")
        _print_sample(campaigns, "campaigns")
    except Exception as exc:  # noqa: BLE001
        print(f"Campaign query FAILED: {exc}", file=sys.stderr)
        campaigns = []
        campaign_ok = False

    # ── Search terms ───────────────────────────────────────────────────────
    search_terms_ok = True
    try:
        search_terms = fetch_search_terms(start_str, end_str)
        print(f"Search term rows: {len(search_terms)}")
        _print_sample(search_terms, "search terms")
    except Exception as exc:  # noqa: BLE001
        print(f"Search term query FAILED: {exc}", file=sys.stderr)
        search_terms = []
        search_terms_ok = False

    # ── Keywords ───────────────────────────────────────────────────────────
    keywords_ok = True
    try:
        keywords = fetch_keyword_performance(start_str, end_str)
        print(f"Keyword rows: {len(keywords)}")
        _print_sample(keywords, "keywords")
    except Exception as exc:  # noqa: BLE001
        print(f"Keyword query FAILED: {exc}", file=sys.stderr)
        keywords = []
        keywords_ok = False

    print()

    all_ok = campaign_ok and search_terms_ok and keywords_ok

    if all_ok:
        print("Status: PASS")
    else:
        print("Status: FAIL")
        sys.exit(1)

    print()


if __name__ == "__main__":
    main()
