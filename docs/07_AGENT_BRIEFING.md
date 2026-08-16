# GitHub Agent Briefing
## Operating manual for the Claude GitHub Agent

**Read this entire document before writing any code.**

---

> ### ⚠️ Authoritative status (PR-ADS-153E-A, August 2026)
>
> This document's phase/status narrative below predates the PR-ADS-153A–D
> sequence and is retained for its architectural content, not its status claims.
> The current authority is
> `docs/audits/PR-ADS-153A-MINIMUM-VIABLE-TRUTH-AUDIT.md` plus the merged PRs.
>
> * **153A–153D: merged.** Truth audit; canonical CRM funnel; canonical Leads
>   experience; search-term waste consolidation.
> * **153E-A: merged.** Canonical deal ledger built and reconciled in SHADOW
>   MODE — see `docs/35_CANONICAL_REVENUE_LEDGER.md`. No revenue consumer has
>   been switched.
> * **153E-A2: cutover gate hardening.** The 153E-A gate proved the ledger was
>   reconciled but never that it was COMPLETE — a portal with no historical
>   bootstrap passed it. The audit now requires proven bootstrap coverage plus a
>   successful incremental on top of it, and
>   `scripts/backfill_canonical_deal_ledger.py` drives that bootstrap to a
>   proven completion. Still shadow mode; still no consumer switched.
> * **153E-B: blocked** until the production evidence in
>   `docs/35_CANONICAL_REVENUE_LEDGER.md` §11 passes
>   (`--all-windows` aggregate `ok: true`). Then: revenue consumer cutover and
>   Unit Economics migration.
> * **153F and 153G: remain.** Geo synchronization; legacy table/route deletion.
> * **Phase 2 / OCT: blocked.** Offline conversion uploads are not started and
>   are not authorized.
> * **Six-month read-only governance: ACTIVE.** No writes to Google Ads or
>   HubSpot. See `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md`.

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
| `connectors/hubspot_pull.py` | ✅ Complete | Extended by PR-ADS-153E-A with the read-only canonical deal-sync contract (all stages, `hs_is_closed_won`, currency trio, association labels). Change it only through a scoped PR |
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

## Critical Windsor Search-Term Contract

Do not guess Windsor search-term field names.

Confirmed:
- `search_term`
- `campaign`
- `ad_group`
- `impressions`
- `clicks`
- `spend`
- `conversions`

Forbidden guesses:
- `query`
- `search_query`
- `search_term_text`

The MCP response requires double parsing:
1. Read `raw_response[0]["text"]`
2. Run `json.loads()` on that string

`ad_group` may be a full resource path. Split by `/` and take the last segment only when an ad group ID is needed.
MCP `get_data` is the confirmed contract; REST runtime parity with MCP must be validated.

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

> **Do NOT assume the roadmap from memory.**
> Always read `docs/04_PHASE_ROADMAP.md` directly to identify the current active PR.

**Currently building:** PR-ADS-063 — Windsor Search-Term Contract Audit, Parser Fix & Docs Sync

This PR:
- Updates roadmap in `docs/04_PHASE_ROADMAP.md` to reflect actual repo state
- Updates this file to remove stale PR-ADS-002 instructions
- Updates `docs/01_PROJECT_MASTER.md` Phase 1 status to stabilization
- Adds PR classification requirements to `docs/PR_TEMPLATE.md`
- Creates `docs/09_REPO_STATE.md` as single source of truth for actual repo state

---

## Known Broken References (as of PR-ADS-012)

The following exist in code but are **not yet fixed** (tracked in PR-ADS-013):

| Reference | Location | Issue |
|-----------|----------|-------|
| `from doctrine.advisor import run_daily_analysis` | `scheduler/daily.py:51` | `doctrine/` directory does not exist |
| `config/patterns.yaml` | `scheduler/daily.py:127` | File is `config/junk_patterns.yaml`, not `patterns.yaml` |
| `config/logistaas_config.yaml` | `connectors/gclid_match.py:53` | File does not exist; code falls back to default |

Do not attempt to use `scheduler/daily.py` until PR-ADS-013 is merged.

---

## If Uncertain

Stop and add a comment to the PR. Do not guess field names. Do not add features not specified. Do not infer what "should" be there based on how other systems work. Build exactly what the PR says.
