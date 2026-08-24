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


def _region(text, start_marker, end_marker):
    """Slice text between two markers, asserting BOTH exist — so a missing marker
    fails loudly instead of yielding an empty slice that false-passes 'not in'."""
    i = text.find(start_marker)
    assert i != -1, f"start marker not found: {start_marker!r}"
    j = text.find(end_marker, i + len(start_marker))
    assert j != -1, f"end marker not found: {end_marker!r}"
    assert j > i
    return text[i:j]


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
    # Default a stable campaign_id from the name (canonical spend is keyed by id).
    return {"campaign_id": cid if cid is not None else f"id-{name.lower()}",
            "campaign_name": name, "spend": native, "spend_usd": usd, "fx_complete": fx}


def _leads(rows, *, available=True, event_date_safe=True):
    return {"available": available, "rows": rows, "event_date_safe": event_date_safe}


def _identity(mappings=None, *, available=True):
    return {"available": available, "mappings": mappings or []}


def _lead_rows(spec):
    """spec: {campaign_name: {'qualified': n, 'junk': n, ...}} → flat deduped rows."""
    out = []
    for name, cats in spec.items():
        for cat, n in cats.items():
            out.extend([{"campaign_name": name, "status_category": cat}] * n)
    return out


def _patch(monkeypatch, spend_result, lead_result, identity_result=None):
    import db.revenue_repository as repo
    calls = {}
    def _fs(start, end):
        calls["spend"] = (start, end)
        return spend_result
    def _fl(start, end):
        calls["leads"] = (start, end)
        return lead_result
    def _fi(customer_id=None):
        calls["identity"] = customer_id
        return identity_result if identity_result is not None else _identity()
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", _fs)
    monkeypatch.setattr(repo, "fetch_lead_quality", _fl)
    monkeypatch.setattr(repo, "fetch_campaign_identity", _fi)
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


def test_unavailable_response_is_consistent_shape():
    # The handler's last-resort fallback returns the SAME shape as a live response
    # (window bounds, semantics, audit) — reconciliation statuses "unavailable",
    # every metric null (never a partial ad-hoc dict / fabricated 0).
    from services.campaign_evidence_service import unavailable_response
    r = unavailable_response("all_time")
    for key in ("window", "window_start", "window_end", "all_time", "generated_at",
                "spend_semantics", "reporting_currency", "lead_semantics",
                "campaigns", "summary", "audit", "db_unavailable"):
        assert key in r, f"fallback missing {key}"
    assert r["db_unavailable"] is True and r["campaigns"] == []
    assert r["summary"]["spend_usd"] is None and r["summary"]["confirmed_sqls_total"] is None
    assert r["audit"]["spend_reconciliation_status"] == "unavailable"
    assert r["audit"]["lead_reconciliation_status"] == "unavailable"
    assert r["audit"]["fx_status"] == "unavailable"


def test_handler_fallback_uses_unavailable_response():
    idx = SERVER.find("def api_campaigns(")
    body = SERVER[idx:SERVER.find("\n@app.", idx + 1)]
    assert "unavailable_response(resolved_window)" in body


def test_evidence_row_return_type_is_dict_not_none():
    # Return annotation + docstring match behaviour (always a dict, never None).
    seg = SERVICE[SERVICE.find("def build_campaign_evidence_row("):
                  SERVICE.find("def build_campaign_evidence_row(") + 900]
    assert "-> dict[str, Any]:" in seg
    assert "Always returns a dict (never None)" in seg


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
    assert c["spend_native"] is None and c["mapping_status"] == "unmatched"


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
    region = _region(APP_JS, "// ── Campaign Evidence page (PR-ADS-143)",
                     "// ── Flagged Waste Terms page — RETIRED (PR-ADS-153D)")
    for banned in ("Latest Snapshot", "latest stored snapshot", "snapshot_metric_period",
                   "per-run snapshot", "Snapshot metrics"):
        assert banned not in region, f"snapshot copy leaked: {banned}"
    # PAGE_EXPLANATIONS.campaigns no longer PROMOTES snapshot semantics (the only
    # allowed mention is the honest "not a scheduler snapshot" clarification).
    exp = _region(APP_JS, "campaigns: {", "\n  },")
    assert "latest snapshot" not in exp.lower()
    assert "latest stored snapshot" not in exp.lower()


