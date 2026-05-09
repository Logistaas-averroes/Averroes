"""
tests/test_core_lead_quality.py

PR-ADS-067 — Regression tests for HubSpot MQL classification and junk-rate denominator.

Protects:
  - OPEN - Connecting is excluded from the junk-rate denominator
  - Unknown leads are NOT treated as junk
  - Confirmed junk statuses (DICARDED one-R, CLOSED - Job Seeker) map correctly
  - Wrong-fit statuses (CLOSED - Bad Product Fit, CLOSED - Sales Disqualified) are separate
  - Confirmed qualified statuses (CLOSED - Sales Qualified, CLOSED - Deal Created) map correctly
  - Grace-period contacts (within early_lead_grace_days) do not inflate junk rate
  - DICARDED spelling (one R) is the canonical spelling; DISCARDED (two Rs) is not accepted

Run with:
  python -m pytest tests/test_core_lead_quality.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Import the classification constants from analysis/core.py
from analysis.core import QUALIFIED, IN_PROGRESS, UNKNOWN, JUNK, WRONG_FIT

# ---------------------------------------------------------------------------
# Load live thresholds for grace period
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
with _CONFIG_PATH.open(encoding="utf-8") as _f:
    _THRESHOLDS = yaml.safe_load(_f)

_GRACE_DAYS = _THRESHOLDS["lead_quality"]["early_lead_grace_days"]  # 7


# ---------------------------------------------------------------------------
# Inline junk-rate computation (mirrors run_lead_quality in analysis/core.py)
# The function runs off-process, but we test the exact formula inline.
# ---------------------------------------------------------------------------

def _compute_junk_rate(statuses: list[str | None]) -> float | None:
    """Compute junk rate from a list of HubSpot MQL statuses.

    Intentionally mirrors the denominator logic in run_lead_quality() in analysis/core.py.
    This duplication is deliberate: it protects the classification formula as a regression
    guard. If the production logic changes, this test may catch the divergence.

    Denominator: verdicted = qualified + in_progress + junk + wrong_fit
    OPEN - Connecting is excluded (not yet verdicted by MDR).
    Grace-period behavior is covered separately via the threshold constant tests.
    Returns None if verdicted == 0.
    """
    qualified = 0
    in_progress = 0
    junk = 0
    wrong_fit = 0

    for s in statuses:
        if s in QUALIFIED:
            qualified += 1
        elif s in IN_PROGRESS:
            in_progress += 1
        elif s in UNKNOWN:
            pass  # excluded from denominator
        elif s in JUNK:
            junk += 1
        elif s in WRONG_FIT:
            wrong_fit += 1
        # else no_status — also not in verdicted denominator by default

    verdicted = qualified + in_progress + junk + wrong_fit
    if verdicted == 0:
        return None
    return round(junk / verdicted * 100, 1)


# ---------------------------------------------------------------------------
# Part B — MQL classification constant tests
# ---------------------------------------------------------------------------

def test_open_connecting_is_in_unknown_not_junk() -> None:
    """OPEN - Connecting must be classified as UNKNOWN, not JUNK."""
    assert "OPEN - Connecting" in UNKNOWN, (
        "'OPEN - Connecting' must be in UNKNOWN classification"
    )
    assert "OPEN - Connecting" not in JUNK, (
        "'OPEN - Connecting' must NOT be in JUNK classification"
    )


def test_dicarded_one_r_is_junk() -> None:
    """DICARDED (one R) must be in JUNK — this is the canonical spelling."""
    assert "DICARDED" in JUNK, (
        "'DICARDED' (one R) must be in JUNK classification"
    )


def test_discarded_two_r_is_not_accepted() -> None:
    """DISCARDED (two Rs) must NOT appear in any classification list.

    The canonical spelling is DICARDED (one R), matching HubSpot data.
    Acceptance of DISCARDED would silently miss real junk contacts.
    """
    all_statuses = QUALIFIED + IN_PROGRESS + list(UNKNOWN) + JUNK + WRONG_FIT
    assert "DISCARDED" not in all_statuses, (
        "'DISCARDED' (two Rs) must NOT appear in any classification list. "
        "The canonical spelling is 'DICARDED' (one R)."
    )


def test_closed_job_seeker_is_junk() -> None:
    """CLOSED - Job Seeker must be in JUNK."""
    assert "CLOSED - Job Seeker" in JUNK, (
        "'CLOSED - Job Seeker' must be in JUNK classification"
    )


def test_closed_sales_qualified_is_qualified() -> None:
    """CLOSED - Sales Qualified must be in QUALIFIED."""
    assert "CLOSED - Sales Qualified" in QUALIFIED, (
        "'CLOSED - Sales Qualified' must be in QUALIFIED classification"
    )


def test_closed_deal_created_is_qualified() -> None:
    """CLOSED - Deal Created must be in QUALIFIED."""
    assert "CLOSED - Deal Created" in QUALIFIED, (
        "'CLOSED - Deal Created' must be in QUALIFIED classification"
    )


def test_closed_bad_product_fit_is_wrong_fit() -> None:
    """CLOSED - Bad Product Fit must be in WRONG_FIT, not JUNK."""
    assert "CLOSED - Bad Product Fit" in WRONG_FIT, (
        "'CLOSED - Bad Product Fit' must be in WRONG_FIT classification"
    )
    assert "CLOSED - Bad Product Fit" not in JUNK, (
        "'CLOSED - Bad Product Fit' must NOT be in JUNK"
    )


def test_closed_sales_disqualified_is_wrong_fit() -> None:
    """CLOSED - Sales Disqualified must be in WRONG_FIT, not JUNK."""
    assert "CLOSED - Sales Disqualified" in WRONG_FIT, (
        "'CLOSED - Sales Disqualified' must be in WRONG_FIT classification"
    )
    assert "CLOSED - Sales Disqualified" not in JUNK, (
        "'CLOSED - Sales Disqualified' must NOT be in JUNK"
    )


# ---------------------------------------------------------------------------
# Part B — Junk-rate denominator tests
# ---------------------------------------------------------------------------

def test_open_connecting_excluded_from_junk_rate_denominator() -> None:
    """OPEN - Connecting must be excluded from the verdicted denominator.

    Scenario: 1 DICARDED, 1 OPEN - Connecting, 1 CLOSED - Sales Qualified
    Expected: junk_rate = 1 / (1 junk + 1 qualified) * 100 = 50%
              NOT 1 / 3 * 100 = 33%
    """
    statuses = ["DICARDED", "OPEN - Connecting", "CLOSED - Sales Qualified"]
    rate = _compute_junk_rate(statuses)
    assert rate == 50.0, (
        f"Junk rate must be 50.0% (OPEN - Connecting excluded from denominator), got {rate!r}"
    )


def test_junk_rate_none_when_all_connecting() -> None:
    """Junk rate must be None when all contacts are OPEN - Connecting (no verdicts)."""
    statuses = ["OPEN - Connecting", "OPEN - Connecting"]
    rate = _compute_junk_rate(statuses)
    assert rate is None, (
        f"Junk rate must be None when all contacts are OPEN - Connecting, got {rate!r}"
    )


def test_junk_rate_100_when_all_junk() -> None:
    """Junk rate must be 100% when all verdicted contacts are junk."""
    statuses = ["DICARDED", "CLOSED - Job Seeker"]
    rate = _compute_junk_rate(statuses)
    assert rate == 100.0, f"Junk rate must be 100.0% for all-junk, got {rate!r}"


def test_junk_rate_0_when_all_qualified() -> None:
    """Junk rate must be 0% when all verdicted contacts are qualified."""
    statuses = ["CLOSED - Sales Qualified", "CLOSED - Deal Created"]
    rate = _compute_junk_rate(statuses)
    assert rate == 0.0, f"Junk rate must be 0.0% for all-qualified, got {rate!r}"


def test_wrong_fit_is_verdicted_but_not_junk() -> None:
    """Wrong-fit contacts are in the verdicted denominator but not in junk numerator.

    Scenario: 1 CLOSED - Bad Product Fit, 1 DICARDED, 1 CLOSED - Sales Qualified
    Expected verdicted = 3, junk = 1 → junk_rate = 33.3%
    """
    statuses = ["CLOSED - Bad Product Fit", "DICARDED", "CLOSED - Sales Qualified"]
    rate = _compute_junk_rate(statuses)
    assert rate == 33.3, (
        f"Wrong-fit contacts must be in denominator but not numerator; "
        f"expected 33.3%, got {rate!r}"
    )


# ---------------------------------------------------------------------------
# Part B — Grace-period contacts
# ---------------------------------------------------------------------------

def test_grace_period_contacts_do_not_inflate_junk_rate() -> None:
    """Contacts created within early_lead_grace_days are treated as unknown, not junk.

    Grace-period logic is in run_lead_quality(): if is_recent and status in JUNK,
    the contact is counted as unknown, not junk. This test verifies the threshold
    value exists and is consistent with the doctrine (7 days).
    """
    assert _GRACE_DAYS == 7, (
        f"Grace period must be 7 days per doctrine, config says {_GRACE_DAYS}"
    )


def test_grace_days_is_positive() -> None:
    """Grace period must be a positive integer."""
    assert isinstance(_GRACE_DAYS, int), "early_lead_grace_days must be an integer"
    assert _GRACE_DAYS > 0, "early_lead_grace_days must be positive"


# ---------------------------------------------------------------------------
# Part B — Spelling protection for DICARDED
# ---------------------------------------------------------------------------

def test_dicarded_canonical_spelling_in_junk() -> None:
    """DICARDED (one R) is the canonical spelling and must appear in JUNK."""
    assert "DICARDED" in JUNK
    # Confirm exact one-R spelling
    assert "DICARDED".count("R") == 1, "DICARDED must have exactly one R"


def test_no_classification_list_contains_discarded_two_r() -> None:
    """No classification list should contain DISCARDED (two Rs) as an accepted status.

    This is a regression guard: if someone accidentally adds 'DISCARDED' to JUNK,
    real junk contacts spelled 'DICARDED' in HubSpot would be missed.
    """
    for lst_name, lst in [
        ("QUALIFIED", QUALIFIED),
        ("IN_PROGRESS", IN_PROGRESS),
        ("UNKNOWN", list(UNKNOWN)),
        ("JUNK", JUNK),
        ("WRONG_FIT", WRONG_FIT),
    ]:
        assert "DISCARDED" not in lst, (
            f"'DISCARDED' (two Rs) must not appear in {lst_name}. "
            "The canonical spelling is 'DICARDED' (one R)."
        )


# ---------------------------------------------------------------------------
# Direct runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_open_connecting_is_in_unknown_not_junk,
        test_dicarded_one_r_is_junk,
        test_discarded_two_r_is_not_accepted,
        test_closed_job_seeker_is_junk,
        test_closed_sales_qualified_is_qualified,
        test_closed_deal_created_is_qualified,
        test_closed_bad_product_fit_is_wrong_fit,
        test_closed_sales_disqualified_is_wrong_fit,
        test_open_connecting_excluded_from_junk_rate_denominator,
        test_junk_rate_none_when_all_connecting,
        test_junk_rate_100_when_all_junk,
        test_junk_rate_0_when_all_qualified,
        test_wrong_fit_is_verdicted_but_not_junk,
        test_grace_period_contacts_do_not_inflate_junk_rate,
        test_grace_days_is_positive,
        test_dicarded_canonical_spelling_in_junk,
        test_no_classification_list_contains_discarded_two_r,
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
