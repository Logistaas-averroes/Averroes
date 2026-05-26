# 23 — Production Reality & UX Navigation Audit

**PR:** PR-ADS-064  
**Date:** 2026-05-25  
**Phase:** 1 — Read Only  
**Status:** 🔨 In Progress  
**Depends On:** PR-ADS-063  
**Blocks:** PR-ADS-065+ production fixes  

---

## 1. Executive Summary

The Logistaas Ads Intelligence platform is technically online and serving authenticated users. The API exposes 30+ endpoints, the scheduler runs daily/weekly/monthly jobs, and the PostgreSQL database stores 13 tables of advertising, CRM, and attribution data.

However, the production UI presents a **trust problem**:

- **Search Terms** page shows 0 matching terms, $0 spend, 0 unanalyzed — yet dataset freshness says "Fresh" and latest weekly run says "success."
- **Waste Terms** page shows 0 flagged waste — because it depends on search_terms evidence.
- **N-Grams** page cannot produce pattern analysis without search_terms data.

A user cannot determine whether this means:
1. Windsor returned no search-term rows
2. Windsor returned rows but DB writing failed
3. DB has rows but the API is filtering them out
4. API returns rows but frontend filters/rendering hides them
5. The weekly run happened before the latest code deployed
6. "Fresh" means "job completed" not "usable data exists"

This audit traces every sidebar page from UI → endpoint → DB table → scheduler → external source, diagnoses freshness semantics, and recommends the next PR sequence.

---

## 2. Production Trust Problems Found

| # | Problem | Severity | Root Cause Hypothesis |
|---|---------|----------|----------------------|
| 1 | Search Terms shows 0 rows while freshness says "fresh" | Critical | "Fresh" only means sync_state was written — does not verify row_count > 0 |
| 2 | Waste Terms shows 0 (depends on empty search_terms) | High | Derived from search_terms; if source is empty, derivative is zero |
| 3 | N-Grams shows no patterns (depends on empty search_terms) | High | Same as above |
| 4 | Sidebar has 17 equal-weight pages with no grouping | Medium | All pages presented flat; user cannot distinguish command vs evidence vs admin |
| 5 | No distinction between "no data" and "broken pipeline" | High | Empty states show generic "no data" without explaining why or what to check |
| 6 | Freshness can be "success" with zero rows | Critical | sync_state tracks sync completion, not data presence |
| 7 | Deployment timing unclear relative to scheduler runs | Medium | No visible indicator of whether latest code was running when scheduler last executed |

---

## 3. Sidebar Page Inventory

| # | Page | data-page | User Purpose | Data Source | Endpoint | DB Table | Populated By | Current Risk | UX Classification |
|---|------|-----------|-------------|-------------|----------|----------|-------------|-------------|-------------------|
| 1 | Dashboard | `dashboard` | Executive overview | DB aggregates | `/api/summary`, `/api/dashboard/trends` | campaigns, leads, waste_terms | weekly/monthly | Needs source clarity | Command Center |
| 2 | Action Queue | `action-queue` | Human review priorities | Analysis outputs | `/api/action-queue` | campaigns, leads, deals | analysis/core | Needs explainability | Command Center |
| 3 | Reports | `reports` | Latest generated report | outputs/ filesystem | `/reports/latest`, `/reports/latest/raw` | filesystem | scheduler | OK if latest visible | Command Center |
| 4 | Campaigns | `campaigns` | Campaign performance truth | Windsor + HubSpot | `/api/campaigns` | campaigns | weekly/monthly/incremental | Needs date/source clarity | Evidence |
| 5 | Waste Terms | `waste` | Flagged waste subset | Analysis over search_terms | `/api/waste` | waste_terms | weekly analysis | **Critical:** shows 0 but depends on search_terms | Evidence / Review |
| 6 | Search Terms | `search-terms` | Raw query universe | Windsor | `/api/search-terms` | search_terms | weekly/daily | **Critical:** shows 0 but fresh | Evidence / Critical |
| 7 | N-Grams | `ngrams` | Search pattern analysis | Computed from search_terms | `/api/search-terms/ngrams` | search_terms (computed) | API on-the-fly | Blocked if search_terms empty | Evidence |
| 8 | Geo | `geo` | Country performance | Windsor | `/api/geo` | geo | weekly/incremental | TBD — needs verification | Evidence |
| 9 | Keywords | `keywords` | Keyword performance | Windsor | `/api/keywords` | keywords | weekly/incremental | TBD — needs verification | Evidence |
| 10 | Lead Quality | `leads` | CRM status quality | HubSpot | `/api/leads`, `/api/leads/country-summary` | leads | weekly/daily/incremental | TBD | Evidence |
| 11 | Deals | `deals` | Pipeline outcomes | HubSpot | `/api/deals` | deals | weekly/incremental | TBD | Evidence |
| 12 | GCLID Attribution | `gclid-attribution` | Click-to-CRM match | Windsor + HubSpot | `/api/gclid-attribution`, `/api/attribution/quality`, `/api/gclid-coverage` | gclid_attribution, gclid_coverage_snapshots | weekly gclid_match | TBD | Evidence |
| 13 | In Progress Leads | `opportunities` | Open MDR leads | HubSpot | `/api/leads` (filtered) | leads | daily/weekly | TBD | Review Queue |
| 14 | Scheduler | `scheduler` | Manual run/status | APScheduler | `/scheduler/status`, `/api/runs` | runs | scheduler | Needs job-level results | Admin |
| 15 | System Health | `health` | System status | DB/env/runs | `/api/monitoring/status`, `/api/datasets/freshness` | sync_state, runs | live | Needs pipeline status | Admin |
| 16 | Historical Backfill | `backfill` | Admin range backfill | CLI/API | `/api/backfill/run`, `/api/backfill/status` | multiple | manual | Search terms backfill unsupported | Admin |
| 17 | Historical Intelligence | `historical-intelligence` | Historical trends | DB | `/api/historical-intelligence` | multiple | backfill | TBD | Admin |

