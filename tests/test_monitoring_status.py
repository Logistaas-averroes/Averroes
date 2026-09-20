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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from api.monitoring import compute_monitoring_status as _compute_monitoring_status
from api.monitoring import monitoring_cadence as _monitoring_cadence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STALE_DAYS = {"daily": 2, "daily_incremental_sync": 2, "weekly": 8, "monthly": 35}
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


def _other_cadences_healthy(*, exclude: str = "") -> list[dict]:
    """A recent success for every monitored cadence except `exclude`.

    PR-ADS-160-F2. Production registers FOUR jobs (`api/scheduler.py:49`), so a
    fixture carrying only three cadences is not a healthy system — it is a
    system with a pipeline missing, and monitoring now says so. Tests that mean
    "everything is fine except the thing under test" have to supply the whole
    board, the way production does.
    """
    ages = {"daily": 0.5, "daily_incremental_sync": 0.4,
            "weekly": 1.0, "monthly": 2.0}
    return [_run(rt, "success", age) for rt, age in ages.items()
            if rt != exclude]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAllSuccess:
    """All three run types have a recent success — expect green, no warnings."""

    def test_severity_green(self):
        runs = _other_cadences_healthy()
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["severity"] == "green"

    def test_no_warnings(self):
        runs = _other_cadences_healthy()
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        assert result["warnings"] == []

    def test_a_missing_pipeline_is_not_a_healthy_system(self):
        """The control for the helper above, and a contract in its own right.

        Drop any ONE registered cadence and the verdict must stop being green.
        A fixture that silently omits a pipeline would otherwise let this whole
        class assert health over a system that is missing one.
        """
        for cadence in ("daily", "daily_incremental_sync", "weekly", "monthly"):
            result = _compute_monitoring_status(
                _other_cadences_healthy(exclude=cadence),
                _STALE_DAYS, _CONSEC_WARNING)
            assert result["severity"] != "green", cadence
            assert any("run found in history" in w for w in result["warnings"]), \
                (cadence, result["warnings"])

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
    """'partial' is a THIRD outcome — neither a failure nor a success.

    PR-ADS-160 (fourth review). This class previously asserted that partial was
    "treated as successful for monitoring purposes", which made a pipeline
    producing nothing but partial runs look perfectly healthy: green severity,
    no warning, and a freshness clock ticking over on runs that never finished
    what they set out to do.

    The distinction now lives in two places, answering two different questions.
    """

    def test_partial_is_not_a_failure_and_breaks_a_failure_streak(self):
        """Something ran and something landed, so it does not count toward red."""
        runs = [
            _run("daily", "failed",  0.3),
            _run("daily", "partial", 1.0),
            _run("daily", "failed",  2.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        # Only 1 consecutive failure at the top (newest).
        assert result["latest_runs"]["daily"]["consecutive_failures"] == 1

    def test_partial_does_not_advance_the_proven_complete_claim(self):
        """`last_success_at` is the coverage claim staleness is measured against.

        A run that stopped short has proven nothing about coverage, so it must
        not reset that clock — otherwise an endlessly-partial pipeline reports
        itself fresh forever.
        """
        runs = [
            _run("daily", "failed",  0.3),
            _run("daily", "partial", 1.0),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        daily = result["latest_runs"]["daily"]

        assert daily["last_success_at"] is None, (
            "a partial run advanced the proven-complete coverage claim")
        # Nothing is hidden: "when did this pipeline last do anything" is still
        # answerable, under a name that does not say success.
        assert daily["last_completed_at"] is not None

    def test_a_partial_latest_run_is_never_green(self):
        """The whole point. Recent, no failures, nothing stale — still not green.

        Without this, the one outcome an operator can miss entirely is the one
        that leaves fresh-looking data behind.
        """
        runs = [
            _run("daily",   "partial", 0.1),
            _run("daily",   "success", 1.0),
            _run("weekly",  "success", 0.5),
            _run("monthly", "success", 0.5),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)

        assert result["latest_runs"]["daily"]["latest_partial"] is True
        assert result["latest_runs"]["daily"]["last_status"] == "partial"
        assert result["latest_runs"]["daily"]["stale"] is False, (
            "control: a recent success means staleness is NOT what fires here")
        assert result["severity"] == "yellow"
        assert result["severity"] != "green"
        assert any("partially" in w for w in result["warnings"]), result["warnings"]

    def test_partial_is_not_red_either(self):
        """Real work landed and the pipeline is not down. Yellow, not red."""
        runs = [
            _run("daily",   "partial", 0.1),
            _run("daily",   "partial", 1.0),
            _run("weekly",  "success", 0.5),
            _run("monthly", "success", 0.5),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)

        assert result["latest_runs"]["daily"]["consecutive_failures"] == 0
        assert result["severity"] == "yellow"

    def test_success_is_still_green(self):
        """The positive control. A guard that reddens everything is not a guard."""
        runs = _other_cadences_healthy()
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)

        assert result["severity"] == "green"
        assert result["warnings"] == []
        assert result["latest_runs"]["daily"]["latest_partial"] is False
        assert result["latest_runs"]["daily"]["last_success_at"] is not None

    def test_failed_is_still_red_at_the_threshold(self):
        """And failure keeps its own, more severe answer."""
        runs = [
            _run("daily",   "failed",  0.1),
            _run("daily",   "failed",  1.0),
            _run("weekly",  "success", 0.5),
            _run("monthly", "success", 0.5),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)

        assert result["latest_runs"]["daily"]["consecutive_failures"] == 2
        assert result["severity"] == "red"
        assert result["latest_runs"]["daily"]["latest_partial"] is False

    def test_the_three_outcomes_stay_distinguishable(self):
        """success ≠ partial ≠ failed, on every field a reader consumes."""
        seen = {}
        for status in ("success", "partial", "failed"):
            runs = ([_run("daily", status, 0.1)]
                    + _other_cadences_healthy(exclude="daily"))
            r = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
            seen[status] = (r["severity"],
                            r["latest_runs"]["daily"]["last_status"],
                            r["latest_runs"]["daily"]["latest_partial"],
                            r["latest_runs"]["daily"]["last_success_at"] is not None)

        assert seen["success"] == ("green", "success", False, True)
        assert seen["partial"][0] == "yellow"
        assert seen["partial"][1:] == ("partial", True, False)
        assert seen["failed"][0] in ("yellow", "red")
        assert seen["failed"][1:] == ("failed", False, False)
        assert len({v[:2] for v in seen.values()}) == 3, (
            "two of the three outcomes are indistinguishable to a reader")


class TestRunTypeIdentity:
    """PR-ADS-160-F1 — the cadence a concrete run type reports into.

    Monitoring reports on three CADENCES; production emits concrete RUN TYPES,
    and they are not spelled the same. The incremental sync writes
    `daily_incremental_sync` (`scheduler.incremental_sync.RUN_TYPE`), which is
    the durable value in the `runs` table.

    Grouping used to match the cadence names literally, so every real
    incremental row was discarded before any of the partial/failed logic ran.
    The failure was silent and inverted: the daily bucket warned "No daily run
    found in history" — a complaint about ABSENCE — while daily runs were
    happening and their outcomes were invisible.
    """

    def test_the_real_production_run_type_maps_to_its_own_cadence(self):
        """The defect, stated as the mapping it needed.

        PR-ADS-160-F2: the cadence is the incremental sync's OWN, not the
        legacy pulse's. See `TestCadenceIsOnePipeline` for why sharing one was
        worse than being discarded.
        """
        from scheduler.incremental_sync import RUN_TYPE

        # Imported, not retyped: if the scheduler ever renames its run type this
        # fails here rather than going quietly unmonitored in production.
        assert RUN_TYPE == "daily_incremental_sync"
        assert _monitoring_cadence(RUN_TYPE) == "daily_incremental_sync"
        assert _monitoring_cadence("daily") == "daily"
        assert _monitoring_cadence(RUN_TYPE) != _monitoring_cadence("daily"), (
            "two independent pipelines sharing one cadence share one verdict, "
            "and the healthier one wins it")

    @pytest.mark.parametrize("status,expected_severity", [
        ("success", "green"),
        ("partial", "yellow"),
        ("failed", "yellow"),      # one failure is yellow; red needs the streak
    ])
    def test_a_real_incremental_run_reaches_monitoring(
            self, status, expected_severity):
        """All three outcomes, under the REAL run type, in the real population.

        The rest of the board is healthy — including the 06:00 pulse, which
        production runs alongside this pipeline. Before PR-ADS-160-F2 that
        neighbour is what made this test pass for the wrong reason.
        """
        runs = ([_run("daily_incremental_sync", status, 0.1)]
                + _other_cadences_healthy(exclude="daily_incremental_sync"))
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)
        incremental = result["latest_runs"]["daily_incremental_sync"]

        assert incremental["last_status"] == status, (
            "the real incremental run never reached monitoring")
        assert incremental["latest_partial"] is (status == "partial")
        assert result["severity"] == expected_severity
        # And it is NOT the "nothing found" complaint the F1 defect produced.
        assert not any("run found in history" in w for w in result["warnings"]), \
            result["warnings"]
        # The pulse's own verdict is untouched by its neighbour's outcome.
        assert result["latest_runs"]["daily"]["last_status"] == "success"

    def test_two_failed_incremental_runs_still_go_red(self):
        """Failure semantics survive the mapping — the streak still reaches red."""
        runs = ([
            _run("daily_incremental_sync", "failed", 0.1),
            _run("daily_incremental_sync", "failed", 1.0),
        ] + _other_cadences_healthy(exclude="daily_incremental_sync"))
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)

        assert result["latest_runs"]["daily_incremental_sync"][
            "consecutive_failures"] == 2
        assert result["severity"] == "red"

    def test_a_partial_incremental_run_does_not_advance_the_success_clock(self):
        """The §4 contract, now reachable by the run type that needed it."""
        runs = ([_run("daily_incremental_sync", "partial", 0.1)]
                + _other_cadences_healthy(exclude="daily_incremental_sync"))
        incremental = _compute_monitoring_status(
            runs, _STALE_DAYS,
            _CONSEC_WARNING)["latest_runs"]["daily_incremental_sync"]

        assert incremental["last_success_at"] is None
        assert incremental["last_completed_at"] is not None

    def test_weekly_and_monthly_are_untouched(self):
        """The mapping must not disturb the cadences that already worked."""
        runs = [
            _run("weekly",  "failed",  0.5),
            _run("monthly", "partial", 0.5),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)

        assert result["latest_runs"]["weekly"]["last_status"] == "failed"
        assert result["latest_runs"]["monthly"]["last_status"] == "partial"
        assert result["latest_runs"]["monthly"]["latest_partial"] is True
        assert _monitoring_cadence("weekly") == "weekly"
        assert _monitoring_cadence("monthly") == "monthly"

    def test_an_unknown_run_type_never_silently_becomes_daily(self):
        """Unknown is not daily, the same way unknown is not zero.

        Folding an unrecognised run type into `daily` would let a new scheduler
        start voting on — and reddening — the daily cadence's health without
        anyone deciding its health belongs there.
        """
        assert _monitoring_cadence("backfill") is None
        assert _monitoring_cadence("some_future_sync") is None
        assert _monitoring_cadence("") is None
        assert _monitoring_cadence(None) is None

        runs = [
            _run("backfill", "failed", 0.1),
            _run("weekly",  "success", 0.5),
            _run("monthly", "success", 0.5),
        ]
        result = _compute_monitoring_status(runs, _STALE_DAYS, _CONSEC_WARNING)

        # It did not join the daily bucket, and it did not red the run.
        assert result["latest_runs"]["daily"]["last_status"] is None
        assert result["latest_runs"]["daily"]["consecutive_failures"] == 0
        assert result["severity"] != "red"

    def test_the_mapping_is_one_table_rather_than_scattered_comparisons(self):
        """Every monitored cadence is reachable, and the table is the contract."""
        from api.monitoring import MONITORING_CADENCES, RUN_TYPE_CADENCE

        assert set(RUN_TYPE_CADENCE.values()) <= set(MONITORING_CADENCES)
        assert set(MONITORING_CADENCES) == set(RUN_TYPE_CADENCE.values()), (
            "a monitored cadence no concrete run type maps to would report "
            "'no run found' forever")
        for cadence in MONITORING_CADENCES:
            assert _monitoring_cadence(cadence) == cadence, (
                "the cadence names must remain valid run types in their own "
                "right — historical rows are spelled that way")
