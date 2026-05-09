"""
scripts/backfill_windsor.py

Google Ads (Windsor.ai) historical backfill — Logistaas Ads Intelligence System.
Roadmap ID: PR-ADS-071 | Phase: 1 — Read-Only Intelligence / Roadmap V4.0

Supported datasets:
    campaigns    — Google Ads campaign performance (all campaigns, active and paused)
    keywords     — Keyword-level performance
    geo          — Geographic performance
    search_terms — Unsupported by current connector (date_preset limitation)

NEVER writes to Google Ads.
NEVER called by any scheduler or Render startup command.
"""

import logging
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)

SUPPORTED_DATASETS = frozenset({"campaigns", "keywords", "geo", "search_terms"})

# search_terms cannot be reliably backfilled — Windsor uses date_preset, not date_from/date_to
_SEARCH_TERMS_NOTE = (
    "unsupported_by_current_connector: Windsor search_terms connector uses date_preset "
    "and cannot accept explicit date_from/date_to for historical backfill."
)


# ---------------------------------------------------------------------------
# Per-dataset pull helpers
# ---------------------------------------------------------------------------

def _pull_campaigns(date_from: date, date_to: date) -> list:
    from connectors.windsor_pull import pull_campaign_performance_range
    return pull_campaign_performance_range(str(date_from), str(date_to))


def _pull_keywords(date_from: date, date_to: date) -> list:
    from connectors.windsor_pull import pull_keyword_performance_range
    return pull_keyword_performance_range(str(date_from), str(date_to))


def _pull_geo(date_from: date, date_to: date) -> list:
    from connectors.windsor_pull import pull_geo_performance_range
    return pull_geo_performance_range(str(date_from), str(date_to))


_PULL_FN = {
    "campaigns": _pull_campaigns,
    "keywords":  _pull_keywords,
    "geo":       _pull_geo,
}


# ---------------------------------------------------------------------------
# Per-dataset write helpers
# ---------------------------------------------------------------------------

def _write_campaigns(run_id: int, rows: list) -> int:
    try:
        from db.writers import write_campaigns
        return write_campaigns(run_id, rows)
    except Exception as exc:  # noqa: BLE001
        log.error("write_campaigns failed: %s", exc)
        return 0


def _write_keywords(run_id: int, rows: list) -> int:
    try:
        from db.writers import write_keywords
        return write_keywords(run_id, rows)
    except Exception as exc:  # noqa: BLE001
        log.error("write_keywords failed: %s", exc)
        return 0


def _write_geo(run_id: int, rows: list) -> int:
    try:
        from db.writers import write_geo
        return write_geo(run_id, rows)
    except Exception as exc:  # noqa: BLE001
        log.error("write_geo failed: %s", exc)
        return 0


_WRITE_FN = {
    "campaigns": _write_campaigns,
    "keywords":  _write_keywords,
    "geo":       _write_geo,
}

# Maps dataset name to sync_state dataset key used in sync_batches
_SYNC_DATASET_KEY = {
    "campaigns": "campaigns",
    "keywords":  "keywords",
    "geo":       "geo",
}


# ---------------------------------------------------------------------------
# Backfill run record helpers
# ---------------------------------------------------------------------------

def _create_backfill_run() -> Optional[int]:
    """Create a 'backfill' run record in the runs table and return its id."""
    try:
        from datetime import datetime, timezone
        from db.writers import write_run
        run_id = write_run({
            "run_type":            "backfill",
            "started_at":          datetime.now(timezone.utc),
            "finished_at":         None,
            "status":              "running",
            "delivery_attempted":  False,
            "delivery_success":    None,
        })
        return run_id
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
# Main entry point
# ---------------------------------------------------------------------------

