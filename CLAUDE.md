# CLAUDE.md — Logistaas Ads Intelligence System

Averroes is a **read-only ads intelligence platform**: it joins Google Ads spend
to HubSpot lifecycle and revenue to expose the gap between what Ads reports and
what the business actually earns. It advises; it does not execute. Its
characteristic failure is not a crash but a surface stating a number the
evidence does not support.

This file covers **how to build, test and ship here**, and routes to the
authoritative documents. It does not restate the product or the architecture.

---

## Reading order

| Document | What it is | Trust its status claims? |
| --- | --- | --- |
| `docs/09_REPO_STATE.md` | living per-PR state log, appended by every PR | **Newest sections yes.** Its header is current as of PR-ADS-160-F2; the "Historical status snapshot" block below it is explicitly labelled and stops at August 2026 |
| `docs/DOCTRINE.md` | the governing advisory rules | Yes |
| `docs/03_ARCHITECTURE.md` | layer rules and data flow | Yes |
| `docs/05_DATA_REFERENCE.md` | confirmed HubSpot/Ads field names and IDs | Yes |
| `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md` | the read-only governance policy | Yes |
| `docs/GITHUB_PR_WORKFLOW.md` | PR rules: roadmap ID, dependencies, repo-state update | Yes |
| `docs/NN_*.md` | one doctrine doc per major PR (e.g. `41_PROSPECTIVE_SQL_COVERAGE_BOUNDARY.md`) | Yes |
| `CLAUDE_CODE_BRIEFING.md` | original strategy→build handoff | **No — stale.** Its "what needs to be built" list is years out of date (it lists `api/server.py` and the dashboard as unbuilt; both exist) |
| `docs/07_AGENT_BRIEFING.md` | architecture + layer rules | Architecture yes; its status narrative is explicitly marked stale |

The repo is at **PR-ADS-161A-1**. Any doc describing "Phase 1" as current is
historical.

---

## Environment

Python 3.11. The container's site-packages are not guaranteed to survive a
session restart — if imports fail, reinstall:

```bash
pip install -r requirements.txt
pip install pytest
```

Always invoke pytest as `python -m pytest` (a bare `pytest` may resolve to a
different interpreter than the one holding the dependencies).

---

## Tests

### Reproducing CI locally

CI is one workflow, `.github/workflows/pr-ads-153d-checks.yml`, with a blocking
job of three pytest steps plus static checks. Run them in this order:

```bash
# static
python -m compileall -q analysis api connectors db scheduler services scripts tests
node --check static/app.js
git diff --check origin/main...HEAD          # whitespace in the diff only

# 1. PostgreSQL integration (21 named modules — see the workflow for the list)
# 2. targeted contract suites (23 named modules)
# 3. full suite, with the two baseline deselects below
python -m pytest -q \
  --deselect "tests/test_search_terms_followup_fixes.py::test_daily_empty_search_terms_marks_success_not_success_empty" \
  --deselect "tests/test_search_terms_followup_fixes.py::test_weekly_empty_search_terms_marks_success_not_success_empty"
```

**Those two deselects are a documented baseline.** They fail identically on
`main` with `OSError: Missing required Google Ads env vars`, and CI routes them
to a separate non-blocking job. Anything failing beyond them is yours.

### PostgreSQL tests

PG-backed tests spin up a throwaway cluster. They **skip** unless all of:
postgres server binaries present, an unprivileged `postgres` user exists, and
`sudo -n` works non-interactively (`_have_postgres()` in
`tests/test_pr_ads_153e_a_pg_integration.py`).

A skipped PG suite is **not** merge evidence. CI's did-run assertion fails the
job on any skipped module or any module contributing zero cases — do not
"fix" a red PG step by making it skip.

### There is no JavaScript test harness

No `package.json`, no `node_modules`, no jsdom. CI's only JS step is
`node --check static/app.js`. Do not add a frontend framework for a small
change. Where behaviour in `static/app.js` needs proving, either assert
structurally against the source or extract the literal and evaluate the real
expression with the `node` binary CI already has (see
`test_106` in `tests/test_pr_ads_160_sql_coverage_boundary.py`).

---

## Conventions

- **Work is numbered `PR-ADS-NNN`.** One branch, one PR, patched in place across
  review rounds — do not open a second PR for the same change.
- **Each significant PR adds `docs/NN_<TOPIC>.md`** and appends a section to
  `docs/09_REPO_STATE.md`.
- **Tests live in `tests/test_pr_ads_NNN_<topic>.py`** with numbered,
  sentence-style names (`test_42_a_snapshot_contact_stays_historical...`).
- **Audit/gate scripts are the enforcement layer.** `scripts/audit_*.py` and
  `scripts/audit_*_gate.py` are read-only and exit `0` holds / `1` violation /
  `2` unavailable. Several are wired into CI. Prefer extending one over adding
  a check that lives only in a test.

### Truth doctrine (the thing this codebase is actually about)

Most review cycles here are about a surface reporting something the evidence
does not support. The recurring rules:

- **Unknown is not zero.** A read that failed returns `None`, never `0`.
- **Fail closed.** An unrecognised status is not evidence of success.
- **`false` and `null` are different claims** — one about the pipeline, one
  about us. Both may block; the explanation must tell them apart.
- **Never substitute a proxy for a measurement**, and never present a bound,
  an upper limit or a partial as the thing itself.
- **A guard whose absence changes nothing is not a guard.** New checks should
  be shown failing against the pre-fix code.
