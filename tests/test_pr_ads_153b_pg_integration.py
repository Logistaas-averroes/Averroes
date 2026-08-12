"""
tests/test_pr_ads_153b_pg_integration.py

PR-ADS-153B — PostgreSQL-backed integration tests for the canonical CRM funnel
spine. These prove behaviour that source-string assertions cannot:

  §32  the canonical contact store is created by the real DDL, and re-ingesting
       the same contact upserts in place instead of appending a second row;
  §35  LATEST-STATE ordering — a newer HubSpot read always wins, an older read
       can never resurrect a superseded lifecycle stage or status, and a
       reporting window never selects an old snapshot just because the newer
       state falls outside it;
  §7   the sync service checkpoints its watermark durably, so a killed run
       resumes from the last PROVEN page rather than restarting;
  §33  historical stage cohorts survive a later stage transition (a contact that
       becomes a customer is still the SQL it was);
  §8   coverage reporting distinguishes "no evidence" from "zero".

The tests spin up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user. If the server binaries or that user are unavailable the
whole module is skipped rather than failing.

Run with:
    python -m pytest tests/test_pr_ads_153b_pg_integration.py -v
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

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# ── Locate the PostgreSQL server binaries ────────────────────────────────────
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
    if shutil.which("sudo") is None:
        return False
    # `sudo` existing is not the same as `sudo` being usable non-interactively.
    # Probe it (-n = never prompt) so the module SKIPS on a password-protected
    # sudo instead of running and then failing inside the cluster fixture.
    probe = subprocess.run(["sudo", "-n", "-u", "postgres", "true"],
                           capture_output=True, text=True)
    return probe.returncode == 0


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
    """A throwaway PostgreSQL cluster (initdb + start), owned by ``postgres``."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pgst153b_")
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

    def connect(self):
        import psycopg2
        return psycopg2.connect(self.url)

    def stop(self):
        _run(["sudo", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
              "-D", self.data, "-w", "stop"])
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture()
def pg(monkeypatch):
    """A live cluster with the real schema applied and db.connection pointed at it."""
    cluster = _PgCluster().start()
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)

        import db.connection as connection
        # Reset any pool created by an earlier test module.
        if hasattr(connection, "_pool"):
            monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()

        from db.schema import init_db
        init_db()

        yield cluster
    finally:
        try:
            import db.connection as connection
            if getattr(connection, "_pool", None) is not None:
                connection._pool.closeall()
                connection._pool = None
        except Exception:  # noqa: BLE001
            pass
        cluster.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _contact(contact_id="1", *, lifecyclestage=None, mql_status=None,
             created="2026-01-05T00:00:00Z", modified="2026-07-01T00:00:00Z",
             entered_lead=None, entered_mql=None, entered_sql=None,
             entered_opportunity=None, entered_customer=None,
             source="PAID_SEARCH", campaign="Brand - US", company=None):
    return {"id": contact_id, "properties": {
        "lifecyclestage": lifecyclestage,
        "mql_status": mql_status,
        "createdate": created,
        "lastmodifieddate": modified,
        "hs_v2_date_entered_lead": entered_lead,
        "hs_v2_date_entered_marketingqualifiedlead": entered_mql,
        "hs_v2_date_entered_salesqualifiedlead": entered_sql,
        "hs_v2_date_entered_opportunity": entered_opportunity,
        "hs_v2_date_entered_customer": entered_customer,
        "hs_analytics_source": source,
        "hs_analytics_source_data_1": campaign,
        "hs_analytics_source_data_2": "tms",
        "company": company or f"Co {contact_id}",
    }}


def _write(contacts):
    from connectors.hubspot_pull import normalize_contact_funnel_row
    from db import writers as db_writers

    rows = [normalize_contact_funnel_row(c) for c in contacts]
    result = db_writers.upsert_hubspot_contact_funnel([r for r in rows if r])
    assert result["ok"] is True, result
    return result


