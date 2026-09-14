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
2. the most recent **incremental** run succeeded — proven by
   ``last_incremental_status``, not inferred;
3. that successful incremental ran **within the freshness threshold**, measured
   on ``last_successful_incremental_at``.

Anything else blocks. The blocking states are kept apart because they have
different remedies: stale means the scheduler is behind, failed means a run
errored, incomplete means the backfill never finished, and unavailable means we
could not look — and only the last one is not a statement about the pipeline.

Which timestamp, and why the obvious one is wrong twice
-------------------------------------------------------
**Not ``latest_modified_at``** — the newest contact modification the sync
happened to see. That goes stale on its own whenever HubSpot is quiet, and a
quiet CRM is not a broken pipeline. Using it would make a working system look
broken every weekend, and a broken one look fine for as long as its last read
stayed recent.

**Not ``last_incremental_at`` either**, despite the name. The contact-funnel
sync stamps it on BOTH bootstrap and incremental runs, so a successful bootstrap
is indistinguishable from a fresh incremental feed. A window could then certify
against a source whose incremental pipeline had died, on the strength of a
backfill.

So the contract reads ``last_successful_incremental_at``, which only an
incremental run that SUCCEEDED ever advances, together with
``last_incremental_status``, which records that incremental's own outcome and
which no bootstrap ever overwrites. That second column exists because two are
not enough:

    bootstrap completes T0 → incremental FAILS T1 → bootstrap succeeds T2

With only ``last_status`` and ``last_sync_mode``, T2 overwrites both and the
evidence that the required incremental failed is gone.

Legacy rows carry NULL in all of these and therefore **fail closed** until one
real incremental sync records the new evidence. That is deliberate: a row that
predates the contract cannot prove anything about it.
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
SYNC_FAILED = "source_last_incremental_failed"
BOOTSTRAP_INCOMPLETE = "source_bootstrap_incomplete"
NEVER_RUN = "source_no_successful_incremental"
STATE_MISSING = "source_sync_state_missing"
STATE_UNAVAILABLE = "source_sync_state_unavailable"
#: The row predates the PR-ADS-160 §3 provenance columns, so it cannot prove a
#: successful incremental either way. Distinct from NEVER_RUN, which is a
#: statement about the pipeline; this is a statement about the record.
PROVENANCE_MISSING = "source_incremental_provenance_missing"

FRESHNESS_REASONS = (FRESH, STALE, SYNC_FAILED, BOOTSTRAP_INCOMPLETE,
                     NEVER_RUN, STATE_MISSING, STATE_UNAVAILABLE,
                     PROVENANCE_MISSING)

#: Every reason except FRESH blocks certification. Listed explicitly rather than
#: derived, so adding a reason without deciding whether it blocks is impossible.
BLOCKING_REASONS = (STALE, SYNC_FAILED, BOOTSTRAP_INCOMPLETE, NEVER_RUN,
                    STATE_MISSING, STATE_UNAVAILABLE, PROVENANCE_MISSING)


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
    mode = row.get("last_sync_mode")
    incremental_status = row.get("last_incremental_status")
    proven_at = _as_datetime(row.get("last_successful_incremental_at"))
    # Kept for the report only. It is NOT the freshness clock — both modes
    # stamp it, so it cannot prove an incremental ran at all.
    last_run = _as_datetime(row.get("last_incremental_at"))

    age_hours = (None if proven_at is None
                 else round((now - proven_at).total_seconds() / 3600.0, 2))
    base = {**base, "bootstrap_status": bootstrap, "age_hours": age_hours,
            "last_sync_mode": mode,
            "last_incremental_status": incremental_status,
            "last_successful_incremental_at": (proven_at.isoformat()
                                               if proven_at else None),
            "last_incremental_at": (last_run.isoformat() if last_run else None)}

    # Order matters: the most fundamental failure is reported, not the first
    # symptom. A partial bootstrap whose last incremental also failed is a
    # bootstrap problem — fixing the incremental alone would still leave the
    # backfill unfinished.
    if bootstrap != "complete":
        return {**base, "reason": BOOTSTRAP_INCOMPLETE,
                "detail": (f"the contact-funnel bootstrap is '{bootstrap}', not "
                           f"complete, so the historical population is still "
                           f"arriving")}

    # A row written before the PR-ADS-160 §3 columns existed can say nothing
    # about incremental success either way. It fails closed rather than being
    # read optimistically — which is what made a bootstrap look fresh.
    #
    # `last_sync_mode` is the migration marker: a row that carries it was
    # written by the current service, so an absent incremental is a fact about
    # the PIPELINE (nothing incremental has run) rather than about the RECORD.
    # The two have different remedies and are reported apart.
    if mode is None and incremental_status is None and proven_at is None:
        return {**base, "reason": PROVENANCE_MISSING,
                "detail": ("this sync state predates the incremental-provenance "
                           "columns, so a successful incremental run cannot be "
                           "proven; it will clear once one real incremental "
                           "sync records the evidence")}

    if incremental_status and incremental_status != "success":
        return {**base, "reason": SYNC_FAILED,
                "detail": (f"the most recent contact-funnel INCREMENTAL run "
                           f"ended '{incremental_status}'. A later bootstrap "
                           f"does not clear this: the incremental feed is what "
                           f"keeps the source current")}

    if proven_at is None:
        return {**base, "reason": NEVER_RUN,
                "detail": ("no incremental contact-funnel sync has ever "
                           "succeeded, so the source has never been proven to "
                           "be updating. A completed bootstrap is a backfill, "
                           "not a feed")}

    if age_hours is not None and age_hours > max_age_hours:
        return {**base, "reason": STALE,
                "detail": (f"the last SUCCESSFUL incremental sync was "
                           f"{age_hours}h ago, beyond the {max_age_hours}h "
                           f"threshold; a window certified now would describe "
                           f"data that stopped arriving")}

    return {**base, "fresh": True, "reason": FRESH,
            "detail": (f"the contact-funnel bootstrap is complete and an "
                       f"incremental sync succeeded {age_hours}h ago, within "
                       f"the {max_age_hours}h threshold")}


def blocks_certification(verdict: dict) -> bool:
    """Anything that is not proven fresh blocks. Unknown blocks too."""
    return (verdict or {}).get("fresh") is not True
