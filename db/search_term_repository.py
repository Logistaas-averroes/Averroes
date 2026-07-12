"""
Search-term evidence repository (PR-ADS-144). Read-only.

Durable fetchers over the ``search_terms`` fact table for the Search Terms +
Patterns evidence page. All selected-window boundaries use ``source_date`` (the
Google Ads reporting date), NEVER ``run_date`` — scheduler timing must not move
business totals.

Natural-key / duplication contract (audited for PR-ADS-144):
  The table enforces a UNIQUE index ``idx_search_terms_unique_fact`` on
  (source_date, COALESCE(campaign_name,''), COALESCE(ad_group,''),
   COALESCE(keyword,''), COALESCE(match_type,''), search_term) and the writer
  (``db.writers.write_search_terms``) upserts ON CONFLICT on that same key.
  A repeated scheduler run therefore UPDATES the existing fact row in place —
  duplicate scheduler copies / overlapping snapshots for the same
  term/day/campaign event CANNOT exist, so summing rows inside a window never
  multiplies the same fact.

Currency contract: the table durably stores ``spend_usd`` only (no cost_micros,
no native currency column). Spend from this repository is therefore *reported
search-term spend in USD as stored at ingestion* — it is never re-derived
through a spot FX rate and never presented as canonical account spend.

No writes. No Google Ads / HubSpot calls.
"""

from __future__ import annotations

import logging
from datetime import date

from db.connection import get_conn

logger = logging.getLogger(__name__)

# The documented durable natural key of the search_terms table (kept in one
# place so the service/audit layer can disclose it verbatim).
SEARCH_TERMS_NATURAL_KEY = (
    "source_date + COALESCE(campaign_name,'') + COALESCE(ad_group,'') + "
    "COALESCE(keyword,'') + COALESCE(match_type,'') + search_term "
    "(UNIQUE index idx_search_terms_unique_fact; writer upserts ON CONFLICT)"
)


