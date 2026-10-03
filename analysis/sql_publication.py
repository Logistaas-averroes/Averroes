"""
analysis/sql_publication.py

PR-ADS-161A-1 — the ONE production-facing SQL publication verdict.

Why this module exists
----------------------
`analysis.lifecycle_sql_coverage.window_coverage` answers a NECESSARY question:
could a complete total exist for this window, given the undated population and
the boundary? It sets `cpql_publishable` from that answer alone, and its own
docstring says so: "The final word belongs to `audit_certification`."

That final word lived in `scripts/audit_lifecycle_sql_coverage.py`, coupled to
a CLI `Findings` object. Nothing a product surface could import. So the only
publication flag reachable from production code was the intermediate one — true
for a window the audit would refuse to certify.

No consumer read it yet. PR-ADS-161 migrates executive surfaces onto canonical
lifecycle truth, and the first of them to reach for `window_coverage()` would
have published a total the audit withholds. This module closes that door before
any consumer is migrated.

The contract
------------
`publication_verdict` is the ONLY function permitted to decide that a complete
SQL total or a CPQL may be shown. It takes every gate at once:

    window-local   membership resolved (historical AND prospective)
                   window lies at or after the boundary
                   no open post-boundary gap can belong to it
                   the contact-funnel source is proven fresh
    global         every canonical reader reconciles
                   the boundary store was readable
                   the post-boundary incident store was readable

All of them, or `publishable` is False and `complete_sql_total` is None.

Each gate fails CLOSED. `None` — could not be read — withholds exactly as a
`False` does: an outage must never certify a window. This is the same rule the
rest of the codebase states as "unknown is not zero", applied to publication.

What a consumer gets
--------------------
`confirmed_sql_subset` is ALWAYS present and always truthfully named: the count
of contacts with a PROVEN SQL-entry date in the window. It is never the total,
and a surface may show it while the total is withheld as long as it says which
one it is showing.

`complete_sql_total` is present ONLY when publishable, and is `None` otherwise
— never 0. A withheld total is not a measurement of zero.

This module is pure: no I/O, no database, no clock. `services.
canonical_sql_publication_service` reads the evidence and calls it.
"""

from __future__ import annotations

from typing import Any

# ── Publication statuses ────────────────────────────────────────────────────
PUBLISHED = "published"
#: Every gate passed. `complete_sql_total` is a complete, certified total.

WITHHELD = "withheld"
#: A gate refused. Evidence may exist — `confirmed_sql_subset` still carries
#: what is proven — but no complete total may be claimed.

UNAVAILABLE = "unavailable"
#: A gate could not be READ. Distinct from `withheld`: one is a verdict about
#: the data, the other is a verdict about us. Both refuse to publish, and the
#: explanation must tell them apart.

#: Why publication was refused. Window-local reasons pass through from
#: `lifecycle_sql_coverage` unchanged; these are the ones this layer adds.
WITHHELD_READERS_NOT_RECONCILED = "canonical_readers_did_not_reconcile"
WITHHELD_RECONCILIATION_NOT_PROVEN = "reader_reconciliation_not_proven"
WITHHELD_RECONCILIATION_STALE = "reader_reconciliation_stale"
WITHHELD_RECONCILIATION_PARTIAL = "reader_reconciliation_incomplete_coverage"
WITHHELD_INPUTS_UNREADABLE = "certification_inputs_unreadable"
WITHHELD_COVERAGE_ABSENT = "coverage_verdict_absent"
WITHHELD_COUNT_ABSENT = "coverage_carries_no_counted_population"
WITHHELD_SOURCE_NOT_FRESH = "source_not_proven_fresh"
WITHHELD_COVERAGE_SELF_CONTRADICTORY = "coverage_verdict_self_contradictory"

