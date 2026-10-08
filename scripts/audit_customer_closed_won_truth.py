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
#: Python packages scanned for any consumer of the service.
PRODUCTION_PACKAGES = ("api", "services", "scheduler", "scripts", "connectors",
                       "db", "analysis")
#: Modules whose import from the truth layer would mean an external call or a
#: write path.
#: The confirmed won stage, restated here (asserted equal to the connector's
#: won stage and the analysis module's constant by test_04).
WON_STAGE_ID = "326093516"
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
    """Every module and name ``rel`` imports, at any depth (function-level
    imports included)."""
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
    # Every Python production path: routes, schedulers, services, scripts —
    # all but the service itself and this audit.
    exempt = {SERVICE_FILE, AUDIT_FILE}
    consumers += sorted(
        str(p.relative_to(_ROOT)) for d in PRODUCTION_PACKAGES
        for p in (_ROOT / d).rglob("*.py")
        if str(p.relative_to(_ROOT)) not in exempt
        and str(p.relative_to(_ROOT)) not in PRODUCTION_SURFACES
        and any("canonical_customer_revenue_service" in n
                for n in _imports_of(str(p.relative_to(_ROOT)))))
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


# ── independent re-derivation ─────────────────────────────────────────────────
# Nothing below calls the 161D analysis module's decision functions. Each rule is
# restated from the raw snapshot rows, so a broken implementation disagrees with
# the audit instead of agreeing with itself.

#: The ledger's currency contract (``analysis.deal_currency``, PR-ADS-153E-A):
#: the only statuses whose ``revenue_usd`` is proven USD.
_PROVEN_CURRENCY = ("verified_usd", "converted")
_ACCOUNT_TZ = "Europe/London"
#: Keys under a window whose values are text or metadata, never a count.
_NON_NUMERIC_KEYS = ("window", "definitions", "publication", "available")


def _independent_bounds(window_type: str, window_key: str, now) -> tuple:
    """``(start, end, is_all_time)`` without the service's window code.

    Evidence: N Europe/London calendar days ending today, from local midnight
    to the local midnight after today. Business: the shared business-window
    definition (``analysis.business_windows``), which IS the definition.
    """
    from datetime import time, timedelta  # noqa: PLC0415
    if window_type == "evidence":
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        tz = ZoneInfo(_ACCOUNT_TZ)
        today = now.astimezone(tz).date()
        days = None if window_key == "all_time" else int(window_key.rstrip("d"))
        end = datetime.combine(today + timedelta(days=1), time.min,
                               tzinfo=tz).astimezone(timezone.utc)
        start = None if days is None else datetime.combine(
            today - timedelta(days=days - 1), time.min,
            tzinfo=tz).astimezone(timezone.utc)
        return start, end, days is None
    from analysis.business_windows import get_window_bounds  # noqa: PLC0415
    start, end = get_window_bounds(window_key, now=now)
    return start, end, start is None


def _in(close, start, end) -> bool:
    return close is not None and (start is None or close >= start) \
        and close < end


