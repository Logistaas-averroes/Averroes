# Phase 1.5 UI War Room — Repository Audit
## Logistaas Ads Intelligence System

**Audit date:** 2026-05-02
**Auditor:** Copilot agent — repo read-only inspection
**Branch audited:** `copilot/audit-repo-before-ui-upgrade`
**Doctrine:** Averroes v1.0 — Phase 1.5 Planning / Audit
**Mode:** AUDIT ONLY — no code was written, no endpoints created, no UI modified

---

## 1. Executive Verdict

The system has a working backend with six live DB-backed API endpoints, cookie-based auth, a 5-page SPA, and a deterministic report pipeline. However, **the UI is an operator's status panel, not an intelligence war room.** The following critical gaps prevent it from being the consultant-grade tool described in the Phase 4 vision:

| Gap | Severity |
|-----|----------|
| No Waste/Search-Terms page — endpoint `/api/waste` exists but no page renders it | 🔴 High |
| No Geo/Country intelligence page — data is fetched by Windsor but never stored in DB | 🔴 High |
| No Keywords page — keyword data fetched but not stored in DB or shown in UI | 🔴 High |
| Opportunities page is misleading — shows `in_progress` CRM contacts, not pipeline deals | 🟡 Medium |
| Time range buttons exist but offer only 4 preset values, no comparison period | 🟡 Medium |
| No AI advisor chat endpoint or UI — `POST /api/chat` does not exist | 🟡 Medium |
| Campaign table Leads column is hardcoded `—` (no lead count shown per campaign) | 🟡 Medium |
| No data freshness badge anywhere in UI | 🟡 Medium |
| No drill-down on any row, card, or map | 🟡 Medium |
| No n-gram, match-type, or negative candidate analysis | 🟠 Lower (Phase 1.5) |

**Phase 1 read-only compliance: ✅ fully respected.** No write endpoints to Google Ads or HubSpot exist.

---

## 2. Current Codebase Inventory

### 2.1 Frontend Inventory

#### File list

| File | Size | PR |
|------|------|----|
| `static/index.html` | 325 lines | PR-ADS-023, PR-ADS-025B |
| `static/app.js` | 1,019 lines | PR-ADS-025B (full rewrite) |
| `static/styles.css` | ~900 lines (est.) | PR-ADS-023 |

#### Sidebar items and pages

| Sidebar label | `data-page` value | Page section ID | Notes |
|---|---|---|---|
| Dashboard | `dashboard` | `#page-dashboard` | Default on login |
| Campaigns | `campaigns` | `#page-campaigns` | Campaign truth table |
| Lead Quality | `leads` | `#page-leads` | Per-campaign MQL breakdown |
| Deals | `deals` | `#page-deals` | GCLID-matched deals pipeline |
| Opportunities | `opportunities` | `#page-opportunities` | In-progress leads — **mislabelled** |
| Scheduler | `scheduler` | `#page-scheduler` | APScheduler status + manual triggers |
| System Health | `health` | `#page-health` | Admin only — readiness checks |

The Login screen is a separate `#login-screen` div shown before auth succeeds.

#### Time-range selector

A `#time-range-bar` div renders above all data pages with four preset buttons: **7d, 14d, 30d (default), 60d**. The selection is stored in `sessionStorage`. Changing the value calls `loadPage()` with the new `?days=` param. No custom date range. No comparison period.

---

#### Page-by-page detail

**Dashboard (`#page-dashboard`)**

| Property | Value |
|----------|-------|
| Route/path | Default on login |
| File | `static/index.html:159–203`, `static/app.js:298–449` |
| API calls | `GET /api/summary?days=N`, `GET /api/campaigns?days=N`, `GET /runs/latest` |
| Visible widgets | 4 KPI cards (Spend, SQLs, Avg CPQL, Confirmed Waste), Campaign Verdict Summary bar chart, Active Alerts panel, Recent Run panel |
| Filters | Time range (4 presets) — no campaign filter, no country filter |
| Time range | ✅ Yes (4 presets) |
| Drill-down | ❌ No — campaign verdict bars are not clickable |
| Loading states | ✅ Yes (`Loading…` placeholders) |
| Error states | ✅ Yes (generic catch message) |
| Empty states | ✅ Yes ("No campaign data yet. Trigger a weekly run to populate.") |
| Data freshness badge | ❌ Missing |
| Useful or cosmetic | ✅ Useful when DB has data — shows correct spend, SQLs, alerts, recent run |
| Known issues | Trend is hardcoded to `"stable"` for all campaigns (comment in code); Leads column shows `—` in campaign truth table |

---

**Campaigns (`#page-campaigns`)**

| Property | Value |
|----------|-------|
| Route/path | `data-page="campaigns"` |
| File | `static/index.html:206–235`, `static/app.js:453–542` |
| API calls | `GET /api/campaigns?days=N` |
| Visible widgets | 4 verdict-count cards (SCALE / FIX / HOLD / CUT), Campaign Truth Table |
| Table columns | Campaign, Spend (avg/run), Leads, SQLs, Junk %, CPQL, Verdict, Runs |
| Filters | Time range only — no campaign search, no country filter |
| Time range | ✅ Yes |
| Drill-down | ❌ No — rows are not clickable |
| Loading states | ✅ Yes |
| Error states | ✅ Yes |
| Empty states | ✅ Yes |
| Data freshness badge | ❌ Missing |
| Useful or cosmetic | ✅ Useful — shows real DB-backed verdicts |
| Known issues | **Leads column always renders `—`** — `total_confirmed_sqls` is summed in the DB query but `total_leads` is not included in `/api/campaigns` response shape; no way to show leads/campaign from this endpoint alone |

---

**Lead Quality (`#page-leads`)**

| Property | Value |
|----------|-------|
| Route/path | `data-page="leads"` |
| File | `static/index.html:238–271`, `static/app.js:546–664` |
| API calls | `GET /api/leads?days=N` (returns raw rows, deduplicated by `contact_id` in JS) |
| Visible widgets | 4 KPI cards (Total Contacts, Confirmed SQLs, Confirmed Junk, In Progress), Per-Campaign Breakdown table with junk rate progress bar |
| Table columns | Campaign, Total, SQL, In Progress, Junk, Wrong Fit, Unknown, Junk Rate |
| Filters | Time range only — no campaign filter, no country filter, no keyword filter |
| Time range | ✅ Yes |
| Drill-down | ❌ No — rows are not clickable |
| Loading states | ✅ Yes |
| Error states | ✅ Yes |
| Empty states | ✅ Yes |
| Grace note | ✅ "Contacts created in the last 7 days are excluded from junk rate" |
| Data freshness badge | ❌ Missing |
| Deduplication | ✅ Client-side dedup by `contact_id`, taking most-recent `run_date` per contact |
| Useful or cosmetic | ✅ Useful — aggregation is correct; junk rate coloring matches thresholds |
| Known issues | Country column available in DB (`leads.country`) but never shown; keyword column available but never shown; 1000-row DB limit may truncate large windows |

