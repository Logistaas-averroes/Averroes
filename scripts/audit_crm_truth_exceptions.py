#!/usr/bin/env python3
"""
scripts/audit_crm_truth_exceptions.py

PR-ADS-191 — read-only certification that the CRM Truth Exceptions workbench
represents the evidence truthfully.

    python -m scripts.audit_crm_truth_exceptions
    python -m scripts.audit_crm_truth_exceptions --json

Reads ONE snapshot of the canonical closed-won universe (REPEATABLE READ, READ
ONLY) and proves:

  * every exception row carries a canonical reason of its own family, a stable
    unique id and a plain-language explanation;
  * every withheld customer / revenue verdict, in every supported window, is
    explained by a named blocker — a record row or a dataset-level blocker —
    and no PUBLISHED total has an unresolved blocker among its members;
  * the workbench's per-window blocker counts equal the canonical truth
    service's own counts for the same window;
  * the identity rows equal the coverage audit's independent derivation from
    the raw association rows, and the revenue rows contain every deal PR-ADS-155's
    missing-amount report names (the only extra being negative / invalid
    amounts the closed-won gate refuses);
  * paging the API read through every page, every filter, and the CSV export
    lose and duplicate no row;
  * the existing customer-truth and association-coverage audits hold.

A CRM finding is a data-quality fact, not a contract failure: genuine
no-company, multi-company or unpriced won deals do NOT fail this audit.

No HubSpot call. No Google Ads call. No database write.

Exit codes
    0  the workbench represents the evidence truthfully (findings may be listed)
    1  a contract is violated: readers disagree, a blocker is lost, a withheld
       verdict is unexplained, or an existing truth audit is violated
    2  unavailable — the evidence or an embedded audit could not be read
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

SERVICE_FILE = "services/crm_truth_exceptions_service.py"
AUDIT_FILE = "scripts/audit_crm_truth_exceptions.py"
FORBIDDEN_IMPORTS = ("db.writers", "writers", "connectors.hubspot_pull",
                     "hubspot_pull", "connectors.google_ads_direct",
                     "google_ads_direct", "connectors.google_ads_source",
                     "connectors.hubspot_deals")
#: Keys whose presence in a window summary would republish a total.
TOTAL_KEYS = ("outcomes", "attribution", "membership")


class Audit:
    def __init__(self) -> None:
        self.violations: list = []
        self.checks: dict = {}

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = self.checks.get(name, True) and bool(ok)
        if not ok:
            self.violations.append(f"{name}: {detail}")


def _imports_of(rel: str) -> set:
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(x.name for x in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{x.name}" for x in node.names)
    return names


def structural_checks(a: Audit) -> None:
    for rel in (SERVICE_FILE, AUDIT_FILE):
        bad = sorted(n for n in _imports_of(rel) if n in FORBIDDEN_IMPORTS)
        a.check("no_external_or_write_path", not bad, f"{rel} imports {bad}")


def row_checks(a: Audit, model: dict) -> None:
    from services import crm_truth_exceptions_service as cte  # noqa: PLC0415

    vocab = cte.reason_vocabulary()
    rows = model["rows"]
    ids = [r["row_id"] for r in rows]
    a.check("row_ids_are_unique", len(ids) == len(set(ids)),
            f"{len(ids) - len(set(ids))} duplicate row id(s)")
    for r in rows:
        a.check("row_id_is_deal_family_reason",
                r["row_id"] == f"{r['deal_id']}:{r['exception_family']}:"
                               f"{r['reason_code']}", r["row_id"])
        a.check("reason_is_canonical_for_its_family",
                r["reason_code"] in vocab.get(r["exception_family"], ()),
                f"{r['row_id']}: {r['reason_code']}")
        a.check("every_row_is_explained", bool(r.get("explanation")),
                r["row_id"])
        a.check("no_proven_usd_on_an_unproven_revenue_row",
                r["exception_family"] != cte.FAMILY_REVENUE
                or r["revenue_usd"] is None, r["row_id"])
        ids_ = r.get("associated_company_ids") or []
        a.check("no_company_picked_from_several",
                r["resolved_company_id"] is None or len(ids_) == 1,
                f"{r['row_id']}: resolved {r['resolved_company_id']} "
                f"from {ids_}")
        a.check("no_company_name_invented",
                r.get("associated_company_names") is None, r["row_id"])
        url = r.get("hubspot_record_url")
        a.check("hubspot_link_is_safe",
                url is None or (url.startswith("https://app.hubspot.com/")
                                and "@" not in url and "?" not in url),
                f"{r['row_id']}: {url}")


def window_checks(a: Audit, model: dict) -> list:
    """Every withheld verdict explained; every count reconciled."""
    summary = []
    for w in model["windows"]:
        a.check("window_summary_republishes_no_total",
                not any(k in w for k in TOTAL_KEYS), w["window"])
        for metric, e in w["explanation"].items():
            a.check("withheld_verdict_is_explained_and_published_total_is_clean",
                    e.get("explained") is True,
                    f"{w['window']} {metric}: {w['publication'][metric]} "
                    f"{e}")
        b, c = w["blocker_deals"], w["canonical_counts"]
        if w["membership_published"]:
            for mine, theirs in (
                    ("customer_identity_unresolved",
                     "unresolved_customer_identity"),
                    ("customer_identity_stale", "stale_customer_identity"),
                    ("revenue_unproven", "deals_without_proven_usd")):
                a.check("window_counts_reconcile_with_truth_service",
                        b[mine] == c[theirs],
                        f"{w['window']}: workbench {mine}={b[mine]}, truth "
                        f"service {theirs}={c[theirs]}")
        summary.append({"window": w["window"],
                        "customers": w["publication"]["customers"],
                        "revenue_usd": w["publication"]["revenue_usd"],
                        "blocker_deals": b})
    return summary


def independent_checks(a: Audit, model: dict, universe: dict) -> dict:
    """The rows against readers that do NOT share the workbench's code path."""
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    from scripts import audit_deal_company_association_coverage as cov  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415
    from services import crm_truth_exceptions_service as cte  # noqa: PLC0415

    rows = model["rows"]

    def deals(family, reasons):
        return {r["deal_id"] for r in rows if r["exception_family"] == family
                and r["reason_code"] in reasons}

    out = {}
    if model.get("identity_evidence_readable"):
        identity = cov.independent_identity(universe)
        for state in (cov.NO_COMPANY, cov.AMBIGUOUS, cov.UNAVAILABLE):
            theirs = {d for d, i in identity.items() if i["state"] == state}
            mine = deals(cte.FAMILY_IDENTITY, (state,))
            a.check("identity_rows_match_coverage_audit", mine == theirs,
                    f"{state}: workbench {sorted(mine)[:10]}, coverage "
                    f"audit {sorted(theirs)[:10]}")
            out[state] = len(mine)
        theirs_stale = {d for d, i in identity.items() if i["stale"]}
        mine_stale = deals(cte.FAMILY_IDENTITY, cwt.STALE_EVIDENCE_REASONS)
        a.check("stale_rows_match_coverage_audit", mine_stale == theirs_stale,
                f"workbench {sorted(mine_stale)[:10]}, coverage audit "
                f"{sorted(theirs_stale)[:10]}")
        out["stale"] = len(mine_stale)
    # PR-ADS-155's missing-amount report, over the same won rows.
    pr155 = crs.missing_amount_deals(
        {"available": True, "deals": universe.get("won_rows") or []})
    theirs = {str(d["deal_id"]) for d in pr155.get("deals") or []}
    mine = {r["deal_id"] for r in rows
            if r["exception_family"] == cte.FAMILY_REVENUE}
    extra = mine - theirs
    a.check("revenue_rows_contain_every_missing_amount_deal", theirs <= mine,
            f"named by PR-ADS-155 but missing here: {sorted(theirs - mine)}")
    extra_reasons = {r["reason_code"] for r in rows
                     if r["deal_id"] in extra
                     and r["exception_family"] == cte.FAMILY_REVENUE}
    a.check("revenue_rows_beyond_pr155_are_invalid_amounts_only",
            extra_reasons <= {cwt.AMOUNT_NEGATIVE, cwt.AMOUNT_INVALID},
            f"extra deals {sorted(extra)} with {sorted(extra_reasons)}")
    out["revenue_unproven"] = len(mine)
    out["pr155_missing_amount"] = len(theirs)
    return out


