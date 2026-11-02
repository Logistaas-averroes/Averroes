# 48 — Customer and Closed-Won Revenue Reader Cutover

**PR-ADS-161E.** Every production page, API route and service that publishes
closed-won deals, customers, closed-won revenue, a spend ratio or
SQL-to-customer conversion now reads the canonical owner and republishes its
verdict. Attribution ROAS and CAC are unavailable everywhere (§4). This PR
adds no classifier, population, won predicate, customer identity rule or
revenue gate. It removes the readers that had their own.

| Owner | Role |
| --- | --- |
| `analysis/closed_won_truth.py` | states, membership, publication; reader contract (`metric`, `subset_outcomes`, `derive_period_revenue_per_ad_spend`, `derive_period_spend_per_customer_won`, `certified_roas`, `certified_cac`, `incompatible_rate`) |
| `services/canonical_customer_revenue_service.py` | per-window truth (`get_closed_won_truth`, `window_view`) over one snapshot |
| `services/canonical_revenue_service.py` | the ONE snapshot (`one_snapshot`, `single_snapshot`), `load_won_deals`, `subset_metrics`, `reader_row` |

## 1. Definitions (unchanged)

* **Won predicate:** `hs_is_closed_won IS TRUE`. Stage `326093516` is a
  cross-check; a disagreement withholds the affected totals, never filters.
* **Closed-won deals:** the won deals whose canonical close date falls in the
  window. All Time also holds the undated won deals. A close later than now is
  in no window.
* **Customer:** a distinct proven HubSpot company
  (`hubspot_deal_company_association`, docs/46 §5). Never a deal, a contact, a
  domain, a name or a campaign. **Closed-won deals, customers and revenue are
  three metrics.**
* **Revenue:** the sum over distinct won deals with proven USD. Zero is a valid
  value. Missing is not zero.
* **Windows:** close-date cohort. Evidence windows are Europe/London calendar
  days. Business windows are `analysis.business_windows`.
* **Marketing SQLs** stay on the acquisition cohort (docs/44). They are never
  divided by a close-date outcome.

## 2. The reader contract

A page never decides whether a figure publishes. It calls:

* `load_won_deals(window)` — the window's canonical population, or
  `available: False` with the canonical `status` and `reason` when the deal
  count is withheld (an undated or conflicting won deal) or the ledger is
  unreadable. The result also carries `publication`, `canonical_metrics`,
  `canonical_window`, `cohort` and `company_ids_by_deal`.
* `reader_row(base, rows, spend_usd=…)` — a row's (campaign, country, source
  group, label, scope, summary) `closed_won_deals`, `customers`,
  `won_revenue`, `period_revenue_per_ad_spend` and
  `period_spend_per_customer_won`, plus `metrics`: each as
  `{value, status, reason}`. `roas` and `cac` are always `null`, and
  `metrics.roas` / `metrics.cac` are always `unavailable` with the cohort
  reason.

A subset inherits the window's verdict. A campaign's revenue publishes only
when the window's revenue publishes; its customers only when the window's
customers publish. **Customers are not additive.** Each row counts distinct
companies, and no page sums rows into a customer total.

**One snapshot per page.** Each page builder is decorated with
`@single_snapshot`. Every closed-won read inside it shares one REPEATABLE READ,
READ ONLY universe, so a page cannot disagree with itself mid-sync.

## 3. Publication on the page

| State | API | UI |
| --- | --- | --- |
| Published, nonzero | `value`, `status: published` | the exact value |
| Published zero | `0`, `status: published` | `0` |
| Withheld | `value: null`, `status: withheld`, `reason` | **Withheld** and the reason |
| Unavailable | `value: null`, `status: unavailable`, `reason` | **Unavailable** and the reason |

The UI never shows a zero, a dash, a lower bound, a stale value or the number
with a suffix for a withheld metric. `canonicalMetricHtml` and `rowMetricHtml`
in `static/app.js` render these states. `rowMetricHtml` trusts the status, not
the value: a withheld metric that wrongly carried a number still renders the
word.

