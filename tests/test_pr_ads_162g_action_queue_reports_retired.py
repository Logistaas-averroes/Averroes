"""PR-ADS-162G — the Action Queue and Reports pages are deleted, not hidden.

Owner decision. Both pages were built on Google-Ads-only legacy data and an
older SQL definition:

* **Action Queue** (`#/action-queue`, ``GET /api/action-queue``) ranked
  "review this" items from the legacy ``campaigns`` snapshot
  (``confirmed_sqls`` from an undated, undeduplicated JSON pull), raw ``leads``
  rows windowed on ``run_date`` and the legacy ``keywords`` snapshot. Its one
  canonical input — flagged search terms — is the Search Terms → Flagged view.
* **Reports** (`#/reports`, ``GET /reports/latest``, ``/reports/latest/raw``)
  displayed the latest generated markdown file: the weekly/monthly advisor
  report, whose verdicts come from that same third SQL definition.

A deletion is only clean if nothing still points at what was deleted. The check
that matters most here is ``test_05``: a Dashboard card whose ``target_page`` is
not a real page renders a link that silently bounces to the Dashboard.

Out of scope, and pinned as untouched by ``test_11``: the report GENERATION
pipeline (scheduler/weekly.py, scheduler/monthly.py, analysis/core.py,
analysis/rule_advisor.py, analysis/advisor.py).
"""
from __future__ import annotations

import ast
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
    "action-queue": {
        "endpoints": ["/api/action-queue"],
        "js": ["loadActionQueue", "_renderActionQueueKPIs", "_getFilteredQueueItems",
               "_renderActionQueueList", "_renderQueueItemCard",
               "applyActionQueueFilters", "QUEUE_TYPE_LABELS", "actionQueueItems",
               "actionQueueStatus", "action_queue", "aq-filter-severity",
               "aq-filter-type", "aq-kpi-", "queue-action-btn", "Action Queue"],
        "css": ["action-queue", "queue-type-badge", "queue-evidence",
                "queue-score-note", "queue-source", "queue-readonly-actions",
                "severity-border-"],
        "server": ["api_action_queue", "_build_campaign_queue_items",
                   "_build_waste_queue_items", "_build_geo_queue_items",
                   "_build_keyword_queue_items", "_build_data_quality_items",
                   "_queue_severity_label", "_queue_id", "_QUEUE_",
                   "_evidence_window_for_days"],
    },
    "reports": {
        "endpoints": ["/reports/latest", "/reports/latest/raw"],
        "js": ["loadReports", "copyLatestReport", "updateReportActionButtons",
               "_renderReportsMeta", "latestReportMarkdown", "latestReportMeta",
               "reportLoadStatus", "reportCopyFeedbackTimer", "report-copy-btn",
               "report-refresh-btn", "reports-viewer", "reports-meta",
               "fetchText"],
        "css": ["report-meta", "report-actions-panel", "report-viewer",
                "report-markdown"],
        "server": ["reports_latest", "reports_latest_raw", "_latest_report_path"],
    },
}


def _pages(js: str = JS) -> set:
    return set(re.findall(r'"([a-z0-9-]+)"',
                          re.search(r"const PAGES = \[(.*?)\];", js, re.S).group(1)))


@pytest.mark.parametrize("page", sorted(RETIRED))
def test_01_the_page_has_no_route_section_or_sidebar_entry(page):
    assert page not in _pages()
    assert f'id="page-{page}"' not in HTML
    assert f'data-page="{page}"' not in HTML
    assert f'id="page-explanation-{page}"' not in HTML
    assert f'case "{page}":' not in JS
    assert f'"{page}"' not in JS, "a leftover reference to the retired page key"
    # No explanation, help, dataset or dependency map still keys the page.
    assert not re.search(rf'^\s*"?{re.escape(page)}"?\s*:', JS, re.M)
    assert not re.search(rf'^\s*{page.replace("-", "_")}\s*:', JS, re.M)


@pytest.mark.parametrize("page", sorted(RETIRED))
def test_02_no_loader_state_style_or_wiring_is_left_behind(page):
    for symbol in RETIRED[page]["js"]:
        assert symbol not in JS, symbol
        assert symbol not in HTML, symbol
    for cls in RETIRED[page]["css"]:
        assert cls not in CSS, cls
        assert cls not in JS, cls
        assert cls not in HTML, cls
    for name in RETIRED[page]["server"]:
        assert name not in SERVER, name


