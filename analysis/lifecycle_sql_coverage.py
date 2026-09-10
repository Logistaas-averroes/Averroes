"""
analysis/lifecycle_sql_coverage.py

PR-ADS-159 §6/§7 — global lifecycle-SQL gaps, per-window membership, and the
rule that decides whether a window's SQL total may be published.

The problem this replaces
-------------------------
The PR-ADS-158 audit reported the SAME 525 undated SQL contacts against all
eleven windows. That is not eleven findings; it is one finding printed eleven
times, and it makes every window look equally broken while telling an operator
nothing about which window is actually unpublishable.

Those 525 are a GLOBAL population: contacts whose lifecycle stage proves they
entered SQL, with no proven entry timestamp. Their membership in any particular
window is unknown — which is precisely why they cannot simply be added to it.

What can honestly be said per window
------------------------------------
One temporal fact is known about a contact with no SQL-entry date: when the
contact record was CREATED. That gives exactly one sound implication:

    a contact created AFTER a window ended cannot have entered SQL
    inside that window.

An SQL transition cannot precede the contact's own existence, so creation is a
lower bound on the event. That is enough to DISPROVE membership, and it is the
only thing creation is used for here. It is never the event date, never a
substitute for one, and never a reason to include a contact in a window.

Everything else stays unknown on purpose:

* creation BEFORE a window's end proves nothing — the contact could have entered
  SQL during that window, or years later, or not at all in it;
* a contact with no creation timestamp cannot be ruled out of ANY window;
* no monotonic-ordering inference is used. "It reached opportunity on date D, so
  it must have reached SQL before D" is a real implication in a portal that
  never skips stages and never back-fills, and this repository has no tested
  evidence that this portal is such a portal. Until it does, that inference is
  not available.

The five reported quantities
----------------------------
``global_missing_sql_entry_date``   the whole undated population, reported ONCE
``window_membership_unresolved``    undated contacts this window cannot rule out
``window_membership_proven_outside``undated contacts proven not to be in it
``window_membership_recovered``     contacts in the window on a recovered date
``window_total_complete``           True only when nothing is unresolved

Publication follows from the last one, and from nothing else.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

_ONE_DAY = timedelta(days=1)

#: Why a window's SQL total is or is not publishable, in stable machine codes.
COVERAGE_COMPLETE = "coverage_complete"
COVERAGE_UNRESOLVED_MEMBERSHIP = "unresolved_sql_entry_dates_may_belong"
COVERAGE_POPULATION_UNAVAILABLE = "sql_population_unavailable"


def _as_datetime(value):
    """Coerce a date/datetime/ISO string to an aware UTC datetime, else None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _window_end_exclusive(end):
    """The first instant AFTER the window. ``None`` means the window is open.

    A date-valued end is INCLUSIVE of that whole day, matching every window
    predicate in the repository (``< end + INTERVAL '1 day'``). Getting this
    boundary wrong by a day would rule a contact out of a window it might
    genuinely belong to — the one direction this module must never err in.
    """
    if end is None:
        return None
    if isinstance(end, datetime):
        return _as_datetime(end)
    ended = _as_datetime(end)
    return None if ended is None else ended + _ONE_DAY


def membership_verdict(created_at, window_end) -> str:
    """Can this undated contact be ruled OUT of the window? One implication only.

    ``"proven_outside"`` when the contact was created at or after the first
    instant past the window: it did not exist while the window was open, so it
    cannot have entered SQL inside it.

    ``"unresolved"`` otherwise — including when creation is unknown. An unknown
    creation time rules the contact out of nothing, and an open-ended window
    (All Time) can never rule anything out.
    """
    end = _window_end_exclusive(window_end)
    if end is None:
        return "unresolved"
    created = _as_datetime(created_at)
    if created is None:
        return "unresolved"
    return "proven_outside" if created >= end else "unresolved"


