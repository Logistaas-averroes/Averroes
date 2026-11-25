"""PR-ADS-177C — what Campaign Evidence asserted, asserted on the live surfaces,
on PostgreSQL through the real writers and builders.

Campaign Evidence (``/api/campaigns``, ``/api/campaign-detail``,
``campaign_evidence_service``) is deleted. Before it went, every truth property
its tests asserted was ported (``docs/60`` §9.3, the port table) onto the live
campaign surfaces: the mart's SETTLED campaign view (the Campaigns page),
Dashboard → Campaigns and the campaign drawer. A property the live surfaces did
not hold is a finding, fixed here — never pinned as observed:

* two Google Ads campaign ids under one name were silently merged into one
  "campaign" (case 03);
* Dashboard → Campaigns' per-campaign split read every account (case 08, M3)
  and let the last of two same-named campaigns overwrite the other (case 03).

It also carries the 177B review minors that need a database: M1 (cards before
the morning sync), M2 (the reader-cutover audit covers the settled view and the
Meta table), M3, M4 (no spend over unsettled spend), M8 (an independent reading
for the settled identities) and O1 (Dashboard → Countries' zero ratio).

Fixtures are PR-ADS-176A / 177A's (``_ready``): campaign `brand - uk` (id 111)
with verified, settled spend; one Google Ads deal on it; the acquisition cohort
c-uk / c-uk2 / c-ae on `brand - uk`, c-fj on the unmapped label "Mystery",
c-nolb with no label, c-org organic.
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, OTHER_CUSTOMER, RATE, live_db,
)
from tests.test_pr_ads_167a_settled_coverage_consumers_pg_integration import (  # noqa: E402
    _before_the_morning_sync, _window_dates,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import (  # noqa: E402
    WINDOW, _identity,
)
from tests.test_pr_ads_177a_one_sql_count_pg_integration import (  # noqa: E402
    _ready, _yesterday_at,
)


def _json(payload):
    return json.loads(json.dumps(payload, default=str))


def _settled():
    from services.revenue_decision_mart import build_revenue_decision_mart
    return _json(build_revenue_decision_mart(view="campaign", window=WINDOW, settled=True))


def _dash():
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    return _json(build_dashboard_campaigns(WINDOW))


def _row(payload, name, key="rows"):
    return next(r for r in payload[key] if r.get("campaign_name") == name)


def _campaign_spend(cluster, campaign_id, name, gbp, *, customer=CUSTOMER, day=None):
    """One more campaign spend row, re-certifying the account's coverage total
    exactly as the spend sync records it (so the window stays verified)."""
    import db.writers as writers
    start, today = _window_dates(WINDOW)
    day = day or (today - timedelta(days=1))
    assert writers.upsert_campaign_daily_spend([{
        "customer_id": customer, "currency_code": "GBP", "campaign_id": campaign_id,
        "campaign_name": name, "spend_date": day,
        "cost_micros": int(round(gbp * 1_000_000)), "source_query_version": "t"}]) == 1
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(cost_micros), 0), COUNT(*) FROM "
                    "google_ads_campaign_daily_spend WHERE customer_id = %s", (customer,))
        total, n = cur.fetchone()
    assert writers.upsert_spend_coverage(customer, start, today, "verified",
                                         rows_written=int(n), cost_micros_total=int(total))


# ── 1. parity keeps a real independent comparison ───────────────────────────
def test_01_parity_reads_the_cohort_by_campaign_id_in_both_groups_and_can_break(
        live_db, monkeypatch):
    """Campaign Evidence was the independent leg of both ``cohort_sql_parity``
    groups. Its replacement reads the canonical funnel through the campaign-ID
    resolver. Equal on consistent data; and when the MART's name-keyed resolver
    wrongly excludes a Google Ads label, both groups break — every other
    reading republishes the mart, so without this leg nothing would."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    from services import cross_page_parity_service as parity
    report = parity.audit_window(WINDOW)
    settled = report["cohort_sql_parity"]
    assert settled["status"] == "identical", settled
    assert settled["readings"]["campaign_id_cohort"] == settled["readings"]["campaigns_page"] == 4
    today_group = settled["campaign_view"]
    assert set(today_group["readings"]) == {"mart_campaign_view", "dashboard_overview",
                                            "campaign_id_cohort"}
    assert today_group["status"] == "identical", today_group
    assert "campaign_evidence" not in settled["readings"]

    # Counterfactual: the mart's resolver excludes `brand - uk` (no approved
    # mapping says so). c-uk and c-ae vanish from every mart-fed reading.
    import services.revenue_attribution_service as ras
    real = ras._mart_label_resolver

    def wrong(resolution_map, excluded):
        resolve = real(resolution_map, excluded)
        return lambda label: (("not_google_ads", None)
                              if str(label or "").strip().lower() == "brand - uk"
                              else resolve(label))
    monkeypatch.setattr(ras, "_mart_label_resolver", wrong)
    broken = parity.audit_window(WINDOW)
    s2 = broken["cohort_sql_parity"]
    assert s2["status"] == "broken", s2
    assert s2["readings"]["campaign_id_cohort"] == 4
    assert s2["readings"]["campaigns_page"] == s2["readings"]["countries_page"] == 2
    assert s2["campaign_view"]["status"] == "broken"
    assert "cohort_sql_parity_broken" in broken["violation_codes"]


