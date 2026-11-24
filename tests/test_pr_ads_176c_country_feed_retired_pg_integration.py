"""PR-ADS-176C — the legacy country feed is retired; its truth properties are
held by the surfaces that are left.

``GET /api/geo`` (``services/country_evidence_service``) and
``GET /api/leads/country-summary`` fed the ``#/geo`` page PR-ADS-176B deleted.
Nothing in ``static/`` read them. Their PostgreSQL suites (PR-ADS-165A,
167A, 170A) asserted a long list of truth properties on the ``/api/geo``
payload. Retiring the feed must not retire a property, so each one is
asserted here — or in its original module, rewritten — on the two country
surfaces that are live:

* the mart's country view, ``build_revenue_decision_mart(view="country")``
  (``GET /api/revenue-performance?view=country``: the one Countries page), and
* Dashboard → Countries, ``build_dashboard_countries`` (``GET
  /api/dashboard/countries``).

Real PostgreSQL, the real writers, the real sync where the old case drove it,
and the real builders. The window is the mart's business window (Last 6
Months, which ends yesterday on the country views — PR-ADS-167A), so every
fixture writes campaign spend that reconciles with its geo spend: the live
gate is the strict one (decision 2), and a fixture that failed it would
prove nothing about the property under test.

The port found four properties the live surfaces did not hold (F1, F2, F3, F5:
docs/58 §8.4). PR-ADS-176C-F1 fixed each in the code; the cases here assert
the correct behaviour and fail on 2b056a4. The port table, old case → new
home, is docs/58 §8.3.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, GE, KZ, NO_CODE, OTHER_CUSTOMER, RATE, UAE, UK, _backdate_ledger,
    _failed_lookup_sync, _GoogleAds, _run_real_sync, _seed_account_zone,
    _seed_legacy_monthly_snapshot, _spend_rows, _today, _verify, _write_spend,
    live_db,
)
from tests.test_pr_ads_167a_settled_coverage_consumers_pg_integration import (  # noqa: E402
    _window_dates,
)

WINDOW = "last_6_months"
WORKFLOW = ROOT / ".github" / "workflows" / "pr-ads-153d-checks.yml"
#: Google keys Jordan under its own criterion — but not lately.
JORDAN = ("2400", "JO", "Jordan")
#: A criterion no stored row names and no owner label names.
LOOSE = ("2997", None, None)
FIJI = ("2242", "FJ", "Fiji")
KOSOVO = ("2900", "XK", "Kosovo")


# ── seed ────────────────────────────────────────────────────────────────────
def _bounds():
    """(window start, today, yesterday) of the mart's Last 6 Months."""
    start, today = _window_dates(WINDOW)
    return start, today, today - timedelta(days=1)


def _fx(lo, hi, *, skip=()):
    import db.writers as writers
    rows, d = [], lo
    while d <= hi:
        if d not in skip:
            rows.append({"rate_date": d, "base_currency": "GBP", "quote_currency": "USD",
                         "rate": RATE, "provider": "test", "source_version": "test"})
        d += timedelta(days=1)
    assert writers.upsert_fx_rates(rows) == len(rows)


def _campaign_from_geo(lo, hi, *, geo_ledger=True):
    """Campaign spend for the configured account equal, day by day, to its geo
    spend as stored — so the gate's geo ↔ campaign reconciliation holds and the
    property under test is the only thing that can withhold a figure. The
    campaign ledger verified over [lo, hi], fetched now; the geo ledger too
    unless ``geo_ledger=False`` (a case whose geo ledger the real sync wrote)."""
    import db.writers as writers
    from db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT spend_date, SUM(cost_micros) FROM google_ads_geo_daily_spend "
                    "WHERE customer_id = %s AND spend_date BETWEEN %s AND %s "
                    "GROUP BY spend_date ORDER BY spend_date", (CUSTOMER, lo, hi))
        per_day = cur.fetchall()
    camp = [{"customer_id": CUSTOMER, "currency_code": "GBP", "campaign_id": "111",
             "campaign_name": "brand - uk", "spend_date": d, "cost_micros": int(m),
             "source_query_version": "t"} for d, m in per_day]
    if camp:
        assert writers.upsert_campaign_daily_spend(camp) == len(camp)
    assert writers.upsert_spend_coverage(
        CUSTOMER, lo, hi, "verified", rows_written=len(camp),
        cost_micros_total=sum(c["cost_micros"] for c in camp))
    if geo_ledger:
        _verify(lo, hi)


def _seed(geo_rows, *, pad_days=30, fx_skip=()):
    """Write ``geo_rows``, FX for every day, reconciling campaign spend and
    both ledgers verified from ``pad_days`` before the window through today."""
    start, today, _ = _bounds()
    lo = start - timedelta(days=pad_days)
    _fx(lo, today, skip=fx_skip)
    if geo_rows:
        _write_spend(geo_rows)
    _campaign_from_geo(lo, today)
    return lo


def _deal_sync(cluster):
    from tests.test_pr_ads_153e_b_pg_integration import _sync_state
    _sync_state(cluster)


def _leads(cluster, rows):
    """``rows``: [(contact_id, country, status_category)] — paid-search leads
    with a real campaign, created yesterday: the mart's lead population
    (``fetch_lead_quality``)."""
    _, _, yesterday = _bounds()
    created = datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc) \
        + timedelta(hours=10)
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO runs (run_type, started_at, status) VALUES "
                    "('daily', now(), 'success') RETURNING id")
        run_id = cur.fetchone()[0]
        for contact_id, country, category in rows:
            cur.execute(
                "INSERT INTO leads (run_id, run_date, contact_id, country, "
                "status_category, campaign_name, keyword, source_type, "
                "contact_created_at) VALUES (%s, CURRENT_DATE, %s, %s, %s, "
                "'brand - uk', 'tms', 'paid_search', %s)",
                (run_id, contact_id, country, category, created))
        conn.commit()


def _mart(window=WINDOW):
    from services.revenue_decision_mart import build_revenue_decision_mart
    return json.loads(json.dumps(build_revenue_decision_mart(view="country", window=window),
                                 default=str))


def _dash(window=WINDOW):
    from services.dashboard_countries_service import build_dashboard_countries
    return json.loads(json.dumps(build_dashboard_countries(window), default=str))


def _by_code(mart) -> dict:
    """Market rows by ISO code; the residual under ``"residual"``."""
    return {("residual" if r.get("is_residual") else r.get("country_code")): r
            for r in mart["rows"]}


def _dash_by_code(page) -> dict:
    return {r.get("country_code"): r for r in page["countries"]}


def _items(mart) -> dict:
    res = _by_code(mart)["residual"]
    return {i["country_code"]: i for i in res.get("outside_listed_market_items") or []}


