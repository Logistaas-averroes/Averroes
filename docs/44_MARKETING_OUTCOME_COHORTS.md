# 44 — Canonical Marketing Outcome Cohorts and the Campaign Evidence Migration

**PR-ADS-161B.** Campaign Evidence stops losing real SQLs to imperfect HubSpot
lifecycle timestamps, by answering the question a paid-marketing decision
actually asks — and by never confusing it with the question it does not.

Definitions used throughout, and nowhere loosened:

* **SQL** — a distinct HubSpot contact (`contact_id`) that canonical lifecycle
  evidence proves reached SQL (§3).
* **Closed-won deal** — a distinct canonical deal (`deal_id`). **Not published
  by this PR** (§5).
* **Customer** — not published. No unique-customer identity is defined or
  certified, so no count of customers is claimed anywhere on this page.

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
> **as of the canonical contact-funnel watermark**?

The watermark is the last successful incremental sync of the contact funnel
(`analysis.sql_coverage_freshness`). It is not "now": a stale source is
published as of an older watermark, and says so.

| | |
|---|---|
| window basis | `contact_created_at` (`hubspot_contact_funnel.created_at`), on **account-local** Google Ads days |
| outcome basis | `latest_canonical_lifecycle_evidence`, read at the canonical contact-funnel watermark |
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
independently recompute campaign placement; it checks it against the page's
own published numbers.

## 5. Closed-won deals — not published

Review rounds 1–2 published closed-won deals on this page. Round 3 (Copilot
review of `52f674b`) found they could not be certified here, and they are
**removed** — no ledger read, no field, no KPI card, no column, no drawer KPI:

* the deal ledger's own sync coverage and watermark were not checked (the
  canonical revenue path checks them, `services/canonical_revenue_service.py`);
  deals were stamped with the *contact-funnel* watermark;
* cohort placement went through the ledger's `primary_contact_id`, which for
  multi-contact deals is the lowest contact id — a **display** identity
  (`analysis/deal_truth.py` rule 2), not an attribution or acquisition fact;
* deal `attribution_status` was derived from the *contact* cohort's buckets.

Every response instead carries `cohort.closed_won_deals`:
`published_on_this_page: false`, `reason: deferred_until_certified`, and the
four requirements a dedicated PR must meet (ledger coverage and watermark,
deterministic placement, deal-specific attribution with explicit ambiguous /
unattributed buckets, coverage-aware publication). Deals are never relabelled
customers. `test_08`–`08e` and PG `test_20` (with a real won deal seeded in the
ledger) prove the absence; the audit's `closed_won_not_published` check goes red
if any closed-won field reappears.

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

## 7. Publication — one verdict, every surface

One verdict, `sql_publication()`, computed once per response.

| `sql_status` | when |
|---|---|
| `published` | funnel read, buckets reconcile, and the freshness verdict is `source_fresh` or `source_stale` **with** a watermark. Stale is published as of its watermark |
| `withheld` | `cohort_reconciliation_failed`; `data_watermark_unknown` (freshness could not be read, `fresh is None`); or **any other freshness verdict, passed through as the reason**: `source_sync_state_missing`, `source_bootstrap_incomplete`, `source_incremental_provenance_missing`, `source_last_incremental_failed`, `source_no_successful_incremental` |
| `unavailable` | `canonical_funnel_unreadable` |

The withheld freshness verdicts are `fresh=False`, not `None`, in
`analysis/sql_coverage_freshness.assess`. Each means the contact population is
not proven complete, so a count over it is partial, not a total.

**What "withheld" means — round 3.** Rounds 1–2 withheld the *label* but still
sent, and rendered, the raw row counts with a "not published" suffix: the
number the verdict refused was on screen (Copilot HIGH). Now, when the verdict
is not `published`:

* the **payload carries no SQL-derived value at all** — every row's
  `cohort_sqls` / `cohort_sqls_missing_event_timestamp` / `cohort_cpql_usd`, every
  summary `cohort_sqls_*`, the metadata bucket counts, the reconciliation counts
  and `cohort.breakdown` are `null`; coverage notes quote no SQL count; failed
  reconciliation problems go to the server log, the page gets only their count.
  The funnel-wide reached-SQL counts in `cohort.lifecycle_event_coverage`
  (`reached_sql_by_current_stage`, `exact_direct_timestamp`,
  `recovered_timestamp`, `missing_exact_timestamp`) are withheld too
  (`counts_withheld: true`): they are read over the same contact funnel, and the
  first round-3 commit still showed "1,531 contacts reached SQL" beside "no SQL
  count is published" (truth auditor, MAJOR). The **open post-boundary incident
  count stays** — it is an integrity fact the coverage gate reports, not an SQL
  total, and it is never suppressed.
  The audit's `withheld_exposures()` lists any location that breaks this;
