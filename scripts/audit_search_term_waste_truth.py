#!/usr/bin/env python3
"""
scripts/audit_search_term_waste_truth.py

PR-ADS-153D §47 — READ-ONLY production validation for the consolidated
search-term waste experience.

This is a MERGE GATE, not a report. It exits non-zero on any failure
condition listed below, so a deploy check or CI job can depend on it. It
reconciles COUNTS (rows, identities, the KPI term count, the pagination
total); it does not reconcile KPI spend or review-needed totals against the
rows:

    python -m scripts.audit_search_term_waste_truth --window 30d
    echo $?          # 0 = reconciled, 1 = validation failed, 2 = usage error

Optional:
    --window 30d     evidence window (7d|14d|30d|60d|180d|all_time; default 30d)
    --json           machine-readable output (still exits non-zero on failure)

Failure conditions (each one exits 1)
-------------------------------------
  * duplicate ``term_identity`` in the COMPLETE flagged population;
  * row count, KPI count and pagination total disagreeing;
  * ``truth_state = mismatch``;
  * canonical facts unavailable, or the local review store unavailable;
  * flag history never persisted (the production writer is not running);
  * any database error.

PR-ADS-162G: the Action Queue page and ``GET /api/action-queue`` were deleted,
and with them this audit's second half, which rebuilt the queue's waste items
and reconciled them against the flagged view (queue/page spend and reason,
duplicate queue identities, a review-store outage presenting as an empty
queue). Search Terms → Flagged is now the only surface for this population, so
every check about that population itself is unchanged.

Guarantees
----------
  * NO writes of any kind — every query is a SELECT and no service called here
    has a write path;
  * NO external API calls — Google Ads, HubSpot and Mailchimp are never
    contacted; every number comes from the local database;
  * NO email addresses or other contact PII are printed;
  * durable identities are computed with the SHARED identity contract
    (``analysis.search_term_identity``), never by concatenating SQL strings —
    a delimiter-joined key would not be the identity the product uses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_USAGE = 2

# Page size large enough that the audit examines the COMPLETE flagged
# population. Auditing only the first page would let a duplicate identity or a
# reconciliation break hide beyond it.
_AUDIT_PAGE_SIZE = 200


class _Findings:
    """Collected validation failures. Empty == the audit passes."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.failures


def _fmt(value) -> str:
    """Render a value without ever turning an unknown into a zero."""
    if value is None:
        return "Unavailable"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ── Read-only collectors ─────────────────────────────────────────────────────
