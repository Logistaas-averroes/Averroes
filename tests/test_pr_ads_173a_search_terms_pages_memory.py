"""
tests/test_pr_ads_173a_search_terms_pages_memory.py

PR-ADS-173A — the Search Terms pages must not build the account's whole window
as Python dicts on every request.

Production is one web process on a 512 MB instance. `/api/search-term-evidence`
(Terms), `/flagged`, `/patterns`, `/patterns/detail`, `/term`, `/export` and the
`/api/waste` adapter each built every term × campaign unit of the selected
window — every grouped `search_terms` row as a dict, every (term, campaign, day)
cost row as a dict, and one 25-key dict holding ten sets per unit. On a
production-scale synthetic dataset that was ≈ 675 MB of resident growth per
request at `all_time` (docs/54_*): a signed-in user choosing "All time" and
opening Search Terms could get the server killed.

Four kinds of proof here:

  1. PURE — the compact unit reads exactly as the dict it replaces; the page
     slice equals the slice of the full sort; the streamed and the row-list
     readers build the same population; the per-status attribution state equals
     the per-unit one.
  2. EQUALITY WITH THE PRE-CHANGE CODE, on a real PostgreSQL — 364 requests
     (every route; windows, sorts, filters, pages 2+, last page, past the end,
     empty results, sort-key ties, mixed / missing currency, a missing FX day,
     spellings that normalize together, two campaigns sharing a name, a
     quarantined population, a population with nothing flagged) publish payloads
     EQUAL AS WHOLE DOCUMENTS to the ones the parent commit published. The
     expected payloads were captured from the parent commit by
     `tests/pr_ads_173a_scenarios.py` and are committed under `tests/fixtures/`;
     they are not what this implementation was observed to produce.
  3. MEASURED, on a production-SCALE synthetic PostgreSQL dataset (the live
     tables' row counts) — each route's peak resident growth at `180d` and
     `all_time` is bounded. These fail against the pre-change code.
  4. FAILURE PATHS — a failed cost read and a failed FX read are unavailable on
     every route, out of memory is raised on every route, a consumer's own
     failure is not reported as the source being unavailable, and the whole
     build reads one snapshot.

Read-only against every external platform; the only writes are local seeds.

Run with:
    python -m pytest tests/test_pr_ads_173a_search_terms_pages_memory.py -v
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import db.revenue_repository as revenue_repo  # noqa: E402
import db.search_term_repository as st_repo  # noqa: E402
import services.platform_sql_attribution_service as psa  # noqa: E402
import services.search_term_evidence_service as st_svc  # noqa: E402
from tests import pr_ads_173a_scenarios as matrix  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, _PgCluster, pg,
)
from tests.test_pr_ads_172a_campaign_evidence_memory import (  # noqa: E402,F401
    _child_env, scale_db,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

ACCOUNT = matrix.ACCOUNT
NOW = matrix.NOW
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_SCENARIOS = Path(__file__).resolve().parent / "pr_ads_173a_scenarios.py"


# ═════════════════════════════════════════════════════════════════════════════
# 1 · PURE — the compact structures are the structures they replace
# ═════════════════════════════════════════════════════════════════════════════

def _group(term, campaign_id="1", name="uk - brand", **kw):
    row = {"search_term": term, "campaign_name": name, "campaign_id": campaign_id,
           "spend_usd": 2.5, "cost_micros": 2_500_000, "currency_codes": ["GBP"],
           "source_systems": ["google_ads_api"], "clicks": 3, "impressions": 40,
           "conversions": 1.0, "row_count": 2,
           "first_seen": date(2026, 11, 1), "last_seen": date(2026, 11, 20),
           "any_flagged": False, "any_unreviewed": True,
           "junk_categories": [], "matched_patterns": [], "ad_groups": ["ag 1"],
           "keywords": ["kw"], "match_types": ["BROAD"]}
    row.update(kw)
    return row


def _dict_unit(term, norm, key, name, status):
    """The unit exactly as `_build_population` created it before PR-ADS-173A."""
    return {
        "search_term": term, "search_term_normalized": norm,
        "search_term_variants": set(), "campaign_key": key, "campaign_name": name,
        "mapping_status": status, "spend_usd": None, "spend_raw": None,
        "clicks": 0, "impressions": 0, "conversions": None, "row_count": 0,
        "cost_micros": None, "currency_codes": set(), "source_systems": set(),
        "first_seen": None, "last_seen": None, "any_flagged": False,
        "any_unreviewed": False, "junk_categories": set(),
        "matched_patterns": set(), "ad_groups": set(), "keywords": set(),
        "match_types": set(), "source_labels": set(), "campaign_ids": set(),
        "_daily_micros": {},
    }


_GROUPS_OF_ONE_UNIT = [
    _group("Freight JOBS", any_flagged=True, junk_categories=["job_seeker"],
           matched_patterns=["jobs"]),
    _group("freight jobs", spend_usd=None, cost_micros=None, conversions=None,
           ad_groups=["ag 2", "ag 3"], keywords=["jobs", "kw"],
           first_seen=date(2026, 10, 1), last_seen=date(2026, 10, 2)),
    _group("freight  jobs", name="uk - brand (old)", currency_codes=["EUR", "GBP"],
           match_types=["EXACT", "PHRASE"], any_unreviewed=False,
           last_seen=date(2026, 11, 21)),
]


def _both_units(groups):
    old = _dict_unit(groups[0]["search_term"], "freight jobs", "1", "uk - brand", "mapped")
    new = st_svc._Unit(groups[0]["search_term"], "freight jobs", "1", "uk - brand", "mapped")
    for g in groups:
        st_svc._merge_group(old, dict(g))
        st_svc._merge_group(new, dict(g))
    for iso, micros in (("2026-11-20", 1_000_000), ("2026-11-01", 500_000),
                        ("2026-11-20", 250_000)):
        old["_daily_micros"][iso] = old["_daily_micros"].get(iso, 0) + micros
        new.add_daily_micros(iso, micros)
    for unit in (old, new):
        unit["search_term"] = st_svc._representative_term(
            unit["search_term_variants"], unit["search_term_normalized"])
    old["_provenance"] = st_svc._assess_currency_provenance(old)
    return old, new


@pytest.mark.parametrize("groups", [_GROUPS_OF_ONE_UNIT, _GROUPS_OF_ONE_UNIT[:1],
                                    _GROUPS_OF_ONE_UNIT[:2]])
def test_01_a_compact_unit_reads_exactly_as_the_dict_it_replaces(monkeypatch, groups):
    """The same groups merged by the same `_merge_group` into the pre-173A dict
    and into the compact unit: every field equal, the same keys present, and
    every function that reads a unit gives the same answer for both."""
    monkeypatch.setattr(
        revenue_repo, "fetch_fx_rates",
        lambda s, e, b, q: {"available": True, "rates": {
            "2026-11-20": 1.25, "2026-11-01": 1.3}})
    old, new = _both_units(groups)
    assert sorted(new.keys()) == sorted(old.keys())
    for key in old:
        assert new[key] == old[key], key
        assert new.get(key) == old.get(key), key
    assert st_svc._assess_currency_provenance(new) == st_svc._assess_currency_provenance(old)
    st_svc._fx_convert_population([old], None, date(2026, 11, 22))
    st_svc._fx_convert_population([new], None, date(2026, 11, 22))
    for key in ("spend_usd", "fx_complete", "currency_status"):
        assert new[key] == old[key], key
    state = psa.unit_state_without_text_evidence("mapped", available=True)
    attr = {"by_unit": {st_svc._st_unit_key(old): state}, "coverage": {"coverage_pct": 12.5}}
    st_svc._apply_search_term_sql([old], attr)
    st_svc._apply_search_term_sql([new], attr)
    assert sorted(new.keys()) == sorted(old.keys())
    for key in old:
        assert new[key] == old[key], key
    assert st_svc._unit_row(new, {"1": {"UK Brand alias"}}) == \
        st_svc._unit_row(old, {"1": {"UK Brand alias"}})
    assert st_svc._unit_state(new) == st_svc._unit_state(old)
    assert st_svc._unit_classification(new) == st_svc._unit_classification(old)
    assert st_svc._monetary_summary([new]) == st_svc._monetary_summary([old])
    assert st_svc.unit_identity(new) == st_svc.unit_identity(old)
    assert {**new} == {**old}


def test_02_what_was_never_set_is_absent_and_nothing_is_stored_silently():
    unit = st_svc._Unit("a", "a", "1", "uk - brand", "mapped")
    # Absent until the pass that sets it has run — exactly as a dict key.
    for key in ("fx_complete", "currency_status", "attributed_sqls",
                "sql_attribution_status", "sql_contact_keys"):
        assert key not in unit and unit.get(key, "missing") == "missing"
        with pytest.raises(KeyError):
            unit[key]
    assert unit.get("_waste_flag") is None and unit.get("_waste_evidence") is None
    assert "_waste_flag" not in unit.keys() and "_waste_evidence" not in unit.keys()
    # A field the unit does not have is refused, never dropped.
    with pytest.raises(KeyError):
        unit["made_up_field"] = 1
    with pytest.raises(KeyError):
        unit["made_up_field"]
    # A set-valued field read from a compact unit cannot be changed in place —
    # the object may be shared by every unit of the population.
    with pytest.raises(AttributeError):
        unit["keywords"].add("x")
    # One attribution field set on its own leaves the others absent.
    unit["attributed_sqls"] = 3
    assert unit["attributed_sqls"] == 3 and "sql_attribution_status" not in unit
    # An attribution state missing a key is completed, as `st.get(key)` did.
    st_svc._apply_search_term_sql(
        [unit], {"by_unit": {st_svc._st_unit_key(unit): {"attributed_sqls": 2}}})
    assert unit["attributed_sqls"] == 2 and unit["sql_attribution_status"] is None
    assert unit["sql_contact_keys"] == []


def _units_for_sorting(count=180, seed=173):
    rng = random.Random(seed)
    units = []
    for i in range(count):
        u = st_svc._Unit(f"term {i % 60}", f"term {i % 60}", str(i % 5), "c", "mapped")
        u["spend_usd"] = rng.choice([None, 1.0, 1.0, 2.5, 2.5, 10.0])
        u["clicks"] = rng.choice([0, 0, 3, 3, 9])
        u["conversions"] = rng.choice([None, 0.0, 1.0])
        u["last_seen"] = rng.choice([None, date(2026, 11, 1), date(2026, 11, 2)])
        u["attributed_sqls"] = rng.choice([None, 0, 1, 1])
        units.append(u)
    # Unique (term, campaign) per unit, as in a real population.
    seen, out = set(), []
    for u in units:
        if (u["search_term_normalized"], u["campaign_key"]) not in seen:
            seen.add((u["search_term_normalized"], u["campaign_key"]))
            out.append(u)
    return out


@pytest.mark.parametrize("sort", st_svc.TERM_SORTS)
def test_03_a_page_is_the_same_slice_of_the_same_order(sort):
    """`_sorted_page` selects with a heap instead of sorting everything; the
    rows, their order and the ties must be exactly the slice of the full sort —
    first page, middle, last, past the end."""
    units = _units_for_sorting()
    full = st_svc._sort_units(units, sort)
    for size in (1, 7, 50, 200):
        for offset in (0, size, 3 * size, len(units) - size - 1, len(units) - 1,
                       len(units), len(units) + 5):
            offset = max(0, offset)
            page = st_svc._sorted_page(units, sort, offset, size)
            assert [id(u) for u in page] == [id(u) for u in full[offset:offset + size]], (
                sort, offset, size)


def _doubles(monkeypatch, groups, *, streamed: bool, daily=None):
    canonical = {"available": True, "customer_id": "c1", "currency_code": "GBP",
                 "reporting_currency": "USD", "fx_complete": True,
                 "total_spend_usd": 100.0,
                 "rows": [{"campaign_id": "1", "campaign_name": "uk - brand"},
                          {"campaign_id": "2", "campaign_name": "uae - competitors"}]}
    source = {"row_count": sum(g["row_count"] for g in groups),
              "spend_usd_total": sum(g["spend_usd"] or 0 for g in groups)}
    daily = daily if daily is not None else [
        {"search_term": g["search_term"], "campaign_name": g["campaign_name"],
         "campaign_id": g["campaign_id"], "source_date": day,
         "cost_micros": g["cost_micros"]}
        for g in groups for day in (date(2026, 11, 20), date(2026, 11, 21))]
    calls = {"groups": [], "costs": []}

    if streamed:
        def _aggregates(start, end, *, sink=None, **kw):
            calls["groups"].append({"sink": sink is not None, **kw})
            for g in groups:
                sink(dict(g))
            return {"available": True, "rows": [], "source": source}

        def _costs(start, end, *, sink=None, **kw):
            calls["costs"].append({"sink": sink is not None, **kw})
            for d in daily:
                sink(d["search_term"], d["campaign_name"], d["campaign_id"],
                     d["source_date"], d["cost_micros"])
            return {"available": True, "rows": []}
    else:
        # The two-argument stand-ins every pre-173A test installs.
        def _aggregates(start, end):
            calls["groups"].append({"sink": False})
            return {"available": True, "rows": [dict(g) for g in groups],
                    "source": source}

        def _costs(start, end):
            calls["costs"].append({"sink": False})
            return {"available": True, "rows": [dict(d) for d in daily]}

    monkeypatch.setattr(st_repo, "fetch_search_term_aggregates", _aggregates)
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_costs", _costs)
    monkeypatch.setattr(st_repo, "fetch_waste_evidence_for_terms",
                        lambda terms: {"available": True, "rows": [
                            {"search_term": "tms course", "campaign_name": "uk - brand",
                             "junk_category": "education", "matched_pattern": "course",
                             "crm_junk_confirmed": 1, "run_date": "2026-11-10"}]})
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *a, **k: canonical)
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(
        revenue_repo, "fetch_fx_rates",
        lambda s, e, b, q: {"available": True, "rates": {
            "2026-11-20": 1.25, "2026-11-21": 1.3}})
    monkeypatch.setattr(st_svc, "_search_term_sql_attribution",
                        lambda pop, s, e: {"by_unit": {}, "reconciliation": {},
                                           "coverage": {}, "audit": {},
                                           "available": False, "contacts": [],
                                           "population_has_text": False})
    monkeypatch.setattr(st_svc, "_canonical_st_reconciliation", lambda w, n: {})
    return calls


_POPULATION_GROUPS = _GROUPS_OF_ONE_UNIT + [
    _group("freight jobs", campaign_id="2", name="uae - competitors"),
    _group("tms course"), _group("usd term", currency_codes=["USD"]),
    _group("no currency", currency_codes=[]),
    _group("unknown campaign", campaign_id="9", name="label nine"),
]


def test_04_a_streaming_reader_and_a_row_list_reader_build_one_population(monkeypatch):
    """The reader hands its rows to a sink when it offers one, and returns them
    when it does not (the two-argument stand-ins older tests install). Either
    way it is one merge: the same units, field for field, and the same pages."""
    built = {}
    for streamed in (False, True):
        calls = _doubles(monkeypatch, _POPULATION_GROUPS, streamed=streamed)
        pop = st_svc._build_population(None, date(2026, 11, 22))
        assert pop["available"] is True
        assert calls["groups"] == [{"sink": streamed}]
        assert calls["costs"] == [{"sink": streamed}]
        built[streamed] = {
            "units": [{k: u[k] for k in u.keys()} for u in pop["units"]],
            "source": pop["source"], "annotation_join": pop["annotation_join"],
            "currency_info": pop["currency_info"],
            "terms": st_svc.build_search_term_evidence("all_time", now=NOW, page_size=3),
            "patterns": st_svc.build_search_pattern_evidence("all_time", now=NOW, n=1),
            "flagged": st_svc.build_flagged_search_terms("all_time", now=NOW),
            "drawer": st_svc.build_search_term_drawer("all_time", "FREIGHT jobs", now=NOW),
        }
    assert built[True] == built[False]
    # Not a comparison of two empty things.
    assert len(built[True]["units"]) == 6
    assert built[True]["flagged"]["pagination"]["total_count"] == 2
    assert {u["currency_status"] for u in built[True]["units"]} == {
        "verified", "verified_same_currency", "mixed_currency",
        "legacy_currency_unverified"}


def test_05_equal_sets_are_one_object_for_the_whole_population(monkeypatch):
    """What makes ≈ 100,000 units affordable: a unit does not own a private
    copy of `{"GBP"}`. Equal set-valued fields are the same object."""
    groups = [_group(f"term {i}", ad_groups=["ag 1"], keywords=["kw"]) for i in range(50)]
    _doubles(monkeypatch, groups, streamed=True)
    units = st_svc._build_population(None, date(2026, 11, 22))["units"]
    assert len(units) == 50 and all(type(u) is st_svc._Unit for u in units)
    for key in ("currency_codes", "source_systems", "ad_groups", "keywords",
                "match_types", "source_labels", "campaign_ids", "junk_categories"):
        assert len({id(u[key]) for u in units}) == 1, key
    # …and a unit with one spelling stores the string, not a set of it.
    assert all(type(u._variants) is str for u in units)
    assert not hasattr(units[0], "__dict__")


def _contact(key, campaign_id, text=None):
    return {"contact_key": key, "contact_id": key, "company": "co",
            "sql_date": "2026-11-10", "campaign_label": "uk - brand",
            "campaign_mapping_status": "mapped" if campaign_id else "unmatched",
            "campaign_key": campaign_id, "canonical_campaign_id": campaign_id,
            "keyword_raw": "kw", "keyword_norm": "kw", "search_term_raw": text,
            "search_term_norm": psa.normalize_term(text), "has_gclid": True}


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("leads_flag", [True, False])
def test_06_without_a_query_the_per_status_state_is_the_per_unit_state(
        monkeypatch, available, leads_flag):
    """No contact carries a query, so the service asks for a unit's attribution
    state per mapping STATUS. It must be what attributing every unit one by one
    answers — every unit field and every response-level block."""
    contacts = [_contact("c1", "1"), _contact("c2", None), _contact("c3", "2")]
    monkeypatch.setattr(psa, "fetch_and_resolve_contacts",
                        lambda s, e: {"available": available, "contacts": contacts,
                                      "leads_have_search_term": leads_flag})
    units = []
    for i, status in enumerate(["mapped", "mapped", "unmatched", "not_google_ads"]):
        u = st_svc._Unit(f"term {i}", f"term {i}", str(i), "c", status)
        units.append(u)
    attr = st_svc._search_term_sql_attribution({"units": units}, None, date(2026, 11, 22))
    assert "unit_state_by_mapping_status" in attr and attr["by_unit"] == {}

    reference = psa.attribute_search_terms(
        contacts, [{"unit_key": st_svc._st_unit_key(u),
                    "campaign_id": u["campaign_key"] if u["mapping_status"] == "mapped" else None,
                    "search_term": u["search_term"],
                    "mapping_status": u["mapping_status"]} for u in units],
        available=available, leads_have_search_term=leads_flag)
    for block in ("reconciliation", "coverage", "completeness", "audit",
                  "available", "population_has_text"):
        assert attr[block] == reference[block], block
    twins = [st_svc._Unit(u["search_term"], u["search_term_normalized"],
                          u["campaign_key"], "c", u["mapping_status"]) for u in units]
    st_svc._apply_search_term_sql(units, attr)
    st_svc._apply_search_term_sql(twins, reference)
    for unit, twin in zip(units, twins):
        for key in st_svc._UNIT_SQL_KEYS + ("sql_attribution_coverage",):
            assert unit[key] == twin[key], (unit["mapping_status"], key)
    assert st_svc._search_term_sql_block(attr) == st_svc._search_term_sql_block(reference)


def test_07_a_single_contact_with_a_query_attributes_every_unit_individually(monkeypatch):
    """The per-status shortcut is for a CRM with no query at all. One contact
    carrying one is enough to take the ordinary path — even when the `leads`
    capability flag still says there are none."""
    for leads_flag in (True, False):
        contacts = [_contact("c1", "1", "term 0"), _contact("c2", "1")]
        monkeypatch.setattr(psa, "fetch_and_resolve_contacts",
                            lambda s, e, _f=leads_flag: {
                                "available": True, "contacts": contacts,
                                "leads_have_search_term": _f})
        units = [st_svc._Unit(f"term {i}", f"term {i}", "1", "c", "mapped")
                 for i in range(3)]
        attr = st_svc._search_term_sql_attribution(
            {"units": units}, None, date(2026, 11, 22))
        assert "unit_state_by_mapping_status" not in attr
        assert set(attr["by_unit"]) == {st_svc._st_unit_key(u) for u in units}
        st_svc._apply_search_term_sql(units, attr)
        if leads_flag:
            assert units[0]["attributed_sqls"] == 1
            assert units[0]["sql_contact_keys"] == ["c1"]
        assert attr["reconciliation"]["uniquely_attributed_sql_contacts"] == 1


class _Recorder(dict):
    """A full flagged row that records which of its keys are read."""

    read: set = set()

    def get(self, key, default=None):
        type(self).read.add(key)
        return dict.get(self, key, default)

    def __getitem__(self, key):
        type(self).read.add(key)
        return dict.__getitem__(self, key)


def test_08_the_flagged_index_carries_every_field_the_view_reads_from_all_rows(
        monkeypatch):
    """The flagged view keeps `_FLAGGED_INDEX_KEYS` of every flagged row and
    builds full rows for the returned page only. So every key its filters,
    sorts, KPIs and facets read must be one of them — and an index row must
    REFUSE any other key rather than answer None."""
    _doubles(monkeypatch, _POPULATION_GROUPS, streamed=True)
    payload = st_svc.build_flagged_search_terms("all_time", now=NOW)
    assert payload["rows"], "the fixture must publish flagged rows"
    rows = [_Recorder(r) for r in payload["rows"]]
    _Recorder.read = set()
    for kw in ({}, {"q": "jobs"}, {"campaign": "1"}, {"review_state": "unreviewed"},
               {"flag_reason": "job_seeker"}, {"min_spend": 1.0},
               {"sql_state": "has_sql"}, {"sql_state": "known_zero"},
               {"sql_state": "unavailable"}):
        st_svc._filter_flagged_rows(rows, **kw)
    for sort in st_svc.FLAGGED_SORTS:
        sorted(rows, key=st_svc._flagged_sort_key(sort))
    st_svc._flagged_kpis(rows, st_svc._monetary_summary([]), {})
    st_svc._flagged_facets(rows)
    assert _Recorder.read <= set(st_svc._FLAGGED_INDEX_KEYS), (
        _Recorder.read - set(st_svc._FLAGGED_INDEX_KEYS))
    assert _Recorder.read, "nothing was read — the probe proves nothing"

    index = st_svc._FlaggedIndexRow(
        (k, payload["rows"][0][k]) for k in st_svc._FLAGGED_INDEX_KEYS)
    assert index.get("clicks") == payload["rows"][0]["clicks"]
    for unread in ("impressions", "flag_reasons", "not_a_field"):
        with pytest.raises(KeyError):
            index.get(unread)
        with pytest.raises(KeyError):
            index[unread]
    # The page's rows are FULL rows, built by the same call on the same unit.
    assert all("flag_reasons" in r and "priority_reasons" in r for r in payload["rows"])


def _reference_pattern_kpis(term_units, rows, membership):
    """The pre-173A overlap arithmetic, verbatim: per term, the number of
    surviving pattern ROWS whose pattern is among the term's grams."""
    surviving_patterns = {p["pattern"] for p in rows}
    surviving_terms = {t for t, grams in membership.items()
                       if surviving_patterns & set(grams)}
    overlapping = sum(
        1 for t, grams in membership.items()
        if t in surviving_terms
        and sum(1 for p in rows if p["pattern"] in set(grams)) > 1)
    return len(surviving_terms), overlapping


