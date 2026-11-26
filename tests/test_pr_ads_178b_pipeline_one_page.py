"""PR-ADS-178B — one Pipeline page: leads and deals together.

The Leads page (``leads``) and the Closed-Won Deals page (``deals``) were two
pages under the Pipeline menu item, and Dashboard → Deals a third surface with
its own composition. Owner decisions 2026-11-25 (``docs/61`` §3): one page,
``pipeline``, in the Dashboard design, on ONE builder (``build_pipeline``);
SQLs = the acquisition cohort through ``cohortCountHtml`` on the response's own
cohort; the funnel labelled "Entered stage in window"; the leads list opens on a
stage click; the legacy "Qualified (lead status)" backlog dropped; the deal list
with a Source column and a filter on the deal's own source group; Dashboard →
Deals reads the same builder.

Every case runs the real ``static/app.js`` in ``node`` (the harness of
``tests/test_pr_ads_161b_marketing_outcome_cohorts``) or reads the real
``static/index.html`` / service sources. Fail-before evidence: ``docs/61`` §7.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

BUSINESS_WINDOWS = ["current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"]
#: A figure no legitimate rendering of the fixture produces.
SENTINEL = 7919
#: PR-ADS-182A-F1 (m3): the sentinel in ANY rendering ("7919", "7,919",
#: "7.919", "7 919", "$7,919.00") — a plain substring check could not fail
#: once the page formats the figure.
_SENTINEL_ANY = re.compile(r"7[,.\s\u202f\u00a0]?919")


def _run(script, *, js=None):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(script, js=js)


def _section(page_id):
    m = re.search(r'<section id="%s".*?\n      </section>' % re.escape(page_id), HTML, re.DOTALL)
    assert m, page_id
    return m.group(0)


def _fn(name):
    m = re.search(r"\n(?:async )?function %s\(" % re.escape(name), JS)
    assert m, f"function {name} not found"
    j = JS.find("\n}\n", m.start())
    return JS[m.start():j + 2]


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# ── fixtures: the builder's own shape ─────────────────────────────────────────
def _m(value, status="published", reason=None, **extra):
    return {"value": value if status == "published" else None, "status": status,
            "reason": None if status == "published" else reason, **extra}


_COHORT = {"sql_status": "published", "sql_reason": None,
           "metric_family": "acquisition_cohort_outcomes", "window_basis": "contact_created_at",
           "window": {"start_date": "2026-10-01", "end_date": "2026-11-25"},
           # PR-ADS-182A: the cohort's all-sources slot — the Pipeline funnel.
           "contacts_without_created_at": 0,
           "breakdown": {"all_sources": {
               "contacts_acquired": 120, "reached_mql": 40, "sqls": 11,
               "sqls_missing_event_timestamp": 0, "reached_opportunity": 6,
               "reached_customer_stage": 3}}}


def _funnel_block(cohort=None, scope="all_source", group=None):
    """PR-ADS-182A: the Pipeline funnel as the builder publishes it — the REAL
    block producer (``pipeline_service._funnel``) over the response's cohort."""
    from services import pipeline_service as ps
    return json.loads(json.dumps(ps._funnel(cohort or _COHORT, scope, group), default=str))
_WINDOW = {"key": "current_quarter", "label": "Current Quarter", "start_date": "2026-10-01",
           "end_date": "2026-11-25", "is_closed_window": False, "through_reason": None}


def _deal(i, group, close, amount, campaign=None, country="United Kingdom"):
    return {"deal_id": f"d-{i}", "deal_name": f"Deal {i}", "deal_close_date": close,
            "deal_amount_usd": amount, "currency_status": "verified_usd",
            "deal_stage_label": "Deal Won", "acquisition_group": group,
            "campaign_name": campaign, "country": country,
            "attribution_status": "google_ads" if group == "google_ads" else "not_google_ads",
            "attribution_scope": "all_source"}


_DEALS = [_deal(1, "google_ads", "2026-11-20T10:00:00+00:00", 4000.0, "brand - uk"),
          _deal(2, "organic", "2026-11-24T09:00:00+00:00", 1500.0),
          _deal(3, "google_ads", "2026-11-24T09:00:00+00:00", 2500.0, "gulf search"),
          _deal(4, None, "2026-10-02T09:00:00+00:00", 1000.0)]


