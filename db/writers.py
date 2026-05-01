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


def _clean_campaign_name(campaign_name: Optional[str]) -> Optional[str]:
    """Normalise campaign name for consistent storage.

    - Lowercase and strip whitespace
    - Return None for HubSpot traffic source pseudo-names
    - Return None for HubSpot email campaign ID strings
    - Return None for empty/null values
    """
    if not campaign_name:
        return None

    stripped = campaign_name.strip()

    if stripped.lower() in _HUBSPOT_SOURCE_PSEUDONAMES:
        return None

    if _EMAIL_CAMPAIGN_PATTERN.search(stripped):
        return None

    return stripped.lower()


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
        campaign_name = raw_name.strip().lower() if raw_name else None
        rows.append((
            run_id,
            run_date,
            campaign_name,
            _float_or_none(c.get("spend_usd") or c.get("spend_30d_usd") or c.get("spend")),
            _int_or_none(c.get("clicks")),
            _int_or_none(c.get("impressions")),
            _float_or_none(c.get("conversions")),
            _int_or_none(c.get("total_leads")),
            _int_or_none(c.get("confirmed_sqls")),
            _int_or_none(c.get("junk_count")),
            _float_or_none(c.get("junk_rate_pct")),
            _float_or_none(c.get("cpql_usd")),
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
                        keyword, country, mql_status, status_category, gclid, source_type
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
