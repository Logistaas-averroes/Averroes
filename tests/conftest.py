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

Why that sentence is not enough
-------------------------------
The F3 review made the fair objection that a default which is always present
makes fail-closed behaviour untestable BY DEFAULT: no test would notice a
consumer that quietly stopped handling an unresolved account, because no test
would ever hand it one. "It only sets the variable when absent" is a claim about
this file; it says nothing about whether anything still checks the other branch.

So the claim is now enforced elsewhere, in
``tests/test_pr_ads_156_f3_review_corrections.py`` §4:

  * the default is proven to yield to any test that overrides or deletes it;
  * every repository reader, endpoint and operational command is asserted to
    return unavailable with the variable removed — including over a POPULATED
    table, which is the only case where fail-closed matters;
  * and the list of scoped consumers is checked for EXHAUSTIVENESS against the
    source tree, so a new one cannot be added without fail-closed coverage.

That last check is the one that makes this fixture safe to keep. Without it the
registry would fall behind the code silently, which is the same failure as the
default itself: a guard that no longer guards anything, still passing.
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