def test_02_m8_the_settled_identities_have_an_independent_reading(live_db, monkeypatch):
    """The five ``*__settled_days`` identities compared Dashboard → Campaigns
    with the settled mart it republishes. A wrong settled figure then broke
    nothing. The canonical reading (spend truth, reader contract, campaign-ID
    cohort) now sits beside them — and catches it."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    from services import cross_page_parity_service as parity
    report = parity.audit_window(WINDOW)
    by = {m["metric"]: m for m in report["metrics"]}
    for key in ("google_ads_spend_usd", "campaign_attributed_won_revenue_usd",
                "campaign_attributed_closed_won_deals", "campaign_attributed_customers",
                "google_ads_cohort_sqls"):
        m = by[f"{key}__settled_days"]
        consumers = [r["consumer"] for r in m["readings"]]
        assert parity.CONSUMER_SETTLED_CAMPAIGN_CANONICAL in consumers, key
        detail = (key, m["status"], [(r["consumer"], r["value"], r["contract_problem"])
                                     for r in m["readings"]])
        if key == "campaign_attributed_customers":
            # No company-association scan in this fixture: customers are
            # withheld by the reader contract, and the independent reading
            # withholds them too — no figure on any side, never a 0.
            assert m["status"] == "unavailable", detail
            assert all(r["value"] is None for r in m["readings"]), detail
            continue
        assert m["status"] == "identical", detail
    assert by["campaign_attributed_closed_won_deals__settled_days"]["value"] == 1

    # Counterfactual: the settled mart over-counts its campaign deals; the
    # Dashboard republishes the same wrong figure. Only the independent
    # reading disagrees — and that is enough.
    import services.revenue_decision_mart as mart
    real = mart._campaign_page_blocks

    def inflated(*a, **k):
        totals, residual = real(*a, **k)
        if totals and totals.get("closed_won_deals") is not None:
            totals = {**totals, "closed_won_deals": totals["closed_won_deals"] + 1}
        return totals, residual
    real_summary = mart._summary_block

    def inflated_summary(core, spend_truth):
        s = real_summary(core, spend_truth)
        if s.get("attributed_closed_won_deals") is not None:
            s = {**s, "attributed_closed_won_deals": s["attributed_closed_won_deals"] + 1}
        return s
    monkeypatch.setattr(mart, "_campaign_page_blocks", inflated)
    monkeypatch.setattr(mart, "_summary_block", inflated_summary)
    bad = {m["metric"]: m for m in parity.audit_window(WINDOW)["metrics"]}
    m = bad["campaign_attributed_closed_won_deals__settled_days"]
    assert m["status"] == "mismatch", m["status"]
    values = {r["consumer"]: r["value"] for r in m["readings"]}
    assert values[parity.CONSUMER_SETTLED_CAMPAIGN_CANONICAL] == 1
    assert values["dashboard/campaigns"] == values[parity.CONSUMER_SETTLED_CAMPAIGN_REFERENCE] == 2


# ── 2. ported Campaign Evidence properties (findings fixed) ─────────────────
def test_03_two_campaign_ids_under_one_name_are_never_presented_as_one_campaign(
        live_db, monkeypatch):
    """Campaign Evidence (PR-ADS-143 / 157): "two campaign ids with the same
    display name are never merged". The live rows are keyed by name (HubSpot
    records campaigns by name), and merged two ids silently. FINDING, fixed:
    the row lists both ids, says the name is shared, and is not judged as one
    campaign; Dashboard → Campaigns sums both ids' spend under it (it printed
    whichever it read last) and marks it for mapping review."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    single = _row(_settled(), "brand - uk")
    assert single["google_ads_campaign_ids"] == ["111"]
    assert single["identity_status"] == "single_campaign"
    _campaign_spend(live_db, "222", "Brand - UK", 40.0)
    settled = _settled()
    row = _row(settled, "brand - uk")
    assert row["google_ads_campaign_ids"] == ["111", "222"]
    assert row["identity_status"] == "campaign_name_shared_by_google_ads_campaigns"
    assert row["verdict"] == "mapping_required"
    assert "2 Google Ads campaigns share this name" in row["identity_reason"]
    assert len([r for r in settled["rows"] if r.get("campaign_name") == "brand - uk"]) == 1
    dash = _dash()
    d = _row(dash, "brand - uk", key="campaigns")
    assert d["status"] == "Mapping needs review"
    assert d["google_ads_campaign_ids"] == ["111", "222"]
    from db import revenue_repository as repo
    start, end = (settled["window"]["start_date"], settled["window"]["end_date"])
    from datetime import date
    spend = repo.fetch_canonical_campaign_spend(date.fromisoformat(start),
                                                date.fromisoformat(end), CUSTOMER)
    both = sum(r["spend"] for r in spend["rows"] if r["campaign_id"] in ("111", "222"))
    assert d["spend_native"] == pytest.approx(both)
    # The certification gate's L5 check holds on this, and fails on a row that
    # hides the sharing.
    import scripts.audit_campaigns_page_certification as cert
    f = cert.Findings()
    cert.audit_window(WINDOW, f)
    assert not [c for c in f.checks if c["check"].startswith("shared_name") and not c["ok"]]
    import services.revenue_attribution_service as ras
    monkeypatch.setattr(ras, "SHARED_DISPLAY_NAME", "x")       # the flag no longer matches
    g = cert.Findings()
    cert.audit_window(WINDOW, g)
    assert any(v.startswith(f"shared_name_not_merged[{WINDOW}]") for v in g.violations)


