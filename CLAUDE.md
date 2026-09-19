# CLAUDE.md — Averroes / Logistaas Ads Intelligence System

> Routing and governance only. This file deliberately contains **no**
> architecture detail, no phase narrative and no data dictionary. It tells you
> where truth lives. It is not itself a source of truth about the system.

---

## What this is

A doctrine-driven Google Ads advisory engine for Logistaas (B2B TMS software
for freight forwarders, 80+ countries, 3–12 month sales cycle). It joins
Google Ads spend to HubSpot pipeline outcomes to expose the gap between what
Google Ads reports and what the business actually earns.

It is an analytics platform whose only product is **truth**. A wrong number
shipped confidently is worse than a missing one.

---

## Governance: advisor-only, read-only

Phase 1 is advisor-only for six months of production use. The system analyses
and recommends; the human executes every change manually, outside the system.

Hard constraints on every PR:

* No Google Ads writes. No HubSpot writes. No external platform mutation.
* No push/apply/execute control in UI, API or advisor output.
* No `POST`/`PUT`/`PATCH`/`DELETE` route for N-Gram or negative candidates.
* Field names must not imply execution (`to_apply`, `push_ready`,
  `auto_negative`, `apply_negative`, `execute`, `pushed`, `synced`).

Full policy: `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md`.
Doctrine: `docs/DOCTRINE.md`.

---

## Truth rules (non-negotiable)

* **Never infer data.** Unknown != zero. Unavailable != empty. Partial !=
  success. A subset != a total.
* A creation date is not a lifecycle transition date. A label is not a proven
  canonical identity. A Google Ads conversion is not a qualified business
  outcome.
* Never compare two totals across different populations, attribution scopes,
  grains, date bases, dedup rules or availability semantics.
* When canonical coverage is incomplete, complete totals and CPQL stay
  **withheld** — see `docs/40_LIFECYCLE_SQL_EVIDENCE_COVERAGE.md` and
  `docs/41_PROSPECTIVE_SQL_COVERAGE_BOUNDARY.md`.
* `success`, `partial` and `failed` are three distinct states and must stay
  distinct all the way to the screen.
* **Never weaken a test to satisfy an implementation.** If a test fails, the
  implementation is the suspect until proven otherwise. Never delete an
  assertion, loosen a comparison, rename a production identifier inside a
  fixture, or convert an unavailable value to an empty one to get green.

---

## Which documents are authoritative

| Question | Authority |
|---|---|
| What is actually built right now | `docs/09_REPO_STATE.md` + `git log` |
| Doctrine / advisor-only rules | `docs/DOCTRINE.md`, `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md` |
| Module boundaries | `docs/03_ARCHITECTURE.md` |
| API surface | `docs/API_CONTRACT.md` |
| PR rules and required checks | `docs/GITHUB_PR_WORKFLOW.md`, `docs/GITHUB_AGENT_BRIEFING.md` |
| A specific subsystem's contract | its numbered canonical doc in `docs/` (`33_`–`41_`) or `docs/audits/` |

**Historical, not authoritative:** `CLAUDE_CODE_BRIEFING.md`,
`docs/01_PROJECT_MASTER.md`, `docs/04_PHASE_ROADMAP.md`,
`docs/07_AGENT_BRIEFING.md`. These retain useful architectural content but
their phase/status narratives predate the current code.

**Read status claims skeptically even in the authoritative files.**
`docs/07_AGENT_BRIEFING.md` and `docs/09_REPO_STATE.md` both open with an
"Authoritative status" block dated PR-ADS-153E-B (August 2026) while the
repository has merged through PR-ADS-160. A header is a claim; `git log` and
the code are evidence.

---

## Architecture boundaries

```
connectors/   external reads (Google Ads, HubSpot, Windsor) — read-only
db/           schema, repositories, writers — the only layer that touches SQL
services/     canonical contracts and business truth (canonical_*.py)
analysis/     pure analysis, no I/O
api/          Flask surface (server.py, monitoring.py, scheduler.py, auth.py)
scheduler/    job orchestration
static/       single-page UI (app.js, index.html, styles.css) — plain JS
scripts/      read-only CLIs, audits, backfills
tests/        pytest, PostgreSQL-backed
```

Canonical reads go through the shared service contract, not ad-hoc SQL.
Revenue reads go through `services/canonical_revenue_service.py` at an explicit
attribution scope
(`all_source ≥ google_ads_source ≥ campaign_attributable ≥ gclid_attributable`).

---

## PR workflow

Every PR: a roadmap ID (`PR-ADS-XXX`), a statement of what it depends on and
what it unblocks, the doctrine-compliance checklist from
`docs/GITHUB_PR_WORKFLOW.md`, and `docs/09_REPO_STATE.md` updated as its final
commit. Run the unsafe-language greps in that document before merge.

---

## Use the truth auditor on high-risk changes

`.claude/agents/averroes-truth-auditor.md` is an independent, read-only
reviewer. Its job is the defect class where **a test is green and production
semantics are still wrong** — the PR-ADS-160 case, where an end-to-end test
read the real `run_type = "daily_incremental_sync"` and substituted `"daily"`
before passing it to monitoring.

Delegate to it for: significant PR review; truth-contract hardening; changes to
lifecycle/SQL/revenue/freshness/monitoring semantics; canonical dataset keys;
attribution scope; run-status semantics; certification gates; preparation for
production validation; and any implementation that has survived several review
rounds and is starting to feel safe.

Do not invoke it for copy edits, CSS, one-line renames or mechanical low-risk
work. It is an independent reviewer, not ceremony.

Invoke with: **"Use the averroes-truth-auditor agent to review <target>."**
