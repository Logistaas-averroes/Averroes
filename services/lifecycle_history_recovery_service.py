"""
services/lifecycle_history_recovery_service.py

PR-ADS-155 §4 — recover missing stage-entry timestamps from REAL HubSpot
evidence, or leave them missing.

The gap
-------
The canonical funnel counts a stage only on its own HubSpot stage-entry
timestamp. Some contacts carry a lifecycle stage that proves they reached
MQL/SQL/Opportunity/Customer while ``hs_v2_date_entered_<stage>`` is null: the
transition is real, the date is unknown. Those contacts are excluded from the
lifecycle cohort and reported as a coverage gap, because the alternative —
substituting contact creation date, the current-stage date, or the ingestion
timestamp — would put a fabricated date inside a governed funnel.

What was audited before writing any of this
-------------------------------------------
Two possible sources of REAL evidence were checked against the live portal:

* **Legacy per-stage date properties** (``hs_lifecyclestage_lead_date`` and
  friends). A property search on the connected portal returns no such
  properties — it exposes only the ``hs_v2_date_entered_*`` /
  ``hs_v2_date_exited_*`` / ``hs_v2_latest_time_in_*`` family. **This source does
  not exist here.**

* **Property history on ``lifecyclestage``.** HubSpot retains the property's
  version history: each historical value, the timestamp it was set, and the
  source that set it. A version whose value IS a funnel stage is HubSpot's own
  record of the transition into that stage — the same underlying evidence
  ``hs_v2_date_entered_*`` is derived from. **This source is real**, and is what
  this module reads.

So a timestamp recovered here is ingested evidence, not an inference. Where
history holds no version for a stage — the contact was set straight to a later
stage, or the version has aged out of HubSpot's retention — nothing is written,
the timestamp stays NULL, and the cohort keeps reporting the gap. The recovery
is therefore best-effort by construction: it can shrink the gap, and it can
never close it by pretending.

PR-ADS-159 — why the first production run recovered nothing
-----------------------------------------------------------
The first dry run examined 50 contacts and reported ``history_payload_missing``
50 times, which reads as "this portal holds no lifecycle history". It was not.
The batch request was handed a plain dict, the SDK serializes a dict verbatim,
and the body therefore went out asking for ``properties_with_history`` — a field
HubSpot's batch endpoint does not know. History was never requested. The request
now goes out as the SDK model, which maps the field to ``propertiesWithHistory``.

Three things follow from that mistake and are built in here:

* a bounded **individual-read fallback**, so a batch that answers nothing can be
  distinguished from a portal that holds nothing;
* an evidence vocabulary that can SAY "the parameter never reached HubSpot",
  which the previous six states could not;
* an **SQL-specific candidate mode**, so a run aimed at the SQL coverage gap
  does not spend its request budget on stages that cannot resolve it.

Guarantees
----------
* **No HubSpot write, ever.** The only HubSpot calls are the batch READ with
  ``propertiesWithHistory`` and, as a bounded fallback, the single-contact READ
  with the same property. Both are GET/POST reads on the CRM read API.
* **Local-database writes only**, into ``hubspot_lifecycle_stage_history``, a
  table the contact sync does not own. (Writing into
  ``hubspot_contact_funnel.date_entered_*`` would be erased by the next
  incremental sync, whose upsert refreshes every column from HubSpot.)
* **Idempotent.** Keyed on ``(contact_id, funnel_event)``; re-running rewrites
  the same value rather than appending.
* **Resumable.** A durable cursor, so a bounded run can stop and continue.
* **Bounded.** Every run takes an explicit contact limit.
* **Provenance-carrying.** Each row records the HubSpot source type/id, the raw
  stage value, and the run that recovered it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from analysis.crm_lifecycle import (
    EVENT_DATE_COLUMN,
    EVENT_SQL,
    EVENT_STAGE,
    FUNNEL_EVENTS,
    LIFECYCLE_RULE_VERSION,
    normalize_lifecycle_stage,
)
from connectors import hubspot_pull as hubspot_states

log = logging.getLogger(__name__)

SCOPE = "lifecycle_stage_history"
MODE_DRY_RUN = "dry_run"
MODE_APPLY = "apply"

# ── PR-ADS-155-F1 — evidence states, told apart ─────────────────────────────
# The first production dry run examined 50 contacts and reported
# `no_history_version_for_stage` 36 times — which conflated four different
# findings and therefore proved none of them. "HubSpot never returned this
# contact", "it returned no history payload", "it returned an empty history" and
# "it returned history that contains no version for this stage" are four facts
# with four different follow-ups, and only the last two are evidence that the
# transition was never recorded.
#
# The connector reports the payload-level state per contact; these are the
# per-(contact, stage) reasons the report publishes.
HISTORY_REQUEST_UNAVAILABLE = "history_request_unavailable"
HISTORY_PAYLOAD_MISSING = "history_payload_missing"
HISTORY_PAYLOAD_EMPTY = "history_payload_empty"
NO_HISTORY_VERSION = "history_present_no_matching_stage_version"
MATCHING_VERSION_RECOVERED = "matching_stage_version_recovered"
NO_TIMESTAMP_ON_VERSION = "history_version_without_timestamp"

# ── PR-ADS-159-R4 — THREE vocabularies, because there are three denominators ─
# The first cut declared ten states as one flat "evidence vocabulary" and
# claimed they were mutually exclusive. They were not one thing at all: some
# describe a REQUEST, some describe a CONTACT's payload, some describe a
# (contact, stage) GAP. Counting them under one heading is the same conflation
# PR-ADS-155-F1 removed, reintroduced one level up.
#
# Worse, two of the ten were unreachable — declared, exported, and emitted by
# nothing — while two states the code DID emit were absent from the list. The
# exhaustiveness test used a subset check, so it passed on both counts. A
# constant kept alive so a static test goes green is not a contract.
#
# So: one vocabulary per denominator, every member reachable, every emitted
# value a member of its own vocabulary, and the tests EXECUTE each state rather
# than asserting the constant exists.

# ── A · per READ ATTEMPT ─────────────────────────────────────────────────────
# Denominator: one HubSpot request. Answers "did we get an answer at all?"
REQUEST_OK = "history_request_ok"
HISTORY_REQUEST_FAILED = "history_request_failed"
HUBSPOT_AUTHORIZATION_FAILED = "hubspot_authorization_failed"

REQUEST_OUTCOMES = (REQUEST_OK, HISTORY_REQUEST_FAILED,
                    HUBSPOT_AUTHORIZATION_FAILED)

# ── A2 · per RUN ─────────────────────────────────────────────────────────────
# Denominator: one invocation of `recover()`. Answers "did the pass complete,
# and if not, what stopped it?" These were previously bare strings scattered
# through the code, belonging to no vocabulary at all.
RUN_OK = "run_completed"
CONTACT_STORE_UNREADABLE = "contact_store_unreadable"
CHECKPOINT_UNREADABLE = "recovery_checkpoint_unreadable"
LOCAL_WRITE_FAILED = "local_write_failed"
#: The durable cursor was not stored. Evidence rows may ALREADY be persisted —
#: this is a partial local write, never "nothing was written".
CHECKPOINT_WRITE_FAILED = "checkpoint_write_failed"

RUN_OUTCOMES = (RUN_OK, CONTACT_STORE_UNREADABLE, CHECKPOINT_UNREADABLE,
                LOCAL_WRITE_FAILED, CHECKPOINT_WRITE_FAILED,
                HISTORY_REQUEST_FAILED, HUBSPOT_AUTHORIZATION_FAILED)

# ── B · per CONTACT ASKED ────────────────────────────────────────────────────
# Denominator: one contact in one pass. Answers "what did HubSpot's payload
# contain for it?" These are the connector's own states, plus one this service
# owns: a contact whose required individual fallback was never attempted.
PAYLOAD_PRESENT = hubspot_states.HISTORY_PRESENT
PAYLOAD_MISSING = hubspot_states.HISTORY_PROPERTY_ABSENT
PAYLOAD_EMPTY = hubspot_states.HISTORY_EMPTY
CONTACT_NOT_RETURNED = hubspot_states.HISTORY_CONTACT_ABSENT
#: Deferred, NOT adjudicated. The pass stopped here; the contact is still owed
#: an individual read and stays eligible for the next run.
PAYLOAD_FALLBACK_DEFERRED = "history_individual_fallback_deferred"

PAYLOAD_OUTCOMES = (PAYLOAD_PRESENT, PAYLOAD_MISSING, PAYLOAD_EMPTY,
                    CONTACT_NOT_RETURNED, PAYLOAD_FALLBACK_DEFERRED)

# ── C · per (CONTACT, SQL GAP) ───────────────────────────────────────────────
# Denominator: one missing SQL-entry date. Answers "what did the evidence
# resolve to?" The three payload reasons appear here too because, for a gap on
# a contact HubSpot answered with nothing, the payload state IS the answer.
HISTORY_PRESENT_NO_SQL_STAGE = "history_present_no_sql_stage"
HISTORY_SQL_VERSION_NO_TIMESTAMP = "history_sql_version_missing_timestamp"
HISTORY_SQL_TIMESTAMP_INVALID = "history_sql_timestamp_invalid"
HISTORY_SQL_TIMESTAMP_RECOVERED = "history_sql_timestamp_recovered"
#: Emitted ONLY when both supported read paths completed and both returned a
#: definitive no-evidence answer. It is a proof, not a shrug — see
#: `_definitively_unrecoverable`.
UNRECOVERABLE_NO_EVIDENCE = "unrecoverable_no_hubspot_evidence"
SQL_DEFERRED_BY_BUDGET = "sql_recovery_deferred_by_budget"
HISTORY_CONTACT_NOT_RETURNED = CONTACT_NOT_RETURNED

SQL_GAP_OUTCOMES = (
    HISTORY_SQL_TIMESTAMP_RECOVERED,
    HISTORY_PRESENT_NO_SQL_STAGE,
    HISTORY_SQL_VERSION_NO_TIMESTAMP,
    HISTORY_SQL_TIMESTAMP_INVALID,
    UNRECOVERABLE_NO_EVIDENCE,
    SQL_DEFERRED_BY_BUDGET,
    PAYLOAD_MISSING,
    PAYLOAD_EMPTY,
    CONTACT_NOT_RETURNED,
)

# ── D · per (CONTACT, STAGE GAP) in the ALL-STAGE run ────────────────────────
# The generic per-stage names from PR-ADS-155-F1, PLUS the SQL ones — because
# the SQL vocabulary is keyed on the EVENT, not on the run mode. An all-stage
# pass adjudicates the SQL gap too, and when it does it reports the SQL state.
# Omitting them here would make a legitimate emission fall outside its own
# declared vocabulary, which is the defect this section exists to remove.
STAGE_GAP_OUTCOMES = (
    MATCHING_VERSION_RECOVERED,
    NO_HISTORY_VERSION,
    NO_TIMESTAMP_ON_VERSION,
    HISTORY_SQL_TIMESTAMP_RECOVERED,
    HISTORY_PRESENT_NO_SQL_STAGE,
    HISTORY_SQL_VERSION_NO_TIMESTAMP,
    HISTORY_SQL_TIMESTAMP_INVALID,
    UNRECOVERABLE_NO_EVIDENCE,
    PAYLOAD_MISSING,
    PAYLOAD_EMPTY,
    CONTACT_NOT_RETURNED,
    SQL_DEFERRED_BY_BUDGET,
)

#: Every vocabulary, by the thing it counts. A report names which one it is
#: using, so two numbers with different denominators can never be added.
VOCABULARIES = {
    "run": RUN_OUTCOMES,
    "request": REQUEST_OUTCOMES,
    "per_contact_payload": PAYLOAD_OUTCOMES,
    "per_sql_gap": SQL_GAP_OUTCOMES,
    "per_stage_gap": STAGE_GAP_OUTCOMES,
}

# ── E · per DIAGNOSIS RUN ────────────────────────────────────────────────────
#: `history_parameter_dropped_or_unsupported` is in NONE of the four above. It is
#: a verdict about the REQUEST — a statement one contact's payload can never
#: make — and it belongs to the connector's diagnosis vocabulary, where it is
#: reachable and tested. Declaring it as a per-contact state is what made it
#: unreachable in the first place.
#:
#: Re-exported here so a caller can check every vocabulary in one place without
#: importing the HubSpot connector. The lifecycle SQL coverage audit reads only
#: the local database, and must not import a module that can reach an API.
DIAGNOSIS_VERDICTS = tuple(hubspot_states.DIAGNOSIS_VERDICTS)
HISTORY_PARAMETER_UNSUPPORTED = hubspot_states.HISTORY_PARAMETER_UNSUPPORTED

#: Connector payload state → the reason an unrecovered stage reports.
#: A state absent from this map is deliberately NOT defaulted: an unknown state
#: is reported as itself rather than folded into the nearest familiar reason.
#:
#: `HISTORY_CONTACT_ABSENT` no longer folds into `history_payload_missing`.
#: "HubSpot did not return this contact" and "HubSpot returned it with no
#: history" were being counted as one number, and they are different problems:
#: the first is an identity question (deleted, merged, invisible to this token),
#: the second is a retention question.
_PAYLOAD_STATE_REASON = {
    hubspot_states.HISTORY_CONTACT_ABSENT: HISTORY_CONTACT_NOT_RETURNED,
    hubspot_states.HISTORY_PROPERTY_ABSENT: HISTORY_PAYLOAD_MISSING,
    hubspot_states.HISTORY_EMPTY: HISTORY_PAYLOAD_EMPTY,
}

#: HubSpot's per-request ceiling for a batch read that includes property history.
BATCH_SIZE = 50


def _stage_reached(current_stage, event: str) -> bool:
    """Does the contact's CURRENT stage imply it must have entered ``event``?

    The same test the funnel uses to decide a missing timestamp is a coverage
    GAP rather than an ordinary non-conversion. Recovery is attempted only for
    gaps: a contact that never reached a stage has no transition to recover, and
    asking HubSpot about it would be noise.
    """
    from analysis.crm_lifecycle import STAGE_RANK  # noqa: PLC0415

    current_rank = STAGE_RANK.get(normalize_lifecycle_stage(current_stage))
    event_rank = STAGE_RANK.get(EVENT_STAGE[event])
    if current_rank is None or event_rank is None:
        return False
    return current_rank >= event_rank


def missing_events(row: dict, events=None) -> list[str]:
    """Funnel events this contact demonstrably reached with no entry timestamp.

    ``events`` restricts the search — PR-ADS-159 §3's SQL mode passes
    ``(EVENT_SQL,)`` so a contact missing only its ``date_entered_lead`` is
    never fetched from HubSpot on an SQL run. It would cost a request and could
    not resolve an SQL gap.
    """
    stage = row.get("lifecycle_stage")
    wanted = tuple(events) if events else FUNNEL_EVENTS
    return [event for event in FUNNEL_EVENTS
            if event in wanted
            and row.get(EVENT_DATE_COLUMN[event]) is None
            and _stage_reached(stage, event)]


#: PR-ADS-159 §2 — the SQL run reports SQL-specific state names. The generic
#: per-stage reasons stay for the all-stage mode; SQL gets the vocabulary the
#: coverage contract is written in, so a report never has to be translated.
_SQL_STATE = {
    NO_HISTORY_VERSION: HISTORY_PRESENT_NO_SQL_STAGE,
    NO_TIMESTAMP_ON_VERSION: HISTORY_SQL_VERSION_NO_TIMESTAMP,
    MATCHING_VERSION_RECOVERED: HISTORY_SQL_TIMESTAMP_RECOVERED,
}


def evidence_state(event: str, generic_reason: str) -> str:
    """The published state for one (event, generic reason) pair.

    Only the SQL event is remapped. An unrecognised reason is returned as
    itself — never defaulted into a neighbouring state, which is how the first
    version of this vocabulary lost three distinct findings.
    """
    if event != EVENT_SQL:
        return generic_reason
    return _SQL_STATE.get(generic_reason, generic_reason)


def select_recovered_events(row: dict, versions: list, events=None) -> tuple[list, list]:
    """Match a contact's history versions to its missing stage-entry dates. Pure.

    Returns ``(recovered, unresolved)``.

    Selection rule: for each missing event, take the LATEST history version whose
    value normalises to that event's stage. Latest, not earliest, because
    ``hs_v2_date_entered_<stage>`` means "when the contact last entered this
    stage" — a contact that cycled back into a stage has its most recent entry
    recorded there. Picking the first version would quietly give the recovered
    dates a different meaning from the ones read directly, and the funnel mixes
    the two in one column.

    Nothing is invented: an event with no matching version, or a matching version
    carrying no usable timestamp, appears in ``unresolved`` with a reason and
    produces no row.
    """
    by_stage: dict[str, list] = {}
    for version in versions or []:
        stage = normalize_lifecycle_stage(version.get("value"))
        if stage:
            by_stage.setdefault(stage, []).append(version)

    recovered, unresolved = [], []
    for event in missing_events(row, events):
        candidates = by_stage.get(EVENT_STAGE[event]) or []
        dated = [v for v in candidates if v.get("timestamp") is not None]
        if not candidates:
            # History WAS returned and contains no version for this stage. This
            # is the only one of the five states that is real evidence the
            # transition was never recorded — the payload-level states are
            # decided by the caller from the connector's own report.
            unresolved.append({
                "funnel_event": event,
                "reason": evidence_state(event, NO_HISTORY_VERSION),
                "history_versions_seen": len(versions or [])})
            continue
        if not dated:
            # PR-ADS-159 §2: a version that CARRIED a timestamp we could not
            # parse is a different finding from one that carried none. Both
            # arrive as `timestamp: None`; only the raw field tells them apart,
            # and only one of them is a bug on our side.
            malformed = any(v.get("timestamp_raw") not in (None, "")
                            for v in candidates)
            reason = (HISTORY_SQL_TIMESTAMP_INVALID
                      if (malformed and event == EVENT_SQL)
                      else evidence_state(event, NO_TIMESTAMP_ON_VERSION))
            unresolved.append({"funnel_event": event, "reason": reason})
            continue
        best = max(dated, key=lambda v: v["timestamp"])
        recovered.append({
            "contact_id": row.get("contact_id"),
            "funnel_event": event,
            # HubSpot's OWN recorded timestamp, carried through unchanged.
            "entered_at": best["timestamp"],
            "hubspot_property": "lifecyclestage",
            # The raw value HubSpot recorded, not our normalised form, so the row
            # can be audited against the version it came from.
            "hubspot_value": best.get("value"),
            "hubspot_source_type": best.get("source_type"),
            "hubspot_source_id": best.get("source_id"),
            "hubspot_source_label": best.get("source_label"),
            "hubspot_updated_by_user_id": best.get("updated_by_user_id"),
            "lifecycle_rule_version": LIFECYCLE_RULE_VERSION,
            "evidence_state": evidence_state(event, MATCHING_VERSION_RECOVERED),
        })
    return recovered, unresolved


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ── PR-ADS-159-R1 — one durable checkpoint per candidate population ──────────
# The general and SQL-only runs shared `lifecycle_stage_history`, so an SQL-only
# run resumed from whatever cursor the last all-stage run happened to leave.
# Since the all-stage population is a superset ordered by the same key, its
# cursor is usually FAR ahead — and every SQL candidate below it would be
# skipped silently, forever, while the report said the run completed.
#
# The general scope keeps its original name so the existing production
# checkpoint continues to resume exactly where it is.
SCOPE_BY_MODE = {None: SCOPE, EVENT_SQL: f"{SCOPE}:{EVENT_SQL}"}


def checkpoint_scope(event: str | None) -> str:
    """The durable checkpoint scope for one candidate mode.

    An unrecognised event gets its own scope rather than falling back to the
    general one: a new population must not inherit another's cursor, which is
    the exact defect this function exists to prevent.
    """
    if event in SCOPE_BY_MODE:
        return SCOPE_BY_MODE[event]
    return f"{SCOPE}:{event}"


def _definitively_unrecoverable(batch_state, individual_state) -> bool:
    """Did BOTH supported paths complete AND both return an affirmative absence?

    "Unrecoverable" is a claim about HubSpot, so it needs proof from HubSpot —
    and the proof required is exactly what the doctrine says it is: an EMPTY
    history from the batch read AND an EMPTY history from the individual read.

    PR-ADS-159-R8: the first version checked only the SURVIVING state and a
    boolean "the fallback ran". It therefore accepted *missing* batch + empty
    individual, and *contact-not-returned* batch + empty individual, as "both
    paths definitively empty". They are not:

      * a missing payload could still be our own request — that was the §1
        defect, and the whole reason a second path exists;
      * a contact HubSpot did not return is an identity question, not a
        retention one.

    Neither proves the transition was never recorded, and neither earns the
    word. Both outcomes are now carried separately so this can be asked
    honestly, and a `None` individual state (never attempted, or deferred) can
    never satisfy it.
    """
    return batch_state == PAYLOAD_EMPTY and individual_state == PAYLOAD_EMPTY


def _gap_reason(batch_state, individual_state, surviving_state) -> str:
    """The per-gap outcome for a contact HubSpot answered with no history."""
    if _definitively_unrecoverable(batch_state, individual_state):
        return UNRECOVERABLE_NO_EVIDENCE
    # Not defaulted: an unrecognised state is reported as ITSELF.
    return _PAYLOAD_STATE_REASON.get(surviving_state, surviving_state)


#: PR-ADS-159 §4 — the individual-read fallback is bounded by a request budget,
#: not by the candidate count. One request per contact is affordable for a few
#: hundred and is not affordable for a portal-wide scan, so the budget is an
#: explicit ceiling the caller raises deliberately.
DEFAULT_INDIVIDUAL_REQUEST_BUDGET = 200

#: Failures that must STOP a run rather than be counted per contact. A token
#: that cannot read contacts would otherwise be reported as "HubSpot holds no
#: history for any of these 200 contacts", which is the same false conclusion
#: PR-ADS-159 §1 was built on.
_STOP_STATUSES = (401, 403)


def _is_permanent_auth_failure(exc) -> bool:
    status = getattr(exc, "status", None)
    if status in _STOP_STATUSES:
        return True
    # HubSpotRetryableError wraps the original; its message carries the status.
    text = str(exc)
    return any(f"status={code}" in text for code in _STOP_STATUSES)


def recover(*, limit: int, apply: bool = False, resume: bool = True,
            client=None, run_id: str | None = None, event: str | None = None,
            individual_fallback: bool = True,
            individual_request_budget: int = DEFAULT_INDIVIDUAL_REQUEST_BUDGET,
            ) -> dict:
    """Run one bounded recovery pass.

    ``apply=False`` (the default) is a DRY RUN: HubSpot is read, every candidate
    is resolved and counted, and nothing is written anywhere. That is the mode
    that answers "how many of these gaps does HubSpot actually hold evidence
    for?", which is a question that can only be answered against the real portal.

    ``event="sql"`` (PR-ADS-159 §3) selects the SQL-SPECIFIC candidate
    population: contacts whose lifecycle stage proves they reached SQL and which
    have no EFFECTIVE SQL-entry timestamp — no direct property and no previously
    recovered one. Without it the scan spends HubSpot requests on contacts whose
    only gap is a stage that cannot resolve an SQL question.

    ``individual_fallback`` retries a contact the batch read could not answer
    through the supported single-contact read, within
    ``individual_request_budget`` requests for the whole run. It is a fallback,
    never a scan: only contacts the batch failed on are retried.

    PR-ADS-159-R1/R2 — two things this pass will not do:

    * It will not resume from another population's cursor. Each candidate mode
      owns an independent durable checkpoint (:func:`checkpoint_scope`), so an
      SQL-only run cannot inherit an all-stage cursor and skip every SQL
      candidate below it.
    * It will not advance the cursor past a contact it did not fully
      adjudicate. When the individual-read budget runs out, the pass STOPS at
      the first contact that still needs one; that contact and everything after
      it stay eligible for the next run. Advancing over a deferred contact
      would skip it permanently, which is worse than doing less work.

    Fails closed: an unreadable contact store, an unreadable checkpoint, or an
    authentication/permission failure returns ``ok=False`` with a reason rather
    than a run that examined nothing and reported success.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    run_id = run_id or uuid.uuid4().hex
    mode = MODE_APPLY if apply else MODE_DRY_RUN
    started = datetime.now(tz=timezone.utc)
    events = (event,) if event else None
    if event and event not in FUNNEL_EVENTS:
        raise ValueError(f"Unknown funnel event '{event}'")
    scope = checkpoint_scope(event)

    state = repo.fetch_lifecycle_recovery_state(scope=scope)
    if not state.get("available"):
        return _failed(run_id, mode, started, CHECKPOINT_UNREADABLE,
                       "the durable checkpoint could not be read, so a run "
                       "could not be resumed or recorded", scope=scope)
    cursor = (state.get("row") or {}).get("last_contact_id") if resume else None

    # PR-ADS-159-R3 — fetch ONE more than we will process. `len(rows) >= limit`
    # cannot tell "exactly the last page" from "another page exists", and an
    # operator reading a false "no more work" stops early on a real gap.
    fetch_size = int(limit) + 1
    if event == EVENT_SQL:
        candidates = repo.fetch_sql_recovery_candidates(
            after_contact_id=cursor, limit=fetch_size)
    else:
        candidates = repo.fetch_contacts_missing_stage_dates(
            after_contact_id=cursor, limit=fetch_size)
    if not candidates.get("available"):
        return _failed(run_id, mode, started, CONTACT_STORE_UNREADABLE,
                       "the canonical contact store could not be read",
                       scope=scope)

    fetched = candidates.get("rows") or []
    beyond_limit = len(fetched) > int(limit)      # real evidence, not a guess
    rows = fetched[:int(limit)]

    examined = 0
    recovered_rows: list = []
    unresolved_rows: list = []
    contacts_without_history = 0
    # PR-ADS-155-F1: the evidence breakdown. A run that recovers nothing must be
    # able to say WHY it recovered nothing, per state, or its zero is unreadable.
    payload_states: dict = {}
    contacts_with_history_and_match = 0
    contacts_with_history_no_match = 0
    # The cursor is the last FULLY ADJUDICATED contact — never the last one
    # merely looked at.
    last_adjudicated_id = cursor
    individual_requests = 0
    individual_rescued = 0
    deferred_at_contact = None
    deferred_count = 0

    try:
        from connectors import hubspot_pull  # noqa: PLC0415

        for batch in _chunks(rows, BATCH_SIZE):
            if deferred_at_contact is not None:
                break
            history = hubspot_pull.fetch_lifecycle_stage_history(
                [r["contact_id"] for r in batch], client=client)
            for row in batch:
                entry = history.get(row["contact_id"]) or {}
                state = entry.get("state") or CONTACT_NOT_RETURNED
                needs_fallback = (individual_fallback
                                  and state != PAYLOAD_PRESENT)

                # ── R2: stop, do not skip ────────────────────────────────────
                # This contact is owed an individual read and there is no budget
                # left to give it one. Everything from here on is unexamined, so
                # the pass ends and the cursor stays where it was.
                if needs_fallback and individual_requests >= individual_request_budget:
                    deferred_at_contact = row["contact_id"]
                    remaining = rows[rows.index(row):]
                    deferred_count = len(remaining)
                    payload_states[PAYLOAD_FALLBACK_DEFERRED] = (
                        payload_states.get(PAYLOAD_FALLBACK_DEFERRED, 0)
                        + deferred_count)
                    unresolved_rows.extend(
                        {"contact_id": pending["contact_id"], "funnel_event": e,
                         "reason": SQL_DEFERRED_BY_BUDGET,
                         "payload_state": PAYLOAD_FALLBACK_DEFERRED,
                         "adjudicated": False}
                        for pending in remaining
                        for e in missing_events(pending, events))
                    break

                examined += 1
                # PR-ADS-159-R8: both path outcomes are kept. The first version
                # overwrote `state` with the individual read's answer, so the
                # batch outcome was gone by the time anything asked whether BOTH
                # paths had been definitive — and "unrecoverable" was decided
                # without the evidence it claimed.
                batch_state = state
                individual_state = None
                if needs_fallback:
                    individual_requests += 1
                    single = hubspot_pull.fetch_lifecycle_stage_history_single(
                        row["contact_id"], client=client)
                    individual_state = single.get("state")
                    if individual_state == PAYLOAD_PRESENT:
                        individual_rescued += 1
                    # The individual read is the more authoritative SURVIVING
                    # answer; the batch outcome is kept beside it, not replaced.
                    entry = single
                    state = individual_state or batch_state

                payload_states[state] = payload_states.get(state, 0) + 1
                # Adjudicated: every read this contact was owed has been made.
                last_adjudicated_id = row["contact_id"]

                if state != PAYLOAD_PRESENT:
                    # HubSpot returned no usable history payload for this contact.
                    # WHICH kind of nothing it returned is preserved: an absent
                    # record and an affirmatively empty history are different
                    # findings, and only the latter says "no transition was ever
                    # recorded". Neither is a connector failure, and neither is
                    # evidence about a specific stage.
                    contacts_without_history += 1
                    reason = _gap_reason(batch_state, individual_state, state)
                    unresolved_rows.extend(
                        {"contact_id": row["contact_id"], "funnel_event": e,
                         "reason": reason, "payload_state": state,
                         "adjudicated": True,
                         # Both outcomes travel with the row, so a reader can
                         # see WHY it is or is not called unrecoverable.
                         "batch_state": batch_state,
                         "individual_state": individual_state,
                         "both_paths_attempted": individual_state is not None}
                        for e in missing_events(row, events))
                    continue

                found, unresolved = select_recovered_events(
                    row, entry.get("versions") or [], events)
                if found:
                    contacts_with_history_and_match += 1
                else:
                    contacts_with_history_no_match += 1
                recovered_rows.extend(found)
                unresolved_rows.extend(
                    {"contact_id": row["contact_id"], "payload_state": state,
                     "adjudicated": True, "batch_state": batch_state,
                     "individual_state": individual_state, **u}
                    for u in unresolved)
    except Exception as exc:  # noqa: BLE001
        # A partial pass is never reported as a completed one, and the cursor is
        # not advanced past work that was not finished.
        #
        # PR-ADS-159 §4: an authentication or permission failure STOPS the run
        # under its own reason. Retrying it would burn the budget on a call that
        # cannot succeed, and counting it per contact would publish "HubSpot
        # holds no history for these contacts" when the truth is that this token
        # may not read them.
        log.error("[lifecycle_history_recovery] HubSpot read failed: %s", exc)
        reason = (HUBSPOT_AUTHORIZATION_FAILED
                  if _is_permanent_auth_failure(exc)
                  else HISTORY_REQUEST_FAILED)
        return _failed(run_id, mode, started, reason, str(exc),
                       examined=examined, scope=scope)

    persisted = 0
    write_error = None
    if apply and recovered_rows:
        from db import writers  # noqa: PLC0415

        result = writers.upsert_lifecycle_stage_history(recovered_rows,
                                                        run_id=run_id)
        if not result.get("ok"):
            return _failed(run_id, mode, started, LOCAL_WRITE_FAILED,
                           result.get("error") or "write not proven",
                           examined=examined, scope=scope)
        persisted = result.get("persisted") or 0

    contacts_recovered = len({r["contact_id"] for r in recovered_rows})
    if apply:
        # ── PR-ADS-159-R7 — the checkpoint write is checked ──────────────────
        # Its result used to be discarded, so a run whose durable cursor was
        # never saved still returned ok=True. The next run would then re-read
        # the same contacts from the old cursor — wasted HubSpot quota — while
        # the operator had been told the pass completed and was resumable.
        #
        # Evidence rows and the checkpoint are written through two different
        # modules with their own connections, so they are NOT one transaction.
        # Rather than pretend otherwise, a failure here reports the partial
        # state exactly: what was persisted, what was not, and what that costs.
        # Re-running is safe — the evidence upsert is keyed on
        # (contact_id, funnel_event), so a retry rewrites rather than appends.
        # R1/R2: this mode's OWN checkpoint, advanced only to the last contact
        # every required read was made for.
        saved = repo.save_lifecycle_recovery_state({
            "last_contact_id": last_adjudicated_id,
            "contacts_examined": examined,
            "contacts_recovered": contacts_recovered,
            "contacts_without_history": contacts_without_history,
            "events_recovered": persisted,
            "last_run_id": run_id,
            "last_run_mode": mode,
            "last_error": write_error,
        }, scope=scope)
        if not saved.get("ok"):
            return _failed(
                run_id, mode, started, CHECKPOINT_WRITE_FAILED,
                saved.get("error") or "the durable checkpoint was not persisted",
                examined=examined, scope=scope,
                evidence_rows_persisted=persisted,
                contacts_recovered=contacts_recovered,
                unsaved_cursor=last_adjudicated_id)

    return {
        "ok": True,
        "run_id": run_id,
        "mode": mode,
        # One value from RUN_OUTCOMES, on every path. Before R7 the stop reasons
        # were bare strings belonging to no vocabulary, and a completed pass
        # named its outcome nowhere at all.
        "run_outcome": RUN_OK,
        "apply": bool(apply),
        # Always explicit, and always False. There is no HubSpot write path in
        # this module, and the report says so rather than leaving it inferred.
        "hubspot_writes_performed": False,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "resume_from": cursor,
        # R2: the last FULLY ADJUDICATED contact. Never a contact whose required
        # individual read was not made — resuming past one would skip it for good.
        "next_cursor": last_adjudicated_id,
        # R3: from evidence, not from `len(rows) >= limit`. A page that is
        # exactly the last page and a page with another row behind it are
        # different facts, and the old comparison could not tell them apart.
        # Budget-deferred candidates are remaining work too.
        "more_candidates_remain": bool(beyond_limit or deferred_at_contact),
        "candidate_mode": event or "all_stages",
        "checkpoint_scope": scope,
        "contacts_examined": examined,
        "contacts_with_gaps": len(rows),
        "contacts_without_history": contacts_without_history,
        # R2: the fallback's own accounting. A deferred contact is UNATTEMPTED —
        # it is not proven unrecoverable, and it stays eligible next run.
        "individual_requests": individual_requests,
        "individual_rescued": individual_rescued,
        "individual_request_budget": int(individual_request_budget),
        "individual_budget_exhausted": deferred_at_contact is not None,
        "deferred_at_contact": deferred_at_contact,
        "contacts_deferred_by_budget": deferred_count,
        # PR-ADS-155-F1: the evidence breakdown. Production's first dry run
        # recovered 0 of 50 and could not say whether HubSpot had answered at
        # all. These counts make a zero readable, and they are the ONLY basis on
        # which an operator should decide whether `--apply` is worth running.
        "payload_states": dict(sorted(payload_states.items())),
        "contacts_with_history_and_match": contacts_with_history_and_match,
        "contacts_with_history_no_match": contacts_with_history_no_match,
        "evidence_states": _evidence_summary(payload_states, unresolved_rows,
                                             recovered_rows, event=event),
        "contacts_recovered": contacts_recovered,
        "events_recovered": len(recovered_rows),
        "events_persisted": persisted,
        "events_unresolved": len(unresolved_rows),
        "unresolved": unresolved_rows,
        "recovered": recovered_rows,
        "source_system": "hubspot_property_history",
        "lifecycle_rule_version": LIFECYCLE_RULE_VERSION,
    }


