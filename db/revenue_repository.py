"""
Revenue Attribution Repository (PR-ADS-108)

Read-only DB query layer for the revenue-attribution endpoint. Provides durable,
production-available aggregates so /api/revenue-attribution does not depend on
ephemeral local JSON files written only as a side effect of the weekly scheduler.

Durable sources (written by the scheduler into PostgreSQL):
  - geo                 -> campaign + country spend (per-day, real country names)
  - leads              -> leads + SQLs (status_category), deduped per contact
  - gclid_attribution  -> closed-won revenue/customers, windowed by deal_close_date
  - sync_state         -> per-dataset freshness for source_health

Guarantees:
  - Read-only. SELECT statements only.
  - No writes to Google Ads, HubSpot, or any external system.
  - No DB writes (no INSERT/UPDATE/DELETE).
  - Non-fatal: when the database is unavailable, every function returns a result
    with available=False so callers can fall back / report source state.
"""

from __future__ import annotations

import logging
from datetime import date

from db.connection import get_conn

logger = logging.getLogger(__name__)

# HubSpot "Deal Won / Payment Received" stage (revenue truth).
WON_DEAL_STAGE_ID = "326093516"
_WON_LABEL_LIKE = "%won%"


def _unavailable(table: str, extra: dict | None = None) -> dict:
    result = {"available": False, "rows": [], "table": table,
              "coverage_start": None, "coverage_end": None}
    if extra:
        result.update(extra)
    return result


def _rows_as_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def db_available() -> bool:
    """Return True if the database pool is reachable."""
    try:
        with get_conn() as conn:
            return conn is not None
    except Exception:  # noqa: BLE001
        return False


def fetch_campaign_country_spend(start: date | None, end: date) -> dict:
    """Per (campaign, country) Google Ads spend from the durable `geo` table.

    Rows that recur across overlapping scheduler runs are deduped to the latest
    run for each (campaign, country, day) so spend is not double-counted.

    Returns dict with available, rows [{campaign_name, country, spend}],
    coverage_start, coverage_end.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable("geo")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT campaign_name, country, SUM(spend_usd)::float AS spend
                    FROM (
                        SELECT DISTINCT ON (campaign_name, country, run_date)
                               campaign_name, country, run_date, spend_usd
                        FROM geo
                        WHERE (%s::date IS NULL OR run_date >= %s)
                          AND run_date <= %s
                        ORDER BY campaign_name, country, run_date, run_id DESC
                    ) d
                    GROUP BY campaign_name, country
                    """,
                    (start, start, end),
                )
                rows = _rows_as_dicts(cur)
                cur.execute(
                    """
                    SELECT MIN(run_date) AS cstart, MAX(run_date) AS cend
                    FROM geo
                    WHERE (%s::date IS NULL OR run_date >= %s) AND run_date <= %s
                    """,
                    (start, start, end),
                )
                cov = cur.fetchone() or (None, None)
            return {
                "available": True,
                "rows": rows,
                "table": "geo",
                "coverage_start": _as_date(cov[0]),
                "coverage_end": _as_date(cov[1]),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_campaign_country_spend failed: %s", exc)
        return _unavailable("geo")


def fetch_lead_quality(start: date | None, end: date) -> dict:
    """Leads + SQLs from the durable `leads` table, deduped per contact.

    SQL = status_category 'qualified' (the existing lead-quality definition).
    Returns rows [{campaign_name, country, status_category, has_gclid}].
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable("leads")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH deduped AS (
                        SELECT DISTINCT ON (COALESCE(NULLIF(contact_id, ''), 'id:' || id::text))
                            campaign_name,
                            country,
                            status_category,
                            (gclid IS NOT NULL AND gclid <> '') AS has_gclid
                        FROM leads
                        WHERE (%s::date IS NULL OR run_date >= %s)
                          AND run_date <= %s
                        ORDER BY COALESCE(NULLIF(contact_id, ''), 'id:' || id::text),
                                 run_date DESC, id DESC
                    )
                    SELECT campaign_name, country, status_category, has_gclid
                    FROM deduped
                    """,
                    (start, start, end),
                )
                rows = _rows_as_dicts(cur)
            return {"available": True, "rows": rows, "table": "leads",
                    "coverage_start": None, "coverage_end": None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_lead_quality failed: %s", exc)
        return _unavailable("leads")


def fetch_won_revenue(start: date | None, end: date) -> dict:
    """Closed-won revenue/customers from the durable `gclid_attribution` table.

    Windowed by the real deal_close_date and deduped per deal_id. Only
    closed-won deals (revenue truth). match_status drives row confidence.

    Returns rows [{campaign_name, country, deal_id, deal_amount_usd, match_status}].
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable("gclid_attribution")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT campaign_name, country, deal_id,
                           deal_amount_usd::float AS deal_amount_usd, match_status
                    FROM (
                        SELECT DISTINCT ON (deal_id)
                            deal_id, campaign_name, country,
                            deal_amount_usd, match_status, deal_close_date
                        FROM gclid_attribution
                        WHERE deal_id IS NOT NULL
                          AND deal_close_date IS NOT NULL
                          AND (deal_stage = %s OR deal_stage_label ILIKE %s)
                          AND (%s::date IS NULL OR deal_close_date >= %s)
                          AND deal_close_date < (%s::date + INTERVAL '1 day')
                        ORDER BY deal_id, created_at DESC
                    ) d
                    """,
                    (WON_DEAL_STAGE_ID, _WON_LABEL_LIKE, start, start, end),
                )
                rows = _rows_as_dicts(cur)
                cur.execute(
                    """
                    SELECT MIN(deal_close_date)::date AS cstart,
                           MAX(deal_close_date)::date AS cend
                    FROM gclid_attribution
                    WHERE deal_id IS NOT NULL
                      AND deal_close_date IS NOT NULL
                      AND (deal_stage = %s OR deal_stage_label ILIKE %s)
                      AND (%s::date IS NULL OR deal_close_date >= %s)
                      AND deal_close_date < (%s::date + INTERVAL '1 day')
                    """,
                    (WON_DEAL_STAGE_ID, _WON_LABEL_LIKE, start, start, end),
                )
                cov = cur.fetchone() or (None, None)
            return {
                "available": True,
                "rows": rows,
                "table": "gclid_attribution",
                "coverage_start": _as_date(cov[0]),
                "coverage_end": _as_date(cov[1]),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_won_revenue failed: %s", exc)
        return _unavailable("gclid_attribution")


def fetch_sync_state() -> dict:
    """Per-(source, dataset) freshness from sync_state for source_health.

    Returns {"available": bool, "datasets": {"source/dataset": {...}}}.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "datasets": {}}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source, dataset, status,
                           last_successful_sync_at, last_source_date
                    FROM sync_state
                    """
                )
                datasets = {}
                for source, dataset, status, last_sync, last_src in cur.fetchall():
                    datasets[f"{source}/{dataset}"] = {
                        "status": status,
                        "last_successful_sync_at": last_sync.isoformat() if last_sync else None,
                        "last_source_date": _as_date(last_src),
                    }
            return {"available": True, "datasets": datasets}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_sync_state failed: %s", exc)
        return {"available": False, "datasets": {}}
