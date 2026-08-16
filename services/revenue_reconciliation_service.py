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
    does not hold AT ALL, in any state. Never expected: it means the canonical
    sync missed something.
  * **won_disagreement** — the canonical ledger HOLDS the deal, but it and the
    legacy ledger disagree about whether it is won. Expected when the legacy
    `ILIKE '%won%'` predicate or the old unknown-stage→won default counted a
    deal HubSpot says is not won; NOT expected when canonical simply does not
    know the won state, or dates the close outside the window.
  * **amount_disagreement** — same deal, different money. Expected when the
    legacy unverified-USD assumption meets canonical fail-closed currency; not
    expected when both sides claim a proven USD figure and still differ, or
    when the legacy ledger holds the deal with NO amount at all while canonical
    has proven one.
  * **duplicate_legacy_rows** — one deal held as several rows by
    `gclid_attribution`'s SHA1 attribution key, within the same window.

Splitting `legacy_only` from `won_disagreement` is the point of the identity
read: "the sync missed this deal" and "the two ledgers classify this deal
differently" have completely different remediations, and collapsing them made
the gate unable to say which had happened.

Every itemized difference carries ``expected``. The gate fails on every
difference that is NOT expected — an unexplained difference is precisely what
PR-ADS-153E-B cannot migrate through.

A legacy lineage that could not be READ fails the gate outright, before any
comparison is attempted. Unavailable is not an empty ledger: against an empty
canonical won population the two are indistinguishable, and a broken read would
otherwise report a perfectly reconciled zero.

Coverage (PR-ADS-153E-A2)
-------------------------
Reconciling what the ledger HOLDS says nothing about what it is MISSING. A
portal with no historical bootstrap at all — one nightly incremental over the
last 24 hours, reporting `success` — reconciled perfectly against the same
24 hours of legacy rows and passed the 153E-A gate. It would have handed the
executive dashboards a ledger containing a day of history.

So `ok: true` additionally requires: a sync-state row exists; the historical
bootstrap is COMPLETE; its start and completion timestamps exist and are
ordered; a successful INCREMENTAL ran after that completion; the last sync
succeeded without recording an error; and stage coverage is readable.

Every violation carries a stable `code` (see the `V_*` constants) alongside its
human message, so a runbook can key off the reason rather than parse English.

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

# ── Difference reasons ───────────────────────────────────────────────────────
# canonical_only
REASON_NON_GCLID_EXCLUDED = "non_gclid_deal_excluded_by_legacy_ledger"
REASON_GCLID_DEAL_MISSING_FROM_LEGACY = "gclid_won_deal_missing_from_legacy_ledger"
REASON_WON_DEAL_MISSING_FROM_LEGACY = "canonical_won_deal_missing_from_legacy_ledger"
# legacy_only
REASON_MISSING_FROM_CANONICAL = "missing_from_canonical_ledger"
# won_disagreement
REASON_LEGACY_PREDICATE_FALSE_POSITIVE = "legacy_predicate_counted_non_won_deal"
REASON_CANONICAL_WON_UNKNOWN = "canonical_won_state_unknown"
REASON_CLOSE_DATE_OUTSIDE_WINDOW = "canonical_close_date_outside_window"
# amount_disagreement
REASON_AMOUNT_PROVEN_BOTH_SIDES = "both_ledgers_claim_a_proven_usd_amount"
REASON_LEGACY_AMOUNT_UNAVAILABLE = "legacy_amount_unavailable"
# ledger-level
REASON_LEGACY_LEDGER_UNAVAILABLE = "legacy_ledger_unavailable"

# The two differences this PR exists to produce. Everything else is unexplained
# and fails the gate.
EXPECTED_REASONS = frozenset({
    # The defect being fixed: the GCLID ledger structurally cannot hold a deal
    # with no click evidence.
    REASON_NON_GCLID_EXCLUDED,
    # Canonical withholds a value legacy asserted without proof, or converts one
    # legacy read as USD. Prefixed reasons are matched separately.
    REASON_LEGACY_PREDICATE_FALSE_POSITIVE,
})

# Amount differences explained by the currency doctrine itself.
_EXPECTED_AMOUNT_PREFIXES = ("canonical_currency_", "currency_resolution_differs:")


def _is_expected(reason: str) -> bool:
    return (reason in EXPECTED_REASONS
            or any(str(reason).startswith(p) for p in _EXPECTED_AMOUNT_PREFIXES))


