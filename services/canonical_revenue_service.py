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
from contextlib import contextmanager
from contextvars import ContextVar
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
#: Why a scope's revenue TOTAL is not publishable even though the population IS
#: readable: a deal in it has no proven amount, so the total is unknown.
#:
#: PR-ADS-155 §5 renamed the VALUE from ``canonical_revenue_amounts_incomplete``
#: to name the cause rather than the symptom. "Incomplete amounts" describes the
#: total; ``closed_won_deals_missing_amount`` describes the 14 source records an
#: operator has to go and price, which is the only action that resolves it.
REASON_REVENUE_INCOMPLETE = "closed_won_deals_missing_amount"
V_CURRENCY_UNPROVEN_DEALS = "currency_unproven_deals_in_population"

ALL_UNAVAILABLE_REASONS = (
    REASON_UNKNOWN_WINDOW,
    REASON_UNKNOWN_SCOPE,
    REASON_LEDGER_UNREADABLE,
    REASON_SYNC_STATE_UNREADABLE,
    REASON_COVERAGE_NOT_PROVEN,
    # PR-ADS-154C-F3-F1 §4. A readable population whose TOTAL is unknown because
    # a deal in it has no proven amount. Distinct from every reason above: those
    # say the population could not be read, this one says it could.
    REASON_REVENUE_INCOMPLETE,
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


# PR-ADS-161E — one coherent snapshot per page. Inside ``one_snapshot()`` every
# closed-won read (``load_won_deals`` for any window, the truth service, the
# exception workbench) is answered from ONE READ ONLY universe, so a page made
# of several reads cannot disagree with itself mid-sync.
_SNAPSHOT: ContextVar = ContextVar("canonical_closed_won_snapshot",
                                   default=None)


@contextmanager
def one_snapshot():
    """Read the closed-won universe once for everything inside the block.

    Nested blocks share the outermost snapshot.
    """
    if _SNAPSHOT.get() is not None:
        yield
        return
    token = _SNAPSHOT.set({})
    try:
        yield
    finally:
        _SNAPSHOT.reset(token)


def snapshot_campaign_resolver(now=None) -> tuple:
    """Campaign Evidence's resolver (``ccr.campaign_resolver``), built once per
    snapshot and account day: it reads all-time canonical spend and the
    approved identity mappings, which do not change inside one page."""
    from services import canonical_customer_revenue_service as ccr

    cache = _SNAPSHOT.get()
    key = ("campaign_resolver", (now or datetime.now(timezone.utc)).date())
    if cache is not None and key in cache:
        return cache[key]
    built = ccr.campaign_resolver(now)
    if cache is not None:
        cache[key] = built
    return built


def single_snapshot(fn):
    """Decorate a page builder so all of its closed-won reads share ONE
    snapshot (:func:`one_snapshot`)."""
    import functools

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        with one_snapshot():
            return fn(*args, **kwargs)
    return _wrapped


def load_won_deals(window=None, *, start=None, end=None, now=None,
                   require_ready: bool = True) -> dict:
    """The canonical won-deal population of one business window.

    PR-ADS-161E: this is now a reader of THE closed-won truth
    (``services.canonical_customer_revenue_service.window_view``), not a second
    windowed query. The population, its window membership and every verdict
    are the canonical ones:

    * members are ``hs_is_closed_won IS TRUE`` deals with an exact close date
      inside ``[start, end)`` and not after now — a close later today is in no
      window yet; All Time also holds the undated won deals (the legacy query
      ``deal_close_date < tomorrow`` dropped them and admitted closes later
      today);
    * the deal count is released only when the canonical verdict publishes it.
      An undated won deal, or a won flag/stage disagreement, withholds a
      finite window's count: the result is then ``available: False`` with the
      canonical ``status`` and ``reason``, never a population missing a deal;
    * ``publication`` carries the canonical verdict for deals, revenue and
      customers, ``customers`` the canonical customer metric (distinct proven
      HubSpot companies), and ``company_ids_by_deal`` the identity map
      :func:`subset_metrics` needs to count customers in a subset.

    Callers pass the result back into :func:`build_snapshot` / the scope
    ladder / :func:`subset_metrics`, so a page's figures share one population.

    Args:
        window: a business-window key from ``analysis.business_windows``. When
            given it fully determines the bounds; ``start``/``end`` are ignored.
        start/end: explicit half-open bounds (``end`` EXCLUSIVE) for the
            trend/series consumers; evaluated by the same canonical membership.
        now: reference time, for deterministic tests.
        require_ready: retained for signature compatibility. The canonical
            truth never releases a population whose coverage is unproven.
    """
    from analysis import closed_won_truth as cwt
    from analysis.business_windows import is_valid_window, resolve_window
    from services import canonical_customer_revenue_service as ccr

    resolved = None
    if window is not None:
        if not is_valid_window(window):
            return unavailable(REASON_UNKNOWN_WINDOW, window=window,
                               detail=f"unknown business window '{window}'")
        resolved = resolve_window(window, now=now)
        bounds = ccr.window_bounds(ccr.WINDOW_BUSINESS, window, now)
        start, end = bounds["start"], bounds["end"]
        view_args = {"window_type": ccr.WINDOW_BUSINESS, "window_key": window}
    else:
        # Normalize BEFORE anything else reads or reports the bounds, so the
        # value membership is evaluated with is the value the response
        # advertises.
        start, end = _utc_bound(start), _utc_bound(end)
        view_args = {"start": start, "end": end}

    universe = load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID)
    sync_row = universe.get("sync_state")
    as_of = _as_of(sync_row)
    if not universe.get("available"):
        return {**unavailable(REASON_LEDGER_UNREADABLE, window=window,
                              start=start, end=end, resolved=resolved,
                              as_of=as_of,
                              detail=universe.get("reason")
                              or universe.get("error")),
                **_canonical_block(None, cwt.UNAVAILABLE,
                                   REASON_LEDGER_UNREADABLE)}

    view = ccr.window_view(**view_args, now=now, universe=universe)
    truth = view["truth"]
    publication = truth.get("publication") or {}
    findings = universe.get("coverage_findings") or []
    if findings:
        # Coverage first: a readable ledger with unproven coverage answers
        # every query while holding an unknown fraction of history.
        return {**unavailable(
            REASON_COVERAGE_NOT_PROVEN, window=window, start=start, end=end,
            resolved=resolved, as_of=as_of,
            violation_codes={f["code"] for f in findings},
            detail="; ".join(f["message"] for f in findings)),
            **_canonical_block(truth, cwt.UNAVAILABLE,
                               REASON_COVERAGE_NOT_PROVEN)}

    deal_pub = publication.get("closed_won_deals") or {}
    if deal_pub.get("status") != cwt.PUBLISHED or view["members"] is None:
        # The canonical deal count is withheld (an undated won deal, or a won
        # flag/stage disagreement): no population is released, so no page can
        # publish a count, a sum or a ratio over an incomplete one.
        return {**unavailable(
            deal_pub.get("reason") or REASON_LEDGER_UNREADABLE,
            window=window, start=start, end=end, resolved=resolved,
            as_of=as_of, detail=_withheld_detail(truth)),
            **_canonical_block(truth, deal_pub.get("status")
                               or cwt.UNAVAILABLE, deal_pub.get("reason"))}

    membership = truth.get("membership") or {}
    return {
        "available": True,
        "reason": None,
        "source": CANONICAL_SOURCE,
        **_window_block(window, start, end, resolved),
        "as_of": as_of,
        "deals": [dict(r) for r in view["members"]],
        "sync_state": sync_row,
        "readiness": {
            "enforced": bool(require_ready),
            "ok": True,
            "violation_codes": [],
            "violations": [],
        },
        # NULL, not 0: an unreadable state count must not render as "no deals
        # with an unknown won state".
        "unknown_won_state_deals": membership.get("unknown_won_state_deals"),
        "undated_won_deals_included": (membership.get("undated_won_deals")
                                       if membership.get("undated_included")
                                       else 0),
        "company_ids_by_deal": view["company_ids_by_deal"],
        **_canonical_block(truth, cwt.PUBLISHED, None),
        "legacy_fallback_used": False,
    }


