"""
PR-ADS-070 — Empty State & Page Explanation Upgrade
Tests that PAGE_EXPLANATIONS and PAGE_DEPENDENCIES exist for every major data-page route,
and that critical empty-state messages meet acceptance criteria.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

# All required data-page route keys
REQUIRED_ROUTES = [
    "dashboard",
    "action-queue",
    "reports",
    "campaigns",
    "waste",
    "search-terms",
    "ngrams",
    "geo",
    "keywords",
    "leads",
    "deals",
    "gclid-attribution",
    "opportunities",
    "scheduler",
    "health",
    "backfill",
    "historical-intelligence",
]


# ── Extraction helpers ────────────────────────────────────────────────────────

def extract_js_object_block(js_text, var_name):
    """
    Extract the full brace-matched content of a JS object variable assignment:
        const <var_name> = { ... };
    Returns the entire {...} string, or None if the variable is not found.
    """
    marker = f"const {var_name} = {{"
    start = js_text.find(marker)
    if start == -1:
        return None
    brace_start = start + len(marker) - 1  # position of the opening '{'
    depth = 0
    for i, ch in enumerate(js_text[brace_start:]):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return js_text[brace_start : brace_start + i + 1]
    return None


def get_route_entry_slice(block, route):
    """
    Find and return the value sub-object ({...}) for a route key inside an
    extracted JS object block.  Returns the {...} string or None.
    """
    patterns = [f'"{route}":', f"'{route}':", f"{route}:"]
    for pat in patterns:
        idx = block.find(pat)
        if idx == -1:
            continue
        brace_start = block.find("{", idx + len(pat))
        if brace_start == -1:
            continue
        depth = 0
        for i, ch in enumerate(block[brace_start:]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return block[brace_start : brace_start + i + 1]
    return None


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPageExplanationsCoverage:
    """PAGE_EXPLANATIONS must contain every required data-page route with all required fields."""

    def test_page_explanations_object_exists(self):
        assert "PAGE_EXPLANATIONS" in APP_JS, "PAGE_EXPLANATIONS object not found in app.js"

    def test_page_explanations_block_extractable(self):
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block from app.js"

    def test_all_routes_have_explanations(self):
        """Every required route must appear as a key inside the PAGE_EXPLANATIONS block."""
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block"
        for route in REQUIRED_ROUTES:
            entry = get_route_entry_slice(block, route)
            assert entry is not None, (
                f"PAGE_EXPLANATIONS missing entry for route: '{route}'"
            )

    def test_explanations_have_required_fields(self):
        """For each route, verify all required fields exist inside that route's entry."""
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block"
        required_fields = ["purpose", "source", "dependsOn", "emptyMeans", "nextAction"]
        for route in REQUIRED_ROUTES:
            entry = get_route_entry_slice(block, route)
            assert entry is not None, f"No entry found for route '{route}'"
            for field in required_fields:
                assert f"{field}:" in entry, (
                    f"Route '{route}' is missing required field '{field}' in PAGE_EXPLANATIONS"
                )


class TestPageDependenciesCoverage:
    """PAGE_DEPENDENCIES must contain every major data page with source-qualified dataset keys."""

    def test_page_dependencies_object_exists(self):
        assert "PAGE_DEPENDENCIES" in APP_JS, "PAGE_DEPENDENCIES object not found in app.js"

    def test_page_dependencies_block_extractable(self):
        block = extract_js_object_block(APP_JS, "PAGE_DEPENDENCIES")
        assert block is not None, "Could not extract PAGE_DEPENDENCIES block from app.js"

    def test_all_routes_have_dependencies(self):
        """Every required route must appear as a key inside the PAGE_DEPENDENCIES block."""
        block = extract_js_object_block(APP_JS, "PAGE_DEPENDENCIES")
        assert block is not None, "Could not extract PAGE_DEPENDENCIES block"
        for route in REQUIRED_ROUTES:
            patterns = [f'"{route}":', f"'{route}':", f"{route}:"]
            found = any(pat in block for pat in patterns)
            assert found, f"PAGE_DEPENDENCIES missing entry for route: '{route}'"

    def test_dependencies_use_source_qualified_keys(self):
        """PAGE_DEPENDENCIES must use source-qualified keys (e.g. google_ads_api/search_terms)."""
        block = extract_js_object_block(APP_JS, "PAGE_DEPENDENCIES")
        assert block is not None, "Could not extract PAGE_DEPENDENCIES block"
        # At least the core data pages should reference source-qualified dataset keys
        qualified_keys = [
            "google_ads_api/search_terms",
            "google_ads_api/campaigns",
            "hubspot/contacts",
        ]
        for key in qualified_keys:
            assert f'"{key}"' in block or f"'{key}'" in block, (
                f"PAGE_DEPENDENCIES should use source-qualified key '{key}'"
            )