def _assert_nothing_published(mart, page, reason=None):
    """The gate withholds: no row and no total carries a spend figure, a lead
    count or an SQL count — never a zero, never a partial (165A
    ``_assert_no_spend_figure_in_the_payload``, on the live surfaces)."""
    st = mart["spend_truth"]
    assert st["country_spend_status"] not in ("verified", "reconciled_with_residual"), st
    if reason:
        assert reason in st["country_gap_codes"], st["country_gap_codes"]
    for r in mart["rows"]:
        assert r["spend"] is None, r
        for k in ("leads", "sqls", "closed_won_deals", "won_revenue",
                  "period_revenue_per_ad_spend"):
            assert r.get(k) is None, (k, r)
    tot = mart["country_totals"]
    for k in ("spend", "leads", "sqls", "closed_won_deals", "won_revenue"):
        assert tot[k] is None, (k, tot)
    assert tot["metrics"]["spend"]["status"] == "unavailable"
    k = page["kpis"]
    assert k["verified_spend_usd"] is None
    for r in page["countries"]:
        assert r["spend_usd"] is None, r


# ═════════════════════════════════════════════════════════════════════════════
# 165A (i): a covered window publishes exact per-country spend, once
# ═════════════════════════════════════════════════════════════════════════════
def test_01_restated_days_count_once_and_other_accounts_and_days_count_never(live_db, monkeypatch):
    """165A case 01. The daily sync's lookback REPLACES recent days with
    Google's restated figures (``replace_geo_daily_spend_chunk``): each day is
    counted once. Another account's spend, a day before the window and today
    (the country views end yesterday) are on no row."""
    import db.writers as writers
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    days = [y - timedelta(days=6 - i) for i in range(7)]
    first = []
    for d in days:
        first += [_spend_rows(UK, d, 60.0, campaign="c1"),
                  _spend_rows(UK, d, 40.0, campaign="c2"),
                  _spend_rows(UAE, d, 40.0, campaign="c1")]
    _write_spend(first, run="sync-1")
    restated = [_spend_rows(UK, d, 70.0, campaign="c1") for d in days[-3:]]
    restated += [_spend_rows(UK, d, 40.0, campaign="c2") for d in days[-3:]]
    restated += [_spend_rows(UAE, d, 40.0, campaign="c1") for d in days[-3:]]
    out = writers.replace_geo_daily_spend_chunk(CUSTOMER, days[-3], days[-1], restated,
                                                sync_run_id="sync-2")
    assert (out["replaced"], out["deleted"], out["written"]) == (True, 9, 9)
    lo = _seed([])
    # Outside the window, and another account: written after the campaign side
    # was derived, so they cannot reconcile their way onto a row.
    _write_spend([_spend_rows(UK, start - timedelta(days=1), 5000.0),
                  _spend_rows(UK, y, 9000.0, customer=OTHER_CUSTOMER),
                  _spend_rows(UK, today, 3000.0)])
    assert lo < start
    _deal_sync(live_db)

    uk_usd, uae_usd = (4 * 100.0 + 3 * 110.0) * RATE, 7 * 40.0 * RATE
    assert (uk_usd, uae_usd) == (912.5, 350.0)
    mart = _mart()
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    rows = _by_code(mart)
    assert (rows["GB"]["spend"], rows["AE"]["spend"]) == (912.5, 350.0)
    assert set(rows) == {"GB", "AE"}               # nothing lands in a residual
    assert mart["country_totals"]["spend"] == 1262.5
    # The metrics the canonical table does not hold are not on any row.
    for r in mart["rows"]:
        assert not {"clicks", "impressions", "conversions"} & set(r), r
    page = _dash()
    d = _dash_by_code(page)
    assert (d["GB"]["spend_native"], d["GB"]["spend_usd"]) == (730.0, 912.5)
    assert (d["AE"]["spend_native"], d["AE"]["spend_usd"]) == (280.0, 350.0)
    assert page["kpis"]["verified_spend_usd"] == pytest.approx(1262.5)
    for r in page["countries"]:
        assert not {"clicks", "impressions", "conversions"} & set(r), r


# ═════════════════════════════════════════════════════════════════════════════
# 165A (ii) / 167A case 06: an uncovered window publishes nothing
# ═════════════════════════════════════════════════════════════════════════════
def _sync_is_behind(lo, y, today):
    _verify(lo, y - timedelta(days=3))
    return ["geo_coverage_incomplete"]


def _one_day_was_never_fetched(lo, y, today):
    hole = y - timedelta(days=3)
    _verify(lo, hole - timedelta(days=1))
    _verify(hole + timedelta(days=1), today)
    return ["geo_coverage_incomplete"]


def _a_chunk_failed(lo, y, today):
    _verify(lo, y - timedelta(days=1))
    _verify(y, today, status="failed")
    return ["geo_coverage_incomplete", "geo_coverage_has_failed_chunks"]


@pytest.mark.parametrize("gap", [_sync_is_behind, _one_day_was_never_fetched,
                                 _a_chunk_failed],
    ids=["sync behind", "gap in the middle", "failed chunk"])
def test_02_an_uncovered_window_shows_no_zero_and_no_partial_sum(gap, live_db, monkeypatch):
    """165A case 02 and 167A case 06. Four days of UK spend ARE stored — their
    sum is the partial that must not be printed as the window's figure — and
    Jordan (leads, a carrier on record) would be a zero in a covered window."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    lo = _seed([_spend_rows(UK, y - timedelta(days=i), 100.0) for i in range(4)]
               + [_spend_rows(JORDAN, start - timedelta(days=20), 12.0)])
    _deal_sync(live_db)
    _leads(live_db, [("h1", "United Kingdom", "qualified"), ("h2", "Jordan", "junk")])
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_coverage")
        conn.commit()
    codes = gap(lo, y, today)

    mart, page = _mart(), _dash()
    _assert_nothing_published(mart, page, codes[0])
    for code in codes:
        assert code in mart["spend_truth"]["country_gap_codes"], code
    rows = _by_code(mart)
    assert rows["JO"]["spend"] is None                  # no zero
    assert rows["GB"]["spend"] is None                  # no partial ($500)
    # The gap is in the COVERAGE: no native figure either.
    assert page["kpis"]["verified_spend_native"]["amount"] is None
    for r in page["countries"]:
        assert r["spend_native"] is None, r
    assert "500.0" not in json.dumps(mart["rows"]) + json.dumps(page["countries"])


def test_02_b_nothing_synced_or_only_another_accounts_history_is_not_covered(
        live_db, monkeypatch):
    """165A case 02b, first two halves. The no-account half is case 02c."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    lo = _seed([_spend_rows(UK, y, 100.0)])
    _deal_sync(live_db)
    _leads(live_db, [("h1", "Jordan", "qualified")])
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_coverage")
        conn.commit()
    _assert_nothing_published(_mart(), _dash(), "geo_coverage_incomplete")
    _verify(lo, today, customer=OTHER_CUSTOMER)
    _assert_nothing_published(_mart(), _dash(), "geo_coverage_incomplete")


