# 38 — Platform Evidence: keyword and search-term freshness

PR-ADS-156. The canonical ownership contract for the two Platform Evidence
datasets: who writes them, when, under which keys, and what "fresh" means.

---

## The contradiction this document replaces

`keyword_daily_facts` and `search_terms` were already canonical, already had
direct Google Ads API services, and were already registered in the freshness
configuration. What was missing was a caller: the primary production command,
`python -m scheduler.incremental_sync`, refreshed neither. Its retired-dataset
registry said so in terms that had stopped being true —

> search terms … refreshed by the weekly scheduler, not by this incremental run
>
> NO canonical Google Ads API incremental persistence path exists for keywords today

— the second of which was simply wrong: `keyword_sync_service` existed and was
the single durable writer. A successful daily run therefore proved nothing about
either evidence page.

Search terms were worse than unscheduled. Three schedulers each held an inline
copy of pull → open batch → write → judge → finish, and the copies had drifted:

| Trigger | Window | A zero-row pull was… |
|---|---|---|
| daily | 2 days | `success` |
| weekly | 60 days | `success`, with the message "evidence pipeline unavailable" |
| monthly | 30 days | fatal |

Three windows is fine. Three definitions of success is not, and a two-day window
cannot recover a missed run: one skipped daily leaves a permanent hole, because
nothing ever asks for that date again.

---

## Ownership, after

| Dataset | Canonical table | Owning service | Natural key |
|---|---|---|---|
| `google_ads_api/keyword_facts` | `keyword_daily_facts` | `services/keyword_sync_service.py` | `(source_date, customer_id, campaign_id, ad_group_id, criterion_id)` |
| `google_ads_api/search_terms` | `search_terms` | `services/search_term_sync_service.py` | `(source_date, campaign_name, campaign_id, ad_group, keyword, match_type, search_term)` |

Both `(source, dataset)` pairs are spelled ONCE, in `services/dataset_keys.py`,
and imported by the writers, the freshness configuration and the schedulers.
Spelling a key in two places is what let canonical campaign spend report
"never run" for weeks while its table filled up normally.

### Triggers and recovery windows

| Trigger | keyword_facts | search_terms |
|---|---|---|
| `scheduler.incremental_sync` (primary, daily) | 30 days | 14 days |
| `scheduler.daily` | 30 days | 14 days |
| `scheduler.weekly` | 30 days | 60 days |
| `scheduler.monthly` | 30 days | 30 days |
| admin refresh / bootstrap | explicit range | — |

Different triggers may ask for different windows. None of them carries different
rules for what a successful sync means. Every window is wider than the interval
between runs, and every write is an upsert on the natural key, so a missed run is
recovered by the next one and overlapping runs cost time, never correctness.

`SEARCH_TERM_SYNC_LOOKBACK_DAYS` configures the search-term window; the floor is
14 days and the code enforces it.

---

## Verified empty

A Google Ads query that succeeds and returns no rows is a **measurement**: the
interval was asked about and had no eligible query data. A query that fails
returns no rows too. Collapsing the two is what let an outage look like a quiet
week.

* verified empty → `ok=True`, `verified_empty=True`, batch `success`, watermark
  **advances** (the interval is now proven);
* failure → `ok=False`, `verified_empty=False`, batch `failed`, watermark does
  **not** advance.

Row absence is never treated as proof of failure, and failure is never treated as
proof of absence. A successful interval containing zero search terms is normal:
Google Ads may simply have no eligible query data for it.

---

## Freshness

Freshness is read from the canonical sync record and the data together:

* `sync_state.last_source_date` — the proven watermark, advanced only by a
  successful `finish_sync_batch`;
* the latest `sync_batches` row for the pair — status and requested interval;
* `MAX(source_date)` in the table — because a `success` row can sit above facts
  that are a month old.

**Rows in an old table are never "fresh".** That is the case an "are there rows"
check calls healthy, and it is the case the audit command exists for.

