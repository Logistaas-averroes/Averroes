"""PR-ADS-161J — the platform certification runs daily in the app's scheduler.

The job runs ``scripts.audit_platform_certification`` after the morning sync
and records ONE verdict row. It adds no gate logic. It fails closed: when the
certification cannot run, the recorded verdict is NOT CERTIFIED, never a pass
and never a missing row.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import platform_certification_service as pcs

T0 = datetime(2026, 11, 17, 7, 30, tzinfo=timezone.utc)


def _clock():
    ticks = iter([T0, T0 + timedelta(minutes=9)])
    return lambda: next(ticks)


class _Writer:
    def __init__(self, ok=True):
        self.ok = ok
        self.rows = []

    def __call__(self, started, finished, exit_code, verdict, summary, *,
                 run_reason="scheduled"):
        self.rows.append(dict(started=started, finished=finished,
                              exit_code=exit_code, verdict=verdict,
                              summary=summary, run_reason=run_reason))
        return self.ok


REPORT = {
    "verdict": "CERTIFIED", "exit_code": 0,
    "generated_at": T0.isoformat(),
    "audits": [{"key": "closed_won_truth", "module": "m", "outcome": "holds",
                "exit_code": 0, "seconds": 2.9, "violations": [],
                "unavailable": [], "report": {"huge": "x" * 10_000}}],
    "crm_data_to_fix": {"deals_with_any_exception": 25},
}


def test_01_the_job_is_registered_after_the_morning_sync():
    from api import scheduler
    assert "platform_certification" in scheduler.get_registered_job_ids()
    desc = scheduler._SCHEDULE_DESCRIPTIONS["platform_certification"]
    assert "10:30" in desc and "Asia/Amman" in desc
    assert "09:00" in scheduler._SCHEDULE_DESCRIPTIONS["daily_incremental_sync"]


def test_02_start_scheduler_adds_the_job_with_its_cron(monkeypatch):
    from api import scheduler
    monkeypatch.setattr(scheduler, "_scheduler", None)
    scheduler.start_scheduler()
    try:
        job = scheduler._scheduler.get_job("platform_certification")
        assert job is not None
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "10" and fields["minute"] == "30"
    finally:
        scheduler._scheduler.shutdown(wait=False)
        monkeypatch.setattr(scheduler, "_scheduler", None)


def test_03_a_certified_run_is_recorded_with_a_compact_summary():
    writer = _Writer()
    out = pcs.run_and_record(runner=lambda: REPORT, writer=writer, now=_clock())
    assert out == {"verdict": "CERTIFIED", "exit_code": 0, "recorded": True,
                   "started_at": T0.isoformat(),
                   "finished_at": (T0 + timedelta(minutes=9)).isoformat()}
    row = writer.rows[0]
    assert (row["exit_code"], row["verdict"]) == (0, "CERTIFIED")
    assert row["summary"]["crm_data_to_fix"] == {"deals_with_any_exception": 25}
    assert "report" not in row["summary"]["audits"][0]


def test_04_a_certification_that_cannot_run_is_recorded_not_certified():
    def broken():
        raise RuntimeError("database not ready")
    writer = _Writer()
    out = pcs.run_and_record(runner=broken, writer=writer, now=_clock())
    assert out["verdict"] == "NOT CERTIFIED" and out["exit_code"] == 2
    row = writer.rows[0]
    assert row["verdict"] == "NOT CERTIFIED"
    assert "database not ready" in row["summary"]["error"]


@pytest.mark.parametrize("bad", [
    {"verdict": "CERTIFIED", "exit_code": 7},
    {"verdict": "CERTIFIED"},
    {"exit_code": 0},
])
def test_05_an_unrecognised_report_never_records_a_pass(bad):
    writer = _Writer()
    out = pcs.run_and_record(runner=lambda: bad, writer=writer, now=_clock())
    assert out["exit_code"] == 2 and out["verdict"] == "NOT CERTIFIED"


def test_06_a_failed_write_is_reported_not_hidden():
    out = pcs.run_and_record(runner=lambda: REPORT, writer=_Writer(ok=False),
                             now=_clock())
    assert out["recorded"] is False


def test_07_the_scheduled_wrapper_never_raises(monkeypatch):
    from api import scheduler
    monkeypatch.setattr(pcs, "run_and_record",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    scheduler._run_platform_certification_scheduled()
    assert scheduler._job_state["platform_certification"] is False


def test_08_the_verdict_round_trips_through_a_live_database(monkeypatch):
    from tests import test_pr_ads_153b_pg_integration as pgmod
    if not pgmod._have_postgres():
        pytest.skip("PostgreSQL not available")
    cluster = pgmod._PgCluster().start()
    import db.connection as connection
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)
        monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()
        from db.schema import init_db
        init_db()
        out = pcs.run_and_record(runner=lambda: REPORT, now=_clock())
        assert out["recorded"] is True
        with cluster.connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT exit_code, verdict, run_reason,
                           summary->'crm_data_to_fix'->>'deals_with_any_exception'
                           FROM platform_certification_runs""")
            assert cur.fetchall() == [(0, "CERTIFIED", "scheduled", "25")]
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None
        cluster.stop()
