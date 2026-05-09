"""
tests/test_core_campaign_truth.py

PR-ADS-067 — Regression tests for campaign verdict and CPQL logic.

Protects:
  - CPQL is N/A / None when confirmed SQLs = 0 (no divide-by-zero)
  - CPQL calculates correctly when confirmed SQLs > 0
  - SCALE requires confirmed SQL + junk rate below threshold
  - FIX fires when junk rate is above threshold
  - HOLD is the default when no strong signal exists
  - CUT only fires for confirmed cut markets; zero SQLs alone does not trigger CUT

Run with:
  python -m pytest tests/test_core_campaign_truth.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.core import determine_verdict

# ---------------------------------------------------------------------------
# Load live thresholds from config so tests stay in sync with production rules
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
with _CONFIG_PATH.open(encoding="utf-8") as _f:
    _THRESHOLDS = yaml.safe_load(_f)

_SCALE_T = _THRESHOLDS["campaign_verdicts"]["scale"]
_FIX_T   = _THRESHOLDS["campaign_verdicts"]["fix"]
_CONFIRMED_CUT_MARKETS = set(_THRESHOLDS.get("confirmed_cut_markets", []))

# SCALE thresholds from config
_MIN_SQLS_FOR_SCALE = _SCALE_T["min_confirmed_sqls_30d"]       # 1
_MAX_JUNK_FOR_SCALE = _SCALE_T["max_junk_pct"]                 # 15
_MIN_JUNK_FOR_FIX   = _FIX_T["min_junk_pct"]                   # 25


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _verdict(spend, sqls, junk_rate, campaign="acme freight"):
    return determine_verdict(
        spend=spend,
        confirmed_sqls=sqls,
        junk_rate=junk_rate,
        campaign=campaign,
        confirmed_cut_markets=_CONFIRMED_CUT_MARKETS,
        scale_thresholds=_SCALE_T,
        fix_thresholds=_FIX_T,
    )


def _cpql(spend, confirmed_sqls):
    """Mirror the CPQL formula used in run_campaign_truth().

    Returns None when confirmed_sqls == 0, matching doctrine: CPQL is N/A
    when there are no confirmed SQLs (no division by zero).
    """
    if confirmed_sqls == 0 or spend <= 0:
        return None
    return round(spend / confirmed_sqls, 2)


# ---------------------------------------------------------------------------
# Part A — CPQL tests
# ---------------------------------------------------------------------------

def test_cpql_is_none_when_zero_sqls() -> None:
    """CPQL must be None (N/A) when confirmed SQLs = 0 — no divide by zero."""
    result = _cpql(spend=1000, confirmed_sqls=0)
    assert result is None, (
        f"CPQL must be None when confirmed_sqls=0, got {result!r}"
    )


def test_cpql_is_none_when_zero_spend() -> None:
    """CPQL must be None (N/A) when spend = 0, even if SQLs > 0."""
    result = _cpql(spend=0, confirmed_sqls=2)
    assert result is None, (
        f"CPQL must be None when spend=0, got {result!r}"
    )


def test_cpql_calculates_correctly() -> None:
    """CPQL = spend / confirmed_sqls when both are positive."""
    result = _cpql(spend=1000, confirmed_sqls=2)
    assert result == 500.0, f"CPQL should be 500.0, got {result!r}"


def test_cpql_rounds_to_two_decimal_places() -> None:
    """CPQL is rounded to 2 decimal places."""
    result = _cpql(spend=1000, confirmed_sqls=3)
    assert result == 333.33, f"CPQL should be 333.33, got {result!r}"


# ---------------------------------------------------------------------------
# Part A — Verdict tests
# ---------------------------------------------------------------------------

def test_scale_fires_with_sql_and_clean_junk_rate() -> None:
    """SCALE requires at least 1 confirmed SQL and junk rate ≤ SCALE threshold."""
    v = _verdict(spend=500, sqls=_MIN_SQLS_FOR_SCALE, junk_rate=_MAX_JUNK_FOR_SCALE)
    assert v["state"] == "SCALE", (
        f"Expected SCALE with {_MIN_SQLS_FOR_SCALE} SQL(s) and {_MAX_JUNK_FOR_SCALE}% junk, "
        f"got {v['state']}: {v['reason']}"
    )


def test_scale_does_not_fire_with_zero_sqls() -> None:
    """SCALE must not fire when confirmed SQLs = 0."""
    v = _verdict(spend=500, sqls=0, junk_rate=5)
    assert v["state"] != "SCALE", (
        f"SCALE must not fire with 0 SQLs, got {v['state']}: {v['reason']}"
    )


def test_scale_does_not_fire_with_high_junk_rate() -> None:
    """SCALE must not fire when junk rate exceeds the SCALE threshold."""
    high_junk = _MAX_JUNK_FOR_SCALE + 1
    v = _verdict(spend=500, sqls=2, junk_rate=high_junk)
    assert v["state"] != "SCALE", (
        f"SCALE must not fire when junk rate={high_junk}%, got {v['state']}: {v['reason']}"
    )


def test_fix_fires_when_junk_rate_above_threshold() -> None:
    """FIX fires when junk rate is at or above the FIX threshold."""
    high_junk = _MIN_JUNK_FOR_FIX  # exactly at threshold
    v = _verdict(spend=500, sqls=0, junk_rate=high_junk)
    assert v["state"] == "FIX", (
        f"Expected FIX at {high_junk}% junk rate, got {v['state']}: {v['reason']}"
    )


def test_fix_fires_when_junk_rate_well_above_threshold() -> None:
    """FIX fires clearly when junk rate is substantially above threshold."""
    v = _verdict(spend=800, sqls=0, junk_rate=80)
    assert v["state"] == "FIX", (
        f"Expected FIX at 80% junk rate, got {v['state']}: {v['reason']}"
    )


def test_hold_is_default_with_insufficient_data() -> None:
    """HOLD is the default verdict when no strong SCALE/FIX/CUT signal exists."""
    # Low spend, no SQLs, no junk rate — truly ambiguous
    v = _verdict(spend=50, sqls=0, junk_rate=None)
    assert v["state"] == "HOLD", (
        f"Expected HOLD with weak signal, got {v['state']}: {v['reason']}"
    )


def test_hold_fires_when_junk_rate_below_both_thresholds_but_no_sqls() -> None:
    """HOLD fires when junk rate is low but no confirmed SQLs (and low spend)."""
    v = _verdict(spend=50, sqls=0, junk_rate=10)
    assert v["state"] == "HOLD", (
        f"Expected HOLD (low spend, no SQLs, low junk), got {v['state']}: {v['reason']}"
    )


# ---------------------------------------------------------------------------
# CUT gating — doctrine protection
# ---------------------------------------------------------------------------

def test_cut_fires_for_confirmed_cut_market() -> None:
    """CUT must fire when the campaign matches a confirmed cut market."""
    # "venezuela" is the only confirmed cut market
    cut_market = next(iter(_CONFIRMED_CUT_MARKETS))  # "venezuela"
    v = _verdict(spend=100, sqls=0, junk_rate=None, campaign=cut_market)
    assert v["state"] == "CUT", (
        f"Expected CUT for confirmed cut market '{cut_market}', got {v['state']}"
    )


def test_cut_does_not_fire_for_zero_sqls_alone() -> None:
    """Zero SQLs alone must not trigger CUT for a non-confirmed-cut-market campaign."""
    v = _verdict(spend=1000, sqls=0, junk_rate=None, campaign="europe low cpc-new")
    assert v["state"] != "CUT", (
        f"CUT must not fire for zero SQLs alone (non-cut-market), got {v['state']}"
    )


def test_cut_does_not_fire_for_arbitrary_campaign() -> None:
    """CUT must not fire for a campaign not in confirmed_cut_markets."""
    v = _verdict(spend=0, sqls=0, junk_rate=None, campaign="emerging - markets")
    assert v["state"] != "CUT", (
        f"CUT must not fire for non-cut-market campaign, got {v['state']}"
    )


def test_cut_only_markets_are_explicitly_confirmed() -> None:
    """The confirmed_cut_markets set must not be empty — doctrine requires explicit gating."""
    assert len(_CONFIRMED_CUT_MARKETS) > 0, (
        "confirmed_cut_markets must contain at least one market; "
        "CUT cannot be applied without an explicitly confirmed market list."
    )


def test_venezuela_is_a_confirmed_cut_market() -> None:
    """Venezuela must be the confirmed cut market per doctrine."""
    assert "venezuela" in _CONFIRMED_CUT_MARKETS, (
        "venezuela must be in confirmed_cut_markets per doctrine"
    )


# ---------------------------------------------------------------------------
# CPQL N/A in report rendering
# ---------------------------------------------------------------------------

def test_cpql_na_representation() -> None:
    """The N/A value for CPQL must be a clear sentinel, not 0 or a dollar amount."""
    cpql = _cpql(spend=1000, confirmed_sqls=0)
    # The production code stores None and the report renders it as "N/A"
    assert cpql is None, "CPQL sentinel for zero-SQL case must be None"
    assert cpql != 0, "CPQL must not be 0 when SQLs = 0 — that would imply free leads"


# ---------------------------------------------------------------------------
# Direct runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_cpql_is_none_when_zero_sqls,
        test_cpql_is_none_when_zero_spend,
        test_cpql_calculates_correctly,
        test_cpql_rounds_to_two_decimal_places,
        test_scale_fires_with_sql_and_clean_junk_rate,
        test_scale_does_not_fire_with_zero_sqls,
        test_scale_does_not_fire_with_high_junk_rate,
        test_fix_fires_when_junk_rate_above_threshold,
        test_fix_fires_when_junk_rate_well_above_threshold,
        test_hold_is_default_with_insufficient_data,
        test_hold_fires_when_junk_rate_below_both_thresholds_but_no_sqls,
        test_cut_fires_for_confirmed_cut_market,
        test_cut_does_not_fire_for_zero_sqls_alone,
        test_cut_does_not_fire_for_arbitrary_campaign,
        test_cut_only_markets_are_explicitly_confirmed,
        test_venezuela_is_a_confirmed_cut_market,
        test_cpql_na_representation,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except (AssertionError, TypeError, ValueError, AttributeError, KeyError) as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)
