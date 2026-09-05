"""
tests/conftest.py

PR-ADS-156-F3 — the test environment has a CONFIGURED GOOGLE ADS ACCOUNT.

Every canonical search-term read is now scoped to the effective configured
customer (``analysis.search_term_scope``), and correctly fails closed when no
account is configured. Production always has one; a test process that did not
would exercise the unavailable branch everywhere by accident, and hundreds of
assertions about page content would start passing or failing for a reason that
has nothing to do with what they are testing.

So the default test world matches production: one configured account, whose id
is the one the fixtures stamp on their rows.

This does NOT hide the fail-closed path. It sets the variable only when it is
absent, so a case that deliberately unsets or overrides it — the F3 test that
proves an unresolved account produces unavailable rather than unscoped totals —
still gets exactly the environment it asks for.
"""

from __future__ import annotations

import os

import pytest

#: The account every fixture stamps and every scoped read resolves to. One
#: value, so a row seeded in one suite is visible to a reader exercised in
#: another and the two cannot silently disagree.
TEST_GOOGLE_ADS_CUSTOMER_ID = "555"


@pytest.fixture(scope="session", autouse=True)
def _configured_google_ads_account():
    """Ensure a configured account for the session, without overriding one."""
    preset = os.environ.get("GOOGLE_ADS_CUSTOMER_ID")
    if preset:
        yield preset
        return
    os.environ["GOOGLE_ADS_CUSTOMER_ID"] = TEST_GOOGLE_ADS_CUSTOMER_ID
    try:
        yield TEST_GOOGLE_ADS_CUSTOMER_ID
    finally:
        os.environ.pop("GOOGLE_ADS_CUSTOMER_ID", None)
