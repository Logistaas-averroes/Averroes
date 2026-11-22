"""PR-ADS-168B — the Revenue by Source page is deleted, not hidden.

PR-ADS-168A put closed-won deals, customers and revenue per channel on the
Channels page through the reader contract, so Revenue by Source became a
second page answering the same question with a different taxonomy. Deleting a
page must leave no half of it behind (the PR-ADS-162D rule):

* no sidebar entry, section, loader, renderer, drawer, CSS or route key;
* its two routes are gone from the running app (404, the convention of every
  route this repo has retired — PR-ADS-162B/162D/165A), and the mart's
  ``source`` view, which only that page read, is a 400 like any unknown view;
* an old ``#/revenue-by-source`` bookmark lands on Channels, not the Dashboard;
* the one panel that lived only there — the admin Source Attribution Backfill —
  is on Revenue Health now;
* every Dashboard link that pointed at it opens Channels;
* Channels still carries per-channel won deals, customers and revenue.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static/styles.css").read_text(encoding="utf-8")
SERVER = (ROOT / "api/server.py").read_text(encoding="utf-8")

PAGE = "revenue-by-source"
ROUTES = ("/api/revenue-by-source", "/api/revenue-performance/source-platform-detail")
DELETED_JS = (
    "loadRevenueBySource", "renderRevenueBySourcePage", "renderRevenueBySourceHealth",
    "renderSourceGroupSection", "renderSourceChannelRows", "renderSourcePlatformRow",
    "sourcePlatformExpandControl", "renderSourceSpendProofChip", "sourceStatusLabel",
    "sourceGaSqlsChip", "fmtSourceRate", "sourceDrilldownCache", "sourceDrilldownKey",
    "renderSourceDrilldown", "renderSourceDrilldownRow", "renderSourceContactRow",
    "loadSourceDrilldown", "data-source-expand", "sourceGroupDealCount", "bySource",
    "source-platform-detail",
)
DELETED_PY = (
    ("services/source_attribution_service.py", "build_source_platform_detail"),
    ("services/source_attribution_service.py", "_source_detail_row"),
    ("services/source_attribution_service.py", "_source_contact_row"),
    ("db/revenue_repository.py", "fetch_source_contact_details"),
    ("api/server.py", "get_revenue_by_source"),
    ("api/server.py", "get_revenue_performance_source_platform_detail"),
)


def _js_const(name: str) -> str:
    m = re.search(rf"const {name} = (\[.*?\]|\{{.*?\n\}});", JS, re.S)
    assert m, name
    return m.group(0)


def _node(script: str) -> str:
    node = shutil.which("node")
    assert node, "node is required: CI runs `node --check static/app.js`"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _section(page_id: str) -> str:
    start = HTML.index(f'<section id="page-{page_id}"')
    return HTML[start:HTML.index("</section>", start)]


def _fn(name: str) -> str:
    start = JS.index(f"function {name}(")
    return JS[start:JS.index("\n}\n", start) + 3]


# ── 1. the routes ─────────────────────────────────────────────────────────


def test_01_the_two_routes_are_gone_from_the_running_app(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client = TestClient(server.app)
    auth = {"Authorization": "Bearer secret-token"}
    paths = {r.path for r in server.app.routes}
    for path in ROUTES:
        assert f'"{path}"' not in SERVER, path
        assert path not in paths, path
        assert client.get(path, headers=auth).status_code == 404, path
        assert client.get(path + "?window=ytd&source_group=organic&source_channel=x"
                          "&source_platform=y", headers=auth).status_code == 404
    # The routes the page shared with others are untouched.
    for kept in ("/api/revenue-performance", "/api/revenue-performance/audit",
                 "/api/source-attribution-backfill/run",
                 "/api/source-attribution-backfill/status",
                 "/api/source-attribution-health", "/api/channel-funnel"):
        assert kept in paths, kept


def test_02_the_mart_source_view_is_an_unknown_view():
    from services.revenue_decision_mart import VALID_VIEWS, build_revenue_decision_mart
    assert VALID_VIEWS == ("campaign", "country", "deal")
    with pytest.raises(ValueError, match="Invalid view 'source'"):
        build_revenue_decision_mart(view="source", window="ytd")
    # The audit no longer compares a page that does not exist.
    mart_src = (ROOT / "services/revenue_decision_mart.py").read_text()
    assert '"revenue_by_source"' not in mart_src
    assert "source_spend_truth" not in mart_src


def test_03_the_route_endpoint_rejects_view_source(monkeypatch):
    import os
    from fastapi.testclient import TestClient
    from api.auth import set_session
    from starlette.responses import Response as StarletteResponse
    import api.server as server
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    r = StarletteResponse()
    set_session(r, "viewer168b", "viewer")
    cookie = next(p.split("=", 1)[1] for p in r.headers["set-cookie"].split(";")
                  if p.strip().startswith("ads_session="))
    client = TestClient(server.app, raise_server_exceptions=False)
    res = client.get("/api/revenue-performance?view=source&window=ytd",
                     cookies={"ads_session": cookie})
    assert res.status_code == 400
    assert "Invalid view 'source'" in res.text


def test_04_code_used_only_by_the_routes_is_deleted():
    for rel, name in DELETED_PY:
        tree = ast.parse((ROOT / rel).read_text())
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert name not in defined, f"{rel}:{name}"
    # build_revenue_by_source itself stays: the Dashboard breakdowns read it.
    from services.source_attribution_service import build_revenue_by_source  # noqa: F401
    for rel in ("services/dashboard_overview_service.py",
                "services/dashboard_revenue_service.py",
                "services/dashboard_deals_service.py",
                "services/dashboard_channels_service.py"):
        assert "build_revenue_by_source(" in (ROOT / rel).read_text(), rel


# ── 2. the page ───────────────────────────────────────────────────────────


def test_05_no_nav_entry_section_route_key_or_css_is_left():
    assert f'data-page="{PAGE}"' not in HTML
    assert f'id="page-{PAGE}"' not in HTML
    assert PAGE not in HTML
    assert "Revenue by Source" not in re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
    assert f'"{PAGE}"' not in _js_const("PAGES")
    assert f'"{PAGE}"' not in _js_const("REVENUE_PAGES")
    assert f'case "{PAGE}":' not in JS
    for symbol in DELETED_JS:
        assert symbol not in JS, symbol
        assert symbol not in HTML, symbol
    for cls in (".revenue-source-section", ".source-group-header",
                ".source-taxonomy-table", ".source-drilldown", ".source-expand-button",
                ".source-proof-chip"):
        assert cls not in CSS, cls
    assert PAGE not in CSS


def test_06_nothing_in_static_references_the_page_but_the_redirect():
    hits = []
    for f in sorted((ROOT / "static").rglob("*")):
        if f.is_file() and f.suffix in (".js", ".html", ".css", ".json", ".svg"):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if PAGE in line:
                    hits.append((f.name, i, line.strip()))
    assert len(hits) == 1, hits
    name, _, line = hits[0]
    assert name == "app.js"
    assert line == f'"{PAGE}": {{ page: "channels" }},'
    assert line in _js_const("RETIRED_PAGE_REDIRECTS")


# ── 3. the redirect ───────────────────────────────────────────────────────


def test_07_an_old_bookmark_lands_on_channels():
    """Evaluate the real router: navigate() consults RETIRED_PAGE_REDIRECTS
    before the PAGES fallback that would send an unknown key to the Dashboard."""
    nav = JS[JS.index("function navigate(page, options) {"):]
    redirect_at = nav.index("const retired = RETIRED_PAGE_REDIRECTS[page];")
    fallback_at = nav.index("if (!PAGES.includes(page) || adminOnlyDenied(page)) {")
    assert redirect_at < fallback_at
    script = "\n".join([
        _js_const("PAGES"), _js_const("RETIRED_PAGE_REDIRECTS"),
        "const seen = [];",
        # The real navigate's first two decisions, verbatim order: redirect, then
        # the PAGES fallback. Anything else the real function does is not under test.
        "function navigate(page) {",
        "  const retired = RETIRED_PAGE_REDIRECTS[page];",
        "  if (retired) { seen.push(page); return navigate(retired.page); }",
        "  if (!PAGES.includes(page)) { seen.push(page); return navigate('dashboard'); }",
        "  seen.push(page); return page;",
        "}",
        f"const landed = navigate('{PAGE}');",
        "process.stdout.write(JSON.stringify({landed, seen,"
        f" inPages: PAGES.includes('{PAGE}')}}));",
    ])
    out = json.loads(_node(script))
    assert out == {"landed": "channels", "seen": [PAGE, "channels"], "inPages": False}


def test_08_the_redirect_target_is_a_real_page():
    assert 'data-page="channels"' in HTML
    assert 'id="page-channels"' in HTML
    assert 'case "channels":' in JS


# ── 4. what moved ─────────────────────────────────────────────────────────


def test_09_the_backfill_panel_moved_to_revenue_health():
    assert 'id="source-backfill-panel"' in _section("revenue-health")
    assert HTML.count('id="source-backfill-panel"') == 1
    load = _fn("loadRevenueHealth")
    assert "renderSourceBackfillPanelInit();" in load
    poll = _fn("pollSourceBackfillOnce")
    assert '_currentPage !== "revenue-health"' in poll
    assert "loadRevenueHealth();" in poll


def test_10_the_backfill_panel_renders_for_an_admin_and_not_for_a_viewer():
    """Run the real renderSourceBackfillPanel against a stub element."""
    script = "\n".join([
        "const el = { innerHTML: 'untouched' };",
        "const document = { getElementById: (id) => id === 'source-backfill-panel' ? el : null };",
        "const escapeHtml = (s) => String(s);",
        "const fmtCount = (n) => String(n);",
        "let sourceBackfillNotice = '';",
        "let _currentUser = { role: 'admin' };",
        _fn("renderSourceBackfillPanel"),
        "renderSourceBackfillPanel({ running: false, summary: { contacts_classified: 7,"
        " deals_attributed: 3, ambiguous_deals: 1, unclassified_deals: 0, failed: 0 } });",
        "const admin = el.innerHTML;",
        "_currentUser = { role: 'viewer' };",
        "renderSourceBackfillPanel({ running: false });",
        "process.stdout.write(JSON.stringify({ admin, viewer: el.innerHTML }));",
    ])
    out = json.loads(_node(script))
    assert "Source Attribution Backfill" in out["admin"]
    assert 'data-source-backfill-action="preview"' in out["admin"]
    assert 'data-source-backfill-action="run"' in out["admin"]
    assert "Contacts classified" in out["admin"] and ">7<" in out["admin"]
    assert out["viewer"] == ""


# ── 5. links ──────────────────────────────────────────────────────────────


def test_11_every_dashboard_target_page_is_a_real_page():
    pages = set(re.findall(r'"([a-z0-9-]+)"', _js_const("PAGES")))
    found = set()
    for f in (ROOT / "services").glob("dashboard_*.py"):
        src = f.read_text()
        assert PAGE not in src, f.name
        assert "Open Revenue by Source" not in src, f.name
        found |= set(re.findall(r'"target_page":\s*"([a-z0-9-]+)"', src))
    assert "channels" in found
    assert found <= pages, sorted(found - pages)
    # The Dashboard Revenue tab's source breakdown opens Channels.
    assert 'b.by_source, "source_group", "channels"' in JS


# ── 6. registries ─────────────────────────────────────────────────────────


def test_12_the_reader_inventory_still_names_every_live_reader():
    from scripts import audit_customer_revenue_reader_cutover as audit
    surfaces = " ".join(r["surface"] for r in audit.INVENTORY)
    assert "/api/revenue-by-source;" not in surfaces
    names = [n for n, _ in audit._page_builders()]
    assert "revenue_decision_mart_source" not in names
    # The surviving source reader is still exercised by the audit's page pass.
    assert "revenue_by_source" in names
    assert any(r["consumer"] == "services/source_attribution_service.py"
               for r in audit.INVENTORY)


def test_13_the_sql_doctrine_registry_names_no_deleted_symbol():
    from scripts import audit_sql_doctrine_inventory as inv
    registry = (ROOT / "analysis/sql_doctrine_registry.py").read_text()
    for gone in ("build_source_platform_detail", "fetch_source_contact_details",
                 "get_revenue_by_source", "/api/revenue-by-source",
                 "source-platform-detail", "renderSourceGroupSection"):
        assert gone not in registry, gone
    static = inv.build_static(ROOT)
    assert static["unclassified_occurrences"] == []
    assert not static["registry_problems"]


# ── 7. Channels still answers the question ────────────────────────────────


def test_14_channels_carries_per_channel_deals_customers_and_revenue():
    """The real channelOutcomeCellsHtml renders the three canonical figures per
    channel row, each through rowMetricHtml (status + reason, never a dash)."""
    script = "\n".join([
        "const calls = [];",
        "function rowMetricHtml(r, key) { calls.push(key); return `[${key}]`; }",
        "function canonicalMetricHtml() { return '[spend]'; }",
        "const channelOutcomeInt = 0, channelOutcomeMoney = 0, channelOutcomeRatio = 0;",
        _fn("channelOutcomeCellsHtml"),
        "const html = channelOutcomeCellsHtml({}, {});",
        "process.stdout.write(JSON.stringify({ calls, html }));",
    ])
    out = json.loads(_node(script))
    assert out["calls"][:3] == ["closed_won_deals", "customers", "won_revenue"]
    assert "[closed_won_deals]" in out["html"] and "[won_revenue]" in out["html"]
    # And the server builds those rows through the reader contract.
    import inspect
    from services import source_attribution_service as sas
    src = inspect.getsource(sas.build_channel_outcomes)
    assert "load_won_deals" in src
    assert "reader_row" in inspect.getsource(sas)
    assert "build_channel_outcomes" in SERVER
