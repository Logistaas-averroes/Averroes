"""PR-ADS-165A — the Google Ads → Countries page publishes spend only for a
window that was actually synced. Real PostgreSQL and the real route handlers.

PR-ADS-176B deleted that page (``#/geo``) and its ``loadGeo``; ``/api/geo``
stays until PR-ADS-176C. Every case keeps its payload assertions, and the
cases whose page assertions carried a payload fact now assert it on the rows.
The page-only properties that still apply — spend Unavailable with its reason,
never a number under a failed gate, "<$1" below a dollar, the owner note —
are guarded on the one Countries page in ``tests/test_pr_ads_176b_*``. The
lead-quality columns and KPIs (junk, in progress, top campaign / keyword) were
dropped by owner decision and are not ported.

The defect, as the first version of this slice would have shipped it. The page
(`#/geo`) read the legacy ``geo`` snapshot through ``/api/geo``:
``SUM(spend_usd) WHERE run_date >= NOW() - N days``. The weekly job rewrote
that table every Monday. With the weekly job retired the monthly job is its
only writer, so:

* twelve days into a month the 7-day window holds no row. ``/api/geo`` returns
  nothing, the leads endpoint still returns countries, and ``loadGeo`` filled
  the gap with ``spend_usd: perf ? … : 0`` — every country "Spend $0", "Total
  Geo Spend $0", under a strip reading "Fresh with data";
* twenty days in, the 30-day window holds the days up to the 1st and their sum
  was printed as the 30-day figure.

Unknown is not zero, and a partial is not the thing itself. The page now reads
the canonical DAILY country table the incremental sync writes
(``google_ads_geo_daily_spend``) and the coverage ledger beside it
(``google_ads_geo_coverage``), judged by the same ``analyze_geo_coverage`` the
Country Revenue & Spend page uses.

Every case here seeds the real tables through the real writers, calls the real
``api_geo`` handler (which reads the lead summary through the real
``api_leads_country_summary``), and renders its response with the real
``loadGeo`` from ``static/app.js``.

Second review. Three more defects in the first rewrite, each with cases here:

* **The join.** The page joined spend to leads in the browser, on the trimmed
  country TEXT. Leads spelled ``UK`` or ``united kingdom`` beside spend for
  "United Kingdom" became three rows, two of them reading ``$0`` in a covered
  window — a zero for a country that had spend. The server now joins on
  ``analysis.country_identity`` and the page prints the rows it is served
  (tests 07, 07b).
* **The window end.** It ended today, which is covered only after the morning
  sync and then holds a few hours of spend. It ends on the last complete
  account day (test 08).
* **all_time.** Coverage started at the earliest verified chunk but the row
  read had no lower bound (test 09).

Third review. Two more, both in what the second rewrite added:

* **"The last complete day" could be a partial day.** The daily sync marks its
  chunk `verified` through TODAY at about 06:00 UTC, and the gate looked at
  chunk status only. So from account-midnight until this morning's sync,
  yesterday read "covered" while holding the six hours yesterday morning's
  sync saw: `$775` printed where the truth was `$875`. A day now counts only
  if a verified chunk containing it was fetched after that day ended (tests
  08, 08b — driven through the real sync writer).
* **Named spend was relabelled "(unresolved location)".** Spend and leads were
  resolved through the supported-MARKET registry (96 codes). Production spend
  falls in 162 ISO codes and its top lead countries are stored lower-case
  ("nigeria", "libyan arab jamahiriya"). Spend is now keyed by Google's own
  ISO code, always, and lead text resolves against the complete ISO table
  (tests 10–12).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


APP_JS = ROOT / "static" / "app.js"
USER = {"username": "tester", "role": "admin"}
CUSTOMER = "1234567890"
OTHER_CUSTOMER = "9999999999"
RATE = 1.25                      # GBP → USD, the same every day in these tests

UK = ("2826", "GB", "United Kingdom")
UAE = ("2784", "AE", "United Arab Emirates")

#: Figures that exist ONLY in the legacy monthly snapshot. None may reach the
#: API payload or the page.
LEGACY_UK_SPEND = 7777.0
LEGACY_CLICKS = 4242


# ── database ────────────────────────────────────────────────────────────────
@pytest.fixture()
def live_db(monkeypatch):
    from tests import test_pr_ads_153b_pg_integration as pgmod
    if not pgmod._have_postgres():
        pytest.skip("PostgreSQL not available")
    import psycopg2.sql  # noqa: F401  (re-registers after sys.modules swaps)
    cluster = pgmod._PgCluster().start()
    import db.connection as connection
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)
        monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
        monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()
        from db.schema import init_db
        init_db()
        _seed_account_zone(cluster, ACCOUNT_ZONE)
        yield cluster
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None
        cluster.stop()


ACCOUNT_ZONE = "Europe/London"


def _seed_account_zone(cluster, zone):
    """The account's time zone, where production holds it: on the account
    daily spend rows Google reported it with. The page reads it from there —
    the same place the geo sync does — and not from a constant."""
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_account_daily_spend")
        if zone is not None:
            cur.execute(
                "INSERT INTO google_ads_account_daily_spend (customer_id, "
                "spend_date, cost_micros, currency_code, account_time_zone) "
                "VALUES (%s, CURRENT_DATE - 1, 0, 'GBP', %s)", (CUSTOMER, zone))
        conn.commit()


def _today() -> date:
    from analysis.account_time import account_today
    return account_today()


def _window(days: int) -> tuple[date, date]:
    """The page's spend window: N days ending on the last COMPLETE day."""
    end = _today() - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def _spend_rows(country, day, gbp, *, campaign="c1", customer=CUSTOMER):
    criterion, code, name = country
    return {"customer_id": customer, "currency_code": "GBP",
            "country_criterion_id": criterion, "country_code": code,
            "country_name": name, "campaign_id": campaign,
            "spend_date": day, "cost_micros": int(round(gbp * 1_000_000)),
            "source_query_version": "test"}


def _write_spend(rows, run="sync-1"):
    import db.writers as writers
    assert writers.upsert_geo_daily_spend(rows, sync_run_id=run) == len(rows)


def _write_fx(start: date, end: date, *, skip=()):
    import db.writers as writers
    rows = []
    d = start
    while d <= end:
        if d not in skip:
            rows.append({"rate_date": d, "base_currency": "GBP",
                         "quote_currency": "USD", "rate": RATE,
                         "provider": "test", "source_version": "test"})
        d += timedelta(days=1)
    assert writers.upsert_fx_rates(rows) == len(rows)


def _verify(start: date, end: date, *, status="verified", customer=CUSTOMER):
    """Record a coverage chunk the way the geo sync does, after its rows."""
    import db.writers as writers
    assert writers.upsert_geo_coverage(customer, start, end, status,
                                       sync_run_id="sync-1") is True


def _seed_leads(cluster, rows):
    """`rows`: [(contact_id, country, status_category)], all dated today."""
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO runs (run_type, started_at, status) VALUES "
                    "('daily', now(), 'success') RETURNING id")
        run_id = cur.fetchone()[0]
        for contact_id, country, category in rows:
            cur.execute(
                "INSERT INTO leads (run_id, run_date, contact_id, country, "
                "status_category, campaign_name, keyword, source_type) "
                "VALUES (%s, CURRENT_DATE, %s, %s, %s, 'brand - uk', 'tms', "
                "'paid_search')", (run_id, contact_id, country, category))
        conn.commit()


def _seed_legacy_monthly_snapshot(cluster, *, days_ago: int = 2):
    """What the monthly job leaves in the legacy `geo` table: per-day rows,
    inside the window, with figures the canonical table does not hold."""
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO runs (run_type, started_at, status) VALUES "
                    "('monthly', now(), 'success') RETURNING id")
        run_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO geo (run_id, run_date, country, campaign_name, "
            "spend_usd, clicks, impressions, conversions) VALUES "
            "(%s, CURRENT_DATE - %s, 'United Kingdom', 'legacy-only-campaign', %s, %s, "
            "90000, 12)", (run_id, days_ago, LEGACY_UK_SPEND, LEGACY_CLICKS))
        conn.commit()


# ── the real endpoints, and the real page ───────────────────────────────────
def _api(window: str) -> dict:
    """The one endpoint the page fetches, through its real handler, and
    through JSON — what the browser receives."""
    import api.server as server
    geo = server.api_geo(user=USER, days=30, window=window)
    # `leads` is NOT read by the page any more (the server joins it into
    # `geo`). It is carried only so that a version of the page which still
    # fetched it separately renders what it really would have, when these
    # cases are run against that version as pre-fix evidence.
    leads = server.api_leads_country_summary(user=USER, days=30, window=window)
    return json.loads(json.dumps({"geo": geo, "leads": leads}, default=str))


