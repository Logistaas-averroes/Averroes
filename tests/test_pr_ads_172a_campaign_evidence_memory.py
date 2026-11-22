"""
tests/test_pr_ads_172a_campaign_evidence_memory.py

PR-ADS-172A — the Campaign Evidence drawer sections must not hold a fact table
in Python to show ten rows.

The daily in-app certification runs `scripts.audit_campaign_evidence_certification`
in a child process under a resident-memory budget with a 320 MB address-space
backstop (PR-ADS-169A). On 2026-11-21 and 2026-11-22 that audit was killed /
stopped at the backstop, so the platform read NOT CERTIFIED for a memory reason.
The same builders run inside the web server for `/api/campaign-detail`.

What was hungry (measured, `docs/51_*`):

  * `build_campaign_flagged_preview` built EVERY term × campaign unit of the
    account for the window — every grouped `search_terms` row as a dict, then
    every (term, campaign, day) cost row as a dict — to publish the flagged
    terms of one campaign;
  * `build_campaign_keyword_preview` read every (criterion, day) cost row of
    the account as a dict carrying two provenance lists nothing reads.

Three kinds of proof here:

  1. PURE — the folded per-date read carries exactly the amounts the per-day
     rows carried, and the builders ask for the narrow reads.
  2. EQUIVALENCE, on a real PostgreSQL — the narrowed flagged population
     publishes the SAME flagged view as the complete one, value for value, on a
     fixture built from the cases where narrowing could go wrong (a unit made of
     two spellings of which one is flagged; one term in two campaigns; an
     annotation that is ambiguous, unplaceable, or outside the window).
  3. MEASURED, on a production-SCALE synthetic PostgreSQL dataset (row counts
     of the live tables) — each section's peak resident growth is bounded, and
     the real audit, run by the real capped runner, holds under the configured
     backstop. These fail against the pre-fix code.

Read-only against every external platform; the only writes are local seeds.

Run with:
    python -m pytest tests/test_pr_ads_172a_campaign_evidence_memory.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import db.keyword_repository as kw_repo  # noqa: E402
import db.search_term_repository as st_repo  # noqa: E402
import services.keyword_evidence_service as kw_svc  # noqa: E402
import services.search_term_evidence_service as st_svc  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, _PgCluster, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

ACCOUNT = "5551234567"


# ═════════════════════════════════════════════════════════════════════════════
# 1 · PURE — the folded read is the per-day rows, and the builders ask for it
# ═════════════════════════════════════════════════════════════════════════════

def _legacy_keyword_fold(rows: list[dict]) -> dict:
    """The loop `keyword_evidence_service._build_population` has always run
    over per-day rows, verbatim. The reference the fold must equal."""
    out: dict = {}
    for d in rows:
        grain = kw_svc._criterion_grain(d)
        micros = d.get("cost_micros")
        if micros is None:
            continue
        iso = kw_svc._iso_str(d.get("source_date"))
        out.setdefault(grain, {})[iso] = out.get(grain, {}).get(iso, 0) + int(micros)
    return out


def test_01_the_keyword_fold_keeps_exactly_the_amounts_the_per_day_rows_carried():
    d1, d2 = date(2026, 11, 1), date(2026, 11, 2)
    rows = [
        {"customer_id": "1", "campaign_id": "10", "ad_group_id": "100",
         "criterion_id": "1000", "source_date": d1, "cost_micros": 1_500_000},
        {"customer_id": "1", "campaign_id": "10", "ad_group_id": "100",
         "criterion_id": "1000", "source_date": d2, "cost_micros": 0},
        # A NULL cost is unavailable, not zero: it contributes no day at all.
        {"customer_id": "1", "campaign_id": "10", "ad_group_id": "100",
         "criterion_id": "1001", "source_date": d1, "cost_micros": None},
        # The same (criterion, day) twice still sums, as the loop did.
        {"customer_id": "1", "campaign_id": "11", "ad_group_id": "100",
         "criterion_id": "1000", "source_date": d1, "cost_micros": 7},
        {"customer_id": "1", "campaign_id": "11", "ad_group_id": "100",
         "criterion_id": "1000", "source_date": d1, "cost_micros": 5},
        # Ids arrive as text or as numbers; a missing id is the '' grain.
        {"customer_id": 1, "campaign_id": 12, "ad_group_id": None,
         "criterion_id": 1002, "source_date": d2, "cost_micros": 9},
    ]
    as_tuples = [(r["customer_id"], r["campaign_id"], r["ad_group_id"],
                  r["criterion_id"], r["source_date"], r["cost_micros"]) for r in rows]
    folded = kw_repo.fold_daily_cost_rows(as_tuples)
    assert folded == _legacy_keyword_fold(rows)
    assert folded[("1", "10", "100", "1000")] == {"2026-11-01": 1_500_000,
                                                  "2026-11-02": 0}
    assert ("1", "10", "100", "1001") not in folded
    assert folded[("1", "11", "100", "1000")] == {"2026-11-01": 12}
    assert folded[("1", "12", "", "1002")] == {"2026-11-02": 9}


def test_02_the_search_term_fold_keeps_one_mapping_per_term_and_campaign():
    d1, d2 = date(2026, 11, 1), date(2026, 11, 2)
    folded = st_repo.fold_daily_cost_rows([
        ("freight jobs", "Brand", "10", d1, 100),
        ("freight jobs", "Brand", "10", d2, 250),
        ("freight jobs", "Brand", "11", d1, 40),        # another campaign id
        ("Freight JOBS", "Brand", "10", d1, 5),          # another raw spelling
        ("tms", "Brand", "10", d1, None),                # unavailable, not zero
    ])
    assert folded == {
        ("freight jobs", "Brand", "10"): {"2026-11-01": 100, "2026-11-02": 250},
        ("freight jobs", "Brand", "11"): {"2026-11-01": 40},
        ("Freight JOBS", "Brand", "10"): {"2026-11-01": 5},
    }


def _keyword_population_with(monkeypatch, daily_reader):
    import db.revenue_repository as rr
    agg_rows = [
        {"customer_id": "1", "campaign_id": "10", "ad_group_id": "100",
         "criterion_id": c, "cost_micros": m, "clicks": 3, "impressions": 30,
         "known_conversion_fact_rows": 1, "missing_conversion_fact_rows": 0,
         "conversions_known_total": 0.0, "fact_rows": 2,
         "currency_codes": ["GBP"], "source_systems": ["google_ads_api"],
         "campaign_name": "Brand", "ad_group_name": "AG", "keyword_text": "kw " + c,
         "match_type": "EXACT", "criterion_status": "ENABLED"}
        for c, m in (("1000", 3_000_000), ("1001", 5_000_000))]
    monkeypatch.setattr(kw_repo, "fetch_keyword_aggregates",
                        lambda s, e, **_k: {"available": True, "rows": agg_rows,
                                            "source": {}})
    monkeypatch.setattr(kw_repo, "fetch_keyword_daily_costs", daily_reader)
    monkeypatch.setattr(rr, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {
                            "available": True, "customer_id": "1",
                            "rows": [{"campaign_id": "10", "campaign_name": "Brand"}]})
    monkeypatch.setattr(rr, "fetch_campaign_identity",
                        lambda customer_id=None: {"available": True, "mappings": []})
    monkeypatch.setattr(rr, "fetch_fx_rates", lambda s, e, b, q: {
        "available": True,
        "rates": {"2026-11-01": 1.25, "2026-11-02": 1.30}})
    pop = kw_svc._build_population(date(2026, 11, 1), date(2026, 11, 2))
    return {u["criterion_id"]: (u["spend_usd"], u["fx_complete"],
                                dict(u["_daily_micros"])) for u in pop["units"]}


def test_03_the_keyword_population_asks_for_the_folded_read_and_gets_the_same_usd(
        monkeypatch):
    d1, d2 = date(2026, 11, 1), date(2026, 11, 2)
    per_day = [("1", "10", "100", "1000", d1, 1_000_000),
               ("1", "10", "100", "1000", d2, 2_000_000),
               ("1", "10", "100", "1001", d2, 5_000_000)]
    asked = []

    def _rows(start, end, **kw):
        asked.append(kw)
        return {"available": True, "rows": [
            {"customer_id": a, "campaign_id": b, "ad_group_id": c, "criterion_id": d,
             "source_date": day, "cost_micros": m,
             "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]}
            for a, b, c, d, day, m in per_day]}

    def _folded(start, end, **kw):
        asked.append(kw)
        return {"available": True, "rows": [],
                "daily_micros": kw_repo.fold_daily_cost_rows(per_day)}

    from_rows = _keyword_population_with(monkeypatch, _rows)
    from_fold = _keyword_population_with(monkeypatch, _folded)
    # The build asks for the folded form — the per-day dict list was the
    # largest single allocation of the keyword evidence build.
    assert asked and all(k.get("folded") is True for k in asked), asked
    # …and a reader answering either way yields the same per-date amounts and
    # the same per-date-FX USD: 1.0×1.25 + 2.0×1.30 = 3.85; 5.0×1.30 = 6.5.
    assert from_rows == from_fold
    assert from_fold["1000"][0] == 3.85 and from_fold["1001"][0] == 6.5
    assert from_fold["1000"][1] is True


def test_04_the_flagged_preview_asks_for_flagged_candidates_and_keeps_the_real_total(
        monkeypatch):
    seen = {}

    def _flagged(window, **kw):
        seen.update(kw)
        return {"window": window, "window_start": "2026-10-24",
                "window_end": "2026-11-22", "all_time": False, "actionable": True,
                "truth_state": {"status": "reconciled", "reasons": []},
                "rows": [{"search_term": f"t{i}"} for i in range(kw["page_size"])],
                "pagination": {"total_count": 137}}

    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    monkeypatch.setattr(st_svc, "build_flagged_search_terms", _flagged)
    section = st_svc.build_campaign_flagged_preview("30d", "23094767510")
    assert seen["population"] == st_svc.POPULATION_FLAGGED_CANDIDATES
    assert seen["campaign"] == "23094767510"
    # A preview limit never becomes the total.
    assert len(section["rows"]) == st_svc.FLAGGED_PREVIEW_LIMIT
    assert section["total_count"] == 137 and section["truncated"] is True


def test_05_an_unknown_population_is_refused_not_defaulted():
    with pytest.raises(st_svc.SearchTermQueryError):
        st_svc.build_flagged_search_terms("30d", population="everything")
    assert st_svc.POPULATIONS == ("complete", "flagged_candidates")


def _tiny_population():
    return {"available": True, "units": [], "source": {"row_count": 0},
            "identity_available": True, "canonical": {"available": True},
            "aliases_by_id": {}, "currency_info": st_svc._monetary_summary([]),
            "annotation_join": {"available": True, "annotation_rows": 0,
                                "attached": 0, "legacy_unresolved": 0}}


@pytest.mark.parametrize("crm_has_queries", [False, True])
def test_06_a_crm_that_carries_exact_queries_gets_the_complete_population(
        monkeypatch, crm_has_queries):
    """Search-term SQL attribution assigns a contact to a unit only when exactly
    ONE unit matches, and counts the attributed share over every unit. Once a
    contact carries an exact query that can no longer be decided from the
    flagged candidates alone, so the narrowed population steps aside."""
    built = []

    def _population(start, end, *, flagged_candidates=False):
        built.append(flagged_candidates)
        return _tiny_population()

    monkeypatch.setattr(st_svc, "_build_population", _population)
    monkeypatch.setattr(
        st_svc, "_search_term_sql_attribution",
        lambda pop, start, end: {"by_unit": {}, "reconciliation": {}, "coverage": {},
                                 "audit": {}, "available": True, "contacts": [],
                                 "population_has_text": crm_has_queries})
    payload = st_svc.build_flagged_search_terms(
        "30d", population=st_svc.POPULATION_FLAGGED_CANDIDATES)
    if crm_has_queries:
        assert built == [True, False]
        assert "population_scope" not in payload
    else:
        assert built == [True]
        assert payload["population_scope"] == st_svc.POPULATION_FLAGGED_CANDIDATES


def test_07_the_complete_flagged_view_never_declares_a_narrowed_population(
        monkeypatch):
    built = []

    def _population(start, end, *, flagged_candidates=False):
        built.append(flagged_candidates)
        return _tiny_population()

    monkeypatch.setattr(st_svc, "_build_population", _population)
    monkeypatch.setattr(
        st_svc, "_search_term_sql_attribution",
        lambda pop, start, end: {"by_unit": {}, "reconciliation": {}, "coverage": {},
                                 "audit": {}, "available": True, "contacts": [],
                                 "population_has_text": False})
    payload = st_svc.build_flagged_search_terms("30d")
    assert built == [False] and "population_scope" not in payload


# ═════════════════════════════════════════════════════════════════════════════
# 2 · EQUIVALENCE on a real PostgreSQL — narrowed == complete, value for value
# ═════════════════════════════════════════════════════════════════════════════

TODAY = date.today()
IN_7D = TODAY - timedelta(days=2)
IN_30D = TODAY - timedelta(days=20)
OLD = TODAY - timedelta(days=120)

CAMP_A, CAMP_B = "23094767501", "23094767502"
#: Two campaign ids sharing one display name — a `waste_terms` annotation,
#: which stores only a name, can never be placed on either.
CAMP_C, CAMP_D = "23094767503", "23094767504"
NAMES = {CAMP_A: "uk - brand", CAMP_B: "uae - competitors",
         CAMP_C: "global - shared name", CAMP_D: "global - shared name"}
WINDOWS = ("7d", "14d", "30d", "60d", "180d", "all_time")


def _exec(sql, params=()):
    from db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def _term(day, campaign_id, term, *, flagged=None, category=None, micros=2_000_000,
          ad_group="ag 1", keyword="kw", match_type="BROAD"):
    _exec("INSERT INTO search_terms "
          "(source_date, campaign_name, campaign_id, ad_group, keyword, match_type,"
          " search_term, customer_id, spend_usd, clicks, impressions, conversions,"
          " cost_micros, currency_code, source_system, is_flagged_waste,"
          " junk_category, matched_pattern) "
          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,3,40,0,%s,'GBP','google_ads_api',"
          " %s,%s,%s)",
          (day, NAMES[campaign_id], campaign_id, ad_group, keyword, match_type,
           term, ACCOUNT, micros / 1_000_000, micros, flagged, category,
           "pattern" if category else None))


def _annotation(term, campaign_name, category="job_seeker", days_ago=5):
    _exec("INSERT INTO waste_terms (run_id, run_date, search_term, campaign_name,"
          " spend_usd, junk_category, matched_pattern, crm_junk_confirmed) "
          "VALUES ((SELECT min(id) FROM runs), %s, %s, %s, 1.0, %s, 'jobs', 1)",
          (TODAY - timedelta(days=days_ago), term, campaign_name, category))


@pytest.fixture()
def adversarial(pg, monkeypatch):  # noqa: F811
    """A small database made of the cases where narrowing could go wrong."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    _exec("INSERT INTO runs (run_type, started_at, status) VALUES ('weekly', now(), 'success')")
    for offset in range(0, 200):
        _exec("INSERT INTO fx_rates (rate_date, base_currency, quote_currency, rate,"
              " provider) VALUES (%s,'GBP','USD',%s,'synthetic')",
              (TODAY - timedelta(days=offset), 1.2 + (offset % 7) * 0.01))
    for cid, name in NAMES.items():
        for day in (IN_7D, IN_30D, OLD):
            _exec("INSERT INTO google_ads_campaign_daily_spend (customer_id,"
                  " currency_code, campaign_id, campaign_name, spend_date,"
                  " cost_micros, spend_account_currency) "
                  "VALUES (%s,'GBP',%s,%s,%s,50000000,50.0)", (ACCOUNT, cid, name, day))

    # A · one unit made of two raw spellings, only ONE of them flagged; the
    #     same term, unflagged, is a separate unit in another campaign.
    _term(IN_7D, CAMP_A, "Freight JOBS", flagged=True, category="job_seeker")
    _term(IN_7D, CAMP_A, "freight jobs", flagged=None, micros=3_000_000)
    _term(IN_30D, CAMP_A, "freight  jobs", flagged=False, micros=4_000_000)
    _term(IN_7D, CAMP_B, "freight jobs", flagged=None)
    # B · flagged only by an annotation that names exactly one campaign.
    _term(IN_7D, CAMP_A, "tms course", flagged=None)
    _annotation("tms course", NAMES[CAMP_A], "education")
    # C · an annotation whose campaign name belongs to TWO ids: unplaceable.
    _term(IN_7D, CAMP_C, "erp login", flagged=None)
    _term(IN_7D, CAMP_D, "erp login", flagged=None)
    _annotation("erp login", NAMES[CAMP_C], "irrelevant")
    # D · an annotation with no campaign at all.
    _term(IN_30D, CAMP_B, "cargo salary", flagged=None)
    _annotation("cargo salary", None)
    # E · annotated against a spelling the fact rows also carry in another case.
    _term(IN_7D, CAMP_B, "Free TMS", flagged=None)
    _term(IN_30D, CAMP_B, "free tms", flagged=None)
    _annotation("free tms", NAMES[CAMP_B], "free_intent")
    # F · flagged only OUTSIDE the short windows; unflagged inside them.
    _term(OLD, CAMP_A, "nvocc salary", flagged=True, category="job_seeker")
    _term(IN_7D, CAMP_A, "nvocc salary", flagged=False)
    # G · an annotation for a term the window does not contain at all.
    _annotation("never searched here", NAMES[CAMP_A])
    _term(OLD, CAMP_B, "old only term", flagged=None)
    _annotation("old only term", NAMES[CAMP_B])
    # H · enough flagged terms in one campaign to overflow a small preview.
    for i in range(14):
        _term(IN_7D if i % 2 else IN_30D, CAMP_A, f"logistics jobs {i}",
              flagged=True, category="job_seeker", micros=(i + 1) * 1_000_000)
    # I · the bulk nobody flagged — what the narrowed population must not load.
    for i in range(60):
        _term((IN_7D, IN_30D, OLD)[i % 3], (CAMP_A, CAMP_B, CAMP_C, CAMP_D)[i % 4],
              f"freight software {i}", flagged=False if i % 2 else None)
    yield pg


