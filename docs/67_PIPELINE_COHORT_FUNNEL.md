# 67 — Pipeline counts by create date: the shared acquisition-cohort funnel

PR-ADS-182A. Builds on `docs/63` (the one funnel block, 180A), `docs/64` (the
acquisition-cohort funnel on Overview, Campaigns, Countries, 180B) and
`docs/65` (the Pipeline undated-reach bound, 180C), which it partly retires.

## 1. Owner decision, and why

The Pipeline page's funnel counted lifecycle stage **entries** in the window
("Entered stage in window", `lifecycle_stage_events`), bounded since 180C by
the undated reach. In production about 9,000 contacts reached Lead with no
Lead-entry date, so most stages read "at least N" permanently (`docs/64`
§4, `docs/65` §1.2).

**The owner chose: the Pipeline funnel counts by contact CREATE DATE** — the
acquisition cohort (`acquisition_cohort_outcomes`, windowed on
`contact_created_at`): contacts created in the window, and how many have
reached each stage so far, as of the canonical contact-funnel watermark. It
needs no stage-entry date, so a stage-proven contact without a timestamp is
counted exactly. The page says: "Contacts created in these days, and how far
they have got so far."

## 2. What changed

### 2.1 One funnel, one producer, one block

* **Backend.** `pipeline_service._funnel(cohort, scope, acquisition_group)`
  republishes the mart deal view's `cohort` block — the SAME build the SQL
  card reads in the same request — through the one block producer the
  Overview uses, `dashboard_overview_service.acquisition_funnel_block`
  (`_acquisition_funnel_block` generalised: `source_group=`, and
  `unavailable_reason=` for a filter the cohort cannot honour; both can only
  withhold, neither re-decides the verdict). No database read of its own, no
  snapshot of its own, no stage-entry build. The block adds Pipeline-only
  keys: `label` ("Acquisition cohort"), `description`, `lead_filters`
  (`scope`, `acquisition_group`, labels, `source_group`, `honoured`,
  `unavailable_reason`), `truth_scope`.
* **Page.** `pipelineFunnelModel(f)` is `acquisitionFunnelModel(f, {shares:
  false, …})` drawn by `funnelBlockHtml` — the block Overview, Campaigns,
  Countries and Channels draw. No rate (the cohort publishes none and the page
  divides nothing). A contact with no created date makes every column "at
  least N" with the reason (`acquisitionCohortBound`, the 180B rule).
* **Metadata.** The block carries the cohort's own `metric_family` /
  `window_basis`, printed on its face; `metric_truth` gains
  `lifecycle_acquisition_funnel` (scope `all_source_acquisition_cohort`
  unfiltered, `lead_filtered_acquisition_cohort:<scope>:<group>` otherwise).
* **The contact list is another count, and says so.** The list
  (`/api/crm-funnel/contacts`) lists contacts that ENTERED a stage in the
  window. The cohort columns are no longer buttons onto it (a click on
  "Reached SQL 175" opening a list of another population would relabel one
  family as the other). Under the funnel, a stage picker ("Entered Lead" …
  "Moved to Customer stage in HubSpot", Disqualified / Other) opens it, under
  the note "Contact list — a different count from the funnel above: contacts
  that ENTERED a stage in this window, each on its own HubSpot stage-entry
  date." The stage-move help is the Customer button's title.

### 2.2 The lead filters on the cohort basis

| Filter | On the cohort funnel |
| --- | --- |
| Scope All sources, Source all | the cohort's `all_sources` slot (= Overview's block) |
| Source *g* (Google Ads, Other Paid, Organic, Offline, Unclassified) | **honoured**: `breakdown.by_source_group[g]` |
| Scope Google Ads-source | **honoured**: the Google Ads source group (`_contact_scopes`: that scope IS the group) |
| Google Ads-source + Source other than Google Ads | **unavailable**, `lead_filters_select_no_contact` (a contact has one group) |
| Scope Campaign- or Keyword-attributable | **not honoured**: unavailable, `lead_scope_not_on_cohort_basis` — "the acquisition cohort is not split by campaign or keyword attribution …; the contact list below still applies this Scope". Never the unfiltered cohort under the filter's name. |

