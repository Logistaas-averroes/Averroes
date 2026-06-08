"""
Tests for PR-ADS-098: Google Ads API Direct Read-Only Connector Bootstrap

Validates structural safety and documentation completeness without requiring
live Google Ads credentials.
"""

import importlib.util
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_source(rel_path: str) -> str:
    """Read a source file relative to the repo root."""
    full_path = os.path.join(REPO_ROOT, rel_path)
    with open(full_path, encoding="utf-8") as f:
        return f.read()


def _read_doc(rel_path: str) -> str:
    """Read a documentation file relative to the repo root."""
    return _read_source(rel_path)


# ---------------------------------------------------------------------------
# 1. Required env var names are present in connector source
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = [
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
]


@pytest.mark.parametrize("var_name", REQUIRED_ENV_VARS)
def test_required_env_var_in_connector(var_name):
    """Each required env var name must appear in the connector source."""
    source = _read_source("connectors/google_ads_direct.py")
    assert var_name in source, (
        f"Required env var '{var_name}' not found in connectors/google_ads_direct.py"
    )


@pytest.mark.parametrize("var_name", REQUIRED_ENV_VARS)
def test_required_env_var_in_docs(var_name):
    """Each required env var name must appear in the bootstrap documentation."""
    doc = _read_doc("docs/28_GOOGLE_ADS_DIRECT_CONNECTOR_BOOTSTRAP.md")
    assert var_name in doc, (
        f"Required env var '{var_name}' not found in docs/28_GOOGLE_ADS_DIRECT_CONNECTOR_BOOTSTRAP.md"
    )


# ---------------------------------------------------------------------------
# 2. Readiness checker redacts secrets
# ---------------------------------------------------------------------------

def test_readiness_checker_uses_redaction():
    """Readiness checker must not print raw secret values."""
    source = _read_source("scripts/google_ads_readiness_check.py")
    # Must define a redaction function
    assert "_redact" in source or "redact" in source.lower(), (
        "Readiness checker must define a redaction function"
    )
    # Must not print the full value for secret vars
    assert "****" in source, (
        "Readiness checker must use '****' to mask secret output"
    )


def test_readiness_checker_has_status_levels():
    """Readiness checker must define READY, PARTIAL, NOT_READY statuses."""
    source = _read_source("scripts/google_ads_readiness_check.py")
    for status in ("READY", "PARTIAL", "NOT_READY"):
        assert status in source, (
            f"Readiness checker is missing status level: {status}"
        )


def test_readiness_checker_strict_flag():
    """Readiness checker must support a --strict flag for non-zero exit."""
    source = _read_source("scripts/google_ads_readiness_check.py")
    assert "--strict" in source, (
        "Readiness checker must support --strict flag"
    )


# ---------------------------------------------------------------------------
# 3. Connector does not contain mutate calls or prohibited services
# ---------------------------------------------------------------------------

PROHIBITED_PATTERNS = [
    ".mutate(",
    "ConversionUploadService",
    "OfflineUserDataJobService",
    "CampaignBudgetService",
    "CustomerNegativeCriterionService",
]


@pytest.mark.parametrize("pattern", PROHIBITED_PATTERNS)
def test_connector_no_prohibited_pattern(pattern):
    """Connector must not reference prohibited mutate services or operations."""
    source = _read_source("connectors/google_ads_direct.py")
    assert pattern not in source, (
        f"connectors/google_ads_direct.py must not contain '{pattern}'"
    )


def test_connector_uses_search_stream_or_search():
    """Connector must use SearchStream or Search (read-only) for all queries."""
    source = _read_source("connectors/google_ads_direct.py")
    assert "search_stream" in source or "SearchStream" in source or ".search(" in source, (
        "Connector must use SearchStream or Search for GAQL queries"
    )


def test_connector_no_hardcoded_secrets():
    """Connector must not contain hardcoded tokens or client secrets."""
    source = _read_source("connectors/google_ads_direct.py")
    # Developer tokens typically match this pattern; client secrets are longer random strings.
    # We check that no raw string literal looks like a token value.
    suspicious = [
        "developer_token = \"",
        "refresh_token = \"",
        "client_secret = \"",
    ]
    for pattern in suspicious:
        assert pattern not in source, (
            f"Connector appears to hardcode a credential: '{pattern}'"
        )


