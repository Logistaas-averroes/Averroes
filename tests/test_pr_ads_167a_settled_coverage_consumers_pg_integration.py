"""PR-ADS-167A — one settle rule for every surface that publishes country spend.

PR-ADS-165A, known gaps 3/4. The Countries page counts a verified chunk only
for the days that had ENDED when it was fetched. Country Revenue & Spend (the
revenue attribution contract, its mart country view and the Dashboard
Countries tab) and the geo reconciliation panel judged the geo coverage ledger
on chunk status alone (``analyze_geo_coverage``). The daily sync marks its
chunk ``verified`` through TODAY at about 06:00 UTC, so those surfaces called a
window ending today — every open business window: current quarter, YTD, last
six months, all time — "covered" while its last day held six hours of spend,
and yesterday "covered" before the morning sync re-read it.

The rule now lives once, in ``google_ads_spend_service.analyze_settled_coverage``,
and ``analyze_published_geo_coverage`` applies it for every publishing geo
consumer. What each consumer now does with a window that ends today:

* Countries (the retired ``/api/geo`` feed, PR-ADS-176C): its window already
  ended yesterday; unchanged.
* Country Revenue & Spend / mart / Dashboard Countries / geo reconciliation:
  their business windows end today, and the page divides revenue THROUGH
  TODAY by this spend, so the spend cannot end a day earlier. The verdict is
  not covered — gap ``window_includes_day_in_progress`` — and country spend
  and its period ratios are withheld for an open window. A closed window
  (last quarter) re-read after it ended publishes.

The sync's own completeness check keeps asking "was every day fetched?"
(``analyze_geo_coverage``), and the repair job likewise: neither publishes.

Also here: Country Revenue & Spend and Dashboard Countries read
``fetch_geo_daily_spend_by_country``, which named each criterion by
``MAX(country_code)`` of the WINDOW's rows. A criterion whose rows in the window
were written while the lookup was failing had no name, and its spend dropped
out of its country. It now carries the same read-time criterion identity as
the Countries page, in the same statement as the rows.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, RATE, UK, _spend_rows, _today, _verify, _write_fx, _write_spend,
    live_db,
)

LONDON = ZoneInfo("Europe/London")


# ── the shared rule, pure ───────────────────────────────────────────────────
def _chunk(start, end, status="verified", fetched=None):
    return {"chunk_start": start.isoformat(), "chunk_end": end.isoformat(),
            "status": status, "fetched_at": fetched}


def test_01_the_shared_rule_names_every_way_a_fetched_window_is_not_complete():
    from services.google_ads_spend_service import analyze_settled_coverage as settle
    now = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    today = date(2026, 11, 19)
    this_morning = datetime(2026, 11, 19, 6, 2, tzinfo=timezone.utc)
    yesterday_morning = this_morning - timedelta(days=1)

    # Fetched this morning through today: complete through yesterday only.
    daily = [_chunk(today - timedelta(days=6), today, fetched=this_morning)]
    closed = settle(today - timedelta(days=6), today - timedelta(days=1), daily,
                    LONDON, now=now)
    assert (closed["complete"], closed["unsettled_reason"]) == (True, None)
    open_ = settle(today - timedelta(days=6), today, daily, LONDON, now=now)
    assert (open_["complete"], open_["complete_by_status"],
            open_["unsettled_reason"]) == (False, True, "window_includes_day_in_progress")

    # Before this morning's sync: yesterday holds only what yesterday's saw.
    stale = [_chunk(today - timedelta(days=7), today - timedelta(days=1),
                    fetched=yesterday_morning)]
    v = settle(today - timedelta(days=7), today - timedelta(days=1), stale, LONDON, now=now)
    assert (v["complete"], v["unsettled_reason"]) == (False, "spend_not_reread_since_day_ended")
    # …and this morning's sync of those days FAILED: say so.
    failed = stale + [_chunk(today - timedelta(days=6), today, status="failed")]
    v = settle(today - timedelta(days=7), today - timedelta(days=1), failed, LONDON, now=now)
    assert v["unsettled_reason"] == "spend_reread_failed"
    # No zone on record: nothing is settled.
    v = settle(today - timedelta(days=6), today - timedelta(days=1), daily, None, now=now)
    assert (v["complete"], v["unsettled_reason"]) == (False, "account_time_zone_unknown")
    # A day never fetched is an ordinary gap, not an unsettled one.
    v = settle(today - timedelta(days=20), today - timedelta(days=1), daily, LONDON, now=now)
    assert (v["complete"], v["unsettled_reason"]) == (False, None)
    # A chunk with no fetch time settles nothing — never "covered by default".
    v = settle(today - timedelta(days=6), today - timedelta(days=1),
               [_chunk(today - timedelta(days=6), today)], LONDON, now=now)
    assert v["complete"] is False


def test_02_there_is_one_settle_rule_and_every_publishing_geo_reader_uses_it():
    # PR-ADS-176C: the first half of this case pinned that the Countries
    # evidence page (services/country_evidence_service, /api/geo) re-exported
    # the rule rather than holding a copy. That page and its feed are retired;
    # the property — ONE definition of the rule — is held below: it is defined
    # once, in the spend service, and nowhere else in production code.
    import services.google_ads_spend_service as spend
    assert callable(spend.settled_through) and callable(spend.settled_chunks)
    defs = sorted(
        str(p.relative_to(ROOT)) for pkg in ("analysis", "api", "db", "scheduler",
                                             "services", "scripts", "connectors")
        for p in (ROOT / pkg).rglob("*.py") if "__pycache__" not in p.parts
        and ("def settled_through" in p.read_text(encoding="utf-8")
             or "def settled_chunks" in p.read_text(encoding="utf-8")))
    assert defs == ["services/google_ads_spend_service.py"]
    # Every PUBLISHING geo reader applies it; the sync's own check does not.
    geo_src = (ROOT / "services" / "google_ads_geo_sync_service.py").read_text(encoding="utf-8")
    recon = geo_src[geo_src.index("def build_geo_reconciliation"):]
    recon = recon[:recon.index("\ndef ")]
    assert "analyze_published_geo_coverage(" in recon
    assert "analyze_geo_coverage(" not in recon
    rev = (ROOT / "services" / "revenue_attribution_service.py").read_text(encoding="utf-8")
    reader = rev[rev.index("def _read_geo_coverage_ledger"):]
    reader = reader[:reader.index("\ndef ")]
    assert "analyze_published_geo_coverage(" in reader
    assert "analyze_geo_coverage(" not in reader


# ── the consumers, on PostgreSQL ────────────────────────────────────────────
def _seed_window(cluster, start: date, end: date, *, days: list):
    """Campaign spend and geo spend that reconcile exactly, on ``days``; both
    coverage ledgers verified over [start, end] (stamped now); FX every day."""
    import db.writers as writers
    _write_fx(start, end)
    campaign = []
    geo = []
    for d in days:
        campaign.append({"customer_id": CUSTOMER, "currency_code": "GBP",
                         "campaign_id": "111", "campaign_name": "brand - uk",
                         "spend_date": d, "cost_micros": 100_000_000,
                         "source_query_version": "t"})
        geo.append(_spend_rows(UK, d, 100.0, campaign="111"))
    assert writers.upsert_campaign_daily_spend(campaign) == len(campaign)
    _write_spend(geo)
    assert writers.upsert_spend_coverage(CUSTOMER, start, end, "verified",
                                         rows_written=len(days),
                                         cost_micros_total=100_000_000 * len(days))
    _verify(start, end)


def _quarter_bounds(window: str):
    from analysis.business_windows import resolve_window_in_zone
    w = resolve_window_in_zone(window, "Europe/London")
    return date.fromisoformat(w["start_date"]), date.fromisoformat(w["end_date"])


def _window_dates(window: str):
    from services.canonical_contract import resolve_canonical_window
    w = resolve_canonical_window(window)
    return (date.fromisoformat(w["start_date"]) if w["start_date"] else None,
            date.fromisoformat(w["end_date"]))


def _before_the_morning_sync(cluster, table="google_ads_geo_coverage"):
    """Every chunk last fetched at 06:02 UTC YESTERDAY: today's sync has not
    run, so yesterday was last read while it was still in progress."""
    yesterday = _today() - timedelta(days=1)
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE {table} SET fetched_at = %s",
                    (datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc)
                     + timedelta(hours=6, minutes=2),))
        conn.commit()


def test_03_an_open_window_ends_yesterday_and_is_covered_after_the_morning_sync(
        live_db, monkeypatch):
    """The geo reconciliation panel (`/api/google-ads-geo-reconcile`). Owner
    decision 2026-11-20: a window that includes today ends YESTERDAY for the
    country consumers, so after the morning sync it is covered — and today's
    part-day spend is not in it."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    from services.google_ads_geo_sync_service import (
        build_geo_reconciliation, describe_geo_gap,
    )
    start, today = _window_dates("last_6_months")
    assert today == _today()
    lq_start, lq_end = _quarter_bounds("last_quarter")
    yesterday = today - timedelta(days=1)
    _seed_window(live_db, start, today,
                 days=[lq_start + timedelta(days=3), yesterday, today])

    open_ = build_geo_reconciliation("last_6_months")
    assert (open_["date_from"], open_["date_to"]) == (start.isoformat(),
                                                      yesterday.isoformat())
    assert open_["through_reason"] == "today_not_settled"
    assert open_["window_range"]["through"] == yesterday.isoformat()
    # Today's £100 is not in either total: both sides end yesterday.
    assert open_["canonical_campaign_total"] == pytest.approx(200.0)
    assert open_["geo_total"] == pytest.approx(200.0)
    assert (open_["geo_ready"], open_["geo_coverage_complete"],
            open_["geo_coverage_unsettled_reason"], open_["geo_gap_codes"]) == (
        True, True, None, [])

    closed = build_geo_reconciliation("last_quarter")
    assert closed["through_reason"] is None
    assert closed["date_to"] == lq_end.isoformat()
    assert (closed["geo_ready"], closed["geo_coverage_complete"],
            closed["geo_gap_codes"]) == (True, True, [])

    # Before this morning's sync: yesterday was last read in progress. Not
    # covered, and the existing reason says so — never "covered".
    _before_the_morning_sync(live_db)
    early = build_geo_reconciliation("last_6_months")
    assert early["date_to"] == yesterday.isoformat()
    assert early["geo_ready"] is False
    assert early["geo_coverage_unsettled_reason"] == "spend_not_reread_since_day_ended"
    assert "spend_not_reread_since_day_ended" in early["geo_gap_codes"]
    assert "The morning sync re-reads it" in describe_geo_gap(early["geo_gap_codes"])


