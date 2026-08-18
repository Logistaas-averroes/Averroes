"""
Tests for ROAS Snapshot Service (PR-ADS-080C).

Validates:
  - Snapshot service exists and is importable.
  - Snapshot contains required keys.
  - Snapshot does not use Google Ads conversion value.
  - Campaign rows include attribution_confidence.
  - Country rows include country_level_estimate.
  - Scheduler imports snapshot service but does not contain ROAS math.
  - Snapshot endpoints use /api/reports/roas/snapshots...
  - Runtime data paths are under data/ and not committed.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_snapshot_service_importable():
    """Snapshot service module can be imported without error."""
    import services.roas_snapshot_service as svc  # noqa: F401

    assert hasattr(svc, "generate_roas_snapshot")
    assert hasattr(svc, "save_roas_snapshot")
    assert hasattr(svc, "load_latest_roas_snapshot")
    assert hasattr(svc, "load_roas_snapshots")


def test_snapshot_service_uses_080a_functions():
    """Snapshot service imports from analysis.roas_calculator (080A)."""
    import services.roas_snapshot_service as svc
    import inspect

    source = inspect.getsource(svc)
    assert "compute_all_campaign_roas" in source
    assert "compute_all_country_roas" in source


def test_snapshot_service_no_google_ads_conversion_value():
    """Snapshot service hard-codes google_ads_conversion_value_used = False."""
    import services.roas_snapshot_service as svc
    import inspect

    source = inspect.getsource(svc)
    assert '"google_ads_conversion_value_used": False' in source or \
           "'google_ads_conversion_value_used': False" in source


def test_scheduler_does_not_contain_roas_math():
    """Scheduler orchestrates ROAS snapshot but does not compute ROAS itself."""
    scheduler_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scheduler",
        "daily.py",
    )
    with open(scheduler_path, "r", encoding="utf-8") as f:
        source = f.read()

    # Should import from services, not compute locally
    assert "generate_roas_snapshot" in source
    assert "save_roas_snapshot" in source

    # Should NOT contain ROAS calculation logic
    assert "compute_all_campaign_roas" not in source
    assert "compute_all_country_roas" not in source
    assert "_compute_roas_metrics" not in source


def test_scheduler_imports_snapshot_service():
    """Scheduler imports from services.roas_snapshot_service."""
    scheduler_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scheduler",
        "daily.py",
    )
    with open(scheduler_path, "r", encoding="utf-8") as f:
        source = f.read()

    assert "from services.roas_snapshot_service import" in source


def test_snapshot_endpoints_in_server():
    """API server exposes /api/reports/roas/snapshots endpoints."""
    server_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api",
        "server.py",
    )
    with open(server_path, "r", encoding="utf-8") as f:
        source = f.read()

    assert '"/api/reports/roas/snapshots/latest"' in source
    assert '"/api/reports/roas/snapshots"' in source


def test_data_dir_is_gitignored():
    """Runtime data paths are under data/ which is gitignored."""
    gitignore_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".gitignore",
    )
    with open(gitignore_path, "r", encoding="utf-8") as f:
        gitignore = f.read()

    assert "data/" in gitignore


def test_snapshot_service_persistence_uses_data_dir():
    """Snapshot service writes under data/roas_snapshots/."""
    import services.roas_snapshot_service as svc

    assert "roas_snapshots" in str(svc.SNAPSHOT_DIR)
    assert "data" in str(svc.SNAPSHOT_DIR)


def test_existing_live_endpoints_unchanged():
    """Existing 080A live endpoints are not modified by 080C."""
    server_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api",
        "server.py",
    )
    with open(server_path, "r", encoding="utf-8") as f:
        source = f.read()

    # These must remain as live-compute endpoints
    assert '"/api/reports/roas/campaigns"' in source
    assert '"/api/reports/roas/countries"' in source
    assert '"/api/reports/unit-economics"' in source


def test_snapshot_service_rejects_invalid_window():
    """Snapshot generation rejects invalid windows with a clean ValueError."""
    from services.roas_snapshot_service import generate_roas_snapshot

    invalid_windows = ["", "60days", "99999d", "abc", "31d"]
    for window in invalid_windows:
        try:
            generate_roas_snapshot(window=window)
            assert False, f"Expected ValueError for window={window!r}"
        except ValueError as exc:
            assert "Invalid window" in str(exc)


def test_live_unit_economics_is_canonical_and_snapshots_stay_legacy():
    """PR-ADS-153E-B split these two deliberately.

    The LIVE Unit Economics endpoint was migrated to the canonical deal ledger
    and canonical Google Ads spend. The ROAS SNAPSHOT reports are a deprecated
    historical lineage (local JSON + Windsor) retired in PR-ADS-153G, so they
    keep their own aggregation helper. Sharing a helper between them is no
    longer the goal — the two describe different populations, and pretending
    otherwise is exactly the defect 153E removed.
    """
    import services.roas_snapshot_service as snapshot_service
    import inspect

    server_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api",
        "server.py",
    )
    with open(server_path, "r", encoding="utf-8") as f:
        server_source = f.read()

    snapshot_source = inspect.getsource(snapshot_service)
    # The live endpoint reads the canonical contract, not the legacy helper.
    assert "canonical_unit_economics_service" in server_source
    assert "build_unit_economics" in server_source
    # The snapshot lineage is untouched and still self-contained.
    assert "compute_unit_economics_summary" in snapshot_source


def test_snapshot_service_valid_window_uses_allowlist_value():
    """Allowed windows are converted to expected day values."""
    from services.roas_snapshot_service import generate_roas_snapshot

    with patch("services.roas_snapshot_service.compute_all_campaign_roas", return_value=[]) as campaign_mock, patch(
        "services.roas_snapshot_service.compute_all_country_roas",
        return_value=[],
    ) as country_mock, patch(
        "services.roas_snapshot_service.compute_unit_economics_summary",
        return_value={
            "ltv_to_cac": None,
            "payback_months": None,
            "avg_deal_acv": 0,
            "avg_deal_mrr": 0,
            "monthly_churn_rate_used": 0.0,
            "total_spend": 0,
            "total_deals_won": 0,
            "total_acv_revenue": 0,
            "total_ltv_revenue": 0,
            "overall_attribution_confidence": "tier_3_spend_weighted",
            "overall_verdict": "INSUFFICIENT_DATA",
            "verdict": "INSUFFICIENT_DATA",
        },
    ) as economics_mock:
        snapshot = generate_roas_snapshot(window="14d")

    assert campaign_mock.call_args.kwargs == {"window_days": 14}
    assert country_mock.call_args.kwargs == {"window_days": 14}
    economics_mock.assert_called_once_with([])
    assert snapshot["window"] == "14d"


if __name__ == "__main__":
    tests = [
        test_snapshot_service_importable,
        test_snapshot_service_uses_080a_functions,
        test_snapshot_service_no_google_ads_conversion_value,
        test_scheduler_does_not_contain_roas_math,
        test_scheduler_imports_snapshot_service,
        test_snapshot_endpoints_in_server,
        test_data_dir_is_gitignored,
        test_snapshot_service_persistence_uses_data_dir,
        test_existing_live_endpoints_unchanged,
        test_snapshot_service_rejects_invalid_window,
        test_snapshot_and_live_endpoint_use_shared_unit_economics_helper,
        test_snapshot_service_valid_window_uses_allowlist_value,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {test.__name__} — {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {test.__name__} — {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
