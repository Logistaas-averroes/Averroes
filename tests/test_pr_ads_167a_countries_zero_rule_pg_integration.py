"""PR-ADS-167A — the Countries page's zero rule no longer over-withholds.

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

Real PostgreSQL, the real writers and the real ``api_geo`` handler — the
harness of ``test_pr_ads_165a_countries_pg_integration``.

PR-ADS-176B deleted the ``#/geo`` page these cases also rendered through
``loadGeo``. Each page assertion is now the same assertion on the row the page
printed (``"$0"`` → ``spend_usd == 0.0`` published; ``"Unavailable — some
spend in this window has no country"`` → ``None`` with
``spend_location_unresolved``). The endpoint stays until PR-ADS-176C. The rule
itself is shared (``services/country_spend_zero_rule.py``) and is proved on
the one Countries page in ``tests/test_pr_ads_176a_*`` and ``176b_*``.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    RATE, UK, _api, _seed_leads, _spend_rows, _verify,
    _window, _write_fx, _write_spend, live_db,
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
#: What the page printed as "Unavailable — some spend in this window has no
#: country", as the row says it.
UNAVAILABLE_NO_COUNTRY = (None, "unavailable", "spend_location_unresolved")
ZERO = (0.0, "published", None)


def _spend(api) -> dict:
    """{country: (spend_usd, spend_status, spend_reason)} — each row's spend
    as served."""
    return {r["country"]: (r["spend_usd"], r["spend_status"], r["spend_reason"])
            for r in api["geo"]["rows"]}


def _production_shape(cluster, *, loose_in_window: bool):
    """UK spend every day; Jordan's only rows 20 days before the window;
    two unlabelled code-less criteria (one inside the window when asked); Pakistan
    with leads and no geo spend ever."""
    start, end = _window(7)
    old = start - timedelta(days=20)
    _write_fx(old, end)
    rows = [_spend_rows(UK, start + timedelta(days=i), 20.0) for i in range(7)]
    rows.append(_spend_rows(JORDAN, old, 12.0))
    rows.append(_spend_rows(LOOSE_B, old + timedelta(days=1), 4.0))
    rows.append(_spend_rows(LOOSE_A, end if loose_in_window else old, 3.0))
    _write_spend(rows)
    _verify(old, end)
    _seed_leads(cluster, [("u1", "United Kingdom", "qualified"),
                          ("j1", "Jordan", "junk"), ("j2", "jordan", "junk"),
                          ("p1", "Pakistan", "junk")])
    return start, end


def test_01_a_country_whose_code_another_criterion_carries_is_a_measured_zero(
        live_db, tmp_path):
    _production_shape(live_db, loose_in_window=True)
    api = _api("7d")
    spend = _spend(api)
    assert spend["Jordan"] == ZERO
    assert spend["Pakistan"] == UNAVAILABLE_NO_COUNTRY
    assert spend["United Kingdom"] == (7 * 20.0 * RATE, "published", None)
    assert spend["(unresolved location)"][:2] == (3.0 * RATE, "published")

    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert (by["code:JO"]["spend_usd"], by["code:JO"]["spend_status"],
            by["code:JO"]["spend_reason"]) == (0.0, "published", None)
    assert (by["code:PK"]["spend_usd"], by["code:PK"]["spend_reason"]) == (
        None, "spend_location_unresolved")
    assert geo["unresolved_spend"] == {
        "criteria": 1, "conflicting_criteria": 0, "spend_usd": 3.0 * RATE,
        "blocks_zero": True, "zero_withheld_countries": ["Pakistan"]}
    # Totals unchanged by the rule: it decides only what a missing row means.
    assert geo["total_spend_usd"] == round((7 * 20.0 + 3.0) * RATE, 2)


def test_02_with_no_code_less_spend_in_the_window_pakistan_too_is_a_zero(
        live_db, tmp_path):
    _production_shape(live_db, loose_in_window=False)
    api = _api("7d")
    spend = _spend(api)
    assert spend["Pakistan"] == ZERO
    assert spend["Jordan"] == ZERO
    assert "(unresolved location)" not in spend
    assert all(s[1] == "published" for s in spend.values()), spend
    assert api["geo"]["unresolved_spend"] == {
        "criteria": 0, "conflicting_criteria": 0, "spend_usd": None,
        "blocks_zero": False, "zero_withheld_countries": []}


def test_03_a_criterion_in_the_window_with_two_codes_withholds_every_spendless_zero(
        live_db, tmp_path):
    """That criterion IS some country — possibly Jordan. Nothing is carried
    away from it, so no spend-less country is a zero."""
    start, end = _production_shape(live_db, loose_in_window=False)
    twice = "2999"
    _write_spend([_spend_rows((twice, "FR", "France"), start - timedelta(days=15), 9.0),
                  _spend_rows((twice, "DE", "Germany"), end, 30.0)])
    api = _api("7d")
    spend = _spend(api)
    assert spend["Jordan"] == UNAVAILABLE_NO_COUNTRY
    assert spend["Pakistan"] == UNAVAILABLE_NO_COUNTRY
    assert spend["United Kingdom"] == (7 * 20.0 * RATE, "published", None)
    loose = api["geo"]["unresolved_spend"]
    assert (loose["conflicting_criteria"], loose["blocks_zero"],
            loose["zero_withheld_countries"]) == (1, True, ["Jordan", "Pakistan"])


def test_04_a_code_under_two_criteria_is_not_evidence_and_the_zero_is_withheld(
        live_db, tmp_path):
    """One country, one criterion is what makes the loose spend "not Jordan".
    Where the stored data shows Jordan under two criteria that premise is
    false for Jordan, so its zero is not measured. (Production: 212 criteria,
    212 codes — no code under two criteria.)"""
    start, _end = _production_shape(live_db, loose_in_window=True)
    _write_spend([_spend_rows(("20401", "JO", "Jordan"), start - timedelta(days=18), 1.0)])
    api = _api("7d")
    assert _spend(api)["Jordan"] == UNAVAILABLE_NO_COUNTRY
    assert api["geo"]["unresolved_spend"]["zero_withheld_countries"] == [
        "Jordan", "Pakistan"]


def test_05_the_carrier_evidence_unreadable_is_carried_by_nothing(
        live_db, tmp_path, monkeypatch):
    _production_shape(live_db, loose_in_window=True)
    from db import revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_geo_code_carriers",
                        lambda codes: {"available": False, "carriers": {}})
    api = _api("7d")
    spend = _spend(api)
    assert spend["Jordan"] == UNAVAILABLE_NO_COUNTRY
    assert spend["Pakistan"] == UNAVAILABLE_NO_COUNTRY
    assert spend["United Kingdom"] == (7 * 20.0 * RATE, "published", None)


def test_06_an_uncovered_window_publishes_no_zero_whatever_is_carried(
        live_db, tmp_path):
    """Rule (a): the carrier evidence never turns an uncovered window's
    missing row into a zero."""
    start, end = _production_shape(live_db, loose_in_window=True)
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_coverage")
        conn.commit()
    _verify(start - timedelta(days=20), end - timedelta(days=1))
    api = _api("7d")
    assert api["geo"]["coverage"]["status"] == "not_covered"
    spend = _spend(api)
    assert spend["Jordan"][:2] == (None, "unavailable")
    assert all(s[0] is None for s in spend.values()), spend
    assert api["geo"]["total_spend_usd"] is None


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
