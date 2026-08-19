# Canonical Country Geography (PR-ADS-153F)

**Status:** built, August 2026. Read-only against every external platform.
**Depends on:** PR-ADS-153E-B (canonical revenue consumer cutover, merged at
`836dc8d`).
**Blocks:** PR-ADS-153G (legacy table/route retirement) — which remains a
separate PR and is **not** started here.

---

## 1. What was wrong

The PR-ADS-153A audit (§1.4) found that country truth was blocked "by a missing
owner, not by a wrong gate". The ROAS by Country blocking behaviour was correct;
everything upstream of it was not.

| Defect | Consequence |
|---|---|
| Nothing scheduled `run_google_ads_geo_sync` — its only caller was an admin endpoint | Canonical geo went stale the moment the window advanced past the last manual click. The page blocked itself, correctly, for a reason no health surface could show. |
| Geo had no coverage ledger, no resume, no per-range failure evidence, no freshness entry | A range that was never fetched and a range that was fetched and genuinely had no country-attributable spend were indistinguishable. Staleness was invisible. |
| Three different country join rules | ROAS by Country grouped on a lowercased string, Dashboard Countries grouped ISO-code-first, the drilldown used code-then-name. The same window produced different rows on pages describing the same thing. |
| Blank-country revenue dropped from ROAS by Country | Revenue that exists simply vanished from one page while Dashboard Countries preserved it as a residual — so the two pages reported different totals. |
| `_CODE_TO_NAME` missing 11 codes; any 2-letter token accepted as ISO | A country could resolve forward and be nameless backward; `"XX"` was as valid as `"AE"`. |
| The mart and Dashboard Countries used different readiness bars | A window could be "ready" on one page and reported as differing on the other. |
| Three freshness datasets had no writer; `canonical_spend`'s key did not match its writer | Datasets that looked monitored could only ever report "never run", and the ROAS denominator had no freshness signal at all. |

The governing rule this restores:

> Same metric + same business window + same **scope** = the same result on
> every page.

---

## 2. Source ownership (unchanged)

| Fact | Owner | Notes |
|---|---|---|
| Advertising spend by country | **Google Ads** `geographic_view` | Where spend occurred. Structurally omits location-less spend — see §6. |
| Clicks, impressions, campaign identifiers, search terms | **Google Ads** | |
| Lifecycle state, SQLs, deals, won status, close date, revenue | **HubSpot**, through the canonical deal ledger | |
| CRM/contact country used for revenue analysis | **HubSpot** | Contact/IP geography, not advertising geography. |

**These are different facts about different entities.** They are joined at
reporting grain on a shared country key so markets can be compared; the join is
**estimate-grade** and is disclosed as such in every country response
(`country_truth.estimate_grade_note`).

Neither source defines the other:

* Google Ads country data never defines the won-deal population or total revenue.
* Google Ads conversion value is never treated as revenue.
* HubSpot country never replaces Google Ads spend geography.
* Attribution coverage is never presented as business-population coverage.
* Ambiguous contact-country evidence stays visible and unassigned.

---

## 3. The canonical country key contract

`analysis/country_identity.py` is the ONE contract. Every consumer groups on
`country_key(...)` and nothing else.

```
country_key(name=None, code=None) -> "code:XX" | "unknown"
```

Rules:

1. **A validated ISO 3166-1 alpha-2 code is the identity.** Names are an input to
   resolution and a display label — never a join key.
2. **A supplied code outranks a name**, because a code is an identity and a name
   is a spelling. A malformed code with a good label still resolves (dropping
   real revenue because one field was wrong would be worse) and the rejection is
   recorded in `reason`, so it stays auditable.
3. **A two-letter token is not a country code.** Only codes in
   `SUPPORTED_COUNTRIES` resolve. `"XX"`, `"ZZ"` and a truncated label do not.
4. **Both directions come from one registry.** `SUPPORTED_COUNTRIES` (code →
   canonical label) drives the alias table and the reverse lookup, so "resolvable
   forward, nameless backward" — the state that left SG, MY, ID, TH, VN, PH, AU,
   NZ, LK, ZA and NG without labels — is unrepresentable.
5. **Normalization is locale-independent**: ASCII casefold over an explicit alias
   table. No `title()`, no locale-sensitive transform whose output depends on the
   server's environment.
6. **Geography that cannot be identified is never dropped and never guessed.**

### Resolution statuses

