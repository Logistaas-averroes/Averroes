"""PR-ADS-165A — the weekly job and the Unit Economics page are retired.

Two owner decisions (2026-11-19):

* "remove the weekly job, this is killing render each week". The weekly data
  refresh (Mondays 04:00 UTC) was killed by the host at the 512 MB memory limit
  every Monday from 2026-10-19 at the latest; its last recorded success was
  2026-06-01.
* "unit economics is not needed". The Unit Economics page is deleted.

Part A is the half that can go wrong quietly. A job that is deleted but still
EXPECTED reads as "failed" or "never ran" for ever, and the `runs` table keeps
every row the job ever wrote — the newest of them `failed`. So this suite does
not stop at "the file is gone". It drives monitoring and the war room over the
history production actually holds (the three live run types, plus OLD `weekly`
rows ending in `failed`) and requires that the retired job is neither an alarm
nor a vote on another job's health.

What was decided, and is asserted here:

* **No dataset lost its only writer.** Every step of the weekly job also runs
  in the monthly job, the daily pulse or the daily incremental sync — except
  the search-term flag history, which moved to the monthly job. So nothing is
  added to ``RETIRED_DATASETS``. Four datasets are now written by the monthly
  job ALONE and carry the monthly staleness threshold instead of the weekly
  one, so they do not read "stale" for three weeks of every four.
* **Search Terms → Flagged stays.** The monthly job writes the same
  ``waste_terms`` annotations, so the tab is not one that can only ever be
  empty. It says how fresh a flag can be: up to a month.
* **Old ``weekly`` rows are history.** They stay in ``runs`` and are reported
  under ``retired_runs``; they have no tile, no severity and no warning.

Tests 01–14 are Part A, 15–22 Part B.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


JS = _read("static/app.js")
HTML = _read("static/index.html")
CSS = _read("static/styles.css")
SERVER = _read("api/server.py")

PACKAGES = ("analysis", "api", "connectors", "db", "scheduler", "services",
            "scripts")

#: The run types the three live run-writing jobs persist.
LIVE_RUN_TYPES = {"daily", "daily_incremental_sync", "monthly"}


def _production_files():
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _client(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    return TestClient(server.app), {"Authorization": "Bearer secret-token"}


# ═════════════════════════════════════════════════════════════════════════════
# A1 — the job is gone: not registered, not triggerable, not importable
# ═════════════════════════════════════════════════════════════════════════════
class _RecordingScheduler:
    """Stands in for APScheduler's BackgroundScheduler: records what
    ``start_scheduler`` registers and starts no thread."""

    instances: list = []

    def __init__(self, **kwargs):
        self.jobs: dict = {}
        self.running = False
        type(self).instances.append(self)

    def add_job(self, func, *, trigger=None, id=None, name=None, **kwargs):
        self.jobs[id] = {"func": func.__name__, "name": name,
                         "trigger": str(trigger)}

    def start(self):
        self.running = True

    def shutdown(self, wait=False):
        self.running = False

    def get_job(self, job_id):
        return None


def test_01_the_weekly_job_is_not_registered_with_the_scheduler(monkeypatch):
    """Behavioural: start the real ``start_scheduler`` against a recording
    scheduler and read what it registered."""
    import api.scheduler as scheduler
    _RecordingScheduler.instances = []
    monkeypatch.setattr(scheduler, "BackgroundScheduler", _RecordingScheduler)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    scheduler.start_scheduler()
    try:
        registered = _RecordingScheduler.instances[-1].jobs
    finally:
        scheduler.stop_scheduler()

    assert "weekly" not in registered
    assert set(registered) == set(scheduler.JOB_IDS)
    assert set(registered) == {"daily", "monthly", "daily_incremental_sync",
                               "platform_certification"}
    # No job left behind runs on a weekly trigger under another id.
    for job_id, job in registered.items():
        assert "day_of_week" not in job["trigger"], (job_id, job["trigger"])
        assert "weekly" not in job["func"].lower(), (job_id, job["func"])
        assert "weekly" not in (job["name"] or "").lower(), (job_id, job["name"])

    # Every surface keyed on the job ids agrees.
    assert "weekly" not in scheduler.JOB_IDS
    assert "weekly" not in scheduler._job_state                  # noqa: SLF001
    assert "weekly" not in scheduler._SCHEDULE_DESCRIPTIONS      # noqa: SLF001
    assert not hasattr(scheduler, "_run_weekly_scheduled")
    assert scheduler.get_registered_job_ids() == list(scheduler.JOB_IDS)
    status_jobs = [j["job"] for j in scheduler.get_scheduler_status()["jobs"]]
    assert "weekly" not in status_jobs
    # The retirement is stated, not merely an absence.
    assert set(scheduler.RETIRED_JOB_IDS) == {"weekly"}
    assert not set(scheduler.RETIRED_JOB_IDS) & set(scheduler.JOB_IDS)


def test_02_the_weekly_job_cannot_be_triggered(monkeypatch):
    import api.server as server
    paths = {r.path for r in server.app.routes}
    assert "/run/weekly" not in paths
    # The other manual triggers are untouched.
    assert {"/run/daily", "/run/monthly", "/run/incremental-sync"} <= paths

    client, auth = _client(monkeypatch)
    assert client.post("/run/weekly", headers=auth).status_code == 404
    assert client.get("/run/weekly", headers=auth).status_code == 404
    assert "/run/weekly" not in SERVER
    assert "run_weekly" not in SERVER

    # The Data Runs page builds one trigger button per job the status endpoint
    # lists, and posts to /run/<that id>: no listed id, no button.
    listed = [j["job"] for j in server.scheduler_status(
        user={"username": "t", "role": "admin"})["jobs"]]
    assert "weekly" not in listed
    assert set(listed) == {"daily", "monthly", "daily_incremental_sync",
                           "platform_certification"}
    loader = JS.split("async function loadScheduler")[1].split("\nasync function ")[0]
    assert 'fetchJSON("/scheduler/status")' in loader
    assert "triggerRun(job.job)" in loader
    trigger = JS.split("async function triggerRun")[1].split("\nfunction ")[0]
    assert "`/run/${encodeURIComponent(jobType)}`" in trigger
    assert "/run/weekly" not in JS and "/run/weekly" not in HTML

    # Nor from a shell: the module and its make target are gone.
    assert not (ROOT / "scheduler/weekly.py").exists()
    assert importlib.util.find_spec("scheduler.weekly") is None
    makefile = _read("Makefile")
    assert not re.search(r"^weekly:", makefile, re.M)
    assert "scheduler.weekly" not in makefile


def test_03_no_production_module_imports_or_names_the_deleted_module():
    """An import of a deleted module is a crash waiting for its code path, and
    two scripts and the readiness endpoint name modules as STRINGS (a list of
    modules to import, a list of files that must exist) — so string constants
    are read too, docstrings excepted."""
    offenders = []
    for path in _production_files():
        rel = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [(rel, a.name) for a in node.names
                              if a.name == "scheduler.weekly"]
            elif isinstance(node, ast.ImportFrom):
                if node.module == "scheduler.weekly":
                    offenders.append((rel, node.module))
                if node.module == "scheduler":
                    offenders += [(rel, "scheduler." + a.name)
                                  for a in node.names if a.name == "weekly"]
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef,
                                        ast.AsyncFunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr)
                      and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                for name in ("scheduler.weekly", "scheduler/weekly.py",
                             "run_weekly_report"):
                    if name in node.value:
                        offenders.append((rel, node.lineno, name))
    assert offenders == []

    # The lists that named it resolve to things that exist.
    import api.server as server
    for name in server._REQUIRED_MODULES:                        # noqa: SLF001
        importlib.import_module(name)
    import api.scheduler as scheduler
    import scripts.phase1_readiness as readiness
    assert readiness._REQUIRED_APSCHEDULER_JOBS <= set(scheduler.JOB_IDS)  # noqa: SLF001
    for rel in readiness._REQUIRED_SCHEDULER_FILES:              # noqa: SLF001
        assert (ROOT / rel).exists(), rel
    makefile = _read("Makefile")
    for target in readiness._REQUIRED_MAKEFILE_TARGETS:          # noqa: SLF001
        assert re.search(rf"^{re.escape(target)}:", makefile, re.M), target


# ═════════════════════════════════════════════════════════════════════════════
# A2 — monitoring over the history production holds
# ═════════════════════════════════════════════════════════════════════════════
NOW = datetime.now(timezone.utc)


def _run(run_type: str, status: str, days_ago: float, hour: int = 4) -> dict:
    ts = (NOW - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"run_type": run_type, "status": status, "started_at": stamp,
            # A run the host killed never wrote `finished_at`.
            "finished_at": stamp if status != "failed" else None}


def _newest_first(rows: list) -> list:
    return sorted(rows, key=lambda r: r["started_at"], reverse=True)


def _live_history(*, days: int = 30, monthly: str = "success") -> list:
    """The three live jobs, under the run types they persist — never relabelled:
    the 06:00 pulse (`daily`), the 09:00 incremental sync
    (`daily_incremental_sync`) and the monthly refresh on the 1st."""
    from scheduler.incremental_sync import RUN_TYPE
    assert RUN_TYPE == "daily_incremental_sync"
    rows = [_run("monthly", monthly, 18, 5)]
    for d in range(days):
        rows.append(_run("daily", "success", d + 0.5, 3))
        rows.append(_run(RUN_TYPE, "success", d + 0.4, 6))
    return rows


def _old_weekly_rows() -> list:
    """What the retired job left behind: killed every Monday (recorded
    `failed`), and one success far in the past."""
    return ([_run("weekly", "failed", d) for d in (3, 10, 17, 24, 31)]
            + [_run("weekly", "success", 80)])


def _production_history() -> list:
    return _newest_first(_live_history() + _old_weekly_rows())


def _thresholds() -> tuple[dict, int]:
    """The thresholds the endpoint itself loads from config/thresholds.yaml."""
    import api.server as server
    return server._load_monitoring_thresholds()                  # noqa: SLF001


def test_04_monitoring_reports_no_weekly_failure_over_production_history():
    import api.monitoring as mon
    stale_days, consec = _thresholds()
    verdict = mon.compute_monitoring_status(
        _production_history(), stale_days, consec)

    assert verdict["severity"] == "green", verdict["warnings"]
    assert verdict["warnings"] == []
    assert set(verdict["latest_runs"]) == LIVE_RUN_TYPES
    assert "weekly" not in verdict["latest_runs"]
    for cadence, state in verdict["latest_runs"].items():
        assert state["consecutive_failures"] == 0, cadence
        assert state["stale"] is False, cadence
        assert state["last_status"] == "success", cadence

    # The old rows are reported — as history, with no verdict attached.
    retired = verdict["retired_runs"]
    assert set(retired) == {"weekly"}
    assert retired["weekly"]["retired"] is True
    assert retired["weekly"]["last_status"] == "failed"
    assert retired["weekly"]["runs_in_history"] == 6
    assert retired["weekly"]["last_run_at"] == _run("weekly", "failed", 3)["started_at"]
    assert "retired" in retired["weekly"]["reason"].lower()
    assert not {"stale", "consecutive_failures", "severity",
                "last_success_at"} & set(retired["weekly"])

    # The UI paints the banner only for a non-green severity with warnings.
    banner = JS.split('fetchJSON("/api/monitoring/status")')[1][:900]
    assert 'data.severity === "green"' in banner
    assert "retired_runs" not in JS, "retired history must not feed the banner"


def test_05_a_history_with_no_weekly_row_raises_no_missing_weekly_warning():
    """A fresh database, or one whose weekly rows have aged out of the 90-day
    lookback: the job is not expected, so its absence is not a finding."""
    import api.monitoring as mon
    stale_days, consec = _thresholds()
    verdict = mon.compute_monitoring_status(
        _newest_first(_live_history()), stale_days, consec)
    assert verdict["severity"] == "green"
    assert verdict["warnings"] == []
    assert verdict["retired_runs"] == {}
    assert not [w for w in verdict["warnings"] if "weekly" in w.lower()]

    # Control: a LIVE cadence that is missing still warns — the guard was not
    # switched off, only the retired job was taken off the board.
    for missing in sorted(LIVE_RUN_TYPES):
        rows = [r for r in _production_history() if r["run_type"] != missing]
        partial_board = mon.compute_monitoring_status(rows, stale_days, consec)
        assert partial_board["severity"] != "green", missing
        assert any("run found in history" in w
                   for w in partial_board["warnings"]), missing
        assert not [w for w in partial_board["warnings"]
                    if "weekly" in w.lower()], missing


def test_06_the_retired_job_is_not_a_cadence_and_has_no_threshold():
    import api.monitoring as mon
    assert set(mon.MONITORING_CADENCES) == LIVE_RUN_TYPES
    assert set(mon.RUN_TYPE_CADENCE) == LIVE_RUN_TYPES
    assert set(mon.RUN_TYPE_CADENCE.values()) == set(mon.MONITORING_CADENCES)
    assert set(mon.STALE_DAYS_DEFAULT) == LIVE_RUN_TYPES
    assert "weekly" not in mon.CADENCE_LABELS
    assert mon.monitoring_cadence("weekly") is None
    assert mon.monitoring_cadence(" Weekly ") is None
    assert mon.is_retired_run_type("weekly") is True
    assert mon.is_retired_run_type("monthly") is False
    # Retired is a decision; unknown is a scheduler nobody registered. They
    # are different answers and neither is a cadence.
    assert mon.is_retired_run_type("backfill") is False
    assert mon.monitoring_cadence("backfill") is None
    assert not set(mon.RETIRED_RUN_TYPES) & set(mon.RUN_TYPE_CADENCE)

    stale_days, _ = _thresholds()
    assert set(stale_days) == LIVE_RUN_TYPES
    import yaml
    config = yaml.safe_load(_read("config/thresholds.yaml"))
    assert set(config["ui"]["monitoring"]["stale_after_days"]) == LIVE_RUN_TYPES

    # Every run type a scheduler persists is either monitored or retired.
    import api.scheduler as scheduler
    from scheduler.incremental_sync import RUN_TYPE
    persisted = set()
    for rel in ("scheduler/daily.py", "scheduler/monthly.py"):
        tree = ast.parse(_read(rel))
        persisted |= {n.args[0].value for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and getattr(n.func, "id", None) == "start_run"}
    persisted.add(RUN_TYPE)
    assert persisted == LIVE_RUN_TYPES
    assert persisted <= set(mon.RUN_TYPE_CADENCE)
    assert persisted <= set(scheduler.JOB_IDS)


def test_07_no_other_cadence_absorbs_the_retired_jobs_rows(monkeypatch):
    """A cadence is ONE pipeline's verdict. Folded into `monthly`, the weekly
    job's old rows would vote on the monthly job's health in both directions:
    its failures would redden a healthy job, and its successes would hide a
    broken one."""
    import api.monitoring as mon
    stale_days, consec = _thresholds()

    # (a) Old weekly FAILURES, newer than the monthly success, do not touch it.
    healthy = mon.compute_monitoring_status(
        _production_history(), stale_days, consec)
    monthly = healthy["latest_runs"]["monthly"]
    assert monthly["consecutive_failures"] == 0
    assert monthly["last_status"] == "success"
    assert monthly["last_success_at"] == _run("monthly", "success", 18, 5)["finished_at"]

    # (b) A weekly SUCCESS newer than two monthly failures does not hide them.
    broken = _newest_first(
        [r for r in _live_history() if r["run_type"] != "monthly"]
        + [_run("monthly", "failed", 18, 5), _run("monthly", "failed", 48, 5),
           _run("weekly", "success", 2)])
    verdict = mon.compute_monitoring_status(broken, stale_days, consec)
    assert verdict["severity"] == "red"
    assert verdict["latest_runs"]["monthly"]["consecutive_failures"] == 2
    assert verdict["latest_runs"]["monthly"]["last_success_at"] is None
    assert verdict["latest_runs"]["monthly"]["stale"] is True
    assert any("Monthly run has failed 2 times in a row" in w
               for w in verdict["warnings"])
    # Nor does it reach either daily cadence.
    for cadence in ("daily", "daily_incremental_sync"):
        assert verdict["latest_runs"][cadence]["last_success_at"] != \
            _run("weekly", "success", 2)["finished_at"], cadence

    # Counterfactual. Fold weekly into monthly and BOTH verdicts go wrong over
    # the identical rows — so the two results above are the mapping's doing.
    monkeypatch.setattr(mon, "MONITORING_CADENCES", tuple(sorted(LIVE_RUN_TYPES)))
    monkeypatch.setattr(mon, "RUN_TYPE_CADENCE",
                        {**{c: c for c in LIVE_RUN_TYPES}, "weekly": "monthly"})
    monkeypatch.setattr(mon, "RETIRED_RUN_TYPES", {}, raising=False)
    reddened = mon.compute_monitoring_status(
        _production_history(), stale_days, consec)
    assert reddened["severity"] == "red", "old weekly failures reddened monthly"
    assert reddened["latest_runs"]["monthly"]["consecutive_failures"] == 3
    masked = mon.compute_monitoring_status(broken, stale_days, consec)
    assert masked["latest_runs"]["monthly"]["consecutive_failures"] == 0
    assert masked["latest_runs"]["monthly"]["last_success_at"] is not None, (
        "a weekly success advanced the monthly job's freshness clock")
    assert masked["severity"] == "green", "a weekly success hid a broken monthly job"


def test_08_the_guard_goes_red_when_weekly_is_a_live_cadence_again(monkeypatch):
    """Counterfactual for test_04: restore the pre-retirement registration over
    the same history and the permanent alarm this slice removes comes back."""
    import api.monitoring as mon
    monkeypatch.setattr(mon, "MONITORING_CADENCES",
                        (*sorted(LIVE_RUN_TYPES), "weekly"))
    monkeypatch.setattr(mon, "RUN_TYPE_CADENCE",
                        {**{c: c for c in LIVE_RUN_TYPES}, "weekly": "weekly"})
    monkeypatch.setattr(mon, "RETIRED_RUN_TYPES", {}, raising=False)
    before = mon.compute_monitoring_status(
        _production_history(),
        {"daily": 2, "daily_incremental_sync": 2, "weekly": 8, "monthly": 35}, 2)
    assert before["severity"] == "red"
    assert before["latest_runs"]["weekly"]["consecutive_failures"] == 5
    assert before["latest_runs"]["weekly"]["stale"] is True
    assert any("Weekly run has failed 5 times in a row" in w
               for w in before["warnings"]), before["warnings"]


def test_08b_a_failed_monthly_run_is_never_green(monkeypatch):
    """The monthly job is now the SOLE scheduled writer of four datasets, and
    it runs once a month. A run that failed yesterday, after a success 31 days
    ago, has a failure streak of 1 (under the red threshold of 2) and a last
    success inside the 35-day staleness threshold — so it read `green`, no
    warnings, the day every dataset it alone writes missed its only refresh
    of the month."""
    import api.monitoring as mon
    stale_days, consec = _thresholds()
    assert (consec, stale_days["monthly"]) == (2, 35)

    def history(*monthly_rows):
        live = [r for r in _live_history() if r["run_type"] != "monthly"]
        return _newest_first(live + list(monthly_rows) + _old_weekly_rows())

    failed = mon.compute_monitoring_status(
        history(_run("monthly", "failed", 1, 5), _run("monthly", "success", 31, 5)),
        stale_days, consec)
    monthly = failed["latest_runs"]["monthly"]
    # The two existing rules both stay silent — that is the gap…
    assert monthly["consecutive_failures"] == 1 < consec
    assert monthly["stale"] is False
    assert monthly["last_status"] == "failed"
    # …so the latest outcome is the verdict.
    assert failed["severity"] == "yellow"
    assert failed["warnings"] == [
        "Monthly run failed on its latest attempt — the datasets only this job "
        "writes were not refreshed, and it does not run again until its next "
        "scheduled date."]
    assert monthly["latest_failed_unresolved"] is True

    # An unrecognised latest status is not evidence of success either.
    odd = mon.compute_monitoring_status(
        history(_run("monthly", "killed", 1, 5), _run("monthly", "success", 31, 5)),
        stale_days, consec)
    assert odd["severity"] == "yellow"
    assert odd["latest_runs"]["monthly"]["latest_failed_unresolved"] is True

    # Partial keeps its own (existing) warning, and is yellow as before.
    partial = mon.compute_monitoring_status(
        history(_run("monthly", "partial", 1, 5), _run("monthly", "success", 31, 5)),
        stale_days, consec)
    assert partial["severity"] == "yellow"
    assert partial["latest_runs"]["monthly"]["latest_failed_unresolved"] is False
    assert partial["warnings"] == [
        "Monthly run completed partially — some datasets were incomplete."]

    # Two in a row is still red, with the streak warning and not this one.
    twice = mon.compute_monitoring_status(
        history(_run("monthly", "failed", 1, 5), _run("monthly", "failed", 31, 5)),
        stale_days, consec)
    assert twice["severity"] == "red"
    assert twice["warnings"] == ["Monthly run has failed 2 times in a row."]

    # A manual re-run that succeeds clears it.
    recovered = mon.compute_monitoring_status(
        history(_run("monthly", "success", 0.6, 5), _run("monthly", "failed", 1, 5),
                _run("monthly", "success", 31, 5)), stale_days, consec)
    assert recovered["severity"] == "green" and recovered["warnings"] == []

    # Control — the daily cadences are NOT changed: one failed morning, retried
    # tomorrow, is still under the streak and still green.
    assert mon.LATEST_RUN_MUST_SUCCEED == frozenset({"monthly"})
    for cadence in ("daily", "daily_incremental_sync"):
        rows = _newest_first(_live_history() + [_run(cadence, "failed", 0.05, 8)])
        assert rows[0]["run_type"] == cadence and rows[0]["status"] == "failed"
        verdict = mon.compute_monitoring_status(rows, stale_days, consec)
        assert verdict["latest_runs"][cadence]["consecutive_failures"] == 1
        assert verdict["latest_runs"][cadence]["latest_failed_unresolved"] is False
        assert verdict["severity"] == "green", (cadence, verdict["warnings"])
        assert verdict["warnings"] == []


def test_09_the_monitoring_endpoint_serves_the_same_verdict(monkeypatch):
    """Through the route handler, with the database offline so the endpoint
    reads the JSONL run history — the same rows, the thresholds it loads
    itself from config/thresholds.yaml."""
    import api.server as server
    monkeypatch.setattr("db.connection.get_conn", lambda: _NoDb())
    monkeypatch.setattr(server, "_read_jsonl_runs", _production_history)
    body = server.api_monitoring_status(user={"username": "t", "role": "admin"})
    assert body["db_unavailable"] is True
    assert body["severity"] == "green", body["warnings"]
    assert body["warnings"] == []
    assert set(body["latest_runs"]) == LIVE_RUN_TYPES
    assert body["retired_runs"]["weekly"]["last_status"] == "failed"
    assert body["retired_runs"]["weekly"]["retired"] is True


class _NoDb:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ═════════════════════════════════════════════════════════════════════════════
# A3 — the war room, over a production-shaped database
# ═════════════════════════════════════════════════════════════════════════════
class _Cursor:
    """One fixed database state: every registered dataset has rows and a sync
    row of the age the fixture gives it, and `runs` holds the three live run
    types plus the retired job's old rows (newest `failed`)."""

    def __init__(self, state):
        self.state = state
        self.description = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        lower = text.lower()
        if "from sync_state" in lower:
            self.description = [(c,) for c in (
                "source", "dataset", "status", "last_successful_sync_at",
                "last_source_date", "last_batch_id", "error_message",
                "updated_at")]
            self._rows = list(self.state["sync_rows"])
        elif "from sync_batches" in lower:
            self.description = [(c,) for c in ("source", "dataset", "status",
                                               "row_count")]
            self._rows = []
        elif "count(*)" in lower:
            self.description = [("count",)]
            self._rows = [(5,)]
        elif "from runs" in lower:
            run_type = (params or [None])[0]
            self.state["run_types_read"].append(run_type)
            self.description = [("status",), ("started_at",), ("finished_at",)]
            row = self.state["runs"].get(run_type)
            self._rows = [row] if row else []
        else:
            self.description = []
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor(self.state)


