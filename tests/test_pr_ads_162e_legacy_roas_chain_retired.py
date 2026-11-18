"""PR-ADS-162E — the legacy local-JSON ROAS chain is deleted.

Since PR-ADS-161E this chain existed only to withhold its own output: it
computed revenue, ROAS, LTV/CAC and SCALE/HOLD/FIX/CUT verdicts from local
JSON files, a daily job persisted them, and every route that served them
replaced each figure with "withheld". One route — the attribution confidence
summary — still published tier counts computed from it.

Deleted: ``analysis/roas_calculator.py``, ``analysis/unit_economics.py``,
``services/unit_economics_service.py``, ``services/roas_snapshot_service.py``,
``connectors/hubspot_churn.py``, ``config/churn_input.yaml``, two smoke
scripts, five routes, the daily snapshot step, the Revenue Snapshot History
block and the Attribution Confidence card.

A deletion is only clean if nothing can still reach what was deleted, and if
the guards that watched it still watch for its return.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "api/server.py").read_text(encoding="utf-8")
JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")

DELETED_FILES = (
    "analysis/roas_calculator.py", "analysis/unit_economics.py",
    "services/unit_economics_service.py", "services/roas_snapshot_service.py",
    "connectors/hubspot_churn.py", "config/churn_input.yaml",
    "scripts/smoke_test_roas.py", "scripts/smoke_test_roas_snapshot.py",
)
DELETED_MODULES = (
    "analysis.roas_calculator", "analysis.unit_economics",
    "services.unit_economics_service", "services.roas_snapshot_service",
    "connectors.hubspot_churn",
)
DELETED_ROUTES = (
    "/api/reports/roas/campaigns", "/api/reports/roas/countries",
    "/api/reports/roas/snapshots", "/api/reports/roas/snapshots/latest",
    "/api/attribution/confidence-summary",
)
PACKAGES = ("analysis", "api", "connectors", "db", "scheduler", "services",
            "scripts")


def _imports(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def test_01_the_files_are_gone():
    for rel in DELETED_FILES:
        assert not (ROOT / rel).exists(), rel


def test_02_no_production_module_imports_the_deleted_chain():
    """An import of a deleted module is a crash waiting for its code path —
    several of these were imported lazily, inside a function."""
    offenders = []
    for pkg in PACKAGES:
        for path in (ROOT / pkg).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            hit = _imports(path) & set(DELETED_MODULES)
            if hit:
                offenders.append((str(path.relative_to(ROOT)), sorted(hit)))
    assert offenders == []


@pytest.mark.parametrize("route", DELETED_ROUTES)
def test_03_the_routes_are_gone_from_the_running_app(route, monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client = TestClient(server.app)
    assert route not in {r.path for r in server.app.routes}
    assert client.get(route, headers={
        "Authorization": "Bearer secret-token"}).status_code == 404
    assert f'"{route}"' not in SERVER


def test_04_the_canonical_unit_economics_route_is_untouched():
    """Its path sits beside the deleted ones; it must not go with them."""
    import api.server as server
    assert "/api/reports/unit-economics" in {r.path for r in server.app.routes}
    route = SERVER.split('@app.get("/api/reports/unit-economics")')[1].split("@app.")[0]
    assert "build_unit_economics" in route


def test_05_the_daily_job_no_longer_persists_a_snapshot():
    daily = (ROOT / "scheduler/daily.py").read_text(encoding="utf-8")
    assert "roas_snapshot" not in daily.lower()
    assert "generate_roas_snapshot" not in daily
    steps = re.findall(r"Step (\d)/(\d)", daily)
    assert steps == [("1", "5"), ("2", "5"), ("3", "5"), ("4", "5"), ("5", "5")]


def test_06_the_ui_has_no_snapshot_history_or_confidence_card():
    for gone in ("loadRevenueSnapshotHistory", "loadAttributionConfidenceSummary",
                 "/api/reports/roas/", "/api/attribution/confidence-summary",
                 "snapshot-history", "attribution-confidence-summary",
                 "_isNotFoundError"):
        assert gone not in JS, gone
        assert gone not in HTML, gone
    # GCLID still loads its readiness. (This also pinned the Reports page's
    # loader; that page was deleted in PR-ADS-162G, so it is pinned absent.)
    assert 'case "reports"' not in JS
    assert "loadReports" not in JS
    assert "loadGclidReadiness();" in JS


def test_07_the_reader_audit_still_guards_against_the_chains_return():
    """The audit's inventory rows for the deleted surfaces are gone, but the
    function names stay entry points: a module that calls one again is a
    closed-won reader the inventory must name."""
    from scripts import audit_customer_revenue_reader_cutover as audit
    surfaces = " | ".join(r["surface"] for r in audit.INVENTORY)
    for gone in ("/api/reports/roas/", "confidence-summary", "snapshot job"):
        assert gone not in surfaces, gone
    for name in ("compute_all_campaign_roas", "compute_all_country_roas",
                 "load_latest_roas_snapshot", "load_roas_snapshots"):
        assert name in audit.READER_ENTRY_POINTS, name
    for rel in audit.RETIRED_LEGACY:
        assert (ROOT / rel).exists(), f"RETIRED_LEGACY names a missing file: {rel}"
    for row in audit.INVENTORY:
        assert (ROOT / row["consumer"]).exists(), row["consumer"]


def test_08_the_audits_structural_checks_hold_on_this_tree():
    from scripts import audit_customer_revenue_reader_cutover as audit
    a = audit.Audit()
    audit.structural_checks(a)
    assert a.violations == [], a.violations
    assert a.checks and all(a.checks.values())


def test_09_a_reintroduced_legacy_call_fails_the_audit(monkeypatch):
    """Counterfactual for test_08: put a legacy computation back into the
    server text and the audit's check must go red."""
    from scripts import audit_customer_revenue_reader_cutover as audit
    real = audit._text

    def with_legacy(rel):
        text = real(rel)
        if rel == audit.SERVER:
            text += "\ncampaigns = compute_all_campaign_roas(window_days=60)\n"
        return text
    monkeypatch.setattr(audit, "_text", with_legacy)
    a = audit.Audit()
    audit.structural_checks(a)
    assert any("deleted legacy ROAS chain" in v for v in a.violations), a.violations
    assert a.checks["no_external_calls_no_writes"] is False


def test_10_no_unread_roas_thresholds_remain():
    import yaml
    cfg = yaml.safe_load((ROOT / "config/thresholds.yaml").read_text())
    assert "roas" not in cfg                # its only reader was roas_calculator
    assert cfg["deal_ledger"]["max_staleness_hours"] == 36   # untouched
