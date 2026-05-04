# Data Warehouse & Historical Sync Readiness Audit

**Document:** `docs/10_DATA_WAREHOUSE_READINESS_AUDIT.md`
**Roadmap ID:** PR-ADS-038
**Phase:** 1.5 — Read-Only Data Foundation
**Owner:** Youssef Awwad
**Audit date:** 2026-05-04
**Status:** Audit-only. No code changed. No schema changed.

---

## 1. Executive Verdict

### Is the system ready to store full historical Windsor + HubSpot data?

**No.** The current system is a Phase 1 intelligence dashboard with a partial persistence model. It can store recent snapshots from each scheduler run but has no mechanism for historical backfill, no upsert-by-source-date strategy, and no watermark tracking.

### What data is already persisted?

| Dataset | Persisted? | Table | Notes |
|---------|-----------|-------|-------|
| Campaign performance | ✅ Yes | `campaigns` | Derived / analysis output per run, NOT raw Windsor rows |
| HubSpot paid-search contacts | ✅ Yes | `leads` | Per run, contact-level |
| GCLID-linked deals | ✅ Yes | `deals` | Per run, deal-level |
| Flagged waste search terms | ✅ Yes | `waste_terms` | Only junk-classified rows |
| Geo performance | ✅ Yes | `geo` | Per run, source_date preserved from Windsor date field |
| Keyword performance | ✅ Yes | `keywords` | Per run, source_date preserved from Windsor date field |
| Scheduler run log | ✅ Yes | `runs` | Every run event |
| Migration registry | ✅ Yes | `migrations` | One-time idempotent DDL guard |

### What data is still temporary or discarded?

| Dataset | Current state | Risk |
|---------|--------------|------|
| Full search terms (non-waste) | ❌ Discarded after analysis | No forensic search-term history |
| Raw Windsor JSON outputs | 🟨 Runtime files only (`data/*.json`) | Overwritten on every run |
| GCLID coverage stats | 🟨 `data/gclid_coverage.json` only | No DB persistence |
| GCLID matched records | 🟨 `data/matched_gclid.json` only | No DB persistence |
| HubSpot raw contact fields | 🟨 Partial | Several fetched fields not stored (email, lifecycle stage, createdate, hs_latest_source*) |
| Sync watermarks | ❌ None | No tracking of what date ranges have been synced |
| Historical backfill data | ❌ None | No backfill mechanism exists |

### Should the system use one-time historical backfill + daily incremental sync?

**Yes.** The correct model is:

1. **One-time historical backfill** — pull the maximum available date range from Windsor and HubSpot once and persist to PostgreSQL.
2. **Daily incremental sync** — pull the last 2–3 days only, upsert to DB, update watermark.
3. **Weekly/monthly analysis** — read from DB facts, not from fresh API pulls.

Fetching all-time data every day is **not acceptable** unless a dataset is provably tiny (< a few hundred rows) and the API allows it safely without rate-limiting. Neither Windsor nor HubSpot currently qualifies.

### Which tables should be created next?

1. `sync_batches` — tracks every sync operation (backfill or incremental)
2. `sync_state` — tracks last successful sync watermark per source+dataset
3. `search_terms` — full raw search-term storage (high priority)
4. `gclid_attribution` — persist matched_gclid.json as a DB table

### Which existing tables should remain unchanged?

All eight existing tables (`runs`, `campaigns`, `leads`, `waste_terms`, `deals`, `geo`, `keywords`, `migrations`) should remain unchanged in PR-ADS-039. New tables are additive only.

### What should PR-ADS-039 build first?

PR-ADS-039 should create `sync_batches` and `sync_state` tables, and add the supporting writer functions. No connector rewrites. No backfill scripts. Watermark infrastructure only.

---

## 2. Current Data Flow Inventory

### Windsor.ai

Base URL: `https://connectors.windsor.ai/all`
Auth: `WINDSOR_API_KEY` + `WINDSOR_ACCOUNT_ID`
Connector: `connectors/windsor_pull.py`

#### `pull_campaign_performance(days_back=30)`

| Field | Fetched? | Stored in DB? | Table | Notes |
|-------|---------|--------------|-------|-------|
| `date` | ✅ | ❌ Raw date not stored | — | `campaigns.run_date` is the *write date*, not source date |
| `campaign` | ✅ | ✅ | `campaigns.campaign_name` | Canonicalised to lowercase |
| `campaign_id` | ✅ | ❌ | — | Fetched but not written |
| `spend` | ✅ | ✅ | `campaigns.spend_usd` | Via analysis layer (campaign truth) |
| `clicks` | ✅ | ✅ | `campaigns.clicks` | |
| `impressions` | ✅ | ✅ | `campaigns.impressions` | |
| `conversions` | ✅ | ✅ | `campaigns.conversions` | |
| `cpc` | ✅ | ❌ | — | Available but not stored |
| `cpm` | ✅ | ❌ | — | Available but not stored |
| `ctr` | ✅ | ❌ | — | Available but not stored |
| `conversion_rate` | ✅ | ❌ | — | Available but not stored |

**Output file:** `data/ads_campaigns.json` (overwritten each run)
**Current consumer:** `analysis/core.py` (campaign truth table), `scheduler/daily.py` (anomaly detection reads this file)
**DB grain:** One row per campaign per run (analysis output, not per source_date). Raw Windsor rows are not stored.
**Limitation:** `campaigns` table stores derived analysis output, not raw Windsor daily rows. Summing across multiple run records inflates metrics.

---

#### `pull_search_terms(days_back=N)`

| Field | Fetched? | Stored in DB? | Table | Notes |
|-------|---------|--------------|-------|-------|
| `date` | ✅ | ❌ | — | Discarded |
| `search_term` | ✅ | 🟨 Waste only | `waste_terms.search_term` | Non-waste terms discarded |
| `campaign` | ✅ | 🟨 Waste only | `waste_terms.campaign_name` | |
| `campaign_id` | ✅ | ❌ | — | Never stored |
| `ad_group` | ✅ | ❌ | — | Fetched, never stored |
| `keyword` | ✅ | ❌ | — | Fetched, never stored |
| `match_type` | ✅ | ❌ | — | Fetched, never stored |
| `spend` | ✅ | 🟨 Waste only | `waste_terms.spend_usd` | |
| `clicks` | ✅ | ❌ | — | Discarded |
| `impressions` | ✅ | ❌ | — | Discarded |
| `conversions` | ✅ | ❌ | — | Discarded |

