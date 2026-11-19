"""PR-ADS-163B — one unreadable row count must not blank the rest.

`/api/datasets/freshness` reads one row count per registered dataset and then
the latest sync batch of every dataset, all in ONE transaction. Two registry
entries named columns their tables do not have (`deals.close_date`,
`gclid_attribution.matched_at`). PostgreSQL aborts a transaction on its first
error, so in production the `deals` read failed and every read after it failed
with "current transaction is aborted": six datasets reported "row count
unavailable" and NO dataset had its latest batch read, on every request.

The earlier endpoint tests could not see this: their cursor is a mock whose
failing table raises and whose next statement succeeds, which a real
transaction does not do. These tests run the real endpoint on a real database
with the real schema.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest

from services import freshness_service as fs

USER = {"username": "tester", "role": "admin"}


@pytest.fixture()
def live_db(monkeypatch):
    from tests import test_pr_ads_153b_pg_integration as pgmod
    if not pgmod._have_postgres():
        pytest.skip("PostgreSQL not available")
    # A test that swaps sys.modules can evict `psycopg2.sql`; importing it
    # here re-registers it, so a composed query below is one the driver knows.
    import psycopg2.sql  # noqa: F401
    cluster = pgmod._PgCluster().start()
    import db.connection as connection
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)
        monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()
        from db.schema import init_db
        init_db()
        yield cluster
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None
        cluster.stop()


def _freshness(days=60):
    import api.server as server
    return server.api_datasets_freshness(user=USER, days=days)


def _run_id(cur):
    cur.execute("INSERT INTO runs (run_type, started_at, status) VALUES "
                "('weekly', now(), 'success') RETURNING id")
    return cur.fetchone()[0]


def _by_key(payload):
    return {d["dataset_key"]: d for d in payload["datasets"]}


def _counted_keys():
    """`source/dataset` of every registered dataset that has a row count."""
    return {f"{c['source']}/{c['dataset']}"
            for c in fs.DATASET_FRESHNESS_CONFIG.values()
            if c.get("table") and c.get("date_column")}


def test_01_every_registered_date_column_exists_in_the_real_schema(live_db):
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'")
        columns = set(cur.fetchall())
    missing = sorted(
        (key, cfg["table"], cfg["date_column"])
        for key, cfg in fs.DATASET_FRESHNESS_CONFIG.items()
        if cfg.get("table") and cfg.get("date_column")
        and (cfg["table"], cfg["date_column"]) not in columns)
    assert missing == []


def test_02_on_the_real_schema_no_row_count_read_fails(live_db, caplog):
    caplog.set_level(logging.WARNING)
    payload = _freshness()
    assert payload.get("db_unavailable") is not True
    failed = [r.getMessage() for r in caplog.records
              if "query failed" in r.getMessage()]
    assert failed == []
    rows = _by_key(payload)
    assert _counted_keys() <= set(rows)
    # An empty database: every count was READ, and is zero — not unknown.
    assert {k: rows[k]["rows_in_window"] for k in _counted_keys()} == {
        k: 0 for k in _counted_keys()}


def test_03_one_unreadable_dataset_costs_only_itself(live_db, monkeypatch, caplog):
    # A registry entry naming a column its table lacks, registered FIRST so
    # that every real dataset is read after it — the production order of harm.
    broken = {"broken": {"table": "leads", "date_column": "no_such_column",
                         "source": "test", "dataset": "broken",
                         "stale_threshold_days": 8, "depends_on": [],
                         "page": None}}
    real_keys = _counted_keys()
    monkeypatch.setattr(fs, "DATASET_FRESHNESS_CONFIG",
                        {**broken, **fs.DATASET_FRESHNESS_CONFIG})
    today = date.today()
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_batches (source, dataset, sync_type, status, "
            "row_count, started_at) VALUES "
            "('gclid', 'matches', 'daily', 'success', 3, now()), "
            "('test', 'broken', 'daily', 'success', 7, now())")
        cur.execute(
            "INSERT INTO sync_state (source, dataset, status, "
            "last_successful_sync_at, last_source_date) VALUES "
            "('gclid', 'matches', 'success', now(), %s), "
            "('test', 'broken', 'success', now(), %s)", (today, today))
        conn.commit()
    caplog.set_level(logging.WARNING)
    rows = _by_key(_freshness())

    # The broken dataset says its count is unknown — and nothing else does.
    assert rows["test/broken"]["canonical_status"] == "unknown_row_count"
    failed = [r.getMessage() for r in caplog.records
              if "query failed" in r.getMessage()]
    assert len(failed) == 1 and "dataset=broken" in failed[0]
    assert "no_such_column" in failed[0]
    assert rows["test/broken"]["rows_in_window"] is None
    unread = sorted(k for k in real_keys if rows[k]["rows_in_window"] is None)
    assert unread == []
    # The latest-batch read, which runs after every row count, still ran.
    assert rows["gclid/matches"]["last_batch_row_count"] == 3
    assert rows["test/broken"]["last_batch_row_count"] == 7


def test_04_the_two_corrected_datasets_count_the_rows_written_in_the_window(live_db):
    today = date.today()
    with live_db.connect() as conn, conn.cursor() as cur:
        # Legacy deal snapshots: one inside the 60-day window, one outside.
        run_id = _run_id(cur)
        cur.execute(
            "INSERT INTO deals (run_id, run_date, contact_id, gclid) VALUES "
            "(%s, %s, 'c-in', 'g1'), (%s, %s, 'c-out', 'g2')",
            (run_id, today - timedelta(days=5),
             run_id, today - timedelta(days=200)))
        # Match rows: two first recorded now; one recorded 200 days ago; and
        # one recorded 200 days ago that a run RE-CONFIRMED today. The window
        # is on first recording — the date the GCLID page windows on — so a
        # touch does not make an old match new.
        cur.execute(
            "INSERT INTO gclid_attribution (attribution_key, gclid) VALUES "
            "('k-new', 'g1'), ('k-new-2', 'g3')")
        cur.execute(
            "INSERT INTO gclid_attribution (attribution_key, gclid, created_at, "
            "updated_at) VALUES "
            "('k-old', 'g2', now() - interval '200 days', now() - interval '200 days'), "
            "('k-touched', 'g4', now() - interval '200 days', now())")
        cur.execute(
            "INSERT INTO sync_state (source, dataset, status, "
            "last_successful_sync_at, last_source_date) VALUES "
            "('hubspot', 'deals', 'success', now(), %s), "
            "('gclid', 'matches', 'success', now(), %s)", (today, today))
        conn.commit()
    rows = _by_key(_freshness(days=60))
    assert rows["hubspot/deals"]["rows_in_window"] == 1
    assert rows["gclid/matches"]["rows_in_window"] == 2
    assert rows["hubspot/deals"]["canonical_status"] == "fresh_with_data"
    assert rows["gclid/matches"]["canonical_status"] == "fresh_with_data"


def test_05_a_successful_sync_that_wrote_nothing_is_reported_as_empty(live_db):
    # Production today: the legacy deal pull has reported success with zero
    # rows since 2026-05-26. That must read as an empty success, never as
    # data, and never as "row count unavailable".
    today = date.today()
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (run_id, run_date, contact_id, gclid) VALUES "
            "(%s, %s, 'c-old', 'g1')", (_run_id(cur), today - timedelta(days=177)))
        cur.execute(
            "INSERT INTO sync_batches (source, dataset, sync_type, status, "
            "row_count, started_at) VALUES "
            "('hubspot', 'deals', 'daily', 'success', 0, now())")
        cur.execute(
            "INSERT INTO sync_state (source, dataset, status, "
            "last_successful_sync_at, last_source_date) VALUES "
            "('hubspot', 'deals', 'success', now(), %s)", (today,))
        conn.commit()
    row = _by_key(_freshness(days=60))["hubspot/deals"]
    assert row["rows_in_window"] == 0
    assert row["last_batch_row_count"] == 0
    assert row["canonical_status"] == "empty_success"


# ── the war room reads the same registry in the same way ──────────────────

def _war_room(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    monkeypatch.setattr(server, "_war_room_cache", {})
    response = TestClient(server.app).get(
        "/api/system/status-war-room?days=60",
        headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200
    return response.json()


def test_06_the_war_room_shows_the_incremental_sync_by_its_persisted_run_type(
        live_db, monkeypatch):
    # The four run types the `runs` table holds — three live jobs and the
    # weekly job PR-ADS-165A retired — written with the statuses the
    # `runs` table held on 2026-11-18 — the incremental sync as the scheduler
    # names it, not as the tile is labelled.
    from scheduler.incremental_sync import RUN_TYPE
    assert RUN_TYPE == "daily_incremental_sync"
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_type, started_at, finished_at, status) VALUES "
            "('daily', now() - interval '7 hours', now() - interval '7 hours', 'success'), "
            "('weekly', now() - interval '2 days', NULL, 'failed'), "
            "('monthly', now() - interval '17 days', now() - interval '17 days', 'success'), "
            "(%s, now() - interval '28 hours', now() - interval '28 hours', 'success'), "
            "(%s, now() - interval '4 hours', now() - interval '4 hours', 'partial')",
            (RUN_TYPE, RUN_TYPE))
        conn.commit()
    scheduler = _war_room(monkeypatch)["scheduler"]
    assert scheduler["latest_daily"]["status"] == "success"
    # PR-ADS-165A: the retired weekly job's last row (`failed`) is in the
    # table above and is NOT a tile — it is history, not a health signal.
    assert "latest_weekly" not in scheduler
    assert scheduler["latest_monthly"]["status"] == "success"
    # The newest incremental run, with the status it recorded — `partial` is
    # not folded into either neighbour.
    assert scheduler["latest_incremental"]["status"] == "partial"


def test_07_in_the_war_room_one_unreadable_dataset_costs_only_itself(
        live_db, monkeypatch, caplog):
    from scheduler.incremental_sync import RUN_TYPE
    real = dict(fs.DATASET_FRESHNESS_CONFIG)
    broken = {"broken": {"table": "leads", "date_column": "no_such_column",
                         "source": "test", "dataset": "broken",
                         "stale_threshold_days": 8, "depends_on": [],
                         "page": None}}
    monkeypatch.setattr(fs, "DATASET_FRESHNESS_CONFIG", {**broken, **real})
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_type, started_at, status) VALUES "
            "(%s, now(), 'success')", (RUN_TYPE,))
        cur.execute(
            "INSERT INTO gclid_attribution (attribution_key, gclid) VALUES "
            "('k1', 'g1')")
        cur.execute(
            "INSERT INTO sync_batches (source, dataset, sync_type, status, "
            "row_count, started_at) VALUES "
            "('gclid', 'matches', 'daily', 'success', 1, now())")
        cur.execute(
            "INSERT INTO sync_state (source, dataset, status, "
            "last_successful_sync_at, last_source_date) VALUES "
            "('gclid', 'matches', 'success', now(), %s)", (date.today(),))
        conn.commit()
    caplog.set_level(logging.WARNING)
    body = _war_room(monkeypatch)
    failed = [r.getMessage() for r in caplog.records
              if "query failed" in r.getMessage()]
    assert len(failed) == 1 and "dataset=broken" in failed[0]
    pipelines = {p["key"]: p for p in body["pipelines"]}
    # Every registered dataset the war room reports had its count read.
    reported = set(real) & set(pipelines)
    assert {"leads", "deals", "gclid_attribution", "canonical_spend",
            "search_terms", "mailchimp_campaigns"} <= reported
    unread = sorted(k for k in reported if pipelines[k]["rows_in_window"] is None)
    assert unread == []
    assert pipelines["gclid_attribution"]["rows_in_window"] == 1
    assert pipelines["gclid_attribution"]["canonical_status"] == "fresh_with_data"
    # The run history, read last on the same transaction, is still there.
    assert body["scheduler"]["latest_incremental"]["status"] == "success"


# ── the two other readers of the same registry ────────────────────────────

def test_08_the_window_diagnostics_count_every_dataset_after_an_unreadable_one(
        live_db, monkeypatch):
    from services import system_status_service as sss
    real = dict(fs.DATASET_FRESHNESS_CONFIG)
    broken = {"broken": {"table": "leads", "date_column": "no_such_column",
                         "source": "test", "dataset": "broken",
                         "stale_threshold_days": 8, "depends_on": [],
                         "page": None}}
    monkeypatch.setattr(sss, "DATASET_FRESHNESS_CONFIG", {**broken, **real})
    with live_db.connect() as conn, conn.cursor() as cur:
        diag = sss.gather_dataset_window_counts(cur, windows=["7d", "60d"])
    assert diag["broken"]["window_counts"] == {"7d": None, "60d": None}
    assert diag["broken"]["missing_date_rows"] is None
    for key in real:
        assert diag[key]["window_counts"] == {"7d": 0, "60d": 0}, key
        assert diag[key]["missing_date_rows"] == 0, key
        assert "row_count_unavailable_reason" not in diag[key], key


def test_09_the_mailchimp_status_counts_each_dataset_on_its_own(
        live_db, monkeypatch):
    from services import mailchimp_status_service as mss
    config = {k: dict(v) for k, v in fs.DATASET_FRESHNESS_CONFIG.items()}
    config["mailchimp_campaigns"]["date_column"] = "no_such_column"
    monkeypatch.setattr(fs, "DATASET_FRESHNESS_CONFIG", config)
    out = mss._dataset_freshness_map()
    assert out.get("__db_unavailable__") is not True
    assert out["campaigns"]["rows_in_window"] is None
    assert out["reports"]["rows_in_window"] == 0
    assert out["audiences"]["rows_in_window"] == 0


def test_10_the_legacy_deal_snapshot_blocks_no_page(live_db, monkeypatch):
    # Production today, written with the real batch writers: the legacy
    # `deals` snapshot's newest row is months old and its sync keeps
    # succeeding with zero rows. The Deals page reads the canonical deal
    # ledger, not this table, so this state must not report that page blocked
    # — and must not be a critical blocker.
    from db import writers
    today = date.today()
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (run_id, run_date, contact_id, gclid) VALUES "
            "(%s, %s, 'c-old', 'g1')", (_run_id(cur), today - timedelta(days=177)))
        conn.commit()
    batch_id = writers.start_sync_batch(
        source="hubspot", dataset="deals", sync_type="daily",
        date_from=today - timedelta(days=7), date_to=today, run_id=None)
    assert batch_id
    writers.finish_sync_batch(batch_id=batch_id, status="success", row_count=0,
                              last_source_date=today)
    body = _war_room(monkeypatch)
    legacy = {p["key"]: p for p in body["pipelines"]}["deals"]
    assert legacy["rows_in_window"] == 0
    assert legacy["canonical_status"] == "empty_success"
    deals_impacts = [i for i in body["page_impact"]
                     if str(i.get("page", "")).lower() == "deals"
                     or i.get("blocked_by") == "deals"]
    assert deals_impacts == []
    assert not [b for b in body["critical_blockers"]
                if str(b.get("id", "")).startswith("deals")]


def test_11_a_dataset_with_no_batch_reports_an_unknown_batch_count(
        live_db, monkeypatch):
    pipelines = {p["key"]: p for p in _war_room(monkeypatch)["pipelines"]}
    assert pipelines["leads"]["last_batch_row_count"] is None
