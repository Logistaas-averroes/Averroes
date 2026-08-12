# Data Reference
## Confirmed field names, IDs, and values — live Logistaas account

**Last verified:** April 12, 2026
**Source:** Live HubSpot account 142257138 and Windsor.ai

---

## HubSpot Account

| Item | Value |
|------|-------|
| Account ID | 142257138 |
| Portal | app-eu1.hubspot.com |
| Timezone | Asia/Amman (UTC+3) |
| Currency | USD |
| Active deals | 557 |
| Paid search contacts (3 weeks) | ~2,582 |

---

## HubSpot Contact Fields

These field names are confirmed from live API calls. Use exactly as shown.

| API Field Name | Label | Notes |
|---------------|-------|-------|
| `hs_google_click_id` | Google ad click ID | GCLID — 100% populated on paid contacts |
| `mql_status` | MQL Status | See values below |
| `hs_lead_status` | Lead Status | Sales outreach status |
| `lifecyclestage` | Lifecycle Stage | marketingqualifiedlead, salesqualifiedlead, opportunity, etc. |
| `hs_analytics_source` | Traffic Source | Filter by `PAID_SEARCH` |
| `hs_analytics_source_data_1` | Campaign | Campaign name from UTM |
| `hs_analytics_source_data_2` | Keyword | Keyword bid on |
| `hs_analytics_first_url` | First URL | Full URL with all UTM + GCLID params |
| `ip_country` | IP Country | Geography — more reliable than `country` field |
| `mql___mdr_comments` | MDR Comments | Three underscores — contains junk signals |
| `createdate` | Create Date | Contact creation timestamp |
| `company` | Company Name | |

---

## Lifecycle Stage & Stage-Entry Dates (PR-ADS-153B — CANONICAL FUNNEL)

**HubSpot Lifecycle Stage is the canonical Averroes funnel.** `mql_status` is an
operational workflow dimension and no longer defines any funnel stage. Full
doctrine: `docs/33_CANONICAL_CRM_FUNNEL.md`.

| API Field Name | Purpose |
|---------------|---------|
| `lifecyclestage` | Canonical funnel stage. Values: `subscriber`, `lead`, `marketingqualifiedlead`, `salesqualifiedlead`, `opportunity`, `customer`, `evangelist`, `other`, plus custom `370543605` (Discarded Contact) and `377714653` (Reseller) |
| `hs_v2_date_entered_lead` | Canonical **Lead** event date |
| `hs_v2_date_entered_marketingqualifiedlead` | Canonical **MQL** event date |
| `hs_v2_date_entered_salesqualifiedlead` | Canonical **SQL** event date |
| `hs_v2_date_entered_opportunity` | Canonical **Opportunity** event date |
| `hs_v2_date_entered_customer` | Canonical **Lifecycle Customer** event date |
| `lastmodifieddate` | Modification watermark driving the incremental contact sync |

Rules:

- A missing `hs_v2_date_entered_*` is a **coverage gap**. `createdate` is NEVER
  substituted for a funnel event date.
- Funnel counts are **not** mutually exclusive by current stage — a contact now at
  `customer` still counts in the SQL cohort of the window it entered SQL.
- Unknown/new lifecycle values are preserved verbatim, never folded into one of
  the five primary stages.

Durable store: `hubspot_contact_funnel` (one row per HubSpot contact id, all
sources). No email address is stored.

---

## MQL Status Values (Exact Spelling)

**`DICARDED` — one R, not two. This is how it appears in HubSpot. Preserve this spelling exactly in all code.**

| MQL Status | Category | Signal |
|-----------|----------|--------|
| `OPEN - Connecting` | Unknown | MDR attempting to reach — no verdict |
| `OPEN - Meeting Booked` | In progress | Meeting scheduled |
| `OPEN - Pending Meeting` | In progress | Meeting arranged, not yet held |
| `CLOSED - Sales Qualified` | Confirmed qualified | Real freight forwarder buyer |
| `CLOSED - Deal Created` | Confirmed qualified | Deal opened in pipeline |
| `CLOSED - Bad Product Fit` | Wrong fit | Wrong company type or size |
| `CLOSED - Job Seeker` | Confirmed junk | Looking for employment, not software |
| `CLOSED - Sales Disqualified` | Wrong fit | Reached, not qualified |
| `DICARDED` | Confirmed junk | No viable lead action — one R |

