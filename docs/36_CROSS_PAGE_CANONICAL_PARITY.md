# Cross-Page Canonical Parity

**PR-ADS-154C** — the last mandatory source-of-truth unification step.

This document is the parity matrix: which production consumer computes which
metric, from which canonical authority, over which window, and what happens when
that authority is unavailable.

---

## 1. The two ways pages disagree

A page showing a different number is the obvious failure. It is not the dangerous
one, because someone notices.

The dangerous failures look like agreement:

| Failure | What it looks like |
|---|---|
| Two consumers ask about **different date ranges** under the same window name | both say "this quarter", different quarters |
| A consumer **falls back to a legacy provider** and publishes under a canonical label | a plausible number, wrong lineage |
| Two **genuinely different** metrics get compared | the real difference is filed as a bug, and a real bug hides inside it |

All three are addressed below, and all three are asserted by
`python -m scripts.audit_cross_page_canonical_parity`.

---

## 2. One window anchor

Every consumer already called `analysis.business_windows.resolve_window`, so the
**resolver** was shared. The **reference instant** was not:

| Consumer group | Anchored on |
|---|---|
| dashboard services | UTC `now` |
| spend / geo services | Google Ads account day from the database, **falling back to UTC** when absent |
| evidence services | `analysis.account_time.ACCOUNT_TZ`, hardcoded `Europe/London` |

Sharing a resolver while disagreeing about *today* is not sharing. At
**23:30 UTC on 30 June under BST** the account day is already 1 July:

```
window            UTC anchor                 account anchor
current_quarter   2026-04-01 .. 2026-06-30   2026-07-01 .. 2026-07-01    <- different QUARTER
last_quarter      2026-01-01 .. 2026-03-31   2026-04-01 .. 2026-06-30
last_6_months     2025-12-30 .. 2026-06-30   2026-01-01 .. 2026-07-01
ytd               2026-01-01 .. 2026-06-30   2026-01-01 .. 2026-07-01
all_time          None       .. 2026-06-30   None       .. 2026-07-01
```

Every named window diverges, not only the quarterly ones.

**The account day wins.** Google Ads reports a day's cost against the account's
local calendar, so a business window anchored on UTC asks about a range the spend
data does not have. `services.canonical_contract.resolve_canonical_window` is the
one anchor; an unknown or missing zone falls back to `ACCOUNT_TZ`, **never to
UTC** — falling back to UTC is what produced the divergence, and a missing
database row is not a reason to change which day it is.

---

## 3. Canonical authority

| Domain | Authority |
|---|---|
| Google Ads spend / performance | canonical Google Ads API campaign-daily facts |
| Country / geo performance | canonical Google Ads geo facts (coverage-backed) |
| Lead and lifecycle metrics | canonical HubSpot contact funnel |
| Won deals and revenue | canonical HubSpot deal ledger → revenue decision mart |
| Currency normalisation | canonical FX layer, per `spend_date` |
| Date boundaries | `resolve_canonical_window` |

These may **not** independently determine a production total: Windsor · direct
live HubSpot calculations · legacy GCLID attribution tables · legacy deal-source
ledgers · ad-hoc per-page SQL · frontend aggregation of raw records · cached
legacy payloads · silent fallback providers.

---

## 4. Parity matrix

Every page composes `revenue_decision_mart`, which computes the canonical truth
once per window. The per-view `rows` change with the grain; `window`,
`spend_truth` and `summary` do not.

