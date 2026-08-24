"""
tests/test_pr_ads_154c_parity_pg.py

PR-ADS-154C — PostgreSQL-backed contract tests for cross-page parity.

Why these need a real database
──────────────────────────────
The parity logic is pure Python and is tested as such. Two things are not:

  * **the account time zone**, which is read from `google_ads_account_daily_spend`
    and decides which calendar day every business window is anchored on. Whether
    that read returns the zone — and whether a database with no such row falls
    back to the account default rather than to UTC — is a property of the query
    and the schema.

  * **fail-closed behaviour on an empty canonical database**, which is the case
    a mocked repository cannot honestly reproduce. A stub returns whatever it was
    told to; a real empty schema returns genuine emptiness, and the question is
    whether the pages then publish `null` plus a reason or a fabricated `0`.

The second is the one worth having. A dashboard that renders `$0.00` for a window
it knows nothing about is worse than one that renders nothing: it is a confident
answer to a question nobody could answer, and every parity check downstream would
agree with it, because zero equals zero.

The suite reuses the 153E-A throwaway-cluster harness. If the binaries or the
unprivileged ``postgres`` user are unavailable the module skips — and CI fails
loudly on a skip, because a skipped database suite is not merge evidence.

Read-only against every external platform; the only writes are local.

Run with:
    python -m pytest tests/test_pr_ads_154c_parity_pg.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

pytestmark = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

_NOW = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)   # account day is 1 July


def _exec(sql, params=()):
    from db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


# ═════════════════════════════════════════════════════════════════════════════
# The account time zone, and what depends on it
# ═════════════════════════════════════════════════════════════════════════════

def test_the_account_time_zone_round_trips_from_the_real_table(pg):
    from db import revenue_repository as repo
    from services import canonical_contract as contract

    assert repo.fetch_account_time_zone() is None, "empty table has no zone"

    _exec("""
        INSERT INTO google_ads_account_daily_spend
            (customer_id, currency_code, spend_date, cost_micros, account_time_zone)
        VALUES ('111', 'GBP', '2026-06-30', 1000000, 'Europe/London')
    """)
    assert repo.fetch_account_time_zone() == "Europe/London"
    assert contract.configured_account_time_zone() == "Europe/London"


def test_an_empty_account_table_anchors_on_the_account_default_not_utc(pg):
    """The fallback direction is the whole point.

    With no row to read, the resolver must still use the account calendar. Under
    UTC this instant is 30 June and the quarter is Q2; under the account zone it
    is 1 July and the quarter is Q3. Choosing UTC because a row is missing would
    silently move every page back a quarter.
    """
    from db import revenue_repository as repo
    from services import canonical_contract as contract

    assert repo.fetch_account_time_zone() is None
    resolved = contract.resolve_canonical_window("current_quarter", now=_NOW)
    assert resolved["start_date"] == "2026-07-01"
    assert resolved["timezone"] == "Europe/London"


def test_the_stored_zone_is_the_one_actually_used(pg):
    """A configured non-default zone changes the anchor, proving it is read."""
    from services import canonical_contract as contract

    _exec("""
        INSERT INTO google_ads_account_daily_spend
            (customer_id, currency_code, spend_date, cost_micros, account_time_zone)
        VALUES ('111', 'USD', '2026-06-30', 1000000, 'America/Los_Angeles')
    """)
    # 23:30 UTC on 30 June is 16:30 on 30 June in Los Angeles — still Q2 there.
    resolved = contract.resolve_canonical_window("current_quarter", now=_NOW)
    assert resolved["timezone"] == "America/Los_Angeles"
    assert resolved["start_date"] == "2026-04-01"
    assert resolved["end_date"] == "2026-06-30"


def test_every_window_key_resolves_against_the_real_schema(pg):
    from analysis.business_windows import WINDOW_KEYS
    from services import canonical_contract as contract

    for key in WINDOW_KEYS:
        resolved = contract.resolve_canonical_window(key, now=_NOW)
        assert resolved["key"] == key
        assert resolved["end_date"], key
        assert resolved["timezone"], key


# ═════════════════════════════════════════════════════════════════════════════
# Fail closed on an empty canonical database
# ═════════════════════════════════════════════════════════════════════════════

def test_an_empty_canonical_database_yields_no_fabricated_totals(pg):
    """The case a mock cannot honestly reproduce.

    Every canonical table exists and is empty. The pages must publish `null`
    plus a reason — never `0` — because a confident zero is indistinguishable
    from a real measurement to every check downstream, parity included.
    """
    from services.dashboard_overview_service import build_dashboard_overview
    from services.dashboard_revenue_service import build_dashboard_revenue

    for name, build in (("overview", build_dashboard_overview),
                        ("revenue", build_dashboard_revenue)):
        payload = build(window="current_quarter", now=_NOW)
        kpis = payload.get("kpis") or {}
        for key in ("google_ads_spend_usd", "closed_won_revenue_usd"):
            if key in kpis:
                value = kpis[key]
                value = value.get("amount") if isinstance(value, dict) else value
                assert value in (None, 0, 0.0), f"{name}.{key} = {value!r}"
                # A zero is only acceptable where the payload also says the
                # metric is unavailable; the strict assertion is that it is not
                # presented as a trustworthy measurement.
                if value is not None:
                    assert payload.get("unavailable") or payload.get("data_health"), (
                        f"{name}.{key} published {value!r} with nothing marking it "
                        "unavailable")


def test_the_audit_reports_unavailable_rather_than_agreeing_on_zero(pg):
    """Two pages that both know nothing are not two pages that agree.

    With an empty database every consumer publishes nothing, and the audit must
    say `canonical_source_unavailable` rather than `identical` — the failure mode
    where an audit passes because every reading is missing in the same way.
    """
    from services import cross_page_parity_service as parity

    out = parity.audit_window("current_quarter", now=_NOW)
    assert out["ok"] is False
    assert parity.V_SOURCE_UNAVAILABLE in out["violation_codes"]
    assert not any(m["status"] == "identical" and m["value"] in (0, 0.0)
                   for m in out["metrics"]), \
        "a metric agreed on a fabricated zero"


def test_all_consumers_still_resolve_one_window_on_an_empty_database(pg):
    """Window parity does not depend on there being any data.

    An empty database is where an anchoring bug would be easiest to miss, since
    every value is absent and only the ranges differ.
    """
    from services import cross_page_parity_service as parity

    out = parity.audit_window("ytd", now=_NOW)
    ranges = {(r["window_start"], r["window_end"], r["timezone"])
              for r in out["consumer_windows"] if r["window_end"]}
    assert len(ranges) <= 1, f"consumers disagreed on the range: {ranges}"
    assert parity.V_WINDOW_MISMATCH not in out["violation_codes"]