@pytest.mark.parametrize("n", [1, 2, 3])
@pytest.mark.parametrize("min_terms", [None, 2, 4, 99])
def test_09_pattern_overlap_counts_equal_the_per_row_definition(monkeypatch, n, min_terms):
    words = ["freight", "jobs", "software", "free", "tms", "course"]
    rng = random.Random(n * 10 + (min_terms or 0))
    groups = [_group(" ".join(rng.choice(words) for _ in range(rng.randint(1, 5))),
                     campaign_id=str(1 + i % 2),
                     name=("uk - brand", "uae - competitors")[i % 2])
              for i in range(70)]
    _doubles(monkeypatch, groups, streamed=True)
    out = st_svc.build_search_pattern_evidence(
        "all_time", now=NOW, n=n, min_terms=min_terms, limit=500)
    pop = st_svc._build_population(None, date(2026, 11, 22))
    term_units = st_svc._term_units_for_patterns(pop["units"])
    patterns, membership = st_svc._build_patterns(term_units, n)
    rows = [p for p in patterns.values() if min_terms is None or p["terms"] >= min_terms]
    terms_analysed, overlapping = _reference_pattern_kpis(term_units, rows, membership)
    assert out["kpis"]["terms_analysed"] == terms_analysed
    assert out["overlap"]["overlapping_term_count"] == overlapping
    assert out["overlap"]["total_pattern_memberships"] == sum(p["terms"] for p in rows)
    assert out["kpis"]["patterns_found"] == len(rows)


