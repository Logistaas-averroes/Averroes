# 📐 Logistaas Ads Intelligence System
## PR-ADS-001 — Connector Validation & GCLID Match Engine (Phase 1 Foundation)

---

## Roadmap Version
`Avverros Doctrine v1.0 — Revenue Intelligence System`

## Project Countdown
| Scope | Status |
|-------|--------|
| Total Roadmap PRs | ~20 |
| Completed | 0 |
| Remaining | ~20 |
| Platform Completion | ~10% after merge |

## Module Progress
| Module | Status |
|--------|--------|
| Connectors (Windsor + HubSpot) | 🟨 Exists — validating in this PR |
| GCLID Reconciliation | 🟨 Building in this PR |
| OCT Uploader | ⬜ Not Started — unblocked by this PR |
| Signal Integrity Engine | ⬜ Not Started |
| N-gram Analysis Engine | ⬜ Not Started |
| Campaign Classifier | ⬜ Not Started |
| Lead Quality Scorer | ⬜ Not Started |
| Doctrine Advisor (Claude API) | 🟨 Exists — not tested yet |
| Schedulers | ⬜ Not Started |
| FastAPI Server | ⬜ Not Started |

## PR Context
This is the Phase 1 foundation PR. It validates the two existing connectors work against real API credentials, fixes any field mapping errors discovered, and builds `connectors/gclid_match.py` — the critical module that links a Google Ads click to a HubSpot pipeline deal. Nothing downstream (OCT, engine layer, schedulers) can run without this working.

---

## 0.3 PR Classification

```
PR Type:         Feature + Hardening
Module:          connectors/
Roadmap PR ID:   PR-ADS-001
Related Prior PRs: None (first PR)
Depends On:      Nothing
Blocks:          PR-ADS-002 (OCT Uploader), PR-ADS-003 (Signal Check)
```

## 0.4 Scope Control

This PR **does** expand functional scope — it adds `gclid_match.py` as a new module.
This PR **does not** touch engine layer, doctrine, or schedulers.

---

## 1️⃣ Summary / Problem Analysis

### Problem
The system has two connectors (`windsor_pull.py`, `hubspot_pull.py`) and a doctrine advisor (`advisor.py`) but no validated data pipeline connecting them. The critical missing piece is the GCLID reconciliation — matching Google Ads clicks (from Windsor) to HubSpot contacts (via `hs_google_click_id`) to create a unified view of ad spend vs pipeline.

### User / System Impact
- Without GCLID match: zero visibility into which keywords generated which deals
- Without GCLID match: OCT uploader cannot function (needs confirmed GCLID per deal)
- Without GCLID match: lead quality scoring is guesswork, not data

### Location
- `connectors/windsor_pull.py` — validate and harden
- `connectors/hubspot_pull.py` — validate and harden
- `connectors/gclid_match.py` — **BUILD THIS** (new file)

### What Is NOT in Scope
- Engine layer (signal_check, ngram, classifier, quality)
- Doctrine advisor (already exists — not tested in this PR)
- Schedulers
- OCT uploader (next PR)

### Scope Boundaries
This PR must NOT modify:
- `docs/DOCTRINE.md`
- `config/logistaas_config.yaml`
- `config/patterns.yaml`
- `doctrine/advisor.py`
- `scheduler/daily.py`

---

## 1.1 Root Cause Analysis

The connectors were built with correct structure but not tested against live APIs with real credentials. Field names may differ between Windsor.ai API documentation and actual response. The GCLID match module was planned in architecture but not yet implemented — it's the linking pin of the entire system.

---

## 2️⃣ Implementation Plan

### File: `connectors/windsor_pull.py` (HARDEN)
**Responsibility:** Pull Google Ads data from Windsor.ai API.

**Changes:**
- Run against live Windsor.ai API and validate all field names
- Fix any field name mismatches in the response
- Add error handling for 429 (rate limit) and 401 (auth failure)
- Add retry logic with exponential backoff
- Confirm `search_term` data is available on paid plan

**Forbidden:**
- Do not add analysis logic
- Do not call HubSpot API

### File: `connectors/hubspot_pull.py` (HARDEN)
**Responsibility:** Pull CRM contacts, deals, and GCLID data.

**Changes:**
- Run against live HubSpot account (ID: 142257138)
- Validate all confirmed field names against live API
- Confirm `hs_google_click_id` is returned (confirmed populated from audit)
- Add pagination handling for large contact sets (account has 557+ deals)
- Add error handling for 429

**Forbidden:**
- Do not add analysis logic
- Do not call Windsor.ai API

### File: `connectors/gclid_match.py` (BUILD NEW)
**Responsibility:** Join Windsor.ai click data with HubSpot contacts via `hs_google_click_id`. Produce a unified matched dataset that maps every known Google Ads click to its HubSpot outcome.

