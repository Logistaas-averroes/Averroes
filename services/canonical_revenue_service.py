"""
services/canonical_revenue_service.py

PR-ADS-153E-B — THE shared canonical revenue read contract.

The defect this replaces
------------------------
Averroes had three revenue lineages and every page picked one:

  * ``gclid_attribution`` — GCLID-bearing deals only, keyed on a SHA1
    attribution hash rather than the deal. Fed Dashboard, Deals, Campaigns,
    Countries and the Revenue Decision Mart.
  * ``deal_source_attribution`` — all closed-won deals, keyed by ``deal_id``,
    but with no lifecycle and no currency contract. Fed Revenue by Source and
    Dashboard Channels.
  * ``data/campaign_performance.json`` (Windsor) — fed Unit Economics.

Every one of them called its output "closed-won revenue", so the same quarter
produced different customer counts and different revenue on different pages, and
nothing in the product could say which population any given number described.

The production ledger makes the size of the error concrete: of 180 won deals,
124 have no GCLID. A dashboard sourcing "total revenue" from GCLID evidence was
showing roughly a third of the business.

The contract
------------
Exactly one module reads canonical revenue, and every page reads it through
this one. Consumers do not re-derive:

* **won status** — ``hs_is_closed_won IS TRUE``, never a stage id or an
  ``ILIKE '%won%'`` label match;
* **deal identity** — ``deal_id``, so a deal counts once no matter how many
  attribution rows a legacy lineage minted for it;
* **the revenue event date** — the canonical deal close date;
* **the revenue value** — ``revenue_usd``, summed only where the currency
  status PROVES it is safe to aggregate (``analysis.deal_currency``);
* **business-window bounds** — ``analysis.business_windows`` and nothing else;
* **the population** — ``analysis.revenue_scope``'s explicit lattice.

Fail-closed, with no silent fallback
------------------------------------
When the ledger is unreadable, or its coverage does not satisfy the same
readiness gate the merge audit applies (``check_sync_coverage``), this module
returns an explicit unavailable response carrying the reason, source, scope and
freshness. It does NOT fall back to ``gclid_attribution``,
``deal_source_attribution``, local JSON, Windsor, or an ad-hoc HubSpot read.

A fallback would be worse than an outage. Each of those sources holds a
DIFFERENT population, so a fallback silently changes what "total revenue" means
mid-incident — reintroducing precisely the contradictory numbers this sequence
of PRs exists to eliminate. An honest "unavailable" is recoverable; a quiet
population swap is not detectable at all.

Read-only. No writes to HubSpot, Google Ads, Mailchimp or the database.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone

from analysis.deal_currency import (
    CURRENCY_UNAVAILABLE,
    SUMMABLE_CURRENCY_STATUSES,
    is_summable,
)
from analysis.revenue_scope import (
    DEFAULT_SCOPE,
    SCOPE_ALL_SOURCE,
    SCOPE_ORDER,
    UnknownScopeError,
    check_lattice,
    filter_deals,
    normalize_scope,
    scope_descriptor,
    scope_evidence_gaps,
)

log = logging.getLogger(__name__)

# The one name every migrated response reports as its revenue provenance.
CANONICAL_SOURCE = "hubspot_deal_ledger"

# Unavailability reasons (stable, machine-readable).
REASON_UNKNOWN_WINDOW = "unknown_window"
REASON_UNKNOWN_SCOPE = "unknown_scope"
REASON_LEDGER_UNREADABLE = "canonical_ledger_unreadable"
REASON_SYNC_STATE_UNREADABLE = "canonical_sync_state_unreadable"
REASON_COVERAGE_NOT_PROVEN = "canonical_coverage_not_proven"

ALL_UNAVAILABLE_REASONS = (
    REASON_UNKNOWN_WINDOW,
    REASON_UNKNOWN_SCOPE,
    REASON_LEDGER_UNREADABLE,
    REASON_SYNC_STATE_UNREADABLE,
    REASON_COVERAGE_NOT_PROVEN,
)


def _as_of(sync_row: dict | None):
    """Freshness of the canonical ledger: when the last sync recorded state.

    ``None`` when unknown. Never defaulted to "now" — a stale ledger presented
    with a fresh timestamp is indistinguishable from a healthy one.
    """
    if not sync_row:
        return None
    return (sync_row.get("updated_at")
            or sync_row.get("last_incremental_at")
            or sync_row.get("bootstrap_completed_at"))


def _iso(value):
    """Render a window bound as ISO-8601, or ``None``.

    `str()` on a timezone-aware datetime yields a SPACE-separated timestamp
    (``2026-04-01 00:00:00+00:00``), which is not ISO-8601 and is not what every
    other service in the product emits. A client parsing window bounds with a
    strict ISO parser would reject it, so the boundary is formatted properly
    here rather than left for each consumer to normalise differently.
    """
    if value is None:
        return None
    if isinstance(value, str):
        # Already serialized upstream — returned untouched. Re-parsing and
        # re-formatting it could silently rewrite an offset we were handed.
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _utc_bound(value):
    """Normalize a window bound to a timezone-aware UTC datetime, or ``None``.

    The ledger read casts its bounds to ``timestamptz``, and PostgreSQL resolves
    a DATE (or a naive TIMESTAMP) against the SESSION time zone. On a server
    whose session is not UTC that silently shifts the window by hours and moves
    deals that closed near a boundary into the wrong period — two pages reading
    "the same" quarter would then disagree.

    Normalizing HERE, at the one contract boundary every consumer goes through,
    is what stops the defect being recreated: a caller may hand in a ``date``, a
    naive ``datetime`` or an ISO string, and the ledger still receives an
    explicit UTC instant. A caller passing bounds it resolved itself gets the
    same treatment as one using ``get_window_bounds``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = datetime.combine(date.fromisoformat(text), time.min)
            except ValueError:
                # Unparseable: hand it back untouched rather than inventing an
                # instant. The read will fail loudly instead of silently
                # selecting the wrong population.
                return value
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        # A bare date means MIDNIGHT UTC on that date, in both bound positions.
        # The caller owns the inclusive/exclusive contract; this only fixes the
        # instant it refers to.
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    return value


