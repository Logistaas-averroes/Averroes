"""PR-ADS-177B — the "Meta Ads — ad sets" table and its endpoint.

``GET /api/meta-ads/adsets`` wraps
``meta_ads_adset_truth_service.build_meta_adset_breakdown_for_days`` for one
business window's SETTLED days (``services/meta_ads_adset_table_service``).

The database cases drive the PR-ADS-171A/171C fake Meta Graph API
(``tests/meta_ads_fakes.py``: two accounts in their own time zones, paging,
renamed labels, ``actions`` only where a row has some) through the real daily
sync and the real backfill into PostgreSQL, then read the table through the
real truth service. Nothing calls Meta.

Owner decision 2026-11-24 (4): each ad set is a row treated as its own
campaign; the table has its own totals, is never summed with Google, and its
SQL / deal / revenue cells are not linked yet.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

import pytest

from services import meta_ads_adset_service as adsets
from services import meta_ads_adset_table_service as table
from services import meta_ads_spend_service as meta
from tests import meta_ads_fakes as fakes
from tests.meta_ads_fakes import (
    ADSET_A1, ADSET_A2, ADSET_B1, CAMPAIGN_A, CAMPAIGN_B, NO_ACTIONS,
    OLD_ACCOUNT_ID, FakeGraph, actions, error_response,
)
from tests.test_pr_ads_171c_meta_adsets_pg_integration import (  # noqa: F401
    _run, live_db,
)

# 12:00 UTC on the 18th. Each Meta account in its own zone: the 17th has
# ended in Amman (the old account, the one with the ads) and in Los Angeles
# (the new one, 04:00). The settled Current Quarter (London) is 1 Oct – 17 Nov.
# At 06:00 UTC the LA account's 17th was still in progress and the whole
# table was withheld for it (`test_04_b`).
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
OLD = OLD_ACCOUNT_ID
LEAD_GEN = "Lead Generation | Look Alike | 2026"
AWARENESS = "Awareness Campaign | 2026"


def _graph() -> FakeGraph:
    graph = FakeGraph()
    old = graph.add_account(OLD)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-10-05", "20.00", name="Look Alike — GCC",
              campaign_name=LEAD_GEN, actions=actions(lead=2, onsite_conversion__lead_grouped=2))
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-15", "40.00",
              actions=actions(lead=3, onsite_conversion__lead_grouped=3))
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-15", "30.00", name="Look Alike — Levant",
              actions=actions(link_click=12))
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-16", "25.00",
              actions=actions(lead=1, onsite_conversion__lead_grouped=1))
    old.adset(ADSET_B1, CAMPAIGN_B, "2026-11-17", "12.00", name="Awareness — MENA",
              campaign_name=AWARENESS, actions=actions(video_view=900))
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-18", "9.99")          # in progress
    return graph


def _load(monkeypatch, graph=None, now=NOW):
    """The daily sync, then both levels backfilled from the quarter's start."""
    graph = graph or _graph()
    fakes.configure(monkeypatch, graph)
    _run(now)
    assert meta.run_backfill(date(2026, 10, 1), None, now=now)["status"] == "success"
    out = meta.run_backfill(date(2026, 10, 1), None, now=now, level="adset")
    assert out["status"] == "success", out
    return graph


def _keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}", k
            yield from _keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _keys(v, f"{path}[{i}]")


