# PR-ADS-158 — System-Wide SQL Doctrine Audit and Legacy Consumer Inventory

Doctrine: Averroes canonical truth · Phase 1 — Read Only · Audited commit: `1943a68`
(PR-ADS-157-F1, `main`).

This document records what the audit found. It does not migrate anything, and it
does not write the migration roadmap. Every finding is tagged:

* **[STATIC]** — a fact about the code at the audited commit. Re-verifiable with
  `python -m scripts.audit_sql_doctrine_inventory --static-only`.
* **[RUNTIME]** — varies with production data. Established by the window
  comparison in `python -m scripts.audit_sql_doctrine_inventory`; the numbers
  quoted here come from the production evidence in the brief and from the
  production-shaped test fixture, not from a run against production.
* **[CONFIRMED]** — read directly in the source, line-verified.
* **[INFERRED]** — a strong inference that needs production validation.

---

## 1. Canonical reference standard

The target doctrine is the existing canonical CRM funnel contract
(`services/canonical_crm_funnel_service.py`, PR-ADS-153B):

| Element | Value |
|---|---|
| SQL event | contact entered HubSpot lifecycle stage `salesqualifiedlead` |
| Source property | `hs_v2_date_entered_salesqualifiedlead` |
| Durable column | `hubspot_contact_funnel.date_entered_sql` (COALESCEd with `hubspot_lifecycle_stage_history` recovery at the headline read) |
| Deduplication key | `contact_id` |
| Window date | SQL stage-entry date |
| Missing timestamp | explicit coverage gap (`missing_stage_entry_date`); never replaced with contact creation date |
| Cohort rule | a contact now at Opportunity / Customer remains in its historical SQL cohort |
| Scope lattice | `keyword_attributable ≤ campaign_attributable ≤ google_ads_source ≤ all_source`, every scope derived from the same lifecycle SQL event population |
| Google Ads conversions | a separate advertising metric, never a HubSpot SQL |

## 2. SQL doctrines found in production code [STATIC][CONFIRMED]

The brief expected two. There are **three**.

| # | Doctrine | Definition as coded | Date field | Dedup | Table | Reference implementation |
|---|---|---|---|---|---|---|
| A | Legacy status | `SQL_DEFINITION = "latest status_category = qualified"` | `contact_created_at` | `COALESCE(NULLIF(contact_id,''),'id:'\|\|leads.id)` | `leads` (+ `contact_source_classification`, `gclid_attribution`) | `services/canonical_contact_outcome_service.py:64-66` |
| B | Canonical lifecycle | entered `salesqualifiedlead` | `date_entered_sql` | `contact_id` | `hubspot_contact_funnel` | `services/canonical_crm_funnel_service.py:build_populations` |
| C | Snapshot / JSON | `QUALIFIED = ["CLOSED - Sales Qualified", "CLOSED - Deal Created"]` counted from `data/crm_contacts.json` | none (report) / `run_date` (snapshot) | none | `campaigns` (scheduler snapshot) | `analysis/core.py:272,349-350,547` → `db/writers.py:write_campaigns` |

Doctrine C is upstream of A (`db/writers._map_status_category` maps the same two
`mql_status` strings to `status_category = 'qualified'`) but is counted with no
deduplication, no `lead_truth_exclusions`, no business date, and is served by
`/api/summary`, `/api/dashboard/trends`, the action queue, Historical
Intelligence, and the emailed weekly / monthly report.

**The module named `canonical_contact_outcome_service` is doctrine A.** Every
`sql_reconciliation` block on every page is a legacy-doctrine block; it never
consults the lifecycle event. `cross_page_parity_service` lists every
legacy↔lifecycle pair under `DISTINCT_BY_DESIGN` (“pairs that must NEVER be
compared”), so no existing audit crosses doctrines except
`/api/crm-funnel/audit`.

## 3. Active consumer inventory [STATIC][CONFIRMED]

Full machine-readable records (endpoint, service function, repository query,
table, definition, date field, dedup key, windows, scope, the eight
affects-flags, truth-status behaviour, migration notes, confidence) are in
`analysis/sql_doctrine_registry.py::CONSUMERS` and in the `inventory` array of
the JSON report. Summary at the audited commit:

| Classification | Count | Consumers |
|---|---|---|
| `canonical_lifecycle_active` | 4 | Canonical CRM funnel contract (`/api/crm-funnel*`, Leads page); Dashboard Overview lifecycle activity / cohort; HubSpot contact funnel sync; lifecycle stage-history recovery (CLI) |
| `legacy_status_active` | 25 | Legacy contact-outcome contract; platform SQL attribution; Campaign Evidence table + KPI strip; Campaign drawer; Keyword Evidence; Search Terms (+ flagged); Geo Intelligence country summary; Revenue Decision Mart; Dashboard Campaigns / Countries / Revenue / Deals; `/api/summary`; `/api/dashboard/trends`; Historical Intelligence; Action Queue campaign items; Action Queue geo items; `/api/leads`; campaign identity workbench; Mailchimp audit; GCLID attribution rows; weekly / monthly report pipeline; `campaigns` snapshot writer; `leads` snapshot writer; classification cache writer |
| `mixed_or_adapter_active` | 6 | Dashboard Overview KPI strip (legacy KPIs beside the lifecycle funnel); revenue attribution service (DB path is A, JSON fallback is C); Revenue by Source (raw classification count overridden by the A contract, raw retained when A is unavailable); Dashboard Channels (inherits the override, sums `None` as 0); lead reconciliation (supplies the legacy date field); search-term waste truth audit (prints “lifecycle SQLs” for an A population) |
| `google_ads_conversion_not_sql` | 1 | keyword / search-term / campaign-snapshot conversions; daily CRM delta |
| `diagnostic_comparison_only` | 5 | SQL-truth audit; CRM funnel reconciliation; cross-page parity; campaign certification gate; PR-ADS-158 audit |
| `inactive_legacy` | 2 | revenue attribution JSON fallback (reachable only when the database read fails); Claude advisor mode |

Consumers affecting executive totals: 16. Consumers affecting operational
decisions: 22. Production occurrences discovered and classified: 1,117 across
16 patterns; unclassified: 0.

### Discovery method

`analysis/sql_doctrine_audit.discover_occurrences` scans `api/`, `services/`,
`analysis/`, `db/`, `scripts/`, `scheduler/`, `static/`, `connectors/` for every
line matching one of 19 regex markers (`status_category = 'qualified'`,
`status_category == "qualified"`, `_QUALIFIED` / `LEGACY_QUALIFIED`,
`canonical_contact_outcome_*`, `platform_sql_attribution_*`, `confirmed_sqls`,
`sqls`, `sql_count`, `sql_reconciliation`, `date_entered_sql`,
`hs_v2_date_entered_salesqualifiedlead`, `salesqualifiedlead`,
`contact_created_at`, `CASE WHEN … 'qualified'`, bare `SQLs` labels in
`static/`, `cpql`, `SQL producer` / `Spend without SQL proof` / `has_sql` /
`no_sql`, `canonical_crm_funnel_service` / `crm_funnel_repository`,
`crm_funnel_reconciliation_service` / `sql_truth_audit_service`). Each
occurrence is attributed to its enclosing Python function (via `ast`) or
top-level JavaScript function, then matched against the reviewed rules in
`analysis/sql_doctrine_registry.py::RULES` (most specific path + symbol +
pattern wins). `tests/`, `docs/`, `*.md`, `*_fixtures.*` and `*.json` are
scanned but classified by location and never counted as production. An
occurrence with no rule is `unknown_requires_review` and makes
`audit_complete = false` (exit 1).

## 4. Date-field differences [STATIC][CONFIRMED]

| Date field | Consumers |
|---|---|
| `contact_created_at` (contact creation) | every doctrine-A consumer: Campaign Evidence, Keyword / Search-Term attribution, Revenue Decision Mart and all Dashboard tabs, Revenue by Source, Mailchimp audit, `/api/audit/sql-truth` |
| `run_date` (scheduler sync date) | Geo Intelligence `/api/leads/country-summary`, Action Queue geo items, `/api/leads` — the same `leads` table windowed on a different column under the same Evidence Window label |
| `run_date` (snapshot) | `/api/summary`, `/api/dashboard/trends`, Action Queue campaign items, Historical Intelligence (all read `campaigns.confirmed_sqls`) |
| none | weekly / monthly report (`analysis/core.py`: contacts undated; `grace_days` affects junk only) |
| `date_entered_sql` (stage entry) | `/api/crm-funnel*`, Dashboard Overview lifecycle section |

## 5. Scope differences [STATIC][CONFIRMED]

* Doctrine A scopes are named `all_source_sqls / google_ads_source_sqls /
  campaign_attributable_sqls / keyword_attributable_sqls`; doctrine B names them
  `all_source / google_ads_source / campaign_attributable / keyword_attributable`.
