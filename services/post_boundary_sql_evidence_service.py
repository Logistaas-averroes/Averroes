"""
services/post_boundary_sql_evidence_service.py

PR-ADS-161C — post-boundary SQL evidence forensics, and the one safe repair.

Production reported 113 open post-boundary incidents, every one of them
``post_boundary_history_has_no_sql_transition``, and the coverage gate called
all of them "our gaps". This module establishes, per incident, where the exact
SQL-entry evidence actually disappeared — and repairs only what HubSpot proves.

Two operations, one owner each
------------------------------
``audit()``  READ-ONLY. Classifies every open incident from the local store
             (no HubSpot call) or, when explicitly asked, against a fresh
             READ-ONLY HubSpot comparison read. Writes nothing anywhere: its
             one database read runs in a READ ONLY transaction, so that is a
             property of the connection rather than of the code after it.
``repair()`` DRY RUN BY DEFAULT. Re-reads HubSpot (read-only) for open
             incidents and, only with ``apply``, persists exact evidence from
             the two permitted sources and closes what that evidence proves —
             all in ONE local transaction that rolls back completely on
             failure.

Neither creates a second incident detector. The scheduled detector remains
``services.sql_coverage_boundary_service.detect_post_boundary_gaps``; the repair
reuses its very HubSpot read and selection (``_consult_history``), so the repair
and the scheduled pass cannot disagree about what counts as evidence.

What is never produced
----------------------
An SQL-entry date from anything but HubSpot's direct
``hs_v2_date_entered_salesqualifiedlead`` property or a genuine
``salesqualifiedlead`` transition in its lifecycle history. See
``analysis.post_boundary_sql_forensics.FORBIDDEN_SQL_DATE_SUBSTITUTES``.
A contact with neither keeps a NULL date, an OPEN incident, and lifecycle-event
publication withheld wherever it could belong — while the acquisition cohort,
which never asks for an SQL-entry date, is governed by its own gates.

No HubSpot write. No Google Ads call. Exit codes follow the repository's audit
convention: 0 holds · 1 violation · 2 unavailable.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from analysis import post_boundary_sql_forensics as fx

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

MODE_LOCAL = "local_store"
MODE_COMPARE = "hubspot_comparison"
MODE_DRY_RUN = "dry_run"
MODE_APPLY = "apply"

# ── Audit verdicts · denominator: one audit invocation ──────────────────────
V_NO_CODE_LOSS = "no_code_owned_loss_detected"
V_CODE_LOSS = "code_owned_loss_detected"
V_INTEGRITY = "incident_evidence_contradiction"
V_UNAVAILABLE = "audit_unavailable"
AUDIT_VERDICTS = (V_NO_CODE_LOSS, V_CODE_LOSS, V_INTEGRITY, V_UNAVAILABLE)

# ── Repair run statuses · denominator: one repair invocation ────────────────
# The scheduler's three outcomes, with the same meaning: `partial` is never
# folded into either neighbour.
R_COMPLETE = "success"
R_PARTIAL = "partial"
R_FAILED = "failed"
R_UNAVAILABLE = "unavailable"
REPAIR_STATUSES = (R_COMPLETE, R_PARTIAL, R_FAILED, R_UNAVAILABLE)

LIFECYCLE_GATE_COMMAND = "python -m scripts.audit_sql_coverage_gate"
COHORT_AUDIT_COMMAND = "python -m scripts.audit_marketing_outcome_cohorts"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _source_freshness() -> dict:
    """The contact-funnel freshness verdict, from the ONE shared contract."""
    from analysis import sql_coverage_freshness as freshness  # noqa: PLC0415
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    try:
        return freshness.assess(repo.fetch_contact_funnel_sync_state())
    except Exception as exc:  # noqa: BLE001
        log.error("[post_boundary_audit] freshness unreadable: %s", exc)
        return {"fresh": None, "reason": "source_sync_state_unavailable",
                "detail": "the contact-funnel sync state could not be read"}


def _local_view(row: dict) -> dict:
    """What the local store holds for one incident. Pure projection."""
    return {
        "reason": row.get("reason"),
        "history_state": row.get("history_state"),
        "history_checked": row.get("history_checked"),
        "direct_property_state": row.get("direct_property_state"),
        "stage_jump_skipped_sql": row.get("stage_jump_skipped_sql"),
        "direct_sql_entry_at": row.get("direct_sql_entry_at"),
        "recovered_sql_entry_at": row.get("recovered_sql_entry_at"),
        "current_lifecycle_stage": row.get("current_lifecycle_stage"),
        "funnel_row_present": bool(row.get("funnel_row_present")),
        "last_modified_at": row.get("last_modified_at"),
        "last_ingested_at": row.get("last_ingested_at"),
    }


def _incident_record(row: dict, verdict: dict, comparison: dict | None) -> dict:
    """One open incident, as the audit publishes it.

    Contact identity is HubSpot's opaque contact id — no email, name or company
    exists in either table this reads, and none is added. Every timestamp is
    labelled for what it is; none is an SQL-entry date unless its key says so
    and it came from one of the two permitted sources.
    """
    return {
        "contact_id": row.get("contact_id"),
        "contact_created_at": _iso(row.get("contact_created_at")),
        "current_lifecycle_stage": row.get("current_lifecycle_stage"),
        "lifecycle_stage_at_detection": row.get("lifecycle_stage_at_detection"),
        # The two PERMITTED sources, read separately. NULL = not held locally.
        "direct_sql_entry_at": _iso(row.get("direct_sql_entry_at")),
        "recovered_sql_entry_at": _iso(row.get("recovered_sql_entry_at")),
        # BOUNDS, never the event.
        "last_known_below_sql_at": _iso(row.get("last_known_below_sql_at")),
        "first_observed_at_or_above_sql": _iso(
            row.get("first_observed_at_or_above_sql")),
        "observation_bounds_basis": row.get("observation_bounds_basis"),
        "history_stage_path": row.get("history_stage_path"),
        "history_versions_seen": row.get("history_versions_seen"),
        # When WE first saw, and last re-checked, the gap — observation times
        # of the incident, not of the transition.
        "first_incident_at": _iso(row.get("detected_at")),
        "latest_incident_at": _iso(row.get("last_checked_at")
                                   or row.get("updated_at")),
        "source_run_id": row.get("detected_by_run_id"),
        "sync_batch_id": row.get("sync_batch_id"),
        "reason": row.get("reason"),
        "history_state": row.get("history_state"),
        "classification": verdict["classification"],
        "owner": verdict["owner"],
        "facts": verdict["facts"],
        "classification_basis": verdict["basis"],
        "comparison": comparison,
    }


def _compare(rows: list, *, client) -> tuple[dict, int, int]:
    """READ-ONLY HubSpot comparison for ``rows``, chunked to the batch limit.

    Returns ``(by_contact, calls_made, calls_failed)``. A failed chunk marks
    ONLY its own contacts ``request_failed`` — never "no evidence".
    """
    from connectors import hubspot_pull  # noqa: PLC0415

    ids = [r["contact_id"] for r in rows if r.get("contact_id")]
    out: dict = {}
    calls = failed = 0
    limit = hubspot_pull.HUBSPOT_HISTORY_BATCH_LIMIT
    for start in range(0, len(ids), limit):
        chunk = ids[start:start + limit]
        calls += 1
        try:
            answer = hubspot_pull.compare_sql_entry_evidence(chunk, client=client)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.error("[post_boundary_audit] comparison read failed for %d "
                      "contact(s): %s", len(chunk), type(exc).__name__)
            for cid in chunk:
                out[cid] = {"request_failed": True}
            continue
        for cid in chunk:
            entry = answer.get(cid) or {}
            direct = entry.get("direct_sql_entry") or {}
            shape = fx.history_shape(entry.get("versions") or [])
            out[cid] = {
                "request_failed": False,
                "returned": bool(entry.get("returned")),
                "lifecycle_stage": entry.get("lifecycle_stage"),
                "last_modified_at": entry.get("last_modified_at"),
                "direct_state": direct.get("state"),
                "direct_sql_entry_at": direct.get("value"),
                "direct_sql_entry_set_at": entry.get("direct_sql_entry_set_at"),
                "history_state": entry.get("history_state"),
                "history_shape": shape,
            }
    return out, calls, failed


def _publishable_comparison(source: dict | None) -> dict | None:
    if source is None:
        return None
    shape = source.get("history_shape") or {}
    return {
        "request_failed": bool(source.get("request_failed")),
        "returned": source.get("returned"),
        "hubspot_lifecycle_stage": source.get("lifecycle_stage"),
        "hubspot_last_modified_at": _iso(source.get("last_modified_at")),
        "hubspot_direct_property_state": source.get("direct_state"),
        "hubspot_direct_sql_entry_at": _iso(source.get("direct_sql_entry_at")),
        "hubspot_direct_property_set_at": _iso(
            source.get("direct_sql_entry_set_at")),
        "hubspot_history_state": source.get("history_state"),
        "hubspot_history_stage_path": shape.get("stage_path"),
        "hubspot_history_has_sql_version": shape.get("has_sql_version"),
        "hubspot_stage_jump_skipped_sql": shape.get("stage_jump_skipped_sql"),
    }


def audit(*, compare_hubspot: bool = False, sample: int | None = None,
          client=None) -> dict:
    """Classify every open post-boundary incident. READ-ONLY, always.

    ``compare_hubspot`` performs READ-ONLY HubSpot calls over the first
    ``sample`` open incidents (all, when ``sample`` is None). Without it, no
    HubSpot call is made. ``sample`` also bounds how many incidents are LISTED;
    every count and breakdown still covers what it says it covers, and the
    comparison breakdown states its own denominator.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    started = _utcnow()
    report: dict = {
        "audit": "post_boundary_sql_incidents",
        "mode": MODE_COMPARE if compare_hubspot else MODE_LOCAL,
        "started_at": started.isoformat(),
        "audit_complete": False,
        "source_fresh": None,
        "source_freshness": None,
        "boundary_id": None,
        "boundary_observed_at": None,
        "open_incidents": None,
        "resolved_incidents": None,
        "contacts_examined": None,
        "hubspot_calls_performed": 0,
        "hubspot_calls_failed": 0,
        # Structural, and stated on every path. This audit has no write path.
        "external_writes_performed": 0,
        "database_writes_performed": 0,
        "forbidden_sql_date_substitutes": list(fx.FORBIDDEN_SQL_DATE_SUBSTITUTES),
    }

    freshness = _source_freshness()
    report["source_freshness"] = freshness
    report["source_fresh"] = freshness.get("fresh")

    boundary_state = repo.fetch_active_sql_coverage_boundary()
    if not boundary_state.get("available"):
        return _audit_unavailable(report, "the boundary store could not be read")
    boundary = boundary_state.get("boundary") or {}
    report["boundary_id"] = boundary.get("boundary_id")
    report["boundary_observed_at"] = _iso(boundary.get("observed_at"))

    forensics = repo.fetch_post_boundary_incident_forensics()
    if not forensics.get("available"):
        return _audit_unavailable(report,
                                  "the post-boundary incident store could not "
                                  "be read; the incident count is unknown, "
                                  "not zero")
    rows = forensics.get("rows") or []
    open_rows = [r for r in rows if (r.get("status") or "") == "open"]
    resolved_rows = [r for r in rows if (r.get("status") or "") == "resolved"]
    report["open_incidents"] = len(open_rows)
    report["resolved_incidents"] = len(resolved_rows)
    report["contacts_examined"] = len(rows)
    detected = [r.get("detected_at") for r in open_rows if r.get("detected_at")]
    report["oldest_open_incident_at"] = _iso(min(detected)) if detected else None
    report["newest_open_incident_at"] = _iso(max(detected)) if detected else None

    local = {r["contact_id"]: fx.classify_local(_local_view(r))
             for r in open_rows}
    report["root_cause_local"] = fx.summarize(list(local.values()))
    report["root_cause_local"]["denominator"] = (
        f"all {len(open_rows)} open incident(s), classified from the local "
        f"store; upstream-present causes need --compare-hubspot")
    report["incident_reasons"] = _count(r.get("reason") for r in open_rows)

    # A RESOLVED incident must still be backed by stored evidence. One that is
    # not was closed by something other than an exact timestamp — or its
    # evidence was erased afterwards — and either is a contradiction.
    unbacked = [r.get("contact_id") for r in resolved_rows
                if r.get("direct_sql_entry_at") is None
                and r.get("recovered_sql_entry_at") is None]
    report["integrity"] = {
        "resolved_without_stored_evidence": len(unbacked),
        "resolved_without_stored_evidence_ids": unbacked[:25],
        "resolved_by": _count(r.get("resolved_by") for r in resolved_rows),
    }

    listed = open_rows if sample is None else open_rows[:max(0, int(sample))]
    comparison: dict = {}
    compared: dict = {}
    if compare_hubspot:
        if client is None:
            from connectors import hubspot_pull  # noqa: PLC0415
            try:
                client = hubspot_pull.get_client()
            except Exception as exc:  # noqa: BLE001
                return _audit_unavailable(
                    report, f"HubSpot is not readable from here "
                            f"({type(exc).__name__}); the comparison could "
                            f"not be made")
        comparison, calls, failed = _compare(listed, client=client)
        report["hubspot_calls_performed"] = calls
        report["hubspot_calls_failed"] = failed
        if listed and failed == calls:
            return _audit_unavailable(report,
                                      "every HubSpot comparison read failed")
        compared = {cid: fx.classify_with_source(
                        _local_view(next(r for r in listed
                                         if r["contact_id"] == cid)), src)
                    for cid, src in comparison.items()}
        report["root_cause_hubspot_comparison"] = fx.summarize(
            list(compared.values()))
        report["root_cause_hubspot_comparison"]["denominator"] = (
            f"{len(compared)} of {len(open_rows)} open incident(s), compared "
            f"against a fresh read-only HubSpot read; NOT extrapolated to the "
            f"rest")
        report["compared_incidents"] = len(compared)

    incidents = []
    for row in listed:
        cid = row["contact_id"]
        src = comparison.get(cid)
        verdict = (fx.classify_with_source(_local_view(row), src)
                   if src is not None else local[cid])
        incidents.append(_incident_record(row, verdict,
                                          _publishable_comparison(src)))
    report["incidents"] = incidents
    report["incidents_listed"] = len(incidents)

    open_known = len(open_rows)
    report["lifecycle_event_publication"] = {
        "status": "withheld" if open_known else "not_assessed_by_this_audit",
        "blocked_by_open_post_boundary_incidents": open_known > 0,
        "detail": ("an open post-boundary incident leaves lifecycle-event SQL "
                   "membership unresolved for every window it could belong to"
                   if open_known else
                   "no open post-boundary incident; certification still "
                   "depends on the coverage gate's other checks"),
        "authoritative_check": LIFECYCLE_GATE_COMMAND,
    }
    report["acquisition_cohort_publication"] = _cohort_publication(freshness)

    # The verdict is taken over EVERY open incident, each on the best basis
    # available for it: the comparison where one was made, the local store
    # otherwise. A comparison sample never hides a local finding outside it.
    final = [compared.get(cid) if compare_hubspot and cid in compared
             else local[cid] for cid in local] if compare_hubspot else list(
        local.values())
    report["root_cause"] = fx.summarize(final)
    report["root_cause"]["denominator"] = (
        f"all {len(open_rows)} open incident(s); "
        f"{report.get('compared_incidents', 0)} on a HubSpot comparison, the "
        f"rest on the local store")
    code_losses = report["root_cause"]["code_owned_losses"]
    if unbacked:
        verdict, code = V_INTEGRITY, EXIT_VIOLATION
    elif code_losses:
        verdict, code = V_CODE_LOSS, EXIT_VIOLATION
    else:
        verdict, code = V_NO_CODE_LOSS, EXIT_OK
    report.update({"audit_complete": True, "verdict": verdict,
                   "exit_code": code, "finished_at": _utcnow().isoformat(),
                   "note": (
                       "exit 0 means no open incident was traced to a "
                       "code-owned loss — NOT that there are no incidents. "
                       "Open incidents keep lifecycle-event publication "
                       "withheld whatever this audit's exit code")})
    return report


