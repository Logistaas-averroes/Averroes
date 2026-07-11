"""
PR-ADS-143 — Campaign Evidence Truth and Layout Repair.

Proves the Campaign Evidence page presents GENUINE selected-window totals from
durable source tables (never the `campaigns` scheduler snapshot):

  - spend = canonical google_ads_campaign_daily_spend for the window (the SAME
    source Revenue by Source / the Revenue Decision Mart use → reconciles);
  - lead outcomes = durable deduplicated HubSpot leads (event-date grain), with
    the APPROVED junk-rate denominator unchanged;
  - all_time = NO lower date bound → genuine cumulative totals, not a snapshot;
  - a factual, window-safe outcome_status (Path B) — never a recomputed verdict;
  - machine-verifiable reconciliation + audit metadata for every window;
  - no fabricated zeros (unavailable stays None / Unavailable / N/A / —).

Read-only. Revenue / Dashboard / ROAS / FX / mapping untouched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
INDEX_HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
STYLES = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SERVICE = open(os.path.join(ROOT, "services", "campaign_evidence_service.py"),
               encoding="utf-8").read()


# ── Durable-layer fixtures (monkeypatch the repo functions directly) ─────────


def _spend(rows, *, total_native, total_usd, fx_complete=True, fx_missing=0,
           available=True):
    return {
        "available": available, "currency_code": "GBP", "reporting_currency": "USD",
        "fx_complete": fx_complete, "fx_missing_days": fx_missing,
        "total_spend": total_native, "total_spend_usd": total_usd,
        "campaign_count": len(rows), "rows": rows,
    }


def _spend_row(name, native, usd, *, cid=None, fx=True):
    return {"campaign_id": cid, "campaign_name": name, "spend": native,
            "spend_usd": usd, "fx_complete": fx}


def _leads(rows, *, available=True, event_date_safe=True):
    return {"available": available, "rows": rows, "event_date_safe": event_date_safe}


def _lead_rows(spec):
    """spec: {campaign_name: {'qualified': n, 'junk': n, ...}} → flat deduped rows."""
    out = []
    for name, cats in spec.items():
        for cat, n in cats.items():
            out.extend([{"campaign_name": name, "status_category": cat}] * n)
    return out


def _patch(monkeypatch, spend_result, lead_result):
    import db.revenue_repository as repo
    calls = {}
    def _fs(start, end):
        calls["spend"] = (start, end)
        return spend_result
    def _fl(start, end):
        calls["leads"] = (start, end)
        return lead_result
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", _fs)
    monkeypatch.setattr(repo, "fetch_lead_quality", _fl)
    return calls


def _build(window):
    from services.campaign_evidence_service import build_campaign_evidence
    return build_campaign_evidence(window)


# A coherent dataset reused across tests.
def _dataset():
    spend = _spend(
        [_spend_row("Brand - UK", 1000.0, 1260.0, cid="1"),
         _spend_row("Gulf", 500.0, 630.0, cid="2")],
        total_native=1500.0, total_usd=1890.0)
    leads = _leads(_lead_rows({
        "brand - uk": {"qualified": 5, "junk": 1, "in_progress": 2, "unknown": 3},
        "gulf": {"junk": 4, "wrong_fit": 1, "unknown": 2},
    }))
    return spend, leads


# ════════════════ 1. Window contract ════════════════


@pytest.mark.parametrize("window", ["7d", "14d", "30d", "60d", "180d", "all_time"])
def test_all_windows_supported(monkeypatch, window):
    _patch(monkeypatch, *_dataset())
    out = _build(window)
    assert out["window"] == window
    assert "campaigns" in out and "summary" in out and "audit" in out


def test_unknown_window_raises(monkeypatch):
    from analysis.evidence_windows import EvidenceWindowError
    _patch(monkeypatch, *_dataset())
    with pytest.raises(EvidenceWindowError):
        _build("99d")


def test_endpoint_rejects_unknown_window():
    # The HTTP handler maps EvidenceWindowError → 400 (never silently coerced).
    idx = SERVER.find("def api_campaigns(")
    body = SERVER[idx:SERVER.find("\n@app.", idx + 1)]
    assert "EvidenceWindowError" in body and "status_code=400" in body


# ════════════════ 2. all_time = genuine cumulative (no lower bound, no snapshot) ══


def test_all_time_passes_no_lower_bound(monkeypatch):
    calls = _patch(monkeypatch, *_dataset())
    _build("all_time")
    # start is None for all_time → the canonical query drops its lower bound.
    assert calls["spend"][0] is None
    assert calls["leads"][0] is None


def test_numeric_window_has_lower_bound(monkeypatch):
    calls = _patch(monkeypatch, *_dataset())
    _build("30d")
    assert calls["spend"][0] is not None      # a real start date
    assert calls["leads"][0] is not None


def test_service_never_touches_campaigns_snapshot_table():
    # No AVG()/SUM() over overlapping snapshot rows; the service reads durable
    # repo functions only, never the `campaigns` scheduler snapshot table.
    assert "FROM campaigns" not in SERVICE
    assert "AVG(" not in SERVICE and "determine_verdict" not in SERVICE
    assert "fetch_canonical_campaign_spend" in SERVICE
    assert "fetch_lead_quality" in SERVICE


def test_all_time_is_cumulative_not_snapshot(monkeypatch):
    # all_time returns the genuine union total (both campaigns), computed from the
    # cumulative source — not a single latest snapshot row.
    _patch(monkeypatch, *_dataset())
    out = _build("all_time")
    assert out["all_time"] is True
    assert out["summary"]["spend_native"] == 1500.0     # cumulative, not one run
    assert out["summary"]["spend_usd"] == 1890.0


# ════════════════ 3. Reconciliation (every window) ════════════════


@pytest.mark.parametrize("window", ["7d", "30d", "180d", "all_time"])
def test_spend_reconciles_to_canonical(monkeypatch, window):
    _patch(monkeypatch, *_dataset())
    out = _build(window)
    # Sum of per-campaign native == canonical native total; USD likewise (FX ok).
    native_sum = sum(c["spend_native"] for c in out["campaigns"]
                     if c["spend_native"] is not None)
    usd_sum = sum(c["spend_usd"] for c in out["campaigns"]
                  if c["spend_usd"] is not None)
    assert round(native_sum, 2) == out["summary"]["spend_native"] == 1500.0
    assert round(usd_sum, 2) == out["summary"]["spend_usd"] == 1890.0
    assert out["audit"]["spend_reconciliation_status"] == "pass"


def test_lead_totals_reconcile_to_deduped_aggregate(monkeypatch):
    _patch(monkeypatch, *_dataset())
    out = _build("30d")
    sql_sum = sum(c["confirmed_sqls"] for c in out["campaigns"]
                  if c["confirmed_sqls"] is not None)
    junk_sum = sum(c["confirmed_junk"] for c in out["campaigns"]
                   if c["confirmed_junk"] is not None)
    assert sql_sum == out["summary"]["confirmed_sqls_total"] == 5   # brand-uk qualified
    assert junk_sum == out["summary"]["confirmed_junk_total"] == 5  # 1 + 4
    assert out["audit"]["lead_reconciliation_status"] == "pass"


def test_audit_block_has_all_required_fields(monkeypatch):
    _patch(monkeypatch, *_dataset())
    audit = _build("all_time")["audit"]
    for key in ("spend_source", "lead_source", "window_start", "window_end",
                "all_time", "fx_status", "spend_reconciliation_status",
                "lead_reconciliation_status"):
        assert key in audit, f"audit missing {key}"
    assert audit["all_time"] is True and audit["window_start"] is None


# ════════════════ 4. Outcome status (Path B — window-safe, factual) ════════════


def _status_for(monkeypatch, spend_rows, lead_spec, *, name, total_native=0.0,
                total_usd=0.0, fx_complete=True, lead_available=True):
    spend = _spend(spend_rows, total_native=total_native, total_usd=total_usd,
                   fx_complete=fx_complete)
    leads = _leads(_lead_rows(lead_spec), available=lead_available)
    _patch(monkeypatch, spend, leads)
    out = _build("30d")
    for c in out["campaigns"]:
        if c["campaign_name"] == name or c["campaign_name"] == name.lower():
            return c
    return None


def test_status_sql_producer(monkeypatch):
    c = _status_for(monkeypatch, [_spend_row("A", 100.0, 120.0)],
                    {"a": {"qualified": 3, "junk": 1}}, name="A",
                    total_native=100.0, total_usd=120.0)
    assert c["outcome_status"] == "SQL producer"


def test_status_junk_heavy(monkeypatch):
    c = _status_for(monkeypatch, [_spend_row("B", 100.0, 120.0)],
                    {"b": {"junk": 8, "wrong_fit": 1}}, name="B",
                    total_native=100.0, total_usd=120.0)
    assert c["outcome_status"] == "Junk-heavy"     # 0 SQL, junk ≥ 25%, sample ≥ 5


def test_status_spend_without_sql(monkeypatch):
    c = _status_for(monkeypatch, [_spend_row("C", 100.0, 120.0)],
                    {"c": {"unknown": 2}}, name="C",
                    total_native=100.0, total_usd=120.0)
    assert c["outcome_status"] == "Spend without SQL proof"


def test_status_mapping_review_leads_without_spend(monkeypatch):
    # Campaign has lead outcomes but NO canonical spend row → Mapping review
    # (absence of a spend row is never treated as £0).
    c = _status_for(monkeypatch, [], {"orphan": {"in_progress": 1, "unknown": 2}},
                    name="orphan")
    assert c["outcome_status"] == "Mapping review"
    assert c["spend_native"] is None and c["mapping_status"] == "unmapped_spend"


def test_status_data_unavailable(monkeypatch):
    # Both sources down → Data unavailable, never fabricated zeros.
    spend = _spend([], total_native=None, total_usd=None, available=False)
    leads = _leads([], available=False)
    _patch(monkeypatch, spend, leads)
    out = _build("30d")
    assert out.get("db_unavailable") is True
    assert out["summary"]["confirmed_sqls_total"] is None


# ════════════════ 5. No fabricated zeros / FX-safe / CPQL ════════════════


def test_lead_only_campaign_spend_is_none_not_zero(monkeypatch):
    c = _status_for(monkeypatch, [], {"orphan": {"qualified": 2}}, name="orphan")
    assert c["spend_native"] is None and c["spend_usd"] is None   # never 0


def test_spend_only_campaign_leads_are_genuine_zero(monkeypatch):
    # Lead source is live and this campaign has no rows → genuine 0 (not None).
    c = _status_for(monkeypatch, [_spend_row("D", 50.0, 60.0)], {"other": {"qualified": 1}},
                    name="D", total_native=50.0, total_usd=60.0)
    assert c["confirmed_sqls"] == 0 and c["confirmed_junk"] == 0
    assert c["cpql_usd"] is None      # 0 SQLs → CPQL N/A, never $0


def test_fx_incomplete_usd_is_none_native_present(monkeypatch):
    spend = _spend([_spend_row("E", 200.0, None, fx=False)],
                   total_native=200.0, total_usd=None, fx_complete=False, fx_missing=2)
    leads = _leads(_lead_rows({"e": {"qualified": 2}}))
    _patch(monkeypatch, spend, leads)
    out = _build("30d")
    c = out["campaigns"][0]
    assert c["spend_native"] == 200.0 and c["spend_usd"] is None
    assert c["cpql_usd"] is None                       # no USD → no CPQL
    assert out["audit"]["fx_status"] == "incomplete"
    assert out["summary"]["spend_usd"] is None          # withheld, never native-as-USD


def test_cpql_is_usd_spend_over_sqls(monkeypatch):
    c = _status_for(monkeypatch, [_spend_row("F", 1000.0, 1260.0)],
                    {"f": {"qualified": 2}}, name="F",
                    total_native=1000.0, total_usd=1260.0)
    assert c["cpql_usd"] == 630.0                        # 1260 / 2


def test_junk_rate_denominator_excludes_unknown(monkeypatch):
    # Approved denominator = qualified+in_progress+junk+wrong_fit (excludes unknown).
    c = _status_for(monkeypatch, [_spend_row("G", 10.0, 12.0)],
                    {"g": {"qualified": 1, "junk": 1, "unknown": 8}}, name="G",
                    total_native=10.0, total_usd=12.0)
    # verdicted = 2 → junk_rate = 50.0 (NOT 10% over the 10 total leads)
    assert c["junk_rate_pct"] == 50.0 and c["verdicted_leads"] == 2
    assert "verdicted = (qualified or 0) + (in_progress or 0) + (junk or 0) + (wrong_fit or 0)" in SERVICE


# ════════════════ 6. Overall CPQL + summary reconcile with canonical ════════════


def test_summary_spend_equals_canonical(monkeypatch):
    _patch(monkeypatch, *_dataset())
    out = _build("all_time")
    # KPI spend is the canonical total (reconciles with Revenue by Source / mart).
    assert out["summary"]["spend_usd"] == 1890.0
    assert out["summary"]["spend_native"] == 1500.0
    assert out["summary"]["overall_cpql_usd"] == round(1890.0 / 5, 2)


# ════════════════ 7. Read-only ════════════════


def test_service_is_read_only():
    for verb in ("INSERT", "UPDATE ", "DELETE", "DROP", "TRUNCATE",
                 "requests.post", ".mutate(", "set_budget", "set_bid"):
        assert verb not in SERVICE


# ════════════════ 8. Frontend — no snapshot, clean layout, factual status ════════


def test_no_snapshot_language_on_campaign_page():
    region = APP_JS[APP_JS.find("// ── Campaign Evidence page (PR-ADS-143)"):
                    APP_JS.find("// ── Waste Terms page")]
    for banned in ("Latest Snapshot", "latest stored snapshot", "snapshot_metric_period",
                   "per-run snapshot", "Snapshot metrics"):
        assert banned not in region, f"snapshot copy leaked: {banned}"
    # PAGE_EXPLANATIONS.campaigns no longer PROMOTES snapshot semantics (the only
    # allowed mention is the honest "not a scheduler snapshot" clarification).
    exp = APP_JS[APP_JS.find("campaigns: {"):APP_JS.find("campaigns: {") + 700]
    assert "latest snapshot" not in exp.lower()
    assert "latest stored snapshot" not in exp.lower()


def test_clean_table_headers():
    region = APP_JS[APP_JS.find("function renderCampaignDecisionTable"):
                    APP_JS.find("function renderCampaignDecisionTable") + 1400]
    for h in (">Campaign<", ">Status<", ">Spend<", ">Leads<", ">SQLs<",
              ">Junk<", ">Junk Rate<", ">CPQL<"):
        assert h in region, f"missing clean header {h}"
    assert "— Latest Snapshot" not in region
    assert "Google Ads" not in region            # no repeated source in headers
    assert ">Decision<" not in region            # verdict column replaced by Status


def test_full_width_page_class():
    assert 'id="page-campaigns" class="page page--wide"' in INDEX_HTML
    assert ".page--wide { max-width: none; }" in STYLES


def test_single_filter_row_no_verdict_chips():
    region = APP_JS[APP_JS.find("function renderCampaignEvidenceFilters"):
                    APP_JS.find("function filterCampaignEvidence")]
    assert 'id="campaign-search"' in region
    assert 'id="campaign-status"' in region and 'id="campaign-outcome"' in region
    assert 'id="campaign-sort"' in region and 'id="campaign-clear-filters"' in region
    # The separate All/SCALE/HOLD/FIX/CUT verdict chip row is gone.
    assert "campaign-verdict-chip" not in region
    assert "data-campaign-verdict" not in APP_JS


def test_factual_status_badges_not_verdicts():
    assert "campaign-status-badge" in APP_JS and "campaign-status-badge" in STYLES
    for s in ("SQL producer", "Junk-heavy", "Spend without SQL proof",
              "Mapping review", "No outcome evidence", "Data unavailable"):
        assert s in APP_JS
    # No SCALE/HOLD/FIX/CUT action verdicts drive the campaign table.
    assert "CAMPAIGN_VERDICT_ORDER" not in APP_JS


def test_no_duplicate_source_strip_inline():
    # Campaign page no longer renders the truth-chip strip / Depends-on inline.
    assert "campaignEvidenceChips" not in APP_JS
    # renderPageExplanation suppresses the inline strip for campaigns (moved to Help).
    assert 'pageKey === "campaigns" && !options.forceVisible' in APP_JS


def test_kpi_cards_are_genuine_window_kpis():
    region = APP_JS[APP_JS.find("function renderCampaignEvidenceKPIs"):
                    APP_JS.find("function renderCampaignEvidenceKPIs") + 2000]
    for label in (">Campaigns<", ">Spend<", ">Confirmed SQLs<", ">Confirmed Junk<",
                  ">Overall CPQL<"):
        assert label in region
    assert "Latest Snapshot" not in region and "Spend Evidence" not in region


# ════════════════ 9. Frontend drawer — window totals, no snapshot ════════════


def test_drawer_uses_window_card_no_snapshot():
    dr = APP_JS[APP_JS.find("function renderCampaignDrawer"):
                APP_JS.find("function _appendDrawerEvidenceSections")]
    assert "snapshot" not in dr.lower()
    # No scheduler verdict in the drawer (camp.verdicted_leads is a lead COUNT, ok).
    assert "camp.verdict_reason" not in dr and "verdictBadge(" not in dr
    assert "Google Conv" not in dr                       # no snapshot conversions
    assert "selected-window evidence" in dr
    assert "campaignStatusBadge(camp.outcome_status)" in dr


def test_backend_drawer_card_is_window_evidence():
    idx = SERVER.find("def _build_campaign_detail(")
    body = SERVER[idx:idx + 3500]
    assert "build_campaign_evidence_row" in body
    assert '"snapshot_date"' not in body and '"verdict"' not in body


# ════════════════ 10. Regression — Revenue/Dashboard/mapping untouched ════════════


def test_revenue_and_dashboard_services_not_imported_into_campaign_service():
    for foreign in ("dashboard_overview_service", "revenue_decision_mart",
                    "source_attribution_service", "build_revenue_by_source"):
        assert foreign not in SERVICE


def test_campaigns_handler_delegates_to_service():
    idx = SERVER.find("def api_campaigns(")
    body = SERVER[idx:SERVER.find("\n@app.", idx + 1)]
    assert "build_campaign_evidence(" in body
    assert "FROM campaigns" not in body        # no snapshot SQL in the handler
