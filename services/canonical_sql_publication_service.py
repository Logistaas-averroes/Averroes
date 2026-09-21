"""
services/canonical_sql_publication_service.py

PR-ADS-161A-1 — the ONE contract an executive surface may read for SQL.

A consumer asks for a window and a scope and gets back a single verdict. It
never sees raw coverage evidence, never sees the pre-certification
`cpql_publishable` from `analysis.lifecycle_sql_coverage`, and never has to
decide for itself whether a number is safe to show.

    verdict = publication_for(window="7d", window_type="evidence",
                              scope="google_ads_source")

    if verdict["publishable"]:
        show(verdict["complete_sql_total"])          # a certified total
    else:
        show_withheld(verdict["withheld_reason"],    # never 0
                      verdict["explanation"],
                      subset=verdict["confirmed_sql_subset"])

What SQL means here
-------------------
A contact ENTERED HubSpot lifecycle stage `salesqualifiedlead`. The effective
event date is the direct entry property, else a genuine recovered lifecycle
transition, else NULL. Never the creation date, the current stage's timestamp,
the MQL date, the ingestion time or the boundary instant. Dedup is
`contact_id`. This service does not re-implement any of that — it reads the
canonical population through `db.crm_funnel_repository`, whose date doctrine is
the single definition.

Why the reconciliation gate is read rather than computed
--------------------------------------------------------
Publication requires that the headline, detail and operational reads of this
population agree, across all 44 window/scope combinations. Proving that reads
the entire funnel table. On a dashboard request that is not affordable, so the
proof is RECORDED by `scripts/record_sql_reader_reconciliation.py` and read
here with a maximum age.

The consequence is deliberate: with no recorded proof, every window is
withheld. That is the correct state, not a degraded one. Nothing is published
on the assumption that a check nobody ran would have passed.

Failure posture
---------------
Every gate fails closed, and the three refusals stay distinct all the way to
the caller:

    published    a complete, certified total exists
    withheld     we looked, and the evidence does not support a total
    unavailable  we could not look

`complete_sql_total` is `None` in the last two — never `0`. A withheld total is
not a measurement of zero, and a consumer that renders it as one has
reintroduced the defect this whole programme exists to remove.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from analysis import sql_publication as pub

log = logging.getLogger(__name__)

#: Re-exported so consumers can branch on status without importing the
#: pure layer directly (and so the AST guard has one legal import site).
PUBLISHED = pub.PUBLISHED
WITHHELD = pub.WITHHELD
UNAVAILABLE = pub.UNAVAILABLE

EVENT_DATE_BASIS = (
    "effective SQL-entry date: hubspot_contact_funnel.date_entered_sql "
    "(hs_v2_date_entered_salesqualifiedlead), else a recovered lifecycle "
    "transition, else NULL; dedup on contact_id")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def publication_inputs(*, now: datetime | None = None) -> dict[str, Any]:
    """Read every global gate input once. Shared by all windows in a request.

    Kept separate from `publication_for` so a page rendering several windows
    pays for the boundary, incident, freshness and reconciliation reads once
    rather than per window — and so a test can supply them directly.
    """
    from analysis import sql_coverage_freshness as freshness_mod
    from db import crm_funnel_repository as repo

    now = now or _utcnow()

    boundary_state = repo.fetch_active_sql_coverage_boundary()
    boundary_readable = boundary_state.get("available") is True
    boundary = (boundary_state.get("boundary") or {}) if boundary_readable else {}

    incidents = repo.fetch_post_boundary_incidents(status="open")
    incidents_readable = incidents.get("available") is True

    try:
        sync_state = repo.fetch_contact_funnel_sync_state()
        freshness = freshness_mod.assess(sync_state, now=now)
    except Exception as exc:  # noqa: BLE001
        log.error("[sql_publication] freshness unreadable: %s", exc)
        freshness = {"fresh": None, "reason": "source_freshness_unreadable",
                     "detail": "the contact-funnel sync state could not be read"}

    reconciliation = repo.fetch_reader_reconciliation(now=now)

    return {
        "now": now,
        "boundary_readable": boundary_readable,
        "boundary_observed_at": boundary.get("observed_at"),
        "boundary_id": boundary.get("boundary_id"),
        "incidents_readable": incidents_readable,
        # `rows`, not `incidents`: PR-ADS-161A-1 review found this reading a
        # key `fetch_post_boundary_incidents` never returns, so the gate was
        # dead on arrival — always None. A consumer passing that into
        # `window_coverage` got CERT_UNAVAILABLE ("the store could not be
        # read") for a store that read fine, and one writing the natural
        # `or []` would have turned a real open gap into a certified zero.
        # Every other caller in the repository reads `rows`.
        "open_incidents": (incidents.get("rows")
                           if incidents_readable else None),
        "freshness": freshness,
        "reconciliation": reconciliation,
    }


def publication_for(*, window: str, window_type: str, scope: str,
                    coverage: dict | None,
                    inputs: dict | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
    """The publication verdict for one window/scope. The only safe answer.

    `coverage` is this window's `lifecycle_sql_coverage.window_coverage()`
    result. Passing it in keeps this service free of the population read, which
    the caller already performs for its own row building — and keeps the
    decision in one pure place.
    """
    inputs = inputs if inputs is not None else publication_inputs(now=now)

    # Gate on the freshness THIS SERVICE read, not only the caller's copy.
    #
    # Round 3 of the audit: `publication_inputs` performs a real database read
    # for contact-funnel freshness, and this function only stamped the result
    # onto the verdict as a label — `withheld_payload` then dropped it. The
    # gate in `analysis.sql_publication` reads `coverage["source_fresh"]`, a
    # value the CALLER copied in. So a caller whose coverage said fresh, over
    # a service that had just read stale, published:
    #
    #     value: 42, available: True, certified: True,
    #     explanation: "...and the contact-funnel source is fresh"
    #     source_freshness_reason: "source_stale"     <- the same object
    #
    # while `audit_certification` refused the identical inputs. That is the
    # F1 blocker with one more level of indirection, and it made the freshness
    # read a guard whose absence changed nothing.
    #
    # Folded into the coverage the gate reads rather than short-circuited
    # ahead of it, so the pure layer keeps deciding the ORDER of refusals: a
    # window that could not be looked at, or whose readers are unreconciled,
    # must not be reported as stale merely because the source also is. Only
    # `source_fresh` is overridden — overriding `certification_status` too
    # would have relabelled a pre-boundary window as a freshness failure,
    # which is the same defect test_35 pins in the audit.
    service_freshness = inputs.get("freshness") or {}
    service_fresh = service_freshness.get("fresh") is True
    if not service_fresh:
        coverage = {**(coverage or {}), "source_fresh": False}

    verdict = pub.publication_verdict(
        coverage=coverage,
        reconciliation=inputs.get("reconciliation"),
        boundary_readable=inputs.get("boundary_readable"),
        incidents_readable=inputs.get("incidents_readable"),
        window=window, window_type=window_type, scope=scope,
        event_date_basis=EVENT_DATE_BASIS)

    # Name what WE measured, but only on the refusal that is actually about
    # freshness — never over a refusal that outranked it.
    if not service_fresh and verdict.get("withheld_reason") in pub.FRESHNESS_REFUSALS:
        verdict["explanation"] = (
            f"the canonical contact-funnel source is not proven fresh "
            f"({service_freshness.get('reason') or 'freshness unknown'}), so "
            f"no window's completeness can be published over it")

    return _with_provenance(verdict, inputs)


def _with_provenance(verdict: dict, inputs: dict) -> dict[str, Any]:
    """Evidence a reviewer can follow back, on every verdict including refusals.

    A withheld number still has to say what it was judged against.
    """
    verdict["boundary_id"] = inputs.get("boundary_id")
    verdict["boundary_observed_at"] = inputs.get("boundary_observed_at")
    verdict["source_freshness_reason"] = (inputs.get("freshness") or {}).get("reason")
    recon = inputs.get("reconciliation") or {}
    verdict["reconciliation_observed_at"] = recon.get("observed_at")
    verdict["reconciliation_age_hours"] = recon.get("age_hours")
    return verdict


def withheld_payload(verdict: dict) -> dict[str, Any]:
    """The API shape for a SQL value, published or not.

    One shape for both outcomes, so a consumer cannot accidentally serialise a
    withheld total as a number. `value` is `None` unless the total is
    certified; `available` says whether it is; `subset` is always the proven
    confirmed count, under a name that cannot be mistaken for a total.
    """
    publishable = verdict.get("publishable") is True
    return {
        "value": verdict.get("complete_sql_total") if publishable else None,
        "available": publishable,
        "status": verdict.get("status"),
        "reason": verdict.get("withheld_reason"),
        "explanation": verdict.get("explanation"),
        "confirmed_sql_subset": verdict.get("confirmed_sql_subset"),
        "certified": verdict.get("certified"),
        "coverage_complete": verdict.get("coverage_complete"),
        "window": verdict.get("window"),
        "window_type": verdict.get("window_type"),
        "scope": verdict.get("scope"),
        "event_date_basis": verdict.get("event_date_basis"),
        # Carried, not dropped: a consumer deciding what to render needs to
        # know the source was stale, and round 3 found this field missing
        # while the verdict beside it said `published`.
        "source_freshness_reason": verdict.get("source_freshness_reason"),
    }
