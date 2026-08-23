# Daily Incremental Sync

**PR-ADS-073 — Roadmap V4.0 Data Foundation**

## Purpose

The daily incremental sync keeps the local database fresh by pulling recent
data from the **Google Ads API** and HubSpot every morning and persisting it
locally.

Without this job, campaign, geo, and CRM data would remain stale between manual
or scheduled report runs.  The daily incremental sync is the routine
data-refresh layer that ensures dashboards always reflect recent platform
activity.

Every external platform is read read-only; the only writes are to the local
database.  The sync does not modify Google Ads, HubSpot, campaigns, bids,
budgets, contacts, deals, or negative keywords.

> **PR-ADS-154 (production hotfix).** Three defects made a failed run look like
> a successful one. They are described in "Database readiness", "Retired
> datasets" and "Exit codes" below. If you are reading this doc to understand a
> run that reported `partial` and exited 0, that was the bug.

## Why Daily Instead of Every 4 Hours

The system uses a daily 9 AM sync because Google Ads and CRM attribution can
lag.  A daily rolling-window sync is simpler, safer, and easier to audit than
frequent partial refreshes.

Specific reasons:

- **Attribution lag**: Google Ads conversion data can lag by 24–72 hours.
  A 4-hour sync would repeatedly pull incomplete conversion data and create
  misleading freshness signals.
- **HubSpot rate limits**: CRM APIs have daily rate limits.  A daily sync
  leaves headroom for manual triggers and other integrations.
- **Audit simplicity**: One daily run per dataset produces one sync batch
  record per day, making the sync history easy to inspect and reason about.
- **Provider API stability**: Lower frequency reduces exposure to transient
  API failures and avoids quota exhaustion.

## Schedule

| Job ID                  | Time (Asia/Amman) | Time (UTC) |
|-------------------------|-------------------|------------|
| `daily_incremental_sync` | 09:00             | 06:00      |

Timezone: `Asia/Amman` (UTC+3 year-round; Jordan suspended DST in 2022).

Schedule is registered in `api/scheduler.py` via APScheduler `CronTrigger`.

Lookback windows are read at module import time from
`config/thresholds.yaml` (`sync.daily_incremental.lookback_days`).
If the config file is unavailable, hard-coded safe defaults (14/14/30 days)
are used so the scheduler always has valid values.

## Datasets Synced

| Dataset                                | Source         | Owner |
|----------------------------------------|----------------|-------|
| `hubspot/contacts`                     | HubSpot        | `pull_paid_search_contacts_in_range` |
| `hubspot/contact_funnel`               | HubSpot        | `hubspot_contact_funnel_sync_service` |
| `hubspot/deals`                        | HubSpot        | `pull_deals_with_gclid` (via contacts) |
| `gclid/matches`                        | HubSpot → local | `revenue_recovery_service` |
| `hubspot/deal_ledger`                  | HubSpot        | `hubspot_deal_sync_service` |
| `hubspot/source_classification`        | HubSpot        | `source_attribution_service` |
| `google_ads_api/canonical_spend`       | Google Ads API | `google_ads_spend_service` |
| `fx/daily_rates`                       | FX reference   | `fx_service` |
| `google_ads_api/canonical_geo`         | Google Ads API | `google_ads_geo_sync_service` |
| `google_ads_api/geo_reconciliation`    | local          | read-only verdict, runs last |
| `mailchimp/refresh`                    | Mailchimp      | `mailchimp_sync_service` |

### One canonical Google Ads source key

`google_ads` and `google_ads_api` were two spellings of one source. Keeping
both is what let the writers and the freshness configuration drift until
neither matched the other — the ROAS denominator had no freshness signal at
all, and `gclid_attribution` reported "never run" while its table filled up.

There is now ONE key, `google_ads_api`, defined in `services/dataset_keys.py`
and imported by every side. The superseded spelling is **normalized**, not
deleted: writers canonicalize before stamping, and an idempotent migration
relabels the accumulated rows, because dropping the spelling would orphan the
history production already holds — the same defect wearing the opposite mask.

### Retired datasets (PR-ADS-154)

Production no longer uses Windsor.ai. These four are recorded as `retired`,
which is neither `skipped` nor `success`: they never run, so they never
contribute to freshness or to the overall verdict.

| Retired                | Replaced by                        |
|------------------------|------------------------------------|
| `windsor/campaigns`    | `google_ads_api/canonical_spend`   |
| `windsor/geo`          | `google_ads_api/canonical_geo`     |
| `windsor/search_terms` | `google_ads_api/search_terms` (weekly scheduler, not this run) |
| `windsor/keywords`     | **nothing** — see below            |

`windsor/keywords` has **no canonical incremental replacement**.
`keyword_daily_facts` is written by the weekly/monthly schedulers, not by this
run. That is stated rather than papered over: keeping the Windsor call or
reporting the dataset successful would both be false. Building an incremental
keyword path is separate work.

They are recorded rather than deleted from the report because a dataset that
silently disappears is indistinguishable from one that was forgotten.

### Database readiness (PR-ADS-154)