| Status | Meaning | Key |
|---|---|---|
| `valid` | Resolved to a supported ISO code | `code:XX` |
| `unknown` | No country evidence at all (blank/null) | `unknown` |
| `invalid` | Evidence exists but resolves to no supported country | `unknown` |
| `residual` | The row IS the explicit unattributed bucket | `unknown` |

The three non-country statuses share the residual key but keep their distinct
status and `reason`, so a page can say *why* a row is in the residual.

---

## 4. Scheduling, coverage and resume

### Pipeline order (a contract, not a coincidence)

The daily incremental sync runs, in this order:

1. `google_ads/canonical_spend` — the reconciliation baseline
2. `fx/daily_rates` — required before any USD geo figure is safe
3. `google_ads/canonical_geo` — reconciled against (1), converted using (2)
4. `google_ads/geo_reconciliation` — evaluated only after 1–3 have landed

Reconciling before geo lands would score the previous run's coverage.

### Durable evidence

| Table | Purpose |
|---|---|
| `google_ads_geo_coverage` | Per-chunk fetch ledger: customer, chunk range, `verified`/`failed`, rows written, cost micros, country count, internal error text, run id. |
| `google_ads_geo_sync_state` | One row per (customer, scope): run status, timestamps, resume `checkpoint_date`, `last_successful_completed_at`, chunk counters, last error. Also carries the run lease. |

Both are **additive**. `google_ads_geo_coverage` is a separate table rather than
a `dataset` column on `google_ads_spend_coverage` because that table's identity
is `(customer_id, chunk_start, chunk_end)` — campaign and geo coverage for the
same range would collide on one row and overwrite each other, and widening a
production unique key is a destructive migration.

### The rules the ledger enforces

* **A refresh REPLACES its range; it does not merge into it.**
  `replace_geo_daily_spend_chunk` validates every row (right customer, inside
  the range), deletes the range and inserts the new response in ONE transaction.
  A merge-only write cannot express "this row no longer exists", so a
  country/campaign/day Google restates away would keep its old row and the chunk
  would then be certified over spend Google no longer reports. An **empty**
  response is an explicit success: the range genuinely becomes empty.
* A chunk is marked `verified` **only after that replacement commits**. A read
  that did not persist raises and is recorded `failed`.
* A `failed` write **never demotes** a chunk that is already `verified`, so a
  transient API error during recovery cannot erase proven coverage. The reverse
  (repairing a failed chunk) does work.
* A failed chunk is written as `failed`, never left absent — so the next run
  re-fetches exactly it.
* An already-`verified` chunk is **skipped** on a recovery run.
* Completeness is re-read from the ledger, not inferred from a run's own
  counters: a run that verified everything it attempted is still incomplete if an
  earlier failure elsewhere in the window is unrepaired.
* `checkpoint_date` and `last_successful_completed_at` advance **only** when the
  ledger says the requested window is fully covered. A partial run cannot publish
  complete coverage or healthy freshness.
* An unreadable coverage ledger causes a **full re-fetch**, never a skip:
  "unreadable" and "nothing covered" are different facts and only one is safe to
  skip on.
* **Coverage is account-scoped.** Every coverage read takes a mandatory
  `customer_id`. Reading the ledger unscoped would let account A's verified
  chunks make account B look covered, and let A's history skip a fetch B never
  performed.

The daily lookback re-fetches the last 7 days on every run because Google Ads
restates recent spend — so a daily chunk is never "already verified", while
historical months are.

**A seven-day refresh does not bootstrap history.** On a fresh coverage ledger
it cannot prove `current_quarter`, `last_quarter`, `last_6_months`, `ytd` or
`all_time`; those windows correctly stay blocked until history is covered. Run
`scripts/backfill_canonical_geo.py` once after deployment (see §9). It delegates
to the same sync function, so it shares one implementation of the lease, the
range replacement, the coverage ledger and the checkpoint — and it exits
non-zero unless the ledger itself proves the requested range covered.

### The run lease — fails closed, fenced by token

Render runs more than one instance and the manual recovery trigger can fire at
any moment, so the overlap guard is a durable conditional `UPDATE`, not a
process-local flag. It is tri-state, and **two of the three states stop the
run before any Google Ads call**:

| Result | Run outcome | Google Ads calls |
|---|---|---|
| `acquired` | proceeds, carrying a unique fencing token | as needed |
| `held` | `skipped_locked` — benign, another worker owns the range | **zero** |
| `unavailable` | **`failed`**, reason `lease_store_unavailable` | **zero** |

