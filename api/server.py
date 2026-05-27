"""
api/server.py

FastAPI web entry point for the Logistaas Ads Intelligence System.

Phase 1 — Read Only.

Responsibility:
  - Expose health and readiness endpoints for Render Web Service.
  - Expose read-only endpoints for latest run history and reports.
  - Expose protected manual run endpoints for Phase 1 schedulers.
  - Provide internal authentication (login/logout/me) via signed cookie sessions.
  - Start the in-app APScheduler on startup and stop it on shutdown.
  - NO writes to Google Ads, HubSpot, or any external service.
  - NO business logic or analysis execution.
  - NO secrets or PII in responses.

Public endpoints:
  GET  /health              — Simple liveness check (no auth required).

Auth endpoints:
  POST /auth/login          — Login with username + password; sets session cookie.
  POST /auth/logout         — Clear session cookie.
  GET  /auth/me             — Return current user info (requires auth).

Protected endpoints (require authenticated session):
  GET  /                    — Dashboard UI (serves static/index.html).
  GET  /readiness           — Structured readiness check (requires admin).
  GET  /runs/latest         — Latest run record (requires auth).
  GET  /reports/latest      — Latest report metadata (requires auth).
  GET  /reports/latest/raw  — Raw report content (requires auth).
  GET  /scheduler/status    — Scheduler state (requires auth).
  POST /run/daily           — Trigger daily run (requires admin or ADMIN_API_TOKEN).
  POST /run/weekly          — Trigger weekly run (requires admin or ADMIN_API_TOKEN).
  POST /run/monthly         — Trigger monthly run (requires admin or ADMIN_API_TOKEN).
  POST /run/incremental-sync — Trigger daily incremental sync manually (requires admin or ADMIN_API_TOKEN).
  GET  /api/geo                    — Windsor geo performance by country/campaign (requires auth).
  GET  /api/keywords               — Windsor keyword performance by campaign/ad group/keyword (requires auth).
  GET  /api/leads/country-summary  — HubSpot lead quality aggregated by country (requires auth).
  GET  /api/campaign-detail        — Campaign drill-down detail, query-param form (requires auth). Preferred.
  GET  /api/campaigns/{campaign_name}/detail — Campaign drill-down detail, path-segment form (requires auth). Legacy.
  GET  /api/config/ui-thresholds  — UI-safe display thresholds from config/thresholds.yaml (requires auth).
  GET  /api/dashboard/trends      — Previous-period trend comparison for dashboard (requires auth).
  GET  /api/action-queue          — Ranked human-review queue based on campaign, waste, geo, keyword, and data signals (requires auth).
  GET  /api/datasets/freshness    — Per-dataset sync state / watermark from sync_state table (requires auth).
  GET  /api/search-terms          — Paginated search-term fact rows from search_terms table (requires auth).
  GET  /api/search-terms/ngrams   — Read-only n-gram analysis over stored search_terms (requires auth).
  GET  /api/gclid-attribution     — Paginated GCLID attribution rows from gclid_attribution table (requires auth).
  GET  /api/gclid-coverage        — GCLID coverage snapshots from gclid_coverage_snapshots table (requires auth).
  GET  /api/monitoring/status     — Read-only monitoring summary: stale/failure state per run type (requires auth).
  POST /api/backfill/run          — Trigger admin-only historical backfill (requires admin or ADMIN_API_TOKEN).
  GET  /api/backfill/status       — Latest backfill run state and summary (requires auth).
  GET  /api/historical-intelligence — Read-only historical trend analysis over local data (requires auth).
"""

import base64
import hashlib
import importlib
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.auth import (
    check_admin_or_token,
    clear_session,
    authenticate_user,
    get_current_user,
    require_auth,
    set_session,
)
from api.scheduler import (
    _job_state,
    _run_lock,
    get_scheduler_status,
    start_scheduler,
    stop_scheduler,
)
from api.monitoring import (
    compute_monitoring_status as _compute_monitoring_status,
    STALE_DAYS_DEFAULT as _MONITORING_STALE_DAYS_DEFAULT,
    CONSECUTIVE_FAILURE_WARNING_DEFAULT as _MONITORING_CONSECUTIVE_FAILURE_WARNING_DEFAULT,
)

log = logging.getLogger(__name__)

# Read APP_ENV for context (e.g. "development" vs "production").
# Not currently used in routing logic but available for future conditional behaviour.
APP_ENV = os.getenv("APP_ENV", "production")

# ---------------------------------------------------------------------------
# Lifespan handler — starts and stops the in-app APScheduler.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start the in-app scheduler on startup; stop it cleanly on shutdown."""
    from db.connection import init_pool
    from db.schema import init_db
    init_pool()
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="Logistaas Ads Intelligence",
    description="Phase 1 read-only API — health, readiness, report, and scheduler endpoints.",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Static assets — served at /static/
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# Path constants — relative to the repo root (CWD when uvicorn starts).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(os.getcwd())
_RUN_HISTORY_FILE = _REPO_ROOT / "runtime_logs" / "run_history.jsonl"
_OUTPUTS_DIR = _REPO_ROOT / "outputs"
_DATA_DIR = _REPO_ROOT / "data"
_CONFIG_THRESHOLDS = _REPO_ROOT / "config" / "thresholds.yaml"
_CONFIG_JUNK = _REPO_ROOT / "config" / "junk_patterns.yaml"
_DOCTRINE_DOC = _REPO_ROOT / "docs" / "DOCTRINE.md"