def diagnose(*, limit: int = 25, client=None) -> dict:
    """Read-only: which HubSpot read actually returns lifecycle history?

    Takes a bounded sample of real SQL-recovery candidates and runs both reads
    over it. Recovers nothing, writes nothing, and reports only structural
    facts — never a contact's property values.

    This is the check that was missing when the batch parameter was being
    dropped: a payload-state count over one read can only ever say "we got
    nothing", and cannot distinguish a portal without history from a request
    that never asked for any.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415
    from connectors import hubspot_pull  # noqa: PLC0415

    started = datetime.now(tz=timezone.utc)
    sample_size = max(1, min(int(limit or 25), BATCH_SIZE))
    candidates = repo.fetch_sql_recovery_candidates(limit=sample_size)
    if not candidates.get("available"):
        return {"ok": False, "reason": "contact_store_unreadable",
                "detail": "the canonical contact store could not be read",
                "hubspot_writes_performed": False,
                "started_at": started.isoformat()}

    ids = [r["contact_id"] for r in (candidates.get("rows") or [])]
    diagnosis = hubspot_pull.diagnose_lifecycle_history_reads(ids, client=client)
    return {
        "ok": True,
        "mode": "diagnose",
        "hubspot_writes_performed": False,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "candidate_mode": EVENT_SQL,
        "diagnosis": diagnosis,
    }


def _evidence_summary(payload_states: dict, unresolved_rows: list,
                      recovered_rows: list, *, event=None) -> dict:
    """Every evidence state this run observed, counted over what it counts.

    Two labelled denominators: payload states are per CONTACT (HubSpot answers
    once per contact); gap reasons are per (contact, stage). Reporting both under
    one heading would be the conflation this section exists to remove.

    PR-ADS-159-R4: the recovered count is built from each row's OWN
    ``evidence_state``. The first cut added the generic
    ``matching_stage_version_recovered`` constant unconditionally, so an SQL run
    whose rows all carried ``history_sql_timestamp_recovered`` was summarised
    under a name that appeared nowhere in its own output — a summary that
    disagreed with the rows it summarised.
    """
    per_gap: dict = {}
    for row in unresolved_rows:
        reason = row.get("reason")
        per_gap[reason] = per_gap.get(reason, 0) + 1
    for row in recovered_rows:
        state = row.get("evidence_state") or MATCHING_VERSION_RECOVERED
        per_gap[state] = per_gap.get(state, 0) + 1

    vocabulary = ("per_sql_gap" if event == EVENT_SQL else "per_stage_gap")
    return {
        "per_contact_payload_state": dict(sorted(payload_states.items())),
        "per_contact_vocabulary": "per_contact_payload",
        "per_stage_gap_reason": dict(sorted(per_gap.items())),
        # Named, so two counts with different denominators can never be summed
        # by a reader who assumed they described the same thing.
        "per_stage_gap_vocabulary": vocabulary,
        "vocabularies": {name: list(states)
                         for name, states in VOCABULARIES.items()},
    }


def _failed(run_id, mode, started, reason, detail, *, examined=0,
            scope=None, evidence_rows_persisted=None, contacts_recovered=None,
            unsaved_cursor=None) -> dict:
    """A run that could not complete. Counts are NULL where nothing was proven.

    PR-ADS-159-R7: a checkpoint failure is a PARTIAL local write, not a run that
    did nothing. Evidence rows may already be in
    ``hubspot_lifecycle_stage_history`` while the cursor is not — so those three
    optional arguments carry what actually happened, and the report must not be
    rendered as "nothing was written".
    """
    evidence_written = bool(evidence_rows_persisted)
    return {
        "ok": False,
        "run_id": run_id,
        "mode": mode,
        "reason": reason,
        # The same field a completed pass carries, from the same vocabulary.
        "run_outcome": reason,
        "detail": detail,
        # Always explicit and always False, on every path. No HubSpot write
        # exists in this module, and a failed run says so rather than leaving
        # the reader to infer it from a missing field.
        "hubspot_writes_performed": False,
        "checkpoint_scope": scope,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "contacts_examined": examined,
        # A pass that could not finish proves nothing about what is left.
        "more_candidates_remain": None,
        # Unknown, not zero: an aborted pass proves nothing about how much
        # evidence HubSpot holds — EXCEPT where the caller measured it before
        # the failure, which is exactly the checkpoint case.
        "contacts_recovered": contacts_recovered,
        "events_recovered": None,
        "events_persisted": evidence_rows_persisted or 0,
        # ── the partial-write disclosure ────────────────────────────────────
        # `checkpoint_persisted` is False only where the write was ATTEMPTED and
        # failed. On every other failure it was never reached, and "not
        # attempted" is not the same claim as "attempted and failed".
        "local_evidence_persisted": evidence_written,
        "checkpoint_persisted": (False if reason == CHECKPOINT_WRITE_FAILED
                                 else None),
        # Never true on a failed run: the cursor is the only thing that makes a
        # bounded pass resumable, and this pass did not prove it was stored.
        "resumability_proven": False,
        "unsaved_cursor": unsaved_cursor,
        "partial_local_write": (reason == CHECKPOINT_WRITE_FAILED
                                and evidence_written),
    }
