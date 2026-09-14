"""
analysis/sql_coverage_freshness.py

PR-ADS-160 §5 — ONE freshness contract for SQL window certification.

Why this is a certification prerequisite and not a footnote
-----------------------------------------------------------
Certification says a window's SQL total is trustworthy. A window can be
*complete* — every undated contact ruled out, no prospective gap able to belong
to it — and still be worthless if the source stopped updating: it would be
complete with respect to data that has stopped arriving. "Nothing is missing
from what we have" is not "nothing is missing".

The first cut checked reader reconciliation and store readability and called
that certification. It never asked whether the contact funnel was still being
fed. A sync that died a week ago would have certified every window behind it.

The contract
------------
Certification requires ALL of:

1. the bootstrap is **complete** — a partial backfill means the historical
   population itself is still arriving;
2. the most recent incremental sync **succeeded** — ``last_error`` is clear;
3. the sync has run **within the freshness threshold**.

Anything else blocks. The four blocking states are kept apart because they have
different remedies: stale means the scheduler is behind, failed means a run
errored, incomplete means the backfill never finished, and unavailable means we
could not look — and only the last one is not a statement about the pipeline.

Which timestamp
---------------
``last_incremental_at`` — when the sync last RAN — not ``latest_modified_at``,
which is the newest contact modification the sync happened to see. The second
one goes stale on its own whenever HubSpot is quiet, and a quiet CRM is not a
broken pipeline. Confusing the two would make a working system look broken every
weekend, and a broken one look fine for as long as its last read stayed recent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

#: How recently the canonical contact-funnel sync must have run. The scheduler
#: runs daily, so a full day plus margin distinguishes "behind schedule" from
#: "between runs" without flapping.
DEFAULT_MAX_AGE_HOURS = 36

# ── Stable reason codes · denominator: one freshness assessment ─────────────
FRESH = "source_fresh"
STALE = "source_stale"
SYNC_FAILED = "source_last_sync_failed"
BOOTSTRAP_INCOMPLETE = "source_bootstrap_incomplete"
NEVER_RUN = "source_never_synced"
STATE_MISSING = "source_sync_state_missing"
STATE_UNAVAILABLE = "source_sync_state_unavailable"

FRESHNESS_REASONS = (FRESH, STALE, SYNC_FAILED, BOOTSTRAP_INCOMPLETE,
                     NEVER_RUN, STATE_MISSING, STATE_UNAVAILABLE)

#: Every reason except FRESH blocks certification. Listed explicitly rather than
#: derived, so adding a reason without deciding whether it blocks is impossible.
BLOCKING_REASONS = (STALE, SYNC_FAILED, BOOTSTRAP_INCOMPLETE, NEVER_RUN,
                    STATE_MISSING, STATE_UNAVAILABLE)


def _as_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def assess(sync_state: dict, *, now: datetime | None = None,
           max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> dict:
    """Is the canonical contact-funnel source fresh enough to certify against?

    ``sync_state`` is ``db.crm_funnel_repository.fetch_contact_funnel_sync_state``'s
    result. Returns::

        {"fresh": bool | None, "reason": str, "age_hours": float | None,
         "last_incremental_at": str | None, "bootstrap_status": str | None,
         "max_age_hours": int, "detail": str}

    ``fresh`` is ``None`` — never ``False`` — when the state could not be read.
    False is a claim about the pipeline; None is a statement about us. Both
    block certification, and the caller must be able to tell them apart when it
    explains why.
    """
    now = now or datetime.now(tz=timezone.utc)
    base = {"fresh": False, "reason": STATE_MISSING, "age_hours": None,
            "last_incremental_at": None, "bootstrap_status": None,
            "max_age_hours": int(max_age_hours), "detail": ""}

    if not (sync_state or {}).get("available"):
        return {**base, "fresh": None, "reason": STATE_UNAVAILABLE,
                "detail": ("the contact-funnel sync state could not be read, so "
                           "it is unknown whether the source is still updating "
                           "— unknown is not fresh, and not a fault of the "
                           "pipeline")}

    row = (sync_state or {}).get("row")
    if not row:
        return {**base, "reason": STATE_MISSING,
                "detail": ("no contact-funnel sync state exists; the canonical "
                           "ingestion has never recorded a run")}

    bootstrap = row.get("bootstrap_status")
    last_run = _as_datetime(row.get("last_incremental_at"))
    last_error = (row.get("last_error") or "").strip()
    age_hours = (None if last_run is None
                 else round((now - last_run).total_seconds() / 3600.0, 2))
    base = {**base, "bootstrap_status": bootstrap, "age_hours": age_hours,
            "last_incremental_at": (last_run.isoformat() if last_run else None)}

    # Order matters: the most fundamental failure is reported, not the first
    # symptom. A partial bootstrap whose last run also errored is a bootstrap
    # problem — fixing the error alone would still leave the backfill unfinished.
    if bootstrap != "complete":
        return {**base, "reason": BOOTSTRAP_INCOMPLETE,
                "detail": (f"the contact-funnel bootstrap is '{bootstrap}', not "
                           f"complete, so the historical population is still "
                           f"arriving")}
    if last_error:
        return {**base, "reason": SYNC_FAILED,
                "detail": (f"the most recent contact-funnel sync recorded an "
                           f"error: {last_error[:200]}")}
    if last_run is None:
        return {**base, "reason": NEVER_RUN,
                "detail": ("the contact-funnel sync has never completed an "
                           "incremental run")}
    if age_hours is not None and age_hours > max_age_hours:
        return {**base, "reason": STALE,
                "detail": (f"the contact-funnel sync last ran {age_hours}h ago, "
                           f"beyond the {max_age_hours}h threshold; a window "
                           f"certified now would describe data that stopped "
                           f"arriving")}

    return {**base, "fresh": True, "reason": FRESH,
            "detail": (f"the contact-funnel sync completed its bootstrap and "
                       f"last ran {age_hours}h ago, within the "
                       f"{max_age_hours}h threshold")}


def blocks_certification(verdict: dict) -> bool:
    """Anything that is not proven fresh blocks. Unknown blocks too."""
    return (verdict or {}).get("fresh") is not True