@pytest.mark.parametrize("path", [p for page in sorted(RETIRED)
                                  for p in RETIRED[page]["endpoints"]])
def test_03_the_endpoints_are_gone_from_the_running_app(path, monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client = TestClient(server.app)
    auth = {"Authorization": "Bearer secret-token"}
    assert path not in SERVER
    assert path not in {r.path for r in server.app.routes}
    assert client.get(path, headers=auth).status_code == 404
    assert client.get(path + "?days=30", headers=auth).status_code == 404
    assert client.post(path, headers=auth, json={}).status_code in (404, 405)
    # …and no page still fetches them.
    assert path not in JS


def test_04_no_visible_text_points_at_a_deleted_page():
    """Search Terms said a term was focused "from an Action Queue link" and
    that no "Action Queue item" came from quarantined evidence; the Dashboard
    help sent the reader to the Action Queue. None of that may remain."""
    for text, label in ((JS, "app.js"), (HTML, "index.html")):
        assert "Action Queue" not in text, label
        assert "action queue" not in text.lower(), label
        for link in ("#/action-queue", "#/reports", 'navigate("action-queue"',
                     'navigate("reports"', 'data-navigate="action-queue"',
                     'data-navigate="reports"'):
            assert link not in text, (label, link)
    # The sidebar and page titles no longer offer either page.
    assert ">Reports<" not in HTML
    assert 'aria-label="Reports"' not in HTML
    # The focus notice still exists, and no longer names a page.
    notice = JS.split("function flagFocusNotice()")[1].split("\n}")[0]
    assert "_flagFocusTerm" in notice
    assert "Queue" not in notice


# ── Dashboard cards: every target is a real page ─────────────────────────────
def _target_pages(source: str, filename: str = "<src>") -> tuple[set, list]:
    """Every ``target_page`` value a service can emit, and every one this
    reader could NOT resolve to a literal (which fails the test: an unread
    target is not evidence of a valid one)."""
    literals, unresolved = set(), []
    tree = ast.parse(source, filename=filename)

    def _take(value, where):
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            literals.add(value.value)
        elif isinstance(value, ast.Name) and value.id == "target_page":
            pass                # a parameter; its call sites are read below
        elif isinstance(value, ast.Constant) and value.value is None:
            pass                # no link rendered
        else:
            unresolved.append(f"{filename}:{where}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "target_page":
                    _take(value, value.lineno)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "target_page":
                    _take(kw.value, kw.value.lineno)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if (isinstance(node.slice, ast.Constant)
                    and node.slice.value == "target_page"):
                unresolved.append(f"{filename}:{node.lineno} (item assignment)")
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            # A LOCAL named target_page is not a parameter: its value is
            # computed here and the `Name` exemption above would wave it
            # through unread. Only a string literal is accepted.
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                if isinstance(tgt, ast.Name) and tgt.id == "target_page":
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        literals.add(value.value)
                    else:
                        unresolved.append(
                            f"{filename}:{node.lineno} (computed local)")
    return literals, unresolved


def test_05_no_dashboard_card_or_signal_targets_a_page_that_does_not_exist():
    """A card whose target is outside PAGES renders "Open … →" and navigate()
    bounces it to the Dashboard — a link to nowhere, with no error. Checked for
    EVERY dashboard service, against the real PAGES list."""
    pages = _pages()
    services = sorted((ROOT / "services").glob("dashboard_*_service.py"))
    assert len(services) >= 6, [p.name for p in services]
    seen = set()
    for path in services:
        literals, unresolved = _target_pages(path.read_text(encoding="utf-8"),
                                             path.name)
        assert unresolved == [], unresolved
        assert literals <= pages, (path.name, sorted(literals - pages))
        seen |= literals
    assert "roas-campaigns" in seen and "revenue-health" in seen   # it read them
    assert not {"action-queue", "reports"} & seen
    # No other service emits a card target this test does not read.
    for path in (ROOT / "services").glob("*.py"):
        if not path.name.startswith("dashboard_"):
            assert '"target_page"' not in path.read_text(encoding="utf-8"), path.name


def test_06_the_target_check_fails_on_a_deleted_or_unreadable_target():
    """Counterfactual for test_05 — the guard must go red on the pre-change
    shape, and on a target it cannot read."""
    pages = _pages()
    before = ('cards.append({"type": "investigate",\n'
              '    "target_page": "action-queue",\n'
              '    "target_label": "Open Action Queue"})\n')
    literals, unresolved = _target_pages(before)
    assert literals - pages == {"action-queue"}
    assert unresolved == []
    literals, unresolved = _target_pages('rows(target_page="reports")\n')
    assert literals - pages == {"reports"}
    literals, unresolved = _target_pages(
        'card = {"target_page": pick_page(waste)}\n')
    assert unresolved, "a computed target must not pass unread"
    # A computed LOCAL named target_page is not a parameter and must not pass.
    literals, unresolved = _target_pages(
        'target_page = "action-" + "queue"\ncard = {"target_page": target_page}\n')
    assert unresolved, "a computed local target must not pass unread"
    literals, unresolved = _target_pages(
        'target_page = "reports"\ncard = {"target_page": target_page}\n')
    assert literals - pages == {"reports"} and unresolved == []


def test_07_the_investigate_cards_open_the_page_that_holds_their_evidence():
    """Both cards are about CAMPAIGNS with spend and no SQL or won-deal proof —
    the biggest_waste signal, whose own target is Campaign Revenue & Spend."""
    service = (ROOT / "services/dashboard_overview_service.py").read_text(
        encoding="utf-8")
    assert "Action Queue" not in service and "action-queue" not in service
    for headline in ("Paid spend without SQL proof", "No unexplained paid spend"):
        card = service.split(f'"headline": "{headline}"')[1].split("})")[0]
        assert '"target_page": "roas-campaigns"' in card, headline
        assert '"target_label": "Open Campaign Revenue & Spend"' in card, headline
    signal = service.split('"label": "Spend with no SQL or won-deal proof"')[1]
    assert '"target_page": "roas-campaigns"' in signal.split("}")[0]


def test_08_an_old_bookmark_lands_on_the_dashboard():
    """navigate() sends any key outside PAGES to the Dashboard. Evaluate the
    real guard with the real PAGES list rather than trusting the layout."""
    pages = re.search(r"const PAGES = \[.*?\];", JS, re.S).group(0)
    guard = "if (!PAGES.includes(page) || adminOnlyDenied(page)) {"
    assert guard in JS
    after = JS[JS.index(guard):][:140]
    assert 'navigate("dashboard", { updateHash: true, replace: true });' in after
    script = pages + (
        "\nconst out = {};"
        "\nfor (const p of ['action-queue', 'reports', 'search-terms', 'roas-campaigns'])"
        " out[p] = PAGES.includes(p);"
        "\nprocess.stdout.write(JSON.stringify(out));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == ('{"action-queue":false,"reports":false,'
                          '"search-terms":true,"roas-campaigns":true}')
    # Neither key was kept as a redirect: there is no equivalent page to send it to.
    redirects = JS.split("const RETIRED_PAGE_REDIRECTS")[1].split("};")[0]
    assert "action-queue" not in redirects and "reports" not in redirects


def test_09_every_remaining_sidebar_page_still_routes():
    nav = re.findall(r'class="nav-item[^"]*" data-page="([a-z0-9-]+)"', HTML)
    pages = _pages()
    assert len(nav) == len(set(nav)) == 18
    assert set(nav) <= pages, sorted(set(nav) - pages)
    for page in sorted(nav):
        assert f'id="page-{page}"' in HTML, page
        assert f'case "{page}":' in JS, page
    # every page section still present has a route (no orphaned markup)
    sections = set(re.findall(r'<section id="page-([a-z0-9-]+)"', HTML))
    assert sections <= pages, sorted(sections - pages)
    # the four Google Ads evidence pages are exactly what the group now holds
    group = HTML.split("<!-- Google Ads (PR-ADS-162F")[1].split("<!-- Sales Results")[0]
    assert re.findall(r'data-page="([a-z0-9-]+)"', group) == [
        "campaigns", "search-terms", "keywords", "geo"]


def test_10_the_doctrine_registry_holds_no_entry_for_the_deleted_surfaces():
    from analysis import sql_doctrine_registry as registry
    from scripts import audit_sql_doctrine_inventory as inv
    text = (ROOT / "analysis/sql_doctrine_registry.py").read_text(encoding="utf-8")
    for gone in ("/api/action-queue", "Action Queue", "_AQC", "_AQG",
                 "_build_campaign_queue_items", "_build_geo_queue_items",
                 "/reports/latest"):
        assert gone not in text, gone
    rule_ids = {r["id"] for r in registry.RULES}
    assert not {"api.aq.campaign", "api.aq.geo", "ui.report"} & rule_ids
    # No rule is bound to a symbol that no longer exists.
    bound = {s for r in registry.RULES for s in r.get("symbol") or []}
    assert not {"copyLatestReport", "loadReports", "loadActionQueue",
                "_build_campaign_queue_items", "_build_geo_queue_items"} & bound
    assert not any("action-queue" in (d.get("kind") or "")
                   for d in registry.DECISION_SURFACES)
    static = inv.build_static(ROOT)
    # The valence rule exists for ONE top-level literal. Bound without a
    # pattern it would pre-classify anything later written inside the function
    # the scanner anchors it to (the freshness strip on every evidence page).
    valence = [r for r in registry.RULES if r["id"] == "ui.overview.delta_valence"]
    assert len(valence) == 1 and valence[0].get("pattern") == ["sqls_field_ref"]
    bound_to_it = [o for o in static["occurrences"]
                   if o.get("rule_id") == "ui.overview.delta_valence"]
    assert [o["snippet"] for o in bound_to_it] == ['sqls: "up-good",']
    assert static["unclassified_occurrences"] == []
    assert not static["registry_problems"]
    # `ui.report` bound the Dashboard's delta-valence literal (`sqls: "up-good"`)
    # to the report pipeline, only because the scanner attributes a top-level
    # literal to the function before it. It is now bound to the Dashboard.
    valence = [o for o in static["occurrences"]
               if o["path"] == "static/app.js" and 'sqls: "up-good"' in o["snippet"]]
    assert len(valence) == 1
    assert valence[0]["rule_id"] == "ui.overview.delta_valence"
    assert valence[0]["consumer"].startswith("Dashboard Overview")


def test_11_the_report_generation_pipeline_is_untouched():
    """Out of scope: deleting the viewer does not delete the generator. The
    weekly/monthly jobs still run and still email the report."""
    from analysis import sql_doctrine_registry as registry
    for rel in ("scheduler/weekly.py", "scheduler/monthly.py", "analysis/core.py",
                "analysis/rule_advisor.py", "analysis/advisor.py"):
        assert (ROOT / rel).exists(), rel
    consumers = {c["consumer"] for c in registry.CONSUMERS}
    assert registry._REPORT in consumers                      # noqa: SLF001
    report_rules = {r["id"] for r in registry.RULES
                    if r["consumer"] == registry._REPORT}     # noqa: SLF001
    assert {"report.core", "report.rule_advisor"} <= report_rules
    import api.server as server
    paths = {r.path for r in server.app.routes}
    assert {"/run/weekly", "/run/monthly"} <= paths


def test_12_the_waste_truth_audit_no_longer_rebuilds_a_queue():
    """Its queue half imported the deleted builder; left in place it would
    have crashed, and `main()` reports a crash as a failed audit forever."""
    import inspect
    from scripts import audit_search_term_waste_truth as audit
    source = inspect.getsource(audit)
    assert "_build_waste_queue_items" not in source
    assert "api.server" not in source
    assert not hasattr(audit, "_queue_items")
    assert '"queue"' not in source and "data[\"queue\"]" not in source
    collect = inspect.getsource(audit.collect)
    for key in ('"canonical"', '"flagged"', '"snapshot_proof"', '"flag_history"'):
        assert key in collect, key


def _healthy_audit_data() -> dict:
    rows = [{"term_identity": "id-1", "search_term": "freight software"},
            {"term_identity": "id-2", "search_term": "free tms"}]
    return {
        "canonical": {"available": True},
        "snapshot_proof": {"available": True},
        "flag_history": {"available": True, "rows_with_latest_flagged_at": 2},
        "flagged": {"complete": True, "truncated": False, "rows": rows,
                    "payload": {"truth_state": {"status": "ok"},
                                "review_state_available": True,
                                "kpis": {"flagged_terms": 2},
                                "filters": {"applied": True},
                                "pagination": {"total_count": 2}}},
    }


def _audit_failures(mutate) -> list:
    import copy
    from scripts import audit_search_term_waste_truth as audit
    data = copy.deepcopy(_healthy_audit_data())
    mutate(data)
    findings = audit._Findings()                              # noqa: SLF001
    audit._validate(data, findings)                           # noqa: SLF001
    return findings.failures


def _set(path, value):
    def apply(data):
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return apply


def _duplicate_identity(data):
    data["flagged"]["rows"].append(
        {"term_identity": "id-1", "search_term": "Freight Software"})


KEPT_CHECKS = [
    ("canonical facts unreadable", _set(("canonical", "available"), False),
     "canonical search-term facts unavailable"),
    ("annotations unreadable", _set(("snapshot_proof", "available"), False),
     "waste_terms annotations unavailable"),
    ("truth mismatch", _set(("flagged", "payload", "truth_state"),
                            {"status": "mismatch", "reasons": ["no_reason"]}),
     "flagged truth_state = mismatch"),
    ("truth unavailable", _set(("flagged", "payload", "truth_state"),
                               {"status": "unavailable"}),
     "flagged truth_state = unavailable"),
    ("database unavailable", _set(("flagged", "payload", "db_unavailable"), True),
     "flagged view reports db_unavailable"),
    ("truncated read", lambda d: d["flagged"].update(complete=False, truncated=True),
     "exceeded the audit page cap"),
    ("incomplete read", _set(("flagged", "complete"), False),
     "could not be read completely"),
    ("review store unavailable",
     _set(("flagged", "payload", "review_state_available"), False),
     "local review store unavailable"),
    ("row without identity",
     lambda d: d["flagged"]["rows"][0].update(term_identity=None),
     "carry no term_identity"),
    ("duplicate identity", _duplicate_identity,
     "duplicate term_identity value(s)"),
    ("KPI count disagrees", _set(("flagged", "payload", "kpis", "flagged_terms"), 5),
     "KPI flagged_terms (5) != unique durable identities (2)"),
    ("pagination total disagrees",
     _set(("flagged", "payload", "pagination", "total_count"), 9),
     "pagination total_count (9) != rows collected (2)"),
    ("flag history unreadable", _set(("flag_history", "available"), False),
     "flag history could not be read"),
    ("flag history never written",
     _set(("flag_history", "rows_with_latest_flagged_at"), 0),
     "NO durable flag history is"),
]


def test_12a_a_healthy_flagged_population_passes_the_audit():
    assert _audit_failures(lambda data: None) == []


@pytest.mark.parametrize("name, mutate, expected", KEPT_CHECKS,
                         ids=[c[0] for c in KEPT_CHECKS])
def test_12b_every_kept_check_still_fires_for_its_own_reason(name, mutate, expected):
    """Run the real `_validate`, break exactly one thing, and require the
    failure that names it. Asserting that a message string still exists in the
    source would pass with the check itself disabled."""
    failures = _audit_failures(mutate)
    assert any(expected in f for f in failures), (name, failures)

def test_13_the_flagged_view_keeps_its_fallback_spend_threshold():
    """`_QUEUE_HIGH_SPEND_USD_DEFAULT` was defined in the Action Queue block and
    read by /api/waste and the flagged endpoint. It survives under a name that
    says what it is for; deleting it with the block would have been a NameError
    on the first request with an unreadable thresholds file."""
    import api.server as server
    assert server._FLAGGED_HIGH_SPEND_USD_DEFAULT == 100      # noqa: SLF001
    assert SERVER.count("or _FLAGGED_HIGH_SPEND_USD_DEFAULT)") == 2
    for route in ('@app.get("/api/waste")',
                  '@app.get("/api/search-term-evidence/flagged")'):
        body = SERVER.split(route)[1].split("@app.")[0]
        assert "_FLAGGED_HIGH_SPEND_USD_DEFAULT" in body, route


def test_14_the_reader_audits_structural_checks_hold_on_this_tree():
    """Includes check 18: no UI string names a period ratio ROAS or CAC — the
    reworded help and card labels must not have introduced one."""
    from scripts import audit_customer_revenue_reader_cutover as audit
    a = audit.Audit()
    audit.structural_checks(a)
    assert a.violations == [], a.violations
    assert a.checks and all(a.checks.values())
