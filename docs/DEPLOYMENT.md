# Logistaas Ads Intelligence System — Deployment Guide

## Overview

This document covers the Render.com deployment for the Logistaas Ads Intelligence System.

Phase 1 deploys a **single Render Web Service** that hosts the UI, API, manual run
endpoints, and all Phase 1 scheduled jobs (daily, weekly, monthly).

> ⚠️ **Warning:** Do NOT enable both Render cron jobs and the in-app scheduler at
> the same time. Duplicate runs will generate duplicate reports and emails.
> The Render cron jobs (`logistaas-daily-pulse`, `logistaas-weekly-report`,
> `logistaas-monthly-strategy`) are **decommissioned** and must not be re-enabled
> while the in-app scheduler is active.

---

## Architecture

```
Render.com
 └── logistaas-ads-intelligence  (web service)
       python -m uvicorn api.server:app --host 0.0.0.0 --port $PORT
       ├── GET  /                    — Dashboard UI (auth state managed by JS)
       ├── GET  /health              — Liveness check (public)
       ├── POST /auth/login          — Login with username + password
       ├── POST /auth/logout         — Clear session cookie
       ├── GET  /auth/me             — Current user info (requires auth)
       ├── GET  /readiness           — Structured readiness check (requires admin)
       ├── GET  /scheduler/status    — In-app scheduler state (requires auth)
       ├── GET  /runs/latest         — Latest run history record (requires auth)
       ├── GET  /reports/latest      — Latest report metadata (requires auth)
       ├── GET  /reports/latest/raw  — Raw report content (requires auth)
       ├── POST /run/daily           — Trigger daily pulse (requires admin)
       ├── POST /run/weekly          — Trigger weekly report (requires admin)
       └── POST /run/monthly         — Trigger monthly report (requires admin)

In-app scheduler (APScheduler, runs inside the web service process):
  Daily pulse      — every day at 06:00 Asia/Amman (03:00 UTC)
  Weekly report    — every Monday at 07:00 Asia/Amman (04:00 UTC)
  Monthly strategy — 1st of month at 08:00 Asia/Amman (05:00 UTC)
```

All services are defined in `render.yaml` at the repo root.

---

## Web Service Deployment (PR-ADS-016 / PR-ADS-017 / PR-ADS-019)

### Render settings

| Field | Value |
|-------|-------|
| **Name** | `logistaas-ads-intelligence` |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python -m uvicorn api.server:app --host 0.0.0.0 --port $PORT` |
| **Health Check Path** | `/health` |
| **Root Directory** | *(empty — repo root)* |
| **Auto Deploy** | On Commit |
| **Instance** | Free for testing; Starter recommended once validation starts |

> **Note:** The Free instance type is acceptable for initial testing only.
> Switch to a paid Starter instance once validation and reporting workflows begin.
> The in-app scheduler requires the process to stay alive between runs — use a
> Starter or higher instance in production to prevent Render from spinning down
> the service.

### Available endpoints

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | None | Liveness check — returns `{"status": "ok"}` |
| `POST /auth/login` | None | Login with username + password; sets session cookie |
| `POST /auth/logout` | None | Clear session cookie |
| `GET /auth/me` | Cookie session | Return current user info |
| `GET /readiness` | Admin | Structured check — dirs, config files, docs, core imports |
| `GET /scheduler/status` | Any auth | In-app scheduler state and next run times (read-only) |
| `GET /runs/latest` | Any auth | Latest record from `runtime_logs/run_history.jsonl` |
| `GET /reports/latest` | Any auth | Metadata for the most recent file in `outputs/` |
| `GET /reports/latest/raw` | Any auth | Raw markdown content of the latest report |
| `POST /run/daily` | Admin (cookie or Bearer) | Trigger daily pulse scheduler |
| `POST /run/weekly` | Admin (cookie or Bearer) | Trigger weekly report scheduler |
| `POST /run/monthly` | Admin (cookie or Bearer) | Trigger monthly report scheduler |

Unauthenticated requests to protected endpoints return **401**.
Authenticated requests with insufficient role return **403**.

### Local verification

```bash
# Syntax check
python -m py_compile api/server.py
python -m py_compile api/scheduler.py

# Start locally
export ADMIN_API_TOKEN=test-token
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000

# Test read-only endpoints (no auth required)
curl http://localhost:8000/health
curl http://localhost:8000/readiness
curl http://localhost:8000/scheduler/status
curl http://localhost:8000/runs/latest
curl http://localhost:8000/reports/latest

# Test authorized run (returns structured JSON)
curl -X POST http://localhost:8000/run/daily \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"

# Test unauthorized run (returns 401)
curl -X POST http://localhost:8000/run/daily