def _pages(*contact_pages, complete=True):
    """Fake iterator that mirrors the real connector's completion sentinel."""
    def iterator(since, max_pages=None):
        for index, page in enumerate(contact_pages):
            yield page, {"watermark_ms": 0, "page_index": index, "complete": False}
        if complete:
            yield [], {"watermark_ms": 0, "complete": True}
    return iterator


def _scalar(pg, sql, params=None):
    with pg.connect() as conn, conn.cursor() as cur:
        # Pass params only when present: an empty tuple makes psycopg2 treat a
        # literal % (e.g. in an ILIKE pattern) as a placeholder.
        cur.execute(sql, params) if params else cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None


def _row_of(pg, contact_id):
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM hubspot_contact_funnel WHERE contact_id = %s",
            (contact_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None


# =============================================================================
# Schema
# =============================================================================
def test_canonical_tables_are_created_by_the_real_ddl(pg):
    for table in ("hubspot_contact_funnel", "hubspot_contact_funnel_sync_state"):
        assert _scalar(pg, "SELECT to_regclass(%s)", (table,)) == table


def test_contact_id_is_unique(pg):
    assert _scalar(pg, """
        SELECT COUNT(*) FROM pg_indexes
        WHERE tablename = 'hubspot_contact_funnel'
          AND indexdef ILIKE '%UNIQUE%contact_id%'
    """) >= 1


def test_no_email_column_exists(pg):
    columns = _scalar(pg, """
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'hubspot_contact_funnel' AND column_name ILIKE '%email%'
    """)
    assert columns == 0


def test_init_db_is_idempotent(pg):
    from db.schema import init_db
    init_db()
    init_db()
    assert _scalar(pg, "SELECT to_regclass('hubspot_contact_funnel')") == (
        "hubspot_contact_funnel")


# =============================================================================
# §32 — Ingestion + idempotency
# =============================================================================
def test_contact_is_persisted_with_all_stage_dates(pg):
    _write([_contact("1", lifecyclestage="customer",
                     entered_lead="2026-01-05T00:00:00Z",
                     entered_mql="2026-03-01T00:00:00Z",
                     entered_sql="2026-07-10T00:00:00Z",
                     entered_opportunity="2026-08-01T00:00:00Z",
                     entered_customer="2026-09-01T00:00:00Z")])
    row = _row_of(pg, "1")
    assert row["lifecycle_stage"] == "customer"
    assert row["date_entered_sql"].date() == date(2026, 7, 10)
    assert row["date_entered_customer"].date() == date(2026, 9, 1)
    assert row["latest_stage_entry_at"].date() == date(2026, 9, 1)


def test_repeated_upsert_is_idempotent(pg):
    contact = _contact("1", lifecyclestage="lead")
    _write([contact])
    _write([contact])
    _write([contact])
    assert _scalar(pg, "SELECT COUNT(*) FROM hubspot_contact_funnel") == 1


def test_changed_lifecycle_updates_the_same_row(pg):
    _write([_contact("1", lifecyclestage="lead",
                     modified="2026-07-01T00:00:00Z")])
    _write([_contact("1", lifecyclestage="salesqualifiedlead",
                     modified="2026-07-20T00:00:00Z",
                     entered_sql="2026-07-20T00:00:00Z")])

    assert _scalar(pg, "SELECT COUNT(*) FROM hubspot_contact_funnel") == 1
    row = _row_of(pg, "1")
    assert row["lifecycle_stage"] == "salesqualifiedlead"
    assert row["date_entered_sql"].date() == date(2026, 7, 20)


def test_all_source_contacts_are_ingested_together(pg):
    _write([
        _contact("1", source="PAID_SEARCH"),
        _contact("2", source="ORGANIC_SEARCH"),
        _contact("3", source="PAID_SOCIAL"),
        _contact("4", source="OFFLINE"),
    ])
    assert _scalar(pg, "SELECT COUNT(*) FROM hubspot_contact_funnel") == 4
    assert _scalar(pg, """
        SELECT COUNT(DISTINCT hs_analytics_source) FROM hubspot_contact_funnel
    """) == 4


def test_missing_stage_dates_are_stored_as_null_not_createdate(pg):
    _write([_contact("1", created="2026-01-05T00:00:00Z")])
    row = _row_of(pg, "1")
    assert row["created_at"] is not None
    for column in ("date_entered_lead", "date_entered_mql", "date_entered_sql",
                   "date_entered_opportunity", "date_entered_customer"):
        assert row[column] is None, column
    assert row["latest_stage_entry_at"] is None


def test_free_text_never_reaches_mql_status(pg):
    _write([_contact("1", mql_status=None, company="Acme")])
    row = _row_of(pg, "1")
    assert row["mql_status"] is None
    assert row["mql_status_category"] == "no_verdict"


def test_unmapped_status_is_persisted_verbatim_and_flagged(pg):
    _write([_contact("1", mql_status="CLOSED - Brand New Value")])
    row = _row_of(pg, "1")
    assert row["mql_status"] == "CLOSED - Brand New Value"
    assert row["mql_status_category"] == "unmapped"


# =============================================================================
# §35 — Latest-state ordering
# =============================================================================
def test_newer_read_wins_over_older(pg):
    _write([_contact("1", lifecyclestage="lead", mql_status="OPEN - Connecting",
                     modified="2026-07-01T00:00:00Z")])
    _write([_contact("1", lifecyclestage="customer",
                     mql_status="CLOSED - Deal Created",
                     modified="2026-09-01T00:00:00Z",
                     entered_customer="2026-09-01T00:00:00Z")])
    row = _row_of(pg, "1")
    assert row["lifecycle_stage"] == "customer"
    assert row["mql_status"] == "CLOSED - Deal Created"


def test_older_read_cannot_resurrect_a_superseded_stage(pg):
    """Ingest newest first, then replay an older page (the overlap window does
    exactly this). The stale read must not win."""
    newest = _contact("1", lifecyclestage="customer",
                      modified="2026-09-01T00:00:00Z",
                      entered_customer="2026-09-01T00:00:00Z")
    older = _contact("1", lifecyclestage="lead", modified="2026-07-01T00:00:00Z")

    _write([newest])
    _write([older])

    row = _row_of(pg, "1")
    assert row["lifecycle_stage"] == "customer"
    assert row["last_modified_at"].date() == date(2026, 9, 1)


def test_read_without_a_modification_timestamp_cannot_overwrite_known_state(pg):
    """An incoming row with NO `lastmodifieddate` must never beat a stored row
    that has one. An unknown modification time is not evidence of recency, and
    admitting it would blank out known-newer lifecycle state."""
    _write([_contact("1", lifecyclestage="customer",
                     modified="2026-09-01T00:00:00Z",
                     entered_customer="2026-09-01T00:00:00Z")])

    # Same contact, no modification timestamp at all, and a regressed stage.
    _write([_contact("1", lifecyclestage="lead", modified=None)])

    row = _row_of(pg, "1")
    assert row["lifecycle_stage"] == "customer"
    assert row["last_modified_at"].date() == date(2026, 9, 1)
    assert row["date_entered_customer"] is not None


def test_first_write_without_a_modification_timestamp_is_still_accepted(pg):
    """The guard protects KNOWN state; it must not block the initial insert or a
    refresh of a row that has no stored timestamp to protect."""
    _write([_contact("1", lifecyclestage="lead", modified=None)])
    assert _row_of(pg, "1")["lifecycle_stage"] == "lead"

    _write([_contact("1", lifecyclestage="salesqualifiedlead", modified=None)])
    assert _row_of(pg, "1")["lifecycle_stage"] == "salesqualifiedlead"


def test_identical_modification_timestamps_are_deterministic(pg):
    """Two reads with the SAME lastmodifieddate must converge on one row and a
    stable value — never flip between runs."""
    a = _contact("1", lifecyclestage="opportunity",
                 modified="2026-08-01T00:00:00Z")
    _write([a])
    first = _row_of(pg, "1")["lifecycle_stage"]
    _write([a])
    second = _row_of(pg, "1")["lifecycle_stage"]
    assert first == second == "opportunity"
    assert _scalar(pg, "SELECT COUNT(*) FROM hubspot_contact_funnel") == 1


def test_window_never_selects_an_old_state_because_new_state_is_out_of_window(pg):
    """A contact whose LATEST modification falls outside the reporting window is
    still read at its latest state — the window applies to EVENT dates, not to
    when the row was last touched."""
    _write([_contact("1", lifecyclestage="customer",
                     modified="2027-01-15T00:00:00Z",       # far outside Q3
                     entered_sql="2026-07-10T00:00:00Z")])

    from db import crm_funnel_repository as repo
    fetched = repo.fetch_funnel_contacts(date(2026, 7, 1), date(2026, 9, 30))
    assert fetched["available"] is True
    assert len(fetched["rows"]) == 1
    assert fetched["rows"][0]["lifecycle_stage"] == "customer"


# =============================================================================
# §33 — Historical cohorts survive later transitions (end-to-end)
# =============================================================================
def test_customer_today_still_counts_in_its_original_sql_quarter(pg):
    _write([_contact("1", lifecyclestage="customer",
                     entered_sql="2026-07-10T00:00:00Z",
                     entered_customer="2026-11-02T00:00:00Z")])

    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as funnel

    fetched = repo.fetch_funnel_contacts(date(2026, 7, 1), date(2026, 9, 30))
    populations = funnel.build_populations(
        fetched["rows"], date(2026, 7, 1), date(2026, 9, 30))
    counts = populations["counts"]
    assert counts["sql"][funnel.SCOPE_ALL_SOURCE] == 1
    assert counts["customer"][funnel.SCOPE_ALL_SOURCE] == 0  # customer was in Q4


def test_repository_returns_a_contact_for_any_in_window_stage_date(pg):
    _write([
        _contact("1", entered_lead="2026-07-02T00:00:00Z"),
        _contact("2", entered_customer="2026-08-02T00:00:00Z"),
        _contact("3", entered_sql="2026-02-02T00:00:00Z"),   # out of window
    ])
    from db import crm_funnel_repository as repo
    rows = repo.fetch_funnel_contacts(date(2026, 7, 1), date(2026, 9, 30))["rows"]
    assert {r["contact_id"] for r in rows} == {"1", "2"}


# =============================================================================
# §7 — Durable sync state
# =============================================================================
def test_sync_state_round_trips(pg):
    from db import writers as db_writers

    watermark = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert db_writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete",
        last_modified_watermark=watermark, contacts_seen=42) is True

    state = db_writers.get_contact_funnel_sync_state("contacts")
    assert state["bootstrap_status"] == "complete"
    assert state["contacts_seen"] == 42
    assert state["last_modified_watermark"] == watermark