**Withheld means absent.** When the window withholds a family (deals,
customers or revenue), no page publishes a figure of it anywhere: no row,
subset, bucket, proof list or identity map. A row whose own metric is withheld
carries no sibling figure for it, and no proof-deal list. The audit runs all
ten page builders (the Mart, attribution, Revenue by Source, Unit Economics and
six dashboards) for every business window and walks each payload
(`withheld_not_reconstructable`). The canonical `confirmed_customer_lower_bound`
is not republished by any page.

The one labelled partial kept on purpose is **"Known revenue from N priced
deals"** (PR-ADS-155). It names its own denominator and is never shown as the
total. Negative amounts are excluded from it, because `summarize_deals` now
uses the canonical `revenue_is_proven`.

Headline revenue KPIs render through `canonicalValueHtml`. A CRM withholding
reads "Withheld" with the reason, and gives admins a View blockers link; it is
not a bare "Unavailable".

## 4. Derived metrics

**Attribution ROAS and CAC are unavailable** (decision 1). A closed-won
outcome is dated by its close and spend by the day it was spent. In a long B2B
sales cycle, a deal closing this window was usually acquired by spend in an
earlier one. So close-date revenue (or customers) over same-window spend is
not an acquisition-cohort ratio, and it is never published as one.

| Metric | API field | Rule |
| --- | --- | --- |
| Attribution ROAS | `roas` (always `null`); `metrics.roas` | `unavailable`: `roas_requires_revenue_and_spend_in_one_acquisition_cohort`. Stays unavailable until revenue and spend use one acquisition cohort and a compatible scope. |
| CAC | `cac` (always `null`); `metrics.cac` | `unavailable`: `cac_requires_spend_and_customers_in_one_acquisition_cohort`. Stays unavailable until spend and distinct customers are joined through one acquisition cohort and attribution scope. |
| **Period Revenue / Ad Spend** | `period_revenue_per_ad_spend` | `derive_period_revenue_per_ad_spend`: published revenue from deals CLOSED in the window ÷ that window's Google Ads USD spend. Withheld when revenue is withheld, with `blocked_by`. Unavailable when spend is unknown or zero, or when the scopes differ. |
| **Spend per Customer Won This Period** | `period_spend_per_customer_won` | `derive_period_spend_per_customer_won`: that window's spend ÷ **canonical customers** whose deals closed in it, never won deals. It counts repeat and expansion companies. Withheld when customers are withheld. |
| SQL-to-customer, revenue-per-SQL, lead-to-customer | — | Unavailable (`*_needs_one_acquisition_cohort_and_scope`). No canonical service supplies SQLs or leads and customers over one acquisition cohort and one attribution scope. |
| Average deal value | `avg_deal_value_usd` | The published revenue total ÷ every won deal in the same population. |

Each period ratio carries:

* `ratio_kind: non_cohort_operational_ratio`;
* `cohort_basis: close_date_outcomes_over_same_window_spend`;
* the disclosure "Non-cohort operational ratio. Closed-won outcomes may
  originate from earlier advertising periods."

The UI shows the disclosure beside every value, header and KPI.

Dashboard rows carry the same names. Examples:

* `period_revenue_per_ad_spend` with `period_ratio_available`;
* `geo_period_revenue_per_ad_spend`;
* `google_ads_period_revenue_per_ad_spend`;
* `period_ratio_status` and `period_ratio_reason` on Revenue by Source.

Kept on purpose: the route ids (`#/roas-campaigns`, `#/roas-countries`) and
the source-health booleans (`campaign_roas_available`,
`country_roas_available`, `country_roas_unblockable`). They are not ratio
values. The spend-truth and source-classification readiness flags are renamed
`period_ratio_available`.