An earlier revision let an `unavailable` lease store proceed without a lease, on
the argument that a visible stale run beat a silent skip. **That was wrong.**
With the store unreachable the run cannot persist geo rows, coverage or state
either — so proceeding buys no visibility at all; it only spends Google Ads
quota and risks an uncoordinated concurrent fetch.

Stopping is right for both, but they are **not the same outcome**. `held` means
the system is working exactly as designed. `unavailable` means the coordination
store could not be reached at all, and reporting that as a lock skip described a
worker that does not exist — geo could then stop syncing for as long as the
database stayed down while every freshness surface stayed calm. The scheduler
therefore records `unavailable` as a **failed sync batch** and adds it to the
run's error list; only `held` is a silent skip, because the worker that holds
the lease will record the real outcome.

**Expiry is recovery; the token is ownership.** A lease carries a stored
`lease_expires_at`, so a worker that died mid-run cannot block geo sync forever.
The deadline is **renewable**, not a fixed window measured from the start: the
historical bootstrap runs many monthly chunks and would otherwise cross its own
deadline mid-run, letting a second worker legitimately claim the lease while the
first kept writing. Rows predating the column fall back to
`last_started_at + lease_minutes`, so an in-flight upgrade still recovers a dead
worker rather than treating NULL as "never expires".

Ownership is therefore proven continuously, not assumed:

* `renew_geo_sync_lease` — **heartbeat before every Google fetch**;
* `holds_geo_sync_lease` — **revalidation after the fetch, before any write**.
  The Google call is the slow part, so it is exactly where a lease lapses
  unnoticed.

Both are fenced on `(customer_id, scope, lease_token, last_status = 'running')`,
so a worker whose lease was reclaimed cannot renew its way back into ownership.
If either check fails the run raises `GeoLeaseLostError` and aborts having
written **nothing** — not the rows, not a `verified` coverage row, and not a
`failed` one either: recording a failure for a range another worker now owns
would corrupt that worker's ledger, which is exactly what the fence protects.

Fencing the terminal write alone was never enough. The geo rows and the coverage
ledger are written long before it, so a run that lost the lease could overwrite
the new owner's range and certify it, and only the final state write would be
rejected — after the damage.

If terminal state cannot be persisted at all, the run reports **failed**. A run
with no durable state has no evidence it happened: its checkpoint did not move
and its freshness was not published, so reporting success would be a claim
nothing can back up.

### `success` is a claim about the RANGE, not about the run

The chunk loop's status comes from its own counters, so a run whose chunks all
happened to work would report `success` even when the final ledger said the
window was still incomplete — or could not be read at all. Withholding the
checkpoint was not enough: `success` was still persisted, still returned, and
still finished the scheduler's sync batch green, so every freshness surface
showed a healthy dataset over a range nothing certified.

Final `success` now requires the re-read coverage to be **readable AND
complete**. Otherwise the run is downgraded, persists the downgraded status, and
advances neither checkpoint nor freshness:

| Final ledger | Status | `reason` |
|---|---|---|
| readable + complete | `success` | — |
| readable, incomplete | `partial` | `final_coverage_incomplete` |
| unreadable | `failed` | `final_coverage_unreadable` |
| lease lost mid-run | `failed` | `lease_ownership_lost` |
| terminal state unpersisted | `failed` | `terminal_state_not_persisted` |

Anything other than `success` fails the scheduler's sync batch, and the
historical bootstrap keeps exiting non-zero.

---

## 5. Dataset keys and freshness

`services/dataset_keys.py` is the ONE registry of `(source, dataset)` machine
keys. Writers and the freshness configuration both import from it, because
spelling a key in two places is what lets them disagree.

| Dataset | Key |
|---|---|
| Canonical campaign spend | `(google_ads, canonical_spend)` |
| Canonical geo spend | `(google_ads, canonical_geo)` |

**Corrected:** `canonical_spend`'s freshness config expected `google_ads_api`
while its writer has always stamped `google_ads`, so the ROAS denominator had no
working freshness signal (PR-ADS-153A §1.7). The config moved to what the writer
actually stamps — renaming the writer would orphan the `sync_state` rows
production has already accumulated.

**Removed (phantom — no table, no writer, could only ever report "never run"):**

| Dataset | Why |
|---|---|
| `ngrams` | Computed on demand from `search_terms`. No table, no writer, no sync batch. The N-Gram page's real dependency is `search_terms`, which has its own entry. |
| `historical_intelligence` | Named a table `db/schema.py` never creates. |
| `mailchimp_attribution` | Computed on demand by `/api/mailchimp/audit` from `mailchimp_campaign_reports`, which has its own entry. |