**Output file:** `data/ads_search_terms.json` (overwritten each run)
**Current consumer:** `analysis/core.py` (waste detection), `connectors/gclid_match.py` (keyword enrichment), `scheduler/daily.py` (junk detection)

**⚠️ Critical limitation — date_preset cap:**
`pull_search_terms()` uses Windsor's `date_preset` parameter, **not** `date_from`/`date_to`. The mapping is:
- `days_back <= 1` → `last_1d`
- `days_back <= 7` → `last_7d`
- `days_back > 7` → `last_14d` (hard cap)

This means even when the monthly scheduler calls `pull_search_terms(days_back=30)`, Windsor only returns the last 14 days. There is no confirmed way to pull search terms beyond 14 days using the current Windsor plan and connector. Historical search-term backfill is technically uncertain and must be verified against the Windsor plan tier before being designed.

**Scheduler call sites:**
- `scheduler/daily.py` → `days_back=1` → `last_1d` preset
- `scheduler/weekly.py` → `days_back=14` → `last_14d` preset
- `scheduler/monthly.py` → `days_back=30` → maps to `last_14d` (no extended preset exists)

---

#### `pull_keyword_performance(days_back=30)`

| Field | Fetched? | Stored in DB? | Table | Notes |
|-------|---------|--------------|-------|-------|
| `date` | ✅ | ✅ | `keywords.run_date` | Source date preserved via `g.get("date")` fallback |
| `campaign` | ✅ | ✅ | `keywords.campaign_name` | Canonicalised |
| `campaign_id` | ✅ | ❌ | — | Not stored |
| `ad_group` | ✅ | ✅ | `keywords.ad_group` | |
| `keyword` | ✅ | ✅ | `keywords.keyword` | |
| `match_type` | ✅ | ✅ | `keywords.match_type` | |
| `quality_score` | ✅ | ✅ | `keywords.quality_score` | |
| `spend` | ✅ | ✅ | `keywords.spend_usd` | |
| `clicks` | ✅ | ✅ | `keywords.clicks` | |
| `impressions` | ✅ | ✅ | `keywords.impressions` | |
| `conversions` | ✅ | ✅ | `keywords.conversions` | |
| `cpc` | ✅ | ✅ | `keywords.cpc_usd` | |

**Output file:** `data/ads_keywords.json`
**Current consumer:** `analysis/core.py` (waste detection fallback if search terms missing), `db/writers.py` → `keywords` table
**DB grain:** One row per (run_id, source_date, campaign, ad_group, keyword, match_type). `write_keywords()` deletes by run_id before insert — re-run safe.

---

#### `pull_geo_performance(days_back=30)`

| Field | Fetched? | Stored in DB? | Table | Notes |
|-------|---------|--------------|-------|-------|
| `date` | ✅ | ✅ | `geo.run_date` | Source date preserved |
| `campaign` | ✅ | ✅ | `geo.campaign_name` | Canonicalised |
| `country` | ✅ | ✅ | `geo.country` | |
| `spend` | ✅ | ✅ | `geo.spend_usd` | |
| `clicks` | ✅ | ✅ | `geo.clicks` | |
| `impressions` | ✅ | ✅ | `geo.impressions` | |
| `conversions` | ✅ | ✅ | `geo.conversions` | |

**Output file:** `data/ads_geos.json`
**Current consumer:** `db/writers.py` → `geo` table
**DB grain:** One row per (run_id, source_date, country, campaign). `write_geo()` deletes by run_id before insert — re-run safe.

---

#### `save_output()` (Windsor)

Writes to:
- `data/ads_campaigns.json`
- `data/ads_search_terms.json`
- `data/ads_keywords.json`
- `data/ads_geos.json`

All four files are **overwritten on every scheduler run**. They are not versioned, not archived, and not persisted to a database. They serve as runtime connector outputs for the analysis layer. They are **not** a persistent data store.

---

### HubSpot

Base URL: `https://api.hubapi.com`
Auth: `HUBSPOT_API_KEY` (Bearer token)
Connector: `connectors/hubspot_pull.py`

#### `pull_paid_search_contacts(days_back=90)`

Filters contacts where `hs_analytics_source = PAID_SEARCH` and `createdate >= cutoff`.

| Field | Fetched? | Stored in DB? | Table | Notes |
|-------|---------|--------------|-------|-------|
| `firstname` / `lastname` | ✅ | ❌ | — | Not stored (PII consideration) |
| `email` | ✅ | ❌ | — | Fetched via CONTACT_PROPERTIES but not written to leads |
| `company` | ✅ | ✅ | `leads.company` | |
| `hs_google_click_id` | ✅ | ✅ | `leads.gclid` | GCLID |
| `mql_status` | ✅ | ✅ | `leads.mql_status` | Raw HubSpot field |
| `hs_lead_status` | ✅ | ❌ | — | Fetched but not stored |
| `lifecyclestage` | ✅ | ❌ | — | Fetched but not stored |
| `hs_analytics_source` | ✅ | ✅ | `leads.source_type` | Mapped to closed enum |
| `hs_analytics_source_data_1` | ✅ | ✅ | `leads.campaign_name` | UTM campaign |
| `hs_analytics_source_data_2` | ✅ | ✅ | `leads.keyword` | UTM keyword |
| `hs_latest_source` | ✅ | ❌ | — | Fetched but not stored |
| `hs_latest_source_data_1` | ✅ | ❌ | — | Fetched but not stored |
| `hs_latest_source_data_2` | ✅ | ❌ | — | Fetched but not stored |
| `hs_analytics_first_url` | ✅ | ❌ | — | Used in GCLID extraction (gclid_match.py) but not stored in leads |
| `ip_country` / `country` | ✅ | ✅ | `leads.country` | |
| `createdate` | ✅ | ❌ | — | Used as filter cutoff but not stored in leads table |
| `hubspot_owner_id` | ✅ | ❌ | — | Fetched but not stored |
| `mql___mdr_comments` | ✅ | ❌ | — | Used in junk signal detection (get_lead_quality_summary) but not stored |
| `search_terms` | ✅ | ❌ | — | Fetched but not stored |