def _withheld_detail(truth) -> str | None:
    membership = (truth or {}).get("membership") or {}
    parts = []
    if membership.get("undated_won_deals"):
        parts.append(f"{membership['undated_won_deals']} won deal(s) have no "
                     "close date and could belong to this window")
    conflicts = membership.get("won_definition_conflicts") or []
    if conflicts:
        parts.append(f"{len(conflicts)} deal(s) where HubSpot's won flag and "
                     "the confirmed won stage disagree")
    return "; ".join(parts) or None


def _canonical_block(truth, status, reason) -> dict:
    """The canonical verdict a reader carries beside its population.

    Serializable: verdicts and the canonical customer metric only — never the
    identity map or member ids of a withheld window.
    """
    from analysis import closed_won_truth as cwt

    publication = (truth or {}).get("publication") or {
        m: {"status": status, "reason": reason}
        for m in ("closed_won_deals", "revenue_usd", "customers")}
    outcomes = (truth or {}).get("outcomes") or {}
    cust = publication.get("customers") or {}
    rev = publication.get("revenue_usd") or {}
    deals = publication.get("closed_won_deals") or {}
    return {
        "status": (deals.get("status") or status),
        "publication": {k: publication.get(k) for k in
                        ("closed_won_deals", "revenue_usd", "customers")},
        "canonical_metrics": {
            "closed_won_deals": cwt.metric(outcomes.get("closed_won_deals"),
                                           deals.get("status") or status,
                                           deals.get("reason") or reason),
            "revenue_usd": cwt.metric(outcomes.get("revenue_usd"),
                                      rev.get("status") or status,
                                      rev.get("reason") or reason),
            "customers": cwt.metric(outcomes.get("customers"),
                                    cust.get("status") or status,
                                    cust.get("reason") or reason,
                                    dedup_key="hubspot_company_id"),
        },
        "canonical_window": (truth or {}).get("window"),
        "cohort": "closed_won_close_date",
        "campaign_placement": _campaign_placement(truth),
    }


