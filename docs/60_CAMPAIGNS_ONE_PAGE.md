# 60 — One Campaigns page, Meta ad sets beside it

Series doc for the Campaigns merge (`docs/59` is 177A). **PR-ADS-177B** (this
PR) builds the one Campaigns page in the Dashboard design, on the mart's
SETTLED campaign view, and adds the separate "Meta Ads — ad sets" table.
**PR-ADS-177C** retires Campaign Evidence's endpoint and renderers after porting
what parity and certification read from them. **PR-ADS-177D** merges Keywords
and Search Terms.

Depends on: PR-ADS-177A (`b1cc9e9`, tree = `5a3fe64`). Unblocks: 177C, 177D.

---

## 1. The problem

Campaigns had two pages under one menu item:

| | Campaign Evidence (`#/campaigns`) | Campaign Revenue & Spend (`#/roas-campaigns`) |
| --- | --- | --- |
| Reader | `/api/campaigns` → `campaign_evidence_service` | `/api/revenue-performance?view=campaign` → the mart |
| Windows | evidence windows (7d…all time), ending today | business windows, ending today |
| Spend gate | not coverage-gated | coverage-gated |
| Columns | junk, junk rate, CPQL | the `derive_*` period ratios |
| Key | `campaign_id` | normalised campaign name |

Dashboard → Campaigns read the mart too, but recomputed its own period ratio
(`_period_ratio`: campaign-attributable revenue over **full-account** spend),
grouped all-source deals by label for its drawer (`_group_deals_by_campaign`,
so a drawer could disagree with its row) and showed keyword themes from the
legacy, non-windowed `keywords` snapshot. Nothing read the Meta ad-set
breakdown (`meta_ads_adset_truth_service`, PR-ADS-171C).

## 2. What this PR does

* One page, `campaigns`, titled "Campaigns", in the Dashboard design (command
  bar with the business windows, KPI cards, campaign table, drawer, the Meta
  table, truth footer). It reads **only** the mart's settled campaign view and
  sums nothing.
* `#/roas-campaigns` is retired (a redirect to `campaigns`).
* The mart gains a **settled campaign view**, asked for explicitly
  (`settled=True` / `?settled=true`), read only by the Campaigns page and
  Dashboard → Campaigns (§4).
* Dashboard → Campaigns reads the same settled view; its page-side ratio, its
  label-grouped drawer, its all-source "Unattributed / Needs Review" row and its
  keyword-theme panel are deleted.
* `GET /api/meta-ads/adsets` and the "Meta Ads — ad sets" table (§6).

## 3. Decisions

**Owner decisions, 2026-11-24** (recorded in `docs/59` §3; repeated here with
what this PR did with each):

1. **One SQL count everywhere: the acquisition cohort.** Done in 177A. Leads and
   SQLs on this page are that cohort (`cohortCountHtml`, the as-of note and the
   "through" suffix).
2. **Business windows** — Current Quarter by default, then Last Quarter, Last
   6 Months, YTD and All time — **ending yesterday, as Countries does**, so a
   figure never includes today's partial day. Built here (§4).
3. **Keywords & Search Terms become one combined page on evidence windows.**
   PR-ADS-177D, not this PR: the `keywords` and `search-terms` pages are
   unchanged; the menu item is `["campaigns", "keywords", "search-terms"]`.
4. **Meta ad sets go in a separate "Meta Ads — ad sets" table.** Each ad set is
   a row treated as its own campaign; the table has its own totals, is never
   summed with Google, and its SQL, deal and revenue cells read "Not linked
   yet". Built here (§6).

**Coordinator decision on decision 2 (option B, 2026-11-24).** Decision 2 is
about the Campaigns page itself. The 2026-11-20 decision that every other
revenue page keeps its window stands. So **only the Campaigns page and
Dashboard → Campaigns read a settled campaign view**; Overview, Revenue, Deals,
Channels and the Closed-Won Deals page keep the campaign and deal views through
today. The trade-off that led there is in §4.1.

**PR-ADS-177B decisions** (readings of standing doctrine):

* **Lead-quality columns dropped** (Junk, Junk rate, CPQL) — consistent with
  Countries' decision 3 (`docs/58` §3). The Leads page owns lead quality.
* **The residual row.** The mart's campaign view counts campaign-attributable
  deals only, so there is no deal residual. One row, pinned last, labelled
  **"No campaign label"**, holds the cohort's `campaign_residual` leads and
  SQLs; its spend, deal, customer, revenue and ratio cells read "Not
  campaign-attributable" with a one-line reason — never "—", never 0, never in a
  total. Leads / SQLs KPIs = Σ rows + residual = the cohort total (conservation,
  as Countries).
* **Keyword themes: dropped** (the smaller honest option). A windowed panel over
  `keyword_daily_facts` would need its own coverage, currency and settle rules;
  the Keywords page already serves that evidence, and 177D merges it. The tab's
  search-term signals stay (window-scoped, over the settled days).
* **Dashboard → Campaigns rows carry the reader-contract ratio.** With
  `_period_ratio` deleted, a row's Period Revenue / Ad Spend is the mart row's
  `derive_period_revenue_per_ad_spend`, with `ratio_kind` and the disclosure.
  Verified spend over a published zero revenue is therefore a measured `0.00x`
  (as on the Campaigns and Countries pages), where the tab's own division
  returned `None`. Both campaign surfaces now print the same figure for the same
  row (§8 notes the CLAUDE.md sentence this makes stale).
* **The strict spend gate behaves as on Countries.** Below
  `readiness.spend_decision_ready` the page shows a card naming the gap
  (`campaign_spend_unsettled_reason` / coverage / FX) and no figure.
* **The drawer fails closed.** It shows the deal list only when its ids equal
  the row's `deal_ids`; otherwise it says it does not match and shows nothing.
* **Campaign Evidence's page UI is replaced.** `/api/campaigns`,
  `/api/campaign-detail` and `build_campaign_evidence` stay (parity's
  `cohort_sql_parity` and the certification audits read them); their JS
  renderers draw on no page now and stay until 177C retires them with the
  endpoint (their 161B tests still exercise the shared SQL gate they hold).

## 4. The settled campaign view

`revenue_decision_mart.build_revenue_decision_mart(view, window, now, *,
settled=False)`. `SETTLED_ON_REQUEST_VIEWS = {"campaign"}`; a settled deal view
is a `ValueError` (400). `SETTLED_DAY_VIEWS` is unchanged (`{"country"}`).

