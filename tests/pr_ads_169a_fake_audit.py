"""PR-ADS-169A — a stand-in audit for the certification memory tests.

Run as ``python -m tests.pr_ads_169a_fake_audit --json``. What it does is
chosen by ``PR_ADS_169A_FAKE_MODE`` (the certification passes no other
arguments):

* ``hold``     — print a holding report, exit 0, using almost no memory;
* ``allocate`` — allocate 8 MB blocks up to 256 MB, then print a holding
  report: under a small cap it runs out of memory; uncapped it "holds";
* ``swallow``  — the same, but an audit-style ``except Exception`` swallows
  the ``MemoryError`` and the audit still prints a holding report and exits 0
  — the case a cap alone cannot see;
* ``sigkill``  — kill itself with SIGKILL, as the host's OOM killer does;
* ``touch``    — like ``allocate``, but writes every page (so the memory is
  RESIDENT, as a real read's is) at a pace the watchdog can see, then holds
  it for a second;
* ``sleep``    — hold for ``PR_ADS_169A_FAKE_SLEEP`` seconds (default 1.5),
  then print a holding report.

``PR_ADS_169A_FAKE_LOG``, when set, gets one ``start``/``end`` line per run, so
a test can prove two audits never overlapped. Bounded: never more than 256 MB.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time

BLOCK = 8 * 1024 * 1024
LIMIT = 256 * 1024 * 1024


def _log(word: str) -> None:
    path = os.environ.get("PR_ADS_169A_FAKE_LOG")
    if path:
        with open(path, "a", encoding="ascii") as fh:
            fh.write(f"{word} {os.getpid()} {time.monotonic():.6f}\n")


def _allocate() -> list:
    held = []
    while len(held) * BLOCK < LIMIT:
        held.append(bytearray(BLOCK))
    return held


def _touch() -> list:
    held = []
    while len(held) * BLOCK < LIMIT:
        block = bytearray(BLOCK)
        block[::4096] = b"\x01" * (BLOCK // 4096)   # make every page resident
        held.append(block)
        time.sleep(0.05)
    time.sleep(1.0)
    return held


def main() -> int:
    mode = os.environ.get("PR_ADS_169A_FAKE_MODE", "hold")
    _log("start")
    if mode == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "allocate":
        held = _allocate()
        del held
    if mode == "swallow":
        try:
            held = _allocate()
            del held
        except Exception:  # noqa: BLE001 — what a read wrapped "never raises" does
            pass
    if mode == "touch":
        held = _touch()
        del held
    if mode == "sleep":
        time.sleep(float(os.environ.get("PR_ADS_169A_FAKE_SLEEP", "1.5")))
    if mode == "hold":
        time.sleep(0.2)
    print(json.dumps({"exit_code": 0, "violations": [], "unavailable": [],
                      "mode": mode}))
    sys.stdout.flush()
    _log("end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