---

## 4. Endpoint Inventory

| Endpoint | Method | Auth | Purpose | DB Table(s) | Notes |
|----------|--------|------|---------|-------------|-------|
| `/` | GET | No | Serve static UI | — | HTMLResponse |
| `/health` | GET | No | Health check | — | Returns status |
| `/readiness` | GET | Admin | Full readiness check | all | Checks env, DB, config |
| `/auth/login` | POST | No | User login | — | Returns session cookie |
| `/auth/logout` | POST | Auth | User logout | — | Clears session |
| `/auth/me` | GET | Auth | Current user info | — | Returns user/role |
| `/runs/latest` | GET | Auth | Latest run info | runs | — |
| `/reports/latest` | GET | Auth | Latest report | filesystem | — |
| `/reports/latest/raw` | GET | Auth | Raw report text | filesystem | — |
| `/scheduler/status` | GET | Auth | Scheduler status | — | APScheduler jobs |
| `/run/daily` | POST | Admin | Trigger daily run | runs + multiple | — |
| `/run/weekly` | POST | Admin | Trigger weekly run | runs + multiple | — |
| `/run/monthly` | POST | Admin | Trigger monthly run | runs + multiple | — |
| `/run/incremental-sync` | POST | Admin | Trigger incremental sync | sync_batches + multiple | — |
| `/api/campaigns` | GET | Auth | Campaign data | campaigns | ?days= param |
| `/api/leads` | GET | Auth | Lead data | leads | ?days= param |
| `/api/deals` | GET | Auth | Deal data | deals | ?days= param |
| `/api/waste` | GET | Auth | Waste terms | waste_terms | ?days= param |
| `/api/runs` | GET | Auth | Run history | runs | — |
| `/api/summary` | GET | Auth | Dashboard summary | campaigns, leads, waste_terms | — |
| `/api/geo` | GET | Auth | Geo performance | geo | ?days= param |
| `/api/keywords` | GET | Auth | Keyword performance | keywords | ?days= param |
| `/api/leads/country-summary` | GET | Auth | Lead quality by country | leads | ?days= param |
| `/api/campaign-detail` | GET | Auth | Campaign detail | campaigns, leads | — |
| `/api/campaigns/{name}/detail` | GET | Auth | Named campaign detail | campaigns, leads | — |
| `/api/config/ui-thresholds` | GET | Auth | UI thresholds | config YAML | — |
| `/api/dashboard/trends` | GET | Auth | Dashboard trends | campaigns | — |
| `/api/action-queue` | GET | Auth | Action queue items | campaigns, leads, deals | — |
| `/api/datasets/freshness` | GET | Auth | Dataset freshness state | sync_state | Critical for trust |
| `/api/search-terms` | GET | Auth | Search terms (paginated) | search_terms | ?days=&cursor=&limit= |
| `/api/search-terms/summary` | GET | Auth | Search terms summary | search_terms | Aggregate stats |
| `/api/search-terms/ngrams` | GET | Auth | N-gram analysis | search_terms (computed) | On-the-fly |
| `/api/gclid-attribution` | GET | Auth | GCLID attribution | gclid_attribution | — |
| `/api/attribution/quality` | GET | Auth | Attribution quality | gclid_attribution | — |
| `/api/gclid-coverage` | GET | Auth | GCLID coverage | gclid_coverage_snapshots | — |
| `/api/monitoring/status` | GET | Auth | System monitoring | sync_state, runs | — |
| `/api/backfill/run` | POST | Admin | Run backfill | multiple | — |
| `/api/backfill/status` | GET | Auth | Backfill status | multiple | — |
| `/api/historical-intelligence` | GET | Auth | Historical trends | multiple | — |

---

## 5. Database Table Inventory

