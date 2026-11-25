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

PR-ADS-169A — memory. Each audit runs in a FRESH interpreter
(``python -m scripts.certification_child --memory-mb N <audit> --json``),
sequentially, never two at once. It is started only within the headroom that
is free when it starts (instance limit − current usage − safety margin; below
``min_audit_budget_mb`` it is not started and is ``crashed`` "insufficient
memory headroom"), the parent SIGKILLs it past that resident budget
("exceeded its memory budget (N MB resident)"), and its address space is
capped at ``audit_address_space_mb`` as a backstop. Any of these makes the run
NOT CERTIFIED. Each audit's budget, its inputs and its peak memory are
recorded beside its outcome.
"""
from __future__ import annotations

import argparse
import contextvars
import json
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

EXIT_CERTIFIED = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT_SECONDS = 900
#: Fallbacks when config/thresholds.yaml cannot be read (PR-ADS-169A).
DEFAULT_INSTANCE_MEMORY_MB = 512
DEFAULT_SAFETY_MARGIN_MB = 64
DEFAULT_MIN_AUDIT_BUDGET_MB = 96
DEFAULT_ADDRESS_SPACE_MB = 320
DEFAULT_HEARTBEAT_SECONDS = 60
#: How often the parent reads the child's resident memory.
WATCHDOG_POLL_SECONDS = 0.25
#: A run whose peak address space came this close to the BACKSTOP is not
#: trusted: an allocation may have failed inside an audit's own exception
#: handler, which then reports on a read it never completed.
MEMORY_GUARD_MB = 16
#: A cgroup v1 "limit" this large is the kernel's way of saying "no limit".
_CGROUP_UNLIMITED = 1 << 60
CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
CGROUP_V2_CURRENT = Path("/sys/fs/cgroup/memory.current")
CGROUP_V1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
CGROUP_V1_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
CHILD_MODULE = "scripts.certification_child"

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
     # PR-ADS-177A-F1 (MINOR 7): since PR-ADS-177A this audit covers every
     # mart / Dashboard surface of the one SQL count, not Campaign Evidence
     # alone.
     # PR-ADS-177C: Campaign Evidence is retired; the per-window independent
     # reconciliation now certifies the Campaigns page's settled view.
     "The acquisition cohort (the Campaigns page and every mart / Dashboard "
     "leads and SQLs count) reconciles, conserves and discloses staleness, "
     "or is withheld"),
    # PR-ADS-177C: replaces ("campaign_evidence",
    # "scripts.audit_campaign_evidence_certification"), which certified the
    # retired Campaign Evidence page. Its checks are ported onto the live
    # Campaigns surfaces (docs/60 §9.4).
    ("campaigns", "scripts.audit_campaigns_page_certification",
     "The Campaigns page, its drawer and Dashboard → Campaigns are certified"),
)

HOLDS, VIOLATION, UNAVAILABLE, CRASHED = (
    "holds", "violation", "unavailable", "crashed")
_OUTCOME_BY_EXIT = {EXIT_CERTIFIED: HOLDS, EXIT_VIOLATION: VIOLATION,
                    EXIT_UNAVAILABLE: UNAVAILABLE}


def load_settings() -> dict:
    """``platform_certification`` from config/thresholds.yaml, with fallbacks."""
    out = {"instance_memory_mb": DEFAULT_INSTANCE_MEMORY_MB,
           "safety_margin_mb": DEFAULT_SAFETY_MARGIN_MB,
           "min_audit_budget_mb": DEFAULT_MIN_AUDIT_BUDGET_MB,
           "audit_address_space_mb": DEFAULT_ADDRESS_SPACE_MB,
           "audit_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
           "heartbeat_seconds": DEFAULT_HEARTBEAT_SECONDS}
    try:
        import yaml  # noqa: PLC0415
        with open(REPO_ROOT / "config" / "thresholds.yaml", encoding="utf-8") as fh:
            cfg = (yaml.safe_load(fh) or {}).get("platform_certification") or {}
        for key in out:
            if cfg.get(key) is not None:
                out[key] = type(out[key])(cfg[key])
    except Exception:  # noqa: BLE001 — the fallbacks are the documented values
        pass
    return out


def parse_child_marker(stderr: str) -> dict:
    """The last ``CERTIFICATION_CHILD`` line the child wrote, as a dict."""
    from scripts.certification_child import MARKER  # noqa: PLC0415
    fields: dict = {}
    for line in (stderr or "").splitlines():
        if line.startswith(MARKER + " "):
            fields = dict(part.split("=", 1) for part in line.split()[1:]
                          if "=" in part)
    return fields


def _mb_from_bytes_file(path: Path):
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None            # cgroup v2 "max": no limit
    value = int(raw)
    if value >= _CGROUP_UNLIMITED:
        return None            # cgroup v1's "no limit"
    return round(value / 2**20, 1)


def _status_kb(pid, field: str):
    """``field`` (e.g. ``VmRSS``) of /proc/<pid>/status in kB, or None."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def instance_memory_limit(settings: dict) -> tuple:
    """``(MB, source)``: the instance's memory limit — cgroup v2, else v1,
    else the configured ``instance_memory_mb``."""
    for path, source in ((CGROUP_V2_MAX, "cgroup_v2"), (CGROUP_V1_LIMIT, "cgroup_v1")):
        mb = _mb_from_bytes_file(path)
        if mb is not None:
            return mb, source
    return float(settings["instance_memory_mb"]), "config"


def instance_memory_usage(limit_source: str) -> tuple:
    """``(MB, source)``: what the instance uses now — from the cgroup whose
    limit was read, else this process's own resident memory."""
    path = {"cgroup_v2": CGROUP_V2_CURRENT, "cgroup_v1": CGROUP_V1_USAGE}.get(limit_source)
    if path is not None:
        mb = _mb_from_bytes_file(path)
        if mb is not None:
            return mb, limit_source
    kb = _status_kb("self", "VmRSS")
    return (round(kb / 1024, 1) if kb is not None else None), "parent_vmrss"


def derive_budget(settings: dict, *, limit_reader=None, usage_reader=None) -> dict:
    """The resident budget one audit may use, derived when it starts.

    limit − current usage − safety margin. The readers are injectable for
    tests; an unreadable usage gives no budget (None), never a guessed one.
    """
    limit_mb, limit_source = (limit_reader or instance_memory_limit)(settings)
    usage_mb, usage_source = (usage_reader or instance_memory_usage)(limit_source)
    margin = float(settings["safety_margin_mb"])
    budget = None
    if limit_mb is not None and usage_mb is not None:
        budget = int(limit_mb - usage_mb - margin)
    return {"budget_mb": budget, "limit_mb": limit_mb, "limit_source": limit_source,
            "usage_mb": usage_mb, "usage_source": usage_source,
            "safety_margin_mb": margin,
            "floor_mb": float(settings["min_audit_budget_mb"])}


def _memory_facts(stderr: str, budget: dict, address_space_mb, *,
                  watchdog_rss_mb=None, max_rss_seen_mb=None) -> dict:
    m = parse_child_marker(stderr)

    def mb(key):
        try:
            return round(int(m[key]) / 1024, 1)
        except (KeyError, TypeError, ValueError):
            return None
    return {**budget, "address_space_mb": address_space_mb,
            "memory_error": m.get("memory_error") == "1",
            "watchdog_killed": watchdog_rss_mb is not None,
            "killed_at_rss_mb": watchdog_rss_mb,
            "max_rss_seen_mb": max_rss_seen_mb,
            "vm_peak_mb": mb("vm_peak_kb"), "peak_rss_mb": mb("vm_hwm_kb")}


#: The options run() hands the default runner for the duration of one run
#: (memory cap, heartbeat). A context variable keeps the runner's contract
#: ``runner(module, timeout)`` unchanged.
_RUN_OPTIONS: contextvars.ContextVar = contextvars.ContextVar(
    "platform_certification_run_options", default={})


def _run_subprocess(module: str, timeout: float):
    """The default runner: one audit, with the current run's options."""
    return _run_capped(module, timeout, **_RUN_OPTIONS.get())


def _run_capped(module: str, timeout: float, *, memory_mb=None,
                address_space_mb=None, heartbeat=None,
                heartbeat_seconds: float = 60.0, settings: dict | None = None,
                readers: dict | None = None,
                poll_seconds: float = WATCHDOG_POLL_SECONDS):
    """Run one audit in a fresh interpreter, within the memory that is free.

    * Its resident budget is derived NOW (:func:`derive_budget`), or is
      ``memory_mb`` when given. Below the floor the audit is not started.
    * The parent reads the child's ``VmRSS`` every ``poll_seconds`` and
      SIGKILLs it past the budget (the watchdog).
    * The child caps its own address space at ``address_space_mb`` — a
      backstop for a spike faster than the poll.

    Returns ``(returncode | None, stdout, stderr, error, memory)``. Output goes
    to temporary files, so a large report can never block the child while the
    parent waits; ``heartbeat`` (if given) is called every
    ``heartbeat_seconds`` while the audit runs.
    """
    settings = settings or load_settings()
    readers = readers or {}
    if memory_mb is not None:
        budget = {"budget_mb": int(memory_mb), "limit_mb": None,
                  "limit_source": "fixed", "usage_mb": None, "usage_source": None,
                  "safety_margin_mb": None,
                  "floor_mb": float(settings["min_audit_budget_mb"])}
    else:
        budget = derive_budget(settings, limit_reader=readers.get("limit"),
                               usage_reader=readers.get("usage"))
    if address_space_mb is None:
        address_space_mb = int(settings["audit_address_space_mb"])
    if memory_mb is None and (budget["budget_mb"] is None
                              or budget["budget_mb"] < budget["floor_mb"]):
        available = budget["budget_mb"]
        return (None, "", "",
                "insufficient memory headroom ("
                + ("unknown" if available is None else f"{max(available, 0)} MB")
                + f" available; the floor is {int(budget['floor_mb'])} MB): "
                  "not started", _memory_facts("", budget, address_space_mb))
    cmd = [sys.executable, "-m", CHILD_MODULE, "--memory-mb",
           str(int(address_space_mb)), module, "--json"]
    limit_kb = budget["budget_mb"] * 1024
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL)
        except OSError as exc:
            return None, "", "", f"could not start: {exc}", None
        now = time.monotonic()
        deadline = now + timeout
        next_beat = now + heartbeat_seconds
        error = killed_rss = None
        max_rss_kb = 0
        while True:
            wait = min(poll_seconds, max(0.0, deadline - time.monotonic()))
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                pass
            rss_kb = _status_kb(proc.pid, "VmRSS")
            if rss_kb is not None:
                max_rss_kb = max(max_rss_kb, rss_kb)
                if rss_kb > limit_kb:
                    proc.kill()
                    proc.wait()
                    killed_rss = round(rss_kb / 1024, 1)
                    break
            now = time.monotonic()
            if now >= deadline:
                proc.kill()
                proc.wait()
                error = f"timed out after {timeout:g}s"
                break
            if heartbeat is not None and now >= next_beat:
                next_beat = now + heartbeat_seconds
                try:
                    heartbeat()
                except Exception:  # noqa: BLE001 — a heartbeat never stops an audit
                    pass
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", "replace")
        stderr = err.read().decode("utf-8", "replace")
    facts = _memory_facts(stderr, budget, address_space_mb, watchdog_rss_mb=killed_rss,
                          max_rss_seen_mb=round(max_rss_kb / 1024, 1) if max_rss_kb else None)
    if error:
        return None, "", stderr, error, facts
    return proc.returncode, stdout, stderr, None, facts


