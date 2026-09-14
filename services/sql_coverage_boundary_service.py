"""
services/sql_coverage_boundary_service.py

PR-ADS-160 — the line between an unknowable past and a guaranteed future.

Why this exists
---------------
PR-ADS-159 finished the historical question by exhausting it. Production
validation read every candidate:

    1,261  contacts whose lifecycle stage proves they reached SQL
      728  carry HubSpot's direct `hs_v2_date_entered_salesqualifiedlead`
        0  had a timestamp recoverable from lifecycle property history
      533  have no provable SQL-entry timestamp

All 533 returned VALID HubSpot lifecycle history, and none of those histories
contained a transition into `salesqualifiedlead`. There is nothing left to read.
Those 533 dates are not missing from our database — they are absent from
HubSpot. No further engineering can recover them.

So this module does not try. It does two separate things:

**Backwards** it records an upper bound. "By instant B, these contacts had
already reached SQL." That is a genuinely weaker claim than a date, and it is
the strongest true one available. Its only sound use is to DISPROVE membership:
an event known to have happened before B cannot have happened inside a window
that opens after B. It can never confirm membership and never supply a date.

**Forwards** it makes the same gap impossible to create again. After the
boundary, a contact that reaches SQL without an exact timestamp from a permitted
source is an INCIDENT — recorded, attributed to a run, and blocking
certification of every window it might belong to. Before this PR such a contact
simply joined the undated population, indistinguishable from the historical 533.

The permitted sources, unchanged from PR-ADS-159
-------------------------------------------------
1. HubSpot's direct ``hs_v2_date_entered_salesqualifiedlead`` property.
2. A genuine ``salesqualifiedlead`` transition timestamp in HubSpot property
   history.

Never, under any circumstances, a substitute:

    contact creation time · latest lifecycle status · latest status update time
    MQL or opportunity timestamps · ingestion time · campaign observation time
    THE BOUNDARY TIMESTAMP ITSELF · inferred lifecycle ordering

The boundary is on that list. It is an upper bound on an unknown event and this
module is the one place in the repository that knows the difference, so it is
the one place most at risk of blurring it. It never writes ``date_entered_sql``,
never writes ``hubspot_lifecycle_stage_history``, and stores its bounds in their
own tables under a column called ``known_reached_sql_by`` — a name a reader
cannot mistake for an event date without contradicting it.

Guarantees
----------
* **No HubSpot write, ever.** The only HubSpot call is the READ-ONLY lifecycle
  history read already built and audited in PR-ADS-159.
* **Local writes only**, into ``sql_coverage_boundary``,
  ``sql_coverage_boundary_contact`` and ``sql_post_boundary_incident``.
* **Dry run by default.** Nothing is written without an explicit ``apply``.
* **Exactly one completed boundary, ever.** Enforced by a partial unique index,
  not only by a service check — two concurrent establishers would both read "no
  boundary exists" and both insert.
* **Immutable.** Completed boundary rows and bounded-contact rows are never
  updated; database triggers make that a property of the tables. An identical
  replay is a verified no-op; a replay that differs is refused and changes
  nothing.
* **Atomic.** The population snapshot, the observation instant and the rows all
  happen in ONE transaction, with the instant stamped AFTER the snapshot.
* **Fail-closed.** An unreadable input is reported as unavailable, never as an
  empty population — which would establish a boundary that bounds nobody.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from analysis.crm_lifecycle import (
    EVENT_SQL,
    LIFECYCLE_RULE_VERSION,
    stages_implying_event,
)

log = logging.getLogger(__name__)

SOURCE_DATASET = "hubspot/contact_funnel"
MODE_DRY_RUN = "dry_run"
MODE_APPLY = "apply"

#: Stated in the boundary row itself, so the population a boundary bounded is
#: readable years later without reconstructing the code that produced it.
POPULATION_DEFINITION = (
    "contacts whose current HubSpot lifecycle stage proves they reached "
    "salesqualifiedlead, and for which neither the direct "
    "hs_v2_date_entered_salesqualifiedlead property nor a recovered "
    "lifecyclestage history transition supplies an SQL-entry timestamp"
)

# ── Run outcomes · denominator: one invocation ──────────────────────────────
# Following PR-ADS-159-R7: one vocabulary per denominator, every member
# reachable, and a run always names exactly one of these.
RUN_OK = "run_completed"
POPULATION_UNREADABLE = "population_unreadable"
BOUNDARY_STORE_UNREADABLE = "boundary_store_unreadable"
BOUNDARY_WRITE_FAILED = "boundary_write_failed"
BOUNDARY_ALREADY_ESTABLISHED = "boundary_already_established"

#: The final verification read failed. Writes may already have landed, so this
#: is reported with truthful partial-write accounting rather than as "nothing
#: happened".
INCIDENT_STORE_UNREADABLE = "incident_store_unreadable"

RUN_OUTCOMES = (RUN_OK, POPULATION_UNREADABLE, BOUNDARY_STORE_UNREADABLE,
                BOUNDARY_WRITE_FAILED, BOUNDARY_ALREADY_ESTABLISHED,
                INCIDENT_STORE_UNREADABLE)

# ── Incident reasons · denominator: one post-boundary contact ───────────────
# Why a contact that reached SQL after the boundary has no exact date. Never a
# bare "missing": each of these has a different follow-up, and collapsing them
# is the conflation PR-ADS-155-F1 removed one layer down.
#: HubSpot's direct property is absent and history was NOT consulted.
INCIDENT_NO_DIRECT_DATE = "post_boundary_no_direct_sql_date"
#: History was read and holds no transition into salesqualifiedlead.
INCIDENT_HISTORY_NO_SQL = "post_boundary_history_has_no_sql_transition"
#: History was requested and the request itself failed. We did not look.
INCIDENT_HISTORY_UNREADABLE = "post_boundary_history_request_failed"
#: HubSpot returned the contact with no history payload at all.
INCIDENT_HISTORY_ABSENT = "post_boundary_history_payload_absent"

INCIDENT_REASONS = (INCIDENT_NO_DIRECT_DATE, INCIDENT_HISTORY_NO_SQL,
                    INCIDENT_HISTORY_UNREADABLE, INCIDENT_HISTORY_ABSENT)

VOCABULARIES = {
    "run": RUN_OUTCOMES,
    "post_boundary_incident": INCIDENT_REASONS,
}


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _contact_sync_provenance(repo) -> str | None:
    """Which contact-funnel sync state the population was read against.

    Recorded in ``source_run_id`` so a boundary can always be traced to the
    ingestion run whose output it snapshotted. Best-effort: an unreadable sync
    state yields ``None`` rather than a fabricated identifier.
    """
    try:
        state = repo.fetch_contact_funnel_sync_state()
    except Exception:  # noqa: BLE001
        return None
    if not (state or {}).get("available"):
        return None
    row = (state or {}).get("row") or {}
    run = row.get("last_batch_id")
    watermark = row.get("last_modified_watermark")
    if run is None and watermark is None:
        return None
    return f"contact_funnel_sync batch={run} watermark={watermark}"


def _run_id() -> str:
    return f"sqlbound_{uuid.uuid4().hex[:12]}"


def establish_boundary(*, apply: bool = False,
                       source_run_id: str | None = None,
                       _clock_sql: str | None = None) -> dict:
    """Propose — and with ``apply``, record — a prospective coverage boundary.

    Read-only unless ``apply`` is True. The dry run returns exactly what an
    ``--apply`` would write, so approval is given against the real population
    rather than against a promise.

    PR-ADS-160 §2 — **there is no caller-supplied observation instant.** The
    boundary time is stamped DATABASE-SIDE, inside the same transaction that
    reads the population and writes the rows, and strictly after that read. An
    operator-controlled timestamp would allow a boundary whose ``observed_at``
    precedes the observation it claims to describe — and every window would then
    rule contacts out on the strength of a bound that was never observed.

    ``_clock_sql`` is private and exists only so tests can inject a
    deterministic SQL clock expression. It is never a timestamp value, and no
    CLI surface exposes it.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    started = _utcnow()
    run_id = _run_id()

    # 1. Is there already a boundary? Establishing a second one silently would
    #    leave two answers to "when did the guaranteed period begin".
    existing = repo.fetch_active_sql_coverage_boundary()
    if not existing.get("available"):
        return _failed(run_id, started, BOUNDARY_STORE_UNREADABLE,
                       "the boundary store could not be read, so it is unknown "
                       "whether a boundary already exists", apply=apply)
    current = existing.get("boundary")

    # 2. Read the population the boundary would bound.
    population = repo.fetch_boundary_candidate_population()
    if not population.get("available"):
        # Critically NOT an empty population. A boundary established over an
        # unreadable population would bound nobody while looking established,
        # and would then silently fail to exclude anything from any window.
        return _failed(run_id, started, POPULATION_UNREADABLE,
                       "the lifecycle-SQL population could not be read; a "
                       "boundary must never be established over an unknown "
                       "population", apply=apply)

    rows = population.get("rows") or []
    contacts = [{
        "contact_id": r.get("contact_id"),
        "created_at": r.get("created_at"),
        "lifecycle_stage": r.get("lifecycle_stage"),
    } for r in rows if r.get("contact_id")]
    missing_creation = sum(1 for c in contacts if c.get("created_at") is None)

    # The identifier is derived from the RUN, not from a timestamp a caller
    # chose — an id encoding an operator-supplied instant would reintroduce the
    # backdating this section removes, one field over.
    bid = f"boundary_{run_id}"
    proposed = {
        "boundary_id": bid,
        # Stamped database-side at apply time. A dry run has not observed
        # anything durably, so it states that rather than inventing an instant.
        "observed_at": None,
        "observed_at_source": "database clock_timestamp(), stamped after the "
                              "population snapshot, inside the write transaction",
        "lifecycle_rule_version": LIFECYCLE_RULE_VERSION,
        "source_dataset": SOURCE_DATASET,
        # Provenance: which sync run's state the population was read against.
        "source_run_id": source_run_id or _contact_sync_provenance(repo),
        "population_definition": POPULATION_DEFINITION,
        "run_id": run_id,
        "legacy_undated_sql_contacts": len(contacts),
        "contacts_examined": len(rows),
    }

    base = {
        "run_id": run_id,
        "mode": MODE_APPLY if apply else MODE_DRY_RUN,
        "apply": bool(apply),
        # Explicit on every path. There is no HubSpot write in this module.
        "hubspot_writes_performed": False,
        "started_at": started.isoformat(),
        "boundary": proposed,
        "existing_boundary": current,
        "contacts_examined": len(rows),
        "legacy_undated_bounded": len(contacts),
        # These contacts carry NO lower bound either, so no window arithmetic
        # can ever rule them out. Stated up front because they cap what the
        # boundary can achieve for overlapping windows.
        "bounded_without_created_at": missing_creation,
        "rows_to_write": len(contacts) + 1,
        "lifecycle_rule_version": LIFECYCLE_RULE_VERSION,
        "stages_implying_sql": list(stages_implying_event(EVENT_SQL)),
        "vocabularies": {k: list(v) for k, v in VOCABULARIES.items()},
    }

    if current:
        # A completed boundary already exists. There is no replacement path:
        # exactly one may exist, and it is immutable.
        return {**base, "ok": False, "run_outcome": BOUNDARY_ALREADY_ESTABLISHED,
                "reason": BOUNDARY_ALREADY_ESTABLISHED,
                "detail": (
                    f"a completed boundary already exists "
                    f"({current.get('boundary_id')} observed at "
                    f"{current.get('observed_at')}). Exactly one completed "
                    f"boundary may exist and it is immutable — two would be "
                    f"two answers to when the guaranteed period began. There "
                    f"is no replacement path"),
                "boundary_written": False,
                "contacts_written": 0,
                "certification_can_begin": False,
                "finished_at": _utcnow().isoformat()}

    if not apply:
        return {**base, "ok": True, "run_outcome": RUN_OK,
                "boundary_written": False, "contacts_written": 0,
                # A dry run proves what WOULD happen, never that it did.
                "certification_can_begin": False,
                "certification_note": (
                    "this is a dry run — no boundary exists yet, so no window "
                    "can be certified. Re-run with --apply to establish it"),
                "finished_at": _utcnow().isoformat()}

    from db import writers  # noqa: PLC0415

    kwargs = {"clock_sql": _clock_sql} if _clock_sql else {}
    result = writers.establish_sql_coverage_boundary(proposed, **kwargs)
    if not result.get("ok"):
        return _failed(run_id, started, BOUNDARY_WRITE_FAILED,
                       result.get("error") or "the boundary write was not proven",
                       apply=apply, extra=base)

    # The instant the DATABASE stamped, echoed back so the report states what
    # was actually recorded rather than what was proposed.
    written = dict(proposed)
    written["observed_at"] = result.get("observed_at")
    base = {**base, "boundary": written,
            "legacy_undated_bounded": result.get("contacts_written") or 0}

    return {**base, "ok": True, "run_outcome": RUN_OK,
            "boundary_written": True,
            "contacts_written": result.get("contacts_written") or 0,
            "already_applied": bool(result.get("already_applied")),
            # The boundary now exists, so windows opening after it CAN become
            # certifiable. Whether any actually certifies is the audit's call:
            # it also requires freshness and reader reconciliation.
            "certification_can_begin": True,
            "certification_note": (
                "windows opening at or after the boundary can now be assessed "
                "for certification; the audit still requires dataset freshness "
                "and agreement across all canonical readers"),
            "finished_at": _utcnow().isoformat()}


