"""
analysis/deal_truth.py

PR-ADS-153E-A — the canonical won predicate and the ONE deal→contact resolver.

Two decisions live here, both previously made inconsistently in several places:

1. **Is this deal won?**  ``hs_is_closed_won`` and nothing else.
2. **Which contact represents this deal, and is its attribution trustworthy?**
   One resolver, shared by GCLID, source, campaign and country attribution.

Why the won predicate moved here
--------------------------------
PR-ADS-153A §9.1 found three different won rules in the codebase, none of them
authoritative:

  * ingest filtered on a hardcoded stage id (``dealstage IN ["326093516"]``);
  * reads used ``deal_stage = '326093516' OR deal_stage_label ILIKE '%won%'``;
  * an unknown stage id was LABELLED ``"Deal Won / Payment Received"`` by
    default, so combined with the ILIKE fallback an unrecognised stage that
    slipped past the filter would silently count as revenue.

``hs_is_closed_won`` is HubSpot's own boolean and is the only thing that decides.
Stage labels are display evidence. A missing boolean FAILS CLOSED: absence of
proof that a deal is won is not proof that it is.

Why one resolver
----------------
The same deal could bucket differently on two pages because ledger A took
``results[0]`` (the arbitrary first association) while ledger B took all contacts
and could return ``ambiguous``. Attribution that disagrees with itself is not
attribution. Every attribution dimension now flows from this one decision.

Purity
------
No DB, no network. Deterministic — the same associations always resolve the same
way, so a re-sync can never quietly move a deal between campaigns.
"""

from __future__ import annotations

DEAL_TRUTH_RULE_VERSION = "v1"

# ── Association statuses ─────────────────────────────────────────────────────
ASSOC_RESOLVED = "resolved"          # exactly one trustworthy primary contact
ASSOC_AMBIGUOUS = "ambiguous"        # several contacts, conflicting evidence
ASSOC_NONE = "none"                  # lookup SUCCEEDED and found no contacts
ASSOC_LOOKUP_FAILED = "lookup_failed"  # lookup FAILED — we know nothing

ALL_ASSOCIATION_STATUSES = (
    ASSOC_RESOLVED, ASSOC_AMBIGUOUS, ASSOC_NONE, ASSOC_LOOKUP_FAILED,
)

# ── Attribution statuses (aligned with analysis/source_classification.py) ────
ATTR_ATTRIBUTED = "attributed"
ATTR_AMBIGUOUS = "ambiguous"
ATTR_UNCLASSIFIED = "unclassified"
ATTR_UNAVAILABLE = "unavailable"     # the lookup failed; not a classification

# The attribution dimensions a conflict can arise in. A disagreement in ANY of
# them makes the deal ambiguous: campaign and country drive different pages, so
# picking a winner in one dimension while ignoring another is how two surfaces
# start disagreeing again.
CONFLICT_DIMENSIONS = ("gclid", "campaign_name_raw", "country_raw",
                       "acquisition_group")


def is_won(hs_is_closed_won) -> bool:
    """THE canonical won predicate.

    True only for an explicit boolean true (or its literal string/1 form as it
    may arrive from the HubSpot JSON layer). Everything else — False, None,
    missing, unparseable — is NOT won.

    Deliberately ignores stage id and stage label. Passing a label here is a
    category error: a label cannot make a deal won.
    """
    if hs_is_closed_won is True:
        return True
    if isinstance(hs_is_closed_won, str):
        return hs_is_closed_won.strip().lower() in ("true", "t", "1", "yes")
    if isinstance(hs_is_closed_won, (int, float)) and not isinstance(hs_is_closed_won, bool):
        return int(hs_is_closed_won) == 1
    return False