| Page / endpoint | Metric | Window resolver | Canonical source | Legacy dependency | Fallback behaviour |
|---|---|---|---|---|---|
| `/api/dashboard/overview` | `google_ads_spend_usd`, `closed_won_revenue_usd` | `resolve_canonical_window` | mart → canonical spend + deal ledger | none | metric `null` + reason |
| `/api/dashboard/revenue` | `closed_won_revenue_usd`, `customers` | `resolve_canonical_window` | mart → canonical deal ledger | `fetch_lead_daily_series` (trend series only) | metric `null` + reason |
| `/api/dashboard/channels` | channel mix | `resolve_canonical_window` | mart → revenue by source | none | metric `null` + reason |
| `/api/dashboard/campaigns` | campaign spend / ROAS | `resolve_canonical_window` | mart → canonical spend | none | ROAS withheld when denominator unsafe |
| `/api/dashboard/countries` | `won_revenue_usd` (country-attributed), geo ROAS | `resolve_canonical_window` | mart → canonical geo | none | blocked unless the geo gate is ready |
| `/api/dashboard/deals` | pipeline / deal mix | `resolve_canonical_window` | mart → canonical deal ledger | `fetch_sql_lead_details` (detail rows only) | metric `null` + reason |
| `revenue_decision_mart` | `spend_usd`, `won_revenue_usd`, `customers` | `resolve_canonical_window` | canonical spend, geo, funnel, ledger, FX | diagnostic reads only | `unavailable` + gap codes |

### Legacy reads that remain, and what they are

Every legacy-table read performed by production code is registered and
classified in `tests/test_pr_ads_154c_cross_page_parity.py`
(`LEGACY_READ_REGISTRY`). Three classifications exist:

- **`detail_rows`** — row-level detail or a trend series shown beside a
  canonical total, never the total itself;
- **`diagnostic`** — reconciliation, audit or campaign mapping;
- **`readiness`** — a boolean probe carrying no figure.

There is deliberately **no `authoritative` classification**. A production total
may not come from a legacy table, so an entry needing one would be a violation to
fix, not a category to add.

The registry is derived from the repository's own SQL, so adding a
legacy-reading helper and calling it from a page **fails the guard** until it is
classified.

---

## 5. Metrics that are supposed to differ

Comparing these is the mistake, not the difference between them:

| Metric | Scope |
|---|---|
| total business revenue | every closed-won deal, any source |
| Google Ads-attributed revenue | the subset attributable to Google Ads |
| country-attributed revenue | the subset assigned to a real country; the rest is the explicit residual |
| campaign spend | the canonical ROAS denominator |
| country-attributed spend | the part `geographic_view` assigns to a country |
| residual / unallocated geo spend | the governed remainder (PR-ADS-131) |

`DISTINCT_BY_DESIGN` in `services/cross_page_parity_service.py` registers these
pairs with their reasons, and the audit never compares across them.

**Labels must carry the distinction.** Google Ads-attributed revenue is never
labelled "Total Revenue".

---

## 6. The truth contract

Every production metric response carries enough metadata to identify what it is:

```json
{
  "data_source": "canonical.revenue_decision_mart",
  "truth_status": "ready",
  "window": "ytd",
  "window_start": "2026-01-01",
  "window_end": "2026-08-24",
  "timezone": "Europe/London",
  "customer_id": "...",
  "currency": "USD",
  "generated_at": "...",
  "fallback_used": false
}
```

`fallback_used: false` is a **claim**, not a default: the figures came from the
named canonical source, and no legacy provider, cached legacy payload or
page-local recomputation contributed. A consumer that cannot make that claim
publishes `true` — or, better, fails closed with `unavailable_contract` and no
figures at all.

Returning legacy numbers under a canonical label is the specific thing this
forbids.

---

## 7. The legacy `hubspot/deals` scan

The production run that motivated this PR showed HubSpot **429** responses in the
legacy contact-association scan, while the canonical `hubspot/deal_ledger`
completed with `association_failures=0` and `write_failures=0`.

Both statements were true, and side by side they invited the wrong conclusion —
that revenue truth had been degraded. It had not: the two datasets answer
different questions, and only one of them is truth.

The scan is **retained** for migration evidence and reconciliation, and is
registered in `scheduler.incremental_sync.LEGACY_NON_AUTHORITATIVE_DATASETS`. The
classification is stamped onto the dataset **result**, so the JSON itself says:

```json
"hubspot/deals": {
  "status": "...", "authoritative": false, "superseded_by": "hubspot/deal_ledger"
}
```

It holds the GCLID-attributable **subset** of deals, so it can never stand in for
all-source revenue, and a partial result there does not move canonical truth
readiness. Nothing is deleted or tombstoned.

---

## 8. The audit command

```bash
python -m scripts.audit_cross_page_canonical_parity --json
python -m scripts.audit_cross_page_canonical_parity --window ytd
echo $?     # 0 = full parity, 1 = violations, 2 = usage error
```