def test_sync_state_upsert_keeps_one_row_per_scope(pg):
    from db import writers as db_writers

    db_writers.update_contact_funnel_sync_state("contacts", contacts_seen=1)
    db_writers.update_contact_funnel_sync_state("contacts", contacts_seen=2)
    assert _scalar(
        pg, "SELECT COUNT(*) FROM hubspot_contact_funnel_sync_state") == 1
    assert db_writers.get_contact_funnel_sync_state("contacts")["contacts_seen"] == 2


def test_killed_sync_resumes_from_the_last_checkpointed_page(pg):
    """Page 1 persists and checkpoints; page 2 explodes. A follow-up run must
    resume from page 1's watermark, not from the beginning."""
    from services import hubspot_contact_funnel_sync_service as sync

    def failing(since, max_pages=None):
        yield [_contact("1", modified="2026-07-01T00:00:00Z")], {"watermark_ms": 0}
        raise RuntimeError("HubSpot 500")

    first = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=failing,
        now=datetime(2026, 7, 5, tzinfo=timezone.utc))
    assert first["status"] == "failed"
    assert _scalar(pg, "SELECT COUNT(*) FROM hubspot_contact_funnel") == 1

    state = sync.db_writers.get_contact_funnel_sync_state("contacts")
    assert state["bootstrap_status"] == "partial"
    assert state["last_modified_watermark"] == datetime(
        2026, 7, 1, tzinfo=timezone.utc)

    seen_since = {}

    def resuming(since, max_pages=None):
        seen_since["value"] = since
        yield ([_contact("2", modified="2026-07-02T00:00:00Z")],
               {"watermark_ms": 0, "complete": False})
        yield [], {"watermark_ms": 0, "complete": True}

    second = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=resuming,
        now=datetime(2026, 7, 6, tzinfo=timezone.utc))
    assert second["status"] == "success"
    # Resumed at the checkpoint minus the deliberate overlap, not at the epoch.
    assert seen_since["value"].year == 2026
    assert _scalar(pg, "SELECT COUNT(*) FROM hubspot_contact_funnel") == 2


