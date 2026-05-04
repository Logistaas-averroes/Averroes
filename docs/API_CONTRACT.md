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

**UI note (PR-ADS-028A):** Used as a JSONL-backed fallback by both the global freshness bar and the Run History panel when `/api/runs` returns `db_unavailable: true` or an empty result. Detect the empty state via `status === "empty"` or missing `run_type`.

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
      "campaign_name": "gulf",
      "latest_verdict": "SCALE",
      "avg_spend_usd": 1400.00,
      "total_confirmed_sqls": 2,
      "avg_junk_rate_pct": 6.0,
      "avg_cpql_usd": 700.00,
      "run_count": 4,
      "total_leads": 18,
      "trend": "stable"
    }
  ]
}
```
`trend` is hardcoded to `"stable"` until 4+ weekly runs are available, at which point it will be calculated from junk rate direction. Valid values when dynamic calculation resumes: `"improving"` / `"stable"` / `"degrading"`.
`total_leads` is the lead count from the **latest campaign snapshot** within the selected date range. It is not summed across overlapping runs (weekly + monthly runs can represent the same analysis window). Returns `0` when no value is recorded.
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
      "company": "Acme Freight",
      "campaign_name": "gulf",
      "keyword": "freight forwarding",
      "country": "AE",
      "mql_status": "CLOSED - Sales Qualified",
      "status_category": "qualified",
      "gclid": "abc123",
      "source_type": "paid_search",
      "run_date": "2026-04-30"
    }
  ]
}
```
When database is unavailable: `{ "days": 30, "leads": [], "db_unavailable": true }`
`company` may be `null` for historical rows written before PR-ADS-026 — existing consumers must handle null safely.

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

**Important scope notes:**
- This endpoint returns **flagged waste terms only** — terms that crossed the current waste-detection rules. It does not return the full search-term universe.
- Filtering (by junk category, campaign, or free-text) is performed **client-side** in the SPA. No filter parameters are accepted by this endpoint.
- The `junk_category` field may be `null` for older rows; consumers must handle null safely.

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

**UI note (PR-ADS-028/028A):** The Run History timeline on the Dashboard page reads up to 10 runs from this endpoint (scoped to `getSelectedDays()`). When `db_unavailable: true`, the UI distinguishes this from an empty result and attempts the `/runs/latest` JSONL fallback. It is read-only and purely presentational.

---

#### `GET /api/geo?days=30`
Windsor geo performance data aggregated by country and campaign for the last N days.

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Response 200:**
```json
{
  "days": 30,
  "rows": [
    {
      "country": "United Arab Emirates",
      "campaign_name": "gulf",
      "spend_usd": 123.45,
      "clicks": 10,
      "impressions": 500,
      "conversions": 1.0,
      "runs": 2,
      "last_run_date": "2026-05-02"
    }
  ]
}
```
`country` may be `null`/blank if the upstream Windsor data does not include a country value.
Data represents Google Ads/Windsor geo performance — not HubSpot lead quality.
No write operations are performed by this endpoint.
Used by the Geo Intelligence page (PR-ADS-030).
When database is unavailable: `{ "days": 30, "rows": [], "db_unavailable": true }`

---

#### `GET /api/keywords?days=30`
Windsor keyword performance data aggregated by campaign, ad group, keyword, and match type for the last N days.

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Read-only:** Yes — no write to Google Ads or any external system
**Source:** `keywords` table — Windsor keyword performance data persisted per run

**Response 200:**
```json
{
  "days": 30,
  "rows": [
    {
      "campaign_name": "global - competitors",
      "ad_group": "competitors",
      "keyword": "cargowise",
      "match_type": "phrase",
      "quality_score": 7.0,
      "spend_usd": 123.45,
      "clicks": 10,
      "impressions": 500,
      "conversions": 1.0,
      "cpc_usd": 12.35,
      "runs": 2,
      "last_run_date": "2026-05-02"
    }
  ]
}
```