---

**Deals (`#page-deals`)**

| Property | Value |
|----------|-------|
| Route/path | `data-page="deals"` |
| File | `static/index.html:274–289`, `static/app.js:668–744` |
| API calls | `GET /api/deals?days=N` |
| Visible widgets | Pipeline funnel (horizontal bar per stage), Deals table |
| Table columns | Company, Country, Stage, Amount, Campaign, Keyword |
| Filters | Time range only |
| Time range | ✅ Yes |
| Drill-down | ❌ No |
| Loading states | ✅ Yes |
| Error states | ✅ Silently falls back (empty state already set before try block) |
| Empty states | ✅ "No GCLID-matched deals found yet. Deals appear here once HubSpot deal attribution is active." |
| Data freshness badge | ❌ Missing |
| Useful or cosmetic | ⚠️ **Cosmetic until deals DB is populated** — data only appears after a weekly run that has GCLID-matched deals |
| Known issues | Pipeline funnel stage matching is string-contains logic in JS (`"Won"` in `deal_stage`) — fragile if HubSpot stage labels change; `deal_stage_label` is preferred but `deal_stage` raw value is the fallback |

---

**Opportunities (`#page-opportunities`)**

| Property | Value |
|----------|-------|
| Route/path | `data-page="opportunities"` |
| File | `static/index.html:292–298`, `static/app.js:748–814` |
| API calls | `GET /api/leads?days=N` |
| Data shown | Filters `leads` rows to `status_category === "in_progress"` — groups by `mql_status` into "Meeting Booked", "Pending Meeting", "Connecting" |
| Filters | Time range only |
| Time range | ✅ Yes |
| Drill-down | ❌ No |
| Loading states | ✅ Yes |
| Error states | ✅ Yes |
| Empty states | ✅ "No active opportunities in the selected window." |
| Data freshness badge | ❌ Missing |
| Useful or cosmetic | ⚠️ **Mislabelled and misleading** |

> **Critical UX issue:** The page is named "Opportunities" and displays contacts in progress — these are CRM contacts in meeting-booked or pending-meeting status, **not sales pipeline opportunities**. The sidebar icon reinforces a "target/deal" metaphor. A viewer expecting deal pipeline will be confused. Additionally, cards show `contact_id` ("ID: 750636...") as the primary identifier rather than company name or contact name — making the page nearly unreadable for business users. Company name is not in the `leads` table and cannot be shown without a join to the `deals` table or a schema change.

---

**Scheduler (`#page-scheduler`)**

| Property | Value |
|----------|-------|
| Route/path | `data-page="scheduler"` |
| File | `static/index.html:301–308`, `static/app.js:818–919` |
| API calls | `GET /scheduler/status`, `POST /run/{daily|weekly|monthly}` (admin only) |
| Visible widgets | Scheduler job cards (schedule, next run time), Run trigger buttons (admin), Run feedback panel |
| Filters | None |
| Time range | ❌ Not applicable |
| Drill-down | ❌ No |
| Useful or cosmetic | ✅ Useful for admin/ops |
| Role restriction | Trigger buttons visible only to `admin` role |

---

**System Health (`#page-health`)**

| Property | Value |
|----------|-------|
| Route/path | `data-page="health"` — admin only |
| File | `static/index.html:311–317`, `static/app.js:922–977` |
| API calls | `GET /readiness` (admin only) |
| Visible widgets | Overall status badge, Config Files group, Directories group, Documentation group, Module Imports group |
| Useful or cosmetic | ✅ Useful for ops verification |
| Role restriction | Nav item hidden from viewer/mdr roles; `/readiness` returns 403 if not admin |

---

### 2.2 CSS / Visual

`static/styles.css` is ~900 lines using CSS custom properties (tokens). It includes: brand tokens (sky blue + midnight), verdict badge colors (SCALE=green, FIX=amber, HOLD=neutral, CUT=red), junk rate coloring (low/mid/high), login screen styles, sidebar layout, KPI cards, panel blocks, data tables, funnel, opportunity cards, scheduler cards, health rows, time-range bar, progress bars, status pills, run feedback. No third-party CSS library. Uses Google Fonts (Sora). No dark mode.

---

## 3. API Inventory

All endpoints are in `api/server.py`. Source-of-truth doc: `docs/API_CONTRACT.md`.

### Complete endpoint table

