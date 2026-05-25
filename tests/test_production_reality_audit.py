"""
tests/test_production_reality_audit.py

Unit tests for the production reality audit helper functions.
Tests pure logic only — no live database connection required.

PR-ADS-064 — Full Production Reality Audit & UX Navigation Diagnosis
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

# Import the module under test.
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.audit_production_reality import (
    Verdict,
    compute_pipeline_blockers,
    compute_verdict,
)


# ─── compute_verdict tests ──────────────────────────────────────────────────


class TestComputeVerdict:
    """Test verdict logic for various dataset states."""

    def test_ok_when_rows_present_and_fresh(self):
        """Verdict is OK when rows exist and latest date is within threshold."""
        verdict, reason = compute_verdict(
            table="campaigns",
            rows_in_window=120,
            latest_date=date.today(),
            sync_status="success",
            days=60,
            stale_threshold=8,
        )
        assert verdict == Verdict.OK
        assert "present" in reason.lower()

    def test_ok_when_rows_present_and_no_sync_record(self):
        """Verdict is OK even without sync record if rows exist and are fresh."""
        verdict, reason = compute_verdict(
            table="campaigns",
            rows_in_window=50,
            latest_date=date.today() - timedelta(days=2),
            sync_status=None,
            days=30,
            stale_threshold=8,
        )
        assert verdict == Verdict.OK

    def test_fresh_but_empty_when_sync_success_but_zero_rows(self):
        """Verdict is FRESH_BUT_EMPTY when sync says success but table is empty."""
        verdict, reason = compute_verdict(
            table="search_terms",
            rows_in_window=0,
            latest_date=None,
            sync_status="success",
            days=60,
            stale_threshold=8,
        )
        assert verdict == Verdict.FRESH_BUT_EMPTY
        assert "zero rows" in reason.lower()

    def test_fresh_but_empty_when_fresh_status(self):
        """Verdict is FRESH_BUT_EMPTY when sync status is 'fresh' but no rows."""
        verdict, reason = compute_verdict(
            table="search_terms",
            rows_in_window=0,
            latest_date=None,
            sync_status="fresh",
            days=60,
        )
        assert verdict == Verdict.FRESH_BUT_EMPTY

    def test_stale_when_latest_date_is_old(self):
        """Verdict is STALE when latest date exceeds threshold."""
        old_date = date.today() - timedelta(days=15)
        verdict, reason = compute_verdict(
            table="campaigns",
            rows_in_window=100,
            latest_date=old_date,
            sync_status="success",
            days=60,
            stale_threshold=8,
        )
        assert verdict == Verdict.STALE
        assert "old" in reason.lower()

    def test_missing_table_verdict_not_computed_here(self):
        """MISSING_TABLE is not directly produced by compute_verdict —
        it is set by the caller when table doesn't exist. Test that zero rows
        with unknown sync gives EMPTY_VALID instead."""
        verdict, reason = compute_verdict(
            table="nonexistent",
            rows_in_window=0,
            latest_date=None,
            sync_status=None,
            days=60,
        )
        assert verdict == Verdict.EMPTY_VALID

    def test_broken_when_sync_failed_and_empty(self):
        """Verdict is BROKEN when sync failed and table is empty."""
        verdict, reason = compute_verdict(
            table="search_terms",
            rows_in_window=0,
            latest_date=None,
            sync_status="failed",
            days=60,
        )
        assert verdict == Verdict.BROKEN
        assert "failed" in reason.lower()

    def test_empty_valid_when_no_sync_and_no_rows(self):
        """Verdict is EMPTY_VALID when no sync record exists and table is empty."""
        verdict, reason = compute_verdict(
            table="gclid_attribution",
            rows_in_window=0,
            latest_date=None,
            sync_status="unknown",
            days=60,
        )
        assert verdict == Verdict.EMPTY_VALID


# ─── compute_pipeline_blockers tests ────────────────────────────────────────


class TestComputePipelineBlockers:
    """Test pipeline blocker detection logic."""

    def test_no_blockers_when_all_ok(self):
        """No blockers when all datasets are OK."""
        datasets = {
            "search_terms": {"verdict": Verdict.OK, "reason": "Data present"},
            "waste_terms": {"verdict": Verdict.OK, "reason": "Data present"},
        }
        blockers = compute_pipeline_blockers(datasets)
        assert blockers == []

    def test_search_terms_blocker_propagates_to_ngrams_and_waste(self):
        """When search_terms is FRESH_BUT_EMPTY, N-Grams and Waste Terms are blocked."""
        datasets = {
            "search_terms": {
                "verdict": Verdict.FRESH_BUT_EMPTY,
                "reason": "Sync says fresh but zero rows",
            },
            "waste_terms": {"verdict": Verdict.FRESH_BUT_EMPTY, "reason": "empty"},
        }
        blockers = compute_pipeline_blockers(datasets)

        # Should have blockers for Search Terms, N-Grams, and Waste Terms.
        pages_blocked = [b["page"] for b in blockers]
        assert "Search Terms" in pages_blocked
        assert "N-Grams" in pages_blocked
        assert "Waste Terms" in pages_blocked

    def test_search_terms_missing_propagates(self):
        """When search_terms table is missing, downstream pages are blocked."""
        datasets = {
            "search_terms": {
                "verdict": Verdict.MISSING_TABLE,
                "reason": "Table does not exist",
            },
            "waste_terms": {"verdict": Verdict.EMPTY_VALID, "reason": ""},
        }
        blockers = compute_pipeline_blockers(datasets)
        pages_blocked = [b["page"] for b in blockers]
        assert "N-Grams" in pages_blocked
        assert "Waste Terms" in pages_blocked

    def test_waste_terms_empty_with_search_terms_ok(self):
        """When search_terms is OK but waste_terms is FRESH_BUT_EMPTY,
        flag waste detection issue."""
        datasets = {
            "search_terms": {"verdict": Verdict.OK, "reason": "Data present"},
            "waste_terms": {
                "verdict": Verdict.FRESH_BUT_EMPTY,
                "reason": "Sync says fresh but empty",
            },
        }
        blockers = compute_pipeline_blockers(datasets)
        assert len(blockers) == 1
        assert "waste detection" in blockers[0]["blocker"].lower()

    def test_search_terms_stale_blocks_downstream(self):
        """When search_terms is STALE, downstream pages are blocked."""
        datasets = {
            "search_terms": {
                "verdict": Verdict.STALE,
                "reason": "Latest date is 15d old",
            },
            "waste_terms": {"verdict": Verdict.OK, "reason": ""},
        }
        blockers = compute_pipeline_blockers(datasets)
        pages_blocked = [b["page"] for b in blockers]
        assert "Search Terms" in pages_blocked
        assert "N-Grams" in pages_blocked
