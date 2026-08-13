#!/usr/bin/env python3
"""
scripts/audit_search_term_waste_truth.py

PR-ADS-153D §47 — READ-ONLY production validation for the consolidated
search-term waste experience.

Run on Render AFTER deployment:

    python -m scripts.audit_search_term_waste_truth

Optional:
    --window 30d      evidence window (7d|14d|30d|60d|180d|all_time; default 30d)
    --json            machine-readable output

Guarantees
----------
  * NO writes of any kind — every query is a SELECT, and no service called here
    has a write path;
  * NO external API calls — Google Ads, HubSpot and Mailchimp are never
    contacted; every number comes from the local database;
  * NO email addresses or other contact PII are printed.

What it proves
--------------
  1. Canonical search-term facts for the window (unique terms, spend, clicks,
     source-date range) straight from ``search_terms``.
  2. The flagged population: unique currently-flagged terms, flagged spend,
     review-needed and resolved counts.
  3. DUPLICATE-SNAPSHOT PROOF — the heart of §10. Compares the raw
     ``waste_terms`` run-snapshot rows against the canonical deduplicated fact
     count and shows the spend the old page would have reported versus the
     canonical spend, so the double-count is visible rather than asserted.
  4. Attribution: attributable SQLs, terms with attribution unavailable, and
     terms with a PROVEN zero — the three are never conflated.
  5. Action Queue: active search-term waste actions and any duplicate durable
     identity (which would break the one-term-one-item rule).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    """Canonical ``search_terms`` totals for the window. SELECT only."""
    from db.connection import get_conn

    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)                             AS fact_rows,
                           COUNT(DISTINCT search_term)          AS unique_terms,
                           COUNT(DISTINCT (COALESCE(campaign_id, campaign_name, '')
                                           || '|' || search_term)) AS unique_identities,
                           SUM(spend_usd)                       AS spend_usd,
                           SUM(clicks)                          AS clicks,
                           SUM(impressions)                     AS impressions,
                           MIN(source_date)                     AS earliest_source_date,
                           MAX(source_date)                     AS latest_source_date,
                           COUNT(DISTINCT source_date)          AS distinct_source_dates
                    FROM search_terms
                    WHERE (%s::date IS NULL OR source_date >= %s)
                      AND source_date <= %s
                    """,
                    (start, start, end),
                )
                cols = [d[0] for d in cur.description]
                row = dict(zip(cols, cur.fetchone()))
        row["available"] = True
        for key in ("spend_usd",):
            if row.get(key) is not None:
                row[key] = float(row[key])
        return row
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}


def _snapshot_proof(start, end) -> dict:
    """The duplicate-snapshot evidence (§47 "Duplicate-snapshot proof").

    ``waste_terms`` is run-grained. This reports how many raw rows exist, how
    many DISTINCT (term, campaign) annotations they represent, and what naive
    ``SUM(spend_usd)`` over those snapshots would have claimed — the number the
    retired page published.
    """
    from db.connection import get_conn

    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False}
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


def _queue_items(window_days: int) -> dict:
    """Active search-term waste actions, straight from the queue builder."""
    from api.server import _build_waste_queue_items  # noqa: PLC0415

    try:
        items = _build_waste_queue_items(None, window_days, 100.0)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}

    identities = [(i.get("evidence") or {}).get("term_identity") for i in items]
    seen, duplicates = set(), []
    for ident in identities:
        if ident in seen:
            duplicates.append(ident)
        seen.add(ident)
    return {"available": True, "count": len(items),
            "duplicate_identities": duplicates,
            "unique_identities": len(seen)}


