"""
Google Ads Refresh Token Generator — PR-ADS-098

Run this script ONCE on your local machine to generate an OAuth refresh token.

Usage:
  Windows:
    set GOOGLE_ADS_CLIENT_SECRETS_FILE=C:\\Users\\Youssef\\Secrets\\google-ads-client-secret.json
    python scripts/google_ads_generate_refresh_token.py

  Mac/Linux:
    export GOOGLE_ADS_CLIENT_SECRETS_FILE=/Users/youssef/secrets/google-ads-client-secret.json
    python scripts/google_ads_generate_refresh_token.py

After the browser flow completes, copy the printed refresh token into the
GOOGLE_ADS_REFRESH_TOKEN environment variable in Render.

WARNING: Do NOT commit the refresh token to this repository.
WARNING: Do NOT paste the refresh token in chat, email, or Slack.
WARNING: Do NOT save the token to any file in this repository.
"""

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/adwords"]


def main() -> None:
    client_secrets_file = os.environ.get("GOOGLE_ADS_CLIENT_SECRETS_FILE")
    if not client_secrets_file:
        print(
            "ERROR: GOOGLE_ADS_CLIENT_SECRETS_FILE is not set.\n"
            "Set it to the path of your OAuth client JSON file, e.g.:\n"
            "  export GOOGLE_ADS_CLIENT_SECRETS_FILE=/path/to/client-secret.json",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.path.isfile(client_secrets_file):
        print(
            f"ERROR: Client secrets file not found: {client_secrets_file}\n"
            "Ensure the file exists at the specified path.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Starting Google Ads OAuth flow...")
    print(f"Using client secrets file: {client_secrets_file}")
    print("A browser window will open. Log in with youssef.awwad@logistaas.com")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(
        client_secrets_file,
        scopes=SCOPES,
    )
    credentials = flow.run_local_server(port=0, prompt="consent")

    print()
    print("=" * 60)
    print("OAuth flow complete.")
    print()
    print("Your refresh token is:")
    print()
    print(credentials.refresh_token)
    print()
    print("=" * 60)
    print()
    print("⚠️  WARNING: Do NOT commit this token to the repository.")
    print("⚠️  WARNING: Do NOT paste it in chat, email, or Slack.")
    print("⚠️  WARNING: Do NOT save it to any file in this repo.")
    print()
    print("✅  Save this token in Render as GOOGLE_ADS_REFRESH_TOKEN.")
    print("    Render → Averroes service → Environment → GOOGLE_ADS_REFRESH_TOKEN")
    print()


if __name__ == "__main__":
    main()
