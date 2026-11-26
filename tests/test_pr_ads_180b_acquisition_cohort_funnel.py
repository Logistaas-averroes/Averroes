"""PR-ADS-180B — the acquisition-cohort funnel on Overview, Campaigns and
Countries; the Activity panel's two open findings from 180A (docs/64).

What is guarded here (the PostgreSQL half is
``test_pr_ads_180b_acquisition_cohort_funnel_pg_integration``):

1. **One producer.** The cohort's MQL / Opportunity / Customer-stage counts
   come from the cohort's own build (``marketing_outcome_cohort_service``),
   on the ONE stage-reach rule the Channels funnel counts by; the two agree
   contact for contact, and the bucket identities hold for every stage.
2. **The mart cohort contract** carries the stages beside the SQL count under
   the same verdict — withheld means absent — and the audits name a leak.
3. **Overview** draws the all-source acquisition cohort (the Lead-anchored
   panel is gone), republished from the same cohort build, under its verdict.
4. **Lower bounds.** A contact with no created date is in no window: while
   any exist (or the response does not say) every column is "at least N".
5. **Campaigns and Countries** each draw one block from their OWN response's
   cohort, through the cohort gate.
6. **Finding (a)**: a previous-period count that is a lower bound is no
   baseline — "No comparison", never "+X%".
7. **Finding (b)**: the Activity bound counts undated contacts OUTSIDE the
   window's rows too.

Every producer runs for real; the repository is replaced at its fetch
boundary only. Freshness goes through the real
``analysis.sql_coverage_freshness.assess``.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
SENTINEL = 7919
STAGE_FIELDS = ("reached_mql", "reached_opportunity", "reached_customer_stage")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _cells(html: str) -> dict:
    out = {}
    for m in re.finditer(r'<(div|button)[^>]*class="dash-funnel__stage[^"]*"[^>]*'
                         r'data-funnel-slot="([a-z]+)"[^>]*>(.*?)</\1>', html, re.S):
        out[m.group(2)] = _text(m.group(3))
    return out


def _fn(name: str, js: str = JS) -> str:
    m = re.search(r"\n(?:async )?function %s\(" % re.escape(name), js)
    assert m, f"function {name} not found"
    return js[m.start():js.index("\n}\n", m.start()) + 2]


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures: canonical rows as the repository returns them
# ═════════════════════════════════════════════════════════════════════════════
def _row(i, stage, *, src="PAID_SEARCH", label="brand - uk", created="2026-11-10T10:00:00Z",
         **dates):
    """One `fetch_acquisition_cohort_contacts` row. ``dates``: e.g.
    ``mql="2026-11-11"`` (direct) or ``mql_recovered="..."``."""
    r = {"funnel_row_id": i, "contact_id": f"c{i}", "created_at": created,
         "lifecycle_stage": stage, "hs_analytics_source": src,
         "hs_analytics_source_data_1": label, "gclid": None, "ip_country": None,
         "country": None}
    for e in ("mql", "sql", "opportunity", "customer"):
        r[f"{e}_entered_direct"] = dates.get(e)
        r[f"{e}_entered_recovered"] = dates.get(f"{e}_recovered")
    return r


def _rows():
    """Eight contacts created in the window:

    * c0 lead                                    — reached nothing
    * c1 MQL (stage only, no date)               — MQL
    * c2 SQL dated, stage moved back to lead      — SQL (by its date) only
    * c3 opportunity, no dates                   — MQL, SQL, Opportunity
    * c4 customer, MQL recovered                 — every stage
    * c5 organic, customer                       — every stage, NOT Google Ads
    * c6 lead stage but an MQL date               — MQL (by its date)
    * c7 opportunity, unmapped label             — MQL, SQL, Opportunity
    """
    return [
        _row(0, "lead"),
        _row(1, "marketingqualifiedlead"),
        _row(2, "lead", sql="2026-11-12"),
        _row(3, "opportunity"),
        _row(4, "customer", mql_recovered="2026-11-11"),
        _row(5, "customer", src="ORGANIC_SEARCH", label=None),
        _row(6, "lead", mql="2026-11-11"),
        _row(7, "opportunity", label="Mystery"),
    ]


def _resolver(label):
    if (label or "").strip().lower() == "brand - uk":
        return ("google_ads", "brand - uk")
    return ("unmatched", str(label))


def _state(*, fresh=True):
    if not fresh:
        return {"available": True, "row": {"bootstrap_status": "running",
                                           "last_sync_mode": "bootstrap",
                                           "last_incremental_status": None,
                                           "last_successful_incremental_at": None}}
    return {"available": True, "row": {
        "bootstrap_status": "complete", "last_sync_mode": "incremental",
        "last_incremental_status": "success",
        "last_successful_incremental_at": (NOW - timedelta(hours=3)).isoformat()}}


def _outcomes(monkeypatch, rows=None, *, fresh=True, missing_created_at=0):
    """The REAL cohort build (``build_window_outcomes``) over fake rows, with
    the REAL freshness assessor."""
    from analysis import sql_coverage_freshness as FR
    from db import crm_funnel_repository as repo
    from services import marketing_outcome_cohort_service as M
    monkeypatch.setattr(repo, "fetch_acquisition_cohort_contacts", lambda a, b: {
        "available": True, "rows": list(_rows() if rows is None else rows),
        "missing_created_at": missing_created_at})
    return M.build_window_outcomes(date(2026, 11, 1), date(2026, 11, 19),
                                   resolve_label=_resolver,
                                   freshness=FR.assess(_state(fresh=fresh), now=NOW), now=NOW)


def _contract(outcomes):
    from services.revenue_attribution_service import _cohort_contract_block, _cohort_slots
    campaign_slots, _ = _cohort_slots(outcomes)
    return json.loads(json.dumps(_cohort_contract_block(
        outcomes, campaign_slots, start_date=date(2026, 11, 1), end_date=date(2026, 11, 19)),
        default=str))


# ═════════════════════════════════════════════════════════════════════════════
# 1. one producer, one stage-reach rule
# ═════════════════════════════════════════════════════════════════════════════
def test_01_a_the_cohort_counts_every_stage_cumulatively_on_the_channels_rule(monkeypatch):
    from services import channel_funnel_service as C
    out = _outcomes(monkeypatch)
    alls = out["cohort"]["all_sources"]
    ga = out["cohort"]["google_ads"]
    # c1 c3 c4 c5 c6 c7 reached MQL; c2 reached SQL by its date only (its
    # stage moved back) — cumulative per stage, never "furthest stage".
    assert (alls["contacts_acquired"], alls["reached_mql"], alls["sqls"],
            alls["reached_opportunity"], alls["reached_customer_stage"]) == (8, 6, 5, 4, 2)
    assert (ga["contacts_acquired"], ga["reached_mql"], ga["sqls"],
            ga["reached_opportunity"], ga["reached_customer_stage"]) == (7, 5, 4, 3, 1)
    # The Channels funnel, over the same contacts, counts exactly the same.
    funnel = C.build_funnel(_rows())["totals"]
    assert (funnel["contacts"], funnel["reached_mql"], funnel["reached_sql"],
            funnel["reached_opportunity"], funnel["reached_customer_stage"]) == (8, 6, 5, 4, 2)
    assert out["reconciliation_problems"] == []


def test_01_b_one_rule_channels_reached_is_the_cohort_rule():
    """Channels' `reached` delegates to the cohort's `stage_reached`: there is
    one stage-reach rule, not a second copy that can drift."""
    from services import channel_funnel_service as C
    src = Path(C.__file__).read_text(encoding="utf-8")
    body = src[src.index("def reached("):src.index("def _slot(")]
    assert "cohort_service.stage_reached(row, event)" in body
    assert "lifecycle_stage" not in body.split('"""')[-1]


