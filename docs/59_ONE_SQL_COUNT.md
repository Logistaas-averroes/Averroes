# 59 — One SQL count: the acquisition cohort everywhere

Series doc for the Campaigns merge. **PR-ADS-177A** (this PR) moves every
leads / SQLs figure the Revenue Decision Mart publishes, and every page that
republishes it, onto the **acquisition cohort** Campaign Evidence already
publishes (PR-ADS-161B, `docs/44`). **PR-ADS-177B** builds the one Campaigns
page in the Dashboard design. **PR-ADS-177C** retires the old pages.

Depends on: PR-ADS-176C (`dce66da`). Unblocks: PR-ADS-177B.

---

## 1. The problem

Two pages could show different "SQLs" for the same campaign and the same days:

| | Campaign Evidence | Revenue Decision Mart and every page that republishes it |
| --- | --- | --- |
| Population | Google Ads-sourced HubSpot contacts **created** in the window (`hubspot_contact_funnel`) | paid-search rows of the legacy `leads` table, contact-created in the window |
| SQL | canonical lifecycle evidence proves the contact reached SQL as of the contact-funnel watermark | latest `leads.status_category = 'qualified'` |
| Verdict | `cohort.sql_status` — withheld when the contact sync is not proven complete | none: published whenever `leads` had no undated paid row |
| Read by | `services/marketing_outcome_cohort_service.py` | `db.revenue_repository.fetch_lead_quality` → `revenue_attribution_service._build_db_rows` |

The mart's count reached: the mart's campaign, country and deal views; the
Countries page (176B); Campaign Revenue & Spend; Dashboard → Overview,
Campaigns, Countries, Revenue and Deals. The SQL-doctrine registry classified
it `CLS_LEGACY`.

## 2. What this PR does

* The mart reads **no** `leads.status_category` count. `_build_from_db` builds
  the window's acquisition cohort once (`_read_cohort` →
  `marketing_outcome_cohort_service.build_window_outcomes`) over the SAME
  account-local days as its spend, with freshness from the real
  `analysis.sql_coverage_freshness.assess`.
* Every row's `leads` / `sqls`, the summary's and every total are the cohort's.
  One verdict (`cohort.sql_status` / `sql_reason`) decides all of them.
* Withheld means absent: when the verdict is not `published`, every lead and
  SQL count in the mart payload and every Dashboard payload is `None`, with
  the reason, and no verdict is computed on it (§6).
* `scripts/audit_marketing_outcome_cohorts.py` now also audits the mart's two
  views and the five Dashboard payloads (§7).
* The cross-page parity audit compares the Countries page, Dashboard Countries
  and Campaign Evidence over the same days (§7).

## 3. Decisions

**Owner decisions, 2026-11-24.** Campaigns becomes one page in the Dashboard
design (177B builds it; 177C retires the old pages). The owner chose:

1. **One SQL count everywhere: the stricter acquisition cohort.** Before
   merging Campaigns, the Dashboard, Countries and the mart switch to the
   cohort, so every page shows the same SQLs — accepting that they are
   withheld when the contact sync is incomplete. *This is PR-ADS-177A.*
2. **Business windows** (Current Quarter by default), ending yesterday as
   Countries does. *177B.*
3. **Keywords & Search Terms** become one combined page on evidence windows.
   *177B.*
4. **Meta ad sets** go in a separate "Meta Ads — ad sets" table, never summed
   with Google, with outcome cells marked not linked. *177B.*

**PR-ADS-177A decisions** (readings of standing doctrine, not new owner calls):

* **Leads are the cohort's contacts, withheld with the SQLs.** "Leads" on every
  mart-derived row is the cohort's contacts acquired. Under a withheld verdict
  the population is not proven complete, so a lead count over it is a partial
  presented as a total: it is withheld with the SQLs, the same reason. (Campaign
  Evidence still shows its own "Leads acquired" marked *partial*; the mart
  pages, which print no partial marker, withhold instead.)
* **A withheld cohort blocks no page.** `readiness.revenue_decision_ready` no
  longer requires the lead metrics. The PR-ADS-109 gate blocked every revenue
  page while the legacy `leads` table had undated paid rows; the cohort now
  withholds the figures it does not prove, one by one, with the reason. The
  owner accepted SQLs withheld — not pages. `readiness.lead_metrics_ready`
  is the cohort verdict.
* **The legacy reconciliation is not attached to a cohort count.** The mart's
  `sql_reconciliation` is the cohort's own (bucket identities re-checked on
  every build, `metric_family` declared). The PR-ADS-152 reconciliation
  compares against the legacy population; attaching it would compare two
  metric families.
* **The JSON fallback publishes no lead or SQL count.** With the database down
  the cohort cannot be read: `leads` / `sqls` are `None` with
  `canonical_funnel_unreadable`. Its old count (`mql_status` in
  `analysis.core.QUALIFIED` over `data/crm_contacts.json`) is deleted.

