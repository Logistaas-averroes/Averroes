# 37 — Lifecycle Cohort Truth & Fail-Closed Revenue

**PR-ADS-155.** Two production truth gaps, closed.

---

## Gap 1 — The Dashboard "Pipeline" was not a pipeline

### What was shown

Five cells in a narrowing strip, joined by conversion arrows:

```
Leads → MQLs → SQLs → Customers → Closed-Won Revenue
```

### What they actually were

Five **independent** populations, each answering a different question about a
different set of records:

| Cell | What it counted |
|---|---|
| Leads | contacts whose Lead-entry date fell inside the window |
| MQLs | contacts whose MQL-entry date fell inside the window |
| SQLs | contacts whose SQL-entry date fell inside the window |
| Customers | closed-won **deals** in the window |
| Closed-Won Revenue | the USD value of those deals |

Nothing tied the first three together. A contact counted at MQL this quarter may
have entered Lead two years ago; a contact counted at Lead this quarter may not
reach MQL for another year. So the strip could — and on production did — widen
between adjacent cells while still reading as attrition, because a narrowing
layout with arrows between the cells says "this many of those became these".

The last two cells were worse than unrelated: they crossed into an entirely
different truth. A **lifecycle Customer** is a HubSpot contact-stage fact. A
**closed-won customer** is a deal fact. No governed contact-to-deal cohort
contract exists in this system, so an arrow between them asserted a relationship
nothing in the product could prove.

### What is shown now

**One Lead-anchored cohort, followed forward.**

```
Lead cohort ≥ reached MQL ≥ reached SQL ≥ reached Opportunity ≥ reached Lifecycle Customer
```

* **The denominator is fixed.** It is the set of contacts whose canonical
  HubSpot Lead-entry date (`hs_v2_date_entered_lead`) falls inside the selected
  window — at every stage, not just the first.
* **Every later stage is a subset of that same cohort.** A contact counts at a
  stage only if it is already counted at the stage before it, so the ordering
  above holds *by construction* rather than by coincidence.
* **Later stages may fall outside the window.** A January lead that became an
  SQL in July is a conversion of the January cohort and counts as one. Only the
  anchor is windowed.
* **A stage entry dated before the anchor is excluded**, and reported. It
  belongs to an earlier lifecycle pass, not to this cohort's progression.

Built by `canonical_crm_funnel_service.lead_cohort_progression()` and published
as `lifecycle_cohort`. The Dashboard service and the frontend contain **no
lifecycle rules and no conversion arithmetic** — every count and every
percentage is read from the contract.

`cohort_conversion()` (adjacent-cohort rates, PR-ADS-153C) still exists and is
still correct for the question it answers. It is **not** used to build the
displayed progression: chaining adjacent independent cohorts produces a different
number, and `test_6` constructs a case where the two deliberately disagree.

### Commercial outcomes moved out

Closed-won customers and revenue now render in their own **Commercial Outcomes**
section, with no arrow connecting them to the lifecycle strip. The Overview
response carries `commercial_outcomes.connected_to_lifecycle_funnel: false`, and
a static test asserts the outcomes markup draws no conversion chips.

That arrow may be drawn again only when a **governed contact-to-deal cohort
contract** exists to prove the relationship. Until then it is absent.

### Partial lifecycle history is stated, never repaired

Some contacts carry a lifecycle stage that proves they reached a stage while
HubSpot holds no entry timestamp for it. The transition is real; the date is
unknown. Those contacts are **excluded from that stage and counted**, under four
named reasons:

| Reason | Meaning |
|---|---|
| `missing_stage_entry_date` | reached this stage; HubSpot has no entry timestamp |
| `stage_entry_before_lead_entry` | the recorded entry predates this cohort's anchor |
| `prior_stage_entry_unproven` | valid date here, no proven entry for the stage before |
| `missing_lead_entry_date` | reached Lead with no Lead timestamp — cannot be placed in *any* window's cohort |

The last one counts over a **different population** (`contacts_considered`, not
`lead_cohort`) and is therefore reported as its own record and excluded from
`excluded_contacts`. Summing it into the cohort's total would mix two
populations in one integer.

The Dashboard shows a **"Partial lifecycle history"** badge with a tooltip, and a
disclosure listing every reason with its count and the population it counts over.
`truth_status` is `not_ready` while coverage is partial — the page still renders
the funnel, because incomplete is not unavailable, but under a status that says
so.

**No proxy date is ever substituted.** Not contact creation date, not the
current-stage date, not the ingestion timestamp, not an interpolation between
neighbouring stages.

---

## Gap 1b — Can the missing timestamps be recovered? (§4)

Two candidate sources of **real** evidence were audited against the live portal
before any code was written.

### Route 1 — legacy per-stage date properties: **does not exist here**

HubSpot historically exposed `hs_lifecyclestage_lead_date`,
`hs_lifecyclestage_customer_date` and similar. A property search against the
connected portal returns **no such properties**. The account exposes only the
`hs_v2_date_entered_*` / `hs_v2_date_exited_*` / `hs_v2_latest_time_in_*` family
— the same properties the sync already reads. There is nothing to recover here.

