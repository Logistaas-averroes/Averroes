"""PR-ADS-178C — the retired Leads / Deals feeds, ported onto the live
surfaces, on PostgreSQL through the real writers and builders (``docs/61`` §13).

The retired ``/api/crm-funnel/customers-won`` carried one truth property the
live surfaces must keep: it was the cross-page parity audit's ONE consumer of
the customers / closed-won-deals identities that read the closed-won reader
contract directly instead of republishing the mart (``leads/customers_won``).
Its reading now lives in the Pipeline builder's ``customers_won`` block, and
parity reads it there (``pipeline/customers_won``). These cases show the port
holds and can fail; that the retired routes answer 404 on a seeded, signed-in
app while the live ones serve; that the reader-cutover audit (production
certification) runs clean with the retirement check; and the round-2
observation fixed: the funnel's lower-bound marks follow the selected scope.

Fixtures: PR-ADS-178B's (``_seed``: the PR-ADS-177A cohort, three won deals of
one company, spend), seeded through the production writers.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, live_db,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import WINDOW  # noqa: E402
from tests.test_pr_ads_178b_pipeline_pg_integration import (  # noqa: E402
    _json, _pipeline, _render, _seed,
)


def _readings(report, metric):
    by = {m["metric"]: m for m in report["metrics"]}
    return by[metric], {r["consumer"]: r for r in by[metric]["readings"]}


# ── 1. the parity port: the independent leg is the Pipeline block now ───────
def test_01_parity_reads_the_pipeline_customers_won_block_as_its_independent_consumer(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from services import canonical_contract
    from services import cross_page_parity_service as parity
    consumer = parity.CONSUMER_PIPELINE_CUSTOMERS_WON
    assert consumer in parity.CERTIFIED_CONSUMERS
    assert "leads/customers_won" not in parity.CERTIFIED_CONSUMERS
    for metric in ("customers", "closed_won_deals"):
        assert parity.expected_source(parity.METRIC_IDENTITIES[metric], consumer) \
            == canonical_contract.SOURCE_CANONICAL_DEAL_LEDGER
    report = parity.audit_window(WINDOW)
    for metric, want in (("customers", 1), ("closed_won_deals", 3)):
        m, readings = _readings(report, metric)
        r = readings[consumer]
        assert r["value"] == want, (metric, r)
        assert r["declared_source"] == canonical_contract.SOURCE_CANONICAL_DEAL_LEDGER
        assert r["contract_problem"] is None
        # The mart-fed readings beside it say the same, and the identity holds.
        for other in ("pipeline", "dashboard/deals", "revenue_decision_mart"):
            assert readings[other]["value"] == want, (metric, other)
        assert m["status"] == "identical", (metric, m["status"])
    assert not [v for v in report["violations"] if v.get("consumer") == consumer]
    # The block parity read is the Pipeline payload's own, and it is the
    # reader contract's verdict read in that payload's snapshot.
    p = _pipeline()
    cw = p["customers_won"]
    assert (cw["customers"]["value"], cw["closed_won_deals"]["value"]) == (1, 3)
    from services import canonical_revenue_service as crs
    base = crs.load_won_deals(WINDOW)
    assert cw["customers"]["value"] == base["canonical_metrics"]["customers"]["value"]
    assert cw["closed_won_deals"]["value"] == \
        base["canonical_metrics"]["closed_won_deals"]["value"]
    assert cw["metric_family"] == "closed_won_customers"
    assert cw["window"]["key"] == p["window"]["key"] == WINDOW


@pytest.mark.parametrize("side", ["block", "mart"])
def test_02_the_port_can_fail_either_side_that_disagrees_is_a_value_mismatch(
        live_db, monkeypatch, side):
    """The leg is independent: a wrong reader reading (the block) or a wrong
    mart summary (every page that republishes it) breaks the identity."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from services import cross_page_parity_service as parity
    from services import pipeline_service
    if side == "block":
        real = pipeline_service._reader_verdict

        def inflated(base, name):
            m = real(base, name)
            return {**m, "value": m["value"] + 1} if name == "customers" else m
        monkeypatch.setattr(pipeline_service, "_reader_verdict", inflated)
    else:
        from services import revenue_decision_mart as mart
        real_build = mart.build_revenue_decision_mart

        def inflated(*a, **k):
            out = real_build(*a, **k)
            metrics = (out.get("summary") or {}).get("metrics") or {}
            if (metrics.get("customers") or {}).get("value") is not None:
                metrics["customers"] = {**metrics["customers"],
                                        "value": metrics["customers"]["value"] + 1}
                out["summary"]["customers"] = metrics["customers"]["value"]
            return out
        monkeypatch.setattr(mart, "build_revenue_decision_mart", inflated)
        monkeypatch.setattr(pipeline_service, "build_revenue_decision_mart", inflated)
    report = parity.audit_window(WINDOW)
    m, readings = _readings(report, "customers")
    block = readings[parity.CONSUMER_PIPELINE_CUSTOMERS_WON]["value"]
    pipeline = readings["pipeline"]["value"]
    assert block != pipeline, (block, pipeline)
    assert m["status"] != "identical"
    assert parity.V_VALUE_MISMATCH in report["violation_codes"]


