#!/usr/bin/env python3
"""Idempotent operator backfill for the durable keyword_daily_facts table
(PR-ADS-146 §4). Strictly READ-ONLY relative to Google Ads (pulls only) and never
touches the legacy ``keywords`` snapshot table.

Rebuilds recent durable keyword history from the DIRECT Google Ads API and writes
through the natural-key upsert, so re-running the same range updates the same
rows (no duplication). A ``google_ads_api/keyword_facts`` sync batch tracks the
operation, and the run FAILS if rows were fetched but none were durably written.

Usage:
  python scripts/backfill_keyword_daily_facts.py --window 30d
  python scripts/backfill_keyword_daily_facts.py --date-from 2026-05-01 --date-to 2026-07-13
  python scripts/backfill_keyword_daily_facts.py --window 60d --dry-run

Requires DATABASE_URL and Google Ads API credentials. Exit 0 on success, 1 on
failure (fetched-but-not-written / partial persistence).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_WINDOWS = {"30d": 30, "60d": 60, "180d": 180}


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Backfill durable keyword_daily_facts (read-only vs Google Ads).")
    p.add_argument("--window", choices=sorted(_WINDOWS), help="Convenience rolling window (ending today).")
    p.add_argument("--date-from", help="Inclusive start date YYYY-MM-DD.")
    p.add_argument("--date-to", help="Inclusive end date YYYY-MM-DD (default today).")
    p.add_argument("--dry-run", action="store_true", help="Pull + report, but do not write.")
    return p.parse_args(argv)


def _resolve_range(args) -> tuple[date, date]:
    end = date.fromisoformat(args.date_to) if args.date_to else date.today()
    if args.window:
        start = end - timedelta(days=_WINDOWS[args.window] - 1)
    elif args.date_from:
        start = date.fromisoformat(args.date_from)
    else:
        raise SystemExit("Specify --window or --date-from.")
    if start > end:
        raise SystemExit(f"date-from {start} is after date-to {end}.")
    return start, end


def _post_audit(start: date, end: date) -> dict:
    """Read-only post-backfill audit: duplicate keys, identity completeness,
    currency lineage, and source-date coverage vs the requested range."""
    from db.connection import get_conn
    with get_conn() as conn:
        if conn is None:
            return {"available": False}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*)::int,
                  COUNT(*) FILTER (WHERE customer_id IS NULL OR campaign_id IS NULL
                                    OR ad_group_id IS NULL OR criterion_id IS NULL)::int,
                  COUNT(*) FILTER (WHERE COALESCE(source_system,'') <> 'google_ads_api'
                                    OR currency_code IS NULL OR cost_micros IS NULL)::int,
                  MIN(source_date), MAX(source_date)
                FROM keyword_daily_facts
                WHERE source_date >= %s AND source_date <= %s
            """, (start, end))
            rows, missing_ids, unverified_ccy, min_sd, max_sd = cur.fetchone()
            cur.execute("""
                SELECT COUNT(*)::int FROM (
                    SELECT 1 FROM keyword_daily_facts
                    WHERE source_date >= %s AND source_date <= %s
                    GROUP BY source_date, customer_id, campaign_id, ad_group_id, criterion_id
                    HAVING COUNT(*) > 1
                ) d
            """, (start, end))
            (dupes,) = cur.fetchone()
    return {"available": True, "rows": rows, "duplicate_keys": dupes,
            "rows_missing_ids": missing_ids, "rows_unverified_currency": unverified_ccy,
            "source_date_min": str(min_sd) if min_sd else None,
            "source_date_max": str(max_sd) if max_sd else None}


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    start, end = _resolve_range(args)

    from connectors.google_ads_source import pull_keyword_performance_range
    from db import writers as db_writers
    from db.connection import init_pool

    init_pool()
    print(f"── Keyword-fact backfill {start} → {end}{' (dry-run)' if args.dry_run else ''} ──")
    print("Pulling from the direct Google Ads API (read-only)…")
    rows = pull_keyword_performance_range(start.isoformat(), end.isoformat())
    fetched = len(rows or [])
    print(f"Fetched {fetched} keyword rows.")

    missing_currency = sum(1 for r in (rows or []) if not r.get("currency_code"))
    missing_identity = sum(1 for r in (rows or [])
                           if not (r.get("customer_id") and r.get("campaign_id")
                                   and r.get("ad_group_id") and r.get("criterion_id")))
    print(f"Rows missing native currency: {missing_currency}")
    print(f"Rows missing an immutable identity id: {missing_identity}")

    if args.dry_run:
        print("Dry run — no rows written. (Legacy keywords table untouched.)")
        return 0

    batch_id = db_writers.start_sync_batch(
        source="google_ads_api", dataset="keyword_facts", sync_type="backfill",
        date_from=start, date_to=end, run_id=None)
    stats = db_writers.write_keyword_daily_facts(
        run_id=None, keyword_rows=rows, sync_batch_id=batch_id or None)
    print(f"Persistence stats: {stats}")

    partial = (stats["skipped_missing_identity"] + stats["skipped_no_date"]) > 0
    fetched_not_written = fetched > 0 and stats["written"] == 0
    ok = (not stats["db_unavailable"] and not fetched_not_written
          and stats["written"] == stats["prepared"] and not partial)

    if batch_id:
        db_writers.finish_sync_batch(
            batch_id=batch_id,
            status="success" if ok else "failed",
            row_count=stats["written"],
            last_source_date=end,
            error_message=None if ok else f"keyword-fact backfill partial/failed: {stats}")

    audit = _post_audit(start, end)
    print("── Post-backfill audit ──")
    if audit.get("available"):
        print(f"Durable rows in range:       {audit['rows']}")
        print(f"Duplicate natural keys:      {audit['duplicate_keys']} (expected 0)")
        print(f"Rows missing immutable IDs:  {audit['rows_missing_ids']} (expected 0)")
        print(f"Rows unverified currency:    {audit['rows_unverified_currency']}"
              f" ({'complete' if audit['rows_unverified_currency'] == 0 else 'partial'} currency lineage)")
        print(f"Source-date coverage:        {audit['source_date_min']} → {audit['source_date_max']}"
              f" (requested {start} → {end})")
        if audit["duplicate_keys"] or audit["rows_missing_ids"]:
            ok = False

    if fetched_not_written:
        print("FAIL: rows were fetched but none were durably written.")
    print("Result:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
