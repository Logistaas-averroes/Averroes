"""
scripts/backfill.py

Manual historical backfill CLI entry point for the Logistaas Ads Intelligence System.

Doctrine: Averroes v1.0 | Phase: 1 — Read-Only Intelligence | Roadmap V4.0
Roadmap ID: PR-ADS-071

Usage (dry-run — always safe, no writes):
    python -m scripts.backfill --source all --from 2024-01-01 --to 2026-05-09 --dry-run
    python -m scripts.backfill --source google_ads --from 2024-01-01 --to 2026-05-09 --dry-run
    python -m scripts.backfill --source hubspot --from 2024-01-01 --to 2026-05-09 --dry-run

Usage (local persistence — writes to local PostgreSQL only):
    python -m scripts.backfill --source google_ads --from 2024-01-01 --to 2026-05-09
    python -m scripts.backfill --source hubspot --from 2026-04-01 --to 2026-04-07 --chunk weekly --max-chunks 1

Rules:
  - --from and --to are required. The script fails with a clear message if omitted.
  - --dry-run prints chunks and datasets only; no API calls, no DB writes.
  - Without --dry-run, backfill pulls data and writes to local PostgreSQL only.
  - This script NEVER writes to Google Ads or HubSpot.
  - This script is NEVER called by any scheduler or Render startup command.

Data flow allowed:
  External platforms → read-only pull → local database

Data flow forbidden:
  local analysis → external platform write

Exit codes:
  0 — success (dry-run or backfill completed)
  1 — argument / validation error
"""

import argparse
import calendar
import json
import logging
import os
import sys
from datetime import date, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SOURCES = {"all", "google_ads", "hubspot"}

# Datasets available per effective source key (google_ads maps to Windsor connector)
GOOGLE_ADS_DATASETS = ["campaigns", "keywords", "geo", "search_terms"]
HUBSPOT_DATASETS    = ["contacts", "deals"]

# search_terms cannot be reliably backfilled via Windsor (date_preset limitation)
_SEARCH_TERMS_UNSUPPORTED = (
    "unsupported_by_current_connector: Windsor search_terms use date_preset, "
    "not explicit date_from/date_to. Historical search-term backfill requires "
    "Windsor plan/API verification."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.backfill",
        description=(
            "Manual historical backfill — Logistaas Ads Intelligence System.\n"
            "Pulls data from Windsor (Google Ads) and HubSpot and writes\n"
            "to the local database only.\n"
            "NEVER writes to Google Ads or HubSpot.\n"
            "NEVER called by any scheduler or Render startup command.\n\n"
            "local persistence enabled — no external writes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--source",
        default="all",
        choices=sorted(VALID_SOURCES),
        help=(
            "Data source to backfill: all (default), google_ads, or hubspot. "
            "google_ads pulls from the Windsor.ai connector."
        ),
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        required=True,
        metavar="YYYY-MM-DD",
        help="Backfill start date (inclusive). Required.",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        required=True,
        metavar="YYYY-MM-DD",
        help="Backfill end date (inclusive). Required.",
    )
    parser.add_argument(
        "--chunk",
        dest="chunk",
        default="monthly",
        choices=["monthly", "weekly"],
        help="Chunking strategy: monthly (default) or weekly.",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help=(
            "Print planned chunks and datasets only. "
            "No API calls. No DB writes. "
            "DRY RUN — no local database writes performed."
        ),
    )
    parser.add_argument(
        "--max-chunks",
        dest="max_chunks",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N chunks per dataset (optional safety cap).",
    )
    parser.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        default=False,
        help="Print additional detail per chunk.",
    )

    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _parse_date(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        print(f"ERROR: {flag} '{value}' is not a valid ISO date (YYYY-MM-DD).", flush=True)
        sys.exit(1)


def _validate_args(args: argparse.Namespace) -> tuple[date, date, list[str]]:
    """Return (date_from, date_to, effective_sources). Exits on validation failure."""
    date_from = _parse_date(args.date_from, "--from")
    date_to   = _parse_date(args.date_to,   "--to")

    if date_from > date_to:
        print("ERROR: --from must be before or equal to --to.", flush=True)
        sys.exit(1)

    if args.max_chunks is not None and args.max_chunks < 1:
        print("ERROR: --max-chunks must be >= 1 when provided.", flush=True)
        sys.exit(1)

    if args.source == "all":
        effective_sources = ["google_ads", "hubspot"]
    else:
        effective_sources = [args.source]

    return date_from, date_to, effective_sources


# ---------------------------------------------------------------------------
# Chunk generation
# ---------------------------------------------------------------------------

def build_chunks(
    date_from: date,
    date_to: date,
    chunk: str,
    max_chunks: Optional[int] = None,
) -> list[tuple[date, date]]:
    """Split [date_from, date_to] into calendar-aligned chunks.

    chunk='monthly' — one chunk per calendar month.
    chunk='weekly'  — one chunk per 7 calendar days.

    max_chunks limits the number of chunks returned.
    """
    if chunk not in ("monthly", "weekly"):
        raise ValueError(f"chunk must be 'monthly' or 'weekly', got {chunk!r}")
    if max_chunks is not None and max_chunks < 1:
        raise ValueError("max_chunks must be >= 1 when provided")

    chunks: list[tuple[date, date]] = []
    cursor = date_from

    while cursor <= date_to:
        if chunk == "monthly":
            # End = last day of the current month, capped at date_to
            last_day = calendar.monthrange(cursor.year, cursor.month)[1]
            chunk_end = min(date(cursor.year, cursor.month, last_day), date_to)
        else:
            # weekly — 7-day windows
            chunk_end = min(cursor + timedelta(days=6), date_to)

        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)

        if max_chunks is not None and len(chunks) >= max_chunks:
            break

    return chunks


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------

