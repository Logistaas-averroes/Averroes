"""PR-ADS-180B-F1 — the truth audit's minors on PR-ADS-180B (docs/64 §12).

What is guarded here (the PostgreSQL half — m2, the undated-reach edges — is
case 05 of ``test_pr_ads_180b_acquisition_cohort_funnel_pg_integration``):

* **m1.** The two "missing means a bound" branches of the Activity trend had
  no case: a build whose coverage does not report a stage, and a previous
  period that carries no ``count_is_bound`` for a stage. Both are a bound —
  "at least N" / "No comparison" — never an exact count or a "+X%".
  Mutations G (``own is not None and own > 0``) and H (``is True``) survive
  without these cases.
* **m4.** ``contacts_without_created_at`` is a table-wide data-quality
  disclosure, not a cohort figure: published whatever the verdict, one value
  in every copy (cohort block, readiness, Overview block, coverage note).
* **m5.** The acquisition legend (and Channels' stage note) states the
  stage-reach rule as it is: entered the stage OR currently at it or later,
  so the columns are not nested subsets.
* **m6.** Countries: the funnel's Reached SQL column covers the residual;
  a note says how many, from published inputs only, under the cohort gate.

Every producer runs for real; the repository is replaced at its fetch
boundary only. Freshness goes through the real
``analysis.sql_coverage_freshness.assess``.
"""
from __future__ import annotations

import json
import re

from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
from tests.test_pr_ads_180b_acquisition_cohort_funnel import (
    JS, NOW, SENTINEL, STAGE_FIELDS, _block, _cells, _contract, _f_row, _none_outside,
    _outcomes, _text, _window_fetch,
)

_EVENTS = ("lead", "mql", "sql", "opportunity", "customer")


# ═════════════════════════════════════════════════════════════════════════════
# m1. missing coverage / missing count_is_bound is a bound
# ═════════════════════════════════════════════════════════════════════════════
def _dated_rows():
    """Q3 (the window, last quarter at NOW): 12 contacts, Lead and MQL dated.
    Q2 (the previous period): 10, Lead and MQL dated. No undated contact
    anywhere — every count is exact, and Lead / MQL read +20%."""
    rows = [_f_row(i, "marketingqualifiedlead", lead="2026-07-05", mql="2026-07-10")
            for i in range(12)]
    rows += [_f_row(i, "marketingqualifiedlead", lead="2026-04-05", mql="2026-04-10")
             for i in range(100, 110)]
    return rows


def _funnel(monkeypatch, rows, window="last_quarter"):
    """The REAL `_lifecycle_funnel_block` over the rows (fetch boundary only),
    with no undated contact outside the window's rows."""
    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as F
    from services import dashboard_overview_service as O
    monkeypatch.setattr(repo, "fetch_funnel_contacts", _window_fetch(rows))
    monkeypatch.setattr(repo, "fetch_undated_stage_reach", _none_outside)
    monkeypatch.setattr(F, "_build_campaign_resolver",
                        lambda s, e: (F.default_campaign_resolver, True))
    monkeypatch.setattr(F, "_sync_state_block", lambda: {"available": True})
    return O._lifecycle_funnel_block(window, now=NOW)


def _activity_of(funnel, window="last_quarter"):
    from services import dashboard_overview_service as O
    act = json.loads(json.dumps(O._lifecycle_activity_block(funnel, window, NOW), default=str))
    html = _run_app("renderDashLifecycleActivity(%s)" % json.dumps({"lifecycle_activity": act}))
    return {s["event"]: s for s in act["stages"]}, _cells(html)


def test_01_a_coverage_that_does_not_report_a_stage_makes_it_a_bound(monkeypatch):
    """`_stage_count_is_bound`: a build whose coverage carries no entry for the
    stage has not shown the stage complete. Mutation G (`own is not None and
    own > 0`) reads the missing entry as 0 and prints MQL exact."""
    funnel = _funnel(monkeypatch, _dated_rows())
    by, _ = _activity_of(funnel)
    # Positive control: every stage reported, nothing undated — exact.
    assert by["mql"]["count_is_bound"] is False
    assert by["mql"]["previous_period"]["status"] == "ok"
    # The same build, its coverage silent on MQL.
    del funnel["coverage"]["stage_reached_without_entry_date"]["mql"]
    by, cells = _activity_of(funnel)
    assert by["mql"]["entered"] == 12
    assert by["mql"]["count_is_bound"] is True
    assert by["mql"]["previous_period"]["status"] == "no_comparison"
    assert by["mql"]["previous_period"]["reason"] == "current count is a lower bound"
    assert cells["mql"].startswith("MQLs at least 12 "), cells["mql"]
    assert "+20.0%" not in cells["mql"]
    # Lead's entry is still reported, so Lead is still exact and measured.
    assert by["lead"]["count_is_bound"] is False
    assert by["lead"]["previous_period"]["status"] == "ok"