# PR-ADS-176B: the node probe that rendered these payloads with the real
# ``loadGeo`` (``_PROBE`` / ``_render``) and its table readers went with the
# #/geo page. ``/api/geo`` stays until PR-ADS-176C and is checked below.


def _assert_no_spend_figure_in_the_payload(api: dict):
    """The unavailable state of ``/api/geo``: no ``$0``, no partial sum.

    PR-ADS-176B: this was ``_assert_no_spend_figure_anywhere(api, page)`` and
    checked the deleted ``#/geo`` page first (KPI cards, notice, table, map).
    The page is gone; the endpoint stays until PR-ADS-176C and is checked
    here unchanged. The one Countries page's unavailable state — the strict
    gate's card, never a number — is guarded in
    ``tests/test_pr_ads_176b_countries_one_page.py`` (case 06)."""
    geo = api["geo"]
    # Rows still list the countries that have leads; none carries a spend.
    for row in geo["rows"]:
        assert row["spend_usd"] is None, row
        assert row["spend_status"] == "unavailable", row
    assert geo["countries_active"] is None
    assert geo["total_spend_usd"] is None
    assert geo["spend_status"] == "unavailable"
    assert geo["coverage"]["status"] != "covered"


# ═════════════════════════════════════════════════════════════════════════════
# (i) a covered window publishes the exact per-country sums, once
# ═════════════════════════════════════════════════════════════════════════════
def test_01_a_covered_window_publishes_exact_per_country_spend_with_no_double_count(
        live_db, tmp_path):
    start, end = _window(7)
    days = [start + timedelta(days=i) for i in range(7)]
    _write_fx(start - timedelta(days=3), end)

    # First sync: UK £100 and UAE £40 a day, two campaigns in the UK.
    first = []
    for d in days:
        first += [_spend_rows(UK, d, 60.0, campaign="c1"),
                  _spend_rows(UK, d, 40.0, campaign="c2"),
                  _spend_rows(UAE, d, 40.0, campaign="c1")]
    _write_spend(first, run="sync-1")
    # The daily sync's lookback re-fetches the last days every morning, and
    # Google restates recent spend. Written with the writer production uses
    # for that refresh — `replace_geo_daily_spend_chunk`, which REPLACES the
    # range — so the overlapped days hold the restated figures once.
    restated = [_spend_rows(UK, d, 70.0, campaign="c1") for d in days[-3:]]
    restated += [_spend_rows(UK, d, 40.0, campaign="c2") for d in days[-3:]]
    restated += [_spend_rows(UAE, d, 40.0, campaign="c1") for d in days[-3:]]
    import db.writers as writers
    replaced = writers.replace_geo_daily_spend_chunk(
        CUSTOMER, days[-3], days[-1], restated, sync_run_id="sync-2")
    assert (replaced["replaced"], replaced["deleted"], replaced["written"]) == (
        True, 9, 9)
    # A day outside the window, and another account's spend, are not counted.
    _write_spend([_spend_rows(UK, start - timedelta(days=1), 5000.0),
                  _spend_rows(UK, end, 9000.0, customer=OTHER_CUSTOMER)])
    # Today — outside the window — has spend too, and is not counted.
    _write_spend([_spend_rows(UK, end + timedelta(days=1), 3000.0)])
    _verify(start - timedelta(days=1), end + timedelta(days=1))
    _seed_leads(live_db, [("h1", "United Kingdom", "qualified"),
                          ("h2", "United Kingdom", "junk")])

    api = _api("7d")
    geo = api["geo"]
    assert round(sum(r["spend_usd"] or 0 for r in geo["rows"]), 2) == 1262.5
    assert geo["coverage"] == {
        "status": "covered", "reason": None,
        "requested_from": start.isoformat(), "requested_through": end.isoformat(),
        "covered_from": start.isoformat(), "covered_through": end.isoformat(),
        "missing_days": 0, "missing_ranges": [], "failed_chunks": []}
    assert geo["spend_status"] == "published"
    assert geo["dataset"] == "google_ads_api/canonical_geo"
    # UK: 4 days × £100 + 3 days × £110 = £730 → $912.50. UAE: 7 × £40 = £280
    # → $350. Doubling the overlapped days would give $1,287.50 / $500.
    uk_usd = (4 * 100.0 + 3 * 110.0) * RATE
    uae_usd = 7 * 40.0 * RATE
    assert {r["country"]: r["spend_usd"] for r in geo["rows"]} == {
        "United Kingdom": uk_usd, "United Arab Emirates": uae_usd}
    assert (uk_usd, uae_usd) == (912.5, 350.0)
    assert geo["total_spend_usd"] == uk_usd + uae_usd == 1262.5
    assert all(r["spend_status"] == "published" for r in geo["rows"])
    # The metrics the canonical table does not hold are named, not filled in.
    assert geo["dropped_metrics"] == ["clicks", "impressions", "conversions",
                                      "campaign_name"]
    for row in geo["rows"]:
        assert not {"clicks", "impressions", "conversions"} & set(row)
    assert geo["window_range"] == {"from": start.isoformat(),
                                   "through": end.isoformat(),
                                   "ends_on": "yesterday_account_local"}



# ═════════════════════════════════════════════════════════════════════════════
# (ii) a window that is not fully covered publishes no spend at all
# ═════════════════════════════════════════════════════════════════════════════
def _sync_is_behind(start, end):
    """The daily sync last ran three days ago."""
    last = end - timedelta(days=3)
    _verify(start - timedelta(days=30), last)
    return {"reason": "geo_coverage_incomplete",
            "covered_from": start, "covered_through": last, "missing": 3}


def _one_day_was_never_fetched(start, end):
    """A gap in the middle: the day's chunk is simply absent."""
    hole = start + timedelta(days=3)
    _verify(start, hole - timedelta(days=1))
    _verify(hole + timedelta(days=1), end)
    return {"reason": "geo_coverage_incomplete",
            "covered_from": start, "covered_through": end, "missing": 1}


def _a_chunk_failed(start, end):
    """Yesterday and today were attempted and failed; nothing re-fetched them."""
    _verify(start, end - timedelta(days=2))
    _verify(end - timedelta(days=1), end, status="failed")
    return {"reason": "geo_coverage_incomplete",
            "covered_from": start, "covered_through": end - timedelta(days=2),
            "missing": 2}


@pytest.mark.parametrize("gap", [_sync_is_behind, _one_day_was_never_fetched,
                                 _a_chunk_failed],
                         ids=["sync behind", "gap in the middle", "failed chunk"])
def test_02_an_uncovered_window_says_so_and_shows_no_zero_and_no_partial_sum(
        gap, live_db, tmp_path):
    start, end = _window(7)
    _write_fx(start - timedelta(days=3), end)
    # Spend rows ARE present for the days that were synced — £100 a day in the
    # UK. Their sum ($500 for four days) is exactly the partial that must not
    # be printed as the 7-day figure.
    _write_spend([_spend_rows(UK, start + timedelta(days=i), 100.0)
                  for i in range(4)])
    expect = gap(start, end)
    _seed_leads(live_db, [("h1", "United Kingdom", "qualified"),
                          ("h2", "Jordan", "junk")])

    api = _api("7d")
    _assert_no_spend_figure_in_the_payload(api)

    cov = api["geo"]["coverage"]
    assert cov["status"] == "not_covered"
    assert cov["reason"] == expect["reason"]
    assert cov["requested_from"] == start.isoformat()
    assert cov["requested_through"] == end.isoformat()
    assert cov["covered_from"] == expect["covered_from"].isoformat()
    assert cov["covered_through"] == expect["covered_through"].isoformat()
    assert cov["missing_days"] == expect["missing"]
    assert cov["missing_ranges"], "the missing days are named"
    # The reader is told which days ARE synced, in words, and why that is not
    # enough — and is not told to run anything.
    def words(d):
        return f"{d.day} {d.strftime('%b %Y')}"


def test_02b_a_window_with_no_synced_day_and_an_unreadable_ledger_are_unavailable(
        live_db, tmp_path, monkeypatch):
    start, end = _window(14)
    _seed_leads(live_db, [("h1", "Jordan", "qualified")])

    # Nothing synced at all for this account.
    api = _api("14d")
    _assert_no_spend_figure_in_the_payload(api)
    cov = api["geo"]["coverage"]
    assert (cov["status"], cov["covered_from"], cov["covered_through"]) == (
        "not_covered", None, None)
    assert cov["missing_days"] == 14

    # Another account's verified history does not cover this account's window.
    _verify(start, end, customer=OTHER_CUSTOMER)
    assert _api("14d")["geo"]["coverage"]["status"] == "not_covered"

    # No account configured: the question cannot be asked.
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID")
    api = _api("14d")
    assert api["geo"]["coverage"]["status"] == "unavailable"
    assert api["geo"]["coverage"]["reason"] == "google_ads_customer_id_not_configured"
    _assert_no_spend_figure_in_the_payload(api)



