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

---

# PR-ADS-156-F1 — closing the false-green freshness paths

The structure above stood; the certification did not. Five paths could still
report a stale or unproven dataset as healthy, and each one is closed by naming
a quantity that used to be conflated with another.

## 1. Coverage is not the newest row

Freshness was derived from `MAX(source_date)`. That is wrong in both directions,
and each direction certifies something untrue:

* a dataset can be **current and empty**. Google Ads had nothing for a quiet
  fortnight; the interval was queried and came back empty. Judged by the newest
  row, a healthy account looks stale.
* a dataset can be **stale and full**. Rows persist; syncs do not. An old
  successful zero-row batch leaves no row at all, so the newest row belongs to
  whatever ran before it — and the dataset reports the freshness of a sync that
  stopped happening.

Three quantities are now published and validated separately:

| Field | Meaning | Role |
|---|---|---|
| `coverage_through` | `MAX(date_to)` over **successful** canonical batches | **freshness is measured from this** |
| `data_last_seen` | newest persisted source row date, nullable | reported, never a freshness signal |
| `verified_empty` | durable proof the queried interval returned zero canonical rows | read from the batch column, never inferred |

`coverage_through` is `MAX(date_to)`, not the `date_to` of whichever batch ran
last: a backfill repairing an old month runs last and covers an older range, and
taking its `date_to` would *retract* coverage the daily runs had established.

A stale `coverage_through` fails **even when the table is legitimately empty**.
A current `coverage_through` passes **even when the newest row is older**.

## 2. Verified-empty is durable evidence

`sync_batches` gains four columns, added by an idempotent `ADD COLUMN IF NOT
EXISTS` migration through the normal `init_db()` path:

```sql
verified_empty BOOLEAN NOT NULL DEFAULT FALSE
fetched_count  INTEGER
prepared_count INTEGER
rejected_count INTEGER
```

`row_count` continues to mean the **written** count. The other three say what it
was written from, so "0 rows" stops being one number with three possible
meanings.

The `FALSE` default is the point, not a side effect: every batch already in
production predates the marker, and some historical successful zero-row batches
were recorded while the evidence pipeline was unavailable. They stay unproven.

`finish_sync_batch` takes the four as **optional keyword arguments**; every
existing caller keeps working unchanged and records nothing it did not measure.
A `verified_empty=True` claim is *validated before it is stored* — the writer
requires a successful status with fetched, prepared, rejected and written all
explicitly zero. A caller that omits the counters has not measured the pull, and
an unmeasured pull is not evidence, so the claim is refused and logged.

## 3. Persistence violations are reachable

Emitted from the durable counters on the latest batch, not guessed at:

| Code | Condition |
|---|---|
| `fetched_rows_not_persisted` | `fetched_count > 0` and `row_count = 0` |
| `partial_persistence` | `prepared_count ≠ row_count`, or `rejected_count > 0` |
| `canonical_sync_failed` | latest attempted batch failed |
| `unproven_empty_interval` | success, nothing written, nothing fetched, **no** durable marker |
| `legacy_source_active` | a production path reads a retired evidence source |

`legacy_source_active` was previously declared and unreachable — the only
implementation of "is a legacy source active?" lived in a test module. In JSON,
a declared violation nothing can raise reads as a check that ran and passed. The
scan and its allowlist now live in `analysis/legacy_source_guard.py`, and the
audit command and the regression suite execute the **same function**.

## 4. Certification is scoped; history is disclosed

Identity, currency and duplication checks used to scan the whole table. That
makes quarantined Windsor-era rows — rows nobody will repair, which the evidence
services already exclude — permanently block the new pipeline, and a check that
can never go green is a check nobody reads.

* **Certified**: canonical-provenance rows (`source_system = 'google_ads_api'`)
  inside the certified interval — the range of the latest successful batch.
  Their defects are **blocking**.
* **Disclosed**: everything else, counted and labelled per `source_system`
  (`legacy_rows_present`). Never relabelled canonical, never repaired here, and
  never the reason current freshness fails.

Nothing historical is backfilled, rewritten or deleted.

## 5. Search-term identity is real

`search_terms` gains a nullable `customer_id`, bringing it to parity with
`keyword_daily_facts`. A newly ingested canonical row must carry:

source date · non-empty search term · campaign ID · ad-group identity (the
`ad_group` name, which is what the current natural key stores) · Google Ads
customer ID · `source = google_ads_api` provenance.

