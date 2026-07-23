"""
tests/test_pr_ads_152_pg_integration.py

PR-ADS-152 — PostgreSQL-backed integration tests for the canonical contact-outcome
layer. Proves behaviour static assertions cannot:

  §3  the classification repair default reads DATED contacts over an explicit
      all-time range (start=None, end=today account-tz); the dry run writes
      nothing and the apply run performs the idempotent local upsert;
  §6  the canonical repository deduplicates to the latest snapshot and applies
      lead_truth_exclusions, so the scoped SQL counts reconcile;
  governance: the repair repairs a stale row and backfills a missing one WITHOUT
      deleting original evidence, and re-running is a no-op.

Throwaway PostgreSQL cluster owned by the unprivileged ``postgres`` user; skipped
when the binaries / user are unavailable.
"""

import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
        self.tmp = tempfile.mkdtemp(prefix="pg152_")
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


def _seed(connection):
    """Insert a run + leads snapshots + a stale and a missing classification."""
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_type, started_at, status) "
                "VALUES ('weekly', NOW(), 'success') RETURNING id")
            run_id = cur.fetchone()[0]

            def lead(contact_id, status, created, campaign, src, run_date):
                cur.execute(
                    """INSERT INTO leads (run_id, run_date, contact_id, campaign_name,
                                          keyword, country, mql_status, status_category,
                                          gclid, source_type, company, contact_created_at,
                                          hs_analytics_source)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (run_id, run_date, contact_id, campaign, "tms", "US", "x", status,
                     "G", "paid_search", f"Co {contact_id}", created, src))

            # c1: two snapshots — older qualified, newer STILL qualified (latest wins,
            # counts once). Google Ads source.
            lead("c1", "qualified", "2026-07-05", "Brand - US", "PAID_SEARCH", "2026-07-06")
            lead("c1", "qualified", "2026-07-05", "Brand - US", "PAID_SEARCH", "2026-07-12")
            # c2: latest wrong_fit → not an SQL.
            lead("c2", "wrong_fit", "2026-07-05", "Brand - US", "PAID_SEARCH", "2026-07-12")
            # c3: organic qualified — all-source but not Google Ads.
            lead("c3", "qualified", "2026-07-05", None, "ORGANIC_SEARCH", "2026-07-12")

            # Stale classification for c1 (says in_progress) + missing for c3.
            cur.execute(
                """INSERT INTO contact_source_classification
                   (contact_key, contact_id, source_primary_raw, source_detail_raw,
                    acquisition_group, classification_rule_version, contact_created_at,
                    status_category)
                   VALUES ('c1','c1','PAID_SEARCH', NULL, 'google_ads', 'v1',
                           '2026-07-05', 'in_progress')""")


def test_repair_dry_run_reads_dated_contacts_apply_repairs(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    _seed(connection)
    from services import canonical_classification_repair_service as repair

    now = datetime(2026, 7, 23, tzinfo=timezone.utc)

    # DRY RUN: default all-time range reads the dated contacts, plans repairs, but
    # writes nothing.
    dry = repair.run_repair(dry_run=True, now=now)
    assert dry["available"] is True
    assert dry["report"]["canonical_contacts"] == 3      # c1, c2, c3 (deduped)
    assert dry["report"]["stale_repaired"] == 1          # c1 status stale
    assert dry["report"]["missing_backfilled"] == 2      # c2 and c3 have no classification
    assert dry["written"] == 0                           # dry run writes nothing

    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status_category FROM contact_source_classification WHERE contact_key='c1'")
            assert cur.fetchone()[0] == "in_progress"    # unchanged by the dry run
            cur.execute("SELECT COUNT(*) FROM contact_source_classification")
            assert cur.fetchone()[0] == 1                # c3 not yet backfilled

    # APPLY: idempotent local upsert repairs c1 and backfills c3.
    applied = repair.run_repair(dry_run=False, now=now)
    assert applied["written"] >= 2
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status_category FROM contact_source_classification WHERE contact_key='c1'")
            assert cur.fetchone()[0] == "qualified"      # repaired to canonical truth
            cur.execute("SELECT source_detail_raw FROM contact_source_classification WHERE contact_key='c3'")
            assert cur.fetchone()[0] is None             # drill-down never invented
            cur.execute("SELECT acquisition_group FROM contact_source_classification WHERE contact_key='c3'")
            assert cur.fetchone()[0] == "organic"        # genuine durable source

    # Re-running is a no-op (idempotent).
    again = repair.run_repair(dry_run=True, now=now)
    assert again["report"]["stale_repaired"] == 0
    assert again["report"]["missing_backfilled"] == 0


def _seed_ordering(connection):
    """Seed the latest-snapshot ordering scenarios. Window = current_quarter
    (Jul 1–23, 2026); in-window date = 2026-07-05, out-of-window date = 2026-06-01."""
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO runs (run_type, started_at, status) "
                        "VALUES ('weekly', NOW(), 'success') RETURNING id")
            run_id = cur.fetchone()[0]

            def lead(contact_id, status, created, run_date):
                cur.execute(
                    """INSERT INTO leads (run_id, run_date, contact_id, campaign_name,
                                          keyword, country, mql_status, status_category,
                                          gclid, source_type, company, contact_created_at,
                                          hs_analytics_source)
                       VALUES (%s,%s,%s,'Brand - US','tms','US','x',%s,'G',
                               'paid_search',%s,%s,'PAID_SEARCH')""",
                    (run_id, run_date, contact_id, status, f"Co {contact_id}", created))

            # s1: older in-window qualified, newer OUT-OF-WINDOW wrong_fit → latest is
            # out of window → NOT counted.
            lead("s1", "qualified", "2026-07-05", "2026-07-06")
            lead("s1", "wrong_fit", "2026-06-01", "2026-07-12")
            # s2: older in-window qualified, newer OUT-OF-WINDOW qualified → NOT counted.
            lead("s2", "qualified", "2026-07-05", "2026-07-06")
            lead("s2", "qualified", "2026-06-01", "2026-07-12")
            # s3: older OUT-OF-WINDOW, newer in-window qualified → counted once.
            lead("s3", "wrong_fit", "2026-06-01", "2026-07-06")
            lead("s3", "qualified", "2026-07-05", "2026-07-12")
            # s4: same-day snapshots — the LATER inserted row (higher id) is qualified;
            # id DESC must pick it → counted (id ASC would pick wrong_fit → not counted).
            lead("s4", "wrong_fit", "2026-07-05", "2026-07-12")
            lead("s4", "qualified", "2026-07-05", "2026-07-12")
            # s5: identical contact_created_at across snapshots; latest run_date status
            # (wrong_fit) must win → NOT counted.
            lead("s5", "qualified", "2026-07-05", "2026-07-06")
            lead("s5", "wrong_fit", "2026-07-05", "2026-07-12")


def test_latest_snapshot_selected_before_window(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    _seed_ordering(connection)
    from services import canonical_contact_outcome_service as canon

    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    out = canon.build(canon.WINDOW_BUSINESS, "current_quarter", now=now)
    counted = {c["contact_key"] for c in out["contacts"] if c["is_sql"]}
    # Only s3 (newer in-window qualified) and s4 (same-day, id DESC → qualified).
    assert counted == {"s3", "s4"}
    assert out["counts"]["total_all_source_sqls"] == 2
    assert out["counts"]["google_ads_source_sqls"] == 2
    # s1/s2 prove a newer out-of-window snapshot suppresses an older in-window one.
    assert "s1" not in counted and "s2" not in counted
    # s5 proves latest status wins even with identical contact_created_at.
    assert "s5" not in counted


def test_canonical_scopes_dedupe_and_exclude(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    _seed(connection)
    from services import canonical_contact_outcome_service as canon

    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    out = canon.build(canon.WINDOW_BUSINESS, "current_quarter", now=now)
    assert out["available"] is True
    # c1 (qualified, deduped once) + c3 (organic qualified) = 2 all-source SQLs;
    # c2 latest wrong_fit is not an SQL.
    assert out["counts"]["total_all_source_sqls"] == 2
    assert out["counts"]["google_ads_source_sqls"] == 1   # only c1 is Google Ads

    # Exclude c1 → it disappears from every scope.
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lead_truth_exclusions (lead_id, reason) VALUES ('c1','test')")
    out2 = canon.build(canon.WINDOW_BUSINESS, "current_quarter", now=now)
    assert out2["counts"]["google_ads_source_sqls"] == 0
    assert out2["counts"]["excluded_sql_contacts"] == 1