- **Never infer a value the data does not carry.** No proxy, no interpolation,
  no derived date standing in for an event that was never recorded.
- **Never weaken a test to satisfy an implementation.** If a test fails because
  production emits something the code does not handle, the code is wrong. Do
  not relabel, normalize or filter production identifiers inside a fixture to
  make an assertion pass — that is the exact defect PR-ADS-160-F1 was opened
  for. Tests are never skipped, disabled or quarantined to reach green.

### High-risk review — use the truth auditor

`.claude/agents/averroes-truth-auditor.md` is an independent, read-only
reviewer. Delegate to it **before merge or production validation** when a change
touches lifecycle/SQL/revenue/freshness/monitoring semantics, canonical dataset
keys, attribution scope, run-status semantics or certification gates — and when
an implementation has survived several review rounds and confidence is running
high. Do not invoke it for copy edits, CSS, renames or mechanical work.

### Read-only governance — ACTIVE

**No writes to Google Ads or HubSpot.** See
`docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md`. HubSpot property history is read;
nothing is written back. Offline conversion uploads (OCT) are not authorized.

---

## Landmines

- **Run status has three outcomes: `success` / `partial` / `failed`.** A
  truncated sync is `partial` on every surface — returned status, sync batches,
  the durable `runs` row, `/api/runs`, canonical dataset freshness and the UI
  strips — and advances no coverage watermark. Do not collapse it into either
  neighbour.

  `finish_sync_batch` has accepted `partial` since PR-ADS-160. Two call sites
  in `scheduler/incremental_sync.py` went on collapsing it to `failed` anyway,
  on a comment asserting the opposite, until **PR-ADS-160-F2**. If you find a
  `"success" if status == "success" else "failed"` anywhere, it is that bug.

- **A monitoring *cadence* is not a *run type*, and a cadence is ONE
  pipeline.** Schedulers persist concrete run types — the incremental sync
  writes `daily_incremental_sync` (`scheduler/incremental_sync.py:274`). The
  translation lives in **one** table, `RUN_TYPE_CADENCE`, via
  `monitoring_cadence()`. A new scheduler writing to `runs` must be added there
  or it goes silently unmonitored; an unrecognised run type maps to `None` and
  is deliberately **not** folded into `daily`.

  This was an open defect through PR-ADS-160 — incremental rows were dropped
  before severity was computed — and the test missed it by rewriting `run_type`
  to `"daily"` before calling monitoring. If you write a test over run health,
  pass the persisted `run_type` through; never relabel it to make an assertion
  pass.

  **PR-ADS-160-F1's fix was itself wrong.** It mapped `daily_incremental_sync`
  onto the `daily` cadence, and `compute_monitoring_status` computes one
  failure streak, one `last_success_at` and one severity PER CADENCE. The 06:00
  pulse then broke the 09:00 incremental sync's failure streak every morning
  and advanced its freshness clock: five days of incremental failures reported
  `green` with no warnings. **Do not map two independent pipelines onto one
  cadence** — they receive one indivisible verdict, and the healthier wins it.
  Corrected in **PR-ADS-160-F2**; see `docs/42_*`. A fixture that carries only
  some of the four registered jobs is not production, and monitoring now says
  so.

- **Never read a SQL total from `analysis/lifecycle_sql_coverage.py`.** Its
  `window_coverage()` answers only "could a complete total exist for this
  window" and sets `cpql_publishable` from that alone — TRUE for windows the
  certification gate refuses. The ONLY production-facing publication verdict is
  `services/canonical_sql_publication_service.publication_for()`, which applies
  every gate (membership, boundary, post-boundary gaps, source freshness,
  recorded reader reconciliation) and fails closed on each. A withheld total is
  `None`, never `0`. An AST guard in
  `tests/test_pr_ads_161a1_sql_publication_contract.py` enforces this; see
  `docs/43_*`.

- **A new canonical freshness status must be registered in five places** or it
  degrades silently to a neutral "unknown": `CanonicalFreshnessStatus.ALL`,
  `SEVERITY_MAP`, `canonical_status_display_label()`, and — as appropriate —
  `HAS_DATA_STATES` / `BLOCKING_STATES`. Then four more in `static/app.js`:
  `_csLabels`, `_csClasses`, `_shortLabels`, `SEVERITY_ORDER`, plus the
  warning/error tallies and the empty-state chain. `_shortLabels` has a
  `|| "Unknown"` fallback that renders a correct summary beside a wrong label.

- **`now()` is transaction-start time.** For an instant stamped *after* a read
  inside the same transaction, use `clock_timestamp()`.

- **One transaction is not one snapshot.** Under the default READ COMMITTED
  isolation PostgreSQL takes a fresh snapshot per *statement*. Reads that must
  agree need `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` before the
  transaction's first query.

- **`ON CONFLICT DO UPDATE SET col = EXCLUDED.col` under a staleness guard is
  not a sparsity guard.** A later payload omitting a property will blank stored
  evidence and report success. Use `COALESCE(EXCLUDED.col, table.col)` for
  columns that carry evidence.

- **`len(requested)` is not `persisted`.** Report what the database wrote.

- **HubSpot's batch history endpoint refuses more than 50 contacts** rather
  than truncating; chunk before calling it.

---

## Git

Work on a feature branch, commit with a descriptive body explaining *why*, and
push with `git push -u origin <branch>`. Never push to `main`.