def _window_block(window, start, end, resolved) -> dict:
    return {
        "window": window,
        "window_label": (resolved or {}).get("label"),
        "window_start": _iso(start),
        "window_end": _iso(end),
        "window_is_closed": (resolved or {}).get("is_closed_window"),
    }


def unavailable(reason, *, scope=DEFAULT_SCOPE, window=None, start=None,
                end=None, resolved=None, as_of=None, detail=None,
                violation_codes=None) -> dict:
    """The explicit quarantined state. Never zeros, never a legacy fallback.

    Counts are ``None``, not ``0``. A page rendering ``0`` cannot distinguish
    "no deals closed this quarter" from "we could not read the ledger", and the
    second is the one that must interrupt an executive rather than reassure one.
    """
    try:
        scope_block = scope_descriptor(scope)
    except UnknownScopeError:
        scope_block = {"scope": str(scope), "scope_label": None,
                       "scope_description": None, "scope_rank": None,
                       "scope_rule_version": None, "is_business_total": False}
    return {
        "available": False,
        "reason": reason,
        "detail": detail,
        "violation_codes": sorted(violation_codes or []),
        "source": CANONICAL_SOURCE,
        **scope_block,
        **_window_block(window, start, end, resolved),
        "as_of": as_of,
        "won_deals": None,
        "revenue_usd": None,
        "revenue_deals": None,
        "currency_unavailable_deals": None,
        "ambiguous_associations": None,
        "failed_associations": None,
        "unknown_won_state_deals": None,
        "attribution_coverage": None,
        "deals": [],
        "legacy_fallback_used": False,
    }


