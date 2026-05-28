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
from typing import Any

# Allow running from repo root without installing the package
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from services.system_status_service import (  # noqa: E402
    build_window_diagnostics,
    db_unavailable_window_payload,
    gather_dataset_window_counts,
)


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

    Delegates to ``services.system_status_service.gather_dataset_window_counts``
    so the script and the ``/api/diagnostics/window-semantics`` endpoint stay
    in lockstep.
    """
    from db.connection import get_conn

    try:
        with get_conn() as conn:
            if conn is None:
                return db_unavailable_window_payload(only_dataset)

            with conn.cursor() as cur:
                return gather_dataset_window_counts(
                    cur, windows=windows, only_dataset=only_dataset
                )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: database error during diagnostics: {exc}", file=sys.stderr)
        return db_unavailable_window_payload(only_dataset)


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
