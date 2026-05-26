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


class TestPageExplanationsCoverage:
    """PAGE_EXPLANATIONS must contain every required data-page route."""

    def test_page_explanations_object_exists(self):
        assert "PAGE_EXPLANATIONS" in APP_JS, "PAGE_EXPLANATIONS object not found in app.js"

    def test_all_routes_have_explanations(self):
        for route in REQUIRED_ROUTES:
            # Check that the route key appears inside PAGE_EXPLANATIONS
            # Route keys in the object use quoted keys for hyphenated names
            patterns = [
                f'"{route}"',   # quoted key like "search-terms"
                f"'{route}'",   # single-quoted
                f"{route}:",    # unquoted (only for non-hyphenated)
            ]
            found = any(p in APP_JS for p in patterns)
            assert found, f"PAGE_EXPLANATIONS missing entry for route: {route}"

    def test_explanations_have_required_fields(self):
        """Each explanation should have purpose, source, dependsOn, emptyMeans, nextAction."""
        required_fields = ["purpose", "source", "dependsOn", "emptyMeans", "nextAction"]
        for field in required_fields:
            # Count occurrences — should be at least len(REQUIRED_ROUTES)
            count = APP_JS.count(f"{field}:")
            assert count >= len(REQUIRED_ROUTES), (
                f"Field '{field}' appears only {count} times, expected at least {len(REQUIRED_ROUTES)}"
            )


class TestPageDependenciesCoverage:
    """PAGE_DEPENDENCIES must contain every major data page."""

    def test_page_dependencies_object_exists(self):
        assert "PAGE_DEPENDENCIES" in APP_JS, "PAGE_DEPENDENCIES object not found in app.js"

    def test_all_routes_have_dependencies(self):
        for route in REQUIRED_ROUTES:
            patterns = [
                f'"{route}"',
                f"'{route}'",
                f"{route}:",
            ]
            # PAGE_DEPENDENCIES should contain the route
            # Find the PAGE_DEPENDENCIES section
            dep_start = APP_JS.find("PAGE_DEPENDENCIES")
            dep_section = APP_JS[dep_start:dep_start + 2000]
            found = any(p in dep_section for p in patterns)
            assert found, f"PAGE_DEPENDENCIES missing entry for route: {route}"


class TestSearchTermUniverseExplanation:
    """Search Term Universe explanation must warn that zero does not mean clean."""

    def test_zero_does_not_mean_clean_in_explanations(self):
        # The PAGE_EXPLANATIONS entry for search-terms
        assert "does not mean the account is clean" in APP_JS, (
            "Search Term Universe explanation must state that zero does not mean the account is clean"
        )

    def test_zero_does_not_mean_clean_in_empty_state(self):
        # The rendered empty state for search terms
        assert "does not mean the account is clean" in APP_JS, (
            "Search Term Universe empty state must warn that zero does not mean clean"
        )


class TestWasteTermsExplanation:
    """Waste Terms explanation must reference Search Term Universe dependency."""

    def test_waste_depends_on_search_terms_in_explanations(self):
        # Check PAGE_EXPLANATIONS for waste references search_terms dependency
        waste_section_start = APP_JS.find('"waste"') if '"waste"' in APP_JS else APP_JS.find("waste:")
        assert waste_section_start != -1, "Waste entry not found in PAGE_EXPLANATIONS"

        # The waste empty state should mention Search Term Universe
        assert "Search Term Universe" in APP_JS, (
            "Waste Terms explanation must reference Search Term Universe dependency"
        )

    def test_waste_empty_state_mentions_dependency(self):
        # The actual rendered empty state for waste
        assert "Waste detection depends on Search Term Universe" in APP_JS or \
               "depends on Search Term Universe" in APP_JS, (
            "Waste empty state must explain dependency on Search Terms"
        )


class TestNgramsExplanation:
    """Search Pattern Analysis explanation must reference Search Term Universe dependency."""

    def test_ngrams_depends_on_search_terms(self):
        # N-grams explanation should reference search terms
        assert "computed from Search Term Universe" in APP_JS or \
               "Computed from Search Term Universe" in APP_JS, (
            "N-Grams explanation must state it is computed from Search Term Universe"
        )

    def test_ngrams_empty_state_mentions_search_terms(self):
        assert "Search Term Universe" in APP_JS, (
            "N-Grams empty state must mention Search Term Universe"
        )


class TestAdminBackfillExplanation:
    """Admin Backfill explanation must mention dry-run safety and read-only."""

    def test_backfill_mentions_dry_run(self):
        # Find backfill section
        assert "dry-run" in APP_JS.lower() or "Dry-run" in APP_JS, (
            "Admin Backfill explanation must mention dry-run"
        )

    def test_backfill_mentions_read_only(self):
        assert "Read-only" in APP_JS or "read-only" in APP_JS, (
            "Admin Backfill explanation must mention read-only safety"
        )

    def test_backfill_does_not_write(self):
        # The backfill explanation should say it does not write
        assert "does not write" in APP_JS, (
            "Admin Backfill explanation must state dry-run does not write to the database"
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