def _canonical_facts(start, end) -> dict:
    """Canonical ``search_terms`` totals for the window. SELECT only.

    Unique durable identities are counted in PYTHON using the shared identity
    contract, not by concatenating columns in SQL: a delimiter-joined string is
    not the identity the product uses, so it could agree here and disagree
    everywhere else.
    """
    from analysis.search_term_identity import term_identity_key
    from analysis.search_term_scope import canonical_scope
    from db.connection import get_conn

    # PR-ADS-156-F3 review §2 — this function is named `_canonical_facts` and
    # was bounded on the date window alone, so during the cutover it summed each
    # observation twice and called the result canonical. An audit that reports a
    # doubled total is worse than no audit: it is a doubled total with a verdict
    # attached.
    #
    # `start` may be None (all-time). The scope takes the same bounds and treats
    # None as "no lower bound", so the `%s::date IS NULL` guard is no longer
    # needed — the predicate simply omits the clause.
    scope = canonical_scope(start, end)
    if not scope.available:
        return {"available": False, "error": scope.reason,
                "reason": scope.reason}

    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "error": "database unavailable"}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)                             AS fact_rows,
                           SUM(spend_usd)                       AS spend_usd,
                           SUM(clicks)                          AS clicks,
                           SUM(impressions)                     AS impressions,
                           MIN(source_date)                     AS earliest_source_date,
                           MAX(source_date)                     AS latest_source_date,
                           COUNT(DISTINCT source_date)          AS distinct_source_dates
                    FROM search_terms
                    WHERE """ + scope.sql,
                    scope.params,
                )
                cols = [d[0] for d in cur.description]
                row = dict(zip(cols, cur.fetchone()))

                # Identity counting through the shared contract.
                cur.execute(
                    """
                    SELECT DISTINCT COALESCE(campaign_id, campaign_name, ''),
                                    search_term
                    FROM search_terms
                    WHERE """ + scope.sql,
                    scope.params,
                )
                pairs = cur.fetchall()
        identities = {term_identity_key(c, t) for c, t in pairs}
        row["unique_identities"] = len(identities)
        row["unique_terms"] = len({t for _, t in pairs})
        row["available"] = True
        if row.get("spend_usd") is not None:
            row["spend_usd"] = float(row["spend_usd"])
        return row
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}


def _snapshot_proof(start, end) -> dict:
    """The duplicate-snapshot evidence (§47 "Duplicate-snapshot proof").

    ``waste_terms`` is run-grained. This reports how many raw rows exist, how
    many DISTINCT annotations they represent, and what a naive
    ``SUM(spend_usd)`` over those snapshots would have claimed — the number the
    retired page published.
    """
    from db.connection import get_conn

    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "error": "database unavailable"}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)                       AS raw_rows,
                           COUNT(DISTINCT (COALESCE(campaign_name, '')
                                           || '|' || search_term)) AS distinct_annotations,
                           COUNT(DISTINCT run_date)       AS distinct_runs,
                           SUM(spend_usd)                 AS naive_snapshot_spend_usd,
                           MIN(run_date)                  AS earliest_run_date,
                           MAX(run_date)                  AS latest_run_date
                    FROM waste_terms
                    WHERE (%s::date IS NULL OR run_date >= %s)
                      AND run_date <= %s
                    """,
                    (start, start, end),
                )
                cols = [d[0] for d in cur.description]
                row = dict(zip(cols, cur.fetchone()))
        row["available"] = True
        if row.get("naive_snapshot_spend_usd") is not None:
            row["naive_snapshot_spend_usd"] = float(row["naive_snapshot_spend_usd"])
        raw = row.get("raw_rows") or 0
        distinct = row.get("distinct_annotations") or 0
        row["repeated_rows"] = max(0, raw - distinct)
        return row
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}


def _flag_history_state() -> dict:
    """Is the PRODUCTION flag-history writer actually running?

    A durable review table with flagged terms but no ``latest_flagged_at``
    anywhere means the scheduled write path never ran — the exact gap PR #156
    merge-blocker 4 names.
    """
    from db.connection import get_conn

    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "error": "database unavailable"}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), "
                    "       COUNT(latest_flagged_at), "
                    "       COUNT(first_flagged_at), "
                    "       MAX(latest_flagged_at) "
                    "FROM search_term_review")
                total, with_latest, with_first, newest = cur.fetchone()
        return {"available": True, "review_rows": int(total or 0),
                "rows_with_latest_flagged_at": int(with_latest or 0),
                "rows_with_first_flagged_at": int(with_first or 0),
                "newest_flag_observation": str(newest) if newest else None}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}


def _flagged_population(window: str) -> dict:
    """The COMPLETE flagged population, paged through before any deduplication.

    Auditing a deduplicated copy of the population would hide exactly the
    defect this gate exists to catch.
    """
    from services.search_term_evidence_service import build_flagged_search_terms

    rows: list = []
    page = 1
    first: dict = {}
    while True:
        payload = build_flagged_search_terms(window, page=page,
                                             page_size=_AUDIT_PAGE_SIZE,
                                             sort="term")
        if page == 1:
            first = payload
        if payload.get("db_unavailable") or payload.get("actionable") is False:
            return {"payload": payload, "rows": [], "complete": False}
        rows.extend(payload.get("rows") or [])
        if not (payload.get("pagination") or {}).get("has_more"):
            break
        page += 1
        if page > 100:  # hard stop; disclosed rather than silently truncated
            return {"payload": first, "rows": rows, "complete": False,
                    "truncated": True}
    return {"payload": first, "rows": rows, "complete": True}