| Part | Rule | Where |
| --- | --- | --- |
| Window | an open window ends yesterday (`resolve_settled_window`); `window.through_reason` / `window_range` declare it with the 167A sentence ("… Other revenue pages, such as Deals, already count deals closed today.") | `build_revenue_attribution(settled_days=True)` |
| Spend | the **settle rule** over the campaign-spend ledger: yesterday publishes only once the morning sync has re-read it (`analyze_settled_coverage`, via `revenue_spend_truth_service._settled_verdict` — the rule `build_google_ads_spend_truth_for_days(settled=True)` applies). Unsettled → coverage incomplete, `spend_truth.campaign_spend_unsettled_reason` | `canonical_campaign_spend_block(settled=True)`, `build_revenue_attribution(settled_spend=True)` |
| Ledger | deals, customers and revenue from `load_won_deals(bounds=settled_revenue_bounds(…))`, inside the mart's `@single_snapshot` | unchanged 167A mechanism |
| Cohort | leads / SQLs over the same settled days | unchanged 177A mechanism |
| Rows | campaign rows carry `deal_ids` (None when the deal count is withheld, never `[]`) | `_build_db_rows` (all campaign rows) |
| Page blocks | `campaign_totals` (the KPIs: the mart's own campaign-attributable verdicts, `{value, status, reason}`; ratio = `derive_period_revenue_per_ad_spend` via the reader contract) and `campaign_residual_row` | `_campaign_page_blocks` (campaign view only) |
| First day | no settled day → every figure `no_settled_day_in_window`; every unpublished `metric_truth` contract names that reason (`canonical_contract.name_no_settled_day`) | mart, Dashboard → Campaigns |

The drawer: `build_campaign_deal_details(…, settled_days=True)` /
`/api/revenue-performance/campaign-detail?settled=true` reads the same bounds,
and now also the row's account scope and "not Google Ads" exclusions (the
country drawer's PR-ADS-176A fix; the campaign drawer read an unscoped account
and no exclusions, so an excluded label's deal was listed under it). It returns
`deal_ids`.

### 4.1 Why only these two pages (the trade-off)

Switching the mart's campaign view to settled days for every consumer is one
line, but each Dashboard page composes the mart with its own reads that still
run through today in the same response — Overview: `build_revenue_by_source`,
`build_revenue_deals`, the daily spend series; Revenue: `load_won_deals`, the
lead series; Deals: `load_won_deals`, `fetch_sql_lead_details`; the mart's deal
view rows (`build_revenue_deals`). Moving the mart alone splits each page across
two windows; an experiment that did so failed the reader-cutover audit's
`windows_reconcile` (mart 7 deals vs canonical 8) and 167A's "a deal closed
today is on the Deals page" contract. Moving them all would reverse the
2026-11-20 decision. Option B keeps that decision and gives the Campaigns page
its own settled view.

### 4.2 Effects

| Surface | Window | Change |
| --- | --- | --- |
| Campaigns page | ends yesterday | new |
| Dashboard → Campaigns | ends yesterday (was today) | **figures move by today's deals / contacts / spend**; the tab says so (`windowThroughNote`) and shows the no-settled-day card on a window's first day |
| Overview, Revenue, Deals, Channels, Closed-Won Deals | through today | unchanged |
| Revenue Health page audit | Campaigns compared with the mart over the SAME settled days (`roas_by_campaign` row, label "Campaigns", window reported) | as Countries since 167A |

177A's "through" disclosures are unchanged in mechanism: every cohort label
states its own last day, so the Campaigns page reads "through <yesterday>" and
Overview "through <today>".

### 4.3 Parity groups

* **Settled group** (`cohort_sql_parity`): Countries page, Dashboard Countries,
  Campaign Evidence over the settled days — **plus** the Campaigns page (Σ rows
  + residual row) and Dashboard → Campaigns (`kpis.sqls`).
* **Today group** (`cohort_sql_parity.campaign_view`): the mart's campaign view,
  the Overview headline, Campaign Evidence over those days. Unchanged.
* **Identities.** Dashboard → Campaigns left the through-today identities
  (`google_ads_spend_usd`, `campaign_attributed_*`, `google_ads_cohort_sqls`).
  New `*__settled_days` identities compare it with the new consumer
  `revenue_decision_mart/campaign_settled` (`contract_metric`: the key both file
  provenance under; `proof_consumer`: coverage proven over the settled days). The
  first-day exemption (177A-F3, the audit's own `resolve_settled_window`) covers
  them because both consumers name `no_settled_day_in_window`.
* `scripts/audit_marketing_outcome_cohorts.py` builds `mart/campaign_settled`
  too, scans `campaign_totals` / `campaign_residual_row` for withheld exposures,
  and checks residual row = `cohort.campaign_residual` and totals = cohort.

## 5. The page

1. **Command bar**: Read-only / spend / revenue chips, the five business
   windows (Current Quarter default), the settled-range chip with the 167A
   disclosure.
2. **Gates**, in order: no settled day → the 167A card only; revenue not
   decision-ready → the revenue card; spend not decision-ready → the spend-gate
   card naming the gap and its next step (`COUNTRY_GAP_NEXT_STEP`). No figure
   under any of them. The Meta table is shown under the revenue / spend cards
   (it has its own gate), not under the no-settled-day card.
3. **KPI cards** from `campaign_totals`: Campaign Spend, Leads, SQLs (with
   "incl. N with no campaign label" and "through …"), Won Deals, Customers
   (distinct companies, "not a sum of the rows"), Won Revenue, Period Revenue /
   Ad Spend (with its disclosure).
4. **Table**: Campaign, Spend, Leads, SQLs, Won Deals, Customers, Won Revenue,
   Period Revenue / Ad Spend, Status; decision-filter chips; the residual row
   pinned last.
5. **Drawer**: the row's figures, then its closed-won deals (§3).
6. **Meta Ads — ad sets** (§6).
7. **Truth footer**: campaign spend (settled days), FX, revenue scope, the Meta
   separation, read-only.

Phone width: the tables scroll inside `.camp-scroll`; measured horizontal page
overflow is 0 px at 390, 1440 (screenshots).

## 6. The Meta endpoint contract

`GET /api/meta-ads/adsets?window=<business window>` (auth required; GET only;
no call to Meta) → `services/meta_ads_adset_table_service.build_meta_adset_table`:

```
{status: published | withheld | unavailable, reason, detail,
 read_only, scope: "meta_ads_adsets", never_summed_with: "google_ads_campaigns",
 window {key, label, start_date, end_date, through_reason, window_range},
 rows: [{account_id, account_name, time_zone, campaign_id, campaign_name,
         adset_id, adset_name, first_day, last_day,
         spend {value, status, reason, currency},      # account currency
         spend_usd {…, currency: "USD"},               # the truth service's FX rule
         impressions, clicks, meta_reported_leads,     # {value, status, reason}
         attribution_settings,
         hubspot_outcomes {hubspot_sql_contacts, hubspot_closed_won_deals,
                           hubspot_won_revenue: {value: null, status: "not_linked",
                           reason: "meta_adset_not_linked_to_hubspot"}}}],
 totals: {adsets, spend, spend_usd, impressions, clicks, meta_reported_leads,
          hubspot_outcomes, never_summed_with} | null,
 outcome_link_status/reason/detail, meta_reported_leads_disclosure,
 accounts, time_zones, mixed_time_zones, mixed_currencies,
 settled_rule_applied, native_lead_action_type,
 meta_reported_leads_attribution, config_problems,
 owner_declared_retired, owner_declared_retired_disclosure}
```

* Days: the business window's settled days; the truth service decides each
  account's required days in **its own time zone**, requires every account's
  ad-set ledger verified AND settled, and the ad-set rows to reconcile with the
  campaign rows. Anything short → `withheld` with the reason and detail, `rows`
  empty, `totals` null (withheld means absent). No settled day →
  `no_settled_day_in_window`.
* Rows are ad sets keyed by ids; names are labels (`names_are_labels`).
* Totals are the table's own: Σ rows, published only when every row's figure
  is (else unavailable with the first blocking ad set); native spend only in
  one currency (`meta_accounts_use_different_currencies`); Meta-reported leads
  only under one attribution setting (`meta_attribution_settings_differ`).
  Never added to any Google figure, KPI or ratio; no ratio over Meta spend; no
  ROAS / CAC key or label.
* Registered: `scripts/audit_customer_revenue_reader_cutover.py`
  `META_ADSET_PAGES` (a module that calls the breakdown and is not registered is
  a violation; it must carry `owner_declared_retired_disclosure`), and the JS
  gate `metaAdsetTableGate` (withholds a table resting on the owner's
  retirement declaration without its disclosure) that `renderMetaAdsetTable`
  must call. The module publishes no HubSpot figure, so the SQL-doctrine
  inventory has nothing to classify in it (its outcome keys say
  `hubspot_sql_contacts`, never a count).

## 7. Tests

New (all fail on `5a3fe64`; `docs/60` §7.1):

| File | Cases | Kind |
| --- | --- | --- |
| `tests/test_pr_ads_177b_campaigns_one_page.py` | 14 | node-evaluated `app.js`, static files, audit structure |
| `tests/test_pr_ads_177b_settled_campaign_view.py` | 6 | mock fixtures, real builders, real parity audit |
| `tests/test_pr_ads_177b_campaigns_pg_integration.py` | 5 | PostgreSQL, real writers and builders, real page |
| `tests/test_pr_ads_177b_meta_adsets_pg_integration.py` | 7 | fake Meta Graph API → real sync/backfill → PostgreSQL; pure totals; the route |

Both PG modules are registered in the CI PostgreSQL step and its did-run list.

### 7.1 Fail-before evidence

The four files copied unchanged onto a `git archive 5a3fe64` tree: **25 of 25**
cases in the first three files fail, and the Meta module fails to import
(`services.meta_ads_adset_table_service` does not exist) — all 7 cases error.
One-page case 06 also carries its own mutation (the page's spend-gate check
replaced by `if (false)` prints the withheld spend).

Named mutations on the finished tree (each makes the listed cases fail):

| Mutation | Fails |
| --- | --- |
| M1 settled view without the settle rule (`settled_spend=False`) | PG 03 |
| M2 the drawer ignores the settled bounds (`bounds=None`) | PG 02 |
| M3 Dashboard → Campaigns reads the through-today view | PG 04, 05 |
| M4 campaign rows carry `deal_ids = []` | PG 02, 04 |
| M5 first-day contracts do not name `no_settled_day_in_window` | settled 04 (×2), 05 |
| M6 the residual row is not rendered | one-page 04 |
| M7 the drawer's id-equality check removed | one-page 09 |
| M8 Meta totals sum what is published, skipping the rest | Meta 02, 05 |
| M9 `metaAdsetTableGate` ignores a missing retirement disclosure | one-page 11, 12 |

The today-vs-settled split is PG 01: a deal closed today is on Overview
(`kpis.closed_won_deals` 2) and Deals, not on the Campaigns page (1), whose
range chip says why.

### 7.2 Existing tests changed

Legend: **(r)** the pinned surface is retired or renamed by this PR and the
same property is asserted on its successor; **(b)** broadened — the original
assertion holds and now covers more; **(d)** the pinned behaviour is changed by
a decision in §3 / option B and the new behaviour is asserted; **(f)** a
fixture completed to production's shape. No test is skipped, deselected or
weakened to pass.

| Test | Kind | Why |
| --- | --- | --- |
| `177a::test_06e` | (b) | renderRoasCampaignTable is deleted with its page; the sweep now covers the five new Campaigns renderers (page table, KPI summary, residual row, drawer, Dashboard residual row) — wider, same assertion |
| `165a_weekly::test_18`, `::test_19` | (r) | `roas-campaigns` is not in `PAGES` and has no loader case or strip tab: it is a retired key (redirect to Campaigns). The "old bookmark lands on the Dashboard" guard for `unit-economics` is unchanged |
| `162h::test_19` | (r) | `"roas-campaigns"` has no `PAGE_DEPENDENCIES` entry; Campaigns still depends on `canonical_spend` (asserted) |
| `162g::test_05`, `::test_07` | (r) | the campaign cards and the biggest-waste signal open `campaigns` / "Open Campaigns" (the page that holds the evidence now); still asserts every target is a real page, and now that none is `roas-campaigns` |
| `162g::test_08`, `::test_09` | (r) | `roas-campaigns` is a retired key, not a page (now also asserts its redirect); 15 → 14 distinct pages; strip `[campaigns, keywords, search-terms]` |
| `162f::01`, `::03`; `174a` (`EXPECTED_ITEMS`, 06, 07) | (r) | the Campaigns item is `[campaigns, keywords, search-terms]`; 15 → 14 pages; no "Campaign Revenue & Spend" tab; dashboard targets include `campaigns` and now never `roas-campaigns` (added). `roas-campaigns` still maps to the Campaigns item (a retired key) |
| `sidebar_navigation_structure` (route keys, labels, revenue section, page containers) | (r) | `roas-campaigns` is a redirect, not a route key or container; the strip is asserted exactly; "Keywords" (which survived only in the deleted "Campaigns & Keywords" tab label) → the Keywords page's own "Keyword Evidence" |
| `help_drawer_and_snapshot_history` (page list, campaign help) | (r) | the campaign-revenue help is the `campaigns` entry; the HubSpot / "does NOT use Google Ads conversion value" assertions unchanged |
| `empty_state_page_explanations::test_explanation_containers_exist` | (r) | `campaigns` is a clean business-window page with no inline explanation container (the ROAS / Countries precedent) |
| `141::test_evidence_pages_list_and_helper`, `::test_revenue_and_dashboard_keep_business_window_selector` | (r) | owner decision 2: Campaigns left `EVIDENCE_PAGES` and joined `REVENUE_PAGES`; both directions asserted |
| `143::test_campaign_freshness_uses_canonical_spend_and_hubspot`, `::test_help_content_has_no_verdict_instructions` | (r) | the page's dependencies are deals + canonical spend + the contact funnel (the evidence-page map line is unchanged and still asserted); the help is the new page's — the no-verdict-terms assertion is unchanged, Campaign Evidence's "Outcome Status" wording is replaced by the page's own not-zero / not-summed / not-added statements |
| `094` (2 cases), `107a` (7), `108` (3), `113::test_campaign_and_country_pages_unchanged`, `115::test_single_readiness_contract_shared`, `124::test_roas_countries_is_a_revenue_page`, `129::test_frontend_campaign_coverage_copy`, `153e_b::test_18f` | (r) | id / function renames only (`roas-campaigns-window` → `campaigns-window`, `page-roas-campaigns` → `page-campaigns`, `loadRoasCampaigns` → `loadCampaignsPage`, `renderRoasCampaignsPage` → `renderCampaignsPage()`, `renderRoasCampaignTable` → `renderCampaignsPageTable`); assertions unchanged. `107a::test_roas_loaders_use_revenue_attribution_endpoint` now pins `view=campaign&settled=true` (still the mart); the subtitle is the new page's; the verdict column is "Status" (the brief's column list) |
| `110` (7 cases) | (r) | `REVENUE_PAGES` lists `campaigns` for `roas-campaigns`; the blocked → empty → table order is asserted on the new renderers; the verdict column is "Status" on Campaigns, "Decision" on Countries (unchanged) |
| `111` (whole module) | (r) | it pinned Campaign Revenue & Spend's renderers (PR-ADS-111 built that page). Each case keeps its property on the one Campaigns page: self-contained, KPI cards (the strip's successor) with deals ≠ customers and no ROAS / CAC label, decision chips, business-value-first sort, blocked guard before summary/table (now also before the spend gate), safe empty state, columns, `fmtPeriodRatio` via `rowMetricHtml`, header/window. Leads / SQLs headers carry the cohort "through" suffix, so they are matched as `>Leads<span` |
| `112::test_campaign_page_not_modified` | (r) | the country page still does not reach into the campaign page's state / renderers, now named `campaignsPage*` |
| `116` (5 cases) | (r) | renames; the loader awaits two reads in one `Promise.all`, so "the range is cleared before the first await" uses the first `await` of any kind (the same boundary); own payload / source health / safe empty text are the new page's |
| `118::test_revenue_pages_hide_global_chrome`, `::test_spend_truth_panel_and_gates_present` | (r) | `campaigns` hides the global bar; the spend gate is the STRICT gate (Countries' rule, §3) — asserted to return before any KPI. Campaign Revenue & Spend showed diagnostic spend under a notice; that page is gone |
| `122::test_roas_page_still_blocks_on_incomplete_coverage` | (r) | `spendCoverageIncomplete` (a notice beside diagnostic spend) left with Campaign Revenue & Spend; the Campaigns page blocks strictly — the gate is asserted to return before the KPIs and table, and the card's text is asserted. The no-"ROAS unavailable" assertion is unchanged |
| `119::test_native_gbp_never_formatted_as_usd`, `120::test_campaign_table_spend_state_ui` | (r) | the page prints USD only past its gate (which requires verified FX); the Dashboard tab's native cell keeps `fmtCompactCurrency(…, native_currency)` — the never-"$"-for-GBP property is asserted where native spend is still printed |
| `123` (4 cases) | (r) | the page uses the Dashboard table (`camp-scroll`, the 176B Countries precedent): same containment (horizontal scroll inside, `min-width: 0` on the panel); compact KPI spend via `fmtMoney`; exact figures in the spend-proof modal opened from the drawer |
| `126` (3 cases), `128` (3 cases) | (r) | the loader reads the mart's settled campaign view (still the mart; no legacy fallback); the page uses its own truth footer over `spend_truth` and the strict gate instead of the diagnostic spend card |
| `130::test_frontend_has_expandable_campaign_row`, `::test_drawer_renders_client_deal_table` | (r) | a row opens the Dashboard drawer (176B pattern) instead of an inline expand row; it lazy-loads `campaign-detail?settled=true`; the drawer title / empty text are the new page's, and the empty text is shown only when the row's own `deal_ids` is empty |
| `134` (4 cases) | (r) | campaign signals / decision cards open `campaigns` / "Open Campaigns"; the tab label is "Campaigns" (its keyword panel is dropped); now also asserts `roas-campaigns` is never a target |
| `137::COVERAGE_COMPLETE`, `138::COVERAGE_COMPLETE` | (f) | the settled view holds spend to the settle rule, which needs each chunk's `fetched_at` — production stamps it, and the 138 geo fixture already carried it. Without it no day can settle |
| `137::test_campaign_period_ratio_only_when_spend_and_revenue_safe` | (d) | the tab's own `_period_ratio` is deleted (brief); its row ratio is the mart row's reader-contract `derive_period_revenue_per_ad_spend`: verified spend with a published ZERO revenue is a measured `0.0` with `ratio_kind` and the disclosure (as on the Campaigns and Countries pages). Was `None`. See §8 |
| `137::test_unattributed_revenue_preserved_and_excluded_from_period_ratio`, `::test_attribution_status_preserves_mapping_distinction`, `::test_truth_status_keys` | (d) | the all-source "Unattributed / Needs Review" row is deleted (option B, 5): the deal on no campaign is on no row, KPI or ratio numerator (it stays on Overview / Deals); the residual is the "No campaign label" row (leads / SQLs only, other cells `not_applicable`). Campaign attribution is `ready` with every row mapped; a mapping-unavailable row still makes it `partial` (unchanged case) |
| `137::test_mapping_unavailable_makes_campaign_attribution_partial`, `::test_unavailable_card_copy_accurate_for_healthy_window`, `::test_keyword_themes_never_fake_outcomes_or_period_ratio`, `::test_keyword_snapshot_unavailable_degrades`, `::test_keyword_and_search_panels_state_read_only`, `::test_campaigns_error_and_loading_states_exist` | (d) | the keyword-theme panel is dropped (§3): no `keyword_themes`, no `keyword_attribution` key, the snapshot is never read (a raising reader proves it), the renderer is gone; the unavailable card flags search-term attribution; the error card is "Campaigns unavailable" |
| `154c::test_f1_9`, `::test_f2_7`, `::test_f2_8`, `::test_f2_8b`; `154c::_payloads` | (b) | Dashboard → Campaigns moved to the five `*__settled_days` identities with the new consumer `revenue_decision_mart/campaign_settled` (§4.3): the contract is read under `contract_metric`; 20 → 25 identities; every consumer is still mutated and must break its own identity (two new parametrizations for the settled reference) |
| `154c_f3::test_f3_2` | (b) | `dashboard/campaigns` left the through-today SQL identity; asserted in the settled identity, still without Countries |
| `156::test_28c` | (d) | the tab no longer reads `fetch_keyword_theme_snapshot` (the panel is dropped). The docstring named this moment: the legacy `keywords` writes now have no in-product reader (§8). Asserts no service reads it |
| `161e::test_33` | (r) | the tab's all-source unattributed row (which held the label-less deal) is gone, so the injected fallback row is a whole campaign row of the tab's shape; the leak under test (proof beside a withheld count) is unchanged and still turns the audit red |
| `171c::test_31` | (d) | owner decision 4 gives the ad-set breakdown its first page. Still: no route reads the truth module directly (the route wraps the one registered reader), `index.html` holds no figure, and `app.js` reads `meta_reported_leads` only inside `renderMetaAdsetTable`, through its gate |
| `177a_f1::test_n3`, `177a_pg::test_04`, `::test_06`, `::test_06c` | (b) | the audits build and compare two more surfaces (`mart/campaign_settled`; readings `campaigns_page`, `dashboard_campaigns`); every existing surface and value unchanged |

## 8. Not done here

* **177C**: retire `/api/campaigns`, `/api/campaign-detail`,
  `campaign_evidence_service` and Campaign Evidence's JS renderers after
  porting what `cohort_sql_parity` and certification read from them; the
  `scripts/screenshot_campaign_evidence.py` script and its fixtures go with
  them. `renderMartSpendTruth` / `renderMartSummaryStrip` (mart client
  renderers no page calls now) and `repo.fetch_keyword_theme_snapshot` (no
  caller now) go there too.
* **177D**: Keywords & Search Terms, one page.
* The mart's other views keep today (§4.1); a settled-everywhere move is an
  owner decision, not a mechanical change.
* Meta ad sets are not linked to HubSpot contacts or deals.
* **The legacy `keywords` snapshot has no in-product reader now.** The
  Dashboard tab's keyword-theme panel was its last one (`156::test_28c`'s
  docstring named this moment); only the orphan `/api/keywords` reads it. The
  scheduler still writes it. Stopping those writes is its own decision, not
  taken here.
