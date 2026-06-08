"""
Google Ads API Readiness Checker — PR-ADS-098

Checks whether all required Google Ads environment variables are present
and prints a redacted summary. Never prints full secret values.

Usage:
  python scripts/google_ads_readiness_check.py          # permissive
  python scripts/google_ads_readiness_check.py --strict # exits non-zero if incomplete
"""

import os
import sys


_REQUIRED_VARS = [
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
]

# Optional: omit for direct customer mode; provide for manager mode.
_OPTIONAL_VARS = [
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
]

# Non-secret vars shown in full; all others are redacted.
_SHOW_FULL = {
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "GOOGLE_ADS_CUSTOMER_ID",
}


def _redact(value: str) -> str:
    """Redact a secret value, showing only the last 4 characters."""
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def check_readiness() -> str:
    """Return 'READY', 'PARTIAL', or 'NOT_READY' based on env var presence.

    READY requires all _REQUIRED_VARS; _OPTIONAL_VARS are not required.
    """
    missing = [v for v in _REQUIRED_VARS if not os.getenv(v)]
    if not missing:
        return "READY"
    if len(missing) == len(_REQUIRED_VARS):
        return "NOT_READY"
    return "PARTIAL"


def main() -> None:
    strict = "--strict" in sys.argv

    print()
    print("Google Ads API readiness")
    print("-" * 40)

    for var in _REQUIRED_VARS:
        value = os.getenv(var)
        if value:
            if var in _SHOW_FULL:
                display = value
            else:
                display = _redact(value)
            print(f"  {var}: present  {display}")
        else:
            print(f"  {var}: missing")

    # Optional vars
    login_customer_id = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip()
    if login_customer_id:
        print(f"  GOOGLE_ADS_LOGIN_CUSTOMER_ID: present  {login_customer_id} — manager mode")
    else:
        print("  GOOGLE_ADS_LOGIN_CUSTOMER_ID: optional missing — direct_customer mode")

    status = check_readiness()
    print()
    print(f"Status: {status}")
    print()

    if strict and status != "READY":
        sys.exit(1)


if __name__ == "__main__":
    main()
