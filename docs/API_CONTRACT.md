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

#### `GET /api/campaigns?window=30d`
GENUINE selected-window campaign evidence (PR-ADS-143). The selected Evidence Window controls the **actual** metrics — spend, leads, SQLs, junk, junk rate and CPQL are computed from durable source tables for the window, **never** the `campaigns` scheduler snapshot.

**Auth:** Auth
**Query params:**
- `window` (string) — evidence window: `7d | 14d | 30d | 60d | 180d | all_time`. **Authoritative** when present; an unknown value returns **400** (never silently coerced).
- `days` (integer, default 30) — legacy fallback used only when `window` is absent. Honoured **exactly** (90 stays 90, 365 stays 365 — never snapped to the nearest dropdown window); out of range (not 1–365) → **400**.

Window boundaries are **inclusive of exactly N calendar dates** (`start = end − (N−1)`) and resolved in the Google Ads **account timezone (Europe/London)**, not an implicit UTC date. `all_time` → no lower bound.

**Sources (durable, reconciled):**
- **Spend** — `db.revenue_repository.fetch_canonical_campaign_spend(start, end)` over `google_ads_campaign_daily_spend`: native GBP always, FX-safe USD (`None` when FX coverage is incomplete — never native relabelled as USD). This is the **same canonical source** Revenue by Source and the Revenue Decision Mart use, so per-window spend reconciles exactly.
- **Lead outcomes** — `fetch_lead_quality(start, end)` over the durable `leads` table, bounded on the HubSpot business-event date (`contact_created_at`, the same grain as `spend_date`), deduplicated per contact, paid-search only, pseudo/email campaigns excluded. Confirmed SQL = status `qualified`; confirmed junk = `junk`. Junk rate uses the **approved denominator unchanged** (`verdicted = qualified + in_progress + junk + wrong_fit`; excludes `unknown`).

The campaign universe is the **UNION** of canonical campaigns with spend and mapped campaigns with lead outcomes — a campaign is never dropped because one side has no record. Campaigns join by the canonical lowercase campaign name (leads carry no `campaign_id`; the `_CAMPAIGN_CANONICAL` write-time vocabulary is authoritative). `all_time` means **NO lower date bound** → genuine cumulative totals, NOT the latest scheduler snapshot.

**Response 200:**
```json
{
  "window": "30d",
  "window_start": "2026-06-11",
  "window_end": "2026-07-11",
  "all_time": false,
  "generated_at": "2026-07-11T10:00:00Z",
  "spend_semantics": "selected_window_canonical_total",
  "spend_currency": "GBP",
  "reporting_currency": "USD",
  "lead_semantics": "selected_window_deduplicated_event_date",
  "campaigns": [
    {
      "campaign_name": "Brand - UK",
      "campaign_id": "101",
      "spend_native": 42180.5,
      "spend_usd": 53147.4,
      "spend_currency": "GBP",
      "fx_complete": true,
      "total_leads": 214,
      "confirmed_sqls": 38,
      "confirmed_junk": 12,
      "in_progress": 9,
      "wrong_fit": 6,
      "unknown": 149,
      "verdicted_leads": 65,
      "junk_rate_pct": 18.46,
      "cpql_usd": 1398.62,
      "campaign_key": "101",
      "aliases": ["mexico,chile"],
      "mapping_status": "mapped",
      "outcome_status": "SQL producer"
    }
  ],
  "summary": {
    "campaigns": 7,
    "spend_usd": 128956.42,
    "spend_native": 105213.95,
    "spend_currency": "GBP",
    "confirmed_sqls_total": 68,
    "confirmed_junk_total": 179,
    "overall_cpql_usd": 1896.42,
    "overall_cpql_scope": "mapped_only",
    "mapping_coverage": {
      "mapped_sqls": 68, "unmatched_sqls": 2, "excluded_not_google_sqls": 7,
      "total_paid_search_sqls": 77, "status": "partial"
    }
  },
  "audit": {
    "spend_source": "google_ads_campaign_daily_spend (canonical)",
    "lead_source": "leads (durable · contact_created_at · deduped · paid_search)",
    "window_start": "2026-06-11",
    "window_end": "2026-07-11",
    "all_time": false,
    "fx_status": "verified",
    "spend_reconciliation_status": "pass",
    "lead_reconciliation_status": "pass",
    "event_date_safe": true,
    "fx_missing_days": 0
  }
}
```

**Campaign identity mapping.** Campaigns are keyed by **`campaign_id`** (the stable Google Ads identity — two campaigns whose display names normalize to the same text are never merged). HubSpot/external lead labels are mapped to canonical campaigns through the durable `google_ads_campaign_identity` table (approved rows only), keyed by the normalized `external_campaign_label`. Precedence: (1) durable approved mapping (`manual` / `exact_normalized`); (2) exact-normalized fallback (`normalize_campaign_name`) against canonical spend names, applied **only** when no durable mapping exists and exactly one spend campaign matches; **never fuzzy**. `match_method = not_google_ads` labels are **excluded** from the Google Ads SQL/CPQL scope (surfaced only in `mapping_coverage`). Labels with no mapping are preserved as **Mapping Review** rows (`mapping_status: "unmatched"`, `campaign_id: null`). Each row exposes `campaign_key` (stable drawer key) and `aliases` (all approved external labels for that canonical campaign). `/api/campaign-detail` accepts `campaign_key` to resolve the headline by id, not display name.

**Outcome status (factual, window-safe — never an action verdict).** The SCALE/HOLD/FIX/CUT verdict doctrine is **NOT valid for arbitrary windows** (it bakes a fixed 30-day design — `min_confirmed_sqls_30d`, `analysis_window_days: 30` — plus a hardcoded `$200` dollar floor, and emits action recommendations calibrated per fixed run-period). So each row carries a factual `outcome_status` computed from the selected-window totals only, first match wins (**risk-first**):
1. **Mapping review** — an unmatched lead label (no canonical spend id).
2. **Data unavailable** — spend and lead sources both unavailable (never coerced to 0).
3. **Junk-heavy** — `junk_rate_pct ≥ 25` on a verdicted sample `≥ 5` (rate + count from config; wins over an incidental SQL so 1 SQL + overwhelming junk is never shown green).
4. **SQL producer** — `confirmed_sqls > 0`.
5. **Spend without SQL proof** — `spend_native > 0`, no confirmed SQLs.
6. **No outcome evidence** — nothing to show for the window.

**Null / unavailable behaviour (no fabricated zeros).** `spend_native`/`spend_usd` are `None` for a campaign with no canonical spend row (unmapped/unavailable — never £0). Lead metrics are genuine `0` when the lead source is live and the campaign has no rows, but `None` when the lead source is unavailable. `cpql_usd` = window USD spend ÷ confirmed SQLs; `None` when spend/FX or the SQL denominator is unavailable, and a genuine zero SQL count renders `N/A` (never `$0`). `spend_usd` is `None` (withheld) whenever FX coverage is incomplete — native GBP is still returned.

**Summary / KPIs + CPQL scope.** `spend_native`/`spend_usd` come straight from the canonical window totals, so the KPI spend **reconciles exactly** with canonical spend for the window (and, for `all_time`, with the same canonical all-time spend used by Revenue by Source / the Revenue Decision Mart). `confirmed_sqls_total`/`confirmed_junk_total` are the **Google-Ads-mapped** scope only. `overall_cpql_usd` = canonical Google Ads **USD spend ÷ SQLs mapped to those same Google Ads campaigns** — it **excludes** unmatched and not-Google-Ads SQLs, so an unmatched qualified lead can never lower it. `overall_cpql_scope` is `"complete"` when mapping coverage is complete, else `"mapped_only"` (the UI discloses "Mapped campaigns only"), or `"unavailable"`. `mapping_coverage` exposes `mapped_sqls`, `unmatched_sqls`, `excluded_not_google_sqls`, `total_paid_search_sqls`, and `status` (`complete | partial | unavailable`).

**Audit metadata (machine-verifiable reconciliation — not shown in the UI).** For every window the `audit` block exposes `spend_source`, `lead_source`, `window_start`, `window_end`, `all_time`, `fx_status` (`verified | incomplete | unavailable`), `spend_reconciliation_status` and `lead_reconciliation_status` (`pass | variance | unavailable`). Reconciliation asserts: sum of per-campaign native spend = canonical native total; sum of per-campaign USD = canonical USD total when FX is complete; per-campaign SQL/junk totals = the deduplicated lead aggregate; `all_time` has no lower date bound; no amount is sourced from `AVG()`/`SUM()` over overlapping snapshot rows.

**DB-unavailable shape.** When both durable sources are down (never a 500):
```json
{
  "window": "30d", "db_unavailable": true, "campaigns": [],
  "spend_semantics": "selected_window_canonical_total",
  "summary": {"campaigns": 0, "spend_usd": null, "spend_native": null, "spend_currency": "GBP", "confirmed_sqls_total": null, "confirmed_junk_total": null, "overall_cpql_usd": null},
  "audit": {"...": "reconciliation statuses are 'unavailable'"}
}
```

