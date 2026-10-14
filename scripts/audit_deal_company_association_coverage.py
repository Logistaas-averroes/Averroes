#!/usr/bin/env python3
"""
scripts/audit_deal_company_association_coverage.py

PR-ADS-161D2 — read-only certification of the deal→company association
evidence that customer identity rests on.

    python -m scripts.audit_deal_company_association_coverage
    python -m scripts.audit_deal_company_association_coverage --json

Reads ONE snapshot — the canonical closed-won universe, which carries every won
deal's association paths and observation state (REPEATABLE READ, READ ONLY) —
and proves, for every canonical won deal:

  * it has an explicit observation outcome (or is reported as never observed);
  * a successful zero-company lookup and a failed / never-made lookup are told
    apart, and the stored paths agree with the recorded success counts;
  * no path is stored twice, no path is malformed, no path exists for a deal
    whose lookup never succeeded;
  * stale evidence (latest attempt failed, or the deal changed after it was
    observed) is identified;
  * coverage is proven by a recorded complete scan, not by rows existing;
  * every window touched by a no-company, multi-company, unobserved or stale
    deal publishes NO exact customer total.

A genuine no-company or multi-company deal is a CRM data-quality finding, not a
contract failure; it is counted and listed. What fails the audit is evidence
the storage contract forbids, coverage that is not proven, or a customer total
published over a deal that cannot support it.

No HubSpot call. No Google Ads call. No database write.

Exit codes
    0  the storage and coverage contracts hold (findings may still be listed)
    1  a contract is violated, or coverage is not proven
    2  unavailable — the database, the universe or the company evidence
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

AUDIT_FILE = "scripts/audit_deal_company_association_coverage.py"
FORBIDDEN_IMPORTS = ("db.writers", "writers", "connectors.hubspot_pull",
                     "hubspot_pull", "connectors.google_ads_direct",
                     "google_ads_direct", "connectors.google_ads_source")

# Independent vocabulary — restated here, not imported from the truth module,
# so the audit cannot agree with a broken implementation by construction.
RESOLVED_SINGLE = "resolved_single_customer"
RESOLVED_MULTI_PATH = "resolved_same_customer_multiple_paths"
NO_COMPANY = "unresolved_no_company"
AMBIGUOUS = "ambiguous_multiple_companies"
UNAVAILABLE = "association_unavailable"
RESOLVED = (RESOLVED_SINGLE, RESOLVED_MULTI_PATH)


class Audit:
    def __init__(self) -> None:
        self.violations: list = []
        self.checks: dict = {}

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = self.checks.get(name, True) and bool(ok)
        if not ok:
            self.violations.append(f"{name}: {detail}")


def _dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        p = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return p if p.tzinfo else p.replace(tzinfo=timezone.utc)


def independent_identity(universe: dict) -> dict:
    """Per won deal: ``{state, companies, paths, stale, observed}``, derived
    from the raw stored rows alone.

    Shared with ``audit_customer_closed_won_truth`` as ITS independent
    derivation of customer identity.
    """
    evidence = universe.get("company_associations") or {}
    won = {str(r["deal_id"]) for r in universe.get("won_rows") or []}
    obs = {str(o["deal_id"]): o for o in evidence.get("observations") or []}
    paths: dict = {}
    for p in evidence.get("paths") or []:
        paths.setdefault(str(p["deal_id"]), []).append(p)
    out = {}
    for deal_id in won:
        o = obs.get(deal_id)
        succeeded = bool(o and o.get("last_success_started_at"))
        rows = paths.get(deal_id, []) if succeeded else []
        companies = {str(r["company_id"]) for r in rows}
        if not succeeded:
            state = UNAVAILABLE
        elif not companies:
            state = NO_COMPANY
        elif len(companies) > 1:
            state = AMBIGUOUS
        else:
            state = RESOLVED_MULTI_PATH if len(rows) > 1 else RESOLVED_SINGLE
        stale = False
        if succeeded:
            stale = o.get("last_attempt_status") != "success"
            seen = _dt(o.get("last_success_started_at"))
            modified = _dt(o.get("deal_lastmodified_at"))
            if seen and modified and seen < modified:
                stale = True
        out[deal_id] = {"state": state, "companies": sorted(companies),
                        "paths": len(rows), "stale": stale,
                        "observed": o is not None}
    return out


def storage_checks(a: Audit, universe: dict) -> dict:
    """The stored evidence is internally consistent, deal by deal."""
    evidence = universe.get("company_associations") or {}
    won = {str(r["deal_id"]) for r in universe.get("won_rows") or []}
    obs = {str(o["deal_id"]): o for o in evidence.get("observations") or []}
    by_deal: dict = {}
    keys = []
    for p in evidence.get("paths") or []:
        deal_id = str(p["deal_id"])
        by_deal.setdefault(deal_id, []).append(p)
        keys.append((deal_id, str(p["company_id"]),
                     str(p["association_path_key"])))
        expected_key = (f"{str(p.get('association_category')).upper()}:"
                        f"{p.get('association_type_id')}")
        a.check("path_key_is_hubspot_type_identity",
                p.get("association_path_key") == expected_key,
                f"deal {deal_id}: path key {p.get('association_path_key')!r}")
    a.check("no_path_stored_twice", len(keys) == len(set(keys)),
            f"{len(keys) - len(set(keys))} duplicate (deal, company, path) row(s)")

    zero = failed_without_success = 0
    for deal_id in won:
        o = obs.get(deal_id)
        rows = by_deal.get(deal_id, [])
        if o is None or not o.get("last_success_started_at"):
            # Never succeeded: nothing may stand as evidence for it.
            a.check("no_paths_without_a_successful_lookup", not rows,
                    f"deal {deal_id}: {len(rows)} path(s) with no successful "
                    f"lookup")
            if o is not None:
                failed_without_success += 1
                a.check("zero_and_failed_are_distinct",
                        o.get("success_zero_companies") is None,
                        f"deal {deal_id}: a zero-company fact with no success")
            continue
        companies = {str(r["company_id"]) for r in rows}
        a.check("paths_reconcile_with_observation",
                o.get("success_company_count") == len(companies)
                and o.get("success_path_count") == len(rows),
                f"deal {deal_id}: stored {len(companies)} company(ies)/"
                f"{len(rows)} path(s), observation says "
                f"{o.get('success_company_count')}/{o.get('success_path_count')}")
        a.check("zero_and_failed_are_distinct",
                o.get("success_zero_companies") is (len(companies) == 0),
                f"deal {deal_id}: success_zero_companies="
                f"{o.get('success_zero_companies')} with {len(companies)} "
                f"company(ies)")
        if not companies:
            zero += 1
    return {"successful_zero_company_lookups": zero,
            "failed_without_any_success": failed_without_success}


def scan_is_current(state: dict, now, max_age_hours: float) -> tuple:
    """``(current, reason)`` — restated here from the stored run record, not
    taken from the service: the latest full-population run succeeded and was
    complete, and the latest complete scan finished within the configured age."""
    state = state or {}
    if state.get("last_full_run_status") != "success" \
            or state.get("last_full_run_complete") is not True:
        return False, "latest full scan not successful and complete"
    finished = _dt(state.get("last_complete_scan_finished_at"))
    if finished is None:
        return False, "no complete scan recorded"
    if (now - finished).total_seconds() / 3600 > max_age_hours:
        return False, f"complete scan older than {max_age_hours:g}h"
    return True, None


def expected_customer_verdict(members, identity, scan_current) -> tuple:
    """``(status, reason)`` the customer metric must carry when the deal count
    is published — restated from the identity rules, not imported."""
    states = [identity[d]["state"] for d in members]
    if any(st == UNAVAILABLE for st in states):
        return "unavailable", "customer_identity_evidence_unavailable"
    if not scan_current:
        return "withheld", "customer_identity_scan_not_current"
    if any(st not in RESOLVED for st in states):
        return "withheld", "customer_identity_unresolved"
    if any(identity[d]["stale"] for d in members):
        return "withheld", "customer_identity_evidence_stale"
    return "published", None


def coverage_checks(a: Audit, universe: dict, identity: dict, *, now,
                    max_age_hours: float) -> dict:
    """Every won deal has an outcome, and coverage is proven by a recorded
    complete scan whose start every won deal's evidence post-dates."""
    evidence = universe.get("company_associations") or {}
    state = evidence.get("sync_state") or {}
    never = sorted(d for d, i in identity.items() if not i["observed"])
    a.check("every_won_deal_has_an_observation_outcome", not never,
            f"{len(never)} won deal(s) never observed")
    scan_start = _dt(state.get("last_complete_scan_started_at"))
    a.check("coverage_proven_by_a_complete_scan", scan_start is not None,
            "no recorded scan has observed every won deal")
    current, why = scan_is_current(state, now, max_age_hours)
    a.check("association_scan_is_current", current, why or "")
    obs = {str(o["deal_id"]): o for o in evidence.get("observations") or []}
    behind = []
    if scan_start is not None:
        for deal_id in identity:
            o = obs.get(deal_id) or {}
            seen = _dt(o.get("last_success_started_at"))
            if seen is None or seen < scan_start:
                behind.append(deal_id)
    # A complete scan must have covered every won deal: a deal whose latest
    # success predates the scan's start was NOT observed by it, so the scan's
    # claim of completeness is false for that deal.
    a.check("complete_scan_covers_every_won_deal", not behind,
            f"{len(behind)} won deal(s) lack a successful observation since "
            f"the recorded complete scan started")
    return {"scan_current": current, "scan_not_current_reason": why,
            "scan_max_age_hours": max_age_hours,
            "never_observed": len(never),
            "never_observed_deal_ids": never[:50],
            "complete_scan_started_at": state.get("last_complete_scan_started_at"),
            "complete_scan_finished_at": state.get(
                "last_complete_scan_finished_at"),
            "complete_scan_population": state.get("last_complete_scan_population"),
            "won_deals_without_evidence_since_last_complete_scan": len(behind),
            "last_run_status": state.get("last_run_status"),
            "last_run_mode": state.get("last_run_mode"),
            "last_run_complete": state.get("last_run_complete"),
            "backfill_status": state.get("backfill_status")}