**Important scope notes:**
- This endpoint returns Google Ads keyword performance only — it does not include HubSpot lead quality.
- It does not include actual user search terms (those are in `/api/waste`).
- `match_type` may be `null`/blank if the upstream Windsor data does not include a match type value.
- `quality_score` may be `null` if not reported by Windsor for a keyword.
- `cpc_usd` is recalculated server-side from `spend / clicks` where clicks > 0; otherwise 0.
- Rows are aggregated over the selected window — `spend_usd`, `clicks`, `impressions`, and `conversions` are summed; `quality_score` is averaged.
- `runs` is the count of distinct run IDs contributing to each aggregated row.
- Rendered by the Keywords page as of PR-ADS-032. Shows Google Ads/Windsor keyword performance only — not HubSpot lead-quality data. Does not include full user search terms. Quality score and match type may be null/unknown.

When database is unavailable: `{ "days": 30, "rows": [], "db_unavailable": true }`

---

#### `GET /api/leads/country-summary?days=30`
HubSpot lead quality aggregated by country for the last N days.

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Read-only:** Yes — no write to HubSpot or any external system
**Source:** `leads` table — HubSpot MQL-derived `status_category` values only

**Response 200:**
```json
{
  "days": 30,
  "rows": [
    {
      "country": "United Arab Emirates",
      "total_leads": 12,
      "confirmed_sqls": 2,
      "in_progress": 4,
      "confirmed_junk": 1,
      "wrong_fit": 0,
      "unknown": 5,
      "verdicted_leads": 7,
      "junk_rate_pct": 14.29,
      "top_campaign": "gulf",
      "top_keyword": "gofreight",
      "last_run_date": "2026-05-02"
    }
  ]
}
```

**Classification:**
- `confirmed_sqls` — `status_category = qualified`
- `in_progress` — `status_category = in_progress`
- `confirmed_junk` — `status_category = junk`
- `wrong_fit` — `status_category = wrong_fit`
- `unknown` — `status_category = unknown` (includes `OPEN - Connecting`)
- `verdicted_leads` — qualified + in_progress + junk + wrong_fit
- `junk_rate_pct` — `confirmed_junk / verdicted_leads × 100`; `null` when `verdicted_leads = 0`

**Important scope notes:**
- Lead quality is derived from HubSpot MQL status only — no inference or country-based scoring.
- Unknown leads (including `OPEN - Connecting`) are **not** counted as junk.
- `junk_rate_pct` denominator excludes unknown contacts — only verdicted leads count.
- `junk_rate_pct` is `null` when `verdicted_leads = 0`; **null means insufficient verdict data, not 0% junk**. Consumers must not display null as 0%.
- Leads are deduplicated server-side by `contact_id` when `contact_id` is present and non-blank (latest run per contact). Rows with NULL or blank `contact_id` use a per-row fallback key and are treated as unique rather than collapsed together.
- Country values are normalized server-side: NULL, blank, and whitespace-only `country` values all become `(unknown)`. This matches the client-side normalization applied before merging with `/api/geo` results.
- This endpoint is ad-performance geography combined UI-side with `/api/geo` — the two sources are separate.
- `top_campaign` and `top_keyword` reflect the most frequent values for that country in the window (PostgreSQL `mode()`).

When database is unavailable: `{ "days": 30, "rows": [], "db_unavailable": true }`

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

**UI note (PR-ADS-028A):** The global data freshness bar reads run records from `/api/runs?days=90` (fixed window, not scoped to the reporting filter) using `normalizeRunStatus()` to handle in-progress rows. When the DB is unavailable, it falls back to `/runs/latest` (JSONL-backed). The badge represents the latest recorded scheduler run — a daily run may not refresh campaign or waste data. It is read-only and purely presentational — it does not write to any system.

---

#### `GET /api/campaign-detail?campaign_name={encoded_name}&days=N` *(Preferred)*
Campaign drill-down detail — full investigation context for a single campaign.

**Auth:** Auth required (cookie session, same as `/api/campaigns`)
**Query params:**
- `campaign_name` (required, URL-encoded string) — campaign name, must use `encodeURIComponent`. Handles campaign names containing spaces, pipes, commas, slashes, and any other punctuation safely via the query string.
- `days` (integer, default 30, max 365)

