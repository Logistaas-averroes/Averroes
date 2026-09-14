# 41 — Prospective SQL Coverage Boundary

**PR-ADS-160.** The line between a past whose SQL dates are unknowable and a
future for which the system guarantees exact evidence.

> Consumer migration is **not** in this PR. The 25 legacy SQL consumers
> inventoried by PR-ADS-158 are unchanged and remain for **PR-ADS-161**.

---

## 1. Why a boundary, and not more recovery

PR-ADS-159 finished the historical question by exhausting it. Production
validation read every candidate:

| | |
| --- | --- |
| contacts whose lifecycle stage proves they reached SQL | **1,261** |
| with HubSpot's direct `hs_v2_date_entered_salesqualifiedlead` | **728** |
| with a timestamp recovered from lifecycle property history | **0** |
| with no provable SQL-entry timestamp | **533** |

All 533 returned **valid** HubSpot lifecycle history, and **none** of those
histories contained a transition into `salesqualifiedlead`. No candidates
remain. Those dates are absent from HubSpot — not merely missing from us — so no
further engineering recovers them.

The 533 therefore never receive SQL dates. Not from this PR, not from any other.

What is still available is a strictly weaker but **true** statement:

> By instant **B**, these contacts had **already** reached SQL.

That is an *upper bound* on an unknown event. Its only sound use is to
**disprove** membership — an event known to be over before a window opened
cannot have happened inside it.

---

## 2. The doctrine, unchanged

An exact SQL event requires one of exactly two sources:

1. HubSpot's direct `hs_v2_date_entered_salesqualifiedlead` property;
2. a genuine `salesqualifiedlead` transition timestamp recovered from HubSpot
   property history.

Never a substitute:

    contact creation time · latest lifecycle status · latest status update time
    MQL or opportunity timestamps · ingestion time · campaign observation time
    THE BOUNDARY TIMESTAMP ITSELF · inferred lifecycle ordering

The boundary is on that list. It is the newest and most plausible-looking
substitute, which is exactly why it is named there explicitly.

### How that is made structural rather than remembered

The bound is stored in its own tables, under a column called
`known_reached_sql_by` — a name a reader cannot mistake for an event date
without contradicting it. It is never written to
`hubspot_contact_funnel.date_entered_*`, never written to
`hubspot_lifecycle_stage_history`, and never coalesced into
`effective_date_sql`. `scripts/audit_sql_coverage_gate.py` fails if any module
outside a small allow-list so much as reads it, and fails again if a boundary
instant ever appears in a stage-entry column.

---

## 3. What the ingestion audit found (§1)

Before claiming any prevention, the live path was traced end to end.

| Question | Answer |
| --- | --- |
| How is the SQL property requested? | `CONTACT_FUNNEL_PROPERTIES` in `connectors/hubspot_pull.py`, sent by the watermarked incremental search. It **is** in the list. |
| How does it reach the database? | `normalize_contact_funnel_row` → `date_entered_sql` → `db.writers.upsert_hubspot_contact_funnel`. |
| **Can a sparse payload overwrite an existing date with NULL?** | **Yes — and this was proven, not inferred.** |
| Does a lifecycle-stage change trigger a property-history read? | **No.** History recovery was a manual CLI, wired into nothing. |
| How can a newly qualified contact arrive with no SQL date? | `lifecycle_stage = salesqualifiedlead` with the property absent. Nothing detected it; it silently joined the undated population. |
| What monitored this? | The `contact_funnel` and `lifecycle_events` freshness entries watch row recency. Neither watches SQL-timestamp completeness. |

### The erasure, reproduced against a real PostgreSQL instance

```
first payload   date_entered_sql = 2026-09-02   -> stored
later payload   property absent (NULL)          -> {'ok': True, 'persisted': 1}
stored value    date_entered_sql = None         ← silently erased
```

The staleness guard on `last_modified_at` defends against a **stale** write. It
does nothing about a **sparse** one, and the two are different hazards. The
write reported success while destroying the only proof that a contact had ever
entered SQL — worse than a gap, because it destroyed the evidence that anything
was lost.

**The fix.** Stage-entry columns are now refreshed only from a *present* value:

```sql
date_entered_sql = COALESCE(EXCLUDED.date_entered_sql,
                            hubspot_contact_funnel.date_entered_sql)
```