# Test missing token env (unset ADMIN_API_TOKEN, returns 503)
unset ADMIN_API_TOKEN
curl -X POST http://localhost:8000/run/daily
```

---

## Required Environment Variables

Set each variable in the Render dashboard under **Service → Environment** for the web service.

| Variable | Required | Description |
|----------|----------|-------------|
| `APP_SECRET_KEY` | ✅ required | Long random secret for signing session cookies — `openssl rand -hex 32` |
| `AUTH_USERS_JSON` | ✅ required | JSON array of users — see "Creating Users" below |
| `HUBSPOT_API_KEY` | ✅ required | HubSpot private app token |
| `WINDSOR_API_KEY` | ✅ required | Windsor.ai API key |
| `WINDSOR_ACCOUNT_ID` | ✅ required | Windsor.ai account identifier |
| `ADVISOR_MODE` | ⬜ optional | `deterministic` (default) or `claude` |
| `ANTHROPIC_API_KEY` | ⬜ optional | Required only when `ADVISOR_MODE=claude` |
| `ADMIN_API_TOKEN` | ⬜ optional | For API-only manual triggers without dashboard login |
| `SENDGRID_API_KEY` | ⬜ optional | For email delivery of reports |
| `REPORT_SENDER_EMAIL` | ⬜ optional | Verified sender address |
| `REPORT_RECIPIENT_EMAIL` | ⬜ optional | Report delivery recipient |

> **Security warning:** `APP_SECRET_KEY` must be a long random secret (at least 32 bytes).
> Never commit it to source control. Rotating this key will invalidate all active sessions.

> **Local development:** Set `APP_ENV=development` in your local `.env` file.
> This disables the `Secure` flag on session cookies, allowing them to work over HTTP.
> On Render (HTTPS), always use `APP_ENV=production`.

> **Note:** `GOOGLE_ADS_*` variables are reserved for Phase 2+. Do not configure until available.

---

## Deterministic Advisor

By default, `ADVISOR_MODE=deterministic`. Reports are generated from structured analysis outputs
(`outputs/waste_report.json`, `outputs/lead_quality.json`, `outputs/campaign_truth.json`)
without calling any external AI model.

Claude API is optional. To switch to Claude mode:

```bash
ADVISOR_MODE=claude
ANTHROPIC_API_KEY=sk-ant-...
```

If `ADVISOR_MODE=claude` is set but `ANTHROPIC_API_KEY` is missing, the scheduler will fail
with a clear error message and suggest switching to deterministic mode.

---

## Internal User Authentication

The dashboard requires login. No external auth provider or database is needed.
Users and roles are configured entirely via `AUTH_USERS_JSON` on Render.

### User roles

| Role | Access |
|------|--------|
| `admin` | Full access — dashboard, reports, run history, scheduler status, manual run triggers |
| `viewer` | Read-only — dashboard, reports, run history, scheduler status |
| `mdr` | Limited read-only — dashboard, reports only |

### Creating users

Generate a password hash for each user:

```bash
python scripts/create_user_hash.py
```

This tool prompts for a username, password, and role, then outputs a JSON entry.
The password is never stored or displayed.

Example output:

```json
{
  "username": "youssef",
  "password_hash": "pbkdf2_sha256$260000$<salt>$<hash>",
  "role": "admin"
}
```

### Setting AUTH_USERS_JSON on Render

Combine all user entries into a JSON array and set it as the `AUTH_USERS_JSON` environment variable:

```json
[
  {"username":"youssef","password_hash":"pbkdf2_sha256$260000$...","role":"admin"},
  {"username":"kareem","password_hash":"pbkdf2_sha256$260000$...","role":"viewer"},
  {"username":"mdr","password_hash":"pbkdf2_sha256$260000$...","role":"mdr"}
]
```

> ⚠️ `AUTH_USERS_JSON` must be a **single-line** JSON string in the Render dashboard.
> Do not include line breaks or indent the JSON when setting the env var.

> ⚠️ Never commit password hashes to source control. Store them only as Render env vars.

### Auth behavior

- Login: `POST /auth/login` with `{"username": "...", "password": "..."}`
- Session stored in HTTP-only signed cookie (8-hour expiry)
- `GET /auth/me` returns `{"username": "...", "role": "..."}`
- `POST /auth/logout` clears the session cookie
- 401 returned for unauthenticated requests to protected endpoints
- 403 returned for authenticated requests with insufficient role

---

## Deployment Steps

### 1. Connect repository to Render

1. Log into [render.com](https://render.com) → **New** → **Blueprint**.
2. Connect the `Logistaas-averroes/Averroes` GitHub repository.
3. Render will detect `render.yaml` and preview **one web service**.
4. Click **Apply**.

### 2. Set environment variables

Open the web service (`logistaas-ads-intelligence`) → **Environment** tab.
Add each variable from the table above.
Click **Save Changes**.

Refer to `.env.example` for the full variable reference.

> ⚠️ If you previously deployed the three Render cron services
> (`logistaas-daily-pulse`, `logistaas-weekly-report`, `logistaas-monthly-strategy`),
> **delete or suspend them** before deploying this version.
> Running both cron services and the in-app scheduler simultaneously will cause
> duplicate reports and duplicate email delivery.

### 3. Verify the in-app scheduler

After deploy, confirm the scheduler started:

```bash
curl https://<service>.onrender.com/scheduler/status
```

Expected response:

```json
{
  "status": "running",
  "jobs": [
    {"job": "daily",   "schedule": "06:00 Asia/Amman (03:00 UTC)",            "next_run": "..."},
    {"job": "weekly",  "schedule": "Monday 07:00 Asia/Amman (04:00 UTC)",     "next_run": "..."},
    {"job": "monthly", "schedule": "1st of month 08:00 Asia/Amman (05:00 UTC)", "next_run": "..."}
  ]
}
```

### 4. Trigger a manual run via API

With `ADMIN_API_TOKEN` set on the web service, you can trigger jobs remotely:

```bash
# Weekly report
curl -X POST https://<service>.onrender.com/run/weekly \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"