| Method | Path | Auth | File / function | Data source | Date range? | Campaign filter? | Country filter? | Keyword filter? | Used by frontend? |
|--------|------|------|----------------|-------------|-------------|-----------------|-----------------|-----------------|------------------|
| GET | `/health` | Public | `server.py:health()` | None (process check) | — | — | — | — | ✅ (sidebar health dot) |
| GET | `/` | Public | `server.py:dashboard()` | `static/index.html` | — | — | — | — | ✅ (serves SPA shell) |
| POST | `/auth/login` | Public | `server.py:auth_login()` | `AUTH_USERS_JSON` env | — | — | — | — | ✅ |
| POST | `/auth/logout` | Public | `server.py:auth_logout()` | Cookie | — | — | — | — | ✅ |
| GET | `/auth/me` | Auth | `server.py:auth_me()` | Cookie | — | — | — | — | ✅ |
| GET | `/readiness` | Admin | `server.py:readiness()` | File system + imports | — | — | — | — | ✅ (health page) |
| GET | `/runs/latest` | Auth | `server.py:runs_latest()` | `runtime_logs/run_history.jsonl` | ❌ (latest only) | ❌ | ❌ | ❌ | ✅ (dashboard recent run) |
| GET | `/reports/latest` | Auth | `server.py:reports_latest()` | `outputs/*.md` | ❌ (latest only) | ❌ | ❌ | ❌ | ❌ (fetched but not rendered) |
| GET | `/reports/latest/raw` | Auth | `server.py:reports_latest_raw()` | `outputs/*.md` | ❌ (latest only) | ❌ | ❌ | ❌ | ❌ (available but no page shows it) |
| GET | `/scheduler/status` | Auth | `server.py:scheduler_status()` | In-memory APScheduler | — | — | — | — | ✅ (scheduler page) |
| POST | `/run/daily` | Admin | `server.py:run_daily()` | Triggers `scheduler/daily.py` | — | — | — | — | ✅ (scheduler page) |
| POST | `/run/weekly` | Admin | `server.py:run_weekly()` | Triggers `scheduler/weekly.py` | — | — | — | — | ✅ (scheduler page) |
| POST | `/run/monthly` | Admin | `server.py:run_monthly()` | Triggers `scheduler/monthly.py` | — | — | — | — | ✅ (scheduler page) |
| GET | `/api/campaigns` | Auth | `server.py:api_campaigns()` | PostgreSQL `campaigns` table | ✅ `?days=` | ❌ | ❌ | ❌ | ✅ (campaigns + dashboard) |
| GET | `/api/leads` | Auth | `server.py:api_leads()` | PostgreSQL `leads` table | ✅ `?days=` | ❌ | ❌ | ❌ | ✅ (leads + opportunities) |
| GET | `/api/deals` | Auth | `server.py:api_deals()` | PostgreSQL `deals` table | ✅ `?days=` | ❌ | ❌ | ❌ | ✅ (deals page) |
| GET | `/api/waste` | Auth | `server.py:api_waste()` | PostgreSQL `waste_terms` table | ✅ `?days=` | ❌ | ❌ | ❌ | ❌ **Not used by any page** |
| GET | `/api/runs` | Auth | `server.py:api_runs()` | PostgreSQL `runs` table | ✅ `?days=` | ❌ | ❌ | ❌ | ❌ **Not used by any page** |
| GET | `/api/summary` | Auth | `server.py:api_summary()` | PostgreSQL `campaigns` + `waste_terms` + `runs` | ✅ `?days=` | ❌ | ❌ | ❌ | ✅ (dashboard KPIs) |

### Endpoints present in API but with no UI page

- `GET /api/waste?days=N` — returns waste terms sorted by spend. Full data available. **No UI page exists.**
- `GET /api/runs?days=N` — returns run history records. **No UI page exists** (dashboard only shows latest single run from `/runs/latest`).
- `GET /reports/latest/raw` — returns markdown report text. **No page renders it.**

### Endpoints that do not exist but are needed for Phase 1.5

- `POST /api/chat` — AI advisor chat (not present)
- `GET /api/geo?days=N` — geo/country intelligence (not present; no DB table for geo data)
- `GET /api/keywords?days=N` — keyword-level data (not present; no DB table for keywords)
- `GET /api/search-terms?days=N` — granular search-term rows (not present; only aggregated waste terms exist)
- `GET /api/leads/country-summary?days=N` — country-level lead counts (not present; derivable from `leads` table)

---

## 4. Data Availability Inventory

### 4.1 PostgreSQL Tables

**`runs` table**

| Field | Type | Notes |
|-------|------|-------|
| id | SERIAL PK | Auto-increment run ID |
| run_type | VARCHAR(20) | "daily", "weekly", "monthly" |
| started_at | TIMESTAMPTZ | ✅ |
| finished_at | TIMESTAMPTZ | ✅ |
| status | VARCHAR(20) | "success", "failed" |
| failed_step | TEXT | Step that failed |
| error_message | TEXT | Error detail |
| report_path | TEXT | Output file path |
| delivery_attempted | BOOLEAN | Email delivery attempted? |
| delivery_success | BOOLEAN | Email delivery succeeded? |

Date field: ✅ (`started_at`)  
Campaign field: ❌  
Country field: ❌  
Keyword field: ❌  
Spend/click/conversion fields: ❌  
SQL/junk/MQL fields: ❌

---

**`campaigns` table**

| Field | Type | Notes |
|-------|------|-------|
| id | SERIAL PK | |
| run_id | INTEGER FK → runs | |
| run_date | DATE | ✅ |
| campaign_name | TEXT | ✅ Normalised to lowercase + canonical map |
| spend_usd | NUMERIC(10,2) | ✅ |
| clicks | INTEGER | ✅ |
| impressions | INTEGER | ✅ |
| conversions | NUMERIC(8,2) | ✅ (Google Ads conversions only — not business KPI) |
| total_leads | INTEGER | ✅ |
| confirmed_sqls | INTEGER | ✅ |
| junk_count | INTEGER | ✅ |
| junk_rate_pct | NUMERIC(5,2) | ✅ |
| cpql_usd | NUMERIC(10,2) | ✅ |
| verdict | VARCHAR(10) | SCALE/FIX/HOLD/CUT |
| verdict_reason | TEXT | Human-readable reason |
| created_at | TIMESTAMPTZ | ✅ |

Date field: ✅ (`run_date`)  
Campaign field: ✅  
Country field: ❌  
Keyword field: ❌  
Search term field: ❌  
Spend/click/conversion: ✅  
SQL/junk/MQL: ✅  

**Missing from campaigns table:** geographic breakdown, keyword breakdown, match-type breakdown, brand vs. non-brand split, comparison-to-previous-period columns.

---

**`leads` table**

| Field | Type | Notes |
|-------|------|-------|
| id | SERIAL PK | |
| run_id | INTEGER FK → runs | |
| run_date | DATE | ✅ |
| contact_id | TEXT | ✅ Nullable |
| campaign_name | TEXT | ✅ Normalised |
| keyword | TEXT | ✅ (from `hs_analytics_source_data_2`) |
| country | TEXT | ✅ (from `ip_country`) |
| mql_status | TEXT | ✅ (exact HubSpot value including `DICARDED`) |
| status_category | VARCHAR(20) | ✅ qualified/in_progress/junk/wrong_fit/unknown |
| gclid | TEXT | ✅ |
| source_type | VARCHAR(30) | ✅ paid_search/organic_search/referral/direct/email/other |
| created_at | TIMESTAMPTZ | ✅ |

Date field: ✅  
Campaign field: ✅  
Country field: ✅  
Keyword field: ✅  
Search term field: ❌ (keyword bid on is stored, but actual search term typed by user is not)  
Spend/click/conversion: ❌  
SQL/junk/MQL: ✅  

**Key gap:** No `company` field in `leads`. Company name is only in `deals`. Opportunities page shows `contact_id` instead of company name because of this. No `mql___mdr_comments` field stored.

---

**`waste_terms` table**

