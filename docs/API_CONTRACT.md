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

### Time-Range Data Endpoints (New in PR-ADS-024)

All endpoints below require authentication, accept a `?days=` query parameter (default 30, max 365), and query the PostgreSQL database. If the database is unavailable, they return a structured empty response with `"db_unavailable": true` — never a 500.

**`?days=` rules:**
- Default: 30
- Maximum: 365 (values above 365 are clamped silently)
- Non-integer: returns 422 validation error (FastAPI rejects before handler)

---

#### `GET /api/campaigns?days=30`
Aggregated campaign metrics for the last N days.

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Response 200:**
```json
{
  "days": 30,
  "generated_at": "2026-04-30T15:00:00Z",
  "campaigns": [
    {
      "campaign_name": "Gulf",
      "latest_verdict": "SCALE",
      "avg_spend_usd": 1400.00,
      "total_confirmed_sqls": 2,
      "avg_junk_rate_pct": 6.0,
      "avg_cpql_usd": 700.00,
      "run_count": 4,
      "trend": "improving"
    }
  ]
}
```
`trend` is `"improving"` / `"stable"` / `"degrading"` based on junk rate direction over the period.
When database is unavailable: `{ "days": 30, "campaigns": [], "db_unavailable": true }`

---

#### `GET /api/leads?days=30`
Individual lead rows for the last N days (max 1000 rows).

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Response 200:**
```json
{
  "days": 30,
  "leads": [
    {
      "contact_id": "12345",
      "campaign_name": "Gulf",
      "keyword": "freight forwarding",
      "country": "AE",
      "mql_status": "CLOSED - Sales Qualified",
      "status_category": "qualified",
      "gclid": "abc123",
      "run_date": "2026-04-30"
    }
  ]
}
```
When database is unavailable: `{ "days": 30, "leads": [], "db_unavailable": true }`

---

#### `GET /api/deals?days=30`
GCLID-matched deal rows for the last N days (max 1000 rows).

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Response 200:**
```json
{
  "days": 30,
  "deals": [
    {
      "contact_id": "12345",
      "company": "Acme Freight",
      "country": "AE",
      "keyword": "freight forwarding",
      "campaign_name": "Gulf",
      "deal_stage": "closedwon",
      "deal_stage_label": "Closed Won",
      "deal_amount_usd": 5000.00,
      "mql_status": "CLOSED - Deal Created",
      "gclid": "abc123",
      "run_date": "2026-04-30"
    }
  ]
}
```
When database is unavailable: `{ "days": 30, "deals": [], "db_unavailable": true }`

---

#### `GET /api/waste?days=30`
Waste search term rows for the last N days (max 500 rows, sorted by spend descending).

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Response 200:**
```json
{
  "days": 30,
  "waste": [
    {
      "search_term": "freight forwarder jobs",
      "campaign_name": "Gulf",
      "spend_usd": 47.20,
      "junk_category": "job_seeker",
      "matched_pattern": "jobs",
      "crm_junk_confirmed": 2,
      "run_date": "2026-04-30"
    }
  ]
}
```
When database is unavailable: `{ "days": 30, "waste": [], "db_unavailable": true }`

---

#### `GET /api/runs?days=30`
Scheduler run records for the last N days.

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Response 200:**
```json
{
  "days": 30,
  "runs": [
    {
      "run_type": "weekly",
      "started_at": "2026-04-30T15:10:01Z",
      "finished_at": "2026-04-30T15:12:44Z",
      "status": "success",
      "report_path": "outputs/weekly_report_2026-04-30.md"
    }
  ]
}
```
When database is unavailable: `{ "days": 30, "runs": [], "db_unavailable": true }`

---

#### `GET /api/summary?days=30`
Aggregated headline metrics for the last N days.

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Response 200:**
```json
{
  "days": 30,
  "total_spend_usd": 6420.00,
  "confirmed_sqls": 8,
  "avg_cpql_usd": 802.50,
  "confirmed_waste_usd": 847.00,
  "total_leads": 196,
  "junk_rate_pct": 27.6,
  "run_count": 4,
  "last_run_at": "2026-04-30T15:10:01Z",
  "last_run_status": "success"
}
```
When database is unavailable: all numeric fields are `null`, `run_count` is `0`, `"db_unavailable": true`.

---

## Endpoint Quick Reference

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | Public | Liveness |
| POST | `/auth/login` | Public | Sign in |
| POST | `/auth/logout` | Public | Sign out |
| GET | `/auth/me` | Auth | Current user |
| GET | `/readiness` | Admin | System readiness |
| GET | `/runs/latest` | Auth | Latest run record (JSONL) |
| GET | `/reports/latest` | Auth | Latest report metadata |
| GET | `/reports/latest/raw` | Auth | Latest report markdown |
| GET | `/scheduler/status` | Auth | Scheduler state |
| POST | `/run/daily` | Admin | Trigger daily |
| POST | `/run/weekly` | Admin | Trigger weekly |
| POST | `/run/monthly` | Admin | Trigger monthly |
| GET | `/api/campaigns` | Auth | Campaign metrics (DB, ?days=) |
| GET | `/api/leads` | Auth | Lead rows (DB, ?days=) |
| GET | `/api/deals` | Auth | Deal rows (DB, ?days=) |
| GET | `/api/waste` | Auth | Waste terms (DB, ?days=) |
| GET | `/api/runs` | Auth | Run records (DB, ?days=) |
| GET | `/api/summary` | Auth | Headline metrics (DB, ?days=) |

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