* every CPQL — page and **every row, including Mapping Review and no-spend
  rows** — inherits the verdict *and its reason*. Row-only refusals (unmapped
  label, no spend row, unreadable identity mappings) are evaluated only beneath
  a published count: they may narrow one row, never contradict the page;
* row outcome statuses that are drawn from the SQL count (*SQL producer*,
  *Spend without SQL proof*, *No outcome evidence*) are never assigned —
  the row reads *Data unavailable* (or *Mapping review*, which is lead-based);
* every surface renders the withholding word — *Withheld* / *Unavailable* —
  in the cell itself (so also on narrow screens, which label cells from
  `data-label`), never a number;
* filters do not classify and sorts do not rank by SQL or CPQL.

**The one stated exception — the legacy family.** The legacy lead-status
fields declared in `legacy_sql` (`confirmed_sqls`, `cpql_usd`,
`confirmed_sqls_total`, `overall_cpql_usd`, `mapping_coverage`,
`sql_reconciliation`) are a different metric family, kept in the payload for
other readers, and are **not** withheld with the cohort. They are therefore
present while the cohort is withheld. That is safe only because no Campaign
Evidence surface reads them, and it is enforced, not assumed:
`audit_campaign_evidence_certification.check_frontend_gates`
(`legacy_sql_not_consumed`) fails if any page surface reads them or the drawer
reads `camp.confirmed_sqls` / `camp.cpql_usd`; `test_16` shows it red under
mutation. API consumers must treat them as legacy, as the declaration says.

**One verdict, carried, never inferred.** Every row and the summary carry the
verdict (`cohort_sql_status` / `cohort_sql_reason`). `app.js` gates every
surface through `campaignSqlPublication(cohort)` — the cohort **is an
argument**; the function reads no page state — and `campaignRowSqlPublication`,
which can only narrow it. Page surfaces pass the page response's cohort. The
drawer passes **its own** `/api/campaign-detail` response's `cohort`: it is
opened from the Action Queue too, where page state may be absent or belong to
an earlier request (Copilot round 3). `test_17` asserts the same verdict on
summary, rows, page CPQL, row CPQL, outcome status, cohort block and the detail
endpoint across eleven evidence states.

While the population is unproven `coverage_status` is
`cohort_population_not_proven`, never `cohort_complete`; Leads acquired is
counted over that population and is marked **partial**.

**Cohort maturity.** A cohort keeps maturing: a recent window's contacts have
had less time to reach SQL, so its cohort SQLs — and the CPQL divided by them —
are a snapshot as of the watermark that can still change. Every response says
so (`metadata.maturity_note`) and the disclosure renders it.

The page label is literally *"SQLs from contacts created during this period,
measured as of the canonical contact-funnel watermark"* — never *"contacts that
entered SQL during this period"*.

## 8. API contract

Every `/api/campaigns` response — the live one, the database-down one and the
last-resort fallback alike, key for key (`test_17c`) — carries:

* `metric_family: acquisition_cohort_outcomes`;
* `cohort.sql_status` / `sql_reason` / `cpql_status` / `cpql_reason`;
* `cohort.metadata`: `metric_family` · `window_basis` · `outcome_basis` ·
  `dedup_key` · `as_of` · `source_freshness` · `sql_status` · `sql_reason` ·
  `attribution_status` · `mapped_count` · `unattributed_count` ·
  `excluded_non_google_count` · `coverage_status` · `coverage_notes` ·
  `basis_label`;
* `cohort.reconciliation` (status, problem count, identities, counts);
* `cohort.breakdown` (published only), `cohort.window_instants`,
  `cohort.closed_won_deals` (the not-published declaration, §5);
* `cohort.lifecycle_event_coverage`, declaring the other family —
  `metric_family: lifecycle_stage_events`, `window_basis: date_entered_sql`,
  `published_on_this_page: false` — with the global coverage figures (the
  reached-SQL counts `null` and `counts_withheld: true` unless the cohort is
  published), always carrying the full key set of
  `lifecycle_disclosure_skeleton()`;
* `legacy_sql`, declaring the legacy fields still returned
  (`confirmed_sqls`, `confirmed_sqls_total`, `cpql_usd`, `overall_cpql_usd`,
  `overall_cpql_scope`, `mapping_coverage`, `sql_reconciliation`) as
  `published_on_this_page: false`.

