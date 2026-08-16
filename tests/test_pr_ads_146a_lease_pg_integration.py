"""
PR-ADS-146A — PostgreSQL-backed integration tests for the durable bootstrap lease.

Proves behaviour static assertions and mocked-DB unit tests cannot: that the real
SQL (make_interval, partial unique index, SELECT ... FOR UPDATE) enforces the
atomic single-owner claim and stale-lease recovery contract.

  §1  db.schema.init_db() adds the lease columns + the partial unique index and
      is idempotent.
  §1  Only one of two concurrent workers acquires the keyword_bootstrap lease.
  §2  A stale (expired) lease is taken over by a new worker, preserving the
      completed-chunk ledger.
  §5  renew/release honour lease-token ownership (a non-owner cannot mutate).

Throwaway PostgreSQL cluster owned by the unprivileged ``postgres`` user; skipped
when the binaries / user are unavailable.
"""

import glob
import os
import shutil
import socket
import subprocess
import tempfile
import time

import pytest

_PG_BIN = None
for _cand in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
    if os.path.exists(os.path.join(_cand, "initdb")):
        _PG_BIN = _cand
        break


def _have_postgres() -> bool:
    if not _PG_BIN:
        return False
    try:
        import pwd
        pwd.getpwnam("postgres")
    except (KeyError, ImportError):
        return False
    return shutil.which("sudo") is not None


pytestmark = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


class _PgCluster:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pgkw146a_")
        # PR-ADS-153D: `postgres` must be able to create data/ and write the
        # socket + log inside this directory. mkdtemp gives us 0700, so a
        # different user cannot even traverse it — which is why this harness
        # errored with "initdb: could not access directory ... Permission
        # denied" on a CI runner while passing locally as root. chmod only
        # needs ownership, which we have, and works without root; handing
        # ownership over is nicer where permitted, so try it too but never
        # depend on it.
        os.chmod(self.tmp, 0o777)
        _run(["sudo", "-n", "chown", "-R", "postgres:postgres", self.tmp])
        _run(["chown", "-R", "postgres:postgres", self.tmp])
        self.data = os.path.join(self.tmp, "data")
        self.port = _free_port()
        self.url = None

    def start(self):
        r = _run(["sudo", "-u", "postgres", os.path.join(_PG_BIN, "initdb"),
                  "-D", self.data, "-A", "trust", "-E", "UTF8"])
        if r.returncode != 0:
            raise RuntimeError(f"initdb failed: {r.stderr}")
        r = _run(["sudo", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
                  "-D", self.data, "-l", os.path.join(self.tmp, "log"), "-w",
                  "-o", f"-p {self.port} -k {self.tmp} -h 127.0.0.1", "start"])
        if r.returncode != 0:
            raise RuntimeError(f"pg_ctl start failed: {r.stderr}")
        import psycopg2
        for _ in range(20):
            try:
                c = psycopg2.connect(host="127.0.0.1", port=self.port,
                                     user="postgres", dbname="postgres")
                break
            except psycopg2.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError("could not connect to freshly started postgres")
        c.autocommit = True
        c.cursor().execute("CREATE DATABASE app")
        c.close()
        self.url = f"postgresql://postgres@127.0.0.1:{self.port}/app"
        return self

    def stop(self):
        _run(["sudo", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
              "-D", self.data, "-w", "stop"])
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture()
def pg():
    cluster = _PgCluster().start()
    try:
        yield cluster
    finally:
        cluster.stop()


def _use_cluster(monkeypatch, pg):
    import db.connection as connection
    monkeypatch.setenv("DATABASE_URL", pg.url)
    connection._pool = None
    connection.init_pool()
    import db.schema as schema
    schema.init_db()
    return connection


from datetime import date  # noqa: E402

_RANGE = dict(date_from=date(2026, 5, 1), date_to=date(2026, 7, 13))


