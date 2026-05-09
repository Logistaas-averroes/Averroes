"""
tests/test_monitoring_status.py

PR-ADS-069 — Unit tests for monitoring status computation logic.

Tests cover:
  - All latest runs successful → severity green, no warnings.
  - Weekly fails twice consecutively → warning generated, severity red.
  - No successful weekly run for > threshold → stale warning.
  - Missing run history does not crash.
  - Response is read-only and contains no action instruction.
  - Single failure below threshold → yellow, not red.

Run with:
  python -m pytest tests/test_monitoring_status.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.monitoring import compute_monitoring_status as _compute_monitoring_status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STALE_DAYS = {"daily": 2, "weekly": 8, "monthly": 35}
_CONSEC_WARNING = 2


def _ts(days_ago: float) -> str:
    """Return a UTC ISO-8601 timestamp for `days_ago` days before now."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(run_type: str, status: str, days_ago: float) -> dict:
    """Build a minimal run record dict for testing."""
    ts = _ts(days_ago)
    return {
        "run_type": run_type,
        "status": status,
        "started_at": ts,
        "finished_at": ts,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAllSuccess:
    """All three run types have a recent success — expect green, no warnings."""

    def test_severity_green(self):
        runs = [
            _run("daily",   "success", 0.5),
            _run("weekly",  "success", 1.0),
            _run("monthly", "success", 2.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["severity"] == "green"

    def test_no_warnings(self):
        runs = [
            _run("daily",   "success", 0.5),
            _run("weekly",  "success", 1.0),
            _run("monthly", "success", 2.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["warnings"] == []

    def test_status_ok(self):
        runs = [_run("daily", "success", 0.1)]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["status"] == "ok"

    def test_consecutive_failures_zero(self):
        runs = [
            _run("daily",   "success", 0.5),
            _run("weekly",  "success", 1.0),
            _run("monthly", "success", 2.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        for rt in ("daily", "weekly", "monthly"):
            assert result["latest_runs"][rt]["consecutive_failures"] == 0

    def test_not_stale(self):
        runs = [
            _run("daily",   "success", 1.0),
            _run("weekly",  "success", 3.0),
            _run("monthly", "success", 10.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        for rt in ("daily", "weekly", "monthly"):
            assert result["latest_runs"][rt]["stale"] is False


class TestWeeklyConsecutiveFailures:
    """Weekly run fails twice in a row → warning, severity red."""

    def _runs(self):
        return [
            _run("weekly", "failed", 0.5),
            _run("weekly", "failed", 1.5),
            _run("weekly", "success", 5.0),
            _run("daily",  "success", 0.3),
            _run("monthly","success", 3.0),
        ]

    def test_severity_red(self):
        result = _compute_monitoring_status(self._runs(), _STALE_DAYS, _CONSEC_WARNING)
        assert result["severity"] == "red"

    def test_warning_message_present(self):
        result = _compute_monitoring_status(self._runs(), _STALE_DAYS, _CONSEC_WARNING)
        assert any("weekly" in w.lower() for w in result["warnings"])

    def test_consecutive_failure_count(self):
        result = _compute_monitoring_status(self._runs(), _STALE_DAYS, _CONSEC_WARNING)
        assert result["latest_runs"]["weekly"]["consecutive_failures"] == 2

    def test_last_status_failed(self):
        result = _compute_monitoring_status(self._runs(), _STALE_DAYS, _CONSEC_WARNING)
        assert result["latest_runs"]["weekly"]["last_status"] == "failed"

    def test_last_success_at_is_populated(self):
        result = _compute_monitoring_status(self._runs(), _STALE_DAYS, _CONSEC_WARNING)
        # The last successful weekly run was 5 days ago — should be populated.
        assert result["latest_runs"]["weekly"]["last_success_at"] is not None


class TestSingleFailureBelowThreshold:
    """One failure — below the consecutive threshold → yellow not red."""

    def test_severity_yellow_not_red(self):
        runs = [
            _run("weekly", "failed",  0.5),
            _run("weekly", "success", 2.0),
            _run("daily",  "success", 0.3),
            _run("monthly","success", 3.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        # 1 failure < threshold of 2, but weekly is stale (last success 2 days ago,
        # threshold is 8 — actually not stale). Check severity is NOT red.
        assert result["severity"] != "red"

    def test_no_consecutive_failure_warning(self):
        runs = [
            _run("weekly", "failed",  0.5),
            _run("weekly", "success", 2.0),
            _run("daily",  "success", 0.3),
            _run("monthly","success", 3.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert not any("2 times" in w or "times in a row" in w for w in result["warnings"])


class TestStaleWeekly:
    """Weekly last success is older than the threshold → stale warning."""

    def test_stale_flag_set(self):
        runs = [
            _run("weekly",  "success", 9.0),   # 9 days ago, threshold is 8
            _run("daily",   "success", 0.5),
            _run("monthly", "success", 3.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["latest_runs"]["weekly"]["stale"] is True

    def test_stale_warning_message(self):
        runs = [
            _run("weekly",  "success", 9.0),
            _run("daily",   "success", 0.5),
            _run("monthly", "success", 3.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert any("weekly" in w.lower() and "stale" in w.lower() for w in result["warnings"])

    def test_severity_at_least_yellow(self):
        runs = [
            _run("weekly",  "success", 9.0),
            _run("daily",   "success", 0.5),
            _run("monthly", "success", 3.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["severity"] in ("yellow", "red")


class TestMissingRunHistory:
    """Empty or missing run history does not crash the function."""

    def test_empty_runs_does_not_crash(self):
        result = _compute_monitoring_status([], _STALE_DAYS, _CONSEC_WARNING)
        assert "status" in result
        assert "severity" in result
        assert "latest_runs" in result
        assert "warnings" in result

    def test_empty_runs_all_types_present(self):
        result = _compute_monitoring_status([], _STALE_DAYS, _CONSEC_WARNING)
        for rt in ("daily", "weekly", "monthly"):
            assert rt in result["latest_runs"]

    def test_none_type_runs_ignored(self):
        # Records with unknown run_type should not crash.
        runs = [{"run_type": "unknown_type", "status": "success", "started_at": _ts(0.5), "finished_at": _ts(0.5)}]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["status"] == "ok"

    def test_malformed_timestamp_does_not_crash(self):
        runs = [{"run_type": "daily", "status": "success", "started_at": "not-a-date", "finished_at": "not-a-date"}]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["status"] == "ok"


class TestReadOnlyContract:
    """Response must be read-only — no action instruction wording."""

    _FORBIDDEN = (
        "auto retry",
        "auto rerun",
        "push negative",
        "apply negative",
        "pause campaign",
        "send to google ads",
        "send to hubspot",
        "upload conversion",
        "change bid",
        "change budget",
    )

    def _result_text(self, runs):
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        return " ".join(result.get("warnings", [])).lower()

    def test_no_action_wording_green(self):
        runs = [_run("daily", "success", 0.5), _run("weekly", "success", 1.0), _run("monthly", "success", 2.0)]
        text = self._result_text(runs)
        for phrase in self._FORBIDDEN:
            assert phrase not in text, f"Forbidden phrase found: {phrase!r}"

    def test_no_action_wording_warnings(self):
        runs = [_run("weekly", "failed", 0.5), _run("weekly", "failed", 1.5)]
        text = self._result_text(runs)
        for phrase in self._FORBIDDEN:
            assert phrase not in text, f"Forbidden phrase found: {phrase!r}"

    def test_response_has_no_mutation_fields(self):
        runs = [_run("daily", "success", 0.5)]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        mutation_keys = {"action", "retry", "rerun", "push", "upload", "write"}
        assert not mutation_keys.intersection(result.keys())


class TestPartialStatus:
    """'partial' runs are treated as successful for monitoring purposes."""

    def test_partial_resets_consecutive_failures(self):
        runs = [
            _run("daily", "failed",  0.3),
            _run("daily", "partial", 1.0),
            _run("daily", "failed",  2.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        # Only 1 consecutive failure at the top (newest).
        assert result["latest_runs"]["daily"]["consecutive_failures"] == 1

    def test_partial_counts_as_last_success(self):
        runs = [
            _run("daily", "failed",  0.3),
            _run("daily", "partial", 1.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["latest_runs"]["daily"]["last_success_at"] is not None
