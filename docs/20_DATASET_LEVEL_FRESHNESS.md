# Dataset-Level Freshness

**PR-ADS-074 — Dataset-Level Freshness Truth Across Dashboard Pages**
Phase 1 · Read-Only Intelligence · Roadmap V4.0 Data Foundation

---

## Purpose

Every major dashboard page now shows the freshness of the specific dataset that
powers it, rather than relying on a single global "latest run" status.

The global freshness bar tells you when the most recent scheduler job finished.
Dataset-level freshness tells you whether the table powering *this particular
page* has been successfully synced recently.

---

## Why Latest-Run Freshness Is Not Enough

A successful daily run does **not** guarantee every dashboard page is fresh.
Each scheduler job writes to different tables:

| Run type | Tables written |
|----------|---------------|
| Daily    | runs, leads (hubspot/contacts), search_terms (windsor/search_terms) |
| Weekly   | campaigns, keywords, geo, deals |
| Monthly  | same as weekly plus report generation |

A page can be showing data from a table that was last written by a *weekly* run
even if the *daily* run succeeded today.  Displaying only the latest-run
timestamp creates a false impression of freshness.

**Example:**  The Campaigns page shows campaign performance from the `campaigns`
table.  If the last *weekly* run was 9 days ago but the daily run succeeded
this morning, a global "run completed today" banner is technically correct but
operationally misleading — campaign data is stale.

---

## Dataset Key Mapping

Dataset keys are `"source/dataset"` strings that match the `sync_state` table
(written by `db.writers.upsert_sync_state`).

| Dataset Key | Source table / connector | Written by |
|---|---|---|
| `windsor/campaigns` | `campaigns` | weekly / monthly scheduler |
| `windsor/keywords` | `keywords` | weekly / monthly scheduler |
| `windsor/search_terms` | `search_terms` | daily scheduler |
| `windsor/geo` | `geo` | weekly / monthly scheduler |
| `hubspot/contacts` | `leads` | daily scheduler |
| `hubspot/deals` | `deals` | weekly / monthly scheduler |
| `gclid/matches` | GCLID attribution | weekly / monthly scheduler |

---

## Page Mapping

The `PAGE_DATASET_MAP` constant in `static/app.js` maps each page's `sectionKey`
to the dataset(s) that power it.

| Page | sectionKey | Dataset(s) |
|---|---|---|
| Campaigns | `campaigns` | `windsor/campaigns` |
| Waste Terms | `waste` | `windsor/search_terms` *(derived)* |
| Search Terms | `search_terms` | `windsor/search_terms` |
| N-Grams | `ngrams` | `windsor/search_terms` *(derived)* |
| Geo | `geo` | `windsor/geo` |
| Keywords | `keywords` | `windsor/keywords` |
| Lead Quality / Leads | `lead_quality` | `hubspot/contacts` |
| Deals | `deals` | `hubspot/deals` |
| GCLID Attribution | `gclid_attribution` | `gclid/matches` |
| In-Progress Leads | `in_progress_leads` | `hubspot/contacts` |
| Action Queue | `action_queue` | `windsor/campaigns`, `hubspot/contacts`, `hubspot/deals` |
| Reports | `reports` | `windsor/campaigns`, `hubspot/contacts`, `hubspot/deals`, `windsor/search_terms` |

Pages without a mapping in `PAGE_DATASET_MAP` (Scheduler, System Health) fall
back to the run-based freshness strip via `renderRunMeta()`.

---

## Derived Datasets

Some pages compute their output from source datasets but do not have their own
tracked sync entry in `sync_state`.

| Page | Derived from | Display copy prefix |
|---|---|---|
| N-Grams | `windsor/search_terms` | `Derived from search terms` |
| Waste Terms | `windsor/search_terms` | `Derived from search terms` |

These pages use the freshness of the source dataset.  The strip copy uses
`"Derived from search terms …"` instead of `"Dataset freshness: …"` to be
honest that the *output* of this page is computed from (not directly stored as)
the synced data.

`waste_terms` is not a registered freshness dataset in `sync_state`.  The waste
analysis output is computed in memory from `windsor/search_terms` rows plus
junk-pattern rules.  Therefore the Waste page's freshness strip shows the
freshness of `windsor/search_terms` with the derived-dataset prefix.

---

## Status Meanings

Status labels are consistent between the per-page freshness strips and the
System Health → Dataset Freshness table.

| Label | Meaning |
|---|---|
| **Fresh** | `sync_state.status = 'success'` and `last_successful_sync_at` is within the stale threshold (default 2 days). |
| **Stale** | `status = 'success'` but the last successful sync is older than the stale threshold.  Display-only; does not change backend status. |
| **Failed** | `status = 'failed'`.  The most recent tracked sync attempt failed.  Previous watermarks are preserved. |
| **Running** | `status = 'running'`.  A sync is currently in progress. |
| **Unknown** | No row in `sync_state` for this dataset yet, or `status` is `null`/unrecognised.  This does **not** mean the source failed — it means no successful sync has been tracked yet. |