#: Window-local statuses this module must recognise BY NAME, spelled as
#: literals to keep the pure layer free of `analysis.lifecycle_sql_coverage`
#: (the AST guard in the contract suite forbids that import). Every one of
#: them is a copy of a `CERT_*` constant, so every one can silently drift
#: away from the layer it describes — `test_39` asserts the whole table
#: against the real constants, which is the only thing making the copies
#: safe. Add a literal here, never inline in a comparison.
COVERAGE_STATUS_NO_BOUNDARY = "not_certifiable_no_boundary_established"
COVERAGE_STATUS_UNAVAILABLE = "certification_unavailable"
COVERAGE_STATUS_STALE_SOURCE = "not_certifiable_source_not_fresh"

#: The refusals that are THEMSELVES about freshness — the ONE table every
#: layer consults. Only a reason in here may be replaced by, or replace, a
#: freshness reason; anything else keeps its own, because the operator's next
#: step differs.
FRESHNESS_REFUSALS = (COVERAGE_STATUS_STALE_SOURCE,
                      WITHHELD_SOURCE_NOT_FRESH)

GLOBAL_WITHHELD_REASONS = (
    WITHHELD_READERS_NOT_RECONCILED,
    WITHHELD_RECONCILIATION_NOT_PROVEN,
    WITHHELD_RECONCILIATION_STALE,
    WITHHELD_RECONCILIATION_PARTIAL,
    WITHHELD_INPUTS_UNREADABLE,
    WITHHELD_COVERAGE_ABSENT,
    WITHHELD_COUNT_ABSENT,
    WITHHELD_SOURCE_NOT_FRESH,
    WITHHELD_COVERAGE_SELF_CONTRADICTORY,
)

#: Reasons that mean "could not look", as opposed to "looked and refused".
_UNAVAILABLE_REASONS = (
    WITHHELD_RECONCILIATION_NOT_PROVEN,
    WITHHELD_RECONCILIATION_PARTIAL,
    WITHHELD_INPUTS_UNREADABLE,
    WITHHELD_COVERAGE_ABSENT,
    WITHHELD_COUNT_ABSENT,
)


def reconciliation_gate(reconciliation: dict | None, *,
                        require_full_scope_coverage: bool = True
                        ) -> tuple[bool, str | None]:
    """The global reader-reconciliation gate, and why it refused.

    Three distinct refusals, kept apart because they need different remedies:

      * no record at all       — nobody has proven the readers agree
      * the record is stale    — they agreed, but too long ago to rely on
      * they did not reconcile — they were checked and they disagreed

    A missing record is NOT "probably fine". Publication requires proof that
    the canonical readers agree, and the absence of a check is the absence of
    proof.
    """
    if not reconciliation:
        return False, WITHHELD_RECONCILIATION_NOT_PROVEN
    if reconciliation.get("available") is not True:
        return False, WITHHELD_RECONCILIATION_NOT_PROVEN
    # Order matters: report the MOST specific true thing. A record that was
    # never written is "not proven", not "stale" — staleness is a property of
    # a record that exists.
    complete = reconciliation.get("reconciliation_complete")
    if complete is False:
        return False, WITHHELD_READERS_NOT_RECONCILED
    if complete is not True:
        return False, WITHHELD_RECONCILIATION_NOT_PROVEN
    if reconciliation.get("stale") is not False:
        # `is not False`, not `is True`: unknown staleness withholds exactly
        # as stale does. This module's own contract says `None` refuses like
        # `False`, and an asymmetry here would be the one gate that fails open.
        return False, WITHHELD_RECONCILIATION_STALE
    if (require_full_scope_coverage
            and reconciliation.get("all_combinations_compared") is not True):
        # PUBLICATION claims "every canonical reader reconciles". A record in
        # which some combinations were never comparable does not support that.
        # The ones that drop out are the identity-dependent scopes
        # (campaign_attributable, keyword_attributable) when the Google Ads
        # campaign-identity contract is unavailable — so a narrower scope
        # would be published as proven on evidence gathered for a wider one,
        # silently crossing the attribution nesting.
        #
        # The AUDIT passes False here, and that is a real difference rather
        # than a hidden one. Its `reconciliation_complete` is documented to
        # mean "every combination reached a proven outcome and every
        # COMPARABLE one agreed"; a pair that failed closed by contract is not
        # comparable and has never blocked its certification. Requiring it
        # there would mean the audit could not certify anything while campaign
        # identity is unavailable, which is a different decision from this
        # one and is not PR-ADS-161A-1's to make.
        #
        # Blocking every scope when any scope is uncomparable is conservative:
        # it withholds `all_source` too, which was comparable. The precise fix
        # is to record per-scope outcomes and require the REQUESTED scope, and
        # that belongs with the consumers that will request them.
        return False, WITHHELD_RECONCILIATION_PARTIAL
    return True, None


