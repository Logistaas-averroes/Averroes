"""
services/hubspot_contact_reconciliation_service.py

PR-ADS-163C — contacts deleted or merged away in HubSpot leave the funnel.

Why
---
``hubspot_contact_funnel`` is filled by a modified-since sync. A contact that
is deleted in HubSpot, or merged into another, is never *modified*: it simply
stops being returned, and the sync has no way to notice. Its row stayed and was
counted by every funnel and cohort read. Measured on 2026-11-18: 30 stored
contacts no longer existed in HubSpot; for contacts created in October 2026
the table held 564 and HubSpot 557.

What this does, once a day, after the contact sync
--------------------------------------------------
1. Reads every stored contact id and asks HubSpot which still exist
   (``connectors.hubspot_pull.check_contacts_exist`` — the search endpoint,
   read-only).
2. Records a STRIKE for each id HubSpot did not return, and deletes the strike
   of each id it did return. An id whose batch had no usable answer is left
   exactly as it was: unknown is not missing.
3. Confirms each contact it is about to remove by a DIRECT READ of that
   contact by id (``confirm_contacts_absent``). Only a 404, or a merge
   survivor that names the id, confirms it. A contact the read FINDS exists:
   the search was wrong, its strike is cleared and the run says so.
4. Moves a contact out of the funnel only when the database finds it missing
   in this run AND on a check at least ``min_hours_between_strikes`` earlier,
   its row was not ingested since this run began, it has no open
   post-boundary SQL incident, and the direct read confirmed it.
5. Moves nothing when any batch went unanswered, and refuses to move anything
   when the contacts that qualify for removal — ALL of them, whichever run
   struck them — number more than ``max_removals_per_run`` or more than
   ``max_removal_fraction`` of the table, or when the removals of the last
   ``removal_window_days`` plus this run's would pass the window limits. A
   mass disappearance is far more likely to be our error or a HubSpot
   incident than the truth.

So a contact is removed only when HubSpot's search omitted it on two fully
answered runs at least 20 hours apart AND a direct read by id returned 404.

Removal is a MOVE into ``hubspot_contact_funnel_removed`` (and
``contact_source_classification_removed``), not a flag: some thirty queries
read the funnel table, and a flag would be honoured only by the ones somebody
remembered to change.

Run status
----------
``success``  every batch was answered; strikes and removals are recorded.
``partial``  strikes are recorded, but the run is not clean: a batch had no
             usable answer (then NOTHING is removed by that run), a contact
             was held because it has an open post-boundary SQL incident, the
             direct read found a contact the search had omitted or could not
             confirm one, or more contacts were struck for the first time than
             one run may remove.
``refused``  the safety valve stopped the removal: the whole backlog of
             contacts that qualify for removal, whichever run struck them, is
             over the limit. Strikes are recorded; nothing was removed.
``failed``   nothing could be learned or recorded (no run record, no readable
             contact ids, no batch answered, or a write failed).

Not covered here: the legacy ``leads`` table, lifecycle history / recovery
tables, post-boundary incidents and the coverage boundary, deals and
companies. See ``docs/09_REPO_STATE.md`` (PR-ADS-163C).

Read-only with respect to HubSpot. NEVER writes to HubSpot or Google Ads.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from db import crm_funnel_repository as funnel_repo
from db import writers as db_writers

log = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_REFUSED = "refused"

#: Fallbacks when config/thresholds.yaml cannot be read — the same numbers as
#: the config, so a missing file does not loosen the gate.
DEFAULT_MAX_REMOVALS_PER_RUN = 50
DEFAULT_MAX_REMOVAL_FRACTION = 0.01
DEFAULT_MIN_HOURS_BETWEEN_STRIKES = 20.0
DEFAULT_REMOVAL_WINDOW_DAYS = 7.0
DEFAULT_MAX_REMOVALS_PER_WINDOW = 100
DEFAULT_MAX_REMOVAL_WINDOW_FRACTION = 0.02

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"


def load_config() -> dict:
    """``contact_reconciliation`` from config/thresholds.yaml.

    A value that is missing, not a number or out of range falls back to its
    default: a typo must not widen the safety valve.
    """
    raw: dict = {}
    try:
        import yaml  # noqa: PLC0415

        with _CONFIG_PATH.open(encoding="utf-8") as fh:
            raw = (yaml.safe_load(fh) or {}).get("contact_reconciliation") or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("contact_reconciliation config unavailable, using defaults: %s",
                  exc)

    def _number(key, default, *, low, high, cast):
        value = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        if not low <= value <= high:
            return default
        return cast(value)

    return {
        "max_removals_per_run": _number(
            "max_removals_per_run", DEFAULT_MAX_REMOVALS_PER_RUN,
            low=0, high=10_000, cast=int),
        "max_removal_fraction": _number(
            "max_removal_fraction", DEFAULT_MAX_REMOVAL_FRACTION,
            low=0, high=1, cast=float),
        "min_hours_between_strikes": _number(
            "min_hours_between_strikes", DEFAULT_MIN_HOURS_BETWEEN_STRIKES,
            low=1, high=24 * 30, cast=float),
        "removal_window_days": _number(
            "removal_window_days", DEFAULT_REMOVAL_WINDOW_DAYS,
            low=1, high=365, cast=float),
        "max_removals_per_window": _number(
            "max_removals_per_window", DEFAULT_MAX_REMOVALS_PER_WINDOW,
            low=0, high=100_000, cast=int),
        "max_removal_window_fraction": _number(
            "max_removal_window_fraction", DEFAULT_MAX_REMOVAL_WINDOW_FRACTION,
            low=0, high=1, cast=float),
    }


def _result(status: str, reconciliation_run_id, config: dict, **fields) -> dict:
    base = {
        "status": status,
        "reconciliation_run_id": reconciliation_run_id or None,
        # Unknown is not zero: every count stays None until it is measured.
        "contacts_stored": None,
        "contacts_checked": None,
        "contacts_unknown": None,
        "batches": None,
        "batches_unknown": None,
        "search_calls": None,
        "missing": None,
        "newly_missing": None,
        "returned": None,
        "would_remove": None,
        "backlog_qualified": None,
        "held_open_incident": None,
        "direct_reads": None,
        "confirmed_absent": None,
        "search_disagreements": None,
        "unconfirmed": None,
        "removed": None,
        "classification_rows_removed": None,
        "refusal_reason": None,
        "error": None,
        "max_removals_per_run": config.get("max_removals_per_run"),
        "max_removal_fraction": config.get("max_removal_fraction"),
        "min_hours_between_strikes": config.get("min_hours_between_strikes"),
        "hubspot_writes_performed": False,
    }
    base.update(fields)
    return base


def _finish(result: dict) -> dict:
    """Write the run's outcome down. A run whose outcome could not be recorded
    is not reported as the outcome it failed to record."""
    run_id = result.get("reconciliation_run_id")
    if not run_id:
        return result
    recorded_error = "; ".join(
        str(part) for part in (result.get("refusal_reason"), result.get("error"))
        if part) or None
    ok = db_writers.finish_contact_reconciliation_run(
        run_id, status=result["status"], error=recorded_error,
        **{k: result.get(k) for k in (
            "contacts_stored", "contacts_checked", "batches",
            "batches_unknown", "missing", "newly_missing", "returned",
            "would_remove", "removed", "classification_rows_removed",
            "backlog_qualified", "held_open_incident",
            "search_disagreements")})
    if not ok:
        note = "the run's outcome could not be recorded"
        log.error("[contact_reconciliation] %s (run %s, status %s)",
                  note, run_id, result["status"])
        result = {**result, "status": STATUS_FAILED,
                  "run_record_write_failed": True,
                  "error": (f"{note}; it ended {result['status']!r}"
                            + (f": {result['error']}" if result.get("error")
                               else ""))}
    return result


def run_contact_reconciliation(*, run_id=None, now: datetime | None = None,
                               client=None, existence_check=None,
                               absence_confirmation=None) -> dict:
    """Check every stored contact against HubSpot and move out the ones it no
    longer has. Never raises.

    ``run_id``           the scheduler's ``runs`` row, when called from one.
    ``now``              the instant of the check. Left None (production) the
                         database stamps each strike with ``clock_timestamp()``
                         after the HubSpot read. Injectable so the two-strike
                         interval is testable without waiting 20 hours.
    ``client``           a HubSpot client for the connector (tests).
    ``existence_check``  replaces the connector's search check (tests).
    ``absence_confirmation``  replaces the connector's direct read (tests).
    """
    config = load_config()
    reconciliation_run_id = db_writers.start_contact_reconciliation_run(
        run_id=run_id)
    if not reconciliation_run_id:
        # Fail closed BEFORE touching HubSpot: a strike must name the run that
        # found it, and a run that cannot be recorded cannot be named.
        message = "durable reconciliation run record could not be created"
        log.warning("[contact_reconciliation] %s — refusing to check", message)
        return _result(STATUS_FAILED, None, config, error=message)

    try:
        return _finish(_reconcile(reconciliation_run_id, config, now=now,
                                  client=client,
                                  existence_check=existence_check,
                                  absence_confirmation=absence_confirmation))
    except Exception as exc:  # noqa: BLE001
        log.warning("[contact_reconciliation] failed: %s", exc)
        return _finish(_result(STATUS_FAILED, reconciliation_run_id, config,
                               error=str(exc)[:500]))


def _confirm_candidates(reconciliation_run_id, config: dict, *, client,
                        absence_confirmation) -> dict:
    """The independent second read: which of this run's removal candidates
    does a DIRECT read of the object confirm are gone?

    ``{ok, error, candidates, confirmed, existing, unknown, direct_reads,
    errors}``. More candidates than one run may remove are not read at all:
    the valve will refuse that run whatever HubSpot says, and reading them
    would spend up to a call per contact on a removal that cannot happen.
    """
    out = {"ok": True, "error": None, "candidates": 0, "confirmed": [],
           "existing": [], "unknown": [], "direct_reads": 0, "errors": []}
    read = db_writers.fetch_removal_candidates(
        reconciliation_run_id,
        min_hours_between_strikes=config["min_hours_between_strikes"])
    if not read.get("ok"):
        return {**out, "ok": False,
                "error": ("the removal candidates could not be read: "
                          f"{read.get('error')}")}
    candidates = list(read.get("contact_ids") or [])
    out["candidates"] = len(candidates)
    if not candidates or len(candidates) > config["max_removals_per_run"]:
        return out
    if absence_confirmation is None:
        from connectors.hubspot_pull import (  # noqa: PLC0415
            confirm_contacts_absent,
        )
        absence_confirmation = confirm_contacts_absent
    answer = absence_confirmation(candidates, client=client)
    confirmed = list(answer.get("absent") or []) \
        + list(answer.get("merged_away") or [])
    existing = list(answer.get("existing") or [])
    unknown = list(answer.get("unknown") or [])
    asked = set(candidates)
    answered = confirmed + existing + unknown
    if len(answered) != len(asked) or set(answered) != asked:
        # The confirmation did not account for exactly the contacts it was
        # asked about. Nothing it says can authorise a removal.
        return {**out, "ok": False,
                "error": (f"the direct read accounted for {len(set(answered) & asked)} "
                          f"of {len(asked)} removal candidates")}
    return {**out, "confirmed": confirmed, "existing": existing,
            "unknown": unknown,
            "direct_reads": answer.get("direct_reads"),
            "errors": list(answer.get("errors") or [])}


def _reconcile(reconciliation_run_id, config: dict, *, now, client,
               existence_check, absence_confirmation) -> dict:
    stored = funnel_repo.fetch_stored_contact_ids()
    if not stored.get("available"):
        return _result(STATUS_FAILED, reconciliation_run_id, config,
                       error="the stored contact ids could not be read")
    contact_ids = list(stored.get("contact_ids") or [])
    facts = {"contacts_stored": len(contact_ids)}
    if not contact_ids:
        # An empty store has nothing to check; that is a complete answer.
        return _result(STATUS_SUCCESS, reconciliation_run_id, config, **facts,
                       contacts_checked=0, contacts_unknown=0, batches=0,
                       batches_unknown=0, search_calls=0, missing=0,
                       newly_missing=0, returned=0, would_remove=0, removed=0,
                       classification_rows_removed=0)

    if existence_check is None:
        from connectors.hubspot_pull import (  # noqa: PLC0415
            check_contacts_exist,
        )
        existence_check = check_contacts_exist
    check = existence_check(contact_ids, client=client)

    existing = list(check.get("existing") or [])
    missing = list(check.get("missing") or [])
    unknown = list(check.get("unknown") or [])
    batches_unknown = int(check.get("batches_unknown") or 0)
    facts.update(
        contacts_checked=len(existing) + len(missing),
        contacts_unknown=len(unknown),
        batches=check.get("batches"),
        batches_unknown=batches_unknown,
        search_calls=check.get("search_calls"),
    )
    accounted = len(existing) + len(missing) + len(unknown)
    if accounted != len(set(contact_ids)):
        # The check did not account for every id it was given. Its lists
        # cannot be trusted to say which contacts are missing.
        return _result(
            STATUS_FAILED, reconciliation_run_id, config, **facts,
            error=(f"the existence check accounted for {accounted} of "
                   f"{len(set(contact_ids))} contact ids"))
    check_errors = "; ".join(str(e) for e in (check.get("errors") or []))[:500]

    if not existing and not missing:
        # Not one batch was answered: nothing was learned about any contact.
        return _result(
            STATUS_FAILED, reconciliation_run_id, config, **facts,
            error=("no batch could be answered by HubSpot"
                   + (f": {check_errors}" if check_errors else "")))

    strikes = db_writers.record_contact_absence(
        reconciliation_run_id, missing_ids=missing, returned_ids=existing,
        observed_at=now)
    if not strikes.get("ok"):
        return _result(
            STATUS_FAILED, reconciliation_run_id, config, **facts,
            error=f"strikes could not be recorded: {strikes.get('error')}")
    facts.update(missing=strikes["missing"],
                 newly_missing=strikes["newly_missing"],
                 returned=strikes["returned"])

    every_batch_answered = not batches_unknown and not unknown

    # Before anything is removed: a second, independent read. The search has
    # now omitted each candidate twice — and a search index can omit a live
    # contact twice. Only a direct read of the object answering 404 (or
    # proving a merge) lets a contact go.
    confirmation = {"candidates": 0, "confirmed": [], "existing": [],
                    "unknown": [], "direct_reads": 0, "errors": []}
    if every_batch_answered:
        confirmation = _confirm_candidates(
            reconciliation_run_id, config, client=client,
            absence_confirmation=absence_confirmation)
        if not confirmation.get("ok"):
            return _result(STATUS_FAILED, reconciliation_run_id, config,
                           **facts, error=confirmation.get("error"))
    facts.update(direct_reads=confirmation["direct_reads"],
                 unconfirmed=len(confirmation["unknown"]))

    move = db_writers.move_absent_contacts(
        reconciliation_run_id,
        min_hours_between_strikes=config["min_hours_between_strikes"],
        max_removals=config["max_removals_per_run"],
        max_fraction=config["max_removal_fraction"],
        # A run that could not check every contact removes NOTHING: it cannot
        # know how large the disappearance is, and removing the answered share
        # is how a mass false removal passes the limit in slices.
        every_batch_answered=every_batch_answered,
        confirmed_absent_ids=confirmation["confirmed"],
        seen_existing_ids=confirmation["existing"],
        window_days=config["removal_window_days"],
        max_window_removals=config["max_removals_per_window"],
        max_window_fraction=config["max_removal_window_fraction"])
    if not move.get("ok"):
        return _result(
            STATUS_FAILED, reconciliation_run_id, config, **facts,
            error=f"the removal could not be completed: {move.get('error')}")
    facts.update(
        would_remove=move["would_remove"], removed=move["removed"],
        backlog_qualified=move["backlog_qualified"],
        held_open_incident=move["held_open_incident"],
        confirmed_absent=move["confirmed_absent"],
        search_disagreements=move["search_disagreements"],
        classification_rows_removed=move["classification_rows_removed"])

    # Everything that keeps this run from being a clean success, in words.
    notes = []
    if not every_batch_answered:
        notes.append(
            f"{batches_unknown} batch(es) ({len(unknown)} contact(s)) had no "
            "usable answer from HubSpot and were left unchanged; a run that "
            "did not check every contact removes nothing"
            + (f" ({move['would_remove']} would have been removed)"
               if move["would_remove"] else "")
            + (f": {check_errors}" if check_errors else ""))
    if move["held_open_incident"]:
        notes.append(
            f"{move['held_open_incident']} contact(s) HubSpot no longer "
            "returns were NOT removed because each has an open post-boundary "
            "SQL incident; they stay counted until the incident is resolved")
    if move["search_disagreements"]:
        notes.append(
            f"HubSpot's search did not return {move['search_disagreements']} "
            "contact(s) that a direct read by id then FOUND: the search is "
            "omitting live contacts. They were not removed and their strikes "
            "were cleared")
    if confirmation["unknown"]:
        direct_errors = "; ".join(confirmation["errors"])[:200]
        notes.append(
            f"{len(confirmation['unknown'])} contact(s) could not be "
            "confirmed absent by a direct read and were not removed"
            + (f": {direct_errors}" if direct_errors else ""))
    mass = _mass_first_strike(strikes, config)
    if mass:
        notes.append(mass)
    note = "; ".join(notes)[:900] or None

    if move.get("refused"):
        log.error("[contact_reconciliation] REFUSED: %s",
                  move.get("refusal_reason"))
        return _result(STATUS_REFUSED, reconciliation_run_id, config, **facts,
                       refusal_reason=move.get("refusal_reason"), error=note)
    if note:
        return _result(STATUS_PARTIAL, reconciliation_run_id, config, **facts,
                       error=note)

    log.info(
        "[contact_reconciliation] checked=%s missing=%s newly_missing=%s "
        "returned=%s removed=%s", facts["contacts_checked"], facts["missing"],
        facts["newly_missing"], facts["returned"], facts["removed"])
    return _result(STATUS_SUCCESS, reconciliation_run_id, config, **facts)


def _mass_first_strike(strikes: dict, config: dict) -> str | None:
    """A first strike on more contacts than one run may remove is not a clean
    run. Nothing is removed on a first strike, but the same limits apply to
    the warning: it must arrive on day one, not as the refusal on day two."""
    newly = strikes.get("newly_missing") or 0
    table_rows = strikes.get("table_rows") or 0
    limit = config["max_removals_per_run"]
    fraction = config["max_removal_fraction"]
    if newly > limit or newly > table_rows * fraction:
        return (f"{newly} stored contacts were not returned by HubSpot for "
                f"the first time in one run (limits: {limit}, or "
                f"{fraction:.2%} of {table_rows} stored contacts); nothing is "
                "removed on a first strike, but this many at once needs a "
                "person to look")
    return None