def test_capped_bootstrap_persists_its_watermark_and_stays_partial(pg):
    from services import hubspot_contact_funnel_sync_service as sync

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP,
        page_iterator=_pages([_contact("1", modified="2026-07-01T00:00:00Z")],
                             complete=False),
        max_pages=1, now=datetime(2026, 7, 5, tzinfo=timezone.utc))

    assert result["status"] == "success"
    assert result["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL
    state = sync.db_writers.get_contact_funnel_sync_state("contacts")
    assert state["last_modified_watermark"] == datetime(
        2026, 7, 1, tzinfo=timezone.utc)


def test_second_bootstrap_resumes_from_the_persisted_watermark(pg):
    """The headline fix: an interrupted bootstrap must not rescan from the epoch."""
    from services import hubspot_contact_funnel_sync_service as sync

    sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP,
        page_iterator=_pages([_contact("1", modified="2026-07-01T12:00:00Z")],
                             complete=False),
        max_pages=1, now=datetime(2026, 7, 5, tzinfo=timezone.utc))

    seen_since = {}

    def resuming(since, max_pages=None):
        seen_since["value"] = since
        yield ([_contact("2", modified="2026-07-02T00:00:00Z")],
               {"watermark_ms": 0, "complete": False})
        yield [], {"watermark_ms": 0, "complete": True}

    second = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=resuming,
        now=datetime(2026, 7, 6, tzinfo=timezone.utc))

    # Resumed at the persisted watermark minus the safe overlap — not the epoch.
    assert seen_since["value"] == datetime(2026, 7, 1, 11, 45, tzinfo=timezone.utc)
    assert seen_since["value"].year != 1970
    assert second["status"] == "success"


