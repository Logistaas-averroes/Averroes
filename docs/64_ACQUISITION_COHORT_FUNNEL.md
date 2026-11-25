# 64 — The acquisition-cohort funnel on every page

PR-ADS-180B. Builds on PR-ADS-180A (`docs/63`, the shared funnel block) and
PR-ADS-161B / 177A (`docs/44`, `docs/59`: the acquisition cohort is the one
SQL count, owner decision 24 Nov).

## 1. Why

* Production has about 9,000 contacts at MQL or later with no
  `date_entered_lead` (6,105 of 10,285 MQLs). The Overview "Lifecycle Cohort"
  was anchored on Lead entry, so after 180A-F1 every stage read "at least N"
  and every rate "—", permanently.
* The owner chose the acquisition cohort everywhere: contacts **created** in
  the window, each stage reached as of the canonical contact-funnel
  watermark. It needs no stage-entry date.
* The owner asked for the full funnel on every page. Campaigns and Countries
  carried only the cohort SQL count.

## 2. What changed

| Surface | Before (`bf9deed`) | Now |
| --- | --- | --- |
| Overview, second lifecycle panel | Lead-anchored "Lifecycle Cohort" (`lifecycle_cohort`, `dashCohortFunnelModel`) | "Acquisition Cohort", all sources (`lifecycle_acquisition_funnel`, `renderDashAcquisitionFunnel`) |
| Overview → Lifecycle Activity | stage-entry events; bound read from the window's own rows | unchanged family; bound also counts undated contacts **outside** those rows (§5); a bound baseline is "No comparison" (§4) |
| Campaigns page | KPI cards: cohort leads and SQLs | + one funnel block: leads, MQL, SQL, Opportunity, Customer stage (Google Ads cohort) |
| Countries page | KPI cards: cohort SQLs (listed markets) | + one funnel block on the cohort (Google Ads, every country, residual included) |
| Channels strip | `contacts_without_created_at` never read | every column "at least N" while it is above 0 or not stated |

### 2.1 One producer

There is still one cohort producer: `marketing_outcome_cohort_service.
build_cohort`. Its slots (`all_sources`, `google_ads`, every campaign,
unattributed, excluded and `by_group` slot) now carry `reached_mql`,
`reached_opportunity` and `reached_customer_stage` beside `sqls`, through one
new rule, `stage_reached(row, event)`:

* SQL is `sql_proof` (unchanged);
* any other stage: its direct stage-entry timestamp, its recovered one, or a
  current lifecycle stage at or beyond it (`stages_implying_event`).

`channel_funnel_service.reached` now delegates to it, so Channels and the
cohort count with **one** rule (test 01_a checks the two agree, contact for
contact). Each stage is cumulative: every contact that has reached it so far,
not a partition by furthest stage. A stage reached only by its date (the
lifecycle stage moved back) counts.

`db.crm_funnel_repository.fetch_acquisition_cohort_contacts` selects the
direct and recovered dates of MQL, Opportunity and Customer next to SQL's.
They are evidence that a stage was reached; membership is still
`created_at` alone.

`reconcile_cohort` now holds every bucket identity for every slot count
(`SLOT_COUNT_FIELDS`), and no stage may exceed the contacts acquired. A
failure withholds the cohort, as before.

### 2.2 The mart cohort contract

`revenue_attribution_service._cohort_contract_block` (every mart view, and
every Dashboard payload that republishes it) adds:

* `totals.reached_mql`, `totals.reached_opportunity`,
  `totals.reached_customer_stage` — the Google Ads slot, beside
  `totals.leads` / `totals.sqls`, under the SAME verdict (`sql_status`);
* `funnel_stages` — the columns in order, each naming the `totals` field it
  reads and its label ("Leads · contacts acquired", "MQL", "SQL",
  "Opportunity", "Customer stage");
* `contacts_without_created_at` — published with the counts, `None`
  otherwise.

