"""
tests/test_pr_ads_158_sql_doctrine_audit_pg_integration.py

PR-ADS-158 — PostgreSQL-backed integration coverage for the SQL doctrine
audit's repository reads and exact contact-set comparisons.

What only a real database can prove
-----------------------------------
  * the audit reads the legacy population through the SAME repository the
    pages use (``canonical_contact_outcome_repository`` — latest snapshot per
    durable key, exclusions, classification cache) and the lifecycle population
    through ``crm_funnel_repository`` (recovery COALESCE included), and compares
    them on durable contact keys;
  * the production-shaped fixture reports legacy 6 versus lifecycle 33/8/8/8
    for the 30d Evidence Window, with 40 SQL-stage contacts lacking an entry
    timestamp reported as a coverage gap, never repaired;
  * a stale classification on a non-SQL contact and a missing classification on
    a non-SQL contact downgrade the production SQL reconciliation to ``partial``
    while the SQL contacts themselves are fully classified — the hidden cause;
  * the read-only guard makes any write through the pool fail at the database,
    so the command cannot write even by accident; row counts are unchanged.

Throwaway PostgreSQL cluster owned by the unprivileged ``postgres`` user;
skipped when the binaries / user are unavailable.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
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
        self.tmp = tempfile.mkdtemp(prefix="pg158_")
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


# ── Fixture: production-shaped 30d Evidence Window ───────────────────────────
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
IN_WINDOW = date(2026, 8, 20)          # inside 30d (2026-08-10 .. 2026-09-08)
BEFORE_WINDOW = date(2026, 7, 1)       # outside 30d, inside all_time
CAMPAIGN = "Brand - US"

#: The six legacy qualified paid-search contacts (Campaign Evidence 30d = 6).
LEGACY_SQLS = ["hs-1", "hs-2", "hs-3", "hs-4", "hs-5", "hs-6"]
#: Lifecycle Google Ads-source SQL entries in the window (8). Four overlap the
#: legacy six; four are lifecycle-only.
LIFECYCLE_GA_SQLS = ["hs-1", "hs-2", "hs-3", "hs-4", "hs-7", "hs-8", "hs-9", "hs-10"]
#: 25 organic lifecycle SQL entries → all-source 33.
LIFECYCLE_ORGANIC_SQLS = [f"org-{i}" for i in range(1, 26)]
#: 40 contacts whose current stage proves SQL but carry no entry timestamp.
MISSING_SQL_DATE = [f"gap-{i}" for i in range(1, 41)]


def _seed(connection):
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO runs (run_type, started_at, status) "
                        "VALUES ('weekly', NOW(), 'success') RETURNING id")
            run_id = cur.fetchone()[0]

            def lead(contact_id, status, created, run_date="2026-09-01", src="PAID_SEARCH",
                     campaign=CAMPAIGN):
                cur.execute(
                    """INSERT INTO leads (run_id, run_date, contact_id, campaign_name,
                                          keyword, country, mql_status, status_category,
                                          gclid, source_type, company, contact_created_at,
                                          hs_analytics_source)
                       VALUES (%s,%s,%s,%s,'tms','US','x',%s,'G','paid_search',%s,%s,%s)""",
                    (run_id, run_date, contact_id, campaign, status,
                     f"Co {contact_id}", created, src))

            # Six legacy SQLs created inside the window.
            for cid in LEGACY_SQLS:
                lead(cid, "qualified", IN_WINDOW)
            # hs-5 is qualified under both doctrines, but entered SQL BEFORE the
            # window → a pure date shift. hs-6 never entered SQL in HubSpot.
            # A non-SQL contact whose latest status is unknown (missing
            # classification) and one in_progress contact with a STALE cache.
            lead("hs-unknown", "unknown", IN_WINDOW)
            lead("hs-progress", "in_progress", IN_WINDOW)

            def classify(contact_id, status, group="google_ads"):
                cur.execute(
                    """INSERT INTO contact_source_classification
                       (contact_key, contact_id, source_primary_raw, acquisition_group,
                        classification_rule_version, contact_created_at, status_category)
                       VALUES (%s,%s,'PAID_SEARCH',%s,'v1',%s,%s)""",
                    (contact_id, contact_id, group, IN_WINDOW, status))

            for cid in LEGACY_SQLS:
                classify(cid, "qualified")          # every SQL fully classified
            classify("hs-progress", "wrong_fit")     # stale: cache disagrees
            # hs-unknown deliberately has NO classification row.

            def funnel(contact_id, *, stage, entered_sql, src, campaign=None, keyword=None):
                cur.execute(
                    """INSERT INTO hubspot_contact_funnel
                       (contact_id, created_at, lifecycle_stage, date_entered_lead,
                        date_entered_sql, hs_analytics_source, hs_analytics_source_data_1,
                        hs_analytics_source_data_2, company)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (contact_id, BEFORE_WINDOW, stage, BEFORE_WINDOW, entered_sql,
                     src, campaign, keyword, f"Co {contact_id}"))

            for cid in LIFECYCLE_GA_SQLS:
                funnel(cid, stage="salesqualifiedlead", entered_sql=IN_WINDOW,
                       src="PAID_SEARCH", campaign=CAMPAIGN, keyword="tms")
            funnel("hs-5", stage="customer", entered_sql=BEFORE_WINDOW,
                   src="PAID_SEARCH", campaign=CAMPAIGN, keyword="tms")
            funnel("hs-6", stage="marketingqualifiedlead", entered_sql=None,
                   src="PAID_SEARCH", campaign=CAMPAIGN, keyword="tms")
            for cid in LIFECYCLE_ORGANIC_SQLS:
                funnel(cid, stage="salesqualifiedlead", entered_sql=IN_WINDOW,
                       src="ORGANIC_SEARCH")
            for cid in MISSING_SQL_DATE:
                funnel(cid, stage="salesqualifiedlead", entered_sql=None,
                       src="ORGANIC_SEARCH")