* **CLAUDE.md's reader-contract landmine** says the Campaigns, Countries and
  Channels dashboard rows "divide the same published inputs themselves and
  carry no metadata … return None, not 0, for zero revenue". Dashboard →
  Campaigns rows now carry the mart row's reader-contract ratio (§3), so that
  sentence no longer holds for Campaigns. CLAUDE.md is not edited in this PR;
  the owner should update it.

## 9. PR-ADS-177C — the old campaign feed retired; one Keywords & Search Terms page

Depends on PR-ADS-177B (`d543dcc`). Read-only throughout: no Google Ads or
HubSpot write is added; the POST `/api/keyword-evidence/refresh` and
`/api/search-term-evidence/review` routes are unchanged (they write only our
own database). The one write this PR *stops* is the monthly job's write of the
legacy `keywords` snapshot — our own table, never an external system (§9.6).

### 9.1 What is deleted

| Kind | Deleted |
| --- | --- |
| Routes (each answers **404**) | `GET /api/campaigns`, `/api/campaign-detail`, `/api/keywords`, `/api/search-terms`, `/api/search-terms/summary`, `/api/search-terms/ngrams`, `/api/revenue-attribution` (its `/audit` sub-route stays) |
| Service | `services/campaign_evidence_service.py` (whole) |
| Helpers only they used | `api/server.py`: `_campaign_keyword_preview`, `_campaign_flagged_preview`, `_build_campaign_detail`, `WINDOW_NOT_RESOLVED`, `_round2`, `_SEARCH_TERMS_*`, `_WASTE_STATE_ALIASES`, `_resolve_waste_state_param`, `_encode/_decode_search_terms_cursor`, `_canonical_search_term_scope`, `_search_term_scope_unavailable`. `keyword_evidence_service.build_campaign_keyword_preview` and `search_term_evidence_service.build_campaign_flagged_preview` with their private helpers. `db/revenue_repository.fetch_keyword_theme_snapshot`, `fetch_campaign_lead_detail`; `db/writers.write_keywords` |
| Scripts | `scripts/audit_campaign_evidence_certification.py` (replaced, §9.4), `scripts/screenshot_campaign_evidence.py`, `scripts/campaign_evidence_fixtures.json`, `scripts/benchmark_ngrams.py` |
| UI | the Campaign Evidence page/drawer JS (~1,700 lines: renderers, drawer, n-gram drawer, keyword prefill, `campaignAttribution*` state, `renderMartSpendTruth`, `martMetricHtml`, `renderMartSummaryStrip`, `renderKeywordShell`, the `loadCampaigns` alias); `#campaign-drawer` and `#page-keywords` markup |
| Tests | `test_pr_ads_143_campaign_evidence_window.py`, `test_pr_ads_157_campaign_evidence_certification.py`, `test_pr_ads_157_f1_population_reconciliation.py`, `test_revenue_attribution_api.py` — each property they held is ported first (§9.3) |