def _datasets_for_source(source: str) -> list[str]:
    if source == "google_ads":
        return list(GOOGLE_ADS_DATASETS)
    if source == "hubspot":
        return list(HUBSPOT_DATASETS)
    return []


def build_backfill_plan(args: argparse.Namespace) -> list[dict]:
    """Build a list of plan entries (source, dataset, chunks). No I/O."""
    date_from, date_to, sources = _validate_args(args)
    plan = []
    for source in sources:
        for dataset in _datasets_for_source(source):
            chunks = build_chunks(date_from, date_to, args.chunk, args.max_chunks)
            entry: dict = {
                "source":  source,
                "dataset": dataset,
                "chunks":  chunks,
                "notes":   [],
            }
            if source == "google_ads" and dataset == "search_terms":
                entry["notes"].append(_SEARCH_TERMS_UNSUPPORTED)
            plan.append(entry)
    return plan


# ---------------------------------------------------------------------------
# Plan printing
# ---------------------------------------------------------------------------

def print_plan(
    plan: list[dict],
    args: argparse.Namespace,
    date_from: date,
    date_to: date,
) -> None:
    print()
    print("=" * 72)
    print("  LOGISTAAS ADS INTELLIGENCE — HISTORICAL BACKFILL")
    print("=" * 72)
    print(f"  Source:        {args.source}")
    print(f"  Date range:    {date_from}  →  {date_to}")
    print(f"  Chunking:      {args.chunk}")
    print(f"  Mode:          {'DRY RUN — no local database writes performed.' if args.dry_run else 'local persistence enabled'}")
    if args.max_chunks:
        print(f"  Max chunks:    {args.max_chunks} per dataset")
    print()

    if not plan:
        print("  (no datasets selected)")
        print("=" * 72)
        return

    total_chunks = sum(len(e["chunks"]) for e in plan)
    print(f"  Datasets:      {len(plan)}")
    print(f"  Total chunks:  {total_chunks}")
    print()
    print("  Datasets included:")
    for entry in plan:
        print(f"    {entry['source']}/{entry['dataset']}  ({len(entry['chunks'])} chunk(s))")
        for note in entry["notes"]:
            print(f"      ⚠  {note}")
    print()
    print("  NOTE: This backfill reads from Windsor.ai and HubSpot and writes")
    print("  only to the local database. It does not modify Google Ads,")
    print("  HubSpot, bids, budgets, campaigns, contacts, deals, or negative")
    print("  keywords.")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Execute dispatch
# ---------------------------------------------------------------------------

