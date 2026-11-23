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
import re
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
    # Truth audit M1: FX is verified here, and native spend is withheld too. No
    # card and no unavailable reason may blame FX or say native spend is shown.
    assert _mart()["spend_truth"]["fx_status"] == "verified"
    texts = ([c["headline"] + " " + c["body"] for c in page["decision_cards"]]
             + [u["reason"] for u in page["unavailable"]])
    for t in texts:
        assert not re.search(r"\bfx\b", t, re.I), t
        assert not re.search(r"native[^.]*(still )?shown", t, re.I), t
    assert any("Pakistan" in t for t in texts if "period revenue" in t.lower()), texts


def test_01_c_the_spend_metric_contract_names_the_zero_rule(live_db, monkeypatch):
    """Truth audit MINOR 3: in a ready window the zero rule withholds the
    listed markets' spend; the metric contract must say why, so the KPI card
    and the parity audit name the cause rather than none."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _zero_rule_scenario(live_db)
    page = _dash()
    mt = page["metric_truth"]["country_attributed_spend_usd"]
    assert (mt["truth_status"], mt.get("unavailable_reason")) == (
        "not_ready", "spend_location_unresolved")


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
    st_js = json.dumps(json.loads(json.dumps(st, default=str)))
    # Truth audit MINOR 1: as an ADMIN, who is the one offered the run button,
    # and case-insensitively — the CTA button reads "Run Google Ads Geo Sync",
    # a non-admin's note "Ask an admin to run Google Ads Geo Sync".
    for user in ('{"username": "a", "role": "admin"}', '{"username": "v", "role": "viewer"}'):
        html = _run_app("_currentUser = %s; renderCountrySpendUnreconciledState(%s)"
                        % (user, st_js))
        assert "last synced while it was still in progress" in html
        assert "The morning sync re-reads it" in html
        assert "Nothing to run" in html
        assert "no canonical google ads geo spend" not in html.lower()
        assert "run google ads geo sync" not in html.lower(), user
        assert 'data-country-cta="run-' not in html, user

    # A window never synced at all still says to run the sync — and offers it.
    never = {"country_spend_status": "unavailable",
             "country_gap_codes": ["geo_coverage_incomplete"],
             "country_gap_primary": "geo_coverage_incomplete",
             "country_gap_message": "part of this window was never synced"}
    html = _run_app('_currentUser = {"role": "admin"}; '
                    "renderCountrySpendUnreconciledState(%s)" % json.dumps(never))
    assert "Run Google Ads Geo Sync" in html
    assert 'data-country-cta="run-geo-sync"' in html


def test_09_b_the_cause_and_the_next_step_come_from_the_same_gap(tmp_path):
    """Truth audit MINOR 1: with two gaps the cause was the gate's FIRST
    (campaign coverage) and the next step the settle gap's "Nothing to run" —
    a card that names a gap and then says to wait for another. Both now come
    from the one primary gap the server names."""
    from services import google_ads_geo_sync_service as geo
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    codes = ["campaign_coverage_incomplete", "spend_not_reread_since_day_ended"]
    st = {"country_spend_status": "unavailable", "country_gap_codes": codes,
          "country_gap_message": geo.describe_geo_gap(codes),
          "country_gap_primary": getattr(geo, "primary_geo_gap", lambda c: None)(codes)}
    assert st["country_gap_primary"] == "campaign_coverage_incomplete"
    html = _run_app('_currentUser = {"role": "admin"}; '
                    "renderCountrySpendUnreconciledState(%s)" % json.dumps(st))
    assert "complete canonical campaign spend coverage" in html
    assert "Nothing to run" not in html
    assert "Spend Backfill" in html


# ── truth audit, round 1 ─────────────────────────────────────────────────────
def _residual(mart):
    return next(r for r in mart["rows"] if r["is_residual"])


def test_11_the_residual_row_never_publishes_a_period_ratio(live_db, monkeypatch):
    """Truth audit M2. The residual row holds spend no market can be said to own
    and deals no market can be said to have won: a ratio between them divides
    two unrelated populations. £3 code-less spend beside a $5,000 deal whose CRM
    country is "Atlantis" published 1333.33; with no residual revenue, 0.0."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db, yesterday_geo=[(LOOSE, 3.0)])
    _sync(live_db)
    _deal(live_db, "d-uk", "United Kingdom")
    res = _residual(_mart())
    assert res["spend"] == pytest.approx(3.0 * RATE)
    assert res["period_revenue_per_ad_spend"] is None            # was 0.0
    m = res["metrics"]["period_revenue_per_ad_spend"]
    assert (m["value"], m["status"], m["reason"]) == (None, "unavailable",
                                                      "residual_not_a_market")
    _deal(live_db, "d-atlantis", "Atlantis", amount=5000.0)
    res = _residual(_mart())
    assert res["closed_won_deals"] == 1 and res["won_revenue"] == 5000.0
    assert res["period_revenue_per_ad_spend"] is None            # was 1333.33
    m = res["metrics"]["period_revenue_per_ad_spend"]
    assert (m["status"], m["reason"]) == ("unavailable", "residual_not_a_market")
    assert res["metrics"]["period_spend_per_customer_won"]["status"] == "unavailable"


