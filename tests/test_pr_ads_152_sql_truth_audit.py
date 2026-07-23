"""
tests/test_pr_ads_152_sql_truth_audit.py

PR-ADS-152 §1/§5/§7 — the admin SQL-truth audit and its endpoint. Database-free:
the canonical inputs are injected, so the contact-level reconciliation (including
the production "7 vs 1"), the set differences, the window reconciliation, the
reconciliation metadata, admin protection and privacy are all proven without a DB.

Run with:
    python -m pytest tests/test_pr_ads_152_sql_truth_audit.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from services import canonical_contact_outcome_service as canon  # noqa: E402
from services import sql_truth_audit_service as audit  # noqa: E402

_NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)


def _lead(key, *, status="qualified", src="PAID_SEARCH", campaign="Brand - US",
          created="2026-07-05", run="2026-07-10", row_id=1, keyword="tms",
          source_type="paid_search", company=None):
    return {
        "contact_key": key, "contact_id": key, "row_id": row_id, "run_date": run,
        "contact_created_at": created, "status_category": status,
        "source_type": source_type, "hs_analytics_source": src,
        "campaign_name": campaign, "keyword": keyword, "country": "US",
        "company": company or f"Co {key}", "has_gclid": True,
    }


def _seven_vs_one_rows():
    """7 Google Ads-source qualified contacts in the current quarter; only 1 has a
    safe Google Ads campaign identity (the other 6 are pseudo/missing campaigns)."""
    rows = []
    for i in range(1, 8):
        campaign = "Brand - US" if i == 1 else "(direct)"
        keyword = "tms" if i == 1 else None
        rows.append(_lead(f"c{i}", campaign=campaign, keyword=keyword))
    return rows


def _windows():
    biz = canon.resolve_window_contract(canon.WINDOW_BUSINESS, "current_quarter", now=_NOW)
    ev = canon.resolve_window_contract(canon.WINDOW_EVIDENCE, "30d", now=_NOW)
    return biz, ev


def _inputs(rows, exclusions=None, classification=None):
    return {"available": True, "lead_rows": rows,
            "exclusions": exclusions or set(), "classification": classification or []}


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the 7 vs 1 is explained contact by contact
# ═════════════════════════════════════════════════════════════════════════════
def test_seven_vs_one_explained_contact_by_contact():
    biz, ev = _windows()
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev, now=_NOW)
    c = out["contracts"][0]
    assert c["revenue_by_source"]["google_ads_qualified_contacts"] == 7
    assert c["dashboard"]["campaign_attributable_sqls"] == 1
    diff = c["differences"]
    assert diff["source_only"]["count"] == 6
    # Every differing contact carries an admin-safe explanation.
    for contact in diff["source_only"]["contacts"]:
        assert contact["reasons"]
        assert canon.REASON_PSEUDO_CAMPAIGN in contact["reasons"] or \
               canon.REASON_MISSING_CAMPAIGN in contact["reasons"]


def test_difference_sets_are_named_and_use_durable_keys():
    biz, ev = _windows()
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev, now=_NOW)
    diff = out["contracts"][0]["differences"]
    for name in ("dashboard_and_keyword", "source_and_dashboard", "source_only",
                 "dashboard_only", "keyword_only"):
        assert name in diff
    # source_and_dashboard is the campaign-attributable overlap (c1).
    assert diff["source_and_dashboard"]["contact_keys"] == ["c1"]


def test_no_fabricated_zero_when_unavailable():
    biz, ev = _windows()
    out = audit.build_audit({"available": False, "lead_rows": [], "exclusions": set(),
                             "classification": []}, biz, ev, now=_NOW)
    assert out["available"] is False
    recon = out["contracts"][0]["reconciliation"]["revenue_by_source"]
    assert recon["reconciliation_status"] == canon.STATUS_UNAVAILABLE
    # Unavailable is None, never 0.
    assert recon["google_ads_source_sqls"] is None


# ═════════════════════════════════════════════════════════════════════════════
# §1 — page contract sections
# ═════════════════════════════════════════════════════════════════════════════
def test_dashboard_section_has_required_fields():
    biz, ev = _windows()
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev, now=_NOW)
    d = out["contracts"][0]["dashboard"]
    for f in ("resolved_start_date", "resolved_end_date", "total_lead_contacts",
              "total_qualified_contacts", "underlying_table", "date_field",
              "contact_key_definition", "exclusion_count",
              "pseudo_or_email_campaign_exclusion_count",
              "not_google_ads_exclusion_count"):
        assert f in d
    assert d["underlying_table"] == "leads"
    assert d["date_field"] == "contact_created_at"


def test_source_section_reports_classification_freshness():
    biz, ev = _windows()
    # 6 of 7 lack a classification row (missing); reported, never silently trusted.
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev, now=_NOW)
    s = out["contracts"][0]["revenue_by_source"]
    assert s["google_ads_qualified_contacts"] == 7
    fresh = s["classification_freshness"]
    assert fresh["missing_classification_contacts"] == 7


def test_keyword_section_coverage_statement():
    biz, ev = _windows()
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev,
                            keyword_attributable_keys={"c1"}, now=_NOW)
    k = out["contracts"][0]["keyword_evidence"]
    # total GA-source SQLs, campaign-attributable subset, uniquely-keyword subset.
    assert k["google_ads_source_sqls"] == 7
    assert k["campaign_attributable_sqls"] == 1
    assert k["uniquely_keyword_attributed"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §5 — window reconciliation (business vs evidence)
# ═════════════════════════════════════════════════════════════════════════════
def test_window_reconciliation_isolates_late_june_overlap():
    biz, ev = _windows()
    rows = _seven_vs_one_rows()
    # Add a late-June contact: only in Last 30 Days, not in Current Quarter.
    rows.append(_lead("late", created="2026-06-27", run="2026-07-01"))
    out = audit.build_audit(_inputs(rows), biz, ev, now=_NOW)
    wr = out["window_reconciliation"]
    assert wr["business_population"]["window_key"] == "current_quarter"
    assert wr["evidence_population"]["window_key"] == "30d"
    # The late-June contact exists only because Last 30 Days includes dates the
    # quarter does not.
    assert wr["evidence_only_count"] == 1
    keys = [c["contact_key"] for c in wr["evidence_only_contacts"]]
    assert "late" in keys
    assert wr["evidence_only_contacts"][0]["reason"] == canon.REASON_OUTSIDE_BUSINESS_WINDOW


def test_window_vocabularies_never_merged_in_output():
    biz, ev = _windows()
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev, now=_NOW)
    assert out["business_window"]["window_type"] == "business"
    assert out["evidence_window"]["window_type"] == "evidence"
    assert out["business_window"]["date_field"] == "contact_created_at"


# ═════════════════════════════════════════════════════════════════════════════
# §7 — reconciliation metadata + governance + privacy
# ═════════════════════════════════════════════════════════════════════════════
def test_reconciliation_metadata_present_per_page():
    biz, ev = _windows()
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev, now=_NOW)
    recon = out["contracts"][0]["reconciliation"]
    for page in ("dashboard", "revenue_by_source", "keyword_evidence"):
        block = recon[page]
        for f in ("sql_definition", "sql_date_field", "sql_dedup_key", "sql_scope",
                  "total_all_source_sqls", "google_ads_source_sqls",
                  "campaign_attributable_sqls", "excluded_sql_contacts",
                  "unmatched_sql_contacts", "reconciliation_status"):
            assert f in block


def test_governance_block_attests_read_only_no_emails():
    biz, ev = _windows()
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev, now=_NOW)
    g = out["governance"]
    assert g["read_only"] is True
    assert g["google_ads_writes"] is False
    assert g["hubspot_writes"] is False
    assert g["offline_conversion_uploads"] is False
    assert g["source_data_deletion"] is False
    assert g["emails_exposed"] is False


def test_no_email_anywhere_in_audit_payload():
    biz, ev = _windows()
    out = audit.build_audit(_inputs(_seven_vs_one_rows()), biz, ev,
                            keyword_attributable_keys={"c1"}, now=_NOW)

    # Permitted "email" tokens name email CAMPAIGNS or the governance attestation
    # — never an email address. Any other email-named field would be a leak.
    _safe = {"emails_exposed", "pseudo_or_email_campaign_exclusion_count"}

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k) not in _safe:
                    assert "email" not in str(k).lower(), f"email key leaked: {k}"
                _walk(v)
        elif isinstance(obj, list):
            for it in obj:
                _walk(it)
    _walk(out)


def test_excluded_contact_absent_from_audit_scopes():
    biz, ev = _windows()
    rows = _seven_vs_one_rows()
    out = audit.build_audit(_inputs(rows, exclusions={"c1"}), biz, ev, now=_NOW)
    c = out["contracts"][0]
    # c1 was the only campaign-attributable SQL; excluding it drops it everywhere.
    assert c["dashboard"]["campaign_attributable_sqls"] == 0
    assert c["revenue_by_source"]["google_ads_qualified_contacts"] == 6
    assert c["dashboard"]["exclusion_count"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# Endpoint — admin protection + contract
# ═════════════════════════════════════════════════════════════════════════════
def _client():
    from fastapi.testclient import TestClient
    import api.server as server
    return TestClient(server.app), server


def test_endpoint_requires_admin():
    client, _server = _client()
    # No auth → 401.
    resp = client.get("/api/audit/sql-truth")
    assert resp.status_code == 401


def test_endpoint_returns_audit_with_admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    monkeypatch.setattr(
        "db.canonical_contact_outcome_repository.fetch_canonical_inputs",
        lambda start, end: _inputs(_seven_vs_one_rows()))
    # The one campaign-attributable contact ("Brand - US") resolves to a canonical
    # Google Ads campaign id via the real identity contract; the other six do not.
    monkeypatch.setattr(
        "db.revenue_repository.fetch_canonical_campaign_spend",
        lambda s, e: {"customer_id": "X", "rows": [{"campaign_id": "1", "campaign_name": "Brand - US"}]})
    monkeypatch.setattr(
        "db.revenue_repository.fetch_campaign_identity",
        lambda cid=None: {"available": True, "mappings": []})
    client, _server = _client()
    resp = client.get("/api/audit/sql-truth?business_window=current_quarter&evidence_window=30d",
                      headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["contracts"][0]["revenue_by_source"]["google_ads_qualified_contacts"] == 7
    assert body["contracts"][0]["dashboard"]["campaign_attributable_sqls"] == 1


def test_endpoint_rejects_bad_window(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client, _server = _client()
    resp = client.get("/api/audit/sql-truth?business_window=not_a_window",
                      headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 400