| Table | Date Column | Populated By | Source | Purpose |
|-------|-------------|-------------|--------|---------|
| `runs` | `started_at` | All schedulers | Internal | Run audit trail |
| `campaigns` | `run_date` | weekly, monthly, incremental_sync | Windsor + HubSpot (merged) | Campaign performance truth table |
| `leads` | `run_date` | weekly, daily, incremental_sync | HubSpot | Lead quality tracking |
| `deals` | `run_date` | weekly, incremental_sync | HubSpot | Deal pipeline outcomes |
| `keywords` | `run_date` | weekly, incremental_sync | Windsor | Keyword performance |
| `geo` | `run_date` | weekly, incremental_sync | Windsor | Country/campaign performance |
| `search_terms` | `source_date` | weekly, daily | Windsor | Raw search term universe |
| `waste_terms` | `run_date` | weekly (analysis) | Analysis over search_terms | Flagged waste terms |
| `gclid_attribution` | `created_at` | weekly (gclid_match) | Windsor + HubSpot join | Click-to-CRM attribution |
| `gclid_coverage_snapshots` | `snapshot_date` | weekly (gclid_match) | Aggregate | GCLID coverage metrics |
| `sync_batches` | `started_at` | All data writes | Internal | Sync audit trail |
| `sync_state` | `updated_at` | All data writes (watermark) | Internal | Dataset freshness watermark |
| `migrations` | — | Schema init | Internal | Schema migration tracking |

---

## 6. Scheduler / Trigger Inventory

| Job | Schedule | Timezone | Module | Datasets Written | Notes |
|-----|----------|----------|--------|-----------------|-------|
| Daily pulse | 06:00 | Asia/Amman | `scheduler/daily.py` | runs, leads, search_terms | 2-day window; anomaly detection in-memory |
| Weekly report | Mon 07:00 | Asia/Amman | `scheduler/weekly.py` | runs, campaigns, leads, deals, keywords, geo, search_terms, waste_terms, gclid_attribution, gclid_coverage_snapshots | Full 30d campaigns + 60d search terms |
| Monthly report | 1st 08:00 | Asia/Amman | `scheduler/monthly.py` | runs, campaigns, leads, deals, keywords, geo | 90-day window |
| Incremental sync | 09:00 daily | Asia/Amman | `scheduler/incremental_sync.py` | campaigns, keywords, geo, leads, deals | 14-day ads / 30-day deals; **search_terms SKIPPED** |

**Critical finding:** Incremental sync explicitly skips search_terms. Only weekly and daily runs write search_terms.

---

## 7. Data Pipeline Map

