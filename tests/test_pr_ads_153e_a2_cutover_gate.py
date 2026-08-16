"""
tests/test_pr_ads_153e_a2_cutover_gate.py

PR-ADS-153E-A2 — the canonical revenue CUTOVER GATE.

The defect this suite pins down
-------------------------------
PR-ADS-153E-A reconciled what the ledger HOLDS. It never checked what the ledger
is MISSING. A portal whose historical bootstrap had never run — one nightly
incremental over the last 24 hours, reporting `success` — reconciled perfectly
against the same 24 hours of legacy rows and returned `ok: true`. That is the
exact signal PR-ADS-153E-B was going to read as permission to repoint the
executive revenue and customer totals at a ledger holding one day of history.

So `ok: true` now additionally requires proven COVERAGE: a complete bootstrap
with ordered timestamps, and a successful incremental sync on top of it.

Pure/unit level. `test_pr_ads_153e_a2_pg_integration.py` proves the durable
half against a real cluster.

Run with:
    python -m pytest tests/test_pr_ads_153e_a2_cutover_gate.py -v
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from services import revenue_reconciliation_service as recon  # noqa: E402

_LEDGER_REPO_PY = (_ROOT / "db" / "deal_ledger_repository.py").read_text()
_SYNC_SERVICE_PY = (_ROOT / "services" / "hubspot_deal_sync_service.py").read_text()
_RECON_SERVICE_PY = (
    _ROOT / "services" / "revenue_reconciliation_service.py").read_text()
_AUDIT_PY = (_ROOT / "scripts" / "audit_canonical_revenue_truth.py").read_text()
_BACKFILL_PY = (
    _ROOT / "scripts" / "backfill_canonical_deal_ledger.py").read_text()


def _code_only(source: str) -> str:
    """Executable code with docstrings and comments stripped.

    Governance guards must inspect what the code DOES, not what its prose says.
    """
    without_docstrings = re.sub(r'(?s)"""[^"]*(?:"(?!"")[^"]*)*"""', "", source)
    return re.sub(r"^\s*#.*$", "", without_docstrings, flags=re.M)


# ─────────────────────────────────────────────────────────────────────────────
# Sync-state fixtures
# ─────────────────────────────────────────────────────────────────────────────
_BOOT_START = "2026-08-01T00:00:00+00:00"
_BOOT_END = "2026-08-01T04:00:00+00:00"
_INCREMENTAL = "2026-08-02T03:00:00+00:00"


def _state(**overrides) -> dict:
    """A sync-state read that satisfies the interlock, before overrides."""
    row = {
        "bootstrap_status": "complete",
        "bootstrap_started_at": _BOOT_START,
        "bootstrap_completed_at": _BOOT_END,
        "last_incremental_at": _INCREMENTAL,
        "last_status": "success",
        "last_error": None,
    }
    row.update(overrides)
    return {"available": True, "row": row}


def _codes(sync_res, stages_res=None) -> list:
    """Violation CODES the coverage gate raises, with everything else clean."""
    findings = recon._check_invariants(
        {}, {}, [], sync_res,
        stages_res if stages_res is not None else {"available": True, "rows": []})
    return [f["code"] for f in findings]


# =============================================================================
# The gate: sync state must exist and be readable
# =============================================================================
def test_a_healthy_state_passes():
    """The control. Everything below changes exactly one thing from this."""
    assert _codes(_state()) == []


def test_unavailable_sync_state_repository_fails():
    codes = _codes({"available": False, "reason": "database_unavailable"})
    assert recon.V_SYNC_STATE_UNAVAILABLE in codes


def test_absent_state_row_fails():
    """`available: True, row: None` — the read SUCCEEDED and found nothing.
    153E-A treated this as "no news is good news"."""
    codes = _codes({"available": True, "row": None})
    assert recon.V_SYNC_STATE_MISSING in codes


def test_an_empty_state_row_fails():
    assert recon.V_SYNC_STATE_MISSING in _codes({"available": True, "row": {}})


# =============================================================================
# The gate: the historical bootstrap must be COMPLETE
# =============================================================================
@pytest.mark.parametrize("status", ["not_started", "in_progress", "partial",
                                    None, "", "unknown"])
def test_bootstrap_that_is_not_complete_fails(status):
    codes = _codes(_state(bootstrap_status=status))
    assert recon.V_BOOTSTRAP_NOT_COMPLETE in codes


def test_the_exact_153e_a_hole_a_successful_incremental_over_no_bootstrap():
    """The scenario that motivated this PR, spelled out.

    Nightly incremental succeeded. Nothing else ever ran. 153E-A returned
    ok: true and would have authorised the cutover.
    """
    codes = _codes({"available": True, "row": {
        "bootstrap_status": "not_started",
        "bootstrap_started_at": None,
        "bootstrap_completed_at": None,
        "last_incremental_at": _INCREMENTAL,
        "last_status": "success",
        "last_error": None,
    }})
    assert recon.V_BOOTSTRAP_NOT_COMPLETE in codes


# =============================================================================
# The gate: a completed bootstrap must be corroborated by its timestamps
# =============================================================================
def test_complete_without_a_start_timestamp_fails():
    codes = _codes(_state(bootstrap_started_at=None))
    assert recon.V_BOOTSTRAP_TIMESTAMP_MISSING in codes


def test_complete_without_a_completion_timestamp_fails():
    codes = _codes(_state(bootstrap_completed_at=None))
    assert recon.V_BOOTSTRAP_TIMESTAMP_MISSING in codes


def test_completion_before_start_fails():
    codes = _codes(_state(bootstrap_started_at=_BOOT_END,
                          bootstrap_completed_at=_BOOT_START))
    assert recon.V_BOOTSTRAP_TIMESTAMP_INVALID in codes


def test_equal_start_and_completion_is_accepted():
    """A portal small enough to bootstrap inside one clock tick is not an
    error. The check is ordering, not duration."""
    assert _codes(_state(bootstrap_started_at=_BOOT_END,
                         bootstrap_completed_at=_BOOT_END)) == []


def test_an_unparseable_timestamp_is_unknown_not_assumed():
    codes = _codes(_state(bootstrap_completed_at="not-a-timestamp"))
    assert recon.V_BOOTSTRAP_TIMESTAMP_MISSING in codes


# =============================================================================
# The gate: an incremental must have succeeded AFTER the bootstrap
# =============================================================================
def test_bootstrap_complete_without_any_later_incremental_fails():
    codes = _codes(_state(last_incremental_at=None))
    assert recon.V_POST_BOOTSTRAP_INCREMENTAL_MISSING in codes


def test_an_incremental_before_bootstrap_completion_fails():
    """Stale evidence. It proves the pipeline worked before the history
    existed, which is not the same as working on top of it."""
    codes = _codes(_state(last_incremental_at="2026-07-01T00:00:00+00:00"))
    assert recon.V_POST_BOOTSTRAP_INCREMENTAL_MISSING in codes


def test_an_incremental_at_exactly_bootstrap_completion_fails():
    """`<=`, deliberately. A timestamp equal to completion cannot be shown to
    have happened after it."""
    codes = _codes(_state(last_incremental_at=_BOOT_END))
    assert recon.V_POST_BOOTSTRAP_INCREMENTAL_MISSING in codes


def test_a_successful_incremental_after_bootstrap_completion_passes():
    assert _codes(_state(last_incremental_at="2026-08-01T04:00:01+00:00")) == []


# =============================================================================
# The gate: the last sync must have honestly succeeded
# =============================================================================
@pytest.mark.parametrize("status", ["partial", "failed", None, ""])
def test_last_sync_not_successful_fails(status):
    codes = _codes(_state(last_status=status))
    assert recon.V_LAST_SYNC_NOT_SUCCESSFUL in codes


def test_success_recorded_alongside_an_error_fails():
    """Success and an error message together is a contradiction, and the error
    is the half that is safe to believe."""
    codes = _codes(_state(last_error="association_lookup_cap_reached"))
    assert recon.V_LAST_SYNC_SUCCESS_WITH_ERROR in codes


# =============================================================================
# The gate: stage coverage must be readable
# =============================================================================
def test_unavailable_stage_breakdown_fails():
    codes = _codes(_state(), stages_res={"available": False,
                                         "reason": "database_unavailable"})
    assert recon.V_STAGE_BREAKDOWN_UNAVAILABLE in codes


def test_an_empty_but_readable_stage_breakdown_passes():
    """Readable-and-empty is a fact; unreadable is not."""
    assert _codes(_state(), stages_res={"available": True, "rows": []}) == []


def test_the_report_never_renders_an_unavailable_breakdown_as_a_list():
    fn = _code_only(
        _RECON_SERVICE_PY.split("def build_revenue_reconciliation(")[1]
        .split("\ndef ")[0])
    assert 'if stages_res.get("available") else None' in fn
    assert '"stage_breakdown_available"' in fn
    render = _AUDIT_PY.split("def _render(")[1].split("\ndef ")[0]
    assert "UNAVAILABLE — stage coverage could not be read" in render


# =============================================================================
# Violation codes are stable and machine-readable
# =============================================================================
@pytest.mark.parametrize("code", [
    "sync_state_missing",
    "bootstrap_not_complete",
    "bootstrap_timestamp_missing",
    "bootstrap_timestamp_invalid",
    "post_bootstrap_incremental_missing",
    "last_sync_not_successful",
    "stage_breakdown_unavailable",
])
def test_required_violation_codes_exist(code):
    assert code in _RECON_SERVICE_PY


def test_every_violation_carries_a_code_and_a_message():
    findings = recon._check_invariants(
        {}, {}, [], {"available": True, "row": {}},
        {"available": False, "reason": "boom"})
    assert findings
    for f in findings:
        assert set(f) == {"code", "message"}
        assert f["code"] and f["message"]


def test_the_report_exposes_codes_alongside_messages():
    fn = _RECON_SERVICE_PY.split("def build_revenue_reconciliation(")[1]
    assert '"violation_codes"' in fn
    assert '"violation_details"' in fn
    # `violations` keeps its original shape — a list of human strings.
    assert 'violations = [f["message"] for f in findings]' in _RECON_SERVICE_PY


# =============================================================================
# Repository: sync mode is declared, never inferred
# =============================================================================
def test_record_sync_state_requires_an_explicit_mode():
    import db.deal_ledger_repository as repo

    import inspect

    sig = inspect.signature(repo.record_sync_state)
    assert "sync_mode" in sig.parameters
    # No default: a caller cannot omit it and get a guess.
    assert sig.parameters["sync_mode"].default is inspect.Parameter.empty
    # And the old inferred flag is gone.
    assert "bootstrap_status" not in sig.parameters


def test_an_unknown_sync_mode_is_rejected_without_touching_the_database():
    import db.deal_ledger_repository as repo

    out = repo.record_sync_state(status="success", sync_mode="whatever")
    assert out["available"] is False
    assert out["reason"] == "invalid_sync_mode"


def test_only_a_proven_complete_successful_run_completes_the_bootstrap():
    fn = _code_only(
        _LEDGER_REPO_PY.split("def record_sync_state(")[1].split("\ndef ")[0])
    assert ("completes_bootstrap = bool(is_bootstrap and proved_complete\n"
            "                               and status == \"success\")") in fn


def test_a_bootstrap_run_never_stamps_last_incremental_at():
    fn = _LEDGER_REPO_PY.split("def record_sync_state(")[1].split("\ndef ")[0]
    # The SQL is an f-string, so the table name appears as {SYNC_STATE_TABLE}.
    clause = fn.split("last_incremental_at = CASE")[1].split("END,")[0]
    assert "WHEN %(is_bootstrap)s" in clause
    assert "{SYNC_STATE_TABLE}.last_incremental_at" in clause
    assert "ELSE NOW()" in clause


def test_an_incremental_run_never_touches_bootstrap_columns():
    fn = _LEDGER_REPO_PY.split("def record_sync_state(")[1].split("\ndef ")[0]
    for column in ("bootstrap_status", "bootstrap_started_at",
                   "bootstrap_completed_at"):
        clause = fn.split(f"{column} = CASE")[1].split("END,")[0]
        assert "WHEN NOT %(is_bootstrap)s" in clause or "%(completes)s" in clause


def test_a_completed_bootstrap_is_never_downgraded():
    fn = _LEDGER_REPO_PY.split("def record_sync_state(")[1].split("\ndef ")[0]
    clause = fn.split("bootstrap_status = CASE")[1].split("END,")[0]
    assert "{SYNC_STATE_TABLE}.bootstrap_status = %(complete)s" in clause
    assert "THEN %(complete)s" in clause


def test_the_first_bootstrap_start_timestamp_survives_retries():
    fn = _LEDGER_REPO_PY.split("def record_sync_state(")[1].split("\ndef ")[0]
    clause = fn.split("bootstrap_started_at = CASE")[1].split("END,")[0]
    assert "COALESCE(" in clause
    assert "{SYNC_STATE_TABLE}.bootstrap_started_at, NOW())" in clause


# =============================================================================
# Sync service: every state write names its mode
# =============================================================================
def test_every_record_sync_state_call_passes_a_mode():
    calls = _SYNC_SERVICE_PY.count("record_sync_state(")
    with_mode = _SYNC_SERVICE_PY.count("sync_mode=sync_mode")
    assert calls >= 3, "expected the two pull-failure paths and the final write"
    assert with_mode == calls, (
        "a record_sync_state call omits sync_mode — a failed bootstrap would "
        "then be filed as a failed incremental")


def test_the_mode_is_resolved_once_before_any_exit_path():
    fn = _SYNC_SERVICE_PY.split("def sync_deals(")[1].split("\ndef ")[0]
    assert "sync_mode = (ledger_repo.SYNC_MODE_BOOTSTRAP if bootstrap" in fn
    # Declared before the read, so the early pull-failure returns carry it too.
    assert fn.index("sync_mode = (") < fn.index("pull_deals_for_ledger")


def test_backfill_always_records_bootstrap_mode():
    fn = _SYNC_SERVICE_PY.split("def backfill_deals(")[1].split("\ndef ")[0]
    assert "bootstrap=True" in fn
    assert "full_refresh=restart" in fn


def test_restart_is_opt_in_and_never_automatic():
    fn = _SYNC_SERVICE_PY.split("def backfill_deals(")[1].split("\ndef ")[0]
    assert "restart: bool = False" in fn
    cli = _code_only(_BACKFILL_PY)
    # The CLI never sets restart on its own, and never as error recovery.
    assert "restart=restart and index == 1" in cli
    assert "restart=True" not in cli


def test_completion_is_passed_from_the_connectors_proof():
    fn = _SYNC_SERVICE_PY.split("def sync_deals(")[1].split("\ndef ")[0]
    assert "proved_complete=complete" in fn
    # `complete` is pull-completeness AND not truncated by the lookup cap.
    assert 'complete = bool(pull.get("complete")) and not truncated' in fn


# =============================================================================
# The backfill operator CLI
# =============================================================================
def _fake_backfill(results):
    """A backfill_deals stub that yields the given pass results in order."""
    calls = []

    def _run(*, restart=False, max_association_lookups=None):
        calls.append({"restart": restart,
                      "max_association_lookups": max_association_lookups})
        return results[min(len(calls) - 1, len(results) - 1)]

    _run.calls = calls
    return _run


def _capped(watermark="2026-08-01T00:00:00+00:00"):
    return {"status": "partial", "sync_mode": "bootstrap", "deals_seen": 5000,
            "written": 5000, "skipped_stale": 0, "association_failures": 0,
            "write_failures": 0, "pages": 50, "complete": False,
            "watermark": watermark, "watermark_is_checkpoint": True,
            "error": "association_lookup_cap_reached"}


def _finished():
    return {"status": "success", "sync_mode": "bootstrap", "deals_seen": 120,
            "written": 120, "skipped_stale": 0, "association_failures": 0,
            "write_failures": 0, "pages": 2, "complete": True,
            "watermark": "2026-08-02T00:00:00+00:00",
            "watermark_is_checkpoint": False, "error": None}


def _broken(error="pull_failed: 503"):
    return {"status": "failed", "sync_mode": "bootstrap", "deals_seen": 0,
            "written": 0, "skipped_stale": 0, "association_failures": 0,
            "write_failures": 1, "pages": 0, "complete": False,
            "watermark": None, "watermark_is_checkpoint": False,
            "error": error}


def _install_cli(monkeypatch, results, *, state_row):
    import db.connection as connection
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc

    runner = _fake_backfill(results)
    monkeypatch.setattr(connection, "init_pool", lambda *a, **k: None)
    monkeypatch.setattr(svc, "backfill_deals", runner)
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "row": state_row})
    return runner