Moved before deleting: `ACCOUNT_TZ` (callers import `analysis.account_time`),
`_window_bounds` → `analysis.evidence_windows.evidence_window_bounds`
(unchanged), `_account_today` → `analysis.account_time.account_today`; the
campaign-ID resolver (`_spend_by_campaign_id` / `_identity_index` /
`_assign_lead`) → `services/campaign_label_resolver.py`, unchanged, which
`canonical_customer_revenue_service.campaign_resolver` (≈ line 152) now
imports. `scripts/verify_search_terms_pipeline.py` reads
`/api/search-term-evidence` instead of `/api/search-terms`.

**Guard.** `tests/test_pr_ads_177c_campaign_feed_retired.py` (cases 01–07):
the running app answers 404 on all seven routes; no handler or decorator
defines them; the files are gone and nothing imports them; the UI requests
none. Its runtime twin is the certification's S1 (§9.4), which case 05 shows
going red when a retired route is reintroduced in a copy of the tree. All 16
cases fail on `d543dcc` (§9.8).

### 9.2 Parity before and after

**Before.** Both `cohort_sql_parity` groups had one independent leg:
Campaign Evidence (`build_campaign_evidence(bounds=…)`), the only consumer that
placed contacts by campaign ID. Every other reading republishes the mart, which
places contacts with one name-keyed resolver (`_mart_label_resolver`).
Removing Campaign Evidence would have left the *today* group comparing the mart
with the Overview — which republishes the mart's `summary.sqls`, so it could
never fail — and the settled group checking only the country / campaign
partitions against each other, never the mart's Google Ads / excluded split.

**After.** `campaign_id_cohort` replaces `campaign_evidence` in both groups:
`campaign_label_resolver.google_ads_cohort_sqls_by_campaign_id(start, end)`
reads the canonical contact funnel through the cohort service's one population
rule (`build_window_outcomes`) but resolves labels with the campaign-ID rule
(approved identity mappings first, then exactly one spend id per normalised
name, never fuzzy), bound to the window's canonical spend **of the configured
account**. It is None unless the cohort verdict is `published` (withheld ⇒
`not_evaluable`, never a pass). No group lost an independent comparison:

| Group | Readings before | Readings after |
| --- | --- | --- |
| settled (`cohort_sql_parity`) | countries_page, dashboard_countries, campaigns_page, dashboard_campaigns, **campaign_evidence** | same four + **campaign_id_cohort** |
| today (`campaign_view`) | mart_campaign_view, dashboard_overview, **campaign_evidence** | mart_campaign_view, dashboard_overview, **campaign_id_cohort** |

Proof it can fail: `177c_port_pg::test_01` makes the mart's resolver exclude
`brand - uk`; every mart-fed reading drops to 2, the independent one stays 4,
and **both** groups go `broken` with `cohort_sql_parity_broken`.

**M8 — the settled identities.** The five `*__settled_days` identities compared
Dashboard → Campaigns with the settled mart it republishes, so a wrong settled
figure broke nothing. A third consumer, `canonical/campaign_settled`
(`cross_page_parity_service.independent_settled_campaign_reading`, ≈ line 1106),
reads each figure without the mart over the same settled days: spend from the
settled spend truth (`build_google_ads_spend_truth_for_days(settled=True)`,
only when verified), deals / customers / revenue from the reader contract
(`load_won_deals(bounds=settled_revenue_bounds)` → `reader_row` over the
campaign-attributable deals), SQLs from the campaign-ID reading — each with its
own `metric_truth` contract naming its source. `test_02` inflates the settled
mart's campaign deals (Dashboard republishes the same wrong figure); only the
independent reading disagrees, and the identity is a `mismatch`.

### 9.3 Port table (Campaign Evidence → live surfaces)