# ═════════════════════════════════════════════════════════════════════════════
# (iii) a covered window: a country with leads and no spend is a measured zero
# ═════════════════════════════════════════════════════════════════════════════
def test_03_in_a_covered_window_a_country_with_leads_and_no_spend_is_a_published_zero(
        live_db, tmp_path):
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, start + timedelta(days=i), 20.0)
                  for i in range(7)])
    _verify(start, end)
    _seed_leads(live_db, [("h1", "United Kingdom", "qualified"),
                          ("h2", "Jordan", "junk"), ("h3", "Jordan", "junk")])

    api = _api("7d")
    assert api["geo"]["coverage"]["status"] == "covered"
    # Jordan's row is a published zero BECAUSE it is a known country (JO), the
    # window is covered, and the canonical table holds no row for it.
    by = {r["country_key"]: r for r in api["geo"]["rows"]}
    assert set(by) == {"code:GB", "code:JO"}
    assert (by["code:JO"]["spend_usd"], by["code:JO"]["spend_status"]) == (
        0.0, "published")
    assert by["code:JO"]["country_resolved"] is True
    assert api["geo"]["total_spend_usd"] == 7 * 20.0 * RATE == 175.0

    # Control — the SAME leads, the same spend rows, with the last day of the
    # window not fetched: the zero is no longer measured, and is not printed.
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_coverage")
        conn.commit()
    _verify(start, end - timedelta(days=1))
    api = _api("7d")
    _assert_no_spend_figure_in_the_payload(api)


def test_03b_a_covered_window_with_no_spend_at_all_is_a_published_zero_total(
        live_db, tmp_path):
    start, end = _window(7)
    _verify(start, end)                 # fetched, and Google reported nothing
    api = _api("7d")
    assert api["geo"]["coverage"]["status"] == "covered"
    assert api["geo"]["rows"] == [] and api["geo"]["total_spend_usd"] == 0.0


def test_03c_a_missing_fx_rate_withholds_that_country_and_the_total(
        live_db, tmp_path):
    """Covered, but one day has no GBP→USD rate: that country's spend is not
    converted at another day's rate, and a total that leaves it out is not
    published as the total."""
    start, end = _window(7)
    hole = start + timedelta(days=2)
    _write_fx(start, end, skip=(hole,))
    rows = [_spend_rows(UK, start + timedelta(days=i), 10.0) for i in range(7)]
    rows += [_spend_rows(UAE, start, 30.0)]      # UAE spent only on a rated day
    _write_spend(rows)
    _verify(start, end)

    geo = _api("7d")["geo"]
    by = {r["country"]: r for r in geo["rows"]}
    assert by["United Arab Emirates"]["spend_usd"] == 37.5
    assert by["United Kingdom"]["spend_usd"] is None
    assert by["United Kingdom"]["spend_status"] == "unavailable"
    assert by["United Kingdom"]["spend_reason"] == "fx_rate_missing"
    assert geo["total_spend_usd"] is None



# ═════════════════════════════════════════════════════════════════════════════
# (iv) the legacy monthly snapshot is never the page's spend
# ═════════════════════════════════════════════════════════════════════════════
def test_04_only_a_legacy_monthly_snapshot_is_unavailable_not_the_snapshot(
        live_db, tmp_path):
    _seed_legacy_monthly_snapshot(live_db, days_ago=2)
    _seed_leads(live_db, [("h1", "United Kingdom", "qualified")])

    for window in ("7d", "30d", "all_time"):
        api = _api(window)
        blob = json.dumps(api["geo"])
        assert "7777" not in blob and str(LEGACY_CLICKS) not in blob, window
        assert "legacy-only-campaign" not in blob, window
        _assert_no_spend_figure_in_the_payload(api)
        assert api["geo"]["coverage"]["status"] == "not_covered", window

    # And beside a COVERED canonical window the snapshot still adds nothing:
    # the published figure is the canonical one, to the cent.
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, end, 8.0)])
    _verify(start, end)
    api = _api("7d")
    assert api["geo"]["total_spend_usd"] == 10.0
    assert "7777" not in json.dumps(api["geo"])


def test_05_the_route_reads_the_canonical_table_and_never_the_legacy_one():
    """Structural, beside the behaviour above: the handler and the service
    name no legacy table, and the page's registries key on the canonical
    dataset."""
    server = (ROOT / "api/server.py").read_text(encoding="utf-8")
    handler = server.split('@app.get("/api/geo")')[1].split("\n@app.")[0]
    assert "build_country_evidence" in handler
    assert "FROM geo" not in handler and "run_date" not in handler
    service = (ROOT / "services/country_evidence_service.py").read_text(
        encoding="utf-8")
    code = "\n".join(line for line in service.splitlines()
                     if not line.lstrip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    assert "FROM geo" not in code and "write_geo" not in code
    # The shared day arithmetic, fed chunks clipped to the days each settles.
    # PR-ADS-167A: that rule now lives once, in the spend service, and this
    # page calls it (``analyze_settled_coverage`` = ``analyze_coverage`` over
    # ``settled_chunks``) rather than holding a copy.
    assert "analyze_settled_coverage(start, end" in code
    spend_src = (ROOT / "services/google_ads_spend_service.py").read_text(
        encoding="utf-8")
    assert "analyze_coverage(start, end, settled_chunks(chunks, tz))" in spend_src
    assert "fetch_geo_daily_spend_by_country" in code

    js = APP_JS.read_text(encoding="utf-8")
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    # PR-ADS-176B: the #/geo page is deleted; the one Countries page
    # (roas-countries) keys its freshness on the same canonical dataset, and
    # the Dataset Freshness link for it opens Countries. Its loader half
    # (`loadGeo` joined nothing, invented no zero) went with the page; the
    # Countries page's own no-zero rules are in tests/test_pr_ads_176b_*.
    block = js.split("const PAGE_DEPENDENCIES = {")[1].split("\n};")[0]
    line = next(ln for ln in block.splitlines() if '"roas-countries":' in ln)
    assert '"google_ads_api/canonical_geo"' in line, line
    for const in ("const PAGE_DATASET_MAP = {", "const PAGE_DEPENDENCIES = {"):
        block = js.split(const)[1].split("\n};")[0]
        assert '"google_ads_api/geo"' not in block, const
    assert '"google_ads_api/canonical_geo": { page: "roas-countries"' in js
    # The page fetched one endpoint and joined nothing in the browser.
    assert "/api/leads/country-summary" not in js
    assert "normalizeCountryKey" not in js
    assert "country_identity.resolve_iso_country_text" in code
    # …and never the supported-market registry the revenue pages group on.
    assert "country_identity.resolve(" not in code
    assert "SUPPORTED_COUNTRIES" not in code
    # No surface tells an operator to run the memory-heavy job mid-month.
    section = html.split('id="page-roas-countries"')[1].split("</section>")[0]
    for text in (js, section):
        assert "Run the monthly scheduler" not in text
        assert "has been synced yet. Run" not in text

    from services import freshness_service as fresh
    from services import system_status_service as status
    assert status.PAGE_PIPELINE_IMPACT["geo"] == ["canonical_geo"]
    assert fresh.DATASET_FRESHNESS_CONFIG["geo"]["page"] is None
    assert status.PIPELINE_DEPENDENCIES["geo"]["page"] is None
    assert "canonical_geo" in status.CORE_DATASETS
    assert fresh.DATASET_FRESHNESS_CONFIG["canonical_geo"]["stale_threshold_days"] == 3
    # No core dataset is one the monthly job alone writes.
    assert not set(status.CORE_DATASETS) & set(fresh.MONTHLY_REFRESH_DATASETS)


def test_06_the_guard_is_the_coverage_verdict(live_db, tmp_path, monkeypatch):
    """Mutation. Tell the service every window is covered and the partial sum
    is published as the window's figure. So tests 02–04 pass because of the
    gate, not by accident.

    PR-ADS-176B: the second half mutated the deleted #/geo page's own window
    check. The one Countries page's gate is mutated the same way in
    tests/test_pr_ads_176b_countries_one_page.py (case 06)."""
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, start + timedelta(days=i), 100.0)
                  for i in range(4)])
    _verify(start, end - timedelta(days=3))
    _seed_leads(live_db, [("h1", "Jordan", "junk")])
    api = _api("7d")
    assert api["geo"]["coverage"]["status"] == "not_covered"
    _assert_no_spend_figure_in_the_payload(api)

    # Server gate removed: four days of spend become "the 7-day figure", and
    # Jordan's unknown becomes a zero.
    from services import google_ads_spend_service as spend_service
    real = spend_service.analyze_coverage

    def always_complete(s, e, chunks):
        out = real(s, e, chunks)
        return {**out, "complete": True, "missing_days": 0, "missing_ranges": []}
    monkeypatch.setattr(spend_service, "analyze_coverage", always_complete)
    broken = _api("7d")["geo"]
    assert broken["total_spend_usd"] == 4 * 100.0 * RATE == 500.0
    jordan = next(r for r in broken["rows"] if r["country"] == "Jordan")
    assert (jordan["spend_usd"], jordan["spend_status"]) == (0.0, "published")
    monkeypatch.setattr(spend_service, "analyze_coverage", real)


