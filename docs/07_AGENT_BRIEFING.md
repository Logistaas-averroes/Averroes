# GitHub Agent Briefing
## Operating manual for the Claude GitHub Agent

**Read this entire document before writing any code.**

---

## What You Are Building

A lean Google Ads signal correction engine for Logistaas. It reads data from Windsor.ai and HubSpot, analyses it using three simple functions, and produces a plain-language weekly report via Claude API.

**Phase 1 is read-only. No writing to Google Ads. No writing to HubSpot.**

---

## Read These Before Starting Each Session

1. `docs/07_AGENT_BRIEFING.md` — this document
2. `docs/03_ARCHITECTURE.md` — layer rules and data flow
3. `docs/05_DATA_REFERENCE.md` — confirmed field names and IDs
4. `docs/02_DOCTRINE.md` — the governing rules
5. The specific PR description for your current task

---

## What's Already Built (Do Not Rebuild)

| File | Status | Notes |
|------|--------|-------|
| `connectors/hubspot_pull.py` | ✅ Complete | Do not modify |
| `connectors/windsor_pull.py` | ✅ Complete | Do not modify |
| `connectors/gclid_match.py` | ✅ Complete | Do not modify |
| `analysis/core.py` | ✅ Complete | waste_detection, lead_quality, campaign_truth |
| `analysis/advisor.py` | ✅ Complete | Claude API integration |
| `config/junk_patterns.yaml` | ✅ Complete | All junk patterns |
| `config/thresholds.yaml` | ✅ Complete | All decision rules |
| `docs/*.md` | ✅ Complete | All project documents |

---

## Layer Rules (Non-Negotiable)

```
connectors/   → ONLY fetches data. Writes to data/. Nothing else.
analysis/     → ONLY reads data/. Returns findings. No external API calls.
scheduler/    → ONLY orchestrates modules in sequence. No business logic.
```

If you find yourself adding analysis logic to a connector — stop.
If you find yourself calling an external API from analysis/ — stop.
If you find yourself adding business logic to a scheduler — stop.

---

## Critical Field Names

```python
# Exact spelling required — do not guess
"hs_google_click_id"       # GCLID
"mql_status"               # MQL status
"mql___mdr_comments"       # THREE underscores
"hs_analytics_source_data_1"  # Campaign name
"hs_analytics_source_data_2"  # Keyword
"ip_country"               # Geography

# MQL status spelling — one R
"DICARDED"                 # NOT "DISCARDED"
```

---

## Config Rules

All thresholds from `config/thresholds.yaml`. All patterns from `config/junk_patterns.yaml`. Nothing hardcoded in Python.

---

## Data Directory Rules

`data/` — gitignored. All connector outputs. Never commit.
`outputs/` — gitignored. All reports. Never commit.

---

## PR Requirements

Every PR must use `docs/08_PR_TEMPLATE.md`.

Minimum required:
1. PR classification block (type, module, depends on, blocks)
2. What the problem is / what was missing
3. File-by-file implementation description
4. Test commands with expected output
5. Doctrine compliance checklist
6. Post-merge verification

---

## Confirmed Account Details

```
HubSpot account:    142257138
Google Ads account: 3059734490
Report email:       youssef.awwad@logistaas.com
Timezone:           Asia/Amman (UTC+3)
```

---

## Current Task (Update This Each Session)

**Currently building:** PR-ADS-002 — Scheduler (weekly.py + daily.py)

`scheduler/weekly.py` should:
1. Call `windsor_pull.pull_campaign_performance()` and `windsor_pull.pull_search_terms()`
2. Call `hubspot_pull.pull_paid_search_contacts()` and `hubspot_pull.pull_deals_with_gclid()`
3. Call `gclid_match.run_gclid_match()`
4. Call `core.run_waste_detection()`, `core.run_lead_quality()`, `core.run_campaign_truth()`
5. Call `advisor.generate_weekly_report()`
6. Send output by email to `REPORT_EMAIL` env var
7. Log completion

`scheduler/daily.py` should:
1. Pull last 2 days of HubSpot contacts only
2. Check for fraud patterns against `config/junk_patterns.yaml`
3. If new fraud contacts spike >20% above 7-day baseline → send alert email
4. Otherwise → log "clean" and exit
5. Must complete in under 2 minutes

---

## If Uncertain

Stop and add a comment to the PR. Do not guess field names. Do not add features not specified. Do not infer what "should" be there based on how other systems work. Build exactly what the PR says.
