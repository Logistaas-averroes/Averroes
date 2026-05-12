"""
tests/test_dataset_freshness_ui_contract.py

PR-ADS-074 — Dataset-Level Freshness Truth Across Dashboard Pages.

Tests cover:
  - PAGE_DATASET_MAP includes all major data pages.
  - Every dataset key in PAGE_DATASET_MAP is a known "source/dataset" string.
  - Derived pages (ngrams, waste) map to windsor/search_terms.
  - Multi-dataset pages (action_queue) reference the expected composite keys.
  - "Unknown" state is described as freshness-unverified, never as "failed".
  - No unsafe action wording appears in the freshness display copy.
  - /api/datasets/freshness response includes dataset_key field.
  - Status labels used in the API summary are the canonical set.

Run with:
    python -m pytest tests/test_dataset_freshness_ui_contract.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Helpers — extract PAGE_DATASET_MAP from app.js without executing JS
# ---------------------------------------------------------------------------

_APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"
_APP_JS_TEXT = _APP_JS.read_text(encoding="utf-8")

# Known canonical freshness dataset keys (must match _KNOWN_DATASETS in server.py)
KNOWN_DATASET_KEYS: set[str] = {
    "windsor/campaigns",
    "windsor/keywords",
    "windsor/search_terms",
    "windsor/geo",
    "hubspot/contacts",
    "hubspot/deals",
    "gclid/matches",
}

# Pages that are expected to have dataset-level freshness strips.
EXPECTED_PAGES: set[str] = {
    "campaigns",
    "waste",
    "search_terms",
    "ngrams",
    "geo",
    "keywords",
    "lead_quality",
    "deals",
    "gclid_attribution",
    "in_progress_leads",
    "action_queue",
}

# Derived pages whose mapping must point exclusively at windsor/search_terms
DERIVED_PAGES: set[str] = {"ngrams", "waste"}

# ---------------------------------------------------------------------------
# Parse PAGE_DATASET_MAP from app.js source (simple pattern extraction)
# ---------------------------------------------------------------------------

def _extract_page_dataset_map() -> dict[str, list[str]]:
    """Return the PAGE_DATASET_MAP keys found in app.js."""
    # Find the block: const PAGE_DATASET_MAP = { ... };
    # Use a non-greedy match; the map only contains string arrays (no nested {}),
    # so stopping at the first } after the opening { is reliable.
    match = re.search(
        r"const\s+PAGE_DATASET_MAP\s*=\s*\{([^}]+)\}",
        _APP_JS_TEXT,
        re.DOTALL,
    )
    if not match:
        return {}

    block = match.group(1)
    result: dict[str, list[str]] = {}

    # Each line looks like:  key: ["a/b", "c/d"],
    for line in block.splitlines():
        line = line.strip().rstrip(",")
        entry = re.match(r'(\w+)\s*:\s*\[([^\]]*)\]', line)
        if not entry:
            continue
        page_key = entry.group(1)
        raw_values = entry.group(2)
        dataset_keys = [v.strip().strip('"').strip("'") for v in raw_values.split(",") if v.strip()]
        result[page_key] = dataset_keys

    return result


_PAGE_MAP = _extract_page_dataset_map()


# ---------------------------------------------------------------------------
# Tests: PAGE_DATASET_MAP structure
# ---------------------------------------------------------------------------

class TestPageDatasetMapExists:
    """PAGE_DATASET_MAP must exist and be non-empty."""

    def test_map_parsed(self):
        assert _PAGE_MAP, "PAGE_DATASET_MAP not found or empty in app.js"

    def test_has_at_least_expected_page_count(self):
        assert len(_PAGE_MAP) >= len(EXPECTED_PAGES), (
            f"Expected at least {len(EXPECTED_PAGES)} pages in PAGE_DATASET_MAP, "
            f"found {len(_PAGE_MAP)}"
        )


class TestPageDatasetMapCoverage:
    """All major data pages must be present."""

    def test_all_expected_pages_present(self):
        missing = EXPECTED_PAGES - set(_PAGE_MAP.keys())
        assert not missing, f"Pages missing from PAGE_DATASET_MAP: {sorted(missing)}"


class TestPageDatasetMapValues:
    """Every dataset key in the map must be a recognised canonical key."""

    def test_all_dataset_keys_are_known(self):
        unknown_keys: list[str] = []
        for page, keys in _PAGE_MAP.items():
            for k in keys:
                if k not in KNOWN_DATASET_KEYS:
                    unknown_keys.append(f"{page} → {k}")
        assert not unknown_keys, (
            "PAGE_DATASET_MAP references unknown dataset key(s): "
            + "; ".join(unknown_keys)
        )

    def test_no_empty_key_lists(self):
        empty = [p for p, keys in _PAGE_MAP.items() if not keys]
        assert not empty, f"Pages with empty dataset key list: {empty}"


# ---------------------------------------------------------------------------
# Tests: Derived pages
# ---------------------------------------------------------------------------

class TestDerivedPages:
    """Derived pages must map to their source dataset, not a non-existent key."""

    def test_ngrams_maps_to_search_terms(self):
        assert "ngrams" in _PAGE_MAP, "ngrams not in PAGE_DATASET_MAP"
        assert "windsor/search_terms" in _PAGE_MAP["ngrams"], (
            "ngrams page must map to windsor/search_terms (derived dataset)"
        )

    def test_waste_maps_to_search_terms(self):
        assert "waste" in _PAGE_MAP, "waste not in PAGE_DATASET_MAP"
        assert "windsor/search_terms" in _PAGE_MAP["waste"], (
            "waste page must map to windsor/search_terms (derived dataset)"
        )

    def test_derived_pages_not_mapped_to_nonexistent_waste_terms_key(self):
        """waste_terms is not a tracked sync_state dataset — must not appear as key."""
        for page in DERIVED_PAGES:
            if page in _PAGE_MAP:
                assert "waste_terms" not in _PAGE_MAP[page], (
                    f"{page} maps to 'waste_terms' which is not a tracked sync_state dataset. "
                    "Use windsor/search_terms instead."
                )


# ---------------------------------------------------------------------------
# Tests: Multi-dataset pages
# ---------------------------------------------------------------------------

class TestMultiDatasetPages:
    """Action queue and similar pages must list multiple datasets."""

    def test_action_queue_has_multiple_datasets(self):
        assert "action_queue" in _PAGE_MAP, "action_queue not in PAGE_DATASET_MAP"
        assert len(_PAGE_MAP["action_queue"]) > 1, (
            "action_queue should reference multiple datasets"
        )

    def test_action_queue_includes_campaigns_and_contacts(self):
        keys = _PAGE_MAP.get("action_queue", [])
        assert "windsor/campaigns" in keys, "action_queue must include windsor/campaigns"
        assert "hubspot/contacts" in keys, "action_queue must include hubspot/contacts"

    def test_multi_dataset_summary_tracks_running_and_unknown(self):
        """Multi-dataset summary must include running and unknown counts, not just fresh/stale/failed."""
        match = re.search(
            r"function renderPageDatasetFreshness\(.+?(?=\nfunction |\nconst |\nasync function |\Z)",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        assert match, "renderPageDatasetFreshness not found"
        fn_text = match.group(0)
        # The multi-dataset block must count runningCount and unknownCount
        assert "runningCount" in fn_text, (
            "Multi-dataset summary must track runningCount (not only fresh/stale/failed)"
        )
        assert "unknownCount" in fn_text, (
            "Multi-dataset summary must track unknownCount (not only fresh/stale/failed)"
        )


# ---------------------------------------------------------------------------
# Tests: renderPageDatasetFreshness function exists in app.js
# ---------------------------------------------------------------------------

class TestRenderFunctionPresent:
    """renderPageDatasetFreshness must be declared in app.js."""

    def test_render_function_declared(self):
        assert "function renderPageDatasetFreshness(" in _APP_JS_TEXT, (
            "renderPageDatasetFreshness() not found in app.js"
        )

    def test_fallback_to_renderRunMeta(self):
        # The new function must fall back to renderRunMeta when cache is empty.
        assert "renderRunMeta(" in _APP_JS_TEXT, (
            "renderRunMeta fallback not found in app.js"
        )


# ---------------------------------------------------------------------------
# Tests: Status label consistency
# ---------------------------------------------------------------------------

class TestStatusLabelConsistency:
    """Status labels in page strips must match System Health table labels."""

    # These are the canonical badge labels used in renderDatasetFreshness().
    CANONICAL_LABELS = {"Fresh", "Stale", "Failed", "Running", "Unknown"}

    def test_fresh_label_appears_in_render_function(self):
        # Extract just the renderPageDatasetFreshness function block.
        match = re.search(
            r"function renderPageDatasetFreshness\(.+?(?=\nfunction |\nconst |\nasync function |\Z)",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        assert match, "renderPageDatasetFreshness function not found for label inspection"
        fn_text = match.group(0)
        assert "Fresh" in fn_text, "renderPageDatasetFreshness must use 'Fresh' label"
        assert "Stale" in fn_text, "renderPageDatasetFreshness must use 'Stale' label"
        assert "Failed" in fn_text, "renderPageDatasetFreshness must use 'Failed' label"
        assert "Running" in fn_text, "renderPageDatasetFreshness must use 'Running' label"
        assert "Unknown" in fn_text, "renderPageDatasetFreshness must use 'Unknown' label"

    def test_no_ok_label_in_page_strips(self):
        """Page strips must not use 'ok' as a status label (use 'Fresh' instead)."""
        match = re.search(
            r"function renderPageDatasetFreshness\(.+?(?=\nfunction |\nconst |\nasync function |\Z)",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        if not match:
            return  # covered by previous test
        fn_text = match.group(0)
        # 'ok' should not appear as a status label string
        assert "· ok" not in fn_text.lower(), (
            "renderPageDatasetFreshness must not use 'ok' as a status label; use 'Fresh'"
        )


# ---------------------------------------------------------------------------
# Tests: Unknown renders as "unverified", not "failed"
# ---------------------------------------------------------------------------

class TestUnknownWording:
    """Unknown freshness state must use 'unverified' wording, not imply failure."""

    def test_unknown_rendered_as_unverified(self):
        match = re.search(
            r"function renderPageDatasetFreshness\(.+?(?=\nfunction |\nconst |\nasync function |\Z)",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        assert match, "renderPageDatasetFreshness not found"
        fn_text = match.group(0)
        assert "unverified" in fn_text.lower(), (
            "renderPageDatasetFreshness must use 'unverified' wording for Unknown state"
        )


# ---------------------------------------------------------------------------
# Tests: No unsafe action wording in freshness copy
# ---------------------------------------------------------------------------

class TestNoUnsafeActionWording:
    """Freshness display copy must not contain any external-action instructions."""

    _FORBIDDEN_PHRASES = (
        "push negative",
        "apply negative",
        "pause campaign",
        "block term",
        "send to google ads",
        "send to hubspot",
        "upload conversion",
        "change bid",
        "change budget",
        "auto retry",
        "auto rerun",
        "auto-fix",
    )

    def _get_freshness_function_text(self) -> str:
        match = re.search(
            r"function renderPageDatasetFreshness\(.+?(?=\nfunction |\nconst |\nasync function |\Z)",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        return match.group(0).lower() if match else ""

    def test_no_forbidden_phrases_in_freshness_renderer(self):
        fn_text = self._get_freshness_function_text()
        if not fn_text:
            return  # covered by existence test
        for phrase in self._FORBIDDEN_PHRASES:
            assert phrase not in fn_text, (
                f"Forbidden phrase found in renderPageDatasetFreshness: {phrase!r}"
            )

    def test_no_forbidden_phrases_in_dataset_map(self):
        map_match = re.search(
            r"const\s+PAGE_DATASET_MAP\s*=\s*\{[^}]+\}",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        if not map_match:
            return
        block = map_match.group(0).lower()
        for phrase in self._FORBIDDEN_PHRASES:
            assert phrase not in block, (
                f"Forbidden phrase found in PAGE_DATASET_MAP block: {phrase!r}"
            )


# ---------------------------------------------------------------------------
# Tests: API endpoint includes dataset_key field
# ---------------------------------------------------------------------------

class TestApiDatasetKey:
    """The /api/datasets/freshness endpoint must include dataset_key in rows."""

    _SERVER_PY = Path(__file__).resolve().parents[1] / "api" / "server.py"
    _SERVER_TEXT = _SERVER_PY.read_text(encoding="utf-8")

    def test_dataset_key_field_present_in_server(self):
        assert '"dataset_key"' in self._SERVER_TEXT, (
            "api/server.py must include 'dataset_key' field in /api/datasets/freshness response"
        )

    def test_dataset_key_is_composite_of_source_and_dataset(self):
        # The value must be constructed by joining source and dataset with '/'.
        # Match the literal f-string pattern used in the server: f"{...source...}/{...dataset...}"
        composite_pattern = re.compile(
            r'"dataset_key"\s*:\s*f["\'][^"\']*source[^"\']*\/[^"\']*dataset',
            re.DOTALL,
        )
        assert composite_pattern.search(self._SERVER_TEXT), (
            "api/server.py must construct dataset_key as an f-string combining "
            "source and dataset separated by '/' (e.g. f\"{source}/{dataset}\")"
        )


# ---------------------------------------------------------------------------
# Tests: DERIVED_DATASET_PAGES set exists
# ---------------------------------------------------------------------------

class TestDerivedDatasetPagesSet:
    """DERIVED_DATASET_PAGES must exist and include ngrams and waste."""

    def test_derived_set_declared(self):
        assert "DERIVED_DATASET_PAGES" in _APP_JS_TEXT, (
            "DERIVED_DATASET_PAGES not found in app.js"
        )

    def test_ngrams_in_derived_set(self):
        match = re.search(
            r"DERIVED_DATASET_PAGES\s*=\s*new Set\(\[([^\]]*)\]\)",
            _APP_JS_TEXT,
        )
        assert match, "DERIVED_DATASET_PAGES Set not found or not parseable"
        contents = match.group(1)
        assert "ngrams" in contents, "ngrams must be in DERIVED_DATASET_PAGES"

    def test_waste_in_derived_set(self):
        match = re.search(
            r"DERIVED_DATASET_PAGES\s*=\s*new Set\(\[([^\]]*)\]\)",
            _APP_JS_TEXT,
        )
        assert match, "DERIVED_DATASET_PAGES Set not found or not parseable"
        contents = match.group(1)
        assert "waste" in contents, "waste must be in DERIVED_DATASET_PAGES"


# ---------------------------------------------------------------------------
# Tests: loadDatasetFreshness populates _datasetFreshnessByKey
# ---------------------------------------------------------------------------

class TestCachePopulation:
    """loadDatasetFreshness() must populate _datasetFreshnessByKey."""

    def test_cache_variable_declared(self):
        assert "_datasetFreshnessByKey" in _APP_JS_TEXT, (
            "_datasetFreshnessByKey not found in app.js"
        )

    def test_cache_populated_in_load_function(self):
        # Find loadDatasetFreshness function body
        match = re.search(
            r"async function loadDatasetFreshness\(\).+?(?=\nasync function |\nfunction |\Z)",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        assert match, "loadDatasetFreshness function not found"
        fn_text = match.group(0)
        assert "_datasetFreshnessByKey" in fn_text, (
            "loadDatasetFreshness must populate _datasetFreshnessByKey"
        )

    def test_page_strip_rerendered_after_cache_loads(self):
        """After populating cache, loadDatasetFreshness must re-render the visible page strip."""
        match = re.search(
            r"async function loadDatasetFreshness\(\).+?(?=\nasync function |\nfunction |\Z)",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        assert match, "loadDatasetFreshness function not found"
        fn_text = match.group(0)
        # Must call renderPageDatasetFreshness after the cache is populated
        assert "renderPageDatasetFreshness(" in fn_text, (
            "loadDatasetFreshness must call renderPageDatasetFreshness() after populating "
            "the cache so fast-navigated pages upgrade from run-meta to dataset-level strips"
        )


# ---------------------------------------------------------------------------
# Tests: Config-driven stale threshold
# ---------------------------------------------------------------------------

class TestConfigDrivenThreshold:
    """datasetDisplayStatus must use _staleAfterDays, not a hardcoded constant."""

    def test_no_hardcoded_dataset_stale_threshold_constant(self):
        assert "DATASET_STALE_THRESHOLD_DAYS" not in _APP_JS_TEXT, (
            "DATASET_STALE_THRESHOLD_DAYS should not exist; use _staleAfterDays instead"
        )

    def test_dataset_display_status_uses_stale_after_days(self):
        match = re.search(
            r"function datasetDisplayStatus\(.+?(?=\nfunction |\nconst |\nasync function |\Z)",
            _APP_JS_TEXT,
            re.DOTALL,
        )
        assert match, "datasetDisplayStatus function not found"
        fn_text = match.group(0)
        assert "_staleAfterDays" in fn_text, (
            "datasetDisplayStatus must use _staleAfterDays (config-driven), not a hardcoded constant"
        )