# Core modules that must be importable for the service to be considered ready.
_REQUIRED_MODULES = [
    "analysis.core",
    "analysis.advisor",
    "analysis.rule_advisor",
    "api.auth",
    "scheduler.daily",
    "scheduler.weekly",
    "scheduler.monthly",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _latest_report_path() -> Path | None:
    """Return the path of the most recently modified report file in outputs/."""
    if not _OUTPUTS_DIR.is_dir():
        return None
    candidates = sorted(
        _OUTPUTS_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # Also include .json reports so daily pulse files appear.
    if not candidates:
        candidates = sorted(
            _OUTPUTS_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Login request schema
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


# ---------------------------------------------------------------------------
# Backfill request schema
# ---------------------------------------------------------------------------

class BackfillRunRequest(BaseModel):
    source: str = "all"
    date_from: str
    date_to: str
    chunk: str = "monthly"
    dry_run: bool = True
    max_chunks: Optional[int] = None


# ---------------------------------------------------------------------------
# In-memory backfill run state.
# _backfill_lock guards _backfill_state against concurrent access.
# _backfill_state["running"] prevents duplicate concurrent runs (double-run
# protection).  _backfill_state["latest"] stores the most recent run result
# for the GET /api/backfill/status endpoint.
#
# Process-local guard only. This is sufficient for the current single-worker
# Render deployment. If the service moves to multiple workers/instances,
# replace with a DB-backed advisory lock.
# ---------------------------------------------------------------------------

_backfill_lock: threading.Lock = threading.Lock()
_backfill_state: dict[str, Any] = {"running": False, "latest": None}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    """Serve the main dashboard page. Auth state is handled client-side."""
    html_file = _STATIC_DIR / "index.html"
    if not html_file.is_file():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return html_file.read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check — always returns ok when the process is running. No auth required."""
    return {"status": "ok", "service": "logistaas-ads-intelligence"}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/auth/login")
def auth_login(body: LoginRequest, response: Response) -> dict[str, Any]:
    """
    Authenticate with username and password.
    Sets an HTTP-only signed session cookie on success.
    Returns 401 for invalid credentials.
    """
    user = authenticate_user(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    role = user.get("role", "viewer")
    set_session(response, user["username"], role)
    return {"username": user["username"], "role": role}


@app.post("/auth/logout")
def auth_logout(response: Response) -> dict[str, str]:
    """Clear the session cookie."""
    clear_session(response)
    return {"status": "ok"}


@app.get("/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    """Return the current authenticated user's username and role. Requires auth."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": user["username"], "role": user["role"]}


# ---------------------------------------------------------------------------
# Protected read-only endpoints
# ---------------------------------------------------------------------------

@app.get("/readiness")
def readiness(request: Request) -> dict[str, Any]:
    """
    Structured readiness check. Requires admin role.

    Verifies required directories, config files, docs, and core module
    imports.  Does NOT call any external API.
    """
    # Require admin — readiness exposes system configuration state
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    checks: dict[str, Any] = {}

    # Required directories — must exist or be creatable.
    dir_results: dict[str, bool] = {}
    for label, path in [("data/", _DATA_DIR), ("outputs/", _OUTPUTS_DIR)]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            dir_results[label] = True
        except OSError:
            dir_results[label] = False
    checks["directories"] = dir_results

    # Required config files.
    config_results: dict[str, bool] = {}
    for label, path in [
        ("config/thresholds.yaml", _CONFIG_THRESHOLDS),
        ("config/junk_patterns.yaml", _CONFIG_JUNK),
    ]:
        config_results[label] = path.is_file()
    checks["config_files"] = config_results

    # Required docs.
    doc_results: dict[str, bool] = {}
    for label, path in [("docs/DOCTRINE.md", _DOCTRINE_DOC)]:
        doc_results[label] = path.is_file()
    checks["docs"] = doc_results

    # Core module imports.
    import_results: dict[str, bool] = {}
    for module_name in _REQUIRED_MODULES:
        try:
            importlib.import_module(module_name)
            import_results[module_name] = True
        except Exception:  # noqa: BLE001
            import_results[module_name] = False
    checks["imports"] = import_results

    # Overall pass/fail.
    all_passed = all(
        v
        for section in checks.values()
        for v in (section.values() if isinstance(section, dict) else [section])
    )
    return {"status": "pass" if all_passed else "fail", "checks": checks}


@app.get("/runs/latest")
def runs_latest(user: dict = Depends(require_auth)) -> dict[str, Any]:
    """Return the most recent record from runtime_logs/run_history.jsonl. Requires auth."""
    if not _RUN_HISTORY_FILE.is_file():
        return {"status": "empty", "message": "No run history found yet"}

    last_line: str | None = None
    try:
        with _RUN_HISTORY_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last_line = line
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read run history: {exc}") from exc

    if last_line is None:
        return {"status": "empty", "message": "No run history found yet"}

    try:
        return json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Malformed run history record: {exc}") from exc


@app.get("/reports/latest")
def reports_latest(user: dict = Depends(require_auth)) -> dict[str, Any]:
    """Return metadata for the latest report file in outputs/. Requires auth."""
    report_path = _latest_report_path()

    if report_path is None:
        return {
            "report_type": None,
            "filename": None,
            "generated_at": None,
            "path": None,
            "exists": False,
        }

    # Derive report_type from filename prefix (e.g. "weekly_2026-04-24.md" → "weekly").
    stem = report_path.stem
    report_type = stem.split("_")[0] if "_" in stem else stem

    # generated_at: use file modification time as a best-effort timestamp.
    try:
        mtime = report_path.stat().st_mtime
        generated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except OSError:
        generated_at = None

    return {
        "report_type": report_type,
        "filename": report_path.name,
        "generated_at": generated_at,
        "path": str(report_path.relative_to(_REPO_ROOT)),
        "exists": True,
    }


@app.get("/reports/latest/raw", response_class=PlainTextResponse)
def reports_latest_raw(user: dict = Depends(require_auth)) -> str:
    """Return the raw content of the latest markdown report as text/plain. Requires auth."""
    report_path = _latest_report_path()

    if report_path is None or report_path.suffix != ".md":
        raise HTTPException(status_code=404, detail="No markdown report found")

    try:
        return report_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read report: {exc}") from exc


# ---------------------------------------------------------------------------
# Scheduler status endpoint — read-only, requires auth.
# ---------------------------------------------------------------------------

@app.get("/scheduler/status")
def scheduler_status(user: dict = Depends(require_auth)) -> dict[str, Any]:
    """Return the in-app scheduler state and next run times for all jobs. Requires auth."""
    return get_scheduler_status()


# ---------------------------------------------------------------------------
# Protected manual run endpoints — require admin role or ADMIN_API_TOKEN.
# ---------------------------------------------------------------------------

@app.post("/run/daily")
def run_daily(request: Request) -> dict[str, Any]:
    """Trigger the daily pulse scheduler. Requires admin session or ADMIN_API_TOKEN."""
    check_admin_or_token(request)

    with _run_lock:
        if _job_state["daily"]:
            raise HTTPException(status_code=409, detail="job already running")
        _job_state["daily"] = True

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("[run/daily] started at %s", started_at)

    try:
        from scheduler.daily import run_daily_pulse  # noqa: PLC0415
        result = run_daily_pulse()
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report_path = result.get(
            "report_path",
            f"outputs/daily_{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}.json",
        ) if isinstance(result, dict) else f"outputs/daily_{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}.json"
        log.info("[run/daily] succeeded, finished at %s", finished_at)
        return {
            "status": "success",
            "job": "daily",
            "started_at": started_at,
            "finished_at": finished_at,
            "result": {"report_path": report_path},
        }
    except Exception as exc:  # noqa: BLE001
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.error("[run/daily] failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "job": "daily",
            "started_at": started_at,
            "finished_at": finished_at,
            "error": f"{type(exc).__name__}: scheduler execution failed",
        }
    finally:
        with _run_lock:
            _job_state["daily"] = False


@app.post("/run/weekly")
def run_weekly(request: Request) -> dict[str, Any]:
    """Trigger the weekly report scheduler. Requires admin session or ADMIN_API_TOKEN."""
    check_admin_or_token(request)

    with _run_lock:
        if _job_state["weekly"]:
            raise HTTPException(status_code=409, detail="job already running")
        _job_state["weekly"] = True

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("[run/weekly] started at %s", started_at)

    try:
        from scheduler.weekly import run_weekly_report  # noqa: PLC0415
        report_path = run_weekly_report()
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("[run/weekly] succeeded, finished at %s", finished_at)
        return {
            "status": "success",
            "job": "weekly",
            "started_at": started_at,
            "finished_at": finished_at,
            "result": {"report_path": str(report_path) if report_path else None},
        }
    except Exception as exc:  # noqa: BLE001
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.error("[run/weekly] failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "job": "weekly",
            "started_at": started_at,
            "finished_at": finished_at,
            "error": f"{type(exc).__name__}: scheduler execution failed",
        }
    finally:
        with _run_lock:
            _job_state["weekly"] = False


@app.post("/run/monthly")
def run_monthly(request: Request) -> dict[str, Any]:
    """Trigger the monthly report scheduler. Requires admin session or ADMIN_API_TOKEN."""
    check_admin_or_token(request)

    with _run_lock:
        if _job_state["monthly"]:
            raise HTTPException(status_code=409, detail="job already running")
        _job_state["monthly"] = True

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("[run/monthly] started at %s", started_at)

    try:
        from scheduler.monthly import run_monthly_report  # noqa: PLC0415
        report_path = run_monthly_report()
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("[run/monthly] succeeded, finished at %s", finished_at)
        return {
            "status": "success",
            "job": "monthly",
            "started_at": started_at,
            "finished_at": finished_at,
            "result": {"report_path": str(report_path) if report_path else None},
        }
    except Exception as exc:  # noqa: BLE001
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.error("[run/monthly] failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "job": "monthly",
            "started_at": started_at,
            "finished_at": finished_at,
            "error": f"{type(exc).__name__}: scheduler execution failed",
        }
    finally:
        with _run_lock:
            _job_state["monthly"] = False


@app.post("/run/incremental-sync")
def run_incremental_sync(request: Request) -> dict[str, Any]:
    """Trigger the daily incremental sync manually. Requires admin session or ADMIN_API_TOKEN.

    Reads recent data from Windsor.ai and HubSpot and writes only to the
    local database.  Does not modify Google Ads, HubSpot, campaigns, bids,
    budgets, contacts, deals, or negative keywords.
    """
    check_admin_or_token(request)

    with _run_lock:
        if _job_state["daily_incremental_sync"]:
            raise HTTPException(status_code=409, detail="job already running")
        _job_state["daily_incremental_sync"] = True

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("[run/incremental-sync] started at %s", started_at)

    try:
        from scheduler.incremental_sync import run_daily_incremental_sync  # noqa: PLC0415
        result = run_daily_incremental_sync(run_reason="manual")
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("[run/incremental-sync] succeeded, finished at %s", finished_at)
        # Strip per-dataset error strings before returning to client; admin can
        # inspect detailed errors via logs.  Status fields are safe to surface.
        safe_datasets = {
            k: {ek: ev for ek, ev in v.items() if ek != "error"}
            for k, v in result.get("datasets", {}).items()
        }
        result_status = result.get("status") or "unknown"
        return {
            "status": result_status,
            "job": "daily_incremental_sync",
            "started_at": started_at,
            "finished_at": finished_at,
            "result": {
                "status": result_status,
                "run_type": result.get("run_type"),
                "lookback": result.get("lookback"),
                "datasets": safe_datasets,
                "error_count": len(result.get("errors", [])),
            },
        }
    except Exception as exc:  # noqa: BLE001
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.error("[run/incremental-sync] failed: %s", exc, exc_info=True)
        return {
            "status": "failed",
            "job": "daily_incremental_sync",
            "started_at": started_at,
            "finished_at": finished_at,
            "error": f"{type(exc).__name__}: scheduler execution failed",
        }
    finally:
        with _run_lock:
            _job_state["daily_incremental_sync"] = False


# ---------------------------------------------------------------------------
# Time-range data endpoints — require auth, accept ?days= parameter.
# All database queries are non-fatal: returns db_unavailable flag when down.
# ---------------------------------------------------------------------------

def _clamp_days(days: int) -> int:
    """Clamp days to the range [1, 365]."""
    return max(1, min(365, days))


def _db_empty_response(days: int, key: str) -> dict[str, Any]:
    """Return a structured empty response when the database is unavailable."""
    return {"days": days, key: [], "db_unavailable": True}


@app.get("/api/campaigns")
def api_campaigns(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return aggregated campaign metrics for the last N days. Requires auth."""
    days = _clamp_days(days)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty_response(days, "campaigns")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH date_filtered AS (
                        SELECT
                            LOWER(campaign_name) AS campaign_name,
                            verdict,
                            spend_usd,
                            confirmed_sqls,
                            junk_rate_pct,
                            cpql_usd,
                            total_leads,
                            run_date,
                            created_at,
                            id
                        FROM campaigns
                        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                    ),
                    latest_verdicts AS (
                        SELECT DISTINCT ON (campaign_name)
                            campaign_name,
                            verdict AS latest_verdict
                        FROM date_filtered
                        ORDER BY campaign_name, run_date DESC, created_at DESC, id DESC
                    ),
                    latest_leads AS (
                        SELECT DISTINCT ON (campaign_name)
                            campaign_name,
                            total_leads
                        FROM date_filtered
                        ORDER BY campaign_name, run_date DESC, id DESC
                    )
                    SELECT
                        agg.campaign_name,
                        lv.latest_verdict,
                        AVG(agg.spend_usd)            AS avg_spend_usd,
                        SUM(agg.confirmed_sqls)       AS total_confirmed_sqls,
                        AVG(agg.junk_rate_pct)        AS avg_junk_rate_pct,
                        AVG(agg.cpql_usd)             AS avg_cpql_usd,
                        COUNT(*)                      AS run_count,
                        COALESCE(MAX(ll.total_leads), 0) AS total_leads
                    FROM date_filtered agg
                    JOIN latest_verdicts lv ON lv.campaign_name = agg.campaign_name
                    LEFT JOIN latest_leads ll ON ll.campaign_name = agg.campaign_name
                    GROUP BY agg.campaign_name, lv.latest_verdict
                    ORDER BY avg_spend_usd DESC NULLS LAST
                    """,
                    (days,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                campaigns = []
                for row in rows:
                    r = dict(zip(cols, row))
                    campaigns.append({
                        "campaign_name": r["campaign_name"],
                        "latest_verdict": r["latest_verdict"],
                        "avg_spend_usd": round(float(r["avg_spend_usd"]), 2) if r["avg_spend_usd"] is not None else None,
                        "total_confirmed_sqls": int(r["total_confirmed_sqls"] or 0),
                        "avg_junk_rate_pct": round(float(r["avg_junk_rate_pct"]), 2) if r["avg_junk_rate_pct"] is not None else None,
                        "avg_cpql_usd": round(float(r["avg_cpql_usd"]), 2) if r["avg_cpql_usd"] is not None else None,
                        "run_count": int(r["run_count"]),
                        "total_leads": int(r["total_leads"]) if r["total_leads"] is not None else 0,
                        # TODO: Replace hardcoded "stable" with junk rate trend calculation
                        # once 4+ weekly runs exist. Pattern: compare avg junk_rate of
                        # older half vs newer half of the date window.
                        # Tracked: PR-ADS-025B or standalone cleanup PR.
                        "trend": "stable",
                    })
    except Exception as exc:  # noqa: BLE001
        log.error("[api/campaigns] database error: %s", exc, exc_info=True)
        return _db_empty_response(days, "campaigns")

    return {
        "days": days,
        "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "campaigns": campaigns,
    }


@app.get("/api/leads")
def api_leads(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return lead rows for the last N days. Requires auth."""
    days = _clamp_days(days)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty_response(days, "leads")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT contact_id, company, campaign_name, keyword, country,
                           mql_status, status_category, gclid, source_type, run_date
                    FROM leads
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                    ORDER BY run_date DESC, id DESC
                    LIMIT 1000
                    """,
                    (days,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                leads = [dict(zip(cols, row)) for row in rows]
                for lead in leads:
                    if lead.get("run_date"):
                        lead["run_date"] = str(lead["run_date"])
    except Exception as exc:  # noqa: BLE001
        log.error("[api/leads] database error: %s", exc, exc_info=True)
        return _db_empty_response(days, "leads")

    return {"days": days, "leads": leads}


@app.get("/api/deals")
def api_deals(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return deal rows for the last N days. Requires auth."""
    days = _clamp_days(days)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty_response(days, "deals")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT contact_id, company, country, keyword, campaign_name,
                           deal_stage, deal_stage_label, deal_amount_usd,
                           mql_status, gclid, run_date
                    FROM deals
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                    ORDER BY run_date DESC, id DESC
                    LIMIT 1000
                    """,
                    (days,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                deals_out = [dict(zip(cols, row)) for row in rows]
                for deal in deals_out:
                    if deal.get("run_date"):
                        deal["run_date"] = str(deal["run_date"])
                    if deal.get("deal_amount_usd") is not None:
                        deal["deal_amount_usd"] = float(deal["deal_amount_usd"])
    except Exception as exc:  # noqa: BLE001
        log.error("[api/deals] database error: %s", exc, exc_info=True)
        return _db_empty_response(days, "deals")

    return {"days": days, "deals": deals_out}


@app.get("/api/waste")
def api_waste(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return waste term rows for the last N days. Requires auth."""
    days = _clamp_days(days)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty_response(days, "waste")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT search_term, campaign_name, spend_usd,
                           junk_category, matched_pattern, crm_junk_confirmed, run_date
                    FROM waste_terms
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                    ORDER BY spend_usd DESC NULLS LAST, run_date DESC
                    LIMIT 500
                    """,
                    (days,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                waste_out = [dict(zip(cols, row)) for row in rows]
                for item in waste_out:
                    if item.get("run_date"):
                        item["run_date"] = str(item["run_date"])
                    if item.get("spend_usd") is not None:
                        item["spend_usd"] = float(item["spend_usd"])
    except Exception as exc:  # noqa: BLE001
        log.error("[api/waste] database error: %s", exc, exc_info=True)
        return _db_empty_response(days, "waste")

    return {"days": days, "waste": waste_out}


@app.get("/api/runs")
def api_runs(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return scheduler run records for the last N days. Requires auth."""
    days = _clamp_days(days)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty_response(days, "runs")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_type, started_at, finished_at, status, report_path
                    FROM runs
                    WHERE started_at >= NOW() - INTERVAL '1 day' * %s
                    ORDER BY started_at DESC
                    """,
                    (days,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                runs_out = []
                for row in rows:
                    r = dict(zip(cols, row))
                    r["started_at"] = (
                        r["started_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        if r.get("started_at") else None
                    )
                    r["finished_at"] = (
                        r["finished_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        if r.get("finished_at") else None
                    )
                    runs_out.append(r)
    except Exception as exc:  # noqa: BLE001
        log.error("[api/runs] database error: %s", exc, exc_info=True)
        return _db_empty_response(days, "runs")

    return {"days": days, "runs": runs_out}


@app.get("/api/summary")
def api_summary(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return aggregated summary metrics for the last N days. Requires auth."""
    days = _clamp_days(days)

    _empty = {
        "days": days,
        "total_spend_usd": None,
        "confirmed_sqls": None,
        "avg_cpql_usd": None,
        "confirmed_waste_usd": None,
        "total_leads": None,
        "junk_rate_pct": None,
        "run_count": 0,
        "last_run_at": None,
        "last_run_status": None,
        "db_unavailable": True,
    }

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _empty

            with conn.cursor() as cur:
                # Campaign aggregates — join on MAX(run_id) from the runs table to
                # guarantee exactly one run's data, even if multiple runs fired on
                # the same calendar day (run_date is a DATE — not a timestamp).
                cur.execute(
                    """
                    WITH latest_run AS (
                        SELECT MAX(id) AS max_run_id
                        FROM runs
                        WHERE started_at >= NOW() - INTERVAL '1 day' * %s
                    )
                    SELECT
                        SUM(c.spend_usd)        AS total_spend_usd,
                        SUM(c.confirmed_sqls)   AS confirmed_sqls,
                        SUM(c.total_leads)      AS total_leads,
                        SUM(c.junk_count)       AS total_junk
                    FROM campaigns c
                    JOIN latest_run lr ON c.run_id = lr.max_run_id
                    """,
                    (days,),
                )
                c_row = cur.fetchone()

                # Waste aggregates
                cur.execute(
                    """
                    SELECT COALESCE(SUM(spend_usd), 0)
                    FROM waste_terms
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                      AND crm_junk_confirmed > 0
                    """,
                    (days,),
                )
                waste_row = cur.fetchone()

                # Run count + last run
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS run_count,
                        MAX(started_at) AS last_run_at,
                        (
                            SELECT status
                            FROM runs latest
                            WHERE latest.started_at >= NOW() - INTERVAL '1 day' * %s
                            ORDER BY latest.started_at DESC
                            LIMIT 1
                        ) AS last_run_status
                    FROM runs
                    WHERE started_at >= NOW() - INTERVAL '1 day' * %s
                    """,
                    (days, days),
                )
                r_row = cur.fetchone()

            total_spend = float(c_row[0]) if c_row and c_row[0] is not None else None
            confirmed_sqls = int(c_row[1]) if c_row and c_row[1] is not None else None
            total_leads = int(c_row[2]) if c_row and c_row[2] is not None else None
            total_junk = int(c_row[3]) if c_row and c_row[3] is not None else None
            confirmed_waste = float(waste_row[0]) if waste_row and waste_row[0] is not None else 0.0
            run_count = int(r_row[0]) if r_row else 0
            last_run_at = (
                r_row[1].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if r_row and r_row[1] else None
            )
            last_run_status = r_row[2] if r_row else None

            avg_cpql = None
            if total_spend is not None and confirmed_sqls:
                avg_cpql = round(total_spend / confirmed_sqls, 2)

            junk_rate = None
            if total_leads and total_junk is not None:
                junk_rate = round((total_junk / total_leads) * 100, 1) if total_leads > 0 else 0.0

    except Exception as exc:  # noqa: BLE001
        log.error("[api/summary] database error: %s", exc, exc_info=True)
        return _empty

    return {
        "days": days,
        "total_spend_usd": round(total_spend, 2) if total_spend is not None else None,
        "confirmed_sqls": confirmed_sqls,
        "avg_cpql_usd": avg_cpql,
        "confirmed_waste_usd": round(confirmed_waste, 2),
        "total_leads": total_leads,
        "junk_rate_pct": junk_rate,
        "run_count": run_count,
        "last_run_at": last_run_at,
        "last_run_status": last_run_status,
    }


@app.get("/api/geo")
def api_geo(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return aggregated Windsor geo performance by country/campaign for the last N days. Requires auth."""
    days = _clamp_days(days)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty_response(days, "rows")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        country,
                        campaign_name,
                        SUM(spend_usd)        AS spend_usd,
                        SUM(clicks)           AS clicks,
                        SUM(impressions)      AS impressions,
                        SUM(conversions)      AS conversions,
                        COUNT(DISTINCT run_id) AS runs,
                        MAX(run_date)         AS last_run_date
                    FROM geo
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                    GROUP BY country, campaign_name
                    ORDER BY spend_usd DESC
                    """,
                    (days,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                geo_out = []
                for row in rows:
                    r = dict(zip(cols, row))
                    geo_out.append({
                        "country": r["country"],
                        "campaign_name": r["campaign_name"],
                        "spend_usd": round(float(r["spend_usd"]), 2) if r["spend_usd"] is not None else 0.0,
                        "clicks": int(r["clicks"] or 0),
                        "impressions": int(r["impressions"] or 0),
                        "conversions": round(float(r["conversions"]), 2) if r["conversions"] is not None else 0.0,
                        "runs": int(r["runs"] or 0),
                        "last_run_date": str(r["last_run_date"]) if r["last_run_date"] else None,
                    })
    except Exception as exc:  # noqa: BLE001
        log.error("[api/geo] database error: %s", exc, exc_info=True)
        return _db_empty_response(days, "rows")

    return {"days": days, "rows": geo_out}


@app.get("/api/keywords")
def api_keywords(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return aggregated Windsor keyword performance for the last N days. Requires auth."""
    days = _clamp_days(days)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty_response(days, "rows")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        campaign_name,
                        ad_group,
                        keyword,
                        match_type,
                        AVG(quality_score)    AS quality_score,
                        SUM(spend_usd)        AS spend_usd,
                        SUM(clicks)           AS clicks,
                        SUM(impressions)      AS impressions,
                        SUM(conversions)      AS conversions,
                        CASE
                            WHEN SUM(clicks) > 0 THEN SUM(spend_usd) / SUM(clicks)
                            ELSE 0
                        END                   AS cpc_usd,
                        COUNT(DISTINCT run_id) AS runs,
                        MAX(run_date)         AS last_run_date
                    FROM keywords
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                    GROUP BY campaign_name, ad_group, keyword, match_type
                    ORDER BY spend_usd DESC
                    """,
                    (days,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                kw_out = []
                for row in rows:
                    r = dict(zip(cols, row))
                    kw_out.append({
                        "campaign_name": r["campaign_name"],
                        "ad_group": r["ad_group"],
                        "keyword": r["keyword"],
                        "match_type": r["match_type"],
                        "quality_score": round(float(r["quality_score"]), 2) if r["quality_score"] is not None else None,
                        "spend_usd": round(float(r["spend_usd"]), 2) if r["spend_usd"] is not None else 0.0,
                        "clicks": int(r["clicks"] or 0),
                        "impressions": int(r["impressions"] or 0),
                        "conversions": round(float(r["conversions"]), 2) if r["conversions"] is not None else 0.0,
                        "cpc_usd": round(float(r["cpc_usd"]), 2) if r["cpc_usd"] is not None else 0.0,
                        "runs": int(r["runs"] or 0),
                        "last_run_date": str(r["last_run_date"]) if r["last_run_date"] else None,
                    })
    except Exception as exc:  # noqa: BLE001
        log.error("[api/keywords] database error: %s", exc, exc_info=True)
        return _db_empty_response(days, "rows")

    return {"days": days, "rows": kw_out}


@app.get("/api/leads/country-summary")
def api_leads_country_summary(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return HubSpot lead quality aggregated by country for the last N days. Requires auth."""
    days = _clamp_days(days)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty_response(days, "rows")
            with conn.cursor() as cur:
                # Deduplicate leads by contact_id (latest run per contact),
                # then aggregate status counts per country.
                cur.execute(
                    """
                    WITH deduped AS (
                        SELECT DISTINCT ON (
                            CASE
                                WHEN contact_id IS NOT NULL AND contact_id <> ''
                                THEN contact_id
                                ELSE CAST(id AS TEXT)
                            END
                        )
                            country,
                            campaign_name,
                            keyword,
                            status_category,
                            run_date
                        FROM leads
                        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                        ORDER BY
                            CASE
                                WHEN contact_id IS NOT NULL AND contact_id <> ''
                                THEN contact_id
                                ELSE CAST(id AS TEXT)
                            END,
                            run_date DESC,
                            id DESC
                    )
                    SELECT
                        COALESCE(NULLIF(BTRIM(country), ''), '(unknown)') AS country,
                        COUNT(*)                                     AS total_leads,
                        SUM(CASE WHEN status_category = 'qualified'   THEN 1 ELSE 0 END) AS confirmed_sqls,
                        SUM(CASE WHEN status_category = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                        SUM(CASE WHEN status_category = 'junk'        THEN 1 ELSE 0 END) AS confirmed_junk,
                        SUM(CASE WHEN status_category = 'wrong_fit'   THEN 1 ELSE 0 END) AS wrong_fit,
                        SUM(CASE WHEN status_category = 'unknown'     THEN 1 ELSE 0 END) AS unknown,
                        mode() WITHIN GROUP (ORDER BY campaign_name)  AS top_campaign,
                        mode() WITHIN GROUP (ORDER BY keyword)        AS top_keyword,
                        MAX(run_date)                                AS last_run_date
                    FROM deduped
                    GROUP BY COALESCE(NULLIF(BTRIM(country), ''), '(unknown)')
                    ORDER BY total_leads DESC
                    """,
                    (days,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                summary_out = []
                for row in rows:
                    r = dict(zip(cols, row))
                    confirmed_junk = int(r["confirmed_junk"] or 0)
                    qualified      = int(r["confirmed_sqls"] or 0)
                    in_progress    = int(r["in_progress"] or 0)
                    wrong_fit      = int(r["wrong_fit"] or 0)
                    verdicted      = qualified + in_progress + confirmed_junk + wrong_fit
                    junk_rate      = None
                    if verdicted > 0:
                        junk_rate = round((confirmed_junk / verdicted) * 100, 2)
                    summary_out.append({
                        "country":         r["country"],
                        "total_leads":     int(r["total_leads"] or 0),
                        "confirmed_sqls":  qualified,
                        "in_progress":     in_progress,
                        "confirmed_junk":  confirmed_junk,
                        "wrong_fit":       wrong_fit,
                        "unknown":         int(r["unknown"] or 0),
                        "verdicted_leads": verdicted,
                        "junk_rate_pct":   junk_rate,
                        "top_campaign":    r["top_campaign"],
                        "top_keyword":     r["top_keyword"],
                        "last_run_date":   str(r["last_run_date"]) if r["last_run_date"] else None,
                    })
    except Exception as exc:  # noqa: BLE001
        log.error("[api/leads/country-summary] database error: %s", exc, exc_info=True)
        return _db_empty_response(days, "rows")

    return {"days": days, "rows": summary_out}


# ── Campaign detail — shared builder ───────────────────────────────────────────

def _build_campaign_detail(campaign_name: str, days: int) -> dict:
    """Assemble full campaign investigation payload for a given campaign.

    Campaign names are already stored lowercase; normalize in Python before
    querying so that direct equality (campaign_name = %s) can use existing
    indexes instead of full-table LOWER() scans.

    Does NOT bail out when the campaigns table has no snapshot in the window —
    lead, keyword, and waste evidence is still returned even when the campaign
    summary is absent (e.g. a daily-pulse window that only wrote leads).

    Returns a safe shape with db_unavailable=True when the database is down.
    Phase 1 read-only — no writes to Google Ads or HubSpot.
    """
    # Normalize to lowercase/strip to match canonical stored values
    # (db/writers.py normalizes to lower+strip on every write).
    name_key = campaign_name.strip().lower()

    _empty: dict = {
        "days":          days,
        "campaign_name": campaign_name,
        "campaign":      None,
        "lead_quality":  None,
        "countries":     [],
        "keywords":      [],
        "waste_terms":   [],
        "recent_leads":  [],
    }
    _db_empty = {**_empty, "db_unavailable": True}

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _db_empty

            with conn.cursor() as cur:
                # ── Campaign summary — latest snapshot in window ──────────────
                # Direct equality on campaign_name; index on campaigns(campaign_name) is usable.
                cur.execute(
                    """
                    WITH date_filtered AS (
                        SELECT
                            id, run_id, run_date, campaign_name,
                            spend_usd, clicks, impressions, conversions,
                            total_leads, confirmed_sqls, junk_count,
                            junk_rate_pct, cpql_usd, verdict, verdict_reason,
                            created_at
                        FROM campaigns
                        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                          AND campaign_name = %s
                    ),
                    run_stats AS (
                        SELECT
                            COUNT(DISTINCT run_id) AS runs,
                            MAX(run_date)          AS last_run_date
                        FROM date_filtered
                    )
                    SELECT DISTINCT ON (df.campaign_name)
                        df.campaign_name,
                        df.spend_usd,
                        df.clicks,
                        df.impressions,
                        df.conversions,
                        df.total_leads,
                        df.confirmed_sqls,
                        df.junk_count,
                        df.junk_rate_pct,
                        df.cpql_usd,
                        df.verdict,
                        df.verdict_reason,
                        rs.runs,
                        rs.last_run_date
                    FROM date_filtered df
                    CROSS JOIN run_stats rs
                    ORDER BY df.campaign_name, df.run_date DESC, df.created_at DESC, df.id DESC
                    """,
                    (days, name_key),
                )
                camp_row = cur.fetchone()

                campaign_out = None
                if camp_row is not None:
                    camp_cols = [d[0] for d in cur.description]
                    camp_dict = dict(zip(camp_cols, camp_row))
                    campaign_out = {
                        "campaign_name":  camp_dict["campaign_name"],
                        "spend_usd":      float(camp_dict["spend_usd"])      if camp_dict["spend_usd"]      is not None else None,
                        "clicks":         int(camp_dict["clicks"])            if camp_dict["clicks"]          is not None else None,
                        "impressions":    int(camp_dict["impressions"])       if camp_dict["impressions"]     is not None else None,
                        "conversions":    float(camp_dict["conversions"])     if camp_dict["conversions"]     is not None else None,
                        "total_leads":    int(camp_dict["total_leads"])       if camp_dict["total_leads"]     is not None else 0,
                        "confirmed_sqls": int(camp_dict["confirmed_sqls"])    if camp_dict["confirmed_sqls"]  is not None else 0,
                        "junk_count":     int(camp_dict["junk_count"])        if camp_dict["junk_count"]      is not None else 0,
                        "junk_rate_pct":  float(camp_dict["junk_rate_pct"])   if camp_dict["junk_rate_pct"]   is not None else None,
                        "cpql_usd":       float(camp_dict["cpql_usd"])        if camp_dict["cpql_usd"]        is not None else None,
                        "verdict":        camp_dict["verdict"],
                        "verdict_reason": camp_dict["verdict_reason"],
                        "runs":           int(camp_dict["runs"])              if camp_dict["runs"]            is not None else 0,
                        "last_run_date":  str(camp_dict["last_run_date"])     if camp_dict["last_run_date"]   else None,
                    }
                # Note: do NOT return early here. Even when campaign_out is None
                # (no snapshot in this window, e.g. a daily-pulse-only window),
                # we continue to query and return leads, keyword, and waste evidence.

                # ── Lead quality — deduped by contact_id, campaign-scoped ─────
                # Direct equality on campaign_name (already normalized).
                cur.execute(
                    """
                    WITH deduped AS (
                        SELECT DISTINCT ON (
                            CASE
                                WHEN contact_id IS NOT NULL AND contact_id <> ''
                                THEN contact_id
                                ELSE CAST(id AS TEXT)
                            END
                        )
                            status_category
                        FROM leads
                        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                          AND campaign_name = %s
                        ORDER BY
                            CASE
                                WHEN contact_id IS NOT NULL AND contact_id <> ''
                                THEN contact_id
                                ELSE CAST(id AS TEXT)
                            END,
                            run_date DESC,
                            id DESC
                    )
                    SELECT
                        COUNT(*)                                                    AS total_leads,
                        SUM(CASE WHEN status_category = 'qualified'   THEN 1 ELSE 0 END) AS confirmed_sqls,
                        SUM(CASE WHEN status_category = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                        SUM(CASE WHEN status_category = 'junk'        THEN 1 ELSE 0 END) AS confirmed_junk,
                        SUM(CASE WHEN status_category = 'wrong_fit'   THEN 1 ELSE 0 END) AS wrong_fit,
                        SUM(CASE WHEN status_category = 'unknown'     THEN 1 ELSE 0 END) AS unknown
                    FROM deduped
                    """,
                    (days, name_key),
                )
                lq_row = cur.fetchone()
                lq_cols = [d[0] for d in cur.description]
                lq = dict(zip(lq_cols, lq_row)) if lq_row else None

                lead_quality_out = None
                if lq and lq.get("total_leads"):
                    confirmed_sqls = int(lq["confirmed_sqls"] or 0)
                    in_progress    = int(lq["in_progress"]    or 0)
                    confirmed_junk = int(lq["confirmed_junk"] or 0)
                    wrong_fit      = int(lq["wrong_fit"]      or 0)
                    unknown        = int(lq["unknown"]        or 0)
                    verdicted      = confirmed_sqls + in_progress + confirmed_junk + wrong_fit
                    junk_rate      = None
                    if verdicted > 0:
                        junk_rate = round((confirmed_junk / verdicted) * 100, 2)
                    lead_quality_out = {
                        "total_leads":     int(lq["total_leads"] or 0),
                        "confirmed_sqls":  confirmed_sqls,
                        "in_progress":     in_progress,
                        "confirmed_junk":  confirmed_junk,
                        "wrong_fit":       wrong_fit,
                        "unknown":         unknown,
                        "verdicted_leads": verdicted,
                        "junk_rate_pct":   junk_rate,
                    }

                # ── Country breakdown — deduped campaign leads by country ──────
                # Direct equality on campaign_name (already normalized).
                cur.execute(
                    """
                    WITH deduped AS (
                        SELECT DISTINCT ON (
                            CASE
                                WHEN contact_id IS NOT NULL AND contact_id <> ''
                                THEN contact_id
                                ELSE CAST(id AS TEXT)
                            END
                        )
                            country,
                            status_category
                        FROM leads
                        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                          AND campaign_name = %s
                        ORDER BY
                            CASE
                                WHEN contact_id IS NOT NULL AND contact_id <> ''
                                THEN contact_id
                                ELSE CAST(id AS TEXT)
                            END,
                            run_date DESC,
                            id DESC
                    )
                    SELECT
                        COALESCE(NULLIF(BTRIM(country), ''), '(unknown)') AS country,
                        COUNT(*)                                     AS total_leads,
                        SUM(CASE WHEN status_category = 'qualified'   THEN 1 ELSE 0 END) AS confirmed_sqls,
                        SUM(CASE WHEN status_category = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                        SUM(CASE WHEN status_category = 'junk'        THEN 1 ELSE 0 END) AS confirmed_junk,
                        SUM(CASE WHEN status_category = 'wrong_fit'   THEN 1 ELSE 0 END) AS wrong_fit,
                        SUM(CASE WHEN status_category = 'unknown'     THEN 1 ELSE 0 END) AS unknown
                    FROM deduped
                    GROUP BY COALESCE(NULLIF(BTRIM(country), ''), '(unknown)')
                    ORDER BY total_leads DESC
                    """,
                    (days, name_key),
                )
                country_rows = cur.fetchall()
                country_cols = [d[0] for d in cur.description]
                countries_out = []
                for row in country_rows:
                    r = dict(zip(country_cols, row))
                    c_sqls = int(r["confirmed_sqls"] or 0)
                    c_prog = int(r["in_progress"]    or 0)
                    c_junk = int(r["confirmed_junk"] or 0)
                    c_wfit = int(r["wrong_fit"]      or 0)
                    c_unk  = int(r["unknown"]        or 0)
                    # verdicted_leads = qualified + in_progress + junk + wrong_fit
                    # (consistent with /api/leads/country-summary definition)
                    c_verd = c_sqls + c_prog + c_junk + c_wfit
                    c_junk_rate = round((c_junk / c_verd) * 100, 2) if c_verd > 0 else None
                    countries_out.append({
                        "country":        r["country"],
                        "total_leads":    int(r["total_leads"] or 0),
                        "confirmed_sqls": c_sqls,
                        "in_progress":    c_prog,
                        "confirmed_junk": c_junk,
                        "wrong_fit":      c_wfit,
                        "unknown":        c_unk,
                        "junk_rate_pct":  c_junk_rate,
                    })

                # ── Keywords preview — top 10 by spend for this campaign ───────
                # Direct equality on campaign_name (already normalized).
                cur.execute(
                    """
                    SELECT
                        keyword,
                        match_type,
                        SUM(spend_usd)     AS spend_usd,
                        SUM(clicks)        AS clicks,
                        SUM(impressions)   AS impressions,
                        SUM(conversions)   AS conversions,
                        AVG(quality_score) AS quality_score,
                        CASE
                            WHEN SUM(clicks) > 0 THEN SUM(spend_usd) / SUM(clicks)
                            ELSE 0
                        END                AS cpc_usd
                    FROM keywords
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                      AND campaign_name = %s
                    GROUP BY keyword, match_type
                    ORDER BY spend_usd DESC NULLS LAST
                    LIMIT 10
                    """,
                    (days, name_key),
                )
                kw_rows = cur.fetchall()
                kw_cols = [d[0] for d in cur.description]
                keywords_out = []
                for row in kw_rows:
                    r = dict(zip(kw_cols, row))
                    keywords_out.append({
                        "keyword":       r["keyword"],
                        "match_type":    r["match_type"],
                        "spend_usd":     round(float(r["spend_usd"]),     2) if r["spend_usd"]     is not None else 0.0,
                        "clicks":        int(r["clicks"]   or 0),
                        "impressions":   int(r["impressions"] or 0),
                        "conversions":   round(float(r["conversions"]),   2) if r["conversions"]   is not None else 0.0,
                        "quality_score": round(float(r["quality_score"]), 2) if r["quality_score"] is not None else None,
                        "cpc_usd":       round(float(r["cpc_usd"]),       2) if r["cpc_usd"]       is not None else 0.0,
                    })

                # ── Waste terms preview — top 10 by spend for this campaign ───
                # Direct equality on campaign_name (already normalized).
                cur.execute(
                    """
                    SELECT
                        search_term,
                        SUM(spend_usd)          AS spend_usd,
                        junk_category,
                        matched_pattern,
                        SUM(crm_junk_confirmed) AS crm_junk_confirmed,
                        MAX(run_date)           AS run_date
                    FROM waste_terms
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                      AND campaign_name = %s
                    GROUP BY search_term, junk_category, matched_pattern
                    ORDER BY spend_usd DESC NULLS LAST
                    LIMIT 10
                    """,
                    (days, name_key),
                )
                wt_rows = cur.fetchall()
                wt_cols = [d[0] for d in cur.description]
                waste_out = []
                for row in wt_rows:
                    r = dict(zip(wt_cols, row))
                    waste_out.append({
                        "search_term":        r["search_term"],
                        "spend_usd":          round(float(r["spend_usd"]), 2) if r["spend_usd"] is not None else 0.0,
                        "junk_category":      r["junk_category"],
                        "matched_pattern":    r["matched_pattern"],
                        "crm_junk_confirmed": int(r["crm_junk_confirmed"] or 0),
                        "run_date":           str(r["run_date"]) if r["run_date"] else None,
                    })

                # ── Recent leads — 10 most recent deduped leads ───────────────
                # Direct equality on campaign_name (already normalized).
                cur.execute(
                    """
                    WITH deduped AS (
                        SELECT DISTINCT ON (
                            CASE
                                WHEN contact_id IS NOT NULL AND contact_id <> ''
                                THEN contact_id
                                ELSE CAST(id AS TEXT)
                            END
                        )
                            company,
                            country,
                            keyword,
                            mql_status,
                            status_category,
                            run_date
                        FROM leads
                        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                          AND campaign_name = %s
                        ORDER BY
                            CASE
                                WHEN contact_id IS NOT NULL AND contact_id <> ''
                                THEN contact_id
                                ELSE CAST(id AS TEXT)
                            END,
                            run_date DESC,
                            id DESC
                    )
                    SELECT company, country, keyword, mql_status, status_category, run_date
                    FROM deduped
                    ORDER BY run_date DESC
                    LIMIT 10
                    """,
                    (days, name_key),
                )
                rl_rows = cur.fetchall()
                rl_cols = [d[0] for d in cur.description]
                recent_leads_out = []
                for row in rl_rows:
                    r = dict(zip(rl_cols, row))
                    recent_leads_out.append({
                        "company":         r["company"],
                        "country":         r["country"],
                        "keyword":         r["keyword"],
                        "mql_status":      r["mql_status"],
                        "status_category": r["status_category"],
                        "run_date":        str(r["run_date"]) if r["run_date"] else None,
                    })

    except Exception as exc:  # noqa: BLE001
        log.error("[api/campaign-detail] database error: %s", exc, exc_info=True)
        return _db_empty

    return {
        "days":          days,
        "campaign_name": campaign_name,
        "campaign":      campaign_out,
        "lead_quality":  lead_quality_out,
        "countries":     countries_out,
        "keywords":      keywords_out,
        "waste_terms":   waste_out,
        "recent_leads":  recent_leads_out,
        "data_sources": {
            "campaign":     "PostgreSQL campaigns table",
            "lead_quality": "HubSpot-derived leads table",
            "keywords":     "Windsor keyword performance",
            "waste_terms":  "Waste detection from search terms",
        },
    }


@app.get("/api/campaign-detail")
def api_campaign_detail_query(
    user: dict = Depends(require_auth),
    campaign_name: str = Query(..., description="Campaign name (URL-encoded)"),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return campaign drill-down detail via query parameter. Preferred endpoint.

    Using a query parameter avoids routing issues with campaign names that
    contain literal forward slashes. The frontend must call
    encodeURIComponent(campaign_name) before appending to the URL.
    Phase 1 read-only — no writes to Google Ads or HubSpot.
    """
    return _build_campaign_detail(campaign_name, _clamp_days(days))


@app.get("/api/campaigns/{campaign_name}/detail")
def api_campaign_detail_path(
    campaign_name: str,
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return campaign drill-down detail via path segment. Legacy compatibility route.

    Prefer /api/campaign-detail?campaign_name=... for new callers.
    Campaign names containing literal '/' cannot be addressed via this route.
    Phase 1 read-only — no writes to Google Ads or HubSpot.
    """
    return _build_campaign_detail(campaign_name, _clamp_days(days))


# ---------------------------------------------------------------------------
# UI config endpoint — read-only, auth required.
# ---------------------------------------------------------------------------

# Safe backend defaults — match current hardcoded UI values.
_UI_THRESHOLDS_DEFAULTS: dict[str, Any] = {
    "junk_rate": {
        "low_pct": 15,
        "high_pct": 30,
    },
    "spend": {
        "high_spend_usd": 100,
    },
    "quality_score": {
        "strong_min": 8,
        "medium_min": 5,
    },
    "freshness": {
        "stale_after_days": 2,
    },
}


def _load_ui_thresholds() -> dict[str, Any]:
    """Load UI-safe threshold values from config/thresholds.yaml.

    Validates each field individually:
    - Numeric type (rejects strings like "30%").
    - Range bounds per field.
    - Ordering constraints (junk high_pct >= low_pct; quality strong_min >= medium_min).
    Falls back to the safe default for any field that fails validation and sets
    using_defaults=True in the response.
    Never exposes API keys, account IDs, or full YAML content.
    """
    defaults = _UI_THRESHOLDS_DEFAULTS
    using_defaults = False

    def _validate_num(value: Any, default: float, lo: float | None = None, hi: float | None = None) -> tuple[float, bool]:
        """Parse *value* as float, enforce range [lo, hi]; return (result, fell_back)."""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default, True
        if lo is not None and parsed < lo:
            return default, True
        if hi is not None and parsed > hi:
            return default, True
        return parsed, False

    def _int_if_whole(v: float) -> int | float:
        """Return an int when the float has no fractional part, to keep JSON tidy."""
        return int(v) if v == int(v) else v

    try:
        with _CONFIG_THRESHOLDS.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        ui_section = raw.get("ui", {}) or {}

        junk_rate     = ui_section.get("junk_rate",     {}) or {}
        spend         = ui_section.get("spend",         {}) or {}
        quality_score = ui_section.get("quality_score", {}) or {}
        freshness     = ui_section.get("freshness",     {}) or {}

        # ── junk_rate ──────────────────────────────────────────────────────
        low_pct,  fb_low  = _validate_num(junk_rate.get("low_pct"),  defaults["junk_rate"]["low_pct"],  lo=0, hi=100)
        high_pct, fb_high = _validate_num(junk_rate.get("high_pct"), defaults["junk_rate"]["high_pct"], lo=0, hi=100)
        if fb_low or fb_high:
            using_defaults = True
        # Ordering: high_pct must be >= low_pct (they bound the mid/yellow band from below and above).
        if high_pct < low_pct:
            log.warning("[/api/config/ui-thresholds] junk_rate.high_pct < low_pct, using defaults")
            low_pct  = float(defaults["junk_rate"]["low_pct"])
            high_pct = float(defaults["junk_rate"]["high_pct"])
            using_defaults = True

        # ── spend ──────────────────────────────────────────────────────────
        high_spend_usd, fb_spend = _validate_num(
            spend.get("high_spend_usd"),
            defaults["spend"]["high_spend_usd"],
            lo=0,
        )
        if fb_spend:
            using_defaults = True

        # ── quality_score ──────────────────────────────────────────────────
        # strong_min is the higher bar (e.g. 8+); medium_min is the lower bar (e.g. 5–7).
        # medium_min must be <= strong_min so the two bands do not overlap or invert.
        strong_min, fb_strong = _validate_num(quality_score.get("strong_min"), defaults["quality_score"]["strong_min"], lo=0, hi=10)
        medium_min, fb_medium = _validate_num(quality_score.get("medium_min"), defaults["quality_score"]["medium_min"], lo=0, hi=10)
        if fb_strong or fb_medium:
            using_defaults = True
        if medium_min > strong_min:
            log.warning("[/api/config/ui-thresholds] quality_score.medium_min > strong_min, using defaults")
            strong_min = float(defaults["quality_score"]["strong_min"])
            medium_min = float(defaults["quality_score"]["medium_min"])
            using_defaults = True

        # ── freshness ──────────────────────────────────────────────────────
        stale_after_days, fb_sad = _validate_num(
            freshness.get("stale_after_days"),
            defaults["freshness"]["stale_after_days"],
            lo=1,
        )
        if fb_sad:
            using_defaults = True

    except Exception as exc:  # noqa: BLE001
        log.warning("[/api/config/ui-thresholds] YAML load failed, using defaults: %s", exc)
        return {**_UI_THRESHOLDS_DEFAULTS, "using_defaults": True}

    result: dict[str, Any] = {
        "junk_rate": {
            "low_pct":  _int_if_whole(low_pct),
            "high_pct": _int_if_whole(high_pct),
        },
        "spend": {
            "high_spend_usd": _int_if_whole(high_spend_usd),
        },
        "quality_score": {
            "strong_min": _int_if_whole(strong_min),
            "medium_min": _int_if_whole(medium_min),
        },
        "freshness": {
            "stale_after_days": _int_if_whole(stale_after_days),
        },
    }
    if using_defaults:
        result["using_defaults"] = True
    return result


@app.get("/api/config/ui-thresholds")
def api_ui_thresholds(user: dict = Depends(require_auth)) -> dict[str, Any]:
    """Return UI-safe display thresholds from config/thresholds.yaml.

    Auth required. Read-only. Does not expose full config, API keys, account
    IDs, or any sensitive values. Falls back to safe defaults if the config
    file cannot be read. Phase 1 read-only — no writes to any external system.
    """
    return _load_ui_thresholds()


# ---------------------------------------------------------------------------
# Dashboard trends endpoint — previous-period comparison. Read-only, auth required.
# ---------------------------------------------------------------------------

# Movement classification thresholds.
# These are local constants pending potential migration to config/thresholds.yaml.
# junk_rate: meaningful if absolute delta >= 10 percentage points.
# spend:     meaningful if relative delta >= 20% of previous period spend.
# sqls:      meaningful if integer delta != 0.
_TREND_JUNK_DELTA_THRESHOLD = 10      # absolute percentage-point change
_TREND_SPEND_DELTA_PCT = 20           # percent spend change considered meaningful
_TREND_HIGH_JUNK_PCT = 30             # matches uiThresholds.junk_rate.high_pct default


def _trend_metric_insufficient(current: float | int | None) -> dict[str, Any]:
    """Build a trend metric object for when no previous-period data exists."""
    return {
        "current":   current,
        "previous":  None,
        "delta":     None,
        "delta_pct": None,
        "trend":     "insufficient_data",
    }


def _trend_metric(cur_val: float, prev_val: float) -> dict[str, Any]:
    """Build a comparable metric object for a float value."""
    c = round(float(cur_val or 0), 2)
    p = round(float(prev_val or 0), 2)
    delta = round(c - p, 2)
    delta_pct = round((delta / p) * 100, 2) if p != 0 else None
    trend = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return {"current": c, "previous": p, "delta": delta, "delta_pct": delta_pct, "trend": trend}


def _trend_metric_int(cur_val: int, prev_val: int) -> dict[str, Any]:
    """Build a comparable metric object for an integer value."""
    c = int(cur_val or 0)
    p = int(prev_val or 0)
    delta = c - p
    delta_pct = round((delta / p) * 100, 2) if p != 0 else None
    trend = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return {"current": c, "previous": p, "delta": delta, "delta_pct": delta_pct, "trend": trend}


def _compute_severity(
    cur_c: dict | None,
    prev_c: dict | None,
    spend_delta_pct: float | None = None,
    junk_delta: float | None = None,
) -> int:
    """Compute a 0–100 display severity score for a campaign movement.

    Scoring (display severity only — not an automated action recommendation):
      +30 if current SQLs = 0 and spend > 0
      +25 if current junk_rate_pct >= high junk threshold
      +20 if spend increased >= 20%
      +20 if junk rate increased >= 10 points
      +15 if verdict is FIX
      +25 if verdict is CUT
    Capped at 100.
    """
    if cur_c is None:
        return 0
    score = 0
    if cur_c["confirmed_sqls"] == 0 and (cur_c["spend_usd"] or 0) > 0:
        score += 30
    if cur_c["junk_rate_pct"] is not None and cur_c["junk_rate_pct"] >= _TREND_HIGH_JUNK_PCT:
        score += 25
    if spend_delta_pct is not None and spend_delta_pct >= _TREND_SPEND_DELTA_PCT:
        score += 20
    if junk_delta is not None and junk_delta >= _TREND_JUNK_DELTA_THRESHOLD:
        score += 20
    verdict = (cur_c.get("verdict") or "").upper()
    if verdict == "FIX":
        score += 15
    elif verdict == "CUT":
        score += 25
    return min(100, score)


def _build_trend_alerts(movements: list[dict], has_previous: bool) -> list[dict]:
    """Build alert objects from campaign movement data.

    Alert language is evidence-based and warrants review only.
    Does not say pause, cut, increase budget, or apply negatives.
    Returns up to 8 alerts ordered by severity bucket (high, medium, low).
    """
    alerts: list[dict] = []

    for m in movements:
        cur_c = m.get("current")
        prev_c = m.get("previous")
        name = m["campaign_name"]
        movement = m["movement"]
        score = m["severity_score"]

        if movement == "worsened":
            if cur_c and cur_c["confirmed_sqls"] == 0 and (cur_c["spend_usd"] or 0) > 0 and prev_c:
                prev_sqls = prev_c["confirmed_sqls"]
                spend_str = f"${cur_c['spend_usd']:.0f}" if cur_c["spend_usd"] else "unknown spend"
                alerts.append({
                    "campaign_name": name,
                    "severity": "high" if score >= 60 else "medium",
                    "title": "Spend rose without SQLs",
                    "detail": (
                        f"Spend is {spend_str} with 0 confirmed SQLs this period"
                        + (f" (was {prev_sqls} SQL{'s' if prev_sqls != 1 else ''} previously)" if prev_sqls else "")
                        + ". Warrants review."
                    ),
                    "source": "campaigns table",
                })
            elif cur_c and cur_c.get("junk_rate_pct") is not None and prev_c and prev_c.get("junk_rate_pct") is not None:
                junk_delta = cur_c["junk_rate_pct"] - prev_c["junk_rate_pct"]
                if junk_delta >= _TREND_JUNK_DELTA_THRESHOLD:
                    alerts.append({
                        "campaign_name": name,
                        "severity": "high" if score >= 60 else "medium",
                        "title": "Junk rate worsened",
                        "detail": (
                            f"Junk rate increased from {prev_c['junk_rate_pct']:.1f}% to "
                            f"{cur_c['junk_rate_pct']:.1f}% (+{junk_delta:.1f} points). Warrants review."
                        ),
                        "source": "campaigns table",
                    })
                else:
                    alerts.append({
                        "campaign_name": name,
                        "severity": "medium" if score >= 40 else "low",
                        "title": f"Campaign moved to {cur_c.get('verdict', 'worsened')} status",
                        "detail": m["reason"] + " Warrants review.",
                        "source": "campaigns table",
                    })
            else:
                alerts.append({
                    "campaign_name": name,
                    "severity": "medium" if score >= 40 else "low",
                    "title": "Campaign performance worsened",
                    "detail": m["reason"] + " Warrants review.",
                    "source": "campaigns table",
                })

        elif movement == "new" and not has_previous:
            alerts.append({
                "campaign_name": name,
                "severity": "low",
                "title": "Campaign has no previous-period comparison",
                "detail": "No previous-period data available for comparison yet.",
                "source": "campaigns table",
            })

        elif cur_c and (cur_c.get("verdict") or "").upper() in ("FIX", "CUT"):
            verdict = cur_c["verdict"].upper()
            if not any(a["campaign_name"] == name for a in alerts):
                alerts.append({
                    "campaign_name": name,
                    "severity": "high" if verdict == "CUT" else "medium",
                    "title": f"Campaign has verdict {verdict}",
                    "detail": f"Campaign is currently rated {verdict}. Warrants review.",
                    "source": "campaigns table",
                })

    # Deduplicate by campaign_name (keep highest severity)
    seen: dict[str, int] = {}
    deduped: list[dict] = []
    severity_rank = {"high": 3, "medium": 2, "low": 1}
    for a in alerts:
        key = a["campaign_name"]
        rank = severity_rank.get(a["severity"], 0)
        if key not in seen or rank > seen[key]:
            seen[key] = rank
            deduped = [x for x in deduped if x["campaign_name"] != key]
            deduped.append(a)

    # Re-sort by severity
    deduped.sort(key=lambda x: severity_rank.get(x["severity"], 0), reverse=True)
    return deduped[:8]


@app.get("/api/dashboard/trends")
def api_dashboard_trends(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return previous-period trend comparison for the dashboard. Requires auth.

    Compares the current period (last N days) against the previous period
    (the N days before that). Returns summary metrics, campaign movements,
    and display alerts. Phase 1 read-only — no writes to any external system.
    """
    from datetime import timedelta  # noqa: PLC0415

    days = _clamp_days(days)

    _safe_empty: dict[str, Any] = {
        "days": days,
        "summary": {},
        "campaign_movements": [],
        "alerts": [],
        "data_quality": {"status": "db_unavailable"},
        "db_unavailable": True,
    }

    # Period date bounds for response metadata (UTC date arithmetic)
    today = datetime.now(tz=timezone.utc).date()
    current_start = today - timedelta(days=days)
    previous_start = today - timedelta(days=days * 2)
    previous_end = current_start  # exclusive upper bound for previous period

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_empty

            with conn.cursor() as cur:
                # ── Run counts per period (data quality) ─────────────────────
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE started_at >= NOW() - INTERVAL '1 day' * %s
                        ) AS current_runs,
                        COUNT(*) FILTER (
                            WHERE started_at >= NOW() - INTERVAL '1 day' * (%s * 2)
                              AND started_at <  NOW() - INTERVAL '1 day' * %s
                        ) AS previous_runs
                    FROM runs
                    """,
                    (days, days, days),
                )
                rc_row = cur.fetchone()
                current_runs = int(rc_row[0] or 0) if rc_row else 0
                previous_runs = int(rc_row[1] or 0) if rc_row else 0
                has_previous = previous_runs > 0

                # ── Summary: spend/SQLs from campaigns (latest run per period) ─
                # Uses the same latest-run-per-period pattern as /api/summary to
                # avoid double-counting overlapping weekly/monthly run snapshots.
                cur.execute(
                    """
                    WITH latest_run AS (
                        SELECT MAX(id) AS max_run_id
                        FROM runs
                        WHERE started_at >= NOW() - INTERVAL '1 day' * %s
                    )
                    SELECT
                        COALESCE(SUM(c.spend_usd), 0)      AS total_spend_usd,
                        COALESCE(SUM(c.confirmed_sqls), 0) AS confirmed_sqls,
                        COALESCE(SUM(c.total_leads), 0)    AS total_leads,
                        COALESCE(SUM(c.junk_count), 0)     AS total_junk
                    FROM campaigns c
                    JOIN latest_run lr ON c.run_id = lr.max_run_id
                    """,
                    (days,),
                )
                cur_camp_agg = cur.fetchone()

                cur.execute(
                    """
                    WITH latest_run AS (
                        SELECT MAX(id) AS max_run_id
                        FROM runs
                        WHERE started_at >= NOW() - INTERVAL '1 day' * (%s * 2)
                          AND started_at <  NOW() - INTERVAL '1 day' * %s
                    )
                    SELECT
                        COALESCE(SUM(c.spend_usd), 0)      AS total_spend_usd,
                        COALESCE(SUM(c.confirmed_sqls), 0) AS confirmed_sqls,
                        COALESCE(SUM(c.total_leads), 0)    AS total_leads,
                        COALESCE(SUM(c.junk_count), 0)     AS total_junk
                    FROM campaigns c
                    JOIN latest_run lr ON c.run_id = lr.max_run_id
                    """,
                    (days, days),
                )
                prev_camp_agg = cur.fetchone()

                # ── Waste spend summed over period (confirmed junk only) ───────
                cur.execute(
                    """
                    SELECT COALESCE(SUM(spend_usd), 0) AS waste_usd
                    FROM waste_terms
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                      AND crm_junk_confirmed > 0
                    """,
                    (days,),
                )
                cur_waste_row = cur.fetchone()

                cur.execute(
                    """
                    SELECT COALESCE(SUM(spend_usd), 0) AS waste_usd
                    FROM waste_terms
                    WHERE run_date >= NOW() - INTERVAL '1 day' * (%s * 2)
                      AND run_date  <  NOW() - INTERVAL '1 day' * %s
                      AND crm_junk_confirmed > 0
                    """,
                    (days, days),
                )
                prev_waste_row = cur.fetchone()

                # ── Campaign snapshots for movement analysis ──────────────────
                # Latest snapshot per campaign in current period.
                cur.execute(
                    """
                    SELECT DISTINCT ON (campaign_name)
                        campaign_name,
                        spend_usd,
                        confirmed_sqls,
                        junk_rate_pct,
                        verdict
                    FROM campaigns
                    WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                    ORDER BY campaign_name, run_date DESC, created_at DESC, id DESC
                    """,
                    (days,),
                )
                cur_camp_rows = cur.fetchall()
                cur_camp_cols = [d[0] for d in cur.description]

                # Latest snapshot per campaign in previous period.
                cur.execute(
                    """
                    SELECT DISTINCT ON (campaign_name)
                        campaign_name,
                        spend_usd,
                        confirmed_sqls,
                        junk_rate_pct,
                        verdict
                    FROM campaigns
                    WHERE run_date >= NOW() - INTERVAL '1 day' * (%s * 2)
                      AND run_date  <  NOW() - INTERVAL '1 day' * %s
                    ORDER BY campaign_name, run_date DESC, created_at DESC, id DESC
                    """,
                    (days, days),
                )
                prev_camp_rows = cur.fetchall()
                prev_camp_cols = [d[0] for d in cur.description]

    except Exception as exc:  # noqa: BLE001
        log.error("[api/dashboard/trends] database error: %s", exc, exc_info=True)
        return _safe_empty

    # ── Build summary metrics ─────────────────────────────────────────────────
    cur_spend   = float(cur_camp_agg[0]  or 0) if cur_camp_agg  else 0.0
    prev_spend  = float(prev_camp_agg[0] or 0) if prev_camp_agg else 0.0
    cur_sqls    = int(cur_camp_agg[1]    or 0) if cur_camp_agg  else 0
    prev_sqls   = int(prev_camp_agg[1]   or 0) if prev_camp_agg else 0
    cur_leads   = int(cur_camp_agg[2]    or 0) if cur_camp_agg  else 0
    cur_junk    = int(cur_camp_agg[3]    or 0) if cur_camp_agg  else 0
    prev_leads  = int(prev_camp_agg[2]   or 0) if prev_camp_agg else 0
    prev_junk   = int(prev_camp_agg[3]   or 0) if prev_camp_agg else 0
    cur_waste   = float(cur_waste_row[0]  or 0) if cur_waste_row  else 0.0
    prev_waste  = float(prev_waste_row[0] or 0) if prev_waste_row else 0.0

    cur_junk_rate  = round((cur_junk  / cur_leads)  * 100, 1) if cur_leads  > 0 else None
    prev_junk_rate = round((prev_junk / prev_leads) * 100, 1) if prev_leads > 0 else None

    summary: dict[str, Any]
    if not has_previous:
        # No previous-period data — return current values only, no fake comparisons against zero.
        summary = {
            "spend_usd":           _trend_metric_insufficient(round(cur_spend, 2)),
            "confirmed_sqls":      _trend_metric_insufficient(cur_sqls),
            "confirmed_waste_usd": _trend_metric_insufficient(round(cur_waste, 2)),
            "avg_junk_rate_pct":   _trend_metric_insufficient(cur_junk_rate),
        }
    else:
        summary = {
            "spend_usd":            _trend_metric(cur_spend, prev_spend),
            "confirmed_sqls":       _trend_metric_int(cur_sqls, prev_sqls),
            "confirmed_waste_usd":  _trend_metric(cur_waste, prev_waste),
        }

        # Junk rate metric — both values may be None if no lead data exists
        if cur_junk_rate is not None and prev_junk_rate is not None:
            junk_delta     = round(cur_junk_rate - prev_junk_rate, 2)
            junk_delta_pct = round((junk_delta / prev_junk_rate) * 100, 2) if prev_junk_rate != 0 else None
            junk_trend     = "up" if junk_delta > 0 else ("down" if junk_delta < 0 else "flat")
            summary["avg_junk_rate_pct"] = {
                "current": cur_junk_rate, "previous": prev_junk_rate,
                "delta": junk_delta, "delta_pct": junk_delta_pct, "trend": junk_trend,
            }
        else:
            summary["avg_junk_rate_pct"] = {
                "current": cur_junk_rate, "previous": prev_junk_rate,
                "delta": None, "delta_pct": None, "trend": "insufficient_data",
            }

    # ── Build campaign movement lookup dicts ──────────────────────────────────
    def _snap(cols: list[str], rows: list[tuple]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for row in rows:
            r = dict(zip(cols, row))
            result[r["campaign_name"]] = {
                "spend_usd":      float(r["spend_usd"])      if r["spend_usd"]      is not None else 0.0,
                "confirmed_sqls": int(r["confirmed_sqls"] or 0),
                "junk_rate_pct":  float(r["junk_rate_pct"]) if r["junk_rate_pct"]  is not None else None,
                "verdict":        r["verdict"],
            }
        return result

    cur_snaps  = _snap(cur_camp_cols,  cur_camp_rows)
    prev_snaps = _snap(prev_camp_cols, prev_camp_rows)

    all_campaign_names = set(cur_snaps.keys()) | set(prev_snaps.keys())
    movements: list[dict] = []

    for name in sorted(all_campaign_names):
        in_current  = name in cur_snaps
        in_previous = name in prev_snaps
        cur_c  = cur_snaps.get(name)
        prev_c = prev_snaps.get(name)

        spend_delta_pct: float | None = None
        junk_rate_delta: float | None = None

        if in_current and not in_previous:
            movement = "new"
            reason   = "Campaign appears in current period with no previous-period data."
            severity = _compute_severity(cur_c, None)

        elif in_previous and not in_current:
            movement = "dropped"
            reason   = "Campaign no longer appears in current period."
            severity = 0

        else:
            # Both periods present — calculate deltas
            if prev_c["spend_usd"] > 0:
                spend_delta_pct = ((cur_c["spend_usd"] - prev_c["spend_usd"]) / prev_c["spend_usd"]) * 100
            if cur_c["junk_rate_pct"] is not None and prev_c["junk_rate_pct"] is not None:
                junk_rate_delta = cur_c["junk_rate_pct"] - prev_c["junk_rate_pct"]

            sql_delta = cur_c["confirmed_sqls"] - prev_c["confirmed_sqls"]

            severity = _compute_severity(cur_c, prev_c, spend_delta_pct, junk_rate_delta)

            if cur_c["junk_rate_pct"] is None and prev_c["junk_rate_pct"] is None:
                movement = "insufficient_data"
                reason   = "No junk rate data available in either period for comparison."
            else:
                spend_rose  = spend_delta_pct is not None and spend_delta_pct >= _TREND_SPEND_DELTA_PCT
                junk_rose   = junk_rate_delta  is not None and junk_rate_delta  >= _TREND_JUNK_DELTA_THRESHOLD
                junk_fell   = junk_rate_delta  is not None and junk_rate_delta  <= -_TREND_JUNK_DELTA_THRESHOLD
                sqls_rose   = sql_delta > 0
                sqls_fell   = sql_delta < 0
                no_sqls_spend = spend_rose and cur_c["confirmed_sqls"] == 0

                if sqls_fell or junk_rose or no_sqls_spend:
                    movement = "worsened"
                    parts: list[str] = []
                    if sqls_fell:
                        parts.append(f"SQLs fell by {abs(sql_delta)}")
                    if junk_rose:
                        parts.append(f"junk rate rose {junk_rate_delta:.1f} points")
                    if no_sqls_spend:
                        parts.append(f"spend rose {spend_delta_pct:.0f}% with no SQLs")
                    if parts:
                        joined = ". ".join(parts)
                        reason = joined[0].upper() + joined[1:] + "."
                    else:
                        reason = "Performance metrics worsened."
                elif sqls_rose or junk_fell:
                    movement = "improved"
                    parts = []
                    if sqls_rose:
                        parts.append(f"SQLs increased by {sql_delta}")
                    if junk_fell:
                        parts.append(f"junk rate fell {abs(junk_rate_delta):.1f} points")
                    if parts:
                        joined = ". ".join(parts)
                        reason = joined[0].upper() + joined[1:] + "."
                    else:
                        reason = "Performance metrics improved."
                else:
                    movement = "stable"
                    reason   = "No meaningful change detected."

        movements.append({
            "campaign_name": name,
            "current":        cur_c,
            "previous":       prev_c,
            "movement":       movement,
            "reason":         reason,
            "severity_score": severity,
        })

    movements.sort(key=lambda x: (-x["severity_score"], x["campaign_name"]))

    # ── Build alerts (display severity only, no action recommendations) ───────
    alerts = _build_trend_alerts(movements, has_previous)

    data_quality: dict[str, Any] = {
        "has_previous_period": has_previous,
        "current_runs":        current_runs,
        "previous_runs":       previous_runs,
        "status":              "ok" if has_previous else "insufficient_previous_data",
    }

    return {
        "days": days,
        "current_period":  {"start": str(current_start),  "end": str(today)},
        "previous_period": {"start": str(previous_start), "end": str(previous_end)},
        "summary":           summary,
        "campaign_movements": movements[:10],  # top 10 by severity_score
        "alerts":            alerts,
        "data_quality":      data_quality,
    }


# ---------------------------------------------------------------------------
# Action Queue endpoint — ranked human-review queue. Read-only, auth required.
# ---------------------------------------------------------------------------

_QUEUE_JUNK_HIGH_PCT_DEFAULT = 30        # fallback if thresholds not loaded
_QUEUE_HIGH_SPEND_USD_DEFAULT = 100      # fallback if thresholds not loaded
_QUEUE_MAX_ITEMS = 30                    # hard cap on returned queue items
_QUEUE_FRAUD_CATEGORIES = {"fraud", "job_seeker", "student", "free_intent_english",
                             "free_intent_spanish", "free_intent_arabic"}


def _queue_severity_label(score: int) -> str:
    """Map a severity score to high / medium / low label."""
    if score >= 75:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _queue_id(prefix: str, *parts: object) -> str:
    """Build a stable, collision-safe queue item ID.

    Combines a readable prefix with a short SHA-1 digest of the
    normalised constituent parts so that items that differ only by
    campaign_name, junk_category, match_type, etc. get distinct IDs.
    """
    raw = "\x00".join(str(p or "").strip().lower() for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]  # noqa: S324 — non-security hash for ID dedup
    safe_prefix = prefix.replace(" ", "-").replace("/", "-").lower()
    return f"{safe_prefix}-{digest}"


def _build_campaign_queue_items(
    cur,
    days: int,
    high_junk_pct: float,
    high_spend_usd: float,
) -> list[dict]:
    """Build campaign_review queue items from the campaigns table."""
    cur.execute(
        """
        SELECT DISTINCT ON (campaign_name)
            campaign_name,
            spend_usd,
            confirmed_sqls,
            junk_rate_pct,
            verdict
        FROM campaigns
        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
        ORDER BY campaign_name, run_date DESC, created_at DESC, id DESC
        """,
        (days,),
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    items: list[dict] = []
    for row in rows:
        r = dict(zip(cols, row))
        spend = float(r["spend_usd"] or 0)
        sqls = int(r["confirmed_sqls"] or 0)
        junk_rate = float(r["junk_rate_pct"]) if r["junk_rate_pct"] is not None else None
        verdict = (r["verdict"] or "").upper()

        # Queue inclusion rules
        qualifies = (
            verdict in ("FIX", "CUT")
            or (sqls == 0 and spend > 0)
            or (junk_rate is not None and junk_rate >= high_junk_pct)
        )
        if not qualifies:
            continue

        # Severity scoring (display-only)
        score = 0
        if sqls == 0 and spend > 0:
            score += 30
        if junk_rate is not None and junk_rate >= high_junk_pct:
            score += 25
        if verdict == "FIX":
            score += 20
        elif verdict == "CUT":
            score += 30
        if spend >= high_spend_usd:
            score += 15
        score = min(100, score)

        name = r["campaign_name"] or ""

        detail_parts: list[str] = []
        if spend > 0:
            detail_parts.append(f"Spend is ${spend:.2f}")
        if sqls == 0 and spend > 0:
            detail_parts.append("confirmed SQLs are 0")
        if junk_rate is not None:
            detail_parts.append(f"junk rate is {junk_rate:.1f}%")
        if verdict in ("FIX", "CUT"):
            detail_parts.append(f"verdict is {verdict}")
        if detail_parts:
            detail_body = ". ".join(p for p in detail_parts if p)
            if detail_body:
                detail = f"{detail_body[:1].upper()}{detail_body[1:]}. Warrants review."
            else:
                detail = "Warrants review."
        else:
            detail = "Warrants review."

        items.append({
            "id": _queue_id("campaign-review", name),
            "type": "campaign_review",
            "severity": _queue_severity_label(score),
            "severity_score": score,
            "title": f"Campaign warrants review: {name}",
            "detail": detail,
            "entity_label": name,
            "entity_type": "campaign",
            "campaign_name": name,
            "source": "campaigns table",
            "evidence": {
                "spend_usd": round(spend, 2),
                "confirmed_sqls": sqls,
                "junk_rate_pct": round(junk_rate, 1) if junk_rate is not None else None,
                "verdict": verdict or None,
            },
            "primary_link": {
                "page": "campaigns",
                "action": "open_campaign_drawer",
                "campaign_name": name,
            },
        })
    return items


def _build_waste_queue_items(
    cur,
    days: int,
    high_spend_usd: float,
) -> list[dict]:
    """Build waste_review queue items from the waste_terms table (top 10 by spend)."""
    cur.execute(
        """
        SELECT
            search_term,
            campaign_name,
            SUM(spend_usd)          AS spend_usd,
            junk_category,
            SUM(crm_junk_confirmed) AS crm_junk_confirmed
        FROM waste_terms
        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
        GROUP BY search_term, campaign_name, junk_category
        HAVING SUM(spend_usd) >= %s OR SUM(crm_junk_confirmed) > 0
        ORDER BY spend_usd DESC NULLS LAST
        LIMIT 10
        """,
        (days, high_spend_usd),
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    items: list[dict] = []
    for row in rows:
        r = dict(zip(cols, row))
        spend = float(r["spend_usd"] or 0)
        crm_confirmed = int(r["crm_junk_confirmed"] or 0)
        category = (r["junk_category"] or "").lower()
        term = r["search_term"] or ""
        camp = r["campaign_name"] or ""

        # Severity scoring
        score = 30
        if spend >= high_spend_usd:
            score += 25
        if crm_confirmed > 0:
            score += 20
        if category in _QUEUE_FRAUD_CATEGORIES:
            score += 10
        score = min(100, score)

        safe_id = term.replace(" ", "-").replace("/", "-")[:40]
        detail = f"Waste term '{term}' has ${spend:.2f} spend"
        if crm_confirmed > 0:
            detail += f" and {crm_confirmed} CRM junk confirmed"
        detail += ". Warrants review."

        items.append({
            "id": _queue_id("waste-review", term, camp, r["junk_category"]),
            "type": "waste_review",
            "severity": _queue_severity_label(score),
            "severity_score": score,
            "title": f"Waste term warrants review: {term}",
            "detail": detail,
            "entity_label": term,
            "entity_type": "waste_term",
            "campaign_name": camp,
            "source": "waste_terms table",
            "evidence": {
                "spend_usd": round(spend, 2),
                "crm_junk_confirmed": crm_confirmed,
                "junk_category": r["junk_category"],
            },
            "primary_link": {
                "page": "waste",
                "action": "navigate",
            },
        })
    return items


def _build_geo_queue_items(
    cur,
    days: int,
    high_junk_pct: float,
) -> list[dict]:
    """Build geo_review queue items using leads country summary logic (top 8)."""
    # Deduplicated country summary from leads
    cur.execute(
        """
        WITH deduped AS (
            SELECT DISTINCT ON (
                CASE
                    WHEN contact_id IS NOT NULL AND contact_id <> ''
                    THEN contact_id
                    ELSE CAST(id AS TEXT)
                END
            )
                country,
                status_category
            FROM leads
            WHERE run_date >= NOW() - INTERVAL '1 day' * %s
            ORDER BY
                CASE
                    WHEN contact_id IS NOT NULL AND contact_id <> ''
                    THEN contact_id
                    ELSE CAST(id AS TEXT)
                END,
                run_date DESC,
                id DESC
        )
        SELECT
            COALESCE(NULLIF(BTRIM(country), ''), '(unknown)') AS country,
            COUNT(*)                                     AS total_leads,
            SUM(CASE WHEN status_category = 'qualified'   THEN 1 ELSE 0 END) AS confirmed_sqls,
            SUM(CASE WHEN status_category = 'junk'        THEN 1 ELSE 0 END) AS confirmed_junk,
            SUM(CASE WHEN status_category = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status_category = 'wrong_fit'   THEN 1 ELSE 0 END) AS wrong_fit
        FROM deduped
        GROUP BY COALESCE(NULLIF(BTRIM(country), ''), '(unknown)')
        ORDER BY total_leads DESC
        """,
        (days,),
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]

    # Also get geo spend per country from the geo table
    cur.execute(
        """
        SELECT
            COALESCE(NULLIF(BTRIM(country), ''), '(unknown)') AS country,
            SUM(spend_usd) AS spend_usd
        FROM geo
        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
        GROUP BY COALESCE(NULLIF(BTRIM(country), ''), '(unknown)')
        """,
        (days,),
    )
    spend_rows = cur.fetchall()
    geo_spend: dict[str, float] = {r[0]: float(r[1] or 0) for r in spend_rows}

    items: list[dict] = []
    for row in rows:
        r = dict(zip(cols, row))
        country = r["country"]
        sqls = int(r["confirmed_sqls"] or 0)
        junk = int(r["confirmed_junk"] or 0)
        in_progress = int(r["in_progress"] or 0)
        wrong_fit = int(r["wrong_fit"] or 0)
        verdicted = sqls + in_progress + junk + wrong_fit
        junk_rate = round((junk / verdicted) * 100, 1) if verdicted > 0 else None
        spend = geo_spend.get(country, 0.0)

        # Queue inclusion rules
        qualifies = (
            (junk_rate is not None and junk_rate >= high_junk_pct and verdicted > 0)
            or (spend > 0 and sqls == 0)
            or country == "(unknown)"
        )
        if not qualifies:
            continue

        # Severity scoring
        score = 25
        if junk_rate is not None and junk_rate >= high_junk_pct:
            score += 25
        if sqls == 0 and spend > 0:
            score += 20
        if country == "(unknown)":
            score += 15
        score = min(100, score)

        safe_id = country.replace(" ", "-").replace("(", "").replace(")", "")
        detail_parts: list[str] = []
        if junk_rate is not None:
            detail_parts.append(f"junk rate is {junk_rate:.1f}%")
        if sqls == 0 and spend > 0:
            detail_parts.append("no confirmed SQLs with active spend")
        if country == "(unknown)":
            detail_parts.append("country is unresolved")
        detail = "Country signal warrants review" + (": " + ", ".join(detail_parts) if detail_parts else "") + "."

        items.append({
            "id": _queue_id("geo-review", country),
            "type": "geo_review",
            "severity": _queue_severity_label(score),
            "severity_score": score,
            "title": f"Country signal warrants review: {country}",
            "detail": detail,
            "entity_label": country,
            "entity_type": "country",
            "campaign_name": None,
            "source": "leads country summary + geo table",
            "evidence": {
                "country": country,
                "confirmed_sqls": sqls,
                "confirmed_junk": junk,
                "verdicted_leads": verdicted,
                "junk_rate_pct": junk_rate,
                "spend_usd": round(spend, 2),
            },
            "primary_link": {
                "page": "geo",
                "action": "navigate",
            },
        })

    # Sort by score desc then country, limit 8
    items.sort(key=lambda x: (-x["severity_score"], x["entity_label"]))
    return items[:8]


def _build_keyword_queue_items(
    cur,
    days: int,
    high_spend_usd: float,
) -> list[dict]:
    """Build keyword_review queue items — keywords with spend >= threshold but no conversions (top 10)."""
    cur.execute(
        """
        SELECT
            campaign_name,
            ad_group,
            keyword,
            match_type,
            SUM(spend_usd)    AS spend_usd,
            SUM(conversions)  AS conversions
        FROM keywords
        WHERE run_date >= NOW() - INTERVAL '1 day' * %s
        GROUP BY campaign_name, ad_group, keyword, match_type
        HAVING SUM(spend_usd) >= %s AND COALESCE(SUM(conversions), 0) = 0
        ORDER BY spend_usd DESC NULLS LAST
        LIMIT 10
        """,
        (days, high_spend_usd),
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    items: list[dict] = []
    for row in rows:
        r = dict(zip(cols, row))
        spend = float(r["spend_usd"] or 0)
        conversions = float(r["conversions"] or 0)
        kw = r["keyword"] or ""
        match_type = (r["match_type"] or "").lower()
        camp = r["campaign_name"] or ""
        ad_group = r["ad_group"] or ""

        # Severity scoring
        score = 20
        if spend >= high_spend_usd:
            score += 25
        if conversions <= 0:
            score += 20
        if match_type == "broad":
            score += 10
        score = min(100, score)

        safe_id = kw.replace(" ", "-").replace("/", "-")[:40]
        detail = (
            f"Keyword '{kw}' has ${spend:.2f} spend with 0 Google Ads conversions"
            + (f" ({match_type} match)" if match_type else "")
            + ". Warrants review."
        )

        items.append({
            "id": _queue_id("keyword-review", camp, ad_group, kw, match_type),
            "type": "keyword_review",
            "severity": _queue_severity_label(score),
            "severity_score": score,
            "title": f"Keyword warrants review: {kw}",
            "detail": detail,
            "entity_label": kw,
            "entity_type": "keyword",
            "campaign_name": camp,
            "source": "keywords table",
            "evidence": {
                "keyword": kw,
                "ad_group": ad_group,
                "match_type": r["match_type"],
                "spend_usd": round(spend, 2),
                "google_ads_conversions": round(conversions, 2),
            },
            "primary_link": {
                "page": "keywords",
                "action": "navigate",
            },
        })
    return items


def _build_data_quality_items(cur, days: int) -> list[dict]:
    """Build data_quality_review items from the runs table."""
    # Get latest run overall and recent weekly/monthly runs
    cur.execute(
        """
        SELECT run_type, started_at, finished_at, status
        FROM runs
        ORDER BY started_at DESC
        LIMIT 1
        """,
    )
    latest_row = cur.fetchone()
    latest_cols = [d[0] for d in cur.description]
    latest_run = dict(zip(latest_cols, latest_row)) if latest_row else None

    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM runs
        WHERE run_type IN ('weekly', 'monthly')
          AND status = 'success'
          AND started_at >= NOW() - INTERVAL '1 day' * %s
        """,
        (days,),
    )
    wm_row = cur.fetchone()
    has_recent_weekly_monthly = int(wm_row[0] or 0) > 0 if wm_row else False

    items: list[dict] = []

    if latest_run:
        latest_status = (latest_run.get("status") or "").lower()
        latest_finished = latest_run.get("finished_at")
        # A row inserted but not finished yet has no finished_at and non-success status — still running
        run_failed = latest_finished is not None and latest_status not in ("success",)

        score = 40
        if run_failed:
            score += 40
        if not has_recent_weekly_monthly:
            score += 20
        score = min(100, score)

        if run_failed or not has_recent_weekly_monthly:
            detail_parts: list[str] = []
            if run_failed:
                detail_parts.append(f"latest run has status '{latest_status}'")
            if not has_recent_weekly_monthly:
                detail_parts.append(f"no successful weekly or monthly run in the last {days} days")
            detail = "Data quality warrants review: " + " and ".join(detail_parts) + "." if detail_parts else "Data quality warrants review."
            items.append({
                "id": "data-quality-run-health",
                "type": "data_quality_review",
                "severity": _queue_severity_label(score),
                "severity_score": score,
                "title": "Data quality warrants review",
                "detail": detail,
                "entity_label": "run health",
                "entity_type": "data_quality",
                "campaign_name": None,
                "source": "runs table",
                "evidence": {
                    "latest_run_status": latest_status,
                    "latest_run_type": latest_run.get("run_type"),
                    "has_recent_weekly_monthly": has_recent_weekly_monthly,
                },
                "primary_link": {
                    "page": "scheduler",
                    "action": "navigate",
                },
            })

    return items


@app.get("/api/action-queue")
def api_action_queue(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return a ranked human-review queue based on campaign, waste, geo, keyword, and data signals.

    Requires auth. Read-only. No write operations. No external API calls.
    Severity is display-only — items are human-review prompts, not automated recommendations.
    Phase 1 read-only — no writes to Google Ads or HubSpot.
    """
    days = _clamp_days(days)

    _safe_empty: dict[str, Any] = {
        "days": days,
        "items": [],
        "summary": {"total": 0, "high": 0, "medium": 0, "low": 0},
        "data_quality": {"status": "db_unavailable"},
        "db_unavailable": True,
    }

    # Load UI thresholds (best-effort; fall back to defaults)
    thresholds = _load_ui_thresholds()
    high_junk_pct = float((thresholds.get("junk_rate") or {}).get("high_pct") or _QUEUE_JUNK_HIGH_PCT_DEFAULT)
    high_spend_usd = float((thresholds.get("spend") or {}).get("high_spend_usd") or _QUEUE_HIGH_SPEND_USD_DEFAULT)

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_empty

            with conn.cursor() as cur:
                campaign_items = _build_campaign_queue_items(cur, days, high_junk_pct, high_spend_usd)
                waste_items    = _build_waste_queue_items(cur, days, high_spend_usd)
                geo_items      = _build_geo_queue_items(cur, days, high_junk_pct)
                keyword_items  = _build_keyword_queue_items(cur, days, high_spend_usd)
                dq_items       = _build_data_quality_items(cur, days)

    except Exception as exc:  # noqa: BLE001
        log.error("[api/action-queue] database error: %s", exc, exc_info=True)
        return _safe_empty

    all_items = campaign_items + waste_items + geo_items + keyword_items + dq_items

    # Sort: highest score first, then type, then entity_label for stable ordering
    all_items.sort(key=lambda x: (-x["severity_score"], x["type"], x["entity_label"]))

    # Cap at 30 items
    all_items = all_items[:_QUEUE_MAX_ITEMS]

    n_high   = sum(1 for i in all_items if i["severity"] == "high")
    n_medium = sum(1 for i in all_items if i["severity"] == "medium")
    n_low    = sum(1 for i in all_items if i["severity"] == "low")

    return {
        "days": days,
        "items": all_items,
        "summary": {
            "total":  len(all_items),
            "high":   n_high,
            "medium": n_medium,
            "low":    n_low,
        },
        "data_quality": {"status": "ok"},
    }


# ---------------------------------------------------------------------------
# Dataset freshness endpoint — read-only, auth required. (PR-ADS-039, PR-ADS-067)
# ---------------------------------------------------------------------------

# Known source/dataset pairs — returned as placeholders when sync_state is empty.
_KNOWN_DATASETS: list[tuple[str, str]] = [
    ("windsor", "campaigns"),
    ("windsor", "keywords"),
    ("windsor", "search_terms"),
    ("windsor", "geo"),
    ("hubspot", "contacts"),
    ("hubspot", "deals"),
    ("gclid",   "matches"),
    ("gclid",   "coverage_snapshots"),
    ("analysis", "waste_terms"),
    ("computed", "ngrams"),
    ("analysis", "historical_intelligence"),
]
_SAFE_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@app.get("/api/datasets/freshness")
def api_datasets_freshness(user: dict = Depends(require_auth), days: int = 60) -> dict[str, Any]:
    """Return per-dataset sync state / watermark from the sync_state table.

    Auth required. Read-only. No live fetch, no sync execution, no external calls.
    Phase 1 read-only — no writes to Google Ads, HubSpot, or any external system.
    Source: sync_state table (PR-ADS-039).
    Enhanced with canonical freshness semantics (PR-ADS-067).
    """
    from services.freshness_service import (  # noqa: PLC0415
        DATASET_FRESHNESS_CONFIG,
        BLOCKING_STATES,
        CanonicalFreshnessStatus,
        compute_canonical_freshness,
    )
    days = max(1, min(90, int(days)))

    _safe_empty: dict[str, Any] = {
        "datasets": [],
        "summary": {"total": 0, "success": 0, "failed": 0, "running": 0, "unknown": 0},
        "db_unavailable": True,
    }

    from psycopg2 import sql as _psql  # noqa: PLC0415

    from db.connection import get_conn  # noqa: PLC0415
    row_counts: dict[str, int | None] = {}
    latest_batch_info: dict[tuple[str, str], dict] = {}

    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_empty

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        source,
                        dataset,
                        status,
                        last_successful_sync_at,
                        last_source_date,
                        last_batch_id,
                        error_message,
                        updated_at
                    FROM sync_state
                    ORDER BY source, dataset
                    """,
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]

                window_start = date.today() - timedelta(days=days)
                _count_cache: dict[tuple[str, str], int | None] = {}

                for cfg_key, cfg in DATASET_FRESHNESS_CONFIG.items():
                    table_name = str(cfg.get("table") or "")
                    date_column = str(cfg.get("date_column") or "")
                    if not table_name or not date_column:
                        continue
                    if not (_SAFE_SQL_IDENTIFIER_RE.match(table_name) and _SAFE_SQL_IDENTIFIER_RE.match(date_column)):
                        log.warning(
                            "[api/datasets/freshness] invalid identifier for dataset=%s table=%s date_column=%s",
                            cfg_key,
                            table_name,
                            date_column,
                        )
                        continue

                    cache_key = (table_name, date_column)
                    if cache_key in _count_cache:
                        row_counts[cfg_key] = _count_cache[cache_key]
                        continue

                    query = _psql.SQL("SELECT COUNT(*) FROM {} WHERE {} >= %s").format(
                        _psql.Identifier(table_name),
                        _psql.Identifier(date_column),
                    )
                    try:
                        cur.execute(query, (window_start,))
                        result = cur.fetchone()
                        count = int(result[0]) if result and result[0] is not None else 0
                        _count_cache[cache_key] = count
                        row_counts[cfg_key] = count
                    except Exception as exc:  # noqa: BLE001
                        _count_cache[cache_key] = None
                        row_counts[cfg_key] = None
                        log.warning(
                            "[api/datasets/freshness] row count query failed for dataset=%s table=%s: %s",
                            cfg_key,
                            table_name,
                            exc,
                        )

                try:
                    cur.execute("""
                        SELECT DISTINCT ON (source, dataset)
                            source, dataset, status, row_count
                        FROM sync_batches
                        ORDER BY source, dataset, started_at DESC
                    """)
                    bcols = [d[0] for d in cur.description]
                    for brow in cur.fetchall():
                        br = dict(zip(bcols, brow))
                        latest_batch_info[(br["source"], br["dataset"])] = {
                            "status": br["status"],
                            "row_count": br["row_count"],
                        }
                except Exception as exc:  # noqa: BLE001
                    log.warning("[api/datasets/freshness] latest sync_batch query failed: %s", exc)

    except Exception as exc:  # noqa: BLE001
        log.error("[api/datasets/freshness] database error: %s", exc, exc_info=True)
        return _safe_empty

    # Build a lookup of rows that exist in sync_state
    db_map: dict[tuple[str, str], dict] = {}
    for row in rows:
        r = dict(zip(cols, row))
        key = (r["source"], r["dataset"])
        db_map[key] = {
            "dataset_key":             f"{r['source']}/{r['dataset']}",
            "source":                  r["source"],
            "dataset":                 r["dataset"],
            "status":                  r["status"],
            "last_successful_sync_at": r["last_successful_sync_at"].isoformat() if r["last_successful_sync_at"] else None,
            "last_source_date":        str(r["last_source_date"]) if r["last_source_date"] else None,
            "last_batch_id":           r["last_batch_id"],
            "error_message":           r["error_message"],
            "updated_at":              r["updated_at"].isoformat() if r["updated_at"] else None,
        }

    # ── Build canonical freshness config lookup ────────────────────────────
    # Map (source, dataset) -> config key for reverse lookup
    _cfg_by_source_dataset: dict[tuple[str, str], str] = {}
    for cfg_key, cfg in DATASET_FRESHNESS_CONFIG.items():
        src = cfg["source"]
        ds = cfg["dataset"]
        _cfg_by_source_dataset[(src, ds)] = cfg_key

    # Merge known dataset list with DB rows; fill missing entries as 'unknown'
    datasets: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for source, dataset in _KNOWN_DATASETS:
        key = (source, dataset)
        seen.add(key)
        if key in db_map:
            datasets.append(db_map[key])
        else:
            datasets.append({
                "dataset_key":             f"{source}/{dataset}",
                "source":                  source,
                "dataset":                 dataset,
                "status":                  "unknown",
                "last_successful_sync_at": None,
                "last_source_date":        None,
                "last_batch_id":           None,
                "error_message":           None,
                "updated_at":              None,
            })

    # Append any extra rows in sync_state that are not in the known list
    for key, row in db_map.items():
        if key not in seen:
            datasets.append(row)

    # ── Compute canonical freshness for each dataset (PR-ADS-067) ──────────
    # First pass: compute non-dependent datasets to build canonical_status_map
    canonical_status_map: dict[str, str] = {}
    for d in datasets:
        src = d.get("source", "")
        ds = d.get("dataset", "")
        cfg_key = _cfg_by_source_dataset.get((src, ds))
        cfg = DATASET_FRESHNESS_CONFIG.get(cfg_key, {}) if cfg_key else {}
        stale_days = cfg.get("stale_threshold_days", 8)
        depends_on = cfg.get("depends_on", [])

        # Get row count for this configured dataset
        rcount = row_counts.get(cfg_key, None) if cfg_key else None

        # Parse dates
        last_sync_at = None
        if d.get("last_successful_sync_at"):
            try:
                last_sync_at = datetime.fromisoformat(d["last_successful_sync_at"])
            except (ValueError, TypeError):
                pass

        latest_src_date = None
        if d.get("last_source_date"):
            try:
                from datetime import date as _d
                latest_src_date = _d.fromisoformat(d["last_source_date"])
            except (ValueError, TypeError):
                pass

        # Get batch info
        batch_info = latest_batch_info.get((src, ds), {})
        batch_status = batch_info.get("status")
        batch_row_count = batch_info.get("row_count")
        sync_status_for_canonical = d.get("status")

        # Placeholder records preserve legacy status="unknown" for response, but canonical
        # freshness should treat missing sync metadata as not_run.
        if (
            sync_status_for_canonical == "unknown"
            and d.get("last_successful_sync_at") is None
            and d.get("last_source_date") is None
            and d.get("last_batch_id") is None
            and d.get("error_message") is None
        ):
            sync_status_for_canonical = None

        # Skip dependency logic in first pass (handled below)
        if not depends_on:
            verdict = compute_canonical_freshness(
                dataset=cfg_key or ds,
                rows_in_window=rcount,
                latest_source_date=latest_src_date,
                sync_status=sync_status_for_canonical,
                latest_batch_status=batch_status,
                latest_batch_row_count=batch_row_count,
                last_successful_sync_at=last_sync_at,
                stale_threshold_days=stale_days,
                dependency_status=None,
            )
        else:
            verdict = None  # Placeholder, computed in second pass

        d["_cfg_key"] = cfg_key
        d["_verdict"] = verdict
        d["_depends_on"] = depends_on
        d["_stale_days"] = stale_days
        d["_rcount"] = rcount
        d["_batch_status"] = batch_status
        d["_batch_row_count"] = batch_row_count
        d["_last_sync_at"] = last_sync_at
        d["_latest_src_date"] = latest_src_date
        d["_sync_status_for_canonical"] = sync_status_for_canonical

        if verdict:
            canonical_status_map[cfg_key or ds] = verdict["canonical_status"]

    # Second pass: compute dependent datasets
    for d in datasets:
        if d["_verdict"] is None:
            depends_on = d["_depends_on"]
            # Check dependency status
            dep_status = None
            for dep in depends_on:
                dep_st = canonical_status_map.get(dep)
                if dep_st and dep_st in BLOCKING_STATES:
                    dep_status = dep_st
                    break

            verdict = compute_canonical_freshness(
                dataset=d["_cfg_key"] or d.get("dataset", ""),
                rows_in_window=d["_rcount"],
                latest_source_date=d["_latest_src_date"],
                sync_status=d["_sync_status_for_canonical"],
                latest_batch_status=d["_batch_status"],
                latest_batch_row_count=d["_batch_row_count"],
                last_successful_sync_at=d["_last_sync_at"],
                stale_threshold_days=d["_stale_days"],
                dependency_status=dep_status,
            )
            d["_verdict"] = verdict
            canonical_status_map[d["_cfg_key"] or d.get("dataset", "")] = verdict["canonical_status"]

    # Enrich each dataset record with canonical fields
    for d in datasets:
        verdict = d.pop("_verdict", {}) or {}
        cfg_key = d.pop("_cfg_key", None)
        depends_on = d.pop("_depends_on", [])
        stale_days = d.pop("_stale_days", 8)
        rcount = d.pop("_rcount", None)
        batch_status = d.pop("_batch_status", None)
        batch_row_count = d.pop("_batch_row_count", 0)
        d.pop("_last_sync_at", None)
        d.pop("_latest_src_date", None)
        d.pop("_sync_status_for_canonical", None)

        d["canonical_status"] = verdict.get("canonical_status", CanonicalFreshnessStatus.UNKNOWN)
        d["severity"] = verdict.get("severity", "neutral")
        d["rows_in_window"] = rcount
        d["latest_source_date"] = d.get("last_source_date")
        d["last_batch_row_count"] = batch_row_count
        d["stale_threshold_days"] = stale_days
        d["depends_on"] = depends_on
        d["dependency_status"] = None
        if depends_on:
            for dep in depends_on:
                dep_st = canonical_status_map.get(dep)
                if dep_st and dep_st in BLOCKING_STATES:
                    d["dependency_status"] = dep_st
                    break
        d["reason"] = verdict.get("reason", "")
        d["next_action"] = verdict.get("next_action", "")

    # Build summary counts
    status_counts: dict[str, int] = {"success": 0, "failed": 0, "running": 0, "unknown": 0}
    canonical_counts: dict[str, int] = {}
    for d in datasets:
        s = d.get("status") or "unknown"
        if s in status_counts:
            status_counts[s] += 1
        else:
            status_counts["unknown"] += 1
        cs = d.get("canonical_status", "unknown")
        canonical_counts[cs] = canonical_counts.get(cs, 0) + 1

    return {
        "datasets": datasets,
        "summary": {
            "total":   len(datasets),
            "success": status_counts["success"],
            "failed":  status_counts["failed"],
            "running": status_counts["running"],
            "unknown": status_counts["unknown"],
        },
        "canonical_summary": canonical_counts,
        "db_unavailable": False,
    }


# ---------------------------------------------------------------------------
# Search Terms endpoint — cursor-paginated, read-only, auth required. (PR-ADS-040)
# ---------------------------------------------------------------------------

_SEARCH_TERMS_MAX_LIMIT   = 500
_SEARCH_TERMS_DEFAULT_DAYS = 14
_SEARCH_TERMS_MAX_DAYS    = 90

_SEARCH_TERMS_DATA_QUALITY_NOTE = (
    "is_flagged_waste is tri-state: null = not analyzed, true = flagged waste, "
    "false = analyzed clean. Current Windsor connector is confirmed up to last_14d "
    "search-term window unless plan supports more."
)

# Allowed waste_state values and their canonical form.
# Note: aliases use both American ("analyzed_clean") and British ("unanalysed") spellings
# intentionally to match the mixed spelling conventions in the existing codebase (PR-ADS-040).
_WASTE_STATE_ALIASES: dict[str, str] = {
    "all":           "all",
    "flagged":       "flagged",
    "waste":         "flagged",
    "clean":         "clean",
    "analyzed_clean": "clean",
    "unanalyzed":    "unanalyzed",
    "unanalysed":    "unanalyzed",
}


def _resolve_waste_state_param(waste_state: str | None, waste_only: bool = False) -> str:
    """Resolve the effective waste-state filter from request parameters.

    Precedence: waste_state (if provided) > waste_only flag > default 'all'.
    Raises HTTPException(400) on unrecognised waste_state values.
    """
    if waste_state is not None:
        effective = _WASTE_STATE_ALIASES.get(waste_state.lower())
        if effective is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid waste_state. Allowed values: all, flagged, clean, unanalyzed.",
            )
        return effective
    if waste_only:
        return "flagged"
    return "all"


def _encode_keyset_cursor(payload: dict) -> str:
    """Encode a keyset cursor as URL-safe base64 JSON (no padding)."""
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_keyset_cursor(token: str) -> dict:
    """Decode a base64 JSON cursor payload or raise ValueError."""
    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        return payload
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


def _encode_search_terms_cursor(source_date: date, row_id: int) -> str:
    return _encode_keyset_cursor({
        "source_date": str(source_date),
        "id": int(row_id),
    })


def _decode_search_terms_cursor(token: str):
    """Decode a validated search-terms cursor."""
    try:
        payload = _decode_keyset_cursor(token)
        source_date = date.fromisoformat(str(payload["source_date"]))
        row_id = int(payload["id"])

        if row_id <= 0:
            raise ValueError("cursor id must be positive")

        return source_date, row_id
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


@app.get("/api/search-terms")
def api_search_terms(
    user: dict = Depends(require_auth),
    days: int = Query(
        default=_SEARCH_TERMS_DEFAULT_DAYS,
        description="Number of days to look back (1–90)",
    ),
    campaign: str = Query(default=None, description="Filter by exact campaign_name"),
    match_type: str = Query(default=None, description="Filter by match_type (contains, case-insensitive)"),
    q: str = Query(default=None, description="Case-insensitive contains search on search_term"),
    waste_state: str = Query(
        default=None,
        description=(
            "Filter by analysis state. "
            "Allowed: all, flagged, clean, unanalyzed. "
            "Aliases: waste=flagged, analyzed_clean=clean, unanalysed=unanalyzed. "
            "Default: all."
        ),
    ),
    waste_only: bool = Query(default=False, description="Deprecated. If true, equivalent to waste_state=flagged. Ignored when waste_state is provided."),
    min_spend: float = Query(default=None, description="Minimum spend_usd threshold"),
    limit: int = Query(default=100, description="Page size (1–500)"),
    cursor: str = Query(default=None, description="Opaque pagination cursor from previous response"),
) -> dict[str, Any]:
    """Return paginated search-term fact rows for the last N days.

    Uses cursor/keyset pagination on (source_date DESC, id DESC).
    Auth required. Read-only. No writes to Google Ads or HubSpot.
    Source: search_terms table (PR-ADS-040).
    """
    # ── Clamp / validate params ────────────────────────────────────────────
    days  = max(1, min(_SEARCH_TERMS_MAX_DAYS, days))
    limit = max(1, min(_SEARCH_TERMS_MAX_LIMIT, limit))

    # ── Resolve effective waste state ──────────────────────────────────────
    effective_state = _resolve_waste_state_param(waste_state, waste_only)

    _safe_empty: dict[str, Any] = {
        "days": days,
        "filters": {
            "waste_state": effective_state,
        },
        "rows": [],
        "pagination": {
            "limit":       limit,
            "next_cursor": None,
            "has_more":    False,
        },
        "data_quality": {
            "source":  "windsor",
            "dataset": "search_terms",
            "status":  "db_unavailable",
        },
        "db_unavailable": True,
    }

    # ── Decode cursor ─────────────────────────────────────────────────────
    cursor_date: date | None = None
    cursor_id:   int | None  = None
    if cursor:
        try:
            cursor_date, cursor_id = _decode_search_terms_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Campaign name normalisation (match stored canonical lowercase) ─────
    campaign_key: str | None = None
    if campaign:
        from db.writers import _canonicalise_campaign_name  # noqa: PLC0415
        campaign_key = _canonicalise_campaign_name(campaign.strip().lower())

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_empty

            with conn.cursor() as cur:
                # ── Build base WHERE clauses (window + filters; excludes cursor) ──
                base_conditions: list[str] = [
                    "source_date >= NOW() - INTERVAL '1 day' * %s",
                ]
                base_params: list[Any] = [days]

                if campaign_key is not None:
                    base_conditions.append("campaign_name = %s")
                    base_params.append(campaign_key)

                if match_type:
                    base_conditions.append("match_type ILIKE %s")
                    base_params.append(f"%{match_type.strip()}%")

                if q:
                    base_conditions.append("search_term ILIKE %s")
                    base_params.append(f"%{q.strip()}%")

                if effective_state == "flagged":
                    base_conditions.append("is_flagged_waste IS TRUE")
                elif effective_state == "clean":
                    base_conditions.append("is_flagged_waste IS FALSE")
                elif effective_state == "unanalyzed":
                    base_conditions.append("is_flagged_waste IS NULL")
                # "all" — no additional condition

                if min_spend is not None:
                    base_conditions.append("spend_usd >= %s")
                    base_params.append(min_spend)

                base_where_sql = " AND ".join(base_conditions)

                # ── Count full filtered window (not current page) ────────────
                cur.execute(
                    "SELECT COUNT(*), MAX(source_date) FROM search_terms WHERE " + base_where_sql,
                    base_params,
                )
                count_row = cur.fetchone()
                total_rows_in_window = int(count_row[0]) if count_row else 0
                latest_source_date = str(count_row[1]) if count_row and count_row[1] else None

                # ── Build paginated WHERE clauses (base + cursor) ─────────────
                conditions = list(base_conditions)
                params = list(base_params)

                if cursor_date is not None and cursor_id is not None:
                    conditions.append(
                        "(source_date < %s OR (source_date = %s AND id < %s))"
                    )
                    params += [cursor_date, cursor_date, cursor_id]

                where_sql = " AND ".join(conditions)

                # Fetch limit+1 to detect whether more rows exist
                fetch_limit = limit + 1
                params.append(fetch_limit)

                # All user-supplied values are passed via parameterized %s placeholders.
                # where_sql is built exclusively from the static string literals in
                # `conditions` above — no user input is ever interpolated into the SQL text.
                query = (
                    "SELECT"
                    " id, source_date, campaign_name, campaign_id,"
                    " ad_group, keyword, match_type, search_term,"
                    " spend_usd, clicks, impressions, conversions,"
                    " is_flagged_waste, junk_category, matched_pattern, updated_at"
                    " FROM search_terms"
                    " WHERE " + where_sql +
                    " ORDER BY source_date DESC, id DESC"
                    " LIMIT %s"
                )
                cur.execute(query, params)
                raw_rows = cur.fetchall()
                cols = [d[0] for d in cur.description]

    except Exception as exc:  # noqa: BLE001
        log.error("[api/search-terms] database error: %s", exc, exc_info=True)
        return _safe_empty

    has_more = len(raw_rows) > limit
    page_rows = raw_rows[:limit]

    out: list[dict] = []
    for row in page_rows:
        r = dict(zip(cols, row))
        out.append({
            "id":              r["id"],
            "source_date":     str(r["source_date"]) if r["source_date"] else None,
            "campaign_name":   r["campaign_name"],
            "campaign_id":     r["campaign_id"],
            "ad_group":        r["ad_group"],
            "keyword":         r["keyword"],
            "match_type":      r["match_type"],
            "search_term":     r["search_term"],
            "spend_usd":       round(float(r["spend_usd"]), 2) if r["spend_usd"] is not None else 0.0,
            "clicks":          int(r["clicks"] or 0),
            "impressions":     int(r["impressions"] or 0),
            "conversions":     round(float(r["conversions"]), 2) if r["conversions"] is not None else 0.0,
            "is_flagged_waste": r["is_flagged_waste"],  # tri-state: null | true | false
            "junk_category":   r["junk_category"],
            "matched_pattern": r["matched_pattern"],
            "last_seen_at":    r["updated_at"].isoformat() if r["updated_at"] else None,
        })

    # Build next cursor from last row on this page
    next_cursor: str | None = None
    if has_more and page_rows:
        last = dict(zip(cols, page_rows[-1]))
        next_cursor = _encode_search_terms_cursor(last["source_date"], int(last["id"]))

    # PR-ADS-065: Enhanced data_quality block
    is_empty = len(out) == 0
    data_quality: dict[str, Any] = {
        "source": "windsor",
        "dataset": "search_terms",
        "table": "search_terms",
        "days": days,
        "rows_in_window": total_rows_in_window,
        "total_rows_in_window": total_rows_in_window,
        "rows_returned": len(out),
        "is_empty": is_empty,
        "note": _SEARCH_TERMS_DATA_QUALITY_NOTE,
        "warning": (
            "No search-term rows found for this window. "
            "Check Windsor pull and sync_batches."
            if is_empty else None
        ),
    }
    data_quality["latest_source_date"] = latest_source_date

    return {
        "days": days,
        "filters": {
            "waste_state": effective_state,
        },
        "rows": out,
        "pagination": {
            "limit":       limit,
            "next_cursor": next_cursor,
            "has_more":    has_more,
        },
        "data_quality": data_quality,
    }


# ---------------------------------------------------------------------------
# Search Terms summary endpoint — aggregate, read-only, auth required. (PR-ADS-053)
# ---------------------------------------------------------------------------

_SEARCH_TERMS_SUMMARY_DATA_QUALITY_NOTE = (
    "Summary is computed from stored search_terms rows in PostgreSQL. "
    "Google conversions are platform conversions, not HubSpot SQLs."
)


@app.get("/api/search-terms/summary")
def api_search_terms_summary(
    user: dict = Depends(require_auth),
    days: int = Query(
        default=_SEARCH_TERMS_DEFAULT_DAYS,
        description="Number of days to look back (1–90)",
    ),
    campaign: str = Query(default=None, description="Filter by exact campaign_name"),
    match_type: str = Query(default=None, description="Filter by match_type (contains, case-insensitive)"),
    q: str = Query(default=None, description="Case-insensitive contains search on search_term"),
    waste_state: str = Query(
        default=None,
        description=(
            "Filter by analysis state. "
            "Allowed: all, flagged, clean, unanalyzed. "
            "Aliases: waste=flagged, analyzed_clean=clean, unanalysed=unanalyzed. "
            "Default: all. "
            "The summary object reflects this filter. "
            "The analysis_state breakdown always covers all states within base filters."
        ),
    ),
    waste_only: bool = Query(default=False, description="Deprecated. If true, equivalent to waste_state=flagged. Ignored when waste_state is provided."),
    min_spend: float = Query(default=None, description="Minimum spend_usd threshold"),
) -> dict[str, Any]:
    """Return aggregate summary counts for the selected filter/window.

    The `summary` object respects all filters including waste_state.
    The `analysis_state` breakdown respects base filters but ignores the selected
    waste_state so callers can see the full flagged/clean/unanalyzed distribution
    within the selected campaign/query/match/spend/date scope.

    No cursor. No pagination. Auth required. Read-only.
    Source: search_terms table (PR-ADS-040).
    Does not write to Google Ads or HubSpot.
    """
    # ── Clamp / validate params ────────────────────────────────────────────
    days = max(1, min(_SEARCH_TERMS_MAX_DAYS, days))

    # ── Resolve effective waste state ──────────────────────────────────────
    effective_state = _resolve_waste_state_param(waste_state, waste_only)

    _zero_summary: dict[str, Any] = {
        "total_terms": 0,
        "unique_search_terms": 0,
        "total_spend_usd": 0.0,
        "total_clicks": 0,
        "total_impressions": 0,
        "google_conversions": 0.0,
        "avg_cpc_usd": None,
        "ctr_pct": None,
        "google_conversion_rate_pct": None,
    }
    _zero_state: dict[str, Any] = {
        "flagged":    {"rows": 0, "spend_usd": 0.0},
        "clean":      {"rows": 0, "spend_usd": 0.0},
        "unanalyzed": {"rows": 0, "spend_usd": 0.0},
    }

    _safe_empty: dict[str, Any] = {
        "days": days,
        "filters": {
            "waste_state": effective_state,
        },
        "summary": _zero_summary,
        "analysis_state": _zero_state,
        "data_quality": {
            "source":  "windsor",
            "dataset": "search_terms",
            "status":  "db_unavailable",
        },
        "db_unavailable": True,
    }

    # ── Campaign name normalisation ────────────────────────────────────────
    campaign_key: str | None = None
    if campaign:
        from db.writers import _canonicalise_campaign_name  # noqa: PLC0415
        campaign_key = _canonicalise_campaign_name(campaign.strip().lower())

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_empty

            with conn.cursor() as cur:
                # ── Build base WHERE clauses (no waste_state) ─────────────
                # Used for the analysis_state breakdown so all three buckets
                # are always visible even when the user filters by one state.
                base_conditions: list[str] = [
                    "source_date >= NOW() - INTERVAL '1 day' * %s",
                ]
                base_params: list[Any] = [days]

                if campaign_key is not None:
                    base_conditions.append("campaign_name = %s")
                    base_params.append(campaign_key)

                if match_type:
                    base_conditions.append("match_type ILIKE %s")
                    base_params.append(f"%{match_type.strip()}%")

                if q:
                    base_conditions.append("search_term ILIKE %s")
                    base_params.append(f"%{q.strip()}%")

                if min_spend is not None:
                    base_conditions.append("spend_usd >= %s")
                    base_params.append(min_spend)

                base_where_sql = " AND ".join(base_conditions)

                # ── Build filtered WHERE clauses (with waste_state) ────────
                # Used for the top-line summary so it honours the selected state.
                filtered_conditions = list(base_conditions)
                filtered_params = list(base_params)

                if effective_state == "flagged":
                    filtered_conditions.append("is_flagged_waste IS TRUE")
                elif effective_state == "clean":
                    filtered_conditions.append("is_flagged_waste IS FALSE")
                elif effective_state == "unanalyzed":
                    filtered_conditions.append("is_flagged_waste IS NULL")
                # "all" — no additional condition

                filtered_where_sql = " AND ".join(filtered_conditions)

                # ── Query 1: top-line summary (filtered scope) ─────────────
                # All user-supplied values go through parameterised %s.
                # where_sql is assembled exclusively from static string literals.
                cur.execute(
                    "SELECT"
                    "  COUNT(*) AS total_terms,"
                    "  COUNT(DISTINCT search_term) AS unique_search_terms,"
                    "  COALESCE(SUM(spend_usd), 0) AS total_spend_usd,"
                    "  COALESCE(SUM(clicks), 0) AS total_clicks,"
                    "  COALESCE(SUM(impressions), 0) AS total_impressions,"
                    "  COALESCE(SUM(conversions), 0) AS google_conversions"
                    " FROM search_terms"
                    " WHERE " + filtered_where_sql,
                    filtered_params,
                )
                summary_row = cur.fetchone()
                if summary_row is None:
                    return _safe_empty
                (
                    total_terms, unique_terms, total_spend,
                    total_clicks, total_impressions, google_conversions,
                ) = summary_row

                total_terms       = int(total_terms or 0)
                unique_terms      = int(unique_terms or 0)
                total_clicks      = int(total_clicks or 0)
                total_impressions = int(total_impressions or 0)
                # Keep raw floats for rate calculations; round only at serialisation.
                raw_spend         = float(total_spend or 0)
                raw_conversions   = float(google_conversions or 0)

                avg_cpc_usd = round(raw_spend / total_clicks, 4) if total_clicks > 0 else None
                ctr_pct = round(total_clicks / total_impressions * 100, 4) if total_impressions > 0 else None
                google_conv_rate = round(raw_conversions / total_clicks * 100, 4) if total_clicks > 0 else None

                # ── Query 2: analysis_state breakdown (base scope only) ────
                cur.execute(
                    "SELECT"
                    "  is_flagged_waste,"
                    "  COUNT(*) AS rows,"
                    "  COALESCE(SUM(spend_usd), 0) AS spend_usd"
                    " FROM search_terms"
                    " WHERE " + base_where_sql +
                    " GROUP BY is_flagged_waste",
                    base_params,
                )
                state_rows = cur.fetchall()

    except Exception as exc:  # noqa: BLE001
        log.error("[api/search-terms/summary] database error: %s", exc, exc_info=True)
        return _safe_empty

    # Map tri-state rows onto the three named buckets
    analysis_state: dict[str, Any] = {
        "flagged":    {"rows": 0, "spend_usd": 0.0},
        "clean":      {"rows": 0, "spend_usd": 0.0},
        "unanalyzed": {"rows": 0, "spend_usd": 0.0},
    }
    for flagged_val, row_count, spend_val in state_rows:
        row_count = int(row_count or 0)
        spend_val = round(float(spend_val or 0), 2)
        if flagged_val is True:
            analysis_state["flagged"] = {"rows": row_count, "spend_usd": spend_val}
        elif flagged_val is False:
            analysis_state["clean"] = {"rows": row_count, "spend_usd": spend_val}
        else:
            analysis_state["unanalyzed"] = {"rows": row_count, "spend_usd": spend_val}

    filters_out: dict[str, Any] = {"waste_state": effective_state}
    if campaign_key is not None:
        filters_out["campaign"] = campaign_key
    if match_type:
        filters_out["match_type"] = match_type.strip()
    if q:
        filters_out["q"] = q.strip()
    if min_spend is not None:
        filters_out["min_spend"] = min_spend

    # PR-ADS-065: Enhanced data_quality for summary
    is_empty = total_terms == 0
    data_quality_out: dict[str, Any] = {
        "source": "windsor",
        "dataset": "search_terms",
        "table": "search_terms",
        "days": days,
        "rows_in_window": total_terms,
        "total_rows_in_window": total_terms,
        "rows_returned": total_terms,
        "is_empty": is_empty,
        "note": _SEARCH_TERMS_SUMMARY_DATA_QUALITY_NOTE,
        "warning": (
            "No search-term rows found for this window. "
            "Check Windsor pull and sync_batches."
            if is_empty else None
        ),
    }

    return {
        "days": days,
        "filters": filters_out,
        "summary": {
            "total_terms":                   total_terms,
            "unique_search_terms":           unique_terms,
            "total_spend_usd":               round(raw_spend, 2),
            "total_clicks":                  total_clicks,
            "total_impressions":             total_impressions,
            "google_conversions":            round(raw_conversions, 2),
            "avg_cpc_usd":                   avg_cpc_usd,
            "ctr_pct":                       ctr_pct,
            "google_conversion_rate_pct":    google_conv_rate,
        },
        "analysis_state": analysis_state,
        "data_quality": data_quality_out,
        "db_unavailable": False,
    }


# ---------------------------------------------------------------------------
# Search Terms N-Gram endpoint — aggregated, read-only, auth required. (PR-ADS-055)
# ---------------------------------------------------------------------------

_NGRAMS_DEFAULT_DAYS  = 14
_NGRAMS_MAX_DAYS      = 30
_NGRAMS_DEFAULT_LIMIT = 100
_NGRAMS_MAX_LIMIT     = 250
_NGRAMS_SOURCE_ROW_CAP = 10_000

_NGRAMS_DATA_QUALITY_NOTE = (
    "N-gram analysis is read-only. "
    "Google conversions are platform conversions, not HubSpot SQLs. "
    "No negative keyword candidates are created."
)


def _parse_n_param(raw: str) -> list[int]:
    """Parse the ``n`` query parameter into a validated list of n-gram lengths.

    Accepted inputs: "1", "2", "3", or comma-separated combinations thereof.
    Raises HTTPException(400) for invalid or out-of-range values.
    """
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    result: list[int] = []
    for part in parts:
        if not part.isdigit():
            raise HTTPException(
                status_code=400,
                detail="Invalid n. Allowed values: 1, 2, 3.",
            )
        val = int(part)
        if val not in (1, 2, 3):
            raise HTTPException(
                status_code=400,
                detail="Invalid n. Allowed values: 1, 2, 3.",
            )
        if val not in result:
            result.append(val)
    if not result:
        raise HTTPException(
            status_code=400,
            detail="Invalid n. Allowed values: 1, 2, 3.",
        )
    return sorted(result)


@app.get("/api/search-terms/ngrams")
def api_search_terms_ngrams(
    user: dict = Depends(require_auth),
    days: int = Query(
        default=_NGRAMS_DEFAULT_DAYS,
        description="Number of days to look back (1–30 for prototype)",
    ),
    campaign: str = Query(default=None, description="Filter by exact campaign_name"),
    match_type: str = Query(default=None, description="Filter by match_type (contains, case-insensitive)"),
    waste_state: str = Query(
        default=None,
        description=(
            "Filter by analysis state. "
            "Allowed: all, flagged, clean, unanalyzed. "
            "Aliases: waste=flagged, analyzed_clean=clean, unanalysed=unanalyzed. "
            "Default: all."
        ),
    ),
    q: str = Query(default=None, description="Case-insensitive contains search on search_term (pre-tokenization filter)"),
    min_spend: float = Query(default=0.0, description="Row-level minimum spend_usd filter"),
    n: str = Query(default="1,2,3", description="Comma-separated n-gram lengths. Allowed: 1, 2, 3."),
    limit: int = Query(default=_NGRAMS_DEFAULT_LIMIT, description="Max n-gram rows to return (1–250)"),
) -> dict[str, Any]:
    """Return aggregated n-gram metrics over stored search_terms rows.

    Auth required. Read-only. No writes to Google Ads or HubSpot.
    Source: search_terms table.
    Does not return scoring, attention_status, recommendations, or
    negative keyword candidates.
    """
    # ── Clamp / validate params ────────────────────────────────────────────
    days  = max(1, min(_NGRAMS_MAX_DAYS, days))
    limit = max(1, min(_NGRAMS_MAX_LIMIT, limit))

    # ── Validate n ────────────────────────────────────────────────────────
    n_list = _parse_n_param(n)
    n_set  = set(n_list)

    # ── Validate min_spend ────────────────────────────────────────────────
    import math as _math  # noqa: PLC0415
    if _math.isnan(min_spend) or min_spend < 0:
        raise HTTPException(
            status_code=400,
            detail="min_spend must be greater than or equal to 0.",
        )

    # ── Resolve effective waste state ──────────────────────────────────────
    effective_state = _resolve_waste_state_param(waste_state)

    # ── Campaign name normalisation ────────────────────────────────────────
    campaign_key: str | None = None
    if campaign:
        from db.writers import _canonicalise_campaign_name  # noqa: PLC0415
        campaign_key = _canonicalise_campaign_name(campaign.strip().lower())

    # ── Safe empty fallback ────────────────────────────────────────────────
    _safe_empty: dict[str, Any] = {
        "days": days,
        "filters": {
            "waste_state": effective_state,
            "n":           n_list,
            "limit":       limit,
            "min_spend":   float(min_spend),
        },
        "rows": [],
        "summary": {
            "ngrams_returned":           0,
            "source_rows_analyzed":      0,
            "unique_search_terms_analyzed": 0,
        },
        "data_quality": {
            "source":  "search_terms",
            "dataset": "ngrams",
            "status":  "db_unavailable",
        },
        "db_unavailable": True,
    }

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_empty

            with conn.cursor() as cur:
                # ── Build WHERE clauses ───────────────────────────────────
                conditions: list[str] = [
                    "source_date >= NOW() - INTERVAL '1 day' * %s",
                ]
                params: list[Any] = [days]

                if campaign_key is not None:
                    conditions.append("campaign_name = %s")
                    params.append(campaign_key)

                if match_type:
                    conditions.append("match_type ILIKE %s")
                    params.append(f"%{match_type.strip()}%")

                if q:
                    conditions.append("search_term ILIKE %s")
                    params.append(f"%{q.strip()}%")

                if min_spend > 0:
                    conditions.append("spend_usd >= %s")
                    params.append(min_spend)

                if effective_state == "flagged":
                    conditions.append("is_flagged_waste IS TRUE")
                elif effective_state == "clean":
                    conditions.append("is_flagged_waste IS FALSE")
                elif effective_state == "unanalyzed":
                    conditions.append("is_flagged_waste IS NULL")
                # "all" — no additional condition

                where_sql = " AND ".join(conditions)

                # Fetch cap+1 rows so we can accurately detect whether additional
                # rows existed beyond the cap.  We trim back to cap after the fetch.
                # All user-supplied values are passed via parameterized %s.
                # where_sql is built exclusively from static string literals above.
                params.append(_NGRAMS_SOURCE_ROW_CAP + 1)
                query = (
                    "SELECT"
                    " search_term, campaign_name, ad_group, keyword, match_type,"
                    " spend_usd, clicks, impressions, conversions, is_flagged_waste"
                    " FROM search_terms"
                    " WHERE " + where_sql +
                    " ORDER BY spend_usd DESC NULLS LAST, source_date DESC, id DESC"
                    " LIMIT %s"
                )
                cur.execute(query, params)
                raw_rows = cur.fetchall()
                cols = [d[0] for d in cur.description]

    except Exception as exc:  # noqa: BLE001
        log.error("[api/search-terms/ngrams] database error: %s", exc, exc_info=True)
        return _safe_empty

    # ── Cap detection: fetch cap+1, detect overflow, trim ─────────────────
    row_cap_applied = len(raw_rows) > _NGRAMS_SOURCE_ROW_CAP
    if row_cap_applied:
        raw_rows = raw_rows[:_NGRAMS_SOURCE_ROW_CAP]

    # ── Convert to dicts ──────────────────────────────────────────────────
    from analysis.ngrams import aggregate_ngrams  # noqa: PLC0415

    source_dicts = [dict(zip(cols, row)) for row in raw_rows]
    source_count = len(source_dicts)
    unique_terms  = len({r.get("search_term") for r in source_dicts if r.get("search_term")})

    # ── Aggregate ─────────────────────────────────────────────────────────
    all_ngrams = aggregate_ngrams(source_dicts, n_set)

    # Apply limit to the aggregated results
    returned_ngrams = all_ngrams[:limit]

    # ── Build filters dict for response ──────────────────────────────────
    filters_out: dict[str, Any] = {
        "waste_state": effective_state,
        "n":           n_list,
        "limit":       limit,
        "min_spend":   float(min_spend),
    }
    if campaign_key is not None:
        filters_out["campaign"] = campaign_key
    if match_type:
        filters_out["match_type"] = match_type.strip()
    if q:
        filters_out["q"] = q.strip()

    # ── data_quality block ────────────────────────────────────────────────
    data_quality: dict[str, Any] = {
        "source":  "search_terms",
        "dataset": "ngrams",
        "note":    _NGRAMS_DATA_QUALITY_NOTE,
    }
    if row_cap_applied:
        data_quality["row_cap_applied"] = True
        data_quality["row_cap"]         = _NGRAMS_SOURCE_ROW_CAP

    return {
        "days": days,
        "filters": filters_out,
        "rows": returned_ngrams,
        "summary": {
            "ngrams_returned":              len(returned_ngrams),
            "source_rows_analyzed":         source_count,
            "unique_search_terms_analyzed": unique_terms,
        },
        "data_quality":   data_quality,
        "db_unavailable": False,
    }


# ---------------------------------------------------------------------------
# GCLID Attribution endpoint — cursor-paginated, read-only, auth required. (PR-ADS-044)
# ---------------------------------------------------------------------------

_GCLID_ATTR_MAX_LIMIT    = 500
_GCLID_ATTR_DEFAULT_DAYS = 30
_GCLID_ATTR_MAX_DAYS     = 365


def _encode_gclid_cursor(created_at: datetime | str, row_id: int) -> str:
    """Encode a keyset cursor for gclid_attribution.

    Accepts datetime-like values and falls back to str() for already-serialized timestamps.
    """
    return _encode_keyset_cursor({
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at),
        "id": int(row_id),
    })


def _decode_gclid_cursor(token: str):
    """Decode a base64 JSON cursor for gclid_attribution.

    Returns (created_at_as_datetime, id_int) or raises ValueError on invalid input.
    """
    try:
        payload = _decode_keyset_cursor(token)
        created_at_raw = str(payload["created_at"])
        try:
            created_at = datetime.fromisoformat(created_at_raw)
        except ValueError:
            if not created_at_raw.endswith("Z"):
                raise
            # Support legacy cursors serialized with a trailing UTC "Z".
            created_at = datetime.fromisoformat(created_at_raw[:-1] + "+00:00")
        row_id = int(payload["id"])

        if row_id <= 0:
            raise ValueError("cursor id must be positive")

        return created_at, row_id
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


@app.get("/api/gclid-attribution")
def api_gclid_attribution(
    user: dict = Depends(require_auth),
    days: int = Query(default=_GCLID_ATTR_DEFAULT_DAYS, description="Number of days to look back (1–365)"),
    campaign: str = Query(default=None, description="Filter by exact campaign_name"),
    gclid: str = Query(default=None, description="Filter by exact gclid value"),
    contact_id: str = Query(default=None, description="Filter by contact_id"),
    deal_id: str = Query(default=None, description="Filter by deal_id"),
    match_status: str = Query(default=None, description="Filter by match_status"),
    limit: int = Query(default=100, description="Page size (1–500)"),
    cursor: str = Query(default=None, description="Opaque pagination cursor from previous response"),
) -> dict[str, Any]:
    """Return paginated GCLID attribution rows for the last N days.

    Uses cursor/keyset pagination on (created_at DESC, id DESC).
    Auth required. Read-only.
    Source: gclid_attribution table (PR-ADS-044).
    Does not upload offline conversions. Does not write to Google Ads or HubSpot.
    """
    days  = max(1, min(_GCLID_ATTR_MAX_DAYS, days))
    limit = max(1, min(_GCLID_ATTR_MAX_LIMIT, limit))

    _safe_empty: dict[str, Any] = {
        "days": days,
        "rows": [],
        "pagination": {
            "limit":       limit,
            "next_cursor": None,
            "has_more":    False,
        },
        "summary": {
            "loaded_rows":                 0,
            "matched_rows":                0,
            "url_fallback_rows":           0,
            "unmatched_rows":              0,
            "total_deal_amount_usd_loaded": 0,
        },
        "db_unavailable": True,
    }

    # ── Decode cursor ─────────────────────────────────────────────────────
    cursor_ts:  datetime | None = None
    cursor_id:  int | None      = None
    if cursor:
        try:
            cursor_ts, cursor_id = _decode_gclid_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Campaign name normalisation ───────────────────────────────────────
    campaign_key: str | None = None
    if campaign:
        from db.writers import _canonicalise_campaign_name  # noqa: PLC0415
        campaign_key = _canonicalise_campaign_name(campaign.strip().lower())

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_empty

            with conn.cursor() as cur:
                conditions: list[str] = [
                    "created_at >= NOW() - INTERVAL '1 day' * %s",
                ]
                params: list[Any] = [days]

                if cursor_ts is not None and cursor_id is not None:
                    conditions.append(
                        "(created_at < %s OR (created_at = %s AND id < %s))"
                    )
                    params += [cursor_ts, cursor_ts, cursor_id]

                if campaign_key is not None:
                    conditions.append("campaign_name = %s")
                    params.append(campaign_key)

                if gclid:
                    conditions.append("gclid = %s")
                    params.append(gclid.strip())

                if contact_id:
                    conditions.append("contact_id = %s")
                    params.append(contact_id.strip())

                if deal_id:
                    conditions.append("deal_id = %s")
                    params.append(deal_id.strip())

                if match_status:
                    conditions.append("match_status = %s")
                    params.append(match_status.strip())

                where_sql = " AND ".join(conditions)
                fetch_limit = limit + 1
                params.append(fetch_limit)

                query = (
                    "SELECT"
                    " id, gclid, contact_id, deal_id,"
                    " campaign_name, keyword, match_type, search_term,"
                    " company, country, first_url,"
                    " contact_created_at, deal_created_at, deal_close_date,"
                    " deal_stage, deal_stage_label, deal_amount_usd,"
                    " mql_status, status_category,"
                    " match_status, match_source,"
                    " created_at"
                    " FROM gclid_attribution"
                    " WHERE " + where_sql +
                    " ORDER BY created_at DESC, id DESC"
                    " LIMIT %s"
                )
                cur.execute(query, params)
                raw_rows = cur.fetchall()
                cols = [d[0] for d in cur.description]

    except Exception as exc:  # noqa: BLE001
        log.error("[api/gclid-attribution] database error: %s", exc, exc_info=True)
        return _safe_empty

    has_more = len(raw_rows) > limit
    page_rows = raw_rows[:limit]

    out: list[dict] = []
    matched_count      = 0
    url_fallback_count = 0
    unmatched_count    = 0
    total_deal_amount  = 0.0

    for row in page_rows:
        r = dict(zip(cols, row))
        ms = r.get("match_status") or ""
        if ms == "matched":
            matched_count += 1
        elif ms == "url_fallback":
            url_fallback_count += 1
        elif ms == "unmatched":
            unmatched_count += 1

        amt = r.get("deal_amount_usd")
        if amt is not None:
            total_deal_amount += float(amt)

        out.append({
            "id":                    r["id"],
            "gclid":                 r["gclid"],
            "contact_id":            r["contact_id"],
            "deal_id":               r["deal_id"],
            "company":               r["company"],
            "country":               r["country"],
            "campaign_name":         r["campaign_name"],
            "keyword":               r["keyword"],
            "match_type":            r["match_type"],
            "search_term":           r["search_term"],
            "first_url":             r["first_url"],
            "contact_created_at":    r["contact_created_at"].isoformat() if r["contact_created_at"] else None,
            "deal_created_at":       r["deal_created_at"].isoformat() if r["deal_created_at"] else None,
            "deal_close_date":       r["deal_close_date"].isoformat() if r["deal_close_date"] else None,
            "deal_stage":            r["deal_stage"],
            "deal_stage_label":      r["deal_stage_label"],
            "deal_amount_usd":       round(float(r["deal_amount_usd"]), 2) if r["deal_amount_usd"] is not None else None,
            "mql_status":            r["mql_status"],
            "status_category":       r["status_category"],
            "match_status":          r["match_status"],
            "match_source":          r["match_source"],
            "created_at":            r["created_at"].isoformat() if r["created_at"] else None,
        })

    # Build next cursor from last row on this page
    next_cursor_token: str | None = None
    if has_more and page_rows:
        last = dict(zip(cols, page_rows[-1]))
        if last.get("created_at"):
            next_cursor_token = _encode_gclid_cursor(last["created_at"], int(last["id"]))

    return {
        "days": days,
        "rows": out,
        "pagination": {
            "limit":       limit,
            "next_cursor": next_cursor_token,
            "has_more":    has_more,
        },
        "summary": {
            "loaded_rows":                  len(out),
            "matched_rows":                 matched_count,
            "url_fallback_rows":            url_fallback_count,
            "unmatched_rows":               unmatched_count,
            "total_deal_amount_usd_loaded": round(total_deal_amount, 2),
        },
    }


# ---------------------------------------------------------------------------
# Attribution Quality endpoint — read-only, auth required. (PR-ADS-048)
# ---------------------------------------------------------------------------

_ATTR_QUALITY_DEFAULT_DAYS = 30
_ATTR_QUALITY_MAX_DAYS     = 365


def _compute_attribution_quality_signals(
    summary: dict,
    freshness: "dict | None",
) -> list:
    """Derive read-only quality signal cards from attribution summary counts.

    Signals are attribution evidence/completeness indicators only.
    Does not recommend actions. Does not claim OCT readiness.
    Forbidden language: OCT ready, upload, push, fix, guaranteed, qualified revenue.
    """
    signals: list = []
    total = summary.get("loaded_scope_rows", 0)

    if total == 0:
        return [{
            "key":      "no_attribution_rows",
            "status":   "unknown",
            "label":    "No attribution rows",
            "detail":   "No GCLID attribution evidence is stored for this scope.",
            "severity": "low",
        }]

    matched_rate      = summary.get("matched_rate_pct", 0.0) or 0.0
    fallback_rate     = summary.get("url_fallback_rate_pct", 0.0) or 0.0
    unmatched_rate    = summary.get("unmatched_rate_pct", 0.0) or 0.0
    deal_link_rate    = summary.get("deal_link_rate_pct", 0.0) or 0.0
    amount_cov        = summary.get("deal_amount_coverage_pct")
    deals_linked      = summary.get("deals_linked", 0) or 0

    # Signal: match_strength
    if matched_rate >= 70:
        ms_status, ms_label, ms_severity = "good",  "Strong match coverage",   "low"
    elif matched_rate >= 40:
        ms_status, ms_label, ms_severity = "watch", "Moderate match coverage", "medium"
    else:
        ms_status, ms_label, ms_severity = "weak",  "Weak match coverage",     "medium"

    signals.append({
        "key":      "match_strength",
        "status":   ms_status,
        "label":    ms_label,
        "detail":   f"{matched_rate:.1f}% of loaded attribution rows are direct matched rows.",
        "severity": ms_severity,
    })

    # Signal: url_fallback_reliance
    if fallback_rate < 10:
        uf_status, uf_label, uf_severity = "good",  "Low URL fallback reliance",  "low"
    elif fallback_rate <= 25:
        uf_status, uf_label, uf_severity = "watch", "URL fallback reliance",       "medium"
    else:
        uf_status, uf_label, uf_severity = "risk",  "High URL fallback reliance",  "high"

    signals.append({
        "key":      "url_fallback_reliance",
        "status":   uf_status,
        "label":    uf_label,
        "detail":   (
            f"{fallback_rate:.1f}% of rows rely on URL fallback rather than direct GCLID match. "
            "URL fallback is weaker attribution evidence than direct GCLID."
        ),
        "severity": uf_severity,
    })

    # Signal: unmatched_rate
    if unmatched_rate < 10:
        um_status, um_label, um_severity = "good",  "Low unmatched rate",      "low"
    elif unmatched_rate <= 25:
        um_status, um_label, um_severity = "watch", "Elevated unmatched rate", "medium"
    else:
        um_status, um_label, um_severity = "risk",  "High unmatched rate",     "high"

    signals.append({
        "key":      "unmatched_rate",
        "status":   um_status,
        "label":    um_label,
        "detail":   f"{unmatched_rate:.1f}% of rows have no matched contact or deal evidence.",
        "severity": um_severity,
    })

    # Signal: deal_linkage
    if deal_link_rate >= 40:
        dl_status, dl_label, dl_severity = "good",  "Strong deal linkage",  "low"
    elif deal_link_rate >= 15:
        dl_status, dl_label, dl_severity = "watch", "Partial deal linkage", "medium"
    else:
        dl_status, dl_label, dl_severity = "weak",  "Thin deal linkage",    "medium"

    signals.append({
        "key":      "deal_linkage",
        "status":   dl_status,
        "label":    dl_label,
        "detail":   (
            f"{deal_link_rate:.1f}% of attribution rows are linked to a deal. "
            "This is a data-completeness signal, not a sales-performance verdict."
        ),
        "severity": dl_severity,
    })

    # Signal: amount_coverage (only when deals are linked)
    if deals_linked > 0 and amount_cov is not None:
        if amount_cov >= 70:
            ac_status, ac_label, ac_severity = "good",  "Good deal amount coverage",    "low"
        elif amount_cov >= 30:
            ac_status, ac_label, ac_severity = "watch", "Partial deal amount coverage", "medium"
        else:
            ac_status, ac_label, ac_severity = "weak",  "Thin deal amount coverage",    "medium"

        signals.append({
            "key":      "amount_coverage",
            "status":   ac_status,
            "label":    ac_label,
            "detail":   f"{amount_cov:.1f}% of deal-linked rows have a deal amount populated.",
            "severity": ac_severity,
        })
    else:
        signals.append({
            "key":      "amount_coverage",
            "status":   "unknown",
            "label":    "No deal amount data",
            "detail":   "No deal-linked rows with amount data found for this scope.",
            "severity": "low",
        })

    # Signal: freshness (from sync_state gclid/matches)
    if freshness:
        from datetime import timezone as _tz  # noqa: PLC0415

        fst       = freshness.get("status") or "unknown"
        last_sync = freshness.get("last_successful_sync_at")

        if fst == "success" and last_sync:
            now_utc = datetime.now(_tz.utc)
            sync_dt = datetime.fromisoformat(str(last_sync).replace("Z", "+00:00"))
            if sync_dt.tzinfo is None:
                sync_dt = sync_dt.replace(tzinfo=_tz.utc)
            age_hours = (now_utc - sync_dt).total_seconds() / 3600

            if age_hours <= 48:
                fr_status, fr_label, fr_severity = "good",  "Local warehouse is fresh",       "low"
                fr_detail = (
                    f"Last successful sync was {age_hours:.0f}h ago. "
                    "Local warehouse freshness only."
                )
            else:
                fr_status, fr_label, fr_severity = "watch", "Local warehouse may be stale", "medium"
                fr_detail = (
                    f"Last successful sync was {age_hours:.0f}h ago. "
                    "Warrants review of local warehouse freshness."
                )
        elif fst == "failed":
            fr_status, fr_label, fr_severity = "risk",    "Sync failure recorded",  "high"
            fr_detail = (
                "The last recorded sync attempt ended with an error. "
                "Local warehouse freshness warrants review."
            )
        else:
            fr_status, fr_label, fr_severity = "unknown", "Sync state unknown", "low"
            fr_detail = (
                "No tracked sync state found. "
                "Local warehouse freshness cannot be assessed."
            )

        signals.append({
            "key":      "freshness",
            "status":   fr_status,
            "label":    fr_label,
            "detail":   fr_detail,
            "severity": fr_severity,
        })

    return signals


@app.get("/api/attribution/quality")
def api_attribution_quality(
    user: dict = Depends(require_auth),
    days: int = Query(
        default=_ATTR_QUALITY_DEFAULT_DAYS,
        description="Number of days to look back (1–365)",
    ),
    campaign: str = Query(
        default=None,
        description="Optional exact canonical campaign name filter",
    ),
) -> dict[str, Any]:
    """Return read-only attribution quality signals for the last N days.

    Auth required. Read-only.
    Source tables: gclid_attribution, sync_state, gclid_coverage_snapshots.
    Does not call Google Ads APIs. Does not call HubSpot APIs.
    Does not upload offline conversions. Does not write to any external system.
    Signals are attribution evidence/completeness indicators only.
    """
    days = max(1, min(_ATTR_QUALITY_MAX_DAYS, days))

    _safe_db_unavailable: dict[str, Any] = {
        "days":           days,
        "summary":        {},
        "rates":          {},
        "signals":        [],
        "db_unavailable": True,
    }

    # Campaign name normalisation
    campaign_key: str | None = None
    if campaign:
        from db.writers import _canonicalise_campaign_name  # noqa: PLC0415
        campaign_key = _canonicalise_campaign_name(campaign.strip().lower())

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_db_unavailable

            with conn.cursor() as cur:
                # ── Aggregate counts from gclid_attribution ──────────────
                # agg_conditions contains only programmer-supplied literal strings with %s
                # placeholders; all user input (days, campaign_key) goes into agg_params.
                # The WHERE clause is never built from raw request data.
                agg_conditions: list[str] = [
                    "created_at >= NOW() - INTERVAL '1 day' * %s",
                ]
                agg_params: list[Any] = [days]

                if campaign_key is not None:
                    agg_conditions.append("campaign_name = %s")
                    agg_params.append(campaign_key)

                agg_where = " AND ".join(agg_conditions)

                cur.execute(
                    f"""
                    SELECT
                        COUNT(*)                                                              AS total_rows,
                        COUNT(*) FILTER (WHERE match_status = 'matched')                     AS matched_rows,
                        COUNT(*) FILTER (WHERE match_status = 'url_fallback')                AS url_fallback_rows,
                        COUNT(*) FILTER (WHERE match_status = 'unmatched')                   AS unmatched_rows,
                        COUNT(*) FILTER (
                            WHERE match_status NOT IN ('matched', 'url_fallback', 'unmatched')
                               OR match_status IS NULL
                        )                                                                     AS unknown_rows,
                        COUNT(*) FILTER (WHERE contact_id IS NOT NULL)                       AS contacts_linked,
                        COUNT(*) FILTER (WHERE deal_id IS NOT NULL)                          AS deals_linked,
                        COUNT(*) FILTER (
                            WHERE deal_amount_usd IS NOT NULL AND deal_id IS NOT NULL
                        )                                                                     AS rows_with_deal_amount,
                        COALESCE(SUM(deal_amount_usd), 0)                                    AS total_deal_amount_usd,
                        MAX(created_at)                                                       AS latest_attribution_at
                    FROM gclid_attribution
                    WHERE {agg_where}
                    """,  # noqa: S608
                    agg_params,
                )
                agg_row  = cur.fetchone()
                agg_cols = [d[0] for d in cur.description]
                agg      = dict(zip(agg_cols, agg_row)) if agg_row else {}

                # ── Freshness from sync_state (gclid / matches) ───────────
                cur.execute(
                    """
                    SELECT source, dataset, status,
                           last_successful_sync_at, last_source_date
                    FROM sync_state
                    WHERE source = 'gclid' AND dataset = 'matches'
                    LIMIT 1
                    """,
                )
                sync_row  = cur.fetchone()
                sync_cols = [d[0] for d in cur.description]
                sync_data = dict(zip(sync_cols, sync_row)) if sync_row else None

                # ── Latest coverage snapshot ──────────────────────────────
                cur.execute(
                    """
                    SELECT snapshot_date,
                           contacts_with_gclid,
                           contacts_without_gclid,
                           coverage_pct
                    FROM gclid_coverage_snapshots
                    ORDER BY snapshot_date DESC, id DESC
                    LIMIT 1
                    """,
                )
                cov_row  = cur.fetchone()
                cov_cols = [d[0] for d in cur.description]
                cov_data = dict(zip(cov_cols, cov_row)) if cov_row else None

    except Exception as exc:  # noqa: BLE001
        log.error("[api/attribution/quality] database error: %s", exc, exc_info=True)
        return _safe_db_unavailable

    total = int(agg.get("total_rows") or 0)

    matched_rows          = int(agg.get("matched_rows")       or 0)
    url_fallback_rows     = int(agg.get("url_fallback_rows")  or 0)
    unmatched_rows        = int(agg.get("unmatched_rows")     or 0)
    unknown_rows          = int(agg.get("unknown_rows")       or 0)
    contacts_linked       = int(agg.get("contacts_linked")    or 0)
    deals_linked          = int(agg.get("deals_linked")       or 0)
    rows_with_deal_amount = int(agg.get("rows_with_deal_amount") or 0)
    total_deal_amount     = float(agg.get("total_deal_amount_usd") or 0.0)
    latest_attr_at        = agg.get("latest_attribution_at")

    summary: dict[str, Any] = {
        "loaded_scope_rows":     total,
        "matched_rows":          matched_rows,
        "url_fallback_rows":     url_fallback_rows,
        "unmatched_rows":        unmatched_rows,
        "unknown_rows":          unknown_rows,
        "contacts_linked":       contacts_linked,
        "deals_linked":          deals_linked,
        "rows_with_deal_amount": rows_with_deal_amount,
        "total_deal_amount_usd": round(total_deal_amount, 2),
        "latest_attribution_at": latest_attr_at.isoformat() if latest_attr_at else None,
    }

    def _pct(n: int, d: int) -> "float | None":
        return round(n / d * 100, 2) if d > 0 else None

    rates: dict[str, Any] = {}
    if total > 0:
        rates["matched_rate_pct"]      = _pct(matched_rows, total)
        rates["url_fallback_rate_pct"] = _pct(url_fallback_rows, total)
        rates["unmatched_rate_pct"]    = _pct(unmatched_rows, total)
        rates["deal_link_rate_pct"]    = _pct(deals_linked, total)
        if deals_linked > 0:
            rates["deal_amount_coverage_pct"] = _pct(rows_with_deal_amount, deals_linked)

    # Flat dict used by signal helper (merge rates into summary for convenience)
    summary_for_signals: dict[str, Any] = {
        **summary,
        "matched_rate_pct":         rates.get("matched_rate_pct", 0.0) or 0.0,
        "url_fallback_rate_pct":    rates.get("url_fallback_rate_pct", 0.0) or 0.0,
        "unmatched_rate_pct":       rates.get("unmatched_rate_pct", 0.0) or 0.0,
        "deal_link_rate_pct":       rates.get("deal_link_rate_pct", 0.0) or 0.0,
        "deal_amount_coverage_pct": rates.get("deal_amount_coverage_pct"),
    }

    # ── Freshness dict ─────────────────────────────────────────────────────
    freshness: "dict[str, Any] | None" = None
    if sync_data:
        freshness = {
            "source":                  sync_data["source"],
            "dataset":                 sync_data["dataset"],
            "status":                  sync_data["status"],
            "last_successful_sync_at": (
                sync_data["last_successful_sync_at"].isoformat()
                if sync_data.get("last_successful_sync_at") else None
            ),
            "last_source_date": (
                str(sync_data["last_source_date"])
                if sync_data.get("last_source_date") else None
            ),
        }

    # ── Coverage snapshot ──────────────────────────────────────────────────
    coverage_snapshot: "dict[str, Any] | None" = None
    if cov_data:
        coverage_snapshot = {
            "snapshot_date":          str(cov_data["snapshot_date"]) if cov_data.get("snapshot_date") else None,
            "contacts_with_gclid":    cov_data.get("contacts_with_gclid"),
            "contacts_without_gclid": cov_data.get("contacts_without_gclid"),
            "coverage_pct":           float(cov_data["coverage_pct"]) if cov_data.get("coverage_pct") is not None else None,
        }

    signals = _compute_attribution_quality_signals(summary_for_signals, freshness)

    scope: dict[str, Any] = {}
    if campaign_key:
        scope["campaign"] = campaign_key

    result: dict[str, Any] = {
        "days":    days,
        "summary": summary,
        "rates":   rates,
        "signals": signals,
    }
    if scope:
        result["scope"] = scope
    if freshness:
        result["freshness"] = freshness
    if coverage_snapshot:
        result["coverage_snapshot"] = coverage_snapshot

    return result


@app.get("/api/gclid-coverage")
def api_gclid_coverage(
    user: dict = Depends(require_auth),
    days: int = Query(default=30, description="Number of days to look back (1–365)"),
) -> dict[str, Any]:
    """Return GCLID coverage snapshot rows for the last N days.

    Auth required. Read-only.
    Source: gclid_coverage_snapshots table (PR-ADS-044).
    Does not write to Google Ads or HubSpot.
    """
    days = max(1, min(365, days))

    _safe_empty: dict[str, Any] = {
        "days": days,
        "rows": [],
        "db_unavailable": True,
    }

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return _safe_empty

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        snapshot_date,
                        total_contacts,
                        contacts_with_gclid,
                        contacts_without_gclid,
                        coverage_pct,
                        created_at
                    FROM gclid_coverage_snapshots
                    WHERE snapshot_date >= CURRENT_DATE - %s
                    ORDER BY snapshot_date DESC, id DESC
                    """,
                    (days,),
                )
                raw_rows = cur.fetchall()
                cols = [d[0] for d in cur.description]

    except Exception as exc:  # noqa: BLE001
        log.error("[api/gclid-coverage] database error: %s", exc, exc_info=True)
        return _safe_empty

    out: list[dict] = []
    for row in raw_rows:
        r = dict(zip(cols, row))
        out.append({
            "snapshot_date":           str(r["snapshot_date"]) if r["snapshot_date"] else None,
            "total_contacts":          r["total_contacts"],
            "contacts_with_gclid":     r["contacts_with_gclid"],
            "contacts_without_gclid":  r["contacts_without_gclid"],
            "coverage_pct":            float(r["coverage_pct"]) if r["coverage_pct"] is not None else None,
            "created_at":              r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {
        "days": days,
        "rows": out,
    }


# ---------------------------------------------------------------------------
# Monitoring status endpoint — read-only, auth required. (PR-ADS-069)
# ---------------------------------------------------------------------------

# Lookback window used to query runs for monitoring purposes.
_MONITORING_LOOKBACK_DAYS = 90


def _load_monitoring_thresholds() -> tuple[dict[str, int], int]:
    """Load monitoring stale thresholds from config/thresholds.yaml.

    Returns:
        (stale_after_days_map, consecutive_failure_warning)
    Falls back to api.monitoring defaults for any missing or invalid value.
    """
    stale = dict(_MONITORING_STALE_DAYS_DEFAULT)
    consec = _MONITORING_CONSECUTIVE_FAILURE_WARNING_DEFAULT
    try:
        with _CONFIG_THRESHOLDS.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        mon = (raw.get("ui", {}) or {}).get("monitoring", {}) or {}
        sad = mon.get("stale_after_days", {}) or {}
        for run_type in ("daily", "weekly", "monthly"):
            raw_val = sad.get(run_type)
            try:
                v = int(raw_val)
                if v >= 1:
                    stale[run_type] = v
            except (TypeError, ValueError):
                pass
        raw_consec = mon.get("consecutive_failure_warning")
        try:
            v = int(raw_consec)
            if v >= 1:
                consec = v
        except (TypeError, ValueError):
            pass
    except Exception as exc:  # noqa: BLE001
        log.warning("[monitoring/status] could not load thresholds config: %s", exc)
    return stale, consec


def _read_jsonl_runs() -> list[dict]:
    """Read the most recent run records from the JSONL file, newest first.

    Bounded to the last 1000 lines to keep memory usage constant as the
    file grows — sufficient for monitoring lookback purposes.
    """
    if not _RUN_HISTORY_FILE.is_file():
        return []
    from collections import deque  # noqa: PLC0415
    # Bounded deque: keeps only the last 1000 records as the file is appended
    # chronologically, so the tail contains the most recent runs.
    bounded: deque = deque(maxlen=1000)
    try:
        with _RUN_HISTORY_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    bounded.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    # bounded contains the last N records in chronological order; reverse for newest-first.
    return list(reversed(bounded))


@app.get("/api/monitoring/status")
def api_monitoring_status(user: dict = Depends(require_auth)) -> dict[str, Any]:
    """Return read-only monitoring summary for scheduled run health.

    Computes per-run-type state (daily/weekly/monthly):
      - last_success_at, last_status, consecutive_failures, stale.
    Derives severity (green/yellow/red) and a human-readable warnings list.

    Auth required. Read-only. No external calls. No mutations.
    Phase 1 read-only — no writes to any external system.
    """
    stale_after_days, consecutive_failure_warning = _load_monitoring_thresholds()

    runs: list[dict] = []
    db_unavailable = False

    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                db_unavailable = True
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        # PostgreSQL-specific interval syntax: INTERVAL '1 day' * N
                        """
                        SELECT run_type, started_at, finished_at, status
                        FROM runs
                        WHERE started_at >= NOW() - INTERVAL '1 day' * %s
                        ORDER BY started_at DESC
                        """,
                        (_MONITORING_LOOKBACK_DAYS,),
                    )
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description]
                    for row in rows:
                        r = dict(zip(cols, row))
                        r["started_at"] = (
                            r["started_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            if r.get("started_at") else None
                        )
                        r["finished_at"] = (
                            r["finished_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            if r.get("finished_at") else None
                        )
                        runs.append(r)
    except Exception as exc:  # noqa: BLE001
        log.error("[api/monitoring/status] database error: %s", exc, exc_info=True)
        db_unavailable = True

    # Fall back to JSONL run history when the DB is offline.
    if db_unavailable or not runs:
        runs = _read_jsonl_runs()

    result = _compute_monitoring_status(runs, stale_after_days, consecutive_failure_warning)
    if db_unavailable:
        result["db_unavailable"] = True
    return result


# ---------------------------------------------------------------------------
# Historical Backfill endpoints — admin-gated, local DB writes only.
# These endpoints call the same backfill framework as the CLI (scripts/backfill.py).
# They NEVER write to Google Ads or HubSpot.
# ---------------------------------------------------------------------------

_VALID_BACKFILL_SOURCES = {"all", "google_ads", "hubspot"}
_VALID_BACKFILL_CHUNKS = {"monthly", "weekly"}


@app.post("/api/backfill/run")
def api_backfill_run(body: BackfillRunRequest, request: Request) -> dict[str, Any]:
    """
    Trigger a historical backfill run.

    Admin-only. Local DB writes only (when dry_run=False).
    NEVER writes to Google Ads or HubSpot.

    Requires admin session or ADMIN_API_TOKEN.
    Returns 409 if a backfill is already running.
    Returns 422 on invalid parameters.
    """
    check_admin_or_token(request)

    # Validate request body before acquiring the lock
    if body.source not in _VALID_BACKFILL_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=f"source must be one of {sorted(_VALID_BACKFILL_SOURCES)}, got {body.source!r}",
        )
    if body.chunk not in _VALID_BACKFILL_CHUNKS:
        raise HTTPException(
            status_code=422,
            detail=f"chunk must be 'monthly' or 'weekly', got {body.chunk!r}",
        )
    if body.max_chunks is not None and body.max_chunks < 1:
        raise HTTPException(status_code=422, detail="max_chunks must be >= 1 when provided")

    try:
        from_date = date.fromisoformat(body.date_from)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"date_from '{body.date_from}' is not a valid ISO date (YYYY-MM-DD)",
        )
    try:
        to_date = date.fromisoformat(body.date_to)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"date_to '{body.date_to}' is not a valid ISO date (YYYY-MM-DD)",
        )
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="date_from must be before or equal to date_to")

    with _backfill_lock:
        if _backfill_state["running"]:
            raise HTTPException(status_code=409, detail="backfill already running")
        _backfill_state["running"] = True

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info(
        "[api/backfill/run] started at %s  source=%s  %s→%s  chunk=%s  dry_run=%s",
        started_at, body.source, body.date_from, body.date_to, body.chunk, body.dry_run,
    )

    try:
        from scripts.backfill import run_backfill_from_options  # noqa: PLC0415
        summary = run_backfill_from_options(
            source=body.source,
            date_from=body.date_from,
            date_to=body.date_to,
            chunk=body.chunk,
            dry_run=body.dry_run,
            max_chunks=body.max_chunks,
        )
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("[api/backfill/run] succeeded, finished at %s", finished_at)
        result: dict[str, Any] = {
            "status": "success",
            "dry_run": body.dry_run,
            "source": body.source,
            "date_from": body.date_from,
            "date_to": body.date_to,
            "chunk": body.chunk,
            "started_at": started_at,
            "finished_at": finished_at,
            "summary": summary,
        }
        with _backfill_lock:
            _backfill_state["latest"] = result
        return result

    except ValueError as exc:
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.warning("[api/backfill/run] validation error: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    except Exception as exc:  # noqa: BLE001
        finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.error("[api/backfill/run] failed: %s", exc, exc_info=True)
        _err_msg = f"{type(exc).__name__}: backfill execution failed"
        err_result: dict[str, Any] = {
            "status": "failed",
            "dry_run": body.dry_run,
            "source": body.source,
            "date_from": body.date_from,
            "date_to": body.date_to,
            "chunk": body.chunk,
            "started_at": started_at,
            "finished_at": finished_at,
            "error": _err_msg,
            "summary": {
                "status": "failed",
                "chunks_total": 0,
                "chunks_completed": 0,
                "datasets": {},
                "errors": [_err_msg],
            },
        }
        with _backfill_lock:
            _backfill_state["latest"] = err_result
        raise HTTPException(
            status_code=500,
            detail=f"Backfill failed: {type(exc).__name__}",
        ) from exc

    finally:
        with _backfill_lock:
            _backfill_state["running"] = False


@app.get("/api/backfill/status")
def api_backfill_status(user: dict = Depends(require_auth)) -> dict[str, Any]:
    """
    Return current backfill run state and the latest run summary.

    Requires auth (any role). Read-only. No external calls. No mutations.
    Phase 1 read-only externally confirmed.
    """
    with _backfill_lock:
        return {
            "running": _backfill_state["running"],
            "latest": _backfill_state["latest"],
        }


# ---------------------------------------------------------------------------
# Historical Intelligence endpoint — GET /api/historical-intelligence
# Read-only trend analysis over local historical data.
# No external writes. No Google Ads writes. No HubSpot writes.
# Phase 1 read-only externally confirmed.
# ---------------------------------------------------------------------------

_HI_ENTITY_CAMPAIGNS = "campaigns"
_HI_ENTITY_GEO       = "geo"
_HI_VALID_ENTITIES   = {_HI_ENTITY_CAMPAIGNS, _HI_ENTITY_GEO}
_HI_DEFAULT_DAYS     = 30
_HI_DEFAULT_LIMIT    = 25
_HI_MAX_DAYS         = 180


@app.get("/api/historical-intelligence")
def api_historical_intelligence(
    user: dict = Depends(require_auth),
    entity: str = Query(
        default=_HI_ENTITY_CAMPAIGNS,
        description="Entity to analyse: campaigns | geo",
    ),
    current_days: int = Query(
        default=_HI_DEFAULT_DAYS,
        description="Length of the current comparison window in days (1–180)",
    ),
    previous_days: int = Query(
        default=_HI_DEFAULT_DAYS,
        description="Length of the previous comparison window in days (1–180)",
    ),
    limit: int = Query(
        default=_HI_DEFAULT_LIMIT,
        description="Maximum rows returned (1–100)",
    ),
) -> dict[str, Any]:
    """Return read-only historical trend signals for the requested entity.

    Compares the current period (most recent N days) to the previous period
    (the N days before that) using data already in the local database.

    Does NOT call Google Ads, HubSpot, Windsor, or any external service.
    Does NOT mutate any data.
    Phase 1 read-only externally confirmed.
    """
    # ── Parameter sanitisation ─────────────────────────────────────────────
    entity = entity.strip().lower() if entity else ""
    if not entity:
        entity = _HI_ENTITY_CAMPAIGNS
    elif entity not in _HI_VALID_ENTITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported entity '{entity}'. Expected one of: {', '.join(sorted(_HI_VALID_ENTITIES))}.",
        )

    current_days  = max(1, min(_HI_MAX_DAYS, current_days))
    previous_days = max(1, min(_HI_MAX_DAYS, previous_days))
    limit         = max(1, min(100, limit))

    from analysis.historical_intelligence import (  # noqa: PLC0415
        compute_campaign_trends,
        compute_geo_trends,
        load_campaign_trend_rows,
        load_geo_trend_rows,
    )
    from db.connection import get_conn  # noqa: PLC0415

    _insufficient: dict[str, Any] = {
        "entity":        entity,
        "current_days":  current_days,
        "previous_days": previous_days,
        "status":        "insufficient_data",
        "message":       "Historical intelligence requires at least two comparable periods.",
        "summary": {
            "improving":          0,
            "deteriorating":      0,
            "stable":             0,
            "insufficient_data":  0,
            "new_activity":       0,
            "no_recent_activity": 0,
        },
        "rows": [],
    }

    try:
        with get_conn() as conn:
            if conn is None:
                return {**_insufficient, "db_unavailable": True}

            if entity == _HI_ENTITY_GEO:
                raw_rows = load_geo_trend_rows(conn, current_days, previous_days)
                result   = compute_geo_trends(
                    raw_rows,
                    current_days=current_days,
                    previous_days=previous_days,
                )
            else:
                raw_rows = load_campaign_trend_rows(conn, current_days, previous_days)
                result   = compute_campaign_trends(
                    raw_rows,
                    current_days=current_days,
                    previous_days=previous_days,
                )

    except Exception as exc:  # noqa: BLE001
        log.error("[api/historical-intelligence] database error: %s", exc, exc_info=True)
        return {**_insufficient, "db_unavailable": True}

    # Apply row limit
    result["rows"] = result.get("rows", [])[:limit]
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM REALITY AUDIT (PR-ADS-064)
# ═══════════════════════════════════════════════════════════════════════════════

_REALITY_AUDIT_CACHE_TTL_SECONDS = 60
_reality_audit_cache_lock = threading.Lock()
_reality_audit_cache: dict[int, dict[str, Any]] = {}


@app.get("/api/system/reality-audit")
def api_system_reality_audit(
    request: Request,
    days: int = Query(default=60, description="Time window in days (1–90)"),
) -> dict[str, Any]:
    """Return a read-only production reality audit diagnostic.

    Admin only. Checks row counts, freshness status, and verdicts for every
    major dataset table. Identifies pipeline blockers and trust issues.
    Intended for manual/admin diagnostics, not high-frequency polling.

    Does NOT call Google Ads, HubSpot, Windsor, or any external service.
    Does NOT mutate any data.
    Phase 1 read-only. PR-ADS-064.
    """
    check_admin_or_token(request)

    days = max(1, min(90, days))

    now_ts = datetime.now(timezone.utc).timestamp()
    with _reality_audit_cache_lock:
        cached = _reality_audit_cache.get(days)
        if cached and now_ts < cached["expires_at"]:
            return cached["data"]

    from db.connection import get_conn  # noqa: PLC0415
    from scripts.audit_production_reality import run_audit  # noqa: PLC0415
    with get_conn() as conn:
        audit = run_audit(days=days, conn=conn)

    with _reality_audit_cache_lock:
        _reality_audit_cache[days] = {
            "expires_at": now_ts + _REALITY_AUDIT_CACHE_TTL_SECONDS,
            "data": audit,
        }
    return audit


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH TERMS PRODUCTION VERDICT (PR-ADS-066)
# ═══════════════════════════════════════════════════════════════════════════════

_SEARCH_TERMS_VERDICT_CACHE_TTL_SECONDS = 60
_search_terms_verdict_cache_lock = threading.Lock()
_search_terms_verdict_cache: dict[int, dict[str, Any]] = {}


def _build_search_terms_verdict(days: int) -> dict[str, Any]:
    """Build focused Search Terms pipeline verdict from DB state.

    Read-only. No external calls. No writes.
    """
    from db.connection import get_conn  # noqa: PLC0415
    from scripts.verify_search_terms_pipeline import (  # noqa: PLC0415
        Verdict,
        compute_search_terms_verdict,
    )

    generated_at = datetime.now(timezone.utc).isoformat()

    db_info: dict[str, Any] = {
        "available": False,
        "rows_7d": 0,
        "rows_14d": 0,
        "rows_30d": 0,
        "rows_60d": 0,
        "latest_source_date": None,
        "blank_search_term_rows": 0,
        "spend_rows": 0,
        "click_rows": 0,
    }
    sync_info: dict[str, Any] = {
        "latest_batch_status": None,
        "latest_batch_row_count": None,
        "latest_batch_started_at": None,
        "sync_state_status": None,
        "last_successful_sync_at": None,
    }

    try:
        with get_conn() as conn:
            if conn is None:
                verdict_str = Verdict.DB_UNAVAILABLE
                reason = "Database connection unavailable"
                return {
                    "generated_at": generated_at,
                    "days": days,
                    "verdict": verdict_str,
                    "reason": reason,
                    "db": db_info,
                    "sync": sync_info,
                    "api": {"checked": False, "rows_returned": None, "total_rows_in_window": 0, "is_empty": True},
                    "next_action": "Fix database connection before checking Search Terms pipeline.",
                }

            db_info["available"] = True

            with conn.cursor() as cur:
                for window, key in [(7, "rows_7d"), (14, "rows_14d"), (30, "rows_30d"), (60, "rows_60d")]:
                    cur.execute(
                        "SELECT COUNT(*) FROM search_terms "
                        "WHERE source_date >= NOW() - INTERVAL '1 day' * %s",
                        (window,),
                    )
                    row = cur.fetchone()
                    db_info[key] = int(row[0]) if row else 0

                # For non-standard windows run an exact count
                if days not in (7, 14, 30, 60):
                    cur.execute(
                        "SELECT COUNT(*) FROM search_terms "
                        "WHERE source_date >= NOW() - INTERVAL '1 day' * %s",
                        (days,),
                    )
                    row = cur.fetchone()
                    db_info["rows_requested"] = int(row[0]) if row else 0

                cur.execute("SELECT MAX(source_date) FROM search_terms")
                row = cur.fetchone()
                db_info["latest_source_date"] = str(row[0]) if row and row[0] else None

                cur.execute(
                    "SELECT COUNT(*) FROM search_terms "
                    "WHERE search_term IS NULL OR TRIM(search_term) = ''"
                )
                row = cur.fetchone()
                db_info["blank_search_term_rows"] = int(row[0]) if row else 0

                cur.execute("SELECT COUNT(*) FROM search_terms WHERE spend_usd > 0")
                row = cur.fetchone()
                db_info["spend_rows"] = int(row[0]) if row else 0

                cur.execute("SELECT COUNT(*) FROM search_terms WHERE clicks > 0")
                row = cur.fetchone()
                db_info["click_rows"] = int(row[0]) if row else 0

                # Sync state — include both windsor (REST) and windsor_mcp (MCP import)
                # to show the most recent successful sync regardless of source.
                cur.execute(
                    "SELECT source, status, last_successful_sync_at "
                    "FROM sync_state "
                    "WHERE source IN ('windsor', 'windsor_mcp') "
                    "  AND dataset = 'search_terms' "
                    "ORDER BY last_successful_sync_at DESC NULLS LAST "
                    "LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    sync_info["sync_source"] = row[0]
                    sync_info["sync_state_status"] = row[1]
                    sync_info["last_successful_sync_at"] = str(row[2]) if row[2] else None

                # Latest sync batch — include both sources, most recent first
                cur.execute(
                    "SELECT source, status, row_count, started_at "
                    "FROM sync_batches "
                    "WHERE source IN ('windsor', 'windsor_mcp') "
                    "  AND dataset = 'search_terms' "
                    "ORDER BY started_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    sync_info["latest_batch_source"] = row[0]
                    sync_info["latest_batch_status"] = row[1]
                    sync_info["latest_batch_row_count"] = row[2]
                    sync_info["latest_batch_started_at"] = str(row[3]) if row[3] else None

                # Latest weekly run for verdict logic
                cur.execute(
                    "SELECT started_at FROM runs "
                    "WHERE run_type = 'weekly' ORDER BY started_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                latest_weekly_run = str(row[0]) if row and row[0] else None

    except Exception as exc:
        log.error("[api/search-terms-verdict] DB error: %s", exc, exc_info=True)
        return {
            "generated_at": generated_at,
            "days": days,
            "verdict": Verdict.DB_UNAVAILABLE,
            "reason": "Database error occurred",
            "db": db_info,
            "sync": sync_info,
            "api": {"checked": False, "rows_returned": None, "total_rows_in_window": 0, "is_empty": True},
            "next_action": "Fix database connection before checking Search Terms pipeline.",
        }

    # Determine the row count for the requested window
    if days in (7, 14, 30, 60):
        db_rows_window = db_info.get(f"rows_{days}d", 0)
    else:
        # Use the real COUNT(*) query result for non-standard windows
        db_rows_window = db_info.get("rows_requested", 0)

    verdict_str, reason = compute_search_terms_verdict(
        db_available=True,
        db_rows_window=db_rows_window,
        window_days=days,
        sync_status=sync_info.get("sync_state_status"),
        latest_weekly_run=latest_weekly_run,
    )

    # Determine next action
    next_actions = {
        Verdict.OK: "Search Terms pipeline is healthy. Proceed to Waste Terms/N-Grams confidence.",
        Verdict.NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT: "Run scheduler (daily or weekly) to trigger first Search Terms sync after deployment.",
        Verdict.WINDSOR_PULL_EMPTY: "Verify Windsor plan/API access or use MCP payload import path.",
        Verdict.WINDSOR_PULL_MISSING_SEARCH_TERM_FIELD: "Check Windsor field mapping — search_term field not present in response.",
        Verdict.FILE_EMPTY: "Check Windsor pull — ads_search_terms.json is empty.",
        Verdict.DB_WRITE_FAILED: "Check write_search_terms() — Windsor returned rows but DB has none.",
        Verdict.DB_HAS_ROWS_API_EMPTY: "Check /api/search-terms endpoint filtering — DB has rows but API returns empty.",
        Verdict.FRESH_BUT_EMPTY: "Sync reports success but zero rows found. Check Windsor pull or scheduler logic.",
        Verdict.DB_UNAVAILABLE: "Fix database connection before checking Search Terms pipeline.",
        Verdict.UNKNOWN: "Unable to determine pipeline state. Run verify_search_terms_pipeline.py manually.",
    }
    next_action = next_actions.get(verdict_str, next_actions[Verdict.UNKNOWN])

    return {
        "generated_at": generated_at,
        "days": days,
        "verdict": verdict_str,
        "reason": reason,
        "db": db_info,
        "sync": sync_info,
        "api": {
            "checked": False,
            "rows_returned": None,
            "total_rows_in_window": db_rows_window,
            "is_empty": db_rows_window == 0,
        },
        "next_action": next_action,
    }


@app.get("/api/system/search-terms-verdict")
def api_system_search_terms_verdict(
    request: Request,
    days: int = Query(default=60, description="Time window in days (1–90)"),
) -> dict[str, Any]:
    """Return a focused Search Terms production verdict.

    Admin only. Reports pipeline health: OK, empty, DB-broken, API-broken,
    or not yet run after deployment. Waste Terms and N-Grams depend on this.

    Does NOT call Google Ads, HubSpot, Windsor, or any external service.
    Does NOT mutate any data.
    Phase 1 read-only. PR-ADS-066.
    """
    check_admin_or_token(request)

    days = max(1, min(90, days))

    now_ts = datetime.now(timezone.utc).timestamp()
    with _search_terms_verdict_cache_lock:
        cached = _search_terms_verdict_cache.get(days)
        if cached and now_ts < cached["expires_at"]:
            return cached["data"]

    result = _build_search_terms_verdict(days)

    with _search_terms_verdict_cache_lock:
        _search_terms_verdict_cache[days] = {
            "expires_at": now_ts + _SEARCH_TERMS_VERDICT_CACHE_TTL_SECONDS,
            "data": result,
        }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM STATUS WAR ROOM (PR-ADS-068)
# ═══════════════════════════════════════════════════════════════════════════════

_WAR_ROOM_CACHE_TTL_SECONDS = 60
_war_room_cache_lock = threading.Lock()
_war_room_cache: dict[int, dict[str, Any]] = {}


@app.get("/api/system/status-war-room")
def api_system_status_war_room(
    request: Request,
    days: int = Query(default=60, description="Time window in days (1–90)"),
) -> dict[str, Any]:
    """Return consolidated system status war room.

    Admin only. Combines canonical freshness, pipeline dependencies,
    source health, scheduler state, and critical blockers into one view.

    Does NOT call Google Ads, HubSpot, Windsor, or any external service.
    Does NOT mutate any data.
    Phase 1 read-only. PR-ADS-068.
    """
    check_admin_or_token(request)

    days = max(1, min(90, days))

    now_ts = datetime.now(timezone.utc).timestamp()
    with _war_room_cache_lock:
        cached = _war_room_cache.get(days)
        if cached and now_ts < cached["expires_at"]:
            return cached["data"]

    from services.freshness_service import (  # noqa: PLC0415
        DATASET_FRESHNESS_CONFIG,
        BLOCKING_STATES,
        compute_canonical_freshness,
    )
    from services.system_status_service import (  # noqa: PLC0415
        build_war_room_response,
    )
    from db.connection import get_conn  # noqa: PLC0415
    from psycopg2 import sql as _psql  # noqa: PLC0415

    # ── Gather data from DB ────────────────────────────────────────────────
    dataset_statuses: dict[str, str] = {}
    dataset_details: dict[str, dict[str, Any]] = {}
    sync_source_info: dict[str, dict[str, Any]] = {}
    runs_data: dict[str, Any] = {}

    try:
        with get_conn() as conn:
            if conn is None:
                # DB unavailable — mark all as db_unavailable
                for cfg_key in DATASET_FRESHNESS_CONFIG:
                    dataset_statuses[cfg_key] = "db_unavailable"
                result = build_war_room_response(
                    days=days,
                    dataset_statuses=dataset_statuses,
                    dataset_details=dataset_details,
                    sync_info=sync_source_info,
                    runs_data=runs_data,
                )
                store_ts = datetime.now(timezone.utc).timestamp()
                with _war_room_cache_lock:
                    _war_room_cache[days] = {
                        "expires_at": store_ts + _WAR_ROOM_CACHE_TTL_SECONDS,
                        "data": result,
                    }
                return result

            with conn.cursor() as cur:
                # 1. Get sync_state
                cur.execute("""
                    SELECT source, dataset, status, last_successful_sync_at,
                           last_source_date, last_batch_id, error_message, updated_at
                    FROM sync_state
                    ORDER BY source, dataset
                """)
                sync_rows = cur.fetchall()
                sync_cols = [d[0] for d in cur.description]
                sync_map: dict[tuple[str, str], dict] = {}
                for row in sync_rows:
                    r = dict(zip(sync_cols, row))
                    sync_map[(r["source"], r["dataset"])] = r

                # 2. Get row counts per dataset
                window_start = date.today() - timedelta(days=days)
                row_counts: dict[str, int | None] = {}
                _count_cache: dict[tuple[str, str], int | None] = {}
                for cfg_key, cfg in DATASET_FRESHNESS_CONFIG.items():
                    table_name = str(cfg.get("table") or "")
                    date_column = str(cfg.get("date_column") or "")
                    if not table_name or not date_column:
                        continue
                    if not (_SAFE_SQL_IDENTIFIER_RE.match(table_name) and _SAFE_SQL_IDENTIFIER_RE.match(date_column)):
                        log.warning(
                            "[api/status-war-room] invalid identifier for dataset %r: table=%r date_column=%r",
                            cfg_key, table_name, date_column,
                        )
                        continue
                    cache_key = (table_name, date_column)
                    if cache_key in _count_cache:
                        row_counts[cfg_key] = _count_cache[cache_key]
                        continue
                    try:
                        query = _psql.SQL("SELECT COUNT(*) FROM {} WHERE {} >= %s").format(
                            _psql.Identifier(table_name),
                            _psql.Identifier(date_column),
                        )
                        cur.execute(query, (window_start,))
                        result_row = cur.fetchone()
                        count = int(result_row[0]) if result_row and result_row[0] is not None else 0
                        _count_cache[cache_key] = count
                        row_counts[cfg_key] = count
                    except Exception:  # noqa: BLE001
                        _count_cache[cache_key] = None
                        row_counts[cfg_key] = None

                # 3. Get latest batch info — one row per source (most recent by started_at)
                latest_batch_by_source: dict[str, dict] = {}
                try:
                    cur.execute("""
                        SELECT DISTINCT ON (source)
                            source, dataset, status, row_count
                        FROM sync_batches
                        ORDER BY source, started_at DESC
                    """)
                    bcols = [d[0] for d in cur.description]
                    for brow in cur.fetchall():
                        br = dict(zip(bcols, brow))
                        latest_batch_by_source[br["source"]] = {
                            "dataset": br["dataset"],
                            "status": br["status"],
                            "row_count": br["row_count"],
                        }
                    # Also keep per-(source, dataset) map for canonical freshness computation
                    latest_batch_info: dict[tuple[str, str], dict] = {}
                    cur.execute("""
                        SELECT DISTINCT ON (source, dataset)
                            source, dataset, status, row_count
                        FROM sync_batches
                        ORDER BY source, dataset, started_at DESC
                    """)
                    bcols2 = [d[0] for d in cur.description]
                    for brow in cur.fetchall():
                        br = dict(zip(bcols2, brow))
                        latest_batch_info[(br["source"], br["dataset"])] = {
                            "status": br["status"],
                            "row_count": br["row_count"],
                        }
                except Exception:  # noqa: BLE001
                    latest_batch_info = {}
                    latest_batch_by_source = {}

                # 4. Compute canonical freshness for each dataset
                canonical_status_map: dict[str, str] = {}

                # First pass: non-dependent
                for cfg_key, cfg in DATASET_FRESHNESS_CONFIG.items():
                    if cfg.get("depends_on"):
                        continue
                    src = cfg["source"]
                    ds = cfg["dataset"]
                    sync_row = sync_map.get((src, ds), {})

                    sync_status = sync_row.get("status")
                    if (
                        sync_status == "unknown"
                        and sync_row.get("last_successful_sync_at") is None
                        and sync_row.get("last_source_date") is None
                        and sync_row.get("last_batch_id") is None
                    ):
                        sync_status = None

                    last_sync_at = sync_row.get("last_successful_sync_at")
                    latest_src_date = None
                    if sync_row.get("last_source_date"):
                        try:
                            latest_src_date = date.fromisoformat(str(sync_row["last_source_date"]))
                        except (ValueError, TypeError):
                            pass

                    batch_info = latest_batch_info.get((src, ds), {})
                    verdict = compute_canonical_freshness(
                        dataset=cfg_key,
                        rows_in_window=row_counts.get(cfg_key),
                        latest_source_date=latest_src_date,
                        sync_status=sync_status,
                        latest_batch_status=batch_info.get("status"),
                        latest_batch_row_count=batch_info.get("row_count"),
                        last_successful_sync_at=last_sync_at,
                        stale_threshold_days=cfg.get("stale_threshold_days", 8),
                        dependency_status=None,
                    )
                    canonical_status_map[cfg_key] = verdict["canonical_status"]
                    dataset_details[cfg_key] = {
                        "rows_in_window": row_counts.get(cfg_key),
                        "latest_source_date": str(sync_row.get("last_source_date")) if sync_row.get("last_source_date") else None,
                        "last_batch_row_count": batch_info.get("row_count", 0),
                        "reason": verdict.get("reason", ""),
                        "next_action": verdict.get("next_action", ""),
                    }

                # Second pass: dependent datasets
                for cfg_key, cfg in DATASET_FRESHNESS_CONFIG.items():
                    if not cfg.get("depends_on"):
                        continue
                    src = cfg["source"]
                    ds = cfg["dataset"]
                    sync_row = sync_map.get((src, ds), {})

                    sync_status = sync_row.get("status")
                    if (
                        sync_status == "unknown"
                        and sync_row.get("last_successful_sync_at") is None
                        and sync_row.get("last_source_date") is None
                        and sync_row.get("last_batch_id") is None
                    ):
                        sync_status = None

                    last_sync_at = sync_row.get("last_successful_sync_at")
                    latest_src_date = None
                    if sync_row.get("last_source_date"):
                        try:
                            latest_src_date = date.fromisoformat(str(sync_row["last_source_date"]))
                        except (ValueError, TypeError):
                            pass

                    batch_info = latest_batch_info.get((src, ds), {})

                    dep_status = None
                    for dep in cfg["depends_on"]:
                        dep_st = canonical_status_map.get(dep)
                        if dep_st and dep_st in BLOCKING_STATES:
                            dep_status = dep_st
                            break

                    verdict = compute_canonical_freshness(
                        dataset=cfg_key,
                        rows_in_window=row_counts.get(cfg_key),
                        latest_source_date=latest_src_date,
                        sync_status=sync_status,
                        latest_batch_status=batch_info.get("status"),
                        latest_batch_row_count=batch_info.get("row_count"),
                        last_successful_sync_at=last_sync_at,
                        stale_threshold_days=cfg.get("stale_threshold_days", 8),
                        dependency_status=dep_status,
                    )
                    canonical_status_map[cfg_key] = verdict["canonical_status"]
                    dataset_details[cfg_key] = {
                        "rows_in_window": row_counts.get(cfg_key),
                        "latest_source_date": str(sync_row.get("last_source_date")) if sync_row.get("last_source_date") else None,
                        "last_batch_row_count": batch_info.get("row_count", 0),
                        "reason": verdict.get("reason", ""),
                        "next_action": verdict.get("next_action", ""),
                    }

                dataset_statuses = canonical_status_map

                # 5. Source sync info
                for src_key in ["windsor", "hubspot", "gclid", "analysis", "computed"]:
                    # Find best last_successful_sync_at for this source
                    best_sync = None
                    for (s, d), srow in sync_map.items():
                        if s == src_key:
                            lsa = srow.get("last_successful_sync_at")
                            if lsa and (best_sync is None or lsa > best_sync):
                                best_sync = lsa
                    # Use latest batch (by started_at DESC) for this source
                    batch_status = latest_batch_by_source.get(src_key, {}).get("status")

                    sync_source_info[src_key] = {
                        "last_successful_sync_at": best_sync.isoformat() if best_sync else None,
                        "latest_batch_status": batch_status,
                    }

                # 6. Scheduler runs
                for run_type in ["daily", "weekly", "monthly"]:
                    try:
                        cur.execute(
                            "SELECT status, started_at, finished_at FROM runs "
                            "WHERE run_type = %s ORDER BY started_at DESC LIMIT 1",
                            (run_type,),
                        )
                        row = cur.fetchone()
                        if row:
                            runs_data[run_type] = {
                                "status": row[0],
                                "started_at": row[1].isoformat() if row[1] else None,
                                "finished_at": row[2].isoformat() if row[2] else None,
                            }
                    except Exception:  # noqa: BLE001
                        pass

                # Incremental sync — check if run_type exists
                try:
                    cur.execute(
                        "SELECT status, started_at, finished_at FROM runs "
                        "WHERE run_type = 'incremental' ORDER BY started_at DESC LIMIT 1",
                    )
                    row = cur.fetchone()
                    if row:
                        runs_data["incremental"] = {
                            "status": row[0],
                            "started_at": row[1].isoformat() if row[1] else None,
                            "finished_at": row[2].isoformat() if row[2] else None,
                        }
                except Exception:  # noqa: BLE001
                    pass

    except Exception as exc:  # noqa: BLE001
        log.error("[api/status-war-room] database error: %s", exc, exc_info=True)
        for cfg_key in DATASET_FRESHNESS_CONFIG:
            dataset_statuses[cfg_key] = "db_unavailable"

    result = build_war_room_response(
        days=days,
        dataset_statuses=dataset_statuses,
        dataset_details=dataset_details,
        sync_info=sync_source_info,
        runs_data=runs_data,
    )

    store_ts = datetime.now(timezone.utc).timestamp()
    with _war_room_cache_lock:
        _war_room_cache[days] = {
            "expires_at": store_ts + _WAR_ROOM_CACHE_TTL_SECONDS,
            "data": result,
        }
    return result


# ---------------------------------------------------------------------------
# ROAS & Revenue Truth Endpoints (PR-ADS-080A)
# ---------------------------------------------------------------------------

def _parse_window(window: str) -> int:
    """Parse window query parameter (e.g., '60d') to integer days."""
    valid_windows = {"7d": 7, "14d": 14, "30d": 30, "60d": 60, "90d": 90, "365d": 365}
    if window in valid_windows:
        return valid_windows[window]
    raise HTTPException(
        status_code=400,
        detail="Invalid window. Valid values: 7d, 14d, 30d, 60d, 90d, 365d",
    )


def _dominant_attribution_from_campaigns(campaigns: list[dict]) -> str:
    """Derive overall attribution confidence, weighted by deals_won."""
    weighted = {}
    for campaign in campaigns:
        confidence = campaign.get("attribution_confidence") or "tier_3_spend_weighted"
        weight = campaign.get("deals_won", 0)
        try:
            weight = int(weight)
        except (ValueError, TypeError):
            weight = 0
        if weight <= 0:
            continue
        weighted[confidence] = weighted.get(confidence, 0) + weight

    if not weighted:
        return "tier_3_spend_weighted"
    return max(weighted, key=weighted.get)


@app.get("/api/reports/roas/campaigns")
async def get_roas_campaigns(
    window: str = Query(default="60d"),
    _user=Depends(require_auth),
):
    """Campaign-level ROAS report.

    Revenue source: HubSpot won deals.
    Spend source: Windsor / Google Ads.
    Google Ads conversion value is NOT used.
    """
    from analysis.roas_calculator import compute_all_campaign_roas

    window_days = _parse_window(window)

    try:
        campaigns = compute_all_campaign_roas(window_days=window_days)
    except Exception as exc:
        log.error("ROAS campaign computation failed: %s", exc)
        raise HTTPException(status_code=500, detail="ROAS computation failed") from exc

    return {
        "window": f"{window_days}d",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_truth": "hubspot_won_deals_plus_windsor_spend",
        "google_ads_conversion_value_used": False,
        "campaigns": campaigns,
    }


@app.get("/api/reports/roas/countries")
async def get_roas_countries(
    window: str = Query(default="60d"),
    _user=Depends(require_auth),
):
    """Country-level ROAS report.

    Hard rule: country_level_estimate = true until GCLID is fully wired.
    All rows include attribution_confidence.
    """
    from analysis.roas_calculator import compute_all_country_roas

    window_days = _parse_window(window)

    try:
        countries = compute_all_country_roas(window_days=window_days)
    except Exception as exc:
        log.error("ROAS country computation failed: %s", exc)
        raise HTTPException(status_code=500, detail="ROAS computation failed") from exc

    return {
        "window": f"{window_days}d",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_truth": "hubspot_won_deals_plus_windsor_spend",
        "google_ads_conversion_value_used": False,
        "country_level_estimate": True,
        "countries": countries,
    }


@app.get("/api/reports/unit-economics")
async def get_unit_economics(
    window: str = Query(default="60d"),
    _user=Depends(require_auth),
):
    """Unit economics report: LTV/CAC, payback, avg deal values."""
    from analysis.roas_calculator import compute_all_campaign_roas
    from connectors.hubspot_churn import get_monthly_churn

    window_days = _parse_window(window)

    try:
        campaigns = compute_all_campaign_roas(window_days=window_days)
    except Exception as exc:
        log.error("Unit economics computation failed: %s", exc)
        raise HTTPException(status_code=500, detail="Unit economics computation failed") from exc

    # Aggregate overall
    total_spend = sum(c.get("spend", 0) for c in campaigns)
    total_deals = sum(c.get("deals_won", 0) for c in campaigns)
    total_acv = sum(c.get("acv_revenue", 0) for c in campaigns)
    total_mrr = sum(c.get("mrr_revenue", 0) for c in campaigns)
    total_ltv = sum(c.get("ltv_revenue", 0) for c in campaigns)

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    churn_info = get_monthly_churn(current_month)
    churn_rate = churn_info["monthly_churn_rate"]

    from analysis.unit_economics import compute_cac, compute_ltv_to_cac, compute_payback_months

    avg_deal_acv = round(total_acv / total_deals) if total_deals > 0 else 0
    avg_deal_mrr = round(total_mrr / total_deals) if total_deals > 0 else 0
    avg_ltv = total_ltv / total_deals if total_deals > 0 else 0

    cac = compute_cac(total_spend, total_deals)
    ltv_cac = compute_ltv_to_cac(avg_ltv, cac) if cac else None
    payback = compute_payback_months(cac, avg_deal_mrr) if cac and avg_deal_mrr > 0 else None

    # Overall verdict
    from analysis.roas_calculator import compute_verdict
    overall_attribution = _dominant_attribution_from_campaigns(campaigns)
    overall_verdict = compute_verdict(ltv_cac, payback, total_deals, overall_attribution)

    return {
        "window": f"{window_days}d",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": {
            "ltv_to_cac": round(ltv_cac, 2) if ltv_cac else None,
            "payback_months": round(payback, 1) if payback else None,
            "avg_deal_acv": avg_deal_acv,
            "avg_deal_mrr": avg_deal_mrr,
            "monthly_churn_rate_used": churn_rate,
            "verdict": overall_verdict,
        },
        "by_campaign": campaigns,
    }


@app.get("/api/admin/churn-input")
async def get_churn_input(
    _user=Depends(check_admin_or_token),
):
    """Return current churn configuration as JSON."""
    from connectors.hubspot_churn import load_churn_input

    try:
        config = load_churn_input()
    except Exception as exc:
        log.error("Failed to load churn config: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load churn config") from exc

    return config


class ChurnInputUpdate(BaseModel):
    month: str
    rate: float


@app.post("/api/admin/churn-input")
async def update_churn_input(
    payload: ChurnInputUpdate,
    _user=Depends(check_admin_or_token),
):
    """Update monthly churn rate in config/churn_input.yaml.

    Validation:
      - Month format must be YYYY-MM.
      - Rate must be 0 <= rate <= 1.
      - Local config write only. No HubSpot write.
    """
    # Validate month format
    if not re.match(r"^\d{4}-\d{2}$", payload.month):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM format")

    # Validate rate bounds
    if payload.rate < 0 or payload.rate > 1:
        raise HTTPException(
            status_code=400,
            detail="rate must be between 0 and 1",
        )

    # Update local YAML
    config_path = Path(__file__).parent.parent / "config" / "churn_input.yaml"

    try:
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        else:
            config = {"default_monthly_churn": 0.03, "monthly": {}, "campaign_overrides": {}}

        if "monthly" not in config:
            config["monthly"] = {}

        config["monthly"][payload.month] = payload.rate

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    except Exception as exc:
        log.error("Failed to update churn config: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to update churn config") from exc

    return {"ok": True}
