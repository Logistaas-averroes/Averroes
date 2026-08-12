"""
db/writers.py

Database write functions for the Logistaas Ads Intelligence System.

Responsibility:
  - Write structured run data to the database after each scheduler step.
  - All functions are non-fatal: DB write failures are logged and swallowed —
    the scheduler must never abort because of a database failure.
  - JSON file writes in the schedulers are NOT replaced; this is additive.

MQL status → status_category mapping:
  qualified    — CLOSED - Sales Qualified, CLOSED - Deal Created
  in_progress  — OPEN - Meeting Booked, OPEN - Pending Meeting
  junk         — CLOSED - Job Seeker, DICARDED   (one R — canonical spelling)
  wrong_fit    — CLOSED - Bad Product Fit, CLOSED - Sales Disqualified
  unknown      — everything else
"""

import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

from db.connection import get_conn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MQL status → status_category
# ---------------------------------------------------------------------------

_QUALIFIED    = {"CLOSED - Sales Qualified", "CLOSED - Deal Created"}
_IN_PROGRESS  = {"OPEN - Meeting Booked", "OPEN - Pending Meeting"}
_JUNK         = {"CLOSED - Job Seeker", "DICARDED"}   # one R — canonical
_WRONG_FIT    = {"CLOSED - Bad Product Fit", "CLOSED - Sales Disqualified"}


def _map_status_category(mql_status: Optional[str]) -> str:
    if not mql_status:
        return "unknown"
    if mql_status in _QUALIFIED:
        return "qualified"
    if mql_status in _IN_PROGRESS:
        return "in_progress"
    if mql_status in _JUNK:
        return "junk"
    if mql_status in _WRONG_FIT:
        return "wrong_fit"
    return "unknown"


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_ts(value):
    """Parse a HubSpot timestamp (ISO string or epoch-ms) to a datetime, or None.

    Used to persist the real business event date (contact createdate). Returns
    None for missing/invalid values so the row is stored with a NULL event date
    and flagged unsafe by the revenue-attribution audit rather than miscounted.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.isdigit() and len(text) >= 12:  # epoch milliseconds
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Campaign name normalisation and source type helpers
# ---------------------------------------------------------------------------

_EMAIL_CAMPAIGN_PATTERN = re.compile(r"EMAIL_CAMPAIGN", re.IGNORECASE)

# HubSpot traffic source values that appear as campaign_name — not real campaigns
_HUBSPOT_SOURCE_PSEUDONAMES = {
    "(referral)", "(organic)", "(direct)", "(not set)",
    "(cross-network)", "(none)", "(content)", "(social)",
}

# Canonical campaign name map: Windsor variant → canonical name
# Canonical = HubSpot UTM convention (how the name appears in lead attribution)
# Note: "mexico, chile, colombia" maps to "mexico,chile" — HubSpot UTM tracks
# this campaign without Colombia in the name; both names refer to the same campaign.
_CAMPAIGN_CANONICAL = {
    "mexico, chile, colombia":  "mexico,chile",
    "compliance markets":       "compliance - markets",
    "emerging markets":         "emerging - markets",
    "mature markets":           "mature - markets",
    "europe low-cpc-2026":      "europe low cpc-new",
    # Add new entries here as campaigns are renamed in Windsor
}


def _canonicalise_campaign_name(name: Optional[str]) -> Optional[str]:
    """Apply canonical name mapping after lowercasing.

    Call this AFTER _clean_campaign_name() — input is already lowercase.
    """
    if name is None:
        return None
    return _CAMPAIGN_CANONICAL.get(name, name)


def _clean_campaign_name(campaign_name: Optional[str]) -> Optional[str]:
    """Normalise campaign name for consistent storage.

    - Lowercase and strip whitespace
    - Return None for HubSpot traffic source pseudo-names
    - Return None for HubSpot email campaign ID strings
    - Return None for empty/null values
    - Apply canonical name mapping (Windsor → HubSpot UTM)
    """
    if not campaign_name:
        return None

    stripped = campaign_name.strip()
    if not stripped:
        return None

    if stripped.lower() in _HUBSPOT_SOURCE_PSEUDONAMES:
        return None

    if _EMAIL_CAMPAIGN_PATTERN.search(stripped):
        return None

    return _canonicalise_campaign_name(stripped.lower())


def _map_source_type(hs_source: str, campaign_name: Optional[str]) -> str:
    """Map HubSpot hs_analytics_source to a clean source_type category.

    HubSpot source values (confirmed from live account):
      PAID_SEARCH       → paid_search
      ORGANIC_SEARCH    → organic_search
      REFERRALS         → referral
      DIRECT_TRAFFIC    → direct
      EMAIL_MARKETING   → email
      (anything else)   → other
    """
    s = (hs_source or "").upper().strip()

    if s == "PAID_SEARCH":
        return "paid_search"
    if s == "ORGANIC_SEARCH":
        return "organic_search"
    if s in ("REFERRALS", "REFERRAL"):
        return "referral"
    if s in ("DIRECT_TRAFFIC", "DIRECT"):
        return "direct"
    if s == "EMAIL_MARKETING" or (
        # Also detect email if the raw campaign_name contains EMAIL_CAMPAIGN —
        # this catches cases where hs_analytics_source is absent/empty but the
        # campaign_name itself reveals it's an email send.  _clean_campaign_name()
        # will still return None for these, but source_type is set first.
        campaign_name and _EMAIL_CAMPAIGN_PATTERN.search(campaign_name)
    ):
        return "email"
    return "other"


# ---------------------------------------------------------------------------
# Public write functions
# ---------------------------------------------------------------------------

def write_run(run_data: dict) -> Optional[int]:
    """Insert a run record and return its auto-generated run_id.

    Returns None if the database is unavailable or the write fails.
    Never raises.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO runs (
                        run_type, started_at, finished_at, status,
                        failed_step, error_message, report_path,
                        delivery_attempted, delivery_success
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        run_data.get("run_type"),
                        run_data.get("started_at"),
                        run_data.get("finished_at"),
                        run_data.get("status", "failed"),
                        run_data.get("failed_step"),
                        run_data.get("error_message"),
                        run_data.get("report_path"),
                        bool(run_data.get("delivery_attempted", False)),
                        run_data.get("delivery_success"),
                    ),
                )
                row = cur.fetchone()
                run_id = row[0] if row else None
                log.info("Wrote run record to database — run_id=%s", run_id)
                return run_id
    except Exception as exc:  # noqa: BLE001
        log.error("write_run failed: %s", exc)
        return None


