"""
tests/test_core_waste_detection.py

PR-ADS-067 — Regression tests for junk-pattern detection.

Protects:
  - English free/job/student patterns are detected
  - Spanish free-intent patterns (gratis) are detected
  - Arabic free-intent patterns are detected (if present in config)
  - Safe freight-forwarder terms are NOT automatically flagged
  - Missing search-term input or empty list is handled safely (no crash)

Run with:
  python -m pytest tests/test_core_waste_detection.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.core import is_junk_term, load_patterns

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixture — load patterns with repo root as CWD so config/ resolves correctly
# regardless of the directory from which pytest was invoked.
# ---------------------------------------------------------------------------

@pytest.fixture()
def junk_patterns(monkeypatch):
    """Load junk patterns with CWD set to the repo root."""
    monkeypatch.chdir(_REPO_ROOT)
    return load_patterns()


# ---------------------------------------------------------------------------
# Helpers — accept patterns explicitly to avoid any module-level CWD dependency
# ---------------------------------------------------------------------------

def _is_junk(term: str, p: dict) -> bool:
    """Return True if is_junk_term flags the term."""
    flagged, _, _ = is_junk_term(term, p)
    return flagged


def _category(term: str, p: dict) -> str | None:
    """Return the junk category for a term, or None if not junk."""
    _, cat, _ = is_junk_term(term, p)
    return cat


# ---------------------------------------------------------------------------
# English patterns
# ---------------------------------------------------------------------------

def test_free_english_intent_detected(junk_patterns) -> None:
    """English 'free trial' must be flagged as junk (free_intent_english).

    Note: uses "cloud erp free trial" — terms containing 'logistics software', 'freight',
    or 'tms software' are protected by safe_terms and will not be flagged, by design.
    """
    assert _is_junk("cloud erp free trial", junk_patterns), (
        "'cloud erp free trial' must be flagged as junk"
    )


def test_job_seeker_pattern_detected(junk_patterns) -> None:
    """'jobs' must be flagged as junk (job_seeker).

    Note: uses "forwarding jobs" — queries containing 'freight' alone are protected by
    safe_terms (freight is a core business term) and will not be flagged, by design.
    """
    assert _is_junk("forwarding jobs", junk_patterns), (
        "'forwarding jobs' must be flagged as junk"
    )


def test_student_pattern_detected(junk_patterns) -> None:
    """'tutorial' must be flagged as junk (student).

    Note: uses "erp tutorial" — queries containing 'tms software' are protected by
    safe_terms and will not be flagged, by design.
    """
    assert _is_junk("erp tutorial", junk_patterns), (
        "'erp tutorial' must be flagged as junk"
    )


def test_job_seeker_category_correct(junk_patterns) -> None:
    """The category for a job-seeker term must be 'job_seeker'."""
    cat = _category("shipping jobs", junk_patterns)
    assert cat == "job_seeker", f"Expected 'job_seeker', got {cat!r}"


def test_student_category_correct(junk_patterns) -> None:
    """The category for a student-intent term must be 'student'."""
    cat = _category("erp tutorial", junk_patterns)
    assert cat == "student", f"Expected 'student', got {cat!r}"


def test_free_english_category_correct(junk_patterns) -> None:
    """The category for an English free-intent term must be 'free_intent_english'."""
    cat = _category("cloud erp free trial", junk_patterns)
    assert cat == "free_intent_english", f"Expected 'free_intent_english', got {cat!r}"


# ---------------------------------------------------------------------------
# Spanish patterns
# ---------------------------------------------------------------------------

def test_gratis_spanish_detected(junk_patterns) -> None:
    """'gratis' (Spanish free-intent) must be flagged as junk."""
    assert _is_junk("software logistica gratis", junk_patterns), (
        "'software logistica gratis' must be flagged as junk"
    )


def test_gratuito_spanish_detected(junk_patterns) -> None:
    """'gratuito' (Spanish free-intent) must be flagged as junk."""
    assert _is_junk("programa de logistica gratuito", junk_patterns), (
        "'programa de logistica gratuito' must be flagged as junk"
    )


def test_spanish_free_intent_category_correct(junk_patterns) -> None:
    """The category for 'gratis' must be 'free_intent_spanish'."""
    cat = _category("software logistica gratis", junk_patterns)
    assert cat == "free_intent_spanish", f"Expected 'free_intent_spanish', got {cat!r}"


# ---------------------------------------------------------------------------
# Arabic patterns (only if present in config — do not invent patterns)
# ---------------------------------------------------------------------------

def test_arabic_free_intent_detected_if_configured(junk_patterns) -> None:
    """Arabic free-intent pattern 'مجاني' is detected if present in config."""
    arabic_terms = junk_patterns.get("free_intent_arabic", {}).get("terms", [])
    if not arabic_terms:
        # Config has no Arabic free-intent terms — skip silently
        return

    # Use the first configured Arabic term as the probe
    first_term = arabic_terms[0]
    test_query = f"برنامج الشحن {first_term}"
    assert _is_junk(test_query, junk_patterns), (
        f"Arabic free-intent term '{first_term}' must be flagged when present in config"
    )


def test_arabic_free_intent_category_correct_if_configured(junk_patterns) -> None:
    """Arabic free-intent terms must map to 'free_intent_arabic' category."""
    arabic_terms = junk_patterns.get("free_intent_arabic", {}).get("terms", [])
    if not arabic_terms:
        return  # No Arabic terms configured — nothing to test

    first_term = arabic_terms[0]
    cat = _category(f"برنامج الشحن {first_term}", junk_patterns)
    assert cat == "free_intent_arabic", (
        f"Expected 'free_intent_arabic' category, got {cat!r}"
    )


# ---------------------------------------------------------------------------
# Safe terms — freight-forwarder terms must NOT be flagged
# ---------------------------------------------------------------------------

def test_freight_forwarding_software_not_flagged(junk_patterns) -> None:
    """'freight forwarding software' is a safe, commercially relevant term."""
    assert not _is_junk("freight forwarding software", junk_patterns), (
        "'freight forwarding software' must NOT be flagged as junk"
    )


def test_tms_software_not_flagged(junk_patterns) -> None:
    """'tms software' is a direct product search and must not be flagged."""
    assert not _is_junk("tms software", junk_patterns), (
        "'tms software' must NOT be flagged as junk"
    )


def test_logistics_software_not_flagged(junk_patterns) -> None:
    """'logistics software' is a legitimate product-search term."""
    assert not _is_junk("logistics software", junk_patterns), (
        "'logistics software' must NOT be flagged as junk"
    )


def test_freight_alone_not_flagged(junk_patterns) -> None:
    """'freight' alone is a core product-category term and must not be flagged."""
    assert not _is_junk("freight", junk_patterns), (
        "'freight' must NOT be flagged as junk (core business term)"
    )


def test_competitor_cargowise_not_flagged(junk_patterns) -> None:
    """'cargowise' is a known competitor and must not be flagged."""
    assert not _is_junk("cargowise alternative", junk_patterns), (
        "'cargowise alternative' must NOT be flagged as junk"
    )


# ---------------------------------------------------------------------------
# Edge cases — empty / missing input handled safely (no patterns needed)
# ---------------------------------------------------------------------------

def test_empty_string_does_not_crash() -> None:
    """is_junk_term must handle empty string without raising."""
    flagged, cat, pattern = is_junk_term("", {})
    assert isinstance(flagged, bool), "is_junk_term must return a bool for empty string"
    assert flagged is False, "Empty string must not be flagged as junk"


def test_none_patterns_does_not_crash() -> None:
    """is_junk_term must handle an empty patterns dict without raising."""
    flagged, cat, pattern = is_junk_term("logistics software gratis", {})
    assert isinstance(flagged, bool)
    assert flagged is False, "No patterns → nothing can be flagged"


def test_load_patterns_returns_dict(junk_patterns) -> None:
    """load_patterns() must return a non-empty dict."""
    assert isinstance(junk_patterns, dict), "load_patterns() must return a dict"
    assert len(junk_patterns) > 0, "load_patterns() must return a non-empty dict"


def test_load_patterns_contains_expected_categories(junk_patterns) -> None:
    """load_patterns() must include the key junk categories."""
    for expected in ("job_seeker", "student", "free_intent_english", "free_intent_spanish"):
        assert expected in junk_patterns, (
            f"Expected category '{expected}' in junk_patterns.yaml"
        )


# ---------------------------------------------------------------------------
# Direct runner (chdirs to repo root before loading patterns)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.chdir(_REPO_ROOT)
    _patterns = load_patterns()

    def _run(fn, *args):
        try:
            fn(*args)
            print(f"  PASS  {fn.__name__}")
            return True
        except (AssertionError, TypeError, ValueError, AttributeError, KeyError) as exc:
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
        return False

    results = [
        _run(test_free_english_intent_detected, _patterns),
        _run(test_job_seeker_pattern_detected, _patterns),
        _run(test_student_pattern_detected, _patterns),
        _run(test_job_seeker_category_correct, _patterns),
        _run(test_student_category_correct, _patterns),
        _run(test_free_english_category_correct, _patterns),
        _run(test_gratis_spanish_detected, _patterns),
        _run(test_gratuito_spanish_detected, _patterns),
        _run(test_spanish_free_intent_category_correct, _patterns),
        _run(test_arabic_free_intent_detected_if_configured, _patterns),
        _run(test_arabic_free_intent_category_correct_if_configured, _patterns),
        _run(test_freight_forwarding_software_not_flagged, _patterns),
        _run(test_tms_software_not_flagged, _patterns),
        _run(test_logistics_software_not_flagged, _patterns),
        _run(test_freight_alone_not_flagged, _patterns),
        _run(test_competitor_cargowise_not_flagged, _patterns),
        _run(test_empty_string_does_not_crash),
        _run(test_none_patterns_does_not_crash),
        _run(test_load_patterns_returns_dict, _patterns),
        _run(test_load_patterns_contains_expected_categories, _patterns),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} tests passed.")
    sys.exit(0 if passed == len(results) else 1)