def _expected(universe: dict, start, end, is_all_time, now) -> dict:
    """The window's truth, re-derived from the raw snapshot."""
    won = {str(r["deal_id"]): r for r in universe.get("won_rows") or []}
    members = []
    for i, r in won.items():
        close = _dt(r.get("deal_close_date"))
        if close is None:
            if is_all_time:
                members.append(i)
        elif close <= now and _in(close, start, end):
            members.append(i)
    conflicts, acq_conflicts = [], []
    for d in universe.get("won_definition_rows") or []:
        flag = d.get("hs_is_closed_won") is True
        stage = str(d.get("deal_stage_id") or "") == WON_STAGE_ID
        if flag == stage:
            continue
        close = _dt(d.get("deal_close_date"))
        if close is None or close <= now:
            acq_conflicts.append(str(d["deal_id"]))
        if close is None or (close <= now and (is_all_time
                                               or _in(close, start, end))):
            conflicts.append(str(d["deal_id"]))
    unknown = None
    if universe.get("unknown_won_rows") is not None:
        unknown = 0
        for d in universe["unknown_won_rows"]:
            close = _dt(d.get("deal_close_date"))
            if (close is None and is_all_time) or (
                    close is not None and close <= now
                    and _in(close, start, end)):
                unknown += 1
    rows = [won[i] for i in members]

    def proven(r):
        try:
            value = float(r.get("revenue_usd"))
        except (TypeError, ValueError):
            return False
        return r.get("currency_status") in _PROVEN_CURRENCY \
            and value == value and value >= 0

    ambiguous = [r for r in rows if r.get("attribution_status") == "ambiguous"
                 or r.get("association_status") == "ambiguous"]
    from analysis.revenue_scope import (  # noqa: PLC0415 - pre-161D lattice
        is_google_ads_attributed,
    )
    google = [r for r in rows if r not in ambiguous
              and r.get("association_status") not in ("none", "lookup_failed")
              and r.get("attribution_status") != "unavailable"
              and is_google_ads_attributed(r)]
    # Acquisition: every associated contact created in the window.
    contacts: dict = {}
    for c in universe.get("acquisition_contacts") or []:
        contacts.setdefault(str(c["deal_id"]), []).append(c)
    acq_members, acq_unknown = [], 0
    for i, r in won.items():
        close = _dt(r.get("deal_close_date"))
        if close is not None and close > now:
            continue
        cs = contacts.get(i) or []
        if r.get("association_status") == "lookup_failed" or (
                not cs and r.get("association_status") != "none") or any(
                c.get("funnel_row_present") is False
                or _dt(c.get("contact_created_at")) is None for c in cs):
            acq_unknown += 1
            continue
        inside = [_in(_dt(c["contact_created_at"]), start, end) for c in cs]
        if cs and all(inside):
            acq_members.append(r)
        elif any(inside):
            acq_unknown += 1
    return {"won": won, "members": sorted(members), "rows": rows,
            "undated": sum(1 for r in won.values()
                           if _dt(r.get("deal_close_date")) is None),
            "conflicts": sorted(set(conflicts)),
            "acq_conflicts": sorted(set(acq_conflicts)),
            "unknown": unknown,
            "proven": [r for r in rows if proven(r)],
            "revenue": round(sum(float(r["revenue_usd"]) for r in rows
                                 if proven(r)), 2),
            "ambiguous": len(ambiguous), "google_family": len(google),
            "acq_members": acq_members, "acq_unknown": acq_unknown,
            "acq_revenue_proven": all(proven(r) for r in acq_members)}