def _breakdown(deals):
    """``source_breakdown`` in the builder's shape (``_source_breakdown``): the
    deal list partitioned by each deal's own group, each part's figures as
    reader-contract verdicts. PR-ADS-178C: the deal-source filter reads its
    per-group counts from here (m6), so the fixture carries it whenever the
    builder does — beside a published deal list."""
    groups = {}
    for d in deals:
        groups.setdefault(d["acquisition_group"] or "not_recorded", []).append(d)
    out = []
    for g, members in groups.items():
        n, rev = len(members), sum(d["deal_amount_usd"] or 0.0 for d in members)
        out.append({"source_group": g, "closed_won_deals": n, "customers": 1,
                    "won_revenue": rev, "customers_additive": False,
                    "metrics": {"closed_won_deals": _m(n), "customers": _m(1),
                                "revenue_usd": _m(rev)}})
    return out


def _payload(*, deals=None, deals_status="published", deals_reason=None, cohort=None,
             kpis=None, customers_won=None, funnel=None, population=None):
    deals = list(_DEALS) if deals is None else deals
    n = len(deals)
    k = {"sqls": _m(4), "closed_won_deals": _m(n),
         "customers": _m(2, dedup_key="hubspot_company_id", additive=False),
         "won_revenue_usd": _m(9000.0), "average_deal_value_usd": _m(2250.0)}
    k.update(kpis or {})
    return {
        "page": "pipeline", "window": _WINDOW, "read_only": True,
        "filters": {"scope": "all_source", "acquisition_group": None, "lead_filtered": False},
        "cohort": cohort or _COHORT, "kpis": k,
        "metrics": {"closed_won_deals": k["closed_won_deals"], "customers": k["customers"],
                    "revenue_usd": k["won_revenue_usd"]},
        "closed_won_population": population or k["closed_won_deals"],
        # PR-ADS-182A: the acquisition-cohort funnel, from the real producer.
        "funnel": funnel or _funnel_block(),
        "deals": deals if deals_status == "published" else None,
        "deals_status": deals_status, "deals_reason": deals_reason,
        "source_breakdown": _breakdown(deals) if deals_status == "published" else None,
        "customers_won": customers_won or {
            "status": "published", "reason": None,
            "rows": [{"company_id": "co-1", "company_name": "Acme Freight",
                      "company_name_status": "named", "closed_won_deals": 3,
                      "latest_close_date": "2026-11-24T09:00:00+00:00"},
                     {"company_id": "co-2", "company_name": None,
                      "company_name_status": "not_loaded", "closed_won_deals": 1,
                      "latest_close_date": "2026-11-24T09:00:00+00:00"}]},
        "truth_footer": "Open and lost deals are not in the deal ledger: only closed-won "
                        "deals are synced, so open pipeline, lost deals and opportunity "
                        "aging are not shown here.",
    }


def _page(payload, *, js=None, user="viewer", extra=""):
    out = _run(
        '_currentUser = {"username": "u", "role": "%s"}; %s'
        "pipelineData = %s; pipelineStatus = 'ok'; renderPipelinePage(); "
        '[__els["pipeline-summary"].innerHTML, __els["pipeline-outcomes"].innerHTML]'
        % (user, extra, json.dumps(payload)), js=js)
    return out[0], out[1]


