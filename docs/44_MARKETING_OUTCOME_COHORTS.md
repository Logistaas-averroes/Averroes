# 44 — Canonical Marketing Outcome Cohorts and the Campaign Evidence Migration

**PR-ADS-161B.** Campaign Evidence stops losing real SQLs and customers to
imperfect HubSpot lifecycle timestamps, by answering the question a
paid-marketing decision actually asks — and by never confusing it with the
question it does not.

> **Numbering.** PR-ADS-161A-2 is reserved by `docs/43` for the event-time
> executive cutover onto the publication contract. This work is a different
> decision — Campaign Evidence moves to *acquisition cohorts*, which never ask
> for an SQL-entry date — so it is numbered 161B rather than taking that slot.

---

## 1. Two metric families

They sound alike. They are different populations, and a number from one is
never a number from the other.

### Acquisition cohort — `acquisition_cohort_outcomes`

> Of the contacts **acquired** during this period, how many have reached SQL
> **as of now**?

| | |
|---|---|
| window basis | `contact_created_at` (`hubspot_contact_funnel.created_at`), on **account-local** Google Ads days |
| outcome basis | `latest_canonical_lifecycle_evidence`, read at the data watermark |
| dedup key | `contact_id` |
| used for | campaign performance, lead-quality analysis, CPQL, paid-marketing decisions |
| implemented by | `services/marketing_outcome_cohort_service.py` |

### Lifecycle events — `lifecycle_stage_events`

> How many contacts **entered** SQL during this period?

| | |
|---|---|
| window basis | `date_entered_sql` (the effective SQL-entry date) |
| used for | funnel-flow reporting, stage velocity, sales operations |
| requires | an exact SQL-entry timestamp for every contact |
| governed by | `analysis/sql_publication.py` and `scripts/audit_sql_coverage_gate.py` (`docs/41`, `docs/43`) |

A contact created in March that became an SQL in June is in **March's**
acquisition cohort and **June's** lifecycle events.

## 2. Why the cohort can count what the event metric cannot

Production evidence supplied with this PR's brief (read against `main`
`944af0c`; not re-read by this PR, which has no production access): 1,531
contacts whose lifecycle stage proves they reached SQL; 863 with a direct
SQL-entry timestamp, 0 recovered, **668 with none**.

The 668 are not zero SQLs. Their stage proves the transition; only its *date*
is unknown. The event metric must withhold — it cannot place them in any
window. The cohort does not need to: its window is creation, which *is* known.
So each of the 668 is counted, once, in the cohort of the period it was
created in, and disclosed as a lifecycle-event coverage gap.

**No SQL-entry timestamp is produced, estimated or substituted.** Not from
creation, not from the coverage boundary, not from a sync time. The cohort
never asks for one. `scripts/audit_marketing_outcome_cohorts.py` proves this
structurally (no classification function reads a boundary, sync or ingestion
stamp — `test_10b` shows the check failing when one does) and in data
(`test_24` shows building the page changes no stage-entry date).

## 3. What proves SQL

A distinct contact has reached SQL when canonical evidence proves **any** of:

| proof | source | carries a timestamp |
|---|---|---|
| `direct_sql_entry_timestamp` | `hubspot_contact_funnel.date_entered_sql` (`hs_v2_date_entered_salesqualifiedlead`) | yes |
| `recovered_lifecycle_history` | `hubspot_lifecycle_stage_history` (`funnel_event = 'sql'`) | yes |
| `lifecycle_stage_implies_sql` | current stage ∈ `analysis.crm_lifecycle.stages_implying_event(EVENT_SQL)` | **no** |

`stages_implying_event(EVENT_SQL)` is the repository's one stage-rank rule:
`salesqualifiedlead`, `opportunity`, `customer`, `evangelist`. There is no
second copy (`test_03c`).

### It is a union — and that is not the 1,531

`fetch_sql_coverage_population` counts only the third row, because its
question is "whose SQL date is missing?". A contact whose SQL-entry date
HubSpot recorded, and whose stage later moved back below SQL, still reached
SQL — `analysis/crm_lifecycle.py`: *"a funnel event is proven by the
stage-entry timestamp, not by the contact's current lifecycle stage."* The
cohort counts it; the coverage population does not.

The all-time cohort therefore differs from 1,531 by exactly:

* contacts with an SQL-entry date whose stage is now below SQL (**added**);
* SQL-proven contacts with no `created_at`, which belong to no window (**not
  counted**, disclosed).

The audit prints both numbers (`population_split`).

## 4. Attribution — three buckets, nothing dropped