# ── Stable violation codes (PR-ADS-153E-A2) ─────────────────────────────────
# Machine-readable identities for every gate failure, so an operator runbook and
# CI can key off the reason rather than parsing English.
V_DEAL_ID_DUPLICATED = "canonical_deal_id_duplicated"
V_WON_WITHOUT_PREDICATE = "won_without_hs_is_closed_won"
V_UNPROVEN_CURRENCY_IN_TOTAL = "unproven_currency_in_usd_total"
V_FAILED_LOOKUP_AS_CLASSIFICATION = "failed_lookup_reported_as_unclassified"
V_ROWS_DISAGREE_WITH_SUMMARY = "rows_disagree_with_summary"
V_CURRENCY_COMPLETENESS_MISREPORTED = "currency_completeness_misreported"
V_LEGACY_LEDGER_UNAVAILABLE = "legacy_ledger_unavailable"
V_LEGACY_DEAL_MISSING_FROM_CANONICAL = "legacy_deal_missing_from_canonical"
V_UNEXPLAINED_DIFFERENCE = "unexplained_difference"
# Coverage — the interlock this PR exists to add.
V_SYNC_STATE_UNAVAILABLE = "sync_state_unavailable"
V_SYNC_STATE_MISSING = "sync_state_missing"
V_BOOTSTRAP_NOT_COMPLETE = "bootstrap_not_complete"
V_BOOTSTRAP_TIMESTAMP_MISSING = "bootstrap_timestamp_missing"
V_BOOTSTRAP_TIMESTAMP_INVALID = "bootstrap_timestamp_invalid"
V_POST_BOOTSTRAP_INCREMENTAL_MISSING = "post_bootstrap_incremental_missing"
V_LAST_SYNC_NOT_SUCCESSFUL = "last_sync_not_successful"
V_LAST_SYNC_NOT_INCREMENTAL = "last_sync_not_successful_incremental"
V_LAST_SYNC_SUCCESS_WITH_ERROR = "last_sync_reported_success_with_error"
V_STAGE_BREAKDOWN_UNAVAILABLE = "stage_breakdown_unavailable"