def _views(window, now, **kw):
    complete = st_svc.build_flagged_search_terms(
        window, now=now, population=st_svc.POPULATION_COMPLETE, **kw)
    narrowed = st_svc.build_flagged_search_terms(
        window, now=now, population=st_svc.POPULATION_FLAGGED_CANDIDATES, **kw)
    return complete, narrowed


@_needs_pg
def test_10_the_narrowed_population_publishes_the_same_flagged_view(adversarial):
    """Every window, every campaign, the unfiltered view and every sort: the
    flagged rows, their summed metrics, the KPIs, the facets, `total_count`, the
    annotation join, the truth state and the audit block are EQUAL."""
    now = datetime.now(tz=timezone.utc)
    compared = flagged_seen = 0
    for window in WINDOWS:
        for campaign in (None, CAMP_A, CAMP_B, CAMP_C, CAMP_D):
            for sort in ("spend", "priority", "term"):
                complete, narrowed = _views(window, now, campaign=campaign,
                                            page_size=500, sort=sort)
                assert "population_scope" not in complete
                assert narrowed.pop("population_scope") == "flagged_candidates"
                assert narrowed == complete, (window, campaign, sort)
                compared += 1
                flagged_seen += complete["pagination"]["total_count"]
    assert compared == len(WINDOWS) * 5 * 3
    # The fixture is not vacuous: flagged terms exist, by BOTH kinds of
    # evidence, and annotations exist that no campaign can take.
    assert flagged_seen > 0
    complete, _ = _views("all_time", now, page_size=500)
    sources = {r["flag_source"] for r in complete["rows"]}
    assert len(sources) >= 2, sources
    join = complete["annotation_join"]
    assert join["attached"] >= 2 and join["legacy_unresolved"] >= 2, join
    # Case A: the flagged unit sums BOTH spellings inside 7d — 2.0 + 3.0 GBP of
    # native cost — although only one spelling carries the flag.
    seven, _ = _views("7d", now, campaign=CAMP_A, page_size=500)
    unit = next(r for r in seven["rows"] if r["search_term_normalized"] == "freight jobs")
    assert unit["spend_native"] == 5.0 and unit["source_rows"] == 2