def test_clean_table_headers():
    region = _region(APP_JS, "function renderCampaignDecisionTable",
                     "function campaignSpendCell")
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
    region = _region(APP_JS, "function renderCampaignEvidenceFilters",
                     "function filterCampaignEvidence")
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
    region = _region(APP_JS, "function renderCampaignEvidenceKPIs",
                     "function renderCampaignEvidenceFilters")
    for label in (">Campaigns<", ">Spend<", ">Confirmed SQLs<", ">Confirmed Junk<",
                  ">Overall CPQL<"):
        assert label in region
    assert "Latest Snapshot" not in region and "Spend Evidence" not in region


# ════════════════ 9. Frontend drawer — window totals, no snapshot ════════════


def test_drawer_uses_window_card_no_snapshot():
    dr = _region(APP_JS, "function renderCampaignDrawer",
                 "function _appendDrawerEvidenceSections")
    assert "snapshot" not in dr.lower()
    # No scheduler verdict in the drawer (camp.verdicted_leads is a lead COUNT, ok).
    assert "camp.verdict_reason" not in dr and "verdictBadge(" not in dr
    assert "Google Conv" not in dr                       # no snapshot conversions
    assert "selected-window evidence" in dr
    assert "campaignStatusBadge(camp.outcome_status)" in dr


def test_backend_drawer_card_is_window_evidence():
    idx = SERVER.find("def _build_campaign_detail(")
    body = SERVER[idx:idx + 4000]
    assert "build_campaign_drawer_evidence" in body      # service-backed, event-date
    assert '"snapshot_date"' not in body and '"verdict"' not in body
    # No run_date lead-window boundary remains in the drawer.
    assert "run_date >= NOW() - INTERVAL '1 day' * %s AND " not in SERVER[idx:idx + 12000]


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


# ════════════ 11. Durable campaign-identity mapping (PR-ADS-143 round 2) ════════
from datetime import datetime, timezone  # noqa: E402

FIXED_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _by_name(out):
    return {c["campaign_name"]: c for c in out["campaigns"]}


def _by_id(out):
    return {c.get("campaign_id"): c for c in out["campaigns"] if c.get("campaign_id")}


def test_mexico_chile_mapped_to_approved_canonical(monkeypatch):
    spend = _spend([_spend_row("Emerging - Markets", 500.0, 630.0, cid="102")],
                   total_native=500.0, total_usd=630.0)
    ident = _identity([{"customer_id": "111", "campaign_id": "102",
                        "canonical_campaign_name": "Emerging - Markets",
                        "external_campaign_label": "mexico,chile", "match_method": "manual"}])
    leads = _leads(_lead_rows({"mexico,chile": {"qualified": 2}}))
    _patch(monkeypatch, spend, leads, ident)
    out = _build("30d")
    row = _by_id(out)["102"]
    assert row["confirmed_sqls"] == 2                 # mapped onto campaign 102
    assert "mexico,chile" in row["aliases"]
    assert row["mapping_status"] == "mapped"


def test_manual_alias_mapping(monkeypatch):
    spend = _spend([_spend_row("Gulf Region", 300.0, 378.0, cid="9")],
                   total_native=300.0, total_usd=378.0)
    ident = _identity([{"customer_id": "111", "campaign_id": "9",
                        "canonical_campaign_name": "Gulf Region",
                        "external_campaign_label": "gulf-promo-2026", "match_method": "manual"}])
    leads = _leads(_lead_rows({"gulf-promo-2026": {"qualified": 3}}))
    _patch(monkeypatch, spend, leads, ident)
    out = _build("30d")
    assert _by_id(out)["9"]["confirmed_sqls"] == 3


def test_not_google_ads_excluded_from_scope(monkeypatch):
    spend = _spend([_spend_row("Brand - UK", 1000.0, 1260.0, cid="1")],
                   total_native=1000.0, total_usd=1260.0)
    ident = _identity([{"customer_id": "111", "campaign_id": None,
                        "canonical_campaign_name": None,
                        "external_campaign_label": "bing brand", "match_method": "not_google_ads"}])
    leads = _leads(_lead_rows({"brand - uk": {"qualified": 5},
                               "bing brand": {"qualified": 4}}))
    _patch(monkeypatch, spend, leads, ident)
    out = _build("30d")
    # The not_google_ads label is not a table row and is excluded from CPQL scope.
    assert "bing brand" not in _by_name(out)
    cov = out["summary"]["mapping_coverage"]
    assert cov["excluded_not_google_sqls"] == 4
    assert out["summary"]["confirmed_sqls_total"] == 5           # mapped only
    # Overall CPQL uses mapped SQLs only (1260/5), scope disclosed.
    assert out["summary"]["overall_cpql_usd"] == 252.0
    assert out["summary"]["overall_cpql_scope"] == "mapped_only"


