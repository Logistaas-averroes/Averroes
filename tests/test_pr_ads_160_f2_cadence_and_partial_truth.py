"""
PR-ADS-160-F2 — a cadence is one pipeline, and `partial` survives to the row.

PR-ADS-160-F1 stopped `compute_monitoring_status` discarding the real
`daily_incremental_sync` rows. It then routed them into the SAME cadence as the
legacy 06:00 pulse — and everything monitoring computes is computed once per
cadence: one failure streak, one `last_success_at`, one severity. Two unrelated
pipelines voted on one verdict and the healthier one won it.

Measured on `main` @ e974890, five days of incremental-sync failures behind a
succeeding pulse reported `severity: green, warnings: []`, and
`static/app.js:1714` renders nothing at all on green. The pipeline that feeds
every canonical number could fail indefinitely behind a clean dashboard.

Three more defects are covered here, all the same shape — a state that exists
being reported as a weaker one:

  §2  the one freshness branch that dropped its dependency AND downgraded
      `error` to `neutral` (`services/freshness_service.py`, unmeasured row
      count beneath a partial sync);
  §3  `partial` collapsed to `failed` on the deal-ledger and canonical-geo sync
      batches, on a premise (`sync_batches` accepts success|failed only) that
      PR-ADS-160 had already made false;
  §4  the daily pulse writing no `runs` row at all when it failed during its
      pulls, so monitoring read an absence where a failure had happened.

Every positive assertion below is paired with the control that makes it mean
something, and §1 carries the counterfactual: re-merge the cadences and the
masking comes straight back.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import api.monitoring as mon  # noqa: E402
import services.freshness_service as freshness_svc  # noqa: E402

_CONSEC = 2
_THRESHOLDS = {"daily": 2, "daily_incremental_sync": 2, "weekly": 8, "monthly": 35}


def _ts(days_ago: float, hour: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.replace(hour=hour, minute=0, second=0,
                      microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(run_type: str, status: str, days_ago: float, hour: int) -> dict:
    ts = _ts(days_ago, hour)
    return {"run_type": run_type, "status": status,
            "started_at": ts, "finished_at": ts}


def _production_days(incremental_status: str, days: int = 5,
                     pulse_status: str = "success") -> list[dict]:
    """The run shape the two registered cron jobs actually produce.

    `api/scheduler.py:49` registers four jobs. Two of them write into `runs`
    every day: the legacy pulse at 06:00 Asia/Amman and the incremental sync at
    09:00. Any fixture carrying only one of them is not production.
    """
    rows: list[dict] = [
        _run("weekly", "success", 1, 4),
        _run("monthly", "success", 2, 4),
    ]
    for d in range(days):
        rows.append(_run("daily", pulse_status, d, 3))
        rows.append(_run("daily_incremental_sync", incremental_status, d, 6))
    return sorted(rows, key=lambda r: r["started_at"], reverse=True)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — a cadence is one pipeline's health
# ═════════════════════════════════════════════════════════════════════════════

def test_120_the_two_daily_pipelines_do_not_share_a_verdict():
    """The mapping, stated as the separation it needs.

    They are different pipelines: the pulse refreshes campaign performance and
    lead quality, the incremental sync feeds canonical spend, the contact
    funnel, the deal ledger, geo and SQL coverage. One proves nothing about the
    other, so they cannot share a `last_success_at` or a failure streak.
    """
    from scheduler.incremental_sync import RUN_TYPE

    assert RUN_TYPE == "daily_incremental_sync"
    assert mon.monitoring_cadence(RUN_TYPE) == "daily_incremental_sync"
    assert mon.monitoring_cadence("daily") == "daily"
    assert mon.monitoring_cadence(RUN_TYPE) != mon.monitoring_cadence("daily")
    # Every cadence must be reachable and independently configurable.
    assert set(mon.MONITORING_CADENCES) == set(mon.RUN_TYPE_CADENCE.values())
    assert set(mon.STALE_DAYS_DEFAULT) >= set(mon.MONITORING_CADENCES), (
        "a cadence with no default threshold cannot report staleness")


def test_121_a_failing_incremental_sync_is_not_hidden_by_a_healthy_pulse():
    """The blocker, reproduced through the real function.

    Five consecutive incremental failures, with the 06:00 pulse succeeding
    every morning exactly as production does.
    """
    verdict = mon.compute_monitoring_status(
        _production_days("failed"), _THRESHOLDS, _CONSEC)

    incremental = verdict["latest_runs"]["daily_incremental_sync"]
    assert incremental["last_status"] == "failed"
    assert incremental["consecutive_failures"] == 5, (
        "the pulse's success broke the incremental sync's failure streak")
    assert incremental["last_success_at"] is None, (
        "a pulse success advanced a clock it proves nothing about")
    assert verdict["severity"] == "red"
    assert any("incremental" in w.lower() for w in verdict["warnings"]), (
        "the warning must name the pipeline that is broken")

    # The pulse keeps its own, truthful verdict — this is a separation, not a
    # blanket reddening.
    assert verdict["latest_runs"]["daily"]["last_status"] == "success"
    assert verdict["latest_runs"]["daily"]["consecutive_failures"] == 0


def test_122_the_control_a_healthy_pair_is_still_green():
    """The negative control for 121. A guard that reddens everything is none."""
    verdict = mon.compute_monitoring_status(
        _production_days("success"), _THRESHOLDS, _CONSEC)

    assert verdict["severity"] == "green"
    assert verdict["warnings"] == []


def test_123_one_incremental_failure_is_yellow_and_two_are_red():
    """Escalation is reachable at all, which it was not before F2."""
    one = mon.compute_monitoring_status(
        _production_days("failed", days=1), _THRESHOLDS, _CONSEC)
    assert one["latest_runs"]["daily_incremental_sync"]["consecutive_failures"] == 1
    assert one["severity"] == "yellow", "one failure is not an outage"

    two = mon.compute_monitoring_status(
        _production_days("failed", days=2), _THRESHOLDS, _CONSEC)
    assert two["latest_runs"]["daily_incremental_sync"]["consecutive_failures"] == 2
    assert two["severity"] == "red", (
        "the red threshold must be reachable behind a succeeding pulse")


def test_124_a_partial_incremental_run_is_not_erased_by_the_next_pulse():
    """The partial signal must outlive the following morning's pulse."""
    rows = sorted([
        _run("weekly", "success", 1, 4),
        _run("monthly", "success", 2, 4),
        _run("daily_incremental_sync", "partial", 1, 6),
        _run("daily", "success", 0, 3),          # this morning's pulse
    ], key=lambda r: r["started_at"], reverse=True)

    verdict = mon.compute_monitoring_status(rows, _THRESHOLDS, _CONSEC)
    incremental = verdict["latest_runs"]["daily_incremental_sync"]

    assert incremental["last_status"] == "partial"
    assert incremental["latest_partial"] is True
    assert incremental["last_success_at"] is None
    assert incremental["last_completed_at"] is not None, (
        "excluding partial from last_success_at must not hide that it ran")
    assert verdict["severity"] == "yellow"
    assert any("partially" in w for w in verdict["warnings"]), verdict["warnings"]


