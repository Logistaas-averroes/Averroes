# 51 — Campaign Evidence drawer sections: bounded memory (PR-ADS-172A)

## 1. What failed

The daily in-app certification (PR-ADS-161J, capped by PR-ADS-169A) runs six
audits, each in a child process under a resident budget with a 320 MB
address-space backstop. Five hold. The sixth,
`scripts.audit_campaign_evidence_certification`, did not:

| day | recorded |
| --- | --- |
| 2026-11-21 | killed by SIGKILL |
| 2026-11-22 | crashed — "reached its memory backstop (320 MB of address space)", `peak_rss_mb` 287, 114 s |

So the platform read NOT CERTIFIED for a memory reason on both days. The cap
did what it is for. **It is unchanged**: `audit_address_space_mb: 320`,
`safety_margin_mb: 64`, and the audit is still in `AUDITS`.

For each of six Evidence Windows that audit calls `build_campaign_evidence`,
then `build_campaign_keyword_preview` and `build_campaign_flagged_preview` for
one campaign. Those are the builders `/api/campaigns` and
`/api/campaign-detail` run inside the web server.

## 2. What was measured, and on what

**Everything below is measured on a synthetic PostgreSQL dataset, not on
production.** Its row counts are the live tables' on 2026-11-22
(`search_terms` 228,043; `keyword_daily_facts` 98,152; `leads` 27,823;
`hubspot_contact_funnel` 21,866; `contact_source_classification` 21,889;
campaign daily spend 4,810). Its content is invented: 27 campaigns, 62,706
distinct queries with a long tail over 180 source dates (102,990 term ×
campaign units), 9,563 fact rows flagged, 9,596 `waste_terms` annotations.
Production's absolute figures will differ. That this dataset, like production,
stalls just under the cap (294 MB here, 287 MB there) validates nothing: any
build that needs more than the cap stops just under it.

**Production observation (read-only query, 2026-11-22).** `search_terms` held
66,455 distinct terms and 101,852 term × campaign units — close to this
dataset's 62,706 and 102,990 — but **zero** rows with `is_flagged_waste = TRUE`,
and `waste_terms` was **empty**. This dataset flags 9,563 rows and carries 9,596
annotations, so its "after" figures for the flagged section are for a flag
state production does not have today. See §4.4.

Peak resident growth of ONE call in a warm process (`VmHWM` after − `VmRSS`
before), MB:

| builder | 7d | 14d | 30d | 60d | 180d | all_time |
| --- | --- | --- | --- | --- | --- | --- |
| `build_campaign_evidence` (unchanged) | 24 | 24 | 24 | 25 | 26 | 34 |
| `build_campaign_keyword_preview` — before | 23 | 24 | 27 | 39 | 117 | 118 |
| `build_campaign_keyword_preview` — after | 22 | 23 | 26 | 29 | 34 | 39 |
| `build_campaign_flagged_preview` — before | 52 | 103 | 207 | 369 | 836 | 836 |
| `build_campaign_flagged_preview` — after | 18 | 18 | 29 | 53 | 113 | 114 |

The whole audit, six windows in one process:

| | peak resident | peak address space | wall |
| --- | --- | --- | --- |
| before, uncapped | 902 MB | — | 52 s |
| before, under the real 320 MB cap | 294 MB | 320 MB (the cap) | — |
| after, under the real 320 MB cap | 175 MB | 208 MB | 19 s |

**What the pre-fix audit REPORTED under the cap is not reproducible, and that
was a defect of its own.** Every reader and both section adapters caught
`Exception` — which includes `MemoryError` — and answered "unavailable", and
the gate passed any unavailable section that carried a reason. So the outcome
depended on which allocation happened to fail: one run here exited 0 with
`certified: true` (sections recorded as passed-unavailable); an independent
run of the same code exited 1 with a false VIOLATION at 60d and
passed-unavailable sections at 180d and all_time. A swallowed `MemoryError`
could yield a false violation or a false pass. Only the certification's guard
band (PR-ADS-169A) stood between that report and the verdict. Fixed in §4.5.