def test_unmatched_label_is_mapping_review(monkeypatch):
    spend = _spend([_spend_row("Brand - UK", 1000.0, 1260.0, cid="1")],
                   total_native=1000.0, total_usd=1260.0)
    leads = _leads(_lead_rows({"brand - uk": {"qualified": 5},
                               "totally unknown label": {"junk": 2}}))
    _patch(monkeypatch, spend, leads, _identity([]))
    out = _build("30d")
    review = _by_name(out).get("totally unknown label")
    assert review is not None and review["outcome_status"] == "Mapping review"
    assert review["campaign_id"] is None and review["spend_native"] is None
    assert out["summary"]["mapping_coverage"]["unmatched_sqls"] == 0


def test_two_campaign_ids_same_display_name_not_merged(monkeypatch):
    # Two DISTINCT ids share the display name "Gulf" — they must stay separate, and
    # a lead whose name normalizes to "gulf" is ambiguous → unmatched (never merged).
    spend = _spend([_spend_row("Gulf", 100.0, 126.0, cid="201"),
                    _spend_row("Gulf", 200.0, 252.0, cid="202")],
                   total_native=300.0, total_usd=378.0)
    leads = _leads(_lead_rows({"gulf": {"qualified": 3}}))
    _patch(monkeypatch, spend, leads, _identity([]))
    out = _build("30d")
    ids = _by_id(out)
    assert "201" in ids and "202" in ids                        # both preserved
    assert ids["201"]["confirmed_sqls"] == 0 and ids["202"]["confirmed_sqls"] == 0
    # The ambiguous lead is NOT merged into either id — it is unmatched.
    assert out["summary"]["mapping_coverage"]["unmatched_sqls"] == 3


def test_punctuation_and_spacing_variations_match(monkeypatch):
    spend = _spend([_spend_row("Brand - UK", 1000.0, 1260.0, cid="1")],
                   total_native=1000.0, total_usd=1260.0)
    # Lead label with extra punctuation/spacing normalizes equal to the spend name.
    leads = _leads(_lead_rows({"Brand   -   UK!!": {"qualified": 4}}))
    _patch(monkeypatch, spend, leads, _identity([]))
    out = _build("30d")
    assert _by_id(out)["1"]["confirmed_sqls"] == 4              # exact-normalized match


# ── CPQL alignment (adversarial) ──


def test_unmatched_qualified_lead_never_lowers_canonical_cpql(monkeypatch):
    spend = _spend([_spend_row("Brand - UK", 1000.0, 1260.0, cid="1")],
                   total_native=1000.0, total_usd=1260.0)
    base_leads = _leads(_lead_rows({"brand - uk": {"qualified": 2}}))
    _patch(monkeypatch, spend, base_leads, _identity([]))
    cpql_before = _build("30d")["summary"]["overall_cpql_usd"]   # 1260/2 = 630
    # Add 100 UNMATCHED qualified leads — canonical CPQL must NOT drop.
    poisoned = _leads(_lead_rows({"brand - uk": {"qualified": 2},
                                  "unmatched noise": {"qualified": 100}}))
    _patch(monkeypatch, spend, poisoned, _identity([]))
    after = _build("30d")["summary"]
    assert after["overall_cpql_usd"] == cpql_before == 630.0     # unchanged
    assert after["overall_cpql_scope"] == "mapped_only"
    assert after["mapping_coverage"]["unmatched_sqls"] == 100


def test_per_campaign_cpql_uses_only_that_campaigns_mapped_sqls(monkeypatch):
    spend = _spend([_spend_row("A", 1000.0, 1000.0, cid="a"),
                    _spend_row("B", 2000.0, 2000.0, cid="b")],
                   total_native=3000.0, total_usd=3000.0)
    leads = _leads(_lead_rows({"a": {"qualified": 4}, "b": {"qualified": 10}}))
    _patch(monkeypatch, spend, leads, _identity([]))
    ids = _by_id(_build("30d"))
    assert ids["a"]["cpql_usd"] == 250.0        # 1000/4
    assert ids["b"]["cpql_usd"] == 200.0        # 2000/10


