"""
tests/test_pr_ads_146b_coverage_gap.py

PR-ADS-146B — Keyword Coverage Gap Proof. Two merged defects fixed:

  §1  Finite-window coverage is proven by the UNION of successful
      keyword_facts sync-batch intervals, not MIN(source_date)/MAX(date_to). A
      failed month between two successful ones is reported as a gap.
  §2  run_keyword_bootstrap enforces the final release_recovery_lease() result —
      a failed release never reports bootstrap success.

Run with:
    python -m pytest tests/test_pr_ads_146b_coverage_gap.py -v
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.writers as writers  # noqa: E402
from services import keyword_sync_service as kss  # noqa: E402

# Reuse the fake lease store + helpers from the automation suite.
from tests.test_pr_ads_146a_keyword_automation import (  # noqa: E402
    FakeLeaseStore, _ok_sync, _patch_lease,
)


def _cov(intervals, ws, we):
    with patch.object(kss, "_keyword_success_intervals", return_value=intervals), \
         patch.object(kss, "_latest_successful_sync_date", return_value=None):
        return kss.selected_window_coverage(ws, we, is_all_time=False)


# ─────────────────────────────────────────────────────────────────────────────
# §1 — interval-union coverage
# ─────────────────────────────────────────────────────────────────────────────
def test_interval_merge_fuses_overlapping_and_adjacent():
    merged = kss._merge_intervals([
        (date(2026, 6, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 7, 13)),   # adjacent (gap = 1 day) → fuse
        (date(2026, 6, 15), date(2026, 7, 5)),   # overlapping
    ])
    assert merged == [(date(2026, 6, 1), date(2026, 7, 13))]


def test_february_gap_between_successful_months_is_missing():
    # Jan success, Feb FAILURE (absent from successful intervals), Mar–Jul success.
    intervals = [(date(2026, 1, 1), date(2026, 1, 31)),
                 (date(2026, 3, 1), date(2026, 7, 31))]
    cov = _cov(intervals, date(2026, 1, 1), date(2026, 7, 31))
    assert cov["selected_window_fully_synced"] is False
    assert cov["selected_window_coverage_status"] == "partial"
    assert cov["missing_window_ranges"] == [{"start": "2026-02-01", "end": "2026-02-28"}]
    assert cov["successful_coverage_ranges"] == [
        {"start": "2026-01-01", "end": "2026-01-31"},
        {"start": "2026-03-01", "end": "2026-07-31"},
    ]


def test_min_max_inference_no_longer_hides_the_gap():
    # MIN(source_date)=Jan and MAX(date_to)=Jul would (wrongly) imply full cover;
    # the interval union proves February is missing.
    intervals = [(date(2026, 1, 1), date(2026, 1, 31)),
                 (date(2026, 3, 1), date(2026, 7, 31))]
    with patch.object(kss, "_keyword_success_intervals", return_value=intervals), \
         patch.object(kss, "_durable_coverage", return_value=(date(2026, 1, 1), date(2026, 7, 31), 999)), \
         patch.object(kss, "_latest_successful_sync_date", return_value=date(2026, 7, 31)):
        cov = kss.selected_window_coverage(date(2026, 1, 1), date(2026, 7, 31), is_all_time=False)
    assert cov["selected_window_fully_synced"] is False
    assert cov["missing_window_ranges"] == [{"start": "2026-02-01", "end": "2026-02-28"}]


def test_successfully_synced_zero_row_prefix_counts_as_covered():
    # A batch that succeeded but returned zero rows still proves its dates synced.
    intervals = [(date(2026, 6, 1), date(2026, 7, 13))]   # success, zero rows inside
    cov = _cov(intervals, date(2026, 6, 1), date(2026, 7, 13))
    assert cov["selected_window_fully_synced"] is True
    assert cov["missing_window_ranges"] == []


def test_overlapping_monthly_and_rolling_intervals_cover_full_window():
    intervals = [(date(2026, 6, 1), date(2026, 6, 30)),    # monthly backfill
                 (date(2026, 6, 14), date(2026, 7, 13))]   # rolling daily overlap
    cov = _cov(intervals, date(2026, 6, 1), date(2026, 7, 13))
    assert cov["selected_window_fully_synced"] is True
    assert cov["missing_window_ranges"] == []
    assert cov["successful_coverage_ranges"] == [{"start": "2026-06-01", "end": "2026-07-13"}]


def test_partial_tail_gap_after_last_successful_batch():
    # Coverage stops 07-06; the window runs to 07-13 → the tail is missing.
    intervals = [(date(2026, 6, 14), date(2026, 7, 6))]
    cov = _cov(intervals, date(2026, 7, 1), date(2026, 7, 13))
    assert cov["selected_window_fully_synced"] is False
    assert cov["missing_window_ranges"] == [{"start": "2026-07-07", "end": "2026-07-13"}]


def test_no_successful_batches_is_not_synced():
    cov = _cov([], date(2026, 7, 7), date(2026, 7, 13))
    assert cov["selected_window_fully_synced"] is False
    assert cov["selected_window_coverage_status"] == "not_synced"


# ─────────────────────────────────────────────────────────────────────────────
# §2 — enforce final lease release
# ─────────────────────────────────────────────────────────────────────────────
def test_failed_final_release_cannot_report_success():
    store = FakeLeaseStore()

    def failing_release(*_a, **_k):
        return False   # final checkpoint could not persist

    with patch.multiple(writers,
                        get_latest_recovery_job=MagicMock(return_value=None),
                        acquire_recovery_lease=MagicMock(side_effect=store.acquire),
                        renew_recovery_lease=MagicMock(side_effect=store.renew),
                        release_recovery_lease=MagicMock(side_effect=failing_release)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=_ok_sync):
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "interrupted"
    assert out["reason"] == "final_checkpoint_failed"
    assert out["error"]


def test_successful_final_release_still_reports_success():
    store = FakeLeaseStore()
    with _patch_lease(store), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=_ok_sync):
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "success"
    assert store.released == "success"
