"""
analysis/search_term_review_state.py

PR-ADS-153D — ONE durable local review vocabulary for search terms.

Shared verbatim by the Search Terms flagged view and the Action Queue. There is
no second review-state system: both surfaces read and write the same
``search_term_review`` row, keyed by the canonical durable identity.

The states
----------
    unreviewed         no human decision recorded yet
    keep               a human judged the term legitimate — stop asking
    monitor            not actionable yet, but worth watching
    exclude_candidate  a human recommends excluding it in Google Ads
    resolved           the review is finished and needs no further action

The critical distinction
------------------------
``exclude_candidate`` is a LOCAL RECOMMENDATION. It records that a human thinks
this query should be excluded. It is NOT evidence that a Google Ads negative
keyword exists — this system has no write path to Google Ads, so it cannot know
whether anyone acted on the recommendation.

Language that would claim the action happened ("Excluded", "Negative keyword
added", "Removed from Google Ads") is therefore forbidden, and
``FORBIDDEN_APPLIED_PHRASES`` exists so a test can enforce that rather than a
reviewer having to notice it (§16).

Purity
------
No DB, no network. Deterministic.
"""

from __future__ import annotations

REVIEW_RULE_VERSION = "v1"

STATE_UNREVIEWED = "unreviewed"
STATE_KEEP = "keep"
STATE_MONITOR = "monitor"
STATE_EXCLUDE_CANDIDATE = "exclude_candidate"
STATE_RESOLVED = "resolved"

REVIEW_STATES = (
    STATE_UNREVIEWED,
    STATE_KEEP,
    STATE_MONITOR,
    STATE_EXCLUDE_CANDIDATE,
    STATE_RESOLVED,
)

REVIEW_STATE_LABELS = {
    STATE_UNREVIEWED: "Unreviewed",
    STATE_KEEP: "Keep",
    STATE_MONITOR: "Monitor",
    # Deliberately "candidate": a recommendation, never a claim that Google Ads
    # was changed.
    STATE_EXCLUDE_CANDIDATE: "Exclude candidate",
    STATE_RESOLVED: "Resolved",
}

REVIEW_STATE_HELP = {
    STATE_UNREVIEWED: "No human decision recorded yet.",
    STATE_KEEP: "Reviewed and judged legitimate — no action needed.",
    STATE_MONITOR: "Watch this term; not actionable yet.",
    STATE_EXCLUDE_CANDIDATE: (
        "A reviewer recommends excluding this query in Google Ads. "
        "This is a LOCAL recommendation only — Averroes has no write path to "
        "Google Ads and cannot confirm the negative keyword was applied."
    ),
    STATE_RESOLVED: "Review complete — no further action.",
}

# States that still require human attention, so still belong in the Action Queue.
# `keep` and `resolved` are finished decisions and are removed from the queue;
# `monitor` stays visible because the reviewer explicitly asked to keep watching.
ACTIONABLE_REVIEW_STATES = frozenset({
    STATE_UNREVIEWED,
    STATE_MONITOR,
    STATE_EXCLUDE_CANDIDATE,
})

# Decisions a human has actually made (i.e. not the default).
DECIDED_REVIEW_STATES = frozenset({
    STATE_KEEP,
    STATE_MONITOR,
    STATE_EXCLUDE_CANDIDATE,
    STATE_RESOLVED,
})

# Wording that would assert a Google Ads mutation occurred. No surface may use
# these for a local review state — enforced by test, not by convention (§16).
FORBIDDEN_APPLIED_PHRASES = (
    "negative keyword added",
    "negative keyword applied",
    "removed from google ads",
    "excluded from google ads",
    "added as negative",
)


def is_valid_review_state(value) -> bool:
    return value in REVIEW_STATES


def normalize_review_state(value) -> str:
    """Coerce a stored/API value to a known state.

    An unknown or missing value becomes ``unreviewed`` — the honest default,
    since an unrecognisable value is not proof that anyone decided anything.
    """
    text = (str(value).strip().lower() if value is not None else "")
    return text if text in REVIEW_STATES else STATE_UNREVIEWED


def requires_action(review_state) -> bool:
    """Does this review state leave work for a human?"""
    return normalize_review_state(review_state) in ACTIONABLE_REVIEW_STATES


def is_decided(review_state) -> bool:
    """Has a human recorded any decision at all?"""
    return normalize_review_state(review_state) in DECIDED_REVIEW_STATES


# Reported when the durable review store cannot be read. This is NOT a state a
# term can be in — it is the absence of knowledge about the state, and it must
# never collapse into `unreviewed` (PR-ADS-153D §32: unavailable is never a
# value). Collapsing it would assert "no human has decided this" on evidence we
# do not have, and would reopen terms a human had already resolved or kept.
REVIEW_STATUS_AVAILABLE = "available"
REVIEW_STATUS_UNAVAILABLE = "unavailable"


def review_state_payload(review_state, *, available: bool = True) -> dict:
    """Self-describing review state for an API payload.

    ``available=False`` means the durable review store could not be read. The
    payload then reports ``review_state: None`` and ``action_needed: None``
    rather than guessing — a caller cannot mistake an outage for a decision, and
    a resolved term cannot be reopened by an unreadable database.
    """
    if not available:
        return {
            "review_state": None,
            "review_state_status": REVIEW_STATUS_UNAVAILABLE,
            "review_state_label": "Review state unavailable",
            "review_state_help": (
                "The local review store could not be read, so no review "
                "decision can be shown or changed for this term. This is not "
                "the same as unreviewed."),
            "requires_action": None,
            "is_decided": None,
            "applied_to_google_ads": False,
        }
    state = normalize_review_state(review_state)
    return {
        "review_state": state,
        "review_state_status": REVIEW_STATUS_AVAILABLE,
        "review_state_label": REVIEW_STATE_LABELS[state],
        "review_state_help": REVIEW_STATE_HELP[state],
        "requires_action": state in ACTIONABLE_REVIEW_STATES,
        "is_decided": state in DECIDED_REVIEW_STATES,
        # Explicit and always present: no local state ever implies a platform
        # change was made.
        "applied_to_google_ads": False,
    }


__all__ = [
    "REVIEW_RULE_VERSION",
    "STATE_UNREVIEWED", "STATE_KEEP", "STATE_MONITOR",
    "STATE_EXCLUDE_CANDIDATE", "STATE_RESOLVED",
    "REVIEW_STATES", "REVIEW_STATE_LABELS", "REVIEW_STATE_HELP",
    "ACTIONABLE_REVIEW_STATES", "DECIDED_REVIEW_STATES",
    "REVIEW_STATUS_AVAILABLE", "REVIEW_STATUS_UNAVAILABLE",
    "FORBIDDEN_APPLIED_PHRASES",
    "is_valid_review_state", "normalize_review_state",
    "requires_action", "is_decided", "review_state_payload",
]
