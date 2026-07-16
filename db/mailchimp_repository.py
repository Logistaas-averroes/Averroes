"""
db/mailchimp_repository.py

Durable persistence + reads for the Mailchimp read-only evidence foundation
(PR-ADS-151).

All writes are idempotent upserts keyed on immutable Mailchimp identity:
  - mailchimp_campaigns          → natural key: campaign_id
  - mailchimp_campaign_reports   → natural key: campaign_id (current-state, one row)
  - mailchimp_campaign_links     → natural key: (campaign_id, link_id)
  - mailchimp_audience_snapshots → natural key: (list_id, snapshot_date)
  - mailchimp_sync_state         → natural key: scope

Repeated syncs therefore UPDATE existing rows and never duplicate campaigns or
metrics. Campaign totals live one-row-per-campaign, never as summable snapshot
rows. Every function is non-fatal (DB failures are logged and swallowed) and
never raises, mirroring the rest of db/writers.py.

Nothing here ever writes to Mailchimp — this module only touches the local DB.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from db.connection import get_conn

log = logging.getLogger(__name__)

MAILCHIMP_SYNC_SCOPE = "campaigns"


# ── coercion helpers ─────────────────────────────────────────────────────────

def _int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _ts(value):
    """Pass ISO strings / datetimes through untouched; blank → None.

    psycopg2 adapts ISO-8601 strings and datetimes into TIMESTAMPTZ directly, so
    we only normalise empties here.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    return s or None


# ── campaigns ────────────────────────────────────────────────────────────────

def upsert_campaigns(rows: list, sync_batch_id: Optional[int] = None) -> dict:
    """Upsert campaign facts keyed on immutable campaign_id.

    Returns ``{prepared, written, skipped_no_id, db_unavailable}``. A row missing
    campaign_id is rejected (fail closed) so an incomplete fact can never be filed
    under a fabricated key.
    """
    stats = {"prepared": 0, "written": 0, "skipped_no_id": 0, "db_unavailable": False}
    prepared = []
    for r in rows or []:
        cid = _clean(r.get("campaign_id"))
        if not cid:
            stats["skipped_no_id"] += 1
            continue
        prepared.append((
            cid, _int(r.get("web_id")), _clean(r.get("list_id")),
            _clean(r.get("campaign_type")), _clean(r.get("status")),
            _clean(r.get("subject_line")), _clean(r.get("title")),
            _ts(r.get("create_time")), _ts(r.get("send_time")),
            _int(r.get("emails_sent")), _int(r.get("recipient_count")),
            _clean(r.get("source_system")) or "mailchimp", sync_batch_id,
        ))
    stats["prepared"] = len(prepared)
    if not prepared:
        return stats

    sql = """
        INSERT INTO mailchimp_campaigns (
            campaign_id, web_id, list_id, campaign_type, status,
            subject_line, title, create_time, send_time,
            emails_sent, recipient_count, source_system, sync_batch_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (campaign_id) DO UPDATE SET
            web_id          = COALESCE(EXCLUDED.web_id, mailchimp_campaigns.web_id),
            list_id         = COALESCE(EXCLUDED.list_id, mailchimp_campaigns.list_id),
            campaign_type   = COALESCE(EXCLUDED.campaign_type, mailchimp_campaigns.campaign_type),
            status          = COALESCE(EXCLUDED.status, mailchimp_campaigns.status),
            subject_line    = COALESCE(EXCLUDED.subject_line, mailchimp_campaigns.subject_line),
            title           = COALESCE(EXCLUDED.title, mailchimp_campaigns.title),
            create_time     = COALESCE(EXCLUDED.create_time, mailchimp_campaigns.create_time),
            send_time       = COALESCE(EXCLUDED.send_time, mailchimp_campaigns.send_time),
            emails_sent     = COALESCE(EXCLUDED.emails_sent, mailchimp_campaigns.emails_sent),
            recipient_count = COALESCE(EXCLUDED.recipient_count, mailchimp_campaigns.recipient_count),
            source_system   = COALESCE(EXCLUDED.source_system, mailchimp_campaigns.source_system),
            sync_batch_id   = COALESCE(EXCLUDED.sync_batch_id, mailchimp_campaigns.sync_batch_id),
            updated_at      = NOW()
    """
    try:
        with get_conn() as conn:
            if conn is None:
                stats["db_unavailable"] = True
                return stats
            with conn.cursor() as cur:
                cur.executemany(sql, prepared)
                stats["written"] = len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_campaigns failed: %s", exc)
        stats["db_unavailable"] = True
    return stats


