"""
db/search_term_review_repository.py

PR-ADS-153D — durable LOCAL review decisions for canonical search terms.

This repository owns exactly one table, ``search_term_review``, which is a
DECISION/ANNOTATION layer keyed by the canonical durable search-term identity
(``analysis/search_term_identity.py``).

What it is NOT
--------------
It is not a Google Ads fact ledger. It stores no spend, clicks or impressions,
and nothing here may ever be summed into a metric. Canonical Google Ads metrics
come from ``search_terms`` and only from there (PR-ADS-153D §23).

It is also not a Google Ads write path. ``exclude_candidate`` records that a
human recommended an exclusion; it is never evidence that a negative keyword was
applied (§16).

Availability is explicit: a database outage returns ``available: False`` — never
an empty result a caller could mistake for "nobody has reviewed anything"
(unavailable ≠ zero).
"""

from __future__ import annotations

import logging
from datetime import datetime

from analysis.search_term_identity import identity_components
from analysis.search_term_review_state import (
    STATE_UNREVIEWED,
    normalize_review_state,
)
from db.connection import get_conn

log = logging.getLogger(__name__)

REVIEW_TABLE = "search_term_review"

_REVIEW_COLUMNS = (
    "term_identity", "campaign_key", "search_term_normalized",
    "search_term_display", "campaign_name_display", "identity_rule_version",
    "review_state", "review_note", "reviewed_by", "reviewed_at",
    "first_flagged_at", "latest_flagged_at",
    "latest_flag_reason", "latest_raw_reason",
    "created_at", "updated_at",
)


def _rows_as_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalise(row: dict) -> dict:
    for field in ("reviewed_at", "first_flagged_at", "latest_flagged_at",
                  "created_at", "updated_at"):
        if field in row:
            row[field] = _iso(row[field])
    row["review_state"] = normalize_review_state(row.get("review_state"))
    return row


def _unavailable(**extra) -> dict:
    payload = {"available": False, "reason": "database_unavailable"}
    payload.update(extra)
    return payload


def fetch_reviews_for_identities(term_identities: list) -> dict:
    """Durable review rows for a batch of durable identities, keyed by identity.

    Absence of a row means "no human decision recorded", which the caller
    represents as ``unreviewed`` — it is never invented as ``keep``.
    """
    ids = [str(i) for i in (term_identities or []) if i]
    if not ids:
        return {"available": True, "rows": {}}
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows={})
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {", ".join(_REVIEW_COLUMNS)}
                    FROM {REVIEW_TABLE}
                    WHERE term_identity = ANY(%s)
                    """,
                    (ids,),
                )
                rows = [_normalise(r) for r in _rows_as_dicts(cur)]
        return {"available": True,
                "rows": {r["term_identity"]: r for r in rows}}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_reviews_for_identities failed: %s", exc)
        return _unavailable(rows={})


def fetch_review(term_identity: str) -> dict:
    """One durable review row, or ``row: None`` when nobody has decided yet."""
    fetched = fetch_reviews_for_identities([term_identity])
    if not fetched.get("available"):
        return _unavailable(row=None)
    return {"available": True,
            "row": (fetched.get("rows") or {}).get(str(term_identity))}


def upsert_review_decision(
    *,
    campaign_key,
    search_term,
    review_state: str,
    search_term_display=None,
    campaign_name_display=None,
    review_note=None,
    reviewed_by=None,
) -> dict:
    """Record ONE human review decision for a durable search-term identity.

    The decision columns are the only ones this write touches. Flag history
    (``first_flagged_at`` / ``latest_flagged_at`` / reason) is owned by the
    flagged-view writer and is deliberately NOT cleared here: a term that a human
    resolves stays auditable as historically flagged (§25).

    Returns ``{available, row}``. Never raises.
    """
    state = normalize_review_state(review_state)
    identity = identity_components(campaign_key, search_term)
    if not identity["search_term_normalized"]:
        return {"available": False, "reason": "empty_search_term", "row": None}

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(row=None)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {REVIEW_TABLE} (
                        term_identity, campaign_key, search_term_normalized,
                        search_term_display, campaign_name_display,
                        identity_rule_version,
                        review_state, review_note, reviewed_by, reviewed_at,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(),
                              NOW(), NOW())
                    ON CONFLICT (term_identity) DO UPDATE SET
                        search_term_display   = COALESCE(
                            EXCLUDED.search_term_display,
                            {REVIEW_TABLE}.search_term_display),
                        campaign_name_display = COALESCE(
                            EXCLUDED.campaign_name_display,
                            {REVIEW_TABLE}.campaign_name_display),
                        review_state = EXCLUDED.review_state,
                        review_note  = EXCLUDED.review_note,
                        reviewed_by  = EXCLUDED.reviewed_by,
                        reviewed_at  = NOW(),
                        updated_at   = NOW()
                    RETURNING {", ".join(_REVIEW_COLUMNS)}
                    """,
                    (identity["term_identity"], identity["campaign_key"],
                     identity["search_term_normalized"],
                     search_term_display if search_term_display is not None
                     else (str(search_term) if search_term is not None else None),
                     campaign_name_display,
                     identity["identity_rule_version"],
                     state, review_note, reviewed_by),
                )
                rows = [_normalise(r) for r in _rows_as_dicts(cur)]
            conn.commit()
        return {"available": True, "row": rows[0] if rows else None}
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_review_decision failed: %s", exc)
        return _unavailable(row=None)


