# Data Sync Coverage Audit — PR-ADS-070

**Doctrine:** Avverros v1.0  
**Phase:** 1 — Read-Only Intelligence / Roadmap V4.0 Data Foundation  
**Date:** 2026-05-09  
**Audit Scope:** Dataset freshness & sync coverage across all major datasets  
**Depends On:** PR-ADS-069  
**Blocks:** PR-ADS-071 Manual Historical Backfill Framework

---

## Executive Summary

**Audit Verdict: YELLOW**

Data exists in the local warehouse for all major datasets. The UNKNOWN freshness
status is caused by **missing sync_batch tracking** in the weekly and monthly
schedulers — not by real data gaps.

- `windsor/search_terms` and `hubspot/contacts` are **FRESH** because they are
  tracked in the daily scheduler via `start_sync_batch` / `finish_sync_batch`.
- `windsor/campaigns`, `windsor/keywords`, `windsor/geo`, and `hubspot/deals`
  are **UNKNOWN** because their DB writes in the weekly/monthly schedulers are
  **not wrapped in sync_batch tracking**. The data IS written to the DB but
  `sync_state` is never updated.
- `gclid/matches` is **UNKNOWN** if no weekly or monthly run has completed the
  GCLID attribution step, or if weekly runs have not yet been triggered. The
  tracking infrastructure is in place; the dataset becomes FRESH once a weekly
  or monthly run completes successfully.
- `waste_terms` is not a registered freshness dataset (no `sync_state` row).
  It is written on every weekly/monthly run but has no freshness key.

**Top recommended fixes:**

1. Add `start_sync_batch` / `finish_sync_batch` around `write_campaigns`,
   `write_keywords`, `write_geo`, and `write_deals` in both
   `scheduler/weekly.py` and `scheduler/monthly.py`. *(Tiny fix — applied in
   this PR.)*
2. Fix the UI freshness map: `gclid/matches` related page label incorrectly
   points to "Deals" instead of "GCLID Attribution". *(Tiny fix — applied in
   this PR.)*
3. Register `waste_terms` as a known freshness dataset in the API and add
   sync_batch tracking. *(PR-ADS-073 — Dataset-Level Freshness Truth.)*

---

## 1. Dataset Inventory

| Dataset | Source | DB Table | Pulled By | Persisted By | Freshness Key | API Endpoint | UI Page | Current Status | Root Cause |
|---|---|---|---|---|---|---|---|---|---|
| campaigns | Windsor / Google Ads | `campaigns` | `weekly.py` Step 1, `monthly.py` Step 1 | `db.writers.write_campaigns()` | `windsor/campaigns` | `GET /api/campaigns` | Campaigns | UNKNOWN | Persistence write has no sync_batch tracking → sync_state never updated |
| keywords | Windsor / Google Ads | `keywords` | `weekly.py` Step 1, `monthly.py` Step 1 | `db.writers.write_keywords()` | `windsor/keywords` | `GET /api/keywords` | Keywords | UNKNOWN | Same: no sync_batch tracking |
| search_terms | Windsor / Google Ads | `search_terms` | `daily.py` Step 4, `weekly.py` Step 1, `monthly.py` Step 1 | `db.writers.write_search_terms()` | `windsor/search_terms` | `GET /api/search-terms` | Search Terms, N-Grams | FRESH | Working: daily + weekly + monthly all call start/finish_sync_batch |
| geo | Windsor / Google Ads | `geo` | `weekly.py` Step 1, `monthly.py` Step 1 | `db.writers.write_geo()` | `windsor/geo` | `GET /api/geo` | Geo | UNKNOWN | Same: no sync_batch tracking |
| contacts | HubSpot CRM | `leads` | `daily.py` Step 2, `weekly.py` Step 2, `monthly.py` Step 2 | `db.writers.write_leads()` | `hubspot/contacts` | `GET /api/leads` | Lead Quality | FRESH | Working: daily calls start/finish_sync_batch for contacts |
| deals | HubSpot CRM | `deals` | `weekly.py` Step 2, `monthly.py` Step 2 | `db.writers.write_deals()` | `hubspot/deals` | `GET /api/deals` | Deals | UNKNOWN | Same: no sync_batch tracking |
| gclid_attribution | HubSpot+Windsor join | `gclid_attribution`, `gclid_coverage_snapshots` | `weekly.py` Step 5b, `monthly.py` Step 5b | `db.writers.write_gclid_attribution()` | `gclid/matches` | `GET /api/gclid-attribution` | GCLID Attribution | UNKNOWN | Tracking infrastructure exists; UNKNOWN only if weekly/monthly has not completed GCLID step |
| waste_terms | Windsor (derived) | `waste_terms` | `weekly.py` Step 3, `monthly.py` Step 3 | `db.writers.write_waste_terms()` | _(unregistered)_ | Embedded in `/api/search-terms` waste fields | Waste Terms, Search Terms | Not tracked | No freshness key or sync_batch tracking defined anywhere |