# ── 1–2. one page; the old pages are redirects carrying their intent ─────────
def test_01_leads_deals_and_opportunities_are_redirects_into_the_one_pipeline_page():
    out = _run(
        "({pages: PAGES.filter((p) => ['pipeline','leads','deals','opportunities'].includes(p)),"
        " item: NAV_ITEMS.find((it) => it.id === 'pipeline').pages,"
        " leads: RETIRED_PAGE_REDIRECTS.leads, deals: RETIRED_PAGE_REDIRECTS.deals,"
        " opps: RETIRED_PAGE_REDIRECTS.opportunities,"
        " navs: ['leads','deals','opportunities','pipeline'].map(navItemForPage),"
        " revenue: REVENUE_PAGES.includes('pipeline') && !REVENUE_PAGES.includes('leads')"
        "   && !REVENUE_PAGES.includes('deals')})")
    assert out["pages"] == ["pipeline"]
    assert out["item"] == ["pipeline"]
    assert out["leads"] == {"page": "pipeline", "section": "leads"}
    assert out["deals"] == {"page": "pipeline", "section": "deals"}
    assert out["opps"] == {"page": "pipeline", "section": "leads", "status": "open_working"}
    assert out["navs"] == ["pipeline"] * 4
    assert out["revenue"] is True
    # The router carries both intents; the page reads them.
    nav = _fn("navigate")
    assert "if (retired.section) pipelineSetPendingSection(retired.section);" in nav
    assert "if (retired.status) leadsSetPendingStatus(retired.status);" in nav
    load = _fn("loadPipeline")
    assert "_leadsStatus = _leadsPendingStatus;" in load and "pipelineScrollToPendingSection();" in load
    # The old pages are deleted, not hidden; no tab strip, no link names them.
    for gone in ('id="page-leads"', 'id="page-deals"', 'data-section="pipeline"',
                 'data-page="leads"', 'data-page="deals"', 'href="#/leads"', 'href="#/deals"'):
        assert gone not in HTML, gone
    for gone in ("loadLeads", "loadDeals", "renderRevenueDealsPage", "leadsCustomerFiguresHtml",
                 "leadsFunnelStripHtml", "renderDealKpiRow", "renderDealLeadStatusBacklog",
                 "renderDealSqlNoDeal", "renderDealAging", "renderDealCampaignPipeline"):
        assert not re.search(r"\b%s\b" % gone, JS), gone


def test_02_one_page_in_the_dashboard_design_with_lead_filters_marked_leads_only():
    sec = _section("page-pipeline")
    assert '<h1 class="page-title">Pipeline</h1>' in sec
    assert 'class="dash-command-bar"' in sec and 'id="pipeline-page-body"' in sec
    opts = re.findall(r'<option value="([a-z_0-9]+)"( selected)?>', sec)
    assert [v for v, _ in opts] == BUSINESS_WINDOWS
    assert [v for v, sel in opts if sel] == ["current_quarter"]
    assert "through today" in sec and "apply to leads only" in sec
    filters = re.search(r'<span class="pipeline-lead-filters".*?</span>', sec, re.DOTALL).group(0)
    assert 'id="leads-scope"' in filters and 'id="leads-source"' in filters
    assert "Leads scope:" in filters and "Leads source:" in filters
    # Sections in order: KPI + funnel, the leads list, then the outcomes.
    assert sec.index('id="pipeline-summary"') < sec.index('id="pipeline-leads"') \
        < sec.index('id="pipeline-outcomes"')
    load = _fn("loadPipeline")
    assert "/api/pipeline?" in load and "acquisition_group" in load
    assert "/api/crm-funnel/customers-won" not in JS
    assert "/api/crm-funnel/contacts?" in _fn("leadsRenderContacts")


# ── 3. the SQL KPI is the cohort, gated on THIS response's cohort ────────────
def test_03_the_sql_kpi_is_the_acquisition_cohort_gated_on_the_responses_own_cohort():
    cases = {
        "published": _page(_payload())[0],
        # A faulty backend: a value beside a withheld cohort. The gate wins.
        "withheld": _page(_payload(
            cohort={**_COHORT, "sql_status": "withheld", "sql_reason": "source_bootstrap_incomplete"},
            kpis={"sqls": {"value": SENTINEL, "status": "withheld",
                           "reason": "source_bootstrap_incomplete"}}))[0],
        "no_cohort": _page({**_payload(kpis={"sqls": _m(SENTINEL)}), "cohort": None})[0],
    }
    card = lambda html: re.search(r'data-kpi="sqls">(.*?)</div>\s*</div>', html, re.DOTALL).group(1)
    assert "SQLs · contacts acquired in window" in card(cases["published"])
    assert ">4" in card(cases["published"]) and "through 25 Nov 2026" in card(cases["published"])
    for key in ("withheld", "no_cohort"):
        c = card(cases[key])
        assert not _SENTINEL_ANY.search(c), key
        assert ("Withheld" in c) or ("Unavailable" in c), key
    assert "Withheld" in card(cases["withheld"])
    kpi = _fn("pipelineKpiCardsHtml")
    assert "cohortCountHtml(sqls.value, d.cohort, null)" in kpi