def test_04_a_closed_window_last_fetched_before_its_last_day_ended_is_not_covered(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    from services.google_ads_geo_sync_service import build_geo_reconciliation
    lq_start, lq_end = _quarter_bounds("last_quarter")
    _seed_window(live_db, lq_start, lq_end, days=[lq_start, lq_end])
    # The ledger as it stands if the last re-read of the quarter's final day
    # was its own morning sync: fetched at 06:05 on that day, never again.
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE google_ads_geo_coverage SET fetched_at = %s",
                    (datetime.combine(lq_end, datetime.min.time(), tzinfo=timezone.utc)
                     + timedelta(hours=6, minutes=5),))
        conn.commit()
    out = build_geo_reconciliation("last_quarter")
    assert out["geo_ready"] is False
    assert out["geo_coverage_unsettled_reason"] == "spend_not_reread_since_day_ended"
    assert "spend_not_reread_since_day_ended" in out["geo_gap_codes"]


def test_05_country_revenue_and_spend_ends_an_open_window_yesterday_for_both_sides(
        live_db, monkeypatch):
    """The revenue attribution contract over the settled-day window, read by
    the mart's country view (Country Revenue & Spend, Dashboard Countries).
    Every other caller keeps its window — the campaign view still ends today."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    from services.revenue_attribution_service import build_revenue_attribution
    from services.revenue_decision_mart import build_revenue_decision_mart
    start, today = _window_dates("last_6_months")
    yesterday = today - timedelta(days=1)
    _seed_window(live_db, start, today, days=[start + timedelta(days=3), yesterday, today])

    settled = build_revenue_attribution("last_6_months", settled_days=True)
    assert settled["window"]["end_date"] == yesterday.isoformat()
    assert settled["window"]["through_reason"] == "today_not_settled"
    sh = settled["source_health"]
    assert (sh["geo_coverage_status"], sh["geo_coverage_unsettled_reason"],
            sh["country_roas_available"]) == ("complete", None, True)

    # The default contract (`/api/revenue-attribution`, Campaign Revenue &
    # Spend) is unchanged: its window ends today and its country spend stays
    # withheld for the day in progress.
    default = build_revenue_attribution("last_6_months")
    assert default["window"]["end_date"] == today.isoformat()
    assert default["window"].get("through_reason") is None
    assert default["source_health"]["geo_coverage_unsettled_reason"] == (
        "window_includes_day_in_progress")

    country = build_revenue_decision_mart(view="country", window="last_6_months")
    w = country["window"]
    assert (w["start_date"], w["end_date"], w["through_reason"]) == (
        start.isoformat(), yesterday.isoformat(), "today_not_settled")
    assert w["window_range"]["disclosure"].startswith(
        f"Through {yesterday.day} {yesterday.strftime('%b')} {yesterday.year} — "
        "today is not included until tomorrow morning's sync.")
    assert country["spend_truth"]["country_roas_unblockable"] is True
    assert country["spend_truth"]["country_gap_codes"] == []
    campaign = build_revenue_decision_mart(view="campaign", window="last_6_months")
    assert campaign["window"]["end_date"] == today.isoformat()
    assert campaign["window"]["through_reason"] is None

    closed = build_revenue_attribution("last_quarter", settled_days=True)
    assert closed["window"]["through_reason"] is None
    assert closed["source_health"]["geo_coverage_status"] == "complete"
    assert closed["source_health"]["country_roas_available"] is True

    _before_the_morning_sync(live_db)
    early = build_revenue_decision_mart(view="country", window="last_6_months")
    assert early["window"]["end_date"] == yesterday.isoformat()
    assert early["spend_truth"]["country_roas_unblockable"] is False
    assert "spend_not_reread_since_day_ended" in early["spend_truth"]["country_gap_codes"]


def test_06_country_spend_readers_name_a_criterion_by_its_identity_on_record(
        live_db, monkeypatch):
    """The window's rows of the UAE were written while the lookup was failing
    (before PR-ADS-167A's sync fix, or by any writer given no code); an older
    row names the criterion. MAX(country_code) over the window said NULL, and
    Country Revenue & Spend dropped the UAE's spend from the UAE."""
    from db import revenue_repository as repo
    today = _today()
    end = today - timedelta(days=1)
    _write_fx(end - timedelta(days=40), end)
    _write_spend([
        _spend_rows(("2784", "AE", "United Arab Emirates"), end - timedelta(days=30), 8.0),
        _spend_rows(("2784", None, None), end, 50.0),
        _spend_rows(UK, end, 100.0),
        # One criterion stored under two codes names nothing.
        _spend_rows(("2999", "FR", "France"), end - timedelta(days=35), 1.0),
        _spend_rows(("2999", "DE", "Germany"), end, 30.0),
        # No code on any row, and not owner-labelled (PR-ADS-170A labelled
        # production's 2192, which this fixture used; the reader's rule for an
        # unnamed criterion is what is asserted, so the id is an unlabelled one).
        _spend_rows(("2997", None, None), end, 0.45),
    ])
    out = repo.fetch_geo_daily_spend_by_country(end - timedelta(days=6), end)
    by = {r["country_criterion_id"]: r for r in out["rows"]}
    assert (by["2784"]["country_code"], by["2784"]["country_name"]) == (
        "AE", "United Arab Emirates")
    assert by["2784"]["spend"] == 50.0                  # only the window's spend
    assert (by["2999"]["country_code"], by["2999"]["identity_codes"]) == (None, ["DE", "FR"])
    assert (by["2997"]["country_code"], by["2997"]["identity_codes"]) == (None, [])
    assert by[UK[0]]["country_code"] == "GB"
    assert out["total_spend"] == pytest.approx(180.45)

    # Dashboard Countries' map keys the UAE's spend on the UAE.
    from services.dashboard_countries_service import _geo_spend_by_country
    maps = _geo_spend_by_country(out)
    from analysis import country_identity
    ae = country_identity.resolve(code="AE").key
    assert maps["by_key"][ae]["native"] == 50.0
    # The unnamed criteria stay in the residual — real spend, no country.
    assert maps["residual"]["native"] == pytest.approx(30.45)


def test_07_the_sync_s_own_completeness_check_is_not_the_publication_rule(
        live_db, monkeypatch):
    """A sync that fetched through today has done its job: it asks "was every
    day fetched?", not "is every day over?". Otherwise every daily run would
    be `partial` forever."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    from services.google_ads_geo_sync_service import (
        analyze_geo_coverage, analyze_published_geo_coverage,
    )
    today = _today()
    _verify(today - timedelta(days=6), today)
    fetched = analyze_geo_coverage(CUSTOMER, today - timedelta(days=6), today)
    published = analyze_published_geo_coverage(CUSTOMER, today - timedelta(days=6), today)
    assert fetched["complete"] is True
    assert (published["complete"], published["unsettled_reason"]) == (
        False, "window_includes_day_in_progress")
    through_yesterday = analyze_published_geo_coverage(
        CUSTOMER, today - timedelta(days=6), today - timedelta(days=1))
    assert through_yesterday["complete"] is True


def _closed_today_instant() -> datetime:
    """An instant TODAY (account-local) that is already past: half way between
    the account midnight and now."""
    now = datetime.now(tz=timezone.utc)
    midnight = datetime.combine(_today(), datetime.min.time(),
                                tzinfo=LONDON).astimezone(timezone.utc)
    return midnight + (now - midnight) / 2


def test_08_a_deal_closed_today_is_on_the_deals_page_and_not_on_the_country_pages(
        live_db, monkeypatch):
    """Owner decision 2026-11-20, end to end on PostgreSQL. Revenue, deals and
    spend on Country Revenue & Spend and Dashboard Countries end at yesterday
    together; the Deals page keeps today's deal; the window line says so."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    from tests.test_pr_ads_153e_b_pg_integration import _insert, _sync_state
    from services.dashboard_countries_service import build_dashboard_countries
    from services.revenue_attribution_service import build_revenue_deals
    from services.revenue_decision_mart import build_revenue_decision_mart
    from services import canonical_revenue_service as crs

    start, today = _window_dates("last_6_months")
    yesterday = today - timedelta(days=1)
    _seed_window(live_db, start, today, days=[start + timedelta(days=3), yesterday, today])
    _sync_state(live_db)
    earlier = datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc) \
        + timedelta(hours=12)
    _insert(live_db, "d-yesterday", close_date=earlier.isoformat(),
            amount=1000.0, revenue_usd=1000.0, country="United Kingdom",
            gclid="gclid-y", group="google_ads", attribution_status="google_ads")
    _insert(live_db, "d-today", close_date=_closed_today_instant().isoformat(),
            amount=500.0, revenue_usd=500.0, country="United Kingdom",
            gclid="gclid-t", group="google_ads", attribution_status="google_ads")

    # The Deals page: every won deal in the window, today's included.
    deals = build_revenue_deals("last_6_months")
    assert sorted(d["deal_id"] for d in deals["deals"]) == ["d-today", "d-yesterday"]
    # The reader contract over the same days, for the reference.
    from services.canonical_contract import resolve_settled_window, settled_revenue_bounds
    settled = crs.load_won_deals(
        "last_6_months",
        bounds=settled_revenue_bounds(resolve_settled_window("last_6_months")))
    assert [d["deal_id"] for d in settled["deals"]] == ["d-yesterday"]

    country = build_revenue_decision_mart(view="country", window="last_6_months")
    s = country["summary"]
    assert s["closed_won_deals"] == 1
    assert s["won_revenue_usd"] == pytest.approx(1000.0)
    assert country["window"]["window_range"]["disclosure"].endswith(
        "Other revenue pages, such as Deals, already count deals closed today.")
    # The campaign view keeps today: one more deal than the country view.
    campaign = build_revenue_decision_mart(view="campaign", window="last_6_months")
    assert campaign["summary"]["closed_won_deals"] == 2

    page = build_dashboard_countries("last_6_months")
    assert page["window"]["end_date"] == yesterday.isoformat()
    assert page["window"]["through_reason"] == "today_not_settled"
    k = page["kpis"]
    # After the morning sync the spend is published for the open window: the
    # card no longer reads "withheld".
    assert k["verified_spend_native"]["amount"] == pytest.approx(200.0)
    assert k["verified_spend_usd"] == pytest.approx(200.0 * RATE)
    assert k["closed_won_deals"] == 1
    assert k["won_revenue_usd"] == pytest.approx(1000.0)
    assert page["metric_truth"]["country_attributed_spend_usd"]["truth_status"] == "ready"
    uk = next(c for c in page["countries"] if c.get("country_code") == "GB")
    assert uk["period_revenue_per_ad_spend"] == pytest.approx(1000.0 / (200.0 * RATE), rel=1e-3)

    # Last Quarter is unchanged: closed, no through_reason, ends on its last day.
    lq = build_dashboard_countries("last_quarter")
    assert lq["window"]["through_reason"] is None
    assert lq["window"]["end_date"] == _quarter_bounds("last_quarter")[1].isoformat()

    # Before the morning sync, the same open window: revenue still ends
    # yesterday and publishes; country spend is withheld with the reason.
    _before_the_morning_sync(live_db)
    early = build_dashboard_countries("last_6_months")
    assert early["window"]["end_date"] == yesterday.isoformat()
    assert early["kpis"]["verified_spend_usd"] is None
    assert early["kpis"]["verified_spend_native"]["amount"] is None
    assert early["metric_truth"]["country_attributed_spend_usd"]["unavailable_reason"] == (
        "spend_not_reread_since_day_ended")