**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system

**Response 200:**
```json
{
  "days": 30,
  "campaign_name": "global - competitors",
  "campaign": {
    "campaign_name": "global - competitors",
    "spend_usd": 1234.56,
    "clicks": 120,
    "impressions": 9000,
    "conversions": 8.0,
    "total_leads": 41,
    "confirmed_sqls": 1,
    "junk_count": 6,
    "junk_rate_pct": 33.33,
    "cpql_usd": 1234.56,
    "verdict": "FIX",
    "verdict_reason": "Junk rate above threshold",
    "runs": 2,
    "last_run_date": "2026-05-02"
  },
  "lead_quality": {
    "total_leads": 41,
    "confirmed_sqls": 1,
    "in_progress": 4,
    "confirmed_junk": 6,
    "wrong_fit": 3,
    "unknown": 27,
    "verdicted_leads": 14,
    "junk_rate_pct": 42.86
  },
  "countries": [
    {
      "country": "Nigeria",
      "total_leads": 10,
      "confirmed_sqls": 0,
      "in_progress": 1,
      "confirmed_junk": 4,
      "wrong_fit": 1,
      "unknown": 4,
      "junk_rate_pct": 66.67
    }
  ],
  "keywords": [
    {
      "keyword": "cargowise",
      "match_type": "phrase",
      "spend_usd": 300.0,
      "clicks": 20,
      "impressions": 1000,
      "conversions": 2.0,
      "quality_score": 7.0,
      "cpc_usd": 15.0
    }
  ],
  "waste_terms": [
    {
      "search_term": "software logistica gratis",
      "spend_usd": 25.0,
      "junk_category": "free_intent_spanish",
      "matched_pattern": "gratis",
      "crm_junk_confirmed": 2,
      "run_date": "2026-05-02"
    }
  ],
  "recent_leads": [
    {
      "company": "ABC Freight",
      "country": "UAE",
      "keyword": "gofreight",
      "mql_status": "OPEN - Meeting Booked",
      "status_category": "in_progress",
      "run_date": "2026-05-02"
    }
  ],
  "data_sources": {
    "campaign": "PostgreSQL campaigns table",
    "lead_quality": "HubSpot-derived leads table",
    "keywords": "Windsor keyword performance",
    "waste_terms": "Waste detection from search terms"
  }
}
```

**Partial payload (campaign snapshot missing in window):**
`campaign` may be `null` when no campaigns-table snapshot was written in the selected window (e.g. a short window that only received a daily-pulse run, which writes leads but not campaign snapshots). **Consumers must not treat `campaign: null` as total absence of detail.** The other sections (`lead_quality`, `countries`, `keywords`, `waste_terms`, `recent_leads`) may still return data.

Example partial response:
```json
{ "days": 7, "campaign_name": "global - competitors", "campaign": null, "lead_quality": { ... }, "countries": [ ... ], ... }
```

**When database is unavailable:** `{ "days": 30, "campaign_name": "...", "campaign": null, "lead_quality": null, "countries": [], "keywords": [], "waste_terms": [], "recent_leads": [], "db_unavailable": true }`

**Data source details:**
- `campaign` — latest snapshot row from the campaigns table for the campaign name in the selected window. `total_leads` uses latest-snapshot semantics (not summed across overlapping runs).
- `lead_quality` — HubSpot-derived `status_category` from the leads table, deduplicated by `contact_id` (latest run per contact wins; null `contact_id` rows treated as unique).
- `countries` — deduped leads grouped by `COALESCE(NULLIF(BTRIM(country), ''), '(unknown)')`, sorted by total leads descending. Includes `in_progress` in both the response and the verdicted_leads denominator (consistent with lead-quality).
- `keywords` — top 10 keyword rows by spend from the keywords table, aggregated by keyword + match_type. Google Ads/Windsor platform metrics only — no HubSpot lead quality joined.
- `waste_terms` — top 10 waste term rows by spend from the waste_terms table, aggregated by search_term + junk_category + matched_pattern.
- `recent_leads` — 10 most recent deduped leads for this campaign (by run_date descending). Does not expose contact_id.
- `lead_quality.junk_rate_pct` — `confirmed_junk / verdicted_leads × 100`; `null` when `verdicted_leads = 0`. Unknown contacts (including `OPEN - Connecting`) are **excluded** from the denominator.

