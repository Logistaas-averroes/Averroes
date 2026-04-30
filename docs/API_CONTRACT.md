# API Contract Reference
## Single source of truth for every endpoint in `api/server.py`

This file defines every HTTP endpoint the FastAPI server exposes, its auth requirement, request shape, and response shape.

**Rules:**
- Frontend code reads this file to understand what to call.
- Backend PRs that change an endpoint must update this file in the same PR.
- No new endpoint may exist in `api/server.py` that is not documented here.

---

## Auth Model

Authentication is session-cookie based. The cookie is HTTP-only, signed with `APP_SECRET_KEY`, and expires after 8 hours.

**Roles:**
- `admin` — full access including manual run triggers and readiness checks
- `viewer` — read-only dashboard, reports, run history, scheduler status
- `mdr` — limited read-only (dashboard + reports only)

**Access control:**
- `Public` — no authentication required
- `Auth` — any authenticated session
- `Admin` — admin role only (cookie or `ADMIN_API_TOKEN` Bearer token)

---

## Endpoints

### Public

#### `GET /health`
Liveness check. Always returns 200 when the service is up.

**Auth:** Public
**Response 200:**
```json
{ "status": "ok", "service": "logistaas-ads-intelligence" }
```

---

### Authentication

#### `POST /auth/login`
Sign in with username and password. Sets session cookie on success.

**Auth:** Public
**Request body:**
```json
{ "username": "youssef", "password": "..." }
```
**Response 200:**
```json
{ "username": "youssef", "role": "admin" }
```
**Response 401:** `{ "detail": "Invalid username or password" }`

---

#### `POST /auth/logout`
Clear session cookie.

**Auth:** Public (no-op if not signed in)
**Response 200:** `{ "status": "ok" }`

---

#### `GET /auth/me`
Return current authenticated user's username and role.

**Auth:** Auth
**Response 200:**
```json
{ "username": "youssef", "role": "admin" }
```
**Response 401:** `{ "detail": "Not authenticated" }`

---

### Read-Only Data

#### `GET /readiness`
Structured pre-flight check. Verifies directories, config files, docs, and core module imports.

**Auth:** Admin only
**Response 200:**
```json
{
  "status": "pass",
  "checks": {
    "directories":   { "data/": true, "outputs/": true },
    "config_files":  { "config/thresholds.yaml": true, "config/junk_patterns.yaml": true },
    "docs":          { "docs/DOCTRINE.md": true },
    "imports":       { "analysis.core": true, "scheduler.daily": true }
  }
}
```
`status` is `"pass"` if every check is true, otherwise `"fail"`.

---

#### `GET /runs/latest`
Return the most recent record from `runtime_logs/run_history.jsonl`.

**Auth:** Auth
**Response 200 (when history exists):**
```json
{
  "run_type": "weekly",
  "started_at": "2026-04-28T07:00:00.000000Z",
  "finished_at": "2026-04-28T07:04:23.000000Z",
  "status": "success",
  "failed_step": null,
  "error_message": null,
  "report_path": "outputs/weekly_report_2026-04-28.md",
  "delivery_attempted": true,
  "delivery_success": true
}
```
**Response 200 (when no history):**
```json
{ "status": "empty", "message": "No run history found yet" }
```

---

#### `GET /reports/latest`
Metadata for the most recently modified file in `outputs/`.

**Auth:** Auth
**Response 200 (when report exists):**
```json
{
  "report_type": "weekly",
  "filename": "weekly_report_2026-04-28.md",
  "generated_at": "2026-04-28T07:04:23Z",
  "path": "outputs/weekly_report_2026-04-28.md",
  "exists": true
}
```
**Response 200 (when no report):**
```json
{ "report_type": null, "filename": null, "generated_at": null, "path": null, "exists": false }
```

---

#### `GET /reports/latest/raw`
Raw markdown content of the latest report. `text/plain` response.

**Auth:** Auth
**Response 200:** Plain markdown text (the report content)
**Response 404:** `{ "detail": "No markdown report found" }`

---

#### `GET /scheduler/status`
In-app scheduler state and next run times.

