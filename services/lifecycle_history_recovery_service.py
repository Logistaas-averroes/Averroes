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

Guarantees
----------
* **No HubSpot write, ever.** The only HubSpot call is a batch READ with
  ``propertiesWithHistory``.
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
    EVENT_STAGE,
    FUNNEL_EVENTS,
    LIFECYCLE_RULE_VERSION,
    normalize_lifecycle_stage,
)

log = logging.getLogger(__name__)

SCOPE = "lifecycle_stage_history"
MODE_DRY_RUN = "dry_run"
MODE_APPLY = "apply"

#: Why one contact/stage produced no recovered row. Reported, never silently
#: dropped: "HubSpot has no record of this transition" is a finding.
NO_HISTORY_VERSION = "no_history_version_for_stage"
NO_TIMESTAMP_ON_VERSION = "history_version_without_timestamp"

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


def missing_events(row: dict) -> list[str]:
    """Funnel events this contact demonstrably reached with no entry timestamp."""
    stage = row.get("lifecycle_stage")
    return [event for event in FUNNEL_EVENTS
            if row.get(EVENT_DATE_COLUMN[event]) is None
            and _stage_reached(stage, event)]


def select_recovered_events(row: dict, versions: list) -> tuple[list, list]:
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
    for event in missing_events(row):
        candidates = by_stage.get(EVENT_STAGE[event]) or []
        dated = [v for v in candidates if v.get("timestamp") is not None]
        if not candidates:
            unresolved.append({"funnel_event": event, "reason": NO_HISTORY_VERSION})
            continue
        if not dated:
            unresolved.append({"funnel_event": event,
                               "reason": NO_TIMESTAMP_ON_VERSION})
            continue
        best = max(dated, key=lambda v: v["timestamp"])
        recovered.append({
            "contact_id": row.get("contact_id"),
            "funnel_event": event,
            "entered_at": best["timestamp"],
            "hubspot_property": "lifecyclestage",
            # The raw value HubSpot recorded, not our normalised form, so the row
            # can be audited against the version it came from.
            "hubspot_value": best.get("value"),
            "hubspot_source_type": best.get("source_type"),
            "hubspot_source_id": best.get("source_id"),
            "hubspot_updated_by_user_id": best.get("updated_by_user_id"),
            "lifecycle_rule_version": LIFECYCLE_RULE_VERSION,
        })
    return recovered, unresolved


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def recover(*, limit: int, apply: bool = False, resume: bool = True,
            client=None, run_id: str | None = None) -> dict:
    """Run one bounded recovery pass.

    ``apply=False`` (the default) is a DRY RUN: HubSpot is read, every candidate
    is resolved and counted, and nothing is written anywhere. That is the mode
    that answers "how many of these gaps does HubSpot actually hold evidence
    for?", which is a question that can only be answered against the real portal.

    Fails closed: an unreadable contact store, or an unreadable checkpoint,
    returns ``ok=False`` with a reason rather than a run that examined nothing
    and reported success.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    run_id = run_id or uuid.uuid4().hex
    mode = MODE_APPLY if apply else MODE_DRY_RUN
    started = datetime.now(tz=timezone.utc)

    state = repo.fetch_lifecycle_recovery_state()
    if not state.get("available"):
        return _failed(run_id, mode, started,
                       "recovery_checkpoint_unreadable",
                       "the durable checkpoint could not be read, so a run "
                       "could not be resumed or recorded")
    cursor = (state.get("row") or {}).get("last_contact_id") if resume else None

    candidates = repo.fetch_contacts_missing_stage_dates(after_contact_id=cursor,
                                                         limit=limit)
    if not candidates.get("available"):
        return _failed(run_id, mode, started, "contact_store_unreadable",
                       "the canonical contact store could not be read")

    rows = candidates.get("rows") or []
    examined = 0
    recovered_rows: list = []
    unresolved_rows: list = []
    contacts_without_history = 0
    last_contact_id = cursor

    try:
        from connectors import hubspot_pull  # noqa: PLC0415

        for batch in _chunks(rows, BATCH_SIZE):
            history = hubspot_pull.fetch_lifecycle_stage_history(
                [r["contact_id"] for r in batch], client=client)
            for row in batch:
                examined += 1
                last_contact_id = row["contact_id"]
                versions = history.get(row["contact_id"])
                if not versions:
                    # Asked, and HubSpot holds nothing. A finding, not a failure.
                    contacts_without_history += 1
                    unresolved_rows.extend(
                        {"contact_id": row["contact_id"],
                         "funnel_event": e, "reason": NO_HISTORY_VERSION}
                        for e in missing_events(row))
                    continue
                found, unresolved = select_recovered_events(row, versions)
                recovered_rows.extend(found)
                unresolved_rows.extend({"contact_id": row["contact_id"], **u}
                                       for u in unresolved)
    except Exception as exc:  # noqa: BLE001
        # A partial pass is never reported as a completed one, and the cursor is
        # not advanced past work that was not finished.
        log.error("[lifecycle_history_recovery] HubSpot read failed: %s", exc)
        return _failed(run_id, mode, started, "hubspot_read_failed", str(exc),
                       examined=examined)

    persisted = 0
    write_error = None
    if apply and recovered_rows:
        from db import writers  # noqa: PLC0415

        result = writers.upsert_lifecycle_stage_history(recovered_rows,
                                                        run_id=run_id)
        if not result.get("ok"):
            return _failed(run_id, mode, started, "local_write_failed",
                           result.get("error") or "write not proven",
                           examined=examined)
        persisted = result.get("persisted") or 0

    contacts_recovered = len({r["contact_id"] for r in recovered_rows})
    if apply:
        repo.save_lifecycle_recovery_state({
            "last_contact_id": last_contact_id,
            "contacts_examined": examined,
            "contacts_recovered": contacts_recovered,
            "contacts_without_history": contacts_without_history,
            "events_recovered": persisted,
            "last_run_id": run_id,
            "last_run_mode": mode,
            "last_error": write_error,
        })

    return {
        "ok": True,
        "run_id": run_id,
        "mode": mode,
        "apply": bool(apply),
        # Always explicit, and always False. There is no HubSpot write path in
        # this module, and the report says so rather than leaving it inferred.
        "hubspot_writes_performed": False,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "resume_from": cursor,
        "next_cursor": last_contact_id,
        "contacts_examined": examined,
        "contacts_with_gaps": len(rows),
        "contacts_without_history": contacts_without_history,
        "contacts_recovered": contacts_recovered,
        "events_recovered": len(recovered_rows),
        "events_persisted": persisted,
        "events_unresolved": len(unresolved_rows),
        "unresolved": unresolved_rows,
        "recovered": recovered_rows,
        "source_system": "hubspot_property_history",
        "lifecycle_rule_version": LIFECYCLE_RULE_VERSION,
    }


def _failed(run_id, mode, started, reason, detail, *, examined=0) -> dict:
    """A run that could not complete. Counts are NULL where nothing was proven."""
    return {
        "ok": False,
        "run_id": run_id,
        "mode": mode,
        "reason": reason,
        "detail": detail,
        "hubspot_writes_performed": False,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        "contacts_examined": examined,
        # Unknown, not zero: an aborted pass proves nothing about how much
        # evidence HubSpot holds.
        "contacts_recovered": None,
        "events_recovered": None,
        "events_persisted": 0,
    }
