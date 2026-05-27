"""
Smoke Test: ROAS Snapshot Generation & Persistence (PR-ADS-080C)

Usage:
    python scripts/smoke_test_roas_snapshot.py

Verifies:
  - Snapshot can be generated from existing 080A functions.
  - Snapshot has all required keys and structure.
  - Snapshot is saved to filesystem.
  - Latest pointer is updated.
  - google_ads_conversion_value_used is false.
  - Campaign rows include attribution_confidence.
  - Country rows include country_level_estimate.
  - Verdict fields exist.
"""

import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.roas_snapshot_service import (
    generate_roas_snapshot,
    save_roas_snapshot,
    load_latest_roas_snapshot,
)


def main():
    errors = []

    print("=" * 60)
    print("SMOKE TEST: ROAS Snapshot (PR-ADS-080C)")
    print("=" * 60)

    # 1. Generate snapshot
    print("\nGenerating ROAS snapshot for window=60d...")
    try:
        snapshot = generate_roas_snapshot(window="60d")
    except Exception as exc:
        print(f"FAIL: Snapshot generation raised exception: {exc}")
        sys.exit(1)

    campaigns = snapshot.get("campaigns", [])
    countries = snapshot.get("countries", [])
    unit_economics = snapshot.get("unit_economics", {})
    summary = snapshot.get("summary", {})

    print(f"  Campaign rows: {len(campaigns)}")
    print(f"  Country rows: {len(countries)}")
    print(f"  Unit economics: {'OK' if unit_economics else 'EMPTY'}")

    # 2. Validate required top-level keys
    required_keys = [
        "snapshot_date",
        "generated_at",
        "window",
        "source_truth",
        "google_ads_conversion_value_used",
        "campaigns",
        "countries",
        "unit_economics",
        "summary",
        "warnings",
    ]
    for key in required_keys:
        if key not in snapshot:
            errors.append(f"Missing top-level key: {key}")

    # 3. google_ads_conversion_value_used must be false
    if snapshot.get("google_ads_conversion_value_used") is not False:
        errors.append("google_ads_conversion_value_used is not false")

    # 4. source_truth
    if snapshot.get("source_truth") != "hubspot_won_deals_plus_windsor_spend":
        errors.append(f"Unexpected source_truth: {snapshot.get('source_truth')}")

    # 5. Campaign rows must include attribution_confidence and verdict
    for i, c in enumerate(campaigns):
        if "attribution_confidence" not in c:
            errors.append(f"Campaign row {i} missing attribution_confidence")
            break
        if "verdict" not in c:
            errors.append(f"Campaign row {i} missing verdict")
            break

    # 6. Country rows must include country_level_estimate
    for i, c in enumerate(countries):
        if "country_level_estimate" not in c:
            errors.append(f"Country row {i} missing country_level_estimate")
            break
        if "attribution_confidence" not in c:
            errors.append(f"Country row {i} missing attribution_confidence")
            break
        if "verdict" not in c:
            errors.append(f"Country row {i} missing verdict")
            break

    # 7. Summary has required fields
    summary_keys = [
        "campaign_count",
        "country_count",
        "scale_count",
        "hold_count",
        "fix_count",
        "cut_count",
        "insufficient_data_count",
        "total_spend",
        "total_acv_revenue",
        "total_ltv_revenue",
    ]
    for key in summary_keys:
        if key not in summary:
            errors.append(f"Missing summary key: {key}")

    # 8. Save snapshot
    print("\nSaving snapshot...")
    save_result = save_roas_snapshot(snapshot)
    if save_result.get("status") != "success":
        errors.append(f"Save failed: {save_result.get('error')}")
    else:
        print(f"  Saved snapshot: {save_result.get('snapshot_path')}")
        print(f"  Latest pointer updated: {save_result.get('latest_path')}")

    # 9. Verify load
    loaded = load_latest_roas_snapshot(window="60d")
    if loaded is None:
        errors.append("Failed to load latest snapshot after save")
    elif loaded.get("snapshot_date") != snapshot.get("snapshot_date"):
        errors.append("Loaded snapshot date does not match saved snapshot date")

    # Result
    print("\n" + "=" * 60)
    if errors:
        print("Snapshot validation: FAIL")
        for err in errors:
            print(f"  ✗ {err}")
        sys.exit(1)
    else:
        print("Snapshot validation: PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
