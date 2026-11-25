"""PR-ADS-177C — the old campaign feed stays retired; one Keywords & Search
Terms page; the 177B review minors that need no database.

Campaign Evidence (``/api/campaigns``, ``/api/campaign-detail``,
``services/campaign_evidence_service``), the drawer's n-gram read
(``/api/search-terms/ngrams``), the orphaned ``/api/keywords``,
``/api/search-terms`` and ``/api/search-terms/summary``, and
``/api/revenue-attribution`` (its ``/audit`` stays) are deleted. Their truth
properties are asserted on the live surfaces in
``tests/test_pr_ads_177c_campaign_feed_port_pg_integration.py`` (``docs/60`` §9).

This module is the guard against bringing any of them back. Every case fails
on d543dcc (``docs/60`` §9.7): the routes are mounted, the handlers and the
service exist, the UI requests them, the certification is the Campaign
Evidence one, the monthly job writes the legacy keyword snapshot, there are
three Campaigns pages, and the 177B minors are as the review found them.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
INDEX = ROOT / "static" / "index.html"

RETIRED_ROUTES = (
    "/api/campaigns", "/api/campaign-detail", "/api/keywords",
    "/api/search-terms", "/api/search-terms/summary", "/api/search-terms/ngrams",
    "/api/revenue-attribution",
)
RETIRED_HANDLERS = (
    "api_campaigns", "api_campaign_detail_query", "_build_campaign_detail",
    "_campaign_keyword_preview", "_campaign_flagged_preview", "api_keywords",
    "api_search_terms", "api_search_terms_summary", "api_search_terms_ngrams",
    "get_revenue_attribution",
)
RETIRED_MODULE = "services.campaign_evidence_service"
RETIRED_FILES = (
    "services/campaign_evidence_service.py",
    "scripts/audit_campaign_evidence_certification.py",
    "scripts/screenshot_campaign_evidence.py",
    "scripts/campaign_evidence_fixtures.json",
    "scripts/benchmark_ngrams.py",
)
PRODUCTION_PACKAGES = ("analysis", "api", "connectors", "db", "scheduler", "scripts",
                       "services")


def _py_files(packages=PRODUCTION_PACKAGES + ("tests",)):
    for pkg in packages:
        for p in sorted((ROOT / pkg).rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p


def _route_paths() -> set:
    import api.server as server
    return {getattr(r, "path", None) for r in server.app.routes}


def _js_code_only(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _run_app(script: str, js: str | None = None):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app as run
    return run(script, js=js)


# ── 1. the routes ───────────────────────────────────────────────────────────
def test_01_the_retired_routes_are_gone_from_the_running_app_and_answer_404(monkeypatch):
    from fastapi.testclient import TestClient

    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client = TestClient(server.app, raise_server_exceptions=False)
    auth = {"Authorization": "Bearer secret-token"}
    paths = _route_paths()
    for path in RETIRED_ROUTES:
        assert path not in paths, path
        for query in ("", "?window=30d", "?days=30", "?window=current_quarter",
                      "?campaign_name=Brand"):
            assert client.get(path + query, headers=auth).status_code == 404, path + query
    # What replaces them is live.
    for kept in ("/api/revenue-performance", "/api/revenue-performance/campaign-detail",
                 "/api/dashboard/campaigns", "/api/meta-ads/adsets",
                 "/api/revenue-attribution/audit", "/api/search-term-evidence",
                 "/api/search-term-evidence/flagged", "/api/search-term-evidence/patterns",
                 "/api/keyword-evidence", "/api/keyword-evidence/refresh",
                 "/api/search-term-evidence/review"):
        assert kept in paths, kept


def test_02_no_handler_or_route_decorator_for_them_is_defined_anywhere():
    for p in _py_files(("api", "services")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not names & set(RETIRED_HANDLERS), (p, names & set(RETIRED_HANDLERS))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(
                    node.args[0], ast.Constant) and node.args[0].value in RETIRED_ROUTES:
                raise AssertionError(f"{p}: a route registers {node.args[0].value}")


def test_03_the_service_and_its_scripts_do_not_exist_and_nothing_imports_them():
    for rel in RETIRED_FILES:
        assert not (ROOT / rel).exists(), rel
    assert importlib.util.find_spec(RETIRED_MODULE) is None
    leaf = RETIRED_MODULE.rsplit(".", 1)[1]
    for p in _py_files():
        if p.name == Path(__file__).name:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name not in (RETIRED_MODULE,
                                          "scripts.audit_campaign_evidence_certification"), p
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in (RETIRED_MODULE,
                                           "scripts.audit_campaign_evidence_certification"), p
                if node.module == "services":
                    assert leaf not in {a.name for a in node.names}, p
            elif (isinstance(node, ast.Call) and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and node.args[0].value == RETIRED_MODULE):
                raise AssertionError(f"{p}: imports {RETIRED_MODULE} dynamically")
    # The section previews only the retired drawer used are gone too.
    import services.keyword_evidence_service as kw
    import services.search_term_evidence_service as st
    assert not hasattr(kw, "build_campaign_keyword_preview")
    assert not hasattr(st, "build_campaign_flagged_preview")


def test_04_the_ui_requests_no_retired_route_and_renders_no_campaign_evidence():
    for path in (APP_JS, INDEX):
        text = _js_code_only(path.read_text(encoding="utf-8"))
        for route in RETIRED_ROUTES:
            # The exact path, then a query string or the end of the literal —
            # never a longer live path (/api/revenue-attribution/audit is live).
            assert not re.search(re.escape(route) + r"(?=[?`'\"])", text), (path.name, route)
    js = APP_JS.read_text(encoding="utf-8")
    for gone in ("function loadCampaignEvidence", "function renderCampaignEvidencePage",
                 "function renderCampaignDrawer", "function openCampaignDrawer",
                 "function _appendDrawerEvidenceSections", "function loadCampaignDrawerNgrams",
                 "function renderMartSpendTruth", "function renderMartSummaryStrip"):
        assert gone not in js, gone
    assert 'id="campaign-drawer"' not in INDEX.read_text(encoding="utf-8")


def test_05_the_certification_gate_fails_if_a_retired_reader_comes_back(tmp_path):
    """The daily certification's S1 check is the runtime twin of this module.
    Named mutation: a copy of the tree that re-registers /api/campaigns and
    re-creates the service fails it; the real tree passes."""
    import scripts.audit_campaigns_page_certification as cert
    f = cert.Findings()
    cert.check_retired_readers_absent(f)
    assert f.exit_code == cert.EXIT_CERTIFIED, f.violations
    for sub in ("api", "static", "services", "scripts", "db", "analysis", "scheduler"):
        (tmp_path / sub).mkdir()
    shutil.copy(ROOT / "static" / "index.html", tmp_path / "static" / "index.html")
    (tmp_path / "static" / "app.js").write_text(
        APP_JS.read_text(encoding="utf-8") + '\nfetch(`/api/campaigns?window=30d`);\n')
    (tmp_path / "api" / "server.py").write_text(
        (ROOT / "api" / "server.py").read_text(encoding="utf-8")
        + '\n@app.get("/api/campaigns")\ndef api_campaigns():\n    return {}\n')
    (tmp_path / "services" / "campaign_evidence_service.py").write_text("X = 1\n")
    (tmp_path / "scripts" / "x.py").write_text(
        "from services.campaign_evidence_service import X\n")
    g = cert.Findings()
    cert.check_retired_readers_absent(g, root=tmp_path)
    assert g.exit_code == cert.EXIT_VIOLATION
    detail = " ".join(g.violations)
    for needle in ("route /api/campaigns is registered", "campaign_evidence_service.py exists",
                   "static/app.js requests /api/campaigns", "imports campaign_evidence_service"):
        assert needle in detail, (needle, detail)


def test_06_the_daily_certification_runs_the_campaigns_gate_not_the_retired_one():
    from scripts.audit_platform_certification import AUDITS
    keys = {k: m for k, m, _ in AUDITS}
    assert keys.get("campaigns") == "scripts.audit_campaigns_page_certification"
    assert "campaign_evidence" not in keys
    assert len(AUDITS) == 6       # one replaced, none dropped
    import importlib
    for _key, module, _proof in AUDITS:
        mod = importlib.import_module(module)
        assert callable(getattr(mod, "main", None)), module


def test_07_no_registry_or_audit_names_a_deleted_reader():
    from analysis import sql_doctrine_registry as reg
    for rule in reg.RULES:
        assert (ROOT / rule["path"]).exists(), rule["id"]
    for c in reg.CONSUMERS:
        path = c["code_location"].split(":", 1)[0]
        assert (ROOT / path).exists(), c["consumer"]
    for entry in reg.CPQL_CONSUMERS + reg.DECISION_SURFACES:
        assert "campaign_evidence_service" not in entry["code_location"]
    from services import cross_page_parity_service as parity
    assert parity.PENDING_REDESIGN_CONSUMERS["platform_evidence"]["services"] == [
        "keyword_evidence_service"]
    import scripts.audit_customer_revenue_reader_cutover as rc
    for item in rc.INVENTORY:
        for path in (item.get("consumers") or []) if isinstance(item, dict) else []:
            assert (ROOT / str(path).split(":", 1)[0]).exists(), path
    src = (ROOT / "scripts" / "audit_customer_revenue_reader_cutover.py").read_text()
    assert "campaign_evidence_service" not in src


# ── 2. the legacy `keywords` snapshot ───────────────────────────────────────
def test_08_the_monthly_job_no_longer_writes_the_legacy_keyword_snapshot():
    """Only OUR table: this write was to the local database (never Google Ads or
    HubSpot, which are read-only throughout). Its last reader went in 177B."""
    monthly = ast.parse((ROOT / "scheduler" / "monthly.py").read_text(encoding="utf-8"))
    calls = {getattr(n.func, "attr", getattr(n.func, "id", None))
             for n in ast.walk(monthly) if isinstance(n, ast.Call)}
    assert "write_keywords" not in calls
    assert "sync_keyword_daily_facts" in calls          # durable facts still synced
    batches = [n for n in ast.walk(monthly) if isinstance(n, ast.Call)
               and getattr(n.func, "attr", None) == "start_sync_batch"]
    datasets = {kw.value.value for n in batches for kw in n.keywords
                if kw.arg == "dataset" and isinstance(kw.value, ast.Constant)}
    assert "keywords" not in datasets and "geo" in datasets
    import db.writers as writers
    import db.revenue_repository as repo
    assert not hasattr(writers, "write_keywords")
    assert not hasattr(repo, "fetch_keyword_theme_snapshot")
    from services import freshness_service as fs
    assert fs.is_retired_dataset("google_ads_api", "keywords")
    assert "keywords" not in fs.DATASET_FRESHNESS_CONFIG
    assert "keywords" not in fs.MONTHLY_REFRESH_DATASETS
    from services import system_status_service as sss
    assert "keywords" not in sss.DATASET_REGISTRY if hasattr(sss, "DATASET_REGISTRY") else True
    assert "keywords" not in sss.PAGE_PIPELINE_IMPACT
    from analysis import legacy_source_guard as guard
    for rel in ("db/revenue_repository.py", "db/writers.py", "api/server.py",
                "services/dashboard_campaigns_service.py"):
        assert rel not in guard.LEGACY_ACCESS_ALLOWLIST, rel
    assert guard.scan_legacy_sources() == []


def test_09_the_evidence_window_bounds_moved_unchanged():
    from datetime import date, datetime, timezone
    from analysis.evidence_windows import EvidenceWindowError, evidence_window_bounds
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    start, end, resolved = evidence_window_bounds("7d", now)
    assert (start, end) == (date(2026, 7, 7), date(2026, 7, 13)) and resolved["days"] == 7
    assert evidence_window_bounds("all_time", now)[0] is None
    with pytest.raises(EvidenceWindowError):
        evidence_window_bounds("90d", now)
    import services.keyword_evidence_service as kw
    import services.search_term_evidence_service as st
    assert kw._window_bounds is evidence_window_bounds
    assert st._window_bounds is evidence_window_bounds
    from services import campaign_label_resolver as clr
    import services.canonical_customer_revenue_service as ccr
    assert "campaign_label_resolver" in (ROOT / "services"
                                          / "canonical_customer_revenue_service.py").read_text()
    # The campaign-ID rule, unchanged: two ids sharing a name are never merged.
    _, norm = clr.spend_by_campaign_id({"rows": [
        {"campaign_id": 1, "campaign_name": "Brand"}, {"campaign_id": 2, "campaign_name": "brand"}]})
    assert clr.assign_label("Brand", {}, norm) == ("unmatched", "brand")
    _, norm1 = clr.spend_by_campaign_id({"rows": [{"campaign_id": 1, "campaign_name": "Brand"}]})
    assert clr.assign_label(" BRAND ", {}, norm1) == ("google_ads", "1")
    assert clr.assign_label("Offline", {"offline": {"match_method": "not_google_ads"}},
                            norm1) == ("not_google_ads", None)
    assert ccr.campaign_resolver is not None


# ── 3. one Keywords & Search Terms page ─────────────────────────────────────
def test_10_one_keywords_and_search_terms_page_under_campaigns():
    out = _run_app(
        "JSON.parse(JSON.stringify({items: NAV_ITEMS.find((i) => i.id === 'campaigns').pages, "
        "redirect: RETIRED_PAGE_REDIRECTS.keywords, inPages: PAGES.includes('keywords'), "
        "tabs: ST_TABS, evidence: EVIDENCE_PAGES, deps: PAGE_DEPENDENCIES['search-terms'], "
        "nav: navItemForPage('keywords'), title: PAGE_EXPLANATIONS['search-terms'].title, "
        "help: Object.keys(PAGE_HELP_CONTENT).includes('keywords'), "
        "related: datasetRelatedPage('google_ads_api', 'keyword_facts')}))")
    assert out["items"] == ["campaigns", "search-terms"]
    assert out["redirect"] == {"page": "search-terms", "tab": "keywords"}
    assert out["inPages"] is False and out["nav"] == "campaigns"
    assert out["tabs"] == ["terms", "flagged", "patterns", "keywords"]
    assert "keywords" not in out["evidence"]
    assert "google_ads_api/keyword_facts" in out["deps"]
    assert out["title"] == "Keywords & Search Terms" and out["help"] is False
    assert out["related"]["page"] == "search-terms"
    html = INDEX.read_text(encoding="utf-8")
    assert "<h1 class=\"page-title\">Keywords &amp; Search Terms</h1>" in html
    assert 'id="page-keywords"' not in html and 'data-page="keywords"' not in html
    strip = html[html.index('data-section="campaigns"'):]
    strip = strip[:strip.index("</nav>")]
    assert re.findall(r'data-page="([^"]+)"', strip) == ["campaigns", "search-terms"]
    assert "Keywords &amp; Search Terms" in strip


def test_11_the_shell_renders_four_tabs_and_the_keywords_tab_renders_inside_it():
    out = _run_app(
        "_stTab = 'keywords'; renderSearchTermsShell(); "
        'const shell = document.getElementById("search-terms-shell").innerHTML; '
        "let kwLoads = 0; loadKeywordHistory = () => {}; loadKeywordTab = () => { kwLoads += 1; }; "
        "loadKeywordsTab(); "
        'JSON.parse(JSON.stringify({shell, body: document.getElementById("st-tab-body").innerHTML, kwLoads}))')
    tabs = re.findall(r'data-tab="([a-z]+)"', out["shell"])
    assert tabs == ["terms", "flagged", "patterns", "keywords"]
    labels = re.findall(r'type="button"[^>]*>([^<]+)</button>', out["shell"])
    assert labels == ["Terms", "Flagged", "Patterns", "Keywords"]
    assert "Keywords &amp; Search Terms tabs" in out["shell"]
    assert 'id="kw-tab-body"' in out["body"] and out["kwLoads"] == 1
    js = APP_JS.read_text(encoding="utf-8")
    assert '"Open Keywords & Search Terms"' in js
    assert '["keywords", "Open Keywords"]' not in js
    assert "Review in Keywords &amp; Search Terms" in js


# ── 4. the 177B minors that need no database ────────────────────────────────
def test_12_m5_a_spend_not_settled_row_has_its_next_action_and_status_colour():
    out = _run_app("JSON.parse(JSON.stringify({a: CAMP_NEXT_ACTION['Spend not settled'], "
                   "m: CAMP_STATUS_META['Spend not settled']}))")
    assert out["a"] == "Not judged — spend not settled"
    assert out["m"]["dot"] == "camp-dot--muted"
    src = (ROOT / "services" / "dashboard_campaigns_service.py").read_text(encoding="utf-8")
    # The stale 177B text: a None ratio over known revenue and spend was
    # "not yet meaningful … never a fabricated 0.00x" — but zero revenue over
    # positive spend IS a measured 0.00x now.
    assert "not yet meaningful" not in src
    assert "no closed-won revenue yet to measure" not in src


def test_13_m6_the_revenue_scope_sentence_is_plain_words_and_says_withheld():
    data = {"summary": {
        "attributed_revenue_scope": "campaign_attributable", "revenue_scope": "all_source",
        "attributed_won_revenue_usd": None, "won_revenue_usd": None,
        "metrics": {"attributed_revenue_usd": {"value": None, "status": "withheld",
                                               "reason": "closed_won_deals_missing_proven_usd_amount"},
                    "revenue_usd": {"value": None, "status": "unavailable",
                                    "reason": "canonical_ledger_stale"}}}}
    html = _run_app("renderRevenueScopeDisclosure(%s)" % json.dumps(data))
    for raw in ("campaign_attributable", "all_source</", ">all_source", "<strong>all_source"):
        assert raw not in html.replace('data-revenue-scope="campaign_attributable"', ""), raw
    assert "placed on a Google Ads campaign" in html and "every source" in html
    assert "Withheld" in html and "Unavailable" in html
    assert html.index("Withheld") < html.index("Unavailable")
    pub = {"summary": {**data["summary"], "attributed_won_revenue_usd": 4000.0,
                       "won_revenue_usd": 9000.0}}
    shown = _run_app("renderRevenueScopeDisclosure(%s)" % json.dumps(pub))
    assert "$4.0k" in shown and "$9.0k" in shown and "Withheld" not in shown


def test_14_m7_the_meta_table_always_says_whose_days_it_counts():
    for state in ({"status": "withheld", "reason": "meta_adset_unverified", "rows": [],
                   "day_basis_disclosure": None},
                  None,
                  {"status": "published", "rows": [], "totals": None,
                   "day_basis_disclosure": "Each Meta ad account is read on its own calendar "
                   "days: 1 on America/Los_Angeles. These are not the Europe/London days the "
                   "Google Ads table counts on; nothing on this page is divided by Meta spend."}):
        html = _run_app("renderMetaAdsetPanel(%s)" % json.dumps(state))
        assert "own calendar days" in html and "Europe/London days" in html, state
    from services.meta_ads_adset_table_service import day_basis_disclosure
    text = day_basis_disclosure([{"account_id": "4730305973961806",
                                  "time_zone": "America/Los_Angeles"},
                                 {"account_id": "944370805110952", "time_zone": "Asia/Amman"}])
    assert "4730305973961806 on America/Los_Angeles" in text
    assert "different 24 hours" in text and "Europe/London days" in text
    assert "Europe/London days" in day_basis_disclosure([])


def test_15_claude_md_states_the_zero_revenue_rule_once_and_correctly():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Those rows return\n  None, not 0, for zero revenue." not in text
    assert "None, not 0, for zero revenue" not in text.replace("\n  ", " ")
    flat = " ".join(text.split())
    assert ("A published zero revenue over verified positive spend is a published 0.00 "
            "with its disclosure; unknown or withheld revenue, or zero or unknown spend, "
            "is never 0.") in flat
    assert ("The Campaigns and Countries dashboard rows republish the mart row's "
            "reader-contract ratio") in flat


def test_16_o1_the_dashboard_countries_ratio_rule_publishes_a_measured_zero():
    from services.dashboard_countries_service import _period_ratio
    assert _period_ratio(0.0, 1250.0) == 0.0
    assert _period_ratio(2500.0, 1250.0) == 2.0
    assert _period_ratio(None, 1250.0) is None
    assert _period_ratio(0.0, 0.0) is None
    assert _period_ratio(100.0, None) is None