def _messages(value, limit: int = 5) -> list:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v)[:300] for v in items[:limit]]


def _signal_name(returncode) -> str:
    try:
        return signal.Signals(-returncode).name
    except (ValueError, TypeError):
        return f"signal {-returncode}"


def classify(returncode, stdout: str, stderr: str = "",
             error: str | None = None, memory: dict | None = None) -> dict:
    """Turn one audit's process result into an outcome. Fail closed.

    ``memory`` is what the capped child reported (``_memory_facts``); None
    when the audit ran without a cap.
    """
    from scripts.certification_child import MARKER  # noqa: PLC0415

    def crashed(detail):
        tail = [line for line in (stderr or "").strip().splitlines()
                if not line.startswith(MARKER)][-3:]
        return {"outcome": CRASHED, "exit_code": returncode, "report": None,
                "detail": detail, "stderr_tail": tail}

    memory = memory or {}
    budget = memory.get("budget_mb")
    backstop = memory.get("address_space_mb")
    if error:
        return crashed(error)
    # PR-ADS-169A — memory, checked before the exit code is believed.
    if memory.get("watchdog_killed"):
        return crashed(f"exceeded its memory budget ({budget} MB resident): "
                       f"{memory.get('killed_at_rss_mb')} MB resident when the "
                       "watchdog stopped it")
    if memory.get("memory_error"):
        return crashed(f"exceeded its memory backstop ({backstop} MB of address "
                       "space): it ran out of memory and was stopped")
    if returncode == -signal.SIGKILL:
        return crashed("killed by SIGKILL (signal 9) — out of memory: the "
                       "host's memory limit was reached"
                       + (f" (its own budget is {budget} MB resident)" if budget else ""))
    if isinstance(returncode, int) and returncode < 0:
        return crashed(f"killed by {_signal_name(returncode)} "
                       f"(signal {-returncode})")
    if backstop is not None and returncode in _OUTCOME_BY_EXIT:
        peak, rss = memory.get("vm_peak_mb"), memory.get("peak_rss_mb")
        if peak is None or rss is None:
            return crashed("ran without reporting its memory use, so its "
                           f"memory budget ({budget} MB resident) is not proven held")
        if budget is not None and rss > budget:
            # Above the budget between two watchdog reads: not held.
            return crashed(f"exceeded its memory budget ({budget} MB resident): "
                           f"peak {rss} MB resident")
        if peak >= backstop - MEMORY_GUARD_MB:
            return crashed(f"reached its memory backstop ({backstop} MB of address "
                           f"space): peak {peak} MB is within {MEMORY_GUARD_MB} MB "
                           "of it, so an allocation may have failed inside the "
                           "audit and its report is not trusted")
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


