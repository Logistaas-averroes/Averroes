"""
PR-ADS-107A — Business Window Resolver tests.

Validates the shared business-revenue window resolver:
  - current_quarter / last_quarter / last_6_months / ytd / all_time
  - human labels
  - timezone-aware UTC date math
  - is_closed_window flagging
  - all_time partial-data note
  - window bounds for filtering
  - invalid window handling
"""

import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.business_windows import (
    DEFAULT_WINDOW,
    WINDOW_KEYS,
    WINDOW_LABELS,
    get_window_bounds,
    is_valid_window,
    list_windows,
    resolve_window,
)

# Fixed reference date in Q2 (2026-06-20) for deterministic assertions.
NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── Keys / labels ───────────────────────────────────────────────────────────


def test_window_keys_exact_set_and_order():
    assert WINDOW_KEYS == [
        "current_quarter",
        "last_quarter",
        "last_6_months",
        "ytd",
        "all_time",
    ]


def test_human_labels_exact():
    assert WINDOW_LABELS == {
        "current_quarter": "Current Quarter",
        "last_quarter": "Last Quarter",
        "last_6_months": "Last 6 Months",
        "ytd": "YTD",
        "all_time": "All Time",
    }


def test_default_window_is_current_quarter():
    assert DEFAULT_WINDOW == "current_quarter"


def test_is_valid_window():
    assert is_valid_window("current_quarter")
    assert not is_valid_window("60d")
    assert not is_valid_window("")


# ── current_quarter ──────────────────────────────────────────────────────────


def test_current_quarter_dates():
    w = resolve_window("current_quarter", now=NOW)
    assert w["key"] == "current_quarter"
    assert w["label"] == "Current Quarter"
    assert w["start_date"] == "2026-04-01"  # Q2 starts April 1
    assert w["end_date"] == "2026-06-20"    # up to "today"
    assert w["is_closed_window"] is False
    assert w["notes"] is None


def test_current_quarter_q1_reference():
    w = resolve_window("current_quarter", now=datetime(2026, 2, 15, tzinfo=timezone.utc))
    assert w["start_date"] == "2026-01-01"
    assert w["end_date"] == "2026-02-15"


def test_current_quarter_q4_reference():
    w = resolve_window("current_quarter", now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert w["start_date"] == "2026-10-01"
    assert w["end_date"] == "2026-11-10"


# ── last_quarter ─────────────────────────────────────────────────────────────


def test_last_quarter_dates():
    w = resolve_window("last_quarter", now=NOW)
    assert w["start_date"] == "2026-01-01"  # Q1
    assert w["end_date"] == "2026-03-31"
    assert w["is_closed_window"] is True


def test_last_quarter_crosses_year_boundary():
    # In Q1, last quarter is Q4 of the previous year.
    w = resolve_window("last_quarter", now=datetime(2026, 2, 15, tzinfo=timezone.utc))
    assert w["start_date"] == "2025-10-01"
    assert w["end_date"] == "2025-12-31"
    assert w["is_closed_window"] is True


def test_last_quarter_q4_reference():
    w = resolve_window("last_quarter", now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert w["start_date"] == "2026-07-01"  # Q3
    assert w["end_date"] == "2026-09-30"


# ── last_6_months ────────────────────────────────────────────────────────────


def test_last_6_months_dates():
    w = resolve_window("last_6_months", now=NOW)
    assert w["start_date"] == "2025-12-20"  # six months before 2026-06-20
    assert w["end_date"] == "2026-06-20"
    assert w["is_closed_window"] is False


# ── ytd ──────────────────────────────────────────────────────────────────────


def test_ytd_dates():
    w = resolve_window("ytd", now=NOW)
    assert w["start_date"] == "2026-01-01"
    assert w["end_date"] == "2026-06-20"
    assert w["is_closed_window"] is False


# ── all_time ─────────────────────────────────────────────────────────────────


def test_all_time_has_no_lower_bound_and_a_note():
    w = resolve_window("all_time", now=NOW)
    assert w["start_date"] is None
    assert w["end_date"] == "2026-06-20"
    assert w["is_closed_window"] is False
    assert w["notes"]
    assert "incomplete" in w["notes"].lower()


# ── invalid ──────────────────────────────────────────────────────────────────


def test_invalid_window_raises_value_error():
    with pytest.raises(ValueError):
        resolve_window("60d", now=NOW)
    with pytest.raises(ValueError):
        resolve_window("", now=NOW)


# ── timezone handling ────────────────────────────────────────────────────────


def test_naive_datetime_is_treated_as_utc():
    naive = datetime(2026, 6, 20, 12, 0, 0)  # no tzinfo
    w = resolve_window("ytd", now=naive)
    assert w["start_date"] == "2026-01-01"
    assert w["end_date"] == "2026-06-20"


def test_resolve_defaults_to_now_when_unspecified():
    # Should not raise and should produce ISO date strings.
    w = resolve_window("current_quarter")
    assert isinstance(w["end_date"], str)
    date.fromisoformat(w["end_date"])  # parseable


# ── window bounds ────────────────────────────────────────────────────────────


def test_get_window_bounds_inclusive_start_exclusive_end():
    start_dt, end_dt = get_window_bounds("last_quarter", now=NOW)
    assert start_dt == datetime(2026, 1, 1, tzinfo=timezone.utc)
    # End is exclusive: the day after end_date (2026-03-31) -> 2026-04-01.
    assert end_dt == datetime(2026, 4, 1, tzinfo=timezone.utc)


def test_get_window_bounds_all_time_has_no_lower_bound():
    start_dt, end_dt = get_window_bounds("all_time", now=NOW)
    assert start_dt is None
    assert end_dt == datetime(2026, 6, 21, tzinfo=timezone.utc)


def test_window_bounds_are_timezone_aware():
    start_dt, end_dt = get_window_bounds("current_quarter", now=NOW)
    assert start_dt.tzinfo is not None
    assert end_dt.tzinfo is not None


# ── list_windows ─────────────────────────────────────────────────────────────


def test_list_windows_returns_all_in_order():
    windows = list_windows(now=NOW)
    assert [w["key"] for w in windows] == WINDOW_KEYS
    assert [w["label"] for w in windows] == [
        "Current Quarter",
        "Last Quarter",
        "Last 6 Months",
        "YTD",
        "All Time",
    ]
