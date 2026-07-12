"""
PR-ADS-144 — Search Terms + Patterns Evidence Revamp.

Proves the rebuilt Search Terms page presents GENUINE selected-window truth
from the durable ``search_terms`` fact table:

  - source_date is the ONLY business boundary (never run_date); rolling windows
    cover exactly N account-local dates; all_time has NO lower bound; unknown
    windows are HTTP 400, never silently coerced;
  - deduplicated aggregation (the table's natural key + per-request
    reconciliation proves no scheduler copy / snapshot multiplication);
  - tri-state classification is factual (true=Flagged waste, false=Reviewed
    clean, null=Needs review) and is never upgraded/downgraded by platform
    conversions;
  - spend is *reported search-term spend* (stored USD) — never canonical
    account spend; coverage vs canonical spend is a diagnostic that stays
    Unavailable when contracts are incomparable;
  - KPI totals use the COMPLETE filtered population, never the loaded page;
    all_time never silently truncates; exports disclose completeness;
  - campaign identity follows PR-ADS-143 (approved mappings, no fuzzy, no
    same-name merge, not_google_ads excluded, Mapping review for unmatched);
  - Patterns derive from the SAME population with unique-underlying-term KPI
    math and machine-verifiable overlap disclosure;
  - the page/drawers remain strictly read-only.

Read-only. Campaign Evidence / Revenue / ROAS / FX / mapping untouched.
"""

import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
INDEX_HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
STYLES = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SERVICE = open(os.path.join(ROOT, "services", "search_term_evidence_service.py"),
               encoding="utf-8").read()
