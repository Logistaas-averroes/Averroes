"""
PR-ADS-107A — Revenue Attribution Service tests.

Validates the shared read-only revenue-attribution contract:
  - output shape (window / summary / campaigns / countries) + source diagnostics
  - campaign spend read from the ACTIVE data/ads_campaigns.json (not the legacy
    Windsor-era campaign_performance.json as primary)
  - geo spend read from data/ads_geos.json; country rows do NOT pretend geo spend
    is country-resolved when ads_geos country is None
  - ROAS / CAC null safety (None when denominator is zero; never Infinity/NaN)
  - confidence labels (high / medium / low) + legacy GCLID downgrade
  - verdict classification (winner / watch / waste / learning)
  - business-window date filtering
  - read-only (no external API / write calls); no Windsor wording
"""

import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.revenue_attribution_service as svc
from services.revenue_attribution_service import (
    build_revenue_attribution,
    classify_verdict,
    compute_cac,
    compute_roas,
    confidence_from_tiers,
)

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── Fixtures: synthetic data ────────────────────────────────────────────────


def _deals():
    return [
        {  # Q2 winner: gulf / Saudi Arabia, GCLID
            "deal_id": "d1", "campaign": "gulf", "country": "Saudi Arabia",
            "amount": 10000, "hs_acv": 10000, "closedate": "2026-05-01T00:00:00Z",
            "attribution_confidence": "tier_1_gclid"},
        {  # Q2 gulf / SA, source tag
            "deal_id": "d2", "campaign": "gulf", "country": "Saudi Arabia",
            "amount": 5000, "hs_acv": 5000, "closedate": "2026-05-10T00:00:00Z",
            "attribution_confidence": "tier_2_source_tag"},
        {  # Q2 europa / France, low confidence
            "deal_id": "d3", "campaign": "europa", "country": "France",
            "amount": 2000, "hs_acv": 2000, "closedate": "2026-04-15T00:00:00Z",
            "attribution_confidence": "tier_3_spend_weighted"},
        {  # Q1 deal (used for date-window filtering)
            "deal_id": "d_old", "campaign": "gulf", "country": "Saudi Arabia",
            "amount": 99999, "hs_acv": 99999, "closedate": "2026-02-01T00:00:00Z",
            "attribution_confidence": "tier_1_gclid"},
    ]


def _campaign_spend():
    """Active Google Ads API data/ads_campaigns.json shape (no country field)."""
    return [
        {"campaign": "gulf", "campaign_id": "111", "date": "2026-05-01", "spend": 1000, "source": "google_ads_api"},
        {"campaign": "europa", "campaign_id": "222", "date": "2026-04-10", "spend": 8000, "source": "google_ads_api"},
        {"campaign": "waste-campaign", "campaign_id": "333", "date": "2026-05-05", "spend": 300, "source": "google_ads_api"},
        {"campaign": "watchco", "campaign_id": "444", "date": "2026-05-03", "spend": 200, "source": "google_ads_api"},
    ]


def _geo_spend():
    """Active Google Ads API data/ads_geos.json shape (country is None — unmapped)."""
    return [
        {"campaign": "gulf", "country_criterion_id": "2682", "country": None, "date": "2026-05-01", "spend": 1000, "source": "google_ads_api"},
        {"campaign": "europa", "country_criterion_id": "2250", "country": None, "date": "2026-04-10", "spend": 8000, "source": "google_ads_api"},
    ]


def _contacts():
    return [
        {"id": "c1", "properties": {"hs_analytics_source_data_1": "gulf",
         "mql_status": "CLOSED - Sales Qualified", "ip_country": "Saudi Arabia",
         "createdate": "2026-05-01T00:00:00Z"}},
        {"id": "c2", "properties": {"hs_analytics_source_data_1": "gulf",
         "mql_status": "OPEN - Connecting", "ip_country": "Saudi Arabia",
         "createdate": "2026-05-02T00:00:00Z"}},
        {"id": "c3", "properties": {"hs_analytics_source_data_1": "europa",
         "mql_status": "CLOSED - Deal Created", "ip_country": "France",
         "createdate": "2026-04-12T00:00:00Z"}},
        {"id": "c4", "properties": {"hs_analytics_source_data_1": "watchco",
         "mql_status": "CLOSED - Sales Qualified", "ip_country": "Jordan",
         "createdate": "2026-05-03T00:00:00Z"}},
    ]


