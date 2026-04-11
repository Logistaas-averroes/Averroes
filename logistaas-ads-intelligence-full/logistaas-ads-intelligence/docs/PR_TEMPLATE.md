# Logistaas Ads Intelligence System
## Canonical PR Blueprint

Every Pull Request — connector, engine module, scheduler, API endpoint, or doctrine update — must follow this structure exactly.

This blueprint ensures signal integrity, system stability, and doctrine compliance across all builds.

---

## 📐 PR Header (Roadmap Context)

Every PR must start with a platform progress header.

**Example:**
```
📐 Logistaas Ads Intelligence System
PR-ADS-001 — HubSpot + Windsor Connectors (Phase 1 Foundation)
```

### Roadmap Version
`Avverros Doctrine v1.0 — Revenue Intelligence System`

### Project Countdown
| Scope | Status |
|-------|--------|
| Total Roadmap PRs | ~20 |
| Completed | 0 |
| Remaining | ~20 |
| Platform Completion | 0% before merge |

### Module Progress
| Module | Status |
|--------|--------|
| Connectors (Windsor + HubSpot) | ⬜ Not Started |
| GCLID Reconciliation | ⬜ Not Started |
| OCT Uploader | ⬜ Not Started |
| Signal Integrity Engine | ⬜ Not Started |
| N-gram Analysis Engine | ⬜ Not Started |
| Campaign Classifier | ⬜ Not Started |
| Lead Quality Scorer | ⬜ Not Started |
| Doctrine Advisor (Claude API) | ⬜ Not Started |
| Daily Scheduler | ⬜ Not Started |
| Weekly Scheduler | ⬜ Not Started |
| Monthly Scheduler | ⬜ Not Started |
| FastAPI Server | ⬜ Not Started |
| Render Deployment | ⬜ Not Started |

### Architecture Status
| Item | Status |
|------|--------|
| Doctrine Rules (DOCTRINE.md) | ✅ Complete |
| Config (logistaas_config.yaml) | ✅ Complete |
| Pattern Library (patterns.yaml) | ✅ Complete |
| Connector Layer | ⬜ Pending |
| Engine Layer | ⬜ Pending |
| Scheduler Layer | ⬜ Pending |
| API Layer | ⬜ Pending |

### PR Context
*Explain how this PR fits the roadmap and what it unblocks.*

---

## 0️⃣ Platform Architecture Rule

The system follows a **single-service, three-layer architecture**.

### Core Stack
- Python backend (connectors, engines, schedulers)
- FastAPI (on-demand API server)
- Render.com (hosting + cron jobs)
- HubSpot CRM (pipeline data source)
- Windsor.ai (Google Ads data source)
- Anthropic Claude API (doctrine analysis engine)

### System Layers
```
Logistaas Ads Intelligence System
 ├── Layer 1: Connectors    (data ingestion — Windsor, HubSpot, Google Ads API)
 ├── Layer 2: Engines       (analysis — signal check, n-gram, classifier, quality)
 ├── Layer 3: Doctrine      (advisory — Claude API with Avverros Doctrine)
 ├── Layer 4: Schedulers    (orchestration — daily, weekly, monthly)
 └── Layer 5: API           (on-demand — FastAPI server on Render)
```

### Forbidden
- Connectors performing analysis (connectors only fetch and save raw data)
- Engines calling external APIs (engines receive data, return findings)
- Schedulers containing business logic (schedulers only orchestrate)
- Any module importing from a higher layer than itself
- Doctrine rules hardcoded in Python (all doctrine lives in `docs/DOCTRINE.md`)

---

## 0.1 Data Flow Rule

**Single direction. No exceptions.**

```
Windsor.ai → connectors/windsor_pull.py → data/ads_*.json
HubSpot    → connectors/hubspot_pull.py → data/crm_*.json
                         ↓
              connectors/gclid_match.py → data/matched.json
                         ↓
              engine/*.py → findings passed to doctrine/advisor.py
                         ↓
              doctrine/advisor.py → outputs/report_*.md
                         ↓
              scheduler/*.py → deliver report
```

**Forbidden:**
- Engines writing to HubSpot or Google Ads (only `oct_uploader.py` may write back)
- Schedulers accessing `data/` directly without going through a module
- Doctrine advisor receiving raw API data (always receives pre-processed engine output)

---

## 0.2 Doctrine Integrity Rule

All recommendations must be consistent with `docs/DOCTRINE.md`.

**The three sacred separations:**
1. Brand vs Non-Brand — NEVER mixed in analysis or reporting
2. Conversions vs Revenue — NEVER equated without CRM verification
3. Platform metrics vs Business metrics — NEVER used interchangeably

**Forbidden in any module:**
- Optimising for CPL alone (must use CPQL)
- Trusting Google Ads conversion counts without HubSpot cross-reference
- Recommending scale without statistical significance check

---

## 0.3 PR Classification Rule

Every PR must declare its category.

