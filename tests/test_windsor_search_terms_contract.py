"""
tests/test_windsor_search_terms_contract.py

Lock the Windsor MCP response parser and row normalizer so no future agent breaks it.
PR-ADS-063 — Windsor Search-Term Contract Audit, Parser Fix & Docs Sync.
"""

import json
from unittest.mock import patch

from connectors.windsor_pull import (
    _parse_windsor_mcp_response,
    _normalize_search_term_row,
    pull_search_terms,
    pull_search_terms_mcp_payload,
)


# ─── Test 1: MCP text response double parse ─────────────────────────────────

class TestMCPDoubleParse:
    """Confirmed MCP response shape: list with text key containing JSON string."""

    def test_parses_text_payload(self):
        raw = [
            {
                "text": json.dumps([
                    {
                        "search_term": "cargowise pricing",
                        "campaign": "global - competitors",
                        "ad_group": "customers/3059734490/adGroups/175677406221",
                        "impressions": 10,
                        "clicks": 2,
                        "spend": 7.5,
                        "conversions": 1,
                    }
                ])
            }
        ]

        rows = _parse_windsor_mcp_response(raw)

        assert len(rows) == 1
        assert rows[0]["search_term"] == "cargowise pricing"
        assert rows[0]["campaign"] == "global - competitors"
        assert rows[0]["impressions"] == 10

    def test_parses_multiple_rows(self):
        raw = [
            {
                "text": json.dumps([
                    {"search_term": "freight software", "campaign": "gulf"},
                    {"search_term": "logistics tms", "campaign": "europa"},
                ])
            }
        ]

        rows = _parse_windsor_mcp_response(raw)
        assert len(rows) == 2

    def test_handles_data_dict_inside_text(self):
        raw = [
            {
                "text": json.dumps({"data": [
                    {"search_term": "cargowise", "campaign": "global"}
                ]})
            }
        ]

        rows = _parse_windsor_mcp_response(raw)
        assert len(rows) == 1
        assert rows[0]["search_term"] == "cargowise"

    def test_empty_response_returns_empty(self):
        assert _parse_windsor_mcp_response([]) == []
        assert _parse_windsor_mcp_response(None) == []
        assert _parse_windsor_mcp_response("") == []

    def test_malformed_text_returns_empty(self):
        raw = [{"text": "not valid json {{{"}]
        rows = _parse_windsor_mcp_response(raw)
        assert rows == []


# ─── Test 2: ad_group ID extraction ─────────────────────────────────────────

class TestAdGroupIdExtraction:
    """ad_group may be a full Google Ads resource path."""

    def test_extracts_id_from_resource_path(self):
        row = {
            "search_term": "cargowise pricing",
            "campaign": "global - competitors",
            "ad_group": "customers/3059734490/adGroups/175677406221",
            "impressions": 10,
            "clicks": 2,
            "spend": 7.5,
            "conversions": 1,
        }

        normalized = _normalize_search_term_row(row)

        assert normalized["ad_group_id"] == "175677406221"
        assert normalized["ad_group"] == "customers/3059734490/adGroups/175677406221"

    def test_plain_ad_group_unchanged(self):
        row = {
            "search_term": "freight",
            "campaign": "gulf",
            "ad_group": "175677406221",
        }

        normalized = _normalize_search_term_row(row)
        assert normalized["ad_group_id"] == "175677406221"
        assert normalized["ad_group"] == "175677406221"

    def test_none_ad_group(self):
        row = {"search_term": "test", "campaign": "test"}

        normalized = _normalize_search_term_row(row)
        assert normalized["ad_group_id"] is None
        assert normalized["ad_group"] is None


# ─── Test 3: Wrong field names not silently accepted ─────────────────────────

class TestWrongFieldNamesRejected:
    """Do not map query or search_query to search_term automatically."""

    def test_row_with_query_has_no_search_term(self):
        row = {
            "query": "cargowise pricing",
            "campaign": "global",
            "ad_group": "some_group",
        }

        normalized = _normalize_search_term_row(row)
        # search_term should be None — we do NOT silently alias from 'query'
        assert normalized["search_term"] is None

    def test_row_with_search_query_has_no_search_term(self):
        row = {
            "search_query": "freight software",
            "campaign": "gulf",
        }

        normalized = _normalize_search_term_row(row)
        assert normalized["search_term"] is None

    def test_row_with_search_term_text_has_no_search_term(self):
        row = {
            "search_term_text": "logistics tms",
            "campaign": "europa",
        }

        normalized = _normalize_search_term_row(row)
        assert normalized["search_term"] is None


# ─── Test 4: Plain list response still supported ────────────────────────────

class TestAlreadyParsedList:
    """Parser handles already-parsed list of row dicts."""

    def test_passthrough_list_of_row_dicts(self):
        raw = [
            {"search_term": "freight software", "campaign": "gulf"},
            {"search_term": "logistics tms", "campaign": "europa"},
        ]

        rows = _parse_windsor_mcp_response(raw)
        assert len(rows) == 2
        assert rows[0]["search_term"] == "freight software"

    def test_full_pipeline_with_mcp_payload(self):
        raw = [
            {
                "text": json.dumps([
                    {
                        "search_term": "cargowise pricing",
                        "campaign": "global - competitors",
                        "ad_group": "customers/3059734490/adGroups/175677406221",
                        "impressions": 10,
                        "clicks": 2,
                        "spend": 7.5,
                        "conversions": 1,
                    }
                ])
            }
        ]

        rows = pull_search_terms_mcp_payload(raw)

        assert len(rows) == 1
        assert rows[0]["search_term"] == "cargowise pricing"
        assert rows[0]["ad_group_id"] == "175677406221"
        assert rows[0]["spend"] == 7.5


class TestSearchTermRestFallback:
    """REST last_60d is an attempted fallback and may downgrade to last_14d."""

    def test_last_60d_empty_falls_back_to_last_14d(self):
        with patch(
            "connectors.windsor_pull._request_with_retry",
            side_effect=[[], [{"search_term": "fallback term"}]],
        ) as mocked:
            rows = pull_search_terms(days_back=60)

        assert rows == [{"search_term": "fallback term"}]
        assert mocked.call_count == 2
        assert mocked.call_args_list[0].args[0]["date_preset"] == "last_60d"
        assert mocked.call_args_list[1].args[0]["date_preset"] == "last_14d"

    def test_non_60d_window_does_not_trigger_legacy_fallback(self):
        with patch(
            "connectors.windsor_pull._request_with_retry",
            return_value=[],
        ) as mocked:
            rows = pull_search_terms(days_back=7)

        assert rows == []
        assert mocked.call_count == 1
        assert mocked.call_args_list[0].args[0]["date_preset"] == "last_7d"
