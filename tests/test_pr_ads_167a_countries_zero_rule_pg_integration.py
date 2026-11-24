"""PR-ADS-167A — the Countries zero rule no longer over-withholds.

PR-ADS-165A, known gap 1. A resolved lead country with no spend row read
"Unavailable — some spend in this window has no country" whenever ANY spend in
the window had no country. Production (read-only, 2026-11-19): 35,297 coded
rows over 212 criteria carrying 212 codes, strictly one-to-one, and 25
code-less rows over two criteria, ``2192`` and ``2364``, that carry no code on
any row (about £11, newest day 2026-11-06). So for a fortnight after any spend
on either, every spend-less lead country on the page read "Unavailable".

Google keys one country under one criterion. A criterion no stored row names
is therefore not a country whose code ANOTHER criterion carries on record —
the page already trusts that identity evidence to name spend. So a country
whose code is carried on record by exactly one criterion (which carries nothing
else) is a measured ``$0`` in a covered window even while code-less spend is in
it. A country never seen in geo spend (Pakistan: leads, no Google spend ever)
could be the loose location, and stays "Unavailable" while there is any.

The rule is shared (``services/country_spend_zero_rule.py``, PR-ADS-176A).
These cases ran against ``GET /api/geo``. PR-ADS-176C retired that feed, so
each one now runs the same evidence through the two live country surfaces —
the mart's country view (the one Countries page) and Dashboard → Countries —
on real PostgreSQL with the real writers, over the mart's business window
with campaign spend that reconciles (the harness of
``tests/test_pr_ads_176c_country_feed_retired_pg_integration``). Every
assertion keeps its meaning: ``(None, "spend_location_unresolved")`` is what
/api/geo said as ``(None, "unavailable", "spend_location_unresolved")``.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, RATE, UK, _spend_rows, _verify, _write_spend, live_db,
)
from tests.test_pr_ads_176c_country_feed_retired_pg_integration import (  # noqa: E402
    _assert_nothing_published, _bounds, _by_code, _dash, _dash_by_code, _deal_sync,
    _leads, _mart, _seed,
)

#: Two criteria that carry no code on any row and that nothing names — the
#: state production's `2192` and `2364` were in when PR-ADS-167A was written.
#: PR-ADS-170A: the owner has since labelled those two (Cuba, Iran), so they no
#: longer exercise "spend no stored row names". These ids are NOT in
#: config/geo_owner_labels.yaml; every assertion below is unchanged. The
#: labelled pair is covered in tests/test_pr_ads_170a_*.
LOOSE_A = ("2997", None, None)
LOOSE_B = ("2998", None, None)
#: A country Google has reported under its own criterion — but not lately.
JORDAN = ("2400", "JO", "Jordan")
#: What /api/geo printed as "Unavailable — some spend in this window has no
#: country", as a live row says it: ``(spend, spend_reason)``.
UNAVAILABLE_NO_COUNTRY = (None, "spend_location_unresolved")
ZERO = (0.0, None)


def _spend(mart) -> dict:
    """{ISO code: (spend, spend_reason)} — each market row's spend as served."""
    return {r["country_code"]: (r["spend"], r.get("spend_reason"))
            for r in mart["rows"] if not r.get("is_residual")}


def _dash_spend(page) -> dict:
    return {r["country_code"]: (r["spend_native"], r.get("spend_reason"))
            for r in page["countries"]}


def _production_shape(cluster, *, loose_in_window: bool, extra=()):
    """UK spend on each of the last seven days; Jordan's only row 20 days
    before the window; two unlabelled code-less criteria (one inside the window
    when asked); Pakistan with leads and no geo spend ever."""
    start, today, y = _bounds()
    old = start - timedelta(days=20)
    rows = [_spend_rows(UK, y - timedelta(days=i), 20.0) for i in range(7)]
    rows.append(_spend_rows(JORDAN, old, 12.0))
    rows.append(_spend_rows(LOOSE_B, old + timedelta(days=1), 4.0))
    rows.append(_spend_rows(LOOSE_A, y if loose_in_window else old, 3.0))
    rows += list(extra)
    lo = _seed(rows)
    _deal_sync(cluster)
    _leads(cluster, [("u1", "United Kingdom", "qualified"),
                     ("j1", "Jordan", "junk"), ("j2", "jordan", "junk"),
                     ("p1", "Pakistan", "junk")])
    return lo, start, today, y


