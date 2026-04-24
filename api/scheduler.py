"""
api/scheduler.py

In-app APScheduler for the Logistaas Ads Intelligence System.

Phase 1 — Read Only.

Responsibility:
  - Manage in-app scheduled jobs for daily, weekly, and monthly Phase 1 runs.
  - Provide shared in-memory concurrency guards used by api/server.py manual
    run endpoints, so scheduled and manual runs cannot overlap.
  - Log job start, success, and failure.
  - NO business logic — only calls existing scheduler module functions.
  - NO Google Ads write-back. NO HubSpot write-back. NO OCT. NO negative
    keyword push.

Schedules (Asia/Amman = UTC+3, no DST since Jordan suspended DST in 2022):
  Daily pulse     — every day at 06:00 Asia/Amman  (03:00 UTC)
  Weekly report   — every Monday at 07:00 Asia/Amman (04:00 UTC)
  Monthly strategy — 1st of month at 08:00 Asia/Amman (05:00 UTC)
"""

import logging
import threading
from datetime import timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

log = logging.getLogger(__name__)

# Timezone for all scheduled jobs.
# Asia/Amman = UTC+3 year-round (Jordan suspended daylight saving time in 2022).
_TZ = pytz.timezone("Asia/Amman")

# ---------------------------------------------------------------------------
# Shared in-memory concurrency guards.
# Imported and used by api/server.py manual run endpoints so that scheduled
# runs and manual runs share the same lock and cannot overlap.
# ---------------------------------------------------------------------------

_job_state: dict[str, bool] = {"daily": False, "weekly": False, "monthly": False}
_run_lock = threading.Lock()

# ---------------------------------------------------------------------------
# APScheduler instance
# ---------------------------------------------------------------------------

_scheduler: BackgroundScheduler | None = None

# Human-readable schedule descriptions (UTC equivalents noted).
_SCHEDULE_DESCRIPTIONS: dict[str, str] = {
    "daily": "06:00 Asia/Amman (03:00 UTC)",
    "weekly": "Monday 07:00 Asia/Amman (04:00 UTC)",
    "monthly": "1st of month 08:00 Asia/Amman (05:00 UTC)",
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


def _run_weekly_scheduled() -> None:
    """Scheduled wrapper for the weekly report."""
    with _run_lock:
        if _job_state["weekly"]:
            log.warning("[scheduler/weekly] skipped — already running (manual or previous scheduled run still active)")
            return
        _job_state["weekly"] = True

    log.info("[scheduler/weekly] started (scheduled)")
    try:
        from scheduler.weekly import run_weekly_report  # noqa: PLC0415
        run_weekly_report()
        log.info("[scheduler/weekly] succeeded")
    except Exception as exc:  # noqa: BLE001
        log.error("[scheduler/weekly] failed: %s", exc, exc_info=True)
    finally:
        with _run_lock:
            _job_state["weekly"] = False


def _run_monthly_scheduled() -> None:
    """Scheduled wrapper for the monthly strategy report."""
    with _run_lock:
        if _job_state["monthly"]:
            log.warning("[scheduler/monthly] skipped — already running (manual or previous scheduled run still active)")
            return
        _job_state["monthly"] = True

    log.info("[scheduler/monthly] started (scheduled)")
    try:
        from scheduler.monthly import run_monthly_report  # noqa: PLC0415
        run_monthly_report()
        log.info("[scheduler/monthly] succeeded")
    except Exception as exc:  # noqa: BLE001
        log.error("[scheduler/monthly] failed: %s", exc, exc_info=True)
    finally:
        with _run_lock:
            _job_state["monthly"] = False


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

    # Weekly report — every Monday at 07:00 Asia/Amman.
    _scheduler.add_job(
        _run_weekly_scheduled,
        trigger=CronTrigger(day_of_week="mon", hour=7, minute=0, timezone=_TZ),
        id="weekly",
        name="Weekly Report",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Monthly strategy — 1st of every month at 08:00 Asia/Amman.
    _scheduler.add_job(
        _run_monthly_scheduled,
        trigger=CronTrigger(day=1, hour=8, minute=0, timezone=_TZ),
        id="monthly",
        name="Monthly Strategy",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    log.info(
        "[scheduler] started — daily 06:00, weekly Mon 07:00, monthly 1st 08:00 (Asia/Amman / UTC+3)"
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

def get_scheduler_status() -> dict:
    """Return current scheduler state and next run times for all three jobs."""
    if _scheduler is None or not _scheduler.running:
        return {
            "status": "not_running",
            "jobs": [
                {"job": job_id, "schedule": _SCHEDULE_DESCRIPTIONS[job_id], "next_run": None}
                for job_id in ("daily", "weekly", "monthly")
            ],
        }

    jobs = []
    for job_id in ("daily", "weekly", "monthly"):
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
