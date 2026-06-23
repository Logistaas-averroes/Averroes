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

# PR-ADS-119: HubSpot closed-won revenue is reported in USD, so Google Ads native
# spend is converted to USD for ROAS using daily FX rates.
FX_REPORTING_CURRENCY = "USD"

# HubSpot traffic-source pseudo-names that must never appear as ROAS campaigns.
_PSEUDO_CAMPAIGNS = (
    "(direct)", "(organic)", "(referral)", "(not set)",
    "(cross-network)", "(none)", "(content)", "(social)",
)

# Stable per-contact identity for dedup / distinct counts across run snapshots.
_CONTACT_KEY = "COALESCE(NULLIF(contact_id, ''), 'id:' || id::text)"


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
    """Leads + SQLs from the durable `leads` table — business-event-date safe.

    PR-ADS-109: filters by HubSpot contact_created_at (the business event date),
    NOT run_date (the scheduler/sync date), so recently-synced or backfilled old
    contacts are not counted inside the current window. Includes paid-search
    leads only, and excludes pseudo-/email campaigns.

    SQL = status_category 'qualified' (the existing lead-quality definition).

    Returns rows [{campaign_name, country, status_category, has_gclid}] plus date
    grain diagnostics (event_date_safe, missing_contact_created_at_count, etc.).
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable("leads", {"event_date_safe": False})
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH deduped AS (
                        SELECT DISTINCT ON ({_CONTACT_KEY})
                            campaign_name,
                            country,
                            status_category,
                            (gclid IS NOT NULL AND gclid <> '') AS has_gclid
                        FROM leads
                        WHERE source_type = 'paid_search'
                          AND contact_created_at IS NOT NULL
                          AND contact_created_at >= COALESCE(%s::timestamptz, contact_created_at)
                          AND contact_created_at < (%s::date + INTERVAL '1 day')
                          AND campaign_name IS NOT NULL
                          AND lower(campaign_name) NOT IN %s
                          AND campaign_name !~* 'email_campaign'
                          AND {_CONTACT_KEY} NOT IN (SELECT lead_id FROM lead_truth_exclusions)
                        ORDER BY {_CONTACT_KEY}, run_date DESC, id DESC
                    )
                    SELECT campaign_name, country, status_category, has_gclid
                    FROM deduped
                    """,
                    (start, end, _PSEUDO_CAMPAIGNS),
                )
                rows = _rows_as_dicts(cur)

                # INCLUDED paid contacts that have NO business event date in ANY
                # row (PR-ADS-115): audited legacy/orphan exclusions are NOT
                # counted as blockers — only included missing-date leads block.
                cur.execute(
                    f"""
                    SELECT COUNT(*) FROM (
                        SELECT {_CONTACT_KEY} AS k, MAX(contact_created_at) AS mc
                        FROM leads
                        WHERE source_type = 'paid_search'
                        GROUP BY 1
                    ) t
                    WHERE t.mc IS NULL
                      AND t.k NOT IN (SELECT lead_id FROM lead_truth_exclusions)
                    """
                )
                missing_event = cur.fetchone()[0]

                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM leads "
                    "WHERE source_type = 'paid_search' AND contact_created_at IS NOT NULL)"
                )
                has_event_date = bool(cur.fetchone()[0])

                # Non-paid contacts inside the window (excluded from ROAS rows).
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT {_CONTACT_KEY})
                    FROM leads
                    WHERE source_type IS DISTINCT FROM 'paid_search'
                      AND contact_created_at IS NOT NULL
                      AND contact_created_at >= COALESCE(%s::timestamptz, contact_created_at)
                      AND contact_created_at < (%s::date + INTERVAL '1 day')
                    """,
                    (start, end),
                )
                excluded_non_paid = cur.fetchone()[0]

                # Paid contacts inside the window with a pseudo / email / null campaign.
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT {_CONTACT_KEY})
                    FROM leads
                    WHERE source_type = 'paid_search'
                      AND contact_created_at IS NOT NULL
                      AND contact_created_at >= COALESCE(%s::timestamptz, contact_created_at)
                      AND contact_created_at < (%s::date + INTERVAL '1 day')
                      AND (campaign_name IS NULL
                           OR lower(campaign_name) IN %s
                           OR campaign_name ~* 'email_campaign')
                    """,
                    (start, end, _PSEUDO_CAMPAIGNS),
                )
                excluded_pseudo = cur.fetchone()[0]

            event_date_safe = (missing_event == 0)
            return {
                "available": True,
                "rows": rows,
                "table": "leads",
                "date_field": "contact_created_at",
                "event_date_safe": event_date_safe,
                "lead_event_date_field_available": has_event_date,
                "missing_contact_created_at_count": int(missing_event),
                "excluded_non_paid_count": int(excluded_non_paid),
                "excluded_pseudo_campaign_count": int(excluded_pseudo),
                "coverage_start": None,
                "coverage_end": None,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_lead_quality failed: %s", exc)
        return _unavailable("leads", {"event_date_safe": False})


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


def fetch_revenue_deals(start: date | None, end: date) -> dict:
    """Closed-won deal ledger rows from the durable `gclid_attribution` table.

    PR-ADS-113: powers the Closed-Won Revenue Ledger (/api/revenue-deals).
    Windowed by the real deal_close_date (NEVER the scheduler run_date) and
    deduped to the latest row per deal_id. Only closed-won deals count as
    revenue. Read-only.

    Returns rows [{deal_id, company, country, campaign_name, deal_close_date,
    deal_amount_usd, deal_stage_label, match_status, match_source}].
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable("gclid_attribution")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT deal_id, company, country, campaign_name,
                           deal_close_date,
                           deal_amount_usd::float AS deal_amount_usd,
                           deal_stage_label, match_status, match_source
                    FROM (
                        SELECT DISTINCT ON (deal_id)
                            deal_id, company, country, campaign_name,
                            deal_close_date, deal_amount_usd, deal_stage_label,
                            match_status, match_source, created_at
                        FROM gclid_attribution
                        WHERE deal_id IS NOT NULL
                          AND deal_close_date IS NOT NULL
                          AND (deal_stage = %s OR deal_stage_label ILIKE %s)
                          AND (%s::date IS NULL OR deal_close_date >= %s)
                          AND deal_close_date < (%s::date + INTERVAL '1 day')
                        ORDER BY deal_id, created_at DESC
                    ) d
                    ORDER BY deal_close_date DESC, deal_amount_usd DESC NULLS LAST
                    """,
                    (WON_DEAL_STAGE_ID, _WON_LABEL_LIKE, start, start, end),
                )
                rows = _rows_as_dicts(cur)
                for row in rows:
                    row["deal_close_date"] = _as_date(row.get("deal_close_date"))
            return {
                "available": True,
                "rows": rows,
                "table": "gclid_attribution",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_revenue_deals failed: %s", exc)
        return _unavailable("gclid_attribution")


def fetch_lead_date_grain_health(start: date | None, end: date) -> dict:
    """Lead date-grain + source-type diagnostics for the audit endpoint.

    Reuses fetch_lead_quality's safety counts and adds counts_by_source_type
    (distinct contacts in the window, by source_type). Read-only.
    """
    base = fetch_lead_quality(start, end)
    counts = {k: 0 for k in
              ("paid_search", "organic_search", "direct", "referral", "email", "other", "null")}
    try:
        with get_conn() as conn:
            if conn is not None:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT COALESCE(source_type, 'null') AS st,
                               COUNT(DISTINCT {_CONTACT_KEY}) AS n
                        FROM leads
                        WHERE contact_created_at IS NOT NULL
                          AND contact_created_at >= COALESCE(%s::timestamptz, contact_created_at)
                          AND contact_created_at < (%s::date + INTERVAL '1 day')
                        GROUP BY COALESCE(source_type, 'null')
                        """,
                        (start, end),
                    )
                    for st, n in cur.fetchall():
                        counts[st if st in counts else "other"] = (
                            counts.get(st if st in counts else "other", 0) + int(n)
                        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_lead_date_grain_health counts failed: %s", exc)

    return {
        "available": base.get("available", False),
        "lead_event_date_field_available": base.get("lead_event_date_field_available", False),
        "lead_window_safe": base.get("event_date_safe", False),
        "missing_contact_created_at_count": base.get("missing_contact_created_at_count", 0),
        "excluded_non_paid_count": base.get("excluded_non_paid_count", 0),
        "excluded_pseudo_campaign_count": base.get("excluded_pseudo_campaign_count", 0),
        "counts_by_source_type": counts,
    }


def fetch_campaign_pollution_report(start: date | None, end: date) -> dict:
    """Report campaign-universe pollution for the audit endpoint (read-only).

    - pseudo_campaign_rows / email_campaign_rows: pseudo or email campaign names
      still present in the leads table (should be empty — cleaned at write time).
    - zero_spend_campaigns_with_leads: paid-search lead campaigns in the window
      that have NO matching Google Ads spend in `geo` (legacy/UTM variants).
    """
    result = {"available": False, "pseudo_campaign_rows": [],
              "email_campaign_rows": [], "zero_spend_campaigns_with_leads": []}
    try:
        with get_conn() as conn:
            if conn is None:
                return result
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT campaign_name FROM leads "
                    "WHERE campaign_name IS NOT NULL AND lower(campaign_name) IN %s",
                    (_PSEUDO_CAMPAIGNS,),
                )
                result["pseudo_campaign_rows"] = [r[0] for r in cur.fetchall()]

                cur.execute(
                    "SELECT DISTINCT campaign_name FROM leads "
                    "WHERE campaign_name IS NOT NULL AND campaign_name ~* 'email_campaign'"
                )
                result["email_campaign_rows"] = [r[0] for r in cur.fetchall()]

                cur.execute(
                    f"""
                    SELECT DISTINCT l.campaign_name
                    FROM leads l
                    WHERE l.source_type = 'paid_search'
                      AND l.campaign_name IS NOT NULL
                      AND lower(l.campaign_name) NOT IN %s
                      AND l.campaign_name !~* 'email_campaign'
                      AND l.contact_created_at IS NOT NULL
                      AND l.contact_created_at >= COALESCE(%s::timestamptz, l.contact_created_at)
                      AND l.contact_created_at < (%s::date + INTERVAL '1 day')
                      AND NOT EXISTS (
                          SELECT 1 FROM geo g
                          WHERE lower(g.campaign_name) = lower(l.campaign_name)
                            AND (%s::date IS NULL OR g.run_date >= %s)
                            AND g.run_date <= %s
                      )
                    """,
                    (_PSEUDO_CAMPAIGNS, start, end, start, start, end),
                )
                result["zero_spend_campaigns_with_leads"] = [r[0] for r in cur.fetchall()]
            result["available"] = True
            return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_campaign_pollution_report failed: %s", exc)
        return result


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


# ── Lead event-date reconciliation (PR-ADS-115) ──────────────────────────────


def fetch_missing_event_date_leads() -> dict:
    """Paid leads missing an event date and not already excluded.

    Returns {available, rows:[{lead_key, contact_id}]} where lead_key is the
    stable contact identity (contact_id, or 'id:<rowid>' when no contact_id) —
    the same key the truth-gate counts. Read-only.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_CONTACT_KEY} AS lead_key,
                           MAX(NULLIF(contact_id, '')) AS contact_id
                    FROM leads
                    WHERE source_type = 'paid_search'
                    GROUP BY {_CONTACT_KEY}
                    HAVING MAX(contact_created_at) IS NULL
                    """
                )
                rows = _rows_as_dicts(cur)
                cur.execute("SELECT lead_id FROM lead_truth_exclusions")
                excluded = {r[0] for r in cur.fetchall()}
            rows = [r for r in rows if r.get("lead_key") not in excluded]
            return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_missing_event_date_leads failed: %s", exc)
        return {"available": False, "rows": []}


def revenue_integration_connected() -> bool:
    """True when ANY closed-won attributed deal exists (integration is wired),
    regardless of the selected business window.

    Lets the contract distinguish a connected integration whose current window is
    safely empty from an integration that was never wired. Read-only.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM gclid_attribution
                        WHERE deal_id IS NOT NULL
                          AND deal_close_date IS NOT NULL
                          AND (deal_stage = %s OR deal_stage_label ILIKE %s)
                    )
                    """,
                    (WON_DEAL_STAGE_ID, _WON_LABEL_LIKE),
                )
                return bool(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("revenue_integration_connected failed: %s", exc)
        return False


# ── Revenue by acquisition source (PR-ADS-117) ───────────────────────────────


def fetch_source_leads(start: date | None, end: date) -> dict:
    """Classified contacts in the window, by acquisition group. Read-only.

    Windowed by contact_created_at (the business event date). Returns
    {available, rows:[{acquisition_group, status_category}]}.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT acquisition_group, status_category
                    FROM contact_source_classification
                    WHERE contact_created_at IS NOT NULL
                      AND contact_created_at >= COALESCE(%s::timestamptz, contact_created_at)
                      AND contact_created_at < (%s::date + INTERVAL '1 day')
                    """,
                    (start, end),
                )
                return {"available": True, "rows": _rows_as_dicts(cur)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_source_leads failed: %s", exc)
        return {"available": False, "rows": []}


def fetch_source_revenue(start: date | None, end: date) -> dict:
    """Closed-won deals in the window, by acquisition group. Read-only.

    Windowed by deal_close_date. Each deal appears once (deal_id is unique in
    deal_source_attribution). Returns {available, rows:[{acquisition_group,
    attribution_status, deal_amount_usd}]}.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT acquisition_group, attribution_status,
                           deal_amount_usd::float AS deal_amount_usd
                    FROM deal_source_attribution
                    WHERE deal_close_date IS NOT NULL
                      AND deal_close_date >= COALESCE(%s::timestamptz, deal_close_date)
                      AND deal_close_date < (%s::date + INTERVAL '1 day')
                    """,
                    (start, end),
                )
                return {"available": True, "rows": _rows_as_dicts(cur)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_source_revenue failed: %s", exc)
        return {"available": False, "rows": []}


# ── Canonical Google Ads spend truth (PR-ADS-118) ────────────────────────────


def fetch_canonical_campaign_spend(start: date | None, end: date) -> dict:
    """Per-campaign canonical Google Ads spend from google_ads_campaign_daily_spend.

    Spend truth read DIRECTLY from the canonical table (never the geo table).
    Aggregates raw cost_micros (no early rounding). Read-only.

    PR-ADS-119: each daily spend row is converted to USD using the FX rate for
    its OWN spend_date (LEFT JOIN fx_rates) — never one spot rate for the whole
    window. ``fx_complete`` is False when any spend_date lacks an FX rate, which
    must block ROAS (USD spend is only trustworthy when FX coverage is complete).

    Returns {available, rows:[{campaign_id, campaign_name, cost_micros, spend,
             spend_usd, fx_complete}], total_cost_micros, total_spend (native),
             total_spend_usd, campaign_count, customer_id, currency_code (native),
             reporting_currency, fx_missing_days, fx_complete, coverage_start,
             coverage_end}.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rows": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.campaign_id, MAX(s.campaign_name) AS campaign_name,
                           SUM(s.cost_micros)::bigint AS cost_micros,
                           SUM((s.cost_micros / 1000000.0) * fx.rate) AS spend_usd,
                           COUNT(DISTINCT CASE WHEN fx.rate IS NULL THEN s.spend_date END)
                               AS fx_missing_days
                    FROM google_ads_campaign_daily_spend s
                    LEFT JOIN fx_rates fx
                      ON fx.rate_date = s.spend_date
                     AND fx.base_currency = s.currency_code
                     AND fx.quote_currency = %s
                    WHERE (%s::date IS NULL OR s.spend_date >= %s) AND s.spend_date <= %s
                    GROUP BY s.campaign_id
                    """,
                    (FX_REPORTING_CURRENCY, start, start, end),
                )
                rows = []
                total = 0
                total_usd = 0.0
                for r in _rows_as_dicts(cur):
                    micros = int(r.get("cost_micros") or 0)
                    total += micros
                    row_missing = int(r.get("fx_missing_days") or 0)
                    usd = None if row_missing else float(r.get("spend_usd") or 0.0)
                    if usd is not None:
                        total_usd += usd
                    rows.append({
                        "campaign_id": r.get("campaign_id"),
                        "campaign_name": r.get("campaign_name"),
                        "cost_micros": micros,
                        "spend": micros / 1_000_000,
                        "spend_usd": usd,
                        "fx_complete": row_missing == 0,
                    })
                cur.execute(
                    """
                    SELECT MIN(spend_date) AS cstart, MAX(spend_date) AS cend,
                           MIN(customer_id) AS customer_id, MIN(currency_code) AS currency
                    FROM google_ads_campaign_daily_spend
                    WHERE (%s::date IS NULL OR spend_date >= %s) AND spend_date <= %s
                    """,
                    (start, start, end),
                )
                meta = cur.fetchone() or (None, None, None, None)
                cur.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT DISTINCT s.spend_date
                        FROM google_ads_campaign_daily_spend s
                        LEFT JOIN fx_rates fx
                          ON fx.rate_date = s.spend_date
                         AND fx.base_currency = s.currency_code
                         AND fx.quote_currency = %s
                        WHERE (%s::date IS NULL OR s.spend_date >= %s) AND s.spend_date <= %s
                          AND fx.rate IS NULL
                    ) m
                    """,
                    (FX_REPORTING_CURRENCY, start, start, end),
                )
                fx_missing_days = int((cur.fetchone() or (0,))[0] or 0)
            fx_complete = fx_missing_days == 0
            return {
                "available": True,
                "rows": rows,
                "total_cost_micros": total,
                "total_spend": total / 1_000_000,
                "total_spend_usd": round(total_usd, 6) if fx_complete else None,
                "campaign_count": len(rows),
                "customer_id": meta[2],
                "currency_code": meta[3],
                "reporting_currency": FX_REPORTING_CURRENCY,
                "fx_missing_days": fx_missing_days,
                "fx_complete": fx_complete,
                "coverage_start": _as_date(meta[0]),
                "coverage_end": _as_date(meta[1]),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_canonical_campaign_spend failed: %s", exc)
        return {"available": False, "rows": []}


def fetch_spend_coverage(start: date | None, end: date) -> dict:
    """Verified/failed Google Ads spend chunks intersecting the window. Read-only.

    Returns {available, chunks:[{chunk_start, chunk_end, status, rows_written,
             cost_micros_total}]}.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "chunks": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_start, chunk_end, status, rows_written, cost_micros_total
                    FROM google_ads_spend_coverage
                    WHERE chunk_end >= COALESCE(%s::date, chunk_end) AND chunk_start <= %s
                    ORDER BY chunk_start
                    """,
                    (start, end),
                )
                chunks = []
                for r in _rows_as_dicts(cur):
                    chunks.append({
                        "chunk_start": _as_date(r.get("chunk_start")),
                        "chunk_end": _as_date(r.get("chunk_end")),
                        "status": r.get("status"),
                        "rows_written": r.get("rows_written"),
                        "cost_micros_total": int(r.get("cost_micros_total") or 0),
                    })
            return {"available": True, "chunks": chunks}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_spend_coverage failed: %s", exc)
        return {"available": False, "chunks": []}