| Field | Type | Notes |
|-------|------|-------|
| id | SERIAL PK | |
| run_id | INTEGER FK → runs | |
| run_date | DATE | ✅ |
| search_term | TEXT | ✅ |
| campaign_name | TEXT | ✅ |
| spend_usd | NUMERIC(10,2) | ✅ |
| junk_category | TEXT | ✅ (job_seeker, student, free_intent_english, etc.) |
| matched_pattern | TEXT | ✅ |
| crm_junk_confirmed | INTEGER | ✅ (count of HubSpot junk contacts with this keyword) |
| created_at | TIMESTAMPTZ | ✅ |

Date field: ✅  
Campaign field: ✅  
Country field: ❌  
Keyword field: ✅ (search_term is the actual user query)  
Search term field: ✅  
Spend: ✅  
SQL/junk/MQL: ✅ (via `crm_junk_confirmed`)  

**Key gap:** No match_type. No matched_keyword (which bid keyword triggered this search term). No impression share. No `clicks` field at search term level — only spend.

---

**`deals` table**

| Field | Type | Notes |
|-------|------|-------|
| id | SERIAL PK | |
| run_id | INTEGER FK → runs | |
| run_date | DATE | ✅ |
| contact_id | TEXT | ✅ |
| company | TEXT | ✅ |
| country | TEXT | ✅ |
| keyword | TEXT | ✅ |
| campaign_name | TEXT | ✅ |
| deal_stage | TEXT | ✅ (raw HubSpot stage ID) |
| deal_stage_label | TEXT | ✅ (human-readable) |
| deal_amount_usd | NUMERIC(12,2) | ✅ |
| mql_status | TEXT | ✅ |
| gclid | TEXT | ✅ |
| created_at | TIMESTAMPTZ | ✅ |

Date field: ✅  
Campaign field: ✅  
Country field: ✅  
Keyword field: ✅  
Spend: ❌  
SQL/junk/MQL: ✅  

---

**`migrations` table**

Tracks one-time idempotent DDL operations. Contains `migration_id` (VARCHAR PK) and `applied_at`. Not surfaced in UI. Used by `db/schema.py` for safe one-time cleanup operations.

---

### 4.2 Data Not Stored in Database (Connector output only — `data/*.json`, gitignored)

| Data type | Connector function | Output file | Stored in DB? | Available for queries? |
|-----------|-------------------|-------------|----------------|------------------------|
| Campaign performance (30d) | `windsor_pull.pull_campaign_performance()` | `data/ads_campaigns.json` | ✅ (via `write_campaigns`) | ✅ |
| Search terms (14d preset) | `windsor_pull.pull_search_terms()` | `data/ads_search_terms.json` | ✅ (via `write_waste_terms`, filtered to junk only) | ⚠️ Partial — non-junk terms not stored |
| Keyword performance (30d) | `windsor_pull.pull_keyword_performance()` | `data/ads_keywords.json` | ❌ No DB table | ❌ Cannot query |
| **Geo performance (30d)** | `windsor_pull.pull_geo_performance()` | `data/ads_geos.json` | ❌ **No DB table** | ❌ **Cannot query** |
| HubSpot contacts (30d) | `hubspot_pull.pull_paid_search_contacts()` | `data/crm_contacts.json` | ✅ (via `write_leads`) | ✅ |
| GCLID-matched deals | `hubspot_pull.pull_deals_with_gclid()` | `data/crm_deals.json` | ✅ (via `write_deals`) | ✅ |
| CRM summary | `hubspot_pull.get_lead_quality_summary()` | `data/crm_summary.json` | ❌ No table | ❌ Cannot query |
| GCLID match coverage | `gclid_match.py` | `data/matched_gclid.json`, `data/gclid_coverage.json` | ❌ No table | ❌ Cannot query |
| Non-junk search terms | (filtered out before `write_waste_terms`) | ❌ Not saved | ❌ | ❌ |

---

### 4.3 Run history (JSONL)

`runtime_logs/run_history.jsonl` — one JSON object per line per run. Read by `GET /runs/latest` (reads last non-empty line). Contains the same fields as the `runs` table. Not queryable by date range from the API — `/runs/latest` only returns the single last entry. Multi-run history requires `GET /api/runs?days=N` (DB-backed).

---

### 4.4 Config data

| File | Fields | Used in analysis? | Used in UI? |
|------|--------|-------------------|-------------|
| `config/thresholds.yaml` | Verdict thresholds, junk rate rules, CPQL, GCLID coverage | ✅ (`analysis/core.py`, `analysis/rule_advisor.py`) | ❌ Not exposed via API |
| `config/junk_patterns.yaml` | 10 junk categories, safe-terms whitelist | ✅ (`analysis/core.py`) | ❌ Not exposed via API |

Thresholds are not exposed through any API endpoint. The UI hardcodes `JUNK_RATE_LOW_THRESHOLD = 15` and `JUNK_RATE_HIGH_THRESHOLD = 30` directly in `app.js:23-24`. These match `config/thresholds.yaml` but are **duplicated** in frontend JavaScript — a maintenance risk.

---

## 5. Missing Pages

The following pages do not exist in the current SPA and are not backed by any existing route:

| Missing page | Data available? | API endpoint available? | DB table ready? | Gap |
|---|---|---|---|---|
| Waste / Search Terms | ✅ Connector + DB | ✅ `GET /api/waste` | ✅ `waste_terms` | No UI page at all |
| Geo / Country Intelligence | ⚠️ JSON file only | ❌ | ❌ No DB table | No DB table, no API, no UI |
| Keywords | ⚠️ JSON file only | ❌ | ❌ No DB table | No DB table, no API, no UI |
| Run History (multi-run) | ✅ DB | ✅ `GET /api/runs` | ✅ `runs` | Endpoint unused by frontend |
| AI Advisor Chat | ❌ No endpoint | ❌ No `POST /api/chat` | N/A | Not built |
| Report Viewer | ✅ Report files | ✅ `GET /reports/latest/raw` | N/A | Endpoint exists, no page renders it |
| N-gram Analysis | ❌ | ❌ | ❌ | Not in any layer |
| Negative Keyword Candidates | ❌ | ❌ | ❌ | Phase 3 — not yet built |

---

## 6. Weak Existing Pages

