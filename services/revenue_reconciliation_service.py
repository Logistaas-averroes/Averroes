"""
services/revenue_reconciliation_service.py

PR-ADS-153E-A — deal-grain reconciliation between the canonical ledger and the
two legacy revenue lineages.

Why deal-grain
--------------
"Totals differ by $X" is not a finding, it is a symptom. Before any consumer can
be migrated in PR-ADS-153E-B, every single difference must be attributable to a
named deal and a stated reason — otherwise the cutover would move numbers on
executive pages without anyone being able to explain which deals moved or why.

So this service itemizes:

  * **canonical_only**  — a deal the canonical ledger has and a legacy ledger
    does not. Expected for non-GCLID revenue, which `gclid_attribution`
    structurally excludes (PR-ADS-153A §9.2) — that exclusion is the whole
    reason this PR exists.
  * **legacy_only**     — a deal a legacy ledger has and the canonical ledger
    does not. NOT expected: it means the canonical sync missed something.
  * **won_disagreement** — the ledgers disagree about whether a deal is won.
    Usually the legacy `ILIKE '%won%'` predicate or the old unknown-stage→won
    default counting a non-won deal as revenue.
  * **amount_disagreement** — same deal, different money. Usually the legacy
    unverified-USD assumption versus canonical fail-closed currency.
  * **duplicate_legacy_rows** — one deal held as several rows by
    `gclid_attribution`'s SHA1 attribution key.

Read-only. No external API calls. Carries no contact names, emails or full
GCLIDs — a GCLID is reported only as present/absent, because reconciliation
output goes into CI logs.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Money below this is treated as equal — floating point and rounding differences
# between lineages are not findings. Anything at or above it is itemized.
AMOUNT_TOLERANCE_USD = 0.01


def _unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason}


def _f(value):
    return None if value is None else float(value)


def _fetch_legacy_gclid_deals(start, end) -> dict:
    """Ledger A: `gclid_attribution`, deduplicated the way its readers do.

    Its readers use ``DISTINCT ON (deal_id) ... ORDER BY created_at DESC`` plus
    the legacy won predicate, so the comparison mirrors that exactly — comparing
    against something the product does not actually read would prove nothing.
    """
    from db.connection import get_conn  # noqa: PLC0415

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable("database_unavailable")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (deal_id)
                           deal_id, deal_amount_usd, deal_close_date,
                           deal_stage, deal_stage_label, gclid
                    FROM gclid_attribution
                    WHERE deal_id IS NOT NULL
                      AND deal_close_date IS NOT NULL
                      AND (deal_stage = '326093516'
                           OR deal_stage_label ILIKE '%%won%%')
                      AND (%s::timestamptz IS NULL OR deal_close_date >= %s)
                      AND (%s::timestamptz IS NULL OR deal_close_date < %s)
                    ORDER BY deal_id, created_at DESC
                    """,
                    (start, start, end, end),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]

                cur.execute(
                    """
                    SELECT deal_id, COUNT(*) AS rows_held
                    FROM gclid_attribution
                    WHERE deal_id IS NOT NULL
                    GROUP BY deal_id HAVING COUNT(*) > 1
                    ORDER BY 2 DESC LIMIT 200
                    """)
                dupes = [{"deal_id": r[0], "rows_held": int(r[1])}
                         for r in cur.fetchall()]
        return {"available": True,
                "deals": {str(r["deal_id"]): r for r in rows},
                "duplicates": dupes}
    except Exception as exc:  # noqa: BLE001
        log.warning("legacy gclid ledger read failed: %s", exc)
        return _unavailable(str(exc))