**Scope boundaries:**
- Does not write to Google Ads.
- Does not write to HubSpot.
- Keyword section is Google Ads/Windsor platform metrics only.
- Waste section shows flagged waste terms only — no apply/push action.
- Lead quality uses HubSpot-derived `status_category` only.
- Phase 1 read-only — no AI inference, no recommendations, no bid/budget changes.

---

#### `GET /api/config/ui-thresholds`
UI-safe display thresholds from `config/thresholds.yaml`.

**Auth:** Auth
**Read-only:** Yes — no writes to any external system
**Source:** `ui:` section of `config/thresholds.yaml`, with safe defaults when the file is missing or malformed

**Response 200:**
```json
{
  "junk_rate": {
    "low_pct": 15,
    "high_pct": 30
  },
  "spend": {
    "high_spend_usd": 100
  },
  "quality_score": {
    "strong_min": 8,
    "medium_min": 5
  }
}
```

**Response 200 (YAML load failed — safe defaults returned):**
```json
{
  "junk_rate": { "low_pct": 15, "high_pct": 30 },
  "spend": { "high_spend_usd": 100 },
  "quality_score": { "strong_min": 8, "medium_min": 5 },
  "using_defaults": true
}
```

**Important scope notes:**
- Returns UI-safe display thresholds only — does not expose full config, API keys, account IDs, or sensitive fields.
- Used by the SPA for: junk-rate visual states (green/yellow/red), high-spend row emphasis on Keywords page, quality-score display bands.
- If the endpoint fails, the SPA falls back to `DEFAULT_UI_THRESHOLDS` (same values) so all visual classification continues to work.
- Does not change backend analysis thresholds.
- Phase 1 read-only — no write to Google Ads, HubSpot, or any external system.

**Response fields:**
- `junk_rate.low_pct` — junk rate below this → green visual state
- `junk_rate.high_pct` — junk rate above this → red visual state
- `spend.high_spend_usd` — keyword spend at or above this → high-spend row emphasis
- `quality_score.strong_min` — quality score at or above this → strong badge
- `quality_score.medium_min` — quality score at or above this (but below strong_min) → medium badge
- `using_defaults` — present and `true` only when the YAML file could not be loaded

---

#### `GET /api/campaigns/{campaign_name}/detail?days=N` *(Legacy — path-segment form)*
Same response shape as `/api/campaign-detail`. Preserved for backwards compatibility.

**Limitation:** Campaign names containing a literal forward slash (`/`) cannot be addressed via this route even when URL-encoded, because the router treats slashes as path separators. Use `/api/campaign-detail?campaign_name=...` for new callers.

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
| GET | `/api/campaign-detail` | Auth | Campaign drill-down detail, query-param form (DB, ?campaign_name=&days=) **Preferred** |
| GET | `/api/campaigns/{campaign_name}/detail` | Auth | Campaign drill-down detail, path-segment form (DB, ?days=) *Legacy* |
| GET | `/api/leads` | Auth | Lead rows (DB, ?days=) |
| GET | `/api/deals` | Auth | Deal rows (DB, ?days=) |
| GET | `/api/waste` | Auth | Waste terms (DB, ?days=) |
| GET | `/api/runs` | Auth | Run records (DB, ?days=) |
| GET | `/api/summary` | Auth | Headline metrics (DB, ?days=) |
| GET | `/api/geo` | Auth | Windsor geo performance by country/campaign (DB, ?days=) |
| GET | `/api/keywords` | Auth | Windsor keyword performance by campaign/ad group/keyword (DB, ?days=) |
| GET | `/api/leads/country-summary` | Auth | HubSpot lead quality by country (DB, ?days=) |
| GET | `/api/config/ui-thresholds` | Auth | UI-safe display thresholds from config/thresholds.yaml |

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