| Page | Issue |
|------|-------|
| **Opportunities** | Shows `in_progress` CRM contacts, not deals. Displays `contact_id` as the primary identifier — unusable for business review. No company name. Misleading sidebar name and icon. Should either be renamed "In Progress Leads" or replaced with a real deal pipeline view. |
| **Deals** | Cosmetic until deals DB is populated. Pipeline funnel uses fragile string-match against `deal_stage` values. No spend context per deal (to show CPQL per won deal). No GCLID attribution quality signal. |
| **Dashboard** | Campaign Verdict Summary bars are not clickable. Alerts panel shows only FIX/CUT campaigns, no severity ranking. Recent Run panel shows only the last run — no history trend. |
| **Campaigns** | Leads column always shows `—`. `trend` field hardcoded to `"stable"`. No drill-down to see individual leads per campaign. No keyword breakdown per campaign. |
| **Lead Quality** | Country column in `leads` DB is never surfaced. Keyword column in `leads` DB is never surfaced. No ability to filter by junk category. No export. |

---

## 7. API and Data Gaps

### 7.1 Time range readiness

| Capability | Status |
|---|---|
| Last 7 days | ✅ All 6 `/api/*` endpoints support `?days=7` |
| Last 14 days | ✅ |
| Last 30 days | ✅ (default) |
| Last 60 days | ✅ |
| Last 90 days | ✅ (UI buttons stop at 60d but `?days=90` works in the API) |
| Custom date range | ❌ API supports only `?days=N` (integer lookback). No `date_from`/`date_to` params exist. |
| Compare to previous period | ❌ Not possible without a second API call or a new endpoint shape |

**What needs to be added for comparison:** Either a `?compare=true` query param that returns two periods in one response, or two separate frontend calls with different `?days=` values plus client-side delta calculation. The DB supports this (date-indexed tables) — it is an API + frontend-only change.

**What cannot be done without connector changes:** Windsor search-term data is capped at `last_14d` preset due to a Windsor API limitation (`pull_search_terms` maps all `days_back > 7` to `last_14d`). Longer windows for waste analysis require Windsor plan verification.

---

### 7.2 Geo intelligence readiness

| Capability | Ready? | Gap |
|---|---|---|
| Windsor geo data pull | ✅ `pull_geo_performance()` exists — fields: date, campaign, country, spend, clicks, impressions, conversions | ✅ Already fetched |
| Geo data in DB | ❌ **No `geo` table in DB schema** | Connector writes to `data/ads_geos.json` only — never inserted to DB |
| `GET /api/geo` endpoint | ❌ Does not exist | Not in `server.py` |
| Country-level lead counts | ⚠️ **Derivable** — `leads.country` field exists | Needs a new `/api/leads/country-summary` endpoint or a GROUP BY country query |
| World map by lead volume | ❌ No endpoint, no page | Needs: DB geo table + `/api/geo` endpoint + Leaflet/D3 map component |
| World map by SQL volume | ❌ | `leads` table has `country` + `status_category` — can query, but no UI page |
| World map by junk volume | ❌ | Same — data is there but needs UI |
| Country hover (leads/SQL/junk/rate/top campaign/keyword) | ❌ | Derivable from `leads` table with GROUP BY country — needs API + map UI |
| Country drill-down | ❌ | Needs a filtered `/api/leads?country=XX` endpoint param + modal/drawer |
| Confirmed CUT market (Venezuela) | ✅ In `config/thresholds.yaml` | Not surfaced in UI anywhere |

**Summary:** Geo data is fetched every weekly run but immediately discarded after writing to a gitignored JSON file. Adding a `geo` DB table and a writer call in `scheduler/weekly.py` is a Phase 1.5 backend prerequisite for any geo intelligence page.

---

### 7.3 Keyword and search-term readiness

| Capability | Ready? | Gap |
|---|---|---|
| Windsor keyword data pull | ✅ `pull_keyword_performance()` — fields: date, campaign, ad_group, keyword, match_type, quality_score, spend, clicks, impressions, conversions, cpc | ✅ Fetched |
| Keyword data in DB | ❌ **No `keywords` table** | Written to `data/ads_keywords.json` only |
| Windsor search term data | ✅ `pull_search_terms()` — fields: date, search_term, campaign, campaign_id, ad_group, keyword, match_type, spend, clicks, impressions, conversions | ✅ Fetched |
| Non-junk search terms in DB | ❌ Only junk-flagged terms stored in `waste_terms` | 95%+ of search terms discarded — not queryable |
| Keywords page | ❌ No endpoint, no UI page | |
| Search Terms / Waste page | ⚠️ Data in `waste_terms` DB, `GET /api/waste` exists | **No UI page renders it** |
| N-gram analysis | ❌ No code exists | Not in any layer |
| Negative keyword candidates | ❌ | Phase 3 (`connectors/negative_pusher.py`) |
| Match-type analysis | ❌ | `match_type` field pulled from Windsor but never stored in DB |
| Broad-match risk alerts | ❌ | Needs match_type in DB |
| Search-term → keyword mapping | ❌ | `keyword` field in search terms data is pulled (`matched_keyword`) but not stored in `waste_terms` table |
| High-spend-zero-SQL flags | ⚠️ Partial | `/api/campaigns` can show spend with 0 SQLs — but no dedicated alert |
| Junk language classification | ✅ `config/junk_patterns.yaml` — 10 categories | ✅ Categories stored in `waste_terms.junk_category` |

**Summary:** The full search-terms dataset exists at connector level but only the junk-flagged subset reaches the DB. A dedicated `search_terms` table (or expanding `waste_terms` to store all terms) is required for keywords page, match-type analysis, and n-gram work.

---

## 8. UI/UX Debt

The following capabilities are absent from the current UI. Each item is a concrete absence, not a subjective preference:

| UX gap | Severity | Affected pages |
|--------|----------|---------------|
| No global time range — each page has presets but no custom date picker | 🔴 | All |
| No comparison period (e.g., "vs. previous 30 days") | 🔴 | All |
| No data freshness badge — user cannot see when data was last updated | 🔴 | All |
| No drill-down — no row, card, or bar is clickable | 🔴 | Campaigns, Leads, Dashboard |
| `GET /api/waste` endpoint exists but zero UI pages render it | 🔴 | (Missing page) |
| Opportunities page shows contact IDs, not company names | 🔴 | Opportunities |
| Campaign table Leads column hardcoded to `—` | 🟡 | Campaigns |
| `trend` field hardcoded to `"stable"` for all campaigns | 🟡 | Campaigns, Dashboard |
| No empty-state explanation that is actionable (most say "trigger a run") | 🟡 | All |
| No alerts priority / severity ranking | 🟡 | Dashboard |
| No action queue ("3 campaigns need review this week") | 🟡 | Dashboard |
| No hover tooltips / contextual definitions | 🟡 | All |
| No investigation drawer or slide-over panel | 🟡 | All |
| No page-level summary ("This week: 8 SQLs, $6k spend, 3 alerts") | 🟡 | All |
| No source-of-truth badges ("from HubSpot", "from Windsor") | 🟡 | All |
| No "why this matters" explanations for verdicts or junk rates | 🟡 | Campaigns, Leads |
| No export / copy workflow (CSV download, clipboard) | 🟠 | Campaigns, Leads, Waste |
| Junk rate thresholds (15%/30%) duplicated in `app.js` — not read from API | 🟠 | Campaigns, Leads |
| No Waste / Search Terms page at all | 🔴 | (Missing page) |
| No Geo / Country map or table | 🔴 | (Missing page) |
| No Keywords performance page | 🔴 | (Missing page) |
| No report viewer (markdown report exists, endpoint exists, no UI) | 🟠 | (Missing page) |
| No AI advisor chat panel | 🟠 | (Planned Phase 4) |
| No run history timeline (only most recent run shown) | 🟠 | Dashboard |
| Login screen has no "forgot password" or error codes — only generic message | 🟠 | Login |

