"""
services/hubspot_contact_funnel_sync_service.py

PR-ADS-153B — the ONE ingestion path that owns ``hubspot_contact_funnel``.

Ownership doctrine (PR-ADS-153B §30)
------------------------------------
Exactly one service writes the canonical contact store. The daily / weekly /
monthly / incremental schedulers may CALL this service, but none of them writes
canonical contact rows independently, so four writers can never append
conflicting truth. The legacy ``leads`` snapshot writers are untouched and keep
serving pre-PR-ADS-153C pages.

Sync doctrine
-------------
  * Watermarked on ``lastmodifieddate``, never on contact-creation recency. A
    contact created two years ago whose lifecycle changed today IS refreshed
    today — the defect PR-ADS-153A found in the legacy 30-day-createdate sync.
  * All sources. No ``hs_analytics_source`` filter, so ``all_source`` genuinely
    means all source (PR-ADS-153B §18).
  * Resumable and restart-safe. The watermark is persisted after EVERY page, so
    completion state never lives in process memory and a killed worker resumes
    where it stopped.
  * Idempotent. Re-ingesting a page upserts on ``contact_id``; rows are never
    duplicated and an older read can never overwrite newer state.
  * Fail-closed. A partial read raises out of the connector and is recorded as a
    failed batch with the watermark left where it was proven — a partial sync is
    never reported as complete.
  * Bootstrap is the same code path with the watermark at the epoch, so there is
    one implementation to reason about, not two.

Read-only with respect to HubSpot. NEVER writes to HubSpot or Google Ads.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import writers as db_writers

log = logging.getLogger(__name__)

SCOPE = "contacts"

# Freshness/registry keys. These MUST match the DATASET_FRESHNESS_CONFIG entries
# (see tests/test_pr_ads_153b_canonical_funnel.py — the writer-key parity test
# exists so the `google_ads` vs `google_ads_api` mismatch class cannot recur).
SYNC_SOURCE = "hubspot"
DATASET_CONTACT_FUNNEL = "contact_funnel"
DATASET_LIFECYCLE_EVENTS = "lifecycle_events"

# Re-fetch a small overlap before the stored watermark so a contact modified in
# the same second as the last checkpoint can never be skipped.
DEFAULT_OVERLAP_MINUTES = 15

# Bootstrap floor. The epoch means "every contact in the portal"; HubSpot returns
# them ascending by modification time so the scan is naturally resumable.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

BOOTSTRAP_NOT_STARTED = "not_started"
BOOTSTRAP_RUNNING = "running"
BOOTSTRAP_PARTIAL = "partial"
BOOTSTRAP_COMPLETE = "complete"
BOOTSTRAP_FAILED = "failed"

MODE_BOOTSTRAP = "bootstrap"
MODE_INCREMENTAL = "incremental"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_since(state: dict | None, mode: str, *, overlap_minutes: int) -> datetime:
    """Resolve the modification watermark this run starts from. Pure.

    Bootstrap always starts at the epoch. Incremental resumes from the durable
    watermark minus an explicit overlap; with no watermark it degrades to a
    bootstrap rather than silently syncing only recent contacts.
    """
    if mode == MODE_BOOTSTRAP:
        return _EPOCH
    watermark = _as_datetime((state or {}).get("last_modified_watermark"))
    if watermark is None:
        return _EPOCH
    return watermark - timedelta(minutes=max(0, int(overlap_minutes)))


def get_bootstrap_mode() -> str:
    """Choose the mode for an automated run, from DURABLE state only.

    A portal that has never completed its historical bootstrap keeps bootstrapping
    (resuming from its watermark) until it finishes; only then does the scheduler
    switch to incremental. Completeness is read from the database, never inferred
    from the presence of recent rows.
    """
    state = db_writers.get_contact_funnel_sync_state(SCOPE) or {}
    status = (state.get("bootstrap_status") or BOOTSTRAP_NOT_STARTED).strip().lower()
    if status == BOOTSTRAP_COMPLETE:
        return MODE_INCREMENTAL
    return MODE_BOOTSTRAP


def run_contact_funnel_sync(
    *,
    mode: str = MODE_INCREMENTAL,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
    max_pages: int | None = None,
    run_id: int | None = None,
    page_iterator=None,
    now: datetime | None = None,
) -> dict:
    """Sync canonical HubSpot contacts into ``hubspot_contact_funnel``.

    ``page_iterator`` is injectable so the whole orchestration — checkpointing,
    idempotency, failure handling — is testable without HubSpot.

    Returns a structured summary. Never raises: a failure is reported as
    ``status='failed'`` with the durable watermark left at the last PROVEN
    checkpoint.
    """
    if mode not in (MODE_BOOTSTRAP, MODE_INCREMENTAL):
        raise ValueError(f"Unknown sync mode '{mode}'")

    now = now or _utcnow()
    state = db_writers.get_contact_funnel_sync_state(SCOPE) or {}
    since = resolve_since(state, mode, overlap_minutes=overlap_minutes)

    sync_type = "backfill" if mode == MODE_BOOTSTRAP else "daily"
    batch_id = db_writers.start_sync_batch(
        source=SYNC_SOURCE, dataset=DATASET_CONTACT_FUNNEL,
        sync_type=sync_type, date_from=since.date(), date_to=now.date(),
        run_id=run_id,
    )
    events_batch_id = db_writers.start_sync_batch(
        source=SYNC_SOURCE, dataset=DATASET_LIFECYCLE_EVENTS,
        sync_type=sync_type, date_from=since.date(), date_to=now.date(),
        run_id=run_id,
    )

    if mode == MODE_BOOTSTRAP:
        db_writers.update_contact_funnel_sync_state(
            SCOPE,
            bootstrap_status=BOOTSTRAP_RUNNING,
            bootstrap_started_at=now,
            last_error=None,
        )

    if page_iterator is None:
        from connectors.hubspot_pull import (  # noqa: PLC0415
            iter_contacts_modified_since,
        )
        page_iterator = iter_contacts_modified_since

    from connectors.hubspot_pull import (  # noqa: PLC0415
        normalize_contact_funnel_row,
    )

    contacts_seen = 0
    contacts_written = 0
    pages = 0
    rejected_no_identity = 0
    stage_events_written = 0
    watermark = _as_datetime(state.get("last_modified_watermark"))
    error: str | None = None

    try:
        for raw_page, meta in page_iterator(since, max_pages=max_pages):
            pages += 1
            contacts_seen += len(raw_page)

            normalised = []
            for raw in raw_page:
                row = normalize_contact_funnel_row(raw)
                if row is None:
                    rejected_no_identity += 1
                    continue
                normalised.append(row)

            written = db_writers.upsert_hubspot_contact_funnel(
                normalised, sync_batch_id=batch_id or None)
            contacts_written += written
            stage_events_written += sum(
                1 for r in normalised if r.get("latest_stage_entry_at") is not None
            )

            page_watermark = _page_watermark(normalised, meta)
            if page_watermark is not None:
                watermark = page_watermark

            # Durable checkpoint after EVERY page — a killed worker resumes here.
            db_writers.update_contact_funnel_sync_state(
                SCOPE,
                last_modified_watermark=watermark,
                latest_modified_at=watermark,
                contacts_seen=contacts_seen,
                pages_fetched=pages,
                last_batch_id=batch_id or None,
                last_error=None,
            )

    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:500]
        log.warning("[hubspot_contact_funnel] sync failed: %s", exc)
        for bid in (batch_id, events_batch_id):
            if bid:
                db_writers.finish_sync_batch(
                    batch_id=bid, status="failed", error_message=str(exc)[:1000])
        db_writers.update_contact_funnel_sync_state(
            SCOPE,
            bootstrap_status=(
                BOOTSTRAP_PARTIAL if mode == MODE_BOOTSTRAP else
                (state.get("bootstrap_status") or BOOTSTRAP_NOT_STARTED)
            ),
            last_error=error,
            contacts_seen=contacts_seen,
            pages_fetched=pages,
        )
        return {
            "status": "failed",
            "mode": mode,
            "error": error,
            "since": since.isoformat(),
            "contacts_seen": contacts_seen,
            "contacts_written": contacts_written,
            "pages": pages,
            "watermark": watermark.isoformat() if watermark else None,
        }

    # A capped run has not proven it reached the end of the backlog.
    truncated = max_pages is not None and pages >= max_pages
    if mode == MODE_BOOTSTRAP:
        bootstrap_status = BOOTSTRAP_PARTIAL if truncated else BOOTSTRAP_COMPLETE
    else:
        bootstrap_status = state.get("bootstrap_status") or BOOTSTRAP_NOT_STARTED

    state_update = {
        "bootstrap_status": bootstrap_status,
        "last_modified_watermark": watermark,
        "latest_modified_at": watermark,
        "last_incremental_at": now,
        "contacts_seen": contacts_seen,
        "pages_fetched": pages,
        "last_batch_id": batch_id or None,
        "last_error": None,
    }
    if mode == MODE_BOOTSTRAP and bootstrap_status == BOOTSTRAP_COMPLETE:
        state_update["bootstrap_completed_at"] = now
    db_writers.update_contact_funnel_sync_state(SCOPE, **state_update)

    if batch_id:
        db_writers.finish_sync_batch(
            batch_id=batch_id, status="success", row_count=contacts_written,
            last_source_date=now.date(),
        )
    if events_batch_id:
        db_writers.finish_sync_batch(
            batch_id=events_batch_id, status="success",
            row_count=stage_events_written, last_source_date=now.date(),
        )

    return {
        "status": "success",
        "mode": mode,
        "since": since.isoformat(),
        "contacts_seen": contacts_seen,
        "contacts_written": contacts_written,
        "stage_events_written": stage_events_written,
        "rejected_no_identity": rejected_no_identity,
        "pages": pages,
        "truncated": truncated,
        "bootstrap_status": bootstrap_status,
        "watermark": watermark.isoformat() if watermark else None,
    }


def _page_watermark(rows: list[dict], meta: dict | None):
    """Newest modification timestamp proven by this page.

    Prefers the rows themselves; falls back to the iterator's reported watermark.
    Never advances past evidence actually persisted.
    """
    stamps = [r.get("last_modified_at") for r in rows if r.get("last_modified_at")]
    stamps = [_as_datetime(s) for s in stamps]
    stamps = [s for s in stamps if s is not None]
    if stamps:
        return max(stamps)
    if meta and meta.get("watermark_ms"):
        return datetime.fromtimestamp(
            int(meta["watermark_ms"]) / 1000.0, tz=timezone.utc)
    return None


def build_coverage() -> dict:
    """Bootstrap + coverage report for the canonical contact store (§8).

    Completeness is never claimed merely because recent rows exist: the bootstrap
    status is explicit and separate from row recency.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    state = db_writers.get_contact_funnel_sync_state(SCOPE)
    coverage = repo.fetch_coverage_summary()

    if not coverage.get("available"):
        return {
            "available": False,
            "reason": "canonical_contact_store_unavailable",
            "bootstrap_status": (state or {}).get("bootstrap_status") or "unknown",
        }

    summary = coverage.get("summary") or {}
    bootstrap_status = (state or {}).get("bootstrap_status") or BOOTSTRAP_NOT_STARTED

    return {
        "available": True,
        "bootstrap_status": bootstrap_status,
        "bootstrap_started_at": _iso((state or {}).get("bootstrap_started_at")),
        "bootstrap_completed_at": _iso((state or {}).get("bootstrap_completed_at")),
        "last_modified_watermark": _iso((state or {}).get("last_modified_watermark")),
        "last_incremental_at": _iso((state or {}).get("last_incremental_at")),
        "last_error": (state or {}).get("last_error"),
        "totals": {
            "contacts": summary.get("total_contacts"),
            "with_lifecycle_stage": summary.get("with_lifecycle_stage"),
            "with_mql_status": summary.get("with_mql_status"),
        },
        "stage_entry_coverage": {
            "lead": summary.get("with_date_entered_lead"),
            "mql": summary.get("with_date_entered_mql"),
            "sql": summary.get("with_date_entered_sql"),
            "opportunity": summary.get("with_date_entered_opportunity"),
            "customer": summary.get("with_date_entered_customer"),
        },
        "earliest_created_at": _iso(summary.get("earliest_created_at")),
        "latest_created_at": _iso(summary.get("latest_created_at")),
        "latest_modified_at": _iso(summary.get("latest_modified_at")),
        "by_lifecycle_stage": coverage.get("by_lifecycle_stage"),
        "by_mql_status": coverage.get("by_mql_status"),
        "by_source": coverage.get("by_source"),
        "note": (
            "Bootstrap completeness is explicit. The presence of recent contacts "
            "is never treated as proof that history was ingested."
        ),
    }


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