def serialization_checks(a: Audit, model: dict) -> dict:
    """The API's paging, every filter and the CSV export, against the model."""
    from services import crm_truth_exceptions_service as cte  # noqa: PLC0415

    all_ids = [r["row_id"] for r in model["rows"]]
    paged, offset = [], 0
    while True:
        page = cte.query(model, limit=7, offset=offset)["rows"]
        if not page:
            break
        paged.extend(r["row_id"] for r in page)
        offset += len(page)
    a.check("paging_loses_and_duplicates_no_row", paged == all_ids,
            f"paged {len(paged)} rows, model has {len(all_ids)}")

    def csv_ids(rows):
        reader = csv.DictReader(io.StringIO(cte.to_csv(rows)))
        a.check("csv_columns_are_stable",
                tuple(reader.fieldnames or ()) == cte.CSV_COLUMNS,
                str(reader.fieldnames))
        return [r["row_id"] for r in reader]

    a.check("csv_matches_the_model", csv_ids(model["rows"]) == all_ids,
            "CSV rows differ from the read model")
    checked = 0
    filters = [{"window": w["window"]} for w in model["windows"]]
    filters += [{"family": f} for f in cte.FAMILIES]
    filters += [{"reason": r} for r in {r["reason_code"]
                                        for r in model["rows"]}]
    for f in filters:
        expected = [r["row_id"] for r in model["rows"]
                    if ("window" not in f
                        or f["window"] in r["affected_windows"])
                    and ("family" not in f
                         or r["exception_family"] == f["family"])
                    and ("reason" not in f or r["reason_code"] == f["reason"])]
        api = cte.query(model, limit=cte.MAX_LIMIT, **f)
        got = [r["row_id"] for r in api["rows"]]
        a.check("api_filter_matches_the_model",
                got == expected and api["pagination"]["total"]
                == len(expected), f"{f}: api {len(got)}, model "
                                  f"{len(expected)}")
        a.check("csv_filter_matches_the_api",
                csv_ids(cte.filter_rows(model, **f)) == got, str(f))
        checked += 1
    return {"pages_read": offset, "filters_checked": checked}


