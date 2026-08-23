"""
tests/test_pr_ads_154b_truth_readiness.py

PR-ADS-154B §2/§3/§5 — like-for-like reconciliation, and execution health told
apart from truth readiness.

The database-level behaviour (the customer_id filters, the coverage ledger's
unique key, the repair loop) is proven against a real PostgreSQL cluster in
``tests/test_pr_ads_154b_coverage_repair_pg.py``. This suite covers the decision
logic layered on top of it:

  §2   a comparison across accounts or currencies is UNAVAILABLE, never a
       mismatch — a mismatch asserts a disagreement about the data, and this one
       is a disagreement about what was measured;
  §3   `execution_status` and `truth_status` answer different questions, and a
       clean run over incomplete data is `success` + `not_ready`;
  §5   identical totals reconcile; a real difference is a real mismatch; and
       `geo_ready` is False unless every condition holds.

Read-only: no external platform is contacted by any test here.

Run with:
    python -m pytest tests/test_pr_ads_154b_truth_readiness.py -v
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import services.google_ads_geo_sync_service as geo  # noqa: E402
import scheduler.incremental_sync as sched  # noqa: E402

_SCHED_SRC = (_ROOT / "scheduler" / "incremental_sync.py").read_text()
_REPAIR_SRC = (_ROOT / "services" / "canonical_coverage_repair_service.py").read_text()
_CLI_SRC = (_ROOT / "scripts" / "backfill_canonical_spend_fx.py").read_text()

#: A fully proven evidence set for the shared gate.
_PROVEN = {
    "campaign_spend_readable": True,
    "campaign_coverage_complete": True,
    "fx_complete": True,
    "geo_readable": True,
    "geo_coverage_readable": True,
    "geo_coverage_complete": True,
    "geo_failed_chunks": [],
    "missing_geo_dates": [],
    "campaigns_missing_geo": [],
    "comparison_like_for_like": True,
}


def _gate(**overrides):
    kwargs = {"reconciled": False, "residual_eligible": False, **_PROVEN}
    kwargs.update(overrides)
    return geo.resolve_country_spend_status(**kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# §2 — like-for-like is a precondition, not a result
# ═════════════════════════════════════════════════════════════════════════════

def test_identical_totals_reconcile():
    status, gaps = _gate(reconciled=True)
    assert status == geo.GEO_STATUS_VERIFIED
    assert gaps == []
    assert geo.country_geo_ready(status) is True


def test_a_genuine_difference_is_a_mismatch():
    """Complete coverage on both sides and the totals still disagree.

    This is the case `mismatch` is FOR, and PR-ADS-154B must not blunt it: the
    inputs are all proven, so the difference is about the data.
    """
    status, gaps = _gate(reconciled=False)
    assert status == geo.GEO_STATUS_MISMATCH
    assert gaps == [geo.GEO_GAP_TOTALS_DIFFER]
    assert geo.country_geo_ready(status) is False


def test_a_cross_account_comparison_is_unavailable_not_a_mismatch():
    """Totals measured over different accounts say nothing about each other."""
    status, gaps = _gate(reconciled=False, comparison_like_for_like=False)
    assert status == geo.GEO_STATUS_UNAVAILABLE
    assert geo.GEO_GAP_NOT_LIKE_FOR_LIKE in gaps
    assert geo.country_geo_ready(status) is False


def test_matching_totals_do_not_rescue_a_non_comparable_pair():
    """Two totals over different scopes that happen to agree prove nothing.

    Same doctrine as PR-ADS-153F blocker 1: agreement is not evidence when the
    things being compared were never established to be comparable.
    """
    status, gaps = _gate(reconciled=True, comparison_like_for_like=False)
    assert status == geo.GEO_STATUS_UNAVAILABLE
    assert geo.GEO_GAP_NOT_LIKE_FOR_LIKE in gaps


def test_a_safe_residual_cannot_bypass_the_scope_check():
    """PR-ADS-131's residual unblock is still gated on comparability."""
    status, gaps = _gate(reconciled=False, residual_eligible=True,
                         comparison_like_for_like=False)
    assert status == geo.GEO_STATUS_UNAVAILABLE
    assert geo.GEO_GAP_NOT_LIKE_FOR_LIKE in gaps