All-time history is **disclosed, never claimed**. The audit reports the stored
range and the intervals canonical syncs covered; it does not assert that every
historical date was queried, and `history_coverage_unproven` is a disclosure
rather than a violation.

---

## Evidence truth is not executive truth

`scheduler.incremental_sync` now answers three separate questions:

| Field | Question |
|---|---|
| `execution_status` | did every step run cleanly? |
| `truth_status` / `gap_codes` | is the canonical executive dataset usable? |
| `evidence_status` / `evidence_gap_codes` | did the two evidence datasets refresh? |

A search-term outage is visible in the dataset result, in `evidence_status`, in
`evidence_gap_codes`, in the logs and in the audit command — and it does **not**
make the HubSpot revenue ledger or the canonical campaign-spend contract
`not_ready`. Nor is it suppressed because those happen to be fine.

`evidence_status` is `ready` (both refreshed), `partial` (one), or `not_ready`
(neither, or the run never reached them).

---

## The audit command

```
python -m scripts.audit_keyword_search_term_freshness --json
python -m scripts.audit_keyword_search_term_freshness          # human-readable
```

Initialises its own pool. Exit `0` only when current canonical freshness AND
persistence are proven; `1` on any violation; `2` when the database could not be
read — because a "0 violations" result over an unopened database is a fabricated
all-clear.

Violation codes: `canonical_sync_never_run`, `canonical_sync_failed`,
`canonical_source_stale`, `canonical_table_unavailable`,
`fetched_rows_not_persisted`, `partial_persistence`, `missing_identity`,
`unproven_currency_lineage`, `duplicate_natural_key`, `legacy_source_active`,
`freshness_key_mismatch`. Disclosure: `history_coverage_unproven`.

---

## Legacy, and why the `keywords` snapshot writes stay

§5 required an inspection before stopping the scheduled legacy writes. It found
**four live consumers** of the legacy `keywords` snapshot, none of them Keyword
Evidence:

1. `/api/keywords` aggregated keyword endpoint (`api/server.py`);
2. the campaign drill-down keyword preview (`api/server.py`);
3. the keyword-review action queue (`api/server.py`);
4. `fetch_keyword_theme_snapshot` → the Campaigns page keyword themes.

Stopping the writes would have starved all four — the "silently remove it" §5
forbids. So the weekly and monthly writes stay, **documented and
non-authoritative**, and the static guard's job is to stop new legacy reads
appearing and to keep the snapshot out of Keyword Evidence, which reads
`keyword_daily_facts` and nothing else.

Windsor is retired. Historical `windsor` / `windsor_mcp` sync rows are still
reported — they are real evidence of how these tables were first populated — but
under keys that say `legacy_sync_state`, where they cannot decide present
freshness. No active path imports the Windsor connector.

No legacy table is deleted or tombstoned.

---

## Failure and recovery

| Symptom | Meaning | Action |
|---|---|---|
| `canonical_sync_never_run` | no state and no batch | run `python -m scheduler.incremental_sync` |
| `canonical_sync_failed` | interval attempted, not covered | read the batch's error; re-run |
| `canonical_source_stale` | rows exist, newest too old | re-run; the rolling window recovers it |
| `fetched_rows_not_persisted` | pull succeeded, write did not | database problem — the batch is already `failed` |
| `duplicate_natural_key` | overlapping runs duplicating | the unique index is missing or was dropped |
| `unproven_currency_lineage` | rows with no currency | excluded from verified monetary totals |

No backfill is required after deployment. The rolling recovery windows close
ordinary gaps by themselves.

---

## Read-only guarantee

Every path in this document reads Google Ads and writes only local tables. No
bid, budget, keyword, negative keyword, campaign or ad-group state is ever
modified, and every evidence result carries `external_writes_performed: false`.
A static test asserts no mutation verb is reachable from any of these modules.
