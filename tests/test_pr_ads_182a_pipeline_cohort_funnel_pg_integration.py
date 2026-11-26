"""PR-ADS-182A — the Pipeline funnel is the acquisition cohort, on
PostgreSQL, through the real repository reads and the real builders
(``docs/67``).

The fixture is PR-ADS-180B's (PR-ADS-177A's spend, geo, a placed deal, the
canonical contact funnel and its sync state, plus c-mql and an optional
contact with no created date). Every figure is read by the production SQL
and built by the production builders — ``build_pipeline`` (its mart deal
view's cohort), ``build_dashboard_overview``, ``build_dashboard_deals`` — and
checked against the cohort audit's OWN SQL (``independent_counts``) and the
real cross-page parity audit. The UI cases render the real payloads with the
real ``static/app.js`` in ``node``.

On ``6de7818`` the Pipeline funnel is the stage-entry funnel: no ``totals``,
no cohort family, no parity identity with the Overview.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest  # noqa: F401  (the live_db fixture skips without PostgreSQL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, live_db,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import WINDOW  # noqa: E402
from tests.test_pr_ads_180b_acquisition_cohort_funnel_pg_integration import (  # noqa: E402
    _cells, _json, _render, _setup,
)

COLUMNS = ("contacts", "reached_mql", "reached_sql", "reached_opportunity",
           "reached_customer_stage")


def _pipeline(**kw):
    from services.pipeline_service import build_pipeline
    return _json(build_pipeline(WINDOW, **kw))


def _overview():
    from services.dashboard_overview_service import build_dashboard_overview
    return _json(build_dashboard_overview(window=WINDOW))


def _panel(payload):
    html = _render("pipelineData = %s; pipelineStatus = 'ok'; renderPipelinePage();"
                   ' __els["pipeline-summary"].innerHTML' % json.dumps(payload))
    return html[html.index('id="pipeline-funnel"'):]


def _t(block):
    return tuple(block["totals"][c] for c in COLUMNS)


# ── 1. one cohort, one block, on Pipeline and Overview ──────────────────────
def test_01_pipeline_and_overview_publish_one_cohort_certified_by_the_audits_own_sql(
        live_db, monkeypatch):
    """All sources: the 177A Google Ads cohort (6) plus c-org → 7 contacts;
    MQL 6, SQL 5, Opportunity 3, Customer stage 2 — each stage cumulative."""
    from scripts import audit_marketing_outcome_cohorts as audit
    from services import cross_page_parity_service as parity
    _setup(live_db, monkeypatch)
    p, o = _pipeline(), _overview()
    f, ov = p["funnel"], o["lifecycle_acquisition_funnel"]
    assert f["publication"] == ov["publication"] == {"status": "published", "reason": None}
    assert (f["metric_family"], f["window_basis"]) == ("acquisition_cohort_outcomes",
                                                       "contact_created_at")
    assert _t(f) == _t(ov) == (7, 6, 5, 3, 2)
    # The cohort audit's own SQL over the same days says the same.
    payloads = audit._build_mart_payloads(WINDOW, None)
    independent = audit.independent_counts(*audit._settled_instants(
        payloads["mart/campaign_settled"]))
    assert {k: f["totals"][k] for k in ("reached_mql", "reached_opportunity",
                                        "reached_customer_stage")} == {
        k: independent[k] for k in ("reached_mql", "reached_opportunity",
                                    "reached_customer_stage")}
    a = audit.Audit()
    audit.audit_mart_window(a, window=WINDOW, payloads=payloads)
    assert a.violations == [], a.violations
    held = {c["check"] for c in a.checks if c["ok"]}
    assert f"[business:{WINDOW}] pipeline acquisition_funnel" in held
    assert f"[business:{WINDOW}] dashboard/deals acquisition_funnel" in held
    # The real cross-page parity audit: Pipeline == Overview, column for column.
    report = parity.audit_window(WINDOW)
    mine = {m["metric"]: m for m in report["metrics"]
            if m["metric"].startswith("all_source_acquisition_cohort_")}
    assert sorted(mine) == sorted(f"all_source_acquisition_cohort_{c}" for c in COLUMNS)
    for key, m in mine.items():
        assert m["status"] == "identical", (key, m["status"], m.get("readings"))
    assert not [v for v in report["violations"]
                if str(v.get("metric", "")).startswith("all_source_acquisition_cohort_")]
    # The page prints them, exact (every contact has a created date).
    cells = _cells(_panel(p))
    assert cells["contacts"].startswith("Contacts acquired 7 ")
    assert cells["sql"].startswith("Reached SQL 5 ")
    assert "at least" not in " ".join(cells.values())


# ── 2. the lead filters on the real table ───────────────────────────────────
def test_02_the_lead_filters_read_the_cohorts_source_group_part(live_db, monkeypatch):
    """Google Ads (Paid Search: the six), Organic (c-org), every other group
    0 — counted by the cohort's own partition; Scope Google Ads-source is the
    Google Ads group; Keyword-attributable is not on the cohort basis."""
    from scripts import audit_marketing_outcome_cohorts as audit
    from services import canonical_crm_funnel_service as crm_funnel
    _setup(live_db, monkeypatch)
    expected = {"google_ads": (6, 5, 4, 2, 1), "organic": (1, 1, 1, 1, 1),
                "other_paid": (0, 0, 0, 0, 0), "offline": (0, 0, 0, 0, 0),
                "unclassified": (0, 0, 0, 0, 0)}
    assert set(expected) == set(crm_funnel.ACQUISITION_GROUPS)
    whole = _pipeline()
    for group, want in expected.items():
        p = _pipeline(acquisition_group=group)
        assert _t(p["funnel"]) == want, group
        assert audit.acquisition_funnel_problems(p, "funnel") == [], group
        # A lead filter never touches the SQL card or a deal figure.
        assert p["kpis"] == whole["kpis"], group
    assert _t(_pipeline(scope="google_ads_source")["funnel"]) == expected["google_ads"]
    kw = _pipeline(scope="keyword_attributable")
    assert kw["funnel"]["publication"] == {"status": "unavailable",
                                           "reason": "lead_scope_not_on_cohort_basis"}
    assert kw["funnel"]["totals"] is None
    panel = _panel(kw)
    assert "is not split by campaign or keyword attribution" in re.sub(r"\s+", " ", panel)
    for slot, cell in _cells(panel).items():
        assert not re.search(r"\d", cell), (slot, cell)
    assert _cells(_panel(_pipeline(acquisition_group="organic")))["contacts"].startswith(
        "Contacts acquired 1 created in this window · Organic")


# ── 3. withheld: no count anywhere, on Pipeline and Dashboard → Deals ───────
def test_03_an_incomplete_contact_sync_leaves_no_funnel_count_in_payload_or_page(
        live_db, monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    from services.dashboard_deals_service import build_dashboard_deals
    _setup(live_db, monkeypatch, fresh=False)
    for p in (_pipeline(), _pipeline(acquisition_group="organic"),
              _json(build_dashboard_deals(window=WINDOW))):
        f = p["funnel"]
        assert f["publication"] == {"status": "withheld",
                                    "reason": "source_bootstrap_incomplete"}
        assert f["totals"] is None
        assert audit.mart_withheld_exposures(p) == [], audit.mart_withheld_exposures(p)
        assert audit.acquisition_funnel_problems(p, "funnel") == []
        assert p["metric_truth"]["lifecycle_acquisition_funnel"]["truth_status"] == "not_ready"
    panel = _panel(_pipeline())
    assert "Withheld" in panel
    for slot, cell in _cells(panel).items():
        assert not re.search(r"\d", cell), (slot, cell)


# ── 4. a contact with no created date: every column "at least N" ────────────
def test_04_a_contact_with_no_created_date_bounds_pipeline_as_it_bounds_overview(
        live_db, monkeypatch):
    _setup(live_db, monkeypatch, no_created_at=True)
    p, o = _pipeline(), _overview()
    assert p["funnel"]["contacts_without_created_at"] == 1
    pipe = _cells(_panel(p))
    over = _cells(_render("renderDashAcquisitionFunnel(%s)" % json.dumps(o)))
    assert pipe == over, (pipe, over)
    assert pipe["contacts"].startswith("Contacts acquired at least 7 ")
    assert "1 contact has no created date" in pipe["customer"]