> **BREAKING (PR-ADS-143).** The Campaign Evidence page no longer uses the `campaigns` scheduler-snapshot contract. Removed per-campaign fields: `verdict`, `latest_verdict`, `verdict_reason`, `clicks`, `impressions`, `conversions`, `run_count`, `last_run_date`, and all `metric_semantics`/`snapshot_date`/`snapshot_metric_period_days`/`snapshot_period_available` snapshot metadata (the PR-142 latest-snapshot contract). Removed summary fields: `verdict_counts`, `completeness`, `spend_requiring_review`. New per-campaign fields: `spend_native`, `spend_usd`, `spend_currency`, `fx_complete`, `total_leads`, `confirmed_sqls`, `confirmed_junk`, `in_progress`, `wrong_fit`, `unknown`, `verdicted_leads`, `junk_rate_pct`, `cpql_usd`, `mapping_status`, `outcome_status`; new top-level `window_start`/`window_end`/`all_time`/`audit`; new summary shape `{campaigns, spend_usd, spend_native, confirmed_sqls_total, confirmed_junk_total, overall_cpql_usd}`. The only consumer (`static/app.js` `loadCampaignEvidence`) was migrated in the same change. Revenue by Source, the Revenue Decision Mart, ROAS by Campaign, Dashboard campaign totals, FX logic and campaign mapping rules are unchanged.

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
Google Ads API geo performance data aggregated by country and campaign for the last N days.

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
`country` may be `null`/blank if the upstream Google Ads API data does not include a country value.
Data represents Google Ads API geo performance — not HubSpot lead quality.
No write operations are performed by this endpoint.
Used by the Geo Intelligence page (PR-ADS-030).
When database is unavailable: `{ "days": 30, "rows": [], "db_unavailable": true }`

---

#### `GET /api/keywords?days=30`
Google Ads API keyword performance data aggregated by campaign, ad group, keyword, and match type for the last N days.

**Auth:** Auth
**Query params:** `days` (integer, default 30, max 365)
**Read-only:** Yes — no write to Google Ads or any external system
**Source:** `keywords` table — Google Ads API keyword performance data persisted per run

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
- `match_type` may be `null`/blank if the upstream Google Ads API data does not include a match type value.
- `quality_score` may be `null` if not reported by the Google Ads API for a keyword.
- `cpc_usd` is recalculated server-side from `spend / clicks` where clicks > 0; otherwise 0.
- Rows are aggregated over the selected window — `spend_usd`, `clicks`, `impressions`, and `conversions` are summed; `quality_score` is averaged.
- `runs` is the count of distinct run IDs contributing to each aggregated row.
- Rendered by the Keywords page as of PR-ADS-032. Shows Google Ads API keyword performance only — not HubSpot lead-quality data. Does not include full user search terms. Quality score and match type may be null/unknown.

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
    "keywords": "Google Ads API keyword performance",
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
- `campaign` — GENUINE selected-window headline evidence (PR-ADS-143), built from the SAME `services.campaign_evidence_service` the `/api/campaigns` table uses, so the drawer headline matches the table row exactly. Fields: `campaign_id`, `campaign_key`, `aliases`, `spend_native`, `spend_usd`, `spend_currency`, `fx_complete`, `total_leads`, `confirmed_sqls`, `confirmed_junk`, `in_progress`, `wrong_fit`, `unknown`, `verdicted_leads`, `junk_rate_pct`, `cpql_usd`, `outcome_status`, `mapping_status`, `window`, `window_start`, `window_end`, `all_time`. It is **not** a scheduler snapshot — there is no `verdict`, `snapshot_date`, or `snapshot_metric_period_days`. Accepts `campaign_key` (query param) to resolve by stable id — when supplied it matches the id ONLY (no display-name fallback), so a wrong key returns absent rather than a same-named duplicate. Unavailable metrics preserve `null` (never coerced to 0).
- `lead_quality` / `countries` / `recent_leads` — aggregated from the durable `leads` table across the campaign's **approved alias set** (canonical Google Ads name + every approved `external_campaign_label`, excluding `not_google_ads`), on the HubSpot event date (`contact_created_at`), deduplicated per contact with the same paid-search / pseudo-email / lead-truth exclusions — so they **reconcile exactly** with the table row. A Mapping Review row uses only its exact unmatched label. `label_set` echoes the labels used.
- `keywords` / `waste_terms` — the **latest coherent Google Ads API snapshot** per keyword / term (`DISTINCT ON … ORDER BY run_date DESC`), scoped by the campaign's label set — **never SUM'd across overlapping scheduler snapshots**. `keywords_note` = "Latest keyword snapshot — not selected-window totals". `data_sources` labels them as latest Google Ads API snapshots (no "Windsor" label); the headline is "Canonical daily Google Ads spend + HubSpot event-date lead evidence".
- `lead_quality` — HubSpot-derived `status_category` from the leads table, deduplicated by `contact_id` (latest run per contact wins; null `contact_id` rows treated as unique).
- `countries` — deduped leads grouped by `COALESCE(NULLIF(BTRIM(country), ''), '(unknown)')`, sorted by total leads descending. Includes `in_progress` in both the response and the verdicted_leads denominator (consistent with lead-quality).
- `keywords` — top 10 keyword rows by spend from the keywords table, aggregated by keyword + match_type. Google Ads API platform metrics only — no HubSpot lead quality joined.
- `waste_terms` — top 10 waste term rows by spend from the waste_terms table, aggregated by search_term + junk_category + matched_pattern.
- `recent_leads` — 10 most recent deduped leads for this campaign (by run_date descending). Does not expose contact_id.
- `lead_quality.junk_rate_pct` — `confirmed_junk / verdicted_leads × 100`; `null` when `verdicted_leads = 0`. Unknown contacts (including `OPEN - Connecting`) are **excluded** from the denominator.

**Scope boundaries:**
- Does not write to Google Ads.
- Does not write to HubSpot.
- Keyword section is Google Ads API platform metrics only.
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
- Keyword `evidence.google_ads_conversions` reflects Google Ads API platform conversions only, not HubSpot SQLs.

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
Enhanced with canonical freshness semantics (PR-ADS-067).

**Auth:** Auth
**Read-only:** Yes — no live fetch, no sync execution, no external API calls
**Source:** `sync_state` table (PR-ADS-039), `sync_batches`, row counts from data tables

**Query parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 60 | Window for row count queries (clamped to 1–90) |

**Known datasets:**

> **PR-ADS-105:** Platform Evidence datasets are sourced directly from the
> Google Ads API (scheduler cutover landed in PR-ADS-104, which writes sync
> batches with `source="google_ads_api"`). Windsor remains only as
> legacy/deprecated history, not the active source.

- `google_ads_api` / `campaigns`
- `google_ads_api` / `keywords`
- `google_ads_api` / `search_terms`
- `google_ads_api` / `geo`
- `hubspot` / `contacts`
- `hubspot` / `deals`
- `gclid` / `matches`
- `gclid` / `coverage_snapshots`
- `analysis` / `waste_terms`
- `computed` / `ngrams`
- `analysis` / `historical_intelligence`

**Status values (legacy):** `success` | `failed` | `running` | `unknown`

**Canonical status values (PR-ADS-067):**
| Status | Severity | Description |
|--------|----------|-------------|
| `fresh_with_data` | ok | Sync succeeded recently and rows exist in window |
| `fresh_but_empty` | warning | Sync succeeded but zero rows in window |
| `stale_with_data` | warning | Rows exist but sync is older than threshold |
| `stale_and_empty` | error | No rows and no recent sync |
| `failed` | error | Latest sync/batch failed |
| `running` | neutral | Sync currently in progress |
| `not_run` | neutral | No sync recorded yet |
| `dependency_blocked` | warning | Upstream dataset is unavailable |
| `db_unavailable` | error | Database connection issue |
| `unknown` | neutral | Could not classify |

**Response 200 (with sync data and canonical fields):**
```json
{
  "datasets": [
    {
      "source": "google_ads_api",
      "dataset": "campaigns",
      "status": "success",
      "last_successful_sync_at": "2026-05-04T06:00:00+00:00",
      "last_source_date": "2026-05-03",
      "last_batch_id": 12,
      "error_message": null,
      "updated_at": "2026-05-04T06:03:00+00:00",
      "canonical_status": "fresh_with_data",
      "severity": "ok",
      "rows_in_window": 45292,
      "latest_source_date": "2026-05-03",
      "last_batch_row_count": 45292,
      "stale_threshold_days": 8,
      "depends_on": [],
      "dependency_status": null,
      "reason": "Data present and recently synced.",
      "next_action": "No action needed."
    }
  ],
  "summary": {
    "total": 11,
    "success": 5,
    "failed": 1,
    "running": 0,
    "unknown": 5
  },
  "canonical_summary": {
    "fresh_with_data": 3,
    "fresh_but_empty": 1,
    "not_run": 5,
    "failed": 1,
    "dependency_blocked": 1
  },
  "db_unavailable": false
}
```