def _lead(cluster, contact_id, country):
    """One paid-search lead created yesterday, not qualified (no SQL)."""
    _, today = _window_dates(WINDOW)
    created = datetime.combine(today - timedelta(days=1), datetime.min.time(),
                               tzinfo=timezone.utc) + timedelta(hours=10)
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO runs (run_type, started_at, status) VALUES "
                    "('daily', now(), 'success') RETURNING id")
        run_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO leads (run_id, run_date, contact_id, country, "
            "status_category, campaign_name, keyword, source_type, "
            "contact_created_at) VALUES (%s, CURRENT_DATE, %s, %s, 'junk', "
            "'brand - uk', 'tms', 'paid_search', %s)",
            (run_id, contact_id, country, created))
        conn.commit()


def test_12_regional_mix_never_states_a_zero_over_an_unknown_spend(live_db, monkeypatch):
    """Truth audit M3(a). Pakistan (a lead, no SQL, no deal, never a geo row)
    has its spend withheld by the zero rule: APAC — Pakistan alone — read
    "No spend in window", and the KPI counted 0 countries with spend over rows
    whose spend is unknown."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db, yesterday_geo=[(LOOSE, 3.0)])
    _sync(live_db)
    _lead(live_db, "c-pk", "Pakistan")
    page = _dash()
    pk = _dash_row(page, "PK")
    assert (pk["spend_native"], pk["closed_won_deals"], pk["sqls"]) == (None, 0, 0)
    apac = next(r for r in page["regional_mix"] if r["region"] == "APAC")
    assert apac["spend_native"] is None
    assert apac["status"] != "No spend in window"
    assert apac["status"] == "Geo attribution needs review"
    assert page["kpis"]["countries_with_spend"] is None


def test_12_b_an_unreadable_dashboard_geo_read_is_no_region_s_zero(live_db, monkeypatch):
    """M3(a), the other way spend becomes unknown: the Dashboard's own geo read
    fails while the mart's succeeds."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _sync(live_db)
    _deal(live_db, "d-pk", "Pakistan")
    import types
    import services.dashboard_countries_service as d
    from services import country_spend_zero_rule as zr
    monkeypatch.setattr(d, "zero_rule", types.SimpleNamespace(
        read_country_spend=lambda *a, **k: {
            "spend": {"available": False, "has_rows": False, "rows": []},
            "zero_rule": None},
        zero_spend_verdict=zr.zero_spend_verdict,
        R_SPEND_LOCATION_UNRESOLVED=zr.R_SPEND_LOCATION_UNRESOLVED))
    page = _dash()
    assert all(r["spend_native"] is None for r in page["countries"])
    assert not any(r["status"] == "No spend in window" for r in page["regional_mix"])
    assert page["kpis"]["countries_with_spend"] is None


