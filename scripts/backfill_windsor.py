"""
scripts/backfill_windsor.py

Windsor backfill skeleton — Logistaas Ads Intelligence System.
Roadmap ID: PR-ADS-041 | Phase: 1.5 Data Foundation

Supported datasets:
    campaigns    — Google Ads campaign performance
    keywords     — Keyword-level performance
    geo          — Geographic performance
    search_terms — Search term report (limitation: connector confirmed only up to last_14d)

In this PR, execution mode is a safe skeleton only.
Connector rewrites are NOT performed here.
Execute mode exits with code 3 (not yet implemented) to avoid silent data-loss or
incorrect writes.

NEVER writes to Google Ads.
NEVER called by any scheduler or Render startup command.
"""

from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Dry-run / planning helpers (called by scripts/backfill.py in all modes)
# ---------------------------------------------------------------------------

#: Datasets this module handles.
SUPPORTED_DATASETS = frozenset({"campaigns", "keywords", "geo", "search_terms"})

#: Datasets that are confirmed to accept explicit date ranges via the Windsor connector.
#: search_terms is excluded because the connector uses a date_preset, not date_from/date_to.
CONNECTOR_DATE_RANGE_READY = frozenset({"campaigns", "keywords", "geo"})


def dataset_warnings(dataset: Optional[str] = None) -> list[str]:
    """
    Return known limitation strings for the given Windsor dataset (or all if None).
    Used by the planner and dry-run output.
    """
    warnings = []
    targets = {dataset} if dataset else SUPPORTED_DATASETS

    if "search_terms" in targets:
        warnings.append(
            "WARNING: Windsor search terms are currently limited by connector "
            "date_preset to last_14d.\n"
            "  Historical search-term backfill requires Windsor API/plan verification.\n"
            "  Do not assume search_terms backfill will return data older than 14 days."
        )

    return warnings


def describe_chunks(
    dataset: str,
    chunks: list[tuple[date, date]],
) -> None:
    """Print a human-readable chunk description for a Windsor dataset."""
    print(f"\n  [windsor / {dataset}]  {len(chunks)} chunk(s)")
    for i, (cf, ct) in enumerate(chunks, 1):
        print(f"    Chunk {i:3d}: {cf}  →  {ct}")

    for w in dataset_warnings(dataset):
        print(f"\n  ⚠  {w}")


# ---------------------------------------------------------------------------
# Execute entry point (skeleton — not yet implemented)
# ---------------------------------------------------------------------------

def run_windsor_backfill(
    dataset: str,
    chunks: list[tuple[date, date]],
) -> int:
    """
    Execute Windsor backfill for the given dataset and date chunks.

    Current status (PR-ADS-041):
        Execute mode for Windsor is a skeleton only.
        The existing Windsor connector uses date_preset parameters.
        A connector rewrite to accept explicit date_from / date_to is required
        before safe per-chunk execution can be implemented.

    Future implementation notes:
        - For campaigns / keywords / geo: call the Windsor connector with
          explicit date_from and date_to once the connector supports it.
        - Write results using db/writers.py per-dataset writers.
        - Record a sync_batches row (status 'success' or 'failed').
        - Update sync_state watermark.
        - search_terms: verify Windsor plan supports date ranges > 14 days
          before implementing.

    Returns:
        3 — not yet implemented (expected in PR-ADS-041)
    """
    print(
        f"\n  [windsor / {dataset}]  Execute mode is not implemented in PR-ADS-041.\n"
        f"  Connector rewrite required to support explicit date_from / date_to.\n"
        f"  This is planned for a future PR.\n"
        f"  Exit code: 3"
    )
    return 3
