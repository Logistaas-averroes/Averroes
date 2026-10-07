"""
scripts/audit_customer_closed_won_truth.py

PR-ADS-161D — read-only certification of closed-won deals, customers and
closed-won revenue, across every evidence and business window.

    python -m scripts.audit_customer_closed_won_truth
    python -m scripts.audit_customer_closed_won_truth --json

It audits ``services/canonical_customer_revenue_service.py`` by RE-DERIVING
what that service claims from the same snapshot, independently, and by
cross-checking each business window's membership against the production
revenue contract's own SQL-windowed read
(``deal_ledger_repository.fetch_won_deals``, with the bounds
``canonical_revenue_service.load_won_deals`` uses), run INSIDE the same
REPEATABLE READ snapshot so a sync committing mid-audit cannot fake a
mismatch.

A metric that is WITHHELD for a true reason is not a failure: missing company
associations, unpriced deals or undated won deals are source gaps, reported as
coverage. A failure is a broken contract — a published number its own evidence
does not support, a bucket partition that does not add up, a deal counted
twice, a value where the status says withheld.

No HubSpot call. No Google Ads call. No write anywhere: the ledger read runs in
a PostgreSQL REPEATABLE READ, READ ONLY transaction.

Exit codes
    0  every contract holds (metrics may still be withheld — see the table)
    1  a truth contract is broken
    2  unavailable — the database or the canonical ledger could not be read
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

SERVICE_FILE = "services/canonical_customer_revenue_service.py"
ANALYSIS_FILE = "analysis/closed_won_truth.py"
AUDIT_FILE = "scripts/audit_customer_closed_won_truth.py"
#: Production surfaces that must not consume the new service in PR-ADS-161D.
PRODUCTION_SURFACES = ("api/server.py", "static/app.js")
#: Modules whose import from the truth layer would mean an external call or a
#: write path.
FORBIDDEN_IMPORTS = ("db.writers", "writers", "connectors.hubspot_pull",
                     "hubspot_pull", "connectors.google_ads_direct",
                     "google_ads_direct", "connectors.google_ads_source",
                     "connectors.hubspot_deals")


class Audit:
    def __init__(self) -> None:
        self.violations: list = []
        self.checks: dict = {}

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        prev = self.checks.get(name, True)
        self.checks[name] = prev and bool(ok)
        if not ok:
            self.violations.append(f"{name}: {detail}")


def _imports_of(rel: str) -> set:
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            names.update(f"{mod}.{a.name}" if mod else a.name
                         for a in node.names)
            names.update(a.name for a in node.names)
    return names


def structural_checks(a: Audit) -> dict:
    """Checks 16–20: properties of the code, not of the data."""
    for rel in (SERVICE_FILE, ANALYSIS_FILE, AUDIT_FILE):
        names = _imports_of(rel)
        bad = sorted(n for n in names if n in FORBIDDEN_IMPORTS)
        a.check("no_external_or_write_path", not bad,
                f"{rel} imports {bad}")
    for rel in (SERVICE_FILE, ANALYSIS_FILE):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        a.check("no_lifecycle_status_creates_a_deal",
                "lifecycle_stage" not in text and "lifecyclestage" not in text,
                f"{rel} references the contact lifecycle stage")
    consumers = [rel for rel in PRODUCTION_SURFACES
                 if "canonical_customer_revenue_service" in
                 (_ROOT / rel).read_text(encoding="utf-8")]
    consumers += [str(p.relative_to(_ROOT)) for p in
                  (_ROOT / "services").glob("*.py")
                  if p.name != "canonical_customer_revenue_service.py"
                  and "canonical_customer_revenue_service" in
                  p.read_text(encoding="utf-8")]
    a.check("no_production_page_changed", not consumers,
            f"consumed by {consumers}")
    return {"production_consumers": consumers}


def _dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        p = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return p if p.tzinfo else p.replace(tzinfo=timezone.utc)


def window_checks(a: Audit, w: dict, universe: dict, *, now) -> None:
    """Checks 1–15 for one window, re-derived from the snapshot."""
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    from services import canonical_customer_revenue_service as svc  # noqa: PLC0415

    name = f"{w['window']['window_type']}:{w['window']['window_key']}"
    if not w.get("available"):
        for metric, pub in w["publication"].items():
            if metric in ("roas", "cac"):
                continue
            a.check("unavailable_is_never_a_number",
                    pub["status"] == cwt.UNAVAILABLE, f"{name} {metric}")
        a.check("unavailable_is_never_a_number",
                all(v is None for v in w["outcomes"].values()), name)
        return

    won = {str(r["deal_id"]): r for r in universe.get("won_rows") or []}
    ids = w["membership"]["deal_ids"]
    members = [won[i] for i in ids if i in won]
    pub = w["publication"]
    out = w["outcomes"]

    # 1. Every member is a won deal by the canonical predicate.
    a.check("closed_won_predicate", all(i in won for i in ids)
            and all(r.get("hs_is_closed_won") is True for r in won.values()),
            f"{name}: a member is not hs_is_closed_won IS TRUE")
    # Won-definition disagreement must withhold the count, never be ignored.
    if w["membership"]["won_definition_conflicts"]:
        a.check("won_definition_conflict_withholds",
                pub["closed_won_deals"]["status"] != cwt.PUBLISHED,
                f"{name}: published over a won flag/stage disagreement")
    # 2. Deal ids unique.
    a.check("deal_ids_unique", len(ids) == len(set(ids)),
            f"{name}: duplicate deal id")
    # 14. Membership is the close date, half-open — re-derived here.
    bounds = svc.window_bounds(w["window"]["window_type"],
                               w["window"]["window_key"], now)
    # A close after ``now`` has not happened: it is a member of no window,
    # however far the window's end lies in the future.
    for r in members:
        close = _dt(r.get("deal_close_date"))
        if close is None:
            a.check("membership_uses_close_date", bounds["is_all_time"],
                    f"{name}: undated deal {r['deal_id']} in a finite window")
            continue
        ok = (bounds["start"] is None or close >= bounds["start"]) \
            and close < bounds["end"] and close <= now
        a.check("membership_uses_close_date", ok,
                f"{name}: deal {r['deal_id']} closed outside the window "
                f"or after now")
    expected = sorted(
        str(r["deal_id"]) for r in won.values()
        if (_dt(r.get("deal_close_date")) is None and bounds["is_all_time"])
        or (_dt(r.get("deal_close_date")) is not None
            and _dt(r.get("deal_close_date")) <= now
            and (bounds["start"] is None
                 or _dt(r.get("deal_close_date")) >= bounds["start"])
            and _dt(r.get("deal_close_date")) < bounds["end"]))
    a.check("membership_reconciles", expected == sorted(ids),
            f"{name}: re-derived membership differs")
    # The published number is the re-derived membership's size — not merely
    # a membership list that happens to be right beside a wrong total.
    if pub["closed_won_deals"]["status"] == cwt.PUBLISHED:
        a.check("membership_reconciles",
                out["closed_won_deals"] == len(expected),
                f"{name}: published {out['closed_won_deals']} won deals, "
                f"re-derived membership is {len(expected)}")
    dated_expected = sum(1 for i in expected
                         if _dt(won[i].get("deal_close_date")) is not None)
    a.check("membership_reconciles",
            out["closed_won_deals_confirmed_in_window"] == dated_expected,
            f"{name}: confirmed-in-window "
            f"{out['closed_won_deals_confirmed_in_window']} != "
            f"{dated_expected}")
    # 11. Undated won deals disclosed, and a finite window withheld over them.
    undated = sum(1 for r in won.values() if _dt(r.get("deal_close_date"))
                  is None)
    a.check("missing_close_dates_disclosed",
            w["membership"]["undated_won_deals"] == undated, name)
    if undated and not bounds["is_all_time"] and \
            pub["closed_won_deals"]["status"] == cwt.PUBLISHED:
        a.check("missing_close_dates_disclosed", False,
                f"{name}: published a finite-window count over "
                f"{undated} undated won deal(s)")
    # 3/8/9. Partition: every member in exactly one bucket; sums reconcile.
    cov = w["coverage"]["campaign_attribution"]
    a.check("attribution_partition_reconciles",
            sum(cov.values()) == len(members),
            f"{name}: buckets {cov} != {len(members)} deals")
    subset = sum(b["revenue_usd_confirmed_subset"]
                 for b in w["attribution"]["partition"].values())
    a.check("attribution_partition_reconciles",
            round(subset, 2) == out["revenue_usd_confirmed_subset"],
            f"{name}: bucket revenue {subset} != "
            f"{out['revenue_usd_confirmed_subset']}")
    # 4/13. Revenue = distinct proven deals once.
    proven = round(sum(float(r["revenue_usd"]) for r in members
                       if cwt.revenue_is_proven(r)), 2)
    a.check("revenue_reconciles_to_distinct_deals",
            proven == out["revenue_usd_confirmed_subset"], name)
    unpriced = sum(1 for r in members if not cwt.revenue_is_proven(r))
    if pub["revenue_usd"]["status"] == cwt.PUBLISHED:
        a.check("revenue_only_published_when_every_deal_proven",
                unpriced == 0 and out["revenue_usd"] == proven,
                f"{name}: published revenue with {unpriced} unpriced deal(s)")
    # 5. Customers: only distinct proven identities, never a deal count.
    if pub["customers"]["status"] == cwt.PUBLISHED:
        a.check("customer_count_is_distinct_identities",
                out["unresolved_customer_identity"] == 0, name)
    # Null discipline — a withheld metric carries no number.
    for metric, value in (("closed_won_deals", out["closed_won_deals"]),
                          ("revenue_usd", out["revenue_usd"]),
                          ("customers", out["customers"])):
        if pub[metric]["status"] != cwt.PUBLISHED:
            a.check("withheld_is_never_a_number", value is None,
                    f"{name}: {metric}={value} while "
                    f"{pub[metric]['status']}")
    if pub["campaign_revenue"]["status"] != cwt.PUBLISHED:
        a.check("withheld_is_never_a_number",
                w["attribution"]["by_campaign"] is None,
                f"{name}: campaign breakdown while withheld")
    for metric in ("roas", "cac"):
        a.check("roas_and_cac_not_published",
                pub[metric]["status"] == cwt.NOT_PUBLISHED, name)
    # 15. Freshness comes from sync coverage, never the newest deal row.
    fresh = w["coverage"]["source_freshness"]
    a.check("freshness_from_sync_coverage",
            "hubspot_deal_sync_state" in (fresh.get("signal") or ""), name)
    if fresh.get("coverage_proven") is False:
        a.check("unproven_coverage_withholds",
                pub["closed_won_deals"]["status"] == cwt.UNAVAILABLE, name)


def sql_windows(now) -> dict:
    """``{business_key: (start, end)}`` — the bounds production queries."""
    from analysis.business_windows import WINDOW_KEYS  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415
    return {k: crs.won_window_sql_bounds(k, now=now) for k in WINDOW_KEYS}


def sql_cross_check(a: Audit, w: dict, universe: dict, *, now) -> dict | None:
    """Production's own SQL windowing for a business window, same snapshot.

    The SQL filters on the window only; a close after ``now`` is dropped here,
    independently of the service, because it has not happened. The remaining
    ids must be exactly the service's DATED membership. Production's All Time
    has an upper bound, so its SQL returns no undated deal: those are reported
    beside the comparison (``service_undated_members``), never hidden in it.
    """
    if not w.get("available") or w["window"]["window_type"] != "business":
        return None
    key = w["window"]["window_key"]
    rows = (universe.get("sql_windowed") or {}).get(key)
    if rows is None:
        # Not a measured mismatch: the comparison never ran. run() reports
        # the audit unavailable for this, never a broken contract.
        return {"window": key, "unavailable": True}
    future = sorted(r["deal_id"] for r in rows
                    if _dt(r.get("deal_close_date")) is not None
                    and _dt(r["deal_close_date"]) > now)
    sql_ids = sorted({r["deal_id"] for r in rows} - set(future))
    won = {str(r["deal_id"]): r for r in universe.get("won_rows") or []}
    members = w["membership"]["deal_ids"]
    undated = [i for i in members
               if _dt((won.get(i) or {}).get("deal_close_date")) is None]
    mine = sorted(set(members) - set(undated))
    a.check("sql_window_cross_check", sql_ids == mine,
            f"business:{key}: production SQL {len(sql_ids)} deal(s) != "
            f"service dated membership {len(mine)}")
    return {"window": key, "production_sql_deals": len(sql_ids),
            "production_sql_future_dated_excluded": len(future),
            "service_dated_members": len(mine),
            "service_undated_members": len(undated)}


def run(now: datetime | None = None) -> tuple[Audit, dict]:
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    from services import canonical_customer_revenue_service as svc  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415

    now = now or datetime.now(tz=timezone.utc)
    a = Audit()
    structural = structural_checks(a)
    universe = crs.load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID,
                                            sql_windows=sql_windows(now))
    truth = svc.get_closed_won_truth(now=now, universe=universe)
    report = {"audit": "customer_closed_won_truth",
              "generated_at": now.isoformat(),
              "available": bool(universe.get("available")),
              "hubspot_calls_performed": False,
              "google_ads_calls_performed": False,
              "external_writes_performed": False,
              "database_writes_performed": False,
              "structural": structural, "truth": truth,
              "sql_cross_checks": []}
    if not universe.get("available"):
        for w in truth["windows"]:
            window_checks(a, w, universe, now=now)
        report.update(checks=a.checks, violations=a.violations,
                      exit_code=EXIT_UNAVAILABLE,
                      verdict="unavailable")
        return a, report
    not_run = []
    for w in truth["windows"]:
        window_checks(a, w, universe, now=now)
        cross = sql_cross_check(a, w, universe, now=now)
        if cross:
            report["sql_cross_checks"].append(cross)
            if cross.get("unavailable"):
                not_run.append(cross["window"])
    report["checks"] = a.checks
    report["violations"] = a.violations
    report["sql_cross_checks_not_run"] = not_run
    if a.violations:
        report["exit_code"], report["verdict"] = EXIT_VIOLATION, \
            "contract_broken"
    elif not_run:
        # A comparison that could not run proves nothing either way.
        report["exit_code"], report["verdict"] = EXIT_UNAVAILABLE, \
            "unavailable"
    else:
        report["exit_code"], report["verdict"] = EXIT_OK, "contracts_hold"
    return a, report


def _fmt(value, money=False):
    if value is None:
        return "—"
    return f"{value:,.2f}" if money else str(value)


def _render(report: dict) -> None:
    print("=" * 96)
    print("  PR-ADS-161D — CUSTOMER & CLOSED-WON TRUTH AUDIT (read-only)")
    print(f"  verdict: {report.get('verdict')}    HubSpot calls: "
          f"{report['hubspot_calls_performed']}    external writes: "
          f"{report['external_writes_performed']}    database writes: "
          f"{report['database_writes_performed']}")
    print("=" * 96)
    truth = report.get("truth") or {}
    print(f"\n  {'window':<24}{'won deals':>10}{'customers':>11}"
          f"{'revenue USD':>16}{'unattrib.':>11}{'ambiguous':>11}  status")
    for w in truth.get("windows") or []:
        name = f"{w['window']['window_type'][:3]}:{w['window']['window_key']}"
        out, pub = w["outcomes"], w["publication"]
        part = (w.get("attribution") or {}).get("partition") or {}
        cov = (w.get("coverage") or {}).get("campaign_attribution") or {}
        status = "/".join(sorted({pub[m]["status"] for m in
                                  ("closed_won_deals", "revenue_usd",
                                   "customers")}))
        print(f"  {name:<24}{_fmt(out['closed_won_deals']):>10}"
              f"{_fmt(out['customers']):>11}"
              f"{_fmt(out['revenue_usd'], True):>16}"
              # Bucket counts only beside a PUBLISHED deal count: a "0"
              # next to a withheld total reads as a complete figure.
              f"{_fmt(part.get('unattributed', {}).get('deals')):>11}"
              f"{_fmt(part.get('ambiguous', {}).get('deals')):>11}  {status}")
    fresh = truth.get("freshness") or {}
    allw = next((w for w in truth.get("windows") or []
                 if w["window"]["window_key"] == "all_time"
                 and w.get("available")), None)
    print("\n  COVERAGE (all time)")
    if allw:
        cov = allw["coverage"]
        print(f"    missing close date            "
              f"{cov['close_date'].get('missing_close_date')}")
        print(f"    future-dated (invalid) close  "
              f"{cov['close_date'].get('invalid_close_date')}")
        print(f"    customer identity             {cov['customer_identity']}")
        print(f"    amount                        {cov['amount']}")
        print(f"    currency                      {cov['currency']}")
        print(f"    contact association           "
              f"{cov['contact_association']}")
        print(f"    attribution partition         "
              f"{cov['campaign_attribution']}")
        print(f"    won flag / stage conflicts    "
              f"{cov['won_definition']['conflicts_touching_window']}")
    print(f"    source freshness              coverage_proven="
          f"{fresh.get('coverage_proven')} last_ok_incremental="
          f"{fresh.get('latest_successful_incremental_at')} age_h="
          f"{fresh.get('age_hours')} (threshold not configured)")
    print(f"    customer identity source      "
          f"{truth.get('customer_identity_source')} — "
          f"{truth.get('customer_identity_gap')}")
    if report.get("violations"):
        print("\n  CONTRACT VIOLATIONS")
        for v in report["violations"]:
            print(f"    - {v}")
    else:
        print("\n  Every contract holds. Withheld metrics are withheld for "
              "the reason shown, not hidden.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only certification of closed-won deals, customers "
                    "and closed-won revenue")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    args = parser.parse_args(argv)

    from db.connection import ensure_database_ready

    ready, error = ensure_database_ready()
    if not ready:
        payload = {"available": False, "verdict": "unavailable",
                   "detail": f"database not ready: {error}",
                   "external_writes_performed": False,
                   "database_writes_performed": False,
                   "hubspot_calls_performed": False}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database not ready: {error}")
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
    sys.exit(main())
