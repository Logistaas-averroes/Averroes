# 48 — Customer and Closed-Won Revenue Reader Cutover

**PR-ADS-161E.** Every production page, API route and service that publishes
closed-won deals, customers, closed-won revenue, ROAS, CAC or SQL-to-customer
conversion now reads the canonical owner and republishes its verdict. This PR
adds no classifier, population, won predicate, customer identity rule or
revenue gate. It removes the readers that had their own.

| Owner | Role |
| --- | --- |
| `analysis/closed_won_truth.py` | states, membership, publication; reader contract (`metric`, `subset_outcomes`, `derive_roas`, `derive_cac`, `incompatible_rate`) |
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
  `won_revenue`, `roas` and `cac`, plus `metrics`: each as
  `{value, status, reason}`.

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

| Metric | Rule |
| --- | --- |
| ROAS | `derive_roas`: published revenue ÷ the same window's Google Ads USD spend. Withheld when revenue is withheld, with `blocked_by`. Unavailable when spend is unknown, zero, or the scopes differ. A **period** ratio (`cohort_basis: close_date_outcomes_over_same_window_spend`). |
| CAC | `derive_cac`: Google Ads USD spend ÷ **canonical customers**, never won deals. Withheld when customers are withheld. |
| SQL-to-customer, revenue-per-SQL, lead-to-customer | Unavailable (`*_needs_one_acquisition_cohort_and_scope`). No canonical service supplies SQLs or leads and customers over one acquisition cohort and one attribution scope. |
| Average deal value | The published revenue total ÷ every won deal in the same population. |

The window verdict (`get_closed_won_truth`) carries no spend, so it still
publishes neither ratio. Its reasons now say they are derived per scope by the
reader contract.

**Scope rule.** ROAS and CAC publish only for `google_ads_source` and
`campaign_attributable`. Otherwise they are unavailable with
`numerator_and_denominator_scopes_differ`; for example, Unit Economics at
`all_source`.

**Per-campaign placement.** The window is evaluated with Campaign Evidence's
own resolver (`ccr.campaign_resolver`, built once per snapshot). The page then
republishes that canonical partition. A Google Ads won deal that resolves to no
campaign (no label, an unmapped label, or no resolver) is in
`google_ads_unplaced`. While one exists:

* every **per-campaign** deal count, revenue, customer count, ROAS and CAC is
  withheld with `google_ads_deals_not_placed_on_a_campaign`
  (`campaign_placement_gate`);
* the `campaign_attributable` **scope total** is still published, because it is
  exact for its own scope;
* the account-level Google Ads figures are not affected.

**A withheld count is never judged.** A row whose deal count is withheld gets
the verdict `outcome_withheld` (ROAS pages) or the status "Closed-won outcome
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
* **Revenue attribution** (ROAS by Campaign and by Country): `reader_row` per
  campaign, country and summary, plus the placement gate.
* **Revenue by Source:** `reader_row` per group, channel and platform. The
  "Customer Rate" column (won deals ÷ leads) is retired.
* **Mapping review, Unit Economics:** `reader_row`. CAC is over canonical
  customers.
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
| Daily ROAS snapshot job (`scheduler/daily.py`) | Still writes a local snapshot file (no external write). Every route that serves it withholds the figures. |
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
fingerprints the closed-won tables before and after. "No database writes" is
measured, not declared. It runs 17 checks:

* the inventory is complete; customer and revenue readers are canonical;
* no won-deal-as-customer label; no legacy table gate;
* windows reconcile (reader = canonical = an independent derivation);
* status and reason are serialized;
* withheld values are absent and not reconstructable; zero is distinguishable;
* derived ratios inherit a withheld input; SQL-to-customer is not mixed;
* All Time holds the undated deals and no future-dated one;
* admin links resolve; non-admins get no link;
* no external call and no write; the derivation is independent.

Exit codes: **0** every reader contract holds, though metrics may still be
withheld for CRM reasons, which are reported and are not failures. **1** a
contract is broken. **2** unavailable, or a page check could not run.
