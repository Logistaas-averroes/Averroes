"""PR-ADS-168A — Channels outcomes on PostgreSQL, through the real reads.

Real tables, real writers: the deal ledger and its contact bridge
(``upsert_deal``), the contact funnel's ``hs_analytics_source``, the
company-association observations and a complete scan, the canonical
campaign spend, its coverage ledger and FX. Nothing is stubbed between the
API builder and the database.

* the source a deal is placed by is read in the SAME REPEATABLE READ snapshot
  as the won population (``fetch_closed_won_universe``);
* the rows reconcile to the window's deal count with every unresolved class
  counted; a company in two channels counts once per row and once in total;
* a deal closed TODAY is not in the outcomes (the window ends yesterday) and
  today's spend is not in the Paid Search spend;
* a verified zero spend is a published 0 with the ratio unavailable;
* a stale ledger publishes no outcome;
* the evidence-day spend helper equals the business-window spend truth over
  every business window (one state machine);
* the reader cutover audit's evidence-window pass holds on these tables.

These cases must run in CI (PG step + did-run assertion), never skip there.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.canonical_ledger_fixtures import ledger_row  # noqa: E402
from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, RATE, _today, _write_fx, live_db,
)

LONDON = ZoneInfo("Europe/London")


# ── seeding ─────────────────────────────────────────────────────────────────
def _ready_ledger():
    from db import deal_ledger_repository as repo
    assert repo.record_sync_state(status="success", sync_mode="bootstrap",
                                  proved_complete=True)["available"]
    assert repo.record_sync_state(status="success",
                                  sync_mode="incremental")["available"]


def _close(day: date, hour=12) -> str:
    return datetime(day.year, day.month, day.day, hour, tzinfo=LONDON).astimezone(
        timezone.utc).isoformat()


def _deal(deal_id, close, contacts, *, revenue=1000.0, status=None, observed=True,
          **extra):
    from analysis.deal_truth import resolve_deal_associations
    from db import deal_ledger_repository as repo
    # The association columns exactly as the deal sync derives them.
    resolution = resolve_deal_associations(
        [{"contact_id": c} for c in contacts], lookup_failed=not observed)
    kw = {k: resolution[k] for k in ("association_status", "association_reason",
                                     "association_count", "primary_contact_id",
                                     "attribution_status", "attribution_reason")}
    if status is not None:
        kw["association_status"] = status
    kw.update(extra)
    row = ledger_row(deal_id, deal_close_date=close, revenue_usd=revenue,
                     amount_raw=revenue, **kw)
    res = repo.upsert_deal(row, associations=[{"contact_id": c} for c in contacts],
                           associations_observed=observed)
    assert res.get("available") is not False, res


def _funnel(cluster, contact_id, source):
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO hubspot_contact_funnel (contact_id, created_at, "
                    "hs_analytics_source) VALUES (%s, %s, %s)",
                    (str(contact_id), datetime(2025, 3, 1, tzinfo=timezone.utc), source))
        conn.commit()


def _companies(mapping: dict):
    """Each deal's company observed, then one complete scan of the won set."""
    from db import deal_company_association_repository as repo
    now = datetime.now(timezone.utc)
    for deal_id, company in mapping.items():
        assert repo.record_observation_success(
            deal_id, [{"company_id": company, "association_category": "HUBSPOT_DEFINED",
                       "association_type_id": "5", "association_label": None,
                       "association_path_key": "HUBSPOT_DEFINED:5"}],
            observation_started_at=now - timedelta(minutes=30))["applied"]
    won = sorted(mapping)
    assert repo.record_sync_run(
        mode="incremental", status="success", started_at=now - timedelta(minutes=60),
        population=len(won), attempted=len(won), succeeded=len(won),
        complete=True, full_population=True, scanned_deal_ids=won)["available"]


def _spend(start: date, end: date, *, per_day_gbp=10.0, days=None):
    """Canonical campaign spend on ``days`` (default: every day), FX every day,
    one verified coverage chunk [start, end] fetched now."""
    import db.writers as writers
    _write_fx(start, end)
    rows, d = [], start
    while d <= end:
        if days is None or d in days:
            rows.append({"customer_id": CUSTOMER, "currency_code": "GBP",
                         "campaign_id": "111", "campaign_name": "brand",
                         "spend_date": d, "cost_micros": int(per_day_gbp * 1e6),
                         "source_query_version": "t"})
        d += timedelta(days=1)
    if rows:
        assert writers.upsert_campaign_daily_spend(rows) == len(rows)
    assert writers.upsert_spend_coverage(
        CUSTOMER, start, end, "verified", rows_written=len(rows),
        cost_micros_total=int(per_day_gbp * 1e6) * len(rows))