# ── Validation ───────────────────────────────────────────────────────────────
def _validate(data: dict, findings: _Findings) -> None:
    canonical = data["canonical"]
    flagged = data["flagged"]
    payload = flagged.get("payload") or {}
    kpis = payload.get("kpis") or {}
    rows = flagged.get("rows") or []
    history = data["flag_history"]
    proof = data["snapshot_proof"]

    # ── canonical availability ──
    if not canonical.get("available"):
        findings.fail("canonical search-term facts unavailable: %s"
                      % canonical.get("error", "unknown"))
    if not proof.get("available"):
        findings.fail("waste_terms annotations unavailable: %s"
                      % proof.get("error", "unknown"))

    # ── truth state ──
    truth = (payload.get("truth_state") or {}).get("status")
    if truth == "mismatch":
        findings.fail("flagged truth_state = mismatch (%s) — decision metrics "
                      "are withheld and must not be published"
                      % ", ".join((payload.get("truth_state") or {}).get("reasons") or []))
    elif truth == "unavailable":
        findings.fail("flagged truth_state = unavailable — the canonical fact "
                      "source could not be read")

    if payload.get("db_unavailable"):
        findings.fail("flagged view reports db_unavailable")
    if not flagged.get("complete"):
        if flagged.get("truncated"):
            findings.fail("flagged population exceeded the audit page cap — "
                          "coverage is incomplete, so reconciliation is unproven")
        elif truth not in ("mismatch", "unavailable"):
            findings.fail("flagged population could not be read completely")

    # ── review store availability ──
    if payload.get("review_state_available") is False:
        findings.fail("local review store unavailable — review state cannot be "
                      "read, so outstanding work cannot be determined")

    # ── identity uniqueness over the COMPLETE population (pre-dedup) ──
    identities = [r.get("term_identity") for r in rows]
    missing = [r.get("search_term") for r in rows if not r.get("term_identity")]
    if missing:
        findings.fail("%d flagged row(s) carry no term_identity" % len(missing))
    duplicates = sorted({i for i in identities if identities.count(i) > 1 and i})
    if duplicates:
        findings.fail("%d duplicate term_identity value(s) in the complete "
                      "flagged population (pre-dedup): %s"
                      % (len(duplicates), ", ".join(duplicates[:5])))

    # ── row / KPI / pagination reconciliation ──
    if flagged.get("complete") and truth not in ("mismatch", "unavailable"):
        kpi_terms = kpis.get("flagged_terms")
        total = (payload.get("pagination") or {}).get("total_count")
        unique = len({i for i in identities if i})
        if kpi_terms is not None and kpi_terms != unique:
            findings.fail("KPI flagged_terms (%s) != unique durable identities "
                          "(%s)" % (kpi_terms, unique))
        if total is not None and len(rows) != total and payload.get("filters"):
            # total_count is the complete filtered population; the audit applies
            # no filters, so it must equal the rows collected.
            findings.fail("pagination total_count (%s) != rows collected (%s)"
                          % (total, len(rows)))
        if len(rows) != unique:
            findings.fail("flagged rows (%s) != unique identities (%s)"
                          % (len(rows), unique))

    # ── flag history persistence ──
    if not history.get("available"):
        findings.fail("flag history could not be read: %s"
                      % history.get("error", "unknown"))
    elif rows and history.get("rows_with_latest_flagged_at", 0) == 0:
        findings.fail("flagged terms exist but NO durable flag history is "
                      "persisted — the production write path "
                      "(scheduler/monthly.py -> record_flag_history) is not "
                      "running")
    elif not rows:
        findings.note("no flagged terms in this window — flag-history "
                      "persistence not exercised")


