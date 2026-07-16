"""
tests/test_daily_incremental_sync.py

PR-ADS-073 — Tests for the daily incremental sync module.

Covers:
  - Summary shape is stable
  - Config values load correctly
  - Rolling date windows are calculated correctly
  - Failed dataset does not fail whole sync
  - Pulled rows > 0 and written rows = 0 marks dataset failed
  - Zero rows from successful pull marks success with row_count=0
  - Unsupported datasets are skipped and documented
  - Scheduler job ID registered
  - POST /run/incremental-sync returns honest top-level status
  - Config lookback values wired into runtime defaults
  - No unsafe external action wording in module source

Run with:
    python -m pytest tests/test_daily_incremental_sync.py -v
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_rows(n: int) -> list[dict]:
    """Return n fake Windsor/HubSpot rows with a date field."""
    today = date.today().isoformat()
    return [{"date": today, "campaign": f"test-campaign-{i}", "spend": 1.0} for i in range(n)]


def _make_fake_sync(
    rows_pulled: int = 3,
    rows_written: int | None = None,
    pull_raises: Exception | None = None,
    write_raises: Exception | None = None,
) -> tuple:
    """Return (fake_pull, fake_write) callables for monkeypatching."""
    written = rows_written if rows_written is not None else rows_pulled

    def fake_pull(*args, **kwargs):
        if pull_raises:
            raise pull_raises
        return _fake_rows(rows_pulled)

    def fake_write(*args, **kwargs):
        if write_raises:
            raise write_raises
        return written

    return fake_pull, fake_write


# ---------------------------------------------------------------------------
# Summary shape
# ---------------------------------------------------------------------------

class TestSummaryShape:
    """The summary dict returned by run_daily_incremental_sync must be stable."""

    def test_top_level_keys_present(self, monkeypatch):
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync(run_reason="test")

        for key in ("status", "run_type", "run_reason", "started_at", "finished_at",
                    "lookback", "datasets", "errors"):
            assert key in result, f"Missing top-level key: {key}"

    def test_run_type_value(self, monkeypatch):
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()
        assert result["run_type"] == "daily_incremental_sync"

    def test_lookback_keys(self, monkeypatch):
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync(
            lookback_days_ads=10,
            lookback_days_hubspot_contacts=7,
            lookback_days_hubspot_deals=21,
        )
        assert result["lookback"]["ads_days"] == 10
        assert result["lookback"]["hubspot_contacts_days"] == 7
        assert result["lookback"]["hubspot_deals_days"] == 21

    def test_datasets_keys_present(self, monkeypatch):
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()
        expected = {
            "windsor/campaigns",
            "windsor/keywords",
            "windsor/geo",
            "windsor/search_terms",
            "hubspot/contacts",
            "hubspot/deals",
            "gclid/matches",
            "hubspot/source_classification",
            "google_ads/canonical_spend",
            "fx/daily_rates",
            "mailchimp/refresh",
        }
        assert set(result["datasets"].keys()) == expected

    def test_errors_is_list(self, monkeypatch):
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()
        assert isinstance(result["errors"], list)


# ---------------------------------------------------------------------------
# Config values
# ---------------------------------------------------------------------------

class TestConfigValues:
    """Verify config/thresholds.yaml sync block loads and contains expected keys."""

    def test_sync_block_present(self):
        import yaml
        config_path = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
        with config_path.open() as f:
            cfg = yaml.safe_load(f)

        assert "sync" in cfg
        assert "daily_incremental" in cfg["sync"]
        di = cfg["sync"]["daily_incremental"]
        assert di["enabled"] is True
        assert di["schedule_hour"] == 9
        assert di["schedule_minute"] == 0
        assert di["timezone"] == "Asia/Amman"
        assert di["lookback_days"]["ads"] == 14
        assert di["lookback_days"]["hubspot_contacts"] == 14
        assert di["lookback_days"]["hubspot_deals"] == 30


# ---------------------------------------------------------------------------
# Rolling date windows
# ---------------------------------------------------------------------------

class TestRollingDateWindows:
    """Rolling date windows must be calculated correctly from lookback_days."""

    def test_ads_window(self, monkeypatch):
        captured = {}

        def fake_campaign_range(date_from, date_to):
            captured["campaigns"] = (date_from, date_to)
            return []

        _patch_all_datasets_success(monkeypatch, campaign_pull=fake_campaign_range)

        from scheduler.incremental_sync import run_daily_incremental_sync
        import datetime as dt
        today = dt.datetime.now(tz=dt.timezone.utc).date()
        run_daily_incremental_sync(lookback_days_ads=14)

        expected_from = str(today - timedelta(days=14))
        expected_to   = str(today)
        assert captured["campaigns"][0] == expected_from
        assert captured["campaigns"][1] == expected_to

    def test_contacts_window(self, monkeypatch):
        captured_calls: list = []

        def fake_contacts_range(date_from, date_to):
            captured_calls.append((date_from, date_to))
            return []

        _patch_all_datasets_success(monkeypatch, contacts_pull=fake_contacts_range)

        from scheduler.incremental_sync import run_daily_incremental_sync
        import datetime as dt
        today = dt.datetime.now(tz=dt.timezone.utc).date()
        run_daily_incremental_sync(lookback_days_hubspot_contacts=7)

        # The first call to pull_paid_search_contacts_in_range is from the
        # hubspot/contacts sync (7-day window).  The second call is from the
        # hubspot/deals sync (30-day window, default).
        assert len(captured_calls) >= 1
        expected_from = str(today - timedelta(days=7))
        assert captured_calls[0][0] == expected_from


# ---------------------------------------------------------------------------
# Dataset failure isolation
# ---------------------------------------------------------------------------

class TestDatasetFailureIsolation:
    """A single failed dataset must not abort the rest of the sync."""

    def test_campaign_failure_does_not_abort_others(self, monkeypatch):
        # Campaigns will raise; other datasets succeed.
        def failing_campaign_pull(*args, **kwargs):
            raise RuntimeError("Windsor API error (test)")

        _patch_all_datasets_success(monkeypatch, campaign_pull=failing_campaign_pull)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        assert result["datasets"]["windsor/campaigns"]["status"] == "failed"
        # Other supported datasets should still have succeeded.
        assert result["datasets"]["windsor/keywords"]["status"] == "success"
        assert result["datasets"]["windsor/geo"]["status"] == "success"
        assert result["datasets"]["hubspot/contacts"]["status"] == "success"

    def test_errors_list_populated_on_failure(self, monkeypatch):
        def failing_pull(*args, **kwargs):
            raise RuntimeError("forced error")

        _patch_all_datasets_success(monkeypatch, campaign_pull=failing_pull)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        assert len(result["errors"]) >= 1
        assert any("windsor/campaigns" in e for e in result["errors"])

    def test_overall_status_partial_on_mixed_results(self, monkeypatch):
        def failing_pull(*args, **kwargs):
            raise RuntimeError("forced error")

        _patch_all_datasets_success(monkeypatch, campaign_pull=failing_pull)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        assert result["status"] == "partial"

    def test_overall_status_success_when_all_succeed(self, monkeypatch):
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        assert result["status"] == "success"

    def test_overall_status_failed_when_all_supported_fail(self, monkeypatch):
        def failing_pull(*args, **kwargs):
            raise RuntimeError("forced error")

        _patch_all_datasets_success(
            monkeypatch,
            campaign_pull=failing_pull,
            keyword_pull=failing_pull,
            geo_pull=failing_pull,
            contacts_pull=failing_pull,
            deals_contacts_pull=failing_pull,
            closed_won_pull=failing_pull,
            source_pull=failing_pull,
        )

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Persistence failure detection
# ---------------------------------------------------------------------------

class TestPersistenceFailure:
    """Pulled > 0 rows but written = 0 must mark the dataset failed."""

    def test_campaigns_zero_written_marks_failed(self, monkeypatch):
        def pull_ok(*args, **kwargs):
            return _fake_rows(5)

        def write_zero(*args, **kwargs):
            return 0  # 5 pulled, 0 written → persistence failure

        _patch_all_datasets_success(
            monkeypatch,
            campaign_pull=pull_ok,
            campaign_write=write_zero,
        )

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        assert result["datasets"]["windsor/campaigns"]["status"] == "failed"

    def test_contacts_zero_written_marks_failed(self, monkeypatch):
        def pull_ok(*args, **kwargs):
            return _fake_rows(3)

        def write_zero(*args, **kwargs):
            return 0

        _patch_all_datasets_success(
            monkeypatch,
            contacts_pull=pull_ok,
            contacts_write=write_zero,
        )

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        assert result["datasets"]["hubspot/contacts"]["status"] == "failed"


# ---------------------------------------------------------------------------
# Zero rows from successful pull
# ---------------------------------------------------------------------------

class TestZeroRowsSuccess:
    """Zero rows from a successful pull → status success, row_count=0."""

    def test_campaigns_zero_rows_success(self, monkeypatch):
        def pull_empty(*args, **kwargs):
            return []  # genuine empty window

        def write_zero(*args, **kwargs):
            return 0

        _patch_all_datasets_success(
            monkeypatch,
            campaign_pull=pull_empty,
            campaign_write=write_zero,
        )

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        ds = result["datasets"]["windsor/campaigns"]
        assert ds["status"] == "success"
        assert ds["rows_pulled"] == 0
        assert ds["rows_written"] == 0

    def test_contacts_zero_rows_success(self, monkeypatch):
        def pull_empty(*args, **kwargs):
            return []

        def write_zero(*args, **kwargs):
            return 0

        _patch_all_datasets_success(
            monkeypatch,
            contacts_pull=pull_empty,
            contacts_write=write_zero,
        )

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        ds = result["datasets"]["hubspot/contacts"]
        assert ds["status"] == "success"
        assert ds["rows_pulled"] == 0
        assert ds["rows_written"] == 0


# ---------------------------------------------------------------------------
# Unsupported datasets
# ---------------------------------------------------------------------------

class TestUnsupportedDatasets:
    """Skipped datasets must appear in summary with status='skipped'."""

    def test_search_terms_skipped(self, monkeypatch):
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        ds = result["datasets"]["windsor/search_terms"]
        assert ds["status"] == "skipped"
        assert "note" in ds
        assert "unsupported_by_current_connector" in ds["note"]

    def test_gclid_matches_persisted_not_skipped(self, monkeypatch):
        # PR-ADS-114: gclid/matches now has a real persistence path (closed-won
        # deals by closedate → gclid_attribution). With no closed-won deals it
        # is a legitimate zero-row success, never "skipped".
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        ds = result["datasets"]["gclid/matches"]
        assert ds["status"] == "success"
        assert "closed_won_deals_found" in ds

    def test_skipped_datasets_not_counted_in_failure(self, monkeypatch):
        """Skipped datasets must not contribute to 'failed' overall status."""
        _patch_all_datasets_success(monkeypatch)

        from scheduler.incremental_sync import run_daily_incremental_sync
        result = run_daily_incremental_sync()

        # With all supported datasets succeeding, status must be 'success'
        # even though search_terms and gclid/matches are skipped.
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Scheduler job ID registration
# ---------------------------------------------------------------------------

class TestSchedulerRegistration:
    """daily_incremental_sync must appear in the registered job IDs."""

    def test_job_id_in_registered_list(self):
        from api.scheduler import get_registered_job_ids
        ids = get_registered_job_ids()
        assert "daily_incremental_sync" in ids

    def test_job_id_in_schedule_descriptions(self):
        from api.scheduler import _SCHEDULE_DESCRIPTIONS
        assert "daily_incremental_sync" in _SCHEDULE_DESCRIPTIONS
        assert "09:00" in _SCHEDULE_DESCRIPTIONS["daily_incremental_sync"]
        assert "Asia/Amman" in _SCHEDULE_DESCRIPTIONS["daily_incremental_sync"]


# ---------------------------------------------------------------------------
# API endpoint — POST /run/incremental-sync status propagation
# ---------------------------------------------------------------------------

class TestApiEndpointStatus:
    """POST /run/incremental-sync must surface the sync result status honestly."""

    def _mock_check_admin(self, monkeypatch):
        """Bypass auth check for all /run/* tests."""
        monkeypatch.setattr(
            "api.server.check_admin_or_token",
            lambda *a, **kw: None,
        )

    def test_returns_partial_status_when_sync_partial(self, monkeypatch):
        self._mock_check_admin(monkeypatch)
        monkeypatch.setattr(
            "scheduler.incremental_sync.run_daily_incremental_sync",
            lambda **kw: {
                "status": "partial",
                "run_type": "daily_incremental_sync",
                "datasets": {"windsor/campaigns": {"status": "failed"}},
                "errors": ["windsor/campaigns: test error"],
                "lookback": {},
            },
        )
        from fastapi.testclient import TestClient
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        res = client.post("/run/incremental-sync")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "partial"
        assert body["result"]["status"] == "partial"

    def test_returns_failed_status_when_sync_failed(self, monkeypatch):
        self._mock_check_admin(monkeypatch)
        monkeypatch.setattr(
            "scheduler.incremental_sync.run_daily_incremental_sync",
            lambda **kw: {
                "status": "failed",
                "run_type": "daily_incremental_sync",
                "datasets": {
                    "windsor/campaigns": {"status": "failed"},
                    "windsor/keywords":  {"status": "failed"},
                },
                "errors": ["windsor/campaigns: error", "windsor/keywords: error"],
                "lookback": {},
            },
        )
        from fastapi.testclient import TestClient
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        res = client.post("/run/incremental-sync")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "failed"
        assert body["result"]["status"] == "failed"

    def test_returns_success_status_when_sync_success(self, monkeypatch):
        self._mock_check_admin(monkeypatch)
        monkeypatch.setattr(
            "scheduler.incremental_sync.run_daily_incremental_sync",
            lambda **kw: {
                "status": "success",
                "run_type": "daily_incremental_sync",
                "datasets": {"windsor/campaigns": {"status": "success", "rows_pulled": 5, "rows_written": 5}},
                "errors": [],
                "lookback": {"ads_days": 14},
            },
        )
        from fastapi.testclient import TestClient
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        res = client.post("/run/incremental-sync")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "success"
        assert body["result"]["status"] == "success"

    def test_raw_error_strings_not_in_response(self, monkeypatch):
        """Per-dataset 'error' field must be stripped from the API response."""
        self._mock_check_admin(monkeypatch)
        monkeypatch.setattr(
            "scheduler.incremental_sync.run_daily_incremental_sync",
            lambda **kw: {
                "status": "partial",
                "run_type": "daily_incremental_sync",
                "datasets": {
                    "windsor/campaigns": {
                        "status": "failed",
                        "error": "SomeException: raw stack detail SECRET",
                    }
                },
                "errors": ["windsor/campaigns: SomeException: raw stack detail SECRET"],
                "lookback": {},
            },
        )
        from fastapi.testclient import TestClient
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        res = client.post("/run/incremental-sync")
        body_text = res.text
        # Raw error string must not appear in the response body
        assert "raw stack detail SECRET" not in body_text


# ---------------------------------------------------------------------------
# Config wiring — runtime defaults sourced from config/thresholds.yaml
# ---------------------------------------------------------------------------

class TestConfigWiring:
    """DEFAULT_LOOKBACK_* constants must match config/thresholds.yaml values."""

    def test_defaults_match_config(self):
        import yaml
        config_path = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        lookback = cfg["sync"]["daily_incremental"]["lookback_days"]

        import importlib
        import scheduler.incremental_sync as sync_mod
        # Reload to pick up current config
        importlib.reload(sync_mod)

        assert sync_mod.DEFAULT_LOOKBACK_ADS == lookback["ads"]
        assert sync_mod.DEFAULT_LOOKBACK_HUBSPOT_CONTACTS == lookback["hubspot_contacts"]
        assert sync_mod.DEFAULT_LOOKBACK_HUBSPOT_DEALS == lookback["hubspot_deals"]


# ---------------------------------------------------------------------------
# No unsafe external action wording
# ---------------------------------------------------------------------------

class TestNoUnsafeWording:
    """Module source must not contain unsafe external-action instructions."""

    UNSAFE_PATTERNS = [
        "push negative",
        "apply negative",
        "pause campaign",
        "block term",
        "send to Google Ads",
        "send to HubSpot",
        "upload conversion",
        "change bid",
        "change budget",
        "mutate",
    ]

    def test_incremental_sync_no_unsafe_wording(self):
        module_path = Path(__file__).resolve().parents[1] / "scheduler" / "incremental_sync.py"
        source = module_path.read_text(encoding="utf-8").lower()
        for pattern in self.UNSAFE_PATTERNS:
            # Allow "mutate" only in doctrine-disclaimer comments, not as code calls
            if pattern == "mutate":
                # Ensure no live code calls mutate — only docstring/comment mentions OK
                import ast
                tree = ast.parse(module_path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        func = node.func
                        if isinstance(func, ast.Attribute) and "mutate" in func.attr.lower():
                            pytest.fail(f"Found live mutate() call in incremental_sync.py")
            else:
                assert pattern.lower() not in source, (
                    f"Unsafe wording found in incremental_sync.py: '{pattern}'"
                )


# ---------------------------------------------------------------------------
# Shared patcher
# ---------------------------------------------------------------------------

def _patch_all_datasets_success(
    monkeypatch,
    *,
    campaign_pull=None,
    campaign_write=None,
    keyword_pull=None,
    keyword_write=None,
    geo_pull=None,
    geo_write=None,
    contacts_pull=None,
    contacts_write=None,
    deals_contacts_pull=None,
    deals_write=None,
    closed_won_pull=None,
    source_pull=None,
) -> None:
    """Patch all connector and writer calls with safe fake implementations.

    Callers can override individual callables to test specific failure modes.
    """
    rows3 = _fake_rows(3)

    def _default_pull(*args, **kwargs):
        return _fake_rows(3)

    def _default_write(*args, **kwargs):
        return 3

    def _default_deals_pull(contacts, *args, **kwargs):
        return _fake_rows(2)

    # Windsor connector
    monkeypatch.setattr(
        "connectors.windsor_pull.pull_campaign_performance_range",
        campaign_pull or _default_pull,
    )
    monkeypatch.setattr(
        "connectors.windsor_pull.pull_keyword_performance_range",
        keyword_pull or _default_pull,
    )
    monkeypatch.setattr(
        "connectors.windsor_pull.pull_geo_performance_range",
        geo_pull or _default_pull,
    )

    # HubSpot connector
    # contacts_pull is used by both the hubspot/contacts sync and as the
    # contact-fetch step within the hubspot/deals sync.
    contacts_pull_fn = contacts_pull or _default_pull
    if deals_contacts_pull is not None:
        # When a separate deals-contacts pull is specified, use it only if no
        # contacts_pull override was given.
        contacts_pull_fn = contacts_pull or deals_contacts_pull or _default_pull
    monkeypatch.setattr(
        "connectors.hubspot_pull.pull_paid_search_contacts_in_range",
        contacts_pull_fn,
    )
    monkeypatch.setattr(
        "connectors.hubspot_pull.pull_deals_with_gclid",
        _default_deals_pull,
    )
    # PR-ADS-114: gclid/matches now pulls closed-won deals by closedate.
    monkeypatch.setattr(
        "connectors.hubspot_pull.pull_closed_won_deals_in_range",
        closed_won_pull or (lambda *a, **kw: []),
    )
    # PR-ADS-117: daily source-classification step.
    monkeypatch.setattr(
        "connectors.hubspot_pull.pull_all_contacts_in_range",
        source_pull or (lambda *a, **kw: []),
    )
    monkeypatch.setattr(
        "connectors.hubspot_pull.pull_closed_won_deals_with_sources_in_range",
        source_pull or (lambda *a, **kw: []),
    )
    monkeypatch.setattr("db.writers.upsert_contact_source_classification", lambda *a, **kw: 0)
    monkeypatch.setattr("db.writers.upsert_deal_source_attribution", lambda *a, **kw: 0)
    # PR-ADS-118: daily canonical Google Ads spend step (patched at the service
    # seam so tests never import the google-ads SDK).
    monkeypatch.setattr(
        "services.google_ads_spend_service.fetch_daily_spend",
        source_pull and (lambda *a, **kw: source_pull()) or (lambda *a, **kw: {
            "customer_id": "123", "currency_code": "USD",
            "source_query_version": "campaign_daily_v1", "rows": []}),
    )
    monkeypatch.setattr("db.writers.upsert_campaign_daily_spend", lambda *a, **kw: 0)
    monkeypatch.setattr("db.writers.upsert_spend_coverage", lambda *a, **kw: True)
    # PR-ADS-120: account-daily reconciliation fetch (best-effort on success).
    monkeypatch.setattr("services.google_ads_spend_service.fetch_account_daily_spend",
                        lambda *a, **kw: {"rows": []})
    monkeypatch.setattr("db.writers.upsert_account_daily_spend", lambda *a, **kw: 0)
    # PR-ADS-119: daily FX rates step. Patch ensure_fx_rates so the FX connector
    # (network) is never hit. Mirror the spend failure mode (source_pull) so the
    # all-datasets-fail test still drives this dataset to failure.
    def _fx_stub(*a, **kw):
        if source_pull:
            source_pull()  # raises in the all-fail scenario
        return {"rows_written": 0, "fetched": 0, "failed": []}
    monkeypatch.setattr("services.fx_service.ensure_fx_rates", _fx_stub)

    # DB writers
    monkeypatch.setattr("db.writers.write_campaigns", campaign_write or _default_write)
    monkeypatch.setattr("db.writers.write_keywords", keyword_write or _default_write)
    monkeypatch.setattr("db.writers.write_geo", geo_write or _default_write)
    monkeypatch.setattr("db.writers.write_leads", contacts_write or _default_write)
    monkeypatch.setattr("db.writers.write_deals", deals_write or _default_write)
    monkeypatch.setattr("db.writers.write_gclid_attribution", lambda *a, **kw: 0)

    # PR-ADS-114: real local run record (returns a non-None run_id).
    monkeypatch.setattr("db.writers.write_run", lambda *a, **kw: 1)
    monkeypatch.setattr("db.writers.update_run", lambda *a, **kw: None)

    # Sync batch tracking — stub out to return a valid batch_id
    monkeypatch.setattr("db.writers.start_sync_batch", lambda *a, **kw: 1)
    monkeypatch.setattr("db.writers.finish_sync_batch", lambda *a, **kw: True)
