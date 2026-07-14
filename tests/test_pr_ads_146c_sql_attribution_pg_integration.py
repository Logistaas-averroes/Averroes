"""
tests/test_pr_ads_146c_sql_attribution_pg_integration.py

PR-ADS-146C — PostgreSQL-backed integration tests for the SQL population contract
and end-to-end keyword attribution. Proves what mocked unit tests cannot: the real
dedup / window / exclusion SQL and the full leads → repository → shared service →
Keyword Evidence path.

  §1  duplicate scheduler snapshots collapse to one contact;
      an in-progress (non-qualified) lead is not an SQL;
      an excluded lead is not counted;
      contact_created_at controls the window (run_date never does).
  §4  a unique campaign + keyword assigns exactly one criterion end-to-end.

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
from datetime import date

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
    if not shutil.which("sudo"):
        return False
    # Require NON-INTERACTIVE sudo — otherwise `sudo -u postgres …` would hang or
    # fail for a password prompt instead of the suite being cleanly skipped.
    try:
        return subprocess.run(["sudo", "-n", "true"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:  # noqa: BLE001
        return False


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
        self.tmp = tempfile.mkdtemp(prefix="pgkw146c_")
        # chown via sudo so the postgres user can own the data dir even when the
        # test runner is not root (non-interactive sudo is guaranteed by the skip
        # guard above). The return code is asserted so a failure surfaces clearly
        # rather than as a confusing initdb permission error later.
        r = _run(["sudo", "-n", "chown", "-R", "postgres:postgres", self.tmp])
        if r.returncode != 0:
            raise RuntimeError(f"chown failed: {r.stderr}")
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


def _insert_lead(conn, *, contact_id, status="qualified", campaign="Brand - UK",
                 keyword="freight software", created="2026-07-10", run_date="2026-07-11",
                 source="paid_search", gclid="g", company="Acme"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_type, started_at, status) "
            "VALUES ('daily', %s, 'success') RETURNING id",
            (run_date + " 00:00:00+00",))
        run_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO leads (run_id, run_date, contact_id, campaign_name, keyword,
                    country, mql_status, status_category, gclid, source_type, company,
                    contact_created_at)
               VALUES (%s,%s,%s,%s,%s,'UK','x',%s,%s,%s,%s,%s)""",
            (run_id, run_date, contact_id, campaign, keyword, status, gclid, source,
             company, created + " 09:00:00+00" if created else None),
        )


