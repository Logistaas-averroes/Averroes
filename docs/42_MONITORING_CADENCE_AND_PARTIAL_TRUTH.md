# 42 — Monitoring Cadence Identity and Partial-State Truth

**PR-ADS-160-F2.** A monitoring cadence is one pipeline's health, and `partial`
survives to the row that production reads.

> No consumer migration here either. The 25 legacy SQL consumers inventoried by
> PR-ADS-158 remain for **PR-ADS-161**. This corrects PR-ADS-160-F1 and closes
> three further partial-state collapses found in the readiness audit for
> production validation.

---

## 1. The blocker PR-ADS-160-F1 introduced

PR-ADS-160 shipped a monitoring test that read the real persisted
`run_type = "daily_incremental_sync"` and substituted `"daily"` before calling
monitoring. The test passed; the implementation discarded every incremental row.
PR-ADS-160-F1 fixed the test and added `RUN_TYPE_CADENCE` so the real run type
reaches `compute_monitoring_status`.

It routed that run type into the **same cadence as the legacy 06:00 pulse**.

Everything monitoring computes is computed once per cadence: one failure
streak, one `last_success_at`, one severity. Two unrelated pipelines then voted
on one verdict, and the healthier one won it.

`api/scheduler.py:49` registers four jobs. Two write to `runs` every day:

| Job | Time | Writes |
| --- | --- | --- |
| `daily` | 06:00 Asia/Amman | campaign performance, lead quality (`scheduler/daily.py`) |
| `daily_incremental_sync` | 09:00 Asia/Amman | canonical spend, contact funnel, deal ledger, geo, SQL coverage (`scheduler/incremental_sync.py`) |

Measured against the real function on `main` @ `e974890`, with five days of
incremental-sync failures behind a succeeding pulse:

```
severity : green
warnings : []
daily    : last_status='failed', consecutive_failures=1, stale=False,
           last_success_at='2026-09-20T03:05:00Z'   ← the PULSE's clock
```

The same five failures with the pulse rows removed:

```
severity : red
warnings : ['Daily run has failed 5 times in a row.']
daily    : consecutive_failures=5, stale=True, last_success_at=None
```

Two mechanisms, both in `api/monitoring.py`:

* **The failure streak stops at the first `success|partial` in the bucket.** A
  pulse succeeding at 06:00 broke the incremental sync's streak every morning,
  so `consecutive_failures` could never exceed 1 while the pulse was healthy.
  Red needs 2. **Red was arithmetically unreachable.**
* **`last_success_at` took the newest success of either pipeline.** A pulse
  proves nothing about canonical spend, the contact funnel, the deal ledger,
  geo or SQL coverage, yet it reset the clock their staleness is measured
  against — the same substitution this module already forbids for `partial`.

`static/app.js:1714` returns early on `severity === "green"` with no warnings,
and the banner is the only consumer of `/api/monitoring/status`. So the
correctly computed `last_status: "failed"` was computed, served, and never
rendered. The pre-F1 behaviour warned about absence while runs were happening;
the post-F1 behaviour was **silent while runs were failing**, which is worse —
a false green is indistinguishable from a true one.

### Why the tests missed it

Every test proving the F1 fix supplied `daily_incremental_sync` alongside
`weekly` and `monthly` **only**. No test anywhere placed a `daily` row and a
`daily_incremental_sync` row in the same run list. The population production
actually emits was untested. This was omission of a scenario, not the
identifier laundering of PR-ADS-160 — that defect is confirmed fixed and stays
fixed.

## 2. The correction

A cadence is now **one pipeline**, not a name several pipelines share.

```python
MONITORING_CADENCES = ("daily", "daily_incremental_sync", "weekly", "monthly")
RUN_TYPE_CADENCE = {
    "daily":                  "daily",
    "daily_incremental_sync": "daily_incremental_sync",
    "weekly":                 "weekly",
    "monthly":                "monthly",
}
```

Two run types may still map to one cadence — but only where they are the same
pipeline, because the verdict they receive is indivisible.

Consequences, all deliberate:

* `config/thresholds.yaml` gains `ui.monitoring.stale_after_days.daily_incremental_sync`,
  and `api/server.py:_load_monitoring_thresholds` iterates `MONITORING_CADENCES`
  instead of the literal `("daily", "weekly", "monthly")` — a cadence added
  without a line there would have silently ignored its configured value.
* **The unchosen 2-day fallback is gone.** `stale_after_days.get(rt, ...get(rt, 2))`
  answered a missing threshold with a number nobody picked. A cadence with no
  configured threshold is now reported as not stale and warned about by name:
  unknown is not two days, the same way unknown is not zero.
* Warning text goes through `CADENCE_LABELS`; `str.capitalize()` rendered the
  new cadence as `Daily_incremental_sync`.
* **A missing pipeline is now visible.** With four registered jobs, a `runs`
  history containing only three cadences is a system with a pipeline missing,
  and monitoring says so. Test fixtures were updated to the production shape
  rather than the assertion being relaxed.

`static/app.js` needs no change: it reads `severity` and `warnings` only.