```
┌─────────────────────────────────────────────────────────────────────────┐
│ EXTERNAL SOURCES                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│ Windsor.ai REST API          │  HubSpot CRM API (v4)                    │
│  • campaigns (30d)           │  • contacts/leads (30d)                  │
│  • search_terms (60d)        │  • deals with GCLID                      │
│  • keywords (30d)            │                                           │
│  • geo (30d)                 │                                           │
└──────────────┬───────────────┴───────────────┬──────────────────────────┘
               │                               │
               ▼                               ▼
┌──────────────────────────────┐ ┌────────────────────────────────────────┐
│ connectors/windsor_pull.py   │ │ connectors/hubspot_pull.py             │
│  pull_campaign_performance() │ │  pull_paid_search_contacts()           │
│  pull_search_terms(60d)      │ │  pull_deals_with_gclid()               │
│  pull_keyword_performance()  │ │                                        │
│  pull_geo_performance()      │ │                                        │
└──────────────┬───────────────┘ └───────────────┬────────────────────────┘
               │                                  │
               ▼                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ analysis/core.py                                                         │
│  run_waste_detection() → waste_terms                                     │
│  run_lead_quality() → lead classifications                               │
│  run_campaign_truth() → merged campaign rows                             │
├─────────────────────────────────────────────────────────────────────────┤
│ connectors/gclid_match.py                                                │
│  run_gclid_match() → attribution rows + coverage snapshots               │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ db/writers.py (PostgreSQL via psycopg2)                                   │
│  write_campaigns() → campaigns table                                     │
│  write_leads() → leads table                                             │
│  write_deals() → deals table                                             │
│  write_keywords() → keywords table                                       │
│  write_geo() → geo table                                                 │
│  write_search_terms() → search_terms table                               │
│  write_waste_terms() → waste_terms table                                 │
│  write_gclid_attribution() → gclid_attribution table                     │
│  write_gclid_coverage_snapshot() → gclid_coverage_snapshots table        │
│  start_sync_batch() / finish_sync_batch() / update_sync_state()           │
│    → sync_batches + sync_state                                             │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ API Layer (api/server.py — FastAPI)                                       │
│  /api/search-terms → search_terms table (paginated, filtered)            │
│  /api/search-terms/ngrams → computed from search_terms table             │
│  /api/waste → waste_terms table                                          │
│  /api/campaigns → campaigns table                                        │
│  /api/datasets/freshness → sync_state table                              │
│  ... (30+ endpoints)                                                     │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Frontend (static/app.js — SPA)                                           │
│  loadSearchTerms() → GET /api/search-terms?days=N&cursor=...             │
│  loadWaste() → GET /api/waste?days=N                                     │
│  loadNgrams() → GET /api/search-terms/ngrams?days=N                      │
│  Per-page freshness strip from /api/datasets/freshness                   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 8. Dataset Freshness Audit

### How Freshness Works

1. **`sync_state` table** stores one row per `(source, dataset)` pair.
2. When `db/writers.py` calls `finish_sync_batch(status="success")`, it updates `sync_state.status = 'success'` and sets `last_successful_sync_at` (or `update_sync_state(...)` does the equivalent upsert).
3. The `/api/datasets/freshness` endpoint reads `sync_state` and returns status per dataset.
4. The frontend (`static/app.js`) displays freshness strips per page via `PAGE_DATASET_MAP`.

### The Trust Bug

**Freshness tracks whether a sync job completed, not whether data was produced.**

If `finish_sync_batch(status="success", row_count=0)` is called, the sync_state shows:
- `status = "success"`
- `last_successful_sync_at = now()`

The UI then says "Fresh" because the sync was recent and successful.

**But the table has zero rows.**

This is the root cause of the Search Terms trust problem.

### Current Freshness States (actual)

| State | Meaning |
|-------|---------|
| `success` | Sync job completed without error |
| `failed` | Sync job threw an exception |
| `running` | Sync is in progress |
| `unknown` | No sync has ever run for this dataset |

### Recommended Freshness States (future PR)

| State | Meaning |
|-------|---------|
| `fresh_with_data` | Sync successful AND row_count > 0 |
| `fresh_but_empty` | Sync successful BUT row_count = 0 |
| `stale_with_data` | Data exists but latest sync is older than threshold |
| `stale_and_empty` | No recent sync and no data |
| `failed` | Sync threw an error |
| `unknown` | No sync record exists |

### Where Freshness Logic Lives

- **Writer:** `db/writers.py` → `start_sync_batch()`, `finish_sync_batch()`, `update_sync_state()` (updates sync_state)
- **API:** `api/server.py` → `/api/datasets/freshness` (reads sync_state)
- **Frontend:** `static/app.js` → `loadDatasetFreshness()` + `PAGE_DATASET_MAP`
- **Display:** Freshness strip rendered per page section using `renderFreshnessStrip()`

---

## 9. Search Terms Pipeline Investigation

### Pipeline Trace

```
Windsor REST API (date_preset=last_60d)
    ↓
connectors/windsor_pull.py :: pull_search_terms(days_back=60)
    ↓
data/ads_search_terms.json (JSON file output)
    ↓
db/writers.py :: write_search_terms(rows, run_id, sync_batch_id)
    ↓
search_terms table (PostgreSQL)
    ↓
api/server.py :: /api/search-terms?days=60&limit=100&cursor=...
    ↓
