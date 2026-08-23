#!/usr/bin/env python3
"""
scripts/backfill_canonical_spend_fx.py

PR-ADS-154B §1 — operator CLI for the resumable canonical spend + FX coverage repair.

    python -m scripts.backfill_canonical_spend_fx --from 2026-01-01 --to 2026-06-22
    python -m scripts.backfill_canonical_spend_fx --from 2026-01-01 --to 2026-06-22 --json
    python -m scripts.backfill_canonical_spend_fx --window current_quarter
    python -m scripts.backfill_canonical_spend_fx --from 2026-01-01 --to 2026-03-31 --restart
    echo $?     # 0 = coverage PROVEN complete, 1 = still incomplete, 2 = usage error

Why this exists
---------------
The scheduled run refreshes campaign spend and FX over a SEVEN-DAY rolling
window, because Google Ads restates recent spend. Correct for maintenance,
useless as a bootstrap: seven proven days can never make ``current_quarter`` or
``ytd`` complete, so ``campaign_coverage_incomplete`` and
``fx_coverage_incomplete`` persist however many times the daily sync succeeds —
which is exactly what production shows.

Canonical geo got this treatment in PR-ADS-153F. Spend and FX did not: their only
repair paths were two HTTP admin endpoints, which cannot be driven from a Render
shell as one resumable operation and report that they RAN rather than that
coverage is now COMPLETE.

What this does NOT do
---------------------
No ingestion logic of its own, and none in this file at all: it calls
``services.canonical_coverage_repair_service.repair_canonical_spend_and_fx``,
which in turn calls the same backfill and FX functions the scheduler uses. One
implementation of each, so a fix in either reaches every caller.

Google Ads is the source for campaign spend. **Windsor is not involved in any
path here.**

What "success" means here
-------------------------
Exit 0 requires BOTH, re-read from the durable ledgers after the work:

  * campaign-spend coverage complete for this customer over the requested range,
    with no unresolved failed chunks, and
  * an FX rate for every canonical spend date in that range.

Not "the backfill returned success". A repair whose success criterion is "no
exception was raised" is how a window stays broken while every signal says the
job worked. ``--dry-run`` therefore NEVER exits 0: it writes nothing, so it can
prove nothing.

Reads Google Ads and the FX provider read-only. Writes ONLY local canonical
tables. Never writes to Google Ads, HubSpot or Mailchimp.
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


def run(*, date_from: str | None, date_to: str | None, window: str | None,
        chunk_months: int, resume: bool, dry_run: bool) -> dict:
    """Resolve the range, repair it, and return the verified outcome."""
    from db.connection import init_pool
    from db import revenue_repository as repo
    from services.canonical_coverage_repair_service import (
        CoverageRepairInputError, repair_canonical_spend_and_fx,
    )
    from services.google_ads_spend_service import (
        DEFAULT_SPEND_BACKFILL_START, _window_bounds,
    )

    # The scheduler proved in PR-ADS-154 that assuming an initialized pool is how
    # a run spends external quota to produce nothing. Same rule here.
    init_pool()

    if window:
        # Resolved through the SAME `_window_bounds` the geo reconciliation uses,
        # in the SAME account time zone. A repair that covered a range differing
        # by even one boundary day from the one the gate measures would report
        # success against a window nobody checks.
        _resolved, start, end = _window_bounds(
            window, None, repo.fetch_account_time_zone())
        if start is None:                       # all_time
            start = DEFAULT_SPEND_BACKFILL_START
    else:
        start = date.fromisoformat(date_from) if date_from else DEFAULT_SPEND_BACKFILL_START
        end = date.fromisoformat(date_to) if date_to else _today()

    try:
        outcome = repair_canonical_spend_and_fx(
            date_from=start, date_to=end, resume=resume,
            chunk_months=chunk_months, dry_run=dry_run)
    except CoverageRepairInputError as exc:
        raise ValueError(str(exc)) from exc

    outcome["window"] = window
    outcome["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
    return outcome


def _render(outcome: dict) -> None:
    cov = outcome["coverage"]
    spend = outcome["spend_backfill"]
    print("\n" + "=" * 68)
    print("CANONICAL SPEND + FX COVERAGE REPAIR")
    print("=" * 68)
    print(f"  customer         : {outcome['customer_id'] or '(not configured — account-wide)'}")
    print(f"  range            : {outcome['date_from']} .. {outcome['date_to']}"
          + (f"  (window={outcome['window']})" if outcome.get("window") else ""))
    print(f"  bounds           : {outcome['bounds']}")
    print(f"  mode             : {'dry run' if outcome['dry_run'] else 'repair'}"
          f", {'resume' if outcome['resume'] else 'restart'}")
    print("-" * 68)
    print(f"  spend backfill   : {spend['status']} — "
          f"{spend['chunks_verified']} chunk(s) verified, "
          f"{spend['chunks_skipped_already_verified']} already covered, "
          f"{spend['chunks_failed']} failed, {spend['rows_written']} row(s) written")
    if spend["retried_failed_chunks"]:
        print("  retried failures :")
        for c in spend["retried_failed_chunks"][:20]:
            print(f"      {c.get('chunk_start')} .. {c.get('chunk_end')}  -> {c.get('status')}")
    print(f"  fx               : {outcome['fx']['rows_written']} rate(s) written, "
          f"{outcome['fx']['rates_fetched']} fetched")
    print("-" * 68)
    print(f"  campaign coverage: readable={cov['campaign_coverage_available']} "
          f"complete={cov['campaign_coverage_complete']}")
    for r in cov["campaign_missing_ranges"][:20]:
        print(f"      missing   {r.get('start')} .. {r.get('end')}")
    for c in cov["campaign_failed_chunks"][:20]:
        print(f"      failed    {c.get('chunk_start')} .. {c.get('chunk_end')}")
    for c in cov["campaign_superseded_failed_chunks"][:20]:
        print(f"      (repaired) {c.get('chunk_start')} .. {c.get('chunk_end')}")
    print(f"  fx coverage      : readable={cov['fx_coverage_available']} "
          f"complete={cov['fx_coverage_complete']} "
          f"({cov['fx_covered_days']}/{cov['fx_spend_days']} spend days)")
    for d in cov["fx_missing_dates"][:20]:
        print(f"      no rate   {d}")
    for err in outcome["errors"][:20]:
        print(f"  error            : {err}")
    print("-" * 68)
    if cov["ok"]:
        print("  RESULT: campaign spend and FX coverage are PROVEN complete for")
        print("  this range. Geo reconciliation can now be trusted for windows")
        print("  inside it; run the incremental sync to re-evaluate it.")
    elif outcome["dry_run"]:
        print("  RESULT: dry run — nothing was written, so nothing is proven.")
        print("  Re-run without --dry-run to repair.")
    else:
        print("  RESULT: coverage is still INCOMPLETE. Re-run this command —")
        print("  verified chunks are skipped, so it resumes from the gaps only.")
        print("  If the same range keeps failing, the Google Ads API is refusing")
        print("  it; the errors above carry the reason.")
    print("=" * 68 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Resumable repair of canonical Google Ads campaign-spend and FX "
                     "coverage. Reads Google Ads and the FX provider read-only; "
                     "writes only local canonical tables."))
    parser.add_argument("--from", dest="date_from", default=None,
                        help="ISO start date, INCLUSIVE (default: the canonical "
                             "Google Ads spend history floor)")
    parser.add_argument("--to", dest="date_to", default=None,
                        help="ISO end date, INCLUSIVE (default: today, UTC)")
    parser.add_argument("--window", default=None,
                        help="business window key to repair instead of explicit "
                             "dates (current_quarter, last_quarter, last_6_months, "
                             "ytd, all_time) — resolved in the account time zone")
    parser.add_argument("--chunk-months", type=int, default=1,
                        help="months per resumable chunk (default: 1)")
    parser.add_argument("--restart", action="store_true",
                        help="re-fetch the whole range, including ranges already "
                             "recorded verified (recovery from a suspected bad fetch)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report without writing; never exits 0, because a run "
                             "that writes nothing proves nothing")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    if args.chunk_months < 1:
        print("--chunk-months must be at least 1", file=sys.stderr)
        return 2
    if args.window and (args.date_from or args.date_to):
        print("--window cannot be combined with --from/--to", file=sys.stderr)
        return 2

    try:
        outcome = run(date_from=args.date_from, date_to=args.date_to,
                      window=args.window, chunk_months=args.chunk_months,
                      resume=not args.restart, dry_run=args.dry_run)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        # Never interpolate a raw exception into operator output: a connection
        # failure carries the DSN, and a DSN carries the password (PR-ADS-154A).
        from db.writers import safe_db_error
        print(f"coverage repair failed: {safe_db_error(exc)}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(outcome, indent=2, default=str))
    else:
        _render(outcome)

    return 0 if outcome["coverage"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
