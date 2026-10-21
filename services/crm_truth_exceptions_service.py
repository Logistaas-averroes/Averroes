"""
services/crm_truth_exceptions_service.py

PR-ADS-191 — the CRM Truth Exceptions read model: which HubSpot won deals stop
a customer or closed-won revenue total from being published, and why.

Not a second classifier
-----------------------
Every verdict here is the canonical one, consumed — never restated:

* the won population, its snapshot and the coverage gate come from
  ``services.canonical_revenue_service.load_closed_won_universe`` (one
  REPEATABLE READ, READ ONLY transaction);
* window membership and every publication verdict come from
  ``services.canonical_customer_revenue_service.get_closed_won_truth`` — a deal
  is attributed to a window only through that window's own
  ``membership.deal_ids``, which the truth service publishes only when the
  window's deal count is published (withheld means absent, here too);
* per-deal states come from ``analysis.closed_won_truth`` (customer identity,
  amount, currency, close date, won-definition agreement, stale evidence) and
  the ledger's own ``analysis.deal_currency`` reason, verbatim — the same
  vocabulary ``/api/audit/missing-deal-amounts`` (PR-ADS-155 §6) already shows;
* scan currency comes from ``association_scan_current`` and the certified set
  from ``scan_certification_sets`` (PR-ADS-161D2-F3).

What this module adds is the record-level view those summary verdicts are
computed from, and an explicit check that every withheld customer or revenue
verdict is explained by a named blocker.

A CRM finding is a data-quality fact about a HubSpot record, not a software
failure. Totals stay withheld until the source record is corrected in HubSpot
and the next scan reads it back.

Read-only: no HubSpot call, no Google Ads call, no write of any kind. Nothing
here picks a company, infers a currency or fills an amount.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from analysis import closed_won_truth as cwt
from analysis.deal_currency import is_summable

# ── Exception families ──────────────────────────────────────────────────────
FAMILY_IDENTITY = "customer_identity"
FAMILY_REVENUE = "revenue_proof"
#: The won deal's membership itself is unproven (no close date, or HubSpot's
#: won flag and the won stage disagree). The deal-count verdict it blocks
#: cascades into customers and revenue.
FAMILY_MEMBERSHIP = "won_deal_membership"
FAMILIES = (FAMILY_IDENTITY, FAMILY_REVENUE, FAMILY_MEMBERSHIP)

METRIC_DEALS = "closed_won_deals"
METRIC_CUSTOMERS = "customers"
METRIC_REVENUE = "revenue_usd"
FAMILY_METRICS = {
    FAMILY_IDENTITY: (METRIC_CUSTOMERS,),
    FAMILY_REVENUE: (METRIC_REVENUE,),
    FAMILY_MEMBERSHIP: (METRIC_DEALS, METRIC_REVENUE, METRIC_CUSTOMERS),
}

# ── Overall workbench status ────────────────────────────────────────────────
STATUS_CERTIFIED_NO_FINDINGS = "certified_no_findings"
STATUS_CERTIFIED_WITH_FINDINGS = "certified_with_findings"
STATUS_SOURCE_STALE = "source_stale"
STATUS_SCAN_NOT_CURRENT = "scan_not_current"
STATUS_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
STATUSES = (STATUS_CERTIFIED_NO_FINDINGS, STATUS_CERTIFIED_WITH_FINDINGS,
            STATUS_SOURCE_STALE, STATUS_SCAN_NOT_CURRENT,
            STATUS_EVIDENCE_UNAVAILABLE)

# ── Dataset-level blockers (no single record carries them) ──────────────────
DATASET_LEDGER_UNREADABLE = cwt.R_SOURCE_UNREADABLE
DATASET_COVERAGE_NOT_PROVEN = cwt.R_COVERAGE_NOT_PROVEN
DATASET_COMPANY_EVIDENCE_UNREADABLE = cwt.R_COMPANY_ASSOCIATIONS_UNREADABLE
DATASET_SCAN_NOT_CURRENT = cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT

# ── Reason vocabulary — every code is a canonical one ───────────────────────
IDENTITY_REASONS = (cwt.CUSTOMER_NO_COMPANY, cwt.CUSTOMER_AMBIGUOUS,
                    cwt.CUSTOMER_ASSOCIATION_UNAVAILABLE,
                    *cwt.STALE_EVIDENCE_REASONS,
                    cwt.NOT_CERTIFIED_BY_COMPLETE_SCAN)


def _revenue_reasons() -> tuple:
    from analysis import deal_currency as dc  # noqa: PLC0415
    from services.canonical_revenue_service import (  # noqa: PLC0415
        MISSING_REASON_UNRECORDED,
    )
    return (dc.REASON_NO_AMOUNT, dc.REASON_UNKNOWN_CURRENCY,
            dc.REASON_HOME_CURRENCY_UNVERIFIED, dc.REASON_NO_CLOSE_DATE,
            dc.REASON_NO_FX_RATE, MISSING_REASON_UNRECORDED,
            cwt.AMOUNT_NEGATIVE, cwt.AMOUNT_INVALID)


MEMBERSHIP_REASONS = (cwt.CLOSE_MISSING, cwt.WON_FLAG_OTHER_STAGE,
                      cwt.WON_STAGE_FLAG_NOT_TRUE)


def reason_vocabulary() -> dict:
    """``{family: (reason, ...)}`` — the only codes a row may carry."""
    return {FAMILY_IDENTITY: IDENTITY_REASONS,
            FAMILY_REVENUE: _revenue_reasons(),
            FAMILY_MEMBERSHIP: MEMBERSHIP_REASONS}


#: Plain-language, factual. Never "probably", "assumed" or "estimated".
EXPLANATIONS = {
    cwt.CUSTOMER_NO_COMPANY: (
        "No company was returned by the latest successful HubSpot association "
        "lookup."),
    cwt.CUSTOMER_AMBIGUOUS: (
        "This won deal is associated with multiple HubSpot companies, so one "
        "customer identity cannot be proven."),
    cwt.CUSTOMER_ASSOCIATION_UNAVAILABLE: (
        "No HubSpot association lookup for this deal has ever succeeded, so "
        "its customer identity is unknown."),
    cwt.STALE_LATEST_ATTEMPT_FAILED: (
        "The latest HubSpot association lookup for this deal failed; the "
        "stored company evidence is from an earlier successful lookup."),
    cwt.STALE_DEAL_MODIFIED_AFTER_OBSERVATION: (
        "The deal changed in HubSpot after its company associations were last "
        "read; the stored evidence may no longer describe it."),
    cwt.NOT_CERTIFIED_BY_COMPLETE_SCAN: (
        "The latest complete association scan did not certify this won deal."),
    "no_amount": "HubSpot holds no amount for this won deal.",
    "unknown_currency": (
        "The deal's revenue cannot be normalized to USD because its source "
        "currency is not proven."),
    "home_currency_unverified": (
        "The deal's revenue cannot be normalized to USD because the account's "
        "home currency is not verified."),
    "no_close_date_for_fx": (
        "The deal's revenue cannot be converted to USD because it has no close "
        "date to choose an exchange rate."),
    "no_fx_rate_for_close_date": (
        "The deal's revenue cannot be converted to USD because no exchange "
        "rate is held for its close date."),
    "currency_reason_not_recorded": (
        "The deal's USD value is not proven and the ledger recorded no "
        "reason."),
    cwt.AMOUNT_NEGATIVE: (
        "The deal's amount is negative; a won deal's value cannot be "
        "negative, so it is not summed."),
    cwt.AMOUNT_INVALID: "The deal's amount is not a valid number.",
    cwt.CLOSE_MISSING: (
        "This won deal has no close date, so the windows it belongs to cannot "
        "be proven."),
    cwt.WON_FLAG_OTHER_STAGE: (
        "HubSpot flags this deal won, but it is not in the confirmed won "
        "stage."),
    cwt.WON_STAGE_FLAG_NOT_TRUE: (
        "This deal is in the confirmed won stage, but HubSpot does not flag "
        "it won."),
    DATASET_LEDGER_UNREADABLE: "The canonical deal ledger could not be read.",
    DATASET_COVERAGE_NOT_PROVEN: (
        "The deal ledger's sync coverage is not proven, so the won population "
        "may be incomplete."),
    DATASET_COMPANY_EVIDENCE_UNREADABLE: (
        "The stored HubSpot company-association evidence could not be read."),
    DATASET_SCAN_NOT_CURRENT: (
        "The latest complete association scan does not certify the current "
        "won-deal population."),
}

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

#: Stable CSV column order.
CSV_COLUMNS = (
    "row_id", "deal_id", "deal_name", "exception_family", "reason_code",
    "explanation", "hs_is_closed_won", "deal_close_date", "close_date_state",
    "amount_raw", "deal_currency_code", "revenue_usd", "currency_proof_status",
    "resolved_company_id", "associated_company_ids", "association_identity_state",
    "affected_windows", "affected_metrics", "latest_success_started_at",
    "latest_attempt_status", "latest_attempt_started_at",
    "latest_failure_reason", "hubspot_record_url",
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class CrmTruthExceptionsQueryError(ValueError):
    """A filter the workbench does not recognise (a 4xx, never an empty list)."""


def _window_id(window: dict) -> str:
    return f"{window['window_type']}/{window['window_key']}"


def _publication(w: dict, metric: str) -> dict:
    p = (w.get("publication") or {}).get(metric) or {}
    return {"status": p.get("status"), "reason": p.get("reason")}


def _revenue_row_reasons(row: dict) -> list:
    """Why this won deal's USD value is not proven — canonical codes only.

    The ledger's own ``analysis.deal_currency`` reason when the currency is not
    proven (verbatim, as ``missing_amount_deals`` reports it), and the truth
    module's amount state when the amount itself cannot be summed. At least one
    whenever ``cwt.revenue_is_proven`` is False.
    """
    from services.canonical_revenue_service import (  # noqa: PLC0415
        MISSING_REASON_UNRECORDED,
    )
    reasons = []
    if not (is_summable(row.get("currency_status"))
            and row.get("revenue_usd") is not None):
        reasons.append(row.get("currency_reason") or MISSING_REASON_UNRECORDED)
    amount = cwt.amount_state(row)
    if amount in (cwt.AMOUNT_NEGATIVE, cwt.AMOUNT_INVALID):
        reasons.append(amount)
    return reasons


def _hubspot_url(deal_id, portal_id):
    from services.canonical_revenue_service import (  # noqa: PLC0415
        _HUBSPOT_DEAL_URL,
    )
    # HubSpot ids are numeric; anything else is not interpolated into a link.
    if not (str(portal_id or "").isdigit() and str(deal_id or "").isdigit()):
        return None
    return _HUBSPOT_DEAL_URL.format(portal_id=portal_id, deal_id=deal_id)


def _unavailable(now, reason, detail, *, freshness=None, scan=None) -> dict:
    """Evidence cannot be read: an explicit state, never an empty list."""
    return {
        "available": False, "status": STATUS_EVIDENCE_UNAVAILABLE,
        "generated_at": now.isoformat(),
        "dataset_blockers": [{"code": reason,
                              "explanation": EXPLANATIONS.get(reason),
                              "detail": detail}],
        "source_freshness": freshness, "association_scan": scan,
        "won_deal_population": None,
        # NULL, not 0: a refused read proves nothing about how many blockers.
        "totals": None, "windows": None, "rows": None,
        **_read_only_flags(),
    }


def _read_only_flags() -> dict:
    return {"hubspot_calls_performed": False,
            "google_ads_calls_performed": False,
            "external_writes_performed": False,
            "database_writes_performed": False}


def build_crm_truth_exceptions(*, now: datetime | None = None,
                               universe: dict | None = None) -> dict:
    """The complete, unfiltered exception read model, from ONE snapshot.

    ``universe`` is injectable for tests; production reads it here.
    """
    from services import canonical_customer_revenue_service as svc  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415

    now = now or datetime.now(tz=timezone.utc)
    if universe is None:
        universe = crs.load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID)
    if not universe.get("available"):
        return _unavailable(now, DATASET_LEDGER_UNREADABLE,
                            universe.get("error") or universe.get("reason"))

    # The truth service on the SAME snapshot. The campaign resolver only feeds
    # campaign revenue, which this workbench does not report; not building it
    # keeps the read to the one snapshot.
    truth = svc.get_closed_won_truth(
        now=now, universe=universe, resolver=None,
        resolver_detail="not_used_by_crm_truth_exceptions")
    freshness = truth.get("freshness")
    scan = truth.get("company_association_freshness")
    if freshness and freshness.get("coverage_proven") is not True:
        # The rows are an unknown fraction of history: a list drawn from them
        # would present "these are the blockers" over an incomplete population.
        return _unavailable(now, DATASET_COVERAGE_NOT_PROVEN,
                            freshness.get("violation_codes"),
                            freshness=freshness, scan=scan)

    won_rows = universe.get("won_rows") or []
    evidence = universe.get("company_associations")
    company = svc.company_identity_inputs(evidence)
    identity_readable = company["company_ids_by_deal"] is not None
    observations = {str(o.get("deal_id")): o
                    for o in ((evidence or {}).get("observations") or [])} \
        if identity_readable else {}
    sync_state = (evidence or {}).get("sync_state") if identity_readable \
        else None
    won_ids = [str(r.get("deal_id")) for r in won_rows]
    cert = svc.scan_certification_sets(sync_state, won_ids)
    scan_verdict = (scan or {}).get("scan_current") or {}
    scan_current = scan_verdict.get("current") is True

    windows = truth.get("windows") or []
    members_by_window = {}
    for w in windows:
        ids = (w.get("membership") or {}).get("deal_ids")
        members_by_window[_window_id(w["window"])] = (
            None if ids is None else {str(d) for d in ids})

    def member_windows(deal_id) -> list:
        return [wid for wid, ids in members_by_window.items()
                if ids is not None and deal_id in ids]

    def deal_count_windows(deal_id, reason) -> list:
        out = []
        for w in windows:
            if reason == cwt.CLOSE_MISSING:
                # An undated won deal withholds the deal count of EVERY window
                # that cannot include it (all but All Time), whatever reason
                # that window happens to show first: a won-definition conflict
                # takes precedence in the verdict but does not lift this one.
                # ``undated_included`` is the truth service's own flag.
                if (w.get("membership") or {}).get("undated_included") is False:
                    out.append(_window_id(w["window"]))
            else:
                conflicts = (w.get("membership") or {}).get(
                    "won_definition_conflicts") or []
                if any(str(c.get("deal_id")) == deal_id for c in conflicts):
                    out.append(_window_id(w["window"]))
        return out

    from services.canonical_revenue_service import hubspot_portal_id  # noqa: PLC0415
    portal_id = hubspot_portal_id()
    by_id = {str(r.get("deal_id")): r for r in won_rows}
    rows = []

    def add(deal_id, family, reason, windows_hit, *, base=None):
        won_row = by_id.get(deal_id)
        base = won_row if won_row is not None else (base or {})
        obs = observations.get(deal_id) or {}
        ids = (company["company_ids_by_deal"] or {}).get(deal_id)
        distinct = sorted({str(c) for c in ids or [] if str(c or "").strip()})
        identity = (cwt.customer_identity_state(
            ids, source_available=identity_readable
            and deal_id in (company["company_ids_by_deal"] or {}))
            if identity_readable else None)
        rows.append({
            "row_id": f"{deal_id}:{family}:{reason}",
            "deal_id": deal_id,
            "deal_name": base.get("deal_name"),
            "exception_family": family,
            "reason_code": reason,
            "explanation": EXPLANATIONS.get(reason),
            "hs_is_closed_won": base.get("hs_is_closed_won"),
            "deal_close_date": base.get("deal_close_date"),
            "amount_raw": base.get("amount_raw"),
            "deal_currency_code": base.get("deal_currency_code"),
            # The proven USD value, or None — never an estimate. Revenue facts
            # exist only for a won deal's ledger row.
            "revenue_usd": (won_row.get("revenue_usd")
                            if won_row is not None
                            and cwt.revenue_is_proven(won_row) else None),
            "currency_proof_status": (cwt.currency_state(won_row)
                                      if won_row is not None else None),
            # One company only when exactly one is proven; never a pick.
            "resolved_company_id": (distinct[0] if identity in
                                    cwt.CUSTOMER_RESOLVED_STATES else None),
            "associated_company_ids": distinct if ids is not None else None,
            # Company names are not ingested (161D2 stores ids only): None,
            # never a name from a deal, domain or contact.
            "associated_company_names": None,
            "association_identity_state": identity,
            "affected_windows": sorted(windows_hit),
            "affected_metrics": list(FAMILY_METRICS[family]),
            # The canonical close-date state. ``invalid_close_date`` (a close
            # after now) is a deal no window contains yet: a finding that
            # blocks no total today. An empty ``affected_windows`` on any other
            # row means every window that could hold it withholds its deal
            # count — the membership blocker is listed instead.
            "close_date_state": cwt.close_date_state(base, now),
            "latest_success_started_at": obs.get("last_success_started_at"),
            "latest_success_at": obs.get("last_success_at"),
            "latest_attempt_status": obs.get("last_attempt_status"),
            "latest_attempt_started_at": obs.get("last_attempt_started_at"),
            "latest_failure_reason": (obs.get("last_failure_reason")
                                      if obs.get("last_attempt_status")
                                      == "failed" else None),
            "hubspot_record_url": _hubspot_url(deal_id, portal_id),
        })

    for r in won_rows:
        deal_id = str(r.get("deal_id"))
        hit = member_windows(deal_id)
        # ── customer identity ───────────────────────────────────────────────
        if identity_readable:
            state = cwt.customer_identity_state(
                company["company_ids_by_deal"].get(deal_id),
                source_available=deal_id in company["company_ids_by_deal"])
            if state not in cwt.CUSTOMER_RESOLVED_STATES:
                add(deal_id, FAMILY_IDENTITY, state, hit)
            for reason in company["stale_reasons"].get(deal_id) or ():
                add(deal_id, FAMILY_IDENTITY, reason, hit)
            # Only against a scan that exists: with no complete scan at all,
            # nothing is certified and the dataset-level blocker says so once
            # — not once per won deal.
            if cert["certified"] is not None \
                    and deal_id in cert["won_not_certified"]:
                add(deal_id, FAMILY_IDENTITY,
                    cwt.NOT_CERTIFIED_BY_COMPLETE_SCAN, hit)
        # ── revenue proof ───────────────────────────────────────────────────
        if not cwt.revenue_is_proven(r):
            for reason in _revenue_row_reasons(r):
                add(deal_id, FAMILY_REVENUE, reason, hit)
        # ── won-deal membership: an undated won deal ────────────────────────
        if cwt.close_date_state(r, now) == cwt.CLOSE_MISSING:
            add(deal_id, FAMILY_MEMBERSHIP, cwt.CLOSE_MISSING,
                deal_count_windows(deal_id, cwt.CLOSE_MISSING))
    # Won-definition disagreements (a deal in the won stage HubSpot does not
    # flag won is not in ``won_rows``, and is listed from its own row).
    for d in universe.get("won_definition_rows") or []:
        state = cwt.won_definition_state(d)
        if state == cwt.WON_DEFINITIONS_AGREE:
            continue
        deal_id = str(d.get("deal_id"))
        add(deal_id, FAMILY_MEMBERSHIP, state,
            deal_count_windows(deal_id, state), base=dict(d))

    rows.sort(key=lambda x: (x["deal_id"], FAMILIES.index(
        x["exception_family"]), x["reason_code"]))

    # ── dataset-level blockers ──────────────────────────────────────────────
    dataset = []
    if not identity_readable:
        dataset.append({"code": DATASET_COMPANY_EVIDENCE_UNREADABLE,
                        "explanation": EXPLANATIONS[
                            DATASET_COMPANY_EVIDENCE_UNREADABLE],
                        "detail": company.get("error")})
    elif not scan_current:
        dataset.append({
            "code": DATASET_SCAN_NOT_CURRENT,
            "explanation": EXPLANATIONS[DATASET_SCAN_NOT_CURRENT],
            "detail": scan_verdict.get("reason"),
            "won_not_certified": len(cert["won_not_certified"]),
            # Historical membership, disclosed — never treated as currently won.
            "certified_no_longer_won_deal_ids": sorted(
                cert["certified_no_longer_won"]),
        })

    window_summaries = [_window_summary(w, rows, members_by_window, dataset,
                                        identity_readable)
                        for w in windows]

    if not identity_readable:
        status = STATUS_EVIDENCE_UNAVAILABLE
    elif not scan_current:
        status = (STATUS_SOURCE_STALE
                  if str(scan_verdict.get("reason") or "").startswith(
                      "complete_scan_older_than")
                  else STATUS_SCAN_NOT_CURRENT)
    else:
        status = (STATUS_CERTIFIED_WITH_FINDINGS if rows
                  else STATUS_CERTIFIED_NO_FINDINGS)

    return {
        "available": True,
        "status": status,
        "generated_at": now.isoformat(),
        "source_freshness": freshness,
        "association_scan": scan,
        "identity_evidence_readable": identity_readable,
        # Every deal HubSpot flags won (hs_is_closed_won IS TRUE), any close
        # date — the population the association scan must certify, and the
        # same count the scan verdict carries. NOT the All Time closed-won
        # count: that excludes future-dated deals, and is withheld while the
        # won flag and won stage disagree.
        "won_deal_population": len(won_ids),
        "won_deal_population_basis": "hs_is_closed_won_true_any_close_date",
        "dataset_blockers": dataset,
        "totals": _totals(rows, identity_readable),
        # Families whose rows are complete. Identity evidence unreadable: the
        # identity family is unknown, so an unfiltered list is not complete.
        "complete_families": (list(FAMILIES) if identity_readable else
                              [FAMILY_REVENUE, FAMILY_MEMBERSHIP]),
        "windows": window_summaries,
        "rows": rows,
        "hubspot_portal_id_configured": bool(portal_id),
        "reason_vocabulary": {k: list(v) for k, v in
                              reason_vocabulary().items()},
        "note": ("CRM findings are data-quality facts about HubSpot records, "
                 "not software failures. Affected totals stay withheld until "
                 "the records are corrected in HubSpot and the next scan reads "
                 "them back. This workbench writes nothing to HubSpot."),
        **_read_only_flags(),
    }


def _totals(rows, identity_readable) -> dict:
    by_family: dict = {}
    for f in FAMILIES:
        fam = [r for r in rows if r["exception_family"] == f]
        reasons: dict = {}
        for r in fam:
            reasons[r["reason_code"]] = reasons.get(r["reason_code"], 0) + 1
        by_family[f] = {
            # Identity unreadable: we could not look — None, never 0.
            "rows": (None if f == FAMILY_IDENTITY and not identity_readable
                     else len(fam)),
            "deals": (None if f == FAMILY_IDENTITY and not identity_readable
                      else len({r["deal_id"] for r in fam})),
            "by_reason": dict(sorted(reasons.items())),
        }
    # One family unknown makes the cross-family totals unknown too.
    return {"rows": len(rows) if identity_readable else None,
            "deals_with_any_exception": (len({r["deal_id"] for r in rows})
                                         if identity_readable else None),
            "by_family": by_family}


def _window_summary(w, rows, members_by_window, dataset,
                    identity_readable=True) -> dict:
    """One window: its canonical verdicts, the blockers among its members, and
    whether every withheld customer / revenue verdict is explained."""
    wid = _window_id(w["window"])
    members = members_by_window.get(wid)
    hit = [r for r in rows if wid in r["affected_windows"]]

    def deals(family, reasons=None):
        return sorted({r["deal_id"] for r in hit
                       if r["exception_family"] == family
                       and (reasons is None or r["reason_code"] in reasons)})

    identity_unresolved = deals(FAMILY_IDENTITY, (
        cwt.CUSTOMER_NO_COMPANY, cwt.CUSTOMER_AMBIGUOUS,
        cwt.CUSTOMER_ASSOCIATION_UNAVAILABLE))
    stale = deals(FAMILY_IDENTITY, cwt.STALE_EVIDENCE_REASONS)
    revenue = deals(FAMILY_REVENUE)
    membership = deals(FAMILY_MEMBERSHIP)
    undated = deals(FAMILY_MEMBERSHIP, (cwt.CLOSE_MISSING,))
    conflicts = deals(FAMILY_MEMBERSHIP, (cwt.WON_FLAG_OTHER_STAGE,
                                          cwt.WON_STAGE_FLAG_NOT_TRUE))
    canonical_conflicts = {str(c.get("deal_id")) for c in (
        w.get("membership") or {}).get("won_definition_conflicts") or []}
    codes = {b["code"] for b in dataset}
    pub = {m: _publication(w, m) for m in (METRIC_DEALS, METRIC_CUSTOMERS,
                                           METRIC_REVENUE)}

    def explained(metric) -> dict:
        p = pub[metric]
        if p["status"] == cwt.PUBLISHED:
            blocking = identity_unresolved + stale if metric == \
                METRIC_CUSTOMERS else revenue
            # A published total over a blocker would be a false total.
            return {"explained": not blocking and not (
                metric == METRIC_CUSTOMERS and DATASET_SCAN_NOT_CURRENT
                in codes), "by": None}
        reason = p["reason"]
        if reason == cwt.R_DEALS_WITHHELD:
            reason = pub[METRIC_DEALS]["reason"]
        need = {
            cwt.R_CUSTOMER_IDENTITY: ("rows", bool(deals(FAMILY_IDENTITY, (
                cwt.CUSTOMER_NO_COMPANY, cwt.CUSTOMER_AMBIGUOUS)))),
            cwt.R_CUSTOMER_IDENTITY_UNAVAILABLE: ("rows", bool(deals(
                FAMILY_IDENTITY, (cwt.CUSTOMER_ASSOCIATION_UNAVAILABLE,)))),
            cwt.R_CUSTOMER_IDENTITY_STALE: ("rows", bool(stale)),
            cwt.R_REVENUE_UNPROVEN: ("rows", bool(revenue)),
            # Membership reasons reconcile against the truth service's own
            # count and list, not against the rows' own attachment.
            cwt.R_UNDATED_WON_DEALS: ("rows", 0 < len(undated) == (
                (w.get("membership") or {}).get("undated_won_deals"))),
            cwt.R_WON_DEFINITION_CONFLICT: ("rows", bool(conflicts) and set(
                conflicts) == canonical_conflicts),
            cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT: (
                "dataset", DATASET_SCAN_NOT_CURRENT in codes),
            cwt.R_COMPANY_ASSOCIATIONS_UNREADABLE: (
                "dataset", DATASET_COMPANY_EVIDENCE_UNREADABLE in codes),
        }.get(reason)
        if need is None:
            return {"explained": False, "by": None,
                    "unexplained_reason": reason}
        return {"explained": need[1], "by": need[0], "reason": reason}

    outcomes = w.get("outcomes") or {}
    identity_known = members is not None and identity_readable
    return {
        "window": wid,
        "window_type": w["window"]["window_type"],
        "window_key": w["window"]["window_key"],
        "label": w["window"].get("label"),
        "membership_published": members is not None,
        "undated_included": (w.get("membership") or {}).get(
            "undated_included"),
        "publication": pub,
        "blocker_deals": {
            # Identity evidence unreadable: no identity row could be built, so
            # these counts are unknown — None, never 0.
            "customer_identity_unresolved": (len(identity_unresolved)
                                             if identity_known else None),
            "customer_identity_stale": (len(stale)
                                        if identity_known else None),
            "customer_identity_any": (
                len(deals(FAMILY_IDENTITY)) if identity_known else None),
            "revenue_unproven": (len(revenue)
                                 if members is not None else None),
            "won_deal_membership": len(membership),
        },
        # The truth service's own counts for the same window, for
        # reconciliation (None wherever it withholds them).
        "canonical_counts": {
            "unresolved_customer_identity": outcomes.get(
                "unresolved_customer_identity"),
            "stale_customer_identity": outcomes.get("stale_customer_identity"),
            "deals_without_proven_usd": outcomes.get(
                "deals_without_proven_usd"),
            "undated_won_deals": (w.get("membership") or {}).get(
                "undated_won_deals"),
        },
        "explanation": {METRIC_CUSTOMERS: explained(METRIC_CUSTOMERS),
                        METRIC_REVENUE: explained(METRIC_REVENUE)},
    }


# ═════════════════════════════════════════════════════════════════════════════
# Filtering, pagination, CSV — no classification below this line
# ═════════════════════════════════════════════════════════════════════════════

def _validate(model, *, window, family, reason, limit, offset):
    if family is not None and family not in FAMILIES:
        raise CrmTruthExceptionsQueryError(
            f"Unknown family '{family}'. Valid: {', '.join(FAMILIES)}")
    vocab = reason_vocabulary()
    allowed = {r for rs in vocab.values() for r in rs}
    if reason is not None and reason not in allowed:
        raise CrmTruthExceptionsQueryError(f"Unknown reason '{reason}'.")
    if family is not None and reason is not None \
            and reason not in vocab[family]:
        raise CrmTruthExceptionsQueryError(
            f"Reason '{reason}' does not belong to family '{family}'.")
    if window is not None:
        from services.canonical_customer_revenue_service import (  # noqa: PLC0415
            all_windows,
        )
        valid = [f"{t}/{k}" for t, k in all_windows()]
        if window not in valid:
            raise CrmTruthExceptionsQueryError(
                f"Unknown window '{window}'. Valid: {', '.join(valid)}")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise CrmTruthExceptionsQueryError(
            f"limit must be between 1 and {MAX_LIMIT}")
    if not isinstance(offset, int) or offset < 0:
        raise CrmTruthExceptionsQueryError("offset must be 0 or more")


def filter_rows(model: dict, *, window=None, family=None, reason=None,
                q=None) -> list | None:
    """The filtered population. ``None`` when the model has no rows to filter
    (evidence unavailable) — never an empty list standing in for "none"."""
    rows = model.get("rows")
    if rows is None:
        return None
    complete = set(model.get("complete_families") or FAMILIES)
    vocab = reason_vocabulary()
    scope = ({family} if family is not None else
             {f for f, rs in vocab.items() if reason in rs}
             if reason is not None else set(FAMILIES))
    if not scope <= complete:
        # Part of the asked-for population could not be read: no list at all
        # rather than a partial one that reads as complete.
        return None
    needle = (q or "").strip().lower()
    out = []
    for r in rows:
        if window is not None and window not in r["affected_windows"]:
            continue
        if family is not None and r["exception_family"] != family:
            continue
        if reason is not None and r["reason_code"] != reason:
            continue
        if needle and needle not in str(r["deal_id"]).lower() \
                and needle not in str(r.get("deal_name") or "").lower():
            continue
        out.append(r)
    return out


def query(model: dict, *, window=None, family=None, reason=None, q=None,
          limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    """The API response: the model's summary plus one page of filtered rows."""
    _validate(model, window=window, family=family, reason=reason,
              limit=limit, offset=offset)
    rows = filter_rows(model, window=window, family=family, reason=reason, q=q)
    page = None if rows is None else rows[offset:offset + limit]
    summary = {k: v for k, v in model.items() if k != "rows"}
    return {**summary,
            "filters": {"window": window, "family": family, "reason": reason,
                        "q": q},
            "pagination": {"limit": limit, "offset": offset,
                           "total": None if rows is None else len(rows),
                           "returned": None if page is None else len(page)},
            "rows": page}


def _cell(value) -> str:
    """One CSV cell: lists joined, None empty, and a string that a spreadsheet
    would execute as a formula neutralised with a leading apostrophe."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value)
    text = str(value)
    # Spreadsheets skip leading whitespace before evaluating a formula.
    if text.startswith(_FORMULA_PREFIXES) or text.lstrip(
            " \n\r\t").startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def to_csv(rows: list) -> str:
    """The filtered rows as UTF-8 CSV text, stable column order."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow([_cell(r.get(c)) for c in CSV_COLUMNS])
    return buf.getvalue()


__all__ = [
    "FAMILIES", "FAMILY_IDENTITY", "FAMILY_REVENUE", "FAMILY_MEMBERSHIP",
    "STATUSES", "CSV_COLUMNS", "EXPLANATIONS", "DEFAULT_LIMIT", "MAX_LIMIT",
    "CrmTruthExceptionsQueryError", "reason_vocabulary",
    "build_crm_truth_exceptions", "filter_rows", "query", "to_csv",
]
