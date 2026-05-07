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

**UI note (PR-ADS-035):** Used by the Reports page to display report metadata cards (file, generated date, source path, type/status). Read-only — does not trigger scheduler jobs, does not call Claude, does not generate new analysis.

---

#### `GET /reports/latest/raw`
Raw markdown content of the latest report. `text/plain` response.

**Auth:** Auth
**Response 200:** Plain markdown text (the report content)
**Response 404:** `{ "detail": "No markdown report found" }`

**UI note (PR-ADS-035):** Used by the Reports page to render the report body. The SPA renders the content as escaped text inside a `<pre>` element — markdown is **not** injected as raw HTML. The Reports page is a read-only viewer: it does not trigger scheduler jobs, does not call Claude, and does not generate new analysis. The report reflects the latest generated markdown file on disk, not a live re-analysis.

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

#### `GET /api/dashboard/trends?days=N`
Previous-period trend comparison for the dashboard. Returns summary metrics, campaign movements, and display alerts comparing the current period against the previous period of equal length.

**Auth:** Auth (session cookie, same as other dashboard endpoints)
**Query params:** `days` (integer, default 30, max 365)
**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system
**Phase 1 read-only:** Confirmed

**Period definition:**
- Current period: `NOW() - N days → NOW()`
- Previous period: `NOW() - 2N days → NOW() - N days`

**Response 200:**
```json
{
  "days": 30,
  "current_period":  { "start": "2026-04-02", "end": "2026-05-02" },
  "previous_period": { "start": "2026-03-03", "end": "2026-04-02" },
  "summary": {
    "spend_usd":           { "current": 6500.25, "previous": 5900.00, "delta": 600.25, "delta_pct": 10.17, "trend": "up" },
    "confirmed_sqls":      { "current": 8,       "previous": 5,       "delta": 3,      "delta_pct": 60.0,  "trend": "up" },
    "confirmed_waste_usd": { "current": 420.00,  "previous": 300.00,  "delta": 120.00, "delta_pct": 40.0,  "trend": "up" },
    "avg_junk_rate_pct":   { "current": 28.5,    "previous": 22.0,    "delta": 6.5,    "delta_pct": 29.55, "trend": "up" }
  },
  "campaign_movements": [
    {
      "campaign_name": "global - competitors",
      "current":  { "spend_usd": 1000, "confirmed_sqls": 0, "junk_rate_pct": 42, "verdict": "FIX" },
      "previous": { "spend_usd": 800,  "confirmed_sqls": 1, "junk_rate_pct": 25, "verdict": "HOLD" },
      "movement": "worsened",
      "reason": "SQLs fell by 1. Junk rate rose 17.0 points.",
      "severity_score": 85
    }
  ],
  "alerts": [
    {
      "campaign_name": "global - competitors",
      "severity": "high",
      "title": "Junk rate worsened",
      "detail": "Junk rate increased from 25.0% to 42.0% (+17.0 points). Warrants review.",
      "source": "campaigns table"
    }
  ],
  "data_quality": {
    "has_previous_period": true,
    "current_runs": 2,
    "previous_runs": 1,
    "status": "ok"
  }
}
```

**Movement classification:**
- `new` — campaign appears in current period but not previous
- `dropped` — campaign present in previous but absent in current
- `improved` — SQLs increased OR junk rate decreased ≥ 10 points
- `worsened` — SQLs decreased OR junk rate increased ≥ 10 points OR spend rose ≥ 20% with zero SQLs
- `stable` — no meaningful change detected
- `insufficient_data` — junk rate unavailable in both periods

**Thresholds used for movement classification (local constants in endpoint):**
- Junk rate meaningful change: absolute delta ≥ 10 percentage points
- Spend meaningful change: relative delta ≥ 20% of previous-period spend
- SQL change meaningful: integer delta ≠ 0

**Severity score (0–100, display-only — not an automated action recommendation):**
- +30 if current SQLs = 0 and spend > 0
- +25 if current junk rate ≥ 30% (high junk threshold default)
- +20 if spend increased ≥ 20%
- +20 if junk rate increased ≥ 10 points
- +15 if verdict is FIX
- +25 if verdict is CUT
- Capped at 100

**Alert language:** Evidence-based, warrants-review only. Does not use the words pause, cut, increase budget, or apply negatives.

**Data quality states:**
- `ok` — previous-period data available
- `insufficient_previous_data` — no previous-period runs found (endpoint still returns current metrics)
- `db_unavailable` — database offline (safe empty response returned)

