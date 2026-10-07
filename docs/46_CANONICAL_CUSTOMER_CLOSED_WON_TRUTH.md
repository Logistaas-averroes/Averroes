# 46 — Canonical Closed-Won Deals, Customers and Revenue

**PR-ADS-161D.** One read-only definition of closed-won deals, customers and
closed-won revenue, per evidence and business window, with attribution,
coverage, freshness and a publication verdict for every metric. Truth service
and audit only: **no production page reads it yet** — that is PR-ADS-161E.

| Module | Role |
| --- | --- |
| `analysis/closed_won_truth.py` | pure states, membership, partition, publication |
| `services/canonical_customer_revenue_service.py` | per-window truth over one snapshot |
| `services/canonical_revenue_service.load_closed_won_universe` | the ONE ledger read it uses |
| `db/deal_ledger_repository.fetch_closed_won_universe` | REPEATABLE READ, READ ONLY |
| `scripts/audit_customer_closed_won_truth.py` | read-only certification, `--json`, exit 0/1/2 |

---

## 1. Three things that are not each other

| Concept | Identity | Rule |
| --- | --- | --- |
| closed-won deal | `hubspot_deal_ledger.deal_id` (PRIMARY KEY) | `hs_is_closed_won IS TRUE` |
| customer | a HubSpot **company** id | distinct proven companies behind the won deals |
| closed-won revenue | sum over distinct won deals | `revenue_usd` where `currency_status` proves USD |

One company with two won deals is two deals and one customer. A deal
associated with five contacts is one deal. A contact at lifecycle stage
`customer` is neither a deal nor revenue — the service never reads lifecycle
stage, and the audit fails if it ever does.

---

## 2. The won predicate, and the stage cross-check

The brief named HubSpot stage `326093516` (*Deal Won / Payment Received*) as
the closed-won ground truth. The repository already decided this question the
other way, deliberately and under test (docs/35 §3): the predicate is
HubSpot's own `hs_is_closed_won`, and a hardcoded stage id as the population
filter is **forbidden**, because it ties revenue truth to one portal's
configuration and the legacy `stage = '326093516' OR label ILIKE '%won%'` read
counted *"Closed Lost – Won Elsewhere"* as revenue.

PR-ADS-161D keeps that predicate and adds the stage as a **cross-check**:

| Deal | State | Effect |
| --- | --- | --- |
| flag TRUE, stage `326093516` | agree | counted |
| flag TRUE, any other stage | `won_flag_true_in_another_stage` | counted by the predicate, and the window's count is **withheld** |
| stage `326093516`, flag FALSE/NULL | `won_stage_but_won_flag_not_true` | never counted, and the window's count is **withheld** |

Neither definition is allowed to silently win a disagreement. The audit
reports every conflict, and any window it could touch publishes no count until
it is resolved at the source.

---

## 3. Windows — two questions

**Closed-won event cohort** — membership is the canonical `deal_close_date`,
half-open `[start, end)`.

* Evidence windows (`7d` … `180d`, `all_time`): N account-local calendar dates
  ending today (`analysis.account_time`, Europe/London), bounded on
  **Europe/London midnights** converted to UTC by Campaign Evidence's
  `window_instants` — under BST a window starts at 23:00Z the day before.
* Business windows (`current_quarter` … `all_time`):
  `analysis.business_windows.get_window_bounds`, unchanged.

**Marketing acquisition cohort** — membership is the creation time of the
deal's associated contacts. A deal is a member of a window only when **every**
associated contact was created in it; contacts spanning the boundary make it
`ambiguous`; a contact whose creation time is not held makes it `unresolved`;
a failed association lookup is `unresolved`, never "no contact".

Its population is the same won population minus deals whose close lies after
"now" (they have not closed); an undated won deal stays, because contact
creation, not a close date, proves its membership.

The two are reported side by side and never combined. Nothing here computes
ROAS or CAC: a spend window divided by close-date deals is neither.

### Dates

| State | Meaning | Finite window | All Time |
| --- | --- | --- | --- |
| `exact_close_date` | dated, not in the future | member iff inside `[start, end)` | member |
| `missing_close_date` | no close date | **withholds the count** (could be in any window) | member |
| `invalid_close_date` | dated after "now" | in no window — even one whose end is still ahead, such as a window ending at tomorrow's midnight | not a member |

A won flag/stage conflict follows the same rule: an undated one blocks every
window, a dated one blocks only a window it could be a member of, and one dated
after "now" blocks none.

No ingestion, sync, record-creation, boundary or association time ever stands
in for a close date.

---

## 4. Amount and currency

Amount: `positive_amount` · `zero_amount` (a real zero) · `missing_amount`
(unknown — never zero) · `invalid_amount` · `negative_amount`.

Currency, from the ledger's own resolution (`analysis.deal_currency`):
`canonical_usd` · `converted_to_usd_with_proven_fx` · `missing_currency` ·
`missing_fx_rate` · `unsupported_currency` (an unrecognised status — fails
closed) · `not_applicable_no_amount`.

Revenue is published only when every deal in the window has proven USD. Otherwise
`revenue_usd` is NULL with `closed_won_deals_missing_proven_usd_amount`, and the
partial sum is carried under its own name, `revenue_usd_confirmed_subset` —
never labelled a total.

---

## 5. Customers — withheld, and why

