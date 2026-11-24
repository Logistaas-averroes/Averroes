"""PR-ADS-177A — one SQL count, on PostgreSQL, through the real builders.

Every case seeds the real tables through the production writers (spend, geo,
FX, the deal ledger, the canonical contact funnel and its sync state) and
calls the real builders: the Revenue Decision Mart's campaign and country
views, Dashboard Overview / Campaigns / Countries / Revenue / Deals, Campaign
Evidence, the cross-page parity audit and the cohort audit. The UI cases
render the real payload with the real ``static/app.js`` in ``node``.

The fixture ALSO seeds the legacy ``leads`` table with qualified rows that
the old mart counted. They are counted nowhere now: the one SQL count is the
acquisition cohort.

Every case fails on ``dce66da`` (PR-ADS-176C): there the mart counted the
legacy rows (2 SQLs here, not the cohort's 3), carried no ``cohort`` block,
published leads and SQLs beside an incomplete contact sync, judged a
spending row "waste" on a withheld count, and Campaign Evidence disagreed
with the Countries page for the same days.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, RATE, UAE, live_db,
)
from tests.test_pr_ads_167a_settled_coverage_consumers_pg_integration import (  # noqa: E402
    _window_dates,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import (  # noqa: E402
    FIJI, WINDOW, _seed, _sync,
)

WITHHELD_REASON = "source_bootstrap_incomplete"


def _json(payload):
    return json.loads(json.dumps(payload, default=str))


def _yesterday_at(hour=10):
    _, today = _window_dates(WINDOW)
    return datetime.combine(today - timedelta(days=1), datetime.min.time(),
                            tzinfo=timezone.utc) + timedelta(hours=hour)


def _funnel_fresh():
    from db import writers
    now = datetime.now(timezone.utc)
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete", last_incremental_at=now,
        last_modified_watermark=now, last_status="success",
        last_sync_mode="incremental", last_incremental_status="success",
        last_successful_incremental_at=now, last_error=None)


def _funnel_bootstrap_running():
    from db import writers
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="running", last_status="partial")


def _contacts():
    """The acquisition cohort, created yesterday (inside the settled window):

    * c-uk   SQL (customer stage, undated), Brand - UK, ip_country UK
    * c-uk2  lead (not an SQL),            Brand - UK, country UK
    * c-ae   SQL (direct date),             Brand - UK, ip_country UAE beats country UK
    * c-fj   SQL (opportunity),             "Mystery" (unmapped) label, Fiji (no market)
    * c-nolb SQL,                           no campaign label, no country
    * c-org  SQL, ORGANIC — not Google Ads: in no row and no total
    """
    from db import writers
    y = _yesterday_at()
    base = {"created_at": y, "last_modified_at": y, "hs_analytics_source": "PAID_SEARCH",
            "hs_analytics_source_data_1": "brand - uk"}
    writers.upsert_hubspot_contact_funnel([
        {**base, "contact_id": "c-uk", "lifecycle_stage": "customer",
         "ip_country": "United Kingdom"},
        {**base, "contact_id": "c-uk2", "lifecycle_stage": "lead",
         "country": "United Kingdom"},
        {**base, "contact_id": "c-ae", "lifecycle_stage": "salesqualifiedlead",
         "date_entered_sql": y, "ip_country": "United Arab Emirates",
         "country": "United Kingdom"},
        {**base, "contact_id": "c-fj", "lifecycle_stage": "opportunity",
         "hs_analytics_source_data_1": "Mystery", "country": "Fiji"},
        {**base, "contact_id": "c-nolb", "lifecycle_stage": "salesqualifiedlead",
         "hs_analytics_source_data_1": None},
        {**base, "contact_id": "c-org", "lifecycle_stage": "customer",
         "hs_analytics_source": "ORGANIC_SEARCH", "ip_country": "United Kingdom"},
    ])


def _legacy_leads(cluster):
    """Legacy `leads` rows the OLD mart counted: two qualified UK leads. The
    acquisition cohort ignores them — they are another metric family."""
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO runs (run_type, started_at, status) VALUES "
                    "('daily', now(), 'success') RETURNING id")
        run_id = cur.fetchone()[0]
        for cid in ("legacy-1", "legacy-2"):
            cur.execute(
                "INSERT INTO leads (run_id, run_date, contact_id, country, "
                "status_category, campaign_name, keyword, source_type, "
                "contact_created_at) VALUES (%s, CURRENT_DATE, %s, 'United Kingdom', "
                "'qualified', 'brand - uk', 'tms', 'paid_search', %s)",
                (run_id, cid, _yesterday_at(9)))
        conn.commit()


def _ready(cluster, *, fresh=True):
    """UK £200 (two days), UAE £16 and Fiji £8 yesterday; campaign spend
    reconciles; one Google Ads UK deal PLACED on `brand - uk` (so the campaign
    placement gate publishes and verdicts are computed); the cohort above;
    legacy leads."""
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    _seed(cluster, yesterday_geo=[(UAE, 16.0), (FIJI, 8.0)])
    _sync(cluster)
    _insert(cluster, "d-uk1", close_date=_yesterday_at(12).isoformat(), amount=4000.0,
            revenue_usd=4000.0, country="United Kingdom", gclid="g-d-uk1",
            campaign="brand - uk", group="google_ads", attribution_status="google_ads")
    _contacts()
    _legacy_leads(cluster)
    if fresh:
        _funnel_fresh()
    else:
        _funnel_bootstrap_running()


def _mart(view):
    from services.revenue_decision_mart import build_revenue_decision_mart
    return _json(build_revenue_decision_mart(view=view, window=WINDOW))


def _row(mart, **match):
    return next(r for r in mart["rows"]
                if all(r.get(k) == v for k, v in match.items()))


# ── 1. published: the mart's leads / SQLs are the cohort, not the leads table ─
def test_01_published_the_mart_counts_the_acquisition_cohort_not_legacy_lead_status(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    m = _mart("campaign")
    c = m["cohort"]
    assert (c["sql_status"], c["sql_reason"]) == ("published", None)
    assert (c["metric_family"], c["window_basis"]) == ("acquisition_cohort_outcomes",
                                                       "contact_created_at")
    assert c["metadata"]["as_of"] and c["metadata"]["source_freshness"]["fresh"] is True
    # Google Ads cohort: c-uk, c-uk2, c-ae, c-fj, c-nolb → 5 leads, 4 SQLs.
    # The legacy rows' two qualified leads are in no figure; c-org is organic.
    s = m["summary"]
    assert (s["leads"], s["sqls"]) == (5, 4)
    assert (s["sqls_status"], s["metric_family"], s["window_basis"]) == (
        "published", "acquisition_cohort_outcomes", "contact_created_at")
    assert s["metrics"]["sqls"] == {"value": 4, "status": "published", "reason": None}
    uk = _row(m, campaign_name="brand - uk")
    assert (uk["leads"], uk["sqls"], uk["sqls_status"]) == (3, 2, "published")
    assert m["sql_reconciliation"]["metric_family"] == "acquisition_cohort_outcomes"
    assert m["sql_reconciliation"]["reconciliation_status"] == "reconciled"
    assert m["readiness"]["lead_metrics_ready"] is True


# ── 2. conservation: rows + residual = the window cohort total ──────────────
def test_02_campaign_rows_plus_the_campaign_residual_equal_the_cohort_total(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    m = _mart("campaign")
    residual = m["cohort"]["campaign_residual"]
    assert (residual["leads"], residual["sqls"]) == (1, 1)          # c-nolb
    mystery = _row(m, campaign_name="Mystery")                      # its own row
    assert (mystery["leads"], mystery["sqls"]) == (1, 1)
    assert mystery["verdict"] == "mapping_required"
    for field in ("leads", "sqls"):
        assert sum(r[field] for r in m["rows"]) + residual[field] == m["summary"][field]


def test_03_country_rows_plus_the_residual_row_equal_the_cohort_total(live_db, monkeypatch):
    """By the contact's own country (ip_country, then country): c-ae is the
    UAE's though its form said UK; Fiji (no market) and no-country are the
    residual's. country_totals (markets) + residual row = the total."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    m = _mart("country")
    assert m["spend_truth"]["country_spend_status"] in ("verified", "reconciled_with_residual")
    uk, ae = _row(m, country_code="GB"), _row(m, country_code="AE")
    residual = _row(m, is_residual=True)
    assert (uk["leads"], uk["sqls"]) == (2, 1)
    assert (ae["leads"], ae["sqls"]) == (1, 1)
    assert (residual["leads"], residual["sqls"]) == (2, 2)          # c-fj, c-nolb
    for field in ("leads", "sqls"):
        assert sum(r[field] for r in m["rows"]) == m["summary"][field]
        assert m["country_totals"][field] + residual[field] == m["summary"][field]
    assert m["country_totals"]["lead_scope"] == "google_ads_cohort_contacts_by_contact_country"
    dash = _json(__import__("services.dashboard_countries_service", fromlist=["x"])
                 .build_dashboard_countries(WINDOW))
    assert dash["kpis"]["sqls"] + dash["residual"]["sqls"] == m["summary"]["sqls"]
    assert dash["cohort"]["sql_status"] == "published"