class TestBuildEmptyStateIntegration:
    """buildEmptyState() must be wired into every critical page renderer."""

    def _get_function_slice(self, fn_signature, window=3000):
        """Return a slice of app.js starting at the given function signature.
        'window' is the number of characters to include after the signature start.
        """
        idx = APP_JS.find(fn_signature)
        assert idx != -1, f"Function not found: {fn_signature}"
        return APP_JS[idx : idx + window]

    def test_build_empty_state_exists(self):
        assert "function buildEmptyState" in APP_JS

    def test_get_page_canonical_status_exists(self):
        assert "function getPageCanonicalStatus" in APP_JS

    def test_build_empty_state_called_in_search_terms(self):
        body = self._get_function_slice("function renderSearchTermsTable(")
        assert "buildEmptyState(" in body, (
            "renderSearchTermsTable must call buildEmptyState() for its empty state"
        )

    def test_build_empty_state_called_in_waste(self):
        body = self._get_function_slice("function renderWasteTable(")
        assert "buildEmptyState(" in body, (
            "renderWasteTable must call buildEmptyState() for its empty state"
        )

    def test_build_empty_state_called_in_ngrams(self):
        body = self._get_function_slice("function renderNgramsTable(")
        assert "buildEmptyState(" in body, (
            "renderNgramsTable must call buildEmptyState() for its empty state"
        )

    def test_build_empty_state_called_in_campaigns(self):
        body = self._get_function_slice("async function loadCampaigns(")
        assert "buildEmptyState(" in body, (
            "loadCampaigns must call buildEmptyState() for its empty state"
        )

    def test_build_empty_state_called_in_leads(self):
        body = self._get_function_slice("async function loadLeads(")
        assert "buildEmptyState(" in body, (
            "loadLeads must call buildEmptyState() for its empty state"
        )

    def test_build_empty_state_called_in_backfill(self):
        body = self._get_function_slice("function renderBackfillSummary(", window=2000)
        assert "buildEmptyState(" in body, (
            "renderBackfillSummary must call buildEmptyState() for its no-run state"
        )


class TestSearchTermUniverseExplanation:
    """Search Term Universe explanation must warn that zero does not mean clean."""

    def test_zero_does_not_mean_clean_in_explanations(self):
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block"
        st_entry = get_route_entry_slice(block, "search-terms")
        assert st_entry is not None, "No search-terms entry in PAGE_EXPLANATIONS"
        assert "does not mean the account is clean" in st_entry, (
            "Search Term Universe PAGE_EXPLANATIONS entry must state zero does not mean clean"
        )

    def test_zero_does_not_mean_clean_in_empty_state(self):
        # buildEmptyState() for search-terms uses emptyMeans from PAGE_EXPLANATIONS
        assert "does not mean the account is clean" in APP_JS, (
            "Search Term Universe empty state must warn that zero does not mean clean"
        )


class TestWasteTermsExplanation:
    """Waste Terms explanation must reference Search Term Universe dependency."""

    def test_waste_depends_on_search_terms_in_explanations(self):
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block"
        waste_entry = get_route_entry_slice(block, "waste")
        assert waste_entry is not None, "No waste entry in PAGE_EXPLANATIONS"
        assert "Search Term Universe" in waste_entry, (
            "Waste PAGE_EXPLANATIONS entry must reference Search Term Universe"
        )

    def test_waste_empty_state_mentions_dependency(self):
        # PAGE_EXPLANATIONS["waste"].emptyMeans must include the dependency
        assert "depends on Search Term Universe" in APP_JS, (
            "Waste empty state must explain dependency on Search Term Universe"
        )


