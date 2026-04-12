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

**Geo level:** `country`, `spend`, `clicks`, `impressions`, `conversions`

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
