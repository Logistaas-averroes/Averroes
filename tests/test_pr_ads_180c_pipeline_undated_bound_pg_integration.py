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


# ── 1. one population, one reading, on both pages ───────────────────────────
def test_01_overview_activity_and_pipeline_read_the_same_population_identically(
        live_db, monkeypatch):
    """The m3 probe on the real table. Every stage's value and bound reason
    is the same on both pages; Lead is "at least 10", naming the seven."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _outside_fixture()
    now = datetime.now(timezone.utc)
    act = _activity(now)
    p = _pipeline()
    f = p["funnel"]
    assert f["available"] is True and f["window"]["start_date"] == act["window"]["start_date"]
    # The figure first: what each page prints.
    activity = _stage_html(_render("renderDashLifecycleActivity(%s)"
                                   % json.dumps({"lifecycle_activity": act})))
    pipeline = _stage_html(_render("pipelineFunnelHtml(%s, null)" % json.dumps(p)))
    assert activity == pipeline, (activity, pipeline)
    by = {s["event"]: s for s in act["stages"]}
    for e in _EVENTS:
        assert f["events"][e]["count"] == by[e]["entered"], e
        assert f["events"][e]["undated_contacts_could_belong"] \
            == by[e]["undated_contacts_could_belong"], e
        assert f["events"][e]["count_is_bound"] == by[e]["count_is_bound"], e
    assert f["events"]["lead"]["undated_contacts_could_belong"] == 7
    assert pipeline["contacts"] == (
        "at least 10", "Lower bound: 7 contacts reached this stage with no entry date "
        "recorded in HubSpot and could belong to this window")
    # MQL: the four MQLs are dated (after the window), nobody undated: exact.
    assert pipeline["mql"] == ("10", None)


# ── 2. the read: what lies outside the window's own rows ────────────────────
def test_02_the_undated_reach_says_what_lies_outside_the_windows_rows(live_db):
    """``outside_window_rows`` is the part of each figure with no stage date
    in the window — ``fetch_funnel_contacts``' own window clause."""
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
    assert read["outside_window_rows"] == {"lead": 7, "mql": 0, "sql": 0,
                                           "opportunity": 0, "customer": 0}
    # All Time: every dated contact is in the rows; outside are the undated-only.
    every = repo.fetch_undated_stage_reach(None, None)
    assert every["counts"]["lead"] == 8                     # + "late", never ruled out
    assert every["outside_window_rows"]["lead"] == 4        # out-lead ×3 + late
    assert every["outside_window_rows"]["sql"] == 0


