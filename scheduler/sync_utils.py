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


# ── Run outcome (PR-ADS-162H) ────────────────────────────────────────────────
#
# The weekly and monthly jobs used to end by generating a report, and a run that
# reached that step was recorded `success` whatever its data steps had done: a
# write that persisted nothing, or a search-term sync that failed, was logged
# and forgotten. With the report gone the data steps ARE the job, so the run
# record states what they did.
#
#   success   every kept step reported landing
#   partial   the run reached its end, its run row exists, and at least one
#             kept step did not land
#   failed    the run could not continue (recorded by the job where it stops),
#             OR it reached its end without a run row: with no durable record
#             of the run there is nothing to call partial. The usual cause is
#             an unreachable database, in which case nothing landed at all.
#
# `partial` is the third outcome everywhere else in this codebase and means the
# same thing here: real work landed, and the run did not finish what it set out
# to do. Monitoring does not advance `last_success_at` on it.
#
# "Reported landing" is the limit of what this knows. A step that swallows its
# own error and returns normally still reads as landed — see docs/09,
# PR-ADS-162H, "Known gaps".

#: The step whose failure means the run itself has no durable record.
RUN_ROW_STEP = "write_run"
def note_step_failure(failures: list, step: str, detail) -> None:
    """Record that one kept step did not land. ``detail`` is kept short: it is
    stored on the run record, not logged in place of the step's own error."""
    failures.append({"step": step, "detail": str(detail)[:300]})


def run_outcome(failures: list) -> tuple:
    """``(status, failed_step, error_message)`` for a run that reached its end.

    An empty list is the only way to ``success``. A run that could not create
    its run row is ``failed``, never ``partial``: partial claims real work
    landed, and a run with no durable row cannot show that it did. The step
    names are joined in the order they failed, so the run record reads in the
    order the job ran.
    """
    if not failures:
        return "success", None, None
    steps = "; ".join(f["step"] for f in failures)
    message = "; ".join(f"{f['step']}: {f['detail']}" for f in failures)
    status = "failed" if any(f["step"] == RUN_ROW_STEP for f in failures) \
        else "partial"
    return status, steps, message[:2000]
