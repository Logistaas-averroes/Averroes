"""
tests/test_search_terms_verdict_endpoint.py

Unit tests for the Search Terms Production Verdict endpoint helper logic.
Tests pure verdict-building logic — no live database connection required.

PR-ADS-066 — Search Terms Production Verdict Panel & Windsor Source-Parity Resolution
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.verify_search_terms_pipeline import Verdict, compute_search_terms_verdict


class TestSearchTermsVerdictEndpoint:
    """Test verdict computation for the /api/system/search-terms-verdict endpoint."""

    def test_ok_when_db_rows_and_api_rows(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=45292,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            api_rows=100,
        )
        assert verdict == Verdict.OK
        assert "OK" in reason or "45292" in reason

    def test_windsor_pull_empty_when_live_pull_0_and_db_0(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status=None,
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=0,
        )
        assert verdict == Verdict.WINDSOR_PULL_EMPTY
        assert "0 rows" in reason.lower() or "empty" in reason.lower()

    def test_db_write_failed_when_live_pull_rows_but_db_0(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status=None,
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=45000,
            live_pull_has_search_term=True,
        )
        assert verdict == Verdict.DB_WRITE_FAILED
        assert "45000" in reason

    def test_db_has_rows_api_empty(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=45000,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            api_rows=0,
        )
        assert verdict == Verdict.DB_HAS_ROWS_API_EMPTY
        assert "API" in reason

    def test_fresh_but_empty_when_sync_success_and_rows_0(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.FRESH_BUT_EMPTY
        assert "success" in reason.lower() or "0 rows" in reason.lower()

    def test_not_deployed_when_no_run_and_no_sync(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status=None,
            latest_weekly_run=None,
        )
        assert verdict == Verdict.NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT
        assert "not have run" in reason.lower() or "no weekly" in reason.lower()

    def test_db_unavailable(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=False,
            db_rows_window=0,
        )
        assert verdict == Verdict.DB_UNAVAILABLE
        assert "unavailable" in reason.lower()

    def test_ok_when_db_rows_no_api_check(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=12000,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.OK

    def test_windsor_pull_missing_search_term_field(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status=None,
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=5000,
            live_pull_has_search_term=False,
        )
        assert verdict == Verdict.WINDSOR_PULL_MISSING_SEARCH_TERM_FIELD

    def test_unknown_fallback(self):
        """Scenario where no verdict matches triggers UNKNOWN."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status="running",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.UNKNOWN