def test_02_c_with_no_account_configured_two_accounts_are_never_summed(
        live_db, monkeypatch):
    """165A case 02b, third half: "no account configured: the question cannot
    be asked". The live surfaces answer it by PR-ADS-154B's design: with no
    account configured they read the one account the data holds (its spend
    and its ledger), and with two accounts in the data the comparison is not
    like for like, so BOTH surfaces withhold — never two accounts summed.

    PR-ADS-176C-F1 (F2): the Dashboard kept the UK's native spend summed over
    both accounts (£700) on its row and its card, labelled "USD unavailable
    (FX)"."""
    import db.writers as writers
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    lo = _seed([_spend_rows(UK, y, 200.0)])
    _deal_sync(live_db)
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID")
    # PR-ADS-154B's design: one account in the data, none configured — the
    # surfaces read that account's spend and ledger, and publish.
    one = _mart()
    assert one["spend_truth"]["country_spend_status"] == "verified"
    assert _by_code(one)["GB"]["spend"] == 250.0

    # A second account's UK spend, campaign spend and ledgers.
    _write_spend([_spend_rows(UK, y, 500.0, campaign="999", customer=OTHER_CUSTOMER)])
    assert writers.upsert_campaign_daily_spend([{
        "customer_id": OTHER_CUSTOMER, "currency_code": "GBP", "campaign_id": "999",
        "campaign_name": "other", "spend_date": y, "cost_micros": 500_000_000,
        "source_query_version": "t"}]) == 1
    assert writers.upsert_spend_coverage(OTHER_CUSTOMER, lo, today, "verified",
                                         rows_written=1, cost_micros_total=500_000_000)
    _verify(lo, today, customer=OTHER_CUSTOMER)
    mart, page = _mart(), _dash()
    assert mart["spend_truth"]["country_gap_codes"] == ["comparison_not_like_for_like"]
    _assert_nothing_published(mart, page)
    # Two accounts are never summed onto a row or a card — neither in USD nor
    # natively (F2: the Dashboard kept £700 native).
    _assert_no_native_spend(page)
    assert "875.0" not in json.dumps(mart["rows"])
    assert "700.0" not in json.dumps(page["countries"]) + json.dumps(page["kpis"])


def _assert_no_native_spend(page):
    """F2: when any gap but FX withholds the gate, Dashboard → Countries
    carries no native spend either — rows, card, outside-market amount."""
    k = page["kpis"]
    assert k["verified_spend_native"]["amount"] is None, k["verified_spend_native"]
    assert k["countries_with_spend"] is None
    out = k.get("spend_outside_listed_markets") or {}
    assert out.get("native") is None and out.get("usd") is None, out
    for r in page["countries"]:
        assert r["spend_native"] is None and r["spend_usd"] is None, r
    assert page["residual"].get("spend_native") is None


def test_02_d_any_gap_but_fx_withholds_native_spend_on_the_dashboard_as_on_the_mart(
        live_db, monkeypatch):
    """PR-ADS-176C-F1 (F2). Geo COVERAGE is complete in each case; another
    gap withholds the gate. Dashboard → Countries now follows the mart's one
    rule (``geo_gate.native_country_spend_publishable``): native spend only
    for an FX-only gap (case 03c keeps it), nothing for any other."""
    import db.writers as writers
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(UK, y, 100.0)])
    _deal_sync(live_db)
    # totals_differ: campaign spend £7,777 beside £100 of geo spend. The card
    # read "£0.00 native" in the legacy-only shape and £100 here.
    assert writers.upsert_campaign_daily_spend([{
        "customer_id": CUSTOMER, "currency_code": "GBP", "campaign_id": "111",
        "campaign_name": "brand - uk", "spend_date": y, "cost_micros": 7_777_000_000,
        "source_query_version": "t"}]) == 1
    mart, page = _mart(), _dash()
    assert mart["spend_truth"]["geo_coverage_status"] == "complete"
    assert mart["spend_truth"]["country_spend_status"] == "mismatch"
    assert "fx_coverage_incomplete" not in mart["spend_truth"]["country_gap_codes"]
    _assert_nothing_published(mart, page)
    _assert_no_native_spend(page)
    # Unreadable campaign spend (the reconciliation baseline).
    from db import revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend",
                        lambda *a, **k: {"available": False, "rows": []})
    mart, page = _mart(), _dash()
    assert mart["spend_truth"]["country_spend_status"] not in (
        "verified", "reconciled_with_residual")
    _assert_no_native_spend(page)


# ═════════════════════════════════════════════════════════════════════════════
# 165A (iii): a measured zero — and only a measured one
# ═════════════════════════════════════════════════════════════════════════════
def test_03_a_listed_market_with_leads_and_no_spend_is_a_published_zero(live_db, monkeypatch):
    """165A case 03. Jordan: leads, no spend row in the window, nothing
    unresolved in it. A known country in a covered window: a measured $0."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(UK, y - timedelta(days=i), 20.0) for i in range(7)])
    _deal_sync(live_db)
    _leads(live_db, [("h1", "United Kingdom", "qualified"),
                     ("h2", "Jordan", "junk"), ("h3", "Jordan", "junk")])
    mart = _mart()
    rows = _by_code(mart)
    assert (rows["JO"]["spend"], rows["JO"].get("spend_reason")) == (0.0, None)
    assert (rows["JO"]["leads"], rows["JO"]["sqls"]) == (2, 0)
    assert mart["country_totals"]["spend"] == 7 * 20.0 * RATE == 175.0
    page = _dash()
    jo = _dash_by_code(page)["JO"]
    assert (jo["spend_native"], jo["spend_usd"], jo.get("spend_reason")) == (0.0, 0.0, None)
    # The control (the same zero in an uncovered window) is case 02.


def test_03_b_a_covered_window_with_no_spend_at_all_is_a_published_zero_total(
        live_db, monkeypatch):
    """165A case 03b: fetched, and Google reported nothing."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed([])
    _deal_sync(live_db)
    _leads(live_db, [("j1", "Jordan", "junk")])
    mart = _mart()
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    assert (mart["country_totals"]["spend"],
            mart["country_totals"]["metrics"]["spend"]["status"]) == (0.0, "published")
    assert _by_code(mart)["JO"]["spend"] == 0.0
    k = _dash()["kpis"]
    assert (k["verified_spend_usd"], k["verified_spend_native"]["amount"]) == (0.0, 0.0)


