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
