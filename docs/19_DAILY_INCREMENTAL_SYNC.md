# Daily Incremental Sync

**PR-ADS-073 — Roadmap V4.0 Data Foundation**

## Purpose

The daily incremental sync keeps the local database fresh by pulling recent
data from Windsor.ai and HubSpot every morning and persisting it locally.

Without this job, campaign, keyword, geo, and CRM data would remain stale
between manual or scheduled report runs.  The daily incremental sync is the
routine data-refresh layer that ensures dashboards always reflect recent
platform activity.

The daily incremental sync reads recent data from Windsor.ai and HubSpot and
writes only to the local database.  It does not modify Google Ads, HubSpot,
campaigns, bids, budgets, contacts, deals, or negative keywords.

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

Config is owned by `config/thresholds.yaml` under `sync.daily_incremental`.

## Datasets Synced

| Dataset                 | Source    | Method                            |
|-------------------------|-----------|-----------------------------------|
| `windsor/campaigns`     | Windsor   | `pull_campaign_performance_range` |
| `windsor/keywords`      | Windsor   | `pull_keyword_performance_range`  |
| `windsor/geo`           | Windsor   | `pull_geo_performance_range`      |
| `windsor/search_terms`  | Windsor   | ⚠ **Skipped** (see below)         |
| `hubspot/contacts`      | HubSpot   | `pull_paid_search_contacts_in_range` |
| `hubspot/deals`         | HubSpot   | `pull_deals_with_gclid` (via contacts) |
| `gclid/matches`         | Local     | ⚠ **Skipped** (see below)         |

### Skipped Datasets

**`windsor/search_terms`**: The Windsor.ai search-terms endpoint uses
`date_preset` only (e.g. `last_14d`).  It does not accept explicit
`date_from`/`date_to` parameters reliably.  Using a preset would not give a
true incremental sync (the preset window shifts with time, not with the last
sync watermark).  To avoid creating false freshness signals, this dataset is
skipped in incremental sync and documented as
`unsupported_by_current_connector`.  Search terms are still written by the
daily pulse scheduler (`scheduler/daily.py`) using the existing preset-based
pull.

**`gclid/matches`**: No incremental DB persistence path exists yet for GCLID
match data.  The dataset is skipped and documented accordingly.

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

2. **Windsor search_terms**: Not supported for incremental sync due to
   connector limitations (preset-only date filter).  See "Skipped Datasets"
   above.

3. **No run_id association**: Incremental sync rows are written with
   `run_id=None` because the sync is not tied to a specific pulse run.
   Writers accept `None` safely.  Rows remain queryable by `source_date`.

4. **Single-worker concurrency guard**: The in-flight guard (`_job_state`) is
   process-local.  For single-worker Render deployments this is sufficient.
   Multi-worker deployments would require a DB-backed advisory lock.