**Output file:** `data/crm_contacts.json`
**Current consumer:** `analysis/core.py` (lead quality, campaign truth), `connectors/gclid_match.py`, `db/writers.py` → `leads`
**Pagination:** Implemented — cursor-based via `response.paging.next.after`. Full paginated pull of all matching contacts.

---

#### `pull_deals_with_gclid(contacts)`

Fetches deals for contacts that have `hs_google_click_id`. Uses CRM v4 REST for associations.

| Field | Fetched? | Stored in DB? | Table | Notes |
|-------|---------|--------------|-------|-------|
| `dealname` | ✅ | ❌ | — | Not stored |
| `dealstage` | ✅ | ✅ | `deals.deal_stage` | Raw stage ID |
| `amount` | ✅ | ✅ | `deals.deal_amount_usd` | |
| `closedate` | ✅ | ❌ | — | Fetched but not stored |
| `createdate` | ✅ | ❌ | — | Fetched but not stored |
| `pipeline` | ✅ | ❌ | — | Fetched but not stored |
| `hs_deal_stage_probability` | ✅ | ❌ | — | Fetched but not stored |
| `gclid` (injected) | N/A | ✅ | `deals.gclid` | Injected from contact |
| `contact_id` (injected) | N/A | ✅ | `deals.contact_id` | |
| `stage_label` (derived) | N/A | ✅ | `deals.deal_stage_label` | Mapped from DEAL_STAGE_MAP |

**Output file:** `data/crm_deals.json`
**⚠️ Association pagination:** `assoc_results` (contact → deal links) currently has no pagination. Tracked as `# TODO PR-ADS-028`. Contacts with more than one page of deal associations may have incomplete deal data.

---

#### `get_lead_quality_summary(contacts)`

In-memory aggregation only. Produces:
- total contact count
- GCLID coverage count and percentage
- MQL status breakdown
- Country breakdown
- Junk indicator list (from MDR comments scan)

**Output file:** `data/crm_summary.json`
**Stored in DB?** ❌ No. Used for report generation and daily CRM delta check only. Not persisted.

---

#### GCLID Match Engine (`connectors/gclid_match.py`)

Reads: `data/ads_search_terms.json`, `data/crm_contacts.json`, `data/crm_deals.json`
Writes: `data/matched_gclid.json`, `data/gclid_coverage.json`

**Stored in DB?** ❌ No. Both files are JSON-only. `matched_gclid.json` contains the full contact→deal→GCLID attribution chain. `gclid_coverage.json` contains coverage statistics.

This is a significant gap. The GCLID attribution dataset is the most valuable cross-platform join in the system and currently has no DB persistence.

---

## 3. Current PostgreSQL Table Inventory

| Table | Purpose | Source | Grain | Date Field | Primary Entity | Snapshot or Event? | Current Limitations |
|-------|---------|--------|-------|------------|----------------|-------------------|---------------------|
| `runs` | Scheduler run log | scheduler | One row per scheduler execution | `started_at` | Run event | Event | No run_type filtering on API |
| `campaigns` | Campaign performance | analysis/core.py campaign truth | One row per campaign per run | `run_date` (write date) | Campaign | Snapshot per run | ⚠️ Stores derived analysis output, not raw Windsor. No source_date grain. Summing across runs inflates metrics. |
| `leads` | HubSpot paid-search contacts | hubspot_pull.py | One row per contact per run | `run_date` (write date) | Contact | Snapshot per run | ⚠️ Same contact appears once per run. No createdate stored. No upsert. No lifecycle stage or MDR comments. |
| `waste_terms` | Junk-classified search terms | analysis/core.py waste detection | One row per flagged term per run | `run_date` (write date) | Search term (junk) | Snapshot per run | ⚠️ Only waste terms stored; non-waste terms discarded entirely |
| `deals` | GCLID-linked deals | hubspot_pull.py | One row per deal per run | `run_date` (write date) | Deal | Snapshot per run | ⚠️ Same deal repeated across runs. No deal_id as stable identifier. |
| `geo` | Geo performance | windsor_pull.py | One row per (run_id, source_date, country, campaign) | `run_date` (source date preserved) | Geo row | Fact (partial) | Source date is preserved. Deletes by run_id before re-insert. No upsert by (source_date, country, campaign). |
| `keywords` | Keyword performance | windsor_pull.py | One row per (run_id, source_date, campaign, ad_group, keyword, match_type) | `run_date` (source date preserved) | Keyword row | Fact (partial) | Source date is preserved. Deletes by run_id before re-insert. No upsert by natural key. |
| `migrations` | One-time DDL guard | db/schema.py | One row per migration_id | `applied_at` | Migration | Event | None |

### Snapshot inflation risk

Tables that store a snapshot per run (`campaigns`, `leads`, `deals`, `waste_terms`) accumulate one new set of rows per weekly/monthly run. If rows are summed across runs without filtering to a single run or date window, metrics will be inflated.

- **Example:** If 5 weekly runs have each stored a `campaigns` row for `europe low cpc-new`, summing `spend_usd` across all 5 rows gives 5× the actual spend.
- **Current mitigation:** `GET /api/campaigns` queries use `DISTINCT ON` with the latest run per campaign, or aggregate over the `?days=` window. This handles display correctly but does not solve the underlying data model issue.
- **Correct future model:** Raw Windsor performance rows should be stored with `source_date` grain and upserted by natural key. Derived analysis outputs (verdicts, CPQL) should be separate or annotated clearly.

---

## 4. Raw Facts vs Derived Analysis

### Raw fact tables (should store external data as close to source as possible)

