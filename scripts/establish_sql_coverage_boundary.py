#!/usr/bin/env python3
"""
scripts/establish_sql_coverage_boundary.py

PR-ADS-160 §2/§3 — draw the line between an unknowable past and a guaranteed
future, and prove what it would do before it does anything.

    # ALWAYS dry-run first. Reads the local database, writes nothing, anywhere.
    python -m scripts.establish_sql_coverage_boundary

    # Show the exact contacts that would be bounded, not just the count.
    python -m scripts.establish_sql_coverage_boundary --show-population

    # Only after a human has approved the dry run's population:
    python -m scripts.establish_sql_coverage_boundary --apply

    # Machine-readable, exit code unchanged.
    python -m scripts.establish_sql_coverage_boundary --json

Exit codes
----------
    0  the run completed (a dry run that proposed, or an apply that recorded)
    1  the run could not complete — the reason is printed; see below for what,
       if anything, was written
    2  usage error

What a boundary IS
------------------
An upper bound on an unknown event: "by instant B, these contacts had ALREADY
reached SQL". PR-ADS-159's production validation exhausted historical recovery —
533 contacts reached SQL, all 533 returned valid HubSpot lifecycle history, and
none of those histories held a transition into ``salesqualifiedlead``. Their
dates are absent from HubSpot, not merely missing from us.

The bound is the strongest TRUE statement still available about them, and its
only sound use is to DISPROVE membership: an event known to have happened before
B cannot have happened inside a window that opens after B.

What a boundary is NOT
----------------------
It is not a date for anything. This command never writes
``hubspot_contact_funnel.date_entered_sql``, never writes
``hubspot_lifecycle_stage_history``, and never contacts HubSpot at all. The
bound is stored in its own table under a column called ``known_reached_sql_by``,
so a reader who mistakes it for an event date is contradicting the column's own
name.

It does not make All Time completable. Every window that opens before the
boundary — All Time included — still contains historical SQL events whose dates
are unknowable, and stays honestly incomplete forever.

Safety
------
Dry run by default · local writes only under ``--apply`` · no HubSpot write path
exists in this module · the boundary and its bounded contacts commit in ONE
transaction, so a half-established boundary is not a state that can occur ·
re-applying the same boundary rewrites the same rows and says so.
"""

from __future__ import annotations

import argparse
import json
import sys

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _database_ready() -> tuple[bool, str | None]:
    """Initialize AND probe the database before reading anything.

    A standalone ``python -m`` process has never called ``init_pool``. Without
    this, the command would read an empty population and could establish a
    boundary that bounds NOBODY while reporting success — the exact failure the
    fail-closed population check exists to prevent, arriving one layer earlier.
    """
    from db.connection import ensure_database_ready

    return ensure_database_ready()