Every response also carries `sql_reconciliation` (legacy; on the fallback, the
same keys with `null` values, built without a database read) and the same
`audit` keys. `test_17c` compares the live and fallback responses **recursively,
path for path**; the only permitted differences are the `db_unavailable` flag
and `cohort.breakdown` (an object when published, otherwise `null`).

Unknown values are `null`, never `0`. Every row carries `COHORT_ROW_FIELDS`
(`cohort_contacts_acquired`, `cohort_sql_status`, `cohort_sql_reason`,
`cohort_sqls`, `cohort_sqls_missing_event_timestamp`, `cohort_cpql_usd`,
`cohort_cpql_status`, `cohort_cpql_reason`).

**`/api/campaign-detail`** (the drawer) carries the same `COHORT_ROW_FIELDS` on
its `campaign` card — copied from the one tuple, so the card equals the table
row field for field (`test_17`) — and its own response's `cohort` verdict. It no
longer carries `confirmed_sqls` or the legacy `cpql_usd`; the legacy lead-status
qualified count travels only as `campaign.legacy_lead_status.qualified`
(`published_as_sql: false`), for the labelled Lead Quality split.

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
  PR-ADS-158 inventory had recorded. Declared before use now (`test_15k`).
* **A Q4 time-bomb in CI** (not this PR's code). `test_pr_ads_152`'s endpoint
  test resolved `current_quarter` from the wall clock against July fixtures and
  failed from 2026-10-01 on `main` too. The test now injects its own `now`
  through the service's existing parameter (production window behaviour
  unchanged), with a regression over several wall clocks and a counterfactual
  showing the unpinned call really depended on the date.

## 11. Legacy readers that remain

Campaign Evidence is migrated for its **published** SQL, CPQL, outcome status,
filters, sorts and drawer headline. Closed-won deals are not shown (§5); the
other pages' customer / closed-won readers are untouched by this PR. Still on
the legacy `leads.status_category` population, labelled as such:

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

* **§1–2** proof, membership (incl. BST midnight edges), attribution buckets,
  dedup — on the pure layer, with negative controls (`test_03b`: a stage below
  SQL is not an SQL, or `test_03` would pass for a rule that called everyone
  one).
* **§3** closed-won is not published: no ledger read, no field, declared,
  absent from every UI surface, and the audit red if it reappears.
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
  The split check restates `normalize_source` in SQL using Python's own
  `str.isspace()` set (PostgreSQL's `\s` misses NBSP). `test_25` writes tab,
  newline, NBSP and EM-space spellings through the production writer and
  requires the audit's count to equal `classify_source`'s. `test_25b` shows
  the first SQL normalisation missing them.
* **The boundary bound** (`test_10e`/`10f`): this PR adds no reader of
  `known_reached_sql_by`. The audit takes the name from
  `audit_sql_coverage_gate.BOUND_COLUMN` rather than spelling it, so it stays
  out of that gate's allow-list. The first commit spelled it and was caught by
  CI's PostgreSQL step (PR-ADS-160 `test_29`). `test_10e` runs the same file
  scan without a database and shows the pre-fix spelling red.
* **§6b** one verdict on every surface across eleven states (`test_17`), the
  row-ordering counterfactual (`test_17b`), the fallback's exact live shape
  (`test_17c`) and the database-down response (`test_17d`).
* **§9** the WHOLE production `app.js`, executed in a node `vm`: KPI strip,
  disclosure, table row (incl. its `data-label` mobile cells), filters, sorts,
  outcome badges and drawer rendered under seven withholding verdicts with an
  adversarial payload carrying a sentinel count — none may show it
  (`test_15`–`15h`, positive controls `15b`/`15f`). Eight mutations of
  `app.js`, each bypassing one guard, are each shown to expose the sentinel
  (`test_15n`, incl. the lifecycle-count gate), and the tightened PR-ADS-157
  certification goes red under every gate mutation, a legacy-field read and a
  literal gate argument (`test_16`). The lifecycle counts' withholding is
  proven on the backend (`test_11h`, positive control `test_14m`) and by the
  audit (`test_14l`).
* **§10** end to end on PostgreSQL: the real funnel, recovered history, spend,
  FX and a won deal seeded through the production writers; the audit
  reconciling all six windows; the audit going red when the page leaks an SQL;
  an incomplete bootstrap withheld on the page, every row, the detail endpoint
  and the audit with nothing exposed (`test_26`); the lifecycle-event reader
  including a contact the cohort excludes; nothing written; the coverage gate
  still red on a real incident.

Run it: `python -m scripts.audit_marketing_outcome_cohorts` (`--json`). Exit 0:
every published window reconciles and every withheld window exposes nothing;
1 is a violation; 2 means it could not look.
