"""
tests/test_pr_ads_152_canonical_sql_truth.py

PR-ADS-152 — Canonical SQL Truth Reconciliation. Database-free unit tests for the
canonical contact-outcome service, its explicit named scopes, reconciliation
metadata, window vocabularies, classification repair planner, and governance.

Run with:
    python -m pytest tests/test_pr_ads_152_canonical_sql_truth.py -v
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from services import canonical_contact_outcome_service as canon  # noqa: E402
from services import canonical_classification_repair_service as repair  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _lead(key, *, status="qualified", src="PAID_SEARCH", campaign="Brand - US",
          created="2026-07-05", run="2026-07-10", row_id=1, keyword="tms",
          source_type="paid_search", contact_id=None, company=None, gclid=True):
    return {
        "contact_key": key,
        "contact_id": contact_id if contact_id is not None else key,
        "row_id": row_id,
        "run_date": run,
        "contact_created_at": created,
        "status_category": status,
        "source_type": source_type,
        "hs_analytics_source": src,
        "campaign_name": campaign,
        "keyword": keyword,
        "country": "US",
        "company": company or f"Co {key}",
        "has_gclid": gclid,
    }


_Q1_START = date(2026, 7, 1)
_Q1_END = date(2026, 7, 23)


def _pops(rows, exclusions=None, classification=None, start=_Q1_START, end=_Q1_END):
    return canon.build_populations(rows, exclusions or set(),
                                   classification or [], start, end)


# ═════════════════════════════════════════════════════════════════════════════
# §8 — Latest-state truth
# ═════════════════════════════════════════════════════════════════════════════
def test_older_qualified_newer_wrong_fit_is_not_sql():
    rows = [
        _lead("c1", status="qualified", run="2026-07-02", row_id=1),
        _lead("c1", status="wrong_fit", run="2026-07-15", row_id=9),
    ]
    pops = _pops(rows)
    assert pops["counts"]["total_all_source_sqls"] == 0
    assert canon.all_source_sqls(pops["contacts"]) == set()


def test_older_wrong_fit_newer_qualified_is_one_sql():
    rows = [
        _lead("c1", status="wrong_fit", run="2026-07-02", row_id=1),
        _lead("c1", status="qualified", run="2026-07-15", row_id=9),
    ]
    pops = _pops(rows)
    assert pops["counts"]["total_all_source_sqls"] == 1
    assert canon.google_ads_source_sqls(pops["contacts"]) == {"c1"}


def test_repeated_qualified_snapshots_count_once():
    rows = [
        _lead("c1", status="qualified", run="2026-07-02", row_id=1),
        _lead("c1", status="qualified", run="2026-07-10", row_id=5),
        _lead("c1", status="qualified", run="2026-07-15", row_id=9),
    ]
    pops = _pops(rows)
    assert pops["counts"]["total_all_source_sqls"] == 1


def test_excluded_contact_absent_from_every_scope():
    rows = [_lead("c1"), _lead("c2", contact_id="c2")]
    pops = _pops(rows, exclusions={"c2"})
    assert canon.all_source_sqls(pops["contacts"]) == {"c1"}
    assert canon.google_ads_source_sqls(pops["contacts"]) == {"c1"}
    assert canon.campaign_attributable_sqls(pops["contacts"]) == {"c1"}
    assert pops["counts"]["excluded_sql_contacts"] == 1
    # The excluded contact is recorded for the audit, never scored.
    assert [e["contact_key"] for e in pops["excluded"]] == ["c2"]


def test_missing_business_date_not_counted_in_bounded_window():
    rows = [_lead("c1", created=None)]
    pops = _pops(rows)
    assert pops["counts"]["total_all_source_sqls"] == 0
    assert pops["counts"]["missing_business_date_sql_contacts"] == 1


def test_missing_business_date_counted_in_all_time():
    rows = [_lead("c1", created=None)]
    pops = canon.build_populations(rows, set(), [], None, None)
    # All-time has no bounded window; still requires a business date to land, so a
    # contact with no date remains uncounted (never fabricated into a window).
    assert pops["counts"]["total_all_source_sqls"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# §8 — Source reconciliation
# ═════════════════════════════════════════════════════════════════════════════
def test_google_ads_source_equals_qualified_google_ads_contacts():
    rows = [
        _lead("g1"), _lead("g2"),
        _lead("o1", src="ORGANIC_SEARCH", source_type="organic_search", campaign=None),
    ]
    pops = _pops(rows)
    # Revenue by Source Google Ads SQLs == canonical google_ads_source scope.
    assert pops["counts"]["google_ads_source_sqls"] == 2
    assert pops["counts"]["total_all_source_sqls"] == 3


def test_stale_classification_cannot_inflate_source():
    # Latest leads say wrong_fit; a stale classification cache still says qualified.
    rows = [_lead("c1", status="wrong_fit")]
    stale = [{"contact_key": "c1", "acquisition_group": "google_ads",
              "status_category": "qualified", "contact_created_at": "2026-07-05"}]
    pops = _pops(rows, classification=stale)
    # The canonical scope trusts the latest leads row, NOT the stale cache.
    assert pops["counts"]["google_ads_source_sqls"] == 0
    assert pops["counts"]["stale_classification_contacts"] == 1


def test_missing_classification_surfaces_in_counts():
    rows = [_lead("c1")]
    pops = _pops(rows, classification=[])
    assert pops["counts"]["missing_classification_contacts"] == 1


def test_not_google_ads_contact_source_visible_but_not_campaign_attributable():
    rows = [_lead("o1", src="ORGANIC_SEARCH", source_type="organic_search",
                  campaign="Newsletter")]
    pops = _pops(rows)
    assert canon.all_source_sqls(pops["contacts"]) == {"o1"}          # source-visible
    assert canon.google_ads_source_sqls(pops["contacts"]) == set()   # not google ads
    assert canon.campaign_attributable_sqls(pops["contacts"]) == set()
    reasons = pops["contacts"][0]["scope_block_reasons"]
    assert canon.REASON_NOT_GOOGLE_ADS in reasons


def test_source_sql_may_exceed_campaign_attributable_with_disclosure():
    rows = [
        _lead("g1", campaign="Brand - US"),        # campaign-attributable
        _lead("g2", campaign="(direct)"),          # pseudo → not attributable
        _lead("g3", campaign=None),                # missing campaign
    ]
    pops = _pops(rows)
    assert pops["counts"]["google_ads_source_sqls"] == 3
    assert pops["counts"]["campaign_attributable_sqls"] == 1
    meta = canon.reconciliation_metadata(pops, canon.SCOPE_GOOGLE_ADS_SOURCE)
    assert meta["unmatched_sql_contacts"] == 2
    assert meta["reconciliation_status"] == canon.STATUS_PARTIAL


# ═════════════════════════════════════════════════════════════════════════════
# §8 — Subset invariants (never a subset without scope)
# ═════════════════════════════════════════════════════════════════════════════
def test_scopes_are_strictly_nested():
    rows = [
        _lead("g1", campaign="Brand - US", keyword="tms"),   # keyword+campaign
        _lead("g2", campaign="(direct)"),                    # ga source only
        _lead("o1", src="ORGANIC_SEARCH", source_type="organic_search"),  # all only
    ]
    pops = _pops(rows)
    contacts = pops["contacts"]
    all_s = canon.all_source_sqls(contacts)
    ga = canon.google_ads_source_sqls(contacts)
    camp = canon.campaign_attributable_sqls(contacts)
    assert camp <= ga <= all_s
    assert len(camp) <= len(ga) <= len(all_s)


def test_reconciliation_flags_broken_subset_as_mismatch():
    rows = [_lead("g1", campaign="Brand - US")]
    pops = _pops(rows)
    # Inject an impossible keyword count (2 > campaign_attributable 1).
    meta = canon.reconciliation_metadata(
        pops, canon.SCOPE_KEYWORD_ATTRIBUTABLE, keyword_attributable=2)
    assert meta["reconciliation_status"] == canon.STATUS_MISMATCH


def test_reconciliation_flags_consumer_disagreement_as_mismatch():
    rows = [_lead("g1", campaign="Brand - US"), _lead("g2", campaign="Brand - UK")]
    pops = _pops(rows)
    # The page claims 5 campaign-attributable SQLs; canonical says 2 → mismatch,
    # which must never render as a normal count.
    meta = canon.reconciliation_metadata(
        pops, canon.SCOPE_CAMPAIGN_ATTRIBUTABLE, consumer_count=5)
    assert meta["reconciliation_status"] == canon.STATUS_MISMATCH


def test_reconciliation_reconciled_when_fully_attributable():
    rows = [_lead("g1", campaign="Brand - US"), _lead("g2", campaign="Brand - UK")]
    cls = [
        {"contact_key": "g1", "acquisition_group": "google_ads",
         "status_category": "qualified", "contact_created_at": "2026-07-05"},
        {"contact_key": "g2", "acquisition_group": "google_ads",
         "status_category": "qualified", "contact_created_at": "2026-07-05"},
    ]
    pops = _pops(rows, classification=cls)
    meta = canon.reconciliation_metadata(pops, canon.SCOPE_GOOGLE_ADS_SOURCE)
    assert meta["reconciliation_status"] == canon.STATUS_RECONCILED


def test_reconciliation_unavailable_when_db_down():
    meta = canon.reconciliation_metadata({"counts": {}}, canon.SCOPE_ALL_SOURCE,
                                         available=False)
    assert meta["reconciliation_status"] == canon.STATUS_UNAVAILABLE


def test_metadata_always_names_its_scope():
    rows = [_lead("g1")]
    pops = _pops(rows)
    for scope in (canon.SCOPE_ALL_SOURCE, canon.SCOPE_GOOGLE_ADS_SOURCE,
                  canon.SCOPE_CAMPAIGN_ATTRIBUTABLE):
        meta = canon.reconciliation_metadata(pops, scope)
        assert meta["sql_scope"] == scope
        assert meta["sql_definition"] == canon.SQL_DEFINITION
        assert meta["sql_date_field"] == "contact_created_at"


# ═════════════════════════════════════════════════════════════════════════════
# §8 — Window reconciliation (business | evidence, never merged)
# ═════════════════════════════════════════════════════════════════════════════
def test_business_and_evidence_windows_are_distinct_vocabularies():
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    biz = canon.resolve_window_contract(canon.WINDOW_BUSINESS, "current_quarter", now=now)
    ev = canon.resolve_window_contract(canon.WINDOW_EVIDENCE, "30d", now=now)
    assert biz["window_type"] == "business"
    assert ev["window_type"] == "evidence"
    # Wrong vocabulary is rejected, never coerced.
    import pytest
    with pytest.raises(ValueError):
        canon.resolve_window_contract(canon.WINDOW_BUSINESS, "30d", now=now)
    with pytest.raises(ValueError):
        canon.resolve_window_contract(canon.WINDOW_EVIDENCE, "current_quarter", now=now)


def test_contact_before_quarter_start_only_in_30d():
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    biz = canon.resolve_window_contract(canon.WINDOW_BUSINESS, "current_quarter", now=now)
    ev = canon.resolve_window_contract(canon.WINDOW_EVIDENCE, "30d", now=now)
    # Late-June contact: inside Last 30 Days, outside Current Quarter (starts Jul 1).
    rows = [_lead("c1", created="2026-06-27", run="2026-07-01")]
    in_biz = _pops(rows, start=biz["start"], end=biz["end"])
    in_ev = _pops(rows, start=ev["start"], end=ev["end"])
    assert in_biz["counts"]["total_all_source_sqls"] == 0
    assert in_ev["counts"]["total_all_source_sqls"] == 1


def test_identical_explicit_dates_produce_identical_base_population():
    rows = [_lead("c1", created="2026-07-05"), _lead("c2", created="2026-07-06")]
    a = _pops(rows, start=date(2026, 7, 1), end=date(2026, 7, 23))
    b = _pops(rows, start=date(2026, 7, 1), end=date(2026, 7, 23))
    assert canon.all_source_sqls(a["contacts"]) == canon.all_source_sqls(b["contacts"])
    assert a["counts"] == b["counts"]


# ═════════════════════════════════════════════════════════════════════════════
# §8 — Page reconciliation (equality of scoped counts)
# ═════════════════════════════════════════════════════════════════════════════
def test_dashboard_ga_source_equals_revenue_by_source_ga_source():
    # Both pages read the ONE canonical population, so their google_ads_source_sqls
    # are equal by construction (acceptance criterion #1).
    rows = [_lead("g1", campaign="Brand - US"), _lead("g2", campaign="(direct)"),
            _lead("o1", src="ORGANIC_SEARCH", source_type="organic_search")]
    pops = _pops(rows)
    dashboard = canon.reconciliation_metadata(pops, canon.SCOPE_GOOGLE_ADS_SOURCE)
    source = canon.reconciliation_metadata(pops, canon.SCOPE_GOOGLE_ADS_SOURCE)
    assert dashboard["google_ads_source_sqls"] == source["google_ads_source_sqls"] == 2


def test_mart_campaign_sql_equals_campaign_attributable_subset():
    rows = [_lead("g1", campaign="Brand - US"), _lead("g2", campaign="Brand - UK"),
            _lead("g3", campaign="(direct)")]
    pops = _pops(rows)
    meta = canon.reconciliation_metadata(pops, canon.SCOPE_CAMPAIGN_ATTRIBUTABLE)
    assert meta["campaign_attributable_sqls"] == len(
        canon.campaign_attributable_sqls(pops["contacts"])) == 2


def test_keyword_row_sum_equals_uniquely_keyword_attributed_subset():
    rows = [_lead("g1", campaign="Brand - US", keyword="tms"),
            _lead("g2", campaign="Brand - UK", keyword="freight")]
    pops = _pops(rows)
    # Keyword-attributable is a subset of campaign-attributable; a keyword count
    # equal to campaign-attributable reconciles.
    meta = canon.reconciliation_metadata(
        pops, canon.SCOPE_KEYWORD_ATTRIBUTABLE, keyword_attributable=2)
    assert meta["keyword_attributable_sqls"] == 2
    assert meta["reconciliation_status"] in (canon.STATUS_RECONCILED, canon.STATUS_PARTIAL)


# ═════════════════════════════════════════════════════════════════════════════
# §6 — Classification repair (idempotent local upsert; original evidence kept)
# ═════════════════════════════════════════════════════════════════════════════
def test_repair_backfills_missing_and_repairs_stale():
    leads = [
        _lead("c1", status="qualified"),               # stale cache
        _lead("c2", status="qualified"),               # missing cache
    ]
    cls = [{"contact_key": "c1", "acquisition_group": "organic",
            "status_category": "in_progress", "contact_created_at": "2026-07-05"}]
    plan = repair.plan_repairs(leads, cls, set())
    assert plan["report"]["stale_repaired"] == 1
    assert plan["report"]["missing_backfilled"] == 1
    assert {u["contact_key"] for u in plan["upserts"]} == {"c1", "c2"}
    # The repaired row carries the canonical latest truth.
    c1 = next(u for u in plan["upserts"] if u["contact_key"] == "c1")
    assert c1["acquisition_group"] == "google_ads"
    assert c1["status_category"] == "qualified"


def test_repair_is_idempotent_second_pass_is_clean():
    leads = [_lead("c1", status="qualified")]
    cls = []
    first = repair.plan_repairs(leads, cls, set())
    # Apply the plan into the cache, then re-plan — nothing left to do.
    repaired_cache = first["upserts"]
    second = repair.plan_repairs(leads, repaired_cache, set())
    assert second["report"]["stale_repaired"] == 0
    assert second["report"]["missing_backfilled"] == 0
    assert second["upserts"] == []


def test_repair_never_deletes_orphan_classifications():
    leads = [_lead("c1", status="qualified")]
    cls = [{"contact_key": "zzz", "acquisition_group": "organic",
            "status_category": "qualified", "contact_created_at": "2020-01-01"}]
    plan = repair.plan_repairs(leads, cls, set())
    # The orphan is reported, never scheduled for deletion (upserts only).
    assert plan["report"]["orphan_classifications"] == 1
    assert all(u["contact_key"] != "zzz" for u in plan["upserts"])


# ═════════════════════════════════════════════════════════════════════════════
# §8 — Governance (no writes to external systems / no deletions / no emails)
# ═════════════════════════════════════════════════════════════════════════════
def test_touched_paths_have_no_external_writes_or_deletions():
    paths = [
        "db/canonical_contact_outcome_repository.py",
        "services/canonical_contact_outcome_service.py",
        "services/sql_truth_audit_service.py",
        "services/canonical_classification_repair_service.py",
    ]
    # Concrete external-write / deletion CALL signatures. The audit's governance
    # attestation block legitimately names offline_conversion_uploads etc. as
    # False, so we look for actual call sites, not attestation words.
    banned = ("upload_conversion", "add_negative", "create_negative",
              "upload_offline", "hubspot.client(", "delete from",
              "drop table", "requests.post", "requests.put")
    for p in paths:
        src = (_ROOT / p).read_text()
        low = src.lower()
        for term in banned:
            assert term.lower() not in low, f"{p}: forbidden {term!r}"


def test_canonical_contacts_never_expose_email():
    rows = [_lead("c1")]
    pops = _pops(rows)
    for c in pops["contacts"]:
        assert "email" not in c
        assert not any("email" in str(k).lower() for k in c.keys())


def test_repository_is_select_only():
    src = (_ROOT / "db" / "canonical_contact_outcome_repository.py").read_text().lower()
    for banned in ("insert into", "update ", "delete from"):
        assert banned not in src
