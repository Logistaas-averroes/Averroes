"""
analysis/revenue_scope.py

PR-ADS-153E-B — the explicit attribution-scope lattice for canonical revenue.

Why a lattice exists at all
---------------------------
Before this module, every page decided for itself which deals "counted". The
Dashboard read ``gclid_attribution`` (GCLID-bearing deals only), Revenue by
Source read ``deal_source_attribution`` (all closed-won deals), and Unit
Economics read a local Windsor JSON file. All three called their output
"revenue", so the same quarter produced three different totals and no page
said which population it was describing.

The fix is not to force every page to the same number — a campaign ROAS view
legitimately shows LESS revenue than the company total. The fix is to make the
population an explicit, named, ordered part of every answer:

    same metric + same business window + same SCOPE = same result, everywhere.

The lattice
-----------
Four scopes, ordered from the widest population to the narrowest::

    all_source  ⊇  google_ads_source  ⊇  campaign_attributable  ⊇  gclid_attributable

Membership is NESTED BY CONSTRUCTION: each level re-tests the level above it and
adds one predicate. That ordering is therefore not a convention this module
hopes callers respect — a deal cannot enter a narrower scope without already
being in every wider one, so a campaign-level total can never exceed the
company-wide total that contains it. ``check_lattice`` re-proves the ordering on
computed aggregates so a regression anywhere upstream fails loudly.

Doctrine this encodes (docs/35_CANONICAL_REVENUE_LEDGER.md)
-----------------------------------------------------------
* Google Ads owns spend, clicks, impressions, search terms and campaign
  identifiers. It does NOT own the population of won deals: 124 of the 180 won
  deals in the production ledger have no GCLID at all. Any page that derived
  "total revenue" from GCLID evidence was reporting roughly a third of the
  business and calling it all of it.
* HubSpot, through the canonical deal ledger, owns won status, close date and
  revenue.
* Ambiguous attribution is NOT attribution. A deal whose contacts disagree about
  the acquisition source stays in ``all_source`` and enters no narrower scope;
  it is reported as a coverage gap rather than assigned to whichever contact the
  API happened to return first.

Purity
------
No DB, no network, no window logic, no currency logic. This module answers one
question about one already-canonical row: which scopes does this deal belong to?
"""

from __future__ import annotations

from analysis.source_classification import (
    ATTR_ATTRIBUTED,
    GROUP_GOOGLE_ADS,
)

SCOPE_RULE_VERSION = "v1"

# ── The scopes, widest first ─────────────────────────────────────────────────
SCOPE_ALL_SOURCE = "all_source"
SCOPE_GOOGLE_ADS_SOURCE = "google_ads_source"
SCOPE_CAMPAIGN_ATTRIBUTABLE = "campaign_attributable"
SCOPE_GCLID_ATTRIBUTABLE = "gclid_attributable"

# Index 0 is the widest population; each subsequent entry is a strict subset of
# the one before it. Order is load-bearing — `check_lattice` walks this list.
SCOPE_ORDER = (
    SCOPE_ALL_SOURCE,
    SCOPE_GOOGLE_ADS_SOURCE,
    SCOPE_CAMPAIGN_ATTRIBUTABLE,
    SCOPE_GCLID_ATTRIBUTABLE,
)

SCOPE_LABELS = {
    SCOPE_ALL_SOURCE: "All sources",
    SCOPE_GOOGLE_ADS_SOURCE: "Google Ads source",
    SCOPE_CAMPAIGN_ATTRIBUTABLE: "Campaign-attributable",
    SCOPE_GCLID_ATTRIBUTABLE: "GCLID-attributable",
}

SCOPE_DESCRIPTIONS = {
    SCOPE_ALL_SOURCE: (
        "Every won deal in the window, regardless of advertising attribution. "
        "This is the company-wide business population and the only scope that "
        "may be presented as total revenue or total customers."
    ),
    SCOPE_GOOGLE_ADS_SOURCE: (
        "Won deals with unambiguous Google Ads acquisition evidence — a "
        "Google Ads source classification or a durable GCLID."
    ),
    SCOPE_CAMPAIGN_ATTRIBUTABLE: (
        "Google Ads deals that also carry a usable campaign identifier, so "
        "they can be placed on a campaign row."
    ),
    SCOPE_GCLID_ATTRIBUTABLE: (
        "Campaign-attributable deals that also carry a GCLID — the narrowest "
        "and highest-confidence advertising attribution evidence."
    ),
}

