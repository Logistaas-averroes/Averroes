#!/usr/bin/env python3
"""
scripts/audit_cross_page_canonical_parity.py

PR-ADS-154C — prove every production page computes the same metric the same way.

    python -m scripts.audit_cross_page_canonical_parity --json
    python -m scripts.audit_cross_page_canonical_parity --window ytd
    echo $?     # 0 = full parity, 1 = violations found, 2 = usage error

What it asserts
---------------
For every required business window, and for each METRIC IDENTITY, that every
consumer publishes the identical value — and that each consumer resolved the same
window key to the same date range, read a canonical source, and used no fallback.

Identical, not "within tolerance". A tolerance on a cross-page check answers the
question "are these close enough to ignore", which is how disagreements survive.
Metrics that are genuinely different — total business revenue versus
country-attributed revenue, campaign spend versus country-attributed spend — are
registered as distinct by design and never compared, so a real difference is
information rather than a bug report.

Read-only. It builds the same service payloads the API serves, contacts no
external platform, and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _render(outcome: dict) -> None:
    print("\n" + "=" * 74)
    print("CROSS-PAGE CANONICAL PARITY AUDIT")
    print("=" * 74)
    for result in outcome["results"]:
        mark = "OK  " if result["ok"] else "FAIL"
        print(f"\n[{mark}] {result['window']}  "
              f"{result['window_start']} .. {result['window_end']}  "
              f"({result['timezone']})")

        ranges = {(r["window_start"], r["window_end"], r["timezone"])
                  for r in result["consumer_windows"] if r["window_end"]}
        # PR-ADS-154C-F2: a count of consumers BUILT said nothing about how many
        # were actually certified — the audit built seven and certified four, and
        # reported seven. Both numbers are printed now.
        cert = result.get("consumer_certification") or []
        certified = sum(1 for c in cert if c.get("certified"))
        print(f"       consumers: {len(result['consumers_inspected'])} built, "
              f"{certified}/{len(cert)} certified, "
              f"distinct window ranges: {len(ranges)}"
              + ("" if len(ranges) <= 1 else "   <-- CONSUMERS DISAGREE ON THE RANGE"))
        for c in cert:
            if not c.get("certified"):
                print(f"             NOT CERTIFIED {c['consumer']:28} "
                      f"0 of {c['identities_registered']} registered identities")

        for m in result["metrics"]:
            # PR-ADS-154C-F1: `.get(status, "·")`, not `[status]`. This indexed
            # a three-entry map and raised KeyError on `unproven` — crashing on
            # precisely the failure the human-readable form exists to explain,
            # and it would do the same for any status added later. An unknown
            # status now renders as a neutral marker beside its own name, which
            # is always readable even when this renderer has not been taught
            # about it.
            symbol = {"identical": "=", "mismatch": "!",
                      "unavailable": "?", "unproven": "~",
                      # PR-ADS-154C-F3: a value published over a source its own
                      # contract calls unavailable. Deliberately its own marker —
                      # it is not a disagreement, and reading it as one is how it
                      # survived.
                      "published_over_unavailable_source": "X"}.get(m["status"], "·")
            print(f"       [{symbol}] {m['metric']:38} {m['status']:34} {m['value']}")
            if m["status"] != "identical":
                for r in m["readings"]:
                    print(f"             {r['consumer']:28} {r['path']:34} {r['value']}")
                    # Say WHY, from the consumer's own declaration, rather than
                    # sending the reader to the database to find out what the
                    # page already knew.
                    bits = []
                    if r.get("truth_status"):
                        bits.append(f"status={r['truth_status']}")
                    if r.get("declared_source"):
                        bits.append(f"source={r['declared_source']}")
                    if r.get("unavailable_reason"):
                        bits.append(f"reason={r['unavailable_reason']}")
                    if r.get("violation_codes"):
                        bits.append(f"codes={','.join(r['violation_codes'])}")
                    if r.get("fallback_used") or r.get("legacy_fallback_used"):
                        bits.append("FALLBACK ATTEMPTED")
                    if bits:
                        print(f"               {'  '.join(bits)}")
                    problem = r.get("contract_problem")
                    if problem:
                        print(f"               contract: {problem}")

        # Split identities that must add back up to their total.
        for c in result.get("conservation") or []:
            mark = {"conserved": "=", "broken": "!"}.get(c["status"], "·")
            parts = " + ".join(f"{p}={v}" for p, v in
                               zip(c["parts"], c["part_values"]))
            print(f"       [{mark}] conservation  {parts} vs {c['total']}="
                  f"{c['total_value']}  ({c['status']})")
            if c["status"] == "broken":
                print(f"             {c['detail']}")

        # PR-ADS-155 §7. Quantified source-system gaps, printed separately from
        # violations. Partial lifecycle history is a disclosed fact about
        # HubSpot's records, not a disagreement between pages, and printing it
        # under VIOLATION would make it indistinguishable from one.
        for c in result.get("coverage_disclosures") or []:
            print(f"       [~] coverage    {c['consumer']}: {c['code']} "
                  f"(cohort={c.get('cohort_size')}, "
                  f"excluded={c.get('excluded_contacts')})")
            for r in c.get("exclusion_reasons") or []:
                print(f"             {r['contacts']}x {r['reason']} "
                      f"[{r['population']}]")

        for v in result["violations"]:
            where = v.get("metric") or v.get("consumer") or "-"
            print(f"       VIOLATION {v['code']}  ({where}): {v.get('detail','')}")

    # PR-ADS-154C-F2 §5. Pages that publish overlapping executive-looking figures
    # and are NOT certified. Printed rather than omitted: a page absent from a
    # parity report reads as a page with nothing to answer for.
    uncertified = outcome.get("uncertified_consumers") or []
    if uncertified:
        print("\n" + "-" * 74)
        print("  EXPLICITLY UNCERTIFIED (not audited for parity, not authoritative)")
        for u in uncertified:
            print(f"    {u['consumer']}  [{u['classification']}]")
            print(f"      overlapping metrics: {', '.join(u['overlapping_metrics'])}")
            print(f"      surfaces: {', '.join(u['services'])}")
            print(f"      {u['note']}")

    print("\n" + "-" * 74)
    if outcome["ok"]:
        print("  RESULT: every consumer agrees, on every window, from canonical")
        print("  sources, with no fallback. Metrics that differ by design are")
        print("  registered as such and were not compared.")
    else:
        print(f"  RESULT: {len(outcome['violations'])} violation(s): "
              f"{', '.join(outcome['violation_codes'])}")
        print("  A value mismatch means two pages answer the same question")
        print("  differently. A window mismatch means they answered different")
        print("  questions while using the same window name. An identity")
        print("  registered as distinct by design is never compared at all.")
        print("  `value_published_while_source_unavailable` is the worst of")
        print("  them: a page published a number its own contract says it had")
        print("  no source for, and two such pages can agree perfectly.")
        print("")
        print("  PR-ADS-155 §7 — an absent metric names WHICH problem it is,")
        print("  because four different people fix these four things:")
        print("    canonical_database_unreadable")
        print("        the canonical store could not be read — an engineer.")
        print("    revenue_population_unavailable")
        print("        readable, but this window's coverage is unproven — a backfill.")
        print("    revenue_total_unpublishable_missing_amount")
        print("        the population is COMPLETE; its total is unknown because")
        print("        closed-won deals in it carry no amount. Fixed by pricing")
        print("        those deals in HubSpot — see")
        print("        `python -m scripts.report_missing_deal_amounts`.")
        print("    canonical_source_unavailable")
        print("        the catch-all: nobody published it and the contracts do")
        print("        not say why. Still a violation — an absence we cannot")
        print("        explain is not an absence we have accounted for.")
    print("=" * 74 + "\n")


def main() -> int:
    from analysis.business_windows import WINDOW_KEYS

    parser = argparse.ArgumentParser(
        description=("Cross-page canonical parity audit. Read-only: builds the same "
                     "service payloads the API serves and writes nothing."))
    parser.add_argument("--window", action="append", default=None,
                        help=f"window to audit, repeatable (default: all of "
                             f"{', '.join(WINDOW_KEYS)})")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    windows = args.window or list(WINDOW_KEYS)
    unknown = [w for w in windows if w not in WINDOW_KEYS]
    if unknown:
        print(f"unknown window(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    try:
        from db.connection import init_pool
        init_pool()
    except Exception as exc:  # noqa: BLE001
        # Never interpolate a raw exception into operator output: a connection
        # failure carries the DSN, and a DSN carries the password (PR-ADS-154A).
        from db.writers import safe_db_error
        print(f"database unavailable: {safe_db_error(exc)}", file=sys.stderr)
        return 1

    from services.cross_page_parity_service import audit_all_windows
    outcome = audit_all_windows(windows)

    if args.json:
        print(json.dumps(outcome, indent=2, default=str))
    else:
        _render(outcome)

    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
