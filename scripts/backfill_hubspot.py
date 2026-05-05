"""
scripts/backfill_hubspot.py

HubSpot backfill skeleton — Logistaas Ads Intelligence System.
Roadmap ID: PR-ADS-041 | Phase: 1.5 Data Foundation

Supported datasets:
    contacts — HubSpot paid-search contacts (filtered by createdate)
    deals    — Deals pulled through GCLID contact associations

In this PR, execution mode is a safe skeleton only.
Connector rewrites are NOT performed here.
Execute mode exits with code 3 (not yet implemented) to avoid silent data-loss or
incorrect writes.

Dry-run output describes what will happen when the connector is ready.

NEVER writes to HubSpot.
NEVER called by any scheduler or Render startup command.
"""

from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Dataset definitions
# ---------------------------------------------------------------------------

SUPPORTED_DATASETS = frozenset({"contacts", "deals"})

#: Caveats known at time of PR-ADS-041.
_DATASET_NOTES: dict[str, list[str]] = {
    "contacts": [
        "contacts can likely be backfilled via paginated createdate filtering "
        "(HubSpot search API supports gte/lte on createdate).",
        "Large contact lists require offset/cursor pagination; existing connector "
        "pagination logic applies.",
    ],
    "deals": [
        "deals are pulled through GCLID contact associations.",
        "Association pagination caveat remains if still unresolved.",
        "Full deal backfill depends on GCLID coverage in contact records.",
    ],
}


# ---------------------------------------------------------------------------
# Dry-run / planning helpers
# ---------------------------------------------------------------------------

def dataset_notes(dataset: Optional[str] = None) -> list[str]:
    """Return informational notes for dry-run output."""
    if dataset:
        return _DATASET_NOTES.get(dataset, [])
    notes = []
    for ds_notes in _DATASET_NOTES.values():
        notes.extend(ds_notes)
    return notes


def describe_chunks(
    dataset: str,
    chunks: list[tuple[date, date]],
) -> None:
    """Print a human-readable chunk description for a HubSpot dataset."""
    print(f"\n  [hubspot / {dataset}]  {len(chunks)} chunk(s)")
    for i, (cf, ct) in enumerate(chunks, 1):
        print(f"    Chunk {i:3d}: {cf}  →  {ct}")

    for note in dataset_notes(dataset):
        print(f"\n  ℹ  {note}")


# ---------------------------------------------------------------------------
# Execute entry point (skeleton — not yet implemented)
# ---------------------------------------------------------------------------

def run_hubspot_backfill(
    dataset: str,
    chunks: list[tuple[date, date]],
) -> int:
    """
    Execute HubSpot backfill for the given dataset and date chunks.

    Current status (PR-ADS-041):
        Execute mode for HubSpot is a skeleton only.
        The existing HubSpot connector does not yet accept explicit date window
        parameters for contacts or deals.

    Future implementation notes:
        contacts:
            - Use HubSpot CRM search API with createdate gte/lte filters.
            - Paginate using after cursor.
            - Write to leads table using db/writers.py write_leads().
            - Record sync_batches and update sync_state.

        deals:
            - Retrieve deals via GCLID contact associations.
            - Resolve association pagination if not yet addressed.
            - Write to deals table.
            - Record sync_batches and update sync_state.

    Returns:
        3 — not yet implemented (expected in PR-ADS-041)
    """
    print(
        f"\n  [hubspot / {dataset}]  Execute mode is not implemented in PR-ADS-041.\n"
        f"  Connector requires explicit date-window support before safe execution.\n"
        f"  This is planned for a future PR.\n"
        f"  Exit code: 3"
    )
    return 3
