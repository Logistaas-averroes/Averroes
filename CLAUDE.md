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
| `docs/09_REPO_STATE.md` | living per-PR state log, appended by every PR | **Newest sections yes.** Its header is current as of PR-ADS-161E; the "Historical status snapshot" block below it is explicitly labelled and stops at August 2026 |
| `docs/DOCTRINE.md` | the governing advisory rules | Yes |
| `docs/03_ARCHITECTURE.md` | layer rules and data flow | Yes |
| `docs/05_DATA_REFERENCE.md` | confirmed HubSpot/Ads field names and IDs | Yes |
| `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md` | the read-only governance policy | Yes |
| `docs/GITHUB_PR_WORKFLOW.md` | PR rules: roadmap ID, dependencies, repo-state update | Yes |
| `docs/NN_*.md` | one doctrine doc per major PR (e.g. `41_PROSPECTIVE_SQL_COVERAGE_BOUNDARY.md`) | Yes |
| `CLAUDE_CODE_BRIEFING.md` | original strategy→build handoff | **No — stale.** Its "what needs to be built" list is years out of date (it lists `api/server.py` and the dashboard as unbuilt; both exist) |
| `docs/07_AGENT_BRIEFING.md` | architecture + layer rules | Architecture yes; its status narrative is explicitly marked stale |

The repo is at **PR-ADS-161E**. Any doc describing "Phase 1" as current is
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

# 1. PostgreSQL integration (named modules — see the workflow for the list)
# 2. targeted contract suites (named modules — see the workflow)
# 3. full suite, with the one baseline deselect below
python -m pytest -q \
  --deselect "tests/test_search_terms_followup_fixes.py::test_daily_empty_search_terms_marks_success_not_success_empty"