def test_04_the_live_campaign_surfaces_hold_campaign_evidences_population_rules(
        live_db, monkeypatch):
    """Ports of PR-ADS-143 / 161B cases: an approved alias places its contact on
    the canonical campaign; an approved not-Google-Ads label leaves the Google
    Ads total (counted, under its reason); an unmapped label keeps its own row
    and its SQLs (never dropped); a lead-only label's spend is unavailable,
    never $0; the totals' spend is the canonical campaign spend over the same
    settled days; no CPQL anywhere."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    from db import writers
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO google_ads_campaign_identity (customer_id, campaign_id, "
            "canonical_campaign_name, external_campaign_label, match_method, "
            "approved_at, approved_by) VALUES (%s, '111', 'brand - uk', "
            "'uk brand alias', 'manual', now(), 'owner')", (CUSTOMER,))
        conn.commit()
    _identity(live_db, CUSTOMER, "offline import")
    y = _yesterday_at()
    writers.upsert_hubspot_contact_funnel([
        {"contact_id": "c-alias", "lifecycle_stage": "salesqualifiedlead",
         "date_entered_sql": y, "created_at": y, "last_modified_at": y,
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "uk brand alias"},
        {"contact_id": "c-offline", "lifecycle_stage": "salesqualifiedlead",
         "date_entered_sql": y, "created_at": y, "last_modified_at": y,
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "offline import"},
    ])
    s = _settled()
    uk = _row(s, "brand - uk")
    assert (uk["leads"], uk["sqls"]) == (4, 3)                  # c-uk, c-ae + c-alias
    assert not any(r.get("campaign_name") == "offline import" for r in s["rows"])
    b = s["cohort"]["breakdown"]
    assert b["excluded_non_google"]["by_reason"].get("label_mapped_not_google_ads") == 1
    assert s["cohort"]["totals"]["sqls"] == 5                   # c-offline is not Google Ads
    mystery = _row(s, "Mystery")
    assert mystery["sqls"] == 1 and mystery["spend"] is None    # never $0
    assert mystery["spend_state"] == "unmapped"
    from datetime import date
    from db import revenue_repository as repo
    canon = repo.fetch_canonical_campaign_spend(
        date.fromisoformat(s["window"]["start_date"]),
        date.fromisoformat(s["window"]["end_date"]), CUSTOMER)
    assert s["campaign_totals"]["spend"] == pytest.approx(canon["total_spend_usd"], abs=0.01)
    from scripts.audit_marketing_outcome_cohorts import _cpql_keys
    assert _cpql_keys(s) == [] and _cpql_keys(_dash()) == []


def test_05_the_cohort_audit_certifies_the_campaigns_surface_and_goes_red(
        live_db, monkeypatch):
    """The per-window independent reconciliation (membership, SQL proof,
    dedup, the Google Ads / excluded split, lifecycle gaps) moved from
    Campaign Evidence onto the settled campaign view."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    import scripts.audit_marketing_outcome_cohorts as audit
    payload = _settled()
    independent = audit.independent_counts(*audit._settled_instants(payload))
    a = audit.Audit()
    out = audit.audit_campaign_cohort(a, window=WINDOW, payload=payload,
                                      independent=independent)
    assert a.violations == [] and a.unavailable == [], (a.violations, a.unavailable)
    assert out["cohort"]["google_ads_sqls"] == 4
    names = {c["check"] for c in a.checks}
    for check in ("membership", "sql_proof", "dedup", "bucket_reconciliation",
                  "google_ads_split", "lifecycle_gaps_disclosed", "no_cpql_published",
                  "lifecycle_events_not_relabelled"):
        assert f"[campaigns:{WINDOW}] {check}" in names, check
    # Counterfactuals: a dropped row SQL, and a published CPQL.
    leaky = json.loads(json.dumps(payload))
    leaky["rows"][0]["sqls"] = (leaky["rows"][0]["sqls"] or 0) + 1
    b = audit.Audit()
    audit.audit_campaign_cohort(b, window=WINDOW, payload=leaky, independent=independent)
    assert any("bucket_reconciliation" in v for v in b.violations)
    cpql = {**payload, "campaign_totals": {**payload["campaign_totals"], "cpql_usd": 12.5}}
    c = audit.Audit()
    audit.audit_campaign_cohort(c, window=WINDOW, payload=cpql, independent=independent)
    assert any("no_cpql_published" in v for v in c.violations)


