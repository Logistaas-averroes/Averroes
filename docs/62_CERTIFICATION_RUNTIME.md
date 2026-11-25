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
pages would join one page's reads to another page's instant. The audit would
then check a combination of reads that neither a request nor a snapshot
ever produced.

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
  READ WRITE, `BEGIN`, `COMMIT` or a bare `ROLLBACK`. The check applies to a
  `str`, `bytes` or `psycopg2.sql` statement, through `execute` or
  `executemany`. This fails closed and is never a silently weaker read.
* a statement that ends the transaction anyway is caught when it returns.
  The snapshot is then marked broken, and every later statement and
  `get_conn()` in it is refused. On 179A this held only for a statement that
  left the connection IDLE (`SELECT 1; COMMIT`). `SELECT 1; COMMIT; BEGIN
  ISOLATION LEVEL READ COMMITTED`, `ROLLBACK AND CHAIN` and a commit through
  the raw connection were not caught when they ran: the next statement in the
  same block ran outside the snapshot. PR-ADS-179B closes that (§8.3).
* a reader that catches its own failed statement leaves the transaction
  aborted. On the reader's clean exit, the view rolls back to the reader's
  savepoint (PR-ADS-179A-F1). Outside a window, `get_conn`'s commit already
  rolls back an aborted transaction quietly, so this matches it.
* every refusal is **recorded on the snapshot as it is raised**
  (`_Pin.violations`). After each window, each audit reads that record
  (`snapshot_memo.refused_reads`). A window with any refusal is reported
  `cannot_check` (cohorts), "unavailable" (campaigns) or a page not run
  (`reader_cutover`), and each of those is exit 2. This holds even when a
  reader caught every exception and published "unavailable" instead.
  `test_10_b` shows a swallowed refusal certifying with exit 0 on `91290f7`.
* a `PinnedSnapshotError` raised outside any `try` in the audit is different.
  For example, `reader_cutover` calls `load_won_deals` directly in its window
  loop. That error ends the audit's process, and the platform certification
  records the audit as **crashed**, which makes the day NOT CERTIFIED rather
  than exit 2. Either way, it is never a pass.
* the transaction is READ ONLY, so PostgreSQL itself refuses a write.
* on exit, the transaction is rolled back and the connection is returned. A
  broken connection is closed, not pooled.

**Locks, and the deploy migration (PR-ADS-179A-F1).** A window holds
AccessShareLock on every table it has read until it ends. `init_db` runs the
startup DDL from the lifespan hook (`api/server.py`): one transaction of 49
`ALTER TABLE … ADD COLUMN IF NOT EXISTS` and many `CREATE … IF NOT EXISTS`,
which take AccessExclusiveLock even when there is nothing to add.

Run mid-window, the two closed a lock cycle. `init_db` held `leads` and
waited for a table the window had read; the window then read `leads`.
PostgreSQL aborted one side. If it aborted the audit's read, the day was NOT
CERTIFIED for no data reason. If it aborted `init_db`, it only logged
"init_db failed" and **the new release ran without its schema migration**.
`test_08_a` reproduces it on `91290f7` as `DeadlockDetected`.

Both sides now take one advisory lock, `SCHEMA_LOCK_KEY`, before any table
lock:

* **A window** takes it SHARED at session level, in its own short
  transaction, before its snapshot is taken. It waits at most
  `WINDOW_SCHEMA_LOCK_WAIT_SECONDS` (60 s) for a running migration. Past that
  it runs unpinned, which is the pre-179A behaviour: every read on its own,
  nothing cached, and a warning logged. Since PR-ADS-179B each audit's report
  also says so, per window (§8.4).
* **`init_db`** takes it EXCLUSIVE as the first statement of its DDL
  transaction. It waits at most `SCHEMA_LOCK_WAIT_SECONDS` (180 s) for the
  windows already open. PostgreSQL queues lock requests in order, so a window
  that starts later waits behind it, and the wait is normally at most one
  window. While it waits it holds no table lock, so it blocks neither the
  window nor any web request.