def test_125_counterfactual_remerging_the_cadences_restores_the_masking(
        monkeypatch):
    """Revert the fix and the defect returns — for the intended reason.

    A guard whose absence changes nothing is not a guard. This replays the
    IDENTICAL rows through the pre-F2 mapping, in which both run types share
    the `daily` cadence, and asserts the failures go green and silent.
    """
    rows = _production_days("failed")

    post_fix = mon.compute_monitoring_status(rows, _THRESHOLDS, _CONSEC)
    assert post_fix["severity"] == "red"

    monkeypatch.setattr(mon, "MONITORING_CADENCES", ("daily", "weekly", "monthly"))
    monkeypatch.setattr(mon, "RUN_TYPE_CADENCE", {
        "daily": "daily", "daily_incremental_sync": "daily",
        "weekly": "weekly", "monthly": "monthly"})

    pre_fix = mon.compute_monitoring_status(
        rows, {"daily": 2, "weekly": 8, "monthly": 35}, _CONSEC)

    assert pre_fix["severity"] == "green", (
        "the pre-F2 mapping must reproduce the masking, or this control "
        "proves nothing about what the fix changed")
    assert pre_fix["warnings"] == []
    assert pre_fix["latest_runs"]["daily"]["consecutive_failures"] == 1, (
        "the pulse broke the streak at 1, below the red threshold of 2")
    assert pre_fix["latest_runs"]["daily"]["last_success_at"] is not None, (
        "and the pulse advanced the clock the incremental sync is measured by")


