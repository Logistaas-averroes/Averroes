"""
analysis/ngrams.py

Pure read-only n-gram analysis helpers for the Logistaas Ads Intelligence System.

Phase 1.5 — Search-Term Intelligence (PR-ADS-055)
Read-only prototype. No DB writes. No external API calls. No FastAPI imports.

Responsibility:
  - Normalize raw search-term strings.
  - Detect dominant script / language of a search term.
  - Tokenize search terms into clean token lists.
  - Build unigrams, bigrams, and trigrams from token lists.
  - Aggregate per-ngram metrics across a list of search_terms rows.

Scope boundary:
  - Does not return attention_status, review_status, negative_candidate,
    recommendation, evidence_note, should_exclude, suggested_match_type,
    action, priority_score, or any scoring/classification field.
  - Does not create negative keyword candidates.
  - Does not write to DB.
  - Does not call Google Ads or HubSpot APIs.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import yaml


logger = logging.getLogger(__name__)

# Path to the stopword/protected-token config file (resolved relative to this module).
_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "ngram_stopwords.yaml"

# ---------------------------------------------------------------------------
# Default stopwords / protected tokens — used as safe fallback if config is
# missing or invalid.  These mirror the values in config/ngram_stopwords.yaml.
# ---------------------------------------------------------------------------

_DEFAULT_STOPWORDS: dict[str, set[str]] = {
    "english": {
        "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to",
        "for", "with", "by", "from", "as", "is", "are", "was", "were", "be",
        "it", "its", "this", "that", "these", "those", "my", "your", "our",
        "their", "his", "her", "we", "i", "you", "he", "she", "they",
        "do", "does", "did", "not", "no", "can", "will", "would", "should",
        "have", "has", "had", "been", "get", "got", "go", "going",
        "near",
    },
    "spanish": {
        "de", "la", "el", "en", "y", "a", "los", "las", "un", "una",
        "con", "por", "para", "que", "del", "al", "se", "su", "lo", "le",
        "es", "si", "ya", "hay", "mas", "como",
    },
    "arabic": {
        "من", "في", "على", "الى", "إلى", "عن", "مع", "و", "أو", "ال",
        "هذا", "هذه",
    },
}

_DEFAULT_PROTECTED_TOKENS: set[str] = {
    "freight", "forwarding", "logistics", "logistic", "shipping", "shipment",
    "cargo", "customs", "warehouse", "warehousing", "software", "system",
    "platform", "erp", "tms", "wms",
    "free", "gratis", "job", "jobs", "hiring", "vacancy", "vacancies",
    "student", "students", "course", "courses", "training",
    "certification", "certificate",
}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_ngram_stopword_config() -> dict:
    """Load n-gram stopword config from ``config/ngram_stopwords.yaml``.

    Falls back to safe defaults if the config file is missing or invalid.
    This function must never call external services.
    """
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.warning(
            "ngram_stopwords.yaml not found at %s — using built-in defaults",
            _CONFIG_PATH,
        )
        return _build_default_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to load ngram_stopwords.yaml (%s) — using built-in defaults", exc
        )
        return _build_default_config()

    warnings = validate_ngram_config(raw)
    if warnings:
        for w in warnings:
            logger.warning("ngram_stopwords.yaml validation: %s", w)
        # Validation warnings are non-fatal; continue with what we got.

    return raw if isinstance(raw, dict) else _build_default_config()


def _build_default_config() -> dict:
    """Return a config dict constructed from the built-in defaults."""
    return {
        "version": 1,
        "stopwords": {lang: list(words) for lang, words in _DEFAULT_STOPWORDS.items()},
        "protected_tokens": {
            "business_terms": [
                t for t in _DEFAULT_PROTECTED_TOKENS
                if t not in {
                    "free", "gratis", "job", "jobs", "hiring", "vacancy",
                    "vacancies", "student", "students", "course", "courses",
                    "training", "certification", "certificate",
                }
            ],
            "waste_signal_terms": [
                t for t in _DEFAULT_PROTECTED_TOKENS
                if t in {
                    "free", "gratis", "job", "jobs", "hiring", "vacancy",
                    "vacancies", "student", "students", "course", "courses",
                    "training", "certification", "certificate",
                }
            ],
        },
    }


def validate_ngram_config(config: dict) -> list[str]:
    """Validate the shape of a loaded ngram stopword config dict.

    Returns a list of warning/error strings (empty = valid).
    """
    warnings: list[str] = []

    if not isinstance(config, dict):
        return ["config root is not a dict"]

    if "version" not in config:
        warnings.append("missing 'version' key")

    stopwords = config.get("stopwords")
    if stopwords is None:
        warnings.append("missing 'stopwords' key")
    elif not isinstance(stopwords, dict):
        warnings.append("'stopwords' must be a dict of language → list")
    else:
        for lang, tokens in stopwords.items():
            if not isinstance(tokens, list):
                warnings.append(f"stopwords.{lang} must be a list")
            else:
                empty = [t for t in tokens if not str(t).strip()]
                if empty:
                    warnings.append(
                        f"stopwords.{lang} contains {len(empty)} empty token(s)"
                    )

    protected = config.get("protected_tokens")
    if protected is None:
        warnings.append("missing 'protected_tokens' key")
    elif not isinstance(protected, (dict, list)):
        warnings.append("'protected_tokens' must be a dict or list")

    return warnings


def get_stopword_tokens() -> set[str]:
    """Return the combined stopword set from all languages in config.

    Tokens are normalized via ``normalize_search_term`` where non-empty
    after normalization; empty results are discarded.
    """
    config = load_ngram_stopword_config()
    stopwords_cfg = config.get("stopwords") or {}
    result: set[str] = set()

    if isinstance(stopwords_cfg, dict):
        for tokens in stopwords_cfg.values():
            if isinstance(tokens, list):
                for tok in tokens:
                    normalized = normalize_search_term(str(tok))
                    if normalized:
                        result.add(normalized)
    return result


def get_protected_tokens() -> set[str]:
    """Return the combined protected-token set from config.

    Tokens are normalized via ``normalize_search_term``; empty results are
    discarded.  Protected tokens override stopwords in tokenization.
    """
    config = load_ngram_stopword_config()
    protected_cfg = config.get("protected_tokens") or {}
    result: set[str] = set()

    if isinstance(protected_cfg, dict):
        for tokens in protected_cfg.values():
            if isinstance(tokens, list):
                for tok in tokens:
                    normalized = normalize_search_term(str(tok))
                    if normalized:
                        result.add(normalized)
    elif isinstance(protected_cfg, list):
        for tok in protected_cfg:
            normalized = normalize_search_term(str(tok))
            if normalized:
                result.add(normalized)

    return result

# Arabic tatweel (U+0640) — decorative elongation character, no semantic value
_TATWEEL = "\u0640"

# Arabic diacritics (harakat) — U+064B through U+065F
_ARABIC_DIACRITICS_RE = re.compile(r"[\u064b-\u065f]")

# Arabic alef variants → plain alef (U+0627)
_ALEF_VARIANTS_RE = re.compile(r"[\u0622\u0623\u0625\u0671]")

# Keep only Arabic letters, ASCII Latin letters, ASCII digits, and spaces.
# Accented Latin characters are normalized to ASCII via NFKD before this step,
# so this pattern is applied only after accent stripping.
_KEEP_CHARS_RE = re.compile(r"[^\u0600-\u06ff\u0750-\u077fa-zA-Z0-9 ]")

# Arabic script detection: any character in the main Arabic Unicode block
_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f]")

# Spanish orthographic markers
_SPANISH_MARKERS_RE = re.compile(r"[ñÑ¿¡áéíóúüÁÉÍÓÚÜ]")

# Common Spanish-domain terms (normalized, lowercase) that strongly signal Spanish
_SPANISH_DOMAIN_TERMS: frozenset[str] = frozenset({
    "gratis", "envio", "envios", "logistica", "sistema", "transporte",
    "gestion", "software", "empresa", "camion", "camiones", "embarque",
    "aduanas", "aduana", "almacen", "almacenes", "precio", "precios",
    "cotizacion", "cotizaciones", "carga", "consolidacion",
})

# Repeated-space normalizer
_MULTI_SPACE_RE = re.compile(r" {2,}")


# ---------------------------------------------------------------------------
# normalize_search_term
# ---------------------------------------------------------------------------

def normalize_search_term(text: str) -> str:
    """Return a normalized form of a raw search-term string.

    Steps (in order):
      1. Lowercase
      2. Strip leading/trailing whitespace
      3. Remove Arabic tatweel (U+0640)
      4. Remove Arabic diacritics (U+064B–U+065F)
      5. Normalize Arabic alef variants (أ إ آ ٱ → ا)
      6. Decompose Latin diacritics via NFKD and strip combining marks
         (á→a, é→e, ñ→n, ü→u, etc.). Arabic characters are unaffected
         because their combining marks were already removed in steps 3–5
         and Arabic base letters do not decompose in NFKD.
      7. Remove remaining non-letter/non-digit/non-space characters
         (preserves Arabic and ASCII-Latin letters, digits, spaces)
      8. Normalize repeated spaces to a single space
      9. Strip again
    """
    if not text:
        return ""
    text = text.lower().strip()
    # Arabic cleanup
    text = text.replace(_TATWEEL, "")
    text = _ARABIC_DIACRITICS_RE.sub("", text)
    text = _ALEF_VARIANTS_RE.sub("\u0627", text)  # ا
    # Latin accent normalization: NFKD decomposes e.g. á → a + combining-acute;
    # we then drop all combining characters, converting accented Latin letters to
    # their ASCII base forms.  Arabic base characters in NFKD are unchanged because
    # their diacritics were already stripped above.
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )
    text = _KEEP_CHARS_RE.sub(" ", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# detect_script_or_language
# ---------------------------------------------------------------------------

def detect_script_or_language(text: str) -> str:
    """Return a simple script/language label for a (normalized) search term.

    Returns one of:
      "arabic"           — text contains Arabic-script characters
      "spanish"          — text contains Spanish orthographic markers or
                           one or more Spanish-domain terms
      "english_or_latin" — everything else

    This is a lightweight first-pass heuristic, not a full language detector.
    """
    if not text:
        return "english_or_latin"

    if _ARABIC_SCRIPT_RE.search(text):
        return "arabic"

    if _SPANISH_MARKERS_RE.search(text):
        return "spanish"

    # Check for Spanish domain terms by splitting on whitespace
    tokens_set = set(text.split())
    if tokens_set & _SPANISH_DOMAIN_TERMS:
        return "spanish"

    return "english_or_latin"


# ---------------------------------------------------------------------------
# tokenize_search_term
# ---------------------------------------------------------------------------

def tokenize_search_term(text: str) -> list[str]:
    """Tokenize a search term into a clean list of meaningful tokens.

    Pipeline:
      1. Normalize via normalize_search_term
      2. Split on whitespace
      3. Exclude empty tokens
      4. Exclude numeric-only tokens  (e.g. "2024", "100")
      5. Exclude single-character tokens  (single Arabic/Latin char = noise)
      6. Exclude tokens longer than 30 characters  (URLs, hashes, garbage)
      7. Exclude stopwords (from config/ngram_stopwords.yaml), unless the
         token is also a protected token — protected tokens always survive.

    Business/waste-signal tokens (freight, gratis, free, job, jobs, student,
    training, logistics, shipping, software) are in the protected-token list
    and will always survive this filter even if accidentally listed under
    stopwords.
    """
    normalized = normalize_search_term(text)
    if not normalized:
        return []

    stopwords = get_stopword_tokens()
    protected = get_protected_tokens()

    tokens: list[str] = []
    for tok in normalized.split():
        if not tok:
            continue
        if tok.isdigit():
            continue
        if len(tok) <= 1:
            continue
        if len(tok) > 30:
            continue
        if tok in stopwords and tok not in protected:
            continue
        tokens.append(tok)

    return tokens


# ---------------------------------------------------------------------------
# build_ngrams
# ---------------------------------------------------------------------------

def build_ngrams(
    tokens: list[str],
    lengths: set[int],
) -> list[tuple[str, int]]:
    """Build n-grams from a token list for the requested lengths.

    Args:
        tokens:  Output of tokenize_search_term.
        lengths: Set of n values to generate. Only {1, 2, 3} are supported;
                 any other value is silently ignored.

    Returns:
        List of (phrase, n) tuples in phrase-order.  Duplicates within a
        single row are preserved so that callers can choose how to count.
    """
    valid_lengths = lengths & {1, 2, 3}
    result: list[tuple[str, int]] = []
    n_tokens = len(tokens)

    for n in sorted(valid_lengths):
        for i in range(n_tokens - n + 1):
            phrase = " ".join(tokens[i : i + n])
            result.append((phrase, n))

    return result


# ---------------------------------------------------------------------------
# aggregate_ngrams
# ---------------------------------------------------------------------------

def aggregate_ngrams(
    rows: list[dict],
    lengths: set[int],
) -> list[dict]:
    """Aggregate per-ngram factual metrics across a list of search_terms rows.

    Each element of ``rows`` is expected to be a dict with the following keys
    (matching the search_terms DB columns):

        search_term        str
        campaign_name      str | None
        ad_group           str | None
        keyword            str | None
        spend_usd          float | None
        clicks             int | None
        impressions        int | None
        conversions        float | None
        is_flagged_waste   bool | None  (tri-state: None=unanalyzed, True=waste, False=clean)

    Returns a list of dicts sorted by:
        total_spend_usd DESC → row_count DESC → ngram ASC

    Each output dict contains factual metrics only — no scoring, no
    attention_status, no recommendation, no negative_candidate fields.
    """
    # keyed by (ngram_phrase, n)
    agg: dict[tuple[str, int], dict] = defaultdict(lambda: {
        "row_count":              0,
        "search_term_set":        set(),
        "campaign_set":           set(),
        "ad_group_set":           set(),
        "keyword_set":            set(),
        "total_spend_usd":        0.0,
        "total_clicks":           0,
        "total_impressions":      0,
        "google_conversions":     0.0,
        "flagged_waste_rows":     0,
        "clean_rows":             0,
        "unanalyzed_rows":        0,
        "flagged_waste_spend":    0.0,
        # (search_term, spend_usd) pairs for sampling
        "_spend_terms":           [],
        # campaign list for sampling (in insertion order)
        "_campaign_list":         [],
    })

    for row in rows:
        raw_term: str = row.get("search_term") or ""
        if not raw_term:
            continue

        tokens = tokenize_search_term(raw_term)
        ngrams = build_ngrams(tokens, lengths)
        if not ngrams:
            continue

        # Pre-compute row-level values once
        spend     = float(row.get("spend_usd") or 0)
        clicks    = int(row.get("clicks") or 0)
        impressions = int(row.get("impressions") or 0)
        conversions = float(row.get("conversions") or 0)
        is_flagged  = row.get("is_flagged_waste")  # True | False | None
        campaign    = row.get("campaign_name") or ""
        ad_group    = row.get("ad_group") or ""
        keyword     = row.get("keyword") or ""

        seen_ngrams: set[tuple[str, int]] = set()
        for phrase, n in ngrams:
            key = (phrase, n)
            bucket = agg[key]

            # row_count increments once per row even if the ngram appears
            # multiple times within that row's token sequence
            if key not in seen_ngrams:
                seen_ngrams.add(key)
                bucket["row_count"] += 1
                bucket["total_spend_usd"]    += spend
                bucket["total_clicks"]       += clicks
                bucket["total_impressions"]  += impressions
                bucket["google_conversions"] += conversions

                if is_flagged is True:
                    bucket["flagged_waste_rows"] += 1
                    bucket["flagged_waste_spend"] += spend
                elif is_flagged is False:
                    bucket["clean_rows"] += 1
                else:
                    bucket["unanalyzed_rows"] += 1

                bucket["search_term_set"].add(raw_term)
                bucket["_spend_terms"].append((spend, raw_term))

                if campaign:
                    bucket["campaign_set"].add(campaign)
                    bucket["_campaign_list"].append(campaign)
                if ad_group:
                    bucket["ad_group_set"].add(ad_group)
                if keyword:
                    bucket["keyword_set"].add(keyword)

    # ── Build output rows ─────────────────────────────────────────────────
    out: list[dict] = []
    for (phrase, n), bucket in agg.items():
        total_spend  = round(bucket["total_spend_usd"], 2)
        total_clicks = bucket["total_clicks"]
        total_impr   = bucket["total_impressions"]
        google_conv  = round(bucket["google_conversions"], 2)

        avg_cpc = round(total_spend / total_clicks, 4) if total_clicks > 0 else 0.0
        ctr_pct = round(total_clicks / total_impr * 100, 4) if total_impr > 0 else 0.0
        conv_rate = (
            round(google_conv / total_clicks * 100, 4) if total_clicks > 0 else 0.0
        )

        # Search terms sample: up to 5, prefer highest spend
        spend_terms = sorted(bucket["_spend_terms"], key=lambda x: x[0], reverse=True)
        search_terms_sample = list(dict.fromkeys(t for _, t in spend_terms))[:5]

        # Campaigns sample: up to 5 unique campaign names (preserve first-seen order)
        campaigns_seen: list[str] = []
        for c in bucket["_campaign_list"]:
            if c not in campaigns_seen:
                campaigns_seen.append(c)
            if len(campaigns_seen) == 5:
                break

        language = detect_script_or_language(phrase)

        out.append({
            "ngram":                    phrase,
            "n":                        n,
            "language":                 language,
            "row_count":                bucket["row_count"],
            "unique_search_terms":      len(bucket["search_term_set"]),
            "campaigns_count":          len(bucket["campaign_set"]),
            "ad_groups_count":          len(bucket["ad_group_set"]),
            "keywords_count":           len(bucket["keyword_set"]),
            "total_spend_usd":          total_spend,
            "total_clicks":             total_clicks,
            "total_impressions":        total_impr,
            "google_conversions":       google_conv,
            "avg_cpc_usd":              avg_cpc,
            "ctr_pct":                  ctr_pct,
            "google_conversion_rate_pct": conv_rate,
            "flagged_waste_rows":       bucket["flagged_waste_rows"],
            "clean_rows":               bucket["clean_rows"],
            "unanalyzed_rows":          bucket["unanalyzed_rows"],
            "flagged_waste_spend_usd":  round(bucket["flagged_waste_spend"], 2),
            "campaigns_sample":         campaigns_seen,
            "search_terms_sample":      search_terms_sample,
        })

    # Sort: total_spend_usd DESC, row_count DESC, ngram ASC
    out.sort(key=lambda r: (-r["total_spend_usd"], -r["row_count"], r["ngram"]))
    return out