@_needs_pg
def test_11_the_narrowed_population_does_not_load_what_cannot_be_flagged(adversarial):
    complete = st_svc._build_population(None, TODAY)
    narrowed = st_svc._build_population(None, TODAY, flagged_candidates=True)
    all_terms = {u["search_term_normalized"] for u in complete["units"]}
    kept = {u["search_term_normalized"] for u in narrowed["units"]}
    assert kept < all_terms
    assert not any(t.startswith("freight software") for t in kept)
    assert len(narrowed["units"]) < len(complete["units"]) / 2
    # What it does load is the complete unit, every spelling and every campaign.
    by_key = {(u["search_term_normalized"], u["campaign_key"]): u
              for u in complete["units"]}
    for u in narrowed["units"]:
        twin = by_key[(u["search_term_normalized"], u["campaign_key"])]
        for field in ("row_count", "clicks", "impressions", "cost_micros",
                      "spend_usd", "spend_raw", "search_term_variants",
                      "_daily_micros", "any_flagged", "any_unreviewed"):
            assert u[field] == twin[field], (u["search_term"], field)
    # Both campaigns' units of a candidate term are present — the pair decides
    # whether an annotation is ambiguous.
    assert {k for k in by_key if k[0] == "erp login"} == {
        (u["search_term_normalized"], u["campaign_key"])
        for u in narrowed["units"] if u["search_term_normalized"] == "erp login"}
    # The narrowed read reconciles against source totals narrowed the same way.
    assert sum(u["row_count"] for u in narrowed["units"]) == narrowed["source"]["row_count"]
    assert narrowed["source"]["row_count"] < complete["source"]["row_count"]


@_needs_pg
def test_12_the_preview_total_is_the_campaigns_whole_flagged_count(adversarial):
    now = datetime.now(tz=timezone.utc)
    for window in WINDOWS:
        complete = st_svc.build_flagged_search_terms(
            window, now=now, campaign=CAMP_A, page_size=500, sort="spend")
        section = st_svc.build_campaign_flagged_preview(window, CAMP_A, limit=3, now=now)
        assert section["available"] is True, section.get("reason")
        total = complete["pagination"]["total_count"]
        assert total > 3
        assert section["total_count"] == total and section["truncated"] is True
        assert section["rows"] == complete["rows"][:3]
        assert section["truth_state"] == complete["truth_state"]
        assert section["annotation_join"] == complete["annotation_join"]
        assert section["coverage_status"] == complete["truth_state"]["status"]
        assert {r["campaign_key"] for r in section["rows"]} == {CAMP_A}


@_needs_pg
def test_13_an_unexplained_flag_quarantines_the_narrowed_view_too(adversarial):
    """A flag with no reason evidence is an internal mismatch. It lives in the
    flagged population, so the narrowed build sees it exactly as the complete
    one does — and the drawer section withholds, it does not publish around it."""
    _term(IN_7D, CAMP_B, "unexplained flag", flagged=True, category=None)
    now = datetime.now(tz=timezone.utc)
    complete, narrowed = _views("7d", now, campaign=CAMP_A)
    assert complete["actionable"] is False and complete["rows"] == []
    assert narrowed.pop("population_scope") == "flagged_candidates"
    assert narrowed == complete
    section = st_svc.build_campaign_flagged_preview("7d", CAMP_A, now=now)
    assert section["available"] is False
    assert section["reason"] == st_svc.FLAGGED_PREVIEW_QUARANTINED
    assert section["total_count"] is None and section["rows"] == []