# ═════════════════════════════════════════════════════════════════════════════
# The join: one row per geography, on the canonical country identity
# ═════════════════════════════════════════════════════════════════════════════
def test_07_lead_spellings_join_the_country_that_spent_and_never_read_zero(
        live_db, tmp_path):
    """Executed against the first rewrite: spend for "United Kingdom" (£700)
    and leads spelled `UK` and `united kingdom` rendered THREE rows —
    `United Kingdom $875` with no leads, `UK $0`, `united kingdom $0` — in a
    covered window. A zero for a country that had spend. `UAE` is a real lead
    value in production (docs/05)."""
    start, end = _window(7)
    _write_fx(start, end)
    rows = []
    for i in range(7):
        d = start + timedelta(days=i)
        rows += [_spend_rows(UK, d, 100.0), _spend_rows(UAE, d, 40.0)]
    _write_spend(rows)
    _verify(start, end + timedelta(days=1))     # today's sync has run too
    _seed_leads(live_db, [
        ("h1", "UK", "qualified"), ("h2", "UK", "junk"),
        ("h3", "united kingdom", "qualified"),
        ("h4", " United Kingdom ", "in_progress"),
        ("h5", "UAE", "qualified"), ("h6", "uae", "junk"),
        ("h7", "Jordan", "junk"),
    ])

    api = _api("7d")

    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert set(by) == {"code:GB", "code:AE", "code:JO"}
    assert by["code:GB"]["lead_country_spellings"] == [
        "UK", "United Kingdom", "united kingdom"]
    assert by["code:GB"]["total_leads"] == 4
    assert by["code:GB"]["verdicted_leads"] == 4
    assert by["code:GB"]["junk_rate_pct"] == 25.0
    assert by["code:AE"]["lead_country_spellings"] == ["UAE", "uae"]
    assert (geo["countries_active"], geo["unmatched_lead_countries"]) == (3, 0)
    assert geo["total_spend_usd"] == 1225.0
    # Nothing was lost in the join: every lead is in exactly one row.
    assert sum(r["total_leads"] for r in geo["rows"]) == 7


def test_07b_an_unmatched_lead_country_is_unavailable_and_is_not_a_geography(
        live_db, tmp_path):
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, start + timedelta(days=i), 100.0)
                  for i in range(7)])
    _verify(start, end + timedelta(days=1))     # today's sync has run too
    _seed_leads(live_db, [
        ("h1", "UK", "qualified"),
        ("h2", "Untied Kingdom", "junk"),        # a typo of a country that spent
        ("h3", "Narnia", "junk"),                # not a country
        # No country at all, six ways. ONE row, and not an unmatched country.
        ("h4", "", "junk"), ("h5", None, "junk"), ("h6", "Other", "junk"),
        ("h7", "N/A", "junk"), ("h8", "unknown", "junk"), ("h9", "ZZ", "junk"),
    ])

    api = _api("7d")

    geo = api["geo"]
    assert (geo["countries_active"], geo["unmatched_lead_countries"]) == (1, 2)
    unresolved = {r["country_key"]: r for r in geo["rows"]
                  if not r["country_resolved"]}
    assert set(unresolved) == {"lead:untied kingdom", "lead:narnia", "lead:none"}
    assert {(r["spend_usd"], r["spend_status"]) for r in unresolved.values()} == {
        (None, "unavailable")}
    assert unresolved["lead:none"]["spend_reason"] == "no_country_recorded"
    assert unresolved["lead:narnia"]["spend_reason"] == "country_not_matched"
    assert unresolved["lead:none"]["total_leads"] == 6
    assert sum(r["total_leads"] for r in geo["rows"]) == 9


# ═════════════════════════════════════════════════════════════════════════════
# The window ends yesterday — and yesterday counts only once it was re-read
# ═════════════════════════════════════════════════════════════════════════════
class _GoogleAds:
    """What Google Ads would answer for a geo fetch, per day, as it stands
    when asked. The REAL sync (`run_google_ads_geo_sync`) is driven over it:
    its chunking, range replacement, country resolution and coverage ledger
    writes are production code; only the two Google calls are this fake."""

    def __init__(self):
        self.gbp_by_day: dict = {}           # {date: {country tuple: gbp}}

    def fetch(self, start_date: str, end_date: str) -> dict:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        rows = []
        for day, by_country in sorted(self.gbp_by_day.items()):
            if start <= day <= end:
                for (criterion, _code, _name), gbp in by_country.items():
                    rows.append({"customer_id": CUSTOMER, "currency_code": "GBP",
                                 "country_criterion_id": criterion,
                                 "campaign_id": "c1", "spend_date": day,
                                 "cost_micros": int(round(gbp * 1_000_000))})
        return {"rows": rows, "source_query_version": "test"}

    @staticmethod
    def country_codes(criterion_ids) -> dict:
        known = {c[0]: {"country_code": c[1], "name": c[2]} for c in (UK, UAE)}
        return {str(i): known[str(i)] for i in criterion_ids if str(i) in known}


def _run_real_sync(monkeypatch, google, date_from: date, date_to: date) -> dict:
    from services import google_ads_geo_sync_service as geo
    monkeypatch.setattr(geo, "fetch_geo_daily", google.fetch)
    monkeypatch.setattr(geo, "fetch_geo_country_codes", google.country_codes)
    result = geo.run_google_ads_geo_sync(
        date_from=date_from.isoformat(), date_to=date_to.isoformat(),
        dry_run=False, job_id=f"sync-{date_to.isoformat()}")
    assert result["status"] == "success", result
    return result


def _backdate_ledger(cluster, when_sql: str):
    """The one thing a test cannot do for real: make the sync have run
    yesterday. Its ledger rows are re-stamped with the time it would have."""
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE google_ads_geo_coverage SET fetched_at = {when_sql}, "
                    f"updated_at = {when_sql}")
        assert cur.rowcount >= 1
        conn.commit()


def test_08_yesterday_is_published_only_after_it_was_re_read(
        live_db, tmp_path, monkeypatch):
    """The reviewer's probe, through the real sync writer.

    Yesterday 06:05 UTC the daily sync fetched [D-7, D-1] and marked the chunk
    `verified`. Yesterday (D-1) had £20 of an eventual £100 in it. Until this
    morning's sync re-reads it, that £20 is all the table holds for D-1 — and
    the ledger still says `verified`."""
    today = _today()
    start, end = _window(7)                       # end == yesterday (D-1)
    assert end == today - timedelta(days=1)
    _write_fx(start, today)
    _seed_leads(live_db, [("h1", "UK", "qualified")])
    google = _GoogleAds()
    for i in range(6):
        google.gbp_by_day[start + timedelta(days=i)] = {UK: 100.0}
    google.gbp_by_day[end] = {UK: 20.0}           # D-1 as seen during D-1

    _run_real_sync(monkeypatch, google, start, end)
    _backdate_ledger(live_db, "(CURRENT_DATE - 1) + interval '6 hours 5 minutes'")

    morning_api = _api("7d")
    _assert_no_spend_figure_in_the_payload(morning_api)
    cov = morning_api["geo"]["coverage"]
    assert (cov["status"], cov["reason"]) == (
        "not_covered", "spend_not_reread_since_day_ended")
    assert cov["covered_through"] == (end - timedelta(days=1)).isoformat()
    assert cov["missing_ranges"] == [{"start": end.isoformat(),
                                      "end": end.isoformat()}]
    assert "775" not in json.dumps(morning_api["geo"])

    # This morning's sync: its rolling window re-reads yesterday, now complete
    # at £100, and fetches the first hours of today.
    google.gbp_by_day[end] = {UK: 100.0}
    google.gbp_by_day[today] = {UK: 37.0}
    _run_real_sync(monkeypatch, google, today - timedelta(days=6), today)

    after_api = _api("7d")
    assert after_api["geo"]["coverage"]["status"] == "covered"
    assert after_api["geo"]["total_spend_usd"] == 875.0   # today's £37 is not in it

    def words(d):
        return f"{d.day} {d.strftime('%b %Y')}"

    from services.country_evidence_service import window_bounds
    assert window_bounds(7) == (start, end)
    assert window_bounds(None) == (None, end)


