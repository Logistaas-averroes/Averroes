"""
db/deal_ledger_repository.py

PR-ADS-153E-A — durable persistence for the canonical deal ledger.

Owns three tables and nothing else:
  * ``hubspot_deal_ledger``                 — one row per deal_id
  * ``hubspot_deal_contact_association``    — every deal→contact association
  * ``hubspot_deal_sync_state``             — watermark / coverage / failures

Guarantees
----------
**Idempotent by ``deal_id``.** Re-processing the same deal updates one row. A
changed campaign label, source classification or GCLID updates that row — it can
never mint a duplicate deal, which is exactly how `gclid_attribution` (keyed on a
SHA1 attribution hash) accumulated several rows per deal.

**Monotonic.** An older replay cannot overwrite newer HubSpot state. The guard is
``hubspot_lastmodified_at``: a write whose source is older than what is stored is
ignored. Out-of-order delivery is normal in a resumable backfill running beside
an incremental sync, and without this guard a backfill would quietly revert a
deal to a stale stage.

**Coverage state is declared, not inferred** (PR-ADS-153E-A2). Every write names
its ``sync_mode``; a bootstrap and an incremental touch different columns, and
only a run that proved it read to the END of the result set may complete the
bootstrap. See ``record_sync_state``.

**Association evidence is never destroyed by a failure.** Associations are
replaced only inside a transaction that observed them successfully. A failed
lookup writes no associations at all and leaves the previous successful
observation standing — on the bridge AND on the ledger row, whose
association-derived columns are excluded from the update entirely. Only the
lookup OUTCOME columns move, so the row reports honestly that this attempt
learned nothing without pretending the deal suddenly has no campaign or GCLID.

No external API calls. No writes to HubSpot, Google Ads or Mailchimp.
"""

from __future__ import annotations

import logging
from datetime import datetime

from db.connection import get_conn

log = logging.getLogger(__name__)

LEDGER_TABLE = "hubspot_deal_ledger"
ASSOCIATION_TABLE = "hubspot_deal_contact_association"
SYNC_STATE_TABLE = "hubspot_deal_sync_state"
SYNC_SCOPE = "deals"

# ── Sync mode (PR-ADS-153E-A2) ───────────────────────────────────────────────
# The mode is DECLARED by the caller, never inferred. 153E-A inferred "this was
# a bootstrap" from `full_refresh and complete`, which meant an ordinary
# incremental run could leave `bootstrap_status = not_started` while reporting
# `last_status = success` — a state the cutover gate then read as healthy.
SYNC_MODE_BOOTSTRAP = "bootstrap"
SYNC_MODE_INCREMENTAL = "incremental"
ALL_SYNC_MODES = (SYNC_MODE_BOOTSTRAP, SYNC_MODE_INCREMENTAL)

# ── Bootstrap coverage states ────────────────────────────────────────────────
BOOTSTRAP_NOT_STARTED = "not_started"
BOOTSTRAP_IN_PROGRESS = "in_progress"
BOOTSTRAP_COMPLETE = "complete"

_LEDGER_COLUMNS = (
    "deal_id", "deal_name", "pipeline_id", "deal_stage_id", "deal_stage_label",
    "hs_is_closed", "hs_is_closed_won",
    "deal_created_at", "deal_close_date", "hubspot_lastmodified_at",
    "amount_raw", "deal_currency_code", "amount_in_home_currency",
    "home_currency_code", "revenue_usd", "currency_status", "currency_reason",
    "primary_contact_id", "association_count", "association_status",
    "association_reason",
    "gclid", "campaign_name_raw", "keyword_raw", "country_raw",
    "source_primary_raw", "source_detail_raw", "acquisition_group",
    "attribution_status", "attribution_reason",
    "sync_batch_id", "source_fetched_at", "created_at", "updated_at",
)

