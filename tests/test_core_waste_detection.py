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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.core import is_junk_term, load_patterns

# ---------------------------------------------------------------------------
# Load live patterns once for all tests
# ---------------------------------------------------------------------------

_PATTERNS = load_patterns()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _is_junk(term: str) -> bool:
    """Return True if is_junk_term flags the term."""
    flagged, _, _ = is_junk_term(term, _PATTERNS)
    return flagged


def _category(term: str) -> str | None:
    """Return the junk category for a term, or None if not junk."""
    _, cat, _ = is_junk_term(term, _PATTERNS)
    return cat


# ---------------------------------------------------------------------------
# English patterns
# ---------------------------------------------------------------------------

def test_free_english_intent_detected() -> None:
    """English 'free trial' must be flagged as junk (free_intent_english).

    Note: uses "cloud erp free trial" — terms containing 'logistics software', 'freight',
    or 'tms software' are protected by safe_terms and will not be flagged, by design.
    """
    assert _is_junk("cloud erp free trial"), (
        "'cloud erp free trial' must be flagged as junk"
    )


def test_job_seeker_pattern_detected() -> None:
    """'jobs' must be flagged as junk (job_seeker).

    Note: uses "forwarding jobs" — queries containing 'freight' alone are protected by
    safe_terms (freight is a core business term) and will not be flagged, by design.
    """
    assert _is_junk("forwarding jobs"), (
        "'forwarding jobs' must be flagged as junk"
    )


def test_student_pattern_detected() -> None:
    """'tutorial' must be flagged as junk (student).

    Note: uses "erp tutorial" — queries containing 'tms software' are protected by
    safe_terms and will not be flagged, by design.
    """
    assert _is_junk("erp tutorial"), (
        "'erp tutorial' must be flagged as junk"
    )


def test_job_seeker_category_correct() -> None:
    """The category for a job-seeker term must be 'job_seeker'."""
    cat = _category("shipping jobs")
    assert cat == "job_seeker", f"Expected 'job_seeker', got {cat!r}"


def test_student_category_correct() -> None:
    """The category for a student-intent term must be 'student'."""
    cat = _category("erp tutorial")
    assert cat == "student", f"Expected 'student', got {cat!r}"


def test_free_english_category_correct() -> None:
    """The category for an English free-intent term must be 'free_intent_english'."""
    cat = _category("cloud erp free trial")
    assert cat == "free_intent_english", f"Expected 'free_intent_english', got {cat!r}"


# ---------------------------------------------------------------------------
# Spanish patterns
# ---------------------------------------------------------------------------

def test_gratis_spanish_detected() -> None:
    """'gratis' (Spanish free-intent) must be flagged as junk."""
    assert _is_junk("software logistica gratis"), (
        "'software logistica gratis' must be flagged as junk"
    )


def test_gratuito_spanish_detected() -> None:
    """'gratuito' (Spanish free-intent) must be flagged as junk."""
    assert _is_junk("programa de logistica gratuito"), (
        "'programa de logistica gratuito' must be flagged as junk"
    )


def test_spanish_free_intent_category_correct() -> None:
    """The category for 'gratis' must be 'free_intent_spanish'."""
    cat = _category("software logistica gratis")
    assert cat == "free_intent_spanish", f"Expected 'free_intent_spanish', got {cat!r}"


# ---------------------------------------------------------------------------
# Arabic patterns (only if present in config — do not invent patterns)
# ---------------------------------------------------------------------------

def test_arabic_free_intent_detected_if_configured() -> None:
    """Arabic free-intent pattern 'مجاني' is detected if present in config."""
    arabic_terms = _PATTERNS.get("free_intent_arabic", {}).get("terms", [])
    if not arabic_terms:
        # Config has no Arabic free-intent terms — skip silently
        return

    # Use the first configured Arabic term as the probe
    first_term = arabic_terms[0]
    test_query = f"برنامج الشحن {first_term}"
    assert _is_junk(test_query), (
        f"Arabic free-intent term '{first_term}' must be flagged when present in config"
    )


