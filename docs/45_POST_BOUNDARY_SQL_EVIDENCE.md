# 45 — Post-Boundary SQL Evidence: Forensics and Gap Prevention

**PR-ADS-161C.** Why post-boundary contacts that reached SQL keep arriving with
no exact SQL-entry timestamp, whose gap each one is, and the one safe repair.

> This PR never manufactures an SQL date. A contact with no exact source
> evidence keeps a NULL date, an open incident, and lifecycle-event publication
> withheld wherever it could belong.

---

## 1. Two questions that are both called "SQLs in this period"

| | Acquisition cohort | Lifecycle-event cohort |
| --- | --- | --- |
| Question | Of the contacts **created** in the window, how many are now proven to have reached SQL? | How many contacts **entered** SQL during the window? |
| Window basis | `contact_created_at` | the exact SQL-entry timestamp |
| SQL proof | direct date, recovered transition, **or** a current stage implying SQL | an exact timestamp, from one of two sources |
| A missing SQL date | disclosed, never blocks | unresolved membership — withholds every window it could belong to |
| Governed by | `services/marketing_outcome_cohort_service.sql_publication` (docs/44) | `services/canonical_sql_publication_service` + `scripts/audit_sql_coverage_gate.py` (docs/41, 43) |
| Reads post-boundary incidents? | **No** — disclosure only | **Yes** — each open incident blocks |

An open post-boundary incident is a lifecycle-event problem. It must never
withhold the acquisition cohort, and the cohort must never be relabelled as an
event count. `test_23` and `test_66` hold both directions.

### Why a stage proves occurrence but not timing

A contact at `opportunity` has, by the one lifecycle rule
(`analysis/crm_lifecycle.stages_implying_event`), reached SQL. That rule ranks
stages; it says nothing about clocks. If the contact went `lead → opportunity`
in one step, HubSpot never held it in `salesqualifiedlead` at all — there is no
moment of SQL entry to read, because in HubSpot's record it did not happen as a
separate event. The acquisition cohort may count it; the lifecycle-event cohort
cannot place it in any window.

---

## 2. The two permitted sources, and their lineage

