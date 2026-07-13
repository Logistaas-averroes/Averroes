"""
Keyword evidence repository (PR-ADS-146). Read-only.

Durable fetchers over the ``keyword_daily_facts`` fact table for the Keyword
Evidence page. All selected-window boundaries use ``source_date`` (the Google
Ads reporting date), NEVER ``run_date`` — scheduler timing must not move
business totals, and overlapping scheduler snapshots must never be SUM'd.

Natural-key / duplication contract:
  The table enforces a UNIQUE index ``idx_keyword_daily_facts_unique`` on
  (source_date, COALESCE(customer_id,''), COALESCE(campaign_id,''),
   COALESCE(ad_group_id,''), COALESCE(criterion_id,'')) and the writer
  (``db.writers.write_keyword_daily_facts``) upserts ON CONFLICT on that same
  key. The key is immutable Google Ads identity — a repeated scheduler pull for
  the same fact UPDATES the row in place, so summing rows inside a window never
  multiplies the same fact, and two campaigns / ad groups / criteria sharing a
  display name stay separate.

Currency contract:
  The table durably stores ``cost_micros`` (raw Google Ads micros),
  ``currency_code`` (native account currency, e.g. GBP), and ``source_system``
  (provenance). Rows whose ``source_system`` is ``'google_ads_api'`` with a
  non-null ``currency_code`` have proven lineage; all others are quarantined for
  monetary metrics. Per-date FX conversion is performed in the evidence service.

Quality contract:
  Quality diagnostics (quality_score, expected_ctr, ad_relevance,
  landing_page_experience) are LATEST-OBSERVED keyword attributes. The
  aggregate fetch returns the most recent NON-NULL observation per criterion —
  never an average across snapshots. A NULL means unavailable and is kept
  distinct from a genuine 0.

The legacy ``keywords`` snapshot table is only read by the audit fetcher (for
comparison counts) and is never reinterpreted as durable USD.

No writes. No Google Ads / HubSpot calls.
"""

from __future__ import annotations

import logging
from datetime import date

from db.connection import get_conn

logger = logging.getLogger(__name__)

# The documented durable natural key (disclosed verbatim by the audit layer).
KEYWORD_DAILY_FACTS_NATURAL_KEY = (
    "source_date + COALESCE(customer_id,'') + COALESCE(campaign_id,'') + "
    "COALESCE(ad_group_id,'') + COALESCE(criterion_id,'') "
    "(UNIQUE index idx_keyword_daily_facts_unique; writer upserts ON CONFLICT)"
)

# Selected-window predicate: NULL start = all_time (no lower bound); source_date
# only, never run_date.
_WINDOW = "(%s::date IS NULL OR source_date >= %s) AND source_date <= %s"