**Auth:** Auth
**Response 200:**
```json
{
  "status": "running",
  "jobs": [
    { "job": "daily",   "schedule": "06:00 Asia/Amman (03:00 UTC)",            "next_run": "2026-05-01T03:00:00Z" },
    { "job": "weekly",  "schedule": "Monday 07:00 Asia/Amman (04:00 UTC)",     "next_run": "2026-05-04T04:00:00Z" },
    { "job": "monthly", "schedule": "1st of month 08:00 Asia/Amman (05:00 UTC)", "next_run": "2026-06-01T05:00:00Z" }
  ]
}
```
`status` may be `"running"` or `"not_running"`. `next_run` may be `null` if a job has no scheduled next execution.

---

### Manual Run Triggers

These endpoints execute Phase 1 schedulers on demand. They share an in-memory lock with the in-app scheduler — concurrent calls return 409.

#### `POST /run/daily`
Trigger the daily pulse scheduler.

**Auth:** Admin only (cookie session OR `Authorization: Bearer <ADMIN_API_TOKEN>`)
**Response 200:**
```json
{
  "status": "success",
  "job": "daily",
  "started_at": "2026-04-30T12:00:00Z",
  "finished_at": "2026-04-30T12:00:42Z",
  "result": { "report_path": "outputs/daily_2026-04-30.json" }
}
```
**Response 200 (failed):**
```json
{
  "status": "failed",
  "job": "daily",
  "started_at": "2026-04-30T12:00:00Z",
  "finished_at": "2026-04-30T12:00:05Z",
  "error": "RuntimeError: scheduler execution failed"
}
```
**Response 401:** `{ "detail": "Not authenticated" }`
**Response 403:** `{ "detail": "Admin role required" }`
**Response 409:** `{ "detail": "job already running" }`

---

#### `POST /run/weekly`
Trigger the weekly report scheduler. Same response shape as `/run/daily`, with `"job": "weekly"` and `result.report_path` ending in `.md`.

**Auth:** Admin only

---

#### `POST /run/monthly`
Trigger the monthly strategy report scheduler. Same response shape as `/run/daily`, with `"job": "monthly"`.

**Auth:** Admin only

---

## Endpoint Quick Reference

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | Public | Liveness |
| POST | `/auth/login` | Public | Sign in |
| POST | `/auth/logout` | Public | Sign out |
| GET | `/auth/me` | Auth | Current user |
| GET | `/readiness` | Admin | System readiness |
| GET | `/runs/latest` | Auth | Latest run record |
| GET | `/reports/latest` | Auth | Latest report metadata |
| GET | `/reports/latest/raw` | Auth | Latest report markdown |
| GET | `/scheduler/status` | Auth | Scheduler state |
| POST | `/run/daily` | Admin | Trigger daily |
| POST | `/run/weekly` | Admin | Trigger weekly |
| POST | `/run/monthly` | Admin | Trigger monthly |

---

## Error Response Shape

All error responses follow FastAPI's default shape:
```json
{ "detail": "Human-readable error message" }
```

Use the HTTP status code to determine error category:
- `401` — Not authenticated, redirect to login
- `403` — Authenticated but insufficient role, show permission denied
- `404` — Resource not found
- `409` — Conflict (job already running)
- `500` — Server error, show generic failure message

---

## Frontend Usage Pattern

Recommended fetch wrapper:

```javascript
async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',  // send session cookie
    headers: { 'Content-Type': 'application/json' },
    ...options
  });
  if (res.status === 401) {
    showLoginScreen();
    throw new Error('Unauthorized');
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}
```

Always send `credentials: 'same-origin'` so the session cookie travels with the request.

---

## Forbidden Endpoints (Phase 1)

These endpoints **must not exist** in Phase 1. Adding them is a doctrine violation.

- ❌ Any `POST` or `PATCH` to Google Ads
- ❌ Any `POST` or `PATCH` to HubSpot
- ❌ Any endpoint that uploads OCT conversions
- ❌ Any endpoint that pushes negative keywords

These are reserved for Phase 2 and Phase 3.

---

## When to Update This File

Update this file in the same PR that:
- Adds a new endpoint to `api/server.py`
- Changes the request body of an existing endpoint
- Changes the response shape of an existing endpoint
- Changes the auth requirement of an existing endpoint
- Removes an endpoint

The reviewer will reject the PR if `api/server.py` and this file disagree.