def test_01_c_every_bucket_identity_holds_for_every_stage(monkeypatch):
    """A leak in one stage's bucket is caught by the cohort's reconciliation
    (and so withholds the verdict)."""
    from services import marketing_outcome_cohort_service as M
    cohort = _outcomes(monkeypatch)["cohort"]
    assert M.reconcile_cohort(cohort) == []
    cohort["by_campaign"]["brand - uk"]["reached_opportunity"] += 1
    problems = M.reconcile_cohort(cohort)
    assert any("reached_opportunity" in p for p in problems), problems
    cohort["by_campaign"]["brand - uk"]["reached_opportunity"] -= 1
    cohort["all_sources"]["reached_customer_stage"] = 99
    assert any("reached_customer_stage" in p for p in M.reconcile_cohort(cohort))


# ═════════════════════════════════════════════════════════════════════════════
# 2. the mart cohort contract
# ═════════════════════════════════════════════════════════════════════════════
def test_02_a_published_the_stages_sit_beside_the_sql_count(monkeypatch):
    c = _contract(_outcomes(monkeypatch, missing_created_at=4))
    assert c["sql_status"] == "published"
    t = c["totals"]
    assert (t["leads"], t["reached_mql"], t["sqls"], t["reached_opportunity"],
            t["reached_customer_stage"]) == (7, 5, 4, 3, 1)
    assert [s["key"] for s in c["funnel_stages"]] == [
        "contacts_acquired", "reached_mql", "reached_sql", "reached_opportunity",
        "reached_customer_stage"]
    assert [s["total"] for s in c["funnel_stages"]] == [
        "leads", "reached_mql", "sqls", "reached_opportunity", "reached_customer_stage"]
    # The Customer column is the lifecycle STAGE, never customers.
    assert c["funnel_stages"][-1]["label"] == "Customer stage"
    assert c["contacts_without_created_at"] == 4
    from scripts import audit_marketing_outcome_cohorts as audit
    assert audit.cohort_stage_problems(c) == []


