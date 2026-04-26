"""
scripts/verify_live_deployment.py

Live deployment verification for the Logistaas Ads Intelligence System.

Verifies the deployed FastAPI web service from an external client perspective.
All checks are read-only.  No business data is written.

Required environment variables:
    SERVICE_URL       — base URL of the deployed service, e.g.
                        https://your-service.onrender.com

Optional environment variables:
    ADMIN_API_TOKEN   — needed only when --trigger-daily is used
    TEST_USERNAME     — optional login test username
    TEST_PASSWORD     — optional login test password (never printed in logs)

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
    - TEST_PASSWORD is never printed.
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
    label = "GET /health (public)"
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


def check_protected_endpoints_require_auth(base: str, timeout: int, debug: bool) -> None:
    """Verify that protected endpoints return 401 when unauthenticated."""
    protected = [
        "/runs/latest",
        "/reports/latest",
        "/reports/latest/raw",
        "/scheduler/status",
        "/readiness",
    ]
    for endpoint in protected:
        label = f"{endpoint} returns 401 when unauthenticated"
        resp = _get(f"{base}{endpoint}", timeout, debug)
        if resp is None:
            _fail(label, "no response")
            continue
        if resp.status_code == 401:
            _pass(label, "HTTP 401")
        else:
            _fail(label, f"expected 401, got HTTP {resp.status_code}")


def check_auth_me_unauthenticated(base: str, timeout: int, debug: bool) -> None:
    label = "GET /auth/me returns 401 when unauthenticated"
    resp = _get(f"{base}/auth/me", timeout, debug)
    if resp is None:
        _fail(label, "no response")
        return
    if resp.status_code == 401:
        _pass(label, "HTTP 401")
    else:
        _fail(label, f"expected 401, got HTTP {resp.status_code}")


def check_login_test(
    base: str,
    timeout: int,
    debug: bool,
    username: str,
    password: str,
) -> None:
    """
    Optional login test.  PASSWORD IS NEVER PRINTED.
    Tests that /auth/login returns a session cookie and /auth/me works.
    """
    label = "POST /auth/login (login test)"
    try:
        session = requests.Session()
        resp = session.post(
            f"{base}/auth/login",
            json={"username": username, "password": password},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        if debug:
            import traceback
            traceback.print_exc()
        _fail(label, f"{type(exc).__name__}: {exc}")
        return

    if resp.status_code == 200:
        _pass(label, f"HTTP 200, role={resp.json().get('role', '?')}")
        # Also test /auth/me with the session cookie
        me_resp = session.get(f"{base}/auth/me", timeout=timeout)
        if me_resp.status_code == 200:
            _pass("GET /auth/me after login", f"HTTP 200, user={me_resp.json().get('username', '?')}")
        else:
            _fail("GET /auth/me after login", f"HTTP {me_resp.status_code}")
        # Logout
        session.post(f"{base}/auth/logout", timeout=timeout)
    elif resp.status_code == 401:
        _fail(label, "HTTP 401 — invalid credentials (check TEST_USERNAME/TEST_PASSWORD)")
    else:
        _fail(label, f"HTTP {resp.status_code}")


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
    test_username = os.environ.get("TEST_USERNAME", "").strip() or None
    # Never print password
    test_password = os.environ.get("TEST_PASSWORD", "") or None

    print()
    print("Logistaas Ads Intelligence — Live Deployment Verification")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Service URL   : {base}")
    print(f"  Token set     : {'yes (redacted)' if admin_token else 'no'}")
    print(f"  Login test    : {'yes (password redacted)' if (test_username and test_password) else 'no'}")
    print(f"  Timeout       : {args.timeout}s")
    print()

    # Required checks
    check_health(base, args.timeout, args.debug)
    check_auth_me_unauthenticated(base, args.timeout, args.debug)
    check_protected_endpoints_require_auth(base, args.timeout, args.debug)

    # Run endpoint rejection checks (unauthenticated)
    label = "/run/daily rejects unauthenticated requests"
    resp = _post(f"{base}/run/daily", args.timeout, args.debug, token=None)
    if resp is None:
        _fail(label, "no response")
    elif resp.status_code == 401:
        _pass(label, "HTTP 401")
    else:
        _fail(label, f"expected 401, got HTTP {resp.status_code}")

    # Optional: login test
    if test_username and test_password:
        check_login_test(base, args.timeout, args.debug, test_username, test_password)
    else:
        _skip("POST /auth/login (login test)", "TEST_USERNAME or TEST_PASSWORD not set")

    # Optional daily trigger (using bearer token or cookie auth)
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
