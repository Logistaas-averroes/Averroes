"""
scripts/validate_phase1.py

Phase 1 end-to-end read-only validation for the Logistaas Ads Intelligence
System.

Checks:
  1. Python syntax (py_compile) for all core .py files.
  2. YAML validity for config files.
  3. Required documentation files exist.
  4. No stale references to removed/renamed paths remain.

Rules:
  - Read-only — no live API calls, no writes.
  - Print PASS / FAIL per check.
  - Exit non-zero if any check fails.

Usage:
  python scripts/validate_phase1.py
"""

import glob
import os
import py_compile
import re
import sys

import yaml

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


def _header(title: str) -> None:
    print(f"\n{_BOLD}{title}{_RESET}")
    print("─" * 60)


# ── 1. Python syntax checks ───────────────────────────────────────────────────

def validate_syntax(failures: list) -> None:
    _header("Python Syntax (py_compile)")
    patterns = [
        "connectors/*.py",
        "analysis/*.py",
        "scheduler/*.py",
        "scripts/*.py",
    ]
    files = []
    for pattern in patterns:
        files.extend(sorted(glob.glob(pattern)))

    for path in files:
        try:
            py_compile.compile(path, doraise=True)
            _pass(path)
        except py_compile.PyCompileError as exc:
            _fail(path, str(exc))
            failures.append(f"syntax:{path}")


# ── 2. YAML validity ─────────────────────────────────────────────────────────

def validate_yaml(failures: list) -> None:
    _header("YAML Validity")
    yaml_files = [
        "config/thresholds.yaml",
        "config/junk_patterns.yaml",
    ]
    if os.path.isfile("render.yaml"):
        yaml_files.append("render.yaml")

    for path in yaml_files:
        if not os.path.isfile(path):
            _fail(path, "file not found")
            failures.append(f"yaml-missing:{path}")
            continue
        try:
            with open(path) as fh:
                yaml.safe_load(fh)
            _pass(path)
        except yaml.YAMLError as exc:
            _fail(path, str(exc))
            failures.append(f"yaml-invalid:{path}")


# ── 3. Required documentation ─────────────────────────────────────────────────

def validate_docs(failures: list) -> None:
    _header("Required Documentation Files")
    required = [
        "docs/01_PROJECT_MASTER.md",
        "docs/04_PHASE_ROADMAP.md",
        "docs/09_REPO_STATE.md",
    ]
    # Accept either docs/02_DOCTRINE.md or docs/DOCTRINE.md
    doctrine_candidates = ["docs/02_DOCTRINE.md", "docs/DOCTRINE.md"]
    doctrine_found = any(os.path.isfile(p) for p in doctrine_candidates)

    for path in required:
        if os.path.isfile(path):
            _pass(path, "exists")
        else:
            _fail(path, "file not found")
            failures.append(f"doc-missing:{path}")

    if doctrine_found:
        found = next(p for p in doctrine_candidates if os.path.isfile(p))
        _pass(found, "exists")
    else:
        _fail("docs/DOCTRINE.md or docs/02_DOCTRINE.md", "file not found")
        failures.append("doc-missing:docs/DOCTRINE.md")


# ── 4. Stale reference scan ───────────────────────────────────────────────────

# Patterns that must NOT appear in any source file.
_STALE_REFERENCES = [
    (r"config/patterns\.yaml",          "config/patterns.yaml (removed; use config/junk_patterns.yaml)"),
    (r"config/logistaas_config\.yaml",  "config/logistaas_config.yaml (not yet created; use config/thresholds.yaml)"),
    (r"doctrine\.advisor",              "doctrine.advisor (module removed; use analysis.advisor)"),
    (r"data/ads_campaigns_7d\.json",    "data/ads_campaigns_7d.json (stale filename; current file is data/ads_campaigns.json)"),
]

# Source file patterns to scan — skip this script itself to avoid self-match on pattern strings.
_SCAN_PATTERNS = [
    "connectors/*.py",
    "analysis/*.py",
    "scheduler/*.py",
    "scripts/*.py",
    "config/*.yaml",
]


def validate_no_stale_refs(failures: list) -> None:
    _header("Stale Reference Scan")

    files = []
    for pattern in _SCAN_PATTERNS:
        files.extend(sorted(glob.glob(pattern)))

    this_file = os.path.abspath(__file__)
    files = [f for f in files if os.path.abspath(f) != this_file]

    any_stale = False
    for path in files:
        try:
            with open(path) as fh:
                content = fh.read()
        except OSError:
            continue
        for regex, description in _STALE_REFERENCES:
            if re.search(regex, content):
                _fail(path, f"contains stale ref: {description}")
                failures.append(f"stale-ref:{path}:{regex}")
                any_stale = True

    if not any_stale:
        _pass("all scanned files", "no stale references found")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"\n{'='*60}")
    print("  LOGISTAAS ADS INTELLIGENCE — PHASE 1 VALIDATION")
    print(f"{'='*60}")

    failures: list = []

    validate_syntax(failures)
    validate_yaml(failures)
    validate_docs(failures)
    validate_no_stale_refs(failures)

    print(f"\n{'='*60}")
    if failures:
        print(f"  {_RED}{_BOLD}VALIDATION FAILED{_RESET}")
        print(f"  {len(failures)} issue(s) found:")
        for item in failures:
            print(f"    • {item}")
        print(f"{'='*60}\n")
        return 1

    print(f"  {_GREEN}{_BOLD}VALIDATION PASSED{_RESET}  — Phase 1 is operationally ready")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