---

## 9. AI Advisor Readiness

| Question | Answer |
|----------|--------|
| Where does advisor code live? | `analysis/advisor.py` — dispatch layer. `analysis/rule_advisor.py` — deterministic generator. Both in `analysis/` package. |
| Can it be called from UI? | ❌ No. There is no `POST /api/chat` endpoint. The advisor is only called by scheduler jobs (`scheduler/weekly.py`, `scheduler/monthly.py`). |
| Does it have access to current data? | ⚠️ Partially. It reads `outputs/waste_report.json`, `outputs/lead_quality.json`, `outputs/campaign_truth.json` from disk. These are the most-recently-generated analysis outputs — not live DB queries. They are stale between runs. |
| Can it answer page-specific questions? | ❌ No. It generates a weekly/monthly summary report. No mechanism exists for ad-hoc or page-specific questions. |
| Does `POST /api/chat` exist? | ❌ **Does not exist in `api/server.py`.** |
| What context would need to be injected per page? | Dashboard → summary metrics + campaign verdicts; Campaigns → campaign truth table; Leads → per-campaign MQL breakdown; Waste → waste terms + junk categories; Geo → country-level lead/junk counts; per-page data would need to be fetched from DB and injected into the prompt at request time. |
| ADVISOR_MODE options | `deterministic` (default, no external API, uses `rule_advisor.py`) or `claude` (requires `ANTHROPIC_API_KEY` env var) |
| Security / token risks if chat is added | Token exhaustion risk if user can submit arbitrary long context; prompt injection risk if DB data is injected unsanitized; no rate limiting on `POST /api/chat` would be needed; `ANTHROPIC_API_KEY` must not be exposed in any API response. |
| Recommendation for Phase 1.5 | **Disable chat until a later PR.** Phase 1.5 should establish the data layer first (geo table, keywords table, waste page, drill-down). Adding chat before the data is clean and DB-backed would mean the advisor answers questions with stale JSON files. |

---

## 10. Architecture Risks

| Risk | Location | Severity | Notes |
|------|----------|----------|-------|
| Junk rate thresholds hardcoded in frontend | `static/app.js:23-24` (`JUNK_RATE_LOW_THRESHOLD = 15`, `JUNK_RATE_HIGH_THRESHOLD = 30`) | 🟡 Medium | Duplicates `config/thresholds.yaml`. If thresholds change in YAML, UI will not reflect them without a JS update. Should be served via a `GET /api/config/thresholds` endpoint. |
| `trend` field hardcoded to `"stable"` | `api/server.py:575` + `api/server.py:API contract` | 🟡 Medium | Comment in server.py acknowledges this — "TODO: Replace hardcoded 'stable'". Will require 4+ runs to compute meaningfully. |
| Geo data fetched but never written to DB | `scheduler/weekly.py:47-48`, `scheduler/monthly.py` | 🔴 High | `geos` variable is passed to `windsor_save()` which writes a gitignored JSON file. It is never passed to `db_writers`. Any geo intelligence page will fail without fixing this. |
| Keyword data fetched but never written to DB | Same schedulers | 🔴 High | Same pattern — written to `data/ads_keywords.json` only. |
| Non-junk search terms discarded pre-DB write | `analysis/core.py` → `scheduler/weekly.py:write_waste_terms()` | 🟡 Medium | Only terms matching junk patterns are stored. Full search term data (needed for n-gram, match-type analysis) is never persisted. |
| `deals` pipeline funnel uses string-contains JS match | `static/app.js:688-691` | 🟠 Low | Matches stage labels with `.toLowerCase().includes(s.toLowerCase())` — fragile if `deal_stage_label` from HubSpot changes. Should use `deal_stage` (raw ID) with an authoritative map. |
| Company name absent from `leads` table | `db/schema.py` | 🟡 Medium | Opportunities page shows `contact_id` instead of company. Fixing requires adding `company` field to `leads` schema + `write_leads()` + `hubspot_pull.py` properties list. `company` is already in `CONTACT_PROPERTIES` in `hubspot_pull.py` (line 42) — it just is not written to DB. |
| `OPEN - Connecting` (status unknown) currently counts toward lead totals on Lead Quality page | `static/app.js:583-610` | 🟠 Low | The JS aggregation counts `unknown` contacts — these are unverdicted. This is not wrong per doctrine but can inflate "total" and create a misleading junk rate if viewed per-campaign. Documented in grace note but not explained per-campaign. |
| Advisor reads from disk files, not DB | `analysis/advisor.py:108-111`, `analysis/rule_advisor.py:465-467` | 🟡 Medium | Report quality is capped at the last written JSON files. If DB data is more current, advisor does not see it. For a live chat feature, this would be a data freshness problem. |
| `docs/DOCTRINE.md` checked at readiness | `api/server.py:113` | 🟠 Low | `/readiness` endpoint verifies `docs/DOCTRINE.md` exists. File exists (`/home/runner/work/Averroes/Averroes/docs/DOCTRINE.md`). |
| No `__init__.py` in `scheduler/` or `analysis/` | `docs/09_REPO_STATE.md` | 🟠 Low | Works with `python -m scheduler.weekly` but may fail in some import contexts. |
| Phase 1 write-back risk | — | ✅ None detected | No endpoints write to Google Ads or HubSpot. `api/server.py` docstring explicitly forbids this. `docs/API_CONTRACT.md` lists forbidden endpoints. |
| Spelling: `DICARDED` | `db/writers.py:35`, `analysis/core.py:102` | ✅ Correct | One-R spelling preserved correctly throughout codebase as required. |
| Spelling: `mql___mdr_comments` | `connectors/hubspot_pull.py:49` | ✅ Correct | Three underscores preserved correctly. |

