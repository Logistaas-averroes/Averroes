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
        print(f"       consumers: {len(result['consumers_inspected'])}, "
              f"distinct window ranges: {len(ranges)}"
              + ("" if len(ranges) <= 1 else "   <-- CONSUMERS DISAGREE ON THE RANGE"))

        for m in result["metrics"]:
            # PR-ADS-154C-F1: `.get(status, "·")`, not `[status]`. This indexed
            # a three-entry map and raised KeyError on `unproven` — crashing on
            # precisely the failure the human-readable form exists to explain,
            # and it would do the same for any status added later. An unknown
            # status now renders as a neutral marker beside its own name, which
            # is always readable even when this renderer has not been taught
            # about it.
            symbol = {"identical": "=", "mismatch": "!",
                      "unavailable": "?", "unproven": "~"}.get(m["status"], "·")
            print(f"       [{symbol}] {m['metric']:38} {m['status']:12} {m['value']}")
            if m["status"] != "identical":
                for r in m["readings"]:
                    print(f"             {r['consumer']:28} {r['path']:34} {r['value']}")
                    problem = r.get("contract_problem")
                    if problem:
                        print(f"               contract: {problem}")

        for v in result["violations"]:
            where = v.get("metric") or v.get("consumer") or "-"
            print(f"       VIOLATION {v['code']}  ({where}): {v.get('detail','')}")

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
        print("  questions while using the same window name.")
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