| Precedence | Source | Stored in | Lineage marker |
| --- | --- | --- | --- |
| 1 | HubSpot `hs_v2_date_entered_salesqualifiedlead` | `hubspot_contact_funnel.date_entered_sql` | the column itself; on a resolved incident `resolved_by = 'direct_property'`, derived in SQL from the stored evidence (never the caller's label) |
| 2 | a `salesqualifiedlead` version in HubSpot `lifecyclestage` history | `hubspot_lifecycle_stage_history` (`funnel_event = 'sql'`) | `hubspot_property`, `hubspot_source_*`, `recovery_run_id`; `resolved_by = 'history'` (derived the same way); `resolved_by_run_id` is set by the atomic writer, NULL on the detector's pre-existing resolution path |

`effective_date_sql` coalesces them in that order and nothing else, unchanged.

### Forbidden as an SQL-entry date

Stated once, in `analysis/post_boundary_sql_forensics.FORBIDDEN_SQL_DATE_SUBSTITUTES`,
and quoted by the audit's output:

    contact creation time · database insertion time · sync time · ingestion time
    boundary time · incident creation time · first time we noticed the contact
    the boundary upper bound · first_observed_at_or_above_sql
    last_known_below_sql_at · any inferred midpoint · opportunity/customer entry

---

## 3. The traced evidence path

```
HubSpot   lifecyclestage + hs_v2_date_entered_salesqualifiedlead + lastmodifieddate
  │  CONTACT_FUNNEL_PROPERTIES (connectors/hubspot_pull.py) — the property IS requested
  ▼
iter_contacts_modified_since — lastmodifieddate watermark, 15-minute overlap
  ▼
normalize_contact_funnel_row → parse_hubspot_timestamp (ISO or epoch ms → UTC)
  ▼
hubspot_contact_funnel_sync_service — sole latest-state writer
  ▼
writers.upsert_hubspot_contact_funnel — last_modified_at guard; stage-entry
  columns COALESCE (PR-ADS-160: absence never erases)
  ▼
hubspot_contact_funnel
  ▼
sql_coverage_boundary_service.detect_post_boundary_gaps — runs in the incremental
  scheduler after the contact sync. Population: current stage implies SQL AND
  (undated AND absent from the boundary snapshot, OR dated at/after the boundary)
  ├── _consult_history → fetch_lifecycle_stage_history (READ-ONLY, ≤ 50 per call)
  │     └── select_recovered_events → hubspot_lifecycle_stage_history
  └── record_post_boundary_incidents / resolve_post_boundary_incidents
  ▼
sql_post_boundary_incident
  ▼
canonical readers (db/crm_funnel_repository.effective_date_sql)
  ▼
analysis/lifecycle_sql_coverage → canonical_sql_publication_service
  ▼
scripts/audit_sql_coverage_gate.py
```

### File ownership map

| Role | Owner |
| --- | --- |
| source request | `connectors/hubspot_pull.py` (`CONTACT_FUNNEL_PROPERTIES`, `fetch_lifecycle_stage_history`, `compare_sql_entry_evidence`) |
| normalization | `connectors/hubspot_pull.normalize_contact_funnel_row`, `parse_hubspot_timestamp`, `_direct_sql_entry` |
| persistence | `db/writers.upsert_hubspot_contact_funnel` (latest state), `upsert_lifecycle_stage_history` (history), `apply_post_boundary_sql_evidence` (161C, see §5.3) |
| incident | `services/sql_coverage_boundary_service.py` (detection) · `db/writers.record_/resolve_post_boundary_incidents` |
| forensics | `analysis/post_boundary_sql_forensics.py` (pure) |
| publication | `services/canonical_sql_publication_service.py` (lifecycle events) · `services/marketing_outcome_cohort_service.py` (cohort) |
| audit | `scripts/audit_post_boundary_sql_incidents.py` · `scripts/audit_sql_coverage_gate.py` |
| repair | `scripts/repair_post_boundary_sql_evidence.py` → `services/post_boundary_sql_evidence_service.repair` |

---

## 4. What the trace found

Four defects, each reproduced on `main`'s production code before it was
changed. These are statements about **code**. Whether any of the 113 production
incidents is caused by them is an empirical question — §7 is how to answer it.

| # | Defect on `main` | Reproduced on `main` | Brief cause |
| --- | --- | --- | --- |
| 1 | **The direct property was never re-read.** The detector re-read `lifecyclestage` history for every undated contact on every run, and never `hs_v2_date_entered_salesqualifiedlead`. A direct date HubSpot holds but the watermark never re-selected stayed invisible forever, recorded as "history has no SQL transition". | HubSpot holds `T`; after two runs: `date_entered_sql = None`, incident `open`, reason `…has_no_sql_transition` | 5, 6 |
| 2 | **Stranded incidents.** An incident whose contact LEFT the detector's population — its direct date turned out to precede the boundary, or its stage fell below SQL — was never examined again. One whose exact date was already stored sat open forever. | direct date stored, incident `open`, open count 1 | 10 |
| 3 | **The resolver closed what it was told to.** `resolve_post_boundary_incidents` closed any id handed in, evidence or not. | no date anywhere; `persisted: 1`, status `resolved` | 4 |
| 4 | **A parse failure was reported as HubSpot's absence.** History holding a `salesqualifiedlead` version whose timestamp would not parse was recorded as "no SQL transition". | reason `…has_no_sql_transition` | 3 |

And one attribution that was never evidence: the gate said of every open
incident *"These are OUR gaps, not HubSpot's"*. Nothing had established that.

---

## 5. What changed

### 5.1 The direct property rides on the history read

`_batch_history_body` now asks for the direct property as a **current** value on
the same request (history is still requested for `lifecyclestage` only).
`_history_from_record` reports it as `present` / `absent` / `unparseable` /
`not_read`. `present` is precedence 1 and outranks history; `unparseable` is
ours and is never folded into `absent`. No extra request is made.

### 5.2 Incidents record what the detector saw

Additive, nullable columns on `sql_post_boundary_incident`:

| Column | Meaning |
| --- | --- |
| `direct_property_state` | the direct property as the read returned it |
| `history_versions_seen`, `history_stage_path` | the history shape — stage values only, no PII |
| `stage_jump_skipped_sql` | TRUE proven skip · FALSE an SQL version exists · NULL undeterminable |
| `last_known_below_sql_at`, `first_observed_at_or_above_sql`, `observation_bounds_basis` | **bounds**, from HubSpot's version timestamps |
| `last_checked_at`, `last_checked_by_run_id` | the latest re-check; `detected_at` is never moved |
| `resolved_by_run_id`, `resolution_evidence_at` | resolution provenance, kept on the row |

A pass that read a history payload replaces the shape as a block; a pass that
did not (failed request, budget) **preserves** the last good shape — a failed
read is not evidence and cannot erase evidence (`test_30`).

Two new reasons: `post_boundary_history_sql_timestamp_unparseable` (ours) and
`post_boundary_history_sql_version_undated` (HubSpot's).

### 5.3 One atomic writer for evidence and its incident

`writers.apply_post_boundary_sql_evidence(direct_rows, history_rows, *, run_id,
resolve_contact_ids)` — ONE transaction:

1. fills `date_entered_sql` **only where it is NULL**, with HubSpot's own direct
   value — never overwrites a stored date;
2. upserts recovered history rows, rewriting only a row that differs;
3. closes incidents **where the same transaction reads evidence back** — the
   database decides, never the caller's list.

Any failure rolls back all three (`test_26`, `test_53`).

**A deliberate, narrow exception to single-writer ownership.** PR-ADS-153B §30
makes the contact sync the sole latest-state writer of `hubspot_contact_funnel`.
Step 1 writes outside it: `date_entered_sql`, only in the one direction that
cannot conflict — a NULL becomes HubSpot's own value of the same property — and,
derived from it in the same statement, `latest_stage_entry_at =
GREATEST(existing, new)` and `updated_at`. `latest_stage_entry_at` is the recency
column behind `lifecycle_events` freshness, so **that freshness signal can move
forward without a successful sync** — by a genuine HubSpot event date, never a
made-up one. The sync's
`COALESCE` keeps it on a sparse payload and replaces it only with a different
non-null HubSpot value, which is exactly what it would do had the sync read the
value itself; `last_modified_at` is not touched, so the next sync still applies
full latest state. The alternative — a second, non-atomic transaction through
the sync — would let evidence land while its incident stayed open, the
contradiction §8.8 forbids.

The pre-existing `resolve_post_boundary_incidents` now carries the same
evidence predicate and stamps `resolution_evidence_at`.

### 5.4 Stranded incidents are swept — closed only on stored evidence

After its final read, the detector takes every open incident whose contact is
outside this run's population and asks the atomic writer to close those the
store can prove. The rest stay open and are counted as
`open_incidents_outside_population`. A contact whose stage fell below SQL is
**not** closed: falling out of the population is not proof that its SQL entry
fell outside any window. Closing it would need a non-membership proof this PR
does not claim.

### 5.5 Observation bounds

`last_known_below_sql_at` — the last instant HubSpot recorded the contact below
SQL, taken only from the version immediately before the current at-or-above-SQL
run, and only when that version has a funnel rank below SQL (`other` or a custom
stage proves nothing and leaves it NULL). `first_observed_at_or_above_sql` — the
first instant of that run. For a stage jump the second is an **opportunity**
timestamp.

They are stored, published and named as bounds. They are **not** consumed by
certification in this PR: using them to rule an incident out of a window would
certify more windows, and that change deserves its own review. Today an
incident is ruled out only by PR-ADS-160's rules (`contact_created_at` lower,
`detected_at` upper).

---

## 6. Incident vocabulary

The status column keeps PR-ADS-160's two values, `open` and `resolved`.
`resolved` requires stored exact evidence (`direct_property` or `history`).
**No new closing status was added.** The gate counts `status = 'open'`, so a
status like `bounded_but_not_exact` would silently stop blocking.

The brief's richer vocabulary lives where it belongs — in the forensic
classification, computed, never a way to close anything:

| Owner | Classification | Meaning |
| --- | --- | --- |
| code-owned | `stored_evidence_incident_open` | the date is stored; the closure was lost |
| | `candidate_not_refreshed` | HubSpot changed after our stored copy; the sync has not re-read it |
| | `late_property_not_refreshed` | HubSpot set the property after we last ingested that version |
| | `direct_property_unparseable` | HubSpot sent a value we could not parse |
| | `history_has_exact_sql_transition` | HubSpot history holds a dated SQL version we did not store |
| | `history_sql_timestamp_unparseable` | an SQL version whose timestamp we could not parse |
| source | `stage_jump_skipped_sql` | below SQL → above SQL, no SQL version anywhere |
| | `history_has_no_sql_transition` | history read; no SQL version |
| | `history_sql_version_undated` | an SQL version HubSpot recorded without a time |
| not determined | `history_request_failed` · `history_payload_absent` · `history_not_consulted` · `cause_unresolved` | we did not look, or got no answer |

Facts (several per incident) use the brief's vocabulary verbatim, plus
`outside_detector_population`, `source_contact_not_returned` and
`direct_property_set_before_last_ingest`.

Three rules keep a classification from outrunning its evidence:

* **A source verdict needs the direct property read as `absent`.** History
  alone cannot say HubSpot holds no exact entry; defect 1 was a direct date
  HubSpot held while history showed no SQL version. Without that read the
  verdict is `cause_unresolved`, and the history facts are kept.
* **`writer_dropped_evidence` is never emitted.** HubSpot having set the
  property before our last *ingestion* does not prove the payload we *read*
  carried it, because ingestion follows the read. Proving writer loss needs the
  payload itself, which is not recorded. That ordering is reported as the fact
  `direct_property_set_before_last_ingest` with `cause_unresolved`.
* **An undated version voids adjacency.** It could sit anywhere in the order,
  so neither bound nor a stage jump is claimed. An SQL version still proves
  "not a skip".

---

## 7. Operations

### The forensic audit — read-only, always

```bash
python -m scripts.audit_post_boundary_sql_incidents              # local store only
python -m scripts.audit_post_boundary_sql_incidents --json
python -m scripts.audit_post_boundary_sql_incidents --sample 25
python -m scripts.audit_post_boundary_sql_incidents --compare-hubspot --sample 25 --json
```

Without `--compare-hubspot` no HubSpot call is made. It makes three database
reads, all plain `SELECT`s: the contact-funnel freshness state, the boundary,
and the incidents beside their stored evidence. The last — the one the
classifications come from — runs in a `REPEATABLE READ, READ ONLY` transaction,
where PostgreSQL refuses a write (`test_41`); the first two run in their own
snapshots, so the reported boundary and freshness can describe a slightly
different instant from the incident rows. Exit **0** no code-owned loss traced (not "no incidents") · **1**
code-owned loss, or a resolved incident with no stored evidence · **2**
unavailable (counts are NULL, never 0).

The local mode cannot see HubSpot, so it cannot detect the upstream-present
causes. Incidents recorded before this PR carry no direct-property state and no
history shape, so the local audit reports them as `cause_unresolved` (not
determined), not as HubSpot's gap. **The next incremental sync re-checks the
open incidents still in the detector's population** — current stage implies
SQL and undated (outside the boundary snapshot), up to `history_budget` (200)
per run, in `contact_id` order — and records both. If HubSpot holds a direct
date, that sync persists it and closes the incident. An incident outside that
population (stage fell below SQL, contact gone) is never re-read from HubSpot;
the sweep closes it only on stored evidence, and it stays not determined until
`--compare-hubspot` is run. Run the local audit after that sync, or use
`--compare-hubspot`. A comparison in which any read fails exits 2: it is
incomplete, not a finding.

`candidate_not_refreshed` fires whenever HubSpot modified the contact after our
stored copy — which includes ordinary latency between two daily syncs. It is
code-owned and exits 1: noisy rather than silent, on purpose. Re-run after a
successful sync before treating it as a watermark bug.

### The repair — dry run first

```bash
python -m scripts.repair_post_boundary_sql_evidence --dry-run --json
python -m scripts.repair_post_boundary_sql_evidence --apply --json
```

Reads HubSpot (read-only) for open incidents through the detector's own read
and selection. Reports `examined`, `recoverable_from_stored_evidence`,
`recoverable_direct_property`, `recoverable_lifecycle_history`, `unresolved` by
reason, `written`, `unchanged`, `incidents_resolved`, `open_before` /
`open_after`. `--apply` is one local transaction; a failure writes nothing.
`status` is `success` / `partial` (a contact could not be read, or came back
with no history payload — PR-ADS-159 showed that can be our own request) /
`failed` /
`unavailable`. Exit 0 / 1 / 2.

### Responding to an open incident

| Classification | Action |
| --- | --- |
| any code-owned | a bug: fix the path, then `--dry-run`, review, `--apply` |
| `stage_jump_skipped_sql`, `history_has_no_sql_transition`, `history_sql_version_undated` | nothing recovers it. It stays open and keeps lifecycle-event windows it could belong to withheld. The acquisition cohort is unaffected |
| `history_request_failed`, `history_payload_absent`, `history_not_consulted` | re-run after the next sync, or `--compare-hubspot` |
| `cause_unresolved` | `--compare-hubspot`; if still unresolved, investigate by contact id |

### Post-merge validation (do not `--apply` first)

```bash
git rev-parse HEAD
python -m scripts.audit_post_boundary_sql_incidents --json; echo "INCIDENT_AUDIT_EXIT=$?"
python -m scripts.audit_post_boundary_sql_incidents --compare-hubspot --sample 25 --json; echo "HUBSPOT_COMPARE_EXIT=$?"
python -m scripts.repair_post_boundary_sql_evidence --dry-run --json; echo "REPAIR_DRY_RUN_EXIT=$?"
python -m scripts.audit_marketing_outcome_cohorts; echo "COHORT_AUDIT_EXIT=$?"
python -m scripts.audit_campaign_evidence_certification; echo "CAMPAIGN_CERT_EXIT=$?"
python -m scripts.audit_sql_coverage_gate; echo "SQL_GATE_EXIT=$?"
```

Expected before any repair: `COHORT_AUDIT_EXIT=0`, `CAMPAIGN_CERT_EXIT=0`,
`SQL_GATE_EXIT=1` while incidents remain open. A non-zero gate after `--apply`
is acceptable only when the audit attributes every remaining incident to the
source. It is then not "certified".

---

## 8. What this PR does not do

* It does not move, replace or rewrite the boundary.
* It does not date the historical undated contacts.
* It does not promise any of the 113 is recoverable.
* It does not make `known_reached_sql_by`, `detected_at` or either new bound an
  event date, and it does not use the new bounds for certification.
* It does not change the acquisition cohort's definition or the gate's verdicts.
* It does not close an incident without stored exact evidence.
* It writes nothing to HubSpot or Google Ads.
* It adds no System Status or API surface. None exists for incidents, so per the
  brief, nothing is extended. The CLI audit carries the fields listed there.