def publication_checks(a: Audit, universe: dict, identity: dict, *, now,
                       scan_current: bool) -> list:
    """No window publishes an exact customer total over a deal that cannot
    support it; a fully supported window's total is the distinct companies."""
    from scripts import audit_customer_closed_won_truth as cw  # noqa: PLC0415
    from services import canonical_customer_revenue_service as svc  # noqa: PLC0415

    truth = svc.get_closed_won_truth(now=now, universe=universe, resolver=None,
                                     resolver_detail="not_used_by_this_audit")
    summary = []
    for w in truth["windows"]:
        name = f"{w['window']['window_type']}:{w['window']['window_key']}"
        if not w.get("available"):
            continue
        start, end, is_all_time = cw._independent_bounds(
            w["window"]["window_type"], w["window"]["window_key"], now)
        members = cw._expected(universe, start, end, is_all_time, now)["members"]
        touched = [d for d in members
                   if identity[d]["state"] not in RESOLVED or identity[d]["stale"]]
        pub = w["publication"]["customers"]
        customers = w["outcomes"]["customers"]
        if w["publication"]["closed_won_deals"]["status"] == "published":
            # The published surface must tell a failed or never-made lookup
            # from a successful "no company" — status, reason and distribution.
            verdict = expected_customer_verdict(members, identity, scan_current)
            a.check("customer_verdict_matches_the_evidence",
                    (pub["status"], pub["reason"]) == verdict,
                    f"{name}: customers {pub['status']}/{pub['reason']}, the "
                    f"evidence says {verdict[0]}/{verdict[1]}")
            dist: dict = {}
            for d in members:
                dist[identity[d]["state"]] = dist.get(identity[d]["state"], 0) + 1
            a.check("identity_distribution_matches_the_evidence",
                    w["coverage"].get("customer_identity") == dist,
                    f"{name}: published {w['coverage'].get('customer_identity')}"
                    f", the evidence says {dist}")
        if touched or not scan_current:
            a.check("customers_withheld_where_identity_is_unsupported",
                    pub["status"] != "published" and customers is None,
                    f"{name}: customers={customers} ({pub['status']}) over "
                    f"{len(touched)} unsupported deal(s), scan current: "
                    f"{scan_current}")
        elif pub["status"] == "published":
            expected = {c for d in members for c in identity[d]["companies"]}
            a.check("published_customers_are_distinct_companies",
                    customers == len(expected),
                    f"{name}: published {customers}, distinct companies "
                    f"{len(expected)}")
        summary.append({"window": name, "members": len(members),
                        "deals_without_supported_identity": len(touched),
                        "customers_status": pub["status"],
                        "customers_reason": pub["reason"],
                        "customers": customers,
                        "lower_bound": w["outcomes"].get(
                            "confirmed_customer_lower_bound")})
    return summary