# ── 2. the retired routes on a seeded, signed-in app; the live ones serve ───
def test_03_the_retired_routes_answer_404_signed_in_and_the_pipeline_serves_their_figures(
        live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from fastapi.testclient import TestClient
    from api.auth import set_session
    from api.server import app
    from starlette.responses import Response as StarletteResponse
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    client = TestClient(app, raise_server_exceptions=False)
    r = StarletteResponse()
    set_session(r, "viewer", "viewer")
    cookie = next(p.split("=", 1)[1] for p in r.headers.get("set-cookie", "").split(";")
                  if p.strip().startswith("ads_session="))
    client.cookies.set("ads_session", cookie)
    for path in ("/api/revenue-deals", "/api/crm-funnel/customers-won"):
        assert client.get(f"{path}?window={WINDOW}").status_code == 404, path
    page = client.get(f"/api/pipeline?window={WINDOW}")
    assert page.status_code == 200
    body = page.json()
    # What the retired routes published, on the live page: every closed-won
    # deal (the ledger's list) and customers won (the reader's verdicts + list).
    assert [d["deal_id"] for d in body["deals"]] == ["d-today", "d-org", "d-uk1"]
    assert body["closed_won_population"]["value"] == 3
    assert body["customers_won"]["customers"]["value"] == 1
    assert body["customers_won"]["rows"][0]["company_name"] == "Acme Freight"
    dash = client.get(f"/api/dashboard/deals?window={WINDOW}")
    assert dash.status_code == 200 and dash.json()["surface"] == "dashboard_deals"
    assert client.get(f"/api/crm-funnel/contacts?window={WINDOW}&event=sql").status_code == 200


# ── 3. the certification audit runs clean, with the retirement check ────────
def test_04_the_reader_cutover_audit_runs_clean_and_holds_the_retirement(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    from scripts import audit_customer_revenue_reader_cutover as rc
    a, report = rc.run()
    assert a.checks.get("retired_readers_absent") is True
    assert a.checks.get("inventory_complete") is True
    assert not [v for v in a.violations if "pipeline" in v or "retired" in v], a.violations
    consumers = {row["consumer"] for row in rc.INVENTORY}
    assert "services/canonical_crm_funnel_service.py" not in consumers
    assert "services/pipeline_service.py" in consumers


# ── 4. the round-2 observation: lower bounds follow the selected scope ──────
def _undated_contacts():
    """Two contacts beside the 177A cohort, through the production writer:

    * c-gx  Google Ads (``brand - uk``), now at Opportunity, dated Lead / MQL /
            SQL yesterday, NO Opportunity-entry date: an Opportunity gap for
            Google Ads AND all sources;
    * c-ox  ORGANIC, now at SQL, dated Lead / MQL yesterday, NO SQL-entry date:
            an SQL gap for all sources only.

    (The 177A cohort's own undated contacts carry no in-window date at all,
    so the funnel never reads them; its c-ae — SQL dated, Lead / MQL not —
    adds Lead and MQL gaps for Google Ads.)"""
    from db import writers
    from tests.test_pr_ads_177a_one_sql_count_pg_integration import _yesterday_at
    y = _yesterday_at()
    base = {"created_at": y, "last_modified_at": y, "date_entered_lead": y,
            "date_entered_mql": y}
    writers.upsert_hubspot_contact_funnel([
        {**base, "contact_id": "c-gx", "lifecycle_stage": "opportunity",
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "brand - uk",
         "date_entered_sql": y},
        {**base, "contact_id": "c-ox", "lifecycle_stage": "salesqualifiedlead",
         "hs_analytics_source": "ORGANIC_SEARCH"},
    ])


def test_05_the_funnel_marks_a_lower_bound_only_for_stages_the_selected_scope_is_missing(
        live_db, monkeypatch):
    """Under All Sources the SQL stage is a lower bound (the organic c-ox);
    under Google Ads-source the window's OWN rows have no SQL gap — 178B
    marked it there from the all-source gap, 178C stopped — while its own
    Opportunity gap (c-gx) stays marked.

    PR-ADS-180C (docs/65): the window's rows are not every contact that could
    belong to the window. The 177A cohort's c-nolb is Google Ads, at SQL,
    with NO SQL-entry date and created in the window: it may have entered SQL
    in it, and the rows never see it (no stage date in the window). So
    Google Ads-source SQL was never exact here; it is a lower bound naming
    the all-source figure. Every Keyword-attributable stage is a bound too,
    but that is OVER-MARKING, accepted per docs/65 §6: no contact here
    carries a keyword, so 178C's exact keyword counts were true — the
    all-source undated reach cannot say how many of its contacts a scope
    holds, and the stage fails safe. Where nothing lies outside the rows,
    the scope's own gap decides alone — test_pr_ads_180c PG case 03."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _seed(live_db)
    _undated_contacts()
    from services import canonical_crm_funnel_service as crm_funnel
    raw = crm_funnel.build("business", WINDOW)
    assert raw["reconciliation"]["status"] == "partial"
    by_scope = raw["coverage"]["stage_reached_without_entry_date_by_scope"]
    whole = raw["coverage"]["stage_reached_without_entry_date"]
    assert whole["sql"] == by_scope["sql"]["all_source"] == 1          # c-ox
    assert by_scope["sql"]["google_ads_source"] == 0
    assert whole["opportunity"] == by_scope["opportunity"]["google_ads_source"] == 1
    for e in whole:
        assert by_scope[e]["all_source"] == whole[e], e
        assert by_scope[e]["keyword_attributable"] == 0, e

    every = _pipeline()
    google = _pipeline(scope="google_ads_source")
    keyword = _pipeline(scope="keyword_attributable")
    assert every["funnel"]["coverage"] == {
        "stage_reached_without_entry_date": whole, "scope": "all_source"}
    assert google["funnel"]["coverage"]["stage_reached_without_entry_date"]["sql"] == 0

    def bounds(payload):
        return re.findall(r'data-lower-bound="([a-z]+)"', _render(payload))
    assert "sql" in bounds(every) and "opportunity" in bounds(every)
    assert "opportunity" in bounds(google)
    # PR-ADS-180C: undated contacts outside the window's rows (c-nolb, c-fj,
    # c-uk: Google Ads, at SQL or later, no SQL-entry date) bound the scope's
    # SQL count; how many of them the filter selects is not measured.
    reach = google["funnel"]["undated_reach"]
    assert reach["outside_window_rows"]["sql"] > 0
    assert google["funnel"]["events"]["sql"]["undated_contacts_could_belong"] is None
    assert google["funnel"]["events"]["sql"]["count_is_bound"] is True
    assert "sql" in bounds(google)
    assert "how many of them the lead filters select is not measured" in _render(google)
    # Over-marking, accepted per docs/65 §6 (no keyword contact exists; the
    # all-source reach cannot be split by scope): fails safe.
    assert bounds(keyword) == ["lead", "mql", "sql", "opportunity", "customer"]
    # The deal figures are untouched by the scope (a lead filter).
    assert google["kpis"] == keyword["kpis"] == every["kpis"]