**DB unavailable response:**
```json
{ "days": 30, "summary": {}, "campaign_movements": [], "alerts": [], "data_quality": { "status": "db_unavailable" }, "db_unavailable": true }
```

**`campaign_movements` ordering:** Sorted by `severity_score` descending; top 10 returned.
**`alerts` ordering:** Sorted by severity (high → medium → low), deduplicated per campaign; up to 8 returned.
**`summary.spend_usd`** uses latest-run-per-period aggregation (same convention as `/api/summary`) to avoid double-counting overlapping weekly/monthly run snapshots.
**`summary.confirmed_waste_usd`** is summed from `waste_terms` rows where `crm_junk_confirmed > 0` in each period.

---

#### `GET /api/action-queue?days=30`
Ranked human-review queue based on campaign, waste, geo, keyword, and data-quality signals.

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system
**Sources:** `campaigns`, `waste_terms`, `geo`, `leads`, `keywords`, `runs` tables

**Item types:**
- `campaign_review` — campaigns with verdict FIX/CUT, zero SQLs with spend, or high junk rate
- `waste_review` — top waste terms by spend that meet high-spend or CRM-confirmed threshold
- `geo_review` — countries with high junk rate, zero SQLs with spend, or unknown country
- `keyword_review` — keywords with spend above threshold and zero Google Ads conversions
- `data_quality_review` — run failures or absence of recent weekly/monthly successful runs

**Severity scoring (display-only — not automated recommendations):**

Campaign:
- +30 if SQLs = 0 and spend > 0
- +25 if junk rate ≥ high_pct threshold (default 30%)
- +20 if verdict = FIX
- +30 if verdict = CUT
- +15 if spend ≥ high_spend_usd threshold (default $100)
- Capped at 100

Waste:
- Base 30 + up to +55 based on spend, CRM confirmed, and fraud/job/student/free category

Geo:
- Base 25 + up to +60 based on junk rate, zero SQLs with spend, unknown country

Keyword:
- Base 20 + up to +55 based on spend threshold, zero conversions, broad match type

Data quality:
- Base 40 + up to +60 based on latest run failure and absence of weekly/monthly runs

**Severity labels:** `high` (score ≥ 75), `medium` (40–74), `low` (< 40)

**Sorting:** Items sorted by `(-severity_score, type, entity_label)` for stable ordering.

**Limit:** Maximum 30 items returned.

**Important scope notes:**
- Queue items are human-review prompts only — not automated action recommendations.
- Severity is display-only.
- No writes are performed by this endpoint.
- No external API calls are made.
- Keyword `evidence.google_ads_conversions` reflects Google Ads/Windsor platform conversions only, not HubSpot SQLs.

**Response 200:**
```json
{
  "days": 30,
  "items": [
    {
      "id": "campaign-global-competitors-review",
      "type": "campaign_review",
      "severity": "high",
      "severity_score": 92,
      "title": "Campaign warrants review: global - competitors",
      "detail": "Spend is $1200.50. Confirmed SQLs are 0. Junk rate is 42.0%. Verdict is FIX. Warrants review.",
      "entity_label": "global - competitors",
      "entity_type": "campaign",
      "campaign_name": "global - competitors",
      "source": "campaigns table",
      "evidence": {
        "spend_usd": 1200.50,
        "confirmed_sqls": 0,
        "junk_rate_pct": 42.0,
        "verdict": "FIX"
      },
      "primary_link": {
        "page": "campaigns",
        "action": "open_campaign_drawer",
        "campaign_name": "global - competitors"
      }
    }
  ],
  "summary": {
    "total": 12,
    "high": 3,
    "medium": 6,
    "low": 3
  },
  "data_quality": {
    "status": "ok"
  }
}
```

**DB unavailable response:**
```json
{
  "days": 30,
  "items": [],
  "summary": { "total": 0, "high": 0, "medium": 0, "low": 0 },
  "data_quality": { "status": "db_unavailable" },
  "db_unavailable": true
}
```

---

#### `GET /api/datasets/freshness`
Per-dataset sync state / watermark. Returns the latest known sync status for each source+dataset pair.

**Auth:** Auth
**Read-only:** Yes — no live fetch, no sync execution, no external API calls
**Source:** `sync_state` table (PR-ADS-039)

**Known datasets:**
- `windsor` / `campaigns`
- `windsor` / `keywords`
- `windsor` / `search_terms`
- `windsor` / `geo`
- `hubspot` / `contacts`
- `hubspot` / `deals`
- `gclid` / `matches`