def update_run(run_id: int, run_data: dict) -> None:
    """Update an existing run record with final status, finished_at, and delivery fields.

    Called after finish_run() so the DB row reflects the true final state of the run.
    Never raises.
    """
    if run_id is None:
        return
    try:
        with get_conn() as conn:
            if conn is None:
                return
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE runs
                    SET finished_at        = %s,
                        status             = %s,
                        failed_step        = %s,
                        error_message      = %s,
                        report_path        = %s,
                        delivery_attempted = %s,
                        delivery_success   = %s
                    WHERE id = %s
                    """,
                    (
                        run_data.get("finished_at"),
                        run_data.get("status", "failed"),
                        run_data.get("failed_step"),
                        run_data.get("error_message"),
                        run_data.get("report_path"),
                        bool(run_data.get("delivery_attempted", False)),
                        run_data.get("delivery_success"),
                        run_id,
                    ),
                )
        log.info("Updated run record in database — run_id=%s status=%s", run_id, run_data.get("status"))
    except Exception as exc:  # noqa: BLE001
        log.error("update_run failed (run_id=%s): %s", run_id, exc)


def write_campaigns(run_id: int, campaigns: list) -> int:
    """Insert campaign rows for this run.

    Each item in *campaigns* should be a dict produced by analysis/core.py
    (campaign truth table output).  Missing keys default to None.
    Returns count of rows inserted (0 for empty input, DB unavailable, or write
    failure).  Use persistence_succeeded() in the scheduler to distinguish a
    legitimate zero-row sync from a failed write.
    Never raises.
    """
    if run_id is None:
        log.debug("write_campaigns skipped — run_id is None")
        return 0
    if not campaigns:
        return 0
    run_date = _today()
    rows = []
    for c in campaigns:
        raw_name = c.get("campaign_name") or c.get("campaign")
        # Windsor campaign data never contains HubSpot pseudo-names, so only
        # canonicalisation is needed here (not the full _clean_campaign_name() filter).
        campaign_name = None
        if raw_name is not None:
            normalized_name = str(raw_name).strip()
            if normalized_name:
                campaign_name = _canonicalise_campaign_name(normalized_name.lower())
        spend = _float_or_none(c.get("spend_usd") or c.get("spend_30d_usd") or c.get("spend")) or 0.0
        sqls = _int_or_none(c.get("confirmed_sqls")) or 0
        cpql = round(spend / sqls, 2) if sqls > 0 else None
        rows.append((
            run_id,
            run_date,
            campaign_name,
            spend,
            _int_or_none(c.get("clicks")),
            _int_or_none(c.get("impressions")),
            _float_or_none(c.get("conversions")),
            _int_or_none(c.get("total_leads")),
            sqls,
            _int_or_none(c.get("junk_count")),
            _float_or_none(c.get("junk_rate_pct")),
            cpql,
            c.get("verdict"),
            c.get("verdict_reason"),
        ))
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO campaigns (
                        run_id, run_date, campaign_name, spend_usd, clicks,
                        impressions, conversions, total_leads, confirmed_sqls,
                        junk_count, junk_rate_pct, cpql_usd, verdict, verdict_reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        log.info("Wrote %d campaign rows to database (run_id=%s)", len(rows), run_id)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.error("write_campaigns failed (run_id=%s): %s", run_id, exc)
        return 0


def write_leads(run_id: int, contacts: list) -> int:
    """Insert lead rows for this run.

    Each item in *contacts* should be a dict produced by hubspot_pull.py.
    mql_status is mapped to status_category automatically.
    Returns count of rows inserted (0 for empty input, DB unavailable, or write
    failure). Use _persistence_succeeded() in the scheduler to distinguish a
    legitimate zero-row sync from a failed write.
    Never raises.
    """
    if run_id is None:
        log.debug("write_leads skipped — run_id is None")
        return 0
    if not contacts:
        return 0
    run_date = _today()
    rows = []
    for c in contacts:
        # HubSpot contacts are {"id": "...", "properties": {...}}
        # Support both shapes: raw HubSpot response and pre-flattened dicts
        props = c.get("properties") or c

        campaign_name = (
            props.get("hs_analytics_source_data_1") or
            props.get("campaign_name") or
            props.get("campaign")
        )
        keyword = props.get("hs_analytics_source_data_2") or props.get("keyword")
        country = props.get("ip_country") or props.get("country")
        mql_status = props.get("mql_status") or props.get("mql___mdr_comments")
        gclid = props.get("hs_google_click_id") or props.get("gclid")
        hs_source = props.get("hs_analytics_source", "")
        company = props.get("company")

        source_type = _map_source_type(hs_source, campaign_name)
        campaign_name_clean = _clean_campaign_name(campaign_name)
        # PR-ADS-109: persist the real HubSpot business event date and raw source.
        contact_created_at = _parse_ts(props.get("createdate"))

        rows.append((
            run_id,
            run_date,
            c.get("id") or c.get("contact_id"),
            campaign_name_clean,
            keyword,
            country,
            mql_status,
            _map_status_category(mql_status),
            gclid,
            source_type,
            company,
            contact_created_at,
            hs_source or None,
        ))
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO leads (
                        run_id, run_date, contact_id, campaign_name,
                        keyword, country, mql_status, status_category, gclid, source_type, company,
                        contact_created_at, hs_analytics_source
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        log.info("Wrote %d lead rows to database (run_id=%s)", len(rows), run_id)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.error("write_leads failed (run_id=%s): %s", run_id, exc)
        return 0


def write_waste_terms(run_id: int, waste_items: list) -> None:
    """Insert waste term rows for this run.

    Each item in *waste_items* should be a dict from the waste detection
    analysis output (confirmed_waste_items list).
    Never raises.
    """
    if run_id is None:
        log.debug("write_waste_terms skipped — run_id is None")
        return
    if not waste_items:
        return
    run_date = _today()
    rows = []
    for w in waste_items:
        rows.append((
            run_id,
            run_date,
            w.get("search_term") or w.get("term", ""),
            w.get("campaign_name") or w.get("campaign"),
            _float_or_none(w.get("spend_usd") or w.get("spend")),
            w.get("junk_category") or w.get("category"),
            w.get("matched_pattern") or w.get("pattern"),
            int(w.get("crm_junk_confirmed", 0) or 0),
        ))
    try:
        with get_conn() as conn:
            if conn is None:
                return
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO waste_terms (
                        run_id, run_date, search_term, campaign_name,
                        spend_usd, junk_category, matched_pattern, crm_junk_confirmed
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        log.info("Wrote %d waste term rows to database (run_id=%s)", len(rows), run_id)
    except Exception as exc:  # noqa: BLE001
        log.error("write_waste_terms failed (run_id=%s): %s", run_id, exc)


def write_geo(run_id: int, geo_rows: list) -> int:
    """Insert geo performance rows for this run.

    Each item in *geo_rows* should be a dict produced by pull_geo_performance().
    Deletes existing geo rows for the same run_id before inserting, keeping
    manual re-runs idempotent.
    Returns count of inserted rows.
    Never raises.
    """
    if run_id is None:
        log.debug("write_geo skipped — run_id is None")
        return 0
    if not geo_rows:
        return 0
    run_date = _today()
    rows = []
    for g in geo_rows:
        raw_name = g.get("campaign") or g.get("campaign_name")
        campaign_name = None
        if raw_name is not None:
            normalized = str(raw_name).strip()
            if normalized:
                campaign_name = _canonicalise_campaign_name(normalized.lower())
        # Resolve run_date from row if available
        row_date = g.get("date") or g.get("run_date")
        if row_date:
            try:
                from datetime import date as _date
                if isinstance(row_date, _date):
                    effective_date = row_date
                else:
                    effective_date = _date.fromisoformat(str(row_date))
            except (ValueError, TypeError):
                effective_date = run_date
        else:
            effective_date = run_date
        rows.append((
            run_id,
            effective_date,
            g.get("country"),
            campaign_name,
            float(_float_or_none(g.get("spend") or g.get("spend_usd")) or 0),
            int(_int_or_none(g.get("clicks")) or 0),
            int(_int_or_none(g.get("impressions")) or 0),
            float(_float_or_none(g.get("conversions")) or 0),
        ))
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.execute("DELETE FROM geo WHERE run_id = %s", (run_id,))
                cur.executemany(
                    """
                    INSERT INTO geo (
                        run_id, run_date, country, campaign_name,
                        spend_usd, clicks, impressions, conversions
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        log.info("Wrote %d geo rows to database (run_id=%s)", len(rows), run_id)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.error("write_geo failed (run_id=%s): %s", run_id, exc)
        return 0


def write_deals(run_id: int, deals: list) -> int:
    """Insert deal rows for this run.

    Each item in *deals* should be a dict produced by hubspot_pull.py
    (pull_deals_with_gclid output).
    Returns count of rows inserted (0 for empty input, DB unavailable, or write
    failure).  Use persistence_succeeded() in the scheduler to distinguish a
    legitimate zero-row sync from a failed write.
    Never raises.
    """
    if run_id is None:
        log.debug("write_deals skipped — run_id is None")
        return 0
    if not deals:
        return 0
    run_date = _today()
    rows = []
    for d in deals:
        rows.append((
            run_id,
            run_date,
            d.get("contact_id") or d.get("id"),
            d.get("company"),
            d.get("country"),
            d.get("keyword"),
            d.get("campaign_name") or d.get("campaign"),
            d.get("deal_stage"),
            d.get("deal_stage_label"),
            _float_or_none(d.get("deal_amount_usd") or d.get("amount")),
            d.get("mql_status"),
            d.get("gclid"),
        ))
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO deals (
                        run_id, run_date, contact_id, company, country,
                        keyword, campaign_name, deal_stage, deal_stage_label,
                        deal_amount_usd, mql_status, gclid
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        log.info("Wrote %d deal rows to database (run_id=%s)", len(rows), run_id)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.error("write_deals failed (run_id=%s): %s", run_id, exc)
        return 0


def write_keywords(run_id: int, keyword_rows: list) -> int:
    """Insert keyword performance rows for this run.

    Each item in *keyword_rows* should be a dict produced by
    pull_keyword_performance(). Deletes existing keyword rows for the same
    run_id before inserting, keeping manual re-runs idempotent.
    Returns count of inserted rows.
    Never raises.
    """
    if run_id is None:
        log.debug("write_keywords skipped — run_id is None")
        return 0
    if not keyword_rows:
        return 0
    today = _today()
    rows = []
    for k in keyword_rows:
        raw_name = k.get("campaign") or k.get("campaign_name")
        campaign_name = None
        if raw_name is not None:
            normalized = str(raw_name).strip()
            if normalized:
                campaign_name = _canonicalise_campaign_name(normalized.lower())
        match_type = k.get("match_type")
        if match_type is not None:
            match_type = str(match_type).strip() or None
        # Resolve run_date from the row if available, falling back to today
        raw_date = k.get("date") if k.get("date") is not None else k.get("run_date")
        if raw_date is not None:
            try:
                if isinstance(raw_date, date):
                    effective_date = raw_date
                else:
                    effective_date = date.fromisoformat(str(raw_date))
            except (ValueError, TypeError):
                effective_date = today
        else:
            effective_date = today
        rows.append((
            run_id,
            effective_date,
            campaign_name,
            k.get("ad_group"),
            k.get("keyword"),
            match_type,
            _float_or_none(k.get("quality_score")),
            float(_float_or_none(k.get("spend") or k.get("spend_usd")) or 0),
            int(_int_or_none(k.get("clicks")) or 0),
            int(_int_or_none(k.get("impressions")) or 0),
            float(_float_or_none(k.get("conversions")) or 0),
            float(_float_or_none(k.get("cpc") or k.get("cpc_usd")) or 0),
        ))
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.execute("DELETE FROM keywords WHERE run_id = %s", (run_id,))
                cur.executemany(
                    """
                    INSERT INTO keywords (
                        run_id, run_date, campaign_name, ad_group, keyword,
                        match_type, quality_score, spend_usd, clicks,
                        impressions, conversions, cpc_usd
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        log.info("Wrote %d keyword rows to database (run_id=%s)", len(rows), run_id)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.error("write_keywords failed (run_id=%s): %s", run_id, exc)
        return 0


def write_search_terms(
    run_id: Optional[int],
    search_term_rows: list,
    sync_batch_id: Optional[int] = None,
) -> int:
    """Upsert raw search-term fact rows into the search_terms table.

    Accepts rows from pull_search_terms() (windsor connector format) or from
    the Google Ads direct connector (with cost_micros + currency_code).
    Preserves existing is_flagged_waste / junk_category / matched_pattern on
    conflict — raw write is NOT allowed to override waste classifications.

    Currency lineage (PR-ADS-144):
      When cost_micros and currency_code are present in the input row, they are
      stored durably so the evidence service can perform per-date FX conversion.
      source_system is inferred from the input row (``source`` field) and
      defaults to ``"unknown"`` for legacy rows.

    Returns count of inserted/updated rows.
    Returns 0 safely for empty input or DB unavailable.
    Never raises.
    """
    if not search_term_rows:
        log.info("write_search_terms: input_rows=0, nothing to write")
        return 0

    today = _today()
    rows = []
    input_rows = len(search_term_rows)
    skipped_blank_search_term = 0

    for raw in search_term_rows:
        # ── Resolve source_date ──────────────────────────────────────────
        raw_date = raw.get("date") or raw.get("source_date")
        if raw_date is not None:
            try:
                if isinstance(raw_date, date):
                    source_date = raw_date
                else:
                    source_date = date.fromisoformat(str(raw_date))
            except (ValueError, TypeError):
                log.warning(
                    "write_search_terms: skipping row with unparseable date %r", raw_date
                )
                continue
        else:
            source_date = today

        # ── Validate search_term ─────────────────────────────────────────
        raw_term = raw.get("search_term") or raw.get("term")
        search_term = str(raw_term).strip() if raw_term is not None else ""
        if not search_term:
            skipped_blank_search_term += 1
            continue

        # ── Campaign name ────────────────────────────────────────────────
        raw_name = raw.get("campaign") or raw.get("campaign_name")
        campaign_name: Optional[str] = None
        if raw_name is not None:
            normalized = str(raw_name).strip()
            if normalized:
                campaign_name = _canonicalise_campaign_name(normalized.lower())

        campaign_id = raw.get("campaign_id")
        ad_group    = raw.get("ad_group")
        keyword     = raw.get("keyword")

        # ── Match type ───────────────────────────────────────────────────
        match_type_raw = raw.get("match_type")
        match_type: Optional[str] = None
        if match_type_raw is not None:
            stripped_mt = str(match_type_raw).strip()
            if stripped_mt:
                match_type = stripped_mt

        # ── Numeric coercion ─────────────────────────────────────────────
        spend_usd   = float(_float_or_none(raw.get("spend") or raw.get("spend_usd")) or 0)
        clicks      = int(_int_or_none(raw.get("clicks")) or 0)
        impressions = int(_int_or_none(raw.get("impressions")) or 0)
        conversions = float(_float_or_none(raw.get("conversions")) or 0)

        # ── Currency lineage (PR-ADS-144) ────────────────────────────────
        # Coerce defensively — write_search_terms must never raise on a stray
        # non-numeric cost_micros (e.g. an empty string). source_system defaults
        # to "unknown" so legacy rows match the documented lineage contract
        # (still not 'google_ads_api', so they stay currency-unverified).
        cost_micros = _int_or_none(raw.get("cost_micros"))
        currency_code = raw.get("currency_code") or None
        source_system = raw.get("source") or "unknown"

        rows.append((
            run_id,
            source_date,
            campaign_name,
            campaign_id,
            ad_group,
            keyword,
            match_type,
            search_term,
            spend_usd,
            clicks,
            impressions,
            conversions,
            cost_micros,
            currency_code,
            source_system,
            sync_batch_id,
        ))

    prepared_rows = len(rows)
    log.info(
        "write_search_terms: input=%d prepared=%d skipped_blank=%d",
        input_rows, prepared_rows, skipped_blank_search_term,
    )

    if skipped_blank_search_term == input_rows and input_rows > 0:
        log.error(
            "write_search_terms: ALL %d search-term rows skipped because "
            "search_term field is missing/blank. This likely indicates a "
            "field mapping issue in the Windsor connector.",
            input_rows,
        )

    if not rows:
        return 0

    _upsert_sql = """
        INSERT INTO search_terms (
            run_id, source_date, campaign_name, campaign_id,
            ad_group, keyword, match_type, search_term,
            spend_usd, clicks, impressions, conversions,
            cost_micros, currency_code, source_system,
            sync_batch_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (
            source_date,
            COALESCE(campaign_name, ''),
            COALESCE(campaign_id,   ''),
            COALESCE(ad_group,      ''),
            COALESCE(keyword,       ''),
            COALESCE(match_type,    ''),
            search_term
        ) DO UPDATE SET
            run_id        = EXCLUDED.run_id,
            sync_batch_id = COALESCE(EXCLUDED.sync_batch_id,
                                     search_terms.sync_batch_id),
            spend_usd     = EXCLUDED.spend_usd,
            clicks        = EXCLUDED.clicks,
            impressions   = EXCLUDED.impressions,
            conversions   = EXCLUDED.conversions,
            cost_micros   = COALESCE(EXCLUDED.cost_micros,
                                     search_terms.cost_micros),
            currency_code = COALESCE(EXCLUDED.currency_code,
                                     search_terms.currency_code),
            source_system = COALESCE(EXCLUDED.source_system,
                                     search_terms.source_system),
            campaign_id   = COALESCE(EXCLUDED.campaign_id,
                                     search_terms.campaign_id),
            updated_at    = NOW()
    """

    # PR-ADS-144: deterministic null-id supersession. campaign_id is part of the
    # natural key, so a legacy row with campaign_id NULL and a later Google Ads
    # row bearing an id for the SAME
    # (source_date, campaign_name, ad_group, keyword, match_type, search_term)
    # fact would otherwise become two rows and double-count. When we write an
    # id-bearing row, the ambiguous NULL-campaign_id twin is superseded and
    # removed (the precise id-bearing identity wins). Two DISTINCT ids (10, 20)
    # sharing a display name are untouched — both are real, distinct facts.
    _null_twin_keys = [
        (r[1], r[2], r[4], r[5], r[6], r[7])   # (date, name, ad_group, kw, mt, term)
        for r in rows if r[3] is not None and str(r[3]).strip()
    ]
    _null_twin_delete = """
        DELETE FROM search_terms
        WHERE campaign_id IS NULL
          AND source_date = %s
          AND COALESCE(campaign_name, '') = COALESCE(%s, '')
          AND COALESCE(ad_group,      '') = COALESCE(%s, '')
          AND COALESCE(keyword,       '') = COALESCE(%s, '')
          AND COALESCE(match_type,    '') = COALESCE(%s, '')
          AND search_term = %s
    """

    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(_upsert_sql, rows)
                if _null_twin_keys:
                    cur.executemany(_null_twin_delete, _null_twin_keys)
                # executemany with ON CONFLICT makes rowcount unreliable for
                # determining actual inserts vs updates; use len(rows) as the
                # attempted-upsert count.
                attempted = len(rows)
        log.info(
            "write_search_terms: upserted %d rows (run_id=%s) "
            "[input=%d prepared=%d skipped_blank=%d written=%d]",
            attempted, run_id, input_rows, prepared_rows,
            skipped_blank_search_term, attempted,
        )
        return attempted
    except Exception as exc:  # noqa: BLE001
        log.error("write_search_terms failed (run_id=%s): %s", run_id, exc)
        return 0


def write_keyword_daily_facts(
    run_id: Optional[int],
    keyword_rows: list,
    sync_batch_id: Optional[int] = None,
) -> dict:
    """Upsert durable keyword daily facts (PR-ADS-146) into keyword_daily_facts.

    Accepts rows from pull_keyword_performance()/normalize_keyword_row (Google
    Ads direct connector). Natural key is immutable Google Ads identity:
    ``source_date + customer_id + campaign_id + ad_group_id + criterion_id`` — a
    repeated scheduler pull for the same fact UPDATES the same row, so overlapping
    windows never multiply totals. Two ids sharing a display name stay separate.

    FAIL CLOSED on identity: a row missing source_date OR any of customer_id /
    campaign_id / ad_group_id / criterion_id is REJECTED (never filed under today
    or under an empty-string id) and counted, so two incomplete facts can never
    collide. The DB enforces the same via NOT NULL columns.

    Currency lineage: raw ``cost_micros`` + native ``currency_code`` +
    ``source_system`` are stored durably; native cost is NEVER written into a USD
    field, and a missing value is NEVER coerced to zero. conversions is kept NULL
    when unavailable (unknown ≠ a genuine 0). Quality diagnostics are
    LATEST-OBSERVED (selected by quality_observed_at); a null never wipes a prior
    observation and a fail-closed no-quality pull carries no observation stamp.

    The legacy ``keywords`` snapshot table is NOT touched. Returns structured
    persistence stats ``{fetched, prepared, written, skipped_no_date,
    skipped_missing_identity, db_unavailable}`` so the scheduler can distinguish
    complete from partial persistence. Never raises.
    """
    input_rows = len(keyword_rows or [])
    stats = {"fetched": input_rows, "prepared": 0, "written": 0,
             "skipped_no_date": 0, "skipped_missing_identity": 0,
             "db_unavailable": False}
    if not keyword_rows:
        log.info("write_keyword_daily_facts: input_rows=0, nothing to write")
        return stats

    rows = []

    def _clean(v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    for raw in keyword_rows:
        # ── Resolve source_date (reporting date, never run_date) ─────────
        raw_date = raw.get("date") or raw.get("source_date")
        if raw_date is None:
            stats["skipped_no_date"] += 1
            continue
        try:
            source_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        except (ValueError, TypeError):
            log.warning("write_keyword_daily_facts: skipping unparseable date %r", raw_date)
            stats["skipped_no_date"] += 1
            continue

        customer_id  = _clean(raw.get("customer_id"))
        campaign_id  = _clean(raw.get("campaign_id"))
        ad_group_id  = _clean(raw.get("ad_group_id"))
        criterion_id = _clean(raw.get("criterion_id"))

        # ── Fail closed on any missing immutable identity ────────────────
        missing = [name for name, val in (("customer_id", customer_id),
                                          ("campaign_id", campaign_id),
                                          ("ad_group_id", ad_group_id),
                                          ("criterion_id", criterion_id))
                   if not val]
        if missing:
            stats["skipped_missing_identity"] += 1
            log.warning("write_keyword_daily_facts: skipping row missing identity field(s): %s",
                        ",".join(missing))
            continue

        campaign_name = _clean(raw.get("campaign") or raw.get("campaign_name"))
        ad_group_name = _clean(raw.get("ad_group") or raw.get("ad_group_name"))
        criterion_status = _clean(raw.get("criterion_status"))
        keyword_text = _clean(raw.get("keyword") or raw.get("keyword_text"))

        match_type_raw = raw.get("match_type")
        match_type = str(match_type_raw).strip() or None if match_type_raw is not None else None

        cost_micros = _int_or_none(raw.get("cost_micros"))
        currency_code = _clean(raw.get("currency_code"))
        source_system = raw.get("source") or "unknown"

        impressions = int(_int_or_none(raw.get("impressions")) or 0)
        clicks      = int(_int_or_none(raw.get("clicks")) or 0)
        # Keep conversions NULL when missing so "unknown" stays distinct from a
        # genuine 0 (and never wipes a prior value on upsert — see COALESCE below).
        conversions = _float_or_none(raw.get("conversions"))

        # Quality attributes — preserve NULL (unavailable) distinct from 0.
        quality_score = _int_or_none(raw.get("quality_score"))
        expected_ctr = _clean(raw.get("expected_ctr"))
        ad_relevance = _clean(raw.get("ad_relevance"))
        landing_page_experience = _clean(raw.get("landing_page_experience"))
        # Genuine observation timestamp — only present when quality was observed.
        quality_observed_at = raw.get("quality_observed_at") if quality_score is not None else None

        rows.append((
            run_id, source_date, customer_id, campaign_id, campaign_name,
            ad_group_id, ad_group_name, criterion_id, keyword_text, match_type,
            criterion_status, cost_micros, currency_code, source_system,
            impressions, clicks, conversions,
            quality_score, expected_ctr, ad_relevance, landing_page_experience,
            quality_observed_at, sync_batch_id,
        ))

    stats["prepared"] = len(rows)
    if not rows:
        log.info("write_keyword_daily_facts: nothing to write after prep "
                 "(skipped_no_date=%d skipped_missing_identity=%d)",
                 stats["skipped_no_date"], stats["skipped_missing_identity"])
        return stats

    _upsert_sql = """
        INSERT INTO keyword_daily_facts (
            run_id, source_date, customer_id, campaign_id, campaign_name,
            ad_group_id, ad_group_name, criterion_id, keyword_text, match_type,
            criterion_status, cost_micros, currency_code, source_system,
            impressions, clicks, conversions,
            quality_score, expected_ctr, ad_relevance, landing_page_experience,
            quality_observed_at, sync_batch_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_date, customer_id, campaign_id, ad_group_id, criterion_id)
        DO UPDATE SET
            run_id           = EXCLUDED.run_id,
            sync_batch_id    = COALESCE(EXCLUDED.sync_batch_id, keyword_daily_facts.sync_batch_id),
            campaign_name    = COALESCE(EXCLUDED.campaign_name, keyword_daily_facts.campaign_name),
            ad_group_name    = COALESCE(EXCLUDED.ad_group_name, keyword_daily_facts.ad_group_name),
            keyword_text     = COALESCE(EXCLUDED.keyword_text, keyword_daily_facts.keyword_text),
            match_type       = COALESCE(EXCLUDED.match_type, keyword_daily_facts.match_type),
            criterion_status = COALESCE(EXCLUDED.criterion_status, keyword_daily_facts.criterion_status),
            cost_micros      = COALESCE(EXCLUDED.cost_micros, keyword_daily_facts.cost_micros),
            currency_code    = COALESCE(EXCLUDED.currency_code, keyword_daily_facts.currency_code),
            source_system    = COALESCE(EXCLUDED.source_system, keyword_daily_facts.source_system),
            impressions      = EXCLUDED.impressions,
            clicks           = EXCLUDED.clicks,
            -- A missing (NULL) conversions in a re-pull never wipes a prior value.
            conversions      = COALESCE(EXCLUDED.conversions, keyword_daily_facts.conversions),
            -- Quality is latest-observed by quality_observed_at: a new observation
            -- (non-null score WITH a stamp) wins; a null score never wipes a prior
            -- observation, and a no-quality fallback (null stamp) leaves it intact.
            -- 0 is a real score and is preserved.
            quality_score       = CASE WHEN EXCLUDED.quality_observed_at IS NOT NULL
                                       AND (keyword_daily_facts.quality_observed_at IS NULL
                                            OR EXCLUDED.quality_observed_at >= keyword_daily_facts.quality_observed_at)
                                       THEN EXCLUDED.quality_score
                                       ELSE keyword_daily_facts.quality_score END,
            expected_ctr        = CASE WHEN EXCLUDED.quality_observed_at IS NOT NULL
                                       AND (keyword_daily_facts.quality_observed_at IS NULL
                                            OR EXCLUDED.quality_observed_at >= keyword_daily_facts.quality_observed_at)
                                       THEN EXCLUDED.expected_ctr
                                       ELSE keyword_daily_facts.expected_ctr END,
            ad_relevance        = CASE WHEN EXCLUDED.quality_observed_at IS NOT NULL
                                       AND (keyword_daily_facts.quality_observed_at IS NULL
                                            OR EXCLUDED.quality_observed_at >= keyword_daily_facts.quality_observed_at)
                                       THEN EXCLUDED.ad_relevance
                                       ELSE keyword_daily_facts.ad_relevance END,
            landing_page_experience = CASE WHEN EXCLUDED.quality_observed_at IS NOT NULL
                                       AND (keyword_daily_facts.quality_observed_at IS NULL
                                            OR EXCLUDED.quality_observed_at >= keyword_daily_facts.quality_observed_at)
                                       THEN EXCLUDED.landing_page_experience
                                       ELSE keyword_daily_facts.landing_page_experience END,
            quality_observed_at = GREATEST(keyword_daily_facts.quality_observed_at,
                                           EXCLUDED.quality_observed_at),
            updated_at       = NOW()
    """

    try:
        with get_conn() as conn:
            if conn is None:
                stats["db_unavailable"] = True
                return stats
            with conn.cursor() as cur:
                cur.executemany(_upsert_sql, rows)
                stats["written"] = len(rows)
        log.info("write_keyword_daily_facts: upserted %d rows (run_id=%s) "
                 "[fetched=%d prepared=%d skipped_no_date=%d skipped_missing_identity=%d]",
                 stats["written"], run_id, input_rows, stats["prepared"],
                 stats["skipped_no_date"], stats["skipped_missing_identity"])
        return stats
    except Exception as exc:  # noqa: BLE001
        log.error("write_keyword_daily_facts failed (run_id=%s): %s", run_id, exc)
        stats["db_unavailable"] = True
        return stats


# ---------------------------------------------------------------------------
# Sync tracking helpers (PR-ADS-039)
# ---------------------------------------------------------------------------

# Allowed values — used for normalisation/validation; not hard-fail guards so
# that new sources/datasets can be added to the system without a code deploy.
# ``google_ads_api`` is the active Platform Evidence source (PR-ADS-104 cutover:
# campaigns / search_terms / geo / keyword_facts); it must be recognised so
# start_sync_batch never logs a false "unknown source 'google_ads_api'".
VALID_SYNC_SOURCES   = {"windsor", "google_ads_api", "hubspot", "gclid", "mailchimp"}
VALID_SYNC_DATASETS  = {"campaigns", "keywords", "keyword_facts", "search_terms",
                        "geo", "contacts", "deals", "matches",
                        # PR-ADS-151 Mailchimp read-only datasets
                        "reports", "audiences", "attribution",
                        # PR-ADS-153B canonical CRM funnel datasets. These keys are
                        # asserted against DATASET_FRESHNESS_CONFIG by a test so the
                        # writer-key/registry-key mismatch class cannot recur.
                        "contact_funnel", "lifecycle_events"}
VALID_SYNC_TYPES     = {"backfill", "daily", "weekly", "monthly", "manual"}
VALID_SYNC_STATUSES  = {"running", "success", "failed", "unknown"}


def _to_date_or_none(value):
    """Coerce a date-like value to datetime.date, or return None.

    Accepts:
    - datetime.date
    - ISO date string: YYYY-MM-DD

    Returns None for:
    - None
    - empty string
    - invalid/unparseable strings
    - unexpected types (not str or datetime.date)
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def start_sync_batch(
    source: str,
    dataset: str,
    sync_type: str,
    date_from=None,
    date_to=None,
    run_id: Optional[int] = None,
) -> int:
    """Insert a sync_batches row with status='running' and return its id.

    Returns 0 if the DB is unavailable or the inputs are invalid.
    Never raises.
    """
    source    = (source    or "").strip().lower()
    dataset   = (dataset   or "").strip().lower()
    sync_type = (sync_type or "").strip().lower()

    if not source or not dataset or not sync_type:
        log.warning("start_sync_batch called with empty source/dataset/sync_type")
        return 0

    if source not in VALID_SYNC_SOURCES:
        log.warning("start_sync_batch: unknown source %r", source)
    if dataset not in VALID_SYNC_DATASETS:
        log.warning("start_sync_batch: unknown dataset %r", dataset)
    if sync_type not in VALID_SYNC_TYPES:
        log.warning("start_sync_batch: unknown sync_type %r", sync_type)

    date_from_value = _to_date_or_none(date_from)
    date_to_value   = _to_date_or_none(date_to)

    if date_from is not None and date_from_value is None:
        log.warning(
            "start_sync_batch: invalid date_from %r; expected datetime.date or ISO format YYYY-MM-DD",
            date_from,
        )
        return 0
    if date_to is not None and date_to_value is None:
        log.warning(
            "start_sync_batch: invalid date_to %r; expected datetime.date or ISO format YYYY-MM-DD",
            date_to,
        )
        return 0

    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_batches (
                        run_id, source, dataset, sync_type,
                        date_from, date_to, started_at, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), 'running')
                    RETURNING id
                    """,
                    (run_id, source, dataset, sync_type,
                     date_from_value, date_to_value),
                )
                row = cur.fetchone()
                batch_id = row[0] if row else 0
                log.info(
                    "start_sync_batch — source=%s dataset=%s sync_type=%s batch_id=%s",
                    source, dataset, sync_type, batch_id,
                )
                return batch_id or 0
    except Exception as exc:  # noqa: BLE001
        log.error("start_sync_batch failed: %s", exc)
        return 0


def finish_sync_batch(
    batch_id: int,
    status: str,
    row_count: int = 0,
    error_message: Optional[str] = None,
    last_source_date=None,
) -> bool:
    """Mark a sync_batches row as finished and update sync_state.

    status must be 'success' or 'failed'.
    Returns True on success, False on DB unavailable or invalid batch_id.
    Never raises.
    """
    if not batch_id:
        log.warning("finish_sync_batch called with invalid batch_id=%r", batch_id)
        return False

    status = (status or "").strip().lower()
    if status not in ("success", "failed"):
        log.warning("finish_sync_batch: invalid status %r for batch_id=%s", status, batch_id)
        return False

    last_source_date_value = _to_date_or_none(last_source_date)
    if last_source_date is not None and last_source_date_value is None:
        log.warning(
            "finish_sync_batch: invalid last_source_date %r;"
            " expected datetime.date or ISO format YYYY-MM-DD; storing NULL",
            last_source_date,
        )

    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                # Update the batch row
                cur.execute(
                    """
                    UPDATE sync_batches
                    SET finished_at   = NOW(),
                        status        = %s,
                        row_count     = %s,
                        error_message = %s
                    WHERE id = %s
                    RETURNING source, dataset, date_to
                    """,
                    (status, row_count or 0, error_message, batch_id),
                )
                batch_row = cur.fetchone()
                if not batch_row:
                    log.warning("finish_sync_batch: batch_id=%s not found", batch_id)
                    return False

                source, dataset, batch_date_to = batch_row

                # Resolve last_source_date
                resolved_source_date = last_source_date_value or _to_date_or_none(batch_date_to)

                if status == "success":
                    cur.execute(
                        """
                        INSERT INTO sync_state (
                            source, dataset,
                            last_successful_sync_at, last_source_date,
                            last_batch_id, status, error_message, updated_at
                        ) VALUES (%s, %s, NOW(), %s, %s, 'success', NULL, NOW())
                        ON CONFLICT (source, dataset) DO UPDATE SET
                            last_successful_sync_at = NOW(),
                            last_source_date        = COALESCE(EXCLUDED.last_source_date, sync_state.last_source_date),
                            last_batch_id           = EXCLUDED.last_batch_id,
                            status                  = 'success',
                            error_message           = NULL,
                            updated_at              = NOW()
                        """,
                        (source, dataset, resolved_source_date, batch_id),
                    )
                else:
                    # failed — update status/error but preserve successful watermark
                    cur.execute(
                        """
                        INSERT INTO sync_state (
                            source, dataset,
                            status, error_message, updated_at
                        ) VALUES (%s, %s, 'failed', %s, NOW())
                        ON CONFLICT (source, dataset) DO UPDATE SET
                            status        = 'failed',
                            error_message = EXCLUDED.error_message,
                            updated_at    = NOW()
                        """,
                        (source, dataset, error_message),
                    )

        log.info(
            "finish_sync_batch — batch_id=%s source=%s dataset=%s status=%s row_count=%s",
            batch_id, source, dataset, status, row_count,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("finish_sync_batch failed (batch_id=%s): %s", batch_id, exc)
        return False


def update_sync_state(
    source: str,
    dataset: str,
    status: str,
    last_successful_sync_at=None,
    last_source_date=None,
    last_batch_id: Optional[int] = None,
    error_message: Optional[str] = None,
) -> bool:
    """Upsert a sync_state row for (source, dataset).

    Does not overwrite last_successful_sync_at or last_source_date with NULL
    on failure unless explicit non-None values are supplied.
    Returns True on success, False on DB unavailable.
    Never raises.
    """
    source  = (source  or "").strip().lower()
    dataset = (dataset or "").strip().lower()
    status  = (status  or "").strip().lower()

    if not source or not dataset or not status:
        log.warning("update_sync_state called with empty source/dataset/status")
        return False

    if source not in VALID_SYNC_SOURCES:
        log.warning("update_sync_state: unknown source %r", source)
    if dataset not in VALID_SYNC_DATASETS:
        log.warning("update_sync_state: unknown dataset %r", dataset)
    if status not in VALID_SYNC_STATUSES:
        log.warning("update_sync_state: unknown status %r", status)

    last_source_date_value = _to_date_or_none(last_source_date)
    if last_source_date is not None and last_source_date_value is None:
        log.warning(
            "update_sync_state: invalid last_source_date %r;"
            " expected datetime.date or ISO format YYYY-MM-DD; storing NULL",
            last_source_date,
        )

    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_state (
                        source, dataset,
                        last_successful_sync_at, last_source_date,
                        last_batch_id, status, error_message, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (source, dataset) DO UPDATE SET
                        last_successful_sync_at = COALESCE(
                            EXCLUDED.last_successful_sync_at,
                            sync_state.last_successful_sync_at
                        ),
                        last_source_date = COALESCE(
                            EXCLUDED.last_source_date,
                            sync_state.last_source_date
                        ),
                        last_batch_id = COALESCE(
                            EXCLUDED.last_batch_id,
                            sync_state.last_batch_id
                        ),
                        status        = EXCLUDED.status,
                        error_message = EXCLUDED.error_message,
                        updated_at    = NOW()
                    """,
                    (
                        source, dataset,
                        last_successful_sync_at,
                        last_source_date_value,
                        last_batch_id,
                        status,
                        error_message,
                    ),
                )
        log.info(
            "update_sync_state — source=%s dataset=%s status=%s",
            source, dataset, status,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("update_sync_state failed (source=%s dataset=%s): %s", source, dataset, exc)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _float_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# GCLID attribution writers (PR-ADS-044)
# ---------------------------------------------------------------------------

def _normalise_gclid_match_status(row: dict) -> str:
    """Return a normalised match_status string for GCLID attribution rows.

    Used consistently in both _make_attribution_key() and write_gclid_attribution()
    so the dedupe key always matches the stored column value.

    Priority:
      1. Explicit match_status field (stripped, lowercased).
      2. Boolean 'matched' flag from connectors.gclid_match output.
      3. Default: "unknown".
    """
    raw = row.get("match_status")
    if raw:
        stripped = str(raw).strip().lower()
        if stripped:
            return stripped

    matched_flag = row.get("matched")
    if matched_flag is True:
        return "matched"
    if matched_flag is False:
        return "unmatched"

    return "unknown"


def _make_attribution_key(row: dict) -> str:
    """Generate a stable SHA1 dedupe key for a GCLID attribution row.

    Key parts:
      gclid | contact_id | (deal_id or first_url) | campaign_name | keyword | match_status

    If deal_id is absent, fall back to including first_url to preserve
    uniqueness across contact-only matches.
    """
    gclid        = (row.get("gclid") or "").strip()
    contact_id   = (row.get("contact_id") or "").strip()
    deal_id      = (row.get("deal_id") or "").strip()
    campaign     = (row.get("campaign_name") or row.get("campaign") or "").strip()
    keyword      = (row.get("keyword") or "").strip()
    match_status = _normalise_gclid_match_status(row)
    first_url    = (row.get("first_url") or "").strip()

    if deal_id:
        parts = f"{gclid}|{contact_id}|{deal_id}|{campaign}|{keyword}|{match_status}"
    else:
        parts = f"{gclid}|{contact_id}||{campaign}|{keyword}|{match_status}|{first_url}"

    return hashlib.sha1(parts.encode("utf-8")).hexdigest()  # noqa: S324  # non-cryptographic dedup key


def _parse_ts_or_none(value) -> Optional[str]:
    """Return a datetime string suitable for psycopg2 TIMESTAMPTZ, or None.

    Accepts ISO strings, datetime objects, or None.  Invalid values return None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        # Validate by parsing — raises if invalid
        datetime.fromisoformat(text)
        return text
    except (ValueError, TypeError):
        return None


def write_gclid_attribution(
    run_id: Optional[int],
    matched_rows: list,
    sync_batch_id: Optional[int] = None,
) -> int:
    """Upsert GCLID attribution rows into the gclid_attribution table.

    Accepts rows from data/matched_gclid.json or from in-memory match output.
    Skips rows with blank/missing gclid.
    Multiple deals for the same contact/GCLID are preserved as separate rows.
    Does not overwrite useful non-null values with nulls on conflict.
    Returns count of upserted rows (attempted upserts for non-empty input).
    Returns 0 on empty input or DB unavailable.
    Never raises.
    """
    if not matched_rows:
        return 0

    rows = []
    for raw in matched_rows:
        gclid = (raw.get("gclid") or "").strip()
        if not gclid:
            log.warning("write_gclid_attribution: skipping row with blank gclid")
            continue

        # Resolve campaign_name — accept both 'campaign' and 'campaign_name' keys
        raw_campaign = raw.get("campaign_name") or raw.get("campaign")
        campaign_name: Optional[str] = None
        if raw_campaign is not None:
            normalized = str(raw_campaign).strip()
            if normalized:
                campaign_name = _canonicalise_campaign_name(normalized.lower())

        mql_status = raw.get("mql_status")
        status_category = raw.get("status_category") or _map_status_category(mql_status)

        # Derive match_status using shared helper so the key and stored value always match.
        match_status_value = _normalise_gclid_match_status(raw)

        attribution_key = _make_attribution_key({
            **raw,
            "campaign_name": campaign_name,
            "match_status": match_status_value,
        })

        rows.append((
            attribution_key,
            run_id,
            sync_batch_id,
            gclid,
            raw.get("contact_id"),
            raw.get("deal_id"),
            campaign_name,
            raw.get("keyword"),
            raw.get("match_type"),
            raw.get("search_term"),
            raw.get("company"),
            raw.get("country"),
            raw.get("first_url"),
            _parse_ts_or_none(raw.get("contact_created_at")),
            _parse_ts_or_none(raw.get("deal_created_at")),
            _parse_ts_or_none(raw.get("deal_close_date")),
            raw.get("deal_stage"),
            raw.get("deal_stage_label"),
            _float_or_none(raw.get("deal_amount_usd") or raw.get("deal_amount")),
            mql_status,
            status_category,
            match_status_value,
            raw.get("match_source"),
        ))

    if not rows:
        return 0

    _upsert_sql = """
        INSERT INTO gclid_attribution (
            attribution_key, run_id, sync_batch_id,
            gclid, contact_id, deal_id,
            campaign_name, keyword, match_type, search_term,
            company, country, first_url,
            contact_created_at, deal_created_at, deal_close_date,
            deal_stage, deal_stage_label, deal_amount_usd,
            mql_status, status_category,
            match_status, match_source
        ) VALUES (
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s
        )
        ON CONFLICT (attribution_key) DO UPDATE SET
            -- Preserve existing run_id/sync_batch_id so that the original run context
            -- is retained for audit trail continuity; only populate when NULL.
            run_id           = COALESCE(gclid_attribution.run_id,        EXCLUDED.run_id),
            sync_batch_id    = COALESCE(gclid_attribution.sync_batch_id, EXCLUDED.sync_batch_id),
            campaign_name    = COALESCE(EXCLUDED.campaign_name,    gclid_attribution.campaign_name),
            keyword          = COALESCE(EXCLUDED.keyword,          gclid_attribution.keyword),
            match_type       = COALESCE(EXCLUDED.match_type,       gclid_attribution.match_type),
            search_term      = COALESCE(EXCLUDED.search_term,      gclid_attribution.search_term),
            company          = COALESCE(EXCLUDED.company,          gclid_attribution.company),
            country          = COALESCE(EXCLUDED.country,          gclid_attribution.country),
            first_url        = COALESCE(EXCLUDED.first_url,        gclid_attribution.first_url),
            deal_stage       = COALESCE(EXCLUDED.deal_stage,       gclid_attribution.deal_stage),
            deal_stage_label = COALESCE(EXCLUDED.deal_stage_label, gclid_attribution.deal_stage_label),
            deal_amount_usd  = COALESCE(EXCLUDED.deal_amount_usd,  gclid_attribution.deal_amount_usd),
            mql_status       = COALESCE(EXCLUDED.mql_status,       gclid_attribution.mql_status),
            status_category  = COALESCE(EXCLUDED.status_category,  gclid_attribution.status_category),
            match_status     = COALESCE(EXCLUDED.match_status,     gclid_attribution.match_status),
            match_source     = COALESCE(EXCLUDED.match_source,     gclid_attribution.match_source),
            updated_at       = NOW()
    """

    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(_upsert_sql, rows)
                attempted = len(rows)
        log.info(
            "write_gclid_attribution: upserted %d rows (run_id=%s)",
            attempted, run_id,
        )
        return attempted
    except Exception as exc:  # noqa: BLE001
        log.error("write_gclid_attribution failed (run_id=%s): %s", run_id, exc)
        return 0


def write_gclid_coverage_snapshot(
    run_id: Optional[int],
    coverage: dict,
    sync_batch_id: Optional[int] = None,
) -> int:
    """Insert one GCLID coverage snapshot row into gclid_coverage_snapshots.

    Extracts known numeric fields from the coverage dict produced by
    run_gclid_match() and stores the full dict as raw_summary JSONB.
    Returns 1 on success, 0 on empty input or DB unavailable.
    Never raises.
    """
    if not coverage:
        return 0

    total_contacts         = _int_or_none(coverage.get("total_paid_contacts"))
    contacts_with_gclid    = _int_or_none(coverage.get("matched_to_windsor") or coverage.get("contacts_with_gclid"))
    contacts_without_gclid = _int_or_none(coverage.get("contacts_without_gclid"))
    coverage_pct           = _float_or_none(coverage.get("gclid_coverage_pct") or coverage.get("coverage_pct"))

    try:
        raw_summary = json.dumps(coverage)
    except (TypeError, ValueError):
        raw_summary = None

    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gclid_coverage_snapshots (
                        run_id, sync_batch_id, snapshot_date,
                        total_contacts, contacts_with_gclid, contacts_without_gclid,
                        coverage_pct, raw_summary
                    ) VALUES (%s, %s, CURRENT_DATE, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        sync_batch_id,
                        total_contacts,
                        contacts_with_gclid,
                        contacts_without_gclid,
                        coverage_pct,
                        raw_summary,
                    ),
                )
        log.info(
            "write_gclid_coverage_snapshot: inserted snapshot (run_id=%s coverage_pct=%s)",
            run_id, coverage_pct,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        log.error("write_gclid_coverage_snapshot failed (run_id=%s): %s", run_id, exc)
        return 0


# ---------------------------------------------------------------------------
# Revenue Truth Recovery jobs (PR-ADS-114) — durable background-job state.
# Local DB only. Never writes to any external platform.
# ---------------------------------------------------------------------------

def _recovery_job_row_to_dict(cols, row) -> dict:
    d = dict(zip(cols, row))
    for k in ("date_from", "date_to", "started_at", "finished_at",
              "created_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


def create_recovery_job(
    job_id: str,
    *,
    dry_run: bool,
    date_from,
    date_to,
    chunk_months: int,
    job_type: str = "revenue_recovery",
) -> bool:
    """Insert a queued background job (revenue_recovery or lead_reconciliation).

    Returns True on success. Never raises.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO revenue_recovery_jobs (
                        job_id, job_type, status, dry_run, date_from, date_to,
                        chunk_months, completed_chunks, errors, started_at
                    ) VALUES (%s, %s, 'queued', %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, NOW())
                    """,
                    (job_id, job_type, bool(dry_run), date_from, date_to, int(chunk_months)),
                )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("create_recovery_job failed (job_id=%s): %s", job_id, exc)
        return False


def update_recovery_job(job_id: str, **fields) -> None:
    """Update mutable fields of a recovery job. JSON fields are serialised.

    Supported fields: status, phase, current_chunk, completed_chunks (list),
    summary (dict), chunks (list), errors (list), finished_at (str/dt).
    Never raises.
    """
    if not fields:
        return
    json_fields = {"completed_chunks", "summary", "chunks", "errors"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in {"status", "phase", "current_chunk", "completed_chunks",
                       "summary", "chunks", "errors", "finished_at"}:
            continue
        if key in json_fields:
            sets.append(f"{key} = %s::jsonb")
            values.append(json.dumps(value))
        else:
            sets.append(f"{key} = %s")
            values.append(value)
    if not sets:
        return
    sets.append("updated_at = NOW()")
    values.append(job_id)
    try:
        with get_conn() as conn:
            if conn is None:
                return
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE revenue_recovery_jobs SET {', '.join(sets)} WHERE job_id = %s",
                    tuple(values),
                )
    except Exception as exc:  # noqa: BLE001
        log.error("update_recovery_job failed (job_id=%s): %s", job_id, exc)


def get_recovery_job(job_id: str) -> Optional[dict]:
    """Return the durable state of a recovery job, or None. Never raises."""
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM revenue_recovery_jobs WHERE job_id = %s", (job_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return _recovery_job_row_to_dict(cols, row)
    except Exception as exc:  # noqa: BLE001
        log.error("get_recovery_job failed (job_id=%s): %s", job_id, exc)
        return None


def get_latest_recovery_job(job_type: Optional[str] = None) -> Optional[dict]:
    """Return the most recent job (optionally filtered by job_type), or None.

    Never raises.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                if job_type:
                    cur.execute(
                        "SELECT * FROM revenue_recovery_jobs WHERE job_type = %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (job_type,),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM revenue_recovery_jobs ORDER BY created_at DESC LIMIT 1",
                    )
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return _recovery_job_row_to_dict(cols, row)
    except Exception as exc:  # noqa: BLE001
        log.error("get_latest_recovery_job failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Durable job lease (PR-ADS-146A) — atomic cross-process claim + heartbeat so
# only one worker owns a resumable job (keyword_bootstrap), and a stale lease
# from a crashed worker can be recovered. The partial unique index
# uq_recovery_running_keyword_bootstrap makes the claim atomic across processes.
# ---------------------------------------------------------------------------

# Sentinel dict returned when the DB is unavailable, so callers can distinguish
# "couldn't reach the durable checkpoint" (fail closed) from "someone else owns it".
_LEASE_DB_UNAVAILABLE = {"claimed": False, "reason": "db_unavailable", "job": None}


def acquire_recovery_lease(
    job_type: str,
    lease_token: str,
    ttl_seconds: int,
    *,
    date_from,
    date_to,
    chunk_months: int = 1,
    seed_completed_chunks: Optional[list] = None,
    stale_takeover: bool = True,
) -> dict:
    """Atomically claim (or take over a stale) durable job for ``job_type``.

    In one transaction: lock the latest job row FOR UPDATE, then decide:
      - a RUNNING job with a live lease  -> not claimed (reason=active_lease);
      - a RUNNING job with an EXPIRED lease -> take it over (recover stale);
      - a resumable non-success job      -> take it over (reuse completed_chunks);
      - otherwise                        -> insert a fresh running job.
    The insert relies on the partial unique index to break a cold-start race.

    Returns ``{claimed: bool, reason: str, job: dict|None}``. Never raises.
    """
    from psycopg2 import errors as _pg_errors  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return dict(_LEASE_DB_UNAVAILABLE)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM revenue_recovery_jobs WHERE job_type = %s "
                    "ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                    (job_type,),
                )
                row = cur.fetchone()
                latest = _recovery_job_row_to_dict([d[0] for d in cur.description], row) if row else None

                if latest and latest.get("status") == "running":
                    # Live lease? Not claimable. Expired? fall through to take over.
                    cur.execute(
                        "SELECT (lease_expires_at IS NOT NULL AND lease_expires_at > NOW()) "
                        "FROM revenue_recovery_jobs WHERE job_id = %s",
                        (latest["job_id"],),
                    )
                    live = cur.fetchone()[0]
                    if live:
                        return {"claimed": False, "reason": "active_lease", "job": latest}
                    if not stale_takeover:
                        return {"claimed": False, "reason": "stale_no_takeover", "job": latest}

                take_over = latest is not None and latest.get("status") in (
                    "running", "partial", "failed", "queued")
                if take_over:
                    cur.execute(
                        """
                        UPDATE revenue_recovery_jobs
                        SET status='running', lease_token=%s,
                            heartbeat_at=NOW(),
                            lease_expires_at=NOW() + make_interval(secs => %s),
                            last_progress_at=NOW(),
                            started_at=COALESCE(started_at, NOW()),
                            date_from=%s, date_to=%s,
                            updated_at=NOW()
                        WHERE id=%s
                        RETURNING *
                        """,
                        (lease_token, int(ttl_seconds), date_from, date_to, latest["id"]),
                    )
                    updated = _recovery_job_row_to_dict([d[0] for d in cur.description], cur.fetchone())
                    return {"claimed": True, "reason": "resumed", "job": updated}

                # Fresh job — seed completed chunks (e.g. from a prior success that
                # no longer covers a newly-closed month) so we never re-sync them.
                new_job_id = f"kwbs_{date_to}_{lease_token[:8]}"
                seed = json.dumps(list(seed_completed_chunks or []))
                try:
                    cur.execute(
                        """
                        INSERT INTO revenue_recovery_jobs (
                            job_id, job_type, status, dry_run, date_from, date_to,
                            chunk_months, completed_chunks, errors,
                            lease_token, heartbeat_at, lease_expires_at,
                            last_progress_at, started_at
                        ) VALUES (%s, %s, 'running', FALSE, %s, %s, %s,
                                  %s::jsonb, '[]'::jsonb,
                                  %s, NOW(), NOW() + make_interval(secs => %s), NOW(), NOW())
                        RETURNING *
                        """,
                        (new_job_id, job_type, date_from, date_to, int(chunk_months),
                         seed, lease_token, int(ttl_seconds)),
                    )
                    created = _recovery_job_row_to_dict([d[0] for d in cur.description], cur.fetchone())
                    return {"claimed": True, "reason": "created", "job": created}
                except _pg_errors.UniqueViolation:
                    # Lost the cold-start race — another worker inserted first.
                    conn.rollback()
                    return {"claimed": False, "reason": "active_lease", "job": None}
    except Exception as exc:  # noqa: BLE001
        log.error("acquire_recovery_lease failed (job_type=%s): %s", job_type, exc)
        return dict(_LEASE_DB_UNAVAILABLE)


def renew_recovery_lease(
    job_id: str,
    lease_token: str,
    ttl_seconds: int,
    *,
    completed_chunks: Optional[list] = None,
    current_chunk: Optional[str] = None,
    errors: Optional[list] = None,
) -> bool:
    """Heartbeat + checkpoint. Advances the lease and (optionally) persists the
    completed-chunk ledger. Returns True only when THIS worker still owns the
    lease (job_id + lease_token match). False => lease lost / DB unavailable, and
    the caller must fail closed rather than continue a non-durable bootstrap."""
    sets = ["heartbeat_at = NOW()",
            "last_progress_at = NOW()",
            "lease_expires_at = NOW() + make_interval(secs => %s)",
            "updated_at = NOW()"]
    values: list = [int(ttl_seconds)]
    if completed_chunks is not None:
        sets.append("completed_chunks = %s::jsonb")
        values.append(json.dumps(list(completed_chunks)))
    if current_chunk is not None:
        sets.append("current_chunk = %s")
        values.append(current_chunk)
    if errors is not None:
        sets.append("errors = %s::jsonb")
        values.append(json.dumps(list(errors)))
    values.extend([job_id, lease_token])
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE revenue_recovery_jobs SET {', '.join(sets)} "
                    "WHERE job_id = %s AND lease_token = %s RETURNING job_id",
                    tuple(values),
                )
                return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        log.error("renew_recovery_lease failed (job_id=%s): %s", job_id, exc)
        return False


def release_recovery_lease(
    job_id: str,
    lease_token: str,
    *,
    status: str,
    summary: Optional[dict] = None,
    completed_chunks: Optional[list] = None,
    finished_at: Optional[str] = None,
) -> bool:
    """Finalise the job, clearing the lease so the next deploy can start fresh.
    Only the current lease owner may release. Returns True on success."""
    sets = ["status = %s", "current_chunk = NULL", "lease_expires_at = NULL",
            "last_progress_at = NOW()", "updated_at = NOW()"]
    values: list = [status]
    if summary is not None:
        sets.append("summary = %s::jsonb")
        values.append(json.dumps(summary))
    if completed_chunks is not None:
        sets.append("completed_chunks = %s::jsonb")
        values.append(json.dumps(list(completed_chunks)))
    sets.append("finished_at = %s")
    values.append(finished_at)
    values.extend([job_id, lease_token])
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE revenue_recovery_jobs SET {', '.join(sets)} "
                    "WHERE job_id = %s AND lease_token = %s RETURNING job_id",
                    tuple(values),
                )
                return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        log.error("release_recovery_lease failed (job_id=%s): %s", job_id, exc)
        return False


def successful_batch_intervals(source: str, dataset: str,
                               window_start=None, window_end=None) -> list:
    """Return ``[(date_from, date_to), …]`` for SUCCESSFUL sync_batches of
    (source, dataset) whose interval intersects ``[window_start, window_end]``
    (either bound may be None to leave that side unbounded). Only batches with
    both dates set are returned. Used for interval-union coverage proof
    (PR-ADS-146B §1) — a window is covered only when the union of these actual
    successful intervals spans it, never inferred from MIN/MAX alone."""
    source = (source or "").strip().lower()
    dataset = (dataset or "").strip().lower()
    clauses = ["source = %s", "dataset = %s", "status = 'success'",
               "date_from IS NOT NULL", "date_to IS NOT NULL"]
    params: list = [source, dataset]
    if window_end is not None:
        clauses.append("date_from <= %s")
        params.append(window_end)
    if window_start is not None:
        clauses.append("date_to >= %s")
        params.append(window_start)
    try:
        with get_conn() as conn:
            if conn is None:
                return []
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT date_from, date_to FROM sync_batches "
                    f"WHERE {' AND '.join(clauses)} ORDER BY date_from, date_to",
                    tuple(params),
                )
                return [(r[0], r[1]) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.error("successful_batch_intervals failed: %s", exc)
        return []


def latest_successful_batch_source_date(source: str, dataset: str):
    """MAX(date_to) across SUCCESSFUL sync_batches for (source, dataset) — the
    furthest date the dataset has been PROVEN synced through, independent of
    whether any rows exist for the most recent dates (zero-activity days). Used
    for selected-window coverage proof (PR-ADS-146A §6). None when unavailable."""
    source = (source or "").strip().lower()
    dataset = (dataset or "").strip().lower()
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(date_to) FROM sync_batches "
                    "WHERE source = %s AND dataset = %s AND status = 'success'",
                    (source, dataset),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
    except Exception as exc:  # noqa: BLE001
        log.error("latest_successful_batch_source_date failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Lead-truth exclusions (PR-ADS-115) — auditable revenue-truth decisions.
# Local DB only. Never overwrites or deletes historical `leads` rows.
# ---------------------------------------------------------------------------

def write_lead_exclusion(
    lead_id: str,
    reason: str,
    *,
    details: Optional[str] = None,
    reconciliation_job_id: Optional[str] = None,
) -> bool:
    """Upsert a durable lead-truth exclusion (keyed by lead_id). Never raises.

    A re-run refreshes the reason/details/job but preserves the original
    excluded_at timestamp for the audit trail.
    """
    if not lead_id:
        return False
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO lead_truth_exclusions (
                        lead_id, reason, details, reconciliation_job_id
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (lead_id) DO UPDATE SET
                        reason                = EXCLUDED.reason,
                        details               = EXCLUDED.details,
                        reconciliation_job_id = EXCLUDED.reconciliation_job_id,
                        updated_at            = NOW()
                    """,
                    (lead_id, reason, details, reconciliation_job_id),
                )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("write_lead_exclusion failed (lead_id=%s): %s", lead_id, exc)
        return False


def count_lead_exclusions() -> int:
    """Return the number of durable lead-truth exclusions. Never raises."""
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM lead_truth_exclusions")
                return int(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        log.error("count_lead_exclusions failed: %s", exc)
        return 0


def backfill_event_date_for_contact(contact_id, created_at) -> int:
    """Backfill contact_created_at for ALL local lead snapshots of a contact_id
    that currently lack it (PR-ADS-115 reconciliation).

    Uses ONLY the supplied HubSpot createdate — never a run/sync/current date,
    and only when a real created date exists. Returns the number of rows updated.
    Never raises.
    """
    if not contact_id or not created_at:
        return 0
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE leads
                    SET contact_created_at = %s::timestamptz
                    WHERE contact_id = %s AND contact_created_at IS NULL
                    """,
                    (created_at, contact_id),
                )
                return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        log.error("backfill_event_date_for_contact failed (contact_id=%s): %s", contact_id, exc)
        return 0


# ---------------------------------------------------------------------------
# Acquisition-source classification (PR-ADS-117) — durable, auditable. Local DB
# only; never writes to or deletes raw HubSpot data.
# ---------------------------------------------------------------------------

def upsert_contact_source_classification(rows: list) -> int:
    """Upsert contact source classifications (keyed by contact_key). Never raises.

    Each row: {contact_key, contact_id, source_primary_raw, source_detail_raw,
    acquisition_group, classification_rule_version, contact_created_at,
    status_category}. Returns count attempted.
    """
    if not rows:
        return 0
    prepared = []
    for r in rows:
        key = (r.get("contact_key") or "").strip()
        if not key:
            continue
        prepared.append((
            key, r.get("contact_id"), r.get("source_primary_raw"),
            r.get("source_detail_raw"), r.get("acquisition_group"),
            r.get("classification_rule_version"),
            _parse_ts_or_none(r.get("contact_created_at")),
            r.get("status_category"),
        ))
    if not prepared:
        return 0
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO contact_source_classification (
                        contact_key, contact_id, source_primary_raw, source_detail_raw,
                        acquisition_group, classification_rule_version,
                        contact_created_at, status_category
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (contact_key) DO UPDATE SET
                        contact_id                  = EXCLUDED.contact_id,
                        source_primary_raw          = EXCLUDED.source_primary_raw,
                        source_detail_raw           = EXCLUDED.source_detail_raw,
                        acquisition_group           = EXCLUDED.acquisition_group,
                        classification_rule_version = EXCLUDED.classification_rule_version,
                        contact_created_at          = EXCLUDED.contact_created_at,
                        status_category             = EXCLUDED.status_category,
                        updated_at                  = NOW()
                    """,
                    prepared,
                )
        return len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_contact_source_classification failed: %s", exc)
        return 0


def upsert_deal_source_attribution(rows: list) -> int:
    """Upsert per-deal source attribution (keyed by deal_id). Never raises.

    Each row: {deal_id, associated_contact_id, acquisition_group,
    source_primary_raw, source_detail_raw, attribution_status,
    attribution_reason, deal_close_date, deal_amount_usd,
    classification_rule_version}. Returns count attempted.
    """
    if not rows:
        return 0
    prepared = []
    for r in rows:
        deal_id = (r.get("deal_id") or "").strip()
        if not deal_id:
            continue
        prepared.append((
            deal_id, r.get("associated_contact_id"), r.get("acquisition_group"),
            r.get("source_primary_raw"), r.get("source_detail_raw"),
            r.get("attribution_status"), r.get("attribution_reason"),
            _parse_ts_or_none(r.get("deal_close_date")),
            _float_or_none(r.get("deal_amount_usd")),
            r.get("classification_rule_version"),
        ))
    if not prepared:
        return 0
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO deal_source_attribution (
                        deal_id, associated_contact_id, acquisition_group,
                        source_primary_raw, source_detail_raw,
                        attribution_status, attribution_reason,
                        deal_close_date, deal_amount_usd, classification_rule_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (deal_id) DO UPDATE SET
                        associated_contact_id       = EXCLUDED.associated_contact_id,
                        acquisition_group           = EXCLUDED.acquisition_group,
                        source_primary_raw          = EXCLUDED.source_primary_raw,
                        source_detail_raw           = EXCLUDED.source_detail_raw,
                        attribution_status          = EXCLUDED.attribution_status,
                        attribution_reason          = EXCLUDED.attribution_reason,
                        deal_close_date             = EXCLUDED.deal_close_date,
                        deal_amount_usd             = EXCLUDED.deal_amount_usd,
                        classification_rule_version = EXCLUDED.classification_rule_version,
                        updated_at                  = NOW()
                    """,
                    prepared,
                )
        return len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_deal_source_attribution failed: %s", exc)
        return 0


def source_attribution_health_counts() -> dict:
    """Return {contacts_classified, deals_attributed, ambiguous_deals,
    unclassified_deals} from the durable source tables. Never raises."""
    out = {"contacts_classified": 0, "deals_attributed": 0,
           "ambiguous_deals": 0, "unclassified_deals": 0}
    try:
        with get_conn() as conn:
            if conn is None:
                return out
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM contact_source_classification")
                out["contacts_classified"] = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT attribution_status, COUNT(*) FROM deal_source_attribution "
                    "GROUP BY attribution_status"
                )
                for status, n in cur.fetchall():
                    if status == "attributed":
                        out["deals_attributed"] += int(n)
                    elif status == "ambiguous":
                        out["ambiguous_deals"] += int(n)
                    elif status == "unclassified":
                        out["unclassified_deals"] += int(n)
        return out
    except Exception as exc:  # noqa: BLE001
        log.error("source_attribution_health_counts failed: %s", exc)
        return out


# ---------------------------------------------------------------------------
# Canonical Google Ads spend (PR-ADS-118) — local DB only; never writes to
# Google Ads. Idempotent upserts keyed by (customer_id, campaign_id, spend_date)
# so duplicate sync/backfill rows never double-count spend.
# ---------------------------------------------------------------------------

def upsert_campaign_daily_spend(rows: list, sync_run_id: Optional[str] = None) -> int:
    """Upsert canonical campaign-daily spend rows. Idempotent. Never raises.

    Each row: {customer_id, currency_code, campaign_id, campaign_name,
    spend_date, cost_micros, source_query_version}. spend_account_currency is
    derived from raw micros (no early rounding of aggregates). Returns count.
    """
    if not rows:
        return 0
    prepared = []
    for r in rows:
        cust = (r.get("customer_id") or "").strip()
        camp = (r.get("campaign_id") or "").strip()
        spend_date = r.get("spend_date")
        if not cust or not camp or not spend_date:
            continue
        micros = int(r.get("cost_micros") or 0)
        prepared.append((
            cust, r.get("currency_code"), camp, r.get("campaign_name"),
            spend_date, micros, micros / 1_000_000,
            sync_run_id, r.get("source_query_version"),
        ))
    if not prepared:
        return 0
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO google_ads_campaign_daily_spend (
                        customer_id, currency_code, campaign_id, campaign_name,
                        spend_date, cost_micros, spend_account_currency,
                        sync_run_id, source_query_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (customer_id, campaign_id, spend_date) DO UPDATE SET
                        currency_code          = EXCLUDED.currency_code,
                        campaign_name          = EXCLUDED.campaign_name,
                        cost_micros            = EXCLUDED.cost_micros,
                        spend_account_currency = EXCLUDED.spend_account_currency,
                        sync_run_id            = EXCLUDED.sync_run_id,
                        source_query_version   = EXCLUDED.source_query_version,
                        updated_at             = NOW()
                    """,
                    prepared,
                )
        return len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_campaign_daily_spend failed: %s", exc)
        return 0


def upsert_spend_coverage(
    customer_id: str, chunk_start, chunk_end, status: str,
    *, rows_written: int = 0, cost_micros_total: int = 0,
    source_query_version: Optional[str] = None, sync_run_id: Optional[str] = None,
) -> bool:
    """Record a fetched spend chunk (verified|failed). Idempotent. Never raises.

    The coverage ledger lets the audit treat a zero-spend day inside a verified
    chunk as real, while a never-fetched range is reported missing — never zero.

    A ``failed`` write never overwrites an existing ``verified`` chunk (the
    ON CONFLICT WHERE guard), so a transient failure cannot silently demote
    coverage that was already proven good.
    """
    if not customer_id or not chunk_start or not chunk_end:
        return False
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO google_ads_spend_coverage (
                        customer_id, chunk_start, chunk_end, status,
                        rows_written, cost_micros_total, source_query_version, sync_run_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (customer_id, chunk_start, chunk_end) DO UPDATE SET
                        status               = EXCLUDED.status,
                        rows_written         = EXCLUDED.rows_written,
                        cost_micros_total    = EXCLUDED.cost_micros_total,
                        source_query_version = EXCLUDED.source_query_version,
                        sync_run_id          = EXCLUDED.sync_run_id,
                        fetched_at           = NOW(),
                        updated_at           = NOW()
                    WHERE google_ads_spend_coverage.status <> 'verified'
                       OR EXCLUDED.status = 'verified'
                    """,
                    (customer_id, chunk_start, chunk_end, status,
                     int(rows_written), int(cost_micros_total), source_query_version, sync_run_id),
                )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_spend_coverage failed: %s", exc)
        return False


def upsert_account_daily_spend(rows: list, sync_run_id: Optional[str] = None) -> int:
    """Upsert account-level daily spend rows (PR-ADS-120). Idempotent. Never raises.

    Each row: {customer_id, spend_date, cost_micros, currency_code,
    account_time_zone, source_query_version}. Keyed by (customer_id, spend_date)
    so a re-fetch never double-counts. Returns the number of rows upserted.
    """
    if not rows:
        return 0
    prepared = []
    for r in rows:
        cust = (r.get("customer_id") or "").strip()
        spend_date = r.get("spend_date")
        if not cust or not spend_date:
            continue
        prepared.append((
            cust, spend_date, int(r.get("cost_micros") or 0),
            r.get("currency_code"), r.get("account_time_zone"),
            sync_run_id, r.get("source_query_version"),
        ))
    if not prepared:
        return 0
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO google_ads_account_daily_spend (
                        customer_id, spend_date, cost_micros, currency_code,
                        account_time_zone, sync_run_id, source_query_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (customer_id, spend_date) DO UPDATE SET
                        cost_micros          = EXCLUDED.cost_micros,
                        currency_code        = EXCLUDED.currency_code,
                        account_time_zone    = EXCLUDED.account_time_zone,
                        sync_run_id          = EXCLUDED.sync_run_id,
                        source_query_version = EXCLUDED.source_query_version,
                        updated_at           = NOW()
                    """,
                    prepared,
                )
        return len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_account_daily_spend failed: %s", exc)
        return 0


def upsert_geo_daily_spend(rows: list, sync_run_id: Optional[str] = None) -> int:
    """Upsert canonical Google Ads geo (country) daily spend (PR-ADS-124).

    Idempotent. Never raises. Each row: {customer_id, currency_code,
    country_criterion_id, campaign_id, spend_date, cost_micros,
    source_query_version}. Keyed by (customer_id, country_criterion_id,
    campaign_id, spend_date) so a re-sync never double-counts. Writes ONLY this
    local table — never Google Ads. Returns the number of rows upserted.
    """
    if not rows:
        return 0
    prepared = []
    for r in rows:
        cust = (r.get("customer_id") or "").strip()
        spend_date = r.get("spend_date")
        if not cust or not spend_date:
            continue
        prepared.append((
            cust, r.get("currency_code"),
            (r.get("country_criterion_id") or "").strip(),
            r.get("country_code"), r.get("country_name"),
            (r.get("campaign_id") or "").strip(),
            spend_date, int(r.get("cost_micros") or 0),
            sync_run_id, r.get("source_query_version"),
        ))
    if not prepared:
        return 0
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO google_ads_geo_daily_spend (
                        customer_id, currency_code, country_criterion_id,
                        country_code, country_name,
                        campaign_id, spend_date, cost_micros,
                        sync_run_id, source_query_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (customer_id, country_criterion_id, campaign_id, spend_date)
                    DO UPDATE SET
                        currency_code        = EXCLUDED.currency_code,
                        country_code         = EXCLUDED.country_code,
                        country_name         = EXCLUDED.country_name,
                        cost_micros          = EXCLUDED.cost_micros,
                        sync_run_id          = EXCLUDED.sync_run_id,
                        source_query_version = EXCLUDED.source_query_version,
                        updated_at           = NOW()
                    """,
                    prepared,
                )
        return len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_geo_daily_spend failed: %s", exc)
        return 0


def upsert_fx_rates(rows: list) -> int:
    """Upsert daily FX rates (PR-ADS-119). Idempotent. Never raises.

    Each row: {rate_date, base_currency, quote_currency, rate, provider,
    source_version}. Keyed by (rate_date, base_currency, quote_currency) so a
    re-fetch never double-writes. Returns the number of rows upserted.
    """
    if not rows:
        return 0
    prepared = []
    for r in rows:
        rd = r.get("rate_date")
        base = (r.get("base_currency") or "").strip().upper()
        quote = (r.get("quote_currency") or "").strip().upper()
        rate = r.get("rate")
        if not rd or not base or not quote or rate is None:
            continue
        prepared.append((rd, base, quote, float(rate),
                         r.get("provider"), r.get("source_version")))
    if not prepared:
        return 0
    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO fx_rates (
                        rate_date, base_currency, quote_currency, rate,
                        provider, source_version
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (rate_date, base_currency, quote_currency) DO UPDATE SET
                        rate           = EXCLUDED.rate,
                        provider       = EXCLUDED.provider,
                        source_version = EXCLUDED.source_version,
                        fetched_at     = NOW(),
                        updated_at     = NOW()
                    """,
                    prepared,
                )
        return len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_fx_rates failed: %s", exc)
        return 0


def upsert_campaign_identity(
    customer_id: str, external_campaign_label: str, *,
    campaign_id: Optional[str] = None,
    canonical_campaign_name: Optional[str] = None,
    historical_campaign_name: Optional[str] = None,
    match_method: str = "manual",
    approved_by: Optional[str] = None,
) -> bool:
    """Record a campaign-identity mapping (PR-ADS-119). Idempotent. Never raises.

    Maps an external HubSpot/UTM label to a canonical Google Ads campaign. Manual
    mappings carry approved_at/approved_by for audit. This NEVER overwrites the
    raw canonical spend-table identity — the historical campaign name is stored
    here as a copy, not by rewriting source rows.
    """
    label = (external_campaign_label or "").strip()
    if not customer_id or not label:
        return False
    # An explicit admin action carries an audit timestamp: manual mappings,
    # auto-approved exact matches, and "not Google Ads" exclusions (PR-ADS-120b).
    approved = match_method in ("manual", "exact_normalized", "not_google_ads")
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO google_ads_campaign_identity (
                        customer_id, campaign_id, canonical_campaign_name,
                        historical_campaign_name, external_campaign_label,
                        match_method, approved_at, approved_by
                    ) VALUES (%s, %s, %s, %s, %s, %s,
                              CASE WHEN %s THEN NOW() ELSE NULL END, %s)
                    ON CONFLICT (customer_id, external_campaign_label) DO UPDATE SET
                        campaign_id              = EXCLUDED.campaign_id,
                        canonical_campaign_name  = EXCLUDED.canonical_campaign_name,
                        historical_campaign_name = EXCLUDED.historical_campaign_name,
                        match_method             = EXCLUDED.match_method,
                        approved_at              = EXCLUDED.approved_at,
                        approved_by              = EXCLUDED.approved_by,
                        updated_at               = NOW()
                    """,
                    (customer_id, campaign_id, canonical_campaign_name,
                     historical_campaign_name, label, match_method,
                     approved, approved_by),
                )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_campaign_identity failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# PR-ADS-153B — canonical CRM funnel contact store
# ---------------------------------------------------------------------------
# ONE ingestion service owns these writes
# (services/hubspot_contact_funnel_sync_service). The legacy `leads` snapshot
# writers are untouched and continue to serve pre-PR-ADS-153C pages.

_CONTACT_FUNNEL_COLUMNS = (
    "contact_id", "created_at", "last_modified_at",
    "lifecycle_stage", "lead_status", "mql_status", "mql_status_category",
    "date_entered_lead", "date_entered_mql", "date_entered_sql",
    "date_entered_opportunity", "date_entered_customer", "latest_stage_entry_at",
    "hs_analytics_source", "hs_analytics_source_data_1", "hs_analytics_source_data_2",
    "hs_latest_source", "hs_latest_source_data_1", "hs_latest_source_data_2",
    "hs_analytics_first_url",
    "ip_country", "country", "company", "owner_id",
    "gclid", "has_gclid",
    "source_system", "lifecycle_rule_version", "mql_rule_version",
)

# Every column except the identity is refreshed from the newest HubSpot read —
# latest-state truth wins (PR-ADS-153B §6). `first_ingested_at` is preserved.
#
# The upsert carries a WHERE guard on `last_modified_at` so a REPLAYED older read
# (the deliberate overlap window, or a retried page) can never resurrect a
# superseded lifecycle stage or status. Latest-state truth is therefore
# structural, not dependent on the order pages happen to arrive in.
#
# A row with NO incoming `last_modified_at` never wins over a stored row that has
# one: an unknown modification time is not evidence of recency, and letting it
# through would blank out known-newer state. The guard only admits a write when
# the STORED timestamp is absent (nothing to protect) or the incoming timestamp is
# present and at least as new.
_CONTACT_FUNNEL_UPDATE_SET = ",\n                        ".join(
    f"{col} = EXCLUDED.{col}" for col in _CONTACT_FUNNEL_COLUMNS if col != "contact_id"
)


def upsert_hubspot_contact_funnel(rows: list, *, sync_batch_id: Optional[int] = None) -> int:
    """Upsert canonical HubSpot contact funnel rows, keyed on contact_id.

    Idempotent: re-ingesting the same contact updates it in place, never appends
    a second row. Latest HubSpot state always wins — an older snapshot can never
    resurrect a superseded lifecycle stage or status.

    Rows must already be normalised by
    ``connectors.hubspot_pull.normalize_contact_funnel_row``. Returns the number
    of rows written. Never raises.
    """
    if not rows:
        return 0

    prepared = []
    for r in rows:
        if not r:
            continue
        contact_id = str(r.get("contact_id") or "").strip()
        if not contact_id:
            # No durable HubSpot identity — never invent a synthetic key.
            continue
        values = [contact_id]
        for col in _CONTACT_FUNNEL_COLUMNS[1:]:
            value = r.get(col)
            if col in {
                "created_at", "last_modified_at", "date_entered_lead",
                "date_entered_mql", "date_entered_sql",
                "date_entered_opportunity", "date_entered_customer",
                "latest_stage_entry_at",
            }:
                values.append(_parse_ts_or_none(value))
            elif col == "has_gclid":
                values.append(bool(value))
            else:
                values.append(value)
        values.append(sync_batch_id)
        prepared.append(tuple(values))

    if not prepared:
        return 0

    column_list = ", ".join(_CONTACT_FUNNEL_COLUMNS) + ", sync_batch_id"
    placeholders = ", ".join(["%s"] * (len(_CONTACT_FUNNEL_COLUMNS) + 1))

    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(
                    f"""
                    INSERT INTO hubspot_contact_funnel ({column_list})
                    VALUES ({placeholders})
                    ON CONFLICT (contact_id) DO UPDATE SET
                        {_CONTACT_FUNNEL_UPDATE_SET},
                        sync_batch_id    = EXCLUDED.sync_batch_id,
                        last_ingested_at = NOW(),
                        updated_at       = NOW()
                    WHERE hubspot_contact_funnel.last_modified_at IS NULL
                       OR (EXCLUDED.last_modified_at IS NOT NULL
                           AND EXCLUDED.last_modified_at
                               >= hubspot_contact_funnel.last_modified_at)
                    """,
                    prepared,
                )
        return len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_hubspot_contact_funnel failed: %s", exc)
        return 0


_FUNNEL_SYNC_STATE_FIELDS = {
    "bootstrap_status", "bootstrap_started_at", "bootstrap_completed_at",
    "last_modified_watermark", "last_incremental_at", "earliest_created_at",
    "latest_modified_at", "contacts_seen", "pages_fetched", "last_batch_id",
    "last_error",
}


def get_contact_funnel_sync_state(scope: str = "contacts") -> Optional[dict]:
    """Read the durable contact-funnel sync state, or None when absent/unavailable.

    Completion state lives here, never in process memory: a restarted worker
    resumes from ``last_modified_watermark``.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT scope, bootstrap_status, bootstrap_started_at,
                           bootstrap_completed_at, last_modified_watermark,
                           last_incremental_at, earliest_created_at,
                           latest_modified_at, contacts_seen, pages_fetched,
                           last_batch_id, last_error, updated_at
                    FROM hubspot_contact_funnel_sync_state
                    WHERE scope = %s
                    """,
                    (scope,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
    except Exception as exc:  # noqa: BLE001
        log.error("get_contact_funnel_sync_state failed: %s", exc)
        return None


def update_contact_funnel_sync_state(scope: str = "contacts", **fields) -> bool:
    """Upsert the durable contact-funnel sync state. Never raises.

    Only the documented fields are accepted; unknown keys are ignored so a typo
    can never silently create an unread column.
    """
    updates = {k: v for k, v in fields.items() if k in _FUNNEL_SYNC_STATE_FIELDS}
    if not updates:
        return False

    for ts_field in (
        "bootstrap_started_at", "bootstrap_completed_at", "last_modified_watermark",
        "last_incremental_at", "earliest_created_at", "latest_modified_at",
    ):
        if ts_field in updates:
            updates[ts_field] = _parse_ts_or_none(updates[ts_field])

    columns = list(updates)
    insert_cols = ", ".join(["scope"] + columns)
    placeholders = ", ".join(["%s"] * (len(columns) + 1))
    update_set = ",\n                        ".join(
        f"{c} = EXCLUDED.{c}" for c in columns
    )
    values = [scope] + [updates[c] for c in columns]

    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO hubspot_contact_funnel_sync_state ({insert_cols})
                    VALUES ({placeholders})
                    ON CONFLICT (scope) DO UPDATE SET
                        {update_set},
                        updated_at = NOW()
                    """,
                    values,
                )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("update_contact_funnel_sync_state failed: %s", exc)
        return False