**Where the ratios are computed.** Reader-contract rows (Mart, attribution,
Revenue by Source, Unit Economics) use the two `derive_*` functions and carry
the metadata. The Campaigns, Countries and Channels dashboard rows divide the
same published inputs themselves and carry no metadata; they return `null`
rather than 0 for zero revenue. Zero spend makes either ratio unavailable
(`no_google_ads_spend_in_window`), never "$0". The page titles read "Campaign Revenue & Spend" and "Country Revenue &
Spend".

The window verdict (`get_closed_won_truth`) carries no spend, so it publishes
neither period ratio and marks ROAS / CAC `not_published` with the cohort
reasons.

**Scope rule.** The period ratios publish only for `google_ads_source` and
`campaign_attributable`. Otherwise they are unavailable with
`numerator_and_denominator_scopes_differ`; for example, Unit Economics at
`all_source`.

**Per-campaign placement.** The window is evaluated with Campaign Evidence's
own resolver (`ccr.campaign_resolver`, built once per snapshot). The page then
republishes that canonical partition. A Google Ads won deal that resolves to no
campaign (no label, an unmapped label, or no resolver) is in
`google_ads_unplaced`. While one exists:

* every **per-campaign** deal count, revenue, customer count and period ratio is
  withheld with `google_ads_deals_not_placed_on_a_campaign`
  (`campaign_placement_gate`);
* the `campaign_attributable` **scope total** is still published, because it is
  exact for its own scope;
* the account-level Google Ads figures are not affected.

**A withheld count is never judged.** A row whose deal count is withheld gets
the verdict `outcome_withheld` (Campaign / Country Revenue & Spend) or the status "Closed-won outcome
withheld" (dashboards), never `waste`, "spend without proof" or "no won
deals". Decision cards say that the outcome is withheld. Charts leave the row
off instead of plotting it at 0.

## 5. "View blockers"

When a customer or revenue metric is withheld for a CRM reason, an
**administrator** sees **View blockers**. It links to
`#/health?cte_window=<type>/<key>&cte_family=<family>`. The CRM Truth Exceptions
workbench (docs/47) opens with that window and family preselected.

| Withholding reason | Family |
| --- | --- |
| `customer_identity_*`, `company_associations_unreadable` | `customer_identity` |
| `closed_won_deals_missing_proven_usd_amount`, `closed_won_deals_missing_amount` | `revenue_proof` |
| `won_deals_without_close_date`, `won_flag_and_stage_disagree` | `won_deal_membership` |

Outage reasons (unreadable ledger, unproven coverage) get no link: there is no
CRM record to fix. A **non-admin** sees the same status and reason with no
link, because the workbench and its API are admin-only. Nothing here repairs
CRM data or writes to HubSpot.

## 6. Migrated readers

The full matrix is `INVENTORY` in
`scripts/audit_customer_revenue_reader_cutover.py`, rendered in the PR
description.

* **Revenue Decision Mart:** `closed_won_deals`; canonical `customers` (+ status
  and reason); attributed subset via `reader_row`; `google_ads_source_*` scope
  totals; `metrics` with each verdict. When either side withholds, the
  revenue metric carries the canonical status and reason.
* **Revenue attribution** (Campaign / Country Revenue & Spend): `reader_row`
  per campaign, country and summary, plus the placement gate.
* **Revenue by Source:** `reader_row` per group, channel and platform. The
  "Customer Rate" column (won deals ÷ leads) is retired.
* **Mapping review, Unit Economics:** `reader_row`. CAC is unavailable;
  Spend per Customer Won This Period is over canonical customers.
* **Dashboards:**
  * Overview, Revenue, Channels, Campaigns and Countries: deals and customers
    are separate. Channel, campaign and country rows count won deals. The
    country residual is the Google Ads-source total minus the country rows
    (it used to subtract from the all-source total).
  * Deals: "SQLs not closed-won" reads canonical won-deal contacts from the
    snapshot, not `gclid_attribution.has_closed_won_deal`.