## 4. Population rules

The window cohort is the Google Ads bucket of the acquisition cohort
(`google_ads = campaign + unattributed_google_ads`, `docs/44` §4): HubSpot
contacts **created** in the window's account-local days whose own original
source is Google Ads (`classify_source(hs_analytics_source)`), minus contacts
whose campaign label an admin marked "not Google Ads". An SQL is a contact
canonical lifecycle evidence proves reached SQL as of the contact-funnel
watermark (`docs/44` §3). No date is inferred.

| View | A contact is on the row … | The residual | Conservation |
| --- | --- | --- | --- |
| **Campaign** (`view=campaign`) | whose HubSpot campaign label (`hs_analytics_source_data_1`) the **mart's own identity rule** resolves to that canonical campaign name: an approved manual mapping, else an exact-normalised match against the window's canonical spend campaigns (`_mart_label_resolver`, the rule the mart's revenue rows are keyed by). A label it cannot resolve is its own row, keyed by the label (spend mapping unavailable → `mapping_required`). | `cohort.campaign_residual`: Google Ads contacts with no campaign label, a pseudo- or email-campaign label, or labels that conflict across the contact's rows. Counted in the total, on no row. | Σ campaign rows + `campaign_residual` = `summary` (leads and SQLs) |
| **Country** (`view=country`) | whose **own HubSpot country** — `ip_country`, then `country`, the product's one precedence (`contact_country`) — `analysis.country_identity.resolve` names as that listed market. Never a campaign's, a deal's or a geo row's country. | The residual row: contacts whose country is no listed market, unrecognised, missing, or that conflicts across the contact's rows. | Σ market rows + residual row = `summary`; `country_totals` (markets) + residual row = `summary` |
| **Dashboard Countries** | the mart's country rows | `residual.sqls` = the mart residual row's own count (it was the legacy campaign total minus the country rows) | `kpis.sqls` + `residual.sqls` = the cohort total |

The partition is computed once, by `marketing_outcome_cohort_service.build_cohort`
(`group_key`, new): every Google Ads contact lands in exactly one country
group, and `reconcile_cohort` re-checks Σ groups = `google_ads` on every build.
A failure withholds the whole cohort (`cohort_reconciliation_failed`).

**The join is the mart's own key, not a new one.** The cohort service takes a
`resolve_label` (it did for Campaign Evidence, which binds its `_assign_lead`
and keys on `campaign_id`). The mart binds its own identity rule and keys on
the canonical campaign name. So a contact lands on the row the mart's spend
and revenue land on. The only difference from Campaign Evidence is where a
normalised name is shared by two spend campaigns: Campaign Evidence refuses
to pick (Mapping Review), the mart merges them into one row as it always has.
Either way the Google Ads **total** is the same — only `not_google_ads`
mappings move a contact out of it, and both read the same approved mappings.
The parity audit checks that total (§7).

## 5. What changes on each surface

Release notes for the owner — **SQL and Leads figures will change value**:

| Surface | Before | After |
| --- | --- | --- |
| Mart campaign view rows / summary | `leads` = legacy paid-search leads; `sqls` = `status_category = 'qualified'`; summary = Σ rows | the cohort per campaign; summary = the window's Google Ads cohort (rows + `campaign_residual`); `cohort` block; `sqls_status` / `sqls_reason` and `metrics.leads` / `metrics.sqls` on every row |
| Mart country view rows, `country_totals`, `regional_mix` | legacy leads by the lead's country text | the cohort by the contact's own country; totals carry the cohort's reason when withheld; `lead_scope = google_ads_cohort_contacts_by_contact_country` |
| Mart deal view | summary `leads` / `sqls` legacy | the cohort (same summary as every view) |
| Countries page (`#/roas-countries`) | Leads / SQLs columns, SQLs KPI: legacy | "Leads · contacts acquired in window" / "SQLs · contacts acquired in window": the cohort, or **Withheld** + reason |
| Campaign Revenue & Spend (`#/roas-campaigns`) | legacy | the cohort; the blocked card no longer blocks on leads |
| Dashboard → Overview | headline "Google Ads-source SQLs" = legacy `page_reconciliation`; `kpis.sqls` = legacy campaign subset | headline (`kpis.google_ads_cohort_sqls`, F1 rename) and `kpis.sqls` = the cohort total; sub-line = the part on a campaign row (`kpis.campaign_row_cohort_sqls`); the SQL delta is shown only for a completed window (F1, §10) |
| Dashboard → Campaigns | `kpis.sqls` = Σ campaign rows (legacy) | `kpis.sqls` = the cohort total; `kpis.sqls_residual` = no-campaign contacts; status "SQLs withheld" |
| Dashboard → Countries | `kpis.sqls` = Σ markets (legacy); `residual.sqls` = legacy total − markets | `kpis.sqls` = Σ markets (cohort); `residual.sqls` = the mart residual row; status "SQLs withheld" |
| Dashboard → Revenue | `kpis.sqls` legacy | the cohort. The (unrendered) `customer_trend` SQL series stays legacy, declared `sqls_metric_family: legacy_lead_status_category` |
| Dashboard → Deals | `kpis.sqls` legacy; campaign breakdown legacy | the cohort; the lead-status backlog and the source breakdown stay legacy and are **relabelled** "Qualified (lead status)". F1: the backlog is `kpis.lead_status_qualified_no_won_deal` (was `sqls_not_closed_won`), **out of the funnel** in its own `lead_status_backlog` block, and no card, status or header over it says "SQL" (§10) |
| Dashboard → Channels | source-group SQLs (`build_revenue_by_source`) | the counts are unchanged — Channels does not read the mart's SQLs (it reads only spend, customers and readiness from it) — but they are the LEGACY lead-status population, so F1 relabels them "Qualified (lead status)" everywhere they print, declared `kpis.total_sqls_metric_family` (§10) |
| Campaign Evidence | the cohort | unchanged (it gains `bounds=` for the parity audit) |

**Expect:**

* per-campaign SQLs to change: the cohort counts every Google Ads-sourced
  contact with SQL proof — including SQLs HubSpot never dated — and no longer
  counts a contact whose legacy lead status says qualified while lifecycle
  evidence does not;
* **two** totals, each the same wherever it is printed — not one (F1
  correction; this bullet said "the same total" on every surface). The
  campaign view and the deal view run from the window's first day **through
  today**; the country view runs **through yesterday** (the settled-day
  window, PR-ADS-167A). So:
  * Overview, Campaigns, Deals, Revenue and the mart's campaign view print one
    number — one mart build over the same days (`google_ads_cohort_sqls`, one
    identity in the parity audit) — and Campaign Evidence built over exactly
    those days agrees (`cohort_sql_parity.campaign_view`, F1);
  * the Countries page (markets + residual), Dashboard Countries (markets +
    residual) and Campaign Evidence over the settled days print the other
    (`cohort_sql_parity`).
  The two differ by the contacts created today (and by any that reached SQL
  between the two builds). Every cohort label says its last day ("through
  24 Nov 2026" / "through 23 Nov 2026", F1) so the reader sees why;
* **Withheld** with the reason wherever the contact-funnel sync is not proven
  complete (bootstrap still running, last incremental failed, never synced …).
  No page is blocked by it.

## 6. Verdicts

A row whose SQLs are withheld is not judged on SQLs nobody published — the
PR-ADS-176B-F1 M1 pattern (`spend_unknown` / `revenue_unknown`):

| Consumer | Rule |
| --- | --- |
| `revenue_attribution_service._finalize_row` (mart campaign + country rows) | `verdict = "sqls_unknown"`, `verdict_reason` = the cohort's reason, **only when the verdict depends on the count** — `classify_verdict` gives a different answer for 0 SQLs than for some. A row judged on its revenue (winner / watch with won deals) or on low spend (learning) keeps that verdict. Precedence: `outcome_withheld` → `spend_unknown` → `revenue_unknown` → `sqls_unknown`; `mapping_required` still overrides. The old `sqls_for_verdict = 0` substitution and the "learning when withheld" fallback are deleted. |
| `dashboard_campaigns_service._campaign_status` | `"SQLs withheld"` when no won deal decides it (was "Spend without SQL / customer proof") |
| `dashboard_countries_service._country_status` and the regional-mix status | `"SQLs withheld"` (was "Spend without a won deal" / "SQL producer") |
| `dashboard_deals_service._source_status`, campaign breakdown | `"SQLs withheld"` (was "No activity"); a withheld row is no longer dropped as noise |
| Dashboard Overview signals / cards | already gated on `summary.sqls is not None`; the blocker names "lead and SQL counts (acquisition cohort withheld)" |
| Decision filter chips (Countries, Campaign Revenue & Spend) | filter on `verdict ∈ {winner, watch, waste, learning}`: a `sqls_unknown` row is in none of them, like `spend_unknown` |
| Watch cards ("spend without won-deal proof") | include "SQLs withheld" rows: their claim is about deals, which are known |
| `getBusinessVerdictBadge` | `sqls_unknown` → "Not judged — SQLs withheld" |

## 7. Guards

* **`scripts/audit_marketing_outcome_cohorts.py`** — new
  `mart_withheld_exposures(payload)` names every place a mart or Dashboard
  payload publishes a lead / SQL count (summary and its metrics, rows,
  `country_totals`, `regional_mix`, `kpis`, `residual`, `campaign_breakdown`,
  the cohort totals and residual, `sql_reconciliation`), and flags any present
  beside a non-published verdict; `mart_conservation_problems` checks rows +
  residual = total under a published one. `run()` builds the two mart views
  and the five Dashboard payloads for every business window
  (`audit_mart_window`); F1 adds `stale_disclosed` (§10.1). The legacy-family fields a page still carries on
  purpose (Deals' lead-status backlog, the source mix) are not cohort counts
  and are not in the scan.
* **`services/cross_page_parity_service.py`** — the identity
  `campaign_attributable_sqls` is now `google_ads_cohort_sqls` (and
  `campaign_attributable_leads` → `google_ads_cohort_leads`); the conservation
  rule's total is the cohort; new `cohort_sql_parity`: the Countries page
  (`country_totals.sqls` + residual row), Dashboard Countries (`kpis.sqls` +
  `residual.sqls`) and Campaign Evidence (`build_campaign_evidence(bounds=…)`
  over the Countries page's own settled days) must be one number.
  `cohort_sql_parity_broken` is a violation; a withheld surface is
  `not_evaluable`, never a pass. F1 adds `cohort_sql_parity.campaign_view`:
  the same rule over the CAMPAIGN view's days (the mart campaign view's
  `summary.sqls`, the Overview headline, Campaign Evidence over exactly those
  days). The two groups are never compared with each other: their days differ.

  **Where it runs** (F1 note): only from the manual CLI,
  `python -m scripts.audit_cross_page_canonical_parity`. It is not one of the
  six certification audits and not in CI against production data; making it
  a daily certification audit is a follow-up.
* **SQL-doctrine registry** — the mart, the revenue-attribution service,
  Dashboard Campaigns and Countries and their UI are `CLS_CANONICAL` with the
  cohort definition; Overview, Revenue and Deals are `CLS_MIXED` (a legacy
  backlog / series / source mix remains, labelled). The inventory reads
  **10 canonical / 11 legacy / 9 mixed, 0 unknown** (was 6 / 16 / 8). The known
  conflict `verdict.withheld_sql_becomes_zero` is removed — fixed.
* **UI** — `cohortCountHtml(value, cohort, row)` over
  `campaignSqlPublication(cohort)`: the cohort is an ARGUMENT (the `cohort`
  block of the response being rendered; `campBlockerOpts(d)` carries it), a
  row can narrow it with its own `sqls_status`, never widen it, and no
  argument is Unavailable (fail closed). Labels:
  "SQLs · contacts acquired in window", "Leads · contacts acquired in window".

## 8. Not done here

* **177B / 177C** — the one Campaigns page and the retirements (§3).
* Legacy SQL readers that remain, each labelled, none mart-derived:
  Dashboard Deals' "Qualified (lead status) not yet closed-won" backlog and
  source breakdown; Dashboard Revenue's unrendered `customer_trend` series;
  the Overview's and Channels' source groups (`build_revenue_by_source`);
  Keyword Evidence, Search Terms, the campaign-mapping workbench, the Campaign
  Evidence drawer's lead-quality splits and `/api/leads`
  (`python -m scripts.audit_sql_doctrine_inventory` lists them).
* `db.revenue_repository.fetch_lead_quality` stays: Campaign Evidence's
  declared legacy fields and the mapping workbench read it.
* The legacy lead-status rows' date field is still keyed `sql_date` /
  `days_since_sql` in the Deals payload although the reader windows on
  `contact_created_at`; F1 relabels the column headers "Contact Created" /
  "Days Since Created" and leaves the key rename to 177C with the reader.
* `cohort_sql_parity` runs only from the manual CLI (§7) — a follow-up.
* `CLAUDE.md` is unchanged: no rule it states changed. Its "SQLs in this
  period is two different metrics" landmine already names Campaign Evidence as
  the cohort's reader; the mart now reads the same family under the same rules
  (`metric_family` / `window_basis` on the payload, `assess` for freshness,
  withheld means absent).

## 9. Tests

`tests/test_pr_ads_177a_one_sql_count.py` (41 cases, no database) and
`tests/test_pr_ads_177a_one_sql_count_pg_integration.py` (13 cases at
PR-ADS-177A, 17 after F1; PostgreSQL; registered in the PG step and the
did-run list). F1 adds `tests/test_pr_ads_177a_f1_truth_audit.py` (14 cases,
no database; §10). The PG cases
seed legacy `leads` rows the old mart counted, beside the canonical funnel.

Fail-before: see §9.1. Existing tests changed: §9.2.

### 9.1 Fail-before evidence

Both files were copied unchanged onto `dce66da` and run there:
**52 of 54 cases fail** (41 + 13; corrected in F1 — this said "50 of 53",
counting before `test_05d` was added and before `test_07b` was made
specific). They fail on `ImportError` / `AttributeError` on the names they
need, or on the defect itself: the mart's 2 legacy SQLs where the cohort has
4, no `cohort` block, leads / SQLs published beside a running contact-funnel
bootstrap, the Gulf campaign judged `waste` on a withheld count, no
`cohort_sql_parity`. The two that pass there are a stated control and a case
shown load-bearing under a mutation; the third bullet records why `test_07b`
is not among them:

* `test_03e` — positive control: a won deal still decides a country's status
  under withheld SQLs (true before and after);
* `test_07c` — the static inventory has no unknown occurrence. It held at
  `dce66da` too; deleting this PR's `diag.parity.cohort` registry rule makes
  it fail (5 unclassified occurrences in `_cohort_sql_parity`);
* PG `test_07b` was made specific after the first run passed on `dce66da` by
  coincidence (2 legacy UK SQLs = 1 + 1 cohort SQLs): it now asserts the
  cohort's 3 leads (`(2, 2) == (3, 2)` fails on `dce66da`) and the UAE row.

Later additions — `test_05d` (a window with no settled day) — fail on
`dce66da` (no `_cohort_contract_block`) and under the mutation that drops the
metadata bucket counts' withholding from `_withhold_cohort`.

### 9.2 Existing tests changed

No assertion was weakened, skipped or quarantined. Every change is one of:
(a) a mock-based fixture that fed the mart's leads / SQLs through a
`fetch_lead_quality` mock now feeds **the same contacts** through the
canonical funnel (`tests/cohort_fixture.py`: one Paid Search contact per
fixture row, its label and country, `qualified` → a stage proving SQL;
membership by the real window rule; freshness by the real `assess`), the
assertions unchanged; (b) an assertion that pinned the legacy design this PR
replaces now pins the cohort contract that replaces it; (c) a rename of the
parity identity.

| Test | Change | Why |
| --- | --- | --- |
| `125::_patch_durable` | (a) | the mart reads the cohort; all 22 cases unchanged |
| `134::_patch_durable`, `135::_patch_durable`, `137::_patch_durable`, `138::_patch_durable`, `139::_patch_durable` | (a) | same; the fixture contacts are created 2026-05-15, inside the YTD and current-quarter windows those files use at their `NOW` (the legacy mock ignored the window) |
| `134::test_withheld_lead_metrics_degrade_watch_and_waste_honestly`, `135::test_rates_unavailable_when_denominator_missing` | (b) | "lead metrics withheld" was the legacy `event_date_safe=False` mock; it is now the cohort's own withheld verdict (bootstrap running, real `assess`). Assertions unchanged |
| `137::test_campaigns_kpi_cards_render_five_metrics`, `138::test_countries_kpi_cards_render_six_metrics` | (b) | the SQL card's label is `COHORT_SQLS_LABEL` ("SQLs · contacts acquired in window"); 138's fixed-length slice grew 4200 → 5200 because the card's gate made the function longer |
| `138::test_build_residual_is_canonical_mart_minus_countries` | (b) | residual SQLs are the mart residual row's own cohort count, no longer a legacy campaign total minus the country rows; customers / revenue subtraction unchanged |
| `109::_build`, `109::test_sql_withheld_when_event_date_unsafe` | (a) | `safe=False` is the cohort's withheld verdict; assertions unchanged |
| `116::_spy_repo`, `116::test_all_sources_share_selected_window` | (b) | the leads read is the cohort's, so the spy captures it; the bounds asserted are the same days as account-local instants (`window_instants`) |
| `152b` ×6 (`test_dashboard_headline_is_google_ads_source_scope`, `test_app_renders_google_ads_source_sql_headline`, `test_app_renders_reconciliation_required_on_mismatch`, `test_app_disclosure_of_campaign_attributable_subset`, `test_dashboard_ga_source_card_has_no_campaign_attributable_delta`, `test_dashboard_null_campaign_attributable_shows_unavailable_not_rate`) | (b) | they pinned the PR-ADS-152 legacy Overview headline the owner decision replaces. Now: the headline is the cohort, scoped; a reconciliation failure is the cohort's withheld verdict through the one gate; the sub-line is the part on a campaign row; the delta is like for like (both periods the cohort) and shown only under a published verdict; the null sub-line never prints a rate |
| `154c` (identity keys ×22), `154c::LEGACY_READ_REGISTRY`, `154c::test_f2_7_…` | (c), (b), (a) | `campaign_attributable_sqls` / `_leads` → `google_ads_cohort_sqls` / `_leads`; the registry's `revenue_attribution_service` → `fetch_lead_quality` entry is removed because that read no longer happens (the registry test requires that); the CLI audits the wall-clock current quarter, so the fixture contacts are created on its first day |
| `154c_f3` (identity ×6), `f3_4`, `f3_8`, `_cli_both_formats` | (c), (a) | same; the 14 + 2 = 16 contacts are fed to the cohort |
| `155_f2::test_19_…` | (c) | the untouched identity is now `google_ads_cohort_sqls` |
| `167a_settled_day_windows::test_07`, `::test_10` | (c) | the conservation rule's total is `google_ads_cohort_sqls` |
| `176b` fixture `_mart`, `test_04`, `test_20` | (b) | the mart's shape carries `cohort` (published) and the cohort `lead_scope`; the headers and the scope label say what they count |
| `revenue_attribution_event_dates::_build` + `test_lead_metrics_withheld_when_event_date_unsafe`, `test_windows_differ_when_event_dates_differ`, `test_service_passes_event_window_dates_to_repo` | (a), (b) | the grain is always the event date; "unsafe" is the cohort's withheld verdict; window difference is shown by contacts created in two windows (1 vs 5, now asserted exactly); the bounds asserted are the cohort read's instants |
| `revenue_attribution_service::_build_db`, `::test_db_path_used_when_database_available` | (a), (b) | the table used is `hubspot_contact_funnel`, not `leads` |
| `110::test_blocked_decision_logic` | (b) | `isRevenueDecisionReady` no longer requires the lead grain (a withheld cohort blocks no page, §3); the test now pins that and the separate `cohortLeadsReady` chip rule. Its revenue assertions are unchanged |
| `112::test_country_blocked_copy` | (b) | the card's "Lead Event-Date Reconciliation" remedy repaired the legacy `leads` table no figure reads; it now names the contact-funnel sync (`COHORT_LEADS_WITHHELD_STEP`). The geo-sync and "never a HubSpot lead re-sync" assertions are unchanged |
| `153c::test_campaign_attributable_sqls_stay_explicitly_named` | (b) | the Overview's `sqls` is still explicitly scoped — by the cohort's `sqls_scope` |
| `176a::_lead`, `176c::_leads` (PG seed helpers, used by 167a / 170a / 176a / 176b / 176c) | (a) | each legacy lead row is also written to the canonical contact funnel by the production writer, with a fresh sync state (`seed_cohort_contacts`); the legacy rows stay and are counted nowhere |
| `176c::test_13_unreadable_leads_are_unknown_and_spend_is_unaffected` | (a) | "readable and none" is a fresh, empty canonical funnel; "unreadable" is the cohort read failing (was `fetch_lead_quality`). Assertions unchanged |
| `test_core_lead_quality` | removal with the code | `analysis.core.QUALIFIED` had one importer, the JSON fallback's SQL count, deleted here (§3). Its three content cases go with the constant (as PR-ADS-162H removed the other four lists' cases); `test_the_live_importer_of_qualified_still_resolves` is replaced by `test_no_production_module_imports_the_retired_qualified_list`, and `QUALIFIED` joins the "gone" assertion |

## 10. PR-ADS-177A-F1 — truth audit round 1

Verdict "fix first, no blockers". Each finding, the fix, and the evidence
that its guard is load-bearing. The new cases live in
`tests/test_pr_ads_177a_f1_truth_audit.py` (14, no database) and four new
cases at the end of `tests/test_pr_ads_177a_one_sql_count_pg_integration.py`
(`test_08`, `test_08b`, `test_09`, `test_10`).

### 10.1 MAJOR 1 — a stale cohort printed as a bare number

`analysis/sql_coverage_freshness.assess` publishes a cohort over a stale
contact funnel (`source_stale`) — as of its watermark (`docs/44` §7).
Campaign Evidence said so; every mart / Dashboard surface printed the bare
number.

* The mart's cohort block now declares `stale` and `as_of`
  (`services/revenue_attribution_service.py` `_cohort_contract_block`;
  `revenue_decision_mart._withhold_cohort` sets `stale: False`).
* `static/app.js`: `cohortIsStale` / `cohortAsOfNote` reuse Campaign
  Evidence's `campaignCohortAsOf` — no second formatter. The one gate,
  `cohortCountHtml`, appends "measured as of …" to every published count it
  prints (every KPI, table cell and drawer that goes through it); the two
  Countries-page paths that print through `rowMetricHtml` (the SQL KPI and
  the regional mix) append it explicitly.
* `scripts/audit_marketing_outcome_cohorts.py`
  `mart_stale_disclosure_problems`, called by `audit_mart_window` as
  `stale_disclosed`: a published payload over a `source_stale` funnel without
  `stale: true`, `cohort.as_of` and `metadata.as_of` is a violation (as is a
  published cohort with no declared window end — MINOR 1).

Evidence: `test_m1_a`–`test_m1_d` and PG `test_08` / `test_08b` fail on
`e7b5bc4` (no `cohortAsOfNote`, no `mart_stale_disclosure_problems`, no
`stale` / `as_of` on the block). Under the mutation that removes
`cohortAsOfNote(cohort)` from `cohortCountHtml`, `test_m1_a`, `test_m1_b`
and PG `test_08` fail.

### 10.2 MAJOR 2 — two different "SQLs" on the Dashboard

* **Deals.** The legacy stage "SQLs Not Yet Closed-Won" is out of the funnel:
  `_build_funnel` ends at Closed Lost, and the backlog is its own
  `lead_status_backlog` block (`_build_lead_status_backlog`), rendered as its
  own panel (`renderDealLeadStatusBacklog`) under the label "Qualified (lead
  status) · no won deal" with "A different metric from the funnel above".
  The KPI key is `lead_status_qualified_no_won_deal` (was
  `sqls_not_closed_won`, which named the cohort's metric over a lead-status
  count). The Watch / Investigate cards, the row statuses ("Qualified (lead
  status) — no won deal yet", "Aging qualified lead — needs sales review",
  "Qualified-lead producer"), the linkage reasons, the Source → Pipeline rate
  column ("Qualified → Customer", reason `R_LEAD_TO_CUSTOMER_INCOMPATIBLE`)
  and the backlog table's headers ("Contact Created" / "Days Since Created":
  the reader windows on `contact_created_at`) no longer say "SQL".