Rows missing any of these are rejected by reason and counted in
`rejected_count`; nothing is dropped quietly. Historical rows stay NULL — a
back-filled guess would be inventing provenance — and are reported as
disclosure, never reinterpreted as current canonical failures.

## 6. One search-term pull per scheduler execution

The weekly and monthly runs pulled search terms **twice**: once at step 1 for
the JSON snapshot, and again through the canonical service later in the run.
Two queries for one dataset is two answers nothing reconciles.

Each run now performs exactly one canonical pull, with `include_rows=True`, and
the rows it returns are the ones analysed — the junk-query check, the n-grams
and the reports all read what was actually persisted. Rows are adopted only
after the `ok` check: `rows` holds what the pull *prepared*, which on a partial
write is not what the database holds.

When the sync fails, downstream search-term analysis is **unavailable**, not
empty: `ngram_data=None` reaches the report as "unavailable", and
`data/ads_search_terms.json` is left untouched rather than overwritten with `[]`
— which would destroy the last surviving copy of the previous observation and
make an outage look like a quiet week. `save_output` now distinguishes `None`
("not measured, leave the file alone") from `[]` ("measured as empty").

Each trigger keeps its own window: daily 14 days, weekly 60, monthly 30.

## 7. One account calendar

`search_term_sync_service._account_today` resolved a canonical window and, on
**any** exception, quietly returned the UTC date. Between 23:00 and 00:00 UTC in
British Summer Time those are different days, so a transient failure could shift
the requested interval by one date and record the wrong day as covered —
silently, because the fallback logged at DEBUG.

It is now a thin alias for `analysis.account_time.account_today`, the helper
Campaign and Keyword Evidence already use. There is no fallback: keyword and
search-term intervals resolve through one function or not at all.

## Failure and recovery (F1 additions)

| Symptom | Meaning | Action |
|---|---|---|
| `canonical_source_stale` | **proven coverage** is old, whatever the rows say | re-run; the rolling window recovers it |
| `unproven_empty_interval` | a zero-row success with no durable marker | re-run through the canonical service, which records the marker |
| `partial_persistence` | prepared ≠ written, or rows rejected | read `rejected_count` and the batch error |
| `legacy_source_active` | a production path reads a retired source | remove the read, or justify it in the allowlist |
| `legacy_rows_present` (disclosure) | historical rows outside the certified interval | none — informational |

---

# PR-ADS-156-F2 — the remaining stale-analysis and durability gaps

F1 closed five false-green paths. Five more remained, each a place where
something was assumed rather than checked.

## 1. Waste detection was reading the snapshot F1 preserved

F1 stopped the weekly and monthly schedulers overwriting
`data/ads_search_terms.json` when the canonical sync failed, so an outage could
not masquerade as a quiet week. `run_waste_detection()` then **reloaded that
preserved file** and published findings from it stamped with the current run's
timestamp. The snapshot was protected from being destroyed and immediately
reused as though it were current — the same falsehood from the other side.

The canonical input is now **passed in**, with an explicit availability flag:

| Input | Behaviour |
|---|---|
| `search_term_evidence_available=False` (or no rows at all) | no analysis; the report is marked unavailable and carries no items |
| rows supplied, available | exactly those rows are analysed — the ones the sync persisted |
| `[]` supplied, available | a verified-empty population: zero findings, no substitution |

The **keyword fallback is gone with the snapshot read**. An empty search-term
population used to silently become a keyword-level analysis, which answers a
different question under the same heading: a verified-empty interval is a
genuine measurement *of search terms*, not a gap to be filled with a different
population. `analysis/rule_advisor.py` now reports the unavailable state instead
of the removed "20–40% higher" fallback warning.

The unavailable report is **written**, not skipped. Leaving the previous
`waste_report.json` in place would be worse than either alternative: its own
`generated_at` is what every reader uses to judge currency, so a preserved
report reads as this week's findings. An explicitly empty, explicitly
unavailable report cannot be mistaken for either.

And a `waste_terms` sync batch is finished **`failed`**, never `success`, when
the evidence never arrived — a `success` advances a freshness watermark, and
doing that from evidence that does not exist reports the dataset current over an
interval nobody measured.

## 2. An unfinalized batch is not a covered interval