**Top allocators before** (`tracemalloc`, flagged preview, `all_time`, 755 MB
traced):

| MB | where |
| --- | --- |
| 269 | `db/search_term_repository.py` `_rows_as_dicts` — every grouped `search_terms` row, then every (term, campaign, day) cost row, as a dict |
| ≈ 315 | `services/search_term_evidence_service.py` `_build_population` — one unit dict and ten sets per term × campaign |
| 34 | `db/search_term_repository.py` — `sorted(...)` of each row's array columns |

Stage by stage (resident): `fetch_search_term_aggregates` +255 MB (102,990
rows); units +250 MB; `fetch_search_term_daily_costs` +264 MB (227,952 rows).
For the keyword preview: `fetch_keyword_daily_costs` +116 MB (98,152 rows; 70 MB
of it `db/keyword_repository.py` `_rows_as_dicts`).

**Accumulation across windows: none measured.** After the fix the six-window
run peaks at 175 MB and the `all_time` window alone at 175 MB. Nothing was
changed in the audit's loop.

## 3. Why the sections were hungry

Neither section is a query for one campaign. Each composes its page's builder
with a campaign filter, and that builder loads the account:

* **Flagged.** `build_flagged_search_terms` built every term × campaign unit in
  the window — flagged or not — then kept the flagged rows of one campaign. A
  preview of ten rows cost the whole fact table, twice (grouped rows, then
  per-day cost rows).
* **Keyword.** The units are aggregated in SQL (1,800 rows here), but per-date
  FX needs each criterion's daily cost, and that arrived as one dict per
  (criterion, day), each carrying two provenance lists nothing reads.

## 4. The fix

### 4.1 Flagged: load only the units that can be flagged

`build_flagged_search_terms(population="flagged_candidates")`, which the drawer
section now passes. Default `"complete"`: the Flagged page is unchanged.

A unit is one **normalized** term × one canonical campaign, flagged only by
durable evidence — a fact row with `is_flagged_waste = true`, or a
`waste_terms` annotation stored against one of its raw spellings. So
`fetch_search_term_aggregates(flagged_candidates=True)`:

1. asks the database which raw terms in the canonical scope carry either kind
   of evidence:

   ```sql
   SELECT search_term FROM search_terms WHERE <canonical scope>
   GROUP BY search_term
   HAVING BOOL_OR(is_flagged_waste IS TRUE)
       OR search_term IN (SELECT search_term FROM waste_terms)
   ```

2. adds every other raw spelling in the scope that normalizes to the same
   term. This is decided by `normalize_search_term` itself (NFKC, casefold,
   whitespace) over `SELECT DISTINCT search_term`, read through a server-side
   cursor — not by a copy of the rule written in SQL, which could disagree
   with it;
3. runs the two existing statements (grouped rows; source totals) with
   `AND search_term = ANY(%s)`, and returns the list so
   `fetch_search_term_daily_costs(search_terms=…, folded=True)` reads the same
   population.

An empty list selects nothing. An unconfigured account is unavailable, as
before. The merge in `_build_population` is the same code for both
populations.

**Why the numbers do not change.** Everything the flagged view publishes is a
function of flagged units, or of the annotation join:

* a flagged unit's terms are all loaded, in every spelling, so its summed
  clicks, cost, per-date FX, first/last seen and variants are the complete
  unit's;
* an annotation attaches only where its campaign label identifies one unit
  among **all units of the same normalized term** — those are loaded, in every
  campaign — and is not globally ambiguous (decided from spend and identity
  rows, not from units);
* `annotation_rows` counts annotations whose term is in the population: every
  such term is evidenced, so it is loaded;
* `truth_state` reads the flagged units, the annotation join and whether SQL
  attribution was available;
* `total_count`, the KPIs and the facets are computed over all flagged rows of
  the account before the page slice. **A preview limit is still never the
  total.**

