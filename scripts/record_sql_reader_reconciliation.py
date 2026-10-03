#!/usr/bin/env python3
"""
scripts/record_sql_reader_reconciliation.py

PR-ADS-161A-1 — run the 44-way canonical reader comparison and RECORD it.

Why this is a separate command
------------------------------
`scripts/audit_lifecycle_sql_coverage.py` already performs this comparison, and
it is read-only by contract — it reports `external_writes_performed: false` and
is run under a session-level `SET TRANSACTION READ ONLY` guard during
production validation. Teaching it to write would break both.

So the comparison is re-run here, by calling the audit's own function rather
than a second copy of the logic, and the outcome is written to
`sql_reader_reconciliation`. `services.canonical_sql_publication_service` reads
the newest row and treats anything older than its max age as no proof at all.

Nothing external is contacted. The only write is one append-only row in our own
database.

    python -m scripts.record_sql_reader_reconciliation            # dry run
    python -m scripts.record_sql_reader_reconciliation --apply    # record it
    python -m scripts.record_sql_reader_reconciliation --json

Exit codes:
    0  the readers reconcile (and, with --apply, the row was written)
    1  the readers do NOT reconcile — recorded truthfully, publication stays withheld
    2  the comparison could not be completed, or the write failed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_OK = 0
EXIT_NOT_RECONCILED = 1
EXIT_UNAVAILABLE = 2


def run(*, apply: bool = False, now: datetime | None = None) -> tuple[int, dict]:
    from db.connection import init_pool
    from scripts.audit_lifecycle_sql_coverage import (Findings,
                                                      audit_read_reconciliation)

    now = now or datetime.now(timezone.utc)
    init_pool()

    findings = Findings()
    # The audit's own function, not a second implementation of it. A copy that
    # agreed would prove nothing and one that disagreed would report this
    # command's bug as the product's.
    result = audit_read_reconciliation(findings, now)

    payload = {
        "observed_at": now.isoformat(),
        "reconciliation_complete": result.get("reconciliation_complete"),
        "combinations_expected": result.get("combinations_expected"),
        "combinations_compared": result.get("combinations_compared"),
        "combinations_mismatched": sum(
            1 for r in (result.get("results") or [])
            if r.get("coverage_status") == "mismatch"),
        "combinations_unavailable": result.get("unavailable"),
        "all_combinations_compared": result.get("all_combinations_compared"),
        "effective_date_basis": result.get("effective_date_basis"),
        "available": result.get("available"),
        "applied": False,
        "external_writes_performed": False,
        "hubspot_calls_performed": False,
    }

    if not result.get("available"):
        payload["detail"] = ("no window/scope combination could be compared, so "
                             "reconciliation is UNKNOWN — nothing is recorded, "
                             "and publication stays withheld")
        return EXIT_UNAVAILABLE, payload

    if result.get("combinations_execution_unavailable"):
        # `reconciliation_complete` is False whenever a comparison FAILED TO
        # RUN, which is not the same fact as "they were compared and they
        # disagreed" — and that second, stronger claim is what a recorded
        # `false` says to every later reader. An unproven run is not evidence;
        # nothing is written, and publication stays withheld for the honest
        # reason (no proof) rather than a fabricated one (disagreement).
        payload["detail"] = (
            f"{result['combinations_execution_unavailable']} combination(s) "
            f"could not be executed, so the outcome is UNKNOWN rather than a "
            f"proven disagreement — nothing is recorded")
        return EXIT_UNAVAILABLE, payload

    if not apply:
        payload["detail"] = "dry run — pass --apply to record this outcome"
        return (EXIT_OK if payload["reconciliation_complete"]
                else EXIT_NOT_RECONCILED), payload

    from db import writers as db_writers
    ok = db_writers.record_reader_reconciliation(
        observed_at=now,
        # NOT `bool(...)`: that coercion turned an unproven `None` into a
        # recorded `False`, defeating the writer's own `is None` refusal —
        # round 1's finding, whose fix added a different guard and left this
        # re-entry point open.
        reconciliation_complete=payload["reconciliation_complete"],
        combinations_expected=payload["combinations_expected"],
        combinations_compared=payload["combinations_compared"],
        combinations_mismatched=payload["combinations_mismatched"],
        combinations_unavailable=payload["combinations_unavailable"],
        all_combinations_compared=payload["all_combinations_compared"],
        effective_date_basis=payload["effective_date_basis"],
        run_id=f"recon_{now.strftime('%Y%m%dT%H%M%SZ')}",
        detail={"blocked": [r for r in (result.get("results") or [])
                            if r.get("coverage_status") != "match"][:50]})
    payload["applied"] = bool(ok)
    if not ok:
        payload["detail"] = ("the reconciliation outcome could not be written; "
                             "publication stays withheld")
        return EXIT_UNAVAILABLE, payload

    payload["detail"] = "recorded"
    return (EXIT_OK if payload["reconciliation_complete"]
            else EXIT_NOT_RECONCILED), payload


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run and record the canonical reader reconciliation")
    ap.add_argument("--apply", action="store_true",
                    help="write the outcome (default: dry run)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    try:
        code, payload = run(apply=args.apply)
    except Exception as exc:  # noqa: BLE001
        payload = {"available": False, "applied": False,
                   "external_writes_performed": False,
                   "detail": f"reconciliation could not run: {exc}"}
        code = EXIT_UNAVAILABLE

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"reconciliation_complete : {payload.get('reconciliation_complete')}")
        print(f"compared                : {payload.get('combinations_compared')}"
              f"/{payload.get('combinations_expected')}")
        print(f"mismatched              : {payload.get('combinations_mismatched')}")
        print(f"recorded                : {payload.get('applied')}")
        print(f"detail                  : {payload.get('detail')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
