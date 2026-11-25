"""
tests/test_pr_ads_172a_campaign_evidence_memory.py

PR-ADS-172A — the search-term and keyword READS behind the (retired) Campaign
Evidence drawer sections must not hold a fact table in Python.

PR-ADS-177C retired Campaign Evidence, its drawer, its two section previews
(``build_campaign_flagged_preview`` / ``build_campaign_keyword_preview``) and
its certification (``scripts.audit_campaign_evidence_certification``). The cases
that exercised those are removed (``docs/60`` §9.6). What stays are the
reader-level guarantees, which the live Keywords & Search Terms page still
relies on:

  1. PURE — the folded per-date reads carry exactly the amounts the per-day
     rows carried; an unknown population is refused; a CRM that carries exact
     queries gets the complete population.
  2. EQUIVALENCE, on a real PostgreSQL — the narrowed flagged population
     publishes the SAME flagged view as the complete one, value for value.
  3. A reader never turns out-of-memory into "unavailable".

The production-scale fixture (``scale_db``) and ``_child_env`` are shared with
``tests/test_pr_ads_173a_search_terms_pages_memory.py``.

Read-only against every external platform; the only writes are local seeds.
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
def test_23_the_narrowed_view_equals_the_complete_one_at_scale(scale_db):
    out = _run_child(_EQUIVALENT_AT_SCALE, scale_db.url, "30d",
                     SCALE_CAMPAIGN, "23094767503")
    for campaign, result in out.items():
        assert result["equal"] is True, (campaign, result)
        assert result["scope"] == "flagged_candidates"
        assert result["total_count"] > 10, result
        assert result["attached"] > 0 and result["unresolved"] > 0, result


# ═════════════════════════════════════════════════════════════════════════════
# 4 · A section that was NOT BUILT is not a section that held (review round)
# ═════════════════════════════════════════════════════════════════════════════
#
# "The audit holds in little memory" is also what an audit looks like when its
# sections were never built. Before this round a failed read — and a swallowed
# MemoryError — came back as `available: False` with a reason, and the gate
# passed any unavailable section that carried one.

# PR-ADS-177C: the Campaign Evidence certification (and its drawer-section
# checks) is retired with the page; the reader-level cases below stay.


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


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Nothing flagged at all — production's state on 2026-11-22
# ═════════════════════════════════════════════════════════════════════════════
#
# A read-only production query on 2026-11-22 found 66,455 distinct terms,
# 101,852 term × campaign units, ZERO rows with is_flagged_waste = TRUE and an
# EMPTY waste_terms. So the narrowed read loads nothing there today. That must
# be a measurement — available, zero rows, a true zero total — and must never
# be confused with a read that failed.


#: Resident growth the flagged section may add when nothing is flagged, MB.
UNFLAGGED_PREVIEW_GROWTH_LIMIT_MB = 60
