#!/usr/bin/env python3
"""
scripts/audit_lifecycle_sql_coverage.py

PR-ADS-159 §8 — READ-ONLY audit of lifecycle SQL evidence coverage.

    python -m scripts.audit_lifecycle_sql_coverage
    python -m scripts.audit_lifecycle_sql_coverage --json
    python -m scripts.audit_lifecycle_sql_coverage --strict
    echo $?

Two different questions, never merged
-------------------------------------
``audit_complete``    the audit itself ran and every check it makes returned an
                      answer. This is about the AUDIT.
``coverage_complete`` every window's lifecycle-SQL population is fully dated, so
                      a complete total may be published. This is about the DATA.

They are reported separately and drive different exits. An audit that runs
perfectly over incomplete data is a successful audit — it is doing its job by
saying the data is incomplete. Requiring zero unresolved contacts before the
command will produce a report would mean no report exists precisely while the
gap is being worked, which is when it is needed most.

Exit codes
----------
    0  the audit ran and its contract checks passed (coverage may still be
       incomplete — read ``coverage_complete``)
    1  a contract check FAILED: the code is inconsistent with its own doctrine
    2  the audit could not run — database or canonical population unavailable
    3  ``--strict`` only: the audit ran and passed, but SQL coverage is
       incomplete

Guarantees
----------
* No writes of any kind, local or external. Every access is a SELECT through
  read-only repositories.
* No HubSpot, Google Ads or Mailchimp call. Nothing here contacts an external
  API; the recovery command is the only thing that reads HubSpot, and this is
  not it.
* No contact PII in the output. Contacts appear only as counts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2
EXIT_COVERAGE_INCOMPLETE = 3

_REPO_FILE = _ROOT / "db" / "crm_funnel_repository.py"

#: The canonical reads that must all filter on the SAME effective SQL-entry
#: expression. Listed by name so a new reader that quietly reintroduces the bare
#: column is a check failure rather than an unnoticed divergence.
_EFFECTIVE_DATE_READERS = (
    "fetch_funnel_contacts",
    "fetch_funnel_contact_page",
    "fetch_operational_status_counts",
    "fetch_sql_recovery_candidates",
    "fetch_sql_coverage_population",
)


class Findings:
    """Contract violations and unavailability, kept apart.

    A violation means the code contradicts its own doctrine. Unavailability
    means the audit could not look. Merging them would make an outage look like
    a defect, and let a defect hide behind an outage.
    """

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.unavailable: list[str] = []
        self.checks: list[dict] = []

    def violation(self, name: str, detail: str) -> None:
        self.violations.append(f"{name}: {detail}")
        self.checks.append({"check": name, "ok": False, "detail": detail})

    def unavailable_now(self, name: str, detail: str) -> None:
        self.unavailable.append(f"{name}: {detail}")
        self.checks.append({"check": name, "ok": False, "detail": detail})

    def passed(self, name: str, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": True, "detail": detail})

    @property
    def exit_code(self) -> int:
        if self.violations:
            return EXIT_VIOLATION
        if self.unavailable:
            return EXIT_UNAVAILABLE
        return EXIT_OK


# ─────────────────────────────────────────────────────────────────────────────
# Static check — one effective-date doctrine, everywhere
# ─────────────────────────────────────────────────────────────────────────────

def _function_source(path: Path, name: str) -> str | None:
    import ast

    try:
        src = path.read_text()
        tree = ast.parse(src)
    except Exception:  # noqa: BLE001
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.get_source_segment(src, node)
    return None


def check_effective_date_consistency(f: Findings) -> dict:
    """Every canonical SQL read must use the ONE shared effective expression.

    Before PR-ADS-159 the headline coalesced the recovered date and the detail
    page did not, so the same window produced two different populations. A
    static check is the right instrument: the divergence is invisible at runtime
    until a contact is actually recovered, and by then two published numbers
    already disagree.
    """
    out: dict = {"readers": {}, "doctrine": None}
    try:
        from db import crm_funnel_repository as repo

        out["doctrine"] = repo.EFFECTIVE_DATE_DOCTRINE
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now("effective_date_doctrine",
                          f"the funnel repository could not be imported: {exc}")
        return out

    for name in _EFFECTIVE_DATE_READERS:
        src = _function_source(_REPO_FILE, name)
        if src is None:
            f.violation("effective_date_consistency",
                        f"{name} could not be located in the funnel repository "
                        "— the audit cannot certify a reader it cannot find")
            out["readers"][name] = "not_found"
            continue
        uses_shared = "effective_date_sql(" in src or "_effective_date_sql(" in src
        uses_bare = "EVENT_DATE_COLUMN[event]" in src
        out["readers"][name] = "shared" if uses_shared else "bare"
        if not uses_shared:
            f.violation(
                "effective_date_consistency",
                f"{name} does not use the shared effective SQL-entry "
                "expression, so it selects a different population from the "
                "headline for the same window")
        elif uses_bare:
            f.violation(
                "effective_date_consistency",
                f"{name} still resolves a bare stage-entry column alongside "
                "the shared expression")

    if not f.violations:
        f.passed("effective_date_consistency",
                 f"{len(_EFFECTIVE_DATE_READERS)} canonical reads share one "
                 "effective SQL-entry expression")

    # The precedence itself, asserted rather than assumed.
    try:
        from analysis.crm_lifecycle import EVENT_SQL

        expr = repo.effective_date_sql(EVENT_SQL)
        out["expression"] = expr
        if not expr.startswith("COALESCE(f.date_entered_sql,"):
            f.violation("effective_date_precedence",
                        f"the direct HubSpot property is not first in {expr!r} "
                        "— recovery must fill a gap, never override a fact")
        else:
            f.passed("effective_date_precedence",
                     "direct property first, recovered history second, then NULL")
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now("effective_date_precedence", str(exc))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Live checks
# ─────────────────────────────────────────────────────────────────────────────

def _in_window(value, start, end) -> bool:
    if value is None:
        return False
    day = value.date() if isinstance(value, datetime) else value
    if start is not None and day < (start.date() if isinstance(start, datetime)
                                    else start):
        return False
    if end is not None and day > (end.date() if isinstance(end, datetime) else end):
        return False
    return True


def audit_population(f: Findings) -> dict:
    """The GLOBAL lifecycle-SQL population, split by evidence provenance."""
    from db import crm_funnel_repository as repo

    population = repo.fetch_sql_coverage_population()
    if not population.get("available"):
        f.unavailable_now("sql_population",
                          "the lifecycle-SQL population could not be read")
        return {"available": False}

    candidates = population.get("candidates")
    direct = population.get("direct")
    recovered = population.get("recovered")
    unresolved = population.get("unresolved")

    parts = [direct, recovered, unresolved]
    if all(p is not None for p in parts) and candidates is not None:
        if sum(parts) != candidates:
            f.violation("population_partition",
                        f"direct({direct}) + recovered({recovered}) + "
                        f"unresolved({unresolved}) != candidates({candidates})")
        else:
            f.passed("population_partition",
                     f"{candidates} lifecycle-SQL contacts partition into "
                     f"{direct} direct, {recovered} recovered, "
                     f"{unresolved} unresolved")
    return {"available": True, **population}


def audit_boundary(f: Findings) -> dict:
    """PR-ADS-160 — the boundary, the bounded population, and the open gaps.

    Reports the historical and prospective sides SEPARATELY. Before this PR
    there was one undated population and one number; after it, "a date HubSpot
    does not hold" and "a date we failed to capture" are different findings with
    different remedies, and adding them would hide the second inside the first.
    """
    from db import crm_funnel_repository as repo

    state = repo.fetch_active_sql_coverage_boundary()
    if not state.get("available"):
        f.unavailable_now("sql_coverage_boundary",
                          "the coverage-boundary store could not be read, so "
                          "no window's certification can be assessed")
        return {"available": False, "boundary": None,
                "boundary_established": None,
                "open_post_boundary_incidents": None,
                "open_incidents": None,
                "post_boundary_incidents_available": False}

    boundary = state.get("boundary")
    incidents = repo.fetch_post_boundary_incidents(status="open")
    if not incidents.get("available"):
        f.unavailable_now("post_boundary_incidents",
                          "the post-boundary incident store could not be read; "
                          "a window must not certify while its blockers are "
                          "invisible")
        open_count = None
    else:
        open_count = incidents.get("open_count")

    if boundary is None:
        # Not a violation and not an outage: the boundary simply has not been
        # established yet. Certification is unavailable, and says so.
        f.passed("sql_coverage_boundary",
                 "no coverage boundary is established yet, so no window is "
                 "certifiable — this is a state, not a failure")
    else:
        f.passed("sql_coverage_boundary",
                 f"boundary {boundary.get('boundary_id')} observed at "
                 f"{boundary.get('observed_at')}, bounding "
                 f"{boundary.get('contacts_bounded')} legacy undated contact(s)")

    rows = incidents.get("rows") or []
    by_reason: dict = {}
    for row in rows:
        key = row.get("reason")
        by_reason[key] = by_reason.get(key, 0) + 1

    return {
        "available": True,
        # PR-ADS-160 §6: the incidents themselves, so each window resolves
        # membership against its own bounds instead of one global count.
        "open_incidents": rows if incidents.get("available") else None,
        "boundary": boundary,
        "boundary_established": boundary is not None,
        "boundary_id": (boundary or {}).get("boundary_id"),
        "boundary_observed_at": (boundary or {}).get("observed_at"),
        "legacy_undated_bounded": (boundary or {}).get("contacts_bounded"),
        # NULL, never 0, when the store could not be read.
        "open_post_boundary_incidents": open_count,
        "post_boundary_incidents_available": bool(incidents.get("available")),
        "post_boundary_incident_reasons": dict(sorted(by_reason.items())),
    }


def audit_source_freshness(f: Findings) -> dict:
    """PR-ADS-160 §5 — is the canonical contact-funnel source still updating?

    A certification prerequisite, not a footnote. A window can be complete and
    still worthless if its source stopped being fed: it would be complete with
    respect to data that has stopped arriving.
    """
    from analysis import sql_coverage_freshness as freshness
    from db import crm_funnel_repository as repo

    state = repo.fetch_contact_funnel_sync_state()
    verdict = freshness.assess(state)

    if verdict["fresh"] is True:
        f.passed("source_freshness", verdict["detail"])
    elif verdict["fresh"] is None:
        f.unavailable_now("source_freshness", verdict["detail"])
    else:
        # A stale or failed pipeline is a real finding about the DATA, like an
        # incomplete window — not a contract violation and not an audit outage.
        f.passed("source_freshness",
                 f"NOT FRESH ({verdict['reason']}): {verdict['detail']} — no "
                 f"window may certify")
    return verdict


def audit_certification(f: Findings, windows: list, boundary: dict,
                        reconciliation: dict,
                        freshness: dict | None = None) -> dict:
    """Which windows may be certified — the whole gate, not the window-local half.

    ``analysis.lifecycle_sql_coverage`` judges the window against the boundary
    and the evidence population. Two further conditions are global and are
    applied here:

      * every canonical reader must reconcile (all 44 combinations);
      * the canonical contact-funnel source must be proven FRESH;
      * the audit itself must have been able to look.

    A window that is locally eligible is NOT certified while either fails.
    Certification is a claim that a published number is trustworthy, so every
    input to it must be proven, not merely un-contradicted.
    """
    from analysis import lifecycle_sql_coverage as coverage
    from analysis import sql_publication as pub

    reconciled = bool(reconciliation.get("reconciliation_complete"))
    boundary_readable = bool(boundary.get("available"))
    incidents_readable = bool(boundary.get("post_boundary_incidents_available"))
    source_fresh = (freshness or {}).get("fresh") is True

    # PR-ADS-161A-1. The decision itself now lives in `analysis.sql_publication`,
    # which production imports. It used to live only here, so the sole
    # publication flag a product surface could reach was the PRE-certification
    # one from `window_coverage` — true for windows this gate refuses. One
    # decision, two callers: if they could drift, the audit would stop
    # describing what production publishes.
    #
    # Reconciliation was computed live immediately above, so it is available
    # and not stale by construction; the staleness arm of the gate exists for
    # the production reader, which consults a recorded verdict.
    recon_state = {"available": True, "stale": False,
                   "reconciliation_complete": reconciled}

    def _withhold(win, label, reason):
        """A blocked window publishes NO complete total and NO CPQL.

        PR-ADS-160 §2 — the defect this closes: a window could report
        `certified: False` and, in the same response, `complete_sql_total: 42`
        and `cpql_publishable: true`. Whoever read the number rather than the
        flag got an incomplete total presented as a complete one. Certification
        is the LAST gate, so it must be able to take both back.

        The confirmed dated subset stays visible under `confirmed_sql_subset` —
        a name that says what it is.
        """
        blocked.append({"window": label, "reason": reason})
        win["certified"] = False
        win["certification_status"] = reason
        win["cpql_publishable"] = False
        win["complete_sql_total"] = None

    certified, blocked = [], []
    for win in windows or []:
        label = f"{win.get('window_type')}/{win.get('window')}"
        verdict = pub.publication_verdict(
            coverage=win, reconciliation=recon_state,
            boundary_readable=boundary_readable,
            incidents_readable=incidents_readable,
            window=win.get("window"), window_type=win.get("window_type"))

        if verdict["publishable"]:
            certified.append(label)
            win["certified"] = True
            continue

        reason = verdict["withheld_reason"]
        # One deliberate difference from the shared gate's own wording: where
        # the source is not fresh, the audit has always reported the FRESHNESS
        # reason (`source_stale`, `source_last_incremental_failed`, …) rather
        # than the window's `not_certifiable_source_not_fresh`, because the
        # operator's next step is the pipeline, not the window. The refusal is
        # identical; only the label an operator reads is more specific.
        if (win.get("certification_status") == coverage.CERT_STALE_SOURCE
                and not source_fresh):
            reason = (freshness or {}).get("reason") or "source_not_fresh"
        _withhold(win, label, reason)

    if certified:
        f.passed("window_certification",
                 f"{len(certified)} window(s) certified: {', '.join(certified)}")
    else:
        f.passed("window_certification",
                 "no window is certified — every window either opens before the "
                 "boundary, carries unresolved membership, or has an unmet "
                 "global prerequisite")

    return {
        "certified_windows": certified,
        "blocked_windows": blocked,
        "windows_certified": len(certified),
        "windows_assessed": len(windows or []),
        "readers_reconciled": reconciled,
        "boundary_readable": boundary_readable,
        "incidents_readable": incidents_readable,
        "source_fresh": source_fresh,
        "source_freshness_reason": (freshness or {}).get("reason"),
    }


def audit_windows(f: Findings, population: dict, now: datetime,
                  boundary: dict | None = None,
                  open_incidents=None, freshness: dict | None = None
                  ) -> list[dict]:
    """Per-window coverage — the global gap resolved against each window ONCE."""
    from analysis import lifecycle_sql_coverage as coverage
    from analysis.crm_lifecycle import EVENT_DATE_COLUMN, EVENT_SQL
    from db import crm_funnel_repository as repo
    from scripts.audit_sql_doctrine_inventory import resolve_all_windows
    from services import canonical_contact_outcome_service as canon

    windows = resolve_all_windows(canon, now)
    # PR-ADS-160: both temporal bounds, not just creation. The boundary's upper
    # bound is what lets a window opening after it stop carrying the historical
    # undated population. It is read here as a BOUND and never as a date.
    unresolved_rows = repo.fetch_unresolved_sql_boundary_bounds(
        boundary_id=(boundary or {}).get("boundary_id"))
    if not unresolved_rows.get("available"):
        f.unavailable_now("window_membership",
                          "the undated lifecycle-SQL contacts could not be read")
        rows, rows_available = [], False
    else:
        rows, rows_available = unresolved_rows.get("rows") or [], True

    boundary_observed = (boundary or {}).get("observed_at")

    contacts = repo.fetch_all_funnel_contacts()
    if not contacts.get("available"):
        f.unavailable_now("window_confirmed_counts",
                          "the canonical contact store could not be read")
        return [{"window": w.get("window_key"),
                 "window_type": w.get("window_type"),
                 "window_total_complete": None, "available": False}
                for w in windows]

    col = EVENT_DATE_COLUMN[EVENT_SQL]
    flag = f"{col}_from_history"
    out = []
    for win in windows:
        start, end = win.get("start"), win.get("end")
        in_window = [r for r in (contacts.get("rows") or [])
                     if _in_window(r.get(col), start, end)]
        confirmed = len(in_window)
        recovered = sum(1 for r in in_window if r.get(flag))
        block = coverage.window_coverage(
            window=win.get("window_key"), window_end=end,
            confirmed_sqls=confirmed, recovered_sqls=recovered,
            unresolved_rows=rows, population_available=rows_available,
            window_start=start, boundary_observed_at=boundary_observed,
            open_post_boundary_incidents=open_incidents,
            freshness=freshness)
        block["window_start"] = str(start) if start else None
        block["window_end"] = str(end) if end else None
        block["window_type"] = win.get("window_type")

        # The fail-closed contract, checked rather than trusted.
        if block["complete_sql_total"] is not None \
                and not block["window_total_complete"]:
            f.violation(f"fail_closed[{win.get('window_key')}]",
                        "a complete SQL total was published for a window whose "
                        "membership is unresolved")
        if block["cpql_publishable"] and not block["window_total_complete"]:
            f.violation(f"cpql_fail_closed[{win.get('window_key')}]",
                        "CPQL was declared publishable without a complete total")
        # PR-ADS-160 §2 — the same guard on the prospective half. A window whose
        # incident store is unreadable, or which an open incident could belong
        # to, has NOT got a complete total however resolved its history is.
        if block["complete_sql_total"] is not None \
                and not block["prospective_membership_complete"]:
            f.violation(f"prospective_fail_closed[{win.get('window_key')}]",
                        "a complete SQL total was published for a window whose "
                        "post-boundary gap membership is unresolved or unknown")
        out.append(block)

    if not any(k.startswith("fail_closed") or k.startswith("cpql_fail_closed")
               for k in (c["check"] for c in f.checks if not c["ok"])):
        f.passed("fail_closed",
                 f"{len(out)} window(s): a complete total and CPQL are offered "
                 "only where membership is fully resolved")

    # The global gap is reported ONCE, not once per window.
    if rows_available and population.get("available"):
        stated = population.get("unresolved")
        if stated is not None and stated != len(rows):
            f.violation("global_gap_consistency",
                        f"the population reports {stated} unresolved contacts "
                        f"but {len(rows)} were listed")
        else:
            f.passed("global_gap_consistency",
                     f"{len(rows)} undated lifecycle-SQL contact(s), counted "
                     "once globally and resolved against each window")
    return out


def audit_read_reconciliation(f: Findings, now: datetime) -> dict:
    """Headline, detail page and operational counts, for EVERY window and scope.

    They are three reads of the same population. Before PR-ADS-159 §5 they used
    two different date expressions, so a recovered contact appeared in one and
    not the others — a disagreement no single number could reveal.

    PR-ADS-159-R5: the first cut checked ``all_time`` with no attribution scope,
    which is one of forty-four combinations, and then the PR claimed the three
    reads reconcile everywhere. They now all run: every resolved lifecycle window
    against every canonical scope.

    The scope allow-lists come from the canonical service's OWN
    ``resolve_population_filters`` and its own ``_build_campaign_resolver``. The
    audit must not re-implement attribution classification: a second copy that
    agreed with the first would prove nothing, and one that disagreed would
    report the audit's bug as the product's.
    """
    from analysis.crm_lifecycle import EVENT_DATE_COLUMN, EVENT_SQL
    from db import crm_funnel_repository as repo
    from scripts.audit_sql_doctrine_inventory import resolve_all_windows
    from services import canonical_contact_outcome_service as canon
    from services import canonical_crm_funnel_service as funnel

    windows = resolve_all_windows(canon, now)
    contacts = repo.fetch_all_funnel_contacts()
    if not contacts.get("available"):
        f.unavailable_now("read_reconciliation",
                          "the canonical contact store could not be read, so no "
                          "window or scope could be reconciled")
        return {"available": False, "combinations": 0,
                "combinations_expected": None, "combinations_compared": 0,
                "combinations_data_unavailable": None,
                "combinations_execution_unavailable": None,
                "all_combinations_compared": False,
                "reconciliation_complete": False, "results": []}

    all_rows = contacts.get("rows") or []
    col = EVENT_DATE_COLUMN[EVENT_SQL]
    results: list[dict] = []
    compared = 0

    for win in windows:
        start, end = win.get("start"), win.get("end")
        # The window's own rows, through the canonical read (which applies the
        # shared effective-date expression).
        windowed = repo.fetch_funnel_contacts(start, end)
        if not windowed.get("available"):
            for scope in funnel.ORDERED_SCOPES:
                results.append(_recon_unavailable(win, scope,
                                                  "contact_read_unavailable"))
            continue

        resolver, identity_available = funnel._build_campaign_resolver(start, end)  # noqa: SLF001
        populations = funnel.build_populations(
            windowed.get("rows") or [], start, end,
            campaign_resolver=resolver, identity_available=identity_available)
        sql_population = (populations.get("events") or {}).get(EVENT_SQL) or []

        for scope in funnel.ORDERED_SCOPES:
            block = _reconcile_one(
                f, repo, funnel, win, scope, start, end, col,
                sql_population, resolver, identity_available, all_rows)
            results.append(block)
            if block["available"]:
                compared += 1

    expected = len(windows) * len(funnel.ORDERED_SCOPES)
    execution_failures = [b for b in results if b.get("execution_failure")]
    data_unavailable = [b for b in results
                        if not b["available"] and not b.get("execution_failure")]

    # R9: an execution failure is unavailability of the AUDIT, and it is
    # recorded as such even when other combinations succeeded. Otherwise 43
    # green pairs hide the one whose repository read never ran.
    if execution_failures:
        by_reason: dict = {}
        for block in execution_failures:
            by_reason[block["reason"]] = by_reason.get(block["reason"], 0) + 1
        f.unavailable_now(
            "read_reconciliation",
            f"{len(execution_failures)} of {expected} window/scope "
            f"combination(s) could not be executed: "
            + ", ".join(f"{n}x {r}" for r, n in sorted(by_reason.items()))
            + " — a required repository read did not run, so the "
              "reconciliation is incomplete regardless of how many other "
              "combinations agreed")
    elif compared:
        f.passed("read_reconciliation",
                 f"{compared} of {expected} window/scope combination(s) "
                 "reconciled across headline, detail and operational reads"
                 + (f"; {len(data_unavailable)} unavailable by contract"
                    if data_unavailable else ""))
    else:
        f.unavailable_now("read_reconciliation",
                          "no window/scope combination could be reconciled")

    # Complete only when EVERY comparable combination ran and agreed. A pair
    # that failed closed by contract (no campaign identity) is not comparable
    # and does not block completeness; a pair that never executed does.
    mismatches = [b for b in results if b.get("coverage_status") == "mismatch"]
    reconciliation_complete = (not execution_failures and not mismatches
                               and compared > 0)

    return {"available": bool(compared),
            "combinations": len(results),
            "combinations_expected": expected,
            "combinations_compared": compared,
            "combinations_data_unavailable": len(data_unavailable),
            "combinations_execution_unavailable": len(execution_failures),
            # Two different claims, kept apart. `reconciliation_complete` says
            # every combination reached a PROVEN outcome and every comparable
            # one agreed. `all_combinations_compared` says every combination
            # was actually compared — which a contract-unavailable pair, by
            # definition, was not. Collapsing them would let "complete" be read
            # as "all forty-four reconciled" when twelve were never comparable.
            "all_combinations_compared": compared == expected,
            "reconciliation_complete": reconciliation_complete,
            "reconciled": compared,
            "unavailable": len(results) - compared,
            "scopes": list(funnel.ORDERED_SCOPES),
            "effective_date_basis": repo.EFFECTIVE_DATE_DOCTRINE,
            "results": results}


# ── PR-ADS-159-R9 — a data state and an execution failure are not the same ───
# `campaign_identity_unavailable` is a DATA/CONTRACT state: the canonical
# service was asked, it failed closed exactly as designed, and the audit proved
# it. That is a successful check with an unavailable answer.
#
# Every other reason here is an EXECUTION failure — a repository read that did
# not run. Those must reach `Findings.unavailable_now`, or a partial database
# outage produces `audit_complete: true` and exit 0 while required reads never
# happened. The first cut recorded them in the result block only, so 43 passing
# combinations could hide one that never executed.
REASON_IDENTITY_UNAVAILABLE = "campaign_identity_unavailable"
_EXECUTION_FAILURES = (
    "contact_read_unavailable",
    "scope_membership_unavailable",
    "scoped_reader_unavailable",
)


def _recon_unavailable(win, scope, reason) -> dict:
    """One window/scope pair the audit could not check. Unavailable, not zero."""
    return {
        "available": False, "reason": reason,
        "execution_failure": reason in _EXECUTION_FAILURES,
        "window": win.get("window_key"), "window_type": win.get("window_type"),
        "scope": scope, "headline": None, "detail_total": None,
        "operational_total": None,
    }


def _reconcile_one(f, repo, funnel, win, scope, start, end, col,
                   sql_population, resolver, identity_available,
                   all_rows) -> dict:
    """Compare the three canonical reads for ONE window and ONE scope."""
    window_key, window_type = win.get("window_key"), win.get("window_type")
    label = f"read_reconciliation[{window_type}/{window_key}/{scope}]"

    # An identity-dependent scope with no identity contract is UNKNOWABLE. The
    # canonical service returns None membership and an unavailable page for it;
    # the audit must agree rather than compare a null against a zero.
    if funnel._identity_dependent_scope_unavailable(scope, identity_available):  # noqa: SLF001
        return _recon_unavailable(win, scope, REASON_IDENTITY_UNAVAILABLE)

    memberships = [c["scopes"].get(scope) for c in sql_population]
    if any(m is None for m in memberships):
        return _recon_unavailable(win, scope, "scope_membership_unavailable")
    headline = sum(1 for m in memberships if m)

    from analysis.crm_lifecycle import EVENT_SQL

    # The canonical service's OWN allow-list resolver — never a second copy of
    # the attribution rules living in the audit.
    filters = funnel.resolve_population_filters(scope, None, resolver, repo)
    scoped = {"source_pairs_in": filters["source_pairs_in"],
              "campaigns_in": filters["campaigns_in"],
              "require_keyword": filters["require_keyword"]}
    page = repo.fetch_funnel_contact_page(EVENT_SQL, start, end, page_size=1,
                                          **scoped)
    ops = repo.fetch_operational_status_counts(EVENT_SQL, start, end, **scoped)

    if not page.get("available") or not ops.get("available"):
        return _recon_unavailable(win, scope, "scoped_reader_unavailable")

    detail = int(page.get("total") or 0)
    operational = sum(int(v) for v in (ops.get("counts") or {}).values())

    block = {
        "available": True, "reason": None,
        "window": window_key, "window_type": window_type, "scope": scope,
        "headline": headline, "detail_total": detail,
        "operational_total": operational,
        "effective_date_basis": repo.EFFECTIVE_DATE_DOCTRINE,
        "coverage_status": "compared",
    }
    if not (headline == detail == operational):
        f.violation(label,
                    f"headline={headline}, detail={detail}, "
                    f"operational={operational} — three reads of one population "
                    "disagree for this window and scope")
        block["coverage_status"] = "mismatch"
    return block


def audit_evidence_states(f: Findings) -> dict:
    """Each vocabulary must be internally exclusive, and belong to ONE denominator.

    PR-ADS-159-R4: the previous check took one flat list and asserted a subset,
    which passed while two of its members were unreachable and two states the
    code actually emitted were missing from it. This checks the property that
    matters instead: no value may appear in two vocabularies that count
    different things, because that is the only way a reader can add two numbers
    with different denominators without noticing.
    """
    from services import lifecycle_history_recovery_service as recovery

    vocabularies = {name: list(states)
                    for name, states in recovery.VOCABULARIES.items()}
    out = {"vocabularies": vocabularies,
           "diagnosis_verdicts": None}

    for name, states in vocabularies.items():
        duplicated = sorted({s for s in states if states.count(s) > 1})
        if duplicated:
            f.violation("evidence_states",
                        f"the {name} vocabulary repeats {duplicated} — a "
                        "vocabulary that counts one thing twice is not exclusive")

    # The request vocabulary describes reads; the gap vocabularies describe
    # evidence. A value in both would make "3 failures" and "3 gaps" addable.
    request = set(vocabularies["request"])
    gaps = set(vocabularies["per_sql_gap"]) | set(vocabularies["per_stage_gap"])
    overlap = sorted(request & gaps)
    if overlap:
        f.violation("evidence_states",
                    f"{overlap} appears in both the request and the gap "
                    "vocabularies, which count different things")

    # Read through the recovery service, which re-exports it. This audit must
    # not import the HubSpot connector: it reads only the local database, and a
    # module that can reach an API has no business on its import path.
    out["diagnosis_verdicts"] = list(recovery.DIAGNOSIS_VERDICTS)
    dropped = recovery.HISTORY_PARAMETER_UNSUPPORTED
    # A statement about the REQUEST. It cannot be a per-contact state, and
    # declaring it as one is what made it unreachable.
    if dropped not in recovery.DIAGNOSIS_VERDICTS:
        f.violation("evidence_states",
                    "the parameter-dropped verdict is not in the diagnosis "
                    "vocabulary, so nothing can ever report it")
    if dropped in gaps:
        f.violation("evidence_states",
                    "the parameter-dropped verdict is declared as a per-gap "
                    "state; one contact's payload cannot diagnose a request")

    if not any(c["check"] == "evidence_states" and not c["ok"] for c in f.checks):
        f.passed("evidence_states",
                 f"{len(vocabularies)} vocabularies, "
                 f"{sum(len(v) for v in vocabularies.values())} states, each "
                 "bound to one denominator")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(now: datetime | None = None) -> tuple[Findings, dict]:
    now = now or datetime.now(tz=timezone.utc)
    f = Findings()
    report: dict = {
        "generated_at": now.isoformat(),
        # Stated as a fact about this command. It performs no writes and
        # contacts no external API; every number came from the local database.
        "external_writes_performed": False,
        "hubspot_calls_performed": False,
    }
    report["effective_date"] = check_effective_date_consistency(f)
    report["evidence_states"] = audit_evidence_states(f)
    report["population"] = audit_population(f)
    report["boundary"] = audit_boundary(f)
    report["source_freshness"] = audit_source_freshness(f)
    report["windows"] = audit_windows(
        f, report["population"], now,
        boundary=report["boundary"].get("boundary"),
        # The incident ROWS, so each window resolves membership itself.
        open_incidents=report["boundary"].get("open_incidents"),
        freshness=report["source_freshness"])
    report["read_reconciliation"] = audit_read_reconciliation(f, now)
    report["certification"] = audit_certification(
        f, report["windows"], report["boundary"], report["read_reconciliation"],
        report["source_freshness"])

    # ── membership, and then publication — two questions, answered apart ─────
    #
    # PR-ADS-160 (third review) §3. These two flags used to be `coverage_complete`
    # itself, which is a MEMBERSHIP verdict: every window's undated population
    # is ruled out. That says nothing about whether the source is still being
    # fed, whether the boundary and its incidents could be read, or whether the
    # 44 canonical reads agree — so the summary could announce a publishable
    # CPQL over a dead pipeline while `certification` reported zero certified
    # windows directly beneath it. Whoever read the summary rather than the
    # per-window detail got the wrong answer.
    #
    # `audit_certification` is the LAST gate and already withholds the total and
    # the CPQL from every window it blocks. The summary is now DERIVED from what
    # survived that gate, so it cannot contradict it.
    windows = report["windows"]
    assessable = [w for w in windows if w.get("window_total_complete") is not None]
    coverage_complete = bool(assessable) and all(
        w.get("window_total_complete") for w in windows)
    # Preserved unchanged, and now explicitly the membership-only question.
    report["coverage_complete"] = coverage_complete

    certification = report["certification"]
    assessed = certification.get("windows_assessed") or 0
    certified = certification.get("windows_certified") or 0
    # Every window this audit assessed must have survived certification. The
    # window set is the same one `coverage_complete` spans, so the two answer
    # the same question about the same windows and can be read side by side.
    every_window_certified = bool(windows) and assessed > 0 and certified == assessed

    report["cpql_publishable"] = bool(
        coverage_complete and every_window_certified
        and all(w.get("cpql_publishable") is True for w in windows))
    report["complete_sql_total_publishable"] = bool(
        coverage_complete and every_window_certified
        and all(w.get("complete_sql_total") is not None for w in windows))

    # Stated rather than left to be inferred from two booleans: a reader can see
    # WHICH question failed without diffing the per-window blocks.
    report["publication_withheld_by_certification"] = bool(
        coverage_complete
        and not (report["cpql_publishable"]
                 and report["complete_sql_total_publishable"]))

    # The contract, checked rather than trusted. A summary claiming publishable
    # while nothing is certified is the exact defect this section closes, so it
    # is a violation of this audit and not merely an odd-looking report.
    if certified == 0 and (report["cpql_publishable"]
                           or report["complete_sql_total_publishable"]):
        f.violation("publication_gate",
                    "the summary reports a publishable total or CPQL while no "
                    "window is certified")
    else:
        f.passed("publication_gate",
                 f"publication follows certification: {certified}/{assessed} "
                 f"window(s) certified, cpql_publishable="
                 f"{report['cpql_publishable']}, complete_sql_total_publishable="
                 f"{report['complete_sql_total_publishable']}")

    report["incomplete_windows"] = [
        w.get("window") for w in report["windows"]
        if not w.get("window_total_complete")]
    return f, report


def _render(report: dict, findings: Findings, exit_code: int) -> None:
    print("=" * 78)
    print("  PR-ADS-159 — LIFECYCLE SQL COVERAGE AUDIT (READ-ONLY)")
    print("=" * 78)
    print(f"  audit complete:            {report['audit_complete']}")
    print(f"  coverage complete:         {report['coverage_complete']}"
          "   (membership only)")
    cert = report.get("certification") or {}
    print(f"  windows certified:         {cert.get('windows_certified')}"
          f"/{cert.get('windows_assessed')}")
    print(f"  complete SQL publishable:  {report['complete_sql_total_publishable']}")
    print(f"  CPQL publishable:          {report['cpql_publishable']}")
    if report.get("publication_withheld_by_certification"):
        print("    ↳ membership is complete; publication is withheld by "
              "certification (freshness, readability or reader reconciliation)")
    print(f"  external writes performed: {report['external_writes_performed']}")

    pop = report.get("population") or {}
    if pop.get("available"):
        print("\n  GLOBAL LIFECYCLE-SQL POPULATION (counted ONCE)")
        print(f"    {pop.get('candidates')}  contacts whose stage proves they reached SQL")
        print(f"    {pop.get('direct')}  with the direct HubSpot property")
        print(f"    {pop.get('recovered')}  with a recovered lifecycle-history timestamp")
        print(f"    {pop.get('unresolved')}  global_missing_sql_entry_date")
        print(f"    {pop.get('unresolved_without_created_at')}  of those with no creation time either")

    bound = report.get("boundary") or {}
    print("\n  COVERAGE BOUNDARY  (historical vs prospective)")
    if not bound.get("available"):
        print("    unavailable — the boundary store could not be read")
    elif not bound.get("boundary_established"):
        print("    none established yet — no window is certifiable")
    else:
        print(f"    boundary id:            {bound.get('boundary_id')}")
        print(f"    observed at (UTC):      {bound.get('boundary_observed_at')}")
        print(f"    legacy undated bounded: {bound.get('legacy_undated_bounded')}"
              "   ← an UPPER BOUND, never a date")
    incidents = bound.get("open_post_boundary_incidents")
    print(f"    open post-boundary gaps: "
          f"{'unavailable' if incidents is None else incidents}")
    for reason, count in (bound.get("post_boundary_incident_reasons") or {}).items():
        print(f"      {count}x {reason}")

    print("\n  PER-WINDOW MEMBERSHIP  (the global gap, resolved against each window)")
    for win in report.get("windows") or []:
        # `all_time` exists in BOTH the evidence and business window families,
        # so the type is part of the label — two rows sharing a key are two
        # different contracts, not a duplicate.
        label = f"{win.get('window_type') or '?'}/{win.get('window') or '?'}"
        if win.get("window_total_complete") is None:
            print(f"    {label:<28} unavailable")
            continue
        mark = "complete" if win["window_total_complete"] else "INCOMPLETE"
        cert = "CERTIFIED" if win.get("certified") else "not certified"
        print(f"    {label:<28} {mark:<11} {cert:<14} "
              f"confirmed={win.get('confirmed_sqls')} "
              f"(recovered={win.get('window_membership_recovered')}) "
              f"unresolved={win.get('window_membership_unresolved')} "
              f"ruled_out={win.get('window_membership_proven_outside')} "
              f"(by_boundary={win.get('window_membership_excluded_by_boundary')})")
        print(f"      why: {win.get('explanation')}")
        if not win.get("certified"):
            print(f"      certification: {win.get('certification_status')}")

    cert = report.get("certification") or {}
    fresh = report.get("source_freshness") or {}
    print(f"\n  CERTIFICATION  {cert.get('windows_certified')}/"
          f"{cert.get('windows_assessed')} window(s) certified")
    print(f"    canonical readers reconciled: {cert.get('readers_reconciled')}")
    print(f"    contact-funnel source fresh:  {fresh.get('fresh')} "
          f"({fresh.get('reason')})")
    if fresh.get("detail"):
        print(f"      {fresh.get('detail')}")

    recon = report.get("read_reconciliation") or {}
    if recon.get("combinations_expected"):
        print(f"\n  READ RECONCILIATION  "
              f"{recon.get('combinations_compared')}/"
              f"{recon.get('combinations_expected')} compared · "
              f"{recon.get('combinations_data_unavailable')} unavailable by "
              f"contract · {recon.get('combinations_execution_unavailable')} "
              f"could not execute")
        print(f"    all combinations compared: "
              f"{recon.get('all_combinations_compared')}")
        print(f"    reconciliation_complete:   "
              f"{recon.get('reconciliation_complete')}")

    for check in findings.checks:
        if not check["ok"]:
            print(f"\n  ✗ {check['check']}: {check['detail']}")

    print()
    if findings.violations:
        print(f"  {len(findings.violations)} contract violation(s) — the code "
              "contradicts its own doctrine.")
    if findings.unavailable:
        print(f"  {len(findings.unavailable)} check(s) could not run.")
    if not report["coverage_complete"] and not findings.violations:
        print("  SQL coverage is INCOMPLETE. That is a finding about the DATA,")
        print("  not a failure of this audit. The complete SQL total and CPQL")
        print("  stay unavailable until every window's membership is resolved.")
    print(f"\n  exit {exit_code}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only lifecycle SQL evidence coverage audit")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    parser.add_argument("--strict", action="store_true",
                        help=f"exit {EXIT_COVERAGE_INCOMPLETE} when SQL coverage "
                             "is incomplete, even though the audit itself passed")
    args = parser.parse_args()

    try:
        from db.connection import init_pool

        init_pool()
    except Exception as exc:  # noqa: BLE001
        payload = {"audit_complete": False, "coverage_complete": False,
                   "external_writes_performed": False,
                   "unavailable": [f"database pool could not be initialised: {exc}"]}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database pool could not be initialised: {exc}")
        return EXIT_UNAVAILABLE

    findings, report = run()
    exit_code = findings.exit_code
    # An audit that could not run some of its checks is NOT complete, even
    # when the checks it DID run found a real violation. Reporting otherwise
    # would let an outage masquerade as a finished audit.
    report["audit_complete"] = not findings.unavailable
    report["violations"] = findings.violations
    report["unavailable"] = findings.unavailable
    report["checks"] = findings.checks

    if exit_code == EXIT_OK and args.strict and not report["coverage_complete"]:
        exit_code = EXIT_COVERAGE_INCOMPLETE
    report["exit_code"] = exit_code

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report, findings, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