"Live" = the settled campaign view (`/api/revenue-performance?view=campaign&settled=true`,
the Campaigns page), Dashboard → Campaigns, and the campaign drawer
(`/api/revenue-performance/campaign-detail?settled=true`).

| Property asserted on Campaign Evidence (source) | Where it holds now | Status |
| --- | --- | --- |
| Withheld cohort ⇒ no lead / SQL count, breakdown or CPQL anywhere (161B, 157 §9–10) | `mart_withheld_exposures` now also scans `cohort.breakdown` and the lifecycle counts; cohort audit `[campaigns:*] withheld_not_exposed`; cert L2 | held |
| Cohort breakdown (google_ads / unattributed / excluded_non_google, reasons) published beside the count (161B) | new `cohort.breakdown` on the mart's cohort block (published only; None when withheld) | **added** (finding) |
| Lifecycle-event SQLs never relabelled as cohort SQLs (161B) | `cohort.lifecycle_event_coverage` skeleton with counts withheld, `not_read_on_this_surface`; cohort audit `lifecycle_events_not_relabelled` | held |
| Membership / SQL proof / dedup against an independent SQL (161B audit) | `audit_marketing_outcome_cohorts.audit_campaign_cohort` on `mart/campaign_settled`, every business window; `177c_port_pg::test_05` + 161B `test_22` | held |
| Σ rows + residual = Google Ads total; totals = breakdown (157 §11, 157-F1) | cohort audit `bucket_reconciliation`, cert L3 | held |
| Two campaign ids sharing a display name keep distinct identities / never merged (143, 157 §13–14) | settled view: a name spent by >1 id carries `google_ads_campaign_ids`, `identity_status = campaign_name_shared_by_google_ads_campaigns`, `verdict = mapping_required`; Dashboard → Campaigns sums both ids under that row and marks it Mapping review (it let the last id overwrite the first); cert L5; `test_03` | **fixed** (finding) |
| Campaign identity unique (157 §12) | cert L4 | held |
| Approved alias places its contact on the campaign; approved not-Google-Ads label leaves the Google Ads total under its reason; unmapped label keeps its row; lead-only label's spend unavailable, never $0 (143) | `test_04` on the settled view | held |
| Totals' spend = canonical campaign spend over the same days (143) | `test_04` | held |
| No CPQL (157 §10; dropped by 177B) | cert L8, cohort audit `no_cpql_published` | asserted absent |
| Drawer deals = the row's own population (new in 177B / 177C — **not** a port of 157 §4–8, which certified the drawer's keyword / flagged preview sections; those are retired with the previews, row below) | cert L6 (every row's drawer population, read once per window by `campaign_deal_details_by_campaign`, lists exactly the row's `deal_ids`; drawers probed recorded; exit 2 when canonical revenue is unavailable — §9.11); `test_06` mutation adds a deal ⇒ violation | held (new check) |
| Account scope applied in SQL; empty candidate list selects nothing (157 §16–17) | cert S3 | held |
| No legacy `keywords` / `waste_terms` reader or metric (157 §2–3) | cert S2; `legacy_source_guard.scan_legacy_sources() == []` | held |
| Frontend gates every SQL figure on its own response's cohort (157 §15, 161B) | cert S4 (`cohortCountHtml(…, cohort)` on all six campaign surfaces; spend gate before any figure) | held |
| Dashboard → Campaigns per-campaign spend is the configured account's | `fetch_canonical_campaign_spend(…, customer_id=configured_customer_id())`; `test_08` | **fixed** (M3, finding) |
| Raw search terms / summary / n-grams: canonical rows only, filters bind, totals not doubled, fail closed with no account (156-F3 13–15b) | ported to `/api/search-term-evidence` and `/patterns` / `/flagged` over PostgreSQL | held; **fixed** the reason (below) |
| `closed_won_not_published` (157-era "deals are not customers on CE") | not ported: the Campaigns page publishes deals and customers through the reader contract, certified by `audit_customer_revenue_reader_cutover` (now incl. the settled view, M2) | superseded |
| Drawer keyword / flagged preview sections (157 §4–8, 18–19) | not ported: the previews are deleted with the drawer; the Keywords & Search Terms tabs carry that evidence on their own routes | retired |

Finding fixed on the search-term routes: with no configured account the live
routes answered `db_unavailable: true` but dropped the scope's reason, which
the retired `/api/search-terms*` routes stated. `_build_population_in_snapshot`
now keeps it and the Terms, Flagged and Patterns payloads carry
`unavailable_reason` (`google_ads_customer_not_configured`) — `156_f3::test_15b`.

### 9.4 The certification port

`scripts/audit_campaigns_page_certification.py` replaces
`audit_campaign_evidence_certification` in `audit_platform_certification.AUDITS`
(key `campaigns`). Same exit contract (0 / 1 / 2), read-only, no external call.
157's checks map as: 1 → L1; 2, 3 → S2; 9, 10 → L2 / L8; 11 → L3;
12 → L4; 13, 14 → L5; 15 → S4; 16, 17 → S3; 4–8 and 18, 19 → **retired**
with the drawer's keyword / flagged previews (they certified those sections,
which no longer exist); 20 → the "no writes" guarantee. New: S1 (the
retirement holds), L6 (each drawer lists exactly its row's deals — a new
check, not a port of 4–8) and L7 (no spend figure over unsettled spend, M4). **Strength check:**
the gate refuses to certify an unreachable database — `init_pool()` does not
raise without `DATABASE_URL` and every builder then returns an empty payload
that would pass every per-window check vacuously, so `database_reachable()`
runs a `SELECT 1` first and exits 2 (found by `161g::test_15`, which runs the
real audits with no database); an `unavailable` cohort (a failed funnel read)
is exit 2, never a pass.

### 9.5 Keywords & Search Terms — one page

`search-terms` is the one page, titled "Keywords & Search Terms", in the
Dashboard design (page header, `dash-command-bar` chips, window chip), with
tabs **Terms, Flagged, Patterns, Keywords**. The Keyword Evidence module renders
unchanged into the Keywords tab (`loadKeywordsTab` → `#kw-tab-body`); its table
takes the shared `data-table` style. `NAV_ITEMS.campaigns = ["campaigns",
"search-terms"]`; `RETIRED_PAGE_REDIRECTS.keywords = {page: "search-terms",
tab: "keywords"}`; links read "Open Keywords & Search Terms" / "Review in
Keywords & Search Terms"; the Keywords help, page dependencies
(`keyword_facts`) and system-status impact moved onto `search-terms`. A phone
rule keeps the four tabs and the freshness chip inside a 390 px page.

### 9.6 The legacy `keywords` snapshot

Nothing reads it now: its last reader, the orphan `/api/keywords`, is deleted.
So `scheduler/monthly.py` no longer calls `write_keywords` (deleted) and no
longer opens a `keywords` sync batch; `sync_keyword_daily_facts` (what the
Keywords tab reads) still runs monthly in its own `try`, failing as step
`sync_keyword_daily_facts`. The table and its rows stay in the schema;
`("google_ads_api", "keywords")` joins `freshness_service.RETIRED_DATASETS`
and leaves `MONTHLY_REFRESH_DATASETS`, the freshness config and System
Status. This is our own table only — no Google Ads or HubSpot write existed
here or is added.

### 9.7 Minors (177B review) and O1

| # | Fix | Where |
| --- | --- | --- |
| M1 | Before the morning sync the Scale card says the spend is not shown yet and why (`_spend_reason_text`), never "with verified Google Ads spend"; the unavailable card's headline is "Spend not settled". PG `test_07` uses `_before_the_morning_sync` | `services/dashboard_campaigns_service.py:453` (`_SPEND_REASON_TEXT`), `:472` (`_build_decision_cards`), `:602`, `:673` (`_build_unavailable`) |
| M2 | Reader-cutover audit builds the settled view and `/api/meta-ads/adsets`; `settled_mart_checks` reconciles the settled summary with the reader contract over the same settled days | `scripts/audit_customer_revenue_reader_cutover.py:887` (`SETTLED_DAY_PAGES`), `:904`, `:1217`–`:1262` |
| M3 | Dashboard → Campaigns spend read scoped to `configured_customer_id()` | `services/dashboard_campaigns_service.py:834` |
| M4 | Unsettled spend: settled view nulls row / total / summary spend, USD spend and period ratios (native kept only when FX is the sole gap), and turns winner / watch / waste / learning into `spend_unknown` with the reason | `services/revenue_decision_mart.py:733`, call at `:1262` |
| M5 | Stale "not yet meaningful … never 0.00x" text removed (Campaigns and, after O1, Countries); "Spend not settled" in `CAMP_STATUS_META` / `CAMP_NEXT_ACTION` | `static/app.js:4828`, `:5209`; `services/dashboard_campaigns_service.py:620`; `services/dashboard_countries_service.py:1121`, `:1213` |
| M6 | `renderRevenueScopeDisclosure` in plain words (`REVENUE_SCOPE_WORDS`); a withheld amount reads "Withheld" / "Unavailable" with its reason | `static/app.js:15625`–`:15641` |
| M7 | The Meta table always states each account's own days vs the Google Ads table's Europe/London days (`day_basis_disclosure`, on every payload; `metaAdsetDayBasisHtml` always rendered). **Documented limitation: while an account stays on America/Los_Angeles, the table is withheld every day** — that account's calendar day closes 8 h after London's, so the settled day the table needs is not yet verified for it when London's is. Stated on the page, not worked around | `services/meta_ads_adset_table_service.py:136`, `:189`, `:211`; `static/app.js:15907`, `:16002` |
| M8 | Independent reading for the five `*__settled_days` identities (§9.2) | `services/cross_page_parity_service.py:303`, `:581`, `:1106` |
| O1 | Dashboard → Countries rows republish the mart row's reader-contract ratio; the KPI / regional mix use `derive_period_revenue_per_ad_spend` — a published zero revenue over verified positive spend is `0.0`, as on the Countries page | `services/dashboard_countries_service.py:152`, `:582` |
| CLAUDE.md | only the "Those rows return None, not 0, for zero revenue" rule and its row wording | `CLAUDE.md` (reader-contract landmine) |