---

## 2. Connector Coverage

### `connectors/windsor_pull.py`

| Function | Dataset | Fields Pulled |
|---|---|---|
| `pull_campaign_performance(days_back)` | campaigns | date, campaign, campaign_id, spend, clicks, impressions, conversions, cpc, cpm, ctr, conversion_rate |
| `pull_search_terms(days_back)` | search_terms | date, search_term, campaign, campaign_id, ad_group, keyword, match_type, spend, clicks, impressions, conversions |
| `pull_keyword_performance(days_back)` | keywords | date, campaign, campaign_id, ad_group, keyword, match_type, quality_score, spend, clicks, impressions, conversions, cpc |
| `pull_geo_performance(days_back)` | geo | date, campaign, country, spend, clicks, impressions, conversions |
| `save_output()` | all | Writes JSON files: `data/ads_campaigns.json`, `data/ads_search_terms.json`, `data/ads_keywords.json`, `data/ads_geos.json` |

Note: `pull_campaign_performance` pulls raw Windsor campaign rows. Campaign
truth table (verdicts, CPQL, junk rate) is computed by `analysis/core.py` and
the result is what gets written to the `campaigns` DB table.

### `connectors/hubspot_pull.py`

| Function | Dataset | Notes |
|---|---|---|
| `pull_paid_search_contacts(days_back)` | contacts | Filters `hs_analytics_source = PAID_SEARCH`; returns full contact objects |
| `pull_deals_with_gclid(contacts)` | deals | For each GCLID-bearing contact, fetches associated HubSpot deals via REST API |
| `get_lead_quality_summary(contacts)` | (derived) | In-memory aggregate; written to `data/crm_summary.json` only |
| `save_output()` | contacts, deals | Writes `data/crm_contacts.json`, `data/crm_deals.json`, `data/crm_summary.json` |

### `connectors/gclid_match.py`

| Function | Dataset | Notes |
|---|---|---|
| `run_gclid_match()` | gclid_attribution | Joins `data/ads_search_terms.json` + `data/crm_contacts.json` + `data/crm_deals.json`; matches via `hs_google_click_id` with URL fallback |
| `save_output()` | gclid_attribution | Writes `data/matched_gclid.json`, `data/gclid_coverage.json` |

---

## 3. Scheduler Coverage

### `scheduler/daily.py` — Runs 6 AM GMT every day

| Step | Dataset | Connector | DB Write | Sync Batch Tracked? |
|---|---|---|---|---|
| Step 1 | campaigns (raw) | `pull_campaign_performance(days_back=2)` | None — raw campaigns not persisted in daily | No |
| Step 2 | contacts | `pull_paid_search_contacts(days_back=2)` | `write_leads()` | **YES** — `start_sync_batch(hubspot, contacts)` / `finish_sync_batch` |
| Step 4 | search_terms | `pull_search_terms(days_back=1)` | `write_search_terms()` | **YES** — `start_sync_batch(windsor, search_terms)` / `finish_sync_batch` |

Daily notable gaps:
- Raw campaign rows from `pull_campaign_performance` are used for anomaly
  detection but NOT written to the DB. Campaign rows in the DB come only from
  the campaign truth table computed during weekly/monthly.
- Keywords, geo, and deals are not pulled in the daily pulse.

### `scheduler/weekly.py` — Runs every Monday 7 AM GMT

| Step | Dataset | Connector | DB Write | Sync Batch Tracked? |
|---|---|---|---|---|
| Step 1 | campaigns, search_terms, keywords, geo | Windsor pull | `write_keywords()`, `write_geo()`, `write_search_terms()` (all), no write for raw campaigns | search_terms **YES**; campaigns/keywords/geo **NO** |
| Step 2 | contacts, deals | HubSpot pull | `write_leads()`, `write_deals()` | contacts: via daily only; deals **NO** |
| Step 3 | waste_terms (derived) | `run_waste_detection()` | `write_waste_terms()` | **NO** |
| Step 5 | campaigns (truth table) | `run_campaign_truth()` | `write_campaigns()` | **NO** |
| Step 5b | gclid_attribution | `run_gclid_match()` | `write_gclid_attribution()`, `write_gclid_coverage_snapshot()` | **YES** — `start_sync_batch(gclid, matches)` / `finish_sync_batch` |