def _campaign_placement(truth) -> dict | None:
    """The canonical placement verdict for per-campaign figures.

    Read from the window's own partition (``cwt.attribution_bucket`` under
    Campaign Evidence's resolver): a Google Ads won deal that resolves to no
    campaign — no label, an unmapped label, or no resolver at all — is in
    ``google_ads_unplaced``. Nothing here classifies a deal.
    """
    from analysis import closed_won_truth as cwt

    if not truth or not truth.get("available", True):
        return None
    part = ((truth.get("attribution") or {}).get("partition") or {}).get(
        cwt.BUCKET_GOOGLE_ADS_UNPLACED) or {}
    unplaced = part.get("deals")
    if truth.get("campaign_resolver_available") is False:
        return {"status": cwt.WITHHELD,
                "reason": cwt.R_CAMPAIGN_IDENTITY_UNAVAILABLE,
                "unplaced_google_ads_deals": unplaced}
    if unplaced is None:
        # Redacted with a withheld deal count: no per-campaign figure exists.
        return {"status": cwt.WITHHELD, "reason": cwt.R_DEALS_WITHHELD,
                "unplaced_google_ads_deals": None}
    if unplaced > 0:
        return {"status": cwt.WITHHELD, "reason": cwt.R_CAMPAIGN_UNPLACED,
                "unplaced_google_ads_deals": unplaced}
    return {"status": cwt.PUBLISHED, "reason": None,
            "unplaced_google_ads_deals": 0}


def subset_metrics(base: dict, rows) -> dict:
    """Closed-won deals, revenue and customers for a subset of ``base``'s
    population — a campaign, a country, a source group, a scope.

    Delegates to ``analysis.closed_won_truth.subset_outcomes``: each metric
    inherits the window's canonical verdict. When ``base`` released no
    population every metric is unavailable with ``base``'s reason.
    ``rows`` may be raw ledger rows or :func:`canonical_deal_rows` rows.
    """
    from analysis import closed_won_truth as cwt

    if not base.get("available"):
        reason = base.get("reason") or REASON_LEDGER_UNREADABLE
        status = base.get("status") or cwt.UNAVAILABLE
        if status == cwt.PUBLISHED:
            status = cwt.UNAVAILABLE
        return {m: cwt.metric(None, status, reason)
                for m in cwt.SUBSET_METRICS}
    by_id = {str(r.get("deal_id")): r for r in base.get("deals") or []}
    members = [by_id.get(str(r.get("deal_id")), r) for r in rows or []]
    return cwt.subset_outcomes(
        members, publication=base.get("publication"),
        company_ids_by_deal=base.get("company_ids_by_deal"))


#: Reasons that mean the ledger could not be READ — an outage — as opposed to a
#: readable ledger whose canonical population is not publishable (coverage
#: unproven, an undated or conflicting won deal). Pages show the two differently.
OUTAGE_REASONS = (REASON_LEDGER_UNREADABLE, REASON_SYNC_STATE_UNREADABLE,
                  "database_unavailable")


def is_ledger_outage(reason) -> bool:
    """Is this unavailable-reason an outage rather than a withheld population?"""
    return reason in OUTAGE_REASONS


def campaign_placement_gate(base: dict) -> dict | None:
    """Why per-campaign closed-won figures are withheld in this window, or None.

    Republishes the canonical placement verdict (:func:`_campaign_placement`):
    a Google Ads won deal that Campaign Evidence's resolver places on no
    campaign could belong to ANY campaign, so every per-campaign deal count,
    revenue, customer count, ROAS and CAC would be a lower bound published as a
    total (``cwt.R_CAMPAIGN_UNPLACED``). Applies to per-campaign rows only: the
    campaign-attributable SCOPE total, and the account-wide Google Ads figures,
    are exact for their own scopes. Fails closed when the verdict is missing.
    """
    from analysis import closed_won_truth as cwt

    if not base.get("available"):
        return None
    placement = base.get("campaign_placement")
    if not placement or placement.get("status") not in cwt.PUBLICATION_STATUSES:
        return {"status": cwt.WITHHELD,
                "reason": cwt.R_CAMPAIGN_IDENTITY_UNAVAILABLE,
                "unplaced_google_ads_deals": None}
    if placement["status"] == cwt.PUBLISHED:
        return None
    return dict(placement)


