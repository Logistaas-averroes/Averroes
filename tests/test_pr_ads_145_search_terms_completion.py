"""
PR-ADS-145 — Search Terms Production Completion.

Two production-validation gaps closed after PR-ADS-144:

  1. Partial monetary coverage. Currency is assessed INDEPENDENTLY per durable
     term × campaign unit, so 27k verified Google Ads GBP rows keep a verified
     FX-converted subtotal even though ~78 legacy rows have no provable currency
     lineage. One unverified row never poisons a verified row; the legacy rows
     stay visible, marked ``legacy_currency_unverified``, never assigned GBP/USD,
     never fabricated to zero.

  2. Waste-evidence bridge. The weekly waste-detection pipeline persists
     confirmed evidence to ``waste_terms``; the Search Terms page now reflects it
     (safely campaign-scoped) so a confirmed term shows Flagged waste instead of
     Needs review — while absence from ``waste_terms`` never means clean.

Read-only. No Google Ads writes, no negative keywords, no HubSpot writes, no
legacy-evidence deletion.
"""

import os
import re
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
SERVICE = open(os.path.join(ROOT, "services", "search_term_evidence_service.py"),
               encoding="utf-8").read()
REPO = open(os.path.join(ROOT, "db", "search_term_repository.py"),
            encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
CONTRACT = open(os.path.join(ROOT, "docs", "API_CONTRACT.md"), encoding="utf-8").read()

ST_MODULE = APP_JS[APP_JS.find("// ── Search Terms + Patterns evidence page (PR-ADS-144)"):
                   APP_JS.find("function downloadWasteCSV() {")]


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _g(term, camp, cid, gbp, *, clicks=1, flagged=False, unrev=False,
       ccy="GBP", src="google_ads_api", junk=None):
    micros = int(gbp * 1_000_000) if gbp is not None else None
    return {"search_term": term, "campaign_name": camp, "campaign_id": cid,
            "spend_usd": gbp, "cost_micros": micros,
            "currency_codes": [ccy] if ccy else [],
            "source_systems": [src] if src else [],
            "clicks": clicks, "impressions": 10, "conversions": 0.0,
            "row_count": 1, "first_seen": date(2026, 7, 1),
            "last_seen": date(2026, 7, 2),
            "any_flagged": flagged, "any_unreviewed": unrev,
            "junk_categories": [junk] if junk else [], "matched_patterns": [],
            "ad_groups": [], "keywords": [], "match_types": []}


def _agg(rows):
    return {"available": True, "rows": rows, "source": {
        "row_count": len(rows),
        "spend_usd_total": sum(float(r["spend_usd"] or 0) for r in rows),
        "cost_micros_total": sum(int(r.get("cost_micros") or 0) for r in rows),
        "clicks_total": sum(r["clicks"] for r in rows),
        "impressions_total": sum(r["impressions"] for r in rows),
        "conversions_total": 0.0, "distinct_source_dates": 2,
        "min_source_date": date(2026, 7, 1), "max_source_date": date(2026, 7, 2),
        "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]}}


def _daily_costs_from(rows):
    def _fn(start, end):
        return {"available": True, "rows": [
            {"search_term": r["search_term"], "campaign_name": r["campaign_name"],
             "campaign_id": r["campaign_id"], "source_date": date(2026, 7, 2),
             "cost_micros": r["cost_micros"], "currency_codes": r["currency_codes"],
             "source_systems": r["source_systems"]}
            for r in rows if r["cost_micros"]]}
    return _fn


def _fx(rate=1.25):
    def _fn(start, end, base, quote):
        return {"available": True, "rates": {
            date(2026, 7, d).isoformat(): rate for d in range(1, 32)}}
    return _fn


def _canonical(total_usd=500.0, fx_complete=True):
    return {"available": True, "customer_id": "c1", "total_spend_usd": total_usd,
            "fx_complete": fx_complete, "rows": [
                {"campaign_id": "1", "campaign_name": "Brand - UK"},
                {"campaign_id": "2", "campaign_name": "Gulf"}]}


def _patch(monkeypatch, rows, *, waste=None, canonical=None, rate=1.25,
           identity=None):
    import db.revenue_repository as rr
    import db.search_term_repository as st
    monkeypatch.setattr(st, "fetch_search_term_aggregates", lambda a, b: _agg(rows))
    monkeypatch.setattr(st, "fetch_search_term_daily_costs", _daily_costs_from(rows))
    monkeypatch.setattr(st, "fetch_waste_evidence_for_terms",
                        lambda terms: waste or {"available": True, "rows": []})
    monkeypatch.setattr(rr, "fetch_canonical_campaign_spend",
                        lambda a, b: canonical or _canonical())
    monkeypatch.setattr(rr, "fetch_campaign_identity",
                        lambda customer_id=None: identity or {"available": True, "mappings": []})
    monkeypatch.setattr(rr, "fetch_fx_rates", _fx(rate))


def _build(now=None, **kw):
    from services.search_term_evidence_service import build_search_term_evidence
    return build_search_term_evidence("30d",
                                      now=now or datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc),
                                      **kw)


def _row(out, term):
    return [r for r in out["rows"] if r["search_term"] == term][0]


# ════════════════ 1. Monetary completeness (three states) ════════════════


def test_partial_verified_subtotal_not_suppressed_by_legacy(monkeypatch):
    rows = [_g(f"verified {i}", "brand - uk", "1", 10.0) for i in range(27)]
    rows.append(_g("legacy term", "old camp", None, 5.0, ccy=None, src=None))
    _patch(monkeypatch, rows)
    out = _build()
    k = out["kpis"]
    mon = k["monetary"]
    # 27 verified rows → verified subtotal survives the 1 unverified legacy row.
    assert k["monetary_status"] == "partial"
    assert mon["verified_currency_units"] == 27
    assert mon["unverified_currency_units"] == 1
    assert mon["fx_complete_units"] == 27
    assert k["verified_spend_usd"] == round(27 * 10.0 * 1.25, 2)   # £270 → $337.50
    assert mon["verified_native_spend"] == 270.0
    assert mon["monetary_population_pct"] == round(27 / 28 * 100, 2)


def test_unverified_row_remains_visible_with_marker(monkeypatch):
    rows = [_g("verified", "brand - uk", "1", 10.0),
            _g("legacy term", "old camp", None, 5.0, ccy=None, src=None)]
    _patch(monkeypatch, rows)
    out = _build()
    legacy = _row(out, "legacy term")
    assert legacy["spend_usd"] is None            # withheld, never fabricated 0
    assert legacy["spend_native"] is None         # never assumed GBP
    assert legacy["cpc_usd"] is None
    assert legacy["legacy_currency_unverified"] is True
    assert legacy["currency_status"] == "legacy_currency_unverified"
    # It still appears in the table.
    assert any(r["search_term"] == "legacy term" for r in out["rows"])


def test_partial_kpi_discloses_excluded_count(monkeypatch):
    rows = [_g("v1", "brand - uk", "1", 10.0), _g("v2", "brand - uk", "1", 10.0),
            _g("legacy a", "old", None, 1.0, ccy=None, src=None),
            _g("legacy b", "old", None, 1.0, ccy=None, src=None)]
    _patch(monkeypatch, rows)
    out = _build()
    assert out["kpis"]["monetary"]["unverified_currency_units"] == 2
    # The frontend renders an accurate exclusion phrase that distinguishes legacy
    # (unverified-currency) rows from FX-incomplete rows — never "excludes 0
    # legacy rows" when the partiality is actually FX-incompleteness.
    assert "function stExclusionPhrase" in ST_MODULE
    assert "stExclusionPhrase(mon.unverified_currency_units, mon.fx_incomplete_units)" in ST_MODULE
    assert "FX-incomplete" in ST_MODULE
    # The coverage subline also discloses both exclusion reasons via the helper.
    assert "stExclusionPhrase(coverage.excluded_unverified_units, coverage.excluded_fx_incomplete_units)" in ST_MODULE
    # Coverage metadata now carries the FX-incomplete exclusion count too.
    cov = out["kpis"]["coverage"]
    assert "excluded_fx_incomplete_units" in cov


def test_complete_population_returns_complete(monkeypatch):
    rows = [_g("a", "brand - uk", "1", 10.0), _g("b", "gulf", "2", 20.0)]
    _patch(monkeypatch, rows)
    out = _build()
    assert out["kpis"]["monetary_status"] == "complete"
    assert out["kpis"]["verified_spend_usd"] == round(30.0 * 1.25, 2)


def test_fully_unverified_population_returns_unavailable(monkeypatch):
    rows = [_g("legacy a", "old", None, 5.0, ccy=None, src=None),
            _g("legacy b", "old", None, 7.0, ccy=None, src=None)]
    _patch(monkeypatch, rows)
    out = _build()
    assert out["kpis"]["monetary_status"] == "unavailable"
    assert out["kpis"]["verified_spend_usd"] is None
    assert out["kpis"]["coverage"]["status"] == "unavailable"


def test_missing_fx_affects_only_the_unit_needing_that_date(monkeypatch):
    import db.revenue_repository as rr
    import db.search_term_repository as st
    rows = [_g("has fx", "brand - uk", "1", 10.0),
            _g("needs missing date", "gulf", "2", 20.0)]

    def _daily(start, end):
        return {"available": True, "rows": [
            {"search_term": "has fx", "campaign_name": "brand - uk",
             "campaign_id": "1", "source_date": date(2026, 7, 2),
             "cost_micros": 10_000_000, "currency_codes": ["GBP"],
             "source_systems": ["google_ads_api"]},
            # This unit's only date has NO fx rate below.
            {"search_term": "needs missing date", "campaign_name": "gulf",
             "campaign_id": "2", "source_date": date(2020, 1, 1),
             "cost_micros": 20_000_000, "currency_codes": ["GBP"],
             "source_systems": ["google_ads_api"]}]}

    monkeypatch.setattr(st, "fetch_search_term_aggregates", lambda a, b: _agg(rows))
    monkeypatch.setattr(st, "fetch_search_term_daily_costs", _daily)
    monkeypatch.setattr(st, "fetch_waste_evidence_for_terms",
                        lambda t: {"available": True, "rows": []})
    monkeypatch.setattr(rr, "fetch_canonical_campaign_spend", lambda a, b: _canonical())
    monkeypatch.setattr(rr, "fetch_campaign_identity",
                        lambda customer_id=None: {"available": True, "mappings": []})
    monkeypatch.setattr(rr, "fetch_fx_rates", _fx(1.25))   # only covers Jul 2026
    out = _build()
    assert _row(out, "has fx")["spend_usd"] == 12.5        # verified
    assert _row(out, "has fx")["fx_complete"] is True
    miss = _row(out, "needs missing date")
    assert miss["spend_usd"] is None                       # only THIS unit withheld
    assert miss["fx_complete"] is False
    assert miss["currency_status"] == "fx_incomplete"
    assert out["kpis"]["monetary_status"] == "partial"


def test_no_native_gbp_labelled_usd(monkeypatch):
    rows = [_g("legacy", "old", None, 5.0, ccy=None, src=None)]
    _patch(monkeypatch, rows)
    out = _build()
    r = _row(out, "legacy")
    assert r["native_currency"] is None       # never asserted GBP from account default
    assert r["spend_usd"] is None
    # Service documents native-not-USD discipline.
    assert "never assigned GBP/USD" in SERVICE or "never assumed GBP/USD" in ST_MODULE


def test_verified_only_coverage_scope_when_partial(monkeypatch):
    rows = [_g("v", "brand - uk", "1", 100.0),
            _g("legacy", "old", None, 5.0, ccy=None, src=None)]
    _patch(monkeypatch, rows, canonical=_canonical(total_usd=500.0, fx_complete=True))
    cov = _build()["kpis"]["coverage"]
    assert cov["status"] == "ok"
    assert cov["scope"] == "verified_only"
    assert cov["excluded_unverified_units"] == 1
    # Numerator is the verified USD subtotal (£100 → $125), not a raw total.
    assert cov["verified_search_term_spend_usd"] == 125.0
    assert cov["coverage_pct"] == round(125.0 / 500.0 * 100, 2)


# ════════════════ 2. Classification bridge ════════════════


def _waste(term, camp, crm=3, junk="job_seeker", pattern="jobs"):
    return {"available": True, "rows": [
        {"search_term": term, "campaign_name": camp, "junk_category": junk,
         "matched_pattern": pattern, "crm_junk_confirmed": crm,
         "run_date": "2026-07-10"}]}


def test_safe_waste_evidence_produces_flagged(monkeypatch):
    # A newly imported term (is_flagged_waste NULL → needs review) that the
    # weekly pipeline already confirmed becomes Flagged waste.
    rows = [_g("freight jobs", "gulf", "2", 20.0, unrev=True)]
    _patch(monkeypatch, rows, waste=_waste("freight jobs", "gulf"))
    out = _build()
    r = _row(out, "freight jobs")
    assert r["state"] == "flagged"
    assert r["classification_source"] == "waste_terms"
    assert r["crm_junk_confirmed"] == 3
    assert out["kpis"]["flagged_waste"] == 1
    assert out["kpis"]["needs_review"] == 0


def test_unmatched_term_remains_needs_review(monkeypatch):
    rows = [_g("freight jobs", "gulf", "2", 20.0, unrev=True)]
    # waste evidence exists but for a DIFFERENT term.
    _patch(monkeypatch, rows, waste=_waste("other term", "gulf"))
    out = _build()
    assert _row(out, "freight jobs")["state"] == "needs_review"


def test_absence_from_waste_terms_never_clean(monkeypatch):
    # is_flagged_waste NULL, no waste evidence → Needs review, NOT clean.
    rows = [_g("brand new term", "gulf", "2", 20.0, unrev=True)]
    _patch(monkeypatch, rows, waste={"available": True, "rows": []})
    out = _build()
    r = _row(out, "brand new term")
    assert r["state"] == "needs_review"
    assert r["classification_source"] == "unclassified"
    assert out["kpis"]["reviewed_clean"] == 0


def test_explicit_false_remains_reviewed_clean(monkeypatch):
    rows = [_g("reviewed term", "gulf", "2", 20.0, flagged=False, unrev=False)]
    _patch(monkeypatch, rows, waste={"available": True, "rows": []})
    out = _build()
    r = _row(out, "reviewed term")
    assert r["state"] == "clean"
    assert r["classification_source"] == "durable_reviewed_clean"


def test_same_name_ambiguous_campaigns_cannot_borrow_waste(monkeypatch):
    # Two campaign ids share the display name "brand - uk"; waste_terms records
    # only the name → ambiguous → neither borrows it.
    rows = [_g("freight software", "brand - uk", "10", 10.0, unrev=True),
            _g("freight software", "brand - uk", "20", 20.0, unrev=True)]
    canonical = {"available": True, "customer_id": "c1", "total_spend_usd": None,
                 "fx_complete": False, "rows": [
                     {"campaign_id": "10", "campaign_name": "brand - uk"},
                     {"campaign_id": "20", "campaign_name": "brand - uk"}]}
    _patch(monkeypatch, rows, waste=_waste("freight software", "brand - uk"),
           canonical=canonical)
    out = _build()
    for r in out["rows"]:
        assert r["state"] == "needs_review"            # neither flagged
        assert r["crm_junk_confirmed"] is None         # evidence not borrowed


def test_globally_ambiguous_name_cannot_borrow_waste_even_when_alone_in_window(monkeypatch):
    # A display name shared by TWO canonical campaign ids is globally ambiguous.
    # waste_terms carries only the name, so even when only ONE of those campaigns
    # has the term in the selected window it must NOT borrow the evidence — the
    # name cannot be attributed to a single campaign id.
    rows = [_g("freight software", "gulf", "2", 20.0, unrev=True)]
    canonical = {"available": True, "customer_id": "c1", "total_spend_usd": None,
                 "fx_complete": False, "rows": [
                     {"campaign_id": "2", "campaign_name": "gulf"},
                     {"campaign_id": "3", "campaign_name": "gulf"}]}
    _patch(monkeypatch, rows, waste=_waste("freight software", "gulf"),
           canonical=canonical)
    out = _build()
    r = _row(out, "freight software")
    assert r["state"] == "needs_review"        # not flagged from an ambiguous name
    assert r["crm_junk_confirmed"] is None      # evidence not borrowed


def test_unmatched_unit_never_borrows_waste(monkeypatch):
    # An unmatched (Mapping review) unit has no confirmed Google Ads identity, so
    # even an exact (term, campaign_name) waste_terms match must not flag it.
    rows = [_g("freight jobs", "mystery co", None, 20.0, unrev=True)]
    _patch(monkeypatch, rows, waste=_waste("freight jobs", "mystery co"))
    out = _build()
    r = _row(out, "freight jobs")
    assert r["mapping_status"] == "unmatched"
    assert r["state"] == "needs_review"
    assert r["crm_junk_confirmed"] is None


def test_durable_flag_beats_everything(monkeypatch):
    rows = [_g("junk term", "gulf", "2", 20.0, flagged=True)]
    _patch(monkeypatch, rows, waste=_waste("junk term", "gulf"))
    out = _build()
    r = _row(out, "junk term")
    assert r["state"] == "flagged"
    assert r["classification_source"] == "durable_flag"


def test_state_totals_reconcile_to_reported_terms(monkeypatch):
    rows = [_g("flagged", "gulf", "2", 10.0, flagged=True),
            _g("confirmed", "gulf", "2", 10.0, unrev=True),
            _g("clean", "gulf", "2", 10.0),
            _g("needs", "gulf", "2", 10.0, unrev=True)]
    _patch(monkeypatch, rows, waste=_waste("confirmed", "gulf"))
    out = _build()
    k = out["kpis"]
    assert k["flagged_waste"] + k["reviewed_clean"] + k["needs_review"] == k["reported_terms"]
    assert k["flagged_waste"] == 2            # durable flag + confirmed waste
    assert k["needs_review"] == 1
    assert k["reviewed_clean"] == 1


def test_patterns_inherit_corrected_states(monkeypatch):
    from services.search_term_evidence_service import build_search_pattern_evidence
    rows = [_g("freight jobs", "gulf", "2", 20.0, unrev=True)]
    _patch(monkeypatch, rows, waste=_waste("freight jobs", "gulf"))
    out = build_search_pattern_evidence(
        "30d", n=1, now=datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc))
    freight = [p for p in out["rows"] if p["pattern"] == "freight"][0]
    # The confirmed-waste term makes its patterns show Flagged present.
    assert freight["flagged_terms"] == 1
    assert freight["signal"] == "flagged_present"


def test_pattern_spend_partial_when_underlying_unverified(monkeypatch):
    from services.search_term_evidence_service import build_search_pattern_evidence
    rows = [_g("freight software", "brand - uk", "1", 100.0),
            _g("freight legacy", "old", None, 5.0, ccy=None, src=None)]
    _patch(monkeypatch, rows)
    out = build_search_pattern_evidence(
        "30d", n=1, now=datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc))
    freight = [p for p in out["rows"] if p["pattern"] == "freight"][0]
    assert freight["spend_partial"] is True         # one underlying term unverified
    assert freight["reported_spend_usd"] == 125.0   # verified subtotal only
    assert out["kpis"]["spend_status"] == "partial"


