"""PR-ADS-161G — one-command platform certification.

The certification adds no gate logic: it runs the six existing audits and
combines their verdicts. These tests pin the combination rules, and above all
that it fails closed — a crashed audit (Python reports an escaped exception
as exit 1) is never read as a violation, and an exit 0 without a readable
report is never read as a pass.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import audit_platform_certification as apc

ROOT = Path(__file__).resolve().parent.parent

SIX = {
    "scripts.audit_customer_revenue_reader_cutover",
    "scripts.audit_customer_closed_won_truth",
    "scripts.audit_deal_company_association_coverage",
    "scripts.audit_crm_truth_exceptions",
    "scripts.audit_marketing_outcome_cohorts",
    "scripts.audit_campaign_evidence_certification",
}


def _ok(extra=None):
    return 0, json.dumps({"verdict": "holds", **(extra or {})}), "", None


def _runner(overrides=None):
    overrides = overrides or {}

    def run(module, timeout):
        return overrides.get(module, _ok())
    return run


def _module(key):
    return next(m for k, m, _ in apc.AUDITS if k == key)


def test_01_the_certification_runs_exactly_the_six_production_audits():
    modules = [m for _, m, _ in apc.AUDITS]
    assert set(modules) == SIX and len(modules) == 6
    for m in modules:
        src = (ROOT / (m.replace(".", "/") + ".py")).read_text()
        assert '"--json"' in src, f"{m} must accept --json"


def test_02_all_six_holding_certifies_with_exit_0():
    report = apc.run(runner=_runner())
    assert report["verdict"] == "CERTIFIED"
    assert report["exit_code"] == apc.EXIT_CERTIFIED
    assert [r["outcome"] for r in report["audits"]] == ["holds"] * 6


def test_03_one_violation_fails_the_platform_with_exit_1():
    m = _module("closed_won_truth")
    report = apc.run(runner=_runner({m: (1, json.dumps(
        {"exit_code": 1, "violations": ["YTD revenue drifted"]}), "", None)}))
    assert report["verdict"] == "VIOLATION"
    assert report["exit_code"] == apc.EXIT_VIOLATION
    row = next(r for r in report["audits"] if r["module"] == m)
    assert row["outcome"] == "violation"
    assert row["violations"] == ["YTD revenue drifted"]


def test_04_a_violation_outranks_an_unavailable_audit():
    report = apc.run(runner=_runner({
        _module("reader_cutover"): (2, json.dumps({"detail": "db down"}),
                                    "", None),
        _module("campaign_evidence"): (1, json.dumps({"violations": ["x"]}),
                                       "", None)}))
    assert report["exit_code"] == apc.EXIT_VIOLATION


def test_05_one_unavailable_audit_is_not_certified_and_says_why():
    m = _module("marketing_outcome_cohorts")
    report = apc.run(runner=_runner({m: (2, json.dumps(
        {"exit_code": 2, "unavailable": ["[7d] database unavailable"]}),
        "", None)}))
    assert report["verdict"] == "NOT CERTIFIED"
    assert report["exit_code"] == apc.EXIT_UNAVAILABLE
    row = next(r for r in report["audits"] if r["module"] == m)
    assert row["unavailable"] == ["[7d] database unavailable"]


def test_06_an_escaped_exception_is_a_crash_not_a_violation_and_never_passes():
    traceback = "Traceback (most recent call last):\n  ...\nKeyError: 'x'"
    m = _module("campaign_evidence")
    report = apc.run(runner=_runner({m: (1, "", traceback, None)}))
    row = next(r for r in report["audits"] if r["module"] == m)
    assert row["outcome"] == "crashed"
    assert row["stderr_tail"][-1] == "KeyError: 'x'"
    assert report["verdict"] == "NOT CERTIFIED"
    assert report["exit_code"] == apc.EXIT_UNAVAILABLE


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]", "null"])
def test_07_exit_0_without_a_readable_report_is_not_a_pass(stdout):
    m = _module("crm_truth_exceptions")
    report = apc.run(runner=_runner({m: (0, stdout, "", None)}))
    assert next(r for r in report["audits"]
                if r["module"] == m)["outcome"] == "crashed"
    assert report["exit_code"] != apc.EXIT_CERTIFIED


@pytest.mark.parametrize("code", [3, -9, 137])
def test_08_an_unrecognised_exit_code_is_a_crash(code):
    m = _module("reader_cutover")
    report = apc.run(runner=_runner({m: (code, "{}", "", None)}))
    assert next(r for r in report["audits"]
                if r["module"] == m)["outcome"] == "crashed"
    assert report["exit_code"] == apc.EXIT_UNAVAILABLE


def test_09_a_timeout_is_a_crash():
    m = _module("deal_company_associations")
    report = apc.run(runner=_runner({m: (None, "", "",
                                         "timed out after 900s")}))
    row = next(r for r in report["audits"] if r["module"] == m)
    assert row["outcome"] == "crashed" and "timed out" in row["detail"]
    assert report["exit_code"] == apc.EXIT_UNAVAILABLE


def test_10_a_report_contradicting_its_process_exit_code_is_a_crash():
    m = _module("marketing_outcome_cohorts")
    report = apc.run(runner=_runner({m: (0, json.dumps({"exit_code": 1}),
                                         "", None)}))
    assert next(r for r in report["audits"]
                if r["module"] == m)["outcome"] == "crashed"
    assert report["exit_code"] != apc.EXIT_CERTIFIED


def test_11_no_audits_certifies_nothing():
    assert apc.run(runner=_runner(), audits=())["exit_code"] == \
        apc.EXIT_UNAVAILABLE


def test_12_crm_data_to_fix_comes_from_the_exceptions_report_unknown_stays_none():
    m = _module("crm_truth_exceptions")
    totals = {"deals_with_any_exception": 26, "by_family": {
        "identity": {"deals": 11, "rows": 11,
                     "by_reason": {"no_company": 5, "multi_company": 6}},
        "amount": {"deals": 14, "rows": 14, "by_reason": {"no_amount": 14}}}}
    report = apc.run(runner=_runner({m: _ok({
        "totals": totals, "dataset_blockers": [],
        "windows": [{"window": "ytd", "customers": {"status": "withheld"},
                     "revenue_usd": {"status": "published"}}]})}))
    fix = report["crm_data_to_fix"]
    assert fix["deals_with_any_exception"] == 26
    assert fix["by_family"]["identity"]["deals"] == 11
    assert fix["windows"] == [{"window": "ytd", "customers": "withheld",
                               "revenue_usd": "published"}]

    # Workbench unavailable (e.g. the deal sync is overdue): the blocker is
    # named and the deal count is unknown — None, never 0.
    stale = apc.run(runner=_runner({m: (2, json.dumps({
        "exit_code": 2, "totals": None,
        "dataset_blockers": [{"code": "canonical_ledger_stale",
                              "detail": "41h"}]}), "", None)}))
    fix = stale["crm_data_to_fix"]
    assert fix["deals_with_any_exception"] is None
    assert fix["dataset_blockers"][0]["code"] == "canonical_ledger_stale"

    crashed = apc.run(runner=_runner({m: (1, "", "boom", None)}))
    assert crashed["crm_data_to_fix"] is None


def test_13_main_json_exit_code_matches_the_report(monkeypatch, capsys):
    monkeypatch.setattr(apc, "_run_subprocess", _runner())
    assert apc.main(["--json"]) == apc.EXIT_CERTIFIED
    assert json.loads(capsys.readouterr().out)["verdict"] == "CERTIFIED"


def test_14_text_output_names_each_audit_and_the_verdict(monkeypatch, capsys):
    m = _module("closed_won_truth")
    monkeypatch.setattr(apc, "_run_subprocess", _runner(
        {m: (1, json.dumps({"violations": ["drift"]}), "", None)}))
    assert apc.main([]) == apc.EXIT_VIOLATION
    out = capsys.readouterr().out
    assert "PLATFORM CERTIFICATION: VIOLATION" in out
    assert "violation: drift" in out
    assert f"details: python -m {m}" in out
    for key, _, _ in apc.AUDITS:
        assert key in out


def test_15_real_audits_without_a_database_are_unavailable_not_crashed(
        monkeypatch):
    """End to end through the real subprocesses: with no database every audit
    must print a readable report and exit 2. Proves the parser matches what
    the six audits actually print on their unavailable path."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # PR-ADS-169A: the memory environment is stated, like the database one.
    # Without a cgroup limit the derived budget counts THIS process as the
    # instance's usage, and a long pytest session is not the web server. The
    # instance stated here is production's: 512 MB with the server idle at
    # 297 MB, so the six real audits must also fit that budget (151 MB).
    readers = {"limit": lambda settings: (512.0, "cgroup_v2"),
               "usage": lambda source: (297.0, source)}
    report = apc.run(timeout=300, readers=readers)
    assert [r["outcome"] for r in report["audits"]] == ["unavailable"] * 6, \
        [(r["key"], r.get("detail")) for r in report["audits"]]
    assert report["exit_code"] == apc.EXIT_UNAVAILABLE
    assert {r["memory_budget_mb"] for r in report["audits"]} == {151}