# ── the account calendar, and windows with no settled day (review round 1) ──
def _seed_q3_boundary(live_db):
    """Reviewer probe (audit167a/test_probe_pg): Q3 2026 spend and three deals
    around the 30 Sep / 1 Oct boundary."""
    from tests.test_pr_ads_153e_b_pg_integration import _insert, _sync_state
    _seed_window(live_db, date(2026, 6, 25), date(2026, 10, 2),
                 days=[date(2026, 8, 10), date(2026, 9, 30), date(2026, 10, 1)])
    _sync_state(live_db)
    for deal_id, close, amount in (
            ("d-aug", "2026-08-12T12:00:00+00:00", 7000.0),
            ("d-sep30", "2026-09-30T12:00:00+00:00", 1000.0),
            ("d-oct1", "2026-10-01T09:00:00+00:00", 500.0),
            # 00:30 BST on 1 Jul: the quarter's first day on the account
            # calendar, 30 Jun on the UTC calendar.
            ("d-jul1-0030bst", "2026-06-30T23:30:00+00:00", 300.0),
            # 00:20 BST on 16 Jul.
            ("d-jul16-0020bst", "2026-07-15T23:20:00+00:00", 200.0)):
        _insert(live_db, deal_id, close_date=close, amount=amount, revenue_usd=amount,
                country="United Kingdom", gclid=f"g-{deal_id}", group="google_ads",
                attribution_status="google_ads")