_COMPLETE_ROW = {"bootstrap_status": "complete",
                 "bootstrap_started_at": _BOOT_START,
                 "bootstrap_completed_at": _BOOT_END,
                 "last_status": "success", "last_error": None}
_IN_PROGRESS_ROW = {"bootstrap_status": "in_progress",
                    "bootstrap_started_at": _BOOT_START,
                    "bootstrap_completed_at": None,
                    "last_status": "partial",
                    "last_error": "association_lookup_cap_reached"}


def test_cli_resumes_through_capped_passes_and_completes(monkeypatch):
    from scripts import backfill_canonical_deal_ledger as cli

    runner = _install_cli(monkeypatch, [_capped(), _capped(), _finished()],
                          state_row=_COMPLETE_ROW)
    out = cli.run(max_passes=10, max_association_lookups=5000, restart=False)

    assert out["ok"] is True
    assert out["reason"] == "bootstrap_complete"
    assert out["passes_run"] == 3
    assert out["bootstrap_status"] == "complete"
    # Only the first pass could ever carry restart, and it did not here.
    assert [c["restart"] for c in runner.calls] == [False, False, False]


def test_cli_stops_immediately_on_failure(monkeypatch):
    from scripts import backfill_canonical_deal_ledger as cli

    runner = _install_cli(monkeypatch, [_capped(), _broken(), _finished()],
                          state_row=_IN_PROGRESS_ROW)
    out = cli.run(max_passes=10, max_association_lookups=5000, restart=False)

    assert out["ok"] is False
    assert "pull_failed" in out["reason"]
    # It did NOT keep going to the pass that would have succeeded.
    assert len(runner.calls) == 2