**Connected (real table + real writer, but nothing stamped their key):**

| Dataset | Fix |
|---|---|
| `waste_terms` | `scheduler/weekly.py` and `scheduler/monthly.py` now open an `(analysis, waste_terms)` sync batch around `write_waste_terms`. |
| `gclid_coverage_snapshots` | Same, under `(gclid, coverage_snapshots)`. |

Aliasing a phantom onto another dataset's batch was rejected: two configs sharing
one `(source, dataset)` pair collide on the key `/api/datasets/freshness` reports
under, so one would silently shadow the other. A guard test now forbids it.

`api/server.py`'s `_KNOWN_DATASETS` was a **fourth** hand-maintained copy of the
registry and had already drifted; it is now derived from
`DATASET_FRESHNESS_CONFIG`.

---

## 6. The residual bucket

One residual per country view, carrying **two independent facts**:

| Side | Source | Meaning |
|---|---|---|
| Spend | Google Ads | The campaign↔geo shortfall `geographic_view` does not assign to any country. Governed by the unchanged PR-ADS-131 eligibility rules. |
| Revenue | HubSpot | Closed-won deals whose CRM country could not be identified. |

* Canonical key: `unknown`
* Label: `Unknown / Unattributed country`

Before this PR the spend residual was appended as its own extra row asserting
`customers: 0, won_revenue: 0.0` while unidentifiable revenue was being
discarded — so the view could claim "no revenue here" over revenue it had thrown
away. Merging them means the reconciliation invariant holds on both sides:

```
Sum(known country rows) + eligible residual = the canonical scope total
```

The residual is surfaced with its deal count, revenue, reason/status and
coverage. It is **never** spread across real countries, never scored as a market
(no ROAS, no verdict, no top campaign) and never given a drawer of its own on the
dashboard — but it **is** drillable by name, which is what makes its totals
auditable rather than merely disclosed.

A drilldown request for a label that is neither a supported country nor the
residual is **refused**, not answered with the residual's contents: every
unidentifiable label resolves to the same residual key, so answering would return
one unknown country's deals under another's name.

---

## 7. The geo readiness gate

One predicate, `google_ads_geo_sync_service.country_geo_ready(status)`. Three
consumers previously answered the same question with their own code, and the
mart's page-difference audit used a stricter bar (`== "verified"`) than the pages
it audits.

### Mandatory inputs — matching totals are not enough

**Every one of these must hold before either accepted state is reachable:**

| Input | Why it is mandatory |
|---|---|
| canonical campaign spend readable | it is the reconciliation baseline |
| canonical campaign coverage complete | an unproven baseline cannot certify anything |
| FX coverage complete | required before any USD geo figure is safe |
| canonical geo rows readable | there is nothing to reconcile otherwise. This is a statement about the QUERY, not about the rows — see below |
| durable geo coverage readable **and** complete, with no failed chunks | the only thing that separates "fetched and genuinely zero" from "never fetched" |

Two unproven numbers can agree. Before this was enforced, a perfectly matching
pair of totals became `verified` even when campaign coverage was incomplete, FX
was incomplete, or geo had never been fetched at all — so agreement was being
read as evidence about inputs nobody had established.

The geo coverage ledger is therefore a **blocking input, not side evidence**. A
gate that reads it without requiring it discards its entire purpose at exactly
the moment that purpose matters.

### Unavailable is not zero — and a proven zero is not unavailable

Three different things used to collapse into one:

| Query | Ledger | `geo_total` | Meaning |
|---|---|---|---|
| failed | anything | `None` | we could not look |
| succeeded, empty | incomplete / unreadable / has failed chunks | `None` | we do not know whether anyone ever looked |
| succeeded, empty | complete, no failed chunks | **`0.0`** | we looked, and the geography genuinely carried no spend |
| succeeded, rows | complete | the sum | ordinary measurement |

An earlier revision set `geo_total = None` for **every** empty response,
classified it `no_geo_data`, and passed `geo_readable = available AND has_rows`.
That made a coverage-verified empty range indistinguishable from a database
outage — which contradicts the ledger's entire purpose, since telling "never
fetched" from "fetched and genuinely zero" is the one question it exists to
answer. An empty table cannot answer that question about itself; only the ledger
can, and the range is never judged from the table's emptiness alone.

A proven zero is a real measurement, so the comparison actually happens:

* zero campaign spend against a proven zero geo total **reconciles** —
  `verified`;
