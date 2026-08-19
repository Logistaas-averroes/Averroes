# GitHub Agent Briefing
## Operating manual for the Claude GitHub Agent

**Read this entire document before writing any code.**

---

> ### ⚠️ Authoritative status (PR-ADS-153E-B, August 2026)
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
>   proven completion.
> * **153E-A3: merged.** Blank HubSpot monetary fields are normalized by one
>   shared parser before persistence, so currency resolution and the ledger's
>   `NUMERIC` columns can never disagree about whether a deal has an amount.
> * **153E-B: consumer cutover DONE.** The production gate passed
>   (`--all-windows` aggregate `ok: true`, 0 write failures, 0 association
>   failures), and every revenue consumer now reads the canonical deal ledger
>   through ONE shared contract (`services/canonical_revenue_service.py`) at an
>   explicit attribution scope
>   (`all_source ≥ google_ads_source ≥ campaign_attributable ≥ gclid_attributable`).
>   Unit Economics is off local JSON / Windsor and on business windows. Shadow
>   mode is over — see `docs/35_CANONICAL_REVENUE_LEDGER.md` §14–§23.
> * **153F and 153G: remain.** Geo synchronization; legacy table/route deletion.
>   No legacy table is dropped by 153E-B; they remain written and readable for
>   the observation period.
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
| `services/canonical_revenue_service.py` | ✅ Complete | **PR-ADS-153E-B** — THE canonical revenue read contract. Every revenue page reads through it. Never add a second revenue read path, never let a consumer import `db.deal_ledger_repository` directly, and never add a fallback to a legacy lineage: CI fails on all three |
| `analysis/revenue_scope.py` | ✅ Complete | **PR-ADS-153E-B** — the attribution-scope lattice. Every revenue response declares its scope; a narrower scope is nested inside every wider one by construction |
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

---

## Country geography (PR-ADS-153F, August 2026)

If you touch anything country-shaped, read
`docs/36_CANONICAL_COUNTRY_GEOGRAPHY.md` first. The short version:

* **Group on `analysis.country_identity.country_key(...)`. Never on a country
  name, never on a code you validated yourself.** Three different join rules
  used to coexist, which is why the same window produced different rows on pages
  claiming to describe the same thing.
* **Never drop a row because its geography is unknown.** Blank, invalid and
  unresolved geography goes to the `unknown` residual with a reason. Dropping it
  is what made two pages disagree about the same total.
* **Never treat a two-letter token as a country code.**
* **Ask `google_ads_geo_sync_service.country_geo_ready(status)`** rather than
  comparing the status string. A page that adopts its own bar re-creates the
  defect where a window was ready on one page and blocked on another.
* **Do not change `SPEND_VARIANCE_TOLERANCE`, window definitions, FX doctrine,
  revenue scopes, won-deal doctrine or PR-ADS-131 residual eligibility to make a
  blocked page go green.** The gate is not the defect.
* **Google Ads geography ≠ HubSpot contact geography.** They are joined at
  reporting grain, estimate-grade, and the response says so.
* Adding a market is one line in `SUPPORTED_COUNTRIES`; both directions and the
  alias table derive from it.