### `scheduler/monthly.py` — Runs 1st of each month 7 AM GMT

Same dataset coverage as weekly. Same tracking gaps: campaigns, keywords, geo,
and deals have no sync_batch tracking. search_terms and gclid/matches do.

---

## 4. DB Persistence Coverage

| DB Table | Writer Function | Write Strategy | Sync Batch Tracked? |
|---|---|---|---|
| `runs` | `write_run()` / `update_run()` | INSERT (one row per run); UPDATE on finish | N/A — internal |
| `campaigns` | `write_campaigns()` | INSERT per run (not upsert) | **NO** |
| `leads` | `write_leads()` | INSERT per run (not upsert) | **NO** for weekly/monthly write itself; contacts batch IS tracked in daily |
| `waste_terms` | `write_waste_terms()` | INSERT per run | **NO** |
| `deals` | `write_deals()` | INSERT per run (not upsert) | **NO** |
| `geo` | `write_geo()` | DELETE+INSERT per run_id (idempotent) | **NO** |
| `keywords` | `write_keywords()` | DELETE+INSERT per run_id (idempotent) | **NO** |
| `search_terms` | `write_search_terms()` | UPSERT on natural key (idempotent) | **YES** — both daily and weekly/monthly |
| `gclid_attribution` | `write_gclid_attribution()` | UPSERT on `attribution_key` (idempotent) | **YES** — weekly/monthly |
| `gclid_coverage_snapshots` | `write_gclid_coverage_snapshot()` | INSERT per run | **YES** (shares batch with gclid_attribution) |
| `sync_batches` | `start_sync_batch()` / `finish_sync_batch()` | INSERT on start; UPDATE on finish | N/A — tracking table |
| `sync_state` | `finish_sync_batch()` / `update_sync_state()` | UPSERT on (source, dataset) | N/A — freshness table |

---

## 5. Freshness Tracking Coverage

The freshness system works as follows:

1. Scheduler calls `start_sync_batch(source, dataset, sync_type, date_from, date_to, run_id)` → inserts a row in `sync_batches` with `status='running'`.
2. After successful write, scheduler calls `finish_sync_batch(batch_id, status='success', row_count, last_source_date)` → updates the `sync_batches` row AND upserts a row in `sync_state`.
3. `GET /api/datasets/freshness` reads `sync_state` and merges with `_KNOWN_DATASETS` list; missing entries are returned as `status='unknown'`.

**Datasets with sync tracking:** `windsor/search_terms`, `hubspot/contacts`, `gclid/matches`

**Datasets WITHOUT sync tracking (gap identified in this audit):**

| Dataset Key | Root Cause |
|---|---|
| `windsor/campaigns` | `write_campaigns()` has no surrounding `start_sync_batch`/`finish_sync_batch` |
| `windsor/keywords` | `write_keywords()` has no surrounding `start_sync_batch`/`finish_sync_batch` |
| `windsor/geo` | `write_geo()` has no surrounding `start_sync_batch`/`finish_sync_batch` |
| `hubspot/deals` | `write_deals()` has no surrounding `start_sync_batch`/`finish_sync_batch` |
| `waste_terms` | No freshness key defined; no tracking infrastructure at all |

---

## 6. API Endpoint Coverage

| Endpoint | Source Table | Returns Data When Freshness is UNKNOWN? |
|---|---|---|
| `GET /api/campaigns` | `campaigns` | **YES** — reads from `campaigns` table regardless of sync_state |
| `GET /api/keywords` | `keywords` | **YES** — reads from `keywords` table regardless of sync_state |
| `GET /api/geo` | `geo` | **YES** — reads from `geo` table regardless of sync_state |
| `GET /api/leads` | `leads` | **YES** — reads from `leads` table regardless of sync_state |
| `GET /api/deals` | `deals` | **YES** — reads from `deals` table regardless of sync_state |
| `GET /api/search-terms` | `search_terms` | YES — and freshness tracked |
| `GET /api/gclid-attribution` | `gclid_attribution` | **YES** — reads from `gclid_attribution` table regardless of sync_state |
| `GET /api/datasets/freshness` | `sync_state` + `_KNOWN_DATASETS` | Returns UNKNOWN for untracked datasets |

**Key finding:** All data endpoints read directly from DB tables. An UNKNOWN
freshness status does NOT mean the page shows no data — it means the system
cannot tell the operator *when* that data was last synced. Pages may show real
data while freshness reports UNKNOWN, creating a trust mismatch.

