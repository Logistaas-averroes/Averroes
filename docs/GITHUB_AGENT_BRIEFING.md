# GitHub Agent Briefing
## Logistaas Ads Intelligence System

> **READ THIS FIRST before touching any file.**
> This is the complete operating manual for the GitHub Claude Agent working on this repository.

---

## Who You Are

You are the build agent for the **Logistaas Ads Intelligence System** — an automated, doctrine-driven Google Ads advisory engine for a B2B SaaS company selling Transportation Management System (TMS) software to freight forwarders across 80+ countries.

You write Python. You commit to GitHub. You open PRs. You follow the doctrine.

---

## What This System Does

Connects three data sources into a single revenue intelligence loop:

1. **Windsor.ai** → pulls Google Ads data (campaigns, search terms, keywords, geo)
2. **HubSpot CRM** → pulls pipeline data (contacts, deals, GCLIDs, MQL status)
3. **Anthropic Claude API** → runs doctrine analysis and produces recommendations

Outputs: daily alerts, weekly doctrine reports, monthly strategy — automatically, on Render.com cron jobs.

---

## The Doctrine (READ THIS — IT GOVERNS EVERYTHING)

Full rules in: `docs/DOCTRINE.md`

**Summary of non-negotiables:**
- Google Ads conversions ≠ revenue. Always cross-reference with HubSpot.
- Brand and non-brand data must NEVER be mixed.
- Never optimise for CPL. Always CPQL (Cost Per Qualified Lead).
- Every campaign must be classified: FIX / HOLD / SCALE / CUT.
- Junk patterns sourced from `config/junk_patterns.yaml` — never hardcoded.
- Thresholds from `config/thresholds.yaml` — never hardcoded (`config/logistaas_config.yaml` does not yet exist; tracked in PR-ADS-005).

---

## Live Account Facts (Confirmed — Do Not Guess)

| Item | Value |
|------|-------|
| HubSpot Account ID | 142257138 |
| HubSpot Owner | Youssef Awwad (youssef.awwad@logistaas.com) |
| Google Ads Customer ID | 3059734490 |
| HubSpot timezone | Asia/Amman (UTC+3) |
| GCLID field | `hs_google_click_id` — confirmed populated |

### Confirmed HubSpot Deal Stage IDs
```
qualifiedtobuy → Proposal/Implementation Plan  → OCT value: $300
334269159      → In Trials                     → OCT value: $1,000
326093513      → Pricing Acceptance             → OCT value: $2,500
326093515      → Invoice Agreement Sent         → OCT value: $4,000
326093516      → Deal Won (primary OCT signal)  → OCT value: actual ACV
379260140      → Unresponsive
379124201      → Lost Deal
379124202      → Downgrade Deal
379124203      → Churn Deal
```

### Confirmed HubSpot Contact Fields
```python
"hs_google_click_id"          # GCLID — key linking field
"mql_status"                  # "OPEN - Connecting", "CLOSED - Job Seeker" etc.
"hs_analytics_source"         # "PAID_SEARCH"
"hs_analytics_source_data_1"  # Campaign name
"hs_analytics_source_data_2"  # Keyword (e.g. "cargowise", "logisys")
"hs_analytics_first_url"      # Full first-click URL with all UTM params
"ip_country"                  # Lead geography
"mql___mdr_comments"          # MDR notes — contains junk signals
```

### Sample Lead Data (Real — for testing logic)
| Contact | Country | Keyword | MQL Status | Assessment |
|---------|---------|---------|------------|------------|
| Ahmed (Contieners) | Tunisia | cargowise | CLOSED - Job Seeker | Junk |
| Mariam (Deye) | Lebanon | logisys | OPEN - Connecting | Wrong industry (solar) |
| Bekir (Bekirduman) | Turkey | gofreight | OPEN - Connecting | Legitimate forwarder |

---

## Repository Structure

```
logistaas-ads-intelligence/
├── connectors/
│   ├── windsor_pull.py       ✅ Built — PR-ADS-001
│   ├── hubspot_pull.py       ✅ Built — PR-ADS-001
│   ├── gclid_match.py        ✅ Built — PR-ADS-001
│   └── oct_uploader.py       ⬜ BUILD THIS (Phase 2 — PR-ADS-008)
├── analysis/
│   ├── core.py               ✅ Built — Phase 1 rebuild
│   └── advisor.py            ✅ Built — Phase 1 rebuild
├── scheduler/
│   ├── daily.py              ✅ Built — PR-ADS-002
│   ├── weekly.py             ✅ Built — PR-ADS-002
│   ├── monthly.py            ✅ Built — PR-ADS-003
│   ├── delivery.py           ✅ Built — PR-ADS-003
│   └── run_history.py        ✅ Built — PR-ADS-012
├── scripts/
│   └── healthcheck.py        ✅ Built — PR-ADS-013
├── api/
│   └── server.py             ⬜ BUILD THIS (Phase 4 — PR-ADS-011)
├── config/
│   ├── logistaas_config.yaml ⬜ MISSING — tracked in PR-ADS-005 (gclid_match.py falls back to defaults)
│   ├── patterns.yaml         ⬜ MISSING — scheduler/daily.py references this; actual file is junk_patterns.yaml
│   └── junk_patterns.yaml    ✅ EXISTS — junk detection patterns
├── docs/
│   ├── DOCTRINE.md           ✅ EXISTS — Claude system prompt source
│   ├── PR_TEMPLATE.md        ✅ EXISTS — use for every PR
│   └── GITHUB_AGENT_BRIEFING.md ← you are here
├── Makefile                   ✅ Built — PR-ADS-015
├── .env.example              ✅ EXISTS
├── requirements.txt          ✅ EXISTS
└── render.yaml               ✅ EXISTS
```