### 9.8 Tests

New:

| File | Cases | Kind |
| --- | --- | --- |
| `tests/test_pr_ads_177c_campaign_feed_retired.py` | 16 | running app (404s), AST, node-evaluated `app.js`, static files, cert mutation |
| `tests/test_pr_ads_177c_campaign_feed_port_pg_integration.py` | 10 | PostgreSQL, real writers / builders / audits |

The PG module is in the CI PostgreSQL step and its did-run list; the deleted
157 / 157-F1 modules left both.

**Fail-before.** Both files copied onto a `git archive d543dcc` tree:
**26 of 26** cases fail (16 of 16 in the
retired module, 10 of 10 in the PG module — missing modules / attributes
(`audit_campaigns_page_certification`, `evidence_window_bounds`,
`CONSUMER_SETTLED_CAMPAIGN_CANONICAL`), the routes still answering, no
`breakdown` / `google_ads_campaign_ids` / `campaign_id_cohort`, the Meta payload
without its day basis, `loadKeywordsTab` undefined, the strip still
`[campaigns, keywords, search-terms]`). The ported `156_f3` 13–15b copied onto
the same tree: 13, 13b, 14, 15 pass (properties the live routes already held —
the port's point), 15b fails 3 of 3 (`unavailable_reason` absent: the finding).

Named mutations on the finished tree (each in the test that carries it):

| Mutation | Fails |
| --- | --- |
| the mart's name resolver excludes `brand - uk` | port 01 (both parity groups `broken`) |
| the settled mart inflates its campaign deals | port 02 (settled identity `mismatch`) |
| `SHARED_DISPLAY_NAME` no longer matches the flag | port 03 (cert L5) |
| a row SQL dropped / a CPQL published | port 05 (cohort audit) |
| the drawer lists one extra deal | port 06 (cert L6) |
| the settled summary's deals +1 | port 10 (reader cutover `windows_reconcile`) |
| a retired route reintroduced in a tree copy | retired 05 (cert S1) |
| the mart's `_cohort_contract_block` drops an all-sources SQL | 161B `test_22` (`sql_proof`) |

### 9.9 Existing tests changed

Legend as §7.2: **(r)** retired / renamed surface, same property on its
successor; **(b)** broadened; **(d)** behaviour changed by a decision of this PR
and the new behaviour asserted; **(f)** fixture completed to production's
shape; **(x)** removed because the code under test is deleted and the property
is ported (row says where). Nothing is skipped, deselected or weakened.

| Test | Kind | Why |
| --- | --- | --- |
| `143`, `157`, `157_f1`, `revenue_attribution_api` (modules) | (x) | the routes / service they tested are deleted; ported per §9.3 |
| `172a`: 04, 12, 13, 21, 22, 24, 31–33e, 34–37, 39, 39b, 40–42 | (x) | Campaign Evidence page / drawer-preview memory and its certification; the Search Terms memory cases stay; the preview builders are deleted |
| `161b`: CE cases 08–08e, 11–11k, 12_pg, 13–13c, 14*, 15–15p, 16, 17*, 20, 22 (old), 23, 24, 26 | (x) | `build_campaign_evidence` / its drawer / its frontend; ported to the settled view (§9.3, port 03–05); `_resolver` → `campaign_label_resolver`; 15l, 18, 21 updated to live renderers; new 22 is the `sql_proof` counterfactual on the mart |
| `169a::test_10` | (x) | parsed the deleted certification script |
| `141` (evidence endpoints, handlers, EVIDENCE_PAGES, loader) | (r) | live evidence routes / `["search-terms","ngrams"]`; 5 CE cases (x) |
| `144` (account tz, drawer link, header, help, CE stays gone, prefill) | (r) | page is Keywords & Search Terms; the drawer opens Campaigns; the window helpers moved |
| `146::test_frontend_page_is_full_width_no_runs_column` | (r) | `#page-keywords` gone; the full-width page is `#page-search-terms` and the module renders into `#kw-tab-body` |
| `152b` (window resolver, wiring list) | (r) | `evidence_window_bounds` in `analysis.evidence_windows` |
| `153d`, `177b_one_page::test_01`, `sidebar_navigation_structure`, `162g::test_09`, `174a` (EXPECTED_ITEMS, labels, 11), `help_drawer_and_snapshot_history`, `empty_state_page_explanations`, `dataset_freshness_ui_contract`, `system_status_war_room` | (r) | strip `[campaigns, search-terms]`; 14 → 13 pages; `keywords` is a redirect, not a route / help / dataset page |
| `177b_one_page::test_02` | (r) | the one-line `loadCampaigns` alias is deleted; the router's `case "campaigns"` calls `loadCampaignsPage()` (asserted) |
| `158` (CPQL consumers ≥ 2, names, probe file) | (r) | CE's CPQL consumers are deleted; the probe file is `keyword_evidence_service` |
| `search_terms_followup_fixes` (2 `api_search_terms` cases) | (x) | the handlers are deleted; data-quality fields ported in `156_f3` 13–15 |
| `156_f3_review_corrections` 13, 13b, 14, 15, 15b, `SCOPED_CONSUMERS` | (r) | the same claims on `/api/search-term-evidence`, `/patterns`, `/flagged` over PostgreSQL; spend compared as `spend_native` (the live routes publish USD only past FX); 15b now also requires `unavailable_reason` (a finding fixed) |
| `156_f3_account_identity_cutover::test_8_to_10` | (r) | parametrised over the three live handlers: no SQL of their own, delegate to the scoped service; asserts the retired ones are gone |
| `156::test_13`, `::test_28c` | (d) | the snapshot has no reader or scheduled write and no freshness entry (§9.6); 28c asserts no `FROM/JOIN keywords` and no `write_keywords(` in production and that the durable facts are still synced |
| `canonical_freshness`, `162h`, `165a_weekly`, `daily_incremental_sync`, `137`, `114` | (d) | the `keywords` dataset / batch / write is retired: config lists, kept calls, retired-datasets set, stubs (`write_keywords` → asserted not called) |
| `154c_cross_page_parity::_payloads`, `LEGACY_READ_REGISTRY` | (f) / (x) | the fixture gains `canonical/campaign_settled` in production's shape; CE's two legacy reads leave the registry with CE |
| `154c_f3::test_f3_2` | (b) | the settled identity has the third, independent consumer |
| `161g` (SIX, 04, 06) | (r) | `campaigns` (the new gate) for `campaign_evidence` |
| `161d::test_34` | (r) | the resolver is bound via `campaign_label_resolver.build_resolver` |
| `177a_f1::test_n1_b`, `177a_pg::test_06`, `::test_06c`, `::test_10`, `177b_pg::test_05` | (r) | reading key `campaign_id_cohort` for `campaign_evidence`; the same days, the same values |
| `173a` (21, 22, 22_b, 39, 40_a; scenarios) | (x) | the 20 golden `preview` requests (16 + 2 + 2) are the deleted `build_campaign_flagged_preview`; the golden file is unchanged and every other request still compares byte for byte |
| `176c_pg::09b`, `::28` | (r) | the open-start spend reader set is the settled mart (`campaign_totals.spend == 125.0`), not CE |
| `126`, `128`, `177a_one_sql_count::06e` | (r) | the deleted mart client renderers are replaced by the live renderers in the same assertions |
| `138` (helper, GB, Europe) | (d) | O1: a published zero revenue over verified spend is `0.0` |
| `108::test_no_windsor_in_revenue_attribution_endpoint` | (r) | the endpoint is deleted; asserted gone, and `/audit` reads no Windsor |
| `platform_evidence_google_ads_api_cutover` | (r) | live routes; datasets → `keyword_facts` |

### 9.10 Not done here

* `search_term_evidence_service`'s narrowed `flagged_candidates` population (and
  `search_term_repository`'s matching SQL) now has no production caller: only
  the deleted flagged preview asked for it. Removing it touches the repository
  SQL and 172A's memory tests; left for its own change.
* CLAUDE.md still names Campaign Evidence and `campaignSqlPublication` in its
  "SQLs in this period" landmine; only the zero-revenue rule was in scope.

### 9.11 PR-ADS-177C-F1 — truth audit round 1

Production evidence (read-only): one name is shared — `low cpc - europe` is
Google Ads campaigns `22536989505` and `23054708812` ($7,985 spend,
2025-05-11 → 2025-11-05), so it is live on `all_time`.

**MAJOR M-1 — the shared-name disclosure never reached people.** 177C set
`google_ads_campaign_ids` / `identity_status` / `verdict = mapping_required` on
the row, but no page printed them: the Campaigns table showed the combined row
as one campaign under a "Spend mapping required" badge (the wrong reason),
Dashboard → Campaigns summed both ids silently, and Overview's best-campaign
and waste signals could name the combined row. Owner decision (coordinator):
**label, don't withhold** — the figures are the name's true combined figures,
never one campaign's.

| Surface | Now | Where |
| --- | --- | --- |
| Campaigns table | under the name: "2 Google Ads campaigns share this name — figures combined"; under the period ratio: "combined across the campaigns sharing this name"; badge "Not judged — shared name" with its own tooltip | `static/app.js` `campaignShared*` helpers (before `campRoasCell`), `renderCampaignsPageTable`, `getBusinessVerdictBadge(verdict, reason)` |
| Campaigns drawer | the same words, the ratio label, and the campaign ids ("cannot be split by campaign") | `renderCampaignsPageDrawer` |
| Dashboard → Campaigns | the same label beside the name and ratio; next action "Not judged — several campaigns share this name" (not "Map campaign spend"); the drawer lists the ids | `renderCampLeaderboard`, `wireCampDrawers` |
| Overview | best-campaign and biggest-waste signals rank distinct campaigns only, carry `shared_name_rows_excluded`, and the card says "N shared-name row(s) … not ranked" | `services/dashboard_overview_service.py` `_build_signals`; `static/app.js` `dashSharedExcludedNote` |
| Certification | S4 requires each label on each surface and the badge's own words; L5 also checks Dashboard rows (status "Mapping needs review") and Overview's two signals | `scripts/audit_campaigns_page_certification.py` (`JS_SHARED_NAME_SURFACES`, L5) |

Observed while testing (existing, correct behaviour, stated so it is not a
surprise): a shared name with a won deal and **no approved identity mapping**
places that deal on no campaign (the campaign-ID resolver never merges two
ids), so the closed-won truth withholds campaign revenue for the whole window
(`google_ads_deals_not_placed_on_a_campaign`, PR-ADS-161D). If
`low cpc - europe` carries won deals, approving its label onto one id in
Mapping Review publishes them; the row stays labelled as two campaigns.

**Minor 1 — false mismatch in the settled identities.** The independent
reading counted deals on labels the account approved as "not Google Ads"; the
mart drops them (PR-ADS-120b). It now reads the configured account's approved
exclusions and applies them before `reader_row`; if they cannot be read the
deal figures are unknown (never counted wider) —
`cross_page_parity_service._approved_not_google_ads_labels`.

**Minor 2 — L6 could pass over no drawer.** L6 now reads every drawer's
population ONCE per window (`revenue_attribution_service.campaign_deal_details_by_campaign`,
which `build_campaign_deal_details` now answers from), checks every row whose
`deal_ids` is published — including `[]` rows — records `drawers_probed`,
`rows_with_deals` and `canonical_revenue` per window, and exits 2 when
canonical revenue cannot be read. The stored run row keeps them
(`platform_certification_service._drawer_populations`, replacing the retired
`_drawer_sections`). The dead `revenue_unavailable` field is deleted.

**Minor 3 — leftovers.** `scripts/backfill_windsor.py` no longer has a
`keywords` step (it imported the deleted `write_keywords` inside a `try` that
swallowed the ImportError and reported 0 rows); `scripts/backfill.py`'s
`GOOGLE_ADS_DATASETS` drops it too. CLAUDE.md's `legacy_sql` sentence named
the deleted gate; **no check enforces that rule now** (its only publisher,
Campaign Evidence, is retired), and the sentence says so. The API-contract
index marks the six retired routes. The stale docstring
(`audit_keyword_search_term_freshness._legacy_facts`) and the
`config/thresholds.yaml` comment are corrected.

**Minor 4 — docs overclaim.** §9.3 / §9.4 said 157 checks 4–8 were ported to
L6. They certified the drawer's keyword / flagged preview sections and are
retired with them; L6 is a new check. Corrected above and in the audit's
docstring.

**Minor 5 — runtime.** Measured under `scripts.certification_child --memory-mb 320`
(all five business windows) on one throwaway PostgreSQL seeded with 21,756
contacts over 18 months, 401 won deals, 42 campaign rows (41 names over 540
days of daily spend, one name on two ids), 20 campaigns with deals
(`scratchpad/measure_177c.py`):

| Tree | Wall (two runs) | Peak RSS | Peak address space | Drawers probed / with deals (all_time) |
| --- | --- | --- | --- | --- |
| 177C (`2cfd095`, one `build_campaign_deal_details` per row) | 46.6 s, 39.0 s | 57.0 MB | 102.3 MB | not recorded |
| 177C-F1 (one grouped read per window, plus Overview for L5) | 26.2 s, 27.9 s | 60.4 MB | 104.8 MB | 42 / 20 |

Both are far inside the 900 s timeout and the 320 MB backstop (the derived
resident budget in production is ~151 MB). The per-row drawer reads were the
part that scaled with rows (≈ 40 % of 177C's wall time here); F1 reads the
ledger once per window. This machine is faster than the production instance
(the cohort audit takes ~430 s there), so the production figure will be
larger — by the cohort audit's ratio, still well under the budget.

**Carried, not fixed here (follow-ups):**
* *L7 coverage* (truth audit minor 4): L7 checks the settled view's rows,
  totals, summary and spend truth; Dashboard → Campaigns' own spend cells are
  not in it.
* *M4 FX-only rows* (truth audit minor 5): when FX is the only gap the settled
  view keeps native spend on rows; whether those rows should also carry the
  `spend_unknown` verdict wording is open.

**Tests.** New `tests/test_pr_ads_177c_f1_truth_audit.py` (8, node-evaluated
renderers, audit structure, run-row summary, leftovers) and PG cases 11–13 in
`tests/test_pr_ads_177c_campaign_feed_port_pg_integration.py` (already in the
CI PostgreSQL step). Changed: port `test_06` — its "wrong drawer" mutation
now targets `campaign_deal_details_by_campaign`, the read L6 and every drawer
share (the old target, `build_campaign_deal_details`, is now a filter over
it), and it asserts `drawers_probed` / `rows_with_deals`.

Fail-before (both copied onto `git archive 2cfd095`): the new module **8 of
8** fail; PG cases 06, 11, 12, 13 **4 of 4** fail (`KeyError:
'drawers_probed'`; Overview names `brand - uk` (the shared row) best
campaign; the settled deals identity reads 1 / 1 / **2** — the false
mismatch; no `campaign_deal_details_by_campaign`). Named mutations: drop any
shared-name helper from any of the four renderers, or the badge's flag test
→ S4 violation (F1 test 04, 9 mutations); Overview ranks every row → best
campaign is the shared row and L5 names it (PG 11); the grouped drawer read
lists an extra deal → L6 violation (PG 06); the grouped read reports the
ledger unavailable → exit 2 with `drawers_probed = None` (PG 13).

### 9.12 PR-ADS-177C-F2 — truth audit round 2

**Production fact.** Both `low cpc - europe` ids are in the configured account
3059734490 and ran back to back: `22536989505` from 2025-05-11 to 2025-09-22
as "LOW CPC - Europe", then `23054708812` from 2025-09-29 to 2025-11-05 as
"Low CPC - Europe". It looks like a relaunch. The window's per-campaign
revenue is withheld until the owner approves that label onto one id in
Mapping Review (pre-existing behaviour, confirmed by the auditor; §9.11).
The row stays labelled as two campaigns either way.

**MAJOR (introduced by F1).** F1 dropped shared-name rows from Overview's
campaign list *before counting*, and the signals and the decision cards both
read the filtered list. Three effects:
1. When the only producer was a shared row, `producing` was non-empty but
   `best` was None, so the Scale card said "No verified won-deal producers yet".
2. The waste total and count dropped the shared row (550 → 50, 2 → 1). With
   the shared row as the only candidate, the signal was None and the page said
   "No signal", with no disclosure.
3. The Watch card still named a shared row unlabelled.

**Fix** (`services/dashboard_overview_service.py`):
- Shared rows stay in every total and count: `waste_total`, `campaign_count`,
  `producing`.
- `_pick_named` (`:529`) passes a shared row over only when ONE row is named.
  If that row is the only candidate, it is named with its label
  (`shared_name_label`, `:519`): "N Google Ads campaigns share this name —
  figures combined".
- The best-campaign signal (`:555`ff.) carries `shared_name_label`.
- The waste signal (`:611`) carries `top_campaign_shared_name_label` and
  `shared_name_rows_included` (in the total) as well as
  `shared_name_rows_excluded` (passed over when naming).
- The Scale (`:738`) and Watch (`:773`) cards name a shared row with its label
  (`_named`, `:704`). The Investigate card says how many of its campaigns are
  shared names.
- The Overview signal lines say the same (`static/app.js`
  `dashSharedIncludedNote` `:3319`, `dashSharedNamed` `:3323`).

**Certification.** L5's Overview check is now
`overview_shared_name_problems` (`scripts/audit_campaigns_page_certification.py:567`).
It reads the mart's through-today campaign view (the rows Overview is
computed from) and checks:
- the waste signal's value and count include every row with spend and no
  SQL or won-deal proof, shared ones too;
- the Scale card never says "no producers" over a producing row;
- every signal or card that names a shared name says so.

S4 also covers Dashboard → Deals' Campaign → Pipeline table. The new function is
registered in the SQL-doctrine inventory (`analysis/sql_doctrine_registry.py`
`diag.cert`). It names `sqls` fields to reconcile them; without the registration,
`test_pr_ads_158_sql_doctrine_audit` fails (4 cases).

**Minors.**
- **Dashboard → Deals.** This was kept minimal because 178B rewrites
  `dashboard_deals_service.py`. Exactly two pass-through fields were added to
  `_build_campaign_breakdown`'s row (`identity_status`,
  `google_ads_campaign_ids`; `services/dashboard_deals_service.py:443`), plus
  one `campaignSharedNameHtml(r)` call in `renderDealCampaignPipeline`'s name
  cell (`static/app.js:6532`).
