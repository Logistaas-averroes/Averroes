"""
Search-term evidence repository (PR-ADS-144). Read-only.

Durable fetchers over the ``search_terms`` fact table for the Search Terms +
Patterns evidence page. All selected-window boundaries use ``source_date`` (the
Google Ads reporting date), NEVER ``run_date`` — scheduler timing must not move
business totals.

Natural-key / duplication contract (audited for PR-ADS-144):
  The table enforces a UNIQUE index ``idx_search_terms_unique_fact`` on
  (source_date, COALESCE(campaign_name,''), COALESCE(campaign_id,''),
   COALESCE(ad_group,''), COALESCE(keyword,''), COALESCE(match_type,''),
   search_term) and the writer (``db.writers.write_search_terms``) upserts ON
  CONFLICT on that same key. campaign_id is included in the key so two campaign
  IDs sharing a display name can never collide. A repeated scheduler run
  therefore UPDATES the existing fact row in place — duplicate scheduler copies
  / overlapping snapshots for the same term/day/campaign event CANNOT exist,
  so summing rows inside a window never multiplies the same fact.

Currency contract (PR-ADS-144):
  The table durably stores ``cost_micros`` (raw Google Ads micros),
  ``currency_code`` (native account currency, e.g. GBP), and
  ``source_system`` (data provenance). ``spend_usd`` is the legacy-column
  value (cost_micros / 1e6 at ingestion time — native currency, NOT proven
  USD despite the column name). Rows whose ``source_system`` is
  ``'google_ads_api'`` and ``currency_code`` is non-null have proven lineage;
  all other rows are quarantined for monetary metrics (unknown provenance).
  Per-date FX conversion to USD is performed in the evidence service using the
  same FX doctrine as canonical campaign spend.

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
    "source_date + COALESCE(campaign_name,'') + COALESCE(campaign_id,'') + "
    "COALESCE(ad_group,'') + COALESCE(keyword,'') + COALESCE(match_type,'') + "
    "search_term "
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

    Currency lineage (PR-ADS-144): each row includes aggregated cost_micros,
    the set of distinct currency_codes, and the set of distinct source_systems
    from the underlying fact rows so the service can assess provenance and
    perform FX conversion.

    Returns {available, rows, source: {row_count, spend_usd_total, clicks_total,
    impressions_total, conversions_total, distinct_source_dates,
    min_source_date, max_source_date, cost_micros_total,
    currency_codes, source_systems}}. Each row: {search_term, campaign_name,
    campaign_id, spend_usd, cost_micros, currency_codes, source_systems,
    clicks, impressions, conversions, row_count, first_seen, last_seen,
    any_flagged, any_unreviewed, junk_categories, matched_patterns, ad_groups,
    keywords, match_types}.

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
                           SUM(cost_micros)::bigint     AS cost_micros,
                           ARRAY_AGG(DISTINCT currency_code)
                               FILTER (WHERE currency_code IS NOT NULL)
                               AS currency_codes,
                           ARRAY_AGG(DISTINCT source_system)
                               FILTER (WHERE source_system IS NOT NULL)
                               AS source_systems,
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
                           SUM(cost_micros)::bigint    AS cost_micros_total,
                           SUM(clicks)::bigint         AS clicks_total,
                           SUM(impressions)::bigint    AS impressions_total,
                           SUM(conversions)            AS conversions_total,
                           COUNT(DISTINCT source_date) AS distinct_source_dates,
                           MIN(source_date)            AS min_source_date,
                           MAX(source_date)            AS max_source_date,
                           ARRAY_AGG(DISTINCT currency_code)
                               FILTER (WHERE currency_code IS NOT NULL)
                               AS currency_codes,
                           ARRAY_AGG(DISTINCT source_system)
                               FILTER (WHERE source_system IS NOT NULL)
                               AS source_systems
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
                row["cost_micros"] = (
                    int(row["cost_micros"]) if row["cost_micros"] is not None else None)
                row["currency_codes"] = sorted(row.get("currency_codes") or [])
                row["source_systems"] = sorted(row.get("source_systems") or [])
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
                    "cost_micros_total": (
                        int(source["cost_micros_total"])
                        if source.get("cost_micros_total") is not None else None),
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
                    "currency_codes": sorted(source.get("currency_codes") or []),
                    "source_systems": sorted(source.get("source_systems") or []),
                },
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_search_term_aggregates failed: %s", exc)
        return {"available": False, "rows": []}


def fetch_search_term_daily_costs(start: date | None, end: date) -> dict:
    """Per-(search_term, campaign_name, campaign_id, source_date) native cost
    for the whole selected window (PR-ADS-144 §2). Read-only.

    Returns the monetary facts at SOURCE-DATE grain so the evidence service can
    convert each day at its own FX rate (never a window-average rate) and then
    aggregate the resulting USD up to the term / campaign / pattern / KPI
    totals. Non-monetary aggregates still come from
    ``fetch_search_term_aggregates`` — this fetch exists purely to preserve the
    per-date native amounts that summing across the window would destroy.

    Each row: {search_term, campaign_name, campaign_id, source_date (date),
    cost_micros, currency_code, source_system}. currency_code / source_system
    are the DISTINCT sets within that (unit, day) so the service can withhold
    monetary metrics for mixed/unproven provenance. Never raises.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT search_term, campaign_name, campaign_id, source_date,
                           SUM(cost_micros)::bigint AS cost_micros,
                           ARRAY_AGG(DISTINCT currency_code)
                               FILTER (WHERE currency_code IS NOT NULL)
                               AS currency_codes,
                           ARRAY_AGG(DISTINCT source_system)
                               FILTER (WHERE source_system IS NOT NULL)
                               AS source_systems
                    FROM search_terms
                    WHERE (%s::date IS NULL OR source_date >= %s)
                      AND source_date <= %s
                    GROUP BY search_term, campaign_name, campaign_id, source_date
                    """,
                    (start, start, end),
                )
                rows = _rows_as_dicts(cur)
            for row in rows:
                row["cost_micros"] = (
                    int(row["cost_micros"]) if row["cost_micros"] is not None else None)
                row["currency_codes"] = sorted(row.get("currency_codes") or [])
                row["source_systems"] = sorted(row.get("source_systems") or [])
            return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_search_term_daily_costs failed: %s", exc)
        return {"available": False, "rows": []}


def fetch_search_term_daily_for_campaign(
    start: date | None, end: date, search_term: str,
    campaign_id: str | None = None,
    campaign_names: list | None = None,
    null_id_only: bool = False,
) -> dict:
    """Per-source_date evidence series for ONE search term scoped to ONE
    campaign identity (drawer). Read-only.

    Campaign scoping (PR-ADS-144 §3):
      - ``campaign_id`` set → only rows with that exact campaign_id, plus (when
        ``campaign_names`` is given) null-campaign_id rows whose name is in that
        set. The name fallback is passed by the caller ONLY when the label set
        uniquely identifies this campaign, so two ids sharing a display name
        never merge.
      - ``null_id_only=True`` → STRICTLY ``campaign_id IS NULL`` rows (matching
        ``campaign_names`` when given, else null-name rows). Used for a no-id
        (unmatched / legacy) unit so it can never pull an id-bearing campaign's
        rows that happen to share its display name.
      - neither → the term's full daily series (combined multi-campaign view).

    Only dates the source actually reported are returned — missing dates are
    NEVER fabricated as zero rows.

    Returns daily-level cost_micros and currency_code alongside spend_usd so
    the service can perform per-date FX conversion.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            conditions = ["(%s::date IS NULL OR source_date >= %s)",
                          "source_date <= %s", "search_term = %s"]
            params: list = [start, start, end, search_term]
            # Campaign-scoped identity: ID-first, name fallback only for
            # null-ID rows (never OR'd together to avoid same-name merge).
            if null_id_only:
                if campaign_names:
                    conditions.append(
                        "campaign_id IS NULL AND campaign_name = ANY(%s)")
                    params.append(list(campaign_names))
                else:
                    conditions.append(
                        "campaign_id IS NULL AND campaign_name IS NULL")
            elif campaign_id is not None:
                id_str = str(campaign_id).strip()
                if id_str:
                    identity_parts = ["campaign_id = %s"]
                    params.append(id_str)
                    if campaign_names:
                        identity_parts.append(
                            "(campaign_id IS NULL AND campaign_name = ANY(%s))")
                        params.append(list(campaign_names))
                    conditions.append("(" + " OR ".join(identity_parts) + ")")
            elif campaign_names:
                conditions.append("campaign_name = ANY(%s)")
                params.append(list(campaign_names))
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_date,
                           SUM(spend_usd)           AS spend_usd,
                           SUM(cost_micros)::bigint  AS cost_micros,
                           ARRAY_AGG(DISTINCT currency_code)
                               FILTER (WHERE currency_code IS NOT NULL)
                               AS currency_codes,
                           ARRAY_AGG(DISTINCT source_system)
                               FILTER (WHERE source_system IS NOT NULL)
                               AS source_systems,
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
                row["cost_micros"] = (
                    int(row["cost_micros"]) if row["cost_micros"] is not None else None)
                row["currency_codes"] = sorted(row.get("currency_codes") or [])
                row["source_systems"] = sorted(row.get("source_systems") or [])
                row["conversions"] = (
                    float(row["conversions"]) if row["conversions"] is not None else None)
                row["clicks"] = int(row["clicks"] or 0)
                row["impressions"] = int(row["impressions"] or 0)
            return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_search_term_daily_for_campaign failed: %s", exc)
        return {"available": False, "rows": []}