def run_google_ads_backfill(
    dataset: str,
    chunks: list[tuple[date, date]],
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """Execute Google Ads (Windsor) backfill for the given dataset and date chunks.

    For search_terms: returns immediately with unsupported_by_current_connector status.
    For campaigns/keywords/geo: calls Windsor connector with explicit date_from/date_to,
    writes results via db/writers.py, and records sync_batches for each chunk.

    Returns a result dict with rows_pulled, rows_written, status, chunks_completed, errors.
    NEVER writes to Google Ads.
    """
    result: dict = {
        "rows_pulled":       0,
        "rows_written":      0,
        "status":            "success",
        "chunks_completed":  0,
        "errors":            [],
    }

    if dataset == "search_terms":
        result["status"] = "unsupported_by_current_connector"
        result["note"]   = _SEARCH_TERMS_NOTE
        print(f"\n  [google_ads/search_terms]  SKIPPED — {_SEARCH_TERMS_NOTE}", flush=True)
        return result

    if dataset not in SUPPORTED_DATASETS:
        msg = f"Unknown dataset '{dataset}' for google_ads source."
        result["status"] = "error"
        result["errors"].append(msg)
        log.error(msg)
        return result

    pull_fn  = _PULL_FN.get(dataset)
    write_fn = _WRITE_FN.get(dataset)
    sync_key = _SYNC_DATASET_KEY.get(dataset, dataset)

    if pull_fn is None or write_fn is None:
        msg = f"No pull/write function registered for dataset '{dataset}'."
        result["status"] = "error"
        result["errors"].append(msg)
        return result

    print(f"\n  [google_ads/{dataset}]  {len(chunks)} chunk(s)  {'DRY RUN' if dry_run else 'local persistence enabled'}", flush=True)

    # Create one backfill run record to scope all writes for this dataset invocation
    run_id: Optional[int] = None
    if not dry_run:
        run_id = _create_backfill_run()
        if run_id is None:
            msg = f"Failed to create backfill run for google_ads/{dataset}; aborting chunk processing."
            result["status"] = "failed"
            result["errors"].append(msg)
            log.error(msg)
            return result

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
                source="windsor",
                dataset=sync_key,
                sync_type="backfill",
                date_from=chunk_from,
                date_to=chunk_to,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("start_sync_batch failed for chunk %s→%s: %s", chunk_from, chunk_to, exc)

        # Pull data
        rows: list = []
        pull_error: Optional[str] = None
        persistence_error: Optional[str] = None
        persisted = False
        try:
            rows = pull_fn(chunk_from, chunk_to)
        except Exception as exc:  # noqa: BLE001
            pull_error = str(exc)
            log.error("Pull failed for %s %s→%s: %s", dataset, chunk_from, chunk_to, exc)

        rows_pulled  = len(rows)
        rows_written = 0

        if pull_error is None and rows:
            rows_written = write_fn(run_id, rows)
        if pull_error is None:
            from scheduler.sync_utils import persistence_succeeded

            persisted = persistence_succeeded(rows, rows_written)
            if not persisted:
                persistence_error = (
                    "Persistence failed or incomplete: "
                    f"pulled={len(rows)}, written={rows_written}"
                )

        result["rows_pulled"]  += rows_pulled
        result["rows_written"] += rows_written

        # Finish sync batch
        if batch_id:
            try:
                from db.writers import finish_sync_batch
                from scheduler.sync_utils import max_source_date
                if pull_error:
                    finish_sync_batch(
                        batch_id, "failed",
                        error_message=pull_error[:500],
                    )
                elif persisted:
                    finish_sync_batch(
                        batch_id, "success",
                        row_count=rows_written,
                        last_source_date=max_source_date(rows, fallback_date=chunk_to),
                    )
                else:
                    finish_sync_batch(
                        batch_id,
                        "failed",
                        row_count=rows_written,
                        error_message=persistence_error[:500],
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("finish_sync_batch failed for batch_id=%s: %s", batch_id, exc)

        if pull_error:
            result["errors"].append(
                f"{dataset} {chunk_from}→{chunk_to}: {pull_error}"
            )
            print(f"  ERROR: {pull_error}", flush=True)
        elif persistence_error:
            result["errors"].append(
                f"{dataset} {chunk_from}→{chunk_to}: {persistence_error}"
            )
            print(f"  ERROR: {persistence_error}", flush=True)
        else:
            print(f"  pulled={rows_pulled:>6}  written={rows_written:>6}", flush=True)
            result["chunks_completed"] += 1

    # Finish the backfill run record
    if not dry_run:
        run_status = "success" if not result["errors"] else "failed"
        _finish_backfill_run(run_id, run_status)

    if result["errors"] and result["chunks_completed"] == 0:
        result["status"] = "failed"
    elif result["errors"]:
        result["status"] = "partial"

    return result