| Dataset | Status | Table | Notes |
|---------|--------|-------|-------|
| Ad campaign performance (per source_date) | 🟨 Partial | `campaigns` | Stored as derived analysis output, not raw Windsor rows |
| Ad keyword performance (per source_date) | ✅ Yes | `keywords` | Source date preserved |
| Ad geo performance (per source_date) | ✅ Yes | `geo` | Source date preserved |
| Ad search terms (all terms) | ❌ Missing | — | No table exists |
| HubSpot contacts (raw) | 🟨 Partial | `leads` | Missing several fields; no createdate; no upsert by contact_id |
| HubSpot deals (raw) | 🟨 Partial | `deals` | Missing deal_id; no upsert; repeated per run |
| GCLID attribution links | ❌ Missing | — | JSON file only |

### Derived analysis tables/outputs (computed from raw facts)

| Dataset | Where it lives | Notes |
|---------|---------------|-------|
| Campaign truth (verdict, CPQL, lead join) | `campaigns` table + analysis/core.py | Currently mixed with raw fields |
| Waste classifications | `waste_terms` table | Derived from pattern matching against search terms |
| Action queue | `GET /api/action-queue` | Computed at query time from DB |
| Dashboard trends | `GET /api/dashboard/trends` | Computed at query time |
| Lead quality breakdown | `GET /api/leads`, `GET /api/summary` | Computed at query time |
| Weekly/monthly reports | `outputs/*.md` files | Generated by advisor, not persisted to DB |
| GCLID coverage summary | `data/gclid_coverage.json` | Computed by gclid_match.py, not persisted |

### Separation rules

```
Connectors       →  fetch raw external data
Writers          →  persist raw facts to PostgreSQL
Analysis modules →  classify, summarise, compute derived outputs
API endpoints    →  read from DB, serve truth to frontend
Frontend         →  display API truth only
```

**Frontend must not create business truth.** Verdicts, waste flags, lead quality scores, and attribution must always originate in the backend.

---

## 5. Historical Backfill Strategy

### One-time historical backfill

**Purpose:** Fill PostgreSQL with as much historical Windsor + HubSpot data as the APIs allow, once.

#### Windsor historical range audit

| Dataset | Parameter | Hard limit | Notes |
|---------|-----------|-----------|-------|
| Campaign performance | `date_from` / `date_to` | Unknown — likely 12–24 months | Uses explicit date range; no preset needed |
| Keyword performance | `date_from` / `date_to` | Unknown — likely 12–24 months | Uses explicit date range |
| Geo performance | `date_from` / `date_to` | Unknown — likely 12–24 months | Uses explicit date range |
| Search terms | `date_preset` only | **Hard cap: `last_14d`** | ⚠️ Cannot pull historical search terms beyond 14 days with current connector design. Verify against Windsor plan tier whether `date_from`/`date_to` is supported for search terms before building backfill. |

Campaign, keyword, and geo data can likely be backfilled over arbitrary date ranges using `date_from`/`date_to`. Search terms may be permanently limited depending on the Windsor plan.

#### HubSpot historical range audit

| Dataset | Filter | Limit | Notes |
|---------|--------|-------|-------|
| Contacts (paid search) | `createdate >= cutoff` | No API hard limit; pagination supported | Can pull all time by setting `days_back` to a large value or removing the date filter |
| Deals | No date filter on deals themselves | Inherits from contact list | Current implementation pulls deals for GCLID contacts only — backfill must iterate all contacts with GCLIDs |

HubSpot contacts and deals can be fully backfilled since the connector uses cursor-based pagination and the date filter is configurable.

#### Recommended backfill command pattern

```bash
# Full backfill for all sources
python -m scripts.backfill --source all --from 2023-01-01 --to 2026-05-04

# Or split by source
python -m scripts.backfill_windsor --from 2023-01-01 --to 2026-05-04
python -m scripts.backfill_hubspot --from 2023-01-01 --to 2026-05-04
```

**Do not implement backfill scripts in this PR.**

Backfill scripts must:
- Require explicit `--from` and `--to` date arguments (no defaults)
- Support `--dry-run` mode
- Write to DB only (no external writes)
- Record each batch in `sync_batches` table
- Update `sync_state` watermark on success

---

### Daily incremental sync

**Purpose:** Update recent data every day without refetching all history.

**Recommended approach:**
- Daily sync pulls the last 2–3 days from Windsor for mutable recent records (late attribution, updated conversions).
- HubSpot contacts/deals should use `createdate` window and, where available, `hs_lastmodifieddate` to catch recently updated records.
- Windsor search terms: pull `last_1d` daily. Store if `search_terms` table exists.
- Do not run analysis (waste detection, campaign truth) on daily sync — analysis should run weekly or on-demand from stored DB facts.

**Recommended command pattern:**

```bash
python -m scheduler.daily_sync
```

Extend the existing daily scheduler only after the audit defines the data model.

**Do not implement in this PR.**

---

## 6. Data Freshness and Watermark Strategy

### Current state

The system has **no sync watermark tracking**. There is no table, no file, and no in-memory record of what date ranges have been synced for each dataset.

**Consequences:**
- Every scheduler run refetches the full configured window (e.g., 30 days) regardless of what was already stored.
- There is no way to know whether a dataset is stale at the dataset level.
- A retry after a failure will re-fetch and re-insert rows with no deduplication.
- The UI shows only the last scheduler run time — not per-dataset freshness.

### Recommended `sync_state` table

```sql
CREATE TABLE IF NOT EXISTS sync_state (
  id                     SERIAL PRIMARY KEY,
  source                 TEXT NOT NULL,         -- windsor | hubspot
  dataset                TEXT NOT NULL,         -- see dataset list below
  last_successful_sync_at TIMESTAMPTZ,
  last_source_date        DATE,                 -- last source date covered
  status                 TEXT,                  -- success | failed | running
  error_message          TEXT,
  updated_at             TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(source, dataset)
);
```

**Datasets:**

| source | dataset |
|--------|---------|
| `windsor` | `campaigns` |
| `windsor` | `keywords` |
| `windsor` | `search_terms` |
| `windsor` | `geo` |
| `hubspot` | `contacts` |
| `hubspot` | `deals` |
| `gclid` | `matches` |

