# 62 — Certification runtime: each window built once, in one snapshot

PR-ADS-179A. Read-only. No check, window or payload is removed.

## 1. Why

The daily platform certification (`scripts.audit_platform_certification`)
runs six audits in sequence, each in its own interpreter under a 900 s
per-audit timeout and a resident budget of 345–374 MB on a 512 MB instance.
A timed-out audit is recorded as crashed, which makes the day NOT CERTIFIED
when nothing is actually wrong.

On 25 Nov 2026, 07:30 UTC, the run took 16.5 minutes:

| Audit | Time | Peak RSS |
| --- | --- | --- |
| `reader_cutover` | 476.7 s | 137.8 MB |
| `marketing_outcome_cohorts` | 429.9 s (about 18 s before PR-ADS-177A) | 128.4 MB |
| `campaign_evidence` (now `campaigns`) | 75 s | 105 MB |
| the other three | under 6 s each | — |

Since then, 177B, 178B and 177C added payloads to these audits: the settled
campaign view, the Meta table, the Pipeline page, Overview's shared-name
checks and per-window drawer reads. With more data, the certificate could go
red for no reason.

## 2. Measurement

### 2.1 Fixture

The fixture is one throwaway PostgreSQL 16 cluster, seeded through the
production writers and sync services by a scratch seeding script (not committed).
The content is synthetic but the sizes match production:

| Table | Rows |
| --- | --- |
| Google Ads campaigns | 40 |
| `google_ads_campaign_daily_spend` | 22,578 (575 days, about 19 months) |
| `google_ads_geo_daily_spend` | 120,792 (reconciles to campaign spend exactly) |
| `hubspot_contact_funnel` | 22,000 (every stage, dated and undated SQLs, mapped and unmapped labels) |
| won deals / all deals | 400 / 1,200 |
| companies (full association scan) | 334 |
| legacy `leads` | 27,823 |
| Meta accounts / ad-set day rows | 2 / 10,350 (real daily sync and backfills, fake Graph API) |

The setup also includes FX rates for every day, verified spend and geo
coverage per month, the account time zone, a current deal-ledger sync and a
fresh contact-funnel sync.

On this fixture every window publishes. Deals, revenue and customers are
published in all five business windows and all four evidence windows. The
cohort is published in every window. All three audits exit 0.

### 2.2 Method

Each audit runs as production runs it:
`python -m scripts.certification_child --memory-mb 320 <driver> <audit>`.
The driver calls the audit's own `run()` with the fixed instant
`2026-11-25T12:35:06Z` and dumps the full report. Wall time is measured
around the child process. Peak RSS (`VmHWM`) and peak address space
(`VmPeak`) come from the child's marker line. Build counts come from
counting wrappers on a separate run.

### 2.3 Where the time went (`ea6a7ae`)

These are calls per business window, per audit. "Core" is
`revenue_attribution_service._build_from_db`; "won" is
`canonical_revenue_service.load_won_deals`; "funnel" is
`canonical_crm_funnel_service.build`.

| Audit | core | won | funnel |
| --- | --- | --- | --- |
| `marketing_outcome_cohorts` (9 payloads per window) | 16 (all_time 11) | 30 (21) | 5 (3) |
| `reader_cutover` (13 pages per window) | 22 (15) | 42 (30) | 5 (3) |
| `campaigns` (5 surfaces per window) | 7 (5) | 12 (9) | 2 (1) |

In the cohort audit, one window's 16 cores came from **5 distinct inputs**,
and its 30 won-deal reads came from **3 distinct inputs**. The Dashboard
tabs each rebuild the current and previous-period marts, and the Pipeline
page is built twice: once directly, and once through Dashboard → Deals,
which republishes it.

A cProfile run of the cohort audit (123 s profiled) breaks down as follows:

* the mart core: 82 s cumulative over 75 builds. Inside it, the geo spend
  read (`fetch_geo_daily_spend_by_country`, 120k rows) is 0.34 s per build.
* `load_won_deals`: 30 s over 141 calls. This includes `window_view` and two
  re-reads of `config/thresholds.yaml` per call: 18 s of YAML parsing.
* `canonical_crm_funnel_service.build`: 20 s over 23 calls.
* SQL `execute`: 48 s self time.

`reader_cutover` has the same shape: 103 cores (97 s), 224 won reads and
928 YAML loads.

## 3. Design

### 3.1 The obstacle