`finish_sync_batch` returns a Boolean and both canonical services ignored it. So
a run could fetch (or verify empty), have its final batch update fail, and still
return `ok=True` — reporting the interval covered while coverage and
verified-empty proof were never durably recorded.

This is the subtlest false green of all, because the DATA would be fine and only
the proof of it missing, so nothing else in the system would ever notice.

Every relevant result is now captured. On a finalization failure both services
return `ok=false`, `verified_empty=false` and a `batch_finalization_failed`
reason, and `evidence_status` cannot read ready. For a non-empty pull whose rows
were written, both facts travel separately:

| Field | Meaning |
|---|---|
| `written` | what this run **certifies** — `0` |
| `rows_possibly_written` | what may nevertheless be in the table |
| `batch_finalized` | whether the certificate persisted |

Reporting only the first would be a false green; reporting only the second would
send someone hunting for data that is already there.

## 3. The account belongs in the natural key

F1 declared `customer_id` part of canonical search-term identity and made the
service reject rows without it — but the UNIQUE index did not contain it. The
contract said two accounts are distinguishable while the index said they are the
same row: two otherwise identical observations from different Google Ads
customers would silently upsert over each other. That is worse than having no
customer column, because the contract invites people to rely on it.

`idx_search_terms_unique_fact` is rebuilt, **keeping its name** (the repository
and the evidence service document the dedup key by that name), to:

```
source_date · customer_id · campaign_name · campaign_id · ad_group ·
keyword · match_type · search_term      (COALESCE on every nullable column)
```

The writer's `ON CONFLICT` target and the audit's duplicate grouping use exactly
the same key — a target that does not match a unique index is a runtime error,
one that matches the *wrong* index silently merges accounts, and an audit
grouping on a narrower key reports correct rows as duplicates. The null-twin
supersession delete is scoped by account too, so an id-bearing row from one
customer cannot delete another customer's row.

The migration is guarded on the index **definition**, so it rebuilds once and a
redeploy is a no-op. Adding a column to a unique key can only make it more
permissive, so the rebuild cannot fail on existing rows, and DDL is transactional
in PostgreSQL, so there is no window without a unique key. Historical rows keep
`customer_id IS NULL` — no account identity is invented for them.

## 4. The legacy guard now sees indirect reads

The shared guard read literal SQL in two directories. A service that imports a
repository module and calls `repo.fetch_keyword_theme_snapshot(...)` contains no
legacy SQL at all — the SQL is in the repository — so it passed cleanly.

Names are what cross a module boundary, so names are what the guard reads now.
It detects, by import **or** call:

* retired providers (`windsor_pull`, `windsor_mcp`, …);
* legacy keyword/search-term repository helpers;
* consumption of a retired local JSON snapshot as current evidence;
* a keyword-population fallback inside search-term evidence;

plus the original literal-SQL and `write_keywords(` checks. `PRODUCTION_DIRS`
widened from `scheduler`/`services` to include `analysis`, `api` and `db` — a
legacy read in those reaches the page by a different route, not a less real one.

Two allowlist entries were added and justified: the guard module itself (it must
name the markers it detects) and `services/dashboard_campaigns_service.py` (the
keyword-theme snapshot behind the Campaigns page — one of the four inspected
non-evidence consumers). The CLI audit and the tests still execute the same
function.

## 5. The audit answers on the account's calendar or not at all

F1 removed the silent UTC fallback from the sync service. The audit still had
one: it caught canonical-window resolution errors and used the UTC date.

Around midnight in British Summer Time the account day and the UTC day differ,
so substituting one for the other moves the staleness boundary by a full day —
silently. The audit now resolves today through the same
`analysis.account_time.account_today` the services use. When the effective
account calendar cannot be resolved it emits `account_calendar_unresolved`,
reports the audit unavailable, and exits `2`. An audit that refuses to answer is
visible; a wrong date is not.

## Failure and recovery (F2 additions)

| Symptom | Meaning | Action |
|---|---|---|
| `batch_finalization_failed` | rows may be stored; the certificate is not | re-run the sync — the upsert is idempotent |
| `account_calendar_unresolved` | the account's today could not be resolved | check `tzdata` in the image; no freshness verdict is produced until it is |
| `legacy_source_active` (indirect) | a production path calls a legacy helper | remove the call, or justify it in the allowlist |
| `waste_report.json` with `search_term_evidence_available: false` | the run had no search-term evidence | not a finding of zero waste; fix the sync and re-run |