```

**That one deselect is a documented baseline.** It fails identically on
`main` with `OSError: Missing required Google Ads env vars`, and CI routes it
to a separate non-blocking job. Anything failing beyond it is yours. (There
were two until PR-ADS-165A: the weekly case was deleted with the weekly job.)

### Production certification (Render shell)

One command runs the six production truth audits and prints one verdict
(`0` CERTIFIED / `1` VIOLATION / `2` NOT CERTIFIED). A crashed audit never
certifies:

```bash
python -m scripts.audit_platform_certification
```

A new production truth audit belongs in its `AUDITS` tuple.

It also runs **daily inside the app** (PR-ADS-161J): the scheduler job
`platform_certification` at 10:30 Asia/Amman (07:30 UTC), after the 09:00
sync, records one row in `platform_certification_runs`. Read the latest
verdict with `SELECT started_at, verdict, summary FROM
platform_certification_runs ORDER BY started_at DESC LIMIT 1`. A run that
cannot execute is recorded as NOT CERTIFIED, never skipped.

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
  some of the three registered run-writing jobs (`daily`,
  `daily_incremental_sync`, `monthly`) is not production, and monitoring now
  says so.

  **A retired job is not a cadence either** (PR-ADS-165A). The weekly job is
  gone, but `runs` keeps its rows and the newest is `failed`. `weekly` is in
  `RETIRED_RUN_TYPES`: it has no cadence, no threshold, no tile and no warning,
  it is never folded into another cadence, and its rows are reported only
  under `retired_runs`, as history. Retiring a job means removing it from
  `RUN_TYPE_CADENCE`, `MONITORING_CADENCES`, `config/thresholds.yaml` and the
  war-room tiles AND adding it to `RETIRED_RUN_TYPES`, or it reads "failed"
  or "never ran" for ever. A dataset the monthly job alone now writes takes
  `MONTHLY_REFRESH_STALE_DAYS` (and `refresh_cadence: "monthly"`), not the
  8-day weekly threshold; one that loses its only writer goes in
  `RETIRED_DATASETS`. A monthly dataset must never be in `CORE_DATASETS` or be
  a page's primary dataset: "fresh" for it means "refreshed on schedule".

  **A failed monthly run is never green** (`LATEST_RUN_MUST_SUCCEED`). The
  streak rule (2) and the 35-day clock both stay silent for a monthly run
  that failed yesterday, so for that cadence the latest outcome is the
  verdict: not `success` is at least yellow. Do not extend it to the daily
  cadences, which retry tomorrow.

- **Never sum a snapshot table over a day window** (PR-ADS-165A). The legacy
  `geo` / `keywords` tables hold whatever days the last monthly run pulled, so
  `SUM(...) WHERE run_date >= NOW() - N days` returns nothing or part of the
  window mid-month — and the Countries page printed that as Spend `$0` under
  "Fresh with data". A page figure over a window comes from a daily canonical
  table AND its coverage ledger (`services/country_evidence_service.py`
  judges the ledger's chunks with `analyze_settled_coverage`, which must
  certify every day), and an uncovered window
  renders "Unavailable" with the covered range — never `$0`, never the sum of
  the days that happen to be present.

  **A `verified` chunk is not a complete day.** The daily sync marks its
  chunk verified through TODAY at ~06:00 UTC, so chunk status alone calls a
  day with six hours of spend "covered" — and yesterday stays that way until
  the next morning's sync. A day is complete only if a verified chunk
  containing it has `fetched_at` at or after the END of that account-local
  day (`settled_through`), in the account time zone the sync itself reads
  (`repo.fetch_account_time_zone`, never a constant; unknown is unavailable).
  The window ends yesterday, is published from the morning sync onward, and
  is "Unavailable" before it; say that, do not promise it is always
  available.

  The rule lives ONCE (PR-ADS-167A): `google_ads_spend_service.
  analyze_settled_coverage`, applied to geo by `analyze_published_geo_coverage`
  for every surface that publishes country spend (Countries, the revenue
  attribution contract behind Country Revenue & Spend / the mart / Dashboard
  Countries, the geo reconciliation panel). Today is never settled. Owner
  decision (2026-11-20): for THOSE country consumers only, a window that
  includes today ENDS AT YESTERDAY (account-local) — one rule,
  `canonical_contract.end_at_settled_day` / `resolve_settled_window`; the
  mart's country view (`SETTLED_DAY_VIEWS`) builds `build_revenue_attribution(
  ..., settled_days=True)`. Deals come from the reader contract with
  `load_won_deals(..., bounds=settled_revenue_bounds(...))`: account-local
  midnight of the first day to account-local midnight of today — the SAME
  days as the settled spend. Never resolve the window key on the UTC day and
  narrow it (that published Q3's deals under an empty Q4 window at 00:30 BST),
  never a page-side filter. The window block declares it (`through_reason:
  today_not_settled`, `window_range` with "Through <date> — today is not
  included until tomorrow morning's sync."). A window with no completed day
  (`window_range.empty`) publishes nothing: every metric is unavailable with
  `no_settled_day_in_window`, never a "verified" zero. Closed windows are
  unchanged (their deals keep the UTC-midnight business bounds: an hour off
  their London spend days under BST — a documented gap), and every other
  revenue page keeps today, so the country pages may differ from them by
  today's deals, and say so. Before the morning sync yesterday is not
  settled: country spend is withheld (`spend_not_reread_since_day_ended`) and
  Dashboard Countries withholds the KPIs AND every row's and the residual's
  same figures — rows that sum to a withheld total publish it. The parity
  audit compares a consumer that declares that window with the settled window
  it resolves itself (and with the settled reference
  `revenue_decision_mart/country` for coverage proof and SQL conservation) —
  never with a window one day longer, and an undeclared shorter window is
  still a mismatch. Do not "fix" any of this by judging
  today on chunk status, or by ending other pages' windows. `analyze_geo_coverage` /
  `analyze_coverage` still mean "was every day FETCHED?" — the sync's and the
  repair job's question; never publish from them. Campaign spend coverage
  (campaign ROAS, the Dashboard) and Campaign Evidence (no coverage gate at
  all) do not apply the rule yet — known gaps in `docs/09`.

  Do not fix a stale-snapshot page with a disclosure or a wider staleness
  threshold. Known remaining offenders are listed in `docs/09`.

- **Never join countries on display text, and never in the browser.** The
  Countries page joined spend to leads on the trimmed country string: `UK`
  leads beside "United Kingdom" spend were two rows and the lead row read
  `$0` in a covered window. Join ON THE SERVER, on an ISO code.

  `country_identity.resolve` / `SUPPORTED_COUNTRIES` is a supported-MARKET
  registry (96 codes) for the revenue pages; do not resolve Google Ads spend
  or free-text lead countries through it — production spend is in 162 codes
  and most of the page would read "(unresolved location)". Key spend by the
  code its Google criterion carries; resolve lead text with
  `resolve_iso_country_text` (the complete ISO table in
  `analysis/iso_countries.py`, exact matches only — bare "Congo", "CA", "IN"
  match nothing). Text that names no single country is its own row,
  "Unavailable — country not matched" — never 0; blanks and placeholders are
  one "No country recorded" row.

  **A geo row's own `country_code` can be blank for a country that has one.**
  Rows written before PR-ADS-167A by a sync whose lookup failed are NULL, and
  a location first seen on a day the lookup fails still is. Read a
  criterion's code from ANY stored row of that criterion — the one statement
  `db/geo_identity_sql.py`, used by every reader and by the writer: one code
  resolves, none or two that disagree do not, and nothing is guessed (not
  even Google's 2000 + ISO-numeric convention). Never `MAX(country_code)`.
  The sync names criteria from storage first, asks Google only about the
  rest, never writes NULL over a stored code, and a failed lookup makes the
  run `partial` (`country_lookup_failed`) — the chunk stays `verified`
  because the spend is real.

  A country with no spend row is `$0` in a covered window when no spend in it
  can be its own: no unresolved spend, OR every unresolved criterion is
  code-less everywhere and the country's code is carried on record by exactly
  one criterion that carries nothing else (`fetch_geo_code_carriers`; one
  country, one criterion). A country never seen in geo spend stays
  "Unavailable — some spend in this window has no country" while there is
  any. A country with a row is complete. `fmtDollar` prints `$0` for 39
  cents: beside a rule that `$0` is measured, render "<$1".

- **Never read a SQL total from `analysis/lifecycle_sql_coverage.py`.** Its
  `window_coverage()` answers only "could a complete total exist for this
  window" and sets `cpql_publishable` from that alone — TRUE for windows the
  certification gate refuses. For the **lifecycle-SQL window contract**, the
  only production-facing publication verdict is
  `services/canonical_sql_publication_service.publication_for()`, which applies
  every gate (membership, boundary, post-boundary gaps, source freshness,
  recorded reader reconciliation) and fails closed on each. A withheld total is
  `None`, never `0`. An AST guard in
  `tests/test_pr_ads_161a1_sql_publication_contract.py` enforces this; see
  `docs/43_*`.

  This does **not** cover every SQL publication rule in the product — and it
  does not cover Campaign Evidence, which (PR-ADS-161B) publishes a
  *different metric family*. See the next landmine.

- **"SQLs in this period" is two different metrics.** An **acquisition
  cohort** (`acquisition_cohort_outcomes`, windowed on `contact_created_at`) asks
  how many contacts *acquired* in the period have reached SQL as of the
  canonical contact-funnel watermark; it
  needs no SQL-entry date, so a stage-proven contact with no timestamp is
  counted. A **lifecycle event** (`lifecycle_stage_events`, windowed on
  `date_entered_sql`) asks how many *entered* SQL in the period; it needs an
  exact timestamp for every contact and stays under the gate above. Campaign
  Evidence and its CPQL use the cohort (`services/marketing_outcome_cohort_service.py`,
  `docs/44_*`); `static/app.js::campaignSqlPublication` reads
  `cohort.sql_status`. Never relabel one as the other, never fill a missing
  SQL-entry date from creation, a boundary or a sync time to move a contact
  between them, and keep the per-response `metric_family` / `window_basis`
  metadata on anything that publishes either.

  The cohort publishes only when `analysis/sql_coverage_freshness.assess`
  returns `source_fresh` or `source_stale`. That function returns `fresh=False`,
  **not** `None`, for a missing sync state, an incomplete bootstrap or a failed
  incremental. So a gate on `fresh is None` alone publishes a partial
  population. Drive freshness tests through the real `assess`, never a
  hand-built verdict.

  **Withheld means absent, not hidden.** When the cohort verdict is not
  `published`, the `/api/campaigns` payload carries no cohort SQL count, SQL
  breakdown, CPQL or funnel-wide reached-SQL count anywhere
  (`audit_marketing_outcome_cohorts.withheld_exposures` must be empty), and
  `static/app.js` renders the withholding word — never the number with a "not
  published" suffix. The one stated exception is the legacy family declared in
  `legacy_sql`, which no Campaign Evidence surface may read (enforced by
  `audit_campaign_evidence_certification`). The gate is `campaignSqlPublication(cohort)`:
  the cohort is an **argument**; page surfaces pass the page response's, the
  drawer passes its own `/api/campaign-detail` response's. A gate that reads
  page-global state authorises a drawer opened from another page with another
  request's verdict. Closed-won deals are not published on Campaign Evidence
  (`docs/44` §5); never call deals customers.

- **"Moved to Customer stage" is a lifecycle event, never a customer**
  (PR-ADS-166A, `docs/49_*`). The Leads page's stage count
  (`hubspot_contact_funnel.date_entered_customer`) counts contacts whose
  HubSpot lifecycle stage was changed, old contacts edited in bulk included:
  219 for Q3 2026 against 8 won deals. Never label it "Customers", add it to,
  or derive it from a customer figure. "Customers won" comes only from the
  reader contract (`build_customers_won` → `load_won_deals`); its
  `won_customers` list exists only when the count is published. The event's
  one label is `crm_lifecycle.EVENT_LABELS` ("Moved to Customer stage"), on
  the Overview panels too; a conversion into it reads "→ Customer stage".
  For names: both stamps NULL means *not loaded yet*, `names_read_at` set with
  a NULL name means *HubSpot has none*, `names_last_asked_at` alone means
  *not returned by HubSpot* — never queue or label on `first_name IS NULL`.

- **A post-boundary incident closes only on stored exact evidence, decided by
  the database.** Both resolvers (`apply_post_boundary_sql_evidence`,
  `resolve_post_boundary_incidents`) close a row only where the same statement
  reads `date_entered_sql` or a recovered `sql` history row back. Never add a
  closing status (`bounded_but_not_exact`, …): the gate counts `status = 'open'`,
  so a new status silently stops blocking. `last_known_below_sql_at` /
  `first_observed_at_or_above_sql` are BOUNDS — for a stage jump the latter is
  an opportunity timestamp — and are never an SQL date. Whose gap an incident
  is: `scripts/audit_post_boundary_sql_incidents.py`; see `docs/45_*`.

- **A won deal is not a customer.** A customer is a distinct HubSpot company
  id from `hubspot_deal_company_association` (PR-ADS-161D2), never a deal,
  contact, email domain, name or campaign. The per-deal
  `hubspot_deal_company_observation` row decides what the bridge means: no
  bridge rows with `success_zero_companies = true` is a CRM finding ("no
  company"); no successful observation is `association_unavailable` — never
  read the absence of rows as either. A failed lookup must never delete or
  replace the last successful set, and writes are ordered by observation START,
  not commit order. Customers publish only when every member deal is resolved
  AND current AND the latest FULL association scan succeeded and STARTED within
  `customer_identity.association_scan_max_age_hours` (a stopped pipeline moves
  no per-deal row, so per-deal staleness alone cannot see it); otherwise the
  lower bound and unresolved counts publish instead. A complete scan is
  proven set-for-set: it stores the deal ids it certified, and backfill
  completion re-derives that set inside its own transaction with the deal
  ledger SHARE-locked — never from a caller's count or an earlier progress
  read (PR-ADS-161D2-F1). The publication gate requires that certified set to
  EQUAL the current won set (and the recorded population to match it); a deal
  won since the last full scan withholds customers even when a subset pass has
  observed it. The audit and the gate share this rule — never let one judge
  coverage the other does not (PR-ADS-161D2-F3).
  The won deals that block a customer or revenue total are named by the CRM
  Truth Exceptions workbench (PR-ADS-191, `docs/47_*`), which consumes these
  verdicts and never restates them.
  The won predicate is `hs_is_closed_won IS TRUE`; stage `326093516` is a
  cross-check whose disagreement withholds, never a filter. See `docs/46_*` §5.

- **Every production reader of closed-won deals, customers, revenue, a spend
  ratio or SQL-to-customer reads the reader contract** (PR-ADS-161E, `docs/48_*`):
  `canonical_revenue_service.load_won_deals` for the window's population and
  `reader_row` / `subset_metrics` for a row's figures, inside ONE snapshot
  (`@single_snapshot` on the page builder). A page never decides publication:
  it republishes `{value, status, reason}`. `closed_won_deals` and `customers`
  are two fields. A row's customers are distinct companies and are **never
  summed** across rows. SQL-to-customer, revenue-per-SQL and
  lead-to-customer are unavailable: two cohorts.

  **Attribution ROAS and CAC are unavailable** (`certified_roas` /
  `certified_cac`). Close-date outcomes over same-window spend are not one
  acquisition cohort in a long B2B cycle. That calculation exists only as
  two non-cohort operational ratios:
  * `period_revenue_per_ad_spend` ("Period Revenue / Ad Spend");
  * `period_spend_per_customer_won` ("Spend per Customer Won This Period",
    divided by customers, never by deals).

  Reader-contract rows get them from `derive_period_revenue_per_ad_spend` /
  `derive_period_spend_per_customer_won`, with `ratio_kind` and the
  disclosure. The Campaigns, Countries and Channels dashboard rows divide the
  same published inputs themselves and carry no metadata. Those rows return
  None, not 0, for zero revenue. The UI shows the disclosure beside every
  period value. **Never put a figure under a key named `roas` / `cac`
  (`geo_roas`, `google_ads_roas` …) or a "ROAS"/"CAC" label on a period
  ratio — nor ROAS/CAC prose in any API string.** Audit check 18
  (`no_period_ratio_named_roas_or_cac`) fails on any of these (`docs/48_*` §4).

  **A stale deal ledger publishes nothing** (PR-ADS-161F). Past
  `deal_ledger.max_staleness_hours` (36h) since the last successful incremental,
  deals, customers and revenue are `unavailable` with `canonical_ledger_stale`.
  Test fixtures that mean "a current ledger" use `fresh_sync_state(now)`, never
  the fixed-date `READY_SYNC_STATE`.

  `revenue_integration_connected()` (an existence check over
  `gclid_attribution`) is deleted. Never gate canonical figures on a legacy
  table again.

  A new reader must be added to `INVENTORY` in
  `scripts/audit_customer_revenue_reader_cutover.py`, or that audit fails.
  The UI renders these metrics with `canonicalMetricHtml` / `rowMetricHtml`:
  0 for a published zero, and "Withheld" or "Unavailable" with the reason
  otherwise, never a dash. An admin also gets "View blockers" into the CRM
  Truth Exceptions workbench.

- **A won deal takes a Channels row only when EVERY associated contact
  agrees** (PR-ADS-168A, `docs/50_*`). The channel is each contact's
  `hubspot_contact_funnel.hs_analytics_source` through
  `channel_funnel_service.source_key`, read in the closed-won universe's own
  snapshot; any disagreement, missing contact, failed lookup, contact outside
  the funnel or blank source puts the deal on `__deal_source_unresolved__`
  ("Source not resolved") with its class. Never place a deal by the ledger's
  `source_primary_raw` (the lowest contact id's source when contacts
  disagree), never by GCLID (a GCLID deal HubSpot recorded as Direct is
  Direct), and never fill the unresolved row from a guess. The rows must add
  back up to the window's deal count or every row withholds
  (`channel_partition_does_not_reconcile`). Channel outcomes are deals CLOSED
  in the evidence window's days ended at yesterday — a different cohort from
  the funnel's contacts acquired: no rate joins them. Google Ads spend
  is compared only with the "Google Ads (canonical evidence)" line, never
  with the Paid Search row (its ratios are `incompatible_rate(
  channel_population_is_not_spend_population)`, its spend cell
  `spend_shown_on_google_ads_line`). The line is the platform's Google Ads
  evidence (the truth's campaign_attributable + google_ads_unplaced): a
  GCLID on any row, plus the Paid Search classification, minus labels
  approved as not Google Ads. It does NOT by itself exclude other search
  engines — never describe it as if it did; its `evidence_split` shows how
  much rests on a click ID. Every other row is `channel_spend_not_connected`.

- **A new canonical freshness status must be registered in five places** or it
  degrades silently to a neutral "unknown": `CanonicalFreshnessStatus.ALL`,
  `SEVERITY_MAP`, `canonical_status_display_label()`, and — as appropriate —
  `HAS_DATA_STATES` / `BLOCKING_STATES`. Then four more in `static/app.js`:
  `_csLabels`, `_csClasses`, `_shortLabels`, `SEVERITY_ORDER`, plus the
  warning/error tallies and the empty-state chain. `_shortLabels` has a
  `|| "Unknown"` fallback that renders a correct summary beside a wrong label.

- **Never page a HubSpot search by offset over data that moves** (PR-ADS-161I).
  The contact scan re-sent an advancing watermark as the filter while keeping
  the offset, skipped ahead every page, and "completed" after 2,000 of 21,751
  contacts. Even with a frozen filter, an offset over the live result set
  skips a contact whenever an already-read one is modified mid-scan. Page by
  keyset (each page a new `>= watermark` query, ties de-duplicated; a tie group
  that fills a page is walked by `hs_object_id GT last`, never by offset), end a scan
  only when HubSpot's own `total` is met (`_assert_scan_complete`), and rewind
  the watermark when a scan proves itself incomplete. A fake HubSpot in a test
  must honour the filter, report `total` and change its data during the scan,
  or it cannot see this class of bug.

- **A modified-since sync never sees a deletion or a merge** (PR-ADS-163C).
  A contact deleted in HubSpot, or merged into another, is not "modified": it
  stops being returned and its row stays in `hubspot_contact_funnel`, counted
  by every funnel and cohort read (30 rows on 2026-11-18). Removal is: search
  → two strikes → direct read → limits → move. **Search**
  (`check_contacts_exist`, `hs_object_id IN` batches of 100, ids compared as
  numbers): unknown is not missing — a batch with no usable answer strikes
  nobody, and a run with any unanswered batch removes NOTHING. **Two
  strikes:** missing on two checks at least 20 hours apart, the second in the
  current run, the row not ingested since that run started. **Direct read**
  (`confirm_contacts_absent`, `get_by_id`): a search index can omit a LIVE
  contact on every run, so nothing is removed on the search's word alone —
  only a 404 by id (or a merge survivor that names the id) confirms; a
  contact the read finds has its strike cleared and makes the run `partial`.
  **Limits, counted over the backlog and over time:** the valve counts EVERY
  contact that qualifies, whichever run struck it (50, or 1% of the table),
  and the last 7 days' removals plus this run's are capped too (100, or 2%);
  over either, nothing moves and the run is `refused`. That many FIRST
  strikes in one run is `partial`. **A move decided by the database:** all of
  it is one statement in `db.writers.move_absent_contacts`, the confirmed ids
  a parameter — never flag a row in place (thirty queries read that table)
  and never act on a count read earlier. **Held:** a contact with an open
  `sql_post_boundary_incident` is not removed (the resolvers read its funnel
  row), is not weighed by the valve, and stays counted until the incident is
  resolved; never add a closing status for it. A column added to
  `hubspot_contact_funnel` must be added to the archive table too
  (`test_pr_ads_163c_removed_contacts.py::test_22`). The legacy `leads`
  table, lifecycle history and boundary tables are not covered.

- **A HubSpot pull that hits an API or transport error, an exhausted retry or
  a malformed response raises; it does not return what it has** (PR-ADS-163A).
  The legacy contact pulls in `connectors/hubspot_pull.py` logged an API error
  and returned `[]` or the pages read so far: with an expired token the monthly
  job recorded `success`, stamped `hubspot/contacts` fresh and overwrote
  `data/crm_contacts.json` with `[]`. Raise `HubSpotPullIncompleteError`, bound
  every retry, do not retry a 404, and never `break` or `return` out of an
  `except` in a pull. A 200 with no `results` list is not an empty answer, and
  a deal returned with `lookup_failed: True` is not a row to write. The SDK
  has one `ApiException` per API package (the contacts one does not catch a
  deals error) and lets urllib3 errors through: catch `HUBSPOT_SDK_READ_ERRORS`.
  A TLS failure is the exception: the SDK dies on it with a raw
  `AttributeError`, which is loud but not the named error.
  This is not a completeness check — those pulls still page by offset and
  never read `total`, the landmine above, which this did not address.

- **`now()` is transaction-start time.** For an instant stamped *after* a read
  inside the same transaction, use `clock_timestamp()`.

- **A `try/except` around a query does not isolate it.** PostgreSQL aborts a
  transaction on its first error, so every later statement on that cursor
  fails with "current transaction is aborted", whatever it asked
  (PR-ADS-163B: one freshness registry entry naming a missing column blanked
  the row count of every dataset after it, and the latest-batch read for all,
  on every request). Independent diagnostic reads that share a cursor go
  through `db.isolated_read.execute_isolated` (a savepoint). A mock cursor
  that lets the statement after a failed one succeed cannot see this class of
  bug; test it on a real database. And every `(table, date_column)` in
  `DATASET_FRESHNESS_CONFIG` must exist in the schema `init_db()` builds —
  `tests/test_pr_ads_163b_freshness_row_counts.py::test_01` is the guard.

- **One transaction is not one snapshot.** Under the default READ COMMITTED
  isolation PostgreSQL takes a fresh snapshot per *statement*. Reads that must
  agree need `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` before the
  transaction's first query.

- **`ON CONFLICT DO UPDATE SET col = EXCLUDED.col` under a staleness guard is
  not a sparsity guard.** A later payload omitting a property will blank stored
  evidence and report success. Use `COALESCE(EXCLUDED.col, table.col)` for
  columns that carry evidence.

- **`len(requested)` is not `persisted`.** Report what the database wrote.

- **The HubSpot SDK has one `ApiException` per package** (contacts, deals,
  companies …) with no common base. Catch `HUBSPOT_API_EXCEPTIONS` (or
  `HUBSPOT_SDK_READ_ERRORS` where transport failures are retried), never the
  contacts class around another package's call (PR-ADS-166B).

- **HubSpot's batch history endpoint refuses more than 50 contacts** rather
  than truncating; chunk before calling it.

---

## Git

Work on a feature branch, commit with a descriptive body explaining *why*, and
push with `git push -u origin <branch>`. Never push to `main`.
