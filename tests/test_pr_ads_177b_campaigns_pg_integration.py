"""PR-ADS-177B — the one Campaigns page, on PostgreSQL, through the real builders.

Every case seeds the real tables through the production writers (the
PR-ADS-176A / 177A fixtures: campaign and geo spend, FX, coverage ledgers, the
deal ledger, the canonical contact funnel and its sync state) and calls the
real builders: the mart's SETTLED campaign view (``settled=True``) and its
campaign view through today, Dashboard Overview and Campaigns, the Deals
contract, the campaign drawer, the cross-page parity audit. The UI cases render
the real payload with the real ``static/app.js`` in ``node``.

Owner decision 2026-11-24 (2): the Campaigns page's windows end YESTERDAY, as
Countries' do. The 2026-11-20 decision stands for every other revenue page:
Overview, Revenue, Deals, Channels and the Closed-Won Deals page keep today.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, RATE, live_db,
)
from tests.test_pr_ads_167a_settled_coverage_consumers_pg_integration import (  # noqa: E402
    _before_the_morning_sync, _closed_today_instant, _window_dates,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import (  # noqa: E402
    WINDOW, _identity,
)
from tests.test_pr_ads_177a_one_sql_count_pg_integration import (  # noqa: E402
    _ready, _yesterday_at,
)


def _json(payload):
    return json.loads(json.dumps(payload, default=str))


def _today_deal(cluster):
    """A Google Ads deal on `brand - uk`, closed TODAY (account-local)."""
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    _insert(cluster, "d-today", close_date=_closed_today_instant().isoformat(),
            amount=500.0, revenue_usd=500.0, country="United Kingdom",
            gclid="g-d-today", campaign="brand - uk", group="google_ads",
            attribution_status="google_ads")


def _offline_deal(cluster):
    """A deal whose label an admin marked "not Google Ads": in no campaign row."""
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    _identity(cluster, CUSTOMER, "offline import")
    _insert(cluster, "d-offline", close_date=_yesterday_at(13).isoformat(),
            amount=700.0, revenue_usd=700.0, country="United Kingdom",
            gclid="g-off", campaign="offline import", group="google_ads",
            attribution_status="google_ads")


def _settled():
    from services.revenue_decision_mart import build_revenue_decision_mart
    return _json(build_revenue_decision_mart(view="campaign", window=WINDOW, settled=True))


def _through_today():
    from services.revenue_decision_mart import build_revenue_decision_mart
    return _json(build_revenue_decision_mart(view="campaign", window=WINDOW))


def _row(mart, name):
    return next(r for r in mart["rows"] if r.get("campaign_name") == name)


def _render(mart, meta=None):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(
        '_currentUser = {"username": "u", "role": "admin"}; '
        "campaignsPageMart = %s; campaignsPageMeta = %s; renderCampaignsPage(); "
        'renderWindowRange("campaigns-range", campaignsPageMart.window); '
        '[__els["campaigns-page-body"].innerHTML, __els["campaigns-range"].textContent]'
        % (json.dumps(mart), json.dumps(meta)))


# ── 1. a deal closed today: on Overview and Deals, not on Campaigns ─────────
def test_01_a_deal_closed_today_is_on_overview_and_deals_and_not_on_campaigns(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    _today_deal(live_db)
    from services.dashboard_overview_service import build_dashboard_overview
    from services.revenue_attribution_service import build_revenue_deals
    _, today = _window_dates(WINDOW)
    yesterday = today - timedelta(days=1)

    settled = _settled()
    w = settled["window"]
    assert settled["settled_days"] is True
    assert (w["end_date"], w["through_reason"]) == (yesterday.isoformat(), "today_not_settled")
    assert w["window_range"]["disclosure"].endswith(
        "Other revenue pages, such as Deals, already count deals closed today.")
    uk = _row(settled, "brand - uk")
    assert uk["deal_ids"] == ["d-uk1"] and uk["closed_won_deals"] == 1
    assert settled["campaign_totals"]["closed_won_deals"] == 1
    assert settled["campaign_totals"]["won_revenue"] == pytest.approx(4000.0)
    assert settled["cohort"]["window"]["end_date"] == yesterday.isoformat()

    # The campaign view through today — Overview, Revenue, Deals, Channels —
    # is unchanged: it keeps today's deal.
    today_view = _through_today()
    assert today_view["settled_days"] is False
    assert today_view["window"]["end_date"] == today.isoformat()
    assert sorted(_row(today_view, "brand - uk")["deal_ids"]) == ["d-today", "d-uk1"]
    overview = _json(build_dashboard_overview(WINDOW))
    assert overview["kpis"]["closed_won_deals"] == 2
    deals = build_revenue_deals(WINDOW)
    assert sorted(d["deal_id"] for d in deals["deals"]) == ["d-today", "d-uk1"]

    # And the Campaigns page says why, beside the range.
    html, rng = _render(settled)
    assert "Other revenue pages, such as Deals, already count deals closed today." in rng
    assert yesterday.isoformat() in rng
    assert "ends yesterday" in (ROOT / "static" / "index.html").read_text()


# ── 2. the drawer is exactly the row's population ───────────────────────────
def test_02_the_drawer_lists_exactly_the_rows_deal_ids_over_the_same_days(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    _today_deal(live_db)
    _offline_deal(live_db)
    from services.revenue_attribution_service import build_campaign_deal_details
    row = _row(_settled(), "brand - uk")
    drawer = build_campaign_deal_details(WINDOW, "brand - uk", settled_days=True)
    assert drawer["settled_days"] is True
    assert sorted(d["deal_record_id"] for d in drawer["details"]) == row["deal_ids"] == ["d-uk1"]
    assert drawer["deal_ids"] == row["deal_ids"]
    # The through-today drawer lists today's deal: the two windows really differ.
    open_drawer = build_campaign_deal_details(WINDOW, "brand - uk")
    assert sorted(d["deal_record_id"] for d in open_drawer["details"]) == ["d-today", "d-uk1"]
    # A label marked "not Google Ads" is in no row, so in no drawer.
    assert not any(r.get("campaign_name") == "offline import" for r in _settled()["rows"])
    assert build_campaign_deal_details(WINDOW, "offline import", settled_days=True)["details"] == []
    # The real drawer payload on the real page matches the row and is shown.
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    html = _run_app("renderCampaignDrilldown(%s, %s)" % (json.dumps(_json(drawer)),
                                                         json.dumps(row)))
    assert "Deal d-uk1" in html and "does not match" not in html


# ── 3. the settle rule over campaign spend ──────────────────────────────────
def test_03_before_the_morning_sync_campaign_spend_is_withheld_with_the_reason(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    assert _settled()["readiness"]["spend_decision_ready"] is True
    _before_the_morning_sync(live_db, table="google_ads_spend_coverage")
    settled = _settled()
    st = settled["spend_truth"]
    assert st["campaign_spend_settled_rule_applied"] is True
    assert st["campaign_spend_unsettled_reason"] == "spend_not_reread_since_day_ended"
    assert st["campaign_spend_status"] == "incomplete"
    assert settled["readiness"]["spend_decision_ready"] is False
    t = settled["campaign_totals"]
    assert t["spend"] is None
    assert t["metrics"]["spend"]["reason"] == "spend_not_reread_since_day_ended"
    assert t["period_revenue_per_ad_spend"] is None
    # The campaign view through today does not apply the settle rule (its
    # consumers keep their window and their spend verdict).
    today_view = _through_today()
    assert today_view["spend_truth"]["campaign_spend_settled_rule_applied"] is False
    # The page draws the gate card and no figure.
    html, _ = _render(settled)
    assert 'data-campaign-spend-gate="spend_not_reread_since_day_ended"' in html
    assert 'data-kpi=' not in html and "camp-page-table" not in html
    # Dashboard → Campaigns reads the same settled view: no spend over moving days.
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    dash = _json(build_dashboard_campaigns(WINDOW))
    assert dash["kpis"]["verified_spend_usd"] is None
    assert dash["kpis"]["verified_spend_native"]["amount"] is None
    assert all(r["spend_native"] is None and r["spend_reason"] == "spend_not_reread_since_day_ended"
               for r in dash["campaigns"])


# ── 4. the residual row, the totals and the Dashboard tab ───────────────────
def test_04_rows_plus_the_residual_row_equal_the_cohort_and_the_dashboard_agrees(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    settled = _settled()
    res = settled["campaign_residual_row"]
    assert res["label"] == "No campaign label" and res["is_residual"] is True
    assert (res["leads"], res["sqls"]) == (1, 1)                    # c-nolb
    assert res["metrics"]["spend"]["status"] == "not_applicable"
    assert res["metrics"]["revenue_usd"]["reason"] == "not_campaign_attributable"
    assert not any(r.get("is_residual") for r in settled["rows"])
    t = settled["campaign_totals"]
    for field in ("leads", "sqls"):
        assert sum(r[field] for r in settled["rows"]) + res[field] == t[field] \
            == settled["summary"][field]
    # The ratio is the mart's own derived one (derive_period_revenue_per_ad_spend).
    ratio = t["metrics"]["period_revenue_per_ad_spend"]
    assert ratio["status"] == "published" and ratio["ratio_kind"] == "non_cohort_operational_ratio"
    assert ratio["value"] == pytest.approx(round(4000.0 / t["spend"], 2))
    assert "customers" in t and t["customers"] == settled["summary"]["attributed_customers"]

    from services.dashboard_campaigns_service import build_dashboard_campaigns
    dash = _json(build_dashboard_campaigns(WINDOW))
    assert dash["settled_days"] is True and dash["window"] == settled["window"]
    k = dash["kpis"]
    assert (k["sqls"], k["leads"], k["sqls_residual"]) == (t["sqls"], t["leads"], 1)
    assert k["period_revenue_per_ad_spend"] == ratio["value"]
    assert k["verified_spend_usd"] == t["spend"]
    assert dash["campaign_residual_row"] == res
    assert "keyword_themes" not in dash
    assert all(r["campaign_name"] != "Unattributed / Needs Review" for r in dash["campaigns"])
    uk = next(r for r in dash["campaigns"] if r["campaign_name"] == "brand - uk")
    assert [d["deal_id"] for d in uk["deals"]] == uk["deal_ids"] == ["d-uk1"]


def test_05_parity_the_settled_campaign_view_joins_the_settled_group(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    _today_deal(live_db)
    from services import cross_page_parity_service as parity
    report = parity.audit_window(WINDOW)
    settled = report["cohort_sql_parity"]
    assert settled["status"] == "identical", settled
    r = settled["readings"]
    assert r["campaigns_page"] == r["dashboard_campaigns"] == r["countries_page"] \
        == r["dashboard_countries"] == r["campaign_evidence"] == 4
    _, today = _window_dates(WINDOW)
    assert settled["window"]["end_date"] == (today - timedelta(days=1)).isoformat()
    cv = settled["campaign_view"]
    assert cv["window"]["end_date"] == today.isoformat()
    assert set(cv["readings"]) == {"mart_campaign_view", "dashboard_overview",
                                   "campaign_evidence"}
    by = {m["metric"]: m for m in report["metrics"]}
    for key in ("google_ads_spend_usd__settled_days",
                "campaign_attributed_closed_won_deals__settled_days",
                "google_ads_cohort_sqls__settled_days"):
        assert by[key]["status"] == "identical", (key, by[key]["status"])
    # Dashboard → Campaigns is no longer compared with the through-today view.
    assert "dashboard/campaigns" not in {r["consumer"] for r in
                                         by["campaign_attributed_closed_won_deals"]["readings"]}
    assert by["campaign_attributed_closed_won_deals__settled_days"]["value"] == 1
    assert by["campaign_attributed_closed_won_deals"]["value"] == 2       # today's deal
    assert "cohort_sql_parity_broken" not in report["violation_codes"]
    assert "value_mismatch" not in report["violation_codes"]
