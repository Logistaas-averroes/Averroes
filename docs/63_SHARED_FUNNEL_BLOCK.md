# 63 — One funnel block on every page

PR-ADS-180A. Presentation only: no backend figure, builder, endpoint or
payload changes. Owner request (task #13): "One funnel, one design, every
page." The menu is Overview, Channels, Campaigns (Campaigns + Keywords &
Search Terms), Countries, Pipeline and System. The Dashboard pages are the
design standard. A later PR revamps the Channels page with the owner; this PR
only puts the shared block on it.

## 1. What changed

Every lifecycle funnel in `static/app.js` is now drawn by one renderer,
`funnelBlockHtml(model, opts)`, styled by one CSS block. The renderer only
draws. Each page has an adapter that reads its own response and builds a
model.

| Surface | Adapter (reads) | Block call |
| --- | --- | --- |
| Overview → Lifecycle Activity | `dashActivityFunnelModel(d.lifecycle_activity)` | `renderDashLifecycleActivity` |
| Overview → Lifecycle Cohort | `dashCohortFunnelModel(d.lifecycle_cohort)` | `renderDashLifecycleCohort` |
| Pipeline | `pipelineFunnelModel(d.funnel)` | `pipelineFunnelHtml(d, activeEvent)` |
| Channels | `channelFunnelModel(data)` | `renderChannelFunnel` |

The Revenue tab's "Customer Efficiency" strip used the funnel markup but is
not a lifecycle funnel (§2, row E). It is now outcome cells with no arrows.

## 2. Inventory (before this PR, on `9a09c6d`)

Every place that drew a lifecycle funnel or a stage strip:

| | Surface / function | Payload → builder | Metric family | Window basis | Withheld / unavailable | Lower bound | Conversions | Stage labels |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | Overview → Lifecycle Activity, `renderDashLifecycleActivity` (`dash-activity*`) | `/api/dashboard/overview` `lifecycle_activity` → `dashboard_overview_service._lifecycle_activity_block` over `canonical_crm_funnel_service.build` (all sources) | Lifecycle stage events. Five independent populations. The block declares `basis: "stage_entry_events"` and has no `metric_family` key. | Each stage's own HubSpot entry date (`event_date_property` `hs_v2_date_entered_*`) | Whole panel `Unavailable — <reason>`. A null count is `dashValue` → "Unavailable". | **Not marked.** A note "N without an entry date" plus the "Partial historical coverage" badge. | Canonical cohort conversions, whole: `rate_pct` plus "converted of cohort_size in the X cohort". Unavailable shows the word plus the reason. No browser arithmetic. | Backend `EVENT_LABELS` (Leads, MQLs, SQLs, Opportunities, Moved to Customer stage). The sub-line is `activity_label`. The trend is inside the card. |
| B | Overview → Lifecycle Cohort, `renderDashLifecycleCohort` (`dash-funnel*`) | `lifecycle_cohort` → `canonical_crm_funnel_service.lead_cohort_progression` via `_lifecycle_funnel_block` | Lead-anchored cohort (`basis: "cohort"`, `anchor_event: "lead"`). Not the acquisition cohort. | Lead-entry date (`stages[0].event_date_column`). Later stages are counted whenever they happened. | Whole panel `Unavailable — <reason>`. `dashValue` per count. | Not marked. A note "N excluded", the "Partial lifecycle history" badge and a disclosure. | `previous_stage_conversion_pct` ("→ X%"). Null shows a muted arrow. `rate_from_anchor_pct` is shown inside the card. | `EVENT_LABELS`. The sub-line is "Entered Lead in this window" or "Of that same cohort". |
| C | Pipeline, `pipelineFunnelHtml` (`dash-funnel*`, `pipeline-funnel*`) | `/api/pipeline` `funnel` → `pipeline_service._funnel` over `canonical_crm_funnel_service.build(scope, acquisition_group)` | `metric_family: "lifecycle_stage_events"` | Per event, `window_basis` `date_entered_<stage>` | Unreadable: "Unavailable" in every stage. Mismatch: "Reconciliation required" in every stage, plus the `leadsTruthStateHtml` banner. | "at least N" plus the reason under `reconciliation.status === "partial"` (178B-F1 o1). | Cohort-safe only. 178B-F2: into a bound "at least X%"; out of a bound "—" with the reason. A missing rate shows "—". | "Entered Lead / MQL / SQL / Opportunity", "Moved to Customer stage in HubSpot" plus help text. Stages are buttons. **It read the page global `_leadsView`.** |
| D | Channels totals strip, `channelFunnelTotalsHtml` (`dash-outcomes` + `channel-funnel-totals`) | `/api/channel-funnel` `totals` / `stages` / `publication` → `channel_funnel_service.build_channel_funnel` | `metric_family: "acquisition_cohort_outcomes"` | `window_basis: "contact_created_at"`. Each stage is the furthest stage reached so far. | Page-level `<strong>Withheld</strong> — <reason>`. Totals are not read. | n/a | No step conversion. Each stage's share of contacts acquired is computed in the browser ("X% of contacts"). | "Contacts acquired", "Reached MQL / SQL / Opportunity / Customer stage". **A missing count rendered as "—".** |
| E | Dashboard → Revenue "Customer Efficiency", `renderRevConversionStrip` (`dash-funnel*`) | `/api/dashboard/revenue` `kpis` → `dashboard_revenue_service.build_dashboard_revenue` | **Mixed, so not a lifecycle funnel.** SQLs are the acquisition cohort, gated by `cohortCountHtml(k.sqls, d.cohort, null)`. Deals, revenue and average deal value are close-date outcomes. | Two windows: contact creation date, and deal close date | `cohortCountHtml` / `canonicalMetricHtml` / `dashValue` | n/a | Arrows between the two populations. The arrows carried `sql_to_customer_rate` and `revenue_per_sql`, both unavailable by contract. | SQLs → Closed-Won Deals → Closed-Won Revenue → Avg Deal Value |

