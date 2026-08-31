#!/usr/bin/env python3
"""
scripts/backfill_lifecycle_stage_history.py

PR-ADS-155 §4 — recover missing lifecycle stage-entry timestamps from HubSpot's
own property history, or prove that HubSpot does not hold them.

    # ALWAYS dry-run first. Reads HubSpot, writes nothing, anywhere.
    python -m scripts.backfill_lifecycle_stage_history --limit 50

    # Only after a dry run has shown what it would recover:
    python -m scripts.backfill_lifecycle_stage_history --limit 50 --apply

Exit codes:
    0  the pass completed (whether or not it recovered anything)
    1  the pass could not complete — the reason is printed, nothing was written
    2  usage error

What this does and does not do
------------------------------
It reads the ``lifecyclestage`` property HISTORY for contacts whose current
lifecycle stage proves they reached a stage for which ``hs_v2_date_entered_*``
is null. Where HubSpot holds a version recording that transition, its timestamp
is ingested with full provenance. Where HubSpot holds no such version, NOTHING
is written: the timestamp stays null, and the lifecycle cohort continues to
report that contact as an excluded coverage gap.

No date is ever synthesised. Contact creation date, the current-stage date, the
ingestion timestamp and neighbouring stage dates are not used, and no
interpolation of any kind is performed.

**It never writes to HubSpot.** The only HubSpot call is a batch READ with
``propertiesWithHistory``. ``--apply`` writes to the LOCAL database only, into
``hubspot_lifecycle_stage_history``.

Bounded, idempotent, resumable
------------------------------
Every run takes an explicit ``--limit``. Rows are keyed on
``(contact_id, funnel_event)``, so a re-run rewrites rather than duplicates. A
durable cursor advances only on ``--apply`` runs that completed, so a stopped
run resumes exactly where it left off. ``--restart`` ignores the cursor.
"""

from __future__ import annotations

import argparse
import json
import sys

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _database_ready() -> tuple[bool, str | None]:
    """Initialize AND probe the database before reading contacts or HubSpot.

    PR-ADS-155-F1, same omission as the missing-amount report: a standalone
    ``python -m`` process has never called ``init_pool``, so this command read
    an empty contact list and could have reported "0 contacts with gaps" over a
    database it never opened. Worse for this command than for the report — it
    spends HubSpot quota, and a run whose local writes silently no-op burns that
    quota to produce nothing.
    """
    from db.connection import ensure_database_ready

    return ensure_database_ready()

DEFAULT_LIMIT = 100
MAX_LIMIT = 2000

#: Plain-English meaning of each evidence state, so the breakdown is readable by
#: whoever runs the command rather than only by whoever wrote it.
_STATE_MEANING = {
    "history_contact_not_returned":
        "HubSpot's response did not include this contact at all",
    "history_payload_missing":
        "returned, but with no lifecyclestage history payload",
    "history_payload_empty":
        "returned an EMPTY history — HubSpot holds no versions",
    "history_payload_present":
        "history was returned and was inspected",
    "history_present_no_matching_stage_version":
        "history exists but holds no version for this stage",
    "history_version_without_timestamp":
        "a matching version exists but carries no usable timestamp",
    "matching_stage_version_recovered":
        "HubSpot's own recorded transition timestamp was ingested",
    "history_request_unavailable":
        "the HubSpot request itself failed — nothing was proven",
}