---

## Build Order (Current Phase 1 Status)

### Completed
| PR ID | Module | Status |
|-------|--------|--------|
| PR-ADS-001 | Connectors + GCLID match | ✅ Complete |
| PR-ADS-002 | Daily + weekly schedulers | ✅ Complete |
| PR-ADS-003 | Monthly scheduler + delivery | ✅ Complete |
| PR-ADS-004 | Analysis core + advisor | ✅ Complete |
| (unnumbered) | Run history (`scheduler/run_history.py`) | ✅ Complete |
| (unnumbered) | Healthcheck (`scripts/healthcheck.py`) | ✅ Complete |
| (unnumbered) | Makefile / manual ops runner | ✅ Complete |
| PR-ADS-012 | Repository reality sync + docs | ✅ Complete |

### Queued (Phase 1 stabilization — next in order)
| PR ID | Module | What it does |
|-------|--------|-------------|
| PR-ADS-013 | `scheduler/daily.py` + `config/` | Broken reference fix (doctrine import, patterns.yaml path, logistaas_config.yaml) |
| PR-ADS-014 | `scheduler/daily.py` | Daily scheduler rebuild — correct imports + config alignment |
| PR-ADS-005 | `config/` | Config hardening — create missing config files, validate all YAML keys |
| PR-ADS-006 | System-wide | End-to-end test + first real report on live environment |
| PR-ADS-007 | Render | Deploy to Render, verify all 3 cron jobs active |

### Phase 2 (after Phase 1 validated)
| PR ID | Module | What it does |
|-------|--------|-------------|
| PR-ADS-008 | `connectors/oct_uploader.py` (dry-run) | Read HubSpot deal stage changes → OCT upload dry-run |
| PR-ADS-009 | OCT live activation | First live OCT conversion upload after human approval |

### Phase 3+
| PR ID | Module | What it does |
|-------|--------|-------------|
| PR-ADS-010 | Negative keyword push | System-generated negatives, human approval gate |
| PR-ADS-011 | `api/server.py` | FastAPI on-demand endpoints |
| PR-ADS-016 | Frontend dashboard | War room Next.js dashboard |
| PR-ADS-017 | Meta Ads integration | Windsor.ai Meta connector |

---

## PR Rules (Mandatory)

Every PR you open must use the template in `docs/PR_TEMPLATE.md`.

**Minimum required in every PR description:**
1. PR ID (PR-ADS-XXX)
2. Module affected
3. What was built / changed
4. How it was tested
5. What it unblocks next

**Never merge a PR that:**
- Mixes brand and non-brand data
- Hardcodes doctrine rules in Python
- Hardcodes API keys
- Calls external APIs from an engine module
- Writes analysis logic inside a connector
- Breaks the data flow direction

---

## Architecture Rules (Non-Negotiable)

### Layer separation
```
connectors/ → ONLY fetches data and writes to data/
engine/     → ONLY reads from data/ and returns findings dict
doctrine/   → ONLY receives findings dict and calls Claude API
scheduler/  → ONLY orchestrates modules in sequence
api/        → ONLY exposes scheduler triggers as HTTP endpoints
```

### Data directory
- All raw data → `data/` (gitignored)
- All reports → `outputs/` (gitignored)
- Never commit data files

### Config rules
- All thresholds → `config/thresholds.yaml`
- All junk patterns → `config/junk_patterns.yaml`
- All secrets → environment variables (never in code)

---

## Environment Variables Required

```bash
ANTHROPIC_API_KEY         # claude-sonnet-4-6 model
HUBSPOT_API_KEY           # HubSpot private app token
WINDSOR_API_KEY           # Windsor.ai API key
WINDSOR_ACCOUNT_ID        # Windsor account ID
GOOGLE_ADS_DEVELOPER_TOKEN
GOOGLE_ADS_CLIENT_ID
GOOGLE_ADS_CLIENT_SECRET
GOOGLE_ADS_REFRESH_TOKEN
GOOGLE_ADS_CUSTOMER_ID    # 3059734490
REPORT_EMAIL              # youssef.awwad@logistaas.com
SLACK_WEBHOOK_URL         # optional
```

---

## How to Start Each Session

1. Read this file
2. Read `docs/04_PHASE_ROADMAP.md` — identifies the current active PR
3. Read `docs/09_REPO_STATE.md` — identifies what is built, broken, and missing
4. Read `docs/DOCTRINE.md`
5. Build only what the current PR description specifies

---

## Questions You Will Be Asked By the PR Reviewer

- Does this module respect layer separation?
- Does it source thresholds from config?
- Does it source junk patterns from patterns.yaml?
- Is brand separated from non-brand?
- Is GCLID required before any OCT upload?
- Was it tested against real HubSpot data?

If you can't answer yes to all of these, the PR is not ready.

---

## Contact

**Project owner:** Youssef Awwad — youssef.awwad@logistaas.com
**Doctrine authority:** `docs/DOCTRINE.md` (this is the final word on all ad strategy decisions)
**Config authority:** `config/thresholds.yaml` (decision thresholds) and `config/junk_patterns.yaml` (junk patterns)
**Roadmap authority:** `docs/04_PHASE_ROADMAP.md` — always read this before starting a session
**Repo state authority:** `docs/09_REPO_STATE.md` — single source of truth for what is built, broken, or missing