DEFAULT_SCOPE = SCOPE_ALL_SOURCE

# The ONLY scope that may be presented as a company-wide business total. Every
# other scope describes an advertising subset and must be labelled as one.
BUSINESS_TOTAL_SCOPE = SCOPE_ALL_SOURCE

# HubSpot traffic-source pseudo-names. They are recorded as the campaign field
# but identify no Google Ads campaign, so they cannot make a deal
# campaign-attributable. Mirrors `db.revenue_repository._PSEUDO_CAMPAIGNS`.
PSEUDO_CAMPAIGN_NAMES = frozenset({
    "(direct)", "(organic)", "(referral)", "(not set)",
    "(cross-network)", "(none)", "(content)", "(social)",
})


class UnknownScopeError(ValueError):
    """Raised for a scope name outside the lattice.

    Deliberately an error rather than a silent fall back to ``all_source``: a
    typo'd scope must not quietly widen an advertising view into the whole
    business.
    """


def is_valid_scope(scope) -> bool:
    return scope in SCOPE_LABELS


def normalize_scope(scope) -> str:
    """Return a known scope name, or raise ``UnknownScopeError``."""
    if scope is None:
        return DEFAULT_SCOPE
    text = str(scope).strip().lower()
    if not is_valid_scope(text):
        raise UnknownScopeError(
            f"unknown revenue scope '{scope}'. Valid scopes: "
            + ", ".join(SCOPE_ORDER)
        )
    return text


def scope_rank(scope) -> int:
    """Position in the lattice: 0 = widest (``all_source``)."""
    return SCOPE_ORDER.index(normalize_scope(scope))


def is_narrower_or_equal(scope, other) -> bool:
    """True when ``scope`` describes a subset of ``other``'s population."""
    return scope_rank(scope) >= scope_rank(other)


# ── Per-deal evidence predicates ─────────────────────────────────────────────
def _text(value) -> str:
    return "" if value is None else str(value).strip()


def has_gclid(row) -> bool:
    """A durable Google Ads click identifier is present."""
    return bool(_text(row.get("gclid")))


def has_campaign(row) -> bool:
    """A usable Google Ads campaign identifier is present.

    HubSpot pseudo-campaigns such as ``(direct)`` are stored in the same field
    and name no campaign, so they are not campaign evidence.
    """
    name = _text(row.get("campaign_name_raw") or row.get("campaign_name"))
    return bool(name) and name.lower() not in PSEUDO_CAMPAIGN_NAMES


def is_google_ads_attributed(row) -> bool:
    """Unambiguous Google Ads acquisition evidence.

    Two independent proofs are accepted, either of which is sufficient:

    * the deal's contacts agree on the Google Ads acquisition group
      (``attribution_status == 'attributed'``), or
    * the deal carries a GCLID, which is a direct record of a Google Ads click
      and does not depend on the contact-source classification at all.

    ``ambiguous`` is NOT accepted. Conflicting contact evidence means we do not
    know the source; it stays in ``all_source`` and is reported as a gap.
    """
    if has_gclid(row):
        return True
    return (
        row.get("acquisition_group") == GROUP_GOOGLE_ADS
        and row.get("attribution_status") == ATTR_ATTRIBUTED
    )


def deal_in_scope(row, scope) -> bool:
    """Does this canonical ledger row belong to ``scope``?

    Nested by construction — each level re-tests the level above it — so the
    lattice ordering holds for any row set without a separate check.
    """
    scope = normalize_scope(scope)
    if scope == SCOPE_ALL_SOURCE:
        return True
    if not is_google_ads_attributed(row):
        return False
    if scope == SCOPE_GOOGLE_ADS_SOURCE:
        return True
    if not has_campaign(row):
        return False
    if scope == SCOPE_CAMPAIGN_ATTRIBUTABLE:
        return True
    return has_gclid(row)