def run(*, timeout: float | None = None, runner=None, audits=AUDITS,
        memory_mb: int | None = None, address_space_mb: int | None = None,
        heartbeat=None, on_audit_start=None, readers: dict | None = None) -> dict:
    """Run the audits ONE AT A TIME and combine their verdicts.

    The default runner gives each audit the resident budget that is free when
    it starts (``derive_budget``; ``memory_mb`` fixes it instead) and an
    address-space backstop (``address_space_mb``, default the configured
    ``audit_address_space_mb``). ``on_audit_start(key)`` and ``heartbeat()``
    let the caller show a live run (PR-ADS-169A).
    """
    settings = load_settings()
    timeout = settings["audit_timeout_seconds"] if timeout is None else timeout
    runner = runner or _run_subprocess
    token = _RUN_OPTIONS.set({
        "memory_mb": memory_mb, "address_space_mb": address_space_mb,
        "heartbeat": heartbeat, "settings": settings, "readers": readers,
        "heartbeat_seconds": settings["heartbeat_seconds"]})
    try:
        results = _run_each(audits, runner, timeout, on_audit_start)
    finally:
        _RUN_OPTIONS.reset(token)
    exit_code, verdict = combine(results)
    return {"audit": "platform_certification",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "verdict": verdict, "exit_code": exit_code,
            "external_writes_performed": False,
            "database_writes_performed": False,
            "audits": results,
            "crm_data_to_fix": crm_data_to_fix(results)}


