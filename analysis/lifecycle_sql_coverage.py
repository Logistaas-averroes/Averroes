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


def membership_verdict(created_at, window_end, known_reached_sql_by=None,
                       window_start=None) -> str:
    """Can this undated contact be ruled OUT of the window? Two implications.

    Both are one-directional. Each can only ever DISPROVE membership, and
    neither can ever confirm it or supply a date.

    **The creation LOWER bound** (PR-ADS-159). ``"proven_outside"`` when the
    contact was created at or after the first instant past the window: it did
    not exist while the window was open, so it cannot have entered SQL inside
    it.

    **The boundary UPPER bound** (PR-ADS-160). ``known_reached_sql_by`` is an
    observation instant at which the contact had ALREADY reached SQL. If that
    instant is STRICTLY before the window's start, the SQL transition happened
    before the window opened, so it cannot belong to this window either. This is
    the rule that lets windows opening after the boundary stop carrying the 533
    historical unknowns.

    The comparison is strict on purpose. At exact equality — a bound landing on
    the window's first instant — the transition could have occurred AT that
    instant, and a window start is inclusive. Ruling the contact out there would
    require interval semantics finer than this system has proven, so equality
    stays unresolved. Erring the other way would silently drop a contact from a
    window it might genuinely belong to, which is the one direction this module
    must never err in.

    Everything else is ``"unresolved"``, including:

    * unknown creation time — rules the contact out of nothing;
    * no boundary evidence — the same;
    * a window that OVERLAPS the boundary, where the unknown event could fall on
      either side of it;
    * an open-ended window (All Time), which can never rule anything out.

    Note what this function still refuses to do. It never returns a date, and
    ``known_reached_sql_by`` is never compared against the window END to place
    the contact INSIDE a window. "It had reached SQL by B, and B is inside this
    window" says nothing about whether the transition happened in this window or
    any earlier one.
    """
    end = _window_end_exclusive(window_end)
    created = _as_datetime(created_at)
    if end is not None and created is not None and created >= end:
        return "proven_outside"

    # The boundary bound needs a window START to compare against. An open-ended
    # window (All Time, or any window with no lower bound) has none, and nothing
    # can be ruled out of it.
    start = _as_datetime(window_start)
    bound = _as_datetime(known_reached_sql_by)
    if start is not None and bound is not None and bound < start:
        return "proven_outside"
    return "unresolved"


def window_membership(unresolved_rows, window_end, window_start=None) -> dict:
    """Split the global undated population against ONE window.

    ``unresolved_rows`` is ``[{"contact_id", "created_at",
    "known_reached_sql_by"}, …]`` — every contact that reached SQL with no
    effective entry date. The same list is used for every window; what changes
    is how much of it can be ruled out.
    """
    rows = list(unresolved_rows or [])
    proven_outside = 0
    unresolved = 0
    unknown_creation = 0
    bounded = 0
    excluded_by_boundary = 0
    for row in rows:
        row = row or {}
        created = row.get("created_at")
        bound = row.get("known_reached_sql_by")
        if created is None:
            unknown_creation += 1
        if bound is not None:
            bounded += 1
        verdict = membership_verdict(created, window_end, bound, window_start)
        if verdict == "proven_outside":
            proven_outside += 1
            # Attributed to the boundary only when creation alone could NOT
            # have ruled it out — so the two rules are never double-counted.
            if membership_verdict(created, window_end) != "proven_outside":
                excluded_by_boundary += 1
        else:
            unresolved += 1
    return {
        "window_membership_unresolved": unresolved,
        "window_membership_proven_outside": proven_outside,
        # Reported separately because it is a different KIND of unknown: these
        # contacts can never be ruled out of any window by any amount of
        # window arithmetic, so they bound what this method can ever achieve.
        "unresolved_without_created_at": unknown_creation,
        # PR-ADS-160: how much of the exclusion the BOUNDARY is responsible for,
        # kept apart from the creation rule so neither can silently absorb the
        # other's work — or its absence.
        "window_membership_bounded": bounded,
        "window_membership_excluded_by_boundary": excluded_by_boundary,
        "global_missing_sql_entry_date": len(rows),
    }