---

## 11. Recommended Phase 1.5 PR Roadmap

Based exclusively on the repo audit above. Each PR builds on the previous. No PR may begin until its predecessor is merged.

---

### PR-ADS-026 — Fix Opportunities Page: Rename + Show Company Name
**Priority:** Highest UX fix — actively misleading users today

| | |
|---|---|
| **Backend files** | `db/schema.py` (add `company TEXT` to `leads` table via `ALTER TABLE … ADD COLUMN IF NOT EXISTS`), `db/writers.py` (`write_leads()` — add company from `props.get("company")`), `api/server.py` (add `company` to `SELECT` in `api_leads()`) |
| **Frontend files** | `static/index.html` (rename sidebar label from "Opportunities" to "In Progress"), `static/app.js` (`loadOpportunities()` — render `company` name in card title instead of `contact_id`) |
| **API contract** | `docs/API_CONTRACT.md` — add `company` field to `/api/leads` response schema |
| **Data contract** | `leads` table gains nullable `company TEXT` column |
| **Testing plan** | `GET /api/leads?days=30` returns `company` field; opportunities page shows company names after next run; zero company rows show `—` not crash |
| **Risk** | Low. `ALTER TABLE … ADD COLUMN IF NOT EXISTS` is idempotent. Existing rows show `NULL` company until next run. |
| **Dependencies** | None — safe to start now |

---

### PR-ADS-028 — Waste & Search Terms Page
**Priority:** High — endpoint exists with full data, no UI renders it

| | |
|---|---|
| **Backend files** | None required (endpoint already exists and is correct) |
| **Frontend files** | `static/index.html` — add sidebar item "Waste Terms" and `#page-waste` section; `static/app.js` — add `loadWaste()` function that calls `GET /api/waste?days=N`; `static/styles.css` — junk-category badge styles |
| **API contract** | No changes (endpoint already documented) |
| **Data contract** | No changes |
| **Testing plan** | Waste page shows table after weekly run with waste terms, spend, category, CRM junk count; time range selector changes data; empty state shown when no waste data |
| **Risk** | Very low. Pure frontend addition. |
| **Dependencies** | None |

---

### PR-ADS-029 — Geo DB Table + Writer + `/api/geo` Endpoint
**Priority:** High — data fetched every run but immediately discarded

| | |
|---|---|
| **Backend files** | `db/schema.py` — add `geo` table (`run_id, run_date, country, campaign_name, spend_usd, clicks, impressions, conversions`); `db/writers.py` — add `write_geo()` function; `scheduler/weekly.py` — call `db_writers.write_geo(run_id, geos)` after Windsor pull; `scheduler/monthly.py` — same; `api/server.py` — add `GET /api/geo?days=N` endpoint |
| **Frontend files** | None in this PR (data layer only) |
| **API contract** | `docs/API_CONTRACT.md` — add `/api/geo` endpoint |
| **Data contract** | New `geo` table; new `write_geo()` function |
| **Testing plan** | After weekly run, `GET /api/geo?days=30` returns country-level rows; `geo` table has rows; re-running weekly run does not duplicate rows for same `run_id` |
| **Risk** | Low. Additive. Non-fatal DB writes. |
| **Dependencies** | None |

---

### PR-ADS-030 — Keywords DB Table + Writer + `/api/keywords` Endpoint
**Priority:** High — keyword data fetched every run but discarded

| | |
|---|---|
| **Backend files** | `db/schema.py` — add `keywords` table (`run_id, run_date, campaign_name, keyword, match_type, quality_score, spend_usd, clicks, impressions, conversions, cpc_usd`); `db/writers.py` — add `write_keywords()`; `scheduler/weekly.py` — call `db_writers.write_keywords(run_id, keywords)`; `scheduler/monthly.py` — same; `api/server.py` — add `GET /api/keywords?days=N` |
| **Frontend files** | None in this PR |
| **API contract** | Add `/api/keywords` |
| **Data contract** | New `keywords` table |
| **Testing plan** | After weekly run, `GET /api/keywords?days=30` returns keyword rows with match_type and spend; no duplicate rows per run_id |
| **Risk** | Low. Additive. |
| **Dependencies** | None (can be done in parallel with PR-ADS-029) |

---

### PR-ADS-031 — Geo Intelligence Page (Country Map + Table)
**Priority:** High — but requires PR-ADS-029 first

| | |
|---|---|
| **Backend files** | `api/server.py` — add `GET /api/leads/country-summary?days=N` that GROUP BY country from `leads` table returning total/sql/junk/junk_rate per country |
| **Frontend files** | `static/index.html` — add "Geo" sidebar item and `#page-geo` section; `static/app.js` — `loadGeo()` combining `/api/geo` (spend/clicks per country) + `/api/leads/country-summary` (leads/sql/junk per country); `static/styles.css` — country table, country cards |
| **API contract** | Add `/api/leads/country-summary` |
| **Data contract** | No changes (reads from existing `leads` + new `geo` table) |
| **Testing plan** | Geo page shows country table with spend + lead count + junk rate after run; top-spend countries listed first |
| **Risk** | Medium — depends on both `geo` table (PR-ADS-029) and `leads.country` being populated correctly |
| **Dependencies** | PR-ADS-029 must be merged first |

---

### PR-ADS-032 — Campaigns Table: Fix Leads Column + Add Country/Keyword Drill-Down
**Priority:** Medium — visible bug fix + first drill-down capability

| | |
|---|---|
| **Backend files** | `api/server.py` — modify `/api/campaigns` query to include `SUM(total_leads)` and expose it in response; add `GET /api/campaigns/{campaign_name}/leads?days=N` for per-campaign lead breakdown |
| **Frontend files** | `static/app.js` — render leads count; add click handler on campaign row to open a side panel or modal with per-campaign lead breakdown table |
| **API contract** | Update `/api/campaigns` response to include `total_leads`; add `GET /api/campaigns/{name}/leads` |
| **Data contract** | No changes |
| **Testing plan** | Campaigns table shows lead count (not `—`); clicking row opens breakdown; breakdown shows country, keyword, mql_status distribution |
| **Risk** | Medium — requires campaign name URL encoding |
| **Dependencies** | None |

---

### PR-ADS-033 — Thresholds API Endpoint (Remove JS Hardcoding)
**Priority:** Low-medium — maintenance risk fix