def _build(window="all_time", *, deals=None, campaign_spend=None, geo_spend=None,
           contacts=None, campaign_status="google_ads_api",
           attribution_status="hubspot_source_tags_only"):
    deals = _deals() if deals is None else deals
    campaign_rows = _campaign_spend() if campaign_spend is None else campaign_spend
    geo_rows = _geo_spend() if geo_spend is None else geo_spend
    contacts = _contacts() if contacts is None else contacts

    file_map = {
        "google_ads_api": "data/ads_campaigns.json",
        "legacy_fallback": "data/campaign_performance.json",
        "no_spend_data": None,
    }
    cs = {"rows": campaign_rows, "status": campaign_status, "file": file_map[campaign_status]}
    gs = {"rows": geo_rows, "file": "data/ads_geos.json" if geo_rows else None}

    with patch.object(svc, "_load_attributed_deals", return_value=deals), \
         patch.object(svc, "_load_campaign_spend", return_value=cs), \
         patch.object(svc, "_load_geo_spend", return_value=gs), \
         patch.object(svc, "_load_contacts", return_value=contacts), \
         patch.object(svc, "_attribution_source_status", return_value=attribution_status):
        return build_revenue_attribution(window, now=NOW)


def _by(rows, field, value):
    for r in rows:
        if r.get(field) == value:
            return r
    return None


# ── Contract shape ───────────────────────────────────────────────────────────


def test_top_level_contract_keys():
    result = _build()
    for key in ("window", "summary", "campaigns", "countries"):
        assert key in result
    assert result["google_ads_conversion_value_used"] is False
    assert result["source_truth"] == "hubspot_closed_won_revenue"
    assert result["spend_source"] == "google_ads_api"
    assert result["revenue_source"] == "hubspot_closed_won"


def test_window_block_shape():
    result = _build("current_quarter")
    w = result["window"]
    for key in ("key", "label", "start_date", "end_date", "is_closed_window"):
        assert key in w
    assert w["key"] == "current_quarter"
    assert w["label"] == "Current Quarter"


def test_summary_shape():
    s = _build()["summary"]
    for key in ("spend", "leads", "sqls", "customers", "won_revenue", "roas", "cac", "confidence"):
        assert key in s
    assert s["leads"] == 4
    assert s["sqls"] == 3            # c1, c3, c4
    assert s["customers"] == 4       # d1, d2, d3, d_old
    assert s["spend"] == 9500        # sum of ads_campaigns spend


def test_campaign_row_shape():
    rows = _build()["campaigns"]
    gulf = _by(rows, "campaign_name", "gulf")
    assert gulf is not None
    for key in ("campaign_id", "campaign_name", "spend", "leads", "sqls", "customers",
                "won_revenue", "roas", "cac", "confidence", "verdict", "attribution_notes"):
        assert key in gulf
    assert gulf["campaign_id"] == "111"  # from ads_campaigns.json
    assert gulf["spend"] == 1000


def test_country_row_shape():
    rows = _build()["countries"]
    sa = _by(rows, "country", "Saudi Arabia")
    assert sa is not None
    for key in ("country", "country_code", "spend", "leads", "sqls", "customers",
                "won_revenue", "roas", "cac", "top_campaign", "confidence", "verdict",
                "attribution_notes"):
        assert key in sa
    assert sa["country_code"] == "SA"
    assert sa["top_campaign"] == "gulf"


# ── Source diagnostics (PR-ADS-107A patch) ───────────────────────────────────


def test_source_diagnostics_present():
    result = _build()
    assert result["spend_source"] == "google_ads_api"
    assert result["revenue_source"] == "hubspot_closed_won"
    assert result["campaign_spend_source_status"] == "google_ads_api"
    assert result["attribution_source_status"] == "hubspot_source_tags_only"
    assert "country_spend_available" in result
    assert "geo_country_mapping_status" in result
    assert isinstance(result["warnings"], list)
    assert result["files_used"]["campaign_spend"] == "data/ads_campaigns.json"
    assert result["files_used"]["geo_spend"] == "data/ads_geos.json"


def test_campaign_spend_primary_source_is_ads_campaigns():
    """_load_campaign_spend prefers data/ads_campaigns.json over the legacy file."""
    def fake_load(path: Path):
        return {
            "ads_campaigns.json": [{"campaign": "x", "spend": 5, "date": "2026-05-01"}],
            "campaign_performance.json": [{"campaign": "legacy", "spend": 999, "date": "2026-05-01"}],
        }.get(path.name, [])

    with patch.object(svc, "_load_json", side_effect=fake_load):
        loaded = svc._load_campaign_spend()
    assert loaded["status"] == "google_ads_api"
    assert loaded["file"] == "data/ads_campaigns.json"
    assert loaded["rows"][0]["campaign"] == "x"