def window_coverage(*, window, window_end, confirmed_sqls, recovered_sqls,
                    unresolved_rows, population_available: bool = True,
                    window_start=None, boundary_observed_at=None,
                    open_post_boundary_incidents=None, freshness=None) -> dict:
    """One window's SQL coverage verdict, and whether it may publish a total.

    ``confirmed_sqls`` is the count of contacts with a PROVEN effective
    SQL-entry date inside the window — the confirmed subset, which may always be
    published as such. ``recovered_sqls`` is how many of those came from
    recovered lifecycle history rather than the direct property.

    The complete total is publishable only when no undated contact could belong
    to this window. While any could, the complete total and the CPQL derived
    from it are unavailable — not zero, not the confirmed subset relabelled.

    PR-ADS-160 adds ``window_start`` (needed for the boundary's upper-bound
    exclusion), ``boundary_observed_at``, ``open_post_boundary_incidents`` and
    ``freshness``,
    and reports a per-window certification verdict alongside the existing
    completeness verdict. The two are different questions: completeness asks
    whether the HISTORICAL population can be ruled out, certification asks
    whether the window lies wholly in the period for which evidence is
    guaranteed going forward.
    """
    if not population_available:
        return {
            "window": window,
            "confirmed_sqls": confirmed_sqls,
            "window_membership_recovered": recovered_sqls,
            "global_missing_sql_entry_date": None,
            "window_membership_unresolved": None,
            "window_membership_proven_outside": None,
            "window_membership_bounded": None,
            "window_membership_excluded_by_boundary": None,
            "window_total_complete": False,
            "complete_sql_total": None,
            "cpql_publishable": False,
            "reason": COVERAGE_POPULATION_UNAVAILABLE,
            "explanation": (
                "the lifecycle-SQL population could not be read, so this "
                "window's completeness is unknown — not complete, and not zero"),
            "post_boundary_gaps_ruled_out": None,
            "post_boundary_gaps_global": None,
            **_certification(None, None, None, None, available=False),
        }

    split = window_membership(unresolved_rows, window_end, window_start)
    unresolved = split["window_membership_unresolved"]
    complete = unresolved == 0

    # PR-ADS-160 §6 — resolve prospective gaps against THIS window, not one
    # global count applied everywhere. A list is resolved per window; a bare
    # int is a pre-resolved count; None means the store could not be read.
    if isinstance(open_post_boundary_incidents, (list, tuple)):
        incident_split = incident_membership(
            open_post_boundary_incidents, window_start, window_end)
    elif open_post_boundary_incidents is None:
        incident_split = {"open_post_boundary_gaps": None,
                          "post_boundary_gaps_ruled_out": None,
                          "post_boundary_gaps_global": None}
    else:
        incident_split = {
            "open_post_boundary_gaps": int(open_post_boundary_incidents),
            "post_boundary_gaps_ruled_out": 0,
            "post_boundary_gaps_global": int(open_post_boundary_incidents)}
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
        "post_boundary_gaps_ruled_out":
            incident_split["post_boundary_gaps_ruled_out"],
        "post_boundary_gaps_global": incident_split["post_boundary_gaps_global"],
        **_certification(window_start, window_end, boundary_observed_at,
                         incident_split["open_post_boundary_gaps"],
                         complete=complete, freshness=freshness),
    }


# ── PR-ADS-160 §6 — per-window certification, stated in stable codes ─────────
#: Every prerequisite this module can judge is met. NOT a claim that the window
#: IS certified: freshness and reader reconciliation are checked by the audit,
#: which may still withhold certification. This says "nothing here blocks it".
CERT_ELIGIBLE = "eligible"
#: The window opens before the boundary, so it contains historical SQL events
#: whose dates are unknowable. It can never be certified.
CERT_PRE_BOUNDARY = "not_certifiable_window_precedes_boundary"
#: The window straddles the boundary instant. An undated historical event could
#: fall on either side of it, so membership stays genuinely open.
CERT_OVERLAPS_BOUNDARY = "not_certifiable_window_overlaps_boundary"
#: Open post-boundary incidents exist that this window cannot rule out.
CERT_POST_BOUNDARY_GAPS = "not_certifiable_open_post_boundary_gaps"
#: Membership of the historical undated population is not fully resolved here.
CERT_INCOMPLETE = "not_certifiable_unresolved_membership"
#: No completed boundary exists yet, so "after the boundary" has no meaning.
CERT_NO_BOUNDARY = "not_certifiable_no_boundary_established"
#: The inputs could not be read. Never rendered as "not certified" — unknown.
CERT_UNAVAILABLE = "certification_unavailable"
#: PR-ADS-160 §5 — the contact-funnel data behind this window is not fresh.
#: Certifying a window whose source stopped updating would publish a number that
#: is complete only with respect to data that has stopped arriving.
CERT_STALE_SOURCE = "not_certifiable_source_not_fresh"


