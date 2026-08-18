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

* A chunk is marked `verified` **only after its rows are durably written**. A
  successful read that did not persist raises `GeoPersistenceError` and is
  recorded `failed`.
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

The daily lookback re-fetches the last 7 days on every run because Google Ads
restates recent spend — so a daily chunk is never "already verified", while
historical months are.

### The run lease

Render runs more than one instance and the manual recovery trigger can fire at
any moment, so the overlap guard is a durable conditional `UPDATE`, not a
process-local flag. It is tri-state on purpose:

| Result | Behaviour |
|---|---|
| `acquired` | This worker owns the lease and proceeds. |
| `held` | Another run owns it. This run refuses to start — the range stays uncovered, the gate keeps Country ROAS blocked, and the next run picks it up. |
| `unavailable` | The lease store is unreachable. The run **proceeds without a lease**. |

The third state is deliberately not folded into `held`: treating an unreachable
lease store as "someone else is running" would turn a transient database blip
into a silently skipped sync, and invisible geo staleness is the defect this PR
exists to remove. Concurrent runs are idempotent upserts over the same rows and
the ledger refuses to demote a verified chunk, so a duplicate fetch is a bounded
cost; unbounded staleness is not. A stale lease expires after 120 minutes so a
worker that died mid-run cannot block geo sync forever.

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

### Truth table

| `country_spend_status` | Condition | Country ROAS |
|---|---|---|
| `verified` | Geo reconciles with canonical campaign spend within `SPEND_VARIANCE_TOLERANCE` | **Shown** |
| `reconciled_with_residual` | The PR-ADS-131 safe-residual predicate passes | **Shown**, with an explicit residual bucket |
| `mismatch` | Totals differ for any other reason | **Withheld** (`null`, never `$0`) |
| `unavailable` | Geo or campaign spend could not be read; reconciliation not measurable | **Withheld** |

`reconciled` is tri-state: `True`, `False`, or `None`. `None` is **not** `False`
— an unmeasured reconciliation is `unavailable`, never a `mismatch`, because
reporting a mismatch would assert a comparison nobody performed.

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

`missing_geo_dates` · `campaign_spend_without_geo` ·
`geo_report_does_not_reconcile_by_design` · `totals_differ`

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

1. Confirm Render is running the exact merge SHA.
2. Allow the scheduled run, or trigger one canonical incremental geo run.
3. Confirm the run completed with no missing expected ranges, no failed chunks,
   no database write failures, durable coverage complete and healthy freshness.
4. Inspect the geo audit/reconciliation response for every supported business
   window.
5. For a freshly covered window, confirm the missing-date query is empty.
6. Compare Dashboard Countries, ROAS by Country, the country drilldown and the
   mart for the same window and revenue scope: same canonical country key set,
   same known-country revenue, same residual, same availability verdict.
7. Confirm `legacy_fallback_used: false`; that invalid countries are not
   presented as valid ISO codes; that blank-country revenue is present in the
   residual; that known countries plus residual reconcile; and that no external
   mutation occurred.

The exact commands are listed in the pull request body.

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
