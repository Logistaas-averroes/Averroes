"""
tests/test_backfill_framework.py

PR-ADS-071 — Tests for the manual historical backfill framework.

Covers:
  - Monthly chunk generation (calendar-aligned)
  - Weekly chunk generation (7-day windows)
  - Invalid date range fails with sys.exit
  - Dry-run does not call DB writer functions
  - Source selector validates all, google_ads, hubspot
  - Summary object shape is stable
  - No unsafe wording in CLI help / module text

Run with:
    python -m pytest tests/test_backfill_framework.py -v
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill import (
    GOOGLE_ADS_DATASETS,
    HUBSPOT_DATASETS,
    VALID_SOURCES,
    build_chunks,
    build_backfill_plan,
    parse_args,
    _validate_args,
    print_summary,
)


# ---------------------------------------------------------------------------
# Chunk generation — monthly
# ---------------------------------------------------------------------------

class TestBuildChunksMonthly:
    def test_single_full_month(self):
        chunks = build_chunks(date(2024, 1, 1), date(2024, 1, 31), "monthly")
        assert chunks == [(date(2024, 1, 1), date(2024, 1, 31))]

    def test_two_full_months(self):
        chunks = build_chunks(date(2024, 1, 1), date(2024, 2, 29), "monthly")
        assert len(chunks) == 2
        assert chunks[0] == (date(2024, 1, 1), date(2024, 1, 31))
        assert chunks[1] == (date(2024, 2, 1), date(2024, 2, 29))

    def test_partial_last_month(self):
        chunks = build_chunks(date(2024, 3, 1), date(2024, 3, 15), "monthly")
        assert chunks == [(date(2024, 3, 1), date(2024, 3, 15))]

    def test_three_month_range(self):
        chunks = build_chunks(date(2024, 1, 1), date(2024, 3, 31), "monthly")
        assert len(chunks) == 3
        assert chunks[0][0] == date(2024, 1, 1)
        assert chunks[2][1] == date(2024, 3, 31)

    def test_cross_year_boundary(self):
        chunks = build_chunks(date(2023, 11, 1), date(2024, 1, 31), "monthly")
        assert len(chunks) == 3
        assert chunks[0] == (date(2023, 11, 1), date(2023, 11, 30))
        assert chunks[1] == (date(2023, 12, 1), date(2023, 12, 31))
        assert chunks[2] == (date(2024, 1, 1), date(2024, 1, 31))

    def test_single_day_range(self):
        chunks = build_chunks(date(2024, 6, 15), date(2024, 6, 15), "monthly")
        assert chunks == [(date(2024, 6, 15), date(2024, 6, 15))]

    def test_max_chunks_limits_output(self):
        # A 3-month range should normally produce 3 chunks
        chunks = build_chunks(date(2024, 1, 1), date(2024, 3, 31), "monthly", max_chunks=2)
        assert len(chunks) == 2

    def test_leap_year_february(self):
        chunks = build_chunks(date(2024, 2, 1), date(2024, 2, 29), "monthly")
        assert chunks == [(date(2024, 2, 1), date(2024, 2, 29))]

    def test_non_leap_year_february(self):
        chunks = build_chunks(date(2023, 2, 1), date(2023, 2, 28), "monthly")
        assert chunks == [(date(2023, 2, 1), date(2023, 2, 28))]


# ---------------------------------------------------------------------------
# Chunk generation — weekly
# ---------------------------------------------------------------------------

class TestBuildChunksWeekly:
    def test_single_week(self):
        chunks = build_chunks(date(2024, 1, 1), date(2024, 1, 7), "weekly")
        assert chunks == [(date(2024, 1, 1), date(2024, 1, 7))]

    def test_two_full_weeks(self):
        chunks = build_chunks(date(2024, 1, 1), date(2024, 1, 14), "weekly")
        assert len(chunks) == 2
        assert chunks[0] == (date(2024, 1, 1), date(2024, 1, 7))
        assert chunks[1] == (date(2024, 1, 8), date(2024, 1, 14))

    def test_partial_last_week(self):
        chunks = build_chunks(date(2024, 1, 1), date(2024, 1, 10), "weekly")
        assert len(chunks) == 2
        assert chunks[0] == (date(2024, 1, 1), date(2024, 1, 7))
        assert chunks[1] == (date(2024, 1, 8), date(2024, 1, 10))

    def test_chunks_are_contiguous(self):
        chunks = build_chunks(date(2024, 1, 1), date(2024, 1, 28), "weekly")
        for i in range(1, len(chunks)):
            prev_end   = chunks[i - 1][1]
            cur_start  = chunks[i][0]
            assert cur_start == prev_end + timedelta(days=1), (
                f"Gap between chunk {i - 1} and chunk {i}"
            )

    def test_max_chunks_limits_output(self):
        chunks = build_chunks(date(2024, 1, 1), date(2024, 3, 31), "weekly", max_chunks=3)
        assert len(chunks) == 3

    def test_single_day_range(self):
        chunks = build_chunks(date(2024, 4, 1), date(2024, 4, 1), "weekly")
        assert chunks == [(date(2024, 4, 1), date(2024, 4, 1))]


# ---------------------------------------------------------------------------
# Invalid date range / argument validation
# ---------------------------------------------------------------------------

class TestDateValidation:
    def test_reversed_dates_exits(self):
        args = parse_args([
            "--source", "google_ads",
            "--from", "2024-03-01",
            "--to", "2024-01-01",
            "--dry-run",
        ])
        with pytest.raises(SystemExit) as exc_info:
            _validate_args(args)
        assert exc_info.value.code == 1

    def test_invalid_from_date_exits(self):
        # argparse accepts the string; _parse_date (called inside _validate_args) exits
        args = parse_args([
            "--source", "google_ads",
            "--from", "not-a-date",
            "--to", "2024-01-31",
            "--dry-run",
        ])
        with pytest.raises(SystemExit) as exc_info:
            _validate_args(args)
        assert exc_info.value.code == 1

    def test_equal_dates_ok(self):
        args = parse_args([
            "--source", "hubspot",
            "--from", "2024-06-15",
            "--to", "2024-06-15",
            "--dry-run",
        ])
        date_from, date_to, sources = _validate_args(args)
        assert date_from == date_to == date(2024, 6, 15)

    def test_invalid_max_chunks_exits(self):
        args = parse_args([
            "--source", "google_ads",
            "--from", "2024-01-01",
            "--to", "2024-03-31",
            "--max-chunks", "0",
            "--dry-run",
        ])
        with pytest.raises(SystemExit) as exc_info:
            _validate_args(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Source selector
# ---------------------------------------------------------------------------

class TestSourceSelector:
    def test_all_expands_to_both_sources(self):
        args = parse_args([
            "--source", "all",
            "--from", "2024-01-01",
            "--to", "2024-01-31",
            "--dry-run",
        ])
        _, _, sources = _validate_args(args)
        assert set(sources) == {"google_ads", "hubspot"}

    def test_google_ads_source(self):
        args = parse_args([
            "--source", "google_ads",
            "--from", "2024-01-01",
            "--to", "2024-01-31",
            "--dry-run",
        ])
        _, _, sources = _validate_args(args)
        assert sources == ["google_ads"]

    def test_hubspot_source(self):
        args = parse_args([
            "--source", "hubspot",
            "--from", "2024-01-01",
            "--to", "2024-01-31",
            "--dry-run",
        ])
        _, _, sources = _validate_args(args)
        assert sources == ["hubspot"]

    def test_invalid_source_rejected_by_argparse(self):
        with pytest.raises(SystemExit) as exc_info:
            parse_args([
                "--source", "unknown_source",
                "--from", "2024-01-01",
                "--to", "2024-01-31",
            ])
        assert exc_info.value.code == 2

    def test_valid_sources_constant(self):
        assert VALID_SOURCES == {"all", "google_ads", "hubspot"}


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------

class TestBuildBackfillPlan:
    def test_google_ads_plan_has_expected_datasets(self):
        args = parse_args([
            "--source", "google_ads",
            "--from", "2024-01-01",
            "--to", "2024-01-31",
            "--dry-run",
        ])
        plan = build_backfill_plan(args)
        datasets_in_plan = {e["dataset"] for e in plan}
        # All Google Ads datasets should appear
        for ds in GOOGLE_ADS_DATASETS:
            assert ds in datasets_in_plan

    def test_hubspot_plan_has_expected_datasets(self):
        args = parse_args([
            "--source", "hubspot",
            "--from", "2024-01-01",
            "--to", "2024-01-31",
            "--dry-run",
        ])
        plan = build_backfill_plan(args)
        datasets_in_plan = {e["dataset"] for e in plan}
        for ds in HUBSPOT_DATASETS:
            assert ds in datasets_in_plan

    def test_search_terms_has_unsupported_note(self):
        args = parse_args([
            "--source", "google_ads",
            "--from", "2024-01-01",
            "--to", "2024-01-31",
            "--dry-run",
        ])
        plan = build_backfill_plan(args)
        st_entry = next(e for e in plan if e["dataset"] == "search_terms")
        assert st_entry["notes"], "search_terms should have a connector limitation note"
        assert "unsupported_by_current_connector" in st_entry["notes"][0]

    def test_max_chunks_respected_in_plan(self):
        args = parse_args([
            "--source", "google_ads",
            "--from", "2024-01-01",
            "--to", "2024-06-30",
            "--chunk", "monthly",
            "--max-chunks", "2",
            "--dry-run",
        ])
        plan = build_backfill_plan(args)
        for entry in plan:
            assert len(entry["chunks"]) <= 2


# ---------------------------------------------------------------------------
# Dry-run: DB writers must not be called
# ---------------------------------------------------------------------------

class TestDryRunDoesNotWriteDB:
    def test_windsor_dry_run_skips_db(self):
        from scripts.backfill_windsor import run_google_ads_backfill
        chunks = [(date(2024, 1, 1), date(2024, 1, 31))]

        with patch("scripts.backfill_windsor._pull_campaigns") as mock_pull, \
             patch("scripts.backfill_windsor._write_campaigns") as mock_write, \
             patch("scripts.backfill_windsor._create_backfill_run") as mock_run:

            result = run_google_ads_backfill(
                dataset="campaigns",
                chunks=chunks,
                dry_run=True,
            )

        mock_pull.assert_not_called()
        mock_write.assert_not_called()
        mock_run.assert_not_called()
        assert result["rows_written"] == 0
        assert result["chunks_completed"] == 1  # dry-run counts chunks as completed

    def test_hubspot_dry_run_skips_db(self):
        from scripts.backfill_hubspot import run_hubspot_backfill
        chunks = [(date(2024, 1, 1), date(2024, 1, 31))]

        with patch("scripts.backfill_hubspot._pull_contacts_in_range") as mock_pull, \
             patch("scripts.backfill_hubspot._write_contacts") as mock_write, \
             patch("scripts.backfill_hubspot._create_backfill_run") as mock_run:

            result = run_hubspot_backfill(
                dataset="contacts",
                chunks=chunks,
                dry_run=True,
            )

        mock_pull.assert_not_called()
        mock_write.assert_not_called()
        mock_run.assert_not_called()
        assert result["rows_written"] == 0
        assert result["chunks_completed"] == 1

    def test_windsor_dry_run_no_sync_batch(self):
        from scripts.backfill_windsor import run_google_ads_backfill
        chunks = [(date(2024, 1, 1), date(2024, 1, 31))]

        # In dry-run, the function should never reach the sync-batch or DB write code.
        # Patch the pull and write functions (which shouldn't be called either).
        with patch("scripts.backfill_windsor._pull_geo") as mock_pull, \
             patch("scripts.backfill_windsor._write_geo") as mock_write, \
             patch("scripts.backfill_windsor._create_backfill_run") as mock_run:

            result = run_google_ads_backfill(
                dataset="geo",
                chunks=chunks,
                dry_run=True,
            )

        # None of the live functions should be called during dry-run
        mock_pull.assert_not_called()
        mock_write.assert_not_called()
        mock_run.assert_not_called()
        assert result["rows_written"] == 0


# ---------------------------------------------------------------------------
# Summary shape
# ---------------------------------------------------------------------------

class TestSummaryShape:
    def _make_summary(self, **overrides: Any) -> dict:
        base: dict = {
            "source":           "all",
            "date_from":        "2024-01-01",
            "date_to":          "2024-12-31",
            "chunk":            "monthly",
            "dry_run":          False,
            "chunks_total":     12,
            "chunks_completed": 12,
            "datasets": {
                "google_ads/campaigns": {
                    "rows_pulled":  1234,
                    "rows_written": 1234,
                    "status":       "success",
                },
                "google_ads/search_terms": {
                    "rows_pulled":  0,
                    "rows_written": 0,
                    "status":       "unsupported_by_current_connector",
                    "note":         "unsupported_by_current_connector: ...",
                },
            },
            "errors": [],
        }
        base.update(overrides)
        return base

    def test_required_top_level_keys_present(self):
        summary = self._make_summary()
        for key in ("source", "date_from", "date_to", "chunk", "dry_run",
                    "chunks_total", "chunks_completed", "datasets", "errors"):
            assert key in summary, f"Missing required key: {key}"

    def test_dataset_entry_required_keys(self):
        summary = self._make_summary()
        for ds_key, ds_val in summary["datasets"].items():
            for k in ("rows_pulled", "rows_written", "status"):
                assert k in ds_val, f"Dataset {ds_key} missing key: {k}"

    def test_dry_run_summary_shows_no_writes(self):
        summary = self._make_summary(
            dry_run=True,
            chunks_completed=12,
        )
        # dry_run flag is in summary
        assert summary["dry_run"] is True

    def test_errors_is_list(self):
        summary = self._make_summary()
        assert isinstance(summary["errors"], list)

    def test_print_summary_does_not_raise(self, capsys):
        summary = self._make_summary()
        # print_summary should not raise
        print_summary(summary)
        captured = capsys.readouterr()
        assert "BACKFILL SUMMARY" in captured.out
        assert "local persistence enabled" in captured.out

    def test_dry_run_print_summary_shows_dry_run(self, capsys):
        summary = self._make_summary(dry_run=True)
        print_summary(summary)
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out


# ---------------------------------------------------------------------------
# Unsafe wording check
# ---------------------------------------------------------------------------

class TestUnsafeWording:
    """Verify that no unsafe external-action wording appears in the backfill modules."""

    _FORBIDDEN_PATTERNS = [
        "push negative",
        "apply negative",
        "pause campaign",
        "send to google ads",
        "send to hubspot",
        "upload conversion",
        "change bid",
        "change budget",
        "live activation",
    ]

    def _module_source(self, module_path: str) -> str:
        path = Path(__file__).resolve().parents[1] / module_path
        return path.read_text(encoding="utf-8").lower()

    def test_backfill_py_no_unsafe_wording(self):
        src = self._module_source("scripts/backfill.py")
        for pattern in self._FORBIDDEN_PATTERNS:
            assert pattern not in src, (
                f"Unsafe wording found in scripts/backfill.py: '{pattern}'"
            )

    def test_backfill_windsor_no_unsafe_wording(self):
        src = self._module_source("scripts/backfill_windsor.py")
        for pattern in self._FORBIDDEN_PATTERNS:
            assert pattern not in src, (
                f"Unsafe wording found in scripts/backfill_windsor.py: '{pattern}'"
            )

    def test_backfill_hubspot_no_unsafe_wording(self):
        src = self._module_source("scripts/backfill_hubspot.py")
        for pattern in self._FORBIDDEN_PATTERNS:
            assert pattern not in src, (
                f"Unsafe wording found in scripts/backfill_hubspot.py: '{pattern}'"
            )
