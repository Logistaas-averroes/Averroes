"""PR-ADS-176B — the one Countries page, on PostgreSQL.

The page reads the mart's country view and decides nothing. Every case seeds
the real tables through the real writers (the PR-ADS-176A fixtures) and calls
the real builders; the UI cases render the real payload with the real
``static/app.js`` in ``node``.

1. The scope declaration says what the rows count. The mart's
   ``country_truth`` and the Dashboard (``revenue_scope`` and its
   ``country_truth``) declared ``all_source`` while every row and drawer
   counts ``google_ads_source`` deals (docs/58 §5). An organic deal is in no
   row here; the declaration now says so.
2. ``country_totals`` and ``regional_mix`` — the figures the page shows above
   and beside its table — are the mart's, over the listed markets only: the
   residual and its itemised outside-market countries are never in them, a
   row whose figure is unknown makes its total unknown, customers are never
   summed, and a gate that is not ready publishes no total at all.

Every case fails on 2eda9ac (PR-ADS-176A-F2): there the declaration said
``all_source`` and the payload had no ``country_totals`` / ``regional_mix``
or row ``region``.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, RATE, UAE, live_db,
)
from tests.test_pr_ads_167a_settled_coverage_consumers_pg_integration import (  # noqa: E402
    _before_the_morning_sync,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import (  # noqa: E402
    FIJI, LOOSE, _dash, _deal, _lead, _mart, _seed, _sync,
)

US = ("2840", "US", "United States")


def _json(payload):
    return json.loads(json.dumps(payload, default=str))


def _markets(mart):
    return [r for r in mart["rows"] if not r["is_residual"]]


def _row(mart, code):
    return next(r for r in mart["rows"] if r.get("country_code") == code)


def _ready(cluster, *, loose=False):
    """UK £200, US £40, UAE £16 and Fiji £8 (not a listed market); campaign
    spend reconciles. Google Ads deals in the UK (2), the US and Atlantis (no
    market: the residual); an ORGANIC UK deal, all-source only."""
    extra = [(US, 40.0), (UAE, 16.0), (FIJI, 8.0)] + ([(LOOSE, 3.0)] if loose else [])
    _seed(cluster, yesterday_geo=extra)
    _sync(cluster)
    _deal(cluster, "d-uk1", "United Kingdom", amount=4000.0)
    _deal(cluster, "d-uk2", "United Kingdom", amount=1000.0)
    _deal(cluster, "d-us1", "United States", amount=2500.0)
    _deal(cluster, "d-atl", "Atlantis", amount=700.0)
    _deal(cluster, "d-org", "United Kingdom", amount=9999.0, google=False)
    for i, country in enumerate(["United Kingdom", "United States", "Fiji"]):
        _lead(cluster, f"c{i}", country)


# ── 1. the scope declaration ─────────────────────────────────────────────────
def test_01_the_country_scope_declaration_says_what_the_rows_count(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    mart = _mart()
    uk = _row(mart, "GB")
    # The rows count google_ads_source deals: the organic UK deal is in none.
    assert uk["closed_won_deals"] == 2 and uk["won_revenue"] == 5000.0
    assert "d-org" not in (uk["deal_ids"] or [])
    assert mart["country_truth"]["revenue_scope"] == "google_ads_source"
    assert mart["country_totals"]["revenue_scope"] == "google_ads_source"
    page = _dash()
    assert page["revenue_scope"] == "google_ads_source"
    assert page["countries"], "the Dashboard has rows to describe"
    # The Dashboard builder's own disclosure block (not served on the route,
    # but the same declaration) says it too.
    from services import dashboard_countries_service as dash
    assert dash._assemble("last_6_months", None)["country_truth"][
        "revenue_scope"] == "google_ads_source"


# ── 2. the page's totals and regional mix ────────────────────────────────────
def test_02_totals_are_the_listed_markets_rows_and_never_the_residual(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    mart = _mart()
    assert mart["spend_truth"]["country_spend_status"] in (
        "verified", "reconciled_with_residual")
    markets = _markets(mart)
    t = mart["country_totals"]
    assert t["markets"] == len(markets) == 3
    assert t["spend"] == pytest.approx(sum(r["spend"] for r in markets))
    assert t["spend"] == pytest.approx((200.0 + 40.0 + 16.0) * RATE)   # not Fiji's £8
    assert t["closed_won_deals"] == sum(r["closed_won_deals"] for r in markets) == 3
    assert t["won_revenue"] == pytest.approx(7500.0)                   # not Atlantis' 700
    assert t["leads"] == 2                                             # not the Fiji lead
    assert t["metrics"]["spend"]["status"] == "published"
    ratio = t["metrics"]["period_revenue_per_ad_spend"]
    assert ratio["status"] == "published"
    assert ratio["value"] == pytest.approx(round(7500.0 / t["spend"], 2))
    assert ratio["ratio_kind"] == "non_cohort_operational_ratio"
    assert "customers" not in t and "customers" not in t["metrics"]

    residual = next(r for r in mart["rows"] if r["is_residual"])
    assert residual["region"] is None
    assert [i["country_code"] for i in residual["outside_listed_market_items"]] == ["FJ"]
    assert {r["country_code"]: r["region"] for r in markets} == {
        "GB": "Europe", "US": "North America", "AE": "Middle East"}
    mix = {r["region"]: r for r in mart["regional_mix"]}
    assert list(mix) == ["North America", "Europe", "Middle East"]   # REGION_ORDER
    assert mix["Europe"]["countries"] == 1
    assert mix["Europe"]["won_revenue"] == pytest.approx(5000.0)
    assert mix["North America"]["spend"] == pytest.approx(40.0 * RATE)
    assert sum(r["closed_won_deals"] for r in mart["regional_mix"]) == t["closed_won_deals"]
    assert all("customers" not in r for r in mart["regional_mix"])


def test_03_an_unknown_row_makes_its_total_unknown_with_that_row_s_reason(
        live_db, monkeypatch):
    """Pakistan won a deal and has no geo row while £3 sits on a criterion no
    stored row names: the zero rule withholds its spend (PR-ADS-176A). The
    markets' spend total is then unknown — with Pakistan's reason — and so is
    the ratio; the deal and revenue totals, all known, still publish."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, loose=True)
    _deal(live_db, "d-pk", "Pakistan", amount=300.0)
    mart = _mart()
    pk = _row(mart, "PK")
    assert pk["spend"] is None and pk["spend_reason"] == "spend_location_unresolved"
    t = mart["country_totals"]
    assert t["spend"] is None
    assert t["metrics"]["spend"]["status"] == "unavailable"
    assert t["metrics"]["spend"]["reason"] == "spend_location_unresolved"
    assert t["period_revenue_per_ad_spend"] is None
    assert t["metrics"]["period_revenue_per_ad_spend"]["reason"] == "spend_location_unresolved"
    assert t["closed_won_deals"] == 4 and t["won_revenue"] == pytest.approx(7800.0)
    apac = next(r for r in mart["regional_mix"] if r["region"] == "APAC")
    assert apac["spend"] is None and apac["metrics"]["spend"]["status"] == "unavailable"
    assert apac["closed_won_deals"] == 1


