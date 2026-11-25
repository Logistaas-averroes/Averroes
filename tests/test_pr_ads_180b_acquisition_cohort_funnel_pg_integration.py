"""PR-ADS-180B — the acquisition-cohort funnel and the Activity bounds, on
PostgreSQL, through the real repository reads and the real builders.

The fixture is PR-ADS-177A's (spend, geo, a placed deal, the canonical
contact funnel and its sync state), plus two contacts of its own. Every
figure is read by the production SQL — ``fetch_acquisition_cohort_contacts``
with its new stage evidence, ``fetch_undated_stage_reach`` — and certified by
the cohort audit's OWN SQL (``independent_counts``). The UI cases render the
real payload with the real ``static/app.js`` in ``node``.

On ``bf9deed`` the cohort read carries no MQL / Opportunity / Customer-stage
evidence, the mart cohort carries no stage totals, the Overview carries no
acquisition funnel, and the Activity Lead count is exact with seven undated
contacts outside the window's rows (case 06).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest  # noqa: F401  (the live_db fixture skips without PostgreSQL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, live_db,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import WINDOW  # noqa: E402
from tests.test_pr_ads_177a_one_sql_count_pg_integration import (  # noqa: E402
    _ready, _yesterday_at,
)

STAGES = ("reached_mql", "reached_opportunity", "reached_customer_stage")


def _json(payload):
    return json.loads(json.dumps(payload, default=str))


def _extra_contacts(*, no_created_at=False):
    """177A's cohort (Google Ads: c-uk customer, c-uk2 lead, c-ae SQL dated,
    c-fj opportunity, c-nolb SQL; organic c-org customer) plus:

    * c-mql  Google Ads, stage `lead` with an MQL DATE — reached MQL by its
      date, not its stage (the stage moved back);
    * c-none (optional) no created date — in no window."""
    from db import writers
    y = _yesterday_at()
    rows = [{"contact_id": "c-mql", "created_at": y, "last_modified_at": y,
             "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "brand - uk",
             "lifecycle_stage": "lead", "date_entered_mql": y, "country": "United Kingdom"}]
    if no_created_at:
        rows.append({"contact_id": "c-none", "created_at": None, "last_modified_at": y,
                     "hs_analytics_source": "PAID_SEARCH", "lifecycle_stage": "lead"})
    writers.upsert_hubspot_contact_funnel(rows)


def _setup(cluster, monkeypatch, *, fresh=True, no_created_at=False):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(cluster, fresh=fresh)
    _extra_contacts(no_created_at=no_created_at)


def _mart(view, **kw):
    from services.revenue_decision_mart import build_revenue_decision_mart
    return _json(build_revenue_decision_mart(view=view, window=WINDOW, **kw))


def _render(script):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app('_currentUser = {"username": "u", "role": "admin"}; ' + script)


def _cells(html):
    from tests.test_pr_ads_180b_acquisition_cohort_funnel import _cells as cells
    return cells(html)


# ── 1. the production read and the cohort's stages ──────────────────────────
def test_01_the_cohort_read_carries_every_stages_evidence_and_the_mart_counts_it(
        live_db, monkeypatch):
    """Google Ads cohort: c-uk (customer), c-uk2 (lead), c-ae (SQL dated),
    c-fj (opportunity), c-nolb (SQL), c-mql (lead, MQL dated) → 6 leads;
    MQL 5 (all but c-uk2), SQL 4, Opportunity 2 (c-uk, c-fj), Customer stage
    1 (c-uk). Each stage cumulative."""
    _setup(live_db, monkeypatch)
    m = _mart("campaign", settled=True)
    c = m["cohort"]
    assert c["sql_status"] == "published"
    t = c["totals"]
    assert (t["leads"], t["reached_mql"], t["sqls"], t["reached_opportunity"],
            t["reached_customer_stage"]) == (6, 5, 4, 2, 1)
    assert c["contacts_without_created_at"] == 0
    # All sources add c-org (organic, customer).
    alls = c["breakdown"]["all_sources"]
    assert (alls["contacts_acquired"], alls["reached_mql"], alls["sqls"],
            alls["reached_opportunity"], alls["reached_customer_stage"]) == (7, 6, 5, 3, 2)
    # The production read itself returns the evidence, direct and recovered apart.
    from db import crm_funnel_repository as repo
    from services import marketing_outcome_cohort_service as M
    start_at, end_before = M.window_instants(None, date.today())
    rows = {r["contact_id"]: r for r in
            repo.fetch_acquisition_cohort_contacts(start_at, end_before)["rows"]}
    assert rows["c-mql"]["mql_entered_direct"] is not None
    assert rows["c-mql"]["opportunity_entered_direct"] is None
    assert {"customer_entered_recovered", "opportunity_entered_recovered"} <= set(rows["c-uk"])


def test_02_the_audits_certify_the_stages_against_their_own_sql(live_db, monkeypatch):
    """The cohort audit's independent SQL re-derives every stage over the
    surface's own settled days; conservation and absence hold on every surface
    the audits build."""
    from scripts import audit_campaigns_page_certification as campaigns_audit
    from scripts import audit_marketing_outcome_cohorts as audit
    _setup(live_db, monkeypatch)
    payloads = audit._build_mart_payloads(WINDOW, None)
    settled = payloads["mart/campaign_settled"]
    independent = audit.independent_counts(*audit._settled_instants(settled))
    assert {k: independent[k] for k in STAGES} == {
        "reached_mql": 6, "reached_opportunity": 3, "reached_customer_stage": 2}
    a = audit.Audit()
    audit.audit_campaign_cohort(a, window=WINDOW, payload=settled, independent=independent)
    audit.audit_mart_window(a, window=WINDOW, payloads=payloads)
    assert a.violations == [], a.violations
    held = {c["check"] for c in a.checks if c["ok"]}
    assert f"[campaigns:{WINDOW}] stage_reach" in held
    assert f"[campaigns:{WINDOW}] stage_conservation" in held
    assert f"[business:{WINDOW}] dashboard/overview acquisition_funnel" in held
    f = campaigns_audit.Findings()
    campaigns_audit.audit_window(WINDOW, f)
    assert not [v for v in f.violations if "conservation" in v], f.violations


def test_02b_counterfactual_the_audit_sees_a_stage_the_surface_got_wrong(live_db, monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    _setup(live_db, monkeypatch)
    payloads = audit._build_mart_payloads(WINDOW, None)
    settled = payloads["mart/campaign_settled"]
    independent = audit.independent_counts(*audit._settled_instants(settled))
    settled["cohort"]["breakdown"]["all_sources"]["reached_opportunity"] += 1
    a = audit.Audit()
    audit.audit_campaign_cohort(a, window=WINDOW, payload=settled, independent=independent)
    assert any("stage_reach" in v and "reached_opportunity" in v for v in a.violations)
    overview = payloads["dashboard/overview"]
    overview["lifecycle_acquisition_funnel"]["totals"]["reached_sql"] -= 1
    a = audit.Audit()
    audit.audit_mart_window(a, window=WINDOW, payloads={"dashboard/overview": overview})
    assert any("acquisition_funnel" in v for v in a.violations)


# ── 3. withheld: no stage count anywhere ─────────────────────────────────────
def test_03_an_incomplete_contact_sync_withholds_every_stage_on_every_surface(
        live_db, monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    _setup(live_db, monkeypatch, fresh=False)
    payloads = audit._build_mart_payloads(WINDOW, None)
    for name, p in payloads.items():
        assert p["cohort"]["sql_status"] == "withheld", name
        assert all(p["cohort"]["totals"][f] is None for f in STAGES), name
        # PR-ADS-180B-F1 (m4): the table-wide no-created-date count is a
        # disclosure, published whatever the verdict, one value in every copy.
        assert p["cohort"]["contacts_without_created_at"] == 0, name
        assert audit.created_at_disclosure_problems(p) == [], name
        assert audit.mart_withheld_exposures(p) == [], (name, audit.mart_withheld_exposures(p))
    f = payloads["dashboard/overview"]["lifecycle_acquisition_funnel"]
    assert f["publication"] == {"status": "withheld", "reason": "source_bootstrap_incomplete"}
    assert f["totals"] is None
    a = audit.Audit()
    audit.audit_mart_window(a, window=WINDOW, payloads=payloads)
    assert a.violations == [], a.violations


# ── 4. the real payloads on the real pages ───────────────────────────────────
def test_04_overview_campaigns_and_countries_draw_the_cohort_funnel(live_db, monkeypatch):
    from services.dashboard_overview_service import build_dashboard_overview
    _setup(live_db, monkeypatch, no_created_at=True)
    overview = _json(build_dashboard_overview(window=WINDOW))
    f = overview["lifecycle_acquisition_funnel"]
    assert f["totals"] == {"contacts": 7, "reached_mql": 6, "reached_sql": 5,
                           "reached_opportunity": 3, "reached_customer_stage": 2}
    assert f["contacts_without_created_at"] == 1
    ov = _cells(_render("renderDashAcquisitionFunnel(%s)" % json.dumps(overview)))
    assert ov["contacts"].startswith("Contacts acquired at least 7 ")
    assert ov["sql"].startswith("Reached SQL at least 5 of the contacts acquired")
    assert "1 contact has no created date" in ov["customer"]
    camp = _mart("campaign", settled=True)
    html = _render('campaignsPageMart = %s; campaignsPageStatus = "ok"; renderCampaignsPage();'
                   ' __els["campaigns-page-body"].innerHTML' % json.dumps(camp))
    assert camp["readiness"]["spend_decision_ready"] is True
    block = html[html.index("mart-cohort-funnel-panel"):]
    cells = _cells(block[:block.index("</section>")])
    assert cells["mql"].startswith("Reached MQL at least 5 ")
    assert cells["customer"].startswith("Reached Customer stage at least 1 ")
    country = _mart("country")
    html = _render("roasCountryMart = %s; roasCountriesData = roasCountryMart.rows || [];"
                   ' renderRoasCountriesPage(); __els["roas-countries-table-body"].innerHTML'
                   % json.dumps(country))
    block = html[html.index("mart-cohort-funnel-panel"):]
    cells = _cells(block[:block.index("</section>")])
    t = country["cohort"]["totals"]
    assert cells["sql"].startswith(f"Reached SQL at least {t['sqls']} ")
    assert cells["opportunity"].startswith(f"Reached Opportunity at least {t['reached_opportunity']} ")


# ── 5. Activity: undated contacts outside the window's rows (finding b) ──────
def _quarter(key="last_quarter"):
    from services.canonical_contract import resolve_canonical_window
    w = resolve_canonical_window(key)
    return date.fromisoformat(w["start_date"]), date.fromisoformat(w["end_date"])


def _at(d, hour=10):
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


def _outside_fixture():
    """audit180a/probe_outside.py on a real table, window = last quarter:
    ten Leads dated in it; four reached Lead undated with their MQL dated
    AFTER it; three at stage lead with no date at all; and one at stage lead,
    undated, created after the window (provably not in it)."""
    from datetime import timedelta
    from db import writers
    start, end = _quarter()
    created = _at(start)
    inside, after = _at(start + timedelta(days=4)), _at(end + timedelta(days=15))
    rows = [{"contact_id": f"in-{i}", "created_at": created, "last_modified_at": created,
             "lifecycle_stage": "marketingqualifiedlead", "hs_analytics_source": "ORGANIC_SEARCH",
             "date_entered_lead": inside, "date_entered_mql": inside} for i in range(10)]
    rows += [{"contact_id": f"out-mql-{i}", "created_at": created, "last_modified_at": created,
              "lifecycle_stage": "marketingqualifiedlead", "hs_analytics_source": "ORGANIC_SEARCH",
              "date_entered_mql": after} for i in range(4)]
    rows += [{"contact_id": f"out-lead-{i}", "created_at": created, "last_modified_at": created,
              "lifecycle_stage": "lead", "hs_analytics_source": "ORGANIC_SEARCH"}
             for i in range(3)]
    rows += [{"contact_id": "late", "created_at": after, "last_modified_at": after,
              "lifecycle_stage": "lead", "hs_analytics_source": "ORGANIC_SEARCH"}]
    writers.upsert_hubspot_contact_funnel(rows)
    return start, end


def _edge_fixture(cluster, start, end):
    """PR-ADS-180B-F1 (m2): the edges of the one narrowing, on the real table.

    The window's end is the database's own ``end::date + 1 day`` instant
    (read back, so the case holds in any session time zone):

    * ``edge-last-day``  stage lead, undated, created ONE MINUTE before that
      instant — on the window's last day: counted;
    * ``edge-no-created`` stage lead, undated, NULL ``created_at``: never
      ruled out, counted;
    * ``edge-day-after`` stage lead, undated, created AT that instant — the
      day after the window: ruled out;
    * ``edge-recovered`` stage lead, created before the window, NO direct Lead
      date, its Lead date recovered from lifecycle history (also before the
      window): it has an effective entry date, so it is not undated.
    """
    from datetime import timedelta
    from db import writers
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT (%s::date + INTERVAL '1 day')::timestamptz", (end,))
        boundary = cur.fetchone()[0]
    early = _at(start - timedelta(days=40))
    base = {"lifecycle_stage": "lead", "hs_analytics_source": "ORGANIC_SEARCH"}
    writers.upsert_hubspot_contact_funnel([
        {**base, "contact_id": "edge-last-day", "created_at": boundary - timedelta(minutes=1),
         "last_modified_at": boundary - timedelta(minutes=1)},
        {**base, "contact_id": "edge-no-created", "created_at": None,
         "last_modified_at": boundary},
        {**base, "contact_id": "edge-day-after", "created_at": boundary,
         "last_modified_at": boundary},
        {**base, "contact_id": "edge-recovered", "created_at": early, "last_modified_at": early},
    ])
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO hubspot_lifecycle_stage_history
                   (contact_id, funnel_event, entered_at, hubspot_property,
                    hubspot_value, hubspot_source_type, lifecycle_rule_version,
                    recovery_run_id)
               VALUES ('edge-recovered', 'lead', %s, 'lifecyclestage', 'lead',
                       'CRM_UI', 'v1', 'pr-ads-180b-f1-fixture')""",
            (_at(start - timedelta(days=30)),))
    return boundary