def fetch_geo_spend_total(start: date | None, end: date) -> dict:
    """Total legacy geo-table spend for the window (for reconciliation). Read-only."""
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "total_spend": 0.0}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(spend_usd), 0)::float FROM (
                        SELECT DISTINCT ON (campaign_name, country, run_date)
                               run_date, spend_usd
                        FROM geo
                        WHERE (%s::date IS NULL OR run_date >= %s) AND run_date <= %s
                        ORDER BY campaign_name, country, run_date, run_id DESC
                    ) d
                    """,
                    (start, start, end),
                )
                total = float(cur.fetchone()[0] or 0.0)
            return {"available": True, "total_spend": total}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_geo_spend_total failed: %s", exc)
        return {"available": False, "total_spend": 0.0}


def fetch_fx_coverage(start: date | None, end: date,
                      base_currency: str, quote_currency: str = FX_REPORTING_CURRENCY) -> dict:
    """FX coverage over the canonical spend dates in the window (PR-ADS-119).

    Returns {available, complete, spend_days, covered_days, missing_dates}. A
    spend_date with no fx_rates row is reported missing — never converted at a
    wrong rate. With no spend dates, coverage is trivially complete (nothing to
    convert). Read-only.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "complete": False, "missing_dates": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.spend_date,
                           MAX(CASE WHEN fx.rate IS NOT NULL THEN 1 ELSE 0 END) AS has_rate
                    FROM (
                        SELECT DISTINCT spend_date, currency_code
                        FROM google_ads_campaign_daily_spend
                        WHERE (%s::date IS NULL OR spend_date >= %s) AND spend_date <= %s
                    ) s
                    LEFT JOIN fx_rates fx
                      ON fx.rate_date = s.spend_date
                     AND fx.base_currency = COALESCE(s.currency_code, %s)
                     AND fx.quote_currency = %s
                    GROUP BY s.spend_date
                    ORDER BY s.spend_date
                    """,
                    (start, start, end, base_currency, quote_currency),
                )
                spend_days = 0
                covered = 0
                missing = []
                for r in _rows_as_dicts(cur):
                    spend_days += 1
                    if int(r.get("has_rate") or 0) == 1:
                        covered += 1
                    else:
                        missing.append(_as_date(r.get("spend_date")))
            return {
                "available": True,
                "complete": not missing,
                "spend_days": spend_days,
                "covered_days": covered,
                "missing_dates": missing,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_fx_coverage failed: %s", exc)
        return {"available": False, "complete": False, "missing_dates": []}


def fetch_fx_rates(start: date | None, end: date,
                   base_currency: str, quote_currency: str = FX_REPORTING_CURRENCY) -> dict:
    """Daily FX rates for the window keyed by ISO date string (PR-ADS-119). Read-only."""
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "rates": {}}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rate_date, rate, provider, source_version
                    FROM fx_rates
                    WHERE base_currency = %s AND quote_currency = %s
                      AND (%s::date IS NULL OR rate_date >= %s) AND rate_date <= %s
                    ORDER BY rate_date
                    """,
                    (base_currency, quote_currency, start, start, end),
                )
                rates = {}
                provider = None
                for r in _rows_as_dicts(cur):
                    d = _as_date(r.get("rate_date"))
                    if d:
                        rates[d] = float(r.get("rate"))
                    provider = r.get("provider") or provider
            return {"available": True, "rates": rates, "provider": provider}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_fx_rates failed: %s", exc)
        return {"available": False, "rates": {}}