def _counts(connection) -> dict:
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            out = {}
            for table in ("leads", "hubspot_contact_funnel", "contact_source_classification",
                          "lead_truth_exclusions", "hubspot_lifecycle_stage_history"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                out[table] = cur.fetchone()[0]
            return out


def _by_window(report_or_windows, window_type, key):
    windows = (report_or_windows["window_comparisons"]
               if isinstance(report_or_windows, dict) and "window_comparisons" in report_or_windows
               else report_or_windows)
    for w in windows:
        if w["window_type"] == window_type and w["window"] == key:
            return w
    raise AssertionError(f"window {window_type}:{key} missing")


def _injected_resolver(_start, _end):
    from services import canonical_contact_outcome_service as canon
    return canon.default_campaign_resolver, True


# ── Tests ────────────────────────────────────────────────────────────────────
def test_repository_reads_and_exact_set_comparison(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    _seed(connection)
    from scripts import audit_sql_doctrine_inventory as cli

    sources = cli.fetch_runtime_sources(NOW)
    assert sources["legacy_inputs"]["available"] is True
    assert sources["funnel_fetch"]["available"] is True

    runtime = cli.build_runtime_comparison(
        legacy_inputs=sources["legacy_inputs"], funnel_fetch=sources["funnel_fetch"],
        windows=sources["windows"], resolver_factory=_injected_resolver)
    assert runtime["available"] is True
    assert len(runtime["windows"]) == 11

    w = _by_window(runtime["windows"], "evidence", "30d")
    # Production-shaped counts: legacy 6 vs lifecycle 33 / 8 / 8 / 8.
    assert w["legacy_counts"]["all_source"] == 6
    assert w["legacy_counts"]["google_ads_source"] == 6
    assert w["legacy_counts"]["campaign_attributable"] == 6
    assert w["lifecycle_counts"] == {"all_source": 33, "google_ads_source": 8,
                                     "campaign_attributable": 8, "keyword_attributable": 8}
    # Exact contact-set comparison, not totals.
    assert w["overlap_count"] == 4
    assert w["legacy_only_count"] == 2          # hs-5 (date shift), hs-6 (never SQL)
    assert w["lifecycle_only_count"] == 29      # 4 GA + 25 organic
    assert w["date_shifted_count"] == 1
    assert w["legacy_only_never_lifecycle_sql_count"] == 1
    assert w["population_difference"] is True
    assert w["missing_sql_entry_date_count"] == 40
    assert "lifecycle_sql_reached_without_entry_timestamp" in w["difference_reason_codes"]
    assert "event_date_moved_from_creation_to_stage_entry" in w["difference_reason_codes"]
    assert "legacy_qualified_without_lifecycle_sql_entry" in w["difference_reason_codes"]
    # The gap is a coverage gap: the lifecycle total is a confirmed subset.
    assert w["lifecycle_complete_total_publishable"] is False
    assert w["lifecycle_complete_total_reasons"] == ["missing_stage_entry_date:sql"]


def test_hidden_non_sql_gap_downgrades_sql_reconciliation(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    _seed(connection)
    from scripts import audit_sql_doctrine_inventory as cli

    sources = cli.fetch_runtime_sources(NOW)
    runtime = cli.build_runtime_comparison(
        legacy_inputs=sources["legacy_inputs"], funnel_fetch=sources["funnel_fetch"],
        windows=sources["windows"], resolver_factory=_injected_resolver)
    w = _by_window(runtime["windows"], "evidence", "30d")
    gaps = w["classification_gaps"]
    # Every SQL contact is fully classified …
    assert gaps["sql_contacts_stale_classification"] == 0
    assert gaps["sql_contacts_missing_classification"] == 0
    # … the only gaps are on non-SQL contacts …
    assert gaps["non_sql_contacts_stale_classification"] == 1
    assert gaps["non_sql_contacts_missing_classification"] == 1
    # … and production still reports partial. Restricting the gap counts to SQL
    # contacts flips the same production function to reconciled.
    assert gaps["production_status"] == "partial"
    assert gaps["status_if_only_sql_gaps_counted"] == "reconciled"
    assert gaps["irrelevant_non_sql_gap_affects_sql_status"] is True
    assert w["legacy_reconciliation"]["status"] == "partial"
    assert set(w["legacy_reconciliation"]["reasons"]) == {
        "stale_non_sql_classification", "missing_non_sql_classification"}
    assert w["legacy_complete_total_publishable"] is False


def test_all_time_and_business_windows_are_audited(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    _seed(connection)
    from scripts import audit_sql_doctrine_inventory as cli

    sources = cli.fetch_runtime_sources(NOW)
    runtime = cli.build_runtime_comparison(
        legacy_inputs=sources["legacy_inputs"], funnel_fetch=sources["funnel_fetch"],
        windows=sources["windows"], resolver_factory=_injected_resolver)
    keys = {(w["window_type"], w["window"]) for w in runtime["windows"]}
    assert keys == {("evidence", k) for k in ("7d", "14d", "30d", "60d", "180d", "all_time")} | {
        ("business", k) for k in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time")}
    all_time = _by_window(runtime["windows"], "evidence", "all_time")
    assert all_time["legacy_counts"]["all_source"] == 6
    assert all_time["lifecycle_counts"]["all_source"] == 34   # 33 + hs-5 (July)
    assert all_time["date_shifted_count"] == 0                # nothing shifts in all-time
    assert all_time["legacy_only_count"] == 1                 # hs-6 only
    # Business windows carry no keyword attribution and say why.
    q = _by_window(runtime["windows"], "business", "current_quarter")
    assert q["legacy_counts"]["keyword_attributable"] is None
    assert "evidence-window only" in q["legacy_counts"]["keyword_attributable_note"]


def test_end_to_end_run_is_read_only_and_exits_zero(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    _seed(connection)
    before = _counts(connection)
    from scripts import audit_sql_doctrine_inventory as cli

    report = cli.run_audit(now=NOW)
    assert report["runtime_comparison_available"] is True
    assert report["audit_complete"] is True
    assert report["migration_complete"] is False
    assert report["verdict"] == "READY_FOR_ROADMAP"
    assert report["exit_code"] == 0
    assert report["external_writes_performed"] is False
    assert report["database_writes_performed"] is False
    assert report["write_safety"]["ok"] is True
    assert report["write_safety"]["runtime_guard"]["installed"] is True

    # No campaign spend is seeded. The production identity contract is
    # CONSULTED and empty, so — by the PR-ADS-152 rule — no label resolves to a
    # Google Ads campaign identity: campaign-attributable is a real 0 on both
    # sides and every Google Ads-source SQL is an unmatched campaign identity.
    w = _by_window(report, "evidence", "30d")
    assert w["legacy_counts"]["all_source"] == 6
    assert w["lifecycle_counts"]["all_source"] == 33
    assert w["legacy_counts"]["campaign_attributable"] == 0
    assert w["lifecycle_counts"]["campaign_attributable"] == 0
    assert w["legacy_campaign_identity"]["unmatched_campaign_identities"] == 6
    assert w["lifecycle_campaign_identity"]["unmatched_campaign_identities"] == 8
    assert "google_ads_sqls_not_campaign_attributable" in w["legacy_reconciliation"]["reasons"]
    assert w["legacy_cpql_denominator_complete"] is False

    # Row counts are unchanged and the guard refuses a write at the database.
    assert _counts(connection) == before
    assert isinstance(connection._pool, cli.ReadOnlyPool)
    import psycopg2
    with pytest.raises(psycopg2.Error, match="read-only"):
        with connection.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO lead_truth_exclusions (lead_id, reason) "
                            "VALUES ('x', 'test')")
    assert _counts(connection) == before


def test_no_email_or_phone_in_runtime_output(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    _seed(connection)
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE leads SET company = 'someone@example.com +44 20 7946 0958'")
    from scripts import audit_sql_doctrine_inventory as cli
    from analysis import sql_doctrine_audit as audit
    import json
    import re

    report = cli.run_audit(now=NOW)
    text = json.dumps(report, default=str) + audit.render_human(report)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    assert "7946" not in text