**Status values:** `success` | `failed` | `running` | `unknown`

**Response 200 (with sync data):**
```json
{
  "datasets": [
    {
      "source": "windsor",
      "dataset": "campaigns",
      "status": "success",
      "last_successful_sync_at": "2026-05-04T06:00:00+00:00",
      "last_source_date": "2026-05-03",
      "last_batch_id": 12,
      "error_message": null,
      "updated_at": "2026-05-04T06:03:00+00:00"
    }
  ],
  "summary": {
    "total": 7,
    "success": 3,
    "failed": 1,
    "running": 0,
    "unknown": 3
  },
  "db_unavailable": false
}
```

**Response 200 (no sync rows yet — returns known placeholders):**
```json
{
  "datasets": [
    { "source": "windsor",  "dataset": "campaigns",    "status": "unknown", "last_successful_sync_at": null, "last_source_date": null, "last_batch_id": null, "error_message": null, "updated_at": null },
    { "source": "windsor",  "dataset": "keywords",     "status": "unknown", "last_successful_sync_at": null, "last_source_date": null, "last_batch_id": null, "error_message": null, "updated_at": null },
    { "source": "windsor",  "dataset": "search_terms", "status": "unknown", "last_successful_sync_at": null, "last_source_date": null, "last_batch_id": null, "error_message": null, "updated_at": null },
    { "source": "windsor",  "dataset": "geo",          "status": "unknown", "last_successful_sync_at": null, "last_source_date": null, "last_batch_id": null, "error_message": null, "updated_at": null },
    { "source": "hubspot",  "dataset": "contacts",     "status": "unknown", "last_successful_sync_at": null, "last_source_date": null, "last_batch_id": null, "error_message": null, "updated_at": null },
    { "source": "hubspot",  "dataset": "deals",        "status": "unknown", "last_successful_sync_at": null, "last_source_date": null, "last_batch_id": null, "error_message": null, "updated_at": null },
    { "source": "gclid",    "dataset": "matches",      "status": "unknown", "last_successful_sync_at": null, "last_source_date": null, "last_batch_id": null, "error_message": null, "updated_at": null }
  ],
  "summary": {
    "total": 7,
    "success": 0,
    "failed": 0,
    "running": 0,
    "unknown": 7
  },
  "db_unavailable": false
}
```

**DB unavailable response:**
```json
{
  "datasets": [],
  "summary": { "total": 0, "success": 0, "failed": 0, "running": 0, "unknown": 0 },
  "db_unavailable": true
}
```

**Important scope notes:**
- Returns data from `sync_state` table only — does not fetch live data from Windsor or HubSpot.
- Does not trigger any sync or scheduler job.
- `last_successful_sync_at` is the system time when the sync succeeded (not the source-data date).
- `last_source_date` is the latest source-data date covered by the last successful sync.
- `last_batch_id` links to the `sync_batches` row for the last successful sync.
- Datasets not yet in `sync_state` are returned with `status: "unknown"` and all watermark fields null.
- Extra `sync_state` rows beyond the known dataset list are appended after the known pairs.
- Success responses include `db_unavailable: false`; failure-safe responses include `db_unavailable: true`.
- Freshness updates only after local persistence succeeds. Scheduler tracking reflects stored local facts,
  not a successful upstream fetch by itself.
- Batch writer row counts may represent attempted upserts, not confirmed physical inserts. Schedulers treat
  non-empty fetched data with zero written rows as failed freshness and must not mark the dataset fresh.
- As of PR-ADS-051, tracked raw-fact freshness includes `windsor/search_terms` on daily/weekly/monthly runs,
  `hubspot/contacts` on daily runs, and `gclid/matches` on weekly/monthly runs. Other datasets may remain
  `unknown` until their raw-fact sync path is implemented. `unknown` does not mean failed; it means no
  successful tracked sync has been recorded yet.
- **UI note (PR-ADS-045):** As of PR-ADS-045, this endpoint is rendered in the Health page
  **Dataset Freshness** panel. The UI is read-only — it does not trigger sync, retry, backfill,
  or any external API call. `unknown` means no tracked sync has been recorded; it is not
  equivalent to `failed`. A display-only `stale` badge may be shown when the last successful
  sync is more than 2 days old, but this does not change backend status.

---

#### `GET /api/search-terms`
Paginated raw search-term fact rows for the last N days.

**Auth:** Auth
**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system
**Source:** `search_terms` table (PR-ADS-040)

