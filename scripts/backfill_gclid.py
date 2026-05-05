"""
scripts/backfill_gclid.py

GCLID attribution backfill skeleton — Logistaas Ads Intelligence System.
Roadmap ID: PR-ADS-041 | Phase: 1.5 Data Foundation

Supported datasets:
    matches — GCLID attribution matches (future; table not yet available)

This file is planning-only for PR-ADS-041.
The gclid_attribution table does not exist yet (planned for PR-ADS-044).
Execute mode is not implemented and exits with code 3.

NEVER called by any scheduler or Render startup command.
"""

from datetime import date


# ---------------------------------------------------------------------------
# Dataset definitions
# ---------------------------------------------------------------------------

SUPPORTED_DATASETS = frozenset({"matches"})

_TABLE_AVAILABLE = False  # gclid_attribution table planned for PR-ADS-044


# ---------------------------------------------------------------------------
# Dry-run / planning helpers
# ---------------------------------------------------------------------------

def describe_chunks(
    dataset: str,
    chunks: list[tuple[date, date]],
) -> None:
    """Print a human-readable chunk description for GCLID dataset."""
    print(f"\n  [gclid / {dataset}]  {len(chunks)} chunk(s)")
    for i, (cf, ct) in enumerate(chunks, 1):
        print(f"    Chunk {i:3d}: {cf}  →  {ct}")
    print(
        "\n  ⚠  GCLID attribution DB table is not available yet. "
        "This dataset is planned for PR-ADS-044."
    )


# ---------------------------------------------------------------------------
# Execute entry point (skeleton — not yet implemented)
# ---------------------------------------------------------------------------

def run_gclid_backfill(
    dataset: str,
    chunks: list[tuple[date, date]],
) -> int:
    """
    Execute GCLID attribution backfill for the given dataset and date chunks.

    Current status (PR-ADS-041):
        The gclid_attribution table does not exist yet (planned for PR-ADS-044).
        Execute mode is not implemented.

    Future implementation notes:
        - Requires gclid_attribution table (PR-ADS-044).
        - Match recorded GCLIDs from leads table against Windsor click data.
        - Record sync_batches and update sync_state.

    Returns:
        3 — not yet implemented (expected in PR-ADS-041)
    """
    print(
        f"\n  [gclid / {dataset}]  Execute mode is not implemented in PR-ADS-041.\n"
        f"  The gclid_attribution table does not exist yet (planned for PR-ADS-044).\n"
        f"  Exit code: 3"
    )
    return 3