`by_source_group` is new in the ONE cohort producer
(`marketing_outcome_cohort_service.build_cohort`): an all-source partition by
`canonical_crm_funnel_service.derive_acquisition_group` — the Source filter's
own rule (Original Source **and** its drill-down: Offline Sources / SalesNash
is Other Paid). Every group is seeded with a zero slot, so a group with no
contact is a counted 0 from the producer, never a reader's inference.
`reconcile_cohort` holds Σ groups = all sources for every field (a failure
withholds the cohort); the mart publishes it inside `cohort.breakdown`
(published only; `_jsonable_cohort` keeps it — counts only, no contact id).

The Google Ads-source funnel is **not** the SQL card's population: the card
is the Google Ads acquisition cohort (Paid Search minus approved
not-Google-Ads labels, `contact_bucket`); the scope is Paid Search by original
source. The page says the card "is the Google Ads acquisition cohort and is
not narrowed by the lead filters".

The no-created-date count is table-wide (all sources); under a Source filter
it only widens "at least" (as 180B §7.4 for Countries).

### 2.3 Freshness and withholding

The funnel publishes only under the cohort's one verdict (the real
`analysis.sql_coverage_freshness.assess`: `source_fresh` / `source_stale`;
bucket reconciliation). Withheld means absent: `totals` is None; the page
prints the word and the reason in every column and no number;
`audit_marketing_outcome_cohorts.mart_withheld_exposures` scans the Pipeline
and Dashboard → Deals `funnel` too. Unreadable is `unavailable`
(`canonical_funnel_unreadable`), never 0; a published verdict whose selected
slot is missing is `acquisition_funnel_counts_missing`, never a partial set
of columns.

## 3. Stage-event figures gone from Pipeline

From `/api/pipeline` → `funnel`: `events` (the five stage-entry counts and
their `metric_family` / `event_date_property`), `conversions` (cohort-safe
stage-entry rates), `coverage`, `undated_reach`, per-stage
`undated_contacts_could_belong` / `count_is_bound` /
`undated_contacts_all_sources`, `reconciliation`, `sync`, `available`,
`scope*`, `acquisition_group*`, `campaign_identity_available`. From the page:
"Entered Lead / MQL / SQL / Opportunity" counts, "Moved to Customer stage in
HubSpot" count, the stage-entry rate chips, the "at least N … no entry date"
bounds, the CRM truth banner (`leadsTruthStateHtml`). The stage-entry family
stays on **Overview → Lifecycle Activity** and in the contact list.

Dashboard → Deals republishes `build_pipeline`: its payload's `funnel`
changes the same way (its tab draws no funnel).

## 4. What was deleted, and what was kept

Deleted (Pipeline was the last consumer):

* `pipeline_service._funnel_coverage`, `REASON_FUNNEL_UNREADABLE`, the
  stage-entry `_funnel`;
* `canonical_crm_funnel_service.undated_reach_for_population`;
* `outside_window_rows` in `undated_stage_reach` and in
  `crm_funnel_repository.fetch_undated_stage_reach` (its `__outside` FILTER
  columns) — its one reader was that narrowing;
* `static/app.js`: the stage-entry `pipelineFunnelModel`,
  `leadsTruthStateHtml`, the `undated_contacts_all_sources` branch of
  `stageEntryCountBounds`, the `lifecycle_funnel_unreadable` label;
* the SQL doctrine registry's `undated_reach_for_population` binding.

Kept (Overview → Lifecycle Activity still uses them): `undated_stage_reach`,
`stage_count_is_bound`, `one_read_snapshot` (two sites, both Overview's),
`fetch_undated_stage_reach` (its counts), `any_stage_date_in_window` (both
readers; the reach rules out by it), `stageEntryCountBounds` /
`stageEntryLowerBounds`, `audit_lifecycle_sql_coverage._SHARED_WINDOW_HELPERS`.
Also kept: `funnelBlockHtml`'s `selectable` option (no caller now; a future
cohort contact list would use it) and `build_populations`'
`stage_reached_without_entry_date_by_scope` (178C; the canonical funnel's
own coverage, no page reads it now) — proposals in §8.

