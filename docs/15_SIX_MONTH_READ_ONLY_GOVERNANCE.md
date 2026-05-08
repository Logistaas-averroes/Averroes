# Six-Month Read-Only Governance

**Document:** `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md`
**Roadmap ID:** PR-ADS-059
**Phase:** 1.5 — Governance Lock
**Owner:** Youssef Awwad
**Status:** Active governance policy. No code changed. No schema changed. No API changed.

Depends on: PR-ADS-058 / PR-ADS-058A
Unblocks: PR-ADS-060 — N-Gram Stopword Config

---

## 1. Executive Policy

The system must remain read-only for the first six months of production use.

During this period, the system may:

- read data
- store data locally
- analyze data
- summarize evidence
- surface risks
- show dashboards
- produce advisory insights
- help the user decide what to do manually

The system must not:

- write to Google Ads
- write to HubSpot
- push negative keywords
- upload offline conversions
- pause campaigns
- edit campaigns
- change bids
- change budgets
- modify targeting
- modify keywords
- modify CRM contacts
- modify CRM deals
- send automated recommendations to external platforms

---

## 2. Operating Model

The system operates as:

```text
Advisor / Assistant / Analyst
```

Not as:

```text
Operator / Executor / Automation Agent
```

The user remains the executor.

**Workflow:**

```text
System analyzes → System explains → User reviews → User manually acts outside the system
```

**Forbidden workflow:**

```text
System analyzes → System applies change
```

---

## 3. External Write Ban

For six months, the following are prohibited:

### Google Ads

- create negative keywords
- edit negative keywords
- remove negative keywords
- pause campaigns
- enable campaigns
- change bids
- change budgets
- change targeting
- change match types
- upload offline conversions
- create experiments
- change assets
- change ads

### HubSpot

- create contacts
- update contacts
- create deals
- update deals
- change lifecycle stages
- change lead status
- update MQL/SQL fields
- write notes
- create tasks
- modify associations

---

## 4. Local Writes Allowed

The system may write to the local PostgreSQL database for:

- imported source data
- reports
- sync tracking
- freshness state
- local analysis outputs
- audit logs
- read-only evidence tables
- future local review states, if explicitly approved

Local writes must not trigger external writes.

---

## 5. Recommendation Language

**Allowed:**

- evidence suggests
- warrants review
- potential issue
- possible waste
- review this manually
- consider checking
- advisory note
- analyst observation

**Forbidden:**

- apply now
- push change
- add negative
- upload conversion
- sync to Google Ads
- fix automatically
- auto-optimize
- execute
- ready to push
- one-click apply

Normal UI filter language such as "Apply filters" is allowed.

---

## 6. Candidate / Recommendation Rules

During the six-month read-only period:

- Candidate generation may only be local and read-only if approved in a later PR.
- Candidate review may only be local and read-only.
- No candidate can be pushed externally.
- No approval state can trigger an external write.
- Any future candidate UI must clearly say:
  > Manual review only — no platform changes are made.

---

## 7. API Rules

No API endpoint may:

- call Google Ads write APIs
- call HubSpot write APIs
- create external mutations
- trigger platform-side changes

Any endpoint that performs local writes must state:

> Local database only. No external platform write.

---

## 8. UI Rules

No UI may include:

- Push to Google Ads
- Add negative
- Apply negative
- Upload conversion
- Sync to HubSpot
- Auto-fix
- Execute
- One-click apply

Allowed UI actions:

- Refresh
- Filter
- Apply filters
- Load more
- View details
- Export/copy evidence if read-only
- Mark local review state only if approved in a later PR

---

## 9. Scheduler Rules

Schedulers may:

- fetch external data
- persist local data
- update local sync state
- generate local reports

Schedulers must not:

- push data to Google Ads
- push data to HubSpot
- upload conversions
- modify campaigns
- modify CRM records

---

## 10. Six-Month Review Gate

After six months, external write capabilities may be reconsidered only if all are true:

- performance has been stable
- data quality is trusted
- false-positive rates are reviewed
- governance docs are updated
- admin approval workflow exists
- rollback plan exists
- audit logging exists
- user explicitly approves the next phase

Until then, all external writes remain blocked.

---

## 11. PR Checklist Addendum

Every future PR must include:

- [ ] No Google Ads writes
- [ ] No HubSpot writes
- [ ] No external platform mutation
- [ ] Local DB writes only, if any
- [ ] Assistant/advisor mode preserved
- [ ] Manual user execution preserved
- [ ] No push/apply/execute controls

---

## 12. Relationship to Existing Audits

This document reinforces:

- PR-ADS-058 — Negative Candidate Readiness Audit
- PR-ADS-057 — N-Gram Performance Hardening Audit
- PR-ADS-054 — N-Gram Readiness Audit
- PR-ADS-044 — GCLID Attribution Persistence boundaries
- PR-ADS-040 — Search Terms persistence boundaries

If any future document conflicts with this governance lock, this document wins until the six-month review gate is explicitly passed.
