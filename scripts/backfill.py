"""
scripts/backfill.py

Manual historical backfill CLI entry point for the Logistaas Ads Intelligence System.

Doctrine: Averroes v1.0 | Phase: 1.5 Data Foundation | Roadmap ID: PR-ADS-041

Usage (dry-run, always safe):
    python -m scripts.backfill --source all --from 2025-01-01 --to 2025-12-31 --dry-run
    python -m scripts.backfill --source windsor --from 2025-01-01 --to 2025-12-31 --dry-run
    python -m scripts.backfill --source hubspot --from 2025-01-01 --to 2025-12-31 --dry-run

Usage (execute — writes to local PostgreSQL only):
    python -m scripts.backfill --source windsor --dataset keywords \\
        --from 2025-01-01 --to 2025-01-31 \\
        --execute --confirm I_UNDERSTAND_THIS_WRITES_LOCAL_DB

Rules:
  - Dry-run is the default when neither --dry-run nor --execute is supplied.
  - --execute requires --confirm I_UNDERSTAND_THIS_WRITES_LOCAL_DB.
  - --from must be <= --to.
  - Date ranges > 730 days require --confirm-large-range I_UNDERSTAND_THIS_IS_LARGE.
  - Source/dataset combinations are validated before any action is taken.
  - This script NEVER writes to Google Ads or HubSpot.
  - This script is never called by any scheduler or Render startup command.

Exit codes:
  0 — success (dry-run completed or backfill completed)
  1 — argument / validation error
  2 — missing execute confirmation
  3 — execute mode not yet implemented for this source
"""

import argparse
import sys
from datetime import date, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SOURCES = {"all", "windsor", "hubspot", "gclid"}

WINDSOR_DATASETS  = {"campaigns", "keywords", "geo", "search_terms"}
HUBSPOT_DATASETS  = {"contacts", "deals"}
GCLID_DATASETS    = {"matches"}

MAX_CHUNK_DAYS: dict[str, int] = {
    "windsor": 31,
    "hubspot": 90,
    "gclid":   90,
}

DEFAULT_CHUNK_DAYS = 30
MAX_RANGE_DAYS_BEFORE_CONFIRM = 730

