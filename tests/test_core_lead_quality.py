"""
tests/test_core_lead_quality.py

PR-ADS-067 — Regression tests for the HubSpot MQL classification constant that
`analysis/core.py` still exports.

PR-ADS-162H. This file used to protect five classification lists (QUALIFIED,
IN_PROGRESS, UNKNOWN, JUNK, WRONG_FIT) and a test-local copy of the junk-rate
formula in `run_lead_quality()`. That function wrote `outputs/lead_quality.json`
for the weekly / monthly advisor report and is deleted with the report; the
four lists only it used are deleted with it, and so are the cases about them
and about the copied formula (a formula no production code runs any more).

`QUALIFIED` survives because `services/revenue_attribution_service.py` imports
it, so every case about it is kept unchanged, and the removal of the rest is
asserted rather than assumed.

The live status taxonomy — which statuses are junk, wrong-fit, in progress or
unknown, including the one-R `DICARDED` spelling — is
`analysis/mql_status_taxonomy.py` and `db.writers._map_status_category`, with
their own suites.

Run with:
  python -m pytest tests/test_core_lead_quality.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analysis.core as core
from analysis.core import QUALIFIED


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


def test_qualified_holds_exactly_the_two_sales_qualified_statuses() -> None:
    """Nothing else is sales-qualified: not a junk status, not an open one, and
    not the two-R spelling HubSpot does not use."""
    assert sorted(QUALIFIED) == ["CLOSED - Deal Created", "CLOSED - Sales Qualified"]
    for status in ("DICARDED", "DISCARDED", "CLOSED - Job Seeker",
                   "CLOSED - Bad Product Fit", "CLOSED - Sales Disqualified",
                   "OPEN - Connecting", "OPEN - Meeting Booked"):
        assert status not in QUALIFIED, status


def test_the_report_only_classification_lists_are_gone() -> None:
    """They had one user, `run_lead_quality`, which is deleted. Left behind they
    would be a second, unread copy of the status taxonomy."""
    for name in ("IN_PROGRESS", "UNKNOWN", "JUNK", "WRONG_FIT",
                 "run_lead_quality", "run_campaign_truth", "determine_verdict"):
        assert not hasattr(core, name), name


def test_the_live_importer_of_qualified_still_resolves() -> None:
    from services import revenue_attribution_service as ras
    assert ras.QUALIFIED is QUALIFIED