def test_03_c_a_missing_fx_rate_is_never_converted_at_another_days_rate(
        live_db, monkeypatch):
    """165A case 03c. One day has no GBP→USD rate. No country's USD spend and
    no total is published (the strict gate withholds the window: stricter than
    /api/geo, which still published the UAE, whose days were all rated); what
    a row carries is the native GBP it was stored in, never a conversion at
    another day's rate (docs/58 §7.3: the FX-only gap keeps native spend)."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    hole = y - timedelta(days=2)
    _seed([_spend_rows(UK, y - timedelta(days=i), 10.0) for i in range(7)]
          + [_spend_rows(UAE, y, 30.0)], fx_skip=(hole,))
    _deal_sync(live_db)
    mart = _mart()
    assert mart["spend_truth"]["country_gap_codes"] == ["fx_coverage_incomplete"]
    # The one gap that keeps native spend (F2's rule, on both surfaces).
    from services import google_ads_geo_sync_service as geo_gate
    assert geo_gate.native_country_spend_publishable(["fx_coverage_incomplete"])
    for gaps in (["totals_differ"], ["comparison_not_like_for_like"],
                 ["fx_coverage_incomplete", "geo_coverage_incomplete"], [], None):
        assert not geo_gate.native_country_spend_publishable(gaps), gaps
    rows = _by_code(mart)
    assert (rows["GB"]["spend"], rows["AE"]["spend"]) == (70.0, 30.0)   # GBP, as stored
    assert rows["GB"]["spend"] != 7 * 10.0 * RATE
    assert mart["country_totals"]["spend"] is None
    assert mart["country_totals"]["metrics"]["spend"]["reason"] == "fx_coverage_incomplete"
    page = _dash()
    d = _dash_by_code(page)
    assert (d["GB"]["spend_native"], d["GB"]["spend_usd"]) == (70.0, None)
    assert (d["AE"]["spend_native"], d["AE"]["spend_usd"]) == (30.0, None)
    assert page["kpis"]["verified_spend_usd"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 165A (iv): the legacy monthly snapshot is never a country's spend
# ═════════════════════════════════════════════════════════════════════════════
def test_04_the_legacy_monthly_snapshot_is_never_a_countrys_spend(live_db, monkeypatch):
    """165A case 04. Only the legacy ``geo`` snapshot holds UK spend (£7,777):
    no row, no total, no Dashboard figure carries it. Beside a covered
    canonical window the published figure is the canonical one, to the cent.

    PR-ADS-176C-F1 (F5). With canonical geo holding no rows and its coverage
    incomplete, the mart's country view still read the legacy table
    (PR-ADS-124's ``legacy_geo_table`` fallback): it built the row list from
    it — a "United Kingdom" row whose ``top_campaign`` was the snapshot's
    campaign, on the mart and the Dashboard — and
    ``spend_truth.country_geo_total`` / ``country_spend_variance`` were its
    sum. Now no field of either payload is derived from the snapshot: the geo
    total is unknown (``None``), there are no country spend rows, and the
    only UK row is the one the UK lead creates."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed_legacy_monthly_snapshot(live_db, days_ago=2)
    _deal_sync(live_db)
    _leads(live_db, [("h1", "United Kingdom", "qualified")])
    for window in (WINDOW, "current_quarter", "all_time"):
        mart, page = _mart(window), _dash(window)
        _assert_nothing_published(mart, page)
        for blob in (json.dumps(mart), json.dumps(page)):
            for legacy in ("7777", "9721", "legacy-only-campaign", "4242"):
                assert legacy not in blob, (window, legacy)
        st = mart["spend_truth"]
        assert (st["country_geo_total"], st["country_spend_variance"]) == (None, None), window
        assert _by_code(mart)["GB"]["top_campaign"] is None, window
        assert _dash_by_code(page)["GB"]["top_campaign"] is None, window

    start, today, y = _bounds()
    _seed([_spend_rows(UK, y, 8.0)])
    mart = _mart()
    assert _by_code(mart)["GB"]["spend"] == 10.0
    assert mart["country_totals"]["spend"] == 10.0
    assert mart["spend_truth"]["country_geo_total"] == 8.0
    assert "7777" not in json.dumps(mart) and "7777" not in json.dumps(_dash())


def test_06_the_guard_is_the_coverage_verdict(live_db, monkeypatch):
    """165A case 06. Mutation: tell the shared coverage rule every window is
    complete, and the four days of spend become the window's figure. So cases
    02–04 pass because of the gate, not by accident."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    lo = _seed([_spend_rows(UK, y - timedelta(days=i), 100.0) for i in range(4)])
    _deal_sync(live_db)
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_coverage")
        conn.commit()
    _verify(lo, y - timedelta(days=3))
    _assert_nothing_published(_mart(), _dash(), "geo_coverage_incomplete")

    from services import google_ads_spend_service as spend_service
    real = spend_service.analyze_coverage

    def always_complete(s, e, chunks):
        return {**real(s, e, chunks), "complete": True, "missing_days": 0,
                "missing_ranges": []}
    monkeypatch.setattr(spend_service, "analyze_coverage", always_complete)
    broken = _mart()
    assert broken["spend_truth"]["country_spend_status"] == "verified"
    assert _by_code(broken)["GB"]["spend"] == 4 * 100.0 * RATE == 500.0
    assert _dash()["kpis"]["verified_spend_usd"] == pytest.approx(500.0)


def test_08_yesterday_is_published_only_after_it_was_re_read(live_db, monkeypatch):
    """165A case 08, through the real sync writer. Yesterday 06:05 the daily
    sync fetched the last seven days and marked the chunk verified; yesterday
    held £20 of an eventual £100. Until this morning's sync re-reads it, £20
    is all the table holds for it — and the ledger says verified. (The same
    rule over a backdated ledger is 167A settled-consumers cases 05 / 08.)"""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    first = y - timedelta(days=6)
    lo = start - timedelta(days=30)
    _fx(lo, today)
    google = _GoogleAds()
    for i in range(6):
        google.gbp_by_day[first + timedelta(days=i)] = {UK: 100.0}
    google.gbp_by_day[y] = {UK: 20.0}               # yesterday, as seen during it
    _run_real_sync(monkeypatch, google, first, y)
    _campaign_from_geo(lo, today, geo_ledger=False)
    _verify(lo, first - timedelta(days=1))
    _backdate_ledger(live_db, "(CURRENT_DATE - 1) + interval '6 hours 5 minutes'")
    _deal_sync(live_db)
    morning, page = _mart(), _dash()
    assert "spend_not_reread_since_day_ended" in morning["spend_truth"]["country_gap_codes"]
    _assert_nothing_published(morning, page)
    blob = json.dumps(morning["rows"]) + json.dumps(morning["country_totals"]) \
        + json.dumps(page["countries"]) + json.dumps(page["kpis"])
    assert "775" not in blob and "620" not in blob

    # This morning's sync re-reads yesterday, complete at £100, and the first
    # hours of today (which no country view counts).
    google.gbp_by_day[y] = {UK: 100.0}
    google.gbp_by_day[today] = {UK: 37.0}
    _run_real_sync(monkeypatch, google, today - timedelta(days=6), today)
    _campaign_from_geo(lo, today, geo_ledger=False)
    after = _mart()
    assert after["spend_truth"]["country_spend_status"] == "verified"
    assert _by_code(after)["GB"]["spend"] == 875.0
    assert after["country_totals"]["spend"] == 875.0
    assert _dash()["kpis"]["verified_spend_usd"] == pytest.approx(875.0)


# ═════════════════════════════════════════════════════════════════════════════
# 165A: the join — one row per geography, nothing lost, nothing guessed
# ═════════════════════════════════════════════════════════════════════════════
def test_07_lead_spellings_join_the_market_that_spent_and_nothing_is_lost(
        live_db, monkeypatch):
    """165A cases 07 and 20. ``UK``, ``united kingdom`` and `` United Kingdom ``
    are ONE row; every lead is on exactly one row (a market or the residual);
    a known zero is the number 0, never None."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([r for i in range(7) for r in (
        _spend_rows(UK, y - timedelta(days=i), 100.0),
        _spend_rows(UAE, y - timedelta(days=i), 40.0))]
        + [_spend_rows(JORDAN, start - timedelta(days=20), 12.0)])
    _deal_sync(live_db)
    leads = [("h1", "UK", "qualified"), ("h2", "UK", "junk"),
             ("h3", "united kingdom", "qualified"), ("h4", " United Kingdom ", "in_progress"),
             ("h5", "UAE", "qualified"), ("h6", "uae", "junk"), ("h7", "Jordan", "junk")]
    _leads(live_db, leads)
    mart = _mart()
    rows = _by_code(mart)
    assert set(rows) == {"GB", "AE", "JO"}       # every spelling joined: no residual
    assert (rows["GB"]["leads"], rows["GB"]["sqls"]) == (4, 2)
    assert (rows["AE"]["leads"], rows["AE"]["sqls"]) == (2, 1)
    assert (rows["JO"]["leads"], rows["JO"]["sqls"], rows["JO"]["spend"]) == (1, 0, 0.0)
    assert sum(r["leads"] for r in mart["rows"]) == len(leads)
    assert mart["country_totals"]["leads"] == 7
    assert mart["country_totals"]["spend"] == 7 * 140.0 * RATE == 1225.0
    d = _dash_by_code(_dash())
    assert (d["GB"]["sqls"], d["AE"]["sqls"], d["JO"]["sqls"]) == (2, 1, 0)


