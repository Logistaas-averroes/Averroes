"""
tests/test_pr_ads_152b_completion.py

PR-ADS-152 final completion — database-free unit + frontend-contract tests for:

  §2  campaign-attributable uses the REAL Google Ads campaign-identity contract
      (approved mapping, not_google_ads, unresolved, duplicate ambiguity, exact
      unique match) — never a bare non-empty campaign string;
  §4  classification repair preserves the HubSpot source taxonomy (never writes
      campaign_name into source_detail_raw; Meta / LinkedIn / Email / Events /
      referral classifications are not damaged);
  §5  the canonical Evidence Window resolves its boundary in the Google Ads
      account timezone (British Summer Time boundary), not UTC now.date();
  §3  the classification repair default resolves an explicit all-time range
      (start=None, end=today in the account timezone);
  §1  the Dashboard and Revenue by Source pages DISPLAY the canonical scoped SQL
      counts (with mismatch withheld), and app.js renders the new label/value.

Run with:
    python -m pytest tests/test_pr_ads_152b_completion.py -v
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tests.canonical_ledger_fixtures import patch_canonical_ledger  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from services import canonical_contact_outcome_service as canon  # noqa: E402
from services import canonical_classification_repair_service as repair  # noqa: E402

_APP = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
_DASH = (_ROOT / "services" / "dashboard_overview_service.py").read_text(encoding="utf-8")


def _lead(key, *, status="qualified", src="PAID_SEARCH", campaign="Brand - US",
          created="2026-07-05", run="2026-07-10", row_id=1, keyword="tms",
          source_type="paid_search"):
    return {
        "contact_key": key, "contact_id": key, "row_id": row_id, "run_date": run,
        "contact_created_at": created, "status_category": status,
        "source_type": source_type, "hs_analytics_source": src,
        "campaign_name": campaign, "keyword": keyword, "country": "US",
        "company": f"Co {key}", "has_gclid": True,
    }


# ═════════════════════════════════════════════════════════════════════════════
# §2 — real campaign identity
# ═════════════════════════════════════════════════════════════════════════════
def _resolver(*, spend=None, mappings=None):
    from services.search_term_evidence_service import _identity_label_index, _spend_index
    spend_by_id, norm_to_ids = _spend_index({"rows": spend or []})
    identity_by_label, _al = _identity_label_index({"mappings": mappings or []})
    return canon.make_identity_campaign_resolver(spend_by_id, norm_to_ids, identity_by_label)


def test_exact_unique_campaign_match_is_attributable():
    r = _resolver(spend=[{"campaign_id": "10", "campaign_name": "Brand UK"}])
    assert r("Brand UK") == (True, None)


def test_approved_manual_mapping_is_attributable():
    r = _resolver(
        spend=[{"campaign_id": "10", "campaign_name": "Canonical Brand"}],
        mappings=[{"external_campaign_label": "HS Brand Label", "campaign_id": "10",
                   "match_method": "manual"}])
    assert r("HS Brand Label") == (True, None)


def test_not_google_ads_exclusion_is_not_attributable():
    r = _resolver(mappings=[{"external_campaign_label": "Offline Import",
                             "match_method": "not_google_ads"}])
    ok, reason = r("Offline Import")
    assert ok is False
    assert reason == canon.REASON_CAMPAIGN_NOT_GOOGLE_ADS


def test_unresolved_external_label_stays_unattributed():
    r = _resolver(spend=[{"campaign_id": "10", "campaign_name": "Brand UK"}])
    ok, reason = r("Totally Unknown Campaign")
    assert ok is False
    assert reason == canon.REASON_UNRESOLVED_CAMPAIGN_MAPPING


def test_duplicate_normalized_campaign_names_are_ambiguous():
    r = _resolver(spend=[{"campaign_id": "1", "campaign_name": "Brand"},
                         {"campaign_id": "2", "campaign_name": "brand"}])
    ok, reason = r("Brand")
    assert ok is False
    assert reason == canon.REASON_AMBIGUOUS_CAMPAIGN_IDENTITY


def test_campaign_attributable_requires_resolved_identity_not_just_a_string():
    # A non-empty, non-pseudo campaign string with NO canonical identity is NOT
    # campaign-attributable under the real contract.
    r = _resolver()  # empty identity index
    pops = canon.build_populations([_lead("c1", campaign="Some Real-Looking Name")],
                                   set(), [], date(2026, 7, 1), date(2026, 7, 23),
                                   campaign_resolver=r)
    assert pops["counts"]["google_ads_source_sqls"] == 1
    assert pops["counts"]["campaign_attributable_sqls"] == 0  # unresolved → not attributable


# ═════════════════════════════════════════════════════════════════════════════
# §4 — source taxonomy is preserved by repair
# ═════════════════════════════════════════════════════════════════════════════
def _off(key, detail, **kw):
    d = _lead(key, src="OFFLINE_SOURCES", campaign="Some Campaign", **kw)
    return d


@pytest.mark.parametrize("primary,stored_group,detail", [
    ("PAID_SOCIAL", "other_paid", "Meta"),          # Meta
    ("PAID_SOCIAL", "other_paid", "LinkedIn"),        # LinkedIn
    ("EMAIL_MARKETING", "other_paid", "Newsletter"),  # Email Marketing
    ("OFFLINE_SOURCES", "other_paid", "SalesNash / Events"),  # Events (drill-down)
    ("REFERRALS", "organic", "Partner"),              # referral
])
def test_repair_does_not_damage_source_classifications(primary, stored_group, detail):
    lead = _lead("x1", src=primary, status="qualified")
    # Stored row is correct on group but STALE on status (in_progress → qualified).
    stored = [{"contact_key": "x1", "acquisition_group": stored_group,
               "status_category": "in_progress", "contact_created_at": "2026-07-05",
               "source_primary_raw": primary, "source_detail_raw": detail}]
    plan = repair.plan_repairs([lead], stored, set())
    up = plan["upserts"][0]
    # Group is NEVER damaged; the drill-down detail is preserved verbatim; only the
    # stale status is repaired.
    assert up["acquisition_group"] == stored_group
    assert up["source_detail_raw"] == detail
    assert up["source_primary_raw"] == primary
    assert up["status_category"] == "qualified"


def test_repair_never_writes_campaign_name_into_source_detail():
    lead = _lead("m1", src="PAID_SOCIAL", campaign="Meta Boost Campaign", status="qualified")
    plan = repair.plan_repairs([lead], [], set())  # missing → backfill
    up = plan["upserts"][0]
    assert up["source_detail_raw"] is None          # never the campaign name
    assert up["source_detail_raw"] != "Meta Boost Campaign"
    assert up["source_primary_raw"] == "PAID_SOCIAL"  # genuine durable HubSpot source


def test_derive_group_ignores_campaign_name_as_drilldown():
    # Two offline contacts with the SAME primary but different campaign names must
    # derive the same group (campaign_name is never the drill-down).
    a = canon.derive_acquisition_group(_lead("a", src="OFFLINE_SOURCES", campaign="Events X"))
    b = canon.derive_acquisition_group(_lead("b", src="OFFLINE_SOURCES", campaign="Reseller Y"))
    assert a == b


def test_repair_source_file_never_assigns_campaign_to_detail():
    src = (_ROOT / "services" / "canonical_classification_repair_service.py").read_text()
    assert '"source_detail_raw": row.get("campaign_name")' not in src
    assert 'source_detail_raw": campaign' not in src.replace(" ", "")


# ═════════════════════════════════════════════════════════════════════════════
# §5 — account-timezone evidence window (British Summer Time)
# ═════════════════════════════════════════════════════════════════════════════
def test_evidence_window_uses_account_timezone_across_bst_boundary():
    # 23:30 UTC on 30 Jun is 00:30 on 1 Jul in Europe/London (BST = UTC+1).
    bst = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)
    w = canon.resolve_window_contract(canon.WINDOW_EVIDENCE, "7d", now=bst)
    assert w["end_date"] == "2026-07-01"          # account-local day
    assert w["end_date"] != bst.date().isoformat()  # NOT the UTC date


def test_evidence_window_matches_campaign_evidence_resolver():
    # The canonical evidence resolver and Campaign Evidence resolve the SAME
    # boundary date (same account-tz "today").
    from services.campaign_evidence_service import _account_today
    now = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)
    w = canon.resolve_window_contract(canon.WINDOW_EVIDENCE, "30d", now=now)
    assert w["end_date"] == _account_today(now).isoformat()


# ═════════════════════════════════════════════════════════════════════════════
# §3 — classification repair bounds
# ═════════════════════════════════════════════════════════════════════════════
def test_repair_resolves_explicit_all_time_range(monkeypatch):
    seen = {}

    def _cap(start, end):
        seen["start"], seen["end"] = start, end
        return {"available": True, "lead_rows": [], "exclusions": set(), "classification": []}

    monkeypatch.setattr(
        "db.canonical_contact_outcome_repository.fetch_canonical_inputs", _cap)
    now = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)  # BST
    repair.run_repair(now=now)
    assert seen["start"] is None                      # explicit no lower bound (all-time)
    assert seen["end"] == date(2026, 7, 1)            # today in the account timezone


# ═════════════════════════════════════════════════════════════════════════════
# §1 — Revenue by Source DISPLAYS canonical google_ads_source SQLs
# ═════════════════════════════════════════════════════════════════════════════
def _patch_source(monkeypatch, *, lead_rows, recon):
    monkeypatch.setattr("db.revenue_repository.fetch_source_leads",
                        lambda s, e, *a, **k: {"available": True, "rows": lead_rows})
    # PR-ADS-153E-B: won revenue comes from the canonical deal ledger. This suite
    # exercises the SQL reconciliation, so the revenue population is empty but
    # READABLE — an unavailable ledger would blank the page for a different reason.
    patch_canonical_ledger(monkeypatch, [])
    monkeypatch.setattr("db.writers.source_attribution_health_counts",
                        lambda: {"contacts_classified": 0, "deals_attributed": 0,
                                 "ambiguous_deals": 0, "unclassified_deals": 0})
    monkeypatch.setattr(
        "services.revenue_spend_truth_service.build_google_ads_spend_truth",
        lambda window, now=None: {"state": "source_unavailable", "usd_spend": None,
                                  "native_spend": None, "native_currency": "GBP"})
    monkeypatch.setattr(
        "services.canonical_contact_outcome_service.page_reconciliation",
        lambda *a, **k: recon)


def _recon(status, ga, camp):
    return {"sql_scope": "google_ads_source_sqls", "reconciliation_status": status,
            "google_ads_source_sqls": ga, "campaign_attributable_sqls": camp,
            "total_all_source_sqls": ga, "excluded_sql_contacts": 0,
            "unmatched_sql_contacts": (None if ga is None else ga - (camp or 0))}


def test_revenue_by_source_displays_canonical_and_moves_legacy(monkeypatch):
    # Classification says 7 qualified Google Ads contacts; canonical reconciles to 2.
    lead_rows = [{"acquisition_group": "google_ads", "status_category": "qualified",
                  "source_primary_raw": "PAID_SEARCH", "source_detail_raw": None}
                 for _ in range(7)]
    _patch_source(monkeypatch, lead_rows=lead_rows,
                  recon=_recon(canon.STATUS_PARTIAL, 2, 1))
    from services.source_attribution_service import build_revenue_by_source
    out = build_revenue_by_source("current_quarter", now=datetime(2026, 6, 22, tzinfo=timezone.utc))
    g = next(x for x in out["groups"] if x["group"] == "google_ads")
    assert g["legacy_classification_sqls"] == 7      # audit-only legacy count
    assert g["sqls"] == 2                            # DISPLAYED canonical google_ads_source
    assert g["google_ads_source_sqls"] == 2
    assert g["campaign_attributable_sqls"] == 1
    assert g["sqls_scope"] == "google_ads_source_sqls"
    # group, its Paid Search channel and Google Ads platform reconcile.
    ch = next(c for c in g["channels"] if c["channel"] == "paid_search")
    assert ch["sqls"] == 2
    pf = next(p for p in ch["platforms"] if p["platform"] == "google_ads")
    assert pf["sqls"] == 2


def test_revenue_by_source_withholds_on_mismatch(monkeypatch):
    lead_rows = [{"acquisition_group": "google_ads", "status_category": "qualified",
                  "source_primary_raw": "PAID_SEARCH", "source_detail_raw": None}]
    _patch_source(monkeypatch, lead_rows=lead_rows,
                  recon=_recon(canon.STATUS_MISMATCH, 5, 1))
    from services.source_attribution_service import build_revenue_by_source
    out = build_revenue_by_source("current_quarter", now=datetime(2026, 6, 22, tzinfo=timezone.utc))
    g = next(x for x in out["groups"] if x["group"] == "google_ads")
    assert g["sqls"] is None                         # withheld, never a contradictory number
    assert g["sql_reconciliation_status"] == canon.STATUS_MISMATCH


def test_revenue_by_source_exposes_top_level_reconciliation(monkeypatch):
    _patch_source(monkeypatch, lead_rows=[], recon=_recon(canon.STATUS_RECONCILED, 0, 0))
    from services.source_attribution_service import build_revenue_by_source
    out = build_revenue_by_source("current_quarter", now=datetime(2026, 6, 22, tzinfo=timezone.utc))
    assert out["sql_reconciliation"]["sql_scope"] == "google_ads_source_sqls"


# ═════════════════════════════════════════════════════════════════════════════
# §1 — Dashboard backend wiring (no summary.sqls as google_ads_source consumer)
# ═════════════════════════════════════════════════════════════════════════════
def test_dashboard_does_not_pass_summary_sqls_to_google_ads_source_scope():
    # The buggy pattern (campaign-attributable count checked against the
    # google_ads_source scope) must be gone.
    assert 'SCOPE_GOOGLE_ADS_SOURCE, now=now,\n        consumer_count=summary.get("sqls")' not in _DASH
    assert 'consumer_count=summary.get("sqls")' not in _DASH


def test_dashboard_headline_is_google_ads_source_scope():
    assert '"google_ads_source_sqls": _ga_source_display' in _DASH
    assert '"google_ads_source_sqls_scope"' in _DASH
    # The mart's campaign-attributable count is explicitly scoped, not a bare KPI.
    assert '"sqls_scope": _canon.SCOPE_CAMPAIGN_ATTRIBUTABLE' in _DASH


# ═════════════════════════════════════════════════════════════════════════════
# §1 — frontend contract (app.js)
# ═════════════════════════════════════════════════════════════════════════════
def test_app_renders_google_ads_source_sql_headline():
    assert 'label: "Google Ads-source SQLs"' in _APP
    assert "k.google_ads_source_sqls" in _APP
    # The old generic "SQLs" KPI is no longer keyed/labelled generically as the
    # dashboard hero SQL metric.
    assert 'key: "sqls",\n      label: "SQLs",' not in _APP


def test_app_renders_reconciliation_required_on_mismatch():
    assert '"Reconciliation required"' in _APP
    assert 'google_ads_source_sqls_status === "mismatch"' in _APP


def test_app_source_page_has_scoped_sql_chip():
    assert "function sourceGaSqlsChip" in _APP
    assert ">Google Ads-source SQLs<" in _APP
    assert "campaign-attributable" in _APP


def test_app_disclosure_of_campaign_attributable_subset():
    assert "safely campaign-attributable" in _APP


# ═════════════════════════════════════════════════════════════════════════════
# Correction 1 — Keyword Evidence coverage disclosure shows the four scoped lines
# ═════════════════════════════════════════════════════════════════════════════
def _kw_note():
    i = _APP.index("function kwSqlCoverageNote")
    return _APP[i:i + 1600]


def test_keyword_coverage_shows_four_scoped_lines():
    note = _kw_note()
    assert "Google Ads-source SQLs:" in note
    assert "Campaign-attributable SQLs:" in note
    assert "Uniquely keyword-attributed SQLs:" in note
    assert "Ambiguous/unattributed:" in note


def test_keyword_coverage_reads_reconciliation_and_attribution():
    note = _kw_note()
    assert "_kwData.sql_reconciliation" in note      # scopes from sql_reconciliation
    assert "_kwData.sql_attribution" in note          # attributed/ambiguous from sql_attribution
    assert "r.google_ads_source_sqls" in note
    assert "r.campaign_attributable_sqls" in note
    assert "s.sql_attributed_count" in note


def test_keyword_coverage_mismatch_shows_reconciliation_required():
    note = _kw_note()
    assert 'reconciliation_status === "mismatch"' in note
    assert "Reconciliation required" in note


# ═════════════════════════════════════════════════════════════════════════════
# Correction 2 — Dashboard Google Ads-source SQL headline has NO mislabelled delta
# ═════════════════════════════════════════════════════════════════════════════
def _ga_source_card():
    """The Google Ads-source SQL card entry, sliced at its own closing brace.

    This took a fixed 1100-character window, which made the test a hostage to
    comment length rather than to the code it checks: PR-ADS-155-F2 added two
    explanatory lines above `delta: ""` and pushed it past the cut-off, failing
    a case about a delta that had not moved. The card ends where the object
    literal ends, so slice there.
    """
    i = _APP.index('key: "google_ads_source_sqls"')
    end = _APP.index("\n    },", i)
    return _APP[i:end]


def test_dashboard_ga_source_card_has_no_campaign_attributable_delta():
    card = _ga_source_card()
    assert 'delta: ""' in card                         # delta removed on this card
    assert 'dashDeltaChip("sqls"' not in card          # never the campaign-attributable delta


def test_dashboard_null_campaign_attributable_shows_unavailable_not_rate():
    card = _ga_source_card()
    # When campaign_attributable_sqls is null, the card shows an honest
    # "unavailable", NOT sqlRate (which belongs to the narrower population).
    assert '? "Campaign attribution unavailable"' in card
    assert "? sqlRate" not in card                     # the old rate fallback is gone


# ═════════════════════════════════════════════════════════════════════════════
# Correction 3 — campaign-identity failure: NO safe-label fallback
# ═════════════════════════════════════════════════════════════════════════════
def test_unavailable_resolver_never_treats_a_label_as_resolved():
    ok, reason = canon.unavailable_campaign_resolver("A Perfectly Real-Looking Campaign")
    assert ok is False
    assert reason == canon.REASON_CAMPAIGN_IDENTITY_UNAVAILABLE


def test_identity_contract_unavailable_returns_no_fallback(monkeypatch):
    # Canonical spend source unavailable → identity_available False, unavailable
    # resolver (NOT the safe-label default).
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": False, "rows": []})
    resolver, available = canon._build_identity_resolver(date(2026, 7, 1), date(2026, 7, 23))
    assert available is False
    assert resolver is canon.unavailable_campaign_resolver
    assert resolver("Brand - US") == (False, canon.REASON_CAMPAIGN_IDENTITY_UNAVAILABLE)


def test_build_nulls_campaign_attributable_when_identity_unavailable(monkeypatch):
    rows = [_lead("g1", campaign="Brand - US"), _lead("g2", campaign="Brand - UK")]
    monkeypatch.setattr(
        "db.canonical_contact_outcome_repository.fetch_canonical_inputs",
        lambda start, end: {"available": True, "lead_rows": rows,
                            "exclusions": set(), "classification": []})
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": False, "rows": []})
    out = canon.build(canon.WINDOW_BUSINESS, "current_quarter",
                      now=datetime(2026, 7, 23, tzinfo=timezone.utc))
    # google_ads_source stays available; campaign_attributable is null, not 0.
    assert out["counts"]["google_ads_source_sqls"] == 2
    assert out["counts"]["campaign_attributable_sqls"] is None
    assert out["reconciliation"]["campaign_attributable_sqls"] is None
    assert out["reconciliation"]["reconciliation_status"] in (
        canon.STATUS_PARTIAL, canon.STATUS_UNAVAILABLE)


def test_reconciliation_status_partial_when_campaign_attributable_null():
    counts = {"total_all_source_sqls": 3, "google_ads_source_sqls": 2,
              "campaign_attributable_sqls": None, "excluded_sql_contacts": 0}
    meta = canon.reconciliation_metadata({"counts": counts}, canon.SCOPE_GOOGLE_ADS_SOURCE)
    assert meta["campaign_attributable_sqls"] is None
    assert meta["reconciliation_status"] == canon.STATUS_PARTIAL


def test_no_safe_label_fallback_in_source():
    src = (_ROOT / "services" / "canonical_contact_outcome_service.py").read_text()
    # The production identity builder must not silently fall back to the safe-label
    # resolver on error.
    assert "using safe-label fallback" not in src
    assert "return default_campaign_resolver" not in src


# ═════════════════════════════════════════════════════════════════════════════
# §6 — every consuming page wires the canonical reconciliation
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("path,needle", [
    ("services/dashboard_overview_service.py", "sql_reconciliation"),
    ("services/source_attribution_service.py", "sql_reconciliation"),
    ("services/revenue_decision_mart.py", "sql_reconciliation"),
    ("services/keyword_evidence_service.py", "sql_reconciliation"),
    ("services/campaign_evidence_service.py", "sql_reconciliation"),
    ("services/search_term_evidence_service.py", "sql_reconciliation"),
    ("api/server.py", "_lead_quality_sql_reconciliation"),
])
def test_page_wires_reconciliation(path, needle):
    assert needle in (_ROOT / path).read_text()