def record_flag_observations(observations: list) -> dict:
    """Append-only flag HISTORY for currently-flagged durable identities.

    Each observation is ``{campaign_key, search_term, flagged_at, reason,
    raw_reason, search_term_display, campaign_name_display}``.

    Idempotent and monotonic:
      * ``first_flagged_at`` only ever moves EARLIER (LEAST);
      * ``latest_flagged_at`` only ever moves LATER (GREATEST);
      * ``review_state`` is never touched — observing a flag again must not
        reopen a decision a human already made (§44).

    Re-running the same observation therefore changes nothing, which is what
    stops repeated sync runs from rewriting history.
    """
    prepared = []
    for obs in (observations or []):
        identity = identity_components(obs.get("campaign_key"),
                                       obs.get("search_term"))
        if not identity["search_term_normalized"]:
            continue
        prepared.append((
            identity["term_identity"], identity["campaign_key"],
            identity["search_term_normalized"],
            obs.get("search_term_display") or obs.get("search_term"),
            obs.get("campaign_name_display"),
            identity["identity_rule_version"],
            obs.get("flagged_at"), obs.get("reason"), obs.get("raw_reason"),
        ))
    if not prepared:
        return {"available": True, "written": 0}

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(written=0)
            with conn.cursor() as cur:
                cur.executemany(
                    f"""
                    INSERT INTO {REVIEW_TABLE} (
                        term_identity, campaign_key, search_term_normalized,
                        search_term_display, campaign_name_display,
                        identity_rule_version,
                        review_state,
                        first_flagged_at, latest_flagged_at,
                        latest_flag_reason, latest_raw_reason,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, '{STATE_UNREVIEWED}',
                              %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (term_identity) DO UPDATE SET
                        search_term_display   = COALESCE(
                            EXCLUDED.search_term_display,
                            {REVIEW_TABLE}.search_term_display),
                        campaign_name_display = COALESCE(
                            EXCLUDED.campaign_name_display,
                            {REVIEW_TABLE}.campaign_name_display),
                        first_flagged_at = LEAST(
                            COALESCE({REVIEW_TABLE}.first_flagged_at,
                                     EXCLUDED.first_flagged_at),
                            COALESCE(EXCLUDED.first_flagged_at,
                                     {REVIEW_TABLE}.first_flagged_at)),
                        latest_flagged_at = GREATEST(
                            COALESCE({REVIEW_TABLE}.latest_flagged_at,
                                     EXCLUDED.latest_flagged_at),
                            COALESCE(EXCLUDED.latest_flagged_at,
                                     {REVIEW_TABLE}.latest_flagged_at)),
                        latest_flag_reason = COALESCE(
                            EXCLUDED.latest_flag_reason,
                            {REVIEW_TABLE}.latest_flag_reason),
                        latest_raw_reason  = COALESCE(
                            EXCLUDED.latest_raw_reason,
                            {REVIEW_TABLE}.latest_raw_reason),
                        updated_at = NOW()
                    """,
                    prepared,
                )
            conn.commit()
        return {"available": True, "written": len(prepared)}
    except Exception as exc:  # noqa: BLE001
        log.error("record_flag_observations failed: %s", exc)
        return _unavailable(written=0)


def fetch_review_state_counts() -> dict:
    """Counts by review state across every durable identity ever recorded."""
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(counts={})
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT review_state, COUNT(*) FROM {REVIEW_TABLE} "
                    f"GROUP BY 1")
                counts = {normalize_review_state(r[0]): int(r[1])
                          for r in cur.fetchall()}
        return {"available": True, "counts": counts}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_review_state_counts failed: %s", exc)
        return _unavailable(counts={})


def fetch_historically_flagged(limit: int = 500) -> dict:
    """Durable identities that carry flag history, currently flagged or not.

    Powers the "historically flagged" audit view: a term whose current evidence
    no longer meets the rule keeps its history here (§25).
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[])
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {", ".join(_REVIEW_COLUMNS)}
                    FROM {REVIEW_TABLE}
                    WHERE latest_flagged_at IS NOT NULL
                    ORDER BY latest_flagged_at DESC, term_identity ASC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                rows = [_normalise(r) for r in _rows_as_dicts(cur)]
        return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_historically_flagged failed: %s", exc)
        return _unavailable(rows=[])
