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

**Association evidence is never destroyed by a failure.** Associations are
replaced only inside a transaction that observed them successfully. A failed
lookup writes no associations at all and leaves the previous successful
observation standing.

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
            bridge is then left completely untouched, preserving the last
            successful evidence.

    Monotonic: the update is skipped when the incoming
    ``hubspot_lastmodified_at`` is strictly older than the stored one.

    Returns ``{available, written, skipped_stale}``. Never raises.
    """
    deal_id = deal.get("deal_id")
    if not deal_id:
        return {"available": False, "reason": "missing_deal_id", "written": 0,
                "skipped_stale": 0}

    values = [deal.get(col) for col in _LEDGER_COLUMNS
              if col not in ("created_at", "updated_at")]
    insert_cols = [c for c in _LEDGER_COLUMNS if c not in ("created_at", "updated_at")]
    placeholders = ", ".join(["%s"] * len(insert_cols))
    updatable = [c for c in insert_cols if c != "deal_id"]
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
                    -- revert a deal to a stale stage or amount. Unknown
                    -- timestamps on either side fall through to "apply", so a
                    -- deal with no modification date is still writable.
                    WHERE {LEDGER_TABLE}.hubspot_lastmodified_at IS NULL
                       OR EXCLUDED.hubspot_lastmodified_at IS NULL
                       OR EXCLUDED.hubspot_lastmodified_at
                          >= {LEDGER_TABLE}.hubspot_lastmodified_at
                    RETURNING deal_id
                    """,
                    tuple(values),
                )
                applied = cur.fetchone() is not None

                if associations_observed:
                    _replace_associations(cur, str(deal_id), associations or [],
                                          deal.get("sync_batch_id"))
            conn.commit()
        return {"available": True, "written": 1 if applied else 0,
                "skipped_stale": 0 if applied else 1}
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_deal failed for %s: %s", deal_id, exc)
        return _unavailable(written=0, skipped_stale=0)


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


def record_sync_state(*, status: str, watermark=None, deals_seen: int = 0,
                      pages_fetched: int = 0, association_failures: int = 0,
                      error: str | None = None, batch_id=None,
                      bootstrap_status: str | None = None) -> dict:
    """Record the outcome of a sync attempt.

    The watermark advances ONLY on a fully successful sync. A partial or failed
    run leaves it where it was, so the next run re-reads the same range rather
    than skipping past deals it never saw.
    """
    advance = status == "success" and watermark is not None
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable()
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {SYNC_STATE_TABLE} (
                        scope, bootstrap_status, last_modified_watermark,
                        last_incremental_at, last_status, last_error,
                        deals_seen, pages_fetched, association_failures,
                        last_batch_id, updated_at
                    ) VALUES (%s, COALESCE(%s, 'not_started'), %s, NOW(), %s, %s,
                              %s, %s, %s, %s, NOW())
                    ON CONFLICT (scope) DO UPDATE SET
                        bootstrap_status = COALESCE(
                            EXCLUDED.bootstrap_status,
                            {SYNC_STATE_TABLE}.bootstrap_status),
                        last_modified_watermark = CASE WHEN %s
                            THEN EXCLUDED.last_modified_watermark
                            ELSE {SYNC_STATE_TABLE}.last_modified_watermark END,
                        last_incremental_at  = NOW(),
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
                    (SYNC_SCOPE, bootstrap_status,
                     watermark if advance else None,
                     status, error, int(deals_seen), int(pages_fetched),
                     int(association_failures), batch_id, advance),
                )
            conn.commit()
        return {"available": True}
    except Exception as exc:  # noqa: BLE001
        log.error("record_sync_state failed: %s", exc)
        return _unavailable()


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
