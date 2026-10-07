"""
scripts/audit_post_boundary_sql_incidents.py

PR-ADS-161C — forensic audit of open post-boundary SQL incidents. READ-ONLY.

    python -m scripts.audit_post_boundary_sql_incidents
    python -m scripts.audit_post_boundary_sql_incidents --json
    python -m scripts.audit_post_boundary_sql_incidents --sample 25
    python -m scripts.audit_post_boundary_sql_incidents --compare-hubspot --sample 25 --json

For every open incident it states WHERE the exact SQL-entry evidence
disappeared, from a finite vocabulary (`analysis/post_boundary_sql_forensics.py`):
a code-owned loss (HubSpot holds the date and our path lost it), a source with
no exact SQL entry (most commonly a stage jump — the contact never sat in
`salesqualifiedlead` at all), or not determined (we did not look, or got no
answer).

Without `--compare-hubspot` it makes NO HubSpot call and classifies from the
local store. With it, it makes READ-ONLY HubSpot reads over the first
`--sample` open incidents (all of them without `--sample`). It never writes —
not to HubSpot, not to Google Ads, not locally: its database read runs in a
PostgreSQL READ ONLY transaction.

Exit codes
    0  audit completed; no open incident traced to a code-owned loss. This
       does NOT mean there are no incidents — see `open_incidents`.
    1  a code-owned loss, or a resolved incident with no stored evidence
    2  unavailable — the boundary, the incident store or HubSpot (when asked
       for) could not be read; nothing is reported as zero
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
    print("  PR-ADS-161C — POST-BOUNDARY SQL INCIDENT FORENSICS (read-only)")
    print(f"  mode: {report.get('mode')}    verdict: {report.get('verdict')}")
    print(f"  HubSpot calls: {report.get('hubspot_calls_performed')}    "
          f"external writes: {report.get('external_writes_performed')}    "
          f"database writes: {report.get('database_writes_performed')}")
    print("=" * 78)
    if not report.get("audit_complete"):
        print(f"\n  AUDIT UNAVAILABLE: {report.get('detail')}")
        print("  Counts are UNKNOWN, not zero.")
        return
    fresh = report.get("source_freshness") or {}
    print(f"\n  boundary:            {report.get('boundary_id')} "
          f"(observed {report.get('boundary_observed_at')})")
    print(f"  source fresh:        {report.get('source_fresh')} "
          f"({fresh.get('reason')})")
    print(f"  open incidents:      {report.get('open_incidents')}")
    print(f"  resolved incidents:  {report.get('resolved_incidents')}")
    print(f"  oldest / newest open: {report.get('oldest_open_incident_at')} / "
          f"{report.get('newest_open_incident_at')}")
    root = report.get("root_cause") or {}
    print(f"\n  ROOT CAUSE ({root.get('denominator')})")
    for key, value in (root.get("by_owner") or {}).items():
        print(f"    {key:<34} {value}")
    for key, value in (root.get("by_classification") or {}).items():
        print(f"      {key:<38} {value}")
    integrity = report.get("integrity") or {}
    print(f"\n  resolved without stored evidence: "
          f"{integrity.get('resolved_without_stored_evidence')}")
    print(f"\n  lifecycle-event publication: "
          f"{(report.get('lifecycle_event_publication') or {}).get('status')}")
    cohort = report.get("acquisition_cohort_publication") or {}
    print(f"  acquisition-cohort freshness gate: {cohort.get('freshness_gate')} "
          f"({cohort.get('freshness_reason')}) — not governed by incidents")
    listed = report.get("incidents") or []
    if listed:
        print(f"\n  INCIDENTS (first {len(listed)} of {report.get('open_incidents')})")
        for item in listed:
            print(f"    {item.get('contact_id'):<14} "
                  f"{item.get('classification'):<34} "
                  f"path={item.get('history_stage_path')}")
    print(f"\n  {report.get('note')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Forensic audit of post-boundary SQL incidents (read-only)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    parser.add_argument("--sample", type=int, default=None,
                        help="list (and, with --compare-hubspot, compare) only "
                             "the first N open incidents")
    parser.add_argument("--compare-hubspot", action="store_true",
                        help="make READ-ONLY HubSpot reads to compare source "
                             "evidence with stored evidence")
    args = parser.parse_args(argv)

    from db.connection import ensure_database_ready

    ready, error = ensure_database_ready()
    if not ready:
        payload = {"audit_complete": False, "verdict": "audit_unavailable",
                   "detail": f"database not ready: {error}",
                   "open_incidents": None, "hubspot_calls_performed": 0,
                   "external_writes_performed": 0,
                   "database_writes_performed": 0}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database not ready: {error}")
        return EXIT_UNAVAILABLE

    from services import post_boundary_sql_evidence_service as service

    try:
        report = service.audit(compare_hubspot=bool(args.compare_hubspot),
                               sample=args.sample)
    except Exception as exc:  # noqa: BLE001
        payload = {"audit_complete": False, "verdict": "audit_unavailable",
                   "detail": f"unexpected error: {type(exc).__name__}: "
                             f"{str(exc)[:300]}",
                   "open_incidents": None}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — {payload['detail']}")
        return EXIT_UNAVAILABLE

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    return int(report.get("exit_code", EXIT_UNAVAILABLE))


if __name__ == "__main__":
    sys.exit(main())
