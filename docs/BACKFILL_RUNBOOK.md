# Historical Backfill Runbook

**Document:** `docs/BACKFILL_RUNBOOK.md`
**Roadmap ID:** PR-ADS-041
**Phase:** 1.5 — Read-Only Data Foundation
**Owner:** Youssef Awwad

---

## 1. Purpose

This runbook describes how to manually backfill historical source data into the
local PostgreSQL database for the Logistaas Ads Intelligence System.

**Key principles:**

- Backfill is **manual**. No scheduler, cron job, or Render startup command calls
  these scripts.
- Backfill writes **only to local PostgreSQL**. It never writes to Google Ads,
  HubSpot, or any external platform.
- Backfill populates PostgreSQL with historical source facts for offline analysis.
- All operations default to **dry-run**. You must opt in to execute mode.

---

## 2. Safety Rules

1. **Always run dry-run first.** Verify the chunk plan looks correct before
   executing.
2. **Always provide explicit `--from` and `--to`.** There is no default date range.
   The script refuses to run without both flags.
3. **Never run all-time daily.** Backfill is a one-time operation per date range.
   Daily incremental sync should use a short overlap window (e.g. last 3 days),
   not a full historical re-fetch.
4. **Use small chunks.** The default `--chunk-days 30` is safe. Reduce it if
   Windsor or HubSpot rate-limits you.
5. **Windsor search terms are currently limited to last\_14d** unless your Windsor
   plan and connector support explicit date ranges. Do not assume historical
   search-term data will be returned for ranges older than 14 days.
6. **Large date ranges (> 730 days) require explicit confirmation.** The script
   refuses to run and prints the required `--confirm-large-range I_UNDERSTAND_THIS_IS_LARGE` flag/value.
7. **Execute mode requires explicit confirmation.** You must pass
   `--confirm I_UNDERSTAND_THIS_WRITES_LOCAL_DB` when using `--execute`.

---

## 3. Dry-Run Examples

Dry-run is always safe. No API calls are made and no data is written.

```bash
# All sources, full year 2025
python -m scripts.backfill --source all --from 2025-01-01 --to 2025-12-31 --dry-run

# Windsor only, full year 2025
python -m scripts.backfill --source windsor --from 2025-01-01 --to 2025-12-31 --dry-run

# Windsor keywords only, single month
python -m scripts.backfill --source windsor --dataset keywords \
    --from 2025-01-01 --to 2025-01-31 --dry-run

# Windsor search terms (will print limitation warning)
python -m scripts.backfill --source windsor --dataset search_terms \
    --from 2025-01-01 --to 2025-01-31 --dry-run

# HubSpot contacts only
python -m scripts.backfill --source hubspot --dataset contacts \
    --from 2025-01-01 --to 2025-12-31 --dry-run

# Dry-run with smaller chunks and a chunk limit for inspection
python -m scripts.backfill --source windsor --dataset geo \
    --from 2025-01-01 --to 2025-12-31 \
    --chunk-days 7 --limit-chunks 4 --dry-run
```

---

## 4. Execute Examples

> **Status as of PR-ADS-041:** Execute mode is **not yet implemented**.
> Running any `--execute` command will print a clear message and exit with
> code 3. No data is written. No API calls are made.
>
> Execute mode will be implemented in a future PR once connector rewrites
> to support explicit `date_from` / `date_to` parameters are complete.

The correct command format for when execute mode is ready:

```bash
# Windsor keywords, single month — execute (writes to local DB only)
python -m scripts.backfill --source windsor --dataset keywords \
    --from 2025-01-01 --to 2025-01-31 \
    --execute --confirm I_UNDERSTAND_THIS_WRITES_LOCAL_DB

# Windsor geo, Q1 2025 — execute
python -m scripts.backfill --source windsor --dataset geo \
    --from 2025-01-01 --to 2025-03-31 \
    --execute --confirm I_UNDERSTAND_THIS_WRITES_LOCAL_DB

# Large range (> 730 days) — requires both confirmations
python -m scripts.backfill --source hubspot --dataset contacts \
    --from 2023-01-01 --to 2025-12-31 \
    --execute \
    --confirm I_UNDERSTAND_THIS_WRITES_LOCAL_DB \
    --confirm-large-range I_UNDERSTAND_THIS_IS_LARGE
```

---

## 5. Dataset Notes

### Windsor