def test_cli_stops_on_a_partial_with_no_checkpoint(monkeypatch):
    """Without a checkpoint the next pass would start from the same place, so
    looping would spin without progressing."""
    from scripts import backfill_canonical_deal_ledger as cli

    stuck = _capped()
    stuck["watermark_is_checkpoint"] = False
    runner = _install_cli(monkeypatch, [stuck], state_row=_IN_PROGRESS_ROW)
    out = cli.run(max_passes=10, max_association_lookups=5000, restart=False)

    assert out["ok"] is False
    assert len(runner.calls) == 1


def test_cli_stops_on_a_partial_with_association_failures(monkeypatch):
    from scripts import backfill_canonical_deal_ledger as cli

    lossy = _capped()
    lossy["association_failures"] = 3
    runner = _install_cli(monkeypatch, [lossy], state_row=_IN_PROGRESS_ROW)
    out = cli.run(max_passes=10, max_association_lookups=5000, restart=False)
    assert out["ok"] is False
    assert len(runner.calls) == 1


def test_cli_is_bounded_by_max_passes(monkeypatch):
    from scripts import backfill_canonical_deal_ledger as cli

    runner = _install_cli(monkeypatch, [_capped()], state_row=_IN_PROGRESS_ROW)
    out = cli.run(max_passes=4, max_association_lookups=5000, restart=False)

    assert out["ok"] is False
    assert "exhausted --max-passes=4" in out["reason"]
    assert len(runner.calls) == 4, "the loop must be bounded, never indefinite"