**Why watermarks matter:**
1. **Prevent all-time refetches.** Once historical data is in the DB, the daily sync only needs to cover a 2–3-day overlap window. Watermarks encode the boundary.
2. **Support retry logic.** If a sync fails mid-run, the watermark is not updated. The next run starts from where the last successful sync left off.
3. **UI freshness display.** `GET /api/datasets/freshness` can show per-dataset last-sync time rather than a single scheduler status.
4. **Audit trail.** Combined with `sync_batches`, watermarks provide a complete audit trail of what data is in the DB.

**Do not implement this table in this PR.**

---

## 7. Recommended Future Tables

### `search_terms` — high priority

Full raw search-term storage for every term (waste and non-waste).

```sql
CREATE TABLE IF NOT EXISTS search_terms (
  id               SERIAL PRIMARY KEY,
  run_id           INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  source_date      DATE         NOT NULL,
  campaign_name    TEXT,
  campaign_id      TEXT,
  ad_group         TEXT,
  keyword          TEXT,
  match_type       TEXT,
  search_term      TEXT,
  spend_usd        NUMERIC(10,2) DEFAULT 0,
  clicks           INTEGER       DEFAULT 0,
  impressions      INTEGER       DEFAULT 0,
  conversions      NUMERIC(8,2)  DEFAULT 0,
  is_flagged_waste BOOLEAN,
  junk_category    TEXT,
  matched_pattern  TEXT,
  created_at       TIMESTAMPTZ   DEFAULT NOW()
);
```

**Design decision — `run_id` nullable vs NOT NULL:**

- If all backfills are structured to create a `runs` record first, `run_id NOT NULL` works.
- Historical backfills that operate independently of the scheduler (e.g., a standalone script) may write search-term facts without a scheduler run context. In that case, `run_id` should be nullable, or a dedicated `sync_batch_id` foreign key should replace it.
- **Recommendation:** Use nullable `run_id` for raw fact tables. Reserve NOT NULL `run_id` for analysis-output tables (campaigns, waste_terms) where the run context is semantically meaningful.

**Upsert key:** `(source_date, campaign_name, ad_group, keyword, match_type, search_term)` — natural deduplication key.

**`is_flagged_waste`, `junk_category`, `matched_pattern`:** These are derived by the analysis layer, not the connector. On initial write from the connector, **all three fields should be left NULL**. The analysis layer updates them in a subsequent pass. This preserves the connector → writer → analysis separation.

`is_flagged_waste` uses three-valued logic:
- `NULL` — not yet analysed by the waste-detection layer
- `TRUE` — analysed and flagged as waste
- `FALSE` — analysed and confirmed clean

Defaulting to `FALSE` would make unanalysed rows indistinguishable from rows that were analysed and found clean. The column must be nullable (`BOOLEAN` without a default) so that the analysis layer can reliably filter for rows that still need classification (`WHERE is_flagged_waste IS NULL`).

---

### `sync_batches` — recommended

Tracks every sync operation including backfills.

```sql
CREATE TABLE IF NOT EXISTS sync_batches (
  id            SERIAL PRIMARY KEY,
  source        TEXT NOT NULL,        -- windsor | hubspot | gclid
  dataset       TEXT NOT NULL,        -- campaigns | keywords | search_terms | ...
  sync_type     TEXT NOT NULL,        -- backfill | daily | weekly | monthly | manual
  date_from     DATE,
  date_to       DATE,
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  status        TEXT,                 -- running | success | failed
  row_count     INTEGER DEFAULT 0,
  error_message TEXT
);
```

**Should this replace `runs`?**

No. `runs` tracks scheduler-level orchestration events (which steps ran, report delivery status). `sync_batches` tracks data-level sync events (which date range was fetched for which dataset). They serve different purposes and should coexist. `sync_batches.run_id` could optionally reference `runs(id)` to link a data sync back to the scheduler run that triggered it.

---

### `raw_contacts` / expanded `leads` table

**Audit of current `leads` table:**

| Field needed | In `leads`? | Notes |
|-------------|------------|-------|
| `contact_id` | ✅ | Nullable |
| `campaign_name` | ✅ | |
| `keyword` | ✅ | |
| `country` | ✅ | |
| `mql_status` | ✅ | |
| `status_category` | ✅ | Derived |
| `gclid` | ✅ | |
| `source_type` | ✅ | |
| `company` | ✅ | |
| `createdate` | ❌ | Not stored — only used as fetch filter |
| `lifecyclestage` | ❌ | Fetched but not stored |
| `hs_lead_status` | ❌ | Fetched but not stored |
| `mql___mdr_comments` | ❌ | Used in in-memory analysis only |
| `hs_analytics_first_url` | ❌ | Used in GCLID extraction only |
| `hs_latest_source*` | ❌ | Fetched but not stored |
| `email` | ❌ | Fetched but intentionally not stored (PII) |

**Recommendation:** Expand `leads` with `createdate DATE` and `lifecyclestage TEXT` at minimum. Adding `mdr_comments TEXT` would enable persistent junk signal history. A separate `hubspot_contacts` raw table is only needed if the analysis/raw separation becomes strict; for Phase 1.5, expanding `leads` is sufficient.

---

### `gclid_attribution`

`data/matched_gclid.json` and `data/gclid_coverage.json` should become a DB table.

```sql
CREATE TABLE IF NOT EXISTS gclid_attribution (
  id                  SERIAL PRIMARY KEY,
  run_id              INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  gclid               TEXT NOT NULL,
  contact_id          TEXT,
  deal_id             TEXT,
  campaign_name       TEXT,
  keyword             TEXT,
  match_type          TEXT,
  first_url           TEXT,
  contact_created_at  TIMESTAMPTZ,
  deal_stage          TEXT,
  deal_stage_label    TEXT,
  deal_amount_usd     NUMERIC(12,2),
  match_status        TEXT,           -- matched | unmatched | url_fallback
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(gclid, contact_id, deal_id)
);
```

**Uniqueness note:** A single contact/GCLID pair may be associated with multiple deals (e.g., a trial deal and a won deal for the same click). The uniqueness key must include `deal_id` to avoid collapsing legitimate multi-deal attribution rows. If `deal_id` is unavailable for some rows (unmatched contacts without any deal), the implementation should use a partial unique index or a generated `attribution_key` so that unmatched rows are not accidentally collapsed against each other.