| Dataset        | Status                       | Notes |
|----------------|------------------------------|-------|
| `campaigns`    | Planned (execute not ready)  | Connector rewrite needed for date_from/date_to |
| `keywords`     | Planned (execute not ready)  | Connector rewrite needed for date_from/date_to |
| `geo`          | Planned (execute not ready)  | Connector rewrite needed for date_from/date_to |
| `search_terms` | **Limited — see warning**    | Connector uses date_preset; confirmed only up to last_14d |

**Windsor search_terms limitation:**
The Windsor connector currently uses a `date_preset` parameter (e.g. `last_14d`),
not explicit `date_from` / `date_to`. Historical search-term backfill requires
verification that your Windsor plan and connector support explicit date ranges.
Do not assume that requesting a range older than 14 days will return data.

### HubSpot

| Dataset    | Status                       | Notes |
|------------|------------------------------|-------|
| `contacts` | Planned (execute not ready)  | Can be backfilled via `createdate` gte/lte filtering |
| `deals`    | Planned (execute not ready)  | Pulled through GCLID contact associations; pagination caveat applies |

**HubSpot contacts:** The HubSpot CRM search API supports `createdate` range
filters. Future execute mode will paginate through contacts using `after` cursor
pagination.

**HubSpot deals:** Deals are fetched through GCLID-associated contacts. Full deal
backfill depends on GCLID coverage in contact records.

### GCLID Attribution

| Dataset   | Status                      | Notes |
|-----------|-----------------------------|-------|
| `matches` | Future (table not available) | gclid_attribution table planned for PR-ADS-044 |

GCLID attribution backfill is deferred until the `gclid_attribution` table is
created (PR-ADS-044).

---

## 6. Troubleshooting

### `ERROR: --from must be before or equal to --to`

You have reversed the date range. Swap `--from` and `--to`.

```bash
# Wrong
python -m scripts.backfill --source windsor --from 2025-02-01 --to 2025-01-01 --dry-run

# Correct
python -m scripts.backfill --source windsor --from 2025-01-01 --to 2025-02-01 --dry-run
```

### `argument --from is required` / `argument --to is required`

Both `--from` and `--to` are mandatory. There is no default date range.

### `ERROR: --execute requires --confirm I_UNDERSTAND_THIS_WRITES_LOCAL_DB`

Execute mode requires explicit confirmation. Add:

```bash
--confirm I_UNDERSTAND_THIS_WRITES_LOCAL_DB
```

### `Execute mode is not implemented in PR-ADS-041. Exit code: 3`

Execute mode for this source/dataset is a skeleton only in this PR.
Connector rewrites are required. Check the roadmap for the implementing PR.

### `ERROR: Date range is N days (>730)`

Add the large-range confirmation flag:

```bash
--confirm-large-range I_UNDERSTAND_THIS_IS_LARGE
```

### `ERROR: Dataset 'X' is not valid for source 'Y'`

The dataset name does not match the source. Check valid datasets per source:

- `windsor`: `campaigns`, `keywords`, `geo`, `search_terms`
- `hubspot`: `contacts`, `deals`
- `gclid`: `matches`

### Windsor search terms return no data for historical dates

This is expected. The Windsor connector uses `date_preset=last_14d` and does not
support arbitrary historical date ranges without connector/plan changes.

### Database unavailable

Execute mode requires a running PostgreSQL instance. Check that `DATABASE_URL`
is set and the database is reachable.

---

## 7. Scheduler Safety

These scripts are **never** called by any scheduler, cron job, or Render startup
command. Verify after any infrastructure change:

```bash
# Should return no results
grep -r "backfill" scheduler/
grep -r "backfill" render.yaml
```

---

## 8. Daily Sync vs Historical Backfill

Daily sync (PR-ADS-042) is separate from historical backfill. The daily scheduler
covers only a short recent overlap window (last 1–2 days) and updates `sync_state`
for supported datasets (`windsor/search_terms`, `hubspot/contacts`). Historical
backfill remains manual, guarded, and is not yet implemented in execute mode.

---

*This script writes only to local PostgreSQL when `--execute` is used.
It never writes to Google Ads or HubSpot.*

---

## Dataset Freshness UI (PR-ADS-045)

The Health page now shows a **Dataset Freshness** panel that reads from `sync_state`
via `/api/datasets/freshness`. This is display-only: it surfaces the current watermark
state for each tracked dataset. It does not trigger backfill, sync jobs, retries, or
any external API call. Manual backfill remains separate and guarded; the UI provides no
way to initiate a backfill from the browser.