| | |
|---|---|
| **Backend files** | `api/server.py` — add `GET /api/config` that reads `config/thresholds.yaml` and returns the junk_rate thresholds (and nothing else sensitive) |
| **Frontend files** | `static/app.js` — replace hardcoded `JUNK_RATE_LOW_THRESHOLD` / `JUNK_RATE_HIGH_THRESHOLD` with values fetched once at startup from `GET /api/config` |
| **API contract** | Add `GET /api/config` |
| **Data contract** | No changes |
| **Testing plan** | Change threshold in YAML → restart → UI reflects new value without JS change |
| **Risk** | Very low |
| **Dependencies** | None |

---

### PR-ADS-034 — Data Freshness Badge + Run History Timeline
**Priority:** Medium — critical for trust in the tool

| | |
|---|---|
| **Backend files** | None — all data already in `GET /api/summary` (`last_run_at`, `last_run_status`) and `GET /api/runs` |
| **Frontend files** | `static/index.html` — add `#data-freshness-bar` above pages; `static/app.js` — fetch `last_run_at` from summary and render "Data as of: 28 Apr · weekly run · success"; Dashboard run panel extended to show last 5 runs from `/api/runs` (timeline) |
| **API contract** | No changes |
| **Data contract** | No changes |
| **Testing plan** | Freshness badge shows correct `last_run_at`; run timeline shows multiple runs |
| **Risk** | Very low |
| **Dependencies** | None |

---

### PR-ADS-035 — Keywords Performance Page (match-type analysis)
**Priority:** Medium — requires PR-ADS-030 first

| | |
|---|---|
| **Backend files** | No new endpoints needed if `GET /api/keywords` is already added |
| **Frontend files** | `static/index.html` — add "Keywords" sidebar item; `static/app.js` — `loadKeywords()` renders keyword table with spend, match_type, quality_score, clicks, CPQL; match-type distribution summary (broad/phrase/exact count) |
| **API contract** | No changes (uses existing endpoint) |
| **Data contract** | No changes |
| **Testing plan** | Keywords page shows sorted keyword table after run; match-type distribution visible |
| **Risk** | Low |
| **Dependencies** | PR-ADS-030 |

---

## 12. Non-Goals for Phase 1.5

The following are **explicitly excluded** from Phase 1.5 and must not be started:

- ❌ `POST /api/chat` — AI advisor chat interface
- ❌ N-gram analysis engine
- ❌ Negative keyword candidates list
- ❌ `connectors/oct_uploader.py` — Phase 2 gate
- ❌ `connectors/negative_pusher.py` — Phase 3
- ❌ Meta Ads connector — Phase 4
- ❌ Any write to Google Ads API
- ❌ Any write to HubSpot API
- ❌ Automated bid changes
- ❌ Automatic campaign pausing
- ❌ Brand vs. non-brand analysis (data not available yet)

---

## 13. Phase 1 Read-Only Compliance Checklist

| Check | Status |
|-------|--------|
| No `POST`/`PATCH` to Google Ads in any file | ✅ Verified |
| No `POST`/`PATCH` to HubSpot in any file | ✅ Verified |
| No OCT conversion upload endpoint | ✅ `connectors/oct_uploader.py` does not exist |
| No negative keyword push endpoint | ✅ `connectors/negative_pusher.py` does not exist |
| `DICARDED` spelled with one R throughout | ✅ Verified in `db/writers.py:35`, `analysis/core.py:102`, `config/junk_patterns.yaml` |
| `mql___mdr_comments` uses three underscores | ✅ Verified in `connectors/hubspot_pull.py:49` |
| All thresholds in YAML (not hardcoded in Python) | ⚠️ Partial — thresholds are in YAML and read by Python; **frontend JS hardcodes them separately** (see `app.js:23-24`) |
| Connectors do analysis | ❌ None detected |
| Schedulers contain business logic | ❌ None detected — they orchestrate only |
| Analysis layer calls external APIs | ❌ `analysis/advisor.py` calls Claude API only when `ADVISOR_MODE=claude` — default is deterministic, no external calls |
| `min_spend_to_flag: 5` waste threshold hardcoded in Python | ❌ Verified: defined in `config/thresholds.yaml` and read by `analysis/core.py:90` |
| No incorrect `DISCARDED` spelling | ✅ Searched `grep DISCARDED` — not found |

---

## 14. Open Questions Requiring Youssef Decision

1. **Opportunities page:** Keep as "In Progress Leads" (showing CRM meeting-booked contacts) or replace with a real deal pipeline view that shows the full funnel? They are different things — current naming is misleading.

2. **Waste page:** When the Waste page is added, should it show *all* search terms (not just junk) grouped by category, or only confirmed/suspected waste? Full search term storage requires expanding the DB significantly.

3. **Geo map:** SVG/CSS-based world map (low dependency), or third-party map library (Leaflet/Mapbox)? Third-party adds external dependency. SVG choropleth requires more frontend code but keeps zero dependencies.

4. **Report viewer:** Should the AI-generated weekly markdown report be rendered in the UI (formatted), or remain email-only? Endpoint `GET /reports/latest/raw` already exists — just no page renders it.

5. **AI advisor chat:** Should Phase 1.5 include a basic read-only chat that queries DB directly and asks Claude for interpretation? Or defer entirely to Phase 4? The data layer must exist first either way — but chat should not be built before the data is clean.

6. **Comparison period:** Should the "vs. previous period" feature be part of Phase 1.5, or deferred? It requires no schema changes — only a new query pattern in the API and frontend delta rendering.

7. **Dashboard alerts priority:** The dashboard currently shows all FIX/CUT campaigns without severity ordering. Should alerts be ranked by spend? By junk rate delta? By both? What is the tiebreaker?

8. **Thresholds via API:** Should `GET /api/config` expose the full `thresholds.yaml` to the frontend, or only the subset relevant to UI coloring (junk rate thresholds)? Exposing full config reveals internal verdict logic — is that acceptable for viewer/mdr roles?

9. **Brand vs. non-brand split:** `config/junk_patterns.yaml` has a `safe_terms` whitelist for competitors (cargowise, gofreight, etc.). Should brand/non-brand segmentation be tracked explicitly in the DB, or derived on the fly from keyword values?

10. **Venezuela CUT market indicator:** `confirmed_cut_markets: [venezuela]` is in `thresholds.yaml` but not shown anywhere in the UI. Should the geo page highlight confirmed-CUT countries with a special marker?

---

*End of audit. No code was written. No endpoints were created. No UI was modified.*
*This document is the complete factual inventory of the repository as of 2026-05-02.*