Read-only: it builds the same service payloads the API serves, contacts no
external platform and writes nothing.

**Parity is exact, not within a tolerance.** A tolerance answers "are these close
enough to ignore", which is how disagreements survive. Reconciliation between two
*different* sources has a tolerance; two renderings of the *same* canonical
figure do not.

### Violation codes

| Code | Meaning |
|---|---|
| `consumer_values_differ` | two consumers answered the same question differently |
| `consumer_windows_differ` | same window name, different date range |
| `consumer_window_missing` | a built consumer published no complete window |
| `consumer_metric_missing` | a registered consumer published no value while others did |
| `metric_contract_invalid` | missing/wrong provenance: source, scope, status, fallback or window |
| `metric_contract_inconsistent` | consumers disagree on currency or customer identity |
| `agreement_on_unproven_coverage` | unanimous, over coverage nobody proved |
| `legacy_fallback_used` | a consumer declared `fallback_used: true` |
| `legacy_source_supplied_production_total` | a legacy provider backed a published total |
| `canonical_source_unavailable` | no consumer could publish the metric |
| `consumer_raised` | a consumer failed to build |

### Provenance is checked, not echoed (PR-ADS-154C-F1)

The first version printed the `canonical_source` the **registry expected** and
called that provenance — a claim about the audit, not about the number. A page
could read anything at all and the audit would echo the source it wished for.

Every audited response now publishes `metric_truth.<metric_identity>`, built by
`services.canonical_contract.metric_contract`, stating per metric its
`data_source`, `scope`, `truth_status`, window, `timezone`, `customer_id`,
`currency` and `fallback_used`. The audit checks each declaration against the
registry. A missing contract is a failure: silence is not proof that the right
source was used.

A single response-level `data_source` cannot describe the Overview, which
publishes Google Ads spend **and** HubSpot revenue — one source name is wrong
about at least one of them.

### Coverage proof is per metric

Campaign-spend coverage was the universal proof, so a country metric could be
certified by evidence about campaign spend — a different table entirely. Each
authority is now asked about itself:

| Metric family | Proof required |
|---|---|
| campaign spend | campaign coverage **and** FX coverage both `verified` |
| country spend / revenue | geo coverage plus an accepted country reconciliation — `verified` **or** `reconciled_with_residual` |
| revenue / customers | canonical deal-ledger availability |
| funnel metrics | canonical contact-funnel availability |

### Fallback flags are read where production actually puts them

Real dashboards expose `legacy_fallback_used` as a **top-level boolean** and
`source_truth` as a **string**. The original guard checked `isinstance(block,
dict)` on nested keys of those names, so it was False for every production
payload — a guard that could not fire on the shape it was written for. Top-level
and nested flags are now both blocking, and the test fixture is shaped like a
real response rather than a synthetic nested dictionary.

### Comparison is exact

`_norm` rounded to six decimals while the command claimed exactness. Rounding is
a tolerance wearing different clothes. Readings are now normalised through
`Decimal(str(value))` and compared exactly, so `2.0` and `2` still agree while
values differing at the seventh decimal are reported as the two answers they are.

One window failing fails the audit: a page that agrees this quarter and disagrees
year-to-date is not a page that agrees.

---

## 9. Post-deployment validation

No new historical backfill is required.

```bash
# 1. Confirm the deployed SHA
curl -s "$AVERROES_URL/api/health" | python -m json.tool

# 2. One incremental synchronization
python -m scheduler.incremental_sync > /tmp/incremental.json; rc=$?
cat /tmp/incremental.json; echo "SYNC_EXIT=$rc"

# 3. The existing truth validator
#    (docs/19_DAILY_INCREMENTAL_SYNC.md — "The truth validator")

# 4. The cross-page parity audit, every window
python -m scripts.audit_cross_page_canonical_parity --json > /tmp/parity.json; rc=$?
cat /tmp/parity.json; echo "PARITY_EXIT=$rc"
```

Both commands must exit `0`. Then spot-check representative pages in the
browser's Network tab against the audit output — the audit inspects services, and
confirming the UI renders what the service returned is the one step it cannot
take for you.
