"""PR-ADS-176C — the legacy country feed stays retired.

``GET /api/geo`` (``services/country_evidence_service``) and
``GET /api/leads/country-summary`` fed the ``#/geo`` page that PR-ADS-176B
deleted; nothing in ``static/`` read them. They are deleted, and their truth
properties are asserted on the live country surfaces in
``tests/test_pr_ads_176c_country_feed_retired_pg_integration.py``.

This module is the guard against bringing them back. Each case fails on
5a9b5e9 (docs/58 §8.6):

* the two routes are gone from the running app and answer 404 — the
  convention of every route this repo has retired (PR-ADS-162B/162D/165A/168B;
  there is no 410 registry);
* ``services/country_evidence_service`` does not exist and nothing imports it,
  by any spelling a module can import it;
* the owner-label zero disclosure and the zero verdict have ONE
  implementation, ``services/country_spend_zero_rule``;
* no registry or audit still names a deleted reader.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETIRED_ROUTES = ("/api/geo", "/api/leads/country-summary")
RETIRED_HANDLERS = ("api_geo", "api_leads_country_summary")
RETIRED_MODULE = "services.country_evidence_service"
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


def test_01_the_retired_routes_are_gone_from_the_running_app_and_answer_404(monkeypatch):
    from fastapi.testclient import TestClient

    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client = TestClient(server.app, raise_server_exceptions=False)
    auth = {"Authorization": "Bearer secret-token"}
    paths = _route_paths()
    for path in RETIRED_ROUTES:
        assert path not in paths, path
        for query in ("", "?window=30d", "?days=30"):
            assert client.get(path + query, headers=auth).status_code == 404, path + query
    # What replaces them is live.
    for kept in ("/api/revenue-performance", "/api/revenue-performance/country-detail",
                 "/api/dashboard/countries", "/api/leads"):
        assert kept in paths, kept


def test_02_no_handler_or_route_decorator_for_them_is_defined_anywhere():
    for p in _py_files(("api", "services")):
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not names & set(RETIRED_HANDLERS), (p, names & set(RETIRED_HANDLERS))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(
                    node.args[0], ast.Constant) and node.args[0].value in RETIRED_ROUTES:
                raise AssertionError(f"{p}: a route registers {node.args[0].value}")


def test_03_the_country_evidence_service_does_not_exist_and_nothing_imports_it():
    assert not (ROOT / "services" / "country_evidence_service.py").exists()
    assert importlib.util.find_spec(RETIRED_MODULE) is None
    leaf = RETIRED_MODULE.rsplit(".", 1)[1]
    for p in _py_files():
        if p.name == Path(__file__).name:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name != RETIRED_MODULE, p
            elif isinstance(node, ast.ImportFrom):
                assert node.module != RETIRED_MODULE, p
                if node.module == "services":
                    assert leaf not in {a.name for a in node.names}, p
            elif (isinstance(node, ast.Call) and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and node.args[0].value == RETIRED_MODULE):
                raise AssertionError(f"{p}: imports {RETIRED_MODULE} dynamically")


def test_04_the_ui_reads_neither_route():
    for name in ("app.js", "index.html"):
        text = (ROOT / "static" / name).read_text(encoding="utf-8")
        for route in RETIRED_ROUTES:
            assert route not in text, (name, route)


def test_05_the_zero_verdict_and_the_owner_label_disclosure_have_one_implementation():
    """The retired service kept its own inline copy of the PR-ADS-170A-F1
    owner-label disclosure (``carried_by_owner`` / ``carried_by_google``).
    The shared module is now the only place either rule is written."""
    rule = "services/country_spend_zero_rule.py"
    for p in _py_files(PRODUCTION_PACKAGES):
        rel = str(p.relative_to(ROOT))
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        if rel == rule:
            assert {"zero_spend_verdict", "zero_rests_on_owner_labels", "assess"} <= defined
            continue
        assert not defined & {"zero_spend_verdict", "zero_rests_on_owner_labels"}, rel
        # Nobody else reads the carrier evidence or the owner/Google carriers.
        assert "fetch_geo_code_carriers(" not in src or rel == "db/revenue_repository.py", rel
        for key in ('"carried_by_owner"', '"carried_by_google"'):
            assert key not in src, (rel, key)


def test_06_no_registry_or_audit_still_names_a_deleted_reader():
    from analysis import sql_doctrine_registry as reg
    from scripts import audit_customer_revenue_reader_cutover as cutover
    from services import cross_page_parity_service as parity
    from services import system_status_service as status

    assert "geo" not in status.PAGE_PIPELINE_IMPACT
    blob = repr((reg.CONSUMERS, reg.RULES, reg.CPQL_CONSUMERS, reg.DECISION_SURFACES,
                 reg.KNOWN_CONTRACT_CONFLICTS))
    for needle in RETIRED_ROUTES + RETIRED_HANDLERS + ("country_evidence_service",):
        assert needle not in blob, needle
    # Every rule binds a file that exists.
    for rule in reg.RULES:
        assert (ROOT / rule["path"]).exists(), rule
    blob = repr((parity.PENDING_REDESIGN_CONSUMERS, parity.METRIC_IDENTITIES,
                 parity.EVIDENCE_WINDOW_CONSUMERS))
    for needle in RETIRED_ROUTES + ("country_evidence_service",):
        assert needle not in blob, needle
    # The parity audit's uncertified lead consumer still reports what is live.
    assert parity.PENDING_REDESIGN_CONSUMERS["lead_intelligence"]["services"] == ["/api/leads"]
    blob = repr((cutover.INVENTORY, cutover.READER_ENTRY_POINTS))
    for needle in RETIRED_ROUTES + RETIRED_HANDLERS + ("country_evidence_service",):
        assert needle not in blob, needle
    for entry in cutover.INVENTORY:
        assert (ROOT / entry["consumer"]).exists(), entry["consumer"]