def _rows_as_dicts(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_search_term_aggregates(start: date | None, end: date) -> dict:
    """Selected-window aggregates at (search_term, campaign_name, campaign_id)
    grain, bounded by ``source_date`` (``start`` None == all_time, no lower
    bound). Read-only.

    Grouping keeps campaign_id in the key so two campaign ids that share a
    display name are NEVER merged; groups whose campaign_id is NULL are merged
    to canonical identity later by the service through the approved durable
    mapping only (never fuzzy).

    Returns {available, rows, source: {row_count, spend_usd_total, clicks_total,
    impressions_total, conversions_total, distinct_source_dates,
    min_source_date, max_source_date}}. Each row: {search_term, campaign_name,
    campaign_id, spend_usd, clicks, impressions, conversions, row_count,
    first_seen, last_seen, any_flagged, any_unreviewed, junk_categories,
    matched_patterns, ad_groups, keywords, match_types}.

    Never raises — DB outage returns {"available": False, "rows": []}.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT search_term,
                           campaign_name,
                           campaign_id,
                           SUM(spend_usd)              AS spend_usd,
                           SUM(clicks)::bigint         AS clicks,
                           SUM(impressions)::bigint    AS impressions,
                           SUM(conversions)            AS conversions,
                           COUNT(*)::bigint            AS row_count,
                           MIN(source_date)            AS first_seen,
                           MAX(source_date)            AS last_seen,
                           BOOL_OR(is_flagged_waste IS TRUE)  AS any_flagged,
                           BOOL_OR(is_flagged_waste IS NULL)  AS any_unreviewed,
                           ARRAY_AGG(DISTINCT junk_category)
                               FILTER (WHERE junk_category IS NOT NULL)
                               AS junk_categories,
                           ARRAY_AGG(DISTINCT matched_pattern)
                               FILTER (WHERE matched_pattern IS NOT NULL)
                               AS matched_patterns,
                           ARRAY_AGG(DISTINCT ad_group)
                               FILTER (WHERE ad_group IS NOT NULL)
                               AS ad_groups,
                           ARRAY_AGG(DISTINCT keyword)
                               FILTER (WHERE keyword IS NOT NULL)
                               AS keywords,
                           ARRAY_AGG(DISTINCT match_type)
                               FILTER (WHERE match_type IS NOT NULL)
                               AS match_types
                    FROM search_terms
                    WHERE (%s::date IS NULL OR source_date >= %s)
                      AND source_date <= %s
                    GROUP BY search_term, campaign_name, campaign_id
                    """,
                    (start, start, end),
                )
                rows = _rows_as_dicts(cur)

                # Source totals over the SAME raw window population — the audit
                # layer reconciles the aggregated rows back to these.
                cur.execute(
                    """
                    SELECT COUNT(*)::bigint            AS row_count,
                           SUM(spend_usd)              AS spend_usd_total,
                           SUM(clicks)::bigint         AS clicks_total,
                           SUM(impressions)::bigint    AS impressions_total,
                           SUM(conversions)            AS conversions_total,
                           COUNT(DISTINCT source_date) AS distinct_source_dates,
                           MIN(source_date)            AS min_source_date,
                           MAX(source_date)            AS max_source_date
                    FROM search_terms
                    WHERE (%s::date IS NULL OR source_date >= %s)
                      AND source_date <= %s
                    """,
                    (start, start, end),
                )
                src = _rows_as_dicts(cur)
            source = src[0] if src else {}
            for row in rows:
                row["spend_usd"] = (
                    float(row["spend_usd"]) if row["spend_usd"] is not None else None)
                row["conversions"] = (
                    float(row["conversions"]) if row["conversions"] is not None else None)
                row["clicks"] = int(row["clicks"] or 0)
                row["impressions"] = int(row["impressions"] or 0)
                row["row_count"] = int(row["row_count"] or 0)
                for k in ("junk_categories", "matched_patterns", "ad_groups",
                          "keywords", "match_types"):
                    row[k] = sorted(row.get(k) or [])
            return {
                "available": True,
                "rows": rows,
                "source": {
                    "row_count": int(source.get("row_count") or 0),
                    "spend_usd_total": (
                        float(source["spend_usd_total"])
                        if source.get("spend_usd_total") is not None else None),
                    "clicks_total": (
                        int(source["clicks_total"])
                        if source.get("clicks_total") is not None else None),
                    "impressions_total": (
                        int(source["impressions_total"])
                        if source.get("impressions_total") is not None else None),
                    "conversions_total": (
                        float(source["conversions_total"])
                        if source.get("conversions_total") is not None else None),
                    "distinct_source_dates": int(source.get("distinct_source_dates") or 0),
                    "min_source_date": source.get("min_source_date"),
                    "max_source_date": source.get("max_source_date"),
                },
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_search_term_aggregates failed: %s", exc)
        return {"available": False, "rows": []}


def fetch_search_term_daily(start: date | None, end: date, search_term: str,
                            campaign_names: list | None = None,
                            campaign_ids: list | None = None) -> dict:
    """Per-source_date evidence series for ONE search term (drawer). Read-only.

    Only dates the source actually reported are returned — missing dates are
    NEVER fabricated as zero rows. Optional campaign_names/campaign_ids narrow
    the series to the drawer row's campaign identity group (an OR across the
    two lists, matching how the service merged the group).
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            conditions = ["(%s::date IS NULL OR source_date >= %s)",
                          "source_date <= %s", "search_term = %s"]
            params: list = [start, start, end, search_term]
            identity_clauses = []
            if campaign_ids:
                identity_clauses.append("campaign_id = ANY(%s)")
                params.append([str(c) for c in campaign_ids])
            if campaign_names:
                identity_clauses.append("campaign_name = ANY(%s)")
                params.append(list(campaign_names))
            if identity_clauses:
                conditions.append("(" + " OR ".join(identity_clauses) + ")")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_date,
                           SUM(spend_usd)           AS spend_usd,
                           SUM(clicks)::bigint      AS clicks,
                           SUM(impressions)::bigint AS impressions,
                           SUM(conversions)         AS conversions
                    FROM search_terms
                    WHERE """ + " AND ".join(conditions) + """
                    GROUP BY source_date
                    ORDER BY source_date
                    """,
                    tuple(params),
                )
                rows = _rows_as_dicts(cur)
            for row in rows:
                row["source_date"] = (
                    row["source_date"].isoformat()
                    if row.get("source_date") is not None else None)
                row["spend_usd"] = (
                    float(row["spend_usd"]) if row["spend_usd"] is not None else None)
                row["conversions"] = (
                    float(row["conversions"]) if row["conversions"] is not None else None)
                row["clicks"] = int(row["clicks"] or 0)
                row["impressions"] = int(row["impressions"] or 0)
            return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_search_term_daily failed: %s", exc)
        return {"available": False, "rows": []}


def fetch_latest_waste_classification(search_term: str) -> dict:
    """Latest stored waste_terms classification evidence for one term. Read-only.

    waste_terms is the analysis-run output table (run_date grain) — used ONLY as
    supplementary classification proof in the drawer (junk category, matched
    rule, CRM-junk confirmation count, classification date), never as the
    selected-window business boundary. Returns {available, row|None}.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "row": None}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT search_term, campaign_name, junk_category,
                           matched_pattern, crm_junk_confirmed, run_date
                    FROM waste_terms
                    WHERE search_term = %s
                    ORDER BY run_date DESC, id DESC
                    LIMIT 1
                    """,
                    (search_term,),
                )
                rows = _rows_as_dicts(cur)
            if not rows:
                return {"available": True, "row": None}
            row = rows[0]
            row["run_date"] = (
                row["run_date"].isoformat() if row.get("run_date") is not None else None)
            row["crm_junk_confirmed"] = (
                int(row["crm_junk_confirmed"])
                if row.get("crm_junk_confirmed") is not None else None)
            return {"available": True, "row": row}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_latest_waste_classification failed: %s", exc)
        return {"available": False, "row": None}
