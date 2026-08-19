"""
tests/test_platform_evidence_google_ads_api_cutover.py

PR-ADS-105 — Platform Evidence & Admin Source Label Cutover.

After PR-ADS-104 cut the daily/weekly/monthly scheduled Google Ads pulls from
Windsor.ai to the direct Google Ads API (sync batches now use
``source="google_ads_api"``), this PR updates the visible app layer + backend
freshness/source mapping so the system truth is consistent:

  * Google Ads API  = active platform-evidence source.
  * Windsor         = legacy/deprecated fallback only, NOT the current source.

These tests assert the user-facing cutover without changing any scheduler,
connector, or DB-writer behaviour, and without introducing Google Ads writes.

Run with:
    python -m pytest tests/test_platform_evidence_google_ads_api_cutover.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SERVER_PY = (ROOT / "api" / "server.py").read_text(encoding="utf-8")


# ── Platform Evidence route keys (must remain stable) ───────────────────────

PLATFORM_EVIDENCE_ROUTES = ["campaigns", "search-terms", "keywords", "geo"]

# Google Ads API datasets that back the Platform Evidence pages.
GOOGLE_ADS_DATASETS = ["campaigns", "search_terms", "keywords", "geo"]


# ── JS extraction helpers (no JS execution) ─────────────────────────────────

def extract_js_object_block(js_text: str, var_name: str) -> str | None:
    """Return the brace-matched ``const <var_name> = { ... }`` block."""
    marker = f"const {var_name} = {{"
    start = js_text.find(marker)
    if start == -1:
        return None
    brace_start = start + len(marker) - 1
    depth = 0
    for i, ch in enumerate(js_text[brace_start:]):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return js_text[brace_start : brace_start + i + 1]
    return None


def get_route_entry_slice(block: str, route: str) -> str | None:
    """Return the value sub-object ``{...}`` for ``route`` inside ``block``."""
    for pat in (f'"{route}":', f"'{route}':", f"{route}:"):
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


# ───────────────────────────────────────────────────────────────────────────
# 1. Platform Evidence routes remain stable (route keys must not change)
# ───────────────────────────────────────────────────────────────────────────

class TestRouteStability:
    def test_pages_list_contains_platform_evidence_routes(self):
        match = re.search(r"const\s+PAGES\s*=\s*\[(.*?)\]", APP_JS, re.DOTALL)
        assert match, "PAGES constant not found in app.js"
        pages_block = match.group(1)
        for route in PLATFORM_EVIDENCE_ROUTES:
            assert f'"{route}"' in pages_block, (
                f"Platform Evidence route key '{route}' must remain in PAGES"
            )

    def test_route_keys_unchanged_not_renamed(self):
        # The cutover must not rename route keys to e.g. google-ads-campaigns.
        for forbidden in (
            "google-ads-campaigns",
            "google-ads-search-terms",
            "google_ads_api/page",
        ):
            assert forbidden not in APP_JS, (
                f"Route keys must remain stable; found renamed key '{forbidden}'"
            )


# ───────────────────────────────────────────────────────────────────────────
# 2. + 3. Platform Evidence page guides mention Google Ads API, not Windsor as
#         the active source.
# ───────────────────────────────────────────────────────────────────────────

class TestPlatformEvidenceCopy:
    def _entries(self, var_name):
        block = extract_js_object_block(APP_JS, var_name)
        assert block is not None, f"Could not extract {var_name}"
        out = {}
        for route in PLATFORM_EVIDENCE_ROUTES:
            slice_ = get_route_entry_slice(block, route)
            assert slice_ is not None, f"{var_name} missing entry for '{route}'"
            out[route] = slice_
        return out

    def test_page_explanations_mention_google_ads_api(self):
        entries = self._entries("PAGE_EXPLANATIONS")
        for route, text in entries.items():
            assert "Google Ads API" in text, (
                f"PAGE_EXPLANATIONS['{route}'] must mention Google Ads API "
                "as the active source"
            )

    def test_page_help_content_mentions_google_ads_api(self):
        entries = self._entries("PAGE_HELP_CONTENT")
        for route, text in entries.items():
            assert "Google Ads API" in text, (
                f"PAGE_HELP_CONTENT['{route}'] must mention Google Ads API"
            )

    def test_page_explanations_do_not_blame_windsor_as_active_source(self):
        entries = self._entries("PAGE_EXPLANATIONS")
        for route, text in entries.items():
            assert "Windsor" not in text, (
                f"PAGE_EXPLANATIONS['{route}'] must not reference Windsor as "
                "the active platform source"
            )

    def test_page_help_content_does_not_blame_windsor_as_active_source(self):
        entries = self._entries("PAGE_HELP_CONTENT")
        for route, text in entries.items():
            assert "Windsor" not in text, (
                f"PAGE_HELP_CONTENT['{route}'] must not reference Windsor as "
                "the active platform source"
            )


# ───────────────────────────────────────────────────────────────────────────
# 4. System Status labels google_ads_api as "Google Ads API"
# ───────────────────────────────────────────────────────────────────────────

class TestSystemStatusLabels:
    def test_source_definitions_use_google_ads_api(self):
        from services.system_status_service import SOURCE_DEFINITIONS

        assert "google_ads_api" in SOURCE_DEFINITIONS, (
            "SOURCE_DEFINITIONS must expose the google_ads_api source"
        )
        assert SOURCE_DEFINITIONS["google_ads_api"]["label"] == "Google Ads API"
        datasets = set(SOURCE_DEFINITIONS["google_ads_api"]["datasets"])
        assert set(GOOGLE_ADS_DATASETS).issubset(datasets), (
            "google_ads_api source must cover campaigns/search_terms/keywords/geo"
        )
        # Windsor must not be the active ad-platform source key.
        assert "windsor" not in SOURCE_DEFINITIONS, (
            "Windsor must not be an active source in SOURCE_DEFINITIONS"
        )

    def test_pipeline_dependencies_use_google_ads_api(self):
        from services.system_status_service import PIPELINE_DEPENDENCIES

        for ds in GOOGLE_ADS_DATASETS:
            assert PIPELINE_DEPENDENCIES[ds]["source"] == "google_ads_api", (
                f"PIPELINE_DEPENDENCIES['{ds}'] must be sourced from google_ads_api"
            )


# ───────────────────────────────────────────────────────────────────────────
# 5. Data Runs source-label mapping
# ───────────────────────────────────────────────────────────────────────────

class TestDataRunsSourceLabels:
    def _source_display_block(self):
        idx = APP_JS.find("function _sourceDisplayName")
        assert idx != -1, "_sourceDisplayName not found in app.js"
        return APP_JS[idx : idx + 600]

    def test_source_display_name_maps_google_ads_api(self):
        block = self._source_display_block()
        assert 'google_ads_api: "Google Ads API"' in block, (
            "Data Runs must label google_ads_api as 'Google Ads API'"
        )

    def test_source_display_name_maps_supporting_sources(self):
        block = self._source_display_block()
        assert 'hubspot: "HubSpot"' in block
        assert 'gclid: "GCLID Attribution"' in block

    def test_source_display_name_keeps_windsor_as_legacy(self):
        block = self._source_display_block()
        assert 'windsor: "Windsor legacy"' in block, (
            "Windsor must be labelled as legacy, not the active platform source"
        )


# ───────────────────────────────────────────────────────────────────────────
# 6. PAGE_DATASET_MAP / backend maps point Platform Evidence pages at
#    google_ads_api datasets.
# ───────────────────────────────────────────────────────────────────────────

class TestDatasetSourceMapping:
    def test_page_dataset_map_uses_google_ads_api_keys(self):
        block = extract_js_object_block(APP_JS, "PAGE_DATASET_MAP")
        assert block is not None, "PAGE_DATASET_MAP not found"
        # PR-ADS-146: the Keyword Evidence page depends on the DURABLE keyword_facts
        # dataset, not the legacy keywords snapshot.
        page_datasets = ["campaigns", "search_terms", "keyword_facts", "geo"]
        for ds in page_datasets:
            assert f"google_ads_api/{ds}" in block, (
                f"PAGE_DATASET_MAP must map to google_ads_api/{ds}"
            )
        # The legacy keywords snapshot must NOT be a page dependency (§6).
        assert "google_ads_api/keywords" not in block, (
            "Keyword Evidence must depend only on keyword_facts, not the legacy "
            "keywords snapshot"
        )
        # No platform-evidence page should still point at windsor/<dataset>.
        for ds in GOOGLE_ADS_DATASETS:
            assert f"windsor/{ds}" not in block, (
                f"PAGE_DATASET_MAP must not reference windsor/{ds} as active"
            )

    def test_backend_freshness_config_uses_google_ads_api(self):
        from services.freshness_service import DATASET_FRESHNESS_CONFIG

        for ds in GOOGLE_ADS_DATASETS:
            assert DATASET_FRESHNESS_CONFIG[ds]["source"] == "google_ads_api", (
                f"DATASET_FRESHNESS_CONFIG['{ds}'] must use source=google_ads_api"
            )

    def test_known_datasets_in_server_use_google_ads_api(self):
        # The freshness endpoint's placeholder pairs must name the active source
        # so it reads PR-ADS-104's google_ads_api sync rows, never Windsor's.
        #
        # PR-ADS-153F: this asserted on a hand-listed `_KNOWN_DATASETS` literal in
        # api/server.py. That list was a fourth copy of the dataset registry and
        # had already drifted (it still named datasets no writer stamps), so it
        # is now DERIVED from DATASET_FRESHNESS_CONFIG. The guarantee is
        # unchanged and is asserted on the real pairs rather than on source text.
        from api.server import _known_dataset_pairs

        pairs = _known_dataset_pairs()
        for ds in GOOGLE_ADS_DATASETS:
            assert ("google_ads_api", ds) in pairs, (
                f"known datasets must include (google_ads_api, {ds})"
            )
            assert ("windsor", ds) not in pairs, (
                f"known datasets must not list windsor/{ds} as active"
            )


# ───────────────────────────────────────────────────────────────────────────
# 7. Search Terms tab model intact — ngrams alias resolves to search-terms /
#    Patterns tab.
# ───────────────────────────────────────────────────────────────────────────

class TestSearchTermsTabModel:
    def test_ngrams_hash_alias_resolves_to_search_terms(self):
        block = extract_js_object_block(APP_JS, "HASH_ROUTE_ALIASES")
        assert block is not None, "HASH_ROUTE_ALIASES not found"
        assert '"ngrams": "search-terms"' in block, (
            "ngrams hash alias must resolve to search-terms"
        )
        assert '"countries": "geo"' in block, (
            "countries hash alias must resolve to geo"
        )

    def test_ngrams_navigation_activates_patterns_tab(self):
        # The navigate('ngrams') branch must redirect to search-terms and click
        # the Patterns tab — the merged tab model must not be split again.
        assert 'navigate("search-terms"' in APP_JS
        assert "tab-btn-patterns" in APP_JS, (
            "ngrams alias must activate the Patterns tab"
        )

    def test_patterns_copy_references_google_ads_api_evidence(self):
        block = extract_js_object_block(APP_JS, "PAGE_EXPLANATIONS")
        ngrams_entry = get_route_entry_slice(block, "ngrams")
        assert ngrams_entry is not None
        assert "Google Ads API search-term evidence" in ngrams_entry, (
            "Patterns copy must say patterns are calculated from Google Ads API "
            "search-term evidence"
        )

    def test_search_term_universe_page_not_resurrected(self):
        # The old zombie split (Search Term Universe vs Search Pattern pages)
        # must not reappear as separate top-level routes.
        match = re.search(r"const\s+PAGES\s*=\s*\[(.*?)\]", APP_JS, re.DOTALL)
        pages_block = match.group(1)
        assert '"search-pattern"' not in pages_block
        assert '"search-term-universe"' not in pages_block


# ───────────────────────────────────────────────────────────────────────────
# 8. Windsor may exist only as legacy/deprecated wording — never active source.
# ───────────────────────────────────────────────────────────────────────────

class TestWindsorIsLegacyOnly:
    def test_platform_evidence_empty_states_do_not_blame_windsor(self):
        # Empty-state subtexts for legacy Platform Evidence pages must point at
        # the Google Ads API, not Windsor. (Keyword Evidence, PR-ADS-146, is now
        # a full evidence page like Search Terms — its empty state is rendered in
        # the shell and its Google-Ads-API source is asserted via help content.)
        for marker in ("geo-empty-subtext",):
            idx = APP_JS.find(marker)
            assert idx != -1, f"empty-state marker '{marker}' not found"
            snippet = APP_JS[idx : idx + 240]
            assert "Windsor" not in snippet, (
                f"Empty state '{marker}' must not blame Windsor"
            )
            assert "Google Ads API" in snippet, (
                f"Empty state '{marker}' must mention Google Ads API"
            )

    def test_index_html_platform_evidence_copy_uses_google_ads_api(self):
        # Geo empty state in index.html. (Keyword Evidence, PR-ADS-146, renders
        # its header/subtitle/empty state in the JS shell, not index.html; its
        # Google-Ads-API source is asserted via PAGE_HELP_CONTENT above.)
        assert "Google Ads API country performance" in INDEX_HTML


# ───────────────────────────────────────────────────────────────────────────
# 9. Non-goals — no scheduler / connector / DB-writer / Google Ads write changes
# ───────────────────────────────────────────────────────────────────────────

class TestNonGoalsRespected:
    def test_no_google_ads_mutate_calls_in_connectors(self):
        connectors_dir = ROOT / "connectors"
        offenders = []
        for path in connectors_dir.glob("google_ads*.py"):
            text = path.read_text(encoding="utf-8")
            # Guard against any mutate / write surface being introduced.
            for needle in (".mutate(", "MutateOperation", "mutate_campaigns",
                           "CampaignOperation", "create_or_update"):
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert not offenders, (
            f"No Google Ads mutate/write calls allowed: {offenders}"
        )

    def test_freshness_service_has_no_write_surface(self):
        text = (ROOT / "services" / "freshness_service.py").read_text(encoding="utf-8")
        # Pure read-only computation module — must not execute SQL writes.
        for needle in ("INSERT ", "UPDATE ", "DELETE ", "execute("):
            assert needle not in text, (
                f"freshness_service.py must remain read-only (found {needle!r})"
            )
