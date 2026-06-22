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

    Accepts rows from pull_search_terms() (windsor connector format).
    Preserves existing is_flagged_waste / junk_category / matched_pattern on
    conflict — raw write is NOT allowed to override waste classifications.

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
            sync_batch_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (
            source_date,
            COALESCE(campaign_name, ''),
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
            campaign_id   = COALESCE(EXCLUDED.campaign_id,
                                     search_terms.campaign_id),
            updated_at    = NOW()
    """

    try:
        with get_conn() as conn:
            if conn is None:
                return 0
            with conn.cursor() as cur:
                cur.executemany(_upsert_sql, rows)
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


# ---------------------------------------------------------------------------
# Sync tracking helpers (PR-ADS-039)
# ---------------------------------------------------------------------------

# Allowed values — used for normalisation/validation; not hard-fail guards so
# that new sources/datasets can be added to the system without a code deploy.
VALID_SYNC_SOURCES   = {"windsor", "hubspot", "gclid"}
VALID_SYNC_DATASETS  = {"campaigns", "keywords", "search_terms", "geo",
                        "contacts", "deals", "matches"}
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