# Daily pulse
curl -X POST https://<service>.onrender.com/run/daily \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"

# Monthly strategy
curl -X POST https://<service>.onrender.com/run/monthly \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

> ⚠️ **Warning:** Manual run endpoints and the in-app scheduler share the same in-memory
> lock. A manual run while the scheduler is executing (or vice versa) will return HTTP 409
> for the second caller. This is by design to prevent duplicate runs.

---

## In-App Scheduler Reference

| Job | Schedule (Asia/Amman) | UTC Equivalent |
|-----|-----------------------|----------------|
| Daily pulse | Every day at 06:00 | 03:00 UTC |
| Weekly report | Every Monday at 07:00 | Monday 04:00 UTC |
| Monthly strategy | 1st of month at 08:00 | 1st of month 05:00 UTC |

**Timezone note:** Asia/Amman = UTC+3 year-round. Jordan suspended daylight saving
time in 2022, so no DST offset applies.

**Overlap prevention:** The in-app scheduler and manual `/run/*` endpoints share the
same in-memory lock per job type. If a job is already running (scheduled or manual),
a second trigger is rejected (HTTP 409 for manual; silent skip with a log warning
for scheduled).

**Misfire grace window:** 1 hour. If the process restarts within 1 hour of a missed
scheduled run time, the job will execute immediately on startup.

---

## Live Deployment Verification

After deploying to Render, verify the live service from the outside:

```bash
SERVICE_URL=https://your-service.onrender.com python scripts/verify_live_deployment.py
```

Optional daily trigger (only if you want to test the authenticated run endpoint):

```bash
SERVICE_URL=https://your-service.onrender.com ADMIN_API_TOKEN=xxx python scripts/verify_live_deployment.py --trigger-daily
```

Or via Makefile:

```bash
SERVICE_URL=https://your-service.onrender.com make verify-live
```

**Live deployment checklist:**

- [ ] Render Web Service deploys successfully
- [ ] `/health` returns OK
- [ ] `/readiness` returns structured status
- [ ] `/scheduler/status` returns jobs
- [ ] dashboard loads at `/`
- [ ] protected run endpoints reject missing/wrong tokens
- [ ] manual daily trigger works with valid token
- [ ] no separate Render cron services are active
- [ ] first report delivery checked via logs/email

---

## Post-Merge Verification Checklist

After deploying, confirm the following:

- [ ] **Live verification passes** — run `make verify-live`; all checks show PASS
- [ ] **Healthcheck passes** — run `make healthcheck`; all critical checks show PASS
- [ ] **Phase 1 validation passes** — run `make validate`; no stale references reported
- [ ] **Scheduler running** — `GET /scheduler/status` returns `"status": "running"` with all three jobs and non-null `next_run` values
- [ ] **UI loads scheduler status** — dashboard shows scheduler card with "running" badge and next run times
- [ ] **Old cron jobs not active** — confirm `logistaas-daily-pulse`, `logistaas-weekly-report`, `logistaas-monthly-strategy` are deleted or suspended in Render dashboard
- [ ] **Manual run endpoints still work** — trigger a run via UI or curl; returns 200 or 409 (if scheduler is executing)
- [ ] **Lock prevents overlap** — triggering a run while scheduler is active returns HTTP 409

---

## Failure Handling

### Missing environment variables

