"""PR-ADS-165A — country spend is published only for a window that was
actually synced. The harness of every Countries PostgreSQL suite.

PR-ADS-165A rebuilt the Google Ads → Countries page (`#/geo`, ``GET
/api/geo``) on the canonical DAILY country table the incremental sync writes
(``google_ads_geo_daily_spend``) and the coverage ledger beside it
(``google_ads_geo_coverage``), after the page had printed ``$0`` for every
country twelve days into a month (the legacy ``geo`` snapshot, written only by
the monthly job, held no row for the window) and the days up to the 1st as
"the 30-day figure".

PR-ADS-176B deleted the page; PR-ADS-176C retired ``/api/geo``,
``/api/leads/country-summary`` and ``services/country_evidence_service``.
Every case this module ran against that payload is now asserted on the
country surfaces that are live — the mart's country view (the one Countries
page) and Dashboard → Countries — in
``tests/test_pr_ads_176c_country_feed_retired_pg_integration.py``; the port
table, case by case, is docs/58 §8.3. Three findings the port surfaced
(properties /api/geo held that the live surfaces do not) are docs/58 §8.4.

What stays here:

* the shared harness — the throwaway cluster (``live_db``), the real writers
  for spend, FX and the coverage ledger, the fake Google Ads behind the REAL
  geo sync, and the fixtures the 167A / 168A / 170A / 176A / 176B / 176C
  suites import;
* case 05, the structural half: the reader both live country builders share
  reads the canonical table, and no page registry points at the monthly one;
* case 08b, the settle rule itself (a day counts only once a verified chunk
  was fetched after it ended), now on ``google_ads_spend_service``, where it
  has lived since PR-ADS-167A.
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




def test_05_the_country_spend_reader_reads_the_canonical_table_and_never_the_legacy_one():
    """Structural, beside the behaviour in the 176C suite. PR-ADS-176C: the
    ``/api/geo`` handler and ``country_evidence_service`` this case read are
    deleted. The reader both live country builders share
    (``country_spend_zero_rule.read_country_spend``: the mart's country view
    and Dashboard → Countries) reads the canonical daily table and names no
    legacy one, and the page registries key on the canonical dataset.

    (The mart's country view still has PR-ADS-124's legacy fallback when
    canonical geo has no rows — FINDING 176C-F5, docs/58 §8.4; every figure
    it could carry is withheld by the gate, case 04 of the 176C suite.)"""
    shared = (ROOT / "services/country_spend_zero_rule.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in shared.splitlines()
                     if not line.lstrip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    assert "repo.fetch_geo_daily_spend_by_country(" in code
    assert "FROM geo" not in code and "write_geo" not in code
    for path in ("services/revenue_attribution_service.py",
                 "services/dashboard_countries_service.py"):
        assert "zero_rule.read_country_spend(" in (ROOT / path).read_text(
            encoding="utf-8"), path
    # The shared day arithmetic, fed chunks clipped to the days each settles.
    spend_src = (ROOT / "services/google_ads_spend_service.py").read_text(
        encoding="utf-8")
    assert "analyze_coverage(start, end, settled_chunks(chunks, tz))" in spend_src

    js = APP_JS.read_text(encoding="utf-8")
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    block = js.split("const PAGE_DEPENDENCIES = {")[1].split("\n};")[0]
    line = next(ln for ln in block.splitlines() if '"roas-countries":' in ln)
    assert '"google_ads_api/canonical_geo"' in line, line
    for const in ("const PAGE_DATASET_MAP = {", "const PAGE_DEPENDENCIES = {"):
        block = js.split(const)[1].split("\n};")[0]
        assert '"google_ads_api/geo"' not in block, const
    assert '"google_ads_api/canonical_geo": { page: "roas-countries"' in js
    for route in ("/api/geo", "/api/leads/country-summary"):
        assert route not in js and route not in html, route
    assert "normalizeCountryKey" not in js
    # No surface tells an operator to run the memory-heavy job mid-month.
    section = html.split('id="page-roas-countries"')[1].split("</section>")[0]
    for text in (js, section):
        assert "Run the monthly scheduler" not in text
        assert "has been synced yet. Run" not in text

    from services import freshness_service as fresh
    from services import system_status_service as status
    # PR-ADS-176C: the retired page's key is gone; the one Countries page
    # depends on canonical geo and the campaign total it reconciles against.
    assert "geo" not in status.PAGE_PIPELINE_IMPACT
    assert status.PAGE_PIPELINE_IMPACT["countries"] == ["canonical_geo", "canonical_spend"]
    assert fresh.DATASET_FRESHNESS_CONFIG["geo"]["page"] is None
    assert status.PIPELINE_DEPENDENCIES["geo"]["page"] is None
    assert "canonical_geo" in status.CORE_DATASETS
    assert fresh.DATASET_FRESHNESS_CONFIG["canonical_geo"]["stale_threshold_days"] == 3
    # No core dataset is one the monthly job alone writes.
    assert not set(status.CORE_DATASETS) & set(fresh.MONTHLY_REFRESH_DATASETS)


def test_08b_a_day_settles_only_when_fetched_after_it_ended():
    """The rule itself, in the account time zone (Europe/London), where the
    day ends at 00:00 UTC in winter and 23:00 UTC in summer."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    # PR-ADS-176C: was ``services.country_evidence_service``, which only
    # re-exported these two from the spend service (PR-ADS-167A).
    from services import google_ads_spend_service as ces
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
# Production-shaped: 162 spend codes, lower-case lead countries
# ═════════════════════════════════════════════════════════════════════════════
KZ = ("2398", "KZ", "Kazakhstan")
GE = ("2268", "GE", "Georgia")
NO_CODE = ("9999", None, None)




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



if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
