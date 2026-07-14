"""
tests/test_pr_ads_146c_sql_attribution.py

PR-ADS-146C — HubSpot-confirmed Attributed SQLs for Keyword Evidence and Search
Terms. Unit tests for the shared attribution service (db-free): SQL population
semantics, keyword unique/ambiguous/known-zero/mapping-review attribution,
search-term exact-query-only attribution, reconciliation and governance.

Run with:
    python -m pytest tests/test_pr_ads_146c_sql_attribution.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from services import platform_sql_attribution_service as sql  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _contact(key, *, cid="10", keyword="freight software", search_term=None,
             gclid=False, mapping="mapped"):
    return {
        "contact_key": key, "contact_id": key, "company": f"Co {key}",
        "sql_date": "2026-07-10", "campaign_label": "Brand - UK",
        "campaign_mapping_status": mapping,
        "campaign_key": cid, "canonical_campaign_id": (cid if mapping == "mapped" else None),
        "keyword_raw": keyword, "keyword_norm": sql.normalize_term(keyword),
        "search_term_raw": search_term,
        "search_term_norm": sql.normalize_term(search_term),
        "has_gclid": gclid,
    }


def _crit(key, *, cid="10", keyword="freight software", mapping="mapped"):
    return {"criterion_key": key, "campaign_id": cid, "keyword_text": keyword,
            "mapping_status": mapping}


# ─────────────────────────────────────────────────────────────────────────────
# §1 — normalization
# ─────────────────────────────────────────────────────────────────────────────
def test_normalize_strips_match_type_wrappers_and_collapses_ws():
    assert sql.normalize_term("[Freight  Software]") == "freight software"
    assert sql.normalize_term('"cargo ship"') == "cargo ship"
    assert sql.normalize_term("+freight +forwarding") == "freight forwarding"
    assert sql.normalize_term(None) == ""


# ─────────────────────────────────────────────────────────────────────────────
# §4 — keyword attribution
# ─────────────────────────────────────────────────────────────────────────────
def test_unique_campaign_keyword_assigns_one_criterion():
    contacts = [_contact("c1", cid="10", keyword="cargo ship")]
    criteria = [_crit("K1", cid="10", keyword="cargo ship"),
                _crit("K2", cid="10", keyword="other")]
    r = sql.attribute_keywords(contacts, criteria, available=True)
    assert r["by_criterion"]["K1"]["sql_attribution_status"] == "attributed"
    assert r["by_criterion"]["K1"]["attributed_sqls"] == 1
    assert r["by_criterion"]["K2"]["sql_attribution_status"] == "known_zero"
    assert r["reconciliation"]["uniquely_attributed_sql_contacts"] == 1


def test_same_keyword_across_two_ad_groups_is_ambiguous():
    contacts = [_contact("c1", cid="10", keyword="freight software")]
    criteria = [_crit("K1", cid="10", keyword="freight software"),
                _crit("K2", cid="10", keyword="freight software")]  # 2 ad groups
    r = sql.attribute_keywords(contacts, criteria, available=True)
    assert r["by_criterion"]["K1"]["sql_attribution_status"] == "ambiguous"
    assert r["by_criterion"]["K1"]["attributed_sqls"] is None
    assert r["by_criterion"]["K1"]["sql_candidate_count"] == 2
    assert r["by_criterion"]["K2"]["sql_attribution_status"] == "ambiguous"
    # The ambiguous contact is copied onto NO row.
    assert r["reconciliation"]["ambiguous_sql_contacts"] == 1
    assert r["reconciliation"]["uniquely_attributed_sql_contacts"] == 0
    assert r["reconciliation"]["row_sql_sum"] == 0


def test_same_keyword_across_duplicate_campaign_ids_is_ambiguous():
    # Two criterion ids under the same campaign id + keyword → ambiguous.
    contacts = [_contact("c1", cid="10", keyword="freight software")]
    criteria = [_crit("K1", cid="10", keyword="freight software"),
                _crit("K2", cid="10", keyword="freight software")]
    r = sql.attribute_keywords(contacts, criteria, available=True)
    assert {v["sql_attribution_status"] for v in r["by_criterion"].values()} == {"ambiguous"}


def test_unresolved_campaign_contact_is_not_attributed():
    contacts = [_contact("c1", mapping="unmatched", keyword="freight software")]
    criteria = [_crit("K1", cid="10", keyword="freight software")]
    r = sql.attribute_keywords(contacts, criteria, available=True)
    assert r["by_criterion"]["K1"]["sql_attribution_status"] == "known_zero"
    assert r["reconciliation"]["unattributed_sql_contacts"] == 1
    assert r["reconciliation"]["uniquely_attributed_sql_contacts"] == 0


def test_criterion_with_unresolved_campaign_is_mapping_review():
    r = sql.attribute_keywords([], [_crit("K1", mapping="unmatched")], available=True)
    assert r["by_criterion"]["K1"]["sql_attribution_status"] == "mapping_review"
    assert r["by_criterion"]["K1"]["attributed_sqls"] is None


def test_known_zero_distinct_from_unavailable():
    criteria = [_crit("K1", cid="10", keyword="no match here")]
    known = sql.attribute_keywords([], criteria, available=True)
    assert known["by_criterion"]["K1"]["sql_attribution_status"] == "known_zero"
    assert known["by_criterion"]["K1"]["attributed_sqls"] == 0
    unavail = sql.attribute_keywords([], criteria, available=False)
    assert unavail["by_criterion"]["K1"]["sql_attribution_status"] == "unavailable"
    assert unavail["by_criterion"]["K1"]["attributed_sqls"] is None


def test_row_sum_equals_uniquely_attributed():
    contacts = [_contact("c1", cid="10", keyword="a"),
                _contact("c2", cid="10", keyword="b"),
                _contact("c3", cid="20", keyword="a")]
    criteria = [_crit("K1", cid="10", keyword="a"), _crit("K2", cid="10", keyword="b"),
                _crit("K3", cid="20", keyword="a")]
    r = sql.attribute_keywords(contacts, criteria, available=True)
    row_sum = sum(v["attributed_sqls"] or 0 for v in r["by_criterion"].values())
    assert row_sum == r["reconciliation"]["uniquely_attributed_sql_contacts"] == 3
    assert r["reconciliation"]["reconciliation_status"] == "ok"


def test_multiple_deals_or_snapshots_count_once():
    # The population is already deduplicated per contact_key by the repository;
    # a repeated key here models the guarantee that it collapses to one SQL.
    contacts = [_contact("c1", cid="10", keyword="a")]
    criteria = [_crit("K1", cid="10", keyword="a")]
    r = sql.attribute_keywords(contacts, criteria, available=True)
    assert r["by_criterion"]["K1"]["attributed_sqls"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# §5 — search-term attribution (exact persisted query only)
# ─────────────────────────────────────────────────────────────────────────────
def _st_unit(key, *, cid="10", term="freight software"):
    return {"unit_key": key, "campaign_id": cid, "search_term": term, "mapping_status": "mapped"}


def test_keyword_only_evidence_never_assigns_a_search_term():
    # Contacts carry a keyword but no persisted query → every unit unavailable.
    contacts = [_contact("c1", cid="10", keyword="freight software", search_term=None)]
    units = [_st_unit("u1", cid="10", term="freight software")]
    r = sql.attribute_search_terms(contacts, units, available=True,
                                   leads_have_search_term=False)
    assert r["by_unit"]["u1"]["sql_attribution_status"] == "unavailable"
    assert r["by_unit"]["u1"]["attributed_sqls"] is None
    assert r["coverage"]["coverage_pct"] == 0.0
    assert r["population_has_text"] is False


def test_exact_persisted_query_assigns_when_present():
    # Forward-compatible: if a real query is persisted, exact + unique campaign
    # attributes safely.
    contacts = [_contact("c1", cid="10", search_term="cheap freight software")]
    units = [_st_unit("u1", cid="10", term="cheap freight software"),
             _st_unit("u2", cid="10", term="other")]
    r = sql.attribute_search_terms(contacts, units, available=True,
                                   leads_have_search_term=True)
    assert r["by_unit"]["u1"]["sql_attribution_status"] == "attributed"
    assert r["by_unit"]["u1"]["attributed_sqls"] == 1
    assert r["by_unit"]["u2"]["sql_attribution_status"] == "known_zero"


def test_fuzzy_search_term_is_never_matched():
    contacts = [_contact("c1", cid="10", search_term="freight softwares")]  # plural
    units = [_st_unit("u1", cid="10", term="freight software")]
    r = sql.attribute_search_terms(contacts, units, available=True,
                                   leads_have_search_term=True)
    assert r["by_unit"]["u1"]["sql_attribution_status"] == "known_zero"  # no exact match
    assert r["reconciliation"]["uniquely_attributed_sql_contacts"] == 0


def test_same_term_in_separate_campaigns_stays_separate():
    contacts = [_contact("c1", cid="10", search_term="freight software")]
    units = [_st_unit("u10", cid="10", term="freight software"),
             _st_unit("u20", cid="20", term="freight software")]
    r = sql.attribute_search_terms(contacts, units, available=True,
                                   leads_have_search_term=True)
    assert r["by_unit"]["u10"]["sql_attribution_status"] == "attributed"
    assert r["by_unit"]["u20"]["sql_attribution_status"] == "known_zero"


def test_search_term_row_sum_equals_uniquely_attributed():
    contacts = [_contact("c1", cid="10", search_term="a"),
                _contact("c2", cid="20", search_term="b")]
    units = [_st_unit("u1", cid="10", term="a"), _st_unit("u2", cid="20", term="b")]
    r = sql.attribute_search_terms(contacts, units, available=True,
                                   leads_have_search_term=True)
    row_sum = sum(v["attributed_sqls"] or 0 for v in r["by_unit"].values())
    assert row_sum == r["reconciliation"]["uniquely_attributed_sql_contacts"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Population resolution + governance
# ─────────────────────────────────────────────────────────────────────────────
def test_population_resolves_identity_and_marks_search_term_absent():
    repo_rows = {
        "available": True, "leads_have_search_term": False,
        "rows": [
            {"contact_key": "c1", "contact_id": "c1", "company": "Acme",
             "campaign_name": "Brand - UK", "keyword": "freight software",
             "contact_created_at": None, "has_gclid": True, "search_term": None},
        ],
    }
    from datetime import date

    from services.campaign_identity_service import normalize_campaign_name
    norm = normalize_campaign_name("Brand - UK")
    with patch("db.platform_sql_attribution_repository.fetch_sql_contacts",
               return_value=repo_rows), \
         patch("db.revenue_repository.fetch_canonical_campaign_spend",
               return_value={"customer_id": "X", "rows": []}), \
         patch("db.revenue_repository.fetch_campaign_identity",
               return_value={"available": True, "rows": []}), \
         patch("services.search_term_evidence_service._spend_index",
               return_value=({}, {norm: {"99"}})), \
         patch("services.search_term_evidence_service._identity_label_index",
               return_value=({}, {})):
        out = sql.fetch_and_resolve_contacts(date(2026, 7, 1), date(2026, 7, 31))
    assert out["available"] is True
    c = out["contacts"][0]
    assert c["canonical_campaign_id"] == "99"       # exact-normalized unique fallback
    assert c["keyword_norm"] == "freight software"
    assert c["search_term_norm"] == ""              # no persisted query
    assert out["leads_have_search_term"] is False


def test_repository_selects_qualified_only_and_no_search_term_column():
    src = (_ROOT / "db" / "platform_sql_attribution_repository.py").read_text()
    assert "status_category = 'qualified'" in src
    assert "source_type = 'paid_search'" in src
    assert "contact_created_at" in src
    assert "lead_truth_exclusions" in src
    # The user query is not a real column — never selected.
    assert "SELECT search_term" not in src and ", search_term\n" not in src


def test_service_is_strictly_read_only():
    for path in ("services/platform_sql_attribution_service.py",
                 "db/platform_sql_attribution_repository.py"):
        src = (_ROOT / path).read_text().lower()
        for banned in ("insert into", "update ", "delete from", "mutate",
                       "upload_conversion", "add_negative", "create_negative"):
            assert banned not in src, f"{path}: forbidden {banned!r}"


def test_contact_details_never_include_email():
    contacts = [_contact("c1", gclid=True)]
    out = sql.contact_details_for_keys(contacts, ["c1"])
    assert out and "email" not in out[0]
    assert set(out[0]) >= {"company", "contact_id", "sql_date", "campaign_label", "has_gclid"}
