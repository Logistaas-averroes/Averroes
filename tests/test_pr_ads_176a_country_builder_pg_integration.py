"""PR-ADS-176A — one rule per figure in the shared country builder.

Two pages show countries: Countries (``/api/geo``,
``services/country_evidence_service``) and Country Revenue & Spend (the mart's
country view over ``services/revenue_attribution_service``), which also feeds
Dashboard → Countries (``services/dashboard_countries_service``). Over the same
evidence they stated different things. Every case below seeds the real tables
through the real writers on PostgreSQL and calls the real builders; each
failed on origin/main before this PR.

1. The zero rule. Countries says a spend-less country is a measured ``$0``
   only when the PR-ADS-167A rule proves it; the mart and the Dashboard said
   ``$0`` for ANY country with no geo row whenever the geo read succeeded —
   while spend Google reported against a location no stored row names (an
   unlabelled code-less criterion, ``2997``) was in the window and could be
   its own. Pakistan (deals, never a geo row) read ``$0`` there.
2. A code-less criterion's spend was dropped before bucketing, so the country
   rows summed to less than the campaign total.
3. Countries outside the 96 listed markets were one number in the residual.
4. Each drawer listed a different population from its row.
5. The Dashboard's geo read had no account scope.
6. A status said "Customer proven" on a deal count.
7. The blocked card said "no canonical geo spend yet — run the Geo Sync" for
   the pre-morning-sync settle gap.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, OTHER_CUSTOMER, RATE, UK, _spend_rows, _today, _verify, _write_fx,
    _write_spend, live_db,
)
from tests.test_pr_ads_167a_settled_coverage_consumers_pg_integration import (  # noqa: E402
    _before_the_morning_sync, _closed_today_instant, _window_dates,
)

WINDOW = "last_6_months"
#: A criterion that carries no code on any row and that nothing (no owner
#: label) names: spend Google reported against a location no stored row names.
LOOSE = ("2997", None, None)
#: Google keys Jordan under its own criterion — but not lately.
JORDAN = ("2400", "JO", "Jordan")
#: A Google-named country outside the 96 listed markets.
FIJI = ("2242", "FJ", "Fiji")
#: The owner-labelled criterion Google reports without a country (Cuba).
CUBA_OWNER_LABELLED = ("2192", None, None)


# ── seed ────────────────────────────────────────────────────────────────────
def _seed(cluster, *, yesterday_geo=(), other_account_uk=None):
    """UK £100 on two days; ``yesterday_geo`` [(criterion, gbp)] more geo rows
    yesterday. Campaign spend equals the configured account's geo spend
    exactly (reconciled). Jordan's only geo row is 20 days before the window.
    Both ledgers verified over the window, fetched now (after yesterday ended)."""
    import db.writers as writers
    start, today = _window_dates(WINDOW)
    yesterday = today - timedelta(days=1)
    _write_fx(start - timedelta(days=30), today)
    days = [start + timedelta(days=3), yesterday]
    geo = [_spend_rows(UK, d, 100.0, campaign="111") for d in days]
    geo.append(_spend_rows(JORDAN, start - timedelta(days=20), 12.0, campaign="111"))
    extra = 0.0
    for criterion, gbp in yesterday_geo:
        geo.append(_spend_rows(criterion, yesterday, gbp, campaign="111"))
        extra += gbp
    if other_account_uk is not None:
        geo.append(_spend_rows(UK, yesterday, other_account_uk, campaign="999",
                               customer=OTHER_CUSTOMER))
    _write_spend(geo)
    campaign = [{"customer_id": CUSTOMER, "currency_code": "GBP",
                 "campaign_id": "111", "campaign_name": "brand - uk",
                 "spend_date": d,
                 "cost_micros": int(round((100.0 + (extra if d == yesterday else 0.0))
                                          * 1_000_000)),
                 "source_query_version": "t"} for d in days]
    assert writers.upsert_campaign_daily_spend(campaign) == len(campaign)
    total = sum(c["cost_micros"] for c in campaign)
    assert writers.upsert_spend_coverage(CUSTOMER, start, today, "verified",
                                         rows_written=len(campaign),
                                         cost_micros_total=total)
    _verify(start, today)
    return start, today, yesterday


def _deal(cluster, deal_id, country, *, close=None, google=True, amount=500.0):
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    _, today = _window_dates(WINDOW)
    close = close or (datetime.combine(today - timedelta(days=1), datetime.min.time(),
                                       tzinfo=timezone.utc) + timedelta(hours=12))
    if google:
        _insert(cluster, deal_id, close_date=close.isoformat(), amount=amount,
                revenue_usd=amount, country=country, gclid=f"g-{deal_id}",
                group="google_ads", attribution_status="google_ads")
    else:
        # Organic: no GCLID, no Google Ads acquisition group. All-source only.
        _insert(cluster, deal_id, close_date=close.isoformat(), amount=amount,
                revenue_usd=amount, country=country, group="organic",
                attribution_status="attributed")


def _sync(cluster):
    from tests.test_pr_ads_153e_b_pg_integration import _sync_state
    _sync_state(cluster)


def _mart():
    from services.revenue_decision_mart import build_revenue_decision_mart
    return build_revenue_decision_mart(view="country", window=WINDOW)


def _dash():
    from services.dashboard_countries_service import build_dashboard_countries
    return json.loads(json.dumps(build_dashboard_countries(WINDOW), default=str))


def _mart_row(mart, code):
    return next(r for r in mart["rows"] if r.get("country_code") == code)


def _dash_row(page, code):
    return next(r for r in page["countries"] if r.get("country_code") == code)


def _zero_rule_scenario(cluster):
    """Pakistan and Jordan each won a Google Ads deal yesterday; neither has a
    geo row in the window; £3 of spend sits on the unlabelled code-less 2997."""
    _seed(cluster, yesterday_geo=[(LOOSE, 3.0)])
    _sync(cluster)
    _deal(cluster, "d-pk", "Pakistan")
    _deal(cluster, "d-jo", "Jordan")
    _deal(cluster, "d-uk", "United Kingdom")


# ── 1. the zero rule, on both builders ──────────────────────────────────────
def test_01_a_never_seen_market_is_unavailable_while_code_less_spend_is_in_the_window(
        live_db, monkeypatch):
    """Pakistan: deals, never a Google Ads geo row, and £3 in the window on a
    criterion no stored row names. That spend may be Pakistan's: Unavailable,
    with the reason — never $0. Jordan's $0 stands: its code is carried on
    record by exactly one criterion, which carries nothing else."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _zero_rule_scenario(live_db)

    mart = _mart()
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    pk, jo, uk = (_mart_row(mart, c) for c in ("PK", "JO", "GB"))
    assert pk["spend"] is None, "Pakistan's spend is not proven to be $0"
    assert pk.get("spend_reason") == "spend_location_unresolved"
    assert pk["period_revenue_per_ad_spend"] is None
    assert (jo["spend"], jo.get("spend_reason")) == (0.0, None)
    assert uk["spend"] == pytest.approx(200.0 * RATE)
    assert pk["closed_won_deals"] == 1          # the outcome is unaffected


