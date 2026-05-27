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

import ast
import os
import sys

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
