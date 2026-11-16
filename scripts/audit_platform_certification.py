"""PR-ADS-161G — one-command platform certification (read-only).

Runs the six production truth audits and prints ONE verdict:

    python -m scripts.audit_platform_certification
    python -m scripts.audit_platform_certification --json

It adds no gate logic of its own. Each audit runs unchanged, in its own
subprocess, with ``--json``; this script only reads the exit code and the
report each one prints, and combines them:

    exit 0  CERTIFIED     every audit exited 0 and printed a consistent report
    exit 1  VIOLATION     at least one audit found a truth-contract violation
    exit 2  NOT CERTIFIED nothing violated, but at least one audit could not
                          finish (unavailable) or did not produce a report we
                          can trust (crashed, timed out, unknown exit code,
                          unparseable output, or a report whose own exit_code
                          disagrees with the process exit code)

Fail closed: an audit's exit code alone is not enough. Two audits let an
unexpected exception escape, and Python reports that as exit 1, which would
read as a violation; an exit 0 with no readable report is not evidence that
anything holds. Either case is ``crashed``, and a crash never certifies.

CERTIFIED means no surface states a number the evidence does not support. It
does NOT mean every figure is published: a withheld figure is a correct
answer. The "CRM data to fix" block lists the won deals that keep figures
withheld (from the CRM Truth Exceptions audit's own report) so they can be
handed to sales; the full list is in the CRM Truth Exceptions workbench.

No writes, no external API calls: each audit states the same about itself.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXIT_CERTIFIED = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT_SECONDS = 900

# (key, module, what an exit 0 from it proves). Order is the order they run
# and print in. Adding an audit here is the only way to add it to the verdict.
AUDITS = (
    ("reader_cutover", "scripts.audit_customer_revenue_reader_cutover",
     "every revenue/customer reader uses the canonical owner"),
    ("closed_won_truth", "scripts.audit_customer_closed_won_truth",
     "closed-won deals, customers and revenue match the ledger"),
    ("deal_company_associations",
     "scripts.audit_deal_company_association_coverage",
     "customer identity (deal -> company) is proven before it publishes"),
    ("crm_truth_exceptions", "scripts.audit_crm_truth_exceptions",
     "every withheld figure names the deals that block it"),
    ("marketing_outcome_cohorts", "scripts.audit_marketing_outcome_cohorts",
     "Campaign Evidence SQL cohorts reconcile or are withheld"),
    ("campaign_evidence", "scripts.audit_campaign_evidence_certification",
     "Campaign Evidence spend and outcomes are certified"),
)

HOLDS, VIOLATION, UNAVAILABLE, CRASHED = (
    "holds", "violation", "unavailable", "crashed")
_OUTCOME_BY_EXIT = {EXIT_CERTIFIED: HOLDS, EXIT_VIOLATION: VIOLATION,
                    EXIT_UNAVAILABLE: UNAVAILABLE}


def _run_subprocess(module: str, timeout: float):
    """Run one audit. Returns (returncode | None, stdout, stderr, error)."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", module, "--json"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, "", "", f"timed out after {timeout:g}s"
    except OSError as exc:
        return None, "", "", f"could not start: {exc}"
    return proc.returncode, proc.stdout, proc.stderr, None


def _messages(value, limit: int = 5) -> list:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v)[:300] for v in items[:limit]]


def classify(returncode, stdout: str, stderr: str = "",
             error: str | None = None) -> dict:
    """Turn one audit's process result into an outcome. Fail closed."""
    def crashed(detail):
        tail = (stderr or "").strip().splitlines()[-3:]
        return {"outcome": CRASHED, "exit_code": returncode, "report": None,
                "detail": detail, "stderr_tail": tail}

    if error:
        return crashed(error)
    if returncode not in _OUTCOME_BY_EXIT:
        return crashed(f"unrecognised exit code {returncode}")
    try:
        report = json.loads(stdout)
    except (TypeError, ValueError):
        return crashed(f"exit {returncode} without a readable JSON report")
    if not isinstance(report, dict):
        return crashed(f"exit {returncode} with a non-object JSON report")
    declared = report.get("exit_code")
    if declared is not None and declared != returncode:
        return crashed(f"report says exit_code={declared!r} but the process "
                       f"exited {returncode}")
    return {"outcome": _OUTCOME_BY_EXIT[returncode], "exit_code": returncode,
            "report": report,
            "violations": _messages(report.get("violations")),
            "unavailable": ((_messages(report.get("unavailable"))
                             or _messages(report.get("detail")))
                            if returncode == EXIT_UNAVAILABLE else [])}


def combine(results: list) -> tuple:
    """(exit_code, verdict) for the platform. Precedence: a violation is
    reported first; then anything that did not hold; only all-holds
    certifies. An empty result list certifies nothing."""
    outcomes = [r["outcome"] for r in results]
    if not outcomes:
        return EXIT_UNAVAILABLE, "NOT CERTIFIED"
    if VIOLATION in outcomes:
        return EXIT_VIOLATION, "VIOLATION"
    if all(o == HOLDS for o in outcomes):
        return EXIT_CERTIFIED, "CERTIFIED"
    return EXIT_UNAVAILABLE, "NOT CERTIFIED"


