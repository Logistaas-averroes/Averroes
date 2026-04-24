"""
scripts/verify_live_deployment.py

Live deployment verification for the Logistaas Ads Intelligence System.

Verifies the deployed FastAPI web service from an external client perspective.
All checks are read-only.  No business data is written.

Required environment variables:
    SERVICE_URL       — base URL of the deployed service, e.g.
                        https://your-service.onrender.com
    ADMIN_API_TOKEN   — optional; needed only when --trigger-daily is used

CLI flags:
    --trigger-daily   — trigger POST /run/daily with a valid token (only when
                        ADMIN_API_TOKEN is set)
    --timeout N       — per-request timeout in seconds (default: 30)
    --debug           — print full exception tracebacks on failure

Exit codes:
    0   — all required checks passed
    1   — one or more required checks failed

Security:
    - ADMIN_API_TOKEN is never printed.
    - Authorization headers are redacted in any log output.
    - Full tracebacks are only shown in --debug mode.

Usage:
    SERVICE_URL=https://your-service.onrender.com \\
        python scripts/verify_live_deployment.py

    SERVICE_URL=https://your-service.onrender.com \\
    ADMIN_API_TOKEN=xxx \\
        python scripts/verify_live_deployment.py --trigger-daily
"""

import argparse
import os
import sys

try:
    import requests
except ImportError:  # pragma: no cover
    print("ERROR: 'requests' package is required. Install with: pip install requests")
    sys.exit(1)

from dotenv import load_dotenv

load_dotenv()

# ── Result tracking ────────────────────────────────────────────────────────────

_results: list[tuple[str, str, str]] = []  # (status, label, detail)


def _record(status: str, label: str, detail: str = "") -> None:
    _results.append((status, label, detail))
    suffix = f"  — {detail}" if detail else ""
    print(f"[{status}] {label}{suffix}")


def _pass(label: str, detail: str = "") -> None:
    _record("PASS", label, detail)


def _fail(label: str, detail: str = "") -> None:
    _record("FAIL", label, detail)


def _skip(label: str, detail: str = "") -> None:
    _record("SKIP", label, detail)


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(url: str, timeout: int, debug: bool) -> requests.Response | None:
    try:
        return requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        if debug:
            import traceback
            traceback.print_exc()
        else:
            print(f"  request error: {type(exc).__name__}: {exc}")
        return None