def test_01_b_no_coverage_at_all_bounds_every_stage():
    """The helper itself: no coverage, or coverage that is not a mapping, is
    a bound for every stage, even with a readable undated reach of 0."""
    from services.dashboard_overview_service import _stage_count_is_bound
    zero = {"available": True, "counts": dict.fromkeys(_EVENTS, 0)}
    for coverage in (None, {}, {"stage_reached_without_entry_date": None},
                     {"stage_reached_without_entry_date": ["lead"]},
                     {"stage_reached_without_entry_date": {"lead": 0}}):
        assert _stage_count_is_bound("mql", coverage, zero) is True, coverage
    assert _stage_count_is_bound(
        "mql", {"stage_reached_without_entry_date": {"mql": 0}}, zero) is False


def test_01_c_a_previous_period_without_count_is_bound_is_no_comparison(monkeypatch):
    """`prev_bounds.get(event) is not False`: a previous period that does not
    say a stage's count is complete is no baseline. Mutation H (`is True`)
    reads the missing key as "complete" and prints "+20.0% vs Prior Quarter"."""
    from services import dashboard_overview_service as O
    funnel = _funnel(monkeypatch, _dated_rows())
    real = O._lifecycle_previous_period
    by, cells = _activity_of(funnel)
    # Positive control: the real previous period states every stage complete.
    assert by["mql"]["previous_period"]["status"] == "ok"
    assert "+20.0% vs Prior Quarter" in cells["mql"]

    def without_bounds(window, now):
        prev = real(window, now)
        assert prev["available"] is True and prev["count_is_bound"]["mql"] is False
        prev.pop("count_is_bound")
        return prev

    monkeypatch.setattr(O, "_lifecycle_previous_period", without_bounds)
    by, cells = _activity_of(funnel)
    for event in ("lead", "mql"):
        trend = by[event]["previous_period"]
        assert (trend["status"], trend["reason"], trend["delta_pct"]) == (
            "no_comparison", "previous count is a lower bound", None), (event, trend)
        # The current counts are exact: only the baseline is in question.
        assert by[event]["count_is_bound"] is False
    assert "No comparison" in cells["mql"] and "+20.0%" not in cells["mql"]

    def one_stage_silent(window, now):
        prev = real(window, now)
        del prev["count_is_bound"]["mql"]
        return prev

    monkeypatch.setattr(O, "_lifecycle_previous_period", one_stage_silent)
    by, _ = _activity_of(funnel)
    assert by["mql"]["previous_period"]["status"] == "no_comparison"
    assert by["lead"]["previous_period"]["status"] == "ok"


# ═════════════════════════════════════════════════════════════════════════════
# m4. the no-created-date count is a table-wide disclosure, one value everywhere
# ═════════════════════════════════════════════════════════════════════════════
def test_04_a_withheld_the_cohort_still_discloses_the_no_created_date_count(monkeypatch):
    """Withheld, every cohort count is absent — but the table-wide count of
    contacts with no created date is published, as readiness and the coverage
    note already publish it. On 46f5396 the cohort block nulled it while the
    other two copies stated it."""
    from scripts import audit_marketing_outcome_cohorts as audit
    c = _contract(_outcomes(monkeypatch, fresh=False, missing_created_at=4))
    assert (c["sql_status"], c["sql_reason"]) == ("withheld", "source_bootstrap_incomplete")
    assert all(c["totals"][f] is None for f in STAGE_FIELDS + ("leads", "sqls"))
    assert c["breakdown"] is None
    assert c["contacts_without_created_at"] == 4
    assert any(n.startswith("4 canonical contact(s) have no created date")
               for n in c["metadata"]["coverage_notes"])
    payload = {"cohort": c, "readiness": {"missing_contact_created_at_count": 4}}
    assert audit.mart_withheld_exposures(payload) == []
    assert audit.created_at_disclosure_problems(payload) == []
    # Nothing on the page reads it as a cohort number: withheld, the block
    # draws the word, no figure and no "no created date" bound.
    c["contacts_without_created_at"] = SENTINEL
    out = _run_app("martCohortFunnelHtml(%s, {})" % json.dumps(c))
    assert str(SENTINEL) not in out and "no created date" not in out
    assert all(cell.endswith("Withheld") for cell in _cells(out).values())


