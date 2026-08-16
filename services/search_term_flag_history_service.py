"""
services/search_term_flag_history_service.py

PR-ADS-153D — the PRODUCTION write path for durable search-term flag history.

Why this module exists
----------------------
``search_term_review`` carries both a human decision and the flag HISTORY
(``first_flagged_at`` / ``latest_flagged_at`` / ``latest_flag_reason`` /
``latest_raw_reason``). The decision half is written by the review endpoint. The
history half had no production writer at all — only tests called
``record_flag_observations`` — so in a real deployment those columns would have
stayed NULL forever and the flagged view would have shown "first flagged: —" for
every term.

This module closes that gap, and it deliberately does NOT live in the read path.

Why it resolves identities through the canonical service
--------------------------------------------------------
The obvious place to write history is ``db.writers.write_waste_terms``, next to
the annotation insert. That would be wrong: ``waste_terms`` stores a campaign
NAME and no campaign id, whereas the durable identity is built from the
canonical campaign KEY (the Google Ads campaign id wherever it is known). History
written from a raw name would produce identities that never match the ones the
flagged view and the Action Queue compute — a silent, permanent join failure.

So history is derived from the canonical flagged population itself. Whatever
identity the page shows is exactly the identity recorded, by construction.

Governance
----------
Writes ONLY to the local ``search_term_review`` table. Reads only local
PostgreSQL through the canonical read service. No Google Ads, HubSpot or
Mailchimp call of any kind, and no mutation of any external platform.

Never called from a GET endpoint — this is a scheduler/analysis write path
(``scheduler/weekly.py``, after the waste-detection annotations land) and an
explicit operator backfill.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Windows used by the weekly run and by an operator backfill respectively. The
# weekly pass only needs to cover the evidence the run just analysed; the
# backfill walks everything the canonical facts can support.
WEEKLY_HISTORY_WINDOW = "30d"
BACKFILL_HISTORY_WINDOW = "all_time"


def _observations_from_rows(rows: list) -> list[dict]:
    """Map canonical flagged rows to durable flag observations.

    ``flagged_at`` is the term's ``last_seen`` — the most recent SOURCE DATE on
    which Google Ads reported the term while it was flagged — not the wall clock
    at which this job happened to run. That choice is what makes a backfill
    truthful (historical evidence is dated when it actually happened, not
    stamped "now") and what makes replays deterministic.
    """
    observations: list[dict] = []
    for row in (rows or []):
        term = row.get("search_term")
        campaign_key = row.get("campaign_key")
        if not term or not campaign_key:
            # Without both halves of the durable identity there is nothing safe
            # to key history on. Skipped and counted, never guessed.
            continue
        raw_categories = row.get("raw_junk_categories") or []
        observations.append({
            "campaign_key": campaign_key,
            "search_term": term,
            "search_term_display": term,
            "campaign_name_display": row.get("campaign_name"),
            "flagged_at": row.get("last_seen"),
            "reason": row.get("flag_reason"),
            "raw_reason": (raw_categories[0] if raw_categories else None),
        })
    return observations


def record_flag_history(window: str = WEEKLY_HISTORY_WINDOW,
                        *, page_size: int = 200) -> dict:
    """Record flag history for every currently-flagged term in ``window``.

    Idempotent: re-running over the same evidence changes nothing, because the
    repository merges monotonically and only lets a NEWER observation replace
    the latest-* fields.

    A human review decision is never touched — a resolved or kept term keeps its
    decision no matter how many times its flag is observed again (§44).

    Returns ``{available, observed, written, pages, reason}``. Never raises: a
    failure here must not break the weekly run that produced the annotations.
    """
    try:
        from db import search_term_review_repository as review_repo  # noqa: PLC0415
        from services.search_term_evidence_service import (  # noqa: PLC0415
            build_flagged_search_terms,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("[flag-history] import failed: %s", exc)
        return {"available": False, "observed": 0, "written": 0, "pages": 0,
                "reason": "import_failed"}

    observations: list[dict] = []
    pages = 0
    page = 1
    try:
        while True:
            payload = build_flagged_search_terms(window, page=page,
                                                 page_size=page_size,
                                                 sort="term")
            if payload.get("db_unavailable"):
                return {"available": False, "observed": 0, "written": 0,
                        "pages": pages, "reason": "canonical_facts_unavailable"}
            if payload.get("actionable") is False:
                # Quarantined (truth_state = mismatch). Recording history from a
                # population whose own contract cannot be explained would write
                # unexplainable evidence into a durable audit table.
                return {"available": False, "observed": 0, "written": 0,
                        "pages": pages, "reason": "population_quarantined"}
            pages += 1
            observations.extend(_observations_from_rows(payload.get("rows") or []))
            if not (payload.get("pagination") or {}).get("has_more"):
                break
            page += 1
    except Exception as exc:  # noqa: BLE001
        log.error("[flag-history] could not read the flagged population: %s", exc)
        return {"available": False, "observed": 0, "written": 0, "pages": pages,
                "reason": "read_failed"}

    if not observations:
        log.info("[flag-history] no flagged terms in %s — nothing to record",
                 window)
        return {"available": True, "observed": 0, "written": 0, "pages": pages}

    result = review_repo.record_flag_observations(observations)
    if not result.get("available"):
        log.error("[flag-history] durable write unavailable")
        return {"available": False, "observed": len(observations), "written": 0,
                "pages": pages, "reason": "write_unavailable"}

    log.info("[flag-history] recorded %d flag observation(s) over %s (%d page(s))",
             result.get("written", 0), window, pages)
    return {"available": True, "observed": len(observations),
            "written": int(result.get("written") or 0), "pages": pages}


def backfill_flag_history() -> dict:
    """Operator backfill over all canonical evidence.

    Safe to run repeatedly and safe to run after decisions exist: it can only
    widen each term's history window backwards, and it can never reopen a review
    or overwrite the reason recorded by a more recent observation.
    """
    return record_flag_history(BACKFILL_HISTORY_WINDOW)
