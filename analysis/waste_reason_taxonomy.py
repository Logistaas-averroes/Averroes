"""
analysis/waste_reason_taxonomy.py

PR-ADS-153D — ONE waste-reason vocabulary, shared by Search Terms, the Action
Queue and any report that names why a search term was flagged.

Why this module exists
----------------------
The raw reason values are the ``junk_category`` keys written by the weekly waste
detection run, which come from ``config/junk_patterns.yaml``. Before this PR the
consuming surfaces each re-interpreted those raw keys inline:

  * ``_QUEUE_FRAUD_CATEGORIES`` in the API tested for ``"fraud"``, a value the
    rules file never emits (it emits ``fraud_indicators``), so the Action Queue's
    fraud escalation silently never fired;
  * the frontend carried its own label map, so a rules-file change renamed a
    category in one place and not the other.

Centralising the vocabulary here makes the raw → business-category mapping one
auditable fact. It maps; it never invents a reason and never edits the rules
file.

Doctrine
--------
Raw evidence is PRESERVED. Every classification returns the raw value alongside
the mapped category, so a reviewer can always see what the rule actually matched.

An unrecognised raw value maps to ``unmapped`` — a first-class, VISIBLE state —
never silently to ``other``. "We have never seen this reason before" and "the
rule deliberately said other" are different facts, and collapsing them would
hide a rules-file change from the people reviewing its output (§14).

Purity
------
No DB, no network, no YAML read at import. Deterministic.
"""

from __future__ import annotations

# Bump when the mapping changes so stored/reported categories stay auditable.
REASON_RULE_VERSION = "v1"

# ── Canonical business categories (stable identifiers) ───────────────────────
REASON_IRRELEVANT_INTENT = "irrelevant_intent"
REASON_JOB_SEEKER = "job_seeker"
REASON_CONSUMER_B2C = "consumer_b2c_intent"
REASON_WRONG_GEOGRAPHY = "wrong_geography"
REASON_WRONG_PRODUCT = "wrong_product_service"
REASON_LOW_COMMERCIAL_INTENT = "low_commercial_intent"
REASON_REPEATED_SPEND_NO_OUTCOME = "repeated_spend_without_qualified_outcome"
REASON_MANUAL_REVIEW_FLAG = "manual_review_flag"
REASON_OTHER = "other"
REASON_UNMAPPED = "unmapped"

REASON_LABELS = {
    REASON_IRRELEVANT_INTENT: "Irrelevant intent",
    REASON_JOB_SEEKER: "Job seeker",
    REASON_CONSUMER_B2C: "Consumer / B2C intent",
    REASON_WRONG_GEOGRAPHY: "Wrong geography",
    REASON_WRONG_PRODUCT: "Wrong product / service",
    REASON_LOW_COMMERCIAL_INTENT: "Low commercial intent",
    REASON_REPEATED_SPEND_NO_OUTCOME: "Repeated spend without qualified outcome",
    REASON_MANUAL_REVIEW_FLAG: "Manual review flag",
    REASON_OTHER: "Other",
    REASON_UNMAPPED: "Unmapped reason — needs taxonomy review",
}

ALL_REASONS = tuple(REASON_LABELS.keys())

# ── Raw ``junk_category`` → canonical category ───────────────────────────────
# Keys are the category names emitted by config/junk_patterns.yaml. Each mapping
# below is justified by that file's own ``description`` field, quoted inline, so
# the mapping can be re-checked against the rules without leaving this module.
_RAW_TO_REASON = {
    # "People looking for employment, not buying software"
    "job_seeker": REASON_JOB_SEEKER,
    # "Students and learners, not buyers" — an individual, not a business buyer.
    "student": REASON_CONSUMER_B2C,
    # "Users seeking free or zero-cost solutions" — real product interest, no
    # commercial intent. Three language variants, one business meaning.
    "free_intent_english": REASON_LOW_COMMERCIAL_INTENT,
    "free_intent_spanish": REASON_LOW_COMMERCIAL_INTENT,
    "free_intent_arabic": REASON_LOW_COMMERCIAL_INTENT,
    # "Shippers and retailers — wrong persona for Logistaas"
    "shipper_intent": REASON_WRONG_PRODUCT,
    # "Fraud and bot signals — confirmed in live account data" — not a buyer of
    # anything we sell, and the strongest irrelevance signal the rules produce.
    "fraud_indicators": REASON_IRRELEVANT_INTENT,
    # "Research queries with no purchase intent"
    "informational": REASON_LOW_COMMERCIAL_INTENT,
    # "Industry category research, not product purchase intent"
    "informational_industry": REASON_LOW_COMMERCIAL_INTENT,
    # A human put the flag there directly rather than a rule matching.
    "manual": REASON_MANUAL_REVIEW_FLAG,
    "manual_review": REASON_MANUAL_REVIEW_FLAG,
    "other": REASON_OTHER,
}