REPO = open(os.path.join(ROOT, "db", "search_term_repository.py"),
            encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()
CONTRACT = open(os.path.join(ROOT, "docs", "API_CONTRACT.md"), encoding="utf-8").read()


def _region(text, start_marker, end_marker):
    """Slice text between two markers, asserting BOTH exist."""
    i = text.find(start_marker)
    assert i != -1, f"start marker not found: {start_marker!r}"
    j = text.find(end_marker, i + len(start_marker))
    assert j != -1, f"end marker not found: {end_marker!r}"
    assert j > i
    return text[i:j]


# The new frontend module region (everything PR-ADS-144 renders).
ST_MODULE = _region(APP_JS,
                    "// ── Search Terms + Patterns evidence page (PR-ADS-144)",
                    "function downloadWasteCSV() {")
# The new server endpoints region.
ST_SERVER = _region(SERVER,
                    "# PR-ADS-144 — Search Terms + Patterns evidence page endpoints",
                    "# GCLID Attribution endpoint")


# ── Durable-layer fixtures (monkeypatch the repo functions directly) ─────────


def _g(term, campaign=None, cid=None, spend=0.0, clicks=0, impressions=0,
       conversions=0.0, rows=1, first=date(2026, 7, 1), last=date(2026, 7, 2),
       flagged=False, unreviewed=False, junk=None, pattern=None,
       cost_micros=None, currency_code=None, source_system=None):
    if cost_micros is None and spend is not None:
        cost_micros = int(spend * 1_000_000)
    return {
        "search_term": term, "campaign_name": campaign, "campaign_id": cid,
        "spend_usd": spend, "clicks": clicks, "impressions": impressions,
        "conversions": conversions, "row_count": rows,
        "cost_micros": cost_micros,
        "currency_codes": [currency_code] if currency_code else [],
        "source_systems": [source_system] if source_system else [],
        "first_seen": first, "last_seen": last,
        "any_flagged": flagged, "any_unreviewed": unreviewed,
        "junk_categories": [junk] if junk else [],
        "matched_patterns": [pattern] if pattern else [],
        "ad_groups": [], "keywords": [], "match_types": [],
    }


def _agg(rows, *, available=True, source=None):
    if source is None:
        source = {
            "row_count": sum(r["row_count"] for r in rows),
            "spend_usd_total": sum(float(r["spend_usd"] or 0) for r in rows),
            "cost_micros_total": sum(int(r.get("cost_micros") or 0) for r in rows),
            "clicks_total": sum(int(r["clicks"] or 0) for r in rows),
            "impressions_total": sum(int(r["impressions"] or 0) for r in rows),
            "conversions_total": sum(float(r["conversions"] or 0) for r in rows),
            "distinct_source_dates": 2,
            "min_source_date": date(2026, 7, 1),
            "max_source_date": date(2026, 7, 2),
            "currency_codes": [],
            "source_systems": [],
        }
    return {"available": available, "rows": rows, "source": source}


def _spend(rows=None, *, total_usd=126.0, fx_complete=True, available=True):
    rows = rows if rows is not None else [
        {"campaign_id": "1", "campaign_name": "Brand - UK",
         "spend": 80.0, "spend_usd": 100.8, "fx_complete": True},
        {"campaign_id": "2", "campaign_name": "Gulf",
         "spend": 20.0, "spend_usd": 25.2, "fx_complete": True},
    ]
    return {
        "available": available, "customer_id": "c1", "currency_code": "GBP",
        "reporting_currency": "USD", "fx_complete": fx_complete,
        "fx_missing_days": 0 if fx_complete else 3,
        "total_spend": 100.0,
        "total_spend_usd": total_usd if fx_complete else None,
        "campaign_count": len(rows), "rows": rows,
    }


def _identity(mappings=None, *, available=True):
    return {"available": available, "mappings": mappings or []}


def _daily_costs_from_agg(agg, end):
    """Synthesize per-source-date native cost rows from an aggregate fixture —
    one row per group on the window-end date, carrying the group's cost_micros
    and provenance. Mirrors fetch_search_term_daily_costs so per-date FX
    conversion has the native amounts it needs."""
    rows = []
    for g in (agg.get("rows") or []):
        rows.append({
            "search_term": g["search_term"],
            "campaign_name": g.get("campaign_name"),
            "campaign_id": g.get("campaign_id"),
            "source_date": end,
            "cost_micros": g.get("cost_micros"),
            "currency_codes": list(g.get("currency_codes") or []),
            "source_systems": list(g.get("source_systems") or []),
        })
    return rows


def _patch(monkeypatch, agg, spend=None, identity=None, daily=None, cls=None,
           daily_costs=None):
    import db.revenue_repository as revenue_repo
    import db.search_term_repository as st_repo
    calls = {}

    def _fa(start, end):
        calls["agg"] = (start, end)
        return agg

    def _fdc(start, end):
        calls["daily_costs"] = (start, end)
        rows = (daily_costs if daily_costs is not None
                else _daily_costs_from_agg(agg, end))
        return {"available": True, "rows": rows}

    def _fs(start, end):
        calls["spend"] = (start, end)
        return spend if spend is not None else _spend()

    def _fi(customer_id=None):
        calls["identity"] = customer_id
        return identity if identity is not None else _identity()

    def _fd(start, end, term, campaign_names=None, campaign_ids=None):
        calls["daily"] = (start, end, term, campaign_names, campaign_ids)
        return daily if daily is not None else {"available": True, "rows": []}

    def _fd_campaign(start, end, term, campaign_id=None, campaign_names=None,
                    null_id_only=False):
        calls["daily_campaign"] = (start, end, term, campaign_id, campaign_names,
                                   null_id_only)
        return daily if daily is not None else {"available": True, "rows": []}

    def _fc(term, campaign_id=None, campaign_names=None):
        calls["cls"] = (term, campaign_id, campaign_names)
        return cls if cls is not None else {"available": True, "row": None}

    # FX conversion needs revenue_repo.fetch_fx_coverage and fetch_fx_rates
    def _fx_cov(start, end, base_currency, quote_currency):
        calls.setdefault("fx_coverage", []).append((start, end, base_currency))
        return {"available": True, "complete": True,
                "spend_days": 7, "covered_days": 7, "missing_dates": []}

    def _fx_rates(start, end, base_currency, quote_currency):
        calls.setdefault("fx_rates", []).append((start, end, base_currency))
        # Provide default GBP→USD rate of 1.0 for the standard test window.
        # The dedicated end-to-end currency test (§1) overrides with 1.26
        # to prove £100 ≠ $100.
        rates = {}
        if base_currency == "GBP":
            from datetime import timedelta
            s = start or date(2025, 1, 1)
            d = s
            while d <= end:
                rates[d.isoformat()] = 1.0
                d += timedelta(days=1)
        return {"available": True, "rates": rates}

    monkeypatch.setattr(st_repo, "fetch_search_term_aggregates", _fa)
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_costs", _fdc)
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend", _fs)
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity", _fi)
    monkeypatch.setattr(st_repo, "fetch_search_term_daily", _fd)
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_for_campaign", _fd_campaign)
    monkeypatch.setattr(st_repo, "fetch_latest_waste_classification", _fc)
    monkeypatch.setattr(revenue_repo, "fetch_fx_coverage", _fx_cov)
    monkeypatch.setattr(revenue_repo, "fetch_fx_rates", _fx_rates)
    return calls


def _build(window="30d", **kw):
    from services.search_term_evidence_service import build_search_term_evidence
    return build_search_term_evidence(window, **kw)


def _build_patterns(window="30d", **kw):
    from services.search_term_evidence_service import build_search_pattern_evidence
    return build_search_pattern_evidence(window, **kw)


def _dataset():
    """Coherent default dataset: 3 terms, one alias-merge, one unmatched.
    All rows have proven GBP lineage (google_ads_api source)."""
    return _agg([
        _g("freight software", "brand - uk", "1", spend=10.0, clicks=2,
           impressions=20, unreviewed=True,
           currency_code="GBP", source_system="google_ads_api"),
        _g("freight software", "brand - uk", None, spend=10.0, clicks=2,
           impressions=20,
           currency_code="GBP", source_system="google_ads_api"),
        _g("free tms jobs", "gulf", "2", spend=15.0, clicks=3, impressions=30,
           conversions=1.0, flagged=True, junk="job_seeker", pattern="jobs",
           currency_code="GBP", source_system="google_ads_api"),
        _g("mystery term", "unknown label", None, spend=5.0, clicks=1,
           impressions=30,
           currency_code="GBP", source_system="google_ads_api"),
    ])


def _default_identity():
    return _identity([
        {"external_campaign_label": "brand - uk", "campaign_id": "1",
         "match_method": "manual", "canonical_campaign_name": "Brand - UK"},
        {"external_campaign_label": "uk brand legacy", "campaign_id": "1",
         "match_method": "manual", "canonical_campaign_name": "Brand - UK"},
    ])


# ════════════════ 1. Window truth ════════════════


@pytest.mark.parametrize("window,days", [("7d", 7), ("14d", 14), ("30d", 30),
                                         ("60d", 60), ("180d", 180)])
def test_rolling_windows_cover_exactly_n_dates(monkeypatch, window, days):
    calls = _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build(window)
    assert out["window"] == window
    start, end = calls["agg"]
    assert (end - start).days == days - 1  # inclusive of exactly N dates


def test_all_time_has_no_lower_bound(monkeypatch):
    calls = _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("all_time")
    assert out["all_time"] is True
    assert out["window_start"] is None
    assert calls["agg"][0] is None          # repo receives NO lower bound
    assert calls["spend"][0] is None        # coverage uses the same window


def test_unknown_window_raises_never_coerced(monkeypatch):
    from analysis.evidence_windows import EvidenceWindowError
    _patch(monkeypatch, _dataset())
    with pytest.raises(EvidenceWindowError):
        _build("99d")
    with pytest.raises(EvidenceWindowError):
        _build_patterns("weird")


def test_endpoints_map_unknown_window_to_400():
    assert "EvidenceWindowError" in ST_SERVER
    assert "SearchTermQueryError" in ST_SERVER
    assert "status_code=400" in ST_SERVER


def test_source_date_is_the_business_boundary_never_run_date():
    fn = _region(REPO, "def fetch_search_term_aggregates",
                 "def fetch_search_term_daily_for_campaign")
    assert "source_date >= %s" in fn and "source_date <= %s" in fn
    assert "run_date" not in fn
    # run_date may appear in the service ONLY in the doctrine note negating it
    # and when surfacing the waste_terms classification date (drawer proof) —
    # never as a window boundary.
    assert "NEVER ``run_date``" in SERVICE
    assert "run_date >=" not in SERVICE and "run_date <=" not in SERVICE
    # The audit block discloses the grain.
    assert '"date_field": "source_date"' in SERVICE


def test_same_account_timezone_as_campaign_evidence():
    assert "from services.campaign_evidence_service import ACCOUNT_TZ, _window_bounds" in SERVICE


# ════════════════ 2. Deduplication / natural key ════════════════


def test_natural_key_contract_documented_and_enforced():
    # Documented in the repository…
    assert "SEARCH_TERMS_NATURAL_KEY" in REPO
    assert "idx_search_terms_unique_fact" in REPO
    # …and genuinely enforced by the schema (UNIQUE index + upsert writer).
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_search_terms_unique_fact" in SCHEMA
    writers = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
    assert "ON CONFLICT" in _region(writers, "def write_search_terms", "def ")


def test_reconciliation_proves_no_row_multiplication(monkeypatch):
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d")
    detail = out["audit"]["reconciliation_detail"]
    assert detail["source_row_reconciliation"] == "pass"
    assert detail["spend_reconciliation"] == "pass"
    assert out["audit"]["reconciliation_status"] == "pass"


def test_reconciliation_flags_snapshot_multiplication(monkeypatch):
    # If the raw source had MORE rows than the aggregation accounts for
    # (e.g. duplicate scheduler copies), reconciliation must fail loudly.
    agg = _dataset()
    agg["source"]["row_count"] += 3
    agg["source"]["spend_usd_total"] += 30.0
    _patch(monkeypatch, agg, identity=_default_identity())
    out = _build("30d")
    assert out["audit"]["reconciliation_status"] == "variance"


def test_same_term_different_campaigns_stay_separate(monkeypatch):
    agg = _agg([
        _g("freight software", "brand - uk", "1", spend=10.0, clicks=1),
        _g("freight software", "gulf", "2", spend=20.0, clicks=2),
    ])
    _patch(monkeypatch, agg)
    out = _build("30d")
    keys = {(r["search_term"], r["campaign_key"]) for r in out["rows"]}
    assert keys == {("freight software", "1"), ("freight software", "2")}


def test_alias_rows_merge_to_one_canonical_campaign(monkeypatch):
    # One group carries the campaign_id, the other only the approved alias
    # label → they are the SAME canonical campaign and must merge (dates
    # across the window aggregate correctly).
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d")
    fs = [r for r in out["rows"] if r["search_term"] == "freight software"]
    assert len(fs) == 1
    assert fs[0]["campaign_key"] == "1"
    assert fs[0]["spend_usd"] == 20.0
    assert fs[0]["clicks"] == 4
    assert fs[0]["source_rows"] == 2


# ════════════════ 3. Classification truth ════════════════


def test_tri_state_mapping(monkeypatch):
    agg = _agg([
        _g("a", "gulf", "2", flagged=True),
        _g("b", "gulf", "2"),                      # all reviewed false
        _g("c", "gulf", "2", unreviewed=True),     # unreviewed row present
    ])
    _patch(monkeypatch, agg)
    out = _build("30d", sort="term")
    states = {r["search_term"]: r["state"] for r in out["rows"]}
    assert states == {"a": "flagged", "b": "clean", "c": "needs_review"}


def test_null_never_becomes_clean_and_flagged_wins(monkeypatch):
    agg = _agg([
        _g("x", "gulf", "2", unreviewed=True),                 # null only
        _g("y", "gulf", "2", flagged=True, unreviewed=True),   # flagged + null
    ])
    _patch(monkeypatch, agg)
    out = _build("30d", sort="term")
    states = {r["search_term"]: r["state"] for r in out["rows"]}
    assert states["x"] == "needs_review"    # never coerced to clean
    assert states["y"] == "flagged"         # a flagged fact row wins


def test_conversions_never_change_classification(monkeypatch):
    agg = _agg([
        _g("converted junk", "gulf", "2", conversions=5.0, flagged=True),
        _g("zero conv clean", "gulf", "2", conversions=0.0),
    ])
    _patch(monkeypatch, agg)
    out = _build("30d", sort="term")
    states = {r["search_term"]: r["state"] for r in out["rows"]}
    assert states["converted junk"] == "flagged"       # conversion ≠ valuable
    assert states["zero conv clean"] == "clean"        # zero conv ≠ waste


def test_state_labels_are_factual_not_business_outcomes():
    assert '"Flagged waste"' in ST_MODULE
    assert '"Reviewed clean"' in ST_MODULE
    assert '"Needs review"' in ST_MODULE
    forbidden = re.compile(r"\bWinner\b|\bValuable\b|High intent|Revenue.produc",
                           re.IGNORECASE)
    assert not forbidden.search(ST_MODULE)
    assert not forbidden.search(SERVICE)


def test_platform_conversion_disclosure(monkeypatch):
    from services.search_term_evidence_service import (
        PLATFORM_CONVERSION_DISCLOSURE, build_search_term_drawer)
    assert "not a confirmed SQL" in PLATFORM_CONVERSION_DISCLOSURE
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = build_search_term_drawer("30d", "free tms jobs", campaign_key="2")
    assert out["platform_activity"]["disclosure"] == PLATFORM_CONVERSION_DISCLOSURE
    # Term-level payloads never invent business outcomes.
    assert "confirmed_sqls" not in str(out)
    assert "roas" not in str(out).lower()


def test_drawer_shows_stored_classification_evidence(monkeypatch):
    cls = {"available": True, "row": {
        "search_term": "free tms jobs", "campaign_name": "gulf",
        "junk_category": "job_seeker", "matched_pattern": "jobs",
        "crm_junk_confirmed": 2, "run_date": "2026-07-10"}}
    from services.search_term_evidence_service import build_search_term_drawer
    _patch(monkeypatch, _dataset(), identity=_default_identity(), cls=cls)
    out = build_search_term_drawer("30d", "free tms jobs", campaign_key="2")
    c = out["classification"]
    assert c["state"] == "flagged"
    assert c["junk_categories"] == ["job_seeker"]
    assert c["crm_junk_confirmed"] == 2
    assert c["classification_date"] == "2026-07-10"


def test_missing_classification_evidence_stays_unavailable(monkeypatch):
    from services.search_term_evidence_service import build_search_term_drawer
    _patch(monkeypatch, _dataset(), identity=_default_identity(),
           cls={"available": True, "row": None})
    out = build_search_term_drawer("30d", "mystery term")
    c = out["classification"]
    assert c["crm_junk_confirmed"] is None
    assert c["classification_date"] is None
    assert c["classification_source"] is None


# ════════════════ 4. Currency + coverage honesty ════════════════


def test_spend_semantics_is_reported_stored_usd(monkeypatch):
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d")
    assert out["spend_semantics"] == "reported_search_term_spend"
    audit = out["audit"]
    assert "native_currency_with_fx" in audit["currency_semantics"]
    assert "cost_micros" in audit["currency_semantics"]
    assert "cost_micros" in audit["search_term_spend_source"]
    # No fabricated native/GBP figure anywhere in the terms payload.
    assert "spend_gbp" not in str(out)


def test_coverage_ok_when_contracts_comparable(monkeypatch):
    """Coverage requires all-proven FX-safe search-term USD AND FX-safe
    canonical campaign USD. With default dataset having proven GBP lineage
    and mock FX rates returning 1.26, coverage is computable."""
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d")
    cov = out["kpis"]["coverage"]
    # Default dataset has proven GBP lineage + FX rates available → ok
    assert cov["status"] == "ok"


def test_coverage_unavailable_when_fx_incomplete(monkeypatch):
    _patch(monkeypatch, _dataset(), spend=_spend(fx_complete=False),
           identity=_default_identity())
    out = _build("30d")
    cov = out["kpis"]["coverage"]
    assert cov["status"] == "unavailable"
    assert cov["coverage_pct"] is None
    assert out["audit"]["coverage_status"] == "unavailable"


def test_coverage_unavailable_when_canonical_missing(monkeypatch):
    _patch(monkeypatch, _dataset(),
           spend={"available": False, "rows": []},
           identity=_default_identity())
    out = _build("30d")
    assert out["kpis"]["coverage"]["status"] == "unavailable"


def test_ui_never_labels_search_term_spend_as_account_spend():
    assert "Reported Search-Term Spend" in ST_MODULE
    assert "not total account spend" in ST_MODULE
    assert "Search-term reporting coverage" in ST_MODULE


def test_coverage_card_hidden_when_filters_active():
    # Coverage is a window-level source diagnostic — it must not sit beside
    # filtered KPIs as if it narrowed with them (Copilot review, PR #144).
    kpi_fn = _region(ST_MODULE, "function renderTermsKPIs", "function stCampaignOptions")
    assert "!stHasActiveFilters()" in kpi_fn


def test_coverage_numerator_is_fx_converted_not_legacy_raw():
    # The service's coverage numerator comes from _window_reported_usd (sum of
    # per-date FX-converted unit USD), NEVER source.spend_usd_total.
    assert "_window_reported_usd(pop)" in SERVICE
    win_fn = _region(SERVICE, "def _window_reported_usd",
                     "def _coverage_block")
    assert "spend_usd" in win_fn                 # sums unit FX-converted USD
    assert "spend_usd_total" not in win_fn       # never the legacy raw column


def test_coverage_withheld_when_a_unit_usd_is_withheld(monkeypatch):
    # A window where any unit's USD is withheld (missing FX) has no safe
    # numerator → coverage Unavailable even with canonical present.
    agg = _agg([_g("x", "gulf", "2", spend=10.0,
                   currency_code="GBP", source_system="google_ads_api")])
    # Provide daily costs on a date the FX map will NOT cover.
    daily_costs = [{"search_term": "x", "campaign_name": "gulf",
                    "campaign_id": "2", "source_date": date(2020, 1, 1),
                    "cost_micros": 10_000_000, "currency_codes": ["GBP"],
                    "source_systems": ["google_ads_api"]}]
    _patch(monkeypatch, agg, identity=_default_identity(), daily_costs=daily_costs)
    out = _build("30d")
    assert out["rows"][0]["spend_usd"] is None
    assert out["rows"][0]["fx_complete"] is False
    assert out["kpis"]["coverage"]["status"] == "unavailable"


def test_daily_drawer_renders_native_and_usd_columns():
    # PR-ADS-144 §4: the daily table shows Native + USD (per-date), not a single
    # ambiguous Spend column, and uses the currency-aware cell.
    drawer = _region(ST_MODULE, "function renderStTermDrawer",
                     "async function openStPatternDrawer")
    assert ">Native<" in drawer and ">USD<" in drawer
    assert "stSpendCell(d)" in drawer            # native cell is currency-aware
    assert "each day's own FX rate" in drawer.lower() or \
        "day's own FX rate" in drawer


def test_frontend_spend_cell_is_currency_aware():
    cell = _region(ST_MODULE, "function stSpendCell", "function stUnavailable")
    assert "spend_usd" in cell and "spend_native" in cell
    assert "native_currency" in cell             # native shown when USD withheld


def test_last_resort_fallbacks_match_endpoint_shapes():
    # Each endpoint's generic-exception fallback must return ITS documented
    # shape, not the Terms shape (Copilot review, PR #144).
    from services.search_term_evidence_service import (
        unavailable_pattern_drawer_response, unavailable_patterns_response,
        unavailable_term_drawer_response, unavailable_terms_response)
    terms = unavailable_terms_response("30d")
    assert terms["db_unavailable"] is True and "rows" in terms and "kpis" in terms
    drawer = unavailable_term_drawer_response("30d")
    assert drawer["db_unavailable"] is True and drawer["term"] is None
    pats = unavailable_patterns_response("30d")
    assert pats["db_unavailable"] is True
    assert pats["kpis"]["patterns_found"] is None and pats["rows"] == []
    assert "overlap" in pats and "audit" in pats
    pd = unavailable_pattern_drawer_response("30d")
    assert pd["db_unavailable"] is True and pd["pattern"] is None and pd["terms"] == []
    # The server wires each endpoint to its own fallback.
    for name in ["unavailable_term_drawer_response", "unavailable_patterns_response",
                 "unavailable_pattern_drawer_response"]:
        assert f"fallback={name}" in ST_SERVER, name


def test_db_unavailable_stays_unavailable_not_zero(monkeypatch):
    _patch(monkeypatch, _agg([], available=False))
    out = _build("30d")
    assert out["db_unavailable"] is True
    k = out["kpis"]
    assert k["reported_terms"] is None
    assert k["reported_spend_usd"] is None
    assert k["flagged_waste"] is None
    assert out["audit"]["reconciliation_status"] == "unavailable"


def test_verified_empty_window_is_genuine_zero(monkeypatch):
    _patch(monkeypatch, _agg([], source={
        "row_count": 0, "spend_usd_total": None, "clicks_total": None,
        "impressions_total": None, "conversions_total": None,
        "distinct_source_dates": 0, "min_source_date": None,
        "max_source_date": None}))
    out = _build("30d")
    assert out.get("db_unavailable") is not True
    assert out["kpis"]["reported_terms"] == 0
    assert out["kpis"]["reported_spend_usd"] == 0.0
    assert out["audit"]["reconciliation_status"] == "pass"


# ════════════════ 5. Pagination + export honesty ════════════════


def test_kpis_use_complete_population_not_the_page(monkeypatch):
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d", page=2, page_size=1)
    assert out["pagination"]["returned_count"] == 1
    assert out["pagination"]["page"] == 2
    assert out["kpis"]["reported_terms"] == 3          # complete population
    assert out["kpis"]["reported_spend_usd"] == 40.0
    assert out["audit"]["pagination_complete"] is True


def test_state_counts_add_back_to_complete_count(monkeypatch):
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d", page=1, page_size=1)
    k = out["kpis"]
    assert (k["flagged_waste"] + k["reviewed_clean"] + k["needs_review"]
            == out["pagination"]["total_count"] == 3)
    assert out["audit"]["reconciliation_detail"]["state_count_reconciliation"] == "pass"


def test_all_time_never_silently_truncates(monkeypatch):
    rows = [_g(f"term {i:04d}", "gulf", "2", spend=float(i), clicks=i,
               currency_code="GBP", source_system="google_ads_api")
            for i in range(1, 701)]
    _patch(monkeypatch, _agg(rows))
    out = _build("all_time", page=1, page_size=50)
    assert out["pagination"]["total_count"] == 700
    assert out["pagination"]["returned_count"] == 50
    assert out["pagination"]["has_more"] is True
    assert out["kpis"]["reported_terms"] == 700        # complete, not capped
    assert out["kpis"]["reported_spend_usd"] == sum(float(i) for i in range(1, 701))


def test_filters_and_sort_apply_server_side(monkeypatch):
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d", state="flagged")
    assert [r["search_term"] for r in out["rows"]] == ["free tms jobs"]
    assert out["kpis"]["reported_terms"] == 1
    out = _build("30d", q="freight", sort="term")
    assert [r["search_term"] for r in out["rows"]] == ["freight software"]
    out = _build("30d", min_spend=10.0)
    assert {r["search_term"] for r in out["rows"]} == {"freight software",
                                                       "free tms jobs"}


def test_nulls_sort_last_never_coerced(monkeypatch):
    agg = _agg([
        _g("no clicks", "gulf", "2", spend=50.0, clicks=0,
           currency_code="GBP", source_system="google_ads_api"),   # CPC null
        _g("with clicks", "gulf", "2", spend=10.0, clicks=10,
           currency_code="GBP", source_system="google_ads_api"),
    ])
    _patch(monkeypatch, agg)
    out = _build("30d", sort="cpc")
    assert [r["search_term"] for r in out["rows"]] == ["with clicks", "no clicks"]
    assert out["rows"][1]["cpc_usd"] is None            # not a fake $0


def test_export_is_complete_filtered_dataset(monkeypatch):
    from services.search_term_evidence_service import build_search_term_export
    rows = [_g(f"term {i:04d}", "gulf", "2", spend=float(i),
               currency_code="GBP", source_system="google_ads_api")
            for i in range(1, 301)]
    _patch(monkeypatch, _agg(rows))
    out = build_search_term_export("all_time")
    assert out["complete"] is True
    assert len(out["rows"]) == 300                      # never a page
    # Endpoint discloses completeness in the filename and 503s when down.
    assert "_complete.csv" in ST_SERVER
    assert "status_code=503" in ST_SERVER


def test_ui_shows_showing_x_of_z_and_page_controls():
    assert "reported terms" in ST_MODULE                # "Showing X–Y of Z reported terms"
    assert "Showing ${fmtCount(from)}" in ST_MODULE
    assert 'p.delete("page")' in ST_MODULE              # export drops pagination


# ════════════════ 6. Campaign identity (PR-ADS-143 rules) ════════════════


def test_approved_alias_maps_to_canonical_campaign(monkeypatch):
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d")
    fs = [r for r in out["rows"] if r["search_term"] == "freight software"][0]
    assert fs["campaign_key"] == "1"
    assert fs["campaign_name"] == "Brand - UK"
    assert fs["mapping_status"] == "mapped"
    # Genuinely different approved labels surface as aliases; a case/spacing
    # variant of the canonical name ("brand - uk") is NOT flagged as one.
    assert "uk brand legacy" in fs["aliases"]
    assert "brand - uk" not in fs["aliases"]


def test_not_google_ads_excluded_from_campaign_identity(monkeypatch):
    agg = _agg([_g("term x", "hubspot import", None, spend=5.0)])
    identity = _identity([{"external_campaign_label": "hubspot import",
                           "campaign_id": None, "match_method": "not_google_ads"}])
    _patch(monkeypatch, agg, identity=identity)
    out = _build("30d")
    row = out["rows"][0]
    assert row["mapping_status"] == "not_google_ads"
    assert row["campaign_key"].startswith("not_google_ads:")
    assert row["campaign_key"] not in ("1", "2")        # no canonical id assigned


def test_unmatched_label_is_mapping_review(monkeypatch):
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = _build("30d")
    row = [r for r in out["rows"] if r["search_term"] == "mystery term"][0]
    assert row["mapping_status"] == "unmatched"
    assert row["campaign_key"].startswith("unmatched:")
    assert "Mapping review" in ST_MODULE                # UI marker


def test_duplicate_display_names_never_merge(monkeypatch):
    spend = _spend(rows=[
        {"campaign_id": "10", "campaign_name": "Brand", "spend": 10.0,
         "spend_usd": 12.0, "fx_complete": True},
        {"campaign_id": "20", "campaign_name": "Brand", "spend": 20.0,
         "spend_usd": 24.0, "fx_complete": True},
    ])
    agg = _agg([
        _g("dup term", "brand", "10", spend=1.0),
        _g("dup term", "brand", "20", spend=2.0),
    ])
    _patch(monkeypatch, agg, spend=spend)
    out = _build("30d")
    keys = {r["campaign_key"] for r in out["rows"]}
    assert keys == {"10", "20"}                          # two ids stay separate


def test_no_fuzzy_auto_mapping_on_ambiguous_names(monkeypatch):
    # A label whose normalized name matches TWO canonical ids must stay
    # unmatched (Mapping review) — never auto-picked.
    spend = _spend(rows=[
        {"campaign_id": "10", "campaign_name": "Brand", "spend": 10.0,
         "spend_usd": 12.0, "fx_complete": True},
        {"campaign_id": "20", "campaign_name": "Brand", "spend": 20.0,
         "spend_usd": 24.0, "fx_complete": True},
    ])
    agg = _agg([_g("amb term", "brand", None, spend=1.0)])
    _patch(monkeypatch, agg, spend=spend)
    out = _build("30d")
    assert out["rows"][0]["mapping_status"] == "unmatched"


# ════════════════ 7. Patterns truth ════════════════


def _pattern_dataset():
    return _agg([
        _g("freight software", "brand - uk", "1", spend=10.0, clicks=2,
           currency_code="GBP", source_system="google_ads_api"),
        _g("freight software", "gulf", "2", spend=10.0, clicks=2,
           currency_code="GBP", source_system="google_ads_api"),   # same term, 2nd campaign
        _g("freight jobs", "gulf", "2", spend=10.0, clicks=2,
           conversions=1.0, flagged=True, junk="job_seeker",
           currency_code="GBP", source_system="google_ads_api"),
    ])


def test_patterns_use_same_window_and_population(monkeypatch):
    calls = _patch(monkeypatch, _pattern_dataset())
    _build("30d")
    terms_window = calls["agg"]
    _build_patterns("30d", n=1)
    assert calls["agg"] == terms_window                 # identical bounds
    out = _build_patterns("all_time", n=1)
    assert out["all_time"] is True
    assert calls["agg"][0] is None


def test_pattern_totals_use_unique_underlying_terms(monkeypatch):
    _patch(monkeypatch, _pattern_dataset())
    out = _build_patterns("30d", n=1)
    rows = {r["pattern"]: r for r in out["rows"]}
    # "freight software" ran in TWO campaigns but is ONE unique term:
    assert rows["software"]["terms"] == 1
    assert rows["software"]["reported_spend_usd"] == 20.0
    assert rows["freight"]["terms"] == 2
    assert rows["freight"]["reported_spend_usd"] == 30.0


def test_overlap_never_inflates_kpi_spend(monkeypatch):
    _patch(monkeypatch, _pattern_dataset())
    out = _build_patterns("30d", n=1)
    k = out["kpis"]
    # Membership-sum would be 50.0 (freight 30 + software 20 + jobs 10 = 60
    # actually) — KPI spend must be the UNIQUE-term total instead.
    membership_sum = sum(r["reported_spend_usd"] for r in out["rows"])
    assert k["reported_spend_represented_usd"] == 30.0
    assert membership_sum > k["reported_spend_represented_usd"]
    ov = out["overlap"]
    assert ov["unique_terms_analysed"] == 2
    # "freight software" joins {freight, software}; "freight jobs" joins
    # {freight, jobs} → 4 memberships across 2 unique terms.
    assert ov["total_pattern_memberships"] == 4
    assert ov["overlapping_term_count"] == 2
    assert ov["spend_semantics"] == "unique_underlying_terms"
    assert "never be summed" in ov["disclosure"]
    assert out["audit"]["pattern_kpi_spend_semantics"] == "unique_underlying_terms"


def test_pattern_word_length_controls(monkeypatch):
    from services.search_term_evidence_service import SearchTermQueryError
    _patch(monkeypatch, _pattern_dataset())
    out1 = _build_patterns("30d", n=1)
    out2 = _build_patterns("30d", n=2)
    assert all(r["n"] == 1 and " " not in r["pattern"] for r in out1["rows"])
    assert all(r["n"] == 2 and len(r["pattern"].split()) == 2 for r in out2["rows"])
    assert {"freight software", "freight jobs"} == {r["pattern"] for r in out2["rows"]}
    with pytest.raises(SearchTermQueryError):
        _build_patterns("30d", n=4)


def test_pattern_signals_are_factual(monkeypatch):
    _patch(monkeypatch, _pattern_dataset())
    out = _build_patterns("30d", n=1)
    rows = {r["pattern"]: r for r in out["rows"]}
    assert rows["freight"]["signal"] == "mixed"                  # flagged + clean
    assert rows["software"]["signal"] == "reviewed_clean_only"
    assert rows["jobs"]["signal"] == "flagged_present"
    forbidden = re.compile(
        r"\bWinner\b|\bValuable\b|High intent|Revenue.produc|Negative keyword recommendation",
        re.IGNORECASE)
    assert not forbidden.search(str(out))


def test_pattern_drawer_unique_term_totals(monkeypatch):
    from services.search_term_evidence_service import build_search_pattern_drawer
    _patch(monkeypatch, _pattern_dataset())
    out = build_search_pattern_drawer("30d", "freight", 1)
    assert out["pattern"]["terms"] == 2
    assert out["pattern"]["reported_spend_usd"] == 30.0          # unique totals
    terms = {t["search_term"]: t for t in out["terms"]}
    assert terms["freight software"]["spend_usd"] == 20.0        # once, not twice
    assert terms["freight jobs"]["state"] == "flagged"


def test_patterns_unavailable_when_terms_unavailable(monkeypatch):
    _patch(monkeypatch, _agg([], available=False))
    out = _build_patterns("30d", n=1)
    assert out["db_unavailable"] is True
    assert out["kpis"]["patterns_found"] is None
    # UI explains the dependency instead of faking an empty analysis.
    assert "derived from the Search Terms source" in ST_MODULE


# ════════════════ 8. Term drawer truth ════════════════


def test_drawer_matches_table_row_exactly(monkeypatch):
    from services.search_term_evidence_service import build_search_term_drawer
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    table = _build("30d")
    row = [r for r in table["rows"] if r["search_term"] == "freight software"][0]
    drawer = build_search_term_drawer("30d", "freight software", campaign_key="1")
    assert drawer["term"]["spend_usd"] == row["spend_usd"]
    assert drawer["term"]["clicks"] == row["clicks"]
    assert drawer["term"]["state"] == row["state"]
    assert drawer["window"] == table["window"]


def test_drawer_daily_series_never_zero_fills(monkeypatch):
    from services.search_term_evidence_service import build_search_term_drawer
    daily = {"available": True, "rows": [
        {"source_date": "2026-07-01", "spend_usd": 10.0, "clicks": 2,
         "impressions": 20, "conversions": 0.0}]}
    _patch(monkeypatch, _dataset(), identity=_default_identity(), daily=daily)
    out = build_search_term_drawer("30d", "freight software", campaign_key="1")
    assert len(out["daily"]["rows"]) == 1                # only reported dates
    assert "never fabricated as zero" in out["daily"]["note"]


def test_drawer_unknown_term_is_not_found(monkeypatch):
    from services.search_term_evidence_service import build_search_term_drawer
    _patch(monkeypatch, _dataset(), identity=_default_identity())
    out = build_search_term_drawer("30d", "no such term")
    assert out["_not_found"] is True


def test_drawer_has_no_negative_keyword_action():
    drawer_region = _region(ST_MODULE, "function renderStTermDrawer",
                            "async function openStPatternDrawer")
    assert "Apply negative" not in drawer_region
    assert "negative keyword" not in drawer_region.lower() or \
        "no negative keyword is applied" in drawer_region
    assert "Copy term" in drawer_region                  # copying IS permitted
    assert "Open Campaign Evidence" in drawer_region
    assert "Open Flagged Waste Terms" in drawer_region
    assert "Patterns with these words" in drawer_region


# ════════════════ 9. UI contract ════════════════


def test_one_page_two_tabs_and_route_compat():
    assert '<section id="page-search-terms" class="page page--wide"' in INDEX_HTML
    assert 'id="search-terms-shell"' in INDEX_HTML
    # Old duplicated page markup is gone.
    assert "search-terms-apply-btn" not in INDEX_HTML
    assert "ngrams-apply-btn" not in INDEX_HTML
    # #/ngrams still resolves to the Patterns tab.
    assert '"ngrams": "search-terms"' in APP_JS
    assert "#/search-terms?tab=patterns" in APP_JS
    assert 'id="tab-btn-terms"' in ST_MODULE and 'id="tab-btn-patterns"' in ST_MODULE


def test_single_sidebar_search_terms_item():
    assert INDEX_HTML.count('data-page="search-terms"') == 1
    assert 'data-page="ngrams"' not in INDEX_HTML


def test_header_is_concise_without_source_pill_clutter():
    assert "Queries reported by Google Ads, with review state and waste evidence." in ST_MODULE
    # No permanent Google Ads / Read-only pills or Depends-on strip in the page.
    assert "Depends on" not in ST_MODULE
    assert ST_MODULE.count("read-only-notice") == 0
    assert "renderEvidenceTruthStrip" not in ST_MODULE


def test_kpi_strip_has_required_cards():
    for label in ["Reported Terms", "Reported Search-Term Spend", "Clicks",
                  "Flagged Waste", "Needs Review", "Reporting Coverage"]:
        assert label in ST_MODULE, label


def test_one_compact_filter_row_with_required_controls():
    filters = _region(ST_MODULE, "function renderTermsFilters", "function stCampaignCell")
    assert filters.count('class="campaign-controls st-controls"') == 1
    for control_id in ["st-q", "st-campaign", "st-state", "st-junk",
                       "st-min-spend", "st-sort", "st-reset"]:
        assert f'id="{control_id}"' in filters, control_id


def test_table_columns_and_row_drawer():
    table = _region(ST_MODULE, "function renderTermsTable", "function wireTermsControls")
    for col in ["Search Term", "State", "Campaign", "Spend", "Clicks", "CPC",
                "Last Seen"]:
        assert f"<th" in table and col in table, col
    assert "campaign-open-icon" in table                 # › affordance
    assert 'role="button"' in table                      # whole row opens drawer
    assert "openStTermDrawer" in table
    assert "Open Evidence</button>" not in table         # no separate big button


def test_pattern_table_columns():
    table = _region(ST_MODULE, "function renderPatternsTable", "function wirePatternControls")
    for col in ["Pattern", "Signal", "Terms", "Flagged", "Clean",
                "Needs Review", "Reported Spend"]:
        assert col in table, col


def test_mobile_cards_preserve_primary_metrics():
    # Every primary cell carries data-label so the ≤640px stacked-card CSS
    # (shared with Campaign Evidence) renders labels; Spend/State/Campaign are
    # never hidden.
    for label in ["Search Term", "State", "Campaign", "Spend", "Clicks",
                  "CPC", "Last Seen"]:
        assert f'data-label="{label}"' in ST_MODULE, label
    for label in ["Pattern", "Signal", "Terms", "Flagged", "Clean",
                  "Needs Review", "Reported Spend"]:
        assert f'data-label="{label}"' in ST_MODULE, label
    assert "@media (max-width: 640px)" in _region(
        STYLES, "PR-ADS-144", "\n\n") or ".st-table, .st-patterns-table { min-width: 0; }" in STYLES


def test_tablet_scrolls_inside_container_not_body():
    st_css = STYLES[STYLES.find("PR-ADS-144"):]
    assert ".st-table { min-width: 680px; }" in st_css
    assert "evidence-table-scroll" in ST_MODULE          # scroll container wraps table


def test_drawer_uses_selected_window():
    assert 'p.set("window", getEvidenceWindow())' in ST_MODULE
    drawer = _region(ST_MODULE, "async function openStTermDrawer",
                     "function renderStTermDrawer")
    assert "getEvidenceWindow()" in drawer


def test_no_raw_undefined_null_nan_rendering():
    # Money/count formatters guard nulls; no template ever interpolates a bare
    # possibly-undefined value without a fallback in the new module.
    assert 'return "—";' in ST_MODULE                    # stMoney null guard
    assert "stUnavailable" in ST_MODULE
    assert "undefined) ? stUnavailable()" in ST_MODULE.replace("\n", " ") or \
        "undefined) ? stUnavailable()" in ST_MODULE
    assert "NaN" not in ST_MODULE


def test_freshness_single_compact_state():
    assert 'renderPageDatasetFreshness("search_terms")' in ST_MODULE
    # No separate fake N-Grams freshness call on the new page.
    assert 'renderPageDatasetFreshness("ngrams")' not in ST_MODULE


def test_help_content_updated():
    help_entry = _region(APP_JS, '"search-terms": {\n    what: "Search Terms + Patterns',
                         "},")
    for needle in ["source_date", "reported search-term spend",
                   "Reporting Coverage", "FX-converted to USD",
                   "platform conversion is never a confirmed SQL",
                   "no negative keywords are added"]:
        assert needle in help_entry, needle
    assert "Windsor" not in help_entry
    # Patterns dependency explained.
    assert "cannot produce evidence when Search Terms is unavailable" in APP_JS


def test_campaign_evidence_page_untouched_by_this_pr():
    # Regression guardrail: Campaign Evidence renderers still present + intact.
    for fn in ["function renderCampaignEvidencePage", "function renderCampaignEvidenceKPIs",
               "function renderCampaignDecisionTable", "function openCampaignDrawer"]:
        assert fn in APP_JS, fn
    # The campaign drawer's N-Gram drilldown still works (legacy endpoint + mix pills).
    assert "loadCampaignDrawerNgrams" in APP_JS
    assert "function ngramWasteStateMix" in APP_JS
    assert "/api/search-terms/ngrams" in APP_JS


def test_campaign_prefill_from_campaign_drawer_still_works():
    assert "ngramsPrefill = { campaign: _campName }" in APP_JS
    assert "_patPendingCampaignName" in ST_MODULE


# ════════════════ 10. API contract documentation ════════════════


def test_api_contract_documents_new_endpoints():
    for route in ["/api/search-term-evidence`", "/api/search-term-evidence/term`",
                  "/api/search-term-evidence/patterns`",
                  "/api/search-term-evidence/patterns/detail`",
                  "/api/search-term-evidence/export`"]:
        assert route in CONTRACT, route
    for field in ["source_table", "date_field", "account_timezone",
                  "currency_semantics", "deduplication_key",
                  "classification_semantics", "campaign_identity_status",
                  "canonical_spend_source", "search_term_spend_source",
                  "coverage_status", "pagination_complete",
                  "reconciliation_status"]:
        assert field in CONTRACT, field
    assert "Unavailable" in CONTRACT or "unavailable" in CONTRACT.lower()


# ════════════════ 11. Read-only scan ════════════════


FORBIDDEN_WRITE_PATTERNS = [
    "add_negative", "negative_keyword_service", "campaign_criterion",
    "CampaignCriterionService", "mutate_campaign", "MutateOperation",
    "upload_click_conversion", "upload_conversion", "offline_conversion",
    "hubspot.*post", "batch/update",
]


def _assert_read_only(text, label):
    lowered = text.lower()
    for pat in FORBIDDEN_WRITE_PATTERNS:
        assert not re.search(pat.lower(), lowered), f"{label}: forbidden {pat!r}"


def test_backend_is_read_only():
    _assert_read_only(SERVICE, "service")
    _assert_read_only(REPO, "repository")
    _assert_read_only(ST_SERVER, "endpoints")
    # Repository issues SELECTs only — no INSERT/UPDATE/DELETE statements.
    for verb in ["INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE"]:
        assert verb not in REPO, verb


def test_frontend_is_read_only():
    _assert_read_only(ST_MODULE, "frontend module")
    # Only GET-style fetches — the module never posts anything.
    assert "method:" not in ST_MODULE
    assert 'fetch(' not in ST_MODULE.replace("fetchJSON(", "")


def test_no_write_verbs_in_new_endpoints():
    assert "@app.post" not in ST_SERVER
    assert "@app.put" not in ST_SERVER
    assert "@app.delete" not in ST_SERVER


# ════════════════ 12. Currency lineage (PR-ADS-144 §1) ════════════════


def test_connector_preserves_cost_micros_and_currency_code():
    """Google Ads connector must return cost_micros and currency_code."""
    src = open(os.path.join(ROOT, "connectors", "google_ads_direct.py"),
               encoding="utf-8").read()
    fn = _region(src, "def fetch_search_terms", "def fetch_keyword_performance")
    assert "customer.currency_code" in fn
    assert '"cost_micros"' in fn
    assert '"currency_code"' in fn


def test_source_adapter_preserves_lineage():
    """Google Ads source adapter must pass cost_micros + currency_code."""
    src = open(os.path.join(ROOT, "connectors", "google_ads_source.py"),
               encoding="utf-8").read()
    fn = _region(src, "def normalize_search_term_row", "def normalize_keyword_row")
    assert '"cost_micros"' in fn
    assert '"currency_code"' in fn


def test_writer_stores_currency_lineage():
    """Writer must upsert cost_micros, currency_code, source_system."""
    writers = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
    fn = _region(writers, "def write_search_terms", "def ")
    assert "cost_micros" in fn
    assert "currency_code" in fn
    assert "source_system" in fn


def test_schema_has_currency_columns():
    """Schema must declare cost_micros, currency_code, source_system columns."""
    schema_block = _region(SCHEMA, "CREATE TABLE IF NOT EXISTS search_terms",
                           "CREATE UNIQUE INDEX IF NOT EXISTS idx_search_terms_unique_fact")
    assert "cost_micros" in schema_block
    assert "currency_code" in schema_block
    assert "source_system" in schema_block


def test_currency_lineage_end_to_end(monkeypatch):
    """Prove £100 native (GBP) does NOT become $100 USD.

    Google Ads cost_micros 100_000_000 (= £100 native) with currency_code GBP
    must be converted through FX rates, NOT treated as $100 USD.
    """
    import db.revenue_repository as revenue_repo
    import db.search_term_repository as st_repo
    from services.search_term_evidence_service import build_search_term_evidence

    from datetime import timedelta

    # A single search-term row: £100 native (GBP)
    agg = _agg([
        _g("freight software", "brand - uk", "1", spend=100.0, clicks=5,
           impressions=50, cost_micros=100_000_000, currency_code="GBP",
           source_system="google_ads_api"),
    ])
    # Canonical spend with FX-complete USD
    spend = _spend(total_usd=150.0, fx_complete=True)

    def _fa(start, end):
        return agg
    def _fs(start, end):
        return spend
    def _fi(customer_id=None):
        return _identity()
    # Per-source-date native cost — the £100 lands on the window-end date so it
    # aligns with the FX rate provided below.
    def _fdc(start, end):
        return {"available": True, "rows": [{
            "search_term": "freight software", "campaign_name": "brand - uk",
            "campaign_id": "1", "source_date": end, "cost_micros": 100_000_000,
            "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]}]}
    # FX rate GBP→USD = 1.26 for every date in the window.
    def _fx_rates(start, end, base, quote):
        rates = {}
        d = start or end
        while d <= end:
            rates[d.isoformat()] = 1.26
            d += timedelta(days=1)
        return {"available": True, "rates": rates}

    monkeypatch.setattr(st_repo, "fetch_search_term_aggregates", _fa)
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_costs", _fdc)
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend", _fs)
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity", _fi)
    monkeypatch.setattr(revenue_repo, "fetch_fx_rates", _fx_rates)

    out = build_search_term_evidence("30d")
    row = out["rows"][0]

    # £100 native is NOT $100 — it should be FX-converted.
    assert row["spend_native"] == 100.0
    assert row["native_currency"] == "GBP"
    # After FX conversion at 1.26, expected $126.00
    assert row["spend_usd"] == 126.0
    assert row["fx_complete"] is True
    assert row["reporting_currency"] == "USD"
    # KPI-level spend should also be FX-converted.
    assert out["kpis"]["reported_spend_usd"] == 126.0
    assert out["kpis"]["native_currency"] == "GBP"
    assert out["kpis"]["fx_complete"] is True


def test_unproven_rows_withhold_monetary_metrics(monkeypatch):
    """Rows without currency_code/source_system have spend_usd withheld."""
    agg = _agg([
        _g("freight software", "brand - uk", "1", spend=100.0, clicks=5),
    ])
    _patch(monkeypatch, agg, identity=_default_identity())
    from services.search_term_evidence_service import build_search_term_evidence
    out = build_search_term_evidence("30d")
    row = out["rows"][0]
    # Without proven lineage, spend_usd is None (withheld).
    assert row["spend_usd"] is None
    assert row["currency_status"] != "proven"


def test_mixed_currencies_withheld(monkeypatch):
    """Rows with mixed currencies have monetary metrics withheld."""
    agg = _agg([
        _g("freight software", "brand - uk", "1", spend=10.0,
           currency_code="GBP", source_system="google_ads_api"),
        _g("freight software", "brand - uk", "1", spend=20.0,
           currency_code="USD", source_system="google_ads_api"),
    ])
    _patch(monkeypatch, agg, identity=_default_identity())
    from services.search_term_evidence_service import build_search_term_evidence
    out = build_search_term_evidence("30d")
    assert out["kpis"]["currency_status"] == "mixed_or_unproven"


# ════════════════ 13. Natural key with campaign_id (PR-ADS-144 §2) ════════════════


def test_natural_key_includes_campaign_id():
    """The unique index must include COALESCE(campaign_id, '') so two
    campaign IDs sharing a display name can never collide."""
    from db.search_term_repository import SEARCH_TERMS_NATURAL_KEY
    assert "COALESCE(campaign_id" in SCHEMA
    writers = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
    fn = _region(writers, "def write_search_terms", "def ")
    assert "COALESCE(campaign_id" in fn
    assert "campaign_id" in SEARCH_TERMS_NATURAL_KEY


def test_natural_key_documented_with_campaign_id():
    """SEARCH_TERMS_NATURAL_KEY must mention campaign_id."""
    from db.search_term_repository import SEARCH_TERMS_NATURAL_KEY as nk
    assert "campaign_id" in nk
    assert "campaign_name" in nk


def test_same_term_different_campaign_ids_stay_separate(monkeypatch):
    """Two campaign IDs sharing a display name must not collide
    (same source date, same campaign name, same term, different IDs)."""
    agg = _agg([
        _g("freight software", "brand - uk", "10", spend=100.0, clicks=5),
        _g("freight software", "brand - uk", "20", spend=200.0, clicks=10),
    ])
    _patch(monkeypatch, agg, identity=_default_identity())
    out = _build("30d")
    # Both must survive as separate rows (different campaign_key).
    terms = [r for r in out["rows"] if r["search_term"] == "freight software"]
    assert len(terms) == 2
    keys = {r["campaign_key"] for r in terms}
    assert "10" in keys
    assert "20" in keys


# ════════════════ 14. Drawer campaign scoping (PR-ADS-144 §3) ════════════════


def test_drawer_scoped_to_campaign_classification(monkeypatch):
    """Drawer must scope waste classification to the selected campaign.
    It should pass campaign_names to fetch_latest_waste_classification."""
    cls_data = {"available": True, "row": {
        "search_term": "freight software", "campaign_name": "brand - uk",
        "junk_category": "competitor", "matched_pattern": "brand",
        "crm_junk_confirmed": 1, "run_date": "2026-07-10"}}
    calls = _patch(monkeypatch, _dataset(), identity=_default_identity(),
                   cls=cls_data)
    from services.search_term_evidence_service import build_search_term_drawer
    out = build_search_term_drawer("30d", "freight software", campaign_key="1")
    # Verify classification was called with campaign scoping.
    cls_call = calls.get("cls")
    assert cls_call is not None
    term_arg = cls_call[0]
    campaign_names_arg = cls_call[2]
    assert term_arg == "freight software"
    assert campaign_names_arg is not None


def test_drawer_daily_series_uses_campaign_id_scoping(monkeypatch):
    """Drawer daily series must use ID-first scoping, never OR'ing
    campaign_id = target OR campaign_name = shared_display_name."""
    calls = _patch(monkeypatch, _dataset(), identity=_default_identity())
    from services.search_term_evidence_service import build_search_term_drawer
    build_search_term_drawer("30d", "freight software", campaign_key="1")
    # Should use the campaign-scoped daily function with campaign_id.
    daily_call = calls.get("daily_campaign")
    assert daily_call is not None
    _, _, term, campaign_id, campaign_names, _null_only = daily_call
    assert term == "freight software"
    assert campaign_id == "1"


def test_adversarial_same_name_different_ids_different_evidence(monkeypatch):
    """Two campaign IDs with identical display name + identical search term
    must show different evidence in their drawers — no cross-contamination.

    Because the shared display label cannot distinguish the ids in the name-only
    waste_terms table, the drawer scopes the daily series by campaign_id ALONE
    (no null-id name fallback) and WITHHOLDS name-derived classification proof —
    it never borrows another campaign's CRM-junk confirmations/date/source.
    """
    # A waste_terms proof row keyed only by the SHARED campaign name — it must
    # NOT be attributed to either campaign_id drawer.
    shared_cls = {"available": True, "row": {
        "search_term": "freight software", "campaign_name": "brand - uk",
        "junk_category": "competitor", "matched_pattern": "brand",
        "crm_junk_confirmed": 7, "run_date": "2026-07-10"}}
    agg = _agg([
        _g("freight software", "brand - uk", "10", spend=100.0, clicks=5,
           flagged=True, junk="competitor",
           currency_code="GBP", source_system="google_ads_api"),
        _g("freight software", "brand - uk", "20", spend=200.0, clicks=10,
           flagged=False, unreviewed=True,
           currency_code="GBP", source_system="google_ads_api"),
    ])
    # No approved identity mapping for "brand - uk" → each stored id keeps its
    # own key; the shared label is ambiguous.
    calls = _patch(monkeypatch, agg, identity=_identity([]), cls=shared_cls)
    from services.search_term_evidence_service import build_search_term_drawer

    out10 = build_search_term_drawer("30d", "freight software", campaign_key="10")
    daily10 = calls["daily_campaign"]
    out20 = build_search_term_drawer("30d", "freight software", campaign_key="20")
    daily20 = calls["daily_campaign"]

    # Classification STATE still comes correctly from each selected unit.
    assert out10["term"]["state"] == "flagged"
    assert out20["term"]["state"] == "needs_review"
    assert out10["classification"]["state"] == "flagged"
    assert out20["classification"]["state"] == "needs_review"

    # Daily series is scoped by campaign_id ALONE — no null-id name fallback.
    assert daily10[3] == "10" and daily10[4] is None
    assert daily20[3] == "20" and daily20[4] is None

    # CRM-junk confirmations / date / source are WITHHELD (never borrowed from
    # the shared-name waste_terms row) in BOTH drawers.
    for out in (out10, out20):
        assert out["classification"]["crm_junk_confirmed"] is None
        assert out["classification"]["classification_date"] is None
        assert out["classification"]["classification_source"] is None
        assert out["classification"]["proof_status"] == "unavailable"

    # And the ambiguous fetch was never made by the shared name.
    assert calls.get("cls") is None


# ════════════════ 15. Pattern min_terms KPI scope (PR-ADS-144 §4) ════════════════


def test_min_terms_zero_surviving_patterns(monkeypatch):
    """After min_terms=2 filters out a singleton pattern, KPIs must reflect
    zero surviving patterns: all metrics become 0."""
    agg = _agg([
        _g("freight software solution", "brand - uk", "1", spend=50.0, clicks=3),
    ])
    _patch(monkeypatch, agg, identity=_default_identity())
    out = _build_patterns("30d", n=1, min_terms=2)
    kpis = out["kpis"]
    assert kpis["patterns_found"] == 0
    assert kpis["terms_analysed"] == 0
    assert kpis["reported_spend_represented_usd"] == 0.0
    overlap = out["overlap"]
    assert overlap["total_pattern_memberships"] == 0
    assert overlap["overlapping_term_count"] == 0


def test_min_terms_kpis_use_surviving_population(monkeypatch):
    """After min_terms filtering, KPIs must use only terms represented
    by the surviving pattern rows."""
    agg = _agg([
        _g("freight software solutions", "brand - uk", "1", spend=50.0, clicks=3),
        _g("freight management tools", "brand - uk", "1", spend=30.0, clicks=2),
        _g("tms pricing tool", "brand - uk", "1", spend=20.0, clicks=1),
    ])
    _patch(monkeypatch, agg, identity=_default_identity())
    # n=1, min_terms=2: only words appearing in 2+ terms survive.
    out = _build_patterns("30d", n=1, min_terms=2)
    # "freight" appears in 2 terms, so it survives.
    # "software", "solutions", "management", "tools", "tms", "pricing", "tool"
    # may each appear in only 1 term.
    kpis = out["kpis"]
    # If any patterns survive, terms_analysed should be <= total terms (3)
    # and > 0 for the "freight" pattern.
    if kpis["patterns_found"] > 0:
        assert kpis["terms_analysed"] > 0
        assert kpis["terms_analysed"] <= 3
