"""
Tests for PR-ADS-104: Incremental Scheduler Cutover to Google Ads API

Validates that:
- scheduler/daily.py, weekly.py, monthly.py do NOT import connectors.windsor_pull
- All three schedulers import connectors.google_ads_source
- Google Ads dataset sync batches use source="google_ads_api"
- source="windsor" is not used for campaigns/search_terms/keywords/geo in schedulers
- connectors.google_ads_source.save_output exists
- save_output writes the four expected compatibility files
- No Google Ads mutate/write services/calls are introduced
- No UI files are touched
"""

import ast
import importlib
import json
import os
import sys
import tempfile
import types
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_source(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _parse_source(rel_path: str) -> ast.Module:
    return ast.parse(_read_source(rel_path))


def _all_import_names(tree: ast.Module) -> list[str]:
    """Return all module names referenced in import/from-import statements."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def _all_string_literals(tree: ast.Module) -> list[str]:
    """Return all string constant values found in the AST."""
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
    return literals


# ---------------------------------------------------------------------------
# Scheduler source texts and trees
# ---------------------------------------------------------------------------

SCHEDULER_PATHS = {
    "daily": "scheduler/daily.py",
    "weekly": "scheduler/weekly.py",
    "monthly": "scheduler/monthly.py",
}


@pytest.fixture(params=list(SCHEDULER_PATHS.keys()))
def scheduler_source(request):
    key = request.param
    return key, _read_source(SCHEDULER_PATHS[key]), _parse_source(SCHEDULER_PATHS[key])


# ---------------------------------------------------------------------------
# 1. No windsor_pull import in any scheduler
# ---------------------------------------------------------------------------

class TestNoWindsorImport:
    @pytest.mark.parametrize("key", list(SCHEDULER_PATHS.keys()))
    def test_no_windsor_pull_import(self, key):
        tree = _parse_source(SCHEDULER_PATHS[key])
        imports = _all_import_names(tree)
        assert "connectors.windsor_pull" not in imports, (
            f"scheduler/{key}.py must not import connectors.windsor_pull (PR-ADS-104)"
        )

    @pytest.mark.parametrize("key", list(SCHEDULER_PATHS.keys()))
    def test_no_windsor_pull_string(self, key):
        src = _read_source(SCHEDULER_PATHS[key])
        assert "windsor_pull" not in src, (
            f"scheduler/{key}.py must not reference windsor_pull"
        )


# ---------------------------------------------------------------------------
# 2. google_ads_source is imported in all schedulers
# ---------------------------------------------------------------------------

class TestGoogleAdsSourceImported:
    @pytest.mark.parametrize("key", list(SCHEDULER_PATHS.keys()))
    def test_imports_google_ads_source(self, key):
        src = _read_source(SCHEDULER_PATHS[key])
        assert "google_ads_source" in src, (
            f"scheduler/{key}.py must import connectors.google_ads_source (PR-ADS-104)"
        )


# ---------------------------------------------------------------------------
# 2b. Review-feedback window semantics remain aligned in daily/weekly schedulers
# ---------------------------------------------------------------------------

class TestReviewFeedbackWindowSemantics:
    def test_weekly_log_mentions_30d_and_60d_windows(self):
        src = _read_source("scheduler/weekly.py")
        assert "campaigns/keywords/geo=30d" in src
        assert "search_terms=60d" in src

    def test_daily_search_terms_pull_uses_two_days(self):
        tree = _parse_source("scheduler/daily.py")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id != "pull_search_terms":
                continue

            days_back_kw = next((kw for kw in node.keywords if kw.arg == "days_back"), None)
            assert days_back_kw is not None, "daily pull_search_terms call must set days_back"
            assert isinstance(days_back_kw.value, ast.Constant)
            assert days_back_kw.value.value == 2, (
                "scheduler/daily.py must use pull_search_terms(days_back=2)"
            )
            return
        pytest.fail("pull_search_terms() call not found in scheduler/daily.py")

    def test_daily_window_wording_matches_two_day_window(self):
        src = _read_source("scheduler/daily.py")
        assert "1-day window" not in src
        assert (
            "2-day window" in src or "today-1 through today" in src
        ), "scheduler/daily.py must describe the daily search-terms window as 2 days"


# ---------------------------------------------------------------------------
# 3. source="google_ads_api" used for Google Ads datasets; no source="windsor"
#    for campaigns/search_terms/keywords/geo
# ---------------------------------------------------------------------------

_ADS_DATASETS = {"campaigns", "search_terms", "keywords", "geo"}


class TestSourceLabels:
    @pytest.mark.parametrize("key", list(SCHEDULER_PATHS.keys()))
    def test_no_windsor_source_for_ads_datasets(self, key):
        """Ensure source="windsor" does not appear in sync_batch calls for Ads datasets."""
        src = _read_source(SCHEDULER_PATHS[key])
        tree = ast.parse(src)

        # Walk all keyword calls looking for start_sync_batch(source=..., dataset=...)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = ""
            if isinstance(func, ast.Attribute):
                func_name = func.attr
            elif isinstance(func, ast.Name):
                func_name = func.id
            if func_name != "start_sync_batch":
                continue

            kw_map = {kw.arg: kw.value for kw in node.keywords}
            source_node = kw_map.get("source")
            dataset_node = kw_map.get("dataset")

            if source_node is None or dataset_node is None:
                continue

            source_val = source_node.value if isinstance(source_node, ast.Constant) else None
            dataset_val = dataset_node.value if isinstance(dataset_node, ast.Constant) else None

            if dataset_val in _ADS_DATASETS:
                assert source_val != "windsor", (
                    f"scheduler/{key}.py has start_sync_batch(source='windsor', "
                    f"dataset='{dataset_val}') — must be source='google_ads_api'"
                )

    @pytest.mark.parametrize("key", list(SCHEDULER_PATHS.keys()))
    def test_google_ads_api_source_present(self, key):
        src = _read_source(SCHEDULER_PATHS[key])
        assert "google_ads_api" in src, (
            f"scheduler/{key}.py must use source='google_ads_api' for Ads datasets"
        )


# ---------------------------------------------------------------------------
# 4. save_output exists in google_ads_source
# ---------------------------------------------------------------------------

class TestGoogleAdsSourceSaveOutput:
    def test_save_output_function_defined(self):
        tree = _parse_source("connectors/google_ads_source.py")
        func_names = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        assert "save_output" in func_names, (
            "connectors/google_ads_source.py must define save_output()"
        )

    def test_save_output_accepts_four_args(self):
        """save_output signature must accept campaigns, search_terms, keywords, geos."""
        tree = _parse_source("connectors/google_ads_source.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "save_output":
                arg_names = [a.arg for a in node.args.args]
                assert arg_names == ["campaigns", "search_terms", "keywords", "geos"], (
                    f"save_output must have args (campaigns, search_terms, keywords, geos); got {arg_names}"
                )
                return
        pytest.fail("save_output not found")


# ---------------------------------------------------------------------------
# 5. save_output writes the four expected files
# ---------------------------------------------------------------------------

def _stub_and_import_google_ads_source():
    """Import connectors.google_ads_source with all heavy dependencies stubbed."""
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

    sys.modules.pop("connectors.google_ads_source", None)
    sys.modules.pop("connectors.google_ads_direct", None)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    patcher = patch.dict(sys.modules, module_stubs)
    patcher.start()
    try:
        mod = importlib.import_module("connectors.google_ads_source")
        return mod, patcher
    except Exception:
        patcher.stop()
        raise


class TestSaveOutputWritesFiles:
    def test_save_output_writes_four_files(self):
        mod, patcher = _stub_and_import_google_ads_source()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                orig_dir = os.getcwd()
                try:
                    os.chdir(tmpdir)
                    campaigns = [{"campaign": "Alpha", "spend": 100.0}]
                    search_terms = [{"search_term": "test query"}]
                    keywords = [{"keyword": "test kw"}]
                    geos = [{"country": "JO"}]
                    mod.save_output(campaigns, search_terms, keywords, geos)

                    expected_files = [
                        "data/ads_campaigns.json",
                        "data/ads_search_terms.json",
                        "data/ads_keywords.json",
                        "data/ads_geos.json",
                    ]
                    for path in expected_files:
                        assert os.path.exists(path), (
                            f"save_output must write {path}"
                        )

                    with open("data/ads_campaigns.json") as f:
                        assert json.load(f) == campaigns

                    with open("data/ads_search_terms.json") as f:
                        assert json.load(f) == search_terms

                    with open("data/ads_keywords.json") as f:
                        assert json.load(f) == keywords

                    with open("data/ads_geos.json") as f:
                        assert json.load(f) == geos
                finally:
                    os.chdir(orig_dir)
        finally:
            patcher.stop()


# ---------------------------------------------------------------------------
# 6. No Google Ads mutate/write calls in google_ads_source
# ---------------------------------------------------------------------------

_MUTATE_PATTERNS = [
    "GoogleAdsMutate",
    "CampaignServiceClient",
    "AdGroupServiceClient",
    "BudgetServiceClient",
    "KeywordPlanServiceClient",
    "NegativeKeyword",
    "campaign_budget_operation",
    "ad_group_criterion_operation",
    "upload_offline_conversion",
    ".mutate(",
    "_mutate_",
]


class TestNoMutateInGoogleAdsSource:
    def test_no_mutate_keywords(self):
        src = _read_source("connectors/google_ads_source.py")
        for kw in _MUTATE_PATTERNS:
            assert kw not in src, (
                f"connectors/google_ads_source.py must not contain mutate pattern: {kw!r}"
            )


# ---------------------------------------------------------------------------
# 7. No UI files are touched by this PR (static/ and templates/ unchanged)
# ---------------------------------------------------------------------------

class TestNoUiFilesTouched:
    """Verify the scheduler cutover PR does not introduce google_ads_source
    references inside the UI layer."""

    def _collect_ui_files(self):
        ui_dirs = [
            os.path.join(REPO_ROOT, "static"),
            os.path.join(REPO_ROOT, "templates"),
        ]
        files = []
        for d in ui_dirs:
            if not os.path.isdir(d):
                continue
            for root, _, fnames in os.walk(d):
                for fname in fnames:
                    files.append(os.path.join(root, fname))
        return files

    def test_ui_files_do_not_reference_google_ads_source(self):
        for path in self._collect_ui_files():
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue
            assert "google_ads_source" not in content, (
                f"UI file {path} must not reference google_ads_source"
            )