def test_07_b_a_text_that_names_no_listed_market_is_never_a_market_or_a_zero(
        live_db, monkeypatch):
    """165A cases 07b, 11 (leads half) and 20. A typo, a non-country, a city,
    an ambiguous name, a bare two-letter token and the no-country placeholders
    reach no market row: they are counted once, in the residual — never
    joined by guess, never given a market's ``$0``."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(UK, y - timedelta(days=i), 100.0) for i in range(7)])
    _deal_sync(live_db)
    leads = [("h1", "UK", "qualified"),
             ("h2", "Untied Kingdom", "junk"), ("h3", "Narnia", "junk"),
             ("h4", "Congo", "junk"), ("h5", "California", "junk"),
             ("h6", "Dubai", "qualified"), ("h7", "IN", "junk"), ("h8", "CA", "junk"),
             ("h9", "", "junk"), ("h10", None, "junk"), ("h11", "Other", "qualified"),
             ("h12", "N/A", "junk"), ("h13", "unknown", "junk"), ("h14", "ZZ", "junk")]
    _leads(live_db, leads)
    mart = _mart()
    rows = _by_code(mart)
    assert set(rows) == {"GB", "residual"}            # no AE (Dubai), IN, CA, CD, CG
    assert (rows["GB"]["leads"], rows["GB"]["sqls"]) == (1, 1)
    assert (rows["residual"]["leads"], rows["residual"]["sqls"]) == (13, 2)
    assert sum(r["leads"] for r in mart["rows"]) == len(leads)
    # The residual's spend is the residual's own (a measured 0 here) — never a
    # figure given to "Narnia" or "Dubai".
    assert rows["residual"]["spend"] == 0.0
    assert rows["residual"]["outside_listed_market_items"] == []
    # The listed markets' totals exclude the residual.
    assert (mart["country_totals"]["leads"], mart["country_totals"]["sqls"]) == (1, 1)


# ═════════════════════════════════════════════════════════════════════════════
# 165A: all_time reads only the certified range
# ═════════════════════════════════════════════════════════════════════════════
def _stray_before_the_certified_range(cluster):
    """UK £10 on each of the last ten days, inside the range BOTH ledgers
    certify; £5,000 on a day twenty days before it, which no chunk certifies.
    Campaign spend equals geo spend day by day, so the two always reconcile —
    with or without the stray day."""
    import db.writers as writers
    start, today, y = _bounds()
    certified_from = y - timedelta(days=9)
    stray = certified_from - timedelta(days=20)
    _fx(stray - timedelta(days=5), today)
    _write_spend([_spend_rows(UK, certified_from + timedelta(days=i), 10.0)
                  for i in range(10)] + [_spend_rows(UK, stray, 5000.0)])
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT spend_date, SUM(cost_micros) FROM google_ads_geo_daily_spend "
                    "GROUP BY spend_date")
        camp = [{"customer_id": CUSTOMER, "currency_code": "GBP", "campaign_id": "111",
                 "campaign_name": "brand - uk", "spend_date": d, "cost_micros": int(m),
                 "source_query_version": "t"} for d, m in cur.fetchall()]
    assert writers.upsert_campaign_daily_spend(camp) == 11
    # Both ledgers certify [certified_from, today] only.
    assert writers.upsert_spend_coverage(CUSTOMER, certified_from, today, "verified",
                                         rows_written=10, cost_micros_total=100_000_000)
    _verify(certified_from, today)
    _deal_sync(cluster)
    return certified_from, stray, today


def test_09_all_time_publishes_only_rows_inside_the_certified_range(live_db, monkeypatch):
    """165A case 09. ``all_time`` has no requested start, so coverage starts at
    the earliest VERIFIED chunk; a row dated 20 days before anything either
    ledger certifies must not be summed into a "covered" figure.

    PR-ADS-176C-F1 (F3): the coverage analyses started at the earliest
    verified chunk but the spend reads had no lower bound — £5,000 on a day no
    chunk certifies was summed on both sides, the two reconciled, and the UK
    row read $6,375 "verified" where the certified figure is $125. Both reads
    are now bounded at each account's earliest verified chunk
    (``revenue_repository._certified_from``)."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    certified_from, stray, today = _stray_before_the_certified_range(live_db)
    mart = _mart("all_time")
    assert mart["window"]["start_date"] is None
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    assert _by_code(mart)["GB"]["spend"] == 100.0 * RATE == 125.0
    assert mart["country_totals"]["spend"] == 125.0
    assert (mart["spend_truth"]["campaign_spend_total"],
            mart["spend_truth"]["country_geo_total"]) == (100.0, 100.0)
    page = _dash("all_time")
    assert page["kpis"]["verified_spend_usd"] == pytest.approx(125.0)
    assert _dash_by_code(page)["GB"]["spend_native"] == pytest.approx(100.0)
    for blob in (json.dumps(mart), json.dumps(page)):
        assert "6375" not in blob and "5100" not in blob
    # A gap INSIDE the certified span still makes all_time not covered.
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_coverage")
        conn.commit()
    _verify(certified_from, certified_from + timedelta(days=3))
    _verify(certified_from + timedelta(days=5), today)
    _assert_nothing_published(_mart("all_time"), _dash("all_time"),
                              "geo_coverage_incomplete")