| bucket | rule |
|---|---|
| `campaign` | Google Ads sourced, placed on one campaign by Campaign Evidence's own `_assign_lead` |
| `unattributed_google_ads` | Google Ads sourced, no campaign to place it on |
| `excluded_non_google` | not Google Ads sourced, or an approved `not_google_ads` label mapping |

*Google Ads sourced* is the contact's own HubSpot original source,
`classify_source(hs_analytics_source) == google_ads` — the rule every
canonical contact read uses. A campaign label never decides it.

Unattributed carries its reason: `unmapped_campaign_label`, `missing_campaign`,
`pseudo_campaign`, `email_campaign`, or `conflicting_attribution_across_rows`.

Excluded carries its reason too, and "not proven Google Ads" is kept apart
from "proven another channel": `non_google_source` (the original source is
another channel), `original_source_unclassified` (blank or unrecognised —
excluded because nothing proves Google Ads bought it, not because anything
proves otherwise), or `label_mapped_not_google_ads`.
An unmapped label gets its own Mapping Review row keyed `unmatched:<label>` —
the same key the legacy lead rows use, from the same resolver, so one label is
one row.

```
google_ads_sqls  = Σ campaign_sqls + unattributed_google_ads_sqls
all_source_sqls  = google_ads_sqls + excluded_non_google_sqls
```

Both hold by construction and are re-checked on every build
(`reconcile_cohort`). That check catches a bucket that is built wrong, not one
that is *classified* wrong: an SQL moved from Google Ads to excluded still adds
up. So the audit re-derives, in its own SQL, three things from raw rows:
all-source membership, SQL proof, and the **Paid Search split**. That last
check is that every Paid Search-sourced SQL is either a Google Ads SQL or
excluded by an approved not-Google-Ads mapping (`google_ads_split`; `test_14i`
shows it red where the bucket identity stays green). The audit does **not**
independently recompute campaign placement or deal placement. It checks those
against the page's own published numbers.

## 5. Closed-won deals

| | |
|---|---|
| source | the canonical deal ledger, `hs_is_closed_won IS TRUE` |
| dedup key | `deal_id` |
| cohort placement | the ledger's `primary_contact_id` → its `created_at` |
| attribution | `analysis.revenue_scope.is_google_ads_attributed` → `has_campaign` → `_assign_lead` |
| buckets | `campaign` · `unattributed_google_ads` · `ambiguous` · `excluded_non_google` |

A deal joins a cohort through **one** contact, so a deal associated with five
contacts is one deal (`test_08`; on PostgreSQL, `test_20` seeds a won deal
associated with three contacts and counts it once). Placing deals by
`deal_close_date` instead would put an event-time metric on a cohort page.

A deal that cannot be placed in time — no primary contact, a contact not in the
canonical funnel, or a contact with no `created_at` — is **unplaceable**, by
reason, in no window, and not counted as zero.

**Limitation — disclosed, not solved.** When several contacts share a deal with
identical evidence, the ledger's `primary_contact_id` is the lowest contact id,
a *display* identity (`analysis/deal_truth.py`, rule 2), not the first contact
acquired. Those contacts may have been created in different windows, so the
deal's window is the display contact's. Every response counts these deals
(`placed_by_display_contact`) and says so in `deal_metadata.coverage_notes`.

**Deals and SQL contacts are attributed on different evidence.** A deal is
Google Ads by the revenue scope lattice: an agreed source **or a GCLID**. A
cohort SQL is Google Ads by the contact's own original source. A deal with a
GCLID whose contact's original source is not Paid Search therefore counts as a
Google Ads deal, while that contact is excluded from Google Ads SQLs. So a
campaign can show closed-won deals with no matching cohort SQL. Each rule is
canonical for its own entity, so this is by design, and it is stated on every
response (`deal_metadata.attribution_basis`).

Contradictory evidence is not attribution: contacts disagreeing on source, or a
GCLID beside a label mapped `not_google_ads`, is `ambiguous`.

These are **closed-won deals**, not unique customers. No customer or company
metric is introduced; none with a certified canonical company key exists to
preserve.

## 6. CPQL

```
CPQL = window Google Ads spend (USD) ÷ Google Ads cohort SQLs from the SAME window
```

* The denominator is **all** Google Ads cohort SQLs — campaign + unattributed —
  matching the numerator's scope. The legacy "mapped_only" CPQL divided all
  spend by a subset of the SQLs it bought.
* Spend days and cohort days are both Europe/London. (The legacy lead read
  bounded creation at midnight in the database session's timezone, drifting
  from spend by the BST offset.)