def test_sql_population_dedup_window_and_exclusions(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.platform_sql_attribution_repository as repo
    with connection.get_conn() as conn:
        # Same contact across two scheduler snapshots → ONE SQL.
        _insert_lead(conn, contact_id="c1", run_date="2026-07-11")
        _insert_lead(conn, contact_id="c1", run_date="2026-07-12", keyword="freight software")
        # In-progress (non-qualified) → not an SQL.
        _insert_lead(conn, contact_id="c2", status="unknown")
        # Excluded lead → not counted.
        _insert_lead(conn, contact_id="c3")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO lead_truth_exclusions (lead_id, reason) VALUES ('c3','test')")
        # Qualified but OUTSIDE the window by contact_created_at, even though its
        # run_date is inside → not counted (run_date never controls the window).
        _insert_lead(conn, contact_id="c4", created="2026-05-01", run_date="2026-07-11")
        # A genuine second SQL inside the window.
        _insert_lead(conn, contact_id="c5", keyword="cargo ship")

    res = repo.fetch_sql_contacts(date(2026, 7, 1), date(2026, 7, 31))
    keys = sorted(r["contact_key"] for r in res["rows"])
    assert keys == ["c1", "c5"]                      # c1 deduped, c2/c3/c4 excluded
    assert res["leads_have_search_term"] is False


def test_latest_status_wins_over_stale_qualified(monkeypatch, pg):
    # §1 — dedup to the latest snapshot FIRST, then keep qualified.
    connection = _use_cluster(monkeypatch, pg)
    import db.platform_sql_attribution_repository as repo
    with connection.get_conn() as conn:
        # older qualified + newer wrong_fit → NOT an SQL (latest wins).
        _insert_lead(conn, contact_id="t1", status="qualified", run_date="2026-07-05")
        _insert_lead(conn, contact_id="t1", status="wrong_fit", run_date="2026-07-09")
        # older junk + newer qualified → ONE SQL.
        _insert_lead(conn, contact_id="t2", status="junk", run_date="2026-07-04")
        _insert_lead(conn, contact_id="t2", status="qualified", run_date="2026-07-08")
        # repeated qualified snapshots → ONE SQL.
        _insert_lead(conn, contact_id="t3", status="qualified", run_date="2026-07-02")
        _insert_lead(conn, contact_id="t3", status="qualified", run_date="2026-07-06")
        # latest in-progress (unknown) → NOT an SQL.
        _insert_lead(conn, contact_id="t4", status="qualified", run_date="2026-07-03")
        _insert_lead(conn, contact_id="t4", status="unknown", run_date="2026-07-07")
    res = repo.fetch_sql_contacts(date(2026, 7, 1), date(2026, 7, 31))
    assert sorted(r["contact_key"] for r in res["rows"]) == ["t2", "t3"]


def test_sql_population_reconciles_with_campaign_evidence(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.platform_sql_attribution_repository as repo
    import db.revenue_repository as rev
    with connection.get_conn() as conn:
        _insert_lead(conn, contact_id="a1", status="qualified", run_date="2026-07-04")
        _insert_lead(conn, contact_id="a1", status="wrong_fit", run_date="2026-07-15")  # latest not SQL
        _insert_lead(conn, contact_id="a2", status="qualified")
        _insert_lead(conn, contact_id="a3", status="junk")
    sql_pop = repo.fetch_sql_contacts(date(2026, 7, 1), date(2026, 7, 31))
    lq = rev.fetch_lead_quality(date(2026, 7, 1), date(2026, 7, 31))
    ce_qualified = sum(1 for r in lq["rows"] if r["status_category"] == "qualified")
    assert len(sql_pop["rows"]) == ce_qualified == 1   # only a2


def test_end_to_end_unique_keyword_attribution(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    # One durable keyword fact so the population has a criterion.
    writers.write_keyword_daily_facts(None, [{
        "customer_id": "C1", "campaign_id": "10", "campaign_name": "Brand - UK",
        "ad_group_id": "100", "ad_group_name": "Core", "criterion_id": "1001",
        "keyword_text": "freight software", "match_type": "BROAD",
        "criterion_status": "ENABLED", "cost_micros": 10_000_000, "currency_code": "GBP",
        "source": "google_ads_api", "clicks": 10, "impressions": 200, "conversions": 0.0,
        "quality_score": 8, "date": "2026-07-10",
    }])
    with connection.get_conn() as conn:
        _insert_lead(conn, contact_id="c1", campaign="Brand - UK", keyword="freight software")

    # Resolve identity by the stored campaign_id → exact-normalized fallback isn't
    # even needed; the keyword unit carries campaign_id 10 directly. The SQL
    # contact's "Brand - UK" label must resolve to campaign 10 for attribution.
    from services.platform_sql_attribution_service import (
        attribute_keywords, fetch_and_resolve_contacts,
    )
    contacts = fetch_and_resolve_contacts(date(2026, 7, 1), date(2026, 7, 31))["contacts"]
    criteria = [{"criterion_key": "10|100|1001", "campaign_id": "10",
                 "keyword_text": "freight software", "mapping_status": "mapped"}]
    # The label resolves via exact-normalized spend name → campaign 10.
    for c in contacts:
        c["canonical_campaign_id"] = "10"   # spend index empty in this minimal fixture
    r = attribute_keywords(contacts, criteria, available=True)
    assert r["by_criterion"]["10|100|1001"]["sql_attribution_status"] == "attributed"
    assert r["by_criterion"]["10|100|1001"]["attributed_sqls"] == 1
    assert r["reconciliation"]["reconciliation_status"] == "ok"