def window_membership(unresolved_rows, window_end) -> dict:
    """Split the global undated population against ONE window.

    ``unresolved_rows`` is ``[{"contact_id", "created_at"}, …]`` — every contact
    that reached SQL with no effective entry date. The same list is used for
    every window; what changes is how much of it can be ruled out.
    """
    rows = list(unresolved_rows or [])
    proven_outside = 0
    unresolved = 0
    unknown_creation = 0
    for row in rows:
        created = (row or {}).get("created_at")
        if created is None:
            unknown_creation += 1
        if membership_verdict(created, window_end) == "proven_outside":
            proven_outside += 1
        else:
            unresolved += 1
    return {
        "window_membership_unresolved": unresolved,
        "window_membership_proven_outside": proven_outside,
        # Reported separately because it is a different KIND of unknown: these
        # contacts can never be ruled out of any window by any amount of
        # window arithmetic, so they bound what this method can ever achieve.
        "unresolved_without_created_at": unknown_creation,
        "global_missing_sql_entry_date": len(rows),
    }


def window_coverage(*, window, window_end, confirmed_sqls, recovered_sqls,
                    unresolved_rows, population_available: bool = True) -> dict:
    """One window's SQL coverage verdict, and whether it may publish a total.

    ``confirmed_sqls`` is the count of contacts with a PROVEN effective
    SQL-entry date inside the window — the confirmed subset, which may always be
    published as such. ``recovered_sqls`` is how many of those came from
    recovered lifecycle history rather than the direct property.

    The complete total is publishable only when no undated contact could belong
    to this window. While any could, the complete total and the CPQL derived
    from it are unavailable — not zero, not the confirmed subset relabelled.
    """
    if not population_available:
        return {
            "window": window,
            "confirmed_sqls": confirmed_sqls,
            "window_membership_recovered": recovered_sqls,
            "global_missing_sql_entry_date": None,
            "window_membership_unresolved": None,
            "window_membership_proven_outside": None,
            "window_total_complete": False,
            "complete_sql_total": None,
            "cpql_publishable": False,
            "reason": COVERAGE_POPULATION_UNAVAILABLE,
            "explanation": (
                "the lifecycle-SQL population could not be read, so this "
                "window's completeness is unknown — not complete, and not zero"),
        }

    split = window_membership(unresolved_rows, window_end)
    unresolved = split["window_membership_unresolved"]
    complete = unresolved == 0
    return {
        "window": window,
        "confirmed_sqls": confirmed_sqls,
        "window_membership_recovered": recovered_sqls,
        **split,
        "window_total_complete": complete,
        # The ONLY circumstance in which a complete total exists: nothing
        # undated could belong here, so the confirmed subset IS the population.
        "complete_sql_total": confirmed_sqls if complete else None,
        "cpql_publishable": complete,
        "reason": COVERAGE_COMPLETE if complete else COVERAGE_UNRESOLVED_MEMBERSHIP,
        "explanation": _explain(complete, unresolved, split, window_end),
    }


def _explain(complete: bool, unresolved: int, split: dict, window_end) -> str:
    """Why completeness is or is not proven, for THIS window. Always stated."""
    if complete:
        proven = split["window_membership_proven_outside"]
        if not split["global_missing_sql_entry_date"]:
            return ("every lifecycle-SQL contact has a proven entry date, so "
                    "this window's population is complete")
        return (f"all {proven} undated lifecycle-SQL contact(s) were created "
                f"after this window ended, so none of them can have entered "
                f"SQL inside it")
    unknown = split["unresolved_without_created_at"]
    tail = ""
    if unknown:
        verb = "has" if unknown == 1 else "have"
        tail = (f" {unknown} of them {verb} no creation timestamp either, so "
                f"they cannot be ruled out of any window")
    if window_end is None:
        return (f"{unresolved} contact(s) reached SQL with no proven entry "
                f"date, and an open-ended window cannot rule any of them "
                f"out.{tail}")
    return (f"{unresolved} contact(s) reached SQL with no proven entry date and "
            f"existed before this window closed, so membership cannot be "
            f"disproven.{tail}")