**Query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `days` | integer | 14 | Look-back window (1–90) |
| `campaign` | string | — | Exact canonical campaign_name match |
| `match_type` | string | — | Case-insensitive contains match on match_type |
| `q` | string | — | Case-insensitive contains search on search_term |
| `waste_state` | string | all | Analysis-state filter. Allowed: `all`, `flagged`, `clean`, `unanalyzed`. Aliases: `waste`=`flagged`, `analyzed_clean`=`clean`, `unanalysed`=`unanalyzed`. Invalid values return HTTP 400. |
| `waste_only` | boolean | false | Deprecated. Equivalent to `waste_state=flagged` when `waste_state` is not provided. Preserved for backward compatibility. |
| `min_spend` | numeric | — | Minimum spend_usd |
| `limit` | integer | 100 | Page size (1–500) |
| `cursor` | string | — | Opaque pagination cursor from previous response |

**Pagination:** Cursor/keyset on `(source_date DESC, id DESC)`. Cursor values are opaque keyset cursors. Clients must not parse, modify, or construct them. Use `pagination.next_cursor` from each response to fetch the next page.

**`is_flagged_waste` tri-state:**
- `null` — not analysed yet
- `true` — analysed and flagged as waste
- `false` — analysed and not flagged (clean)

**Response 200:**
```json
{
  "days": 14,
  "filters": {
    "waste_state": "unanalyzed"
  },
  "rows": [
    {
      "id": 12345,
      "source_date": "2026-05-03",
      "campaign_name": "global - competitors",
      "campaign_id": "123",
      "ad_group": "competitors",
      "keyword": "cargowise",
      "match_type": "broad",
      "search_term": "software logistica gratis",
      "spend_usd": 25.50,
      "clicks": 4,
      "impressions": 120,
      "conversions": 1.0,
      "is_flagged_waste": true,
      "junk_category": "free_intent_spanish",
      "matched_pattern": "gratis",
      "last_seen_at": "2026-05-04T06:00:00+00:00"
    }
  ],
  "pagination": {
    "limit": 100,
    "next_cursor": "eyJzb3VyY2VfZGF0ZSI6ICIyMDI2LTA1LTAzIiwgImlkIjogMTIzNDV9",
    "has_more": true
  },
  "data_quality": {
    "source": "windsor",
    "dataset": "search_terms",
    "note": "is_flagged_waste is tri-state: null = not analyzed, true = flagged waste, false = analyzed clean. Current Windsor connector is confirmed up to last_14d search-term window unless plan supports more."
  }
}
```

**DB unavailable response:**
```json
{
  "days": 14,
  "filters": {
    "waste_state": "all"
  },
  "rows": [],
  "pagination": { "limit": 100, "next_cursor": null, "has_more": false },
  "data_quality": { "source": "windsor", "dataset": "search_terms", "status": "db_unavailable" },
  "db_unavailable": true
}
```
The `filters` object is returned whenever the request can be parsed, including DB-unavailable fallback responses. `waste_state` reflects the effective resolved state (e.g. `"unanalyzed"` if `?waste_state=unanalyzed` was sent).

**Invalid cursor response:**
```json
{ "detail": "Invalid cursor: ..." }
```
HTTP 400.

**Important scope notes:**
- This endpoint returns the full search-term universe — not just flagged waste terms. Use `?waste_state=flagged` to filter waste rows.
- Does not include HubSpot lead quality or SQL enrichment.
- Does not infer negative keyword candidates.
- Does not push negative keywords to Google Ads.
- `is_flagged_waste` is nullable tri-state — `null` means not yet analysed, `true` means flagged waste, `false` means analysed clean. Do not treat `null` as clean.
- Historical range depends on Windsor plan/connector limitation (confirmed up to last_14d).
- Cursor is opaque to callers — do not parse or modify it. Invalid cursors return HTTP 400.
- The `q` parameter uses case-insensitive contains matching on `search_term`.
- At scale, broad contains-search should be backed by PostgreSQL trigram indexing (`pg_trgm`).
  Without `pg_trgm`, `ILIKE '%term%'` can become slower on large datasets.
- `waste_only=true` is preserved for backward compatibility and maps to `waste_state=flagged` when `waste_state` is not provided.
- Invalid `waste_state` values return HTTP 400 with `{ "detail": "Invalid waste_state. Allowed values: all, flagged, clean, unanalyzed." }`.

