# 46 — Canonical Closed-Won Deals, Customers and Revenue

**PR-ADS-161D**, customer identity added by **PR-ADS-161D2**. One read-only definition of closed-won deals, customers and
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
| `connectors/hubspot_pull.fetch_deal_company_associations` | 161D2: read-only deal→company read, every page and path, fails closed |
| `db/deal_company_association_repository.py` | 161D2: atomic, failure-preserving, monotonic association storage |
| `services/deal_company_association_sync_service.py` | 161D2: scan of the whole won population |
| `scripts/backfill_deal_company_associations.py` | 161D2: dry-run / apply / resume historical backfill |
| `scripts/audit_deal_company_association_coverage.py` | 161D2: read-only association coverage certification |

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

Amount — the state of the value that would be **summed** (`revenue_usd` when
its currency is proven, since a home-currency deal may carry no raw `amount`;
otherwise the raw amount): `positive_amount` · `zero_amount` (a real zero) ·
`missing_amount` (unknown — never zero) · `invalid_amount` · `negative_amount`.
A negative value on a won deal is invalid evidence, not a refund: it is never
netted off the total, and withholds revenue like any unproven amount.

Currency, from the ledger's own resolution (`analysis.deal_currency`):
`canonical_usd` · `converted_to_usd_with_proven_fx` · `missing_currency` ·
`missing_fx_rate` · `unsupported_currency` (an unrecognised status — fails
closed) · `not_applicable_no_amount`.

Revenue is published only when every deal in the window has proven USD. Otherwise
`revenue_usd` is NULL with `closed_won_deals_missing_proven_usd_amount`, and
`deals_without_proven_usd` says how many. **No partial sum is carried anywhere**
— not on the window, not on a bucket, not on a scope: the known part of a total
reads as the total.

---

## 5. Customers — deal→company identity (PR-ADS-161D2)

A customer is ONE distinct HubSpot **company id**, read from HubSpot's own
deal→company associations. Never a deal count, a contact, a lifecycle stage, an
email domain, a company or deal name, a GCLID, a campaign or a source.

### 5.1 The evidence

| Table | One row per | Holds |
| --- | --- | --- |
| `hubspot_deal_company_association` | (deal, company, association **path**) | the set from the deal's latest **successful** lookup |
| `hubspot_deal_company_observation` | deal | latest attempt (success/failed, when, why) and latest success (when, companies, paths, `success_zero_companies`) |
| `hubspot_deal_company_sync_state` | dataset | last run, last **complete scan**, historical backfill generation |

A path is HubSpot's association category and type id (`HUBSPOT_DEFINED:5`,
`USER_DEFINED:17`). Two paths to one company are kept as two rows and are still
one company.

**Successful zero vs never looked.** A lookup that succeeded and found no
company writes `success_zero_companies = true`. A lookup that failed — or was
never made — leaves no success at all. "No bridge rows" is never read as "no
company": the observation row decides.

**Failure preserves.** A failed refresh records the failed attempt and leaves
the last successful set and its counts untouched, in the bridge and on the
observation row. A successful refresh replaces the set (paths HubSpot no longer
reports are removed) and records itself in ONE transaction.

**Monotonic.** Every write carries the instant its HubSpot read STARTED, taken
from the **database clock** (`clock_timestamp()`), so two hosts with different
clocks cannot misorder each other. A success applies only if it started at or
after the stored latest success; an attempt outcome only if at or after the
stored latest attempt. A slow historical pass cannot overwrite a newer
observation, whatever the commit order. The same holds one level up: every
run-state field (last run, last full run, complete scan) and the backfill
generation anchor are stamped on the database clock and ordered by run start,
so an older scan committing after a newer failed one cannot restore a passing
verdict.

### 5.2 Identity per deal

| State | When |
| --- | --- |
| `resolved_single_customer` | successful lookup, exactly one company, one path |
| `resolved_same_customer_multiple_paths` | one company, more than one path |
| `unresolved_no_company` | successful lookup, no company |
| `ambiguous_multiple_companies` | more than one distinct company — never counted as several customers |
| `association_unavailable` | no successful lookup (never looked, or every look failed) |

Evidence is **stale** when the latest attempt failed (the stored set is older
than our latest look) or when the deal changed in HubSpot
(`hubspot_lastmodified_at`) after the evidence was observed. The second rule
compares HubSpot's clock with ours, and associating a company is not guaranteed
to move `hs_lastmodifieddate` — so per-deal staleness is a disclosure, not the
currency guarantee. That is the scan gate below.