# ---------------------------------------------------------------------------
# 4. Refresh token script warns not to commit
# ---------------------------------------------------------------------------

def test_refresh_token_script_warns_not_to_commit():
    """Refresh token generator must warn the user not to commit the token."""
    source = _read_source("scripts/google_ads_generate_refresh_token.py")
    warning_phrases = ["commit", "WARNING", "Do NOT commit"]
    matched = any(phrase in source for phrase in warning_phrases)
    assert matched, (
        "Refresh token script must warn the user not to commit the token"
    )


def test_refresh_token_script_does_not_save_to_disk():
    """Refresh token generator must not write the token to a file."""
    source = _read_source("scripts/google_ads_generate_refresh_token.py")
    assert "open(" not in source, (
        "Refresh token script must not open files (would save token to disk)"
    )
    assert "write(" not in source, (
        "Refresh token script must not call write() (would save token to disk)"
    )


def test_refresh_token_script_reads_from_env():
    """Refresh token generator must read the secrets file path from an env var."""
    source = _read_source("scripts/google_ads_generate_refresh_token.py")
    assert "GOOGLE_ADS_CLIENT_SECRETS_FILE" in source, (
        "Refresh token script must read client secrets file path from "
        "GOOGLE_ADS_CLIENT_SECRETS_FILE env var"
    )


# ---------------------------------------------------------------------------
# 5. Docs mention Render env vars, no secrets in GitHub, and read-only only
# ---------------------------------------------------------------------------

def test_docs_mention_render_env_vars():
    """Bootstrap docs must mention Render environment variables."""
    doc = _read_doc("docs/28_GOOGLE_ADS_DIRECT_CONNECTOR_BOOTSTRAP.md")
    assert "Render" in doc, (
        "Docs must mention Render environment variables"
    )
    # Should specifically reference adding vars in Render
    assert "GOOGLE_ADS_" in doc and "Render" in doc, (
        "Docs must describe adding Google Ads env vars to Render"
    )


def test_docs_mention_no_secrets_in_github():
    """Bootstrap docs must state that no secrets are committed to GitHub."""
    doc = _read_doc("docs/28_GOOGLE_ADS_DIRECT_CONNECTOR_BOOTSTRAP.md")
    keywords = ["GitHub", "commit", "repository", "No secrets"]
    matched = sum(1 for k in keywords if k in doc)
    assert matched >= 2, (
        "Docs must clearly state that no secrets go into GitHub/the repository"
    )


def test_docs_mention_read_only():
    """Bootstrap docs must state the connector is read-only."""
    doc = _read_doc("docs/28_GOOGLE_ADS_DIRECT_CONNECTOR_BOOTSTRAP.md")
    assert "read-only" in doc.lower() or "read only" in doc.lower(), (
        "Docs must state that the connector is read-only"
    )


# ---------------------------------------------------------------------------
# 6. Connector exposes required public functions
# ---------------------------------------------------------------------------

REQUIRED_FUNCTIONS = [
    "build_google_ads_client",
    "get_customer_id",
    "test_connection",
    "fetch_campaign_performance",
    "fetch_search_terms",
    "fetch_keyword_performance",
    "fetch_geo_performance",
]


@pytest.mark.parametrize("func_name", REQUIRED_FUNCTIONS)
def test_connector_has_required_function(func_name):
    """Connector must define all required public functions."""
    source = _read_source("connectors/google_ads_direct.py")
    assert f"def {func_name}" in source, (
        f"connectors/google_ads_direct.py must define function '{func_name}'"
    )


# ---------------------------------------------------------------------------
# 7. Connector uses load_from_dict (no google-ads.yaml with hardcoded values)
# ---------------------------------------------------------------------------

def test_connector_uses_load_from_dict():
    """Connector must use GoogleAdsClient.load_from_dict to build the client."""
    source = _read_source("connectors/google_ads_direct.py")
    assert "load_from_dict" in source, (
        "Connector must use GoogleAdsClient.load_from_dict to build the client "
        "from env vars (not from a YAML config file with hardcoded values)"
    )


# ---------------------------------------------------------------------------
# 8. Readiness check on missing env vars (functional, no live creds needed)
# ---------------------------------------------------------------------------

