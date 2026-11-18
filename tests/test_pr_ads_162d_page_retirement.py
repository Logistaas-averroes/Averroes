"""PR-ADS-162D — two pages nothing depended on are deleted, not hidden.

* **Historical Trends** (`#/historical-intelligence`) had no sidebar entry and
  no link from any page. It published a CPQL "improving / deteriorating"
  verdict from ``COALESCE(confirmed_sqls, 0)`` over the legacy ``campaigns``
  snapshot — a missing value turned into a zero, and a third SQL definition.
* **Churn Input** stored a monthly churn rate in a local YAML file. The
  canonical Unit Economics service never read it: LTV, CAC and payback are
  withheld there for want of recurring revenue, whatever the churn rate is.

Deleting a page must leave no half of it behind: no route, no section, no
loader, no endpoint, no registry entry — and an old bookmark must land on the
Dashboard rather than a blank screen.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static/styles.css").read_text(encoding="utf-8")
SERVER = (ROOT / "api/server.py").read_text(encoding="utf-8")

RETIRED = {
    "historical-intelligence": {
        "endpoints": ["/api/historical-intelligence"],
        "js": ["loadHistoricalIntelligence", "_renderHistoricalTable",
               "_hiMovementArrow", "_HI_TREND_LABELS", "hi-entity-select"],
        "files": ["analysis/historical_intelligence.py"],
    },
    "churn-input": {
        "endpoints": ["/api/admin/churn-input"],
        "js": ["loadChurnInput", "renderChurnInputPage",
               "handleChurnInputSubmit", "churnInputData", "nav-churn-input-item"],
        "files": [],
    },
}


def _pages() -> set:
    return set(re.findall(r'"([a-z0-9-]+)"',
                          re.search(r"const PAGES = \[(.*?)\];", JS, re.S).group(1)))


@pytest.mark.parametrize("page", sorted(RETIRED))
def test_01_the_page_has_no_route_section_or_sidebar_entry(page):
    assert page not in _pages()
    assert f'id="page-{page}"' not in HTML
    assert f'data-page="{page}"' not in HTML
    assert f'case "{page}":' not in JS
    assert f'"{page}"' not in JS, "a leftover reference to the retired page key"
    assert page not in CSS


@pytest.mark.parametrize("page", sorted(RETIRED))
def test_02_no_loader_state_or_wiring_is_left_behind(page):
    for symbol in RETIRED[page]["js"]:
        assert symbol not in JS, symbol
        assert symbol not in HTML, symbol
    for path in RETIRED[page]["files"]:
        assert not (ROOT / path).exists(), path


@pytest.mark.parametrize("page", sorted(RETIRED))
def test_03_the_endpoints_are_gone_from_the_running_app(page, monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client = TestClient(server.app)
    auth = {"Authorization": "Bearer secret-token"}
    for path in RETIRED[page]["endpoints"]:
        assert path not in SERVER
        assert client.get(path, headers=auth).status_code == 404
        assert client.post(path, headers=auth, json={}).status_code in (404, 405)
        assert path not in {r.path for r in server.app.routes}


def test_04_an_old_bookmark_lands_on_the_dashboard():
    """navigate() sends any key outside PAGES to the Dashboard. Evaluate the
    real guard with the real PAGES list rather than trusting the layout."""
    pages = re.search(r"const PAGES = \[.*?\];", JS, re.S).group(0)
    guard = "if (!PAGES.includes(page) || adminOnlyDenied(page)) {"
    assert guard in JS
    after = JS[JS.index(guard):][:140]
    assert 'navigate("dashboard", { updateHash: true, replace: true });' in after
    script = pages + (
        "\nconst out = {};"
        "\nfor (const p of ['historical-intelligence', 'churn-input', 'channels', 'system'])"
        " out[p] = PAGES.includes(p);"
        "\nprocess.stdout.write(JSON.stringify(out));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == ('{"historical-intelligence":false,"churn-input":false,'
                          '"channels":true,"system":true}')


def test_05_every_remaining_sidebar_page_still_routes():
    nav = set(re.findall(r'class="nav-item[^"]*" data-page="([a-z0-9-]+)"', HTML))
    pages = _pages()
    assert nav <= pages, sorted(nav - pages)
    for page in sorted(nav):
        assert f'id="page-{page}"' in HTML, page
        assert f'case "{page}":' in JS, page
    # every page section still present has a route (no orphaned markup)
    sections = set(re.findall(r'<section id="page-([a-z0-9-]+)"', HTML))
    assert sections <= pages, sorted(sections - pages)


def test_06_the_legacy_sql_surface_left_the_doctrine_registry():
    from pathlib import Path as _P
    from scripts import audit_sql_doctrine_inventory as inv
    registry = (ROOT / "analysis/sql_doctrine_registry.py").read_text()
    assert "/api/historical-intelligence" not in registry
    assert "analysis/historical_intelligence.py" not in registry
    static = inv.build_static(_P(ROOT))
    assert static["unclassified_occurrences"] == []
    assert not static["registry_problems"]


def test_07_no_page_still_points_the_reader_at_a_retired_page():
    """Unit Economics help told the reader LTV used the churn rate and sent
    them to Churn Input. LTV is withheld there; the pointer was false."""
    assert "Churn Input" not in JS
    assert "configured churn rate" not in JS
    assert "Historical Trends" not in JS and "Historical Trends" not in HTML
