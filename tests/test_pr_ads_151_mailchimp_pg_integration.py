"""
PR-ADS-151 — PostgreSQL-backed integration tests for the Mailchimp read-only
foundation.

Proves behaviour that mocked-DB unit tests cannot:
  §2/§3 durable report-refresh selection reads SENT campaigns from the durable
        mailchimp_campaigns table (rolling window + sent-only), independent of
        discovery, and never selects drafts.
  §5   the durable mailchimp_backfill lease (partial unique index) enforces a
        single active backfill and recovers a stale lease.
  §4   fetch_durable_outcome_populations reuses the durable leads /
        deal_source_attribution / lead_truth_exclusions contracts.
  upsert idempotency: repeated syncs never duplicate campaigns/metrics.

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
from datetime import date, timedelta

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
        self.tmp = tempfile.mkdtemp(prefix="pgmc151_")
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


# ── schema idempotence + mailchimp backfill lease index ──────────────────────
def test_schema_creates_mailchimp_tables_and_lease_index(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.schema as schema
    schema.init_db()  # second apply must be a no-op
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name IN ('mailchimp_campaigns','mailchimp_campaign_reports',
                    'mailchimp_campaign_links','mailchimp_audience_snapshots','mailchimp_sync_state')
            """)
            assert cur.fetchone()[0] == 5
            cur.execute("SELECT COUNT(*) FROM pg_indexes "
                        "WHERE indexname = 'uq_recovery_running_mailchimp_backfill'")
            assert cur.fetchone()[0] == 1


# ── §2/§3 durable sent-campaign refresh selection ────────────────────────────
def test_sent_refresh_selection_is_durable_and_sent_only(monkeypatch, pg):
    _use_cluster(monkeypatch, pg)
    from db import mailchimp_repository as repo
    today = date.today()
    rows = [
        {"campaign_id": "sent15", "status": "sent",
         "send_time": (today - timedelta(days=15)).isoformat() + "T00:00:00+00:00"},
        {"campaign_id": "sent40", "status": "sent",
         "send_time": (today - timedelta(days=40)).isoformat() + "T00:00:00+00:00"},
        {"campaign_id": "draft", "status": "save", "send_time": None},
    ]
    assert repo.upsert_campaigns(rows)["written"] == 3

    # Rolling 30-day window → only the 15-day-old sent campaign.
    assert repo.sent_campaign_ids_for_refresh(window_days=30) == ["sent15"]
    # No window → all sent (newest first), drafts excluded.
    assert repo.sent_campaign_ids_for_refresh(window_days=None) == ["sent15", "sent40"]
    # Audit sampling is sent-only too.
    assert set(repo.recent_sent_campaign_ids(limit=10)) == {"sent15", "sent40"}


# ── upsert idempotency: repeated syncs never duplicate ───────────────────────
def test_repeated_upserts_do_not_duplicate(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    from db import mailchimp_repository as repo
    camp = {"campaign_id": "c1", "status": "sent", "send_time": "2026-06-01T00:00:00+00:00",
            "emails_sent": 100}
    repo.upsert_campaigns([camp])
    repo.upsert_campaigns([{**camp, "emails_sent": 120}])  # metric changed
    rep = {"campaign_id": "c1", "emails_sent": 120, "unique_opens": 50}
    repo.upsert_campaign_reports([rep])
    repo.upsert_campaign_reports([{**rep, "unique_opens": 60}])
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), MAX(emails_sent) FROM mailchimp_campaigns")
            assert cur.fetchone() == (1, 120)
            cur.execute("SELECT COUNT(*), MAX(unique_opens) FROM mailchimp_campaign_reports")
            assert cur.fetchone() == (1, 60)


# ── §5 durable backfill lease: single active + stale takeover ────────────────
def test_mailchimp_backfill_lease_single_active_and_stale_takeover(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    import db.writers as w
    rng = dict(date_from=date.today(), date_to=date.today())
    a = w.acquire_recovery_lease("mailchimp_backfill", "tokA", 900, **rng)
    b = w.acquire_recovery_lease("mailchimp_backfill", "tokB", 900, **rng)
    assert a["claimed"] is True and a["reason"] == "created"
    assert b["claimed"] is False and b["reason"] == "active_lease"
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM revenue_recovery_jobs "
                        "WHERE job_type='mailchimp_backfill' AND status='running'")
            assert cur.fetchone()[0] == 1
            # Force the lease to have expired (simulate a crashed worker).
            cur.execute("UPDATE revenue_recovery_jobs "
                        "SET lease_expires_at = NOW() - interval '1 hour' "
                        "WHERE job_id = %s", (a["job"]["job_id"],))
    takeover = w.acquire_recovery_lease("mailchimp_backfill", "tokC", 900, **rng)
    assert takeover["claimed"] is True and takeover["reason"] == "resumed"
    assert takeover["job"]["job_id"] == a["job"]["job_id"]


# ── §4 durable outcome populations reuse Averroes truth ──────────────────────
def test_durable_outcome_populations(monkeypatch, pg):
    connection = _use_cluster(monkeypatch, pg)
    from db import mailchimp_repository as repo
    today = date.today()
    within = today - timedelta(days=10)
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO runs (run_type, started_at, status) "
                        "VALUES ('test', NOW(), 'success') RETURNING id")
            run_id = cur.fetchone()[0]

            def _lead(contact_id, status_category, created, run_date):
                cur.execute(
                    "INSERT INTO leads (run_id, run_date, contact_id, status_category, "
                    "source_type, contact_created_at) VALUES (%s,%s,%s,%s,'email',%s)",
                    (run_id, run_date, contact_id, status_category, created))

            # 100 qualified within window → counts.
            _lead("100", "qualified", within, today)
            # 101 junk → excluded by definition.
            _lead("101", "junk", within, today)
            # 102 qualified but present in lead_truth_exclusions → excluded.
            _lead("102", "qualified", within, today)
            cur.execute("INSERT INTO lead_truth_exclusions (lead_id, reason) "
                        "VALUES ('102','test')")
            # 103 has an older qualified snapshot AND a newer junk snapshot → dedup
            #     to LATEST (junk) → not counted.
            _lead("103", "qualified", within, today - timedelta(days=2))
            _lead("103", "junk", within, today)

            # Closed-won deals (source-agnostic durable ledger).
            def _deal(deal_id, contact_id, amount, close):
                cur.execute(
                    "INSERT INTO deal_source_attribution (deal_id, associated_contact_id, "
                    "acquisition_group, attribution_status, deal_close_date, deal_amount_usd) "
                    "VALUES (%s,%s,'email','attributed',%s,%s)",
                    (deal_id, contact_id, close, amount))
            _deal("d1", "200", 5000, within)   # customer + revenue
            _deal("d2", "201", 0, within)      # customer, no revenue

    pops = repo.fetch_durable_outcome_populations(today - timedelta(days=365), today)
    assert pops["db_unavailable"] is False
    assert pops["sql_contacts"] == {"100"}
    assert pops["customer_contacts"] == {"200", "201"}
    assert pops["closed_won_by_contact"] == {"200": 5000.0}
    assert pops["durable_sql_population"] == 1
    assert pops["durable_closed_won_population"] == 1