def load_won_deals(window=None, *, start=None, end=None, now=None,
                   require_ready: bool = True) -> dict:
    """Read the canonical won-deal population for one business window.

    This is the single database read behind every migrated page. Callers pass
    the result back into :func:`build_snapshot` for each scope they need, so a
    page showing all-source revenue beside campaign-attributed revenue proves
    the two came from ONE population — the subset relationship is a filter over
    the same rows, not a second query that might disagree.

    Args:
        window: a business-window key from ``analysis.business_windows``. When
            given it fully determines the bounds; ``start``/``end`` are ignored.
        start/end: explicit bounds for callers that already resolved a window
            (``end`` EXCLUSIVE). Used by the trend/series consumers.
        now: reference time, for deterministic tests.
        require_ready: enforce the coverage gate. Only the audit/diagnostic
            paths may pass False, and they must say so in their output.

    Returns ``{available, deals, readiness, ...}``. On any failure the result is
    an ``unavailable`` block — the caller never receives a partial population it
    could mistake for a complete one.
    """
    from analysis.business_windows import is_valid_window, resolve_window, get_window_bounds
    from db import deal_ledger_repository as ledger_repo
    from services.revenue_reconciliation_service import check_sync_coverage

    resolved = None
    if window is not None:
        if not is_valid_window(window):
            return unavailable(REASON_UNKNOWN_WINDOW, window=window,
                               detail=f"unknown business window '{window}'")
        resolved = resolve_window(window, now=now)
        start, end = get_window_bounds(window, now=now)

    # Normalize BEFORE anything else reads or reports the bounds, so the value
    # the ledger is queried with is the value the response advertises.
    start, end = _utc_bound(start), _utc_bound(end)

    sync_res = ledger_repo.fetch_sync_state()
    as_of = _as_of(sync_res.get("row"))

    # Coverage first: a readable ledger with unproven coverage is the exact
    # failure 153E-A2 was built to catch — it answers every query happily while
    # holding an unknown fraction of history.
    findings = check_sync_coverage(sync_res)
    if require_ready and findings:
        codes = {f["code"] for f in findings}
        return unavailable(
            REASON_COVERAGE_NOT_PROVEN, window=window, start=start, end=end,
            resolved=resolved, as_of=as_of, violation_codes=codes,
            detail="; ".join(f["message"] for f in findings))

    deals_res = ledger_repo.fetch_won_deals(start, end)
    if not deals_res.get("available"):
        return unavailable(REASON_LEDGER_UNREADABLE, window=window, start=start,
                           end=end, resolved=resolved, as_of=as_of,
                           detail=deals_res.get("reason"))

    states_res = ledger_repo.fetch_won_state_counts(start, end)

    return {
        "available": True,
        "reason": None,
        "source": CANONICAL_SOURCE,
        **_window_block(window, start, end, resolved),
        "as_of": as_of,
        "deals": deals_res.get("rows") or [],
        "sync_state": sync_res.get("row"),
        "readiness": {
            "enforced": bool(require_ready),
            "ok": not findings,
            "violation_codes": sorted({f["code"] for f in findings}),
            "violations": [f["message"] for f in findings],
        },
        # NULL, not 0: an unreadable state count must not render as "no deals
        # with an unknown won state".
        "unknown_won_state_deals": (
            (states_res.get("counts") or {}).get("unknown_won")
            if states_res.get("available") else None),
        "legacy_fallback_used": False,
    }


def summarize_deals(deals, scope=DEFAULT_SCOPE) -> dict:
    """Aggregate an already-loaded canonical row set for one scope. Pure.

    Splitting this from the read is what makes the cross-page guarantee
    mechanical: every consumer aggregates through this function, so "same metric
    + same window + same scope" cannot produce two answers.
    """
    scope = normalize_scope(scope)
    rows = filter_deals(deals or [], scope)

    revenue = 0.0
    revenue_deals = 0
    currency_unavailable = 0
    ambiguous = 0
    failed = 0
    for row in rows:
        status = row.get("currency_status")
        value = row.get("revenue_usd")
        if is_summable(status) and value is not None:
            revenue += float(value)
            revenue_deals += 1
        else:
            # Includes an amount HubSpot never gave us and an amount whose
            # currency we could not prove. Both are unknown, not zero, so
            # neither joins the total and both stay countable.
            currency_unavailable += 1
        if row.get("association_status") == "ambiguous" \
                or row.get("attribution_status") == "ambiguous":
            ambiguous += 1
        if row.get("association_status") == "lookup_failed":
            failed += 1

    return {
        **scope_descriptor(scope),
        "won_deals": len(rows),
        # A total over ZERO proven deals is 0.0 only when there were also no
        # unproven ones; otherwise every deal's value is unknown and the total
        # is unknown too, not $0.
        "revenue_usd": (round(revenue, 2)
                        if revenue_deals or not currency_unavailable else None),
        "revenue_deals": revenue_deals,
        "currency_unavailable_deals": currency_unavailable,
        "currency_complete": currency_unavailable == 0,
        "ambiguous_associations": ambiguous,
        "failed_associations": failed,
    }