def _violation(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _ts(value):
    """Parse a repository ISO timestamp, or None. An unparseable one is UNKNOWN.

    Never substituted with a default — a guessed timestamp would let the
    ordering checks below pass on evidence that does not exist.
    """
    if value in (None, ""):
        return None
    try:
        from datetime import datetime  # noqa: PLC0415

        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


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

                # Bounded by the SAME window as the read above. An unbounded
                # GROUP BY over the whole legacy table turns this audit into a
                # full-table scan that grows without limit, and it would also
                # report duplicates for deals outside the window being
                # reconciled. The won predicate is deliberately NOT applied: a
                # relabelled duplicate row can carry a different stage label, and
                # excluding it would hide the very defect being counted.
                cur.execute(
                    """
                    SELECT deal_id, COUNT(*) AS rows_held
                    FROM gclid_attribution
                    WHERE deal_id IS NOT NULL
                      AND deal_close_date IS NOT NULL
                      AND (%s::timestamptz IS NULL OR deal_close_date >= %s)
                      AND (%s::timestamptz IS NULL OR deal_close_date < %s)
                    GROUP BY deal_id HAVING COUNT(*) > 1
                    ORDER BY 2 DESC LIMIT 200
                    """,
                    (start, start, end, end),
                )
                dupes = [{"deal_id": str(r[0]), "rows_held": int(r[1])}
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


def _diff_against_legacy(canonical_won: dict, canonical_states: dict,
                         legacy: dict, label: str,
                         *, expect_gclid_only: bool) -> dict:
    """Itemize every difference between canonical and one legacy ledger.

    Args:
        canonical_won: the canonical WON population for the window, by deal id.
        canonical_states: canonical identity for every deal either side holds,
            in any state and any window. This is what separates "the sync missed
            this deal" from "the ledgers classify it differently".
    """
    def _item(payload: dict) -> dict:
        payload["expected"] = _is_expected(payload["reason"])
        return payload

    # ── The legacy ledger could not be READ ─────────────────────────────────
    # Comparing against it now would be comparing against nothing, and every
    # canonical deal would be itemized as "absent from legacy" — a fabricated
    # finding. `legacy_deal_count` stays NULL rather than 0, because 0 is a
    # claim the ledger is empty and we do not know that.
    if not legacy.get("available"):
        return {
            "ledger": label,
            "available": False,
            "reason": REASON_LEGACY_LEDGER_UNAVAILABLE,
            "unavailable_detail": legacy.get("reason"),
            "legacy_deal_count": None,
            "canonical_only": [], "legacy_only": [], "won_disagreement": [],
            "amount_disagreement": [], "duplicate_legacy_rows": [],
        }

    legacy_deals = legacy.get("deals") or {}
    canonical_only, legacy_only, won_diff, amount_diff = [], [], [], []

    for deal_id, row in canonical_won.items():
        if deal_id not in legacy_deals:
            if expect_gclid_only and not row.get("gclid"):
                # The defect this PR exists to fix: ledger A structurally cannot
                # hold a deal with no click evidence.
                reason = REASON_NON_GCLID_EXCLUDED
            elif expect_gclid_only:
                # A won deal that DOES carry a GCLID and is still absent is not
                # explained by that structural exclusion. Something is wrong.
                reason = REASON_GCLID_DEAL_MISSING_FROM_LEGACY
            else:
                reason = REASON_WON_DEAL_MISSING_FROM_LEGACY
            canonical_only.append(_item({
                "deal_id": deal_id,
                "revenue_usd": _f(row.get("revenue_usd")),
                "currency_status": row.get("currency_status"),
                "has_gclid": bool(row.get("gclid")),
                "reason": reason,
            }))
            continue
        legacy_amount = _f(legacy_deals[deal_id].get("deal_amount_usd"))
        canonical_amount = _f(row.get("revenue_usd"))
        if canonical_amount is None and legacy_amount is not None:
            amount_diff.append(_item({
                "deal_id": deal_id, "canonical_usd": None,
                "legacy_usd": legacy_amount,
                "canonical_currency_status": row.get("currency_status"),
                "reason": f"canonical_currency_{row.get('currency_reason')}",
            }))
        elif canonical_amount is not None and legacy_amount is None:
            # The legacy ledger holds the deal but no money for it. Silently
            # skipping this was the mirror image of the fail-closed rule above:
            # canonical is about to become the source of truth for an amount the
            # outgoing ledger never carried, and nobody would see it move.
            amount_diff.append(_item({
                "deal_id": deal_id, "canonical_usd": canonical_amount,
                "legacy_usd": None,
                "canonical_currency_status": row.get("currency_status"),
                "reason": REASON_LEGACY_AMOUNT_UNAVAILABLE,
            }))
        elif (canonical_amount is not None and legacy_amount is not None
              and abs(canonical_amount - legacy_amount) >= AMOUNT_TOLERANCE_USD):
            # A converted amount legitimately differs from a figure legacy read
            # as USD without checking. Two independently PROVEN USD amounts that
            # still differ are not explained by the currency doctrine.
            from analysis.deal_currency import CURRENCY_VERIFIED_USD  # noqa: PLC0415

            status = row.get("currency_status")
            amount_diff.append(_item({
                "deal_id": deal_id, "canonical_usd": canonical_amount,
                "legacy_usd": legacy_amount,
                "canonical_currency_status": status,
                "reason": (REASON_AMOUNT_PROVEN_BOTH_SIDES
                           if status == CURRENCY_VERIFIED_USD
                           else f"currency_resolution_differs:{status}"),
            }))

    for deal_id, row in legacy_deals.items():
        if deal_id in canonical_won:
            continue
        state = canonical_states.get(deal_id)
        base = {
            "deal_id": deal_id,
            "legacy_usd": _f(row.get("deal_amount_usd")),
            "legacy_stage": row.get("deal_stage"),
            "legacy_stage_label": row.get("deal_stage_label"),
        }
        if state is None:
            # The canonical ledger has never seen this deal at all.
            legacy_only.append(_item({**base,
                                      "reason": REASON_MISSING_FROM_CANONICAL}))
            continue
        won = state.get("hs_is_closed_won")
        if won is False:
            # HubSpot's own boolean says not won. The legacy `ILIKE '%won%'`
            # predicate and the unknown-stage→won default are exactly how a
            # non-won deal became revenue.
            reason = REASON_LEGACY_PREDICATE_FALSE_POSITIVE
        elif won is None:
            reason = REASON_CANONICAL_WON_UNKNOWN
        else:
            # Canonically won, but its close date puts it in a different window.
            reason = REASON_CLOSE_DATE_OUTSIDE_WINDOW
        won_diff.append(_item({
            **base,
            "canonical_is_closed_won": won,
            "canonical_close_date": state.get("deal_close_date"),
            "canonical_stage_label": state.get("deal_stage_label"),
            "reason": reason,
        }))

    return {
        "ledger": label,
        "available": bool(legacy.get("available")),
        "legacy_deal_count": len(legacy_deals),
        "canonical_only": sorted(canonical_only, key=lambda r: r["deal_id"]),
        "legacy_only": sorted(legacy_only, key=lambda r: r["deal_id"]),
        "won_disagreement": sorted(won_diff, key=lambda r: r["deal_id"]),
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

    # Canonical IDENTITY for every legacy deal not already in the won population
    # — across ALL states and windows. Without this read the gate cannot tell a
    # deal the sync missed from a deal the two ledgers merely classify
    # differently, and would report both as "missing from canonical".
    lookup_ids = {
        did
        for legacy in (legacy_gclid, legacy_source)
        for did in (legacy.get("deals") or {})
        if did not in won_rows
    }
    states_res = ledger_repo.fetch_deal_states(lookup_ids)
    if not states_res.get("available"):
        return {"available": False, "window": window,
                "reason": "canonical_deal_states_unavailable"}
    canonical_states = states_res.get("rows") or {}

    diffs = [
        _diff_against_legacy(won_rows, canonical_states, legacy_gclid,
                             "gclid_attribution", expect_gclid_only=True),
        _diff_against_legacy(won_rows, canonical_states, legacy_source,
                             "deal_source_attribution", expect_gclid_only=False),
    ]

    findings = _check_invariants(summary, won_rows, diffs, sync_res, stages_res)
    violations = [f["message"] for f in findings]

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
        # NULL, not [], when the read failed — an unavailable breakdown must
        # not render as "no deals in the ledger yet".
        "stage_breakdown": (stages_res.get("rows") or []
                            if stages_res.get("available") else None),
        "stage_breakdown_available": bool(stages_res.get("available")),
        "sync_state": sync_res.get("row"),
        "legacy_diffs": diffs,
        "violations": violations,
        "violation_codes": sorted({f["code"] for f in findings}),
        "violation_details": findings,
        "ok": not findings,
        "governance": {
            "read_only": True, "external_writes": False,
            "shadow_mode": True,
            "note": ("Canonical ledger is populated and reconciled but consumed "
                     "by no production page until PR-ADS-153E-B."),
        },
    }


def _check_invariants(summary: dict, won_rows: dict, diffs: list,
                      sync_res: dict, stages_res: dict | None = None) -> list:
    """Every condition that must fail the merge/production gate.

    Returns a list of ``{code, message}``. The code is the stable identity; the
    message is for humans and may be reworded freely.
    """
    violations: list = []

    # A duplicated primary key would mean the ledger lost its identity contract.
    total = summary.get("total_deals")
    distinct = summary.get("distinct_deals")
    if total is not None and distinct is not None and total != distinct:
        violations.append(_violation(
            V_DEAL_ID_DUPLICATED,
            f"canonical deal_id duplicated: {total} rows for {distinct} deals"))

    # Won population must be exactly hs_is_closed_won IS TRUE.
    not_won = [d for d, r in won_rows.items() if r.get("hs_is_closed_won") is not True]
    if not_won:
        violations.append(_violation(
            V_WON_WITHOUT_PREDICATE,
            f"{len(not_won)} row(s) counted as won without hs_is_closed_won=true: "
            + ", ".join(sorted(not_won)[:5])))

    # An unproven currency must never contribute to a USD total.
    from analysis.deal_currency import SUMMABLE_CURRENCY_STATUSES

    leaked = [d for d, r in won_rows.items()
              if r.get("revenue_usd") is not None
              and r.get("currency_status") not in SUMMABLE_CURRENCY_STATUSES]
    if leaked:
        violations.append(_violation(
            V_UNPROVEN_CURRENCY_IN_TOTAL,
            f"{len(leaked)} deal(s) carry revenue_usd with an unproven currency: "
            + ", ".join(sorted(leaked)[:5])))

    # A failed association lookup must never be recorded as a classification.
    misreported = [d for d, r in won_rows.items()
                   if r.get("association_status") == "lookup_failed"
                   and r.get("attribution_status") == "unclassified"]
    if misreported:
        violations.append(_violation(
            V_FAILED_LOOKUP_AS_CLASSIFICATION,
            f"{len(misreported)} failed association lookup(s) reported as "
            "unclassified: " + ", ".join(sorted(misreported)[:5])))

    # Row-level and summary-level truth must agree.
    if summary.get("won_deals") is not None and len(won_rows) != summary["won_deals"]:
        violations.append(_violation(
            V_ROWS_DISAGREE_WITH_SUMMARY,
            f"ledger rows ({len(won_rows)}) disagree with ledger summary "
            f"({summary['won_deals']} won deals)"))

    proven = sum(1 for r in won_rows.values()
                 if r.get("currency_status") in SUMMABLE_CURRENCY_STATUSES)
    if summary.get("won_currency_proven") is not None \
            and proven != summary["won_currency_proven"]:
        violations.append(_violation(
            V_CURRENCY_COMPLETENESS_MISREPORTED,
            f"currency completeness misreported: {proven} rows proven vs "
            f"{summary['won_currency_proven']} in summary"))

    for diff in diffs:
        # A legacy lineage we could not READ makes the reconciliation
        # meaningless, and it must fail loudly. Unavailable is NOT an empty
        # ledger: with an empty canonical won population the two look identical
        # — zero differences, apparently reconciled — which is exactly how a
        # broken read would have waved PR-ADS-153E-B's cutover through.
        if not diff.get("available"):
            detail = diff.get("unavailable_detail")
            violations.append(_violation(
                V_LEGACY_LEDGER_UNAVAILABLE,
                f"legacy ledger {diff['ledger']} unavailable — reconciliation "
                "cannot be performed"
                + (f" ({detail})" if detail else "")))
            continue

        # A deal the legacy ledger holds and the canonical does not hold at all
        # means the sync is incomplete.
        if diff.get("legacy_only"):
            violations.append(_violation(
                V_LEGACY_DEAL_MISSING_FROM_CANONICAL,
                f"{len(diff['legacy_only'])} deal(s) present in "
                f"{diff['ledger']} but missing from the canonical ledger: "
                + ", ".join(r["deal_id"] for r in diff["legacy_only"][:5])))

        # Every other difference must be EXPLAINED. An unexplained one is a deal
        # PR-ADS-153E-B would move on a live page with no reason to give.
        for category in ("canonical_only", "won_disagreement",
                         "amount_disagreement"):
            unexplained = [r for r in (diff.get(category) or [])
                           if not r.get("expected")]
            if not unexplained:
                continue
            by_reason: dict = {}
            for row in unexplained:
                by_reason.setdefault(row["reason"], []).append(row["deal_id"])
            for reason, ids in sorted(by_reason.items()):
                violations.append(_violation(
                    V_UNEXPLAINED_DIFFERENCE,
                    f"{len(ids)} unexplained {category} difference(s) vs "
                    f"{diff['ledger']} ({reason}): "
                    + ", ".join(sorted(ids)[:5])))

    # ── Stage coverage must be READABLE ─────────────────────────────────────
    # An unreadable stage breakdown was previously flattened to `[]` and printed
    # as "no deals in the ledger yet" — an unavailable read rendered as a fact.
    if stages_res is not None and not stages_res.get("available"):
        violations.append(_violation(
            V_STAGE_BREAKDOWN_UNAVAILABLE,
            "stage coverage unavailable — open/lost/downgrade/churn storage "
            "cannot be verified"
            + (f" ({stages_res.get('reason')})" if stages_res.get("reason")
               else "")))

    # ── Sync coverage must be COMPLETE, ORDERED and honest ──────────────────
    # PR-ADS-153E-A checked only that the state read succeeded and that the last
    # status was not a failure. That let a portal with NO historical bootstrap
    # at all pass the gate: one nightly incremental over the last 24 hours
    # reports `success`, and the ledger — holding a day of deals and calling
    # itself reconciled — would have been handed to the executive dashboards.
    if not sync_res.get("available"):
        violations.append(_violation(
            V_SYNC_STATE_UNAVAILABLE,
            "deal sync state unavailable — coverage cannot be verified"))
        return violations

    state = sync_res.get("row") or {}
    if not state:
        violations.append(_violation(
            V_SYNC_STATE_MISSING,
            "no deal sync has ever run — there is no coverage to verify"))
        return violations

    bootstrap_status = state.get("bootstrap_status")
    started = _ts(state.get("bootstrap_started_at"))
    completed = _ts(state.get("bootstrap_completed_at"))
    incremental = _ts(state.get("last_incremental_at"))

    if bootstrap_status != "complete":
        violations.append(_violation(
            V_BOOTSTRAP_NOT_COMPLETE,
            f"historical bootstrap is {bootstrap_status or 'unknown'}, not "
            "complete — the ledger holds an unknown fraction of history"))
    else:
        # Claimed complete: the timestamps must actually corroborate it.
        missing = [name for name, value in (("bootstrap_started_at", started),
                                            ("bootstrap_completed_at", completed))
                   if value is None]
        if missing:
            violations.append(_violation(
                V_BOOTSTRAP_TIMESTAMP_MISSING,
                "bootstrap claims complete without " + " and ".join(missing)))
        elif completed < started:
            violations.append(_violation(
                V_BOOTSTRAP_TIMESTAMP_INVALID,
                f"bootstrap completed_at ({completed.isoformat()}) precedes "
                f"started_at ({started.isoformat()})"))

    # A successful INCREMENTAL after the bootstrap is what proves the ongoing
    # pipeline works on top of the historical base — not just that a one-off
    # backfill once ran.
    if completed is None:
        if bootstrap_status == "complete":
            pass    # already reported as a missing timestamp above
    elif incremental is None:
        violations.append(_violation(
            V_POST_BOOTSTRAP_INCREMENTAL_MISSING,
            "no incremental sync has run since the bootstrap completed"))
    elif incremental <= completed:
        violations.append(_violation(
            V_POST_BOOTSTRAP_INCREMENTAL_MISSING,
            f"last incremental ({incremental.isoformat()}) is not after "
            f"bootstrap completion ({completed.isoformat()})"))

    # The MODE that produced `last_status` is a durable fact, never inferred.
    # `last_status` and `last_error` are shared between modes, so without it a
    # bootstrap rerun's success validated an incremental timestamp it never
    # wrote: bootstrap completes at T0, the incremental FAILS at T1, then a
    # bootstrap reruns successfully at T2 — preserving T0 and T1, overwriting
    # last_status. The ordering check saw T1 > T0 and a success, and passed a
    # history containing no successful incremental after the bootstrap at all.
    last_mode = state.get("last_sync_mode")
    if last_mode != "incremental":
        violations.append(_violation(
            V_LAST_SYNC_NOT_INCREMENTAL,
            "the most recent sync was "
            + (f"a {last_mode}" if last_mode
               else "recorded before sync mode was tracked")
            + " — a successful INCREMENTAL is what proves the ongoing pipeline "
              "works on top of the historical base"))

    last_status = state.get("last_status")
    if last_status != "success":
        violations.append(_violation(
            V_LAST_SYNC_NOT_SUCCESSFUL,
            f"last deal sync was {last_status or 'never recorded'}"
            + (f" ({state.get('last_error')})" if state.get("last_error")
               else "")))
    elif state.get("last_error"):
        # Success and an error message together is a contradiction, and the
        # error is the half that is safe to believe.
        violations.append(_violation(
            V_LAST_SYNC_SUCCESS_WITH_ERROR,
            "last deal sync claims success but recorded an error: "
            f"{state.get('last_error')}"))

    return violations


__all__ = [
    "AMOUNT_TOLERANCE_USD", "EXPECTED_REASONS",
    "REASON_NON_GCLID_EXCLUDED", "REASON_GCLID_DEAL_MISSING_FROM_LEGACY",
    "REASON_WON_DEAL_MISSING_FROM_LEGACY", "REASON_MISSING_FROM_CANONICAL",
    "REASON_LEGACY_PREDICATE_FALSE_POSITIVE", "REASON_CANONICAL_WON_UNKNOWN",
    "REASON_CLOSE_DATE_OUTSIDE_WINDOW", "REASON_AMOUNT_PROVEN_BOTH_SIDES",
    "REASON_LEGACY_AMOUNT_UNAVAILABLE", "REASON_LEGACY_LEDGER_UNAVAILABLE",
    "V_LAST_SYNC_NOT_INCREMENTAL",
    "build_revenue_reconciliation",
]