# Legacy compat alias — existing code that calls the old function signature.
def fetch_search_term_daily(start: date | None, end: date, search_term: str,
                            campaign_names: list | None = None,
                            campaign_ids: list | None = None) -> dict:
    """Backward-compatible wrapper — delegates to the campaign-scoped variant.

    When a single campaign_id is provided, uses ID-first scoping. Otherwise
    falls back to name-only scoping.
    """
    cid = None
    if campaign_ids and len(campaign_ids) == 1:
        cid = campaign_ids[0]
    return fetch_search_term_daily_for_campaign(
        start, end, search_term,
        campaign_id=cid,
        campaign_names=campaign_names,
    )


def fetch_legacy_currency_audit() -> dict:
    """Operator audit of legacy search_terms rows with unverified currency
    lineage (PR-ADS-145 §3). Read-only — SELECT only, never deletes/relabels.

    A row is legacy-unverified when it lacks provable Google Ads GBP lineage:
    ``source_system`` is not 'google_ads_api', OR ``currency_code`` IS NULL, OR
    ``cost_micros`` IS NULL. For each such row it reports whether an EXACT
    verified replacement exists — a row sharing the durable natural key
    (source_date, campaign_name, ad_group, keyword, match_type, search_term)
    that DOES have proven lineage. Returns {available, summary, rows}.
    """
    legacy_pred = ("(COALESCE(source_system,'') <> 'google_ads_api' "
                   "OR currency_code IS NULL OR cost_micros IS NULL)")
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "summary": {}, "rows": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)::bigint AS row_count,
                           COUNT(DISTINCT search_term) AS terms,
                           COUNT(DISTINCT COALESCE(campaign_name,'')) AS campaigns,
                           MIN(source_date) AS min_date,
                           MAX(source_date) AS max_date
                    FROM search_terms
                    WHERE """ + legacy_pred,
                )
                summ = _rows_as_dicts(cur)[0]
                cur.execute(
                    "SELECT DISTINCT campaign_name FROM search_terms WHERE "
                    + legacy_pred + " ORDER BY campaign_name")
                campaigns = [r[0] for r in cur.fetchall()]
                # Per-row detail + whether an exact verified replacement exists.
                cur.execute(
                    """
                    SELECT l.id, l.source_date, l.campaign_name, l.campaign_id,
                           l.ad_group, l.keyword, l.match_type, l.search_term,
                           l.currency_code, l.source_system,
                           EXISTS (
                               SELECT 1 FROM search_terms v
                               WHERE v.source_date = l.source_date
                                 AND COALESCE(v.campaign_name,'') = COALESCE(l.campaign_name,'')
                                 AND COALESCE(v.ad_group,'')      = COALESCE(l.ad_group,'')
                                 AND COALESCE(v.keyword,'')       = COALESCE(l.keyword,'')
                                 AND COALESCE(v.match_type,'')    = COALESCE(l.match_type,'')
                                 AND v.search_term = l.search_term
                                 AND v.source_system = 'google_ads_api'
                                 AND v.currency_code IS NOT NULL
                                 AND v.cost_micros IS NOT NULL
                           ) AS has_verified_replacement
                    FROM search_terms l
                    WHERE """ + legacy_pred + """
                    ORDER BY l.source_date, l.search_term
                    """,
                )
                rows = _rows_as_dicts(cur)
            for r in rows:
                r["source_date"] = (r["source_date"].isoformat()
                                    if r.get("source_date") is not None else None)
            return {
                "available": True,
                "summary": {
                    "legacy_unverified_row_count": int(summ.get("row_count") or 0),
                    "terms_represented": int(summ.get("terms") or 0),
                    "campaigns_represented": int(summ.get("campaigns") or 0),
                    "min_source_date": (summ["min_date"].isoformat()
                                        if summ.get("min_date") else None),
                    "max_source_date": (summ["max_date"].isoformat()
                                        if summ.get("max_date") else None),
                    "campaigns": [c for c in campaigns if c is not None],
                    "rows_with_verified_replacement": sum(
                        1 for r in rows if r.get("has_verified_replacement")),
                },
                "rows": rows,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_legacy_currency_audit failed: %s", exc)
        return {"available": False, "summary": {}, "rows": []}


def fetch_waste_evidence_for_terms(search_terms: list) -> dict:
    """Latest waste_terms evidence for a batch of search terms, keyed by
    (search_term, campaign_name) (PR-ADS-145 §4). Read-only.

    Bridges the weekly waste-detection pipeline's persisted evidence into the
    Search Terms page. waste_terms stores ``campaign_name`` but NOT
    ``campaign_id``, so the SERVICE is responsible for attaching a row only when
    the (term, campaign_name) uniquely and safely identifies one canonical
    campaign — this fetch merely returns the durable rows. Returns
    {available, rows:[{search_term, campaign_name, junk_category,
    matched_pattern, crm_junk_confirmed, run_date}]} with the latest row per
    (search_term, campaign_name). Never raises.
    """
    if not search_terms:
        return {"available": True, "rows": []}
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (search_term, campaign_name)
                           search_term, campaign_name, junk_category,
                           matched_pattern, crm_junk_confirmed, run_date
                    FROM waste_terms
                    WHERE search_term = ANY(%s)
                    ORDER BY search_term, campaign_name, run_date DESC, id DESC
                    """,
                    (list(search_terms),),
                )
                rows = _rows_as_dicts(cur)
            for row in rows:
                row["run_date"] = (
                    row["run_date"].isoformat()
                    if row.get("run_date") is not None else None)
                row["crm_junk_confirmed"] = (
                    int(row["crm_junk_confirmed"])
                    if row.get("crm_junk_confirmed") is not None else None)
            return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_waste_evidence_for_terms failed: %s", exc)
        return {"available": False, "rows": []}