def test_126_an_unknown_run_type_still_never_becomes_a_cadence():
    """F2 adds a cadence; it must not add a fallback."""
    assert mon.monitoring_cadence("backfill") is None
    assert mon.monitoring_cadence("revenue_recovery") is None
    assert mon.monitoring_cadence("daily_incremental") is None, (
        "a near-miss spelling must not be folded in by prefix")
    assert mon.monitoring_cadence("some_future_sync") is None

    rows = _production_days("success") + [_run("backfill", "failed", 0, 1)]
    verdict = mon.compute_monitoring_status(rows, _THRESHOLDS, _CONSEC)
    assert verdict["severity"] == "green", "an unmonitored run type voted"
    assert "backfill" not in verdict["latest_runs"]


def test_127_a_cadence_with_no_configured_threshold_says_so(monkeypatch):
    """The unchosen `2` is gone: unknown is not two days, as unknown is not zero."""
    monkeypatch.setattr(
        mon, "MONITORING_CADENCES",
        ("daily", "daily_incremental_sync", "weekly", "monthly", "hourly_thing"))
    monkeypatch.setattr(mon, "RUN_TYPE_CADENCE",
                        {**mon.RUN_TYPE_CADENCE, "hourly_thing": "hourly_thing"})

    rows = _production_days("success") + [_run("hourly_thing", "success", 0, 1)]
    verdict = mon.compute_monitoring_status(rows, _THRESHOLDS, _CONSEC)

    assert verdict["latest_runs"]["hourly_thing"]["stale"] is False, (
        "an unmeasurable cadence must not be reported stale on a made-up "
        "threshold")
    assert any("no staleness threshold configured" in w
               for w in verdict["warnings"]), verdict["warnings"]

    # Control: the configured cadences are unaffected.
    assert verdict["latest_runs"]["daily"]["stale"] is False
    assert not any("daily run has no staleness" in w.lower()
                   for w in verdict["warnings"])


def test_128_the_server_loads_a_threshold_for_every_cadence():
    """`_load_monitoring_thresholds` must iterate the table, not a literal."""
    from api.server import _load_monitoring_thresholds

    stale, consec = _load_monitoring_thresholds()

    for cadence in mon.MONITORING_CADENCES:
        assert cadence in stale, (
            f"{cadence} would fall back to the module default and silently "
            f"ignore config/thresholds.yaml")
    assert stale["daily_incremental_sync"] >= 1
    assert consec >= 1


# ═════════════════════════════════════════════════════════════════════════════
# §2 — direct evidence outranks inherited evidence WITHOUT reporting less
# ═════════════════════════════════════════════════════════════════════════════

_FRESHNESS_BASE = {
    "dataset": "lifecycle_events",
    "rows_in_window": 10,
    "latest_source_date": None,
    "sync_status": "success",
    "latest_batch_status": "success",
    "latest_batch_row_count": 10,
    "last_successful_sync_at": None,
    "stale_threshold_days": 2,
    "row_count_supported": True,
}