def test_pattern_spend_unavailable_when_all_underlying_unverified(monkeypatch):
    # Every contributing term is legacy/unverified → there is NO verified
    # FX-complete spend at all. The represented-spend KPI must be Unavailable
    # (null + "unavailable"), never a null presented as merely "partial".
    from services.search_term_evidence_service import build_search_pattern_evidence
    rows = [_g("freight legacy one", "old", None, 5.0, ccy=None, src=None),
            _g("freight legacy two", "old", None, 7.0, ccy=None, src=None)]
    _patch(monkeypatch, rows)
    out = build_search_pattern_evidence(
        "30d", n=1, now=datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc))
    assert out["kpis"]["reported_spend_represented_usd"] is None
    assert out["kpis"]["spend_status"] == "unavailable"


# ════════════════ 3. Regression / read-only ════════════════


def test_service_and_repo_are_read_only():
    forbidden = ["INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE",
                 "add_negative", "MutateOperation", "upload_conversion"]
    for verb in forbidden:
        assert verb not in REPO, f"repository not read-only: {verb}"
        assert verb not in SERVICE, f"service not read-only: {verb}"


def test_no_write_verbs_in_new_endpoint():
    region = SERVER[SERVER.find("def api_search_term_currency_audit"):
                    SERVER.find("def api_search_term_evidence_export")]
    assert "read_only" in region
    assert "@app.post" not in region and "@app.put" not in region
    assert "@app.delete" not in region