# ── 4. withheld: no count anywhere, the reason everywhere ───────────────────
def test_04_an_incomplete_contact_sync_withholds_every_count_on_every_surface(
        live_db, monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, fresh=False)
    payloads = audit._build_mart_payloads(WINDOW, None)
    assert set(payloads) == {"mart/campaign", "mart/country", "dashboard/overview",
                             "dashboard/campaigns", "dashboard/countries",
                             "dashboard/revenue", "dashboard/deals"}
    for name, p in payloads.items():
        assert p is not None, name
        assert (p["cohort"]["sql_status"], p["cohort"]["sql_reason"]) == (
            "withheld", WITHHELD_REASON), name
        assert audit.mart_withheld_exposures(p) == [], (name, audit.mart_withheld_exposures(p))
    mart = payloads["mart/campaign"]
    assert mart["summary"]["metrics"]["sqls"] == {
        "value": None, "status": "withheld", "reason": WITHHELD_REASON}
    assert all(r["sqls"] is None and r["sqls_status"] == "withheld" for r in mart["rows"])
    # The withheld cohort blocks no page (owner decision): revenue is still
    # decision-ready, only the leads / SQLs are withheld.
    assert mart["readiness"]["revenue_decision_ready"] is True
    assert mart["readiness"]["lead_metrics_ready"] is False
    assert payloads["dashboard/overview"]["kpis"]["google_ads_source_sqls"] is None
    country = payloads["mart/country"]
    assert country["country_totals"]["metrics"]["sqls"]["reason"] == WITHHELD_REASON
    # The audit's per-window check agrees.
    a = audit.Audit()
    audit.audit_mart_window(a, window=WINDOW, payloads=payloads)
    assert a.violations == [] and a.unavailable == []


