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
"""

import importlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.auth import (
    check_admin_or_token,
    clear_session,
    get_current_user,
    get_user,
    require_auth,
    set_session,
    verify_password,
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
    user = get_user(body.username)
    if not user or not verify_password(body.password, user.get("password_hash", "")):
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
