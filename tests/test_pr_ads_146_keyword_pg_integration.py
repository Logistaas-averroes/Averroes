"""
PR-ADS-146 — PostgreSQL-backed integration tests (keyword durable facts).

Proves behaviour static source-string assertions cannot:

  §1  db.schema.init_db() creates keyword_daily_facts + the unique natural-key
      index and is idempotent (re-applying the DDL is a no-op);
  §2  write_keyword_daily_facts upserts on the immutable natural key — a repeated
      pull for the same fact UPDATES one row (metrics replaced, never summed), so
      overlapping scheduler snapshots cannot multiply totals;
  §3  two campaign IDs sharing a display name, and two criterion IDs sharing a
      keyword, both persist as separate facts; source_date is preserved;
  §4  end-to-end connector-shaped GBP rows → writer → repository → service prove
      £ native is NOT rendered as $ USD and the verified subtotal survives an
      unverified legacy row;
  §5  quality is LATEST-OBSERVED per criterion — never averaged across snapshots.

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
from datetime import date, datetime, timezone
from unittest.mock import patch

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
        self.tmp = tempfile.mkdtemp(prefix="pgkw146_")
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


def _kwrow(**kw):
    base = dict(customer_id="C1", campaign_id="10", campaign_name="Brand - UK",
                ad_group_id="100", ad_group_name="Core", criterion_id="1001",
                keyword_text="freight software", match_type="BROAD",
                criterion_status="ENABLED", cost_micros=10_000_000,
                currency_code="GBP", source="google_ads_api",
                clicks=10, impressions=200, conversions=0.0, quality_score=8,
                expected_ctr="AVERAGE", ad_relevance="AVERAGE",
                landing_page_experience="AVERAGE",
                quality_observed_at="2026-07-12T05:00:00Z", date="2026-07-12")
    base.update(kw)
    return base


def _count(connection, sql, params=None):
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()


# ── §1 schema + idempotence ──────────────────────────────────────────────────
def test_schema_creates_table_and_is_idempotent(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.schema as schema
    schema.init_db()   # second apply must be a no-op
    row = _count(connection, """
        SELECT
          to_regclass('keyword_daily_facts') IS NOT NULL,
          (SELECT COUNT(*) FROM pg_indexes WHERE indexname = 'idx_keyword_daily_facts_unique'),
          (SELECT COUNT(*) FROM migrations WHERE migration_id = 'PR-ADS-146-keyword-daily-facts')
    """)
    assert row[0] is True
    assert row[1] == 1
    assert row[2] == 1


# ── §2 upsert: no multiplication ─────────────────────────────────────────────
def test_repeated_pull_updates_one_row(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    writers.write_keyword_daily_facts(None, [_kwrow(clicks=10, cost_micros=10_000_000)])
    writers.write_keyword_daily_facts(None, [_kwrow(clicks=15, cost_micros=12_000_000)])
    n, clicks, micros = _count(connection,
        "SELECT COUNT(*), MAX(clicks), MAX(cost_micros) FROM keyword_daily_facts")
    assert n == 1                      # one fact row, not two
    assert clicks == 15                # replaced, not summed (would be 25)
    assert micros == 12_000_000


# ── §3 identity separation + source_date ─────────────────────────────────────
def test_duplicate_ids_stay_separate_and_source_date_preserved(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    writers.write_keyword_daily_facts(None, [
        _kwrow(campaign_id="10", campaign_name="Brand", criterion_id="1001"),
        _kwrow(campaign_id="20", campaign_name="Brand", criterion_id="1001"),  # same name, diff id
        _kwrow(campaign_id="10", criterion_id="1001", keyword_text="freight"),
        _kwrow(campaign_id="10", criterion_id="2002", keyword_text="freight"),  # same kw, diff crit
    ])
    (n,) = _count(connection, "SELECT COUNT(*) FROM keyword_daily_facts")
    assert n == 3   # (10/1001), (20/1001), (10/2002) — the two 10/1001 collapsed
    (sd,) = _count(connection, "SELECT DISTINCT source_date FROM keyword_daily_facts")
    assert str(sd) == "2026-07-12"


# ── §5 quality latest-observed by quality_observed_at, never averaged ─────────
def test_quality_latest_by_observed_at_not_averaged(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    import db.keyword_repository as kr
    # The EARLIER activity date carries the LATER observation → its quality wins,
    # proving selection is by quality_observed_at, not by source_date.
    writers.write_keyword_daily_facts(None, [
        _kwrow(date="2026-07-02", criterion_id="1001", quality_score=5, cost_micros=1_000_000,
               quality_observed_at="2026-07-02T05:00:00Z"),
        _kwrow(date="2026-07-01", criterion_id="1001", quality_score=8, cost_micros=1_000_000,
               quality_observed_at="2026-07-03T05:00:00Z"),
    ])
    agg = kr.fetch_keyword_aggregates(date(2026, 7, 1), date(2026, 7, 2))
    assert agg["available"]
    assert len(agg["rows"]) == 1
    assert agg["rows"][0]["quality_score"] == 8    # latest observation, not average


def test_quality_observed_at_is_pull_time_not_source_date(monkeypatch, pg):
    # A 30-day-historical activity pull that runs "today" must record the quality
    # observation date as TODAY (pull time), never the historical source_date.
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    import db.keyword_repository as kr
    writers.write_keyword_daily_facts(None, [
        _kwrow(date="2026-06-13", criterion_id="1001", quality_score=7,
               quality_observed_at="2026-07-13T05:30:00Z", cost_micros=1_000_000),
    ])
    agg = kr.fetch_keyword_aggregates(date(2026, 6, 1), date(2026, 7, 13))
    row = agg["rows"][0]
    assert str(row["quality_observed_at"]).startswith("2026-07-13")   # pull day
    assert not str(row["quality_observed_at"]).startswith("2026-06-13")  # not activity day


def test_null_quality_pull_never_wipes_prior_observation(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    import db.keyword_repository as kr
    writers.write_keyword_daily_facts(None, [
        _kwrow(criterion_id="1001", quality_score=8, quality_observed_at="2026-07-12T05:00:00Z")])
    # A later fail-closed pull with NO quality (no score, no stamp).
    writers.write_keyword_daily_facts(None, [
        _kwrow(criterion_id="1001", quality_score=None, expected_ctr=None,
               ad_relevance=None, landing_page_experience=None, quality_observed_at=None)])
    row = kr.fetch_keyword_aggregates(date(2026, 7, 1), date(2026, 7, 31))["rows"][0]
    assert row["quality_score"] == 8     # prior observation intact


# ── §2 fail closed on missing immutable identity ──────────────────────────────
def test_missing_identity_rows_rejected_before_insert(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    stats = writers.write_keyword_daily_facts(None, [
        _kwrow(criterion_id="", keyword_text="no-crit"),          # blank criterion_id
        _kwrow(ad_group_id=None, keyword_text="no-adgroup"),      # missing ad_group_id
        _kwrow(criterion_id="1001", keyword_text="valid"),        # the only valid row
    ])
    assert stats["skipped_missing_identity"] == 2
    assert stats["written"] == 1
    (n,) = _count(connection, "SELECT COUNT(*) FROM keyword_daily_facts")
    assert n == 1     # two incomplete rows never inserted, so never collide


def test_conversions_null_preserved_distinct_from_zero(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    writers.write_keyword_daily_facts(None, [
        _kwrow(criterion_id="1001", conversions=None),
        _kwrow(criterion_id="1002", conversions=0.0)])
    unavailable, zero = _count(connection,
        "SELECT (SELECT conversions FROM keyword_daily_facts WHERE criterion_id='1001'), "
        "(SELECT conversions FROM keyword_daily_facts WHERE criterion_id='1002')")
    assert unavailable is None      # unknown stays NULL
    assert float(zero) == 0.0       # genuine zero preserved


# ── §4 end-to-end: £ native ≠ $ USD, verified subtotal survives legacy ────────
def test_service_end_to_end_currency_truth(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as writers
    writers.write_keyword_daily_facts(None, [
        _kwrow(criterion_id="1001", keyword_text="freight software",
               cost_micros=100_000_000, currency_code="GBP", source="google_ads_api"),
        # legacy row: no provable currency lineage.
        _kwrow(criterion_id="9001", keyword_text="legacy kw", campaign_id="10",
               cost_micros=5_000_000, currency_code=None, source=None),
    ])

    import db.revenue_repository as rr

    def _fx(start, end, base, quote):
        return {"available": True, "rates": {date(2026, 7, 12): 1.25}}

    def _canonical(start, end):
        return {"available": True, "customer_id": "C1", "total_spend_usd": 500.0,
                "fx_complete": True, "rows": [{"campaign_id": "10", "campaign_name": "Brand - UK"}]}

    with patch.object(rr, "fetch_fx_rates", _fx), \
         patch.object(rr, "fetch_canonical_campaign_spend", _canonical), \
         patch.object(rr, "fetch_campaign_identity",
                      lambda customer_id=None: {"available": True, "mappings": []}):
        from services.keyword_evidence_service import build_keyword_evidence
        out = build_keyword_evidence("30d", now=datetime(2026, 7, 13, tzinfo=timezone.utc))

    verified = [r for r in out["rows"] if r["keyword"] == "freight software"][0]
    assert verified["spend_native"] == 100.0       # £100 native
    assert verified["spend_usd"] == 125.0          # £100 × 1.25 — NOT $100
    legacy = [r for r in out["rows"] if r["keyword"] == "legacy kw"][0]
    assert legacy["spend_usd"] is None             # withheld, never $ or £ assumed
    mon = out["kpis"]["monetary"]
    assert mon["verified_usd_spend"] == 125.0
    assert mon["monetary_completeness_status"] == "partial"

    # Read-only audit works on the live DB.
    audit = __import__("db.keyword_repository", fromlist=["fetch_keyword_legacy_audit"]).fetch_keyword_legacy_audit()
    assert audit["available"]
    assert audit["summary"]["durable_criteria"] == 2
    assert audit["summary"]["rows_unverified_currency"] == 1
    assert audit["summary"]["duplicate_key_groups"] == 0