def _run_each(audits, runner, timeout, on_audit_start) -> list:
    """Each audit in turn — one finishes before the next starts."""
    results = []
    for key, module, proves in audits:
        if on_audit_start is not None:
            try:
                on_audit_start(key)
            except Exception:  # noqa: BLE001 — progress never stops a run
                pass
        started = time.monotonic()
        outcome = runner(module, timeout)
        rc, out, err, error = outcome[:4]
        memory = outcome[4] if len(outcome) > 4 else None
        res = classify(rc, out, err, error, memory)
        res.update(key=key, module=module, proves=proves,
                   seconds=round(time.monotonic() - started, 1))
        if memory:
            res.update(memory_budget_mb=memory.get("budget_mb"),
                       peak_rss_mb=memory.get("peak_rss_mb"),
                       vm_peak_mb=memory.get("vm_peak_mb"),
                       memory=memory)
        results.append(res)
    return results


_MARK = {HOLDS: "PASS", VIOLATION: "FAIL", UNAVAILABLE: "UNAVAILABLE",
         CRASHED: "CRASHED"}


def _render(report: dict) -> None:
    print("=" * 78)
    print(f"  PLATFORM CERTIFICATION: {report['verdict']}"
          f"   (exit {report['exit_code']})")
    print("  read-only: no database writes, no external API calls")
    print("=" * 78)
    for r in report["audits"]:
        mem = (f"  peak {r['peak_rss_mb']} MB"
               if r.get("peak_rss_mb") is not None else "")
        print(f"  {_MARK[r['outcome']]:<12}{r['key']:<28}{r['seconds']:>6}s"
              f"  {r['proves']}{mem}")
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
    parser.add_argument("--timeout", type=float, default=None,
                        help="seconds allowed per audit (default: "
                             "platform_certification.audit_timeout_seconds)")
    parser.add_argument("--memory-mb", type=int, default=None,
                        help="fixed resident budget per audit in MB (default: "
                             "derived from the instance's free memory)")
    args = parser.parse_args(argv)
    report = run(timeout=args.timeout, memory_mb=args.memory_mb)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
