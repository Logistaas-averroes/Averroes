"""PR-ADS-161H — attribution feasibility report (read-only, measure first).

Attribution ROAS and CAC are unavailable (docs/48 §4): close-date revenue over
same-window spend is not one acquisition cohort when a B2B deal takes months to
close. A real cohort figure needs three things the data may or may not hold.
This report measures each one, so the decision to build cohort attribution
rests on numbers, not hope:

    python -m scripts.report_attribution_feasibility
    python -m scripts.report_attribution_feasibility --json

1. **Campaign placement.** Can a won Google Ads deal be put on ONE campaign?
   Read from the canonical closed-won truth's own attribution partition
   (``closed_won_truth.attribution_bucket`` with the production campaign
   resolver), never recomputed here.
2. **Acquisition date.** Do we hold the creation time of every contact on the
   deal? Without it a deal belongs to no acquisition month.
3. **Cycle length.** How long from first contact creation to close? That is
   the spend window a cohort figure must wait for before it means anything.

It prints measurements and plain-language readings. It sets no threshold and
publishes nothing: whether the numbers justify building is the owner's call.

Exit 0: measured. Exit 2: unavailable — the ledger could not be read or its
coverage is not proven, and an unproven population is not measured.
No writes, no external API calls.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

EXIT_MEASURED = 0
EXIT_UNAVAILABLE = 2

WINDOWS = (("business", "all_time"), ("business", "last_6_months"),
           ("business", "ytd"))
HORIZON_DAYS = (30, 90, 180, 365)
PERCENTILES = (25, 50, 75, 90)

# Contact evidence classes for one won deal.
C_DATED = "every_contact_creation_time_held"
C_UNDATED = "a_contact_creation_time_missing"
C_NO_CONTACT = "no_associated_contact"
C_UNRESOLVED = "contact_association_unresolved"


def _as_utc(value):
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    return cwt._as_utc(value)  # noqa: SLF001 — the truth's own parser


def contact_evidence(row: dict, contacts: list) -> str:
    """Mirror of ``closed_won_truth.acquisition_state``'s evidence rules,
    without a window: only a SUCCESSFUL lookup that found nobody is
    'no contact'; a failed lookup, or contacts the ledger says exist but none
    stored, is unresolved."""
    rows = [c for c in contacts or [] if c.get("contact_id")]
    if row.get("association_status") == "lookup_failed":
        return C_UNRESOLVED
    if not rows:
        return C_NO_CONTACT if row.get("association_status") == "none" \
            else C_UNRESOLVED
    if any(_as_utc(c.get("contact_created_at")) is None for c in rows):
        return C_UNDATED
    return C_DATED


def percentile(sorted_values: list, p: int):
    """Nearest-rank percentile. None for an empty list — never 0."""
    if not sorted_values:
        return None
    k = max(1, -(-p * len(sorted_values) // 100))   # ceil(p*n/100)
    return sorted_values[k - 1]


def cycle_lengths(won_rows: list, contacts_by_deal: dict, now) -> dict:
    """Days from the deal's FIRST contact creation to its close, for won deals
    with an exact close date and every contact's creation time held.

    A contact created after the close (added to the deal later) gives no
    acquisition-to-close interval; it is counted, never folded in as 0 or a
    negative cycle."""
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    from analysis.revenue_scope import is_google_ads_attributed  # noqa: PLC0415

    def segment(rows):
        lags, after_close, not_measurable = [], 0, 0
        for r in rows:
            cid = str(r.get("deal_id"))
            contacts = contacts_by_deal.get(cid) or []
            if cwt.close_date_state(r, now) != cwt.CLOSE_EXACT \
                    or contact_evidence(r, contacts) != C_DATED:
                not_measurable += 1
                continue
            first = min(_as_utc(c["contact_created_at"]) for c in contacts
                        if c.get("contact_id"))
            close = _as_utc(r.get("deal_close_date"))
            if first > close:
                after_close += 1
                continue
            lags.append((close - first).total_seconds() / 86400.0)
        lags.sort()
        n = len(lags)
        return {
            "deals": len(rows),
            "measured": n,
            "not_measurable": not_measurable,
            "first_contact_created_after_close": after_close,
            "percentile_days": {f"p{p}": (None if n == 0 else
                                          round(percentile(lags, p), 1))
                                for p in PERCENTILES},
            "closed_within_days": {
                str(h): (None if n == 0 else
                         {"deals": sum(1 for x in lags if x <= h),
                          "share": round(sum(1 for x in lags if x <= h) / n,
                                         3)})
                for h in HORIZON_DAYS},
        }

    google_ads = [r for r in won_rows if is_google_ads_attributed(r)
                  and r.get("attribution_status") != "ambiguous"]
    return {"all_won": segment(won_rows), "google_ads": segment(google_ads)}


def _window_summary(w: dict) -> dict:
    """One truth window, as the truth reports it. Redacted values stay None."""
    attribution = w.get("attribution") or {}
    partition = attribution.get("partition") or {}
    acq = w.get("acquisition_cohort") or {}
    meta = w.get("window") or {}
    deal_pub = (w.get("publication") or {}).get("closed_won_deals") or {}
    return {
        "window": meta.get("window_key"),
        "label": meta.get("label"),
        "closed_won_deals": (w.get("outcomes") or {}).get("closed_won_deals"),
        "closed_won_deals_status": deal_pub.get("status"),
        "closed_won_deals_reason": deal_pub.get("reason"),
        "google_ads_source_deals": (attribution.get("google_ads_source")
                                    or {}).get("deals"),
        "partition": {b: {"deals": (t or {}).get("deals"),
                          "reasons": (t or {}).get("reasons")}
                      for b, t in partition.items()},
        "acquisition_cohort": {k: acq.get(k) for k in (
            "status", "reason", "closed_won_deals", "unresolved_membership",
            "ambiguous_membership")},
    }


def measure(universe: dict, truth: dict, now) -> dict:
    """Pure: the report from one universe snapshot and the truth computed
    from that same snapshot."""
    from analysis import closed_won_truth as cwt  # noqa: PLC0415

    base = {"report": "attribution_feasibility", "generated_at": now.isoformat(),
            "external_writes_performed": False,
            "database_writes_performed": False,
            "hubspot_calls_performed": False,
            "google_ads_calls_performed": False}
    if not universe.get("available"):
        return {**base, "available": False, "exit_code": EXIT_UNAVAILABLE,
                "detail": f"the canonical deal ledger could not be read "
                          f"({universe.get('reason')})"}
    findings = universe.get("coverage_findings")
    if findings is None or findings:
        # Absent is not "no findings" — an unproven population is not measured.
        return {**base, "available": False, "exit_code": EXIT_UNAVAILABLE,
                "detail": "deal-ledger coverage is not proven",
                "coverage_findings": findings}

    if truth.get("available") is not True:
        return {**base, "available": False, "exit_code": EXIT_UNAVAILABLE,
                "detail": "the canonical closed-won truth is unavailable"}
    # The truth applies gates the raw universe does not carry (e.g. the
    # deal-ledger staleness limit). If it cannot vouch for the population
    # in any window, the population is not measured here either.
    blocked = [w for w in truth.get("windows") or []
               if ((w.get("publication") or {}).get("closed_won_deals")
                   or {}).get("status") == "unavailable"]
    if blocked:
        reason = ((blocked[0].get("publication") or {}).get(
            "closed_won_deals") or {}).get("reason")
        return {**base, "available": False, "exit_code": EXIT_UNAVAILABLE,
                "detail": f"the canonical closed-won truth is unavailable "
                          f"({reason})"}

    won = list(universe.get("won_rows") or [])
    contacts_by_deal: dict = {}
    for c in universe.get("acquisition_contacts") or []:
        contacts_by_deal.setdefault(str(c.get("deal_id")), []).append(c)
    evidence: dict = {}
    for r in won:
        e = contact_evidence(r, contacts_by_deal.get(str(r.get("deal_id"))))
        evidence[e] = evidence.get(e, 0) + 1
    return {**base, "available": True, "exit_code": EXIT_MEASURED,
            "campaign_resolver": truth.get("campaign_resolver"),
            "won_deals": len(won),
            "contact_evidence": {k: evidence.get(k, 0) for k in (
                C_DATED, C_UNDATED, C_NO_CONTACT, C_UNRESOLVED)},
            "cycle_length": cycle_lengths(won, contacts_by_deal, now),
            "windows": [_window_summary(w)
                        for w in truth.get("windows") or []],
            "rule_version": cwt.CLOSED_WON_TRUTH_RULE_VERSION}


def run(now: datetime | None = None) -> dict:
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    from services import canonical_customer_revenue_service as ccr  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415

    now = now or datetime.now(tz=timezone.utc)
    universe = crs.load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID)
    # The truth reads the SAME universe: one snapshot for every number below.
    truth = ccr.get_closed_won_truth(list(WINDOWS), now=now, universe=universe)
    return measure(universe, truth, now)


def _n(value) -> str:
    return "withheld/unknown" if value is None else str(value)


def _render(report: dict) -> None:
    print("=" * 78)
    print("  ATTRIBUTION FEASIBILITY (read-only measurement — publishes nothing)")
    print("=" * 78)
    if not report.get("available"):
        print(f"  UNAVAILABLE — {report.get('detail')}")
        for f in report.get("coverage_findings") or []:
            print(f"    - {f.get('code')}: {f.get('message')}")
        return
    won = report["won_deals"]
    print(f"  won deals (hs_is_closed_won): {won}")
    print(f"  campaign resolver: {report.get('campaign_resolver')}")

    print("\n  1. CAMPAIGN PLACEMENT (canonical attribution partition)")
    for w in report["windows"]:
        status = w.get("closed_won_deals_status")
        note = "" if status == "published" else \
            f"  [{status}: {w.get('closed_won_deals_reason')}]"
        print(f"    {str(w['label'] or w['window']):<22}"
              f"won {_n(w['closed_won_deals'])}, "
              f"Google Ads source {_n(w['google_ads_source_deals'])}{note}")
        for bucket, t in w["partition"].items():
            reasons = t.get("reasons") or {}
            print(f"      {bucket:<30}{_n(t.get('deals')):>6}  "
                  f"{reasons if reasons else ''}")

    print("\n  2. ACQUISITION DATE (contact creation time held?)")
    for k, v in report["contact_evidence"].items():
        print(f"    {k:<40}{v:>6}")
    for w in report["windows"]:
        a = w["acquisition_cohort"]
        print(f"    cohort {str(w['label'] or w['window']):<22}{a['status']}"
              f"{' (' + a['reason'] + ')' if a.get('reason') else ''}; "
              f"unresolved {_n(a['unresolved_membership'])}, "
              f"spanning windows {_n(a['ambiguous_membership'])}")

    print("\n  3. CYCLE LENGTH (first contact created -> deal closed, days)")
    for seg, c in report["cycle_length"].items():
        pct = ", ".join(f"{k} {_n(v)}" for k, v in
                        c["percentile_days"].items())
        print(f"    {seg:<12}measured {c['measured']} of {c['deals']} "
              f"(not measurable {c['not_measurable']}, contact created "
              f"after close {c['first_contact_created_after_close']})")
        print(f"    {'':<12}{pct}")
        within = ", ".join(
            f"<={h}d {_n(None if v is None else round(100 * v['share']))}%"
            for h, v in c["closed_within_days"].items())
        print(f"    {'':<12}{within}")

    print("\n  HOW TO READ THIS")
    print("    * Cohort attribution can only cover deals in "
          "'campaign_attributable'.\n      Every 'google_ads_unplaced' deal is "
          "a Google Ads deal no campaign can claim.")
    print("    * A deal whose contact creation time is missing belongs to no "
          "acquisition\n      month; those deals keep a cohort figure "
          "withheld.")
    print("    * The p75/p90 cycle is how long a month's spend must wait "
          "before its\n      cohort revenue is close to complete. Before "
          "that, a cohort figure is a\n      lower bound, never the figure.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only measurement: could cohort attribution work?")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    args = parser.parse_args(argv)

    from db.connection import ensure_database_ready  # noqa: PLC0415

    ready, detail = ensure_database_ready()
    if not ready:
        report = {"report": "attribution_feasibility", "available": False,
                  "exit_code": EXIT_UNAVAILABLE,
                  "detail": f"database not ready: {detail}"}
    else:
        try:
            report = run()
        except Exception as exc:  # noqa: BLE001
            report = {"report": "attribution_feasibility", "available": False,
                      "exit_code": EXIT_UNAVAILABLE,
                      "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