**The run initializes the pool and probes it with `SELECT 1` before contacting
any external platform**, and aborts with `status: failed`,
`reason: database_unavailable` and an empty `datasets` map if that fails.

A standalone `python -m scheduler.incremental_sync` process is not the Flask
app: nothing had called `init_pool()`, so `_pool` was `None` and every
persistence call received `conn is None` and degraded quietly to a no-op. The
run pulled real rows from Google Ads and HubSpot, wrote none of them, and
reported `partial`.

`init_pool()` swallows its own failure and leaves `_pool = None`, and a pool
that exists can still front an unreachable server — so "we called init_pool" is
not evidence. The probe is.

## Rolling Windows

The sync uses rolling date windows anchored to today's UTC date:

| Dataset               | Default Lookback | Config Key                       |
|-----------------------|-----------------|-----------------------------------|
| Windsor ads datasets  | 14 days         | `sync.daily_incremental.lookback_days.ads` |
| HubSpot contacts      | 14 days         | `sync.daily_incremental.lookback_days.hubspot_contacts` |
| HubSpot deals         | 30 days         | `sync.daily_incremental.lookback_days.hubspot_deals` |

These windows are intentionally overlapping: data from the last N days is
re-pulled each day.  Writers use upsert semantics so re-pulling does not
duplicate rows.

## Freshness Metadata

Dataset freshness (in `sync_state` table) is updated **only after successful
DB persistence**.

- If rows were pulled but zero were written → `sync_batch` is marked
  **failed** and `sync_state` is not updated to `success`.
- If the pull raises an exception → `sync_batch` is marked **failed**.
- If the pull returns zero rows (genuine empty window) → `sync_batch` is
  marked **success** with `row_count=0`.
- If persistence succeeds → `sync_batch` is marked **success** and
  `sync_state.last_successful_sync_at` is updated.

## What It Does Not Do

This job intentionally does NOT:

- Run historical backfill (use `scripts/backfill.py` or `/api/backfill/run`)
- Generate weekly or monthly advisor reports
- Generate N-gram analysis or waste detection recommendations
- Modify campaign bids, budgets, or keywords in Google Ads
- Modify contacts or deals in HubSpot
- Upload conversions (OCT)
- Push negative keywords
- Pause or enable campaigns
- Write to any external platform

## Manual Trigger

An admin can trigger the sync manually via:

```
POST /run/incremental-sync
```

Requires admin session cookie or `ADMIN_API_TOKEN` header.

Returns the full sync summary JSON including per-dataset status.

Example response:

```json
{
  "status": "success",
  "job": "daily_incremental_sync",
  "started_at": "2026-05-12T06:00:00Z",
  "finished_at": "2026-05-12T06:01:23Z",
  "result": {
    "status": "success",
    "run_type": "daily_incremental_sync",
    "run_reason": "manual",
    "started_at": "...",
    "finished_at": "...",
    "lookback": {
      "ads_days": 14,
      "hubspot_contacts_days": 14,
      "hubspot_deals_days": 30
    },
    "datasets": {
      "windsor/campaigns":    { "status": "success", "rows_pulled": 84, "rows_written": 84 },
      "windsor/keywords":     { "status": "success", "rows_pulled": 320, "rows_written": 320 },
      "windsor/geo":          { "status": "success", "rows_pulled": 210, "rows_written": 210 },
      "windsor/search_terms": { "status": "skipped", "note": "unsupported_by_current_connector: ..." },
      "hubspot/contacts":     { "status": "success", "rows_pulled": 15, "rows_written": 15 },
      "hubspot/deals":        { "status": "success", "rows_pulled": 4, "rows_written": 4 },
      "gclid/matches":        { "status": "skipped", "note": "unsupported_by_current_connector: ..." }
    },
    "errors": []
  }
}
```

It can also be triggered from the CLI:

```bash
python -m scheduler.incremental_sync
```

## Validation

```bash
# Syntax and import check
make validate

# Unit tests
pytest tests/test_daily_incremental_sync.py -v

# Scheduler status — confirm job appears
curl http://localhost:8000/scheduler/status

# Manual trigger (admin token required)
curl -X POST http://localhost:8000/run/incremental-sync \
  -H "X-Admin-Token: $ADMIN_API_TOKEN"
```

Expected scheduler status output includes `daily_incremental_sync` with
schedule `09:00 Asia/Amman (06:00 UTC)`.

Mutation safety grep:

```bash
grep -R "mutate\|requests.post\|httpx.post\|PATCH\|DELETE" \
  scheduler/incremental_sync.py connectors/windsor_pull.py connectors/hubspot_pull.py
```

Expected: no external write paths.

## Known Limitations

1. **HubSpot deals via contacts only**: Deals are fetched by pulling GCLID
   contacts in the rolling window and then fetching associated deals.  Deals
   linked to contacts created outside the window are not refreshed in this
   incremental pass.  Full historical deal coverage requires backfill.

2. **No incremental keyword path**: `windsor/keywords` was retired with no
   canonical replacement. `keyword_daily_facts` is refreshed by the
   weekly/monthly schedulers. See "Retired datasets" above.