**Response 200 (no sync rows yet — returns known placeholders):**
```json
{
  "datasets": [
    { "source": "google_ads_api",  "dataset": "campaigns",    "status": "unknown", "last_successful_sync_at": null, "last_source_date": null, "last_batch_id": null, "error_message": null, "updated_at": null, "canonical_status": "not_run", "severity": "neutral", "rows_in_window": null, "latest_source_date": null, "last_batch_row_count": 0, "stale_threshold_days": 8, "depends_on": [], "dependency_status": null, "reason": "No sync state or sync batches exist for this dataset.", "next_action": "Trigger a sync via scheduler or manual run." }
  ],
  "summary": {
    "total": 11,
    "success": 0,
    "failed": 0,
    "running": 0,
    "unknown": 11
  },
  "canonical_summary": { "not_run": 11 },
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
- As of PR-ADS-051, tracked raw-fact freshness includes `google_ads_api/search_terms` on daily/weekly/monthly runs,
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
    "source": "google_ads_api",
    "dataset": "search_terms",
    "table": "search_terms",
    "days": 14,
    "rows_in_window": 45292,
    "total_rows_in_window": 45292,
    "rows_returned": 100,
    "latest_source_date": "2026-05-25",
    "is_empty": false,
    "note": "is_flagged_waste is tri-state: null = not analyzed, true = flagged waste, false = analyzed clean. Current Windsor connector is confirmed up to last_14d search-term window unless plan supports more.",
    "warning": null
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
  "data_quality": { "source": "google_ads_api", "dataset": "search_terms", "status": "db_unavailable" },
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

**Frontend usage (as of PR-ADS-053):**
- As of PR-ADS-053, Search Terms KPI cards use `/api/search-terms/summary`. The table remains cursor-paginated via `/api/search-terms`.
- UI uses cursor pagination via `pagination.next_cursor` — Load More button appends rows.
- KPI cards show backend summary counts and spend for the selected filter/window.
- Load More only appends table rows; it does not reload or alter the KPI summary.
- All analysis-state filters (`flagged`, `clean`, `unanalyzed`) are applied server-side via `waste_state`. Client-side state filtering has been removed.
- Page is read-only: no negative keyword push, no marking/editing waste state, no campaign actions.

---

#### `GET /api/search-terms/summary`
Aggregate summary counts for the selected filter/window.

**Auth:** Auth
**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system
**Source:** `search_terms` table (PR-ADS-040)
**No pagination** — returns a single aggregate response for the entire filtered scope.

**Query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `days` | integer | 14 | Look-back window (1–90) |
| `campaign` | string | — | Exact canonical campaign_name match |
| `match_type` | string | — | Case-insensitive contains match on match_type |
| `q` | string | — | Case-insensitive contains search on search_term |
| `waste_state` | string | all | Analysis-state filter. Allowed: `all`, `flagged`, `clean`, `unanalyzed`. Aliases: `waste`=`flagged`, `analyzed_clean`=`clean`, `unanalysed`=`unanalyzed`. Invalid values return HTTP 400. |
| `waste_only` | boolean | false | Deprecated. Equivalent to `waste_state=flagged` when `waste_state` is not provided. |
| `min_spend` | numeric | — | Minimum spend_usd |

**Filter behaviour:**

- The `summary` object respects `waste_state` — it reflects totals for the selected filter/window.
- The `analysis_state` breakdown respects base filters (days, campaign, match_type, q, min_spend) but **ignores** the selected `waste_state`, so callers can see the full flagged/clean/unanalyzed distribution within the selected scope even when `waste_state` is set to a single bucket.

**Response 200:**
```json
{
  "days": 14,
  "filters": {
    "campaign": "global - competitors",
    "match_type": "broad",
    "q": "gratis",
    "min_spend": 10,
    "waste_state": "all"
  },
  "summary": {
    "total_terms": 128,
    "unique_search_terms": 94,
    "total_spend_usd": 2480.50,
    "total_clicks": 410,
    "total_impressions": 18200,
    "google_conversions": 17.0,
    "avg_cpc_usd": 6.05,
    "ctr_pct": 2.25,
    "google_conversion_rate_pct": 4.15
  },
  "analysis_state": {
    "flagged":    { "rows": 22, "spend_usd": 610.25 },
    "clean":      { "rows": 36, "spend_usd": 740.10 },
    "unanalyzed": { "rows": 70, "spend_usd": 1130.15 }
  },
  "data_quality": {
    "source": "google_ads_api",
    "dataset": "search_terms",
    "table": "search_terms",
    "days": 14,
    "rows_in_window": 128,
    "total_rows_in_window": 128,
    "rows_returned": 128,
    "note": "Summary is computed from stored search_terms rows in PostgreSQL. Google conversions are platform conversions, not HubSpot SQLs."
  },
  "db_unavailable": false
}
```

**DB unavailable response:**
```json
{
  "days": 14,
  "filters": { "waste_state": "all" },
  "summary": {
    "total_terms": 0,
    "unique_search_terms": 0,
    "total_spend_usd": 0,
    "total_clicks": 0,
    "total_impressions": 0,
    "google_conversions": 0,
    "avg_cpc_usd": null,
    "ctr_pct": null,
    "google_conversion_rate_pct": null
  },
  "analysis_state": {
    "flagged":    { "rows": 0, "spend_usd": 0 },
    "clean":      { "rows": 0, "spend_usd": 0 },
    "unanalyzed": { "rows": 0, "spend_usd": 0 }
  },
  "data_quality": { "source": "google_ads_api", "dataset": "search_terms", "status": "db_unavailable" },
  "db_unavailable": true
}
```

**Important scope notes:**
- Google conversions are platform conversions, not HubSpot SQLs.
- Does not classify search terms, mark rows clean or waste, or write to any external system.
- `avg_cpc_usd`, `ctr_pct`, and `google_conversion_rate_pct` are `null` when the denominator is zero.
- Invalid `waste_state` values return HTTP 400 with `{ "detail": "Invalid waste_state. Allowed values: all, flagged, clean, unanalyzed." }`.

---

#### `GET /api/search-terms/ngrams`
Read-only n-gram analysis over stored `search_terms` rows. (PR-ADS-055)

**Auth:** Auth
**Read-only:** Yes — no write to Google Ads, HubSpot, or any external system
**Source:** `search_terms` table — same source as `/api/search-terms`
**Prototype:** Yes — dynamic analysis over a filtered recent window. May be performance-hardened in a later phase.

**Query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `days` | integer | 14 | Look-back window (1–30, max 30 in prototype) |
| `campaign` | string | — | Exact canonical campaign_name match |
| `match_type` | string | — | Case-insensitive contains match on match_type |
| `waste_state` | string | all | Analysis-state filter. Allowed: `all`, `flagged`, `clean`, `unanalyzed`. Aliases: `waste`=`flagged`, `analyzed_clean`=`clean`, `unanalysed`=`unanalyzed`. Invalid values return HTTP 400. |
| `q` | string | — | Case-insensitive contains search on `search_term` (applied before tokenization) |
| `min_spend` | numeric | 0 | Row-level minimum spend_usd filter |
| `n` | string | `1,2,3` | Comma-separated n-gram lengths. Allowed values: `1`, `2`, `3`. Invalid values return HTTP 400. |
| `limit` | integer | 100 | Max aggregated n-gram rows to return (1–250) |

**Source row ordering:** Source rows are fetched ordered by `spend_usd DESC`, `source_date DESC`, `id DESC` before the prototype row cap (10,000 rows). This ensures economically significant data is prioritised when the cap applies.

**Response 200:**
```json
{
  "days": 14,
  "filters": {
    "campaign": "global - broad",
    "match_type": "broad",
    "waste_state": "all",
    "q": null,
    "min_spend": 0,
    "n": [1, 2, 3],
    "limit": 100
  },
  "rows": [
    {
      "ngram": "gratis",
      "n": 1,
      "language": "spanish",
      "row_count": 18,
      "unique_search_terms": 12,
      "campaigns_count": 3,
      "ad_groups_count": 5,
      "keywords_count": 6,
      "total_spend_usd": 420.50,
      "total_clicks": 70,
      "total_impressions": 3100,
      "google_conversions": 0.0,
      "avg_cpc_usd": 6.01,
      "ctr_pct": 2.26,
      "google_conversion_rate_pct": 0.0,
      "flagged_waste_rows": 11,
      "clean_rows": 0,
      "unanalyzed_rows": 7,
      "flagged_waste_spend_usd": 310.00,
      "campaigns_sample": ["global - broad", "latam - broad"],
      "search_terms_sample": ["software de logistica gratis", "sistema de envios gratis"]
    }
  ],
  "summary": {
    "ngrams_returned": 100,
    "source_rows_analyzed": 2500,
    "unique_search_terms_analyzed": 900
  },
  "data_quality": {
    "source": "search_terms",
    "dataset": "ngrams",
    "note": "N-gram analysis is read-only. Google conversions are platform conversions, not HubSpot SQLs. No negative keyword candidates are created. N-gram tokenization uses config/ngram_stopwords.yaml for stopwords and protected tokens. Protected tokens are preserved even if present in a stopword list. The endpoint remains read-only and does not generate negative keyword candidates."
  },
  "db_unavailable": false
}
```

**`data_quality.row_cap_applied`** — present and `true` only when the 10,000-row prototype cap was applied. In that case `data_quality.row_cap` is also included.

**DB unavailable response:**
```json
{
  "days": 14,
  "filters": { "waste_state": "all", "n": [1, 2, 3], "limit": 100 },
  "rows": [],
  "summary": { "ngrams_returned": 0, "source_rows_analyzed": 0, "unique_search_terms_analyzed": 0 },
  "data_quality": { "source": "search_terms", "dataset": "ngrams", "status": "db_unavailable" },
  "db_unavailable": true
}
```

**Invalid `n` response:** HTTP 400 — `{ "detail": "Invalid n. Allowed values: 1, 2, 3." }`

**Invalid `waste_state` response:** HTTP 400 — `{ "detail": "Invalid waste_state. Allowed values: all, flagged, clean, unanalyzed." }`

**`language` field values:** `arabic`, `spanish`, `english_or_latin` — lightweight script/language heuristic on the n-gram phrase.

**Sorting:** Aggregated rows are sorted `total_spend_usd DESC`, `row_count DESC`, `ngram ASC`. Spend-first surfaces economically meaningful patterns.

**UI row-cap visibility (PR-ADS-061):** The N-Gram UI surfaces `data_quality.row_cap_applied` when present. Operators should narrow filters/date range when the row cap is applied.

**Important scope notes:**
- Auth required — same session cookie as `/api/search-terms`.
- Read-only. No writes to Google Ads, HubSpot, or any external system.
- Source is `search_terms` table only. No HubSpot join.
- Does **not** return `attention_status`, `review_status`, `evidence_note`, `severity`, `score`, `negative_candidate`, `recommended_action`, or any scoring/recommendation field.
- Does **not** create negative keyword candidates.
- Does **not** push negative keywords.
- `google_conversions` are Google Ads platform conversions — **not** HubSpot SQLs.
- `search_terms_sample` contains up to 5 raw search terms with the highest `spend_usd` that contain the n-gram.
- `campaigns_sample` contains up to 5 campaign names observed for the n-gram.
- Prototype max window is 30 days. A larger window or materialization may be added in a later phase.
- The `n` parameter accepts `1`, `2`, `3` only. Values of `0`, `4+`, or non-integer strings return HTTP 400.
- No cursor needed — endpoint returns aggregated rows with `limit` cap.

**Frontend usage (PR-ADS-056):**
As of PR-ADS-056, this endpoint is rendered by the N-Grams page in the SPA.
The page is read-only and displays factual n-gram metrics only.
It does not create negative keyword candidates, push changes, or provide recommendations.

> **PR-ADS-144:** the Search Terms + Patterns page now uses the
> `/api/search-term-evidence` family below. The three legacy endpoints above
> remain live (the Campaign Evidence drawer's N-Gram drilldown still uses
> `/api/search-terms/ngrams`) but are no longer the page's primary source.

---

#### `GET /api/search-term-evidence` *(PR-ADS-144 — Search Terms page, Terms tab)*

Complete selected-window Search Term Universe: durable `search_terms` rows
bounded by **`source_date`** (never `run_date`), deduplicated by the table's
natural key, aggregated per **search term × canonical campaign**, with
server-side filtering / sorting / pagination and complete-population KPIs.

**Auth:** Auth
**Read-only:** Yes — no negative keywords, no Google Ads / HubSpot writes

**Query Parameters**

| Param | Default | Notes |
|---|---|---|
| `window` | `30d` | `7d\|14d\|30d\|60d\|180d\|all_time`. Rolling windows cover exactly N account-local dates (Europe/London — same boundary as Campaign Evidence); `all_time` has **no lower bound**. Unknown window → **HTTP 400**, never silently coerced. |
| `page` | `1` | 1-based page number. |
| `page_size` | `50` | 1–200. |
| `q` | — | Case-insensitive contains filter on `search_term`. |
| `campaign` | — | Canonical `campaign_key` (from `facets.campaigns`). |
| `state` | — | `flagged\|clean\|needs_review` (invalid → 400). |
| `junk_category` | — | From `facets.junk_categories`. |
| `min_spend` | — | Minimum reported search-term spend (USD). Null spend never passes a floor. |
| `sort` | `spend` | `spend\|clicks\|cpc\|conversions\|last_seen\|term` (invalid → 400). Nulls always sort last — never coerced to 0. |

**Response (200)**

```jsonc
{
  "window": "30d", "window_start": "2026-06-13", "window_end": "2026-07-12",
  "all_time": false, "generated_at": "…",
  "spend_semantics": "reported_search_term_spend",
  "reporting_currency": "USD",
  "kpis": {
    "reported_terms": 412,            // COMPLETE filtered population, never the page
    "unique_search_terms": 388,
    "reported_spend_usd": 1234.56,    // reported search-term spend — NOT account spend
    "clicks": 2210,
    "flagged_waste": 61, "reviewed_clean": 214, "needs_review": 137,
    "coverage": {                     // Search-term reporting coverage (diagnostic)
      "status": "ok|unavailable",
      "canonical_spend_usd": 1890.0,  // canonical campaign spend, FX-safe USD
      "reported_search_term_spend_usd": 1234.56,
      "coverage_pct": 65.32,          // null when contracts are not comparable
      "note": "…"
    }
  },
  "rows": [{
    "search_term": "freight software", "campaign_key": "123",
    "campaign_name": "Brand - UK", "mapping_status": "mapped|unmatched|not_google_ads",
    "aliases": ["brand - uk"],
    "state": "flagged|clean|needs_review",   // tri-state is_flagged_waste truth
    "spend_usd": 20.0, "spend_native": 15.87, "native_currency": "GBP",
    // fx_complete / currency_status reflect the ACTUAL per-date conversion —
    // "verified" when every source date had a rate, "fx_incomplete" when a
    // date was missing (spend_usd then null), "mixed_or_unproven"/"unavailable"
    // when provenance is not proven. NOT merely provenance availability.
    "fx_complete": true, "currency_status": "verified",
    "clicks": 4, "impressions": 40,
    "conversions": 0.0,                      // platform evidence only — not an SQL
    "cpc_usd": 5.0, "first_seen": "2026-07-01", "last_seen": "2026-07-02",
    "junk_categories": [], "matched_patterns": [], "source_rows": 2
  }],
  "pagination": { "total_count": 412, "returned_count": 50, "page": 1,
                  "page_size": 50, "has_more": true },
  "facets": { "campaigns": [{ "campaign_key": "…", "campaign_name": "…",
                              "mapping_status": "…" }],
              "junk_categories": ["job_seeker"] },
  "filters": { "q": null, "campaign": null, "state": null,
               "junk_category": null, "min_spend": null, "sort": "spend" },
  "audit": {
    "source_table": "search_terms",
    "date_field": "source_date",
    "window_start": "2026-06-13", "window_end": "2026-07-12", "all_time": false,
    "account_timezone": "Europe/London",
    "currency_semantics": "fx_converted: search_terms stores cost_micros (native account currency) + currency_code + source_system; FX-converted to USD per-row using the same per-date FX doctrine as canonical campaign spend",
    "deduplication_key": "source_date + COALESCE(campaign_name,'') + COALESCE(campaign_id,'') + COALESCE(ad_group,'') + COALESCE(keyword,'') + COALESCE(match_type,'') + search_term (UNIQUE index idx_search_terms_unique_fact; writer upserts ON CONFLICT)",
    "classification_semantics": "is_flagged_waste tri-state: …",
    "campaign_identity_status": "available|unavailable",
    "canonical_spend_source": "google_ads_campaign_daily_spend (canonical)",
    "search_term_spend_source": "search_terms.cost_micros (native account currency, FX-converted to USD)",
    "coverage_status": "ok|unavailable",
    "pagination_complete": true,               // KPIs use the complete population
    "reconciliation_status": "pass|variance|unavailable",
    "reconciliation_detail": {
      "source_row_reconciliation": "pass",     // merged units == raw source row count
      "spend_reconciliation": "pass",          // unit spend sum == deduped source total
      "state_count_reconciliation": "pass"     // state counts add back to total_count
    }
  }
}
```

**Truth contract**

- **Duplication:** the `search_terms` table enforces a UNIQUE natural key
  (`source_date`, `campaign_name`, `campaign_id`, `ad_group`, `keyword`,
  `match_type`, `search_term`) and the writer upserts ON CONFLICT on that key,
  so a repeated scheduler run can never multiply a term/day/campaign fact. The
  audit block re-proves this per request (`source_row_reconciliation`,
  `spend_reconciliation`).
- **Currency (genuine per-source-date FX):** the table durably stores
  `cost_micros` (native account currency from Google Ads `metrics.cost_micros`)
  plus `currency_code` and `source_system` provenance. USD is computed as
  `Σ(native_day × rate[day])` — **each source date is converted at its own FX
  rate**, never a window-average rate — using the same `fx_rates` doctrine as
  canonical campaign spend. The legacy `spend_usd` column contains
  `cost_micros / 1_000_000` (native account currency, **NOT** proven USD) and is
  never surfaced to the UI as USD. When ANY required source-date rate is
  missing, or currency provenance is unproven/mixed, `spend_usd`, `cpc_usd`, and
  coverage are withheld (`null` / `Unavailable`) and `fx_complete` is `false` —
  native spend (`spend_native`, `native_currency`) is still preserved.
  Reporting **Coverage** uses the FX-converted window search-term USD as its
  numerator (never the legacy `spend_usd` raw total), and is available only when
  that numerator is fully FX-safe AND canonical campaign USD is FX-complete.
- **Storage migration:** a database predating this contract is upgraded by a
  guarded, idempotent migration (`db/schema.py`, migration id
  `PR-ADS-144-currency-and-id-key`): it `ADD COLUMN IF NOT EXISTS` the three
  lineage columns, deterministically removes any null-`campaign_id` legacy row
  that collides with an id-bearing row for the same fact (the id-bearing
  identity wins; two distinct ids sharing a name are untouched), and drops/
  recreates `idx_search_terms_unique_fact` with `campaign_id` included. The
  writer's `ON CONFLICT` target matches the live index and, going forward,
  supersedes a null-id twin when an id-bearing row for the same fact is written
  (no double-count). `docs`-level and PG-backed integration tests
  (`tests/test_pr_ads_144_pg_integration.py`) prove the upgrade end-to-end.
- **Daily drawer currency (`/term` → `daily.rows`):** each row carries
  `source_date`, `cost_micros`, `native_currency`, `spend_native`, `spend_usd`
  (converted at THAT day's rate, or `null`), `reporting_currency`,
  `fx_complete`, `currency_status`, `clicks`, `impressions`. A day without a
  rate shows native only.
- **Classification:** `is_flagged_waste` true → `flagged` (Flagged waste,
  human-review candidate), false → `clean` (Reviewed clean — never renamed
  "valuable"), NULL → `needs_review`. Platform conversions never change the
  state.
- **Campaign identity (PR-ADS-143 rules):** stored `campaign_id` first, then
  the approved durable mapping, then the exact-normalized fallback against
  canonical spend names (single id only) — never fuzzy. `not_google_ads`
  labels are excluded from Google Ads campaign identity; unmatched labels are
  `mapping_status: "unmatched"` (Mapping review). Two campaign ids sharing a
  display name are never merged.
- **DB unavailable:** same shape with `"db_unavailable": true`, null KPIs
  (never fabricated zeros) and `reconciliation_status: "unavailable"`.

---

#### `GET /api/search-term-evidence/term` *(PR-ADS-144 — term drawer)*

**Auth:** Auth · **Read-only:** Yes

| Param | Notes |
|---|---|
| `term` | Required — exact search term. |
| `campaign_key` | Optional — the table row's canonical campaign key. Omitted → combined view across campaigns. |
| `window` | Same contract as above (400 on unknown). |

Returns `{term (same row shape as the table), campaigns (per-campaign
context incl. mapping_status/aliases/source_labels), matching_context
(ad_groups/keywords/match_types), classification (state, junk_categories,
matched_patterns, crm_junk_confirmed, classification_date/source from the
latest waste_terms analysis row — `null`/Unavailable when not stored),
platform_activity (conversions + disclosure that a platform event is not a
confirmed SQL/customer/closed-won outcome), daily {rows per source_date —
only dates the source actually reported; missing dates are never fabricated
as zero}}`. Unknown term → `{"_not_found": true}`.

---

#### `GET /api/search-term-evidence/patterns` *(PR-ADS-144 — Patterns tab)*

Patterns (n-grams) derived from the **same** selected-window deduplicated
Search Term Universe with the same filters, window, source-date boundary,
currency contract and classification states.

**Auth:** Auth · **Read-only:** Yes

| Param | Default | Notes |
|---|---|---|
| `window` | `30d` | Same contract as the Terms endpoint (400 on unknown). |
| `n` | `1` | Pattern word length `1\|2\|3` (invalid → 400). |
| `q` / `campaign` / `state` / `min_spend` | — | Same semantics as Terms (applied to underlying term rows before unification). |
| `min_terms` | — | Minimum unique terms per pattern. |
| `sort` | `spend` | `spend\|terms\|flagged\|pattern` (invalid → 400). |
| `limit` | `100` | 1–500 pattern rows; `pagination.total_count`/`has_more` disclose truncation. |

Response: `kpis {patterns_found, terms_analysed, patterns_with_flagged,
patterns_needing_review, reported_spend_represented_usd}`, `rows [{pattern, n,
signal (flagged_present|needs_review|mixed|reviewed_clean_only), terms,
flagged_terms, clean_terms, needs_review_terms, reported_spend_usd, clicks,
conversions, campaigns_count}]`, plus the same `audit` block extended with
`patterns_derivation` and `pattern_kpi_spend_semantics:
"unique_underlying_terms"`.

**N-gram overlap contract:** the same search term contributes to multiple
patterns, so pattern-row spend is NEVER additive and is never summed into an
account total. KPI spend is computed once per unique underlying term. The
machine-verifiable `overlap` block discloses `unique_terms_analysed`,
`total_pattern_memberships`, `overlapping_term_count` and `spend_semantics`.

---

#### `GET /api/search-term-evidence/patterns/detail` *(PR-ADS-144 — pattern drawer)*

**Auth:** Auth · **Read-only:** Yes

Params: `pattern` (required), `n` (1|2|3), `window`, plus the shared
`q`/`campaign`/`state`/`min_spend` filters. Returns `{pattern {…factual
split + unique-term totals + campaigns}, platform_activity, terms [{search_term,
state, campaigns, spend_usd, clicks, last_seen}] (top by spend, truncation
disclosed via terms_truncated), overlap_note}`. Totals use unique term
identities — a term is never totalled twice for appearing in multiple
campaigns, dates or pattern positions.

---

#### `GET /api/search-term-evidence/export` *(PR-ADS-144 — CSV export)*

**Auth:** Auth · **Read-only:** Yes

Same filter params as `GET /api/search-term-evidence` (no `page`/`page_size`).
Streams a `text/csv` attachment containing the **complete server-filtered
dataset** at term × campaign grain — never a silently truncated page — named
`search_terms_{window}_complete.csv`. When the source is unavailable the
endpoint returns **503** (an empty file is never presented as a complete
export). Invalid window/filters → 400.

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
| `source` | TEXT NOT NULL | `google_ads_api` \| `hubspot` \| `gclid` \| `windsor` (legacy) |
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
| GET | `/api/geo` | Auth | Google Ads API geo performance by country/campaign (DB, ?days=) |
| GET | `/api/keywords` | Auth | Google Ads API keyword performance by campaign/ad group/keyword (DB, ?days=) |
| GET | `/api/leads/country-summary` | Auth | HubSpot lead quality by country (DB, ?days=) |
| GET | `/api/config/ui-thresholds` | Auth | UI-safe display thresholds from config/thresholds.yaml |
| GET | `/api/dashboard/trends` | Auth | Previous-period trend comparison for dashboard (DB, ?days=) |
| GET | `/api/action-queue` | Auth | Ranked human-review queue (DB, ?days=) |
| GET | `/api/datasets/freshness` | Auth | Per-dataset sync state / watermark (sync_state table) |
| GET | `/api/search-terms` | Auth | Paginated search-term fact rows (search_terms table, cursor pagination) |
| GET | `/api/search-terms/summary` | Auth | Aggregate summary counts for selected filter/window (search_terms table, no pagination) |
| GET | `/api/search-terms/ngrams` | Auth | Read-only n-gram analysis over stored search_terms (aggregated, no pagination) |
| GET | `/api/search-term-evidence` | Auth | PR-ADS-144 Search Term Universe — selected-window source_date aggregates, complete-population KPIs, server-side pagination + audit block |
| GET | `/api/search-term-evidence/term` | Auth | PR-ADS-144 search-term evidence drawer (campaign context, classification proof, daily source-date series) |
| GET | `/api/search-term-evidence/patterns` | Auth | PR-ADS-144 Patterns (n-grams) derived from the same term population; unique-term KPI math + overlap disclosure |
| GET | `/api/search-term-evidence/patterns/detail` | Auth | PR-ADS-144 pattern drawer — unique underlying terms + factual split |
| GET | `/api/search-term-evidence/export` | Auth | PR-ADS-144 complete server-filtered CSV export (503 when source unavailable) |
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

## Six-Month Read-Only Governance

All endpoints in Phase 1 / Phase 1.5 are read-only with respect to external platforms. Local PostgreSQL writes may occur for imported data, reports, sync state, and analysis evidence, but no endpoint may write to Google Ads or HubSpot during the governance period.

See `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md` for the full policy.

---

## Search Terms Production Verdict (PR-ADS-066)

### `GET /api/system/search-terms-verdict`

**Auth:** Admin only (session with admin role, or `ADMIN_API_TOKEN` Bearer header)  
**Purpose:** Focused Search Terms pipeline health verdict. Reports whether Search Terms data is present, syncing, broken, or missing.  
**Added:** PR-ADS-066

#### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | int | 60 | Time window in days (1–90) |

#### Response Shape (OK)

```json
{
  "generated_at": "2026-05-25T12:00:00+00:00",
  "days": 60,
  "verdict": "OK",
  "reason": "Pipeline OK: DB has 45292 rows in 60-day window",
  "db": {
    "available": true,
    "rows_7d": 12000,
    "rows_14d": 24000,
    "rows_30d": 39000,
    "rows_60d": 45292,
    "latest_source_date": "2026-05-25",
    "blank_search_term_rows": 0,
    "spend_rows": 19000,
    "click_rows": 16000
  },
  "sync": {
    "latest_batch_status": "success",
    "latest_batch_row_count": 45292,
    "latest_batch_started_at": "2026-05-25T07:00:00",
    "sync_state_status": "success",
    "last_successful_sync_at": "2026-05-25T07:04:00"
  },
  "api": {
    "checked": false,
    "rows_returned": null,
    "total_rows_in_window": 45292,
    "is_empty": false
  },
  "next_action": "Search Terms pipeline is healthy. Proceed to Waste Terms/N-Grams confidence."
}
```

#### Response Shape (WINDSOR_PULL_EMPTY)

```json
{
  "generated_at": "2026-05-25T12:00:00+00:00",
  "days": 60,
  "verdict": "WINDSOR_PULL_EMPTY",
  "reason": "Windsor pull returned 0 rows and DB has 0 rows; source is empty or REST endpoint not returning data",
  "db": { "available": true, "rows_7d": 0, "rows_14d": 0, "rows_30d": 0, "rows_60d": 0, "latest_source_date": null, "blank_search_term_rows": 0, "spend_rows": 0, "click_rows": 0 },
  "sync": { "latest_batch_status": null, "latest_batch_row_count": null, "latest_batch_started_at": null, "sync_state_status": null, "last_successful_sync_at": null },
  "api": { "checked": false, "rows_returned": null, "total_rows_in_window": 0, "is_empty": true },
  "next_action": "Verify Windsor plan/API access or use MCP payload import path."
}
```

#### Verdicts

| Verdict | Meaning |
|---------|---------|
| `OK` | Search terms exist in DB and pipeline is healthy |
| `NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT` | No weekly run found; pipeline may not have run since deployment |
| `WINDSOR_PULL_EMPTY` | Windsor REST returned 0 rows and DB is empty |
| `WINDSOR_PULL_MISSING_SEARCH_TERM_FIELD` | Windsor returned rows but search_term field is missing |
| `FILE_EMPTY` | ads_search_terms.json is empty and DB has 0 rows |
| `DB_WRITE_FAILED` | Windsor returned rows but DB has none |
| `DB_HAS_ROWS_API_EMPTY` | DB has rows but /api/search-terms returns empty |
| `FRESH_BUT_EMPTY` | Sync says success but zero rows in window |
| `DB_UNAVAILABLE` | Database connection failed |
| `UNKNOWN` | Could not determine pipeline state |

#### Notes

- **Read-only.** No external API calls. No data mutation.
- Short-TTL cached (60s) to reduce DB load.
- Search Terms is the raw evidence layer. Waste Terms and N-Grams depend on this dataset.

---

## System Reality Audit (PR-ADS-064)

### `GET /api/system/reality-audit`

**Auth:** Admin only (session with admin role, or `ADMIN_API_TOKEN` Bearer header)  
**Purpose:** Read-only production reality diagnostic. Reports row counts, freshness status, and verdicts for every major dataset table.  
**Added:** PR-ADS-064

#### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | int | 60 | Time window in days (1–90) |

#### Response Shape

```json
{
  "generated_at": "2026-05-25T12:00:00+00:00",
  "days": 60,
  "db_available": true,
  "latest_run": {
    "run_type": "weekly",
    "status": "success",
    "started_at": "2026-05-25T00:01:00+00:00",
    "finished_at": "2026-05-25T00:05:00+00:00"
  },
  "datasets": {
    "search_terms": {
      "dataset": "search_terms",
      "rows_7d": 0,
      "rows_14d": 0,
      "rows_30d": 0,
      "rows_60d": 0,
      "latest_date": null,
      "freshness_status": "success",
      "source": "google_ads_api",
      "last_sync_type": "weekly/daily",
      "last_sync_status": "success",
      "verdict": "FRESH_BUT_EMPTY",
      "reason": "Sync status says 'success' but table has zero rows in 60d window"
    }
  },
  "sync_state": {},
  "pipeline_blockers": [
    {
      "page": "Search Terms",
      "blocker": "search_terms table: FRESH_BUT_EMPTY — ..."
    },
    {
      "page": "N-Grams",
      "blocker": "Depends on search_terms table which is empty/broken"
    }
  ]
}
```

#### Verdicts

| Verdict | Meaning |
|---------|---------|
| `OK` | Data present and within freshness threshold |
| `RUNNING` | Sync is currently in progress (data may still be loading) |
| `FRESH_BUT_EMPTY` | Sync says success but table has zero rows |
| `STALE` | Latest date exceeds staleness threshold |
| `EMPTY_VALID` | No sync expected and table is empty |
| `MISSING_TABLE` | Table does not exist in the database |
| `DB_UNAVAILABLE` | Database connection failed |
| `BROKEN` | Exception or inconsistent state |

#### Notes

- **Read-only.** No external API calls. No data mutation.
- Equivalent to running `python scripts/audit_production_reality.py --days 60 --json`
- Admin/manual diagnostic endpoint. Responses are short-TTL cached in API to reduce DB load from repeated polling.

---

## System Status War Room (PR-ADS-068)

### `GET /api/system/status-war-room`

**Auth:** Admin only (session with admin role, or `ADMIN_API_TOKEN` Bearer header)  
**Purpose:** Consolidated system status, blockers, pipelines, source health, scheduler state. One-page operational view.  
**Added:** PR-ADS-068

#### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | int | 60 | Time window in days (1–90) |

#### Response Shape

```json
{
  "generated_at": "2026-05-26T08:00:00+00:00",
  "days": 60,
  "overall_status": "warning",
  "overall_label": "System usable — Search Terms empty; 2 blocked",
  "summary": { "ok": 6, "warning": 1, "error": 0, "neutral": 1, "blocked": 2 },
  "critical_blockers": [
    {
      "id": "search_terms_empty",
      "severity": "warning",
      "title": "Search Terms has no usable rows",
      "affected_pages": ["Search Terms", "Waste Terms", "N-Grams"],
      "reason": "Search Terms is fresh_but_empty.",
      "next_action": "Check Search Terms verdict and Google Ads API search-term sync."
    }
  ],
  "sources": [
    {
      "source": "google_ads_api",
      "label": "Google Ads API",
      "status": "warning",
      "datasets": ["campaigns", "search_terms", "keywords", "geo"],
      "last_successful_sync_at": "2026-05-25T07:04:00+00:00",
      "latest_batch_status": "success",
      "next_action": "Check Search Terms if empty."
    }
  ],
  "pipelines": [
    {
      "key": "search_terms",
      "label": "Search Terms Pipeline",
      "source": "google_ads_api",
      "page": "Search Terms",
      "canonical_status": "fresh_but_empty",
      "severity": "warning",
      "rows_in_window": 0,
      "latest_source_date": null,
      "last_batch_row_count": 0,
      "depends_on": [],
      "blocks": ["waste_terms", "ngrams"],
      "reason": "Latest sync succeeded but no rows exist in the selected window.",
      "next_action": "Check source pull, sync batch row count, and pipeline verifier."
    }
  ],
  "scheduler": {
    "latest_daily": { "status": "success", "started_at": "2026-05-26T06:00:00+00:00", "finished_at": "2026-05-26T06:03:00+00:00" },
    "latest_weekly": { "status": "success", "started_at": "2026-05-25T07:00:00+00:00", "finished_at": "2026-05-25T07:08:00+00:00" },
    "latest_monthly": null,
    "latest_incremental": null
  },
  "page_impact": [
    {
      "page": "Waste",
      "status": "blocked",
      "blocked_by": "waste_terms",
      "reason": "Waste depends on Waste Terms."
    }
  ]
}
```

#### Notes

- **Read-only.** No external API calls. No data mutation. No scheduler triggers.
- Short-TTL cached (60s) to reduce DB load.
- Combines canonical freshness, pipeline dependencies, source health, and scheduler run state.
- Uses `services/system_status_service.py` for pure helper logic.

#### PR-ADS-095 — Refined truth semantics

PR-ADS-095 expanded the canonical state set so System Status can distinguish
"sync failed but rows still available" from "sync failed and no data":

- `data_available_latest_sync_failed` — sync failed, but `rows_in_window > 0`;
  the page is **degraded**, not blocked. Severity: **warning**.
- `failed_no_data` — sync failed and no usable rows; the page is **blocked**.
  Severity: **error**.
- `not_run_but_derivable` — derived dataset has not run, but its upstream has
  rows; the page is **action_needed**, not blocked. Severity: **warning**.
- `not_run_no_upstream_data` — derived dataset has not run and upstream is
  also not run / empty; effectively blocked. Severity: **error**.
- `unknown_row_count` — the row-count query was attempted but failed for this
  dataset at runtime. Severity: **neutral**.
- `row_count_not_enabled` — the dataset has no row-count query configured
  (e.g. missing or non-identifier table/date_column). Severity: **neutral**.
- `blocked_by_dependency` — refined emission for the previously-named
  `dependency_blocked`; emitted when upstream is actively broken. Severity:
  **error**. The legacy `dependency_blocked` state is still recognised
  downstream so older sync_state rows render correctly.
- `empty_success` — latest sync succeeded explicitly with zero rows from the
  source (vs. `fresh_but_empty` which means "window query is zero, batch
  reported rows"). Severity: **warning**.

`/api/datasets/freshness` and `/api/system/status-war-room` now share the
same upstream derivation logic: derived datasets whose upstream is in a
HAS_DATA state (fresh, stale_with_data, or data_available_latest_sync_failed)
are classified `not_run_but_derivable` in both endpoints.

`page_impact` entries now carry one of `ok | degraded | action_needed |
blocked | unknown`. Pipelines now also include a `page_status` field with the
same set. When the `scheduler` block is empty but `sync_info` shows successful
syncs, the response includes:

```json
"scheduler": {
  "latest_daily": null,
  "latest_weekly": null,
  "latest_monthly": null,
  "latest_incremental": null,
  "diagnostic_status": "no_scheduler_run_recorded",
  "message": "Source sync timestamps exist, but no scheduler run records were found.",
  "next_action": "Check whether background/manual syncs write scheduler metadata."
}
```

---

### `GET /api/diagnostics/window-semantics`

**Auth:** Admin only (session with admin role, or `ADMIN_API_TOKEN` Bearer header)
**Purpose:** Compare per-dataset row counts across multiple time windows to verify whether 7d / 30d / 60d windows actually produce different counts, and to surface whether row-count queries are unavailable due to a query failure or because the dataset doesn't expose row counts.
**Added:** PR-ADS-095

#### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `windows` | string | `7d,30d,60d` | Comma-separated windows. Valid: `7d, 14d, 30d, 60d, 90d, 365d` |

#### Response Shape

```json
{
  "generated_at": "2026-05-28T04:00:00+00:00",
  "windows": ["7d", "30d", "60d"],
  "datasets": [
    {
      "key": "campaigns",
      "source": "google_ads_api",
      "table": "campaigns",
      "date_column": "run_date",
      "window_counts": { "7d": 1200, "30d": 3900, "60d": 5707 },
      "latest_source_date": "2026-05-25",
      "latest_sync_status": "failed",
      "last_successful_sync_at": "2026-05-25T07:04:00+00:00",
      "missing_date_rows": 0,
      "invalid_date_rows": null,
      "diagnostic_status": "data_available_latest_sync_failed",
      "usable_for_page": true,
      "reason": "Latest sync failed, but usable rows exist in the selected window.",
      "next_action": "Review latest sync error, but page can still render using existing data."
    }
  ]
}
```

#### Notes

- **Read-only.** No external API calls. No data mutation.
- Mirrors the output of `python scripts/diagnose_window_semantics.py`.
- `diagnostic_status` values are the same canonical states used by `/api/system/status-war-room`.
- `usable_for_page` is `true` when rows exist in at least one of the requested windows, even if the latest sync failed.

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

---

## ROAS & Revenue Truth Endpoints (PR-ADS-080A)

These endpoints form the Revenue Truth Layer. Revenue source is HubSpot won deals.
Spend source is the Google Ads API. **Google Ads conversion value is NOT used.**
All route paths follow the repo convention and are mounted under `/api/...`.

---

### `GET /api/reports/roas/campaigns`

**Auth:** Auth (any authenticated session)

**Query params:**
| Param  | Type   | Default | Description |
|--------|--------|---------|-------------|
| window | string | `60d`   | Time window. Valid: `7d`, `14d`, `30d`, `60d`, `90d`, `365d` |

**Validation (400):**
- Invalid `window` values return HTTP 400 with:
  - `"detail": "Invalid window. Valid values: 7d, 14d, 30d, 60d, 90d, 365d"`

**Response (200):**
```json
{
  "window": "60d",
  "generated_at": "2026-05-27T10:00:00+00:00",
  "source_truth": "hubspot_won_deals_plus_windsor_spend",
  "google_ads_conversion_value_used": false,
  "campaigns": [
    {
      "campaign": "gulf",
      "spend": 36852.25,
      "deals_won": 6,
      "acv_revenue": 23862.80,
      "arr_revenue": 13072.80,
      "mrr_revenue": 1101.80,
      "ltv_revenue": 36393.00,
      "acv_roas": 0.65,
      "arr_roas": 0.35,
      "ltv_roas": 0.99,
      "cac": 6142.04,
      "ltv_to_cac": 0.99,
      "payback_months": 33.5,
      "true_cpl": 142.50,
      "close_rate": null,
      "attribution_confidence": "tier_2_source_tag",
      "verdict": "HOLD",
      "warnings": []
    }
  ]
}
```

**Attribution confidence values:**
- `tier_1_gclid` — Exact GCLID match to Windsor click data
- `tier_2_source_tag` — HubSpot analytics source matches campaign tag
- `tier_3_spend_weighted` — Fallback estimate (never allows SCALE verdict)

**Verdict values:**
- `SCALE` — LTV/CAC >= 3.0 and payback <= 18 months (never from tier_3)
- `HOLD` — 1.5 <= LTV/CAC < 3.0, or tier_3 attribution
- `FIX` — LTV/CAC < 1.5 or payback > 36 months
- `CUT` — LTV/CAC < 1.0
- `INSUFFICIENT_DATA` — Fewer than configured min_deals_for_verdict

---

### `GET /api/reports/roas/countries`

**Auth:** Auth (any authenticated session)

**Query params:**
| Param  | Type   | Default | Description |
|--------|--------|---------|-------------|
| window | string | `60d`   | Time window. Valid: `7d`, `14d`, `30d`, `60d`, `90d`, `365d` |

**Validation (400):**
- Invalid `window` values return HTTP 400 with:
  - `"detail": "Invalid window. Valid values: 7d, 14d, 30d, 60d, 90d, 365d"`

**Response (200):**
```json
{
  "window": "60d",
  "generated_at": "2026-05-27T10:00:00+00:00",
  "source_truth": "hubspot_won_deals_plus_windsor_spend",
  "google_ads_conversion_value_used": false,
  "country_level_estimate": true,
  "countries": [
    {
      "country": "uae",
      "spend": 12000.00,
      "deals_won": 3,
      "acv_revenue": 9000.00,
      "arr_revenue": 7200.00,
      "mrr_revenue": 600.00,
      "ltv_revenue": 20000.00,
      "acv_roas": 0.75,
      "arr_roas": 0.60,
      "ltv_roas": 1.67,
      "cac": 4000.00,
      "ltv_to_cac": 1.67,
      "payback_months": 20.0,
      "true_cpl": 4000.00,
      "close_rate": null,
      "attribution_confidence": "tier_3_spend_weighted",
      "verdict": "HOLD",
      "warnings": [],
      "country_level_estimate": true
    }
  ]
}
```

**Hard rule:** `country_level_estimate: true` on every country row until GCLID attribution is fully wired. Attribution confidence defaults to `tier_3_spend_weighted` unless tier_1 GCLID match actually exists.

---

### `GET /api/reports/unit-economics`

**Auth:** Auth (any authenticated session)

**Query params:**
| Param  | Type   | Default | Description |
|--------|--------|---------|-------------|
| window | string | `60d`   | Time window. Valid: `7d`, `14d`, `30d`, `60d`, `90d`, `365d` |

**Validation (400):**
- Invalid `window` values return HTTP 400 with:
  - `"detail": "Invalid window. Valid values: 7d, 14d, 30d, 60d, 90d, 365d"`

**Response (200):**
```json
{
  "window": "60d",
  "generated_at": "2026-05-27T10:00:00+00:00",
  "overall": {
    "ltv_to_cac": 1.4,
    "payback_months": 26.0,
    "avg_deal_acv": 4977,
    "avg_deal_mrr": 184,
    "monthly_churn_rate_used": 0.03,
    "verdict": "HOLD"
  },
  "by_campaign": []
}
```

---

## ROAS Snapshot Endpoints (PR-ADS-080C)

These endpoints return **persisted** historical ROAS snapshots. Unlike the live-compute endpoints above (`/api/reports/roas/campaigns`, `/api/reports/roas/countries`, `/api/reports/unit-economics`), snapshot endpoints serve pre-generated daily records from `data/roas_snapshots/`.

**Key difference:**
- Live endpoints: compute ROAS on demand from current data.
- Snapshot endpoints: return previously persisted daily snapshots.

Snapshot files are runtime-only (`data/` is gitignored). No external writes.

---

### `GET /api/reports/roas/snapshots/latest`

**Auth:** Auth (any authenticated session)

**Query params:**
| Param  | Type   | Default | Description |
|--------|--------|---------|-------------|
| window | string | `60d`   | Time window. Valid: `7d`, `14d`, `30d`, `60d`, `90d`, `365d` |

**Response (200):**
```json
{
  "snapshot_date": "2026-05-27",
  "generated_at": "2026-05-27T07:00:00+00:00",
  "window": "60d",
  "source_truth": "hubspot_won_deals_plus_windsor_spend",
  "google_ads_conversion_value_used": false,
  "campaigns": [],
  "countries": [],
  "unit_economics": {},
  "summary": {
    "campaign_count": 12,
    "country_count": 28,
    "scale_count": 1,
    "hold_count": 7,
    "fix_count": 3,
    "cut_count": 1,
    "insufficient_data_count": 0,
    "total_spend": 0,
    "total_acv_revenue": 0,
    "total_ltv_revenue": 0
  },
  "warnings": []
}
```

**Response (404):** No snapshot exists for the requested window.

---

### `GET /api/reports/roas/snapshots`

**Auth:** Auth (any authenticated session)

**Query params:**
| Param  | Type    | Default | Description |
|--------|---------|---------|-------------|
| window | string  | `60d`   | Time window. Valid: `7d`, `14d`, `30d`, `60d`, `90d`, `365d` |
| limit  | integer | `30`    | Max snapshots to return (1–100) |

**Response (200):**
```json
{
  "window": "60d",
  "limit": 30,
  "count": 5,
  "snapshots": []
}
```

Each entry in `snapshots` has the same shape as the latest snapshot response above.

---

### `GET /api/admin/churn-input`

**Auth:** Admin (admin role cookie or `ADMIN_API_TOKEN` token)

**Response (200):**
```json
{
  "default_monthly_churn": 0.03,
  "monthly": {
    "2026-05": 0.03
  },
  "campaign_overrides": {
    "gulf": 0.022,
    "mena": 0.035
  }
}
```

---

### `POST /api/admin/churn-input`

**Auth:** Admin (admin role cookie or `ADMIN_API_TOKEN` token)

**Request body:**
```json
{
  "month": "2026-05",
  "rate": 0.029
}
```

**Validation:**
- `month` must be `YYYY-MM` format
- `rate` must be `0 <= rate <= 1`

**Response (200) — success:**
```json
{
  "ok": true
}
```

**Response (400) — invalid rate:**
```json
{
  "detail": "rate must be between 0 and 1"
}
```

**Response (400) — invalid month:**
```json
{
  "detail": "month must be YYYY-MM format"
}
```

**Notes:**
- Local YAML config write only.
- No HubSpot write.
- No Google Ads write.

---

#### `GET /api/attribution/gclid-readiness`

GCLID Bridge Readiness Audit (PR-ADS-081). Read-only audit of whether the system is ready for click-level GCLID attribution.

**Auth:** Auth (session cookie required)

**Query parameters:**
| Param  | Type   | Default | Valid values                       |
|--------|--------|---------|-------------------------------------|
| window | string | 60d     | 7d, 14d, 30d, 60d, 90d, 365d      |

**Response (200):**
```json
{
  "window": "60d",
  "generated_at": "2026-05-27T12:00:00+00:00",
  "readiness_status": "NOT_READY",
  "readiness_score": 42,
  "summary": {
    "won_deals": 50,
    "deals_with_direct_gclid": 0,
    "deals_with_contact_gclid": 8,
    "deals_without_gclid": 42,
    "windsor_rows_with_gclid": 0,
    "tier_1_possible_matches": 0,
    "tier_2_source_tag_matches": 34,
    "tier_3_estimated_required": 16
  },
  "blockers": [
    {
      "severity": "high",
      "code": "WINDSOR_GCLID_MISSING",
      "message": "Windsor campaign data does not currently expose click-level GCLID rows."
    }
  ],
  "next_checks": [
    "Confirm whether Windsor connector can pull click-level GCLID data."
  ],
  "notes": [
    "This audit is read-only. It does not implement the GCLID bridge."
  ]
}
```

**Response (400) — invalid window:**
```json
{
  "detail": "Invalid window. Valid values: 7d, 14d, 30d, 60d, 90d, 365d"
}
```

**Readiness statuses:**
- `READY` — Tier 1 possible matches exist and both HubSpot + Windsor expose GCLID.
- `PARTIAL` — HubSpot has some GCLID coverage but Windsor/click matching is incomplete.
- `NOT_READY` — No reliable Tier 1 GCLID match path exists.
- `UNKNOWN` — Required source files are missing or empty.

**Non-goals:**
- Does not implement the GCLID bridge.
- Does not write to Google Ads, HubSpot, or any external system.
- Does not upload offline conversions.

---

#### `GET /api/attribution/confidence-summary`

Attribution Confidence Summary (PR-ADS-082). Returns confidence tier distribution across ROAS data.

**Auth:** Auth (session cookie required)

**Query parameters:**
| Param  | Type   | Default | Valid values                       |
|--------|--------|---------|-------------------------------------|
| window | string | 60d     | 7d, 14d, 30d, 60d, 90d, 365d      |

**Response (200):**
```json
{
  "window": "60d",
  "generated_at": "2026-05-27T12:00:00+00:00",
  "overall_confidence": "LOW",
  "summary": {
    "campaign_rows": 12,
    "country_rows": 28,
    "tier_1_count": 0,
    "tier_2_count": 9,
    "tier_3_count": 31,
    "tier_1_share": 0.0,
    "tier_2_share": 0.225,
    "tier_3_share": 0.775
  },
  "definitions": {
    "tier_1_gclid": {
      "label": "Exact GCLID",
      "trust_level": "exact",
      "description": "Revenue is matched to ad interaction using GCLID-level evidence."
    },
    "tier_2_source_tag": {
      "label": "Source Tag",
      "trust_level": "directional",
      "description": "Revenue is matched using HubSpot paid-search source/campaign tags."
    },
    "tier_3_spend_weighted": {
      "label": "Estimate",
      "trust_level": "estimated",
      "description": "Revenue is estimated using fallback allocation. Use directionally, not as final truth."
    }
  },
  "message": "Most ROAS rows are estimated. Use revenue pages directionally until GCLID matching is wired."
}
```

**Response (400) — invalid window:**
```json
{
  "detail": "Invalid window. Valid values: 7d, 14d, 30d, 60d, 90d, 365d"
}
```

**Confidence definitions:**
| Tier                    | Label       | Trust Level  | Description                                                |
|-------------------------|-------------|--------------|-------------------------------------------------------------|
| tier_1_gclid            | Exact GCLID | exact        | Revenue matched via GCLID-level evidence.                  |
| tier_2_source_tag       | Source Tag  | directional  | Revenue matched via HubSpot paid-search source/campaign.   |
| tier_3_spend_weighted   | Estimate    | estimated    | Revenue estimated via fallback allocation.                 |

**Overall confidence rules:**
- `HIGH` — tier_1_share >= 70%
- `MEDIUM` — tier_1_share + tier_2_share >= 70%
- `LOW` — tier_3_share > 50%
- `UNKNOWN` — no rows available

**Non-goals:**
- Does not rewrite ROAS calculations.
- Does not write to Google Ads, HubSpot, or any external system.
- Does not upload offline conversions.
