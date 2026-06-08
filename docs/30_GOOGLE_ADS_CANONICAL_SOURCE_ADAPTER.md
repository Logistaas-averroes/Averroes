# 30 — Google Ads Canonical Source Adapter

**PR:** PR-ADS-103
**Status:** Read-only adapter — no production cutover
**Module:** `connectors/google_ads_source.py`
**Depends on:** PR-ADS-098 (direct connector), PR-ADS-101 (direct customer mode), PR-ADS-099 (parity audit)
**Blocks:** PR-ADS-104 (scheduler cutover)

---

## Purpose

PR-ADS-098 established a working direct Google Ads API connector.
However its raw row shapes (`campaign_name`, `keyword_text`, `keyword_match_type`, …)
do not match the internal field contracts already consumed by:

- `db.writers`
- `scheduler/weekly.py`
- `analysis/waste_detection`
- `analysis/campaign_truth`
- keyword analysis
- geo / country pages
- search term universe

This PR adds **`connectors/google_ads_source.py`** — a canonical adapter layer
that converts direct Google Ads API rows into the Windsor-compatible internal
shapes.  It exposes the same pull-function names as `windsor_pull.py` but is
powered by the Google Ads API.

**This adapter creates the replacement interface without switching the production
scheduler.**  The scheduler cutover (Windsor → Google Ads) is PR-ADS-104.

---

## No-Write Guarantee

`connectors/google_ads_source.py` is **strictly read-only**.

| Guarantee | Detail |
|-----------|--------|
| No Google Ads writes | No mutate operations, no bid/budget/campaign/keyword changes |
| No negative keyword changes | Entirely absent from this module |
| No OCT / offline conversion upload | Not referenced |
| No database writes | No `db.writers` imports; no insert/update calls |
| No scheduler changes | `scheduler/` files are not modified |
| No Windsor removal | `windsor_pull.py` is not changed or deleted |
| No HubSpot changes | Not referenced |

---

## Public API

### Date helper

```python
get_date_range(days_back: int = 30) -> tuple[str, str]
```

Returns `(start_date, end_date)` ISO strings.  The window is exactly
`days_back` calendar days including today (UTC).

```
end   = datetime.now(timezone.utc).date()
start = end − (days_back − 1)
```

**Windsor parity note:** Windsor used `start = end - timedelta(days=days_back)`
which produced `days_back + 1` calendar days.  This adapter corrects the
off-by-one so `days_back=30` covers exactly 30 days.  For historical
comparisons, use the explicit `_range` functions directly.

---

### days_back pull functions

```python
pull_campaign_performance(days_back: int = 30)  -> list[dict]
pull_search_terms(days_back: int = 60)          -> list[dict]
pull_keyword_performance(days_back: int = 30)   -> list[dict]
pull_geo_performance(days_back: int = 30)       -> list[dict]
```

Each function:
1. Calls `get_date_range(days_back)` to compute `(start, end)`.
2. Delegates to the corresponding `_range` function.

---

### Explicit date-range pull functions

```python
pull_campaign_performance_range(date_from, date_to)  -> list[dict]
pull_search_terms_range(date_from, date_to)          -> list[dict]
pull_keyword_performance_range(date_from, date_to)   -> list[dict]
pull_geo_performance_range(date_from, date_to)       -> list[dict]
```

Each function:
1. Calls the corresponding `fetch_*` function in `connectors.google_ads_direct`.
2. Applies the appropriate `normalize_*` helper to every raw row.
3. For search terms: skips rows where `search_term` is blank or null.
4. Returns the normalised list.

---

## Internal Field Contracts

### Campaign rows

Source: `connectors.google_ads_direct.fetch_campaign_performance`

| Output field | Source field | Rule |
|---|---|---|
| `date` | `date` | pass-through |
| `campaign` | `campaign_name` | rename |
| `campaign_id` | `campaign_id` | cast to `str` |
| `spend` | `spend` | pass-through |
| `clicks` | `clicks` | pass-through |
| `impressions` | `impressions` | pass-through |
| `conversions` | `conversions` | pass-through |
| `conversions_value` | `conversions_value` | pass-through |
| `cpc` | derived | `spend / clicks` if `clicks > 0` else `0` |
| `ctr` | derived | `clicks / impressions` if `impressions > 0` else `0` |
| `conversion_rate` | derived | `conversions / clicks` if `clicks > 0` else `0` |
| `source` | — | `"google_ads_api"` |

**Example row:**
```json
{
    "date": "2026-06-08",
    "campaign": "Gulf",
    "campaign_id": "22546856086",
    "spend": 64.055188,
    "clicks": 31,
    "impressions": 317,
    "conversions": 1.0,
    "conversions_value": 0.0,
    "cpc": 2.066296,
    "ctr": 0.097792,
    "conversion_rate": 0.032258,
    "source": "google_ads_api"
}
```

---

### Search term rows

Source: `connectors.google_ads_direct.fetch_search_terms`

| Output field | Source field | Rule |
|---|---|---|
| `date` | `date` | pass-through |
| `search_term` | `search_term` | **exact name — do not alias** |
| `campaign` | `campaign_name` | rename |
| `campaign_id` | `campaign_id` | cast to `str` |
| `ad_group` | `ad_group_name` | rename |
| `ad_group_id` | `ad_group_id` | cast to `str` |
| `impressions` | `impressions` | pass-through |
| `clicks` | `clicks` | pass-through |
| `spend` | `spend` | pass-through |
| `conversions` | `conversions` | pass-through |
| `source` | — | `"google_ads_api"` |