def collect(window: str) -> dict:
    from analysis.evidence_windows import resolve_evidence_window
    from services.search_term_evidence_service import (
        build_flagged_search_terms,
    )
    from services.campaign_evidence_service import _window_bounds

    resolved = resolve_evidence_window(window)
    start, end, _ = _window_bounds(window, None)

    flagged = build_flagged_search_terms(window, page=1, page_size=200,
                                         sort="priority")
    return {
        "window": window,
        "window_start": str(start) if start else None,
        "window_end": str(end),
        "canonical": _canonical_facts(start, end),
        "flagged": flagged,
        "snapshot_proof": _snapshot_proof(start, end),
        "queue": _queue_items(resolved["days"] or 365),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only PR-ADS-153D search-term waste validation")
    parser.add_argument("--window", default="30d",
                        help="Evidence window (default: 30d)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output")
    args = parser.parse_args()

    from db.connection import init_pool

    init_pool()
    data = collect(args.window)

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    canonical = data["canonical"]
    flagged = data["flagged"]
    kpis = flagged.get("kpis") or {}
    proof = data["snapshot_proof"]
    queue = data["queue"]

    print("=" * 72)
    print("PR-ADS-153D — SEARCH-TERM WASTE TRUTH (READ-ONLY)")
    print(f"Evidence window: {data['window']}  "
          f"({data['window_start'] or 'no lower bound'} → {data['window_end']})")
    print("=" * 72)

    # ── 1 ────────────────────────────────────────────────────────────────────
    _section("1. CANONICAL SEARCH TERMS (search_terms — the ONE fact source)")
    if not canonical.get("available"):
        print("  Unavailable — the canonical fact source could not be read.")
    else:
        print(f"    {'unique terms':<28} {_fmt(canonical.get('unique_terms')):>16}")
        print(f"    {'unique term identities':<28} {_fmt(canonical.get('unique_identities')):>16}")
        print(f"    {'spend (USD)':<28} {_fmt(canonical.get('spend_usd')):>16}")
        print(f"    {'clicks':<28} {_fmt(canonical.get('clicks')):>16}")
        print(f"    {'impressions':<28} {_fmt(canonical.get('impressions')):>16}")
        print(f"    {'earliest source date':<28} {_fmt(canonical.get('earliest_source_date')):>16}")
        print(f"    {'latest source date':<28} {_fmt(canonical.get('latest_source_date')):>16}")
        print(f"    {'fact rows':<28} {_fmt(canonical.get('fact_rows')):>16}")

    # ── 2 ────────────────────────────────────────────────────────────────────
    _section("2. FLAGGED (Search Terms → Flagged)")
    if flagged.get("db_unavailable"):
        print("  Unavailable — no flagged metrics can be verified.")
    else:
        print(f"    {'unique flagged terms':<28} {_fmt(kpis.get('flagged_terms')):>16}")
        print(f"    {'flagged spend (USD)':<28} {_fmt(kpis.get('flagged_spend_usd')):>16}")
        print(f"    {'review needed':<28} {_fmt(kpis.get('review_needed')):>16}")
        resolved_count = sum(
            1 for r in (flagged.get("rows") or [])
            if r.get("review_state") in ("keep", "resolved"))
        print(f"    {'resolved / keep (page)':<28} {_fmt(resolved_count):>16}")
        truth = flagged.get("truth_state") or {}
        print(f"    {'truth state':<28} {_fmt(truth.get('status')):>16}")
        if truth.get("reasons"):
            print(f"      reasons: {', '.join(truth['reasons'])}")
        join = flagged.get("annotation_join") or {}
        print(f"    {'annotation rows':<28} {_fmt(join.get('annotation_rows')):>16}")
        print(f"    {'annotations attached':<28} {_fmt(join.get('attached')):>16}")
        print(f"    {'legacy_unresolved':<28} {_fmt(join.get('legacy_unresolved')):>16}")

    # ── 3 ────────────────────────────────────────────────────────────────────
    _section("3. DUPLICATE-SNAPSHOT PROOF (the PR-ADS-153A defect)")
    if not proof.get("available"):
        print("  Unavailable — waste_terms could not be read.")
    else:
        print(f"    {'waste_terms raw rows':<28} {_fmt(proof.get('raw_rows')):>16}")
        print(f"    {'distinct annotations':<28} {_fmt(proof.get('distinct_annotations')):>16}")
        print(f"    {'repeated rows':<28} {_fmt(proof.get('repeated_rows')):>16}")
        print(f"    {'distinct runs observed':<28} {_fmt(proof.get('distinct_runs')):>16}")
        print()
        naive = proof.get("naive_snapshot_spend_usd")
        print(f"    {'OLD page spend claim':<28} {_fmt(naive):>16}"
              "   ← SUM over run snapshots")
        print(f"    {'canonical flagged spend':<28} {_fmt(kpis.get('flagged_spend_usd')):>16}"
              "   ← deduplicated facts")
        if naive is not None and kpis.get("flagged_spend_usd") is not None:
            diff = naive - float(kpis["flagged_spend_usd"])
            print(f"    {'difference':<28} {_fmt(diff):>16}")
        print()
        if (proof.get("repeated_rows") or 0) > 0:
            print("  Repeated snapshot rows EXIST in this window. The canonical")
            print("  figures above are unaffected by them: spend, clicks and term")
            print("  counts come from `search_terms`, whose unique fact index makes")
            print("  re-ingestion an upsert. waste_terms contributes classification")
            print("  annotations only and is never summed.")
        else:
            print("  No repeated snapshot rows in this window. The canonical figures")
            print("  would be unaffected either way — they never read waste_terms")
            print("  for spend, clicks or impressions.")

    # ── 4 ────────────────────────────────────────────────────────────────────
    _section("4. ATTRIBUTION (unavailable is never zero)")
    if flagged.get("db_unavailable"):
        print("  Unavailable.")
    else:
        available = kpis.get("sql_evidence_available")
        print(f"    {'attributable SQLs':<28} "
              f"{_fmt(kpis.get('sql_evidence')) if available else 'Unavailable':>16}")
        print(f"    {'terms: attribution unavailable':<28} "
              f"{_fmt(kpis.get('terms_with_attribution_unavailable')):>16}")
        print(f"    {'terms: proven zero SQLs':<28} "
              f"{_fmt(kpis.get('terms_with_proven_zero_sqls')):>16}")
        print()
        print("  'Attribution unavailable' and 'proven zero' are reported separately")
        print("  and never summed together. A term with spend and unavailable")
        print("  attribution is NOT equivalent to one with a proven zero, and it")
        print("  contributes nothing to review priority.")
        print(f"  Scope: {kpis.get('sql_evidence_label')} — a strict subset of")
        print("  campaign-attributable ≤ Google Ads-source ≤ all-source lifecycle SQLs.")

    # ── 5 ────────────────────────────────────────────────────────────────────
    _section("5. ACTION QUEUE (one durable term → one action)")
    if not queue.get("available"):
        print(f"  Unavailable — {queue.get('error', 'queue could not be built')}")
    else:
        print(f"    {'active waste actions':<28} {_fmt(queue.get('count')):>16}")
        print(f"    {'unique durable identities':<28} {_fmt(queue.get('unique_identities')):>16}")
        dupes = queue.get("duplicate_identities") or []
        print(f"    {'duplicate identities':<28} {_fmt(len(dupes)):>16}")
        if dupes:
            print("  ⚠ Duplicate durable identities found — the one-term-one-item")
            print("    invariant is broken. Investigate before trusting the queue.")
            for ident in dupes[:10]:
                print(f"      {ident}")
        else:
            print("  Every active action maps to a distinct durable search-term")
            print("  identity. Repeated sync runs cannot add another item for a")
            print("  term that is already queued.")

    print()
    print("=" * 72)
    print("Read-only audit complete. No writes and no external API calls were made.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