def _attribution_coverage(deals) -> dict:
    """How much of the all-source population each narrower scope covers."""
    totals = {}
    revenue = {}
    for scope in SCOPE_ORDER:
        summary = summarize_deals(deals, scope)
        totals[scope] = summary["won_deals"]
        revenue[scope] = summary["revenue_usd"]

    base = totals[SCOPE_ALL_SOURCE]
    coverage = {
        "won_deals_by_scope": totals,
        "revenue_usd_by_scope": revenue,
        # Percentages are withheld rather than shown as 0% when there is no
        # population to be a percentage OF.
        "pct_of_won_deals_by_scope": {
            scope: (round(100.0 * count / base, 1) if base else None)
            for scope, count in totals.items()
        },
        "lattice_violations": check_lattice(totals),
        **scope_evidence_gaps(deals),
    }
    return coverage


def build_snapshot(base, scope=DEFAULT_SCOPE, *, include_deals: bool = False) -> dict:
    """Turn a :func:`load_won_deals` result into one scoped revenue response.

    Carries every field the 153E-B response contract requires: ``source``,
    ``scope``, ``window``, ``window_start``, ``window_end``, ``as_of``,
    ``available``, ``won_deals``, ``revenue_usd``, ``currency_unavailable_deals``,
    ``ambiguous_associations``, ``failed_associations`` and attribution coverage.
    """
    if not base.get("available"):
        # Propagate the quarantined state verbatim, re-scoped. A caller asking
        # for campaign-scope revenue during an outage must not be handed an
        # all-source-shaped answer.
        return unavailable(
            base.get("reason") or REASON_LEDGER_UNREADABLE,
            scope=scope, window=base.get("window"),
            start=base.get("window_start"), end=base.get("window_end"),
            as_of=base.get("as_of"), detail=base.get("detail"),
            violation_codes=base.get("violation_codes"))

    try:
        scope = normalize_scope(scope)
    except UnknownScopeError as exc:
        return unavailable(REASON_UNKNOWN_SCOPE, scope=scope,
                           window=base.get("window"),
                           start=base.get("window_start"),
                           end=base.get("window_end"),
                           as_of=base.get("as_of"), detail=str(exc))

    deals = base.get("deals") or []
    summary = summarize_deals(deals, scope)
    snapshot = {
        "available": True,
        "reason": None,
        "source": CANONICAL_SOURCE,
        **summary,
        "window": base.get("window"),
        "window_label": base.get("window_label"),
        "window_start": base.get("window_start"),
        "window_end": base.get("window_end"),
        "as_of": base.get("as_of"),
        "unknown_won_state_deals": base.get("unknown_won_state_deals"),
        "attribution_coverage": _attribution_coverage(deals),
        "readiness": base.get("readiness"),
        "legacy_fallback_used": False,
    }
    if include_deals:
        snapshot["deals"] = filter_deals(deals, scope)
    return snapshot


def get_revenue_snapshot(window=None, scope=DEFAULT_SCOPE, *, start=None,
                         end=None, now=None, include_deals: bool = False) -> dict:
    """One-call convenience: read the window, then summarize it for one scope."""
    base = load_won_deals(window, start=start, end=end, now=now)
    return build_snapshot(base, scope, include_deals=include_deals)


def get_scope_ladder(window=None, *, start=None, end=None, now=None,
                     base=None) -> dict:
    """Every scope for one window, proven to nest.

    This is what a ROAS page renders beside its attributed figure so a reader can
    see the attributed number IS a subset — and how large a subset — rather than
    inferring that it is the whole business.
    """
    if base is None:
        base = load_won_deals(window, start=start, end=end, now=now)
    if not base.get("available"):
        return {
            "available": False,
            "reason": base.get("reason"),
            "detail": base.get("detail"),
            "violation_codes": base.get("violation_codes") or [],
            "source": CANONICAL_SOURCE,
            "window": base.get("window"),
            "window_start": base.get("window_start"),
            "window_end": base.get("window_end"),
            "as_of": base.get("as_of"),
            "scopes": {},
            "lattice_violations": [],
        }

    deals = base.get("deals") or []
    scopes = {s: summarize_deals(deals, s) for s in SCOPE_ORDER}
    return {
        "available": True,
        "reason": None,
        "source": CANONICAL_SOURCE,
        "window": base.get("window"),
        "window_label": base.get("window_label"),
        "window_start": base.get("window_start"),
        "window_end": base.get("window_end"),
        "as_of": base.get("as_of"),
        "scopes": scopes,
        "lattice_violations": check_lattice(
            {s: scopes[s]["won_deals"] for s in SCOPE_ORDER}),
        "revenue_lattice_violations": check_lattice(
            {s: scopes[s]["revenue_usd"] for s in SCOPE_ORDER
             if scopes[s]["revenue_usd"] is not None}),
        "evidence_gaps": scope_evidence_gaps(deals),
        "legacy_fallback_used": False,
    }