**The scan gate.** A pipeline that stops — the population unreadable, no batch,
the step killed — moves no per-deal row, so every deal's evidence would still
look untouched. Customers therefore also require the dataset's own record: the
latest FULL-population run succeeded and covered every won deal, and the latest
complete scan finished within `customer_identity.association_scan_max_age_hours`
(config/thresholds.yaml, 48h — one missed daily run tolerated). A backfill
resume pass covers a subset and never counts; a completed backfill generation
does. Stale evidence is
kept and disclosed (`stale_customer_identity`), counts toward nothing exact,
and withholds the total.

### 5.3 Publication

| Members of the window | `customers` |
| --- | --- |
| deal count not published | follows the deal count |
| company evidence unreadable | `unavailable` (`company_associations_unreadable`) |
| any member `association_unavailable` | `unavailable` (`customer_identity_evidence_unavailable`) |
| scan not current (see above) | `withheld` (`customer_identity_scan_not_current`) — and no lower bound |
| any member no-company or multi-company | `withheld` (`customer_identity_unresolved`) |
| any member stale | `withheld` (`customer_identity_evidence_stale`) |
| every member resolved and current | **published**: distinct company ids |

Whenever the total is not published and the deal count is,
`confirmed_customer_lower_bound` (distinct companies of resolved, current
members), `unresolved_customer_identity`, `stale_customer_identity` and the
`customer_identity` distribution are published. The lower bound is NULL — not
0 — when the scan is not current or when no member was ever successfully
observed: "0 confirmed" would be a finding nobody made. The won-deal count
never stands in for customers.

Unreadable company evidence is isolated under a SAVEPOINT inside the universe's
one READ ONLY snapshot: customers become unavailable, deals and revenue stay
publishable.

### 5.4 Keeping it current

* **Population:** every deal the canonical ledger holds as won. Never the
  legacy GCLID deal subset.
* **Incremental:** `hubspot/deal_company_associations` runs in the daily
  incremental sync right after `hubspot/deal_ledger`, and reads the WHOLE won
  population every run — associating a company to a deal is not guaranteed to
  move `hs_lastmodifieddate`, so a watermark scan could miss exactly this
  change. Clean complete run → `success` (and a recorded complete scan);
  lookup or write failures → `partial`/`failed`; cap reached → `partial`; no
  durable batch → aborts before any HubSpot call. Its failure withholds
  customers and never touches deal or revenue evidence.
* **Historical backfill:** `python -m scripts.backfill_deal_company_associations`
  — dry run by default (no writes of any kind); `--apply` opens a generation;
  `--resume --apply` continues it; a plain `--apply` refuses to restart over an
  in-progress generation. Stops at the first failure with that deal as the
  checkpoint. Completion is proven when every won deal has a success at or
  after the generation's start.

**Installing this does not make customer totals complete.** Completeness is a
property of production evidence after the backfill: genuinely unassociated or
multi-company won deals stay withheld, and are a CRM remediation queue (listed
by the coverage audit), not something the application may resolve by guessing.

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
revenue is withheld. It is also withheld (`google_ads_deals_not_placed_on_a_campaign`)
while **any** Google Ads deal in the window is unplaced: that deal could belong
to any campaign, so every per-campaign figure would be a lower bound published
as a total.

The all-time spend names are deliberate — a deal's campaign must not depend on
which window is asked — and differ from Campaign Evidence's page, which matches
against the window's spend; the same label can place differently on the two.

Nested scopes are reported beside the partition: `all_source`,
`paid_search_source` (original source `PAID_SEARCH`, unambiguous evidence) and
`google_ads_source` (the existing lattice predicate, excluding ambiguous). The
scope is the lattice's question, not the partition's: it includes deals whose
label an approved mapping excludes from Google Ads.

---

## 7. Freshness

From `hubspot_deal_sync_state` through the shared coverage gate
(`check_sync_coverage`): bootstrap complete, a successful INCREMENTAL after it,
last status success. `latest_successful_incremental_at` is reported only when
that is proven. The newest deal row is never the signal, and a universe that
carries no verdict at all is treated as unproven.

**No staleness threshold is configured for the deal ledger** anywhere in the
repository. Age is reported and not judged (`staleness_assessed: false`). An
unproven coverage gate makes every metric `unavailable`.

---

## 8. Publication