**Frontend usage (as of PR-ADS-052):**
- Rendered by the Search Terms page in the SPA.
- UI uses cursor pagination via `pagination.next_cursor` — Load More button appends rows.
- KPI cards show counts and spend for currently loaded rows only, not the total database count.
- All analysis-state filters (`flagged`, `clean`, `unanalyzed`) are applied server-side via `waste_state`. Client-side state filtering has been removed.
- Page is read-only: no negative keyword push, no marking/editing waste state, no campaign actions.

---

#### `GET /api/gclid-attribution`
Paginated GCLID attribution rows for the last N days.

**Auth:** Auth
**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system
**Source:** `gclid_attribution` table (PR-ADS-044)

**Query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `days` | integer | 30 | Look-back window (1–365) |
| `campaign` | string | — | Exact canonical campaign_name match |
| `gclid` | string | — | Exact gclid value match |
| `contact_id` | string | — | Exact contact_id match |
| `deal_id` | string | — | Exact deal_id match |
| `match_status` | string | — | Exact match_status match |
| `limit` | integer | 100 | Page size (1–500) |
| `cursor` | string | — | Opaque pagination cursor from previous response |

**Pagination:** Cursor/keyset on `(created_at DESC, id DESC)`. Cursor values are opaque keyset cursors. Clients must not parse, modify, or construct them. Use `pagination.next_cursor` from each response to fetch the next page.

**Response 200:**
```json
{
  "days": 30,
  "rows": [
    {
      "id": 123,
      "gclid": "abc123",
      "contact_id": "987",
      "deal_id": "456",
      "company": "ABC Freight",
      "country": "UAE",
      "campaign_name": "global - competitors",
      "keyword": "cargowise",
      "match_type": "broad",
      "search_term": "freight forwarding software",
      "first_url": "https://...",
      "contact_created_at": "2026-05-01T08:00:00+00:00",
      "deal_created_at": "2026-05-03T08:00:00+00:00",
      "deal_close_date": null,
      "deal_stage": "appointmentscheduled",
      "deal_stage_label": "Appointment Scheduled",
      "deal_amount_usd": 2500.00,
      "mql_status": "OPEN - Meeting Booked",
      "status_category": "in_progress",
      "match_status": "matched",
      "match_source": "gclid",
      "created_at": "2026-05-04T10:00:00+00:00"
    }
  ],
  "pagination": {
    "limit": 100,
    "next_cursor": "opaque-token-or-null",
    "has_more": false
  },
  "summary": {
    "loaded_rows": 1,
    "matched_rows": 1,
    "url_fallback_rows": 0,
    "unmatched_rows": 0,
    "total_deal_amount_usd_loaded": 2500.00
  }
}
```

**Summary fields are loaded-page summary only** — they reflect the rows returned on the current page, not the total account coverage.

**DB unavailable response:**
```json
{
  "days": 30,
  "rows": [],
  "pagination": { "limit": 100, "next_cursor": null, "has_more": false },
  "summary": {
    "loaded_rows": 0,
    "matched_rows": 0,
    "url_fallback_rows": 0,
    "unmatched_rows": 0,
    "total_deal_amount_usd_loaded": 0
  },
  "db_unavailable": true
}
```

**Invalid cursor response:**
```json
{ "detail": "Invalid cursor: ..." }
```
HTTP 400.

**Important scope notes:**
- Does not upload offline conversions.
- Does not call Google Ads API.
- Does not write to HubSpot.
- Does not mutate CRM, deals, or any external system.
- Future OCT workflows must be separate and human-approved.
- Multiple deals for the same contact/GCLID are preserved as separate rows.
- `summary` reflects current-page loaded rows only — not total database counts.
- Invalid cursors return HTTP 400.

**Frontend usage (as of PR-ADS-047):**
- As of PR-ADS-047, the Campaign Investigation Drawer uses this endpoint with the `campaign` filter to display a read-only attribution preview.
- The preview is limited (`limit=5`) and loaded-page scoped. It does not upload offline conversions, mutate Google Ads, or update HubSpot.
- Campaign drawer fetches attribution in parallel with campaign detail; attribution failure is non-fatal and does not block the drawer.
- A "View full GCLID Attribution page" button navigates to the full GCLID Attribution page and pre-fills the campaign filter.

**Frontend usage (as of PR-ADS-046):**
- Rendered by the GCLID Attribution page in the SPA.
- Page uses cursor pagination (`pagination.next_cursor`) and loads additional rows only when the operator clicks **Load more**.
- KPI values on the page are loaded-page values unless explicitly labeled as coverage snapshot values.
- UI is strictly read-only: it does not upload offline conversions and does not modify Google Ads, HubSpot, deals, or contacts.

---

