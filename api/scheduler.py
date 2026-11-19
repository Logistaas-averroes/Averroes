"""
api/scheduler.py

In-app APScheduler for the Logistaas Ads Intelligence System.

Phase 1 — Read Only.

Responsibility:
  - Manage in-app scheduled jobs for the daily and monthly Phase 1 runs,
    the daily incremental sync and the daily platform certification.
  - Provide shared in-memory concurrency guards used by api/server.py manual
    run endpoints, so scheduled and manual runs cannot overlap.
  - Log job start, success, and failure.
  - NO business logic — only calls existing scheduler module functions.
  - NO Google Ads write-back. NO HubSpot write-back. NO OCT. NO negative
    keyword push.

Schedules (Asia/Amman = UTC+3, no DST since Jordan suspended DST in 2022):
  Daily pulse     — every day at 06:00 Asia/Amman  (03:00 UTC)
  Monthly data refresh — 1st of month at 08:00 Asia/Amman (05:00 UTC)

PR-ADS-162H: the monthly job no longer generates or emails a report. Its job
id, schedule and run type are unchanged.

PR-ADS-165A: the weekly data refresh (Mondays 07:00 Asia/Amman) is retired by
owner decision. Render killed it at the 512 MB memory limit every Monday from
2026-10-19 at the latest, and its last recorded success was 2026-06-01. Every
dataset it wrote is also written by the monthly job, the daily pulse or the
daily incremental sync; its one unique step, the search-term flag history, now
runs in the monthly job. It is not registered and has no manual trigger;
`weekly` is recorded in `RETIRED_JOB_IDS`, and a test holds that no retired id
is also a job id. Historical `weekly` rows stay in the `runs` table as history.
"""

import logging
import threading
from datetime import timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

# Timezone for all scheduled jobs.
# Asia/Amman = UTC+3 year-round (Jordan suspended daylight saving time in 2022).
_TZ = ZoneInfo("Asia/Amman")

# ---------------------------------------------------------------------------
# Shared in-memory concurrency guards.
# Imported and used by api/server.py manual run endpoints so that scheduled
# runs and manual runs share the same lock and cannot overlap.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Canonical job IDs — single source of truth for every scheduled job.
# Used by _job_state, start_scheduler(), get_registered_job_ids(), and
# get_scheduler_status() to avoid duplication that could cause drift.
# ---------------------------------------------------------------------------

JOB_IDS: tuple[str, ...] = ("daily", "monthly", "daily_incremental_sync",
                            "platform_certification")

#: PR-ADS-165A — job ids that used to be scheduled and are not any more, with
#: the reason. A retired id has no scheduler registration, no `_job_state`
#: slot, no schedule description and no `/run/<id>` endpoint.
RETIRED_JOB_IDS: dict[str, str] = {
    "weekly": ("Retired in PR-ADS-165A: killed by the host at the 512 MB "
               "memory limit every Monday; every dataset it wrote has another "
               "scheduled writer."),
}

_job_state: dict[str, bool] = {job_id: False for job_id in JOB_IDS}
_run_lock = threading.Lock()

# ---------------------------------------------------------------------------
# APScheduler instance
# ---------------------------------------------------------------------------

_scheduler: BackgroundScheduler | None = None

# Human-readable schedule descriptions (UTC equivalents noted).
_SCHEDULE_DESCRIPTIONS: dict[str, str] = {
    "daily": "06:00 Asia/Amman (03:00 UTC)",
    "monthly": "1st of month 08:00 Asia/Amman (05:00 UTC)",
    "daily_incremental_sync": "09:00 Asia/Amman (06:00 UTC)",
    # PR-ADS-161J: after the incremental sync has had 90 minutes to finish.
    "platform_certification": "10:30 Asia/Amman (07:30 UTC)",
}


# ---------------------------------------------------------------------------
# Scheduled job wrappers — call existing scheduler modules only.
# ---------------------------------------------------------------------------

def _run_daily_scheduled() -> None:
    """Scheduled wrapper for the daily pulse."""
    with _run_lock:
        if _job_state["daily"]:
            log.warning("[scheduler/daily] skipped — already running (manual or previous scheduled run still active)")
            return
        _job_state["daily"] = True

    log.info("[scheduler/daily] started (scheduled)")
    try:
        from scheduler.daily import run_daily_pulse  # noqa: PLC0415
        run_daily_pulse()
        log.info("[scheduler/daily] succeeded")
    except Exception as exc:  # noqa: BLE001
        log.error("[scheduler/daily] failed: %s", exc, exc_info=True)
    finally:
        with _run_lock:
            _job_state["daily"] = False


def _log_data_refresh_outcome(job: str, outcome) -> None:
    """Log what the monthly data refresh reported about itself.

    PR-ADS-162H. "Did not raise" used to be logged as "succeeded", which was
    false for a monthly run that had recorded itself `failed` and returned
    None. The job's own outcome is logged instead; one that is missing or
    unrecognised is an error, never a success.
    """
    status = outcome.get("status") if isinstance(outcome, dict) else None
    steps = "; ".join(str(s) for s in (outcome.get("failed_steps") or [])) \
        if isinstance(outcome, dict) else ""
    if status == "success":
        log.info("[scheduler/%s] succeeded", job)
    elif status == "partial":
        log.warning("[scheduler/%s] finished partially — did not land: %s", job, steps)
    elif status == "failed":
        log.error("[scheduler/%s] failed at: %s", job, steps)
    else:
        log.error("[scheduler/%s] returned no recognisable outcome: %r", job, outcome)