def _cohort_publication(freshness: dict) -> dict:
    """The acquisition cohort's own freshness gate — independent of incidents.

    The cohort counts contacts CREATED in a window; it never asks for an
    SQL-entry date, so an open lifecycle-event incident is not one of its
    inputs. Reported here so an operator reading this audit cannot mistake a
    withheld lifecycle-event total for a withheld Campaign Evidence cohort.
    """
    from services.marketing_outcome_cohort_service import (  # noqa: PLC0415
        PUBLISHABLE_FRESHNESS_REASONS,
    )

    reason = (freshness or {}).get("reason")
    return {
        "freshness_gate": ("passes" if reason in PUBLISHABLE_FRESHNESS_REASONS
                           else "withholds"),
        "freshness_reason": reason,
        "reads_post_boundary_incidents": False,
        "detail": ("governed by its own freshness, reconciliation and mapping "
                   "gates; open post-boundary incidents do not withhold it"),
        "authoritative_check": COHORT_AUDIT_COMMAND,
    }


def _audit_unavailable(report: dict, detail: str) -> dict:
    report.update({"audit_complete": False, "verdict": V_UNAVAILABLE,
                   "exit_code": EXIT_UNAVAILABLE, "detail": detail,
                   "finished_at": _utcnow().isoformat()})
    return report


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


