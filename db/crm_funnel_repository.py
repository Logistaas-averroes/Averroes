"""
db/crm_funnel_repository.py

PR-ADS-153B — READ-ONLY access to the canonical CRM funnel spine
(``hubspot_contact_funnel``) plus the raw ingredients needed to reconcile it
against the legacy ``leads`` / ``status_category`` doctrine.

This module performs NO classification and NO windowing decisions of its own: it
returns raw rows so ``services/canonical_crm_funnel_service.py`` remains the one
place funnel truth is defined, and so the whole reconciliation stays unit-testable
without a database.

Availability is explicit. When the database is unreachable every fetch returns
``available: False`` — never an empty result that a caller could mistake for
"zero contacts" (Minimum Viable Truth rule 8: unavailable ≠ zero).
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from analysis.crm_lifecycle import EVENT_DATE_COLUMN, FUNNEL_EVENTS
from db.connection import get_conn

log = logging.getLogger(__name__)

# Durable contact identity used by the legacy `leads` table (PR-ADS-152).
_LEGACY_CONTACT_KEY = "COALESCE(NULLIF(contact_id, ''), 'id:' || id::text)"

FUNNEL_TABLE = "hubspot_contact_funnel"

_FUNNEL_COLUMNS = (
    "contact_id", "created_at", "last_modified_at",
    "lifecycle_stage", "lead_status", "mql_status", "mql_status_category",
    "date_entered_lead", "date_entered_mql", "date_entered_sql",
    "date_entered_opportunity", "date_entered_customer",
    "hs_analytics_source", "hs_analytics_source_data_1", "hs_analytics_source_data_2",
    "ip_country", "country", "company", "gclid", "has_gclid",
)

_TIMESTAMP_FIELDS = (
    "created_at", "last_modified_at", "date_entered_lead", "date_entered_mql",
    "date_entered_sql", "date_entered_opportunity", "date_entered_customer",
)


def _rows_as_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _as_date(value):
    """Coerce a timestamptz/date to ``datetime.date``; None stays None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _normalise_row(row: dict) -> dict:
    for field in _TIMESTAMP_FIELDS:
        if field in row:
            row[field] = _as_date(row[field])
    return row


def _unavailable(**extra) -> dict:
    payload = {"available": False, "reason": "database_unavailable"}
    payload.update(extra)
    return payload


def fetch_funnel_contacts(start: date | None, end: date | None) -> dict:
    """Canonical contact rows whose ANY stage-entry date falls in ``[start, end]``.

    One row per HubSpot contact (the table is already latest-state, keyed on
    ``contact_id``), so no dedup is required here.

    The OR predicate across all five stage-entry columns means a single fetch
    serves every funnel event for the window, and — critically — a contact that
    entered SQL inside the window is returned even when its CURRENT lifecycle
    stage is ``customer``. Historical cohorts are never lost to current state.

    ``start=None`` means no lower bound; ``end=None`` means no upper bound
    (All Time). Rows also carry every later-stage date regardless of window, so
    cohort-safe conversions can be computed downstream.
    """
    date_columns = [EVENT_DATE_COLUMN[e] for e in FUNNEL_EVENTS]
    window_clause = " OR ".join(
        f"""(
            {col} IS NOT NULL
            AND ({col} >= COALESCE(%s::timestamptz, {col}))
            AND (%s::date IS NULL OR {col} < (%s::date + INTERVAL '1 day'))
        )"""
        for col in date_columns
    )
    params: list = []
    for _ in date_columns:
        params.extend([start, end, end])

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[])
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {", ".join(_FUNNEL_COLUMNS)}
                    FROM {FUNNEL_TABLE}
                    WHERE {window_clause}
                    """,
                    params,
                )
                rows = [_normalise_row(r) for r in _rows_as_dicts(cur)]
        return {"available": True, "rows": rows, "table": FUNNEL_TABLE}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_funnel_contacts failed: %s", exc)
        return _unavailable(rows=[])


def fetch_all_funnel_contacts() -> dict:
    """Every canonical contact row. Used by All-Time reporting and the audit."""
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[])
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(_FUNNEL_COLUMNS)} FROM {FUNNEL_TABLE}"
                )
                rows = [_normalise_row(r) for r in _rows_as_dicts(cur)]
        return {"available": True, "rows": rows, "table": FUNNEL_TABLE}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_all_funnel_contacts failed: %s", exc)
        return _unavailable(rows=[])


def fetch_coverage_summary() -> dict:
    """Ingestion + stage-evidence coverage for the canonical contact store.

    Proves completeness honestly (PR-ADS-153B §8): the presence of recent rows is
    never treated as proof that history was ingested.
    """
    stage_counts_sql = ",\n                           ".join(
        f"COUNT({EVENT_DATE_COLUMN[e]}) AS with_{EVENT_DATE_COLUMN[e]}"
        for e in FUNNEL_EVENTS
    )
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable()
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COUNT(*) AS total_contacts,
                           COUNT(lifecycle_stage) AS with_lifecycle_stage,
                           COUNT(mql_status) AS with_mql_status,
                           {stage_counts_sql},
                           MIN(created_at) AS earliest_created_at,
                           MAX(created_at) AS latest_created_at,
                           MAX(last_modified_at) AS latest_modified_at,
                           MAX(last_ingested_at) AS last_ingested_at
                    FROM {FUNNEL_TABLE}
                    """
                )
                summary = _rows_as_dicts(cur)[0]

                cur.execute(
                    f"""
                    SELECT COALESCE(lifecycle_stage, '(null)') AS lifecycle_stage,
                           COUNT(*) AS contacts
                    FROM {FUNNEL_TABLE}
                    GROUP BY 1
                    ORDER BY 2 DESC
                    """
                )
                by_stage = _rows_as_dicts(cur)

                cur.execute(
                    f"""
                    SELECT COALESCE(mql_status, '(null)') AS mql_status,
                           COALESCE(mql_status_category, '(null)') AS mql_status_category,
                           COUNT(*) AS contacts
                    FROM {FUNNEL_TABLE}
                    GROUP BY 1, 2
                    ORDER BY 3 DESC
                    """
                )
                by_status = _rows_as_dicts(cur)

                cur.execute(
                    f"""
                    SELECT COALESCE(hs_analytics_source, '(null)') AS hs_analytics_source,
                           COUNT(*) AS contacts
                    FROM {FUNNEL_TABLE}
                    GROUP BY 1
                    ORDER BY 2 DESC
                    """
                )
                by_source = _rows_as_dicts(cur)

        for key in ("earliest_created_at", "latest_created_at",
                    "latest_modified_at", "last_ingested_at"):
            summary[key] = _as_date(summary.get(key))

        return {
            "available": True,
            "summary": summary,
            "by_lifecycle_stage": by_stage,
            "by_mql_status": by_status,
            "by_source": by_source,
        }
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_coverage_summary failed: %s", exc)
        return _unavailable()