One thing does depend on units that are not flagged: search-term SQL
attribution, once the CRM carries an exact user query (a contact attaches to a
unit only if exactly one unit matches, and the attributed share is counted over
all units). `leads` persists no query today (`LEADS_HAVE_SEARCH_TERM = False`),
so every unit's attribution is decided by its own mapping status. If that ever
changes, `_flagged_population` sees `population_has_text`, builds the complete
population instead, and the response carries no `population_scope`.

A narrowed response says so: `population_scope: "flagged_candidates"`. Its
`audit` block reconciles the narrowed units against source totals narrowed by
the same predicate.

### 4.2 Keyword: fold the per-day costs

`fetch_keyword_daily_costs(folded=True)` reads the same grouped statement
(minus the two unread array columns) through a server-side cursor and returns
`{(customer_id, campaign_id, ad_group_id, criterion_id): {iso_date: micros}}`.
`_build_population` always asks for it, so the Keyword Evidence page gets the
same reduction. The amounts are integers summed under the same keys; the FX
arithmetic that follows is untouched. Account scope is still applied in SQL by
`_scope(customer_ids)`.

### 4.3 Not held longer than used

`_build_population` (search terms) no longer keeps the grouped rows and the
cost rows referenced until it returns.

### 4.4 What to expect in production — conditional on the flag state

The flagged section's cost follows the number of flagged and annotated terms,
not the table. With production's state on 2026-11-22 (nothing flagged, no
annotation) the candidate list is empty: the narrowed read runs its first
grouped statement, loads no unit, and the section is **available with zero
rows and a total of 0** — a measurement that nothing is flagged, not an
unavailable section and not a failed read. On the production-scale dataset
with that flag state the section's resident growth is asserted ≤ 60 MB and the
real audit holds with both sections available in all six windows
(test_41, test_42). **This expectation holds only while that count stays near
zero.** It grows as terms are flagged or annotated: at this dataset's 13,387
candidate units the section costs ≈ 114 MB.

### 4.5 A section that was not built is not a section that held

"The audit holds in little memory" is also what an audit looks like when its
sections were never built. So, in the same change:

* **`MemoryError` is never converted.** Every broad `except Exception` in
  `db/search_term_repository.py`, `db/keyword_repository.py`,
  `services/search_term_evidence_service.py` and
  `services/keyword_evidence_service.py` is preceded by `except MemoryError:
  raise`, and so are the two in the audit's `_audit_window`. Out of memory
  ends the audit; the capped runner records it as crashed.
* **The gate reads WHY a section is unavailable.**
  `audit_campaign_evidence_certification.classify_section_reason` classifies
  every reason code a builder can emit:

  | class | reasons | result |
  | --- | --- | --- |
  | declared withholding | `google_ads_customer_not_configured`, `search_term_truth_mismatch` | passed |
  | failure | `keyword_evidence_unavailable`, `keyword_preview_failed`, `keyword_preview_context_failed`, `search_term_evidence_unavailable`, `flagged_preview_failed`, `flagged_preview_context_failed`, `campaign_identity_unresolved`, `window_not_resolved` | `unavailable_now` — exit 2 |
  | violation | `keyword_scope_account_mismatch` | violation — exit 1 |
  | anything else | — | `unavailable_now` — exit 2 |

  `campaign_identity_unresolved` is a failure HERE because the audit probes
  only a campaign whose identity the headline build resolved. A test fails if
  a builder gains a reason code that is not in this table.
* **The stored run proves it.** The audit's report carries each section's
  `available` / `reason` per window on every path. `platform_certification_runs.
  summary` used to drop the whole report; `summarize` now keeps
  `audits[].drawer_sections = {window: {section: {available, reason}}}` for an
  audit that builds sections, so the row alone shows both sections were built
  in all six windows.
* **One snapshot.** The narrowed read is four statements whose answers must
  agree; it now opens with `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ,
  READ ONLY`. Under READ COMMITTED a term flagged between the candidate
  statement and the grouped statement would have been silently absent.
* **A failed cost read is unavailable.** `_build_population` (both services)
  ignored `available: False` from the per-date cost read and published every
  unit, in an available response, as `fx_incomplete` — a right withholding
  with a false reason. The population is now unavailable
  (`search_term_evidence_unavailable` / `keyword_evidence_unavailable` on the
  drawer sections). This applies to the pages too.