#: The freshness-config keys of the datasets the monthly job alone writes.
#: Stated here, not read from the service, so the fixture describes the
#: database and not the code under test; test_12 proves the service's own
#: list equals it.
#: PR-ADS-177C: "keywords" (the legacy keyword snapshot) is retired.
MONTHLY_ONLY_KEYS = ("waste_terms", "geo", "gclid_coverage_snapshots")
#: PR-ADS-177C: the weekly job's one step whose dataset is now retired — its
#: last reader went in PR-ADS-177B, so the monthly job no longer runs it.
RETIRED_177C_CALLS = {"write_keywords"}
RETIRED_177C_BATCHES = {("google_ads_api", "keywords")}


def _db_state(monkeypatch, *, monthly_age_days: float = 18.0) -> dict:
    """`monthly_age_days` is the age of the last monthly run and of every
    dataset only that run writes; everything else synced an hour ago."""
    from services import freshness_service as fresh
    hour_ago = NOW - timedelta(hours=1)
    monthly_at = NOW - timedelta(days=monthly_age_days)
    rows = []
    for key, cfg in fresh.DATASET_FRESHNESS_CONFIG.items():
        at = monthly_at if key in MONTHLY_ONLY_KEYS else hour_ago
        rows.append((cfg["source"], cfg["dataset"], "success", at, at.date(),
                     1, None, at))
    state = {
        "sync_rows": rows,
        "run_types_read": [],
        "runs": {
            "daily": ("success", NOW - timedelta(hours=7), NOW - timedelta(hours=7)),
            "daily_incremental_sync": ("success", NOW - timedelta(hours=4),
                                       NOW - timedelta(hours=4)),
            "monthly": ("success", monthly_at, monthly_at),
            # The retired job's newest row: killed by the host three days ago.
            "weekly": ("failed", NOW - timedelta(days=3), None),
        },
    }
    monkeypatch.setattr("db.connection.get_conn", lambda: _Conn(state))
    import api.server as server
    monkeypatch.setattr(server, "_war_room_cache", {})
    return state