static/app.js :: loadSearchTerms() renders page
```

### Step-by-Step Analysis

#### Step 1: Windsor Pull

**File:** `connectors/windsor_pull.py`  
**Function:** `pull_search_terms(days_back=60)`  
**Behavior:** Calls Windsor REST API with `date_preset=last_60d` (PR-ADS-063 confirmed rolling presets, not date_from/date_to).  
**Expected:** Returns list of dicts with search term rows.  
**Failure modes:**
- Windsor API key invalid → empty response
- Windsor has no search term data for account → empty list
- Network error → exception logged, empty list returned
- `date_preset` not recognized → 400 error (fixed in PR-ADS-027)

**Verification:**
```bash
# Check Windsor pull logs:
grep "search.term" runtime_logs/run_history.jsonl
# Or check sync_batches:
SELECT * FROM sync_batches WHERE dataset='search_terms' ORDER BY started_at DESC LIMIT 5;
```

#### Step 2: JSON File Output

**File:** `data/ads_search_terms.json`  
**Behavior:** Windsor pull writes JSON to disk before DB write.  
**Verification:**
```bash
ls -la data/ads_search_terms.json
python -c "import json; d=json.load(open('data/ads_search_terms.json')); print(len(d), 'rows')"
```

#### Step 3: DB Write

**File:** `db/writers.py`  
**Function:** `write_search_terms(rows, run_id, sync_batch_id)`  
**Behavior:**
- Unique key: `(source_date, campaign_name, ad_group, keyword, match_type, search_term)`
- Uses `INSERT ... ON CONFLICT DO UPDATE` (upsert)
- Writes sync_batch tracking
- `is_flagged_waste` left NULL on initial write (analysis step sets it later)
- Returns row count written

**Failure modes:**
- `start_sync_batch()` returns 0 → DB unavailable, write skipped
- Empty input list → no rows written, batch still marked success
- Schema mismatch → SQL error logged

**Verification:**
```sql
SELECT COUNT(*) FROM search_terms;
SELECT COUNT(*) FROM search_terms WHERE source_date >= CURRENT_DATE - INTERVAL '60 days';
SELECT MAX(source_date) FROM search_terms;
SELECT * FROM sync_batches WHERE dataset='search_terms' ORDER BY started_at DESC LIMIT 3;
```

#### Step 4: API Endpoint

**File:** `api/server.py` line 3053  
**Route:** `GET /api/search-terms`  
**Parameters:** `days` (1-90, default 14), `campaign`, `match_type`, `q`, `waste_state`, `limit` (1-500, default 100), `cursor`  
**Behavior:**
- Queries `search_terms WHERE source_date >= CURRENT_DATE - INTERVAL 'N days'`
- Cursor/keyset pagination on `(source_date DESC, id DESC)`
- Returns `{rows: [...], pagination: {...}, data_quality: {...}}`
- If DB unavailable, returns empty with `db_unavailable: true`

**Important:** Default `days=14`. If the UI doesn't pass `?days=60`, only the last 14 days are queried.

**Failure modes:**
- `days` too small relative to when data was written → rows excluded
- All rows filtered by `waste_state` parameter → empty result
- `campaign` filter with wrong case → no matches (names stored lowercase)

**Verification:**
```bash
curl -s -H "Cookie: ads_session=..." "$APP_URL/api/search-terms?days=60&limit=10" | jq '.rows | length'
```

#### Step 5: Frontend Rendering

**File:** `static/app.js`  
**Function:** `loadSearchTerms()`  
**Behavior:**
- Calls `GET /api/search-terms?days=${_selectedDays}&limit=100`
- Also calls `GET /api/search-terms/summary?days=${_selectedDays}` for summary stats
- Renders matching terms count, spend, unanalyzed count
- Uses `_selectedDays` (session-stored, default 30)

**Key observation:** The frontend uses `_selectedDays` which defaults to 30. The API default is 14. If the frontend sends days=30 correctly, and the API has no rows in that window, the page shows 0.

**Failure modes:**
- Frontend sends wrong days parameter → fewer rows returned
- Summary endpoint returns 0 counts → "Matching terms: 0"
- Empty response interpreted as "no data" without distinguishing cause

### Deployment Timing Check

**Critical question:** Was PR-ADS-063 deployed before or after the latest weekly run?

If the latest weekly run occurred before PR-ADS-063 was deployed:
- The old search-term pull code ran (which may have used broken parameters)
- Zero rows is expected because the old pull may have failed silently
- Next weekly run with new code should fix this

**How to verify:**
```sql
SELECT run_type, status, started_at FROM runs WHERE run_type='weekly' ORDER BY started_at DESC LIMIT 3;
```
Compare `started_at` to PR-ADS-063 merge timestamp.

### Root Cause Hypothesis

Most likely cause of Search Terms showing 0:

1. **Timing:** Latest weekly run occurred before PR-ADS-063 code was deployed → Windsor pull used old broken parameters → returned 0 rows → wrote 0 rows → marked sync as "success" with row_count=0
2. **Or:** Windsor actually returned 0 rows for this account's search terms in the last 60 days (unlikely for an active account)
3. **Or:** `write_search_terms()` received rows but a schema/constraint error prevented inserts

**Resolution path:** Wait for next weekly run (Mon 07:00 Asia/Amman) and check sync_batches.row_count afterward. If still 0, investigate Windsor response directly.

---

## 10. Waste Terms vs Search Terms Explanation

### Definitions

| Dataset | What It Is | Source | When Populated |
|---------|-----------|--------|----------------|
| **Search Terms** | Full raw search query universe from Google Ads via Windsor | Windsor REST API | Weekly pull (60d window) + Daily pull (2d window) |
| **Waste Terms** | Subset of search terms flagged by waste detection rules | `analysis/core.py :: run_waste_detection()` | Weekly analysis step (after search terms are pulled) |
| **N-Grams** | Aggregated phrase/pattern analysis over search terms | Computed on-the-fly by API | Not stored; computed per-request from search_terms table |

### Dependency Chain

```
search_terms (populated by Windsor pull)
    ↓ waste detection runs against search_terms
waste_terms (subset flagged as waste)
    ↓ n-gram analysis runs against search_terms