def incident_membership(incidents, window_start, window_end) -> dict:
    """Which open post-boundary incidents could belong to THIS window.

    PR-ADS-160 first passed one GLOBAL open-incident count to every window, so a
    single gap blocked certification everywhere — including windows that closed
    before the contact existed. That is the same conflation this module removes
    for historical gaps, reintroduced for prospective ones.

    An incident carries no SQL entry date; if it did, it would not be an
    incident. So the same two sound bounds apply, and only those:

    * ``contact_created_at`` is a LOWER bound. A contact created at or after a
      window's end cannot have entered SQL inside it.
    * ``detected_at`` is an observation UPPER bound — the instant we first saw
      the contact already at SQL with no date. If that is strictly before the
      window opened, the transition was over before the window began.

    Neither is the event. Nothing here ever returns a date, and an incident with
    neither bound readable blocks every window, because nothing rules it out.
    """
    rows = list(incidents or [])
    end = _window_end_exclusive(window_end)
    start = _as_datetime(window_start)

    could_belong = 0
    ruled_out = 0
    for row in rows:
        row = row or {}
        created = _as_datetime(row.get("contact_created_at"))
        detected = _as_datetime(row.get("detected_at"))

        # Created after this window closed: it did not exist while the window
        # was open, so it cannot have entered SQL inside it.
        if end is not None and created is not None and created >= end:
            ruled_out += 1
            continue
        # Already at SQL before this window opened. Strict, for the same reason
        # the historical rule is strict: at equality the transition could have
        # happened at the window's inclusive first instant.
        if start is not None and detected is not None and detected < start:
            ruled_out += 1
            continue
        could_belong += 1

    return {
        "open_post_boundary_gaps": could_belong,
        "post_boundary_gaps_ruled_out": ruled_out,
        "post_boundary_gaps_global": len(rows),
    }