def _coerce_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _failed(run_id, started, reason, detail, *, apply=False, extra=None) -> dict:
    """A run that could not complete. Counts are NULL where nothing was proven."""
    out = dict(extra or {})
    out.update({
        "ok": False,
        "run_id": run_id,
        "mode": MODE_APPLY if apply else MODE_DRY_RUN,
        "apply": bool(apply),
        "run_outcome": reason,
        "reason": reason,
        "detail": detail,
        "hubspot_writes_performed": False,
        "started_at": started.isoformat(),
        "finished_at": _utcnow().isoformat(),
        "boundary_written": False,
        "contacts_written": 0,
        "certification_can_begin": False,
    })
    # An aborted run proves nothing about the population it could not read.
    out.setdefault("contacts_examined", None)
    out.setdefault("legacy_undated_bounded", None)
    if reason in (POPULATION_UNREADABLE, BOUNDARY_STORE_UNREADABLE):
        out["contacts_examined"] = None
        out["legacy_undated_bounded"] = None
    return out


# ═════════════════════════════════════════════════════════════════════════════
# §5 — prospective gap prevention
# ═════════════════════════════════════════════════════════════════════════════

def detect_post_boundary_gaps(*, apply: bool = False, run_id: str | None = None,
                              client=None, history_budget: int = 200) -> dict:
    """Every post-boundary SQL contact must hold an exact entry date.

    Runs after the contact sync. For each contact whose stage proves SQL and
    whose evidence places it at or after the boundary:

      * an exact date from either permitted source  → nothing to do, and any
        open incident for it is RESOLVED;
      * no direct date                              → HubSpot property history
        is read (the same read-only path PR-ADS-159 built and proved);
      * a genuine SQL transition in history         → persisted as evidence;
      * neither                                     → an explicit INCIDENT.

    The incident is the point. Before it, such a contact silently joined the
    undated population and became indistinguishable from the historical 533.

    Read-only unless ``apply``. Never writes to HubSpot.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    started = _utcnow()
    rid = run_id or _run_id()

    boundary_state = repo.fetch_active_sql_coverage_boundary()
    if not boundary_state.get("available"):
        return _gap_failed(rid, started, BOUNDARY_STORE_UNREADABLE,
                           "the boundary store could not be read", apply=apply)
    boundary = boundary_state.get("boundary")
    if not boundary:
        # No boundary yet is not a failure — there is simply no prospective
        # period to police. Reported explicitly so it can never read as "checked
        # and found nothing wrong".
        return {"ok": True, "run_id": rid, "run_outcome": RUN_OK,
                "apply": bool(apply), "hubspot_writes_performed": False,
                "boundary_established": False,
                "started_at": started.isoformat(),
                "finished_at": _utcnow().isoformat(),
                "new_sql_transitions_observed": 0,
                "direct_sql_timestamps_present": 0,
                "history_timestamps_recovered": 0,
                "new_undated_sql_gaps": 0,
                "unresolved_post_boundary_incidents": 0,
                "detail": ("no coverage boundary is established, so there is no "
                           "prospective period to check yet")}

    since = _coerce_utc(boundary.get("observed_at"))
    population = repo.fetch_post_boundary_sql_contacts(
        boundary_id=boundary.get("boundary_id"), since=since)
    if not population.get("available"):
        return _gap_failed(rid, started, POPULATION_UNREADABLE,
                           "the post-boundary population could not be read",
                           apply=apply)

    rows = population.get("rows") or []
    with_exact = [r for r in rows
                  if r.get("effective_date_entered_sql") is not None]
    needs_evidence = [r for r in rows
                      if r.get("effective_date_entered_sql") is None]
    direct_present = sum(1 for r in rows
                         if r.get("direct_date_entered_sql") is not None)

    recovered_rows: list = []
    incidents: list = []
    history_requests = 0

    if needs_evidence:
        recovered_rows, incidents, history_requests = _consult_history(
            needs_evidence, boundary_id=boundary.get("boundary_id"),
            client=client, budget=history_budget)

    persisted_events = 0
    incidents_written = 0
    resolved = 0
    write_error = None

    if apply:
        from db import writers  # noqa: PLC0415

        if recovered_rows:
            res = writers.upsert_lifecycle_stage_history(recovered_rows,
                                                         run_id=rid)
            if not res.get("ok"):
                return _gap_failed(rid, started, BOUNDARY_WRITE_FAILED,
                                   res.get("error") or "evidence write not proven",
                                   apply=apply)
            persisted_events = res.get("persisted") or 0

        if incidents:
            res = writers.record_post_boundary_incidents(incidents, run_id=rid)
            if not res.get("ok"):
                return _gap_failed(rid, started, BOUNDARY_WRITE_FAILED,
                                   res.get("error") or "incident write not proven",
                                   apply=apply,
                                   evidence_persisted=persisted_events)
            incidents_written = res.get("persisted") or 0

        # A contact that now HAS an exact date closes its incident. Resolution
        # is always attributable to which permitted source supplied the date.
        #
        # PR-ADS-160 §4: every one of these writes is CHECKED, and the count
        # reported is what the database persisted — not how many ids we asked
        # about. `len(requested)` would report a clean run while the resolution
        # silently failed, leaving incidents open that the report calls closed.
        closeable = [r["contact_id"] for r in with_exact if r.get("contact_id")]
        recovered_ids = [r["contact_id"] for r in recovered_rows]
        if closeable:
            res = writers.resolve_post_boundary_incidents(
                closeable, resolved_by="direct_property")
            if not res.get("ok"):
                return _gap_failed(rid, started, BOUNDARY_WRITE_FAILED,
                                   res.get("error")
                                   or "direct-property incident resolution "
                                      "was not proven",
                                   apply=apply,
                                   evidence_persisted=persisted_events,
                                   incidents_written=incidents_written,
                                   incidents_resolved=resolved)
            resolved += int(res.get("persisted") or 0)
        if recovered_ids:
            res = writers.resolve_post_boundary_incidents(
                recovered_ids, resolved_by="history")
            if not res.get("ok"):
                return _gap_failed(rid, started, BOUNDARY_WRITE_FAILED,
                                   res.get("error")
                                   or "history incident resolution was not "
                                      "proven",
                                   apply=apply,
                                   evidence_persisted=persisted_events,
                                   incidents_written=incidents_written,
                                   incidents_resolved=resolved)
            resolved += int(res.get("persisted") or 0)

    # The FINAL verification read. If this cannot be made, the run proves
    # nothing about the prospective guarantee and must not report ok — a null
    # open-incident count beside ok:true reads as "checked, all clear" to every
    # consumer that only looks at `ok`.
    open_state = repo.fetch_post_boundary_incidents(status="open")
    if not open_state.get("available"):
        return _gap_failed(rid, started, INCIDENT_STORE_UNREADABLE,
                           "the post-boundary incident store could not be read, "
                           "so the prospective SQL guarantee is unverified for "
                           "this run",
                           apply=apply, evidence_persisted=persisted_events,
                           incidents_written=incidents_written,
                           incidents_resolved=resolved)
    open_count = open_state.get("open_count")

    return {
        "ok": True,
        "run_id": rid,
        "run_outcome": RUN_OK,
        "apply": bool(apply),
        "hubspot_writes_performed": False,
        "boundary_established": True,
        "boundary_id": boundary.get("boundary_id"),
        "boundary_observed_at": (since.isoformat() if since else None),
        "started_at": started.isoformat(),
        "finished_at": _utcnow().isoformat(),
        # ── the scheduler-facing metrics (§5) ────────────────────────────────
        "new_sql_transitions_observed": len(rows),
        "direct_sql_timestamps_present": direct_present,
        "history_timestamps_recovered": len(recovered_rows),
        "history_events_persisted": persisted_events,
        "new_undated_sql_gaps": len(incidents),
        "incidents_written": incidents_written,
        "incidents_resolved": resolved,
        # NULL, never 0, when the incident store could not be read: a window
        # must not certify because an outage made its blockers invisible.
        "unresolved_post_boundary_incidents": open_count,
        "incident_store_available": True,
        # The open incidents themselves, so each WINDOW can resolve membership
        # against its own bounds rather than being handed one global count.
        "open_incidents": open_state.get("rows") or [],
        "history_requests": history_requests,
        "history_budget": int(history_budget),
        "incident_reasons": _count_reasons(incidents),
        "write_error": write_error,
    }


def _consult_history(rows, *, boundary_id, client, budget):
    """Ask HubSpot for real history, once per contact, within a budget.

    Returns ``(recovered_rows, incidents, requests_made)``. A contact the budget
    could not fund is reported as an incident whose reason says the request was
    never made — "we did not look" is never reported as "there is nothing".
    """
    from connectors import hubspot_pull  # noqa: PLC0415
    from services.lifecycle_history_recovery_service import (  # noqa: PLC0415
        PAYLOAD_PRESENT, select_recovered_events,
    )

    recovered: list = []
    incidents: list = []
    requests = 0

    ids = [r["contact_id"] for r in rows if r.get("contact_id")]
    by_id = {r["contact_id"]: r for r in rows if r.get("contact_id")}
    funded = set(ids[:budget])

    # HubSpot's batch history endpoint takes at most 50 contacts and the
    # connector REFUSES more rather than silently truncating. Passing the whole
    # budget in one call would raise, the except below would swallow it, and
    # every contact would be reported as "history unreadable" — a request that
    # never asks, reported as an answer. That is exactly the PR-ADS-159 §1
    # defect, and it is not being reintroduced one layer up.
    history: dict = {}
    failed_chunks: set = set()
    batch = hubspot_pull.HUBSPOT_HISTORY_BATCH_LIMIT
    ordered = [cid for cid in ids if cid in funded]
    for start in range(0, len(ordered), batch):
        chunk = ordered[start:start + batch]
        try:
            requests += 1
            history.update(
                hubspot_pull.fetch_lifecycle_stage_history(
                    chunk, client=client) or {})
        except Exception as exc:  # noqa: BLE001
            # One failed chunk must not be reported as "these contacts have no
            # history" — that is a claim about HubSpot drawn from a request
            # that got no answer. Only this chunk is marked unreadable.
            log.error("[sql_boundary] history read failed for %d contact(s): %s",
                      len(chunk), exc)
            failed_chunks.update(chunk)

    for cid in ids:
        row = by_id[cid]
        base = {
            "contact_id": cid,
            "boundary_id": boundary_id,
            "lifecycle_stage": row.get("lifecycle_stage"),
            "contact_created_at": row.get("created_at"),
        }
        if cid not in funded:
            # The budget could not fund a read for this contact. "We did not
            # look" is recorded as such, never as "there is nothing".
            incidents.append({**base, "history_checked": False,
                              "history_state": None,
                              "reason": INCIDENT_NO_DIRECT_DATE})
            continue
        if cid in failed_chunks:
            incidents.append({**base, "history_checked": True,
                              "history_state": None,
                              "reason": INCIDENT_HISTORY_UNREADABLE})
            continue

        entry = history.get(cid) or {}
        state = entry.get("state")
        if not entry:
            incidents.append({**base, "history_checked": True,
                              "history_state": None,
                              "reason": INCIDENT_HISTORY_UNREADABLE})
            continue
        if state != PAYLOAD_PRESENT:
            incidents.append({**base, "history_checked": True,
                              "history_state": state,
                              "reason": INCIDENT_HISTORY_ABSENT})
            continue

        found, _unresolved = select_recovered_events(
            {"contact_id": cid, "lifecycle_stage": row.get("lifecycle_stage")},
            entry.get("versions") or [], (EVENT_SQL,))
        if found:
            recovered.extend(found)
        else:
            incidents.append({**base, "history_checked": True,
                              "history_state": state,
                              "reason": INCIDENT_HISTORY_NO_SQL})
    return recovered, incidents, requests


def _count_reasons(incidents) -> dict:
    out: dict = {}
    for i in incidents or []:
        r = (i or {}).get("reason")
        out[r] = out.get(r, 0) + 1
    return dict(sorted(out.items()))


def _gap_failed(run_id, started, reason, detail, *, apply=False,
                evidence_persisted=0, incidents_written=0,
                incidents_resolved=0) -> dict:
    """A pass that could not complete, with TRUTHFUL partial-write accounting.

    Some of these failures happen after writes have already landed. Reporting
    them as "nothing happened" would send an operator looking for rows that
    exist, so what was persisted is stated and what was never proven is null.
    """
    return {
        "ok": False,
        "run_id": run_id,
        "run_outcome": reason,
        "reason": reason,
        "detail": detail,
        "apply": bool(apply),
        "hubspot_writes_performed": False,
        "started_at": started.isoformat(),
        "finished_at": _utcnow().isoformat(),
        # What DID land, stated rather than assumed to be nothing.
        "history_events_persisted": evidence_persisted,
        "incidents_written": incidents_written,
        "incidents_resolved": incidents_resolved,
        "partial_local_write": bool(evidence_persisted or incidents_written
                                    or incidents_resolved),
        # A pass that could not complete proves nothing about the prospective
        # population. Unknown, never zero.
        "new_sql_transitions_observed": None,
        "direct_sql_timestamps_present": None,
        "history_timestamps_recovered": None,
        "new_undated_sql_gaps": None,
        "unresolved_post_boundary_incidents": None,
    }