# ── 4. the funnel: the acquisition cohort, never a bare "SQLs" ───────────────
def test_04_the_funnel_is_the_acquisition_cohort_and_the_list_keeps_the_customer_stage_help():
    """PR-ADS-182A (owner decision): the funnel counts by contact create date
    — "Entered stage in window" is gone from the funnel. Its stages are the
    acquisition cohort's; the contact list (its own stage picker) keeps the
    stage-entry labels and the stage-move help."""
    summary, _ = _page(_payload())
    funnel = summary[summary.index('id="pipeline-funnel"'):]
    assert "Lifecycle funnel · Acquisition cohort" in _text(funnel)
    assert "Entered stage in window" not in funnel
    labels = re.findall(r'class="dash-funnel__stage-label">(.*?)</span>', funnel)
    assert labels == ["Contacts acquired", "Reached MQL", "Reached SQL", "Reached Opportunity",
                      "Reached Customer stage"]
    assert not re.search(r"\bSQLs\b", _text(funnel)), "no bare 'SQLs' on the lifecycle funnel"
    assert "never companies, never deals" in _text(funnel)
    # The contact list's stage picker: every stage opens the list, under the
    # stage-entry labels, the stage move with its help.
    assert re.findall(r'data-leads-stage="([a-z]+)"', funnel) == [
        "lead", "mql", "sql", "opportunity", "customer"]
    picker = re.findall(r'data-leads-stage="[a-z]+"[^>]*>(.*?)</button>', funnel, re.S)
    assert picker == ["Entered Lead", "Entered MQL", "Entered SQL", "Entered Opportunity",
                      "Moved to Customer stage in HubSpot"]
    assert "including old contacts updated by the team. It is not new customers." in funnel
    assert "120" in _text(funnel)
    assert "11" in _text(funnel)
    # An unreadable cohort is Unavailable, never 0.
    s2, _ = _page(_payload(funnel=_funnel_block(
        {"sql_status": "unavailable", "sql_reason": "canonical_funnel_unreadable",
         "metric_family": "acquisition_cohort_outcomes", "window_basis": "contact_created_at"})))
    f2 = s2[s2.index('id="pipeline-funnel"'):]
    assert f2.count("Unavailable") >= 5 and ">0<" not in f2


# ── 5. the leads list opens on a stage click; names, no emails ───────────────
def test_05_the_leads_list_opens_on_a_stage_click_with_names_and_never_an_email():
    closed = _run('_leadsView = null; leadsRenderActiveView(); __els["leads-table-body"].innerHTML')
    # PR-ADS-182A: the list opens from the stage picker under the funnel.
    assert "Pick a stage under the funnel above" in closed
    rows = [{"contact_id": "101", "contact_name": "Rana Haddad", "contact_name_status": "recorded",
             "company": "Acme Freight", "event_date": "2026-11-20",
             "lifecycle_stage_label": "SQL", "email": "rana@acme.example"}]
    script = (
        "fetchJSON = () => Promise.resolve(%s); _leadsView = 'sql';"
        "leadsRenderActiveView().then(() => {}); null" % json.dumps(
            {"available": True, "rows": rows, "page": 1, "page_size": 50, "total": 1,
             "has_more": False, "names_progress": {"available": True, "read": 1, "total": 1,
                                                   "never_asked": 0}}))
    _run(script)  # parses and runs without error
    contacts = _fn("leadsRenderContacts")
    assert "row.email" not in contacts and "r.email" not in contacts
    assert "leadsContactNameHtml(r)" in contacts
    assert "Contacts that entered the ${stage.stage} stage in this window" in contacts
    # The page wires a click on each stage of the list's picker to the list
    # (PR-ADS-182A: the cohort funnel's columns are not buttons onto it).
    wire = _fn("pipelineWireFunnel")
    assert '_leadsView = btn.getAttribute("data-leads-stage")' in wire
    assert "leadsRenderActiveView()" in wire
    # The contact drawer is the existing one.
    assert 'id="leads-drawer"' in HTML and "leadsOpenDrawer(rows[" in contacts


