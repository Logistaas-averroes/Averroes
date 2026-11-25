"""PR-ADS-177A — one SQL count: the acquisition cohort everywhere.

Owner decision 2026-11-24: every page shows the same SQLs — the stricter
acquisition cohort Campaign Evidence already publishes — accepting that they
are withheld when the contact sync is incomplete. Before this PR the Revenue
Decision Mart, and every page that republishes it (Dashboard Overview,
Campaigns, Countries, Revenue, Deals; the Countries page; Campaign Revenue &
Spend), counted the legacy ``leads.status_category = 'qualified'`` population.

These cases need no database: the pure cohort partition, the mart's row
finaliser and verdicts, the Dashboard statuses, the JSON fallback, the audit's
exposure scan, the UI gate in the real ``static/app.js`` (node) and the
static inventory. The PostgreSQL cases that drive the real builders end to end
are in ``test_pr_ads_177a_one_sql_count_pg_integration.py``.

Every case fails on ``dce66da`` (PR-ADS-176C): the names it imports or the
fields it reads do not exist there, or the behaviour it pins is the defect.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_JS = ROOT / "static" / "app.js"

INSIDE = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _contact(cid, *, stage="lead", label="Brand - UK", source="PAID_SEARCH",
             ip_country=None, country=None, created=INSIDE):
    return {"funnel_row_id": hash(cid) % 10_000, "contact_id": cid, "created_at": created,
            "lifecycle_stage": stage, "sql_entered_direct": None,
            "sql_entered_recovered": None, "hs_analytics_source": source,
            "hs_analytics_source_data_1": label, "gclid": None,
            "ip_country": ip_country, "country": country}


def _resolver(campaigns=("Brand - UK",), excluded=()):
    from services import revenue_attribution_service as ras
    rmap = {ras._ident_norm(c): c for c in campaigns}
    return ras._mart_label_resolver(rmap, {ras._ident_norm(e) for e in excluded})


def _country_key(row):
    from analysis import country_identity
    from services import marketing_outcome_cohort_service as cs
    return country_identity.country_key(name=cs.contact_country(row))


# ── 1. the cohort partitions Google Ads contacts by a second key ─────────────
def test_01_a_second_partition_conserves_the_google_ads_cohort():
    """Every Google Ads contact lands in exactly one country group; non-Google
    contacts in none. Σ groups = google_ads, on contacts, SQLs and undated SQLs."""
    from services import marketing_outcome_cohort_service as cs
    rows = [_contact("a", stage="customer", ip_country="United Kingdom"),
            _contact("b", stage="lead", country="Jordan"),
            _contact("c", stage="salesqualifiedlead", country="Atlantis"),
            _contact("d", stage="customer", source="ORGANIC_SEARCH", ip_country="Jordan"),
            _contact("e", stage="opportunity", label="", ip_country="Jordan")]
    cohort = cs.build_cohort(rows, resolve_label=_resolver(), start_at=START,
                             end_before=END, group_key=_country_key)
    g = cohort["by_group"]
    assert set(g) == {"code:GB", "code:JO", "unknown"}
    assert g["code:JO"]["contacts_acquired"] == 2 and g["code:JO"]["sqls"] == 1
    assert g["unknown"]["sqls"] == 1                       # Atlantis: no listed market
    for f in ("contacts_acquired", "sqls", "sqls_missing_event_timestamp"):
        assert sum(s[f] for s in g.values()) == cohort["google_ads"][f]
    assert cs.reconcile_cohort(cohort) == []


def test_01b_ip_country_wins_over_the_contact_country_the_products_one_precedence():
    from services import marketing_outcome_cohort_service as cs
    assert cs.contact_country({"ip_country": "Jordan", "country": "Kuwait"}) == "Jordan"
    assert cs.contact_country({"ip_country": "", "country": "Kuwait"}) == "Kuwait"
    assert cs.contact_country({}) is None


def test_01c_duplicate_rows_that_disagree_on_country_are_placed_in_the_residual():
    from services import marketing_outcome_cohort_service as cs
    rows = [_contact("a", ip_country="Jordan"), {**_contact("a", ip_country="Kuwait"),
                                                  "funnel_row_id": 9}]
    cohort = cs.build_cohort(rows, resolve_label=_resolver(), start_at=START,
                             end_before=END, group_key=_country_key)
    assert cohort["by_group"] == {"unknown": {"display_name": None, "contacts_acquired": 1,
                                              "sqls": 0, "sqls_missing_event_timestamp": 0}}


def test_01d_mutation_a_partition_that_drops_a_contact_fails_reconciliation():
    from services import marketing_outcome_cohort_service as cs
    rows = [_contact("a", stage="customer", ip_country="United Kingdom"),
            _contact("b", stage="customer", country="Jordan")]
    cohort = cs.build_cohort(rows, resolve_label=_resolver(), start_at=START,
                             end_before=END, group_key=_country_key)
    del cohort["by_group"]["code:JO"]
    assert any("groups" in p for p in cs.reconcile_cohort(cohort))


# ── 2. the mart's placement is the mart's own key ───────────────────────────
def test_02_the_mart_resolver_keys_on_the_canonical_campaign_name_and_excludes_not_google_labels():
    resolve = _resolver(campaigns=("Brand - UK",), excluded=("Offline Import",))
    assert resolve("brand  -  uk") == ("google_ads", "Brand - UK")
    assert resolve("Offline Import") == ("not_google_ads", None)
    assert resolve("Mystery") == ("unmatched", "Mystery")


def test_02b_campaign_slots_are_the_rows_the_mart_can_form_and_the_rest_is_the_residual():
    from services import marketing_outcome_cohort_service as cs
    from services import revenue_attribution_service as ras
    rows = [_contact("a", stage="customer"),                     # Brand - UK
            _contact("b", stage="customer", label="Mystery"),    # its own unmapped row
            _contact("c", stage="customer", label=""),           # no label: residual
            _contact("d", stage="customer", label="Offline Import")]  # excluded
    cohort = cs.build_cohort(rows, resolve_label=_resolver(excluded=("Offline Import",)),
                             start_at=START, end_before=END)
    outcomes = {"cohort": cohort, "sql_publication": ("published", None),
                "freshness": {}, "missing_created_at": 0}
    campaign_slots, _ = ras._cohort_slots(outcomes)
    assert {k: v["sqls"] for k, v in campaign_slots.items()} == {"Brand - UK": 1, "Mystery": 1}
    block = ras._cohort_contract_block(outcomes, campaign_slots)
    assert block["totals"]["sqls"] == 3                          # a, b, c — not d
    assert block["campaign_residual"]["sqls"] == 1               # c
    assert block["metric_family"] == "acquisition_cohort_outcomes"
    assert block["window_basis"] == "contact_created_at"


def test_02c_a_withheld_cohort_block_carries_no_count():
    from services import revenue_attribution_service as ras
    block = ras._cohort_contract_block(
        {"cohort": None, "sql_publication": ("withheld", "source_bootstrap_incomplete"),
         "freshness": {}, "missing_created_at": None}, {})
    assert (block["sql_status"], block["sql_reason"]) == ("withheld",
                                                          "source_bootstrap_incomplete")
    assert all(v is None for v in block["totals"].values())
    assert block["campaign_residual"]["sqls"] is None
    assert block["campaign_residual"]["leads"] is None


# ── 3. a verdict is never computed on a withheld SQL count ───────────────────
def _bucket(spend, sqls=3, deals=0, revenue=0.0):
    from services import revenue_attribution_service as ras
    b = ras._new_bucket("x")
    b.update(spend=spend, leads=10, sqls=sqls, closed_won_deals=deals, won_revenue=revenue)
    return b


def test_03_a_spending_row_with_withheld_sqls_is_not_judged_waste_or_watch():
    """The PR-ADS-176B-F1 M1 pattern: a verdict that would differ between 0 and
    some SQLs rests on the count, so it is `sqls_unknown` with the reason."""
    from services import revenue_attribution_service as ras
    row = ras._finalize_row(_bucket(500.0, sqls=0),
                            cohort_verdict=("withheld", "source_bootstrap_incomplete"))
    assert row["sqls"] is None and row["leads"] is None
    assert row["verdict"] == ras.VERDICT_SQLS_UNKNOWN == "sqls_unknown"
    assert row["verdict_reason"] == "source_bootstrap_incomplete"
    assert (row["sqls_status"], row["sqls_reason"]) == ("withheld",
                                                        "source_bootstrap_incomplete")
    # Positive control: the same row under a published verdict IS judged.
    pub = ras._finalize_row(_bucket(500.0, sqls=0), cohort_verdict=("published", None))
    assert pub["verdict"] == "waste" and pub["sqls"] == 0


def test_03b_a_verdict_that_does_not_depend_on_sqls_is_kept():
    """Low spend, no revenue: "learning" whatever the SQLs are — not unknown."""
    from services import revenue_attribution_service as ras
    row = ras._finalize_row(_bucket(10.0), cohort_verdict=("withheld", "x"))
    assert row["verdict"] == "learning" and row["verdict_reason"] is None


def test_03c_no_sql_note_is_written_beside_a_withheld_count():
    from services import revenue_attribution_service as ras
    row = ras._finalize_row(_bucket(500.0, sqls=4), cohort_verdict=("withheld", "x"))
    assert not any("SQLs present" in n for n in row["attribution_notes"])
    assert any("withheld" in n for n in row["attribution_notes"])


@pytest.mark.parametrize("module,fn,kwargs", [
    ("services.dashboard_campaigns_service", "_campaign_status",
     dict(native_spend=100.0, usd_spend=125.0, fx_complete=True, sqls=None, deals=0,
          won_revenue=0.0, spend_state="mapped_exact", amount_unknown=False)),
    ("services.dashboard_countries_service", "_country_status",
     dict(native_spend=100.0, usd_spend=125.0, fx_complete=True, sqls=None,
          customers=0, won_revenue=0.0, attribution_status="mapped")),
    ("services.dashboard_deals_service", "_source_status", dict(sqls=None, deals=0, won=0.0)),
])
def test_03d_every_dashboard_status_says_sqls_withheld_never_a_claim(module, fn, kwargs):
    import importlib
    mod = importlib.import_module(module)
    status = getattr(mod, fn)(**kwargs)
    assert status == "SQLs withheld"
    assert getattr(mod, fn)(**{**kwargs, "sqls": 0}) != "SQLs withheld"


def test_03e_a_won_deal_still_decides_the_status_under_withheld_sqls():
    from services import dashboard_countries_service as dc
    assert dc._country_status(native_spend=1.0, usd_spend=1.0, fx_complete=True, sqls=None,
                              customers=2, won_revenue=900.0,
                              attribution_status="mapped") == dc.STATUS_REVENUE_PROVEN


def test_03f_the_deals_campaign_breakdown_keeps_a_withheld_row_instead_of_dropping_it():
    """`(sqls or 0) == 0` skipped a row as noise on a count nobody published."""
    from services import dashboard_deals_service as dd
    out = dd._build_campaign_breakdown(
        [{"campaign_name": "Brand", "sqls": None, "sqls_status": "withheld",
          "closed_won_deals": 0, "won_revenue": 0.0}], True, set())
    assert [r["campaign_name"] for r in out] == ["Brand"]
    assert out[0]["status"] == "SQLs withheld"


# ── 4. the JSON fallback publishes no lead or SQL count ─────────────────────
def test_04_the_db_down_fallback_carries_an_unavailable_cohort_and_no_count():
    from services import revenue_attribution_service as ras
    contract = {"summary": {"leads": 7, "sqls": 3},
                "campaigns": [{"campaign_name": "a", "leads": 5, "sqls": 2,
                               "closed_won_deals": None, "verdict": "watch"}],
                "countries": [{"country": "UK", "leads": 1, "sqls": 1,
                               "closed_won_deals": None, "verdict": "watch"}]}
    out = ras._withhold_json_cohort(contract)
    assert out["summary"]["leads"] is None and out["summary"]["sqls"] is None
    for r in out["campaigns"] + out["countries"]:
        assert r["leads"] is None and r["sqls"] is None
        assert r["verdict"] == "outcome_withheld"
    assert (out["cohort"]["sql_status"], out["cohort"]["sql_reason"]) == (
        "unavailable", "canonical_funnel_unreadable")
    import ast
    tree = ast.parse((ROOT / "services" / "revenue_attribution_service.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "QUALIFIED" not in names                           # the mql_status read is gone


# ── 5. the audit's exposure scan covers the mart and the Dashboards ──────────
def _withheld_payload(**extra):
    return {"cohort": {"sql_status": "withheld", "sql_reason": "source_bootstrap_incomplete",
                       "totals": {"leads": None, "sqls": None},
                       "campaign_residual": {"leads": None, "sqls": None},
                       "metadata": {}},
            "summary": {"leads": None, "sqls": None}, **extra}


@pytest.mark.parametrize("leak,where", [
    ({"summary": {"leads": None, "sqls": 4}}, "summary.sqls"),
    ({"rows": [{"campaign_name": "a", "leads": None, "sqls": 0}]}, "rows[a].sqls"),
    ({"rows": [{"country": "UK", "leads": 2, "sqls": None}]}, "rows[UK].leads"),
    ({"country_totals": {"sqls": 0}}, "country_totals.sqls"),
    ({"regional_mix": [{"region": "Europe", "sqls": 1}]}, "regional_mix[0].sqls"),
    ({"kpis": {"sqls_residual": 0}}, "kpis.sqls_residual"),
    ({"kpis": {"google_ads_cohort_sqls": 9}}, "kpis.google_ads_cohort_sqls"),
    ({"residual": {"sqls": 0}}, "residual.sqls"),
    ({"countries": [{"country_name": "UK", "sqls": 3}]}, "countries[UK].sqls"),
    ({"campaign_breakdown": [{"campaign_name": "b", "sqls": 1}]}, "campaign_breakdown[b].sqls"),
    ({"sql_reconciliation": {"consumer_count": 5}}, "sql_reconciliation.consumer_count"),
])
def test_05_the_audit_flags_every_count_beside_a_withheld_cohort(leak, where):
    from scripts import audit_marketing_outcome_cohorts as audit
    assert audit.mart_withheld_exposures(_withheld_payload()) == []
    payload = _withheld_payload(**leak)
    found = audit.mart_withheld_exposures(payload)
    assert any(f.startswith(where.split("[")[0]) for f in found), found


def test_05b_the_in_residual_marker_is_not_a_count():
    from scripts import audit_marketing_outcome_cohorts as audit
    payload = _withheld_payload(rows=[{"country": "res", "leads": None, "sqls": None,
                                       "outside_listed_market_items": [
                                           {"country_code": "FJ", "leads": "in_residual",
                                            "sqls": "in_residual"}]}])
    assert audit.mart_withheld_exposures(payload) == []


def test_05c_conservation_is_checked_under_a_published_cohort():
    from scripts import audit_marketing_outcome_cohorts as audit
    ok = {"cohort": {"sql_status": "published", "totals": {"sqls": 3, "leads": 5},
                     "campaign_residual": {"sqls": 1, "leads": 1}},
          "summary": {"sqls": 3},
          "rows": [{"sqls": 2, "leads": 4}]}
    assert audit.mart_conservation_problems(ok, view="campaign") == []
    broken = {**ok, "rows": [{"sqls": 1, "leads": 4}]}
    assert audit.mart_conservation_problems(broken, view="campaign")


# ── 6. the UI: the cohort is an ARGUMENT, the word and the reason ────────────
def _run(script):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(script)


WITHHELD = {"sql_status": "withheld", "sql_reason": "source_bootstrap_incomplete"}
PUBLISHED = {"sql_status": "published", "sql_reason": None}


def test_06_a_withheld_cohort_renders_the_word_and_the_reason_never_the_number():
    out = _run("[cohortCountHtml(41, %s, null), cohortCountHtml(41, %s, null), "
               "cohortCountHtml(0, %s, null)]"
               % (json.dumps(WITHHELD), json.dumps(PUBLISHED), json.dumps(WITHHELD)))
    assert "Withheld" in out[0] and "41" not in out[0]
    assert "backfill is still running" in out[0]               # the reason, in words
    assert out[1] == "41"                                      # positive control
    assert "0" not in out[2].replace("0x", "")                 # never a published zero


def test_06b_no_cohort_argument_is_unavailable_whatever_the_page_state_says():
    """Fail closed: page-global state never authorises a cell."""
    out = _run("roasCountryMart = {cohort: %s}; dashCountries = {cohort: %s}; "
               "[cohortCountHtml(12, undefined, null), cohortCountHtml(12, null, null)]"
               % (json.dumps(PUBLISHED), json.dumps(PUBLISHED)))
    assert all("Unavailable" in h and "12" not in h for h in out)


def test_06c_a_row_can_narrow_a_published_cohort_never_widen_a_withheld_one():
    out = _run("[cohortCountHtml(5, %s, {sqls_status: 'unavailable', "
               "sqls_reason: 'geo_coverage_incomplete'}), "
               "cohortCountHtml(5, %s, {sqls_status: 'published'})]"
               % (json.dumps(PUBLISHED), json.dumps(WITHHELD)))
    assert "Unavailable" in out[0] and ">5<" not in out[0]
    assert "Withheld" in out[1] and ">5<" not in out[1]


def test_06d_labels_say_what_the_count_is():
    js = APP_JS.read_text()
    assert 'const COHORT_SQLS_LABEL = "SQLs · contacts acquired in window";' in js
    assert 'const COHORT_LEADS_LABEL = "Leads · contacts acquired in window";' in js


def test_06e_every_mart_and_dashboard_sql_cell_goes_through_the_gate():
    """No mart / Dashboard renderer prints a leads or SQLs value with
    `dashValue` or `fmtCount` any more — each goes through cohortCountHtml
    with the cohort of the response it renders."""
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _js_function
    js = APP_JS.read_text()
    for fn in ("renderRoasCountryTable", "renderCountryDrawer", "renderCountriesResidualPanel",
               "renderRoasCampaignTable", "renderMartSummaryStrip", "renderCampLeaderboard",
               "renderCtryLeaderboard", "renderCtryResidualPanel", "renderDealCampaignPipeline"):
        body = _js_function(js, fn)
        for bad in ("dashValue(r.sqls", "dashValue(r.leads", "fmtCount(r.sqls)",
                    "fmtCount(r.leads)", "dashValue(res.sqls", "fmtCount(s.sqls)",
                    "fmtCount(s.leads)"):
            assert bad not in body, (fn, bad)
        assert "cohortCountHtml(" in body, fn


def test_06f_the_new_statuses_have_a_colour_and_a_next_action():
    js = APP_JS.read_text()
    for table in ("CAMP_STATUS_META", "CTRY_STATUS_META", "DEAL_STATUS_META",
                  "CAMP_NEXT_ACTION", "CTRY_NEXT_ACTION"):
        start = js.index(f"const {table} = {{")
        assert '"SQLs withheld"' in js[start:js.index("};", start)], table


def test_06g_the_verdict_badge_names_sqls_unknown():
    out = _run("getBusinessVerdictBadge('sqls_unknown')")
    assert "Not judged" in out and "SQLs withheld" in out


def test_06h_a_withheld_cohort_blocks_no_page():
    """Revenue decision readiness no longer waits on the leads: a withheld
    cohort withholds the figures, it does not block the page."""
    out = _run("[isRevenueDecisionReady({canonical_revenue_status: 'canonical_available', "
               "lead_metrics_status: 'withheld'}), isRevenueDecisionReady("
               "{canonical_revenue_status: 'canonical_unavailable'})]")
    assert out == [True, False]


# ── 7. the legacy path is gone from the mart; the inventory knows ────────────
def test_07_the_mart_reads_no_legacy_lead_status_count():
    import ast
    src = (ROOT / "services" / "revenue_attribution_service.py").read_text()
    calls = [n.func.attr for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "fetch_lead_quality" not in calls
    literals = {n.value for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "status_category" not in literals and "qualified" not in literals


def test_07b_the_registry_classifies_the_mart_and_its_pages_as_the_cohort():
    from analysis import sql_doctrine_registry as reg
    by = {c["consumer"]: c for c in reg.CONSUMERS}
    for name in ("Revenue Decision Mart summary.sqls / row sqls",
                 "Revenue attribution service (Campaign Revenue & Spend / Country, verdicts)",
                 "Dashboard Campaigns", "Dashboard Countries"):
        assert by[name]["classification"] == "canonical_lifecycle_active", name
        assert by[name]["sql_definition"] == reg.COHORT_DEF, name
    assert not any(c["id"] == "verdict.withheld_sql_becomes_zero"
                   for c in reg.KNOWN_CONTRACT_CONFLICTS)


def test_07c_the_static_inventory_has_no_unknown_occurrence():
    from scripts import audit_sql_doctrine_inventory as inv
    static = inv.build_static(ROOT)
    assert static["unclassified_occurrences"] == [], static["unclassified_occurrences"][:5]
    assert static["registry_problems"] == []


def test_05d_a_window_with_no_settled_day_publishes_no_cohort_count():
    """The first day of a quarter: the country view measured no day. A cohort
    over no day read zero contacts, which is not a published zero — every
    count, the bucket counts included, is None with the window's reason."""
    from scripts import audit_marketing_outcome_cohorts as audit
    from services import revenue_attribution_service as ras
    from services import revenue_decision_mart as mart
    from services import marketing_outcome_cohort_service as cs
    cohort = cs.build_cohort([], resolve_label=_resolver(), start_at=START, end_before=END)
    block = ras._cohort_contract_block(
        {"cohort": cohort, "sql_publication": ("published", None), "freshness": {},
         "missing_created_at": 0}, {})
    assert block["totals"]["sqls"] == 0 and block["metadata"]["mapped_count"] == 0
    out = mart._withhold_cohort(block, "no_settled_day_in_window")
    assert (out["sql_status"], out["sql_reason"]) == ("unavailable", "no_settled_day_in_window")
    assert audit.mart_withheld_exposures({"cohort": out}) == []
