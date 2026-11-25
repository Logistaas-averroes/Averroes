"""PR-ADS-177A-F1 — truth audit round 1 on the one SQL count.

Each case names the finding it closes. Every case fails on ``e7b5bc4``
(PR-ADS-177A) or under the mutation its docstring names; ``docs/59`` §10
records the evidence.

* MAJOR 1 — a cohort published over a STALE contact funnel rendered as a bare
  number on every mart / Dashboard surface. It now says "measured as of …"
  (Campaign Evidence's own ``campaignCohortAsOf``), and the cohort audit fails
  a stale payload that does not declare it.
* MAJOR 2 — Dashboard Deals and Channels printed the LEGACY lead-status count
  under the word "SQL"/"SQLs", and the Deals funnel ran the cohort SQL stage
  straight into a legacy "SQLs Not Yet Closed-Won" stage.
* MINOR 1 — each cohort count says the last day it counts; the cross-page
  parity audit compares the campaign view's days like for like too.
* MINOR 2 — no period delta on a still-maturing (open) window.
* MINOR 3 — ``run()`` wiring, the no-mart-residual branch.
* MINOR 4 — Overview keys named for what they hold.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app  # noqa: E402

APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
AS_OF = "2026-11-22T06:00:00+00:00"


def _render(script):
    return _run_app('_currentUser = {"username": "u", "role": "admin"}; ' + script)


def _cohort(*, stale=False, status="published", reason=None, end_date="2026-11-24"):
    """A mart cohort block as `_cohort_contract_block` emits it."""
    return {
        "metric_family": "acquisition_cohort_outcomes",
        "window_basis": "contact_created_at",
        "sql_status": status, "sql_reason": reason,
        "metadata": {
            "as_of": AS_OF,
            "source_freshness": {"fresh": not stale,
                                 "reason": "source_stale" if stale else "source_fresh"},
            "maturity_note": "A cohort keeps maturing after its window closes.",
        },
        "window": {"start_date": "2026-10-01", "end_date": end_date},
        "stale": bool(stale and status == "published"),
        "as_of": AS_OF,
        "totals": {"leads": 9, "sqls": 4},
        "campaign_residual": {"leads": 1, "sqls": 1},
    }


# ══ MAJOR 1 — a stale cohort is a count AS OF its watermark ═════════════════
def test_m1_a_the_one_gate_prints_measured_as_of_beside_a_stale_count():
    out = _render("[cohortCountHtml(4, %s, null), cohortCountHtml(4, %s, null)]"
                  % (json.dumps(_cohort(stale=True)), json.dumps(_cohort())))
    stale, fresh = out
    assert stale.startswith("4") and "measured as of" in stale
    # Campaign Evidence's own helper formats it — one helper, not two.
    assert _render("campaignCohortAsOf(%s)" % json.dumps(_cohort(stale=True))) in stale
    assert "measured as of" not in fresh


def test_m1_b_every_dashboard_kpi_and_the_countries_page_say_it_when_stale():
    c = json.dumps(_cohort(stale=True))
    out = _render(
        "const c = %s; const k = {google_ads_cohort_sqls: 4, campaign_row_cohort_sqls: 3,"
        " sqls: 4, sqls_residual: 1, leads: 9, metrics: {}};"
        "const d = {cohort: c, kpis: k, period_change: {}, window: {is_closed_window: false}};"
        # PR-ADS-178B: Dashboard → Deals renders the Pipeline KPI cards,
        # whose figures are {value, status, reason}.
        "const dd = {cohort: c, kpis: {sqls: {value: 4, status: 'published', reason: null}},"
        " window: {is_closed_window: false}};"
        "const page = {cohort: c, country_totals: {sqls: 4, leads: 9, sqls_status: 'published',"
        " metrics: {sqls: {value: 4, status: 'published', reason: null}}}, rows: [], summary: {},"
        " regional_mix: [{region: 'Europe', sqls: 4, metrics: {sqls: {value: 4, status:"
        " 'published', reason: null}}}]};"
        "[renderDashKpiRow(d), renderCampKpiRow(d), renderCtryKpiRow(d), pipelineKpiCardsHtml(dd, null),"
        " renderRoasCountrySummary(page), renderCountriesRegionalMix(page)]" % c)
    names = ["overview", "campaigns", "countries", "deals", "countries page", "regional mix"]
    for name, html in zip(names, out):
        assert "measured as of" in html, name


def _stale_payload(**over):
    c = _cohort(stale=True)
    c.update(over)
    return {"cohort": c, "summary": {"leads": 9, "sqls": 4}}


def test_m1_c_the_cohort_audit_fails_a_stale_published_payload_without_as_of():
    from scripts import audit_marketing_outcome_cohorts as audit
    assert audit.mart_stale_disclosure_problems(_stale_payload()) == []
    assert audit.mart_stale_disclosure_problems(_stale_payload(stale=False))
    assert audit.mart_stale_disclosure_problems(_stale_payload(as_of=None))
    bad = _stale_payload()
    bad["cohort"]["metadata"] = {**bad["cohort"]["metadata"], "as_of": None}
    assert audit.mart_stale_disclosure_problems(bad)
    a = audit.Audit()
    audit.audit_mart_window(a, window="current_quarter",
                            payloads={"dashboard/overview": _stale_payload(as_of=None)})
    assert any("stale_disclosed" in v for v in map(str, a.violations)), a.violations


def test_m1_d_the_mart_block_declares_stale_and_as_of(monkeypatch):
    """Through the real cohort read and the REAL freshness `assess`: a contact
    sync that last succeeded 48h ago is `source_stale` — published, as of its
    watermark, and the mart block says so."""
    from datetime import date
    from services import revenue_attribution_service as ras
    from tests.cohort_fixture import contacts_from_lead_rows, healthy_sync_row, patch_cohort
    now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    stale_at = now - timedelta(hours=48)
    patch_cohort(monkeypatch, contacts_from_lead_rows(
        [{"status_category": "qualified", "campaign_name": "Brand - UK"},
         {"status_category": "lead", "campaign_name": "Brand - UK"}],
        created=datetime(2026, 11, 2, 12, tzinfo=timezone.utc)), as_of=now,
        sync_row={**healthy_sync_row(now), "last_successful_incremental_at": stale_at,
                  "last_incremental_at": stale_at})
    start, end = date(2026, 10, 1), date(2026, 11, 24)
    outcomes = ras._read_cohort(start, end, now=now,
                                resolve_label=ras._mart_label_resolver({}, set()))
    block = ras._cohort_contract_block(outcomes, {}, start_date=start, end_date=end)
    assert (block["sql_status"], block["metadata"]["source_freshness"]["reason"]) == (
        "published", "source_stale")
    assert block["stale"] is True
    assert block["as_of"] and block["as_of"] == block["metadata"]["as_of"]
    assert block["window"] == {"start_date": "2026-10-01", "end_date": "2026-11-24"}
    from scripts import audit_marketing_outcome_cohorts as audit
    assert audit.mart_stale_disclosure_problems(
        json.loads(json.dumps({"cohort": block}, default=str))) == []


# ══ MAJOR 2 — no legacy count under the word "SQL" ═════════════════════════
_BARE_SQL = re.compile(r"\bSQLs?\b")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _deals_payload(monkeypatch, *, withheld=False):
    import tests.test_pr_ads_139_dashboard_deals as t139
    t139._patch_durable(monkeypatch)
    if withheld:
        from tests.cohort_fixture import (contacts_from_lead_rows, patch_cohort,
                                          withheld_sync_row)
        patch_cohort(monkeypatch, contacts_from_lead_rows(
            t139.LEAD_ROWS, created=datetime(2026, 5, 15, 12, tzinfo=timezone.utc)),
            as_of=t139.NOW, sync_row=withheld_sync_row(t139.NOW))
    from services.dashboard_deals_service import build_dashboard_deals
    return json.loads(json.dumps(build_dashboard_deals(t139.WINDOW, now=t139.NOW), default=str))


def test_m2_a_the_deals_funnel_holds_no_legacy_stage_and_the_backlog_is_its_own_block(
        monkeypatch):
    # PR-ADS-178B (owner decision 4): the legacy lead-status backlog is
    # dropped, not kept beside the funnel; Dashboard → Deals publishes the
    # Pipeline builder, whose funnel is the lifecycle stage-entry funnel.
    d = _deals_payload(monkeypatch)
    assert "lead_status_backlog" not in d
    assert "lead_status_qualified_no_won_deal" not in json.dumps(d)
    assert "sqls_not_closed_won" not in json.dumps(d)
    assert d["funnel"]["label"] == "Entered stage in window"
    assert not any("Not Yet" in str(k) for k in (d["funnel"].get("events") or {}))


def test_m2_b_the_real_deals_payload_never_puts_a_legacy_count_under_sql(monkeypatch):
    d = _deals_payload(monkeypatch)
    # PR-ADS-178B: the tab's real payload through the renderers it draws.
    out = _render(
        "const d = %s; [pipelineKpiCardsHtml(d, d.period_change),"
        " pipelineSourceBreakdownHtml(d), pipelineDealsHtml(d),"
        " pipelineCustomersWonHtml(d), pipelineFunnelHtml(d)]" % json.dumps(d))
    kpis, source, deals, customers, funnel = out
    # No legacy lead-status count is drawn at all.
    for name, html in (("kpis", kpis), ("source", source), ("deals", deals),
                       ("customers", customers), ("funnel", funnel)):
        assert "Qualified (lead status)" not in html, name
    # The deal blocks carry no "SQL" word; the KPI's SQL figure is the cohort,
    # under the cohort label, and the funnel never says a bare "SQLs".
    for name, html in (("source", source), ("deals", deals), ("customers", customers)):
        assert not _BARE_SQL.search(_text(html)), (name, _text(html)[:600])
    assert "SQLs · contacts acquired in window" in kpis
    assert not re.search(r"\bSQLs\b", _text(funnel).replace(
        "a different metric from the SQL card above", ""))


def test_m2_c_under_a_withheld_cohort_the_funnel_says_withheld_and_the_reason(
        monkeypatch):
    # PR-ADS-178B: the tab's SQL figure is the Pipeline KPI card (the deal
    # funnel is gone); under a withheld cohort it says Withheld and why.
    d = _deals_payload(monkeypatch, withheld=True)
    sqls = d["kpis"]["sqls"]
    assert (sqls["value"], sqls["status"]) == (None, "withheld")
    assert sqls["reason"] == "source_bootstrap_incomplete"
    assert d["cohort"]["sql_reason"] == "source_bootstrap_incomplete"
    kpis = _render("const d = %s; pipelineKpiCardsHtml(d, null)" % json.dumps(d))
    first = kpis[kpis.index('data-kpi="sqls"'):]
    first = first[:first.index("dash-kpi-card__sub")]
    assert "Withheld" in first and "Stage unavailable" not in first
    assert "Unavailable" not in first.replace("dash-unavailable", "")
    assert "backfill is still running" in first        # the reason, in words


def _channels_payload(monkeypatch):
    import tests.test_pr_ads_136_dashboard_channels as t136
    return json.loads(json.dumps(t136._channels(monkeypatch), default=str))


def test_m2_d_the_real_channels_payload_never_says_sql_over_its_lead_status_counts(
        monkeypatch):
    d = _channels_payload(monkeypatch)
    assert d["kpis"]["total_sqls_label"] == "Qualified (lead status)"
    assert d["kpis"]["total_sqls_metric_family"] == "legacy_lead_status_category"
    out = _render(
        "const d = %s; [renderChanKpiRow(d), renderChanQualityPanel(d),"
        " renderChanPlatformMatrix(d), renderChanChannelList(d), renderDashDecisionCards(d)]"
        % json.dumps(d))
    for name, html in zip(("kpis", "quality", "matrix", "channel list", "cards"), out):
        assert not _BARE_SQL.search(_text(html)), (name, _BARE_SQL.findall(_text(html)))
    kpi = out[0][out[0].index('data-kpi="sqls"'):]
    assert "Qualified (lead status)" in kpi
    for card in d["decision_cards"]:
        assert not _BARE_SQL.search(card["headline"] + " " + card["body"]), card
    # The drawer is wired on click (DOM): its header names the same thing.
    i = APP.index("function wireChanChannelDrawers(")
    drawer = APP[i:APP.index("\n}\n", i)]
    assert "<th>SQLs</th>" not in drawer and "${LEAD_STATUS_QUALIFIED_LABEL}" in drawer
    assert "Platform-level SQLs" not in drawer


# ══ MINOR 1 — each count says the last day it counts ═══════════════════════
def test_n1_a_every_cohort_label_carries_its_window_end():
    camp = json.dumps(_cohort(end_date="2026-11-24"))
    ctry = json.dumps(_cohort(end_date="2026-11-23"))
    out = _render(
        "const k = {google_ads_cohort_sqls: 4, campaign_row_cohort_sqls: 3, sqls: 4,"
        " metrics: {}};"
        "const d = {cohort: %s, kpis: k, period_change: {}, window: {}};"
        "const dc = {cohort: %s, kpis: k, period_change: {}, window: {}};"
        # PR-ADS-178B: the Deals tab renders the Pipeline KPI cards.
        "const dd = {cohort: d.cohort, kpis: {sqls: {value: 4, status: 'published',"
        " reason: null}}, window: {}};"
        "const page = {cohort: %s, country_totals: {sqls: 4, leads: 9,"
        " metrics: {sqls: {value: 4, status: 'published', reason: null}}}, rows: [], summary: {}};"
        "[renderDashKpiRow(d), renderCampKpiRow(d), pipelineKpiCardsHtml(dd, null), renderCtryKpiRow(dc),"
        " renderRoasCountrySummary(page)]" % (camp, ctry, ctry))
    for name, html in zip(("overview", "campaigns", "deals"), out[:3]):
        assert "through 24 Nov 2026" in html, name
    for name, html in zip(("dashboard countries", "countries page"), out[3:]):
        assert "through 23 Nov 2026" in html, name


def test_n1_b_parity_compares_the_campaign_view_days_like_for_like(monkeypatch):
    from services import cross_page_parity_service as parity
    # PR-ADS-177C: the independent leg is the campaign-ID cohort reading
    # (Campaign Evidence, its former source, is retired); the property is
    # unchanged — it is read over exactly the campaign view's days.
    seen = {}

    def _independent(start, end, now):
        seen["bounds"] = (start, end)
        return 4
    monkeypatch.setattr(parity, "_campaign_id_cohort_sqls", _independent)
    consumers = {
        "revenue_decision_mart": {"payload": {
            "cohort": {"sql_status": "published"}, "summary": {"sqls": 4},
            "window": {"start_date": "2026-10-01", "end_date": "2026-11-24"}}},
        "dashboard/overview": {"payload": {"kpis": {"google_ads_cohort_sqls": 4}}},
    }
    ok = parity._cohort_sql_parity_campaign_view(consumers, "current_quarter", None)
    assert ok["status"] == "identical", ok
    from datetime import date
    assert seen["bounds"] == (date(2026, 10, 1), date(2026, 11, 24))
    consumers["dashboard/overview"]["payload"]["kpis"]["google_ads_cohort_sqls"] = 5
    broken = parity._cohort_sql_parity_campaign_view(consumers, "current_quarter", None)
    assert broken["status"] == "broken"
    consumers["revenue_decision_mart"]["payload"]["cohort"]["sql_status"] = "withheld"
    consumers["dashboard/overview"]["payload"]["kpis"]["google_ads_cohort_sqls"] = None
    assert parity._cohort_sql_parity_campaign_view(
        consumers, "current_quarter", None)["status"] == "not_evaluable"


# ══ MINOR 2 — no delta on a still-maturing window ═════════════════════════
def test_n2_the_sql_delta_is_shown_only_for_a_completed_window():
    c = json.dumps(_cohort())
    pc = '{"available": true, "sqls": {"current": 4, "previous": 8, "delta": -4, "pct": -0.5}}'
    out = _render(
        "const c = %s; const pc = %s;"
        "[cohortDeltaChip('sqls', pc, {cohort: c, window: {is_closed_window: false}}),"
        " cohortDeltaChip('sqls', pc, {cohort: c, window: {is_closed_window: true}}),"
        " dashDeltaChip('sqls', pc)]" % (c, pc))
    open_w, closed_w, raw = out
    assert open_w != raw and "still maturing" in open_w
    assert "A cohort keeps maturing" in open_w        # the maturity note, beside it
    assert closed_w == raw
    # PR-ADS-178B: the Deals tab's KPI row is the Pipeline KPI cards.
    for fn in ("renderDashKpiRow", "renderCampKpiRow", "renderCtryKpiRow", "pipelineKpiCardsHtml"):
        i = APP.index(f"\nfunction {fn}(")
        body = APP[i:APP.index("\n}\n", i)]
        assert 'dashDeltaChip("sqls"' not in body, fn
        assert 'cohortDeltaChip("sqls", pc, d)' in body, fn


# ══ MINOR 3 — wiring ════════════════════════════════════════════════════════
def test_n3_a_run_audits_every_business_window_and_every_payload(monkeypatch):
    from analysis.business_windows import WINDOW_KEYS
    from scripts import audit_marketing_outcome_cohorts as audit
    import services.dashboard_campaigns_service as dcs
    import services.dashboard_countries_service as dco
    import services.dashboard_deals_service as dd
    import services.dashboard_overview_service as dov
    import services.dashboard_revenue_service as drv
    import services.revenue_decision_mart as mart

    calls = []

    def _payload(name):
        def build(*a, **k):
            calls.append((name, k.get("window", a[0] if a else None)))
            return {"cohort": _cohort(), "summary": {"leads": 9, "sqls": 4}}
        return build
    monkeypatch.setattr(mart, "build_revenue_decision_mart",
                        lambda **k: _payload("mart/" + k["view"]
                                             + ("_settled" if k.get("settled") else ""))(**k))
    monkeypatch.setattr(dov, "build_dashboard_overview", _payload("dashboard/overview"))
    monkeypatch.setattr(dcs, "build_dashboard_campaigns", _payload("dashboard/campaigns"))
    monkeypatch.setattr(dco, "build_dashboard_countries", _payload("dashboard/countries"))
    monkeypatch.setattr(drv, "build_dashboard_revenue", _payload("dashboard/revenue"))
    monkeypatch.setattr(dd, "build_dashboard_deals", _payload("dashboard/deals"))
    # PR-ADS-178B: the Pipeline page's builder is audited too.
    import services.pipeline_service as ps
    monkeypatch.setattr(ps, "build_pipeline", _payload("pipeline"))
    monkeypatch.setattr(audit, "independent_counts", lambda *a, **k: None)
    monkeypatch.setattr(audit, "global_population_split", lambda: None)
    _a, report = audit.run(now=datetime(2026, 11, 24, 12, tzinfo=timezone.utc))
    # PR-ADS-177B: the Campaigns page's settled campaign view is audited too.
    surfaces = {"mart/campaign", "mart/country", "mart/campaign_settled",
                "dashboard/overview",
                "dashboard/campaigns", "dashboard/countries", "dashboard/revenue",
                "dashboard/deals", "pipeline"}
    assert [w["window"] for w in report["business_windows"]] == list(WINDOW_KEYS)
    for w in report["business_windows"]:
        assert set(w["surfaces"]) == surfaces, w["window"]
        assert all(s["sql_status"] == "published" for s in w["surfaces"].values())
    assert {(n, w) for n, w in calls} == {(n, w) for n in surfaces for w in WINDOW_KEYS}


def test_n3_b_with_no_mart_residual_row_the_residual_is_zero_only_when_published():
    from services.dashboard_countries_service import _build_residual
    spend = {"native_currency": "GBP"}
    published = _build_residual({}, [], spend, None, False, False,
                                cohort={"sql_status": "published"})
    assert (published["sqls"], published["leads"]) == (0, 0)
    for status in ("withheld", "unavailable", None):
        out = _build_residual({}, [], spend, None, False, False,
                              cohort={"sql_status": status} if status else None)
        assert (out["sqls"], out["leads"]) == (None, None), status


# ══ MINOR 4 — keys named for what they hold ════════════════════════════════
def test_n4_the_overview_keys_name_the_cohort_not_another_scope():
    src = (ROOT / "services" / "dashboard_overview_service.py").read_text(encoding="utf-8")
    assert '"google_ads_cohort_sqls": summary.get("sqls")' in src
    assert '"campaign_row_cohort_sqls": _on_campaign_rows' in src
    assert '"campaign_attributable_sqls"' not in src
    assert '"google_ads_source_sqls"' not in src
    i = APP.index("\nfunction renderDashKpiRow(")
    body = APP[i:APP.index("\n}\n", i)]
    assert "k.campaign_attributable_sqls" not in body and "k.google_ads_source_sqls" not in body