# ── 3. a lead filter: the all-source figure is never the subset's ───────────
def _scope_fixture():
    """Two Google Ads SQLs dated in the window, and one ORGANIC contact at SQL
    in the window's rows (Lead / MQL dated in it) with no SQL date: an SQL gap
    for All Sources only. Nothing undated lies outside the rows."""
    start, _end = _quarter()
    created, inside = _at(start), _at(start + timedelta(days=5))
    paid = {"hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "brand - uk"}
    _write([{"contact_id": f"g-{i}", "created_at": created, "last_modified_at": created,
             "lifecycle_stage": "salesqualifiedlead", **paid,
             "date_entered_lead": inside, "date_entered_mql": inside,
             "date_entered_sql": inside} for i in range(2)]
           + [{"contact_id": "o-gap", "created_at": created, "last_modified_at": created,
               "lifecycle_stage": "salesqualifiedlead", "hs_analytics_source": "ORGANIC_SEARCH",
               "date_entered_lead": inside, "date_entered_mql": inside}])
    return created, paid


def _sql_cell(payload):
    return _stage_html(_render("pipelineFunnelHtml(%s, null)" % json.dumps(payload)))["sql"]


def test_03_a_a_scope_is_exact_where_nothing_lies_outside_the_rows(live_db, monkeypatch):
    """Positive control (178C's narrowing, now through the real read): the
    organic gap in the rows bounds All Sources, not Google Ads-source, because
    the read proves nothing undated lies outside the window's rows. On
    7cd5e22 the figures are the same; the payload carries no such proof."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _scope_fixture()
    every, google = _pipeline(), _pipeline(scope="google_ads_source")
    assert _sql_cell(every) == (
        "at least 2", "Lower bound: 1 contact reached this stage with no entry date recorded "
        "in HubSpot and could belong to this window")
    assert _sql_cell(google) == ("2", None)
    assert google["funnel"]["undated_reach"]["outside_window_rows"]["sql"] == 0
    assert every["funnel"]["events"]["sql"]["count_is_bound"] is True
    assert google["funnel"]["events"]["sql"]["undated_contacts_could_belong"] == 0
    assert google["funnel"]["events"]["sql"]["count_is_bound"] is False


def test_03_b_an_undated_contact_outside_the_rows_bounds_the_scope(live_db, monkeypatch):
    """A Google Ads contact at SQL, undated, created in the window, with no
    stage date in it: the window's rows never see it, so Google Ads-source
    SQL is a lower bound naming the ALL-SOURCE figure — never stating how
    many of them the scope holds. On 7cd5e22: "2", exact."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    created, paid = _scope_fixture()
    _write([{"contact_id": "g-out", "created_at": created, "last_modified_at": created,
             "lifecycle_stage": "salesqualifiedlead", **paid}])
    google = _pipeline(scope="google_ads_source")
    assert _sql_cell(google) == (
        "at least 2", "Lower bound: 2 contacts across all sources reached this stage with no "
        "entry date recorded in HubSpot and could belong to this window; how many of them "
        "the lead filters select is not measured")
    sql = google["funnel"]["events"]["sql"]
    assert google["funnel"]["coverage"]["stage_reached_without_entry_date"]["sql"] == 0
    assert google["funnel"]["undated_reach"]["outside_window_rows"]["sql"] == 1
    assert sql["undated_contacts_could_belong"] is None
    assert sql["undated_contacts_all_sources"] == 2
    assert sql["count_is_bound"] is True


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


def test_04_a_a_sync_between_the_pipeline_reads_never_prints_a_false_exact(
        live_db, monkeypatch):
    """Before the sync, g-out is an undated Google Ads SQL contact outside the
    window's rows: "at least 2". After it, g-out has an in-window MQL date
    and is in the rows, still with no SQL date: "at least 2". On 98cd829 the
    rows were read before the sync and the undated reach after it:
    `outside_window_rows.sql` 0, and Google Ads-source SQL printed "2"."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    created, paid = _scope_fixture()
    start, _ = _quarter()
    _write([{"contact_id": "g-out", "created_at": created, "last_modified_at": created,
             "lifecycle_stage": "salesqualifiedlead", **paid}])
    assert _sql_cell(_pipeline(scope="google_ads_source"))[0] == "at least 2"
    fired = _sync_mid_read(monkeypatch, [{
        "contact_id": "g-out", "created_at": created,
        "last_modified_at": _at(start + timedelta(days=40)),
        "lifecycle_stage": "salesqualifiedlead", **paid,
        "date_entered_mql": _at(start + timedelta(days=10))}])
    torn = _pipeline(scope="google_ads_source")
    assert fired[1:] == [1]
    assert _sql_cell(torn)[0] == "at least 2", _sql_cell(torn)
    assert torn["funnel"]["events"]["sql"]["count_is_bound"] is True
    # Both reads saw the database before the sync: g-out outside the rows.
    assert torn["funnel"]["undated_reach"]["outside_window_rows"]["sql"] == 1
    # The sync did commit: the next build sees g-out in the rows.
    from db import crm_funnel_repository as repo
    monkeypatch.setattr(repo, "fetch_funnel_contacts", fired[0])
    after = _pipeline(scope="google_ads_source")
    assert after["funnel"]["coverage"]["stage_reached_without_entry_date"]["sql"] == 1
    assert _sql_cell(after)[0] == "at least 2"


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
def test_05_the_rows_and_the_outside_count_share_every_window_edge(live_db):
    """Four undated SQL contacts, each with ONE stage date (MQL) at an edge of
    the window, at the database's own instants (read back, so the case holds
    in any session time zone): one minute before the start, AT the start,
    one minute before the end of the last day, AT the day after. The two
    inside are the window's rows; the two outside are `outside_window_rows`.
    A second copy of the clause with a drifted edge (the audit's m2: end +2
    days; m3: start -1 day) moves a contact across and fails here."""
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
    assert read["outside_window_rows"] == {"lead": 2, "mql": 0, "sql": 2,
                                           "opportunity": 0, "customer": 0}
    # The same edges under an open start / end.
    assert repo.fetch_undated_stage_reach(None, end)["outside_window_rows"]["sql"] == 1
    assert repo.fetch_undated_stage_reach(start, None)["outside_window_rows"]["sql"] == 1
    assert {r["contact_id"] for r in repo.fetch_funnel_contacts(None, end)["rows"]} \
        == {"e-before", "e-start", "e-end"}