def _render(result: dict, *, show_population: bool = False) -> None:
    print("=" * 78)
    print("  PR-ADS-160 — PROSPECTIVE SQL COVERAGE BOUNDARY")
    print(f"  mode: {result.get('mode')}    run: {result.get('run_id')}")
    print(f"  HubSpot writes performed: {result.get('hubspot_writes_performed')}")
    print("=" * 78)

    if not result.get("ok"):
        print(f"\n  RUN DID NOT COMPLETE: {result.get('reason')}")
        print(f"  {result.get('detail')}")
        if result.get("boundary_written"):
            # Cannot currently happen — the write is one transaction — but the
            # renderer states what was written rather than assuming nothing was.
            print(f"\n  NOTE: the boundary WAS written "
                  f"({result.get('contacts_written')} bounded contacts).")
        else:
            print("\n  No boundary was established and no contact was bounded.")
        print("  Counts above are UNKNOWN where the input could not be read —")
        print("  never zero. A boundary over an unknown population would bound")
        print("  nobody while appearing established.")
        return

    boundary = result.get("boundary") or {}
    existing = result.get("existing_boundary")
    if existing:
        print(f"\n  EXISTING BOUNDARY: {existing.get('boundary_id')}")
        print(f"    observed at: {existing.get('observed_at')}")

    print(f"\n  PROPOSED BOUNDARY")
    print(f"    boundary id:            {boundary.get('boundary_id')}")
    print(f"    observed at (UTC):      {boundary.get('observed_at')}")
    print(f"    lifecycle rule version: {boundary.get('lifecycle_rule_version')}")
    print(f"    source dataset:         {boundary.get('source_dataset')}")
    print(f"    population definition:  {boundary.get('population_definition')}")

    print(f"\n  POPULATION")
    print(f"    contacts examined:               {result.get('contacts_examined')}")
    print(f"    legacy undated SQL contacts:     "
          f"{result.get('legacy_undated_bounded')}")
    print(f"    of those, with NO creation time: "
          f"{result.get('bounded_without_created_at')}")
    print(f"    rows that would be written:      {result.get('rows_to_write')}")

    if show_population:
        rows = result.get("population_rows") or []
        if rows:
            print(f"\n  BOUNDED CONTACTS (first 50 of {len(rows)})")
            for row in rows[:50]:
                print(f"    {row.get('contact_id'):<16} "
                      f"stage={row.get('lifecycle_stage')} "
                      f"created={row.get('created_at')}")
            print("    Each of these gets an UPPER BOUND, not a date. None of")
            print("    them receives a value in date_entered_sql.")

    print(f"\n  WRITTEN")
    print(f"    boundary recorded:  {result.get('boundary_written')}")
    print(f"    contacts bounded:   {result.get('contacts_written')}")
    if result.get("already_applied"):
        print("    (this boundary already existed — the rows were rewritten,")
        print("     not appended; the run is an idempotent no-op)")

    print(f"\n  CERTIFICATION")
    print(f"    can begin: {result.get('certification_can_begin')}")
    note = result.get("certification_note")
    if note:
        print(f"    {note}")

    if not result.get("apply"):
        print("\n  DRY RUN — nothing was written, locally or anywhere else.")
        print("  Re-run with --apply once the population above is approved.")

    print("\n  The 533 historical contacts do NOT receive SQL dates from this")
    print("  command, or from any other. All Time and every window overlapping")
    print("  the boundary stay incomplete.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Establish the prospective SQL coverage boundary (local only)")
    parser.add_argument("--apply", action="store_true",
                        help="record the boundary LOCALLY (default: dry run)")
    parser.add_argument("--boundary-id", default=None,
                        help="explicit boundary identifier; required to replace "
                             "an existing boundary")
    parser.add_argument("--observed-at", default=None,
                        help="ISO-8601 UTC observation instant (default: now)")
    parser.add_argument("--show-population", action="store_true",
                        help="list the contacts that would be bounded")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    args = parser.parse_args()

    ready, error = _database_ready()
    if not ready:
        payload = {"ok": False, "reason": "database_unavailable",
                   "detail": error, "hubspot_writes_performed": False,
                   "boundary_written": False, "contacts_written": 0}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database not ready: {error}")
        return EXIT_FAILED

    from services import sql_coverage_boundary_service as service

    try:
        result = service.establish_boundary(
            apply=bool(args.apply),
            observed_at=args.observed_at,
            boundary_id=args.boundary_id,
        )
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "reason": "unexpected_error",
                   "detail": str(exc)[:500],
                   "hubspot_writes_performed": False,
                   "boundary_written": False, "contacts_written": 0}
        print(json.dumps(payload, indent=2) if args.json
              else f"FAILED — {exc}")
        return EXIT_FAILED

    if args.show_population and result.get("ok"):
        from db import crm_funnel_repository as repo

        population = repo.fetch_boundary_candidate_population()
        result["population_rows"] = (population.get("rows") or []
                                     if population.get("available") else [])

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _render(result, show_population=args.show_population)

    return EXIT_OK if result.get("ok") else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