def _numbers(value, path=""):
    """Paths of every number or non-empty list under ``value``."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return []
    if isinstance(value, (int, float)):
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _numbers(v, f"{path}.{k}")]
    if isinstance(value, (list, tuple)):
        return [path] if value else []
    return []


def window_checks(a: Audit, w: dict, universe: dict, *, now) -> None:
    """Checks 1–15 for one window, re-derived from the snapshot."""
    UNAVAILABLE, PUBLISHED, NOT_PUBLISHED = \
        "unavailable", "published", "not_published"

    name = f"{w['window']['window_type']}:{w['window']['window_key']}"
    pub = w["publication"]
    for metric in ("roas", "cac"):
        a.check("roas_and_cac_not_published",
                pub[metric]["status"] == NOT_PUBLISHED, f"{name} {metric}")
    if not w.get("available"):
        for metric, p in pub.items():
            if metric in ("roas", "cac"):
                continue
            a.check("unavailable_is_never_a_number",
                    p["status"] == UNAVAILABLE, f"{name} {metric}")
        a.check("unavailable_is_never_a_number",
                all(v is None for v in w["outcomes"].values()), name)
        return

    start, end, is_all_time = _independent_bounds(
        w["window"]["window_type"], w["window"]["window_key"], now)
    exp = _expected(universe, start, end, is_all_time, now)
    out, members = w["outcomes"], exp["rows"]
    findings = universe.get("coverage_findings")
    proven_coverage = findings == []

    # 15. Freshness comes from sync coverage, never the newest deal row; an
    # unproven gate makes EVERY metric unavailable and every number absent.
    fresh = w["coverage"]["source_freshness"]
    a.check("freshness_from_sync_coverage",
            "hubspot_deal_sync_state" in (fresh.get("signal") or ""), name)
    a.check("freshness_from_sync_coverage",
            fresh.get("coverage_proven") is proven_coverage, name)
    if not proven_coverage:
        for metric, p in pub.items():
            if metric not in ("roas", "cac"):
                a.check("unproven_coverage_withholds",
                        p["status"] == UNAVAILABLE, f"{name} {metric}")
        leaked = [p for key, v in w.items() if key not in _NON_NUMERIC_KEYS
                  and key != "coverage" for p in _numbers(v, key)]
        leaked += [p for k, v in w["coverage"].items()
                   if k != "source_freshness"
                   for p in _numbers(v, f"coverage.{k}")]
        a.check("unproven_coverage_withholds", not leaked,
                f"{name}: numbers under unproven coverage: {leaked[:5]}")
        return

    count_published = pub["closed_won_deals"]["status"] == PUBLISHED
    # 1. Won predicate, and the flag/stage cross-check re-derived from the raw
    # definition rows. A conflict that could touch the window withholds the
    # count AND the acquisition cohort.
    a.check("closed_won_predicate",
            all(r.get("hs_is_closed_won") is True for r in exp["won"].values()),
            f"{name}: a non-won row in the won universe")
    reported = sorted(str(c.get("deal_id")) for c in
                      w["membership"]["won_definition_conflicts"] or [])
    a.check("won_definition_conflict_withholds", reported == exp["conflicts"],
            f"{name}: conflicts {reported} != re-derived {exp['conflicts']}")
    if exp["conflicts"]:
        a.check("won_definition_conflict_withholds", not count_published,
                f"{name}: published over a won flag/stage disagreement")
    if exp["acq_conflicts"]:
        a.check("won_definition_conflict_withholds",
                pub["acquisition_cohort"]["status"] != PUBLISHED,
                f"{name}: acquisition cohort published over a conflict")
    # 11. Undated won deals disclosed; a finite window withheld over them.
    a.check("missing_close_dates_disclosed",
            w["membership"]["undated_won_deals"] == exp["undated"], name)
    if exp["undated"] and not is_all_time:
        a.check("missing_close_dates_disclosed", not count_published,
                f"{name}: published a finite-window count over "
                f"{exp['undated']} undated won deal(s)")
    # Deals whose won state is unknown are disclosed, never dropped (docs/35).
    a.check("unknown_won_state_disclosed",
            w["membership"].get("unknown_won_state_deals") == exp["unknown"],
            f"{name}: unknown-won-state {w['membership'].get('unknown_won_state_deals')}"
            f" != re-derived {exp['unknown']}")

    if count_published:
        ids = w["membership"]["deal_ids"] or []
        # 2. Deal ids unique. 14. Membership is the close date, half-open,
        # never after now — against independently computed bounds.
        a.check("deal_ids_unique", len(ids) == len(set(ids)),
                f"{name}: duplicate deal id")
        a.check("membership_uses_close_date", sorted(ids) == exp["members"],
                f"{name}: membership {len(ids)} != re-derived "
                f"{len(exp['members'])}")
        a.check("membership_reconciles",
                out["closed_won_deals"] == len(exp["members"]),
                f"{name}: published {out['closed_won_deals']} won deals, "
                f"re-derived membership is {len(exp['members'])}")
        # 3/8/9. Partition: every member in exactly one bucket, and the
        # buckets re-derivable from the rows' own evidence.
        cov = w["coverage"]["campaign_attribution"] or {}
        if not all(isinstance(v, int) for v in cov.values()) or not cov:
            # A published count beside a redacted partition is a broken
            # contract, not something to sum.
            a.check("attribution_partition_reconciles", False,
                    f"{name}: published count with partition {cov}")
            cov = {}
        a.check("attribution_partition_reconciles",
                sum(cov.values()) == len(members),
                f"{name}: buckets {cov} != {len(members)} deals")
        a.check("attribution_partition_reconciles",
                cov.get("ambiguous") == exp["ambiguous"],
                f"{name}: ambiguous {cov.get('ambiguous')} != "
                f"{exp['ambiguous']}")
        family = sum(cov.get(b, 0) for b in ("campaign_attributable",
                                             "google_ads_unplaced",
                                             "excluded_by_approved_mapping"))
        a.check("attribution_partition_reconciles",
                family == exp["google_family"],
                f"{name}: Google Ads evidence {family} != "
                f"{exp['google_family']}")
    else:
        # Withheld means absent: no id, bucket, coverage distribution or
        # confirmed count from which the withheld number could be rebuilt.
        leaked = _numbers(w["membership"]["deal_ids"], "membership.deal_ids")
        leaked += [p for p in _numbers(w["attribution"], "attribution")]
        leaked += [p for k, v in out.items() if k not in ()
                   for p in _numbers(v, f"outcomes.{k}")]
        leaked += [p for k in ("deal_identity", "close_date_of_members",
                               "customer_identity", "amount", "currency",
                               "contact_association", "campaign_attribution")
                   for p in _numbers(w["coverage"].get(k),
                                     f"coverage.{k}")]
        a.check("withheld_is_never_a_number", not leaked,
                f"{name}: withheld count recoverable from {leaked[:5]}")

    # 4/13. Revenue: distinct deals, each with independently proven USD.
    if pub["revenue_usd"]["status"] == PUBLISHED:
        a.check("revenue_only_published_when_every_deal_proven",
                count_published and len(exp["proven"]) == len(members)
                and out["revenue_usd"] == exp["revenue"],
                f"{name}: published revenue {out['revenue_usd']} over "
                f"{len(members) - len(exp['proven'])} unproven deal(s); "
                f"re-derived {exp['revenue']}")
    else:
        a.check("withheld_is_never_a_number", out["revenue_usd"] is None,
                f"{name}: revenue_usd={out['revenue_usd']} while "
                f"{pub['revenue_usd']['status']}")
        if count_published:
            leaked = [p for p in _numbers(w["attribution"], "attribution")
                      if p.endswith(".revenue_usd")]
            a.check("withheld_is_never_a_number", not leaked,
                    f"{name}: revenue withheld but present at {leaked[:5]}")
    # 5. Customers: only distinct proven identities, never a deal count.
    if pub["customers"]["status"] == PUBLISHED:
        a.check("customer_count_is_distinct_identities",
                count_published and out["unresolved_customer_identity"] == 0,
                name)
    else:
        a.check("withheld_is_never_a_number", out["customers"] is None,
                f"{name}: customers={out['customers']} while withheld")
    # Campaign revenue: only over published revenue with every Google Ads
    # deal placed — otherwise each campaign figure is a lower bound.
    if pub["campaign_revenue"]["status"] == PUBLISHED:
        a.check("campaign_revenue_only_when_every_deal_placed",
                pub["revenue_usd"]["status"] == PUBLISHED
                and w["coverage"]["campaign_attribution"].get(
                    "google_ads_unplaced") == 0, name)
    else:
        a.check("withheld_is_never_a_number",
                w["attribution"]["by_campaign"] is None,
                f"{name}: campaign breakdown while withheld")
    # Acquisition cohort, re-derived from contact creation.
    acq = w["acquisition_cohort"]
    if pub["acquisition_cohort"]["status"] == PUBLISHED:
        a.check("acquisition_cohort_reconciles",
                exp["acq_unknown"] == 0 and acq["closed_won_deals"]
                == len(exp["acq_members"]),
                f"{name}: acquisition {acq['closed_won_deals']} != re-derived "
                f"{len(exp['acq_members'])} ({exp['acq_unknown']} unresolved)")
    else:
        a.check("withheld_is_never_a_number",
                acq["closed_won_deals"] is None and acq["revenue_usd"] is None,
                f"{name}: acquisition numbers while withheld")
    if pub["acquisition_revenue"]["status"] == PUBLISHED:
        a.check("acquisition_cohort_reconciles",
                pub["acquisition_cohort"]["status"] == PUBLISHED
                and exp["acq_revenue_proven"], name)


def sql_windows(now) -> dict:
    """``{business_key: (start, end)}`` — the bounds production queries."""
    from analysis.business_windows import WINDOW_KEYS  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415
    return {k: crs.won_window_sql_bounds(k, now=now) for k in WINDOW_KEYS}


def sql_cross_check(a: Audit, w: dict, universe: dict, *, now) -> dict | None:
    """Production's own SQL windowing for a business window, same snapshot.

    The SQL filters on the window only; a close after ``now`` is dropped here
    because it has not happened. The remaining ids must be exactly the
    window's DATED membership as this audit re-derives it. Production's All
    Time is bounded above, so its SQL returns no undated deal: those are
    reported beside the comparison, never hidden in it.
    """
    if not w.get("available") or w["window"]["window_type"] != "business":
        return None
    key = w["window"]["window_key"]
    rows = (universe.get("sql_windowed") or {}).get(key)
    if rows is None:
        # Not a measured mismatch: the comparison never ran. run() reports
        # the audit unavailable for this, never a broken contract.
        return {"window": key, "unavailable": True}
    start, end, is_all_time = _independent_bounds("business", key, now)
    exp = _expected(universe, start, end, is_all_time, now)
    future = sorted(r["deal_id"] for r in rows
                    if _dt(r.get("deal_close_date")) is not None
                    and _dt(r["deal_close_date"]) > now)
    sql_ids = sorted({r["deal_id"] for r in rows} - set(future))
    dated = sorted(i for i in exp["members"]
                   if _dt(exp["won"][i].get("deal_close_date")) is not None)
    a.check("sql_window_cross_check", sql_ids == dated,
            f"business:{key}: production SQL {len(sql_ids)} deal(s) != "
            f"re-derived dated membership {len(dated)}")
    return {"window": key, "production_sql_deals": len(sql_ids),
            "production_sql_future_dated_excluded": len(future),
            "dated_members": len(dated),
            "undated_members": len(exp["members"]) - len(dated)}


def source_diagnostics(universe: dict, now) -> dict:
    """Operator diagnostics over the whole snapshot. NOT a published figure:
    an audit explains data gaps; it is not a surface that publishes counts."""
    won = universe.get("won_rows") or []

    def tally(values):
        out: dict = {}
        for v in values:
            out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: str(kv[0])))

    exp = _expected(universe, None, datetime.max.replace(tzinfo=timezone.utc),
                    True, now)
    return {
        "label": "source diagnostics — not a published figure",
        "won_flag_rows": len(won),
        "missing_close_date": exp["undated"],
        "future_close_date": sum(1 for r in won if _dt(r.get("deal_close_date"))
                                 and _dt(r["deal_close_date"]) > now),
        "unknown_won_state": (len(universe["unknown_won_rows"])
                              if universe.get("unknown_won_rows") is not None
                              else None),
        "won_flag_stage_conflicts": len(exp["conflicts"]),
        "currency_status": tally(r.get("currency_status") for r in won),
        "association_status": tally(r.get("association_status") for r in won),
        "without_proven_usd": len(won) - len(exp["proven"]),
    }


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
    report["source_diagnostics"] = source_diagnostics(universe, now)
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
    diag = report.get("source_diagnostics") or {}
    print("\n  SOURCE DIAGNOSTICS — whole snapshot, not a published figure")
    for key, label in (("won_flag_rows", "rows with the won flag"),
                       ("missing_close_date", "missing close date"),
                       ("future_close_date", "future-dated (invalid) close"),
                       ("unknown_won_state", "won state unknown (NULL flag)"),
                       ("won_flag_stage_conflicts", "won flag / stage conflicts"),
                       ("without_proven_usd", "without proven USD"),
                       ("currency_status", "currency status"),
                       ("association_status", "contact association")):
        if key in diag:
            print(f"    {label:<30}{_fmt(diag[key]) if not isinstance(diag[key], dict) else diag[key]}")
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