# ── 6. the deal list: only beside a published count it equals ────────────────
def test_06_the_deal_list_is_shown_only_when_its_length_equals_the_published_count():
    _, ok = _page(_payload())
    assert ok.count('data-pipeline-deal="') == 4
    # Close date newest first, then the larger amount.
    order = re.findall(r'data-pipeline-deal="(d-\d)"', ok)
    assert order == ["d-3", "d-2", "d-1", "d-4"]
    # One more row than the count (a faulty backend): withheld, no deal named.
    extra = _deal(9, "organic", "2026-11-01T00:00:00+00:00", float(SENTINEL))
    _, bad = _page(_payload(deals=_DEALS + [extra], kpis={"closed_won_deals": _m(4)},
                            population=_m(4)))
    assert 'data-pipeline-deal="' not in bad and not _SENTINEL_ANY.search(bad)
    assert "Unavailable" in bad and "Deal 1" not in bad
    assert "the deal rows received do not match the published count" in bad
    # The population withheld: the PR-ADS-164A card, no list, never "none".
    _, held = _page(_payload(deals_status="withheld", deals_reason="canonical_ledger_stale",
                             kpis={"closed_won_deals": _m(None, "withheld", "canonical_ledger_stale")},
                             population=_m(None, "withheld", "canonical_ledger_stale")))
    assert 'data-pipeline-deal="' not in held and "This does not mean that no deals closed" in held
    assert "No closed-won deals closed in this period" not in held
    # Published zero: the empty state.
    _, empty = _page(_payload(deals=[], kpis={"closed_won_deals": _m(0)}))
    assert "No closed-won deals closed in this period" in empty


# ── 7. the deal list's Source column and its own filter ──────────────────────
def test_07_the_deal_source_filter_filters_on_the_deals_own_source_group_and_leaves_the_kpis():
    _, all_ = _page(_payload())
    head = re.search(r"<thead>(.*?)</thead>", all_[all_.index("pipeline-deal-table"):], re.DOTALL).group(1)
    assert re.findall(r"<th[^>]*>(.*?)</th>", head) == [
        "Deal", "Closed", "Won Revenue", "Source", "Campaign", "Country"]
    assert "Source not recorded" in all_          # a deal with no group is shown as itself
    # PR-ADS-178C m6: the selection is per surface and per window.
    summary, filtered = _page(_payload(), extra=(
        "pipelineSetDealSource({window: {key: 'current_quarter'}}, 'google_ads');"))
    assert re.findall(r'data-pipeline-deal="(d-\d)"', filtered) == ["d-3", "d-1"]
    assert "Showing 2 of 4 closed-won deals" in filtered
    assert "The cards above count every deal." in filtered
    assert re.search(r'data-kpi="deals">.*?>4<', summary, re.DOTALL)
    out = _run("pipelineFilterDeals(%s, 'not_recorded').map((r) => r.deal_id)" % json.dumps(_DEALS))
    assert out == ["d-4"]


# ── 8. customers won: a list only when it numbers the published count ────────
def test_08_customers_won_is_a_list_only_when_published_and_equal_to_the_count():
    _, ok = _page(_payload())
    cust = ok[ok.index('id="pipeline-customers"'):]
    assert "Acme Freight" in cust and "Company name not loaded yet" in cust
    # The count says 3, the list has 2: no list.
    _, bad = _page(_payload(kpis={"customers": _m(3)}))
    cb = bad[bad.index('id="pipeline-customers"'):]
    assert "Acme Freight" not in cb and "Withheld" in cb and "no list of customers" in cb
    # Under a lead filter the list is the same all-source population (F1 m1),
    # and no deal KPI is blanked.
    filt = _payload()
    filt["filters"] = {"scope": "all_source", "acquisition_group": "organic",
                       "lead_filtered": True}
    summary, fo = _page(filt)
    fc = fo[fo.index('id="pipeline-customers"'):]
    assert "Acme Freight" in fc and "Unavailable" not in fc
    # A status and reason the payload carries are said in words, no list.
    # (PR-ADS-178C: a reason the builder can send — the PR-ADS-166A
    # "not split by contact source" refusal is retired with its route.)
    _, uo = _page(_payload(customers_won={"status": "unavailable",
                                          "reason": "company_associations_unreadable"}))
    uc = uo[uo.index('id="pipeline-customers"'):]
    assert "Unavailable" in uc and "Acme Freight" not in uc
    assert _run('canonicalReasonLabel("company_associations_unreadable")') in uc
    assert re.search(r'data-kpi="customers">.*?>2<', summary, re.DOTALL)
    assert re.search(r'data-kpi="revenue">.*?\$9', summary, re.DOTALL)
    # Never merged with the deal list: the deal section holds no customer table.
    deals_section = fo[fo.index('id="pipeline-deals"'):fo.index('id="pipeline-customers"')]
    assert "pipeline-customers-table" not in deals_section and "Acme Freight" not in deals_section