def test_08b_a_day_settles_only_when_fetched_after_it_ended():
    """The rule itself, in the account time zone (Europe/London), where the
    day ends at 00:00 UTC in winter and 23:00 UTC in summer."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from services import country_evidence_service as ces
    london = ZoneInfo("Europe/London")

    def settled_through(fetched_at):
        return ces.settled_through(fetched_at, london)

    def settled_chunks(chunks):
        return ces.settled_chunks(chunks, london)

    def utc(*a):
        return datetime(*a, tzinfo=timezone.utc)
    # Winter: fetched 06:05 UTC on the 19th → the 18th is complete, the 19th not.
    assert settled_through(utc(2026, 11, 19, 6, 5)) == date(2026, 11, 18)
    assert settled_through(utc(2026, 11, 18, 23, 59)) == date(2026, 11, 17)
    assert settled_through(utc(2026, 11, 19, 0, 0)) == date(2026, 11, 18)
    # Summer (BST): the account day of 1 July ends at 23:00 UTC on 1 July.
    assert settled_through(utc(2026, 7, 1, 22, 59)) == date(2026, 6, 30)
    assert settled_through(utc(2026, 7, 1, 23, 0)) == date(2026, 7, 1)
    # Unknown fetch time settles nothing; a naive timestamp is read as UTC.
    assert settled_through(None) is None
    assert settled_through("not a time") is None
    assert settled_through(datetime(2026, 11, 19, 6, 5)) == date(2026, 11, 18)
    assert settled_through("2026-11-19T06:05:00+00:00") == date(2026, 11, 18)

    # The zone is the account's, not a constant: the same instant settles a
    # different day for an account east of Greenwich. And with no zone on
    # record nothing settles.
    amman = ZoneInfo("Asia/Amman")
    assert ces.settled_through(utc(2026, 11, 18, 21, 30), amman) == date(2026, 11, 18)
    assert ces.settled_through(utc(2026, 11, 18, 21, 30), london) == date(2026, 11, 17)
    assert ces.settled_through(utc(2026, 11, 19, 6, 5), None) is None
    assert ces.settled_chunks([{"chunk_start": "2026-11-01", "chunk_end": "2026-11-10",
                                "status": "verified",
                                "fetched_at": utc(2026, 11, 19, 6, 5)}], None) == []

    chunks = [
        {"chunk_start": "2026-11-12", "chunk_end": "2026-11-18", "status": "verified",
         "fetched_at": utc(2026, 11, 18, 6, 5)},      # fetched DURING its last day
        {"chunk_start": "2026-11-19", "chunk_end": "2026-11-19", "status": "verified",
         "fetched_at": utc(2026, 11, 19, 6, 5)},      # settles nothing
        {"chunk_start": "2026-11-01", "chunk_end": "2026-11-30", "status": "verified",
         "fetched_at": utc(2026, 11, 10, 12, 0)},     # a backfill of a month in progress
        {"chunk_start": "2026-11-05", "chunk_end": "2026-11-06", "status": "verified",
         "fetched_at": None},
        {"chunk_start": "2026-11-17", "chunk_end": "2026-11-18", "status": "failed",
         "fetched_at": utc(2026, 11, 18, 6, 5)},
    ]
    out = settled_chunks(chunks)
    assert [(c["chunk_start"], c["chunk_end"], c["status"]) for c in out] == [
        ("2026-11-12", "2026-11-17", "verified"),
        ("2026-11-01", "2026-11-09", "verified"),
        ("2026-11-17", "2026-11-18", "failed")]


# ═════════════════════════════════════════════════════════════════════════════
# all_time reads only the certified range
# ═════════════════════════════════════════════════════════════════════════════
def test_09_all_time_publishes_only_rows_inside_the_certified_range(
        live_db, tmp_path):
    """`all_time` has no requested start, so coverage starts at the earliest
    VERIFIED chunk. The row read had no lower bound: a row dated 20 days before
    anything the ledger certifies was summed into a "covered" figure."""
    _, end = _window(7)
    certified_from = end - timedelta(days=9)
    stray = certified_from - timedelta(days=20)
    _write_fx(stray, end)
    _write_spend([_spend_rows(UK, certified_from + timedelta(days=i), 10.0)
                  for i in range(10)])
    _write_spend([_spend_rows(UK, stray, 5000.0),          # never certified
                  _spend_rows(UAE, stray, 800.0)])
    _verify(certified_from, end + timedelta(days=1))   # today's sync has run

    api = _api("all_time")
    geo = api["geo"]
    assert geo["coverage"]["status"] == "covered"
    assert geo["coverage"]["requested_from"] is None
    assert geo["coverage"]["covered_from"] == certified_from.isoformat()
    assert geo["window_range"]["from"] == certified_from.isoformat()
    assert geo["total_spend_usd"] == 125.0
    assert [r["country_key"] for r in geo["rows"]] == ["code:GB"]
    assert "6250" not in json.dumps(geo) and "6375" not in json.dumps(geo)

    def words(d):
        return f"{d.day} {d.strftime('%b %Y')}"

    # A gap inside the certified span makes all_time not covered, as before.
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_coverage")
        conn.commit()
    _verify(certified_from, certified_from + timedelta(days=3))
    _verify(certified_from + timedelta(days=5), end)
    gapped = _api("all_time")
    assert gapped["geo"]["coverage"]["status"] == "not_covered"
    _assert_no_spend_figure_in_the_payload(gapped)


# ═════════════════════════════════════════════════════════════════════════════
# Production-shaped: 162 spend codes, lower-case lead countries
# ═════════════════════════════════════════════════════════════════════════════
KZ = ("2398", "KZ", "Kazakhstan")
GE = ("2268", "GE", "Georgia")
NO_CODE = ("9999", None, None)


def _seed_leads_full(cluster, rows):
    """`rows`: [(contact_id, country, status_category, campaign, keyword)]."""
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO runs (run_type, started_at, status) VALUES "
                    "('daily', now(), 'success') RETURNING id")
        run_id = cur.fetchone()[0]
        for contact_id, country, category, campaign, keyword in rows:
            cur.execute(
                "INSERT INTO leads (run_id, run_date, contact_id, country, "
                "status_category, campaign_name, keyword, source_type) "
                "VALUES (%s, CURRENT_DATE, %s, %s, %s, %s, %s, 'paid_search')",
                (run_id, contact_id, country, category, campaign, keyword))
        conn.commit()


def test_10_every_production_spend_code_is_its_own_row_and_leads_join_it(
        live_db, tmp_path):
    """Verified read-only in production on 2026-11-19: spend in 162 distinct
    ISO codes, and lead countries stored lower-case. Through the supported-
    market registry 81 of those codes collapsed into ONE "(unresolved
    location)" row and leads for Nigeria or Kazakhstan read "not matched"."""
    from analysis import country_identity as ci
    from tests.test_pr_ads_165a_country_identity_complete import (
        PRODUCTION_LEAD_COUNTRIES, PRODUCTION_SPEND_CODES)
    start, end = _window(7)
    _write_fx(start, end)
    # One spend row per production code: £(i+1), under Google's criterion id
    # and Google's name for it, plus one row Google gave no country for.
    spend = []
    for i, code in enumerate(PRODUCTION_SPEND_CODES):
        spend.append(_spend_rows((f"2{i:03d}", code, ci.iso_country_label(code)),
                                 end, float(i + 1)))
    spend.append(_spend_rows(NO_CODE, end, 7.0))
    _write_spend(spend)
    _verify(start, end)
    # One lead under each of the 40 production spellings, as stored.
    _seed_leads(live_db, [(f"p{i}", text, "qualified" if i % 2 else "junk")
                          for i, text in enumerate(sorted(PRODUCTION_LEAD_COUNTRIES))])

    api = _api("7d")
    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}

    # Every code is its own row, under its own name, with exactly its spend.
    for i, code in enumerate(PRODUCTION_SPEND_CODES):
        row = by[f"code:{code}"]
        assert row["country"] == ci.iso_country_label(code), code
        assert row["country_resolved"] is True, code
        assert (row["spend_usd"], row["spend_status"]) == (
            round((i + 1) * RATE, 2), "published"), code
    # Only the row Google gave no code for is unresolved.
    unresolved = [r for r in geo["rows"] if not r["country_resolved"]]
    assert [(r["country_key"], r["country"], r["spend_usd"]) for r in unresolved] == [
        ("spend:unresolved", "(unresolved location)", 8.75)]
    # Every lead spelling joined its country — none is "not matched", none is
    # a row of its own. 39 of the 40 are countries that spent. Pakistan is in
    # the lead list and not in the spend list — and this window holds spend
    # with no country (the no-code row), which may be Pakistan's. So Pakistan
    # is not a zero: it is unavailable, and says why.
    assert geo["unmatched_lead_countries"] == 0
    no_spend = sorted(t for t, c in PRODUCTION_LEAD_COUNTRIES.items()
                      if c not in PRODUCTION_SPEND_CODES)
    assert no_spend == ["pakistan"]
    for text, code in PRODUCTION_LEAD_COUNTRIES.items():
        row = by[f"code:{code}"]
        assert row["lead_country_spellings"] == [text], text
        assert row["total_leads"] == 1, text
        if text == "pakistan":
            continue
        assert row["spend_status"] == "published", text
        assert row["spend_usd"] > 0, text
    assert (by["code:PK"]["spend_usd"], by["code:PK"]["spend_status"],
            by["code:PK"]["spend_reason"]) == (
        None, "unavailable", "spend_location_unresolved")
    assert geo["unresolved_spend"] == {"criteria": 1, "conflicting_criteria": 0,
                                       "spend_usd": 8.75, "blocks_zero": True,
                                       # PR-ADS-167A: the countries it affects.
                                       "zero_withheld_countries": ["Pakistan"]}
    # 162 spend countries + Pakistan + the one unresolved spend row.
    assert len(geo["rows"]) == 164
    assert geo["countries_active"] == 163
    total = round((sum(range(1, 163)) + 7.0) * RATE, 2)
    assert geo["total_spend_usd"] == total == 16512.5
    assert round(sum(r["spend_usd"] or 0 for r in geo["rows"]), 2) == total
    assert sum(r["total_leads"] for r in geo["rows"]) == 40



