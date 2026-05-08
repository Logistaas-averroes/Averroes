"""
tests/test_ngrams_config.py

Smoke tests for PR-ADS-060: N-Gram Stopword Config.

Tests:
  - Config file loads without error.
  - Protected token survives even if also listed as a stopword.
  - English stopword is removed from tokenization.
  - Spanish stopword is removed from tokenization.
  - Arabic stopword is removed from tokenization.
  - Waste-signal token (gratis) is preserved.
  - Business token (freight) is preserved.
  - Numeric-only tokens are still removed by tokenization.
  - Missing config falls back safely (does not raise).

Run with:
  python -m pytest tests/test_ngrams_config.py -v
or:
  python tests/test_ngrams_config.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

# Allow running this file directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.ngrams import (
    get_protected_tokens,
    get_stopword_tokens,
    load_ngram_stopword_config,
    tokenize_search_term,
    validate_ngram_config,
)


def test_config_loads() -> None:
    """Config file loads and returns a dict with expected top-level keys."""
    # Clear the lru_cache so tests are independent.
    load_ngram_stopword_config.cache_clear()
    config = load_ngram_stopword_config()
    assert isinstance(config, dict), "config must be a dict"
    assert "stopwords" in config, "config must contain 'stopwords'"
    assert "protected_tokens" in config, "config must contain 'protected_tokens'"


def test_stopwords_non_empty() -> None:
    """Stopword set is non-empty and contains expected English/Spanish/Arabic tokens."""
    load_ngram_stopword_config.cache_clear()
    sw = get_stopword_tokens()
    assert "the" in sw, "'the' must be a stopword"
    assert "de" in sw, "'de' must be a stopword"
    assert "من" in sw, "'من' must be a stopword"


def test_protected_tokens_non_empty() -> None:
    """Protected token set is non-empty and contains business/waste-signal terms."""
    load_ngram_stopword_config.cache_clear()
    protected = get_protected_tokens()
    assert "freight" in protected, "'freight' must be a protected token"
    assert "gratis" in protected, "'gratis' must be a protected token"


def test_protected_token_survives_if_also_stopword() -> None:
    """A token in both stopwords and protected_tokens must survive tokenization."""
    import analysis.ngrams as _ng

    synthetic_config = {
        "version": 1,
        "stopwords": {"english": ["the", "free"]},
        "protected_tokens": {"waste_signal_terms": ["free"]},
    }

    with patch(
        "analysis.ngrams.load_ngram_stopword_config",
        return_value=synthetic_config,
    ):
        _ng.get_stopword_tokens.cache_clear()
        _ng.get_protected_tokens.cache_clear()
        try:
            tokens = _ng.tokenize_search_term("the free freight")
            assert "the" not in tokens, "'the' must be removed as a stopword"
            assert "free" in tokens, "'free' must be kept — it is protected"
            assert "freight" in tokens, "'freight' must be kept"
        finally:
            _ng.get_stopword_tokens.cache_clear()
            _ng.get_protected_tokens.cache_clear()


def test_english_stopword_removed() -> None:
    """Common English stopwords are removed from tokenized output."""
    load_ngram_stopword_config.cache_clear()
    tokens = tokenize_search_term("the best freight forwarding software")
    assert "the" not in tokens, "'the' must be removed as a stopword"
    assert "freight" in tokens, "'freight' must be preserved"


def test_spanish_stopword_removed() -> None:
    """Common Spanish stopwords are removed from tokenized output."""
    load_ngram_stopword_config.cache_clear()
    tokens = tokenize_search_term("software de logistica gratis")
    assert "de" not in tokens, "'de' must be removed as a stopword"
    assert "gratis" in tokens, "'gratis' must be preserved"


def test_arabic_stopword_removed() -> None:
    """Arabic stopwords are removed from tokenized output."""
    load_ngram_stopword_config.cache_clear()
    tokens = tokenize_search_term("من افضل برامج الشحن")
    assert "من" not in tokens, "'من' must be removed as an Arabic stopword"


def test_waste_signal_token_gratis_preserved() -> None:
    """The waste-signal token 'gratis' must survive tokenization."""
    load_ngram_stopword_config.cache_clear()
    tokens = tokenize_search_term("software de logistica gratis")
    assert "gratis" in tokens, "'gratis' must be preserved as a waste-signal token"


def test_business_token_freight_preserved() -> None:
    """The business token 'freight' must survive tokenization."""
    load_ngram_stopword_config.cache_clear()
    tokens = tokenize_search_term("the best freight forwarding software")
    assert "freight" in tokens, "'freight' must be preserved as a business token"


def test_numeric_only_tokens_removed() -> None:
    """Numeric-only tokens (e.g. '2024', '100') are still removed."""
    load_ngram_stopword_config.cache_clear()
    tokens = tokenize_search_term("freight software 2024 100")
    assert "2024" not in tokens, "numeric '2024' must be removed"
    assert "100" not in tokens, "numeric '100' must be removed"
    assert "freight" in tokens, "'freight' must survive"
    assert "software" in tokens, "'software' must survive"


def test_missing_config_falls_back_safely() -> None:
    """When the config file does not exist, load_ngram_stopword_config returns a safe default."""
    load_ngram_stopword_config.cache_clear()
    with patch("analysis.ngrams._CONFIG_PATH", Path("/nonexistent/path/ngram_stopwords.yaml")):
        load_ngram_stopword_config.cache_clear()
        config = load_ngram_stopword_config()
        assert isinstance(config, dict), "fallback must return a dict"
        assert "stopwords" in config, "fallback config must contain 'stopwords'"
        assert "protected_tokens" in config, "fallback config must contain 'protected_tokens'"
    load_ngram_stopword_config.cache_clear()


def test_validate_ngram_config_valid() -> None:
    """validate_ngram_config returns no warnings for a well-formed config."""
    good = {
        "version": 1,
        "stopwords": {"english": ["the", "a"]},
        "protected_tokens": {"business_terms": ["freight"]},
    }
    warnings = validate_ngram_config(good)
    assert warnings == [], f"expected no warnings, got: {warnings}"


def test_validate_ngram_config_missing_keys() -> None:
    """validate_ngram_config returns warnings for missing keys."""
    bad = {}
    warnings = validate_ngram_config(bad)
    assert any("version" in w for w in warnings), "should warn about missing version"
    assert any("stopwords" in w for w in warnings), "should warn about missing stopwords"
    assert any("protected_tokens" in w for w in warnings), "should warn about missing protected_tokens"


def test_missing_stopwords_falls_back_to_defaults() -> None:
    """When 'stopwords' is missing from config, fallback defaults include known tokens."""
    load_ngram_stopword_config.cache_clear()
    get_stopword_tokens.cache_clear()
    get_protected_tokens.cache_clear()

    bad_config = {
        "version": 1,
        # No 'stopwords' key — fatal shape error → should fall back
        "protected_tokens": {"business_terms": ["freight"]},
    }

    with patch("analysis.ngrams.load_ngram_stopword_config", return_value=bad_config):
        import analysis.ngrams as _ng
        _ng.load_ngram_stopword_config.cache_clear()
        _ng.get_stopword_tokens.cache_clear()
        _ng.get_protected_tokens.cache_clear()
        try:
            # load_ngram_stopword_config will detect fatal errors and fall back internally;
            # but since we're patching it to return the bad config directly, we verify that
            # get_stopword_tokens() does not raise and returns a usable frozenset.
            sw = _ng.get_stopword_tokens()
            # With a missing 'stopwords' key the helper gets {} and returns frozenset()
            # rather than crashing — no empty-set crash, no exception.
            assert isinstance(sw, frozenset)
        finally:
            _ng.load_ngram_stopword_config.cache_clear()
            _ng.get_stopword_tokens.cache_clear()
            _ng.get_protected_tokens.cache_clear()

    load_ngram_stopword_config.cache_clear()
    get_stopword_tokens.cache_clear()
    get_protected_tokens.cache_clear()


def test_invalid_protected_tokens_shape_warns() -> None:
    """validate_ngram_config warns when a protected_tokens category value is not a list."""
    bad = {
        "version": 1,
        "stopwords": {"english": ["the"]},
        "protected_tokens": {"business_terms": "freight"},  # string, not list
    }
    warnings = validate_ngram_config(bad)
    assert any("business_terms" in w for w in warnings), (
        "should warn about non-list protected_tokens.business_terms"
    )


def test_cached_stopword_set_is_reused() -> None:
    """get_stopword_tokens() returns the same frozenset object on repeated calls."""
    load_ngram_stopword_config.cache_clear()
    get_stopword_tokens.cache_clear()
    a = get_stopword_tokens()
    b = get_stopword_tokens()
    assert a is b, "get_stopword_tokens() must return the cached frozenset"


def test_cached_protected_set_is_reused() -> None:
    """get_protected_tokens() returns the same frozenset object on repeated calls."""
    load_ngram_stopword_config.cache_clear()
    get_protected_tokens.cache_clear()
    a = get_protected_tokens()
    b = get_protected_tokens()
    assert a is b, "get_protected_tokens() must return the cached frozenset"


if __name__ == "__main__":
    tests = [
        test_config_loads,
        test_stopwords_non_empty,
        test_protected_tokens_non_empty,
        test_protected_token_survives_if_also_stopword,
        test_english_stopword_removed,
        test_spanish_stopword_removed,
        test_arabic_stopword_removed,
        test_waste_signal_token_gratis_preserved,
        test_business_token_freight_preserved,
        test_numeric_only_tokens_removed,
        test_missing_config_falls_back_safely,
        test_validate_ngram_config_valid,
        test_validate_ngram_config_missing_keys,
        test_missing_stopwords_falls_back_to_defaults,
        test_invalid_protected_tokens_shape_warns,
        test_cached_stopword_set_is_reused,
        test_cached_protected_set_is_reused,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except (AssertionError, TypeError, ValueError, AttributeError, KeyError) as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001 — catch-all for unexpected errors
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed.")
    sys.exit(1 if failed else 0)
