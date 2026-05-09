"""
tests/test_rule_advisor_ngram_block.py

Tests for PR-ADS-066: Read-Only N-Gram Rollup in Weekly/Monthly Reports.

Tests:
  - N-Gram block renders when data is provided.
  - Missing N-Gram data (None) does not crash report generation.
  - Row-cap note appears when row_cap_applied is True.
  - Row-cap note uses the effective row_cap from ngram_data, not the constant.
  - Forbidden imperative words do not appear in the N-Gram block.
  - All three sub-tables render when data supports them.
  - Flagged-waste table is omitted when no flagged rows exist.
  - compute_ngram_findings returns None for empty/None input.
  - compute_ngram_findings normalizes Windsor field names.
  - compute_ngram_findings includes row_cap in the returned dict.
  - _normalize_rows_for_ngrams preserves legitimate zero values.

Run with:
  python -m pytest tests/test_rule_advisor_ngram_block.py -v
or:
  python tests/test_rule_advisor_ngram_block.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.rule_advisor import (
    _build_ngram_findings_block,
    _normalize_rows_for_ngrams,
    compute_ngram_findings,
    _NGRAM_ROW_CAP,
    _FORBIDDEN_NGRAM_PHRASES,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SAMPLE_FINDINGS = [
    {
        "ngram": "freight forwarding",
        "n": 2,
        "row_count": 15,
        "total_spend_usd": 250.50,
        "total_clicks": 40,
        "total_impressions": 800,
        "flagged_waste_rows": 5,
        "clean_rows": 8,
        "unanalyzed_rows": 2,
    },
    {
        "ngram": "logistics software",
        "n": 2,
        "row_count": 12,
        "total_spend_usd": 180.00,
        "total_clicks": 30,
        "total_impressions": 600,
        "flagged_waste_rows": 0,
        "clean_rows": 10,
        "unanalyzed_rows": 2,
    },
    {
        "ngram": "free shipping",
        "n": 2,
        "row_count": 8,
        "total_spend_usd": 95.00,
        "total_clicks": 20,
        "total_impressions": 400,
        "flagged_waste_rows": 8,
        "clean_rows": 0,
        "unanalyzed_rows": 0,
    },
]

_SAMPLE_NGRAM_DATA = {
    "findings": _SAMPLE_FINDINGS,
    "row_cap_applied": False,
    "total_rows_analyzed": 50,
}

_SAMPLE_NGRAM_DATA_CAPPED = {
    "findings": _SAMPLE_FINDINGS,
    "row_cap_applied": True,
    "total_rows_analyzed": _NGRAM_ROW_CAP,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ngram_block_renders_with_data() -> None:
    """N-Gram block renders the section header and key content when data is provided."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA)
    assert "N-Gram Findings" in block, "Block must contain section header"
    assert "freight forwarding" in block, "Block must contain a sample pattern"
    assert "Read-Only" in block, "Block must include read-only label"
    assert "human review" in block.lower(), "Block must mention human review"
    assert "candidate" in block.lower(), "Block must use 'candidate' language"


def test_ngram_block_missing_data_does_not_crash() -> None:
    """_build_ngram_findings_block(None) must not raise and renders a safe message."""
    block = _build_ngram_findings_block(None)
    assert isinstance(block, str), "Block must be a string even when data is None"
    assert "N-Gram findings were unavailable" in block, (
        "Missing-data message must appear when ngram_data is None"
    )
    assert "N-Gram Findings" in block, "Section header must still render"


def test_ngram_block_row_cap_note_appears() -> None:
    """Row-cap note must appear in the block when row_cap_applied is True."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA_CAPPED)
    assert "row-capped" in block.lower() or "row_cap" in block.lower() or "directional" in block, (
        "Row-cap note must appear when row_cap_applied=True"
    )
    assert "directional" in block.lower(), "Row-cap note must use 'directional' language"


def test_ngram_block_no_row_cap_note_when_not_capped() -> None:
    """Row-cap note must NOT appear when row_cap_applied is False."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA)
    assert "row-capped" not in block.lower(), (
        "Row-cap note must not appear when row_cap_applied=False"
    )