def test_campaign_performance_is_not_primary_source():
    """When ads_campaigns.json is missing, campaign_performance.json is only an
    explicit, clearly-labeled legacy fallback — never the primary source."""
    def fake_load(path: Path):
        return {
            "ads_campaigns.json": [],  # active file missing/empty
            "campaign_performance.json": [{"campaign": "legacy", "spend": 999, "date": "2026-05-01"}],
        }.get(path.name, [])

    with patch.object(svc, "_load_json", side_effect=fake_load):
        loaded = svc._load_campaign_spend()
    assert loaded["status"] == "legacy_fallback"
    assert loaded["file"] == "data/campaign_performance.json"


def test_no_spend_data_when_no_files():
    with patch.object(svc, "_load_json", side_effect=lambda path: []):
        loaded = svc._load_campaign_spend()
    assert loaded["status"] == "no_spend_data"
    assert loaded["file"] is None
    assert loaded["rows"] == []


def test_legacy_fallback_emits_warning():
    result = _build(campaign_status="legacy_fallback")
    assert result["campaign_spend_source_status"] == "legacy_fallback"
    assert any("legacy" in w.lower() and "campaign_performance" in w for w in result["warnings"])


def test_service_module_uses_active_google_ads_files():
    import inspect
    source = inspect.getsource(svc)
    assert "ads_campaigns.json" in source
    assert "ads_geos.json" in source


# ── Country geo handling (geo country is None) ───────────────────────────────


def test_country_spend_not_available_when_geo_country_none():
    result = _build()  # default geo rows all have country=None
    assert result["country_spend_available"] is False
    assert result["geo_country_mapping_status"] == "not_implemented"
    sa = _by(result["countries"], "country", "Saudi Arabia")
    # Unmapped geo spend must NOT be merged into the named country.
    assert sa["spend"] == 0
    assert sa["roas"] is None
    assert sa["cac"] is None
    assert any("not country-resolved" in n for n in sa["attribution_notes"])


def test_unmapped_geo_spend_not_merged_into_named_countries():
    result = _build()
    total_country_spend = sum(r["spend"] for r in result["countries"])
    assert total_country_spend == 0  # geo spend is unmapped, never attributed


def test_country_spend_available_when_geo_has_country():
    geo = [
        {"campaign": "gulf", "country": "Saudi Arabia", "date": "2026-05-01", "spend": 1000},
        {"campaign": "europa", "country": "France", "date": "2026-04-10", "spend": 8000},
    ]
    result = _build(geo_spend=geo)
    assert result["country_spend_available"] is True
    assert result["geo_country_mapping_status"] == "available"
    sa = _by(result["countries"], "country", "Saudi Arabia")
    assert sa["spend"] == 1000
    assert sa["roas"] is not None  # spend now resolved


# ── ROAS / CAC null safety ───────────────────────────────────────────────────


def test_compute_roas_null_safety():
    assert compute_roas(1000, 0) is None
    assert compute_roas(0, 0) is None
    assert compute_roas(2000, 1000) == 2.0


def test_compute_cac_null_safety():
    assert compute_cac(1000, 0) is None       # zero customers
    assert compute_cac(0, 0) is None
    assert compute_cac(0, 5) is None          # zero/unavailable spend -> not "free"
    assert compute_cac(1000, 2) == 500.0


def test_no_infinity_or_nan_anywhere():
    result = _build()
    rows = result["campaigns"] + result["countries"] + [result["summary"]]
    for row in rows:
        for key in ("roas", "cac"):
            value = row.get(key)
            if value is not None:
                assert not math.isinf(value)
                assert not math.isnan(value)


def test_zero_spend_campaign_has_null_roas_and_cac():
    contacts = [{"id": "x", "properties": {"hs_analytics_source_data_1": "no_spend_co",
                "mql_status": "CLOSED - Sales Qualified", "ip_country": "Oman",
                "createdate": "2026-05-01T00:00:00Z"}}]
    result = _build(campaign_spend=[], deals=[], contacts=contacts, geo_spend=[])
    row = _by(result["campaigns"], "campaign_name", "no_spend_co")
    assert row is not None
    assert row["spend"] == 0
    assert row["roas"] is None
    assert row["cac"] is None


def test_zero_customers_has_null_cac():
    result = _build()
    waste = _by(result["campaigns"], "campaign_name", "waste-campaign")
    assert waste is not None
    assert waste["customers"] == 0
    assert waste["cac"] is None


# ── Confidence labels ────────────────────────────────────────────────────────


def test_confidence_from_tiers_labels():
    assert confidence_from_tiers(["tier_1_gclid"]) == "high"
    assert confidence_from_tiers(["tier_2_source_tag"]) == "medium"
    assert confidence_from_tiers(["tier_3_spend_weighted"]) == "low"
    assert confidence_from_tiers([]) == "low"