def test_legacy_rows_never_deleted_or_relabelled():
    fn = REPO[REPO.find("def fetch_legacy_currency_audit"):
              REPO.find("def fetch_waste_evidence_for_terms")]
    # The audit is SELECT/EXISTS only — no mutation of the legacy rows.
    for verb in ["DELETE", "UPDATE", "INSERT"]:
        assert verb not in fn, verb
    assert "EXISTS" in fn                    # verified-replacement check


def test_currency_audit_endpoint_documented():
    assert "/api/search-term-evidence/currency-audit" in CONTRACT
    normalized = re.sub(r"\s+", " ", CONTRACT)
    for needle in ["legacy_currency_unverified", "verified replacement",
                   "partial", "Absence from `waste_terms` never means clean"]:
        assert re.sub(r"\s+", " ", needle) in normalized, needle


def test_frontend_shows_complete_partial_unavailable():
    assert "Verified Search-Term Spend" in ST_MODULE
    assert "st-mon-badge--partial" in ST_MODULE
    assert "st-mon-badge--complete" in ST_MODULE
    assert "Unverified" in ST_MODULE                 # legacy row marker
    # No forbidden business-value wording introduced.
    assert not re.search(r"\bWinner\b|\bValuable\b", ST_MODULE)


def test_currency_audit_read_only_shape(monkeypatch):
    import db.search_term_repository as st

    def _audit():
        return {"available": True, "summary": {
            "legacy_unverified_row_count": 78, "terms_represented": 40,
            "campaigns_represented": 5, "min_source_date": "2026-01-01",
            "max_source_date": "2026-03-01", "campaigns": ["a"],
            "rows_with_verified_replacement": 0}, "rows": []}
    monkeypatch.setattr(st, "fetch_legacy_currency_audit", _audit)
    import os as _os
    _os.environ.setdefault("DASHBOARD_PASSWORD_HASH", "x")
    from fastapi.testclient import TestClient
    import api.server as srv
    srv.app.dependency_overrides[srv.require_auth] = lambda: {"user": "t"}
    client = TestClient(srv.app)
    r = client.get("/api/search-term-evidence/currency-audit")
    assert r.status_code == 200
    body = r.json()
    assert body["read_only"] is True
    assert body["marker"] == "legacy_currency_unverified"
    assert body["summary"]["legacy_unverified_row_count"] == 78
    srv.app.dependency_overrides.clear()