def _certification(window_start, window_end, boundary_observed_at,
                   open_incidents, *, complete: bool = False,
                   available: bool = True, freshness: dict | None = None) -> dict:
    """Can THIS window become certified? The window-local half of the answer.

    Certification is deliberately split in two. This function judges what can be
    judged from the window and the evidence population alone; the audit adds
    dataset freshness and the 44-way reader reconciliation on top and may still
    refuse. Neither half can grant certification by itself.

    An open-ended window is never certifiable: "All Time" necessarily includes
    the historical period whose dates are unknowable, and no boundary changes
    that.

    ``open_incidents`` is this window's OWN count from ``incident_membership``,
    never a global one. ``freshness`` is the contact-funnel freshness verdict:
    a window whose source has stopped updating cannot certify, because its
    completeness would be complete only with respect to data that stopped
    arriving.
    """
    if not available:
        return {"certification_status": CERT_UNAVAILABLE,
                "certification_eligible": False,
                "window_after_boundary": None,
                "open_post_boundary_gaps": None,
                "source_fresh": None,
                "certification_explanation": (
                    "the inputs could not be read, so certification is unknown "
                    "— not granted, and not refused")}

    start = _as_datetime(window_start)
    boundary = _as_datetime(boundary_observed_at)
    end = _window_end_exclusive(window_end)
    # None means the incident store could not be read. That is NOT zero gaps:
    # an unknown number of blockers must block, or an outage certifies windows.
    gaps_unknown = open_incidents is None
    gaps = 0 if gaps_unknown else int(open_incidents)
    fresh_state = (freshness or {}).get("fresh")
    fresh_reason = (freshness or {}).get("reason")

    if boundary is None:
        return {"certification_status": CERT_NO_BOUNDARY,
                "certification_eligible": False,
                "window_after_boundary": None,
                "open_post_boundary_gaps": open_incidents,
                "source_fresh": fresh_state,
                "certification_explanation": (
                    "no completed coverage boundary exists, so no window can "
                    "yet be certified")}

    # An open-ended or unbounded-start window spans the historical period.
    if start is None:
        return {"certification_status": CERT_PRE_BOUNDARY,
                "certification_eligible": False,
                "window_after_boundary": False,
                "open_post_boundary_gaps": open_incidents,
                "source_fresh": fresh_state,
                "certification_explanation": (
                    "this window has no start bound, so it includes the "
                    "historical period whose SQL dates are unknowable")}

    after_boundary = start >= boundary
    if not after_boundary:
        # Does it merely precede the boundary, or straddle it?
        straddles = end is None or end > boundary
        status = CERT_OVERLAPS_BOUNDARY if straddles else CERT_PRE_BOUNDARY
        detail = ("straddles the boundary instant, so an undated historical "
                  "event could fall on either side of it"
                  if straddles else
                  "closes before the boundary, so it lies entirely inside the "
                  "historical period whose SQL dates are unknowable")
        return {"certification_status": status,
                "certification_eligible": False,
                "window_after_boundary": False,
                "open_post_boundary_gaps": open_incidents,
                "source_fresh": fresh_state,
                "certification_explanation": f"this window {detail}"}

    if gaps_unknown:
        return {"certification_status": CERT_UNAVAILABLE,
                "certification_eligible": False,
                "window_after_boundary": True,
                "open_post_boundary_gaps": None,
                "source_fresh": fresh_state,
                "certification_explanation": (
                    "the post-boundary incident store could not be read, so it "
                    "is unknown whether any prospective gap belongs to this "
                    "window — unknown blockers must block")}

    if gaps:
        return {"certification_status": CERT_POST_BOUNDARY_GAPS,
                "certification_eligible": False,
                "window_after_boundary": True,
                "open_post_boundary_gaps": open_incidents,
                "source_fresh": fresh_state,
                "certification_explanation": (
                    f"{gaps} post-boundary contact(s) reached SQL with no exact "
                    f"entry date and cannot be ruled out of this window")}

    if not complete:
        return {"certification_status": CERT_INCOMPLETE,
                "certification_eligible": False,
                "window_after_boundary": True,
                "open_post_boundary_gaps": open_incidents,
                "source_fresh": fresh_state,
                "certification_explanation": (
                    "membership of the undated population is not fully "
                    "resolved for this window")}

    # PR-ADS-160 §5 — freshness is a PREREQUISITE, not a footnote. A window
    # whose source stopped updating is complete only with respect to data that
    # stopped arriving, and `fresh is not True` covers stale, failed, missing
    # and unreadable alike: only a proven-fresh source certifies.
    if fresh_state is not True:
        return {"certification_status": CERT_STALE_SOURCE,
                "certification_eligible": False,
                "window_after_boundary": True,
                "open_post_boundary_gaps": open_incidents,
                "source_fresh": fresh_state,
                "certification_explanation": (
                    f"the canonical contact-funnel source is not proven fresh "
                    f"({fresh_reason or 'freshness unknown'}), so this window's "
                    f"completeness describes data that may have stopped "
                    f"arriving")}

    return {"certification_status": CERT_ELIGIBLE,
            "certification_eligible": True,
            "window_after_boundary": True,
            "open_post_boundary_gaps": open_incidents,
            "source_fresh": True,
            "certification_explanation": (
                "this window opens at or after the proven boundary, no open "
                "post-boundary gap can belong to it, every historical undated "
                "contact is ruled out, and the contact-funnel source is fresh "
                "— reader reconciliation is still checked by the audit")}


def _explain(complete: bool, unresolved: int, split: dict, window_end) -> str:
    """Why completeness is or is not proven, for THIS window. Always stated."""
    if complete:
        proven = split["window_membership_proven_outside"]
        if not split["global_missing_sql_entry_date"]:
            return ("every lifecycle-SQL contact has a proven entry date, so "
                    "this window's population is complete")
        bounded = split.get("window_membership_excluded_by_boundary") or 0
        if bounded:
            by_creation = proven - bounded
            return (f"all {proven} undated lifecycle-SQL contact(s) are ruled "
                    f"out of this window: {by_creation} created after it ended, "
                    f"and {bounded} already at SQL before it began. Neither "
                    f"gives any of them a date")
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
