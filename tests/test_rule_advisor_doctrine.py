"""
tests/test_rule_advisor_doctrine.py

PR-ADS-067 — Regression tests for deterministic advisor wording doctrine.

Protects:
  - Advisor output avoids imperative action wording (pause, cut, push negative, etc.)
  - Advisor uses advisory language (warrants review, suggests attention, candidate, etc.)
  - CPQL renders as N/A in report table when SQLs = 0
  - N-Gram block is read-only, candidate-only, and contains no unsafe imperative commands

Run with:
  python -m pytest tests/test_rule_advisor_doctrine.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.rule_advisor import (
    _build_campaign_truth_table,
    _build_executive_summary,
    _build_human_review_items,
    _build_ngram_findings_block,
    _build_phase1_reminder,
    _FORBIDDEN_NGRAM_PHRASES,
    generate_deterministic_report,
)


# ---------------------------------------------------------------------------
# Forbidden active-instruction phrases — must NOT appear in generated output
# These are phrases that would imply the system is taking or ordering actions.
# ---------------------------------------------------------------------------

_FORBIDDEN_ACTIVE_INSTRUCTIONS = [
    "pause this campaign",
    "cut this campaign",
    "increase budget",
    "apply negative",
    "push negative",
    "block this term",
    "change bid",
    "change budget",
    "upload conversion",
]

# Phrases that are safe governor language and should NOT be flagged:
# e.g., "does not push negative keywords" is a read-only disclaimer.
_SAFE_DISCLAIMER_FRAGMENTS = [
    "does not push",
    "does not add",
    "does not modify",
    "does not pause",
    "does not take actions",
    "no negative keywords have been added",
    "no google ads campaigns have been paused",
]

# ---------------------------------------------------------------------------
# Advisory language — at least one of these must appear in relevant sections
# ---------------------------------------------------------------------------

_ADVISORY_LANGUAGE = [
    "warrants review",
    "suggests attention",
    "human review",
    "candidate",
    "read-only",
    "before any action",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_CAMPAIGN_TRUTH = {
    "campaigns": [
        {
            "campaign": "europe low cpc-new",
            "spend_30d_usd": 800.00,
            "total_leads": 10,
            "confirmed_sqls": 2,
            "junk_rate_pct": 10.0,
            "cpql_usd": 400.0,
            "verdict": "SCALE",
            "verdict_reason": "2 confirmed SQL(s) | 10.0% junk rate",
            "warnings": [],
        },
        {
            "campaign": "emerging - markets",
            "spend_30d_usd": 500.00,
            "total_leads": 5,
            "confirmed_sqls": 0,
            "junk_rate_pct": None,
            "cpql_usd": None,
            "verdict": "HOLD",
            "verdict_reason": "Insufficient data to classify or no clear signal",
            "warnings": [],
        },
        {
            "campaign": "mexico,chile",
            "spend_30d_usd": 600.00,
            "total_leads": 8,
            "confirmed_sqls": 0,
            "junk_rate_pct": 50.0,
            "cpql_usd": None,
            "verdict": "FIX",
            "verdict_reason": "50.0% junk rate exceeds 25% threshold",
            "warnings": [],
        },
    ],
    "summary": {
        "fix_count": 1,
        "cut_count": 0,
        "scale_count": 1,
        "hold_count": 1,
        "total_spend_usd": 1900.00,
        "total_confirmed_sqls": 2,
    },
}

_SAMPLE_LEAD_QUALITY = {
    "total_contacts_analysed": 23,
    "by_campaign": [
        {
            "campaign": "europe low cpc-new",
            "total": 10,
            "qualified": 2,
            "in_progress": 1,
            "unknown": 2,
            "junk": 1,
            "wrong_fit": 4,
            "no_status": 0,
            "verdicted_contacts": 8,
            "junk_rate_pct": 12.5,
            "cpql": None,
            "qualified_examples": [],
            "junk_examples": [],
            "warnings": [],
        }
    ],
}

_SAMPLE_WASTE = {
    "generated_at": "2026-01-01T00:00:00",
    "data_source": "search_terms",
    "data_warning": None,
    "total_spend_analysed": 1900.00,
    "confirmed_waste_usd": 120.00,
    "suspected_waste_usd": 45.00,
    "confirmed_waste_items": [
        {
            "term": "logistics software free",
            "campaign": "mexico,chile",
            "spend_usd": 120.00,
            "junk_category": "free_intent_english",
            "matched_pattern": " free",
            "crm_contacts_found": 2,
            "crm_junk_confirmed": 1,
            "crm_qualified": 0,
        }
    ],
    "suspected_waste_items": [],
}

_SAMPLE_NGRAM_DATA = {
    "findings": [
        {
            "ngram": "freight forwarding",
            "n": 2,
            "row_count": 15,
            "total_spend_usd": 250.50,
            "total_clicks": 40,
            "total_impressions": 800,
            "flagged_waste_rows": 3,
            "clean_rows": 8,
            "unanalyzed_rows": 4,
        },
        {
            "ngram": "free logistics",
            "n": 2,
            "row_count": 8,
            "total_spend_usd": 90.00,
            "total_clicks": 15,
            "total_impressions": 300,
            "flagged_waste_rows": 8,
            "clean_rows": 0,
            "unanalyzed_rows": 0,
        },
    ],
    "row_cap": 1000,
    "row_cap_applied": False,
    "total_rows_analyzed": 50,
}


# ---------------------------------------------------------------------------
# Helper: check text for forbidden active instructions
# Exempts lines that are clearly read-only disclaimers (safe governor language).
# ---------------------------------------------------------------------------

def _has_forbidden_instruction(text: str, phrase: str) -> bool:
    """Return True only if the phrase appears as an active instruction.

    A line is considered a safe disclaimer if it contains any of the known
    safe-disclaimer fragments (e.g. "does not push negative keywords").
    """
    text_lower = text.lower()
    phrase_lower = phrase.lower()

    if phrase_lower not in text_lower:
        return False  # phrase doesn't appear at all — safe

    # Check each line that contains the phrase
    for line in text.splitlines():
        if phrase_lower in line.lower():
            is_disclaimer = any(
                frag in line.lower() for frag in _SAFE_DISCLAIMER_FRAGMENTS
            )
            if not is_disclaimer:
                return True  # Found as active instruction

    return False  # Only appeared in disclaimers


# ---------------------------------------------------------------------------
# Part D — Campaign truth table: CPQL N/A when SQLs = 0
# ---------------------------------------------------------------------------

def test_cpql_renders_na_when_sqls_zero() -> None:
    """Campaign truth table must render CPQL as N/A when confirmed_sqls = 0."""
    block = _build_campaign_truth_table(_SAMPLE_CAMPAIGN_TRUTH)
    # "emerging - markets" and "mexico,chile" have 0 confirmed SQLs
    lines = [ln for ln in block.splitlines() if "emerging" in ln.lower() or "mexico,chile" in ln.lower()]
    for line in lines:
        assert "N/A" in line, (
            f"CPQL must be N/A for zero-SQL campaigns, got line: {line!r}"
        )


def test_cpql_does_not_show_dollar_zero_for_zero_sqls() -> None:
    """CPQL must not display $0.00 for campaigns with zero SQLs."""
    block = _build_campaign_truth_table(_SAMPLE_CAMPAIGN_TRUTH)
    lines = [ln for ln in block.splitlines() if "emerging" in ln.lower() or "mexico,chile" in ln.lower()]
    for line in lines:
        # $0.00 would be wrong — it should be N/A
        assert "$0.00" not in line, (
            f"CPQL must not be $0.00 for zero-SQL campaigns, got line: {line!r}"
        )


def test_cpql_shows_value_when_sqls_positive() -> None:
    """CPQL must show a dollar amount for campaigns with positive SQLs."""
    block = _build_campaign_truth_table(_SAMPLE_CAMPAIGN_TRUTH)
    lines = [ln for ln in block.splitlines() if "europe low cpc-new" in ln.lower()]
    assert lines, "Expected a row for 'europe low cpc-new' in campaign table"
    assert "N/A" not in lines[0] or "$400" in lines[0], (
        f"CPQL should be $400.00 for positive-SQL campaign, got: {lines[0]!r}"
    )
    assert "$400" in lines[0], (
        f"CPQL for europe low cpc-new with 2 SQLs and $800 spend should be $400.00, "
        f"got: {lines[0]!r}"
    )


# ---------------------------------------------------------------------------
# Part D — Advisor language: active instructions must not appear
# ---------------------------------------------------------------------------

def test_campaign_truth_block_has_no_forbidden_instructions() -> None:
    """Campaign truth table must not contain forbidden active instructions."""
    block = _build_campaign_truth_table(_SAMPLE_CAMPAIGN_TRUTH)
    for phrase in _FORBIDDEN_ACTIVE_INSTRUCTIONS:
        assert not _has_forbidden_instruction(block, phrase), (
            f"Forbidden phrase '{phrase}' found as active instruction in campaign truth block"
        )


def test_human_review_block_has_no_forbidden_instructions() -> None:
    """Human review section must not contain forbidden active instructions."""
    block = _build_human_review_items(_SAMPLE_WASTE, _SAMPLE_LEAD_QUALITY, _SAMPLE_CAMPAIGN_TRUTH)
    for phrase in _FORBIDDEN_ACTIVE_INSTRUCTIONS:
        assert not _has_forbidden_instruction(block, phrase), (
            f"Forbidden phrase '{phrase}' found as active instruction in human-review block"
        )


def test_phase1_reminder_has_no_forbidden_instructions() -> None:
    """Phase 1 reminder must not contain forbidden active instructions."""
    block = _build_phase1_reminder()
    for phrase in _FORBIDDEN_ACTIVE_INSTRUCTIONS:
        assert not _has_forbidden_instruction(block, phrase), (
            f"Forbidden phrase '{phrase}' found as active instruction in phase1 reminder"
        )


def test_executive_summary_has_no_forbidden_instructions() -> None:
    """Executive summary must not contain forbidden active instructions."""
    block = _build_executive_summary(
        _SAMPLE_WASTE, _SAMPLE_LEAD_QUALITY, _SAMPLE_CAMPAIGN_TRUTH, "weekly"
    )
    for phrase in _FORBIDDEN_ACTIVE_INSTRUCTIONS:
        assert not _has_forbidden_instruction(block, phrase), (
            f"Forbidden phrase '{phrase}' found as active instruction in executive summary"
        )


def test_ngram_block_has_no_forbidden_instructions() -> None:
    """N-Gram findings block must not contain forbidden active instructions."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA)
    for phrase in _FORBIDDEN_ACTIVE_INSTRUCTIONS:
        assert not _has_forbidden_instruction(block, phrase), (
            f"Forbidden phrase '{phrase}' found as active instruction in N-Gram block"
        )


