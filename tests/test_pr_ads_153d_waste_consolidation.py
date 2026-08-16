"""
tests/test_pr_ads_153d_waste_consolidation.py

PR-ADS-153D — Search-Term Waste Consolidation & Navigation Cleanup.

Covers the required suites:
  §40  navigation — Flagged Waste Terms retired, Search Terms stays under
       Google Ads, GCLID Attribution moves to Admin, old URLs redirect, no dead
       Action Queue links;
  §41  search-term truth — repeated run snapshots cannot double-count, canonical
       spend is the ONE source, annotations are not a second ledger, campaign
       identity is preserved and same-text terms never collide;
  §42  SQL attribution — canonical lifecycle SQL is only ATTRIBUTED here,
       unavailable is never zero, proven zero is 0, and the count never exceeds
       a broader scope;
  §43  flag / review state — durable, shared, local-only, auditable after
       resolution, unmapped reasons surfaced;
  §44  Action Queue — one durable term → one item, stable across repeated sync,
       resolved terms not reopened, same spend and reason as the page;
  §45  windows — evidence vocabulary, no leftover waste-specific resolver;
  §46  privacy / governance — no PII, no Google Ads mutation, no Mailchimp.

Run with:
    python -m pytest tests/test_pr_ads_153d_waste_consolidation.py -v
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import search_term_identity as identity  # noqa: E402
from analysis import search_term_review_state as review  # noqa: E402
from analysis import waste_reason_taxonomy as taxonomy  # noqa: E402

_APP_JS = (_ROOT / "static" / "app.js").read_text()
_INDEX_HTML = (_ROOT / "static" / "index.html").read_text()
_SERVER_PY = (_ROOT / "api" / "server.py").read_text()
_SERVICE_PY = (_ROOT / "services" / "search_term_evidence_service.py").read_text()
_SCHEMA_PY = (_ROOT / "db" / "schema.py").read_text()
_REVIEW_REPO_PY = (_ROOT / "db" / "search_term_review_repository.py").read_text()


# =============================================================================
# Fixtures — durable layer patched directly, mirroring the PR-ADS-144 suite
# =============================================================================
def _g(term, campaign=None, cid=None, spend=0.0, clicks=0, impressions=0,
       conversions=0.0, rows=1, first=date(2026, 7, 1), last=date(2026, 7, 2),
       flagged=False, unreviewed=False, junk=None, pattern=None):
    return {
        "search_term": term, "campaign_name": campaign, "campaign_id": cid,
        "spend_usd": spend, "clicks": clicks, "impressions": impressions,
        "conversions": conversions, "row_count": rows,
        "cost_micros": int((spend or 0) * 1_000_000),
        "currency_codes": ["GBP"], "source_systems": ["google_ads_api"],
        "first_seen": first, "last_seen": last,
        "any_flagged": flagged, "any_unreviewed": unreviewed,
        "junk_categories": [junk] if junk else [],
        "matched_patterns": [pattern] if pattern else [],
        "ad_groups": [], "keywords": [], "match_types": [],
    }


def _agg(rows):
    return {"available": True, "rows": rows, "source": {
        "row_count": sum(r["row_count"] for r in rows),
        "spend_usd_total": sum(float(r["spend_usd"] or 0) for r in rows),
        "cost_micros_total": sum(int(r["cost_micros"] or 0) for r in rows),
        "clicks_total": sum(int(r["clicks"] or 0) for r in rows),
        "impressions_total": sum(int(r["impressions"] or 0) for r in rows),
        "conversions_total": 0.0, "distinct_source_dates": 2,
        "min_source_date": date(2026, 7, 1), "max_source_date": date(2026, 7, 2),
        "currency_codes": ["GBP"], "source_systems": ["google_ads_api"],
    }}


_CAMPAIGNS = [
    {"campaign_id": "1", "campaign_name": "Brand - UK", "spend": 80.0,
     "spend_usd": 80.0, "fx_complete": True},
    {"campaign_id": "2", "campaign_name": "Gulf", "spend": 20.0,
     "spend_usd": 20.0, "fx_complete": True},
]


def _patch(monkeypatch, agg, *, waste_rows=None, reviews=None, sql_attr=None):
    """Patch every durable read the flagged view performs."""
    from datetime import timedelta

    import db.revenue_repository as revenue_repo
    import db.search_term_repository as st_repo
    import db.search_term_review_repository as review_repo
    import services.search_term_evidence_service as svc

    monkeypatch.setattr(st_repo, "fetch_search_term_aggregates",
                        lambda s, e: agg)
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_costs",
                        lambda s, e: {"available": True, "rows": [{
                            "search_term": g["search_term"],
                            "campaign_name": g.get("campaign_name"),
                            "campaign_id": g.get("campaign_id"),
                            "source_date": e,
                            "cost_micros": g.get("cost_micros"),
                            "currency_codes": ["GBP"],
                            "source_systems": ["google_ads_api"],
                        } for g in agg["rows"]]})
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e: {"available": True, "customer_id": "c1",
                                      "currency_code": "GBP",
                                      "reporting_currency": "USD",
                                      "fx_complete": True, "fx_missing_days": 0,
                                      "total_spend": 100.0,
                                      "total_spend_usd": 100.0,
                                      "campaign_count": 2, "rows": _CAMPAIGNS})
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(st_repo, "fetch_waste_evidence_for_terms",
                        lambda terms: {"available": True,
                                       "rows": waste_rows or []})
    monkeypatch.setattr(st_repo, "fetch_latest_waste_classification",
                        lambda *a, **k: {"available": True, "row": None})
    monkeypatch.setattr(revenue_repo, "fetch_fx_coverage",
                        lambda s, e, b, q: {"available": True, "complete": True,
                                            "spend_days": 7, "covered_days": 7,
                                            "missing_dates": []})

    def _rates(start, end, base, quote):
        out, d = {}, (start or date(2026, 7, 1))
        while d <= end:
            out[d.isoformat()] = 1.0
            d += timedelta(days=1)
        return {"available": True, "rates": out}

    monkeypatch.setattr(revenue_repo, "fetch_fx_rates", _rates)
    monkeypatch.setattr(review_repo, "fetch_reviews_for_identities",
                        lambda ids: {"available": True, "rows": reviews or {}})
    if sql_attr is not None:
        monkeypatch.setattr(svc, "_search_term_sql_attribution",
                            lambda pop, s, e: sql_attr)


def _flagged(window="30d", **kw):
    from services.search_term_evidence_service import build_flagged_search_terms
    return build_flagged_search_terms(window, **kw)


def _no_attribution():
    return {"by_unit": {}, "reconciliation": {}, "coverage": {}, "audit": {},
            "available": False, "contacts": [], "population_has_text": False}


# =============================================================================
# §40 — Navigation
# =============================================================================
def test_flagged_waste_terms_nav_item_is_removed():
    assert 'data-page="waste"' not in _INDEX_HTML
    assert "Flagged Waste Terms</span>" not in _INDEX_HTML


def test_standalone_waste_page_markup_and_loader_are_deleted():
    assert 'id="page-waste"' not in _INDEX_HTML
    assert 'id="waste-table-body"' not in _INDEX_HTML
    assert 'id="waste-kpi-spend"' not in _INDEX_HTML
    for fn in ("function loadWaste(", "function renderWasteTable(",
               "function renderWasteKPIs(", "function populateWasteFilters(",
               "function getFilteredWasteTerms(", "function applyWasteFilters(",
               "function copyWasteTerms(", "function downloadWasteCSV("):
        assert fn not in _APP_JS, fn


def test_old_waste_url_redirects_to_the_flagged_view():
    assert 'waste: { page: "search-terms", tab: "flagged" }' in _APP_JS
    # The redirect carries the INTENT, not merely the URL.
    assert "if (retired.tab) stSetPendingTab(retired.tab);" in _APP_JS


def test_ngrams_url_still_redirects_to_patterns():
    assert '"ngrams": "search-terms"' in _APP_JS
    assert 'startsWith("#/ngrams")' in _APP_JS


def test_search_terms_stays_under_platform_evidence():
    section = _INDEX_HTML.split("<!-- Platform Evidence -->")[1]
    section = section.split("<!-- CRM & Revenue")[0]
    assert 'data-page="search-terms"' in section
    assert 'data-page="campaigns"' in section
    assert 'data-page="keywords"' in section
    assert 'data-page="geo"' in section
    # And the retired page is not there.
    assert 'data-page="waste"' not in section


def test_gclid_attribution_moves_to_admin():
    admin = _INDEX_HTML.split(">Admin</li>")[1]
    assert 'data-page="gclid-attribution"' in admin
    crm = _INDEX_HTML.split("<!-- CRM & Revenue")[1].split("<!-- Admin -->")[0]
    assert 'data-page="gclid-attribution"' not in crm


def test_no_standalone_ngrams_navigation_item():
    assert 'data-page="ngrams"' not in _INDEX_HTML


def test_no_dead_action_queue_or_drawer_links_to_the_retired_page():
    # Nothing navigates to a page that no longer loads.
    assert 'navigate("waste"' not in _APP_JS
    assert 'href="#/waste"' not in _APP_JS
    assert 'data-navigate="waste"' not in _APP_JS
    # The queue's waste action goes to the canonical destination instead.
    assert 'data-navigate="search-terms"' in _APP_JS


def test_action_queue_link_lands_on_the_flagged_view_for_that_term():
    fn = _SERVER_PY.split("def _build_waste_queue_items(")[1].split("\ndef ")[0]
    assert '"page": "search-terms"' in fn
    assert "#/search-terms?tab=flagged&term=" in fn
    # The frontend honours a deep link only when it targets the declared page.
    assert "link.hash.startsWith(`#/${linkPage}`)" in _APP_JS


def test_retired_page_registries_are_cleaned_up():
    for banned in ('PAGE_EXPLANATIONS missing', '  waste: {\n    title: "Flagged'):
        assert banned not in _APP_JS
    evidence_block = _APP_JS.split("const EVIDENCE_PAGES")[1][:200]
    assert '"waste"' not in evidence_block


# =============================================================================
# §41 — Search-term truth (no snapshot double-counting, one fact source)
# =============================================================================
def test_repeated_snapshots_cannot_change_canonical_metrics(monkeypatch):
    """The same durable term observed by many runs is ONE row with ONE spend.

    `row_count=5` models five canonical fact rows; the annotation table may hold
    five run snapshots for it. Neither multiplies the metric.
    """
    _patch(monkeypatch, _agg([
        _g("freight jobs", "Brand - UK", "1", spend=40.0, clicks=10, rows=5,
           flagged=True, junk="job_seeker"),
    ]), waste_rows=[
        {"search_term": "freight jobs", "campaign_name": "Brand - UK",
         "junk_category": "job_seeker", "matched_pattern": "jobs",
         "crm_junk_confirmed": 2, "run_date": "2026-07-0%d" % d}
        for d in range(1, 6)
    ], sql_attr=_no_attribution())

    payload = _flagged()
    assert payload["kpis"]["flagged_terms"] == 1
    assert payload["kpis"]["flagged_spend_usd"] == 40.0
    assert payload["kpis"]["clicks"] == 10
    assert len(payload["rows"]) == 1


def test_annotations_are_never_a_second_spend_ledger(monkeypatch):
    """waste_terms carries no metric that reaches a KPI."""
    _patch(monkeypatch, _agg([
        _g("freight jobs", "Brand - UK", "1", spend=40.0, flagged=True,
           junk="job_seeker"),
    ]), waste_rows=[
        # A wildly wrong spend in the annotation table must change nothing.
        {"search_term": "freight jobs", "campaign_name": "Brand - UK",
         "junk_category": "job_seeker", "matched_pattern": "jobs",
         "crm_junk_confirmed": 99, "spend_usd": 999999.0,
         "run_date": "2026-07-01"},
    ], sql_attr=_no_attribution())

    payload = _flagged()
    assert payload["kpis"]["flagged_spend_usd"] == 40.0
    assert payload["annotation_source"]["table"] == "waste_terms"
    assert "never a source of spend" in payload["annotation_source"]["role"].lower()


def test_canonical_fact_source_is_declared_in_the_payload(monkeypatch):
    _patch(monkeypatch, _agg([_g("t", "Brand - UK", "1", flagged=True, junk="student")]),
           sql_attr=_no_attribution())
    src = _flagged()["canonical_fact_source"]
    assert src["table"] == "search_terms"
    assert src["dedup_key"] == "idx_search_terms_unique_fact"
    assert src["identity"] == "analysis/search_term_identity.term_identity_key"


def test_same_term_in_two_campaigns_does_not_collide(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
        _g("tms", "Gulf", "2", spend=20.0, flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())

    payload = _flagged()
    assert payload["kpis"]["flagged_terms"] == 2
    identities = {r["term_identity"] for r in payload["rows"]}
    assert len(identities) == 2
    assert payload["kpis"]["flagged_spend_usd"] == 30.0


def test_campaign_identity_is_preserved_on_every_row(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    assert row["campaign_key"] == "1"
    assert row["campaign_name"] == "Brand - UK"
    assert row["mapping_status"] == "mapped"


def test_flagged_is_not_derived_from_spend_without_sqls(monkeypatch):
    """A high-spend term with NO durable flag must not appear."""
    _patch(monkeypatch, _agg([
        _g("expensive but clean", "Brand - UK", "1", spend=5000.0,
           unreviewed=True),
    ]), sql_attr=_no_attribution())
    payload = _flagged()
    assert payload["kpis"]["flagged_terms"] == 0
    assert payload["rows"] == []


def _strip_py_docstrings_and_comments(source: str) -> str:
    """Executable code only — prose necessarily NAMES the rule it forbids."""
    without_docstrings = re.sub(r'(?s)""".*?"""', "", source)
    return re.sub(r"^\s*#.*$", "", without_docstrings, flags=re.M)


def test_service_contains_no_spend_without_sqls_waste_rule():
    fn = _SERVICE_PY.split("def build_flagged_search_terms(")[1].split("\ndef ")[0]
    # The docstring states the prohibition explicitly (it wraps across lines)...
    assert re.search(r"NEVER\s+derived\s+from", fn)
    # ...and no code implements it.
    code = _strip_py_docstrings_and_comments(fn).lower()
    assert "sqls = 0" not in code
    assert "sqls == 0" not in code


# ── Durable identity (§9) ───────────────────────────────────────────────────
def test_identity_is_stable_and_normalizes_the_query():
    a = identity.term_identity_key("1", "Freight  JOBS")
    b = identity.term_identity_key("1", "freight jobs")
    assert a == b
    # Different campaigns never share an identity.
    assert identity.term_identity_key("2", "freight jobs") != a


def test_identity_encoding_cannot_be_confused_across_the_boundary():
    """Length-prefixed encoding: ("ab","c") and ("a","bc") must differ."""
    assert identity.term_identity_key("ab", "c") != identity.term_identity_key("a", "bc")


def test_identity_keeps_punctuation_distinct():
    """Google Ads reports these separately and a reviewer may judge them
    differently, so collapsing them would merge two distinct facts."""
    assert (identity.term_identity_key("1", "logistics software")
            != identity.term_identity_key("1", "logistics-software"))


def test_missing_campaign_is_a_named_unknown_not_an_empty_bucket():
    assert identity.normalize_campaign_key(None) == identity.CAMPAIGN_KEY_UNKNOWN
    assert identity.normalize_campaign_key("  ") == identity.CAMPAIGN_KEY_UNKNOWN


def test_identity_components_stay_auditable():
    parts = identity.identity_components("1", "Freight Jobs")
    assert parts["campaign_key"] == "1"
    assert parts["search_term_normalized"] == "freight jobs"
    assert parts["identity_rule_version"] == identity.IDENTITY_RULE_VERSION
    assert parts["term_identity"] == identity.term_identity_key("1", "Freight Jobs")


# ── Join contract (§24) ─────────────────────────────────────────────────────
def test_unjoinable_annotations_are_counted_not_guessed(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), waste_rows=[
        # Belongs to a campaign that is not in this window's population.
        {"search_term": "tms", "campaign_name": "Some Other Campaign",
         "junk_category": "job_seeker", "matched_pattern": "x",
         "crm_junk_confirmed": 0, "run_date": "2026-07-01"},
    ], sql_attr=_no_attribution())

    join = _flagged()["annotation_join"]
    assert join["annotation_rows"] == 1
    assert join["attached"] == 0
    assert join["legacy_unresolved"] == 1
    # And the unplaceable evidence never leaks onto the term.
    assert _flagged()["rows"][0]["flag_reason"] != "job_seeker"


def test_unresolved_annotations_downgrade_the_truth_state(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), waste_rows=[
        {"search_term": "tms", "campaign_name": "Ghost", "junk_category": "x",
         "matched_pattern": None, "crm_junk_confirmed": 0,
         "run_date": "2026-07-01"},
    ], sql_attr=_no_attribution())
    truth = _flagged()["truth_state"]
    assert truth["status"] == "partial"
    assert any(r.startswith("legacy_unresolved_annotations")
               for r in truth["reasons"])


# =============================================================================
# §42 — SQL attribution
# =============================================================================
def _attr(by_unit, *, available=True):
    return {"by_unit": by_unit, "reconciliation": {}, "coverage": {},
            "audit": {}, "available": available, "contacts": [],
            "population_has_text": True}


def test_attribution_unavailable_is_null_never_zero(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())
    payload = _flagged()
    assert payload["kpis"]["sql_evidence"] is None
    assert payload["kpis"]["sql_evidence_available"] is False
    assert payload["rows"][0]["attributed_sqls"] is None
    assert payload["rows"][0]["sql_attribution_status"] == "unavailable"


def test_proven_zero_is_zero(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_attr({"1\x00tms": {
        "attributed_sqls": 0, "sql_attribution_status": "known_zero",
        "sql_attribution_source": "direct_query", "sql_ambiguity_reason": None,
        "sql_candidate_count": 0, "sql_contact_keys": []}}))
    payload = _flagged()
    assert payload["kpis"]["sql_evidence"] == 0
    assert payload["kpis"]["sql_evidence_available"] is True
    assert payload["kpis"]["terms_with_proven_zero_sqls"] == 1
    assert payload["kpis"]["terms_with_attribution_unavailable"] == 0


def test_attributed_sqls_are_summed(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_attr({"1\x00tms": {
        "attributed_sqls": 3, "sql_attribution_status": "attributed",
        "sql_attribution_source": "direct_query", "sql_ambiguity_reason": None,
        "sql_candidate_count": 3, "sql_contact_keys": []}}))
    assert _flagged()["kpis"]["sql_evidence"] == 3


def test_sql_metric_is_always_labelled_as_an_attribution_subset(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())
    assert _flagged()["kpis"]["sql_evidence_label"] == "Search-term-attributable SQLs"
    # And the UI never prints a naked "SQLs" header for it.
    render = _APP_JS.split("function renderFlaggedTable(")[1].split("\nfunction ")[0]
    assert "strict attribution subset" in render


def test_unavailable_and_proven_zero_are_counted_separately(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("a", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
        _g("b", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_attr({"1\x00a": {
        "attributed_sqls": 0, "sql_attribution_status": "known_zero",
        "sql_attribution_source": None, "sql_ambiguity_reason": None,
        "sql_candidate_count": 0, "sql_contact_keys": []}}))
    kpis = _flagged()["kpis"]
    assert kpis["terms_with_proven_zero_sqls"] == 1
    assert kpis["terms_with_attribution_unavailable"] == 1


def test_search_terms_only_attributes_the_canonical_sql_event():
    """The lifecycle SQL definition lives in the CRM funnel contract; this page
    must not redefine it."""
    assert "hs_v2_date_entered_salesqualifiedlead" not in _SERVICE_PY
    doc = (_ROOT / "docs" / "34_SEARCH_TERM_WASTE_CONSOLIDATION.md").read_text()
    assert "hs_v2_date_entered_salesqualifiedlead" in doc
    assert "does not define it" in doc


def test_frontend_renders_unavailable_attribution_as_dash(monkeypatch):
    fn = _APP_JS.split("function flagSqlCell(")[1].split("\nfunction ")[0]
    assert "known_zero" in fn and "attributed" in fn
    assert "not a proven zero" in fn
    assert 'detail-unavailable' in fn


# =============================================================================
# §43 — Flag / review state
# =============================================================================
def test_review_vocabulary_is_the_required_one():
    assert review.REVIEW_STATES == (
        "unreviewed", "keep", "monitor", "exclude_candidate", "resolved")


def test_unreviewed_flag_appears_in_the_flagged_view(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    assert row["review_state"] == "unreviewed"
    assert row["action_needed"] is True


def test_keep_and_resolved_remove_the_remaining_action(monkeypatch):
    for state in ("keep", "resolved"):
        agg = _agg([_g("tms", "Brand - UK", "1", spend=10.0, flagged=True,
                       junk="student")])
        ident = identity.term_identity_key("1", "tms")
        _patch(monkeypatch, agg, reviews={ident: {
            "term_identity": ident, "review_state": state,
            "first_flagged_at": "2026-06-01T00:00:00+00:00",
            "latest_flagged_at": "2026-07-02T00:00:00+00:00"}},
            sql_attr=_no_attribution())
        row = _flagged()["rows"][0]
        assert row["review_state"] == state
        assert row["action_needed"] is False, state


def test_monitor_remains_actionable(monkeypatch):
    agg = _agg([_g("tms", "Brand - UK", "1", spend=10.0, flagged=True,
                   junk="student")])
    ident = identity.term_identity_key("1", "tms")
    _patch(monkeypatch, agg, reviews={ident: {
        "term_identity": ident, "review_state": "monitor"}},
        sql_attr=_no_attribution())
    assert _flagged()["rows"][0]["action_needed"] is True


def test_exclude_candidate_is_local_only_and_says_so(monkeypatch):
    agg = _agg([_g("tms", "Brand - UK", "1", spend=10.0, flagged=True,
                   junk="student")])
    ident = identity.term_identity_key("1", "tms")
    _patch(monkeypatch, agg, reviews={ident: {
        "term_identity": ident, "review_state": "exclude_candidate"}},
        sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    assert row["review_state_label"] == "Exclude candidate"
    assert row["applied_to_google_ads"] is False
    assert "no write path to Google Ads" in row["review_state_help"]


@pytest.mark.parametrize("phrase", review.FORBIDDEN_APPLIED_PHRASES)
def test_no_surface_claims_a_google_ads_mutation_happened(phrase):
    """Scoped to the surfaces that render a LOCAL review decision.

    Whole-file matching would trip on unrelated, truthful copy elsewhere (a
    non-Google campaign label really is "excluded from Google Ads ROAS"). What
    §16 forbids is a local review state WORDED as a completed platform action,
    so the guard covers exactly the places that word one.
    """
    flagged_ui = _APP_JS.split("// ── Flagged tab (PR-ADS-153D)")[1]
    flagged_ui = flagged_ui.split("// ── Patterns tab ─")[0]
    review_endpoint = _SERVER_PY.split(
        '@app.post("/api/search-term-evidence/review")')[1].split("@app.")[0]
    review_module = (_ROOT / "analysis" / "search_term_review_state.py").read_text()
    queue_builder = _SERVER_PY.split(
        "def _build_waste_queue_items(")[1].split("\ndef ")[0]

    for text, label in ((flagged_ui, "flagged tab"),
                        (review_endpoint, "review endpoint"),
                        (queue_builder, "queue builder")):
        assert phrase not in text.lower(), f"{label}: {phrase!r}"
    # The review module names the phrases only to forbid them.
    assert phrase in review_module.lower()


def test_historical_flag_survives_resolution(monkeypatch):
    """A resolved term keeps its flag history — audit trail is not deleted."""
    agg = _agg([_g("tms", "Brand - UK", "1", spend=10.0, flagged=True,
                   junk="student")])
    ident = identity.term_identity_key("1", "tms")
    _patch(monkeypatch, agg, reviews={ident: {
        "term_identity": ident, "review_state": "resolved",
        "first_flagged_at": "2026-01-05T00:00:00+00:00",
        "latest_flagged_at": "2026-07-02T00:00:00+00:00"}},
        sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    assert row["first_flagged_at"] == "2026-01-05T00:00:00+00:00"
    assert row["latest_flagged_at"] == "2026-07-02T00:00:00+00:00"


def test_flag_history_writer_never_touches_a_human_decision():
    fn = _REVIEW_REPO_PY.split("def record_flag_observations(")[1].split("\ndef ")[0]
    # The ON CONFLICT update list must not include review_state.
    update = fn.split("DO UPDATE SET")[1]
    assert "review_state" not in update
    # History is monotonic, so re-observing changes nothing.
    assert "LEAST(" in update and "GREATEST(" in update


def test_unknown_flag_reason_surfaces_as_unmapped():
    classified = taxonomy.classify_reason("some_brand_new_rule")
    assert classified["reason"] == "unmapped"
    assert classified["unmapped"] is True
    # The raw evidence is preserved, not discarded.
    assert classified["raw_reason"] == "some_brand_new_rule"


def test_unmapped_is_distinct_from_other():
    assert taxonomy.classify_reason("other")["reason"] == "other"
    assert taxonomy.classify_reason("other")["unmapped"] is False
    assert taxonomy.classify_reason(None)["reason"] == "unmapped"


@pytest.mark.parametrize("raw,expected", [
    ("job_seeker", "job_seeker"),
    ("student", "consumer_b2c_intent"),
    ("free_intent_english", "low_commercial_intent"),
    ("free_intent_spanish", "low_commercial_intent"),
    ("free_intent_arabic", "low_commercial_intent"),
    ("shipper_intent", "wrong_product_service"),
    ("fraud_indicators", "irrelevant_intent"),
    ("informational", "low_commercial_intent"),
    ("informational_industry", "low_commercial_intent"),
])
def test_every_rules_file_category_is_mapped(raw, expected):
    assert taxonomy.classify_reason(raw)["reason"] == expected


def test_taxonomy_covers_every_category_the_rules_file_emits():
    import yaml
    rules = yaml.safe_load((_ROOT / "config" / "junk_patterns.yaml").read_text())
    for category in rules:
        if category == "safe_terms":     # an exclusion list, not a waste reason
            continue
        classified = taxonomy.classify_reason(category)
        assert not classified["unmapped"], (
            f"junk_patterns.yaml emits '{category}' but the taxonomy does not "
            "map it — it would render as Unmapped in production")


def test_frontend_badges_an_unmapped_reason(monkeypatch):
    fn = _APP_JS.split("function flagReasonCell(")[1].split("\nfunction ")[0]
    assert "flag_reason_unmapped" in fn
    assert "not in the shared taxonomy" in fn


def test_no_duplicate_frontend_reason_mapping():
    """The reason vocabulary lives server-side; the UI renders labels only."""
    for raw in ("job_seeker:", "free_intent_english", "shipper_intent",
                "informational_industry"):
        assert raw not in _APP_JS, raw


# =============================================================================
# §44 — Action Queue
# =============================================================================
def _queue(monkeypatch, agg, **kw):
    from api import server

    _patch(monkeypatch, agg, **kw)
    return server._build_waste_queue_items(None, 30, 100.0)  # noqa: SLF001


def test_one_durable_term_produces_one_queue_item(monkeypatch):
    items = _queue(monkeypatch, _agg([
        _g("freight jobs", "Brand - UK", "1", spend=200.0, rows=5,
           flagged=True, junk="job_seeker"),
    ]), sql_attr=_no_attribution())
    assert len(items) == 1
    assert items[0]["evidence"]["term_identity"] == identity.term_identity_key(
        "1", "freight jobs")


def test_repeated_sync_does_not_duplicate_a_queue_item(monkeypatch):
    """Five run snapshots of the same term still yield one item with one id."""
    agg = _agg([_g("freight jobs", "Brand - UK", "1", spend=200.0, rows=5,
                   flagged=True, junk="job_seeker")])
    waste = [{"search_term": "freight jobs", "campaign_name": "Brand - UK",
              "junk_category": "job_seeker", "matched_pattern": "jobs",
              "crm_junk_confirmed": 1, "run_date": f"2026-07-0{d}"}
             for d in range(1, 6)]
    first = _queue(monkeypatch, agg, waste_rows=waste, sql_attr=_no_attribution())
    second = _queue(monkeypatch, agg, waste_rows=waste, sql_attr=_no_attribution())
    assert len(first) == len(second) == 1
    assert first[0]["id"] == second[0]["id"]      # stable across runs


def test_resolved_term_is_not_reopened_by_a_repeated_observation(monkeypatch):
    agg = _agg([_g("freight jobs", "Brand - UK", "1", spend=200.0,
                   flagged=True, junk="job_seeker")])
    ident = identity.term_identity_key("1", "freight jobs")
    items = _queue(monkeypatch, agg, reviews={ident: {
        "term_identity": ident, "review_state": "resolved"}},
        sql_attr=_no_attribution())
    assert items == []


def test_queue_item_and_page_row_share_spend_and_reason(monkeypatch):
    agg = _agg([_g("freight jobs", "Brand - UK", "1", spend=200.0,
                   flagged=True, junk="job_seeker")])
    _patch(monkeypatch, agg, sql_attr=_no_attribution())
    row = _flagged()["rows"][0]

    from api import server
    item = server._build_waste_queue_items(None, 30, 100.0)[0]  # noqa: SLF001
    assert item["evidence"]["spend_usd"] == row["spend_usd"]
    assert item["evidence"]["flag_reason"] == row["flag_reason"]
    assert item["evidence"]["term_identity"] == row["term_identity"]


def test_queue_discloses_unavailable_attribution(monkeypatch):
    items = _queue(monkeypatch, _agg([
        _g("freight jobs", "Brand - UK", "1", spend=200.0, flagged=True,
           junk="job_seeker"),
    ]), sql_attr=_no_attribution())
    assert "not a proven zero" in items[0]["detail"]
    assert items[0]["evidence"]["sql_attribution_status"] == "unavailable"
    assert items[0]["evidence"]["applied_to_google_ads"] is False


def test_queue_never_reads_the_run_snapshot_table():
    fn = _SERVER_PY.split("def _build_waste_queue_items(")[1].split("\ndef ")[0]
    # Checked against the CODE — the docstring necessarily quotes the old SQL in
    # order to explain the defect it replaced.
    code = _strip_py_docstrings_and_comments(fn)
    # The builder issues no SQL of its own at all — it calls the canonical
    # service. (`waste_terms` still appears as a provenance LABEL on the item,
    # which is a truthful description of the annotation source, not a read.)
    assert "cur.execute" not in code
    assert "FROM waste_terms" not in code
    assert "SUM(spend_usd)" not in code
    assert "build_flagged_search_terms" in code


def test_priority_is_explainable_and_never_an_ai_score(monkeypatch):
    items = _queue(monkeypatch, _agg([
        _g("freight jobs", "Brand - UK", "1", spend=200.0, rows=4,
           flagged=True, junk="job_seeker"),
    ]), sql_attr=_no_attribution())
    reasons = items[0]["evidence"]["priority_reasons"]
    codes = {r["code"] for r in reasons}
    assert "high_spend" in codes
    assert "clear_disqualifying_intent" in codes
    assert "never_reviewed" in codes
    # Every component states its own points and a human-readable detail.
    for r in reasons:
        assert isinstance(r["points"], int) and r["detail"]
    for banned in ("ai_score", "ml_score", "propensity", "confidence_model"):
        assert banned not in _SERVICE_PY.lower()


def test_unavailable_attribution_never_raises_priority(monkeypatch):
    """§13 — an unknown must not be laundered into a waste signal."""
    from services.search_term_evidence_service import _flagged_priority

    unit = {"spend_usd": 10.0, "junk_categories": ["informational"],
            "row_count": 1, "sql_attribution_status": "unavailable"}
    unavailable = _flagged_priority(unit, "unreviewed", 100.0)
    proven = _flagged_priority({**unit, "sql_attribution_status": "known_zero"},
                               "unreviewed", 100.0)
    assert proven["priority_score"] > unavailable["priority_score"]
    assert not any(r["code"] == "proven_zero_qualified_outcome"
                   for r in unavailable["priority_reasons"])


# =============================================================================
# §45 — Windows
# =============================================================================
def test_flagged_view_uses_the_evidence_window_vocabulary(monkeypatch):
    from analysis.evidence_windows import EvidenceWindowError

    _patch(monkeypatch, _agg([_g("t", "Brand - UK", "1", flagged=True,
                                 junk="student")]), sql_attr=_no_attribution())
    for window in ("7d", "14d", "30d", "60d", "180d", "all_time"):
        assert _flagged(window)["window"] == window
    with pytest.raises(EvidenceWindowError):
        _flagged("90d")


def test_flagged_tab_uses_the_pages_own_window():
    fn = _APP_JS.split("function flagBuildParams(")[1].split("\n}")[0]
    assert 'p.set("window", getEvidenceWindow())' in fn


def test_no_waste_specific_window_resolver_remains():
    evidence_block = _APP_JS.split("const EVIDENCE_PAGES")[1][:200]
    assert '"waste"' not in evidence_block
    assert "_wasteMeta" not in _APP_JS
    assert "_wasteData" not in _APP_JS


def test_action_queue_snaps_to_a_canonical_evidence_window():
    from api.server import _evidence_window_for_days

    assert _evidence_window_for_days(7) == "7d"
    assert _evidence_window_for_days(30) == "30d"
    # Never narrower than requested — that direction cannot under-report.
    assert _evidence_window_for_days(31) == "60d"
    assert _evidence_window_for_days(365) == "all_time"


def test_all_time_does_not_claim_completeness(monkeypatch):
    _patch(monkeypatch, _agg([_g("t", "Brand - UK", "1", flagged=True,
                                 junk="student")]), sql_attr=_no_attribution())
    payload = _flagged("all_time")
    assert payload["all_time"] is True
    # Coverage/truth are still disclosed rather than assumed complete.
    assert payload["truth_state"]["status"] in ("partial", "reconciled", "mismatch")


# =============================================================================
# §46 — Privacy / governance
# =============================================================================
def test_no_email_or_contact_pii_in_the_flagged_contract(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    for key in row:
        assert "email" not in key.lower()
        assert "phone" not in key.lower()


def test_flagged_frontend_never_renders_an_email_field():
    module = _APP_JS.split("// ── Flagged tab (PR-ADS-153D)")[1]
    module = module.split("// ── Patterns tab ─")[0].lower()
    for accessor in (".email", '"email"', "'email'"):
        assert accessor not in module


def test_governance_block_states_the_read_only_contract(monkeypatch):
    _patch(monkeypatch, _agg([_g("t", "Brand - UK", "1", flagged=True,
                                 junk="student")]), sql_attr=_no_attribution())
    gov = _flagged()["governance"]
    assert gov["read_only"] is True
    assert gov["google_ads_mutations"] is False
    assert gov["negative_keywords_applied"] is False


def test_no_google_ads_mutation_path_anywhere_in_the_touched_backend():
    banned = ("add_negative", "negative_keyword_operation", "campaign_operation",
              "ad_group_criterion_operation", ".mutate(", "upload_conversions",
              "upload_offline")
    for text, label in ((_SERVICE_PY, "service"), (_REVIEW_REPO_PY, "review repo")):
        lowered = text.lower()
        for token in banned:
            assert token.lower() not in lowered, f"{label}: {token}"


def test_no_mailchimp_work_in_this_pr():
    for text, label in ((_SERVICE_PY, "service"), (_REVIEW_REPO_PY, "review repo"),
                        ((_ROOT / "analysis" / "search_term_identity.py").read_text(), "identity"),
                        ((_ROOT / "analysis" / "waste_reason_taxonomy.py").read_text(), "taxonomy")):
        assert "mailchimp" not in text.lower(), label


def test_review_table_holds_no_platform_metric():
    """The decision layer must never become a second Google Ads fact ledger."""
    ddl = _SCHEMA_PY.split("CREATE TABLE IF NOT EXISTS search_term_review (")[1]
    ddl = ddl.split(");")[0].lower()
    for metric in ("spend", "clicks", "impressions", "conversions", "cost"):
        assert metric not in ddl, metric


def test_review_repository_writes_only_its_own_table():
    # `DO UPDATE SET` is part of an upsert onto the same table, not a second
    # write target, so match only statement-leading write verbs.
    statements = re.findall(
        r"(?:INSERT INTO|DELETE FROM|(?<!DO )UPDATE)\s+\{?(\w+)",
        _REVIEW_REPO_PY)
    assert statements, "expected at least one write statement"
    for table in statements:
        assert table in ("REVIEW_TABLE", "search_term_review"), table


# =============================================================================
# §30 — API strategy
# =============================================================================
def test_no_parallel_analytics_endpoint_was_created():
    for banned in ("/api/waste-v2", "/api/search-terms-new", "/api/flagged-waste"):
        assert banned not in _SERVER_PY, banned


def test_flagged_endpoint_lives_in_the_canonical_family():
    assert '@app.get("/api/search-term-evidence/flagged")' in _SERVER_PY
    assert '@app.post("/api/search-term-evidence/review")' in _SERVER_PY


def test_legacy_waste_route_is_a_documented_compatibility_adapter():
    fn = _SERVER_PY.split('@app.get("/api/waste")')[1].split("@app.")[0]
    assert "COMPATIBILITY ADAPTER" in fn
    assert "PR-ADS-153G" in fn
    # It reads the canonical facts, not the run-snapshot table.
    assert "build_flagged_search_terms" in fn
    assert "FROM waste_terms" not in fn


def test_retirement_manifest_documents_every_touched_legacy_route():
    doc = (_ROOT / "docs" / "34_SEARCH_TERM_WASTE_CONSOLIDATION.md").read_text()
    for route in ("/api/waste", "/api/search-terms", "/api/search-terms/summary",
                  "/api/search-terms/ngrams"):
        assert route in doc, route
    assert "Delete in 153G?" in doc
    # And the touched tables are audited with their grain and status.
    for table in ("search_terms", "waste_terms", "search_term_review"):
        assert table in doc, table


# =============================================================================
# Review feedback (PR #156) — each fix pinned by a test
# =============================================================================
def test_deep_link_term_is_re_encoded_into_the_hash():
    """URLSearchParams.get() returns a DECODED value, so writing it back raw
    would break a term containing a space, "&" or "%" — or inject a second
    query param."""
    fn = _APP_JS.split("function navigateToHash(")[1].split("\n}")[0]
    assert "encodeURIComponent(term)" in fn
    assert "&term=${term}`" not in fn


def test_focus_term_is_not_decoded_twice():
    """A second decodeURIComponent throws URIError on a legitimate term such as
    "100%", which would break the whole page load, not just the filter."""
    fn = _APP_JS.split("function loadSearchTermsEvidence(")[1].split("\n}")[0]
    assert "decodeURIComponent" not in fn
    assert "_flagFocusTerm = focusTerm;" in fn


def test_last_seen_sorts_most_recent_first(monkeypatch):
    """Same direction as the Terms tab — "Last seen" must not mean opposite
    things on two tabs of one page."""
    _patch(monkeypatch, _agg([
        _g("old", "Brand - UK", "1", spend=10.0, flagged=True, junk="student",
           last=date(2026, 7, 1)),
        _g("new", "Brand - UK", "1", spend=10.0, flagged=True, junk="student",
           last=date(2026, 7, 20)),
    ]), sql_attr=_no_attribution())
    rows = _flagged(sort="last_seen")["rows"]
    assert [r["search_term"] for r in rows] == ["new", "old"]


def test_last_seen_sort_puts_unknown_dates_last(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("known", "Brand - UK", "1", spend=10.0, flagged=True, junk="student",
           last=date(2026, 7, 1)),
        _g("unknown", "Brand - UK", "1", spend=10.0, flagged=True,
           junk="student", last=None),
    ]), sql_attr=_no_attribution())
    rows = _flagged(sort="last_seen")["rows"]
    assert rows[-1]["search_term"] == "unknown"


def test_review_endpoint_requires_a_campaign_key():
    """campaign_key is HALF the durable identity. Without it the decision would
    normalise to `unknown_campaign` and merge across every campaign that ever
    triggered the query."""
    from fastapi import HTTPException

    from api import server

    for missing in (None, "", "   "):
        with pytest.raises(HTTPException) as exc:
            server.api_search_term_evidence_review(
                payload={"search_term": "tms", "review_state": "keep",
                         "campaign_key": missing},
                user={"email": "x"})
        assert exc.value.status_code == 400
        assert "campaign_key" in str(exc.value.detail)


def test_review_endpoint_still_rejects_bad_state_and_missing_term():
    from fastapi import HTTPException

    from api import server

    with pytest.raises(HTTPException) as exc:
        server.api_search_term_evidence_review(
            payload={"campaign_key": "1", "review_state": "keep"},
            user={"email": "x"})
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        server.api_search_term_evidence_review(
            payload={"campaign_key": "1", "search_term": "tms",
                     "review_state": "deleted_from_google"},
            user={"email": "x"})
    assert exc.value.status_code == 400


def test_review_store_outage_is_reported_as_unavailable(monkeypatch):
    """An empty review map is ambiguous on its own — it means both "nobody has
    reviewed anything" and "the store could not be read". Reporting the outage
    as available would present it as a verified all-unreviewed state."""
    import db.search_term_review_repository as review_repo

    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())
    monkeypatch.setattr(review_repo, "fetch_reviews_for_identities",
                        lambda ids: {"available": False, "rows": {}})

    payload = _flagged()
    assert payload["review_state_available"] is False
    # PR #156 merge-blocker 3: an outage is NOT "unreviewed". Both the state and
    # the action verdict are null, because "we could not read the store" is
    # exactly what we do not know.
    assert payload["rows"][0]["review_state"] is None
    assert payload["rows"][0]["review_state_status"] == "unavailable"
    assert payload["rows"][0]["action_needed"] is None


def test_review_store_available_is_reported_when_it_is(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())
    assert _flagged()["review_state_available"] is True


# =============================================================================
# Merge-blocker 1 (PR #156) — aggregation grain MUST equal the identity grain
# =============================================================================
# Before: _build_population grouped on the RAW search term while the durable
# identity normalized case and whitespace. "Freight JOBS" and "freight jobs"
# became two rows sharing ONE identity, so the table showed 2 rows and
# pagination said 2, the KPI counted 1 term, and the Action Queue kept a single
# item carrying only one variant's spend ($20 of $30). Every surface disagreed.
#
# After: units merge on (normalize_search_term, campaign_key) — the identity
# grain itself — and every raw variant is preserved as display evidence.

def _flagged_variant_case():
    return _agg([
        _g("Freight JOBS", "Brand - UK", "1", spend=10.0, clicks=1,
           impressions=10, flagged=True, junk="job_seeker"),
        _g("freight jobs", "Brand - UK", "1", spend=20.0, clicks=2,
           impressions=20, flagged=True, junk="job_seeker"),
    ])


def test_case_variants_reconcile_across_every_surface(monkeypatch):
    """The exact reported reproduction, asserted end to end."""
    from api import server

    _patch(monkeypatch, _flagged_variant_case(), sql_attr=_no_attribution())
    payload = _flagged()

    assert len(payload["rows"]) == 1
    assert payload["pagination"]["total_count"] == 1
    assert payload["kpis"]["flagged_terms"] == 1
    assert payload["kpis"]["flagged_spend_usd"] == 30.0
    assert payload["rows"][0]["spend_usd"] == 30.0

    items = server._build_waste_queue_items(None, 30, 100.0)  # noqa: SLF001
    assert len(items) == 1
    assert items[0]["evidence"]["spend_usd"] == 30.0
    assert items[0]["evidence"]["term_identity"] == payload["rows"][0]["term_identity"]


def test_row_count_always_equals_identity_count(monkeypatch):
    """The hard invariant, stated as the spec states it."""
    _patch(monkeypatch, _flagged_variant_case(), sql_attr=_no_attribution())
    rows = _flagged()["rows"]
    assert len(rows) == len({row["term_identity"] for row in rows})


def test_identity_divergence_raises_instead_of_serving(monkeypatch):
    """If the grain ever drifts again, the page must fail loudly rather than
    publish rows, KPIs and queue items that contradict each other."""
    import services.search_term_evidence_service as svc

    _patch(monkeypatch, _flagged_variant_case(), sql_attr=_no_attribution())
    # Force the old (raw-term) grain back.
    monkeypatch.setattr(svc, "normalize_search_term", lambda v: str(v or ""))
    with pytest.raises(svc.SearchTermIdentityError):
        _flagged()


def test_variants_aggregate_clicks_and_impressions(monkeypatch):
    _patch(monkeypatch, _flagged_variant_case(), sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    assert row["clicks"] == 3
    assert row["impressions"] == 30


def test_repeated_whitespace_variants_merge(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("freight  jobs", "Brand - UK", "1", spend=10.0, flagged=True,
           junk="job_seeker"),
        _g("freight jobs", "Brand - UK", "1", spend=20.0, flagged=True,
           junk="job_seeker"),
        _g(" freight jobs ", "Brand - UK", "1", spend=5.0, flagged=True,
           junk="job_seeker"),
    ]), sql_attr=_no_attribution())
    payload = _flagged()
    assert len(payload["rows"]) == 1
    assert payload["kpis"]["flagged_terms"] == 1
    assert payload["rows"][0]["spend_usd"] == 35.0


def test_unicode_nfkc_variants_merge(monkeypatch):
    """NFKC-equivalent forms are the same query — the identity says so, so the
    aggregation must too. U+FF46 FULLWIDTH LATIN SMALL LETTER F normalizes to
    an ASCII "f"."""
    assert identity.normalize_search_term("ｆreight jobs") == "freight jobs"
    _patch(monkeypatch, _agg([
        _g("ｆreight jobs", "Brand - UK", "1", spend=10.0, flagged=True,
           junk="job_seeker"),
        _g("freight jobs", "Brand - UK", "1", spend=20.0, flagged=True,
           junk="job_seeker"),
    ]), sql_attr=_no_attribution())
    payload = _flagged()
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["spend_usd"] == 30.0


def test_punctuation_variants_stay_distinct(monkeypatch):
    """Punctuation is NOT normalized — Google Ads reports these separately and a
    reviewer may judge them differently, so merging would destroy a real fact."""
    _patch(monkeypatch, _agg([
        _g("logistics software", "Brand - UK", "1", spend=10.0, flagged=True,
           junk="student"),
        _g("logistics-software", "Brand - UK", "1", spend=20.0, flagged=True,
           junk="student"),
    ]), sql_attr=_no_attribution())
    payload = _flagged()
    assert len(payload["rows"]) == 2
    assert payload["kpis"]["flagged_terms"] == 2
    assert len({r["term_identity"] for r in payload["rows"]}) == 2


def test_same_normalized_term_in_two_campaigns_stays_two_units(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("Freight JOBS", "Brand - UK", "1", spend=10.0, flagged=True,
           junk="job_seeker"),
        _g("freight jobs", "Gulf", "2", spend=20.0, flagged=True,
           junk="job_seeker"),
    ]), sql_attr=_no_attribution())
    payload = _flagged()
    assert len(payload["rows"]) == 2
    assert payload["kpis"]["flagged_terms"] == 2
    by_campaign = {r["campaign_name"]: r["spend_usd"] for r in payload["rows"]}
    assert by_campaign["Brand - UK"] == 10.0
    assert by_campaign["Gulf"] == 20.0


def test_raw_variants_are_preserved_as_display_evidence(monkeypatch):
    """Nothing is silently collapsed — the merged variants stay visible."""
    _patch(monkeypatch, _flagged_variant_case(), sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    assert row["search_term_normalized"] == "freight jobs"
    # The display label is deterministic, and the other casing rides along.
    assert row["search_term"] == "freight jobs"
    assert "Freight JOBS" in row["search_term_variants"]


def test_representative_term_is_deterministic(monkeypatch):
    """No variant equals the normalized form → lexicographically first wins,
    so the same population always renders the same label."""
    _patch(monkeypatch, _agg([
        _g("Freight JOBS", "Brand - UK", "1", spend=10.0, flagged=True,
           junk="job_seeker"),
        _g("FREIGHT Jobs", "Brand - UK", "1", spend=20.0, flagged=True,
           junk="job_seeker"),
    ]), sql_attr=_no_attribution())
    first = _flagged()["rows"][0]
    second = _flagged()["rows"][0]
    assert first["search_term"] == second["search_term"] == "FREIGHT Jobs"
    assert first["search_term_normalized"] == "freight jobs"


def test_free_text_filter_finds_a_merged_unit_by_any_raw_variant(monkeypatch):
    _patch(monkeypatch, _flagged_variant_case(), sql_attr=_no_attribution())
    assert len(_flagged(q="Freight JOBS")["rows"]) == 1
    assert len(_flagged(q="freight jobs")["rows"]) == 1


def test_waste_annotation_joins_across_a_casing_difference(monkeypatch):
    """An annotation written against one casing must reach the merged unit."""
    _patch(monkeypatch, _agg([
        _g("Freight JOBS", "Brand - UK", "1", spend=10.0, unreviewed=True),
    ]), waste_rows=[
        {"search_term": "freight jobs", "campaign_name": "Brand - UK",
         "junk_category": "job_seeker", "matched_pattern": "jobs",
         "crm_junk_confirmed": 1, "run_date": "2026-07-01"},
    ], sql_attr=_no_attribution())
    payload = _flagged()
    assert payload["kpis"]["flagged_terms"] == 1
    assert payload["rows"][0]["flag_reason"] == "job_seeker"
    assert (payload["annotation_join"]["legacy_unresolved"] or 0) == 0


def test_action_queue_refuses_to_silently_drop_a_duplicate_identity(monkeypatch):
    """The queue must never quietly skip a repeat — that is how a variant's
    spend went missing. A duplicate is an invariant break and raises."""
    import services.search_term_evidence_service as svc
    from api import server

    _patch(monkeypatch, _flagged_variant_case(), sql_attr=_no_attribution())
    real = svc.build_flagged_search_terms          # capture BEFORE patching

    def _duplicated(window, **kw):
        payload = real(window, **kw)
        payload["rows"] = payload["rows"] + payload["rows"]   # forced break
        return payload

    monkeypatch.setattr(svc, "build_flagged_search_terms", _duplicated)
    with pytest.raises(RuntimeError, match="duplicate durable"):
        server._build_waste_queue_items(None, 30, 100.0)  # noqa: SLF001


# =============================================================================
# Merge-blocker 2 (PR #156) — mismatch data must never render as trustworthy
# =============================================================================
# Before: truth_state = "mismatch" returned populated KPIs and rows, and the UI
# printed a warning that counts were withheld immediately above the counts.
# After: the payload is quarantined — null KPIs, no rows, no facets, filters and
# review actions disabled, and a separate diagnostic block the normal renderers
# cannot consume.

def _mismatch_population():
    """A durably flagged term carrying NO reason evidence — the invariant break
    the flagged contract is defined to reject."""
    return _agg([
        _g("mystery term", "Brand - UK", "1", spend=250.0, clicks=9,
           flagged=True, junk=None),
    ])


def test_flagged_with_no_reason_evidence_reports_mismatch(monkeypatch):
    _patch(monkeypatch, _mismatch_population(), sql_attr=_no_attribution())
    payload = _flagged()
    assert payload["truth_state"]["status"] == "mismatch"
    assert any(r.startswith("flagged_without_reason_evidence")
               for r in payload["truth_state"]["reasons"])


def test_mismatch_exposes_no_kpi_row_or_action_count(monkeypatch):
    from api import server

    _patch(monkeypatch, _mismatch_population(), sql_attr=_no_attribution())
    payload = _flagged()

    assert payload["actionable"] is False
    assert payload["rows"] == []
    assert payload["pagination"]["total_count"] is None
    assert payload["pagination"]["returned_count"] == 0
    for key, value in payload["kpis"].items():
        # Descriptive labels/status strings are not decision metrics.
        if key in ("sql_evidence_label", "reporting_currency",
                   "review_state_status"):
            continue
        assert value in (None, False), f"{key} leaked a value on mismatch: {value!r}"

    # And no action is manufactured from it.
    assert server._build_waste_queue_items(None, 30, 100.0) == []  # noqa: SLF001


def test_mismatch_disables_filters_pagination_and_review_actions(monkeypatch):
    _patch(monkeypatch, _mismatch_population(), sql_attr=_no_attribution())
    payload = _flagged()
    assert payload["filters_enabled"] is False
    assert payload["review_actions_enabled"] is False
    assert payload["facets"]["campaigns"] == []
    assert payload["facets"]["flag_reasons"] == []
    assert payload["pagination"]["has_more"] is False


def test_mismatch_carries_a_separate_diagnostic_block(monkeypatch):
    """Diagnosis must be possible without the normal UI being able to render
    it as decision data."""
    _patch(monkeypatch, _mismatch_population(), sql_attr=_no_attribution())
    q = _flagged()["quarantine"]
    assert q["reason"] == "internal_invariant_failed"
    assert q["affected_term_count"] == 1
    assert "mystery term" in q["affected_terms_sample"]
    # The sample is plain strings, not row objects the table renderer accepts.
    assert all(isinstance(t, str) for t in q["affected_terms_sample"])


def test_mismatch_filters_cannot_be_used_to_page_around_the_quarantine(monkeypatch):
    _patch(monkeypatch, _mismatch_population(), sql_attr=_no_attribution())
    for kwargs in ({"q": "mystery"}, {"page": 2}, {"sort": "spend"},
                   {"min_spend": 1}, {"review_state": "unreviewed"}):
        payload = _flagged(**kwargs)
        assert payload["rows"] == [], kwargs
        assert payload["kpis"]["flagged_terms"] is None, kwargs


def test_legacy_waste_adapter_also_withholds_mismatch_data(monkeypatch):
    from api import server

    _patch(monkeypatch, _mismatch_population(), sql_attr=_no_attribution())
    out = server.api_waste(user={"e": "x"}, days=30, window="30d")
    assert out["waste"] == []
    assert out["total_count"] == 0
    assert out["truth_state"]["status"] == "mismatch"
    assert out["quarantine"]["reason"] == "internal_invariant_failed"


def test_frontend_returns_before_writing_kpi_or_table_elements():
    """Structural, not cosmetic: the quarantine branch returns BEFORE the KPI
    grid and table shell exist, so there is no element for the table renderer to
    populate — the warning can never sit on top of the numbers."""
    fn = _APP_JS.split("function renderFlaggedTab(")[1].split("\nfunction ")[0]
    quarantine_at = fn.index("_flagData.actionable === false")
    kpis_at = fn.index("renderFlaggedKPIs(")
    table_at = fn.index('id="st-flagged-table"')
    assert quarantine_at < kpis_at < table_at
    branch = fn[quarantine_at:fn.index("body.innerHTML = `\n    ${flagTruthNotice")]
    assert "return;" in branch

    quarantine = _APP_JS.split("function renderFlaggedQuarantine(")[1].split("\nfunction ")[0]
    for banned in ("dash-kpi-card", "st-flagged-table", "renderFlaggedKPIs",
                   "renderFlaggedFilters", "data-flag-review"):
        assert banned not in quarantine, banned


def test_healthy_payload_is_marked_actionable(monkeypatch):
    _patch(monkeypatch, _agg([
        _g("tms", "Brand - UK", "1", spend=10.0, flagged=True, junk="student"),
    ]), sql_attr=_no_attribution())
    payload = _flagged()
    assert payload["actionable"] is True
    assert payload["filters_enabled"] is True
    assert payload["review_actions_enabled"] is True
    assert payload["truth_state"]["status"] != "mismatch"


# =============================================================================
# Merge-blocker 3 (PR #156) — a review-store outage is not "unreviewed"
# =============================================================================
# Before: an unreadable search_term_review defaulted every row to `unreviewed`,
# which added the never_reviewed priority component and reintroduced resolved
# and kept terms into the Action Queue. An outage silently undid human
# decisions.
# After: review_state is null with an explicit `unavailable` status,
# action_needed is null, Review Needed is withheld, review controls are
# disabled, and the queue DISCLOSES the outage instead of returning an empty
# list that reads as "no work".

def _outage(monkeypatch, agg, **kw):
    import db.search_term_review_repository as review_repo

    _patch(monkeypatch, agg, **kw)
    monkeypatch.setattr(review_repo, "fetch_reviews_for_identities",
                        lambda ids: {"available": False, "rows": {},
                                     "reason": "database_unavailable"})


def _flagged_one():
    return _agg([_g("freight jobs", "Brand - UK", "1", spend=200.0,
                    flagged=True, junk="job_seeker")])


def test_outage_reports_null_state_not_unreviewed(monkeypatch):
    _outage(monkeypatch, _flagged_one(), sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    assert row["review_state"] is None
    assert row["review_state_status"] == "unavailable"
    assert row["review_state_label"] == "Review state unavailable"
    assert row["action_needed"] is None
    assert row["requires_action"] is None
    assert row["is_decided"] is None


def test_outage_withholds_review_needed_rather_than_reporting_zero(monkeypatch):
    _outage(monkeypatch, _flagged_one(), sql_attr=_no_attribution())
    kpis = _flagged()["kpis"]
    assert kpis["review_needed"] is None
    assert kpis["review_state_status"] == "unavailable"


def test_outage_does_not_add_the_never_reviewed_priority_component(monkeypatch):
    """Scoring an unknown as "never reviewed" would promote terms a human had
    already dealt with."""
    _outage(monkeypatch, _flagged_one(), sql_attr=_no_attribution())
    row = _flagged()["rows"][0]
    codes = {r["code"] for r in row["priority_reasons"]}
    assert "never_reviewed" not in codes


def test_outage_disables_review_actions_and_the_review_filter(monkeypatch):
    from services.search_term_evidence_service import SearchTermQueryError

    _outage(monkeypatch, _flagged_one(), sql_attr=_no_attribution())
    payload = _flagged()
    assert payload["review_actions_enabled"] is False
    assert payload["review_state_available"] is False
    # Filtering would return an empty list that reads as a real "no terms in
    # that state" — refuse it instead.
    with pytest.raises(SearchTermQueryError, match="review_state filter"):
        _flagged(review_state="unreviewed")


def test_outage_queue_discloses_rather_than_returning_an_empty_list(monkeypatch):
    """An empty waste-action list would be read as zero outstanding work."""
    from api import server

    _outage(monkeypatch, _flagged_one(), sql_attr=_no_attribution())
    items = server._build_waste_queue_items(None, 30, 100.0)  # noqa: SLF001
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "waste-review-unavailable"
    assert item["evidence"]["review_state_status"] == "unavailable"
    assert item["evidence"]["actions_available"] is False
    assert "NOT zero outstanding work" in item["detail"]
    # It is a disclosure, not an action against a term.
    assert item["entity_type"] == "review_store"


def test_outage_never_reopens_a_resolved_or_kept_term(monkeypatch):
    """The whole point: an unreadable store must not resurrect decided terms as
    actionable queue items."""
    from api import server

    _outage(monkeypatch, _flagged_one(), sql_attr=_no_attribution())
    items = server._build_waste_queue_items(None, 30, 100.0)  # noqa: SLF001
    # Exactly one disclosure item, and no per-term action for anything.
    assert [i["entity_type"] for i in items] == ["review_store"]
    assert not any(i["evidence"].get("term_identity") for i in items)


def test_frontend_says_unavailable_not_unreviewed():
    fn = _APP_JS.split("function flagReviewCell(")[1].split("\nfunction ")[0]
    assert '"unavailable"' in fn
    assert "Review state unavailable" in fn
    # It must not fall through to the Unreviewed label on a null state.
    assert "review_state === null" in fn


def test_frontend_disables_review_controls_during_an_outage():
    filters = _APP_JS.split("function renderFlaggedFilters(")[1].split("\nfunction ")[0]
    assert "review_state_available === false" in filters
    assert "disabled" in filters

    drawer = _APP_JS.split("function openFlaggedDrawer(")[1].split("\nasync function ")[0]
    assert "review_actions_enabled === false" in drawer
    assert "Review actions are unavailable" in drawer

    notice = _APP_JS.split("function flagReviewOutageNotice(")[1].split("\nfunction ")[0]
    assert "not the same as" in notice


def test_review_state_payload_never_collapses_an_outage_into_a_state():
    available = review.review_state_payload("resolved")
    outage = review.review_state_payload("resolved", available=False)
    assert available["review_state"] == "resolved"
    assert outage["review_state"] is None
    assert outage["review_state_status"] == "unavailable"
    assert outage["requires_action"] is None
    # And it still never implies a platform change.
    assert outage["applied_to_google_ads"] is False


# =============================================================================
# Merge-blocker 4 (PR #156) — flag history must be WIRED, not merely writable
# =============================================================================
_WEEKLY_PY = (_ROOT / "scheduler" / "weekly.py").read_text()
_HISTORY_SERVICE_PY = (
    _ROOT / "services" / "search_term_flag_history_service.py").read_text()


def test_flag_history_is_called_from_the_weekly_analysis_run():
    """Before this fix record_flag_observations had no caller outside tests, so
    a real deployment left every history column NULL forever."""
    assert "record_flag_history" in _WEEKLY_PY
    assert "search_term_flag_history_service" in _WEEKLY_PY
    # It runs AFTER the annotations land, so it reads a population that already
    # reflects this run's classification.
    assert (_WEEKLY_PY.index("write_waste_terms")
            < _WEEKLY_PY.index("record_flag_history"))


def test_flag_history_is_never_written_from_a_get_endpoint():
    """A read path must not mutate the durable audit table."""
    for name in ("record_flag_history", "backfill_flag_history",
                 "record_flag_observations"):
        assert name not in _SERVICE_PY, f"{name} reachable from the read service"
    # And no GET handler calls it.
    for block in _SERVER_PY.split("@app.get(")[1:]:
        handler = block.split("@app.")[0]
        assert "record_flag_history" not in handler
        assert "record_flag_observations" not in handler


def test_flag_history_service_writes_only_local_state():
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", _HISTORY_SERVICE_PY,
                         re.M)
    for module in imports:
        assert not module.startswith("connectors"), module
        assert not any(p in module.lower()
                       for p in ("google", "hubspot", "mailchimp")), module
    for call in ("mutate", "requests.", "httpx.", "add_negative"):
        assert call not in _HISTORY_SERVICE_PY.lower(), call


def test_flag_history_dates_observations_by_evidence_not_wall_clock():
    """flagged_at is the term's last_seen SOURCE DATE, which is what makes a
    backfill truthful and replays deterministic."""
    fn = _HISTORY_SERVICE_PY.split("def _observations_from_rows(")[1].split("\ndef ")[0]
    assert '"flagged_at": row.get("last_seen")' in fn
    assert "datetime.now" not in fn and "utcnow" not in fn


def test_flag_history_skips_rows_without_a_full_durable_identity():
    fn = _HISTORY_SERVICE_PY.split("def _observations_from_rows(")[1].split("\ndef ")[0]
    assert "if not term or not campaign_key:" in fn
    assert "continue" in fn