* **The funnel's SQL stage under a withheld cohort** carries
  `sql_status` / `sql_reason`, status "SQLs withheld", and renders through
  `cohortCountHtml` with the response's cohort: "Withheld" and the reason —
  never "Unavailable / Stage unavailable". Its label is the cohort label.
* **Channels.** Every count it prints is the legacy lead-status population
  (the source groups): the KPI, the quality panel and its axis and tooltip,
  the platform matrix, the channel list, the drawer and the Watch card say
  "Qualified (lead status)"; the payload declares
  `kpis.total_sqls_label` / `total_sqls_metric_family`.

Evidence: `test_m2_a`–`test_m2_d` render the REAL Deals and Channels payloads
(built by production code over the 139 / 136 fixtures) with the real
renderers, including `renderDealFunnel`, and assert no `\bSQLs?\b` over a
legacy count. All four fail on `e7b5bc4` (the funnel's legacy stage, "SQLs"
on the Channels KPI, "Stage unavailable" on the withheld SQL stage). Writing
`test_m2_b` found three more occurrences the audit had not listed (the
backlog panel sub-line, the source table's rate reason, the backlog
definition text) — fixed, not exempted.

### 10.3 MINOR 1 — window mismatch

* The cohort block declares `window: {start_date, end_date}`; every cohort
  label and KPI sub-line appends `cohortThroughSuffix(cohort)` ("through
  24 Nov 2026" on the campaign / deal views, "through 23 Nov 2026" on the
  country views).
