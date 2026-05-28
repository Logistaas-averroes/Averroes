#!/usr/bin/env python3
"""
scripts/diagnose_window_semantics.py

PR-ADS-095 — Window/Data Diagnostics

Compare per-dataset row counts across 7d / 30d / 60d (or custom) windows and
report each dataset's diagnostic status. Helps operators answer:

  - Do 7d / 30d / 60d windows actually differ for this dataset?
  - Is the row count unavailable because the query failed or because the
    dataset doesn't expose a row-count diagnostic yet?
  - Is the page usable even if the latest sync failed?

Usage:
    python scripts/diagnose_window_semantics.py
    python scripts/diagnose_window_semantics.py --windows 7d,30d,60d,90d
    python scripts/diagnose_window_semantics.py --json
    python scripts/diagnose_window_semantics.py --dataset campaigns

Read-only. No external API calls. No data mutation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from typing import Any

# Allow running from repo root without installing the package
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from services.freshness_service import DATASET_FRESHNESS_CONFIG  # noqa: E402
from services.system_status_service import build_window_diagnostics  # noqa: E402


_VALID_WINDOWS = ("7d", "14d", "30d", "60d", "90d", "365d")


def _parse_windows(raw: str) -> list[str]:
    out: list[str] = []
    for w in raw.split(","):
        w = w.strip()
        if not w:
            continue
        if w not in _VALID_WINDOWS:
            print(
                f"ERROR: Invalid window '{w}'. Valid values: {', '.join(_VALID_WINDOWS)}",
                file=sys.stderr,
            )
            sys.exit(2)
        if w not in out:
            out.append(w)
    return out or ["7d", "30d", "60d"]


def _gather_diagnostics(
    windows: list[str], only_dataset: str | None
) -> dict[str, dict[str, Any]]:
    """Pull per-dataset window counts and sync state from the DB.

    Returns the raw dict expected by ``build_window_diagnostics``.
    """
    from db.connection import get_conn
    from psycopg2 import sql as _psql
    import re

    _SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    window_days: dict[str, int] = {w: int(w.rstrip("d")) for w in windows}

    diagnostics: dict[str, dict[str, Any]] = {}

    def _db_unavailable_payload() -> dict[str, dict[str, Any]]:
        keys = [k for k in DATASET_FRESHNESS_CONFIG if not only_dataset or k == only_dataset]
        return {k: {"db_unavailable": True} for k in keys}

    try:
        with get_conn() as conn:
            if conn is None:
                return _db_unavailable_payload()

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source, dataset, status, last_successful_sync_at, last_source_date "
                    "FROM sync_state"
                )
                sync_rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                sync_map: dict[tuple[str, str], dict] = {}
                for row in sync_rows:
                    r = dict(zip(cols, row))
                    sync_map[(r["source"], r["dataset"])] = r

                for cfg_key, cfg in DATASET_FRESHNESS_CONFIG.items():
                    if only_dataset and cfg_key != only_dataset:
                        continue
                    table_name = str(cfg.get("table") or "")
                    date_column = str(cfg.get("date_column") or "")
                    diag: dict[str, Any] = {
                        "source": cfg.get("source"),
                        "table": table_name or None,
                        "date_column": date_column or None,
                        "window_counts": {},
                        "row_count_available": False,
                    }

                    if not table_name or not date_column or not (
                        _SAFE_IDENT.match(table_name) and _SAFE_IDENT.match(date_column)
                    ):
                        diag["row_count_unavailable_reason"] = (
                            "row-count query not enabled for this dataset "
                            "(missing or non-identifier table/date_column)"
                        )
                    else:
                        diag["row_count_available"] = True
                        for label, ndays in window_days.items():
                            ws = date.today() - timedelta(days=ndays)
                            try:
                                q = _psql.SQL(
                                    "SELECT COUNT(*) FROM {} WHERE {} >= %s"
                                ).format(
                                    _psql.Identifier(table_name),
                                    _psql.Identifier(date_column),
                                )
                                cur.execute(q, (ws,))
                                r2 = cur.fetchone()
                                count = int(r2[0]) if r2 and r2[0] is not None else 0
                                diag["window_counts"][label] = count
                            except Exception:  # noqa: BLE001
                                diag["window_counts"][label] = None
                                diag["row_count_unavailable_reason"] = (
                                    "row-count query failed at execution time"
                                )

                        try:
                            q = _psql.SQL(
                                "SELECT COUNT(*) FROM {} WHERE {} IS NULL"
                            ).format(
                                _psql.Identifier(table_name),
                                _psql.Identifier(date_column),
                            )
                            cur.execute(q)
                            r3 = cur.fetchone()
                            diag["missing_date_rows"] = (
                                int(r3[0]) if r3 and r3[0] is not None else 0
                            )
                        except Exception:  # noqa: BLE001
                            diag["missing_date_rows"] = None

                    sync_row = sync_map.get((cfg.get("source"), cfg.get("dataset")), {})
                    diag["latest_source_date"] = (
                        str(sync_row["last_source_date"])
                        if sync_row.get("last_source_date") else None
                    )
                    diag["latest_sync_status"] = sync_row.get("status")
                    diag["last_successful_sync_at"] = (
                        sync_row["last_successful_sync_at"].isoformat()
                        if sync_row.get("last_successful_sync_at") else None
                    )
                    diagnostics[cfg_key] = diag
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: database error during diagnostics: {exc}", file=sys.stderr)
        return _db_unavailable_payload()

    return diagnostics


def _print_text(payload: dict[str, Any]) -> None:
    print(f"Window/Data Diagnostics  (generated_at={payload['generated_at']})")
    print(f"Windows: {', '.join(payload['windows'])}")
    print("-" * 78)
    for ds in payload["datasets"]:
        print(f"Dataset: {ds['key']}")
        if ds.get("db_unavailable"):
            print("  status: db_unavailable")
            print(f"  reason: {ds.get('reason', '')}")
            print()
            continue
        counts = ds.get("window_counts", {})
        for w in payload["windows"]:
            v = counts.get(w)
            shown = "—" if v is None else str(v)
            print(f"  {w} rows: {shown}")
        print(f"  latest_source_date: {ds.get('latest_source_date') or '—'}")
        print(f"  latest_sync_status: {ds.get('latest_sync_status') or '—'}")
        miss = ds.get("missing_date_rows")
        print(f"  missing_date_rows: {'—' if miss is None else miss}")
        inv = ds.get("invalid_date_rows")
        if inv is not None:
            print(f"  invalid_date_rows: {inv}")
        print(f"  diagnostic_status: {ds.get('diagnostic_status')}")
        print(f"  usable_for_page: {ds.get('usable_for_page')}")
        if ds.get("reason"):
            print(f"  reason: {ds['reason']}")
        if ds.get("next_action"):
            print(f"  next_action: {ds['next_action']}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare per-dataset row counts across windows (PR-ADS-095)."
    )
    parser.add_argument(
        "--windows",
        default="7d,30d,60d",
        help="Comma-separated windows. Valid: " + ", ".join(_VALID_WINDOWS),
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Optional: limit to a single dataset key (e.g. 'campaigns').",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    windows = _parse_windows(args.windows)
    raw = _gather_diagnostics(windows, args.dataset)
    payload = build_window_diagnostics(windows=windows, dataset_diagnostics=raw)

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_text(payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())
