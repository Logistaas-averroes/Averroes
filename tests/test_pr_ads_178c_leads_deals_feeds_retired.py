"""PR-ADS-178C — the legacy Leads / Deals feeds are retired (``docs/61`` §13).

PR-ADS-178B replaced the Leads and Closed-Won Deals pages with one Pipeline
page and kept three routes alive for this PR. Two are retired here:

* ``GET /api/revenue-deals`` — no caller since PR-ADS-178B;
* ``GET /api/crm-funnel/customers-won`` — read only by the cross-page parity
  audit (``leads/customers_won``) and the 166A tests. Its reading moved into
  the Pipeline builder (``customers_won``) and parity reads it there
  (``pipeline/customers_won``) — ported first, then deleted.

``GET /api/dashboard/deals`` is KEPT: Dashboard → Deals fetches it.

These cases hold the retirement (the running app, the source, the audit that
enforces it in production), the dead code 178B listed, and 178B's carried
minors m6 (the deal-source filter) and the round-2 observation (funnel
lower-bound coverage per attribution scope). The PostgreSQL ports are in
``tests/test_pr_ads_178c_leads_deals_port_pg_integration.py``.

Every UI case runs the real ``static/app.js`` in ``node``.
"""
from __future__ import annotations

import ast
import json
import re
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
SERVER = (ROOT / "api" / "server.py").read_text(encoding="utf-8")

RETIRED_ROUTES = ("/api/revenue-deals", "/api/crm-funnel/customers-won")
#: Routes 178B kept beside them that stay: a page reads each.
KEPT_ROUTES = ("/api/dashboard/deals", "/api/pipeline", "/api/crm-funnel/contacts",
               "/api/crm-funnel/operational-status")
PRODUCTION_PACKAGES = ("api", "services", "scheduler", "db", "analysis",
                       "connectors", "scripts")

#: Everything this PR deletes, by name: no production code may name one.
DELETED_PY = ("get_revenue_deals", "api_crm_funnel_customers_won",
              "build_customers_won", "_build_customers_won", "_cw_metric",
              "_cw_envelope", "_cw_unavailable", "_cw_parity_blocks",
              "_cw_canonical_window", "_won_customers_block",
              "CONSUMER_LEADS_CUSTOMERS_WON", "REASON_WINDOW_NOT_EXPRESSIBLE",
              "REASON_FILTER_NOT_APPLICABLE")
DELETED_JS = ("renderMartDiagnostics", "_pipelineDealSource",
              "customers_won_needs_a_business_window",
              "customers_won_not_split_by_contact_source",
              "customers_won_unreadable", "customers_won_window_mismatch")
#: The deleted Deals-tab / Leads-page / diagnostics CSS classes.
DELETED_CSS = ("deal-kpi-grid", "deal-dot", "deal-status", "deal-funnel",
               "deal-funnel-panel", "deal-aging-panel", "deal-aging-list",
               "deal-aging-row", "deal-campaign-panel", "deal-sql-panel",
               "deal-review-note", "deal-drawer__actions", "deal-drawer__link",
               "leads-customer-figures", "leads-customer-figure",
               "leads-won-customers", "revenue-diagnostics", "revenue-diagnostic")


def _run(script, *, js=None):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(script, js=js)


def _production_py():
    return sorted(p for d in PRODUCTION_PACKAGES for p in (ROOT / d).rglob("*.py")
                  if "__pycache__" not in p.parts)


def _code_names(path: Path) -> set:
    """Every identifier the file's CODE uses or defines (not comments or
    docstrings): a note that something was retired is not a reference."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.name.split(".")[-1])
            if node.asname:
                names.add(node.asname)
    return names


def _js_code(src: str) -> str:
    """``src`` without ``//`` line comments and ``/* */`` blocks."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//[^\n]*|(?<=[;{}),\s])//[^\n]*", "", src)


def _registered(src: str) -> set:
    from scripts.audit_customer_revenue_reader_cutover import _registered_routes
    return _registered_routes(src)


