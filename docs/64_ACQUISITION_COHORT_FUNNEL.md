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
* `contacts_without_created_at` — the count of canonical contacts with no
  created date (table-wide). **F1 (§12.3): published whatever the verdict**
  — it is a data-quality disclosure, not a cohort figure; `None` only when
  unread.

Withheld means absent: unless `sql_status == "published"`, every stage total
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
  `reached_opportunity`, `reached_customer_stage`): `None` unless published;
  `contacts_without_created_at`: the cohort's, republished whatever the
  verdict (F1, §12.3).

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
`_lifecycle_activity_block`: MQL is "No comparison" with that reason, and in
**that fixture** Lead still shows its measured "-25.0%" (the fixture has no
undated Lead contact). On `bf9deed` MQL read "+20.0% vs Prior Quarter".

**Production will not look like the fixture** (F1 correction). About 9,000
production contacts reached Lead with no Lead-entry date, and
`fetch_undated_stage_reach` counts them for every window they could belong
to. So in production every Activity stage reads "at least N" and every
trend reads "No comparison" — in practice permanently, until those dates
are recovered. That is the correct reading of the evidence, not a defect of
the rule.

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
  read `fetch_undated_stage_reach` as Activity now does. *(Built by
  PR-ADS-180C, `docs/65`.)*
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
  * `mart_withheld_exposures` — the three stage totals and the Overview's
    `lifecycle_acquisition_funnel` (`acquisition_funnel_exposures`).
    (180B also scanned `contacts_without_created_at`; F1 replaced that with
    `created_at_disclosure_problems`, §12.3);
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

## 12. PR-ADS-180B-F1 — the truth audit's minors

The truth audit of 180B (`46f5396`) found no Major. F1 closes its minors.
Guards: `tests/test_pr_ads_180b_f1_truth_audit_minors.py` (13 cases) and an
extended PG case 05.

### 12.1 m1 — the "missing means a bound" branches are tested

`_stage_count_is_bound` (`own is None or own > 0`) and the Activity block's
`prev_bounds.get(event) is not False` had no case: mutations G
(`own is not None and own > 0`) and H (`is True`) survived. Now:

* 01_a — a build whose coverage does not report MQL: `count_is_bound` is
  True and the trend is "No comparison" (current count is a lower bound);
* 01_b — the helper with no coverage, non-mapping coverage, or a coverage
  silent on the stage: True even with a readable undated reach of 0;
* 01_c — a previous period with no `count_is_bound` (all stages, then MQL
  only): "No comparison", "previous count is a lower bound"; with it, the
  measured "+20.0%".

**Found while writing 01_a.** The backend marked MQL a bound, but the page
printed "MQLs 12" exact: `dashActivityLowerBounds` re-derived the bound from
the coverage (only when reconciliation is `partial`) and the undated reach,
and never read the payload's own `count_is_bound`. It now republishes it:
`count_is_bound === true` is a lower bound on the page even where no other
reason is named. It only ever adds a bound. (A stage without the key keeps
the page's own rules: the real producer always sets it, and treating its
absence as a bound would rewrite 180A's fixtures rather than fix a
production path.) 01_a fails on `46f5396` on the figure ("MQLs 12").

### 12.2 m2 — the undated reach's edges on PostgreSQL

PG case 05 adds four contacts, at the database's own `end::date + 1 day`
instant (read back, so the case holds in any session time zone):

| Contact | Record | Expected |
| --- | --- | --- |
| `edge-last-day` | stage lead, undated, created one minute before the window's end | counted |
| `edge-no-created` | stage lead, undated, NULL `created_at` | counted |
| `edge-day-after` | stage lead, undated, created at the window's end instant | ruled out |
| `edge-recovered` | stage lead, no direct Lead date, Lead date recovered from history | not undated |

Lead is 9 (was 7), All Time 11 (was 8). Mutation K (drop
`created_at IS NOT NULL`) and mutation L (`>= end` for `>= end + 1 day`)
each fail it.

### 12.3 m4 — `contacts_without_created_at` is a disclosure, published always

180B withheld `cohort.contacts_without_created_at` with the cohort, but the
same table-wide count stayed in `readiness.missing_contact_created_at_count`
and in `cohort.metadata.coverage_notes`. Three copies, two policies.

**Choice: publish it whatever the verdict.** It counts every canonical
contact with no created date — all sources, no window — so it is a
data-quality fact about the table, not a figure of the cohort, and no
withheld cohort count can be recovered from it. Nothing reads it as a cohort
number: on the page, `martCohortFunnelData` passes it only under
`campaignSqlPublication(cohort)`, and `acquisitionFunnelModel` reads it only
beside published counts (04_a / 04_b render a withheld block carrying it:
no figure, no "no created date" note).

