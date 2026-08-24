"""
tests/test_pr_ads_154b_f1_residual_truth_readiness.py

PR-ADS-154B-F1 — an ACCEPTED geo residual is truth-ready, and everything else
that blocked still blocks.

The defect
──────────
`build_truth_block` required `reconciled` — exact equality of the campaign and
geo totals — *on top of* the shared gate's `geo_ready`. That is the mistake
PR-ADS-153F blocker 1 removed one layer down, reintroduced one layer up.

The shared gate accepts `reconciled_with_residual`: the PR-ADS-131 case where
Google Ads' geographic view does not assign some spend to any country, the
shortfall is published as an explicit residual bucket, and every coverage, scope
and completeness condition passes. The totals genuinely do not match, by design,
and the country figures are still safe to use.

Production hit exactly that state and reported::

    country_spend_status: reconciled_with_residual   geo_ready: true   (dataset)
    truth_status: not_ready                          geo_ready: false  (top level)
    gap_codes: []            <- blocked, with nothing to act on

Blocked, with an empty gap list. Unactionable: the reader is told to fix
something and not told what.

What this suite fixes in place
─────────────────────────────
  1  exact reconciliation is still ready;
  2  an accepted residual is ready, while `geo_reconciled` stays False;
  3  a genuine mismatch still blocks;
  4-6 an accepted residual cannot bypass incomplete campaign / FX / geo coverage;
  7  a non-like-for-like comparison still blocks;
  8  a blocked verdict can never publish an empty gap list.

Read-only: no external platform and no database is touched by any test here.

Run with:
    python -m pytest tests/test_pr_ads_154b_f1_residual_truth_readiness.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import scheduler.incremental_sync as sched  # noqa: E402
import services.google_ads_geo_sync_service as geo  # noqa: E402


def _recon(**kw):
    """A fully proven, exactly-reconciled reconciliation dataset, plus overrides."""
    base = {
        "status": "success",
        "reconciliation_status": "reconciled",
        "available": True,
        "reconciled": True,
        "country_spend_status": geo.GEO_STATUS_VERIFIED,
        "geo_ready": True,
        "geo_gap_codes": [],
        "campaign_coverage_complete": True,
        "fx_coverage_complete": True,
        "geo_coverage_complete": True,
        "comparison_like_for_like": True,
    }
    base.update(kw)
    return {sched.LABEL_GEO_RECONCILIATION: base}


#: The PR-ADS-131 accepted residual, exactly as production reported it.
_ACCEPTED_RESIDUAL = dict(
    reconciliation_status="mismatch",
    reconciled=False,
    country_spend_status=geo.GEO_STATUS_RECONCILED_WITH_RESIDUAL,
    geo_ready=True,
    geo_gap_codes=[],
    reason="geo_report_does_not_reconcile_by_design",
)


# ═════════════════════════════════════════════════════════════════════════════
# 1-2 — both accepted states are ready
# ═════════════════════════════════════════════════════════════════════════════

def test_1_exact_reconciliation_is_ready():
    """The unchanged case, asserted so the fix cannot regress it."""
    out = sched.build_truth_block(_recon())
    assert out["truth_status"] == sched.TRUTH_READY
    assert out["geo_ready"] is True
    assert out["geo_reconciled"] is True
    assert out["gap_codes"] == []


def test_2_an_accepted_residual_is_ready_and_still_reports_no_exact_match():
    """The production payload, verbatim.

    Both halves matter. Ready, because the shared gate accepted the residual and
    every input is proven — and `geo_reconciled` still False, because the raw
    totals genuinely do not match and claiming they do would be the opposite
    error to the one being fixed.
    """
    out = sched.build_truth_block(_recon(**_ACCEPTED_RESIDUAL))
    assert out["truth_status"] == sched.TRUTH_READY
    assert out["geo_ready"] is True
    assert out["geo_reconciled"] is False
    assert out["gap_codes"] == []


def test_2b_readiness_does_not_require_exact_reconciliation():
    """Asserted on behaviour: the ONLY difference between these two inputs is
    `reconciled`, and both are ready. That is the property the fix establishes.
    """
    exact = sched.build_truth_block(_recon())
    residual = sched.build_truth_block(_recon(**_ACCEPTED_RESIDUAL))
    assert exact["truth_status"] == residual["truth_status"] == sched.TRUTH_READY
    assert exact["geo_reconciled"] != residual["geo_reconciled"]


# ═════════════════════════════════════════════════════════════════════════════
# 3 — a genuine mismatch still blocks
# ═════════════════════════════════════════════════════════════════════════════

def test_3_a_genuine_mismatch_stays_not_ready():
    """Totals disagree, the gate refused, and it says why.

    The difference from case 2 is not the arithmetic — it is that the shared gate
    did NOT accept this shortfall as an explained residual.
    """
    out = sched.build_truth_block(_recon(
        reconciliation_status="mismatch",
        reconciled=False,
        country_spend_status=geo.GEO_STATUS_MISMATCH,
        geo_ready=False,
        geo_gap_codes=[geo.GEO_GAP_TOTALS_DIFFER],
    ))
    assert out["truth_status"] == sched.TRUTH_NOT_READY
    assert out["geo_ready"] is False
    assert out["gap_codes"] == [geo.GEO_GAP_TOTALS_DIFFER]


def test_3b_missing_geo_dates_stay_blocked_even_with_a_residual_status():
    """PR-ADS-131's own rule, re-asserted at this layer.

    Days with campaign spend and no geographic spend at all are missing data, not
    unattributed spend, and the gate refuses them. This layer must not rescue
    that by reading `country_spend_status` and stopping there.
    """
    out = sched.build_truth_block(_recon(
        reconciled=False,
        country_spend_status=geo.GEO_STATUS_MISMATCH,
        geo_ready=False,
        geo_gap_codes=[geo.GEO_GAP_MISSING_DATES],
    ))
    assert out["truth_status"] == sched.TRUTH_NOT_READY
    assert out["gap_codes"] == [geo.GEO_GAP_MISSING_DATES]


# ═════════════════════════════════════════════════════════════════════════════
# 4-6 — an accepted residual cannot bypass unproven coverage
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("short,gap", [
    ("campaign_coverage_complete", geo.GEO_GAP_CAMPAIGN_COVERAGE_INCOMPLETE),
    ("fx_coverage_complete", geo.GEO_GAP_FX_COVERAGE_INCOMPLETE),
    ("geo_coverage_complete", geo.GEO_GAP_GEO_COVERAGE_INCOMPLETE),
])
def test_4_5_6_a_residual_cannot_bypass_incomplete_coverage(short, gap):
    """Each of the three coverage inputs, independently fatal.

    The residual explains a SHORTFALL between two totals. It says nothing about
    whether the range behind either of them was fetched, so it must not stand in
    for coverage that was never proven.
    """
    out = sched.build_truth_block(_recon(
        **{**_ACCEPTED_RESIDUAL, short: False,
           "geo_ready": False, "geo_gap_codes": [gap]}))
    assert out["truth_status"] == sched.TRUTH_NOT_READY
    assert out["geo_ready"] is False
    assert out[short] is False
    assert gap in out["gap_codes"]


@pytest.mark.parametrize("short", [
    "campaign_coverage_complete", "fx_coverage_complete", "geo_coverage_complete",
])
def test_4b_5b_6b_incomplete_coverage_blocks_even_if_the_gate_reported_ready(short):
    """Belt and braces: the coverage flags are checked here as well.

    If the dataset ever reported `geo_ready: true` alongside an incomplete
    coverage input, the two disagree and this layer takes the strict side rather
    than deferring. The gate already requires all three, so this is a guard
    against a future inconsistency, not a second opinion.
    """
    out = sched.build_truth_block(_recon(**{short: False}))
    assert out["truth_status"] == sched.TRUTH_NOT_READY
    assert out["geo_ready"] is False
    assert out["gap_codes"], "a blocked verdict must carry a reason"


# ═════════════════════════════════════════════════════════════════════════════
# 7 — scope is still a precondition
# ═════════════════════════════════════════════════════════════════════════════

def test_7_a_non_like_for_like_comparison_stays_blocked():
    """PR-ADS-154B §2, unchanged by this fix.

    Totals measured over different accounts or currencies say nothing about each
    other, residual or no residual.
    """
    out = sched.build_truth_block(_recon(**{
        **_ACCEPTED_RESIDUAL,
        "comparison_like_for_like": False,
        "geo_ready": False,
        "geo_gap_codes": [geo.GEO_GAP_NOT_LIKE_FOR_LIKE],
    }))
    assert out["truth_status"] == sched.TRUTH_NOT_READY
    assert out["geo_ready"] is False
    assert out["comparison_like_for_like"] is False
    assert geo.GEO_GAP_NOT_LIKE_FOR_LIKE in out["gap_codes"]


def test_7b_a_missing_like_for_like_flag_is_read_fail_closed():
    """An absent field is not evidence that the fact holds."""
    ds = _recon(**_ACCEPTED_RESIDUAL)
    del ds[sched.LABEL_GEO_RECONCILIATION]["comparison_like_for_like"]
    out = sched.build_truth_block(ds)
    assert out["truth_status"] == sched.TRUTH_NOT_READY
    assert out["gap_codes"]


# ═════════════════════════════════════════════════════════════════════════════
# 8 — a blocked verdict always carries something to act on
# ═════════════════════════════════════════════════════════════════════════════

def test_8_a_not_ready_result_can_never_publish_an_empty_gap_list():
    """The incoherent state production actually emitted.

    Here the inputs are deliberately contradictory — the gate says blocked and
    reports no gaps — which is the shape that produced `not_ready` + `geo_ready:
    false` + `gap_codes: []`. It is now impossible to emit: the inconsistency
    becomes its own code.
    """
    out = sched.build_truth_block(_recon(geo_ready=False, geo_gap_codes=[]))
    assert out["truth_status"] == sched.TRUTH_NOT_READY
    assert out["gap_codes"] == [sched.GAP_NOT_READY_WITHOUT_REASON]


def test_8b_the_defensive_code_is_logged_with_the_inputs_and_no_secrets(caplog):
    """An operator needs to see WHICH input contradicted, not just that one did."""
    import logging
    with caplog.at_level(logging.WARNING):
        sched.build_truth_block(_recon(geo_ready=False, geo_gap_codes=[]))
    text = caplog.text
    assert sched.GAP_NOT_READY_WITHOUT_REASON in text
    assert "geo_ready=False" in text
    for secret in ("postgres://", "postgresql://", "password"):
        assert secret not in text.lower()


def test_8c_every_blocked_shape_carries_at_least_one_gap_code():
    """Swept across the blocking inputs, including the empty-gap variants."""
    blocked = [
        _recon(geo_ready=False, geo_gap_codes=[]),
        _recon(campaign_coverage_complete=False, geo_gap_codes=[]),
        _recon(fx_coverage_complete=False, geo_gap_codes=[]),
        _recon(geo_coverage_complete=False, geo_gap_codes=[]),
        _recon(comparison_like_for_like=False, geo_gap_codes=[]),
        _recon(available=False, geo_gap_codes=[], reason=None),
        {},
    ]
    for ds in blocked:
        out = sched.build_truth_block(ds)
        assert out["truth_status"] != sched.TRUTH_READY
        assert out["gap_codes"], f"blocked with no reason: {ds}"


def test_8d_an_unevaluated_run_is_still_unknown_rather_than_not_ready():
    """Unchanged by this fix, and asserted so it stays that way."""
    out = sched.build_truth_block(_recon(available=False, geo_ready=False))
    assert out["truth_status"] == sched.TRUTH_UNKNOWN
    assert out["geo_ready"] is False
    assert out["gap_codes"]


# ═════════════════════════════════════════════════════════════════════════════
# Coherence — the published payload can never contradict itself
# ═════════════════════════════════════════════════════════════════════════════

def test_top_level_geo_ready_always_agrees_with_truth_status():
    """`geo_ready` and `truth_status` are two names for one verdict here.

    They disagreed in production — dataset `geo_ready: true` beside top-level
    `geo_ready: false` — so this asserts the invariant across every shape above.
    """
    for ds in (_recon(), _recon(**_ACCEPTED_RESIDUAL),
               _recon(geo_ready=False, geo_gap_codes=["x"]),
               _recon(available=False), {}):
        out = sched.build_truth_block(ds)
        assert out["geo_ready"] is (out["truth_status"] == sched.TRUTH_READY)


def test_the_accepted_states_here_are_the_shared_gates_accepted_states():
    """This layer must not keep its own list of what counts as usable.

    If PR-ADS-131's residual state were ever dropped from the shared gate, or a
    new accepted state added, this suite's premise would move with it rather than
    silently diverge.
    """
    assert geo.country_geo_ready(geo.GEO_STATUS_VERIFIED) is True
    assert geo.country_geo_ready(geo.GEO_STATUS_RECONCILED_WITH_RESIDUAL) is True
    assert geo.country_geo_ready(geo.GEO_STATUS_MISMATCH) is False
    assert geo.country_geo_ready(geo.GEO_STATUS_UNAVAILABLE) is False


def test_readiness_reads_the_gate_verdict_rather_than_rebuilding_it():
    """Asserted on the source: no `reconciled` term in the readiness condition.

    A text guard would trip on the docstring explaining the removal, so this
    walks the AST of the readiness branch and checks which names it tests.
    """
    import ast
    src = (_ROOT / "scheduler" / "incremental_sync.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "build_truth_block")
    # The `elif` that decides TRUTH_READY.
    ready_tests = [
        n.test for n in ast.walk(fn)
        if isinstance(n, ast.If)
        and any(isinstance(s, ast.Assign)
                and isinstance(s.value, ast.Name) and s.value.id == "TRUTH_READY"
                for s in n.body)
    ]
    assert len(ready_tests) == 1, "expected exactly one readiness condition"
    names = {n.id for n in ast.walk(ready_tests[0]) if isinstance(n, ast.Name)}
    assert "geo_ready" in names, "readiness must defer to the shared gate's verdict"
    assert "reconciled" not in names, (
        "exact reconciliation must not be a precondition of readiness — that is "
        "the defect PR-ADS-154B-F1 removes")
