"""
scripts/diagnose_window_semantics.py

PR-ADS-095 — Window/Data Diagnostics Script

Compares 7d/30d/60d row counts for all datasets to determine whether
data windows actually produce different results and whether datasets
are usable despite sync failures.

Run:
    python scripts/diagnose_window_semantics.py

Phase 1 — Read Only. No external writes. No data mutation.
"""

from __future__ import annotations

import sys
import os
from datetime import date, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


WINDOWS = {"7d": 7, "30d": 30, "60d": 60}

DATASETS = [
    "campaigns",
    "search_terms",
    "keywords",
    "geo",
    "leads",
    "deals",
    "gclid_attribution",
    "gclid_coverage_snapshots",
    "waste_terms",
    "ngrams",
    "historical_intelligence",
]


def run_diagnostics() -> None:
    """Run window semantics diagnostics against the database."""
    try:
        from services.freshness_service import DATASET_FRESHNESS_CONFIG
        from db.connection import get_conn
        from psycopg2 import sql as psql
    except ImportError as e:
        print(f"[ERROR] Cannot import required modules: {e}")
        print("Make sure you are running from the project root with dependencies installed.")
        sys.exit(1)

    try:
        conn = get_conn()
    except Exception as e:
        print(f"[ERROR] Cannot connect to database: {e}")
        print("Set DATABASE_URL or check your .env configuration.")
        sys.exit(1)

    if conn is None:
        print("[ERROR] Database connection returned None.")
        sys.exit(1)

    # Get sync state
    sync_map: dict[tuple[str, str], dict] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source, dataset, status, last_successful_sync_at, last_source_date
                FROM sync_state
                ORDER BY source, dataset
            """)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                sync_map[(r["source"], r["dataset"])] = r
    except Exception as e:
        print(f"[WARNING] Could not read sync_state: {e}")

    print("=" * 70)
    print("  WINDOW/DATA DIAGNOSTICS — PR-ADS-095")
    print("=" * 70)
    print()

    for dataset_key in DATASETS:
        cfg = DATASET_FRESHNESS_CONFIG.get(dataset_key)
        if not cfg:
            print(f"Dataset: {dataset_key}")
            print("  [SKIP] No configuration found")
            print()
            continue

        table_name = cfg.get("table", "")
        date_column = cfg.get("date_column", "")
        source = cfg.get("source", "")
        ds_name = cfg.get("dataset", dataset_key)

        print(f"Dataset: {dataset_key}")
        print(f"  Source: {source}")
        print(f"  Table: {table_name}.{date_column}")

        # Row counts per window
        for wlabel, wdays in WINDOWS.items():
            window_start = date.today() - timedelta(days=wdays)
            try:
                with conn.cursor() as cur:
                    query = psql.SQL("SELECT COUNT(*) FROM {} WHERE {} >= %s").format(
                        psql.Identifier(table_name),
                        psql.Identifier(date_column),
                    )
                    cur.execute(query, (window_start,))
                    result_row = cur.fetchone()
                    count = int(result_row[0]) if result_row and result_row[0] is not None else 0
                    print(f"  {wlabel} rows: {count:,}")
            except Exception as e:
                print(f"  {wlabel} rows: [ERROR] {e}")

        # Latest source date
        try:
            with conn.cursor() as cur:
                query = psql.SQL("SELECT MAX({}) FROM {}").format(
                    psql.Identifier(date_column),
                    psql.Identifier(table_name),
                )
                cur.execute(query)
                result_row = cur.fetchone()
                latest_date = result_row[0] if result_row else None
                print(f"  latest_source_date: {latest_date}")
        except Exception as e:
            print(f"  latest_source_date: [ERROR] {e}")

        # Sync status
        sync_row = sync_map.get((source, ds_name), {})
        sync_status = sync_row.get("status", "unknown")
        print(f"  latest_sync_status: {sync_status}")

        # Determine usability
        try:
            with conn.cursor() as cur:
                query = psql.SQL("SELECT COUNT(*) FROM {} WHERE {} >= %s").format(
                    psql.Identifier(table_name),
                    psql.Identifier(date_column),
                )
                cur.execute(query, (date.today() - timedelta(days=60),))
                result_row = cur.fetchone()
                max_rows = int(result_row[0]) if result_row and result_row[0] is not None else 0
        except Exception:
            max_rows = 0

        if sync_status == "failed" and max_rows > 0:
            status = "data_available_latest_sync_failed"
            usable = True
        elif sync_status == "failed" and max_rows == 0:
            status = "failed_no_data"
            usable = False
        elif max_rows > 0:
            status = "fresh_with_data"
            usable = True
        elif sync_status is None or sync_status == "unknown":
            status = "not_run"
            usable = False
        else:
            status = "unknown"
            usable = False

        print(f"  usable_for_page: {usable}")
        print(f"  status: {status}")
        print()

    print("=" * 70)
    print("  DIAGNOSTICS COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    run_diagnostics()