**PR-ADS-153B:** the canonical mapping of every value now lives in ONE place —
`analysis/mql_status_taxonomy.py`. It additionally maps `CLOSED - Bad Contact`,
`CLOSED - No Response` and `RESELLER` (previously unmapped and silently collapsed
into `unknown`), and distinguishes `no_verdict` (property is null) from `unmapped`
(a NEW production value that must surface as an audit warning). The legacy
`mql_status ← mql___mdr_comments` fallback is removed from the canonical path so
free text can no longer reach the typed property. The `status_category` column
derived from these values is now **compatibility-only** and is retired for funnel
counting in PR-ADS-153C.

---

## HubSpot Deal Stage IDs

| Stage ID | Stage Label | Phase 2 OCT Value |
|---------|------------|------------------|
| `qualifiedtobuy` | Proposal / Implementation Plan | $300 |
| `334269159` | In Trials | $1,000 |
| `326093513` | Pricing Acceptance | $2,500 |
| `326093515` | Invoice Agreement Sent | $4,000 |
| `326093516` | Deal Won / Payment Received | Actual ACV |
| `379260140` | Unresponsive | No OCT |
| `379124201` | Lost Deal | No OCT |
| `379124202` | Downgrade Deal | No OCT |
| `379124203` | Churn Deal | No OCT |

---

## Active Won Deals (Validation Reference)

These deals are used to validate the OCT dry-run in Phase 1:

| Deal | Amount | Stage |
|------|--------|-------|
| Al-Ahmadi Logistics Co., Ltd. | $2,400 | Won |
| Hero Freight | $2,580 | Won |
| Offshore Freight | $8,932 | Won |
| Beyond3PL | $21,870 | Won |
| Akzent | $51,366 | Won |
| Iscotrans Middle East Marine | $4,290 | Won |

---

## Active Campaigns (Confirmed from Contact Data)

| Campaign Name | Region | Notes |
|--------------|--------|-------|
| `global - competitors` | Global | Highest volume, high discard rate |
| `compliance - markets` | Compliance | Better quality leads |
| `emerging - markets` | Emerging | Mixed quality |
| `mexico,chile` | LATAM | High Spanish free-intent junk |
| `gulf` | Gulf GCC | Best SQL rate |
| `mena` | MENA | Arabic free-intent detected |
| `europa` | Europe | Mixed |
| `europe low cpc-new` | Eastern Europe | High discard rate |
| `mature - markets` | Mature markets | Low volume |
| `sa 2 \| medium cpc (latin america).` | South America | Spanish language |
| `competitors - lowcpc` | Budget competitors | Small, some real leads |
| `cpc - premium` | Premium | Small, high quality |

---

## Three Reference Contacts (For Testing)

**Confirmed junk — job seeker:**
- Contact ID: `750636300494`
- Country: Tunisia | Keyword: cargowise | Campaign: emerging - markets
- MQL: `CLOSED - Job Seeker`

**Confirmed wrong industry:**
- Contact ID: `747395549402`
- Country: Lebanon | Keyword: logisys | Campaign: emerging - markets
- Company: Deye (solar inverter manufacturer)
- MQL: `OPEN - Connecting`

**Confirmed real lead:**
- Contact ID: `754397677758`
- Country: UAE | Keyword: gofreight | Campaign: compliance - markets
- MQL: `OPEN - Meeting Booked`

---

## Windsor.ai Fields

**Campaign level:** `campaign`, `campaign_id`, `date`, `spend`, `clicks`, `impressions`, `conversions`, `cpc`, `ctr`

**Keyword level:** `keyword`, `match_type` (b/e/p), `ad_group`, `quality_score`, `spend`, `clicks`, `conversions`

**Search term level (requires paid plan — verify active):** `search_term`, `matched_keyword`, `match_type`, `spend`, `clicks`, `conversions`

> **Note:** `matched_keyword` and `match_type` are not confirmed in the MCP contract below. The confirmed MCP extraction fields are the definitive list for 60-day search-term pulls.

**Geo level:** `country`, `spend`, `clicks`, `impressions`, `conversions`

---

## Windsor.ai MCP Search-Term Contract — Confirmed May 2026

**Status:** Confirmed working from live account.
**Account:** `305-973-4490`
**Connector:** `google_ads`
**Date preset:** `last_60d`
**Returned volume:** ~45,292 rows across 10 campaigns

### Required get_data request

Fields array:

- `search_term`
- `campaign`
- `ad_group`
- `impressions`
- `clicks`
- `spend`
- `conversions`

### Response shape

The MCP response returns a list containing a `text` key. The `text` value is a JSON string.