def publication_verdict(*, coverage: dict | None,
                        reconciliation: dict | None,
                        boundary_readable: bool | None,
                        incidents_readable: bool | None,
                        window: str | None = None,
                        window_type: str | None = None,
                        scope: str | None = None,
                        event_date_basis: str | None = None,
                        require_full_scope_coverage: bool = True
                        ) -> dict[str, Any]:
    """The single publication verdict for one window/scope. Fails closed.

    `coverage` is a `lifecycle_sql_coverage.window_coverage()` result. Its
    `cpql_publishable` is deliberately NOT propagated: this function recomputes
    publication from `certification_eligible` plus the global gates, so a caller
    cannot reach the intermediate value through the returned dict.
    """
    if not isinstance(coverage, dict) or not coverage:
        return _refused(UNAVAILABLE, WITHHELD_COVERAGE_ABSENT,
                        "no coverage verdict was produced for this window, so "
                        "its completeness is unknown — not complete, not zero",
                        coverage={}, window=window, window_type=window_type,
                        scope=scope, event_date_basis=event_date_basis,
                        readers_reconciled=None)

    reconciled, recon_reason = reconciliation_gate(
        reconciliation,
        require_full_scope_coverage=require_full_scope_coverage)

    # Window-local half. `certification_eligible` already encodes: boundary
    # exists, window opens at or after it, no open gap belongs to it,
    # membership resolved, source proven fresh.
    locally_eligible = coverage.get("certification_eligible") is True

    stores_readable = boundary_readable is True and incidents_readable is True

    if not locally_eligible:
        reason = coverage.get("certification_status") or WITHHELD_COVERAGE_ABSENT

        # ROUND 4, BLOCKER — an unread boundary store was published as the
        # affirmative claim "no coverage boundary exists".
        #
        # `publication_inputs` sets `boundary_observed_at = None` whenever the
        # store is unreadable, so a caller building its coverage from those
        # inputs — the only caller shape the service supports — hands
        # `window_coverage` a missing boundary and gets `CERT_NO_BOUNDARY`
        # back. That fired HERE, before the readability gate below was ever
        # evaluated, and `withheld_payload` drops `boundary_id` and
        # `boundary_observed_at`, so the two states were byte-identical at
        # every surface:
        #
        #     store unreadable   -> withheld  not_certifiable_no_boundary_established
        #     no boundary yet    -> withheld  not_certifiable_no_boundary_established
        #
        # During a boundary-store outage every SQL surface stated a permanent,
        # benign, nothing-to-do condition and an operator would wait it out.
        # `false` and `null` are different claims; this collapsed them, and
        # it made `boundary_readable` a parameter that changed nothing on any
        # input `publication_inputs` can actually produce.
        if reason == COVERAGE_STATUS_NO_BOUNDARY and boundary_readable is not True:
            return _refused(UNAVAILABLE, WITHHELD_INPUTS_UNREADABLE,
                            "the coverage boundary store could not be read, so "
                            "it is unknown whether a boundary exists — that is "
                            "not the same claim as no boundary having been "
                            "established yet",
                            coverage=coverage, window=window,
                            window_type=window_type, scope=scope,
                            event_date_basis=event_date_basis,
                            readers_reconciled=reconciled)

        # ROUND 4, BLOCKER — freshness is gated in TWO places, and the
        # window-local one made the reordering below inert.
        #
        # `_certification` refuses an otherwise-perfect window with
        # `CERT_STALE_SOURCE`, so on every coherent production input a stale
        # source was reported HERE, at step 1 — ahead of the readability and
        # reconciliation gates that the round-3 reordering was written to put
        # in front of it. Measured before this fix, with the source stale:
        #
        #     stale + reconciliation record absent -> not_certifiable_source_not_fresh
        #     stale + readers disagreed            -> not_certifiable_source_not_fresh
        #     stale + boundary store unreadable    -> not_certifiable_source_not_fresh
        #
        # Today no reconciliation record exists at all (the recorder has no
        # scheduled home), so a stale sync would have sent an operator to fix
        # the pipeline while the refusal that actually blocks every window
        # went unreported.
        #
        # Deferred rather than returned: fall through to the global gates and
        # let the freshness gate below catch it, which preserves this window's
        # own, more specific reason. Guarded on `source_fresh is not True` so
        # the fall-through cannot reach the publishing branch — an incoherent
        # dict claiming both a stale status and a fresh source keeps the
        # immediate refusal.
        defer_to_global_gates = (reason in FRESHNESS_REFUSALS
                                 and coverage.get("source_fresh") is not True)

        if not defer_to_global_gates:
            status = (UNAVAILABLE
                      if coverage.get("certification_eligible") is None
                      or reason == COVERAGE_STATUS_UNAVAILABLE
                      else WITHHELD)
            return _refused(status, reason,
                            coverage.get("certification_explanation")
                            or coverage.get("explanation") or "",
                            coverage=coverage, window=window,
                            window_type=window_type, scope=scope,
                            event_date_basis=event_date_basis,
                            readers_reconciled=reconciled)

    if not stores_readable:
        return _refused(UNAVAILABLE, WITHHELD_INPUTS_UNREADABLE,
                        "the boundary or post-boundary incident store could "
                        "not be read, so certification is unknown — unknown "
                        "inputs must block",
                        coverage=coverage, window=window,
                        window_type=window_type, scope=scope,
                        event_date_basis=event_date_basis,
                        readers_reconciled=reconciled)

    if not reconciled:
        status = (UNAVAILABLE if recon_reason in _UNAVAILABLE_REASONS
                  else WITHHELD)
        return _refused(status, recon_reason,
                        _RECON_EXPLANATIONS[recon_reason],
                        coverage=coverage, window=window,
                        window_type=window_type, scope=scope,
                        event_date_basis=event_date_basis,
                        readers_reconciled=False)

    # Freshness is a gate HERE, not only inside `certification_eligible`.
    # F1 added an independent freshness check to the audit and not to
    # production, so the audit became the better-defended caller — and the
    # audit is what we point at to describe what production publishes.
    #
    # Placed AFTER the readability and reconciliation gates on purpose: those
    # answer "could we look at all", and a `withheld` verdict must not
    # displace an `unavailable` one. Round 3 caught the first version doing
    # exactly that.
    if coverage.get("source_fresh") is not True:
        # NOT the window's `certification_status`: on the only shape
        # `window_coverage` can emit with `certification_eligible: True`,
        # that status is literally `"eligible"` — so the refusal would be
        # served to a consumer under a reason meaning "every prerequisite is
        # met". Use the window's own reason only when it is itself about
        # freshness.
        window_status = coverage.get("certification_status")
        reason = (window_status
                  if window_status in FRESHNESS_REFUSALS
                  else WITHHELD_SOURCE_NOT_FRESH)
        return _refused(WITHHELD, reason,
                        "the canonical contact-funnel source is not proven "
                        "fresh, so this window's completeness describes data "
                        "that may have stopped arriving",
                        coverage=coverage, window=window,
                        window_type=window_type, scope=scope,
                        event_date_basis=event_date_basis,
                        readers_reconciled=True)

    # ROUND 4, MAJOR — `certification_eligible: True` beside
    # `window_total_complete: False` published the confirmed subset as a
    # certified COMPLETE total:
    #
    #     value: 42, available: True, certified: True, coverage_complete: False
    #
    # That pair is PR-ADS-160 §2's defect verbatim, in one object. It cannot
    # come out of `window_coverage` — `_certification` requires `complete`
    # before it returns `CERT_ELIGIBLE` — so it only arises from a
    # caller-built dict, which is exactly the seam `publication_for` exposes
    # and the reason `test_24` exists for the sibling missing-count case. The
    # PR guarded "eligible with no count" (a blank) and left "eligible with
    # unresolved membership" (a WRONG NUMBER) open.
    #
    # An internally contradictory coverage dict is evidence of a caller bug,
    # so it fails closed under its own reason rather than being silently
    # resolved in either direction.
    if coverage.get("window_total_complete") is not True:
        return _refused(UNAVAILABLE, WITHHELD_COVERAGE_SELF_CONTRADICTORY,
                        "this window is marked certifiable while its undated "
                        "membership is not resolved — the two cannot both be "
                        "true, so no total is published",
                        coverage=coverage, window=window,
                        window_type=window_type, scope=scope,
                        event_date_basis=event_date_basis,
                        readers_reconciled=True)

    # A gate cannot certify a number that is not there. `publication_for`
    # takes `coverage` from its CALLER, so an absent count is reachable even
    # though `window_coverage` always sets one — and `available: true` beside
    # `value: null` is exactly the shape a consumer renders as a blank total.
    if coverage.get("confirmed_sqls") is None:
        return _refused(UNAVAILABLE, WITHHELD_COUNT_ABSENT,
                        "every gate passed but the window carries no counted "
                        "population, so there is no total to publish",
                        coverage=coverage, window=window,
                        window_type=window_type, scope=scope,
                        event_date_basis=event_date_basis,
                        readers_reconciled=True)

    # Every gate passed. This is the ONLY return that publishes a total.
    return {
        **_common(coverage, window, window_type, scope, event_date_basis),
        "status": PUBLISHED,
        "publishable": True,
        "complete_sql_total": coverage.get("confirmed_sqls"),
        "cpql_publishable": True,
        "withheld_reason": None,
        "explanation": coverage.get("certification_explanation") or "",
        "certified": True,
        "readers_reconciled": True,
    }