def test_06_the_campaigns_certification_holds_and_catches_a_wrong_drawer(
        live_db, monkeypatch):
    """The daily certification's Campaigns gate (it replaces the Campaign
    Evidence one): every check holds on consistent data; a drawer that lists
    a deal its row does not count is a violation."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    import scripts.audit_campaigns_page_certification as cert
    f, per = cert.run((WINDOW,))
    assert f.violations == [] and f.unavailable == [], (f.violations, f.unavailable)
    assert f.exit_code == cert.EXIT_CERTIFIED and per[WINDOW]["available"]
    # PR-ADS-177C-F1: L6 records how many drawers it probed (never a silent
    # pass over none); the brand - uk row has its deal.
    assert per[WINDOW]["drawers_probed"] >= 1 and per[WINDOW]["rows_with_deals"] == 1
    assert per[WINDOW]["canonical_revenue"] == "available"
    import services.revenue_attribution_service as ras
    # The drawers answer from ONE grouped read (PR-ADS-177C-F1); a drawer
    # population that lists a deal its row does not count is a violation.
    real = ras.campaign_deal_details_by_campaign

    def extra(*a, **k):
        out = real(*a, **k)
        by = {k_: list(v) for k_, v in (out.get("by_campaign") or {}).items()}
        by.setdefault("brand - uk", []).append({"deal_record_id": "d-not-on-the-row"})
        return {**out, "by_campaign": by}
    monkeypatch.setattr(ras, "campaign_deal_details_by_campaign", extra)
    g, _ = cert.run((WINDOW,))
    assert g.exit_code == cert.EXIT_VIOLATION
    assert any(v.startswith(f"drawer_population[{WINDOW}]") for v in g.violations)
    # …and the one-drawer route answers from the same read.
    d = ras.build_campaign_deal_details(WINDOW, "brand - uk", settled_days=True)
    assert "d-not-on-the-row" in d["deal_ids"]


# ── 3. the 177B review minors ───────────────────────────────────────────────
def test_07_m1_m4_before_the_morning_sync(live_db, monkeypatch):
    """Before the morning sync re-reads yesterday, the settled view's spend is
    not settled. M4: the payload carries no spend figure (rows, summary, spend
    truth) and no row is judged on it. M1: Dashboard → Campaigns does not say
    "verified Google Ads spend", nor that FX is the gap, nor that native spend
    is shown — none of which is true then."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    control = _dash()
    scale = next(c for c in control["decision_cards"] if c["type"] == "scale")
    assert "with verified Google Ads spend" in scale["body"]
    assert _row(_settled(), "brand - uk")["spend"] is not None

    _before_the_morning_sync(live_db, table="google_ads_spend_coverage")
    s = _settled()
    assert s["readiness"]["spend_decision_ready"] is False
    assert s["summary"]["spend_usd"] is None
    assert s["spend_truth"]["usd_spend"] is None and s["spend_truth"]["native_spend"] is None
    for r in s["rows"]:
        assert r.get("spend") is None and r.get("period_revenue_per_ad_spend") is None, r
        assert r.get("verdict") not in ("winner", "watch", "waste", "learning"), r
    uk = _row(s, "brand - uk")
    assert uk["verdict"] == "spend_unknown"
    assert uk["verdict_reason"] == "spend_not_reread_since_day_ended"
    from scripts.audit_campaigns_page_certification import spend_exposures
    assert spend_exposures(s) == []

    dash = _dash()
    cards = {c["type"]: c for c in dash["decision_cards"]}
    text = " ".join(c["headline"] + " " + c["body"] for c in dash["decision_cards"])
    assert "verified Google Ads spend" not in text
    assert "FX coverage is incomplete" not in text
    assert "native GBP spend is still shown" not in text
    assert cards["unavailable"]["headline"] == "Spend not settled"
    assert "re-read since the day ended" in cards["unavailable"]["body"]
    assert "not shown yet" in cards["scale"]["body"]
    reasons = {u["metric"]: u["reason"] for u in dash["unavailable"]}
    assert "not settled" in reasons["verified_spend_usd"]
    assert "native GBP spend is shown instead" not in reasons["verified_spend_usd"]
    assert "settled spend" in reasons["period_revenue_per_ad_spend"]


