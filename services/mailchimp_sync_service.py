"""
services/mailchimp_sync_service.py

Read-only Mailchimp synchronisation (PR-ADS-151).

ONE shared path keeps the durable Mailchimp evidence tables current and complete:

  - ``run_backfill()``     — full historical campaign + report + link + audience
                             pull. Idempotent (natural-key upserts) so repeated
                             runs never duplicate campaigns or metrics.
  - ``run_incremental()``  — daily refresh: pull campaigns since the watermark and
                             re-pull reports/links for campaigns inside a rolling
                             recent window (metrics keep changing after send), plus
                             a fresh audience snapshot.
  - ``maybe_start_backfill_on_deploy()`` — spawns the backfill on a daemon thread
                             when durable coverage is empty / backfill incomplete.

Every pull is GET-only (governance enforced in the connector). Freshness is
tracked through the shared ``sync_batches`` / ``sync_state`` tables (datasets
mailchimp/campaigns, mailchimp/reports, mailchimp/audiences) AND the Mailchimp-
specific ``mailchimp_sync_state`` backfill/rolling lifecycle table.

Nothing here ever writes to Mailchimp.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MAILCHIMP_SOURCE = "mailchimp"
DS_CAMPAIGNS = "campaigns"
DS_REPORTS = "reports"
DS_AUDIENCES = "audiences"

# Rolling window (days) over which sent-campaign reports are refreshed because
# opens/clicks/bounces/unsubscribes keep changing after a send.
DEFAULT_REFRESH_RECENT_DAYS = 30
# Small overlap so a campaign sent right on the watermark boundary is not missed.
INCREMENTAL_OVERLAP_DAYS = 2

_backfill_lock = threading.Lock()
_backfill_running = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _skip_result(reason: str, detail: str) -> dict:
    return {"status": "skipped", "reason": reason, "detail": detail,
            "datasets": {}, "ok": False}


# ── Guarded entry points ──────────────────────────────────────────────────────

def _preflight() -> tuple[bool, dict]:
    """Return (ready, skip_result). Never touches the network on failure."""
    from connectors import mailchimp_pull as mc  # noqa: PLC0415
    cfg = mc.config_status()
    if not cfg["enabled"]:
        return False, _skip_result("not_configured", "MAILCHIMP_ENABLED is off")
    if not cfg["has_api_key"]:
        return False, _skip_result("not_configured", "MAILCHIMP_API_KEY is not set")
    if not cfg["server_prefix"]:
        return False, _skip_result(
            "not_configured", "Mailchimp server prefix could not be resolved")
    return True, {}


def run_incremental(*, refresh_recent_days: int = DEFAULT_REFRESH_RECENT_DAYS,
                    run_id=None) -> dict:
    """Daily incremental: pull recent campaigns + refresh recent reports + snapshot
    audiences. Idempotent. Returns a structured summary; never raises."""
    ready, skip = _preflight()
    if not ready:
        return skip
    from db import mailchimp_repository as repo  # noqa: PLC0415

    state = repo.get_sync_state() or {}
    watermark = state.get("latest_send_time")
    since = None
    if watermark:
        try:
            wm = datetime.fromisoformat(str(watermark))
            since = _iso(wm - timedelta(days=INCREMENTAL_OVERLAP_DAYS))
        except (ValueError, TypeError):
            since = None

    result = _sync_campaigns(sync_type="daily", since_send_time=since,
                             refresh_recent_days=refresh_recent_days,
                             full=False, run_id=run_id)
    result["datasets"][DS_AUDIENCES] = _sync_audiences(sync_type="daily", run_id=run_id)
    repo.update_sync_state(last_incremental_at=_now())
    result["ok"] = all(
        d.get("ok") for d in result["datasets"].values() if isinstance(d, dict)
    )
    result["status"] = "success" if result["ok"] else "partial"
    return result


def run_backfill(*, run_id=None) -> dict:
    """Full historical backfill of every campaign + report + link, plus a current
    audience snapshot. Idempotent (natural-key upserts). Returns a summary."""
    ready, skip = _preflight()
    if not ready:
        return skip
    from db import mailchimp_repository as repo  # noqa: PLC0415

    repo.update_sync_state(backfill_status="running", backfill_started_at=_now(),
                           last_error=None)
    result = _sync_campaigns(sync_type="backfill", since_send_time=None,
                             refresh_recent_days=None, full=True, run_id=run_id)
    result["datasets"][DS_AUDIENCES] = _sync_audiences(sync_type="backfill", run_id=run_id)

    ok = all(d.get("ok") for d in result["datasets"].values() if isinstance(d, dict))
    if ok:
        repo.update_sync_state(backfill_status="complete",
                               backfill_completed_at=_now(), last_error=None)
    else:
        errs = "; ".join(
            str(d.get("error")) for d in result["datasets"].values()
            if isinstance(d, dict) and d.get("error"))
        repo.update_sync_state(backfill_status="partial", last_error=errs[:1000] or "partial backfill")
    result["ok"] = ok
    result["status"] = "success" if ok else "partial"
    return result


# ── Core campaign + report sync ───────────────────────────────────────────────

def _sync_campaigns(*, sync_type: str, since_send_time, refresh_recent_days,
                    full: bool, run_id) -> dict:
    """Pull campaigns, upsert them, then refresh reports + links for the campaigns
    that need it. Creates a mailchimp/campaigns batch and a mailchimp/reports
    batch so both datasets get independent freshness."""
    from connectors import mailchimp_pull as mc  # noqa: PLC0415
    from db import mailchimp_repository as repo  # noqa: PLC0415
    import db.writers as w  # noqa: PLC0415

    today = _now().date()
    out = {"datasets": {}}

    # ── campaigns dataset ─────────────────────────────────────────────────────
    camp_batch = w.start_sync_batch(source=MAILCHIMP_SOURCE, dataset=DS_CAMPAIGNS,
                                    sync_type=sync_type, run_id=run_id)
    campaigns = []
    camp_ds = {"ok": False, "fetched": 0, "written": 0, "batch_id": camp_batch or None}
    try:
        campaigns = mc.list_campaigns(since_send_time=since_send_time)
        stats = repo.upsert_campaigns(campaigns, sync_batch_id=camp_batch or None)
        camp_ds.update(fetched=len(campaigns), **stats)
        camp_ok = (not stats.get("db_unavailable")
                   and stats.get("written", 0) == stats.get("prepared", 0))
        camp_ds["ok"] = camp_ok
        if camp_batch:
            w.finish_sync_batch(
                batch_id=camp_batch,
                status="success" if camp_ok else "failed",
                row_count=stats.get("written", 0),
                last_source_date=today if camp_ok else None,
                error_message=None if camp_ok else f"campaign upsert incomplete: {stats}")
    except mc.MailchimpError as exc:
        camp_ds["error"] = str(exc)
        camp_ds["error_kind"] = type(exc).__name__
        if camp_batch:
            w.finish_sync_batch(batch_id=camp_batch, status="failed", row_count=0,
                                error_message=str(exc))
        repo.update_sync_state(last_error=str(exc)[:1000])
        out["datasets"][DS_CAMPAIGNS] = camp_ds
        out["datasets"][DS_REPORTS] = {"ok": False, "error": str(exc),
                                       "error_kind": type(exc).__name__, "refreshed": 0}
        return out
    out["datasets"][DS_CAMPAIGNS] = camp_ds

    # ── reports dataset (+ links) ─────────────────────────────────────────────
    report_batch = w.start_sync_batch(source=MAILCHIMP_SOURCE, dataset=DS_REPORTS,
                                      sync_type=sync_type, run_id=run_id)
    rep_ds = {"ok": False, "refreshed": 0, "links": 0, "errors": 0,
              "batch_id": report_batch or None}

    to_refresh = _campaigns_needing_report(campaigns, today, refresh_recent_days, full)
    refreshed = links_written = errors = 0
    first_error = None
    for cid in to_refresh:
        try:
            report = mc.get_campaign_report(cid)
            rstats = repo.upsert_campaign_reports([report], sync_batch_id=report_batch or None)
            if rstats.get("written"):
                refreshed += 1
            links = mc.get_campaign_link_details(cid)
            lstats = repo.upsert_campaign_links(links, sync_batch_id=report_batch or None)
            links_written += lstats.get("written", 0)
        except mc.MailchimpRateLimited as exc:
            # Rate limit is terminal for this run — stop hammering the API.
            first_error = first_error or str(exc)
            errors += 1
            logger.warning("Mailchimp report refresh rate limited at %s: %s", cid, exc)
            break
        except mc.MailchimpError as exc:
            first_error = first_error or str(exc)
            errors += 1
            logger.warning("Mailchimp report refresh failed for %s: %s", cid, exc)
            continue

    rep_ds.update(refreshed=refreshed, links=links_written, errors=errors,
                  attempted=len(to_refresh))
    rep_ok = errors == 0
    rep_ds["ok"] = rep_ok
    if first_error:
        rep_ds["error"] = first_error
    if report_batch:
        w.finish_sync_batch(
            batch_id=report_batch,
            status="success" if rep_ok else "failed",
            row_count=refreshed,
            last_source_date=today if rep_ok else None,
            error_message=None if rep_ok else f"{errors} report refresh error(s): {first_error}")
    out["datasets"][DS_REPORTS] = rep_ds

    # ── update mailchimp_sync_state watermarks ────────────────────────────────
    send_times = [c.get("send_time") for c in campaigns if c.get("send_time")]
    updates = {"campaigns_seen": len(campaigns), "reports_refreshed": refreshed,
               "last_batch_id": report_batch or camp_batch or None}
    if send_times:
        state = repo.get_sync_state() or {}
        newest = max(send_times)
        oldest = min(send_times)
        prev_latest = state.get("latest_send_time")
        prev_earliest = state.get("earliest_send_time")
        updates["latest_send_time"] = (
            max(newest, str(prev_latest)) if prev_latest else newest)
        updates["earliest_send_time"] = (
            min(oldest, str(prev_earliest)) if prev_earliest else oldest)
    repo.update_sync_state(**updates)
    return out


def _campaigns_needing_report(campaigns, today, refresh_recent_days, full) -> list:
    """Which campaign ids to (re)pull reports for.

    Backfill (full=True): every campaign. Incremental: campaigns whose send_time
    falls inside the rolling recent window (metrics may still be moving)."""
    ids = []
    cutoff = None
    if not full and refresh_recent_days is not None:
        cutoff = today - timedelta(days=refresh_recent_days)
    for c in campaigns:
        cid = c.get("campaign_id")
        if not cid:
            continue
        if full or cutoff is None:
            ids.append(cid)
            continue
        st = c.get("send_time")
        if not st:
            # Unsent/draft — no report to refresh in incremental mode.
            continue
        try:
            sd = datetime.fromisoformat(str(st).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            ids.append(cid)
            continue
        if sd >= cutoff:
            ids.append(cid)
    return ids


# ── Audiences ─────────────────────────────────────────────────────────────────

def _sync_audiences(*, sync_type: str, run_id) -> dict:
    """Pull lists and write a point-in-time audience snapshot for today."""
    from connectors import mailchimp_pull as mc  # noqa: PLC0415
    from db import mailchimp_repository as repo  # noqa: PLC0415
    import db.writers as w  # noqa: PLC0415

    today = _now().date()
    batch = w.start_sync_batch(source=MAILCHIMP_SOURCE, dataset=DS_AUDIENCES,
                               sync_type=sync_type, run_id=run_id)
    ds = {"ok": False, "fetched": 0, "written": 0, "batch_id": batch or None}
    try:
        audiences = mc.list_audiences()
        stats = repo.upsert_audience_snapshots(audiences, snapshot_day=today,
                                               sync_batch_id=batch or None)
        ds.update(fetched=len(audiences), **stats)
        ok = (not stats.get("db_unavailable")
              and stats.get("written", 0) == stats.get("prepared", 0))
        ds["ok"] = ok
        if batch:
            w.finish_sync_batch(batch_id=batch, status="success" if ok else "failed",
                                row_count=stats.get("written", 0),
                                last_source_date=today if ok else None,
                                error_message=None if ok else f"audience upsert incomplete: {stats}")
    except mc.MailchimpError as exc:
        ds["error"] = str(exc)
        ds["error_kind"] = type(exc).__name__
        if batch:
            w.finish_sync_batch(batch_id=batch, status="failed", row_count=0,
                                error_message=str(exc))
    return ds


# ── Auto backfill on deploy ───────────────────────────────────────────────────

def _backfill_needed() -> bool:
    from db import mailchimp_repository as repo  # noqa: PLC0415
    state = repo.get_sync_state()
    if not state:
        return True
    if state.get("backfill_status") in ("complete",):
        return False
    return True


def maybe_start_backfill_on_deploy() -> str:
    """Spawn the full backfill on a daemon thread when Mailchimp is configured and
    the backfill has not completed. Never blocks startup. Returns a status string."""
    global _backfill_running
    try:
        ready, _skip = _preflight()
        if not ready:
            return "not_configured"
        if not _backfill_needed():
            return "not_needed"
        with _backfill_lock:
            if _backfill_running:
                return "already_running"
            _backfill_running = True

        def _worker():
            global _backfill_running
            try:
                run_backfill()
            except Exception as exc:  # noqa: BLE001
                logger.error("Mailchimp backfill worker failed: %s", exc)
            finally:
                with _backfill_lock:
                    _backfill_running = False

        threading.Thread(target=_worker, name="mailchimp-backfill", daemon=True).start()
        return "started"
    except Exception as exc:  # noqa: BLE001
        logger.error("maybe_start_backfill_on_deploy failed: %s", exc)
        return "error"