## 3. Direct evidence must not report LESS than what it displaces

PR-ADS-160-F1 §4 made a dataset's own adverse state outrank an inherited
blocking one, and promised the dependency would be *named in the reason rather
than dropped*. True at four of the five returns in
`services/freshness_service.py`. At the fifth — partial sync with an
**unmeasured** row count — `dependency_note` was never interpolated, and the
verdict fell from `blocked_by_dependency` / `error` to `unknown_row_count` /
`neutral`, which is not in `BLOCKING_STATES`, so the cascade to any further
dependant was lost too.

Same inputs, both versions:

```
POST-F1 : unknown_row_count       neutral   dependency mentioned? False
PRE-F1  : blocked_by_dependency   error     "…depends on Contact Funnel…"
```

Reachable whenever the row-count `SELECT` at `api/server.py:3521` raises — a
missing table on a partly-migrated database, a permission error, a lock
timeout. That is precisely when a blocking error matters most.

Both returns now carry `{dependency_note}`, and `_result` takes an explicit
`severity` override so the verdict is floored at `error` when it displaces a
blocking inherited state. Precedence must never mean reporting less.

## 4. `partial` is not `failed` on the deal ledger or canonical geo

`scheduler/incremental_sync.py` collapsed the producer's status at two call
sites, under this comment:

```python
# sync_batches accepts success|failed only, so a PARTIAL sync is recorded
# as failed with its reason — a partial run must never look successful.
```

The premise stopped being true in PR-ADS-160, which added `partial` to
`VALID_SYNC_STATUSES` and to `finish_sync_batch`'s own validation
(`db/writers.py:1599`). The collapse was no longer preventing anything; it was
losing a state — on the canonical **revenue** population, in the same release
that spent four review rounds separating those three states.

`sync_state.status` is what `compute_canonical_freshness` reads, so a truncated
deal-ledger sync rendered as "Latest sync failed" on the revenue freshness
strip. Severity coincided, so this was never a green-vs-red lie; it was a
three-states-to-two lie, and it sends an operator to *retry* when the remedy is
*resume*. Both sites now pass the status through. The writer already withholds
`last_source_date` for anything but success, so no watermark rule changes.

## 5. A failed pulse leaves a row behind

`scheduler/daily.py` built its run record in memory at `start_run("daily")` and
inserted it **after** both pulls. A pulse that failed at pull time called
`update_run(None, ...)`, which returns immediately — so the failure wrote
nothing to `runs` at all.

This is the most likely origin of the "No daily run found in history" symptom
PR-ADS-160-F1 set out to explain, and it is why §1's masking was *dormant*
rather than active: the pulse was often absent rather than succeeding. It also
means the masking could have switched on without warning the day the pulse
started succeeding again.

The insert now happens immediately after `start_run`, before any connector
import, matching `scheduler/incremental_sync.py:451`.

## 6. Evidence

Every guard here was shown failing against the pre-fix code. With
`api/monitoring.py`, `services/freshness_service.py`,
`scheduler/incremental_sync.py` and `scheduler/daily.py` reverted to
`e974890`, `tests/test_pr_ads_160_f2_cadence_and_partial_truth.py` fails with,
among others:

```
test_121  AssertionError: assert 'green' == 'red'
test_129  AssertionError: the dependency was dropped; the operator is told nothing about it
test_131  AssertionError: direct evidence downgraded error to neutral
test_134  AssertionError: deal ledger still collapses partial into failed
test_134  AssertionError: canonical geo still collapses partial into failed
test_136  AssertionError: the run row is written after pull_campaign_performance(
```

`test_122` (a healthy pair is still green) passes in both, which is what makes
the others mean something. `test_125` carries the counterfactual inside the
suite: it re-merges the cadences at runtime and asserts the masking returns.

## 7. Still open

* **`last_completed_at` has no reader.** It is a derived monitoring response
  field, not a database column, and no UI consumes it — nor does any UI consume
  `latest_runs`, `last_status`, `last_success_at` or `consecutive_failures`.
  The banner renders `warnings` and nothing else. The field is correct and
  invisible; rendering the per-cadence block belongs to a UI change, not here.
* **`/api/monitoring/status` falls back to `runtime_logs/run_history.jsonl`**
  when a reachable database returns zero runs in 90 days
  (`api/server.py:5977`). That file is only written by the three legacy
  schedulers and never contains `daily_incremental_sync`. Pre-existing.
* **`analysis/lifecycle_sql_coverage.py:346` returns `cpql_publishable`
  pre-certification**, retracted only by the CLI audit's `_withhold`. Nothing
  in `api/` or `static/` imports the module today, so no product surface can
  read the un-retracted flag — but PR-ADS-161's consumer migration must not
  call `window_coverage()` directly and trust that field.
* **No structural guard** prevents the run-type-relabelling pattern recurring.
  `test_35` in the PR-ADS-160 suite shows the repo already knows how to write
  an AST self-audit; the same technique over run-type and dataset-key literals
  in monitoring tests would make the class unreachable rather than forbidden
  in prose.