def reader_row(base: dict, rows, *, spend_usd=None, spend_reason=None,
               period_ratio_scope_ok: bool = True, gate: dict | None = None) -> dict:
    """The canonical closed-won fields of one page row (a campaign, a country,
    a source group, a summary), from one population.

    ``closed_won_deals`` is a deal count, ``customers`` distinct proven HubSpot
    companies (never a deal count), ``won_revenue`` a total or None. ``gate``
    (e.g. :func:`campaign_placement_gate`) withholds the row's deals, revenue
    and customers for a reason the window carries.

    Ratios (PR-ADS-161E, decision 1):

    * ``roas`` / ``cac`` are ALWAYS None, their metrics ``unavailable``:
      certified ROAS and CAC need spend and outcomes joined through one
      acquisition cohort, which no canonical service supplies.
    * ``period_revenue_per_ad_spend`` / ``period_spend_per_customer_won`` are
      the close-date operational ratios, under explicit names, each carrying
      ``ratio_kind: non_cohort_operational_ratio`` and its disclosure.

    ``metrics`` holds every value with its status and reason.
    """
    from analysis import closed_won_truth as cwt

    m = subset_metrics(base, rows)
    if gate is not None:
        m = {k: cwt.metric(None, gate["status"], gate["reason"],
                           **({"dedup_key": "hubspot_company_id",
                               "additive": False} if k == "customers" else {}))
             for k in cwt.SUBSET_METRICS}
    per_spend = cwt.derive_period_revenue_per_ad_spend(
        m["revenue_usd"], spend_usd, spend_reason=spend_reason,
        scope_compatible=period_ratio_scope_ok)
    per_customer = cwt.derive_period_spend_per_customer_won(
        m["customers"], spend_usd, spend_reason=spend_reason,
        scope_compatible=period_ratio_scope_ok)
    return {
        "closed_won_deals": m["closed_won_deals"]["value"],
        "customers": m["customers"]["value"],
        "won_revenue": m["revenue_usd"]["value"],
        cwt.PERIOD_REVENUE_PER_AD_SPEND: per_spend["value"],
        cwt.PERIOD_SPEND_PER_CUSTOMER_WON: per_customer["value"],
        "roas": None,
        "cac": None,
        "metrics": {**m,
                    cwt.PERIOD_REVENUE_PER_AD_SPEND: per_spend,
                    cwt.PERIOD_SPEND_PER_CUSTOMER_WON: per_customer,
                    "roas": cwt.certified_roas(),
                    "cac": cwt.certified_cac()},
    }


def won_window_sql_bounds(window: str, *, now=None) -> tuple:
    """The exact ``(start, end)`` :func:`load_won_deals` queries for ``window``.

    Business-window bounds, normalized to UTC exactly as that read does. Used
    by the PR-ADS-161D audit so its in-snapshot cross-check queries the
    production SQL with the production bounds.
    """
    from analysis.business_windows import get_window_bounds
    start, end = get_window_bounds(window, now=now)
    return _utc_bound(start), _utc_bound(end)


