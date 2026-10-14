#!/usr/bin/env python3
"""
scripts/backfill_deal_company_associations.py

PR-ADS-161D2 — historical deal→company association backfill for every
canonical won deal.

    python -m scripts.backfill_deal_company_associations --json            # dry run
    python -m scripts.backfill_deal_company_associations --apply --json
    python -m scripts.backfill_deal_company_associations --resume --apply --json
    python -m scripts.backfill_deal_company_associations --restart --apply  # deliberate

Modes
-----
**Dry run (default).** Reads the won population from the local ledger (READ ONLY
transaction) and each deal's companies from HubSpot. Writes NOTHING — no
association, no observation, no sync batch, no run record.

**--apply.** Opens a backfill GENERATION anchored at its start instant and
observes every won deal. A deal is done when it has a successful observation
started at or after that anchor — so completion is PROVEN from stored
observations, never inferred from "some rows exist".

**--resume --apply.** Continues the in-progress generation: only deals not yet
done are read. The anchor does not move, so nothing already proven is re-read.

**--restart --apply.** Deliberately opens a NEW generation and re-reads every
deal. Never chosen automatically — a plain ``--apply`` refuses to run over an
in-progress generation rather than silently starting again.

Stops visibly at the FIRST lookup or persistence failure, with the failed deal
left as the checkpoint. A failed lookup is recorded as a failed attempt and
preserves any earlier evidence for that deal.

Exit codes
    0  dry run: every deal read successfully | apply: generation PROVEN complete
    1  stopped — a failure, the --max-lookups checkpoint, or a refused start
    2  usage error, or the database / won population is unavailable

Guarantees: HubSpot READ-ONLY (GET); writes only local PostgreSQL (and nothing
in a dry run); no Google Ads call; bounded by --max-lookups. Output carries
counts, statuses and deal ids only — no company names, contact names, emails or
GCLIDs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_UNAVAILABLE = 2

DEFAULT_MAX_LOOKUPS = 2000


def _run_summary(result: dict) -> dict:
    """The non-sensitive facts of one service run."""
    keys = ("status", "population", "attempted", "succeeded", "failed",
            "applied", "paths_written", "skipped_stale", "write_failures",
            "remaining", "complete", "truncated", "stopped_on_failure",
            "shapes", "failure_reasons", "failed_deal_ids", "error")
    return {k: result.get(k) for k in keys}


def dry_run(*, max_lookups: int, resume: bool) -> dict:
    from db import deal_company_association_repository as repo
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )

    out = {"mode": "dry_run", "ok": False, "reason": None,
           "database_writes_performed": False,
           "hubspot_writes_performed": False,
           "google_ads_calls_performed": False}
    deal_ids = None
    if resume:
        state = repo.fetch_sync_state()
        row = (state.get("row") or {}) if state.get("available") else None
        if row is None:
            out.update(reason="sync_state_unreadable", exit_code=EXIT_UNAVAILABLE)
            return out
        if row.get("backfill_status") != repo.BACKFILL_IN_PROGRESS:
            out.update(reason="no_backfill_in_progress_to_resume",
                       exit_code=EXIT_STOPPED)
            return out
        progress = repo.fetch_backfill_progress(row["backfill_started_at"])
        if not progress.get("available"):
            out.update(reason="progress_unreadable", exit_code=EXIT_UNAVAILABLE)
            return out
        deal_ids = progress["remaining"]
        out["generation_started_at"] = row["backfill_started_at"]
    result = sync_deal_company_associations(
        deal_ids=deal_ids, mode="backfill", max_lookups=max_lookups,
        dry_run=True, record_run=False)
    out["run"] = _run_summary(result)
    if result.get("population") is None:
        out.update(reason=result.get("error") or "population_unreadable",
                   exit_code=EXIT_UNAVAILABLE)
        return out
    out["ok"] = bool(result.get("complete"))
    out["reason"] = ("every_deal_read" if out["ok"]
                     else result.get("error") or "not_every_deal_read")
    out["exit_code"] = EXIT_OK if out["ok"] else EXIT_STOPPED
    return out


def apply(*, max_lookups: int, resume: bool, restart: bool) -> dict:
    import db.writers as writers
    from db import deal_company_association_repository as repo
    from services.dataset_keys import (
        DEAL_COMPANY_ASSOCIATION_DATASET, DEAL_COMPANY_ASSOCIATION_SOURCE,
    )
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )

    out = {"mode": "apply", "ok": False, "reason": None, "resume": resume,
           "restart": restart, "hubspot_writes_performed": False,
           "google_ads_calls_performed": False}
    state = repo.fetch_sync_state()
    if not state.get("available"):
        out.update(reason="sync_state_unreadable", exit_code=EXIT_UNAVAILABLE)
        return out
    row = state.get("row") or {}
    in_progress = row.get("backfill_status") == repo.BACKFILL_IN_PROGRESS

    if resume:
        if not in_progress:
            out.update(reason="no_backfill_in_progress_to_resume",
                       exit_code=EXIT_STOPPED)
            return out
        generation = row["backfill_started_at"]
    else:
        if in_progress and not restart:
            # Never restart silently over a generation that has made progress.
            out.update(reason=("backfill_in_progress: rerun with --resume "
                               "--apply, or --restart --apply to deliberately "
                               "start over"),
                       generation_started_at=row.get("backfill_started_at"),
                       exit_code=EXIT_STOPPED)
            return out
        # The anchor is compared with observation starts, which use the
        # DATABASE clock — so the anchor must too, or a host running ahead
        # would leave every deal pending forever.
        clock = repo.db_now()
        if not clock.get("available"):
            out.update(reason="database_clock_unavailable",
                       exit_code=EXIT_UNAVAILABLE)
            return out
        generation = clock["now"]
        opened = repo.start_backfill_generation(generation)
        if not opened.get("available"):
            out.update(reason=f"generation_not_recorded:{opened.get('error')}",
                       exit_code=EXIT_UNAVAILABLE)
            return out
        generation = generation.isoformat()
    out["generation_started_at"] = generation

    before = repo.fetch_backfill_progress(generation)
    if not before.get("available"):
        out.update(reason="progress_unreadable", exit_code=EXIT_UNAVAILABLE)
        return out
    out["before"] = {k: before.get(k) for k in ("population", "done")}
    out["before"]["remaining"] = len(before["remaining"])

    # No durable batch → no HubSpot call.
    batch_id = writers.start_sync_batch(
        source=DEAL_COMPANY_ASSOCIATION_SOURCE,
        dataset=DEAL_COMPANY_ASSOCIATION_DATASET, sync_type="backfill")
    if not batch_id:
        out.update(reason="sync_batch_not_opened", exit_code=EXIT_STOPPED)
        return out
    out["batch_id"] = batch_id

    result = sync_deal_company_associations(
        batch_id=batch_id, deal_ids=before["remaining"], mode="backfill",
        max_lookups=max_lookups, stop_on_failure=True)
    out["run"] = _run_summary(result)
    writers.finish_sync_batch(
        batch_id=batch_id, status=result.get("status") or "failed",
        row_count=result.get("applied") or 0,
        error_message=result.get("error"))

    after = repo.fetch_backfill_progress(generation)
    if not after.get("available"):
        out.update(reason="progress_unreadable_after_run",
                   exit_code=EXIT_UNAVAILABLE)
        return out
    out["after"] = {"population": after["population"], "done": after["done"],
                    "remaining": len(after["remaining"])}

    if after["remaining"]:
        if result.get("stopped_on_failure"):
            reason = f"stopped_on_failure: {result.get('error')}"
        elif result.get("truncated"):
            reason = (f"checkpoint: --max-lookups={max_lookups} reached with "
                      f"{len(after['remaining'])} deal(s) remaining")
        else:
            reason = (f"{len(after['remaining'])} deal(s) still lack a "
                      f"successful observation in this generation")
        out.update(reason=reason + " — rerun with --resume --apply",
                   exit_code=EXIT_STOPPED)
        return out

    # Every won deal has a success at or after the anchor: PROVEN complete.
    done = repo.complete_backfill_generation(generation,
                                             population=after["population"])
    if not (done.get("available") and done.get("completed")):
        out.update(reason=("every deal is observed but the generation could "
                           "not be marked complete (restarted elsewhere, or "
                           "the state write failed)"),
                   exit_code=EXIT_STOPPED)
        return out
    out.update(ok=True, reason="backfill_generation_complete",
               exit_code=EXIT_OK)
    return out


def _render(out: dict) -> None:
    print("=" * 74)
    print("PR-ADS-161D2 — DEAL→COMPANY ASSOCIATION BACKFILL "
          f"({out.get('mode')})")
    print("=" * 74)
    for key in ("generation_started_at", "before", "after", "batch_id"):
        if key in out:
            print(f"    {key:<26} {out[key]}")
    run = out.get("run") or {}
    for key in ("status", "population", "attempted", "succeeded", "failed",
                "applied", "paths_written", "skipped_stale", "write_failures",
                "shapes", "failure_reasons", "failed_deal_ids", "error"):
        if key in run:
            print(f"    {key:<26} {run[key]}")
    print("-" * 74)
    print(f"    {'OK' if out.get('ok') else 'NOT OK'} — {out.get('reason')}")
    print("    HubSpot read-only. No Google Ads call. "
          + ("No database write (dry run)." if out.get("mode") == "dry_run"
             else "Local PostgreSQL writes only."))
    print("=" * 74)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill HubSpot deal→company associations for every "
                    "canonical won deal (read-only against HubSpot; dry run "
                    "unless --apply)")
    parser.add_argument("--apply", action="store_true",
                        help="Write to local PostgreSQL. Without it, nothing "
                             "is written.")
    parser.add_argument("--resume", action="store_true",
                        help="Continue the in-progress backfill generation.")
    parser.add_argument("--restart", action="store_true",
                        help="DELIBERATELY open a new generation and re-read "
                             "every deal. Never needed to recover from an "
                             "error — use --resume.")
    parser.add_argument("--max-lookups", type=int, default=DEFAULT_MAX_LOOKUPS,
                        help=f"HubSpot lookups in this run (default "
                             f"{DEFAULT_MAX_LOOKUPS}); reaching it stops at a "
                             f"resumable checkpoint.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (same exit code)")
    args = parser.parse_args(argv)

    if args.max_lookups < 1:
        print("--max-lookups must be at least 1.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    if args.resume and args.restart:
        print("--resume and --restart are mutually exclusive.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    if args.restart and not args.apply:
        print("--restart only applies with --apply.", file=sys.stderr)
        return EXIT_UNAVAILABLE

    from db.connection import ensure_database_ready

    ready, detail = ensure_database_ready()
    if not ready:
        out = {"ok": False, "reason": f"database_unavailable: {detail}",
               "exit_code": EXIT_UNAVAILABLE}
    else:
        try:
            out = (apply(max_lookups=args.max_lookups, resume=args.resume,
                         restart=args.restart) if args.apply
                   else dry_run(max_lookups=args.max_lookups,
                                resume=args.resume))
        except Exception as exc:  # noqa: BLE001
            out = {"ok": False, "reason": f"backfill could not run: "
                   f"{type(exc).__name__}: {str(exc)[:200]}",
                   "exit_code": EXIT_STOPPED}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        _render(out)
    return int(out.get("exit_code", EXIT_STOPPED))


if __name__ == "__main__":
    raise SystemExit(main())
