"""
db/platform_sql_attribution_repository.py

PR-ADS-146C — read-only source of HubSpot-confirmed SQL outcome contacts for the
Keyword Evidence and Search Terms attribution views.

Reuses the EXACT Campaign Evidence lead-quality contract (db/revenue_repository)
so the SQL population reconciles with Campaign Evidence:

  - source_type = 'paid_search'
  - status_category = 'qualified'                (the SQL definition)
  - contact_created_at is the business-event date (never run_date)
  - inside the selected Evidence Window by contact_created_at
  - NOT present in lead_truth_exclusions
  - deduplicated once per durable contact identity
    ``COALESCE(NULLIF(contact_id, ''), 'id:' || id::text)``

Strictly read-only: SELECT only, no writes to Google Ads / HubSpot / local data.

IMPORTANT — the durable ``leads`` table carries a HubSpot ``keyword`` but NOT the
actual user query (there is no ``search_term`` column). Search-term-level SQL
attribution therefore has no direct evidence to stand on and is reported as
unavailable rather than a fabricated zero (PR-ADS-146C §5/§6). Keyword and search
term are never inferred from one another.
"""

from __future__ import annotations

import logging
from datetime import date

from db.connection import get_conn
from db.revenue_repository import (
    _CONTACT_KEY,
    _PSEUDO_CAMPAIGNS,
    _as_date,
    _rows_as_dicts,
    _unavailable,
)

logger = logging.getLogger(__name__)

# The durable leads table has no persisted user query. Exposed as a module
# constant so the service/audit can state this plainly and tests can assert it.
LEADS_HAVE_SEARCH_TERM = False


def fetch_sql_contacts(start: date | None, end: date) -> dict:
    """Deduplicated QUALIFIED paid-search SQL contacts in ``[start, end]`` by
    ``contact_created_at``. One row per durable contact identity — a contact that
    appears across many scheduler snapshots (or has many associated deals) is
    returned once. ``start=None`` means no lower bound (All time). Read-only.

    Returns ``{available, rows, table, date_field}`` where each row is
    ``{contact_key, contact_id, company, campaign_name, keyword,
    contact_created_at, has_gclid, status_category}``. ``search_term`` is not
    selected because the column does not exist — callers treat it as absent.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable("leads", {"date_field": "contact_created_at"})
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH deduped AS (
                        SELECT DISTINCT ON ({_CONTACT_KEY})
                            {_CONTACT_KEY} AS contact_key,
                            contact_id,
                            company,
                            campaign_name,
                            keyword,
                            status_category,
                            contact_created_at,
                            (gclid IS NOT NULL AND gclid <> '') AS has_gclid
                        FROM leads
                        WHERE source_type = 'paid_search'
                          AND status_category = 'qualified'
                          AND contact_created_at IS NOT NULL
                          AND contact_created_at >= COALESCE(%s::timestamptz, contact_created_at)
                          AND contact_created_at < (%s::date + INTERVAL '1 day')
                          AND campaign_name IS NOT NULL
                          AND lower(campaign_name) NOT IN %s
                          AND campaign_name !~* 'email_campaign'
                          AND {_CONTACT_KEY} NOT IN (SELECT lead_id FROM lead_truth_exclusions)
                        ORDER BY {_CONTACT_KEY}, run_date DESC, id DESC
                    )
                    SELECT contact_key, contact_id, company, campaign_name, keyword,
                           status_category, contact_created_at, has_gclid
                    FROM deduped
                    ORDER BY contact_created_at DESC
                    """,
                    (start, end, _PSEUDO_CAMPAIGNS),
                )
                rows = _rows_as_dicts(cur)
                for r in rows:
                    r["contact_created_at"] = _as_date(r.get("contact_created_at"))
                    # The user query is not persisted on leads (see module note).
                    r["search_term"] = None
            return {"available": True, "rows": rows, "table": "leads",
                    "date_field": "contact_created_at",
                    "leads_have_search_term": LEADS_HAVE_SEARCH_TERM}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_sql_contacts failed: %s", exc)
        return _unavailable("leads", {"date_field": "contact_created_at"})
