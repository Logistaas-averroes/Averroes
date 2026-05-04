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


def write_campaigns(run_id: int, campaigns: list) -> None:
    """Insert campaign rows for this run.

    Each item in *campaigns* should be a dict produced by analysis/core.py
    (campaign truth table output).  Missing keys default to None.
    Never raises.
    """
    if run_id is None:
        log.debug("write_campaigns skipped — run_id is None")
        return
    if not campaigns:
        return
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
                return
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
    except Exception as exc:  # noqa: BLE001
        log.error("write_campaigns failed (run_id=%s): %s", run_id, exc)


def write_leads(run_id: int, contacts: list) -> None:
    """Insert lead rows for this run.

    Each item in *contacts* should be a dict produced by hubspot_pull.py.
    mql_status is mapped to status_category automatically.
    Never raises.
    """
    if run_id is None:
        log.debug("write_leads skipped — run_id is None")
        return
    if not contacts:
        return
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
        ))
    try:
        with get_conn() as conn:
            if conn is None:
                return
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO leads (
                        run_id, run_date, contact_id, campaign_name,
                        keyword, country, mql_status, status_category, gclid, source_type, company
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        log.info("Wrote %d lead rows to database (run_id=%s)", len(rows), run_id)
    except Exception as exc:  # noqa: BLE001
        log.error("write_leads failed (run_id=%s): %s", run_id, exc)


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


def write_deals(run_id: int, deals: list) -> None:
    """Insert deal rows for this run.

    Each item in *deals* should be a dict produced by hubspot_pull.py
    (pull_deals_with_gclid output).
    Never raises.
    """
    if run_id is None:
        log.debug("write_deals skipped — run_id is None")
        return
    if not deals:
        return
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
                return
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
    except Exception as exc:  # noqa: BLE001
        log.error("write_deals failed (run_id=%s): %s", run_id, exc)


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
        return 0

    today = _today()
    rows = []

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
            log.warning("write_search_terms: skipping row with blank search_term")
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
            COALESCE(search_term,   '')
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
                # executemany rowcount reflects the last statement in psycopg2;
                # use attempted count as a reliable proxy for high-volume datasets.
                attempted = len(rows)
        log.info(
            "write_search_terms: upserted %d rows (run_id=%s)", attempted, run_id
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