# ── §1 schema idempotence ────────────────────────────────────────────────────
def test_lease_schema_columns_and_index(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.schema as schema
    schema.init_db()   # second apply must be a no-op
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE column_name IN
                    ('lease_token','heartbeat_at','lease_expires_at','last_progress_at')),
                  (SELECT COUNT(*) FROM pg_indexes
                     WHERE indexname = 'uq_recovery_running_keyword_bootstrap')
                FROM information_schema.columns
                WHERE table_name = 'revenue_recovery_jobs'
            """)
            cols, idx = cur.fetchone()
    assert cols == 4
    assert idx == 1


# ── §1 atomic single-owner claim ─────────────────────────────────────────────
def test_only_one_worker_acquires_lease(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as w
    a = w.acquire_recovery_lease("keyword_bootstrap", "tokenA", 900, **_RANGE)
    b = w.acquire_recovery_lease("keyword_bootstrap", "tokenB", 900, **_RANGE)
    assert a["claimed"] is True and a["reason"] == "created"
    assert b["claimed"] is False and b["reason"] == "active_lease"

    # Exactly one running keyword_bootstrap row exists (the partial unique index).
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM revenue_recovery_jobs "
                        "WHERE job_type='keyword_bootstrap' AND status='running'")
            assert cur.fetchone()[0] == 1


# ── §2 stale-lease recovery preserves the ledger ─────────────────────────────
def test_stale_lease_is_taken_over_and_ledger_preserved(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as w
    claim = w.acquire_recovery_lease("keyword_bootstrap", "dead", 900, **_RANGE)
    job_id = claim["job"]["job_id"]
    assert w.renew_recovery_lease(job_id, "dead", 900, completed_chunks=["2026-05", "2026-06"])

    # Simulate a crashed worker: force the lease to have already expired.
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE revenue_recovery_jobs "
                        "SET lease_expires_at = NOW() - interval '1 hour' WHERE job_id = %s",
                        (job_id,))

    # A fresh worker takes over the SAME row, keeping the completed-chunk ledger.
    takeover = w.acquire_recovery_lease("keyword_bootstrap", "alive", 900, **_RANGE)
    assert takeover["claimed"] is True and takeover["reason"] == "resumed"
    assert takeover["job"]["job_id"] == job_id
    assert set(takeover["job"]["completed_chunks"]) == {"2026-05", "2026-06"}

    # The dead worker can no longer renew or release — it lost the lease (§5).
    assert w.renew_recovery_lease(job_id, "dead", 900) is False
    assert w.release_recovery_lease(job_id, "dead", status="success") is False
    assert w.renew_recovery_lease(job_id, "alive", 900) is True


# ── §1 fresh claim after a clean release ─────────────────────────────────────
def test_new_job_created_after_success_release(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as w
    first = w.acquire_recovery_lease("keyword_bootstrap", "t1", 900, **_RANGE)
    fid = first["job"]["job_id"]
    assert w.release_recovery_lease(fid, "t1", status="success",
                                    completed_chunks=["2026-05", "2026-06"]) is True

    # A later run for a newly-closed month may seed the fresh job from the prior
    # ledger so already-synced months are never re-pulled.
    second = w.acquire_recovery_lease("keyword_bootstrap", "t2", 900,
                                      date_from=date(2026, 5, 1), date_to=date(2026, 8, 20),
                                      seed_completed_chunks=["2026-05", "2026-06"])
    assert second["claimed"] is True and second["reason"] == "created"
    assert second["job"]["job_id"] != fid
    assert set(second["job"]["completed_chunks"]) == {"2026-05", "2026-06"}


# ── §6 proven-coverage watermark from successful batches ─────────────────────
def test_latest_successful_batch_source_date(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as w
    src, ds = "google_ads_api", "keyword_facts"
    # A later FAILED batch must NOT advance the proven watermark past an earlier
    # successful one.
    b1 = w.start_sync_batch(source=src, dataset=ds, sync_type="backfill",
                            date_from=date(2026, 6, 1), date_to=date(2026, 6, 30))
    w.finish_sync_batch(batch_id=b1, status="success", row_count=5, last_source_date=date(2026, 6, 30))
    b2 = w.start_sync_batch(source=src, dataset=ds, sync_type="daily",
                            date_from=date(2026, 7, 1), date_to=date(2026, 7, 13))
    w.finish_sync_batch(batch_id=b2, status="failed", row_count=0, error_message="boom")
    assert w.latest_successful_batch_source_date(src, ds) == date(2026, 6, 30)

    # A later SUCCESSFUL batch advances it.
    b3 = w.start_sync_batch(source=src, dataset=ds, sync_type="daily",
                            date_from=date(2026, 7, 1), date_to=date(2026, 7, 13))
    w.finish_sync_batch(batch_id=b3, status="success", row_count=0, last_source_date=date(2026, 7, 13))
    assert w.latest_successful_batch_source_date(src, ds) == date(2026, 7, 13)


# ── §B1 interval-union coverage detects a failed-month gap (real Postgres) ────
def test_interval_union_coverage_reports_february_gap(monkeypatch, pg):
    _use_cluster(monkeypatch, pg)
    import db.writers as w
    from services import keyword_sync_service as kss
    src, ds = "google_ads_api", "keyword_facts"

    def _batch(df, dt, status):
        bid = w.start_sync_batch(source=src, dataset=ds, sync_type="backfill",
                                 date_from=df, date_to=dt)
        w.finish_sync_batch(batch_id=bid, status=status, row_count=1,
                            last_source_date=(dt if status == "success" else None))

    _batch(date(2026, 1, 1), date(2026, 1, 31), "success")
    _batch(date(2026, 2, 1), date(2026, 2, 28), "failed")   # February gap
    _batch(date(2026, 3, 1), date(2026, 3, 31), "success")

    # Only the successful intervals are returned.
    ivs = w.successful_batch_intervals(src, ds, date(2026, 1, 1), date(2026, 3, 31))
    assert ivs == [(date(2026, 1, 1), date(2026, 1, 31)),
                   (date(2026, 3, 1), date(2026, 3, 31))]

    cov = kss.selected_window_coverage(date(2026, 1, 1), date(2026, 3, 31), is_all_time=False)
    assert cov["selected_window_fully_synced"] is False
    assert cov["missing_window_ranges"] == [{"start": "2026-02-01", "end": "2026-02-28"}]

    # Re-running February successfully closes the gap.
    _batch(date(2026, 2, 1), date(2026, 2, 28), "success")
    cov2 = kss.selected_window_coverage(date(2026, 1, 1), date(2026, 3, 31), is_all_time=False)
    assert cov2["selected_window_fully_synced"] is True
    assert cov2["missing_window_ranges"] == []