ngrams (computed on-the-fly)
```

### If Search Terms is empty:
- Waste Terms will always be empty (no source data to analyze)
- N-Grams will always return no patterns (no rows to aggregate)
- These are NOT independent failures — they are consequences of the same root cause

### If Search Terms has rows but Waste Terms is empty:
- Either no terms matched waste detection rules (legitimate: clean account)
- Or waste detection did not run (missing step in scheduler)
- Or waste detection ran but `write_waste_terms()` wrote 0 rows (threshold too strict)

### UI Confusion

Current UI treats Waste Terms as if it's an independent dataset. Users expect to see "waste" data even when search_terms is empty. The page should explain its dependency.

---

## 11. Historical Backfill vs Rolling Sync Explanation

### Rolling Sync (Weekly/Daily)

| Dataset | Window | Method |
|---------|--------|--------|
| Campaigns | Last 30 days | `date_from`/`date_to` parameters |
| Keywords | Last 30 days | `date_from`/`date_to` parameters |
| Geo | Last 30 days | `date_from`/`date_to` parameters |
| Search Terms | Last 60 days | `date_preset=last_60d` (rolling preset) |
| Leads | Last 30 days | `createdate` filter |
| Deals | All with GCLID | No date filter |

### Historical Backfill (Manual)

**File:** `scripts/backfill.py`, `scripts/backfill_windsor.py`  
**Endpoint:** `POST /api/backfill/run`

Supports explicit `date_from`/`date_to` for:
- ✅ Campaigns
- ✅ Keywords
- ✅ Geo
- ✅ Leads
- ✅ Deals
- ❌ **Search Terms — NOT SUPPORTED**

**Reason:** Windsor search terms use rolling date presets (`last_7d`, `last_30d`, `last_60d`), not arbitrary `date_from`/`date_to`. You cannot backfill search terms for "January 2025" because Windsor's API doesn't support it.

### Key Distinction for UI

The Historical Backfill admin page should NOT show a "Search Terms" option (or should show it greyed out with explanation). Currently this may cause confusion if users expect to backfill search term history.

---

## 12. UX Navigation Diagnosis

### Current State: 17 Flat Pages

All 17 sidebar pages are presented with equal visual weight, no grouping, and no hierarchy. A new user cannot distinguish:
- Which pages are "command" views (actionable decisions)
- Which pages are "evidence" views (supporting data)
- Which pages are "admin" views (system management)
- Which pages depend on other pages being populated

### Problems

1. **No grouping:** Dashboard, System Health, and GCLID Attribution all appear as equal-weight peers
2. **No dependency visibility:** N-Grams doesn't indicate it depends on Search Terms
3. **No role awareness:** Admin pages (Scheduler, Backfill) visible to all users at same level
4. **Unclear naming:** "Waste Terms" sounds like all waste, but it's a subset; "Geo" is too abbreviated; "N-Grams" is technical jargon
5. **Empty pages have no context:** "No data found" doesn't tell user whether to wait, investigate, or contact admin

### Empty State Classification

| Page | Current Empty State | Classification |
|------|-------------------|----------------|
| Search Terms | "No search terms found" + "Matching: 0" | **Dangerous** — fresh badge + zero data |
| Waste Terms | "No waste terms found" | **Bad** — no explanation of dependency |
| N-Grams | "No data" | **Bad** — no explanation |
| Dashboard | Zeros in summary cards | **Dangerous** — looks broken vs new |
| Campaigns | Empty table | **Acceptable** — time range selector visible |
| Leads | Empty table | **Acceptable** |
| Others | Various "no data" messages | **Bad** — no context |

---

## 13. Recommended Sidebar Restructure

### Proposed Grouping

```
Command Center
├── Dashboard
├── Action Queue
└── Reports

Evidence
├── Campaigns
├── Search Term Universe (rename from "Search Terms")
├── Search Pattern Analysis (rename from "N-Grams")
├── Keyword Performance (rename from "Keywords")
├── Country Performance (rename from "Geo")
├── Lead Quality
├── Deals
└── GCLID Attribution

Waste & Review
├── Flagged Waste Terms (rename from "Waste Terms")
└── In Progress Leads