def test_13_a_region_whose_deal_count_is_withheld_is_not_spend_without_a_won_deal(
        live_db, monkeypatch):
    """Truth audit M3(b). No deal sync state: the ledger is not current, so
    every market's won-deal count is withheld. Europe (UK: £200 spend) read
    "Spend without a won deal" — a claim about a count nobody published."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _deal(live_db, "d-uk", "United Kingdom")          # never published: no sync state
    page = _dash()
    uk = _dash_row(page, "GB")
    assert uk["closed_won_deals"] is None and uk["spend_native"] == pytest.approx(200.0)
    europe = next(r for r in page["regional_mix"] if r["region"] == "Europe")
    assert europe["closed_won_deals"] is None
    assert europe["status"] == "Closed-won outcome withheld"


def test_14_a_withheld_geo_gate_publishes_no_deal_denominators(live_db, monkeypatch):
    """Truth audit MINOR 2. Before the morning sync the gate withholds every
    country figure (the mart's deal_ids with them), and the revenue disclosure
    published closed_won_deals 0 / revenue_proven_deals 0 over a population
    nobody counted. Withheld means absent."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _sync(live_db)
    _deal(live_db, "d-uk", "United Kingdom")
    _before_the_morning_sync(live_db)
    disc = _dash()["country_revenue_disclosure"]
    for k in ("closed_won_deals", "revenue_proven_deals", "revenue_unavailable_deals"):
        assert disc[k] is None, (k, disc[k])


def test_15_withheld_rows_carry_no_population_and_no_itemised_spend(live_db, monkeypatch):
    """Truth audit MINOR 4: claims of PR-ADS-176A nothing guarded. With the geo
    gate withholding, the mart's rows carry no deal_ids and its residual no
    outside-market items; the Dashboard's residual carries no items."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db, yesterday_geo=[(FIJI, 7.0)])
    _sync(live_db)
    _deal(live_db, "d-uk", "United Kingdom")
    ready = _mart()
    assert _mart_row(ready, "GB")["deal_ids"] == ["d-uk"]
    assert _residual(ready)["outside_listed_market_items"]
    _before_the_morning_sync(live_db)
    mart = _mart()
    assert all(r["deal_ids"] is None for r in mart["rows"])
    assert _residual(mart)["outside_listed_market_items"] is None
    assert _dash()["residual"]["outside_listed_market_items"] is None


def _identity(cluster, customer, label, *, method="not_google_ads"):
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO google_ads_campaign_identity (customer_id, "
            "external_campaign_label, match_method, approved_at, approved_by) "
            "VALUES (%s, %s, %s, now(), 'owner')", (customer, label, method))
        conn.commit()


def test_16_the_drawer_applies_the_row_s_not_google_ads_exclusion(live_db, monkeypatch):
    """Truth audit MINOR 4. A label the admin marked "not Google Ads" is not in
    the row; it must not be in the row's drawer either."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _sync(live_db)
    _identity(live_db, CUSTOMER, "offline import")
    _deal(live_db, "d-uk", "United Kingdom")
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    _, today = _window_dates(WINDOW)
    close = datetime.combine(today - timedelta(days=1), datetime.min.time(),
                             tzinfo=timezone.utc) + timedelta(hours=12)
    _insert(live_db, "d-offline", close_date=close.isoformat(), amount=700.0,
            revenue_usd=700.0, country="United Kingdom", gclid="g-off",
            campaign="offline import", group="google_ads",
            attribution_status="google_ads")
    from services.revenue_attribution_service import build_country_deal_details
    row = _mart_row(_mart(), "GB")
    assert row["deal_ids"] == ["d-uk"]
    drawer = build_country_deal_details(WINDOW, "United Kingdom", "GB")
    assert [d["deal_record_id"] for d in drawer["details"]] == ["d-uk"]


def test_16_b_the_drawer_reads_the_exclusions_of_the_row_s_account(live_db, monkeypatch):
    """Truth audit MINOR 4. The drawer read campaign spend with no account
    scope and took its mappings from that read's MIN(customer_id) — another
    account's, which carries no exclusion — so the excluded deal came back."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    import db.writers as writers
    start, today, yesterday = _seed(live_db)
    _sync(live_db)
    other = "0000000001"                       # sorts before the configured id
    assert writers.upsert_campaign_daily_spend([{
        "customer_id": other, "currency_code": "GBP", "campaign_id": "777",
        "campaign_name": "other account", "spend_date": yesterday,
        "cost_micros": 50_000_000, "source_query_version": "t"}]) == 1
    _identity(live_db, CUSTOMER, "offline import")
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    close = datetime.combine(yesterday, datetime.min.time(),
                             tzinfo=timezone.utc) + timedelta(hours=12)
    _insert(live_db, "d-offline", close_date=close.isoformat(), amount=700.0,
            revenue_usd=700.0, country="United Kingdom", gclid="g-off",
            campaign="offline import", group="google_ads",
            attribution_status="google_ads")
    from services.revenue_attribution_service import build_country_deal_details
    row = _mart_row(_mart(), "GB")
    assert row["closed_won_deals"] == 0 and row["deal_ids"] == []
    drawer = build_country_deal_details(WINDOW, "United Kingdom", "GB")
    assert [d["deal_record_id"] for d in drawer["details"]] == []


def test_17_a_withheld_deal_count_carries_no_deal_ids(live_db, monkeypatch):
    """Truth audit MINOR 5. With no deal sync state the won-deal count is
    withheld (None) and the row carried ``deal_ids: []`` — an empty proof
    list reads as "no deals"."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _deal(live_db, "d-uk", "United Kingdom")
    rows = _mart()["rows"]
    gb = _mart_row({"rows": rows}, "GB")
    assert gb["closed_won_deals"] is None
    assert gb["deal_ids"] is None


def test_17_b_the_reader_cutover_audit_knows_deal_ids_is_a_proof_list():
    """Truth audit MINOR 5: the audit's proof-list check names ``deal_ids``."""
    from scripts import audit_customer_revenue_reader_cutover as audit
    withheld = {"status": "unavailable", "value": None, "reason": "x"}
    for ids in ([], ["d1"]):
        a = audit.Audit()
        audit.page_leak_checks(a, "k", "page", {}, {"rows": [{
            "country": "Pakistan", "closed_won_deals": None,
            "metrics": {"closed_won_deals": withheld}, "deal_ids": ids}]})
        assert a.checks.get("withheld_not_reconstructable") is False, ids
    a = audit.Audit()
    audit.page_leak_checks(a, "k", "page", {}, {"rows": [{
        "country": "Pakistan", "closed_won_deals": None,
        "metrics": {"closed_won_deals": withheld}, "deal_ids": None}]})
    assert a.checks.get("withheld_not_reconstructable") is not False


def test_18_the_country_tables_print_unavailable_with_the_reason(tmp_path):
    """Truth audit MINOR 6 and M3. Country Revenue & Spend printed "—" for
    Pakistan's withheld spend; the Dashboard printed "Unavailable" with no
    reason; the KPI printed "—" for a withheld count of countries with spend."""
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    row = {"country": "Pakistan", "country_code": "PK", "spend": None,
           "spend_reason": "spend_location_unresolved", "leads": 0, "sqls": 0,
           "closed_won_deals": 1, "metrics": None, "verdict": "learning"}
    out = _run_app("renderRoasCountryTable([%s])" % json.dumps(row))
    assert "Unavailable" in out and "some spend in this window has no country" in out
    assert "<td>—</td>" not in out.split("Pakistan", 1)[1].split("</td>", 2)[1] + "</td>"
    dash = {"country_name": "Pakistan", "spend_native": None, "spend_usd": None,
            "spend_reason": "spend_location_unresolved", "native_currency": "GBP"}
    cell = _run_app("ctrySpendCell(%s)" % json.dumps(dash))
    assert "Unavailable" in cell and "some spend in this window has no country" in cell
    kpi = _run_app("renderCtryKpiRow(%s)" % json.dumps(
        {"kpis": {"countries_with_spend": None, "verified_spend_native": {"amount": None},
                  "verified_spend_usd": None, "verified_spend_reason": "spend_location_unresolved"},
         "metric_truth": {"country_attributed_spend_usd": {
             "unavailable_reason": "spend_location_unresolved"}}}))
    assert "countries with spend withheld" in kpi.lower()
    assert "Country spend withheld — some spend in this window has no country" in kpi


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