EXECUTE_CONFIRM_VALUE      = "I_UNDERSTAND_THIS_WRITES_LOCAL_DB"
LARGE_RANGE_CONFIRM_VALUE  = "I_UNDERSTAND_THIS_IS_LARGE"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.backfill",
        description=(
            "Manual historical backfill — Logistaas Ads Intelligence System.\n"
            "Dry-run by default. Writes only to local PostgreSQL when --execute is given.\n"
            "NEVER writes to Google Ads or HubSpot."
        ),
    )

    parser.add_argument(
        "--source",
        required=True,
        choices=sorted(VALID_SOURCES),
        help="Data source to backfill (all, windsor, hubspot, gclid).",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        required=True,
        metavar="YYYY-MM-DD",
        help="Backfill start date (inclusive).",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        required=True,
        metavar="YYYY-MM-DD",
        help="Backfill end date (inclusive).",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        metavar="DATASET",
        help=(
            "Specific dataset to backfill (optional). "
            "Windsor: campaigns, keywords, geo, search_terms. "
            "HubSpot: contacts, deals. "
            "GCLID: matches."
        ),
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print planned chunks only — no API calls, no DB writes (default behaviour).",
    )
    mode_group.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        default=False,
        help="Perform local DB writes. Requires --confirm.",
    )

    parser.add_argument(
        "--confirm",
        default=None,
        metavar="TOKEN",
        help=f"Required when --execute is used. Value must be: {EXECUTE_CONFIRM_VALUE}",
    )
    parser.add_argument(
        "--confirm-large-range",
        dest="confirm_large_range",
        default=None,
        metavar="TOKEN",
        help=(
            f"Required when date range exceeds {MAX_RANGE_DAYS_BEFORE_CONFIRM} days. "
            f"Value must be: {LARGE_RANGE_CONFIRM_VALUE}"
        ),
    )
    parser.add_argument(
        "--chunk-days",
        dest="chunk_days",
        type=int,
        default=DEFAULT_CHUNK_DAYS,
        metavar="N",
        help=f"Days per chunk (default {DEFAULT_CHUNK_DAYS}; max 31 for Windsor, 90 for others).",
    )
    parser.add_argument(
        "--limit-chunks",
        dest="limit_chunks",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N chunks (optional safety cap for testing).",
    )

    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _parse_date(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        print(f"ERROR: {flag} '{value}' is not a valid ISO date (YYYY-MM-DD).")
        sys.exit(1)


def _validate_args(args: argparse.Namespace) -> tuple[date, date, list[str]]:
    """
    Validate parsed arguments.  Returns (date_from, date_to, datasets).
    Exits with a non-zero code on any validation failure.
    """
    date_from = _parse_date(args.date_from, "--from")
    date_to   = _parse_date(args.date_to,   "--to")

    if date_from > date_to:
        print("ERROR: --from must be before or equal to --to.")
        sys.exit(1)

    range_days = (date_to - date_from).days + 1
    if range_days > MAX_RANGE_DAYS_BEFORE_CONFIRM:
        if args.confirm_large_range != LARGE_RANGE_CONFIRM_VALUE:
            print(
                f"ERROR: Date range is {range_days} days (>{MAX_RANGE_DAYS_BEFORE_CONFIRM}).\n"
                f"  Add: --confirm-large-range {LARGE_RANGE_CONFIRM_VALUE}"
            )
            sys.exit(1)

    # Determine effective sources
    if args.source == "all":
        effective_sources = ["windsor", "hubspot", "gclid"]
    else:
        effective_sources = [args.source]

    # Validate dataset against source
    if args.dataset is not None:
        dataset_map = {
            "windsor": WINDSOR_DATASETS,
            "hubspot": HUBSPOT_DATASETS,
            "gclid":   GCLID_DATASETS,
        }
        for src in effective_sources:
            allowed = dataset_map.get(src, set())
            if args.dataset not in allowed:
                print(
                    f"ERROR: Dataset '{args.dataset}' is not valid for source '{src}'.\n"
                    f"  Allowed: {', '.join(sorted(allowed))}"
                )
                sys.exit(1)

    # Validate chunk_days
    for src in effective_sources:
        cap = MAX_CHUNK_DAYS.get(src, 90)
        if args.chunk_days > cap:
            print(
                f"ERROR: --chunk-days {args.chunk_days} exceeds the maximum of {cap} "
                f"for source '{src}'."
            )
            sys.exit(1)

    # Determine datasets per source
    if args.dataset is not None:
        datasets_for = {src: [args.dataset] for src in effective_sources}
    else:
        datasets_for = {
            "windsor": sorted(WINDSOR_DATASETS),
            "hubspot": sorted(HUBSPOT_DATASETS),
            "gclid":   sorted(GCLID_DATASETS),
        }

    return date_from, date_to, effective_sources, datasets_for


# ---------------------------------------------------------------------------
# Chunk generation
# ---------------------------------------------------------------------------

def _build_chunks(
    date_from: date,
    date_to: date,
    chunk_days: int,
    limit_chunks: Optional[int],
) -> list[tuple[date, date]]:
    """Split [date_from, date_to] into chunks of at most chunk_days days."""
    chunks = []
    cursor = date_from
    while cursor <= date_to:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), date_to)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
        if limit_chunks is not None and len(chunks) >= limit_chunks:
            break
    return chunks


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------

def build_backfill_plan(
    args: argparse.Namespace,
) -> list[dict]:
    """
    Build a list of backfill plan entries — one per (source, dataset, chunk).
    Does NOT perform any I/O.
    """
    date_from, date_to, sources, datasets_for = _validate_args(args)
    plan = []
    for source in sources:
        datasets = datasets_for.get(source, [])
        for dataset in datasets:
            chunks = _build_chunks(date_from, date_to, args.chunk_days, args.limit_chunks)
            plan.append(
                {
                    "source":   source,
                    "dataset":  dataset,
                    "chunks":   chunks,
                    "warnings": _dataset_warnings(source, dataset),
                }
            )
    return plan