def embedded_audits(a: Audit, now) -> dict:
    from scripts import audit_customer_closed_won_truth as cw  # noqa: PLC0415
    from scripts import audit_deal_company_association_coverage as cov  # noqa: PLC0415

    _, cw_report = cw.run(now=now)
    _, cov_report = cov.run(now=now)
    for name, report in (("customer_closed_won_truth", cw_report),
                         ("deal_company_association_coverage", cov_report)):
        a.check(f"{name}_audit_holds",
                report.get("exit_code") != EXIT_VIOLATION,
                f"{name}: {report.get('violations')}")
    return {"customer_closed_won_truth": cw_report.get("exit_code"),
            "deal_company_association_coverage": cov_report.get("exit_code"),
            "coverage_crm_findings": cov_report.get("crm_findings"),
            "unavailable": [n for n, r in (
                ("customer_closed_won_truth", cw_report),
                ("deal_company_association_coverage", cov_report))
                if r.get("exit_code") == EXIT_UNAVAILABLE]}


def run(now: datetime | None = None, *, with_embedded: bool = True) -> tuple:
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415
    from services import crm_truth_exceptions_service as cte  # noqa: PLC0415

    now = now or datetime.now(tz=timezone.utc)
    a = Audit()
    structural_checks(a)
    report = {"audit": "crm_truth_exceptions",
              "generated_at": now.isoformat(),
              "external_calls_performed": False,
              "hubspot_calls_performed": False,
              "google_ads_calls_performed": False,
              "database_writes_performed": False}
    universe = crs.load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID)
    model = cte.build_crm_truth_exceptions(now=now, universe=universe)
    report["workbench_status"] = model["status"]
    report["dataset_blockers"] = model["dataset_blockers"]
    if not model.get("available"):
        report.update(audit_complete=False, checks=a.checks,
                      violations=a.violations, exit_code=EXIT_UNAVAILABLE,
                      verdict="unavailable")
        return a, report
    report["won_deal_population"] = model["won_deal_population"]
    report["association_scan"] = (model.get("association_scan") or {}).get(
        "scan_current")
    report["totals"] = model["totals"]
    row_checks(a, model)
    report["windows"] = window_checks(a, model)
    report["independent"] = independent_checks(a, model, universe)
    report["serialization"] = serialization_checks(a, model)
    if with_embedded:
        report["embedded_audits"] = embedded_audits(a, now)
    unavailable = (report.get("embedded_audits") or {}).get("unavailable")
    report["audit_complete"] = not unavailable and model.get(
        "identity_evidence_readable") is True
    report["checks"] = a.checks
    report["violations"] = a.violations
    if a.violations:
        report["exit_code"], report["verdict"] = EXIT_VIOLATION, \
            "contract_broken"
    elif not report["audit_complete"]:
        report["exit_code"], report["verdict"] = EXIT_UNAVAILABLE, \
            "unavailable"
    else:
        report["exit_code"], report["verdict"] = EXIT_OK, \
            "represents_the_evidence_truthfully"
    return a, report