def test_09_b_every_open_start_spend_reader_counts_only_certified_days(live_db, monkeypatch):
    """PR-ADS-176C-F1 (F3): the bound lives in the three repository reads, so
    it reaches every caller that reads them with an open start (all_time).
    Each surface below is driven over the same evidence: certified £100, plus
    £5,000 before the certified range. No surface sums the £5,000; a window
    WITH a start is not bounded by the ledger (its start bounds it), and the
    campaign-name identity read opts out on purpose."""
    from db import revenue_repository as repo
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    certified_from, stray, today = _stray_before_the_certified_range(live_db)

    # The three reads, account-wide and account-scoped.
    for cid in (None, CUSTOMER):
        camp = repo.fetch_canonical_campaign_spend(None, today, cid)
        assert (camp["total_spend"], str(camp["coverage_start"])) == (
            100.0, certified_from.isoformat()), cid
        assert repo.fetch_geo_daily_spend_total(None, today, cid)["total_spend"] == 100.0
        by = repo.fetch_geo_daily_spend_by_country(None, today, cid)
        assert sum(r["spend"] for r in by["rows"]) == 100.0
    # A window with a start is its own bound: the stray day is read when asked.
    assert repo.fetch_canonical_campaign_spend(stray, today)["total_spend"] == 5100.0
    assert repo.fetch_geo_daily_spend_total(stray, today)["total_spend"] == 5100.0
    # Identity evidence (campaign names for the deal resolver) is unbounded.
    assert repo.fetch_canonical_campaign_spend(
        None, today, certified_only=False)["total_spend"] == 5100.0

    # The surfaces.
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    from services.dashboard_overview_service import build_dashboard_overview
    from services.google_ads_geo_sync_service import build_geo_reconciliation
    from services.revenue_attribution_service import build_revenue_attribution
    from services.revenue_decision_mart import build_revenue_decision_mart
    attribution = build_revenue_attribution("all_time")
    assert attribution["source_health"]["spend_native_total"] == 100.0
    campaign_view = build_revenue_decision_mart(view="campaign", window="all_time")
    assert campaign_view["spend_truth"]["native_spend"] == 100.0
    assert campaign_view["summary"]["spend_usd"] == pytest.approx(125.0)
    overview = json.loads(json.dumps(build_dashboard_overview("all_time"), default=str))
    assert overview["kpis"]["google_ads_spend_usd"] == pytest.approx(125.0)
    campaigns = json.loads(json.dumps(build_dashboard_campaigns("all_time"), default=str))
    assert campaigns["kpis"]["verified_spend_usd"] == pytest.approx(125.0)
    panel = build_geo_reconciliation("all_time")
    assert (panel["canonical_campaign_total"], panel["geo_total"]) == (
        pytest.approx(100.0), pytest.approx(100.0))
    # The panel's per-day / per-campaign breakdown reads the same bound (the
    # fixture's geo rows sit under campaign "c1", its campaign rows under
    # "111": only the certified £100 shows as a per-campaign gap).
    assert panel["campaign_spend_without_geo_native"] == pytest.approx(100.0)
    for blob in (json.dumps(attribution, default=str), json.dumps(campaign_view, default=str),
                 json.dumps(overview), json.dumps(campaigns), json.dumps(panel, default=str)):
        assert "6375" not in blob and "5100" not in blob
    # The evidence-window and audit surfaces that read campaign spend.
    from services.campaign_evidence_service import build_campaign_evidence
    from services.campaign_identity_service import build_mapping_review
    from services.google_ads_spend_service import (
        build_google_ads_spend_audit, is_window_spend_verified)
    from services.revenue_spend_truth_service import build_google_ads_spend_truth_for_days
    evidence = json.loads(json.dumps(build_campaign_evidence("all_time"), default=str))
    assert (evidence["summary"]["spend_native"], evidence["summary"]["spend_usd"]) == (
        100.0, 125.0)
    audit = build_google_ads_spend_audit("all_time")
    assert (audit["native_total"], audit["campaign_daily_total"]) == (100.0, 100.0)
    assert is_window_spend_verified("all_time")["local_total_spend"] == 100.0
    mapping = build_mapping_review("all_time")
    assert [o["native_spend"] for o in mapping["campaign_options"]] == [100.0]
    days = build_google_ads_spend_truth_for_days(None, today - timedelta(days=1))
    assert (days["native_spend"], days["usd_spend"]) == (100.0, 125.0)
    for blob in (json.dumps(evidence), json.dumps(audit, default=str),
                 json.dumps(mapping, default=str), json.dumps(days, default=str)):
        assert "6375" not in blob and "5100" not in blob
    breakdown = repo.fetch_geo_reconciliation_breakdown(None, today)
    assert sum(d["campaign_spend"] for d in breakdown["daily"]) == 100.0
    assert sum(d["geo_spend"] for d in breakdown["daily"]) == 100.0
    series = repo.fetch_spend_daily_series(None, today)
    assert min(str(r["spend_date"]) for r in series["rows"]) == certified_from.isoformat()


# ═════════════════════════════════════════════════════════════════════════════
# 165A: production-shaped — 162 spend codes, lower-case lead countries
# ═════════════════════════════════════════════════════════════════════════════
def test_10_every_production_spend_code_keeps_its_spend_and_leads_join_their_market(
        live_db, monkeypatch):
    """165A case 10, on the 96 listed markets + the residual. Every production
    spend code keeps exactly its spend: a listed market is its own row, any
    other code is an itemised country of the residual (never "(unresolved
    location)"); the code-less row is the residual's ``country_unidentified``.
    Pakistan (leads, never a geo row) is unavailable while that code-less spend
    is in the window. Totals are conserved.

    PR-ADS-176C-F1 (F1): a lead text that names a listed market by an ISO or
    legacy name the market alias table does not carry — production's
    "libyan arab jamahiriya" (Libya) — was counted in the residual, so
    Libya's row understated its leads. ``country_identity.resolve`` now falls
    back to the exact complete-ISO lookup (``resolve_iso_country_text``) for a
    listed market, as /api/geo did. Every production spelling joins its
    market; conservation holds."""
    from analysis import country_identity as ci
    from tests.test_pr_ads_165a_country_identity_complete import (
        PRODUCTION_LEAD_COUNTRIES, PRODUCTION_SPEND_CODES)
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    spend = [_spend_rows((f"2{i:03d}", code, ci.iso_country_label(code)), y, float(i + 1))
             for i, code in enumerate(PRODUCTION_SPEND_CODES)]
    spend.append(_spend_rows(NO_CODE, y, 7.0))
    _seed(spend)
    _deal_sync(live_db)
    _leads(live_db, [(f"p{i}", text, "qualified" if i % 2 else "junk")
                     for i, text in enumerate(sorted(PRODUCTION_LEAD_COUNTRIES))])

    mart = _mart()
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    rows, items = _by_code(mart), _items(mart)
    listed = [c for c in PRODUCTION_SPEND_CODES if ci.resolve(code=c).is_country]
    assert len(listed) == 89 and len(PRODUCTION_SPEND_CODES) - len(listed) == 73
    for i, code in enumerate(PRODUCTION_SPEND_CODES):
        expect = round((i + 1) * RATE, 2)
        if code in listed:
            assert rows[code]["spend"] == expect, code
        else:
            assert items[code]["spend"] == pytest.approx(expect), code
            assert items[code]["leads"] == "in_residual", code
    comp = rows["residual"]["spend_components"]
    assert comp["country_unidentified"] == 8.75
    assert comp["outside_listed_markets"] == pytest.approx(
        sum(i["spend"] for i in items.values()))
    total = round((sum(range(1, 163)) + 7.0) * RATE, 2)
    assert total == 16512.5
    assert sum(r["spend"] or 0 for r in mart["rows"]) == pytest.approx(total)
    # Pakistan: unavailable, and says why — never $0.
    assert (rows["PK"]["spend"], rows["PK"]["spend_reason"]) == (
        None, "spend_location_unresolved")
    # Leads: EVERY production spelling of a listed market joins it — the
    # resolver is asked by text, and must name the code the spelling is.
    for text, code in PRODUCTION_LEAD_COUNTRIES.items():
        if ci.resolve(code=code).is_country:
            assert ci.resolve(name=text).code == code, text
            assert rows[code]["leads"] == 1, text
    outside = sorted(t for t, c in PRODUCTION_LEAD_COUNTRIES.items()
                     if not ci.resolve(code=c).is_country)
    assert outside == ["benin", "cameroon", "kazakhstan", "mozambique", "nepal",
                       "somalia", "uzbekistan"]
    # F1: Libya is a listed market and spent; its lead is on its row.
    assert ci.resolve(code="LY").is_country and rows["LY"]["spend"] > 0
    assert rows["LY"]["leads"] == 1
    assert rows["residual"]["leads"] == len(outside)
    assert sum(r["leads"] for r in mart["rows"]) == 40

    page = _dash()
    d = _dash_by_code(page)
    assert (d["PK"]["spend_native"], d["PK"]["spend_reason"]) == (
        None, "spend_location_unresolved")
    assert page["kpis"]["zero_withheld_countries"] == ["Pakistan"]
    for i, code in enumerate(PRODUCTION_SPEND_CODES):
        if code in listed:
            assert d[code]["spend_native"] == float(i + 1), code