def test_01_b_the_dashboard_countries_payload_applies_the_same_rule(live_db, monkeypatch):
    """The same evidence through Dashboard → Countries, which re-read geo spend
    and called any market with no geo row a measured zero."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _zero_rule_scenario(live_db)
    page = _dash()
    pk, jo = _dash_row(page, "PK"), _dash_row(page, "JO")
    assert (pk["spend_native"], pk["spend_usd"]) == (None, None)
    assert pk.get("spend_reason") == "spend_location_unresolved"
    assert (jo["spend_native"], jo["spend_usd"], jo.get("spend_reason")) == (0.0, 0.0, None)
    # The listed markets' total is unknown while one market's spend is: withheld
    # with the reason, never a partial presented as the total.
    k = page["kpis"]
    assert k["verified_spend_native"]["amount"] is None
    assert k["verified_spend_usd"] is None
    assert k["verified_spend_reason"] == "spend_location_unresolved"
    assert k["zero_withheld_countries"] == ["Pakistan"]
    why = next(u for u in page["unavailable"] if u["metric"] == "verified_spend_usd")
    assert "Pakistan" in why["reason"] and "FX" not in why["reason"]


def test_02_with_nothing_unresolved_in_the_window_a_spend_less_market_is_a_measured_zero(
        live_db, monkeypatch):
    """The counterfactual: the same markets, no code-less spend in the window.
    The rule withholds only what it must. (Passes on origin/main too: it pins
    that the fix does not over-withhold.)"""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _sync(live_db)
    _deal(live_db, "d-pk", "Pakistan")
    mart = _mart()
    assert (_mart_row(mart, "PK")["spend"], _mart_row(mart, "PK").get("spend_reason")) == (
        0.0, None)
    page = _dash()
    assert _dash_row(page, "PK")["spend_native"] == 0.0
    assert page["kpis"]["verified_spend_native"]["amount"] == pytest.approx(200.0)


def test_03_one_rule_the_countries_page_and_both_builders_call_the_same_function(
        live_db, monkeypatch):
    """A mutation that removes the zero rule (the shared verdict always says
    "publishable") turns Pakistan back into a $0 on BOTH builders: the guard
    is the shared function, and its absence changes the output."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _zero_rule_scenario(live_db)
    from services import country_spend_zero_rule as zr
    monkeypatch.setattr(zr, "zero_spend_verdict", lambda evidence, code: (True, None))
    assert _mart_row(_mart(), "PK")["spend"] == 0.0
    assert _dash_row(_dash(), "PK")["spend_native"] == 0.0
    # And every builder imports it from one module, never a copy.
    for path in ("services/country_evidence_service.py",
                 "services/dashboard_countries_service.py",
                 "services/revenue_attribution_service.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "country_spend_zero_rule" in src, path
        assert "fetch_geo_code_carriers(" not in src, path


# ── 2/3. conservation, and the outside-market countries itemised ────────────
def test_04_rows_plus_residual_equal_the_campaign_total_with_code_less_and_outside_spend(
        live_db, monkeypatch):
    """UK £200, the code-less 2997 £3, Fiji (Google-named, not a listed market)
    £7, Cuba (owner-labelled) £2; campaign spend £212, reconciled exactly.
    Before: 2997's £3 was dropped before bucketing — rows + residual = £209."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db, yesterday_geo=[(LOOSE, 3.0), (FIJI, 7.0), (CUBA_OWNER_LABELLED, 2.0)])
    _sync(live_db)
    mart = _mart()
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    campaign_total_usd = 212.0 * RATE
    assert sum(r["spend"] for r in mart["rows"]) == pytest.approx(campaign_total_usd)
    residual = next(r for r in mart["rows"] if r["is_residual"])
    comp = residual["spend_components"]
    assert comp["outside_listed_markets"] == pytest.approx(9.0 * RATE)
    assert comp["country_unidentified"] == pytest.approx(3.0 * RATE)
    # Itemised: a breakdown of `outside_listed_markets`, never additional.
    items = residual["outside_listed_market_items"]
    assert [(i["country_code"], i["provenance"]) for i in items] == [
        ("CU", "owner_labelled"), ("FJ", "google_ads")]
    assert sum(i["spend"] for i in items) == pytest.approx(comp["outside_listed_markets"])
    fj = next(i for i in items if i["country_code"] == "FJ")
    assert fj["spend"] == pytest.approx(7.0 * RATE)
    # Their outcomes are not per country here: in the residual, never 0.
    assert (fj["leads"], fj["sqls"], fj["closed_won_deals"], fj["won_revenue"]) == (
        "in_residual",) * 4
    assert not any(r.get("country_code") in ("FJ", "CU") for r in mart["rows"]
                   if not r["is_residual"])

    page = _dash()
    items = page["residual"]["outside_listed_market_items"]
    assert [(i["country_code"], i["spend_native"], i["provenance"]) for i in items] == [
        ("CU", 2.0, "owner_labelled"), ("FJ", 7.0, "google_ads")]
    assert sum(i["spend_native"] for i in items) == pytest.approx(
        page["kpis"]["spend_outside_listed_markets"]["native"])
    assert items[1]["closed_won_deals"] == "in_residual"


# ── 4. one population per row and its drawer ─────────────────────────────────
def test_05_a_deal_closed_today_is_in_neither_the_country_row_nor_its_drawer(
        live_db, monkeypatch):
    """Country Revenue & Spend's row is the mart's settled-day view (it ends
    yesterday); its drawer read the window through today."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _sync(live_db)
    _deal(live_db, "d-yesterday", "United Kingdom")
    _deal(live_db, "d-today", "United Kingdom", close=_closed_today_instant())
    from services.revenue_attribution_service import build_country_deal_details
    row = _mart_row(_mart(), "GB")
    assert row["closed_won_deals"] == 1
    drawer = build_country_deal_details(WINDOW, "United Kingdom", "GB")
    assert [d["deal_record_id"] for d in drawer["details"]] == ["d-yesterday"]
    assert drawer["summary"]["closed_won_deals"] == 1
    assert row["deal_ids"] == ["d-yesterday"]
    page_row = _dash_row(_dash(), "GB")
    assert [d["deal_id"] for d in page_row["deals"]] == ["d-yesterday"]


def test_06_an_all_source_deal_does_not_inflate_the_dashboard_drawer(live_db, monkeypatch):
    """The Dashboard row counts the market's Google Ads-source deals; its
    drawer listed every won deal of the country, any channel."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _sync(live_db)
    _deal(live_db, "d-google", "United Kingdom")
    _deal(live_db, "d-organic", "United Kingdom", google=False, amount=900.0)
    page = _dash()
    uk = _dash_row(page, "GB")
    assert uk["closed_won_deals"] == 1
    assert [d["deal_id"] for d in uk["deals"]] == ["d-google"]
    # The revenue verdict's deal denominators come from the drawers.
    assert page["country_revenue_disclosure"]["closed_won_deals"] == 1


# ── 5. the Dashboard's geo read is the configured account's ──────────────────
def test_07_a_second_account_s_geo_spend_is_on_no_market_row(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db, other_account_uk=500.0)
    _sync(live_db)
    page = _dash()
    uk = _dash_row(page, "GB")
    assert uk["spend_native"] == pytest.approx(200.0)
    assert page["kpis"]["verified_spend_native"]["amount"] == pytest.approx(200.0)
    # Country Revenue & Spend's rows reconcile against the scoped geo total, so
    # they read the same account.
    assert _mart_row(_mart(), "GB")["spend"] == pytest.approx(200.0 * RATE)


# ── 6. no status says "customer" when it is computed from deals ──────────────
def test_08_no_country_status_names_customers():
    import services.dashboard_countries_service as d
    statuses = {v for k, v in vars(d).items()
                if k.startswith("STATUS_") and isinstance(v, str)}
    assert statuses and not any("customer" in s.lower() for s in statuses), statuses
    # A market with a won deal whose amount is unknown: deals, not customers.
    st = d._country_status(native_spend=10.0, usd_spend=12.5, fx_complete=True,
                           sqls=0, customers=1, won_revenue=None,
                           attribution_status="mapped")
    assert st == "Won deal, revenue not proven"
    st = d._country_status(native_spend=10.0, usd_spend=12.5, fx_complete=True,
                           sqls=0, customers=0, won_revenue=0.0,
                           attribution_status="mapped")
    assert st == "Spend without a won deal"
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    for s in statuses:
        if s in ("Country figures withheld",):
            continue
        assert f'"{s}":' in js, f"app.js has no colour / next action for {s!r}"
    assert '"Customer proven"' not in js


# ── 7. the blocked card names the settle gap ────────────────────────────────
def test_09_before_the_morning_sync_the_blocked_card_names_the_settle_gap(
        live_db, monkeypatch, tmp_path):
    """The real mart payload before this morning's sync, rendered by the real
    ``renderCountrySpendUnreconciledState``. It said "There is no canonical
    Google Ads geo spend for this window yet … Run Google Ads Geo Sync" —
    wrong on both counts: the spend is there, and nothing needs running."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _sync(live_db)
    _before_the_morning_sync(live_db)
    st = _mart()["spend_truth"]
    assert st["country_spend_status"] == "unavailable"
    assert "spend_not_reread_since_day_ended" in st["country_gap_codes"]
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    html = _run_app("renderCountrySpendUnreconciledState(%s)"
                    % json.dumps(json.loads(json.dumps(st, default=str))))
    assert "last synced while it was still in progress" in html
    assert "The morning sync re-reads it" in html
    assert "Nothing to run" in html
    assert "no canonical Google Ads geo spend" not in html
    assert "Run Google Ads Geo Sync" not in html

    # A window never synced at all still says to run the sync.
    never = {"country_spend_status": "unavailable",
             "country_gap_codes": ["geo_coverage_incomplete"],
             "country_gap_message": "part of this window was never synced"}
    html = _run_app("renderCountrySpendUnreconciledState(%s)" % json.dumps(never))
    assert "Run Google Ads Geo Sync" in html


# ── the shared read fails closed ─────────────────────────────────────────────
def test_10_a_snapshot_that_cannot_open_is_unreadable_geo_never_a_crash_or_a_zero(
        monkeypatch):
    """The geo snapshot is opened by every country builder (and by every mart
    view, through the revenue contract). A database that refuses the
    connection must read as unreadable geo — no zero proven — not a 500."""
    import contextlib

    from db import revenue_repository as repo
    from services import country_spend_zero_rule as zr

    @contextlib.contextmanager
    def refused():
        raise RuntimeError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(repo, "geo_read_snapshot", refused)
    out = zr.read_country_spend(date(2026, 1, 1), date(2026, 1, 31), CUSTOMER, ["PK"])
    assert out["spend"]["available"] is False and out["zero_rule"] is None
    assert zr.zero_spend_verdict(out["zero_rule"], "PK") == (False, "geo_rows_unreadable")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
