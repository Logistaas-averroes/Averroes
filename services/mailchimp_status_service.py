"""
services/mailchimp_status_service.py

Read-only Mailchimp connection + dataset status (PR-ADS-151).

Surfaces the states the System Status page needs:
  connection:  connected | not_configured | permission_denied | rate_limited | failed | not_checked
  per dataset: fresh | stale | partial_backfill | failed | not_run | empty | db_unavailable

Credentials are NEVER included in any output (only the non-secret data-centre
prefix). All checks are read-only; the optional live ping issues a single GET.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Connection states (string enum — ``state`` is ALWAYS one of these)
CONN_CONNECTED = "connected"
CONN_NOT_CONFIGURED = "not_configured"
CONN_PERMISSION_DENIED = "permission_denied"
CONN_RATE_LIMITED = "rate_limited"
CONN_FAILED = "failed"
# Configured but the live ping was intentionally skipped (live=0) — the
# connection was not verified this call. Never None, so clients can rely on the
# state being a defined string.
CONN_NOT_CHECKED = "not_checked"

# Mailchimp datasets tracked in the shared sync_state / freshness model.
MAILCHIMP_DATASETS = ["campaigns", "reports", "audiences", "attribution"]

# Dataset states
DS_FRESH = "fresh"
DS_STALE = "stale"
DS_PARTIAL_BACKFILL = "partial_backfill"
DS_FAILED = "failed"
DS_NOT_RUN = "not_run"
DS_EMPTY = "empty"
DS_DB_UNAVAILABLE = "db_unavailable"


def derive_connection_state(config: dict, ping: Callable[[], Any]) -> dict:
    """Map connector config + a ping callable to a connection state.

    ``ping`` is called only when the connector is configured. Typed connector
    errors map deterministically:
      MailchimpNotConfigured  → not_configured
      MailchimpAuthError      → permission_denied
      MailchimpRateLimited    → rate_limited
      any other MailchimpError/Exception → failed
    Never raises; never returns the API key.
    """
    from connectors import mailchimp_pull as mc  # noqa: PLC0415

    if not config.get("enabled"):
        return {"state": CONN_NOT_CONFIGURED,
                "detail": "MAILCHIMP_ENABLED is not set to true"}
    if not config.get("has_api_key"):
        return {"state": CONN_NOT_CONFIGURED, "detail": "MAILCHIMP_API_KEY is not set"}
    if not config.get("server_prefix"):
        return {"state": CONN_NOT_CONFIGURED,
                "detail": "Mailchimp server prefix could not be resolved"}
    try:
        result = ping()
        return {"state": CONN_CONNECTED,
                "detail": (result or {}).get("health_status") if isinstance(result, dict) else None}
    except mc.MailchimpNotConfigured as exc:
        return {"state": CONN_NOT_CONFIGURED, "detail": str(exc)}
    except mc.MailchimpAuthError as exc:
        return {"state": CONN_PERMISSION_DENIED, "detail": str(exc)}
    except mc.MailchimpRateLimited as exc:
        return {"state": CONN_RATE_LIMITED, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 — any transient/other failure
        return {"state": CONN_FAILED, "detail": str(exc)}


def map_dataset_state(canonical_status: Optional[str], *, backfill_status: Optional[str],
                      has_rows: bool) -> str:
    """Collapse the canonical freshness status + backfill lifecycle into one of
    the Mailchimp dataset states shown on System Status."""
    from services.freshness_service import CanonicalFreshnessStatus as C  # noqa: PLC0415

    if canonical_status == C.DB_UNAVAILABLE:
        return DS_DB_UNAVAILABLE
    # A running/partial/failed backfill with rows already present is "partial".
    if backfill_status in ("running", "partial") and has_rows:
        return DS_PARTIAL_BACKFILL
    if canonical_status in (C.FAILED, C.FAILED_NO_DATA,
                            C.DATA_AVAILABLE_LATEST_SYNC_FAILED):
        return DS_FAILED
    if canonical_status in (C.STALE_WITH_DATA, C.STALE_AND_EMPTY):
        return DS_STALE
    if canonical_status == C.FRESH_WITH_DATA:
        return DS_FRESH
    if canonical_status in (C.FRESH_BUT_EMPTY, C.EMPTY_SUCCESS):
        return DS_EMPTY
    if canonical_status in (C.NOT_RUN, C.NOT_RUN_NO_UPSTREAM_DATA, C.UNKNOWN,
                            None):
        return DS_NOT_RUN
    # Any other canonical state → stale-ish; fall back to not_run if no rows.
    return DS_FRESH if has_rows else DS_NOT_RUN


def _dataset_freshness_map() -> dict:
    """Compute canonical freshness per Mailchimp dataset from the shared
    sync_state + sync_batches + row counts. Returns {dataset: {canonical, has_rows}}.

    Read-only. Degrades to empty on DB failure (caller marks db_unavailable).
    """
    from datetime import date, timedelta  # noqa: PLC0415
    from db.connection import get_conn  # noqa: PLC0415
    from services.freshness_service import (  # noqa: PLC0415
        DATASET_FRESHNESS_CONFIG, compute_canonical_freshness,
    )

    out: dict[str, dict] = {}
    try:
        with get_conn() as conn:
            if conn is None:
                return {"__db_unavailable__": True}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dataset, status, last_successful_sync_at, last_source_date, "
                    "last_batch_id FROM sync_state WHERE source = 'mailchimp'")
                state_rows = {r[0]: r for r in cur.fetchall()}

                cur.execute(
                    """
                    SELECT DISTINCT ON (dataset) dataset, status, row_count
                    FROM sync_batches WHERE source = 'mailchimp'
                    ORDER BY dataset, started_at DESC
                    """)
                batch_rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

                for ds in MAILCHIMP_DATASETS:
                    cfg_key = f"mailchimp_{ds}"
                    cfg = DATASET_FRESHNESS_CONFIG.get(cfg_key, {})
                    table = cfg.get("table")
                    date_col = cfg.get("date_column")
                    stale_days = cfg.get("stale_threshold_days", 8)
                    rcount = None
                    if table and date_col:
                        try:
                            from psycopg2 import sql as _psql  # noqa: PLC0415
                            ws = date.today() - timedelta(days=60)
                            q = _psql.SQL("SELECT COUNT(*) FROM {} WHERE {} >= %s").format(
                                _psql.Identifier(table), _psql.Identifier(date_col))
                            cur.execute(q, (ws,))
                            rcount = int(cur.fetchone()[0] or 0)
                        except Exception:  # noqa: BLE001
                            rcount = None

                    srow = state_rows.get(ds)
                    sync_status = srow[1] if srow else None
                    last_sync_at = srow[2] if srow else None
                    last_src = srow[3] if srow else None
                    batch = batch_rows.get(ds)
                    verdict = compute_canonical_freshness(
                        dataset=cfg_key,
                        rows_in_window=rcount,
                        latest_source_date=last_src,
                        sync_status=sync_status,
                        latest_batch_status=batch[0] if batch else None,
                        latest_batch_row_count=batch[1] if batch else None,
                        last_successful_sync_at=last_sync_at,
                        stale_threshold_days=stale_days,
                        dependency_status=None,
                        row_count_supported=bool(table and date_col),
                    )
                    out[ds] = {
                        "canonical": verdict["canonical_status"],
                        "severity": verdict["severity"],
                        "reason": verdict["reason"],
                        "has_rows": bool(rcount and rcount > 0),
                        "rows_in_window": rcount,
                    }
    except Exception as exc:  # noqa: BLE001
        logger.error("mailchimp dataset freshness map failed: %s", exc)
        return {"__db_unavailable__": True}
    return out


def get_status(*, live_ping: bool = True) -> dict:
    """Assemble the full Mailchimp status payload. Read-only; no secrets.

    ``live_ping=False`` skips the network entirely (config + durable-state only).
    """
    from connectors import mailchimp_pull as mc  # noqa: PLC0415
    from db import mailchimp_repository as repo  # noqa: PLC0415

    config = mc.config_status()

    if live_ping:
        connection = derive_connection_state(config, mc.ping)
    elif not config.get("configured"):
        # Not configured — report it without a network call (ping never invoked).
        connection = derive_connection_state(config, lambda: None)
    else:
        # Configured, but the caller asked to skip the live ping. Return a defined
        # state (never None) so clients can rely on the string-enum contract.
        connection = {"state": CONN_NOT_CHECKED, "detail": "live ping skipped (live=0)"}

    coverage = repo.coverage()
    mc_state = repo.get_sync_state() or {}
    backfill_status = mc_state.get("backfill_status")

    fresh_map = _dataset_freshness_map()
    db_unavailable = bool(fresh_map.get("__db_unavailable__") or coverage.get("db_unavailable"))

    datasets = []
    for ds in MAILCHIMP_DATASETS:
        info = fresh_map.get(ds, {}) if not db_unavailable else {}
        state = (DS_DB_UNAVAILABLE if db_unavailable
                 else map_dataset_state(info.get("canonical"),
                                        backfill_status=backfill_status,
                                        has_rows=info.get("has_rows", False)))
        datasets.append({
            "dataset": f"mailchimp/{ds}",
            "state": state,
            "canonical_status": info.get("canonical"),
            "rows_in_window": info.get("rows_in_window"),
            "reason": info.get("reason"),
        })

    return {
        "connection": connection,
        "config": {
            "enabled": config["enabled"],
            "configured": config["configured"],
            "server_prefix": config["server_prefix"],
            "server_prefix_source": config["server_prefix_source"],
            "has_api_key": config["has_api_key"],
            # NOTE: the API key itself is never included.
        },
        "backfill": {
            "status": backfill_status or "not_started",
            "started_at": mc_state.get("backfill_started_at"),
            "completed_at": mc_state.get("backfill_completed_at"),
            "last_incremental_at": mc_state.get("last_incremental_at"),
            "campaigns_seen": mc_state.get("campaigns_seen"),
            "last_error": mc_state.get("last_error"),
        },
        "coverage": {
            "campaigns": coverage.get("campaigns"),
            "reports": coverage.get("reports"),
            "links": coverage.get("links"),
            "audiences": coverage.get("audiences"),
            "earliest_send_time": coverage.get("earliest_send_time"),
            "latest_send_time": coverage.get("latest_send_time"),
            "latest_report_update": coverage.get("latest_report_update"),
        },
        "datasets": datasets,
        "db_unavailable": db_unavailable,
    }