def test_11_named_spend_is_never_relabelled_and_no_text_is_joined_by_guess(
        live_db, monkeypatch):
    """165A case 11. Kazakhstan and Georgia are not listed markets: their spend
    is theirs, itemised in the residual — not merged with the spend Google
    gave no country for. Lead text joins only a market it names exactly.

    F1: "Türkiye" and "Jordan." name listed markets the alias table does not
    spell; the exact ISO lookup joins them to Turkey and Jordan. "Congo",
    "Dubai", "IN", "CA" and "North Korea" (not a listed market) still name no
    market."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(UK, y, 100.0), _spend_rows(KZ, y, 50.0), _spend_rows(GE, y, 30.0),
           _spend_rows(NO_CODE, y, 7.0)])
    _deal_sync(live_db)
    leads = [("a1", "Kazakhstan", "qualified"), ("a2", "kazakhstan", "junk"),
             ("a3", "Georgia", "junk"), ("a4", "Congo", "junk"), ("a5", "California", "junk"),
             ("a6", "Dubai", "qualified"), ("a7", "IN", "junk"), ("a8", "CA", "junk"),
             ("a9", "Other", "junk"), ("a10", "N/A", "junk"), ("a11", None, "junk"),
             ("a12", "US", "junk"), ("a13", "Türkiye", "junk"),
             ("a14", "North Korea", "junk"), ("a15", "England", "in_progress"),
             ("a16", "Jordan.", "junk")]
    _leads(live_db, leads)
    mart = _mart()
    rows, items = _by_code(mart), _items(mart)
    assert (items["KZ"]["spend"], items["GE"]["spend"]) == (62.5, 37.5)
    assert rows["residual"]["spend_components"]["country_unidentified"] == 8.75
    assert rows["GB"]["spend"] == 125.0
    for code in ("CG", "CD", "IN", "CA", "AE", "KP"):
        assert code not in rows, code
    assert (rows["GB"]["leads"], rows["US"]["leads"]) == (1, 1)   # England, US
    # F1: Türkiye and "Jordan." are on Turkey and Jordan.
    assert (rows["TR"]["leads"], rows["JO"]["leads"]) == (1, 1)
    assert rows["residual"]["leads"] == len(leads) - 4    # England, US, Türkiye, Jordan.
    assert sum(r["leads"] for r in mart["rows"]) == len(leads)
    assert sum(r["spend"] or 0 for r in mart["rows"]) == pytest.approx(
        (100 + 50 + 30 + 7) * RATE)


def test_11_b_a_code_outside_every_table_is_keyed_by_googles_code(live_db, monkeypatch):
    """165A cases 11b and 21. Google reports Kosovo as ``XK``, which ISO never
    assigned: its spend is Kosovo's, itemised in the residual, and its leads
    are in the residual with it (Kosovo is not a listed market). Two criteria
    carrying one code are one row, summed."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(KOSOVO, y, 40.0), _spend_rows(UK, y, 10.0),
           _spend_rows(("20339", "gb", "United Kingdom"), y, 6.0)])
    _deal_sync(live_db)
    _leads(live_db, [("x1", "Kosovo", "qualified"), ("x2", "kosovo", "junk")])
    mart = _mart()
    rows, items = _by_code(mart), _items(mart)
    assert (items["XK"]["country"], items["XK"]["spend"]) == ("Kosovo", 50.0)
    assert items["XK"]["provenance"] == "google_ads"
    assert rows["GB"]["spend"] == 20.0
    assert (rows["residual"]["leads"], rows["residual"]["sqls"]) == (2, 1)
    assert [r for r in mart["rows"] if r.get("country_code") == "XK"] == []


# ═════════════════════════════════════════════════════════════════════════════
# 165A: unreadable leads are unknown, never "no leads"
# ═════════════════════════════════════════════════════════════════════════════
def test_13_unreadable_leads_are_unknown_and_spend_is_unaffected(live_db, monkeypatch):
    """165A cases 13 and 18."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(UK, y, 100.0)])
    _deal_sync(live_db)
    # Readable, and none: that IS "no leads" — the number 0.
    rows = _by_code(_mart())
    assert (rows["GB"]["leads"], rows["GB"]["sqls"], rows["GB"]["spend"]) == (0, 0, 125.0)

    _leads(live_db, [("c1", "UK", "qualified")])
    from db import revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: {"available": False, "rows": [],
                                      "event_date_safe": False})
    mart = _mart()
    rows = _by_code(mart)
    assert (rows["GB"]["leads"], rows["GB"]["sqls"]) == (None, None)
    assert rows["GB"]["spend"] == 125.0                 # spend is unaffected
    assert (mart["country_totals"]["leads"], mart["country_totals"]["sqls"]) == (None, None)
    page = _dash()
    assert _dash_by_code(page)["GB"]["sqls"] is None
    assert page["kpis"]["sqls"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 165A: spend with no country, and the sync's failed lookup
# ═════════════════════════════════════════════════════════════════════════════
def test_14_a_failed_country_lookup_does_not_turn_a_countrys_spend_into_zero(
        live_db, monkeypatch):
    """165A case 14, through the real sync. The UAE's code is on record from
    older rows; this morning's lookup failed. The UAE keeps its spend."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    first = y - timedelta(days=6)
    lo = start - timedelta(days=30)
    _fx(lo, today)
    google = _GoogleAds()
    for i in range(7):
        google.gbp_by_day[first + timedelta(days=i)] = {UK: 100.0}
    google.gbp_by_day[y - timedelta(days=2)][UAE] = 50.0
    google.gbp_by_day[first - timedelta(days=2)] = {UAE: 8.0}     # older history
    _run_real_sync(monkeypatch, google, first - timedelta(days=2), y)
    _backdate_ledger(live_db, "(CURRENT_DATE - 1) + interval '6 hours 5 minutes'")
    google.gbp_by_day[today] = {UK: 5.0}
    result = _failed_lookup_sync(monkeypatch, google, today)
    assert result["status"] == "success", result
    # Campaign spend that reconciles; the geo ledger is the real sync's, plus
    # the earlier months (settled long ago).
    _campaign_from_geo(lo, today, geo_ledger=False)
    _verify(lo, first - timedelta(days=3))
    _deal_sync(live_db)
    mart = _mart()
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    rows = _by_code(mart)
    assert rows["AE"]["spend"] == (50.0 + 8.0) * RATE == 72.5
    assert rows["GB"]["spend"] == 875.0
    assert "residual" not in rows                 # no spend without a country
    assert _dash_by_code(_dash())["AE"]["spend_native"] == 58.0