def test_cli_requires_the_durable_state_to_agree(monkeypatch):
    """A pass claiming completion is this process's opinion. The sync state is
    what the audit gate will read tomorrow, and it wins."""
    from scripts import backfill_canonical_deal_ledger as cli

    _install_cli(monkeypatch, [_finished()], state_row=_IN_PROGRESS_ROW)
    out = cli.run(max_passes=10, max_association_lookups=5000, restart=False)

    assert out["ok"] is False
    assert "durable sync state is in_progress" in out["reason"]


def test_cli_fails_when_the_state_cannot_be_read(monkeypatch):
    import db.connection as connection
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc
    from scripts import backfill_canonical_deal_ledger as cli

    monkeypatch.setattr(connection, "init_pool", lambda *a, **k: None)
    monkeypatch.setattr(svc, "backfill_deals", _fake_backfill([_finished()]))
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": False, "reason": "db down"})
    out = cli.run(max_passes=2, max_association_lookups=5000, restart=False)

    assert out["ok"] is False
    assert "unreadable" in out["reason"]


def test_cli_restart_applies_only_to_the_first_pass(monkeypatch):
    """Re-reading the whole portal on every pass would never converge."""
    from scripts import backfill_canonical_deal_ledger as cli

    runner = _install_cli(monkeypatch, [_capped(), _capped(), _finished()],
                          state_row=_COMPLETE_ROW)
    cli.run(max_passes=10, max_association_lookups=5000, restart=True)
    assert [c["restart"] for c in runner.calls] == [True, False, False]