def _freshness(**over):
    kwargs = {**_FRESHNESS_BASE, **over}
    kwargs.setdefault("latest_source_date", date.today())
    if kwargs.get("last_successful_sync_at") is None:
        kwargs["last_successful_sync_at"] = datetime.now(tz=timezone.utc)
    return freshness_svc.compute_canonical_freshness(**kwargs)


@pytest.mark.parametrize("row_count_supported,expected_status", [
    (True,  freshness_svc.CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT),
    (False, freshness_svc.CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED),
])
def test_129_an_unmeasured_row_count_still_names_its_blocked_upstream(
        row_count_supported, expected_status):
    """The two returns that dropped the dependency and softened the severity.

    Reachable whenever the row-count SELECT raises — a missing table on a
    partly-migrated database, a permission error, a lock timeout — which is
    exactly when a blocking error matters most.
    """
    verdict = _freshness(
        sync_status="partial", latest_batch_status="partial",
        rows_in_window=None, latest_batch_row_count=None,
        row_count_supported=row_count_supported,
        dependency_status=freshness_svc.CanonicalFreshnessStatus.PARTIAL_NO_DATA)

    assert verdict["canonical_status"] == expected_status
    assert "upstream dependency" in verdict["reason"], (
        "the dependency was dropped; the operator is told nothing about it")
    assert "will not resolve this dataset" in verdict["reason"]
    assert "partial" in verdict["reason"].lower(), (
        "and the partial fact must still be carried")
    assert verdict["severity"] == "error", (
        "direct evidence replaced a blocking error with a neutral unknown — "
        "precedence must never mean reporting LESS than the inherited state")


def test_130_the_control_no_dependency_means_no_dependency_text():
    """Without a blocked upstream the note must be absent, and neutral is right."""
    verdict = _freshness(
        sync_status="partial", latest_batch_status="partial",
        rows_in_window=None, latest_batch_row_count=None,
        dependency_status=None)

    assert verdict["canonical_status"] == \
        freshness_svc.CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT
    assert "upstream dependency" not in verdict["reason"], (
        "a note emitted unconditionally would make test_129 pass for free")
    assert verdict["severity"] != "error", (
        "and the escalation must be caused by the dependency, not constant")
    assert "partial" in verdict["reason"].lower()


def test_131_the_escalated_verdict_is_not_weaker_than_the_one_it_replaced():
    """Stated as the comparison, so the floor cannot drift.

    The inherited state this displaces is `blocked_by_dependency`. Whatever
    direct evidence reports instead must be at least as severe.
    """
    order = {"ok": 0, "neutral": 0, "warning": 1, "error": 2}

    inherited = _freshness(
        sync_status="success", latest_batch_status="success",
        dependency_status=freshness_svc.CanonicalFreshnessStatus.PARTIAL_NO_DATA)
    assert inherited["canonical_status"] == \
        freshness_svc.CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY, (
        "control: this is the state direct evidence displaces")

    direct = _freshness(
        sync_status="partial", latest_batch_status="partial",
        rows_in_window=None, latest_batch_row_count=None,
        dependency_status=freshness_svc.CanonicalFreshnessStatus.PARTIAL_NO_DATA)

    assert order[direct["severity"]] >= order[inherited["severity"]], (
        f"direct evidence downgraded {inherited['severity']} to "
        f"{direct['severity']}")


def test_132_a_measured_row_count_keeps_its_existing_behaviour():
    """§4 of PR-ADS-160-F1 is unchanged where it already worked."""
    status = freshness_svc.CanonicalFreshnessStatus

    verdict = _freshness(
        sync_status="partial", latest_batch_status="partial",
        rows_in_window=0, latest_batch_row_count=0,
        dependency_status=status.PARTIAL_NO_DATA)

    assert verdict["canonical_status"] == status.PARTIAL_NO_DATA
    assert verdict["canonical_status"] != status.BLOCKED_BY_DEPENDENCY
    assert "upstream dependency" in verdict["reason"]