## 5. Registries and audits

* **Cross-page parity** (`cross_page_parity_service.METRIC_IDENTITIES`): five
  identities `all_source_acquisition_cohort_<column>` — Overview
  `lifecycle_acquisition_funnel.totals.<col>` against Pipeline
  `funnel.totals.<col>`, provenance under `lifecycle_acquisition_funnel`,
  coverage proof the mart lead population. A filtered Pipeline names another
  scope and fails the contract check rather than being compared.
* **Cohort audit** (`audit_marketing_outcome_cohorts`):
  `acquisition_funnel_problems(payload, key)` checks Overview's and
  Pipeline's block (and Dashboard → Deals') against the slot it names
  (`all_sources` or `by_source_group[g]`), and that an unhonoured filter
  publishes nothing; `mart_withheld_exposures` and
  `created_at_disclosure_problems` scan the `funnel` block;
  `cohort_stage_problems` checks the source-group partition sums to all
  sources. `audit_mart_window` runs it on `pipeline` and `dashboard/deals`.
* **SQL doctrine registry**: the Pipeline page entry describes the cohort
  funnel; `_cohort_selection`, `pipelineFunnelModel`,
  `pipelineFunnelPopulationText` bound.
* **Reader-cutover inventory**: unchanged (the funnel reads no closed-won
  deal).
* **CI**: the new PG module is in the PostgreSQL step and its did-run list.

## 6. Tests

### 6.1 New

`tests/test_pr_ads_182a_pipeline_cohort_funnel.py` (17) and
`tests/test_pr_ads_182a_pipeline_cohort_funnel_pg_integration.py` (4). Real
producers; the contact repository replaced at its fetch boundary; the real
freshness `assess`; the PG module through the real reads, the real mart, the
cohort audit's own SQL and the real parity audit. **All 21 fail on
`6de7818`** (log: `scratchpad/pipeline_review/failbefore_6de7818.log`).

### 6.2 Ported (each keeps its truth content on the cohort basis)

* **166A** — the harness's substituted input is the mart deal view's cohort
  block (the real contract over fake rows) instead of the stage-event slice;
  the funnel cell read is `data-funnel-slot="customer"`; node gets its
  script from a file (four real payloads exceed one argv string). 02
  (renamed `…_customer_stage_contacts_and_customers_won_are_two_figures`),
  03, 05, 07, 10: the Customer column ("Reached Customer stage", contacts)
  and Customers won (companies) stay two figures, never derived from each
  other; filters narrow the funnel, never customers won. 23 (DB): the real
  stage-entry funnel still counts 219 moves; the real cohort over the same
  table counts the 37 created in the quarter — the owner's basis.
* **178B one page** — `_payload`'s funnel is the real producer's over the
  response's cohort; 04 (renamed `…_is_the_acquisition_cohort_and_the_list_keeps_the_customer_stage_help`),
  05 (the list opens from the picker). **178B PG** 01, 02, 09.
* **178B-F1 round 1** — 05_b (the funnel's reasons in words), 06 (no raw code
  in any funnel state), 07, 08 (a lower bound is "at least N" — the
  created-date gap), 09_a / 09_b (renamed `…_exact_evidence_draws_no_rate_either`):
  no rate chip at all.
* **178C** test_09 (renamed `…_only_where_the_cohort_has_a_gap`); **178C PG**
  test_05 (renamed `…_counts_the_undated_stage_contacts_exactly_on_the_cohort_basis`).
* **180A** 01_a, 04_b, 04_c, 05_d, 07_h (a withheld cohort draws neither rate
  nor count).
