"""
scripts/create_user_hash.py

Generate a password hash entry for AUTH_USERS_JSON.

Usage:
    python scripts/create_user_hash.py

Prompts for:
    - username
    - password (hidden input — not echoed or stored)
    - role (admin / viewer / mdr)

Output:
    JSON object suitable for inclusion in AUTH_USERS_JSON

Rules:
    - Password is never printed or logged.
    - No external service required.
    - Uses PBKDF2-HMAC-SHA256 with 260,000 iterations and a random salt.
"""

import getpass
import json
import sys

# Allow running from the repo root without installing the package.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.auth import hash_password

VALID_ROLES = ("admin", "viewer", "mdr")


def main() -> None:
    print()
    print("Logistaas Ads Intelligence — Create User Hash")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("This tool generates a password hash for AUTH_USERS_JSON.")
    print("The password will NOT be stored or printed.\n")

    # Username
    username = input("Username: ").strip()
    if not username:
        print("ERROR: Username cannot be empty.")
        sys.exit(1)

    # Password (hidden)
    password = getpass.getpass("Password: ")
    if not password:
        print("ERROR: Password cannot be empty.")
        sys.exit(1)

    # Confirm password
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("ERROR: Passwords do not match.")
        sys.exit(1)

    # Role
    role_input = input(f"Role ({'/'.join(VALID_ROLES)}): ").strip().lower()
    if role_input not in VALID_ROLES:
        print(f"ERROR: Role must be one of: {', '.join(VALID_ROLES)}")
        sys.exit(1)

    # Hash password
    print("\nHashing password…")
    password_hash = hash_password(password)

    # Clear sensitive variables
    del password
    del confirm

    # Output
    entry = {
        "username": username,
        "password_hash": password_hash,
        "role": role_input,
    }

    print("\nUser entry (add to AUTH_USERS_JSON):\n")
    print(json.dumps(entry, indent=2))
    print()
    print("To configure multiple users, set AUTH_USERS_JSON to a JSON array:")
    print('  AUTH_USERS_JSON=\'[{"username":"...","password_hash":"...","role":"..."}]\'')
    print()
    print("NOTE: Store AUTH_USERS_JSON as a Render environment variable.")
    print("      Never commit password hashes to source control.")


if __name__ == "__main__":
    main()