def test_ngram_block_has_no_forbidden_ngram_phrases() -> None:
    """N-Gram block must not contain the defined forbidden N-Gram phrases."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA).lower()
    for phrase in _FORBIDDEN_NGRAM_PHRASES:
        assert phrase.lower() not in block, (
            f"Forbidden N-Gram phrase '{phrase}' must not appear in N-Gram block"
        )


# ---------------------------------------------------------------------------
# Part D — Advisory language must be present
# ---------------------------------------------------------------------------

def test_human_review_block_uses_advisory_language() -> None:
    """Human review section must use advisory language (warrants review, etc.)."""
    block = _build_human_review_items(
        _SAMPLE_WASTE, _SAMPLE_LEAD_QUALITY, _SAMPLE_CAMPAIGN_TRUTH
    ).lower()
    found = [phrase for phrase in _ADVISORY_LANGUAGE if phrase in block]
    assert found, (
        f"Human review block must contain at least one advisory phrase from {_ADVISORY_LANGUAGE}; "
        f"none found in block."
    )


def test_ngram_block_uses_advisory_language() -> None:
    """N-Gram block must use advisory language (candidate, read-only, human review, etc.)."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA).lower()
    found = [phrase for phrase in _ADVISORY_LANGUAGE if phrase in block]
    assert found, (
        f"N-Gram block must contain at least one advisory phrase from {_ADVISORY_LANGUAGE}; "
        f"none found."
    )