* **180C** 01_a (renamed: the seven undated contacts are counted exactly on
  Pipeline while Activity bounds them), 01_b (renamed: Pipeline reads
  identically to Overview's acquisition block), 01_c (Activity), 01_e (one
  stage-entry rule, Activity's; Pipeline reads none), 01_f (Activity half
  kept). **180C PG** 01, 02, 05 (renamed), 06 (Overview's two sites).
* **153C** five structural cases, **141**, **177A-F1** m2_a,
  **empty-state** leads case, **154C** (fixture carries the acquisition
  blocks; 24 → 29 identities).

### 6.3 Removed, with the reason

| Test | Why |
| --- | --- |
| 180C 01_d | a lead filter never reads the all-source undated figure as its own — the stage-entry narrowing is gone; the cohort analogue is 182A 02_a / 02_c |
| 180C 01_g | unit of `undated_reach_for_population` — deleted (Pipeline its only consumer) |
| 180C 08 | the narrowed reach's population label — same function |
| 180C PG 03_a, 03_b | the narrowing on a real table — same |
| 180C PG 04_a | Pipeline's two-read tear — Pipeline pairs no reads now (one cohort read in the mart); Activity's tear stays guarded by 04_b / 04_c |
| assertion: 178B-F1 06 property-name title | the cohort reads no stage-entry property |
| assertions: 153C `leadsTruthStateHtml` banner | the function is deleted; the cohort's state is its verdict line (ported) |
| assertions: 166A stage-move help in the funnel cell | the help is the list picker's Customer title (182A 05_b, 178B 04) |
| assertions: 180C PG 02 / 05 `outside_window_rows` | the field is deleted; counts and rows' edges kept |

## 7. Evidence

### 7.1 Named mutations

Each applied alone to a copy of the tree; the 182A modules, 154C and 180B run
(`scratchpad/pipeline_review/mutations/`).

| | Mutation | Failed |
| --- | --- | --- |
| M1 | Pipeline's block relabelled as the stage-event family | 01_a, 01_b, 03_a, 05_a, 06_a, PG 01, PG 02, PG 03 |
| M1b | Pipeline reads stage events again (`pipeline_service`, the funnel service and the repository reverted to `6de7818`) | all 17 unit, all 4 PG, 154C f1_9, f2_7 |
| M2 | the shared adapter draws totals under a withheld verdict | 03_d, 180B 03_f |
| M2b | the cohort audit does not scan the Pipeline `funnel` block | 03_a |
| M3 | Pipeline states 0 contacts without a created date (bound dropped) | 01_b, 03_a, 03_b, 04_a, PG 04 |
| M4 | Campaign-/Keyword-attributable silently shows the unfiltered cohort | 02_c, PG 02 |
| M4b | the Source filter is silently dropped | 02_a, 02_b, 02_d, 03_c, 06_a, PG 02 |
| M5 | parity: the Pipeline consumer dropped from the five identities | 01_c |
| M5b | the unfiltered funnel reads the Google Ads part (parity broken) | 01_a, 01_b, 02_d, 03_c, 04_a, PG 01, PG 04 |
| M6 | `reconcile_cohort` ignores the source-group partition | 06_b |
| M7 | the partition classifies without the drill-down | 02_a |

None survives.

### 7.2 Screenshots

Real `build_pipeline` of each tree over the same ~22k generated contacts and
ledger (`scratchpad/pipeline_review/tools/gen_payloads.py`), the real SPA in
Chromium (`tools/shoot.js`), desktop 1440 and phone 390:
`scratchpad/pipeline_review/before/` (6de7818, stage entries) and `after/`
(published, withheld, bound, source-organic, scope-keyword). Page width equals
the viewport in every shot; no clipped figure (`report.json`).

## 8. Open, and proposals (not built)

* **A cohort contact list** (contacts created in the window that reached a
  stage), so the funnel columns can open their own population again.
* **Campaign- / keyword-attributable on the cohort** — needs the campaign
  resolver per contact inside the cohort build.
* **Retire** `funnelBlockHtml`'s `selectable` option and
  `stage_reached_without_entry_date_by_scope` if no consumer appears.
* The production volume of the extra `by_source_group` slot is five small
  dicts per cohort build; not measured on production.