**Allowed Types:**
- `Feature` — new module or capability
- `Fix` — bug or incorrect behavior
- `Hardening` — making existing module more robust
- `Refactor` — restructuring without behavior change
- `Doctrine` — updating doctrine rules or config
- `Config` — updating YAML config files
- `Test` — adding or updating tests
- `Docs` — documentation only
- `Migration` — data structure changes
- `Performance` — speed or efficiency improvement

**Required Block:**
```
PR Type:
Module:
Roadmap PR ID:
Related Prior PRs:
Depends On:
Blocks:
```

---

## 0.4 Scope Control Rule

Every PR must declare:

```
This PR does / does not expand functional scope.
```

---

## 1️⃣ Summary / Problem Analysis

### Problem
*Describe the issue or missing feature.*

### Incorrect Behavior (if fix)
*What is currently happening that is wrong.*

### User / System Impact
- Daily pulse not running
- Lead quality scoring incorrect
- GCLID matching broken
- OCT not uploading to Google Ads
- Campaign classification wrong
- Report not delivered

### Location
- `connectors/` — data ingestion
- `engine/` — analysis logic
- `doctrine/` — Claude API integration
- `scheduler/` — orchestration
- `api/` — FastAPI server
- `config/` — YAML configuration
- `docs/` — doctrine and documentation

### What Is NOT Broken
*Explicitly list unaffected modules.*

### Scope Boundaries
*Explicitly define what this PR must not modify.*

---

## 1.1 Root Cause Analysis

**Required:**
- What caused the issue
- Why the previous implementation failed or was missing
- Why the new implementation is correct

---

## 2️⃣ Implementation Plan

Each change must specify:

### File: `connectors/example.py`
**Responsibility:** *What this module is responsible for.*

**Changes:**
- Add X
- Fix Y
- Remove Z

**Forbidden changes in this file:**
- Do not add analysis logic (engines only)
- Do not call Claude API (doctrine only)

---

## 2.1 Interface / Contract Impact

Declare what changes externally visible behavior.

```
Data Output Changed:     None / Yes — describe new schema
Config Change:           None / Yes — describe new keys
Claude Prompt Changed:   None / Yes — describe doctrine impact
API Endpoint Changed:    None / Yes — describe new contract
HubSpot Field Added:     None / Yes — describe field
Windsor Field Added:     None / Yes — describe field
```

---

## 2.2 Data / Config Change Plan

If `config/logistaas_config.yaml` or `config/patterns.yaml` changes:

```
Config File Changed:     Yes / No
New Keys Added:          Yes / No — list them
Breaking Change:         Yes / No
Default Values Set:      Yes / No
Rollback Safe:           Yes / No
```

---

## 2.3 Signal Integrity Impact

**Required if PR touches:**
- GCLID matching logic
- Brand vs non-brand separation
- Conversion counting
- Lead quality scoring
- Campaign classification thresholds

**Must declare:**
- What signal is affected
- How integrity is preserved
- Whether doctrine thresholds changed
- Whether existing classifications change after merge

---

## 3️⃣ Testing Plan

### Connector Validation
```bash
python connectors/hubspot_pull.py
python connectors/windsor_pull.py
```
Verify: data files written to `data/`, correct field names, no API errors.

### Engine Validation
```bash
python engine/signal_check.py
python engine/ngram_analysis.py
python engine/campaign_classifier.py
python engine/lead_quality.py
```
Verify: output matches expected doctrine classifications.

### Scheduler Validation
```bash
python scheduler/daily.py
```
Verify: full run completes, report saved to `outputs/`.

### API Validation
```bash
uvicorn api.server:app --reload
curl http://localhost:8000/health
```

### Regression Areas
- GCLID coverage calculation
- FIX/HOLD/SCALE/CUT classification logic
- Brand vs non-brand split
- OCT upload (do not accidentally double-upload)
- Junk pattern matching

---

## 3.1 Required Test Evidence

```
Automated Tests Added:
Automated Tests Updated:
Manual Validation Performed:
Data Output Sample Attached:
API Response Evidence Attached:
```

---

## 3.2 Regression Risk Statement

*What could break, why it's at risk, what checks were performed.*

---

## 4️⃣ Mandatory Checklist (Definition of Done)

Mark each: ✅ Complete | 🟨 Partial | ❌ Missing

### Phase A — Code Quality
- [ ] Connector only fetches and saves (no analysis)
- [ ] Engine only receives data and returns findings (no API calls)
- [ ] No doctrine rules hardcoded in Python (all in DOCTRINE.md)
- [ ] No API keys in code (all from environment variables)
- [ ] Config values from `logistaas_config.yaml` not hardcoded
- [ ] Data written to `data/` only (not hardcoded paths)
- [ ] Reports written to `outputs/` only

