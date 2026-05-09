# Historical Backfill Framework

**Document:** `docs/18_HISTORICAL_BACKFILL.md`
**Roadmap ID:** PR-ADS-071
**Phase:** 1 — Read-Only Intelligence / Roadmap V4.0
**Module:** scripts/, connectors/, db/

> **This backfill reads from Windsor.ai and HubSpot and writes only to the local
> database. It does not modify Google Ads, HubSpot, bids, budgets, campaigns,
> contacts, deals, or negative keywords.**

---

## Purpose

The historical backfill framework populates the local PostgreSQL database with
historical data from Google Ads (via Windsor.ai) and HubSpot. This data is used
for reporting, campaign analysis, and the V4.0 data foundation.

Without historical data the dashboard and reports reflect only the current rolling
window. With historical data the system can surface:

- Which campaigns wasted money over time
- Which campaigns produced SQLs historically
- Which keywords and geos underperformed across multiple periods
- Paused and retired campaigns (historically expensive lessons)
- GCLID-linked attribution history

---

## What It Pulls

| Source      | Dataset       | Status   | Notes |
|-------------|---------------|----------|-------|
| google_ads  | campaigns     | ✅ Ready | All campaigns including paused. Windsor `date_from`/`date_to` supported. |
| google_ads  | keywords      | ✅ Ready | Keyword-level performance by date range. |
| google_ads  | geo           | ✅ Ready | Country-level performance by date range. |
| google_ads  | search_terms  | ⚠ Unsupported | Windsor connector uses `date_preset`, not `date_from`/`date_to`. Historical search-term backfill requires Windsor plan/API verification. Documented as `unsupported_by_current_connector`. |
| hubspot     | contacts      | ✅ Ready | Paid-search contacts filtered by `createdate` GTE/LTE. |
| hubspot     | deals         | ✅ Ready | Deals via GCLID contact associations. Depends on GCLID coverage. |

---

## What It Does Not Do

This framework is explicitly forbidden from the following actions:

- Writing to Google Ads (no mutations, bid changes, budget changes, campaign pauses)
- Writing to HubSpot (no contact updates, deal updates)
- Pushing negative keywords
- Uploading offline conversion tracking (OCT)
- Triggering any scheduler or Render startup process
- Adding a dashboard backfill button (planned for PR-ADS-072)
- Running automatically on a schedule (manual only)

---

## Read-Only Governance

This framework follows the Phase 1 / Six-Month Governance read-only doctrine:

```
External platforms → read-only pull → local database → reports/dashboard
```

The following data flow is forbidden:

```
local analysis → external platform write
```

---

## CLI Usage

The backfill is invoked as a Python module. `--from` and `--to` are always required.

```bash
# Dry run — always safe, prints plan only, no API calls, no DB writes
python -m scripts.backfill --source all --from 2024-01-01 --to 2026-05-09 --dry-run

# Google Ads only — local persistence enabled
python -m scripts.backfill --source google_ads --from 2024-01-01 --to 2026-05-09

# HubSpot only — local persistence enabled
python -m scripts.backfill --source hubspot --from 2024-01-01 --to 2026-05-09

# Weekly chunking, limited to 1 chunk (safe test)
python -m scripts.backfill --source hubspot --from 2026-04-01 --to 2026-04-07 \
    --chunk weekly --max-chunks 1
```

### Supported Arguments

| Argument                       | Default  | Description |
|--------------------------------|----------|-------------|
| `--source`                     | `all`    | `all`, `google_ads`, or `hubspot` |
| `--from YYYY-MM-DD`            | required | Backfill start date (inclusive) |
| `--to YYYY-MM-DD`              | required | Backfill end date (inclusive) |
| `--chunk monthly\|weekly`      | monthly  | Chunking strategy |
| `--dry-run`                    | false    | Print plan only; no API calls; no DB writes |
| `--max-chunks N`               | none     | Safety cap on chunks per dataset |
| `--verbose`                    | false    | Print extra detail per chunk |

---

## Dry Run