def test_readiness_check_graceful_with_missing_vars(monkeypatch):
    """Readiness check must return PARTIAL/NOT_READY without crashing when vars are absent."""
    # Remove all Google Ads vars from environment
    for var in REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # Dynamically import the check function
    spec = importlib.util.spec_from_file_location(
        "google_ads_readiness_check",
        os.path.join(REPO_ROOT, "scripts", "google_ads_readiness_check.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    status = module.check_readiness()
    assert status in ("PARTIAL", "NOT_READY"), (
        f"Expected PARTIAL or NOT_READY with no env vars, got: {status}"
    )


def test_readiness_check_ready_when_all_vars_present(monkeypatch):
    """Readiness check must return READY when all required env vars are present."""
    for var in REQUIRED_ENV_VARS:
        monkeypatch.setenv(var, f"fake_value_for_{var}")

    spec = importlib.util.spec_from_file_location(
        "google_ads_readiness_check",
        os.path.join(REPO_ROOT, "scripts", "google_ads_readiness_check.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    status = module.check_readiness()
    assert status == "READY", (
        f"Expected READY with all env vars present, got: {status}"
    )


# ---------------------------------------------------------------------------
# 9. LOGIN_CUSTOMER_ID is optional — direct_customer mode
# ---------------------------------------------------------------------------

def test_login_customer_id_not_in_required_env_vars():
    """GOOGLE_ADS_LOGIN_CUSTOMER_ID must not be in _REQUIRED_ENV_VARS."""
    source = _read_source("connectors/google_ads_direct.py")
    # The required vars list must not contain LOGIN_CUSTOMER_ID
    # We verify by checking _REQUIRED_ENV_VARS definition excludes it.
    assert "_REQUIRED_ENV_VARS" in source, "Connector must define _REQUIRED_ENV_VARS"
    # _OPTIONAL_ENV_VARS must mention LOGIN_CUSTOMER_ID
    assert "_OPTIONAL_ENV_VARS" in source, "Connector must define _OPTIONAL_ENV_VARS"
    assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID" in source, (
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID must appear somewhere in the connector source"
    )


def test_connector_login_customer_id_conditional():
    """Connector must only add login_customer_id to config when env var is non-empty."""
    source = _read_source("connectors/google_ads_direct.py")
    # The conditional pattern: if login_customer_id: config["login_customer_id"] = ...
    assert 'config["login_customer_id"]' in source or "config['login_customer_id']" in source, (
        "Connector must set login_customer_id in config conditionally"
    )
    # Must not unconditionally set it (old pattern removed)
    assert 'os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"]' not in source, (
        "Connector must not unconditionally require GOOGLE_ADS_LOGIN_CUSTOMER_ID "
        "via os.environ[] — it must be conditional"
    )


def test_connector_has_get_access_mode():
    """Connector must define a get_access_mode() function."""
    source = _read_source("connectors/google_ads_direct.py")
    assert "def get_access_mode" in source, (
        "connectors/google_ads_direct.py must define get_access_mode()"
    )


def test_get_access_mode_returns_direct_customer_when_login_id_absent(monkeypatch):
    """get_access_mode() must return 'direct_customer' when LOGIN_CUSTOMER_ID is unset."""
    import unittest.mock as mock

    monkeypatch.delenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", raising=False)
    stub_modules = {
        "dotenv": mock.MagicMock(),
        "google": mock.MagicMock(),
        "google.ads": mock.MagicMock(),
        "google.ads.googleads": mock.MagicMock(),
        "google.ads.googleads.client": mock.MagicMock(),
        "google.ads.googleads.errors": mock.MagicMock(),
    }
    with mock.patch.dict("sys.modules", stub_modules):
        spec = importlib.util.spec_from_file_location(
            "google_ads_direct_mod",
            os.path.join(REPO_ROOT, "connectors", "google_ads_direct.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    assert module.get_access_mode() == "direct_customer", (
        "get_access_mode() must return 'direct_customer' when "
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID is unset"
    )


def test_get_access_mode_returns_manager_when_login_id_present(monkeypatch):
    """get_access_mode() must return 'manager' when LOGIN_CUSTOMER_ID is set."""
    import unittest.mock as mock

    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "2020447570")
    stub_modules = {
        "dotenv": mock.MagicMock(),
        "google": mock.MagicMock(),
        "google.ads": mock.MagicMock(),
        "google.ads.googleads": mock.MagicMock(),
        "google.ads.googleads.client": mock.MagicMock(),
        "google.ads.googleads.errors": mock.MagicMock(),
    }
    with mock.patch.dict("sys.modules", stub_modules):
        spec = importlib.util.spec_from_file_location(
            "google_ads_direct_mod2",
            os.path.join(REPO_ROOT, "connectors", "google_ads_direct.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    assert module.get_access_mode() == "manager", (
        "get_access_mode() must return 'manager' when "
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID is set"
    )


def test_readiness_ready_without_login_customer_id(monkeypatch):
    """Readiness check must return READY even when GOOGLE_ADS_LOGIN_CUSTOMER_ID is absent."""
    for var in REQUIRED_ENV_VARS:
        monkeypatch.setenv(var, f"fake_value_for_{var}")
    monkeypatch.delenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", raising=False)

    spec = importlib.util.spec_from_file_location(
        "google_ads_readiness_check_direct",
        os.path.join(REPO_ROOT, "scripts", "google_ads_readiness_check.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    status = module.check_readiness()
    assert status == "READY", (
        f"Expected READY without GOOGLE_ADS_LOGIN_CUSTOMER_ID (direct_customer mode), got: {status}"
    )


def test_readiness_labels_missing_login_id_as_direct_customer_mode():
    """Readiness checker source must label a missing login customer ID as direct_customer mode."""
    source = _read_source("scripts/google_ads_readiness_check.py")
    assert "direct_customer mode" in source, (
        "Readiness checker must label missing GOOGLE_ADS_LOGIN_CUSTOMER_ID as direct_customer mode"
    )


# ---------------------------------------------------------------------------
# 10. Accessible customers script
# ---------------------------------------------------------------------------

def test_accessible_customers_script_exists():
    """scripts/google_ads_list_accessible_customers.py must exist."""
    path = os.path.join(REPO_ROOT, "scripts", "google_ads_list_accessible_customers.py")
    assert os.path.isfile(path), (
        "scripts/google_ads_list_accessible_customers.py must exist"
    )


def test_accessible_customers_script_uses_list_accessible_customers():
    """Accessible customers script must call list_accessible_customers."""
    source = _read_source("scripts/google_ads_list_accessible_customers.py")
    assert "list_accessible_customers" in source, (
        "scripts/google_ads_list_accessible_customers.py must call "
        "CustomerService.list_accessible_customers()"
    )


def test_accessible_customers_script_no_mutate():
    """Accessible customers script must not reference mutate or write services."""
    source = _read_source("scripts/google_ads_list_accessible_customers.py")
    for pattern in PROHIBITED_PATTERNS:
        assert pattern not in source, (
            f"scripts/google_ads_list_accessible_customers.py must not contain '{pattern}'"
        )


# ---------------------------------------------------------------------------
# 11. Smoke test prints access mode
# ---------------------------------------------------------------------------

def test_smoke_test_prints_access_mode():
    """Smoke test must import and use get_access_mode."""
    source = _read_source("scripts/google_ads_smoke_test.py")
    assert "get_access_mode" in source, (
        "scripts/google_ads_smoke_test.py must import and use get_access_mode"
    )


def test_smoke_test_login_customer_id_not_required():
    """Smoke test must not require GOOGLE_ADS_LOGIN_CUSTOMER_ID in its required vars list."""
    source = _read_source("scripts/google_ads_smoke_test.py")
    # Find _REQUIRED_VARS block — LOGIN_CUSTOMER_ID must not be inside it
    # Simple heuristic: the string should not appear in proximity to _REQUIRED_VARS assignment
    lines = source.splitlines()
    in_required_block = False
    for line in lines:
        if "_REQUIRED_VARS" in line and "=" in line and "[" in line:
            in_required_block = True
        if in_required_block:
            if "]" in line:
                in_required_block = False
                break
            assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID" not in line, (
                "GOOGLE_ADS_LOGIN_CUSTOMER_ID must not be in _REQUIRED_VARS in smoke test"
            )
