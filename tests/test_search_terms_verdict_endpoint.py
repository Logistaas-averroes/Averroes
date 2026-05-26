"""
tests/test_search_terms_verdict_endpoint.py

Unit tests for the Search Terms Production Verdict endpoint helper logic.
Tests pure verdict-building logic — no live database connection required.

PR-ADS-066 — Search Terms Production Verdict Panel & Windsor Source-Parity Resolution
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

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

    def test_non_standard_days_verdict_ok(self):
        """compute_search_terms_verdict correctly handles a non-standard window (e.g., days=45)."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=3500,
            window_days=45,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.OK
        assert "3500" in reason or "OK" in reason

    def test_non_standard_days_verdict_empty(self):
        """Non-standard window with 0 rows and no sync = NOT_DEPLOYED."""
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            window_days=1,
            sync_status=None,
            latest_weekly_run=None,
        )
        assert verdict == Verdict.NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT


class TestBuildSearchTermsVerdictDB:
    """Test _build_search_terms_verdict DB query logic with mocked DB connection."""

    def _make_cursor(self, query_results: list):
        """Return a mock cursor that returns query_results in sequence."""
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.side_effect = query_results
        return cur

    def _make_conn(self, cursor):
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cursor)
        return conn

    def _patch_get_conn(self, conn):
        """Patch get_conn so _build_search_terms_verdict uses our mock."""
        mock_db_conn = MagicMock()
        mock_db_conn.get_conn = MagicMock(return_value=conn)
        return patch.dict("sys.modules", {"db.connection": mock_db_conn})

    def test_non_standard_days_queries_exact_window(self):
        """For days=1, _build_search_terms_verdict runs a COUNT(*) for the 1-day window."""
        # Query results in order:
        # rows_7d, rows_14d, rows_30d, rows_60d, rows_requested(1d),
        # MAX(source_date), blank rows, spend rows, click rows,
        # sync_state row (None), sync_batches row (None), runs row (None)
        query_results = [
            (150,),   # rows_7d
            (400,),   # rows_14d
            (800,),   # rows_30d
            (1200,),  # rows_60d
            (25,),    # rows_requested (days=1 — non-standard)
            ("2026-05-25",),  # MAX(source_date)
            (0,),     # blank_search_term_rows
            (100,),   # spend_rows
            (80,),    # click_rows
            None,     # sync_state (no row)
            None,     # sync_batches (no row)
            None,     # runs (no row)
        ]
        cursor = self._make_cursor(query_results)
        conn = self._make_conn(cursor)

        with self._patch_get_conn(conn):
            from api.server import _build_search_terms_verdict
            result = _build_search_terms_verdict(days=1)

        # The verdict should use the 1-day count (25 rows), not the 60d count (1200)
        assert result["db"]["rows_requested"] == 25
        # The api total_rows_in_window reflects the 1-day count
        assert result["api"]["total_rows_in_window"] == 25

    def test_windsor_mcp_sync_visible_in_verdict(self):
        """When windsor_mcp has the latest sync, the verdict endpoint surfaces it."""
        # Query results in order for days=60 (standard):
        # rows_7d, rows_14d, rows_30d, rows_60d (no rows_requested since days=60),
        # MAX(source_date), blank rows, spend rows, click rows,
        # sync_state row (windsor_mcp success), sync_batches row (windsor_mcp), runs row
        query_results = [
            (0,),    # rows_7d
            (0,),    # rows_14d
            (100,),  # rows_30d
            (250,),  # rows_60d
            ("2026-05-10",),     # MAX(source_date)
            (0,),                # blank_search_term_rows
            (50,),               # spend_rows
            (40,),               # click_rows
            ("windsor_mcp", "success", "2026-05-10 08:00:00"),  # sync_state row
            ("windsor_mcp", "success", 250, "2026-05-10 08:00:00"),  # sync_batches row
            None,                # runs row (no weekly run)
        ]
        cursor = self._make_cursor(query_results)
        conn = self._make_conn(cursor)

        with self._patch_get_conn(conn):
            from api.server import _build_search_terms_verdict
            result = _build_search_terms_verdict(days=60)

        assert result["sync"]["sync_source"] == "windsor_mcp"
        assert result["sync"]["sync_state_status"] == "success"
        assert result["sync"]["latest_batch_source"] == "windsor_mcp"
        assert result["sync"]["latest_batch_row_count"] == 250
