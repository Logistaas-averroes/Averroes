"""
scripts/phase1_readiness.py

Read-only local production readiness audit for the Logistaas Ads Intelligence
System — Phase 1.

Checks:
  1. Required environment variables are set.
  2. Required configuration files exist.
  3. Required documentation files exist (including PHASE1_PRODUCTION_READINESS.md).
  4. Makefile exists and contains all expected targets.
  5. render.yaml exists and contains the three cron job definitions.
  6. Scheduler source files exist.
  7. No forbidden Phase 1 write-back modules are present.
  8. scripts/healthcheck.py logic runs without critical failure.
  9. scripts/validate_phase1.py logic runs without critical failure.

Forbidden:
  - No live API calls.
  - No Google Ads writes.
  - No HubSpot writes.
  - No report generation.
  - No delivery attempts.

Exit codes:
  0 — all critical readiness checks passed
  1 — one or more critical checks failed

Usage:
  python scripts/phase1_readiness.py
"""

import importlib
import os
import re
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

# ── Colour helpers (degrade gracefully when stdout is not a TTY) ──────────────

_NO_COLOUR = not sys.stdout.isatty()

_GREEN  = "" if _NO_COLOUR else "\033[92m"
_RED    = "" if _NO_COLOUR else "\033[91m"
_YELLOW = "" if _NO_COLOUR else "\033[93m"
_RESET  = "" if _NO_COLOUR else "\033[0m"
_BOLD   = "" if _NO_COLOUR else "\033[1m"


def _pass(label: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"  {_GREEN}PASS{_RESET}  {label}{suffix}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"  {_RED}FAIL{_RESET}  {label}{suffix}")


def _warn(label: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"  {_YELLOW}WARN{_RESET}  {label}{suffix}")


def _header(title: str) -> None:
    print(f"\n{_BOLD}{title}{_RESET}")
    print("─" * 60)


# ── 1. Required environment variables ────────────────────────────────────────

_REQUIRED_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "HUBSPOT_API_KEY",
    "WINDSOR_API_KEY",
    "WINDSOR_ACCOUNT_ID",
    "SENDGRID_API_KEY",
    "REPORT_SENDER_EMAIL",
    "REPORT_RECIPIENT_EMAIL",
]


def check_env_vars(failures: list) -> None:
    _header("Required Environment Variables")
    for name in _REQUIRED_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            _pass(name)
        else:
            _fail(name, "not set or empty")
            failures.append(f"env:{name}")


# ── 2. Required configuration files ──────────────────────────────────────────

_REQUIRED_CONFIG_FILES = [
    "config/thresholds.yaml",
    "config/junk_patterns.yaml",
    "render.yaml",
    "Makefile",
    "requirements.txt",
    ".env.example",
]


def check_config_files(failures: list) -> None:
    _header("Required Configuration and Deployment Files")
    for path in _REQUIRED_CONFIG_FILES:
        if os.path.isfile(path):
            _pass(path, "exists")
        else:
            _fail(path, "file not found")
            failures.append(f"file:{path}")


# ── 3. Required documentation files ──────────────────────────────────────────

_REQUIRED_DOCS = [
    "docs/01_PROJECT_MASTER.md",
    "docs/03_ARCHITECTURE.md",
    "docs/04_PHASE_ROADMAP.md",
    "docs/09_REPO_STATE.md",
    "docs/PHASE1_PRODUCTION_READINESS.md",
]

_DOCTRINE_CANDIDATES = ["docs/DOCTRINE.md", "docs/02_DOCTRINE.md"]


def check_docs(failures: list) -> None:
    _header("Required Documentation Files")
    for path in _REQUIRED_DOCS:
        if os.path.isfile(path):
            _pass(path, "exists")
        else:
            _fail(path, "file not found")
            failures.append(f"doc:{path}")

    doctrine_found = any(os.path.isfile(p) for p in _DOCTRINE_CANDIDATES)
    if doctrine_found:
        found = next(p for p in _DOCTRINE_CANDIDATES if os.path.isfile(p))
        _pass(found, "exists")
    else:
        _fail("docs/DOCTRINE.md or docs/02_DOCTRINE.md", "file not found")
        failures.append("doc:docs/DOCTRINE.md")


# ── 4. Makefile targets ───────────────────────────────────────────────────────

_REQUIRED_MAKEFILE_TARGETS = [
    "healthcheck",
    "daily",
    "weekly",
    "monthly",
    "validate",
    "readiness",
    "runs",
]


def check_makefile_targets(failures: list) -> None:
    _header("Makefile — Required Targets")
    if not os.path.isfile("Makefile"):
        _fail("Makefile", "file not found")
        failures.append("file:Makefile")
        return

    with open("Makefile") as fh:
        content = fh.read()

    for target in _REQUIRED_MAKEFILE_TARGETS:
        # A Makefile target begins at column 0 followed by a colon.
        if re.search(rf"^{re.escape(target)}:", content, re.MULTILINE):
            _pass(f"make {target}")
        else:
            _fail(f"make {target}", "target not found in Makefile")
            failures.append(f"makefile-target:{target}")


# ── 5. render.yaml cron job definitions ──────────────────────────────────────

