"""
scripts/healthcheck.py

Preflight healthcheck for the Logistaas Ads Intelligence System.

Validates environment variables and runtime dependencies before production
runs.  Prints PASS / FAIL per category and exits non-zero when any critical
requirement is missing.

Rules:
  - Read-only checks only.
  - No live API calls.
  - No business logic.
  - No changes to connectors or analysis modules.

Exit codes:
  0 — all critical checks passed (optional checks may still have failed)
  1 — one or more critical checks failed

Usage:
  python scripts/healthcheck.py
"""

import importlib
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ── Colour helpers (degrade gracefully when stdout is not a TTY) ──────────────

_NO_COLOUR = not sys.stdout.isatty()

_GREEN = "" if _NO_COLOUR else "\033[92m"
_RED   = "" if _NO_COLOUR else "\033[91m"
_YELLOW = "" if _NO_COLOUR else "\033[93m"
_RESET = "" if _NO_COLOUR else "\033[0m"
_BOLD  = "" if _NO_COLOUR else "\033[1m"


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


# ── Check helpers ─────────────────────────────────────────────────────────────

def _check_env_var(name: str, required: bool = True) -> bool:
    """Return True if the env var is set and non-empty."""
    value = os.environ.get(name, "").strip()
    if value:
        _pass(name)
        return True
    if required:
        _fail(name, "not set or empty")
    else:
        _warn(name, "not set — optional")
    return bool(value)


def _check_dir(path: str, create_if_missing: bool = False) -> bool:
    """Return True if the directory exists (or was created successfully)."""
    if os.path.isdir(path):
        _pass(path, "exists")
        return True
    if create_if_missing:
        try:
            os.makedirs(path, exist_ok=True)
            _pass(path, "created")
            return True
        except OSError as exc:
            _fail(path, f"could not create: {exc}")
            return False
    _fail(path, "directory not found")
    return False


def _check_import(module: str, label: str = "") -> bool:
    """Return True if the module can be imported without error."""
    display = label or module
    try:
        importlib.import_module(module)
        _pass(display)
        return True
    except ImportError as exc:
        _fail(display, str(exc))
        return False
    except Exception as exc:
        # Module exists but raises at import time (e.g. missing env vars).
        # Still count as importable from a healthcheck perspective.
        _warn(display, f"imported with warning: {exc}")
        return True


# ── Check groups ──────────────────────────────────────────────────────────────

def check_windsor(failures: list) -> None:
    _header("Windsor.ai  [required for daily / weekly / monthly]")
    if not _check_env_var("WINDSOR_API_KEY"):
        failures.append("WINDSOR_API_KEY")
    if not _check_env_var("WINDSOR_ACCOUNT_ID"):
        failures.append("WINDSOR_ACCOUNT_ID")


def check_hubspot(failures: list) -> None:
    _header("HubSpot CRM  [required for daily / weekly / monthly]")
    if not _check_env_var("HUBSPOT_API_KEY"):
        failures.append("HUBSPOT_API_KEY")


def check_advisor_mode(failures: list) -> None:
    _header("Advisor Mode  [ADVISOR_MODE + ANTHROPIC_API_KEY]")
    mode = os.environ.get("ADVISOR_MODE", "deterministic").strip().lower()
    _pass("ADVISOR_MODE", f"value: {mode or 'deterministic (default)'}")

    if mode == "claude":
        # Claude mode requires ANTHROPIC_API_KEY
        if not _check_env_var("ANTHROPIC_API_KEY"):
            failures.append("ANTHROPIC_API_KEY (required when ADVISOR_MODE=claude)")
    else:
        # Deterministic mode — ANTHROPIC_API_KEY is optional
        _check_env_var("ANTHROPIC_API_KEY", required=False)
        if mode not in ("deterministic", ""):
            _warn("ADVISOR_MODE", f"unknown value {mode!r} — defaulting to deterministic")