### Route 2 — `lifecyclestage` property history: **real, and bounded**

HubSpot retains version history for the `lifecyclestage` property: each
historical value, the timestamp it was set, and the source that set it, exposed
through `propertiesWithHistory` on the CRM read API (confirmed present on the
installed SDK's batch-read model). A version whose value **is** a funnel stage is
HubSpot's own record of the transition into that stage — the same evidence
`hs_v2_date_entered_*` is derived from, read from the other end.

So recovery ingests evidence; it does not infer. What it cannot promise is
completeness:

* history depth is bounded by HubSpot's retention, and an old transition may not
  be returned;
* a contact set straight to a later stage never had a version for the stages it
  skipped.

Where no matching version exists, **nothing is written**, the timestamp stays
NULL, and the cohort keeps reporting the gap. Recovery can shrink the gap and can
never close it by pretending.

**How much it will actually recover is only knowable by running the read.** That
is what the dry run is for, and it has not been run against production in this
PR.

### Why a separate table

`hubspot_contact_funnel.date_entered_*` is owned by the contact sync, whose
upsert refreshes every column from the newest HubSpot read
(`date_entered_lead = EXCLUDED.date_entered_lead`). A recovered timestamp written
there would be **erased by the very next incremental sync**, because HubSpot
still returns null for the property it was recovered for.

Recovery therefore lives in `hubspot_lifecycle_stage_history`, and
`db/crm_funnel_repository` COALESCEs it into the one read every funnel consumer
goes through. The base column always wins; history fills gaps only. Every row
reports which source supplied each date, so a recovered timestamp is never
indistinguishable from a directly-read one:

```
event_date_provenance[event] ∈ {
  hubspot_stage_entry_property,            # hs_v2_date_entered_*
  hubspot_lifecyclestage_property_history, # recovered
}
```

### The command

```bash
# ALWAYS dry-run first. Reads HubSpot, writes nothing, anywhere.
python -m scripts.backfill_lifecycle_stage_history --limit 50

# Only after a dry run has shown what it would recover:
python -m scripts.backfill_lifecycle_stage_history --limit 50 --apply
```

* **Never writes to HubSpot.** The only HubSpot call is a batch READ with
  `propertiesWithHistory`. A static test asserts no mutation verb appears
  anywhere in the recovery path.
* **Local-database writes only**, and only under `--apply`.
* **Bounded** — every run takes an explicit `--limit`.
* **Idempotent** — keyed `(contact_id, funnel_event)`.
* **Resumable** — a durable cursor in
  `hubspot_lifecycle_history_recovery_state`, advanced only by completed
  `--apply` runs.
* **Provenance-carrying** — HubSpot source type/id, the raw stage value, and the
  run that recovered it.

**No production backfill was run in this PR.**

---

## Gap 2 — All-Time revenue: correctly unavailable, unhelpfully so

### The situation

Of 181 closed-won deals in the canonical ledger, **14 carry no amount in
HubSpot**. Verified live against the portal: a search for closed-won deals
without the `amount` property returns exactly 14 records, none of which carries
`amount`, `deal_currency_code` or `amount_in_home_currency`.

So the All-Time total is genuinely unknown, and PR-ADS-154C-F3 correctly stopped
the product publishing `$878,324.80` under a "Closed-Won Revenue" heading — that
figure is the value of **167** deals, not of 181.

Correct, and on its own a dead end: a blocked number with nothing to do about it.

### The parts, published separately

`canonical_revenue_service.revenue_disclosure()` publishes each fact under a name
that says what it counts:

| Field | Production value | What it is |
|---|---|---|
| `closed_won_deals` | 181 | every won deal. **Complete.** |
| `revenue_proven_deals` | 167 | those with a proven amount and currency |
| `revenue_unavailable_deals` | 14 | those without — the actionable number |
| `known_revenue_usd` | 878324.80 | what the **priced** deals are worth |
| `known_revenue_label` | "Known revenue from 167 priced deals" | the only caption it may carry |
| `total_revenue_usd` | `null` | not zero, not the sum of the rest |
| `total_revenue_publishable` | `false` | the canonical verdict |
| `unavailable_reason` | `closed_won_deals_missing_amount` | |
| `violation_codes` | `["currency_unproven_deals_in_population"]` | |

Two rules hold this together:

* **The count is never gated on the total.** 181 customers is a complete fact
  even while their combined value is not. Blanking a number we did measure would
  be its own fabrication.
* **The partial sum may appear only under a label naming its own denominator.**
  It is a real measurement of a named subset, and it is never contracted or
  rendered as the window total. The Dashboard renders it in a visually
  subordinate cell captioned "A named subset — not the window total", and a
  static test asserts the total cell reads `total_revenue_usd` and never
  `known_revenue_usd`.

The reason VALUE was renamed from `canonical_revenue_amounts_incomplete` to
`closed_won_deals_missing_amount`: the old name described the symptom (the total
is incomplete), the new one names the cause an operator can act on.

### The blockers, named (§6)

```bash
python -m scripts.report_missing_deal_amounts --window all_time
GET /api/audit/missing-deal-amounts?window=all_time     # admin-only
```

Per deal: id, name, close date, stage, amount status, currency-code status,
currency status, the **canonical** `analysis.deal_currency` reason verbatim, a
HubSpot record URL, `fallback_used: false`, and a generated timestamp.

The record URL is built from the configured portal id
(`HUBSPOT_PORTAL_ID`, else `config/thresholds.yaml` `accounts.hubspot_id`) and is
**omitted rather than guessed** when the portal is unknown — a guessed portal is
a link into somebody else's CRM.

Nothing writes to HubSpot, and no amount is inferred: not from the company, the
campaign, the account's previous deals, associated contacts, or by dividing the
known revenue across the unpriced deals. Exit codes: `0` clean, `1` deals are
missing amounts, `2` the ledger could not be read (so the count is **unknown**,
not zero), `3` usage.

---

## Gap 3 — The parity audit conflated four situations (§7)

`canonical_source_unavailable` was one bucket holding four problems that four
different people fix:

| Class | Code | Who fixes it |
|---|---|---|
| the store could not be read | `canonical_database_unreadable` | an engineer |
| coverage for the window is unproven | `revenue_population_unavailable` | a backfill |
| population complete, total unknown | `revenue_total_unpublishable_missing_amount` | whoever owns the deal, in HubSpot |
| lifecycle evidence incomplete | `lifecycle_coverage_partial` | HubSpot's records; disclosed |

The audit now classifies on the consumers' **own declared reasons**, which the
readings already carried. An absence it cannot classify stays
`canonical_source_unavailable` — an unexplained absence is still a violation, and
pretending to know which kind it is would be its own fabrication. Consumers
declaring *different* reasons for the same metric is itself reported.

**All-Time revenue therefore now reports
`revenue_total_unpublishable_missing_amount`**, not a generic outage.

`lifecycle_coverage_partial` is reported in a separate `coverage_disclosures`
section rather than as a violation. It is a disclosed, quantified gap in
HubSpot's records, not a disagreement between pages; marking the audit red for it
would make red the permanent state of a condition we have chosen to report rather
than invent our way out of. **The audit does not go green while the 14 source
records remain unpriced** — that is a violation, under its own precise code.

---

## Files

| File | Change |
|---|---|
| `services/canonical_crm_funnel_service.py` | `lead_cohort_progression()`, the `lifecycle_cohort` block, stage-date provenance |
| `services/dashboard_overview_service.py` | publishes `lifecycle_cohort` + `commercial_outcomes`; new metric contracts |
| `services/canonical_revenue_service.py` | `revenue_disclosure()`, `disclosure_from_ladder()`, `missing_amount_deals()`, reason rename |
| `services/revenue_decision_mart.py` | publishes the disclosure from the scope ladder |
| `services/cross_page_parity_service.py` | `_classify_unavailable()`, coverage disclosures |
| `services/lifecycle_history_recovery_service.py` | **new** — recovery from property history |
| `connectors/hubspot_pull.py` | `fetch_lifecycle_stage_history()` — read-only |
| `db/schema.py` | `hubspot_lifecycle_stage_history`, `hubspot_lifecycle_history_recovery_state` |
| `db/crm_funnel_repository.py` | COALESCE recovery into the canonical read; recovery cursor |
| `db/writers.py` | `upsert_lifecycle_stage_history()` — local DB only |
| `api/server.py` | `GET /api/audit/missing-deal-amounts` |
| `scripts/backfill_lifecycle_stage_history.py` | **new** — dry-run-first recovery command |
| `scripts/report_missing_deal_amounts.py` | **new** — the actionable blocker report |
| `scripts/audit_cross_page_canonical_parity.py` | prints coverage disclosures |
| `static/app.js`, `static/styles.css` | cohort funnel, coverage badge/disclosure, outcomes section |

---

## Production procedure

1. Deploy. The two new tables are created by `CREATE TABLE IF NOT EXISTS`; both
   start empty, so nothing about the funnel changes on deploy.
2. Open the Dashboard. Confirm the funnel is Lead-anchored, monotonically
   non-increasing, and that Commercial Outcomes is a separate section with no
   arrow into it.
3. Confirm the coverage badge reflects reality — "Partial lifecycle history"
   with a disclosure, or "Complete lifecycle history".
4. `python -m scripts.report_missing_deal_amounts --window all_time` — expect
   exit 1 and 14 deals listed.
5. `python -m scripts.audit_cross_page_canonical_parity` — expect All-Time
   revenue under `revenue_total_unpublishable_missing_amount`, not
   `canonical_source_unavailable`.
6. `python -m scripts.backfill_lifecycle_stage_history --limit 50` — **dry run**.
   Read what it would recover and what HubSpot has no evidence for.
7. Only if the dry run shows real recoveries, and only with sign-off, run the
   same command with `--apply`, in bounded passes, re-checking the funnel between
   passes.
8. The 14 unpriced deals are fixed **in HubSpot**, by whoever owns them. Once
   priced, the ledger sync picks up the amounts and the total becomes publishable
   with no code change.
