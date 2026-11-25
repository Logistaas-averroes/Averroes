"""PR-ADS-178B — the one Pipeline page, on PostgreSQL, through the real builders.

Every database case seeds the real tables through the production writers (the
PR-ADS-176A / 177A fixtures: campaign spend, the canonical deal ledger and its
sync state, the canonical contact funnel and its sync state; the PR-ADS-168A
company-association writer) and calls the real builders: ``build_pipeline``,
``build_dashboard_deals``, the mart deal view, the cross-page parity audit, the
reader-cutover audit and the marketing-outcome-cohort audit. The UI case
renders the real payload with the real ``static/app.js`` in ``node``.

Owner decisions 2026-11-25 (``docs/61`` §3): business windows through today;
SQLs = the acquisition cohort; the funnel = lifecycle stage entries ("Entered
stage in window"); lead filters apply to leads only and blank no deal figure;
customers won is unavailable under a lead filter; a list only when it numbers
its published count; Dashboard → Deals reads the same builder.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, live_db,
)
from tests.test_pr_ads_167a_settled_coverage_consumers_pg_integration import (  # noqa: E402
    _closed_today_instant, _window_dates,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import WINDOW  # noqa: E402
from tests.test_pr_ads_177a_one_sql_count_pg_integration import (  # noqa: E402
    _funnel_bootstrap_running, _ready, _yesterday_at,
)


def _json(payload):
    return json.loads(json.dumps(payload, default=str))


def _pipeline(**kw):
    from services.pipeline_service import build_pipeline
    return _json(build_pipeline(WINDOW, **kw))


def _seed(cluster, *, companies=True):
    """The 177A fixture (one Google Ads deal ``d-uk1`` yesterday, the cohort
    of 5 leads / 4 SQLs, legacy leads, spend), plus an ORGANIC deal closed
    yesterday and a Google Ads deal closed TODAY. All three deals belong to ONE
    company: three deals, one customer — and one customer in each of two
    source groups, which do not add up to the total."""
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    _ready(cluster)
    _insert(cluster, "d-org", close_date=_yesterday_at(15).isoformat(), amount=1500.0,
            revenue_usd=1500.0, country="United Arab Emirates", group="organic",
            attribution_status="not_google_ads")
    _insert(cluster, "d-today", close_date=_closed_today_instant().isoformat(),
            amount=500.0, revenue_usd=500.0, country="United Kingdom",
            gclid="g-d-today", campaign="brand - uk", group="google_ads",
            attribution_status="google_ads")
    if companies:
        from tests.test_pr_ads_168a_channel_outcomes_pg_integration import _companies
        _companies({"d-uk1": "co-acme", "d-org": "co-acme", "d-today": "co-acme"})
        from db import writers
        writers.upsert_company_names([{"company_id": "co-acme", "name": "Acme Freight"}],
                                     read_at=datetime.now(timezone.utc))


# ── 1. the builder: one source per figure, two SQL ideas never one word ─────
def test_01_the_pipeline_builder_publishes_the_cohort_the_ledger_and_the_funnel_apart(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    p = _pipeline()
    _, today = _window_dates(WINDOW)
    # Owner decision 1: through today (a deal closed today is on the page).
    assert p["window"]["end_date"] == today.isoformat()
    k = p["kpis"]
    # SQLs: the acquisition cohort (the 177A fixture's 4), with its block.
    assert k["sqls"] == {"value": 4, "status": "published", "reason": None}
    assert (p["cohort"]["sql_status"], p["metric_family"], p["window_basis"]) == (
        "published", "acquisition_cohort_outcomes", "contact_created_at")
    # The funnel: lifecycle stage ENTRIES (c-ae entered SQL yesterday) — a
    # different metric from the cohort's 4, under its own label.
    f = p["funnel"]
    assert (f["label"], f["metric_family"]) == ("Entered stage in window",
                                                "lifecycle_stage_events")
    assert f["events"]["sql"]["count"] == 1 and f["available"] is True
    # Deals, customers, revenue, average: the mart deal view's reader contract.
    assert k["closed_won_deals"] == {"value": 3, "status": "published", "reason": None}
    assert (k["customers"]["value"], k["customers"]["status"]) == (1, "published")
    assert k["won_revenue_usd"]["value"] == pytest.approx(6000.0)
    assert k["average_deal_value_usd"]["value"] == pytest.approx(2000.0)
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = _json(build_revenue_decision_mart(view="deal", window=WINDOW))
    assert k["closed_won_deals"]["value"] == mart["summary"]["closed_won_deals"] \
        == mart["ledger_summary"]["closed_won_deals"]
    assert k["customers"]["value"] == mart["summary"]["customers"]
    assert k["won_revenue_usd"]["value"] == pytest.approx(mart["summary"]["won_revenue_usd"])
    # The deal list: every won deal, newest close first, equal to the count.
    assert [d["deal_id"] for d in p["deals"]] == ["d-today", "d-org", "d-uk1"]
    assert p["deals_status"] == "published" and len(p["deals"]) == k["closed_won_deals"]["value"]
    assert "company" not in p["deals"][0]           # always null on the ledger: dropped
    groups = {g["source_group"]: g for g in p["source_breakdown"]}
    assert (groups["google_ads"]["closed_won_deals"], groups["organic"]["closed_won_deals"]) == (2, 1)
    # One company wins in both groups: customers per group never add up.
    assert groups["google_ads"]["customers"] == groups["organic"]["customers"] == 1
    assert sum(g["customers"] for g in groups.values()) == 2 != k["customers"]["value"]
    # Customers won: one entry per company, exactly the published count.
    cw = p["customers_won"]
    assert cw["status"] == "published" and len(cw["rows"]) == k["customers"]["value"]
    assert cw["rows"][0]["company_name"] == "Acme Freight"
    assert cw["rows"][0]["closed_won_deals"] == 3
    # Provenance for the parity audit; no ROAS / CAC anywhere.
    assert set(p["metric_truth"]) == {"closed_won_revenue_usd", "closed_won_deals",
                                      "customers", "google_ads_cohort_sqls"}
    assert not re.search(r'"(roas|cac)[a-z_]*":\s*[0-9]', json.dumps(p))


# ── 2. lead filters narrow leads only ───────────────────────────────────────
@pytest.mark.parametrize("filters", [{"acquisition_group": "organic"},
                                     {"scope": "google_ads_source"}])
def test_02_a_lead_filter_narrows_the_funnel_and_blanks_no_deal_figure(
        live_db, monkeypatch, filters):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    whole, narrowed = _pipeline(), _pipeline(**filters)
    assert narrowed["filters"]["lead_filtered"] is True
    assert narrowed["funnel"]["acquisition_group"] == filters.get("acquisition_group")
    assert narrowed["funnel"]["scope"] == filters.get("scope", "all_source")
    # Every deal figure and the cohort KPI are the window's, filter or not.
    assert narrowed["kpis"] == whole["kpis"]
    assert narrowed["deals"] == whole["deals"]
    assert narrowed["source_breakdown"] == whole["source_breakdown"]
    # Customers won is the window's all-source won population: the lead
    # filters do not touch it (PR-ADS-178B-F1 m1).
    assert narrowed["customers_won"] == whole["customers_won"]
    assert whole["customers_won"]["status"] == "published"
    assert len(whole["customers_won"]["rows"]) == whole["kpis"]["customers"]["value"]
    with pytest.raises(ValueError):
        _pipeline(acquisition_group="not_a_group")


# ── 3. withheld means absent, with the reason ───────────────────────────────
def test_03_withheld_customers_and_a_stale_ledger_leave_no_figure_and_no_list(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db, companies=False)          # no company was ever observed
    p = _pipeline()
    c = p["kpis"]["customers"]
    assert c["value"] is None and c["status"] in ("withheld", "unavailable") and c["reason"]
    assert p["customers_won"] == {"status": c["status"], "reason": c["reason"]}
    assert "rows" not in p["customers_won"]
    assert p["deals_status"] == "published" and len(p["deals"]) == 3   # deals stand
    # A stale deal ledger (PR-ADS-161F): deals, customers and revenue withheld.
    from tests.test_pr_ads_153e_b_pg_integration import _sync_state
    _sync_state(live_db, incremental=(datetime.now(timezone.utc) - timedelta(hours=72)).isoformat())
    s = _pipeline()
    for key in ("closed_won_deals", "customers", "won_revenue_usd", "average_deal_value_usd"):
        assert s["kpis"][key]["value"] is None, key
        assert s["kpis"][key]["reason"] == "canonical_ledger_stale", key
    assert s["deals"] is None and s["source_breakdown"] is None
    held = s["kpis"]["closed_won_deals"]["status"]
    assert held in ("withheld", "unavailable")
    assert (s["deals_status"], s["deals_reason"]) == (held, "canonical_ledger_stale")
    assert s["kpis"]["sqls"]["value"] == 4            # the cohort is not the ledger
    # The audit's leak walk finds nothing on the withheld page.
    from scripts import audit_customer_revenue_reader_cutover as rc
    a = rc.Audit()
    pub = {m: {"status": held, "reason": "canonical_ledger_stale"}
           for m in ("closed_won_deals", "customers", "revenue_usd")}
    rc.page_leak_checks(a, WINDOW, "pipeline", pub, s, None)
    assert a.violations == []
    html = _render(s)
    assert "Deal d-uk1" not in html and "This does not mean that no deals closed" in html


# ── 4. Dashboard → Deals reads the same builder ─────────────────────────────
def test_04_dashboard_deals_republishes_the_pipeline_builder_and_reads_no_legacy_backlog(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from db import revenue_repository as repo
    assert not hasattr(repo, "fetch_sql_lead_details")
    from services.dashboard_deals_service import build_dashboard_deals
    dash = _json(build_dashboard_deals(WINDOW))
    p = _pipeline()
    for key in ("kpis", "deals", "customers_won", "source_breakdown", "cohort", "window",
                "closed_won_population", "funnel"):
        assert dash[key] == p[key], key
    # The provenance is the same contract (its build instant aside).
    strip = lambda mt: {m: {k: v for k, v in c.items() if "generated" not in k and k != "as_of"}
                        for m, c in mt.items()}
    assert strip(dash["metric_truth"]) == strip(p["metric_truth"])
    assert dash["surface"] == "dashboard_deals"
    pc = dash["period_change"]
    assert set(pc["metrics"]) == {"sqls", "closed_won_deals", "closed_won_customers",
                                  "closed_won_revenue_usd"}
    for gone in ("lead_status_backlog", "sql_no_deal", "sql_no_deal_total",
                 "campaign_breakdown", "aging_buckets", "decision_cards", "stage_mix"):
        assert gone not in dash, gone
    assert "lead_status_qualified_no_won_deal" not in json.dumps(dash)


# ── 5. parity: the Pipeline page joins the all-source identities ────────────
def test_05_parity_the_pipeline_page_and_dashboard_deals_are_one_identity_with_the_mart(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from services import cross_page_parity_service as parity
    report = parity.audit_window(WINDOW)
    by = {m["metric"]: m for m in report["metrics"]}
    for key, want in (("closed_won_deals", 3), ("customers", 1),
                      ("google_ads_cohort_sqls", 4)):
        consumers = {r["consumer"] for r in by[key]["readings"]}
        assert {"pipeline", "dashboard/deals"} <= consumers, key
        assert by[key]["status"] == "identical", (key, by[key]["status"])
        assert by[key]["value"] == want, key
    assert by["closed_won_revenue_usd"]["status"] == "identical"
    assert "value_mismatch" not in report["violation_codes"]


# ── 6. the reader-cutover audit builds and reconciles the Pipeline page ─────
def test_06_the_reader_cutover_audit_walks_and_reconciles_the_pipeline_payload(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from scripts import audit_customer_revenue_reader_cutover as rc
    assert "pipeline" in dict(rc._page_builders())
    a, report = rc.run()
    assert not [v for v in a.violations if "pipeline" in v], a.violations
    assert not [n for n in report["pages_not_run"] if n.get("page") == "pipeline"]
    assert a.checks.get("windows_reconcile") is True
    # The reconciliation bites: a pipeline payload whose deal list misses a
    # member is a windows_reconcile violation.
    from services import canonical_customer_revenue_service as svc
    truth = svc.get_closed_won_truth([("business", WINDOW)])
    tw = truth["windows"][0]
    p = _pipeline()
    p["deals"] = p["deals"][:-1]
    b = rc.Audit()
    rc.pipeline_checks(b, WINDOW, "pipeline", tw.get("publication") or {}, tw, p)
    assert any("deal list" in v for v in b.violations), b.violations


# ── 7. the marketing-outcome-cohort audit covers the Pipeline payload ───────
def test_07_the_cohort_audit_finds_no_withheld_sql_on_the_pipeline_page(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from scripts import audit_marketing_outcome_cohorts as mo
    assert "pipeline" in mo._build_mart_payloads(WINDOW, None)
    _funnel_bootstrap_running()
    held = _pipeline()
    assert held["cohort"]["sql_status"] == "withheld"
    assert held["kpis"]["sqls"]["value"] is None
    assert held["kpis"]["sqls"]["reason"] == held["cohort"]["sql_reason"]
    assert mo.mart_withheld_exposures(held) == []
    a = mo.Audit()
    mo.audit_mart_window(a, window=WINDOW, payloads={"pipeline": held})
    assert not [c for c in a.checks if not c["ok"]], a.checks
    # A leak is caught: a value beside the withheld verdict.
    leak = {**held, "kpis": {**held["kpis"], "sqls": {"value": 4, "status": "withheld",
                                                      "reason": "x"}}}
    assert mo.mart_withheld_exposures(leak) == ["kpis.sqls.value"]


# ── 8. the route ────────────────────────────────────────────────────────────
def test_08_the_route_is_read_only_authenticated_and_400s_a_bad_filter(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from fastapi.testclient import TestClient
    from api.auth import set_session
    from api.server import app
    from starlette.responses import Response as StarletteResponse
    routes = [r for r in app.routes if getattr(r, "path", "") == "/api/pipeline"]
    assert len(routes) == 1 and routes[0].methods == {"GET"}
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(f"/api/pipeline?window={WINDOW}").status_code == 401
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    r = StarletteResponse()
    set_session(r, "viewer", "viewer")
    cookie = next(p.split("=", 1)[1] for p in r.headers.get("set-cookie", "").split(";")
                  if p.strip().startswith("ads_session="))
    client.cookies.set("ads_session", cookie)
    ok = client.get(f"/api/pipeline?window={WINDOW}&acquisition_group=organic")
    assert ok.status_code == 200
    assert ok.json()["filters"]["acquisition_group"] == "organic"
    assert ok.json()["kpis"]["closed_won_deals"]["value"] == 3
    assert client.get("/api/pipeline?window=30d").status_code == 400
    assert client.get(f"/api/pipeline?window={WINDOW}&scope=bogus").status_code == 400
    assert client.post(f"/api/pipeline?window={WINDOW}").status_code == 405


# ── 9. the real payload on the real page ────────────────────────────────────
def _render(payload):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    out = _run_app('_currentUser = {"username": "u", "role": "viewer"};'
                   "pipelineData = %s; pipelineStatus = 'ok'; renderPipelinePage();"
                   '__els["pipeline-summary"].innerHTML + __els["pipeline-outcomes"].innerHTML'
                   % json.dumps(payload))
    return out


def test_09_the_real_payload_renders_its_figures_lists_and_footer(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    html = _render(_pipeline())
    assert re.search(r'data-kpi="sqls">.*?>4', html, re.DOTALL)
    assert re.search(r'data-kpi="deals">.*?>3<', html, re.DOTALL)
    assert re.search(r'data-kpi="customers">.*?>1<', html, re.DOTALL)
    assert html.count('data-pipeline-deal="') == 3 and "Acme Freight" in html
    assert "Entered stage in window" in html
    assert html.count("dash-truth-footer") == 1


# ── 10. pure: the list guard and the average (no database) ──────────────────
def test_10_the_builder_withholds_a_list_that_does_not_number_its_count():
    from services import pipeline_service as ps
    pub = {"value": 2, "status": "published", "reason": None}
    rows = [{"deal_id": "a", "deal_close_date": "2026-11-01"}]
    assert ps._deal_list(rows, pub, pub) == (
        None, "unavailable", "closed_won_rows_do_not_match_count")
    two = rows + [{"deal_id": "b", "deal_close_date": "2026-11-02"}]
    assert ps._deal_list(two, pub, pub)[0][0]["deal_id"] == "b"
    # The deal view's own population disagreeing with the card: withheld.
    three = {"value": 3, "status": "published", "reason": None}
    assert ps._deal_list(two, pub, three)[0] is None
    held = {"value": None, "status": "withheld", "reason": "canonical_ledger_stale"}
    assert ps._deal_list(rows, held, pub) == (None, "withheld", "canonical_ledger_stale")
    zero = {"value": 0, "status": "published", "reason": None}
    assert ps._average_deal_value({"average_deal_value": None}, zero, pub) == {
        "value": None, "status": "unavailable", "reason": "no_closed_won_deals_in_window"}
    assert ps._average_deal_value({}, pub, held)["reason"] == "canonical_ledger_stale"
    # A published verdict with no number is not a publishable zero.
    assert ps._verdict({"value": None, "status": "published"})["status"] == "unavailable"