def _assert_nothing_measured(mart, page):
    reason = "no_settled_day_in_window"
    w = mart["window"]
    assert w["window_range"]["empty"] is True and w["through_reason"] == "today_not_settled"
    assert w["window_range"]["disclosure"].startswith("No completed day in this window yet")
    s = mart["summary"]
    for k in ("closed_won_deals", "won_revenue_usd", "spend_usd", "sqls", "leads",
              "google_ads_source_closed_won_deals", "google_ads_source_won_revenue_usd"):
        assert s[k] is None, (k, s[k])
    assert mart["rows"] == []
    st = mart["spend_truth"]
    assert (st["country_roas_unblockable"], st["country_spend_status"],
            st["country_gap_codes"]) == (False, "unavailable", [reason])
    assert mart["readiness"]["country_decision_ready"] is False
    k = page["kpis"]
    for key in ("verified_spend_usd", "won_revenue_usd", "closed_won_deals", "sqls",
                "geo_period_revenue_per_ad_spend"):
        assert k[key] is None, (key, k[key])
    assert k["verified_spend_native"]["amount"] is None
    assert page["countries"] == []
    for m in ("country_attributed_spend_usd", "country_attributed_won_revenue_usd",
              "country_attributed_closed_won_deals", "country_attributed_sqls"):
        mt = page["metric_truth"][m]
        assert (mt["truth_status"], mt["unavailable_reason"]) == ("not_ready", reason), m