* A missing SQL-entry date **never** blocks it.

| outcome | when |
|---|---|
| `published` | SQL count published, spend + FX available, source fresh, spend > 0, SQLs > 0 |
| *inherits the SQL verdict* | the SQL count is not published (§7): CPQL takes **its status and its reason** — an incomplete bootstrap is never explained as "stale" |
| `not_applicable` (`N/A`) | zero cohort SQLs (`zero_cohort_sqls`), or zero window spend (`zero_window_spend`) — never $0, never ∞ |
| `withheld` | the SQL count is published but its source is stale (`source_not_fresh`) — spend is current, a stale funnel's outcomes are not |
| `unavailable` | no spend read, FX incomplete |

`fetch_canonical_campaign_spend` reports `0.0` for a window with no spend
rows, so zero spend is a reachable state. A `$0.00` CPQL would read as free
SQLs, so zero spend is never divided.

Row CPQL additionally withholds when the identity mappings are unreadable
(`campaign_attribution_unavailable`) — its denominator would be whatever the
exact-name fallback placed — and is unavailable on a Mapping Review row or a
campaign with no spend row in the window. It is computed from the spend the
row publishes, so it reproduces exactly from the published numbers.

## 7. Publication of the SQL count

One verdict, `sql_publication()`. The SQL count, every CPQL, and both metadata
blocks' `coverage_status` all derive from it.

| `sql_status` | when |
|---|---|
| `published` | funnel read, buckets reconcile, and the freshness verdict is `source_fresh` or `source_stale` **with** a watermark. Stale is published as of its watermark |
| `withheld` | `cohort_reconciliation_failed`; `data_watermark_unknown` (freshness could not be read, `fresh is None`); or **any other freshness verdict, passed through as the reason**: `source_sync_state_missing`, `source_bootstrap_incomplete`, `source_incremental_provenance_missing`, `source_last_incremental_failed`, `source_no_successful_incremental` |
| `unavailable` | `canonical_funnel_unreadable` |

The withheld freshness verdicts are `fresh=False`, not `None`, in
`analysis/sql_coverage_freshness.assess`. Each means the contact population
itself is not proven complete, so a count over it is partial, not a total.
The first commit of this PR withheld on `None` only, and so published every
one of them (`test_11h`, driven through the real `assess`). While the
population is unproven, `coverage_status` is `cohort_population_not_proven`,
never `cohort_complete`. Leads acquired and closed-won deals are counted over
the same population, so the page shows them marked **partial**.

The page label is literally *"SQLs from contacts created during this period,
measured as of [data watermark]"* — never *"contacts that entered SQL during
this period"*.

## 8. API contract

Every `/api/campaigns` response carries `cohort.metadata` (SQLs) and
`cohort.deal_metadata` (deals), each with:

`metric_family` · `window_basis` · `outcome_basis` · `dedup_key` · `as_of` ·
`source_freshness` · `attribution_status` · `mapped_count` ·
`unattributed_count` · `excluded_non_google_count` · `coverage_status` ·
`coverage_notes`

`cohort.lifecycle_event_coverage` declares the other family —
`metric_family: lifecycle_stage_events`, `window_basis: date_entered_sql`,
`published_on_this_page: false` — with the global coverage figures (reached by
stage, direct, recovered, missing, open post-boundary incidents, boundary).

`legacy_sql` declares the legacy fields still returned (`confirmed_sqls`,
`confirmed_sqls_total`, `cpql_usd`, `overall_cpql_usd`, `overall_cpql_scope`,
`mapping_coverage`, `sql_reconciliation`) as `published_on_this_page: false`.

## 9. What did not change

* **The lifecycle coverage gate.** Not edited, not widened, not bypassed. The
  cohort imports nothing from `analysis.lifecycle_sql_coverage` and names
  neither pre-certification key, so it needs no exemption from PR-ADS-161A-1's
  AST guard. `test_12` seeds a real open post-boundary incident and shows
  `audit_sql_coverage_gate` still goes red beside a published cohort.
* **The 103 open incidents** are reported, not resolved. **The boundary** is
  not touched.
* **Junk.** Confirmed junk and junk rate remain the lead-quality
  classification from the `leads` table. The canonical lifecycle taxonomy has
  no junk category, and inventing a mapping onto it is out of scope. Junk and
  Leads acquired are **different populations**, and Junk Rate's denominator
  is verdicted leads in that table, not Leads acquired. Every junk label on
  the page (KPI, table headers, drawer, drawer total) therefore names its
  lead-status basis. The "Junk-heavy" outcome status still reads the legacy
  junk rate.