def _seed_production_shape(cluster):
    """Three placed deals (one company in two channels), one deal per
    unresolved class, and a deal closed today."""
    today = _today()
    y = today - timedelta(days=1)
    for cid, src in (("c1", "PAID_SEARCH"), ("c2", "PAID_SEARCH"),
                     ("c3", "DIRECT_TRAFFIC"), ("c4", "OFFLINE"),
                     ("c5", "PAID_SEARCH"), ("c6", "DIRECT_TRAFFIC"),
                     ("c8", None), ("c9", "PAID_SEARCH")):
        _funnel(cluster, cid, src)
    _deal("ps", _close(today - timedelta(days=10)), ["c1", "c2"])
    _deal("dt", _close(today - timedelta(days=20)), ["c3"])
    _deal("off", _close(y), ["c4"])
    _deal("mix", _close(today - timedelta(days=5)), ["c5", "c6"])
    _deal("none", _close(today - timedelta(days=6)), [])
    _deal("lost", _close(today - timedelta(days=7)), ["c7"])        # not in funnel
    _deal("blank", _close(today - timedelta(days=8)), ["c8"])       # no source
    # One minute after the account's midnight today: already past, not settled.
    _deal("todays", (datetime.combine(today, datetime.min.time(), tzinfo=LONDON)
                     + timedelta(minutes=1)).astimezone(timezone.utc).isoformat(),
          ["c9"])
    _companies({"ps": "co-1", "dt": "co-1", "off": "co-2", "mix": "co-3",
                "none": "co-4", "lost": "co-5", "blank": "co-6", "todays": "co-7"})


def _row(out, source):
    return next(r for r in out["channels"] if r["source"] == source)


# ── cases ───────────────────────────────────────────────────────────────────
def test_01_the_contact_source_is_read_in_the_won_snapshot(live_db):
    from db import deal_ledger_repository as repo
    _ready_ledger()
    _funnel(live_db, "c1", "PAID_SEARCH")
    _deal("d1", _close(_today() - timedelta(days=3)), ["c1", "c-missing"])
    u = repo.fetch_closed_won_universe("326093516")
    got = {c["contact_id"]: (c["funnel_row_present"], c["hs_analytics_source"])
           for c in u["acquisition_contacts"]}
    assert got == {"c1": (True, "PAID_SEARCH"), "c-missing": (False, None)}


def test_02_the_rows_reconcile_and_every_class_is_counted_on_real_tables(live_db):
    from services.source_attribution_service import build_channel_outcomes
    _ready_ledger()
    _seed_production_shape(live_db)
    today = _today()
    _spend(today - timedelta(days=200), today, per_day_gbp=10.0)
    out = build_channel_outcomes("30d")
    p = out["partition"]
    assert p["reconciles"] is True
    # The deal closed today (account midnight) is not in the window.
    assert p["canonical_closed_won_deals"] == 7 == p["deals_in_rows"]
    assert p["unresolved_by_class"] == {
        "contacts_disagree": 1, "no_associated_contact": 1,
        "association_lookup_failed": 0, "contact_not_in_funnel": 1,
        "contact_has_no_source": 1, "associated_contacts_not_stored": 0}
    assert _row(out, "PAID_SEARCH")["closed_won_deals"] == 1
    assert _row(out, "DIRECT_TRAFFIC")["closed_won_deals"] == 1
    assert _row(out, "OFFLINE")["closed_won_deals"] == 1
    assert _row(out, "__deal_source_unresolved__")["closed_won_deals"] == 4
    # One company won through Paid Search and Direct: once per row, once in total.
    assert _row(out, "PAID_SEARCH")["customers"] == 1
    assert _row(out, "DIRECT_TRAFFIC")["customers"] == 1
    assert out["totals"]["customers"] == 6
    assert sum(r["customers"] or 0 for r in out["channels"]) == 7
    assert out["window"]["end_date"] == (today - timedelta(days=1)).isoformat()
    # Spend: 29 settled days at £10, at RATE — today's £10 is not in it.
    ps = _row(out, "PAID_SEARCH")
    assert ps["spend"]["reason"] == "spend_shown_on_google_ads_line"
    assert out["spend"]["google_ads"]["value"] == pytest.approx(29 * 10.0 * RATE)
    # Paid Search is HubSpot's source, not Google Ads' population: no ratio.
    assert ps["period_revenue_per_ad_spend"] is None
    assert ps["metrics"]["period_revenue_per_ad_spend"]["reason"] == \
        "channel_population_is_not_spend_population"
    # No deal here carries Google Ads evidence: the line is a measured 0.
    g = out["google_ads_line"]
    assert (g["closed_won_deals"], g["won_revenue"]) == (0, 0.0)
    assert g["spend"]["value"] == pytest.approx(29 * 10.0 * RATE)
    assert _row(out, "OFFLINE")["spend"]["reason"] == "channel_spend_not_connected"


