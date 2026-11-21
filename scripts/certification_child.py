"""PR-ADS-169A — run ONE certification audit under an address-space backstop.

    python -m scripts.certification_child --memory-mb 320 \
        scripts.audit_customer_closed_won_truth --json

The platform certification runs inside the 512Mi web instance (PR-ADS-161J).
An audit that outgrew the memory left beside the server took the whole
instance down with it (OOM 2026-11-19, 2026-11-20) and no verdict row was
written. Each audit now runs in a fresh interpreter started by this module,
which caps its OWN address space (``RLIMIT_AS``) before it imports the audit.

That cap is a BACKSTOP, not the budget: ``RLIMIT_AS`` also counts reserved,
never-touched mappings. The real budget is resident memory, derived by the
parent when the audit starts and enforced by the parent's watchdog
(``scripts.audit_platform_certification._run_capped``).

Why the cap is set here and not in ``preexec_fn``: ``preexec_fn`` makes
``subprocess`` ``fork()`` the 300 MB multi-threaded server and run Python in
the child before ``exec`` — documented as unsafe with threads (a lock held by
another thread at fork time deadlocks the child) and it disables the
``vfork`` fast path. Started without it, the server's memory is never copied,
and the limit is in place before the audit module is imported.

What this module tells the parent, on stderr, in one marker line written as
the process ends (``MARKER``):

* ``memory_error=1`` — a ``MemoryError`` reached the top level. The process
  then exits :data:`EXIT_MEMORY` without allocating anything further.
* ``vm_peak_kb`` / ``vm_hwm_kb`` — this process's peak address space and peak
  resident memory (``/proc/self/status``), so the parent can record each
  audit's real peak and can tell a run that came within its guard band of the
  cap (where an allocation may have failed inside an audit's own exception
  handler) from one that did not.

The audit's own stdout and exit code pass through unchanged. No database
access, no writes; the audit it runs is read-only.
"""
from __future__ import annotations

import os
import resource
import runpy
import sys

MARKER = "CERTIFICATION_CHILD"
#: Exit code for a MemoryError that reached the top level. Not an audit exit
#: code (0/1/2); the parent reads the marker, never this number alone.
EXIT_MEMORY = 3
EXIT_USAGE = 64


def _vm_status() -> dict:
    out = {}
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("VmPeak", "VmHWM"):
                    out[key] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return out


def _write_marker(**fields) -> None:
    status = _vm_status()
    fields.setdefault("vm_peak_kb", status.get("VmPeak", ""))
    fields.setdefault("vm_hwm_kb", status.get("VmHWM", ""))
    line = MARKER + " " + " ".join(f"{k}={v}" for k, v in fields.items())
    os.write(2, ("\n" + line + "\n").encode("ascii", "replace"))


def cap_address_space(memory_mb: int) -> None:
    """Set the soft and hard ``RLIMIT_AS`` of THIS process to ``memory_mb``."""
    limit = int(memory_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3 or argv[0] != "--memory-mb":
        print("usage: python -m scripts.certification_child --memory-mb N "
              "<module> [args...]", file=sys.stderr)
        return EXIT_USAGE
    try:
        memory_mb = int(argv[1])
    except ValueError:
        print(f"--memory-mb must be an integer, got {argv[1]!r}", file=sys.stderr)
        return EXIT_USAGE
    module, args = argv[2], argv[3:]
    # Precomputed now: when memory runs out there may be none left to build it.
    out_of_memory = (f"\n{MARKER} memory_error=1 memory_mb={memory_mb}\n"
                     ).encode("ascii")
    cap_address_space(memory_mb)
    code = 0
    try:
        sys.argv = [module, *args]
        try:
            runpy.run_module(module, run_name="__main__", alter_sys=True)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (
                0 if exc.code is None else 1)
            if not isinstance(exc.code, (int, type(None))):
                print(exc.code, file=sys.stderr)
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
    except MemoryError:
        os.write(2, out_of_memory)
        os._exit(EXIT_MEMORY)
    except BaseException:  # noqa: BLE001 — what Python itself does: traceback, exit 1
        import traceback  # noqa: PLC0415
        traceback.print_exc()
        code = 1
    _write_marker(memory_mb=memory_mb, exit_code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
