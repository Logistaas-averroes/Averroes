from datetime import date

_DATE_KEYS = ("date", "source_date", "createdate")


def _parse_date_safe(value):
    """Parse a date-like value to datetime.date, or return None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10].strip())
    except (ValueError, TypeError):
        return None


def max_source_date(rows, fallback_date):
    """Return the maximum source date found in rows, or fallback_date."""
    dates = []
    for row in rows or []:
        props = row.get("properties") or {}
        value = None
        for key in _DATE_KEYS:
            value = row.get(key) or props.get(key)
            if value:
                break
        parsed = _parse_date_safe(value)
        if parsed:
            dates.append(parsed)
    return max(dates) if dates else fallback_date


def persistence_succeeded(expected_rows, written_count) -> bool:
    """Return True only when local DB persistence appears successful.

    Zero rows written is acceptable only when zero rows were fetched.
    If rows were fetched, the write count must be positive.
    A None write count is always treated as failure.
    """
    expected_count = len(expected_rows or [])
    if written_count is None:
        return False
    if expected_count == 0:
        return written_count == 0
    return written_count > 0
