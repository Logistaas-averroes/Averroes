"""
tests/test_search_terms_pipeline_verifier.py

Unit tests for the Search Terms pipeline verifier verdict logic.
Tests pure logic only — no live database connection required.

PR-ADS-065 — Search Terms Pipeline Verification & Repair
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import verify_search_terms_pipeline as verifier
from scripts.verify_search_terms_pipeline import Verdict, compute_search_terms_verdict


class TestSearchTermsVerdict:
    """Test verdict computation for various pipeline states."""

    def test_ok_when_db_has_rows_and_api_has_rows(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=45000,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=45000,
            live_pull_has_search_term=True,
            api_rows=100,
        )
        assert verdict == Verdict.OK
        assert "45000" in reason or "OK" in reason

    def test_ok_when_db_has_rows_no_api_check(self):
        verdict, _ = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=12000,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.OK

    def test_windsor_pull_empty(self):
        verdict, _ = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=0,
            live_pull_has_search_term=None,
        )
        assert verdict == Verdict.WINDSOR_PULL_EMPTY

    def test_db_write_failed(self):
        verdict, _ = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=45000,
            live_pull_has_search_term=True,
        )
        assert verdict == Verdict.DB_WRITE_FAILED

    def test_db_has_rows_api_empty(self):
        verdict, _ = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=45000,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            api_rows=0,
        )
        assert verdict == Verdict.DB_HAS_ROWS_API_EMPTY

    def test_fresh_but_empty(self):
        verdict, _ = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.FRESH_BUT_EMPTY

    def test_missing_search_term_field(self):
        verdict, _ = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
            live_pull_rows=45000,
            live_pull_has_search_term=False,
        )
        assert verdict == Verdict.WINDSOR_PULL_MISSING_SEARCH_TERM_FIELD

    def test_db_unavailable(self):
        verdict, _ = compute_search_terms_verdict(db_available=False)
        assert verdict == Verdict.DB_UNAVAILABLE

    def test_not_deployed(self):
        verdict, _ = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status=None,
            latest_weekly_run=None,
        )
        assert verdict == Verdict.NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT

    def test_a_failed_canonical_sync_is_named_not_swallowed_as_unknown(self):
        """PR-ADS-156 §8: `sync_status="failed"` used to fall through to UNKNOWN.

        This exact input — the canonical batch recorded FAILED and no rows in
        the window — is the clearest signal the pipeline can produce, and the
        verdict for it was "Could not determine pipeline state from available
        evidence". The evidence was available; nothing read it.
        """
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status="failed",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.CANONICAL_SYNC_FAILED
        assert "NOT covered" in reason

    def test_unknown_fallback_is_kept_for_a_genuinely_unreadable_state(self):
        """The catch-all still exists — it is simply no longer the answer to a
        question the evidence could have answered."""
        verdict, _ = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=0,
            sync_status=None,
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.UNKNOWN

    def test_requested_window_used_in_reason(self):
        verdict, reason = compute_search_terms_verdict(
            db_available=True,
            db_rows_window=12,
            window_days=14,
            sync_status="success",
            latest_weekly_run="2026-05-25 07:00:00",
        )
        assert verdict == Verdict.OK
        assert "14-day window" in reason


def test_run_verification_respects_db_only(monkeypatch):
    monkeypatch.setattr(
        verifier,
        "_check_db",
        lambda days: {
            "available": True,
            "rows_60d": 99,
            "rows_requested_window": 7,
            "sync_status": "success",
            "latest_weekly_run": {"started_at": "2026-05-25 07:00:00"},
        },
    )
    monkeypatch.setattr(verifier, "_check_file", lambda: {"row_count": 0})
    pull_mock = Mock(return_value={"row_count": 100, "has_search_term_field": True})
    api_mock = Mock(return_value={"search_terms_rows": 1})
    monkeypatch.setattr(verifier, "_check_live_pull", pull_mock)
    monkeypatch.setattr(verifier, "_check_api", api_mock)

    result = verifier.run_verification(
        days=14,
        db_only=True,
        pull_live=True,
        api_url="https://example.com",
        admin_token="token",
    )

    assert result["modes"]["db_only"] is True
    assert result["modes"]["live_pull"] is False
    assert result["modes"]["api"] is False
    pull_mock.assert_not_called()
    api_mock.assert_not_called()
    assert result["verdict"] == Verdict.OK
    assert "14-day window" in result["reason"]


def test_check_db_uses_last_successful_sync_column(monkeypatch):
    executed_queries: list[str] = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed_queries.append(query)

        def fetchone(self):
            q = executed_queries[-1]
            if "FROM sync_state" in q:
                return ("success", "2026-05-25T07:00:00+00:00", None)
            if "FROM runs" in q or "FROM sync_batches" in q:
                return None
            return (0,)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_db_connection = types.SimpleNamespace(get_conn=lambda: FakeConn())
    monkeypatch.setitem(sys.modules, "db.connection", fake_db_connection)

    result = verifier._check_db(days=14)

    assert any("last_successful_sync_at" in q for q in executed_queries)
    assert result["sync_status"] == "success"
    assert result["sync_last_success"] == "2026-05-25T07:00:00+00:00"