def test_10_export_rows_are_the_list_they_replace(monkeypatch):
    """The export's `rows` build each row when it is read. It must still BE the
    sequence it replaces: same length, same rows, same order, indexable."""
    _doubles(monkeypatch, _POPULATION_GROUPS, streamed=True)
    out = st_svc.build_search_term_export("all_time", now=NOW, sort="term")
    rows = out["rows"]
    pop = st_svc._build_population(None, date(2026, 11, 22))
    st_svc._apply_search_term_sql(pop["units"], st_svc._search_term_sql_attribution(
        pop, None, date(2026, 11, 22)))
    expected = [st_svc._unit_row(u, pop["aliases_by_id"])
                for u in st_svc._sort_units(pop["units"], "term")]
    assert out["complete"] is True and len(rows) == len(expected) == 6
    assert list(rows) == expected and rows == expected
    assert rows[0] == expected[0] and rows[-1] == expected[-1]
    assert rows[1:3] == expected[1:3]
    assert list(rows) == list(rows), "reading the rows twice gives the same rows"
    assert rows != expected[:-1]


def test_11_a_reader_is_asked_for_what_its_signature_offers():
    assert st_repo.reader_accepts(st_repo.fetch_search_term_aggregates, "sink")
    assert st_repo.reader_accepts(st_repo.fetch_search_term_aggregates, "normalized_terms")
    assert st_repo.reader_accepts(st_repo.fetch_search_term_daily_costs, "sink")
    assert not st_repo.reader_accepts(lambda a, b: None, "sink")
    assert st_repo.reader_accepts(lambda *a, **k: None, "sink")
    assert not st_repo.reader_accepts(object(), "sink")