# ── campaign reports ─────────────────────────────────────────────────────────

def upsert_campaign_reports(rows: list, sync_batch_id: Optional[int] = None) -> dict:
    """Upsert current-state campaign report metrics keyed on campaign_id.

    One row per campaign — metrics are UPDATED in place on each refresh, never
    appended, so recent-campaign refreshes never double-count.
    """
    stats = {"prepared": 0, "written": 0, "skipped_no_id": 0, "db_unavailable": False}
    now = datetime.now(timezone.utc)
    prepared = []
    for r in rows or []:
        cid = _clean(r.get("campaign_id"))
        if not cid:
            stats["skipped_no_id"] += 1
            continue
        prepared.append((
            cid, _clean(r.get("list_id")), _clean(r.get("campaign_type")),
            _clean(r.get("subject_line")), _clean(r.get("title") or r.get("campaign_title")),
            _ts(r.get("send_time")),
            _int(r.get("emails_sent")), _int(r.get("delivered_estimate")),
            _int(r.get("opens_total")), _int(r.get("unique_opens")), _num(r.get("open_rate")),
            _int(r.get("clicks_total")), _int(r.get("unique_clicks")),
            _int(r.get("unique_subscriber_clicks")), _num(r.get("click_rate")),
            _int(r.get("hard_bounces")), _int(r.get("soft_bounces")), _int(r.get("syntax_errors")),
            _int(r.get("unsubscribes")), _int(r.get("abuse_reports")), _int(r.get("forwards_count")),
            _ts(r.get("last_open")), _ts(r.get("last_click")), now,
            _clean(r.get("source_system")) or "mailchimp", sync_batch_id,
        ))
    stats["prepared"] = len(prepared)
    if not prepared:
        return stats

    sql = """
        INSERT INTO mailchimp_campaign_reports (
            campaign_id, list_id, campaign_type, subject_line, title, send_time,
            emails_sent, delivered_estimate, opens_total, unique_opens, open_rate,
            clicks_total, unique_clicks, unique_subscriber_clicks, click_rate,
            hard_bounces, soft_bounces, syntax_errors, unsubscribes, abuse_reports,
            forwards_count, last_open, last_click, last_report_update,
            source_system, sync_batch_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (campaign_id) DO UPDATE SET
            list_id                  = COALESCE(EXCLUDED.list_id, mailchimp_campaign_reports.list_id),
            campaign_type            = COALESCE(EXCLUDED.campaign_type, mailchimp_campaign_reports.campaign_type),
            subject_line             = COALESCE(EXCLUDED.subject_line, mailchimp_campaign_reports.subject_line),
            title                    = COALESCE(EXCLUDED.title, mailchimp_campaign_reports.title),
            send_time                = COALESCE(EXCLUDED.send_time, mailchimp_campaign_reports.send_time),
            emails_sent              = EXCLUDED.emails_sent,
            delivered_estimate       = EXCLUDED.delivered_estimate,
            opens_total              = EXCLUDED.opens_total,
            unique_opens             = EXCLUDED.unique_opens,
            open_rate                = EXCLUDED.open_rate,
            clicks_total             = EXCLUDED.clicks_total,
            unique_clicks            = EXCLUDED.unique_clicks,
            unique_subscriber_clicks = EXCLUDED.unique_subscriber_clicks,
            click_rate               = EXCLUDED.click_rate,
            hard_bounces             = EXCLUDED.hard_bounces,
            soft_bounces             = EXCLUDED.soft_bounces,
            syntax_errors            = EXCLUDED.syntax_errors,
            unsubscribes             = EXCLUDED.unsubscribes,
            abuse_reports            = EXCLUDED.abuse_reports,
            forwards_count           = EXCLUDED.forwards_count,
            last_open                = COALESCE(EXCLUDED.last_open, mailchimp_campaign_reports.last_open),
            last_click               = COALESCE(EXCLUDED.last_click, mailchimp_campaign_reports.last_click),
            last_report_update       = EXCLUDED.last_report_update,
            source_system            = COALESCE(EXCLUDED.source_system, mailchimp_campaign_reports.source_system),
            sync_batch_id            = COALESCE(EXCLUDED.sync_batch_id, mailchimp_campaign_reports.sync_batch_id),
            updated_at               = NOW()
    """
    try:
        with get_conn() as conn:
            if conn is None:
                stats["db_unavailable"] = True
                return stats
            with conn.cursor() as cur:
                cur.executemany(sql, prepared)
                stats["written"] = len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_campaign_reports failed: %s", exc)
        stats["db_unavailable"] = True
    return stats


