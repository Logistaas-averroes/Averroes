"""
Lead Event-Date Reconciliation Service (PR-ADS-115)

Reconciles paid-search lead snapshots that are missing ``contact_created_at``
(the business event date) so the revenue truth layer can become safe — WITHOUT
inventing dates or hiding anything.

Every missing-date paid lead is classified as exactly one of:

  recovered        contact_created_at backfilled from HubSpot's real ``createdate``
  excluded_legacy  no verifiable HubSpot identity / created date — written to
                   lead_truth_exclusions with a durable, visible reason and kept
                   out of revenue-truth metrics (date stays null)
  unresolved       a valid identity whose HubSpot lookup failed transiently —
                   NOT excluded, remains a genuine retryable blocker

Doctrine:
  - Reads HubSpot read-only; writes ONLY to the local DB.
  - NEVER writes to HubSpot or Google Ads.
  - NEVER uses a run/sync/current date as an event date, and NEVER fabricates one.
  - Reuses the durable background-job pattern (checkpoint / resume) from PR-ADS-114.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100

EXCLUSION_NO_IDENTITY = "no_contact_identity"
EXCLUSION_NOT_FOUND = "hubspot_contact_not_found"
EXCLUSION_NO_CREATEDATE = "hubspot_contact_no_createdate"


def _empty_summary() -> dict:
    return {
        "missing_event_dates": 0,
        "recoverable_by_contact_id": 0,
        "recovered_from_createdate": 0,
        "no_usable_contact_identity": 0,
        "hubspot_contact_not_found": 0,
        "hubspot_contact_no_createdate": 0,
        "already_excluded_legacy": 0,
        "remaining_unresolved": 0,
    }


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), max(1, size)):
        yield seq[i:i + size]


def run_lead_reconciliation(
    *,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    job_id: str | None = None,
    progress: dict | None = None,
    checkpoint=None,
    load_completed=None,
    resume: bool = False,
) -> dict:
    """Reconcile missing lead event dates. Read-only to HubSpot; local DB only.

    Args:
        dry_run: when True, classify and report expected impact but write nothing
            (no backfills, no exclusions).
        batch_size: HubSpot contact batch-read size.
        job_id / progress / checkpoint / load_completed / resume: durable
            background-job plumbing (see PR-ADS-114). ``checkpoint(job_id, snap)``
            persists progress after each batch; resume is idempotent because the
            still-missing set is re-read from the DB.

    Returns a result dict with summary, ``revenue_truth_lead_dates_ready``,
    per-batch detail, and errors.
    """
    from connectors.hubspot_pull import pull_contacts_by_ids  # noqa: PLC0415
    import db.writers as db_writers  # noqa: PLC0415
    import db.revenue_repository as repo  # noqa: PLC0415

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = progress if progress is not None else {}
    state.update({
        "running": True, "dry_run": dry_run, "phase": "loading",
        "job_id": job_id, "started_at": started_at,
    })

    summary = _empty_summary()
    summary["already_excluded_legacy"] = db_writers.count_lead_exclusions()
    errors: list = []
    batches_detail: list = []
    completed: list = []

    def _emit(status: str, extra: dict | None = None) -> None:
        if checkpoint is None:
            return
        snap = {
            "status": status,
            "phase": state.get("phase"),
            "current_chunk": state.get("current_chunk"),
            "completed_chunks": list(completed),
            "summary": dict(summary),
            "chunks": list(batches_detail),
            "errors": list(errors),
        }
        if extra:
            snap.update(extra)
        try:
            checkpoint(job_id, snap)
        except Exception as exc:  # noqa: BLE001
            log.warning("[lead_reconciliation] checkpoint failed: %s", exc)

    data = repo.fetch_missing_event_date_leads()
    leads = data.get("rows", []) if data.get("available") else []
    summary["missing_event_dates"] = len(leads)

    with_id = [l for l in leads if (l.get("contact_id") or "").strip()]
    without_id = [l for l in leads if not (l.get("contact_id") or "").strip()]
    summary["recoverable_by_contact_id"] = len(with_id)
    summary["no_usable_contact_identity"] = len(without_id)

    # ── Rows with no usable HubSpot identity → audited legacy exclusion ────────
    state["phase"] = "excluding_orphans"
    for lead in without_id:
        if not dry_run:
            db_writers.write_lead_exclusion(
                lead.get("lead_key"), EXCLUSION_NO_IDENTITY,
                details="No HubSpot contact_id on any local snapshot for this lead.",
                reconciliation_job_id=job_id,
            )
    _emit("running")

    # ── Rows with a contact_id → batch-read HubSpot createdate ────────────────
    by_cid: dict = {}
    for lead in with_id:
        by_cid.setdefault(lead["contact_id"], lead.get("lead_key") or lead["contact_id"])
    cids = list(by_cid)

    state["phase"] = "reconciling"
    for batch_index, batch in enumerate(_chunks(cids, batch_size)):
        chunk_key = f"batch:{batch_index}"
        state["current_chunk"] = chunk_key
        batch_result = {"chunk": chunk_key, "size": len(batch),
                        "recovered": 0, "not_found": 0, "no_createdate": 0, "unresolved": 0}
        try:
            found = pull_contacts_by_ids(batch)  # may raise on transient API error
        except Exception as exc:  # noqa: BLE001
            # Transient failure → unresolved (retryable), never excluded.
            errors.append(f"{chunk_key}: {exc}")
            summary["remaining_unresolved"] += len(batch)
            batch_result["unresolved"] = len(batch)
            batches_detail.append(batch_result)
            completed.append(chunk_key)
            _emit("running")
            continue

        for cid in batch:
            lead_key = by_cid[cid]
            if cid in found:
                created = found[cid]
                if created:
                    summary["recovered_from_createdate"] += 1
                    batch_result["recovered"] += 1
                    if not dry_run:
                        # Backfill EVERY duplicate snapshot for this contact, using
                        # ONLY the real HubSpot createdate.
                        db_writers.backfill_event_date_for_contact(cid, created)
                else:
                    summary["hubspot_contact_no_createdate"] += 1
                    batch_result["no_createdate"] += 1
                    if not dry_run:
                        db_writers.write_lead_exclusion(
                            lead_key, EXCLUSION_NO_CREATEDATE,
                            details="HubSpot contact exists but has no createdate.",
                            reconciliation_job_id=job_id,
                        )
            else:
                summary["hubspot_contact_not_found"] += 1
                batch_result["not_found"] += 1
                if not dry_run:
                    db_writers.write_lead_exclusion(
                        lead_key, EXCLUSION_NOT_FOUND,
                        details="HubSpot contact_id not found on read.",
                        reconciliation_job_id=job_id,
                    )

        batches_detail.append(batch_result)
        completed.append(chunk_key)
        _emit("running")

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # After a real run, the only remaining included missing-date leads are the
    # unresolved (retryable) ones; everything else was recovered or excluded.
    ready = summary["remaining_unresolved"] == 0
    status = "failed" if (errors and not batches_detail) else ("partial" if errors else "success")

    result = {
        "status": status,
        "dry_run": dry_run,
        "started_at": started_at,
        "finished_at": finished_at,
        "summary": summary,
        "revenue_truth_lead_dates_ready": ready,
        "chunks": batches_detail,
        "errors": errors,
    }

    state.update({"running": False, "phase": "done", "finished_at": finished_at, "latest": result})
    if checkpoint is not None:
        _state_phase = state.get("phase")
        state["phase"] = "done"
        _emit(status, {"finished_at": finished_at, "revenue_truth_lead_dates_ready": ready})
        state["phase"] = _state_phase
    return result