def deal_display_row(row) -> dict:
    """One canonical deal, shaped for a page's deal table.

    ``deal_name`` replaces the legacy ``company`` column. That column came from
    the associated CONTACT's ``company`` property in ``gclid_attribution``, not
    from the deal, so it disagreed with the deal whenever a deal's contacts
    belonged to different companies — and it does not exist on the canonical
    ledger at all. Presenting the deal's own name under a "Company" heading
    would be a fabricated label; the field is renamed instead.

    ``revenue_usd`` is None whenever the currency is unproven, and the status is
    carried alongside so a table can render "unavailable" rather than "$0".
    """
    status = row.get("currency_status") or CURRENCY_UNAVAILABLE
    return {
        "deal_id": row.get("deal_id"),
        "deal_name": row.get("deal_name"),
        "country": row.get("country_raw"),
        "campaign_name": row.get("campaign_name_raw"),
        "gclid": row.get("gclid"),
        "deal_close_date": row.get("deal_close_date"),
        "deal_stage_label": row.get("deal_stage_label"),
        "revenue_usd": row.get("revenue_usd") if is_summable(status) else None,
        "currency_status": status,
        "currency_reason": row.get("currency_reason"),
        "acquisition_group": row.get("acquisition_group"),
        "attribution_status": row.get("attribution_status"),
        "association_status": row.get("association_status"),
        "primary_contact_id": row.get("primary_contact_id"),
        "source": CANONICAL_SOURCE,
    }


def canonical_deal_rows(base, scope=DEFAULT_SCOPE) -> list:
    """Canonical won deals in the row shape the migrated page services consume.

    One deliberate continuity: the money key stays ``deal_amount_usd``, the name
    the legacy repository used. The VALUE and the POPULATION are entirely new —
    it is now the canonical ``revenue_usd``, present only when the currency was
    proven, over the ``hs_is_closed_won`` population rather than the GCLID one.
    Keeping the key avoided a mechanical rename across ~40 arithmetic sites in
    the page services, where a single missed site would have silently mixed a
    legacy amount into a canonical total. The rename that DOES happen is the one
    that carries meaning: legacy ``company`` (the associated contact's employer)
    becomes ``deal_name`` (the deal's own name), because the ledger has no
    company field and presenting one under that heading would be invented.

    ``match_status`` / ``match_source`` are gone. They described how a GCLID was
    matched to a click, which says nothing about whether a deal is won or what
    it was worth. Attribution evidence is now carried explicitly as
    ``attribution_status`` / ``acquisition_group`` / ``attribution_scope``.
    """
    from analysis.revenue_scope import narrowest_scope

    rows = []
    for row in filter_deals(base.get("deals") or [], scope):
        status = row.get("currency_status") or CURRENCY_UNAVAILABLE
        rows.append({
            "deal_id": row.get("deal_id"),
            "deal_name": row.get("deal_name"),
            "country": row.get("country_raw"),
            "campaign_name": row.get("campaign_name_raw"),
            "keyword": row.get("keyword_raw"),
            "gclid": row.get("gclid"),
            "deal_close_date": row.get("deal_close_date"),
            "deal_created_at": row.get("deal_created_at"),
            "deal_stage_label": row.get("deal_stage_label"),
            "deal_amount_usd": (row.get("revenue_usd")
                                if is_summable(status) else None),
            "currency_status": status,
            "currency_reason": row.get("currency_reason"),
            "deal_currency_code": row.get("deal_currency_code"),
            "acquisition_group": row.get("acquisition_group"),
            "attribution_status": row.get("attribution_status"),
            "attribution_reason": row.get("attribution_reason"),
            "attribution_scope": narrowest_scope(row),
            "association_status": row.get("association_status"),
            "association_count": row.get("association_count"),
            "primary_contact_id": row.get("primary_contact_id"),
            "source_primary_raw": row.get("source_primary_raw"),
            "source_detail_raw": row.get("source_detail_raw"),
            "source": CANONICAL_SOURCE,
        })
    return rows