_RECON_EXPLANATIONS = {
    WITHHELD_RECONCILIATION_NOT_PROVEN: (
        "no proven canonical reader reconciliation is on record, so it is "
        "unknown whether the headline, detail and operational reads of this "
        "population agree — an unproven agreement is not an agreement"),
    WITHHELD_RECONCILIATION_STALE: (
        "the canonical readers last reconciled too long ago to be relied on; "
        "the population has moved since they were compared"),
    WITHHELD_RECONCILIATION_PARTIAL: (
        "the recorded reconciliation did not compare every window and scope, "
        "so the requested scope may never have been checked — a narrower "
        "number must not be published on evidence gathered for a wider one"),
    WITHHELD_READERS_NOT_RECONCILED: (
        "the canonical readers of this population do not agree, so no single "
        "number can be published for it"),
}


def _common(coverage, window, window_type, scope, event_date_basis) -> dict:
    """Fields every verdict carries, published or not."""
    return {
        "window": window if window is not None else coverage.get("window"),
        "window_type": window_type,
        "scope": scope,
        "event_date_basis": event_date_basis,
        # ALWAYS present, always a subset, never renamed to look like a total.
        "confirmed_sql_subset": coverage.get("confirmed_sql_subset",
                                             coverage.get("confirmed_sqls")),
        "recovered_in_subset": coverage.get("window_membership_recovered"),
        "coverage_complete": coverage.get("window_total_complete"),
        "coverage_reason": coverage.get("reason"),
        "certification_status": coverage.get("certification_status"),
        "source_fresh": coverage.get("source_fresh"),
        "window_after_boundary": coverage.get("window_after_boundary"),
        "open_post_boundary_gaps": coverage.get("open_post_boundary_gaps"),
    }


def _refused(status, reason, explanation, *, coverage, window, window_type,
             scope, event_date_basis, readers_reconciled) -> dict:
    """A refusal. No complete total, no CPQL, and `None` rather than `0`."""
    return {
        **_common(coverage, window, window_type, scope, event_date_basis),
        "status": status,
        "publishable": False,
        # Not zero. A withheld total is not a measurement.
        "complete_sql_total": None,
        "cpql_publishable": False,
        "withheld_reason": reason,
        "explanation": explanation,
        "certified": False,
        "readers_reconciled": readers_reconciled,
    }