---

# PR-ADS-156-F3 — completing the search-term account-identity cutover

## Exact root cause

The ingestion side of PR-ADS-156 worked. The first production sync on
`31dad24` fetched and wrote **16,267** search terms and **3,895** keyword facts,
rejected nothing, left zero missing identities on the latest batch, and reported
`evidence_status: ready` with no external writes.

The freshness audit failed anyway, and it was right to:

| Diagnosis | Value |
|---|---|
| latest batch | `1378` |
| certified window | `2026-08-23 → 2026-09-05` |
| total rows in that window | **32,367** |
| rows missing identity | **16,100** |
| missing identities in the latest batch | 0 |
| missing identities from earlier batches | 16,100 |
| missing `customer_id` | 16,100 |
| missing campaign id / ad group / search term | 0 / 0 / 0 |

F2 put `customer_id` into the natural key. Under the NEW key a complete row and
its account-less predecessor are **different rows**, so the complete rows did
not conflict with the old ones, did not supersede them, and both populations
stayed — almost exactly one stale twin per new row.

**The new ingestion is correct. The cutover is incomplete.**

And it was never only an audit-display problem: every reader that queried
`search_terms` on a date window alone counted both copies.

## 1. One canonical scope

`analysis/search_term_scope.py` defines the population, and every reader
composes it. A row is canonical only when it proves all four:

* `source_system = 'google_ads_api'`;
* a non-empty account identity;
* that identity equals the effective configured Google Ads customer;
* complete campaign / ad-group / search-term identity.

Both exact spellings of the account are accepted (`1234567890` and
`123-456-7890`) because which one reached the column depends on how the variable
was typed that day — a fixed candidate set, never a pattern, never NULL.

When the account cannot be resolved the scope is **unavailable**. It does not
widen to every account and does not admit null-account rows. There is one
configured account today, which makes "just read everything" look harmless; that
is the trap, because the day a second account exists every historical total
silently changes meaning and nothing marks when it happened.

No account id is ever invented for a historical row.

## 2. Every production reader

| Reader | Route |
|---|---|
| `/api/search-terms` (rows + count) | scope seeded into `base_conditions` |
| `/api/search-terms/summary` | scope seeded into both the filtered and base clauses |
| `/api/search-terms/ngrams` | scope seeded into `conditions` |
| Search Terms diagnostics verdict | every count scoped; unscoped rows reported separately |
| `fetch_search_term_aggregates` | `canonical_scope(start, end)` |
| `fetch_search_term_daily_costs` | `canonical_scope(start, end)` |
| `fetch_search_term_daily_for_campaign` | `canonical_scope(start, end)` |
| `revenue_repository.fetch_search_term_signals` | `canonical_scope(start, end)` |
| Search Terms + Patterns evidence | via the scoped repository |
| Flagged / Waste evidence + Action Queue | via the scoped repository |
| Dashboard Campaign search-term signals | via `fetch_search_term_signals` |

`fetch_legacy_currency_audit` stays deliberately unscoped — it is the diagnostic
that reports legacy rows, and scoping it would hide the thing it exists to show.

Legacy and unscoped rows are disclosed, never counted. An n-gram is a count of
phrases across the source rows, so a duplicated population does not merely
inflate a total — it changes which phrases rank as wasteful.

## 3. Deterministic supersession of exact twins

`write_search_terms` now supersedes a `customer_id IS NULL` twin, and only when
**every** other natural-key component matches exactly — source date, campaign
name, campaign id, ad group, keyword, match type, search term — and the twin
carries canonical Google Ads provenance.

Before the twin is removed, its durable LOCAL analysis state is carried across:
`is_flagged_waste`, `junk_category`, `matched_pattern`. Those exist nowhere
upstream; deleting the twin without them would silently un-review work someone
did. `COALESCE`, so a decision already on the canonical row always wins.

Never touched: a row belonging to another non-null customer; a Windsor or
unknown-provenance row; a row differing in any key component; an unmatched
historical row. The `DELETE` is gated on an `EXISTS` for the replacement, so a
twin is removed only because its complete counterpart is demonstrably present.

Upsert, carry-over and delete run in **one transaction**, and repeating the sync
changes nothing but timestamps. There is no startup deletion and no blanket
`UPDATE customer_id = …`: the ordinary rolling window closes the gap as the
source returns each complete replacement.