def test_09_first_hour_of_a_quarter_under_bst_publishes_none_of_last_quarter(
        live_db, monkeypatch):
    """Reviewer p2. 23:30 UTC on 30 Sep is 00:30 BST on 1 Oct: the current
    quarter (Q4) has no settled day. Before the fix the reader resolved the
    key on the UTC day (Q3) and published 2 deals / $8,000 beside £0 spend."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    from services.dashboard_countries_service import build_dashboard_countries
    from services.revenue_decision_mart import build_revenue_decision_mart
    _seed_q3_boundary(live_db)
    now = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)
    _assert_nothing_measured(
        build_revenue_decision_mart(view="country", window="current_quarter", now=now),
        build_dashboard_countries("current_quarter", now=now))


def test_10_the_first_day_of_a_quarter_publishes_no_verified_zero(live_db, monkeypatch):
    """Reviewer p1. Noon on 1 Oct: zero settled days. Before the fix: deals 0,
    revenue 0, spend 0 and country_roas_unblockable True — "verified" zeros
    over no day."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    from services.dashboard_countries_service import build_dashboard_countries
    from services.google_ads_geo_sync_service import build_geo_reconciliation
    from services.revenue_decision_mart import build_revenue_decision_mart
    _seed_q3_boundary(live_db)
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    _assert_nothing_measured(
        build_revenue_decision_mart(view="country", window="current_quarter", now=now),
        build_dashboard_countries("current_quarter", now=now))
    panel = build_geo_reconciliation("current_quarter", now=now)
    assert (panel["geo_ready"], panel["status"], panel["geo_gap_codes"],
            panel["canonical_campaign_total"]) == (
        False, "unavailable", ["no_settled_day_in_window"], None)