| Metric | Published when |
| --- | --- |
| `closed_won_deals` | coverage proven; no won flag/stage conflict touching the window; no undated won deal (finite windows) |
| `revenue_usd` | deals published and every deal has proven USD |
| `customers` | deals published, every member's company identity observed, resolved and current (§5.3) |
| `campaign_revenue` | revenue published and the campaign resolver available |
| `campaign_revenue` (above) | … and no Google Ads deal in the window left unplaced |
| `acquisition_cohort` | coverage proven; no won flag/stage conflict dated up to now (or undated) — acquisition membership ignores the close date, so any such conflict touches it; no unresolved or ambiguous membership |
| `acquisition_revenue` | the acquisition cohort published and every member has proven USD |
| `roas`, `cac` | **never** in this PR (`not_published`) |

**Withheld means absent, not hidden.** A status other than `published` carries
a NULL value, and so does everything the number could be rebuilt from: when the
deal count is not published, member ids, bucket and scope counts, coverage
distributions over members and the confirmed-in-window count are all NULL; when
coverage is unproven, every number in the window is. What remains is why:
reasons, the undated and future-dated counts, the conflicts list.

**Unknown won state** (`hs_is_closed_won IS NULL`) is neither won nor lost, so it
is in no count; it is disclosed per window as `unknown_won_state_deals` (and
`unknown_won_state_undated`), as `load_won_deals` reports it (docs/35 §3) — NULL
when it could not be read, never 0.

---

## 9. The audit

```bash
python -m scripts.audit_customer_closed_won_truth
python -m scripts.audit_customer_closed_won_truth --json
```

Re-derives, per window and from the same snapshot's **raw rows**, what the
service claims — without calling the 161D analysis module's decision functions:
evidence-window bounds (Europe/London midnights, computed itself), close-date
membership and the published count, the flag/stage conflicts (from the raw
definition rows), unknown-won-state disclosure, proven USD (the ledger's two
proven statuses, non-negative), the partition's ambiguous and Google Ads
families, acquisition-cohort membership (from contact creation), and that no
withheld or unavailable number is recoverable anywhere in the payload. Two
inputs are shared because they ARE the definition, not 161D logic: the business
windows (`analysis.business_windows`) and the Google Ads lattice predicate
(`analysis.revenue_scope`). Each independence is shown by a test that breaks the
implementation, not the output (`test_65_*`).
For every business window it also runs production's own windowed SQL
(`deal_ledger_repository.fetch_won_deals` on the audit's cursor, with
`load_won_deals`' bounds)
**inside the same READ ONLY snapshot**, drops closes after "now", and requires
the result to equal the service's dated membership. Undated members are
reported beside that comparison, because production's All Time is bounded
above and its SQL returns none. Structurally it checks there is
no external or write import, no lifecycle-stage reference, and no production
page consuming the service.

Exit **0** every contract holds (metrics may still be withheld) · **1** a
contract is broken · **2** unavailable — the ledger could not be read, or a
cross-check could not run (an unmeasured comparison is not a violation).

**Customers (PR-ADS-161D2).** The audit re-derives each member's identity from
the raw association rows and observation states — shared with the coverage
audit's `independent_identity`, not the truth module's resolver — and requires a
published total to equal the distinct company ids of members that are all
resolved and current, and a published lower bound to equal the distinct
companies of the resolved, current members. `test_43` breaks the resolver
itself and shows the audit refuses.

### 9.1 Association coverage audit

```bash
python -m scripts.audit_deal_company_association_coverage
python -m scripts.audit_deal_company_association_coverage --json
```

One READ ONLY snapshot; no HubSpot, Google Ads or database write. Proves that
every won deal has an explicit observation outcome, that a successful zero and
a failed/never-made lookup are distinct, that stored paths reconcile with the
recorded success counts, that no path is stored twice or exists without a
successful lookup, that coverage is proven by a recorded complete scan, and
that every won deal has a success at or after the recorded complete scan's
start (a scan that did not observe a deal cannot claim it), that every window
touched by a no-company, multi-company, unobserved or stale deal publishes no
exact customer total, that the scan is current, and — for
every window whose deal count is published — that the customer status, reason
and identity distribution match what the raw evidence says (so a failed lookup
published as "no company" is caught even though the total stays withheld). It
lists no-company and multi-company deals as **CRM findings** — data quality,
not contract failures. Deleted or merged HubSpot deals are not handled by the
ledger: a won deal that now 404s fails every scan and keeps its windows
withheld — closed, but noisy.

Exit **0** storage and coverage contracts hold · **1** a contract is violated
or coverage is not proven (expected before the first backfill) · **2**
unavailable.

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