## 4. One declared key

`SEARCH_TERMS_NATURAL_KEY` moved into `analysis/search_term_scope.py` beside the
scope it describes, and now reads:

```
source_date + COALESCE(customer_id,'') + COALESCE(campaign_name,'') +
COALESCE(campaign_id,'') + COALESCE(ad_group,'') + COALESCE(keyword,'') +
COALESCE(match_type,'') + search_term
```

The repository re-exports it, the evidence service's provenance payload declares
the account-first grain with `account_scoped: true`, and the identity module says
so too. A contract describing a key that no longer exists is worse than none,
because people act on it.

## 5. A strict, explanatory audit

The audit measures **both** populations. The account-scoped one is what it
certifies; the provenance-only one is what the cutover has to be complete over.
Measuring only the first would let the narrower filter certify a cutover that
never happened — the null-account rows would simply fall outside `current`,
`rows_missing_identity` would read 0, and the audit would agree with readers
that had merely stopped looking at them.

Three causes, three codes, no double-reporting:

| Code | Meaning | Blocking |
|---|---|---|
| `pre_cutover_null_customer_twin` | a null-account row still coexists with its EXACT replacement | yes |
| `missing_identity` | this account's own row lacks full identity, explained by neither a twin nor history | yes |
| `unmatched_null_customer_rows_excluded` | a null-account row with no replacement — history | disclosure |
| `google_ads_customer_not_configured` | no account to certify; not "everything" — nothing | exit 2 |

Freshness is still measured from successful batch coverage, never the newest
row, and the command remains strictly read-only.

## Production procedure after merge

No manual SQL delete. No historical backfill.

1. Confirm the deployed merge SHA.
2. Run one normal incremental sync.
3. Confirm the latest search-term batch still has zero missing identities.
4. Confirm exact null-customer twins in its covered interval are zero.
5. Run `python -m scripts.audit_keyword_search_term_freshness`.
6. Require both datasets `ok=true`, no blocking violation codes, exit `0`.
7. Confirm `external_writes_performed` remains false.
8. Run the cross-page parity regression. The already-deferred all-time
   missing-deal-amount exception is unrelated and unchanged.

Unmatched historical rows will remain, disclosed and uncounted. That is the
correct end state: they describe observations nobody can attribute to an
account, and inventing one for them would be fabricating provenance.

---

# PR-ADS-156-F3 review corrections

Four findings from the F3 review. One was an arithmetic defect that could turn
the audit green over a database that still had the problem; three were places
the cutover had been carried less far than the tests suggested.

## 1. The residual may only be reduced by subsets of its own population

`missing_identity` is reported over the residual — the canonical-provenance rows
missing identity, minus those already explained as un-superseded twins and minus
unmatched history — so that one broken row produces one code rather than three.

That subtraction is valid only while both subtrahends are **subsets of the
minuend**. The orphan query was provenance-blind: it counted every account-less
row in the window, including Windsor and unlabelled ones. So one unmatched
Windsor row could cancel one genuinely malformed Google Ads row, and the audit
would certify a cutover it had not verified.

Both cutover queries now bind canonical provenance on **both sides**, and the
candidate replacement must also carry the configured account — another account's
row is a different observation, not a supersession, and a Windsor row is not a
replacement for anything.

| Count | Population | Used for |
|---|---|---|
| `null_customer_twins` | account-less, canonical, HAS an exact canonical replacement for this account | blocking violation, subtracted |
| `unmatched_null_customer_rows` | account-less, canonical, no replacement | disclosure, subtracted |
| `noncanonical_null_customer_rows` | account-less, NOT canonical provenance | disclosure only — never subtracted |

The third is deliberately inert. It is reported so the payload accounts for
every row an operator can see, and a source-level test asserts it can never be
added to the residual later.

## 2. Operational commands read what the product reads

`verify_search_terms_pipeline` and `audit_search_term_waste_truth` still bounded
on `source_date` alone. During the cutover they reported roughly double — so the
command an operator runs to check the pipeline would have confirmed the
duplicated table as healthy.

Both now compose their predicates from `analysis.search_term_scope` and fail
closed on an unresolved account, the verifier under its own
`ACCOUNT_NOT_CONFIGURED` verdict (exit 2), checked **before** the row counts so a
configuration problem is never reported as an empty pipeline.