def test_phase1_reminder_uses_read_only_language() -> None:
    """Phase 1 reminder must use read-only language."""
    block = _build_phase1_reminder().lower()
    assert "read-only" in block or "no actions" in block, (
        "Phase 1 reminder must mention read-only or no-actions doctrine"
    )


# ---------------------------------------------------------------------------
# Part D — N-Gram block safety end-to-end
# ---------------------------------------------------------------------------

def test_ngram_block_exists_when_data_present() -> None:
    """N-Gram section must render and contain the section header."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA)
    assert "N-Gram Findings" in block, "N-Gram block must contain section header"


def test_ngram_block_contains_candidate_language() -> None:
    """N-Gram block must describe patterns as 'candidate' patterns."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA).lower()
    assert "candidate" in block, (
        "N-Gram block must use 'candidate' language for patterns"
    )


def test_ngram_block_is_labeled_read_only() -> None:
    """N-Gram block must be labeled Read-Only."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA)
    assert "Read-Only" in block, "N-Gram block must be labeled Read-Only"


def test_ngram_block_mentions_human_review() -> None:
    """N-Gram block must mention human review."""
    block = _build_ngram_findings_block(_SAMPLE_NGRAM_DATA).lower()
    assert "human review" in block, "N-Gram block must mention human review"


# ---------------------------------------------------------------------------
# Part D — Full report via generate_deterministic_report using tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_workspace(tmp_path, monkeypatch):
    """Set up a temporary workspace for generate_deterministic_report.

    Creates outputs/ with JSON fixtures and config/ (copied from the real config),
    then changes the working directory so the module's relative paths resolve here.
    All file I/O is isolated to tmp_path — no writes to the real repository.
    """
    import shutil

    # Create outputs dir with fixture JSON files
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "waste_report.json").write_text(
        json.dumps(_SAMPLE_WASTE), encoding="utf-8"
    )
    (out / "lead_quality.json").write_text(
        json.dumps(_SAMPLE_LEAD_QUALITY), encoding="utf-8"
    )
    (out / "campaign_truth.json").write_text(
        json.dumps(_SAMPLE_CAMPAIGN_TRUTH), encoding="utf-8"
    )

    # Copy config dir so tests work on any platform (avoids symlink permission issues)
    real_config = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(str(real_config), str(tmp_path / "config"))

    # chdir so module-level relative paths work
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_full_report_generates_without_error(test_workspace) -> None:
    """generate_deterministic_report must produce a report file without raising."""
    result = generate_deterministic_report("weekly", ngram_data=_SAMPLE_NGRAM_DATA)
    assert result is not None, "generate_deterministic_report must return a path, not None"
    assert Path(result).exists(), f"Report file must exist at {result}"


def test_full_report_has_no_forbidden_instructions(test_workspace) -> None:
    """Full generated report must not contain forbidden active instructions."""
    report_path = generate_deterministic_report("weekly", ngram_data=_SAMPLE_NGRAM_DATA)
    assert report_path is not None
    text = Path(report_path).read_text(encoding="utf-8")
    for phrase in _FORBIDDEN_ACTIVE_INSTRUCTIONS:
        assert not _has_forbidden_instruction(text, phrase), (
            f"Forbidden phrase '{phrase}' found as active instruction in full report"
        )


def test_full_report_contains_advisory_language(test_workspace) -> None:
    """Full generated report must contain advisory language."""
    report_path = generate_deterministic_report("weekly", ngram_data=_SAMPLE_NGRAM_DATA)
    assert report_path is not None
    text = Path(report_path).read_text(encoding="utf-8").lower()
    found = [phrase for phrase in _ADVISORY_LANGUAGE if phrase in text]
    assert found, (
        f"Full report must contain at least one advisory phrase; checked: {_ADVISORY_LANGUAGE}"
    )


def test_full_report_cpql_na_for_zero_sql_campaigns(test_workspace) -> None:
    """Full report must show CPQL as N/A for campaigns with 0 confirmed SQLs."""
    report_path = generate_deterministic_report("weekly", ngram_data=_SAMPLE_NGRAM_DATA)
    assert report_path is not None
    text = Path(report_path).read_text(encoding="utf-8")
    # "emerging - markets" has 0 SQLs → CPQL must be N/A
    for line in text.splitlines():
        if "emerging" in line.lower() and "|" in line:
            assert "N/A" in line, (
                f"CPQL must be N/A for zero-SQL campaign in full report, got: {line!r}"
            )


def test_full_report_ngram_block_is_read_only(test_workspace) -> None:
    """Full report must include the N-Gram block with read-only label."""
    report_path = generate_deterministic_report("weekly", ngram_data=_SAMPLE_NGRAM_DATA)
    assert report_path is not None
    text = Path(report_path).read_text(encoding="utf-8")
    assert "N-Gram Findings" in text, "Full report must include N-Gram section"
    assert "Read-Only" in text, "N-Gram section must be labeled Read-Only"


def test_full_report_ngram_block_has_no_forbidden_ngram_phrases(test_workspace) -> None:
    """Full report N-Gram section must not contain forbidden N-Gram phrases."""
    report_path = generate_deterministic_report("weekly", ngram_data=_SAMPLE_NGRAM_DATA)
    assert report_path is not None
    text = Path(report_path).read_text(encoding="utf-8").lower()
    for phrase in _FORBIDDEN_NGRAM_PHRASES:
        assert phrase.lower() not in text, (
            f"Forbidden N-Gram phrase '{phrase}' must not appear in full report"
        )


# ---------------------------------------------------------------------------
# Direct runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests_no_fixture = [
        test_cpql_renders_na_when_sqls_zero,
        test_cpql_does_not_show_dollar_zero_for_zero_sqls,
        test_cpql_shows_value_when_sqls_positive,
        test_campaign_truth_block_has_no_forbidden_instructions,
        test_human_review_block_has_no_forbidden_instructions,
        test_phase1_reminder_has_no_forbidden_instructions,
        test_executive_summary_has_no_forbidden_instructions,
        test_ngram_block_has_no_forbidden_instructions,
        test_ngram_block_has_no_forbidden_ngram_phrases,
        test_human_review_block_uses_advisory_language,
        test_ngram_block_uses_advisory_language,
        test_phase1_reminder_uses_read_only_language,
        test_ngram_block_exists_when_data_present,
        test_ngram_block_contains_candidate_language,
        test_ngram_block_is_labeled_read_only,
        test_ngram_block_mentions_human_review,
    ]
    failed = 0
    for t in tests_no_fixture:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except (AssertionError, TypeError, ValueError, AttributeError, KeyError) as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{len(tests_no_fixture) - failed}/{len(tests_no_fixture)} tests passed.")
    print("(Note: full-report tests require pytest fixtures — run with pytest for those)")
    sys.exit(1 if failed else 0)