def test_02_b_withheld_the_stages_are_absent_and_the_audit_names_a_leak(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    c = _contract(_outcomes(monkeypatch, fresh=False, missing_created_at=4))
    assert (c["sql_status"], c["sql_reason"]) == ("withheld", "source_bootstrap_incomplete")
    assert all(c["totals"][f] is None for f in STAGE_FIELDS)
    assert c["breakdown"] is None
    # PR-ADS-180B-F1 (m4): the no-created-date count is a table-wide
    # disclosure, published whatever the verdict (docs/64 §12.3).
    assert c["contacts_without_created_at"] == 4
    payload = {"cohort": c}
    assert audit.mart_withheld_exposures(payload) == []
    for field in STAGE_FIELDS:
        leaked = json.loads(json.dumps(payload))
        leaked["cohort"]["totals"][field] = 0          # a fabricated zero
        assert f"cohort.totals.{field}" in audit.mart_withheld_exposures(leaked)
    # Its copies are held to one value instead of to absence.
    leaked = json.loads(json.dumps(payload))
    leaked["readiness"] = {"missing_contact_created_at_count": 5}
    assert any("readiness" in p for p in audit.created_at_disclosure_problems(leaked))


def test_02_c_the_audit_catches_a_stage_that_breaks_conservation(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    c = _contract(_outcomes(monkeypatch))
    bad = json.loads(json.dumps(c))
    bad["totals"]["reached_mql"] += 1
    assert any("reached_mql" in p for p in audit.cohort_stage_problems(bad))
    bad = json.loads(json.dumps(c))
    bad["totals"]["reached_opportunity"] = None     # a partial funnel beside SQLs
    assert any("not published beside" in p for p in audit.cohort_stage_problems(bad))
    bad = json.loads(json.dumps(c))
    bad["breakdown"]["excluded_non_google"]["reached_customer_stage"] += 1
    assert any("reached_customer_stage" in p for p in audit.cohort_stage_problems(bad))
    # And through the view's conservation the audits run (Campaigns, Countries).
    payload = {"cohort": bad, "rows": [], "summary": {"sqls": c["totals"]["sqls"]}}
    assert any("reached_customer_stage" in p for p in
               audit.mart_conservation_problems(payload, view="country"))


def test_02_d_a_window_with_no_settled_day_withholds_the_stages_too(monkeypatch):
    from services.revenue_decision_mart import _withhold_cohort
    c = _withhold_cohort(_contract(_outcomes(monkeypatch, missing_created_at=2)),
                         "no_settled_day_in_window")
    assert all(c["totals"][f] is None for f in STAGE_FIELDS)
    # PR-ADS-180B-F1 (m4): kept — a table-wide disclosure, not a window figure.
    assert c["contacts_without_created_at"] == 2


# ═════════════════════════════════════════════════════════════════════════════
# 3. Overview: the all-source acquisition cohort
# ═════════════════════════════════════════════════════════════════════════════
def _overview_block(monkeypatch, **kw):
    from services.dashboard_overview_service import _acquisition_funnel_block
    contract = _contract(_outcomes(monkeypatch, **kw))
    return contract, json.loads(json.dumps(_acquisition_funnel_block(contract), default=str))


def test_03_a_overview_republishes_the_cohorts_all_sources_slot_under_its_verdict(monkeypatch):
    contract, f = _overview_block(monkeypatch, missing_created_at=0)
    assert f["publication"] == {"status": "published", "reason": None}
    assert (f["metric_family"], f["window_basis"]) == ("acquisition_cohort_outcomes",
                                                       "contact_created_at")
    assert f["scope"] == "all_source"
    assert f["totals"] == {"contacts": 8, "reached_mql": 6, "reached_sql": 5,
                           "reached_opportunity": 4, "reached_customer_stage": 2}
    assert f["contacts_without_created_at"] == 0
    from scripts import audit_marketing_outcome_cohorts as audit
    assert audit.acquisition_funnel_problems(
        {"cohort": contract, "lifecycle_acquisition_funnel": f}) == []


def test_03_b_overview_withheld_carries_no_count(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    contract, f = _overview_block(monkeypatch, fresh=False, missing_created_at=3)
    assert f["publication"] == {"status": "withheld", "reason": "source_bootstrap_incomplete"}
    # PR-ADS-180B-F1 (m4): the cohort's table-wide disclosure is republished.
    assert f["totals"] is None and f["contacts_without_created_at"] == 3
    payload = {"cohort": contract, "lifecycle_acquisition_funnel": f}
    assert audit.acquisition_funnel_problems(payload) == []
    assert audit.mart_withheld_exposures(payload) == []
    leaked = json.loads(json.dumps(payload))
    leaked["lifecycle_acquisition_funnel"]["totals"] = {"contacts": SENTINEL}
    assert "lifecycle_acquisition_funnel.totals.contacts" in audit.mart_withheld_exposures(leaked)
    assert audit.acquisition_funnel_problems(leaked)


def test_03_c_a_published_verdict_with_a_missing_count_is_unavailable_never_partial(monkeypatch):
    from services.dashboard_overview_service import _acquisition_funnel_block
    contract = _contract(_outcomes(monkeypatch))
    del contract["breakdown"]["all_sources"]["reached_opportunity"]
    f = _acquisition_funnel_block(contract)
    assert f["publication"] == {"status": "unavailable",
                                "reason": "acquisition_funnel_counts_missing"}
    assert f["totals"] is None
    assert _acquisition_funnel_block(None)["publication"]["status"] == "unavailable"


def test_03_d_the_audit_catches_an_overview_funnel_that_disagrees_with_its_cohort(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    contract, f = _overview_block(monkeypatch)
    bad = json.loads(json.dumps(f))
    bad["totals"]["reached_sql"] = 4          # the Google Ads count, not all sources
    assert any("reached_sql" in p for p in audit.acquisition_funnel_problems(
        {"cohort": contract, "lifecycle_acquisition_funnel": bad}))
    assert audit.acquisition_funnel_problems({"cohort": contract}) == [
        "the Overview carries no lifecycle_acquisition_funnel block"]


def test_03_e_the_lead_anchored_cohort_panel_is_gone_and_the_page_draws_the_acquisition_block():
    src = _fn("renderDashboardOverview")
    assert "renderDashAcquisitionFunnel(d)" in src and "renderDashLifecycleActivity(d)" in src
    for gone in ("renderDashLifecycleCohort", "dashCohortFunnelModel", "dashCohortStageBounds",
                 "lead_entry_cohort"):
        assert gone not in JS, gone


def test_03_f_overview_renders_the_published_block_and_the_withheld_word(monkeypatch):
    _c, pub = _overview_block(monkeypatch)
    _c, wh = _overview_block(monkeypatch, fresh=False)
    wh["totals"] = {"contacts": SENTINEL}       # a faulty payload: never read
    out = _run_app("[renderDashAcquisitionFunnel({lifecycle_acquisition_funnel: %s}),"
                   " renderDashAcquisitionFunnel({lifecycle_acquisition_funnel: %s}),"
                   " renderDashAcquisitionFunnel({})]" % (json.dumps(pub), json.dumps(wh)))
    cells = _cells(out[0])
    assert cells["contacts"].startswith("Contacts acquired 8 ")
    # No rate on the Overview: it publishes none and divides nothing.
    assert cells["sql"].startswith("Reached SQL 5 of the contacts acquired, so far")
    assert "%" not in _text(out[0]).split("Contacts acquired", 1)[1].split("Each stage")[0]
    assert cells["customer"].startswith("Reached Customer stage 2 ")
    assert 'data-metric-family="acquisition_cohort_outcomes"' in out[0]
    assert 'data-window-basis="contact_created_at"' in out[0]
    assert str(SENTINEL) not in out[1] and "<strong>Withheld</strong>" in out[1]
    assert all(c.endswith("Withheld") for c in _cells(out[1]).values())
    assert "<strong>Unavailable</strong>" in out[2]
    assert all(c.endswith("Unavailable") for c in _cells(out[2]).values())


# ═════════════════════════════════════════════════════════════════════════════
# 4. lower bounds: contacts with no created date
# ═════════════════════════════════════════════════════════════════════════════
def test_04_a_a_contact_with_no_created_date_bounds_every_column(monkeypatch):
    _c, f = _overview_block(monkeypatch, missing_created_at=3)
    _c, exact = _overview_block(monkeypatch, missing_created_at=0)
    unstated = {**exact, "contacts_without_created_at": None}
    out = _run_app("[%s].map((f) => renderDashAcquisitionFunnel({lifecycle_acquisition_funnel: f}))"
                   % ", ".join(json.dumps(x) for x in (f, exact, unstated)))
    bound, ok, missing = (_cells(h) for h in out)
    for slot, cell in bound.items():
        assert " at least " in f" {cell}", (slot, cell)
        assert "3 contacts have no created date" in cell
    assert "% of contacts" not in bound["sql"]
    assert all("at least" not in c for c in ok.values())
    # Not stated is not zero: the bound stands, with its own reason.
    assert all("at least" in c and "does not say how many" in c for c in missing.values())


def test_04_b_the_channels_strip_reads_contacts_without_created_at():
    """180A's re-audit: app.js never read it. It now bounds the strip.
    PR-ADS-181A: the strip is the "All channels" drawer's block, and the same
    bound reaches the page's KPI cards and table (one rule, the model's)."""
    from tests.test_pr_ads_162i_channels_page_revamp import _payload
    p = {**_payload(), "metric_family": "acquisition_cohort_outcomes",
         "window_basis": "contact_created_at"}
    bounded = {**p, "contacts_without_created_at": 2}
    exactp = {**p, "contacts_without_created_at": 0}
    out = _run_app("[channelDrawerHtml(%s, '__all__'), channelDrawerHtml(%s, '__all__'),"
                   " renderChannelFunnel(%s, {}), renderChannelFunnel(%s, {})]"
                   % (json.dumps(bounded), json.dumps(exactp), json.dumps(bounded),
                      json.dumps(exactp)))
    bound, exact = _cells(out[0]), _cells(out[1])
    assert bound["contacts"].startswith("Contacts acquired at least 2,951 ")
    assert bound["opportunity"].startswith("Reached Opportunity at least 231 share unknown")
    assert exact["opportunity"].startswith("Reached Opportunity 231 7.8% of contacts")
    text = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))  # noqa: E731
    page_bound, page_exact = text(out[2]), text(out[3])
    assert "New contacts at least 2,951" in page_bound
    assert "Reached SQL so far at least 377" in page_bound and "12.8%" not in page_bound
    assert "2 contacts have no created date" in page_bound
    assert "Reached SQL so far 377 12.8% of contacts" in page_exact
    # "Most from" survives a known bound only when the undated contacts could
    # not change the leader (1,147 > 746 + 2); an unstated count names none.
    # PR-ADS-181A-F1 (audit M3): the leader's figure is a bound too, "at least
    # 1,147" — this line pinned it as exact before, which was the defect.
    assert "Most from Paid Social · at least 1,147" in page_bound
    unstated = text(_run_app("renderChannelFunnel(%s, {})"
                             % json.dumps({**p, "contacts_without_created_at": None})))
    assert "Most from" not in unstated.split("Channels ranked")[0]


# ═════════════════════════════════════════════════════════════════════════════
# 5. Campaigns and Countries: one block, on the response's own cohort
# ═════════════════════════════════════════════════════════════════════════════
def _mart_page(contract, view):
    data = {"window": {"key": "current_quarter"}, "cohort": contract,
            "readiness": {"revenue_decision_ready": True, "spend_decision_ready": True},
            "rows": [{"campaign_name": "brand - uk", "campaign_id": "1", "leads": 1, "sqls": 1,
                      "sqls_status": contract["sql_status"]}],
            "campaign_totals": {"metrics": {}}, "country_totals": {"metrics": {}},
            "spend_truth": {"country_spend_status": "verified"}, "summary": {}}
    if view == "campaigns":
        return ('campaignsPageMart = %s; campaignsPageStatus = "ok"; renderCampaignsPage();'
                ' __els["campaigns-page-body"].innerHTML' % json.dumps(data))
    data["rows"] = [{"country": "United Kingdom", "country_code": "GB", "leads": 1, "sqls": 1}]
    return ("roasCountryMart = %s; roasCountriesData = roasCountryMart.rows;"
            ' renderRoasCountriesPage(); __els["roas-countries-table-body"].innerHTML'
            % json.dumps(data))


def _block(html):
    i = html.index('class="dash-panel dash-anim mart-cohort-funnel-panel"')
    return html[i:html.index("</section>", i)]


@pytest.mark.parametrize("view", ["campaigns", "countries"])
def test_05_a_each_page_draws_one_block_from_its_cohort(monkeypatch, view):
    contract = _contract(_outcomes(monkeypatch))
    html = _run_app('_currentUser = {"role": "admin"}; ' + _mart_page(contract, view))
    assert html.count("mart-cohort-funnel-panel") == 1
    block = _block(html)
    assert block.count('class="funnel-block"') == 1
    assert re.findall(r'data-funnel-slot="([a-z]+)"', block) == [
        "contacts", "mql", "sql", "opportunity", "customer"]
    cells = _cells(block)
    assert cells["contacts"].startswith("Leads · contacts acquired 7 ")
    assert cells["mql"].startswith("Reached MQL 5 ")
    assert cells["sql"].startswith("Reached SQL 4 ")
    assert cells["customer"].startswith("Reached Customer stage 1 ")
    assert 'data-metric-family="acquisition_cohort_outcomes"' in block
    assert "through 19 Nov 2026" in _text(block)


@pytest.mark.parametrize("view", ["campaigns", "countries"])
def test_05_b_withheld_each_page_shows_the_word_and_no_figure(monkeypatch, view):
    contract = _contract(_outcomes(monkeypatch, fresh=False))
    contract["totals"] = {**contract["totals"], "reached_mql": SENTINEL, "leads": SENTINEL}
    html = _run_app('_currentUser = {"role": "admin"}; ' + _mart_page(contract, view))
    block = _block(html)
    assert str(SENTINEL) not in block
    assert "<strong>Withheld</strong>" in block and "backfill is still running" in block
    assert all(c.endswith("Withheld") for c in _cells(block).values())


def test_05_c_the_gate_is_the_argument_not_page_state(monkeypatch):
    """`martCohortFunnelData(cohort)` gates on the cohort it is given: a
    withheld page global never publishes a drawer's — or another page's —
    numbers, and a published one never unlocks a withheld argument."""
    pub = _contract(_outcomes(monkeypatch))
    wh = _contract(_outcomes(monkeypatch, fresh=False))
    out = _run_app("campaignsPageMart = {cohort: %s}; roasCountryMart = {cohort: %s};"
                   " [martCohortFunnelHtml(%s, {}), martCohortFunnelHtml(%s, {})]"
                   % (json.dumps(wh), json.dumps(wh), json.dumps(pub), json.dumps(wh)))
    assert _cells(out[0])["sql"].startswith("Reached SQL 4 ")
    assert "Withheld" in _cells(out[1])["sql"]
    body = _fn("martCohortFunnelData")
    assert "campaignSqlPublication(cohort)" in body
    for global_ in ("campaignsPageMart", "roasCountryMart", "dashOverview"):
        assert global_ not in body


def test_05_d_the_customer_column_is_the_lifecycle_stage_never_companies(monkeypatch):
    for name in ("acquisitionFunnelModel", "martCohortFunnelData", "renderDashAcquisitionFunnel"):
        body = re.sub(r"//.*", "", _fn(name))
        for figure in (".customers", "customers_won", "closed_won", "company"):
            assert figure not in body, (name, figure)


def test_05_e_the_campaigns_audit_gates_the_block_on_its_cohort():
    from scripts import audit_campaigns_page_certification as A
    f = A.Findings()
    A.check_frontend_gates(f)
    assert f.violations == [] and f.unavailable == []
    mutated = JS.replace("const gate = campaignSqlPublication(cohort);",
                         "const gate = { publish: true };", 1)
    assert mutated != JS
    f = A.Findings()
    A.check_frontend_gates(f, js=mutated)
    assert any("martCohortFunnelData" in v for v in f.violations)


# ═════════════════════════════════════════════════════════════════════════════
# 6. finding (a): a lower-bound baseline is no comparison
# ═════════════════════════════════════════════════════════════════════════════
def _f_row(i, stage, created="2026-01-01", **d):
    r = {"contact_id": f"c{i}", "lifecycle_stage": stage, "created_at": created,
         "hs_analytics_source": "ORGANIC_SEARCH"}
    for k, v in d.items():
        r[f"date_entered_{k}"] = v
    return r


_EVENTS = ("lead", "mql", "sql", "opportunity", "customer")
_RANK = {"lead": 1, "marketingqualifiedlead": 2, "salesqualifiedlead": 3,
         "opportunity": 4, "customer": 5, "evangelist": 6}
_EVENT_RANK = {"lead": 1, "mql": 2, "sql": 3, "opportunity": 4, "customer": 5}


def _window_fetch(rows):
    """`fetch_funnel_contacts`, honouring [start, end] like the real read."""
    def fetch(start, end):
        out = []
        for r in rows:
            for k, v in r.items():
                if k.startswith("date_entered_") and v:
                    d = date.fromisoformat(v)
                    if (start is None or d >= start) and (end is None or d <= end):
                        out.append(r)
                        break
        return {"available": True, "rows": out}
    return fetch


def _undated_reach(rows):
    """`fetch_undated_stage_reach` over the SAME whole table, by its documented
    rule (the SQL itself is proven on PostgreSQL in the PG module): a contact
    whose stage implies the event and that has no date for it, ruled out only
    when created after the window AND no date of it falls in the window."""
    def read(start, end):
        counts = dict.fromkeys(_EVENTS, 0)
        for r in rows:
            dates = [date.fromisoformat(v) for k, v in r.items()
                     if k.startswith("date_entered_") and v]
            in_window = any((start is None or d >= start) and (end is None or d <= end)
                            for d in dates)
            created = date.fromisoformat(r["created_at"]) if r.get("created_at") else None
            if created and end and created > end and not in_window:
                continue
            for e in _EVENTS:
                if _RANK.get(r["lifecycle_stage"], 0) >= _EVENT_RANK[e] \
                        and not r.get(f"date_entered_{e}"):
                    counts[e] += 1
        return {"available": True, "counts": counts}
    return read


def _activity(monkeypatch, rows, *, undated=None, window="last_quarter"):
    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as F
    from services import dashboard_overview_service as O
    monkeypatch.setattr(repo, "fetch_funnel_contacts", _window_fetch(rows))
    # raising=False: the read is new in 180B, so on bf9deed this case still
    # runs and fails on the figure it prints, not on a missing name.
    monkeypatch.setattr(repo, "fetch_undated_stage_reach",
                        undated if undated is not None else _undated_reach(rows),
                        raising=False)
    monkeypatch.setattr(F, "_build_campaign_resolver",
                        lambda s, e: (F.default_campaign_resolver, True))
    monkeypatch.setattr(F, "_sync_state_block", lambda: {"available": True})
    funnel = O._lifecycle_funnel_block(window, now=NOW)
    act = json.loads(json.dumps(O._lifecycle_activity_block(funnel, window, NOW), default=str))
    html = _run_app("renderDashLifecycleActivity(%s)" % json.dumps({"lifecycle_activity": act}))
    return act, _cells(html)


def _prevbound_rows():
    """The auditor's probe (audit180a/probe_prevbound.py): Q3 fully dated (12
    Leads → 12 MQLs); Q2 has 10 dated Lead→MQL contacts and 6 at SQL whose
    Lead and SQL are dated in Q2 but with NO MQL date — so Q2's MQL count
    (10) is a lower bound."""
    rows = [_f_row(i, "marketingqualifiedlead", lead="2026-07-05", mql="2026-07-10")
            for i in range(12)]
    rows += [_f_row(i, "marketingqualifiedlead", lead="2026-04-05", mql="2026-04-10")
             for i in range(100, 110)]
    rows += [_f_row(i, "salesqualifiedlead", lead="2026-04-06", sql="2026-05-02")
             for i in range(200, 206)]
    return rows


def _none_outside(_start, _end):
    """No undated contact outside the window's rows: isolates finding (a)."""
    return {"available": True, "counts": dict.fromkeys(_EVENTS, 0)}


def test_06_a_a_lower_bound_baseline_prints_no_comparison(monkeypatch):
    """Finding (a), the auditor's probe through the REAL
    `_lifecycle_activity_block`. On bf9deed MQL read "+20.0% vs Prior Quarter"
    — 12 against a baseline of "10" that was at least 10."""
    act, cells = _activity(monkeypatch, _prevbound_rows(), undated=_none_outside)
    by = {s["event"]: s for s in act["stages"]}
    assert by["mql"]["previous_period"]["status"] == "no_comparison"
    assert by["mql"]["previous_period"]["reason"] == "previous count is a lower bound"
    assert by["mql"]["previous_period"]["delta_pct"] is None
    assert "No comparison" in cells["mql"] and "%" not in cells["mql"].split("→")[0]
    assert "+20.0%" not in cells["mql"]
    # Lead's baseline is complete: its change is still measured.
    assert by["lead"]["previous_period"]["status"] == "ok"
    assert "-25.0% vs Prior Quarter" in cells["contacts"]


def test_06_b_a_bound_current_count_is_no_comparison_either(monkeypatch):
    """The current side, in the backend too (180A-F1 closed it in the page):
    the same probe with its real undated reach — the six Q2 contacts with no
    MQL date could have entered MQL in Q3 — bounds Q3's MQL count."""
    act, cells = _activity(monkeypatch, _prevbound_rows())
    by = {s["event"]: s for s in act["stages"]}
    assert by["mql"]["count_is_bound"] is True
    assert by["mql"]["previous_period"]["reason"] == "current count is a lower bound"
    assert cells["mql"].startswith("MQLs at least 12 ")


# ═════════════════════════════════════════════════════════════════════════════
# 7. finding (b): undated contacts outside the window's rows
# ═════════════════════════════════════════════════════════════════════════════
def _outside_rows():
    """The auditor's probe (audit180a/probe_outside.py), window Q3: ten dated
    Leads; four reached Lead undated and entered MQL in OCTOBER; three sit at
    stage lead with no date at all. None of the seven is in Q3's rows."""
    rows = [_f_row(i, "marketingqualifiedlead", created="2026-07-01",
                   lead="2026-07-05", mql="2026-07-10") for i in range(10)]
    rows += [_f_row(50 + i, "marketingqualifiedlead", created="2026-07-01", mql="2026-10-15")
             for i in range(4)]
    rows += [_f_row(80 + i, "lead", created="2026-07-01") for i in range(3)]
    return rows


def test_07_a_undated_contacts_outside_the_windows_rows_bound_the_lead_count(monkeypatch):
    """On bf9deed Lead read "10" — exact — because the window's rows held no
    undated contact. Seven contacts that could have entered Lead in Q3 are
    outside them."""
    act, cells = _activity(monkeypatch, _outside_rows(), undated=None)
    assert cells["contacts"].startswith("Leads at least 10 "), cells["contacts"]
    by = {s["event"]: s for s in act["stages"]}
    assert by["lead"]["missing_entry_date_contacts"] == 0     # the old, narrow count
    assert by["lead"]["undated_contacts_could_belong"] == 7
    assert by["lead"]["count_is_bound"] is True
    assert "7 contacts reached this stage with no entry date recorded in HubSpot and " \
           "could belong to this window" in cells["contacts"]
    # MQL: the four October MQLs are dated, so nobody undated reached MQL.
    assert by["mql"]["undated_contacts_could_belong"] == 0
    assert "at least" not in cells["mql"]


def test_07_b_a_contact_created_after_the_window_cannot_bound_it(monkeypatch):
    """Positive control for the one narrowing: created after the window ended
    and with no date in it, the contact cannot have entered a stage there."""
    rows = [_f_row(i, "marketingqualifiedlead", created="2026-07-01",
                   lead="2026-07-05", mql="2026-07-10") for i in range(10)]
    rows += [_f_row(90, "lead", created="2026-10-20")]          # created in Q4
    act, cells = _activity(monkeypatch, rows)
    assert cells["contacts"].startswith("Leads 10 ")
    assert {s["event"]: s for s in act["stages"]}["lead"]["undated_contacts_could_belong"] == 0


def test_07_c_an_unreadable_reach_is_a_bound_never_exact(monkeypatch):
    act, cells = _activity(monkeypatch, _outside_rows(),
                           undated=lambda s, e: {"available": False, "counts": None})
    assert all(s["count_is_bound"] is True for s in act["stages"])
    assert all(s["undated_contacts_could_belong"] is None for s in act["stages"])
    assert all("could not all be counted" in c for c in cells.values())
    assert all("No comparison" in c for c in cells.values())