* **No writes.** Not to HubSpot, not to Google Ads, not to our database. The
  audit runs its own reads under `SET TRANSACTION READ ONLY`.

## 10. Also fixed, found on the way

* **An absence became an accusation.** `_outcome_status` coerced an
  unavailable SQL count to 0 (`confirmed_sqls or 0`), so a campaign with spend
  read *"Spend without SQL proof"* whenever the SQL source was down. It now
  reads *"Data unavailable"*.
* **The drawer threw on open.** `renderCampaignDrawer` read `drawerSqlPub`
  before its `const` declaration — a temporal-dead-zone `ReferenceError` the
  PR-ADS-158 inventory had recorded. Declared before use now (`test_15f`).

## 11. Legacy readers that remain

Campaign Evidence is migrated for its **published** SQL, CPQL, outcome status,
filters, sorts and drawer headline. Still on the legacy
`leads.status_category` population, labelled as such:

* the Campaign drawer's Lead Quality split, Country split and Recent Leads
  (`fetch_campaign_lead_detail`) — shown as *"Qualified (lead status)"*;
* the legacy fields in the `/api/campaigns` payload (§8), consumed by
  `scripts/audit_campaign_evidence_certification.py`.

Every other page is unchanged. The PR-ADS-158 inventory now reads **23 legacy /
8 mixed / 5 canonical** (was 25 / 6 / 4): Campaign Evidence and its drawer move
from legacy to mixed, and this contract is added as canonical. The legacy
consumers include Keyword Evidence, Search Terms, Geo Intelligence, Dashboard
Overview (headline), the Revenue Decision Mart and every page reading it, the
Action Queue and the weekly/monthly reports —
`python -m scripts.audit_sql_doctrine_inventory` lists all of them.

## 12. Guards

`tests/test_pr_ads_161b_marketing_outcome_cohorts.py` — the brief's twelve
required cases plus a counterfactual for every guard:

* **§1–3** proof, membership (incl. BST midnight edges), attribution buckets,
  dedup, deals — on the pure layer, with negative controls (`test_03b`: a
  stage below SQL is not an SQL, or `test_03` would pass for a rule that
  called everyone one).
* **§4** CPQL: zero SQLs → `N/A`; stale and unknown freshness withhold with
  different reasons; never blocked by a missing SQL date; zero spend is never
  a $0 CPQL (`test_11k`, page and row).
* **Publication** (`test_11h`–`11j`): every not-proven freshness verdict, from
  the **real** `assess` over a real-shaped sync row, withholds SQLs, CPQL and
  `cohort_complete` with its own reason. Positive control: only fresh and
  stale publish. Counterfactual: the pre-fix `None`-only rule would have
  published a running bootstrap.
* **§5** no invented date, and the contamination check shown failing on a
  mutated `sql_proof` and refusing to be emptied by a rename.
* **§6–7** through the real `build_campaign_evidence`: row/summary
  reconciliation, metadata contract, unavailable is `None`.
* **§8** the audit: passes a coherent page, goes red on a dropped SQL, a
  disagreement with SQL, undisclosed gaps, a CPQL not drawn from cohort SQLs,
  an SQL moved from Google Ads to excluded (`test_14i`; `test_14j` is the
  sanctioned-mapping positive control), and a CPQL published over a withheld
  count or zero spend (`test_14k`). Lifecycle gaps alone do **not** fail it.
* **The boundary bound** (`test_10e`/`10f`): this PR adds no reader of
  `known_reached_sql_by`. The audit takes the name from
  `audit_sql_coverage_gate.BOUND_COLUMN` rather than spelling it, so it stays
  out of that gate's allow-list. The first commit spelled it and was caught by
  CI's PostgreSQL step (PR-ADS-160 `test_29`). `test_10e` runs the same file
  scan without a database and shows the pre-fix spelling red.
* **§9** the frontend gate executed in `node` over every status, including one
  it has never seen; the three retargeted PR-ADS-157 checks each shown red
  under a mutation of `app.js`.
* **§10** end to end on PostgreSQL: the real funnel, recovered history, spend,
  FX and ledger seeded through the production writers; the audit reconciling
  all six windows; the audit going red when the page leaks an SQL; the
  lifecycle-event reader including a contact the cohort excludes; nothing
  written; the coverage gate still red on a real incident.

Run it: `python -m scripts.audit_marketing_outcome_cohorts` (`--json`). Exit 0
reconciles every window; 1 is a violation; 2 means it could not look.
