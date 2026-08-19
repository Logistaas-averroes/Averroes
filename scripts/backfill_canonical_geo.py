#!/usr/bin/env python3
"""
scripts/backfill_canonical_geo.py

PR-ADS-153F — operator CLI for the resumable historical canonical-geo bootstrap.

    python -m scripts.backfill_canonical_geo
    python -m scripts.backfill_canonical_geo --json
    python -m scripts.backfill_canonical_geo --date-from 2025-01-01
    python -m scripts.backfill_canonical_geo --no-resume     # deliberate re-fetch
    echo $?      # 0 = history PROVEN covered, 1 = failure, 2 = usage error

Why this exists
---------------
The scheduled geo step refreshes a SEVEN-DAY rolling window, because Google Ads
restates recent spend. That is the right maintenance behaviour and the wrong
bootstrap behaviour: on a fresh ``google_ads_geo_coverage`` ledger, seven days of
proven coverage cannot satisfy ``current_quarter``, ``last_quarter``,
``last_6_months``, ``ytd`` or ``all_time``. Every one of those windows would
correctly report incomplete geo coverage, and the documented post-deploy
verification could never pass from the daily run alone.

So history needs one deliberate, resumable pass — and it needs to be a command,
not a person clicking the admin trigger repeatedly and deciding by eye whether
it finished.

What this does NOT do
---------------------
It contains **no sync logic of its own**. It calls
``services.google_ads_geo_sync_service.run_google_ads_geo_sync``, which is the
same function the scheduler and the manual recovery trigger call, so this shares
one implementation of the lease, the atomic range replacement, the coverage
ledger and the checkpoint. A second copy of that logic would be a second set of
rules to keep in step, which is the defect class this whole programme removes.

What "success" means here
-------------------------
Exit 0 requires ALL of:

  * the run reported ``success`` (no failed chunks, no partial outcome),
  * the durable coverage ledger is READABLE, and
  * it reports the requested range fully covered for THIS customer.

Anything else exits non-zero — partial, failed, skipped because another run
holds the lease, an unreadable ledger, terminal state that could not be
persisted, or a final coverage check that still shows gaps. A bootstrap that
"probably worked" is not evidence, and the windows above will keep blocking
until it demonstrably did.

Read-only against Google Ads (``geographic_view``). Writes ONLY local canonical
tables. Never writes to Google Ads or HubSpot.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _today() -> date:
    return datetime.now(tz=timezone.utc).date()


def run(*, date_from: str | None, date_to: str | None, chunk_months: int,
        resume: bool, dry_run: bool) -> dict:
    """Drive one historical geo bootstrap and verify it against the ledger."""
    from db.connection import init_pool
    from services.google_ads_geo_sync_service import (
        DEFAULT_GEO_SYNC_START, analyze_geo_coverage, configured_customer_id,
        run_google_ads_geo_sync,
    )

    init_pool()

    start = date.fromisoformat(date_from) if date_from else DEFAULT_GEO_SYNC_START
    end = date.fromisoformat(date_to) if date_to else _today()
    if start > end:
        raise ValueError("--date-from must be on or before --date-to")

    customer_id = configured_customer_id()
    job_id = f"geo-bootstrap-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    result = run_google_ads_geo_sync(
        date_from=start.isoformat(), date_to=end.isoformat(),
        dry_run=dry_run, chunk_months=chunk_months, resume=resume, job_id=job_id,
    )

    # Verified independently from the LEDGER, not from the run's own counters: a
    # run that skipped everything because it was already verified is complete,
    # and a run whose own chunks all succeeded is still incomplete if an earlier
    # failure elsewhere in the range was never repaired.
    coverage = ({"available": False, "complete": False, "reason": "dry_run"}
                if dry_run else analyze_geo_coverage(customer_id, start, end))

    summary = result.get("summary") or {}
    status = result.get("status")
    ok = bool(
        status == "success"
        and coverage.get("available")
        and coverage.get("complete")
    )
    return {
        "ok": ok,
        "status": status,
        "reason": result.get("reason"),
        "customer_id": customer_id,
        "job_id": job_id,
        "dry_run": dry_run,
        "resume": resume,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "chunk_months": chunk_months,
        "chunks_verified": summary.get("chunks_verified", 0),
        "chunks_skipped": summary.get("chunks_skipped", 0),
        "chunks_failed": summary.get("chunks_failed", 0),
        "rows_written": summary.get("rows_written", 0),
        "rows_deleted": summary.get("rows_deleted", 0),
        "coverage_available": bool(coverage.get("available")),
        "coverage_complete": bool(coverage.get("complete")),
        "coverage_missing_ranges": coverage.get("missing_ranges", []),
        "coverage_failed_chunks": coverage.get("failed_chunks", []),
        "coverage_reason": coverage.get("reason"),
        "errors": result.get("errors", []),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _render(outcome: dict) -> None:
    print("\n" + "=" * 68)
    print("CANONICAL GEO HISTORICAL BOOTSTRAP")
    print("=" * 68)
    print(f"  customer         : {outcome['customer_id']}")
    print(f"  range            : {outcome['date_from']} .. {outcome['date_to']}")
    print(f"  run status       : {outcome['status']}"
          + (f" ({outcome['reason']})" if outcome.get("reason") else ""))
    print(f"  chunks           : {outcome['chunks_verified']} verified, "
          f"{outcome['chunks_skipped']} already covered, "
          f"{outcome['chunks_failed']} failed")
    print(f"  rows             : {outcome['rows_written']} written, "
          f"{outcome['rows_deleted']} replaced")
    print(f"  ledger readable  : {outcome['coverage_available']}")
    print(f"  ledger complete  : {outcome['coverage_complete']}")
    if outcome["coverage_missing_ranges"]:
        print("  missing ranges   :")
        for r in outcome["coverage_missing_ranges"][:20]:
            print(f"      {r.get('start')} .. {r.get('end')}")
    if outcome["coverage_failed_chunks"]:
        print("  failed chunks    :")
        for c in outcome["coverage_failed_chunks"][:20]:
            print(f"      {c.get('chunk_start')} .. {c.get('chunk_end')}")
    for err in outcome["errors"][:20]:
        print(f"  error            : {err}")
    print("-" * 68)
    if outcome["ok"]:
        print("  RESULT: geo history is PROVEN covered for this customer.")
        print("  The scheduled 7-day refresh maintains recent restatements from here.")
    else:
        print("  RESULT: NOT proven. Re-run this command — completed chunks are")
        print("  skipped, so it resumes from the missing or failed work only.")
    print("=" * 68 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Resumable historical bootstrap for canonical Google Ads geo "
                     "spend. Reads Google Ads read-only; writes only local tables."))
    parser.add_argument("--date-from", default=None,
                        help="ISO start date (default: the canonical Google Ads "
                             "spend history floor)")
    parser.add_argument("--date-to", default=None,
                        help="ISO end date (default: today, UTC)")
    parser.add_argument("--chunk-months", type=int, default=1,
                        help="months per resumable chunk (default: 1)")
    parser.add_argument("--no-resume", action="store_true",
                        help="re-fetch chunks already recorded verified "
                             "(recovery from a suspected bad fetch)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report without writing (never proves coverage)")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    if args.chunk_months < 1:
        print("--chunk-months must be at least 1", file=sys.stderr)
        return 2

    try:
        outcome = run(date_from=args.date_from, date_to=args.date_to,
                      chunk_months=args.chunk_months, resume=not args.no_resume,
                      dry_run=args.dry_run)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"geo bootstrap failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(outcome, indent=2, default=str))
    else:
        _render(outcome)
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