* `keyword_attributable` has **two definitions**: platform attribution assigns a
  contact to a criterion only when exactly one `(campaign_id, exact normalized
  keyword)` matches; the lifecycle funnel marks a contact keyword-attributable
  when it is campaign-attributable **and** carries any `hs_analytics_source_data_2`
  label. They are not the same population.
* Campaign Evidence publishes four A sub-populations (`mapped`, `unmatched`,
  `excluded_not_google`, `total_paid_search`); the headline and CPQL denominator
  are `mapped` only.
* Geo Intelligence and `/api/leads` apply no `paid_search`, pseudo-campaign or
  `lead_truth_exclusions` filter, so their “all-source” is wider than doctrine
  A's all-source.
* Named scopes remain legitimate subsets. The contradiction is not scope; it is
  that the same scope word is computed from three different events.

## 6. Why legacy and lifecycle counts differ [RUNTIME]

For every window the audit reports, on durable contact keys:

* **overlap** — SQL under both doctrines inside the window;
* **legacy_only / lifecycle_only** — in one window population only;
* **date_shifted** — SQL under both doctrines at any time, but the event date
  moved from creation to stage entry, so the contact changed window;
* **legacy_only_never_lifecycle_sql** — legacy qualified with no HubSpot SQL
  entry ever (the PR-ADS-153B `legacy_qualified_never_entered_sql` class);
* **lifecycle_only_never_legacy_qualified** — entered SQL in HubSpot but the
  latest `status_category` is not `qualified`;
* **legacy_sqls_without_hubspot_identity** — `id:<leads.id>` keys that can never
  match;
* **missing_sql_entry_date** — contacts whose current stage proves SQL but carry
  no entry timestamp (the production 40).

Reason codes are attached per window
(`event_date_moved_from_creation_to_stage_entry`,
`legacy_qualified_without_lifecycle_sql_entry`,
`lifecycle_sql_entry_without_legacy_qualified_status`,
`legacy_rows_without_hubspot_identity`,
`lifecycle_sql_reached_without_entry_timestamp`, `legacy_lead_truth_exclusions`,
`populations_identical`). Two equal totals over different contacts are reported
as `totals_equal: true, populations_equal: false`.

**The Campaign Evidence 6-versus-8 [INFERRED until the production run]:** the
brief's 30d evidence shows legacy campaign-attributable 6 and lifecycle
campaign-attributable 8. The audit's production-shaped fixture reproduces
exactly this shape (overlap 4, one date-shifted legacy contact, one legacy
contact HubSpot never marked SQL, four lifecycle-only Google Ads contacts) and
the PostgreSQL integration test asserts it end to end. Which of those causes
holds in production, and in what proportion, is what the Render run of the
command will establish.

## 7. CPQL dependencies [STATIC][CONFIRMED]

Six CPQL-family fields, none on the lifecycle denominator:

| Field | Numerator | Denominator | Scope | Date | Publication |
|---|---|---|---|---|---|
| Campaign Evidence row `cpql_usd` | canonical per-campaign USD spend (FX per `spend_date`) | A, mapped rows | campaign_attributable | `contact_created_at` | per row; UI withholds unless reconciled |
| Campaign Evidence `overall_cpql_usd` | account-wide canonical USD spend | A, `mapped_sqls` only | campaign_attributable | `contact_created_at` | labelled `complete` / `mapped_only`; UI withholds unless reconciled |
| `/api/summary avg_cpql_usd` | `SUM(campaigns.spend_usd)` snapshot | C snapshot | per label | `run_date` | published, unreconciled |
| Historical Intelligence CPQL movement | snapshot spend 30d vs 30d | `COALESCE(confirmed_sqls, 0)` | per label | `run_date` | drives improving / deteriorating |
| `campaigns.cpql_usd` (emailed report table) | 30d spend pull | C JSON | per label | none | emailed; `N/A` on 0 |
| Dashboard Revenue `revenue_per_sql_usd` | canonical revenue | A | campaign_attributable | `contact_created_at` | None on None/0 |

Numerator and denominator cover the same window in the Campaign Evidence
fields and Dashboard Revenue; the snapshot fields share a snapshot, not a
business window. Unmatched SQLs are excluded only in Campaign Evidence and the
Dashboard Revenue field. No CPQL denominator can be incomplete for a missing
stage-entry date today, because none uses stage-entry dates. The runtime report
states per window whether a lifecycle CPQL denominator **would** be complete
(`lifecycle_cpql_denominator_complete`).

## 8. Decision surfaces depending on an SQL count [STATIC][CONFIRMED]

Sixteen, listed in `analysis/sql_doctrine_registry.py::DECISION_SURFACES`.
Highlights:

