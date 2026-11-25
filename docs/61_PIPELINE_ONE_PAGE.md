# 61 — One Pipeline page: leads and deals together

**PR-ADS-178B** (this PR) builds one Pipeline page in the Dashboard design on
one builder, `build_pipeline`, and points Dashboard → Deals at the same
builder. **PR-ADS-178C** retires the old routes after porting what parity and
the audits still read from them (§8).

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
  deal figure and the cohort KPI are the window's whatever they are. Under
  either, customers won is `unavailable` /
  `customers_won_not_split_by_contact_source` (the 166A reason); the
  Customers card is a deal figure and is not blanked. The page labels the
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
 customers_won {status, reason, rows?},               # rows only when published
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
cases fail (no `services.pipeline_service`, no `/api/pipeline`, no Pipeline
page or renderers).

Named mutations on the finished tree (each makes the listed case fail; run by
a scratch script that restores the file after each):

| Mutation | Fails |
| --- | --- |
| M1 the builder lists rows that do not number the count | PG 10 |
| M2 customers won ignores a lead filter | PG 02 (×2) |
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
