"""
scripts/backfill_hubspot.py

HubSpot historical backfill — Logistaas Ads Intelligence System.
Roadmap ID: PR-ADS-071 | Phase: 1 — Read-Only Intelligence / Roadmap V4.0

Supported datasets:
    contacts — HubSpot paid-search contacts (filtered by createdate range)
    deals    — Deals associated with GCLID-linked contacts in the range

Required HubSpot field canonical names preserved:
    hs_google_click_id, mql_status, hs_lead_status, lifecyclestage,
    hs_analytics_source, hs_analytics_source_data_1, hs_analytics_source_data_2,
    hs_analytics_first_url, ip_country, mql___mdr_comments, createdate, company

Note on MQL status spelling: DICARDED (one R, not two) is the canonical HubSpot mql_status
value as confirmed from the live Logistaas account. Do not change to DISCARDED — see db/writers.py
_JUNK set which preserves this exact spelling.

NEVER writes to HubSpot.
NEVER called by any scheduler or Render startup command.
"""

import logging
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)

SUPPORTED_DATASETS = frozenset({"contacts", "deals"})


# ---------------------------------------------------------------------------
# Pull helpers
# ---------------------------------------------------------------------------

def _pull_contacts_in_range(date_from: date, date_to: date) -> list:
    """Pull paid-search contacts created in [date_from, date_to]."""
    try:
        from connectors.hubspot_pull import pull_paid_search_contacts_in_range
        return pull_paid_search_contacts_in_range(str(date_from), str(date_to))
    except Exception as exc:  # noqa: BLE001
        log.error("pull_paid_search_contacts_in_range failed (%s→%s): %s", date_from, date_to, exc)
        return []


def _pull_deals_for_contacts(contacts: list) -> list:
    """Pull deals for GCLID-linked contacts (read-only)."""
    try:
        from connectors.hubspot_pull import pull_deals_with_gclid
        return pull_deals_with_gclid(contacts)
    except Exception as exc:  # noqa: BLE001
        log.error("pull_deals_with_gclid failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _write_contacts(run_id: int, contacts: list) -> int:
    try:
        from db.writers import write_leads
        return write_leads(run_id, contacts)
    except Exception as exc:  # noqa: BLE001
        log.error("write_leads failed: %s", exc)
        return 0


def _write_deals(run_id: int, deals: list) -> int:
    try:
        from db.writers import write_deals
        return write_deals(run_id, deals)
    except Exception as exc:  # noqa: BLE001
        log.error("write_deals failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Backfill run record helpers
# ---------------------------------------------------------------------------

def _create_backfill_run() -> Optional[int]:
    try:
        from datetime import datetime, timezone
        from db.writers import write_run
        return write_run({
            "run_type":           "backfill",
            "started_at":         datetime.now(timezone.utc),
            "finished_at":        None,
            "status":             "running",
            "delivery_attempted": False,
            "delivery_success":   None,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not create backfill run record: %s", exc)
        return None


def _finish_backfill_run(run_id: Optional[int], status: str) -> None:
    if run_id is None:
        return
    try:
        from datetime import datetime, timezone
        from db.writers import update_run
        update_run(run_id, {
            "finished_at": datetime.now(timezone.utc),
            "status":      status,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not finish backfill run record (run_id=%s): %s", run_id, exc)


# ---------------------------------------------------------------------------
# Dataset sync key mapping
# ---------------------------------------------------------------------------

_SYNC_DATASET_KEY: dict[str, str] = {
    "contacts": "contacts",
    "deals":    "deals",
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_hubspot_backfill(
    dataset: str,
    chunks: list[tuple[date, date]],
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """Execute HubSpot backfill for the given dataset and date chunks.

    contacts:
        Pulls paid-search contacts created in [chunk_from, chunk_to] via
        hs_analytics_source = PAID_SEARCH and createdate GTE/LTE filters.
        Writes to leads table via write_leads().

    deals:
        Pulls contacts in range, then pulls deals via GCLID contact associations.
        Writes to deals table via write_deals().

    Records a sync_batch for each chunk and updates sync_state on success.
    NEVER writes to HubSpot.
    """
    result: dict = {
        "rows_pulled":      0,
        "rows_written":     0,
        "status":           "success",
        "chunks_completed": 0,
        "errors":           [],
    }

    if dataset not in SUPPORTED_DATASETS:
        msg = f"Unknown dataset '{dataset}' for hubspot source."
        result["status"] = "error"
        result["errors"].append(msg)
        log.error(msg)
        return result

    sync_key = _SYNC_DATASET_KEY.get(dataset, dataset)

    print(f"\n  [hubspot/{dataset}]  {len(chunks)} chunk(s)  {'DRY RUN' if dry_run else 'local persistence enabled'}", flush=True)

    run_id: Optional[int] = None
    if not dry_run:
        run_id = _create_backfill_run()
        if run_id is None:
            log.warning("Proceeding without run_id — DB writes will be skipped by writers.")

    for i, (chunk_from, chunk_to) in enumerate(chunks, 1):
        print(f"    Chunk {i:3d}/{len(chunks)}: {chunk_from}  →  {chunk_to}", end="", flush=True)

        if dry_run:
            print("  [dry-run]", flush=True)
            result["chunks_completed"] += 1
            continue

        # Start sync batch
        batch_id = 0
        try:
            from db.writers import start_sync_batch
            batch_id = start_sync_batch(
                source="hubspot",
                dataset=sync_key,
                sync_type="backfill",
                date_from=chunk_from,
                date_to=chunk_to,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("start_sync_batch failed for chunk %s→%s: %s", chunk_from, chunk_to, exc)

        rows_pulled  = 0
        rows_written = 0
        pull_error: Optional[str] = None

        try:
            if dataset == "contacts":
                contacts = _pull_contacts_in_range(chunk_from, chunk_to)
                rows_pulled = len(contacts)
                if contacts:
                    rows_written = _write_contacts(run_id, contacts)

            elif dataset == "deals":
                # Pull contacts first (to get GCLID-linked contacts), then deals
                contacts = _pull_contacts_in_range(chunk_from, chunk_to)
                deals    = _pull_deals_for_contacts(contacts) if contacts else []
                rows_pulled = len(deals)
                if deals:
                    rows_written = _write_deals(run_id, deals)

        except Exception as exc:  # noqa: BLE001
            pull_error = str(exc)
            log.error("Backfill failed for hubspot/%s %s→%s: %s", dataset, chunk_from, chunk_to, exc)

        result["rows_pulled"]  += rows_pulled
        result["rows_written"] += rows_written

        # Finish sync batch
        if batch_id:
            try:
                from db.writers import finish_sync_batch
                if pull_error:
                    finish_sync_batch(
                        batch_id, "failed",
                        error_message=pull_error[:500],
                    )
                else:
                    finish_sync_batch(
                        batch_id, "success",
                        row_count=rows_written,
                        last_source_date=chunk_to,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("finish_sync_batch failed for batch_id=%s: %s", batch_id, exc)

        if pull_error:
            result["errors"].append(
                f"hubspot/{dataset} {chunk_from}→{chunk_to}: {pull_error}"
            )
            print(f"  ERROR: {pull_error}", flush=True)
        else:
            print(f"  pulled={rows_pulled:>6}  written={rows_written:>6}", flush=True)
            result["chunks_completed"] += 1

    if not dry_run:
        run_status = "success" if not result["errors"] else "failed"
        _finish_backfill_run(run_id, run_status)

    if result["errors"] and result["chunks_completed"] == 0:
        result["status"] = "failed"
    elif result["errors"]:
        result["status"] = "partial"

    return result