def test_writer_never_raises_on_bad_cost_micros_and_defaults_source(monkeypatch):
    """PR-ADS-145 review: write_search_terms must not raise on a non-numeric
    cost_micros (e.g. empty string) and must default source_system to 'unknown'
    to match the documented lineage contract."""
    import contextlib
    import db.writers as w

    captured = {}

    class _Cur:
        def executemany(self, sql, rows): captured["rows"] = rows
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()

    @contextlib.contextmanager
    def _fake_conn():
        yield _Conn()

    monkeypatch.setattr(w, "get_conn", _fake_conn)
    n = w.write_search_terms(1, [
        {"search_term": "x", "campaign": "c", "date": "2026-07-01",
         "cost_micros": "", "currency_code": "GBP"},                 # stray empty
        {"search_term": "y", "campaign": "c", "date": "2026-07-01",
         "cost_micros": "50", "source": "google_ads_api"},
    ])
    assert n == 2                                    # did not raise / abort
    # Tuple layout: (...,, cost_micros[12], currency_code[13], source_system[14], ...)
    by_term = {r[7]: r for r in captured["rows"]}
    assert by_term["x"][12] is None                  # empty string → None (not a crash)
    assert by_term["x"][14] == "unknown"             # documented default
    assert by_term["y"][12] == 50
    assert by_term["y"][14] == "google_ads_api"