def test_cli_passes_the_lookup_cap_through(monkeypatch):
    from scripts import backfill_canonical_deal_ledger as cli

    runner = _install_cli(monkeypatch, [_finished()], state_row=_COMPLETE_ROW)
    cli.run(max_passes=2, max_association_lookups=250, restart=False)
    assert runner.calls[0]["max_association_lookups"] == 250


def test_cli_empty_but_PROVEN_portal_passes(monkeypatch):
    """A genuinely empty portal that proved end-of-results is complete."""
    from scripts import backfill_canonical_deal_ledger as cli

    empty = _finished()
    empty.update({"deals_seen": 0, "written": 0, "watermark": None})
    _install_cli(monkeypatch, [empty], state_row=_COMPLETE_ROW)
    out = cli.run(max_passes=2, max_association_lookups=5000, restart=False)
    assert out["ok"] is True
    assert out["deals_seen_total"] == 0


def test_cli_empty_and_UNPROVEN_portal_fails(monkeypatch):
    """Zero deals and no proof of end-of-results is not an empty portal — it is
    a read that told us nothing."""
    from scripts import backfill_canonical_deal_ledger as cli

    unproven = _broken(error="pull_unavailable")
    _install_cli(monkeypatch, [unproven], state_row=_IN_PROGRESS_ROW)
    out = cli.run(max_passes=2, max_association_lookups=5000, restart=False)
    assert out["ok"] is False