def test_11_named_spend_is_never_relabelled_and_no_text_is_joined_by_guess(
        live_db, tmp_path):
    """The reviewer's probe. Kazakhstan and Georgia are outside the supported-
    market registry: their spend was merged into "(unresolved location)" with
    the spend Google really gave no country for, and the Kazakhstan lead read
    "not matched"."""
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, end, 100.0), _spend_rows(KZ, end, 50.0),
                  _spend_rows(GE, end, 30.0), _spend_rows(NO_CODE, end, 7.0)])
    _verify(start, end)
    leads = [
        ("a1", "Kazakhstan", "qualified"), ("a2", "kazakhstan", "junk"),
        ("a3", "Georgia", "junk"),
        # Text that names no single country — never joined, never zero.
        ("a4", "Congo", "junk"), ("a5", "California", "junk"),
        ("a6", "Dubai", "qualified"), ("a7", "IN", "junk"), ("a8", "CA", "junk"),
        # No country at all.
        ("a9", "Other", "junk"), ("a10", "N/A", "junk"), ("a11", None, "junk"),
        # Spellings of countries.
        ("a12", "US", "junk"), ("a13", "Türkiye", "junk"),
        ("a14", "North Korea", "junk"), ("a15", "England", "in_progress"),
        ("a16", "Jordan.", "junk"),
    ]
    _seed_leads(live_db, leads)

    api = _api("7d")

    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert by["code:KZ"]["lead_country_spellings"] == ["Kazakhstan", "kazakhstan"]
    assert (by["code:KZ"]["spend_usd"], by["code:GE"]["spend_usd"]) == (62.5, 37.5)
    assert by["spend:unresolved"]["spend_usd"] == 8.75
    assert "code:CG" not in by and "code:CD" not in by       # "Congo" is neither
    assert "code:IN" not in by and "code:CA" not in by
    assert "code:AE" not in by                               # Dubai is not the UAE
    # Totals conserved: nothing dropped, nothing counted twice.
    assert geo["total_spend_usd"] == (100 + 50 + 30 + 7) * RATE == 233.75
    assert round(sum(r["spend_usd"] or 0 for r in geo["rows"]), 2) == 233.75
    assert sum(r["total_leads"] for r in geo["rows"]) == len(leads)
    assert (geo["countries_active"], geo["unmatched_lead_countries"]) == (7, 5)


def test_11b_a_code_outside_every_table_is_still_keyed_by_googles_code(
        live_db, tmp_path):
    """Google reports Kosovo as `XK`, which ISO never assigned. The code Google
    supplied is the identity; no registry of ours has to list it."""
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(("2900", "XK", "Kosovo"), end, 40.0),
                  # Two criteria, one code: one row, summed.
                  _spend_rows(("2826", "GB", "United Kingdom"), end, 10.0),
                  _spend_rows(("20339", "gb", "United Kingdom"), end, 6.0)])
    _verify(start, end)
    geo = _api("7d")["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert set(by) == {"code:XK", "code:GB"}
    assert (by["code:XK"]["country"], by["code:XK"]["spend_usd"]) == ("Kosovo", 50.0)
    assert by["code:XK"]["country_resolved"] is True
    assert by["code:GB"]["spend_usd"] == 20.0


def test_12_a_merged_countrys_top_campaign_is_shown_only_when_it_is_known(
        live_db, tmp_path):
    """The lead summary carries each spelling's most common campaign, not the
    counts behind it. Across spellings the true mode here is camp-B (4 of 7);
    the largest single spelling ("UK", 3 leads) says camp-A. Showing camp-A
    would be wrong, so a merged row shows one only when every spelling
    agrees."""
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, end, 100.0), _spend_rows(UAE, end, 10.0)])
    _verify(start, end)
    _seed_leads_full(live_db, [
        ("b1", "UK", "junk", "camp-A", "kwA"), ("b2", "UK", "junk", "camp-A", "kwA"),
        ("b3", "UK", "unknown", "camp-A", "kwA"),
        ("b4", "United Kingdom", "qualified", "camp-B", "kwB"),
        ("b5", "United Kingdom", "qualified", "camp-B", "kwB"),
        ("b6", "England", "qualified", "camp-B", "kwB"),
        ("b7", "Great Britain", "qualified", "camp-B", "kwB"),
        # Two spellings that agree: the mode of each is the mode of both.
        ("b8", "UAE", "junk", "camp-C", "kwC"),
        ("b9", "united arab emirates", "junk", "camp-C", "kwC"),
    ])
    api = _api("7d")
    by = {r["country_key"]: r for r in api["geo"]["rows"]}
    assert by["code:GB"]["total_leads"] == 7
    assert (by["code:GB"]["top_campaign"], by["code:GB"]["top_keyword"]) == (None, None)
    assert (by["code:AE"]["top_campaign"], by["code:AE"]["top_keyword"]) == (
        "camp-C", "kwC")
    assert (by["code:GB"]["top_campaign_several"],
            by["code:GB"]["top_keyword_several"]) == (True, True)
    assert by["code:AE"]["top_campaign_several"] is False
    assert "camp-A" not in json.dumps(api["geo"])


def test_13_unreadable_leads_are_unknown_on_the_table_and_on_the_map(
        live_db, tmp_path, monkeypatch):
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, end, 100.0)])
    _verify(start, end)
    _seed_leads(live_db, [("c1", "UK", "qualified")])
    import api.server as server
    monkeypatch.setattr(
        server, "api_leads_country_summary",
        lambda **k: {"days": 7, "rows": [], "db_unavailable": True})
    geo = json.loads(json.dumps(
        server.api_geo(user=USER, days=30, window="7d"), default=str))
    assert geo["leads_status"] == "unavailable"
    assert geo["countries_active"] is None
    row = geo["rows"][0]
    assert row["spend_usd"] == 125.0            # spend is unaffected
    for field in ("total_leads", "confirmed_sqls", "confirmed_junk", "junk_rate_pct"):
        assert row[field] is None, field



# ══════════════════════════════════════════════════════════════════════════
# Fourth review — spend with no country, and the minors
# ══════════════════════════════════════════════════════════════════════════
PK_TEXT = "Pakistan"
# A criterion no row ever names. Until PR-ADS-170A this fixture used production's
# `2192`; the owner has since labelled 2192 Cuba (config/geo_owner_labels.yaml),
# so it is no longer "a location nothing names". The rule under test is about
# such a location, so the fixture uses an id the owner has NOT labelled. The
# assertions are unchanged. 2192 itself: tests/test_pr_ads_170a_*.
UNNAMED = ("2997", None, None)
UNAVAILABLE_NO_COUNTRY = "Unavailable — some spend in this window has no country"