Quality counts moved to a new `claimed_scope()` — correct provenance, correct
account, identity-completeness clauses deliberately omitted. Counting malformed
rows inside a filter that requires them to be well-formed reads zero forever over
any table: **a filter must not contain the thing it is measuring.**

A static guard, `scan_unscoped_search_term_readers`, now prevents the next one.
It scans `scripts/` as well as the production directories, judges fully literal
queries exactly (their whole predicate is visible) and variable-built ones by
whether their function obtains a scope, ignores writers, migrations and schema
operations, and exempts only allowlisted historical diagnostics —
`fetch_legacy_currency_audit`, whose entire purpose is to count what the
canonical scope excludes.

## 3. Endpoint tests execute, they do not inspect

The F3 endpoint tests asserted over the AST. That proves a call was written; it
cannot prove the composed SQL binds its parameters in the right order — and
order is what breaks when a predicate is spliced into a hand-built WHERE clause,
because placeholders fill by position and a predicate inserted at the front
shifts every argument after it. A misordered query does not raise. It compares
the search text against a date and returns nothing, which looks like "no
matches".

All three endpoints now run against PostgreSQL seeded with one canonical row,
its exact pre-cutover twin, another account's row and a Windsor row.

## 4. The suite-wide account default cannot hide fail-closed

`tests/conftest.py` gives the session a configured account, which is right —
without one every scoped read would take the unavailable branch by accident. The
cost is that a default which is always present makes fail-closed untestable by
default: nothing would notice a consumer that stopped handling an unresolved
account, because nothing would ever hand it one.

The fixture is kept and the claim is now enforced: the default is proven to
yield to any test that deletes it; every repository reader, endpoint and
operational command is asserted unavailable with it removed, **including over a
populated table** — the only case where fail-closed matters; and the registry of
scoped consumers is checked for exhaustiveness against the source tree, so a new
one cannot be added without coverage.

Nothing in this change writes to Google Ads or HubSpot, deletes historical rows,
invents an account identity, or performs a backfill.

---

# PR-ADS-156-F3 final review correction

Three findings. One was a SQL correctness bug that silently lost rows from both
populations at once; one was a fail-closed path that existed in the payload but
not in the verdict; one was a guard weak enough to certify what it was meant to
catch.

## 1. The verdict endpoint fails closed on an unresolved account

`_build_search_terms_verdict()` resolved the scope, recorded
`db.canonical_scope_available: false`, and then called
`compute_search_terms_verdict()` without passing it. With no account the
predicate is `FALSE`, so every count came back `0` — correctly — and the verdict
function read those zeros as evidence **about the pipeline**.

| | reviewed `b7c964d` | corrected |
|---|---|---|
| `verdict` | `NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT` | `ACCOUNT_NOT_CONFIGURED` |
| `reason` | "No weekly run found and no sync state…" | names `GOOGLE_ADS_CUSTOMER_ID` |
| `db.canonical_scope_available` | `false` | `false` |
| `db.canonical_scope_reason` | `null` | `google_ads_customer_not_configured` |
| `db.rows_30d` | `0` | `null` |
| `db.spend_rows` | `0` | `null` |
| `api.total_rows_in_window` | `0` | `null` |
| `next_action` | "Run scheduler…" | "Set `GOOGLE_ADS_CUSTOMER_ID`…" |

Measured over a table holding four rows. The truth was already in the payload
and the headline contradicted it, which is worse than omitting it — people act
on headlines. The old next action would have had an operator run a scheduler
that was working.

The scope is now resolved and **rejected before any population query runs**, and
the counts are omitted rather than reported as zero: a zero here is not a
measurement of this account's population, it is the absence of an account to
measure. Database availability is still reported independently — the database is
fine, and saying otherwise sends the operator somewhere else wrong.

`ACCOUNT_NOT_CONFIGURED` and `API_HAS_ROWS_UI_LIKELY_FILTERED` both gained
entries in `next_actions`; a test now asserts every declared verdict has an
operator instruction, comparing by value so the retired Windsor-era aliases do
not read as gaps.

## 2. The historical complement is NULL-safe

`unscoped_history_scope()` built `NOT (<canonical predicate>)`. That is not the
complement of a predicate in SQL, because SQL is three-valued: the canonical
predicate contains `customer_id = ANY(%s)`, which is **NULL — not FALSE** — for a
row with no account, so the conjunction is NULL and `NOT NULL` is NULL again.