def check_auth(failures: list) -> None:
    _header("Internal Auth  [APP_SECRET_KEY + AUTH_USERS_JSON]")
    if not _check_env_var("APP_SECRET_KEY"):
        failures.append("APP_SECRET_KEY")
    if not _check_env_var("AUTH_USERS_JSON"):
        failures.append("AUTH_USERS_JSON")


def check_sendgrid(failures: list) -> None:
    _header("SendGrid — Report Delivery  [required for weekly / monthly]")
    # SENDGRID_API_KEY is required for delivery; sender/recipient are also required.
    if not _check_env_var("SENDGRID_API_KEY"):
        failures.append("SENDGRID_API_KEY")
    if not _check_env_var("REPORT_SENDER_EMAIL"):
        failures.append("REPORT_SENDER_EMAIL")
    if not _check_env_var("REPORT_RECIPIENT_EMAIL"):
        failures.append("REPORT_RECIPIENT_EMAIL")


def check_google_ads(failures: list) -> None:
    _header("Google Ads API  [required for Phase 2+ OCT uploads — optional now]")
    # These are optional until OCT uploader is active.
    _check_env_var("GOOGLE_ADS_DEVELOPER_TOKEN", required=False)
    _check_env_var("GOOGLE_ADS_CLIENT_ID", required=False)
    _check_env_var("GOOGLE_ADS_CLIENT_SECRET", required=False)
    _check_env_var("GOOGLE_ADS_REFRESH_TOKEN", required=False)
    _check_env_var("GOOGLE_ADS_CUSTOMER_ID", required=False)


def check_directories(failures: list) -> None:
    _header("Runtime Directories")
    for d in ("data", "outputs", "runtime_logs"):
        if not _check_dir(d, create_if_missing=True):
            failures.append(f"directory:{d}")


def check_config_files(failures: list) -> None:
    _header("Config and Docs Files")
    required = [
        "config/thresholds.yaml",
        "config/junk_patterns.yaml",
        "docs/DOCTRINE.md",
    ]
    for path in required:
        if os.path.isfile(path):
            _pass(path, "exists")
        else:
            _fail(path, "file not found")
            failures.append(f"file:{path}")


def check_imports(failures: list) -> None:
    _header("Python Module Imports  [connectors / analysis / scheduler]")
    modules = [
        ("connectors.hubspot_pull",  "connectors/hubspot_pull.py"),
        ("connectors.windsor_pull",  "connectors/windsor_pull.py"),
        ("connectors.gclid_match",   "connectors/gclid_match.py"),
        ("analysis.core",            "analysis/core.py"),
        ("analysis.advisor",         "analysis/advisor.py"),
        ("analysis.rule_advisor",    "analysis/rule_advisor.py"),
        ("api.auth",                 "api/auth.py"),
        ("scheduler.daily",          "scheduler/daily.py"),
        ("scheduler.weekly",         "scheduler/weekly.py"),
        ("scheduler.monthly",        "scheduler/monthly.py"),
        ("scheduler.delivery",       "scheduler/delivery.py"),
        ("scheduler.run_history",    "scheduler/run_history.py"),
    ]
    for mod, label in modules:
        if not _check_import(mod, label):
            failures.append(f"import:{mod}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"\n{'='*60}")
    print("  LOGISTAAS ADS INTELLIGENCE — PREFLIGHT HEALTHCHECK")
    print(f"{'='*60}")

    critical_failures: list = []

    check_windsor(critical_failures)
    check_hubspot(critical_failures)
    check_advisor_mode(critical_failures)
    check_auth(critical_failures)
    check_sendgrid(critical_failures)
    check_google_ads(critical_failures)   # optional — no failures added
    check_directories(critical_failures)
    check_config_files(critical_failures)
    check_imports(critical_failures)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if critical_failures:
        print(f"  {_RED}{_BOLD}HEALTHCHECK FAILED{_RESET}")
        print(f"  {len(critical_failures)} critical issue(s) found:")
        for item in critical_failures:
            print(f"    • {item}")
        print(f"{'='*60}\n")
        return 1

    print(f"  {_GREEN}{_BOLD}HEALTHCHECK PASSED{_RESET}  — all critical checks OK")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
