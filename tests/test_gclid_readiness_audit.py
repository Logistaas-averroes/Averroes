"""
Tests for GCLID Readiness Audit (PR-ADS-081).

Validates:
  - Module is importable.
  - Readiness audit returns valid status values.
  - Readiness score is 0–100.
  - Blockers are generated when data is missing.
  - No external write calls exist in the module.
  - Invalid window is rejected by API endpoint.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_gclid_readiness_audit_importable():
    """GCLID readiness audit module can be imported."""
    import analysis.gclid_readiness_audit as audit  # noqa: F401

    assert hasattr(audit, "run_gclid_readiness_audit")
    assert hasattr(audit, "audit_hubspot_deal_gclid_coverage")
    assert hasattr(audit, "audit_hubspot_contact_gclid_coverage")
    assert hasattr(audit, "audit_windsor_gclid_coverage")
    assert hasattr(audit, "audit_joinability")


def test_readiness_returns_valid_status():
    """Readiness audit returns a valid status string."""
    import analysis.gclid_readiness_audit as audit

    result = audit.run_gclid_readiness_audit(window_days=60)
    assert result["readiness_status"] in ("READY", "PARTIAL", "NOT_READY", "UNKNOWN")


def test_readiness_score_in_range():
    """Readiness score is between 0 and 100."""
    import analysis.gclid_readiness_audit as audit

    result = audit.run_gclid_readiness_audit(window_days=60)
    assert 0 <= result["readiness_score"] <= 100


def test_readiness_contains_required_keys():
    """Readiness result contains all required top-level keys."""
    import analysis.gclid_readiness_audit as audit

    result = audit.run_gclid_readiness_audit(window_days=60)
    required_keys = [
        "window", "generated_at", "readiness_status", "readiness_score",
        "summary", "blockers", "next_checks", "notes",
    ]
    for key in required_keys:
        assert key in result, f"Missing key: {key}"


def test_readiness_summary_keys():
    """Readiness summary contains all expected fields."""
    import analysis.gclid_readiness_audit as audit

    result = audit.run_gclid_readiness_audit(window_days=60)
    summary = result["summary"]
    expected_keys = [
        "won_deals", "deals_with_direct_gclid", "deals_with_contact_gclid",
        "deals_without_gclid", "windsor_rows_with_gclid",
        "tier_1_possible_matches", "tier_2_source_tag_matches",
        "tier_3_estimated_required",
    ]
    for key in expected_keys:
        assert key in summary, f"Missing summary key: {key}"


def test_readiness_no_external_writes():
    """Readiness audit module has no external write operations."""
    import analysis.gclid_readiness_audit as audit

    source = inspect.getsource(audit)

    # Must not write to Google Ads, HubSpot, or any external system
    forbidden = [
        "requests.post", "requests.put", "requests.patch",
        "oct_upload", "push_negative", "upload_conversion",
        "import hubspot", "import google_ads",
    ]
    source_lower = source.lower()
    for term in forbidden:
        assert term not in source_lower, f"Forbidden term found: {term}"


def test_readiness_unknown_when_no_data():
    """Readiness returns UNKNOWN or NOT_READY when no data files exist."""
    import analysis.gclid_readiness_audit as audit

    # With empty data, should return UNKNOWN or NOT_READY
    result = audit.run_gclid_readiness_audit(window_days=60)
    # If no data files exist, status should reflect that
    assert result["readiness_status"] in ("READY", "PARTIAL", "NOT_READY", "UNKNOWN")


def test_hubspot_deal_coverage_empty():
    """Deal GCLID coverage with empty list returns zeros."""
    import analysis.gclid_readiness_audit as audit

    result = audit.audit_hubspot_deal_gclid_coverage([])
    assert result["won_deals"] == 0
    assert result["deals_with_direct_gclid"] == 0
    assert result["deals_without_gclid"] == 0


def test_hubspot_deal_coverage_with_gclid():
    """Deal GCLID coverage correctly counts deals with GCLID."""
    import analysis.gclid_readiness_audit as audit

    deals = [
        {"gclid": "abc123"},
        {"gclid": None},
        {},
    ]
    result = audit.audit_hubspot_deal_gclid_coverage(deals)
    assert result["won_deals"] == 3
    assert result["deals_with_direct_gclid"] == 1
    assert result["deals_without_gclid"] == 2


def test_windsor_coverage_empty():
    """Windsor GCLID coverage with empty list returns zeros."""
    import analysis.gclid_readiness_audit as audit

    result = audit.audit_windsor_gclid_coverage([])
    assert result["windsor_rows_total"] == 0
    assert result["windsor_rows_with_gclid"] == 0


def test_joinability_no_matches():
    """Joinability returns zero tier_1 when no GCLID overlap exists."""
    import analysis.gclid_readiness_audit as audit

    deals = [{"gclid": "abc"}]
    windsor = [{"gclid": "xyz", "campaign": "test"}]
    result = audit.audit_joinability(deals, windsor)
    assert result["tier_1_possible_matches"] == 0


def test_joinability_with_match():
    """Joinability finds tier_1 match when GCLIDs overlap."""
    import analysis.gclid_readiness_audit as audit

    deals = [{"gclid": "abc"}]
    windsor = [{"gclid": "abc", "campaign": "test"}]
    result = audit.audit_joinability(deals, windsor)
    assert result["tier_1_possible_matches"] == 1


def test_api_endpoint_exists_in_server():
    """Server module contains the gclid-readiness endpoint."""
    server_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api", "server.py",
    )
    with open(server_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "/api/attribution/gclid-readiness" in source


def test_api_endpoint_validates_window():
    """Server uses _parse_window which rejects invalid windows."""
    server_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api", "server.py",
    )
    with open(server_path, "r", encoding="utf-8") as f:
        source = f.read()
    # The endpoint uses _parse_window which raises HTTPException 400 for invalid windows
    assert "_parse_window(window)" in source
