# 29 — Google Ads vs Windsor Parity Audit

**PR:** PR-ADS-099
**Status:** Read-only audit — no production cutover
**Scope:** Compare direct Google Ads API data against Windsor/current source

## Overview

This document describes the parity audit process for evaluating whether the
direct Google Ads API connector (PR-ADS-098) can fully replace Windsor as the
production data source for campaigns, search terms, keywords, and geo data.

**Important:** This audit does NOT switch production data sources. It produces
a comparison report only. There is no production source switch in this PR.

## How to Run

```bash
# Full audit (default: 7d, 30d, 60d windows; all datasets)
python scripts/google_ads_parity_audit.py

# Custom windows
python scripts/google_ads_parity_audit.py --windows 7,30

# Specific datasets
python scripts/google_ads_parity_audit.py --datasets campaigns,keywords

# Combined
python scripts/google_ads_parity_audit.py --windows 7,30,60 --datasets campaigns,search_terms,keywords,geo
```

### Prerequisites

- `GOOGLE_ADS_DEVELOPER_TOKEN` — Google Ads API developer token
- `GOOGLE_ADS_CLIENT_ID` — OAuth2 client ID
- `GOOGLE_ADS_CLIENT_SECRET` — OAuth2 client secret
- `GOOGLE_ADS_REFRESH_TOKEN` — OAuth2 refresh token
- `GOOGLE_ADS_CUSTOMER_ID` — Google Ads customer ID
- `WINDSOR_API_KEY` — Windsor.ai API key
- `WINDSOR_ACCOUNT_ID` — Windsor account ID

## Interpreting Results

### Status Values

| Status | Meaning |
|--------|---------|
| **PASS** | Spend delta ≤ 3% AND click/impression delta ≤ 5%. Sources are in agreement. |
| **WARNING** | Spend delta ≤ 10% OR row-count differs materially but spend is close. Review before cutover. |
| **FAIL** | Spend/click/impression deltas are large or source errors occurred. Do NOT cut over. |
| **NOT_AVAILABLE** | Windsor/current source returned no data for comparison. Cannot assess parity. |

### Thresholds

- **PASS**: spend delta ≤ 3% AND click/impression delta ≤ 5%
- **WARNING**: spend delta ≤ 10% OR row-count differs materially but spend is close
- **FAIL**: spend/click/impression deltas > 10% or source errors
- **NOT_AVAILABLE**: Windsor/current source is missing or returned zero rows

## Known Expected Differences

### Search Term Row Counts

The direct Google Ads API typically returns **more rows** than Windsor for search
terms. This is expected behavior:

- Google Ads API returns all search terms that triggered ads
- Windsor may aggregate or filter low-impression terms
- Row count differences of 50-100% are normal for search terms
- **Do not fail solely because search term row count differs** if spend/clicks/impressions are close

### Geo Data

- Google Ads API uses `geographic_view` with `country_criterion_id`
- Windsor uses country name strings
- Geo parity may show NOT_AVAILABLE if geographic_view is restricted

### Data Freshness

- Google Ads API reflects data as of the latest available date (usually yesterday)
- Windsor data may lag by 24-48 hours depending on sync schedule
- This can cause small metric differences for recent dates

### Conversion Attribution Windows

- Google Ads API and Windsor may use different attribution windows
- Small conversion differences (< 5%) are expected due to attribution model timing

## Recommendation Criteria for Cutting Windsor

The following criteria should ALL be met before cutting Windsor:

1. **Campaigns**: PASS across all windows (7d, 30d, 60d)
2. **Search Terms**: PASS or WARNING (row count differs but spend is close)
3. **Keywords**: PASS across all windows
4. **Geo**: PASS or NOT_AVAILABLE (acceptable if geo_view is restricted)
5. **No FAIL status** for any dataset/window combination
6. **Consistent results** across at least 3 consecutive audit runs on different days

## Next Migration PR Plan

After parity is confirmed:

1. **PR-ADS-100**: Switch production source from Windsor to Google Ads direct
   - Update scheduler to use `google_ads_direct` connector
   - Implement fallback logic (revert to Windsor if direct API fails)
   - Add monitoring alerts for data freshness from new source
   - Keep Windsor credentials active as backup

2. **PR-ADS-101+**: Windsor deprecation
   - Remove Windsor dependency after 30-day parallel run
   - Archive Windsor connector code
   - Update documentation

## Architecture

```
scripts/google_ads_parity_audit.py
├── fetch_google_ads_data()    → connectors/google_ads_direct.py
│   ├── fetch_campaign_performance()
│   ├── fetch_search_terms()
│   ├── fetch_keyword_performance()
│   └── fetch_geo_performance()
├── fetch_windsor_data()       → connectors/windsor_pull.py
│   ├── pull_campaign_performance()
│   ├── pull_search_terms()
│   ├── pull_keyword_performance()
│   └── pull_geo_performance()
├── compare_dataset()          → Computes deltas and classifies status
├── format_report()            → Human-readable output
└── run_audit()                → Orchestrates full audit
```

## Safety Guarantees

- **Read-only**: No writes to Google Ads, no database writes, no scheduler changes
- **No production source switch**: This PR only audits; it does not change what serves production
- **No bid/budget/campaign changes**: Direct connector is read-only by design
- **No Windsor removal**: Windsor connector remains active and unchanged
- **No OCT/offline conversion upload**: No conversion data is written anywhere

## Output Example

```
======================================================================
Google Ads vs Windsor Parity Audit
Generated: 2026-06-08 14:00:00 UTC
======================================================================

Window: 7d
----------------------------------------

  CAMPAIGNS
    Google Ads API rows: 56
    Windsor rows:        52
    Row delta:           +4
    Google Ads spend:    $1,234.56
    Windsor spend:       $1,212.34
    Spend delta:         +1.8%
    Clicks delta:        -0.4%
    Impressions delta:   +0.2%
    Conversions delta:   +1.1%
    Status:              PASS

  SEARCH_TERMS
    Google Ads API rows: 6769
    Windsor rows:        3391
    Row delta:           +3378
    Google Ads spend:    $1,234.56
    Windsor spend:       $1,223.45
    Spend delta:         +0.9%
    Clicks delta:        +0.5%
    Impressions delta:   +1.2%
    Conversions delta:   +0.8%
    Status:              WARNING
    Notes:               Row count differs materially — expected for search terms

  KEYWORDS
    Google Ads API rows: 677
    Windsor rows:        690
    Row delta:           -13
    Google Ads spend:    $1,234.56
    Windsor spend:       $1,232.12
    Spend delta:         +0.2%
    Clicks delta:        -0.1%
    Impressions delta:   +0.3%
    Conversions delta:   +0.4%
    Status:              PASS
```
