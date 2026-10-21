# 47 — CRM Truth Exceptions Workbench (PR-ADS-191)

**Status:** current. Depends on PR-ADS-161D / 161D2 (F1–F3), docs/46.

## 1. What it is

The canonical customer and closed-won services already withhold a customer or
revenue total when the evidence does not support it. This workbench names the
exact HubSpot won deals responsible, so the CRM team can correct them.

It is a **visibility and triage** surface:

- It repairs nothing.
- It writes nothing to HubSpot.
- It infers no company, currency or amount.
- It changes no publication gate.

**A CRM finding is a data-quality fact about a HubSpot record, not a software
failure.** An affected total stays withheld until the record is corrected in
HubSpot and the next association scan or deal sync reads it back.

## 2. Where it lives

| Piece | Location |
| --- | --- |
| Read model | `services/crm_truth_exceptions_service.py` |
| JSON API | `GET /api/audit/crm-truth-exceptions` (admin cookie or `ADMIN_API_TOKEN` bearer) |
| CSV export | `GET /api/audit/crm-truth-exceptions.csv` (same access control) |
| UI | System Status (`#/health`, admin-only) → **CRM Truth Exceptions** panel |
| Audit | `python -m scripts.audit_crm_truth_exceptions [--json]` |

## 3. No second classifier

Every verdict is consumed from its canonical owner, never restated:

| Question | Owner |
| --- | --- |
| Won population, snapshot, ledger coverage | `canonical_revenue_service.load_closed_won_universe` (one REPEATABLE READ, READ ONLY transaction) |
| Window membership, publication verdicts | `canonical_customer_revenue_service.get_closed_won_truth` |
| Customer identity, amount, currency, close date, won-definition agreement | `analysis.closed_won_truth` |
| Why a deal's evidence is stale | `company_identity_inputs` → `stale_reasons` (PR-ADS-191) |
| Scan currency and certified set | `association_scan_current`, `scan_certification_sets` |
| Unproven-currency reason | the ledger's own `analysis.deal_currency` reason, verbatim (as `/api/audit/missing-deal-amounts` shows it) |
| HubSpot link | `canonical_revenue_service.hubspot_portal_id` + the deal record URL; no portal configured → no link |

A deal is attributed to a window only through that window's own
`membership.deal_ids`. The truth service publishes those ids only when the
window's deal count is published, so where the deal count is withheld the
workbench attributes nothing to the window either: withheld means absent here
too. That window's blocker is then the deal-count reason (an undated won deal,
or a won-definition conflict), which the workbench names.

The only canonical-owner change in this PR: `company_identity_inputs` now also
returns `stale_reasons`, which names why each stale deal is stale. The `stale`
set the publication gate reads is exactly its keys, so it is unchanged.

## 4. Reason vocabulary

| Family | Reason codes | Metrics affected |
| --- | --- | --- |
| `customer_identity` | `unresolved_no_company`, `ambiguous_multiple_companies`, `association_unavailable`, `latest_association_attempt_failed`, `deal_modified_after_association_observation`, `not_certified_by_latest_complete_scan` | customers |
| `revenue_proof` | `no_amount`, `unknown_currency`, `home_currency_unverified`, `no_close_date_for_fx`, `no_fx_rate_for_close_date`, `currency_reason_not_recorded`, `negative_amount`, `invalid_amount` | revenue_usd |
| `won_deal_membership` | `missing_close_date`, `won_flag_true_in_another_stage`, `won_stage_but_won_flag_not_true` | closed_won_deals, revenue_usd, customers |

Every code is defined in `analysis/closed_won_truth.py`, `analysis/deal_currency.py`
or `services/canonical_revenue_service.py`; the workbench defines no synonym.

- **Row identity** is `deal_id:family:reason`. One deal with two blockers is
  two rows, and is counted once in `deals_with_any_exception`.
- **`not_certified_by_latest_complete_scan`** appears only when a complete scan
  exists and its certified set differs from the won population. With no complete
  scan at all, a single dataset-level blocker says so, rather than one row per
  won deal.