**Field discipline:** The internal contract requires the field to be named
exactly `search_term`.  Do **not** rename to `query`, `search_query`, or
`search_term_text`.  This discipline was carried over from Windsor.

Rows where `search_term` is blank (`""`, whitespace, or `None`) are **skipped**
and do not appear in the output list.

**Example row:**
```json
{
    "date": "2026-06-08",
    "search_term": "freight forwarding software",
    "campaign": "MENA",
    "campaign_id": "22488997098",
    "ad_group": "Home Page",
    "ad_group_id": "175677406221",
    "impressions": 12,
    "clicks": 1,
    "spend": 0.87,
    "conversions": 0.0,
    "source": "google_ads_api"
}
```

---

### Keyword rows

Source: `connectors.google_ads_direct.fetch_keyword_performance`

| Output field | Source field | Rule |
|---|---|---|
| `date` | `date` | pass-through |
| `campaign` | `campaign_name` | rename |
| `campaign_id` | `campaign_id` | cast to `str` |
| `ad_group` | `ad_group_name` | rename |
| `ad_group_id` | `ad_group_id` | cast to `str` |
| `keyword` | `keyword_text` | rename |
| `match_type` | `keyword_match_type` | normalise to uppercase (`EXACT` / `PHRASE` / `BROAD`) |
| `spend` | `spend` | pass-through |
| `clicks` | `clicks` | pass-through |
| `impressions` | `impressions` | pass-through |
| `conversions` | `conversions` | pass-through |
| `cpc` | derived | `spend / clicks` if `clicks > 0` else `0` |
| `source` | — | `"google_ads_api"` |

**Example row:**
```json
{
    "date": "2026-06-08",
    "campaign": "MENA",
    "campaign_id": "22488997098",
    "ad_group": "Home Page",
    "ad_group_id": "175677406221",
    "keyword": "transport management system",
    "match_type": "BROAD",
    "spend": 1.98858,
    "clicks": 1,
    "impressions": 2,
    "conversions": 0.0,
    "cpc": 1.98858,
    "source": "google_ads_api"
}
```

---

### Geo rows

Source: `connectors.google_ads_direct.fetch_geo_performance`

| Output field | Source field | Rule |
|---|---|---|
| `date` | `date` | pass-through |
| `campaign` | `campaign_name` | rename |
| `campaign_id` | `campaign_id` | cast to `str` |
| `country_criterion_id` | `country_criterion_id` | cast to `str` |
| `country` | — | `None` (see note) |
| `spend` | `spend` | pass-through |
| `clicks` | `clicks` | pass-through |
| `impressions` | `impressions` | pass-through |
| `conversions` | `conversions` | pass-through |
| `source` | — | `"google_ads_api"` |

**Country name mapping:** `country` is `None` because no criterion-ID-to-name
mapping is implemented in this PR.  A lookup table (e.g. mapping `2400` → `"AE"`)
can be added in a future PR.  Country names must not be invented or hard-coded.

**Example row:**
```json
{
    "date": "2026-06-08",
    "campaign": "MENA",
    "campaign_id": "22488997098",
    "country_criterion_id": "2400",
    "country": null,
    "spend": 12.34,
    "clicks": 5,
    "impressions": 100,
    "conversions": 0.0,
    "source": "google_ads_api"
}
```

---

## Internal Normalizer Helpers

The following pure functions perform field mapping and derivation.
They have **no I/O, no API calls, no DB writes**.

| Function | Purpose |
|---|---|
| `safe_divide(numerator, denominator)` | Returns `0.0` when denominator is zero |
| `normalize_campaign_row(row)` | Maps raw campaign row → internal shape |
| `normalize_search_term_row(row)` | Maps raw search term row → internal shape, or `None` if blank |
| `normalize_keyword_row(row)` | Maps raw keyword row → internal shape |
| `normalize_geo_row(row)` | Maps raw geo row → internal shape |

---

## Why This Exists Before Scheduler Cutover

The scheduler (`scheduler/weekly.py`) currently imports Windsor functions
directly.  Switching the scheduler requires confidence that:

1. The Google Ads API returns all the fields the scheduler expects.
2. Row shapes match what `db.writers` and downstream analysis modules consume.
3. Edge cases (blank search terms, zero denominators, missing IDs) are handled
   identically to Windsor.

This adapter satisfies (1) and (2) by providing a validated translation layer.
The scheduler cutover (PR-ADS-104) will simply swap Windsor imports for
`google_ads_source` imports — the field shapes will already match.

---

## How to Replace Windsor Interfaces

When PR-ADS-104 is ready, the scheduler imports will change from:

```python
# Before (Windsor)
from connectors.windsor_pull import (
    pull_campaign_performance,
    pull_search_terms,
    pull_keyword_performance,
    pull_geo_performance,
)
```

to:

```python
# After (Google Ads API)
from connectors.google_ads_source import (
    pull_campaign_performance,
    pull_search_terms,
    pull_keyword_performance,
    pull_geo_performance,
)
```

All downstream consumers see identical field names and types.

---

## Next Steps

| PR | Task |
|---|---|
| **PR-ADS-104** | Scheduler cutover — swap Windsor imports for `google_ads_source` |
| Future | Add criterion-ID → country name lookup table to `normalize_geo_row` |
| Future | Add `conversions_value` to keyword and search term rows if needed by writers |