- **CLAUDE.md.** The `campaignSqlPublication` paragraph no longer says the
  drawer passes "its own `/api/campaign-detail` response's" cohort. It now
  says the Campaigns drawer passes the cohort of the settled campaign-view
  response its row came from, and that `/api/campaign-detail` is retired.

**Tests.**
- `tests/test_pr_ads_177c_f1_truth_audit.py::test_05` is rewritten. F1's
  version held trivially, because its shared row had 2 deals and so could
  never be waste. The shared row is now the largest spender without proof.
- New cases in the same module:
  - `05b`: (a) the only producer is a shared row;
  - `05c`: (b) the only spend without proof is a shared row;
  - `05d`: the Watch card;
  - `05e`: the certification's Overview check, with five mutated payloads;
  - `05f`: Dashboard → Deals.

**Fail-before.** The module was copied onto `git archive 8876c0f`: **6 of 6**
new or rewritten cases fail.
- 05: `campaign_count` 1 ≠ 2.
- 05b: `best_campaign` None.
- 05c: `biggest_waste` None.
- 05d: the Watch body is unlabelled.
- 05e: `overview_shared_name_problems` does not exist.
- 05f: the Deals row has no `identity_status`.

The 7 cases unchanged since F1 pass there.

### 9.13 Rebased onto PR-ADS-178B (one Pipeline page), and the round-3 minors

