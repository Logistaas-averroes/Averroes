"""
scheduler/incremental_sync.py

PR-ADS-073 — Daily Incremental Sync at 9 AM Asia/Amman

Responsibility:
  Run a rolling-window incremental sync for all major datasets.

  This is NOT the daily pulse (scheduler/daily.py) which runs anomaly
  detection and CRM delta checks.  This module's sole job is to pull
  fresh data from Windsor.ai and HubSpot and persist it to the local DB
  so that dashboard pages stay up to date.

Doctrine:
  - Read-only from external platforms (Windsor, HubSpot).
  - Writes only to the local database.
  - Never modifies Google Ads campaigns, bids, budgets, keywords, or
    negative keywords.
  - Never modifies HubSpot contacts or deals.
  - Never triggers OCT uploads or external ad-platform mutations.

Data flow:
  External platforms → read-only pull → local DB → freshness metadata

Public entry point:
  run_daily_incremental_sync(
      *,
      lookback_days_ads: int = 14,
      lookback_days_hubspot_contacts: int = 14,
      lookback_days_hubspot_deals: int = 30,
      run_reason: str = "scheduled",
  ) -> dict
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db.writers as db_writers
from scheduler.sync_utils import max_source_date, persistence_succeeded

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading — reads sync.daily_incremental from config/thresholds.yaml.
# Falls back to hard-coded defaults if config is unavailable at import time.
# ---------------------------------------------------------------------------

_CONFIG_FALLBACK_ADS = 14
_CONFIG_FALLBACK_HUBSPOT_CONTACTS = 14
_CONFIG_FALLBACK_HUBSPOT_DEALS = 30


def _load_sync_config() -> dict:
    """Load sync.daily_incremental from config/thresholds.yaml.

    Returns the lookback_days sub-dict.  Falls back to hard-coded defaults
    if the config file is unavailable or the key is missing, so the module
    always has safe values regardless of deployment state.
    """
    try:
        import yaml  # noqa: PLC0415
        config_path = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        return cfg.get("sync", {}).get("daily_incremental", {}).get("lookback_days", {})
    except Exception:  # noqa: BLE001
        return {}


_SYNC_CONFIG = _load_sync_config()

DEFAULT_LOOKBACK_ADS = int(_SYNC_CONFIG.get("ads", _CONFIG_FALLBACK_ADS))
DEFAULT_LOOKBACK_HUBSPOT_CONTACTS = int(
    _SYNC_CONFIG.get("hubspot_contacts", _CONFIG_FALLBACK_HUBSPOT_CONTACTS)
)
DEFAULT_LOOKBACK_HUBSPOT_DEALS = int(
    _SYNC_CONFIG.get("hubspot_deals", _CONFIG_FALLBACK_HUBSPOT_DEALS)
)

# search_terms uses preset-only querying (no explicit date range supported
# by Windsor.ai connector); document as such and skip reliably.
_SEARCH_TERMS_NOTE = (
    "unsupported_by_current_connector: Windsor search_terms endpoint uses "
    "date_preset only; explicit date-range incremental sync is not supported. "
    "Dataset skipped to avoid false freshness."
)

# gclid/matches: no DB persistence path exists yet for incremental sync.
_GCLID_NOTE = (
    "unsupported_by_current_connector: no incremental-sync DB persistence path "
    "for gclid/matches. Dataset skipped."
)


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def run_daily_incremental_sync(
    *,
    lookback_days_ads: int = DEFAULT_LOOKBACK_ADS,
    lookback_days_hubspot_contacts: int = DEFAULT_LOOKBACK_HUBSPOT_CONTACTS,
    lookback_days_hubspot_deals: int = DEFAULT_LOOKBACK_HUBSPOT_DEALS,
    run_reason: str = "scheduled",
) -> dict:
    """Run daily rolling-window incremental sync for all major datasets.

    Syncs:
      - windsor/campaigns   (explicit date range)
      - windsor/keywords    (explicit date range)
      - windsor/geo         (explicit date range)
      - windsor/search_terms — skipped; connector does not support explicit
                               date ranges reliably (documents as unsupported)
      - hubspot/contacts    (explicit date range via createdate filter)
      - hubspot/deals       (via GCLID contacts pulled in the deals window)
      - gclid/matches       — skipped; no incremental DB path yet

    Each dataset failure is isolated: one failed dataset does not abort the
    rest.  The returned summary includes per-dataset status and an ``errors``
    list for datasets that raised unexpected exceptions.

    Returns a structured summary dict.  The caller (scheduler wrapper or
    manual trigger) is responsible for logging.

    NEVER writes to Google Ads, HubSpot, or any external platform.
    """
    now = datetime.now(tz=timezone.utc)
    started_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now.date()

    date_from_ads = today - timedelta(days=lookback_days_ads)
    date_from_contacts = today - timedelta(days=lookback_days_hubspot_contacts)
    date_from_deals = today - timedelta(days=lookback_days_hubspot_deals)

    datasets: dict[str, dict] = {}
    errors: list[str] = []

    log.info(
        "[incremental_sync] started reason=%s ads_lookback=%d "
        "contacts_lookback=%d deals_lookback=%d",
        run_reason, lookback_days_ads,
        lookback_days_hubspot_contacts, lookback_days_hubspot_deals,
    )

    # ── windsor/campaigns ────────────────────────────────────────────────────
    datasets["windsor/campaigns"] = _sync_windsor_campaigns(
        date_from=date_from_ads, date_to=today, errors=errors,
    )

    # ── windsor/keywords ─────────────────────────────────────────────────────
    datasets["windsor/keywords"] = _sync_windsor_keywords(
        date_from=date_from_ads, date_to=today, errors=errors,
    )

    # ── windsor/geo ──────────────────────────────────────────────────────────
    datasets["windsor/geo"] = _sync_windsor_geo(
        date_from=date_from_ads, date_to=today, errors=errors,
    )

    # ── windsor/search_terms — unsupported for reliable incremental sync ──────
    datasets["windsor/search_terms"] = {
        "status": "skipped",
        "note": _SEARCH_TERMS_NOTE,
    }
    log.info("[incremental_sync] windsor/search_terms: %s", _SEARCH_TERMS_NOTE)

    # ── hubspot/contacts ──────────────────────────────────────────────────────
    datasets["hubspot/contacts"] = _sync_hubspot_contacts(
        date_from=date_from_contacts, date_to=today, errors=errors,
    )

    # ── hubspot/deals (via GCLID contacts in the deal window) ────────────────
    datasets["hubspot/deals"] = _sync_hubspot_deals(
        date_from=date_from_deals, date_to=today, errors=errors,
    )

    # ── gclid/matches — no incremental persistence path yet ──────────────────
    datasets["gclid/matches"] = {
        "status": "skipped",
        "note": _GCLID_NOTE,
    }
    log.info("[incremental_sync] gclid/matches: %s", _GCLID_NOTE)

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    overall_status = _overall_status(datasets)

    summary = {
        "status": overall_status,
        "run_type": "daily_incremental_sync",
        "run_reason": run_reason,
        "started_at": started_at,
        "finished_at": finished_at,
        "lookback": {
            "ads_days": lookback_days_ads,
            "hubspot_contacts_days": lookback_days_hubspot_contacts,
            "hubspot_deals_days": lookback_days_hubspot_deals,
        },
        "datasets": datasets,
        "errors": errors,
    }

    log.info(
        "[incremental_sync] finished status=%s datasets=%s errors=%d",
        overall_status, list(datasets.keys()), len(errors),
    )
    return summary


# ---------------------------------------------------------------------------
# Per-dataset sync helpers
# ---------------------------------------------------------------------------

def _sync_windsor_campaigns(
    *, date_from, date_to, errors: list
) -> dict:
    """Pull and persist windsor/campaigns for the given date range."""
    from connectors.windsor_pull import pull_campaign_performance_range  # noqa: PLC0415

    batch_id = db_writers.start_sync_batch(
        source="windsor",
        dataset="campaigns",
        sync_type="daily",
        date_from=date_from,
        date_to=date_to,
    )
    try:
        rows = pull_campaign_performance_range(
            date_from=str(date_from), date_to=str(date_to),
        )
        rows_pulled = len(rows)

        # write_campaigns needs a run_id; use None for incremental syncs that
        # are not associated with a pulse run.  Writers accept None safely.
        rows_written = db_writers.write_campaigns(None, rows)

        if not persistence_succeeded(rows, rows_written):
            raise RuntimeError(
                f"windsor/campaigns persistence failed: "
                f"{rows_pulled} pulled, {rows_written} written"
            )

        last_date = max_source_date(rows, fallback_date=date_to)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="success",
                row_count=rows_written,
                last_source_date=last_date,
            )
        return {"status": "success", "rows_pulled": rows_pulled, "rows_written": rows_written}

    except Exception as exc:  # noqa: BLE001
        err = f"windsor/campaigns: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="failed",
                error_message=str(exc)[:1000],
            )
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_windsor_keywords(
    *, date_from, date_to, errors: list
) -> dict:
    """Pull and persist windsor/keywords for the given date range."""
    from connectors.windsor_pull import pull_keyword_performance_range  # noqa: PLC0415

    batch_id = db_writers.start_sync_batch(
        source="windsor",
        dataset="keywords",
        sync_type="daily",
        date_from=date_from,
        date_to=date_to,
    )
    try:
        rows = pull_keyword_performance_range(
            date_from=str(date_from), date_to=str(date_to),
        )
        rows_pulled = len(rows)
        rows_written = db_writers.write_keywords(None, rows)

        if not persistence_succeeded(rows, rows_written):
            raise RuntimeError(
                f"windsor/keywords persistence failed: "
                f"{rows_pulled} pulled, {rows_written} written"
            )

        last_date = max_source_date(rows, fallback_date=date_to)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="success",
                row_count=rows_written,
                last_source_date=last_date,
            )
        return {"status": "success", "rows_pulled": rows_pulled, "rows_written": rows_written}

    except Exception as exc:  # noqa: BLE001
        err = f"windsor/keywords: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="failed",
                error_message=str(exc)[:1000],
            )
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_windsor_geo(
    *, date_from, date_to, errors: list
) -> dict:
    """Pull and persist windsor/geo for the given date range."""
    from connectors.windsor_pull import pull_geo_performance_range  # noqa: PLC0415

    batch_id = db_writers.start_sync_batch(
        source="windsor",
        dataset="geo",
        sync_type="daily",
        date_from=date_from,
        date_to=date_to,
    )
    try:
        rows = pull_geo_performance_range(
            date_from=str(date_from), date_to=str(date_to),
        )
        rows_pulled = len(rows)
        rows_written = db_writers.write_geo(None, rows)

        if not persistence_succeeded(rows, rows_written):
            raise RuntimeError(
                f"windsor/geo persistence failed: "
                f"{rows_pulled} pulled, {rows_written} written"
            )

        last_date = max_source_date(rows, fallback_date=date_to)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="success",
                row_count=rows_written,
                last_source_date=last_date,
            )
        return {"status": "success", "rows_pulled": rows_pulled, "rows_written": rows_written}

    except Exception as exc:  # noqa: BLE001
        err = f"windsor/geo: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="failed",
                error_message=str(exc)[:1000],
            )
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_hubspot_contacts(
    *, date_from, date_to, errors: list
) -> dict:
    """Pull and persist hubspot/contacts for the given date range."""
    from connectors.hubspot_pull import pull_paid_search_contacts_in_range  # noqa: PLC0415

    batch_id = db_writers.start_sync_batch(
        source="hubspot",
        dataset="contacts",
        sync_type="daily",
        date_from=date_from,
        date_to=date_to,
    )
    try:
        rows = pull_paid_search_contacts_in_range(
            date_from=str(date_from), date_to=str(date_to),
        )
        rows_pulled = len(rows)
        rows_written = db_writers.write_leads(None, rows)

        if not persistence_succeeded(rows, rows_written):
            raise RuntimeError(
                f"hubspot/contacts persistence failed: "
                f"{rows_pulled} pulled, {rows_written} written"
            )

        last_date = max_source_date(rows, fallback_date=date_to)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="success",
                row_count=rows_written,
                last_source_date=last_date,
            )
        return {"status": "success", "rows_pulled": rows_pulled, "rows_written": rows_written}

    except Exception as exc:  # noqa: BLE001
        err = f"hubspot/contacts: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="failed",
                error_message=str(exc)[:1000],
            )
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_hubspot_deals(
    *, date_from, date_to, errors: list
) -> dict:
    """Pull and persist hubspot/deals for contacts created in the deals window.

    Deals are fetched via GCLID contacts (pull_deals_with_gclid).  Only
    contacts within the deals rolling window are queried; this avoids a
    full-history scan every day.

    Limitation: deals without a GCLID contact in the window are not captured
    in this incremental pass.  Full deal coverage requires historical backfill.
    """
    from connectors.hubspot_pull import (  # noqa: PLC0415
        pull_paid_search_contacts_in_range,
        pull_deals_with_gclid,
    )

    batch_id = db_writers.start_sync_batch(
        source="hubspot",
        dataset="deals",
        sync_type="daily",
        date_from=date_from,
        date_to=date_to,
    )
    try:
        contacts = pull_paid_search_contacts_in_range(
            date_from=str(date_from), date_to=str(date_to),
        )
        rows = pull_deals_with_gclid(contacts)
        rows_pulled = len(rows)
        rows_written = db_writers.write_deals(None, rows)

        if not persistence_succeeded(rows, rows_written):
            raise RuntimeError(
                f"hubspot/deals persistence failed: "
                f"{rows_pulled} pulled, {rows_written} written"
            )

        last_date = max_source_date(rows, fallback_date=date_to)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="success",
                row_count=rows_written,
                last_source_date=last_date,
            )
        return {"status": "success", "rows_pulled": rows_pulled, "rows_written": rows_written}

    except Exception as exc:  # noqa: BLE001
        err = f"hubspot/deals: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="failed",
                error_message=str(exc)[:1000],
            )
        return {"status": "failed", "error": str(exc)[:500]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _overall_status(datasets: dict) -> str:
    """Derive overall sync status from individual dataset outcomes.

    Returns 'success' if all non-skipped datasets succeeded,
    'partial' if some succeeded and some failed,
    'failed' if all non-skipped datasets failed.
    """
    statuses = [v["status"] for v in datasets.values() if v.get("status") != "skipped"]
    if not statuses:
        return "success"  # only skipped datasets — nothing to fail
    successes = statuses.count("success")
    failures  = statuses.count("failed")
    if failures == 0:
        return "success"
    if successes == 0:
        return "failed"
    return "partial"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = run_daily_incremental_sync(run_reason="cli")
    import json as _json
    print(_json.dumps(result, indent=2, default=str))