def test_04_b_the_overview_block_republishes_it_under_any_verdict(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    from services.dashboard_overview_service import _acquisition_funnel_block
    for fresh in (False, True):
        c = _contract(_outcomes(monkeypatch, fresh=fresh, missing_created_at=3))
        f = json.loads(json.dumps(_acquisition_funnel_block(c), default=str))
        assert f["contacts_without_created_at"] == 3, fresh
        payload = {"cohort": c, "lifecycle_acquisition_funnel": f}
        assert audit.acquisition_funnel_problems(payload) == []
        assert audit.mart_withheld_exposures(payload) == []
        if not fresh:
            assert f["totals"] is None
            html = _run_app("renderDashAcquisitionFunnel(%s)" % json.dumps(
                {"lifecycle_acquisition_funnel": {**f, "contacts_without_created_at": SENTINEL}}))
            assert str(SENTINEL) not in html and "no created date" not in html


def test_04_c_a_window_with_no_settled_day_keeps_the_disclosure(monkeypatch):
    from services.revenue_decision_mart import _withhold_cohort
    c = _withhold_cohort(_contract(_outcomes(monkeypatch, missing_created_at=2)),
                         "no_settled_day_in_window")
    assert all(c["totals"][f] is None for f in STAGE_FIELDS)
    assert c["contacts_without_created_at"] == 2


def test_04_d_the_audit_holds_every_copy_to_one_value(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    from services.dashboard_overview_service import _acquisition_funnel_block
    c = _contract(_outcomes(monkeypatch, missing_created_at=4))
    f = json.loads(json.dumps(_acquisition_funnel_block(c), default=str))
    good = {"cohort": c, "readiness": {"missing_contact_created_at_count": 4},
            "lifecycle_acquisition_funnel": f}
    assert audit.created_at_disclosure_problems(good) == []

    def problems(mutate):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        return audit.created_at_disclosure_problems(bad)

    assert any("readiness" in p for p in problems(
        lambda p: p["readiness"].update(missing_contact_created_at_count=5)))
    assert any("readiness" in p for p in problems(
        lambda p: p["readiness"].update(missing_contact_created_at_count=0)))
    assert any("lifecycle_acquisition_funnel" in p for p in problems(
        lambda p: p["lifecycle_acquisition_funnel"].update(contacts_without_created_at=None)))
    assert any("coverage_notes" in p for p in problems(
        lambda p: p["cohort"].update(contacts_without_created_at=0)))
    assert any("coverage_notes" in p for p in problems(
        lambda p: p["cohort"]["metadata"].update(coverage_notes=[
            "7 canonical contact(s) have no created date and belong to no "
            "acquisition window, All Time included. No other date is substituted."])))
    # Unread is None in every copy, and no note states a number.
    c0 = _contract(_outcomes(monkeypatch, missing_created_at=None))
    assert c0["contacts_without_created_at"] is None
    assert audit.created_at_disclosure_problems(
        {"cohort": c0, "readiness": {"missing_contact_created_at_count": None}}) == []
    assert audit.created_at_disclosure_problems(
        {"cohort": c0, "readiness": {"missing_contact_created_at_count": 0}})
    # The audit runs it on every mart / Dashboard payload of a window.
    a = audit.Audit()
    bad = json.loads(json.dumps(good))
    bad["readiness"]["missing_contact_created_at_count"] = 9
    audit.audit_mart_window(a, window="current_quarter", payloads={"mart/campaign": bad})
    assert any("created_at_disclosure" in v for v in a.violations), a.violations


def test_04_e_readiness_never_reads_an_unread_count_as_zero():
    """`_readiness_block` defaulted a missing count to 0; unknown is not zero."""
    from services.revenue_decision_mart import _readiness_block
    spend = {"campaign_spend_status": "verified", "fx_status": "verified",
             "country_spend_status": "verified"}
    assert _readiness_block({"source_health": {}}, spend)[
        "missing_contact_created_at_count"] is None
    assert _readiness_block({"source_health": {"missing_contact_created_at_count": 6}},
                            spend)["missing_contact_created_at_count"] == 6


# ═════════════════════════════════════════════════════════════════════════════
# m5. the legend states the stage-reach rule as it is
# ═════════════════════════════════════════════════════════════════════════════
def test_05_a_the_acquisition_legend_says_entered_or_later_and_not_nested(monkeypatch):
    c = _contract(_outcomes(monkeypatch))
    out = _run_app("[martCohortFunnelHtml(%s, {}), renderDashAcquisitionFunnel({})]"
                   % json.dumps(c))
    for html in out:
        legend = _text(re.search(r'<p class="lifecycle-legend">(.*?)</p>', html, re.S).group(1))
        assert "records it entering that stage, or when its current lifecycle stage " \
               "is that stage or a later one" in legend, legend
        assert "not nested subsets" in legend
        assert "a later column can exceed an earlier one" in legend
    from services import channel_funnel_service as C
    assert "not nested subsets" in C.STAGE_NOTE
    assert "a later column can exceed an earlier one" in C.STAGE_NOTE


def test_05_b_the_rule_the_legend_describes_is_the_one_that_counts(monkeypatch):
    """The legend describes the rule; it does not change it. The fixture's c2
    (stage moved back to lead, SQL dated) counts at SQL and not at MQL — the
    reason the columns are not nested — and the SQL count is unchanged."""
    from services import marketing_outcome_cohort_service as M
    from tests.test_pr_ads_180b_acquisition_cohort_funnel import _row
    c2 = _row(2, "lead", sql="2026-11-12")
    assert M.stage_reached(c2, "sql") is True and M.stage_reached(c2, "mql") is False
    alls = _outcomes(monkeypatch)["cohort"]["all_sources"]
    assert (alls["reached_mql"], alls["sqls"]) == (6, 5)


# ═════════════════════════════════════════════════════════════════════════════
# m6. Countries: the residual inside the funnel's Reached SQL
# ═════════════════════════════════════════════════════════════════════════════
def _countries(contract, rows):
    data = {"window": {"key": "current_quarter"}, "cohort": contract,
            "readiness": {"revenue_decision_ready": True, "spend_decision_ready": True},
            "rows": rows, "country_totals": {"metrics": {}},
            "spend_truth": {"country_spend_status": "verified"}, "summary": {}}
    return _block(_run_app(
        '_currentUser = {"role": "admin"}; roasCountryMart = %s;'
        ' roasCountriesData = roasCountryMart.rows; renderRoasCountriesPage();'
        ' __els["roas-countries-table-body"].innerHTML' % json.dumps(data)))


_UK = {"country": "United Kingdom", "country_code": "GB", "leads": 5, "sqls": 3,
       "sqls_status": "published"}


def _residual(**kw):
    return {"country": "Unknown / Unattributed", "country_key": "unknown", "is_residual": True,
            "leads": 2, "sqls": 1, "sqls_status": "published", **kw}


def test_06_a_the_funnel_sql_column_names_the_residual_it_includes(monkeypatch):
    """Cohort SQLs 4 = UK 3 (the SQLs card) + residual 1. The column says so;
    on 46f5396 only the panel subtitle hinted at it."""
    c = _contract(_outcomes(monkeypatch))
    assert c["totals"]["sqls"] == 4
    cells = _cells(_countries(c, [_UK, _residual()]))
    assert cells["sql"].startswith("Reached SQL 4 ")
    assert "Includes 1 in markets not listed or with no country (the residual row)" \
        in cells["sql"], cells["sql"]
    assert "SQLs card above counts the listed markets only" in cells["sql"]
    # Only the SQL column carries it.
    assert all("residual" not in cells[k] for k in ("contacts", "mql", "opportunity",
                                                    "customer"))


def test_06_b_an_unpublished_residual_is_said_in_words_never_subtracted(monkeypatch):
    c = _contract(_outcomes(monkeypatch))
    withheld = _residual(sqls=None, sqls_status="unavailable",
                         sqls_reason="country_geo_not_ready")
    cell = _cells(_countries(c, [_UK, withheld]))["sql"]
    assert "their number is not published in this response" in cell, cell
    # 4 - 3 = 1 is never computed in the browser.
    assert "Includes 1" not in cell
    cell = _cells(_countries(c, [_UK]))["sql"]
    assert "carries no residual row" in cell and "is not stated" in cell
    assert "Includes 0" not in cell


def test_06_c_the_note_is_gated_by_the_cohort_like_the_funnel(monkeypatch):
    c = _contract(_outcomes(monkeypatch, fresh=False))
    block = _countries(c, [_UK, _residual(sqls=SENTINEL)])
    assert str(SENTINEL) not in block and "residual row" not in block
    assert _cells(block)["sql"].endswith("Withheld")
    body = re.search(r"\nfunction countryFunnelResidualNote\(.*?\n}\n", JS, re.S).group(0)
    assert "campaignSqlPublication(cohort)" in body
    for global_ in ("roasCountryMart", "roasCountriesData", "campaignsPageMart"):
        assert global_ not in body
    # The residual is subtracted from nothing: the note reads the row's own count.
    assert not re.search(r"totals\.sqls\s*-|-\s*\w*\.sqls", body)