# ── campaign links ───────────────────────────────────────────────────────────

def upsert_campaign_links(rows: list, sync_batch_id: Optional[int] = None) -> dict:
    """Upsert per-link click metrics keyed on (campaign_id, link_id)."""
    stats = {"prepared": 0, "written": 0, "skipped_no_id": 0, "db_unavailable": False}
    prepared = []
    for r in rows or []:
        cid = _clean(r.get("campaign_id"))
        lid = _clean(r.get("link_id"))
        if not cid or not lid:
            stats["skipped_no_id"] += 1
            continue
        prepared.append((
            cid, lid, _clean(r.get("url")), _int(r.get("total_clicks")),
            _int(r.get("unique_clicks")), _num(r.get("click_percentage")),
            _num(r.get("unique_click_percentage")),
            _clean(r.get("source_system")) or "mailchimp", sync_batch_id,
        ))
    stats["prepared"] = len(prepared)
    if not prepared:
        return stats

    sql = """
        INSERT INTO mailchimp_campaign_links (
            campaign_id, link_id, url, total_clicks, unique_clicks,
            click_percentage, unique_click_percentage, source_system, sync_batch_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (campaign_id, link_id) DO UPDATE SET
            url                     = COALESCE(EXCLUDED.url, mailchimp_campaign_links.url),
            total_clicks            = EXCLUDED.total_clicks,
            unique_clicks           = EXCLUDED.unique_clicks,
            click_percentage        = EXCLUDED.click_percentage,
            unique_click_percentage = EXCLUDED.unique_click_percentage,
            source_system           = COALESCE(EXCLUDED.source_system, mailchimp_campaign_links.source_system),
            sync_batch_id           = COALESCE(EXCLUDED.sync_batch_id, mailchimp_campaign_links.sync_batch_id),
            updated_at              = NOW()
    """
    try:
        with get_conn() as conn:
            if conn is None:
                stats["db_unavailable"] = True
                return stats
            with conn.cursor() as cur:
                cur.executemany(sql, prepared)
                stats["written"] = len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_campaign_links failed: %s", exc)
        stats["db_unavailable"] = True
    return stats


# ── audience snapshots ───────────────────────────────────────────────────────