**Important:** `Unknown` means *freshness is unverified*, not that the data is
necessarily missing or incorrect.  Data written before `sync_state` tracking
began will show as `Unknown`.

---

## API Contract

`GET /api/datasets/freshness` — PR-ADS-039, hardened in PR-ADS-074.

Auth required.  Read-only.  No live fetch, no sync execution, no external calls.

Response shape (additive field `dataset_key` added in PR-ADS-074):

```json
{
  "datasets": [
    {
      "dataset_key":             "windsor/campaigns",
      "source":                  "windsor",
      "dataset":                 "campaigns",
      "status":                  "success",
      "last_successful_sync_at": "2024-01-15T09:04:00Z",
      "last_source_date":        "2024-01-14",
      "last_batch_id":           42,
      "error_message":           null,
      "updated_at":              "2024-01-15T09:04:05Z"
    },
    ...
  ],
  "summary": {
    "total":   7,
    "success": 5,
    "failed":  1,
    "running": 0,
    "unknown": 1
  },
  "db_unavailable": false
}
```

The `dataset_key` field is a convenience alias for `"${source}/${dataset}"`.
It is additive and does not replace or rename any existing field.

---

## Frontend Implementation

`static/app.js` — key symbols:

| Symbol | Purpose |
|---|---|
| `PAGE_DATASET_MAP` | Constant: sectionKey → dataset key(s) |
| `DERIVED_DATASET_PAGES` | Set of sectionKeys whose output is derived from a source dataset |
| `_datasetFreshnessByKey` | Runtime cache: dataset key → freshness row |
| `loadDatasetFreshness()` | Fetches `/api/datasets/freshness`, populates `_datasetFreshnessByKey` and the System Health table |
| `renderPageDatasetFreshness(sectionKey)` | Renders the per-page freshness strip into `#run-meta-{sectionKey}` |
| `renderRunMeta(sectionKey)` | Legacy run-based strip (still used for Scheduler and System Health pages) |

`loadDatasetFreshness()` is now called at app startup (alongside
`loadDataFreshness()`) so that the cache is populated before the user navigates
to any data page.

---

## Known Limitations

1. **Race on first page load**: if the user navigates to a data page before
   `loadDatasetFreshness()` completes (very fast navigation), the strip falls
   back to the run-based `renderRunMeta()` copy.  This is safe — it is never
   false-fresh, only less specific.

2. **No gclid/matches write tracking before PR-ADS-073**: GCLID Attribution
   may show `Unknown` on instances where the backfill ran before sync-state
   tracking was enabled.

3. **waste_terms not a sync_state dataset**: Waste page freshness is derived
   from `windsor/search_terms`.  If search-term sync is fresh but the waste
   analysis has not re-run, the strip still shows `search_terms` freshness.
   This is the best available signal — waste analysis does not write
   `sync_state`.

4. **Stale threshold is display-only**: The `Stale` badge is applied by the
   frontend when `last_successful_sync_at` is older than `_staleAfterDays`
   (default 2, configurable via `/api/config/ui-thresholds`).  It does not
   change the backend `sync_state.status` value.

---

## Validation

```bash
# Run validation and tests
make validate
pytest tests/test_dataset_freshness_ui_contract.py -v

# Check freshness endpoint (requires running server)
curl -b "ads_session=<token>" http://localhost:8000/api/datasets/freshness | python -m json.tool

# Unsafe wording grep
grep -R "push negative\|apply negative\|pause campaign\|block term\|send to Google Ads\|send to HubSpot\|upload conversion\|change bid\|change budget" static api docs tests --include="*.py" --include="*.js" --include="*.html" --include="*.md"
# Expected: only safe governance disclaimers / forbidden-action examples

# Mutation grep
grep -R "POST\|PUT\|PATCH\|DELETE" api static scripts scheduler analysis connectors db | grep -i "google\|hubspot\|negative\|conversion\|budget\|bid\|freshness"
# Expected: no new mutation endpoint for freshness; no external write path
```

Manual UI checks:

- [ ] Campaigns page shows `windsor/campaigns` freshness
- [ ] Keywords page shows `windsor/keywords` freshness
- [ ] Geo page shows `windsor/geo` freshness
- [ ] Search Terms page shows `windsor/search_terms` freshness
- [ ] N-Grams page shows "Derived from search terms" freshness
- [ ] Waste page shows "Derived from search terms" freshness
- [ ] Lead Quality page shows `hubspot/contacts` freshness
- [ ] Deals page shows `hubspot/deals` freshness
- [ ] GCLID page shows `gclid/matches` freshness
- [ ] Action Queue page shows multi-dataset summary
- [ ] Unknown datasets say "freshness is unverified", not "failed"
- [ ] Failed datasets show failure state with "Check System Health" link
- [ ] System Health Dataset Freshness table uses same status labels (Fresh / Stale / Failed / Running / Unknown)
- [ ] No page implies Google Ads or HubSpot modification