This enables: GCLID attribution history, trend analysis on GCLID coverage over time, and a future `GET /api/gclid-attribution` endpoint without reading JSON files at request time.

---

## 8. Upsert, Deduplication, and Snapshot Strategy

| Dataset | Recommended Grain | Upsert Key | Keep Snapshots? | Notes |
|---------|------------------|-----------|----------------|-------|
| Campaigns (raw Windsor) | `source_date + campaign_name` | Yes | No | Do not sum across multiple run_ids |
| Campaigns (analysis output) | `run_date + campaign_name` | Yes (by run) | For trend analysis | Verdict/CPQL is run-time derived |
| Keywords | `source_date + campaign_name + ad_group + keyword + match_type` | Yes | No | Daily performance grain |
| Search Terms | `source_date + campaign_name + ad_group + keyword + match_type + search_term` | Yes | No | High volume; upsert is essential |
| Geo | `source_date + country + campaign_name` | Yes | No | Performance per country per day |
| Contacts | `contact_id` | Yes | History optional | HubSpot is source of truth for contact state |
| Deals | `deal_id` (when stored) | Yes | Yes (stage changes matter) | Deal stage changes are meaningful history |
| Waste Terms | Derived from search_terms | Snapshot per run | Keep for current page | Until search_terms table is live |
| GCLID Attribution | `gclid + contact_id + deal_id` | Yes | No | Include deal_id to allow multi-deal contacts |

**Key recommendation:**

> For **raw performance data** (campaigns, keywords, search terms, geo): prefer **source-date grain with upsert** over run-snapshot accumulation.
> For **analysis outputs** (verdicts, CPQL, waste flags, quality scores): keep **run snapshots** so trend analysis can compare values over time.

This distinction is important. Mixing the two models in the same table (as `campaigns` currently does) creates ambiguity and metric inflation risk.

---

## 9. Search Terms Storage Recommendation

### Should full search terms be stored?

**Yes.** Search terms are the raw signal for waste detection, N-gram forensics, negative keyword recommendations, and attribution analysis. Discarding non-waste terms after each run permanently destroys forensic capability.

### Decision matrix

| Question | Answer |
|---------|--------|
| Should full search terms be stored? | ✅ Yes — `search_terms` table (new) |
| Should `waste_terms` remain? | ✅ Yes — for backwards compatibility with `/api/waste` |
| Should non-junk terms go into `waste_terms`? | ❌ No — `waste_terms` is waste-only |
| Should `search_terms` include waste flags? | ✅ Yes — `is_flagged_waste`, `junk_category`, `matched_pattern` fields |
| Who sets waste flags? | Analysis layer (not writer) — writer sets them NULL on insert |
| Should `/api/waste` continue reading `waste_terms`? | ✅ Yes — no change to Phase 1 endpoints |
| Should future `/api/search-terms` read `search_terms`? | ✅ Yes — broader search-terms page |

### Windsor search-term date range limitations

The current connector uses Windsor `date_preset` exclusively for search terms:
- Maximum confirmed window: `last_14d`
- No `date_from`/`date_to` support confirmed for search terms
- Historical backfill of search terms **may not be possible** depending on Windsor plan tier

Before building a search-terms backfill script, verify with Windsor whether the search query type (`data_source=google_ads`, search term fields) supports arbitrary date ranges or is permanently limited to preset windows.

---

## 10. App Page Data Requirements Matrix

| Page | Windsor Campaigns | Keywords | Search Terms | Geo | HubSpot Contacts | HubSpot Deals | GCLID | Current DB Support | Missing |
|------|------------------|---------|-------------|-----|-----------------|--------------|-------|--------------------|---------|
| Dashboard | ✅ | — | — | — | ✅ | — | partial | ✅ campaigns, leads | GCLID coverage not from DB |
| Action Queue | ✅ | ✅ | ✅ (waste) | — | ✅ | — | — | ✅ partial | Full search terms |
| Reports | ✅ | ✅ | ✅ (waste) | ✅ | ✅ | ✅ | — | ✅ partial | Report content from JSON files, not DB |
| Campaigns | ✅ | — | — | — | ✅ | — | — | ✅ | — |
| Campaign Drawer | ✅ | ✅ | ✅ (waste) | ✅ | ✅ | — | — | ✅ partial | Full search terms per campaign |
| Waste Terms | — | — | ✅ (waste) | — | ✅ | — | — | ✅ waste_terms | Full search terms (non-waste) |
| Geo | — | — | — | ✅ | — | — | — | ✅ | — |
| Keywords | — | ✅ | — | — | — | — | — | ✅ | — |
| Lead Quality | — | — | — | — | ✅ | — | — | ✅ | createdate, lifecycle stage |
| Deals | — | — | — | — | ✅ | ✅ | ✅ | ✅ partial | deal_id upsert, deal stage history |
| In Progress Leads | — | — | — | — | ✅ | ✅ | — | ✅ partial | lifecycle stage, MDR comments |
| Scheduler | — | — | — | — | — | — | — | ✅ runs | sync_batches (future) |
| Health | — | — | — | — | — | — | — | ✅ | sync_state freshness (future) |
| Future: Search Terms | — | — | ✅ (full) | — | — | — | — | ❌ Missing | search_terms table |
| Future: N-Grams | — | — | ✅ (full) | — | — | — | — | ❌ Missing | search_terms table |
| Future: Negative Candidate Review | — | — | ✅ (full) | — | — | — | — | ❌ Missing | search_terms table + analysis layer |

---

## 11. Data Volume and Cost Risk

### Estimated row volume per run (weekly)

| Dataset | Estimated rows/run | Notes |
|---------|------------------|-------|
| Campaign rows | ~10–30 | Small number of active campaigns |
| Keyword rows | ~500–2,000 | Varies by number of active ad groups |
| Search term rows | ~1,000–10,000 | Highly variable; 14-day window |
| Contact rows | ~100–1,000 | Paid search contacts only |
| Deal rows | ~50–500 | GCLID-linked only |
| Geo rows | ~100–500 | ~80 countries × campaigns |

### 12-month volume estimate

