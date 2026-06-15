# 31 — Scheduler Cutover to Google Ads API (Incremental)

**PR:** PR-ADS-104
**Status:** Complete — incremental scheduled sync cut over
**Depends on:** PR-ADS-103 (canonical source adapter)
**Blocks:** PR-ADS-108 (historical backfill), PR-ADS-109 (Windsor deprecation)

---

## Summary

PR-ADS-104 cuts the **daily, weekly, and monthly incremental scheduled pulls**
from Windsor.ai to the direct Google Ads API via `connectors/google_ads_source.py`.

Windsor.ai is **not removed** — full deprecation is PR-ADS-109.

---

## Scope

| File | Change |
|------|--------|
| `connectors/google_ads_source.py` | Added `save_output()` for local file compatibility |
| `scheduler/daily.py` | Replaced `windsor_pull` with `google_ads_source` |
| `scheduler/weekly.py` | Replaced `windsor_pull` with `google_ads_source` |
| `scheduler/monthly.py` | Replaced `windsor_pull` with `google_ads_source` |
| `tests/test_scheduler_google_ads_cutover.py` | New test suite |
| `docs/31_SCHEDULER_CUTOVER_GOOGLE_ADS_API.md` | This document |

---

## Changes in Detail

### connectors/google_ads_source.py

Added `save_output(campaigns, search_terms, keywords, geos)`.

Writes the four local compatibility files expected by downstream analysis:
- `data/ads_campaigns.json`
- `data/ads_search_terms.json`
- `data/ads_keywords.json`
- `data/ads_geos.json`

**Local file output only. No writes to Google Ads. No database writes.**

### scheduler/daily.py

- `from connectors.windsor_pull import pull_campaign_performance` →
  `from connectors.google_ads_source import pull_campaign_performance`
- `from connectors.windsor_pull import pull_search_terms` →
  `from connectors.google_ads_source import pull_search_terms`
- `source="windsor"` → `source="google_ads_api"` for `search_terms` daily sync batch
- Updated log/comment text to remove Windsor wording

### scheduler/weekly.py

- Windsor import block replaced with `connectors.google_ads_source` equivalents
- `windsor_save(...)` → `google_ads_save(...)`
- `source="windsor"` → `source="google_ads_api"` for:
  - `campaigns`, `search_terms`, `keywords`, `geo`
- Removed Windsor MCP `date_preset=last_60d` comment; Google Ads API honours the
  requested window directly

### scheduler/monthly.py

- Windsor import block replaced with `connectors.google_ads_source` equivalents
- `windsor_save(...)` → `google_ads_save(...)`
- `source="windsor"` → `source="google_ads_api"` for:
  - `campaigns`, `search_terms`, `keywords`, `geo`
- Removed old Windsor last_14d search-term window comment;
  Google Ads API honours `days_back=30` through `google_ads_source`

---

## Non-Goals (Hard Boundaries)

| Non-goal | Reason |
|----------|--------|
| Historical backfill | Deferred to PR-ADS-108 |
| Windsor deletion / env cleanup | Deferred to PR-ADS-109 |
| UI page cutover | Handled in **PR-ADS-105** (see `docs/32_PLATFORM_EVIDENCE_SOURCE_LABEL_CUTOVER.md`) |
| Platform Evidence page rewrite | Out of scope (PR-ADS-105 is a label cutover only, not a rewrite) |
| Lead Intelligence rewrite | Out of scope |
| ROAS math changes | Out of scope |
| GCLID bridge implementation | Out of scope |
| Google Ads writes / mutations | Hard non-goal forever in this module |
| Bid / budget / campaign / keyword changes | Hard non-goal |
| OCT / offline conversion upload | Hard non-goal |
| HubSpot changes | Out of scope |

---

## Validation

```
pytest tests/test_scheduler_google_ads_cutover.py
pytest tests/test_google_ads_source_adapter.py
```

Existing Google Ads bootstrap tests should still pass:
```
pytest tests/test_google_ads_direct_bootstrap.py
pytest tests/test_google_ads_parity_audit.py
```