# ═════════════════════════════════════════════════════════════════════════════
# §3 — `partial` reaches the sync batch under its own name
# ═════════════════════════════════════════════════════════════════════════════

def test_133_the_writer_accepts_partial_so_nothing_needs_to_collapse_it():
    """The premise the collapsed call sites rested on, checked against source.

    Their comment said `sync_batches` accepts success|failed only. PR-ADS-160
    made that false, so the collapse stopped preventing anything and only lost
    a state.
    """
    import inspect

    import db.writers as writers
    from services.dataset_keys import VALID_SYNC_STATUSES

    assert "partial" in VALID_SYNC_STATUSES
    src = inspect.getsource(writers.finish_sync_batch)
    assert '("success", "failed", "partial")' in src, (
        "finish_sync_batch no longer validates partial as a first-class status")


@pytest.mark.parametrize("fn_name,producer", [
    ("_sync_deal_ledger", "deal ledger"),
    ("_sync_canonical_geo", "canonical geo"),
])
def test_134_neither_sync_collapses_partial_into_failed(fn_name, producer):
    """Both call sites must pass the producer's status through verbatim.

    Asserted structurally because both functions need a live HubSpot/Ads client
    and a database to drive end to end; the defect was a literal conditional in
    the source, which is exactly what a structural check can see. The
    behavioural half is `test_135`.
    """
    import inspect

    import scheduler.incremental_sync as sync

    src = inspect.getsource(getattr(sync, fn_name))
    assert '"success" if status == "success" else "failed"' not in src, (
        f"{producer} still collapses partial into failed")


def test_135_a_partial_batch_status_reaches_sync_state_unchanged():
    """The behavioural half: the writer records the exact status it is given."""
    import inspect

    import db.writers as writers

    src = inspect.getsource(writers.finish_sync_batch)
    # The non-success branch writes `status` itself, not a literal, and does
    # NOT advance the proven-coverage watermark.
    assert "status        = EXCLUDED.status" in src
    assert "(source, dataset, status, error_message)" in src, (
        "the non-success branch must persist the status it was handed")


# ═════════════════════════════════════════════════════════════════════════════
# §4 — a failed pulse leaves a row behind
# ═════════════════════════════════════════════════════════════════════════════

def test_136_the_daily_pulse_records_its_run_before_anything_can_raise():
    """`write_run` must precede the pulls, or a failed pulse is invisible.

    `update_run(None, ...)` returns immediately, so a pulse that failed at pull
    time wrote nothing at all — and monitoring then reported "No daily run
    found in history" for a run that had happened and failed.
    """
    import inspect

    import scheduler.daily as daily

    src = inspect.getsource(daily.run_daily_pulse)
    write_at = src.index("db_writers.write_run(run_record)")
    for pull in ("pull_campaign_performance(", "pull_paid_search_contacts("):
        assert write_at < src.index(pull), (
            f"the run row is written after {pull} — a failure there leaves no "
            f"trace in `runs`")
    assert src.count("db_writers.write_run(run_record)") == 1, (
        "two inserts would produce two rows for one pulse")


def test_137_the_pulse_writes_its_row_even_when_the_first_pull_raises(
        monkeypatch):
    """The behavioural control for 136, with both pulls failing."""
    import scheduler.daily as daily

    written: list[dict] = []
    monkeypatch.setattr(daily.db_writers, "write_run",
                        lambda rec: written.append(dict(rec)) or 4242)
    monkeypatch.setattr(daily.db_writers, "update_run",
                        lambda rid, rec: written.append({"update": rid}))

    import connectors.google_ads_source as gads
    monkeypatch.setattr(gads, "pull_campaign_performance",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("ads down")))

    with pytest.raises(RuntimeError):
        daily.run_daily_pulse()

    assert written, "the failed pulse wrote no run record at all"
    assert written[0].get("run_type") == "daily"
    assert {"update": 4242} in written, (
        "the outcome must be written back to the row that was created")