def test_04_a_gate_that_is_not_ready_publishes_no_total(live_db, monkeypatch):
    """Before the morning sync the gate is not ready and the rows carry no
    figures (PR-ADS-167A). A sum over withheld rows is not a zero: every
    total and every region is unavailable with the gate's gap."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    _before_the_morning_sync(live_db)
    mart = _mart()
    st = mart["spend_truth"]
    assert st["country_spend_status"] == "unavailable"
    gap = st["country_gap_codes"][0]
    t = mart["country_totals"]
    for key in ("spend", "leads", "sqls", "closed_won_deals", "won_revenue",
                "period_revenue_per_ad_spend"):
        assert t[key] is None, key
    for key, m in t["metrics"].items():
        assert m["status"] == "unavailable" and m["value"] is None, key
        assert m["reason"] == gap or (m.get("blocked_by") or {}).get("reason") == gap, key
    for region in mart["regional_mix"]:
        assert region["won_revenue"] is None and region["closed_won_deals"] is None


# ── 3. the real payload on the real page ─────────────────────────────────────
def _render(mart, user="admin"):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(
        '_currentUser = {"username": "u", "role": "%s"}; '
        "roasCountryMart = %s; roasCountriesData = roasCountryMart.rows || []; "
        'renderRoasCountriesPage(); __els["roas-countries-table-body"].innerHTML'
        % (user, json.dumps(_json(mart))))


def test_05_the_page_renders_the_real_payload(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, loose=True)
    _deal(live_db, "d-pk", "Pakistan", amount=300.0)
    html = _render(_mart())
    assert 'data-kpi="spend"' in html
    kpi = re.search(r'data-kpi="spend".*?dash-kpi-card__source', html, re.DOTALL).group(0)
    assert "Unavailable" in kpi and "some spend in this window has no country" in kpi
    assert 'aria-label="Open Pakistan detail"' in html
    assert "</span>Fiji" in html and "In residual" in html
    assert "Regional Mix" in html and ">Europe<" in html

    _before_the_morning_sync(live_db)
    blocked = _render(_mart())
    assert "revenue-blocked-card" in blocked and "Nothing to run" in blocked
    assert "ctry-table" not in blocked and "data-kpi=" not in blocked