def test_arabic_free_intent_category_correct_if_configured() -> None:
    """Arabic free-intent terms must map to 'free_intent_arabic' category."""
    arabic_terms = _PATTERNS.get("free_intent_arabic", {}).get("terms", [])
    if not arabic_terms:
        return  # No Arabic terms configured — nothing to test

    first_term = arabic_terms[0]
    cat = _category(f"برنامج الشحن {first_term}")
    assert cat == "free_intent_arabic", (
        f"Expected 'free_intent_arabic' category, got {cat!r}"
    )


# ---------------------------------------------------------------------------
# Safe terms — freight-forwarder terms must NOT be flagged
# ---------------------------------------------------------------------------

def test_freight_forwarding_software_not_flagged() -> None:
    """'freight forwarding software' is a safe, commercially relevant term."""
    assert not _is_junk("freight forwarding software"), (
        "'freight forwarding software' must NOT be flagged as junk"
    )


def test_tms_software_not_flagged() -> None:
    """'tms software' is a direct product search and must not be flagged."""
    assert not _is_junk("tms software"), (
        "'tms software' must NOT be flagged as junk"
    )


def test_logistics_software_not_flagged() -> None:
    """'logistics software' is a legitimate product-search term."""
    assert not _is_junk("logistics software"), (
        "'logistics software' must NOT be flagged as junk"
    )


def test_freight_alone_not_flagged() -> None:
    """'freight' alone is a core product-category term and must not be flagged."""
    assert not _is_junk("freight"), (
        "'freight' must NOT be flagged as junk (core business term)"
    )


def test_competitor_cargowise_not_flagged() -> None:
    """'cargowise' is a known competitor and must not be flagged."""
    assert not _is_junk("cargowise alternative"), (
        "'cargowise alternative' must NOT be flagged as junk"
    )


# ---------------------------------------------------------------------------
# Edge cases — empty / missing input handled safely
# ---------------------------------------------------------------------------

def test_empty_string_does_not_crash() -> None:
    """is_junk_term must handle empty string without raising."""
    flagged, cat, pattern = is_junk_term("", _PATTERNS)
    assert isinstance(flagged, bool), "is_junk_term must return a bool for empty string"
    assert flagged is False, "Empty string must not be flagged as junk"


def test_none_patterns_does_not_crash() -> None:
    """is_junk_term must handle an empty patterns dict without raising."""
    flagged, cat, pattern = is_junk_term("logistics software gratis", {})
    assert isinstance(flagged, bool)
    assert flagged is False, "No patterns → nothing can be flagged"


def test_load_patterns_returns_dict() -> None:
    """load_patterns() must return a non-empty dict."""
    patterns = load_patterns()
    assert isinstance(patterns, dict), "load_patterns() must return a dict"
    assert len(patterns) > 0, "load_patterns() must return a non-empty dict"


def test_load_patterns_contains_expected_categories() -> None:
    """load_patterns() must include the key junk categories."""
    patterns = load_patterns()
    for expected in ("job_seeker", "student", "free_intent_english", "free_intent_spanish"):
        assert expected in patterns, f"Expected category '{expected}' in junk_patterns.yaml"


# ---------------------------------------------------------------------------
# Direct runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_free_english_intent_detected,
        test_job_seeker_pattern_detected,
        test_student_pattern_detected,
        test_job_seeker_category_correct,
        test_student_category_correct,
        test_free_english_category_correct,
        test_gratis_spanish_detected,
        test_gratuito_spanish_detected,
        test_spanish_free_intent_category_correct,
        test_arabic_free_intent_detected_if_configured,
        test_arabic_free_intent_category_correct_if_configured,
        test_freight_forwarding_software_not_flagged,
        test_tms_software_not_flagged,
        test_logistics_software_not_flagged,
        test_freight_alone_not_flagged,
        test_competitor_cargowise_not_flagged,
        test_empty_string_does_not_crash,
        test_none_patterns_does_not_crash,
        test_load_patterns_returns_dict,
        test_load_patterns_contains_expected_categories,
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
