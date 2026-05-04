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
  GET  /api/geo                    — Windsor geo performance by country/campaign (requires auth).
  GET  /api/keywords               — Windsor keyword performance by campaign/ad group/keyword (requires auth).
  GET  /api/leads/country-summary  — HubSpot lead quality aggregated by country (requires auth).
  GET  /api/campaign-detail        — Campaign drill-down detail, query-param form (requires auth). Preferred.
  GET  /api/campaigns/{campaign_name}/detail — Campaign drill-down detail, path-segment form (requires auth). Legacy.
  GET  /api/config/ui-thresholds  — UI-safe display thresholds from config/thresholds.yaml (requires auth).
  GET  /api/dashboard/trends      — Previous-period trend comparison for dashboard (requires auth).
  GET  /api/action-queue          — Ranked human-review queue based on campaign, waste, geo, keyword, and data signals (requires auth).
"""

import hashlib
import importlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