def _failed_lookup_sync(monkeypatch, google, today):
    """This morning's REAL sync, with Google's country lookup failing.

    Before PR-ADS-167A the sync treated that as non-fatal: it stored every row
    of the range with no country code, marked the chunk `verified` and
    reported `success`. Now it names each criterion from the codes already
    stored for it, stores only the criteria nothing on record names with no
    code, and reports `partial` (``country_lookup_failed``) — the chunk is
    still `verified`, because the spend landed."""
    from services import google_ads_geo_sync_service as geo
    monkeypatch.setattr(geo, "fetch_geo_daily", google.fetch)

    def boom(ids):
        raise RuntimeError("geo_target_constant: DEADLINE_EXCEEDED")
    monkeypatch.setattr(geo, "fetch_geo_country_codes", boom)
    result = geo.run_google_ads_geo_sync(
        date_from=(today - timedelta(days=6)).isoformat(),
        date_to=today.isoformat(), dry_run=False, job_id="sync-today")
    # The status is the caller's to assert: `success` when storage named
    # every criterion (Google was not asked), `partial` when it had to ask.
    assert result["status"] in ("success", "partial"), result
    return result


def test_14_a_failed_country_lookup_does_not_turn_a_countrys_spend_into_zero(
        live_db, tmp_path, monkeypatch):
    """The reviewer's scenario, with the history production has.

    The UAE spent £50 three days ago, and £8 nine days ago (outside this
    morning's re-read range, so that row keeps the code it was stored with).
    This morning the lookup failed: every row of the last seven days was
    rewritten with NO country code. The page used to read "United Arab
    Emirates $0" beside "(unresolved location) $63".

    The criterion id is Google's own id for the location. A code any stored
    row of that criterion carries is stored evidence about the same location,
    so the UAE's code-less rows are the UAE's."""
    today = _today()
    start, end = _window(7)
    _write_fx(start - timedelta(days=5), today)
    _seed_leads(live_db, [("h1", "UAE", "qualified"),
                          ("h2", "United Arab Emirates", "junk")])
    google = _GoogleAds()
    for i in range(7):
        google.gbp_by_day[start + timedelta(days=i)] = {UK: 100.0}
    google.gbp_by_day[end - timedelta(days=2)][UAE] = 50.0
    google.gbp_by_day[start - timedelta(days=2)] = {UAE: 8.0}     # older history
    _run_real_sync(monkeypatch, google, start - timedelta(days=2), end)
    _backdate_ledger(live_db, "(CURRENT_DATE - 1) + interval '6 hours 5 minutes'")
    google.gbp_by_day[today] = {UK: 5.0}
    result = _failed_lookup_sync(monkeypatch, google, today)
    # Every criterion in the range has a code on record: Google was not asked,
    # so its failure did not matter, and the run is clean.
    assert result["status"] == "success", result
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM google_ads_geo_daily_spend "
                    "WHERE country_code IS NULL AND spend_date BETWEEN %s AND %s",
                    (start, end))
        # PR-ADS-167A: the sync no longer blanks them — every criterion here
        # has a code on record, so storage named it and Google was not asked.
        # (Before: >= 7 blank rows, and the page read them through the
        # criterion's identity — which it still does, for rows a pre-167A
        # sync left blank in production.)
        assert cur.fetchone()[0] == 0

    api = _api("7d")
    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert set(by) == {"code:GB", "code:AE"}
    assert (by["code:AE"]["spend_usd"], by["code:AE"]["spend_status"]) == (
        62.5, "published")
    assert by["code:AE"]["country"] == "United Arab Emirates"
    assert by["code:GB"]["spend_usd"] == 875.0
    assert geo["total_spend_usd"] == 937.5
    assert geo["unresolved_spend"] == {"criteria": 0, "conflicting_criteria": 0,
                                       "spend_usd": None, "blocks_zero": False,
                                       "zero_withheld_countries": []}


def test_14b_when_no_stored_row_names_the_location_the_country_is_not_zero(
        live_db, tmp_path, monkeypatch):
    """No stored row names criterion 2784. Nothing on record says that spend
    is the UAE's, and nothing here guesses it. But the UAE is not `$0`
    either: the window holds spend with no country, and that is said.

    PR-ADS-167A changed how this state arises. In 165A it was the UAE's only
    rows being re-read by a sync whose lookup failed — the sync now keeps
    their stored code (test_pr_ads_167a_geo_sync_country_codes). It still
    arises for a location FIRST seen on the morning the lookup fails: the UAE
    spends for the first time inside this morning's re-read range."""
    today = _today()
    start, end = _window(7)
    _write_fx(start - timedelta(days=2), today)
    _seed_leads(live_db, [("h1", "UAE", "qualified"),
                          ("h2", "United Arab Emirates", "junk")])
    google = _GoogleAds()
    for i in range(7):
        google.gbp_by_day[start + timedelta(days=i)] = {UK: 100.0}
    _run_real_sync(monkeypatch, google, start, end)
    _backdate_ledger(live_db, "(CURRENT_DATE - 1) + interval '6 hours 5 minutes'")
    google.gbp_by_day[end - timedelta(days=2)][UAE] = 50.0
    google.gbp_by_day[today] = {UK: 5.0}
    result = _failed_lookup_sync(monkeypatch, google, today)
    # The UAE had to be looked up, and the lookup failed: said, not swallowed.
    assert (result["status"], result["reason"]) == (
        "partial", "country_lookup_failed"), result

    api = _api("7d")
    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert (by["code:AE"]["spend_usd"], by["code:AE"]["spend_status"],
            by["code:AE"]["spend_reason"]) == (
        None, "unavailable", "spend_location_unresolved")
    assert geo["total_spend_usd"] == 937.5
    assert geo["unresolved_spend"]["blocks_zero"] is True


def test_15_a_location_nothing_names_blocks_a_zero_only_in_a_window_that_holds_it(
        live_db, tmp_path):
    """Production, read on 2026-11-19: criterion `2192` had 17 rows, none with
    a country code or name, the newest on 2026-11-06 — a standing, tiny state.
    (PR-ADS-170A: the owner has since labelled `2192`; `UNNAMED` is an
    unlabelled criterion in the same state.)

    In a window that holds some of it, a lead country with no spend row is
    unavailable (that spend may be its own) and every country WITH a row is
    published unchanged. In a window that holds none of it, the same lead
    country is a true `$0`."""
    start, end = _window(7)
    older = start - timedelta(days=10)
    _write_fx(older, end)
    _seed_leads(live_db, [("k1", PK_TEXT, "junk"), ("k2", "UK", "qualified")])
    _write_spend([_spend_rows(UK, end, 100.0), _spend_rows(UAE, end, 40.0),
                  _spend_rows(UNNAMED, older, 0.45)])           # before the window
    _verify(older, end)

    # 7 days: none of the unnamed location's spend is in the window.
    api = _api("7d")
    assert api["geo"]["unresolved_spend"]["blocks_zero"] is False
    pk = {r["country_key"]: r for r in api["geo"]["rows"]}["code:PK"]
    assert (pk["spend_usd"], pk["spend_status"], pk["spend_reason"]) == (
        0.0, "published", None)

    # 30 days: 45p of it is.
    _write_fx(end - timedelta(days=40), end)
    _verify(end - timedelta(days=40), older - timedelta(days=1))
    api = _api("30d")
    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert (by["code:PK"]["spend_usd"], by["code:PK"]["spend_reason"]) == (
        None, "spend_location_unresolved")
    assert (by["code:GB"]["spend_status"], by["code:AE"]["spend_status"]) == (
        "published", "published")
    assert geo["unresolved_spend"] == {"criteria": 1, "conflicting_criteria": 0,
                                       "spend_usd": 0.56, "blocks_zero": True,
                                       "zero_withheld_countries": ["Pakistan"]}
    assert geo["total_spend_usd"] == round((100 + 40 + 0.45) * RATE, 2)


def test_16_a_criterion_stored_under_two_codes_is_not_given_either(
        live_db, tmp_path):
    """If the rows of one Google criterion disagree about its country, the
    stored data contradicts itself. Neither code is used: the spend is
    unresolved, and is not added to either country."""
    start, end = _window(7)
    _write_fx(start - timedelta(days=20), end)
    twice = "2999"
    _write_spend([
        _spend_rows(UK, end, 100.0),
        _spend_rows((twice, "FR", "France"), start - timedelta(days=15), 9.0),
        _spend_rows((twice, "DE", "Germany"), end, 30.0),
        # The same criterion again, with no code: still not resolvable.
        _spend_rows((twice, None, None), end - timedelta(days=1), 10.0)])
    _verify(start, end)
    _seed_leads(live_db, [("g1", "Germany", "qualified"), ("g2", "France", "junk")])

    api = _api("7d")
    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert by["code:DE"]["spend_usd"] is None and by["code:FR"]["spend_usd"] is None
    assert geo["unresolved_spend"] == {"criteria": 1, "conflicting_criteria": 1,
                                       "spend_usd": 50.0, "blocks_zero": True,
                                       "zero_withheld_countries": ["France", "Germany"]}
    assert geo["total_spend_usd"] == 175.0

    # The rule itself: one code resolves, none and two do not.
    from services.country_evidence_service import identify_spend_rows
    rows, unresolved, conflicting = identify_spend_rows(
        [{"country_criterion_id": "1", "country_code": None},
         {"country_criterion_id": "2", "country_code": "ZZ"},
         {"country_criterion_id": "3", "country_code": "DE"},
         {"country_criterion_id": "", "country_code": "GB"}],
        {"1": {"codes": ["AE"], "name": "United Arab Emirates"},
         "2": {"codes": [], "name": None},
         "3": {"codes": ["DE", "FR"], "name": "Germany"},
         "": {"codes": ["GB"], "name": "United Kingdom"}})
    assert [r["country_code"] for r in rows] == ["AE", None, None, None]
    assert (unresolved, conflicting) == (["", "2", "3"], ["3"])


