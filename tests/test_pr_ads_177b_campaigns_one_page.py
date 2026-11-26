"""PR-ADS-177B — one Campaigns page, Meta ad sets beside it.

Campaign Evidence (``campaigns``) and Campaign Revenue & Spend
(``roas-campaigns``) were two pages with different windows, a different SQL
population, a different spend gate and a different campaign key. Owner
decisions 2026-11-24 (``docs/60`` §3): one page in the Dashboard design, on
business windows that end yesterday, reading the mart's SETTLED campaign view;
Meta ad sets in a separate table, never summed with Google, outcomes not
linked yet.

Every case runs the real ``static/app.js`` in ``node`` (the harness of
``tests/test_pr_ads_161b_marketing_outcome_cohorts``) or reads the real
``static/index.html`` / ``static/styles.css`` / service sources. Fail-before
evidence is in ``docs/60`` §7.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")

BUSINESS_WINDOWS = ["current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"]
#: Figures no legitimate rendering of the fixture produces.
SENTINEL = 7919
META_SENTINEL = 6007


def _run(script, *, js=None):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(script, js=js)


def _section(page_id):
    m = re.search(r'<section id="%s".*?</section>' % re.escape(page_id), HTML, re.DOTALL)
    assert m, page_id
    return m.group(0)


def _fn(name):
    """One top-level function (sync or async), to its column-0 closing brace."""
    m = re.search(r"\n(?:async )?function %s\(" % re.escape(name), JS)
    assert m, f"function {name} not found"
    j = JS.find("\n}\n", m.start())
    return JS[m.start():j + 2]


# ── fixtures: the settled campaign view, in the mart's own shape ─────────────
def _metric(value, status="published", reason=None, **extra):
    return {"value": value if status == "published" else None, "status": status,
            "reason": reason, **extra}


_PERIOD = {"cohort_basis": "close_date_outcomes_over_same_window_spend",
           "ratio_kind": "non_cohort_operational_ratio"}
_COHORT = {"sql_status": "published", "sql_reason": None,
           "metric_family": "acquisition_cohort_outcomes",
           "window_basis": "contact_created_at",
           "window": {"start_date": "2026-10-01", "end_date": "2026-11-23"},
           "campaign_residual": {"label": "Google Ads contacts on no campaign row",
                                 "leads": 4, "sqls": 1}}
_WINDOW = {"key": "current_quarter", "label": "Current Quarter",
           "start_date": "2026-10-01", "end_date": "2026-11-23",
           "through_reason": "today_not_settled",
           "window_range": {"start": "2026-10-01", "through": "2026-11-23",
                            "through_reason": "today_not_settled", "empty": False,
                            "disclosure": "Through 23 Nov 2026 — today is not included "
                                          "until tomorrow morning's sync. Other revenue "
                                          "pages, such as Pipeline, already count deals "
                                          "closed today."}}


def _row(name, spend, *, leads=3, sqls=1, deals=1, revenue=1000.0, verdict="winner",
         deal_ids=("d-1",), ratio=None, customers=1):
    ratio = ratio if ratio is not None else (round(revenue / spend, 2) if spend else None)
    return {
        "campaign_id": name.lower(), "campaign_name": name, "spend": spend,
        "spend_state": "mapped_exact", "spend_mapping": "matched",
        "leads": leads, "sqls": sqls, "sqls_status": "published", "sqls_reason": None,
        "closed_won_deals": deals, "customers": customers, "won_revenue": revenue,
        "period_revenue_per_ad_spend": ratio, "verdict": verdict,
        "deal_ids": list(deal_ids) if deal_ids is not None else None,
        "aliases": [],
        "metrics": {
            "closed_won_deals": _metric(deals), "customers": _metric(customers),
            "revenue_usd": _metric(revenue),
            "period_revenue_per_ad_spend": (_metric(ratio, **_PERIOD) if ratio is not None
                                            else _metric(None, "unavailable", "no_spend")),
        },
    }


def _residual(leads=4, sqls=1, status="published", reason=None):
    na = {"value": None, "status": "not_applicable", "reason": "not_campaign_attributable"}
    return {"is_residual": True, "campaign_id": None, "campaign_name": "No campaign label",
            "label": "No campaign label",
            "definition": "Google Ads-sourced contacts created in the window whose HubSpot "
                          "campaign label is missing, a pseudo- or email campaign, or "
                          "conflicting across their rows",
            "leads": leads, "sqls": sqls, "sqls_status": status, "sqls_reason": reason,
            "spend": None, "closed_won_deals": None, "customers": None,
            "won_revenue": None, "period_revenue_per_ad_spend": None, "deal_ids": None,
            "not_attributable_reason": "not_campaign_attributable",
            "verdict": "not_campaign_attributable",
            "metrics": {k: dict(na) for k in ("spend", "closed_won_deals", "customers",
                                             "revenue_usd", "period_revenue_per_ad_spend")}}


def _totals(**over):
    t = {"spend": float(SENTINEL), "leads": 12, "sqls": 4, "closed_won_deals": 3,
         "customers": 2, "won_revenue": 9000.0, "period_revenue_per_ad_spend": 1.14}
    t.update(over)
    return {
        "population": "campaign_attributable", "revenue_scope": "campaign_attributable",
        **t,
        "metrics": {
            "spend": _metric(t["spend"]), "leads": _metric(t["leads"]),
            "sqls": _metric(t["sqls"]), "closed_won_deals": _metric(t["closed_won_deals"]),
            "customers": _metric(t["customers"]), "revenue_usd": _metric(t["won_revenue"]),
            "period_revenue_per_ad_spend": _metric(t["period_revenue_per_ad_spend"], **_PERIOD),
        },
    }


def _mart(*, rows=None, totals=None, cohort=None, residual="default", window=None,
          spend_ready=True, revenue_ready=True, spend_truth=None):
    rows = rows if rows is not None else [
        _row("Brand - UK", 250.0, leads=5, sqls=2, deals=2, revenue=6000.0,
             deal_ids=("d-1", "d-2")),
        _row("Gulf Search", 400.0, leads=3, sqls=1, deals=1, revenue=3000.0,
             deal_ids=("d-3",)),
    ]
    return {
        "view": "campaign", "settled_days": True,
        "window": window or _WINDOW,
        "readiness": {"revenue_decision_ready": revenue_ready,
                      "spend_decision_ready": spend_ready,
                      "canonical_revenue_status": "canonical_available",
                      "lead_metrics_status": "db"},
        "spend_truth": spend_truth or {"campaign_spend_status": "verified",
                                       "fx_status": "verified",
                                       "spend_coverage_status": "complete",
                                       "campaign_spend_unsettled_reason": None},
        "rows": rows,
        "campaign_totals": totals or _totals(),
        "campaign_residual_row": _residual() if residual == "default" else residual,
        "cohort": cohort or _COHORT,
    }


def _meta(status="published", *, retired=(), disclosure=None, rows=None):
    def m(v, cur=None):
        out = {"value": v, "status": "published", "reason": None}
        if cur:
            out["currency"] = cur
        return out
    not_linked = {k: {"value": None, "status": "not_linked",
                      "reason": "meta_adset_not_linked_to_hubspot"}
                  for k in ("hubspot_sql_contacts", "hubspot_closed_won_deals",
                            "hubspot_won_revenue")}
    rows = rows if rows is not None else [
        {"account_id": "act_1", "account_name": "Logistaas MENA", "time_zone": "Asia/Amman",
         "campaign_id": "c-1", "campaign_name": "Lead Generation | 2026",
         "adset_id": "as-1", "adset_name": "Look Alike — GCC",
         "spend": m(40.0, "JOD"), "spend_usd": m(56.42, "USD"), "impressions": m(2000),
         "clicks": m(50), "meta_reported_leads": m(3), "attribution_settings": ["7d_click_1d_view"],
         "hubspot_outcomes": not_linked},
    ]
    return {"status": status, "reason": None if status == "published" else "meta_adset_coverage_incomplete",
            "detail": None if status == "published" else "account act_1: ad-set detail is not verified",
            "scope": "meta_ads_adsets", "never_summed_with": "google_ads_campaigns",
            "window": {"key": "current_quarter"},
            "rows": rows if status == "published" else [],
            "totals": ({"adsets": len(rows), "spend": m(40.0, "JOD"),
                        "spend_usd": {**m(float(META_SENTINEL)), "currency": "USD"},
                        "impressions": m(2000), "clicks": m(50), "meta_reported_leads": m(3),
                        "hubspot_outcomes": not_linked} if status == "published" else None),
            "meta_reported_leads_disclosure": "Meta-reported leads are Meta's own attributed "
                                              "counts under each ad set's attribution setting.",
            "owner_declared_retired": list(retired),
            "owner_declared_retired_disclosure": disclosure}


def _page(mart, meta=None, *, js=None, user="admin"):
    """The real renderCampaignsPage over ``mart`` (and the Meta table)."""
    return _run(
        '_currentUser = {"username": "u", "role": "%s"}; '
        "campaignsPageMart = %s; campaignsPageMeta = %s; renderCampaignsPage(); "
        '__els["campaigns-page-body"].innerHTML'
        % (user, json.dumps(mart), json.dumps(meta if meta is not None else _meta())), js=js)


def _cells(html, marker):
    i = html.index(marker)
    tr = html[html.rfind("<tr", 0, i):html.index("</tr>", i)]
    return re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)


# ── 1–4. one page; the old pages and their bookmark ─────────────────────────
def test_01_the_old_bookmark_lands_on_campaigns_and_the_menu_has_one_campaigns_page():
    out = _run(
        "({redirect: RETIRED_PAGE_REDIRECTS['roas-campaigns'],"
        " isPage: PAGES.includes('roas-campaigns'), campaignsIsPage: PAGES.includes('campaigns'),"
        " item: navItemForPage('roas-campaigns'),"
        " pages: NAV_ITEMS.find((it) => it.id === 'campaigns').pages,"
        " evidence: EVIDENCE_PAGES.includes('campaigns'),"
        " revenue: REVENUE_PAGES.includes('campaigns') && !REVENUE_PAGES.includes('roas-campaigns')})")
    assert out["redirect"] == {"page": "campaigns"}
    assert out["isPage"] is False and out["campaignsIsPage"] is True
    assert out["item"] == "campaigns"
    # PR-ADS-177C: Keywords is a tab of the one Keywords & Search Terms page.
    assert out["pages"] == ["campaigns", "search-terms"]
    assert out["evidence"] is False, "Campaigns is on business windows now"
    assert out["revenue"] is True


def test_02_one_page_in_the_dashboard_design_on_the_five_business_windows():
    sec = _section("page-campaigns")
    assert '<h1 class="page-title">Campaigns</h1>' in sec
    assert 'class="dash-command-bar"' in sec and 'id="campaigns-page-body"' in sec
    assert "ends yesterday" in sec
    opts = re.findall(r'<option value="([a-z_0-9]+)"( selected)?>', sec)
    assert [v for v, _ in opts] == BUSINESS_WINDOWS
    assert [v for v, sel in opts if sel] == ["current_quarter"]
    assert 'data-business-window-select' in sec
    for gone in ("7d", "14d", "30d", "evidence-window"):
        assert gone not in sec
    # The settled view is what the page reads, and its drawer reads the same days.
    load = _fn("loadCampaignsPage")
    assert "view=campaign&settled=true" in load and "/api/meta-ads/adsets" in load
    assert "campaign-detail?settled=true" in _fn("loadCampaignsPageDrilldown")
    # PR-ADS-177C: the one-line ``loadCampaigns`` alias went with the Campaign
    # Evidence code; the router calls the Campaigns page loader itself.
    assert "function loadCampaigns(" not in JS
    assert re.search(r'case "campaigns":\s+loadCampaignsPage\(\);', JS)


def test_03_campaign_revenue_and_spend_is_deleted_whole():
    assert 'id="page-roas-campaigns"' not in HTML
    assert 'data-page="roas-campaigns"' not in HTML and 'href="#/roas-campaigns"' not in HTML
    assert 'campaign-evidence-shell' not in HTML
    for gone in ("loadRoasCampaigns", "renderRoasCampaignsPage", "renderRoasCampaignTable",
                 "renderRoasCampaignSummary", "renderRoasCampaignFilters",
                 "loadCampaignDrilldown", "roasCampaignMart", "roasCampaignSourceHealth"):
        assert not re.search(r"\b%s\b" % gone, JS), gone
    assert "campaign-expand-btn" not in JS and ".campaign-expand-btn" not in CSS
    # No link anywhere still names the deleted page.
    assert "Campaign Revenue & Spend\"" not in JS
    assert "Open Campaign Revenue & Spend" not in JS
    for svc in ("dashboard_overview_service", "dashboard_revenue_service",
                # PR-ADS-181A: dashboard_channels_service is deleted.
                "dashboard_campaigns_service"):
        src = (ROOT / "services" / f"{svc}.py").read_text(encoding="utf-8")
        assert "roas-campaigns" not in src and "Open Campaign Revenue & Spend" not in src, svc


def test_04_the_columns_and_the_residual_row_pinned_last():
    html = _page(_mart())
    head = re.search(r"<thead>(.*?)</thead>", html[html.index("camp-page-table"):], re.DOTALL).group(1)
    names = [re.sub(r"<span.*?</span>", "", h).strip()
             for h in re.findall(r"<th[^>]*>(.*?)</th>", head, re.DOTALL)]
    assert names == ["Campaign", "Spend", "Leads", "SQLs", "Won Deals", "Customers",
                     "Won Revenue", "Period Revenue / Ad Spend", "Status"]
    for dropped in ("Junk", "Junk rate", "Junk Rate", "CPQL"):
        assert f">{dropped}<" not in head
    body = html[html.index("<tbody>", html.index("camp-page-table")):]
    assert body.rfind("No campaign label") > body.rfind("Gulf Search"), "residual pinned last"
    cells = _cells(body, "No campaign label")
    assert len(cells) == 9
    assert ">4<" in cells[2].replace("4 ", "4<") or cells[2].startswith("4")
    assert cells[3].startswith("1")
    for i in (1, 4, 5, 6, 7):
        assert "Not campaign-attributable" in cells[i], (i, cells[i])
        assert "contacts with no campaign label" in cells[i]
        assert "—" not in cells[i] and not re.search(r">\s*0\s*<", cells[i])


# ── 5–8. the page prints the server's figures, and withholds honestly ────────
def test_05_the_kpis_are_the_servers_totals_never_a_client_sum():
    html = _page(_mart())
    spend_card = re.search(r'data-kpi="spend".*?dash-kpi-card__source', html, re.DOTALL).group(0)
    assert "$7.9k" in spend_card, "the KPI is campaign_totals.spend, not Σ rows ($650)"
    assert "$650" not in spend_card
    for key, text in (("leads", "12"), ("sqls", "4"), ("deals", "3"), ("customers", "2"),
                      ("revenue", "$9.0k")):
        card = re.search(r'data-kpi="%s".*?dash-kpi-card__source' % key, html, re.DOTALL).group(0)
        assert text in card, (key, card[:300])
    sql_card = re.search(r'data-kpi="sqls".*?dash-kpi-card__source', html, re.DOTALL).group(0)
    assert "incl. 1 with no campaign label" in sql_card and "through 23 Nov 2026" in sql_card
    ratio = re.search(r'data-kpi="period_revenue_per_ad_spend".*?dash-kpi-card__source',
                      html, re.DOTALL).group(0)
    assert "1.14x" in ratio and "Non-cohort operational ratio" in ratio
    assert "ROAS" not in html and "CAC" not in html
    # The page has no client-side summing helper left to call.
    assert "summarizeRoasCampaignRows" not in JS


def test_06_the_strict_spend_gate_shows_the_card_and_no_figure():
    st = {"campaign_spend_status": "incomplete", "fx_status": "verified",
          "spend_coverage_status": "incomplete",
          "campaign_spend_unsettled_reason": "spend_not_reread_since_day_ended"}
    html = _page(_mart(spend_ready=False, spend_truth=st))
    assert 'data-campaign-spend-gate="spend_not_reread_since_day_ended"' in html
    assert "yesterday&#39;s spend has not been re-read since the day ended" in html \
        or "yesterday's spend has not been re-read since the day ended" in html
    assert "the morning sync re-reads the newest day" in html
    assert "7.9k" not in html and "camp-page-table" not in html and 'data-kpi=' not in html
    # Mutation: the gate check removed → the withheld figures print. It is a guard.
    gate = "  if (data.readiness.spend_decision_ready !== true) {"
    assert gate in JS
    mutated = JS.replace(gate, "  if (false) {")
    assert "7.9k" in _page(_mart(spend_ready=False, spend_truth=st), js=mutated)


def test_07_a_window_with_no_settled_day_shows_the_disclosure_only():
    window = {**_WINDOW, "start_date": "2026-10-01", "end_date": "2026-09-30",
              "window_range": {**_WINDOW["window_range"], "empty": True,
                               "disclosure": "No completed day in this window yet — the "
                                             "first day ends tonight."}}
    html = _page(_mart(window=window))
    assert "No completed day yet" in html and "No completed day in this window yet" in html
    assert 'data-kpi=' not in html and "camp-page-table" not in html and "Meta Ads" not in html
    assert "7.9k" not in html


def test_08_a_withheld_cohort_reads_withheld_never_zero_or_a_dash():
    cohort = {**_COHORT, "sql_status": "withheld", "sql_reason": "source_bootstrap_incomplete",
              "campaign_residual": {"leads": None, "sqls": None}}
    rows = [{**_row("Brand - UK", 250.0), "leads": None, "sqls": None,
             "sqls_status": "withheld", "sqls_reason": "source_bootstrap_incomplete"}]
    html = _page(_mart(rows=rows, cohort=cohort,
                       residual=_residual(None, None, "withheld", "source_bootstrap_incomplete"),
                       totals=_totals(leads=None, sqls=None)))
    for marker in ("Brand - UK", "No campaign label"):
        cells = _cells(html[html.index("<tbody>", html.index("camp-page-table")):], marker)
        for c in (cells[2], cells[3]):
            assert "Withheld" in c and "backfill" in c, c
            assert "—" not in c and not re.search(r">\s*0\s*<", c)
    for key in ("leads", "sqls"):
        card = re.search(r'data-kpi="%s".*?dash-kpi-card__source' % key, html, re.DOTALL).group(0)
        assert "Withheld" in card and "incl." not in card


# ── 9. the drawer is the row's own population ───────────────────────────────
def test_09_the_drawer_lists_exactly_the_rows_deal_ids_or_says_why_not():
    row = _row("Brand - UK", 250.0, deals=2, deal_ids=("d-1", "d-2"))
    detail = {"details": [{"deal_record_id": "d-2", "deal_name": "Two", "deal_amount_usd": 5000},
                          {"deal_record_id": "d-1", "deal_name": "One", "deal_amount_usd": 1000}],
              "source_health": {"status": "available"}}
    out = _run("[renderCampaignDrilldown(%s, %s), renderCampaignDrilldown(%s, %s),"
               " renderCampaignsPageDrawer(%s, %s)]"
               % (json.dumps(detail), json.dumps(row),
                  json.dumps({**detail, "details": detail["details"][:1]}), json.dumps(row),
                  json.dumps({**row, "closed_won_deals": None, "deal_ids": None,
                              "metrics": {**row["metrics"],
                                          "closed_won_deals": _metric(None, "withheld",
                                                                      "google_ads_deals_not_placed_on_a_campaign")}}),
                  json.dumps(_mart())))
    full, short, withheld = out
    assert "One" in full and "Two" in full and "does not match" not in full
    assert "does not match this row" in short and "Two" not in short and "One" not in short
    assert "Closed-won deals are withheld" in withheld
    # A withheld row reads nothing: no fetch is started for it.
    body = _fn("openCampaignsPageDrawer")
    assert "if (r.closed_won_deals !== null && r.closed_won_deals !== undefined)" in body
    # The drawer is opened on a market row only — never on the residual.
    page = _page(_mart())
    resid = page[page.rfind("<tr", 0, page.index("No campaign label")):]
    assert "data-campaign-page-row" not in resid[:resid.index("</tr>")]


# ── 10–12. the Meta Ads — ad sets table ─────────────────────────────────────
def test_10_meta_ad_sets_are_a_separate_table_with_their_own_totals():
    html = _page(_mart())
    meta = html[html.index('aria-label="Meta Ads — ad sets"'):]
    assert "Meta Ads — ad sets" in meta and "never summed with Google Ads" in meta
    head = re.findall(r"<th>(.*?)</th>", meta)
    assert head == ["Ad set", "Spend (account currency)", "Spend (USD)", "Impressions",
                    "Clicks", "Meta-reported leads", "SQLs", "Won deals", "Won revenue"]
    row = _cells(meta, "Look Alike — GCC")
    assert "40.00 JOD" in row[1] and "$56.42 USD" in row[2] and "2,000" in row[3]
    assert row[5].strip() == "3"
    for c in row[6:]:
        assert "Not linked yet" in c, c
    total = _cells(meta, "Meta ad sets — total")
    assert "6,007" in total[2], "the table's own server total"
    assert all("Not linked yet" in c for c in total[6:])
    assert "No Meta ad set is joined to a HubSpot contact or deal yet" in meta
    # Never added to Google: the Google KPI cards are untouched by the Meta total.
    kpis = html[:html.index("camp-page-table")]
    assert "6,007" not in kpis and "13,926" not in html and "13.9k" not in html
    assert "ROAS" not in meta and "CAC" not in meta and "per lead" not in meta.lower()


def test_11_meta_figures_are_withheld_with_the_reason_never_shown_partially():
    html = _page(_mart(), _meta("withheld"))
    meta = html[html.index('aria-label="Meta Ads — ad sets"'):]
    assert "Withheld" in meta and "act_1" in meta
    assert "6,007" not in meta and "<tbody>" not in meta
    # A table resting on the owner's retirement declaration without its
    # disclosure shows no figure (the gate, as on the Meta spend line).
    retired = _page(_mart(), _meta(retired=["act_old"], disclosure=None))
    meta = retired[retired.index('aria-label="Meta Ads — ad sets"'):]
    assert "6,007" not in meta and "Withheld" in meta
    shown = _page(_mart(), _meta(retired=["act_old"], disclosure="The owner retired act_old."))
    assert "The owner retired act_old." in shown and "6,007" in shown
    gate = _fn("metaAdsetTableGate")
    assert "owner_declared_retired_disclosure" in gate and "meta_retired_disclosure_missing" in gate
    assert "metaAdsetTableGate(" in _fn("renderMetaAdsetTable")


def test_12_the_reader_audits_register_the_meta_table_and_find_no_ratio_label():
    from scripts import audit_customer_revenue_reader_cutover as audit
    a = audit.Audit()
    audit.meta_structural_checks(a, JS)
    assert a.checks.get("meta_spend_disclosure_carried") is True, a.violations
    assert a.checks.get("no_rate_over_meta_spend") is True, a.violations
    assert {r["consumer"] for r in audit.META_ADSET_PAGES} == {
        "services/meta_ads_adset_table_service.py"}
    assert audit.ui_ratio_label_violations(JS, HTML) == []
    # Mutation: a page module reading the breakdown that is not registered fails.
    saved = audit.META_ADSET_PAGES
    try:
        audit.META_ADSET_PAGES = ()
        b = audit.Audit()
        audit.meta_structural_checks(b, JS)
        assert b.checks.get("meta_spend_disclosure_carried") is False
    finally:
        audit.META_ADSET_PAGES = saved


# ── 13–14. Dashboard → Campaigns reads the same settled view ────────────────
def _dash_payload():
    m = _mart()
    rows = [{**r, "spend_native": r["spend"], "native_currency": "GBP",
             "spend_usd": r["spend"], "won_revenue_usd": r["won_revenue"],
             "period_ratio_available": True, "status": "Revenue proven",
             "attribution_status": "mapped", "deals": []} for r in m["rows"]]
    return {"window": _WINDOW, "cohort": _COHORT, "campaigns": rows,
            "campaign_residual_row": _residual(),
            "kpis": {"verified_spend_native": {"amount": 500.0, "currency": "GBP"},
                     "verified_spend_usd": 650.0, "sqls": 4, "sqls_residual": 1,
                     "customers": 2, "customers_metric": _metric(2),
                     "closed_won_deals": 3, "won_revenue_usd": 9000.0,
                     "revenue_metric": _metric(9000.0),
                     "period_revenue_per_ad_spend": 13.85,
                     "metrics": {"period_revenue_per_ad_spend": _metric(13.85, **_PERIOD)},
                     "campaigns_with_spend": 2},
            "period_change": {}, "search_term_signals": {"status": "ready"},
            "decision_cards": [], "truth_status": {}, "deal_proof_available": True}


def test_13_the_dashboard_tab_pins_the_residual_and_drops_the_keyword_panel():
    html = _run("dashCampaigns = %s; dashCampaignsStatus = 'ok'; renderDashboardCampaigns();"
                ' __els["dashboard-campaigns-root"].innerHTML' % json.dumps(_dash_payload()))
    assert "Keyword Theme" not in html and "keyword theme" not in html.lower()
    lb = html[html.index("Campaign Leaderboard"):]
    assert lb.rfind("No campaign label") > lb.rfind("Gulf Search")
    cells = _cells(lb, "No campaign label")
    assert cells[2].startswith("1")
    assert all("Not campaign-attributable" in c for c in (cells[1], cells[3], cells[4], cells[5], cells[6]))
    assert "Unattributed / Needs Review" not in html
    ratio = re.search(r'data-kpi="period_revenue_per_ad_spend".*?dash-kpi-card__source',
                      html, re.DOTALL).group(0)
    assert "13.85x" in ratio and "Non-cohort operational ratio" in ratio
    assert "Through 23 Nov 2026" in html, "the settled window says so on the tab"
    assert "renderCampKeywordThemes" not in JS


def test_14_the_dashboard_service_recomputes_no_ratio_and_groups_no_deals_by_label():
    src = (ROOT / "services" / "dashboard_campaigns_service.py").read_text(encoding="utf-8")
    from services import dashboard_campaigns_service as svc
    for gone in ("_period_ratio", "_group_deals_by_campaign", "_build_keyword_themes",
                 "UNATTRIBUTED_LABEL"):
        assert not hasattr(svc, gone), gone
    assert "fetch_keyword_theme_snapshot" not in src
    assert 'settled=True' in src and "campaign_totals" in src
    assert "deal_ids" in src