def collect(window: str) -> dict:
    from services.campaign_evidence_service import _window_bounds

    # Raises EvidenceWindowError for an unknown window.
    start, end, _ = _window_bounds(window, None)

    return {
        "window": window,
        "window_start": str(start) if start else None,
        "window_end": str(end),
        "canonical": _canonical_facts(start, end),
        "flagged": _flagged_population(window),
        "snapshot_proof": _snapshot_proof(start, end),
        "flag_history": _flag_history_state(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only PR-ADS-153D search-term waste validation gate")
    parser.add_argument("--window", default="30d",
                        help="Evidence window (default: 30d)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (still exits non-zero "
                             "when validation fails)")
    args = parser.parse_args()

    findings = _Findings()

    try:
        from db.connection import init_pool

        init_pool()
        data = collect(args.window)
    except Exception as exc:  # noqa: BLE001
        # Any database or import error is a FAILED audit, never a pass.
        if args.json:
            print(json.dumps({"ok": False, "window": args.window,
                              "failures": [f"audit could not run: {exc}"]},
                             indent=2))
        else:
            print(f"AUDIT FAILED — could not run: {exc}")
        return EXIT_VALIDATION_FAILED

    _validate(data, findings)
    exit_code = EXIT_OK if findings.ok else EXIT_VALIDATION_FAILED

    if args.json:
        print(json.dumps({"ok": findings.ok,
                          "failures": findings.failures,
                          "notes": findings.notes,
                          "data": data}, indent=2, default=str))
        return exit_code

    canonical = data["canonical"]
    flagged = data["flagged"]
    payload = flagged.get("payload") or {}
    kpis = payload.get("kpis") or {}
    rows = flagged.get("rows") or []
    proof = data["snapshot_proof"]
    history = data["flag_history"]

    print("=" * 72)
    print("PR-ADS-153D — SEARCH-TERM WASTE TRUTH (READ-ONLY MERGE GATE)")
    print(f"Evidence window: {data['window']}  "
          f"({data['window_start'] or 'no lower bound'} → {data['window_end']})")
    print("=" * 72)

    # ── 1 ────────────────────────────────────────────────────────────────────
    _section("1. CANONICAL SEARCH TERMS (search_terms — the ONE fact source)")
    if not canonical.get("available"):
        print(f"  Unavailable — {canonical.get('error', 'unknown')}")
    else:
        print(f"    {'unique terms':<30} {_fmt(canonical.get('unique_terms')):>16}")
        print(f"    {'unique durable identities':<30} {_fmt(canonical.get('unique_identities')):>16}")
        print(f"    {'spend (USD)':<30} {_fmt(canonical.get('spend_usd')):>16}")
        print(f"    {'clicks':<30} {_fmt(canonical.get('clicks')):>16}")
        print(f"    {'earliest source date':<30} {_fmt(canonical.get('earliest_source_date')):>16}")
        print(f"    {'latest source date':<30} {_fmt(canonical.get('latest_source_date')):>16}")

    # ── 2 ────────────────────────────────────────────────────────────────────
    _section("2. FLAGGED (Search Terms → Flagged, COMPLETE population)")
    truth = (payload.get("truth_state") or {}).get("status")
    if payload.get("actionable") is False:
        print(f"  QUARANTINED (truth_state = {truth}) — metrics withheld.")
        q = payload.get("quarantine") or {}
        print(f"  {q.get('detail', '')}")
    elif payload.get("db_unavailable"):
        print("  Unavailable — no flagged metrics can be verified.")
    else:
        unique = len({r.get("term_identity") for r in rows})
        print(f"    {'rows collected':<30} {_fmt(len(rows)):>16}")
        print(f"    {'unique durable identities':<30} {_fmt(unique):>16}")
        print(f"    {'KPI flagged terms':<30} {_fmt(kpis.get('flagged_terms')):>16}")
        print(f"    {'pagination total':<30} {_fmt((payload.get('pagination') or {}).get('total_count')):>16}")
        print(f"    {'flagged spend (USD)':<30} {_fmt(kpis.get('flagged_spend_usd')):>16}")
        print(f"    {'review needed':<30} {_fmt(kpis.get('review_needed')):>16}")
        resolved_count = sum(1 for r in rows
                             if r.get("review_state") in ("keep", "resolved"))
        print(f"    {'resolved / keep':<30} {_fmt(resolved_count):>16}")
        print(f"    {'truth state':<30} {_fmt(truth):>16}")
        join = payload.get("annotation_join") or {}
        print(f"    {'legacy_unresolved annotations':<30} {_fmt(join.get('legacy_unresolved')):>16}")

    # ── 3 ────────────────────────────────────────────────────────────────────
    _section("3. DUPLICATE-SNAPSHOT PROOF (the PR-ADS-153A defect)")
    if not proof.get("available"):
        print(f"  Unavailable — {proof.get('error', 'unknown')}")
    else:
        print(f"    {'waste_terms raw rows':<30} {_fmt(proof.get('raw_rows')):>16}")
        print(f"    {'distinct annotations':<30} {_fmt(proof.get('distinct_annotations')):>16}")
        print(f"    {'repeated rows':<30} {_fmt(proof.get('repeated_rows')):>16}")
        print(f"    {'distinct runs observed':<30} {_fmt(proof.get('distinct_runs')):>16}")
        print()
        naive = proof.get("naive_snapshot_spend_usd")
        print(f"    {'OLD page spend claim':<30} {_fmt(naive):>16}   ← Σ run snapshots")
        print(f"    {'canonical flagged spend':<30} {_fmt(kpis.get('flagged_spend_usd')):>16}   ← deduplicated facts")
        if naive is not None and kpis.get("flagged_spend_usd") is not None:
            print(f"    {'difference':<30} {_fmt(naive - float(kpis['flagged_spend_usd'])):>16}")

    # ── 4 ────────────────────────────────────────────────────────────────────
    _section("4. ATTRIBUTION (unavailable is never zero)")
    if payload.get("actionable") is False or payload.get("db_unavailable"):
        print("  Unavailable.")
    else:
        available = kpis.get("sql_evidence_available")
        print(f"    {'attributable SQLs':<30} "
              f"{_fmt(kpis.get('sql_evidence')) if available else 'Unavailable':>16}")
        print(f"    {'terms: attribution unavailable':<30} "
              f"{_fmt(kpis.get('terms_with_attribution_unavailable')):>16}")
        print(f"    {'terms: proven zero SQLs':<30} "
              f"{_fmt(kpis.get('terms_with_proven_zero_sqls')):>16}")
        print(f"  Scope: {kpis.get('sql_evidence_label')} — a strict subset of")
        print("  campaign-attributable ≤ Google Ads-source ≤ all-source lifecycle SQLs.")

    # ── 5 ────────────────────────────────────────────────────────────────────
    _section("5. FLAG HISTORY PERSISTENCE (production write path)")
    if not history.get("available"):
        print(f"  Unavailable — {history.get('error', 'unknown')}")
    else:
        print(f"    {'review rows':<30} {_fmt(history.get('review_rows')):>16}")
        print(f"    {'with first_flagged_at':<30} {_fmt(history.get('rows_with_first_flagged_at')):>16}")
        print(f"    {'with latest_flagged_at':<30} {_fmt(history.get('rows_with_latest_flagged_at')):>16}")
        print(f"    {'newest observation':<30} {_fmt(history.get('newest_flag_observation')):>16}")

    # ── verdict ──────────────────────────────────────────────────────────────
    _section("VERDICT")
    for note in findings.notes:
        print(f"  note: {note}")
    if findings.ok:
        print("  PASS — page rows, KPIs, pagination, review state and flag history")
        print("  reconcile.")
    else:
        print(f"  FAIL — {len(findings.failures)} validation failure(s):")
        for failure in findings.failures:
            print(f"    ✗ {failure}")

    print()
    print("=" * 72)
    print("Read-only audit complete. No writes and no external API calls were made.")
    print(f"exit={exit_code}")
    print("=" * 72)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