def test_05_the_undated_reach_counts_outside_the_windows_rows_on_the_real_table(live_db):
    from db import crm_funnel_repository as repo
    start, end = _outside_fixture()
    _edge_fixture(live_db, start, end)
    window_rows = repo.fetch_funnel_contacts(start, end)["rows"]
    assert len(window_rows) == 10                     # none of the others is in them
    # The recovered Lead date is read: the contact has an effective date.
    every = {r["contact_id"]: r for r in repo.fetch_funnel_contacts(None, None)["rows"]}
    assert every["edge-recovered"]["date_entered_lead"] is not None
    read = repo.fetch_undated_stage_reach(start, end)
    assert read["available"] is True
    # Lead: 4 + 3 undated, could belong; plus edge-last-day (created on the
    # window's last day) and edge-no-created (no created date is never ruled
    # out). "late" and edge-day-after are ruled out by their creation;
    # edge-recovered is not undated.
    assert read["counts"] == {"lead": 9, "mql": 0, "sql": 0, "opportunity": 0, "customer": 0}
    # All Time rules nothing out: 4 + 3 + late + the three undated edges.
    assert repo.fetch_undated_stage_reach(None, None)["counts"]["lead"] == 11


def test_06_the_activity_lead_count_is_a_lower_bound_through_the_real_reads(live_db):
    """On bf9deed: "Leads 10", exact. Through the real repository and the real
    builders it is now "at least 10", naming the seven."""
    from services import dashboard_overview_service as O
    _outside_fixture()
    now = datetime.now(timezone.utc)
    funnel = O._lifecycle_funnel_block("last_quarter", now=now)
    act = _json(O._lifecycle_activity_block(funnel, "last_quarter", now))
    cells = _cells(_render("renderDashLifecycleActivity(%s)"
                           % json.dumps({"lifecycle_activity": act})))
    assert cells["contacts"].startswith("Leads at least 10 "), cells["contacts"]
    lead = {s["event"]: s for s in act["stages"]}["lead"]
    assert lead["entered"] == 10
    assert lead["missing_entry_date_contacts"] == 0
    assert lead["undated_contacts_could_belong"] == 7
    assert "7 contacts reached this stage with no entry date" in cells["contacts"]
    assert "No comparison" in cells["contacts"]
