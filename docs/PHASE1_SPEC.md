# Logistaas Ads Intelligence — Phase 1 Spec
## What the system does, how it works, what it outputs

**Version:** 2.0 — Rebuilt for simplicity
**Date:** April 2026
**Phase:** 1 — Read, analyse, recommend. Nothing else.

---

## The Single Question This System Answers

> **"Where am I losing money right now?"**

That's it. Everything in Phase 1 serves this question. Nothing else gets built.

---

## What Phase 1 Does NOT Do

- Does not push anything to Google Ads
- Does not modify campaigns, bids, or keywords
- Does not upload offline conversions
- Does not automate any action
- Does not score leads with complex ML models
- Does not build dashboards or UIs

All of that comes later, after the outputs are verified against reality for 4+ weeks.

---

## The Three Outputs

### Output 1 — Waste Detection
**Question it answers:** Which search terms are burning money with zero qualified outcome?

**Source:** Windsor.ai search terms report + HubSpot MQL status cross-reference

**What it produces:**
A ranked list of search term patterns with confirmed waste. Each entry states:
- The search term or pattern
- Which campaign it came from
- Estimated weekly spend on this term
- Why it's waste (job seeker, free-intent, wrong industry, fraud)
- Evidence from CRM (how many contacts from this term are DICARDED or Bad Fit)

**Fallback when search terms are missing:** Fall back to keyword-level analysis. Note clearly in output: "Search terms not available — analysis based on keyword level only. True waste may be 20–40% higher."

**Example output line:**
```
"software logistica gratis" | mexico,chile | ~$12/week | free-intent | 1 contact: OPEN-Connecting (likely window shopper)
```

---

### Output 2 — Lead Quality Split
**Question it answers:** What fraction of my spend is generating real pipeline vs junk?

**Source:** HubSpot MQL status for all paid search contacts (last 30 days)

**What it produces:**
A breakdown of every paid contact by their CRM outcome. Grouped by campaign. No scoring model — just direct MQL status counts.

**Lead categories (directly from HubSpot MQL status):**

| Category | MQL Status Values | Meaning |
|----------|------------------|---------|
| Confirmed qualified | `CLOSED - Sales Qualified`, `CLOSED - Deal Created` | Real pipeline |
| In progress | `OPEN - Meeting Booked`, `OPEN - Pending Meeting` | Potential pipeline |
| Unknown | `OPEN - Connecting` | No verdict yet |
| Confirmed junk | `CLOSED - Job Seeker`, `DICARDED` | Zero value |
| Wrong fit | `CLOSED - Bad Product Fit`, `CLOSED - Sales Disqualified` | Wrong market/product |

**Why no scoring model:** Because a score without transparency is a black box that produces wrong recommendations. MQL status IS the ground truth. If an MDR has marked a lead as Job Seeker, that's more accurate than any algorithm.

**Important:** `DICARDED` is spelled with one R in HubSpot. The system preserves this spelling exactly.

**Example output:**
```
Campaign: gulf
  Leads (30 days): 18
  Confirmed qualified: 2  (11%) — Haridas SQL, Ameer meeting booked
  In progress: 6  (33%)
  Unknown: 8  (44%)
  Confirmed junk: 1  (6%) — DICARDED
  Wrong fit: 1  (6%) — Bad product fit
```

---

### Output 3 — Campaign Truth Table
**Question it answers:** For each campaign, what is the real cost per qualified outcome?

**Source:** Windsor.ai spend data + HubSpot MQL status counts

**What it produces:**
One table. One row per campaign. The columns that matter.

```
| Campaign              | Spend (30d) | Leads | Confirmed SQL | Junk % | CPQL    | Verdict   |
|-----------------------|-------------|-------|----------------|--------|---------|-----------|
| gulf                  | ~$1,400     | 18    | 2              | 6%     | $700    | SCALE     |
| compliance - markets  | ~$580       | 14    | 1              | 14%    | $580    | SCALE     |
| cpc - premium         | ~$290       | 8     | 1              | 0%     | $290    | SCALE     |
| mena                  | ~$620       | 22    | 0              | 27%    | N/A     | HOLD      |
| emerging - markets    | ~$510       | 19    | 0              | 21%    | N/A     | FIX       |
| global - competitors  | ~$1,240     | 41    | 1              | 29%    | $1,240  | FIX       |
| europa                | ~$480       | 16    | 1              | 25%    | $480    | HOLD      |
| mature - markets      | ~$195       | 9     | 0              | 11%    | N/A     | HOLD      |
| mexico,chile          | ~$640       | 28    | 0              | 39%    | N/A     | FIX       |
| europe low cpc-new    | ~$290       | 12    | 0              | 42%    | N/A     | FIX       |
| sa 2 - latam          | ~$310       | 14    | 0              | 21%    | N/A     | HOLD      |
| competitors - lowcpc  | ~$85        | 4     | 0              | 0%     | N/A     | HOLD      |
```

**Verdict definitions (strict):**
- **SCALE** — Confirmed SQLs, CPQL within acceptable range, clean junk rate (<15%)
- **HOLD** — Insufficient data to classify, or no SQLs yet but no red flags
- **FIX** — High junk rate (>25%) OR spend with zero SQLs AND evidence of intent mismatch
- **CUT** — 60+ days, zero SQLs, confirmed wrong market. Venezuela is the only confirmed CUT.