def test_04b_counterfactual_the_audit_goes_red_when_a_withheld_count_leaks(
        live_db, monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, fresh=False)
    payloads = audit._build_mart_payloads(WINDOW, None)
    payloads["dashboard/countries"]["residual"]["sqls"] = 0     # a fabricated zero
    a = audit.Audit()
    audit.audit_mart_window(a, window=WINDOW, payloads=payloads)
    assert any("dashboard/countries withheld_not_exposed" in v for v in a.violations)


# ── 5. verdicts are never computed on a withheld SQL count ──────────────────
def test_05_a_spending_campaign_with_withheld_sqls_is_not_judged(live_db, monkeypatch):
    """The Gulf campaign (£300 spend, no deal, no contact) read 'waste' on
    dce66da whatever the contact sync said. It is 'waste' only when the cohort
    PROVES zero SQLs; withheld, it is not judged — and `brand - uk`, judged on
    its revenue, keeps its verdict."""
    from db import writers
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, fresh=False)
    start, today = _window_dates(WINDOW)
    writers.upsert_campaign_daily_spend([{
        "customer_id": CUSTOMER, "currency_code": "GBP", "campaign_id": "222",
        "campaign_name": "Gulf", "spend_date": today - timedelta(days=2),
        "cost_micros": 300_000_000, "source_query_version": "t"}])
    m = _mart("campaign")
    gulf = _row(m, campaign_name="Gulf")
    assert gulf["closed_won_deals"] == 0 and gulf["sqls"] is None
    assert (gulf["verdict"], gulf["verdict_reason"]) == ("sqls_unknown", WITHHELD_REASON)
    brand = _row(m, campaign_name="brand - uk")
    assert brand["closed_won_deals"] == 1 and brand["verdict"] in ("winner", "watch")
    dash = _json(__import__("services.dashboard_campaigns_service", fromlist=["x"])
                 .build_dashboard_campaigns(WINDOW))
    drow = next(r for r in dash["campaigns"] if r["campaign_name"] == "Gulf")
    assert drow["status"] == "SQLs withheld"
    # Positive control: published, the same campaign is judged on a proven zero.
    _funnel_fresh()
    m2 = _mart("campaign")
    gulf2 = _row(m2, campaign_name="Gulf")
    assert gulf2["sqls"] == 0 and gulf2["verdict"] == "waste"
    dash2 = _json(__import__("services.dashboard_campaigns_service", fromlist=["x"])
                  .build_dashboard_campaigns(WINDOW))
    assert next(r for r in dash2["campaigns"] if r["campaign_name"] == "Gulf")[
        "status"] == "Spend without SQL / customer proof"


