"""
Evidence windows (PR-ADS-141) — read-only.

Platform Evidence (Campaigns, Search Terms, Keywords, Countries) and Lead
Intelligence (Lead Quality, In Progress Leads, Flagged Waste Terms) pages use
*evidence windows* — rolling day ranges over durable Google Ads / HubSpot
evidence tables. These are DISTINCT from the *business windows*
(current_quarter, ytd, all_time, …) used by the Dashboard and Revenue &
Attribution pages; the two vocabularies must never be mixed.

This module is the single source of truth for the evidence-window vocabulary and
how each window resolves to a day lookback (or all-time = no lower date bound). It
performs NO I/O and NO writes — it only maps a window key to a lookback.

Doctrine:
  - Known windows only. An unknown window is an error, never silently coerced to a
    smaller window (e.g. all_time must never become 60d).
  - all_time resolves to ``days = None`` — the caller uses NO lower date bound. A
    caller that cannot serve all_time safely rejects it (allow_all_time=False),
    it never fabricates an empty/zero result.
"""

from __future__ import annotations

# Ordered evidence windows: value -> lookback days (None == all-time / no lower bound).
EVIDENCE_WINDOW_DAYS: dict[str, int | None] = {
    "7d": 7,
    "14d": 14,
    "30d": 30,
    "60d": 60,
    "180d": 180,
    "all_time": None,
}

# Ordered tuple of valid window keys (stable for UI + error messages).
EVIDENCE_WINDOWS: tuple[str, ...] = tuple(EVIDENCE_WINDOW_DAYS.keys())

# Default evidence window (mirrors the frontend default and the old 30d button).
DEFAULT_EVIDENCE_WINDOW = "30d"


class EvidenceWindowError(ValueError):
    """Raised for an unsupported evidence window — never silently coerced."""


def is_evidence_window(value) -> bool:
    """True when ``value`` is a known evidence-window key."""
    return value in EVIDENCE_WINDOW_DAYS


def resolve_evidence_window(window, *, allow_all_time: bool = True) -> dict:
    """Resolve an evidence-window key to ``{key, days, is_all_time}``.

    Args:
        window: One of EVIDENCE_WINDOWS.
        allow_all_time: When False, ``all_time`` is rejected (for a data source
            that cannot safely serve an unbounded query) — never coerced.

    Returns:
        {"key": str, "days": int | None, "is_all_time": bool}. ``days`` is None
        for all_time (the caller must use NO lower date bound).

    Raises:
        EvidenceWindowError: For an unknown window, or all_time when disallowed.
    """
    if window not in EVIDENCE_WINDOW_DAYS:
        raise EvidenceWindowError(
            f"Unsupported evidence window '{window}'. "
            f"Valid windows: {', '.join(EVIDENCE_WINDOWS)}."
        )
    days = EVIDENCE_WINDOW_DAYS[window]
    is_all = days is None
    if is_all and not allow_all_time:
        raise EvidenceWindowError(
            "Evidence window 'all_time' is not available for this data source."
        )
    return {"key": window, "days": days, "is_all_time": is_all}
