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

#: Every module that binds ``get_conn`` BY VALUE at import time, i.e.
#: ``from db.connection import get_conn`` at module scope.
#:
#: Why this list exists
#: --------------------
#: ``monkeypatch.setattr(db.connection, "get_conn", fake)`` replaces the
#: attribute on ``db.connection`` and restores it on teardown. That is correct
#: for any module that resolves the name through the module at call time — and
#: WRONG for any module that copied the function into its own namespace at
#: import time. Such a module keeps whichever object was bound when IT was first
#: imported.
#:
#: So if a module's first import happens DURING a test that has a fake
#: installed, it captures the fake permanently, and every later test in the same
#: process writes through a fake cursor. `db.writers` swallows the resulting
#: error and logs it, so the symptom is not a crash: writes silently vanish,
#: reads legitimately return nothing, and unrelated suites fail with empty
#: result sets far from the cause.
#:
#: That is exactly what happened in PR-ADS-157: adding one call to the canonical
#: keyword evidence service in the campaign drawer moved the first import of
#: `db.writers` into `test_pr_ads_141_evidence_lead_foundation.py`, whose
#: fixtures patch `get_conn` — and nine PostgreSQL tests in PR-ADS-146 and
#: PR-ADS-146C began failing with empty tables. Nothing was wrong with those
#: tests, the new code, or the database.
#:
#: Importing these modules once, before any test runs, removes the hazard for
#: the whole suite: the binding they capture is always the real ``get_conn``.
#: This is a test-isolation fix and changes no production behaviour — in
#: production nothing patches ``db.connection``.
_EAGER_DB_MODULES = (
    "db.connection",
    "db.writers",
    "db.keyword_repository",
    "db.platform_sql_attribution_repository",
    "db.search_term_repository",
    "db.search_term_review_repository",
    "db.revenue_repository",
    "db.canonical_contact_outcome_repository",
    "db.crm_funnel_repository",
    "db.deal_ledger_repository",
    "db.mailchimp_repository",
    "db.schema",
)


@pytest.fixture(scope="session", autouse=True)
def _import_db_modules_before_any_fake_is_installed():
    """Bind every by-value ``get_conn`` importer to the REAL function.

    Ordered before any test so no module can capture a monkeypatched connection
    factory as its permanent binding. An import failure is not fatal — a module
    that cannot be imported here will fail loudly in the test that needs it,
    which is a better error than a silent one.
    """
    import importlib

    for name in _EAGER_DB_MODULES:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001, S110
            pass


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