A row that is NULL under both predicates is counted by **neither**. The rows
that fell through are exactly the pre-cutover account-less twins this cutover is
about: correctly invisible in the canonical totals, and wrongly invisible in the
disclosure that exists to reveal them.

Five rows — one canonical, one canonical-provenance with no account, one
Windsor, one other account, one this account with incomplete identity:

| | `NOT (…)` | `(…) IS NOT TRUE` |
|---|---|---|
| canonical | 1 | 1 |
| historical | 3 | **4** |
| **in neither population** | **1** | **0** |

The two scopes now partition the window: every row lands in exactly one, and the
counts add to the total.

`null_customer_rows_in_window` is a **diagnostic subset** of
`unscoped_historical_rows_in_window`, not a population beside it. Now that the
complement is NULL-safe the two overlap, so the payload carries
`null_customer_rows_note` saying so — two adjacent counts invite addition, and
adding these double-counts the rows the cutover is about.

## 3. The reader guard checks the query, not the function

The guard accepted any dynamic query whose enclosing function called a `*_scope`
factory anywhere. **Calling a factory is not using its result**: a function
could resolve a scope, check `available`, and then run a date-only query three
lines later with the guard's blessing.

It now runs an intra-function taint analysis to a fixpoint. A name is
scope-carrying when it is assigned from an expression that calls a scope factory
or references an already-tainted name, and the query passes only when **its own
SQL expression** references such a name. That follows the chain production
actually writes — `scope` into `conditions` into `where_sql` into the query —
without special-casing any of their names.

It deliberately does not follow a scope through a helper function, through
`self`, or into a container mutated by a method call it was not seeded with.
Those surface as findings rather than silent passes, which is the correct
direction for a guard to be wrong in: a false finding gets argued about and then
allowlisted with a reason, while a false pass is never noticed at all.

Still zero findings on the tree — the production readers do not merely call a
scope factory, they use what it returns.

Nothing in this change alters ingestion, deletes production rows, runs a
backfill, or touches Platform Evidence UI.

---

# PR-ADS-156-F4 — residual exact twins across the certified interval

## Root cause

F3 reduced the production duplicate population from **16,100** exact
account-less twins to exactly **one**, and the audit kept blocking with
`pre_cutover_null_customer_twin`:

| | |
|---|---|
| source_date | 2026-09-04 |
| campaign | `global - competitors` (id `23094767513`) |
| ad_group | `Competitors List` |
| keyword / match_type | empty / empty |
| search_term | `winfleet` |
| legacy twin | id 330284, run/batch 164 / 1355 |
| canonical row | id 331967, run/batch 166 / **1378** |
| latest successful batch | **1405** |

The canonical row still carrying batch 1378 is the whole diagnosis. Batch 1405
covered that date and **did not return this identity**.

Google Ads search-term reporting is **mutable**: an identity present in one pull
can be absent from the next. An older canonical observation therefore stays
stored while disappearing from later pulls — and F3 supersedes twins only for
identities present in the **current input rows**. The key that would have
matched this twin was never in a later pull, so per-row supersession could not
reach it, and it sat inside a newly certified interval untouched.

That is not a flaw in F3's rule. It is a gap in its **reach**.

## The change

The reconciliation is now performed by **interval** as well as by input row,
inside the same write transaction. It asks a different question — not "did this
pull mention that identity" but "does this interval still hold an exact twin of
a canonical row".

Every safety condition is unchanged. A twin is superseded only when it is
account-less, carries canonical Google Ads provenance, and matches a replacement
on **all seven** remaining natural-key components; the replacement must belong to
the configured account (exact hyphenated or unhyphenated spelling), carry
canonical provenance, and have complete campaign / ad-group / term identity. The
`DELETE` is gated on an `EXISTS`, so an unmatched historical row has nothing to
satisfy it.

The audit is **not weakened anywhere**. It stops blocking because the condition
it reports genuinely stops being true.

### Before / after, on a real database

Fixture: the production pair, plus 24 unmatched account-less rows standing in
for the disclosed history.

| | before | after |
|---|---|---|
| `violation_codes` | `pre_cutover_null_customer_twin` | *(none for identity)* |
| `null_customer_twins` | **1** | **0** |
| `unmatched_null_customer_rows` | 24 | **24** — untouched |
| `current.row_count` | 1 | 2 |
| `duplicate_natural_key_groups` | 0 | 0 |
| rows missing identity (provenance) | 25 | 24 |
| **total rows in table** | 26 | **26** |