* positive campaign spend against a proven zero geo total is a **measured
  disagreement** — `mismatch`, variance 100%, Country ROAS withheld. It is not a
  by-design residual: Google omitting *some* location-less spend is a known
  artefact, Google reporting *none* of it is not.

The response publishes `geo_verified_zero` and `geo_readable` so a reader can
tell which case they are in without re-deriving it. The same rule reaches the
country row source: when canonical geo is a proven zero, ROAS by Country stays
on the canonical source with no rows rather than falling back to the legacy geo
table — reaching for a second source would replace a proven answer with a guess
at the same question. The legacy fallback still applies when the range is
genuinely unproven.

### Every mandatory input is a required argument

`resolve_country_spend_status` has **no defaults**. Each mandatory fact — and
each evidence list — is a required keyword-only argument, so a caller that
forgets one raises `TypeError` at the call site.

An earlier revision defaulted them to `True`, which meant
`resolve_country_spend_status(reconciled=True, residual_eligible=False)` still
returned `verified` having proven nothing at all. A permissive default on a
safety precondition is not a convenience; it is the original defect with a
friendlier syntax, waiting for the first caller who forgets an argument.
Defaulting `missing_geo_dates` to empty was the same trap: it would let a caller
reach an accepted state by asserting `residual_eligible=True` without ever
having looked.

The rule applies one layer up too. The mart reads its gate inputs fail-closed
(`is True`, an explicit membership test), because `.get(key, True)` on a
`source_health` field that was never published asserts the fact rather than
admitting it is unknown.

### Truth table

| `country_spend_status` | Condition | Country ROAS |
|---|---|---|
| `verified` | **all mandatory inputs hold** AND geo reconciles with canonical campaign spend within `SPEND_VARIANCE_TOLERANCE` | **Shown** |
| `reconciled_with_residual` | **all mandatory inputs hold** AND the PR-ADS-131 safe-residual predicate passes (no missing geo dates, no campaigns without geo) | **Shown**, with an explicit residual bucket |
| `mismatch` | inputs hold, but totals differ for a reason that is not the by-design residual | **Withheld** (`null`, never `$0`) |
| `unavailable` | a mandatory input is unreadable or incomplete, or reconciliation is not measurable | **Withheld** |

`reconciled` is tri-state: `True`, `False`, or `None`. `None` is **not** `False`
— an unmeasured reconciliation is `unavailable`, never a `mismatch`, because
reporting a mismatch would assert a comparison nobody performed. For the same
reason an **unproven input** is `unavailable` rather than a mismatch: "we never
established this" and "we compared and they disagreed" are different — and
differently alarming — statements.

Blocked responses carry machine `gap_codes` naming which input failed:
`campaign_spend_unreadable`, `campaign_coverage_incomplete`,
`fx_coverage_incomplete`, `geo_rows_unreadable`,
`geo_coverage_ledger_unreadable`, `geo_coverage_incomplete`,
`geo_coverage_has_failed_chunks`, plus the reconciliation reasons below.

`geo_ready`, `country_roas_unblockable`, `country_roas_available`,
`country_decision_ready` and `country_truth.reconciliation_status` are ONE
verdict published under several names — all derived from this predicate over
this status, so they cannot contradict one another.

### Safe residual eligibility (PR-ADS-131 — unchanged)

All of these must hold:

* campaign-spend coverage complete
* FX coverage complete
* geo rows exist
* **no** missing geo dates
* **no** campaigns that spent with no geo rows
* the shortfall is positive and its reason is
  `geo_report_does_not_reconcile_by_design`

### Stable gap reasons

Reconciliation outcomes:
`missing_geo_dates` · `campaign_spend_without_geo` ·
`geo_report_does_not_reconcile_by_design` · `totals_differ`

Mandatory-input failures (every one of these yields `unavailable`, never
`mismatch` — nobody performed a comparison, so nothing disagreed):
`campaign_spend_unreadable` · `campaign_coverage_incomplete` ·
`fx_coverage_incomplete` · `geo_rows_unreadable` ·
`geo_coverage_ledger_unreadable` · `geo_coverage_incomplete` ·
`geo_coverage_has_failed_chunks`

Because one status now stands for several very different causes, each code
carries its own operator sentence in `GEO_GAP_MESSAGES`, and
`describe_geo_gap(codes)` picks the one to repair **first**. FX ranks last among
the blocking gaps: it is the only one that still leaves native-currency spend
usable on the page, so it is never the headline while the spend baseline itself
cannot be read. Dashboard Countries publishes that sentence as the `geo_roas`
unavailability reason, so "re-run the geo sync" is only ever shown to the
operator who actually needs to.