* “SQL producer” / “Spend without SQL proof” are computed in the backend
  (`campaign_evidence_service._outcome_status`) and gated only in the frontend
  (`campaignSqlPublication()`), only on Campaign Evidence. Dashboard Campaigns
  and Countries compute their own `SQL producer` with a different second label
  (“Spend without SQL / customer proof”) and no gate.
* Campaign verdicts FIX / HOLD / SCALE / CUT come from doctrine C
  (`analysis/core.determine_verdict`): SCALE requires
  `min_confirmed_sqls_30d`; FIX fires on `confirmed_sqls == 0 and spend > 200`;
  `cut.min_zero_sql_days` in `config/thresholds.yaml` has no reader.
* SQL / no-SQL filters, SQL sorts and CPQL sorts are disabled when unproven on
  Campaign Evidence only; Keyword Evidence and Search Terms keep theirs live
  even though Keyword Evidence holds the reconciliation object.
* Action-queue campaign items (+30 for `sqls == 0` with spend) and geo items
  (+20) score on doctrine C and on `run_date`-windowed doctrine A respectively.
* Weekly recommendations (“0 confirmed SQLs with spend”) come from doctrine C.
* `revenue_attribution_service.classify_verdict` substitutes 0 for a withheld
  SQL count when computing a row verdict.

All of these are **row evidence** presented as if from a complete total; only
Campaign Evidence distinguishes the two.

## 9. Coverage gaps [RUNTIME]

* `lifecycle_sql_reached_without_entry_timestamp` — production 40. Blocks a
  complete lifecycle SQL total in every window (a missing date is
  window-independent). Reduced only by running
  `scripts/backfill_lifecycle_stage_history.py --apply`, which is not scheduled.
* `campaign_identity_unavailable_windows` — windows where the Google Ads identity
  contract could not be consulted; campaign / keyword scopes are `null`.
* `legacy_sqls_without_hubspot_identity_windows` — legacy SQLs with an
  `id:<row>` key.
* `windows_with_population_differences` and
  `windows_with_incomplete_lifecycle_timestamp_coverage` are counted in the
  summary.

## 10. Hidden reconciliation causes [STATIC][CONFIRMED] + [RUNTIME]

`canonical_contact_outcome_service._reconciliation_status` returns `partial`
when `counts["stale_classification_contacts"]` or
`counts["missing_classification_contacts"]` is non-zero. Both counts are
accumulated in `build_populations` over **every** in-window deduplicated
contact, SQL or not (lines 300-306). So a non-SQL contact whose latest status
is `unknown` and has no `contact_source_classification` row makes the Campaign
Evidence SQL reconciliation `partial`, which withholds the aggregate SQL total,
overall CPQL, the SQL-dependent filters and sorts, and the SQL-dependent status
badges — even when `all_source = google_ads_source = campaign_attributable`,
excluded = 0, unmatched = 0 and mapping coverage is complete.

The audit reports per window: `sql_contacts_stale_classification`,
`sql_contacts_missing_classification`,
`non_sql_contacts_stale_classification`,
`non_sql_contacts_missing_classification`, whether each category currently
changes the status, `production_status`,
`status_if_only_sql_gaps_counted` (the same production function re-run with
the stale / missing counts restricted to SQL contacts), and
`irrelevant_non_sql_gap_affects_sql_status` — `true` only when removing the
non-SQL gaps changes the production status. `tests/test_pr_ads_158_sql_doctrine_audit.py::test_15`
and the PostgreSQL test `test_hidden_non_sql_gap_downgrades_sql_reconciliation`
prove the mechanism.

A second hidden cause on the lifecycle side [STATIC][CONFIRMED]:
`canonical_crm_funnel_service.reconciliation_status` returns `partial` when any
of the five events has a missing stage date or an unknown lifecycle stage
exists, so the SQL event can be complete while the funnel status is partial.
The audit lists `reasons_not_about_sql` per window.

Nothing is fixed here.

## 11. Legacy dependencies the roadmap must account for [STATIC][CONFIRMED]

1. Every `sql_reconciliation` block (Campaign, Keyword, Search Terms, Dashboard
   Overview, Revenue by Source, Revenue Decision Mart, `/api/leads`) is produced
   by doctrine A. Migrating a page's count without migrating its reconciliation
   block would reconcile a lifecycle number against a legacy population.
2. `cross_page_parity_service` asserts `campaign_attributable_sqls` equal across
   five Dashboard pages and the mart; a page migrated alone breaks parity by
   design.
