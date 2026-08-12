"""
analysis/mql_status_taxonomy.py

PR-ADS-153B — the ONE mapping of the HubSpot ``mql_status`` property.

Doctrine
--------
``mql_status`` is an OPERATIONAL WORKFLOW dimension inside the MQL process. It is
NOT the definition of any funnel stage. The canonical funnel is HubSpot Lifecycle
Stage (see ``analysis/crm_lifecycle.py``); this module only classifies what the
MDR team recorded about a contact while working it.

Before PR-ADS-153B the mapping was duplicated across ``db/writers.py``,
``analysis/core.py``, the waste logic and the UI, and the four copies disagreed
(``CLOSED - Bad Product Fit`` was junk in one place and wrong-fit in another).
This module is now the single source of truth; every consumer imports it.

Two distinct absences are never merged:

  ``no_verdict``  the property is null/blank — the MDR has not recorded anything.
  ``unmapped``    a non-null value Averroes does not know — a NEW production
                  value that must surface as an audit warning, never be silently
                  normalised into a working category.

Free text can no longer reach the typed property (PR-ADS-153B §15 removed the
``mql_status or mql___mdr_comments`` fallback), but historical rows may still
carry it, so ``looks_like_free_text`` lets the audit quantify the pollution
without rewriting evidence.

This module is pure: no I/O, no database, no HubSpot calls.
"""

from __future__ import annotations

# Bump when the mapping below changes so persisted classifications stay auditable.
MQL_STATUS_RULE_VERSION = "v1"

# ── Operational categories ───────────────────────────────────────────────────
CATEGORY_OPEN_WORKING = "open_working"
CATEGORY_SALES_QUALIFIED_SIGNAL = "sales_qualified_signal"
CATEGORY_DEAL_CREATED_SIGNAL = "deal_created_signal"
CATEGORY_DISQUALIFIED = "disqualified"
CATEGORY_BAD_FIT = "bad_fit"
CATEGORY_CONTACT_QUALITY = "contact_quality"
CATEGORY_NO_RESPONSE = "no_response"
CATEGORY_DISCARDED = "discarded"
CATEGORY_RESELLER = "reseller"

# Absence vocabulary — deliberately distinct (PR-ADS-153B §17).
CATEGORY_NO_VERDICT = "no_verdict"
CATEGORY_UNMAPPED = "unmapped"

OPERATIONAL_CATEGORIES = (
    CATEGORY_OPEN_WORKING,
    CATEGORY_SALES_QUALIFIED_SIGNAL,
    CATEGORY_DEAL_CREATED_SIGNAL,
    CATEGORY_DISQUALIFIED,
    CATEGORY_BAD_FIT,
    CATEGORY_CONTACT_QUALITY,
    CATEGORY_NO_RESPONSE,
    CATEGORY_DISCARDED,
    CATEGORY_RESELLER,
)

ALL_CATEGORIES = OPERATIONAL_CATEGORIES + (CATEGORY_NO_VERDICT, CATEGORY_UNMAPPED)

CATEGORY_LABELS = {
    CATEGORY_OPEN_WORKING: "Open — being worked",
    CATEGORY_SALES_QUALIFIED_SIGNAL: "Sales-qualified signal",
    CATEGORY_DEAL_CREATED_SIGNAL: "Deal-created signal",
    CATEGORY_DISQUALIFIED: "Sales disqualified",
    CATEGORY_BAD_FIT: "Bad product fit",
    CATEGORY_CONTACT_QUALITY: "Contact quality",
    CATEGORY_NO_RESPONSE: "No response",
    CATEGORY_DISCARDED: "Discarded",
    CATEGORY_RESELLER: "Reseller",
    CATEGORY_NO_VERDICT: "No verdict yet",
    CATEGORY_UNMAPPED: "Unmapped value",
}