```python
raw_response = get_data(...)
text_payload = raw_response[0]["text"]
rows = json.loads(text_payload)
```

### Critical rules

- Field name is exactly `search_term`.
- Do not use `search_term_text`, `query`, or `search_query`.
- `ad_group` may return a Google Ads resource path, e.g. `customers/3059734490/adGroups/175677406221`
- To get only the ad group ID: `ad_group_id = row["ad_group"].split("/")[-1]`

### Output meaning

Each row represents one search-term / campaign / ad-group combination with metrics.

### Runtime availability

MCP get_data is **not** available at app runtime (no MCP client in Render deployment).
The REST API connector (`pull_search_terms()`) attempts `date_preset=last_60d` as runtime fallback and falls back to `last_14d` if empty.
**MCP get_data is confirmed; REST runtime parity must be validated.**
For MCP-extracted payloads, use `_parse_windsor_mcp_response()` in `connectors/windsor_pull.py`.

### Pipeline verification (PR-ADS-065)

The Search Terms pipeline now includes explicit logging and verification:

- **Connector** (`pull_search_terms()`): Logs date_preset, row count, sample keys, search_term presence.
  Warns loudly if zero rows. Errors if search_term field is missing.
- **Normalizer** (`normalize_search_term_rows()`): Skips blank search_term rows, preserves field contract.
- **DB Writer** (`write_search_terms()`): Logs input/prepared/skipped/written counts.
  Errors if all rows skipped due to missing field.
- **Scheduler**: Records zero-row pulls as `status="success"` with `row_count=0` and a warning message.
  Fetched > 0 but written = 0 is marked as failed.
- **Verification script**: `scripts/verify_search_terms_pipeline.py --days 60 --db-only --pretty`

### MCP Import Path (PR-ADS-066)

If Windsor REST returns 0 rows but MCP previously returned data:

```bash
# Dry-run (default):
python scripts/import_windsor_mcp_search_terms.py --input data/windsor_mcp_search_terms_raw.json --dry-run

# Apply:
python scripts/import_windsor_mcp_search_terms.py --input data/windsor_mcp_search_terms_raw.json --apply
```

Rules:
- Dry-run by default — validates without writing.
- Requires `search_term` field in every row (rejects blanks).
- Normalizes `ad_group_id` from Google Ads resource paths.
- Creates sync_batch with `source="windsor_mcp"` `dataset="search_terms"`.
- Never calls Google Ads, HubSpot, or any external API.

---

## UTM Parameters in First-Click URL

All paid contacts carry a full UTM URL in `hs_analytics_first_url`:
```
utm_term=cargowise          ← keyword bid on
utm_campaign=Emerging       ← campaign name
utm_source=adwords
utm_medium=ppc
hsa_cam=23345129000         ← campaign ID
hsa_grp=188615891183        ← ad group ID
hsa_kw=cargowise            ← keyword text
hsa_mt=b                    ← match type (b=broad, e=exact, p=phrase)
gclid=Cj0KCQjwyr3O...       ← GCLID (also in hs_google_click_id)
```

---

## System Status War Room (PR-ADS-068)

The System Status War Room (`GET /api/system/status-war-room`) provides a consolidated
view of all data pipelines, combining canonical freshness semantics with dependency mapping.

### Pipeline Dependencies

| Dataset | Source | Depends On | Blocks |
|---------|--------|-----------|--------|
| campaigns | windsor | — | — |
| search_terms | windsor | — | waste_terms, ngrams |
| waste_terms | analysis | search_terms | — |
| ngrams | computed | search_terms | — |
| keywords | windsor | — | — |
| geo | windsor | — | — |
| leads | hubspot | — | — |
| deals | hubspot | — | — |
| gclid_attribution | gclid | — | — |
| gclid_coverage_snapshots | gclid | — | — |
| historical_intelligence | analysis | — | — |

### Source Groupings

| Source | Label | Datasets |
|--------|-------|----------|
| windsor | Windsor / Google Ads | campaigns, search_terms, keywords, geo |
| hubspot | HubSpot CRM | leads, deals |
| gclid | GCLID Match | gclid_attribution, gclid_coverage_snapshots |
| analysis | Analysis Layer | waste_terms, historical_intelligence |
| computed | Computed Layer | ngrams |

Service logic: `services/system_status_service.py`

---

## UI Navigation — Route Stability

All frontend pages use `data-page` route keys that are stable identifiers. Visible labels may be changed for UX clarity without affecting routing or data references.

See `docs/24_UI_NAVIGATION_MODEL.md` for the full navigation model and rename map.