| Condition | Behaviour |
|-----------|-----------|
| `ANTHROPIC_API_KEY` missing | `analysis/advisor.py` raises `AuthenticationError` on first Claude call; run exits with traceback |
| `HUBSPOT_API_KEY` missing | `connectors/hubspot_pull.py` returns empty contacts list; downstream steps proceed with no data |
| `WINDSOR_API_KEY` / `WINDSOR_ACCOUNT_ID` missing | `connectors/windsor_pull.py` returns empty campaigns list; downstream steps proceed with no data |
| `SENDGRID_API_KEY` missing | `scheduler/delivery.py` logs `[DELIVERY FAILURE] SENDGRID_API_KEY is not set` and returns `False`; scheduler still exits cleanly |
| `REPORT_SENDER_EMAIL` missing | Same as above — delivery skipped, run does not fail |
| `REPORT_RECIPIENT_EMAIL` missing | Same as above |

### API failures during cron run

| Condition | Behaviour |
|-----------|-----------|
| Windsor.ai API returns error / timeout | Monthly and weekly schedulers catch the exception, log `Step 1/6 FAILED`, and return `None` (run aborts cleanly) |
| HubSpot API returns 429 rate limit | `hubspot_pull.py` retries with exponential back-off; if retries exhausted, raises exception caught by monthly scheduler |
| HubSpot API returns other error | Same retry logic; ultimately caught by monthly scheduler |
| Claude API returns unexpected format | `analysis/advisor.py` returns `None`; monthly scheduler logs `Step 6/6 FAILED` |
| SendGrid delivery fails | `delivery.py` logs `[DELIVERY FAILURE]` with HTTP status; returns `False`; does not raise |

All failures are logged to stdout, which Render captures and displays in the **Logs** tab.

---

## Rollback

To roll back to Render cron jobs:

1. Disable the in-app scheduler: set `DISABLE_SCHEDULER=true` environment variable
   (not yet implemented — rollback requires reverting this PR).
2. Revert this commit in Render → **Deploys** → select a previous deploy → **Redeploy**.
3. Re-enable the three cron services in the Render dashboard.

**Rollback risk level: Medium** — the in-app scheduler changes operational scheduling
behavior. Reverting does not affect data already written to `data/` or `outputs/`.

---

## Local Validation

To validate schedulers locally before deploying:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env with real credentials

# 3. Run the preflight healthcheck
make healthcheck
# or directly:
python scripts/healthcheck.py

# 4. Run Phase 1 end-to-end validation (syntax, YAML, docs, stale refs)
make validate
# or directly:
python scripts/validate_phase1.py

# 5. Run each scheduler manually
make daily
make weekly
make monthly
# or directly:
python scheduler/daily.py
python scheduler/weekly.py
python scheduler/monthly.py
```

### Makefile command reference

| Command | Runs | Purpose |
|---------|------|---------|
| `make healthcheck` | `python scripts/healthcheck.py` | Validate env vars, dirs, imports |
| `make daily` | `python -m scheduler.daily` | Run the daily pulse |
| `make weekly` | `python -m scheduler.weekly` | Run the weekly report |
| `make monthly` | `python -m scheduler.monthly` | Run the monthly report |
| `make validate` | `python scripts/validate_phase1.py` | Phase 1 read-only validation |
| `make runs` | `tail runtime_logs/run_history.jsonl` | Show last 20 run records |

To syntax-check without running:
```bash
python -m py_compile scheduler/daily.py
python -m py_compile scheduler/weekly.py
python -m py_compile scheduler/monthly.py
```

---

## Web UI Dashboard (PR-ADS-018)

The dashboard is served at `/` and provides:

- **System status cards** — health, readiness, latest run, latest report.
- **Latest Run panel** — reads from `/runs/latest`.
- **Latest Report panel** — reads from `/reports/latest`; raw report viewer calls `/reports/latest/raw`.
- **Manual Run Controls** — buttons for Daily, Weekly, Monthly. Require `ADMIN_API_TOKEN`.
- **Doctrine reminder** — Phase 1 read-only status always visible.

### Token security

- `ADMIN_API_TOKEN` is **only required for manual run buttons**. Health and readiness are public.
- The token is stored in `sessionStorage` only (cleared when the tab closes). It is never persisted to `localStorage` or cookies.
- Do **not** expose `ADMIN_API_TOKEN` publicly or commit it to source control.

### Local verification

```bash
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000
# Open http://localhost:8000/
```

---

## Non-Goals (Phase 1)

- No Slack delivery
- No retry queue
- No OCT uploads (requires `connectors/oct_uploader.py`)
- No background queue or database scheduler
- No separate Render cron jobs (decommissioned by PR-ADS-019)
