"""PR-ADS-180C — the Pipeline funnel reads the undated-reach bound, on
PostgreSQL, through the real repository reads and the real builders (docs/65).

The contacts are PR-ADS-180B's outside-the-rows fixture (window = last
quarter): ten Leads dated in it; four reached Lead undated with their MQL
dated after it; three at stage lead with no date at all; one at stage lead,
undated, created after the window. Every figure is read by the production
SQL (``fetch_funnel_contacts``, ``fetch_undated_stage_reach``) and built by
the production builders (``_lifecycle_activity_block`` for Overview,
``build_pipeline`` for Pipeline). The UI cases render the real payloads with
the real ``static/app.js`` in ``node``.

On ``7cd5e22`` the Overview read "Leads at least 10 … 7 contacts …" and the
Pipeline, over the same population, "Entered Lead 10" — exact (case 01).

PR-ADS-182A (owner decision, docs/67): the Pipeline funnel is the
acquisition cohort (by create date) — one cohort read inside the mart, no
stage-entry count, no undated reach. Case 01 is ported (the same contacts
are counted EXACTLY there, Activity keeps its bound); 02 and 05 keep their
counts and edges without ``outside_window_rows`` (removed with its one
reader); 06 keeps Overview's two sites. Removed with the Pipeline
lead-filter narrowing they tested: 03_a, 03_b, 04_a (docs/67 §6).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest  # noqa: F401  (the live_db fixture skips without PostgreSQL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, live_db,
)
from tests.test_pr_ads_180b_acquisition_cohort_funnel_pg_integration import (  # noqa: E402
    _at, _outside_fixture, _quarter, _render,
)
from tests.test_pr_ads_180c_pipeline_undated_bound import _stage_html  # noqa: E402

WINDOW = "last_quarter"
_EVENTS = ("lead", "mql", "sql", "opportunity", "customer")


def _json(payload):
    return json.loads(json.dumps(payload, default=str))


def _pipeline(**kw):
    from services.pipeline_service import build_pipeline
    return _json(build_pipeline(WINDOW, **kw))


def _activity(now):
    from services import dashboard_overview_service as O
    funnel = O._lifecycle_funnel_block(WINDOW, now=now)
    return _json(O._lifecycle_activity_block(funnel, WINDOW, now))


def _write(rows):
    from db import writers
    writers.upsert_hubspot_contact_funnel(rows)


# ── 1. one population: Activity bounds it, the cohort counts it ─────────────
def test_01_activity_bounds_the_undated_contacts_and_the_pipeline_cohort_counts_them(
        live_db, monkeypatch):
    """The m3 probe on the real table. Overview → Lifecycle Activity (stage
    entries) is "at least 10", naming the seven undated contacts; the
    Pipeline funnel (PR-ADS-182A: the acquisition cohort, the real mart read)
    counts the same seventeen contacts created in the quarter EXACTLY — it
    needs no stage-entry date."""
    from tests.test_pr_ads_177a_one_sql_count_pg_integration import _ready
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)                       # the contact-funnel sync state: fresh
    _outside_fixture()
    now = datetime.now(timezone.utc)
    act = _activity(now)
    p = _pipeline()
    f = p["funnel"]
    assert f["publication"] == {"status": "published", "reason": None}
    assert f["window"]["start_date"] == act["window"]["start_date"]
    activity = _stage_html(_render("renderDashLifecycleActivity(%s)"
                                   % json.dumps({"lifecycle_activity": act})))
    assert activity["contacts"] == (
        "at least 10", "Lower bound: 7 contacts reached this stage with no entry date "
        "recorded in HubSpot and could belong to this window")
    pipeline = _stage_html(_render("pipelineFunnelHtml(%s, null)" % json.dumps(p)))
    assert (f["totals"]["contacts"], f["totals"]["reached_mql"]) == (17, 14)
    assert pipeline["contacts"] == ("17", None), pipeline
    assert pipeline["mql"] == ("14", None), pipeline
    assert "events" not in f and "undated_reach" not in f


# ── 2. the read: every undated contact that could belong to the window ──────
def test_02_the_undated_reach_counts_inside_and_outside_the_windows_rows(live_db):
    """The reach Overview Activity reads, over the whole table. (PR-ADS-182A:
    ``outside_window_rows`` — its split by the window's rows — is gone with
    its one reader, the Pipeline lead-filter narrowing.)"""
    from db import crm_funnel_repository as repo
    start, end = _outside_fixture()
    inside = _at(start + timedelta(days=6))
    # One SQL contact IN the window's rows (Lead / MQL dated in it, no SQL date).
    _write([{"contact_id": "in-sql", "created_at": _at(start), "last_modified_at": _at(start),
             "lifecycle_stage": "salesqualifiedlead", "hs_analytics_source": "ORGANIC_SEARCH",
             "date_entered_lead": inside, "date_entered_mql": inside}])
    rows = {r["contact_id"] for r in repo.fetch_funnel_contacts(start, end)["rows"]}
    assert "in-sql" in rows and len(rows) == 11
    read = repo.fetch_undated_stage_reach(start, end)
    # Lead: the seven outside; MQL: none undated; SQL: in-sql, in the rows.
    assert read["counts"] == {"lead": 7, "mql": 0, "sql": 1, "opportunity": 0, "customer": 0}
    assert "outside_window_rows" not in read
    every = repo.fetch_undated_stage_reach(None, None)
    assert every["counts"]["lead"] == 8                     # + "late", never ruled out


# ── 4. PR-ADS-180C-F1 (minor 1): one database snapshot for the two reads ────
def _sync_mid_read(monkeypatch, rows):
    """`fetch_funnel_contacts` as production runs it, then a contact-funnel
    sync that COMMITS before the builder's next read (the auditor's probe,
    scratchpad/probes180c/test_probe_torn.py). The sync runs on its own
    thread, so on its own connection — as a concurrent sync would."""
    import threading
    from db import crm_funnel_repository as repo
    real = repo.fetch_funnel_contacts
    fired = []

    def fetch_then_sync(s, e):
        out = real(s, e)
        if len(fired) == 1:
            fired.append(1)
            t = threading.Thread(target=_write, args=(rows,))
            t.start()
            t.join()
        return out
    monkeypatch.setattr(repo, "fetch_funnel_contacts", fetch_then_sync)
    fired.append(real)            # fired[0]: the production read, to restore
    return fired


def test_04_b_a_sync_between_the_activity_reads_never_prints_a_false_exact(live_db):
    """Overview → Lifecycle Activity had the same tear since 180B. Before the
    sync: two SQLs dated in the window and g-out undated outside the rows —
    "at least 2". After it, g-out has its SQL date in the window — "3". Torn
    (rows before, reach after): "SQLs 2", exact. On 98cd829 it printed that."""
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        start, _ = _quarter()
        created, inside = _at(start), _at(start + timedelta(days=5))
        paid = {"hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "brand - uk"}
        _write([{"contact_id": f"g-{i}", "created_at": created, "last_modified_at": created,
                 "lifecycle_stage": "salesqualifiedlead", **paid, "date_entered_lead": inside,
                 "date_entered_mql": inside, "date_entered_sql": inside} for i in range(2)]
               + [{"contact_id": "g-out", "created_at": created, "last_modified_at": created,
                   "lifecycle_stage": "salesqualifiedlead", **paid}])
        now = datetime.now(timezone.utc)
        sql = lambda act: _stage_html(_render(  # noqa: E731
            "renderDashLifecycleActivity(%s)" % json.dumps({"lifecycle_activity": act})))["sql"]
        assert sql(_activity(now))[0] == "at least 2"
        fired = _sync_mid_read(mp, [{
            "contact_id": "g-out", "created_at": created,
            "last_modified_at": _at(start + timedelta(days=40)),
            "lifecycle_stage": "salesqualifiedlead", **paid, "date_entered_lead": inside,
            "date_entered_mql": inside, "date_entered_sql": inside}])
        torn = _activity(now)
        assert fired[1:] == [1]
        assert sql(torn)[0] == "at least 2", sql(torn)
    finally:
        mp.undo()
    assert sql(_activity(now)) == ("3", None)          # the sync committed


def test_04_c_a_sync_between_the_previous_period_reads_leaves_no_baseline(live_db):
    """The previous period pairs the same two reads (its trend baseline). Its
    window holds two dated SQLs and g-out, undated, outside its rows: its SQL
    count is a lower bound — no baseline. A sync between the reads that dates
    g-out's SQL in that window would, torn, state it complete (rows before,
    reach after). With one snapshot, `count_is_bound.sql` stays True."""
    from datetime import date as _date
    import pytest as _pytest
    from services import dashboard_overview_service as O
    now = datetime.now(timezone.utc)
    window = O._lifecycle_previous_period(WINDOW, now)["window"]
    start = _date.fromisoformat(str(window["start_date"])[:10])
    created, inside = _at(start), _at(start + timedelta(days=5))
    paid = {"hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "brand - uk"}
    _write([{"contact_id": f"p-{i}", "created_at": created, "last_modified_at": created,
             "lifecycle_stage": "salesqualifiedlead", **paid, "date_entered_lead": inside,
             "date_entered_mql": inside, "date_entered_sql": inside} for i in range(2)]
           + [{"contact_id": "p-out", "created_at": created, "last_modified_at": created,
               "lifecycle_stage": "salesqualifiedlead", **paid}])
    assert O._lifecycle_previous_period(WINDOW, now)["count_is_bound"]["sql"] is True
    mp = _pytest.MonkeyPatch()
    try:
        fired = _sync_mid_read(mp, [{
            "contact_id": "p-out", "created_at": created,
            "last_modified_at": _at(start + timedelta(days=40)),
            "lifecycle_stage": "salesqualifiedlead", **paid, "date_entered_lead": inside,
            "date_entered_mql": inside, "date_entered_sql": inside}])
        torn = O._lifecycle_previous_period(WINDOW, now)
        assert fired[1:] == [1]
        assert torn["counts"]["sql"] == 2
        assert torn["count_is_bound"]["sql"] is True, torn["count_is_bound"]
    finally:
        mp.undo()
    after = O._lifecycle_previous_period(WINDOW, now)          # the sync committed
    assert (after["counts"]["sql"], after["count_is_bound"]["sql"]) == (3, False)


# ── 5. PR-ADS-180C-F1 (minor 2): the window's edges, one clause ─────────────
def test_05_the_rows_and_the_reach_share_every_window_edge(live_db):
    """Four undated SQL contacts, each with ONE stage date (MQL) at an edge of
    the window, at the database's own instants (read back, so the case holds
    in any session time zone): one minute before the start, AT the start,
    one minute before the end of the last day, AT the day after. The two
    inside are the window's rows; every one is in the reach (created before
    the window). (PR-ADS-182A: the `outside_window_rows` half of this case
    went with that field; the rows' edges and the reach's counts stay.)"""
    from db import crm_funnel_repository as repo
    start, end = _quarter()
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT %s::timestamptz, (%s::date + INTERVAL '1 day')::timestamptz",
                    (start, end))
        lo, hi = cur.fetchone()
    early = lo - timedelta(days=40)
    edges = {"e-before": lo - timedelta(minutes=1), "e-start": lo,
             "e-end": hi - timedelta(minutes=1), "e-after": hi}
    _write([{"contact_id": cid, "created_at": early, "last_modified_at": early,
             "lifecycle_stage": "salesqualifiedlead", "hs_analytics_source": "ORGANIC_SEARCH",
             "date_entered_mql": at} for cid, at in edges.items()])
    rows = {r["contact_id"] for r in repo.fetch_funnel_contacts(start, end)["rows"]}
    assert rows == {"e-start", "e-end"}
    read = repo.fetch_undated_stage_reach(start, end)
    # Every one reached Lead and SQL undated, was created before the window,
    # and may belong to it; the two outside the rows are exactly the edges.
    assert read["counts"] == {"lead": 4, "mql": 0, "sql": 4, "opportunity": 0, "customer": 0}
    assert "outside_window_rows" not in read
    # The same edges under an open start / end.
    assert {r["contact_id"] for r in repo.fetch_funnel_contacts(None, end)["rows"]} \
        == {"e-before", "e-start", "e-end"}
    assert {r["contact_id"] for r in repo.fetch_funnel_contacts(start, None)["rows"]} \
        == {"e-start", "e-end", "e-after"}


# ── 6. PR-ADS-180C-F2: a snapshot that cannot be taken fails closed, fast ───
def test_06_no_snapshot_means_no_funnel_at_every_site_within_the_web_wait(live_db):
    """A migration holds the schema lock (EXCLUSIVE, on its own connection),
    so no pinned snapshot can start. Each funnel-read site must report the
    funnel unreadable — never fall back to two unpinned reads — and must give
    up within the web wait (5 s), not certification's 60 s. (PR-ADS-182A: two
    sites, both Overview's; the Pipeline funnel takes no snapshot of its own.)"""
    import time
    from db.connection import SCHEMA_LOCK_KEY
    from services import dashboard_overview_service as O
    _outside_fixture()
    now = datetime.now(timezone.utc)
    # Positive control: with the lock free every site reads its funnel.
    assert O._lifecycle_funnel_block(WINDOW, now=now)["available"] is True
    assert O._lifecycle_previous_period(WINDOW, now)["available"] is True

    holder = live_db.connect()
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
        holder.commit()
        t1 = time.monotonic()
        block = O._lifecycle_funnel_block(WINDOW, now=now)
        t2 = time.monotonic()
        assert block["available"] is False and block["lead"] is None
        assert block["reconciliation"]["reasons"] == ["canonical_funnel_read_failed"]
        prev = O._lifecycle_previous_period(WINDOW, now)
        t3 = time.monotonic()
        assert (prev["available"], prev["counts"], prev["reason"]) == (
            False, {}, "Previous period unavailable.")
        # The web wait, not the 60 s certification wait, per site.
        for elapsed in (t2 - t1, t3 - t2):
            assert 3 <= elapsed < 20, elapsed
    finally:
        try:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,))
            holder.commit()
        finally:
            holder.close()
    assert O._lifecycle_funnel_block(WINDOW, now=now)["available"] is True