Withheld means absent: unless `sql_status == "published"`, every one of them
is `None`. `revenue_decision_mart._withhold_cohort` (a window with no settled
day) nulls them too. No per-row stage counts are published (the slots have
them; the rows do not carry them).

### 2.3 Overview

`dashboard_overview_service._acquisition_funnel_block(cohort)` republishes the
`all_sources` slot of the mart cohort the Overview already reads (the same
build as its Google Ads leads / SQLs, over the same days, through today). It
adds no database read and decides nothing:

* `publication` = the cohort's verdict (the real freshness `assess`, the
  bucket reconciliation). A published verdict whose slot lacks a count is
  `unavailable` / `acquisition_funnel_counts_missing`, never a partial set
  of columns;
* `metric_family: "acquisition_cohort_outcomes"`,
  `window_basis: "contact_created_at"`, copied from the cohort;
* `totals` (Channels-shaped: `contacts`, `reached_mql`, `reached_sql`,
  `reached_opportunity`, `reached_customer_stage`) and
  `contacts_without_created_at`: `None` unless published.

`metric_truth` gains `lifecycle_acquisition_funnel`
(`all_source_acquisition_cohort`).

### 2.4 The page

* `acquisitionFunnelModel(data, opts)` is the one adapter for the
  acquisition cohort. Channels (`channelFunnelModel`) and Overview pass their
  payloads; Campaigns and Countries pass `martCohortFunnelData(cohort)`.
* `martCohortFunnelData(cohort)` gates on `campaignSqlPublication(cohort)` —
  the cohort is the **argument**, the block of the same response the page's
  KPI cards read. It reads no page global (05_c).
* `martCohortFunnelHtml(cohort, opts)` draws the panel; Campaigns places it
  after the KPI cards, behind the strict spend gate; Countries after its
  KPI cards, behind its geo gate.