class TestNgramsExplanation:
    """Search Pattern Analysis explanation must reference Search Term Universe dependency."""

    def test_ngrams_depends_on_search_terms(self):
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block"
        ngrams_entry = get_route_entry_slice(block, "ngrams")
        assert ngrams_entry is not None, "No ngrams entry in PAGE_EXPLANATIONS"
        assert "Search Term Universe" in ngrams_entry, (
            "N-Grams PAGE_EXPLANATIONS entry must reference Search Term Universe"
        )

    def test_ngrams_empty_state_mentions_search_terms(self):
        # PR-ADS-105: Patterns are now described as calculated from Google Ads
        # API search-term evidence, while still depending on Search Term Universe.
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        ngrams_entry = get_route_entry_slice(block, "ngrams")
        assert ngrams_entry is not None, "No ngrams entry in PAGE_EXPLANATIONS"
        assert "Search Term Universe" in ngrams_entry, (
            "N-Grams explanation must reference Search Term Universe"
        )
        assert "Google Ads API search-term evidence" in ngrams_entry, (
            "N-Grams/Patterns explanation must state patterns are calculated "
            "from Google Ads API search-term evidence"
        )


class TestAdminBackfillExplanation:
    """Admin Backfill explanation must mention dry-run safety and read-only."""

    def test_backfill_mentions_dry_run(self):
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block"
        backfill_entry = get_route_entry_slice(block, "backfill")
        assert backfill_entry is not None, "No backfill entry in PAGE_EXPLANATIONS"
        assert "dry-run" in backfill_entry.lower() or "dry run" in backfill_entry.lower(), (
            "Admin Backfill PAGE_EXPLANATIONS entry must mention dry-run"
        )

    def test_backfill_mentions_read_only(self):
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block"
        backfill_entry = get_route_entry_slice(block, "backfill")
        assert backfill_entry is not None, "No backfill entry in PAGE_EXPLANATIONS"
        assert "read-only" in backfill_entry.lower() or "Read-only" in backfill_entry, (
            "Admin Backfill PAGE_EXPLANATIONS entry must mention read-only safety"
        )

    def test_backfill_does_not_write(self):
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        assert block is not None, "Could not extract PAGE_EXPLANATIONS block"
        backfill_entry = get_route_entry_slice(block, "backfill")
        assert backfill_entry is not None, "No backfill entry in PAGE_EXPLANATIONS"
        assert "does not write" in backfill_entry, (
            "Admin Backfill PAGE_EXPLANATIONS entry must state dry-run does not write"
        )


class TestNoGenericEmptyStates:
    """Critical pages must not rely only on generic 'No data found' copy."""

    def test_no_bare_no_data_found(self):
        # "No data found" without context should not appear for critical pages
        # We check that the exact generic phrase is not used as a sole empty state
        bare_pattern = re.compile(r'class="empty-state"[^>]*>\s*No data found\.?\s*<')
        matches = bare_pattern.findall(APP_JS)
        assert len(matches) == 0, (
            f"Found {len(matches)} generic 'No data found' empty states — "
            "all critical pages should have contextual empty states"
        )


class TestRenderHelpers:
    """Helper functions must exist."""

    def test_render_page_explanation_exists(self):
        assert "function renderPageExplanation" in APP_JS

    def test_build_empty_state_exists(self):
        assert "function buildEmptyState" in APP_JS

    def test_get_page_canonical_status_exists(self):
        assert "function getPageCanonicalStatus" in APP_JS


class TestPageExplanationContainers:
    """HTML must have page-explanation containers for key pages."""

    def setup_method(self):
        self.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_explanation_containers_exist(self):
        key_pages = [
            "dashboard", "action-queue", "reports", "campaigns",
            "waste", "search-terms", "ngrams", "geo", "keywords",
            "leads", "deals", "gclid-attribution", "opportunities",
            "scheduler", "health", "backfill", "historical-intelligence",
        ]
        for page in key_pages:
            assert f'id="page-explanation-{page}"' in self.html, (
                f"Missing page-explanation container for: {page}"
            )


class TestCSSStyles:
    """Required CSS classes must exist."""

    def setup_method(self):
        self.css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")

    def test_page_explanation_styles(self):
        assert ".page-explanation" in self.css
        assert ".page-explanation__grid" in self.css
        assert ".page-explanation__item" in self.css

    def test_context_chip_styles(self):
        assert ".page-context-chips" in self.css
        assert ".context-chip" in self.css
        assert ".context-chip--source" in self.css
        assert ".context-chip--readonly" in self.css

    def test_empty_state_severity_styles(self):
        assert ".empty-state--warning" in self.css
        assert ".empty-state--blocked" in self.css
        assert ".empty-state--error" in self.css
        assert ".empty-state--info" in self.css

    def test_empty_state_block_styles(self):
        assert ".empty-state__title" in self.css
        assert ".empty-state__body" in self.css