# ── Exact rolling-day boundaries (fixed now) ──


@pytest.mark.parametrize("window,expect", [("7d", 7), ("14d", 14), ("30d", 30),
                                           ("60d", 60), ("180d", 180)])
def test_exact_inclusive_day_boundaries(monkeypatch, window, expect):
    _patch(monkeypatch, _spend([], total_native=0.0, total_usd=0.0),
           _leads([]), _identity([]))
    out = _build_now(window)
    from datetime import date
    start = date.fromisoformat(out["window_start"])
    end = date.fromisoformat(out["window_end"])
    assert (end - start).days + 1 == expect        # exactly N calendar dates
    assert out["all_time"] is False


def test_all_time_has_no_lower_bound(monkeypatch):
    _patch(monkeypatch, _spend([], total_native=0.0, total_usd=0.0),
           _leads([]), _identity([]))
    out = _build_now("all_time")
    assert out["window_start"] is None and out["all_time"] is True


def test_account_timezone_is_europe_london():
    from services.campaign_evidence_service import ACCOUNT_TZ
    assert ACCOUNT_TZ == "Europe/London"


def _build_now(window):
    from services.campaign_evidence_service import build_campaign_evidence
    return build_campaign_evidence(window, now=FIXED_NOW)


# ── Exact numeric days (no snapping) ──


def test_numeric_days_honoured_exactly(monkeypatch):
    _patch(monkeypatch, _spend([], total_native=0.0, total_usd=0.0),
           _leads([]), _identity([]))
    from services.campaign_evidence_service import build_campaign_evidence
    from datetime import date
    for days in (90, 365, 45):
        out = build_campaign_evidence(window=None, days=days, now=FIXED_NOW)
        assert out["window"] == f"{days}d"          # never snapped to a dropdown window
        start = date.fromisoformat(out["window_start"])
        end = date.fromisoformat(out["window_end"])
        assert (end - start).days + 1 == days


def test_out_of_range_days_rejected(monkeypatch):
    _patch(monkeypatch, _spend([], total_native=0.0, total_usd=0.0),
           _leads([]), _identity([]))
    from services.campaign_evidence_service import build_campaign_evidence, EvidenceWindowError
    with pytest.raises(EvidenceWindowError):
        build_campaign_evidence(window=None, days=400, now=FIXED_NOW)


def test_endpoint_days_not_snapped_to_window():
    # The handler passes exact days to the service, never mapping 365→180d etc.
    idx = SERVER.find("def api_campaigns(")
    body = SERVER[idx:SERVER.find("\n@app.", idx + 1)]
    assert "build_campaign_evidence(window=None, days=days)" in body
    assert "_days_to_evidence_window" not in SERVER      # nearest-window mapper removed


# ── Risk-first outcome status (section 7) ──


def test_high_junk_with_sql_is_junk_heavy_not_sql_producer(monkeypatch):
    # 1 SQL + overwhelming, well-sampled junk → Junk-heavy wins (risk-first).
    spend = _spend([_spend_row("Noisy", 500.0, 630.0, cid="n")],
                   total_native=500.0, total_usd=630.0)
    leads = _leads(_lead_rows({"noisy": {"qualified": 1, "junk": 20}}))
    _patch(monkeypatch, spend, leads, _identity([]))
    row = _by_id(_build("30d"))["n"]
    assert row["confirmed_sqls"] == 1 and row["junk_rate_pct"] >= 25
    assert row["outcome_status"] == "Junk-heavy"


def test_junk_heavy_precedence_documented():
    assert "RISK-FIRST" in SERVICE


# ── Freshness dependency (section 6) ──


def test_campaign_freshness_uses_canonical_spend_and_hubspot():
    # The campaigns PAGE freshness now reflects BOTH canonical spend AND HubSpot
    # contacts (worst-of); it no longer tracks only the snapshot `campaigns` table.
    assert 'campaigns:         ["google_ads_api/canonical_spend", "hubspot/contacts"]' in APP_JS
    assert 'campaigns:               ["google_ads_api/canonical_spend", "hubspot/contacts"]' in APP_JS
    # No campaigns-page mapping still points at the snapshot campaigns dataset alone.
    assert 'campaigns:         ["google_ads_api/campaigns"]' not in APP_JS
    assert 'campaigns:               ["google_ads_api/campaigns"]' not in APP_JS
    fresh = open(os.path.join(ROOT, "services", "freshness_service.py"), encoding="utf-8").read()
    assert '"canonical_spend": {' in fresh
    assert '"google_ads_campaign_daily_spend"' in fresh