3. **`lookback_days.ads` no longer drives a pull**: Windsor was its only
   consumer. The canonical Google Ads steps own their own lookbacks
   (`DAILY_SPEND_LOOKBACK_DAYS`, `DAILY_GEO_LOOKBACK_DAYS`), because the window
   that is safe to re-fetch is a property of the dataset, not of the scheduler.
   The parameter is still accepted and reported so the summary contract is
   unchanged.

4. **Single-worker concurrency guard**: The in-flight guard (`_job_state`) is
   process-local.  For single-worker Render deployments this is sufficient.
   Multi-worker deployments would require a DB-backed advisory lock.


## Exit codes (PR-ADS-154)

`python -m scheduler.incremental_sync` exits **0 only when every dataset that
ran succeeded**. `partial` and `failed` both exit 1.

The entry point previously fell off the end of the module at 0, so a run
reporting nine failed datasets was indistinguishable, to any caller, from a
clean one — and an operator reading `echo $?` was told everything worked.

### The safe operator command

```bash
python -m scheduler.incremental_sync > /tmp/incremental.json; rc=$?
cat /tmp/incremental.json
echo "EXIT_CODE=$rc"
```

**Do not pipe through `tee`.** `python ... | tee file; echo $?` reports
**`tee`'s** exit status, not Python's — and `tee` almost always succeeds, so a
failed run reads as `0` and the exit-code fix above is defeated by the very
command used to check it. The redirect form above captures Python's own status
in `rc` before anything else can overwrite `$?`.

If you must pipe, make the pipeline honest first:

```bash
set -o pipefail                                    # bash/zsh
python -m scheduler.incremental_sync | tee /tmp/incremental.json
echo "EXIT_CODE=$?"

# or, without pipefail:
python -m scheduler.incremental_sync | tee /tmp/incremental.json
echo "EXIT_CODE=${PIPESTATUS[0]}"
```

## A dataset never reports success it cannot back up

Several results were derived from what a step *prepared* rather than what
landed. Each is now fail-closed:

| Case | Before | Now |
|---|---|---|
| Batch could not be opened (`start_sync_batch` → 0) | skipped the finish call, returned `success` | fails **before** the external pull |
| No local run record | continued with a falsy `run_id` | aborts with `run_record_write_failed` (PR-ADS-154A — see below) |
| Source classification | reported rows PREPARED as `contacts_classified` | reports rows PERSISTED; pulled-positive/written-zero fails |
| FX refresh | `rows_written: 0` meant both "nothing was missing" and "nothing persisted" | `fetched` separates them; fetched-positive/written-zero fails |
| Geo reconciliation | `success` whatever the verdict | `unavailable`/`no_geo_data` fail — a comparison that could not be performed is not an answer. `mismatch` stays `success`: the step ran and disagreed, which is true |
| Unknown dataset status | counted as not-a-failure | counts as a failure — defaulting the unknown case to "fine" is how a new outcome string turns a broken run green |


## The run record is a distinct failure mode (PR-ADS-154A)

PR-ADS-154 made the run abort when `write_run()` returned no id — but reported
it as `database_unavailable`. Production showed why that is not good enough:

```
PostgreSQL pool initialized successfully
write_run() failed: value too long for type character varying(20)
status: failed   reason: database_unavailable   EXIT_CODE=1
```

The readiness probe had **passed**. The database was reachable, answered, and
then refused the row, because `runs.run_type` was `VARCHAR(20)` while the
scheduler's canonical run type `daily_incremental_sync` is 22 characters. The
mismatch predates PR-ADS-154; that PR surfaced it by finally initializing the
pool and requiring a durable run record before any external pull, which turned
a silent no-op into a loud failure.

Blaming connectivity sent the operator to check a connection that was already
fine while the actual defect went unnamed. So there are now two reasons:

| Reason | Meaning | What to fix |
|---|---|---|
| `database_unavailable` | no pool, or the `SELECT 1` probe failed | connectivity, `DATABASE_URL`, the server |
| `run_record_write_failed` | the database answered and **refused the row** | whatever the message says — schema, constraint, permissions |

Both stop before every external connector, both return `run_id: null` and an
empty `datasets` map, and both exit non-zero. Only the reason differs — which
is the entire point.

The driver's own message is carried into `errors`, because it *is* the
diagnosis. It is redacted first (`db.writers.safe_db_error`): a connection
error can carry the DSN, and a DSN carries the password.

### The column moved to the contract, not the contract to the column

`run_type` is now `VARCHAR(64)`, and `daily_incremental_sync` is unchanged.
Shortening the value to fit an outdated column would have been the cheaper
edit and the wrong one — it is already what scheduler output, tests, monitoring
and operational diagnostics key on.

Existing databases are migrated through the normal `init_db()` deployment path
(`ALTER TABLE runs ALTER COLUMN run_type TYPE VARCHAR(64)`), guarded on the
current length so a redeploy issues no DDL. `CREATE TABLE IF NOT EXISTS` shapes
only NEW databases, which is exactly why production kept `VARCHAR(20)`. Widening
a varchar is a catalog-only change in PostgreSQL: no table rewrite, and every
existing row — `daily`, `backfill`, `revenue_recovery` — keeps its value.

No manual production SQL is required.