#### `GET /api/gclid-coverage`
GCLID coverage snapshot rows for the last N days.

**Auth:** Auth
**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system
**Source:** `gclid_coverage_snapshots` table (PR-ADS-044)

**Query params:** `days` (integer, default 30, max 365)

**Response 200:**
```json
{
  "days": 30,
  "rows": [
    {
      "snapshot_date": "2026-05-04",
      "total_contacts": 100,
      "contacts_with_gclid": 80,
      "contacts_without_gclid": 20,
      "coverage_pct": 80.0,
      "created_at": "2026-05-04T10:00:00+00:00"
    }
  ]
}
```

**DB unavailable response:**
```json
{ "days": 30, "rows": [], "db_unavailable": true }
```

**Frontend usage (as of PR-ADS-046):**
- Rendered as a latest coverage snapshot on the GCLID Attribution page.
- Coverage snapshots are local DB records, not live Google Ads status.
- UI is read-only and resilient: if the endpoint is unavailable or empty, the page shows a safe unavailable/empty coverage state.

---

#### `GET /api/attribution/quality`
Read-only attribution quality signals derived from stored GCLID evidence. (PR-ADS-048)

**Auth:** Auth
**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system
**Source tables:** `gclid_attribution`, `sync_state` (gclid/matches row), `gclid_coverage_snapshots`
**Does not:** call Google Ads APIs · call HubSpot APIs · upload offline conversions · mutate any record

**Query params:**
- `days` — integer, default 30, max 365
- `campaign` — optional exact canonical campaign name filter (same normalisation as `/api/gclid-attribution`)

**Response 200 (rows present):**
```json
{
  "days": 30,
  "scope": { "campaign": "global - competitors" },
  "summary": {
    "loaded_scope_rows": 42,
    "matched_rows": 30,
    "url_fallback_rows": 5,
    "unmatched_rows": 7,
    "unknown_rows": 0,
    "contacts_linked": 25,
    "deals_linked": 12,
    "rows_with_deal_amount": 8,
    "total_deal_amount_usd": 45000.00,
    "latest_attribution_at": "2026-05-06T07:00:00+00:00"
  },
  "rates": {
    "matched_rate_pct": 71.43,
    "url_fallback_rate_pct": 11.90,
    "unmatched_rate_pct": 16.67,
    "deal_link_rate_pct": 28.57,
    "deal_amount_coverage_pct": 66.67
  },
  "signals": [
    {
      "key": "match_strength",
      "status": "good",
      "label": "Strong match coverage",
      "detail": "71.4% of loaded attribution rows are direct matched rows.",
      "severity": "low"
    },
    {
      "key": "url_fallback_reliance",
      "status": "watch",
      "label": "URL fallback reliance",
      "detail": "11.9% of rows rely on URL fallback rather than direct GCLID match. URL fallback is weaker attribution evidence than direct GCLID.",
      "severity": "medium"
    }
  ],
  "freshness": {
    "source": "gclid",
    "dataset": "matches",
    "status": "success",
    "last_successful_sync_at": "2026-05-06T07:00:00+00:00",
    "last_source_date": "2026-05-05"
  },
  "coverage_snapshot": {
    "snapshot_date": "2026-05-06",
    "contacts_with_gclid": 120,
    "contacts_without_gclid": 30,
    "coverage_pct": 80.0
  }
}
```

**Response 200 (no rows in scope):**
```json
{
  "days": 30,
  "summary": { "loaded_scope_rows": 0, ... },
  "rates": {},
  "signals": [
    {
      "key": "no_attribution_rows",
      "status": "unknown",
      "label": "No attribution rows",
      "detail": "No GCLID attribution evidence is stored for this scope.",
      "severity": "low"
    }
  ]
}
```

**DB unavailable response:**
```json
{ "days": 30, "summary": {}, "rates": {}, "signals": [], "db_unavailable": true }
```

**Signal keys and semantics:**

| Signal key | Status values | Basis | Notes |
|---|---|---|---|
| `match_strength` | good / watch / weak / unknown | `matched_rate_pct` ≥70 / 40–70 / <40 | Evidence/completeness only |
| `url_fallback_reliance` | good / watch / risk | `url_fallback_rate_pct` <10 / 10–25 / >25 | URL fallback is weaker evidence, not automatically bad |
| `unmatched_rate` | good / watch / risk | `unmatched_rate_pct` <10 / 10–25 / >25 | Evidence completeness only |
| `deal_linkage` | good / watch / weak | `deal_link_rate_pct` ≥40 / 15–40 / <15 | Data-completeness signal, not sales verdict |
| `amount_coverage` | good / watch / weak / unknown | `deal_amount_coverage_pct` ≥70 / 30–70 / <30 / no deals | Data-completeness signal only |
| `freshness` | good / watch / risk / unknown | sync_state age ≤48h / >48h / failed / missing | Local warehouse freshness only — not live platform status |
| `no_attribution_rows` | unknown | loaded_scope_rows == 0 | Placeholder when no data exists |