def fetch_legacy_outcome_rows() -> dict:
    """Latest legacy ``leads`` outcome per durable contact identity.

    Returns the pre-PR-ADS-153B truth (``status_category`` derived from
    ``mql_status``) so the reconciliation can compare it, contact by contact,
    against HubSpot lifecycle evidence. Read-only; the legacy table is never
    modified by this PR.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[], exclusions=set())
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT ON ({_LEGACY_CONTACT_KEY})
                           {_LEGACY_CONTACT_KEY} AS contact_key,
                           contact_id,
                           id AS row_id,
                           run_date,
                           contact_created_at,
                           status_category,
                           mql_status,
                           hs_analytics_source,
                           campaign_name,
                           company
                    FROM leads
                    ORDER BY {_LEGACY_CONTACT_KEY}, run_date DESC, id DESC
                    """
                )
                rows = _rows_as_dicts(cur)
                for r in rows:
                    r["contact_created_at"] = _as_date(r.get("contact_created_at"))
                    r["run_date"] = _as_date(r.get("run_date"))

                cur.execute("SELECT lead_id FROM lead_truth_exclusions")
                exclusions = {row[0] for row in cur.fetchall()}

        return {"available": True, "rows": rows, "exclusions": exclusions}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_legacy_outcome_rows failed: %s", exc)
        return _unavailable(rows=[], exclusions=set())


def fetch_polluted_mql_status_rows(limit: int = 200) -> dict:
    """Legacy ``leads`` rows whose ``mql_status`` holds a value HubSpot never emits.

    These are the residue of the removed ``mql_status or mql___mdr_comments``
    fallback (PR-ADS-153B §15). Detection only — this PR never rewrites or deletes
    historical evidence. No email addresses are returned.
    """
    from analysis.mql_status_taxonomy import KNOWN_MQL_STATUS_VALUES  # noqa: PLC0415

    known = sorted(KNOWN_MQL_STATUS_VALUES)
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[], total=0)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM leads
                    WHERE mql_status IS NOT NULL
                      AND btrim(mql_status) <> ''
                      AND btrim(mql_status) <> ALL(%s)
                    """,
                    (known,),
                )
                total = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT DISTINCT ON (btrim(mql_status))
                           btrim(mql_status) AS raw_value, COUNT(*) OVER () AS distinct_values
                    FROM leads
                    WHERE mql_status IS NOT NULL
                      AND btrim(mql_status) <> ''
                      AND btrim(mql_status) <> ALL(%s)
                    ORDER BY btrim(mql_status)
                    LIMIT %s
                    """,
                    (known, int(limit)),
                )
                rows = _rows_as_dicts(cur)
        return {"available": True, "rows": rows, "total": int(total or 0)}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_polluted_mql_status_rows failed: %s", exc)
        return _unavailable(rows=[], total=0)