# Columns whose value is DERIVED FROM the deal→contact association lookup. When
# that lookup fails the sync service has no evidence for any of them and passes
# NULLs; writing those NULLs would erase a GCLID, campaign or country we had
# already proven and silently move revenue between sources on the next read.
# They are therefore dropped from the ON CONFLICT update when the lookup was not
# observed, exactly as the association bridge is left untouched.
#
# The failed ATTEMPT is not lost by preserving them: it is counted in
# ``hubspot_deal_sync_state.association_failures`` and surfaced by the audit. A
# deal with no previous row still records `lookup_failed` / `unavailable`,
# because on INSERT there is nothing to preserve.
_ASSOCIATION_DERIVED_COLUMNS = (
    "primary_contact_id", "association_count",
    "association_status", "association_reason",
    "gclid", "campaign_name_raw", "keyword_raw", "country_raw",
    "source_primary_raw", "source_detail_raw", "acquisition_group",
    "attribution_status", "attribution_reason",
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
    for field in ("deal_created_at", "deal_close_date", "hubspot_lastmodified_at",
                  "source_fetched_at", "created_at", "updated_at",
                  "last_observed_at",
                  # Sync-state timestamps — normalised here too so every value
                  # this repository returns is a consistent ISO string.
                  "last_modified_watermark", "last_incremental_at",
                  "bootstrap_started_at", "bootstrap_completed_at"):
        if field in row:
            row[field] = _iso(row[field])
    for field in ("amount_raw", "amount_in_home_currency", "revenue_usd"):
        if row.get(field) is not None:
            row[field] = float(row[field])
    return row


def _unavailable(**extra) -> dict:
    payload = {"available": False, "reason": "database_unavailable"}
    payload.update(extra)
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Writes (local PostgreSQL only)
# ─────────────────────────────────────────────────────────────────────────────
def upsert_deal(deal: dict, *, associations: list | None = None,
                associations_observed: bool = True) -> dict:
    """Write ONE canonical deal, plus its associations when observed.

    Args:
        deal: a normalized ledger row (see ``_LEDGER_COLUMNS``).
        associations: every observed deal→contact association.
        associations_observed: False when the association lookup FAILED. The
            bridge is then left completely untouched, and the ledger row's
            association-derived columns (``_ASSOCIATION_DERIVED_COLUMNS``) are
            excluded from the update, preserving the last successful evidence.
            Deal facts read successfully in the same run — stage, amount,
            currency, last-modified — still update.

    Monotonic: the update is skipped when the incoming
    ``hubspot_lastmodified_at`` is older than the stored one, OR when it is
    unknown and the stored one is known. An unknown timestamp is not evidence of
    recency, so it may only write a NEW row (or one whose stored timestamp is
    itself unknown).

    Atomic: the association bridge is replaced ONLY when the ledger update was
    actually applied. A stale replay therefore leaves the row AND the bridge
    untouched — replacing associations from an observation the ledger just
    rejected as old would reintroduce exactly the out-of-order corruption the
    guard exists to prevent.

    Returns ``{available, written, skipped_stale, error}``. Never raises — but
    ``available: False`` is a FAILURE the caller must propagate, not a quiet
    zero-row success.
    """
    deal_id = deal.get("deal_id")
    if not deal_id:
        return {"available": False, "reason": "missing_deal_id", "written": 0,
                "skipped_stale": 0, "error": "missing_deal_id"}

    values = [deal.get(col) for col in _LEDGER_COLUMNS
              if col not in ("created_at", "updated_at")]
    insert_cols = [c for c in _LEDGER_COLUMNS if c not in ("created_at", "updated_at")]
    placeholders = ", ".join(["%s"] * len(insert_cols))
    updatable = [c for c in insert_cols if c != "deal_id"]
    if not associations_observed:
        # The lookup failed: we observed none of this evidence, so we do not get
        # to overwrite it. (On INSERT the NULLs are still written — nothing prior
        # existed to preserve.)
        updatable = [c for c in updatable
                     if c not in _ASSOCIATION_DERIVED_COLUMNS]
    set_clause = ",\n                            ".join(
        f"{c} = EXCLUDED.{c}" for c in updatable)

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(written=0, skipped_stale=0)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {LEDGER_TABLE} ({", ".join(insert_cols)},
                                                created_at, updated_at)
                    VALUES ({placeholders}, NOW(), NOW())
                    ON CONFLICT (deal_id) DO UPDATE SET
                            {set_clause},
                            updated_at = NOW()
                    -- MONOTONIC GUARD. An older replay (a backfill running
                    -- beside an incremental sync, a retried page) must not
                    -- revert a deal to a stale stage or amount.
                    --
                    -- A stored NULL means we never knew the deal's modification
                    -- time, so anything is at least as good — apply.
                    -- An INCOMING NULL, however, is NOT permission to overwrite
                    -- a known timestamp: "we don't know when this was modified"
                    -- cannot outrank "we know it was modified on Tuesday".
                    WHERE {LEDGER_TABLE}.hubspot_lastmodified_at IS NULL
                       OR (EXCLUDED.hubspot_lastmodified_at IS NOT NULL
                           AND EXCLUDED.hubspot_lastmodified_at
                               >= {LEDGER_TABLE}.hubspot_lastmodified_at)
                    RETURNING deal_id
                    """,
                    tuple(values),
                )
                applied = cur.fetchone() is not None

                # Associations follow the ledger row. If the row was rejected as
                # stale, this observation is stale too and must not replace the
                # bridge.
                if applied and associations_observed:
                    _replace_associations(cur, str(deal_id), associations or [],
                                          deal.get("sync_batch_id"))
            conn.commit()
        return {"available": True, "written": 1 if applied else 0,
                "skipped_stale": 0 if applied else 1, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_deal failed for %s: %s", deal_id, exc)
        return _unavailable(written=0, skipped_stale=0, error=str(exc))


def _replace_associations(cur, deal_id: str, associations: list,
                          batch_id) -> None:
    """Replace a deal's associations INSIDE the caller's transaction.

    Only ever reached when the lookup succeeded, so "no associations" is a fact
    we observed rather than a lookup we failed to make.
    """
    observed_ids = [str(a.get("contact_id")) for a in associations
                    if a.get("contact_id") not in (None, "")]

    # Drop associations HubSpot no longer reports for this deal. Safe because
    # this runs only on a successful observation.
    if observed_ids:
        cur.execute(
            f"DELETE FROM {ASSOCIATION_TABLE} "
            f"WHERE deal_id = %s AND NOT (contact_id = ANY(%s))",
            (deal_id, observed_ids))
    else:
        cur.execute(f"DELETE FROM {ASSOCIATION_TABLE} WHERE deal_id = %s",
                    (deal_id,))

    for assoc in associations:
        contact_id = assoc.get("contact_id")
        if contact_id in (None, ""):
            continue
        cur.execute(
            f"""
            INSERT INTO {ASSOCIATION_TABLE} (
                deal_id, contact_id, association_type_id, association_label,
                is_primary, primary_selection_reason,
                gclid, campaign_name_raw, keyword_raw, country_raw,
                source_primary_raw, source_detail_raw, acquisition_group,
                last_observed_batch_id, last_observed_at,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, NOW(), NOW(), NOW())
            ON CONFLICT (deal_id, contact_id) DO UPDATE SET
                association_type_id      = EXCLUDED.association_type_id,
                association_label        = EXCLUDED.association_label,
                is_primary               = EXCLUDED.is_primary,
                primary_selection_reason = EXCLUDED.primary_selection_reason,
                gclid                    = EXCLUDED.gclid,
                campaign_name_raw        = EXCLUDED.campaign_name_raw,
                keyword_raw              = EXCLUDED.keyword_raw,
                country_raw              = EXCLUDED.country_raw,
                source_primary_raw       = EXCLUDED.source_primary_raw,
                source_detail_raw        = EXCLUDED.source_detail_raw,
                acquisition_group        = EXCLUDED.acquisition_group,
                last_observed_batch_id   = EXCLUDED.last_observed_batch_id,
                last_observed_at         = NOW(),
                updated_at               = NOW()
            """,
            (deal_id, str(contact_id), assoc.get("association_type_id"),
             assoc.get("association_label"), bool(assoc.get("is_primary")),
             assoc.get("primary_selection_reason"),
             assoc.get("gclid"), assoc.get("campaign_name_raw"),
             assoc.get("keyword_raw"), assoc.get("country_raw"),
             assoc.get("source_primary_raw"), assoc.get("source_detail_raw"),
             assoc.get("acquisition_group"), batch_id),
        )


def record_sync_state(*, status: str, sync_mode: str, watermark=None,
                      deals_seen: int = 0, pages_fetched: int = 0,
                      association_failures: int = 0, error: str | None = None,
                      batch_id=None, watermark_is_checkpoint: bool = False,
                      proved_complete: bool = False) -> dict:
    """Record the outcome of a sync attempt.

    Args:
        sync_mode: ``bootstrap`` or ``incremental``. DECLARED, never inferred —
            the two modes write different columns, and guessing is what let an
            ordinary incremental run report success over a bootstrap that had
            never happened.
        proved_complete: True only when the connector proved it reached the END
            of the result set. A bootstrap may be marked complete on no other
            basis: "we stopped and nothing went wrong" is not proof that there
            was nothing left to read.

    Column semantics
    ----------------
    ================= ============================ ==========================
    Column            bootstrap run                incremental run
    ================= ============================ ==========================
    bootstrap_status  in_progress → complete       untouched
    bootstrap_started COALESCE(existing, NOW())    untouched
    bootstrap_compl.  set once, when proven        untouched
    last_incremental  UNTOUCHED                    NOW()
    ================= ============================ ==========================

    ``last_incremental_at`` is the load-bearing one. 153E-A stamped it on every
    run including bootstraps, so it could not answer the question the cutover
    gate actually asks: *did a normal incremental sync succeed AFTER the
    historical bootstrap finished?*

    Two monotonic guarantees:

    * a completed bootstrap is never downgraded to ``in_progress`` or
      ``not_started`` — coverage once proven stays proven;
    * ``bootstrap_completed_at`` keeps its FIRST value. Re-running the backfill
      (resume or ``--restart``) re-proves the same coverage; restamping it would
      invalidate the "incremental after bootstrap" ordering and silently revoke
      a passing gate until the next daily sync.

    The watermark advances on a fully successful sync, or — when
    ``watermark_is_checkpoint`` — to the end of a CLEANLY PROCESSED PREFIX of a
    capped run. Deals are read in ascending ``hs_lastmodifieddate`` order, so a
    checkpoint at the last fully committed deal skips nothing: it is the
    difference between a capped backfill that resumes and one that reprocesses
    its first page forever.

    A failed run, or a partial run with no clean prefix, leaves the watermark
    where it was.

    Returns ``{available, error}``; ``available: False`` is a FAILURE the caller
    must propagate rather than treat as a recorded success.
    """
    if sync_mode not in ALL_SYNC_MODES:
        return _unavailable(reason="invalid_sync_mode",
                            error=f"unknown sync_mode {sync_mode!r}")

    is_bootstrap = sync_mode == SYNC_MODE_BOOTSTRAP
    # Only a run that both SUCCEEDED and proved it read to the end may complete
    # the bootstrap.
    completes_bootstrap = bool(is_bootstrap and proved_complete
                               and status == "success")
    advance = watermark is not None and (
        status == "success"
        or (status == "partial" and watermark_is_checkpoint))

    params = {
        "scope": SYNC_SCOPE,
        "is_bootstrap": is_bootstrap,
        "completes": completes_bootstrap,
        "watermark": watermark if advance else None,
        "advance": advance,
        "status": status,
        "error": error,
        "deals_seen": int(deals_seen),
        "pages": int(pages_fetched),
        "assoc_failures": int(association_failures),
        "batch_id": batch_id,
        "in_progress": BOOTSTRAP_IN_PROGRESS,
        "complete": BOOTSTRAP_COMPLETE,
        "not_started": BOOTSTRAP_NOT_STARTED,
    }

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable()
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {SYNC_STATE_TABLE} (
                        scope, bootstrap_status,
                        bootstrap_started_at, bootstrap_completed_at,
                        last_modified_watermark, last_incremental_at,
                        last_status, last_error,
                        deals_seen, pages_fetched, association_failures,
                        last_batch_id, updated_at
                    ) VALUES (
                        %(scope)s,
                        CASE WHEN %(is_bootstrap)s
                             THEN CASE WHEN %(completes)s THEN %(complete)s
                                       ELSE %(in_progress)s END
                             ELSE %(not_started)s END,
                        CASE WHEN %(is_bootstrap)s THEN NOW() ELSE NULL END,
                        CASE WHEN %(completes)s THEN NOW() ELSE NULL END,
                        %(watermark)s,
                        CASE WHEN %(is_bootstrap)s THEN NULL ELSE NOW() END,
                        %(status)s, %(error)s,
                        %(deals_seen)s, %(pages)s, %(assoc_failures)s,
                        %(batch_id)s, NOW())
                    ON CONFLICT (scope) DO UPDATE SET
                        -- Never downgraded: coverage once proven stays proven.
                        bootstrap_status = CASE
                            WHEN NOT %(is_bootstrap)s
                                THEN {SYNC_STATE_TABLE}.bootstrap_status
                            WHEN {SYNC_STATE_TABLE}.bootstrap_status = %(complete)s
                                THEN %(complete)s
                            WHEN %(completes)s THEN %(complete)s
                            ELSE %(in_progress)s END,
                        -- The FIRST attempt's start time survives every retry.
                        bootstrap_started_at = CASE
                            WHEN NOT %(is_bootstrap)s
                                THEN {SYNC_STATE_TABLE}.bootstrap_started_at
                            ELSE COALESCE(
                                {SYNC_STATE_TABLE}.bootstrap_started_at, NOW())
                            END,
                        bootstrap_completed_at = CASE
                            WHEN %(completes)s THEN COALESCE(
                                {SYNC_STATE_TABLE}.bootstrap_completed_at, NOW())
                            ELSE {SYNC_STATE_TABLE}.bootstrap_completed_at END,
                        last_modified_watermark = CASE WHEN %(advance)s
                            THEN EXCLUDED.last_modified_watermark
                            ELSE {SYNC_STATE_TABLE}.last_modified_watermark END,
                        -- A bootstrap is NOT an incremental. Stamping this here
                        -- is what made "an incremental succeeded after the
                        -- bootstrap" unprovable.
                        last_incremental_at = CASE WHEN %(is_bootstrap)s
                            THEN {SYNC_STATE_TABLE}.last_incremental_at
                            ELSE NOW() END,
                        last_status          = EXCLUDED.last_status,
                        last_error           = EXCLUDED.last_error,
                        deals_seen           = EXCLUDED.deals_seen,
                        pages_fetched        = EXCLUDED.pages_fetched,
                        association_failures = EXCLUDED.association_failures,
                        last_batch_id        = COALESCE(
                            EXCLUDED.last_batch_id,
                            {SYNC_STATE_TABLE}.last_batch_id),
                        updated_at           = NOW()
                    """,
                    params,
                )
            conn.commit()
        return {"available": True, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.error("record_sync_state failed: %s", exc)
        return _unavailable(error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Reads (reconciliation / audit only — no production consumer in 153E-A)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_sync_state() -> dict:
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(row=None)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT scope, bootstrap_status, bootstrap_started_at, "
                    f"bootstrap_completed_at, last_modified_watermark, "
                    f"last_incremental_at, last_status, last_error, deals_seen, "
                    f"pages_fetched, association_failures, updated_at "
                    f"FROM {SYNC_STATE_TABLE} WHERE scope = %s", (SYNC_SCOPE,))
                rows = [_normalise(r) for r in _rows_as_dicts(cur)]
        return {"available": True, "row": rows[0] if rows else None}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_sync_state failed: %s", exc)
        return _unavailable(row=None)


def fetch_deal(deal_id: str) -> dict:
    """One ledger row, for tests and deal-grain reconciliation."""
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(row=None)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(_LEDGER_COLUMNS)} FROM {LEDGER_TABLE} "
                    f"WHERE deal_id = %s", (str(deal_id),))
                rows = [_normalise(r) for r in _rows_as_dicts(cur)]
        return {"available": True, "row": rows[0] if rows else None}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_deal failed: %s", exc)
        return _unavailable(row=None)