def test_killed_bootstrap_resumes_and_eventually_completes(pg):
    """Kill → resume → complete, without ever rescanning from the epoch."""
    from services import hubspot_contact_funnel_sync_service as sync

    def failing(since, max_pages=None):
        yield ([_contact("1", modified="2026-07-01T00:00:00Z")],
               {"watermark_ms": 0, "complete": False})
        raise RuntimeError("worker killed mid-bootstrap")

    first = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=failing,
        now=datetime(2026, 7, 5, tzinfo=timezone.utc))
    assert first["status"] == "failed"
    assert sync.db_writers.get_contact_funnel_sync_state(
        "contacts")["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL
    # An unfinished bootstrap keeps bootstrapping rather than going incremental.
    assert sync.get_bootstrap_mode() == sync.MODE_BOOTSTRAP

    seen_since = {}

    def finishing(since, max_pages=None):
        seen_since["value"] = since
        yield ([_contact("2", modified="2026-07-03T00:00:00Z")],
               {"watermark_ms": 0, "complete": False})
        yield [], {"watermark_ms": 0, "complete": True}

    second = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=finishing,
        now=datetime(2026, 7, 6, tzinfo=timezone.utc))

    assert seen_since["value"].year == 2026  # resumed, not restarted
    assert second["bootstrap_status"] == sync.BOOTSTRAP_COMPLETE
    assert _scalar(pg, "SELECT COUNT(*) FROM hubspot_contact_funnel") == 2
    # Only now does the scheduler switch to incremental.
    assert sync.get_bootstrap_mode() == sync.MODE_INCREMENTAL