---

## 7. UI Page Mapping

The `datasetRelatedPage()` function in `static/app.js` maps freshness rows to
UI navigation:

| Freshness Key | Related Page (Before Fix) | Related Page (After Fix) | Correct? |
|---|---|---|---|
| `windsor/search_terms` | Search Terms | Search Terms | ✓ |
| `windsor/keywords` | Keywords | Keywords | ✓ |
| `windsor/geo` | Geo | Geo | ✓ |
| `windsor/campaigns` | Campaigns | Campaigns | ✓ |
| `hubspot/contacts` | Lead Quality | Lead Quality | ✓ |
| `hubspot/deals` | Deals | Deals | ✓ |
| `gclid/matches` | **Deals** ❌ | **GCLID Attribution** ✓ | Fixed in this PR |

The `gclid/matches` dataset's related page was mapped to "Deals" (page key:
`deals`) but the correct page is "GCLID Attribution" (page key:
`gclid-attribution`). This is a label/mapping bug with no data impact.

### Per-page freshness strip

The per-page freshness strip (rendered by `renderRunMeta()`) reads from the
latest run record, not from `sync_state`. This means it shows run-level
freshness (last scheduler run), not dataset-level freshness. Pages using this
strip cannot distinguish between "a run happened recently but campaigns weren't
part of it" vs "campaigns data is fresh."

---

## 8. Root Cause of UNKNOWN Datasets

| Dataset | Data Exists in DB? | Freshness Tracked? | Root Cause Classification |
|---|---|---|---|
| `windsor/campaigns` | **YES** — written by weekly/monthly `write_campaigns()` | **NO** — no sync_batch tracking | **B: Data exists but freshness metadata missing** |
| `windsor/keywords` | **YES** — written by weekly/monthly `write_keywords()` | **NO** — no sync_batch tracking | **B: Data exists but freshness metadata missing** |
| `windsor/geo` | **YES** — written by weekly/monthly `write_geo()` | **NO** — no sync_batch tracking | **B: Data exists but freshness metadata missing** |
| `hubspot/deals` | **YES** — written by weekly/monthly `write_deals()` | **NO** — no sync_batch tracking | **B: Data exists but freshness metadata missing** |
| `gclid/matches` | Depends on whether weekly/monthly ran | **Conditional** — tracking exists but requires a successful weekly/monthly GCLID step | **B/E: Data and tracking exist but GCLID step may not have run yet** |
| `waste_terms` | **YES** — written by weekly/monthly `write_waste_terms()` | **NO** — no freshness key defined | **E: Not currently supported as a tracked freshness dataset** |

**Overall verdict: YELLOW.** No core data loss. The freshness metadata layer is
incomplete for 4 of 7 tracked datasets. The fix is additive sync_batch
instrumentation — no schema change required.

---

## 9. Minimal Fix Recommendations

The following tiny fixes are implemented in this PR (within audit scope):

### Fix 1 — `static/app.js`: Correct `gclid/matches` related page mapping
**File:** `static/app.js`, function `datasetRelatedPage()`  
**Change:** `"gclid/matches": { page: "deals", label: "Deals" }` →
`"gclid/matches": { page: "gclid-attribution", label: "GCLID Attribution" }`  
**Risk:** Zero — pure label/navigation mapping fix.

### Fix 2 — `scheduler/weekly.py`: Add sync_batch tracking for campaigns, keywords, geo, deals
**Files:** `scheduler/weekly.py`, `scheduler/monthly.py`  
**Change:** Wrap `write_campaigns()`, `write_keywords()`, `write_geo()`, and
`write_deals()` calls in `start_sync_batch()` / `finish_sync_batch()` pairs,
following the same pattern already used for `search_terms` and
`gclid/matches` in the same files.  
**Risk:** Low — additive only; does not alter data write behaviour. Matching
the pattern already used in 3 other datasets in the same files.

### Larger fixes deferred to future PRs:

- **PR-ADS-073 — Dataset-Level Freshness Truth:** Register `waste_terms` as a
  known freshness dataset; add tracking; review per-page freshness strip to use
  dataset-level data rather than run-level data.

---

## 10. Backfill / Daily Sync Implications

### PR-ADS-071 — Manual Historical Backfill Framework