@_needs_pg
def test_14_an_unconfigured_account_is_unavailable_not_a_wider_read(adversarial,
                                                                     monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    monkeypatch.setattr("analysis.search_term_scope.configured_customer_id",
                        lambda: None)
    narrowed = st_repo.fetch_search_term_aggregates(None, TODAY, flagged_candidates=True)
    assert narrowed["available"] is False and narrowed["rows"] == []
    payload = st_svc.build_flagged_search_terms(
        "30d", population=st_svc.POPULATION_FLAGGED_CANDIDATES)
    assert payload["db_unavailable"] is True
    assert payload["pagination"]["total_count"] is None


@_needs_pg
def test_15_an_empty_term_list_selects_nothing(adversarial):
    """`search_terms=[]` is "no term", never "no filter"."""
    none = st_repo.fetch_search_term_daily_costs(None, TODAY, search_terms=[],
                                                 folded=True)
    assert none == {"available": True, "rows": [], "daily_micros": {}}
    assert st_repo.fetch_search_term_daily_costs(
        None, TODAY, search_terms=[])["rows"] == []
    everything = st_repo.fetch_search_term_daily_costs(None, TODAY)["rows"]
    assert len(everything) > 50


@_needs_pg
def test_16_the_folded_reads_equal_the_per_day_rows_on_a_real_server(adversarial):
    rows = st_repo.fetch_search_term_daily_costs(None, TODAY)["rows"]
    folded = st_repo.fetch_search_term_daily_costs(None, TODAY, folded=True)
    assert folded["rows"] == []
    assert folded["daily_micros"] == st_repo.fold_daily_cost_rows(
        (r["search_term"], r["campaign_name"], r["campaign_id"], r["source_date"],
         r["cost_micros"]) for r in rows)
    one = st_repo.fetch_search_term_daily_costs(
        None, TODAY, search_terms=["freight jobs"], folded=True)["daily_micros"]
    assert {k[0] for k in one} == {"freight jobs"} and len(one) == 2

    for i, (cid, day) in enumerate(((CAMP_A, IN_7D), (CAMP_A, IN_30D), (CAMP_B, IN_7D))):
        _exec("INSERT INTO keyword_daily_facts (source_date, customer_id, campaign_id,"
              " campaign_name, ad_group_id, ad_group_name, criterion_id, keyword_text,"
              " match_type, criterion_status, cost_micros, currency_code,"
              " source_system, impressions, clicks, conversions) "
              "VALUES (%s,%s,%s,%s,'ag1','AG','c1','freight software','EXACT',"
              " 'ENABLED',%s,'GBP','google_ads_api',100,10,1.0)",
              (day, ACCOUNT, cid, NAMES[cid], (i + 1) * 1_000_000))
    for ids in (None, [ACCOUNT], []):
        per_day = kw_repo.fetch_keyword_daily_costs(None, TODAY, customer_ids=ids)["rows"]
        kw_fold = kw_repo.fetch_keyword_daily_costs(None, TODAY, customer_ids=ids,
                                                    folded=True)
        assert kw_fold["rows"] == []
        assert kw_fold["daily_micros"] == _legacy_keyword_fold(per_day), ids
    assert per_day == []          # an empty account list still selects nothing

    now = datetime.now(tz=timezone.utc)
    real = kw_repo.fetch_keyword_daily_costs
    after = kw_svc.build_keyword_evidence("30d", campaign=CAMP_A,
                                          customer_ids=[ACCOUNT], now=now)
    try:
        kw_repo.fetch_keyword_daily_costs = (
            lambda s, e, **k: real(s, e, **{**k, "folded": False}))
        before = kw_svc.build_keyword_evidence("30d", campaign=CAMP_A,
                                               customer_ids=[ACCOUNT], now=now)
    finally:
        kw_repo.fetch_keyword_daily_costs = real
    assert after == before
    assert after["rows"][0]["spend_usd"] is not None
    assert after["pagination"]["total_count"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 3 · MEASURED on a production-SCALE synthetic dataset
# ═════════════════════════════════════════════════════════════════════════════
#
# Row counts are the live table sizes on 2026-11-22 (search_terms 228,043;
# keyword_daily_facts 98,152; leads 27,823; hubspot_contact_funnel 21,866;
# contact_source_classification 21,889; campaign daily spend ≈ 4,800). The
# CONTENT is synthetic: 27 campaigns, ≈ 62,000 distinct queries with a long
# tail, 180 source dates, ≈ 4% of fact rows flagged and ≈ 9% of queries
# annotated. Nothing here is a production measurement.

SCALE_CAMPAIGN = "23094767510"

#: Peak resident growth one drawer section may add to a warm process, MB.
#: Measured on this dataset: flagged 836 → 114, keyword 118 → 39 (docs/51).
FLAGGED_PREVIEW_GROWTH_LIMIT_MB = 170
KEYWORD_PREVIEW_GROWTH_LIMIT_MB = 70
#: The resident budget the certification derived on the day it failed.
AUDIT_BUDGET_MB = 293

_SCALE_SEED = r"""
SELECT setseed(0.172);

INSERT INTO runs (run_type, started_at, finished_at, status)
SELECT 'weekly', now() - (g || ' days')::interval, now() - (g || ' days')::interval, 'success'
FROM generate_series(1, 200) g;

INSERT INTO fx_rates (rate_date, base_currency, quote_currency, rate, provider, source_version)
SELECT current_date - g, 'GBP', 'USD', 1.25 + (g % 17) * 0.001, 'synthetic', 'v1'
FROM generate_series(0, 800) g;

CREATE TEMP TABLE camp AS
SELECT g AS n, (23094767500 + g)::text AS campaign_id,
       (ARRAY['uk','uae','ksa','global','us','eu','apac','mena','india'])[1 + g % 9]
       || ' - ' ||
       (ARRAY['brand','competitors','freight software','logistics crm','customs'])[1 + g % 5]
       || ' ' || g AS campaign_name
FROM generate_series(1, 27) g;

INSERT INTO google_ads_campaign_daily_spend
  (customer_id, currency_code, campaign_id, campaign_name, spend_date, cost_micros, spend_account_currency)
SELECT '__ACCT__', 'GBP', c.campaign_id, c.campaign_name, current_date - d,
       (10 + (c.n * d) % 90) * 1000000, 10 + (c.n * d) % 90
FROM camp c, generate_series(1, 180) d
WHERE (c.n * 7 + d) % 100 < 99;

CREATE TEMP TABLE words AS SELECT ARRAY[
 'freight','forwarding','software','logistics','crm','customs','clearance','shipping',
 'cargo','tracking','system','management','platform','warehouse','transport','best',
 'free','jobs','salary','course','login','download','open','source','cloud','erp',
 'for','small','business','company','companies','in','dubai','uk','india','near','me',
 'price','pricing','demo','what','is','how','to','air','sea','ocean','nvocc','tms','wms'] AS w;

INSERT INTO search_terms
  (run_id, source_date, campaign_name, campaign_id, ad_group, keyword, match_type,
   search_term, customer_id, spend_usd, clicks, impressions, conversions, cost_micros,
   currency_code, source_system, is_flagged_waste, junk_category, matched_pattern)
SELECT 1 + (x.d % 200), current_date - x.d, c.campaign_name, c.campaign_id,
       'ad group ' || x.ag, 'keyword ' || x.kw || ' freight software',
       (ARRAY['BROAD','PHRASE','EXACT'])[1 + x.kw % 3],
       w.w[1 + x.t % 50] || ' ' || w.w[1 + (x.t / 50) % 50] || ' ' || w.w[1 + (x.t / 2500) % 50]
         || ' ' || w.w[1 + (x.t * 7) % 50] || CASE WHEN x.t % 3 = 0 THEN ' ' || (x.t % 997) ELSE '' END,
       '__ACCT__', round((x.micros / 1000000.0)::numeric, 2), 1 + x.t % 9, 10 + x.t % 400,
       CASE WHEN x.t % 40 = 0 THEN 1.0 ELSE 0 END, x.micros, 'GBP', 'google_ads_api',
       CASE WHEN x.t % 25 = 0 THEN TRUE WHEN x.t % 3 = 0 THEN FALSE ELSE NULL END,
       CASE WHEN x.t % 25 = 0 THEN (ARRAY['job_seeker','free_intent','education','competitor','irrelevant'])[1 + x.t % 5] END,
       CASE WHEN x.t % 25 = 0 THEN (ARRAY['jobs','free','course','login','salary'])[1 + x.t % 5] END
FROM (
  SELECT g, g % 180 AS d,
         -- long-tail query popularity: head queries repeat daily
         floor(70000 * power(random(), 2.2))::int AS t,
         random() AS r1, random() AS r2,
         (100000 + floor(random() * 9000000))::bigint AS micros
  FROM generate_series(1, 420000) g
) x0
CROSS JOIN LATERAL (
  -- a query mostly re-triggers the same keyword in the same ad group; one time
  -- in five it lands somewhere else in the account
  SELECT CASE WHEN x0.r1 < 0.8 THEN 1 + (x0.t * 13) % 140 ELSE 1 + floor(x0.r2 * 140)::int END AS ag,
         CASE WHEN x0.r1 < 0.8 THEN 1 + (x0.t * 7) % 1800 ELSE 1 + floor(x0.r2 * 1800)::int END AS kw
) pick
CROSS JOIN LATERAL (SELECT x0.g, x0.d, x0.t, x0.micros, pick.ag, pick.kw) x
JOIN camp c ON c.n = 1 + x.ag % 27
CROSS JOIN words w
ON CONFLICT DO NOTHING;
DELETE FROM search_terms WHERE id IN (
  SELECT id FROM search_terms ORDER BY md5(id::text)
  LIMIT GREATEST(0, (SELECT count(*) FROM search_terms) - 228043));

INSERT INTO waste_terms (run_id, run_date, search_term, campaign_name, spend_usd,
                         junk_category, matched_pattern, crm_junk_confirmed)
SELECT 1 + (row_number() OVER ()) % 200, current_date - ((row_number() OVER ()) % 180)::int,
       s.search_term, s.campaign_name, 5.0, 'job_seeker', 'jobs', 1
FROM (SELECT DISTINCT search_term, campaign_name FROM search_terms
      WHERE md5(search_term) < '18') s;

INSERT INTO keyword_daily_facts
  (run_id, source_date, customer_id, campaign_id, campaign_name, ad_group_id, ad_group_name,
   criterion_id, keyword_text, match_type, criterion_status, cost_micros, currency_code,
   source_system, impressions, clicks, conversions, quality_score, expected_ctr,
   ad_relevance, landing_page_experience, quality_observed_at)
SELECT 1, current_date - d, '__ACCT__', c.campaign_id, c.campaign_name,
       (9000 + k % 140)::text, 'ad group ' || (k % 140), (700000 + k)::text,
       'keyword ' || k || ' freight software', (ARRAY['BROAD','PHRASE','EXACT'])[1 + k % 3],
       CASE WHEN k % 11 = 0 THEN 'PAUSED' ELSE 'ENABLED' END,
       ((k * d) % 9000) * 1000, 'GBP', 'google_ads_api', 5 + (k * d) % 300, (k + d) % 12,
       CASE WHEN (k + d) % 30 = 0 THEN 1.0 ELSE 0 END, 1 + k % 10,
       'AVERAGE', 'ABOVE_AVERAGE', 'BELOW_AVERAGE', now() - (d || ' days')::interval
FROM generate_series(1, 1800) k
JOIN camp c ON c.n = 1 + (k % 140) % 27
CROSS JOIN generate_series(1, 180) d
WHERE (k * 31 + d * 17) % 1000 < 303
ON CONFLICT DO NOTHING;
DELETE FROM keyword_daily_facts WHERE id IN (
  SELECT id FROM keyword_daily_facts ORDER BY md5(id::text)
  LIMIT GREATEST(0, (SELECT count(*) FROM keyword_daily_facts) - 98152));

INSERT INTO leads (run_id, run_date, contact_id, campaign_name, keyword, country,
                   mql_status, status_category, gclid, source_type, company,
                   contact_created_at, hs_analytics_source)
SELECT 1 + g % 200, current_date - (g % 200), (100000 + g % 9300)::text,
       CASE WHEN (g % 9300) % 10 < 8 THEN (SELECT campaign_name FROM camp WHERE n = 1 + (g % 9300) % 27)
            ELSE 'legacy label ' || (g % 9300) % 40 END,
       'keyword ' || (1 + (g % 9300) % 1800) || ' freight software',
       (ARRAY['AE','GB','SA','IN','US','DE'])[1 + (g % 9300) % 6],
       'OPEN', (ARRAY['qualified','in_progress','junk','wrong_fit','in_progress','junk'])[1 + (g % 9300) % 6],
       CASE WHEN (g % 9300) % 3 = 0 THEN 'gclid-' || (g % 9300) END,
       CASE WHEN (g % 9300) % 5 < 3 THEN 'paid_search' ELSE 'organic' END,
       'Company ' || (g % 9300),
       now() - (((g % 9300) % 700) || ' days')::interval, 'PAID_SEARCH'
FROM generate_series(1, 27823) g;

INSERT INTO hubspot_contact_funnel
  (contact_id, created_at, last_modified_at, lifecycle_stage, lead_status, mql_status,
   mql_status_category, date_entered_lead, date_entered_sql,
   hs_analytics_source, hs_analytics_source_data_1, hs_analytics_source_data_2,
   hs_latest_source, ip_country, country, company, gclid, has_gclid, source_system)
SELECT (100000 + g)::text, now() - ((g % 700) || ' days')::interval, now() - ((g % 30) || ' days')::interval,
       (ARRAY['lead','marketingqualifiedlead','salesqualifiedlead','opportunity','customer','subscriber'])[1 + g % 6],
       'OPEN', 'OPEN', (ARRAY['qualified','in_progress','junk','wrong_fit'])[1 + g % 4],
       now() - ((g % 700) || ' days')::interval,
       CASE WHEN g % 6 IN (2,3,4) THEN now() - ((g % 690) || ' days')::interval END,
       (ARRAY['PAID_SEARCH','ORGANIC_SEARCH','DIRECT_TRAFFIC','PAID_SOCIAL'])[1 + g % 4],
       CASE WHEN g % 4 = 0 THEN (SELECT campaign_name FROM camp WHERE n = 1 + g % 27) ELSE 'google' END,
       'keyword ' || (1 + g % 1800), 'PAID_SEARCH',
       (ARRAY['ae','gb','sa','in','us','de'])[1 + g % 6], 'United Arab Emirates', 'Company ' || g,
       CASE WHEN g % 3 = 0 THEN 'gclid-' || g END, g % 3 = 0, 'hubspot'
FROM generate_series(1, 21866) g;

INSERT INTO contact_source_classification
  (contact_key, contact_id, source_primary_raw, source_detail_raw, acquisition_group,
   classification_rule_version, contact_created_at, status_category)
SELECT 'id:' || (100000 + g), (100000 + g)::text,
       (ARRAY['PAID_SEARCH','ORGANIC_SEARCH','DIRECT_TRAFFIC','PAID_SOCIAL'])[1 + g % 4], 'google',
       (ARRAY['google_ads','organic','direct','paid_social'])[1 + g % 4], 'v1',
       now() - ((g % 700) || ' days')::interval,
       (ARRAY['qualified','in_progress','junk','wrong_fit'])[1 + g % 4]
FROM generate_series(1, 21889) g;

ANALYZE;
"""

#: Run in a FRESH interpreter so the peak is this build's and nobody else's.
#: Prints one JSON line: resident memory before the build, the process's peak
#: afterwards, and what the section published.
_MEASURE = r"""
import gc, json, os, sys
sys.path.insert(0, os.getcwd())

def _status(field):
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024.0

import db.connection as connection
connection.init_pool()
import api.server                                   # as the audit and the server do
import services.keyword_evidence_service as kw
import services.search_term_evidence_service as st

builder, window, campaign = sys.argv[1:4]
warm = {"flagged": st.build_campaign_flagged_preview,
        "keyword": kw.build_campaign_keyword_preview}[builder]
warm("7d", None)                                    # imports and lazy state, no read
gc.collect()
before = _status("VmRSS")
section = warm(window, campaign)
print(json.dumps({"before_mb": before, "peak_mb": _status("VmHWM"),
                  "available": section.get("available"), "reason": section.get("reason"),
                  "rows": len(section.get("rows") or []),
                  "total_count": section.get("total_count")}))
"""

_EQUIVALENT_AT_SCALE = r"""
import json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.getcwd())
import db.connection as connection
connection.init_pool()
import services.search_term_evidence_service as st

now = datetime.now(tz=timezone.utc)
out = {}
for campaign in sys.argv[2:]:
    complete = st.build_flagged_search_terms(sys.argv[1], now=now, campaign=campaign,
                                             page_size=500, sort="spend")
    narrowed = st.build_flagged_search_terms(sys.argv[1], now=now, campaign=campaign,
                                             page_size=500, sort="spend",
                                             population="flagged_candidates")
    scope = narrowed.pop("population_scope", None)
    out[campaign] = {"equal": narrowed == complete, "scope": scope,
                     "total_count": complete["pagination"]["total_count"],
                     "attached": complete["annotation_join"]["attached"],
                     "unresolved": complete["annotation_join"]["legacy_unresolved"]}
print(json.dumps(out))
"""


def _child_env(url: str) -> dict:
    env = dict(os.environ)
    env.update({"DATABASE_URL": url, "GOOGLE_ADS_CUSTOMER_ID": ACCOUNT})
    return env


def _run_child(code: str, url: str, *args, timeout=900) -> dict:
    proc = subprocess.run([sys.executable, "-c", code, *args], cwd=_ROOT,
                          env=_child_env(url), capture_output=True, text=True,
                          timeout=timeout)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def scale_db():
    """ONE throwaway cluster holding the production-scale dataset, shared by the
    measured cases. The schema is applied by a child process so this module
    never points the test process's own pool at it."""
    cluster = _PgCluster().start()
    try:
        init = subprocess.run(
            [sys.executable, "-c",
             "import db.connection as c; c.init_pool(); "
             "from db.schema import init_db; init_db()"],
            cwd=_ROOT, env=_child_env(cluster.url), capture_output=True, text=True)
        assert init.returncode == 0, init.stderr[-2000:]
        conn = cluster.connect()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(_SCALE_SEED.replace("__ACCT__", ACCOUNT))
                cur.execute("SELECT (SELECT count(*) FROM search_terms),"
                            " (SELECT count(*) FROM keyword_daily_facts),"
                            " (SELECT count(DISTINCT search_term) FROM search_terms),"
                            " (SELECT count(*) FROM waste_terms)")
                cluster.sizes = cur.fetchone()
        finally:
            conn.close()
        yield cluster
    finally:
        cluster.stop()


@_needs_pg
def test_20_the_scale_dataset_has_the_live_tables_row_counts(scale_db):
    """A bound measured on a toy proves nothing. State what was measured."""
    search_terms, keyword_facts, distinct_terms, annotations = scale_db.sizes
    assert search_terms == 228_043
    assert keyword_facts == 98_152
    assert distinct_terms > 50_000
    assert annotations > 5_000


@_needs_pg
def test_21_the_flagged_preview_of_one_campaign_stays_bounded_at_all_time(scale_db):
    """Before PR-ADS-172A this section built every term × campaign unit of the
    account: ≈ 836 MB of resident growth on this dataset."""
    m = _run_child(_MEASURE, scale_db.url, "flagged", "all_time", SCALE_CAMPAIGN)
    assert m["available"] is True, m
    # The preview is ten rows; the total is the campaign's whole flagged count.
    assert m["rows"] == st_svc.FLAGGED_PREVIEW_LIMIT and m["total_count"] > 100, m
    growth = m["peak_mb"] - m["before_mb"]
    assert growth <= FLAGGED_PREVIEW_GROWTH_LIMIT_MB, (
        f"the flagged preview added {growth:.0f} MB of resident memory "
        f"(limit {FLAGGED_PREVIEW_GROWTH_LIMIT_MB} MB): {m}")


@_needs_pg
def test_22_the_keyword_preview_of_one_campaign_stays_bounded_at_all_time(scale_db):
    """Before PR-ADS-172A this section read every (criterion, day) cost row of
    the account as a dict: ≈ 118 MB of resident growth on this dataset."""
    m = _run_child(_MEASURE, scale_db.url, "keyword", "all_time", SCALE_CAMPAIGN)
    assert m["available"] is True, m
    assert m["rows"] == kw_svc.CAMPAIGN_PREVIEW_LIMIT and m["total_count"] > 10, m
    growth = m["peak_mb"] - m["before_mb"]
    assert growth <= KEYWORD_PREVIEW_GROWTH_LIMIT_MB, (
        f"the keyword preview added {growth:.0f} MB of resident memory "
        f"(limit {KEYWORD_PREVIEW_GROWTH_LIMIT_MB} MB): {m}")


@_needs_pg
def test_23_the_narrowed_view_equals_the_complete_one_at_scale(scale_db):
    out = _run_child(_EQUIVALENT_AT_SCALE, scale_db.url, "30d",
                     SCALE_CAMPAIGN, "23094767503")
    for campaign, result in out.items():
        assert result["equal"] is True, (campaign, result)
        assert result["scope"] == "flagged_candidates"
        assert result["total_count"] > 10, result
        assert result["attached"] > 0 and result["unresolved"] > 0, result


@_needs_pg
def test_24_the_real_audit_holds_under_the_real_cap_at_production_scale(
        scale_db, monkeypatch):
    """The production failure, end to end: the six-window Campaign Evidence
    audit, started by the certification's own capped runner with the configured
    address-space backstop, classified by the certification's own rules.

    Before PR-ADS-172A, on this dataset: "reached its memory backstop (320 MB
    of address space)" at ≈ 294 MB resident — the message production recorded
    on 2026-11-22 at 287 MB."""
    import scripts.audit_platform_certification as apc
    monkeypatch.setenv("DATABASE_URL", scale_db.url)
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    settings = apc.load_settings()
    assert int(settings["audit_address_space_mb"]) == 320     # the cap is not raised
    assert int(settings["safety_margin_mb"]) == 64            # nor the margin lowered

    module = "scripts.audit_campaign_evidence_certification"
    assert module in [audit[1] for audit in apc.AUDITS]       # nor the audit exempted
    rc, out, err, error, memory = apc._run_capped(module, 900,
                                                  memory_mb=AUDIT_BUDGET_MB)
    outcome = apc.classify(rc, out, err, error=error, memory=memory)
    assert outcome["outcome"] == apc.HOLDS, (outcome.get("detail"), memory)
    assert memory["memory_error"] is False and memory["watchdog_killed"] is False
    # Not merely under the cap: with room to spare, so growth is seen early.
    assert memory["peak_rss_mb"] <= AUDIT_BUDGET_MB * 0.75, memory
    assert memory["vm_peak_mb"] <= 320 - 3 * apc.MEMORY_GUARD_MB, memory
    report = outcome["report"]
    assert report["windows_audited"] == list(WINDOWS)
    assert report["certified"] is True and not report["violations"]
    # Every window probed a real campaign and both sections were available —
    # an audit that held because its sections came back unavailable is not this.
    for window in WINDOWS:
        sections = report["per_window"][window]["sections"]
        assert sections == {"keyword": {"available": True, "reason": None},
                            "flagged": {"available": True, "reason": None}}, window
    # …and the row the certification stores keeps that proof.
    from services.platform_certification_service import summarize
    stored = summarize({"audits": [{**outcome, "key": "campaign_evidence"}]})
    assert stored["audits"][0]["drawer_sections"] == {
        w: report["per_window"][w]["sections"] for w in WINDOWS}


# ═════════════════════════════════════════════════════════════════════════════
# 4 · A section that was NOT BUILT is not a section that held (review round)
# ═════════════════════════════════════════════════════════════════════════════
#
# "The audit holds in little memory" is also what an audit looks like when its
# sections were never built. Before this round a failed read — and a swallowed
# MemoryError — came back as `available: False` with a reason, and the gate
# passed any unavailable section that carried one.

import scripts.audit_campaign_evidence_certification as cert  # noqa: E402


class _BrokenConn:
    """A pooled connection whose every statement fails with ``exc``."""

    def __init__(self, exc):
        self.exc = exc

    def cursor(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        raise self.exc


def _broken_get_conn(exc):
    import contextlib

    @contextlib.contextmanager
    def _get_conn():
        yield _BrokenConn(exc)
    return _get_conn


_READERS = [
    (st_repo, lambda: st_repo.fetch_search_term_aggregates(None, TODAY)),
    (st_repo, lambda: st_repo.fetch_search_term_aggregates(None, TODAY,
                                                           flagged_candidates=True)),
    (st_repo, lambda: st_repo.fetch_search_term_daily_costs(None, TODAY)),
    (st_repo, lambda: st_repo.fetch_search_term_daily_costs(None, TODAY, search_terms=["x"],
                                                            folded=True)),
    (kw_repo, lambda: kw_repo.fetch_keyword_aggregates(None, TODAY, customer_ids=["1"])),
    (kw_repo, lambda: kw_repo.fetch_keyword_daily_costs(None, TODAY)),
    (kw_repo, lambda: kw_repo.fetch_keyword_daily_costs(None, TODAY, folded=True)),
]


@pytest.mark.parametrize("index", range(len(_READERS)))
def test_30_a_reader_never_turns_out_of_memory_into_unavailable(monkeypatch, index):
    module, read = _READERS[index]
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    # An ordinary failure is still an unavailable answer, never a raise…
    monkeypatch.setattr(module, "get_conn", _broken_get_conn(RuntimeError("cursor lost")))
    assert read()["available"] is False
    # …but out of memory is not an answer at all.
    monkeypatch.setattr(module, "get_conn", _broken_get_conn(MemoryError()))
    with pytest.raises(MemoryError):
        read()


def test_31_a_drawer_section_never_turns_out_of_memory_into_unavailable(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)

    def _oom(*a, **k):
        raise MemoryError()

    monkeypatch.setattr(st_repo, "fetch_search_term_aggregates", _oom)
    with pytest.raises(MemoryError):
        st_svc.build_campaign_flagged_preview("30d", CAMP_A)
    with pytest.raises(MemoryError):
        st_svc.build_flagged_search_terms("30d")
    monkeypatch.setattr(kw_repo, "fetch_keyword_aggregates", _oom)
    with pytest.raises(MemoryError):
        kw_svc.build_campaign_keyword_preview("30d", CAMP_A)
    # The SQL-attribution helpers are "defensive" for the same reason, and are
    # equally not allowed to absorb it.
    import services.platform_sql_attribution_service as psa
    monkeypatch.setattr(psa, "fetch_and_resolve_contacts", _oom)
    with pytest.raises(MemoryError):
        st_svc._search_term_sql_attribution({"units": []}, None, TODAY)
    with pytest.raises(MemoryError):
        kw_svc._keyword_sql_attribution({"units": []}, None, TODAY)


def _emitted_reason_codes() -> set:
    import api.server as server
    codes = {getattr(kw_svc, n) for n in dir(kw_svc) if n.startswith("PREVIEW_UNAVAILABLE_")}
    codes |= {getattr(st_svc, n) for n in dir(st_svc)
              if n.startswith("FLAGGED_PREVIEW_UNAVAILABLE_")}
    codes |= {st_svc.FLAGGED_PREVIEW_QUARANTINED, server.WINDOW_NOT_RESOLVED}
    return codes


def test_32_every_reason_a_section_can_emit_is_classified_and_unknown_fails_closed():
    emitted = _emitted_reason_codes()
    assert len(emitted) >= 11, emitted
    tables = (cert.SECTION_REASONS_DECLARED, cert.SECTION_REASONS_FAILURE,
              cert.SECTION_REASONS_VIOLATION)
    classified = set().union(*tables)
    assert emitted <= classified, f"unclassified reason codes: {emitted - classified}"
    assert classified <= emitted, f"classified but never emitted: {classified - emitted}"
    assert sum(len(t) for t in tables) == len(classified)      # one class each
    # A read that failed is never a declared withholding.
    for failed in (kw_svc.PREVIEW_UNAVAILABLE_SOURCE, kw_svc.PREVIEW_UNAVAILABLE_ERROR,
                   kw_svc.PREVIEW_UNAVAILABLE_CONTEXT,
                   st_svc.FLAGGED_PREVIEW_UNAVAILABLE_SOURCE,
                   st_svc.FLAGGED_PREVIEW_UNAVAILABLE_ERROR,
                   st_svc.FLAGGED_PREVIEW_UNAVAILABLE_CONTEXT):
        assert cert.classify_section_reason(failed)[0] == cert.SECTION_REASON_FAILURE
    assert cert.classify_section_reason(
        kw_svc.PREVIEW_UNAVAILABLE_SCOPE)[0] == cert.SECTION_REASON_VIOLATION
    for unknown in ("brand_new_reason", "", None, 7, "Keyword_Preview_Failed"):
        assert cert.classify_section_reason(unknown)[0] == cert.SECTION_REASON_UNRECOGNISED


def _audit_one_window(monkeypatch, *, keyword_reason=None, flagged_reason=None):
    """`_audit_window` over a headline build that succeeded, with each drawer
    section either available or unavailable for the given reason."""
    import services.campaign_evidence_service as ce
    monkeypatch.setattr(ce, "build_campaign_evidence", lambda window: {
        "campaigns": [{"campaign_key": CAMP_A, "campaign_name": "uk - brand"}],
        "summary": {}, "sql_reconciliation": {}})

    def _section(real_unavailable, reason, window, key):
        if reason is not None:
            return real_unavailable(reason, window=window, campaign_key=key,
                                    customer_id=ACCOUNT)
        base = real_unavailable("x", window=window, campaign_key=key, customer_id=ACCOUNT)
        return {**base, "available": True, "reason": None, "window": window,
                "window_start": "2026-10-24", "window_end": "2026-11-22",
                "all_time": False, "rows": [], "total_count": 0}

    monkeypatch.setattr(kw_svc, "build_campaign_keyword_preview",
                        lambda w, k: _section(kw_svc.keyword_preview_unavailable,
                                              keyword_reason, w, k))
    monkeypatch.setattr(st_svc, "build_campaign_flagged_preview",
                        lambda w, k: _section(st_svc.flagged_preview_unavailable,
                                              flagged_reason, w, k))
    findings = cert.Findings()
    result = cert._audit_window("30d", findings)
    checks = {c["check"]: c["ok"] for c in findings.checks}
    return findings, result, checks


@pytest.mark.parametrize("section", ["keyword", "flagged"])
def test_33_a_section_that_failed_to_build_is_not_a_passed_check(monkeypatch, section):
    control, result, checks = _audit_one_window(monkeypatch)
    assert checks["section[keyword/30d]"] is True and checks["section[flagged/30d]"] is True
    assert not [u for u in control.unavailable if u.startswith("section[")]
    assert result["sections"] == {"keyword": {"available": True, "reason": None},
                                  "flagged": {"available": True, "reason": None}}

    failures = sorted(r for r in cert.SECTION_REASONS_FAILURE
                      if r != "window_not_resolved")
    for reason in failures + ["a_reason_nobody_declared"]:
        findings, result, checks = _audit_one_window(
            monkeypatch, **{f"{section}_reason": reason})
        label = f"section[{section}/30d]"
        assert checks[label] is False, reason
        assert any(u.startswith(label) for u in findings.unavailable), reason
        assert findings.exit_code != cert.EXIT_CERTIFIED, reason
        # …and the report says what the section was, for the stored run.
        assert result["sections"][section] == {"available": False, "reason": reason}

    # "No account configured" read nothing: with the headline built, not a pass.
    assert "google_ads_customer_not_configured" in failures


def test_33b_a_declared_withholding_passes_and_is_named_as_a_warning(monkeypatch):
    """A quarantined flagged population is the section refusing correctly — a
    pass, told apart from a failure, and never a silent one."""
    assert cert.SECTION_REASONS_DECLARED.keys() == {st_svc.FLAGGED_PREVIEW_QUARANTINED}
    findings, _result, checks = _audit_one_window(
        monkeypatch, flagged_reason=st_svc.FLAGGED_PREVIEW_QUARANTINED)
    assert checks["section[flagged/30d]"] is True
    assert not [u for u in findings.unavailable if u.startswith("section[")]
    assert len(findings.warnings) == 1
    assert findings.warnings[0].startswith(
        "section[flagged/30d]: unavailable, reason=search_term_truth_mismatch")
    clean, _result, _checks = _audit_one_window(monkeypatch)
    assert clean.warnings == []

    from services.platform_certification_service import summarize
    sections = {"30d": {"sections": {
        "keyword": {"available": True, "reason": None},
        "flagged": {"available": False, "reason": "search_term_truth_mismatch"}}}}
    stored = summarize({"verdict": "CERTIFIED", "exit_code": 0, "audits": [
        {"key": "campaign_evidence", "outcome": "holds", "exit_code": 0,
         "report": {"per_window": sections, "warnings": findings.warnings}},
        {"key": "campaign_evidence", "outcome": "holds", "exit_code": 0,
         "report": {"per_window": sections, "warnings": []}},
        {"key": "closed_won_truth", "outcome": "holds", "exit_code": 0,
         "report": {"totals": {}}}]})
    warned, quiet, other = stored["audits"]
    assert warned["drawer_warnings"] == findings.warnings
    assert quiet["drawer_warnings"] == []
    assert "drawer_warnings" not in other


@_needs_pg
def test_33c_with_no_account_configured_the_window_is_not_certified(
        adversarial, monkeypatch):
    """Canonical spend rows exist, so the headline builds and resolves a
    campaign — and then neither section reads anything. That run built no
    drawer section at all and used to exit as if it had."""
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    monkeypatch.setattr("analysis.search_term_scope.configured_customer_id",
                        lambda: None)
    findings = cert.Findings()
    result = cert._audit_window("30d", findings)
    assert result.get("probe_campaign_key")
    assert result["sections"] == {
        name: {"available": False, "reason": "google_ads_customer_not_configured"}
        for name in ("keyword", "flagged")}
    checks = {c["check"]: c["ok"] for c in findings.checks}
    for name in ("keyword", "flagged"):
        assert checks[f"section[{name}/30d]"] is False
        assert any(u.startswith(f"section[{name}/30d]") for u in findings.unavailable)
    assert findings.exit_code != cert.EXIT_CERTIFIED


@pytest.mark.parametrize("reader", ["fetch_fx_rates", "fetch_canonical_campaign_spend",
                                    "fetch_campaign_identity", "fetch_sql_contacts"])
def test_33d_the_readers_these_builders_call_never_absorb_out_of_memory(
        monkeypatch, reader):
    import db.platform_sql_attribution_repository as sql_repo
    import db.revenue_repository as rr
    module = sql_repo if reader == "fetch_sql_contacts" else rr
    call = {"fetch_fx_rates": lambda: rr.fetch_fx_rates(None, TODAY, "GBP", "USD"),
            "fetch_canonical_campaign_spend":
                lambda: rr.fetch_canonical_campaign_spend(None, TODAY),
            "fetch_campaign_identity": lambda: rr.fetch_campaign_identity(None),
            "fetch_sql_contacts": lambda: sql_repo.fetch_sql_contacts(None, TODAY)}[reader]
    monkeypatch.setattr(module, "get_conn", _broken_get_conn(RuntimeError("cursor lost")))
    assert call()["available"] is False
    monkeypatch.setattr(module, "get_conn", _broken_get_conn(MemoryError()))
    with pytest.raises(MemoryError):
        call()


def test_33e_the_headline_builder_never_absorbs_out_of_memory(monkeypatch):
    import services.campaign_evidence_service as ce
    import services.canonical_contact_outcome_service as canon
    import services.marketing_outcome_cohort_service as cohort_svc
    import db.revenue_repository as rr

    def _oom(*a, **k):
        raise MemoryError()

    calls = []

    def _oom_once(*a, **k):
        # The handler this guards RETRIES the read; a second attempt that
        # happened to fit would have hidden the first one running out.
        calls.append(k)
        if len(calls) == 1:
            raise MemoryError()
        return {"reconciliation_status": "reconciled"}

    monkeypatch.setattr(canon, "page_reconciliation", _oom_once)
    with pytest.raises(MemoryError):
        ce._canonical_sql_reconciliation("30d", 0, None)
    assert len(calls) == 1
    monkeypatch.setattr(cohort_svc, "lifecycle_event_disclosure", _oom)
    with pytest.raises(MemoryError):
        ce._safe_lifecycle_disclosure(cohort_svc)
    monkeypatch.setattr(rr, "fetch_canonical_campaign_spend", _oom)
    with pytest.raises(MemoryError):
        ce.build_campaign_evidence("30d")


@_needs_pg
def test_39b_a_failed_fx_read_is_unavailable_and_a_missing_rate_is_fx_incomplete(
        adversarial, monkeypatch):
    import db.revenue_repository as rr
    now = datetime.now(tz=timezone.utc)
    _exec("INSERT INTO keyword_daily_facts (source_date, customer_id, campaign_id,"
          " campaign_name, ad_group_id, ad_group_name, criterion_id, keyword_text,"
          " match_type, criterion_status, cost_micros, currency_code,"
          " source_system, impressions, clicks, conversions) "
          "VALUES (%s,%s,%s,%s,'ag1','AG','c1','freight software','EXACT',"
          " 'ENABLED',1000000,'GBP','google_ads_api',100,10,1.0)",
          (IN_7D, ACCOUNT, CAMP_A, NAMES[CAMP_A]))

    # A rate genuinely MISSING for a day is a fact about the rate table: the
    # section stays available and says fx_incomplete, exactly as before.
    _exec("DELETE FROM fx_rates WHERE rate_date = %s", (IN_7D,))
    section = st_svc.build_campaign_flagged_preview("7d", CAMP_A, now=now)
    assert section["available"] is True
    assert "fx_incomplete" in {r["currency_status"] for r in section["rows"]}
    assert all(r["spend_usd"] is None for r in section["rows"]
               if r["currency_status"] == "fx_incomplete")
    kw_section = kw_svc.build_campaign_keyword_preview("7d", CAMP_A, now=now)
    assert kw_section["available"] is True
    assert kw_section["rows"][0]["spend_usd"] is None

    # A rate read that FAILED is a fact about us: nothing is published as
    # FX-incomplete on the strength of it.
    monkeypatch.setattr(rr, "fetch_fx_rates",
                        lambda *a, **k: {"available": False, "rates": {}})
    for population in st_svc.POPULATIONS:
        payload = st_svc.build_flagged_search_terms("30d", now=now, population=population)
        assert payload["db_unavailable"] is True and payload["rows"] == []
    section = st_svc.build_campaign_flagged_preview("30d", CAMP_A, now=now)
    assert (section["available"], section["reason"], section["total_count"]) == (
        False, st_svc.FLAGGED_PREVIEW_UNAVAILABLE_SOURCE, None)
    assert kw_svc.build_keyword_evidence("30d", now=now)["db_unavailable"] is True
    kw_section = kw_svc.build_campaign_keyword_preview("30d", CAMP_A, now=now)
    assert (kw_section["available"], kw_section["reason"]) == (
        False, kw_svc.PREVIEW_UNAVAILABLE_SOURCE)

    findings = cert.Findings()
    cert._audit_window("30d", findings)
    checks = {c["check"]: c["ok"] for c in findings.checks}
    assert checks["section[flagged/30d]"] is False
    assert checks["section[keyword/30d]"] is False
    assert findings.exit_code != cert.EXIT_CERTIFIED


def test_34_a_scope_breach_reported_by_a_section_is_a_violation(monkeypatch):
    findings, _result, checks = _audit_one_window(
        monkeypatch, keyword_reason=kw_svc.PREVIEW_UNAVAILABLE_SCOPE)
    assert checks["section[keyword/30d]"] is False
    assert any(v.startswith("section[keyword/30d]") for v in findings.violations)
    assert findings.exit_code == cert.EXIT_VIOLATION


def test_35_out_of_memory_ends_the_audit_instead_of_becoming_a_check(monkeypatch):
    import services.campaign_evidence_service as ce

    def _oom(*a, **k):
        raise MemoryError()

    monkeypatch.setattr(ce, "build_campaign_evidence", _oom)
    with pytest.raises(MemoryError):
        cert._audit_window("30d", cert.Findings())
    monkeypatch.setattr(ce, "build_campaign_evidence", lambda window: {
        "campaigns": [{"campaign_key": CAMP_A, "campaign_name": "uk - brand"}],
        "summary": {}, "sql_reconciliation": {}})
    monkeypatch.setattr(kw_svc, "build_campaign_keyword_preview", _oom)
    with pytest.raises(MemoryError):
        cert._audit_window("30d", cert.Findings())


def test_36_the_stored_run_keeps_what_each_drawer_section_was():
    from services.platform_certification_service import summarize
    per_window = {w: {"window": w, "available": True, "sections": {
        "keyword": {"available": True, "reason": None},
        "flagged": {"available": w != "all_time",
                    "reason": None if w != "all_time" else "flagged_preview_failed"}}}
        for w in WINDOWS}
    per_window["7d"] = {"window": "7d", "available": False}      # built no section
    summary = summarize({"verdict": "NOT CERTIFIED", "exit_code": 2, "audits": [
        {"key": "campaign_evidence", "outcome": "unavailable", "exit_code": 2,
         "report": {"per_window": per_window, "checks": ["large"] * 500}},
        {"key": "closed_won_truth", "outcome": "holds", "exit_code": 0,
         "report": {"totals": {}}},
        {"key": "crashed_one", "outcome": "crashed", "exit_code": None, "report": None}]})
    campaign, closed_won, crashed = summary["audits"]
    assert "report" not in campaign
    assert set(campaign["drawer_sections"]) == set(WINDOWS)
    assert campaign["drawer_sections"]["7d"] == {}
    assert campaign["drawer_sections"]["30d"] == {
        "keyword": {"available": True, "reason": None},
        "flagged": {"available": True, "reason": None}}
    assert campaign["drawer_sections"]["all_time"]["flagged"] == {
        "available": False, "reason": "flagged_preview_failed"}
    # Only an audit that builds sections carries the key.
    assert "drawer_sections" not in closed_won and "drawer_sections" not in crashed
    json.dumps(summary)


@_needs_pg
@pytest.mark.parametrize("path", ["flagged", "keyword"])
def test_37_the_audit_does_not_certify_a_window_whose_section_read_failed(
        adversarial, monkeypatch, path):
    """End to end on a real database: the headline builds, one section's read
    fails. Before this round the audit recorded that section as PASSED."""
    _exec("INSERT INTO keyword_daily_facts (source_date, customer_id, campaign_id,"
          " campaign_name, ad_group_id, ad_group_name, criterion_id, keyword_text,"
          " match_type, criterion_status, cost_micros, currency_code,"
          " source_system, impressions, clicks, conversions) "
          "VALUES (%s,%s,%s,%s,'ag1','AG','c1','freight software','EXACT',"
          " 'ENABLED',1000000,'GBP','google_ads_api',100,10,1.0)",
          (IN_7D, ACCOUNT, CAMP_A, NAMES[CAMP_A]))
    label = f"section[{path}/30d]"
    findings, per_window = cert.run(("30d",))
    control = {c["check"]: c["ok"] for c in findings.checks}
    assert control[label] is True
    assert per_window["30d"]["sections"][path] == {"available": True, "reason": None}

    module, name = {"flagged": (st_repo, "fetch_search_term_aggregates"),
                    "keyword": (kw_repo, "fetch_keyword_aggregates")}[path]

    def _statement_timeout(*a, **k):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(module, name, _statement_timeout)
    findings, per_window = cert.run(("30d",))
    checks = {c["check"]: c["ok"] for c in findings.checks}
    assert checks[label] is False
    assert any(u.startswith(label) for u in findings.unavailable)
    assert findings.exit_code != cert.EXIT_CERTIFIED
    assert per_window["30d"]["sections"][path]["available"] is False
    assert per_window["30d"]["sections"][path]["reason"] in cert.SECTION_REASONS_FAILURE

    def _oom(*a, **k):
        raise MemoryError()

    monkeypatch.setattr(module, name, _oom)
    with pytest.raises(MemoryError):
        cert.run(("30d",))


@_needs_pg
def test_38_the_narrowed_read_is_one_snapshot(adversarial, monkeypatch):
    """A row committed BETWEEN the candidate statement and the grouped
    statement must not be half-visible. Under READ COMMITTED the grouped
    statement saw it although the statements before it had not."""
    key = ("Freight JOBS", CAMP_A)

    def _rows_for(result):
        return [r for r in result["rows"]
                if (r["search_term"], r["campaign_id"]) == key][0]["row_count"]

    before = st_repo.fetch_search_term_aggregates(None, TODAY, flagged_candidates=True)
    assert _rows_for(before) == 1

    real = st_repo.normalize_search_term
    wrote = []

    def _normalize_after_a_concurrent_commit(value):
        if not wrote:
            # Runs after the first statement's rows are fetched and before the
            # next statement — on ANOTHER connection, committed at once.
            other = adversarial.connect()
            try:
                other.autocommit = True
                with other.cursor() as cur:
                    cur.execute(
                        "INSERT INTO search_terms (source_date, campaign_name,"
                        " campaign_id, ad_group, keyword, match_type, search_term,"
                        " customer_id, spend_usd, clicks, impressions, conversions,"
                        " cost_micros, currency_code, source_system,"
                        " is_flagged_waste, junk_category) VALUES "
                        "(%s,%s,%s,'ag concurrent','kw','BROAD','Freight JOBS',%s,"
                        " 9.0,1,1,0,9000000,'GBP','google_ads_api',TRUE,'job_seeker'),"
                        "(%s,%s,%s,'ag concurrent','kw','BROAD','brand new flagged',%s,"
                        " 9.0,1,1,0,9000000,'GBP','google_ads_api',TRUE,'job_seeker')",
                        (IN_7D, NAMES[CAMP_A], CAMP_A, ACCOUNT,
                         IN_7D, NAMES[CAMP_A], CAMP_A, ACCOUNT))
            finally:
                other.close()
            wrote.append(True)
        return real(value)

    monkeypatch.setattr(st_repo, "normalize_search_term",
                        _normalize_after_a_concurrent_commit)
    during = st_repo.fetch_search_term_aggregates(None, TODAY, flagged_candidates=True)
    assert wrote == [True]
    # Every statement of the read saw the database as it was when it began.
    assert _rows_for(during) == 1
    assert during["search_terms"] == before["search_terms"]
    assert during["source"] == before["source"]
    assert sum(r["row_count"] for r in during["rows"]) == during["source"]["row_count"]
    # The NEXT read sees the commit whole: the extra row AND the new term.
    monkeypatch.setattr(st_repo, "normalize_search_term", real)
    after = st_repo.fetch_search_term_aggregates(None, TODAY, flagged_candidates=True)
    assert _rows_for(after) == 2 and "brand new flagged" in after["search_terms"]
    assert st_repo.NARROWED_READ_TRANSACTION == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


@_needs_pg
def test_39_a_failed_cost_read_is_unavailable_not_published_as_fx_incomplete(
        adversarial, monkeypatch):
    """Without per-date cost a unit cannot be converted, and withholding its
    USD is right. Saying an FX rate is missing — in an available response — is
    not: nothing about FX was ever learned."""
    now = datetime.now(tz=timezone.utc)
    _exec("INSERT INTO keyword_daily_facts (source_date, customer_id, campaign_id,"
          " campaign_name, ad_group_id, ad_group_name, criterion_id, keyword_text,"
          " match_type, criterion_status, cost_micros, currency_code,"
          " source_system, impressions, clicks, conversions) "
          "VALUES (%s,%s,%s,%s,'ag1','AG','c1','freight software','EXACT',"
          " 'ENABLED',1000000,'GBP','google_ads_api',100,10,1.0)",
          (IN_7D, ACCOUNT, CAMP_A, NAMES[CAMP_A]))
    healthy = st_svc.build_campaign_flagged_preview("30d", CAMP_A, now=now)
    assert healthy["available"] is True
    assert {r["currency_status"] for r in healthy["rows"]} == {"verified"}
    assert kw_svc.build_campaign_keyword_preview("30d", CAMP_A, now=now)["available"] is True

    failed = lambda *a, **k: {"available": False, "rows": []}  # noqa: E731
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_costs", failed)
    monkeypatch.setattr(kw_repo, "fetch_keyword_daily_costs", failed)

    for population in st_svc.POPULATIONS:
        payload = st_svc.build_flagged_search_terms("30d", now=now, population=population)
        assert payload["db_unavailable"] is True and payload["rows"] == [], population
        assert payload["pagination"]["total_count"] is None
    assert st_svc.build_search_term_evidence("30d", now=now)["db_unavailable"] is True
    section = st_svc.build_campaign_flagged_preview("30d", CAMP_A, now=now)
    assert (section["available"], section["reason"]) == (
        False, st_svc.FLAGGED_PREVIEW_UNAVAILABLE_SOURCE)
    assert section["total_count"] is None and section["rows"] == []

    assert kw_svc.build_keyword_evidence("30d", now=now)["db_unavailable"] is True
    section = kw_svc.build_campaign_keyword_preview("30d", CAMP_A, now=now)
    assert (section["available"], section["reason"]) == (
        False, kw_svc.PREVIEW_UNAVAILABLE_SOURCE)
    assert section["total_count"] is None and section["rows"] == []


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Nothing flagged at all — production's state on 2026-11-22
# ═════════════════════════════════════════════════════════════════════════════
#
# A read-only production query on 2026-11-22 found 66,455 distinct terms,
# 101,852 term × campaign units, ZERO rows with is_flagged_waste = TRUE and an
# EMPTY waste_terms. So the narrowed read loads nothing there today. That must
# be a measurement — available, zero rows, a true zero total — and must never
# be confused with a read that failed.

@_needs_pg
def test_40_nothing_flagged_is_an_available_section_with_a_true_zero(adversarial):
    _exec("UPDATE search_terms SET is_flagged_waste = FALSE, junk_category = NULL,"
          " matched_pattern = NULL")
    _exec("DELETE FROM waste_terms")
    now = datetime.now(tz=timezone.utc)
    narrowed = st_repo.fetch_search_term_aggregates(None, TODAY, flagged_candidates=True)
    assert narrowed["available"] is True
    assert narrowed["search_terms"] == [] and narrowed["rows"] == []
    assert narrowed["source"]["row_count"] == 0
    for window in WINDOWS:
        complete, lean = _views(window, now, campaign=CAMP_A, page_size=500)
        assert lean.pop("population_scope") == "flagged_candidates"
        assert lean == complete
        section = st_svc.build_campaign_flagged_preview(window, CAMP_A, now=now)
        assert section["available"] is True and section["reason"] is None
        assert section["rows"] == [] and section["total_count"] == 0
        assert section["truncated"] is False
        assert section["coverage_status"] == complete["truth_state"]["status"]
    findings, per_window = cert.run(("30d",))
    checks = {c["check"]: c["ok"] for c in findings.checks}
    assert checks["section[flagged/30d]"] is True
    assert per_window["30d"]["sections"]["flagged"] == {"available": True, "reason": None}


#: Resident growth the flagged section may add when nothing is flagged, MB.
UNFLAGGED_PREVIEW_GROWTH_LIMIT_MB = 60


@pytest.fixture(scope="module")
def unflagged_scale_db(scale_db):
    """The production-scale dataset with production's flag state: no flagged
    row and no annotation. A copy of the seeded database in the same cluster."""
    name = "app_unflagged"
    admin = scale_db.connect()
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                        " WHERE datname = 'app' AND pid <> pg_backend_pid()")
    finally:
        admin.close()
    import psycopg2
    base = scale_db.url.rsplit("/", 1)[0]
    maint = psycopg2.connect(base + "/postgres")
    try:
        maint.autocommit = True
        with maint.cursor() as cur:
            cur.execute(f"CREATE DATABASE {name} TEMPLATE app")
    finally:
        maint.close()
    url = base + "/" + name
    conn = psycopg2.connect(url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("UPDATE search_terms SET is_flagged_waste = FALSE,"
                        " junk_category = NULL, matched_pattern = NULL"
                        " WHERE is_flagged_waste IS TRUE")
            cur.execute("TRUNCATE waste_terms")
            cur.execute("ANALYZE search_terms")
            cur.execute("SELECT count(*), count(*) FILTER (WHERE is_flagged_waste)"
                        " FROM search_terms")
            assert cur.fetchone() == (228_043, 0)
    finally:
        conn.close()
    return url


@_needs_pg
def test_41_at_scale_with_nothing_flagged_the_section_is_available_zero_and_small(
        unflagged_scale_db):
    m = _run_child(_MEASURE, unflagged_scale_db, "flagged", "all_time", SCALE_CAMPAIGN)
    assert m["available"] is True and m["reason"] is None, m
    assert m["rows"] == 0 and m["total_count"] == 0, m
    growth = m["peak_mb"] - m["before_mb"]
    assert growth <= UNFLAGGED_PREVIEW_GROWTH_LIMIT_MB, (
        f"with nothing flagged the section added {growth:.0f} MB: {m}")


@_needs_pg
def test_42_at_scale_with_nothing_flagged_the_real_audit_holds_with_sections_built(
        unflagged_scale_db, monkeypatch):
    import scripts.audit_platform_certification as apc
    from services.platform_certification_service import summarize
    monkeypatch.setenv("DATABASE_URL", unflagged_scale_db)
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    module = "scripts.audit_campaign_evidence_certification"
    rc, out, err, error, memory = apc._run_capped(module, 900, memory_mb=AUDIT_BUDGET_MB)
    outcome = apc.classify(rc, out, err, error=error, memory=memory)
    assert outcome["outcome"] == apc.HOLDS, (outcome.get("detail"), memory)
    stored = summarize({"audits": [{**outcome, "key": "campaign_evidence"}]})
    sections = stored["audits"][0]["drawer_sections"]
    assert set(sections) == set(WINDOWS)
    for window in WINDOWS:
        assert sections[window] == {"keyword": {"available": True, "reason": None},
                                    "flagged": {"available": True, "reason": None}}, window