def _war_room(monkeypatch) -> dict:
    client, auth = _client(monkeypatch)
    response = client.get("/api/system/status-war-room?days=60", headers=auth)
    assert response.status_code == 200
    return response.json()


def test_10_the_war_room_shows_no_weekly_tile_and_no_weekly_blocker(monkeypatch):
    state = _db_state(monkeypatch)
    body = _war_room(monkeypatch)

    scheduler = body["scheduler"]
    assert "latest_weekly" not in scheduler
    assert {k for k in scheduler if k.startswith("latest_")} == {
        "latest_daily", "latest_monthly", "latest_incremental"}
    assert scheduler["latest_daily"]["status"] == "success"
    assert scheduler["latest_monthly"]["status"] == "success"
    assert scheduler["latest_incremental"]["status"] == "success"
    # The retired job's row was never even asked for.
    assert "weekly" not in state["run_types_read"]
    assert set(state["run_types_read"]) == LIVE_RUN_TYPES

    # Nothing anywhere in the response is about the weekly job…
    assert "weekly" not in json.dumps(body).lower()
    # …and, this being a healthy system, nothing is wrong at all.
    assert body["critical_blockers"] == []
    assert body["overall_status"] == "ok", body["overall_label"]
    assert not [i for i in body["page_impact"]
                if i.get("status") in ("blocked", "degraded")], body["page_impact"]

    # The page renders the tiles it is given, from a fixed list of keys.
    tiles = re.search(r"for \(const \[key, label\] of (\[\[.*?\]\])\) \{",
                      JS.split("Scheduler Latest Runs")[1]).group(1)
    assert json.loads(tiles) == [["latest_daily", "Daily"],
                                 ["latest_monthly", "Monthly"],
                                 ["latest_incremental", "Incremental"]]
    assert "latest_weekly" not in JS