The round-3 truth audit verdict was **merge** (no Blocking, no Major).
PR-ADS-178B merges first, so 177C's three commits were cherry-picked onto
`bffe604` (main `07be622` + 178B).

**Conflicts and resolutions** (both PRs' intent kept):

| File | Resolution |
| --- | --- |
| `.github/workflows/pr-ads-153d-checks.yml` | Union. The PG step and the did-run list carry both `test_pr_ads_178b_pipeline_pg_integration` and `test_pr_ads_177c_campaign_feed_port_pg_integration`; 177C's deleted 157 / 157-F1 modules stay out |
| `db/revenue_repository.py` | Each PR deleted the neighbour of the other's deletion (`fetch_campaign_lead_detail` / `fetch_sql_lead_details`). Both stay deleted, each with its own comment |
| `docs/09_REPO_STATE.md` | Union |
| `scripts/audit_customer_revenue_reader_cutover.py` | Union of `_page_builders()`: 178B's `pipeline` and 177C's `revenue_decision_mart_campaign_settled` / `meta_ads_adsets`. `pipeline_checks` and `settled_mart_checks` both run (auto-merged), and the `pipeline` INVENTORY row is kept |
| `static/app.js` | `PAGES` has `pipeline`, and neither `keywords` (177C) nor `leads` / `deals` / `opportunities` (178B). `PAGE_DATASET_MAP`, `PAGE_EXPLANATIONS`, `PAGE_DEPENDENCIES` and the `loadPage` switch have `pipeline`'s entry and no `keywords` entry. The Leads/Deals header comment is 178B's. F2's `renderDealCampaignPipeline` hunk is dropped (178B deleted the function) |
| `static/styles.css` | 178B's Pipeline block plus 177C's phone rule. 164A's `#page-deals` footnote rule stays deleted (178B removed `#page-deals`) |
| `services/dashboard_deals_service.py` (F2) | 178B's file kept whole. F2's two pass-through fields lived in `_build_campaign_breakdown`, which 178B deleted, so they are not re-added |
| `tests/test_pr_ads_143_campaign_evidence_window.py` | Deleted (177C; ported in §9.3). 178B's only edit there moved a slice bound past the deleted `fetch_sql_lead_details`, inside a test of the also-deleted `fetch_campaign_lead_detail` |
| `tests/test_pr_ads_141…` | 177C's deletion of the Campaign Evidence drawer cases kept. 178B's edit to one of them was a slice-bound move only |
| `tests/test_pr_ads_126…` | 178B's "the Pipeline page shows no spend" assertion is kept. The two slices of renderers 177C deleted (`renderMartSpendTruth`, `renderMartSummaryStrip`) stay removed |
| `tests/test_pr_ads_162g…::test_09` | Both comments kept; 14 → **12** distinct pages (−1 for Pipeline, −1 for Keywords) |
| `tests/test_pr_ads_165a_weekly…`, `test_dataset_freshness_ui_contract`, `test_empty_state_page_explanations`, `test_sidebar_navigation_structure` | Both: `pipeline` in, and neither `keywords` nor `leads` / `deals` |
| `tests/test_pr_ads_174a…::test_06` | Strip labels: "Keywords & Search Terms"; no Leads / Deals / Keyword Evidence |
| `tests/test_pr_ads_177a_one_sql_count…::test_06e` | Neither deleted renderer (`renderMartSummaryStrip` by 177C, `renderDealCampaignPipeline` by 178B); 178B's `pipelineKpiCardsHtml` added |

**After the merge:**
- The Campaigns page drawer's `["deals", "Open Deals"]` is now
  `["pipeline", "Open Pipeline"]`, the link 178B left to 177C (docs/61 §7).
  `178b_one_page::test_12` now checks this drawer too.
- `test_pr_ads_177c_f1_truth_audit::test_05f` is replaced. The table it
  labelled is gone, so it now asserts that the Deals campaign table stays
  gone and that every surface the certification's S4 lists exists.
- The certification no longer lists `renderDealCampaignPipeline`.
- A scan of every top-level `app.js` name defined at `07be622`, `bffe604`,
  `2cfd095` or `dc1d199`: no name that is now deleted is still referenced.

**Round-3 carried minors** (`services/dashboard_overview_service.py`, `scripts/audit_campaigns_page_certification.py`):
1. The Scale card names the best single campaign. When a shared row won
   more, the body says so: "<name> (N Google Ads campaigns share this name —
   figures combined) won more ($…), but it is not one campaign, so it is not
   named the best." The signal carries `bigger_shared_rows_skipped`.
2. The Scale and Watch headlines count names when one is shared:
   "N campaign names … (K shared by several Google Ads campaigns)"
   (`_rows_headline`).
3. L5's card check matches the EXACT label. Every occurrence of a shared
   name must be followed by ` (<n> Google Ads campaigns share this name —
   figures combined)`, with n being that row's id count, and a signal's
   label must equal it. A loose "share" substring passed a wrong count or the
   word used in another sentence.

Tests: `test_05g`, `05h` and `05i`, plus `05b`'s headline (which changes
under minor 2). All four fail on the rebased F2 tip `5f5b299`.

**A test gap I had missed, now fixed:** `tests/test_pr_ads_162f_sidebar_groups.py`
cases 01 and 03 pinned the Campaigns strip as `[campaigns, keywords,
search-terms]` and the page count. They also failed on the un-rebased 177C
(`dc1d199`), because my earlier verification sweeps did not include that
module. Both are updated with 177C's change (kind (r): Keywords is a tab):
the strip is `[campaigns, search-terms]` and there are 12 distinct pages
after both PRs.

**Commits.** The three 177C commits, the conflict resolutions, the round-3
minors and the 162f fix are squashed into one commit on `bffe604`,
"PR-ADS-177C: retire the old campaign feed; one Keywords & Search Terms
page (on 178B)". That way no intermediate commit carries the 162f
failure.