Deliberately narrow. It applies **only** to stage-entry evidence, which HubSpot
only ever adds to. Lifecycle stage, status and the source fields keep plain
latest-state semantics, because for those a *cleared* value is itself a real
fact that must propagate — `test_15` proves a cleared stage still does.

---

## 4. The boundary record (§2)

Three tables, all local, none touching stage-entry evidence.

`sql_coverage_boundary` — one row per boundary: `boundary_id`, `observed_at`,
`lifecycle_rule_version`, `source_dataset`, `source_run_id`,
`population_definition`, `run_id`, the observed counts, `status`
(`pending`/`complete`/`failed`), failure detail, and creation/completion times.
Only a **complete** boundary is usable.

`sql_coverage_boundary_contact` — one row per bounded contact:
`known_reached_sql_by` (the upper bound), `lifecycle_stage_at_boundary`, and
`created_at_lower_bound` (PR-ADS-159's sound lower bound, carried alongside).

`sql_post_boundary_incident` — one row per post-boundary contact that reached
SQL with no exact date: `reason`, `history_checked`, `history_state`,
`detected_by_run_id`, `status`, and how it was resolved.

The boundary row and its bounded contacts are written in **one transaction**. A
boundary that existed without its contacts would rule nothing out while looking
established; contacts without their boundary would be bounds nobody can trace.

---

## 5. The command (§3)

```bash
# ALWAYS dry-run first. Reads the local database, writes nothing, anywhere.
python -m scripts.establish_sql_coverage_boundary

# Show the exact contacts that would be bounded, not just the count.
python -m scripts.establish_sql_coverage_boundary --show-population

# Only after a human has approved that population:
python -m scripts.establish_sql_coverage_boundary --apply
```

| Exit | Meaning |
| --- | --- |
| 0 | the run completed (a dry run that proposed, or an apply that recorded) |
| 1 | the run could not complete; the report says what, if anything, was written |
| 2 | usage error |

Fail-closed behaviour that matters: a boundary is **never** established over an
unreadable population. That would bound nobody while appearing established, and
would then silently fail to exclude anything from any window for the rest of the
system's life. An unreadable population returns `population_unreadable` with
**null** counts, never zero.

A second boundary is refused unless an explicit `--boundary-id` is given: two
boundaries are two answers to "when did the guarantee begin".

---

## 6. Per-window exclusion (§4)

Two one-directional rules. Each can only ever **disprove** membership; neither
can confirm it or supply a date.

| Rule | Evidence | Excludes when |
| --- | --- | --- |
| creation lower bound (PR-ADS-159) | `created_at` | `created_at >= window_end_exclusive` |
| boundary upper bound (PR-ADS-160) | `known_reached_sql_by` | `known_reached_sql_by <= window_start` |

| Window vs boundary B | Verdict |
| --- | --- |
| opens at or after B | **proven_outside** — the transition was already over |
| straddles B | unresolved — it could fall either side |
| closes before B | unresolved |
| no boundary evidence for the contact | unresolved |
| open-ended (All Time) | unresolved, always |

The comparison is against the window **start**, never the end. Comparing against
the end would place a contact *inside* a window, which the bound cannot support:
"it had reached SQL by B, and B is inside this window" says nothing about
whether the transition happened in this window or an earlier one.

The two rules are never double-counted. A contact both rules exclude is counted
once, and attributed to the boundary only when creation alone could not have
ruled it out.

**All Time stays incomplete forever.** It necessarily contains the historical
period, and no amount of bounding changes that a transition happened at an
unknown instant inside it.

---

## 7. Prospective gap prevention (§5)

After the boundary, `services/sql_coverage_boundary_service.detect_post_boundary_gaps`
runs in the incremental scheduler immediately after the contact sync, over the
contacts that sync just wrote.

For each contact whose stage proves SQL and whose evidence places it at or after
the boundary:

* an exact date from either permitted source → nothing to do, and any open
  incident for it is **resolved**, attributed to the source that supplied it;
* no direct date → HubSpot property history is **read** (the same read-only path
  PR-ADS-159 built and proved);
* a genuine SQL transition in history → persisted as evidence;
* neither → an explicit **incident**.

Incident reasons are distinct, because each has a different follow-up:

| Reason | Meaning |
| --- | --- |
| `post_boundary_no_direct_sql_date` | no direct property, and history was **not** consulted |
| `post_boundary_history_has_no_sql_transition` | history read, and it holds no SQL transition |
| `post_boundary_history_request_failed` | the request failed — **we did not look** |
| `post_boundary_history_payload_absent` | HubSpot returned the contact with no history |

"We did not look" is never reported as "there is nothing".

The scheduler dataset `hubspot/sql_coverage_gaps` exposes: new SQL transitions
observed, direct timestamps present, history timestamps recovered, new undated
gaps, unresolved incidents, and execution errors. **A new gap is an error on the
run**, not a log line — it is the condition this PR exists to surface.

---

## 8. Certification (§6)

Certification is split in two, and neither half can grant it alone.

`analysis/lifecycle_sql_coverage.py` judges the window-local half:

| Status | Meaning |
| --- | --- |
| `eligible` | nothing local blocks certification |
| `not_certifiable_window_precedes_boundary` | contains the unknowable period |
| `not_certifiable_window_overlaps_boundary` | straddles the boundary instant |
| `not_certifiable_open_post_boundary_gaps` | an open incident could belong to it |
| `not_certifiable_unresolved_membership` | historical membership unresolved |
| `not_certifiable_no_boundary_established` | no boundary exists yet |
| `certification_unavailable` | inputs unreadable — unknown, not refused |

`scripts/audit_lifecycle_sql_coverage.py` adds the global half: all 44
window/scope reader combinations must reconcile, and the audit must have been
able to look. A locally eligible window is **not** certified while either fails.

The audit reports the historical and prospective sides **separately** —
`legacy_undated_bounded` and `open_post_boundary_incidents`. "A date HubSpot does
not hold" and "a date we failed to capture" have different remedies, and adding
them would hide the second inside the first.

Exit codes are unchanged: **1** contract violation · **2** execution
unavailability · **3** `--strict` incomplete coverage.

---

## 9. The monitoring gate (§7)

```bash
python -m scripts.audit_sql_coverage_gate          # 0 holds · 1 broken · 2 blind
```

Read-only. Writes nothing, to HubSpot or locally. It fails when:

1. a post-boundary contact reached SQL with no exact timestamp;
2. an exact timestamp **can** be overwritten with NULL — checked *structurally*
   against the writer, because by the time it shows in data the evidence that it
   happened is precisely what was destroyed;
3. a boundary instant appears in a stage-entry column;
4. a module outside the allow-list reads `known_reached_sql_by`;
5. a window reported certified carries unresolved membership;
6. the canonical readers disagree.

Check 2 is the one worth dwelling on: it is the only check that can fire
**before** the damage rather than after it.

---

## 10. Production validation procedure

Documented here; **not executed** by this PR.

```bash
# 1. Confirm the deployed commit
git rev-parse HEAD

# 2. Dry run — reads the local database, writes nothing
python -m scripts.establish_sql_coverage_boundary --json

# 3. Review the exact population that would be bounded
python -m scripts.establish_sql_coverage_boundary --show-population

# 4. Apply ONLY after a human approves that population
python -m scripts.establish_sql_coverage_boundary --apply

# 5. Run the incremental sync (now including gap detection)
python -m scheduler.incremental_sync

# 6. Coverage audit — boundary, per-window certification, 44-way reconciliation
python -m scripts.audit_lifecycle_sql_coverage --json

# 7. Verify zero post-boundary gaps
python -m scripts.audit_sql_coverage_gate --json

# 8. Verify all 44 canonical reads reconcile
#    (read_reconciliation.combinations_compared == 44
#     and reconciliation_complete == true in step 6's output)
```

Expected after step 4: `legacy_undated_bounded` = **533**,
`date_entered_sql` unchanged for all 533, `hubspot_lifecycle_stage_history`
unchanged.

---

## 11. What this PR does not do

* It does **not** invent or backfill dates for the 533. They stay undated.
* It does **not** run the production apply command.
* It writes **nothing** to HubSpot.
* It does **not** migrate the 25 legacy SQL consumers — **PR-ADS-161**.
* It does **not** redesign Campaign Evidence or Lead Intelligence.
* It does **not** publish complete historical SQL totals, or CPQL where coverage
  is incomplete.
* It does **not** claim all-time SQL coverage can ever become complete.