# Raw categories whose intent is disqualifying on its face. Used ONLY to explain
# priority (§19) — never to apply anything to Google Ads. Replaces the API's
# ``_QUEUE_FRAUD_CATEGORIES``, whose "fraud" key the rules file never emits.
CLEAR_DISQUALIFYING_REASONS = frozenset({
    REASON_IRRELEVANT_INTENT,
    REASON_JOB_SEEKER,
    REASON_CONSUMER_B2C,
    REASON_WRONG_PRODUCT,
})


def normalize_raw_reason(value) -> str:
    """Comparison form of a raw ``junk_category``: trimmed, lowercased,
    hyphens/spaces treated as underscores. Returns "" for None/blank."""
    if value is None:
        return ""
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return "_".join(part for part in text.split("_") if part)


def classify_reason(raw_category) -> dict:
    """Map one raw ``junk_category`` to the canonical vocabulary.

    Returns ``{reason, reason_label, raw_reason, mapped, unmapped}``:

      * a known raw value  → its business category, ``mapped=True``
      * a missing value    → ``unmapped`` (there is no reason on record, which is
                             not the same as a reason of "other")
      * an unknown value   → ``unmapped``, ``mapped=False``, raw preserved

    ``unmapped`` is deliberately visible so a rules-file change that introduces a
    new category surfaces in the UI instead of hiding inside "Other".
    """
    raw = normalize_raw_reason(raw_category)
    reason = _RAW_TO_REASON.get(raw)
    if reason is None:
        return {
            "reason": REASON_UNMAPPED,
            "reason_label": REASON_LABELS[REASON_UNMAPPED],
            "raw_reason": (str(raw_category) if raw_category not in (None, "") else None),
            "mapped": False,
            "unmapped": True,
        }
    return {
        "reason": reason,
        "reason_label": REASON_LABELS[reason],
        "raw_reason": str(raw_category),
        "mapped": True,
        "unmapped": False,
    }


def classify_reasons(raw_categories) -> list[dict]:
    """Classify a set of raw categories, de-duplicated and stably ordered.

    A canonical unit can carry several raw categories (different underlying fact
    rows matched different rules). All are kept — the strongest is never allowed
    to erase the others.
    """
    seen: dict = {}
    for raw in (raw_categories or []):
        classified = classify_reason(raw)
        key = (classified["reason"], classified["raw_reason"])
        seen.setdefault(key, classified)
    return sorted(seen.values(),
                  key=lambda c: (c["reason"], c["raw_reason"] or ""))


def primary_reason(raw_categories) -> dict:
    """The single reason to show when there is room for only one.

    A clearly-disqualifying reason outranks a soft one; an unmapped value
    outranks nothing but is never hidden by one. With no evidence at all the
    result is ``unmapped`` — never a fabricated "other".
    """
    classified = classify_reasons(raw_categories)
    if not classified:
        return classify_reason(None)
    for c in classified:
        if c["reason"] in CLEAR_DISQUALIFYING_REASONS:
            return c
    return classified[0]


def has_unmapped(raw_categories) -> bool:
    """Does this evidence contain a reason the taxonomy does not recognise?"""
    return any(c["unmapped"] for c in classify_reasons(raw_categories))


__all__ = [
    "REASON_RULE_VERSION",
    "ALL_REASONS",
    "REASON_LABELS",
    "CLEAR_DISQUALIFYING_REASONS",
    "REASON_IRRELEVANT_INTENT", "REASON_JOB_SEEKER", "REASON_CONSUMER_B2C",
    "REASON_WRONG_GEOGRAPHY", "REASON_WRONG_PRODUCT",
    "REASON_LOW_COMMERCIAL_INTENT", "REASON_REPEATED_SPEND_NO_OUTCOME",
    "REASON_MANUAL_REVIEW_FLAG", "REASON_OTHER", "REASON_UNMAPPED",
    "normalize_raw_reason", "classify_reason", "classify_reasons",
    "primary_reason", "has_unmapped",
]