def test_11_the_scheduler_summary_ignores_a_weekly_row_it_is_handed():
    """The unit under the route: even handed the retired job's row, the summary
    does not republish it, and it does not count as "a run was recorded"."""
    from services.system_status_service import compute_scheduler_summary
    failed = {"status": "failed", "started_at": "2026-11-16T04:00:00+00:00",
              "finished_at": None}
    summary = compute_scheduler_summary({"weekly": failed})
    assert "latest_weekly" not in summary
    assert failed not in summary.values()
    assert summary["diagnostic_status"] == "no_scheduler_run_recorded"
    assert "weekly" not in summary["next_action"].lower()


# ═════════════════════════════════════════════════════════════════════════════
# A4 — datasets: nothing lost its only writer; four are monthly now
# ═════════════════════════════════════════════════════════════════════════════
#: Every (source, dataset) batch the weekly job stamped (scheduler/weekly.py at
#: 5574170), and the service-owned syncs it called.
#: PR-ADS-177C: ("google_ads_api", "keywords") — the legacy keyword snapshot —
#: is retired (no reader, no writer); RETIRED_177C below.
WEEKLY_JOB_BATCHES = {("hubspot", "contacts"), ("hubspot", "deals"),
                      ("google_ads_api", "geo"),
                      ("analysis", "waste_terms"), ("gclid", "matches"),
                      ("gclid", "coverage_snapshots")}
WEEKLY_JOB_CALLS = {
    "pull_campaign_performance", "pull_keyword_performance",
    "pull_geo_performance", "pull_paid_search_contacts", "pull_deals_with_gclid",
    "write_leads", "write_deals", "write_geo",
    "sync_keyword_daily_facts", "sync_recent_search_terms",
    "run_waste_detection", "write_waste_terms", "record_flag_history",
    "run_gclid_match", "write_gclid_attribution",
    "write_gclid_coverage_snapshot", "google_ads_save", "hubspot_save",
    "gclid_save",
}