def _rows_as_dicts(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_keyword_aggregates(start: date | None, end: date) -> dict:
    """Window aggregates at the unique-criterion grain
    (customer_id, campaign_id, ad_group_id, criterion_id).

    Each row sums durable source-date facts ONCE (the natural key guarantees no
    duplicate facts), carries the currency/source provenance sets for the unit,
    and exposes the LATEST-OBSERVED identity + quality (most recent non-null
    quality observation) via ordered ARRAY_AGG — never a snapshot average.

    Returns ``{available, rows, source}`` (source = window totals for
    reconciliation) or ``{available: False}`` when the DB is unavailable.
    """
    agg_sql = f"""
        SELECT
          customer_id, campaign_id, ad_group_id, criterion_id,
          SUM(cost_micros)::bigint            AS cost_micros,
          SUM(clicks)::bigint                 AS clicks,
          SUM(impressions)::bigint            AS impressions,
          SUM(conversions)::numeric           AS conversions,
          MIN(source_date)                    AS first_seen,
          MAX(source_date)                    AS last_seen,
          COUNT(*)::int                       AS fact_rows,
          ARRAY_AGG(DISTINCT currency_code) FILTER (WHERE currency_code IS NOT NULL) AS currency_codes,
          ARRAY_AGG(DISTINCT source_system) FILTER (WHERE source_system IS NOT NULL) AS source_systems,
          -- Latest-observed identity (most recent source_date wins).
          (ARRAY_AGG(campaign_name    ORDER BY source_date DESC, id DESC))[1] AS campaign_name,
          (ARRAY_AGG(ad_group_name    ORDER BY source_date DESC, id DESC))[1] AS ad_group_name,
          (ARRAY_AGG(keyword_text     ORDER BY source_date DESC, id DESC))[1] AS keyword_text,
          (ARRAY_AGG(match_type       ORDER BY source_date DESC, id DESC))[1] AS match_type,
          (ARRAY_AGG(criterion_status ORDER BY source_date DESC, id DESC))[1] AS criterion_status,
          -- Latest quality observation selected by quality_observed_at (the genuine
          -- observation time), NEVER by the activity source_date. 0 is a real
          -- value; NULL = unavailable.
          (ARRAY_AGG(quality_score ORDER BY quality_observed_at DESC NULLS LAST, id DESC)
             FILTER (WHERE quality_score IS NOT NULL))[1] AS quality_score,
          (ARRAY_AGG(expected_ctr ORDER BY quality_observed_at DESC NULLS LAST, id DESC)
             FILTER (WHERE expected_ctr IS NOT NULL))[1] AS expected_ctr,
          (ARRAY_AGG(ad_relevance ORDER BY quality_observed_at DESC NULLS LAST, id DESC)
             FILTER (WHERE ad_relevance IS NOT NULL))[1] AS ad_relevance,
          (ARRAY_AGG(landing_page_experience ORDER BY quality_observed_at DESC NULLS LAST, id DESC)
             FILTER (WHERE landing_page_experience IS NOT NULL))[1] AS landing_page_experience,
          MAX(quality_observed_at) FILTER (WHERE quality_score IS NOT NULL) AS quality_observed_at
        FROM keyword_daily_facts
        WHERE {_WINDOW}
        GROUP BY customer_id, campaign_id, ad_group_id, criterion_id
        ORDER BY SUM(cost_micros) DESC NULLS LAST
    """
    totals_sql = f"""
        SELECT
          COUNT(*)::int AS fact_rows,
          COUNT(DISTINCT (customer_id, campaign_id, ad_group_id, criterion_id))::int
                        AS unique_criteria,
          SUM(cost_micros)::bigint  AS cost_micros_total,
          SUM(clicks)::bigint       AS clicks_total,
          SUM(impressions)::bigint  AS impressions_total,
          SUM(conversions)::numeric AS conversions_total,
          COUNT(DISTINCT source_date)::int AS distinct_source_dates,
          MIN(source_date) AS min_source_date,
          MAX(source_date) AS max_source_date,
          ARRAY_AGG(DISTINCT currency_code) FILTER (WHERE currency_code IS NOT NULL) AS currency_codes,
          ARRAY_AGG(DISTINCT source_system) FILTER (WHERE source_system IS NOT NULL) AS source_systems
        FROM keyword_daily_facts
        WHERE {_WINDOW}
    """
    params = (start, start, end)
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": [], "source": {}}
            with conn.cursor() as cur:
                cur.execute(agg_sql, params)
                rows = _rows_as_dicts(cur)
                cur.execute(totals_sql, params)
                source = (_rows_as_dicts(cur) or [{}])[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_keyword_aggregates failed: %s", exc)
        return {"available": False, "rows": [], "source": {}}

    for r in rows:
        r["currency_codes"] = list(r.get("currency_codes") or [])
        r["source_systems"] = list(r.get("source_systems") or [])
        if r.get("conversions") is not None:
            r["conversions"] = float(r["conversions"])
        if r.get("quality_score") is not None:
            r["quality_score"] = int(r["quality_score"])
    if source:
        source["currency_codes"] = list(source.get("currency_codes") or [])
        source["source_systems"] = list(source.get("source_systems") or [])
    return {"available": True, "rows": rows, "source": source}


def fetch_keyword_daily_costs(start: date | None, end: date) -> dict:
    """Per-(criterion, source_date) native cost so the service can FX-convert at
    EACH date's own rate (never a window-average). Returns ``{available, rows}``.
    """
    sql = f"""
        SELECT
          customer_id, campaign_id, ad_group_id, criterion_id, source_date,
          SUM(cost_micros)::bigint AS cost_micros,
          ARRAY_AGG(DISTINCT currency_code) FILTER (WHERE currency_code IS NOT NULL) AS currency_codes,
          ARRAY_AGG(DISTINCT source_system) FILTER (WHERE source_system IS NOT NULL) AS source_systems
        FROM keyword_daily_facts
        WHERE {_WINDOW}
        GROUP BY customer_id, campaign_id, ad_group_id, criterion_id, source_date
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(sql, (start, start, end))
                rows = _rows_as_dicts(cur)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_keyword_daily_costs failed: %s", exc)
        return {"available": False, "rows": []}
    for r in rows:
        r["currency_codes"] = list(r.get("currency_codes") or [])
        r["source_systems"] = list(r.get("source_systems") or [])
    return {"available": True, "rows": rows}


def fetch_keyword_daily_for_criterion(
    start: date | None,
    end: date,
    *,
    criterion_id: str | None,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    customer_id: str | None = None,
) -> dict:
    """Drawer daily series scoped to ONE keyword criterion by immutable id — never
    borrows another criterion's rows. Returns ``{available, rows}`` ordered by
    source_date ASC with native cost + currency/source per day.
    """
    conds = [_WINDOW]
    params: list = [start, start, end]
    # Scope strictly by the immutable identity columns provided.
    for col, val in (("criterion_id", criterion_id), ("campaign_id", campaign_id),
                     ("ad_group_id", ad_group_id), ("customer_id", customer_id)):
        if val is not None:
            conds.append(f"COALESCE({col}, '') = %s")
            params.append(str(val))
    sql = f"""
        SELECT
          source_date,
          SUM(cost_micros)::bigint AS cost_micros,
          SUM(clicks)::bigint      AS clicks,
          SUM(impressions)::bigint AS impressions,
          SUM(conversions)::numeric AS conversions,
          ARRAY_AGG(DISTINCT currency_code) FILTER (WHERE currency_code IS NOT NULL) AS currency_codes,
          ARRAY_AGG(DISTINCT source_system) FILTER (WHERE source_system IS NOT NULL) AS source_systems
        FROM keyword_daily_facts
        WHERE {' AND '.join(conds)}
        GROUP BY source_date
        ORDER BY source_date ASC
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = _rows_as_dicts(cur)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_keyword_daily_for_criterion failed: %s", exc)
        return {"available": False, "rows": []}
    for r in rows:
        r["currency_codes"] = list(r.get("currency_codes") or [])
        r["source_systems"] = list(r.get("source_systems") or [])
        if r.get("conversions") is not None:
            r["conversions"] = float(r["conversions"])
        # Normalize to ISO string so the shared _convert_daily_series can match
        # per-date FX rates (keyed by ISO date) — same contract as search terms.
        sd = r.get("source_date")
        r["source_date"] = sd.isoformat() if hasattr(sd, "isoformat") else (
            str(sd) if sd is not None else None)
    return {"available": True, "rows": rows}


def fetch_keyword_legacy_audit() -> dict:
    """Read-only operator audit (SELECT only) of durable vs legacy keyword data.

    Reports durable-fact health and the legacy ``keywords`` snapshot count so an
    operator can verify the rebuild without touching any row:
      * durable rows + distinct criteria + earliest/latest durable source dates;
      * rows with missing immutable IDs (would fall back to the '' legacy key);
      * rows with unverified currency lineage (quarantined from USD);
      * duplicate-key candidates (should be 0 given the unique index);
      * legacy snapshot rows in the untouched ``keywords`` table.

    Returns ``{available, summary}`` or ``{available: False}``.
    """
    summary_sql = """
        SELECT
          COUNT(*)::int AS durable_rows,
          COUNT(DISTINCT (customer_id, campaign_id, ad_group_id, criterion_id))::int
                        AS durable_criteria,
          MIN(source_date) AS durable_source_date_min,
          MAX(source_date) AS durable_source_date_max,
          COUNT(*) FILTER (
              WHERE COALESCE(campaign_id,'')  = ''
                 OR COALESCE(ad_group_id,'')  = ''
                 OR COALESCE(criterion_id,'') = ''
          )::int AS rows_missing_ids,
          COUNT(*) FILTER (
              WHERE COALESCE(source_system,'') <> 'google_ads_api'
                 OR currency_code IS NULL
                 OR cost_micros IS NULL
          )::int AS rows_unverified_currency,
          COUNT(*) FILTER (WHERE quality_score IS NOT NULL)::int AS rows_with_quality,
          COUNT(DISTINCT source_date)::int AS distinct_source_dates
        FROM keyword_daily_facts
    """
    # Duplicate-key candidates: the unique index makes this 0, but the audit
    # proves it rather than assuming it.
    dup_sql = """
        SELECT COUNT(*)::int AS duplicate_key_groups FROM (
            SELECT 1
            FROM keyword_daily_facts
            GROUP BY source_date, COALESCE(customer_id,''), COALESCE(campaign_id,''),
                     COALESCE(ad_group_id,''), COALESCE(criterion_id,'')
            HAVING COUNT(*) > 1
        ) d
    """
    legacy_sql = """
        SELECT
          COUNT(*)::int AS legacy_snapshot_rows,
          COUNT(DISTINCT run_id)::int AS legacy_distinct_runs,
          MIN(run_date) AS legacy_run_date_min,
          MAX(run_date) AS legacy_run_date_max
        FROM keywords
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False}
            with conn.cursor() as cur:
                cur.execute(summary_sql)
                summary = (_rows_as_dicts(cur) or [{}])[0]
                cur.execute(dup_sql)
                summary.update((_rows_as_dicts(cur) or [{}])[0])
                try:
                    cur.execute(legacy_sql)
                    summary.update((_rows_as_dicts(cur) or [{}])[0])
                except Exception:  # noqa: BLE001 - legacy table may be absent
                    conn.rollback()
                    summary.setdefault("legacy_snapshot_rows", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_keyword_legacy_audit failed: %s", exc)
        return {"available": False}
    for k in ("durable_source_date_min", "durable_source_date_max",
              "legacy_run_date_min", "legacy_run_date_max"):
        v = summary.get(k)
        if isinstance(v, date):
            summary[k] = v.isoformat()
    summary["natural_key"] = KEYWORD_DAILY_FACTS_NATURAL_KEY
    return {"available": True, "summary": summary}