_REQUIRED_RENDER_PATTERNS = [
    ("daily cron",   r"scheduler/daily\.py"),
    ("weekly cron",  r"scheduler/weekly\.py"),
    ("monthly cron", r"scheduler/monthly\.py"),
]


def check_render_yaml(failures: list) -> None:
    _header("render.yaml — Cron Job Definitions")
    if not os.path.isfile("render.yaml"):
        _fail("render.yaml", "file not found")
        failures.append("file:render.yaml")
        return

    with open("render.yaml") as fh:
        content = fh.read()

    for label, pattern in _REQUIRED_RENDER_PATTERNS:
        if re.search(pattern, content):
            _pass(f"render.yaml: {label}")
        else:
            _fail(f"render.yaml: {label}", f"pattern '{pattern}' not found")
            failures.append(f"render:{label}")


# ── 6. Scheduler source files ─────────────────────────────────────────────────

_REQUIRED_SCHEDULER_FILES = [
    "scheduler/daily.py",
    "scheduler/weekly.py",
    "scheduler/monthly.py",
    "scheduler/delivery.py",
    "scheduler/run_history.py",
]

_REQUIRED_CONNECTOR_FILES = [
    "connectors/hubspot_pull.py",
    "connectors/windsor_pull.py",
    "connectors/gclid_match.py",
]

_REQUIRED_ANALYSIS_FILES = [
    "analysis/core.py",
    "analysis/advisor.py",
]

_REQUIRED_SCRIPT_FILES = [
    "scripts/healthcheck.py",
    "scripts/validate_phase1.py",
]


def check_source_files(failures: list) -> None:
    _header("Required Source Files")
    groups = [
        ("Scheduler",  _REQUIRED_SCHEDULER_FILES),
        ("Connectors", _REQUIRED_CONNECTOR_FILES),
        ("Analysis",   _REQUIRED_ANALYSIS_FILES),
        ("Scripts",    _REQUIRED_SCRIPT_FILES),
    ]
    for _group, paths in groups:
        for path in paths:
            if os.path.isfile(path):
                _pass(path, "exists")
            else:
                _fail(path, "file not found")
                failures.append(f"file:{path}")


# ── 7. Forbidden write-back modules (Phase 2+) ───────────────────────────────

_FORBIDDEN_PHASE1_MODULES = [
    ("connectors/oct_uploader.py",    "OCT uploader — Phase 2, must not be active"),
    ("connectors/negative_pusher.py", "Negative pusher — Phase 3, must not be active"),
    ("api/server.py",                 "FastAPI server — Phase 4, must not be active"),
]


def check_no_forbidden_modules(failures: list) -> None:
    _header("Phase 1 Doctrine — No Forbidden Write-Back Modules Active")
    all_clear = True
    for path, description in _FORBIDDEN_PHASE1_MODULES:
        if os.path.isfile(path):
            _fail(path, description)
            failures.append(f"forbidden:{path}")
            all_clear = False

    if all_clear:
        _pass("no forbidden Phase 2+ modules detected")


# ── 8. Delegate to healthcheck ────────────────────────────────────────────────

def check_healthcheck(failures: list) -> None:
    _header("scripts/healthcheck.py — Preflight Healthcheck")
    result = subprocess.run(
        [sys.executable, "scripts/healthcheck.py"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        _pass("scripts/healthcheck.py", "exit 0")
    else:
        _fail("scripts/healthcheck.py", f"exit {result.returncode}")
        failures.append("healthcheck:non-zero-exit")
        # Print the captured output so the operator can see what failed.
        if result.stdout:
            for line in result.stdout.splitlines():
                print(f"    {line}")


# ── 9. Delegate to validate_phase1 ───────────────────────────────────────────

def check_validate_phase1(failures: list) -> None:
    _header("scripts/validate_phase1.py — Phase 1 Validation")
    result = subprocess.run(
        [sys.executable, "scripts/validate_phase1.py"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        _pass("scripts/validate_phase1.py", "exit 0")
    else:
        _fail("scripts/validate_phase1.py", f"exit {result.returncode}")
        failures.append("validate:non-zero-exit")
        if result.stdout:
            for line in result.stdout.splitlines():
                print(f"    {line}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"\n{'='*60}")
    print("  LOGISTAAS ADS INTELLIGENCE — PHASE 1 READINESS AUDIT")
    print(f"{'='*60}")

    critical_failures: list = []

    check_env_vars(critical_failures)
    check_config_files(critical_failures)
    check_docs(critical_failures)
    check_makefile_targets(critical_failures)
    check_render_yaml(critical_failures)
    check_source_files(critical_failures)
    check_no_forbidden_modules(critical_failures)
    check_healthcheck(critical_failures)
    check_validate_phase1(critical_failures)

    print(f"\n{'='*60}")
    if critical_failures:
        print(f"  {_RED}{_BOLD}READINESS AUDIT FAILED{_RESET}")
        print(f"  {len(critical_failures)} critical issue(s) found:")
        for item in critical_failures:
            print(f"    • {item}")
        print(f"{'='*60}\n")
        return 1

    print(f"  {_GREEN}{_BOLD}READINESS AUDIT PASSED{_RESET}  — Phase 1 is production-ready")
    print(f"  System may enter the 4-week validation period.")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