* §5's "same total" claim is corrected (two groups, and why).
* `cohort_sql_parity.campaign_view` (`_cohort_sql_parity_campaign_view`)
  compares the campaign view like for like: the mart's campaign
  `summary.sqls`, the Overview headline, and Campaign Evidence built with
  `bounds=` the campaign view's days. The other campaign-view Dashboard pages
  are already one identity with the mart. A broken group is a
  `cohort_sql_parity_broken` violation.

Evidence: `test_n1_a`, `test_n1_b` and PG `test_10` fail on `e7b5bc4`.

### 10.4 MINOR 2 — the Overview SQL delta on an open window

An acquisition cohort keeps maturing after its window opens; a current
quarter or YTD count against a closed prior period reads as a fall it is
not. `cohortDeltaChip(key, pc, d)` shows the chip only when the cohort is
published AND `d.window.is_closed_window === true`; otherwise "No comparison
· window still maturing" with `COHORT_MATURITY_NOTE` as its title. All four
Dashboard SQL KPI cards use it.

The PR-ADS-177A rewrite of
`152b::test_dashboard_ga_source_card_has_no_campaign_attributable_delta`
required `dashDeltaChip` under a published cohort alone. That pinned the
defect this finding names, so it now requires `cohortDeltaChip("sqls", pc, d)`,
asserts the card calls no raw `dashDeltaChip`, and asserts the helper gates on
both the publication and `is_closed_window`.

