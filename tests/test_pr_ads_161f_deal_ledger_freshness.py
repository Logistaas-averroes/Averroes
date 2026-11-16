"""
tests/test_pr_ads_161f_deal_ledger_freshness.py

PR-ADS-161F — a stopped deal sync must not leave the last figures looking
current. Closed-won deals, customers and revenue publish only while the last
SUCCESSFUL incremental deal sync is at most ``deal_ledger.max_staleness_hours``
(36h, owner-set in config/thresholds.yaml) old. Past that, every one of them is
``unavailable`` with ``canonical_ledger_stale`` — never the old number.

Before this PR the ledger's age was "reported, not judged": a June sync read in
October still published (docs/46; test_161d_51 pinned it).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import analysis.closed_won_truth as cwt
from services import canonical_customer_revenue_service as svc
from services import canonical_revenue_service as crs
from tests.canonical_ledger_fixtures import (
    READY_SYNC_STATE, company_evidence, fresh_sync_state, ledger_row,
    patch_canonical_ledger,
)

_ROOT = Path(__file__).resolve().parents[1]
BUSINESS = "business"
FAMILIES = ("closed_won_deals", "revenue_usd", "customers")


def _t137():
    from tests import test_pr_ads_137_dashboard_campaigns as t137
    return t137


def _window(monkeypatch, *, hours_ago, sync_state=None):
    t137 = _t137()
    from tests.canonical_ledger_fixtures import from_legacy_deal_rows
    t137._patch_durable(monkeypatch)
    rows = from_legacy_deal_rows(t137.DEAL_ROWS)
    state = (fresh_sync_state(t137.NOW, hours_ago=hours_ago)
             if sync_state is None else sync_state)
    patch_canonical_ledger(monkeypatch, rows, sync_state=state,
                           company_associations=company_evidence(rows))
    truth = svc.get_closed_won_truth([(BUSINESS, "ytd")], now=t137.NOW,
                                     resolver=None, resolver_detail="t")
    return truth["windows"][0], t137


# ── the threshold is the owner's, read from config ───────────────────────────

def test_01_the_threshold_is_36_hours_from_config():
    assert svc.deal_ledger_max_staleness_hours() == 36.0
    assert svc._DEFAULT_DEAL_LEDGER_MAX_STALENESS_HOURS == 36   # same number


def test_02_a_missing_config_does_not_loosen_the_gate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)            # no config/thresholds.yaml here
    assert svc.deal_ledger_max_staleness_hours() == 36.0


# ── the verdict ──────────────────────────────────────────────────────────────

def test_03_a_current_ledger_publishes(monkeypatch):
    w, _ = _window(monkeypatch, hours_ago=35)
    fresh = w["coverage"]["source_freshness"]
    assert (fresh["stale"], fresh["max_staleness_hours"]) == (False, 36.0)
    for m in FAMILIES[:2]:
        assert w["publication"][m]["status"] == cwt.PUBLISHED, m


def test_04_a_ledger_older_than_36_hours_withholds_every_figure(monkeypatch):
    w, _ = _window(monkeypatch, hours_ago=37)
    fresh = w["coverage"]["source_freshness"]
    assert fresh["stale"] is True and fresh["age_hours"] == 37.0
    assert "36h limit" in fresh["stale_reason"]
    for m in FAMILIES:
        assert w["publication"][m] == {"status": cwt.UNAVAILABLE,
                                       "reason": cwt.R_LEDGER_STALE}, m
        assert w["outcomes"][m] is None, m          # unknown is not zero


def test_05_exactly_36_hours_is_still_current(monkeypatch):
    w, _ = _window(monkeypatch, hours_ago=36)
    assert w["coverage"]["source_freshness"]["stale"] is False
    assert w["publication"]["closed_won_deals"]["status"] == cwt.PUBLISHED


def test_06_a_proven_ledger_with_no_sync_timestamp_is_not_current():
    """Age unknown is not "current": fail closed."""
    fresh = {"coverage_proven": True, "age_hours": None}
    verdict = svc.ledger_staleness(fresh, max_hours=36)
    assert verdict["stale"] is True
    assert "no successful incremental" in verdict["stale_reason"]


def test_07_an_unproven_ledger_keeps_its_own_reason(monkeypatch):
    """Staleness is judged only on a proven ledger; an unproven one already
    withholds as canonical_coverage_not_proven, however old."""
    broken = dict(READY_SYNC_STATE, bootstrap_status="in_progress")
    w, _ = _window(monkeypatch, hours_ago=0, sync_state=broken)
    assert w["coverage"]["source_freshness"]["stale"] is None
    assert w["publication"]["closed_won_deals"]["reason"] == \
        cwt.R_COVERAGE_NOT_PROVEN


# ── every reader inherits it ─────────────────────────────────────────────────

def test_08_the_reader_contract_releases_no_population_when_stale(monkeypatch):
    t137 = _t137()
    _window(monkeypatch, hours_ago=37)
    base = crs.load_won_deals("ytd", now=t137.NOW)
    assert base["available"] is False
    assert base["reason"] == cwt.R_LEDGER_STALE
    assert not base.get("deals")


def test_09_a_production_page_publishes_no_closed_won_figure_when_stale(
        monkeypatch):
    t137 = _t137()
    _window(monkeypatch, hours_ago=37)
    from services.revenue_decision_mart import build_revenue_decision_mart
    from scripts import audit_customer_revenue_reader_cutover as audit
    mart = build_revenue_decision_mart(view="campaign", window="ytd", now=t137.NOW)
    s = mart["summary"]
    for key in ("closed_won_deals", "customers", "won_revenue_usd"):
        assert s.get(key) is None, key
    for m in FAMILIES:
        assert s["metrics"][m]["status"] == cwt.UNAVAILABLE
        assert s["metrics"][m]["reason"] == cwt.R_LEDGER_STALE
    assert audit.ratio_name_violations(mart) == []


def test_10_the_workbench_lists_no_blockers_from_a_stale_ledger():
    """A blocker list drawn from a stopped sync may name deals already fixed
    in HubSpot, so the workbench shows the stale dataset blocker instead."""
    from services import crm_truth_exceptions_service as cte
    from tests.test_pr_ads_161d2_deal_company_associations import (
        NOW, _ok_obs, _universe)
    rows = [ledger_row("1", deal_close_date="2026-10-02T09:00:00+00:00")]
    u = _universe(rows, [_ok_obs("1", 0, 0)], [])
    current = cte.build_crm_truth_exceptions(now=NOW, universe=u)
    assert current["rows"] is not None
    u["sync_state"] = fresh_sync_state(NOW, hours_ago=40)
    stale = cte.build_crm_truth_exceptions(now=NOW, universe=u)
    assert stale["rows"] is None
    assert [b["code"] for b in stale["dataset_blockers"]] == [cwt.R_LEDGER_STALE]


def test_11_parity_routes_a_stale_ledger_to_the_sync_owner():
    from services import cross_page_parity_service as cpp
    readings = [{"consumer": c, "unavailable_reason": cwt.R_LEDGER_STALE}
                for c in ("revenue_decision_mart", "dashboard/overview")]
    status, code, detail = cpp._classify_unavailable(readings, "closed_won_deals")
    assert (status, code) == ("population_unavailable", cpp.V_POPULATION_UNAVAILABLE)
    assert "restore the incremental sync" in detail


def test_12_the_ui_says_the_sync_is_overdue_with_no_workbench_link():
    """An overdue sync is not a CRM record to fix: "Unavailable" with the
    reason in words, and no View blockers link, even for an admin."""
    from tests.test_pr_ads_161e_reader_cutover import _node
    m = {"value": None, "status": "unavailable", "reason": cwt.R_LEDGER_STALE}
    out = _node({"cell": ["canonicalMetricHtml", [m, "__fmt"]]}, role="admin")
    assert "Unavailable" in out["cell"]
    assert "deal sync is overdue" in out["cell"]
    assert "View blockers" not in out["cell"]
    js = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert js.count("canonical_ledger_stale:") == 2     # both label tables