# ── 1. the running app: the retired routes answer 404; the kept ones are served ──
def test_01_the_running_app_answers_404_on_both_retired_routes_and_keeps_the_others(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client = TestClient(server.app, raise_server_exceptions=False)
    auth = {"Authorization": "Bearer secret-token"}
    paths = {getattr(r, "path", None) for r in server.app.routes}
    for route in RETIRED_ROUTES:
        assert route not in paths, route
        # 404, signed in or not: there is no route (a gated live one says 401).
        assert client.get(route).status_code == 404, route
        assert client.get(f"{route}?window=last_quarter", headers=auth).status_code == 404
    for route in KEPT_ROUTES:
        assert route in paths, route
        assert client.get(route).status_code == 401, route


# ── 2. the source: no route, handler, builder or request names a retired reader ──
def test_02_no_handler_builder_or_page_request_names_a_retired_reader():
    assert not set(RETIRED_ROUTES) & _registered(SERVER)
    assert set(KEPT_ROUTES) <= _registered(SERVER)
    offenders = []
    for path in _production_py():
        names = _code_names(path)
        offenders += [f"{path.relative_to(ROOT)}: {n}" for n in DELETED_PY if n in names]
    assert offenders == [], offenders
    for page in (JS, HTML):
        for route in RETIRED_ROUTES:
            for quote in ('"', "'", "`"):
                assert f"{quote}{route}" not in page, route
    # Dashboard → Deals still reads its route — the reason it is kept.
    load = JS[JS.index("async function loadDashboardDeals("):]
    load = load[:load.index("\n}\n")]
    assert "`/api/dashboard/deals?window=${encodeURIComponent(window_)}`" in load
    # The funnel service reads no closed-won deal any more; the Pipeline
    # builder holds the reading.
    funnel = _code_names(ROOT / "services" / "canonical_crm_funnel_service.py")
    assert not funnel & {"load_won_deals", "single_snapshot", "canonical_metrics",
                         "fetch_company_names"}
    pipeline = _code_names(ROOT / "services" / "pipeline_service.py")
    assert {"_customers_won_reading", "_reader_verdict", "_won_customers_list",
            "load_won_deals"} <= pipeline


# ── 3. the enforcement: the reader-cutover audit fails if one comes back ────────
def test_03_the_reader_cutover_audit_holds_the_retirement_and_fails_when_one_returns(monkeypatch):
    """Its runtime twin: ``retired_readers_absent`` is a structural check of
    ``audit_customer_revenue_reader_cutover`` — in the production
    certification's AUDITS — so a reintroduction goes red there too."""
    from scripts import audit_customer_revenue_reader_cutover as audit
    assert audit.RETIRED_READER_ROUTES == RETIRED_ROUTES
    assert "build_customers_won" in audit.RETIRED_READER_SYMBOLS
    from scripts.audit_platform_certification import AUDITS
    assert any(m == "scripts.audit_customer_revenue_reader_cutover" for _, m, _ in AUDITS)

    a = audit.Audit()
    audit.structural_checks(a)
    assert a.checks.get("retired_readers_absent") is True, a.violations

    real_text = audit._text

    def failing(mutate):
        b = audit.Audit()
        monkeypatch.setattr(audit, "_text", mutate)
        try:
            audit.retired_reader_checks(b, mutate(audit.APP_JS))
        finally:
            monkeypatch.setattr(audit, "_text", real_text)
        return b.checks.get("retired_readers_absent"), b.violations

    # Each mutation: a route re-registered, a page requesting one, a module
    # bringing the builder back.
    route_back = ('\n\n@app.get("/api/crm-funnel/customers-won")\n'
                  'def api_crm_funnel_customers_won():\n    return {}\n')
    ok, why = failing(lambda rel: real_text(rel) + (route_back if rel == audit.SERVER else ""))
    assert ok is False and "registers /api/crm-funnel/customers-won" in why[0]
    ok, why = failing(lambda rel: real_text(rel) + (
        '\n@app.get("/api/revenue-deals")\nasync def get_revenue_deals():\n    return {}\n'
        if rel == audit.SERVER else ""))
    assert ok is False and "registers /api/revenue-deals" in why[0]
    ok, why = failing(lambda rel: real_text(rel) + (
        '\nfetchJSON(`/api/revenue-deals?window=${w}`);\n' if rel == audit.APP_JS else ""))
    assert ok is False and "requests /api/revenue-deals" in why[0]
    ok, why = failing(lambda rel: real_text(rel) + (
        "\n\ndef build_customers_won(*a, **k):\n    return {}\n"
        if rel == "services/pipeline_service.py" else ""))
    assert ok is False and "services/pipeline_service.py defines or calls build_customers_won" in why[0]
    # A comment that says it was retired is not a reader.
    ok, why = failing(lambda rel: real_text(rel) + (
        "\n# build_customers_won and /api/revenue-deals are retired.\n"
        if rel in (audit.SERVER, "services/pipeline_service.py") else ""))
    assert ok is True, why


# ── 4. dead code 178B listed, deleted; no registry names a retired page ────────
def test_04_no_page_registry_names_a_retired_page_and_the_pipeline_page_names_its_datasets():
    from services.freshness_service import DATASET_FRESHNESS_CONFIG
    from services.system_status_service import PAGE_PIPELINE_IMPACT
    pages = set(re.findall(r'"([a-z-]+)"', re.search(r"const PAGES = \[(.*?)\];", JS,
                                                      re.S).group(1)))
    retired = set(re.findall(r"^\s*([a-z-]+|\"[a-z-]+\"):\s*\{\s*page:",
                             re.search(r"const RETIRED_PAGE_REDIRECTS = \{(.*?)\n\};",
                                       JS, re.S).group(1), re.M))
    retired = {r.strip('"') for r in retired}
    assert {"leads", "deals", "opportunities"} <= retired
    assert "pipeline" in pages
    # A retired page (a redirect key no longer in PAGES) is not a page the
    # impact map can report on: none remains there.
    gone = retired - pages
    assert not set(PAGE_PIPELINE_IMPACT) & gone, sorted(set(PAGE_PIPELINE_IMPACT) & gone)
    # The Pipeline page depends on what it reads: the canonical contact funnel.
    assert PAGE_PIPELINE_IMPACT["pipeline"] == ["contact_funnel", "lifecycle_events"]
    for ds in PAGE_PIPELINE_IMPACT["pipeline"]:
        assert ds in DATASET_FRESHNESS_CONFIG, ds
        assert DATASET_FRESHNESS_CONFIG[ds]["table"] == "hubspot_contact_funnel"
    # The frontend's own map names the same sync (hubspot/contact_funnel).
    assert '"hubspot/contact_funnel"' in re.search(
        r"\n  pipeline:\s*\[(.*?)\]", JS).group(1)
    # Every freshness `page` is a live page or none; the legacy `leads`
    # snapshot names none (the Pipeline page does not read it).
    for key, cfg in DATASET_FRESHNESS_CONFIG.items():
        assert cfg.get("page") not in gone, (key, cfg["page"])
    assert DATASET_FRESHNESS_CONFIG["leads"]["table"] == "leads"
    assert DATASET_FRESHNESS_CONFIG["leads"]["page"] is None
    assert DATASET_FRESHNESS_CONFIG["contact_funnel"]["page"] == "pipeline"
    assert DATASET_FRESHNESS_CONFIG["lifecycle_events"]["page"] == "pipeline"


def test_05_the_deleted_names_and_classes_are_gone_and_nothing_references_them():
    code = _js_code(JS)
    for name in DELETED_JS:
        assert not re.search(r"\b%s\b" % re.escape(name), code), name
    css_code = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    for cls in DELETED_CSS:
        assert not re.search(r"\.%s(?![A-Za-z0-9_-])" % re.escape(cls), css_code), cls
        assert not re.search(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % re.escape(cls),
                             code + HTML), cls
    # The scan that bites: every deal-* / leads-* / pipeline-* / revenue-diagnostic*
    # class styled in styles.css is drawn by app.js or index.html.
    page_code = code + HTML
    styled = set(re.findall(r"\.((?:deal|leads|pipeline)-[A-Za-z0-9_-]+)", css_code))
    unused = sorted(c for c in styled if not re.search(
        r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % re.escape(c), page_code))
    assert unused == [], unused


# ── 6. m6: the deal-source filter, per surface, per window, from the payload ───
def _m(value, status="published", reason=None):
    return {"value": value if status == "published" else None, "status": status,
            "reason": None if status == "published" else reason}


def _deals_payload(window="last_quarter", surface=None, breakdown="auto"):
    deals = [{"deal_id": f"d-{i}", "deal_name": f"Deal {i}", "acquisition_group": g,
              "deal_close_date": f"2026-08-{10 + i:02d}T09:00:00+00:00",
              "deal_amount_usd": 1000.0 * i, "campaign_name": None, "country": None}
             for i, g in ((1, "google_ads"), (2, "organic"), (3, "google_ads"))]
    if breakdown == "auto":
        breakdown = [
            {"source_group": "google_ads", "closed_won_deals": 2,
             "metrics": {"closed_won_deals": _m(2)}},
            {"source_group": "organic", "closed_won_deals": 1,
             "metrics": {"closed_won_deals": _m(1)}}]
    p = {"window": {"key": window}, "kpis": {"closed_won_deals": _m(3)},
         "closed_won_population": _m(3), "deals": deals, "deals_status": "published",
         "deals_reason": None, "source_breakdown": breakdown}
    if surface:
        p["surface"] = surface
    return p


def _rows(html):
    return re.findall(r'data-pipeline-deal="(d-\d)"', html)


def test_06_m6_each_surface_keeps_its_own_deal_source_choice_and_a_new_window_starts_clear():
    page = _deals_payload()
    dash = _deals_payload(surface="dashboard_deals")
    out = _run(
        "const page = %s, dash = %s;"
        "pipelineSetDealSource(dash, 'organic');"
        "const a = [pipelineDealsHtml(page), pipelineDealsHtml(dash)];"
        "pipelineSetDealSource(page, 'google_ads');"
        "const b = [pipelineDealsHtml(page), pipelineDealsHtml(dash)];"
        "const nextWindow = Object.assign({}, page, {window: {key: 'current_quarter'}});"
        "const c = [pipelineDealsHtml(nextWindow), pipelineDealSourceFor(nextWindow),"
        "           pipelineDealSourceFor(page)];"
        "[a, b, c]" % (json.dumps(page), json.dumps(dash)))
    (a_page, a_dash), (b_page, b_dash), (c_page, c_sel, c_old) = out
    # A choice on Dashboard → Deals never filters the Pipeline page …
    assert _rows(a_page) == ["d-3", "d-2", "d-1"] and _rows(a_dash) == ["d-2"]
    # … nor the reverse; each surface keeps its own.
    assert _rows(b_page) == ["d-3", "d-1"] and _rows(b_dash) == ["d-2"]
    # A different window opens on every deal source.
    assert _rows(c_page) == ["d-3", "d-2", "d-1"] and c_sel == ""
    assert "data-pipeline-deal-filter-note" not in c_page
    assert c_old == "google_ads"            # the window it was made in keeps it
    # The two surfaces' selects do not share an id.
    assert 'id="pipeline-deal-source"' in a_page
    assert 'id="dashboard_deals-deal-source"' in a_dash


def test_07_m6_the_per_group_counts_are_the_payloads_source_breakdown_never_a_row_count():
    # The breakdown is the ONE source: it says 5 / 4 here (a server-side
    # count), the rows hold 2 / 1 — the dropdown says 5 and 4.
    odd = _deals_payload(breakdown=[
        {"source_group": "google_ads", "metrics": {"closed_won_deals": _m(5)}},
        {"source_group": "organic", "metrics": {"closed_won_deals": _m(4)}}])
    unpublished = _deals_payload(breakdown=[
        {"source_group": "google_ads", "metrics": {"closed_won_deals": _m(
            None, "withheld", "canonical_ledger_stale")}},
        {"source_group": "organic", "metrics": {"closed_won_deals": _m(1)}}])
    none = _deals_payload(breakdown=None)
    out = _run("[%s].map((p) => pipelineDealsHtml(p))" % ", ".join(
        json.dumps(p) for p in (odd, unpublished, none)))
    options = [re.findall(r"<option[^>]*>(.*?)</option>", h) for h in out]
    assert options[0] == ["All deal sources (3)", "Google Ads (5)", "Organic (4)"]
    # A group whose count is not published says so — never a row count.
    assert options[1] == ["All deal sources (3)", "Google Ads (count not published)",
                          "Organic (1)"]
    # No breakdown: no filter is offered (it would need a second count).
    assert options[2] == [] and "data-pipeline-deal-source" not in out[2]
    assert _rows(out[2]) == ["d-3", "d-2", "d-1"]
    # The renderer counts no rows for the options.
    body = JS[JS.index("function pipelineDealsHtml("):]
    body = body[:body.index("\n}\n")]
    assert "rows.filter((r) => dealSourceKey(r) === g).length" not in body
    assert "pipelineDealSourceOptions(d)" in body


# ── 7. round-2 observation: lower-bound coverage per attribution scope ────────
def _contact(cid, stage, *, source="PAID_SEARCH", label="brand - uk", **dates):
    return {"contact_id": cid, "lifecycle_stage": stage, "hs_analytics_source": source,
            "hs_analytics_source_data_1": label, "created_at": date(2026, 8, 1), **dates}


def test_08_the_funnel_counts_undated_stage_reaches_per_scope_and_unknown_is_never_zero():
    from services import canonical_crm_funnel_service as f
    start, end = date(2026, 7, 1), date(2026, 9, 30)
    rows = [
        # A Google Ads contact at SQL with an SQL date: complete.
        _contact("c1", "salesqualifiedlead", date_entered_sql=date(2026, 8, 3),
                 date_entered_lead=date(2026, 8, 1), date_entered_mql=date(2026, 8, 2)),
        # An ORGANIC contact at SQL with no SQL date: a gap for all sources only.
        _contact("c2", "salesqualifiedlead", source="ORGANIC_SEARCH", label=None,
                 date_entered_lead=date(2026, 8, 1), date_entered_mql=date(2026, 8, 2)),
    ]
    pop = f.build_populations(rows, start, end, identity_available=True)
    cov = pop["coverage"]
    assert cov["stage_reached_without_entry_date"]["sql"] == 1
    by = cov["stage_reached_without_entry_date_by_scope"]["sql"]
    assert by["all_source"] == 1
    assert by["google_ads_source"] == 0
    assert set(by) == set(f.ORDERED_SCOPES)
    # Nested like the counts: a narrower scope never has more gaps.
    order = list(f.ORDERED_SCOPES)
    vals = [by[s] for s in order]
    assert vals == sorted(vals, reverse=True)
    # An identity outage: the narrow scopes' gaps are unknown (None), never 0.
    out = f.build_populations(rows, start, end, identity_available=False)
    by = out["coverage"]["stage_reached_without_entry_date_by_scope"]["sql"]
    for s in f.IDENTITY_DEPENDENT_SCOPES:
        assert by[s] is None, s
    assert by["all_source"] == 1


def test_09_the_pipeline_marks_a_lower_bound_only_where_the_selected_scope_has_a_gap():
    from services.pipeline_service import _funnel_coverage
    events = ("lead", "mql", "sql", "opportunity", "customer")
    whole = {e: 0 for e in events} | {"sql": 1}
    by_scope = {e: {"all_source": whole[e], "google_ads_source": 0,
                    "campaign_attributable": 0, "keyword_attributable": None}
                for e in events}
    cov = {"stage_reached_without_entry_date": whole,
           "stage_reached_without_entry_date_by_scope": by_scope}
    narrow = _funnel_coverage(cov, "google_ads_source")
    assert narrow == {"stage_reached_without_entry_date": {e: 0 for e in events},
                      "scope": "google_ads_source"}
    assert _funnel_coverage(cov, "all_source")["stage_reached_without_entry_date"] == whole
    # An unknown scope figure stays unknown (the page reads it as a bound).
    unknown = _funnel_coverage(cov, "keyword_attributable")
    assert set(unknown["stage_reached_without_entry_date"].values()) == {None}
    # A funnel with no per-scope figure passes the whole one (fails safe).
    assert _funnel_coverage({"stage_reached_without_entry_date": whole}, "google_ads_source") \
        == {"stage_reached_without_entry_date": whole, "scope": None}
    assert _funnel_coverage(None, "all_source") is None

    def funnel(coverage):
        return {"available": True, "events": {e: {"count": 3} for e in events},
                "conversions": [], "reconciliation": {"status": "partial"},
                "coverage": coverage}
    html = _run("[%s].map((f) => pipelineFunnelHtml({funnel: f}))" % ", ".join(
        json.dumps(funnel(c)) for c in (_funnel_coverage(cov, "all_source"), narrow, unknown)))
    marked = [re.findall(r'data-lower-bound="([a-z]+)"', h) for h in html]
    assert marked[0] == ["sql"]           # all sources: the organic gap
    assert marked[1] == []                # Google Ads-source: complete, exact
    assert marked[2] == list(events)      # unknown: every stage a bound
