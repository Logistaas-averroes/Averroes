"""
db/canonical_contact_outcome_repository.py

PR-ADS-152 — the single read-only DB layer feeding the canonical contact-outcome
service (services/canonical_contact_outcome_service.py) and the admin SQL-truth
audit (GET /api/audit/sql-truth).

Design goal: reconcile the contradictory SQL counts across Dashboard, Revenue by
Source, Revenue Decision Mart, Campaign / Keyword Evidence and Search Terms by
building ONE canonical population — one row per durable contact identity — that
every scope is derived from. To keep that population fully auditable AND unit
testable without a live database, this repository performs only simple SELECTs
and returns the RAW ingredients; ALL deduplication, exclusion, classification and
scoping happen in the pure service layer:

  - raw ``leads`` snapshot rows (every source, never pre-deduped here);
  - the durable ``lead_truth_exclusions`` key set;
  - the durable ``contact_source_classification`` rows keyed by contact_key.

The durable contact key is the SAME one the rest of the system uses
(``db.revenue_repository._CONTACT_KEY``):

    COALESCE(NULLIF(contact_id, ''), 'id:' || id::text)

Strictly read-only: SELECT statements only. No writes to Google Ads, HubSpot, or
any local table. When the database is unavailable every function returns an
``available=False`` result so callers degrade honestly instead of fabricating a
zero (PR-ADS doctrine — never a silent 0).
"""

from __future__ import annotations

import logging
from datetime import date

from db.connection import get_conn
from db.revenue_repository import _CONTACT_KEY, _as_date, _rows_as_dicts

logger = logging.getLogger(__name__)

# Columns preserved on every canonical snapshot row (PR-ADS-152 §2). The durable
# ``leads`` table has NO persisted user query, so ``search_term`` is never a
# column here — search-term attribution stays exact-query-only (PR-ADS-146C).
_SNAPSHOT_COLUMNS = (
    "contact_key", "contact_id", "row_id", "run_date", "contact_created_at",
    "status_category", "source_type", "hs_analytics_source", "campaign_name",
    "keyword", "country", "company", "has_gclid",
)


def fetch_canonical_inputs(start: date | None, end: date) -> dict:
    """Raw ingredients for the canonical contact-outcome population. Read-only.

    Returns ``{available, lead_rows, exclusions, classification, table,
    date_field}`` where:

      - ``lead_rows`` — the LATEST ``leads`` snapshot per durable contact identity
        (DISTINCT ON contact key, run_date DESC, id DESC across ALL snapshots),
        THEN windowed to those whose latest ``contact_created_at`` is inside
        ``[start, end]`` (or ``contact_created_at IS NULL``, so the service can
        prove the "missing business date → not counted" rule), across ALL sources.
        Deduplicating globally BEFORE windowing means a newer out-of-window
        snapshot correctly suppresses an older in-window one — latest-state truth
        is read from the true latest snapshot. One row per contact (not unlimited
        history). ``start=None`` means no lower bound. The service re-applies the
        window/dedup harmlessly (idempotent) and layers exclusions + scopes on top.
      - ``exclusions`` — the set of ``lead_truth_exclusions.lead_id`` keys.
      - ``classification`` — the durable ``contact_source_classification`` rows
        (one per contact_key) so the service can detect stale / missing
        classification instead of silently trusting it.

    ``lead_rows`` items carry the columns in ``_SNAPSHOT_COLUMNS``; the durable
    contact key is exposed as ``contact_key`` and the surrogate row id as
    ``row_id`` (both needed for the latest-snapshot ordering).
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable()
            with conn.cursor() as cur:
                # PR-ADS-152: deduplicate to the LATEST snapshot per durable contact
                # key GLOBALLY first (across all snapshots), and only THEN window the
                # resulting latest rows. Filtering by contact_created_at before the
                # dedup would let an older in-window snapshot survive when the
                # contact's true latest snapshot is out of the window — the window
                # (and the latest status) must be read from the latest snapshot. The
                # dedup is one row per contact (not unlimited history) so it stays
                # bounded.
                cur.execute(
                    f"""
                    WITH latest AS (
                        SELECT DISTINCT ON ({_CONTACT_KEY})
                               {_CONTACT_KEY} AS contact_key,
                               contact_id,
                               id AS row_id,
                               run_date,
                               contact_created_at,
                               status_category,
                               source_type,
                               hs_analytics_source,
                               campaign_name,
                               keyword,
                               country,
                               company,
                               (gclid IS NOT NULL AND gclid <> '') AS has_gclid
                        FROM leads
                        ORDER BY {_CONTACT_KEY}, run_date DESC, id DESC
                    )
                    SELECT contact_key, contact_id, row_id, run_date, contact_created_at,
                           status_category, source_type, hs_analytics_source, campaign_name,
                           keyword, country, company, has_gclid
                    FROM latest
                    WHERE contact_created_at IS NULL
                       OR (contact_created_at >= COALESCE(%s::timestamptz, contact_created_at)
                           AND (%s::date IS NULL
                                OR contact_created_at < (%s::date + INTERVAL '1 day')))
                    """,
                    (start, end, end),
                )
                lead_rows = _rows_as_dicts(cur)
                for r in lead_rows:
                    r["contact_created_at"] = _as_date(r.get("contact_created_at"))
                    r["run_date"] = _as_date(r.get("run_date"))

                cur.execute("SELECT lead_id FROM lead_truth_exclusions")
                exclusions = {row[0] for row in cur.fetchall()}

                cur.execute(
                    """
                    SELECT contact_key, contact_id, acquisition_group,
                           source_primary_raw, source_detail_raw,
                           status_category, contact_created_at,
                           classification_rule_version, classified_at, updated_at
                    FROM contact_source_classification
                    """
                )
                classification = _rows_as_dicts(cur)
                for r in classification:
                    r["contact_created_at"] = _as_date(r.get("contact_created_at"))
                    r["classified_at"] = _as_date(r.get("classified_at"))
                    r["updated_at"] = _as_date(r.get("updated_at"))
            return {
                "available": True,
                "lead_rows": lead_rows,
                "exclusions": exclusions,
                "classification": classification,
                "table": "leads",
                "date_field": "contact_created_at",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_canonical_inputs failed: %s", exc)
        return _unavailable()


def _unavailable() -> dict:
    return {
        "available": False,
        "lead_rows": [],
        "exclusions": set(),
        "classification": [],
        "table": "leads",
        "date_field": "contact_created_at",
    }