def test_16b_an_unreadable_identity_lookup_is_unavailable_not_a_guess(
        live_db, tmp_path, monkeypatch):
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, end, 100.0)])
    _verify(start, end)
    _seed_leads(live_db, [("u1", "UK", "qualified")])
    from db import revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_geo_criterion_identities",
                        lambda ids: {"available": False, "identities": {}})
    api = _api("7d")
    _assert_no_spend_figure_in_the_payload(api)
    assert api["geo"]["coverage"]["reason"] == "geo_rows_unreadable"


def test_17_spend_under_a_dollar_never_reads_as_zero(live_db, tmp_path):
    """39p is `$0.49`, and `fmtDollar` rounds to whole dollars: it printed
    `$0` — beside the rule that `$0` is a measured zero."""
    start, end = _window(7)
    _write_fx(start, end)
    fiji = ("2242", "FJ", "Fiji")
    _write_spend([_spend_rows(fiji, end, 0.39), _spend_rows(UK, end, 0.79),
                  _spend_rows(UAE, end, 0.80)])
    _verify(start, end)
    _seed_leads(live_db, [("f1", "Pakistan", "junk")])
    api = _api("7d")
    # PR-ADS-176B: the payload half (the "<$1" display rule moved with the
    # page to the one Countries page: tests/test_pr_ads_176b_* case 11).
    by = {r["country"]: r for r in api["geo"]["rows"]}
    assert by["Fiji"]["spend_usd"] == 0.49                  # 39p, rounded to the cent
    assert by["United Kingdom"]["spend_usd"] == 0.99
    assert by["United Arab Emirates"]["spend_usd"] == 1.0
    assert (by["Pakistan"]["spend_usd"], by["Pakistan"]["spend_status"]) == (
        0.0, "published")                              # the one measured zero
    assert [c for c, r in by.items() if r["spend_usd"] == 0] == ["Pakistan"]

    # A window whose whole spend is under a dollar: the total is not 0.
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM google_ads_geo_daily_spend "
                    "WHERE country_code <> 'FJ'")
        conn.commit()
    assert _api("7d")["geo"]["total_spend_usd"] == 0.49


def test_18_unreadable_leads_are_not_reported_as_no_leads(
        live_db, tmp_path, monkeypatch):
    """Nothing synced and the lead summary unreadable: the table said "No
    leads in the selected window." — a count nobody read."""
    import api.server as server
    real_summary = server.api_leads_country_summary
    monkeypatch.setattr(
        server, "api_leads_country_summary",
        lambda **k: {"days": 7, "rows": [], "db_unavailable": True})
    api = _api_geo_only("7d")
    assert api["geo"]["rows"] == [] and api["geo"]["leads_status"] == "unavailable"

    # The route's own fallback, when the lead read raises.
    def raising(**k):
        raise RuntimeError("boom")
    monkeypatch.setattr(server, "api_leads_country_summary", raising)
    api = _api_geo_only("7d")
    assert api["geo"]["leads_status"] == "unavailable"

    # Readable leads, and none: that IS "no leads".
    monkeypatch.setattr(server, "api_leads_country_summary", real_summary)
    api = _api("7d")
    assert api["geo"]["leads_status"] == "published" and api["geo"]["rows"] == []


def _api_geo_only(window: str) -> dict:
    import api.server as server
    return json.loads(json.dumps(
        {"geo": server.api_geo(user=USER, days=30, window=window)}, default=str))


def test_19_a_failed_re_read_is_not_described_as_waiting_for_the_morning_sync(
        live_db, tmp_path, monkeypatch):
    """Yesterday was fetched during yesterday. This morning's sync ran — and
    failed. The page still said "The morning sync re-reads it; this page is
    available after that"."""
    today = _today()
    start, end = _window(7)
    _write_fx(start - timedelta(days=2), today)
    _seed_leads(live_db, [("h1", "UK", "qualified")])
    google = _GoogleAds()
    for i in range(7):
        google.gbp_by_day[start + timedelta(days=i)] = {UK: 100.0}
    _run_real_sync(monkeypatch, google, start, end)
    _backdate_ledger(live_db, "(CURRENT_DATE - 1) + interval '6 hours 5 minutes'")
    from services import google_ads_geo_sync_service as geo

    def down(a, b):
        raise RuntimeError("Google Ads API 503")
    monkeypatch.setattr(geo, "fetch_geo_daily", down)
    result = geo.run_google_ads_geo_sync(
        date_from=(today - timedelta(days=6)).isoformat(),
        date_to=today.isoformat(), dry_run=False, job_id="sync-today")
    assert result["status"] != "success"

    api = _api("7d")
    _assert_no_spend_figure_in_the_payload(api)
    cov = api["geo"]["coverage"]
    assert (cov["status"], cov["reason"]) == ("not_covered", "spend_reread_failed")


def test_20_the_country_counts_count_countries(live_db, tmp_path):
    """"Countries with SQLs" and "High Junk Countries" counted an unmatched
    lead text and the "No country recorded" row as countries."""
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, end, 100.0)])
    _verify(start, end)
    _seed_leads(live_db, [
        ("s1", "UK", "qualified"),
        ("s2", "Dubai", "qualified"), ("s3", "Dubai", "junk"), ("s4", "Dubai", "junk"),
        ("s5", "Other", "qualified"), ("s6", None, "junk"), ("s7", "N/A", "junk"),
        ("s8", "Jordan", "junk"),
    ])
    # PR-ADS-176B: the page's two KPI counts ("Countries with SQLs", "High
    # Junk Countries") are lead-quality figures, dropped with the page (owner
    # decision 3). What the payload says about each row stays checked.
    by = {r["country"]: r for r in _api("7d")["geo"]["rows"]}
    assert by["Dubai"]["confirmed_sqls"] == 1 and by["Dubai"]["country_resolved"] is False
    assert by["No country recorded"]["confirmed_sqls"] == 1
    assert by["No country recorded"]["country_resolved"] is False
    # A known zero is the number 0, not None.
    assert (by["Jordan"]["confirmed_sqls"], by["Jordan"]["in_progress"]) == (0, 0)
    assert by["United Kingdom"]["confirmed_junk"] == 0


def test_21_kosovo_leads_join_kosovos_spend(live_db, tmp_path):
    """Google reports Kosovo under `XK`, a code ISO has not assigned. The lead
    text "Kosovo" resolved to nothing, so it sat in its own row, "not
    matched", beside Kosovo's spend."""
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(("2900", "XK", "Kosovo"), end, 12.0)])
    _verify(start, end)
    _seed_leads(live_db, [("x1", "Kosovo", "qualified"), ("x2", "kosovo", "junk")])
    api = _api("7d")
    rows = api["geo"]["rows"]
    assert [r["country"] for r in rows] == ["Kosovo"]
    assert (rows[0]["spend_usd"], rows[0]["total_leads"], rows[0]["confirmed_sqls"]) == (
        15.0, 2, 1)
    assert api["geo"]["unmatched_lead_countries"] == 0


def test_22_the_account_time_zone_is_read_where_the_sync_reads_it(
        live_db, tmp_path):
    """Not a constant. With no zone on record "the day ended" has no meaning,
    and the page is unavailable rather than assuming one."""
    start, end = _window(7)
    _write_fx(start, end)
    _write_spend([_spend_rows(UK, end, 100.0)])
    _verify(start, end)
    _seed_leads(live_db, [("z1", "UK", "qualified")])
    api = _api("7d")
    assert api["geo"]["account_time_zone"] == "Europe/London"
    assert api["geo"]["coverage"]["status"] == "covered"

    _seed_account_zone(live_db, None)
    api = _api("7d")
    _assert_no_spend_figure_in_the_payload(api)
    cov = api["geo"]["coverage"]
    assert (cov["status"], cov["reason"]) == ("unavailable", "account_time_zone_unknown")

    _seed_account_zone(live_db, "Not/AZone")
    assert _api("7d")["geo"]["coverage"]["reason"] == "account_time_zone_unknown"