These surfaces are **not** funnels. They were inventoried and left as they
are:

* **Campaigns and Countries.** This covers the page KPI cards, the drawers,
  and the Dashboard → Campaigns / Countries tabs. They show cohort Leads and
  SQLs (`cohortCountHtml` on the response's cohort) as KPI cards and drawer
  rows. They have no MQL, Opportunity or Customer-stage figure and no stage
  strip. See §8 for why no block was added.
* **The Channels page body.** This is the per-channel table cells, the two
  rankings and the stage picker. They are per-channel funnel columns, not
  the funnel block. The owner's later Channels revamp covers them.
* **The Pipeline KPI cards and Overview → Commercial Outcomes.** They hold
  the reader-contract deals, customers and revenue, deliberately outside the
  funnel (PR-ADS-155 §2).
* **The contact drawer's "Funnel history".** This is one contact's
  timeline, not a population funnel.
* **Dashboard → Channels.** It uses HubSpot lead status for quality and has
  no lifecycle stages.

## 3. Design

### 3.1 The model

```
{ family:      {key, basis},              // the payload's own metadata
  verdict:     {status, reason, word, note},
  stages:      [{slot, key, label, metric {value,status,reason}, bound_reason,
                 not_on_page, sub, sub_title, notes[], help, action_title}],
  conversions: [{kind, pct, basis, title}] }   // four, between adjacent slots
opts: {ariaLabel, selectable, activeKey}
```

### 3.2 The five columns

The slots are `FUNNEL_BLOCK_SLOTS = contacts, mql, sql, opportunity,
customer`. They appear in the same order and the same grid on every page,
and each column has its own colour from one ramp (`FUNNEL_BLOCK_RAMP`, the
Dashboard's).

Each column's label is the page's own label for what it counts. The labels
are:

* "Entered Lead" (Pipeline);
* "Leads" (Overview);
* "Contacts acquired" (Channels).

The block does not rename a stage to make the pages read alike. Identical
columns do not mean identical populations (§3.4).

### 3.3 Values

A stage value renders through `canonicalMetricHtml(metric, fmtCount)`:

* A published 0 is `0`.
* Withheld or unavailable is the word plus its reason. It is never a dash
  and never a number.
* A published count with `bound_reason` is `at least N`. The value carries
  `data-lower-bound` and the stage shows a note "Lower bound: \<reason\>".
* A column the model does not carry is **"Not available on this page"**
  plus a reason. It is never 0 and never a dash.

### 3.4 The whole-funnel verdict

If the verdict is not "published", every column shows the word and one line
gives the reason. The block reads no stage figure at all: withheld means
absent. A model without a verdict fails closed as Unavailable.

### 3.5 Conversions (178B-F2, exactly)

| Kind | Renders as |
| --- | --- |
| `exact` | `X%` |
| `at_least` | `at least X%`. The TO stage is a lower bound. |
| `unknown` | `—`, with the reason in the title. The FROM stage is a lower bound or unknown. |
| `unavailable` | The word "Unavailable", plus the reason |
| `none` | A plain arrow and no rate. Used where the page publishes none (Channels). |

An exact or at-least connector that has no rate is "Unavailable", never
`0%`. The renderer has no division: every rate is the adapter's, read whole
from the payload.

`stageEntryConversion` is the single implementation of the F2 rule. It is
shared by Pipeline and Overview → Activity. `stageEntryLowerBounds` (the old
`pipelineFunnelLowerBounds`) is shared the same way.

### 3.6 Metric family and window basis

The family and window basis are printed on the block's face. They are also
set as `data-metric-family` and `data-window-basis`, taken from the
payload:

| Surface | Payload metadata the adapter copies | Label |
| --- | --- | --- |
| Pipeline | `funnel.metric_family` (`lifecycle_stage_events`) | "Lifecycle stage events …" |
| Overview → Activity | `lifecycle_activity.basis` (`stage_entry_events`) | "Lifecycle stage events …" |
| Overview → Cohort | `anchor_event: "lead"` (→ `lead_entry_cohort`) | "Lead cohort …" |
| Channels | `metric_family` (`acquisition_cohort_outcomes`) | "Acquisition cohort …" |

The window basis comes from these fields:

* Pipeline: every event's own `window_basis` / `event_date_property`.
* Overview → Activity: every stage's `event_date_property`.
* Overview → Cohort: the anchor stage's `event_date_column`.
* Channels: `window_basis`.

A key the block does not know, or a missing key, is printed as "Metric
family not stated by this response". The block never guesses. Two pages
show different SQL families: Overview and Pipeline show stage entries,
Channels shows the acquisition cohort. Each block says which one it is, and
no adapter computes one family from another.

### 3.7 No page state

The renderer takes its data only as an argument. Pipeline's selected stage
used to be read from the global `_leadsView`. It is now
`pipelineFunnelHtml(d, activeEvent)`, and `renderPipelinePage` passes it.

The renderer's only indirect global is `_currentUser`. It is read inside
`canonicalMetricHtml` → `crmBlockersHref`, which is the admin-only "View
blockers" link of every reader-contract value. A lifecycle stage reason maps
to no blocker family, so no link is drawn.

### 3.8 CSS

There is one block in `static/styles.css`. It is a single grid row: five
equal columns and four connectors, using the old activity panel's layout
(PR-ADS-155-F2). Below 900px it is one column, and each connector spans the
row between its two stages.

Selectable stages are `button.dash-funnel__stage--selectable`. Notes (trend,
share, exclusion, bound) sit inside the stage and are never between two
stages.

## 4. Choices where the truth was ambiguous

1. **The Customer column is the lifecycle Customer stage (contacts), never
   companies.** The brief said "the Customer stage is distinct companies from
   the reader contract". Every ported funnel's fifth stage counts contacts
   moved to the lifecycle Customer stage. Those are "Moved to Customer stage
   in HubSpot" and "Reached Customer stage", and PR-ADS-166A deliberately
   labels them as not customers.

   Distinct companies is a different population: deals by close date. PR-ADS-155 §2 took
   exactly that crossing out of the Dashboard funnel ("the last two cells
   crossed into an entirely different truth — deals, not contacts — under the
   same connecting arrows"). Putting companies in the column would join two
   cohorts under one arrow. Labelling the contacts "Customers" would call
   contacts customers.

   So the column keeps its contact count and its stage label. The
   reader-contract Customers (distinct companies, never summed) stay where
   they are: the Pipeline KPI card, Overview → Commercial Outcomes and the
   Campaigns cards. `test_06_e` guards this: no adapter reads a
   customer, deal or company figure, and the column's label always says
   "stage".
2. **Overview → Lifecycle Activity now marks lower bounds.** It is the same
   stage-entry count on the same all-source scope as Pipeline. On `9a09c6d`
   Pipeline said "at least 42" where Overview said "42". Both now use
   `stageEntryLowerBounds` and the F2 conversion rule. This reads only the
   payload's existing `reconciliation` and `coverage`. The old
   "N without an entry date" note is replaced by the bound note; the count is
   in it, and the coverage disclosure still lists every gap.
3. **Overview → Lifecycle Cohort exclusions are not marked as bounds.** I
   stopped here. A contact excluded for `missing_stage_entry_date` or
   `prior_stage_unproven` did reach the stage, so that stage's count, and
   every later one, may be an undercount. But the cohort payload's exclusion
   records do not say which later stages each excluded contact would have
   reached. Turning "N excluded" into "at least N" would be a new semantic
   for that contract. The block keeps the existing exact count, "N excluded"
   note, badge and disclosure. **Proposal for the truth auditor:** decide
   whether a cohort stage with a missing-date exclusion at or before it is a
   lower bound.
4. **A missing conversion is "Unavailable", not "—".** This applies to
   Pipeline (no cohort-safe rate) and the Overview cohort (null
   `previous_stage_conversion_pct`, which was a muted arrow). Both now show
   the reader-contract word with the reason. The F2 "—" means "unknown
   because the FROM stage is a bound", and it is kept exactly. Each mark now
   means one thing.
5. **A missing Channels totals count is "Unavailable", not "—".** The
   block's rule is never a dash. The per-channel table cells keep their "—"
   because the table is not part of the block, and the owner will redesign
   Channels separately.
6. **The family key for the Lead cohort is derived.** `lead_entry_cohort` is
   derived from the payload's `anchor_event`, and the activity block's
   family comes from its `basis`. Neither payload carries `metric_family`,
   and no backend metadata was added (no backend change).
7. **Channels: a stage key the API adds later** (not one of the four mapped
   keys) is not drawn in the five-column block. The table and rankings still
   show it.
8. **The Revenue tab strip is outcome cells.** Its figures and verdicts are
   unchanged. The two cross-cohort ratios are labelled cells that remain
   unavailable, with their reasons. A double escape of the "through …" suffix
   is fixed.

## 5. Port table: old function → where each behaviour now holds

| Old (deleted or rewritten) | Behaviour | Now |
| --- | --- | --- |
| `renderDashLifecycleActivity` markup | Stage cells, connectors, unavailable panel | `funnelBlockHtml` (+ `dashActivityFunnelModel`) |
| `dashActivityConversion` (HTML) | Fail-closed guard; canonical rate plus basis; reason words | `dashActivityConversion` returns a connector model (same guard); `stageEntryConversion`; `funnelBlockConvHtml` |
| `dashActivityTrend` (HTML) | A trend inside the card; "No comparison" | `dashActivityTrend` returns a stage note; `funnelBlockStageHtml` renders it inside the stage |
| Activity "N without an entry date" | Gap disclosure | Bound note (`stageEntryLowerBounds`) plus `dashActivityCoverageDisclosure` (kept) |
| `renderDashLifecycleCohort` markup | Cells, "→ X%", share, "N excluded" | `dashCohortFunnelModel` → `funnelBlockHtml`. The share and exclusion are stage notes. A null rate is "Unavailable". |
| `pipelineFunnelHtml` markup | Buttons, `data-funnel-event`, active state, Unavailable / Reconciliation required, help text | `pipelineFunnelModel` → `funnelBlockHtml({selectable, activeKey})`. The panel header, truth banner, working-status button and lead-filter note stay in `pipelineFunnelHtml`. |
| `pipelineFunnelLowerBounds` | Lower bounds with reasons | `stageEntryLowerBounds(f, events)`, shared |
| Pipeline F2 chips | at least X% / — | `stageEntryConversion`, shared |
| `channelFunnelTotalsHtml` | Contacts acquired plus Reached X with share | `channelFunnelModel` → `funnelBlockHtml` (share in the sub-line) |
| Channels withheld panel | `<strong>Withheld</strong> — reason. No figures …` | The block's verdict line (same words) |
| `renderRevConversionStrip` | SQLs, deals, revenue, average, two ratios | `renderRevCustomerEfficiency` (outcome cells, no arrows) |
| `DASH_FUNNEL_RAMP` | Stage colours | `FUNNEL_BLOCK_RAMP` |
| CSS `.dash-activity*`, `.pipeline-funnel*`, `.channel-funnel-totals*`, `.dash-funnel__stage-share/-gap/-delta`, `.leads-customer-figure__help` | Per-page funnel styles | The PR-ADS-180A block in `styles.css` |

## 6. Guards and evidence

`tests/test_pr_ads_180a_shared_funnel_block.py` has 29 cases. All 29 fail on
`9a09c6d`, where `funnelBlockHtml` does not exist.

The cases are grouped as follows:

* **Every surface calls the block.** These are 01_a and 01_b. The four surfaces
  render exactly one block each, with five slots in order. No other function
  emits `dash-funnel*` / `funnel-block*` (02_a), and the old renderers and
  CSS are gone (02_c).
* **Renderer behaviour, evaluated in `node` on the extracted block section.**
  These are 03_a to 03_i. The harness runs the block, plus `canonicalMetricHtml`
  and what it needs, and nothing else. The cases cover:
  * a published value and a published 0;
  * withheld and unavailable stages with their reasons;
  * a withheld verdict beside a faulty sentinel value;
  * a lower bound;
  * all five connector kinds;
  * "Not available on this page";
  * a missing verdict;
  * no division;
  * the selection argument.
* **The family comes from the argument and the payload.** 04_a to 04_c. Mutating
  each payload's metadata changes the label.
* **No page state.** 05_a to 05_d: a static scan of the block against every
  top-level `let` and every mutated top-level object, the isolated run, an
  unchanged render after poisoning the page globals, and the Pipeline
  selection taken from the argument.
* **Per-page truth.** 06_a to 06_g: Activity rates and trend placement, the
  activity lower bound under F2, cohort service rates, the unavailable
  blocks, the Customer column, Channels withheld with no figure, and the
  Revenue cells.

### Named mutations

Each mutation was applied to `static/app.js`. Each line below gives the
mutation and the tests that then failed.

| Mutation | Change | Tests that failed |
| --- | --- | --- |
| M1 (in-module, 02_b) | A second function emits a `dash-funnel__stage` cell | The scan names it |
| M2 (in-module, 05_b) | The block reads `_leadsView` | The scan names it, and the isolated run throws |
| M3 (in-module, 04_d) | The family label is hard-coded | The label no longer follows the argument |
| M4 | The lower-bound branch in `funnelBlockValueHtml` is removed | 180A 03_d, 06_b; 178B-F1 test_08_a; 178C test_09 |
| M5 | `funnelBlockValueHtml` returns "—" for a published 0 | 180A 03_a; 166A test_10 |
| M6 | The verdict check in `funnelBlockValueHtml` is removed (withheld reads values) | 180A 03_c, 06_f |
| M7 | `stageEntryConversion` ignores `fromBound` | 180A 06_b; 178B-F1 test_09_a |
| M8 | `pipelineFunnelModel` hard-codes `key: "lifecycle_stage_events"` | 180A 04_b |
| M9 | `funnelBlockConvHtml` renders an exact chip with a null rate as `0%` | 180A 03_e |

The 178B-F1 static scan, "every function app.js calls is defined"
(`test_02_every_function_app_js_calls_is_defined`), passes.

## 7. Changes to existing tests (each keeps its truth content)

* **`test_empty_state_page_explanations` `test_build_empty_state_called_in_leads`, and
  `test_pr_ads_153c` `test_frontend_renders_unavailable_scope_as_dash_never_zero`.**
  They sliced `pipelineFunnelHtml` for `(count === null …)` and `"Unavailable"`.
  The count now goes through `funnelCountMetric` (null becomes an
  "unavailable" verdict) to `canonicalMetricHtml` (the word). Both tests now
  assert that chain, and `|| 0` is still banned.
* **`test_pr_ads_153c` `test_frontend_withholds_counts_on_mismatch`.** The same
  two expressions are now asserted in `pipelineFunnelModel`.
  `test_conversions_render_only_when_cohort_safe` asserts that the model calls
  `stageEntryConversion`, and that this function requires `conv.available`
  and `conv.basis === "cohort"`.
* **`test_pr_ads_153c` `test_dashboard_conversion_helper_requires_cohort_basis`.**
  It now slices `dashCohortFunnelModel`. The new doc comment above that
  function would otherwise have satisfied the assertion with prose. It
  asserts the field reads `s.previous_stage_conversion_pct` and
  `s.rate_from_anchor_pct`.
* **`test_pr_ads_134` `test_funnel_conversion_never_fakes_zero`.** "A null rate
  is a muted arrow" is now "a null rate is the muted word Unavailable" (§4.4).
  The null-rate guard and the null-count path (`funnelCountMetric(s.reached,
  null)`) are asserted in the adapter.
* **`test_pr_ads_155_f2`.**
  * test_4 asserts that the rate the reader sees is `conv.rate_pct` whole:
    `stageEntryConversion` holds `pct: conv.rate_pct` with no division, and
    the chip prints `c.pct`.
  * test_8 asserts the unavailable connector model and its muted rendering.
  * test_9_a asserts `a.available !== true` (stricter than `!activity.available`).
  * test_10 is now rendered in `node` from the backend-built block: the trend
    is inside each stage cell and never in a connector.
  * test_21 asserts the same grid, shrink and 900px stacking rules on
    `.dash-funnel`, and that `.dash-activity` is gone.
* **The Channels harnesses (`test_pr_ads_162a` `_render`,
  `test_pr_ads_162i` `_node`, `test_pr_ads_168a` `_render`, which 171B
  reuses).** They evaluated a slice of `app.js`: the Channels span plus
  hand-picked helpers. The page now calls the shared block, which is defined
  outside that span. The harnesses now evaluate the whole real `app.js` in the
  161B vm harness. The expressions and payloads are the same, `168a`'s
  `mutate` still applies inside the Channels span only. The unused
  helper-extraction constants are removed; `_js_extract` stays because 171B
  imports it.
* **`test_pr_ads_162i` `test_10_a`.** "Reached Opportunity — share unknown"
  becomes "Reached Opportunity Unavailable the server sent no count for this
  stage share unknown" (§4.5). The table assertions with "—" are unchanged.
* **`test_pr_ads_166a` `test_32`.** No change. It asserts the activity chip's
  tooltip "went on to enter Customer stage." Its real fixture leaves the
  Customer stage undated, so that chip is now "at least X%" (§4.2). The
  at-least title keeps the cohort description after the bound's reason, so
  the test passes unedited.

## 8. Proposals (not built)

* **A block on Campaigns and Countries.** Neither page draws a funnel today.
  Their lifecycle figures are acquisition-cohort Leads and SQLs. They carry
  no MQL, Opportunity or Customer-stage count, and their only customer figure
  is close-date distinct companies, which cannot be a column (§4.1). A block
  there would be two figures and three "Not available on this page" columns.
  A useful block needs the acquisition cohort's MQL, Opportunity and
  Customer-stage counts per window, added to the mart's `cohort` contract
  (`marketing_outcome_cohort_service`, which Channels already computes per
  channel). That is a backend addition and is left for the owner to
  approve.
* **Lead cohort lower bounds.** See §4.3.

## 9. Not verified

Browser rendering. The layout is the old Overview activity grid applied to
every block; no screenshot was taken.
