# 32 — Platform Evidence & Admin Source Label Cutover

**PR:** PR-ADS-105
**Status:** Complete — visible app layer + freshness/source mapping cut over
**Depends on:** PR-ADS-104 (scheduler cutover to Google Ads API)
**Blocks:** PR-ADS-106 (Lead Intelligence rebuild)

---

## Summary

PR-ADS-104 cut the daily/weekly/monthly scheduled Google Ads pulls from
Windsor.ai to the direct Google Ads API. Sync batches for the ad-platform
datasets now write `source="google_ads_api"`.

PR-ADS-105 updates the **visible app layer** and the **freshness/source
mapping** so the system truth is consistent:

- **Google Ads API** = active platform-evidence source.
- **Windsor** = legacy/deprecated fallback only — never shown as the current
  production source for ad-platform datasets.

This is a **label + mapping cutover only**. No scheduler, connector, or
DB-writer behaviour changes; no Google Ads writes/mutations.

---

## Pages & Surfaces Covered

**Platform Evidence**
- Campaigns
- Search Terms (+ Patterns tab)
- Keywords
- Countries / Geo

**Admin support surfaces**
- Data Runs (source labels)
- System Status (source health, pipeline map, dataset freshness table)
- Page Guide / Help Drawer copy
- Empty states
- Freshness / source labels

---

## Changes in Detail

### Backend source/freshness mapping

| File | Change |
|------|--------|
| `services/freshness_service.py` | `DATASET_FRESHNESS_CONFIG` source for `campaigns`, `search_terms`, `keywords`, `geo` → `google_ads_api` |
| `services/system_status_service.py` | `PIPELINE_DEPENDENCIES` source → `google_ads_api`; `SOURCE_DEFINITIONS` key `windsor` → `google_ads_api` (label **Google Ads API**); next-action wording updated |
| `api/server.py` | `_KNOWN_DATASETS` placeholders → `google_ads_api/*`; war-room source key list → `google_ads_api`; search-term `data_quality.source` → `google_ads_api` |

The mapping change is required for correctness: PR-ADS-104 writes
`sync_state` / `sync_batches` rows with `source="google_ads_api"`, so the
read side must look up the same `(source, dataset)` pair.

### Frontend (`static/app.js`, `static/index.html`)

- `PAGE_DATASET_MAP`, `PAGE_DEPENDENCIES`, `_datasetDisplayName`,
  `datasetRelatedPage` dataset keys `windsor/*` → `google_ads_api/*`.
  **Route keys are unchanged** (`campaigns`, `search-terms`, `keywords`,
  `geo`).
- `_sourceDisplayName` Data Runs / freshness mapping:
  - `google_ads_api` → **Google Ads API**
  - `hubspot` → **HubSpot**
  - `gclid` → **GCLID Attribution**
  - `windsor` → **Windsor legacy**
- Page Guide / Help Drawer copy for Campaigns / Search Terms / Keywords /
  Countries / Data Runs / System Status updated to name the Google Ads API as
  the active source.
- Patterns copy: "Patterns are calculated from Google Ads API search-term
  evidence." The merged Search Terms + Patterns tab model is unchanged; the
  `ngrams` route alias still resolves to `search-terms` with the Patterns tab
  active.
- Empty states no longer blame Windsor for active ad datasets:
  "No Google Ads API data has been synced yet. Run the scheduler or check
  System Status."

---

## Route Stability

Route keys are used across `data-page`, `PAGE_DATASET_MAP`, URL routing,
tests, and backend freshness mapping. They are **not** renamed:

```
campaigns   search-terms   keywords   geo
```

`ngrams` (Patterns) and `countries` remain hash-route aliases of
`search-terms` and `geo` respectively.

---

## Non-Goals (Hard Boundaries)

| Non-goal | Reason |
|----------|--------|
| Scheduler changes | Landed in PR-ADS-104 |
| Connector changes | Out of scope |
| DB schema / DB writes | Out of scope |
| Google Ads writes / mutations | Hard non-goal forever |
| HubSpot changes | Out of scope |
| Lead Intelligence rewrite | PR-ADS-106 |
| ROAS math changes | Out of scope (prose labels only) |
| GCLID bridge implementation | Out of scope |
| Historical backfill | PR-ADS-108 |
| Windsor deletion / Render env cleanup | PR-ADS-109 |

The legacy Admin Backfill page and GCLID readiness audit retain Windsor
wording as legacy diagnostics — they are out of scope for this cutover.

---

## Validation

```
pytest tests/test_platform_evidence_google_ads_api_cutover.py
pytest tests/test_dataset_freshness_endpoint.py
pytest tests/test_dataset_freshness_ui_contract.py
pytest tests/test_system_status_truth.py
pytest tests/test_system_status_war_room.py
pytest tests/test_sidebar_navigation_structure.py
pytest tests/test_empty_state_page_explanations.py
```
