"""
api/auth.py

Internal authentication and role permissions for the Logistaas Ads Intelligence
System.

Uses only Render environment variables — no external auth provider, no database.

Environment variables:
  APP_SECRET_KEY   — long random secret used to sign session cookies
  AUTH_USERS_JSON  — JSON array of user objects with username, password_hash, role

User roles:
  admin   — full access including manual run triggers
  viewer  — read-only dashboard, reports, run history, scheduler status
  mdr     — limited read-only (dashboard, lead quality/reports, report metadata)

Session cookies:
  - HTTP-only, signed with HMAC-SHA256 using APP_SECRET_KEY
  - No external session store required
  - Expire after SESSION_MAX_AGE_SECONDS

Password hashing:
  - PBKDF2-HMAC-SHA256 with random 16-byte salt
  - 260,000 iterations (NIST-recommended as of 2023)
  - Constant-time comparison via hmac.compare_digest
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Response

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SESSION_COOKIE = "ads_session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60  # 8 hours
_PBKDF2_ITERATIONS = 260_000
_PBKDF2_HASH = "sha256"

# Role hierarchy (higher index = more privilege)
_ROLE_ORDER = ["mdr", "viewer", "admin"]

# Permissions per role
_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "view_dashboard",
        "view_reports",
        "view_run_history",
        "view_scheduler_status",
        "trigger_runs",
        "view_readiness",
    },
    "viewer": {
        "view_dashboard",
        "view_reports",
        "view_run_history",
        "view_scheduler_status",
    },
    "mdr": {
        "view_dashboard",
        "view_reports",
    },
}


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """
    Hash a password using PBKDF2-HMAC-SHA256 with a random salt.
    Returns a string in the format: pbkdf2_sha256$<iterations>$<salt_hex>$<dk_hex>
    """
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_HASH,
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, hash_str: str) -> bool:
    """
    Verify a password against a stored hash.
    Uses constant-time comparison to prevent timing attacks.
    Returns False on any format error (does not raise).
    """
    try:
        parts = hash_str.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        _, iters_str, salt, stored_dk_hex = parts
        iters = int(iters_str)
        dk = hashlib.pbkdf2_hmac(
            _PBKDF2_HASH,
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iters,
        )
        return hmac.compare_digest(dk.hex(), stored_dk_hex)
    except Exception:  # noqa: BLE001
        return False


# ── User loading ──────────────────────────────────────────────────────────────

def load_users() -> list[dict[str, Any]]:
    """
    Load users from the AUTH_USERS_JSON environment variable.
    Returns an empty list if the variable is not set or contains invalid JSON.
    Never logs or exposes raw user data.
    """
    raw = os.getenv("AUTH_USERS_JSON", "[]").strip()
    if not raw:
        return []
    try:
        users = json.loads(raw)
        if not isinstance(users, list):
            log.warning("AUTH_USERS_JSON is not a JSON array — no users loaded")
            return []
        return users
    except json.JSONDecodeError:
        log.warning("AUTH_USERS_JSON contains invalid JSON — no users loaded")
        return []


def get_user(username: str) -> dict[str, Any] | None:
    """
    Look up a user by username (case-insensitive).
    Returns None if not found.
    """
    username_lower = username.lower()
    for user in load_users():
        if isinstance(user, dict) and user.get("username", "").lower() == username_lower:
            return user
    return None


# ── Cookie signing ────────────────────────────────────────────────────────────

def _get_secret_bytes() -> bytes:
    secret = os.getenv("APP_SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError("APP_SECRET_KEY is not configured")
    return secret.encode("utf-8")


def _sign_cookie(payload: dict) -> str:
    """
    Create a signed cookie value from a payload dict.
    Format: <base64url-json>.<hmac-sha256-hex>
    """
    data_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    data_b64 = base64.urlsafe_b64encode(data_bytes).decode("utf-8").rstrip("=")
    sig = hmac.new(
        _get_secret_bytes(),
        data_b64.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"{data_b64}.{sig}"


def _verify_cookie(cookie_val: str) -> dict | None:
    """
    Verify the cookie signature and return the payload dict, or None if invalid/expired.
    Never raises — returns None on any error.
    """
    try:
        dot_idx = cookie_val.rfind(".")
        if dot_idx == -1:
            return None
        data_b64 = cookie_val[:dot_idx]
        provided_sig = cookie_val[dot_idx + 1:]

        expected_sig = hmac.new(
            _get_secret_bytes(),
            data_b64.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(provided_sig, expected_sig):
            return None

        # Decode payload
        pad = "=" * (-len(data_b64) % 4)
        data_bytes = base64.urlsafe_b64decode(data_b64 + pad)
        payload = json.loads(data_bytes)

        # Check expiry
        exp = payload.get("exp", 0)
        if datetime.now(tz=timezone.utc).timestamp() > exp:
            return None

        return payload
    except Exception:  # noqa: BLE001
        return None


# ── Session helpers ───────────────────────────────────────────────────────────

def set_session(response: Response, username: str, role: str) -> None:
    """Set a signed HTTP-only session cookie on the response."""
    exp = int(
        (datetime.now(tz=timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).timestamp()
    )
    payload = {"u": username, "r": role, "exp": exp}
    cookie_val = _sign_cookie(payload)
    is_production = os.getenv("APP_ENV", "production") == "production"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=cookie_val,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_production,
    )


def clear_session(response: Response) -> None:
    """Clear the session cookie."""
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        samesite="lax",
    )


def get_current_user(request: Request) -> dict | None:
    """
    Extract and verify the current user from the session cookie.
    Returns a dict with 'username' and 'role' keys, or None if not authenticated.
    """
    cookie_val = request.cookies.get(SESSION_COOKIE)
    if not cookie_val:
        return None
    payload = _verify_cookie(cookie_val)
    if not payload:
        return None
    return {"username": payload.get("u", ""), "role": payload.get("r", "")}


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def require_auth(request: Request) -> dict:
    """
    FastAPI dependency: require any authenticated user.
    Raises HTTP 401 if not authenticated.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_viewer(request: Request) -> dict:
    """
    FastAPI dependency: require viewer or admin role.
    Raises HTTP 401 if not authenticated, HTTP 403 if insufficient role.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    role = user.get("role", "")
    if role not in ("viewer", "admin"):
        raise HTTPException(status_code=403, detail="Viewer or admin role required")
    return user


def require_admin_cookie(request: Request) -> dict:
    """
    FastAPI dependency: require admin role via cookie session only.
    Raises HTTP 401 if not authenticated, HTTP 403 if insufficient role.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def check_admin_or_token(request: Request) -> dict:
    """
    Check for admin access via either:
      1. Cookie session with admin role (preferred for dashboard)
      2. Bearer token matching ADMIN_API_TOKEN (for API automation)

    Returns user dict on success.
    Raises HTTP 401 or 403 on failure.
    """
    # 1. Try cookie session
    user = get_current_user(request)
    if user and user.get("role") == "admin":
        return user

    # 2. Try Bearer token
    admin_token = os.getenv("ADMIN_API_TOKEN", "").strip()
    if admin_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[len("Bearer "):]
            if hmac.compare_digest(provided, admin_token):
                return {"username": "api-token", "role": "admin"}

    # Determine appropriate error code
    if user:
        raise HTTPException(status_code=403, detail="Admin role required")
    raise HTTPException(status_code=401, detail="Not authenticated")


def has_permission(user: dict, permission: str) -> bool:
    """Return True if the user's role grants the specified permission."""
    role = user.get("role", "")
    return permission in _ROLE_PERMISSIONS.get(role, set())