### Explicitly unchanged

`SPEND_VARIANCE_TOLERANCE` (0.02) · business-window definitions · FX doctrine ·
revenue scope definitions · won-deal doctrine · residual eligibility rules.

The gate was never the defect. Loosening it would "fix" a blocked page by
lowering the bar rather than by giving geo an owner.

---

## 8. Disclosure (`country_truth`)

Every country response carries one shared disclosure block, built by
`build_country_truth_disclosure` so Dashboard Countries and ROAS by Country
disclose the same facts in the same shape:

* `revenue_source`, `revenue_scope`, `spend_source`, `geo_spend_source`,
  `geo_spend_grain`, `country_identity_contract`, `estimate_grade_note`
* `window`: key, label, dates, **`start_utc` and `end_utc_exclusive`**, and
  `bounds: "inclusive_start_exclusive_end_utc"`
* `as_of`, `geo_coverage_status`, `geo_coverage_missing_ranges`,
  `geo_failed_ranges`, `campaign_spend_coverage_status`, `fx_status`
* `reconciliation_status`, `reconciliation_tolerance`, `geo_ready`,
  `geo_accepted_states`, `gap_codes`
* `residual_accepted`, `residual_label`, `residual_key`,
  `residual_spend_native`, `residual_spend_usd`, `residual_spend_pct`
* `revenue_available`, `revenue_unavailable_reason`, `revenue_violation_codes`
* `legacy_fallback_used: false`

Exact UTC bounds are published, not only calendar dates: dates alone leave the
day-boundary convention implicit, which is exactly where timezone-dependent
population differences hide.

When geo coverage or reconciliation is unsafe the response returns
`available: false` with a stable reason and gap codes, affected metrics as
`null` (never zero), and missing-range evidence. There is **no** fallback to
Windsor, legacy geo calculations, local JSON or ad-hoc queries, and an empty
dataset is never rendered as "no country activity".

---

## 9. Production verification

Run after merge and Render deployment. Record only aggregate, non-sensitive
proof — no production deal IDs, contact information, tokens or raw GCLIDs.

**The order matters.** A seven-day incremental run cannot prove any of the
business windows on a fresh coverage ledger, so the historical bootstrap comes
first.

1. **Confirm Render is running the exact merge SHA.**
2. **Run the historical geo bootstrap** —
   `python -m scripts.backfill_canonical_geo --json`. It is resumable: completed
   chunks are skipped, so re-running after a partial result continues from the
   missing or failed work only. It exits non-zero unless the ledger itself
   proves the requested range covered.
3. **Prove the daily incremental step** — `python -m scheduler.incremental_sync`,
   then confirm the `google_ads/canonical_geo` dataset reports success.
4. **Query the durable ledgers** for run state, checkpoint, and any chunk not
   recorded `verified`.
5. **Inspect reconciliation for every supported business window** —
   `current_quarter`, `last_quarter`, `last_6_months`, `ytd`, `all_time`. These
   are the only keys `analysis.business_windows.WINDOW_KEYS` accepts; any other
   value is a 400.
6. **Confirm the missing-date query is empty** for a freshly covered window.
7. **Compare the four country surfaces** for the same window and revenue scope:
   same canonical country key set, same known-country revenue, same residual,
   same availability verdict.
8. **Confirm** `legacy_fallback_used: false`; that invalid countries are not
   presented as valid ISO codes; that blank-country revenue is present in the
   residual; and that known countries plus residual reconcile.
9. **Confirm no external mutation occurred.**

The exact commands, one per block, are in the pull request body.

---

## 10. Rollback

**Code deployment rollback only** — revert the merge commit.

Do **not** truncate or drop `google_ads_geo_coverage`,
`google_ads_geo_sync_state` or `google_ads_geo_daily_spend`. Retain geo rows,
coverage state, failure history, checkpoints and reconciliation evidence. Both
new tables are additive, so a revert simply stops writing them; nothing needs
data work, and the evidence stays available for diagnosis.

---

## 11. What this PR deliberately does not do

* No legacy table or route deletion — that is **PR-ADS-153G**, and it must not
  start before PR-ADS-153F production verification passes.
* No OCT or offline conversion upload.
* No Google Ads campaign, bid, budget, keyword or negative-keyword mutation.
* No tolerance change, no new attribution model.
* No UI redesign unrelated to geography truth.

Six-month read-only governance remains **active**. Phase 2 / OCT remains
**blocked**.