def fetch_associations(deal_id: str) -> dict:
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[])
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT deal_id, contact_id, association_type_id, "
                    f"association_label, is_primary, primary_selection_reason, "
                    f"gclid, campaign_name_raw, keyword_raw, country_raw, "
                    f"source_primary_raw, source_detail_raw, acquisition_group, "
                    f"last_observed_at "
                    f"FROM {ASSOCIATION_TABLE} WHERE deal_id = %s "
                    f"ORDER BY contact_id", (str(deal_id),))
                rows = [_normalise(r) for r in _rows_as_dicts(cur)]
        return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_associations failed: %s", exc)
        return _unavailable(rows=[])


def fetch_ledger_summary(start=None, end=None) -> dict:
    """Aggregate ledger facts for a window.

    ``end`` is EXCLUSIVE, matching ``analysis.business_windows.get_window_bounds``
    — an inclusive comparison here would pull in the first instant of the
    following day and silently overlap adjacent quarters.

    ``won_*`` counts use ``hs_is_closed_won IS TRUE`` — never a stage label.
    ``revenue_usd`` is summed only over rows whose currency was PROVEN, and the
    rows that were excluded are reported alongside so the total is never mistaken
    for complete.
    """
    from analysis.deal_currency import SUMMABLE_CURRENCY_STATUSES

    summable = list(SUMMABLE_CURRENCY_STATUSES)
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(summary={})
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      COUNT(*)                                       AS total_deals,
                      COUNT(DISTINCT deal_id)                        AS distinct_deals,
                      COUNT(*) FILTER (WHERE hs_is_closed_won IS TRUE)  AS won_deals,
                      COUNT(*) FILTER (WHERE hs_is_closed_won IS NULL)  AS unknown_won_deals,
                      COUNT(*) FILTER (WHERE hs_is_closed_won IS TRUE
                                         AND gclid IS NOT NULL)      AS won_with_gclid,
                      COUNT(*) FILTER (WHERE hs_is_closed_won IS TRUE
                                         AND gclid IS NULL)          AS won_without_gclid,
                      COUNT(*) FILTER (WHERE hs_is_closed_won IS TRUE
                                         AND deal_close_date IS NULL) AS won_without_close_date,
                      COUNT(*) FILTER (WHERE hs_is_closed_won IS TRUE
                                         AND currency_status = ANY(%s)) AS won_currency_proven,
                      COUNT(*) FILTER (WHERE hs_is_closed_won IS TRUE
                                         AND (currency_status IS NULL
                                              OR NOT (currency_status = ANY(%s))))
                                                                     AS won_currency_unavailable,
                      SUM(revenue_usd) FILTER (WHERE hs_is_closed_won IS TRUE
                                                 AND currency_status = ANY(%s))
                                                                     AS revenue_usd,
                      SUM(amount_raw)  FILTER (WHERE hs_is_closed_won IS TRUE)
                                                                     AS amount_raw_total,
                      COUNT(*) FILTER (WHERE association_status = 'ambiguous') AS ambiguous_assoc,
                      COUNT(*) FILTER (WHERE association_status = 'lookup_failed')
                                                                     AS failed_assoc,
                      COUNT(*) FILTER (WHERE deal_stage_label LIKE 'Unknown stage%%')
                                                                     AS unknown_stage
                    FROM {LEDGER_TABLE}
                    WHERE (%s::timestamptz IS NULL OR deal_close_date >= %s)
                      AND (%s::timestamptz IS NULL OR deal_close_date < %s)
                    """,
                    (summable, summable, summable, start, start, end, end),
                )
                cols = [d[0] for d in cur.description]
                summary = dict(zip(cols, cur.fetchone()))
        for key in ("revenue_usd", "amount_raw_total"):
            if summary.get(key) is not None:
                summary[key] = float(summary[key])
        return {"available": True, "summary": summary}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_ledger_summary failed: %s", exc)
        return _unavailable(summary={})


def fetch_ledger_rows(start=None, end=None, *, won_only: bool = False,
                      limit: int = 100000) -> dict:
    """Deal-grain rows for reconciliation. Carries no contact PII.

    ``end`` is EXCLUSIVE (see ``fetch_ledger_summary``).
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[])
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT deal_id, deal_stage_id, deal_stage_label,
                           hs_is_closed, hs_is_closed_won, deal_close_date,
                           amount_raw, deal_currency_code, revenue_usd,
                           currency_status, currency_reason,
                           gclid, campaign_name_raw, country_raw,
                           acquisition_group, attribution_status,
                           association_status, association_count
                    FROM {LEDGER_TABLE}
                    WHERE (%s::timestamptz IS NULL OR deal_close_date >= %s)
                      AND (%s::timestamptz IS NULL OR deal_close_date < %s)
                      AND (NOT %s OR hs_is_closed_won IS TRUE)
                    ORDER BY deal_id
                    LIMIT %s
                    """,
                    (start, start, end, end, won_only, int(limit)),
                )
                rows = [_normalise(r) for r in _rows_as_dicts(cur)]
        return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_ledger_rows failed: %s", exc)
        return _unavailable(rows=[])


def fetch_deal_states(deal_ids) -> dict:
    """Canonical IDENTITY for a specific set of deals, across every state.

    Unbounded by window and by won-state on purpose. It answers one question the
    windowed won-population read cannot: does the canonical ledger hold this deal
    AT ALL? Without it, a deal the canonical sync genuinely missed and a deal it
    holds but classifies differently are indistinguishable — and only the first
    means the sync is incomplete.
    """
    ids = sorted({str(d) for d in (deal_ids or []) if d not in (None, "")})
    if not ids:
        return {"available": True, "rows": {}}
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows={})
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT deal_id, hs_is_closed, hs_is_closed_won,
                           deal_close_date, deal_stage_id, deal_stage_label,
                           revenue_usd, currency_status, currency_reason
                    FROM {LEDGER_TABLE}
                    WHERE deal_id = ANY(%s)
                    """,
                    (ids,),
                )
                rows = [_normalise(r) for r in _rows_as_dicts(cur)]
        return {"available": True,
                "rows": {str(r["deal_id"]): r for r in rows}}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_deal_states failed: %s", exc)
        return _unavailable(rows={})


def fetch_stage_breakdown() -> dict:
    """Deals per stage — proves open/lost/downgrade/churn are actually stored."""
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(rows=[])
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT deal_stage_id, deal_stage_label, hs_is_closed, "
                    f"hs_is_closed_won, COUNT(*) AS deals "
                    f"FROM {LEDGER_TABLE} GROUP BY 1,2,3,4 ORDER BY 5 DESC")
                rows = _rows_as_dicts(cur)
        return {"available": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_stage_breakdown failed: %s", exc)
        return _unavailable(rows=[])