Evidence: `test_n2` fails on `e7b5bc4` (no `cohortDeltaChip`).

### 10.5 MINOR 3 — untested wiring

* `test_n3_a` runs the real `run()` with every builder replaced by a stub
  and asserts `report["business_windows"]` covers every window and all seven
  payloads. It passes on `e7b5bc4` (the wiring was right); it fails under
  either mutation: dropping `dashboard/deals` from `_build_mart_payloads`, or
  auditing only the first window.
* `test_n3_b` covers `_build_residual` with no mart residual row: 0 under a
  published cohort, None otherwise. It fails under `… = 0` and under
  `… = None`.
* PG `test_09`: an approved manual mapping (`upsert_campaign_identity`,
  "Mystery" → `brand - uk`) moves the cohort contact onto the mapped row, with
  conservation intact. It passes on `e7b5bc4`; under the mutation that makes
  `_mart_label_resolver` ignore the resolution map, `test_09` is the ONE case
  in the module that fails (16 others pass): the mutation that survived PG
  now dies.

### 10.6 MINOR 4 — key names

The Overview's `kpis.google_ads_source_sqls` / `campaign_attributable_sqls`
held the cohort under the names of two OTHER scopes
(`canonical_contact_outcome_service.SCOPE_GOOGLE_ADS_SOURCE` and the
resolved-identity scope `analysis/revenue_scope.py` names). They are now
`google_ads_cohort_sqls` (+ `_scope`, `_status`) and
`campaign_row_cohort_sqls`. The Keyword page's `sql_reconciliation` still
carries the real `google_ads_source_sqls` / `campaign_attributable_sqls`
scopes under those names — correctly. Evidence: `test_n4` fails on
`e7b5bc4`.