**Forbidden language in `detail` and UI labels:**
- OCT ready · upload · push · fix · guaranteed · qualified revenue · proven ROI

**Allowed language:**
- attribution evidence · match coverage · URL fallback reliance · deal linkage · amount coverage · warrants review · local warehouse freshness

**Frontend usage (as of PR-ADS-048):**
- Rendered as the "Attribution Quality" panel on the GCLID Attribution page above the evidence table.
- The panel auto-reloads on filter apply, filter clear, refresh, and time-range change.
- The `/api/gclid-attribution` page may display quality signals sourced from this endpoint.
- UI is read-only — no action buttons are rendered.
- As of PR-ADS-049, the Campaign Investigation Drawer uses this endpoint with the `campaign` filter to render compact attribution quality signals.
- The drawer overlay is read-only and does not upload offline conversions, modify Google Ads, or update HubSpot.

---

## DB Schema Notes (PR-ADS-039 / PR-ADS-040)

### `sync_batches`
One row per dataset sync operation (backfill, daily, weekly, monthly, manual).

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | Auto-generated batch ID |
| `run_id` | INTEGER (nullable FK → runs) | Nullable — manual backfills may run outside scheduler runs |
| `source` | TEXT NOT NULL | `windsor` \| `hubspot` \| `gclid` |
| `dataset` | TEXT NOT NULL | `campaigns` \| `keywords` \| `search_terms` \| `geo` \| `contacts` \| `deals` \| `matches` |
| `sync_type` | TEXT NOT NULL | `backfill` \| `daily` \| `weekly` \| `monthly` \| `manual` |
| `date_from` | DATE | Nullable — start of data range synced |
| `date_to` | DATE | Nullable — end of data range synced |
| `started_at` | TIMESTAMPTZ | Set to NOW() on insert |
| `finished_at` | TIMESTAMPTZ | Nullable — set by `finish_sync_batch()` |
| `status` | TEXT NOT NULL | `running` \| `success` \| `failed` |
| `row_count` | INTEGER | Default 0 |
| `error_message` | TEXT | Nullable |
| `created_at` | TIMESTAMPTZ | Set to NOW() on insert |

### `sync_state`
One row per source+dataset — the current watermark/freshness state.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `source` | TEXT NOT NULL | |
| `dataset` | TEXT NOT NULL | |
| `last_successful_sync_at` | TIMESTAMPTZ | Nullable — system time of last successful sync |
| `last_source_date` | DATE | Nullable — latest source-data date covered |
| `last_batch_id` | INTEGER (nullable FK → sync_batches) | |
| `status` | TEXT NOT NULL | Default `unknown` |
| `error_message` | TEXT | Nullable |
| `updated_at` | TIMESTAMPTZ | Updated on every upsert |

UNIQUE constraint on `(source, dataset)` — serves as the primary lookup index.

---