def test_03_verified_zero_spend_is_published_zero_with_the_ratio_unavailable(live_db):
    from services.source_attribution_service import build_channel_outcomes
    _ready_ledger()
    _seed_production_shape(live_db)
    today = _today()
    _spend(today - timedelta(days=200), today, days=set())
    out = build_channel_outcomes("30d")
    ps = _row(out, "PAID_SEARCH")
    g = out["google_ads_line"]
    assert (g["spend"]["status"], g["spend"]["value"]) == ("published", 0.0)
    assert out["google_ads_line"]["metrics"]["period_revenue_per_ad_spend"]["reason"] == \
        "no_google_ads_spend_in_window"


def test_04_a_stale_ledger_publishes_no_outcome(live_db):
    from services.source_attribution_service import build_channel_outcomes
    _ready_ledger()
    _seed_production_shape(live_db)
    with live_db.connect() as conn, conn.cursor() as cur:
        # A scheduler that stopped 80 hours ago: bootstrap before it, then the
        # last successful incremental, then nothing.
        cur.execute("UPDATE hubspot_deal_sync_state SET "
                    "bootstrap_started_at = NOW() - INTERVAL '101 hours', "
                    "bootstrap_completed_at = NOW() - INTERVAL '100 hours', "
                    "last_incremental_at = NOW() - INTERVAL '80 hours'")
        conn.commit()
    out = build_channel_outcomes("180d")
    assert out["channels"] is None and out["partition"] is None
    assert out["totals"]["closed_won_deals"] is None
    assert out["totals"]["metrics"]["closed_won_deals"]["reason"] == "canonical_ledger_stale"


@pytest.mark.parametrize("variant", ["verified", "fx_gap", "failed_chunk"])
def test_05_evidence_day_spend_equals_business_window_spend_truth(live_db, variant):
    from analysis.business_windows import WINDOW_KEYS
    from services.canonical_contract import resolve_canonical_window
    from services import revenue_spend_truth_service as sts
    import db.writers as writers
    today = _today()
    start = today - timedelta(days=400)
    _spend(start, today)
    if variant == "fx_gap":
        with live_db.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM fx_rates WHERE rate_date = %s",
                        (today - timedelta(days=3),))
            conn.commit()
    if variant == "failed_chunk":
        writers.upsert_spend_coverage(CUSTOMER, today - timedelta(days=2), today,
                                      "failed", rows_written=0, cost_micros_total=0)
        with live_db.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM google_ads_spend_coverage WHERE status = 'verified'")
            conn.commit()
        writers.upsert_spend_coverage(CUSTOMER, start, today - timedelta(days=3),
                                      "verified", rows_written=1, cost_micros_total=1)
    keys = ("state", "usd_spend", "native_spend", "fx_status",
            "spend_coverage_status", "period_ratio_available",
            "google_ads_spend_source")
    states = set()
    for w in WINDOW_KEYS:
        r = resolve_canonical_window(w)
        s = date.fromisoformat(r["start_date"]) if r["start_date"] else None
        e = date.fromisoformat(r["end_date"])
        business = sts.build_google_ads_spend_truth(w)
        days = sts.build_google_ads_spend_truth_for_days(s, e)
        assert {k: business[k] for k in keys} == {k: days[k] for k in keys}, w
        states.add(days["state"])
    assert states  # every window compared