* **Cross-page parity:** `closed_won_deals` and `customers` are two identities.
* **Removed:** `revenue_integration_connected()`, an existence check over
  `gclid_attribution`. Readiness reads `canonical_revenue_available` and
  `canonical_revenue_status`.
* **Withheld legacy routes:**
  * `/api/reports/roas/campaigns|countries` and `/api/reports/roas/snapshots*`
    publish no figure (`legacy_local_revenue_not_canonical`).
  * `/api/attribution/gclid-readiness` withholds every local-file deal count.
    Direct plus without-GCLID would otherwise re-add to the won-deal count.
  * `/api/mailchimp/audit` (admin) withholds every closed-won outcome figure
    (`legacy_deal_source_attribution_not_canonical`). Its coverage figures
    stay.

## 7. Deferred readers

| Reader | Why deferred |
| --- | --- |
| `/api/attribution/confidence-summary` | Publishes attribution tier counts only, with no deal, customer, revenue or ratio value. |
| `/api/audit/revenue-truth` | An admin shadow-reconciliation diagnostic that compares the ledger to the legacy ledgers. It is not a page publishing a total. |
| Daily legacy snapshot job (`scheduler/daily.py`) | Still writes a local snapshot file (no external write). Every route that serves it withholds the figures. |
| Admin revenue-recovery job report (`revenue_recovery_service`) | Counts what one recovery run processed. It is a job report, not a window's business total. |
| `source_attribution_health_counts` | Classification-health counts (ambiguous or unclassified deals) on Revenue by Source. They are a data-quality signal, not an outcome. |

**Known limitations.**

* An All Time trend cannot place an undated won deal in a bucket. The All Time
  total holds it, so the trend buckets can sum to less than the total.
* A Google Ads-source deal whose label is approved as `not_google_ads` is in no
  country row. It therefore falls in the "no country" residual.

## 8. Audit

`python -m scripts.audit_customer_revenue_reader_cutover [--json]`. It reads
the snapshot, runs the ten page builders for every business window, and
fingerprints the closed-won tables before and after (and also builds the
Mart's source and country views, for check 18). "No database writes" is
measured, not declared. It runs 18 checks:

* the inventory is complete; customer and revenue readers are canonical;
* no won-deal-as-customer label; no legacy table gate;
* windows reconcile (reader = canonical = an independent derivation);
* status and reason are serialized;
* withheld values are absent and not reconstructable; zero is distinguishable;
* period ratios inherit a withheld input; SQL-to-customer is not mixed;
* All Time holds the undated deals and no future-dated one;
* admin links resolve; non-admins get no link;
* no external call and no write; the derivation is independent;
* **no period ratio is named ROAS or CAC** (`no_period_ratio_named_roas_or_cac`).
  It walks every page payload and fails on:
  * a number, a digit-bearing string or a numeric descendant under any key
    naming ROAS/CAC. That covers any case, camelCase and the synonyms
    `return_on_ad_spend` / `customer_acquisition_cost`;
  * a `roas`/`cac` metric whose status is not `unavailable`/`not_published`;
  * a row naming ROAS/CAC by value with a number;
  * any string, in a dict or a list, that calls something ROAS or CAC, outside
    the exact certified-unavailable phrasings and LTV/CAC;
  * a period ratio under another name or without its disclosure.

  It also reads `static/app.js` and `static/index.html` and fails on:
  * visible ROAS/CAC text (any case variant or synonym). Only the exact
    allowed tokens are exempt, never a whole line;
  * a certified label rendered beside anything but its own metric;
  * a missing period label or disclosure.

  Tests 38–45 are its counterfactuals; the UI at `64b2ce3` has 97 violations.

Exit codes: **0** every reader contract holds, though metrics may still be
withheld for CRM reasons, which are reported and are not failures. **1** a
contract is broken. **2** unavailable, or a page check could not run.
