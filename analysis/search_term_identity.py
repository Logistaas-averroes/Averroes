"""
analysis/search_term_identity.py

PR-ADS-153D — ONE canonical durable identity for a Google Ads search term.

Why this module exists
----------------------
Before this PR, three surfaces disagreed about what "a search term" is:

  * the Search Terms page merged canonical facts at
    ``(search_term, canonical campaign identity)`` — the right grain, but the key
    lived as a private helper inside the evidence service;
  * ``/api/waste`` grouped ``waste_terms`` rows by ``(search_term,
    campaign_name)`` per RUN, so one term observed by five weekly runs counted
    five times;
  * the Action Queue grouped by ``(search_term, campaign_name, junk_category)``,
    so a term whose junk category changed between runs produced two queue items
    for the same term.

This module promotes the ALREADY-CANONICAL Search Terms key into one shared,
documented contract. It defines no new taxonomy and weakens nothing: the grain
is exactly the grain the canonical evidence service has always merged at.

The durable identity
--------------------
    durable term identity := canonical campaign identity + normalized search term

``campaign_key`` is whatever the canonical campaign-identity contract resolved
(``services/search_term_evidence_service._resolve_campaign_identity``): the
Google Ads ``campaign_id`` when the campaign is mapped, otherwise a normalized
label key that is explicitly marked unmapped. It is NEVER a fuzzy campaign-name
match where a stronger id exists, so the same term text in two different
campaigns can never collide (PR-ADS-153D §24).

Why ad group is evidence, not identity
--------------------------------------
The canonical FACT key includes ad group, keyword and match type
(``idx_search_terms_unique_fact``) — that is the grain of one ingested row, and
it is what makes ingestion idempotent. The durable REVIEW identity is
deliberately coarser, because it is the thing a human reviews and acts on:

  * PR-ADS-153D §17 defines the action object as search term + campaign;
  * §18 and §44 require ONE durable term to produce ONE queue item.

Keying review state or queue items by ad group would split one human decision
about one query into several, which is exactly the duplication this PR removes.
Ad groups, keywords and match types are preserved as evidence WITHIN the unit and
surfaced in the drawer — no information is lost, it is simply not identity.

Storage
-------
``term_identity_key`` is a hex digest, not a delimiter-joined string: campaign
labels and user queries can contain any character (including the ``\\x00`` the
in-memory key used), and PostgreSQL ``TEXT`` cannot store NUL. Callers persist
the digest AND both readable components, so every stored row stays auditable.

Purity
------
No DB, no network. Every function here is deterministic.
"""

from __future__ import annotations

import hashlib
import unicodedata

# Bump when the identity rules change so stored identities stay auditable.
IDENTITY_RULE_VERSION = "v1"

# Sentinel campaign key used when a search-term fact carries no campaign
# evidence at all. Kept explicit so an identity is never silently built from a
# missing campaign — an unknown campaign is a NAMED unknown, not an empty string
# that would merge every campaign-less term into one bucket per term text.
CAMPAIGN_KEY_UNKNOWN = "unknown_campaign"


def normalize_search_term(value) -> str:
    """Canonical comparison form of a user query.

    Unicode NFKC, casefolded, whitespace collapsed. Punctuation is PRESERVED:
    "logistics software" and "logistics-software" are different queries that
    Google Ads reports separately and that a reviewer may judge differently, so
    collapsing them would merge two distinct facts.

    Returns "" for None/blank.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return " ".join(text.split())


def normalize_campaign_key(value) -> str:
    """Comparison form of an already-resolved canonical campaign key.

    The campaign key arrives from the canonical identity contract, so this only
    trims and casefolds it — it never re-derives, re-matches or guesses an
    identity from a campaign name.
    """
    if value is None:
        return CAMPAIGN_KEY_UNKNOWN
    text = str(value).strip()
    return text.casefold() if text else CAMPAIGN_KEY_UNKNOWN


def term_identity_key(campaign_key, search_term) -> str:
    """The durable identity of one reviewable search term, as a stable digest.

    Deterministic across processes and releases: the same
    (campaign identity, query) always yields the same key, and two different
    pairs never collide on it. Length-prefixed encoding means
    ``("ab", "c")`` and ``("a", "bc")`` cannot produce the same digest.
    """
    campaign = normalize_campaign_key(campaign_key)
    term = normalize_search_term(search_term)
    payload = f"{IDENTITY_RULE_VERSION}|{len(campaign)}:{campaign}|{len(term)}:{term}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def identity_components(campaign_key, search_term) -> dict:
    """Digest plus the readable components, for durable storage and auditing.

    Persisting the components alongside the digest is what keeps a stored review
    decision explainable years later, and what lets a human verify the join
    without recomputing a hash.
    """
    return {
        "term_identity": term_identity_key(campaign_key, search_term),
        "campaign_key": normalize_campaign_key(campaign_key),
        "search_term_normalized": normalize_search_term(search_term),
        "identity_rule_version": IDENTITY_RULE_VERSION,
    }


def unit_identity(unit: dict) -> str:
    """Durable identity of a canonical Search Terms evidence unit.

    ``unit`` is a merged unit from ``search_term_evidence_service`` — the same
    object the page renders — so the page row, the review record and the Action
    Queue item provably describe one thing.
    """
    return term_identity_key(unit.get("campaign_key"), unit.get("search_term"))


__all__ = [
    "IDENTITY_RULE_VERSION",
    "CAMPAIGN_KEY_UNKNOWN",
    "normalize_search_term",
    "normalize_campaign_key",
    "term_identity_key",
    "identity_components",
    "unit_identity",
]