def upsert_audience_snapshots(rows: list, snapshot_day: Optional[date] = None,
                              sync_batch_id: Optional[int] = None) -> dict:
    """Upsert point-in-time audience state keyed on (list_id, snapshot_date).

    A re-sync on the same day overwrites the day's row (never accumulates).
    """
    stats = {"prepared": 0, "written": 0, "skipped_no_id": 0, "db_unavailable": False}
    snap = snapshot_day or datetime.now(timezone.utc).date()
    prepared = []
    for r in rows or []:
        lid = _clean(r.get("list_id"))
        if not lid:
            stats["skipped_no_id"] += 1
            continue
        prepared.append((
            lid, snap, _clean(r.get("list_name")), _ts(r.get("date_created")),
            _int(r.get("member_count")), _int(r.get("unsubscribe_count")),
            _int(r.get("cleaned_count")), _int(r.get("pending_count")),
            _int(r.get("member_count_since_send")),
            _num(r.get("open_rate")), _num(r.get("click_rate")),
            _int(r.get("campaign_count")),
            _clean(r.get("source_system")) or "mailchimp", sync_batch_id,
        ))
    stats["prepared"] = len(prepared)
    if not prepared:
        return stats

    sql = """
        INSERT INTO mailchimp_audience_snapshots (
            list_id, snapshot_date, list_name, date_created, member_count,
            unsubscribe_count, cleaned_count, pending_count, member_count_since_send,
            open_rate, click_rate, campaign_count, source_system, sync_batch_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (list_id, snapshot_date) DO UPDATE SET
            list_name               = COALESCE(EXCLUDED.list_name, mailchimp_audience_snapshots.list_name),
            date_created            = COALESCE(EXCLUDED.date_created, mailchimp_audience_snapshots.date_created),
            member_count            = EXCLUDED.member_count,
            unsubscribe_count       = EXCLUDED.unsubscribe_count,
            cleaned_count           = EXCLUDED.cleaned_count,
            pending_count           = EXCLUDED.pending_count,
            member_count_since_send = EXCLUDED.member_count_since_send,
            open_rate               = EXCLUDED.open_rate,
            click_rate              = EXCLUDED.click_rate,
            campaign_count          = EXCLUDED.campaign_count,
            source_system           = COALESCE(EXCLUDED.source_system, mailchimp_audience_snapshots.source_system),
            sync_batch_id           = COALESCE(EXCLUDED.sync_batch_id, mailchimp_audience_snapshots.sync_batch_id),
            updated_at              = NOW()
    """
    try:
        with get_conn() as conn:
            if conn is None:
                stats["db_unavailable"] = True
                return stats
            with conn.cursor() as cur:
                cur.executemany(sql, prepared)
                stats["written"] = len(prepared)
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_audience_snapshots failed: %s", exc)
        stats["db_unavailable"] = True
    return stats


# ── mailchimp_sync_state ─────────────────────────────────────────────────────

def get_sync_state(scope: str = MAILCHIMP_SYNC_SCOPE) -> Optional[dict]:
    """Return the durable mailchimp_sync_state row for a scope, or None."""
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM mailchimp_sync_state WHERE scope = %s", (scope,))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                d = dict(zip(cols, row))
                for k in ("backfill_started_at", "backfill_completed_at",
                          "last_incremental_at", "earliest_send_time",
                          "latest_send_time", "updated_at"):
                    if d.get(k) is not None:
                        d[k] = d[k].isoformat() if hasattr(d[k], "isoformat") else str(d[k])
                return d
    except Exception as exc:  # noqa: BLE001
        log.error("get_sync_state failed: %s", exc)
        return None


def update_sync_state(scope: str = MAILCHIMP_SYNC_SCOPE, **fields) -> bool:
    """Upsert mutable mailchimp_sync_state fields for a scope. Never raises."""
    allowed = {
        "backfill_status", "backfill_started_at", "backfill_completed_at",
        "last_incremental_at", "earliest_send_time", "latest_send_time",
        "campaigns_seen", "reports_refreshed", "last_batch_id", "last_error",
    }
    sets, insert_cols, insert_vals, params_update, params_insert = [], ["scope"], ["%s"], [], [scope]
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = %s")
        params_update.append(v)
        insert_cols.append(k)
        insert_vals.append("%s")
        params_insert.append(v)
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    sql = (
        f"INSERT INTO mailchimp_sync_state ({', '.join(insert_cols)}) "
        f"VALUES ({', '.join(insert_vals)}) "
        f"ON CONFLICT (scope) DO UPDATE SET {', '.join(sets)}"
    )
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params_insert + params_update))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("update_sync_state failed (scope=%s): %s", scope, exc)
        return False


# ── reads for API endpoints ──────────────────────────────────────────────────

