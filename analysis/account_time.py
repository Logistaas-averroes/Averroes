"""
analysis/account_time.py

PR-ADS-152 — the single account-timezone "today" resolver, shared so that the
canonical Evidence Window reconciliation resolves its boundary date in exactly the
same way as Campaign, Keyword and Search-Term Evidence (Google Ads account local
day, Europe/London), instead of an implicit UTC ``now.date()``.

Pure and dependency-free. British Summer Time is handled by ``zoneinfo`` (the
account day flips at 23:00 UTC in summer, 00:00 UTC in winter).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

# The Google Ads account local timezone (confirmed Europe/London).
ACCOUNT_TZ = "Europe/London"


def account_today(now: datetime | None = None) -> date:
    """The current calendar date in the Google Ads account timezone.

    ``now`` may be tz-aware or naive UTC; None means the real current UTC time.
    Falls back to the UTC date only if the tz database is unavailable.
    """
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        tz = ZoneInfo(ACCOUNT_TZ)
    except Exception:  # noqa: BLE001 - zoneinfo/tzdata unavailable
        tz = timezone.utc
    base = now or datetime.now(tz=timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(tz).date()
