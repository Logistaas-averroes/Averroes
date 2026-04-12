# System Architecture
## Logistaas Ads Intelligence — Technical Blueprint

---

## Design Principle

Simple. Verifiable. Each module does one thing. Nothing is hidden.

A senior engineer should be able to read any file in this system and understand exactly what it does, why, and how to verify it is working correctly.

---

## Phase 1 Architecture (Current)

```
Windsor.ai API          HubSpot API
      ↓                      ↓
windsor_pull.py        hubspot_pull.py
      ↓                      ↓
data/ads_*.json        data/crm_*.json
           ↓          ↓
        gclid_match.py
              ↓
       data/matched_gclid.json
              ↓
    ┌─────────────────────┐
    │   analysis/core.py  │
    │  waste_detection()  │
    │  lead_quality()     │
    │  campaign_truth()   │
    └─────────────────────┘
              ↓
    outputs/*.json (structured findings)
              ↓
    analysis/advisor.py
    (Claude API — doctrine as system prompt)
              ↓
    outputs/weekly_report_YYYY-MM-DD.md
              ↓
    Email delivery → youssef.awwad@logistaas.com
```

**Data flow is one direction only. No circular dependencies. No module calls another module.**

---

## Module Responsibilities

### `connectors/windsor_pull.py`
- Fetches campaign performance, search terms, keyword data, geo data from Windsor.ai
- Writes to `data/` directory
- Does nothing else. No analysis. No decisions.

### `connectors/hubspot_pull.py`
- Fetches paid search contacts (last 30 days) with MQL status, keyword, campaign, country, GCLID
- Fetches associated deals for GCLID-linked contacts
- Writes to `data/` directory
- Does nothing else.

### `connectors/gclid_match.py`
- Joins Windsor click data with HubSpot contacts via `hs_google_click_id`
- Produces `data/matched_gclid.json` and `data/gclid_coverage.json`
- Does nothing else.

### `analysis/core.py` — three functions

**`run_waste_detection()`**
- Reads search terms (or keywords if search terms unavailable)
- Matches against `config/junk_patterns.yaml`
- Cross-references with HubSpot contacts to confirm waste
- Outputs ranked waste list with spend estimates and fallback warning if data incomplete

**`run_lead_quality()`**
- Groups HubSpot contacts by campaign
- Counts MQL status values per campaign
- Calculates junk rate (verdicted contacts only)
- Outputs lead quality breakdown — no scoring model, pure aggregation

**`run_campaign_truth()`**
- Joins Windsor spend with HubSpot MQL counts
- Calculates CPQL where confirmed SQLs exist, N/A where they don't
- Applies FIX/HOLD/SCALE/CUT verdict from `config/thresholds.yaml`
- Outputs campaign truth table

### `analysis/advisor.py`
- Receives the three structured JSON outputs
- Loads doctrine from `docs/DOCTRINE.md` as system prompt
- Calls Claude API (temperature=0 for deterministic output)
- Returns plain-language weekly report

### `scheduler/weekly.py`
- Orchestrates: all connectors → all analysis → advisor → deliver
- Runs Monday 7am GMT via Render cron
- No business logic — pure orchestration

### `scheduler/daily.py` (Phase 1 — lightweight only)
- Checks for new junk contacts (last 2 days) against fraud patterns
- Sends alert only if spike detected (>20% above 7-day average)
- Does NOT run full analysis — that is weekly only

---

## Configuration (All Decision Rules in YAML)

### `config/junk_patterns.yaml`
- Junk search term patterns by category
- English, Spanish, and Arabic variants confirmed from live data
- Add new patterns here without code changes

### `config/thresholds.yaml`
- FIX/HOLD/SCALE/CUT verdict thresholds
- GCLID coverage warning threshold
- Ads vs CRM delta alert threshold
- Confirmed CUT markets (Venezuela confirmed)
- All decision rules in one place

**Nothing is hardcoded in Python. All rules live in YAML.**

---

## Directory Structure

```
logistaas-ads-intelligence/
│
├── connectors/                 ← Data ingestion (Phase 1 complete)
│   ├── __init__.py
│   ├── hubspot_pull.py        ✅ Built — PR-ADS-001
│   ├── windsor_pull.py        ✅ Built — PR-ADS-001
│   └── gclid_match.py         ✅ Built — PR-ADS-001
│
├── analysis/                   ← Intelligence layer (Phase 1)
│   ├── core.py                ✅ Built — Phase 1 rebuild
│   └── advisor.py             ✅ Built — Phase 1 rebuild
│
├── scheduler/                  ← Orchestration
│   ├── weekly.py              🔨 Building — PR-ADS-002
│   └── daily.py               🔨 Building — PR-ADS-002
│
├── docs/                       ← All project documents (this folder)
│   ├── 01_PROJECT_MASTER.md
│   ├── 02_DOCTRINE.md
│   ├── 03_ARCHITECTURE.md
│   ├── 04_PHASE_ROADMAP.md
│   ├── 05_DATA_REFERENCE.md
│   ├── 06_LEAD_QUALITY_LOGIC.md
│   ├── 07_AGENT_BRIEFING.md
│   └── 08_PR_TEMPLATE.md
│
├── config/
│   ├── junk_patterns.yaml     ✅ Built
│   └── thresholds.yaml        ✅ Built
│
├── data/                       ← Runtime data (gitignored)
├── outputs/                    ← Generated reports (gitignored)
├── .env.example
├── requirements.txt
├── render.yaml
└── .gitignore
```

---

## Future Architecture (Phases 2–4)

### Phase 2 additions
```
connectors/oct_uploader.py     ← Reads matched_gclid.json + HubSpot deals
                                  Uploads conversion events to Google Ads API
                                  --dry-run mode during Phase 1
```

### Phase 3 additions
```
connectors/negative_pusher.py  ← Human-approved negatives → Google Ads API
                                  Full audit trail, never automatic
```

### Phase 4 additions
```
api/server.py                  ← FastAPI for on-demand triggers
frontend/                      ← Next.js war room dashboard
connectors/meta_pull.py        ← Meta Ads via Windsor.ai (same plan)
```

---

## Hosting

**Render.com** — $7/month

Three cron jobs defined in `render.yaml`:
- `weekly` — `0 7 * * 1` — Monday 7am GMT
- `daily` — `0 6 * * *` — 6am GMT every day

All environment variables in Render dashboard. Never in code.

---

## Environment Variables

```
ANTHROPIC_API_KEY          ← Claude API
HUBSPOT_API_KEY            ← HubSpot private app token
WINDSOR_API_KEY            ← Windsor.ai
WINDSOR_ACCOUNT_ID         ← Windsor account ID
GOOGLE_ADS_CUSTOMER_ID     ← 3059734490 (Phase 2+ only for write operations)
REPORT_EMAIL               ← youssef.awwad@logistaas.com
```
