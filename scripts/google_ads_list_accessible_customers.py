"""
Google Ads List Accessible Customers — PR-ADS-101

Proves which Google Ads customer accounts the configured OAuth user can access.
Uses CustomerService.list_accessible_customers() (read-only, no mutate calls).

If the target customer (e.g. customers/3059734490) does not appear in the output,
the OAuth user needs to be granted access before the smoke test will succeed.

Usage:
  python scripts/google_ads_list_accessible_customers.py

Prerequisites:
  GOOGLE_ADS_DEVELOPER_TOKEN
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_REFRESH_TOKEN
  GOOGLE_ADS_CUSTOMER_ID
  GOOGLE_ADS_LOGIN_CUSTOMER_ID  (optional — omit for direct customer mode)
"""

import os
import sys

# Ensure repo root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors.google_ads_direct import build_google_ads_client, get_access_mode


def main() -> None:
    print()
    print("Google Ads accessible customers")
    print("-" * 40)

    try:
        client = build_google_ads_client()
    except EnvironmentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    mode = get_access_mode()
    print(f"Mode: {mode}")

    customer_service = client.get_service("CustomerService")
    response = customer_service.list_accessible_customers()

    resource_names = list(response.resource_names)
    if not resource_names:
        print("No accessible customers returned.", file=sys.stderr)
        print(
            "Grant the OAuth user access to the Google Ads account, "
            "then regenerate the refresh token.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Accessible customers:")
    for name in resource_names:
        print(f"  - {name}")
    print()


if __name__ == "__main__":
    main()