# ── 9. the deal drawer drops the always-null fields ──────────────────────────
def test_09_the_deal_drawer_shows_only_fields_the_ledger_carries():
    html = _run("pipelineDealDrawerHtml(%s)" % json.dumps(_DEALS[0]))
    for gone in ("Company", "Main Contact", "Created date", "Age (days)", "Days to close"):
        assert gone not in html, gone
    for shown in ("Deal 1", "d-1", "Close date", "Google Ads", "brand - uk", "United Kingdom"):
        assert shown in html, shown
    fields = [k for k, _ in _run("PIPELINE_DEAL_DRAWER_FIELDS")]
    assert "company" not in fields and "age_days" not in fields and "created_date" not in fields


# ── 10. one footer line; the always-Unavailable and legacy panels are gone ───
def test_10_one_truth_footer_line_and_no_always_unavailable_or_legacy_panels():
    summary, outcomes = _page(_payload())
    page = summary + outcomes
    assert page.count("dash-truth-footer") == 1
    assert "Open and lost deals are not in the deal ledger" in page
    for gone in ("Opportunities Created", "Open Pipeline", "Opportunity Aging",
                 "Opportunity → Customer", "SQL → Customer", "Qualified (lead status)",
                 "dash-decisions"):
        assert gone not in page, gone


# ── 11. Dashboard → Deals renders the same builder's figures ─────────────────
def test_11_dashboard_deals_renders_the_pipeline_builders_figures_with_the_same_renderers():
    d = {**_payload(), "surface": "dashboard_deals",
         "source_breakdown": [
             {"source_group": "google_ads", "closed_won_deals": 2, "customers": 1,
              "won_revenue": 6500.0, "metrics": {"closed_won_deals": _m(2), "customers": _m(1),
                                                 "revenue_usd": _m(6500.0)}},
             {"source_group": "organic", "closed_won_deals": 1, "customers": 1,
              "won_revenue": 1500.0, "metrics": {"closed_won_deals": _m(1), "customers": _m(1),
                                                 "revenue_usd": _m(1500.0)}}],
         "period_change": {"available": False, "reason": "All Time has no previous period."}}
    html = _run('_currentUser = {"username": "u", "role": "viewer"};'
                "dashDeals = %s; dashDealsStatus = 'ok'; renderDashboardDeals();"
                '__els["dashboard-deals-root"].innerHTML' % json.dumps(d))
    assert html.count('data-kpi="') == 5
    for gone in ("Qualified (lead status)", "Opportunities Created", ">Open Pipeline<",
                 "Opportunity Aging", "Campaign → Pipeline", "dash-decisions"):
        assert gone not in html, gone
    assert html.count('data-pipeline-deal="') == 4 and "Acme Freight" in html
    assert "Closed-won deals by source" in html and "are not added up" in html
    assert 'href="#/pipeline"' in html
    render = _fn("renderDashboardDeals")
    for fn in ("pipelineKpiCardsHtml(d, pc)", "pipelineDealsHtml(d)",
               "pipelineCustomersWonHtml(d)", "pipelineSourceBreakdownHtml(d)"):
        assert fn in render, fn


# ── 12. links that pointed at the old pages now name the Pipeline page ───────
def test_12_links_to_the_old_pages_are_relabelled_and_the_window_change_reloads_pipeline():
    related = _run("[datasetRelatedPage('hubspot', 'contacts'), datasetRelatedPage('hubspot', 'deals')]")
    assert related == [{"page": "pipeline", "label": "Pipeline"}] * 2
    # PR-ADS-177C (rebased on 178B): the Campaigns page drawer too.
    for fn in ("renderCountryDrawer", "renderCampaignsPageDrawer"):
        assert '["pipeline", "Open Pipeline"]' in _fn(fn) and '"Open Deals"' not in _fn(fn)
    rev = (ROOT / "services" / "dashboard_revenue_service.py").read_text()
    assert '"target_page": "deals"' not in rev and '"target_page": "pipeline"' in rev
    change = _fn("handleBusinessWindowSelectChange")
    assert 'case "pipeline":        _leadsPage = 1; loadPipeline(); break;' in change
    assert 'case "leads"' not in change and 'case "deals"' not in change
    assert 'case "pipeline":      loadPipeline();      break;' in _fn("loadPage")