def _dataset_warnings(source: str, dataset: str) -> list[str]:
    """Return known limitation warnings for a given source/dataset pair."""
    warnings = []
    if source == "windsor" and dataset == "search_terms":
        warnings.append(
            "WARNING: Windsor search terms are currently limited by connector "
            "date_preset to last_14d. Historical search-term backfill requires "
            "connector/API verification before it can be trusted."
        )
    if source == "gclid":
        warnings.append(
            "WARNING: GCLID attribution DB table is not available yet. "
            "This dataset is planned for PR-ADS-044."
        )
    return warnings


# ---------------------------------------------------------------------------
# Plan printing
# ---------------------------------------------------------------------------

def print_plan(plan: list[dict]) -> None:
    print()
    print("=" * 70)
    print("  BACKFILL PLAN")
    print("=" * 70)
    print(
        "\n  NOTE: This script writes ONLY to local PostgreSQL when --execute\n"
        "  is used. It NEVER writes to Google Ads or HubSpot.\n"
    )

    if not plan:
        print("  (no datasets selected)")
        print("=" * 70)
        return

    total_chunks = sum(len(entry["chunks"]) for entry in plan)
    print(f"  Total planned chunk operations: {total_chunks}\n")

    for entry in plan:
        source  = entry["source"]
        dataset = entry["dataset"]
        chunks  = entry["chunks"]
        print(f"  Source: {source}  |  Dataset: {dataset}  |  Chunks: {len(chunks)}")
        for i, (cf, ct) in enumerate(chunks, 1):
            print(f"    Chunk {i:3d}: {cf}  →  {ct}")
        for w in entry["warnings"]:
            print(f"\n  ⚠  {w}")
        print()

    print("=" * 70)


# ---------------------------------------------------------------------------
# Confirmation helpers
# ---------------------------------------------------------------------------

def _confirmed_execute(args: argparse.Namespace) -> bool:
    return args.confirm == EXECUTE_CONFIRM_VALUE


# ---------------------------------------------------------------------------
# Execute dispatch
# ---------------------------------------------------------------------------

def run_backfill_plan(plan: list[dict]) -> int:
    """
    Dispatch each plan entry to its source-specific backfill module.
    Only called when --execute is given.
    """
    from scripts.backfill_windsor import run_windsor_backfill
    from scripts.backfill_hubspot import run_hubspot_backfill
    from scripts.backfill_gclid   import run_gclid_backfill

    dispatch = {
        "windsor": run_windsor_backfill,
        "hubspot": run_hubspot_backfill,
        "gclid":   run_gclid_backfill,
    }

    overall_rc = 0
    for entry in plan:
        source  = entry["source"]
        dataset = entry["dataset"]
        chunks  = entry["chunks"]
        fn = dispatch.get(source)
        if fn is None:
            print(f"ERROR: No backfill implementation for source '{source}'.")
            overall_rc = 3
            continue
        rc = fn(dataset=dataset, chunks=chunks)
        if rc != 0:
            print(f"  Source '{source}' / dataset '{dataset}' exited with code {rc}.")
            overall_rc = max(overall_rc, rc)

    return overall_rc


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    # If neither --dry-run nor --execute was supplied, default to dry-run.
    if not args.execute and not args.dry_run:
        args.dry_run = True

    plan = build_backfill_plan(args)
    print_plan(plan)

    if not args.execute:
        print("Dry run only. No API calls. No DB writes.")
        return 0

    if not _confirmed_execute(args):
        print(
            f"ERROR: --execute requires --confirm {EXECUTE_CONFIRM_VALUE}\n"
            f"  Add: --confirm {EXECUTE_CONFIRM_VALUE}"
        )
        return 2

    return run_backfill_plan(plan)


if __name__ == "__main__":
    sys.exit(main())