def narrowest_scope(row) -> str:
    """The narrowest scope this deal qualifies for (always at least all_source)."""
    result = SCOPE_ALL_SOURCE
    for scope in SCOPE_ORDER:
        if deal_in_scope(row, scope):
            result = scope
        else:
            break
    return result


def filter_deals(rows, scope) -> list:
    """Every row in ``rows`` that belongs to ``scope``, order preserved."""
    scope = normalize_scope(scope)
    return [r for r in (rows or []) if deal_in_scope(r, scope)]


def scope_evidence_gaps(rows) -> dict:
    """Deals whose evidence is inconsistent with the nesting, counted honestly.

    A deal can carry a GCLID and still have no usable campaign name. Nesting
    keeps it out of ``campaign_attributable`` (it cannot be placed on a campaign
    row) and therefore out of ``gclid_attributable`` too. That is the correct
    ROAS behaviour, but it is a real evidence gap and must be visible rather
    than silently absorbed — a page showing GCLID ROAS should be able to say how
    many click-attributed deals it could not place.
    """
    gclid_without_campaign = 0
    ambiguous_attribution = 0
    for row in rows or []:
        if has_gclid(row) and not has_campaign(row):
            gclid_without_campaign += 1
        if row.get("attribution_status") == "ambiguous":
            ambiguous_attribution += 1
    return {
        "gclid_without_campaign": gclid_without_campaign,
        "ambiguous_attribution": ambiguous_attribution,
    }


def check_lattice(counts_by_scope) -> list:
    """Re-prove ``all_source ≥ google_ads_source ≥ campaign ≥ gclid`` on totals.

    ``deal_in_scope`` makes the ordering true by construction, so a violation
    here means an aggregate was assembled from something other than one scoped
    read of one canonical row set — exactly the class of defect this PR exists
    to remove. Returns a list of human-readable violations; empty means the
    ordering holds.

    Missing scopes are skipped, not treated as zero: a caller that computed only
    two scopes is not asserting the other two are empty.
    """
    violations = []
    present = [(s, counts_by_scope.get(s)) for s in SCOPE_ORDER
               if counts_by_scope.get(s) is not None]
    for (wider, wider_value), (narrower, narrower_value) in zip(present, present[1:]):
        try:
            if float(narrower_value) > float(wider_value):
                violations.append(
                    f"{narrower} ({narrower_value}) exceeds {wider} "
                    f"({wider_value}) — a narrower attribution scope cannot "
                    "contain more than the population it is a subset of")
        except (TypeError, ValueError):
            violations.append(
                f"non-numeric scope total for {narrower}/{wider}: "
                f"{narrower_value!r}/{wider_value!r}")
    return violations


def scope_descriptor(scope) -> dict:
    """The scope block every revenue response carries."""
    scope = normalize_scope(scope)
    return {
        "scope": scope,
        "scope_label": SCOPE_LABELS[scope],
        "scope_description": SCOPE_DESCRIPTIONS[scope],
        "scope_rank": scope_rank(scope),
        "scope_rule_version": SCOPE_RULE_VERSION,
        "is_business_total": scope == BUSINESS_TOTAL_SCOPE,
    }


__all__ = [
    "SCOPE_RULE_VERSION",
    "SCOPE_ALL_SOURCE", "SCOPE_GOOGLE_ADS_SOURCE",
    "SCOPE_CAMPAIGN_ATTRIBUTABLE", "SCOPE_GCLID_ATTRIBUTABLE",
    "SCOPE_ORDER", "SCOPE_LABELS", "SCOPE_DESCRIPTIONS",
    "DEFAULT_SCOPE", "BUSINESS_TOTAL_SCOPE", "PSEUDO_CAMPAIGN_NAMES",
    "UnknownScopeError",
    "is_valid_scope", "normalize_scope", "scope_rank", "is_narrower_or_equal",
    "has_gclid", "has_campaign", "is_google_ads_attributed",
    "deal_in_scope", "narrowest_scope", "filter_deals",
    "scope_evidence_gaps", "check_lattice", "scope_descriptor",
]