* `revenue_attribution_service._cohort_contract_block`: published always
  (`_disclosed_count`: the integer read, `None` only when unread);
* `revenue_decision_mart._withhold_cohort` keeps it (a window with no
  settled day still discloses it, as readiness does);
* `dashboard_overview_service._acquisition_funnel_block` republishes it
  under any verdict;
* `revenue_decision_mart._readiness_block`: `missing_contact_created_at_count`
  no longer defaults to 0 when the core did not read it (unknown is not
  zero);
* the audit: `mart_withheld_exposures` / `acquisition_funnel_exposures` no
  longer scan it; new `created_at_disclosure_problems` holds every copy to
  one value — cohort, readiness, Overview block, and exactly one coverage
  note naming it when above 0 (none at 0 or unknown) — and
  `audit_mart_window` runs it on every payload (`created_at_disclosure`).

Changed 180B assertions (the contract changed; each now asserts the value):
02_b (`== 4`, and a disagreeing readiness is named), 02_d (`== 2`), 03_b
(`== 3`), PG 03 (`== 0` and `created_at_disclosure_problems == []`).

Channels (`channel_funnel_service`) still withholds its own copy with its
funnel: its response carries no readiness or coverage-note copy, so within
that response the figure is either present everywhere or nowhere, and its
"withheld: no number anywhere" test (162A test_08) stays as it is.

### 12.4 m5 — the legend states the stage-reach rule

`ACQUISITION_FUNNEL_LEGEND` (Overview, Campaigns, Countries) and Channels'
`STAGE_NOTE` now say a contact counts at a stage when HubSpot records it
entering that stage **or** its current lifecycle stage is that stage or a
later one, and that the columns are **not nested subsets** — a contact whose
stage moved back can count at a later stage without an earlier one, so a
later column can exceed an earlier one. The rule itself
(`marketing_outcome_cohort_service.stage_reached`) is unchanged: it drives
the one SQL count, and changing it is an owner decision.

### 12.5 m6 — Countries: the residual inside Reached SQL

The Countries funnel's Reached SQL is the whole Google Ads cohort
(`cohort.totals.sqls`); the SQLs card sums the listed markets. The SQL
column now carries a note, `countryFunnelResidualNote(cohort, residual)`:

* the residual row of the **same** response publishes its SQL count
  (`cohortCountPublication`, the table cell's gate): "Includes N in markets
  not listed or with no country (the residual row); the SQLs card above
  counts the listed markets only";
* the residual's count is not published: said in words, with its reason —
  never `cohort − card` in the browser;
* no residual row: "how many … are outside the listed markets is not
  stated";
* gated by `campaignSqlPublication(cohort)` like the funnel; unpublished,
  no note (and stage notes draw only under a published verdict anyway).

### 12.6 The `lower_bound` → `count_is_bound` rename

180B named the per-stage flag `count_is_bound`, not `lower_bound`. The 161E
guard (`test_pr_ads_161e_reader_cutover.py` test_10_a) bans the substring
`lower_bound` in the revenue reader services (`dashboard_overview_service`
among them) and in `static/app.js`, to stop a reader republishing
`confirmed_customer_lower_bound` as a customer count. This flag is a boolean on lifecycle stage counts —
"this count is a lower bound" — and carries no customer figure, so the
guard's intent is not evaded by the name. The guard remains a substring
check; a future flag must be judged on what it carries, not on whether its
name passes.

### 12.7 Recorded, not changed

* **m7 does not arise today.** Production has **0** contacts with NULL
  `created_at` (checked 25 Nov, 21,947 contacts). So the KPI cards and table
  cells printing exact cohort counts beside a block that would read "at
  least N" (§7.6) cannot happen now. It stays open **as a rule**: the day a
  contact without a created date appears, those figures are lower bounds
  and must say so.
* **m3 is the next PR.** Pipeline's stage-entry bound still reads only the
  window's rows, not `fetch_undated_stage_reach`: same window, same
  population, Activity "Leads at least 10" against Pipeline "Entered Lead
  10". Masked in production by the ~9,000 undated Lead contacts.
  *(Closed by PR-ADS-180C, `docs/65`.)*
* Audit gap carried: stage counts are re-derived by the audit's own SQL for
  `all_sources` only; the Google Ads stage split is checked against the
  payload's breakdown.
