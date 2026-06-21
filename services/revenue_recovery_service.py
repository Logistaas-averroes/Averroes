"""
Revenue Truth Recovery Service (PR-ADS-114)

Admin-only, resumable, local recovery of the revenue truth layer:

  1. Historical paid-search **contacts** by HubSpot ``createdate`` — so every
     paid contact has at least one row with a real ``contact_created_at``
     (the prerequisite for Leads / SQLs to become available).
  2. Historical closed-won **deals** by HubSpot ``closedate`` — fetched DIRECTLY
     (never via recently-created contacts), with their real GCLID / campaign /
     keyword / country evidence written into ``gclid_attribution``.

Doctrine (identical to the scheduler):
  - Read-only from external platforms (HubSpot). Writes only to the local DB.
  - NEVER writes to Google Ads, HubSpot, bids, budgets, or conversions.
  - NEVER fabricates GCLIDs. A closed-won deal without real click evidence is
    reported honestly and is NOT turned into attributed revenue.

The work is processed in safe date chunks and is resumable: completed chunks are
recorded in the shared progress state, so a re-run with ``resume=True`` skips
chunks already done. Dry-run performs the read-only pulls and reports expected
impact without writing anything.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from dateutil.relativedelta import relativedelta

from analysis.business_windows import resolve_window

log = logging.getLogger(__name__)

# All-Time floor for the default recovery range. HubSpot paid-search history for
# this account does not predate this; chunking from here is safe and bounded.
DEFAULT_RECOVERY_START = date(2018, 1, 1)
DEFAULT_CHUNK_MONTHS = 1

# HubSpot won stage (revenue truth).
WON_DEAL_STAGE_ID = "326093516"


def _today() -> date:
    return datetime.now(tz=timezone.utc).date()


def _empty_summary() -> dict:
    return {
        "paid_contacts_recovered": 0,
        "contacts_still_missing_createdate": 0,
        "closed_won_deals_found": 0,
        "closed_won_deals_with_gclid": 0,
        "attributed_deal_rows_written": 0,
        "closed_won_deals_without_gclid": 0,
        "deals_without_campaign_mapping": 0,
    }


def _has_createdate(contact: dict) -> bool:
    props = contact.get("properties") or contact
    return bool((props.get("createdate") or "").strip() if isinstance(props.get("createdate"), str)
                else props.get("createdate"))


def build_attribution_rows_from_deals(deals: list) -> tuple[list, dict]:
    """Split closed-won deals into attributable rows + honest counts.

    Only deals with REAL GCLID evidence become ``gclid_attribution`` rows. No
    synthetic GCLID is ever generated. Returns (rows, counts) where counts has
    closed_won_deals_found / _with_gclid / _without_gclid / deals_without_campaign_mapping.
    """
    rows: list = []
    found = len(deals)
    with_gclid = 0
    without_campaign = 0

    for d in deals:
        gclid = (d.get("gclid") or "").strip()
        campaign = (d.get("campaign_name") or d.get("campaign") or "").strip()
        if not campaign:
            without_campaign += 1
        if not gclid:
            continue  # no real click evidence — never attribute
        with_gclid += 1
        # Preserve the supplied evidence quality: a direct GCLID stays "gclid";
        # a GCLID recovered from the landing-page URL is "first_url". Default to
        # "gclid" only when the caller did not specify a source.
        match_source = (d.get("match_source") or "gclid").strip().lower()
        match_status = "matched" if match_source == "gclid" else "url_fallback"
        rows.append({
            "gclid": gclid,
            "contact_id": d.get("contact_id"),
            "deal_id": d.get("deal_id"),
            "campaign_name": d.get("campaign_name") or d.get("campaign"),
            "keyword": d.get("keyword"),
            "country": d.get("country"),
            "company": d.get("company"),
            "first_url": d.get("first_url"),
            "deal_close_date": d.get("deal_close_date"),
            "deal_amount_usd": d.get("deal_amount_usd") or d.get("deal_amount"),
            "deal_stage": d.get("deal_stage") or WON_DEAL_STAGE_ID,
            "deal_stage_label": d.get("deal_stage_label") or "Deal Won / Payment Received",
            "match_status": match_status,
            "match_source": match_source,
        })

    counts = {
        "closed_won_deals_found": found,
        "closed_won_deals_with_gclid": with_gclid,
        "closed_won_deals_without_gclid": found - with_gclid,
        "deals_without_campaign_mapping": without_campaign,
    }
    return rows, counts


def _iter_chunks(start: date, end: date, chunk_months: int):
    """Yield (chunk_from, chunk_to) date pairs covering [start, end] inclusive."""
    cursor = start
    step = relativedelta(months=max(1, chunk_months))
    while cursor <= end:
        chunk_to = min(cursor + step - relativedelta(days=1), end)
        yield cursor, chunk_to
        cursor = chunk_to + relativedelta(days=1)


def _resolve_range(date_from, date_to) -> tuple[date, date]:
    """Resolve the recovery range; default is All Time (DEFAULT_RECOVERY_START→today)."""
    end = date.fromisoformat(date_to) if date_to else _today()
    start = date.fromisoformat(date_from) if date_from else DEFAULT_RECOVERY_START
    if start > end:
        raise ValueError("date_from must be before or equal to date_to")
    return start, end


def run_revenue_recovery(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    dry_run: bool = True,
    chunk_months: int = DEFAULT_CHUNK_MONTHS,
    resume: bool = False,
    progress: dict | None = None,
    run_id: int | None = None,
    job_id: str | None = None,
    load_completed=None,
    checkpoint=None,
) -> dict:
    """Recover the revenue truth layer over a date range, in resumable chunks.

    Read-only pulls from HubSpot; local DB writes only when ``dry_run`` is False.
    NEVER writes to any external platform and NEVER fabricates a GCLID.

    Args:
        date_from / date_to: ISO dates. Defaults to All Time.
        dry_run: when True, pull read-only and report expected impact; write nothing.
        chunk_months: chunk size in months.
        resume: skip chunks already completed (loaded durably via ``load_completed``).
        progress: shared mutable state dict (for live status); created if None.
        run_id: local run id to attribute writes to (a real one is created when None).
        job_id: durable job identifier passed to ``checkpoint``.
        load_completed: optional () -> iterable of completed chunk keys, read from
            durable storage so resume survives a process restart (not in-memory).
        checkpoint: optional (job_id, snapshot_dict) -> None, called after each
            chunk and at completion to persist progress to durable storage.

    Returns a structured result dict with summary, per-chunk detail, and errors.
    """
    from connectors.hubspot_pull import (  # noqa: PLC0415
        pull_paid_search_contacts_in_range,
        pull_closed_won_deals_in_range,
    )
    import db.writers as db_writers  # noqa: PLC0415

    start, end = _resolve_range(date_from, date_to)
    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # A genuine local run record so every recovered write is attributable.
    if run_id is None and not dry_run:
        run_id = db_writers.write_run({
            "run_type": "revenue_recovery",
            "started_at": started_at,
            "status": "running",
        })

    state = progress if progress is not None else {}
    state.update({
        "running": True,
        "dry_run": dry_run,
        "phase": "starting",
        "job_id": job_id,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "chunk_months": chunk_months,
        "started_at": started_at,
    })

    # Resume reads completed chunks from DURABLE storage when available, so a
    # restarted process picks up exactly where it left off (not from memory).
    if resume and load_completed is not None:
        try:
            completed = set(load_completed() or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("[revenue_recovery] load_completed failed: %s", exc)
            completed = set()
    else:
        completed = set(state.get("completed_chunks", []) if resume else [])
    state["completed_chunks"] = list(completed)

    summary = _empty_summary()
    chunks_detail: list = []
    errors: list = []

    def _emit_checkpoint(status: str) -> None:
        if checkpoint is None:
            return
        try:
            checkpoint(job_id, {
                "status": status,
                "phase": state.get("phase"),
                "current_chunk": state.get("current_chunk"),
                "completed_chunks": list(completed),
                "summary": dict(summary),
                "chunks": list(chunks_detail),
                "errors": list(errors),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("[revenue_recovery] checkpoint failed: %s", exc)

    for chunk_from, chunk_to in _iter_chunks(start, end, chunk_months):
        chunk_key = f"{chunk_from.isoformat()}:{chunk_to.isoformat()}"
        if resume and chunk_key in completed:
            continue

        chunk_result = {
            "date_from": chunk_from.isoformat(),
            "date_to": chunk_to.isoformat(),
            "contacts_read": 0, "contacts_written": 0,
            "contacts_missing_createdate": 0,
            "deals_found": 0, "deals_with_gclid": 0,
            "attributed_rows_written": 0,
        }

        # ── Phase 1: contacts (prerequisite for Leads / SQLs) ─────────────────
        state["phase"] = "contacts"
        state["current_chunk"] = chunk_key
        try:
            contacts = pull_paid_search_contacts_in_range(
                date_from=chunk_from.isoformat(), date_to=chunk_to.isoformat(),
            )
            chunk_result["contacts_read"] = len(contacts)
            missing = sum(1 for c in contacts if not _has_createdate(c))
            chunk_result["contacts_missing_createdate"] = missing
            summary["contacts_still_missing_createdate"] += missing
            if not dry_run and contacts:
                written = db_writers.write_leads(run_id, contacts)
                chunk_result["contacts_written"] = written
                summary["paid_contacts_recovered"] += written
            else:
                summary["paid_contacts_recovered"] += len(contacts)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"contacts {chunk_key}: {exc}")
            log.warning("[revenue_recovery] contacts %s failed: %s", chunk_key, exc)

        # ── Phase 2: closed-won deals by closedate → attribution ──────────────
        state["phase"] = "revenue"
        try:
            deals = pull_closed_won_deals_in_range(
                date_from=chunk_from.isoformat(), date_to=chunk_to.isoformat(),
            )
            rows, counts = build_attribution_rows_from_deals(deals)
            chunk_result["deals_found"] = counts["closed_won_deals_found"]
            chunk_result["deals_with_gclid"] = counts["closed_won_deals_with_gclid"]
            summary["closed_won_deals_found"] += counts["closed_won_deals_found"]
            summary["closed_won_deals_with_gclid"] += counts["closed_won_deals_with_gclid"]
            summary["closed_won_deals_without_gclid"] += counts["closed_won_deals_without_gclid"]
            summary["deals_without_campaign_mapping"] += counts["deals_without_campaign_mapping"]
            if not dry_run and rows:
                written = db_writers.write_gclid_attribution(run_id, rows)
                chunk_result["attributed_rows_written"] = written
                summary["attributed_deal_rows_written"] += written
            # In dry-run, attributed_deal_rows_written stays 0 (nothing written);
            # closed_won_deals_with_gclid is the expected impact.
        except Exception as exc:  # noqa: BLE001
            errors.append(f"revenue {chunk_key}: {exc}")
            log.warning("[revenue_recovery] revenue %s failed: %s", chunk_key, exc)

        chunks_detail.append(chunk_result)
        completed.add(chunk_key)
        state["completed_chunks"] = list(completed)
        state["last_chunk_result"] = chunk_result
        # Persist a durable checkpoint after every chunk so progress (and the
        # completed-chunk set used by resume) survives a process restart.
        _emit_checkpoint("running")

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = "failed" if errors and not chunks_detail else ("partial" if errors else "success")

    if run_id is not None and not dry_run:
        db_writers.update_run(run_id, {
            "finished_at": finished_at,
            "status": status,
        })

    result = {
        "status": status,
        "dry_run": dry_run,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "chunk_months": chunk_months,
        "started_at": started_at,
        "finished_at": finished_at,
        "summary": summary,
        "chunks": chunks_detail,
        "errors": errors,
    }

    state.update({
        "running": False,
        "phase": "done",
        "finished_at": finished_at,
        "latest": result,
    })
    state["phase"] = "done"
    if checkpoint is not None:
        try:
            checkpoint(job_id, {
                "status": status,
                "phase": "done",
                "current_chunk": None,
                "completed_chunks": list(completed),
                "summary": dict(summary),
                "chunks": list(chunks_detail),
                "errors": list(errors),
                "finished_at": finished_at,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("[revenue_recovery] final checkpoint failed: %s", exc)
    return result


def recovery_dry_run(date_from: str | None = None, date_to: str | None = None,
                     chunk_months: int = DEFAULT_CHUNK_MONTHS) -> dict:
    """Convenience: expected-impact dry run over the (default All-Time) range."""
    return run_revenue_recovery(
        date_from=date_from, date_to=date_to, dry_run=True, chunk_months=chunk_months,
    )
