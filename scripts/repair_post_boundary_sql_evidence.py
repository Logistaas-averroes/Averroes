"""
scripts/repair_post_boundary_sql_evidence.py

PR-ADS-161C — recover exact SQL-entry evidence for OPEN post-boundary
incidents from HubSpot, and only from HubSpot. DRY RUN BY DEFAULT.

    python -m scripts.repair_post_boundary_sql_evidence --dry-run --json
    python -m scripts.repair_post_boundary_sql_evidence --apply --json

Reads HubSpot (read-only) for each open incident. A timestamp is persisted only
from HubSpot's direct `hs_v2_date_entered_salesqualifiedlead` property or from a
genuine `salesqualifiedlead` transition in its lifecycle history. Everything
else stays NULL with its incident open. Zero recoverable is a truthful result.

`--apply` writes ONLY to the local database, in ONE transaction: the direct
column where it is NULL, recovered lifecycle-history rows, and the closure of
the incidents that evidence proves. Any failure rolls all of it back. Never
writes to HubSpot; never calls Google Ads. Rerunning `--apply` is a no-op for
everything already repaired.

Exit codes
    0  completed (dry run or apply); unresolved incidents are reported, not
       failures
    1  partial (some contacts could not be read from HubSpot) or failed (the
       local write rolled back)
    2  unavailable — database, boundary/incident store or HubSpot unreadable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2


def _render(report: dict) -> None:
    print("=" * 78)
    print("  PR-ADS-161C — POST-BOUNDARY SQL EVIDENCE REPAIR")
    print(f"  mode: {report.get('mode')}    run: {report.get('run_id')}    "
          f"status: {report.get('status')}")
    print(f"  HubSpot writes: {report.get('hubspot_writes_performed')}    "
          f"Google Ads calls: {report.get('google_ads_calls_performed')}")
    print("=" * 78)
    for key in ("open_before", "examined", "recoverable_from_stored_evidence",
                "recoverable_direct_property", "recoverable_lifecycle_history",
                "unresolved", "contacts_unread", "written", "unchanged",
                "incidents_resolved", "open_after"):
        if key in report:
            print(f"  {key:<36} {report.get(key)}")
    if report.get("unresolved_reasons"):
        print("\n  unresolved, by reason:")
        for reason, count in report["unresolved_reasons"].items():
            print(f"    {reason:<52} {count}")
    print(f"\n  {report.get('detail')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Repair post-boundary SQL evidence from HubSpot (local "
                    "writes only; dry run by default)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true",
                       help="read and report; write nothing (the default)")
    group.add_argument("--apply", action="store_true",
                       help="persist recoverable evidence LOCALLY, in one "
                            "transaction")
    parser.add_argument("--limit", type=int, default=None,
                        help="examine at most N open incidents")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    args = parser.parse_args(argv)

    from db.connection import ensure_database_ready

    ready, error = ensure_database_ready()
    if not ready:
        payload = {"status": "unavailable", "detail": f"database not ready: "
                   f"{error}", "hubspot_writes_performed": False,
                   "written": 0, "examined": None}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database not ready: {error}")
        return EXIT_UNAVAILABLE

    from services import post_boundary_sql_evidence_service as service

    try:
        report = service.repair(apply=bool(args.apply), limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        payload = {"status": "failed",
                   "detail": f"unexpected error: {type(exc).__name__}: "
                             f"{str(exc)[:300]}",
                   "hubspot_writes_performed": False}
        print(json.dumps(payload, indent=2) if args.json
              else f"FAILED — {payload['detail']}")
        return EXIT_VIOLATION

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    return int(report.get("exit_code", EXIT_UNAVAILABLE))


if __name__ == "__main__":
    sys.exit(main())