### Phase B — Doctrine Compliance
- [ ] Brand and non-brand are NEVER mixed
- [ ] CPQL used, not CPL alone
- [ ] Conversions cross-referenced with HubSpot before any recommendation
- [ ] FIX/HOLD/SCALE/CUT thresholds match `logistaas_config.yaml`
- [ ] Junk patterns sourced from `config/patterns.yaml`

### Phase C — Local Testing
- [ ] Module runs without error on local machine
- [ ] Data output files are valid JSON/YAML
- [ ] No hardcoded test data left in production code
- [ ] Full scheduler run completes end-to-end

### Phase D — Security
- [ ] No API keys committed
- [ ] `.env` in `.gitignore`
- [ ] No PII (emails, names) written to output files
- [ ] No debug `print` statements left in production paths

---

## 4.1 Operational Readiness

```
Logging added:               Yes / No
Error handling added:        Yes / No
Graceful failure on API down: Yes / No
Environment variable documented in .env.example: Yes / No
Config key documented in logistaas_config.yaml:  Yes / No
```

---

## 4.2 Failure Modes

*Describe what happens when:*
- Windsor.ai API is down
- HubSpot API returns 429 (rate limit)
- GCLID field is empty on a contact
- Google Ads API rejects OCT upload
- Claude API returns unexpected format

---

## 5️⃣ PR Description

### Context
*Why this PR exists.*

### Problem
*What was wrong or missing.*

### Root Cause
*Why it was wrong.*

### Implementation
*What changed.*

### Validation
*How it was tested.*

### Impact
*Behavior after merge.*

### Non-goals
*What this PR deliberately does not do.*

### Risks
*What reviewers should watch.*

### Follow-ups
*Future PRs this enables or requires.*

---

## 6️⃣ Vertical Module Development Rule

Full-feature PRs must include:
- Connector or engine Python module
- Unit test or validation script
- Config update if thresholds added
- Doctrine update if rules added
- `CLAUDE_CODE_BRIEFING.md` updated to mark module as complete

System must remain runnable after each PR (no broken imports).

---

## 6.1 Hardening / Audit PR Exception

Exceptions allowed for:
- Hardening existing connectors
- Doctrine rule updates
- Config-only changes
- Test-only PRs
- Docs PRs

These PRs must explain why full vertical slice is unnecessary.

---

## 7️⃣ Ads Intelligence Hierarchy

The system respects this data hierarchy. Never break it.

```
Google Ads Click (GCLID)
 └── HubSpot Contact (hs_google_click_id)
      └── MQL Status (mql_status)
           └── HubSpot Deal (dealstage)
                └── OCT Conversion Event (Google Ads API)
                     └── Bidding Signal (Smart Bidding learns)
```

**Forbidden:**
- OCT upload without confirmed GCLID
- Lead quality score without MQL status
- Campaign classification without minimum data threshold
- Doctrine recommendation without CRM cross-reference

### Confirmed HubSpot Field IDs (live account 142257138)
```
hs_google_click_id         → GCLID (confirmed populated)
mql_status                 → Lead qualification
hs_analytics_source_data_1 → Campaign name
hs_analytics_source_data_2 → Keyword
ip_country                 → Geography
mql___mdr_comments         → MDR comments (junk signals)

Deal stage IDs:
qualifiedtobuy → Proposal/Implementation Plan
334269159      → In Trials
326093513      → Pricing Acceptance
326093515      → Invoice Agreement Sent
326093516      → Deal Won (primary OCT signal)
379124201      → Lost Deal
```

---

## 8️⃣ Doctrine Document

File: `docs/DOCTRINE.md`

This is the system north star. It is injected verbatim as the Claude API system prompt. Every PR must preserve its integrity.

**Never:**
- Hardcode doctrine rules in Python
- Contradict DOCTRINE.md in engine logic
- Change classification thresholds without updating `logistaas_config.yaml`

### Roadmap Alignment
```
Roadmap Item:
PR Role: Complete / Advance / Harden / Align
Roadmap Deviation: None / Yes
Doctrine Debt Created: None / Yes
```

---

## 9️⃣ Deployment / Rollback Plan

```
Deployment Order:
Requires Coordinated Deploy: Yes / No
Render Cron Jobs Affected:   Yes / No
Rollback Plan:
Rollback Risk Level:         Low / Medium / High
```

---

## 🔟 Post-Merge Verification

```
python scheduler/daily.py    → runs clean
python connectors/hubspot_pull.py → returns contacts
Render deployment successful → Yes / No
Cron jobs registered in Render → Yes / No
```

**Required Block:**
```
Post-Merge Checks:
Owner: Youssef Awwad
Success Criteria:
```

---

## 1️⃣1️⃣ Follow-Up Governance

```
Follow-up PR ID:
Doctrine Debt Description:
Reason Deferred:
Risk of Deferral:
```

---

## 1️⃣2️⃣ Optional Appendices

- Sample data output (JSON snippet)
- Before/after classification table
- Search term examples flagged as junk
- GCLID match rate before/after
- API response evidence
