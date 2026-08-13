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


# ---------------------------------------------------------------------------
# PR-ADS-153C — server-side contact paging for the canonical Leads page
# ---------------------------------------------------------------------------
# The Leads page must never pull the contact store into the browser. Every
# filter, the ordering and the page slice are applied in SQL; the browser only
# ever receives one bounded page.

MAX_CONTACT_PAGE_SIZE = 100
DEFAULT_CONTACT_PAGE_SIZE = 50

_CONTACT_PAGE_COLUMNS = (
    "contact_id", "company", "lifecycle_stage", "lead_status",
    "mql_status", "mql_status_category",
    "created_at", "last_modified_at",
    "date_entered_lead", "date_entered_mql", "date_entered_sql",
    "date_entered_opportunity", "date_entered_customer",
    "hs_analytics_source", "hs_analytics_source_data_1",
    "hs_analytics_source_data_2", "ip_country", "country", "owner_id",
    "has_gclid",
)


def fetch_distinct_facets() -> dict:
    """Distinct source-evidence PAIRS and campaign labels in the canonical store.

    The acquisition-group and campaign-attributable scopes are decided by pure
    Python classifiers, so the caller resolves those classifiers ONCE over these
    small distinct sets and passes the matching values back as an allow-list.
    That keeps filtering server-side (correct pagination) without duplicating the
    classification rules in SQL.

    ``source_pairs`` is ``(hs_analytics_source, hs_analytics_source_data_1)`` —
    Original Source together with its Drill-Down. The classifier needs BOTH:
    "Offline Sources" alone is ambiguous, and only the drill-down separates
    SalesNash / Events from reseller / referral / direct email.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(source_pairs=[], campaigns=[])
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT DISTINCT hs_analytics_source,
                                        hs_analytics_source_data_1
                        FROM {FUNNEL_TABLE}""")
                source_pairs = [(r[0], r[1]) for r in cur.fetchall()]
                cur.execute(
                    f"SELECT DISTINCT hs_analytics_source_data_1 FROM {FUNNEL_TABLE}")
                campaigns = [r[0] for r in cur.fetchall()]
        return {"available": True, "source_pairs": source_pairs,
                "campaigns": campaigns}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_distinct_facets failed: %s", exc)
        return _unavailable(source_pairs=[], campaigns=[])


def _append_source_pair_filter(where: list, params: list, source_pairs_in) -> None:
    """Constrain rows to a pre-resolved ``(source, drill-down)`` allow-list.

    ``None`` means no constraint; an EMPTY list means "the classifier matched
    nothing", which correctly yields an empty page rather than being ignored.

    The pairs travel as two parallel text arrays and are re-zipped by ``unnest``,
    so the predicate stays a single bounded parameter pair no matter how many
    distinct pairs exist. ``IS NOT DISTINCT FROM`` is required because both
    HubSpot fields are frequently NULL and ``NULL = NULL`` would drop those rows.
    """
    if source_pairs_in is None:
        return
    if not source_pairs_in:
        where.append("FALSE")
        return
    where.append(
        f"""EXISTS (
            SELECT 1
            FROM unnest(%s::text[], %s::text[]) AS allowed(src, detail)
            WHERE allowed.src IS NOT DISTINCT FROM {FUNNEL_TABLE}.hs_analytics_source
              AND allowed.detail IS NOT DISTINCT FROM {FUNNEL_TABLE}.hs_analytics_source_data_1
        )""")
    params.append([pair[0] for pair in source_pairs_in])
    params.append([pair[1] for pair in source_pairs_in])


# A HubSpot keyword label must be a real value, not an empty string.
_KEYWORD_PRESENT_SQL = ("(hs_analytics_source_data_2 IS NOT NULL "
                        "AND btrim(hs_analytics_source_data_2) <> '')")


def _append_campaign_filter(where: list, params: list, campaigns_in) -> None:
    """Constrain rows to campaign labels that resolved to a real Google Ads
    campaign identity. ``None`` means no constraint; an EMPTY list filters
    everything out rather than being ignored."""
    if campaigns_in is None:
        return
    if not campaigns_in:
        where.append("FALSE")
        return
    where.append("(hs_analytics_source_data_1 = ANY(%s))")
    params.append([c for c in campaigns_in if c is not None])


