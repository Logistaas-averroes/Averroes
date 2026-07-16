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
import os
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

# PR-ADS-151 §5 — durable backfill lease (reuses the revenue_recovery_jobs lease).
BACKFILL_JOB_TYPE = "mailchimp_backfill"
LEASE_TTL_SECONDS = int(os.getenv("MAILCHIMP_BACKFILL_LEASE_TTL_SECONDS", "900"))  # 15 min

# Fast in-process guard (the DB lease is the authoritative cross-process guard).
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
    if not cfg["has_api_key"]:
        return False, _skip_result("not_configured", "MAILCHIMP_API_KEY is not set")
    if not cfg["server_prefix"]:
        return False, _skip_result(
            "not_configured", "Mailchimp server prefix could not be resolved "
            "(use a -usXX key suffix or set MAILCHIMP_SERVER_PREFIX)")
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
            # Normalise a trailing 'Z' (Mailchimp send_time uses it) to +00:00 so
            # a Z-suffixed watermark never fails to parse and silently re-pulls
            # every campaign each incremental run.
            wm = datetime.fromisoformat(str(watermark).replace("Z", "+00:00"))
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
    audience snapshot. Idempotent (natural-key upserts).

    PR-ADS-151 §5 — concurrency + checkpoint safety. Guarded by a DURABLE atomic
    lease (revenue_recovery_jobs, job_type='mailchimp_backfill') reusing the proven
    Keyword-Evidence recovery-job pattern:
      - at most ONE active backfill across workers/deploys (partial unique index);
      - a crashed worker's stale lease is recovered by the next run;
      - Mailchimp is NEVER called if the durable checkpoint cannot be created (fail
        closed on DB-unavailable);
      - final ``success`` is reported ONLY after durable completion is persisted.
    """
    ready, skip = _preflight()
    if not ready:
        return skip
    import uuid  # noqa: PLC0415
    import db.writers as w  # noqa: PLC0415
    from db import mailchimp_repository as repo  # noqa: PLC0415

    today = _now().date()
    lease_token = uuid.uuid4().hex
    claim = w.acquire_recovery_lease(
        BACKFILL_JOB_TYPE, lease_token, LEASE_TTL_SECONDS,
        date_from=today, date_to=today, chunk_months=1)

    if not claim.get("claimed"):
        reason = claim.get("reason")
        if reason == "db_unavailable":
            # No durable checkpoint → do NOT touch Mailchimp.
            return {"status": "failed", "reason": "durable_job_unavailable",
                    "ok": False, "datasets": {},
                    "error": "could not create/claim a durable Mailchimp backfill "
                             "job (database unavailable) — refusing to run without a "
                             "checkpoint"}
        if reason == "active_lease":
            return {"status": "running", "reason": "already_running", "ok": False,
                    "datasets": {}, "job_id": (claim.get("job") or {}).get("job_id")}
        return {"status": "skipped", "reason": reason, "ok": False, "datasets": {},
                "job_id": (claim.get("job") or {}).get("job_id")}

    job = claim["job"]
    job_id = job["job_id"]
    repo.update_sync_state(backfill_status="running", backfill_started_at=_now(),
                           last_error=None)

    result = _sync_campaigns(sync_type="backfill", since_send_time=None,
                             refresh_recent_days=None, full=True, run_id=run_id)
    # Heartbeat between phases so a long report refresh does not let the lease lapse.
    w.renew_recovery_lease(job_id, lease_token, LEASE_TTL_SECONDS, current_chunk="audiences")
    result["datasets"][DS_AUDIENCES] = _sync_audiences(sync_type="backfill", run_id=run_id)

    ok = all(d.get("ok") for d in result["datasets"].values() if isinstance(d, dict))
    errs = "; ".join(
        str(d.get("error")) for d in result["datasets"].values()
        if isinstance(d, dict) and d.get("error"))
    final_status = "success" if ok else "partial"

    summary = {
        "campaigns": (result["datasets"].get(DS_CAMPAIGNS) or {}).get("written"),
        "reports_refreshed": (result["datasets"].get(DS_REPORTS) or {}).get("refreshed"),
        "audiences": (result["datasets"].get(DS_AUDIENCES) or {}).get("written"),
        "completed_at": _now().isoformat(),
    }
    released = w.release_recovery_lease(
        job_id, lease_token, status=final_status, summary=summary,
        finished_at=_now().isoformat())
    if not released:
        # Final checkpoint did not persist (lease lost / DB down). Do NOT claim
        # success — the job stays recoverable via stale-lease takeover.
        repo.update_sync_state(backfill_status="partial",
                               last_error="final backfill checkpoint did not persist")
        return {"status": "interrupted", "reason": "final_checkpoint_failed",
                "ok": False, "job_id": job_id, "datasets": result["datasets"],
                "error": "could not persist the final Mailchimp backfill checkpoint; "
                         "the job remains recoverable via stale-lease takeover"}

    if ok:
        repo.update_sync_state(backfill_status="complete",
                               backfill_completed_at=_now(), last_error=None)
    else:
        repo.update_sync_state(backfill_status="partial",
                               last_error=errs[:1000] or "partial backfill")
    result["ok"] = ok
    result["status"] = final_status
    result["job_id"] = job_id
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
    # PR-ADS-151 §2/§3: the rolling report-refresh set is selected from the DURABLE
    # mailchimp_campaigns table (proven-sent campaigns in the rolling window), NOT
    # from whatever the discovery/watermark call above returned. So a campaign sent
    # 15 days ago is still refreshed even when discovery finds no new campaigns, and
    # unsent campaigns are never requested (no spurious report failures).
    report_batch = w.start_sync_batch(source=MAILCHIMP_SOURCE, dataset=DS_REPORTS,
                                      sync_type=sync_type, run_id=run_id)
    rep_ds = {"ok": False, "refreshed": 0, "links": 0, "errors": 0,
              "batch_id": report_batch or None}

    to_refresh = repo.sent_campaign_ids_for_refresh(
        window_days=None if full else refresh_recent_days)
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
    import db.writers as w  # noqa: PLC0415
    from db import mailchimp_repository as repo  # noqa: PLC0415
    # A live durable lease means a worker already owns the backfill — not needed.
    job = w.get_latest_recovery_job(BACKFILL_JOB_TYPE)
    if job and job.get("status") == "running" and _lease_active(job):
        return False
    state = repo.get_sync_state()
    if not state:
        return True
    return state.get("backfill_status") != "complete"


def _lease_active(job: dict) -> bool:
    """True when the job holds a non-expired durable lease."""
    exp = (job or {}).get("lease_expires_at")
    if exp is None:
        return False
    if isinstance(exp, str):
        try:
            exp = datetime.fromisoformat(exp)
        except ValueError:
            return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


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