* The block prints the family and window basis from the payload.
* No rate is drawn on Overview, Campaigns or Countries (`shares: false`):
  those pages publish none and divide nothing (PR-ADS-155's rule). Channels
  keeps its browser-divided share, as on 180A.

## 3. What was deleted, and what was kept

### 3.1 Port table

| Old | Behaviour | Now |
| --- | --- | --- |
| `renderDashLifecycleCohort` | Overview Lead-cohort panel | `renderDashAcquisitionFunnel` (acquisition cohort) |
| `dashCohortFunnelModel` | Lead-cohort model: service rates, share of Lead cohort | `acquisitionFunnelModel` (no rate; counts only on Overview) |
| `dashCohortStageBounds` | three Lead-cohort bound rules | `acquisitionCohortBound` (the acquisition cohort's one gap: no created date) |
| `dashCohortSubtitle`, `dashCohortReasonLabel` | panel text, unavailable reasons | fixed subtitle; `cohortFunnelReasonText` (the cohort's reason sentences) |
| `dashCohortCoverageBadge`, `dashCohortExclusionTitle`, `dashCohortExclusionLabel`, `dashCohortCoverageDisclosure` | Lead-cohort exclusion disclosure | the bound note on each column ("N contacts have no created date…") |
| `FUNNEL_FAMILY_LABELS.lead_entry_cohort`, `FUNNEL_WINDOW_BASIS_LABELS.date_entered_lead` | Lead-cohort family / basis labels | none (no surface draws that family) |
| `channelFunnelModel` body | Channels model | thin wrapper over `acquisitionFunnelModel` (same behaviour, plus the bound) |
| `channel_funnel_service.reached` body / `_IMPLYING` | stage-reach rule | `marketing_outcome_cohort_service.stage_reached` (delegated) |

### 3.2 Kept: the Lead-cohort backend

`dashboard_overview_service._lifecycle_funnel_block` and
`canonical_crm_funnel_service.lead_cohort_progression` are **kept**, and so
is the Overview payload's `lifecycle_cohort` key:

* `_lifecycle_funnel_block` is the Activity panel's producer;
* `lead_cohort_progression` runs inside every `canonical_crm_funnel_service.
  build` (Pipeline and Activity read that build);
* `cross_page_parity_service._lifecycle_coverage_disclosures` (the parity
  audit) reads the Overview payload's `lifecycle_cohort` coverage.

Only the panel was removed. Removing the key would silently empty that audit
disclosure; porting it to the acquisition cohort is a change to the parity
audit and is left as a proposal (§8).

## 4. Finding (a): the previous-period trend

`_lifecycle_previous_period` kept `partial` counts, so Activity printed an
exact "+X% vs prior" against a lower-bound baseline (docs/63 §8).

Now `_lifecycle_previous_period` returns `count_is_bound` per stage, by
`_stage_count_is_bound`: a bound when the previous build's
`coverage.stage_reached_without_entry_date` for the stage is above 0 **or
missing**, or when the undated reach of §5 is above 0 or unreadable.
`_activity_trend` then returns `status: "no_comparison"`:

* reason `"previous count is a lower bound"` for a bound baseline;
* reason `"current count is a lower bound"` for a bound current count (180A-F1
  closed this side on the page only; it is now in the payload too).

The page shows "No comparison" with the reason in its title. Test 06_a runs
the auditor's probe (`audit180a/probe_prevbound.py`) through the real
`_lifecycle_activity_block`: MQL is "No comparison" with that reason, Lead
still shows its measured "-25.0%". On `bf9deed` MQL read "+20.0% vs Prior
Quarter".

## 5. Finding (b): undated contacts outside the window's rows

`fetch_funnel_contacts` returns only contacts with some stage date in the
window, so its undated count never saw, for example, a contact that reached
Lead undated with its MQL date after the window, or one at stage lead with no
date at all. "0 anchorless" did not prove a Lead count exact.

New read: `crm_funnel_repository.fetch_undated_stage_reach(start, end)`. One
statement, counts only, over the **whole** table: per event, the contacts
whose current stage implies the event (the coverage rule
`build_populations` uses) and that have no effective entry date. A contact
is ruled out only where its own record proves it cannot belong: created
after the window ended **and** no stage date of its in the window (so every
contact the window's own read returns stays counted). A contact with no
`created_at` is never ruled out; All Time rules out nothing.

The Activity block publishes, per stage, `undated_contacts_could_belong` and
`count_is_bound`, and the read as `undated_reach`. `dashActivityLowerBounds`
adds to the Pipeline rule (`stageEntryLowerBounds`): above 0 → "at least N"
naming the count; unreadable → "at least N", "could not all be counted".
Unknown is never exact.

### 5.1 Choices made, and why they are not a proxy

* **Created after the window disproves membership.** A stage is entered no
  earlier than the contact exists. This is the rule the repository already
  applies to undated SQL contacts (`fetch_unresolved_sql_created_at_bounds`:
  "used to prove a contact CANNOT belong to a window"). Nothing is dated by
  it; it only rules a contact out.
* **Later stages are NOT used to narrow.** docs/63 §8 suggested narrowing
  where all later stages fall before the window. Lifecycle stages can move
  backwards and be re-entered, so a later stage dated before the window
  does not prove the earlier one was entered before it. Not narrowing errs
  toward "at least", never toward false exactness.
* **Activity only.** Pipeline uses the same stage-entry counts and the same
  narrow gap; it is not changed here (§8).

## 6. Lower bounds: contacts with no created date

A contact with no `created_at` is in no acquisition window (no other date is
substituted), so any such contact may belong to this one. While
`contacts_without_created_at` is above 0 — or the response does not state it
— every column of the acquisition block is "at least N" with the reason,
on Overview, Channels, Campaigns and Countries (`acquisitionCohortBound`).
Channels' "X% of contacts" becomes "share unknown" (both sides are bounds).

## 7. Choices where the truth was ambiguous

1. **The Overview block is the mart cohort's `all_sources` slot**, not a
   second Channels build. Channels' `build_channel_funnel` would be the same
   rule over the same contacts, but on its own evidence windows and a
   separate read. Reading the slot means the Overview's funnel and its Google
   Ads leads / SQLs are one build, one snapshot, one verdict, and no extra
   database read. Consequence: a Google Ads **attribution** reconciliation
   failure also withholds the all-source funnel. That is fail-closed, and it
   is stated here.
2. **Countries' block is the whole Google Ads cohort**, residual included,
   not the listed markets its SQL KPI card sums. The country rows carry no
   stage counts, and summing stage counts over the geo-gated rows would tie a
   lifecycle funnel to the spend gate. The panel says so in its subtitle.
   Its SQL column therefore equals `cohort.totals.sqls` (the mart total),
   which can exceed the KPI card's listed-markets figure by the residual.
3. **The Customer column is the HubSpot lifecycle Customer stage, in
   contacts** ("Reached Customer stage"), never companies or deals
   (CLAUDE.md, PR-ADS-166A). No adapter reads a customer, deal or company
   figure (05_d).
4. **The no-created-date count is the whole table's**, all sources. For the
   Google Ads cohort that is wider than the contacts that could be Google
   Ads; it only widens "at least", never narrows it.
5. **`contacts_without_created_at` missing under a published verdict is a
   bound** (fail closed). The Channels test fixture lacked the key the
   producer always publishes; it now carries `0` (§9).
6. **The KPI cards and table cells are unchanged.** Campaigns' and
   Countries' leads / SQLs cards, the Overview's Google Ads cohort KPIs and
   the Channels table cells still print the cohort counts without "at least"
   when `contacts_without_created_at > 0`. The brief scoped the bound to the
   blocks; the same production condition applies to those figures (§8).

## 8. Open, and proposals (not built)

* **Bound the cohort KPI cards and table cells** where
  `contacts_without_created_at > 0` (§7.6).
* **Pipeline's stage-entry bound** reads only the window's rows; it should
  read `fetch_undated_stage_reach` as Activity now does.
* **Port the parity audit's Lead-cohort coverage disclosure** to the
  acquisition block, then delete the Overview `lifecycle_cohort` key.
* **A Google Ads-scoped no-created-date count** for the mart cohort (§7.4).

## 9. Guards and evidence

`tests/test_pr_ads_180b_acquisition_cohort_funnel.py` — 27 cases, real
producers, repository replaced at its fetch boundary, freshness through the
real `assess`. `tests/test_pr_ads_180b_acquisition_cohort_funnel_pg_integration.py`
— 7 cases on PostgreSQL through the real reads and builders (added to the CI
PG step and its did-run list). **All 34 fail on `bf9deed`.** The decisive
ones fail on the figure, not on a missing name:

* 06_a: MQL `previous_period.status` is `ok` (+20%);
* 07_a: "Leads 10 …" — exact, with seven undated contacts outside;
* PG 06: "Leads 10 …" through the real repository.

### Audits extended (no check lives only in a test)

* `scripts/audit_marketing_outcome_cohorts.py`:
  * `mart_withheld_exposures` — the three stage totals,
    `contacts_without_created_at`, and the Overview's
    `lifecycle_acquisition_funnel` (`acquisition_funnel_exposures`);
  * `cohort_stage_problems` — under a published cohort each stage total
    equals the Google Ads slot, Google Ads + excluded = all sources, none
    above the leads, none missing beside the SQL count; called from
    `mart_conservation_problems` (Campaigns, Countries) and from
    `audit_campaign_cohort` (`stage_conservation`);
  * `independent_counts` re-derives each stage in SQL;
    `audit_campaign_cohort` checks the published all-sources slot against it
    (`stage_reach`);
  * `acquisition_funnel_problems` — the Overview block carries the cohort's
    verdict and family and equals its `all_sources` slot, column for column
    (`audit_mart_window`, `dashboard/overview acquisition_funnel`).
* `scripts/audit_campaigns_page_certification.py` S4: the Campaigns funnel
  block is gated by `campaignSqlPublication(cohort)` on `data.cohort`, behind
  the spend gate. L2 / L3 inherit the cohort audit's helpers above.
* `analysis/sql_doctrine_registry.py`: the new symbols are bound; the static
  inventory shows 0 unknown occurrences.

### Named mutations

Each applied alone to a copy of the tree, then both 180B modules run:

| | Mutation | Failed |
| --- | --- | --- |
| M1 | mart stage totals bypass the verdict (`ga.get(f)` for `_count(...)`) | 02_b, 03_b, PG 03 |
| M2 | `stage_reached` ignores the lifecycle stage | 01_a, 02_a, 03_a, 03_f, 05_a ×2, PG 01, PG 02 (the audit's own SQL), PG 04 |
| M3 | `_activity_trend` ignores a bound baseline | 06_a |
| M4 | the Activity adapter reads only `stageEntryLowerBounds` | 06_b, 07_a, 07_c, PG 06 |
| M5 | `fetch_undated_stage_reach` counts only the window's rows | PG 05, PG 06 |
| M6 | `acquisitionCohortBound` never bounds | 04_a, 04_b, PG 04 |
| M7 | `martCohortFunnelData` ignores the cohort gate | 05_b ×2, 05_c, 05_e (the campaigns audit) |
| M8 | `cohort_stage_problems` returns nothing | 02_c |
| M9 | `acquisition_funnel_problems` skips the equality | 03_d, PG 02b |
| M10 | `reconcile_cohort` checks only the old three fields | 01_c |

## 10. Changes to existing tests (each keeps its truth content)

* **`test_pr_ads_180a_shared_funnel_block`.** `_SURFACES` lists
  `renderDashAcquisitionFunnel` and `martCohortFunnelHtml` instead of the
  Lead-cohort renderer; 01_b, 04_b, 04_c, 06_d, 06_e render / scan the
  acquisition block. 06_c asserts the acquisition block draws no rate (was:
  the Lead cohort's service rates). The activity fixture carries the undated
  reach the producer now publishes (0), and bound fixtures carry it equal to
  their coverage gap (`_bound_activity`). 07_a–07_d keep their Activity and
  producer assertions; their Lead-cohort-panel assertions went with the panel
  (and `_cohort` / `_conv_titles` with them). `_overview_panels` also patches
  `fetch_undated_stage_reach` with the read's documented rule over the same
  rows (the rows are the whole table).
* **`test_pr_ads_155_f2`.** `_funnel_block` carries the undated reach
  (= its own coverage, the rows being the whole table); test_10's baseline
  states `count_is_bound: False`; the renderer slice ends at the new panel;
  test_22 checks the new panel order.
* **`test_pr_ads_155`** 31/32/35/36, **`test_pr_ads_153c`** (three
  dashboard tests), **`test_pr_ads_134`** `test_funnel_conversion_never_
  fakes_zero`: the "one cohort contract, no browser arithmetic, partial
  coverage shown" requirements are asserted on the acquisition panel (no
  rate; `shares: false`; the no-created-date bound).
* **`test_pr_ads_166a` test_32.** Renders the acquisition panel as the
  second panel ("Reached Customer stage"); the activity panel still must say
  "Moved to Customer stage"; the published Lead cohort is still checked.
* **`test_pr_ads_177a`** 01c (a slot's exact shape now has the three
  stages) and 05c (a published cohort fixture carries its stage totals).
* **`test_pr_ads_162i` `_payload`** carries `contacts_without_created_at: 0`,
  which `build_channel_funnel` publishes beside every published funnel.

## 11. Not verified

Browser rendering (no screenshot). The production volumes of
`fetch_undated_stage_reach` (one count-only scan per Overview build, two with
the previous period) were not measured on production data.