# ── Audit surfaces mapping + timezone ──


def test_audit_surfaces_identity_and_timezone(monkeypatch):
    _patch(monkeypatch, _spend([_spend_row("A", 10.0, 12.0, cid="a")],
                                total_native=10.0, total_usd=12.0),
           _leads(_lead_rows({"a": {"qualified": 1}})), _identity([]))
    audit = _build("30d")["audit"]
    assert audit["account_timezone"] == "Europe/London"
    assert audit["identity_source"] == "google_ads_campaign_identity (approved mappings)"
    assert audit["identity_available"] is True


# ════════════ 12. Drawer truth repair (PR-ADS-143 round 3) ════════════════════

def _detail(rows, *, available=True):
    return {"available": available, "rows": rows}


def _patch_drawer(monkeypatch, spend_result, lead_result, identity_result, detail_result):
    """Patch the durable layer incl. fetch_campaign_lead_detail for drawer tests."""
    import db.revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e, *_a, **_k: spend_result)
    monkeypatch.setattr(repo, "fetch_lead_quality", lambda s, e: lead_result)
    monkeypatch.setattr(repo, "fetch_campaign_identity",
                        lambda customer_id=None: identity_result)
    monkeypatch.setattr(repo, "fetch_campaign_lead_detail", lambda s, e: detail_result)


def _drawer(window, name, key=None):
    from services.campaign_evidence_service import build_campaign_drawer_evidence
    return build_campaign_drawer_evidence(window, name, campaign_key=key, now=FIXED_NOW)


def _detail_row(label, cat, *, country="MX", cid="c", created="2026-07-05"):
    return {"campaign_name": label, "status_category": cat, "country": country,
            "keyword": "kw", "company": "Co", "mql_status": "x", "contact_id": cid,
            "contact_created_at": created, "has_gclid": False}


def test_drawer_aggregates_across_approved_aliases_and_reconciles(monkeypatch):
    # Canonical "Mexico, Chile, Colombia" (id 500); approved alias "mexico,chile";
    # leads exist ONLY under the alias. Table and drawer must agree exactly.
    spend = _spend([_spend_row("Mexico, Chile, Colombia", 800.0, 1008.0, cid="500")],
                   total_native=800.0, total_usd=1008.0)
    ident = _identity([{"customer_id": "111", "campaign_id": "500",
                        "canonical_campaign_name": "Mexico, Chile, Colombia",
                        "external_campaign_label": "mexico,chile", "match_method": "manual"}])
    lq = _leads(_lead_rows({"mexico,chile": {"qualified": 4, "junk": 2}}))
    detail = _detail([_detail_row("mexico,chile", "qualified", cid=f"q{i}") for i in range(4)]
                     + [_detail_row("mexico,chile", "junk", country="CL", cid=f"j{i}") for i in range(2)])
    _patch_drawer(monkeypatch, spend, lq, ident, detail)

    trow = _by_id(_build("30d"))["500"]
    ev = _drawer("30d", "Mexico, Chile, Colombia", key="500")
    dq = ev["lead_quality"]
    assert dq["confirmed_sqls"] == trow["confirmed_sqls"] == 4
    assert dq["confirmed_junk"] == trow["confirmed_junk"] == 2
    assert dq["total_leads"] == trow["total_leads"] == 6
    assert dq["junk_rate_pct"] == trow["junk_rate_pct"]
    # Aliases feed the label set that scopes every drawer lead section.
    assert "mexico,chile" in ev["label_set"] and "Mexico, Chile, Colombia" in ev["label_set"]
    # Countries + recent leads derived from the SAME alias-scoped population.
    assert {c["country"] for c in ev["countries"]} == {"MX", "CL"}
    assert len(ev["recent_leads"]) == 6


def test_drawer_lead_detail_bounds_on_contact_created_at_not_run_date():
    repo_src = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
    fn = repo_src[repo_src.find("def fetch_campaign_lead_detail("):
                  repo_src.find("def fetch_sql_lead_details(")]
    assert "contact_created_at >=" in fn and "contact_created_at <" in fn
    assert "source_type = 'paid_search'" in fn
    assert "lead_truth_exclusions" in fn
    assert "email_campaign" in fn                      # pseudo/email exclusion
    # The window is NOT bounded on run_date.
    assert "run_date >=" not in fn and "run_date <" not in fn