## 5. Proof

`tests/test_pr_ads_172a_campaign_evidence_memory.py` (41 cases; in CI's
PostgreSQL step and did-run list):

* **Equivalence, real PostgreSQL.** On a fixture built from the cases where
  narrowing could go wrong — a unit of three spellings with one flagged, the
  same term in two campaigns, an annotation whose campaign name belongs to two
  ids, an annotation with no campaign, a flag outside the short windows, an
  annotation for a term the window lacks — the narrowed and complete responses
  are **equal as whole dictionaries** for 6 windows × 5 campaign filters × 3
  sorts. Repeated at scale (30d, two campaigns) in a child process.
* **Measured, production-scale synthetic data.** Flagged preview growth
  ≤ 170 MB (114 measured), keyword ≤ 70 MB (39 measured), `all_time`, each in
  a fresh interpreter. The real audit, started by
  `audit_platform_certification._run_capped` with the configured backstop and
  classified by `classify`, holds with resident peak ≤ 75% of a 293 MB budget
  and with both sections available in every window.
* Of the first 20 cases, 17 fail on `main` — but only **three** for the memory
  reason: test_21 ("the flagged preview added 839 MB"), test_22 ("the keyword
  preview added 123 MB") and test_24 ("reached its memory backstop (320 MB of
  address space): peak 320.0 MB is within 16 MB of it"). The other 14 fail
  because the API they call does not exist there.
* **§4.5.** A reader and a section re-raise `MemoryError`; every reason code is
  classified and an unknown one fails closed; a failed section is
  `unavailable_now`, a scope breach a violation, a declared withholding a
  pass; on a real database a failed section read leaves the audit not
  certified and out of memory ends it; the stored summary keeps the sections;
  a row committed between the narrowed read's statements is not half-visible;
  a failed cost read is unavailable. Nothing flagged — on the fixture and at
  scale — is available, zero, and the audit holds. Against the first 172A
  commit 20 of these 22 selected cases fail; the two that pass are the
  nothing-flagged section cases, whose behaviour was already right.

Every existing test passes unmodified.

## 6. Not fixed

1. **The Search Terms pages still build the complete population in the web
   server.** `/api/search-term-evidence` (Terms), `/flagged` (the page itself),
   `/patterns`, `/patterns/detail`, `/term`, `/export` and the `/api/waste`
   adapter: 839 → 702 MB of growth at `all_time` on this dataset (§4.3 only),
   211 → 170 MB at 30d. Terms and Patterns publish every unit, so narrowing
   does not apply; they need server-side pagination or aggregation of their
   own. The Flagged page could take `flagged_candidates` once its audit block's
   "every source row" claim is restated for a narrowed population — a contract
   decision, not made here.
2. **The flagged section now scales with the account's flagged and annotated
   terms**, not with the table: 13,387 candidate units here cost ≈ 110 MB,
   ≈ 30 MB of it full row dictionaries for flagged terms of other campaigns
   (built for the facets and the identity invariant). `truth_state` and
   `annotation_join` are account-wide by contract, which is why the read is not
   narrowed to the one campaign.
3. `build_campaign_evidence` (24–34 MB) is unchanged; most of it is the
   canonical contact reconciliation PR-ADS-169A already reduced.
4. `api/server.py`'s `_campaign_keyword_preview` / `_campaign_flagged_preview`
   still catch `Exception`, so inside the web server a `MemoryError` raised by
   a section becomes an unavailable section of that one response. Not changed
   here: it is the server's choice for a request, not a certification.
5. The narrowed read's snapshot covers the four statements of
   `fetch_search_term_aggregates`. The per-date cost read that follows is a
   second transaction, as it is on the complete path.
6. Production peaks are not measurable from here. The first production run
   after deploy should show `campaign_evidence` `holds` with `peak_rss_mb` well
   under its budget in `platform_certification_runs.summary`.