Admin
├── Data Runs (rename from "Scheduler")
├── System Status (rename from "System Health")
├── Historical Backfill
└── Historical Trends (rename from "Historical Intelligence")
```

### Proposed Renames

| Current | Proposed | Reason |
|---------|----------|--------|
| Search Terms | Search Term Universe | Clarifies it's the full raw dataset, not just waste |
| Waste Terms | Flagged Waste Terms | Clarifies it's a flagged subset, not all waste |
| N-Grams | Search Pattern Analysis | Less technical jargon |
| Geo | Country Performance | More descriptive |
| Keywords | Keyword Performance | More descriptive |
| Scheduler | Data Runs | User-friendly |
| System Health | System Status | Clearer |
| Historical Intelligence | Historical Trends | Less grandiose |

### Implementation Notes

- Rename is a frontend-only change (data-page attributes + display labels)
- Grouping requires HTML restructure of sidebar
- Should be a dedicated PR (PR-ADS-068) to avoid mixing with data fixes

---

## 14. Follow-up PR Roadmap

Based on this audit, the recommended sequence is:

| PR | Title | Priority | Rationale |
|----|-------|----------|-----------|
| **PR-ADS-065** | Search Terms Pipeline Verification & Repair | P0 | Verify next weekly run produces rows; if not, debug Windsor pull → DB write path |
| **PR-ADS-066** | Freshness Semantics & Zero-Row Truth States | P1 | Implement `fresh_with_data` / `fresh_but_empty` distinction in sync_state and UI |
| **PR-ADS-067** | Canonical Freshness Semantics & Zero-Row Truth States | P1 | Backend canonical freshness service with 10 truth states |
| **PR-ADS-068** | System Status War Room & Pipeline Dependency Map | P1 | Consolidated system status page with blockers, pipelines, source health, scheduler |
| **PR-ADS-069** | Sidebar UX Grouping & Page Rename | P2 | Implement grouping + renames from Section 13 |
| **PR-ADS-070** | Empty State + Page Explanation Upgrade | P2 | Each page explains what it shows, data source, and what empty means |

### Decision Logic

- If next weekly run produces search_term rows → PR-ADS-065 is just "verify and close"
- If next weekly run still produces 0 → PR-ADS-065 must debug Windsor response shape vs parser
- PR-ADS-066 is independent of search-term fix — freshness semantics are broken regardless
- PR-ADS-067 and PR-ADS-068 can proceed in parallel

---

## 15. Open Questions / Requires Production Logs

| # | Question | How to Answer | Who |
|---|----------|---------------|-----|
| 1 | Was the latest weekly run before or after PR-ADS-063 deployed? | `SELECT started_at FROM runs WHERE run_type='weekly' ORDER BY started_at DESC LIMIT 1` vs deploy timestamp | Admin |
| 2 | What did Windsor return for search terms in the last run? | Check `sync_batches WHERE dataset='search_terms'` row_count | Admin |
| 3 | Does `data/ads_search_terms.json` exist on the production server? | SSH/file check | Admin |
| 4 | Are there any error logs from `write_search_terms()`? | Check application logs for "write_search_terms" | Admin |
| 5 | Is the Google Ads account actually serving ads with search terms? | Check Google Ads UI → Search Terms report | Account owner |
| 6 | Is the Windsor API key authorized for search term data? | Test `pull_search_terms()` locally with production credentials | Admin |
| 7 | Did incremental_sync ever accidentally overwrite search_terms? | Incremental_sync skips search_terms — confirm in logs | Admin |
| 8 | What timezone is the Render deployment running in? | Check deployment config / env | Admin |

---

## Appendix A: Diagnostic Script

A read-only diagnostic script has been created at:

```
scripts/audit_production_reality.py
```

Usage:
```bash
python scripts/audit_production_reality.py --days 60 --pretty
python scripts/audit_production_reality.py --days 60 --json
```

The script:
- Connects to PostgreSQL via DATABASE_URL (read-only mode)
- Checks row counts for 7d/14d/30d/60d windows per table
- Checks sync_state for freshness records
- Flags `FRESH_BUT_EMPTY` states (the core trust problem)
- Reports pipeline blockers (search_terms → waste_terms → ngrams)
- Never writes, never calls external APIs, never triggers schedulers

---

## Appendix B: PAGE_DATASET_MAP (Frontend)

The frontend maps pages to datasets for freshness display:

```javascript
const PAGE_DATASET_MAP = {
  campaigns:         ["windsor/campaigns"],
  waste:             ["windsor/search_terms"],   // derived from search_terms
  search_terms:      ["windsor/search_terms"],
  ngrams:            ["windsor/search_terms"],   // derived from search_terms
  geo:               ["windsor/geo"],
  keywords:          ["windsor/keywords"],
  lead_quality:      ["hubspot/contacts"],
  deals:             ["hubspot/deals"],
  gclid_attribution: ["gclid/matches"],
  in_progress_leads: ["hubspot/contacts"],
  action_queue:      ["windsor/campaigns", "hubspot/contacts", "hubspot/deals"],
  reports:           ["windsor/campaigns", "hubspot/contacts", "hubspot/deals", "windsor/search_terms"],
};
```

Note: `waste` and `ngrams` are marked as `DERIVED_DATASET_PAGES` — the freshness strip says "Derived from" instead of "Dataset freshness:".

---

## PR-ADS-065 Follow-up Finding

### Search Terms Pipeline Verification & Repair

**Diagnosis (PR-ADS-065):**

The Search Terms pipeline required targeted verification and hardening because:
- `persistence_succeeded()` treated zero fetched + zero written as success
- Zero rows could produce "successful" sync state while evidence layer is empty
- Waste Terms and N-Grams depend on Search Terms being populated

**Changes made:**

1. **Connector hardening** (`connectors/windsor_pull.py`):
   - Added explicit logging: date_preset, row count, sample keys, search_term field presence
   - Added `normalize_search_term_rows()` function
   - Loud WARNING when both last_60d and last_14d return zero rows
   - ERROR when rows lack search_term field (contract violation)

2. **DB writer hardening** (`db/writers.py`):
   - Logs input_rows, prepared_rows, skipped_blank_search_term, written_rows
   - ERROR when all rows are skipped due to missing/blank search_term

3. **Scheduler hardening** (`scheduler/weekly.py`, `scheduler/daily.py`):
   - Zero-row pulls now use `status="success"` with `row_count=0` plus warning message
   - Fetched > 0 but written = 0 explicitly raises and marks failed

4. **API data_quality enhancement** (`api/server.py`):
   - `/api/search-terms` and `/api/search-terms/summary` now include:
     `table`, `days`, `rows_in_window`, `total_rows_in_window`, `rows_returned`, `latest_source_date`, `is_empty`, `warning`

5. **Frontend safety** (`static/app.js`):
   - Empty state now warns about pipeline issue, not just "no results"
   - Directs user to check Scheduler/System Health/Reality Audit

6. **Verification script** (`scripts/verify_search_terms_pipeline.py`):
   - Supports: `--db-only`, `--pull-live`, `--api-url`, `--admin-token`, `--pretty`, `--json`
   - Produces verdicts: OK, WINDSOR_PULL_EMPTY, DB_WRITE_FAILED, etc.

**Expected production run output:**

```
Verdict will be determined by first production run after this PR merges.
Until scheduler runs with these changes, the actual diagnosis is:
NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT
```

**Next steps:**
- After deployment, run: `python scripts/verify_search_terms_pipeline.py --days 60 --db-only --pretty`
- If verdict is WINDSOR_PULL_EMPTY: investigate Windsor REST vs MCP parity (PR-ADS-066)
- If verdict is DB_WRITE_FAILED: fix write_search_terms() before proceeding
- If verdict is OK: proceed with Waste Terms and N-Grams confidence

---

## PR-ADS-066 — Search Terms Production Verdict Panel & Windsor Source-Parity Resolution

**Goal:** Make Search Terms pipeline status visible and actionable from the System Health page.

**Added:**
1. **Verdict endpoint** (`GET /api/system/search-terms-verdict?days=60`):
   - Admin-only. Returns verdict, DB row counts, sync status, next action.
   - Verdicts: OK, WINDSOR_PULL_EMPTY, DB_WRITE_FAILED, DB_HAS_ROWS_API_EMPTY, FRESH_BUT_EMPTY, NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT, DB_UNAVAILABLE, UNKNOWN.

2. **System Health panel** (Search Terms Pipeline):
   - Verdict card with color coding (green/orange/red/gray).
   - Row counts, latest source date, sync status, batch rows, next action.
   - Copy: "Search Terms is the raw evidence layer. Waste Terms and N-Grams depend on this dataset."

3. **MCP import script** (`scripts/import_windsor_mcp_search_terms.py`):
   - Bridge for importing Windsor MCP payload when REST returns empty.
   - Dry-run by default. `--apply` to write.
   - Validates search_term field, normalizes ad_group_id from resource path.
   - Creates sync_batch with source="windsor_mcp".

**Production verifier output (CI — no DB):**
```
Verdict: DB_UNAVAILABLE
Reason: Database connection unavailable
```

**REST vs MCP parity:** If production shows WINDSOR_PULL_EMPTY while MCP previously returned ~45,292 rows, use the MCP import path as a bridge until REST parity is confirmed.
---

## PR-ADS-069 — Sidebar UX Grouping & Page Rename

**Goal:** Restructure the sidebar into clear operator-intent groups and rename confusing page labels.

**Changes:**
1. **Sidebar grouped** into four sections: Command Center, Evidence, Review & Quality, Admin.
2. **Labels renamed** to operator-friendly language (see `docs/24_UI_NAVIGATION_MODEL.md` for full rename map).
3. **Route keys unchanged** — all `data-page` attributes remain stable.
4. **CSS** — section label styles, admin quieting, no accordion.
5. **Page headers** updated to match new visible labels.
6. **Documentation** — `docs/24_UI_NAVIGATION_MODEL.md` added with navigation model and route stability rule.

**Route stability rule:** Visible page names may change, but `data-page` route keys must remain stable unless a dedicated migration PR updates every reference.

---

## PR-ADS-070 — Empty State & Page Explanation Upgrade

**Goal:** Upgrade page explanations and empty states so users never see vague "No data found" without context.

**Changes:**
1. **PAGE_EXPLANATIONS** — config object in `static/app.js` with purpose/source/dependsOn/emptyMeans/nextAction for all 17 routes.
2. **PAGE_DEPENDENCIES** — mapping of each page to its upstream dataset dependencies.
3. **renderPageExplanation()** — reusable helper rendering compact explanation panels with context chips.
4. **buildEmptyState()** — helper generating severity-aware empty state blocks based on canonical status.
5. **Critical empty states updated** — Search Term Universe warns zero ≠ clean; Waste Terms and N-Grams explain Search Terms dependency; Admin Backfill states dry-run is safe/read-only.
6. **CSS** — `.page-explanation`, `.page-context-chips`, `.context-chip`, `.empty-state--warning/blocked/error/info` styles added.
7. **Documentation** — `docs/25_EMPTY_STATE_AND_PAGE_EXPLANATION_MODEL.md` created.

**Key principle:** A zero-row page should only look scary if canonical freshness says it is suspicious, blocked, stale, or failed.