def test_08_m3_dashboard_campaigns_reads_only_the_configured_accounts_spend(
        live_db, monkeypatch):
    """Another account's campaign of the same name spent yesterday. The mart
    row is the configured account's; Dashboard → Campaigns' native split was
    read across every account."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    before = _row(_dash(), "brand - uk", key="campaigns")["spend_native"]
    _campaign_spend(live_db, "999", "brand - uk", 5000.0, customer=OTHER_CUSTOMER)
    after = _row(_dash(), "brand - uk", key="campaigns")
    assert after["spend_native"] == pytest.approx(before)
    assert after["google_ads_campaign_ids"] == ["111"]


def test_09_o1_a_verified_market_with_zero_revenue_reads_the_same_on_both_country_pages(
        live_db, monkeypatch):
    """UAE spent (verified, settled) and won nothing: a published zero
    revenue. The Countries page (the mart row's reader-contract ratio) prints
    0.00x; Dashboard → Countries printed None. Now both print 0.0."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    from services.dashboard_countries_service import build_dashboard_countries
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = _json(build_revenue_decision_mart(view="country", window=WINDOW))
    dash = _json(build_dashboard_countries(WINDOW))
    m = next(r for r in mart["rows"] if r.get("country_code") == "AE")
    d = next(r for r in dash["countries"] if r.get("country_code") == "AE")
    assert m["won_revenue"] == 0 and (m["spend"] or 0) > 0
    assert m["period_revenue_per_ad_spend"] == 0.0
    assert d["period_revenue_per_ad_spend"] == m["period_revenue_per_ad_spend"] == 0.0
    ratio = d["metrics"]["period_revenue_per_ad_spend"]
    assert ratio["ratio_kind"] == "non_cohort_operational_ratio"
    uk_m = next(r for r in mart["rows"] if r.get("country_code") == "GB")
    uk_d = next(r for r in dash["countries"] if r.get("country_code") == "GB")
    assert uk_d["period_revenue_per_ad_spend"] == uk_m["period_revenue_per_ad_spend"] > 0


def test_10_m2_the_reader_cutover_audit_covers_the_settled_view_and_the_meta_table(
        live_db, monkeypatch):
    """Check 18 (no ROAS / CAC name), the leak checks and windows_reconcile now
    run over the Campaigns page's settled view and ``/api/meta-ads/adsets``.
    The settled view's summary is reconciled with the canonical reader over
    the SAME settled days — and a wrong one is caught."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    from scripts import audit_customer_revenue_reader_cutover as rc
    names = [n for n, _ in rc._page_builders()]
    assert {"revenue_decision_mart_campaign_settled", "meta_ads_adsets"} <= set(names)
    assert {"revenue_decision_mart_campaign_settled", "dashboard_campaigns",
            "meta_ads_adsets"} <= rc.SETTLED_DAY_PAGES
    a = rc.Audit()
    out = rc.settled_mart_checks(a, WINDOW, None, _settled())
    assert out["closed_won_deals"] == 1
    assert not a.violations, a.violations
    assert a.checks.get("windows_reconcile") is True
    wrong = _settled()
    wrong["summary"]["closed_won_deals"] += 1
    b = rc.Audit()
    rc.settled_mart_checks(b, WINDOW, None, wrong)
    assert any("campaign_settled" in v for v in b.violations), b.violations


# ── 4. PR-ADS-177C-F1 (truth audit round 1) ─────────────────────────────────
def _won_deal(cluster, deal_id, campaign, amount, *, gclid):
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    _insert(cluster, deal_id, close_date=_yesterday_at(9).isoformat(), amount=amount,
            revenue_usd=amount, country="United Kingdom", gclid=gclid, campaign=campaign,
            group="google_ads", attribution_status="google_ads")


def test_11_m1_overview_never_names_a_shared_row_the_best_campaign(live_db, monkeypatch):
    """Production: "low cpc - europe" is two Google Ads campaign ids. A shared
    row with the HIGHEST revenue is the name's true combined figure, not one
    campaign's — Overview's best-campaign signal names the best DISTINCT
    campaign, says it left the shared row out, and the certification's L5 now
    checks Overview too."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    _campaign_spend(live_db, "222", "Brand - UK", 40.0)          # brand - uk is shared now
    _campaign_spend(live_db, "333", "gulf search - uae", 90.0)
    # Without an approved mapping a shared name places no deal by campaign ID
    # (never merged), and the window's campaign revenue is withheld whole
    # (PR-ADS-161D) — no "best campaign" at all. With the label approved onto
    # one id, revenue publishes and the name-keyed row is still two ids.
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO google_ads_campaign_identity (customer_id, campaign_id, "
            "canonical_campaign_name, external_campaign_label, match_method, "
            "approved_at, approved_by) VALUES (%s, '111', 'brand - uk', "
            "'brand - uk', 'manual', now(), 'owner')", (CUSTOMER,))
        conn.commit()
    _won_deal(live_db, "d-big", "brand - uk", 50000.0, gclid="g-big")
    _won_deal(live_db, "d-gulf", "gulf search - uae", 2000.0, gclid="g-gulf")
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = _json(build_revenue_decision_mart(view="campaign", window=WINDOW))
    top = max((r for r in mart["rows"] if r.get("won_revenue") is not None),
              key=lambda r: r["won_revenue"])
    assert top["campaign_name"] == "brand - uk" and top["won_revenue"] > 50000.0
    assert top["identity_status"] == "campaign_name_shared_by_google_ads_campaigns"
    assert _row(mart, "gulf search - uae")["won_revenue"] == 2000.0
    from services.dashboard_overview_service import build_dashboard_overview
    ov = _json(build_dashboard_overview(WINDOW))
    best = ov["top_signals"]["best_campaign"]
    assert best["label"] == "gulf search - uae", best
    assert best["shared_name_rows_excluded"] == 1
    import scripts.audit_campaigns_page_certification as cert
    f = cert.Findings()
    cert.audit_window(WINDOW, f)
    assert not [v for v in f.violations if v.startswith("shared_name")], f.violations
    # Counterfactual: Overview ranks every row (the 177C code) — the shared
    # row wins, and L5 names it.
    import services.dashboard_overview_service as ovs
    real = ovs._build_signals

    def unfiltered(campaign_rows, *a, **k):
        rows = [{**r, "identity_status": None} for r in campaign_rows]
        return real(rows, *a, **k)
    monkeypatch.setattr(ovs, "_build_signals", unfiltered)
    assert _json(build_dashboard_overview(WINDOW))["top_signals"]["best_campaign"][
        "label"] == "brand - uk"
    g = cert.Findings()
    cert.audit_window(WINDOW, g)
    assert any(v.startswith(f"shared_name_not_merged[{WINDOW}]") and "overview:best_campaign"
               in v for v in g.violations), g.violations


def test_12_minor1_an_approved_not_google_ads_label_with_a_won_deal_stays_identical(
        live_db, monkeypatch):
    """The mart drops the deals of a label the account's admin approved as
    "not Google Ads" (PR-ADS-120b). The independent settled reading counted
    them, so correct data read as a mismatch. It applies the same approved
    exclusions now."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    _identity(live_db, CUSTOMER, "offline import")
    _won_deal(live_db, "d-offline", "offline import", 8000.0, gclid="g-off")
    from services import cross_page_parity_service as parity
    by = {m["metric"]: m for m in parity.audit_window(WINDOW)["metrics"]}
    for key in ("campaign_attributed_closed_won_deals", "campaign_attributed_won_revenue_usd"):
        m = by[f"{key}__settled_days"]
        assert m["status"] == "identical", (key, [(r["consumer"], r["value"])
                                                  for r in m["readings"]])
    assert by["campaign_attributed_closed_won_deals__settled_days"]["value"] == 1


def test_13_minor2_l6_is_unavailable_when_canonical_revenue_is(live_db, monkeypatch):
    """A drawer check over a ledger nobody could read proves nothing: exit 2,
    with the reason recorded beside drawers_probed = None."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    import scripts.audit_campaigns_page_certification as cert
    import services.revenue_attribution_service as ras
    monkeypatch.setattr(ras, "campaign_deal_details_by_campaign",
                        lambda *a, **k: {"available": False,
                                         "reason": "canonical_ledger_stale",
                                         "by_campaign": {}})
    f, per = cert.run((WINDOW,))
    assert f.exit_code == cert.EXIT_UNAVAILABLE, (f.violations, f.unavailable)
    assert any(u.startswith(f"drawer_population[{WINDOW}]") and "canonical_ledger_stale" in u
               for u in f.unavailable)
    assert per[WINDOW]["drawers_probed"] is None
    assert per[WINDOW]["canonical_revenue"] == "canonical_ledger_stale"
