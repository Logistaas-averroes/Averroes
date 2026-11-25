# 61 — One Pipeline page: leads and deals together

**PR-ADS-178B** builds one Pipeline page in the Dashboard design on
one builder, `build_pipeline`, and points Dashboard → Deals at the same
builder. **PR-ADS-178C** retires the old routes after porting what parity and
the audits still read from them (§8 listed them; §13 is the record).

Depends on: PR-ADS-177B (`d543dcc`, in CI as PR #233). Unblocks: 178C.

---

## 1. The problem

The Pipeline menu item held two pages, and Dashboard → Deals was a third
surface with its own composition:

| Surface | Reader | Figures |
| --- | --- | --- |
| Leads (`#/leads`, `loadLeads`) | `/api/crm-funnel` → `canonical_crm_funnel_service.build`; `/api/crm-funnel/contacts`; `/api/crm-funnel/customers-won` → `build_customers_won`; `/api/crm-funnel/operational-status` | lifecycle stage entries; customers won (its own `load_won_deals` read) |
| Closed-Won Deals (`#/deals`, `loadDeals`) | `/api/revenue-performance?view=deal` → mart `_view_payload` → `build_revenue_deals` | deal list; `ledger_summary` strip (deals, revenue, average, GCLID subset) |
| Dashboard → Deals | `/api/dashboard/deals` → `build_dashboard_deals` (`@single_snapshot`) | mart deal summary **and** `load_won_deals` / `summarize_deals` **and** `build_revenue_by_source` **and** the through-today campaign view **and** `fetch_sql_lead_details` (legacy) **and** a previous-window mart |

The same metric came from different builders: three SQL counts (the cohort,
the funnel's lifecycle SQL stage, the legacy lead-status backlog); three
closed-won counts over one ledger (mart summary, `ledger_summary`,
`summarize_deals`); two customer builders (mart, `build_customers_won`); won
revenue with a page-side `amount_unknown` check beside the reader contract;
"Opportunities" meaning a lifecycle stage on Leads and an always-Unavailable
deal stage on the Dashboard. Four Dashboard → Deals KPI cards were always
"Unavailable" (Opportunities Created, Open Pipeline, SQL→Customer,
Opportunity→Customer); `_deal_row`'s Company and Age were always null.
`/api/revenue-deals` has no caller.

## 2. What this PR does

* One page, `pipeline`, titled "Pipeline", in the Dashboard design (§5).
* One builder, `services/pipeline_service.build_pipeline` (`@single_snapshot`),
  served at `GET /api/pipeline?window=&scope=&acquisition_group=` (§4). The
  paged contact rows stay on `/api/crm-funnel/contacts` (and the working-status
  breakdown on `/api/crm-funnel/operational-status`).
* Dashboard → Deals republishes `build_pipeline` (plus a previous-period
  comparison from the same builder) and renders the Pipeline page's own
  renderers. Its second composition is deleted (§6).
* `fetch_sql_lead_details` (the legacy lead-status backlog's reader) is
  deleted; every reader of `kpis.lead_status_qualified_no_won_deal` is moved
  (§6.1).
* Navigation: `NAV_ITEMS.pipeline = ["pipeline"]`; `leads`, `deals` and
  `opportunities` are redirects that carry their intent (§7).
* Registries and audits: the reader-cutover INVENTORY and `_page_builders()`
  (with a new `pipeline_checks` for `windows_reconcile`), cross-page parity
  (a `pipeline` consumer on four identities), the cohort audit (the pipeline
  payload, `{value, status, reason}` KPIs scanned), the SQL-doctrine registry
  (**12 canonical / 11 legacy / 8 mixed, 0 unknown**; was 10 / 11 / 9).

## 3. Decisions

**Owner decisions, 2026-11-25:**

1. **Windows: business windows through today**, as today and per the
   2026-11-20 decision. Not settled. (Campaigns alone ends yesterday,
   `docs/60` §4.1.) A deal closed today is on the Pipeline page.
2. **SQL headline: the Google Ads acquisition cohort** — the same count as
   Overview, Campaigns and Dashboard → Deals — gated with `cohortCountHtml` on
   THIS response's own `cohort` block. The funnel stays lifecycle stage
   entries, labelled **"Entered stage in window"**; its stages read "Entered
   Lead", "Entered MQL", "Entered SQL", "Entered Opportunity", "Moved to
   Customer stage in HubSpot". No bare "SQLs" label sits on the funnel.
3. **Lead lists open on a funnel-stage click**, names shown, no emails — the
   current Leads behaviour and the current contacts route contract.
4. **Drop the legacy "Qualified (lead status)" backlog** and the source
   breakdown's lead-status column. Not replaced by "cohort SQLs with no won
   deal" (that would mix two cohorts).
5. **Deals list: all closed-won deals**, with a Source column and a filter on
   the deal's own source group.
6. **Keep Dashboard → Deals**, reading the same builder (the 177B precedent).
7. **Defaults as today:** Current Quarter, All Sources; deals by close date
   newest first, then amount; leads by newest stage entry first (the contacts
   route's own order).

**PR-ADS-178B decisions** (readings of the brief and standing doctrine):

* **KPI sources.** SQLs = the mart deal view's `summary.metrics.sqls` (the
  cohort's one verdict). Closed-won deals, customers, won revenue = the mart
  deal view's `summary.metrics` — the window's canonical reader-contract
  verdicts, the identity Overview, Revenue and Channels publish and the parity
  audit checks. Average deal value = the deal view's own `ledger_summary`
  (published total ÷ every won deal), shown only when that summary counted the
  same deals and revenue as the cards; a published 0 deals has no average
  (`no_closed_won_deals_in_window`), never $0. Each KPI is `{value, status,
  reason}`; a withheld one carries no value.
* **The deal list** is `build_revenue_deals`' rows (the mart deal view's),
  shown only when the closed-won count is published, the deal view's own
  population verdict says the same count, and the list numbers exactly that
  count; otherwise `deals` is `None` with the count's status and reason (or
  `unavailable` / `closed_won_rows_do_not_match_count`, the PR-ADS-164A
  reason). The client checks again (`pipelineDealRows`: the 164A
  `closedWonPopulationState` guard, and the card's count).
* **Customers won** (§6 of the brief): `_won_customers_block` over the same
  snapshot's `load_won_deals`, one entry per distinct company, only beside a
  published customer count and only when it numbers that count. Never merged
  with the deal list.
* **Lead filters.** Scope and Source are LEAD filters: they narrow the funnel
  (`crm_funnel.build(scope=, acquisition_group=)`) and the leads list; every
  deal figure and the cohort KPI are the window's whatever they are.
  Customers won is the window's all-source won-deal population too: the lead
  filters do not touch it, so it is the same list filtered or not
  (PR-ADS-178B-F1 m1; 178B had made it `unavailable` /
  `customers_won_not_split_by_contact_source` under a lead filter, withholding
  a figure no filter changes). Under a lead filter the SQL card says it is the
  Google Ads cohort, not narrowed by the lead filters, and the funnel note
  says it is a different population (all sources) (F1 m5). The page labels the
  selectors "Leads scope" / "Leads source" inside a dashed "lead filters" group.
* **The working-status breakdown** ("Disqualified / Other", operational, not
  a funnel stage) is kept as a muted button under the funnel, rendering into the
  leads panel exactly as the Leads page's tab did.
* **The deal drawer** shows only fields the ledger carries for a deal (deal,
  id, revenue, close date, stage, source group, campaign label, country,
  attribution status / scope). Company, company id, main contact, created
  date, age and days to close (always null) are dropped.
* **Source breakdown** (Dashboard → Deals only): the deal list partitioned by
  each deal's own source group, each part through `subset_metrics`; customers
  per group are distinct companies and are not added up.
* **Truth footer.** One line: "Open and lost deals are not in the deal ledger:
  only closed-won deals are synced, so open pipeline, lost deals and
  opportunity aging are not shown here." The always-Unavailable cards, the
  aging panel and the decision cards are dropped; so is the GCLID-attributable
  deals strip figure (not one of the brief's five cards).
* **Previous-period deltas on Dashboard → Deals** are `build_pipeline` over the
  previous window (same builder, another window) — no second composition. The
  SQL card's chip is the 177A-F1 one (completed windows only).

## 4. The builder contract

`build_pipeline(window, now=None, *, scope="all_source", acquisition_group=None)`
(`@canonical_revenue_service.single_snapshot`; `ValueError` → 400 on a bad
window, scope or group):

```
{page: "pipeline", window, generated_at, read_only, source_truth,
 revenue_scope: "all_source", google_ads_conversion_value_used: False,
 legacy_fallback_used: False, as_of,
 filters {scope, scope_label, acquisition_group, acquisition_group_label,
          lead_filtered, applies_to: ["funnel", "leads"]},
 cohort, metric_family, window_basis,                 # the mart's cohort block
 kpis {sqls, closed_won_deals, customers (dedup_key, additive: False),
       won_revenue_usd, average_deal_value_usd},       # each {value, status, reason}
 metrics {closed_won_deals, customers, revenue_usd},  # the list's own verdicts (audit sibling rule)
 closed_won_population,                               # the deal view's verdict
 funnel {available, reason, label: "Entered stage in window",
         metric_family: "lifecycle_stage_events", window, scope(_label),
         acquisition_group(_label), campaign_identity_available,
         events, conversions, reconciliation, sync},
 deals: [...] | None, deals_status, deals_reason,
 source_breakdown: [{source_group, closed_won_deals, customers, won_revenue,
                     metrics, customers_additive: False}] | None,
 customers_won {status, reason, rows?,                # rows only when published
                # PR-ADS-178C: + the reader contract's own reading (§13.3):
                metric_family, window_basis, dedup_key, window, as_of, cohort,
                customers, closed_won_deals, metric_truth, ...},
 truth_footer, sql_to_customer_rate (unavailable, R_SQL_TO_CUSTOMER_INCOMPATIBLE),
 metric_truth {closed_won_revenue_usd, closed_won_deals, customers,
               google_ads_cohort_sqls}}
```

`build_dashboard_deals(window, now)` = that payload + `surface:
"dashboard_deals"` + `period_change`.

## 5. The page

1. **Command bar**: Read-only / Leads: HubSpot lifecycle / Deals: HubSpot
   Closed-Won · every source chips; the five business windows (Current Quarter
   default); the lead-filter group (Leads scope, Leads source); Refresh; range.
2. **KPI cards**: SQLs · contacts acquired in window (cohort, "through …");
   Closed-won deals; Customers ("Distinct HubSpot companies · never summed");
   Won revenue; Average deal value. Withheld / Unavailable with the reason
   (`canonicalMetricHtml`, View blockers for admins).
3. **Funnel** "Lifecycle funnel · Entered stage in window": five stage
   buttons (cohort-safe conversion chips between them), the Customer stage's
   help text, the truth notes (`leadsTruthStateHtml`), the lead-filter line,
   and the "Disqualified / Other — working status" button.
4. **Leads list**: on a stage click, `/api/crm-funnel/contacts` (names, no
   emails), working-status and search filters, paging, the existing contact
   drawer.
5. **Closed-won deals**: Deal, Closed, Won Revenue, Source, Campaign, Country;
   the "Deal source" filter (each deal's own group, with counts; a note says the
   cards count every deal); the drawer.
6. **Customers won**: the list, or the word and reason with no list.
7. **Truth footer**: one line.

Phone width: tables scroll inside `.deal-scroll`; the command bar wraps;
measured horizontal page overflow is 0 px at 390 and 1440 (screenshots).

## 6. Dashboard → Deals

`services/dashboard_deals_service.py` is now ~100 lines: `build_dashboard_deals`
and `_build_period_change`. Deleted with the composition: `_build_kpis`,
`_build_funnel`, `_build_lead_status_backlog`, `_build_stage_mix`,
`_build_aging_buckets`, `_build_source_breakdown`, `_build_campaign_breakdown`,
`_build_sql_no_deal`, `_canonical_won_contacts`, `_build_decision_cards`,
`_build_truth_status`, `_build_unavailable`, `_rate`, `_source_status`,
`_deal_row`; and `db.revenue_repository.fetch_sql_lead_details`. In app.js:
`renderDealKpiRow`, `renderDealFunnel`, `renderDealLeadStatusBacklog`,
`renderDealAging`, `renderDealSourcePipeline`, `renderDealCampaignPipeline`,
`renderDealTable`, `wireDealDrawers`, `renderDealSqlNoDeal`,
`renderDealTruthFooter`, `DEAL_STATUS_META`, `DEAL_DRAWER_FIELDS`,
`dealRate*`. The tab renders `pipelineKpiCardsHtml` (with deltas),
`pipelineSourceBreakdownHtml`, `pipelineDealsHtml`, `pipelineCustomersWonHtml`,
`pipelineTruthFooterHtml`, and links "Open Pipeline".

### 6.1 Readers of `kpis.lead_status_qualified_no_won_deal`, moved

| Reader | Before | After |
| --- | --- | --- |
| `services/dashboard_deals_service.py` | produced it (`_build_kpis`, `_build_sql_no_deal`, `fetch_sql_lead_details`) | deleted; the payload is `build_pipeline`'s |
| `static/app.js` | `renderDealKpiRow` "notwon" card, `renderDealLeadStatusBacklog`, `renderDealSqlNoDeal` | deleted; the tab renders the Pipeline cards |
| `analysis/sql_doctrine_registry.py` | Dashboard Deals consumer `CLS_MIXED` naming the backlog; `revrepo.sql_details` rule; `deals` rules for 12 symbols; the `_source_status` decision surface | Dashboard Deals `CLS_CANONICAL` (cohort only), rules bound to `build_dashboard_deals` / `_build_period_change`; new "Pipeline page" consumer + `pipeline.*` / `ui.pipeline` rules; `revrepo.sql_details` and the decision surface removed |
| `services/cross_page_parity_service.py` | never read the key; read `kpis.closed_won_revenue_usd` / `closed_won_deals` / `closed_won_customers` / `sqls` on `dashboard/deals` | reads `kpis.<metric>.value` on `dashboard/deals` and on the new `pipeline` consumer |
| `scripts/audit_marketing_outcome_cohorts.py` | excluded the backlog by name (comment) | builds the `pipeline` payload; scans `{value, status, reason}` KPIs |
| `scripts/audit_customer_revenue_reader_cutover.py` | `dashboard_deals_service` row via `load_closed_won_universe`; `fetch_sql_lead_details` an entry point | the row via `build_pipeline`; new `pipeline_service` row; `pipeline` page builder + `pipeline_checks`; the entry-point name stays so a module that brings it back is a reader |
| `services/system_status_service.py` | never read the key; a comment named `/api/dashboard/deals` as the Deals page's read | comment now names the Pipeline page (`/api/pipeline`) |

## 7. Navigation

`PAGES` holds `pipeline` (not `leads`, `deals`, `opportunities`).
`RETIRED_PAGE_REDIRECTS`: `leads → {pipeline, section: "leads"}`, `deals →
{pipeline, section: "deals"}`, `opportunities → {pipeline, section: "leads",
status: "open_working"}`; `navigate()` carries `section` (scroll) and `status`
(the leads list opens on the Lead stage filtered to open_working).
`REVENUE_PAGES`, `PAGE_DATASET_MAP`, `PAGE_EXPLANATIONS`,
`PAGE_HELP_CONTENT`, `PAGE_DEPENDENCIES`, `datasetRelatedPage` name `pipeline`.
Links relabelled "Open Pipeline": the Countries page drawer, the Dashboard
Countries drawer, Dashboard → Revenue's two deal decision cards. **Not
relabelled**: the Campaigns page drawer's `["deals", "Open Deals"]` (177B
code, in 177C's area) — `#/deals` redirects to the Pipeline page's deals
section, so it lands right.

## 8. Not done here (178C)

*PR-ADS-178C did these — see §13 for what was retired, kept and ported.*

* Retire `/api/revenue-deals` (no caller), `/api/crm-funnel` (no page caller),
  `/api/crm-funnel/customers-won` (parity's `leads/customers_won` consumer
  and its 166A tests read it), the mart's `ledger_summary` UI use is gone but
  the payload keeps it; port parity's `leads/customers_won` identity to
  `pipeline` first.
* `renderMartDiagnostics` has no caller now (it joins 177B's
  `renderMartSpendTruth` / `renderMartSummaryStrip`, left for 177C).
* `services/system_status_service.PAGE_PIPELINE_IMPACT` still maps the retired
  `leads` / `opportunities` keys to the legacy `leads` dataset;
  `freshness_service` names `page: "leads"` for that dataset.
* Unused CSS of the deleted Deals-tab panels (`.deal-aging-*`, `.deal-funnel*`,
  `.deal-backlog`).

## 9. Tests

| File | Cases | Kind |
| --- | --- | --- |
| `tests/test_pr_ads_178b_pipeline_one_page.py` | 12 | node-evaluated `app.js` over builder-shaped payloads; static files |
| `tests/test_pr_ads_178b_pipeline_pg_integration.py` | 11 (10 functions) | PostgreSQL, real writers and builders, parity, both audits, the route, the real page |

The PG module is in the CI PostgreSQL step and its did-run list.

### 9.1 Fail-before evidence

Both files copied unchanged onto a `git archive d543dcc` tree: **23 of 23**
cases fail — but only because the feature was missing (no
`services.pipeline_service`, no `/api/pipeline`, no Pipeline page or
renderers: an `ImportError`, a 404 or an undefined function). That proves
nothing about any guard: a case that fails for a missing module would fail
for a wrong one too. *(Wording corrected by PR-ADS-178C, m8: this paragraph
read as if the 23 cases failed because they caught wrong behaviour.)*

What each case is evidence of:

* **Proven by a named mutation** (the table below: the case fails when its
  guard is broken on the finished tree) — one-page 03, 04, 06, 08, 09; PG 02
  (×2), 04, 06, 07, 10.
* **Feature presence only** (no mutation was run; the case shows the feature
  exists and behaves on the fixture, not that it would catch a regression) —
  one-page 01, 02, 05, 07, 10, 11, 12; PG 01, 03, 05, 08, 09.

Named mutations on the finished tree (each makes the listed case fail; run by
a scratch script that restores the file after each):

| Mutation | Fails |
| --- | --- |
| M1 the builder lists rows that do not number the count | PG 10 |
| M2 a lead filter ignored by the funnel (178B-F1 m1: customers won now deliberately ignores it — PG 02 asserts the same list) | PG 02 (×2) |
| M3 the SQL KPI printed around `cohortCountHtml` | one-page 03 |
| M4 `pipelineDealRows` without the 164A count guard | one-page 06 |
| M5 `pipelineCustomersWonRows` without the count check | one-page 08 |
| M6 the reader-cutover audit does not build `pipeline` | PG 06 |
| M7 `pipeline_checks` drops the deal-list membership check | PG 06 |
| M8 Dashboard → Deals builds its own (filtered) figures | PG 04 |
| M9 the cohort audit does not scan `{value, status}` KPIs | PG 07 |
| M10 the funnel labels its SQL stage "SQLs" | one-page 04 |
| M11 the drawer keeps the always-null company field | one-page 09 |

### 9.2 Existing tests changed

Legend: **(r)** the pinned surface is retired or renamed by this PR and the
same property is asserted on its successor; **(b)** broadened — the assertion
holds and now covers more; **(d)** the pinned behaviour is changed by a
decision in §3 and the new behaviour is asserted; **(x)** removed with the code
it pinned (owner decision 4 or the brief's drop list), replaced by an
assertion that the code and its keys are gone. No test is skipped, deselected
or quarantined.

| Test | Kind | Why |
| --- | --- | --- |
| `139` docstring, `_patch_durable` | (x) | `fetch_sql_lead_details` is deleted; nothing patches it |
| `139::test_kpis_closed_won_and_sqls`, `::test_disconnected_integration_blanks_revenue`, `::test_deal_ledger_outage_blanks_deal_proof`, `::test_null_amount_withholds_aggregate_revenue`, `::test_funnel_none_count_is_unavailable_not_no_activity` | (r) | KPIs are `{value, status, reason}` (pipeline shape); a withheld list is `None` with the count's status, never `[]`; the per-source part holding an unpriced deal withholds revenue (was the per-campaign part — the campaign breakdown is deleted). Every value and withholding asserted as before |
| `139::test_closed_won_revenue_only_from_closed_won_deals`, `::test_null_amount_stays_unavailable_not_zero` | (r) | `stage_group` / `status` / `company` were per-row decorations of the deleted `_deal_row`; the rows are the canonical population (ids, stage labels) and an unpriced amount is `None`, rendered "no proven USD amount" |
| `139::test_open_pipeline_is_unavailable_not_revenue`, `::test_closed_lost_is_unavailable_not_negative_revenue`, `::test_opportunity_rates_unavailable`, `::test_pipeline_stage_data_flagged_unavailable`, `::test_aging_buckets_all_unavailable`, `::test_truth_status_keys`, `::test_decision_cards_shape_and_safety`, `::test_open_pipeline_labeled_not_revenue` | (d) | the brief drops the always-Unavailable cards, the aging panel, the legacy decision cards and the chip footer: no open-pipeline / lost / opportunity / aging key carries anything, and the one footer line says why |
| `139::test_rate_never_fake_zero_percent` | (x) | `_rate` is deleted (no rate is computed); the named rate is the unavailable verdict |
| `139::test_source_breakdown_closed_won_no_open_pipeline` | (d) | decision 4 drops the lead-status column; the breakdown is by the deal's own group, through the reader contract — 3 deals / $33,000 as before |
| `139::test_campaign_breakdown_reconciles`, `::test_161e_an_unplaced_google_ads_deal_withholds_per_campaign_figures` | (d) | the campaign breakdown (a second build over the through-today campaign view) is deleted; no per-campaign figure is published here, so none can be a lower bound; the all-source figures are asserted as before |
| `139::test_sql_no_deal_preserved_and_excludes_customers`, `::test_sql_no_deal_unavailable_when_source_down`, `::test_sql_no_deal_unavailable_when_revenue_disconnected`, `::test_sql_no_gclid_is_attribution_review`, `::test_new_repo_fetcher_is_read_only`, `::test_sql_fetcher_status_filter_is_post_dedup` | (x) | owner decision 4: the backlog, its reader and its states are gone; the first and last two now assert no key, no reader, no call |
| `139::test_no_fake_deltas_when_previous_baseline_missing` | (r) | the reason is on `period_change` itself (the `unavailable` list went with the cards) |
| `139::test_no_platform_writes_in_touched_files` | (b) | + `services/pipeline_service.py` |
| `139::_BLOCK_START`, `::test_deals_renders_all_panels`, `::test_deals_reuses_prior_design_system`, `::test_drawer_has_proof_fields`, `::test_deals_admin_gated_links_respect_role` | (r) / (d) | the tab draws the Pipeline renderers; the drawer drops the always-null fields (brief); the one link is gated by `dashCanNavigate` |
| `153c` (25 cases) | (r) | Leads is the Pipeline page: route / nav / window / handler / source and scope wiring / funnel mismatch and cohort-safe conversions are asserted on `loadPipeline`, `pipelineFunnelHtml`, `pipeline-window`; the "page module" is the Pipeline module; its endpoints are `/api/pipeline`, the contacts and operational-status routes; `leadsCount` → the funnel's "Unavailable" |
| `166a::test_01`, `02`, `05`, `06`, `07`, `10`, `11`, `16`, `18`, `23`, `27`, `28`, `31`; `_js_definition`, `_figure` | (r) / (d) | the two customer figures are the funnel's Customer-stage cell and the Customers card + Customers won section (rendered by the real renderers over the same verdicts); exact texts updated. `10`: the cross-response cases (no customers-won payload, another family, another window) cannot arise on one response; the page's own fall-backs remain. `07`: a lead filter makes the LIST unavailable with the 166A reason and never blanks the deal card (decision). `31`: Dashboard → Deals publishes `{value, status, reason}` |
| `164a::_payloads`, `_PROBE`, `_mutated`, `test_10`, `11`, `12`, `15`, `18`, `19`, `20`, `21`, `22`, `23` | (r) | the deal list is driven through the real `loadPipeline` with the real `build_pipeline` payload; the strip's successor is the KPI cards (the GCLID subset is not one of the five — (d)); `20`: the Pipeline builder also withholds the list, so the guard is shown load-bearing against a faulty server sending a "published" empty list beside a stale population (without the guard: "No closed-won deals…"; with it: the reason) |
| `113` (6 cases), `116` (3), `118::test_revenue_pages_hide_global_chrome`, `126` (3), `127` (2), `128` (2), `110` (3) | (r) | `deals` → `pipeline` in `REVENUE_PAGES`; `revenue-deals-range` → `pipeline-range`; `loadDeals` → `loadPipeline`; the deal-list region and states are `pipelineDealsHtml` / `pipelineDealRows` (the 164A guard kept); the KPIs come from the mart deal view server-side, never the attribution total; no legacy endpoint fallback |
| `141::test_evidence_loaders_use_window_param`, `::test_revenue_and_dashboard_keep_business_window_selector`, `::test_campaign_drawer_no_longer_bounds_on_run_date`; `143::test_drawer_lead_detail_bounds_on_contact_created_at_not_run_date` | (r) | the funnel is built server-side on the business window; the slice boundary `fetch_sql_lead_details` (deleted) is the next function, `fetch_won_revenue` — same body |
| `153d::test_gclid_attribution_moves_to_admin`, `153e_b` fixture | (r) / (x) | Pipeline has no strip; `fetch_sql_lead_details` patch removed |
| `154c::_payloads`, `LEGACY_READ_REGISTRY`, `test_f2_2d`, `test_f2_7b`, `test_f2_8` | (r) / (b) | Dashboard → Deals in production shape (`kpis.<m>.value`) and a `pipeline` consumer; the registry entry for the deleted read is removed (the registry test requires it); the identities gain the Pipeline page; a new parametrization mutates it |
| `154c_f3::_REVENUE_PATHS`, `_ALL_SOURCE_CONSUMERS`, `_customers_of`, `test_f3_2`, `test_f3f1_9` | (r) / (b) | same shape change; + `pipeline` |
| `161e::test_13` | (r) | Dashboard → Deals' rate is the Pipeline builder's `_rates()` (unavailable, same reason) |
| `165a::test_18`, `test_19`, `test_20` | (r) / (d) | `deals` is a retired key, `pipeline` the live page; no Pipeline strip; `test_20`: the audit now completes over a tree missing an inventoried file and records `inventory_complete` False (161E test_26 runs it on a base tree predating `pipeline_service.py`) instead of crashing first |
| `162f::EXPECTED`, `test_03`; `162g::test_09`; `174a::EXPECTED_ITEMS`, `EXPECTED_ITEM_FOR_PAGE`, `test_06`, `test_07`, `test_09`, `test_11`, `test_13`; `sidebar_navigation_structure`; `help_drawer`; `empty_state` (4); `dataset_freshness_ui_contract` | (r) | Pipeline is one page (13 pages, no strip); `leads`/`deals` are redirects; the registries name `pipeline`; `174a::test_09`'s broken-table mutation empties the Pipeline item |
| `168b::test_04`, `test_15` | (r) | Dashboard → Deals no longer calls `build_revenue_by_source`; 16 → 10 Channels links (its six decision-card links went with the cards; every remaining one carries the label) |
| `177a::test_03d` (deals case), `test_03f` | (x) | `_source_status` / `_build_campaign_breakdown` are deleted; no Deals row is judged on, or drops, an SQL count |
| `177a::test_06e`, `test_06f` | (r) | the Deals SQL figure is `pipelineKpiCardsHtml` (through the gate); `DEAL_STATUS_META` is gone |
| `177a_f1::test_m1_b`, `test_n1_a`, `test_n2` | (r) | the Deals KPI row is `pipelineKpiCardsHtml` (as-of note, "through" suffix, gated delta) |
| `177a_f1::test_m2_a`, `test_m2_b`, `test_m2_c`; `177a_f2::test_g` | (x) / (r) | decision 4 removes the backlog: no legacy count, no "Qualified (lead status)" block, and the deal blocks carry no SQL word; under a withheld cohort the SQL card says Withheld and the reason |
| `177a_f1::test_n3`; `177a_pg::test_04` | (b) | the cohort audit builds one more surface, `pipeline` |

## 10. Verification run (fast mode)

Static: `python3.11 -m compileall -q analysis api connectors db scheduler
services scripts tests`, `node --check static/app.js`, `git diff --check
d543dcc`. Modules run: see the PR report (every touched / added module and the
CI PostgreSQL-step modules importing a changed module).

## 11. PR-ADS-178B-F1 — truth audit round 1

One Major, no blockers; fixed in place on the same branch.

| Finding | Fix (file:line at F1) | Guard |
| --- | --- | --- |
| **MAJOR-1** the working-status breakdown called `leadsCount`, deleted by 178B: "Loading…" forever, an unhandled rejection, and `loadPipeline` threw on a window or filter change with that view selected | `static/app.js` `leadsCount` restored before `leadsRenderOperational` — null/undefined renders the word "Unavailable", never 0 and never a dash | `f1::test_01_a/b/c` (node, awaited: the table renders its counts; `loadPipeline` resolves with the view selected); `f1::test_02` every function `app.js` calls is defined (`test_02_b`: the scan names a deleted function); `153c::test_frontend_renders_unavailable_scope_as_dash_never_zero` restored to pin `leadsCount` and its call site |
| m1 customers won withheld under a lead filter that does not touch it | `services/pipeline_service.py` `_customers_won` (no `lead_filtered` branch) | PG `test_02`: narrowed `customers_won` == whole; one-page `test_08` |
| m2 A1 the KPI half of `pipeline_checks` survived being set to `True` | — (guard existed) | `f1::test_03_a` (×3), `test_03_b` |
| m2 A2 `period_change` survived building the current window as "previous" | — | `f1::test_04` (×4): the previous window's bounds, like for like |
| m2 A3 / m3 the average's deal-count agreement survived removal; it compared `closed_won_deals`, not the divisor `deal_count`; a mismatch said `closed_won_verdict_missing` | `pipeline_service._average_deal_value`: compares `deal_count` AND `closed_won_deals` with the published deals; a mismatch is `average_deal_value_population_mismatch` (labelled in words) | `f1::test_05`, `test_05_b` |
| m4 raw reason codes and HubSpot property names in the funnel's text | `leadsTruthStateHtml` maps reasons through `canonicalReasonLabel`; the stage's property name moved into a `title` ("HubSpot stage-entry date" shown) | `f1::test_06` |
| m5 the SQL card and funnel did not say which population a lead filter narrows | `pipelineKpiCardsHtml` note under `filters.lead_filtered`; funnel note "a different population (all sources …)" | `f1::test_07` |
| o1 (pre-existing) under `reconciliation.status = partial` a stage with undated entries showed a plain number that is a lower bound | `pipelineFunnelLowerBounds` + `pipelineFunnelHtml`: "at least N" with the reason; every stage when the payload cannot say which (the builder now passes `coverage.stage_reached_without_entry_date`) | `f1::test_08` |

Fail-before: `tests/test_pr_ads_178b_f1_truth_audit_round1.py` copied onto a
`git archive 4d088cd` tree (re-run by PR-ADS-178C, m8 — 11 fail, 9 pass):

* **fail because the behaviour was wrong** — MAJOR-1 `01_a`, `01_b`, `01_c`,
  `02` (the deleted `leadsCount` is called); m4 `06` (a raw reason code on the
  page); m5 `07` (no note under a lead filter); o1 `08` (an exact-looking
  lower bound); and F2's `09` (an exact rate beside a lower bound);
* **fail only because a name was missing** — `02_b` (its probe text is not in
  the older `app.js`) and m3 `05`, `05_b` (`REASON_AVERAGE_POPULATION_MISMATCH`
  does not exist there). m3's behaviour is proven by mutation A3 below, not by
  this run;
* the three m2 counterfactuals pass there (the guards existed) and fail only
  under their named mutation on the F1 tree:

| Mutation | Fails |
| --- | --- |
| A1 the KPI conditions of `pipeline_checks` replaced by `True` | f1 03_a (×3), 03_b |
| A2 `_build_period_change` builds `(window, now)` instead of the previous reference | f1 04 (×4) |
| A3 the deal-count agreement removed from `_average_deal_value` | f1 05 |
| M-MAJOR `leadsCount` deleted again | f1 01_a, 01_b, 01_c, 02, 02_b; 153c |

Existing tests changed in F1: `153c::test_frontend_renders_unavailable_scope_as_dash_never_zero`
(178B had rewritten it to the funnel only; F1 restores its `leadsCount`
intent with the word in place of the dash, and keeps the funnel half);
`178b_pg::test_02` and `178b_one_page::test_08` (m1: the list is the same
under a lead filter — a behaviour change, not a weakening; the renderer
still says a carried reason in words); `166a::test_07` (comment only).

Carried, not fixed here (follow-ups):

* **m6** the deal-source filter's state (`_pipelineDealSource`) is shared by
  the Pipeline page and Dashboard → Deals.
* **m7** 166A's customer cases reach the Pipeline renderers through an
  adapter (`_pipeline_from`) rather than a real `build_pipeline` payload.
* **m8** §9.1's fail-before wording ("fail on d543dcc") does not say which
  cases fail for a missing module versus a wrong behaviour.

## 12. Truth audit round 2 (PR-ADS-178B-F2)

Round 2 found no Blocking or Major issue. It found one figure-affecting
Minor, fixed here:

* **The problem.** The conversion % next to a lower-bound stage was still
  shown as an exact number. `cohort_conversion` counts a contact as converted
  only when the contact has a date for the TO stage. So when the TO stage
  has undated contacts, the converted count is too low, and the rate shown
  is lower than the real one.
* **The fix.** `pipelineFunnelHtml` now handles two cases:
  * **TO stage is a lower bound:** the rate reads "at least X%", with the
    reason in a tooltip.
  * **FROM stage is a lower bound:** the cohort itself is incomplete, so the
    rate is unknown. It reads "—", with the reason in a tooltip.
* **Tests.** `test_09_a` (partial evidence, one undated stage) and
  `test_09_b` (reconciled evidence keeps exact rates). `test_09_a` fails on
  2904d7f (`'0%' == 'at least 0%'`).

Two observations are carried, not fixed:

* **Coverage is not split by attribution scope.** Under a narrow Scope this
  over-marks: a stage can be marked as a lower bound when it is complete for
  that scope. It never under-marks. *(Fixed in PR-ADS-178C, §13.5: the funnel
  now counts the gap per scope.)*
* **Contacts with an unrecognised lifecycle stage are not counted as
  undated.** That gap is shown in words in the partial-evidence banner.

## 13. PR-ADS-178C — the legacy Leads / Deals feeds retired

On main `ea6a7ae` (178B + 177C). Read-only throughout: no Google Ads or
HubSpot write is added or removed. The precedent is 176C (`docs/58` §8) and
177C (`docs/60` §9): every truth property a retired surface's tests assert is
first asserted on a live surface; a property the live surfaces did not hold
is a finding, fixed — never pinned "as observed".

### 13.1 What was retired, and what was kept

| Route | Callers on `ea6a7ae` | Decision |
| --- | --- | --- |
| `GET /api/revenue-deals` | none (no page, no audit; `build_revenue_deals` is called by the mart deal view and Overview directly) | **retired** — answers 404 |
| `GET /api/crm-funnel/customers-won` | the cross-page parity audit (`leads/customers_won`, via `build_customers_won`) and the 166A tests | **retired after porting** (§13.3) — answers 404 |
| `GET /api/dashboard/deals` | `static/app.js` `loadDashboardDeals` (Dashboard → Deals); the cohort audit and parity build `build_dashboard_deals` directly | **kept** — the tab reads it. Its docstring now describes what it serves |
| `GET /api/crm-funnel` | none in `static/` | **not in this PR's brief; left as is** (§13.9) |

Deleted with them:

| Kind | Deleted |
| --- | --- |
| Routes / handlers | `api.server.get_revenue_deals`, `api.server.api_crm_funnel_customers_won` and their docstring lines |
| Builders only they used | `canonical_crm_funnel_service.build_customers_won`, `_build_customers_won`, `_cw_envelope`, `_cw_unavailable`, `_cw_parity_blocks`, `_cw_canonical_window`, `REASON_WINDOW_NOT_EXPRESSIBLE`, `REASON_FILTER_NOT_APPLICABLE`. **Moved** (not deleted) into `services/pipeline_service.py`: `_cw_metric` → `_reader_verdict`, `_won_customers_block` → `_won_customers_list`, the `CUSTOMERS_WON_*` / `COMPANY_*` constants. The funnel service reads no closed-won deal any more |
| Parity | `CONSUMER_LEADS_CUSTOMERS_WON` (`leads/customers_won`) — replaced by `pipeline/customers_won` |
| Audit inventory | the reader-cutover `INVENTORY` row for `services/canonical_crm_funnel_service.py` (it is no reader now; the Pipeline row names the moved reading) |
| UI (178B's dead code) | `renderMartDiagnostics` (no caller since 178B); the four "Customers won" refusal labels only the retired route / Leads page sent (`customers_won_needs_a_business_window`, `…_not_split_by_contact_source`, `…_unreadable`, `…_window_mismatch`); the global `_pipelineDealSource` (§13.4) |
| Registries | `system_status_service.PAGE_PIPELINE_IMPACT["leads"]` and `["opportunities"]` (retired page keys on the legacy `leads` snapshot); `freshness_service` `leads.page = "leads"` |
| CSS | the Deals-tab panels' rules (`.deal-kpi-grid`, `.deal-dot*`, `.deal-status`, `.deal-funnel*`, `.deal-aging-*`, `.deal-campaign-panel`, `.deal-sql-panel`, `.deal-review-note`, `.deal-drawer__actions`, `.deal-drawer__link`, the `.deal-kpi-grid` media rules); the retired Leads page's `.leads-customer-figures`, `.leads-customer-figure`, `.leads-won-customers`; `.revenue-diagnostics` / `.revenue-diagnostic` (with `renderMartDiagnostics`). Every one was drawn only by code 178B deleted (checked against `07be622`) |

Changed so they name nothing retired: the server and `/api/pipeline`
docstrings; `db/revenue_repository.fetch_revenue_deals`' docstring; the
`sql_doctrine_registry` `api.crm_funnel` rule (the retired handler's symbol
removed; the inventory still reports **0 unknown**: 12 canonical / 11 legacy
/ 6 mixed); `docs/API_CONTRACT.md` (a retired-route note).

**Registries repointed, not just trimmed.** The Pipeline page does not read
the legacy `leads` snapshot (`hubspot/contacts`): its funnel and lead list
read the canonical contact funnel, which `PAGE_DATASET_MAP.pipeline` already
names (`hubspot/contact_funnel`). So `PAGE_PIPELINE_IMPACT["pipeline"] =
["contact_funnel", "lifecycle_events"]`, those two freshness datasets name
`page: "pipeline"`, and the legacy `leads` dataset names **no** page (as the
legacy `deals` snapshot has since 163B). Repointing `leads` itself to
`pipeline` would have declared a dependency the page does not have.

**Static scan.** No production code (AST names in `api`, `services`, `db`,
`analysis`, `scripts`, `scheduler`, `connectors`; `app.js` without comments;
`styles.css` without comments) names a deleted symbol, route request, reason
label or class — `test_02`, `test_05` of the retired module. The names remain
only in comments that record the retirement, and in the audit's own
`RETIRED_READER_*` lists. 178B-F1 `test_02` (every function `app.js` calls is
defined) passes.

### 13.2 The guard, and its runtime twin

`tests/test_pr_ads_178c_leads_deals_feeds_retired.py` `test_01` (the running
app answers 404 on both retired routes, signed in or not, and 401 — served,
gated — on `/api/dashboard/deals`, `/api/pipeline` and the two live
`/api/crm-funnel/*` rows), `test_02` (no decorator registers them, no
production module names a deleted symbol, no page requests them; the funnel
service reads no closed-won deal).

The runtime twin is in the enforcement layer: check 21
`retired_readers_absent` of `scripts/audit_customer_revenue_reader_cutover.py`
(`retired_reader_checks`, `RETIRED_READER_ROUTES`, `RETIRED_READER_SYMBOLS`),
which runs in the production certification (`audit_platform_certification`
`AUDITS`). It reads the server's route decorators by AST (a docstring naming
a path is not a route), `app.js` requests, and every production module's code
names. `test_03` shows it red for a re-registered route (either path), a page
request and a module bringing `build_customers_won` back, and green for a
comment that names them.

### 13.3 Port table

"Live" = `build_pipeline` (`/api/pipeline`, the Pipeline page), and Dashboard
→ Deals (`build_dashboard_deals`, which republishes it). 166A cases are
`tests/test_pr_ads_166a_leads_page_names_and_customers.py`; "port n" is
`tests/test_pr_ads_178c_leads_deals_port_pg_integration.py`; "retired n" is
the non-PG 178C module.

| Property asserted on the retired surface (source) | Where it holds now | Status |
| --- | --- | --- |
| Parity: the customers / closed-won-deals identities have ONE consumer that reads the closed-won reader contract directly, not the mart (166A review m4, `leads/customers_won`, 166A `31`) | `pipeline/customers_won`: `build_pipeline`'s `customers_won` block carries `load_won_deals`' own verdicts, window and deal-ledger provenance, read in the builder's snapshot; parity reads the block off the Pipeline payload (`_pipeline_customers_won_reading`) — no extra build. 166A `31`, port `01` | **ported** |
| …and that leg can disagree with the mart (otherwise it is no check) | port `02`: a wrong reader verdict (block side) and a wrong mart summary (every republishing page) each make `consumer_values_differ` | **added** (the 166A suite never showed it failing) |
| Customers won = distinct companies, never the deal count, never the stage-move count, reads nothing from the funnel (166A `02`, `03`) | the block (`_customers_won`, `_customers_won_reading`, `_reader_verdict`: no funnel token); the reading is unchanged when the funnel says 219 or 0 — 166A `03` | ported (behaviour added) |
| Metric family / window basis / dedup key / close-date cohort / window bounds on the payload (166A `04`) | the same fields on the block; the window block's key is `key` (the parity signature field), was `window_key` | ported |
| Withheld / unavailable / stale / no-companies: the reason, no number, no list (166A `05`, `06`, `27`) | the block's verdicts and `status`/`reason`; the Customers card; the whole page payload carries no company name or id when withheld — 166A `05`, `06`, `27` | ported |
| A malformed or missing reader verdict fails closed; a withheld value loses its number (166A `09`) | `_reader_verdict`, through the real builder — 166A `09` | ported |
| The list only beside a published count it numbers; names and their four states; names unavailable when the store is (166A `18`, `27`, `28`) | `_won_customers_list` / `_customers_won`, through the real builder; the client guard (`pipelineCustomersWonRows`) over faulty-server payloads — 166A `18`, `27`, `28` | ported |
| Only admins get "View blockers" from the customer figure (166A `11`) | the Pipeline Customers card over the real payload — 166A `11` | ported |
| The reader is inventoried; the audit fails without its row (166A `12`) | the reading moved, so the Pipeline row is the one that must stand: removing it fails `inventory_complete` naming `pipeline_service`; the funnel service is no reader — 166A `12` | ported |
| The customers-won route is gated like the contact rows (166A `13`) | `/api/pipeline` and `/api/crm-funnel/contacts` answer 401 unauthenticated; the retired route 404 — 166A `13`, retired `01` | ported |
| A non-business window is refused, never approximated (166A `08`) | `build_pipeline` raises `ValueError` (route 400) before any ledger read — 166A `08` | ported |
| Under a Scope / Source filter: no customers-won figure, no ledger read (166A `07`) | **not ported — reversed by the 178B-F1 m1 owner decision**: the lead filters narrow the funnel only; customers won is the same all-source list, and the SQL card / funnel say the populations differ. 166A `07` asserts the decided behaviour (kind d) | superseded by decision |
| `/api/revenue-deals`: business windows only, Current Quarter default, no day window (113 `test_endpoint_registered_business_window_only`) | `/api/pipeline`'s route (same defaults) and `build_revenue_deals` (still rejects a day window, 113 `test_service_rejects_invalid_window`) | ported |
| `/api/revenue-deals` among the live sibling routes (165A `17`) | `/api/pipeline` and `/api/dashboard/deals` in its place | ported |
| `/api/revenue-deals`' payload properties (164A population verdict, ledger summary, withheld = no rows) | not route properties: `build_revenue_deals` is unchanged and feeds the mart deal view, asserted by 113 / 164A / 178B on the live Pipeline payload | held (no change) |

**Finding fixed while porting.** None of the 166A cases showed the
independent leg failing; a parity consumer that republished the mart would
have passed all of them. Port `02` is the missing counterfactual, and named
mutation M2 (§13.7) — the block republishing the card instead of reading the
reader — turns both of its cases red.

### 13.4 m6 — the deal-source filter, per surface, from the payload

`static/app.js`: `_pipelineDealSourceBySurface` (≈ line 6579) replaces the
one global; `pipelineDealSurface` (≈ 6946: `surface: "dashboard_deals"` is
Dashboard → Deals, anything else the Pipeline page), `pipelineDealSourceFor` /
`pipelineSetDealSource` (≈ 6956 / 6962: each selection remembers the window
it was made in), `pipelineDealSourceOptions` (≈ 6973), used by
`pipelineDealsHtml` (≈ 6987) and the change handler in `pipelineWireDeals`
(≈ 7098).

* **Per surface.** A group picked on Dashboard → Deals never filters the
  Pipeline page, nor the reverse; the two selects have distinct ids.
* **Reset on a window change — chosen.** A group that exists in one quarter
  may not in the next, and a page that opens filtered by a choice made for
  another period shows fewer deals than its cards count with only a small
  note to say so. Same window (a refresh, a lead-filter change) keeps it.
* **Counts from the payload.** The options read `source_breakdown` (each
  group's `metrics.closed_won_deals`, the reader contract through
  `subset_metrics`) and the "All" option the card's published deal count. A
  group whose count is not published reads "count not published"; with no
  breakdown no filter is offered. The renderer counts no rows.

Tests: retired `06` (surfaces, window), `07` (counts are the payload's even
when they differ from the rows; unpublished; none).

### 13.5 The round-2 observation — lower bounds per attribution scope

Cheap, so done rather than disclosed: `build_populations` already knew each
contact's scope membership when it counted an undated stage reach, so it now
also files `coverage.stage_reached_without_entry_date_by_scope`
(`services/canonical_crm_funnel_service.py` ≈ line 426, filled ≈ 492,
published ≈ 513) — per stage, per scope; a narrow scope's figure is `None`
(unknown, never 0) when campaign identity could not be consulted or a
contact's membership is unknown. `pipeline_service._funnel_coverage` (≈ line
362, used ≈ 414) passes the SELECTED scope's figures (and names the scope);
an unknown figure reaches the page as `null`, which `pipelineFunnelLowerBounds`
already reads as a bound. A funnel with no per-scope figure passes the
all-source one (over-marking: it fails safe). `reconciliation.status` is
unchanged (it still says `partial` for the whole funnel).

Tests: retired `08` (the per-scope counts, nesting, `None` under an identity
outage), `09` (the selection, the fall-backs and the rendered marks); port
`05` on real tables: an ORGANIC contact at SQL with no SQL date marks SQL a
lower bound under All Sources but not under Google Ads-source, where the
Google Ads contact's own Opportunity gap stays marked; nothing is marked
under Keyword-attributable.

### 13.6 m7 and m8

* **m7.** `tests/test_pr_ads_166a_leads_page_names_and_customers.py` no
  longer has `_pipeline_from`. Its customer cases call the real builder
  (`_pipeline` ≈ line 104, `_won` ≈ 127) over the same ledger fixtures and
  render its real payload. The one substituted input is the funnel slice
  (`crm_funnel.build` returns the 166A slice — or, in the database cases, the
  real funnel's own payload). Non-database cases first apply PR-ADS-125's
  coherent durable dataset (`_ledger(durable=True)`) so the mart deal view
  runs without a database; the database cases read the real tables. Renderer
  cases for states no builder sends (a verdict without a status, no block, a
  withheld count carrying a list, a published list that does not number its
  count) use builder-shaped payloads (`_shaped` ≈ 258), each named as a
  faulty-server state. No assertion is weakened: every value, status, reason
  and rendered text is asserted as before; `03` gains a behavioural check,
  `05` / `27` / `28` assert the card and the whole page payload too.
* **m8.** §9.1 now says the 23 cases fail on `d543dcc` because the feature
  was missing, and lists which cases a named mutation proves and which are
  feature-presence only. §11's fail-before was re-run on `4d088cd` and now
  separates the cases that failed for wrong behaviour from those that failed
  for a missing name (m3's two cases — proven by mutation A3).

### 13.7 Tests

New:

| File | Cases | Kind |
| --- | --- | --- |
| `tests/test_pr_ads_178c_leads_deals_feeds_retired.py` | 9 | running app (404s), AST, the audit's check 21 and its mutations, registries, static scans, node-evaluated `app.js` |
| `tests/test_pr_ads_178c_leads_deals_port_pg_integration.py` | 6 (5 functions) | PostgreSQL, real writers / builders / parity / reader-cutover audit / route / page |

The PG module is in the CI PostgreSQL step and its did-run list. No module
was deleted.

**Fail-before.** Both copied onto a `git archive ea6a7ae` tree: **15 of 15**
fail.

* **Wrong behaviour on `ea6a7ae`:** retired `01` (`/api/revenue-deals` is a
  route), `04` (`PAGE_PIPELINE_IMPACT` holds `leads`, `opportunities`), `05`
  (`renderMartDiagnostics` present), `07` (the dropdown says "Organic (1)",
  a row count, where the payload says 4); port `03` (`/api/revenue-deals`
  answers 200).
* **Missing feature only** (proven by the mutations below instead): retired
  `02` (`_registered_routes`), `03` (`RETIRED_READER_ROUTES`), `06`
  (`pipelineSetDealSource`), `08` / `09` (no per-scope coverage); port `01`,
  `02` (no `pipeline/customers_won`), `04` (no check 21), `05` (no per-scope
  coverage).

Named mutations on the finished tree (a scratch script on a copy of the
tree; each restored after its run; each makes every listed case fail, for the stated reason):

| Mutation | Fails |
| --- | --- |
| M1 parity builds no `pipeline/customers_won` | port 01, 02 ×2 (`registered_consumer_not_built`, never a pass) |
| M2 the block republishes the card instead of reading the reader | port 02 ×2 (block and pipeline agree: the leg is gone) |
| M3 `structural_checks` skips check 21 | retired 03, port 04 |
| M4 the route scan reads no decorator | retired 03 |
| M5 one filter for both surfaces | retired 06 |
| M6 the selection survives a window change | retired 06 |
| M7 the dropdown counts rows again | retired 07 |
| M8 the builder passes the whole-population coverage | retired 09, port 05 |
| M9 per-scope gaps counted without membership | retired 08, port 05 |
| M10 the customers-won route re-registered | retired 01, 02, 03; port 03 |
| M11 a Deals-tab CSS rule comes back | retired 05 |
| M12 the `leads` impact entry comes back | retired 04 |

### 13.8 Existing tests changed

Legend as §9.2: **(r)** the pinned surface is retired and the same property
is asserted on its successor; **(b)** broadened; **(d)** behaviour changed by
a recorded decision, the new behaviour asserted; **(x)** removed with the code
it pinned, replaced by an assertion that it is gone; **(f)** fixture completed
to production's shape. Nothing is skipped, deselected, quarantined or
weakened.

| Test | Kind | Why |
| --- | --- | --- |
| `113::test_endpoint_registered_business_window_only` | (r) | the route is retired; the same window contract is asserted on `/api/pipeline`, and its absence |
| `126::test_all_revenue_pages_share_mart_renderers` | (x) | `renderMartDiagnostics` is deleted (no caller); asserted absent. The loop's diagnostics assertion was unreachable (both listed pages `continue` before it) and the dead `renderRevenueDealsPage` branch is removed; an unlisted page now raises instead of passing silently |
| `126::test_parity_css_present` | (x) | the diagnostics CSS is deleted with its renderer; asserted absent; the parity panel's CSS still asserted |
| `139::test_deals_responsive_classes_exist` | (r) / (x) | the tab draws the Pipeline panels; their classes asserted styled and drawn; the deleted panels' classes asserted gone |
| `154c::_pipeline_payload`, `_customers_won_block`, `_payloads`, `test_f2_2d` | (r) | `leads/customers_won` → `pipeline/customers_won`, the block of the same pipeline payload (production's shape: `_build_consumers` derives it) |
| `165a::test_17` | (r) / (b) | `/api/pipeline` and `/api/dashboard/deals` in place of `/api/revenue-deals`, which is asserted absent |
| `166a` (module docstring; helpers `_pipeline`, `_won`, `_stable`, `_shaped`, `_ledger(durable)`; `_pipeline_from` deleted) | m7 | §13.6 |
| `166a::02`, `03`, `04`, `05`, `06`, `09`, `11`, `18`, `23`, `28` | (r) / (b) | the real builder's block and page in place of the route's payload; same values, reasons and texts; `03` gains the funnel-independence check |
| `166a::07` (renamed `…_narrows_the_funnel_and_never_the_customers_won_figure`) | (d) | 178B-F1 m1: the filter refusal is reversed by decision; the same list under every filter, the figures kept apart |
| `166a::08` | (r) | the builder refuses a non-business window before any ledger read |
| `166a::10` | (r) | the zero and moves-unknown cases from the real builder; no-verdict / no-block as faulty-server payloads |
| `166a::12` | (r) | the reading's inventory row is `pipeline_service`'s |
| `166a::13` | (r) / (x) | `/api/pipeline` gated like the contact rows; the retired route 404 |
| `166a::27` | (r) / (d) | the real builder; the last case (a lead filter) asserts the decided same-list behaviour |
| `166a::31` | (r) | the parity consumer is `pipeline/customers_won`; every assertion kept |
| `178b_one_page::_payload` (`_breakdown`) | (f) | the builder always sends `source_breakdown` beside a published list; the filter reads it (m6) |
| `178b_one_page::test_07` | (r) | the selection is set through `pipelineSetDealSource` (per surface, per window) |
| `178b_one_page::test_08` | (r) | the carried-reason case uses a reason the builder sends (the 166A refusal label is deleted) |
| `178b_pg::test_02`, `test_04` | (f) | the block carries provenance; compared with its build instant aside |
| `178b_pg::test_03` | (b) | withheld: the block's own reader verdict says the same status and reason |

### 13.9 Not done here

* **`GET /api/crm-funnel`** has no page caller (178B §8 listed it). The brief
  for this PR named three routes and not this one, so it is left. At least
  `153c` inspects its handler, so retiring it needs its own port.
* `db/revenue_repository.fetch_revenue_deals` (legacy `gclid_attribution`
  read) has no production caller; it stays as one of the reader-cutover
  audit's entry points. Removing it is its own change.
* `system_status_service.PIPELINE_DEPENDENCIES["leads"]["page"]` still says
  "Lead Quality" (a page retired in 153C) as the label of the legacy leads
  pipeline; and `datasetRelatedPage('hubspot', 'contacts')` sends the legacy
  contacts dataset to the Pipeline page. Both are labels, not dependencies;
  left for a System Status pass.
* Unused CSS from earlier retirements (Campaign Evidence, keyword / n-gram /
  quality badges — about 40 classes) is outside this PR's Deals-tab scope.