3. `campaigns.confirmed_sqls` / `cpql_usd` (doctrine C) feed `/api/summary`,
   `/api/dashboard/trends`, action-queue campaign items, Historical Intelligence
   and the emailed report through `db/writers.write_campaigns`; none of them is a
   registered parity identity.
4. Platform attribution (`platform_sql_attribution_service`) attaches SQL
   contacts to keyword criteria on `contact_created_at`; the lifecycle funnel
   has no criterion-level join (PR-ADS-153A), so keyword-level lifecycle
   attribution does not exist yet.
5. Search-term attribution is structurally unavailable under either doctrine
   (no persisted user query).
6. The classification cache (`contact_source_classification.status_category`) is
   written by the incremental sync and the repair endpoint from `mql_status`; it
   has no lifecycle column.
7. `lead_reconciliation_service` and `revenue_recovery_service` keep the legacy
   date field (`contact_created_at`) complete; they become irrelevant to SQL
   once consumers move to `date_entered_sql`, but Campaign Evidence's
   `event_date_safe` diagnostics still depend on them.
8. The frontend publication gate exists only for Campaign Evidence
   (`campaignSqlPublication()`); the backend never withholds an SQL aggregate.
9. `db/crm_funnel_repository.fetch_funnel_contact_page` and
   `fetch_operational_status_counts` filter the bare `date_entered_sql` column
   without the history-recovery COALESCE used by the headline read.

## 12. Defects observed (documented, not fixed) [STATIC][CONFIRMED]

* `static/app.js:renderCampaignDrawer` reads `drawerSqlPub` at line 14205 and
  declares it with `const` at line 14229 in the same block — a temporal dead
  zone. As written, the drawer's SQL gate cannot execute.
* `dashboard_channels_service._build_kpis` sums withheld channel SQL counts as 0.
* `static/app.js:loadGeo` renders a country absent from the lead summary as 0
  SQLs; the Geo table labels Google Ads conversions `Conv.` three columns from
  `SQLs` with no qualifier.
* `revenue_attribution_service.classify_verdict` uses 0 for a withheld SQL count.
* `scripts/audit_search_term_waste_truth.py` prints “lifecycle SQLs” for a
  doctrine-A population.
* `analysis/historical_intelligence.load_campaign_trend_rows` coerces a missing
  `confirmed_sqls` to 0.
* `config/thresholds.yaml: cut.min_zero_sql_days` has no reader.
* `db/crm_funnel_repository.py` declares itself read-only and contains
  `save_lifecycle_recovery_state` (INSERT … ON CONFLICT).

## 13. Unknowns requiring follow-up [INFERRED]

* The production proportions behind 6-versus-8 (date shift vs never-entered vs
  lifecycle-only) — established by the Render run.
* Whether the production stale classification contact is an SQL contact (which
  would make `partial` legitimate for that window) — the per-window
  `classification_gaps` block answers this.
* Whether the 40 missing-timestamp contacts are recoverable from
  `lifecyclestage` property history — only a dry run of
  `scripts/backfill_lifecycle_stage_history.py` can say.
* Whether any external consumer reads `/api/campaigns` without the frontend
  gate (the backend publishes `confirmed_sqls_total` and `overall_cpql_usd`
  unguarded).
* Whether `/api/leads` (no UI caller) and `/api/summary` are still consumed by
  anything; both are live and legacy.

## 14. Evidence needed before the roadmap

The Render run of `python -m scripts.audit_sql_doctrine_inventory --json`
provides, per window: both populations on durable keys, the difference reason
codes, the classification-gap split and the `irrelevant_non_sql_gap_affects_sql_status`
flag, the missing-timestamp count, and publishability of a complete total and a
CPQL denominator under each doctrine. Together with §3 and §11 above, that is
the input the migration roadmap needs. It is not written here.

---

### Command contract

```
python -m scripts.audit_sql_doctrine_inventory            # human report
python -m scripts.audit_sql_doctrine_inventory --json     # machine-readable
python -m scripts.audit_sql_doctrine_inventory --static-only
python -m scripts.audit_sql_doctrine_inventory --json --occurrences   # include every occurrence
```

Exit codes: `0` audit complete (legacy may remain) · `1` audit incomplete /
internal contradiction / unclassified production occurrence · `2` required
database or canonical source unavailable.

Read-only: no HubSpot, Google Ads or Mailchimp call; every pooled connection
this process obtains is set `SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`
so a write would fail at the database; the audit's own modules are proven
write-free by static inspection on every run (`write_safety` in the JSON). No
email address or phone number appears in any output.