**The repository holds no HubSpot company association for any deal.** The
canonical ledger has no company column, no deal→company table exists, and
docs/35 §19 already records that the legacy "company" field was a contact's
employer, not a company record. Every won deal's customer identity is therefore
`association_unavailable`, the customer count is **withheld**
(`company_associations_not_ingested`), and `confirmed_customer_lower_bound` is
**NULL** — "we could not look" is not "zero customers".

The resolver and its publication rule are built and tested so ingestion can
light them up without a second definition:

| State | Meaning |
| --- | --- |
| `resolved_single_customer` | exactly one company |
| `resolved_same_customer_multiple_paths` | one company, reached more than once |
| `unresolved_no_company` | association read, no company |
| `ambiguous_multiple_companies` | more than one company |
| `association_unavailable` | no association evidence at all |

A count is published only when every deal in the window is resolved. Only a
company id counts: never a contact, an email domain, a company name or a
campaign.

**To publish customers**, company associations must be ingested by the deal
sync (read-only from HubSpot) and the historical ledger re-synced. That is a
sync change and a backfill, outside this PR's read-only truth-service scope.

---

## 6. Attribution — a partition that always adds up

Every won deal lands in exactly one bucket, so the buckets sum to the
all-source total. No deal disappears for lacking a GCLID, a contact, a campaign
or a source.

| Bucket | When |
| --- | --- |
| `campaign_attributable` | Google Ads evidence AND the label resolves to a campaign id |
| `google_ads_unplaced` | Google Ads evidence, no deterministic campaign (no label, unmapped, or resolver unavailable) |
| `excluded_by_approved_mapping` | the label is approved as not Google Ads |
| `attributed_other_source` | unambiguous evidence of another acquisition group |
| `unattributed` | no contact, a failed association lookup, or an unclassified source |
| `ambiguous` | associated contacts disagree (`analysis.deal_truth` rule 3) |

Deal-level evidence is the ledger's own resolution: unambiguous only with one
contact, or several contacts carrying **identical** evidence. The lowest
contact id is a display identity, never an attribution choice (PR-ADS-161B's
objection, answered at the evidence level).

Campaign placement uses Campaign Evidence's resolver, `_assign_lead`: an
approved durable mapping, else an exact normalized match to exactly one
all-time canonical spend campaign. Never fuzzy. With no resolver, campaign
revenue is withheld.

Nested scopes are reported beside the partition: `all_source`,
`paid_search_source` (original source `PAID_SEARCH`, unambiguous evidence) and
`google_ads_source` (the existing lattice predicate, excluding ambiguous).

---

## 7. Freshness

From `hubspot_deal_sync_state` through the shared coverage gate
(`check_sync_coverage`): bootstrap complete, a successful INCREMENTAL after it,
last status success. `latest_successful_incremental_at` is reported only when
that is proven. The newest deal row is never the signal.

**No staleness threshold is configured for the deal ledger** anywhere in the
repository. Age is reported and not judged (`staleness_assessed: false`). An
unproven coverage gate makes every metric `unavailable`.

---

## 8. Publication

| Metric | Published when |
| --- | --- |
| `closed_won_deals` | coverage proven; no won flag/stage conflict touching the window; no undated won deal (finite windows) |
| `revenue_usd` | deals published and every deal has proven USD |
| `customers` | deals published, company identity ingested, every deal resolved |
| `campaign_revenue` | revenue published and the campaign resolver available |
| `acquisition_cohort` | coverage proven; no unresolved or ambiguous membership |
| `roas`, `cac` | **never** in this PR (`not_published`) |

A status other than `published` always carries a NULL value.

---

## 9. The audit

```bash
python -m scripts.audit_customer_closed_won_truth
python -m scripts.audit_customer_closed_won_truth --json
```

Re-derives, per window and from the same snapshot, what the service claims:
the won predicate, unique deal ids, close-date membership (independently), the
partition sum, revenue over distinct proven deals, null discipline on withheld
metrics, ROAS/CAC unpublished, freshness from sync coverage, and conflicts
withholding, and that a published count is the re-derived membership's size.
For every business window it also runs production's own windowed SQL
(`deal_ledger_repository.WON_DEALS_WINDOW_SQL`, with `load_won_deals`' bounds)
**inside the same READ ONLY snapshot**, drops closes after "now", and requires
the result to equal the service's dated membership. Undated members are
reported beside that comparison, because production's All Time is bounded
above and its SQL returns none. Structurally it checks there is
no external or write import, no lifecycle-stage reference, and no production
page consuming the service.

Exit **0** every contract holds (metrics may still be withheld) · **1** a
contract is broken · **2** unavailable — the ledger could not be read, or a
cross-check could not run (an unmeasured comparison is not a violation).

---

## 10. Reader inventory (not migrated here)

See the PR description for the full table. Three findings PR-ADS-161E must
address:

* **Won deals are called customers** across the Mart, the revenue attribution
  and source attribution services, unit economics (whose CAC denominator is the
  won-deal count) and the dashboards.
* **A legacy table gates canonical revenue.** `revenue_integration_connected()`
  is an existence check over `gclid_attribution`; dashboards blank canonical
  customers and revenue on it.
* **`sql_to_customer_rate`** divides all-source won deals by
  campaign-attributable SQLs — two populations.
* **Production's All Time drops undated won deals.** `load_won_deals("all_time")`
  queries `deal_close_date < tomorrow`, which excludes every NULL close date;
  this service counts them in All Time and withholds finite windows over them.
  Its windows also end at tomorrow's UTC midnight, so a close later today is
  admitted. The audit reports both per window.