def test_05b_country_verdicts_and_statuses_under_a_withheld_cohort(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, fresh=False)
    m = _mart("country")
    uk = _row(m, country_code="GB")
    assert uk["closed_won_deals"] == 1 and uk["verdict"] in ("winner", "watch")
    for r in m["rows"]:
        if not r["is_residual"] and not r["closed_won_deals"]:
            # A market with no won deal is never "waste" / "watch" on withheld SQLs.
            assert r["verdict"] not in ("waste", "watch"), r
    dash = _json(__import__("services.dashboard_countries_service", fromlist=["x"])
                 .build_dashboard_countries(WINDOW))
    ae = next(r for r in dash["countries"] if r["country_code"] == "AE")
    # UAE: spend, no deal — its status must not claim anything about SQLs.
    assert ae["status"] == "SQLs withheld"
    assert ae["sqls"] is None and ae["sqls_status"] == "withheld"
    mix = {r["region"]: r for r in dash["regional_mix"]}
    assert mix["Middle East"]["status"] == "SQLs withheld"


# ── 6. parity: the same days, one number on three surfaces ──────────────────
def test_06_the_countries_page_dashboard_countries_and_campaign_evidence_agree(
        live_db, monkeypatch):
    from services import cross_page_parity_service as parity
    from services.campaign_evidence_service import build_campaign_evidence
    from datetime import date
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    page = _mart("country")
    w = page["window"]
    ce = build_campaign_evidence(WINDOW, bounds=(
        date.fromisoformat(w["start_date"]), date.fromisoformat(w["end_date"])))
    assert ce["summary"]["cohort_sqls_google_ads"] == page["summary"]["sqls"] == 4
    report = parity.audit_window(WINDOW)
    p = report["cohort_sql_parity"]
    assert p["status"] == "identical", p
    assert p["readings"] == {"countries_page": 4, "dashboard_countries": 4,
                             "campaign_evidence": 4}
    conservation = next(r for r in report["conservation"]
                        if r["total"] == "google_ads_cohort_sqls")
    assert conservation["status"] == "conserved"
    ids = {m["metric"]: m for m in report["metrics"]}
    assert ids["google_ads_cohort_sqls"]["status"] == "identical"
    assert ids["google_ads_cohort_sqls"]["value"] == 4
    assert "cohort_sql_parity_broken" not in report["violation_codes"]