def _job_function(rel: str, name: str) -> ast.FunctionDef:
    return next(n for n in ast.parse(_read(rel)).body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _called(node: ast.AST) -> set:
    out = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name:
                out.add(name)
    return out


def _batches(node: ast.AST) -> set:
    found = set()
    for inner in ast.walk(node):
        if (isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "start_sync_batch"):
            kw = {k.arg: k.value for k in inner.keywords}
            source, dataset = kw.get("source"), kw.get("dataset")
            if isinstance(source, ast.Constant) and isinstance(dataset, ast.Constant):
                found.add((source.value, dataset.value))
    return found


def test_12_every_step_of_the_weekly_job_still_has_a_scheduled_writer():
    """The audit behind the deletion, as a test: read from the AST of the
    monthly job FUNCTION (a step named only in a comment is not a step)."""
    monthly = _job_function("scheduler/monthly.py", "run_monthly_report")
    assert WEEKLY_JOB_CALLS - _called(monthly) == set()
    assert WEEKLY_JOB_BATCHES - _batches(monthly) == set()
    # PR-ADS-177C: the one exception, retired on purpose (and registered so).
    assert not RETIRED_177C_CALLS & _called(monthly)
    assert not RETIRED_177C_BATCHES & _batches(monthly)

    # So no dataset is retired by this slice: the registry of retired datasets
    # is exactly what it was, and every dataset the weekly job fed is still a
    # live, registered one.
    from services import freshness_service as fresh
    assert set(fresh.RETIRED_DATASETS) == ({("google_ads_api", "campaigns")}
                                           | RETIRED_177C_BATCHES)
    registered = {(c["source"], c["dataset"])
                  for c in fresh.DATASET_FRESHNESS_CONFIG.values()}
    assert WEEKLY_JOB_BATCHES <= registered
    assert {("google_ads_api", "keyword_facts"),
            ("google_ads_api", "search_terms")} <= registered
    for pair in WEEKLY_JOB_BATCHES:
        assert fresh.is_retired_dataset(*pair) is False, pair

    # The datasets written by the monthly job ALONE are derived, not listed by
    # hand: what monthly stamps, minus what a daily job stamps.
    from scheduler import incremental_sync as inc
    daily_pulse = _batches(ast.parse(_read("scheduler/daily.py")))
    stamped_daily = (daily_pulse | set(inc.ACTIVE_SYNC_PAIRS)
                     | set(inc.SERVICE_OWNED_SYNC_PAIRS))
    monthly_only = _batches(monthly) - stamped_daily
    declared = {(fresh.DATASET_FRESHNESS_CONFIG[k]["source"],
                 fresh.DATASET_FRESHNESS_CONFIG[k]["dataset"])
                for k in fresh.MONTHLY_REFRESH_DATASETS}
    assert set(fresh.MONTHLY_REFRESH_DATASETS) == set(MONTHLY_ONLY_KEYS)
    assert monthly_only == declared == {
        ("google_ads_api", "geo"),
        ("analysis", "waste_terms"), ("gclid", "coverage_snapshots")}
    assert fresh.MONTHLY_REFRESH_STALE_DAYS == 35
    import api.monitoring as mon
    assert fresh.MONTHLY_REFRESH_STALE_DAYS == mon.STALE_DAYS_DEFAULT["monthly"]
    for key, cfg in fresh.DATASET_FRESHNESS_CONFIG.items():
        monthly_cfg = key in fresh.MONTHLY_REFRESH_DATASETS
        assert (cfg.get("refresh_cadence") == "monthly") is monthly_cfg, key
        assert (cfg["stale_threshold_days"] == 35) is monthly_cfg, key


def test_13_monthly_datasets_are_marked_monthly_and_none_of_them_is_core(
        monkeypatch):
    """Eighteen days after the 1st — today, in production.

    Against the weekly threshold of 8 days these four datasets read
    `stale_with_data` from the 9th of every month to the end of it. They take
    the monthly threshold instead — and that is only honest under two
    conditions this case holds, because "fresh" for them means "refreshed on
    schedule", not "recent":

    * none of them is a CORE dataset or the dataset of a page's primary
      figure. (The first version of this slice left the legacy `geo` and
      `keywords` snapshots in CORE_DATASETS and `geo` as the Countries page's
      dataset, and asserted `overall_status == "ok"` over them 18 days old —
      a healthy verdict resting on a wide threshold. The Countries page now
      reads the daily `canonical_geo`; see
      tests/test_pr_ads_165a_countries_pg_integration.py.)
    * every surface that prints their status says they are monthly."""
    from services import freshness_service as fresh
    from services import system_status_service as status
    assert not set(status.CORE_DATASETS) & set(MONTHLY_ONLY_KEYS)
    assert status.CORE_DATASETS == frozenset({
        "search_terms", "leads", "keyword_facts", "canonical_spend",
        "canonical_geo"})
    # No core dataset has a threshold wider than the weekly one.
    for key in status.CORE_DATASETS:
        assert fresh.DATASET_FRESHNESS_CONFIG[key]["stale_threshold_days"] <= 8, key
        assert "refresh_cadence" not in fresh.DATASET_FRESHNESS_CONFIG[key], key
    # The pages that DO name a monthly dataset, stated: the dashboard's and the
    # retired waste route's flag count, and the GCLID coverage panel.
    monthly_pages = {page: sorted(set(deps) & set(MONTHLY_ONLY_KEYS))
                     for page, deps in status.PAGE_PIPELINE_IMPACT.items()
                     if set(deps) & set(MONTHLY_ONLY_KEYS)}
    assert monthly_pages == {
        "dashboard": ["waste_terms"], "waste": ["waste_terms"],
        "gclid-attribution": ["gclid_coverage_snapshots"]}
    # PR-ADS-176C: the retired #/geo page's key is removed; the one Countries
    # page (`countries`) keys on canonical geo and canonical spend.
    assert "geo" not in status.PAGE_PIPELINE_IMPACT
    assert status.PAGE_PIPELINE_IMPACT["countries"] == ["canonical_geo", "canonical_spend"]
    # PR-ADS-177C: Keyword Evidence is the Keywords tab of the one Keywords &
    # Search Terms page; its key is retired and the page keys on both.
    assert "keywords" not in status.PAGE_PIPELINE_IMPACT
    assert status.PAGE_PIPELINE_IMPACT["search-terms"] == ["search_terms", "keyword_facts"]

    _db_state(monkeypatch, monthly_age_days=18.0)
    body = _war_room(monkeypatch)
    by_key = {p["key"]: p for p in body["pipelines"]}
    for key in MONTHLY_ONLY_KEYS:
        assert by_key[key]["canonical_status"] == "fresh_with_data", key
        # The war room says so beside the status…
        assert by_key[key]["refresh_cadence"] == "monthly", key
    for key in status.CORE_DATASETS:
        assert by_key[key]["refresh_cadence"] is None, key
        assert by_key[key]["canonical_status"] == "fresh_with_data", key
    assert body["critical_blockers"] == []
    # …and the overall verdict is "ok" on the strength of the CORE datasets,
    # every one of which synced within the hour — not of the monthly ones.
    assert body["overall_status"] == "ok", body["overall_label"]

    import api.server as server
    freshness = server.api_datasets_freshness(user={"username": "t"}, days=60)
    rows = {d["dataset_key"]: d for d in freshness["datasets"]}
    for key in MONTHLY_ONLY_KEYS:
        cfg = fresh.DATASET_FRESHNESS_CONFIG[key]
        row = rows[f"{cfg['source']}/{cfg['dataset']}"]
        assert row["canonical_status"] == "fresh_with_data", key
        assert row["stale_threshold_days"] == 35, key
        assert row["refresh_cadence"] == "monthly", key
    assert rows["google_ads_api/canonical_spend"]["refresh_cadence"] is None
    assert rows["google_ads_api/canonical_geo"]["refresh_cadence"] is None
    assert freshness["retired_datasets"] == []
    # The freshness table prints the mark wherever the endpoint sends it.
    table = JS.split("function renderDatasetFreshness(data)")[1].split(
        "\nfunction ")[0]
    assert 'row.refresh_cadence === "monthly"' in table
    assert "refreshed monthly" in table

    # Control 1: a monthly run that was MISSED is still caught. 36 days is past
    # the threshold: the four go stale, and the system is no longer "ok".
    _db_state(monkeypatch, monthly_age_days=36.0)
    late_body = _war_room(monkeypatch)
    late = {p["key"]: p for p in late_body["pipelines"]}
    for key in MONTHLY_ONLY_KEYS:
        assert late[key]["canonical_status"] == "stale_with_data", key
    assert late["canonical_spend"]["canonical_status"] == "fresh_with_data"
    assert late_body["overall_status"] == "warning", late_body["overall_label"]

    # Control 2: a CORE dataset three days behind is NOT covered by anybody's
    # monthly threshold — canonical country spend goes stale at 3 days.
    state = _db_state(monkeypatch, monthly_age_days=18.0)
    behind = NOW - timedelta(days=4)
    state["sync_rows"] = [
        (src, ds, st, behind, behind.date(), bid, err, behind)
        if (src, ds) == ("google_ads_api", "canonical_geo")
        else (src, ds, st, at, day, bid, err, upd)
        for (src, ds, st, at, day, bid, err, upd) in state["sync_rows"]]
    stale_core = _war_room(monkeypatch)
    geo_pipe = {p["key"]: p for p in stale_core["pipelines"]}["canonical_geo"]
    assert geo_pipe["canonical_status"] == "stale_with_data"
    assert stale_core["overall_status"] != "ok"
    assert [b["id"] for b in stale_core["critical_blockers"]] == [
        "canonical_geo_stale"]
    # PR-ADS-176C: the retired #/geo page ("Geo") is no longer a page; the one
    # Countries page is the page a stale canonical_geo degrades.
    assert not [i for i in stale_core["page_impact"] if i.get("page") == "Geo"]
    geo_impact = [i for i in stale_core["page_impact"] if i.get("page") == "Countries"]
    assert [(i["status"], i["blocked_by"]) for i in geo_impact] == [
        ("degraded", "canonical_geo")], stale_core["page_impact"]


def test_13b_a_retired_datasets_old_sync_row_is_reported_retired_not_stale(
        monkeypatch):
    """The existing retirement mechanism, over the same database plus the row
    production still holds for the one retired dataset."""
    state = _db_state(monkeypatch)
    old = NOW - timedelta(days=40)
    state["sync_rows"].append(("google_ads_api", "campaigns", "success", old,
                               old.date(), 2, None, old))
    import api.server as server
    freshness = server.api_datasets_freshness(user={"username": "t"}, days=60)
    assert [r["dataset_key"] for r in freshness["retired_datasets"]] == [
        "google_ads_api/campaigns"]
    assert "google_ads_api/campaigns" not in {
        d["dataset_key"] for d in freshness["datasets"]}
    assert not [d for d in freshness["datasets"]
                if d["canonical_status"].startswith("stale")]
    body = _war_room(monkeypatch)
    assert body["overall_status"] == "ok", body["overall_label"]
    assert "campaigns" not in {p["key"] for p in body["pipelines"]}


def test_13c_the_countries_page_is_not_fed_by_a_monthly_table():
    """The first version of this case asserted that the Countries page SAID it
    was monthly — help strings beside a page that printed `$0` for a window
    with no rows. A disclosure is not a fix. The page now reads the daily
    canonical country spend behind a coverage verdict; the behaviour is proved
    on PostgreSQL and in node in
    tests/test_pr_ads_165a_countries_pg_integration.py, and this case holds
    the registry half: nothing about the page points at the monthly snapshot."""
    from services import freshness_service as fresh
    assert fresh.DATASET_FRESHNESS_CONFIG["geo"]["page"] is None
    # PR-ADS-176C: the /api/geo handler this case read (it built from the
    # canonical service and named no legacy table) is retired with its feed;
    # the route is asserted gone in tests/test_pr_ads_176c_country_feed_retired.
    assert '@app.get("/api/geo")' not in SERVER
    # PR-ADS-176B: the Countries page is `roas-countries` (the #/geo page is
    # deleted and redirects there). The same property, on the page that is
    # left: its copy does not point at the monthly snapshot, and its freshness
    # keys are the daily canonical country spend, never the legacy `geo`.
    for text in (JS.split("const PAGE_EXPLANATIONS = {")[1].split("\n};")[0]
                 .split('\n  "roas-countries": {')[1].split("\n  }")[0],
                 JS.split("const PAGE_HELP_CONTENT = {")[1].split("\n};")[0]
                 .split('\n  "roas-countries": {')[1].split("\n  },")[0],
                 HTML.split('id="page-roas-countries"')[1].split("</section>")[0]):
        assert "once a month" not in text and "monthly" not in text.lower()
        assert "weekly" not in text.lower()
    deps = JS.split("const PAGE_DEPENDENCIES = {")[1].split("\n};")[0]
    line = next(l for l in deps.splitlines() if '"roas-countries":' in l)
    assert '"google_ads_api/canonical_geo"' in line and '"google_ads_api/geo"' not in line
    assert "\n  geo:" not in deps
    # The legacy snapshot stays registered (three non-page readers remain) and
    # is named for what it is.
    from services import system_status_service as status
    assert status.PIPELINE_DEPENDENCIES["geo"]["label"] == \
        "Geo (legacy monthly snapshot)"
    assert not fresh.is_retired_dataset("google_ads_api", "geo")
    assert "write_geo(" in _read("scheduler/monthly.py")


# ═════════════════════════════════════════════════════════════════════════════
# A5 — Search Terms → Flagged: kept, fed monthly, and says so
# ═════════════════════════════════════════════════════════════════════════════
def test_14_the_flagged_tab_is_still_fed_and_states_how_fresh_it_can_be(
        monkeypatch, tmp_path):
    # Fed: the monthly job, run with every external call stubbed, writes the
    # waste annotations and THEN records flag history from them.
    import test_pr_ads_162h_report_generation_retired as t162h
    h = t162h._Harness(monkeypatch, tmp_path, "monthly")        # noqa: SLF001
    outcome = h.run()
    assert outcome == {"run_type": "monthly", "status": "success",
                       "failed_steps": []}
    feed = [c for c in h.calls
            if c in ("sync_recent_search_terms", "run_waste_detection",
                     "write_waste_terms", "record_flag_history")]
    assert feed == ["sync_recent_search_terms", "run_waste_detection",
                    "write_waste_terms", "record_flag_history"]
    assert h.batch_status(("analysis", "waste_terms")) == ["success"]
    assert h.waste_args == (t162h._Harness.TERMS, True)         # noqa: SLF001

    # Read: the tab and its endpoint are still there, and the retired #/waste
    # route still lands on it.
    import api.server as server
    assert "/api/search-term-evidence/flagged" in {
        r.path for r in server.app.routes}
    tabs = re.search(r"const ST_TABS = (\[.*?\]);", JS).group(1)
    # PR-ADS-177C: one Keywords & Search Terms page — the Keywords tab joined.
    assert json.loads(tabs) == ["terms", "flagged", "patterns", "keywords"]
    assert 'waste: { page: "search-terms", tab: "flagged" }' in JS
    assert "fetchJSON(`/api/search-term-evidence/flagged?" in JS

    # Stated: evaluate the real literal with node and read what the tab prints.
    literal = re.search(r"const FLAG_DETECTION_CADENCE_NOTE =\s*([\s\S]*?);\n",
                        JS).group(1)
    out = subprocess.run(
        ["node", "-e", f"process.stdout.write({literal});"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    note = out.stdout
    assert "once a month" in note and "on the 1st" in note
    assert "previous 30 days" in note
    assert "up to a month old" in note
    assert "not a clean result" in note
    assert "weekly" not in note.lower()
    render = JS.split("function renderFlaggedTab()")[1].split("\nfunction ")[0]
    assert "${escapeHtml(FLAG_DETECTION_CADENCE_NOTE)}" in render
    # The 30 days the note states is the window the monthly job analyses.
    assert 'sync_recent_search_terms("monthly", days=30' in _read(
        "scheduler/monthly.py")
    from services import search_term_flag_history_service as history
    assert history.SCHEDULED_HISTORY_WINDOW == "30d"
    # No surface still credits a weekly detection.
    assert "weekly detection" not in JS
    assert "weekly waste detection" not in _read(
        "services/search_term_evidence_service.py")


def test_14b_the_search_terms_verdict_no_longer_asks_for_a_weekly_run():
    # Neither reader selects the retired run type any more: an old `weekly`
    # row is not evidence that the search-term pipeline has run.
    for rel in ("api/server.py", "scripts/verify_search_terms_pipeline.py"):
        src = _read(rel)
        assert "run_type = 'weekly'" not in src, rel
        assert "latest_weekly_run" not in src, rel
        assert "SEARCH_TERM_RUN_TYPES" in src, rel
        assert "run_type = ANY(%s)" in src, rel
    from scripts import verify_search_terms_pipeline as verifier
    assert set(verifier.SEARCH_TERM_RUN_TYPES) == LIVE_RUN_TYPES
    # No run of a job that syncs search terms, no rows, no sync state: the
    # pipeline has not run.
    verdict, reason = verifier.compute_search_terms_verdict(
        db_available=True, db_rows_window=0, sync_status=None,
        latest_search_term_run=None)
    assert verdict == verifier.Verdict.NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT
    assert "weekly" not in reason.lower()
    # Control: with such a run on record the verdict moves on.
    other, _ = verifier.compute_search_terms_verdict(
        db_available=True, db_rows_window=0, sync_status=None,
        latest_search_term_run="2026-11-19 06:00:00")
    assert other != verifier.Verdict.NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT


def test_14c_ci_deselects_only_tests_that_exist():
    """One of the two documented baseline deselects drove the deleted job. A
    deselect for a test that no longer exists is a line nobody can check."""
    workflow = _read(".github/workflows/pr-ads-153d-checks.yml")
    deselected = re.findall(r'--deselect "([^"]+)"', workflow)
    assert deselected == [
        "tests/test_search_terms_followup_fixes.py::"
        "test_daily_empty_search_terms_marks_success_not_success_empty"]
    for node_id in deselected:
        rel, name = node_id.split("::")
        assert re.search(rf"^def {re.escape(name)}\(", _read(rel), re.M), node_id
    # CLAUDE.md documents the same baseline.
    claude = _read("CLAUDE.md")
    assert re.findall(r'--deselect "([^"]+)"', claude) == deselected
    assert "test_weekly_empty_search_terms" not in claude


# ═════════════════════════════════════════════════════════════════════════════
# B — the Unit Economics page
# ═════════════════════════════════════════════════════════════════════════════
PAGE = "unit-economics"
ENDPOINT = "/api/reports/unit-economics"
SERVICE = "services/canonical_unit_economics_service.py"


def _pages(js: str = JS) -> set:
    return set(re.findall(r'"([a-z0-9-]+)"',
                          re.search(r"const PAGES = \[(.*?)\];", js, re.S).group(1)))


def test_15_the_page_has_no_route_section_sidebar_entry_or_registry_key():
    assert PAGE not in _pages()
    assert f'id="page-{PAGE}"' not in HTML
    assert f'data-page="{PAGE}"' not in HTML
    assert f'id="page-explanation-{PAGE}"' not in HTML
    assert f'case "{PAGE}":' not in JS
    # No explanation, help, dataset or dependency map still keys the page, and
    # no selector, id or class still carries its name.
    for text, label in ((JS, "app.js"), (HTML, "index.html"), (CSS, "styles.css")):
        assert PAGE not in text, label
        assert "unit_economics" not in text, label
        assert "unitEconomics" not in text, label
    # The sidebar and every visible string no longer offer it.
    for text, label in ((JS, "app.js"), (HTML, "index.html")):
        assert "Unit Economics" not in text, label
        assert "unit economics" not in text.lower(), label
        for link in ("#/unit-economics", 'navigate("unit-economics"'):
            assert link not in text, (label, link)


def test_16_no_loader_state_style_route_or_service_is_left_behind():
    for symbol in ("loadUnitEconomics", "renderUnitEconomicsPage",
                   "unitEconomicsData", "unitEconomicsStatus",
                   "UNIT_ECONOMICS_SCOPE", "ueRefresh", "ueWindow",
                   "unit-economics-window", "unit-economics-refresh-btn",
                   "unit-economics-kpis", "unit-economics-verdict",
                   "unit-economics-table-body"):
        assert symbol not in JS, symbol
        assert symbol not in HTML, symbol
    for cls in ("unit-economics-verdict-card", "unit-economics-verdict-explanation"):
        assert cls not in CSS, cls
    for name in ("get_unit_economics", "build_unit_economics",
                 "canonical_unit_economics_service", "DEFAULT_ADVERTISING_SCOPE"):
        assert name not in SERVER, name

    assert not (ROOT / SERVICE).exists()
    assert importlib.util.find_spec(
        "services.canonical_unit_economics_service") is None
    # Nothing in production imports it, or names it as a string to import.
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "services.canonical_unit_economics_service", path
                assert not (node.module == "services" and any(
                    a.name == "canonical_unit_economics_service"
                    for a in node.names)), path
            elif isinstance(node, ast.Import):
                assert not any(
                    a.name == "services.canonical_unit_economics_service"
                    for a in node.names), path
    # The helpers the page shared with other pages are still there for them.
    for shared in ("function rowMetricHtml(", "function canonicalMetricHtml(",
                   "function canonicalValueHtml(", "function fmtPeriodRatio(",
                   "function handleBusinessWindowSelectChange("):
        assert shared in JS, shared


def test_17_the_endpoint_is_gone_from_the_running_app(monkeypatch):
    import api.server as server
    assert ENDPOINT not in {r.path for r in server.app.routes}
    assert ENDPOINT not in SERVER and ENDPOINT not in JS
    client, auth = _client(monkeypatch)
    assert client.get(ENDPOINT, headers=auth).status_code == 404
    assert client.get(ENDPOINT + "?window=ytd&scope=campaign_attributable",
                      headers=auth).status_code == 404
    assert client.post(ENDPOINT, headers=auth, json={}).status_code in (404, 405)
    # The sibling report routes other pages read are untouched.
    paths = {r.path for r in server.app.routes}
    # PR-ADS-168B deleted /api/revenue-by-source with its page; PR-ADS-177C
    # deleted /api/revenue-attribution (no page read it) and kept its /audit,
    # which the Revenue Health page reads. PR-ADS-178C deleted
    # /api/revenue-deals (no page read it after PR-ADS-178B); the deal list's
    # page route is /api/pipeline, and Dashboard → Deals' is kept.
    assert {"/api/revenue-attribution/audit", "/api/pipeline",
            "/api/dashboard/deals", "/api/dashboard/overview"} <= paths
    assert "/api/revenue-attribution" not in paths
    assert "/api/revenue-deals" not in paths


def test_18_an_old_bookmark_lands_on_the_dashboard():
    """navigate() sends any key outside PAGES to the Dashboard. Evaluate the
    real guard with the real PAGES list rather than trusting the layout."""
    pages = re.search(r"const PAGES = \[.*?\];", JS, re.S).group(0)
    guard = "if (!PAGES.includes(page) || adminOnlyDenied(page)) {"
    assert guard in JS
    after = JS[JS.index(guard):][:140]
    assert 'navigate("dashboard", { updateHash: true, replace: true });' in after
    # It is not a redirect to somewhere else either.
    redirects = JS.split("const RETIRED_PAGE_REDIRECTS = {")[1].split("\n};")[0]
    assert "economics" not in redirects
    script = pages + (
        "\nconst out = {};"
        "\nfor (const p of ['unit-economics', 'roas-campaigns', 'roas-countries',"
        " 'pipeline', 'dashboard'])"
        " out[p] = PAGES.includes(p);"
        "\nprocess.stdout.write(JSON.stringify(out));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    # PR-ADS-177B: roas-campaigns is retired too; it survives only as a
    # redirect to Campaigns (RETIRED_PAGE_REDIRECTS), never as a page.
    # PR-ADS-178B: "deals" likewise (a redirect to Pipeline); "pipeline" is
    # the live page it was checked as.
    assert json.loads(out.stdout) == {
        "unit-economics": False, "roas-campaigns": False, "roas-countries": True,
        "pipeline": True, "dashboard": True}


def test_19_every_sidebar_page_is_still_routable():
    nav = re.findall(r'class="nav-item[^"]*" data-page="([a-z0-9-]+)"', HTML)
    assert len(nav) == len(set(nav)), "a sidebar entry is duplicated"
    pages = _pages()
    assert set(nav) <= pages, sorted(set(nav) - pages)
    for page in nav:
        assert f'id="page-{page}"' in HTML, page
        assert f'case "{page}":' in JS, page
    # Every page section still present has a route (no orphaned markup).
    sections = set(re.findall(r'<section id="page-([a-z0-9-]+)"', HTML))
    assert sections <= pages, sorted(sections - pages)
    # The group it left keeps its other entries. PR-ADS-174A split the Sales
    # Results group ([leads, deals, roas-campaigns, roas-countries]) across the
    # six-item menu: Pipeline holds leads and deals, and each Revenue & Spend
    # page sits with its evidence page.
    def strip(item):
        body = HTML.split(f'data-section="{item}"')[1].split("</nav>")[0]
        return re.findall(r'data-page="([a-z0-9-]+)"', body)
    # PR-ADS-178B: Pipeline is one page, the item itself (no strip).
    assert 'data-section="pipeline"' not in HTML
    assert 'data-page="pipeline" data-nav-item="pipeline"' in HTML
    # PR-ADS-177B: Campaign Revenue & Spend merged into the Campaigns page.
    # PR-ADS-177C: Keywords & Search Terms is one page.
    assert strip("campaigns") == ["campaigns", "search-terms"]
    # PR-ADS-176B: Countries is one page, the item itself (no strip).
    assert 'data-page="roas-countries" data-nav-item="countries"' in HTML
    assert "unit-economics" not in HTML
    # The loader switch lost exactly one case: every page it loaded before
    # this slice, bar Unit Economics, is still loaded by it.
    switch = JS.split("function loadPage(")[1].split("\n}\n")[0]
    loaded = set(re.findall(r'case "([a-z0-9-]+)":', switch))
    assert loaded == {
        "dashboard", "campaigns", "search-terms", "pipeline",
        "gclid-attribution", "scheduler", "health", "backfill",
        "roas-countries", "revenue-health",
        "channels", "system"}  # PR-ADS-168B: revenue-by-source deleted;
    #                              PR-ADS-176B: geo deleted (#/geo redirects);
    #                              PR-ADS-177B: roas-campaigns deleted (redirects)
    #                              PR-ADS-177C: keywords is a tab (redirects)
    assert loaded <= pages


def test_20_the_reader_cutover_audit_still_holds(monkeypatch):
    from scripts import audit_customer_revenue_reader_cutover as audit
    a = audit.Audit()
    audit.structural_checks(a)
    audit.pure_checks(a)
    assert a.violations == [], a.violations
    assert a.checks and all(a.checks.values())

    consumers = {row["consumer"] for row in audit.INVENTORY}
    assert SERVICE not in consumers
    assert not [row for row in audit.INVENTORY
                if "unit" in row["surface"].lower()]
    for rel in consumers:
        assert (ROOT / rel).exists(), rel
    assert SERVICE not in audit.OWNERS and SERVICE not in audit.RETIRED_LEGACY
    # Its entry point is still named, so a module that brings it back is a
    # reader that must be inventoried.
    assert "build_unit_economics" in audit.READER_ENTRY_POINTS
    builders = dict(audit._page_builders())                      # noqa: SLF001
    assert "unit_economics" not in builders
    assert {"revenue_decision_mart", "revenue_attribution", "revenue_by_source",
            "dashboard_overview", "dashboard_revenue", "dashboard_channels",
            "dashboard_campaigns", "dashboard_countries",
            "dashboard_deals", "pipeline"} <= set(builders)

    # Counterfactual: leave the deleted file in the inventory and the audit
    # says so — the row was removed because it had to be, not for tidiness.
    stale_row = {"consumer": SERVICE, "surface": ENDPOINT,
                 "families": ("customers",), "canonical_via": "reader_row",
                 "status": "migrated"}
    monkeypatch.setattr(audit, "INVENTORY", (*audit.INVENTORY, stale_row))
    b = audit.Audit()
    # PR-ADS-178B: the audit now completes over a tree that lacks an
    # inventoried file (161E test_26 runs it on the base tree, which predates
    # services/pipeline_service.py) and records the missing file as an
    # inventory_complete violation — it used to crash on the read first.
    audit.structural_checks(b)
    assert b.checks.get("inventory_complete") is False
    assert any(SERVICE in v for v in b.violations), b.violations


def test_21_the_closed_won_truth_audit_still_holds():
    from scripts import audit_customer_closed_won_truth as audit
    a = audit.Audit()
    found = audit.structural_checks(a)
    assert a.violations == [], a.violations
    assert a.checks and all(a.checks.values())
    assert found["production_consumers"] == []
    for rel in (*audit.READER_CONTRACT, *audit.CERTIFICATION_CONSUMERS):
        assert (ROOT / rel).exists(), rel
        assert "unit_economics" not in rel
    assert SERVICE not in audit.PRODUCTION_SURFACES


def test_22_the_sql_doctrine_inventory_still_holds():
    from analysis import sql_doctrine_registry as registry
    from scripts import audit_sql_doctrine_inventory as inventory
    static = inventory.build_static(ROOT)
    assert static["unclassified_occurrences"] == []
    assert static["registry_problems"] == []

    # No rule, consumer, CPQL entry, decision surface or known conflict points
    # at either thing this slice deleted.
    gone = ("scheduler/weekly.py", "scheduler.weekly", SERVICE,
            "canonical_unit_economics", ENDPOINT)
    for rule in registry.RULES:
        assert (ROOT / rule["path"]).exists(), rule["id"]
    for entry in (*registry.RULES, *registry.CONSUMERS, *registry.CPQL_CONSUMERS,
                  *registry.DECISION_SURFACES,
                  *registry.KNOWN_CONTRACT_CONFLICTS):
        blob = json.dumps(entry, default=str)
        for name in gone:
            assert name not in blob, (name, blob[:120])
    for entry in (*registry.CONSUMERS, *registry.CPQL_CONSUMERS,
                  *registry.DECISION_SURFACES,
                  *registry.KNOWN_CONTRACT_CONFLICTS):
        location = entry.get("code_location", "")
        if location:
            assert (ROOT / location.split(":")[0]).exists(), location
    assert not [o for o in static["occurrences"]
                if o["path"] in ("scheduler/weekly.py", SERVICE)]
    # The leads snapshot writer names the schedulers that call it today.
    writer = next(c for c in registry.CONSUMERS
                  if c["consumer"].startswith("leads snapshot writer"))
    assert "weekly" not in writer["api_endpoint"]
    assert "monthly" in writer["api_endpoint"]
    for rel in ("scheduler/daily.py", "scheduler/monthly.py",
                "scheduler/incremental_sync.py"):
        assert "write_leads(" in _read(rel), rel