def crm_data_to_fix(results: list):
    """The won deals that keep figures withheld, read from the CRM Truth
    Exceptions audit's own report. None when that report is unreadable —
    never an empty list standing in for 'we could not look'."""
    r = next((x for x in results if x["key"] == "crm_truth_exceptions"),
             None)
    report = (r or {}).get("report")
    if not isinstance(report, dict) or not report:
        return None
    # The workbench can be unavailable (e.g. the deal sync is overdue) and
    # still name its dataset blockers; deal counts are then unknown (None).
    totals = report.get("totals") if isinstance(report.get("totals"),
                                                 dict) else {}
    return {"deals_with_any_exception": totals.get("deals_with_any_exception"),
            "by_family": {f: {"deals": t.get("deals"),
                              "by_reason": t.get("by_reason")}
                          for f, t in (totals.get("by_family") or {}).items()},
            "dataset_blockers": report.get("dataset_blockers") or [],
            "windows": [{"window": w.get("window"),
                         "customers": (w.get("customers") or {}).get("status"),
                         "revenue_usd": (w.get("revenue_usd") or {}).get(
                             "status")}
                        for w in report.get("windows") or []]}


def run(*, timeout: float = DEFAULT_TIMEOUT_SECONDS, runner=None,
        audits=AUDITS) -> dict:
    runner = runner or _run_subprocess
    results = []
    for key, module, proves in audits:
        started = time.monotonic()
        rc, out, err, error = runner(module, timeout)
        res = classify(rc, out, err, error)
        res.update(key=key, module=module, proves=proves,
                   seconds=round(time.monotonic() - started, 1))
        results.append(res)
    exit_code, verdict = combine(results)
    return {"audit": "platform_certification",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "verdict": verdict, "exit_code": exit_code,
            "external_writes_performed": False,
            "database_writes_performed": False,
            "audits": results,
            "crm_data_to_fix": crm_data_to_fix(results)}


_MARK = {HOLDS: "PASS", VIOLATION: "FAIL", UNAVAILABLE: "UNAVAILABLE",
         CRASHED: "CRASHED"}


def _render(report: dict) -> None:
    print("=" * 78)
    print(f"  PLATFORM CERTIFICATION: {report['verdict']}"
          f"   (exit {report['exit_code']})")
    print("  read-only: no database writes, no external API calls")
    print("=" * 78)
    for r in report["audits"]:
        print(f"  {_MARK[r['outcome']]:<12}{r['key']:<28}{r['seconds']:>6}s"
              f"  {r['proves']}")
        for v in r.get("violations") or []:
            print(f"  {'':<12}  violation: {v}")
        for u in r.get("unavailable") or []:
            print(f"  {'':<12}  unavailable: {u}")
        if r["outcome"] == CRASHED:
            print(f"  {'':<12}  {r['detail']}")
            for line in r.get("stderr_tail") or []:
                print(f"  {'':<12}  | {line[:200]}")
        if r["outcome"] != HOLDS:
            print(f"  {'':<12}  details: python -m {r['module']}")

    fix = report.get("crm_data_to_fix")
    print()
    print("  CRM data to fix (won deals that keep figures withheld)")
    if fix is None:
        print("    unknown — the CRM Truth Exceptions audit produced no "
              "readable report")
    else:
        n = fix["deals_with_any_exception"]
        print(f"    deals with any exception: "
              f"{'unknown' if n is None else n}")
        for fam, t in fix["by_family"].items():
            d = "unknown" if t["deals"] is None else t["deals"]
            print(f"    {fam:<24}{d} deal(s)  {t['by_reason']}")
        for b in fix["dataset_blockers"]:
            print(f"    dataset blocker: {b.get('code')} ({b.get('detail')})")
        for w in fix["windows"]:
            print(f"    {str(w['window']):<24}customers {w['customers']}, "
                  f"revenue {w['revenue_usd']}")
        print("    full list: CRM Truth Exceptions workbench "
              "(admin → View blockers)")
    print()
    if report["exit_code"] == EXIT_CERTIFIED:
        print("  CERTIFIED: no surface states a number the evidence does not "
              "support.\n  Withheld figures above are correct answers, not "
              "failures.")
    elif report["exit_code"] == EXIT_VIOLATION:
        print("  VIOLATION: a surface may be stating an unsupported number. "
              "Do not trust\n  the dashboard until the failing audit passes.")
    else:
        print("  NOT CERTIFIED: no violation was found, but not every audit "
              "finished.\n  Unknown is not a pass.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the six read-only truth audits; print one verdict")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    parser.add_argument("--timeout", type=float,
                        default=DEFAULT_TIMEOUT_SECONDS,
                        help="seconds allowed per audit (default %(default)s)")
    args = parser.parse_args(argv)
    report = run(timeout=args.timeout)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