| Table | Est. rows / year | Risk |
|-------|-----------------|------|
| `campaigns` | ~2,000 | Low |
| `keywords` | ~100,000 | Low |
| `leads` | ~50,000 | Low |
| `deals` | ~25,000 | Low |
| `geo` | ~25,000 | Low |
| `waste_terms` | ~5,000 | Low |
| `search_terms` (new) | ~500,000–5,000,000 | **Medium–High** |

Search terms are the primary volume concern. 14 days of data per weekly run × 52 runs = up to 520,000+ rows/year assuming ~1,000 terms/day. If the dataset includes high-volume brand searches or broad match terms, this could reach millions per year.

### Indexes required for `search_terms`

```sql
CREATE INDEX ON search_terms(source_date);
CREATE INDEX ON search_terms(campaign_name);
CREATE INDEX ON search_terms(is_flagged_waste);

-- Basic equality / prefix ordering helper
CREATE INDEX ON search_terms(search_term);

-- For contains / ILIKE / pattern search at scale, enable pg_trgm and use a trigram index:
-- CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- CREATE INDEX idx_search_terms_search_term_trgm
--   ON search_terms USING gin (search_term gin_trgm_ops);
```

**Important:** A plain B-tree index on `search_term` does not efficiently support contains searches such as `ILIKE '%term%'`. If `/api/search-terms?q=` supports substring or contains search, PostgreSQL trigram indexing (`pg_trgm` extension, GIN index with `gin_trgm_ops`) should be considered from day one. Without it, contains searches will degrade to sequential scans on a table that may reach millions of rows.

### Retention and pagination

- `/api/search-terms` **must support pagination from day one** — do not allow unbounded result sets.
- Consider adding a `days=N` query parameter (default 14 or 30).
- Monthly partition or time-based pruning may be needed if volume grows beyond ~5M rows.
- Full search-term table should never be fetched in full into the frontend.

**Do not implement these indexes or pagination in this PR.**

---

## 12. API Design Recommendations

### `GET /api/search-terms`

| Attribute | Value |
|-----------|-------|
| Source table | `search_terms` |
| Purpose | Browse and filter all stored search terms |
| Required pagination | ✅ Yes — cursor/keyset preferred |
| Phase | 2 (after search_terms table created) |
| Read-only | ✅ Yes |

```
GET /api/search-terms
  ?days=N          (default: 14)
  &campaign=       (filter by campaign_name)
  &match_type=     (exact, phrase, broad)
  &q=              (text search on search_term)
  &waste_only=     (boolean)
  &min_spend=      (numeric)
  &limit=          (default: 100, max: 500)
  &cursor=         (opaque cursor for keyset pagination)
```

**Recommended response shape:**

```json
{
  "rows": [],
  "pagination": {
    "limit": 100,
    "next_cursor": "opaque-token-or-null",
    "has_more": false
  }
}
```

**Pagination note:** Use cursor/keyset pagination rather than `offset`. The `search_terms` table may reach hundreds of thousands to millions of rows. Offset pagination becomes progressively slower for deep pages (full index/table scan to skip N rows) and can return unstable slices while new rows are inserted concurrently. A keyset cursor — typically the `id` or `(source_date, id)` of the last row returned — avoids both problems and performs consistently at scale.

---

### `GET /api/datasets/freshness`

| Attribute | Value |
|-----------|-------|
| Source table | `sync_state` |
| Purpose | Show per-dataset last-sync time and status |
| Required pagination | No |
| Phase | After sync_state table created (PR-ADS-039) |
| Read-only | ✅ Yes |

---

### `GET /api/sync-batches`

| Attribute | Value |
|-----------|-------|
| Source table | `sync_batches` |
| Purpose | Browse sync history, diagnose failures |
| Required pagination | Optional |
| Phase | After sync_batches table created (PR-ADS-039) |
| Read-only | ✅ Yes |

```
GET /api/sync-batches
  ?days=N          (default: 7)
  &source=         (windsor | hubspot)
  &dataset=        (campaigns | keywords | ...)
  &status=         (success | failed | running)
```

---

### `GET /api/gclid-attribution`

| Attribute | Value |
|-----------|-------|
| Source table | `gclid_attribution` |
| Purpose | Browse GCLID-linked contact/deal attribution |
| Required pagination | ✅ Yes |
| Phase | After gclid_attribution table created |
| Read-only | ✅ Yes |

```
GET /api/gclid-attribution
  ?days=N          (default: 30)
  &campaign=
  &limit=
  &offset=
```

---

## 13. Scheduler Design Recommendation

### Current scheduler responsibilities (Phase 1)

| Scheduler | Frequency | Pulls | DB Writes |
|-----------|----------|-------|-----------|
| Daily | 06:00 daily | campaigns (2d), contacts (2d), search_terms (1d) | runs, leads |
| Weekly | Mon 07:00 | campaigns (30d), search_terms (14d), keywords (30d), geo (30d), contacts (30d), deals | runs, leads, deals, geo, keywords, waste_terms, campaigns |
| Monthly | 1st 08:00 | Same as weekly (30d windows) | Same as weekly |

### Recommended future scheduler design

```
Daily Sync (06:00)
  ├── Pull Windsor last 2–3 days: campaigns, keywords, geo, search_terms
  ├── Pull HubSpot contacts/deals modified recently
  ├── Upsert to DB (source-date grain)
  ├── Update sync_state watermarks
  └── Does NOT run waste detection, campaign truth, or reports

Weekly Analysis (Mon 07:00)
  ├── Reads from DB facts (no fresh API calls unless needed)
  ├── Runs: waste detection, lead quality, campaign truth, action queue
  ├── Generates weekly report
  └── Delivers report

Monthly Analysis (1st 08:00)
  ├── Reads from DB facts
  ├── Generates longer trend/board report
  └── Delivers report
```

**Key principle:** Schedulers orchestrate only. No business logic in schedulers. Analysis modules are called by schedulers but own their own logic.

This separation means:
- Data sync can be run independently of analysis.
- Analysis can be re-run from stored DB facts without re-fetching.
- Backfill can populate the DB without triggering analysis runs.

---

## 14. Risks and Guardrails

### Risks