# ═════════════════════════════════════════════════════════════════════════════
# 2 · EQUALITY WITH THE PRE-CHANGE CODE — 364 requests on a real PostgreSQL
# ═════════════════════════════════════════════════════════════════════════════

def _golden() -> tuple[dict, dict]:
    index = json.loads((_FIXTURES / "pr_ads_173a_golden_index.json").read_text())
    raw = gzip.decompress((_FIXTURES / index["payloads_file"]).read_bytes())
    return index, {"raw": raw, "payloads": json.loads(raw)}


FAMILIES = ("terms", "flagged", "waste", "preview", "patterns", "pattern_detail",
            "term", "export", "quarantine", "nothing_flagged")


def _family(name: str) -> str:
    head = name.split("/")[0]
    return head.split(":")[0] if ":" in head else head


def test_20_the_golden_payloads_are_the_parent_commits_and_are_not_trivial():
    """What the equality below is taken against. The payloads were captured
    from the PARENT commit; the index records which, and summarises each
    request so a reader can see the matrix is not 364 empty answers."""
    index, golden = _golden()
    assert index["captured_from_commit"] == "6c1cb0480c97fd110d1145be9a279b8e7b73159f"
    assert hashlib.sha256(golden["raw"]).hexdigest() == index["payloads_sha256"]
    payloads = golden["payloads"]
    assert len(payloads) == index["scenario_count"] == 364
    assert set(payloads) == set(index["scenarios"])
    for name, payload in payloads.items():
        assert matrix.digest(payload) == index["scenarios"][name]["sha256"], name
    assert {_family(n) for n in payloads} == set(FAMILIES)

    def rows(name):
        return payloads[name]["rows"]

    # Pages 2+, the last page, past the end, an empty result.
    assert payloads["terms/all_time/page=2"]["pagination"]["has_more"] is True
    last = payloads["terms/all_time/page=16"]["pagination"]
    assert last["returned_count"] == 2 and last["has_more"] is False
    assert payloads["terms/all_time/page=15"]["pagination"]["has_more"] is True
    assert rows("terms/all_time/page=17") == [] == rows("terms/all_time/page=40")
    assert payloads["terms/all_time/page=40"]["pagination"]["total_count"] > 90
    assert payloads["terms/all_time/q=none"]["pagination"]["total_count"] == 0
    assert payloads["flagged/all_time/page=3"]["pagination"]["returned_count"] == 5
    # Sort-key ties on the published page.
    tied = [r for r in rows("terms/all_time/page_size=200")
            if r["search_term"].startswith("tie ")]
    assert len(tied) == 10 and len({(r["spend_usd"], r["clicks"]) for r in tied}) == 1
    assert [r["search_term"] for r in tied][4:] == [f"tie term {i}" for i in range(1, 7)]
    # …including a flagged sort key (the term) shared by two campaigns.
    same_key = rows("flagged/all_time/campaign=name")
    assert [(r["search_term"], r["campaign_key"]) for r in same_key] == [
        ("tie jobs", matrix.CAMP_C), ("tie jobs", matrix.CAMP_D)]
    assert len({(r["spend_usd"], r["priority_score"]) for r in same_key}) == 1
    # Mixed / missing currency, a missing FX day, native USD.
    statuses = {r["currency_status"] for r in rows("terms/all_time/page_size=200")}
    assert statuses >= {"verified", "verified_same_currency", "fx_incomplete",
                        "mixed_currency", "legacy_currency_unverified"}
    assert payloads["terms/all_time/sort=spend"]["kpis"]["monetary_status"] == "partial"
    # Spellings that normalize together are one row carrying its variants.
    merged = [r for r in rows("terms/all_time/q=Freight JOBS")
              if r["campaign_key"] == matrix.CAMP_A][0]
    assert len(merged["search_term_variants"]) == 3 and merged["source_rows"] == 4
    # Two campaigns sharing a display name stay two facets and two rows.
    shared = [c for c in payloads["terms/all_time/sort=spend"]["facets"]["campaigns"]
              if c["campaign_name"] == matrix.NAMES[matrix.CAMP_C]]
    assert [c["campaign_key"] for c in shared] == [matrix.CAMP_C, matrix.CAMP_D]
    assert len(payloads["term/all_time/erp login/None"]["campaigns"]) == 2
    # The flagged view: rows, an annotation join with unplaceable rows, reviews.
    flagged = payloads["flagged/all_time/sort=priority"]
    assert flagged["pagination"]["total_count"] > 20
    assert flagged["annotation_join"]["attached"] > 0
    assert flagged["annotation_join"]["legacy_unresolved"] > 0
    assert rows("flagged/all_time/review=keep") and rows("flagged/all_time/review=resolved")
    # A quarantined population and a population with nothing flagged.
    assert payloads["quarantine:flagged/all_time"]["actionable"] is False
    assert payloads["nothing_flagged:flagged/all_time"]["pagination"]["total_count"] == 0
    # The export is the complete filtered population, one line per unit.
    export = payloads["export/all_time"]["csv"]
    assert export.count("\r\n") == 1 + payloads["terms/all_time/sort=spend"][
        "pagination"]["total_count"]
    # Errors are part of the contract.
    assert payloads["terms/error/sort"]["__raised__"] == "SearchTermQueryError"
    assert payloads["terms/error/window"]["__raised__"] == "EvidenceWindowError"