### `search_terms` (PR-ADS-040)
Raw search-term fact table. Grain: `source_date` + `campaign_name` + `ad_group` + `keyword` + `match_type` + `search_term`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `run_id` | INTEGER (nullable FK → runs) | Nullable — backfill/sync rows may not have a scheduler run_id |
| `source_date` | DATE NOT NULL | Actual search-term data date (not the scheduler write date) |
| `campaign_name` | TEXT | Canonical lowercase campaign name |
| `campaign_id` | TEXT | Google Ads campaign ID (nullable) |
| `ad_group` | TEXT | |
| `keyword` | TEXT | |
| `match_type` | TEXT | |
| `search_term` | TEXT | The actual user search query |
| `spend_usd` | NUMERIC(10,2) | Default 0 |
| `clicks` | INTEGER | Default 0 |
| `impressions` | INTEGER | Default 0 |
| `conversions` | NUMERIC(8,2) | Default 0 |
| `is_flagged_waste` | BOOLEAN (nullable) | **Tri-state:** `NULL` = not analysed \| `TRUE` = waste \| `FALSE` = clean |
| `junk_category` | TEXT | Waste category label (nullable) |
| `matched_pattern` | TEXT | Waste pattern that triggered classification (nullable) |
| `sync_batch_id` | INTEGER (nullable FK → sync_batches) | |
| `created_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | Updated on every upsert |

**Unique natural key index:** `(source_date, COALESCE(campaign_name,''), COALESCE(ad_group,''), COALESCE(keyword,''), COALESCE(match_type,''), COALESCE(search_term,''))`

**Important:**
- `is_flagged_waste` is nullable tri-state — do NOT treat `null` as `false`.
- Raw writer never sets `is_flagged_waste = FALSE`. Only waste-analysis logic may update that field.
- `source_date` reflects the actual Google Ads data date, not the write timestamp.
- Historical range is limited by the Windsor plan/connector window (confirmed up to last_14d).

---

### `gclid_attribution` (PR-ADS-044)
GCLID attribution evidence table. One row per matched GCLID evidence record. Multiple deals for the same contact/GCLID are preserved as separate rows. Deduplicated via `attribution_key` (SHA1 of key fields).

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `attribution_key` | TEXT NOT NULL UNIQUE | SHA1 of gclid\|contact_id\|(deal_id or first_url)\|campaign_name\|keyword\|match_status; first_url is used when deal_id is absent to avoid collapsing legitimate rows |
| `run_id` | INTEGER (nullable FK → runs) | |
| `sync_batch_id` | INTEGER (nullable FK → sync_batches) | |
| `gclid` | TEXT NOT NULL | Google Click ID |
| `contact_id` | TEXT | HubSpot contact ID |
| `deal_id` | TEXT | HubSpot deal ID (nullable) |
| `campaign_name` | TEXT | Canonical lowercase campaign name |
| `keyword` | TEXT | |
| `match_type` | TEXT | |
| `search_term` | TEXT | |
| `company` | TEXT | |
| `country` | TEXT | |
| `first_url` | TEXT | |
| `contact_created_at` | TIMESTAMPTZ | |
| `deal_created_at` | TIMESTAMPTZ | |
| `deal_close_date` | TIMESTAMPTZ | |
| `deal_stage` | TEXT | |
| `deal_stage_label` | TEXT | |
| `deal_amount_usd` | NUMERIC(12,2) | |
| `mql_status` | TEXT | |
| `status_category` | TEXT | qualified \| in_progress \| junk \| wrong_fit \| unknown |
| `match_status` | TEXT | matched \| unmatched \| url_fallback \| unknown |
| `match_source` | TEXT | gclid \| first_url \| crm_field \| unknown |
| `created_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | Updated on every upsert |

**Important:**
- Multiple deals for the same contact/GCLID are preserved as separate rows via `attribution_key`.
- Writer skips rows with blank/missing `gclid`.
- Upsert preserves non-null existing values — null incoming fields do not overwrite stored data.

---

### `gclid_coverage_snapshots` (PR-ADS-044)
One GCLID coverage snapshot per run, capturing aggregate coverage statistics.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `run_id` | INTEGER (nullable FK → runs) | |
| `sync_batch_id` | INTEGER (nullable FK → sync_batches) | |
| `snapshot_date` | DATE NOT NULL | Defaults to CURRENT_DATE |
| `total_contacts` | INTEGER | |
| `contacts_with_gclid` | INTEGER | |
| `contacts_without_gclid` | INTEGER | |
| `coverage_pct` | NUMERIC(6,2) | |
| `total_deals` | INTEGER | |
| `matched_deals` | INTEGER | |
| `unmatched_deals` | INTEGER | |
| `raw_summary` | JSONB | Full coverage dict from run_gclid_match() |
| `created_at` | TIMESTAMPTZ | |

---

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
| GET | `/api/dashboard/trends` | Auth | Previous-period trend comparison for dashboard (DB, ?days=) |
| GET | `/api/action-queue` | Auth | Ranked human-review queue (DB, ?days=) |
| GET | `/api/datasets/freshness` | Auth | Per-dataset sync state / watermark (sync_state table) |
| GET | `/api/search-terms` | Auth | Paginated search-term fact rows (search_terms table, cursor pagination) |
| GET | `/api/gclid-attribution` | Auth | Paginated GCLID attribution rows (gclid_attribution table, cursor pagination) |
| GET | `/api/gclid-coverage` | Auth | GCLID coverage snapshots (gclid_coverage_snapshots table) |
| GET | `/api/attribution/quality` | Auth | Read-only attribution quality signals (gclid_attribution + sync_state + gclid_coverage_snapshots) |

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