26 → 26 is the point: one twin removed, one upstream row written. The disclosed
history is exactly as it was.

## Bounds, counts and failure

* The **requested** interval is passed explicitly from the sync service, not
  inferred from the returned rows — inferring it would shrink the swept span by
  precisely the dates whose identities went missing, which is the entire class
  of row this exists to reach. Without explicit bounds the prepared rows' own
  span is used: still bounded, never table-wide.
* An **unresolved account sweeps nothing**. There is no fallback to matching
  every account.
* `row_count` still means **upstream rows written** and stays comparable with
  `fetched` and `prepared`. Superseded twins are reconciliation actions, not
  source rows, and are logged separately as `residual_twins_superseded=<count>`.
* The sweep runs in the **same transaction** as the upsert. If it fails, the
  upsert rolls back with it and the writer returns 0, which the sync service
  reads as failed persistence and records a `failed` batch. A reconciliation
  that did not complete never leaves an interval certified.

## Not done here

No manual SQL cleanup, no production migration, no historical backfill, and no
special case for row `330284` or the term `winfleet` — there is no such literal
in the implementation. The production row disappears because it satisfies the
general rule.

## Production procedure after deployment

1. Confirm the deployed SHA.
2. Run one incremental sync.
3. Run `python -m scripts.audit_keyword_search_term_freshness`.

Acceptance: sync exit `0`; fetched = prepared = written; rejected `0`; evidence
status ready; null-account twins `0`; missing identity `0`; duplicate keys `0`;
audit `ok = True` and exit `0`.

## F4 review correction — the verified-empty path certifies an interval too

`sync_search_terms` handles `fetched == 0` **before** it ever calls the writer.
The first cut of F4 therefore left exactly one certified interval unreconciled:
a verified-empty pull created a successful certified interval, never ran the
residual reconciliation, and could leave an exact account-less twin sitting
inside it while returning `ok=True` and `verified_empty=True`.

That contradicts F4's own invariant — *a reconciliation that did not complete
never leaves an interval certified* — and it is reachable rather than
theoretical, for the same reason F4 exists: mutable reporting means a stored
canonical identity and its twin can both sit inside an interval a later pull
returns nothing for.

Both paths now run the **same rule through the same helper**,
`_execute_residual_twin_reconciliation`, which takes an open cursor so the
caller owns the transaction. What differs is only the transaction: the non-empty
path shares the upsert's; the empty path, having no upsert to share, gets one of
its own via `reconcile_residual_search_term_twins`.

### Why the entry point returns a result, not a count

`write_search_terms` returns an integer, and `0` means both *nothing needed
superseding* and *the write failed*. Those decide **opposite** things about
certifying an interval, so a caller inferring success from the count would
certify on failure. The entry point returns
`{"ok": bool, "superseded": int, "reason": str | None}` instead.

| Outcome | `ok` | Effect |
|---|---|---|
| reconciled, 0 or more twins | `True` | batch may finalize successful and verified empty |
| database unavailable / SQL error | `False` | batch `failed`, `verified_empty=False`, no coverage advance |
| no account resolves | `True`, `reason` set | nothing to reconcile; disclosed, audit remains the fail-closed gate |

The third row is a judgement call worth naming: an unresolved account is not a
reconciliation *failure*, it is the absence of a population to reconcile —
exactly how `canonical_scope` treats it. It is reported so the caller can
disclose it, and the freshness audit already exits `2` on an unconfigured
account, so nothing is certified quietly.

### Failure contract, verified-empty path

On failure: batch finalized `failed`; `verified_empty=False`; **no**
`last_source_date`, so proven coverage does not advance; `ok=False`; the reason
disclosed under its own code `residual_twin_reconciliation_failed` rather than a
generic persistence error — the pull may have been perfectly healthy, and
"persistence failed" would send an operator to the wrong place. Any partial
cleanup rolls back: the carry-over and the delete share one transaction, so an
interrupted run cannot leave annotations moved onto a canonical row whose twin
is still there.

On success: `row_count=0`, `fetched=0`, `prepared=0`, `rejected=0` all unchanged
— a superseded twin is never an upstream row — with the count reported as
`residual_twins_superseded` on the result and in the log.
