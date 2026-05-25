#!/usr/bin/env python3
"""
scripts/audit_production_reality.py

Read-only production reality audit diagnostic.

Connects to the PostgreSQL database and reports row counts, freshness status,
and verdict for every major dataset table.  Never writes, never calls external
APIs, never triggers schedulers.

Usage:
    python scripts/audit_production_reality.py --days 60 --pretty
    python scripts/audit_production_reality.py --days 60 --json

PR-ADS-064 — Full Production Reality Audit & UX Navigation Diagnosis
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)
_CONN_UNSET = object()

# ─── Configuration ──────────────────────────────────────────────────────────

# Tables to audit with their date column for time-range queries.
TABLES: dict[str, str] = {
    "runs": "started_at",
    "campaigns": "run_date",
    "leads": "run_date",
    "deals": "run_date",
    "keywords": "run_date",
    "geo": "run_date",
    "search_terms": "source_date",
    "waste_terms": "run_date",
    "gclid_attribution": "created_at",
    "gclid_coverage_snapshots": "snapshot_date",
}

# sync_state key mapping for audited tables.
# Keys use "source/dataset" to match sync_state UNIQUE(source, dataset).
TABLE_TO_SYNC_DATASET_KEY: dict[str, str | None] = {
    "runs": None,
    "campaigns": "windsor/campaigns",
    "leads": "hubspot/contacts",
    "deals": "hubspot/deals",
    "keywords": "windsor/keywords",
    "geo": "windsor/geo",
    "search_terms": "windsor/search_terms",
    "waste_terms": None,
    "gclid_attribution": "gclid/matches",
    "gclid_coverage_snapshots": "gclid/coverage",
}

# External source for each dataset (informational only).
DATASET_SOURCE: dict[str, str] = {
    "runs": "internal",
    "campaigns": "windsor+hubspot",
    "leads": "hubspot",
    "deals": "hubspot",
    "keywords": "windsor",
    "geo": "windsor",
    "search_terms": "windsor",
    "waste_terms": "analysis",
    "gclid_attribution": "gclid_match",
    "gclid_coverage_snapshots": "gclid_match",
}

# Last sync type mapping (which scheduler populates this).
DATASET_SYNC_TYPE: dict[str, str] = {
    "runs": "all",
    "campaigns": "weekly/monthly/incremental",
    "leads": "weekly/daily/incremental",
    "deals": "weekly/incremental",
    "keywords": "weekly/incremental",
    "geo": "weekly/incremental",
    "search_terms": "weekly/daily",
    "waste_terms": "weekly",
    "gclid_attribution": "weekly",
    "gclid_coverage_snapshots": "weekly",
}

# Freshness thresholds in days — if latest date is older than this, it's stale.
STALE_THRESHOLD_DAYS: dict[str, int] = {
    "runs": 2,
    "campaigns": 8,
    "leads": 3,
    "deals": 8,
    "keywords": 8,
    "geo": 8,
    "search_terms": 8,
    "waste_terms": 8,
    "gclid_attribution": 8,
    "gclid_coverage_snapshots": 8,
}

# Datasets that depend on search_terms being populated.
SEARCH_TERMS_DEPENDENTS = {"waste_terms", "ngrams"}

# ─── Verdict logic (exported for testing) ───────────────────────────────────


class Verdict:
    OK = "OK"
    RUNNING = "RUNNING"
    FRESH_BUT_EMPTY = "FRESH_BUT_EMPTY"
    STALE = "STALE"
    EMPTY_VALID = "EMPTY_VALID"
    MISSING_TABLE = "MISSING_TABLE"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    BROKEN = "BROKEN"


def compute_verdict(
    *,
    table: str,
    rows_in_window: int,
    latest_date: Any | None,
    sync_status: str | None,
    days: int,
    stale_threshold: int | None = None,
) -> tuple[str, str]:
    """Compute a (verdict, reason) tuple for a dataset.

    Args:
        table: Table name.
        rows_in_window: Row count within the time window.
        latest_date: Latest date value in the table (date or datetime).
        sync_status: Status from sync_state (success/failed/running/unknown/None).
        days: Time window in days.
        stale_threshold: Days after which dataset is stale. If None, use default.

    Returns:
        Tuple of (verdict_str, reason_str).
    """
    if stale_threshold is None:
        stale_threshold = STALE_THRESHOLD_DAYS.get(table, 8)

    if sync_status == "running":
        if rows_in_window > 0:
            return (Verdict.RUNNING, "Sync currently running; partial data may still be loading")
        return (Verdict.RUNNING, "Sync currently running and table has zero rows so far")

    # Fresh sync but zero rows → suspicious.
    if rows_in_window == 0 and sync_status in ("success", "fresh"):
        return (
            Verdict.FRESH_BUT_EMPTY,
            f"Sync status says '{sync_status}' but table has zero rows in {days}d window",
        )

    # Zero rows with no sync record or unknown status → valid empty.
    if rows_in_window == 0 and sync_status in (None, "unknown"):
        return (
            Verdict.EMPTY_VALID,
            "No sync record found and table is empty — likely not yet populated",
        )

    # Zero rows with failed sync → broken.
    if rows_in_window == 0 and sync_status == "failed":
        return (
            Verdict.BROKEN,
            "Last sync failed and table has zero rows",
        )

    # Has rows — check staleness.
    if rows_in_window > 0 and latest_date is not None:
        from datetime import date as _date  # noqa: PLC0415

        if hasattr(latest_date, "date"):
            latest_as_date = latest_date.date()
        elif isinstance(latest_date, _date):
            latest_as_date = latest_date
        else:
            latest_as_date = None

        if latest_as_date is not None:
            age_days = (_date.today() - latest_as_date).days
            if age_days > stale_threshold:
                return (
                    Verdict.STALE,
                    f"Latest date is {age_days}d old (threshold: {stale_threshold}d)",
                )

    # Has rows and is fresh.
    if rows_in_window > 0:
        return (Verdict.OK, "Data present and within freshness threshold")

    return (Verdict.BROKEN, "Unexpected state")


def compute_pipeline_blockers(
    dataset_results: dict[str, dict],
    *,
    db_available: bool = True,
) -> list[dict[str, str]]:
    """Identify pipeline blockers based on dataset verdicts.

    If search_terms is empty/broken, downstream pages (N-Grams, Waste Terms)
    are blocked.

    Returns list of {page, blocker} dicts.
    """
    blockers: list[dict[str, str]] = []

    if not db_available:
        blockers.append({
            "page": "System",
            "blocker": "Database unavailable — audit data cannot be trusted",
        })

    st = dataset_results.get("search_terms", {})
    st_verdict = st.get("verdict", "")

    if st_verdict in (
        Verdict.FRESH_BUT_EMPTY,
        Verdict.MISSING_TABLE,
        Verdict.BROKEN,
        Verdict.STALE,
        Verdict.DB_UNAVAILABLE,
    ):
        blockers.append({
            "page": "Search Terms",
            "blocker": f"search_terms table: {st_verdict} — {st.get('reason', 'unknown')}",
        })
        blockers.append({
            "page": "N-Grams",
            "blocker": "Depends on search_terms table which is empty/broken",
        })
        blockers.append({
            "page": "Waste Terms",
            "blocker": "Depends on waste detection over search_terms which is empty/broken",
        })

    wt = dataset_results.get("waste_terms", {})
    wt_verdict = wt.get("verdict", "")
    if wt_verdict in (Verdict.FRESH_BUT_EMPTY,) and st_verdict == Verdict.OK:
        blockers.append({
            "page": "Waste Terms",
            "blocker": "search_terms has data but waste_terms is empty — waste detection may not have run",
        })

    return blockers


# ─── Database queries ───────────────────────────────────────────────────────


def _get_connection():
    """Get a psycopg2 connection from DATABASE_URL. Returns None if unavailable."""
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2  # noqa: PLC0415
        conn = psycopg2.connect(url)
        conn.set_session(readonly=True)
        return conn
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to connect to database: %s", exc)
        return None


def _table_exists(cur, table: str) -> bool:
    """Check if a table exists in the current database."""
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
        (table,),
    )
    return cur.fetchone()[0]


def _existing_tables(cur) -> set[str]:
    """Return all table names in the current schema."""
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    return {row[0] for row in cur.fetchall()}


def _count_rows(cur, table: str, date_col: str, days: int) -> int:
    """Count rows in table within the last N days."""
    from psycopg2 import sql  # noqa: PLC0415

    query = sql.SQL("SELECT COUNT(*) FROM {} WHERE {} >= CURRENT_DATE - INTERVAL %s").format(
        sql.Identifier(table),
        sql.Identifier(date_col),
    )
    cur.execute(query, (f"{days} days",))
    return cur.fetchone()[0]


def _latest_date(cur, table: str, date_col: str):
    """Get the maximum date value from a table."""
    from psycopg2 import sql  # noqa: PLC0415

    query = sql.SQL("SELECT MAX({}) FROM {}").format(
        sql.Identifier(date_col),
        sql.Identifier(table),
    )
    cur.execute(query)
    result = cur.fetchone()
    return result[0] if result else None


def _get_sync_state(cur, existing_tables: set[str] | None = None) -> dict[str, dict]:
    """Fetch sync_state rows as a dict keyed by source/dataset."""
    if existing_tables is not None and "sync_state" not in existing_tables:
        return {}
    if existing_tables is None and not _table_exists(cur, "sync_state"):
        return {}
    cur.execute(
        """
        SELECT source, dataset, status, last_successful_sync_at, last_source_date, error_message
        FROM sync_state
        ORDER BY source, dataset
        """
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    result = {}
    for row in rows:
        r = dict(zip(cols, row))
        result[f"{r['source']}/{r['dataset']}"] = r
    return result


def _get_latest_run(cur, existing_tables: set[str] | None = None) -> dict | None:
    """Get the most recent run record."""
    if existing_tables is not None and "runs" not in existing_tables:
        return None
    if existing_tables is None and not _table_exists(cur, "runs"):
        return None
    cur.execute(
        "SELECT run_type, status, started_at, finished_at FROM runs ORDER BY started_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


# ─── Main audit logic ──────────────────────────────────────────────────────


def run_audit(days: int = 60, conn=_CONN_UNSET) -> dict[str, Any]:
    """Run the full production reality audit. Returns structured result dict."""
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "latest_run": None,
        "datasets": {},
        "sync_state": {},
        "pipeline_blockers": [],
        "db_available": False,
    }

    owns_connection = conn is _CONN_UNSET
    if conn is _CONN_UNSET:
        conn = _get_connection()

    if conn is None:
        # Mark all as DB_UNAVAILABLE.
        for table in TABLES:
            result["datasets"][table] = {
                "dataset": table,
                "rows_7d": 0,
                "rows_14d": 0,
                "rows_30d": 0,
                "rows_60d": 0,
                "latest_date": None,
                "freshness_status": "unknown",
                "source": DATASET_SOURCE.get(table, "unknown"),
                "last_sync_type": DATASET_SYNC_TYPE.get(table, "unknown"),
                "last_sync_status": None,
                "verdict": Verdict.DB_UNAVAILABLE,
                "reason": "Database connection unavailable",
            }
        result["pipeline_blockers"] = compute_pipeline_blockers(
            result["datasets"],
            db_available=False,
        )
        return result

    result["db_available"] = True

    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        existing_tables = _existing_tables(cur)

        # Get latest run.
        latest_run = _get_latest_run(cur, existing_tables)
        if latest_run:
            result["latest_run"] = {
                "run_type": latest_run["run_type"],
                "status": latest_run["status"],
                "started_at": latest_run["started_at"].isoformat() if latest_run["started_at"] else None,
                "finished_at": latest_run["finished_at"].isoformat() if latest_run["finished_at"] else None,
            }

        # Get sync state.
        sync_state = _get_sync_state(cur, existing_tables)
        result["sync_state"] = {
            k: {
                "source": v.get("source"),
                "dataset": v.get("dataset"),
                "status": v.get("status"),
                "last_successful_sync_at": v["last_successful_sync_at"].isoformat() if v.get("last_successful_sync_at") else None,
                "last_source_date": str(v["last_source_date"]) if v.get("last_source_date") else None,
            }
            for k, v in sync_state.items()
        }

        # Audit each table.
        for table, date_col in TABLES.items():
            if table not in existing_tables:
                result["datasets"][table] = {
                    "dataset": table,
                    "rows_7d": 0,
                    "rows_14d": 0,
                    "rows_30d": 0,
                    "rows_60d": 0,
                    "latest_date": None,
                    "freshness_status": "unknown",
                    "source": DATASET_SOURCE.get(table, "unknown"),
                    "last_sync_type": DATASET_SYNC_TYPE.get(table, "unknown"),
                    "last_sync_status": None,
                    "verdict": Verdict.MISSING_TABLE,
                    "reason": f"Table '{table}' does not exist in the database",
                }
                continue

            # Row counts for standard windows.
            rows_7d = _count_rows(cur, table, date_col, 7)
            rows_14d = _count_rows(cur, table, date_col, 14)
            rows_30d = _count_rows(cur, table, date_col, 30)
            rows_60d = _count_rows(cur, table, date_col, 60)

            # Latest date.
            latest_dt = _latest_date(cur, table, date_col)

            # Sync status for this dataset.
            sync_key = TABLE_TO_SYNC_DATASET_KEY.get(table)
            sync_info = sync_state.get(sync_key, {}) if sync_key else {}
            sync_status = sync_info.get("status") if sync_info else None

            # Use the requested window for verdict.
            rows_in_window = _count_rows(cur, table, date_col, days) if days not in (7, 14, 30, 60) else {
                7: rows_7d, 14: rows_14d, 30: rows_30d, 60: rows_60d,
            }.get(days, rows_60d)

            verdict, reason = compute_verdict(
                table=table,
                rows_in_window=rows_in_window,
                latest_date=latest_dt,
                sync_status=sync_status,
                days=days,
            )

            latest_date_str = None
            if latest_dt is not None:
                if hasattr(latest_dt, "isoformat"):
                    latest_date_str = latest_dt.isoformat()
                else:
                    latest_date_str = str(latest_dt)

            result["datasets"][table] = {
                "dataset": table,
                "rows_7d": rows_7d,
                "rows_14d": rows_14d,
                "rows_30d": rows_30d,
                "rows_60d": rows_60d,
                "latest_date": latest_date_str,
                "freshness_status": sync_status or "unknown",
                "source": DATASET_SOURCE.get(table, "unknown"),
                "last_sync_type": DATASET_SYNC_TYPE.get(table, "unknown"),
                "last_sync_status": sync_status,
                "sync_dataset_key": sync_key,
                "verdict": verdict,
                "reason": reason,
            }

        cur.close()
    except Exception as exc:  # noqa: BLE001
        log.error("Audit query error: %s", exc)
        result["error"] = str(exc)
    finally:
        if owns_connection:
            conn.close()

    result["pipeline_blockers"] = compute_pipeline_blockers(
        result["datasets"],
        db_available=result["db_available"],
    )
    return result


# ─── Output formatters ──────────────────────────────────────────────────────


def format_pretty(audit: dict[str, Any]) -> str:
    """Format audit result as pretty console output."""
    lines: list[str] = []
    lines.append("")
    lines.append("PRODUCTION REALITY AUDIT")
    lines.append("=" * 60)
    lines.append("")

    # Latest run.
    run = audit.get("latest_run")
    if run:
        lines.append(f"Latest run: {run['run_type']} · {run['status']} · {run.get('started_at', 'unknown')}")
    else:
        lines.append("Latest run: No run records found")
    lines.append("")

    # DB status.
    if not audit.get("db_available"):
        lines.append("⚠️  Database unavailable — cannot perform audit")
        lines.append("")
        return "\n".join(lines)

    # Dataset checks.
    lines.append("Dataset checks:")
    lines.append("-" * 60)
    for table, info in audit.get("datasets", {}).items():
        verdict = info.get("verdict", "UNKNOWN")
        icon = {
            "OK": "✅",
            "RUNNING": "🏃",
            "FRESH_BUT_EMPTY": "⚠️ ",
            "STALE": "⏰",
            "EMPTY_VALID": "🔲",
            "MISSING_TABLE": "❌",
            "DB_UNAVAILABLE": "🔌",
            "BROKEN": "💔",
        }.get(verdict, "❓")

        latest = info.get("latest_date", "none")
        if latest and len(str(latest)) > 10:
            latest = str(latest)[:10]

        lines.append(
            f"  {icon} {table:<28} rows_60d={info.get('rows_60d', 0):<6} "
            f"latest={latest or 'none':<12} {verdict}"
        )

    lines.append("")

    # Pipeline blockers.
    blockers = audit.get("pipeline_blockers", [])
    if blockers:
        lines.append("Pipeline Blockers:")
        lines.append("-" * 60)
        for b in blockers:
            lines.append(f"  🚫 {b['page']}: {b['blocker']}")
        lines.append("")

    # Sync state summary.
    sync_state = audit.get("sync_state", {})
    if sync_state:
        lines.append("Sync State (from sync_state table):")
        lines.append("-" * 60)
        for ds, info in sync_state.items():
            lines.append(
                f"  {ds}: "
                f"status={info.get('status', '?')} "
                f"last_sync={info.get('last_successful_sync_at', 'never')}"
            )
        lines.append("")

    return "\n".join(lines)


# ─── CLI entry point ────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only production reality audit for Logistaas Ads Intelligence",
    )
    parser.add_argument(
        "--days", type=int, default=60,
        help="Time window in days for row counts and freshness (default: 60)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output as JSON",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Output as pretty-printed console summary",
    )
    args = parser.parse_args()

    # Default to pretty if neither specified.
    if not args.json_output and not args.pretty:
        args.pretty = True

    logging.basicConfig(level=logging.WARNING)

    audit = run_audit(days=args.days)

    if args.json_output:
        print(json.dumps(audit, indent=2, default=str))
    if args.pretty:
        print(format_pretty(audit))


if __name__ == "__main__":
    main()
