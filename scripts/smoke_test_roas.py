"""
Smoke Test — ROAS Backend Pipeline
Proves the backend revenue pipeline works before frontend/menu PR begins.

Usage:
    python scripts/smoke_test_roas.py

Expected output:
    - Pulled 50+ won deals
    - Attribution tier counts printed
    - Gulf ROAS printed
    - Verdict printed
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    print("=" * 60)
    print("PR-ADS-080A — ROAS Backend Smoke Test")
    print("=" * 60)

    # Step 1: Pull won deals
    print("\n[1] Pulling HubSpot won deals...")
    try:
        from connectors.hubspot_deals import pull_won_deals

        deals = pull_won_deals(days_back=365)
        print(f"    Pulled {len(deals)} won deals")

        if len(deals) == 0:
            print("    ⚠ No deals pulled — check HUBSPOT_API_KEY and connectivity")
            print("    Attempting to load from existing data file...")
            data_file = Path(__file__).parent.parent / "data" / "hubspot_won_deals.json"
            if data_file.exists():
                with open(data_file, "r") as f:
                    deals = json.load(f)
                print(f"    Loaded {len(deals)} deals from cache")
            else:
                print("    ✗ No cached data available")
                sys.exit(1)

        if deals:
            sample = deals[0]
            print(f"    Sample deal: {sample.get('dealname', 'N/A')}")
            print(f"      Amount: ${sample.get('amount', 0):.2f}")
            print(f"      MRR: ${sample.get('hs_mrr', 0):.2f}")
            print(f"      Close date: {sample.get('closedate', 'N/A')}")

    except Exception as exc:
        print(f"    ✗ Failed to pull deals: {exc}")
        print("    Attempting to load from existing data file...")
        data_file = Path(__file__).parent.parent / "data" / "hubspot_won_deals.json"
        if data_file.exists():
            with open(data_file, "r") as f:
                deals = json.load(f)
            print(f"    Loaded {len(deals)} deals from cache")
        else:
            print("    ✗ No cached data available. Exiting.")
            sys.exit(1)

    # Step 2: Attribution
    print("\n[2] Running attribution matcher...")
    try:
        from analysis.attribution_matcher import attribute_deals, get_attribution_summary

        attributed = attribute_deals(deals)
        summary = get_attribution_summary(attributed)
        print(f"    Attribution: {summary}")
        print(f"      Tier 1 (GCLID): {summary['tier_1']}")
        print(f"      Tier 2 (Source tag): {summary['tier_2']}")
        print(f"      Tier 3 (Spend-weighted): {summary['tier_3']}")

        # Tier 1 remaining 0 is expected until GCLID is wired
        if summary["tier_1"] == 0:
            print("    ℹ Tier 1 = 0 is expected until GCLID attribution is wired")

    except Exception as exc:
        print(f"    ✗ Attribution failed: {exc}")
        sys.exit(1)

    # Step 3: ROAS Calculation
    print("\n[3] Computing campaign ROAS...")
    try:
        from analysis.roas_calculator import compute_campaign_roas, compute_all_campaign_roas

        all_roas = compute_all_campaign_roas(window_days=60)
        print(f"    Computed ROAS for {len(all_roas)} campaigns")

        # Try Gulf specifically
        gulf_roas = compute_campaign_roas("gulf", window_days=60)
        print(f"\n    Gulf ROAS:")
        print(f"      Spend: ${gulf_roas.get('spend', 0):,.2f}")
        print(f"      ACV revenue: ${gulf_roas.get('acv_revenue', 0):,.2f}")
        print(f"      ACV ROAS: {gulf_roas.get('acv_roas', 'N/A')}x")
        print(f"      LTV/CAC: {gulf_roas.get('ltv_to_cac', 'N/A')}")
        print(f"      Payback months: {gulf_roas.get('payback_months', 'N/A')}")
        print(f"      Verdict: {gulf_roas.get('verdict', 'N/A')}")
        print(f"      Attribution: {gulf_roas.get('attribution_confidence', 'N/A')}")

        if gulf_roas.get("warnings"):
            print(f"      Warnings: {gulf_roas['warnings']}")

    except Exception as exc:
        print(f"    ✗ ROAS calculation failed: {exc}")
        sys.exit(1)

    # Step 4: Unit Economics
    print("\n[4] Computing unit economics...")
    try:
        from connectors.hubspot_churn import get_monthly_churn
        from datetime import datetime

        current_month = datetime.utcnow().strftime("%Y-%m")
        churn = get_monthly_churn(current_month, "gulf")
        print(f"    Churn config: {churn}")

    except Exception as exc:
        print(f"    ✗ Unit economics failed: {exc}")
        sys.exit(1)

    # Step 5: Country ROAS
    print("\n[5] Computing country ROAS...")
    try:
        from analysis.roas_calculator import compute_all_country_roas

        country_roas = compute_all_country_roas(window_days=60)
        print(f"    Computed ROAS for {len(country_roas)} countries")
        for cr in country_roas[:3]:
            print(f"      {cr.get('country', 'N/A')}: "
                  f"estimate={cr.get('country_level_estimate', True)}, "
                  f"confidence={cr.get('attribution_confidence', 'N/A')}")

    except Exception as exc:
        print(f"    ✗ Country ROAS failed: {exc}")
        sys.exit(1)

    # Step 6: Validation checks
    print("\n[6] Validation checks...")
    errors = []

    # Window filter regression check (old deals must be excluded from 60d window)
    from analysis.roas_calculator import _filter_deals_by_window
    now = datetime.utcnow()
    sample_window_deals = [
        {"deal_id": "recent", "closedate": (now - timedelta(days=5)).isoformat() + "Z"},
        {"deal_id": "old", "closedate": (now - timedelta(days=120)).isoformat() + "Z"},
    ]
    filtered_ids = {
        d.get("deal_id") for d in _filter_deals_by_window(sample_window_deals, window_days=60)
    }
    if "old" in filtered_ids:
        errors.append("Window filter regression: old deal included in 60d window")
    if "recent" not in filtered_ids:
        errors.append("Window filter regression: recent deal excluded in 60d window")

    # Check all campaign rows have attribution_confidence
    for camp in all_roas:
        if "attribution_confidence" not in camp:
            errors.append(f"Campaign {camp.get('campaign')} missing attribution_confidence")

    # Check country rows are marked as estimate
    for cr in country_roas:
        if not cr.get("country_level_estimate"):
            errors.append(f"Country {cr.get('country')} not marked as estimate")

    # Check verdict exists
    for camp in all_roas:
        if "verdict" not in camp:
            errors.append(f"Campaign {camp.get('campaign')} missing verdict")

    if errors:
        print("    ✗ Validation errors:")
        for e in errors:
            print(f"      - {e}")
        sys.exit(1)
    else:
        print("    ✓ All validation checks passed")

    print("\n" + "=" * 60)
    print("✓ ROAS Backend Smoke Test PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