def test_cli_json_is_valid_and_carries_no_pii(monkeypatch, capsys):
    from scripts import backfill_canonical_deal_ledger as cli

    _install_cli(monkeypatch, [_capped(), _finished()],
                 state_row=_COMPLETE_ROW)
    monkeypatch.setattr(sys, "argv", ["backfill", "--json", "--max-passes", "5"])
    code = cli.main()
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["passes_run"] == 2
    blob = json.dumps(payload).lower()
    for banned in ("email", "@", "firstname", "lastname", "deal_name",
                   "company", "gclid"):
        assert banned not in blob, banned


def test_cli_exit_codes(monkeypatch, capsys):
    from scripts import backfill_canonical_deal_ledger as cli

    _install_cli(monkeypatch, [_broken()], state_row=_IN_PROGRESS_ROW)
    monkeypatch.setattr(sys, "argv", ["backfill", "--json"])
    assert cli.main() == cli.EXIT_FAILED
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["backfill", "--max-passes", "0"])
    assert cli.main() == cli.EXIT_USAGE


def test_cli_summary_carries_only_non_sensitive_fields():
    fn = _BACKFILL_PY.split("def _pass_summary(")[1].split("\ndef ")[0]
    for banned in ("deal_name", "email", "contact", "company", "gclid"):
        assert banned not in fn, banned
    for expected in ("status", "deals_seen", "written", "complete",
                     "association_failures", "write_failures"):
        assert expected in fn


def test_cli_opens_no_http_endpoint_and_mutates_nothing_external():
    code = _code_only(_BACKFILL_PY).lower()
    for banned in ("fastapi", "@app.", "router", "flask", "uvicorn",
                   "requests.post", "requests.put", "requests.patch",
                   "requests.delete", "mailchimp", "googleads", "google.ads"):
        assert banned not in code, banned


# =============================================================================
# The all-window audit gate
# =============================================================================
def test_gate_windows_cover_every_required_business_window():
    from scripts.audit_canonical_revenue_truth import GATE_WINDOWS

    for window in ("current_quarter", "last_quarter", "last_6_months", "ytd",
                   "all_time"):
        assert window in GATE_WINDOWS, window


def test_gate_windows_are_all_valid_business_windows():
    from analysis.business_windows import WINDOW_KEYS
    from scripts.audit_canonical_revenue_truth import GATE_WINDOWS

    for window in GATE_WINDOWS:
        assert window in WINDOW_KEYS, window


def _install_audit(monkeypatch, per_window):
    import db.connection as connection
    import services.revenue_reconciliation_service as recon_mod

    monkeypatch.setattr(connection, "init_pool", lambda *a, **k: None)

    def _build(window, now=None):
        return per_window[window]

    monkeypatch.setattr(recon_mod, "build_revenue_reconciliation", _build)


def _window_report(window, ok=True, **extra):
    report = {"available": True, "window": window, "ok": ok,
              "violations": [] if ok else [f"{window} failed"],
              "violation_codes": [] if ok else ["bootstrap_not_complete"],
              "canonical": {}, "legacy_diffs": [], "sync_state": {},
              "stage_breakdown": [], "stage_breakdown_available": True}
    report.update(extra)
    return report


def test_all_windows_passes_only_when_every_window_passes(monkeypatch, capsys):
    from scripts import audit_canonical_revenue_truth as audit

    _install_audit(monkeypatch,
                   {w: _window_report(w) for w in audit.GATE_WINDOWS})
    monkeypatch.setattr(sys, "argv", ["audit", "--all-windows", "--json"])
    code = audit.main()
    payload = json.loads(capsys.readouterr().out)

    assert code == audit.EXIT_OK
    assert payload["ok"] is True
    assert set(payload["results"]) == set(audit.GATE_WINDOWS)
    assert payload["failing_windows"] == []


def test_one_failing_window_fails_all_windows(monkeypatch, capsys):
    from scripts import audit_canonical_revenue_truth as audit

    reports = {w: _window_report(w) for w in audit.GATE_WINDOWS}
    reports["ytd"] = _window_report("ytd", ok=False)
    _install_audit(monkeypatch, reports)
    monkeypatch.setattr(sys, "argv", ["audit", "--all-windows", "--json"])
    code = audit.main()
    payload = json.loads(capsys.readouterr().out)

    assert code == audit.EXIT_VALIDATION_FAILED
    assert payload["ok"] is False
    assert payload["failing_windows"] == ["ytd"]