def fetch_latest_waste_classification(
    search_term: str,
    campaign_id: str | None = None,
    campaign_names: list | None = None,
) -> dict:
    """Latest stored waste_terms classification evidence for one term, scoped
    to the selected campaign identity. Read-only.

    Campaign scoping (PR-ADS-144 §3):
      waste_terms stores ``campaign_name`` but not ``campaign_id``. When
      ``campaign_names`` is provided, only waste_terms rows whose
      ``campaign_name`` is in that set are considered. When the waste table
      cannot identify the campaign safely, classification proof is returned
      as unavailable rather than showing another campaign's evidence.

    waste_terms is the analysis-run output table (run_date grain) — used ONLY as
    supplementary classification proof in the drawer (junk category, matched
    rule, CRM-junk confirmation count, classification date), never as the
    selected-window business boundary. Returns {available, row|None}.
    """
    # ``campaign_id`` is accepted for call-site symmetry with the other
    # campaign-scoped fetchers, but waste_terms has NO campaign_id column, so
    # scoping is by campaign_names only (the service supplies the safe,
    # unambiguous label set). Explicitly unused here.
    del campaign_id
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "row": None}
            conditions = ["search_term = %s"]
            params: list = [search_term]
            if campaign_names:
                conditions.append("campaign_name = ANY(%s)")
                params.append(list(campaign_names))
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT search_term, campaign_name, junk_category,
                           matched_pattern, crm_junk_confirmed, run_date
                    FROM waste_terms
                    WHERE """ + " AND ".join(conditions) + """
                    ORDER BY run_date DESC, id DESC
                    LIMIT 1
                    """,
                    tuple(params),
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