**What it must produce:**
```python
{
  "gclid": "Cj0KCQjwyr3OBhD0...",
  "contact_id": "750636300494",
  "company": "Contieners",
  "country": "tunisia",
  "keyword": "cargowise",
  "campaign": "Emerging - Markets",
  "match_type": "b",
  "mql_status": "CLOSED - Job Seeker",
  "deal_stage": None,
  "deal_amount": None,
  "quality_flag": "junk",
  "quality_reason": "job_seeker"
}
```

**Logic:**
1. Load `data/ads_search_terms.json` (Windsor output)
2. Load `data/crm_contacts.json` (HubSpot output)
3. Extract GCLID from Windsor click data (via `hsa_kw`, `gad_source`, `gclid` params in URL)
4. Join on `hs_google_click_id` = Windsor GCLID
5. For each matched contact, fetch associated deal stage
6. Output to `data/matched_gclid.json`
7. Output GCLID coverage stats to `data/gclid_coverage.json`

**GCLID coverage metric:**
```
gclid_coverage_pct = matched_contacts / total_paid_contacts * 100
```
Target: >70%. Flag if below.

**Forbidden:**
- Do not perform analysis (that's `engine/signal_check.py`)
- Do not classify leads (that's `engine/lead_quality.py`)
- Do not call Claude API
- Do not write to HubSpot or Google Ads

---

## 2.1 Interface / Contract Impact

```
Data Output Changed:     Yes — adds data/matched_gclid.json, data/gclid_coverage.json
Config Change:           None
Claude Prompt Changed:   None
API Endpoint Changed:    None
HubSpot Field Added:     None
Windsor Field Added:     None
```

---

## 2.2 Data / Config Change Plan

```
Config File Changed:     No
New Keys Added:          No
Breaking Change:         No
Default Values Set:      N/A
Rollback Safe:           Yes — new files only, nothing deleted
```

---

## 2.3 Signal Integrity Impact

This PR establishes the GCLID match rate baseline. Critical doctrine implication:
- If GCLID coverage is below 70%, the system cannot reliably attribute spend to pipeline
- This will be flagged in the daily pulse as a structural integrity alert
- Thresholds sourced from `logistaas_config.yaml` → `doctrine_thresholds.min_gclid_coverage_pct`

---

## 3️⃣ Testing Plan

### Step 1 — Test Windsor connector
```bash
cp .env.example .env
# Fill in WINDSOR_API_KEY and WINDSOR_ACCOUNT_ID
python connectors/windsor_pull.py
# Expected: data/ads_campaigns.json, data/ads_search_terms.json written
# Expected: print output shows total spend and campaign count
```

### Step 2 — Test HubSpot connector
```bash
# Fill in HUBSPOT_API_KEY
python connectors/hubspot_pull.py
# Expected: data/crm_contacts.json written
# Expected: hs_google_click_id populated on contacts
# Expected: GCLID coverage % printed
```

### Step 3 — Test GCLID match
```bash
python connectors/gclid_match.py
# Expected: data/matched_gclid.json written
# Expected: coverage stats printed
# Expected: at least 3 sample matches shown
```

### Regression Areas
- `hs_google_click_id` field name — must match exactly (confirmed: `hs_google_click_id`)
- Deal stage IDs — must use confirmed IDs from `logistaas_config.yaml`
- HubSpot pagination — account has 557+ deals, must page correctly

---

## 3.1 Required Test Evidence

```
Automated Tests Added:       No (manual validation in Phase 1)
Automated Tests Updated:     No
Manual Validation Performed: Yes — run against live APIs
Data Output Sample Attached: Yes — attach snippet of matched_gclid.json
API Response Evidence:       Yes — attach Windsor field list
```

---

## 3.2 Regression Risk Statement

**Low risk.** This PR only adds new files and hardens existing ones. No existing module is modified in a breaking way. The only regression risk is if field name changes in `windsor_pull.py` break an expected key downstream — mitigated by keeping original field names and only adding aliases.

---

## 4️⃣ Mandatory Checklist

### Phase A — Code Quality
- [ ] Connector only fetches and saves (no analysis)
- [ ] No doctrine rules hardcoded in Python
- [ ] No API keys in code
- [ ] Config values from `logistaas_config.yaml`
- [ ] Data written to `data/` only

### Phase B — Doctrine Compliance
- [ ] GCLID required before any join (no GCLID = no match, not an error)
- [ ] GCLID coverage metric calculated correctly
- [ ] Coverage threshold sourced from config (`min_gclid_coverage_pct: 70`)

### Phase C — Local Testing
- [ ] `windsor_pull.py` runs clean against live API
- [ ] `hubspot_pull.py` returns contacts with `hs_google_click_id`
- [ ] `gclid_match.py` produces `matched_gclid.json`
- [ ] No hardcoded test data in production paths

### Phase D — Security
- [ ] No API keys committed
- [ ] `.env` confirmed in `.gitignore`
- [ ] No emails or names in output files

---

## 4.1 Operational Readiness

```
Error handling for API down:        Yes — try/except with clear error message
Rate limit handling (429):          Yes — exponential backoff
Missing GCLID handling:             Yes — skip contact, count in coverage stats
Logging:                            Yes — print statements minimum, structured logging preferred
Environment variables documented:   Yes — in .env.example
```

---

## 4.2 Failure Modes

| Failure | Behavior |
|---------|----------|
| Windsor API down | Log error, write empty `data/ads_campaigns.json`, alert in daily pulse |
| HubSpot API 429 | Exponential backoff, max 3 retries, then fail gracefully |
| `hs_google_click_id` empty on contact | Skip — count in `gclid_without_match` stat |
| Windsor field name changed | Will raise KeyError — must be caught and logged |

---

## 5️⃣ PR Description

### Context
Phase 1 foundation. The system has structure but no validated data flow. This PR makes the connectors actually work and adds the GCLID match — the critical link between ad spend and pipeline.

### Problem
Two connectors exist but have never run against live APIs. The GCLID match module is missing entirely, blocking all downstream work.

### Root Cause
Initial code was written from API documentation, not live testing. GCLID match was deferred to focus on structure first.

### Implementation
- Harden `windsor_pull.py` and `hubspot_pull.py` against live APIs
- Build `gclid_match.py` to join Windsor clicks + HubSpot contacts via GCLID
- Output unified matched dataset to `data/matched_gclid.json`

### Validation
Run all three modules sequentially against live credentials. Verify output files. Check GCLID coverage metric.

### Impact
After merge: Phase 1 is complete. OCT uploader (PR-ADS-002) and all engines are unblocked.

### Non-goals
- Not building OCT uploader (next PR)
- Not building engines (Phase 2)
- Not testing Doctrine advisor (Phase 3)

### Risks
Windsor.ai field names may differ from documentation. Test carefully.

### Follow-ups
- PR-ADS-002: `connectors/oct_uploader.py`
- PR-ADS-003: `engine/signal_check.py`

---

## 9️⃣ Deployment / Rollback Plan

```
Deployment Order:            Connectors → GCLID match
Requires Coordinated Deploy: No
Render Cron Jobs Affected:   No
Rollback Plan:               Delete new files, revert connector changes
Rollback Risk Level:         Low
```

---

## 🔟 Post-Merge Verification

```
python connectors/windsor_pull.py   → data files written
python connectors/hubspot_pull.py   → GCLID coverage printed
python connectors/gclid_match.py    → matched_gclid.json written
```

```
Post-Merge Checks: All three scripts run clean
Owner: Youssef Awwad
Success Criteria: GCLID coverage > 0%, matched_gclid.json contains real data
```

---

## 1️⃣1️⃣ Follow-Up Governance

```
Follow-up PR ID: PR-ADS-002
Doctrine Debt: None
Reason Deferred: OCT uploader requires Google Ads API setup — separate PR
Risk of Deferral: Low — connectors work independently
```

---

## Appendix A — Expected GCLID Match Output Sample

```json
[
  {
    "gclid": "Cj0KCQjwyr3OBhD0ARIsALlo-Oll7NaJnr0vPhSlhp-FgsQq...",
    "contact_id": "750636300494",
    "company": "Contieners",
    "country": "tunisia",
    "keyword": "cargowise",
    "campaign": "Emerging - Markets",
    "match_type": "b",
    "mql_status": "CLOSED - Job Seeker",
    "deal_stage": null,
    "deal_amount": null,
    "matched": true
  },
  {
    "gclid": "CjwKCAjwvqjOBhAGEiwAngeQnYjMdrhgM7dKr1Eww3JuYj48...",
    "contact_id": "747395549402",
    "company": "Deye",
    "country": "lebanon",
    "keyword": "logisys",
    "campaign": "Emerging - Markets",
    "match_type": "b",
    "mql_status": "OPEN - Connecting",
    "deal_stage": null,
    "deal_amount": null,
    "matched": true
  }
]
```

## Appendix B — Coverage Stats Sample

```json
{
  "total_paid_contacts": 312,
  "matched_to_windsor": 187,
  "gclid_coverage_pct": 59.9,
  "alert": true,
  "alert_reason": "GCLID coverage below 70% threshold",
  "contacts_without_gclid": 125
}
```