def summable_revenue(row):
    """This row's contribution to a USD total, or ``None`` when it has none."""
    if is_summable(row.get("currency_status")) and row.get("revenue_usd") is not None:
        return float(row["revenue_usd"])
    return None


#: Why a scope's revenue TOTAL is not publishable, even though the population is.
REASON_REVENUE_INCOMPLETE = "canonical_revenue_amounts_incomplete"
V_CURRENCY_UNPROVEN_DEALS = "currency_unproven_deals_in_population"


def revenue_total_publishable(base: dict, scope=DEFAULT_SCOPE) -> dict:
    """THE one decision: may a production page publish this scope's revenue total?

    PR-ADS-154C-F3. Two conditions, and the second was the live defect.

    **1. The population must be available.** A page must never publish revenue
    when :func:`load_won_deals` refused to release the population.

    **2. Every deal in it must have a proven amount.** ``summarize_deals``
    returns ``revenue_usd`` as the sum of the deals whose currency WAS proven —
    a partial known-dollar sum — whenever at least one such deal exists. Over a
    population holding 181 won deals of which one has no proven amount, that is
    a number smaller than the truth, published under the heading "Closed-Won
    Revenue" with ``truth_status: ready``.

    Channels, Campaigns, Countries and Deals already refused it, each with its
    own "never a partial sum implying completeness" rule; the mart, Overview and
    Revenue did not, which is precisely why All Time showed
    ``$878,324.80`` on three pages and ``unavailable`` on four. One rule, applied
    once, replaces four private copies and the mart's silent exception.

    Returns ``{publishable, reason, detail, violation_codes, currency_unavailable_deals}``.
    ``publishable`` False means the total is UNKNOWN — never zero, never partial.

    The COUNT is deliberately not governed here. ``won_deals`` is complete
    whatever the amounts are, so a page may keep publishing customers while
    withholding revenue; blanking a count we did measure would be its own
    fabrication.
    """
    if not base.get("available"):
        return {
            "publishable": False,
            "reason": base.get("reason") or REASON_LEDGER_UNREADABLE,
            "detail": base.get("detail"),
            "violation_codes": sorted(base.get("violation_codes") or []),
            "currency_unavailable_deals": None,
        }

    summary = summarize_deals(base.get("deals") or [], scope)
    unproven = summary.get("currency_unavailable_deals") or 0
    if unproven:
        return {
            "publishable": False,
            "reason": REASON_REVENUE_INCOMPLETE,
            "detail": (
                f"{unproven} of {summary.get('won_deals')} closed-won deal(s) in "
                f"scope '{summary.get('scope', scope)}' have no proven amount, so "
                "the window total is unknown — the sum of the rest is a partial "
                "figure and is not published as the total"),
            "violation_codes": [V_CURRENCY_UNPROVEN_DEALS],
            "currency_unavailable_deals": unproven,
        }

    return {
        "publishable": True,
        "reason": None,
        "detail": None,
        "violation_codes": [],
        "currency_unavailable_deals": 0,
    }


__all__ = [
    "CANONICAL_SOURCE", "SUMMABLE_CURRENCY_STATUSES",
    "REASON_UNKNOWN_WINDOW", "REASON_UNKNOWN_SCOPE",
    "REASON_LEDGER_UNREADABLE", "REASON_SYNC_STATE_UNREADABLE",
    "REASON_COVERAGE_NOT_PROVEN", "REASON_REVENUE_INCOMPLETE",
    "V_CURRENCY_UNPROVEN_DEALS", "ALL_UNAVAILABLE_REASONS",
    "unavailable", "load_won_deals", "summarize_deals", "build_snapshot",
    "get_revenue_snapshot", "get_scope_ladder", "deal_display_row",
    "canonical_deal_rows", "summable_revenue", "revenue_total_publishable",
]