# ═════════════════════════════════════════════════════════════════════════════
# Repair — dry run by default
# ═════════════════════════════════════════════════════════════════════════════

def repair(*, apply: bool = False, client=None, limit: int | None = None,
           run_id: str | None = None) -> dict:
    """Recover exact SQL evidence for OPEN incidents, from HubSpot only.

    Dry run unless ``apply``. HubSpot is READ, never written; with ``apply`` the
    only writes are LOCAL — the direct column where it is NULL, recovered
    lifecycle-history rows, and the closure of incidents that evidence proves —
    all in ONE transaction (``writers.apply_post_boundary_sql_evidence``).

    A contact with no exact source evidence is reported unresolved and keeps a
    NULL date and an open incident. Zero recoverable is a truthful outcome.
    """
    from connectors import hubspot_pull  # noqa: PLC0415
    from db import crm_funnel_repository as repo  # noqa: PLC0415
    from services import sql_coverage_boundary_service as boundary_svc  # noqa: PLC0415

    rid = run_id or f"sqlrepair_{uuid.uuid4().hex[:12]}"
    started = _utcnow()
    report: dict = {
        "run_id": rid,
        "mode": MODE_APPLY if apply else MODE_DRY_RUN,
        "apply": bool(apply),
        "started_at": started.isoformat(),
        "hubspot_writes_performed": False,
        "google_ads_calls_performed": False,
        "examined": None,
        "recoverable_from_stored_evidence": None,
        "recoverable_direct_property": None,
        "recoverable_lifecycle_history": None,
        "unresolved": None,
        "written": 0,
        "unchanged": 0,
        "incidents_resolved": 0,
        "database_writes_performed": 0,
    }

    boundary_state = repo.fetch_active_sql_coverage_boundary()
    if not boundary_state.get("available"):
        return _repair_end(report, R_UNAVAILABLE, EXIT_UNAVAILABLE,
                           "the boundary store could not be read")
    boundary = boundary_state.get("boundary")
    if not boundary:
        return _repair_end(report, R_COMPLETE, EXIT_OK,
                           "no coverage boundary is established, so no "
                           "post-boundary incident can exist", examined=0)

    forensics = repo.fetch_post_boundary_incident_forensics()
    if not forensics.get("available"):
        return _repair_end(report, R_UNAVAILABLE, EXIT_UNAVAILABLE,
                           "the post-boundary incident store could not be read")
    open_rows = [r for r in (forensics.get("rows") or [])
                 if (r.get("status") or "") == "open"]
    report["open_before"] = len(open_rows)
    examined = open_rows if limit is None else open_rows[:max(0, int(limit))]
    report["examined"] = len(examined)

    stored = [r for r in examined if r.get("direct_sql_entry_at") is not None
              or r.get("recovered_sql_entry_at") is not None]
    stored_ids = {r["contact_id"] for r in stored}
    need = [r for r in examined if r["contact_id"] not in stored_ids]
    report["recoverable_from_stored_evidence"] = len(stored)

    direct_rows: list = []
    recovered: list = []
    unresolved: list = []
    requests = 0
    if need:
        if client is None:
            try:
                client = hubspot_pull.get_client()
            except Exception as exc:  # noqa: BLE001
                return _repair_end(report, R_UNAVAILABLE, EXIT_UNAVAILABLE,
                                   f"HubSpot is not readable from here "
                                   f"({type(exc).__name__})")
        # The stage AT DETECTION is the stage that proved SQL was reached when
        # the incident opened. A contact that has since fallen below SQL still
        # owes that transition a date; its current stage would make the
        # recovery skip it, and that would be a claim about HubSpot drawn from
        # our own filter.
        consult_rows = [{
            "contact_id": r["contact_id"],
            "created_at": r.get("contact_created_at"),
            "lifecycle_stage": (r.get("lifecycle_stage_at_detection")
                                or r.get("current_lifecycle_stage")),
        } for r in need]
        recovered, unresolved, requests = boundary_svc._consult_history(
            consult_rows, boundary_id=boundary.get("boundary_id"),
            client=client, budget=len(consult_rows), direct_out=direct_rows)

    unread = [u for u in unresolved
              if u.get("reason") == boundary_svc.INCIDENT_HISTORY_UNREADABLE]
    report.update({
        "hubspot_requests": requests,
        "recoverable_direct_property": len(direct_rows),
        "recoverable_lifecycle_history": len(recovered),
        "unresolved": len(unresolved),
        "unresolved_reasons": _count(u.get("reason") for u in unresolved),
        "contacts_unread": len(unread),
        "provenance": {
            "direct_property": "hubspot_contact_funnel.date_entered_sql "
                               "(HubSpot hs_v2_date_entered_salesqualifiedlead)",
            "lifecycle_history": "hubspot_lifecycle_stage_history "
                                 "(HubSpot lifecyclestage property history)",
            "run_id": rid,
        },
    })
    status = R_PARTIAL if unread else R_COMPLETE

    if not apply:
        return _repair_end(report, status,
                           EXIT_VIOLATION if unread else EXIT_OK,
                           "dry run — nothing was written, anywhere")

    from db import writers  # noqa: PLC0415

    res = writers.apply_post_boundary_sql_evidence(
        direct_rows, recovered, run_id=rid,
        resolve_contact_ids=[r["contact_id"] for r in examined])
    if not res.get("ok"):
        # One transaction: a failure wrote NOTHING. Said explicitly, because
        # "failed" alone would leave an operator hunting for partial rows.
        return _repair_end(report, R_FAILED, EXIT_VIOLATION,
                           f"the local write failed and was rolled back in "
                           f"full; nothing was written "
                           f"({res.get('error')})")

    written = int(res.get("direct_written") or 0) + int(
        res.get("history_written") or 0)
    report.update({
        "written": written,
        "direct_written": int(res.get("direct_written") or 0),
        "history_written": int(res.get("history_written") or 0),
        "unchanged": int(res.get("direct_unchanged") or 0)
        + int(res.get("history_unchanged") or 0),
        "incidents_resolved": int(res.get("incidents_resolved") or 0),
        "resolved_by": res.get("resolved_by") or {},
        "database_writes_performed": written
        + int(res.get("incidents_resolved") or 0),
    })

    after = repo.fetch_post_boundary_incident_forensics()
    if not after.get("available"):
        report["open_after"] = None
        return _repair_end(report, R_PARTIAL, EXIT_VIOLATION,
                           "the evidence was written, but the incident store "
                           "could not be re-read to verify the result")
    report["open_after"] = sum(1 for r in after.get("rows") or []
                               if (r.get("status") or "") == "open")
    return _repair_end(report, status, EXIT_VIOLATION if unread else EXIT_OK,
                       "applied — every write and resolution landed in one "
                       "transaction" + (
                           f"; {len(unread)} contact(s) could not be read "
                           f"from HubSpot and were left untouched"
                           if unread else ""))


def _repair_end(report: dict, status: str, code: int, detail: str,
                **extra) -> dict:
    report.update(extra)
    report.update({"status": status, "exit_code": code, "detail": detail,
                   "finished_at": _utcnow().isoformat()})
    return report