def test_06b_counterfactual_a_surface_that_disagrees_breaks_parity(live_db, monkeypatch):
    from services import cross_page_parity_service as parity
    import services.dashboard_countries_service as dc
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    real = dc.build_dashboard_countries

    def _off_by_one(*a, **k):
        out = real(*a, **k)
        out["residual"] = {**out["residual"], "sqls": (out["residual"]["sqls"] or 0) + 1}
        return out
    monkeypatch.setattr(dc, "build_dashboard_countries", _off_by_one)
    report = parity.audit_window(WINDOW)
    assert report["cohort_sql_parity"]["status"] == "broken"
    assert "cohort_sql_parity_broken" in report["violation_codes"]


def test_06c_withheld_parity_is_not_evaluable_never_a_pass(live_db, monkeypatch):
    from services import cross_page_parity_service as parity
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, fresh=False)
    p = parity.audit_window(WINDOW)["cohort_sql_parity"]
    assert p["status"] == "not_evaluable"
    assert p["readings"] == {"countries_page": None, "dashboard_countries": None,
                             "campaign_evidence": None}


# ── 7. the real payload on the real page ─────────────────────────────────────
def _render(script):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app('_currentUser = {"username": "u", "role": "admin"}; ' + script)


def test_07_the_countries_page_renders_the_word_and_the_reason_never_a_number(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, fresh=False)
    m = _mart("country")
    html = _render("roasCountryMart = %s; roasCountriesData = roasCountryMart.rows || []; "
                   "renderRoasCountriesPage(); __els['roas-countries-table-body'].innerHTML"
                   % json.dumps(m))
    assert "SQLs · contacts acquired in window" in html
    assert "Withheld" in html and "backfill is still running" in html
    kpi = html[html.index('data-kpi="sqls"'):html.index('data-kpi="deals"')]
    assert "Withheld" in kpi and ">0<" not in kpi


def test_07b_published_the_same_page_prints_the_cohort_numbers(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    m = _mart("country")
    out = _render("roasCountryMart = %s; [renderRoasCountryTable(roasCountryMart.rows, "
                  "campBlockerOpts(roasCountryMart)), renderRoasCountrySummary(roasCountryMart)]"
                  % json.dumps(m))
    table, kpis = out
    assert "Withheld" not in table
    # The cohort's markets: UK 2 leads / 1 SQL, the UAE 1 / 1 (c-ae's own
    # ip_country). The legacy table's UK-only 2 / 2 is in no cell.
    assert (m["country_totals"]["leads"], m["country_totals"]["sqls"]) == (3, 2)
    kpi = kpis[kpis.index('data-kpi="sqls"'):kpis.index('data-kpi="deals"')]
    assert "SQLs · contacts acquired in window" in kpi and ">2<" in kpi
    ae = table[table.index("United Arab Emirates"):]
    ae = ae[:ae.index("</tr>")]
    assert '<td class="ctry-num">1</td>' in ae                 # its lead and its SQL


def test_07c_dashboard_campaigns_renders_the_gate_from_its_own_response(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db, fresh=False)
    d = _json(__import__("services.dashboard_campaigns_service", fromlist=["x"])
              .build_dashboard_campaigns(WINDOW))
    assert (d["cohort"]["sql_status"], d["metric_family"]) == (
        "withheld", "acquisition_cohort_outcomes")
    assert d["revenue_scope"] == "campaign_attributable"
    out = _render("const d = %s; [renderCampKpiRow(d), renderCampLeaderboard(d)]"
                  % json.dumps(d))
    assert all("Withheld" in h for h in out)
    kpi = out[0][out[0].index('data-kpi="sqls"'):]
    assert "SQLs · contacts acquired in window" in kpi