def test_restart_from_epoch_is_an_explicit_operator_choice(pg):
    from services import hubspot_contact_funnel_sync_service as sync

    sync.db_writers.update_contact_funnel_sync_state(
        "contacts", last_modified_watermark=datetime(
            2026, 7, 1, tzinfo=timezone.utc))

    seen_since = {}

    def scan(since, max_pages=None):
        seen_since["value"] = since
        yield [], {"watermark_ms": 0, "complete": True}

    sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=scan, restart_from_epoch=True,
        now=datetime(2026, 7, 6, tzinfo=timezone.utc))
    assert seen_since["value"].year == 1970


def test_scan_without_completion_proof_never_marks_bootstrap_complete(pg):
    from services import hubspot_contact_funnel_sync_service as sync

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP,
        page_iterator=_pages([_contact("1")], complete=False),
        now=datetime(2026, 7, 5, tzinfo=timezone.utc))

    assert result["scan_complete"] is False
    assert result["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL
    assert sync.db_writers.get_contact_funnel_sync_state(
        "contacts")["bootstrap_completed_at"] is None


def test_persistence_failure_leaves_the_watermark_untouched(pg, monkeypatch):
    """A DB write that cannot be proven must not advance progress."""
    from db import writers as db_writers
    from services import hubspot_contact_funnel_sync_service as sync

    monkeypatch.setattr(
        db_writers, "upsert_hubspot_contact_funnel",
        lambda rows, **kw: {"ok": False, "attempted": len(rows),
                            "persisted": 0, "error": "disk on fire"})

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP,
        page_iterator=_pages([_contact("1", modified="2026-07-01T00:00:00Z")]),
        now=datetime(2026, 7, 5, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    state = sync.db_writers.get_contact_funnel_sync_state("contacts")
    assert state["last_modified_watermark"] is None
    assert state["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL


def test_stale_row_no_op_is_success_not_failure(pg):
    """`persisted < attempted` is the latest-state guard working as designed."""
    from db import writers as db_writers
    from connectors.hubspot_pull import normalize_contact_funnel_row

    _write([_contact("1", lifecyclestage="customer",
                     modified="2026-09-01T00:00:00Z")])
    stale = normalize_contact_funnel_row(
        _contact("1", lifecyclestage="lead", modified="2026-07-01T00:00:00Z"))
    result = db_writers.upsert_hubspot_contact_funnel([stale])

    assert result["ok"] is True          # not a failure
    assert result["attempted"] == 1
    assert result["persisted"] == 0      # guard rejected the stale row
    assert _row_of(pg, "1")["lifecycle_stage"] == "customer"


def test_sync_records_a_freshness_batch_under_the_registered_keys(pg):
    from services import hubspot_contact_funnel_sync_service as sync

    def pages(since, max_pages=None):
        yield ([_contact("1", entered_sql="2026-07-01T00:00:00Z")],
               {"watermark_ms": 0, "complete": False})
        yield [], {"watermark_ms": 0, "complete": True}

    sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=pages,
        now=datetime(2026, 7, 5, tzinfo=timezone.utc))

    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT source, dataset, status FROM sync_batches
            WHERE source = 'hubspot' AND dataset IN ('contact_funnel', 'lifecycle_events')
            ORDER BY dataset
        """)
        rows = cur.fetchall()
    assert {(r[0], r[1]) for r in rows} == {
        ("hubspot", "contact_funnel"), ("hubspot", "lifecycle_events")}
    assert all(r[2] == "success" for r in rows)


# =============================================================================
# §8 — Coverage reporting
# =============================================================================
def test_coverage_reports_stage_evidence_gaps(pg):
    _write([
        _contact("1", lifecyclestage="customer",
                 entered_sql="2026-07-10T00:00:00Z",
                 entered_customer="2026-09-01T00:00:00Z"),
        _contact("2", lifecyclestage="lead"),          # no stage evidence at all
    ])
    from services import hubspot_contact_funnel_sync_service as sync

    coverage = sync.build_coverage()
    assert coverage["available"] is True
    assert coverage["totals"]["contacts"] == 2
    assert coverage["stage_entry_coverage"]["sql"] == 1
    assert coverage["stage_entry_coverage"]["lead"] == 0


def test_coverage_reports_bootstrap_state_separately_from_row_recency(pg):
    """Recent rows must not be mistaken for a completed historical bootstrap."""
    _write([_contact("1", modified="2026-07-01T00:00:00Z")])
    from services import hubspot_contact_funnel_sync_service as sync

    coverage = sync.build_coverage()
    assert coverage["totals"]["contacts"] == 1
    assert coverage["bootstrap_status"] == "not_started"


def test_unavailable_database_is_not_reported_as_zero(pg, monkeypatch):
    from db import crm_funnel_repository as repo

    monkeypatch.setattr(repo, "get_conn", lambda: _NullConn())
    result = repo.fetch_funnel_contacts(date(2026, 7, 1), date(2026, 9, 30))
    assert result["available"] is False
    assert result["rows"] == []


class _NullConn:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# =============================================================================
# End-to-end reconciliation against the legacy table
# =============================================================================
def test_legacy_vs_lifecycle_reconciliation_end_to_end(pg):
    from db import writers as db_writers

    run_id = db_writers.write_run({
        "run_type": "test", "started_at": "2026-07-01T00:00:00Z", "status": "running"})

    # Legacy doctrine: qualified, dated by contact creation.
    db_writers.write_leads(run_id, [
        {"id": "1", "properties": {
            "mql_status": "CLOSED - Sales Qualified",
            "createdate": "2026-01-05T00:00:00Z",
            "hs_analytics_source": "PAID_SEARCH",
            "hs_analytics_source_data_1": "Brand - US",
            "company": "Co 1"}},
    ])

    # Canonical doctrine: entered SQL in Q3.
    _write([_contact("1", lifecyclestage="salesqualifiedlead",
                     created="2026-01-05T00:00:00Z",
                     entered_sql="2026-07-10T00:00:00Z")])

    from services import crm_funnel_reconciliation_service as recon

    payload = recon.run(business_window="current_quarter",
                        now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert payload["available"] is True

    comparison = payload["sql_comparison"]
    # Legacy counts it in Q1 (creation), canonical counts it in Q3 (qualification).
    assert comparison["legacy_sql_count"] == 0
    assert comparison["lifecycle_sql_count"] == 1
    assert comparison["date_shifted_contacts"] == 1
    assert comparison["sets"]["date_shifted"] == ["1"]


def test_reconciliation_reports_unavailable_when_sources_are_missing(pg, monkeypatch):
    from db import crm_funnel_repository as repo
    from services import crm_funnel_reconciliation_service as recon

    monkeypatch.setattr(repo, "fetch_all_funnel_contacts",
                        lambda: {"available": False, "rows": []})
    payload = recon.run(business_window="current_quarter")
    assert payload["available"] is False
    assert "unavailable" in payload["reason"]