def load_closed_won_universe(won_stage_id: str, *,
                             sql_windows: dict | None = None) -> dict:
    """The all-time closed-won universe, for the PR-ADS-161D truth service.

    Goes through this module so the ledger is still read in exactly one place
    and the won predicate is still ``hs_is_closed_won IS TRUE``. Unlike
    :func:`load_won_deals` it applies NO window and does NOT enforce the
    coverage gate: the caller windows the rows itself (it must SEE undated
    deals to disclose them) and reports the gate's verdict per metric rather
    than refusing to answer at all. The gate's findings are returned beside the
    rows, computed by the one shared implementation (``check_sync_coverage``).

    ``won_stage_id`` is a cross-check input only — see
    ``db.deal_ledger_repository.fetch_closed_won_universe``. ``sql_windows``
    (``{key: (start, end)}``) asks for production's windowed won read in the
    same snapshot; only the audit passes it.
    """
    from db import deal_ledger_repository as ledger_repo
    from services.revenue_reconciliation_service import check_sync_coverage

    cache = _SNAPSHOT.get()
    key = ("universe", str(won_stage_id))
    if cache is not None and sql_windows is None and key in cache:
        return cache[key]
    universe = ledger_repo.fetch_closed_won_universe(
        won_stage_id, sql_windows=sql_windows)
    if not universe.get("available"):
        out = {**universe, "coverage_findings": None,
               "source": CANONICAL_SOURCE}
    else:
        findings = check_sync_coverage(
            {"available": True, "row": universe.get("sync_state")})
        out = {**universe, "coverage_findings": findings,
               "source": CANONICAL_SOURCE}
    if cache is not None and sql_windows is None:
        cache[key] = out
    return out


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
    from analysis.closed_won_truth import revenue_is_proven

    for row in rows:
        value = row.get("revenue_usd")
        # PR-ADS-161E: the canonical rule — proven USD AND a valid amount. A
        # negative amount is invalid evidence, not a refund to net off the
        # total; it is counted as unproven exactly as the truth service does.
        if revenue_is_proven(row):
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

    # PR-ADS-154C-F3-F1 §2. `revenue_usd` used to be the sum of the deals whose
    # currency WAS proven, published as soon as one such deal existed — a
    # partial known-dollar sum that read like a complete total and was consumed
    # as one. The two facts are now separate fields and cannot be confused:
    #
    #   known_revenue_usd  the diagnostic: what the PROVEN deals are worth.
    #                      Always a number when any amount was proven, and what
    #                      reconciliation compares populations with.
    #   revenue_usd        the TOTAL: None whenever any deal in scope has no
    #                      proven amount, because then the total is unknown —
    #                      not zero, not the sum of the rest.
    #
    # A total over zero proven deals is 0.0 only when there were also no
    # unproven ones; otherwise every deal's value is unknown and so is the total.
    known_revenue = (round(revenue, 2)
                     if revenue_deals or not currency_unavailable else None)
    return {
        **scope_descriptor(scope),
        "won_deals": len(rows),
        "known_revenue_usd": known_revenue,
        "revenue_usd": None if currency_unavailable else known_revenue,
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
    # PR-ADS-154C-F3-F1: every scope carries the publishable VERDICT, from
    # `revenue_total_publishable`, beside its arithmetic. The ladder is what
    # every ROAS and business-total consumer reads, so attaching the decision
    # here is what makes it THE decision rather than a function that merely
    # exists — the mart had grown a private copy of the same rule, which is the
    # fifth copy of a rule this programme was consolidating.
    scopes = {}
    for s in SCOPE_ORDER:
        summary = summarize_deals(deals, s)
        verdict = revenue_total_publishable(base, s, summary=summary)
        summary["revenue_total_available"] = verdict["publishable"]
        summary["revenue_total_unavailable_reason"] = verdict["reason"]
        summary["revenue_total_unavailable_detail"] = verdict["detail"]
        summary["revenue_total_violation_codes"] = verdict["violation_codes"]
        scopes[s] = summary
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
    """This row's contribution to a USD total, or ``None`` when it has none
    (PR-ADS-161E: the canonical ``revenue_is_proven`` — a negative amount has
    none)."""
    from analysis.closed_won_truth import revenue_is_proven

    if revenue_is_proven(row):
        return float(row["revenue_usd"])
    return None


def total_verdict_for_population(*, won_deals, unpriced_deals, scope,
                                 population_available: bool = True,
                                 unavailable_reason: str | None = None,
                                 unavailable_detail: str | None = None,
                                 violation_codes=None,
                                 unsummable_parts: int = 0,
                                 parts_label: str = "component") -> dict:
    """THE rule, over ANY closed-won population — not only a lattice scope.

    PR-ADS-155-F1. ``campaign_attributable`` is a scope in
    ``analysis.revenue_scope``, so its verdict came free from the ladder.
    ``country_attributed`` is not: it is a different axis, grouped by the deal's
    own country evidence, and the Countries page had no way to ask the canonical
    contract "is THIS population's total publishable?". It therefore derived its
    contract from geo readiness instead, and published
    ``truth_status: ready`` beside a ``null`` revenue value — a contract that
    contradicts the number it describes.

    This generalises the rule so any consumer holding a scoped population can get
    the SAME verdict, computed the same way, rather than inventing a local
    approximation of it. :func:`_revenue_total_verdict` now delegates here, so
    there is still exactly one implementation.

    Three conditions, in order:

    1. **The population must be readable.** An unreadable one yields the caller's
       own reason — the ledger's, not a substituted one — and counts stay NULL.
    2. **Every deal in it must have a proven amount.** Otherwise the total is
       unknown: not zero, and not the sum of the priced remainder.
    3. **Every PART the caller would sum must itself be summable.**
       ``unsummable_parts`` is for a consumer whose total is a sum of aggregates
       rather than of deals — the Countries page adds up per-country rows. If any
       row withholds its revenue, the page cannot compute the total no matter
       what the deal arithmetic says, and a contract claiming readiness beside a
       value the page could not compute is the exact defect this rule exists to
       prevent.

       By construction the deal count already catches every such case, so this is
       belt and braces. It lives HERE rather than in the page because exactly one
       module decides revenue completeness (PR-ADS-154C-F3-F1 §4): a caller that
       assembled this verdict itself would be a second decider, which the static
       guard in `test_f3f1_4` correctly refuses.
    """
    if not population_available:
        return {
            "publishable": False,
            "reason": unavailable_reason or REASON_LEDGER_UNREADABLE,
            "detail": unavailable_detail,
            "violation_codes": sorted(set(violation_codes or [])),
            # NULL, not 0: an unreadable population proves nothing about how many
            # of its deals are unpriced.
            "currency_unavailable_deals": None,
        }

    unproven = int(unpriced_deals or 0)
    if unproven:
        return {
            "publishable": False,
            "reason": REASON_REVENUE_INCOMPLETE,
            "detail": (
                f"{unproven} of {won_deals} closed-won deal(s) in scope "
                f"'{scope}' have no proven amount, so the window total is "
                "unknown — the sum of the rest is a partial figure and is not "
                "published as the total"),
            "violation_codes": [V_CURRENCY_UNPROVEN_DEALS],
            "currency_unavailable_deals": unproven,
        }

    unsummable = int(unsummable_parts or 0)
    if unsummable:
        return {
            "publishable": False,
            "reason": REASON_REVENUE_INCOMPLETE,
            "detail": (
                f"{unsummable} published {parts_label}(s) in scope '{scope}' "
                "withhold their closed-won revenue, so the total cannot be "
                "summed — the sum of the rest is a partial figure and is not "
                "published as the total"),
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


def _revenue_total_verdict(summary: dict, scope) -> dict:
    """THE rule, over one already-aggregated scope summary.

    Split out so :func:`revenue_total_publishable` and :func:`get_scope_ladder`
    share one implementation rather than one calling the other's shape back into
    existence. Delegates to :func:`total_verdict_for_population`, which is the
    same rule stated over a bare population so non-lattice scopes can use it too.
    """
    return total_verdict_for_population(
        won_deals=summary.get("won_deals"),
        unpriced_deals=summary.get("currency_unavailable_deals") or 0,
        scope=summary.get("scope", scope))


def revenue_total_publishable(base: dict, scope=DEFAULT_SCOPE, *,
                              summary: dict | None = None) -> dict:
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

    ``summary`` lets a caller that has already aggregated this scope — the scope
    ladder does, for every scope — pass it in rather than have it recomputed.
    The rule applied is identical either way; only the arithmetic is reused.
    """
    if not base.get("available"):
        return {
            "publishable": False,
            "reason": base.get("reason") or REASON_LEDGER_UNREADABLE,
            "detail": base.get("detail"),
            "violation_codes": sorted(base.get("violation_codes") or []),
            "currency_unavailable_deals": None,
        }

    if summary is None:
        summary = summarize_deals(base.get("deals") or [], scope)
    verdict = _revenue_total_verdict(summary, scope)
    if not verdict["publishable"]:
        return verdict
    # The scope's own rule publishes; the canonical window verdict may still
    # withhold (a negative or invalid amount anywhere in the window).
    canonical = _canonical_revenue_gate(base)
    if canonical is not None:
        return {**canonical,
                "currency_unavailable_deals": summary.get(
                    "currency_unavailable_deals")}
    return verdict


def _canonical_revenue_gate(base: dict) -> dict | None:
    """PR-ADS-161E: the canonical revenue verdict governs every scope.

    A scope's total is a subset of the window's. When the canonical truth
    withholds the window's revenue (an unproven, negative or invalid amount on
    any member) no scope publishes one either. ``None`` when the canonical
    verdict publishes — the scope's own completeness rule then still applies.
    """
    from analysis import closed_won_truth as cwt

    rev = (base.get("publication") or {}).get("revenue_usd")
    if rev is None or rev.get("status") == cwt.PUBLISHED:
        return None
    return {
        "publishable": False,
        "reason": rev.get("reason") or REASON_REVENUE_INCOMPLETE,
        "detail": ("the canonical closed-won truth withholds this window's "
                   f"revenue ({rev.get('reason')}), so no scope's total "
                   "within it is published"),
        "violation_codes": [V_CURRENCY_UNPROVEN_DEALS],
    }


# ── PR-ADS-155 §5 — the fail-closed revenue disclosure ───────────────────────
#: The label under which the partial sum — and ONLY the partial sum — may appear.
#: It names its own denominator, so it cannot be read as a window total.
KNOWN_REVENUE_LABEL_TEMPLATE = "Known revenue from {revenue_deals} priced deals"


def revenue_disclosure(base: dict, scope=DEFAULT_SCOPE, *,
                       summary: dict | None = None) -> dict:
    """The full, honest picture of one scope's closed-won revenue.

    PR-ADS-155 §5. All-Time revenue is CORRECTLY unavailable: 14 of the 181
    closed-won deals carry no amount in HubSpot, so the window total is unknown.
    The failure mode this block exists to prevent is not the withholding — it is
    presenting the withholding as though nothing were measurable, or presenting
    the $878,324.80 we DID measure as if it were the total.

    Both are avoided by publishing the parts separately, each labelled by what it
    actually counts:

        ``closed_won_deals``           every won deal in scope. Complete.
        ``revenue_proven_deals``       those with a proven amount and currency.
        ``revenue_unavailable_deals``  those without. The actionable number.
        ``known_revenue_usd``          what the PROVEN deals are worth. A real
                                       measurement of a named subset — never a
                                       total, and never labelled as one.
        ``known_revenue_label``        the ONLY caption it may carry, naming its
                                       own denominator.
        ``total_revenue_usd``          NULL whenever any deal is unpriced. Not
                                       zero, not the sum of the rest.
        ``total_revenue_publishable``  the canonical verdict, from
                                       :func:`revenue_total_publishable`.
        ``unavailable_reason`` / ``violation_codes``  why, in machine-readable
                                       form, so every consumer republishes ONE
                                       refusal rather than inventing its own.

    The count is never gated on the total: 181 customers is a complete fact even
    while their combined value is not.
    """
    if not base.get("available"):
        return _disclosure_block(None, revenue_total_publishable(base, scope),
                                 scope=scope, context=base)

    if summary is None:
        summary = summarize_deals(base.get("deals") or [], scope)
    return _disclosure_block(
        summary, revenue_total_publishable(base, scope, summary=summary),
        scope=scope, context=base)


def disclosure_from_ladder(ladder: dict, scope=DEFAULT_SCOPE) -> dict:
    """The same §5 disclosure, built from an already-computed scope ladder.

    Consumers that hold a :func:`get_scope_ladder` result must not re-read the
    ledger to obtain this block, and must not assemble their own version of it
    from the ladder's fields — that is how the mart came to hold a private copy
    of the publishable rule in the first place. The ladder already carries the
    verdict per scope; this reshapes it, and decides nothing.
    """
    if not ladder.get("available"):
        verdict = {
            "publishable": False,
            "reason": ladder.get("reason") or REASON_LEDGER_UNREADABLE,
            "detail": ladder.get("detail"),
            "violation_codes": sorted(ladder.get("violation_codes") or []),
        }
        return _disclosure_block(None, verdict, scope=scope, context=ladder)

    summary = (ladder.get("scopes") or {}).get(normalize_scope(scope)) or {}
    verdict = {
        "publishable": bool(summary.get("revenue_total_available")),
        "reason": summary.get("revenue_total_unavailable_reason"),
        "detail": summary.get("revenue_total_unavailable_detail"),
        "violation_codes": summary.get("revenue_total_violation_codes") or [],
    }
    return _disclosure_block(summary, verdict, scope=scope, context=ladder)


def _disclosure_block(summary, verdict, *, scope, context) -> dict:
    """Shape the §5 fields. Decides nothing — ``verdict`` already decided.

    ``summary`` is ``None`` for a refused read, and then every count is NULL. A
    refused read carries no population, so reporting zeros here would count an
    empty list and publish it as a measurement.
    """
    available = summary is not None
    proven = (summary.get("revenue_deals") or 0) if available else None
    known = summary.get("known_revenue_usd") if available else None
    return {
        "available": available,
        "scope": (summary.get("scope") if available else None) or scope,
        "source": CANONICAL_SOURCE,
        "closed_won_deals": summary.get("won_deals") if available else None,
        "revenue_proven_deals": proven,
        "revenue_unavailable_deals": (summary.get("currency_unavailable_deals")
                                      if available else None),
        "known_revenue_usd": known,
        "known_revenue_label": (
            None if known is None
            else KNOWN_REVENUE_LABEL_TEMPLATE.format(revenue_deals=proven)),
        # The ONE field a page may render under a "total revenue" heading.
        "total_revenue_usd": (summary.get("revenue_usd")
                              if available and verdict["publishable"] else None),
        "total_revenue_publishable": bool(verdict["publishable"]),
        "unavailable_reason": verdict["reason"],
        "unavailable_detail": verdict.get("detail"),
        "violation_codes": verdict["violation_codes"],
        "window": context.get("window"),
        "window_start": context.get("window_start"),
        "window_end": context.get("window_end"),
        "as_of": context.get("as_of"),
        "fallback_used": False,
    }


# ── PR-ADS-155 §6 — the actionable missing-amount report ─────────────────────
AMOUNT_STATUS_MISSING = "missing"
AMOUNT_STATUS_PRESENT = "present"
CURRENCY_CODE_MISSING = "missing"
CURRENCY_CODE_PRESENT = "present"

#: Used only when the ledger stored no reason at all. Every other row reports the
#: canonical ``analysis.deal_currency`` reason VERBATIM. Those reasons already
#: separate the operator actions ("HubSpot never gave us an amount" vs "no FX
#: rate for that close date"), and a second vocabulary here would be exactly the
#: kind of parallel taxonomy this programme exists to remove.
MISSING_REASON_UNRECORDED = "currency_reason_not_recorded"

#: HubSpot's deal record URL. ``0-3`` is HubSpot's own object-type id for deals.
_HUBSPOT_DEAL_URL = "https://app.hubspot.com/contacts/{portal_id}/record/0-3/{deal_id}"


def hubspot_portal_id() -> str | None:
    """The configured HubSpot portal id, or ``None`` when it is not known.

    A record URL is only "safely constructible" when the portal it points into is
    a configured fact. Guessing one would hand an operator a link to somebody
    else's CRM, so an unknown portal yields no URL at all rather than a plausible
    one.
    """
    import os  # noqa: PLC0415

    configured = (os.getenv("HUBSPOT_PORTAL_ID") or "").strip()
    if configured:
        return configured
    try:
        import yaml  # noqa: PLC0415

        with open("config/thresholds.yaml", encoding="utf-8") as fh:
            accounts = (yaml.safe_load(fh) or {}).get("accounts") or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("portal id unavailable from thresholds.yaml: %s", exc)
        return None
    portal = str(accounts.get("hubspot_id") or "").strip()
    return portal or None


def missing_amount_deals(base: dict, scope=DEFAULT_SCOPE) -> dict:
    """Every closed-won deal in scope whose amount could not be proven.

    PR-ADS-155 §6. The revenue total is withheld because of a fixed, small,
    nameable set of source records — 14 of them in production. Withholding
    without naming them leaves an operator with a blocked number and no way to
    unblock it, so the same canonical read that refuses the total also lists
    exactly what has to change, in HubSpot, for it to become publishable.

    Read-only in the strongest sense: this NEVER writes to HubSpot, and it never
    guesses an amount — not from the company, not from the campaign, not from the
    account's previous deals, not from associated contacts, and not by dividing
    the known revenue. A missing amount is reported as missing.
    """
    if not base.get("available"):
        return {
            "available": False,
            "reason": base.get("reason") or REASON_LEDGER_UNREADABLE,
            "detail": base.get("detail"),
            "scope": scope,
            "source": CANONICAL_SOURCE,
            # NULL, not 0: a refused read proves nothing about how many deals
            # are unpriced.
            "deal_count": None,
            "deals": None,
            "window": base.get("window"),
            "window_start": base.get("window_start"),
            "window_end": base.get("window_end"),
            "as_of": base.get("as_of"),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "fallback_used": False,
        }

    scope = normalize_scope(scope)
    portal_id = hubspot_portal_id()
    deals = []
    for row in filter_deals(base.get("deals") or [], scope):
        status = row.get("currency_status") or CURRENCY_UNAVAILABLE
        if summable_revenue(row) is not None:
            continue
        deal_id = row.get("deal_id")
        has_amount = row.get("amount_raw") is not None
        currency_code = row.get("deal_currency_code")
        deals.append({
            "deal_id": deal_id,
            "deal_name": row.get("deal_name"),
            "deal_close_date": row.get("deal_close_date"),
            "deal_stage_label": row.get("deal_stage_label"),
            "amount_status": (AMOUNT_STATUS_PRESENT if has_amount
                              else AMOUNT_STATUS_MISSING),
            # Two separate facts, because they are two separate fixes: HubSpot
            # never gave us a number, versus it gave us a number in a currency we
            # could not convert.
            "currency_code_status": (CURRENCY_CODE_PRESENT if currency_code
                                     else CURRENCY_CODE_MISSING),
            "currency_status": status,
            "currency_code": currency_code,
            # The canonical `analysis.deal_currency` reason, verbatim.
            "reason": row.get("currency_reason") or MISSING_REASON_UNRECORDED,
            # Omitted, not guessed, when the portal is unknown.
            "hubspot_record_url": (
                _HUBSPOT_DEAL_URL.format(portal_id=portal_id, deal_id=deal_id)
                if portal_id and deal_id else None),
            "acquisition_group": row.get("acquisition_group"),
            "canonical_source": CANONICAL_SOURCE,
            # Explicit and always False: no amount on this row came from anywhere
            # but HubSpot, and none was inferred.
            "fallback_used": False,
        })

    deals.sort(key=lambda d: (str(d.get("deal_close_date") or ""),
                              str(d.get("deal_id") or "")))
    return {
        "available": True,
        "reason": None,
        "detail": None,
        "scope": scope,
        "source": CANONICAL_SOURCE,
        "deal_count": len(deals),
        "deals": deals,
        "hubspot_portal_id": portal_id,
        "window": base.get("window"),
        "window_start": base.get("window_start"),
        "window_end": base.get("window_end"),
        "as_of": base.get("as_of"),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "writes_performed": False,
        "fallback_used": False,
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
    "total_verdict_for_population",
    # PR-ADS-155 §5/§6
    "revenue_disclosure", "disclosure_from_ladder",
    "missing_amount_deals", "hubspot_portal_id",
    "KNOWN_REVENUE_LABEL_TEMPLATE", "MISSING_REASON_UNRECORDED",
    "AMOUNT_STATUS_MISSING", "AMOUNT_STATUS_PRESENT",
]