def coverage() -> dict:
    """Durable coverage summary for status: campaign/report/link/audience counts
    and the send-time span of stored campaigns."""
    out = {
        "campaigns": 0, "reports": 0, "links": 0, "audiences": 0,
        "earliest_send_time": None, "latest_send_time": None,
        "latest_report_update": None, "db_unavailable": False,
    }
    try:
        with get_conn() as conn:
            if conn is None:
                out["db_unavailable"] = True
                return out
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*), MIN(send_time), MAX(send_time) FROM mailchimp_campaigns")
                n, mn, mx = cur.fetchone()
                out["campaigns"] = int(n or 0)
                out["earliest_send_time"] = mn.isoformat() if mn else None
                out["latest_send_time"] = mx.isoformat() if mx else None
                cur.execute("SELECT COUNT(*), MAX(last_report_update) FROM mailchimp_campaign_reports")
                rn, ru = cur.fetchone()
                out["reports"] = int(rn or 0)
                out["latest_report_update"] = ru.isoformat() if ru else None
                cur.execute("SELECT COUNT(*) FROM mailchimp_campaign_links")
                out["links"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(DISTINCT list_id) FROM mailchimp_audience_snapshots")
                out["audiences"] = int(cur.fetchone()[0] or 0)
    except Exception as exc:  # noqa: BLE001
        log.error("coverage failed: %s", exc)
        out["db_unavailable"] = True
    return out


def list_campaigns(limit: int = 100, offset: int = 0,
                   list_id: Optional[str] = None) -> dict:
    """Paginated read of stored campaigns joined to their report metrics.

    Read-only. Returns ``{items, total, db_unavailable}``. No PII — subject/title
    are campaign metadata, not recipient data.
    """
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    where, params = "", []
    if list_id:
        where = "WHERE c.list_id = %s"
        params.append(list_id)
    out = {"items": [], "total": 0, "db_unavailable": False}
    try:
        with get_conn() as conn:
            if conn is None:
                out["db_unavailable"] = True
                return out
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM mailchimp_campaigns c {where}", tuple(params))
                out["total"] = int(cur.fetchone()[0] or 0)
                cur.execute(
                    f"""
                    SELECT c.campaign_id, c.list_id, c.campaign_type, c.status,
                           c.subject_line, c.title, c.send_time, c.emails_sent,
                           r.delivered_estimate, r.opens_total, r.unique_opens,
                           r.clicks_total, r.unique_clicks, r.hard_bounces,
                           r.soft_bounces, r.unsubscribes, r.abuse_reports,
                           r.open_rate, r.click_rate, r.last_report_update
                    FROM mailchimp_campaigns c
                    LEFT JOIN mailchimp_campaign_reports r ON r.campaign_id = c.campaign_id
                    {where}
                    ORDER BY c.send_time DESC NULLS LAST, c.campaign_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    tuple(params + [limit, offset]),
                )
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    d = dict(zip(cols, row))
                    for k in ("send_time", "last_report_update"):
                        if d.get(k) is not None:
                            d[k] = d[k].isoformat()
                    for k in ("open_rate", "click_rate"):
                        if d.get(k) is not None:
                            d[k] = float(d[k])
                    out["items"].append(d)
    except Exception as exc:  # noqa: BLE001
        log.error("list_campaigns failed: %s", exc)
        out["db_unavailable"] = True
    return out


def campaign_detail(campaign_id: str) -> dict:
    """Full stored detail for one campaign: metadata + report + link rows.

    Read-only. Returns ``{found, campaign, report, links, db_unavailable}``.
    """
    out = {"found": False, "campaign": None, "report": None, "links": [],
           "db_unavailable": False}
    try:
        with get_conn() as conn:
            if conn is None:
                out["db_unavailable"] = True
                return out
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM mailchimp_campaigns WHERE campaign_id = %s", (campaign_id,))
                row = cur.fetchone()
                if row:
                    out["found"] = True
                    out["campaign"] = _row_to_jsonable(cur, row)
                cur.execute("SELECT * FROM mailchimp_campaign_reports WHERE campaign_id = %s", (campaign_id,))
                rrow = cur.fetchone()
                if rrow:
                    out["found"] = True
                    out["report"] = _row_to_jsonable(cur, rrow)
                cur.execute(
                    "SELECT * FROM mailchimp_campaign_links WHERE campaign_id = %s "
                    "ORDER BY total_clicks DESC NULLS LAST, link_id",
                    (campaign_id,))
                lcols = [d[0] for d in cur.description]
                for lrow in cur.fetchall():
                    out["links"].append(_dict_jsonable(dict(zip(lcols, lrow))))
    except Exception as exc:  # noqa: BLE001
        log.error("campaign_detail failed (campaign_id=%s): %s", campaign_id, exc)
        out["db_unavailable"] = True
    return out


def latest_audience_snapshots() -> list:
    """Most recent snapshot per list (read-only)."""
    try:
        with get_conn() as conn:
            if conn is None:
                return []
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (list_id) *
                    FROM mailchimp_audience_snapshots
                    ORDER BY list_id, snapshot_date DESC
                    """)
                cols = [d[0] for d in cur.description]
                return [_dict_jsonable(dict(zip(cols, r))) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.error("latest_audience_snapshots failed: %s", exc)
        return []


def recent_sent_campaign_ids(limit: int = 25) -> list:
    """Return the most-recently-sent campaign ids (audit sampling). Read-only.

    Sent-only: a campaign counts as sent when status='sent' AND it has a real
    send_time — drafts / scheduled / paused / cancelled campaigns are excluded so
    the audit never samples an unsent campaign.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return []
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT campaign_id FROM mailchimp_campaigns "
                    "WHERE status = 'sent' AND send_time IS NOT NULL "
                    "ORDER BY send_time DESC NULLS LAST LIMIT %s",
                    (max(1, min(500, int(limit))),))
                return [r[0] for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.error("recent_sent_campaign_ids failed: %s", exc)
        return []


def sent_campaign_ids_for_refresh(window_days: Optional[int] = None) -> list:
    """Campaign ids whose reports should be (re)pulled, read from the DURABLE
    ``mailchimp_campaigns`` table — NOT from whatever a watermark API call just
    returned (PR-ADS-151 §2/§3).

    Only PROVEN-SENT campaigns qualify: ``status='sent'`` AND a real ``send_time``.
    Draft / scheduled / paused / cancelled / unsent campaigns are never selected,
    so they can never produce a report error or keep a backfill partial.

    ``window_days=None`` → every sent campaign (full backfill refresh).
    ``window_days=N``    → sent campaigns whose send_time is within the last N
                           days (the rolling report-refresh window; report metrics
                           keep changing after send).
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return []
            with conn.cursor() as cur:
                if window_days is None:
                    cur.execute(
                        "SELECT campaign_id FROM mailchimp_campaigns "
                        "WHERE status = 'sent' AND send_time IS NOT NULL "
                        "ORDER BY send_time DESC")
                else:
                    cur.execute(
                        "SELECT campaign_id FROM mailchimp_campaigns "
                        "WHERE status = 'sent' AND send_time IS NOT NULL "
                        "AND send_time >= (NOW() - make_interval(days => %s)) "
                        "ORDER BY send_time DESC",
                        (int(window_days),))
                return [r[0] for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.error("sent_campaign_ids_for_refresh failed: %s", exc)
        return []


def fetch_durable_outcome_populations(window_start=None, window_end=None) -> dict:
    """Read Averroes' DURABLE business-truth populations for the attribution audit
    (PR-ADS-151 §4). Read-only. Never recomputes SQL/customer/revenue from live
    HubSpot lifecycle — it reuses the same durable contracts used elsewhere:

      - SQL  = ``leads`` deduped per durable contact key to the LATEST snapshot,
               ``status_category='qualified'``, honouring ``lead_truth_exclusions``,
               windowed by ``contact_created_at`` (the business event date);
      - customers + closed-won revenue = ``deal_source_attribution`` (the complete,
               source-agnostic closed-won ledger) deduped per ``deal_id`` and
               windowed by ``deal_close_date``; revenue is ``SUM(deal_amount_usd)``
               per contact.

    Returns sets of real HubSpot contact_ids + a contact→closed-won amount map, plus
    the durable population sizes (so the audit can prove attributed outcomes are a
    subset). ``db_unavailable`` distinguishes a DB failure from genuinely empty
    populations. Never raises.
    """
    out = {
        "sql_contacts": set(), "customer_contacts": set(),
        "closed_won_by_contact": {},
        "durable_sql_population": 0, "durable_customer_population": 0,
        "durable_closed_won_population": 0,
        "db_unavailable": False,
        "window_start": window_start.isoformat() if hasattr(window_start, "isoformat") else window_start,
        "window_end": window_end.isoformat() if hasattr(window_end, "isoformat") else window_end,
    }
    try:
        with get_conn() as conn:
            if conn is None:
                out["db_unavailable"] = True
                return out
            with conn.cursor() as cur:
                # ── SQL / qualified contacts (durable `leads` contract) ────────
                cur.execute(
                    """
                    WITH deduped AS (
                        SELECT DISTINCT ON (COALESCE(NULLIF(contact_id,''),'id:'||id::text))
                            NULLIF(contact_id,'') AS contact_id,
                            status_category
                        FROM leads
                        WHERE contact_created_at IS NOT NULL
                          AND (%s::timestamptz IS NULL OR contact_created_at >= %s::timestamptz)
                          AND (%s::date IS NULL OR contact_created_at < (%s::date + INTERVAL '1 day'))
                          AND COALESCE(NULLIF(contact_id,''),'id:'||id::text)
                              NOT IN (SELECT lead_id FROM lead_truth_exclusions)
                        ORDER BY COALESCE(NULLIF(contact_id,''),'id:'||id::text),
                                 run_date DESC, id DESC
                    )
                    SELECT contact_id FROM deduped
                    WHERE status_category = 'qualified' AND contact_id IS NOT NULL
                    """,
                    (window_start, window_start, window_end, window_end),
                )
                out["sql_contacts"] = {str(r[0]) for r in cur.fetchall() if r[0]}

                # ── customers + closed-won revenue (durable deal ledger) ───────
                cur.execute(
                    """
                    WITH won AS (
                        SELECT DISTINCT ON (deal_id)
                            deal_id,
                            associated_contact_id AS contact_id,
                            deal_amount_usd
                        FROM deal_source_attribution
                        WHERE deal_id IS NOT NULL
                          AND associated_contact_id IS NOT NULL
                          AND deal_close_date IS NOT NULL
                          AND (%s::date IS NULL OR deal_close_date >= %s::date)
                          AND (%s::date IS NULL OR deal_close_date < (%s::date + INTERVAL '1 day'))
                        ORDER BY deal_id, classified_at DESC
                    )
                    SELECT contact_id, SUM(COALESCE(deal_amount_usd, 0)) AS revenue
                    FROM won GROUP BY contact_id
                    """,
                    (window_start, window_start, window_end, window_end),
                )
                for cid, revenue in cur.fetchall():
                    cid = str(cid)
                    amt = float(revenue or 0)
                    out["customer_contacts"].add(cid)
                    if amt > 0:
                        out["closed_won_by_contact"][cid] = amt

        out["durable_sql_population"] = len(out["sql_contacts"])
        out["durable_customer_population"] = len(out["customer_contacts"])
        out["durable_closed_won_population"] = len(out["closed_won_by_contact"])
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_durable_outcome_populations failed: %s", exc)
        out["db_unavailable"] = True
    return out


def _row_to_jsonable(cur, row) -> dict:
    cols = [d[0] for d in cur.description]
    return _dict_jsonable(dict(zip(cols, row)))


def _dict_jsonable(d: dict) -> dict:
    from decimal import Decimal  # noqa: PLC0415
    out: dict[str, Any] = {}
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out