def test_06_the_audits_evidence_pass_holds_on_real_tables(live_db):
    from scripts import audit_customer_revenue_reader_cutover as audit
    _ready_ledger()
    _seed_production_shape(live_db)
    today = _today()
    _spend(today - timedelta(days=200), today)
    _, report = audit.run()
    assert not report["violations"], report["violations"]
    ev = report["evidence_windows"]
    assert not [w for w in report["windows"] if str(w["window"]).startswith("evidence/")]
    assert [w["window"] for w in ev] == [f"evidence/{k}" for k in audit.EVIDENCE_PASS_WINDOWS]
    assert not [p for p in report["pages_not_run"] if p["page"] == "channel_outcomes"]
    assert all(w["independent_members"] >= 7 for w in ev)


def test_07_the_google_ads_line_reads_the_truths_evidence_on_real_tables(live_db):
    """On real tables, with the production campaign resolver (all-time
    canonical spend names) and deals written as the deal sync writes them
    (``acquisition_group`` from the real ``classify_source``):

    * g1 — GCLID, campaign "brand", Paid Search: Google Ads evidence;
    * d1 — GCLID, campaign "brand", HubSpot source Direct: evidence, on Direct;
    * p1 — Paid Search, no GCLID, no label: evidence by the platform's
      doctrine (the Paid Search classification), shown as "source only";
    * o1 — Offline source, no GCLID: no Google Ads evidence."""
    from analysis.source_classification import classify_source
    from services.source_attribution_service import build_channel_outcomes
    _ready_ledger()
    today = _today()
    for cid, src in (("c1", "PAID_SEARCH"), ("c2", "DIRECT_TRAFFIC"),
                     ("c3", "PAID_SEARCH"), ("c4", "OFFLINE")):
        _funnel(live_db, cid, src)

    def synced(src):
        return {"source_primary_raw": src,
                "acquisition_group": classify_source(src, None)}

    _deal("g1", _close(today - timedelta(days=4)), ["c1"], revenue=1000.0,
          gclid="g-1", campaign_name_raw="brand", **synced("PAID_SEARCH"))
    _deal("d1", _close(today - timedelta(days=5)), ["c2"], revenue=2000.0,
          gclid="g-2", campaign_name_raw="brand", **synced("DIRECT_TRAFFIC"))
    _deal("p1", _close(today - timedelta(days=6)), ["c3"], revenue=4000.0,
          **synced("PAID_SEARCH"))
    _deal("o1", _close(today - timedelta(days=7)), ["c4"], revenue=8000.0,
          **synced("OFFLINE"))
    _companies({"g1": "co-1", "d1": "co-2", "p1": "co-3", "o1": "co-4"})
    _spend(today - timedelta(days=200), today, per_day_gbp=10.0)
    out = build_channel_outcomes("30d")
    ps = _row(out, "PAID_SEARCH")
    assert (ps["closed_won_deals"], ps["won_revenue"]) == (2, 5000.0)
    assert ps["metrics"]["period_revenue_per_ad_spend"] == {
        "value": None, "status": "unavailable",
        "reason": "channel_population_is_not_spend_population"}
    assert ps["spend"]["reason"] == "spend_shown_on_google_ads_line"
    g = out["google_ads_line"]
    assert g["evidence"] == {"status": "published", "reason": None}
    assert (g["closed_won_deals"], g["won_revenue"], g["customers"]) == (3, 7000.0, 3)
    sp = g["evidence_split"]
    assert (sp["with_gclid"]["closed_won_deals"]["value"],
            sp["with_gclid"]["revenue_usd"]["value"]) == (2, 3000.0)
    assert (sp["source_classification_only"]["closed_won_deals"]["value"],
            sp["source_classification_only"]["revenue_usd"]["value"]) == (1, 4000.0)
    spend = 29 * 10.0 * RATE
    assert g["spend"]["value"] == pytest.approx(spend)
    assert g["period_revenue_per_ad_spend"] == pytest.approx(round(7000.0 / spend, 2))
    assert _row(out, "DIRECT_TRAFFIC")["won_revenue"] == 2000.0
    assert _row(out, "OFFLINE")["won_revenue"] == 8000.0