def test_backfilled_contact_excluded_from_recent_window(monkeypatch):
    # A contact synced recently but CREATED before the window must not appear.
    spend = _spend([_spend_row("Brand - UK", 100.0, 120.0, cid="1")],
                   total_native=100.0, total_usd=120.0)
    lq = _leads(_lead_rows({"brand - uk": {"qualified": 1}}))
    # Simulate the durable event-date filter: only in-window contacts are returned.
    def _detail_fn(start, end):
        rows = [_detail_row("brand - uk", "qualified", cid="fresh", created="2026-07-05")]
        old = _detail_row("brand - uk", "qualified", cid="backfilled", created="2020-01-01")
        # The real query drops old contact_created_at; the fake honours the window.
        from datetime import date
        if start is None or date.fromisoformat(old["contact_created_at"]) >= start:
            rows.append(old)
        return {"available": True, "rows": rows}
    import db.revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e, *_a, **_k: spend)
    monkeypatch.setattr(repo, "fetch_lead_quality", lambda s, e: lq)
    monkeypatch.setattr(repo, "fetch_campaign_identity", lambda customer_id=None: _identity([]))
    monkeypatch.setattr(repo, "fetch_campaign_lead_detail", _detail_fn)
    ev = _drawer("30d", "Brand - UK", key="1")
    companies = [r["run_date"] for r in ev["recent_leads"]]
    assert "2020-01-01" not in companies              # backfilled contact excluded
    assert len(ev["recent_leads"]) == 1


def test_keyword_and_waste_snapshots_not_summed():
    kw = _region(SERVER, "# ── Keywords preview — LATEST snapshot per keyword",
                 "# ── Waste terms preview")
    assert "DISTINCT ON (keyword, match_type)" in kw
    assert "run_date DESC" in kw
    assert "SUM(spend_usd)" not in kw and "SUM(clicks)" not in kw
    waste = _region(SERVER, "# ── Waste terms preview — LATEST snapshot per term",
                    "except Exception")
    assert "DISTINCT ON (search_term, junk_category, matched_pattern)" in waste
    assert "SUM(spend_usd)" not in waste


def test_stable_key_wrong_key_returns_not_found_no_name_fallback(monkeypatch):
    # Two campaigns share the display name "Gulf"; a wrong key must NOT fall back
    # to a same-named row.
    spend = _spend([_spend_row("Gulf", 100.0, 120.0, cid="201"),
                    _spend_row("Gulf", 200.0, 240.0, cid="202")],
                   total_native=300.0, total_usd=360.0)
    _patch(monkeypatch, spend, _leads([]), _identity([]))
    from services.campaign_evidence_service import build_campaign_evidence_row
    got = build_campaign_evidence_row("30d", "Gulf", campaign_key="999")
    assert got.get("_not_found") is True
    # Correct key resolves to exactly that id.
    ok = build_campaign_evidence_row("30d", "Gulf", campaign_key="202")
    assert ok.get("campaign_id") == "202"


def test_help_content_has_no_verdict_instructions():
    help_content = _region(APP_JS, "const PAGE_HELP_CONTENT = {", "\n};")
    help_block = _region(help_content, "campaigns: {", "  \"search-terms\": {")
    for v in ("SCALE", "HOLD", "FIX", "CUT"):
        assert v not in help_block, f"stale verdict term {v} in campaigns help"
    assert "Outcome Status" in help_block
    assert "campaign-identity" in help_block or "Mapping Review" in help_block


def test_no_windsor_label_in_campaign_evidence():
    # The drawer data_sources + keyword note must not label keyword evidence "Windsor".
    ds = _region(SERVER, '"data_sources": {', "},",)
    assert "Windsor" not in ds
    assert "latest snapshot" in ds.lower()
    # The frontend keyword note is honest about the latest-snapshot source.
    assert "Latest keyword snapshot" in APP_JS


def test_drawer_data_sources_identify_canonical_headline():
    ds = _region(SERVER, '"data_sources": {', "},")
    assert "Canonical daily Google Ads spend" in ds
    assert "HubSpot" in ds and "contact_created_at" in ds