def test_ngram_block_forbidden_words_absent() -> None:
    """Forbidden imperative phrases must not appear anywhere in the N-Gram block."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA).lower()
    for phrase in _FORBIDDEN_NGRAM_PHRASES:
        assert phrase.lower() not in block, (
            f"Forbidden phrase '{phrase}' must not appear in the N-Gram block"
        )


def test_ngram_block_forbidden_words_absent_when_no_data() -> None:
    """Forbidden phrases must not appear in the missing-data fallback block either."""
    block = _build_ngram_findings_block(None).lower()
    for phrase in _FORBIDDEN_NGRAM_PHRASES:
        assert phrase.lower() not in block, (
            f"Forbidden phrase '{phrase}' must not appear in the missing-data block"
        )


def test_ngram_block_has_spend_table() -> None:
    """N-Gram block must include a spend-exposure sub-table."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA)
    assert "Spend Exposure" in block or "spend" in block.lower(), (
        "Block must include a spend-related sub-table"
    )


def test_ngram_block_flagged_waste_table_shown() -> None:
    """Flagged-waste sub-table appears when findings contain flagged rows."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA)
    assert "Flagged-Waste" in block or "flagged" in block.lower(), (
        "Flagged-waste sub-table must appear when flagged rows exist"
    )


def test_ngram_block_flagged_waste_table_hidden_when_no_flagged() -> None:
    """Flagged-waste sub-table must be omitted when no findings have flagged rows."""
    data_no_flagged = {
        "findings": [
            {
                "ngram": "logistics software",
                "n": 2,
                "row_count": 10,
                "total_spend_usd": 100.0,
                "total_clicks": 20,
                "total_impressions": 400,
                "flagged_waste_rows": 0,
                "clean_rows": 10,
                "unanalyzed_rows": 0,
            }
        ],
        "row_cap_applied": False,
        "total_rows_analyzed": 10,
    }
    block = _build_ngram_findings_block(data_no_flagged)
    assert "Flagged-Waste Concentration" not in block, (
        "Flagged-waste sub-table must be omitted when no flagged rows exist"
    )


def test_ngram_block_empty_findings_list() -> None:
    """When findings list is empty, a safe message is rendered without crashing."""
    data_empty = {
        "findings": [],
        "row_cap_applied": False,
        "total_rows_analyzed": 0,
    }
    block = _build_ngram_findings_block(data_empty)
    assert "No recurring patterns found" in block, (
        "Empty-findings message must appear when findings list is empty"
    )


def test_compute_ngram_findings_returns_none_for_none() -> None:
    """compute_ngram_findings(None) returns None without raising."""
    result = compute_ngram_findings(None)
    assert result is None, "compute_ngram_findings(None) must return None"


def test_compute_ngram_findings_returns_none_for_empty_list() -> None:
    """compute_ngram_findings([]) returns None without raising."""
    result = compute_ngram_findings([])
    assert result is None, "compute_ngram_findings([]) must return None"


def test_compute_ngram_findings_normalises_windsor_fields() -> None:
    """Windsor field names (campaign, spend) are normalized before aggregation."""
    windsor_rows = [
        {
            "search_term": "freight forwarding software",
            "campaign": "ACME Freight",
            "ad_group": "Forwarding",
            "keyword": "freight software",
            "spend": 50.0,
            "clicks": 10,
            "impressions": 200,
            "conversions": 0.0,
        },
        {
            "search_term": "logistics platform free",
            "campaign": "ACME Freight",
            "ad_group": "Logistics",
            "keyword": "logistics platform",
            "spend": 30.0,
            "clicks": 6,
            "impressions": 120,
            "conversions": 0.0,
        },
    ]
    result = compute_ngram_findings(windsor_rows)
    assert result is not None, "compute_ngram_findings must return a dict for non-empty input"
    assert "findings" in result, "Result must contain 'findings'"
    assert "row_cap_applied" in result, "Result must contain 'row_cap_applied'"
    assert "total_rows_analyzed" in result, "Result must contain 'total_rows_analyzed'"
    assert isinstance(result["findings"], list), "'findings' must be a list"
    assert len(result["findings"]) > 0, "Non-empty input must produce at least one n-gram"
    assert result["row_cap_applied"] is False, "row_cap_applied must be False for 2 rows"
    assert result["total_rows_analyzed"] == 2


def test_compute_ngram_findings_row_cap_flag() -> None:
    """row_cap_applied is True when input exceeds the cap."""
    # Build enough rows to exceed the cap
    many_rows = [
        {
            "search_term": f"freight forwarding term{i}",
            "campaign": "Campaign A",
            "spend": float(i),
            "clicks": 1,
            "impressions": 10,
            "conversions": 0.0,
        }
        for i in range(_NGRAM_ROW_CAP + 5)
    ]
    result = compute_ngram_findings(many_rows)
    assert result is not None
    assert result["row_cap_applied"] is True, (
        "row_cap_applied must be True when input exceeds _NGRAM_ROW_CAP"
    )
    assert result["total_rows_analyzed"] == _NGRAM_ROW_CAP, (
        "total_rows_analyzed must equal the cap when capped"
    )


def test_normalize_rows_zero_spend_preserved() -> None:
    """A legitimate spend_usd=0.0 must not fall back to a non-zero 'spend' field."""
    row = {"search_term": "freight", "spend_usd": 0.0, "spend": 99.99}
    result = _normalize_rows_for_ngrams([row])
    assert result[0]["spend_usd"] == 0.0, (
        "spend_usd=0.0 must be preserved even when 'spend' has a non-zero value"
    )


def test_normalize_rows_zero_clicks_preserved() -> None:
    """A legitimate clicks=0 must not be silently replaced by a non-zero fallback."""
    row = {"search_term": "freight", "clicks": 0}
    result = _normalize_rows_for_ngrams([row])
    assert result[0]["clicks"] == 0, "clicks=0 must be preserved"


def test_normalize_rows_zero_impressions_preserved() -> None:
    """A legitimate impressions=0 must not be silently replaced."""
    row = {"search_term": "freight", "impressions": 0}
    result = _normalize_rows_for_ngrams([row])
    assert result[0]["impressions"] == 0, "impressions=0 must be preserved"


def test_normalize_rows_zero_conversions_preserved() -> None:
    """A legitimate conversions=0.0 must not be silently replaced."""
    row = {"search_term": "freight", "conversions": 0.0}
    result = _normalize_rows_for_ngrams([row])
    assert result[0]["conversions"] == 0.0, "conversions=0.0 must be preserved"


def test_normalize_rows_spend_fallback_when_spend_usd_absent() -> None:
    """When spend_usd is absent (None), the 'spend' field must be used."""
    row = {"search_term": "freight", "spend_usd": None, "spend": 55.5}
    result = _normalize_rows_for_ngrams([row])
    assert result[0]["spend_usd"] == 55.5, (
        "'spend' must be used as fallback when spend_usd is None"
    )


def test_ngram_block_row_cap_note_uses_custom_cap() -> None:
    """Row-cap note must display the effective row_cap from ngram_data, not the constant."""
    custom_cap = 250
    data = {
        "findings": _SAMPLE_FINDINGS,
        "row_cap": custom_cap,
        "row_cap_applied": True,
        "total_rows_analyzed": custom_cap,
    }
    block = _build_ngram_findings_block(data)
    assert str(custom_cap) in block, (
        f"Row-cap note must show the effective cap ({custom_cap}), not the default constant"
    )
    assert str(_NGRAM_ROW_CAP) not in block or str(custom_cap) in block, (
        "If the default constant appears it must be because cap equals the default"
    )


def test_compute_ngram_findings_includes_row_cap_in_result() -> None:
    """compute_ngram_findings must include 'row_cap' in its returned dict."""
    rows = [{"search_term": "freight forwarding", "spend_usd": 10.0}]
    result = compute_ngram_findings(rows, row_cap=500)
    assert result is not None
    assert "row_cap" in result, "Result dict must contain 'row_cap'"
    assert result["row_cap"] == 500, "row_cap must equal the parameter passed to the function"


# ---------------------------------------------------------------------------
# Direct runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_ngram_block_renders_with_data,
        test_ngram_block_missing_data_does_not_crash,
        test_ngram_block_row_cap_note_appears,
        test_ngram_block_no_row_cap_note_when_not_capped,
        test_ngram_block_forbidden_words_absent,
        test_ngram_block_forbidden_words_absent_when_no_data,
        test_ngram_block_has_spend_table,
        test_ngram_block_flagged_waste_table_shown,
        test_ngram_block_flagged_waste_table_hidden_when_no_flagged,
        test_ngram_block_empty_findings_list,
        test_compute_ngram_findings_returns_none_for_none,
        test_compute_ngram_findings_returns_none_for_empty_list,
        test_compute_ngram_findings_normalises_windsor_fields,
        test_compute_ngram_findings_row_cap_flag,
        test_normalize_rows_zero_spend_preserved,
        test_normalize_rows_zero_clicks_preserved,
        test_normalize_rows_zero_impressions_preserved,
        test_normalize_rows_zero_conversions_preserved,
        test_normalize_rows_spend_fallback_when_spend_usd_absent,
        test_ngram_block_row_cap_note_uses_custom_cap,
        test_compute_ngram_findings_includes_row_cap_in_result,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except (AssertionError, TypeError, ValueError, AttributeError, KeyError) as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)