- **Zero revenue** is proven revenue, not missing revenue.

**Revenue rule.** The workbench follows the closed-won publication gate
(`cwt.revenue_is_proven`), which also refuses negative and invalid amounts.
PR-ADS-155's missing-amount report does not refuse those, so it can name fewer
deals. The audit proves the workbench's list contains every deal PR-ADS-155
names, and that any extra deal is a negative or invalid amount.

## 5. States

| `status` | Meaning |
| --- | --- |
| `certified_no_findings` | Scan current, no blockers |
| `certified_with_findings` | Scan current, CRM findings listed |
| `source_stale` | The latest complete scan started more than `association_scan_max_age_hours` ago |
| `scan_not_current` | The latest full scan is not successful and complete, or its certified set or recorded population does not match the current won population |
| `evidence_unavailable` | The ledger, its coverage or the association evidence cannot be read or proven |

**Unavailable is never zero:**

- **Ledger unreadable, or ledger coverage unproven:**
  - the JSON route returns `available: false`, `rows: null` and `totals: null`;
  - the CSV route returns 503;
  - the UI renders "Evidence unavailable", never "0 issues".
- **Association evidence unreadable:** the identity totals are `null` and the
  revenue rows are still listed.

## 6. API contract

`GET /api/audit/crm-truth-exceptions`

| Parameter | Behaviour |
| --- | --- |
| `window_type` + `window_key` | Both or neither, e.g. `business` + `ytd`. Any window from `all_windows()`. |
| `family`, `reason` | Must be known values; a reason must belong to its family |
| `q` | Substring of the deal id or name |
| `limit` | 1–1000, default 100 |
| `offset` | ≥ 0 |

An invalid filter returns **400**, never an empty list.

The response carries:

- `status` and `generated_at`;
- `source_freshness` and `association_scan`;
- `won_deal_population` and `dataset_blockers`;
- `totals` by family and reason;
- `windows`: each window's publication verdict, its blocker counts, the truth
  service's own counts and whether each withheld verdict is explained;
- `reason_vocabulary` and `pagination`;
- `rows`, ordered by `(deal_id, family, reason)` so pages are stable.

**CSV:** the same filters and the same read model (one snapshot), every row,
UTF-8, stable columns (`CSV_COLUMNS`). A cell beginning with `=`, `+`, `-`, `@`,
a tab or a carriage return is prefixed with an apostrophe, so a spreadsheet will
not execute it.

**Fields that are never invented:**

- **Company names** are always `null`. Association ingestion stores HubSpot
  company ids only, and a name is never taken from a deal, domain or contact.
- **`resolved_company_id`** is set only when exactly one company is proven.

## 7. The audit

`python -m scripts.audit_crm_truth_exceptions` proves:

- **Rows:** every row's reason belongs to its family and has an explanation.
- **Withheld verdicts:** every withheld customer or revenue verdict, in every
  window, is explained by a row or a dataset blocker.
- **Published totals:** no published total has an unresolved blocker among its
  members.
- **Window counts:** each window's blocker counts equal the truth service's
  `unresolved_customer_identity`, `stale_customer_identity` and
  `deals_without_proven_usd`.
- **Independent readers:** identity rows equal the coverage audit's independent
  derivation from the raw association rows, and revenue rows contain every deal
  PR-ADS-155 names.
- **Serialization:** paging, every filter and the CSV export lose and duplicate
  no row.
- **Existing audits:** `audit_customer_closed_won_truth` and
  `audit_deal_company_association_coverage` hold.

Exit codes:

- **0:** the workbench represents the evidence truthfully. CRM findings may
  still be listed.
- **1:** a contract is violated.
- **2:** the evidence, or an embedded audit, is unavailable.

## 8. Deferred

**"View blockers" links from withheld metrics.** No production page shows the
canonical customer metric yet (PR-ADS-161E migrates readers). Revenue-withheld
messages exist only on legacy readers, so linking from them would mean
unrelated page refactors. The workbench is reachable from System Status.