# ── 1. published: one row per ad set, its own totals, outcomes not linked ───
def test_01_the_table_publishes_ad_sets_by_id_with_their_own_totals(live_db, monkeypatch):
    monkeypatch.setattr(adsets, "native_lead_action_type", lambda: "lead")
    _load(monkeypatch)
    out = json.loads(json.dumps(table.build_meta_adset_table("current_quarter", now=NOW),
                                default=str))
    assert out["status"] == "published", (out["reason"], out["detail"], out["accounts"])
    assert out["read_only"] is True and out["scope"] == "meta_ads_adsets"
    assert out["never_summed_with"] == "google_ads_campaigns"
    w = out["window"]
    assert (w["start_date"], w["end_date"], w["through_reason"]) == (
        "2026-10-01", "2026-11-17", "today_not_settled")
    assert out["settled_rule_applied"] is True
    assert {a["account_id"]: (a["time_zone"], a["required_through"]) for a in out["accounts"]} == {
        "4730305973961806": ("America/Los_Angeles", "2026-11-17"),
        OLD: ("Asia/Amman", "2026-11-17")}
    rows = {(r["account_id"], r["adset_id"]): r for r in out["rows"]}
    assert set(rows) == {(OLD, ADSET_A1), (OLD, ADSET_A2), (OLD, ADSET_B1)}
    a1 = rows[(OLD, ADSET_A1)]
    # The 18th is in progress: not in the settled days.
    assert a1["spend"]["value"] == 60.0 and a1["spend"]["currency"] == "USD"
    assert a1["campaign_id"] == CAMPAIGN_A and a1["time_zone"] == "Asia/Amman"
    assert out["names_are_labels"] is True
    for r in out["rows"]:
        assert {k: v["status"] for k, v in r["hubspot_outcomes"].items()} == {
            "hubspot_sql_contacts": "not_linked", "hubspot_closed_won_deals": "not_linked",
            "hubspot_won_revenue": "not_linked"}
        assert all(v["value"] is None for v in r["hubspot_outcomes"].values())
    t = out["totals"]
    assert t["adsets"] == 3
    assert t["spend"] == {"value": 127.0, "status": "published", "reason": None,
                          "currency": "USD"}
    assert t["spend_usd"]["value"] == pytest.approx(sum(r["spend_usd"]["value"]
                                                       for r in out["rows"]))
    assert t["impressions"]["value"] == sum(r["impressions"]["value"] for r in out["rows"])
    assert t["meta_reported_leads"] == {"value": 6, "status": "published", "reason": None}
    assert t["hubspot_outcomes"]["hubspot_won_revenue"]["status"] == "not_linked"
    assert "owner_declared_retired_disclosure" in out
    # No ROAS / CAC key, and no ratio over Meta spend, anywhere.
    for path, key in _keys(out):
        assert key.lower() not in ("roas", "cac", "cpl", "cpa"), path
        assert "per_" not in key.lower() and "ratio" not in key.lower(), path


def test_02_a_total_is_never_a_partial_sum(live_db, monkeypatch):
    """A day of A1 carried no `actions`: its Meta-reported leads are unknown,
    so the table's lead total is unknown too — never the sum of the rest."""
    monkeypatch.setattr(adsets, "native_lead_action_type", lambda: "lead")
    graph = _graph()
    graph.accounts[OLD].adset(ADSET_A1, CAMPAIGN_A, "2026-11-16", "35.34", actions=NO_ACTIONS)
    _load(monkeypatch, graph)
    out = table.build_meta_adset_table("current_quarter", now=NOW)
    assert out["status"] == "published"
    a1 = next(r for r in out["rows"] if r["adset_id"] == ADSET_A1)
    assert a1["meta_reported_leads"]["status"] == "unavailable"
    lt = out["totals"]["meta_reported_leads"]
    assert lt["value"] is None and lt["status"] == "unavailable"
    assert lt["reason"] == "meta_actions_missing_on_some_days"
    assert lt["blocked_by_adset"] == ADSET_A1
    # The spend total still publishes: every row's spend is known.
    assert out["totals"]["spend"]["status"] == "published"


def test_03_unverified_days_withhold_the_whole_table_with_a_reason(live_db, monkeypatch):
    """The next morning the ad-set read fails: the 18th is verified at
    campaign level only. Nothing is returned for any account — not the rows
    that are verified, not a total."""
    graph = _load(monkeypatch)
    graph.accounts[OLD].adset_refusal = error_response(500, 2)
    tomorrow = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    _run(tomorrow)
    out = table.build_meta_adset_table("current_quarter", now=tomorrow)
    assert out["status"] == "withheld"
    assert out["reason"] == "meta_adset_coverage_incomplete"
    assert OLD in out["detail"]
    assert out["rows"] == [] and out["totals"] is None