Dry-run mode is always safe. It prints the backfill plan but makes no API calls
and performs no database writes.

```bash
python -m scripts.backfill --source all --from 2024-01-01 --to 2026-05-09 --dry-run
```

Expected output:

```
  Mode:          DRY RUN — no local database writes performed.

  DRY RUN — no local database writes performed.
```

**Dry run is recommended before any live backfill run.**

---

## Chunking

The backfill splits the date range into manageable chunks to avoid overwhelming
the Windsor or HubSpot APIs with large single requests.

### `--chunk monthly` (default)

Splits the range into calendar-month-aligned windows:

- 2024-01-01 → 2024-01-31
- 2024-02-01 → 2024-02-29
- 2024-03-01 → 2024-03-15 (if `--to` is 2024-03-15)

### `--chunk weekly`

Splits the range into 7-day windows:

- 2024-01-01 → 2024-01-07
- 2024-01-08 → 2024-01-14
- …

Use `--max-chunks N` to limit the number of chunks processed per dataset — useful
for testing or resuming a partial backfill safely.

---

## Resumability / Safe Re-runs

The backfill is designed to be safe to re-run:

- **search_terms** — not currently backfilled by this framework because the
  current Windsor connector uses `date_preset` rather than explicit
  `date_from`/`date_to` for historical ranges. The existing writer may support
  safe re-runs if connector support is added later, but search_terms are
  skipped by PR-ADS-071.
- **campaigns, keywords, geo, leads, deals** — each backfill run creates a new
  `run_id` record in the `runs` table. Re-running creates a new run with fresh
  data. Historical data from prior runs is preserved.
- **sync_batches** — a new batch record is created per (dataset, chunk). On
  success, `sync_state` is updated with the latest watermark.

---

## Dataset Freshness Updates

For each chunk that succeeds:

1. `start_sync_batch(source, dataset, sync_type="backfill", date_from, date_to)` is
   called at the start of each chunk.
2. `finish_sync_batch(batch_id, "success", row_count=N, last_source_date=chunk_to)`
   is called after successful local persistence.
3. `sync_state` is updated with the latest watermark for this source/dataset.

If a chunk fails, `finish_sync_batch(..., "failed", error_message=...)` is recorded
and `sync_state` preserves the previous successful watermark.

---

## Validation Commands

After a live backfill run, verify data was written:

```bash
# Validate Python syntax of all scripts
make validate

# Run tests
pytest tests/test_backfill_framework.py -v

# Check mutation safety (expect no results)
grep -R "mutate\|GoogleAdsClient.*mutate\|requests.post\|httpx.post\|PATCH\|DELETE" \
    connectors scripts scheduler db api analysis \
    | grep -i "google\|hubspot\|ads\|deal\|contact\|campaign\|keyword\|budget\|negative"

# Check unsafe wording (expect no results in production paths)
grep -R "push negative\|apply negative\|pause campaign\|send to Google Ads\|send to HubSpot\|upload conversion\|change bid\|change budget" \
    scripts connectors scheduler docs tests --include="*.py" --include="*.md"
```

---

## Known Unsupported Datasets

### `google_ads / search_terms`

Windsor search terms use a `date_preset` parameter (`last_14d`, `last_7d`) rather
than explicit `date_from`/`date_to`. Historical search-term backfill for dates
older than 14 days requires Windsor API/plan verification.

Status in backfill output: `unsupported_by_current_connector`

This does not affect other Google Ads datasets (campaigns, keywords, geo) which
use explicit `date_from`/`date_to` and work correctly.

---

## Security and Governance

- No external write paths are added in this framework.
- No secrets are printed in backfill output or summary files.
- Backfill is manual only — it is not scheduled, not triggered by the UI, and not
  called by any Render startup command.
- The UI admin backfill button is deferred to PR-ADS-072.
- Local DB writes happen only when the command is run explicitly without `--dry-run`.

---

*PR-ADS-071 — Manual Historical Backfill Framework*
*Phase 1 read-only externally confirmed.*