def _render(report: dict) -> None:
    print("=" * 84)
    print("  PR-ADS-191 — CRM TRUTH EXCEPTIONS AUDIT (read-only)")
    print(f"  verdict: {report.get('verdict')}   audit complete: "
          f"{report.get('audit_complete')}")
    print("  external calls performed: False   database writes performed: "
          "False")
    print("=" * 84)
    print(f"  workbench status             {report.get('workbench_status')}")
    for b in report.get("dataset_blockers") or []:
        print(f"  dataset blocker              {b.get('code')} "
              f"({b.get('detail')})")
    if report.get("won_deal_population") is None:
        print("  UNAVAILABLE — the evidence could not be read.")
        return
    print(f"  current won-deal population  {report['won_deal_population']}")
    print(f"  association scan             {report.get('association_scan')}")
    for fam, t in (report.get("totals") or {}).get("by_family", {}).items():
        print(f"  {fam:<29}{t['deals']} deal(s), {t['rows']} row(s)  "
              f"{t['by_reason']}")
    print(f"  independent reconciliation   {report.get('independent')}")
    print(f"  JSON / CSV reconciliation    {report.get('serialization')}")
    emb = report.get("embedded_audits") or {}
    print(f"  customer truth audit exit    "
          f"{emb.get('customer_closed_won_truth')}")
    print(f"  association coverage exit    "
          f"{emb.get('deal_company_association_coverage')}")
    print()
    print(f"  {'window':<28}{'customers':<44}{'revenue_usd'}")
    for w in report.get("windows") or []:
        c, r = w["customers"], w["revenue_usd"]
        print(f"  {w['window']:<28}"
              f"{(c['status'] + ' ' + (c['reason'] or ''))[:43]:<44}"
              f"{r['status']} {r['reason'] or ''}")
    if report.get("violations"):
        print("\n  CONTRACT VIOLATIONS")
        for v in report["violations"]:
            print(f"    - {v}")
    else:
        print("\n  Exit 0 means the workbench represents the evidence "
              "truthfully — not that\n  HubSpot has no data-quality findings.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only certification of the CRM Truth Exceptions "
                    "workbench")
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
