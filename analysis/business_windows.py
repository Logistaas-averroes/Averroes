"""
Business Window Resolver (PR-ADS-107A)

Shared, read-only helper that resolves business-revenue reporting windows to
concrete UTC date ranges. Revenue analysis for a long B2B sales cycle (SQLs,
customers, and deals do not close cleanly inside 7-day ad windows) needs
business windows, not ad-style 7d/14d/30d/60d windows.

Resolved window keys:
  - current_quarter
  - last_quarter
  - last_6_months
  - ytd
  - all_time

Each resolved window returns:
  - key
  - label
  - start_date          (ISO "YYYY-MM-DD", or None for all_time lower bound)
  - end_date            (ISO "YYYY-MM-DD")
  - is_closed_window    (True only when the window is a completed period)
  - notes               (used by all_time to flag partial-data limitations)

What is NOT in this file:
  - No external API calls.
  - No Google Ads / HubSpot reads or writes.
  - No revenue math.
  - No frontend formatting.

Date logic is timezone-aware UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from dateutil.relativedelta import relativedelta

# Ordered list of supported business-window keys.
WINDOW_KEYS = [
    "current_quarter",
    "last_quarter",
    "last_6_months",
    "ytd",
    "all_time",
]

# Human-readable labels exactly as surfaced on the ROAS pages.
WINDOW_LABELS = {
    "current_quarter": "Current Quarter",
    "last_quarter": "Last Quarter",
    "last_6_months": "Last 6 Months",
    "ytd": "YTD",
    "all_time": "All Time",
}

# Default business window used when none is supplied.
DEFAULT_WINDOW = "current_quarter"

# Note attached to all_time to avoid overclaiming completeness.
ALL_TIME_NOTE = (
    "All Time is bounded by the earliest synced data. Older periods may be "
    "incomplete depending on Google Ads and HubSpot sync history."
)


def _utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _coerce_utc(now: datetime | None) -> datetime:
    """Coerce an optional datetime to timezone-aware UTC."""
    if now is None:
        return _utcnow()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _quarter_start_month(month: int) -> int:
    """Return the first month of the calendar quarter containing ``month``.

    Jan-Mar -> 1, Apr-Jun -> 4, Jul-Sep -> 7, Oct-Dec -> 10.
    """
    return ((month - 1) // 3) * 3 + 1


def _iso(d: date | None) -> str | None:
    """Render a date as an ISO string, or None."""
    return d.isoformat() if d is not None else None


def is_valid_window(key: str) -> bool:
    """Return True if ``key`` is a supported business-window key."""
    return key in WINDOW_LABELS


def resolve_window(key: str, now: datetime | None = None) -> dict:
    """Resolve a business-window key to a concrete UTC date range.

    Args:
        key: One of WINDOW_KEYS.
        now: Optional reference time (timezone-aware or naive UTC). Defaults to
            the current UTC time. Exposed for deterministic testing.

    Returns:
        Dict with key, label, start_date, end_date, is_closed_window, notes.

    Raises:
        ValueError: If ``key`` is not a supported business window.
    """
    if not is_valid_window(key):
        raise ValueError(
            f"Invalid window '{key}'. Valid windows: {', '.join(WINDOW_KEYS)}"
        )

    now = _coerce_utc(now)
    today = now.date()
    year = today.year
    label = WINDOW_LABELS[key]

    start: date | None = None
    end: date = today
    is_closed = False
    notes: str | None = None

    if key == "current_quarter":
        start = date(year, _quarter_start_month(today.month), 1)
        end = today
        is_closed = False

    elif key == "last_quarter":
        current_quarter_start = date(year, _quarter_start_month(today.month), 1)
        last_quarter_end = current_quarter_start - relativedelta(days=1)
        last_quarter_start = date(
            last_quarter_end.year,
            _quarter_start_month(last_quarter_end.month),
            1,
        )
        start = last_quarter_start
        end = last_quarter_end
        # A prior quarter is always a completed period.
        is_closed = True

    elif key == "last_6_months":
        start = today - relativedelta(months=6)
        end = today
        is_closed = False

    elif key == "ytd":
        start = date(year, 1, 1)
        end = today
        is_closed = False

    elif key == "all_time":
        start = None
        end = today
        is_closed = False
        notes = ALL_TIME_NOTE

    return {
        "key": key,
        "label": label,
        "start_date": _iso(start),
        "end_date": _iso(end),
        "is_closed_window": is_closed,
        "notes": notes,
    }


def resolve_window_in_zone(key: str, time_zone: str | None,
                           now: datetime | None = None) -> dict:
    """Resolve a business window against the calendar day in ``time_zone``.

    PR-ADS-154C §"Window parity". Every consumer already called
    :func:`resolve_window`, so the *resolver* was shared — but the reference
    instant was not. Three anchors were in use for one set of window keys:

      * the dashboard services passed a UTC ``now``;
      * the spend and geo services anchored on the Google Ads ACCOUNT day, read
        from the database, silently falling back to UTC when no row carried one;
      * the evidence services anchored on ``analysis.account_time.ACCOUNT_TZ``,
        hardcoded to Europe/London.

    Sharing a resolver while disagreeing about "today" is not shared at all. At
    23:30 UTC on 30 June during British Summer Time the account day is already
    1 July, so ``current_quarter`` resolves to Q2 under one anchor and Q3 under
    another — two pages showing different quarters and both calling it "this
    quarter". Every named window diverges at that instant, not just the
    quarterly ones.

    The account day wins, because spend is denominated in it: Google Ads reports
    a day's cost against the account's local calendar, so a business window that
    disagrees with the account is asking about a range the spend data does not
    have. A ``None`` or unknown zone falls back to :data:`ACCOUNT_TZ` rather than
    to UTC — falling back to UTC is precisely what produced the divergence, and a
    missing database row is not a reason to change which day it is.

    Pure: no database access, no I/O. Callers holding a configured zone pass it;
    everything else gets the account default.
    """
    from analysis.account_time import ACCOUNT_TZ  # noqa: PLC0415

    requested = time_zone or ACCOUNT_TZ
    base = _coerce_utc(now)

    # PR-ADS-154C-F1: report the zone actually USED, not the one asked for.
    # Returning the requested string after falling back to the account default
    # told a reader the dates were computed in a zone they were not, which is the
    # same class of defect as the anchoring bug this resolver exists to fix — a
    # field that describes something other than what happened.
    effective = requested
    local_day = None
    for candidate in (requested, ACCOUNT_TZ):
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415
            local_day = base.astimezone(ZoneInfo(candidate)).date()
            effective = candidate
            break
        except Exception:  # noqa: BLE001 - unknown zone / tzdata unavailable
            continue
    if local_day is None:
        # No tz database at all. UTC is the only thing left, and saying so is
        # better than naming a zone that was never applied.
        local_day = base.date()
        effective = "UTC"

    # Anchor at midday so the delegated resolver, which coerces to UTC, cannot
    # shift the day back across the boundary it was just moved over.
    anchored = datetime(local_day.year, local_day.month, local_day.day,
                        12, 0, 0, tzinfo=timezone.utc)
    resolved = resolve_window(key, now=anchored)
    resolved["timezone"] = effective
    resolved["timezone_requested"] = requested
    return resolved


def get_window_bounds(
    key: str, now: datetime | None = None
) -> tuple[datetime | None, datetime]:
    """Return (start_dt, end_dt) UTC datetimes for filtering a resolved window.

    The lower bound (start_dt) is inclusive and may be None for ``all_time``
    (no lower bound). The upper bound (end_dt) is EXCLUSIVE and set to the start
    of the day after ``end_date`` so the full end day is included.

    Args:
        key: One of WINDOW_KEYS.
        now: Optional reference time.

    Returns:
        (start_dt, end_dt) tuple of timezone-aware UTC datetimes; start_dt may
        be None.
    """
    window = resolve_window(key, now=now)

    start_dt: datetime | None = None
    if window["start_date"]:
        s = date.fromisoformat(window["start_date"])
        start_dt = datetime(s.year, s.month, s.day, tzinfo=timezone.utc)

    e = date.fromisoformat(window["end_date"])
    end_dt = datetime(e.year, e.month, e.day, tzinfo=timezone.utc) + relativedelta(
        days=1
    )

    return start_dt, end_dt


def list_windows(now: datetime | None = None) -> list[dict]:
    """Resolve every supported business window (ordered by WINDOW_KEYS)."""
    return [resolve_window(k, now=now) for k in WINDOW_KEYS]
