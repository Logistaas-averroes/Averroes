"""PR-ADS-177A-F2 — truth audit round 2 on the one SQL count.

Each case names the finding it closes. Every case fails on ``7ad983f``
(PR-ADS-177A-F1) or under the mutation its docstring names; ``docs/59`` §11
records the evidence.

* MAJOR — F1 made the acquisition cohort's verdict the revenue audit's
  ``lead_window_safe``: Revenue Health then said "Revenue is not ready",
  "Lead date grain: Unsafe (sync date)" and "Run Lead Event-Date
  Reconciliation" whenever the cohort was withheld. A withheld cohort blocks
  no page (owner decision). The cohort is now its own check.
* MINOR 1 — three renderers printed a count around the gate or under "SQLs".
* MINOR 2 — the campaign-view parity violation was untested wiring.
* MINOR 3 — on a window's first day the parity audit reported six false
  violations for the settled-day consumers' `no_settled_day_in_window`.
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
from tests.test_pr_ads_177a_f1_truth_audit import _cohort  # noqa: E402

APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
_BARE_SQL = re.compile(r"\bSQLs?\b")


def _render(script):
    return _run_app('_currentUser = {"username": "u", "role": "admin"}; ' + script)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _fn(name: str) -> str:
    i = APP.index(f"\nfunction {name}(")
    return APP[i:APP.index("\n}\n", i)]


def _literals(src: str) -> list[str]:
    """The string and template literals of ``src``, comments removed, with
    `${…}` interpolations dropped (their text is code, not copy)."""
    code = re.sub(r"(?m)^\s*//.*$", "", src)
    return [re.sub(r"\$\{[^}]*\}", "", a or b)
            for a, b in re.findall(r'"([^"\n]*)"|`([^`]*)`', code)]


# ══ MAJOR — a withheld cohort is not "revenue is not ready" ═════════════════
def _audit_payload(cohort_sync_row=None):
    """The REAL /api/revenue-attribution/audit payload: an available deal ledger
    (the canonical fixture) and the cohort fed through the real `assess`."""
    import tests.test_revenue_attribution_audit as t
    return json.loads(json.dumps(t._audit(cohort_sync_row=cohort_sync_row), default=str))


def test_a_withheld_cohort_is_reported_apart_from_the_revenue_verdict():
    from tests.cohort_fixture import withheld_sync_row
    import tests.test_revenue_attribution_audit as t
    a = _audit_payload(withheld_sync_row(t.NOW))
    assert a["canonical_revenue_status"] == "canonical_available"
    assert a["verdict"] == "SAFE" and a["blockers"] == []
    lc = a["lead_cohort"]
    assert (lc["status"], lc["reason"], lc["blocks_revenue"]) == (
        "withheld", "source_bootstrap_incomplete", False)
    assert lc["next_step"] == "contact_funnel_sync"
    assert "lead_window_safe" not in a["date_grain_health"]
    # Each comparison window carries its own cohort, for the gate.
    assert set(a["window_cohorts"]) == {"current_quarter", "ytd", "all_time"}


def test_b_revenue_health_with_a_withheld_cohort_is_revenue_ready_and_names_the_cohort():
    from tests.cohort_fixture import withheld_sync_row
    import tests.test_revenue_attribution_audit as t
    a = _audit_payload(withheld_sync_row(t.NOW))
    html = _render("renderRevenueHealth(%s)" % json.dumps(a))
    assert "Revenue is not ready" not in html
    assert "Revenue Ready" in html
    assert "Lead Event-Date Reconciliation" not in html
    assert "sync date" not in html and "Lead date grain" not in html
    chip = html[html.index("Leads / SQLs (acquisition cohort)"):]
    chip = chip[:chip.index("</div>")]
    assert "Withheld" in chip and "backfill is still running" in chip  # the reason
    assert "contact-funnel sync" in html                               # its next step
    # The comparison table's leads go through the gate: withheld, never a count.
    table = html[html.index("Window comparison"):]
    assert "Withheld" in table


def test_c_revenue_health_with_a_published_cohort_says_published():
    a = _audit_payload()
    assert a["lead_cohort"]["published"] is True
    html = _render("renderRevenueHealth(%s)" % json.dumps(a))
    chip = html[html.index("Leads / SQLs (acquisition cohort)"):]
    assert "Published" in chip[:chip.index("</div>")]
    assert "Revenue Ready" in html and "contact-funnel sync" not in html


def test_d_revenue_health_window_leads_are_gated_on_each_windows_own_cohort():
    a = {"verdict": "SAFE", "canonical_revenue_status": "canonical_available",
         "revenue_window_status": "has_revenue", "lead_cohort": {"published": True},
         "window_comparison": {"current_quarter": {"leads": 7}, "ytd": {"leads": 9},
                               "all_time": {"leads": None}},
         "window_cohorts": {"current_quarter": _cohort(stale=True), "ytd": _cohort(),
                            "all_time": _cohort(status="withheld",
                                                reason="source_bootstrap_incomplete")}}
    html = _render("renderRevenueHealth(%s)" % json.dumps(a))
    table = html[html.index("Window comparison"):]
    rows = table.split("<tr>")[2:]
    assert "7" in rows[0] and "measured as of" in rows[0]
    assert "9" in rows[1] and "measured as of" not in rows[1]
    assert "Withheld" in rows[2]


# ══ MINOR 1 — no renderer around the gate, no "SQLs" over lead status ══════
def test_e_the_revenue_tab_rate_card_prints_the_cohort_through_the_gate():
    out = _render(
        "const k = {sqls: 4, sql_to_customer_rate: null, sql_to_customer_rate_status:"
        " 'unavailable', metrics: {}};"
        "[renderRevKpiRow({cohort: %s, kpis: k, period_change: {}, window: {}}),"
        " renderRevKpiRow({cohort: %s, kpis: {...k, sqls: null}, period_change: {}, window: {}})]"
        % (json.dumps(_cohort(stale=True)),
           json.dumps(_cohort(status="withheld", reason="source_bootstrap_incomplete"))))
    stale, withheld = out
    card = stale[stale.index('data-kpi="rate"'):]
    assert "measured as of" in card and "through 24 Nov 2026" in card
    card = withheld[withheld.index('data-kpi="rate"'):]
    assert "Withheld" in card and "backfill is still running" in card


def test_f_the_channels_momentum_toggle_and_every_channels_label_say_lead_status():
    label = _render('CHAN_METRICS.find((m) => m.key === "sqls").label')
    assert label == "Qualified (lead status)"
    # JS constants and labels, not only payload strings: every string literal
    # in the Channels renderers and CHAN_METRICS (all lead-status counts).
    i = APP.index("const CHAN_METRICS = [")
    sources = [APP[i:APP.index("];", i)]] + [
        _fn(n) for n in ("renderChanKpiRow", "renderChanMomentumPanel",
                         "chanMomentumChartSVG", "renderChanMixPanel",
                         "renderChanQualityPanel", "renderChanPlatformMatrix",
                         "renderChanChannelList", "wireChanChannelDrawers")]
    for src in sources:
        for text in _literals(src):
            assert not _BARE_SQL.search(text), text[:120]


def test_g_the_deals_legacy_renderers_carry_no_sql_literal():
    """The JS behind every legacy lead-status count on Deals (not only what a
    fixture happens to render)."""
    for name in ("renderDealLeadStatusBacklog", "renderDealSourcePipeline",
                 "renderDealSqlNoDeal"):
        for text in _literals(_fn(name)):
            assert not _BARE_SQL.search(text), (name, text[:120])


#: Every function that may print a lead / SQL count with a bare formatter: the
#: count there is NOT the acquisition cohort (lead-status Channels / Deals
#: source rows, the mapping workbench's legacy rows) or is already behind
#: Campaign Evidence's own row gate.
_BARE_FORMAT_ALLOWED = {
    "renderChanKpiRow", "renderChanPlatformMatrix", "renderChanChannelList",
    "wireChanChannelDrawers", "renderChanQualityPanel", "renderDealSourcePipeline",
    "renderCampaignMappingCard", "renderCampaignEvidenceRow",
}


def test_h_no_cohort_count_is_formatted_around_the_gate():
    """The sweep, as a guard: a lead / SQL count field formatted by
    `fmtCount` / `dashValue` outside the allow-list is a count printed around
    `cohortCountHtml`."""
    pat = re.compile(r"(fmtCount|dashValue)\(\s*[A-Za-z_.]*\b(sqls|leads|sqls_residual|"
                     r"leads_residual|campaign_row_cohort_sqls|google_ads_cohort_sqls|"
                     r"cohort_sqls)\b")
    offenders = []
    for m in pat.finditer(APP):
        head = APP.rfind("\nfunction ", 0, m.start())
        name = APP[head + 10:APP.index("(", head + 10)]
        if name not in _BARE_FORMAT_ALLOWED:
            line = APP.count("\n", 0, m.start()) + 1
            offenders.append(f"{name}:{line}")
    assert offenders == [], offenders


# ══ MINOR 2 — the campaign-view parity violation is wired ═══════════════════
def _healthy_parity_at(monkeypatch, now, *, mutate=None):
    """The 154c_f3 "fully healthy" scenario, audited at ``now``: the 138
    fixture, its contacts created two hours before ``now``, one priced won
    deal closed an hour before it, a current deal ledger."""
    import tests.test_pr_ads_138_dashboard_countries as t138
    import tests.test_pr_ads_154c_f3_country_sql_and_revenue_gate as f3
    from services import cross_page_parity_service as parity
    from tests.canonical_ledger_fixtures import (company_evidence, fresh_sync_state,
                                                 patch_canonical_ledger)
    from tests.cohort_fixture import contacts_from_lead_rows, patch_cohort
    t138._patch_durable(monkeypatch)
    patch_cohort(monkeypatch, contacts_from_lead_rows(
        t138.LEAD_ROWS, created=now - timedelta(hours=2)), as_of=now)
    rows = [{**f3._deal(1), "deal_close_date": (now - timedelta(hours=1)).isoformat()}]
    patch_canonical_ledger(
        monkeypatch, rows,
        sync_state={**fresh_sync_state(now),
                    "bootstrap_started_at": "2025-12-01T00:00:00+00:00",
                    "bootstrap_completed_at": "2025-12-01T06:00:00+00:00"},
        company_associations=company_evidence(rows))

    def build(w, n):
        built = f3._with_synced_lifecycle(f3._ORIGINAL_BUILD(w, n))
        return mutate(built) if mutate else built
    monkeypatch.setattr(parity, "_build_consumers", build)
    return parity


def test_i_a_broken_campaign_view_is_a_cohort_sql_parity_violation(monkeypatch):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    parity = _healthy_parity_at(monkeypatch, now)
    healthy = parity.audit_window("current_quarter", now=now)
    assert healthy["cohort_sql_parity"]["campaign_view"]["status"] == "identical"
    assert "cohort_sql_parity_broken" not in healthy["violation_codes"]

    def off_by_one(built):
        k = built["dashboard/overview"]["payload"]["kpis"]
        k["google_ads_cohort_sqls"] = (k["google_ads_cohort_sqls"] or 0) + 1
        return built
    parity = _healthy_parity_at(monkeypatch, now, mutate=off_by_one)
    r = parity.audit_window("current_quarter", now=now)
    assert r["cohort_sql_parity"]["campaign_view"]["status"] == "broken"
    assert r["cohort_sql_parity"]["status"] != "broken"   # the settled group holds
    assert "cohort_sql_parity_broken" in r["violation_codes"]


# ══ MINOR 3 — a window's first day is not six violations ═══════════════════
_FIRST_DAYS = [("current_quarter", datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)),
               ("ytd", datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))]


@pytest.mark.parametrize("window,now", _FIRST_DAYS)
def test_j_a_windows_first_day_reports_no_violation(monkeypatch, window, now):
    parity = _healthy_parity_at(monkeypatch, now)
    r = parity.audit_window(window, now=now)
    assert r["ok"] is True, r["violation_codes"]
    by = {m["metric"]: m for m in r["metrics"]}
    for key in ("country_attributed_spend_usd", "country_attributed_won_revenue_usd",
                "country_attributed_closed_won_deals", "country_attributed_sqls",
                "country_unattributed_residual_sqls"):
        assert by[key]["status"] == "not_evaluable_no_settled_day", (key, by[key]["status"])
    cert = {c["consumer"]: c for c in r["consumer_certification"]}
    assert cert["dashboard/countries"]["certified"] is False      # nothing was checked
    assert cert["dashboard/countries"]["not_evaluable_reason"] == "no_settled_day_in_window"


def test_k_counterfactual_another_reason_on_the_first_day_is_still_a_violation(
        monkeypatch):
    """Only a UNANIMOUS `no_settled_day_in_window` is exempt: one consumer
    declaring anything else keeps the identity a violation."""
    window, now = _FIRST_DAYS[0]

    def other_reason(built):
        truth = built["dashboard/countries"]["payload"]["metric_truth"]
        truth["country_attributed_sqls"] = {**truth["country_attributed_sqls"],
                                            "unavailable_reason": "something_else"}
        return built
    parity = _healthy_parity_at(monkeypatch, now, mutate=other_reason)
    r = parity.audit_window(window, now=now)
    assert "canonical_source_unavailable" in r["violation_codes"]
    by = {m["metric"]: m for m in r["metrics"]}
    assert by["country_attributed_sqls"]["status"] == "unavailable"
    # And the consumer is no longer wholly "no settled day": uncertified again.
    assert "consumer_certified_nothing" in r["violation_codes"]
    # The four identities that still say only `no_settled_day_in_window` stay
    # exempt — the exemption is per identity, never page-wide.
    assert by["country_attributed_spend_usd"]["status"] == "not_evaluable_no_settled_day"