Memoising per input looks simple (`docs/59` §11.5). The problem is what
`@single_snapshot` actually guarantees. It is a Python-level cache of the
closed-won universe (`one_snapshot`), read in one REPEATABLE READ
transaction. Every other read a page makes goes through its own
`get_conn()`: spend, geo, the funnel, the sync states and the mart core's
own reads. Each is its own transaction and, under READ COMMITTED, its own
snapshot per statement.

So no page is one database snapshot today. A cached core shared across
pages would join one page's reads to another page's instant, and the audit
would then check a payload no request served.

Pinning everything to one transaction is not free either. Readers open
their own transactions: `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ,
READ ONLY` (which PostgreSQL refuses after the first query), their own
`SET TRANSACTION READ ONLY`, `conn.commit()`, `conn.rollback()` and
savepoints. `@single_snapshot` nesting is not the obstacle, because nested
`one_snapshot()` blocks already share the outermost. The obstacle is that
every reader's transaction contract has to keep working inside a shared
transaction.

### 3.2 One pinned read snapshot (`db/connection.py`)

`pinned_read_snapshot()` takes one pooled connection and runs
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`. It then takes
the snapshot immediately with `SELECT 1`, so the window's instant is the
block's start. Inside the block (a ContextVar, so this context only), every
`get_conn()` yields a view of that one connection:

* each `get_conn()` is a **SAVEPOINT**. A clean exit releases it. An
  exception rolls back to it, so the reader's failed statement is undone and
  the snapshot survives. The reader's own `rollback()` also rolls back to it,
  and `commit()` releases and re-opens it.
* a reader's own `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ[, READ
  ONLY]` or `SET TRANSACTION READ ONLY` is already satisfied, so it is not
  re-sent.
* any other mode raises `PinnedSnapshotError`: READ COMMITTED, SERIALIZABLE,
  READ WRITE, `BEGIN`, `COMMIT` or a bare `ROLLBACK`. This fails closed and
  is never a silently weaker read. A builder that raises becomes
  `cannot_check` (exit 2) in every audit, never a pass.
* the transaction is READ ONLY, so PostgreSQL itself refuses a write.
* on exit, the transaction is rolled back and the connection is returned. A
  broken connection is closed, not pooled.

Outside a pinned block, `get_conn()` is unchanged: every web request, every
sync and every other audit. Threads do not inherit the pin.

### 3.3 A memo bound to that snapshot (`services/snapshot_memo.py`)

`certification_window()` opens, for one block:

* one pinned read snapshot;
* one closed-won universe (`one_snapshot()`, which the builders'
  `@single_snapshot` joins);
* one `MemoScope`.

`@memoised` caches a function's result in the active scope. The key is the
call as the function sees it: `inspect.signature(...).bind`, with defaults
filled in. Every caller gets `copy.deepcopy` of the stored value, so a
caller that mutates its payload cannot change another's.

The memo applies only to:

| Function | What it reads |
| --- | --- |
| `revenue_attribution_service._build_from_db` | spend, geo, FX, coverage, the cohort, won deals |
| `canonical_revenue_service.load_won_deals` | the window's canonical won population |
| `canonical_crm_funnel_service.build` | the contact funnel, the lifecycle stages |
| `canonical_contact_outcome_service.build` | the canonical contact outcomes |

The rules:

* **Outside a certification window the wrapper is a plain call.** Nothing is
  cached for a request.
* **A call with no explicit `now` is never cached.** Its result depends on
  the wall clock, not only the snapshot.
* **A scope is consulted only while its own snapshot is the active one**
  (`MemoScope.valid()`). It cannot outlive or cross a snapshot.
* A call whose arguments cannot be keyed exactly is not cached.
* A function without a `now` parameter cannot be decorated (`TypeError`).

Inside one REPEATABLE READ snapshot, with `now` fixed, each of these
functions is a pure function of its arguments. The same holds for config
and environment, which are constant for an audit's life. So a memoised value
is the value a fresh call would return, and timing fields
(`generated_at`) are the only exception.

### 3.4 The audits

Each business window, and in `reader_cutover` each evidence window, runs
inside its own `certification_window()`:

* **`marketing_outcome_cohorts`**: the nine payloads, plus the audit's own
  independent SQL (`independent_counts`). The independent reconciliation is
  now read at the same instant as the payload it reconciles, so a sync that
  commits mid-audit can no longer cause a false mismatch.
  `global_population_split` stays outside, because it belongs to no window.
* **`reader_cutover`**: the reader-contract read, the 13 pages, the settled
  publication and the window checks. The closed-won universe is still the one
  read once at the start and shared with the truth service, because that is
  the audit's design. Inside a window, every page joins that outer
  `one_snapshot`. The no-writes fingerprint is still taken before and after,
  outside any pin.
* **`campaigns`**: the five surfaces and the drawer population. `run()` now
  takes **one instant for the whole audit**. Before, every builder read the
  wall clock on its own, and a wall-clock call is never memoised.

Nothing is carried from one window to the next. Each window has its own
transaction and its own memo, and it sees whatever committed before it
started.

### 3.5 Alternatives not taken

* **Memoise across the per-page snapshots** (`docs/59` §11.5 as first
  sketched). This is unsound, as explained in §3.1.
* **One snapshot for the whole audit.** This would be cheaper, but it gives
  every window one instant and holds a transaction for minutes. A window
  should describe what a request at that window's instant would have served.
* **Memoise the page builders themselves.** This would hide a page's own
  code from the audit when a page is built twice: Pipeline directly and
  through Dashboard → Deals. Every page builder still runs every time, and
  only the inputs they share are memoised.

## 4. Results

These are the same fixture and the same instant. "Before" is `ea6a7ae`,
"after" is this PR. Each audit ran under `certification_child --memory-mb
320`, two to three times per tree, and the table gives the range.

| Audit | Wall before | Wall after | Peak RSS before | Peak RSS after | VmPeak after |
| --- | --- | --- | --- | --- | --- |
| `reader_cutover` | 84.1–87.5 s | 30.8–32.6 s (−63%) | 109.1–109.9 MB | 113.6 MB | 160.2 MB |
| `marketing_outcome_cohorts` | 63.3–68.6 s | 23.3–24.4 s (−64%) | 103.4–104.2 MB | 107.6–108.1 MB | 153.0 MB |
| `campaigns` | 27.7–36.5 s | 15.0–15.5 s (−45 to −59%) | 98.9–100.7 MB | 104.3–105.1 MB | 149.8 MB |

Peak RSS rises by 4–5 MB: one window's memo, about 6 cores plus their won
populations. That is far inside the 345–374 MB budgets and the 320 MB
address-space cap.

Builds per window after the change, in the order current_quarter,
last_quarter, last_6_months, ytd, all_time:

| Audit | core built / reused | won built / reused | funnel built / reused |
| --- | --- | --- | --- |
| `marketing_outcome_cohorts` | 6/10 ×4, 3/8 | 2–4 / 16–18, 2/11 | 2/3 ×4, 1/2 |
| `reader_cutover` | 6/16 ×4, 3/12 | 3–5 / 24–25, 3/18 | 2/3 ×4, 1/2 |
| `campaigns` | 4/3 ×4, 2/3 | 2–4 / 5–7, 2/4 | 2/0 ×4, 1/0 |

Production ran about 5.5–6.3 times slower than this fixture before the
change: 476.7/87.5 for `reader_cutover`, 429.9/68.6 for the cohort audit.
That points to roughly 170–190 s and 140–150 s after the change, but it is an
estimate, not a measurement. Confirm it on the next daily run.

## 5. Equivalence

### 5.1 On the production-sized fixture

At `2026-11-25T12:35:06Z`, `ea6a7ae` and this PR were compared.

* **Full reports.** Each audit's `run()` report is identical once
  `generated_at` keys are removed. That covers every check and its detail,
  violations, unavailable entries and per-window results:
  * `campaigns`: 44 checks;
  * `marketing_outcome_cohorts`: 107 checks, 5 published windows;
  * `reader_cutover`: 20 checks, 9 windows.
* **Every payload.** A wrapper on every page builder recorded a SHA-256 of
  each payload, with timing fields removed, by (window, builder, arguments)
  and in call order. That covers the mart, the Dashboard tabs, Pipeline,
  revenue-by-source, channel outcomes, the Meta table, the campaign drawers
  and the nested builders they call. The two runs are identical:
  * `campaigns`: 90 of 90 payloads, 18.1 MB;
  * `marketing_outcome_cohorts`: 212 of 212, 39.5 MB;
  * `reader_cutover`: 277 of 277, 56.7 MB.

  `load_won_deals` is called less often (141 → 93 in the cohort audit), with
  the same set of payloads for every argument set.

### 5.2 In CI

`test_06` runs all three audits on the PostgreSQL fixture with the windows
replaced by a null context (the pre-179A behaviour) and with them. All three
reports must be equal, and so must every page payload the audits build, by
builder and arguments and in call order. The report alone was not enough: a
first version compared only reports, and M13 survived it. The audits check
conservation and withholding, which hold even when a previous period is
served the current period's population.

## 6. Guards

All tests are in `tests/test_pr_ads_179a_certification_runtime.py`: 10
PostgreSQL cases and 1 case that needs no database. The module is in CI's
PG step and its did-run list.

| Case | Proves | Fails on `ea6a7ae` | Fails under |
| --- | --- | --- | --- |
| 01_a | a shared input is built once per distinct input; the payloads equal the unwindowed ones | yes | M6 |
| 01_b | a reused value is an independent copy | yes | M1, M3 |
| 01_c | outside a window nothing is cached; a wall-clock call is never cached | yes | M1, M4, M5 |
| 02 | data committed between two windows is seen by the second | yes | M1 |
| 03 | a commit by another session during a window is seen by no reader inside it, including a core first built after the commit | yes | M1, M2 |
| 04 | the pinned view: a reader's REPEATABLE READ / READ ONLY request is satisfied; READ COMMITTED, SERIALIZABLE, READ WRITE, BEGIN, COMMIT and ROLLBACK raise; a write fails and rolls back to its savepoint; commit / rollback keep the snapshot | yes | M2, M11 |
| 05_a | the cohort audit builds all 9 payloads for all 5 windows, each window in its own snapshot | yes | M7, M10 |
| 05_b | `reader_cutover` builds all 13 pages for each business window and channel outcomes for each evidence window, each window in its own snapshot | yes | M8 |
| 05_c | `campaigns` builds its 5 surfaces for each window, each in its own snapshot | yes | M9 |
| 06 | every audit's full report AND every page payload it builds (by builder and arguments, in call order) are identical with and without the windows; the fixture includes a previous-quarter deal so a period mix-up changes a payload | yes | M6, M8, M13 |
| 07 | no database, or a snapshot that could not open: the window yields None, every call builds, no universe is shared (no database needed) | yes | M4, M12 |

The mutations, each applied alone to a copy of this tree
(a scratch script, not committed; the results are in §6.1):

* **M1**: one memo shared by every window.
* **M2**: the pinned snapshot is READ COMMITTED.
* **M3**: a reused value is the stored object, not a copy.
* **M4**: the memo also caches outside a window.
* **M5**: a call with no `now` is cached.
* **M6**: the memo key ignores keyword-only arguments.
* **M7**: the cohort audit drops the Pipeline payload.
* **M8**: `reader_cutover` drops the Meta ad-set table.
* **M9**: the campaigns audit skips Overview.
* **M10**: one snapshot for the whole cohort audit.
* **M11**: any `SET TRANSACTION` is treated as satisfied.
* **M12**: a window shares one universe even without a pinned snapshot.
* **M13**: the memo key ignores the `window` argument.

### 6.1 Mutation results

On the final tree, each mutation was applied alone and the whole module
(11 cases) was run. Every mutation is caught:

| Mutation | Cases that fail |
| --- | --- |
| M1 one memo shared by every window | 01_b, 01_c, 02, 03 |
| M2 READ COMMITTED pin | 03, 04 |
| M3 no copy | 01_b |
| M4 caches outside a window | 01_c, 07 |
| M5 caches a call with no `now` | 01_c |
| M6 key drops keyword-only arguments | 01_a, 06 |
| M7 cohort audit drops Pipeline | 05_a |
| M8 `reader_cutover` drops the Meta table | 05_b, 06 |
| M9 campaigns audit skips Overview | 05_c |
| M10 one snapshot for the whole cohort audit | 05_a |
| M11 any SET TRANSACTION accepted | 04 |
| M12 universe shared without a snapshot | 07 |
| M13 key ignores `window` | 06 |

On `ea6a7ae`, all 11 cases fail: the pinned snapshot and
`services.snapshot_memo` are missing.

## 7. What did not change, and what is left

* No check, window, payload or audit was removed. No gate changed. No
  publication rule changed.
* Web requests are unchanged. They never open a certification window, so
  nothing is cached and every read is its own transaction, as before.
* **Remaining cost.** The geo spend read inside each distinct core (0.3 s on
  this fixture) and the contact funnel read inside each distinct funnel
  build. `canonical_customer_revenue_service.deal_ledger_max_staleness_hours`
  and `association_scan_max_age_hours` still re-parse `config/thresholds.yaml`
  on every won-deal read. That is a follow-up if needed; it is not done here,
  because it changes a production reader outside this PR's scope.
* **Long transaction.** A window holds one READ ONLY transaction for its
  duration: seconds here, expected under a minute in production. It holds
  back vacuum's horizon for that long and takes no locks.