### 10.7 MINOR 5 — the revenue-attribution audit endpoint

`build_revenue_attribution_audit` judged lead-window safety on the legacy
`leads` table, which feeds no window figure since PR-ADS-177A. It now
**points at the cohort verdict**: `lead_window_safe` is the window's cohort
being `published` (read from the same mart block the pages read), and a
blocker names the cohort's status and reason. The legacy table's grain is
reported as `legacy_leads_table_window_safe` and judged on nothing. The
pseudo / email campaign-row blockers are kept (conservative: they can only
make the verdict more cautious). Evidence:
`test_revenue_attribution_audit::test_audit_verdict_unsafe_when_lead_grain_unsafe`
(rewritten: a withheld cohort makes it UNSAFE) and the new
`test_audit_legacy_leads_grain_alone_no_longer_decides_the_verdict` fail on
`e7b5bc4`.

### 10.8 MINOR 6 — the quarter-day-1 flake

`154c::test_f2_7` and `154c_f3::test_f3_27` ran the parity CLI at the wall
clock with contacts created at noon on the current quarter's first day. On
that day the settled country view is an empty range and the contacts lie
after the clock, so "fully healthy" was not. Both now pin the CLI's instant
(`audit_all_windows(…, now=…)`, 2026-06-22 12:00Z) and create the contacts
inside its quarter. Evidence, with the fake-clock harness
(`FAKE_NOW=2026-10-01T08:00:00+00:00`): both fail on `e7b5bc4`
(`rc == 1`, `not_evaluable`; `canonical_source_unavailable`) and both pass
after the fix, at that instant and at the wall clock.