def _run_monthly_scheduled() -> None:
    """Scheduled wrapper for the monthly data refresh."""
    with _run_lock:
        if _job_state["monthly"]:
            log.warning("[scheduler/monthly] skipped — already running (manual or previous scheduled run still active)")
            return
        _job_state["monthly"] = True

    log.info("[scheduler/monthly] started (scheduled)")
    try:
        from scheduler.monthly import run_monthly_report  # noqa: PLC0415
        _log_data_refresh_outcome("monthly", run_monthly_report())
    except Exception as exc:  # noqa: BLE001
        log.error("[scheduler/monthly] failed: %s", exc, exc_info=True)
    finally:
        with _run_lock:
            _job_state["monthly"] = False


def _run_daily_incremental_sync_scheduled() -> None:
    """Scheduled wrapper for the daily incremental sync (9 AM Asia/Amman)."""
    with _run_lock:
        if _job_state["daily_incremental_sync"]:
            log.warning(
                "[scheduler/daily_incremental_sync] skipped — already running "
                "(manual or previous scheduled run still active)"
            )
            return
        _job_state["daily_incremental_sync"] = True

    log.info("[scheduler/daily_incremental_sync] started (scheduled)")
    try:
        from scheduler.incremental_sync import run_daily_incremental_sync  # noqa: PLC0415
        run_daily_incremental_sync(run_reason="scheduled")
        log.info("[scheduler/daily_incremental_sync] succeeded")
    except Exception as exc:  # noqa: BLE001
        log.error("[scheduler/daily_incremental_sync] failed: %s", exc, exc_info=True)
    finally:
        with _run_lock:
            _job_state["daily_incremental_sync"] = False


def _run_platform_certification_scheduled() -> None:
    """Scheduled wrapper for the daily platform certification (PR-ADS-161J).

    Runs the six production truth audits through
    ``scripts.audit_platform_certification`` and records ONE verdict row in
    ``platform_certification_runs``. The service never raises and records
    NOT CERTIFIED when the certification itself cannot run.
    """
    with _run_lock:
        if _job_state["platform_certification"]:
            log.warning("[scheduler/platform_certification] skipped — already running")
            return
        _job_state["platform_certification"] = True

    log.info("[scheduler/platform_certification] started (scheduled)")
    try:
        from services.platform_certification_service import run_and_record  # noqa: PLC0415
        result = run_and_record(run_reason="scheduled")
        log.info("[scheduler/platform_certification] %s (exit %s, recorded=%s)",
                 result["verdict"], result["exit_code"], result["recorded"])
    except Exception as exc:  # noqa: BLE001
        log.error("[scheduler/platform_certification] failed: %s", exc, exc_info=True)
    finally:
        with _run_lock:
            _job_state["platform_certification"] = False


# ---------------------------------------------------------------------------
# Lifecycle helpers — called by api/server.py lifespan handler.
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    """Start the background scheduler. Safe to call once on app startup."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        log.warning("[scheduler] start_scheduler() called but scheduler is already running")
        return

    _scheduler = BackgroundScheduler(timezone=_TZ)

    # Daily pulse — every day at 06:00 Asia/Amman.
    _scheduler.add_job(
        _run_daily_scheduled,
        trigger=CronTrigger(hour=6, minute=0, timezone=_TZ),
        id="daily",
        name="Daily Pulse",
        replace_existing=True,
        misfire_grace_time=3600,  # 1-hour grace window for process restarts.
    )

    # Monthly data refresh — 1st of every month at 08:00 Asia/Amman.
    _scheduler.add_job(
        _run_monthly_scheduled,
        trigger=CronTrigger(day=1, hour=8, minute=0, timezone=_TZ),
        id="monthly",
        name="Monthly Data Refresh",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Daily incremental sync — every day at 09:00 Asia/Amman.
    _scheduler.add_job(
        _run_daily_incremental_sync_scheduled,
        trigger=CronTrigger(hour=9, minute=0, timezone=_TZ),
        id="daily_incremental_sync",
        name="Daily Incremental Sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Daily platform certification — 10:30 Asia/Amman, after the 09:00 sync.
    _scheduler.add_job(
        _run_platform_certification_scheduled,
        trigger=CronTrigger(hour=10, minute=30, timezone=_TZ),
        id="platform_certification",
        name="Daily Platform Certification",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    log.info(
        "[scheduler] started — daily 06:00, monthly 1st 08:00, "
        "daily_incremental_sync 09:00, platform_certification 10:30 "
        "(Asia/Amman / UTC+3)"
    )


def stop_scheduler() -> None:
    """Stop the background scheduler cleanly. Safe to call on app shutdown."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("[scheduler] stopped")
    _scheduler = None


# ---------------------------------------------------------------------------
# Status helper — called by GET /scheduler/status endpoint.
# ---------------------------------------------------------------------------

def get_registered_job_ids() -> list[str]:
    """Return the IDs of all Phase 1 scheduled jobs.

    Safe to call without starting the scheduler — does not create background
    threads or trigger any job execution.  Used by the readiness check to
    verify that the in-app APScheduler model is correctly declared.
    """
    return list(JOB_IDS)


def get_scheduler_status() -> dict:
    """Return current scheduler state and next run times for every job."""
    if _scheduler is None or not _scheduler.running:
        return {
            "status": "not_running",
            "jobs": [
                {"job": job_id, "schedule": _SCHEDULE_DESCRIPTIONS[job_id], "next_run": None}
                for job_id in JOB_IDS
            ],
        }

    jobs = []
    for job_id in JOB_IDS:
        job = _scheduler.get_job(job_id)
        next_run: str | None = None
        if job and job.next_run_time:
            next_run = job.next_run_time.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        jobs.append(
            {
                "job": job_id,
                "schedule": _SCHEDULE_DESCRIPTIONS[job_id],
                "next_run": next_run,
            }
        )

    return {"status": "running", "jobs": jobs}