@pytest.fixture(scope="module")
def published():
    """Every request of the matrix, answered by THIS tree on a fresh cluster.
    Run in a child process so the suite's own connection pool is never pointed
    at it — and because that is exactly how the golden file was produced."""
    cluster = _PgCluster().start()
    try:
        proc = subprocess.run(
            [sys.executable, str(_SCENARIOS)], cwd=_ROOT, env=_child_env(cluster.url),
            capture_output=True, text=True, timeout=1800)
        assert proc.returncode == 0, proc.stderr[-3000:]
        yield json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        cluster.stop()


def _first_difference(expected, actual, path="$"):
    if type(expected) is not type(actual):
        return f"{path}: {expected!r} != {actual!r}"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                return f"{path}.{key}: present on one side only"
            found = _first_difference(expected[key], actual[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: {len(expected)} items != {len(actual)} items"
        for i, (a, b) in enumerate(zip(expected, actual)):
            found = _first_difference(a, b, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if expected == actual else f"{path}: {expected!r} != {actual!r}"


@_needs_pg
def test_21_the_same_requests_are_answered(published):
    """No request of the matrix silently dropped, none added unrecorded."""
    _, golden = _golden()
    assert set(published) == set(golden["payloads"])


@_needs_pg
@pytest.mark.parametrize("family", FAMILIES)
def test_22_every_payload_equals_the_one_the_parent_commit_published(published, family):
    """Whole-document equality, request by request: every number, row, total,
    facet, KPI, sort order and key. Nothing is normalized, rounded, sorted or
    removed before comparing."""
    _, golden = _golden()
    names = [n for n in golden["payloads"] if _family(n) == family]
    assert names, family
    differing = {}
    for name in names:
        if published[name] != golden["payloads"][name]:
            differing[name] = _first_difference(golden["payloads"][name], published[name])
    assert not differing, (
        f"{len(differing)} of {len(names)} {family} payloads differ from the "
        f"parent commit's: " + "; ".join(f"{k} → {v}" for k, v in list(differing.items())[:5]))


# ═════════════════════════════════════════════════════════════════════════════
# 3 · MEASURED on production-scale synthetic data — every route, 180d + all_time
# ═════════════════════════════════════════════════════════════════════════════
#
# The dataset is PR-ADS-172A's (`scale_db`): the live tables' row counts on
# 2026-11-22 with invented content. Nothing here is a production measurement.
#
# Peak resident growth one request may add to a warm process, MB. Measured on
# this dataset (docs/54): before ≈ 673–683 for every route; after — Terms 75,
# Flagged 79, /api/waste 79, Patterns 107, pattern drawer 78, term drawer 2,
# CSV export 95.

GROWTH_LIMIT_MB = {
    "terms": 115, "terms_filtered_page": 115, "flagged": 120, "waste": 120,
    "patterns": 140, "pattern_detail": 115, "term": 30, "export": 130,
}
#: Every route's limit is under the 150 MB the change was asked to stay within.
assert max(GROWTH_LIMIT_MB.values()) < 150

SCALE_TERM = "Freight FREIGHT freight freight 0"       # a head query, 27 campaigns

#: Run in a FRESH interpreter so the peak is this request's and nobody else's.
_MEASURE_ROUTE = r"""
import asyncio, gc, json, os, sys
sys.path.insert(0, os.getcwd())

def _status(field):
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024.0

import db.connection as connection
connection.init_pool()
import api.server as srv                           # as the web process does
import services.search_term_evidence_service as st

route, window, term = sys.argv[1:4]

def call(w):
    if route == "terms":
        return st.build_search_term_evidence(w)
    if route == "terms_filtered_page":
        return st.build_search_term_evidence(w, page=3, sort="clicks", q="freight")
    if route == "flagged":
        return st.build_flagged_search_terms(w)
    if route == "waste":
        return srv.api_waste(user={}, days=30, window=w)
    if route == "patterns":
        return st.build_search_pattern_evidence(w, n=2)
    if route == "pattern_detail":
        return st.build_search_pattern_drawer(w, "freight software", 2)
    if route == "term":
        return st.build_search_term_drawer(w, term)
    if route == "export":
        response = srv.api_search_term_evidence_export(
            user={}, window=w, q=None, campaign=None, state=None,
            junk_category=None, min_spend=None, sort="spend", sql_state=None)
        if hasattr(response, "body_iterator"):
            async def drain():
                lines = size = 0
                async for chunk in response.body_iterator:
                    text = chunk if isinstance(chunk, str) else chunk.decode()
                    lines += text.count("\r\n"); size += len(text)
                return lines, size
            lines, size = asyncio.run(drain())
        else:
            text = response.body.decode()
            lines, size = text.count("\r\n"), len(text)
        return {"csv_lines": lines, "csv_bytes": size}
    raise SystemExit(route)

call("7d")                                         # imports and lazy state
gc.collect()
before = _status("VmRSS")
out = call(window)
facts = {"before_mb": before, "peak_mb": _status("VmHWM"),
         "db_unavailable": out.get("db_unavailable"),
         "total_count": (out.get("pagination") or {}).get("total_count",
                                                          out.get("total_count")),
         "rows": len(out.get("rows") or out.get("waste") or out.get("terms") or []),
         "csv_lines": out.get("csv_lines"), "not_found": out.get("_not_found"),
         "campaigns": len(out.get("campaigns") or []),
         "pattern_terms": (out.get("pattern") or {}).get("terms")
         if isinstance(out.get("pattern"), dict) else None}
print(json.dumps(facts))
"""


def _measure(url: str, route: str, window: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _MEASURE_ROUTE, route, window, SCALE_TERM],
        cwd=_ROOT, env=_child_env(url), capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_published(route: str, m: dict) -> None:
    """A bound is worth nothing if it was met by not building the page."""
    assert not m["db_unavailable"], m
    if route in ("terms", "export"):
        units = m["total_count"] if route == "terms" else m["csv_lines"] - 1
        assert units > 100_000, m                    # ≈ 103,000 term × campaign units
    elif route == "terms_filtered_page":
        assert m["total_count"] > 10_000 and m["rows"] == 50, m
    elif route in ("flagged", "waste"):
        assert m["total_count"] > 10_000 and m["rows"] >= 50, m
    elif route == "patterns":
        assert m["total_count"] > 1_000 and m["rows"] == 100, m
    elif route == "pattern_detail":
        assert m["pattern_terms"] > 100 and m["rows"] == 100, m
    elif route == "term":
        assert not m["not_found"] and m["campaigns"] > 20, m


@_needs_pg
@pytest.mark.parametrize("window", ["180d", "all_time"])
@pytest.mark.parametrize("route", sorted(GROWTH_LIMIT_MB))
def test_30_a_search_terms_request_stays_bounded_at_production_scale(
        scale_db, route, window):  # noqa: F811
    """Before PR-ADS-173A every one of these added ≈ 675 MB to the web process
    on this dataset — more than the instance has."""
    m = _measure(scale_db.url, route, window)
    _assert_published(route, m)
    growth = m["peak_mb"] - m["before_mb"]
    assert growth <= GROWTH_LIMIT_MB[route], (
        f"{route} at {window} added {growth:.0f} MB of resident memory "
        f"(limit {GROWTH_LIMIT_MB[route]} MB): {m}")


# ═════════════════════════════════════════════════════════════════════════════
# 4 · FAILURE PATHS — unavailable stays unavailable, out of memory stays raised
# ═════════════════════════════════════════════════════════════════════════════

def _exec(sql, params=()):
    from db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


@pytest.fixture()
def seeded(pg, monkeypatch):  # noqa: F811
    """The adversarial dataset of the equality matrix, in this process."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    matrix.seed(_exec)
    yield pg


def _export(window="30d"):
    import api.server as srv
    return srv.api_search_term_evidence_export(
        user={}, window=window, q=None, campaign=None, state=None,
        junk_category=None, min_spend=None, sort="spend", sql_state=None)


def _every_route_reports_unavailable():
    import api.server as srv
    from fastapi import HTTPException

    terms = st_svc.build_search_term_evidence("30d", now=NOW)
    assert terms["db_unavailable"] is True and terms["rows"] == []
    assert terms["pagination"]["total_count"] is None
    assert terms["kpis"]["reported_terms"] is None           # unknown, never 0
    flagged = st_svc.build_flagged_search_terms("30d", now=NOW)
    assert flagged["db_unavailable"] is True and flagged["rows"] == []
    assert flagged["pagination"]["total_count"] is None
    assert flagged["kpis"]["flagged_terms"] is None
    patterns = st_svc.build_search_pattern_evidence("30d", now=NOW)
    assert patterns["db_unavailable"] is True and patterns["rows"] == []
    assert patterns["kpis"]["patterns_found"] is None
    detail = st_svc.build_search_pattern_drawer("30d", "jobs", 1, now=NOW)
    assert detail["db_unavailable"] is True and detail["pattern"] is None
    drawer = st_svc.build_search_term_drawer("30d", "freight jobs", now=NOW)
    assert drawer["db_unavailable"] is True and drawer["term"] is None
    export = st_svc.build_search_term_export("30d", now=NOW)
    assert export["db_unavailable"] is True and export["complete"] is False
    assert list(export["rows"]) == []
    # An empty file is never presented as a complete export: 503.
    with pytest.raises(HTTPException) as refused:
        _export()
    assert refused.value.status_code == 503
    waste = srv.api_waste(user={}, days=30, window="30d")
    assert waste["waste"] == [] and "kpis" not in waste
    section = st_svc.build_campaign_flagged_preview("30d", matrix.CAMP_A, now=NOW)
    assert (section["available"], section["reason"]) == (
        False, st_svc.FLAGGED_PREVIEW_UNAVAILABLE_SOURCE)


@_needs_pg
def test_40_a_failed_cost_read_is_unavailable_on_every_route(seeded, monkeypatch):
    """The streamed read must keep what PR-ADS-172A established: a per-date
    cost read that FAILED withholds the population — it is not published as
    `fx_incomplete`, and not as a population with no cost."""
    assert st_svc.build_search_term_evidence("30d", now=NOW)["pagination"][
        "total_count"] > 50
    real = st_repo.get_conn
    state = {"statements": 0}

    class _FailsOnTheCostStatement:
        """The real connection, except that the per-date cost statement fails —
        on whichever cursor, streamed or not, runs it."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def cursor(self, *a, **k):
            cur = self._conn.cursor(*a, **k)
            execute = cur.execute

            class _Cursor:
                def __getattr__(self, name):
                    return getattr(cur, name)

                def __enter__(self):
                    cur.__enter__()
                    return self

                def __exit__(self, *exc):
                    return cur.__exit__(*exc)

                def __iter__(self):
                    return iter(cur)

                def execute(self, sql, params=None):
                    if "GROUP BY search_term, campaign_name, campaign_id, source_date" in sql:
                        state["statements"] += 1
                        raise RuntimeError("cost statement failed")
                    return execute(sql, params)

            return _Cursor()

    import contextlib

    @contextlib.contextmanager
    def _get_conn():
        with real() as conn:
            yield _FailsOnTheCostStatement(conn)

    monkeypatch.setattr(st_repo, "get_conn", _get_conn)
    assert st_repo.fetch_search_term_daily_costs(None, matrix.D0)["available"] is False
    _every_route_reports_unavailable()
    assert state["statements"] > 8, "the cost statement must have been reached"
    pop = st_svc._build_population(None, matrix.D0)
    assert pop["available"] is False
    assert pop["unavailable_reason"] == "search_term_daily_costs_unavailable"


@_needs_pg
def test_41_a_failed_fx_read_is_unavailable_and_a_missing_rate_is_fx_incomplete(
        seeded, monkeypatch):
    # A rate genuinely missing for one day: published, withheld per unit.
    healthy = st_svc.build_search_term_evidence("30d", now=NOW, q="customs")
    assert {r["search_term"]: r["currency_status"] for r in healthy["rows"]} == {
        "customs broker": "fx_incomplete", "customs software": "fx_incomplete",
        "ksa customs clearance": "verified"}
    assert all(r["spend_usd"] is None for r in healthy["rows"]
               if r["currency_status"] == "fx_incomplete")
    # The rate READ failing is a different statement, about us.
    monkeypatch.setattr(revenue_repo, "fetch_fx_rates",
                        lambda *a, **k: {"available": False, "rates": {}})
    _every_route_reports_unavailable()
    pop = st_svc._build_population(None, matrix.D0)
    assert pop["unavailable_reason"] == "fx_rates_unavailable"


def _oom(*a, **k):
    raise MemoryError()


@pytest.mark.parametrize("reader", [
    (st_repo, "fetch_search_term_aggregates"),
    (st_repo, "fetch_search_term_daily_costs"),
    (st_repo, "fetch_waste_evidence_for_terms"),
    (revenue_repo, "fetch_fx_rates"),
    (revenue_repo, "fetch_canonical_campaign_spend"),
])
def test_42_out_of_memory_is_raised_by_every_route_never_answered(monkeypatch, reader):
    """Out of memory is not "the database is unavailable". Every builder lets
    it through, and so do the route handlers in front of them — which used to
    answer it with the db-unavailable shape, a reason that is false."""
    import api.server as srv

    _doubles(monkeypatch, _POPULATION_GROUPS, streamed=True)
    module, name = reader
    monkeypatch.setattr(module, name, _oom)
    for build in (
            lambda: st_svc.build_search_term_evidence("30d", now=NOW),
            lambda: st_svc.build_flagged_search_terms("30d", now=NOW),
            lambda: st_svc.build_search_pattern_evidence("30d", now=NOW),
            lambda: st_svc.build_search_pattern_drawer("30d", "jobs", 1, now=NOW),
            lambda: st_svc.build_search_term_drawer("30d", "freight jobs", now=NOW),
            lambda: st_svc.build_search_term_export("30d", now=NOW),
            lambda: srv._search_term_evidence_call(
                "30d", st_svc.build_search_term_evidence, "30d"),
            lambda: srv._search_term_evidence_call(
                "30d", st_svc.build_flagged_search_terms, "30d",
                fallback=lambda w: {"db_unavailable": True}),
            _export,
            lambda: srv.api_waste(user={}, days=30, window="30d")):
        with pytest.raises(MemoryError):
            build()


def test_43_an_ordinary_failure_is_still_an_unavailable_page_not_a_500(monkeypatch):
    """Letting MemoryError through must not have widened: any other failure is
    answered with the endpoint's unavailable shape, as before."""
    import api.server as srv

    def _broken(*a, **k):
        raise RuntimeError("builder failed")

    out = srv._search_term_evidence_call("30d", _broken, "30d")
    assert out["db_unavailable"] is True and out["rows"] == []
    monkeypatch.setattr(st_svc, "build_flagged_search_terms", _broken)
    waste = srv.api_waste(user={}, days=30, window="30d")
    assert waste["waste"] == [] and waste["total_count"] == 0


class _FailsAfter:
    """A pooled connection whose cursor yields ``rows`` and then fails."""

    def __init__(self, rows, exc):
        self.rows, self.exc = rows, exc

    def cursor(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return None

    def __iter__(self):
        yield from self.rows
        if self.exc is not None:
            raise self.exc

    def fetchall(self):
        return []

    description = ()


def _conn_yielding(rows, exc):
    import contextlib

    @contextlib.contextmanager
    def _get_conn():
        yield _FailsAfter(rows, exc)
    return _get_conn


_GROUP_RECORD = ("freight jobs", "uk - brand", "1", 2.5, 2_500_000, ["GBP"],
                 ["google_ads_api"], 3, 40, 0, 1, date(2026, 11, 1),
                 date(2026, 11, 2), False, True, None, None, ["ag 1"], ["kw"], ["BROAD"])


def test_44_a_stream_that_breaks_midway_is_unavailable_never_a_partial_population(
        monkeypatch):
    """The streamed read hands rows to the consumer BEFORE the result set is
    complete. If the cursor then fails, the reader must answer unavailable and
    the build must publish NOTHING — never the units that happened to arrive."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    monkeypatch.setattr(st_repo, "get_conn",
                        _conn_yielding([_GROUP_RECORD], RuntimeError("cursor lost")))
    seen = []
    result = st_repo.fetch_search_term_aggregates(None, matrix.D0, sink=seen.append)
    assert len(seen) == 1 and result == {"available": False, "rows": []}

    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *a, **k: {"available": True, "rows": []})
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})
    pop = st_svc._build_population(None, matrix.D0)
    assert pop["available"] is False and pop["units"] == []
    terms = st_svc.build_search_term_evidence("all_time", now=NOW)
    assert terms["db_unavailable"] is True and terms["rows"] == []
    assert terms["pagination"]["total_count"] is None

    # The same for the per-date costs: one day delivered, then the cursor fails.
    monkeypatch.setattr(st_repo, "get_conn", _conn_yielding(
        [("freight jobs", "uk - brand", "1", date(2026, 11, 1), 1_000_000)],
        RuntimeError("cursor lost")))
    days = []
    result = st_repo.fetch_search_term_daily_costs(
        None, matrix.D0, sink=lambda *row: days.append(row))
    assert len(days) == 1 and result == {"available": False, "rows": []}
    # …and out of memory mid-stream is still out of memory.
    monkeypatch.setattr(st_repo, "get_conn",
                        _conn_yielding([_GROUP_RECORD], MemoryError()))
    with pytest.raises(MemoryError):
        st_repo.fetch_search_term_aggregates(None, matrix.D0, sink=seen.append)


def test_45_a_consumers_own_failure_is_not_reported_as_the_source_being_unavailable(
        monkeypatch):
    """The readers answer a DATABASE failure with `available: False`. A defect
    in the code consuming the rows is not that, and must not be absorbed into
    "the source is unavailable"."""
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)

    def _bug(*a):
        raise ZeroDivisionError("a defect in the merge")

    monkeypatch.setattr(st_repo, "get_conn", _conn_yielding([_GROUP_RECORD], None))
    with pytest.raises(ZeroDivisionError):
        st_repo.fetch_search_term_aggregates(None, matrix.D0, sink=_bug)
    monkeypatch.setattr(st_repo, "get_conn", _conn_yielding(
        [("freight jobs", "uk - brand", "1", date(2026, 11, 1), 1_000_000)], None))
    with pytest.raises(ZeroDivisionError):
        st_repo.fetch_search_term_daily_costs(None, matrix.D0, sink=_bug)


def test_46_a_snapshot_that_cannot_be_opened_is_unavailable_not_a_raise(monkeypatch):
    """`population_snapshot` wraps readers whose contract is "never raises". A
    dead pool must leave them answering unavailable, as it always did."""
    import contextlib

    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)

    @contextlib.contextmanager
    def _dead():
        raise RuntimeError("connection to server failed")
        yield  # pragma: no cover

    monkeypatch.setattr(st_repo, "get_conn", _dead)
    with st_repo.population_snapshot():
        assert st_repo._ambient_conn.get() is None
    pop = st_svc._build_population(None, matrix.D0)
    assert pop["available"] is False
    assert st_svc.build_search_term_evidence("30d", now=NOW)["db_unavailable"] is True

    class _Broken:
        def cursor(self, *a, **k):
            raise RuntimeError("server closed the connection")

    @contextlib.contextmanager
    def _unusable():
        yield _Broken()

    monkeypatch.setattr(st_repo, "get_conn", _unusable)
    with st_repo.population_snapshot():
        assert st_repo._ambient_conn.get() is None
    assert st_svc.build_search_term_evidence("30d", now=NOW)["db_unavailable"] is True
    # Out of memory while opening it is still out of memory.
    monkeypatch.setattr(st_repo, "get_conn",
                        contextlib.contextmanager(lambda: _oom() or (yield)))
    with pytest.raises(MemoryError):
        with st_repo.population_snapshot():
            pass


def test_47_an_unconfigured_account_reads_nothing_at_all(monkeypatch):
    """The identity context is read lazily now. It must still not be read for a
    population the scope has already refused."""
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    monkeypatch.setattr(st_svc, "_canonical_st_reconciliation", lambda w, n: {})
    import analysis.search_term_scope as scope
    monkeypatch.setattr(scope, "configured_customer_id", lambda: None)
    monkeypatch.setattr(scope, "customer_id_candidates", lambda configured=None: [])

    def _must_not_run(*a, **k):
        raise AssertionError("read although the account scope was refused")

    for name in ("fetch_canonical_campaign_spend", "fetch_campaign_identity",
                 "fetch_fx_rates"):
        monkeypatch.setattr(revenue_repo, name, _must_not_run)
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_costs", _must_not_run)
    monkeypatch.setattr(st_repo, "fetch_waste_evidence_for_terms", _must_not_run)
    assert st_svc._build_population(None, matrix.D0)["available"] is False
    assert st_svc.build_search_term_evidence("30d", now=NOW)["db_unavailable"] is True


@_needs_pg
def test_48_the_whole_population_is_read_on_one_snapshot(seeded, monkeypatch):
    """A sync committing BETWEEN the grouped rows and the per-date costs used to
    produce units whose total cost and whose per-date amounts described two
    different tables — in a response that reconciled. Every statement of the
    build now sees the database as it was when the build began."""
    real_costs = st_repo.fetch_search_term_daily_costs
    wrote = []

    def _costs_after_a_concurrent_commit(start, end, **kw):
        if not wrote:
            other = seeded.connect()
            try:
                other.autocommit = True
                with other.cursor() as cur:
                    # More cost on an existing fact row's unit, a new day for
                    # it, and a brand new term — committed at once.
                    cur.execute(
                        "INSERT INTO search_terms (source_date, campaign_name,"
                        " campaign_id, ad_group, keyword, match_type, search_term,"
                        " customer_id, spend_usd, clicks, impressions, conversions,"
                        " cost_micros, currency_code, source_system) VALUES "
                        "(%s,%s,%s,'ag concurrent','kw','BROAD','usd term',%s,"
                        " 9.0,1,1,0,9000000,'USD','google_ads_api'),"
                        "(%s,%s,%s,'ag concurrent','kw','BROAD','arrived mid build',%s,"
                        " 9.0,1,1,0,9000000,'USD','google_ads_api')",
                        (matrix.IN_30D, matrix.NAMES[matrix.CAMP_B], matrix.CAMP_B,
                         ACCOUNT, matrix.IN_7D, matrix.NAMES[matrix.CAMP_B],
                         matrix.CAMP_B, ACCOUNT))
                    cur.execute("INSERT INTO waste_terms (run_id, run_date,"
                                " search_term, campaign_name, spend_usd, junk_category,"
                                " matched_pattern, crm_junk_confirmed) VALUES "
                                "((SELECT min(id) FROM runs), %s, 'usd term', %s, 1.0,"
                                " 'job_seeker', 'jobs', 1)",
                                (matrix.D0, matrix.NAMES[matrix.CAMP_B]))
            finally:
                other.close()
            wrote.append(True)
        return real_costs(start, end, **kw)

    before = st_svc._build_population(None, matrix.D0)
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_costs",
                        _costs_after_a_concurrent_commit)
    during = st_svc._build_population(None, matrix.D0)
    assert wrote == [True]

    def facts(pop):
        return {(u["search_term_normalized"], u["campaign_key"]):
                (u["cost_micros"], u["_daily_micros"], u["row_count"], u["spend_usd"],
                 u.get("_waste_flag"))
                for u in pop["units"]}

    # The build that the commit landed in the middle of saw none of it…
    assert facts(during) == facts(before)
    assert during["source"] == before["source"]
    assert during["annotation_join"] == before["annotation_join"]
    for u in during["units"]:
        if u["cost_micros"] is not None:
            assert sum(u["_daily_micros"].values()) == u["cost_micros"], u["search_term"]
    # …and the next build sees all of it.
    monkeypatch.setattr(st_repo, "fetch_search_term_daily_costs", real_costs)
    after = facts(st_svc._build_population(None, matrix.D0))
    assert ("arrived mid build", matrix.CAMP_B) in after
    assert after[("usd term", matrix.CAMP_B)][0] == 18_000_000
    assert after[("usd term", matrix.CAMP_B)][4] is True
    assert st_repo._ambient_conn.get() is None, "the snapshot ended with the build"


@_needs_pg
def test_49_the_term_drawer_loads_the_units_of_its_term_only(seeded, monkeypatch):
    """The drawer publishes one term. It reads every raw spelling of it, in
    every campaign — and nothing else — unless the CRM carries exact queries,
    when attribution has to see the whole population."""
    complete = st_svc._build_population(None, matrix.D0)
    narrowed = st_svc._build_population(None, matrix.D0,
                                        normalized_terms={"freight jobs"})
    assert {u["search_term_normalized"] for u in narrowed["units"]} == {"freight jobs"}
    assert len(narrowed["units"]) == 2 < len(complete["units"])
    by_key = {(u["search_term_normalized"], u["campaign_key"]): u
              for u in complete["units"]}
    for u in narrowed["units"]:
        twin = by_key[(u["search_term_normalized"], u["campaign_key"])]
        assert sorted(u.keys()) == sorted(twin.keys())
        for key in u.keys():
            assert u[key] == twin[key], key
    assert len(by_key[("freight jobs", matrix.CAMP_A)]["search_term_variants"]) == 4
    # Narrowed source totals are narrowed by the same predicate.
    assert sum(u["row_count"] for u in narrowed["units"]) == narrowed["source"]["row_count"]
    # A term the window lacks loads nothing, and is "not found", not unavailable.
    assert st_svc._build_population(
        None, matrix.D0, normalized_terms={"never searched here"})["units"] == []
    assert st_svc._build_population(None, matrix.D0, normalized_terms=set())["units"] == []

    real = st_svc._build_population
    built = []

    def _population(start, end, **kw):
        built.append(kw)
        return real(start, end, **kw)

    monkeypatch.setattr(st_svc, "_build_population", _population)
    plain = st_svc.build_search_term_drawer("all_time", "Freight JOBS", now=NOW)
    assert built == [{"normalized_terms": {"freight jobs"}}]
    assert st_svc.build_search_term_drawer(
        "all_time", "never searched here", now=NOW)["_not_found"] is True

    # A CRM that carries exact queries: the complete population is built.
    del built[:]
    real_attr = st_svc._search_term_sql_attribution
    monkeypatch.setattr(st_svc, "_search_term_sql_attribution",
                        lambda pop, s, e: {**real_attr(pop, s, e),
                                           "population_has_text": True})
    with_queries = st_svc.build_search_term_drawer("all_time", "Freight JOBS", now=NOW)
    assert built == [{"normalized_terms": {"freight jobs"}}, {}]
    for payload in (plain, with_queries):
        payload["sql_attribution"].pop("exact_query_evidence_available")
    assert with_queries == plain


@_needs_pg
def test_50_the_csv_is_streamed_and_is_the_file_it_always_was(seeded, monkeypatch):
    """The export is sent as it is serialized. The bytes are the golden file's
    (section 2); here: it IS a stream, in more than one chunk, and an
    unavailable source is refused before the response starts."""
    import api.server as srv
    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    real_base = st_svc._base
    monkeypatch.setattr(st_svc, "_base",
                        lambda window, now: real_base(window, now or NOW))
    monkeypatch.setattr(srv, "_EXPORT_ROWS_PER_CHUNK", 10)
    response = _export("all_time")
    assert isinstance(response, StreamingResponse)
    assert response.headers["content-disposition"] == (
        'attachment; filename="search_terms_all_time_complete.csv"')

    async def _chunks():
        return [c async for c in response.body_iterator]

    import asyncio
    chunks = asyncio.run(_chunks())
    assert len(chunks) > 5
    _, golden = _golden()
    assert "".join(chunks) == golden["payloads"]["export/all_time"]["csv"]

    monkeypatch.setattr(st_repo, "fetch_search_term_aggregates",
                        lambda *a, **k: {"available": False, "rows": []})
    with pytest.raises(HTTPException) as refused:
        _export("all_time")
    assert refused.value.status_code == 503


# ═════════════════════════════════════════════════════════════════════════════
# 5 · The repository, on a real server
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_60_the_streamed_reads_are_the_row_list_reads(seeded, monkeypatch):
    """Same statement, same conversions: the rows a sink receives are the rows
    the list form returns (in a stated order), and the source totals agree."""
    listed = st_repo.fetch_search_term_aggregates(None, matrix.D0)
    streamed_rows: list = []
    streamed = st_repo.fetch_search_term_aggregates(
        None, matrix.D0, sink=streamed_rows.append)
    assert streamed["available"] is True and streamed["rows"] == []
    assert streamed["source"] == listed["source"]

    def key(r):
        return (r["search_term"].encode(), r["campaign_name"] is None,
                (r["campaign_name"] or "").encode(), r["campaign_id"].encode())

    assert streamed_rows == sorted(listed["rows"], key=key)
    assert len(streamed_rows) > 100
    assert sum(r["row_count"] for r in streamed_rows) == streamed["source"]["row_count"]

    per_day = st_repo.fetch_search_term_daily_costs(None, matrix.D0)
    expected: dict = {}
    for r in per_day["rows"]:
        k = (r["search_term"], r["campaign_name"], r["campaign_id"], r["source_date"])
        expected[k] = r["cost_micros"]
    got: dict = {}
    result = st_repo.fetch_search_term_daily_costs(
        None, matrix.D0, sink=lambda *row: got.__setitem__(row[:4], row[4]))
    assert result == {"available": True, "rows": []}
    assert got == expected and None in got.values()        # a NULL cost stays None

    # The annotation read asked in batches returns the rows one statement does.
    terms = sorted({r["search_term"] for r in listed["rows"]})
    whole = st_repo.fetch_waste_evidence_for_terms(terms)
    assert len(whole["rows"]) >= 5
    monkeypatch.setattr(st_repo, "WASTE_EVIDENCE_TERMS_PER_STATEMENT", 7)
    batched = st_repo.fetch_waste_evidence_for_terms(terms)
    assert batched == whole