def test_one_unavailable_window_fails_all_windows(monkeypatch, capsys):
    from scripts import audit_canonical_revenue_truth as audit

    reports = {w: _window_report(w) for w in audit.GATE_WINDOWS}
    reports["all_time"] = {"available": False, "reason": "ledger_unavailable"}
    _install_audit(monkeypatch, reports)
    monkeypatch.setattr(sys, "argv", ["audit", "--all-windows", "--json"])
    code = audit.main()
    payload = json.loads(capsys.readouterr().out)

    assert code == audit.EXIT_VALIDATION_FAILED
    assert payload["failing_windows"] == ["all_time"]


def test_human_output_names_every_failing_window(monkeypatch, capsys):
    from scripts import audit_canonical_revenue_truth as audit

    reports = {w: _window_report(w) for w in audit.GATE_WINDOWS}
    reports["ytd"] = _window_report("ytd", ok=False)
    reports["last_quarter"] = _window_report("last_quarter", ok=False)
    _install_audit(monkeypatch, reports)
    monkeypatch.setattr(sys, "argv", ["audit", "--all-windows"])
    code = audit.main()
    out = capsys.readouterr().out

    assert code == audit.EXIT_VALIDATION_FAILED
    assert "FAILING WINDOWS:" in out
    assert "ytd" in out.split("FAILING WINDOWS:")[1]
    assert "last_quarter" in out.split("FAILING WINDOWS:")[1]
    assert "aggregate ok = False" in out


def test_window_and_all_windows_are_mutually_exclusive(monkeypatch, capsys):
    from scripts import audit_canonical_revenue_truth as audit

    monkeypatch.setattr(sys, "argv",
                        ["audit", "--window", "ytd", "--all-windows"])
    assert audit.main() == audit.EXIT_USAGE
    assert "mutually exclusive" in capsys.readouterr().err


def test_single_window_mode_still_works(monkeypatch, capsys):
    from scripts import audit_canonical_revenue_truth as audit

    _install_audit(monkeypatch, {"ytd": _window_report("ytd")})
    monkeypatch.setattr(sys, "argv", ["audit", "--window", "ytd", "--json"])
    code = audit.main()
    payload = json.loads(capsys.readouterr().out)

    assert code == audit.EXIT_OK
    # A single window returns the report itself, unchanged from 153E-A.
    assert payload["window"] == "ytd"
    assert "results" not in payload


def test_an_unknown_window_is_a_usage_error(monkeypatch, capsys):
    from scripts import audit_canonical_revenue_truth as audit

    monkeypatch.setattr(sys, "argv", ["audit", "--window", "last_fortnight"])
    assert audit.main() == audit.EXIT_USAGE
    assert "Unknown window" in capsys.readouterr().err