def test_04_a_window_with_no_settled_day_measures_nothing(live_db, monkeypatch):
    _load(monkeypatch)
    first = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    out = table.build_meta_adset_table("current_quarter", now=first)
    assert out["status"] == "withheld" and out["reason"] == "no_settled_day_in_window"
    assert out["rows"] == [] and out["totals"] is None


def test_04_b_each_account_is_read_in_its_own_time_zone(live_db, monkeypatch):
    """At 06:00 UTC on the 18th the 17th has ended in London (the page's
    settled window) and in Amman, but not in Los Angeles: the LA account's
    17th is still in progress, so the window cannot be held for it and the
    whole table is withheld, naming the account — never published without
    one account. At 12:00 (test_01) the LA 17th has ended and it publishes."""
    early = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
    _load(monkeypatch, now=early)
    out = table.build_meta_adset_table("current_quarter", now=early)
    assert out["window"]["end_date"] == "2026-11-17"
    assert out["status"] == "withheld"
    assert out["reason"] == "meta_campaign_spend_not_published"
    assert "4730305973961806" in out["detail"] and "2026-11-16" in out["detail"]
    assert out["rows"] == [] and out["totals"] is None


# ── 5–6. the pure parts and the route ───────────────────────────────────────
def _m(value, cur=None, status="published", reason=None):
    out = {"value": value if status == "published" else None, "status": status,
           "reason": reason}
    if cur:
        out["currency"] = cur
    return out


def test_05_totals_refuse_two_currencies_and_two_attribution_settings():
    rows = [
        {"adset_id": "a", "spend": _m(10.0, "USD"), "spend_usd": _m(10.0, "USD"),
         "impressions": _m(5), "clicks": _m(1), "meta_reported_leads": _m(1),
         "attribution_settings": ["7d_click_1d_view"]},
        {"adset_id": "b", "spend": _m(8.0, "GBP"), "spend_usd": _m(10.0, "USD"),
         "impressions": _m(6), "clicks": _m(2), "meta_reported_leads": _m(2),
         "attribution_settings": ["1d_click"]},
    ]
    t = table.table_totals(rows)
    assert t["spend"]["value"] is None and t["spend"]["reason"] == "meta_accounts_use_different_currencies"
    assert t["spend_usd"]["value"] == 20.0
    assert t["meta_reported_leads"]["value"] is None
    assert t["meta_reported_leads"]["reason"] == "meta_attribution_settings_differ"
    assert (t["impressions"]["value"], t["clicks"]["value"]) == (11, 3)
    rows[1]["spend_usd"] = _m(None, "USD", "unavailable", "fx_rates_missing")
    assert table.table_totals(rows)["spend_usd"] == {
        "value": None, "status": "unavailable", "reason": "fx_rates_missing",
        "blocked_by_adset": "b", "currency": "USD"}


def test_06_the_route_is_read_only_authenticated_and_wraps_the_table(monkeypatch):
    from fastapi.testclient import TestClient
    from api.auth import set_session
    from api.server import app
    from starlette.responses import Response as StarletteResponse
    routes = [r for r in app.routes if getattr(r, "path", "") == "/api/meta-ads/adsets"]
    assert len(routes) == 1 and routes[0].methods == {"GET"}
    seen = {}

    def fake(window, now=None):
        seen["window"] = window
        return {"status": "withheld", "reason": "x", "rows": [], "totals": None}
    monkeypatch.setattr(table, "build_meta_adset_table", fake)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/meta-ads/adsets?window=ytd").status_code == 401
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    r = StarletteResponse()
    set_session(r, "viewer", "viewer")
    cookie = next(p.split("=", 1)[1] for p in r.headers.get("set-cookie", "").split(";")
                  if p.strip().startswith("ads_session="))
    client.cookies.set("ads_session", cookie)
    resp = client.get("/api/meta-ads/adsets?window=ytd")
    assert resp.status_code == 200 and seen == {"window": "ytd"}
    assert client.post("/api/meta-ads/adsets?window=ytd").status_code == 405