def structural_checks(a: Audit) -> None:
    tree = ast.parse((_ROOT / AUDIT_FILE).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(x.name for x in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{x.name}" for x in node.names)
    bad = sorted(n for n in names if n in FORBIDDEN_IMPORTS)
    a.check("no_external_or_write_path", not bad, f"imports {bad}")


def run(now: datetime | None = None) -> tuple[Audit, dict]:
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415

    now = now or datetime.now(tz=timezone.utc)
    a = Audit()
    structural_checks(a)
    report = {"audit": "deal_company_association_coverage",
              "generated_at": now.isoformat(),
              "hubspot_calls_performed": False,
              "google_ads_calls_performed": False,
              "database_writes_performed": False}
    universe = crs.load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID)
    evidence = universe.get("company_associations") or {}
    if not universe.get("available") or not evidence.get("available"):
        report.update(available=False, verdict="unavailable",
                      exit_code=EXIT_UNAVAILABLE, checks=a.checks,
                      violations=a.violations,
                      detail=("canonical universe unreadable"
                              if not universe.get("available")
                              else f"company evidence unreadable: "
                                   f"{evidence.get('error')}"))
        return a, report

    from services.canonical_customer_revenue_service import (  # noqa: PLC0415
        association_scan_max_age_hours,
    )
    identity = independent_identity(universe)
    report["available"] = True
    report["won_deals"] = len(identity)
    report["storage"] = storage_checks(a, universe)
    report["coverage"] = coverage_checks(
        a, universe, identity, now=now,
        max_age_hours=association_scan_max_age_hours())
    states: dict = {}
    for i in identity.values():
        states[i["state"]] = states.get(i["state"], 0) + 1
    obs = evidence.get("observations") or []
    reasons: dict = {}
    for o in obs:
        if o.get("last_attempt_status") == "failed":
            r = o.get("last_failure_reason") or "unknown"
            reasons[r] = reasons.get(r, 0) + 1
    report["identity_states"] = dict(sorted(states.items()))
    report["stale_evidence"] = {
        "deals": sum(1 for i in identity.values() if i["stale"]),
        "latest_attempt_failed": sum(
            1 for o in obs if o.get("last_attempt_status") == "failed"
            and o.get("last_success_started_at")),
    }
    report["latest_failure_reasons"] = dict(sorted(reasons.items()))
    report["crm_findings"] = {
        "no_company_deal_ids": sorted(
            d for d, i in identity.items() if i["state"] == NO_COMPANY)[:100],
        "multiple_company_deal_ids": sorted(
            d for d, i in identity.items() if i["state"] == AMBIGUOUS)[:100],
    }
    report["windows"] = publication_checks(
        a, universe, identity, now=now,
        scan_current=report["coverage"]["scan_current"])
    report["checks"] = a.checks
    report["violations"] = a.violations
    report["exit_code"] = EXIT_VIOLATION if a.violations else EXIT_OK
    report["verdict"] = ("contract_broken" if a.violations
                         else "contracts_hold")
    return a, report


def _render(report: dict) -> None:
    print("=" * 84)
    print("  PR-ADS-161D2 — DEAL→COMPANY ASSOCIATION COVERAGE AUDIT (read-only)")
    print(f"  verdict: {report.get('verdict')}   HubSpot calls: False   "
          f"Google Ads calls: False   database writes: False")
    print("=" * 84)
    if not report.get("available"):
        print(f"  UNAVAILABLE — {report.get('detail')}")
        return
    print(f"  won deals                    {report['won_deals']}")
    print(f"  identity states              {report['identity_states']}")
    print(f"  stale evidence               {report['stale_evidence']}")
    print(f"  latest failure reasons       {report['latest_failure_reasons']}")
    for k, v in report["storage"].items():
        print(f"  {k:<29}{v}")
    for k, v in report["coverage"].items():
        if k != "never_observed_deal_ids":
            print(f"  {k:<29}{v}")
    print(f"  CRM findings (no company)    "
          f"{len(report['crm_findings']['no_company_deal_ids'])} deal(s)")
    print(f"  CRM findings (several)       "
          f"{len(report['crm_findings']['multiple_company_deal_ids'])} deal(s)")
    print()
    print(f"  {'window':<24}{'members':>8}{'unsupported':>13}{'customers':>11}"
          f"{'lower bd':>10}  status / reason")
    for w in report["windows"]:
        print(f"  {w['window']:<24}{w['members']:>8}"
              f"{w['deals_without_supported_identity']:>13}"
              f"{'—' if w['customers'] is None else w['customers']:>11}"
              f"{'—' if w['lower_bound'] is None else w['lower_bound']:>10}  "
              f"{w['customers_status']} / {w['customers_reason']}")
    if report.get("violations"):
        print("\n  CONTRACT VIOLATIONS")
        for v in report["violations"]:
            print(f"    - {v}")
    else:
        print("\n  Storage and coverage contracts hold. CRM findings above are "
              "data quality,\n  not software failures — and every window they "
              "touch withholds its customer total.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only certification of deal→company association "
                    "coverage")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from db.connection import ensure_database_ready

    ready, detail = ensure_database_ready()
    if not ready:
        payload = {"available": False, "verdict": "unavailable",
                   "detail": f"database not ready: {detail}"}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database not ready: {detail}")
        return EXIT_UNAVAILABLE
    try:
        _, report = run()
    except Exception as exc:  # noqa: BLE001
        payload = {"available": False, "verdict": "unavailable",
                   "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — {payload['detail']}")
        return EXIT_UNAVAILABLE
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
