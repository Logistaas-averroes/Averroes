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