def _post(
    url: str,
    timeout: int,
    debug: bool,
    token: str | None = None,
) -> requests.Response | None:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return requests.post(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        if debug:
            import traceback
            traceback.print_exc()
        else:
            print(f"  request error: {type(exc).__name__}: {exc}")
        return None


# ── Individual checks ──────────────────────────────────────────────────────────

def check_health(base: str, timeout: int, debug: bool) -> None:
    label = "/health"
    resp = _get(f"{base}/health", timeout, debug)
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code != 200:
        _fail(label, f"HTTP {resp.status_code}")
        return
    try:
        body = resp.json()
    except Exception:
        _fail(label, "non-JSON response")
        return
    if body.get("status") != "ok":
        _fail(label, f"status not 'ok' — got {body.get('status')!r}")
        return
    _pass(label)


def check_readiness(base: str, timeout: int, debug: bool) -> None:
    label = "/readiness"
    resp = _get(f"{base}/readiness", timeout, debug)
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code != 200:
        _fail(label, f"HTTP {resp.status_code}")
        return
    try:
        body = resp.json()
    except Exception:
        _fail(label, "non-JSON response")
        return
    if not isinstance(body, dict):
        _fail(label, "response is not a JSON object")
        return
    _pass(label)


def check_scheduler_status(base: str, timeout: int, debug: bool) -> None:
    label = "/scheduler/status"
    resp = _get(f"{base}/scheduler/status", timeout, debug)
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code != 200:
        _fail(label, f"HTTP {resp.status_code}")
        return
    try:
        body = resp.json()
    except Exception:
        _fail(label, "non-JSON response")
        return
    if not isinstance(body, dict):
        _fail(label, "response is not a JSON object")
        return
    jobs = body.get("jobs")
    if not jobs:
        _fail(label, "no jobs listed in scheduler status")
        return
    # Verify daily/weekly/monthly are present
    job_names = {j.get("job") for j in jobs if isinstance(j, dict)}
    missing = {"daily", "weekly", "monthly"} - job_names
    if missing:
        _fail(label, f"missing jobs: {', '.join(sorted(missing))}")
        return
    _pass(label)


def check_runs_latest(base: str, timeout: int, debug: bool) -> None:
    label = "/runs/latest"
    resp = _get(f"{base}/runs/latest", timeout, debug)
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code not in (200, 404):
        _fail(label, f"HTTP {resp.status_code}")
        return
    try:
        resp.json()
    except Exception:
        _fail(label, "non-JSON response")
        return
    _pass(label)


def check_reports_latest(base: str, timeout: int, debug: bool) -> None:
    label = "/reports/latest"
    resp = _get(f"{base}/reports/latest", timeout, debug)
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code not in (200, 404):
        _fail(label, f"HTTP {resp.status_code} (expected 200 or 404)")
        return
    try:
        resp.json()
    except Exception:
        _fail(label, "non-JSON response")
        return
    _pass(label)


def check_missing_token(base: str, timeout: int, debug: bool) -> None:
    label = "/run/daily rejects missing token"
    resp = _post(f"{base}/run/daily", timeout, debug, token=None)
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code in (401, 503):
        _pass(label, f"HTTP {resp.status_code}")
    else:
        _fail(label, f"expected 401 or 503, got HTTP {resp.status_code}")


def check_wrong_token(base: str, timeout: int, debug: bool) -> None:
    label = "/run/daily rejects wrong token"
    resp = _post(f"{base}/run/daily", timeout, debug, token="definitely_wrong_token")
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code in (401, 503):
        _pass(label, f"HTTP {resp.status_code}")
    else:
        _fail(label, f"expected 401 or 503, got HTTP {resp.status_code}")


def check_valid_token_trigger(
    base: str,
    timeout: int,
    debug: bool,
    token: str,
) -> None:
    label = "valid-token daily trigger"
    resp = _post(f"{base}/run/daily", timeout, debug, token=token)
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code in (200, 409):
        _pass(label, f"HTTP {resp.status_code}")
    else:
        _fail(label, f"expected 200 or 409, got HTTP {resp.status_code}")


# ── Main ───────────────────────────────────────────────────────────────────────

def _normalize_url(raw: str) -> str:
    """Strip trailing slashes so endpoint paths can always start with /."""
    return raw.rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a live Logistaas Ads Intelligence deployment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--trigger-daily",
        action="store_true",
        help="Trigger POST /run/daily with ADMIN_API_TOKEN (requires token to be set).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="N",
        help="Per-request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print full exception tracebacks on failure.",
    )
    args = parser.parse_args()

    service_url_raw = os.environ.get("SERVICE_URL", "").strip()
    if not service_url_raw:
        print("ERROR: SERVICE_URL environment variable is not set.")
        print("  Example: SERVICE_URL=https://your-service.onrender.com python scripts/verify_live_deployment.py")
        return 1

    base = _normalize_url(service_url_raw)
    admin_token = os.environ.get("ADMIN_API_TOKEN", "").strip() or None

    print()
    print("Logistaas Ads Intelligence — Live Deployment Verification")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Service URL : {base}")
    print(f"  Token set   : {'yes (redacted)' if admin_token else 'no'}")
    print(f"  Timeout     : {args.timeout}s")
    print()

    # Required checks
    check_health(base, args.timeout, args.debug)
    check_readiness(base, args.timeout, args.debug)
    check_scheduler_status(base, args.timeout, args.debug)
    check_runs_latest(base, args.timeout, args.debug)
    check_reports_latest(base, args.timeout, args.debug)
    check_missing_token(base, args.timeout, args.debug)
    check_wrong_token(base, args.timeout, args.debug)

    # Optional daily trigger
    if args.trigger_daily:
        if admin_token:
            check_valid_token_trigger(base, args.timeout, args.debug, admin_token)
        else:
            _skip(
                "valid-token daily trigger",
                "--trigger-daily requested but ADMIN_API_TOKEN is not set",
            )
    else:
        _skip("valid-token daily trigger", "not requested")

    # Summary
    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    failed = [label for status, label, _ in _results if status == "FAIL"]
    if failed:
        print("LIVE DEPLOYMENT VERIFICATION: FAIL")
        print()
        print("Failed checks:")
        for label in failed:
            print(f"  ✗  {label}")
        print()
        return 1

    print("LIVE DEPLOYMENT VERIFICATION: PASS")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