def test_row_confidence_values_are_labels():
    result = _build()
    valid = {"high", "medium", "low"}
    for row in result["campaigns"] + result["countries"]:
        assert row["confidence"] in valid
    assert result["summary"]["confidence"] in valid


def test_legacy_gclid_index_downgrades_high_confidence():
    """High-confidence GCLID claims from the legacy index are downgraded."""
    result = _build(attribution_status="legacy_gclid_index")
    assert result["attribution_source_status"] == "legacy_gclid_index"
    gulf = _by(result["campaigns"], "campaign_name", "gulf")
    # gulf is GCLID-dominant; legacy index => downgraded from high to medium.
    assert gulf["confidence"] == "medium"
    assert any("downgraded" in n.lower() for n in gulf["attribution_notes"])
    assert any("gclid" in w.lower() and "legacy" in w.lower() for w in result["warnings"])


def test_high_confidence_kept_when_attribution_not_legacy():
    result = _build(attribution_status="hubspot_source_tags_only")
    gulf = _by(result["campaigns"], "campaign_name", "gulf")
    assert gulf["confidence"] == "high"


# ── Verdict classification ───────────────────────────────────────────────────


def test_classify_verdict_winner():
    assert classify_verdict(1000, 5, 2, 5000, 5.0) == "winner"


def test_classify_verdict_watch_sqls_no_revenue():
    assert classify_verdict(1000, 3, 0, 0, None) == "watch"


def test_classify_verdict_watch_revenue_but_weak_roas():
    assert classify_verdict(1000, 0, 1, 200, 0.2) == "watch"


def test_classify_verdict_waste():
    assert classify_verdict(500, 0, 0, 0, None) == "waste"


def test_classify_verdict_learning_low_spend():
    assert classify_verdict(10, 0, 0, 0, None) == "learning"


def test_classify_verdict_learning_no_signal():
    assert classify_verdict(0, 0, 0, 0, None) == "learning"


def test_campaign_verdicts_match_expectations():
    result = _build()
    campaigns = result["campaigns"]
    assert _by(campaigns, "campaign_name", "gulf")["verdict"] == "winner"
    assert _by(campaigns, "campaign_name", "europa")["verdict"] == "watch"
    assert _by(campaigns, "campaign_name", "waste-campaign")["verdict"] == "waste"
    assert _by(campaigns, "campaign_name", "watchco")["verdict"] == "watch"


def test_all_verdict_values_are_in_allowed_set():
    result = _build()
    allowed = {"winner", "watch", "waste", "learning"}
    for row in result["campaigns"] + result["countries"]:
        assert row["verdict"] in allowed


# ── Business-window date filtering ───────────────────────────────────────────


def test_current_quarter_excludes_q1_deal():
    result = _build("current_quarter")
    gulf = _by(result["campaigns"], "campaign_name", "gulf")
    assert gulf["customers"] == 2
    assert gulf["won_revenue"] == 15000


def test_ytd_includes_q1_deal():
    result = _build("ytd")
    gulf = _by(result["campaigns"], "campaign_name", "gulf")
    assert gulf["customers"] == 3
    assert gulf["won_revenue"] == 114999


def test_last_quarter_only_sees_q1_deal():
    result = _build("last_quarter")
    gulf = _by(result["campaigns"], "campaign_name", "gulf")
    assert gulf["customers"] == 1
    assert gulf["won_revenue"] == 99999


def test_invalid_window_raises():
    with pytest.raises(ValueError):
        _build("60d")


# ── Read-only / no-Windsor guarantees ────────────────────────────────────────


def test_service_makes_no_external_or_write_calls():
    import inspect
    source = inspect.getsource(svc)
    assert "import requests" not in source
    assert "requests.post" not in source
    assert "requests.put" not in source
    assert "requests.patch" not in source
    assert "requests.delete" not in source
    assert "mutate" not in source.lower()
    assert "google.ads" not in source
    assert "hubapi.com" not in source
    assert "hubspot_api_client" not in source.lower()


def test_no_windsor_wording_in_service():
    import inspect
    assert "windsor" not in inspect.getsource(svc).lower()


def test_empty_inputs_produce_safe_empty_contract():
    result = _build(deals=[], campaign_spend=[], geo_spend=[], contacts=[],
                    campaign_status="no_spend_data")
    assert result["campaigns"] == []
    assert result["countries"] == []
    s = result["summary"]
    assert s["spend"] == 0
    assert s["roas"] is None
    assert s["cac"] is None
    assert s["confidence"] == "low"