def test_11_revenue_days_are_account_days(live_db, monkeypatch):
    """The BST cases on PostgreSQL. At 12:00 UTC on 16 Jul the current quarter
    runs 1–15 Jul on the account calendar: the deal at 00:30 BST on 1 Jul is
    in; the deal at 00:20 BST on 16 Jul (today) is out — and both sides of the
    window are the same days as the settled spend."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    from services.dashboard_countries_service import build_dashboard_countries
    from services.revenue_decision_mart import build_revenue_decision_mart
    _seed_q3_boundary(live_db)
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    mart = build_revenue_decision_mart(view="country", window="current_quarter", now=now)
    w = mart["window"]
    assert (w["start_date"], w["end_date"]) == ("2026-07-01", "2026-07-15")
    assert (w["start_utc"], w["end_utc_exclusive"]) == (
        "2026-06-30T23:00:00+00:00", "2026-07-15T23:00:00+00:00")
    assert mart["summary"]["closed_won_deals"] == 1
    assert mart["summary"]["won_revenue_usd"] == pytest.approx(300.0)
    page = build_dashboard_countries("current_quarter", now=now)
    assert page["kpis"]["closed_won_deals"] == 1
    assert page["kpis"]["won_revenue_usd"] == pytest.approx(300.0)


def test_12_the_countries_geo_reads_share_one_snapshot(live_db):
    """Final review C (audit167a/test_probe_snap). Inside
    ``repo.geo_read_snapshot()`` a sync committing a code for a code-less
    criterion on another connection is NOT seen by the identities or carriers reads —
    the case that could turn "Unavailable" into a false $0 — and is seen
    after. An error inside makes the readers unavailable (fail closed); a
    write inside is refused (READ ONLY).

    PR-ADS-170A: the criterion was production's 2192, which the owner has
    since labelled Cuba — it would now carry CU before any commit, and the
    test would no longer show what the snapshot hides. The criterion is an
    unlabelled one; a carrier now also states its provenance."""
    from db import revenue_repository as repo
    d = _today() - timedelta(days=3)
    _write_fx(d - timedelta(days=2), d + timedelta(days=1))
    _write_spend([_spend_rows(("2997", None, None), d, 3.0)])
    with repo.geo_read_snapshot():
        before = repo.fetch_geo_criterion_identities(["2997"])
        # Another connection commits a row naming the criterion as Cuba.
        _write_spend([_spend_rows(("2997", "CU", "Cuba"), d - timedelta(days=1), 1.0)],
                     run="sync-2")
        carriers_inside = repo.fetch_geo_code_carriers(["CU"])
        identities_inside = repo.fetch_geo_criterion_identities(["2997"])
    after = repo.fetch_geo_code_carriers(["CU"])
    assert before["identities"]["2997"]["codes"] == []
    assert identities_inside["identities"]["2997"]["codes"] == []
    assert carriers_inside == {"available": True, "carriers": {}}
    assert after["carriers"]["CU"] == [
        {"criterion_id": "2997", "codes": ["CU"], "provenance": "google_ads"}]

    with repo.geo_read_snapshot():
        conn = repo._GEO_SNAPSHOT_CONN.get()
        with pytest.raises(Exception):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM no_such_table")
        assert repo.fetch_geo_code_carriers(["CU"])["available"] is False
        assert repo.fetch_geo_criterion_identities(["2997"])["available"] is False

    import psycopg2
    with pytest.raises(psycopg2.Error, match="read-only"):
        with repo.geo_read_snapshot():
            conn = repo._GEO_SNAPSHOT_CONN.get()
            with conn.cursor() as cur:
                cur.execute("CREATE TEMP TABLE zz (x int)")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