def run_backfill_plan(plan: list[dict], args: argparse.Namespace) -> dict:
    """
    Dispatch each plan entry to its source-specific backfill module.
    Returns a summary dict.
    """
    from scripts.backfill_windsor import run_google_ads_backfill
    from scripts.backfill_hubspot import run_hubspot_backfill

    dispatch = {
        "google_ads": run_google_ads_backfill,
        "hubspot":    run_hubspot_backfill,
    }

    summary: dict = {
        "source":          args.source,
        "date_from":       args.date_from,
        "date_to":         args.date_to,
        "chunk":           args.chunk,
        "dry_run":         args.dry_run,
        "chunks_total":    sum(len(e["chunks"]) for e in plan),
        "chunks_completed": 0,
        "datasets":        {},
        "errors":          [],
    }

    for entry in plan:
        source  = entry["source"]
        dataset = entry["dataset"]
        chunks  = entry["chunks"]

        ds_key = f"{source}/{dataset}"

        fn = dispatch.get(source)
        if fn is None:
            msg = f"No backfill implementation for source '{source}'."
            print(f"  ERROR: {msg}", flush=True)
            summary["errors"].append(msg)
            summary["datasets"][ds_key] = {
                "rows_pulled":  0,
                "rows_written": 0,
                "status":       "error",
                "note":         msg,
            }
            continue

        unsupported_note = next(
            (note for note in entry.get("notes", []) if note.startswith("unsupported_by_current_connector")),
            None,
        )
        if unsupported_note:
            print(f"\n  [{ds_key}]  SKIPPED — {unsupported_note}", flush=True)
            summary["datasets"][ds_key] = {
                "rows_pulled":  0,
                "rows_written": 0,
                "status":       "unsupported_by_current_connector",
                "note":         unsupported_note,
            }
            continue

        ds_result = fn(
            dataset=dataset,
            chunks=chunks,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        summary["datasets"][ds_key] = ds_result
        summary["chunks_completed"] += ds_result.get("chunks_completed", 0)

        if ds_result.get("errors"):
            summary["errors"].extend(ds_result["errors"])

    return summary


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------

def write_summary(summary: dict) -> Optional[str]:
    """Write backfill summary JSON to outputs/ directory if it exists.

    Returns the output path, or None if the directory does not exist.
    """
    outputs_dir = "outputs"
    if not os.path.isdir(outputs_dir):
        return None

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(outputs_dir, f"backfill_summary_{ts}.json")

    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
        return path
    except OSError as exc:
        log.warning("Could not write backfill summary to %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Print summary
# ---------------------------------------------------------------------------

def print_summary(summary: dict) -> None:
    print()
    print("=" * 72)
    print("  BACKFILL SUMMARY")
    print("=" * 72)
    print(f"  Source:            {summary['source']}")
    print(f"  Date range:        {summary['date_from']}  →  {summary['date_to']}")
    print(f"  Chunk strategy:    {summary['chunk']}")
    print(f"  Mode:              {'DRY RUN — no local database writes performed.' if summary['dry_run'] else 'local persistence enabled'}")
    print(f"  Chunks planned:    {summary['chunks_total']}")
    print(f"  Chunks completed:  {summary['chunks_completed']}")
    print()
    print("  Dataset Results:")
    for ds_key, ds in summary["datasets"].items():
        status       = ds.get("status", "unknown")
        rows_pulled  = ds.get("rows_pulled", 0)
        rows_written = ds.get("rows_written", 0)
        print(f"    {ds_key:<35}  pulled={rows_pulled:>6}  written={rows_written:>6}  status={status}")
        if ds.get("note"):
            print(f"      ℹ  {ds['note']}")
    if summary["errors"]:
        print()
        print(f"  Errors ({len(summary['errors'])}):")
        for err in summary["errors"]:
            print(f"    • {err}")
    else:
        print()
        print("  No errors.")
    print("=" * 72)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    date_from, date_to, _ = _validate_args(args)

    plan = build_backfill_plan(args)
    print_plan(plan, args, date_from, date_to)

    if args.dry_run:
        print()
        print("  DRY RUN — no local database writes performed.")
        print()
        return 0

    print()
    print("  Starting backfill — local persistence enabled.", flush=True)
    print()

    summary = run_backfill_plan(plan, args)
    print_summary(summary)

    out_path = write_summary(summary)
    if out_path:
        print(f"\n  Summary written to: {out_path}")

    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