| Risk | Severity | Notes |
|------|----------|-------|
| Windsor API rate limits | Medium | 3-retry backoff exists; backfill must space requests |
| Windsor historical limits for search terms | High | `last_14d` hard cap confirmed; historical backfill may not be possible |
| HubSpot pagination / timeout on large contact pulls | Medium | Pagination implemented; timeout may need tuning for large accounts |
| Duplicate rows from overlapping daily windows | High | No upsert key currently on campaigns, leads, deals |
| Run snapshots inflating aggregate metrics | High | Confirmed risk; DISTINCT ON mitigates display but not data model |
| Frontend loading too many search terms | High | Requires pagination from day one |
| Mixing Windsor conversions with HubSpot SQLs | Medium | These are different metrics; must not be compared directly |
| Treating missing search terms as zero waste | Medium | If `pull_search_terms()` returns empty, waste detection falls back to keywords |
| Storing PII unnecessarily | Medium | Email not stored (correct); firstname/lastname not stored (correct) |
| Exposing sensitive CRM data | Low | Auth required for all API endpoints |
| Phase 1 accidentally writing to external platforms | Low | No write operations to Google Ads or HubSpot in any current module |

### Guardrails

1. **Backfill scripts must be manual and explicit.** No backfill triggered by schedulers. Require explicit date arguments.
2. **Daily sync must be incremental.** Pull only the recent overlap window (2–3 days). No all-time refetch.
3. **All write operations are local DB only.** No Google Ads writes. No HubSpot writes. Phase 1 read-only is preserved.
4. **Use pagination for large endpoints.** `/api/search-terms`, `/api/gclid-attribution` must not return unlimited rows.
5. **Use watermarks.** `sync_state` must be updated after every successful sync. Never backfill without recording a batch.
6. **Use clear source/date fields.** Raw fact tables must store `source_date` (the date the data pertains to), not just `run_date` (the date the scheduler ran).
7. **Search-term table must not be seeded from analysis output.** The analysis layer reads from `search_terms` to set waste flags; it does not write raw search-term rows.
8. **Do not fetch all-time data every day.** The one-time backfill fills history. The daily sync covers the recent overlap window only.

---

## 15. Recommended PR Sequence After Audit

### PR-ADS-039 — Sync Batch + Watermark Foundation

- Add `sync_batches` table to `db/schema.py`
- Add `sync_state` table to `db/schema.py`
- Add `write_sync_batch()` and `update_sync_state()` to `db/writers.py`
- Add `GET /api/datasets/freshness` endpoint (reads `sync_state`)
- No connector rewrites
- No backfill scripts
- Local DB writes only

### PR-ADS-040 — Search Terms DB Table + Writer + `/api/search-terms`

- Add `search_terms` table to `db/schema.py` (`is_flagged_waste BOOLEAN` nullable, no default)
- Add `write_search_terms()` to `db/writers.py`
- Connect `pull_search_terms()` output to `write_search_terms()` in weekly/daily schedulers
- Add `GET /api/search-terms` with **cursor/keyset pagination** from the first implementation (not offset)
- Waste flag population handled by analysis layer (not writer); raw inserts leave `is_flagged_waste`, `junk_category`, `matched_pattern` as NULL
- No UI page yet

### PR-ADS-041 — Historical Backfill Script (Skeleton)

- `scripts/backfill_windsor.py` — dry-run mode; requires `--from` and `--to`; no external writes
- `scripts/backfill_hubspot.py` — dry-run mode; full pagination; no external writes
- Records each batch in `sync_batches`; updates `sync_state` on success
- Verify Windsor search-term historical range before implementing search-term backfill

### PR-ADS-042 — Daily Incremental Sync Refactor

- Refactor `scheduler/daily.py` to pull only the 2–3 day overlap window
- Upsert to DB by source-date grain (campaigns, keywords, geo, search_terms)
- Update `sync_state` watermarks after each dataset write
- Keep weekly analysis runs separate (do not run waste detection or campaign truth in daily)

### PR-ADS-043 — Search Terms Forensics Page

- Frontend page for browsing all search terms
- Uses paginated `GET /api/search-terms`
- Shows waste flags, spend, match type, campaign
- No writes

### PR-ADS-044 — GCLID Attribution DB Table

- Add `gclid_attribution` table to `db/schema.py`
- Persist `matched_gclid.json` output to DB via `write_gclid_attribution()`
- Add `GET /api/gclid-attribution` endpoint

### PR-ADS-045 — Dataset Freshness UI

- Show per-dataset freshness on the Health / Scheduler page
- Uses `GET /api/datasets/freshness`
- Shows source, dataset, last sync time, status

---

## 16. Non-Goals

This audit explicitly does **not**:

- Make any code changes
- Add any DB tables
- Add any API endpoints
- Modify any schedulers
- Modify any connectors
- Modify any frontend UI
- Fetch live data
- Write to Google Ads
- Write to HubSpot
- Implement backfill scripts
- Implement upsert logic
- Implement watermark tracking
- Implement pagination
- Implement an AI chat feature
- Push negative keywords to Google Ads
- Make any bid, budget, or campaign changes
- Create any OCT uploads

---

## 17. Phase 1 Read-Only Compliance Checklist

- [x] Audit-only
- [x] No code changed
- [x] No schema changed
- [x] No API changed
- [x] No UI changed
- [x] No scheduler changed
- [x] No connector changed
- [x] No live data fetched
- [x] No Google Ads write operations
- [x] No HubSpot write operations
- [x] No OCT upload
- [x] No negative keyword push
- [x] No bid/budget/campaign changes

---

## Important Note

> **Do not design this as "fetch all-time every day."**
>
> Correct model:
> 1. One-time historical backfill (manual, explicit date range, dry-run supported).
> 2. Daily incremental sync with 2–3 day overlap window, watermark updated on success.
> 3. Weekly/monthly analysis from stored DB facts — no fresh API pull unless necessary.
>
> Fetching all-time every day is not acceptable unless the audit proves the dataset is tiny and APIs allow it safely without rate limiting. Neither Windsor campaign/keyword/geo data nor HubSpot contacts/deals currently qualify for all-time daily refetch at scale.

---

*Unblocks: PR-ADS-039 — Sync Batch + Watermark Foundation*
