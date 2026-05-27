"""
Tests for Attribution Confidence Layer (PR-ADS-082).

Validates:
  - Module is importable.
  - Confidence definitions include all 3 tiers.
  - tier_3 maps to estimated/low trust.
  - Overall confidence is LOW when tier_3_share > 50%.
  - Overall confidence is HIGH when tier_1_share >= 70%.
  - Overall confidence is UNKNOWN when no rows.
  - Confidence summary endpoint exists in server.
  - No external write calls.
  - Frontend calls the confidence summary endpoint.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_attribution_confidence_importable():
    """Attribution confidence module can be imported."""
    import analysis.attribution_confidence as conf  # noqa: F401

    assert hasattr(conf, "get_confidence_label")
    assert hasattr(conf, "get_confidence_description")
    assert hasattr(conf, "get_confidence_severity")
    assert hasattr(conf, "summarize_roas_confidence")
    assert hasattr(conf, "decorate_roas_rows_with_confidence")
    assert hasattr(conf, "CONFIDENCE_DEFINITIONS")


def test_confidence_definitions_include_all_tiers():
    """Confidence definitions include tier_1_gclid, tier_2_source_tag, tier_3_spend_weighted."""
    from analysis.attribution_confidence import CONFIDENCE_DEFINITIONS

    assert "tier_1_gclid" in CONFIDENCE_DEFINITIONS
    assert "tier_2_source_tag" in CONFIDENCE_DEFINITIONS
    assert "tier_3_spend_weighted" in CONFIDENCE_DEFINITIONS


def test_tier3_maps_to_estimated_low():
    """tier_3_spend_weighted maps to estimated trust level and low severity."""
    from analysis.attribution_confidence import CONFIDENCE_DEFINITIONS

    tier3 = CONFIDENCE_DEFINITIONS["tier_3_spend_weighted"]
    assert tier3["trust_level"] == "estimated"
    assert tier3["severity"] == "low"


def test_tier1_maps_to_exact_high():
    """tier_1_gclid maps to exact trust level and high severity."""
    from analysis.attribution_confidence import CONFIDENCE_DEFINITIONS

    tier1 = CONFIDENCE_DEFINITIONS["tier_1_gclid"]
    assert tier1["trust_level"] == "exact"
    assert tier1["severity"] == "high"


def test_tier2_maps_to_directional_medium():
    """tier_2_source_tag maps to directional trust level and medium severity."""
    from analysis.attribution_confidence import CONFIDENCE_DEFINITIONS

    tier2 = CONFIDENCE_DEFINITIONS["tier_2_source_tag"]
    assert tier2["trust_level"] == "directional"
    assert tier2["severity"] == "medium"


def test_overall_confidence_low_when_tier3_dominant():
    """Overall confidence is LOW when tier_3_share > 50%."""
    from analysis.attribution_confidence import summarize_roas_confidence

    # 8 tier_3 rows out of 10 total = 80% tier_3
    campaigns = [
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_2_source_tag"},
    ]
    countries = [
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_2_source_tag"},
    ]
    result = summarize_roas_confidence(campaigns, countries)
    assert result["overall_confidence"] == "LOW"


def test_overall_confidence_high_when_tier1_dominant():
    """Overall confidence is HIGH when tier_1_share >= 70%."""
    from analysis.attribution_confidence import summarize_roas_confidence

    campaigns = [
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_2_source_tag"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
    ]
    result = summarize_roas_confidence(campaigns, [])
    assert result["overall_confidence"] == "HIGH"


def test_overall_confidence_medium():
    """Overall confidence is MEDIUM when tier_1 + tier_2 >= 70%."""
    from analysis.attribution_confidence import summarize_roas_confidence

    campaigns = [
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_2_source_tag"},
        {"attribution_confidence": "tier_2_source_tag"},
        {"attribution_confidence": "tier_2_source_tag"},
        {"attribution_confidence": "tier_2_source_tag"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
        {"attribution_confidence": "tier_3_spend_weighted"},
    ]
    result = summarize_roas_confidence(campaigns, [])
    assert result["overall_confidence"] == "MEDIUM"


def test_overall_confidence_unknown_no_rows():
    """Overall confidence is UNKNOWN when no rows available."""
    from analysis.attribution_confidence import summarize_roas_confidence

    result = summarize_roas_confidence([], [])
    assert result["overall_confidence"] == "UNKNOWN"


def test_get_confidence_label():
    """get_confidence_label returns correct labels."""
    from analysis.attribution_confidence import get_confidence_label

    assert get_confidence_label("tier_1_gclid") == "Exact GCLID"
    assert get_confidence_label("tier_2_source_tag") == "Source Tag"
    assert get_confidence_label("tier_3_spend_weighted") == "Estimate"
    assert get_confidence_label("unknown_tier") == "Unknown"


def test_get_confidence_severity():
    """get_confidence_severity returns correct severity levels."""
    from analysis.attribution_confidence import get_confidence_severity

    assert get_confidence_severity("tier_1_gclid") == "high"
    assert get_confidence_severity("tier_2_source_tag") == "medium"
    assert get_confidence_severity("tier_3_spend_weighted") == "low"


def test_decorate_rows():
    """decorate_roas_rows_with_confidence adds metadata to rows."""
    from analysis.attribution_confidence import decorate_roas_rows_with_confidence

    rows = [
        {"campaign": "gulf", "attribution_confidence": "tier_1_gclid"},
        {"campaign": "mena", "attribution_confidence": "tier_3_spend_weighted"},
    ]
    decorated = decorate_roas_rows_with_confidence(rows)
    assert decorated[0]["confidence_label"] == "Exact GCLID"
    assert decorated[0]["trust_level"] == "exact"
    assert decorated[1]["confidence_label"] == "Estimate"
    assert decorated[1]["trust_level"] == "estimated"


def test_summarize_counts():
    """summarize_roas_confidence correctly counts tiers."""
    from analysis.attribution_confidence import summarize_roas_confidence

    campaigns = [
        {"attribution_confidence": "tier_1_gclid"},
        {"attribution_confidence": "tier_2_source_tag"},
        {"attribution_confidence": "tier_3_spend_weighted"},
    ]
    countries = [
        {"attribution_confidence": "tier_3_spend_weighted"},
    ]
    result = summarize_roas_confidence(campaigns, countries)
    assert result["campaign_rows"] == 3
    assert result["country_rows"] == 1
    assert result["tier_1_count"] == 1
    assert result["tier_2_count"] == 1
    assert result["tier_3_count"] == 2


def test_no_external_writes():
    """Attribution confidence module has no external write operations."""
    import analysis.attribution_confidence as conf

    source = inspect.getsource(conf)
    source_lower = source.lower()

    forbidden = [
        "requests.post", "requests.put", "requests.patch",
        "oct_upload", "push_negative", "upload_conversion",
        "import hubspot", "import google_ads",
    ]
    for term in forbidden:
        assert term not in source_lower, f"Forbidden term found: {term}"


def test_confidence_summary_endpoint_exists():
    """Server contains the confidence-summary endpoint."""
    server_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api", "server.py",
    )
    with open(server_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "/api/attribution/confidence-summary" in source


def test_frontend_calls_confidence_endpoint():
    """Frontend app.js calls /api/attribution/confidence-summary."""
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "app.js",
    )
    with open(app_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "/api/attribution/confidence-summary" in source


def test_frontend_calls_gclid_readiness_endpoint():
    """Frontend app.js calls /api/attribution/gclid-readiness."""
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "app.js",
    )
    with open(app_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "/api/attribution/gclid-readiness" in source


def test_frontend_gclid_page_has_readiness_containers():
    """GCLID Attribution page contains readiness containers."""
    html_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "index.html",
    )
    with open(html_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "gclid-readiness-summary" in source
    assert "attribution-confidence-summary" in source
    assert "gclid-readiness-blockers" in source


def test_roas_pages_still_show_attribution_badges():
    """ROAS pages still use getAttributionBadge for confidence display."""
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "app.js",
    )
    with open(app_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "getAttributionBadge" in source
    assert "attribution-badge--tier1" in source
    assert "attribution-badge--tier2" in source
    assert "attribution-badge--tier3" in source