def _render(result: dict) -> None:
    print("=" * 78)
    print("  LIFECYCLE STAGE-ENTRY RECOVERY FROM HUBSPOT PROPERTY HISTORY")
    print(f"  mode: {result.get('mode')}    run: {result.get('run_id')}")
    print(f"  HubSpot writes performed: {result.get('hubspot_writes_performed')}")
    print("=" * 78)

    if not result.get("ok"):
        print(f"\n  RUN DID NOT COMPLETE: {result.get('reason')}")
        print(f"  {result.get('detail')}")
        print("\n  Nothing was written. Recovered counts are UNKNOWN, not zero:")
        print("  an aborted pass proves nothing about how much evidence HubSpot holds.")
        return

    print(f"\n  resume from cursor:        {result.get('resume_from')}")
    print(f"  next cursor:               {result.get('next_cursor')}")
    print(f"  contacts with gaps read:   {result.get('contacts_with_gaps')}")
    print(f"  contacts examined:         {result.get('contacts_examined')}")
    print(f"  contacts with NO history:  {result.get('contacts_without_history')}")
    print(f"  contacts with recovery:    {result.get('contacts_recovered')}")
    print(f"  stage events recovered:    {result.get('events_recovered')}")
    print(f"  stage events persisted:    {result.get('events_persisted')}")
    print(f"  stage events unresolved:   {result.get('events_unresolved')}")

    recovered = result.get("recovered") or []
    if recovered:
        print("\n  RECOVERED (HubSpot property-history evidence)")
        for row in recovered[:25]:
            print(f"    {row['contact_id']}  {row['funnel_event']:<12} "
                  f"{row['entered_at']}  "
                  f"[{row.get('hubspot_source_type') or 'source unknown'}]")
        if len(recovered) > 25:
            print(f"    … and {len(recovered) - 25} more")

    # PR-ADS-155-F1: a zero must be readable. These are the states that explain
    # WHY nothing was recovered, and they are the basis for deciding whether
    # --apply is worth running at all.
    evidence = result.get("evidence_states") or {}
    per_contact = evidence.get("per_contact_payload_state") or {}
    if per_contact:
        print("\n  HUBSPOT EVIDENCE — per contact asked")
        for state, count in per_contact.items():
            print(f"    {count:>5}  {state}  — {_STATE_MEANING.get(state, '')}")

    per_stage = evidence.get("per_stage_gap_reason") or {}
    if per_stage:
        print("\n  OUTCOME — per (contact, missing stage)")
        for reason, count in per_stage.items():
            print(f"    {count:>5}  {reason}  — {_STATE_MEANING.get(reason, '')}")

    if result.get("events_recovered") == 0 and per_contact:
        print("\n  Nothing was recovered. Read the states above before deciding:")
        print("  an absent payload is not the same finding as an empty history,")
        print("  and neither is the same as history that holds no version for")
        print("  the stage. Every unrecovered timestamp stays NULL.")

    if result.get("mode") == "dry_run":
        print("\n  DRY RUN: nothing was written to the local database, and nothing")
        print("  was ever written to HubSpot. Re-run with --apply to persist.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover lifecycle stage-entry dates from HubSpot property "
                    "history (read-only against HubSpot).")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"max contacts to examine (default {DEFAULT_LIMIT}, "
                             f"max {MAX_LIMIT})")
    parser.add_argument("--apply", action="store_true",
                        help="persist recovered timestamps to the LOCAL database "
                             "(never to HubSpot). Default is a dry run.")
    parser.add_argument("--restart", action="store_true",
                        help="ignore the durable cursor and start from the first "
                             "contact id")
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    args = parser.parse_args()

    if args.limit < 1 or args.limit > MAX_LIMIT:
        print(f"--limit must be between 1 and {MAX_LIMIT}", file=sys.stderr)
        return EXIT_USAGE

    # Before the contact read and before any HubSpot call.
    ready, detail = _database_ready()
    if not ready:
        print(f"database unavailable: {detail}", file=sys.stderr)
        print("No contact was examined and no HubSpot call was made. The number "
              "of recoverable timestamps is UNKNOWN, not zero.", file=sys.stderr)
        return EXIT_FAILED

    from services import lifecycle_history_recovery_service as recovery

    result = recovery.recover(limit=args.limit, apply=args.apply,
                              resume=not args.restart)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _render(result)

    return EXIT_OK if result.get("ok") else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