def test_the_scope_gap_code_has_its_own_operator_sentence():
    assert geo.GEO_GAP_NOT_LIKE_FOR_LIKE in geo.GEO_GAP_MESSAGES
    sentence = geo.GEO_GAP_MESSAGES[geo.GEO_GAP_NOT_LIKE_FOR_LIKE]
    assert "account" in sentence and "currenc" in sentence
    # It outranks every other gap: if the two sides are not comparable, none of
    # the other findings are worth reporting.
    assert geo._GEO_GAP_PRIORITY[0] == geo.GEO_GAP_NOT_LIKE_FOR_LIKE


def test_the_gate_refuses_to_run_without_the_scope_input():
    """Required, keyword-only, no default — the PR-ADS-153F doctrine.

    A permissive default on a safety precondition is the failure mode with a
    friendlier syntax.
    """
    kwargs = {"reconciled": True, "residual_eligible": False, **_PROVEN}
    kwargs.pop("comparison_like_for_like")
    with pytest.raises(TypeError):
        geo.resolve_country_spend_status(**kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# §3 — execution health vs truth readiness
# ═════════════════════════════════════════════════════════════════════════════

def _recon(**kw):
    base = {
        "status": "success", "available": True, "reconciled": True,
        "geo_ready": True, "geo_gap_codes": [],
        "campaign_coverage_complete": True, "fx_coverage_complete": True,
        "geo_coverage_complete": True,
    }
    base.update(kw)
    return {sched.LABEL_GEO_RECONCILIATION: base}


def test_a_fully_proven_run_is_ready_with_no_gap_codes():
    out = sched.build_truth_block(_recon())
    assert out["truth_status"] == sched.TRUTH_READY
    assert out["geo_ready"] is True
    assert out["gap_codes"] == []
    assert all(out[k] is True for k in
               ("campaign_coverage_complete", "fx_coverage_complete",
                "geo_coverage_complete", "geo_reconciled"))


def test_ingestion_can_succeed_while_truth_is_not_ready():
    """The production case this PR exists for.

    Every dataset ran cleanly and the reconciliation was performed — so
    execution is a genuine success — while campaign and FX coverage are
    incomplete and the totals do not reconcile. Reporting one number for both
    questions is what let "status: success" describe that run.
    """
    datasets = _recon(reconciled=False, geo_ready=False,
                      campaign_coverage_complete=False,
                      fx_coverage_complete=False,
                      geo_gap_codes=["campaign_coverage_incomplete",
                                     "fx_coverage_incomplete"])
    # Execution: nothing failed.
    assert sched._overall_status(datasets) == "success"
    # Truth: not ready, and it says exactly why.
    out = sched.build_truth_block(datasets)
    assert out["truth_status"] == sched.TRUTH_NOT_READY
    assert out["geo_ready"] is False
    assert out["gap_codes"] == ["campaign_coverage_incomplete",
                                "fx_coverage_incomplete"]


def test_an_unevaluated_reconciliation_is_unknown_not_not_ready():
    """A run that never reached the comparison learned nothing about the data.

    `not_ready` would report a finding about canonical completeness that nobody
    made; `unknown` is the honest third state.
    """
    out = sched.build_truth_block(_recon(status="failed", available=False,
                                         reconciled=False, geo_ready=False,
                                         geo_gap_codes=[]))
    assert out["truth_status"] == sched.TRUTH_UNKNOWN
    assert out["geo_ready"] is False
    assert out["gap_codes"] == ["geo_reconciliation_not_evaluated"]


def test_a_missing_reconciliation_dataset_is_unknown():
    out = sched.build_truth_block({})
    assert out["truth_status"] == sched.TRUTH_UNKNOWN
    assert out["geo_ready"] is False
    assert out["gap_codes"]


@pytest.mark.parametrize("short", [
    "campaign_coverage_complete", "fx_coverage_complete", "geo_coverage_complete",
])
def test_geo_ready_stays_false_when_any_single_coverage_input_is_short(short):
    """One missing input is enough, whatever the reconciliation says.

    Including the case where the totals DO agree — two figures over a
    half-fetched range can match and mean nothing.
    """
    out = sched.build_truth_block(_recon(**{short: False}))
    assert out["geo_ready"] is False
    assert out["truth_status"] == sched.TRUTH_NOT_READY


def test_geo_ready_stays_false_when_the_gate_itself_says_blocked():
    """Even with all three coverage flags true and the totals agreeing.

    `geo_ready` is the shared gate's verdict, not a local recomputation of it;
    if the gate blocked for a reason this block does not model, the block must
    still report blocked.
    """
    out = sched.build_truth_block(_recon(geo_ready=False))
    assert out["geo_ready"] is False
    assert out["truth_status"] == sched.TRUTH_NOT_READY


def test_an_aborted_run_publishes_the_full_truth_shape(monkeypatch):
    """Every result carries the same keys, so no consumer tests which shape it got."""
    monkeypatch.setattr(sched, "ensure_database_ready",
                        lambda: (False, "connection pool is not available"))
    out = sched.run_daily_incremental_sync()

    assert out["status"] == "failed"
    assert out["execution_status"] == "failed"
    assert out["truth_status"] == sched.TRUTH_UNKNOWN
    assert out["geo_ready"] is False
    assert out["gap_codes"] == [sched.DB_UNAVAILABLE_REASON]
    assert out["datasets"] == {}
    for key in ("campaign_coverage_complete", "fx_coverage_complete",
                "geo_coverage_complete", "geo_reconciled"):
        assert out[key] is False


def test_execution_status_and_status_always_agree():
    """Two names, one verdict — they must never be able to drift apart.

    Asserted on the source: `execution_status` is assigned from the same
    expression as `status`, not computed a second time.
    """
    tree = ast.parse(_SCHED_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_daily_incremental_sync")
    summary = next(n for n in ast.walk(fn)
                   if isinstance(n, ast.Dict)
                   and any(isinstance(k, ast.Constant) and k.value == "execution_status"
                           for k in n.keys if k is not None))
    values = {k.value: v for k, v in zip(summary.keys, summary.values)
              if isinstance(k, ast.Constant)}
    assert isinstance(values["status"], ast.Name)
    assert isinstance(values["execution_status"], ast.Name)
    assert values["status"].id == values["execution_status"].id


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the repair command's contract, asserted on the source
# ═════════════════════════════════════════════════════════════════════════════

def test_the_repair_command_exits_on_proven_coverage_not_on_completion():
    """Exit 0 is a claim about the RANGE, never about the run finishing.

    `return 0 if outcome["coverage"]["ok"] else 1` — and `ok` is set from a
    re-read of the durable ledgers, so a backfill that "succeeded" over a range
    that is still short exits 1.
    """
    assert 'return 0 if outcome["coverage"]["ok"] else 1' in _CLI_SRC
    assert 'ok = bool(spend_coverage["complete"] and fx_coverage["complete"])' in _REPAIR_SRC


def test_the_repair_service_owns_no_ingestion_of_its_own():
    """It orchestrates the SAME functions the scheduler calls.

    A second copy of the spend fetch or the FX fetch would be a second set of
    rules to keep in step — the defect class this programme removes.
    """
    assert "run_google_ads_spend_backfill" in _REPAIR_SRC
    assert "ensure_fx_rates" in _REPAIR_SRC
    # No direct connector import anywhere in the repair path.
    assert "connectors." not in _REPAIR_SRC
    assert "connectors." not in _CLI_SRC


def test_windsor_appears_nowhere_in_the_repair_path():
    """Campaign spend comes from the Google Ads API. Asserted, not assumed.

    Checked on the AST — imports, names and attributes — rather than on the
    text. A text search matches the docstring that explains Windsor is NOT used,
    so it fails on correct code and passes on a commented-out call: precisely
    backwards. This walks what the module actually references.
    """
    for src in (_REPAIR_SRC, _CLI_SRC):
        tree = ast.parse(src)
        # Docstring nodes identified STRUCTURALLY — the first statement of a
        # scope, when it is a bare string. Comparing against
        # `ast.get_docstring()` does not work: it re-indents the text, so the
        # cleaned value never equals the raw node's.
        docstring_nodes = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) and body:
                first = body[0]
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstring_nodes.add(id(first.value))

        referenced: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                referenced.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                referenced.add(node.module or "")
                referenced.update(a.name for a in node.names)
            elif isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstring_nodes):
                # A string literal could still name a Windsor dataset key.
                referenced.add(node.value)
        assert not [r for r in referenced if "windsor" in r.lower()]


def test_the_cli_never_interpolates_a_raw_exception():
    """PR-ADS-154A: a connection failure carries the DSN, and a DSN carries the
    password. Operator-facing text goes through the redactor."""
    assert "safe_db_error(exc)" in _CLI_SRC
    assert "{exc}" not in _CLI_SRC.replace("safe_db_error(exc)", "")


def test_the_repair_treats_both_boundaries_as_inclusive():
    """Matching every coverage query's `>= start AND <= end`."""
    assert '"bounds": "inclusive_start_inclusive_end"' in _REPAIR_SRC
