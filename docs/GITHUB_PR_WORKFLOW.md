# GitHub PR Workflow
## Logistaas Ads Intelligence System

> This document defines workflow rules that apply to every PR opened against this repository.
> Read `docs/GITHUB_AGENT_BRIEFING.md` for the full operating manual.

---

## PR Sequencing Rules

- Every PR must have a roadmap ID (PR-ADS-XXX).
- Every PR must reference what it depends on and what it unblocks.
- Every PR must update `docs/09_REPO_STATE.md` as its final commit.
- No Phase 2+ PR may be opened until Phase 1 validation criteria are met.

---

## Doctrine Compliance

Every PR must confirm:

- [ ] Phase 1 read-only constraint respected
- [ ] No Google Ads writes
- [ ] No HubSpot writes
- [ ] No external platform mutation
- [ ] Assistant/advisor mode preserved
- [ ] No push/apply/execute controls introduced

See `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md` for the full governance policy.

---

## Negative Candidate Workflow Rule

Negative candidate detection is not the same as negative keyword execution.

Any PR touching N-Gram analysis, negative candidate logic, or search-term waste detection must explicitly confirm:

- [ ] Candidates are read-only — no write operation exists
- [ ] Candidates require human review before any action
- [ ] No external Google Ads write operation exists or was introduced
- [ ] No language in UI, API, or advisor output suggests automatic action
- [ ] No POST, PUT, PATCH, or DELETE endpoint was added for N-Gram or negative candidates
- [ ] Field names follow safe conventions (`review_candidates`, `candidate_terms`, `manual_review_required`, `evidence`, `estimated_spend`, `row_cap_applied`, `source_limitations`) — not (`to_apply`, `push_ready`, `auto_negative`, `apply_negative`, `execute`, `blocked`, `pushed`, `synced`)

Any future negative keyword push workflow remains blocked until the six-month read-only governance review is complete and explicitly approved.

---

## Unsafe Language Check

Before merging any PR, run:

```bash
grep -R "push negative\|apply negative\|pause campaign\|block term\|auto negative\|write to Google Ads\|shared negative" .
```

Expected result: only governance/docs references should appear. No UI/API/advisor output should imply execution.

Also check for unsafe HTTP methods near N-Gram routes:

```bash
grep -R "POST\|PUT\|PATCH\|DELETE" api webapp src | grep -i "ngram\|negative"
```

Expected result: no mutation endpoint for N-Gram or negative candidates.

---

## N-Gram Feature Status

The N-Gram Intelligence block is complete and closed as of PR-ADS-062.
Do not open more N-Gram feature PRs unless a real bug is found.
See `docs/04_PHASE_ROADMAP.md` for the full N-Gram block closure record.