def parse_hubspot_bool(value):
    """Tri-state parse of a HubSpot boolean property.

    Returns True / False / None. ``None`` means HubSpot did not tell us, which
    is NOT the same as False and must be stored as unknown so the audit can see
    how many deals lack the property.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "t", "1", "yes"):
        return True
    if text in ("false", "f", "0", "no"):
        return False
    return None


def _evidence_of(contact: dict) -> tuple:
    """Comparable attribution evidence for one associated contact."""
    return tuple(
        (str(contact.get(dim)).strip().lower() if contact.get(dim) not in (None, "") else None)
        for dim in CONFLICT_DIMENSIONS
    )


def resolve_deal_associations(contacts, *, lookup_failed: bool = False) -> dict:
    """Resolve a deal's contacts into ONE primary and an attribution verdict.

    Args:
        contacts: list of dicts, each carrying at least ``contact_id`` plus any
            of the CONFLICT_DIMENSIONS evidence fields.
        lookup_failed: True when the HubSpot association lookup ERRORED. This is
            categorically different from a successful lookup that returned no
            contacts, and the two must never collapse into one state.

    Returns ``{primary_contact_id, association_count, association_status,
    association_reason, attribution_status, attribution_reason,
    rule_version}``.

    Rules (PR-ADS-153E-A §6):
      1. one contact                          → primary = it
      2. several with IDENTICAL evidence      → attribution unambiguous; the
                                                lowest stable contact id is the
                                                DISPLAY identity only
      3. several with CONFLICTING evidence    → primary None, ambiguous, and
                                                every association is retained
      4. lookup failed                        → nothing is inferred at all
    """
    base = {"rule_version": DEAL_TRUTH_RULE_VERSION}

    # ── 4. Failed lookup: we know nothing. Not "unclassified" — unclassified
    # is a CONCLUSION, and we did not reach one.
    if lookup_failed:
        return {
            **base,
            "primary_contact_id": None,
            "association_count": None,
            "association_status": ASSOC_LOOKUP_FAILED,
            "association_reason": "association_lookup_failed",
            "attribution_status": ATTR_UNAVAILABLE,
            "attribution_reason": "association_lookup_failed",
        }

    rows = [c for c in (contacts or []) if c and c.get("contact_id") not in (None, "")]

    if not rows:
        return {
            **base,
            "primary_contact_id": None,
            "association_count": 0,
            "association_status": ASSOC_NONE,
            "association_reason": "no_associated_contacts",
            "attribution_status": ATTR_UNCLASSIFIED,
            "attribution_reason": "no_associated_contacts",
        }

    # Stable ordering by contact id so the choice never depends on API order.
    def _sort_key(c):
        cid = str(c["contact_id"])
        return (0, int(cid), cid) if cid.isdigit() else (1, 0, cid)

    rows = sorted(rows, key=_sort_key)

    # ── 1. Exactly one contact ──────────────────────────────────────────────
    if len(rows) == 1:
        return {
            **base,
            "primary_contact_id": str(rows[0]["contact_id"]),
            "association_count": 1,
            "association_status": ASSOC_RESOLVED,
            "association_reason": "single_associated_contact",
            "attribution_status": ATTR_ATTRIBUTED,
            "attribution_reason": "single_contact",
        }

    distinct_evidence = {_evidence_of(c) for c in rows}

    # ── 2. Several contacts that agree ──────────────────────────────────────
    if len(distinct_evidence) == 1:
        return {
            **base,
            # Display identity only — the attribution is the same whichever of
            # these contacts is shown, which is precisely why picking one is safe
            # here and unsafe in case 3.
            "primary_contact_id": str(rows[0]["contact_id"]),
            "association_count": len(rows),
            "association_status": ASSOC_RESOLVED,
            "association_reason": (
                "multiple_contacts_identical_evidence; lowest contact id chosen "
                "as display identity only"),
            "attribution_status": ATTR_ATTRIBUTED,
            "attribution_reason": "multi_same_evidence",
        }

    # ── 3. Several contacts that disagree ───────────────────────────────────
    conflicting = sorted(
        dim for i, dim in enumerate(CONFLICT_DIMENSIONS)
        if len({e[i] for e in distinct_evidence}) > 1
    )
    return {
        **base,
        # No primary. Choosing arbitrarily here is exactly the bug that let one
        # deal bucket differently on two pages.
        "primary_contact_id": None,
        "association_count": len(rows),
        "association_status": ASSOC_AMBIGUOUS,
        "association_reason": "conflicting_evidence:" + ",".join(conflicting),
        "attribution_status": ATTR_AMBIGUOUS,
        "attribution_reason": "multi_conflicting_" + ",".join(conflicting),
    }


def primary_contact_evidence(contacts, resolution: dict) -> dict:
    """Attribution evidence carried onto the ledger row for a resolved deal.

    Ambiguous, empty and failed resolutions contribute NO evidence — the bridge
    keeps every candidate, but the ledger must not display one campaign as if it
    were the answer when the deal has no single answer.
    """
    empty = {dim: None for dim in
             ("gclid", "campaign_name_raw", "keyword_raw", "country_raw",
              "source_primary_raw", "source_detail_raw", "acquisition_group")}
    if resolution.get("association_status") != ASSOC_RESOLVED:
        return empty
    primary_id = resolution.get("primary_contact_id")
    for c in (contacts or []):
        if str(c.get("contact_id")) == str(primary_id):
            return {dim: c.get(dim) for dim in empty}
    return empty


__all__ = [
    "DEAL_TRUTH_RULE_VERSION",
    "ASSOC_RESOLVED", "ASSOC_AMBIGUOUS", "ASSOC_NONE", "ASSOC_LOOKUP_FAILED",
    "ALL_ASSOCIATION_STATUSES", "CONFLICT_DIMENSIONS",
    "ATTR_ATTRIBUTED", "ATTR_AMBIGUOUS", "ATTR_UNCLASSIFIED", "ATTR_UNAVAILABLE",
    "is_won", "parse_hubspot_bool",
    "resolve_deal_associations", "primary_contact_evidence",
]
