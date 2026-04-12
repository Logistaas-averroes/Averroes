# Phase Roadmap
## Logistaas Ads Intelligence — Full System Vision

---

## Overview

The system is built in four phases. Each phase extends the previous one. No phase begins until the previous phase has been validated.

The guiding principle: **earn the right to automate through proven accuracy.**

---

## Phase 1 — Signal Intelligence
### "Where am I losing money right now?"
**Status:** Building now
**Duration:** 4–8 weeks minimum before advancing

### What it builds
- Data connectors (Windsor.ai + HubSpot) ✅ Complete
- GCLID reconciliation ✅ Complete
- Waste detection engine ✅ Complete
- Lead quality breakdown ✅ Complete
- Campaign truth table ✅ Complete
- Weekly report via Claude API ✅ Complete
- Scheduler (weekly + daily lightweight) 🔨 In progress

### What it outputs
Every Monday morning:
1. **Waste report** — search terms burning money with no qualified outcome, estimated weekly cost
2. **Lead quality breakdown** — per-campaign split of qualified vs junk vs unknown
3. **Campaign truth table** — spend vs confirmed SQLs vs junk rate vs verdict (FIX/HOLD/SCALE/CUT)

### What it does NOT do
- Does not touch Google Ads
- Does not push negatives
- Does not modify bids or budgets
- Does not upload conversions
- Does not automate any action

### Advancement criteria
All of these must be true:
- System has run 4+ consecutive weeks without data errors
- Weekly reports have been manually checked against actual account performance
- At least 3 specific recommendations have been applied by hand and the outcome verified
- Youssef explicitly approves Phase 2

---

## Phase 2 — Signal Correction
### "Teach Google what a real customer looks like"
**Prerequisite:** Phase 1 validated
**Duration:** 4–8 weeks minimum before advancing

### What it adds
**OCT Uploader** (`connectors/oct_uploader.py`)
- Reads HubSpot deal stage changes
- Matches each deal to the originating ad click via GCLID
- Pushes conversion events to Google Ads with real deal values
- Google's Smart Bidding now learns from revenue, not form fills

### Deal stage → conversion value mapping
| HubSpot Stage | Conversion Action | Value |
|--------------|------------------|-------|
| Proposal sent | `logistaas_proposal` | $300 |
| In Trials | `logistaas_trial` | $1,000 |
| Pricing accepted | `logistaas_pricing` | $2,500 |
| Invoice sent | `logistaas_invoice` | $4,000 |
| Deal Won | `logistaas_won` | Actual ACV |

### Important constraints
- `--dry-run` mode built during Phase 1 — logs what would be uploaded without touching Google Ads
- First live activation requires explicit approval
- Cannot undo uploaded conversions — irreversible
- Requires one-time setup: create 5 conversion actions in Google Ads UI before activation

### Why this matters
The 5 won deals from March–April 2026 (Al-Ahmadi, Hero Freight, Offshore Freight, Beyond3PL, Akzent) total ~$36,000 in revenue. Google Ads never received a signal that any of these clicks led to revenue. Phase 2 changes that permanently.

### Advancement criteria
- OCT running cleanly for 4+ weeks
- CPQL visible in Google Ads and trending in expected direction
- No duplicate upload incidents
- Youssef approves Phase 3

---

## Phase 3 — Active Defence
### "Stop the waste automatically, with human approval"
**Prerequisite:** Phase 2 validated

### What it adds

**Negative keyword push** (`connectors/negative_pusher.py`)
- System generates negative keyword candidates from weekly N-gram analysis
- Presents ranked list with spend justification
- Human reviews and approves
- Approved negatives pushed to Google Ads shared negative lists
- Full audit trail — every push logged with timestamp and approver

**Why human approval gate:**
Negative keywords are permanent decisions. Adding the wrong negative can kill real pipeline. The system identifies candidates. Humans decide.

### What it does NOT add
- No automatic bid changes
- No automatic budget changes
- No automatic campaign pausing
- Human remains in control of all structural decisions

---

## Phase 4 — Intelligence Platform
### "The full war room"
**Prerequisite:** Phase 3 proven

### What it adds

**Frontend dashboard** (Next.js on Render)
- Campaign states live (FIX/HOLD/SCALE/CUT)
- Real-time alerts
- Geo intelligence map (80+ countries)
- Lead quality by campaign
- Search term forensics
- AI consultant chat interface

**Meta Ads integration**
- Windsor.ai already supports Meta — same plan, no extra cost
- `connectors/meta_pull.py` — pulls Meta campaign data
- All analysis modules extended to cover Meta
- Unified view: Google + Meta spend vs pipeline

**Gamma report integration**
- Weekly and monthly reports auto-generated as Gamma presentations
- Beautiful formatted slides delivered alongside raw markdown
- Uses the Gamma MCP connector already configured

**FastAPI server**
- On-demand report triggers via HTTP
- Powers the frontend dashboard data layer

---

## PR Roadmap (Current Build Status)

| PR | Module | Status | Phase |
|----|--------|--------|-------|
| PR-ADS-001 | Connectors + GCLID match | ✅ Merged | 1 |
| PR-ADS-002 | Scheduler (weekly + daily) | 🔨 In progress | 1 |
| PR-ADS-003 | Config validation + pattern update | ⬜ Queued | 1 |
| PR-ADS-004 | End-to-end test + first real report | ⬜ Queued | 1 |
| PR-ADS-005 | OCT uploader (dry-run only) | ⬜ Queued | 1/2 |
| PR-ADS-006 | Render deployment | ⬜ Queued | 1 |
| PR-ADS-007 | OCT live activation | ⬜ Phase 2 gate | 2 |
| PR-ADS-008 | Negative keyword push | ⬜ Phase 3 gate | 3 |
| PR-ADS-009 | FastAPI server | ⬜ Phase 4 | 4 |
| PR-ADS-010 | Frontend dashboard | ⬜ Phase 4 | 4 |
| PR-ADS-011 | Meta Ads integration | ⬜ Phase 4 | 4 |
| PR-ADS-012 | Gamma report integration | ⬜ Phase 4 | 4 |

---

## The Long-Term Vision

When fully built, this system is Logistaas's permanent paid media intelligence layer:

- Every dollar spent is tracked from click to closed deal
- Google Ads learns from revenue, not form fills
- Junk is blocked before it poisons the algorithm
- Every market's performance is visible and actionable
- The team gets a weekly briefing without lifting a finger
- Strategic decisions are made on verified data, not platform metrics

**The goal is not a better dashboard. The goal is a better signal.**