def test_14_b_a_location_no_stored_row_names_leaves_the_country_unavailable(
        live_db, monkeypatch):
    """165A case 14b. The UAE spends for the FIRST time inside the range of the
    morning whose lookup fails: nothing on record names criterion 2784, so its
    spend is unresolved — and the UAE, never carried, is not $0."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    first = y - timedelta(days=6)
    lo = start - timedelta(days=30)
    _fx(lo, today)
    _leads(live_db, [("h1", "UAE", "qualified")])
    google = _GoogleAds()
    for i in range(7):
        google.gbp_by_day[first + timedelta(days=i)] = {UK: 100.0}
    _run_real_sync(monkeypatch, google, first, y)
    _backdate_ledger(live_db, "(CURRENT_DATE - 1) + interval '6 hours 5 minutes'")
    google.gbp_by_day[y - timedelta(days=2)][UAE] = 50.0
    google.gbp_by_day[today] = {UK: 5.0}
    result = _failed_lookup_sync(monkeypatch, google, today)
    assert (result["status"], result["reason"]) == ("partial", "country_lookup_failed")
    _campaign_from_geo(lo, today, geo_ledger=False)
    _verify(lo, first - timedelta(days=1))
    _deal_sync(live_db)
    mart = _mart()
    rows = _by_code(mart)
    assert (rows["AE"]["spend"], rows["AE"]["spend_reason"]) == (
        None, "spend_location_unresolved")
    assert rows["AE"]["leads"] == 1
    assert rows["residual"]["spend_components"]["country_unidentified"] == 62.5
    assert sum(r["spend"] or 0 for r in mart["rows"]) == pytest.approx(
        (700.0 + 50.0) * RATE)
    ae = _dash_by_code(_dash())["AE"]
    assert (ae["spend_native"], ae["spend_reason"]) == (None, "spend_location_unresolved")


def test_16_b_unreadable_geo_rows_are_unavailable_not_a_guess(live_db, monkeypatch):
    """165A case 16b. The identity of each criterion is resolved in the same
    statement as the spend rows (``fetch_geo_daily_spend_by_country``), so
    "the identity read failed" is "the rows are unreadable": unknown, never
    rows named from their own (possibly blank) codes. The snapshot itself
    failing is 176A case 10."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(UK, y, 100.0)])
    _deal_sync(live_db)
    _leads(live_db, [("u1", "UK", "qualified")])
    from db import revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country",
                        lambda *a, **k: {"available": False, "has_rows": False, "rows": []})
    mart, page = _mart(), _dash()
    assert mart["spend_truth"]["country_gap_codes"] == ["geo_rows_unreadable"]
    _assert_nothing_published(mart, page)


def test_17_spend_under_a_dollar_never_reads_as_zero(live_db, monkeypatch):
    """165A case 17, the payload half (the "<$1" display rule is 176B case 11).
    39p, 79p and 80p are not zeros; the one measured zero is Pakistan's."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(FIJI, y, 0.39), _spend_rows(UK, y, 0.79), _spend_rows(UAE, y, 0.80)])
    _deal_sync(live_db)
    _leads(live_db, [("f1", "Pakistan", "junk")])
    mart = _mart()
    rows, items = _by_code(mart), _items(mart)
    assert (rows["GB"]["spend"], rows["AE"]["spend"]) == (0.99, 1.0)
    assert items["FJ"]["spend"] == 0.49
    assert rows["residual"]["spend"] == 0.49
    assert (rows["PK"]["spend"], rows["PK"].get("spend_reason")) == (0.0, None)
    assert [c for c, r in rows.items() if r["spend"] == 0] == ["PK"]
    assert mart["country_totals"]["spend"] == 1.99
    # A window whose whole market spend is under a dollar: the total is not 0.
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_daily_spend WHERE country_code <> 'GB'")
        cur.execute("UPDATE google_ads_campaign_daily_spend SET cost_micros = 790000")
        conn.commit()
    assert _mart()["country_totals"]["spend"] == 0.99
    assert _dash()["kpis"]["verified_spend_usd"] == 0.99


def test_19_a_failed_re_read_is_not_described_as_waiting_for_the_morning_sync(
        live_db, monkeypatch):
    """165A case 19. Yesterday was fetched during yesterday; this morning's
    sync ran and FAILED. The gap is ``spend_reread_failed``, not "the morning
    sync re-reads it"."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    first = y - timedelta(days=6)
    lo = start - timedelta(days=30)
    _fx(lo, today)
    google = _GoogleAds()
    for i in range(7):
        google.gbp_by_day[first + timedelta(days=i)] = {UK: 100.0}
    _run_real_sync(monkeypatch, google, first, y)
    _campaign_from_geo(lo, today, geo_ledger=False)
    _verify(lo, first - timedelta(days=1))
    _backdate_ledger(live_db, "(CURRENT_DATE - 1) + interval '6 hours 5 minutes'")
    from services import google_ads_geo_sync_service as geo

    def down(a, b):
        raise RuntimeError("Google Ads API 503")
    monkeypatch.setattr(geo, "fetch_geo_daily", down)
    result = geo.run_google_ads_geo_sync(
        date_from=(today - timedelta(days=6)).isoformat(), date_to=today.isoformat(),
        dry_run=False, job_id="sync-today")
    assert result["status"] != "success"
    _deal_sync(live_db)
    mart, page = _mart(), _dash()
    assert "spend_reread_failed" in mart["spend_truth"]["country_gap_codes"]
    assert "spend_not_reread_since_day_ended" not in mart["spend_truth"]["country_gap_codes"]
    _assert_nothing_published(mart, page)


def test_22_the_account_time_zone_is_read_where_the_sync_reads_it(live_db, monkeypatch):
    """165A case 22. Not a constant: with no zone on record — or one that is
    not a zone — "the day ended" has no meaning, and nothing is published."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    start, today, y = _bounds()
    _seed([_spend_rows(UK, y, 100.0)])
    _deal_sync(live_db)
    assert _mart()["spend_truth"]["country_spend_status"] == "verified"
    for zone in (None, "Not/AZone"):
        _seed_account_zone(live_db, zone)
        _assert_nothing_published(_mart(), _dash(), "account_time_zone_unknown")


def test_23_this_module_is_in_ci_s_postgresql_step_and_its_did_run_list():
    text = WORKFLOW.read_text(encoding="utf-8")
    name = Path(__file__).stem
    assert f"tests/{name}.py \\" in text
    assert f'"tests.{name}",' in text


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
