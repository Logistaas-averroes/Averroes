#!/usr/bin/env python3
"""
scripts/backfill_canonical_deal_ledger.py

PR-ADS-153E-A2 §4 — operator CLI for the resumable historical deal bootstrap.

    python -m scripts.backfill_canonical_deal_ledger
    python -m scripts.backfill_canonical_deal_ledger --max-passes 40 --json
    python -m scripts.backfill_canonical_deal_ledger --restart      # deliberate
    echo $?      # 0 = bootstrap PROVEN complete, 1 = failure, 2 = usage error

Why this exists
---------------
`backfill_deals()` is bounded: a pass stops at ``--max-association-lookups`` and
checkpoints at its last fully committed deal. On a portal larger than that cap a
single call can therefore never finish, and there was no supported way to drive
it to completion and *prove* the result. PR-ADS-153E-A2 makes a complete
bootstrap a hard precondition of the revenue cutover gate, so proving it needs a
command, not a person running the same thing repeatedly and deciding by eye.

What "success" means here
-------------------------
    THIS execution proved completion   AND   the durable state agrees

Exit 0 requires BOTH halves:

  * a pass in THIS run that reported ``status == "success"`` and
    ``complete == true``, with no association failures, no write failures and
    no error — the connector proved it reached the end of the result set and
    everything it read actually landed;
  * a re-read of the durable sync state showing ``bootstrap_status ==
    "complete"``, ``last_status == "success"`` and no ``last_error``.

Neither half is redundant. The first is this process's opinion; the second is
what the audit gate will actually read tomorrow, and if they disagree this
command fails rather than reporting a completion the gate will not honour.

The first half is equally load-bearing in the other direction.
``bootstrap_status`` is deliberately monotonic — coverage once proven stays
proven — so checking only the durable state would let a run that died on its
first pull exit 0 purely because someone completed a bootstrap last week. An
operator asks this command "did the backfill work?", not "has one ever worked?".
The reason string always names the pass that stopped and why.

Failure posture
---------------
Stops IMMEDIATELY on any pull, association, persistence or state-recording
failure. It never retries past an error, and it never escalates to ``--restart``
on its own: re-reading an entire portal is an operator's decision, not a
program's reaction to a bad night.

Guarantees
----------
  * Reads HubSpot READ-ONLY. Writes only local PostgreSQL. No Google Ads call of
    any kind, no HubSpot mutation, no Mailchimp.
  * Bounded by ``--max-passes``. It cannot loop indefinitely.
  * NO PII in the output: no contact names, no email addresses, no full GCLIDs,
    no deal names, no company names. Counts, statuses and timestamps only —
    this output goes into operator logs and PR evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

DEFAULT_MAX_PASSES = 200


def _fmt(value) -> str:
    """Render a value without ever turning an unknown into a zero."""
    if value is None:
        return "Unavailable"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _pass_summary(index: int, result: dict) -> dict:
    """The non-sensitive facts about one pass. Deliberately NOT the raw result."""
    return {
        "pass": index,
        "status": result.get("status"),
        "sync_mode": result.get("sync_mode"),
        "deals_seen": result.get("deals_seen"),
        "written": result.get("written"),
        "skipped_stale": result.get("skipped_stale"),
        "association_failures": result.get("association_failures"),
        "write_failures": result.get("write_failures"),
        "pages": result.get("pages"),
        "complete": result.get("complete"),
        "checkpointed": bool(result.get("watermark_is_checkpoint")),
        "watermark": result.get("watermark"),
        "error": result.get("error"),
    }


def _is_cap_only_pause(result: dict) -> bool:
    """True when a pass stopped ONLY because the lookup cap was reached.

    A capped pass is resumable; anything else is a failure. The checkpoint is
    required as well — without one the next pass would start from the same place
    and this loop would spin without progressing.
    """
    if result.get("status") != "partial":
        return False
    if result.get("write_failures"):
        return False
    if result.get("association_failures"):
        return False
    if not result.get("watermark_is_checkpoint"):
        return False
    return "association_lookup_cap_reached" in (result.get("error") or "")


def _pass_proves_completion(result: dict) -> bool:
    """Did THIS pass prove the bootstrap finished?

    Every clause matters. A pass that succeeded but lost associations, failed a
    write, or carried an error did not establish complete coverage, and a pass
    that never reached the end of the result set did not establish it either.
    """
    return bool(
        result.get("status") == "success"
        and result.get("complete") is True
        and not result.get("association_failures")
        and not result.get("write_failures")
        and not result.get("error")
    )


def run(*, max_passes: int, max_association_lookups: int,
        restart: bool) -> dict:
    """Drive the bounded bootstrap to a PROVEN completion, or stop and say why.

    The contract is::

        THIS execution proved completion   AND   the durable state agrees

    It is emphatically NOT "this execution failed but an older bootstrap once
    completed". `bootstrap_status` is deliberately monotonic — coverage once
    proven stays proven — so reading it alone would let a run that failed on its
    first pull report success purely because someone completed a bootstrap last
    week. An operator asks this command "did the backfill work?", not "has one
    ever worked?".
    """
    from db.connection import init_pool
    from db.deal_ledger_repository import BOOTSTRAP_COMPLETE, fetch_sync_state
    from services.hubspot_deal_sync_service import backfill_deals

    init_pool()

    passes: list = []
    # Proof from THIS execution. Never seeded from durable state.
    pass_proved_complete = False
    outcome = {"ok": False, "reason": "not_started", "passes": passes,
               "passes_run": 0, "max_passes": max_passes,
               "restart": restart,
               "max_association_lookups": max_association_lookups,
               "pass_proved_complete": False,
               "bootstrap_status": None, "bootstrap_started_at": None,
               "bootstrap_completed_at": None, "last_status": None,
               "last_error": None, "last_sync_mode": None, "watermark": None,
               "deals_seen_total": 0, "written_total": 0}

    for index in range(1, max_passes + 1):
        # `restart` applies to the FIRST pass only. Every later pass must resume
        # from the checkpoint the previous one wrote, or the loop would re-read
        # the whole portal each time and never converge.
        result = backfill_deals(restart=restart and index == 1,
                                max_association_lookups=max_association_lookups)
        summary = _pass_summary(index, result)
        passes.append(summary)
        outcome["passes_run"] = index
        outcome["deals_seen_total"] += int(result.get("deals_seen") or 0)
        outcome["written_total"] += int(result.get("written") or 0)
        outcome["watermark"] = result.get("watermark") or outcome["watermark"]

        if _pass_proves_completion(result):
            pass_proved_complete = True
            outcome["pass_proved_complete"] = True
            outcome["reason"] = "bootstrap_pass_reported_complete"
            break

        if result.get("status") == "success" and result.get("complete"):
            # Reached the end of the result set, but not cleanly. Coverage is
            # only as good as the deals that actually landed.
            outcome["reason"] = (
                f"pass {index} reached the end of the result set but was not "
                f"clean: {result.get('association_failures') or 0} association "
                f"failure(s), {result.get('write_failures') or 0} write "
                f"failure(s)"
                + (f", error: {result.get('error')}" if result.get("error")
                   else ""))
            break

        if _is_cap_only_pause(result):
            continue

        # Anything else — a pull failure, an association failure, a persistence
        # failure, a state-write failure, or a partial with no checkpoint to
        # resume from — stops here. Retrying past it would either spin forever
        # or bury the reason.
        outcome["reason"] = (f"pass {index} stopped: "
                             f"{result.get('status')} "
                             f"({result.get('error') or 'no reason recorded'})")
        break
    else:
        outcome["reason"] = (
            f"exhausted --max-passes={max_passes} without proving completion")

    # ── The durable proof, re-read from the database ────────────────────────
    state = fetch_sync_state()
    if not state.get("available"):
        outcome["reason"] = ("sync state unreadable after the final pass — "
                             "completion cannot be proven")
        return outcome

    row = state.get("row") or {}
    outcome.update({
        "bootstrap_status": row.get("bootstrap_status"),
        "bootstrap_started_at": row.get("bootstrap_started_at"),
        "bootstrap_completed_at": row.get("bootstrap_completed_at"),
        "last_status": row.get("last_status"),
        "last_error": row.get("last_error"),
        "last_sync_mode": row.get("last_sync_mode"),
    })

    # ── This execution must have proven it ──────────────────────────────────
    # Checked FIRST, and it never overwrites the reason the loop recorded. The
    # operator needs "pass 3 stopped: failed (pull_failed: 503)", not a generic
    # "not complete" that hides which pass died and why.
    if not pass_proved_complete:
        outcome["ok"] = False
        return outcome

    # ── And the durable state must agree ────────────────────────────────────
    if row.get("bootstrap_status") != BOOTSTRAP_COMPLETE:
        outcome["ok"] = False
        outcome["reason"] = (
            "a pass proved completion but the durable sync state is "
            f"{row.get('bootstrap_status') or 'unknown'}")
        return outcome

    # The durable record of that pass must itself be clean. A success the
    # database recorded as failed, or alongside an error, is not a success.
    if row.get("last_status") != "success":
        outcome["ok"] = False
        outcome["reason"] = (
            "a pass proved completion but the durable last_status is "
            f"{row.get('last_status') or 'unknown'}")
        return outcome

    if row.get("last_error"):
        outcome["ok"] = False
        outcome["reason"] = (
            "a pass proved completion but the durable state recorded an "
            f"error: {row.get('last_error')}")
        return outcome

    outcome["ok"] = True
    outcome["reason"] = "bootstrap_complete"
    return outcome


def _render(outcome: dict) -> None:
    print("=" * 74)
    print("PR-ADS-153E-A2 — CANONICAL DEAL LEDGER HISTORICAL BOOTSTRAP")
    print("=" * 74)
    print(f"    {'mode':<36} "
          f"{'RESTART (full re-read)' if outcome.get('restart') else 'resume'}")
    print(f"    {'association lookups per pass':<36} "
          f"{_fmt(outcome.get('max_association_lookups'))}")
    print(f"    {'passes run / allowed':<36} "
          f"{_fmt(outcome.get('passes_run'))} / {_fmt(outcome.get('max_passes'))}")
    print()
    print(f"    {'pass':<6}{'status':<10}{'seen':>10}{'written':>10}"
          f"{'assoc fail':>12}{'write fail':>12}{'complete':>10}")
    for p in outcome.get("passes") or []:
        print(f"    {p['pass']:<6}{str(p.get('status')):<10}"
              f"{_fmt(p.get('deals_seen')):>10}{_fmt(p.get('written')):>10}"
              f"{_fmt(p.get('association_failures')):>12}"
              f"{_fmt(p.get('write_failures')):>12}"
              f"{_fmt(p.get('complete')):>10}")
        if p.get("error"):
            print(f"           error: {p['error']}")

    print()
    print(f"    {'deals seen (all passes)':<36} "
          f"{_fmt(outcome.get('deals_seen_total'))}")
    print(f"    {'rows written (all passes)':<36} "
          f"{_fmt(outcome.get('written_total'))}")
    print()
    print(f"    {'this run proved completion':<36} "
          f"{_fmt(outcome.get('pass_proved_complete'))}")
    print()
    print("    ── durable sync state ──")
    for key in ("bootstrap_status", "bootstrap_started_at",
                "bootstrap_completed_at", "last_status", "last_sync_mode",
                "last_error"):
        print(f"    {key:<36} {_fmt(outcome.get(key))}")

    print()
    print("=" * 74)
    if outcome.get("ok"):
        print("  COMPLETE — this run proved it AND the durable state agrees.")
        print("  Next: run ONE normal incremental sync, then")
        print("  python -m scripts.audit_canonical_revenue_truth --all-windows")
    else:
        print(f"  NOT COMPLETE — {outcome.get('reason')}")
        print("  Re-run to resume from the checkpoint. Do NOT pass --restart")
        print("  as a reaction to this failure; it re-reads the whole portal.")
    print("=" * 74)
    print("Read-only against HubSpot. No writes to any external platform.")
    print("=" * 74)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive the bounded, resumable canonical deal-ledger "
                    "historical bootstrap to a proven completion (read-only "
                    "against HubSpot)")
    parser.add_argument("--max-passes", type=int, default=DEFAULT_MAX_PASSES,
                        help=f"Maximum resume passes (default {DEFAULT_MAX_PASSES}). "
                             "Bounds the run; it never loops indefinitely.")
    parser.add_argument("--max-association-lookups", type=int, default=None,
                        help="Association lookups per pass. Lower it to shorten "
                             "each pass; the checkpoint makes progress durable.")
    parser.add_argument("--restart", action="store_true",
                        help="DELIBERATE full re-read: ignore the checkpoint and "
                             "start from the beginning. Much slower, and never "
                             "needed to recover from an error — a normal resume "
                             "continues from the last committed deal.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (same exit code)")
    args = parser.parse_args()

    if args.max_passes < 1:
        print("--max-passes must be at least 1.", file=sys.stderr)
        return EXIT_USAGE
    if args.max_association_lookups is not None and args.max_association_lookups < 1:
        print("--max-association-lookups must be at least 1.", file=sys.stderr)
        return EXIT_USAGE

    try:
        from services.hubspot_deal_sync_service import (
            DEFAULT_MAX_ASSOCIATION_LOOKUPS,
        )

        lookups = (args.max_association_lookups
                   if args.max_association_lookups is not None
                   else DEFAULT_MAX_ASSOCIATION_LOOKUPS)
        outcome = run(max_passes=args.max_passes,
                      max_association_lookups=lookups,
                      restart=args.restart)
    except Exception as exc:  # noqa: BLE001
        # A bootstrap that cannot run is a FAILED bootstrap, never a pass.
        failure = {"ok": False, "reason": f"bootstrap could not run: {exc}"}
        if args.json:
            print(json.dumps(failure, indent=2))
        else:
            print(f"BOOTSTRAP FAILED — could not run: {exc}")
        return EXIT_FAILED

    exit_code = EXIT_OK if outcome.get("ok") else EXIT_FAILED
    if args.json:
        print(json.dumps(outcome, indent=2, default=str))
        return exit_code

    _render(outcome)
    print(f"exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