def fetch_funnel_contact_page(
    event: str,
    start: date | None,
    end: date | None,
    *,
    source_pairs_in: list | None = None,
    campaigns_in: list | None = None,
    require_keyword: bool = False,
    operational_status: str | None = None,
    company_query: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_CONTACT_PAGE_SIZE,
) -> dict:
    """One bounded page of contacts whose ``event`` stage-entry date is in window.

    Filtering, ordering and the page slice all happen in SQL.

    ``source_pairs_in`` / ``campaigns_in`` are the pre-resolved allow-lists
    produced by the pure classifiers (acquisition group, campaign identity).
    ``None`` means no constraint; an EMPTY list means "the classifier matched
    nothing", which correctly yields an empty page rather than being ignored.

    Ordering is newest-event-first and deterministic (``contact_id`` tiebreak) so
    paging can never repeat or skip a row.
    """
    if event not in FUNNEL_EVENTS:
        raise ValueError(f"Unknown funnel event '{event}'")

    date_column = EVENT_DATE_COLUMN[event]
    size = max(1, min(int(page_size or DEFAULT_CONTACT_PAGE_SIZE),
                      MAX_CONTACT_PAGE_SIZE))
    page_number = max(1, int(page or 1))
    offset = (page_number - 1) * size

    where = [f"{date_column} IS NOT NULL"]
    params: list = []

    where.append(f"({date_column} >= COALESCE(%s::timestamptz, {date_column}))")
    params.append(start)
    where.append(f"(%s::date IS NULL OR {date_column} < (%s::date + INTERVAL '1 day'))")
    params.extend([end, end])

    _append_source_pair_filter(where, params, source_pairs_in)
    _append_campaign_filter(where, params, campaigns_in)
    if require_keyword:
        where.append(_KEYWORD_PRESENT_SQL)

    if operational_status:
        where.append("(mql_status_category = %s)")
        params.append(operational_status)

    if company_query:
        where.append("(company ILIKE %s)")
        params.append(f"%{company_query.strip()}%")

    where_sql = " AND ".join(where)

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[], total=0, page=page_number,
                                    page_size=size)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM {FUNNEL_TABLE} WHERE {where_sql}",
                    params)
                total = int(cur.fetchone()[0] or 0)

                cur.execute(
                    f"""
                    SELECT {", ".join(_CONTACT_PAGE_COLUMNS)}
                    FROM {FUNNEL_TABLE}
                    WHERE {where_sql}
                    ORDER BY {date_column} DESC, contact_id ASC
                    LIMIT %s OFFSET %s
                    """,
                    params + [size, offset])
                rows = [_normalise_row(r) for r in _rows_as_dicts(cur)]
        return {
            "available": True,
            "rows": rows,
            "total": total,
            "page": page_number,
            "page_size": size,
            "has_more": (offset + len(rows)) < total,
            "order_by": f"{date_column} DESC",
        }
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_funnel_contact_page failed: %s", exc)
        return _unavailable(rows=[], total=0, page=page_number, page_size=size)


def fetch_operational_status_counts(
    event: str, start: date | None, end: date | None,
    *, source_pairs_in: list | None = None,
    campaigns_in: list | None = None,
    require_keyword: bool = False) -> dict:
    """Counts by ``mql_status_category`` for one event window.

    Powers the Disqualified / Other view and the working-status filter without a
    second full scan in the browser.

    ``source_pairs_in`` / ``campaigns_in`` / ``require_keyword`` are the SAME
    pre-resolved allow-lists ``fetch_funnel_contact_page`` takes, produced by the
    SAME resolver, so a named scope selects one population in both views. ``None``
    means no constraint; an EMPTY list means the classifier matched nothing and
    must filter everything out.
    """
    if event not in FUNNEL_EVENTS:
        raise ValueError(f"Unknown funnel event '{event}'")
    date_column = EVENT_DATE_COLUMN[event]

    where = [
        f"{date_column} IS NOT NULL",
        f"({date_column} >= COALESCE(%s::timestamptz, {date_column}))",
        f"(%s::date IS NULL OR {date_column} < (%s::date + INTERVAL '1 day'))",
    ]
    params: list = [start, end, end]
    _append_source_pair_filter(where, params, source_pairs_in)
    _append_campaign_filter(where, params, campaigns_in)
    if require_keyword:
        where.append(_KEYWORD_PRESENT_SQL)

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(counts={})
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COALESCE(mql_status_category, 'no_verdict') AS category,
                           COUNT(*) AS contacts
                    FROM {FUNNEL_TABLE}
                    WHERE {" AND ".join(where)}
                    GROUP BY 1
                    """,
                    params)
                counts = {r[0]: int(r[1]) for r in cur.fetchall()}
        return {"available": True, "counts": counts}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_operational_status_counts failed: %s", exc)
        return _unavailable(counts={})