def _fetch_legacy_source_deals(start, end) -> dict:
    """Ledger B: `deal_source_attribution` (deal-keyed, all closed-won)."""
    from db.connection import get_conn  # noqa: PLC0415

    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable("database_unavailable")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT deal_id, deal_amount_usd, deal_close_date,
                           acquisition_group, attribution_status
                    FROM deal_source_attribution
                    WHERE (%s::timestamptz IS NULL OR deal_close_date >= %s)
                      AND (%s::timestamptz IS NULL OR deal_close_date < %s)
                    """,
                    (start, start, end, end),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"available": True,
                "deals": {str(r["deal_id"]): r for r in rows}}
    except Exception as exc:  # noqa: BLE001
        log.warning("legacy source ledger read failed: %s", exc)
        return _unavailable(str(exc))


def _diff_against_legacy(canonical_won: dict, legacy: dict, label: str,
                         *, expect_gclid_only: bool) -> dict:
    """Itemize every difference between canonical and one legacy ledger."""
    legacy_deals = legacy.get("deals") or {}
    canonical_only, legacy_only, amount_diff = [], [], []

    for deal_id, row in canonical_won.items():
        if deal_id not in legacy_deals:
            canonical_only.append({
                "deal_id": deal_id,
                "revenue_usd": _f(row.get("revenue_usd")),
                "currency_status": row.get("currency_status"),
                "has_gclid": bool(row.get("gclid")),
                # The expected, designed-for difference: ledger A cannot hold a
                # deal with no click evidence.
                "reason": ("non_gclid_deal_excluded_by_legacy_ledger"
                           if expect_gclid_only and not row.get("gclid")
                           else "missing_from_legacy_ledger"),
            })
            continue
        legacy_amount = _f(legacy_deals[deal_id].get("deal_amount_usd"))
        canonical_amount = _f(row.get("revenue_usd"))
        if canonical_amount is None and legacy_amount is not None:
            amount_diff.append({
                "deal_id": deal_id, "canonical_usd": None,
                "legacy_usd": legacy_amount,
                "reason": f"canonical_currency_{row.get('currency_reason')}",
            })
        elif (canonical_amount is not None and legacy_amount is not None
              and abs(canonical_amount - legacy_amount) >= AMOUNT_TOLERANCE_USD):
            amount_diff.append({
                "deal_id": deal_id, "canonical_usd": canonical_amount,
                "legacy_usd": legacy_amount,
                "reason": ("currency_resolution_differs:"
                           f"{row.get('currency_status')}"),
            })

    for deal_id, row in legacy_deals.items():
        if deal_id not in canonical_won:
            legacy_only.append({
                "deal_id": deal_id,
                "legacy_usd": _f(row.get("deal_amount_usd")),
                "legacy_stage": row.get("deal_stage"),
                "legacy_stage_label": row.get("deal_stage_label"),
                # Either the canonical sync missed it, or the legacy predicate
                # counted something HubSpot does not consider won.
                "reason": "legacy_won_not_canonical_won_or_missing_from_ledger",
            })

    return {
        "ledger": label,
        "available": bool(legacy.get("available")),
        "legacy_deal_count": len(legacy_deals),
        "canonical_only": sorted(canonical_only, key=lambda r: r["deal_id"]),
        "legacy_only": sorted(legacy_only, key=lambda r: r["deal_id"]),
        "amount_disagreement": sorted(amount_diff, key=lambda r: r["deal_id"]),
        "duplicate_legacy_rows": legacy.get("duplicates") or [],
    }


def build_revenue_reconciliation(window: str = "current_quarter",
                                 now=None) -> dict:
    """Full shadow reconciliation for one business window.

    Returns the canonical summary, the per-ledger deal-grain diffs, the stage
    breakdown, sync coverage, and a list of invariant violations. Read-only.
    """
    from analysis.business_windows import get_window_bounds, is_valid_window
    from db import deal_ledger_repository as ledger_repo

    if not is_valid_window(window):
        return {"available": False, "reason": f"unknown_window:{window}"}

    # get_window_bounds returns (start, end) with an EXCLUSIVE upper bound —
    # the repository filters use `< end` to match.
    start, end = get_window_bounds(window, now)

    summary_res = ledger_repo.fetch_ledger_summary(start, end)
    rows_res = ledger_repo.fetch_ledger_rows(start, end, won_only=True)
    stages_res = ledger_repo.fetch_stage_breakdown()
    sync_res = ledger_repo.fetch_sync_state()

    if not summary_res.get("available") or not rows_res.get("available"):
        return {"available": False, "window": window,
                "reason": "canonical_ledger_unavailable"}

    summary = summary_res.get("summary") or {}
    won_rows = {str(r["deal_id"]): r for r in (rows_res.get("rows") or [])}

    legacy_gclid = _fetch_legacy_gclid_deals(start, end)
    legacy_source = _fetch_legacy_source_deals(start, end)

    diffs = [
        _diff_against_legacy(won_rows, legacy_gclid, "gclid_attribution",
                             expect_gclid_only=True),
        _diff_against_legacy(won_rows, legacy_source, "deal_source_attribution",
                             expect_gclid_only=False),
    ]

    violations = _check_invariants(summary, won_rows, diffs, sync_res)

    return {
        "available": True,
        "window": window,
        "window_start": str(start) if start else None,
        "window_end": str(end) if end else None,
        "canonical": {
            "distinct_deals": summary.get("distinct_deals"),
            "total_rows": summary.get("total_deals"),
            "won_deals": summary.get("won_deals"),
            "unknown_won_deals": summary.get("unknown_won_deals"),
            "won_with_gclid": summary.get("won_with_gclid"),
            "won_without_gclid": summary.get("won_without_gclid"),
            "won_without_close_date": summary.get("won_without_close_date"),
            "won_currency_proven": summary.get("won_currency_proven"),
            "won_currency_unavailable": summary.get("won_currency_unavailable"),
            "revenue_usd": summary.get("revenue_usd"),
            "amount_raw_total": summary.get("amount_raw_total"),
            "ambiguous_associations": summary.get("ambiguous_assoc"),
            "failed_associations": summary.get("failed_assoc"),
            "unknown_stage_deals": summary.get("unknown_stage"),
        },
        "stage_breakdown": stages_res.get("rows") or [],
        "sync_state": sync_res.get("row"),
        "legacy_diffs": diffs,
        "violations": violations,
        "ok": not violations,
        "governance": {
            "read_only": True, "external_writes": False,
            "shadow_mode": True,
            "note": ("Canonical ledger is populated and reconciled but consumed "
                     "by no production page until PR-ADS-153E-B."),
        },
    }


def _check_invariants(summary: dict, won_rows: dict, diffs: list,
                      sync_res: dict) -> list:
    """Every condition that must fail the merge/production gate."""
    violations: list = []

    # A duplicated primary key would mean the ledger lost its identity contract.
    total = summary.get("total_deals")
    distinct = summary.get("distinct_deals")
    if total is not None and distinct is not None and total != distinct:
        violations.append(
            f"canonical deal_id duplicated: {total} rows for {distinct} deals")

    # Won population must be exactly hs_is_closed_won IS TRUE.
    not_won = [d for d, r in won_rows.items() if r.get("hs_is_closed_won") is not True]
    if not_won:
        violations.append(
            f"{len(not_won)} row(s) counted as won without hs_is_closed_won=true: "
            + ", ".join(sorted(not_won)[:5]))

    # An unproven currency must never contribute to a USD total.
    from analysis.deal_currency import SUMMABLE_CURRENCY_STATUSES

    leaked = [d for d, r in won_rows.items()
              if r.get("revenue_usd") is not None
              and r.get("currency_status") not in SUMMABLE_CURRENCY_STATUSES]
    if leaked:
        violations.append(
            f"{len(leaked)} deal(s) carry revenue_usd with an unproven currency: "
            + ", ".join(sorted(leaked)[:5]))

    # A failed association lookup must never be recorded as a classification.
    misreported = [d for d, r in won_rows.items()
                   if r.get("association_status") == "lookup_failed"
                   and r.get("attribution_status") == "unclassified"]
    if misreported:
        violations.append(
            f"{len(misreported)} failed association lookup(s) reported as "
            "unclassified: " + ", ".join(sorted(misreported)[:5]))

    # Row-level and summary-level truth must agree.
    if summary.get("won_deals") is not None and len(won_rows) != summary["won_deals"]:
        violations.append(
            f"ledger rows ({len(won_rows)}) disagree with ledger summary "
            f"({summary['won_deals']} won deals)")

    proven = sum(1 for r in won_rows.values()
                 if r.get("currency_status") in SUMMABLE_CURRENCY_STATUSES)
    if summary.get("won_currency_proven") is not None \
            and proven != summary["won_currency_proven"]:
        violations.append(
            f"currency completeness misreported: {proven} rows proven vs "
            f"{summary['won_currency_proven']} in summary")

    # A deal the legacy ledger holds and the canonical does not means the sync
    # is incomplete — the one difference direction that is never expected.
    for diff in diffs:
        if diff.get("legacy_only"):
            violations.append(
                f"{len(diff['legacy_only'])} deal(s) present in "
                f"{diff['ledger']} but missing from the canonical ledger")

    # Sync coverage must be complete and honest.
    state = sync_res.get("row") or {}
    if not sync_res.get("available"):
        violations.append("deal sync state unavailable")
    elif state and state.get("last_status") not in (None, "success"):
        violations.append(
            f"last deal sync was {state.get('last_status')}"
            + (f" ({state.get('last_error')})" if state.get("last_error") else ""))

    return violations


__all__ = ["AMOUNT_TOLERANCE_USD", "build_revenue_reconciliation"]