* **Past the bound, `init_db` raises `SchemaMigrationBlocked`** and logs at
  ERROR. The lifespan hook then fails, the instance does not start, and
  Render's deploy health check fails the deploy and keeps the old release.
  A lock wait past the bound therefore never yields a release running
  without its schema. That is the only guarantee: every other `init_db`
  error is still logged and swallowed, as before 179A (§8.2).

Neither side ever waits while holding a table lock the other needs, so no
cycle is possible. On this fixture the longest window is about 6 s; at
production's measured 5.5–6.3× slowdown that is about 40 s. 180 s is 4–5
times that, and well inside Render's deploy health-check window. The
health-check window is documented as minutes, not seconds; I did not
re-measure it from this session. `test_08_a` to `test_08_c` cover the
protocol, the loud bound and the window-side bound.

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
is the value a fresh call in the same snapshot would return, and timing
fields (`generated_at`) are the only exception.

**What this certifies, exactly (PR-ADS-179A-F1).** Certification now checks
a **snapshot-consistent build** of each page: every read the page makes,
taken as of one instant. The exception is a window that ran unpinned (§3.2,
"Locks"): there each read is its own transaction, as before 179A, and since
PR-ADS-179B the report says so for that window (§8.4). A web request does not run in a window. It still
reads statement by statement under READ COMMITTED, so its reads can straddle
a sync's commit (the PR-ADS-161E / 173A class of torn read).

When nothing commits during a request, the request serves exactly what
certification checked. When something does commit mid-request, it may not.
Certification cannot see such a torn read by construction. Before 179A it
saw one only by chance, when a commit happened to fall inside the audit's own
page build. The guard against torn reads is still `@single_snapshot` and the
per-reader REPEATABLE READ transactions; this PR neither strengthens nor
weakens them.

### 3.4 The audits

Each business window, and in `reader_cutover` each evidence window, runs
inside its own `certification_window()`:

* **`marketing_outcome_cohorts`**: the nine payloads, plus the audit's own
  independent SQL (`independent_counts`). The independent reconciliation is
  now read at the same instant as the payload it reconciles, so a sync that
  commits mid-audit can no longer cause a false mismatch.
  `global_population_split` stays outside, because it belongs to no window.
* **`reader_cutover`**: the reader-contract read, the 13 pages, the settled
  publication and the window checks. The closed-won universe is **read
  outside every pin**: once, at the start, and shared with the truth service,
  because that is the audit's design and unchanged by 179A. Inside a window,
  every page joins that outer `one_snapshot`. So in this audit the won-deal
  population comes from the start of the audit, and every other read comes
  from the window's snapshot. The no-writes fingerprint is still taken before
  and after, outside any pin.
* **`campaigns`**: the five surfaces and the drawer population. `run()` now
  takes **one instant for the whole audit**. Before, every builder read the
  wall clock on its own, and a wall-clock call is never memoised.

Nothing is carried from one window to the next. Each window has its own
transaction and its own memo, and it sees whatever committed before it
started. A window in which the snapshot refused any read is reported exit 2
(§3.2).

### 3.5 Alternatives not taken

* **Memoise across the per-page snapshots** (`docs/59` §11.5 as first
  sketched). This is unsound, as explained in §3.1.
* **One snapshot for the whole audit.** This would be cheaper, but it gives
  every window one instant and holds a transaction and its locks for minutes.
  It would also make a migration wait minutes (see "Locks" in §3.2).
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

All tests are in `tests/test_pr_ads_179a_certification_runtime.py`. This
section covers the first 11 cases: 10 PostgreSQL cases and 1 that needs no
database. §6.2 covers the six F1 cases. The module is in CI's
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

### 6.2 PR-ADS-179A-F1 (truth audit round 1)

Six cases were added (17 in the module, 16 needing PostgreSQL). Each fails
on `91290f7`, and each fails under a named mutation of the fixed tree:

| Case | Proves | On `91290f7` | Fails under |
| --- | --- | --- | --- |
| 08_a | `init_db` run mid-window (another session) neither deadlocks nor fails. It waits holding nothing, every window read succeeds (including one of `leads`, which `init_db` ALTERs first), and the migration completes after the window ends | `DeadlockDetected` on the window's `leads` read | M14 |
| 08_b | a window held past `SCHEMA_LOCK_WAIT_SECONDS` makes `init_db` raise `SchemaMigrationBlocked` and log at ERROR, and the window's reads are unaffected | no bound exists | M14 |
| 08_c | a window that would start during a migration waits, bounded, then runs unpinned with a warning | no schema lock | — |
| 09_a | a reader that swallows its own failed statement does not poison the window: the next read and the next page succeed | `PinnedSnapshotError` from the reader's clean exit | M15 |
| 10_a | refusals of `sql.Composed`, `bytes` and `executemany` statements are recorded even when swallowed. A statement that ends the transaction and leaves the connection IDLE (`SELECT 1; COMMIT`) is caught when it runs: the next statement in the same block never runs, and the window refuses every later read. (It does not cover an escape that opens another transaction; see §8.3.) | `_Pin` has no record | M17 |
| 10_b | in all three audits, a refusal any reader swallowed makes every window exit 2 (named as the snapshot's), and nothing else | **exit 0**: the swallowed refusal certified | M16 (cohort audit) |

* **M14**: `init_db` takes no schema lock.
* **M15**: a swallowed reader error is not rolled back.
* **M16**: the cohort audit ignores the snapshot's refusals.
* **M17**: a statement that ends the pinned transaction is not detected when
  it runs. A first version of 10_a did not catch M17, because the block's
  RELEASE backstop still raised. It now asserts that the next statement
  never ran.

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
* **Long transaction, and its locks.** A window holds one READ ONLY
  transaction for its duration: seconds here, expected under a minute in
  production. It holds back vacuum's horizon for that long. It also holds
  AccessShareLock on every table it has read, plus the shared schema lock.
  These conflict only with DDL. The only DDL is `init_db`, which waits on the
  schema lock while holding nothing (§3.2). Any future DDL path must take
  `SCHEMA_LOCK_KEY` exclusive first in the same way, or it can deadlock with
  a window.

## 8. 179B follow-ups

PR-ADS-179B fixes the minors left by the independent re-audit of 179A. The
cases are in `tests/test_pr_ads_179b_certification_runtime_followups.py`
(10 cases, all on PostgreSQL; in CI's PG step and did-run list).

### 8.1 `SchemaMigrationBlocked` names both holders; the backfill exits 2

The schema lock is exclusive for every `init_db`, so it also serialises two
migrations: `scripts/backfill_meta_ads_spend.py` runs `init_db`, and run
during a deploy it holds the lock exactly as a window does. The message said
"certification window(s) held the schema lock", which could be false. It now
names both possible holders: certification window(s) (shared) or another
migration (another `init_db`, exclusive). Which one held it is not known at
that point: the transaction is aborted, and the holder may have changed.

The backfill CLI did not catch the exception: a traceback and exit 1, which
reads as a failed backfill. It now prints `{"status": "failed", "reason":
"schema_migration_blocked"}` and exits 2 ("unavailable"), before reaching
Meta. `api/server.py` still lets the exception fail the lifespan hook (§3.2).

### 8.2 What `init_db` does and does not guarantee

Only the lock-wait bound raises. Every other `init_db` error is logged at
ERROR and swallowed, as before 179A, and the caller cannot tell that the
migration did not run. That includes an unreachable database, a failed DDL
statement, and a `QueryCanceled` from a role- or database-level
`statement_timeout` that fires during the advisory wait itself, before
`lock_timeout` does. The 179A docstring ("a migration that did not run is
never reported as an applied schema") overclaimed and is corrected. Whether
those errors should also raise is a separate decision, not taken here.

### 8.3 No statement runs after a reader's statement ends the pinned transaction

The 179A check after each statement was "is the connection IDLE?". Three
escapes leave it in a transaction, a NEW one:

* `SELECT 1; COMMIT; BEGIN ISOLATION LEVEL READ COMMITTED`: READ COMMITTED
  and writable;
* `SELECT 1; ROLLBACK AND CHAIN` (and a bare `ROLLBACK AND CHAIN`, which the
  start-of-statement refusal missed): the SAME modes, repeatable read and
  read only, but a new snapshot;
* `cursor.connection.commit()`: psycopg2's own connection, which ends the
  transaction without sending a statement. psycopg2 then opens a READ
  COMMITTED transaction for the next one.

On `9a09c6d` the next statement in the same block ran in that transaction.
It was caught only at the block's savepoint release, after it had read.

**Choice: ask the session, not the statement text.** Refusing any statement
whose text contains a transaction-control verb cannot be made exact: `END`
closes every `CASE`, and a verb can sit in a string literal or a comment. A
text rule strict enough to be safe refuses ordinary reads; one loose enough
to pass them can be evaded. So the text is only a cheap trigger, and the
session decides:

* the pinned transaction carries a **transaction-local marker**
  (`set_config('averroes.pinned_read_snapshot', <token>, true)`), set at the
  top level before any savepoint. No savepoint rollback removes it; any new
  transaction, however it was opened, lacks it;
* a statement can end a transaction block only by naming `COMMIT`, `END`,
  `ROLLBACK`, `ABORT` or `PREPARE`. A procedure or `DO` block cannot commit
  inside a transaction block, and a keyword cannot be quoted. The view checks
  that against both the template and the text psycopg2 actually sent
  (`cursor.query`), because a parameter (`AsIs`) can carry SQL the template
  does not show. It asks the session for the marker and the modes after:
  * any statement whose text names one of those verbs (a `CASE … END`
    included);
  * any statement whose text cannot be read;
  * every `executemany`, whose `cursor.query` holds only its last statement.

  The answer must be the pin's token, `repeatable read`, read only `on`;
  otherwise the snapshot is broken. Modes alone would miss `ROLLBACK AND
  CHAIN`, which is why the marker exists;
* before every statement, and before every `get_conn()` savepoint, the view
  refuses a connection that is no longer in a transaction. That catches the
  raw-connection commit, which sends no statement;
* a statement that names a verb and fails cannot be checked while the
  transaction is aborted. The pin is then marked suspect, and the next
  statement asks the session first. In an aborted transaction no read can
  succeed in the meantime;
* `ROLLBACK AND CHAIN`, `ABORT` and `PREPARE TRANSACTION` are now also
  refused before they are sent. A reader's own `ROLLBACK [WORK] TO
  [SAVEPOINT] name` still runs.

The cost is one round trip per statement that names a verb, and per
`executemany` call. Other reads cost nothing extra: the status check is
local to libpq. A reader's own
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` / `READ ONLY` is still
absorbed.

`callproc`, `copy_expert`, `copy_to` and `copy_from` are guarded like
`execute`, and are always verified (PR-ADS-179B-F1, §9). Before that they
passed straight through. A pinned view refuses `connection.reset()`.

**The remaining, disclosed bypass.** A reader that takes psycopg2's OWN
cursor (`cursor.connection.cursor()`, or anything reached through
`cursor.connection` other than `commit()` / `rollback()`) bypasses the
view's per-statement checks. Its statements are not checked before or after
they run, so a read it makes after ending the pinned transaction is NOT
prevented. No production reader does this.

What catches such an escape is the reader's own savepoint: on the block's
exit, the RELEASE fails, or on an exception the ROLLBACK TO fails, because
the savepoint went with the transaction. Either failure is recorded as a
refusal on the pin (`_Pin.violations`, PR-ADS-179B-F1). That is what the
audits read (`snapshot_memo.refused_reads`): the window is then reported
unavailable (exit 2), and its `snapshot` entry reads `snapshot_broken`.
Before F1, the exception path set `broken` without recording anything, so
a window whose last `get_conn()` escaped this way and then raised certified
with exit 0 and read `pinned: true`.

### 8.4 An unpinned window is disclosed

A window that cannot take the shared schema lock within
`WINDOW_SCHEMA_LOCK_WAIT_SECONDS` runs unpinned. It still certifies, with
exit 0, as before 179A, and that is unchanged. But the reports did not say
so, while this document says certification checks a snapshot-consistent
build. Now `certification_window(disclosure)` records whether the window was
pinned and why not. `snapshot_memo.window_snapshot` turns that into each
window's entry, `{"pinned", "reason", "detail"}`, where `reason` is one of:

* `schema_lock_wait_exceeded`;
* `snapshot_open_failed`;
* `no_database`;
* `not_recorded`. A window is pinned only when its scope carries a pin, and
  a missing record is never read as pinned;
* `snapshot_broken` (PR-ADS-179B-F1): the window was pinned, but its snapshot
  broke or refused a read. Such a window is also unavailable (exit 2); it is
  never reported `pinned: true`.

`snapshot_memo.pinning_summary` adds a report-level `snapshot_pinning`, with
a note that names the unpinned windows and says that what was certified
there is NOT a snapshot-consistent build:

| Audit | Per window | Summary |
| --- | --- | --- |
| `marketing_outcome_cohorts` | `business_windows[i].snapshot` | `snapshot_pinning` |
| `reader_cutover` | `windows[i].snapshot`, `evidence_windows[i].snapshot` | `snapshot_pinning` |
| `campaigns` | `per_window[w].snapshot` | `Findings.snapshot_pinning`; `snapshot_pinning` in `--json` |

`test_06` now compares the reports without these keys, because they differ
by design. Since F1 it removes them only at the exact paths in this table.
It asserts the disclosure itself: every window is pinned with the windows,
and every window is `not_recorded` without them.

Since PR-ADS-179B-F1 the disclosure also reaches the daily verdict. The
stored `platform_certification_runs.summary` row
(`platform_certification_service.summarize`) keeps each audit's
`snapshot_pinning`: the counts, the windows not pinned with their reasons,
and the note. The platform certification's human output prints the note
under any audit with a window that was not pinned. Before F1 both dropped
it, so an unpinned window was visible only in that audit's own report.

### 8.5 The shared lock never rides a pooled connection

The window takes the schema lock SHARED at session level, which outlives its
transaction. A connection pooled while still holding it would block every
later migration for as long as the pool kept it. Only `08_a` guarded this,
and only because `init_db` runs on another thread there. `05` reads
`pg_locks` on the window connection's own backend pid. The lock is held
during the window and gone once the window closes, and the pool then hands
out that same connection again.

### 8.6 Guards

| Case | Proves | On `9a09c6d` | Fails under |
| --- | --- | --- | --- |
| 01_a (×2: shared, exclusive holder) | the backfill CLI exits 2 with `schema_migration_blocked` and never reaches Meta | `SchemaMigrationBlocked` escapes `main` | M23 |
| 01_b | held by another migration, the message names both holders | names windows only | — |
| 03_a | the re-audit's probe: the next statement is refused, never `read committed` | it ran: `('read committed', 'off')` | M19 |
| 03_b | `SELECT 1; ROLLBACK AND CHAIN`: the next read is refused, never a new snapshot; a bare `ROLLBACK AND CHAIN` is refused before it is sent | it read a row committed after the window opened (7, not 6) | M19, M21 |
| 03_c | `cursor.connection.commit()`: the next statement is refused | it ran: `('read committed', 'off')` | M20 |
| 03_c2 | the same escape carried in an `AsIs` parameter (the template names no verb): the next statement is refused | it ran | M19, M25 |
| 03_d | not an over-refusal: `CASE … END`, a reader's own savepoint and `ROLLBACK TO SAVEPOINT`, `executemany` and the satisfied `SET TRANSACTION` forms run in the snapshot, with nothing recorded | passes (a control) | — |
| 04 | all three audits disclose every unpinned window (`schema_lock_wait_exceeded`) with a summary note, and exit 0 as before; a pinned window reads `pinned: true` | no disclosure | M22 |
| 05 | the shared lock is gone from `pg_locks` for the window's backend pid once it closes, and that connection is pooled and reused | passes: 179A unlocks | M18 |
| 179a `06` (amended) | the disclosure: all pinned with the windows, all `not_recorded` without | — | M24 |

The mutations, each applied alone to a copy of the fixed tree:

* **M18**: `_release_connection` skips `pg_advisory_unlock_shared`.
* **M19**: no statement is verified against the session.
* **M20**: an IDLE connection is not refused before a statement.
* **M21**: the verification checks the modes only, not the marker.
* **M22**: `certification_window` does not record why it ran unpinned.
* **M23**: the backfill CLI does not catch `SchemaMigrationBlocked`.
* **M24**: a window with no record reads as pinned.
* **M25**: only the template is checked for a verb, not the text sent.

## 9. PR-ADS-179B-F1 (truth audit round 1)

The verdict was merge with minors. Base and head reports were identical
apart from the new `snapshot` / `snapshot_pinning` keys. 271 of 2,938
statements triggered the CASE … END check, and every one passed. Three
items were fixed:

1. **A broken pin never certifies or reads as pinned.** `_Pin._undo`
   records a refusal whenever it marks the snapshot broken.
   `window_snapshot` reports `pinned: false`, `reason: "snapshot_broken"`
   for a broken pin or a pin with any refusal. The summary note names such
   a window and never calls it snapshot-consistent. The reader-cutover
   audit now reads the entry after the window's block, as the other two
   already did.
2. **The disclosure reaches the daily verdict.** `summarize()` keeps
   `snapshot_pinning` for each audit, and `_render` prints its note (§8.4).
3. **The pass-through bypasses are closed.** `callproc`, `copy_*` and
   `reset()` are guarded (§8.3). The one remaining bypass, psycopg2's own
   cursor, is disclosed in §8.3.

| Case | Proves | On `acb4f66` | Fails under |
| --- | --- | --- | --- |
| 06_a[raw_cursor_escape] | the auditor's probe as the window's LAST read: the escape read `('read committed', 'off')`, then raised; the window records a refusal, exits 2, reads `snapshot_broken`, and the note says NOT snapshot-consistent | exit 0, `pinned: true`, no refusal | M26, M27 |
| 06_a[rollback_and_chain] | a swallowed `SELECT 1; ROLLBACK AND CHAIN`: exit 2 AND `snapshot_broken` | exit 2 but `pinned: true` and "snapshot-consistent" | M27 |
| 06_b | `window_snapshot` reads the pin: a refusal alone makes it `snapshot_broken` | `pinned: true` | M27 |
| 07 | `summarize()` stores the windows not pinned, with reasons and the note; `_render` prints it; an all-pinned audit prints nothing extra; an audit with no windows stores no key (no database needed) | `KeyError: 'snapshot_pinning'` | M28, M29 |
| 08 | after `cursor.connection.commit()`, `copy_expert`, `copy_to` and `callproc` are refused before they read; in an intact snapshot they run in it; `reset()` is refused and recorded, and the snapshot stays intact | `copy_expert` read `read committed\toff` | M30–M33 |

* **M26**: `_undo` sets `broken` without recording a refusal.
* **M27**: `window_snapshot` ignores `broken` and the refusals.
* **M28**: `summarize()` drops `snapshot_pinning`.
* **M29**: `_render` prints nothing about it.
* **M30**: `copy_expert` passes through unguarded.
* **M31**: `callproc` passes through unguarded.
* **M32**: `copy_to` passes through unguarded.
* **M33**: the pinned view does not refuse `reset()`.
