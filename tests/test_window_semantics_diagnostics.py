"""
tests/test_window_semantics_diagnostics.py

PR-ADS-095 — Window/Data Diagnostics

Tests for the pure window-diagnostics builder used by both the diagnostic
script (``scripts/diagnose_window_semantics.py``) and the
``/api/diagnostics/window-semantics`` endpoint.
"""

from services.freshness_service import CanonicalFreshnessStatus
from services.system_status_service import (
    build_window_diagnostics,
    diagnose_dataset_window,
)


# ── Per-dataset diagnostic verdicts ────────────────────────────────────────


def test_diagnose_failed_sync_with_rows_is_data_available_latest_sync_failed():
    verdict = diagnose_dataset_window(
        rows_by_window={"7d": 1200, "30d": 3900, "60d": 5707},
        latest_sync_status="failed",
        row_count_available=True,
        row_count_unavailable_reason=None,
    )
    assert verdict["diagnostic_status"] == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
    assert verdict["usable_for_page"] is True


def test_diagnose_failed_sync_no_rows_is_failed_no_data():
    verdict = diagnose_dataset_window(
        rows_by_window={"7d": 0, "30d": 0, "60d": 0},
        latest_sync_status="failed",
        row_count_available=True,
        row_count_unavailable_reason=None,
    )
    assert verdict["diagnostic_status"] == CanonicalFreshnessStatus.FAILED_NO_DATA
    assert verdict["usable_for_page"] is False


def test_diagnose_fresh_with_data():
    verdict = diagnose_dataset_window(
        rows_by_window={"7d": 700, "30d": 3000, "60d": 6000},
        latest_sync_status="success",
        row_count_available=True,
        row_count_unavailable_reason=None,
    )
    assert verdict["diagnostic_status"] == CanonicalFreshnessStatus.FRESH_WITH_DATA
    assert verdict["usable_for_page"] is True


def test_diagnose_row_count_not_enabled():
    verdict = diagnose_dataset_window(
        rows_by_window={},
        latest_sync_status="success",
        row_count_available=False,
        row_count_unavailable_reason=(
            "row-count query not enabled for this dataset (missing or non-identifier table)"
        ),
    )
    assert verdict["diagnostic_status"] == CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED
    assert verdict["usable_for_page"] is False


def test_diagnose_row_count_unavailable_after_query_failure():
    verdict = diagnose_dataset_window(
        rows_by_window={"7d": None, "30d": None},
        latest_sync_status="success",
        row_count_available=False,
        row_count_unavailable_reason="row-count query failed at execution time",
    )
    assert verdict["diagnostic_status"] == CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT
    assert verdict["usable_for_page"] is False


def test_diagnose_empty_when_all_windows_zero():
    verdict = diagnose_dataset_window(
        rows_by_window={"7d": 0, "30d": 0, "60d": 0},
        latest_sync_status="success",
        row_count_available=True,
        row_count_unavailable_reason=None,
    )
    assert verdict["diagnostic_status"] == CanonicalFreshnessStatus.FRESH_BUT_EMPTY


# ── Full assembly ──────────────────────────────────────────────────────────


def test_build_window_diagnostics_includes_all_window_counts():
    payload = build_window_diagnostics(
        windows=["7d", "30d", "60d"],
        dataset_diagnostics={
            "campaigns": {
                "source": "windsor",
                "table": "campaigns",
                "date_column": "run_date",
                "window_counts": {"7d": 1200, "30d": 3900, "60d": 5707},
                "row_count_available": True,
                "latest_sync_status": "failed",
                "latest_source_date": "2026-05-25",
                "missing_date_rows": 0,
                "last_successful_sync_at": "2026-05-25T07:04:00+00:00",
            },
        },
    )
    assert payload["windows"] == ["7d", "30d", "60d"]
    ds = payload["datasets"][0]
    assert ds["key"] == "campaigns"
    assert ds["window_counts"]["7d"] == 1200
    assert ds["window_counts"]["30d"] == 3900
    assert ds["window_counts"]["60d"] == 5707
    assert ds["latest_source_date"] == "2026-05-25"
    assert ds["missing_date_rows"] == 0
    assert ds["diagnostic_status"] == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
    assert ds["usable_for_page"] is True


def test_build_window_diagnostics_handles_db_unavailable():
    payload = build_window_diagnostics(
        windows=["7d", "30d"],
        dataset_diagnostics={
            "campaigns": {"db_unavailable": True},
        },
    )
    ds = payload["datasets"][0]
    assert ds["diagnostic_status"] == CanonicalFreshnessStatus.DB_UNAVAILABLE
    assert ds["usable_for_page"] is False
    assert "database" in ds["reason"].lower()


def test_build_window_diagnostics_marks_row_count_not_enabled():
    """When the dataset entry has no table/date_column, mark it not enabled."""
    payload = build_window_diagnostics(
        windows=["7d", "30d", "60d"],
        dataset_diagnostics={
            "deals": {
                "source": "hubspot",
                "table": None,
                "date_column": None,
                "window_counts": {},
                "row_count_available": False,
                "row_count_unavailable_reason": (
                    "row-count query not enabled for this dataset "
                    "(missing or non-identifier table/date_column)"
                ),
                "latest_sync_status": None,
            },
        },
    )
    ds = payload["datasets"][0]
    assert ds["diagnostic_status"] == CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED


def test_build_window_diagnostics_response_has_generated_at_and_windows():
    payload = build_window_diagnostics(
        windows=["7d"],
        dataset_diagnostics={},
    )
    assert "generated_at" in payload
    assert payload["windows"] == ["7d"]
    assert payload["datasets"] == []