def test_01_a_country_whose_code_another_criterion_carries_is_a_measured_zero(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _production_shape(live_db, loose_in_window=True)
    mart = _mart()
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    spend = _spend(mart)
    assert spend["JO"] == ZERO
    assert spend["PK"] == UNAVAILABLE_NO_COUNTRY
    assert spend["GB"] == (7 * 20.0 * RATE, None)
    # The loose spend is real spend, in the residual — never a country's.
    residual = _by_code(mart)["residual"]
    assert residual["spend_components"]["country_unidentified"] == 3.0 * RATE
    # Totals unchanged by the rule: it decides only what a missing row means.
    assert sum(r["spend"] or 0 for r in mart["rows"]) == pytest.approx((7 * 20.0 + 3.0) * RATE)

    page = _dash()
    d = _dash_spend(page)
    assert d["JO"] == (0.0, None) and d["PK"] == UNAVAILABLE_NO_COUNTRY
    assert page["kpis"]["zero_withheld_countries"] == ["Pakistan"]


def test_02_with_no_code_less_spend_in_the_window_pakistan_too_is_a_zero(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _production_shape(live_db, loose_in_window=False)
    mart = _mart()
    spend = _spend(mart)
    assert spend["PK"] == ZERO
    assert spend["JO"] == ZERO
    assert all(s[0] is not None for s in spend.values()), spend
    rows = _by_code(mart)
    assert "residual" not in rows                     # no spend without a country
    page = _dash()
    assert _dash_spend(page)["PK"] == (0.0, None)
    assert page["kpis"]["zero_withheld_countries"] == []


def test_03_a_criterion_in_the_window_with_two_codes_withholds_every_spendless_zero(
        live_db, monkeypatch):
    """That criterion IS some country — possibly Jordan. Nothing is carried
    away from it, so no spend-less country is a zero. (165A case 16: its spend
    is not given to either of its codes.)"""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    twice = "2999"
    _production_shape(live_db, loose_in_window=False, extra=[
        _spend_rows((twice, "FR", "France"), start - timedelta(days=15), 9.0),
        _spend_rows((twice, "DE", "Germany"), y, 30.0)])
    _leads(live_db, [("g1", "Germany", "qualified"), ("f1", "France", "junk")])
    mart = _mart()
    spend = _spend(mart)
    assert spend["JO"] == UNAVAILABLE_NO_COUNTRY
    assert spend["PK"] == UNAVAILABLE_NO_COUNTRY
    assert spend["DE"] == UNAVAILABLE_NO_COUNTRY and spend["FR"] == UNAVAILABLE_NO_COUNTRY
    assert spend["GB"] == (7 * 20.0 * RATE, None)
    assert _by_code(mart)["residual"]["spend_components"]["country_unidentified"] == (
        30.0 * RATE)
    page = _dash()
    assert sorted(page["kpis"]["zero_withheld_countries"]) == [
        "France", "Germany", "Jordan", "Pakistan"]


def test_04_a_code_under_two_criteria_is_not_evidence_and_the_zero_is_withheld(
        live_db, monkeypatch):
    """One country, one criterion is what makes the loose spend "not Jordan".
    Where the stored data shows Jordan under two criteria that premise is
    false for Jordan, so its zero is not measured. (Production: 212 criteria,
    212 codes — no code under two criteria.)"""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _production_shape(live_db, loose_in_window=True, extra=[
        _spend_rows(("20401", "JO", "Jordan"), start - timedelta(days=18), 1.0)])
    mart = _mart()
    assert _spend(mart)["JO"] == UNAVAILABLE_NO_COUNTRY
    assert _dash()["kpis"]["zero_withheld_countries"] == ["Jordan", "Pakistan"]


def test_05_the_carrier_evidence_unreadable_is_carried_by_nothing(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _production_shape(live_db, loose_in_window=True)
    from db import revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_geo_code_carriers",
                        lambda codes: {"available": False, "carriers": {}})
    spend = _spend(_mart())
    assert spend["JO"] == UNAVAILABLE_NO_COUNTRY
    assert spend["PK"] == UNAVAILABLE_NO_COUNTRY
    assert spend["GB"] == (7 * 20.0 * RATE, None)
    d = _dash_spend(_dash())
    assert d["JO"] == UNAVAILABLE_NO_COUNTRY and d["PK"] == UNAVAILABLE_NO_COUNTRY


def test_06_an_uncovered_window_publishes_no_zero_whatever_is_carried(
        live_db, monkeypatch):
    """Rule (a): the carrier evidence never turns an uncovered window's
    missing row into a zero."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    lo, start, today, y = _production_shape(live_db, loose_in_window=True)
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_coverage")
        conn.commit()
    _verify(lo, y - timedelta(days=1))
    mart, page = _mart(), _dash()
    _assert_nothing_published(mart, page, "geo_coverage_incomplete")
    assert all(s[0] is None for s in _spend(mart).values())
    assert _dash_by_code(page)["JO"]["spend_native"] is None


def test_07_the_carrier_read_is_one_statement_of_the_shared_identity_rule():
    """The carrier read and every other identity read run the SAME statement
    (`db.geo_identity_sql`) — one rule, not a copy per reader."""
    src = (ROOT / "db" / "revenue_repository.py").read_text(encoding="utf-8")
    body = src[src.index("def fetch_geo_code_carriers"):]
    body = body[:body.index("\ndef ")]
    assert "criterion_identity_sql(" in body
    for fn in ("def fetch_geo_criterion_identities",
               "def fetch_geo_daily_spend_by_country"):
        part = src[src.index(fn):]
        part = part[:part.index("\ndef ")]
        assert "criterion_identity_sql(" in part, fn
        assert "MAX(g.country_code)" not in part, fn


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
