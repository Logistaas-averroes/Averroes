"""
tests/test_search_terms_pipeline_verifier.py

Unit tests for the Search Terms pipeline verifier verdict logic.
Tests pure logic only — no live database connection required.

PR-ADS-065 — Search Terms Pipeline Verification & Repair
"""

from __future__ import annotations

import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.verify_search_terms_pipeline import Verdict, compute_search_terms_verdict


class TestSearchTermsVerdict:
    """Test verdict computation for various pipeline states."""

    def test_ok_when_db_has_rows_and_api_has_rows(self):
        """Verdict is OK when live pull > 0, DB > 0, API > 0."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=45000,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=45000,
            live_pull_has_search_term=True,
            api_rows=100,
        )
        assert verdict == Verdict.OK
        assert "45000" in reason or "OK" in reason

    def test_ok_when_db_has_rows_no_api_check(self):
        """Verdict is OK when DB has rows and no API check is done."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=12000,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.OK

    def test_windsor_pull_empty(self):
        """Verdict is WINDSOR_PULL_EMPTY when live pull returns 0 and DB is 0."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=0,
            live_pull_has_search_term=None,
        )
        assert verdict == Verdict.WINDSOR_PULL_EMPTY

    def test_db_write_failed(self):
        """Verdict is DB_WRITE_FAILED when pull > 0 but DB = 0."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=45000,
            live_pull_has_search_term=True,
        )
        assert verdict == Verdict.DB_WRITE_FAILED

    def test_db_has_rows_api_empty(self):
        """Verdict is DB_HAS_ROWS_API_EMPTY when DB > 0 but API = 0."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=45000,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            api_rows=0,
        )
        assert verdict == Verdict.DB_HAS_ROWS_API_EMPTY

    def test_fresh_but_empty(self):
        """Verdict is FRESH_BUT_EMPTY when sync success but rows = 0."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.FRESH_BUT_EMPTY

    def test_missing_search_term_field(self):
        """Verdict is WINDSOR_PULL_MISSING_SEARCH_TERM_FIELD when sample keys lack search_term."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=45000,
            live_pull_has_search_term=False,
        )
        assert verdict == Verdict.WINDSOR_PULL_MISSING_SEARCH_TERM_FIELD

    def test_db_unavailable(self):
        """Verdict is DB_UNAVAILABLE when database is not accessible."""
        verdict, reason = compute_search_terms_verdict(
            db_available=False,
        )
        assert verdict == Verdict.DB_UNAVAILABLE

    def test_not_deployed(self):
        """Verdict is NOT_DEPLOYED when no run and no sync state."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=0,
            sync_status=None,
            latest_weekly_run=None,
        )
        assert verdict == Verdict.NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT

    def test_unknown_fallback(self):
        """Verdict is UNKNOWN when no clear signal."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_60d=0,
            sync_status="failed",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.UNKNOWN