# ── Raw HubSpot values → operational category ────────────────────────────────
# Verified against the live portal enumeration. ``DICARDED`` is the real HubSpot
# INTERNAL value (one R); its display label is "DISCARDED". Preserve the internal
# spelling exactly — see docs/05_DATA_REFERENCE.md.
MQL_STATUS_CATEGORY = {
    "Open": CATEGORY_OPEN_WORKING,
    "OPEN - Connecting": CATEGORY_OPEN_WORKING,
    "OPEN - Pending Meeting": CATEGORY_OPEN_WORKING,
    "OPEN - Meeting Booked": CATEGORY_OPEN_WORKING,
    "Closed": CATEGORY_NO_RESPONSE,
    "CLOSED - Job Seeker": CATEGORY_CONTACT_QUALITY,
    "CLOSED - Bad Contact": CATEGORY_CONTACT_QUALITY,
    "CLOSED - Bad Product Fit": CATEGORY_BAD_FIT,
    "CLOSED - No Response": CATEGORY_NO_RESPONSE,
    "CLOSED - Sales Qualified": CATEGORY_SALES_QUALIFIED_SIGNAL,
    "CLOSED - Sales Disqualified": CATEGORY_DISQUALIFIED,
    "CLOSED - Deal Created": CATEGORY_DEAL_CREATED_SIGNAL,
    "Other": CATEGORY_UNMAPPED,
    "DICARDED": CATEGORY_DISCARDED,
    "RESELLER": CATEGORY_RESELLER,
}

# ``Closed`` and ``Other`` are bare parent values HubSpot exposes alongside the
# specific ones. ``Other`` carries no operational meaning, so it is deliberately
# mapped to ``unmapped`` — it must surface in the audit rather than masquerade as
# a verdict.

KNOWN_MQL_STATUS_VALUES = frozenset(MQL_STATUS_CATEGORY)

# Case-insensitive lookup so a portal casing change does not silently unmap a
# known value. The canonical spelling is still what gets persisted.
_NORMALISED_LOOKUP = {k.strip().lower(): k for k in MQL_STATUS_CATEGORY}


def normalize_mql_status(value) -> str | None:
    """Trim a raw ``mql_status``; returns None for null/blank.

    The stored value is never rewritten to a different spelling — this only
    strips surrounding whitespace.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def canonical_mql_status(value) -> str | None:
    """Resolve a raw value to its canonical HubSpot spelling when recognised.

    Returns the input (trimmed) when unrecognised, or None when blank.
    """
    text = normalize_mql_status(value)
    if text is None:
        return None
    return _NORMALISED_LOOKUP.get(text.lower(), text)


def classify_mql_status(value) -> str:
    """Classify a raw ``mql_status`` into ONE operational category.

    Null/blank → ``no_verdict``.
    Non-null but unrecognised → ``unmapped`` (an audit warning, never a verdict).
    """
    text = normalize_mql_status(value)
    if text is None:
        return CATEGORY_NO_VERDICT
    known = _NORMALISED_LOOKUP.get(text.lower())
    if known is None:
        return CATEGORY_UNMAPPED
    return MQL_STATUS_CATEGORY[known]


def is_mapped(value) -> bool:
    """True when a non-null value is one Averroes knows.

    Null/blank returns False — it is ``no_verdict``, which is not "mapped" but is
    also not an audit warning. Use ``classify_mql_status`` to distinguish.
    """
    text = normalize_mql_status(value)
    if text is None:
        return False
    return text.lower() in _NORMALISED_LOOKUP


# ── Free-text pollution detection (historical rows only) ─────────────────────
# PR-ADS-153B §15: ``mql_status`` must contain ONLY the HubSpot property. Legacy
# rows written by the removed ``mql_status or mql___mdr_comments`` fallback can
# still hold MDR prose. These are detected and reported, never auto-rewritten.
_FREE_TEXT_MAX_LEN = 40
_FREE_TEXT_MARKERS = (".", "!", "?", ",", ";", ":")


def looks_like_free_text(value) -> bool:
    """Heuristic: does this unmapped value look like MDR prose rather than a status?

    Only meaningful for values that are already ``unmapped`` — a known status is
    never free text. Conservative by design: it flags for audit, and nothing in
    the system deletes or rewrites a row because of it.
    """
    text = normalize_mql_status(value)
    if text is None or is_mapped(text):
        return False
    if len(text) > _FREE_TEXT_MAX_LEN:
        return True
    if any(marker in text for marker in _FREE_TEXT_MARKERS):
        return True
    # A status value is a short label; prose runs longer.
    return len(text.split()) > 5


def describe(value) -> dict:
    """Full classification of one raw value — the shape used by audits and APIs."""
    text = normalize_mql_status(value)
    category = classify_mql_status(text)
    return {
        "raw": text,
        "canonical": canonical_mql_status(text),
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "is_mapped": is_mapped(text),
        "is_no_verdict": category == CATEGORY_NO_VERDICT,
        "is_unmapped": category == CATEGORY_UNMAPPED,
        "looks_like_free_text": looks_like_free_text(text),
        "rule_version": MQL_STATUS_RULE_VERSION,
    }