### 10.9 MINOR 7 — docs

§9.1's counts are corrected (54 cases, 52 fail); §7 records that
`cohort_sql_parity` runs only from the manual CLI (§8 lists it as a
follow-up); the `AUDITS` description of the marketing-cohort audit in
`scripts/audit_platform_certification.py` now says it covers every mart /
Dashboard leads and SQLs count.

### 10.10 Existing tests changed in F1

No assertion was weakened, skipped or quarantined.

| Test | Why |
| --- | --- |
| `152b::test_dashboard_headline_is_google_ads_source_scope`, `::test_app_renders_google_ads_source_sql_headline`, `::test_app_renders_reconciliation_required_on_mismatch`, `::_ga_source_card` | MINOR 4 rename: the same assertions over `google_ads_cohort_sqls`; the first also asserts the old key is gone |
| `152b::test_dashboard_ga_source_card_has_no_campaign_attributable_delta` | MINOR 2: it pinned the delta on an open window (the defect); see §10.4 |
| `152b::test_dashboard_null_campaign_attributable_shows_unavailable_not_rate` | MINOR 4 rename (`campaign_row_cohort_sqls`); also asserts the old key is gone from the card |
| `153c::test_campaign_attributable_sqls_stay_explicitly_named` | MINOR 4 rename |
| `139::test_sql_no_deal_preserved_and_excludes_customers`, `::test_sql_no_deal_unavailable_when_source_down`, `::test_sql_no_deal_unavailable_when_revenue_disconnected` | MAJOR 2: the key is `lead_status_qualified_no_won_deal`; the row status is "Aging qualified lead — needs sales review". The first also asserts the backlog block carries the same count. Values unchanged |
| `177a::test_05_the_audit_flags_every_count_beside_a_withheld_cohort` (one parameter), PG `test_04` | MINOR 4 rename of the Overview key the exposure scan names |
| `revenue_attribution_audit::_audit`, `::test_audit_date_grain_health_fields`, `::test_audit_verdict_unsafe_when_lead_grain_unsafe` | MINOR 5: the fixture feeds the cohort through the real `assess` (healthy unless the case passes another sync row); the lead date field is the cohort's; "lead window unsafe" is a withheld cohort |
| `154c::test_f2_7…`, `154c_f3::_cli_both_formats` | MINOR 6: a fixed instant instead of the wall clock (§10.8). Assertions unchanged |