def fetch_campaign_identity(customer_id: str | None = None) -> dict:
    """Approved campaign-identity mappings keyed by normalized external label.

    PR-ADS-119: external HubSpot/UTM label -> canonical Google Ads campaign. Only
    rows with approved_at set are returned (manual + auto-approved exact matches).
    Read-only — the raw spend-table identity is never modified here.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "mappings": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT customer_id, campaign_id, canonical_campaign_name,
                           historical_campaign_name, external_campaign_label,
                           match_method, approved_at, approved_by
                    FROM google_ads_campaign_identity
                    WHERE approved_at IS NOT NULL
                      AND (%s::text IS NULL OR customer_id = %s)
                    """,
                    (customer_id, customer_id),
                )
                mappings = []
                for r in _rows_as_dicts(cur):
                    mappings.append({
                        "customer_id": r.get("customer_id"),
                        "campaign_id": r.get("campaign_id"),
                        "canonical_campaign_name": r.get("canonical_campaign_name"),
                        "historical_campaign_name": r.get("historical_campaign_name"),
                        "external_campaign_label": r.get("external_campaign_label"),
                        "match_method": r.get("match_method"),
                        "approved_at": str(r.get("approved_at")) if r.get("approved_at") else None,
                        "approved_by": r.get("approved_by"),
                    })
            return {"available": True, "mappings": mappings}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_campaign_identity failed: %s", exc)
        return {"available": False, "mappings": []}