| Question | Finding |
|---|---|
| Which datasets need historical backfill? | `campaigns`, `keywords`, `geo`, `deals`, `gclid_attribution`, `search_terms` (limited to Windsor 14-day window) |
| Which source functions already support date ranges? | `pull_campaign_performance(days_back)`, `pull_keyword_performance(days_back)`, `pull_geo_performance(days_back)`, `pull_paid_search_contacts(days_back)` — all accept `days_back` |
| Which need date-range support? | `pull_search_terms()` — currently maps any `days_back > 7` to the Windsor `last_14d` preset; cannot pull arbitrary ranges |
| Which tables can safely upsert historical rows? | `search_terms` (has unique natural key), `gclid_attribution` (has `attribution_key` unique constraint) |
| Which tables use INSERT (not upsert)? | `campaigns`, `leads`, `waste_terms`, `deals`, `geo`, `keywords` — need run-scoped partitioning or dedup strategy before backfill |
| Which freshness keys must update after backfill? | All that are backfilled; `sync_type='backfill'` is already a valid value in `VALID_SYNC_TYPES` |
| Active or all campaigns? | Doctrine: pull all campaigns, active and inactive — historical failures are useful intelligence |

### PR-ADS-072 — Daily Incremental Sync at 9 AM

| Question | Finding |
|---|---|
| Which datasets should sync daily? | `campaigns` (raw Windsor, 2-day rolling), `keywords` (rolling), `geo` (rolling), `deals` (rolling), `search_terms` (already done), `contacts` (already done) |
| What rolling window? | 2-day window for daily anomaly detection; 14-day for search terms (Windsor preset limit) |
| Which freshness keys should update? | All synced datasets; use same `start_sync_batch`/`finish_sync_batch` pattern |
| How to prevent overwrite/data loss? | Use upsert for `search_terms` and `gclid_attribution`; for `campaigns`/`keywords`/`geo`/`deals` use DELETE+INSERT scoped to run_id (already the pattern for keywords/geo) |
| How to record sync batches? | Existing `sync_batches` + `sync_state` infrastructure is sufficient; just add tracking calls |

---

## 11. Final Verdict

**YELLOW — data exists but freshness metadata/mapping is incomplete.**

- All 7 known datasets have DB persistence paths.
- 4 datasets (`campaigns`, `keywords`, `geo`, `deals`) are missing sync_batch
  tracking; their data is fresh but the system cannot prove it.
- 1 dataset (`gclid/matches`) has tracking infrastructure but requires a
  completed weekly/monthly run to become FRESH.
- 1 dataset (`waste_terms`) has no freshness tracking defined at all.
- 1 UI mapping bug: `gclid/matches` was linked to the Deals page instead of
  the GCLID Attribution page.

Tiny fixes applied in this PR close the sync_batch tracking gap for 4 datasets
and correct the UI mapping. After these fixes are deployed and the next
weekly/monthly run completes, the System Health → Dataset Freshness page should
show 6–7 datasets as FRESH (or stale if they have not run recently).

---

## Appendix A — Validation Grep Results

### sync_state / sync_batches tracking calls in scheduler files

```
scheduler/daily.py:
  hubspot/contacts: start_sync_batch + finish_sync_batch ✓
  windsor/search_terms: start_sync_batch + finish_sync_batch ✓

scheduler/weekly.py:
  windsor/search_terms: start_sync_batch + finish_sync_batch ✓
  gclid/matches: start_sync_batch + finish_sync_batch ✓
  windsor/campaigns: MISSING (write_campaigns() not wrapped)
  windsor/keywords: MISSING (write_keywords() not wrapped)
  windsor/geo: MISSING (write_geo() not wrapped)
  hubspot/deals: MISSING (write_deals() not wrapped)

scheduler/monthly.py:
  windsor/search_terms: start_sync_batch + finish_sync_batch ✓
  gclid/matches: start_sync_batch + finish_sync_batch ✓
  windsor/campaigns: MISSING
  windsor/keywords: MISSING
  windsor/geo: MISSING
  hubspot/deals: MISSING
```

### No unsafe external write paths

No active Google Ads writes, HubSpot writes, negative keyword push, bid/budget
mutations, OCT uploads, or conversion uploads were found in any connector,
scheduler, API, or script file.

---

## Appendix B — Doctrine Checklist

- [x] Phase 1 remains read-only
- [x] Six-month governance lock respected
- [x] No Google Ads writes added
- [x] No HubSpot writes added
- [x] No negative keyword push added
- [x] No OCT upload added
- [x] No campaign pause/bid/budget mutation added
- [x] No scheduler timing changed
- [x] No backfill built yet
- [x] No daily sync job built yet
- [x] Dataset coverage audited
- [x] Freshness tracking audited
- [x] Unknown datasets classified
- [x] V4 backfill requirements documented (Section 10)
- [x] V4 daily sync requirements documented (Section 10)