def test_an_import_failure_is_a_failed_window_not_a_crash(monkeypatch, capsys):
    """A broken dependency raises at IMPORT time. Letting that escape would
    crash the process instead of reporting a failed window — turning a gate
    failure into an absence of a result, and taking the aggregate with it."""
    import builtins

    import db.connection as connection
    from scripts import audit_canonical_revenue_truth as audit

    real_import = builtins.__import__

    def _explode(name, *args, **kwargs):
        if name == "services.revenue_reconciliation_service":
            raise ImportError("cannot import name 'build_revenue_reconciliation'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(connection, "init_pool", lambda *a, **k: None)
    monkeypatch.setattr(builtins, "__import__", _explode)
    monkeypatch.setattr(sys, "argv", ["audit", "--all-windows", "--json"])

    code = audit.main()
    monkeypatch.undo()
    payload = json.loads(capsys.readouterr().out)

    assert code == audit.EXIT_VALIDATION_FAILED
    assert payload["ok"] is False
    assert payload["failing_windows"] == list(audit.GATE_WINDOWS)


def test_the_window_runner_imports_inside_its_try():
    fn = _AUDIT_PY.split("def _audit_window(")[1].split("\ndef ")[0]
    body = fn.split("try:")[1]
    assert "from services.revenue_reconciliation_service import" in body, (
        "the import sits outside the try, so an ImportError crashes the CLI "
        "instead of being reported as a failed audit")


def test_an_audit_that_cannot_run_is_a_failure(monkeypatch, capsys):
    from scripts import audit_canonical_revenue_truth as audit

    import db.connection as connection
    import services.revenue_reconciliation_service as recon_mod

    def _boom(window, now=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(connection, "init_pool", lambda *a, **k: None)
    monkeypatch.setattr(recon_mod, "build_revenue_reconciliation", _boom)
    monkeypatch.setattr(sys, "argv", ["audit", "--all-windows", "--json"])
    code = audit.main()
    payload = json.loads(capsys.readouterr().out)

    assert code == audit.EXIT_VALIDATION_FAILED
    assert payload["ok"] is False
    assert payload["failing_windows"] == list(audit.GATE_WINDOWS)


# =============================================================================
# Preserved 153E-A protections and doctrine
# =============================================================================
def test_amount_tolerance_is_unchanged():
    assert recon.AMOUNT_TOLERANCE_USD == 0.01
    assert "AMOUNT_TOLERANCE_USD = 0.01" in _RECON_SERVICE_PY


def test_existing_reconciliation_invariants_are_all_still_present():
    fn = _RECON_SERVICE_PY.split("def _check_invariants(")[1].split("\ndef ")[0]
    for code in (recon.V_DEAL_ID_DUPLICATED, recon.V_WON_WITHOUT_PREDICATE,
                 recon.V_UNPROVEN_CURRENCY_IN_TOTAL,
                 recon.V_FAILED_LOOKUP_AS_CLASSIFICATION,
                 recon.V_ROWS_DISAGREE_WITH_SUMMARY,
                 recon.V_CURRENCY_COMPLETENESS_MISREPORTED,
                 recon.V_LEGACY_LEDGER_UNAVAILABLE,
                 recon.V_LEGACY_DEAL_MISSING_FROM_CANONICAL,
                 recon.V_UNEXPLAINED_DIFFERENCE):
        assert code.split("_")[0] in fn or code in _RECON_SERVICE_PY


def test_the_won_predicate_is_still_the_only_one():
    for text in (_RECON_SERVICE_PY, _LEDGER_REPO_PY, _SYNC_SERVICE_PY,
                 _BACKFILL_PY):
        code = _code_only(text)
        assert "ILIKE '%won%'" not in code or "gclid_attribution" in code
    # The canonical won filter is the boolean.
    assert "hs_is_closed_won IS TRUE" in _LEDGER_REPO_PY


def test_unavailable_is_never_rendered_as_zero():
    fmt = _AUDIT_PY.split("def _fmt(")[1].split("\ndef ")[0]
    assert 'return "Unavailable"' in fmt
    fmt2 = _BACKFILL_PY.split("def _fmt(")[1].split("\ndef ")[0]
    assert 'return "Unavailable"' in fmt2


def test_the_governance_block_still_says_shadow_mode():
    """This PR is an interlock. It does not authorise the cutover."""
    fn = _RECON_SERVICE_PY.split("def build_revenue_reconciliation(")[1]
    assert '"shadow_mode": True' in fn
    assert '"read_only": True' in fn
    assert '"external_writes": False' in fn


# ── Consumer boundary — nothing is switched in this PR ──────────────────────
CONSUMER_MODULES = [
    "services/dashboard_overview_service.py",
    "services/dashboard_revenue_service.py",
    "services/dashboard_campaigns_service.py",
    "services/dashboard_countries_service.py",
    "services/dashboard_deals_service.py",
    "services/dashboard_channels_service.py",
    "services/revenue_attribution_service.py",
    "services/unit_economics_service.py",
    "services/revenue_decision_mart_service.py",
]


def test_no_production_consumer_reads_the_canonical_ledger():
    for module in CONSUMER_MODULES:
        path = _ROOT / module
        if not path.exists():
            continue
        assert "hubspot_deal_ledger" not in path.read_text(), module
        assert "deal_ledger_repository" not in path.read_text(), module


def test_no_external_mutation_path_is_introduced():
    for text, label in ((_LEDGER_REPO_PY, "repo"),
                        (_SYNC_SERVICE_PY, "sync"),
                        (_RECON_SERVICE_PY, "recon"),
                        (_AUDIT_PY, "audit"),
                        (_BACKFILL_PY, "backfill")):
        code = _code_only(text).lower()
        for banned in ("requests.post", "requests.put", "requests.patch",
                       "requests.delete", "batch_api.update", "basic_api.update",
                       "basic_api.create", "mutate", "mailchimp"):
            assert banned not in code, f"{label}: {banned}"


def test_no_legacy_table_is_dropped_or_truncated():
    for text in (_LEDGER_REPO_PY, _SYNC_SERVICE_PY, _RECON_SERVICE_PY,
                 _AUDIT_PY, _BACKFILL_PY):
        code = _code_only(text).upper()
        for banned in ("DROP TABLE", "TRUNCATE TABLE",
                       "DELETE FROM GCLID_ATTRIBUTION",
                       "DELETE FROM DEAL_SOURCE_ATTRIBUTION",
                       "DELETE FROM DEALS"):
            assert banned not in code, banned


def test_no_application_startup_backfill_was_added():
    """A bootstrap that runs itself on boot would hammer HubSpot on every
    restart. It stays an operator command."""
    server = (_ROOT / "api" / "server.py").read_text()
    assert "backfill_canonical_deal_ledger" not in server
    assert "backfill_deals" not in server
