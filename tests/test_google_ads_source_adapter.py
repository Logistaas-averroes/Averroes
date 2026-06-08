"""
Tests for PR-ADS-103: Google Ads Canonical Source Adapter

Validates normalizers, field contracts, safety constraints, and delegation
behaviour without requiring live Google Ads credentials.
"""

import ast
import importlib
import os
import sys
import types
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_PATH = os.path.join(REPO_ROOT, "connectors", "google_ads_source.py")


def _read_source(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _parse_adapter() -> ast.Module:
    with open(ADAPTER_PATH, encoding="utf-8") as f:
        return ast.parse(f.read())


def _import_adapter():
    """Import connectors.google_ads_source, stubbing out the Google Ads SDK."""
    # Stub the google.ads.googleads package so no credentials are required.
    google_stub = types.ModuleType("google")
    ads_stub = types.ModuleType("google.ads")
    googleads_stub = types.ModuleType("google.ads.googleads")
    client_stub = types.ModuleType("google.ads.googleads.client")
    errors_stub = types.ModuleType("google.ads.googleads.errors")

    client_stub.GoogleAdsClient = MagicMock()
    errors_stub.GoogleAdsException = Exception

    google_stub.ads = ads_stub
    ads_stub.googleads = googleads_stub
    googleads_stub.client = client_stub
    googleads_stub.errors = errors_stub

    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda: None
    module_stubs = {
        "google": google_stub,
        "google.ads": ads_stub,
        "google.ads.googleads": googleads_stub,
        "google.ads.googleads.client": client_stub,
        "google.ads.googleads.errors": errors_stub,
        "dotenv": dotenv_stub,
    }

    # Force reimport of only the adapter module.
    sys.modules.pop("connectors.google_ads_source", None)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    patcher = patch.dict(sys.modules, module_stubs)
    patcher.start()
    try:
        return importlib.import_module("connectors.google_ads_source"), patcher
    except Exception:
        patcher.stop()
        raise


# ---------------------------------------------------------------------------
# Fixture: loaded adapter module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def adapter():
    adapter_module, patcher = _import_adapter()
    try:
        yield adapter_module
    finally:
        patcher.stop()
        sys.modules.pop("connectors.google_ads_source", None)


# ---------------------------------------------------------------------------
# 1. Public API surface
# ---------------------------------------------------------------------------

PUBLIC_FUNCTIONS = [
    "get_date_range",
    "pull_campaign_performance",
    "pull_search_terms",
    "pull_keyword_performance",
    "pull_geo_performance",
    "pull_campaign_performance_range",
    "pull_search_terms_range",
    "pull_keyword_performance_range",
    "pull_geo_performance_range",
    "safe_divide",
    "normalize_id",
    "normalize_campaign_row",
    "normalize_search_term_row",
    "normalize_keyword_row",
    "normalize_geo_row",
]


@pytest.mark.parametrize("fn_name", PUBLIC_FUNCTIONS)
def test_public_function_exists(adapter, fn_name):
    """Every required public function must be present in the adapter."""
    assert hasattr(adapter, fn_name), f"Missing public function: {fn_name}"
    assert callable(getattr(adapter, fn_name))


# ---------------------------------------------------------------------------
# 2. safe_divide
# ---------------------------------------------------------------------------

def test_safe_divide_normal(adapter):
    assert adapter.safe_divide(10, 4) == pytest.approx(2.5)


def test_safe_divide_zero_denominator(adapter):
    assert adapter.safe_divide(100, 0) == 0.0


def test_safe_divide_zero_numerator(adapter):
    assert adapter.safe_divide(0, 5) == 0.0


def test_safe_divide_zero_both(adapter):
    assert adapter.safe_divide(0, 0) == 0.0


def test_normalize_id_none_returns_empty_string(adapter):
    assert adapter.normalize_id(None) == ""


def test_normalize_id_string_none_returns_empty_string(adapter):
    assert adapter.normalize_id("None") == ""


def test_normalize_id_numeric_returns_string(adapter):
    assert adapter.normalize_id(123456) == "123456"


# ---------------------------------------------------------------------------
# 3. normalize_campaign_row
# ---------------------------------------------------------------------------

CAMPAIGN_ROW = {
    "date": "2026-06-08",
    "campaign_id": 22546856086,
    "campaign_name": "Gulf",
    "campaign_status": "ENABLED",
    "impressions": 317,
    "clicks": 31,
    "cost_micros": 64055188,
    "spend": 64.055188,
    "conversions": 1.0,
    "conversions_value": 0.0,
}


def test_campaign_name_maps_to_campaign(adapter):
    out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    assert out["campaign"] == "Gulf"


def test_campaign_id_is_string(adapter):
    out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    assert isinstance(out["campaign_id"], str)
    assert out["campaign_id"] == "22546856086"


def test_campaign_id_none_maps_to_empty_string(adapter):
    out = adapter.normalize_campaign_row({**CAMPAIGN_ROW, "campaign_id": None})
    assert out["campaign_id"] == ""


def test_campaign_id_string_none_maps_to_empty_string(adapter):
    out = adapter.normalize_campaign_row({**CAMPAIGN_ROW, "campaign_id": "None"})
    assert out["campaign_id"] == ""


def test_campaign_spend_preserved(adapter):
    out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    assert out["spend"] == pytest.approx(64.055188)


def test_campaign_cpc_calculated(adapter):
    out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    expected_cpc = 64.055188 / 31
    assert out["cpc"] == pytest.approx(expected_cpc)


def test_campaign_ctr_calculated(adapter):
    out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    expected_ctr = 31 / 317
    assert out["ctr"] == pytest.approx(expected_ctr)


def test_campaign_conversion_rate_calculated(adapter):
    out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    expected_cr = 1.0 / 31
    assert out["conversion_rate"] == pytest.approx(expected_cr)


def test_campaign_cpc_zero_when_no_clicks(adapter):
    row = {**CAMPAIGN_ROW, "clicks": 0}
    out = adapter.normalize_campaign_row(row)
    assert out["cpc"] == 0.0


def test_campaign_ctr_zero_when_no_impressions(adapter):
    row = {**CAMPAIGN_ROW, "impressions": 0}
    out = adapter.normalize_campaign_row(row)
    assert out["ctr"] == 0.0


def test_campaign_conversion_rate_zero_when_no_clicks(adapter):
    row = {**CAMPAIGN_ROW, "clicks": 0}
    out = adapter.normalize_campaign_row(row)
    assert out["conversion_rate"] == 0.0


def test_campaign_source_field(adapter):
    out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    assert out["source"] == "google_ads_api"


def test_campaign_date_preserved(adapter):
    out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    assert out["date"] == "2026-06-08"


# ---------------------------------------------------------------------------
# 4. normalize_search_term_row
# ---------------------------------------------------------------------------

SEARCH_TERM_ROW = {
    "date": "2026-06-08",
    "search_term": "freight forwarding software",
    "campaign_id": 22488997098,
    "campaign_name": "MENA",
    "ad_group_id": 175677406221,
    "ad_group_name": "Home Page",
    "impressions": 12,
    "clicks": 1,
    "cost_micros": 870000,
    "spend": 0.87,
    "conversions": 0.0,
}


def test_search_term_exact_field_preserved(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert "search_term" in out
    assert out["search_term"] == "freight forwarding software"


def test_search_term_no_alias_query(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert "query" not in out


def test_search_term_no_alias_search_query(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert "search_query" not in out


def test_search_term_no_alias_search_term_text(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert "search_term_text" not in out


def test_search_term_campaign_name_maps(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert out["campaign"] == "MENA"


def test_search_term_ad_group_name_maps(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert out["ad_group"] == "Home Page"


def test_search_term_campaign_id_is_string(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert isinstance(out["campaign_id"], str)
    assert out["campaign_id"] == "22488997098"


def test_search_term_ad_group_id_is_string(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert isinstance(out["ad_group_id"], str)
    assert out["ad_group_id"] == "175677406221"


def test_search_term_campaign_id_none_maps_to_empty_string(adapter):
    out = adapter.normalize_search_term_row({**SEARCH_TERM_ROW, "campaign_id": None})
    assert out is not None
    assert out["campaign_id"] == ""


def test_search_term_ad_group_id_none_maps_to_empty_string(adapter):
    out = adapter.normalize_search_term_row({**SEARCH_TERM_ROW, "ad_group_id": None})
    assert out is not None
    assert out["ad_group_id"] == ""


def test_search_term_blank_returns_none(adapter):
    row = {**SEARCH_TERM_ROW, "search_term": ""}
    assert adapter.normalize_search_term_row(row) is None


def test_search_term_whitespace_returns_none(adapter):
    row = {**SEARCH_TERM_ROW, "search_term": "   "}
    assert adapter.normalize_search_term_row(row) is None


def test_search_term_null_returns_none(adapter):
    row = {**SEARCH_TERM_ROW, "search_term": None}
    assert adapter.normalize_search_term_row(row) is None


def test_search_term_missing_key_returns_none(adapter):
    row = {k: v for k, v in SEARCH_TERM_ROW.items() if k != "search_term"}
    assert adapter.normalize_search_term_row(row) is None


def test_search_term_source_field(adapter):
    out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)
    assert out is not None
    assert out["source"] == "google_ads_api"


# ---------------------------------------------------------------------------
# 5. normalize_keyword_row
# ---------------------------------------------------------------------------

KEYWORD_ROW = {
    "date": "2026-06-08",
    "campaign_id": 22488997098,
    "campaign_name": "MENA",
    "ad_group_id": 175677406221,
    "ad_group_name": "Home Page",
    "keyword_text": "transport management system",
    "keyword_match_type": "BROAD",
    "impressions": 2,
    "clicks": 1,
    "cost_micros": 1988580,
    "spend": 1.98858,
    "conversions": 0.0,
}


def test_keyword_text_maps_to_keyword(adapter):
    out = adapter.normalize_keyword_row(KEYWORD_ROW)
    assert out["keyword"] == "transport management system"


def test_keyword_match_type_uppercase(adapter):
    out = adapter.normalize_keyword_row(KEYWORD_ROW)
    assert out["match_type"] == "BROAD"


def test_keyword_match_type_exact_uppercase(adapter):
    row = {**KEYWORD_ROW, "keyword_match_type": "exact"}
    out = adapter.normalize_keyword_row(row)
    assert out["match_type"] == "EXACT"


def test_keyword_match_type_phrase_uppercase(adapter):
    row = {**KEYWORD_ROW, "keyword_match_type": "phrase"}
    out = adapter.normalize_keyword_row(row)
    assert out["match_type"] == "PHRASE"


def test_keyword_cpc_calculated(adapter):
    out = adapter.normalize_keyword_row(KEYWORD_ROW)
    assert out["cpc"] == pytest.approx(1.98858)


def test_keyword_cpc_zero_when_no_clicks(adapter):
    row = {**KEYWORD_ROW, "clicks": 0}
    out = adapter.normalize_keyword_row(row)
    assert out["cpc"] == 0.0


def test_keyword_campaign_id_is_string(adapter):
    out = adapter.normalize_keyword_row(KEYWORD_ROW)
    assert isinstance(out["campaign_id"], str)


def test_keyword_ad_group_id_is_string(adapter):
    out = adapter.normalize_keyword_row(KEYWORD_ROW)
    assert isinstance(out["ad_group_id"], str)


def test_keyword_campaign_id_none_maps_to_empty_string(adapter):
    out = adapter.normalize_keyword_row({**KEYWORD_ROW, "campaign_id": None})
    assert out["campaign_id"] == ""


def test_keyword_ad_group_id_none_maps_to_empty_string(adapter):
    out = adapter.normalize_keyword_row({**KEYWORD_ROW, "ad_group_id": None})
    assert out["ad_group_id"] == ""


def test_keyword_source_field(adapter):
    out = adapter.normalize_keyword_row(KEYWORD_ROW)
    assert out["source"] == "google_ads_api"


# ---------------------------------------------------------------------------
# 6. normalize_geo_row
# ---------------------------------------------------------------------------

GEO_ROW = {
    "date": "2026-06-08",
    "campaign_id": 22488997098,
    "campaign_name": "MENA",
    "country_criterion_id": 2400,
    "impressions": 100,
    "clicks": 5,
    "cost_micros": 12340000,
    "spend": 12.34,
    "conversions": 0.0,
}


def test_geo_country_criterion_id_preserved(adapter):
    out = adapter.normalize_geo_row(GEO_ROW)
    assert out["country_criterion_id"] == "2400"


def test_geo_country_criterion_id_is_string(adapter):
    out = adapter.normalize_geo_row(GEO_ROW)
    assert isinstance(out["country_criterion_id"], str)


def test_geo_country_is_none(adapter):
    out = adapter.normalize_geo_row(GEO_ROW)
    assert out["country"] is None


def test_geo_no_fake_country_names(adapter):
    out = adapter.normalize_geo_row(GEO_ROW)
    # country must not be a non-empty string (no invented mapping)
    assert not out["country"]


def test_geo_campaign_id_is_string(adapter):
    out = adapter.normalize_geo_row(GEO_ROW)
    assert isinstance(out["campaign_id"], str)


def test_geo_campaign_id_none_maps_to_empty_string(adapter):
    out = adapter.normalize_geo_row({**GEO_ROW, "campaign_id": None})
    assert out["campaign_id"] == ""


def test_geo_country_criterion_id_none_maps_to_empty_string(adapter):
    out = adapter.normalize_geo_row({**GEO_ROW, "country_criterion_id": None})
    assert out["country_criterion_id"] == ""


def test_geo_source_field(adapter):
    out = adapter.normalize_geo_row(GEO_ROW)
    assert out["source"] == "google_ads_api"


# ---------------------------------------------------------------------------
# 7. get_date_range
# ---------------------------------------------------------------------------

def test_get_date_range_returns_two_strings(adapter):
    start, end = adapter.get_date_range(30)
    assert isinstance(start, str)
    assert isinstance(end, str)


def test_get_date_range_window_length(adapter):
    from datetime import date
    start, end = adapter.get_date_range(30)
    delta = date.fromisoformat(end) - date.fromisoformat(start)
    assert delta.days == 29  # 30 days inclusive → difference of 29


def test_get_date_range_default_30(adapter):
    from datetime import date
    start, end = adapter.get_date_range()
    delta = date.fromisoformat(end) - date.fromisoformat(start)
    assert delta.days == 29


def test_get_date_range_60(adapter):
    from datetime import date
    start, end = adapter.get_date_range(60)
    delta = date.fromisoformat(end) - date.fromisoformat(start)
    assert delta.days == 59


# ---------------------------------------------------------------------------
# 8. days_back functions delegate to range functions
# ---------------------------------------------------------------------------

def test_pull_campaign_performance_calls_range(adapter):
    with patch.object(adapter, "pull_campaign_performance_range", return_value=[]) as mock_fn:
        adapter.pull_campaign_performance(days_back=7)
        mock_fn.assert_called_once()


def test_pull_search_terms_calls_range(adapter):
    with patch.object(adapter, "pull_search_terms_range", return_value=[]) as mock_fn:
        adapter.pull_search_terms(days_back=14)
        mock_fn.assert_called_once()


def test_pull_keyword_performance_calls_range(adapter):
    with patch.object(adapter, "pull_keyword_performance_range", return_value=[]) as mock_fn:
        adapter.pull_keyword_performance(days_back=7)
        mock_fn.assert_called_once()


def test_pull_geo_performance_calls_range(adapter):
    with patch.object(adapter, "pull_geo_performance_range", return_value=[]) as mock_fn:
        adapter.pull_geo_performance(days_back=7)
        mock_fn.assert_called_once()


# ---------------------------------------------------------------------------
# 9. Range functions call the correct google_ads_direct fetchers
# ---------------------------------------------------------------------------

def test_pull_campaign_range_calls_fetch_campaign(adapter):
    with patch.object(adapter, "fetch_campaign_performance", return_value=[]) as mock_fn:
        adapter.pull_campaign_performance_range("2026-05-01", "2026-05-31")
        mock_fn.assert_called_once_with("2026-05-01", "2026-05-31")


def test_pull_search_terms_range_calls_fetch_search_terms(adapter):
    with patch.object(adapter, "fetch_search_terms", return_value=[]) as mock_fn:
        adapter.pull_search_terms_range("2026-05-01", "2026-05-31")
        mock_fn.assert_called_once_with("2026-05-01", "2026-05-31")


def test_pull_keyword_range_calls_fetch_keyword(adapter):
    with patch.object(adapter, "fetch_keyword_performance", return_value=[]) as mock_fn:
        adapter.pull_keyword_performance_range("2026-05-01", "2026-05-31")
        mock_fn.assert_called_once_with("2026-05-01", "2026-05-31")


def test_pull_geo_range_calls_fetch_geo(adapter):
    with patch.object(adapter, "fetch_geo_performance", return_value=[]) as mock_fn:
        adapter.pull_geo_performance_range("2026-05-01", "2026-05-31")
        mock_fn.assert_called_once_with("2026-05-01", "2026-05-31")


# ---------------------------------------------------------------------------
# 10. Range functions normalise and skip blank search terms
# ---------------------------------------------------------------------------

def test_pull_search_terms_range_skips_blank(adapter):
    raw = [
        {**SEARCH_TERM_ROW, "search_term": "valid term"},
        {**SEARCH_TERM_ROW, "search_term": ""},
        {**SEARCH_TERM_ROW, "search_term": None},
    ]
    with patch.object(adapter, "fetch_search_terms", return_value=raw):
        result = adapter.pull_search_terms_range("2026-05-01", "2026-05-31")
    assert len(result) == 1
    assert result[0]["search_term"] == "valid term"


def test_pull_campaign_range_normalises_source(adapter):
    with patch.object(adapter, "fetch_campaign_performance", return_value=[CAMPAIGN_ROW]):
        result = adapter.pull_campaign_performance_range("2026-05-01", "2026-05-31")
    assert result[0]["source"] == "google_ads_api"


# ---------------------------------------------------------------------------
# 11. Safety: no Windsor imports in adapter source
# ---------------------------------------------------------------------------

def _get_import_lines(rel_path: str) -> list[str]:
    """Return all import-statement lines from a Python source file."""
    source = _read_source(rel_path)
    tree = ast.parse(source)
    import_lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_lines.append(ast.unparse(node).lower())
    return import_lines


def test_adapter_has_no_windsor_imports():
    import_lines = _get_import_lines("connectors/google_ads_source.py")
    for line in import_lines:
        assert "windsor" not in line, (
            f"connectors/google_ads_source.py must not import Windsor — found: {line}"
        )


# ---------------------------------------------------------------------------
# 12. Safety: no DB writer imports in adapter source
# ---------------------------------------------------------------------------

DB_WRITER_PATTERNS = ["db.writers", "db_writer", "dbwriter", "from db import"]


@pytest.mark.parametrize("pattern", DB_WRITER_PATTERNS)
def test_adapter_has_no_db_writer_imports(pattern):
    import_lines = _get_import_lines("connectors/google_ads_source.py")
    for line in import_lines:
        assert pattern not in line, (
            f"connectors/google_ads_source.py must not contain DB writer import: {pattern}"
        )


# ---------------------------------------------------------------------------
# 13. Safety: no Google Ads mutate/write service calls in adapter source
# ---------------------------------------------------------------------------

MUTATE_CALL_PATTERNS = [
    "campaign_service",
    "ad_group_service",
    "keyword_plan",
    "budget_service",
    "bidding_strategy",
    "offline_conversion",
    "conversion_upload",
    ".mutate(",
]


@pytest.mark.parametrize("pattern", MUTATE_CALL_PATTERNS)
def test_adapter_has_no_mutate_calls(pattern):
    source = _read_source("connectors/google_ads_source.py")
    assert pattern not in source.lower(), (
        f"connectors/google_ads_source.py must not contain mutate/write call: {pattern}"
    )


# ---------------------------------------------------------------------------
# 14. All rows include source="google_ads_api"
# ---------------------------------------------------------------------------

def test_all_output_rows_have_source_field(adapter):
    """Spot-check that each normalizer adds source=google_ads_api."""
    campaign_out = adapter.normalize_campaign_row(CAMPAIGN_ROW)
    kw_out = adapter.normalize_keyword_row(KEYWORD_ROW)
    geo_out = adapter.normalize_geo_row(GEO_ROW)
    st_out = adapter.normalize_search_term_row(SEARCH_TERM_ROW)

    for row in [campaign_out, kw_out, geo_out, st_out]:
        assert row["source"] == "google_ads_api"