**What "N/A" means in CPQL:** No confirmed qualified leads. CPQL is undefined — not infinite, not calculable. This is intentional. Stating "$5,000 CPQL" would imply the campaign could theoretically produce an SQL at that price. There is no evidence of that.

---

## Data Flow — Brutally Simple

```
Windsor.ai API
    ↓
connectors/windsor_pull.py
    → data/ads_campaigns.json      (spend by campaign, 30 days)
    → data/ads_search_terms.json   (actual queries, if available)
    → data/ads_keywords.json       (keyword performance, fallback)

HubSpot API
    ↓
connectors/hubspot_pull.py
    → data/crm_contacts.json       (paid search contacts, 30 days)
    → data/crm_summary.json        (MQL status counts by campaign)

analysis/waste_detection.py
    → Reads: ads_search_terms.json + crm_contacts.json
    → Outputs: outputs/waste_report.json

analysis/lead_quality.py
    → Reads: crm_contacts.json
    → Outputs: outputs/lead_quality.json

analysis/campaign_truth.py
    → Reads: ads_campaigns.json + crm_summary.json
    → Outputs: outputs/campaign_truth.json

analysis/advisor.py
    → Reads: all three output JSONs
    → Calls: Claude API with DOCTRINE.md as system prompt
    → Outputs: outputs/weekly_report.md (plain language)
```

No layers. No pipelines. No engines. Four analysis scripts. One report.

---

## What Each Script Does and Does Not Do

### `connectors/windsor_pull.py`
Does: Fetches campaign spend, search terms, keyword data from Windsor.ai. Saves to `data/`.
Does not: Analyse anything. Make decisions. Call any other API.

### `connectors/hubspot_pull.py`
Does: Fetches paid search contacts (last 30 days) with their MQL status. Saves to `data/`.
Does not: Analyse anything. Score leads. Make decisions.

### `analysis/waste_detection.py`
Does: Reads search terms. Matches against junk patterns in `config/patterns.yaml`. Counts spend. Lists confirmed waste terms.
Does not: Recommend actions. Call external APIs. Score leads.

**Uncertainty handling:** If search terms are missing or incomplete, outputs a clear warning: `"WARNING: Search term data missing or incomplete. Waste analysis based on keyword level. Estimated coverage: X%. True waste may be significantly higher."`

### `analysis/lead_quality.py`
Does: Groups all paid contacts by campaign. Counts MQL status values. Calculates percentages. No more.
Does not: Build models. Score contacts. Infer intent. Call APIs.

### `analysis/campaign_truth.py`
Does: Joins Windsor spend data with HubSpot MQL counts. Calculates CPQL where confirmed SQLs exist. Applies FIX/HOLD/SCALE/CUT verdict based on simple threshold rules from config.
Does not: Blend brand and non-brand. Make probabilistic predictions. Infer future performance.

### `analysis/advisor.py`
Does: Takes the three structured JSON outputs. Formats them into a clear data summary. Calls Claude API with DOCTRINE.md as system prompt. Asks for plain language explanation of findings.
Does not: Generate strategy from thin air. Hallucinate data. Make recommendations not supported by the data.

**Critical constraint on the AI:** Claude receives only the structured findings. It explains what the data shows in plain language. It does not extrapolate, predict, or recommend actions not directly supported by the numbers.

---

## Scheduler (Minimal)

Two jobs. Both run on Render.com.

**Weekly** — Monday 7am GMT
Runs: windsor_pull → hubspot_pull → all three analysis scripts → advisor → delivers `weekly_report.md` by email

**Daily** — 6am GMT  
Runs: hubspot_pull (2 days only) → checks if any new contacts are confirmed junk patterns → sends alert only if confirmed junk spike detected (>20% above 7-day average)

The daily job does NOT run the full analysis. It only checks for new junk contacts. It is intentionally lightweight.

---

## Configuration Files

**`config/doctrine.md`** — Claude system prompt. The rules governing all plain-language output.

**`config/junk_patterns.yaml`** — Pattern library. The ONLY place junk rules are defined.

**`config/thresholds.yaml`** — Decision thresholds. The ONLY place classification rules live.

```yaml
# thresholds.yaml
campaign_verdicts:
  scale_requires:
    min_confirmed_sqls: 1
    max_junk_pct: 15
  fix_triggers:
    min_junk_pct: 25      # OR
    zero_sqls_with_spend: true  # AND evidence of intent mismatch
  hold_default: true      # Default for anything that doesn't meet FIX or SCALE
  cut_requires:
    zero_sqls_days: 60
    confirmed_wrong_market: true   # Must be explicitly confirmed, not assumed

data_gaps:
  missing_search_terms_warning: true
  min_coverage_to_trust_waste_analysis: 60  # percent
```

Nothing is hardcoded in Python. All decision rules live in YAML.

---

## Phase 2 Conditions (Not Yet)

Phase 2 (OCT write-back) only begins when ALL of these are true:
1. System has run for 4+ weeks with no data errors
2. Weekly reports have been manually checked against what actually happened in the account
3. At least 3 recommendations have been applied by hand and the outcome was correct
4. Youssef explicitly decides to activate Phase 2

Until then: read, analyse, report. Human decides.
