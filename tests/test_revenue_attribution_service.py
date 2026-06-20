"""
PR-ADS-107A — Revenue Attribution Service tests.

Validates the shared read-only revenue-attribution contract:
  - output shape (window / summary / campaigns / countries)
  - ROAS null safety (None when spend is zero, never Infinity/NaN)
  - CAC null safety (None when customers is zero)
  - confidence labels (high / medium / low)
  - verdict classification (winner / watch / waste / learning)
  - business-window date filtering
  - read-only (no external API / write calls)
"""

import math
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

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
            "attribution_confidence": "tier_1_gclid",
        },
        {  # Q2 gulf / SA, source tag
            "deal_id": "d2", "campaign": "gulf", "country": "Saudi Arabia",
            "amount": 5000, "hs_acv": 5000, "closedate": "2026-05-10T00:00:00Z",
            "attribution_confidence": "tier_2_source_tag",
        },
        {  # Q2 europa / France, low confidence, small revenue vs spend
            "deal_id": "d3", "campaign": "europa", "country": "France",
            "amount": 2000, "hs_acv": 2000, "closedate": "2026-04-15T00:00:00Z",
            "attribution_confidence": "tier_3_spend_weighted",
        },
        {  # Q1 deal (used for date-window filtering)
            "deal_id": "d_old", "campaign": "gulf", "country": "Saudi Arabia",
            "amount": 99999, "hs_acv": 99999, "closedate": "2026-02-01T00:00:00Z",
            "attribution_confidence": "tier_1_gclid",
        },
    ]


def _spend():
    return [
        {"campaign": "gulf", "country": "Saudi Arabia", "date": "2026-05-01", "spend": 1000},
        {"campaign": "europa", "country": "France", "date": "2026-04-10", "spend": 8000},
        {"campaign": "waste-campaign", "country": "Egypt", "date": "2026-05-05", "spend": 300},
        {"campaign": "watchco", "country": "Jordan", "date": "2026-05-03", "spend": 200},
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


def _build(window="all_time", deals=None, spend=None, contacts=None):
    with patch.object(svc, "_load_attributed_deals", return_value=deals if deals is not None else _deals()), \
         patch.object(svc, "_load_spend_rows", return_value=spend if spend is not None else _spend()), \
         patch.object(svc, "_load_contacts", return_value=contacts if contacts is not None else _contacts()):
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
    # all_time: everything in fixtures is included.
    assert s["leads"] == 4
    assert s["sqls"] == 3  # c1, c3, c4
    assert s["customers"] == 4  # d1, d2, d3, d_old
    assert s["spend"] == 9500


def test_campaign_row_shape():
    rows = _build()["campaigns"]
    gulf = _by(rows, "campaign_name", "gulf")
    assert gulf is not None
    for key in ("campaign_id", "campaign_name", "spend", "leads", "sqls", "customers",
                "won_revenue", "roas", "cac", "confidence", "verdict", "attribution_notes"):
        assert key in gulf
    assert isinstance(gulf["attribution_notes"], list)


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


# ── ROAS / CAC null safety ───────────────────────────────────────────────────


def test_compute_roas_null_safety():
    assert compute_roas(1000, 0) is None       # zero spend
    assert compute_roas(0, 0) is None
    assert compute_roas(1000, 0.0) is None
    assert compute_roas(2000, 1000) == 2.0


def test_compute_cac_null_safety():
    assert compute_cac(1000, 0) is None         # zero customers
    assert compute_cac(0, 0) is None
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


def test_zero_spend_campaign_has_null_roas():
    # A contact-only campaign with no spend and no deals -> spend 0 -> roas None.
    contacts = [{"id": "x", "properties": {"hs_analytics_source_data_1": "no_spend_co",
                "mql_status": "CLOSED - Sales Qualified", "ip_country": "Oman",
                "createdate": "2026-05-01T00:00:00Z"}}]
    result = _build(spend=[], deals=[], contacts=contacts)
    row = _by(result["campaigns"], "campaign_name", "no_spend_co")
    assert row is not None
    assert row["spend"] == 0
    assert row["roas"] is None


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
    assert confidence_from_tiers([]) == "low"  # no deals => inferred => low


def test_row_confidence_values_are_labels():
    result = _build()
    valid = {"high", "medium", "low"}
    for row in result["campaigns"] + result["countries"]:
        assert row["confidence"] in valid
    assert result["summary"]["confidence"] in valid


def test_europa_low_confidence():
    result = _build()
    europa = _by(result["campaigns"], "campaign_name", "europa")
    assert europa["confidence"] == "low"  # tier_3_spend_weighted


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


def test_row_verdicts_match_expectations():
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
    # d_old (Feb, Q1) excluded; only d1 + d2 remain.
    assert gulf["customers"] == 2
    assert gulf["won_revenue"] == 15000


def test_ytd_includes_q1_deal():
    result = _build("ytd")
    gulf = _by(result["campaigns"], "campaign_name", "gulf")
    assert gulf["customers"] == 3
    assert gulf["won_revenue"] == 114999


def test_last_quarter_only_sees_q1_deal():
    result = _build("last_quarter")  # Q1 2026
    gulf = _by(result["campaigns"], "campaign_name", "gulf")
    assert gulf["customers"] == 1
    assert gulf["won_revenue"] == 99999


def test_invalid_window_raises():
    import pytest
    with pytest.raises(ValueError):
        _build("60d")


# ── Read-only guarantees ─────────────────────────────────────────────────────


def test_service_makes_no_external_or_write_calls():
    import inspect

    source = inspect.getsource(svc)
    # No network clients.
    assert "import requests" not in source
    assert "requests.post" not in source
    assert "requests.put" not in source
    assert "requests.patch" not in source
    assert "requests.delete" not in source
    # No Google Ads mutate / OCT.
    assert "mutate" not in source.lower()
    assert "google.ads" not in source
    # No HubSpot writes.
    assert "hubapi.com" not in source
    assert "hubspot_api_client" not in source.lower()


def test_empty_inputs_produce_safe_empty_contract():
    result = _build(deals=[], spend=[], contacts=[])
    assert result["campaigns"] == []
    assert result["countries"] == []
    s = result["summary"]
    assert s["spend"] == 0
    assert s["roas"] is None
    assert s["cac"] is None
    assert s["confidence"] == "low"
