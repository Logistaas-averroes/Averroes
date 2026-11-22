"""
services/country_evidence_service.py

PR-ADS-165A — the Google Ads → Countries page (`#/geo`, ``GET /api/geo``): one
row per country, Google Ads spend and HubSpot lead figures already joined.

Why this module exists
----------------------
The page read the legacy ``geo`` snapshot: ``SUM(spend_usd) WHERE run_date >=
NOW() - N days``. The weekly job rewrote that table every Monday. With the
weekly job retired the monthly job is its only writer, so twelve days into a
month the 7-day window held no rows at all — and the page rendered every
country's spend as ``$0`` and "Total Geo Spend $0" — and a 30-day window held
the days up to the 1st and presented their sum as the 30-day figure. A partial
is not the thing itself, and unknown is not zero.

The daily incremental sync already writes per-country spend for every day into
``google_ads_geo_daily_spend`` and records what it fetched in the coverage
ledger ``google_ads_geo_coverage``. Spend here comes from those, and nothing
else.

The gate this page applies — exactly
------------------------------------
Every day of the window must be covered by a ``verified`` chunk of the geo
coverage ledger (for the configured account) that was FETCHED AFTER THAT DAY
ENDED, and the rows must be readable.

The second half matters. The daily sync fetches a range ending today and
records the chunk ``verified`` at about 06:00 UTC, so the ledger says
"verified" for a day that had six hours of spend in it. Judged on chunk status
alone, yesterday reads as covered from account-midnight until this morning's
sync re-reads it — holding only what yesterday morning's sync saw — and all
day if the sync fails. So a day counts only if a verified chunk containing it
has ``fetched_at`` at or after the end of that account-local day. Until then
the window is "not covered", reason ``spend_not_reread_since_day_ended`` — or
``spend_reread_failed`` when a sync covering those days has run and failed.

"Account-local" is the zone Google reported with the account's spend, read
where the geo sync reads it (``repo.fetch_account_time_zone``) and not from a
constant. With no zone on record nothing can be said about when a day ended:
the window is ``unavailable`` / ``account_time_zone_unknown``.

Consequence, stated on the page: spend is available from the morning sync
onward, and reads "Unavailable" between account-midnight and that sync.

The rule is the shared ``google_ads_spend_service.analyze_settled_coverage``
(PR-ADS-167A moved it there from this module), which Country Revenue & Spend
and the geo reconciliation panel apply too. The gap codes are the geo
service's own.

It is NOT Country Revenue & Spend's gate. That page divides revenue by this
spend, so it ALSO requires complete campaign-spend coverage, FX coverage and a
geo↔campaign reconciliation within tolerance. This page divides by nothing and
requires geo coverage only, so the two can differ: a window whose geo total
does not reconcile to campaign spend is published here and blocked there. Both
apply the same fetched-after-the-day-ended rule (PR-ADS-167A).

The window
----------
N account-local days ending YESTERDAY in the Google Ads account time zone, not
today: today is a day still in progress. Yesterday is "the last complete day"
only once it has been re-read after it ended — which is what the gate above
checks; the window bound alone does not make it complete.
(Country Revenue & Spend's business windows end today; since PR-ADS-167A such a
window is ``window_includes_day_in_progress`` there, never complete.)

``all_time`` has no requested lower bound. The coverage analysis then starts at
the earliest VERIFIED chunk, and the row read is bounded to that same certified
range: a row dated before anything the ledger certifies is not published as
"covered".

The join
--------
Spend and leads are joined HERE, on an ISO 3166-1 country code, never by
display text in the browser:

* a spend row is keyed by the ISO code of its Google CRITERION and shown under
  Google's country name. The code is the one any stored row of that criterion
  carries — over the whole table, not only the window — because the sync
  stores a row with no code when its country lookup fails that day, and the
  criterion id is Google's own id for the location. Exactly one code on
  record resolves; none, or two that disagree, do not. A criterion with NO
  code on record that the owner has labelled
  (``config/geo_owner_labels.yaml``, PR-ADS-170A — production's ``2192`` Cuba
  and ``2364`` Iran) is keyed by the label's code, and its row says
  ``country_provenance: "owner_labelled"``; a stored Google code always wins
  over a label. Unresolved spend is
  one "(unresolved location)" row and stays in the total. The code is not
  required to be in a supported-markets list: production spend falls in 162
  distinct codes;
* a lead country resolves by its text against the COMPLETE ISO country table
  (``country_identity.resolve_iso_country_text`` — every ISO name plus the
  legacy and inverted names HubSpot stores), so ``uk`` and ``United Kingdom``
  are ONE row with the leads summed and ``libyan arab jamahiriya`` joins
  Libya;
* a lead country that names no single country ("Congo", "California", a typo)
  is its own row and its spend is ``unavailable`` / ``country_not_matched`` —
  it may be a spelling of a country that did spend, so it is never ``0``;
* blank and placeholder values ("Other", "N/A", "(unknown)") are ONE row, "No
  country recorded". It is not an unmatched country and is not counted as one;
* a ``0`` is published only for a country that RESOLVED, in a COVERED window,
  with no spend row, when no spend in the window can be its own. Spend whose
  criterion no stored row names can only be a country no criterion carries:
  Google keys one country under one criterion. So (PR-ADS-167A) the zero stands
  when every unresolved criterion in the window is code-less everywhere AND
  the country's code is carried on record by exactly one criterion that
  carries nothing else (``repo.fetch_geo_code_carriers``; an owner-labelled
  criterion is a carrier of its code, not unresolved spend). A country never seen
  in geo spend, or any spend-less country while a criterion in the window
  carries two codes, is ``unavailable`` / ``spend_location_unresolved`` while
  there is unresolved spend. A country WITH a spend row is unaffected.

The market registry ``country_identity.SUPPORTED_COUNTRIES`` (96 codes), which
the revenue pages group on, is not used here and is not changed.

What the canonical table does not hold
--------------------------------------
``google_ads_geo_daily_spend`` stores cost only. Clicks, impressions,
conversions and the campaign NAME were columns of the legacy snapshot. They are
not filled in from it. ``dropped_metrics`` names them.

Read-only. No Google Ads or HubSpot call.
"""

from __future__ import annotations

import contextlib

import logging
from datetime import date, datetime, timedelta

from datetime import timezone

from analysis import country_identity
from analysis.account_time import account_today
from services.dataset_keys import CANONICAL_GEO_DATASET, CANONICAL_GEO_SOURCE

log = logging.getLogger(__name__)

#: Coverage verdicts for one requested window.
COVERAGE_COVERED = "covered"
COVERAGE_NOT_COVERED = "not_covered"
COVERAGE_UNAVAILABLE = "unavailable"

#: Why a figure is not published. The geo reasons are the geo service's own
#: gap codes, so this page and Country Revenue & Spend name one condition one
#: way.
R_ACCOUNT_NOT_CONFIGURED = "google_ads_customer_id_not_configured"
R_FX_RATE_MISSING = "fx_rate_missing"
R_COUNTRY_NOT_MATCHED = "country_not_matched"
R_NO_COUNTRY_RECORDED = "no_country_recorded"
# The settle reasons are the shared rule's own codes (PR-ADS-167A), so this page
# and Country Revenue & Spend name one condition one way.
from services.google_ads_spend_service import (  # noqa: E402
    COVERAGE_GAP_DAY_NOT_REREAD as R_DAY_NOT_REREAD,
    COVERAGE_GAP_REREAD_FAILED as R_REREAD_FAILED,
    COVERAGE_GAP_TZ_UNKNOWN as R_ACCOUNT_TZ_UNKNOWN,
)
#: Every day has a verified chunk, but the newest day(s) were last fetched
#: while still in progress (``R_DAY_NOT_REREAD``) — or, ``R_REREAD_FAILED``, a
#: sync that would have re-read them FAILED, so waiting for "the morning sync"
#: is the wrong advice. ``R_ACCOUNT_TZ_UNKNOWN``: the account's time zone is
#: not on record, so "the day ended" has no meaning.
#: A country with no spend row, in a window that holds spend Google reported
#: against a location no stored row names: that spend may be this country's.
R_SPEND_LOCATION_UNRESOLVED = "spend_location_unresolved"
R_LEADS_UNREADABLE = "lead_summary_unreadable"

#: Columns the legacy `geo` snapshot carried and the canonical table does not.
DROPPED_METRICS = ("clicks", "impressions", "conversions", "campaign_name")

#: PR-ADS-170A: a country a row carries because the owner labelled the Google
#: criterion (``config/geo_owner_labels.yaml``), not because Google named it.
from analysis.geo_owner_labels import (  # noqa: E402
    OWNER_LABEL_NOTE, PROVENANCE_OWNER_LABELLED as OWNER_LABELLED,
)

#: The label for geo spend Google Ads reports against no resolvable country.
UNRESOLVED_COUNTRY_LABEL = "(unresolved location)"
#: The label for leads with no country at all (blank, "Other", "N/A", …).
NO_COUNTRY_LABEL = "No country recorded"

#: Lead-summary count fields, summed when several spellings are one country.
LEAD_COUNT_FIELDS = ("total_leads", "confirmed_sqls", "in_progress",
                     "confirmed_junk", "wrong_fit", "unknown")

WINDOW_ENDS_ON = "yesterday_account_local"


def account_zone():
    """``(name, ZoneInfo)`` of the Google Ads account, or ``(None, None)``.

    The SAME source the geo sync reads (``repo.fetch_account_time_zone``: the
    zone Google reported with the account's spend) — not a constant. Unknown is
    unknown: the caller fails closed rather than assume a zone, because the
    settle rule below turns on where the account's midnight falls.
    """
    from db import revenue_repository as repo  # noqa: PLC0415
    try:
        name = repo.fetch_account_time_zone()
        if not name:
            return None, None
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        return str(name), ZoneInfo(str(name))
    except Exception as exc:  # noqa: BLE001 — unreadable / not a zone
        log.error("[country-evidence] account time zone unavailable: %s", exc)
        return None, None


def window_bounds(days: int | None, now: datetime | None = None, tz=None) -> tuple:
    """``(start | None, end)`` of the page's spend window, account-local dates.

    INCLUSIVE of exactly N calendar dates ending YESTERDAY in the account
    time zone ``tz``. ``days is None`` (all_time) has no lower bound. Ending
    yesterday does not by itself make yesterday complete — see
    :func:`settled_through`. With no ``tz`` the bounds are only a label for a
    window that is not published (see :func:`build_country_evidence`).
    """
    if tz is None:
        today = account_today(now)
    else:
        base = now or datetime.now(tz=timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        today = base.astimezone(tz).date()
    end = today - timedelta(days=1)
    start = None if days is None else end - timedelta(days=int(days) - 1)
    return start, end


# PR-ADS-167A: the settle rule lives in ONE place,
# ``services.google_ads_spend_service`` (``settled_through``, ``settled_chunks``,
# ``analyze_settled_coverage``), so Country Revenue & Spend and the geo
# reconciliation panel apply exactly the rule this page applies. Re-exported
# under the names this module has always published.
from services.google_ads_spend_service import (  # noqa: E402
    settled_chunks, settled_through,
)


def _covered_span(start: date | None, end: date, analysis: dict) -> tuple:
    """First and last settled day inside the window, or (None, None)."""
    first = last = None
    for chunk in analysis.get("verified_chunks") or []:
        try:
            cs = date.fromisoformat(str(chunk.get("chunk_start")))
            ce = date.fromisoformat(str(chunk.get("chunk_end")))
        except (TypeError, ValueError):
            continue
        if start is not None:
            cs = max(cs, start)
        ce = min(ce, end)
        if cs > ce:
            continue
        first = cs if first is None else min(first, cs)
        last = ce if last is None else max(last, ce)
    return first, last


def _coverage_verdict(start: date | None, end: date, customer_id, tz,
                      now: datetime | None = None) -> dict:
    """The coverage verdict for ``[start, end]``, from the geo coverage ledger."""
    from db import revenue_repository as repo  # noqa: PLC0415
    from services import google_ads_geo_sync_service as geo  # noqa: PLC0415
    from services.google_ads_spend_service import analyze_settled_coverage  # noqa: PLC0415

    base = {
        "requested_from": start.isoformat() if start else None,
        "requested_through": end.isoformat(),
        "covered_from": None,
        "covered_through": None,
        "missing_days": None,
        "missing_ranges": [],
        "failed_chunks": [],
    }
    if not customer_id:
        return {**base, "status": COVERAGE_UNAVAILABLE,
                "reason": R_ACCOUNT_NOT_CONFIGURED}
    if tz is None:
        return {**base, "status": COVERAGE_UNAVAILABLE,
                "reason": R_ACCOUNT_TZ_UNKNOWN}
    try:
        ledger = repo.fetch_geo_coverage(customer_id, start, end)
        if not ledger.get("available"):
            return {**base, "status": COVERAGE_UNAVAILABLE,
                    "reason": geo.GEO_GAP_GEO_COVERAGE_UNREADABLE}
        # Complete days only: each verified chunk counts for the days that had
        # ended when it was fetched — the shared rule (PR-ADS-167A).
        analysis = analyze_settled_coverage(start, end, ledger.get("chunks") or [], tz,
                                            now=now)
    except Exception as exc:  # noqa: BLE001 — unknown, never "covered"
        log.error("[country-evidence] geo coverage read failed: %s", exc)
        return {**base, "status": COVERAGE_UNAVAILABLE,
                "reason": geo.GEO_GAP_GEO_COVERAGE_UNREADABLE}

    first, last = _covered_span(start, end, analysis)
    verdict = {
        **base,
        "covered_from": first.isoformat() if first else None,
        "covered_through": last.isoformat() if last else None,
        "missing_days": analysis.get("missing_days"),
        "missing_ranges": analysis.get("missing_ranges") or [],
        "failed_chunks": analysis.get("failed_chunks") or [],
    }
    if analysis.get("complete") and first is not None:
        return {**verdict, "status": COVERAGE_COVERED, "reason": None}
    if analysis.get("unsettled_reason"):
        # Every day has a verified chunk; the newest are part-days (or a sync
        # that should have re-read them failed). The shared rule names which.
        reason = analysis["unsettled_reason"]
    elif analysis.get("failed_chunks") and not analysis.get("missing_days"):
        reason = geo.GEO_GAP_GEO_CHUNKS_FAILED
    else:
        reason = geo.GEO_GAP_GEO_COVERAGE_INCOMPLETE
    return {**verdict, "status": COVERAGE_NOT_COVERED, "reason": reason}


def _new_row(key: str, label: str, code, resolved: bool) -> dict:
    return {
        "country_key": key, "country": label, "country_code": code,
        "country_resolved": resolved,
        "_spend_parts": 0, "_spend_usd": 0.0, "_spend_published": True,
        "_lead_parts": [], "_names": set(), "_no_country": False,
        "_owner_criteria": set(),
    }


def identify_spend_rows(spend_rows: list, identities: dict) -> tuple:
    """Give each spend row the country its CRITERION carries anywhere on record.

    The window read groups by Google's criterion id. A row's own code can be
    missing because the lookup failed on the day it was written, so the
    identity is the one any stored row of that same criterion carries
    (``repo.fetch_geo_criterion_identities``). That is stored evidence about
    the same Google id, not a guess.

    * exactly one code on record -> that code, and the name stored with it;
    * no code on any row         -> the owner's label when the criterion is
      listed in ``config/geo_owner_labels.yaml`` (PR-ADS-170A), marked
      ``identity_provenance == "owner_labelled"``; otherwise unresolved;
    * two different codes        -> unresolved. The stored data contradicts
      itself and nothing here picks a side — an owner label does not either.

    The rule is ``db.geo_identity_sql.criterion_identity``; this function
    applies it and decides nothing itself. Each returned row also carries
    ``owner_label_mismatch`` (a stored Google code that differs from the
    criterion's label: the stored code is used).

    Returns ``(rows, unresolved_criteria, conflicting_criteria)`` — the two
    lists name the criterion ids, never a country.
    """
    from db.geo_identity_sql import criterion_identity  # noqa: PLC0415

    out, unresolved, conflicting = [], [], []
    for r in spend_rows:
        criterion = str(r.get("country_criterion_id") or "").strip()
        ident = identities.get(criterion) or {}
        resolved = criterion_identity(criterion, ident.get("codes"),
                                      ident.get("name"))
        extra = {"identity_provenance": resolved["provenance"],
                 "owner_label": resolved["owner_label"],
                 "owner_label_mismatch": resolved["owner_label_mismatch"]}
        if criterion and resolved["code"]:
            out.append({**r, "country_code": resolved["code"],
                        "country_name": resolved["name"], **extra})
            continue
        if len(resolved["stored_codes"]) > 1:
            conflicting.append(criterion)
        unresolved.append(criterion)
        out.append({**r, "country_code": None, "country_name": None,
                    **extra, "identity_provenance": None})
    return out, sorted(set(unresolved)), sorted(set(conflicting))


def _owner_label_disclosure(spend_rows: list) -> dict:
    """``{criteria, mismatches}`` for the identified spend rows (PR-ADS-170A)."""
    criteria, mismatches = {}, {}
    for r in spend_rows:
        cid = str(r.get("country_criterion_id") or "").strip()
        label = r.get("owner_label") or {}
        if r.get("identity_provenance") == OWNER_LABELLED and label:
            criteria[cid] = {"criterion_id": cid, "country_code": label.get("code"),
                             "country": label.get("name"),
                             "approved": label.get("approved"), "by": label.get("by")}
        if r.get("owner_label_mismatch"):
            mismatches[cid] = dict(r["owner_label_mismatch"])
    return {"criteria": [criteria[k] for k in sorted(criteria)],
            "mismatches": [mismatches[k] for k in sorted(mismatches)]}


def _join(spend_rows: list, lead_rows: list) -> dict:
    """Spend rows and lead rows keyed on an ISO 3166-1 country code."""
    rows: dict = {}
    for r in spend_rows:
        code = str(r.get("country_code") or "").strip().upper()
        if code:
            # Google's own code is the identity — whether or not any registry
            # of ours lists it — and Google's name is the label.
            key = f"code:{code}"
            row = rows.setdefault(key, _new_row(key, None, code, True))
            row["_names"].add(str(r.get("country_name") or "").strip())
            if r.get("identity_provenance") == OWNER_LABELLED:
                # PR-ADS-170A: this spend is under the country on the owner's
                # word, not Google's. The row says so.
                row["_owner_criteria"].add(
                    str(r.get("country_criterion_id") or "").strip())
        else:
            # Spend whose criterion carries no single country code on any
            # stored row. Its own row: it is real spend and stays in the total.
            row = rows.setdefault("spend:unresolved", _new_row(
                "spend:unresolved", UNRESOLVED_COUNTRY_LABEL, None, False))
        usd = r.get("spend_usd")
        published = bool(r.get("fx_complete")) and usd is not None
        row["_spend_parts"] += 1
        row["_spend_published"] = row["_spend_published"] and published
        if published:
            row["_spend_usd"] += float(usd)

    for r in lead_rows:
        raw = str(r.get("country") or "").strip()
        if country_identity.is_no_country_placeholder(raw):
            # Not a country that failed to match: no country at all. One row.
            row = rows.setdefault("lead:none", _new_row(
                "lead:none", NO_COUNTRY_LABEL, None, False))
            row["_no_country"] = True
        else:
            code = country_identity.resolve_iso_country_text(raw)
            if code:
                key = f"code:{code}"
                row = rows.setdefault(key, _new_row(key, None, code, True))
            else:
                # Names no single country. It might be a spelling of one that
                # did spend, so it is never joined by guess and never given a
                # zero.
                key = f"lead:{country_identity.normalize_country_text(raw) or raw.casefold()}"
                row = rows.setdefault(key, _new_row(key, raw, None, False))
        row["_lead_parts"].append({**r, "_raw": raw or NO_COUNTRY_LABEL})

    for row in rows.values():
        names = {n for n in row.pop("_names") if n}
        if row["country"] is None:
            # Google's name where it gave exactly one; the ISO name otherwise
            # (a lead-only country, or a code Google named two ways).
            row["country"] = (next(iter(names)) if len(names) == 1 else
                              country_identity.iso_country_label(row["country_code"])
                              or (sorted(names)[0] if names else row["country_code"]))
    return rows


def _lead_figures(parts: list) -> dict:
    """One country's lead figures from every spelling that resolved to it."""
    if not parts:
        return {**{f: 0 for f in LEAD_COUNT_FIELDS}, "verdicted_leads": 0,
                "junk_rate_pct": None, "top_campaign": None,
                "top_keyword": None, "top_campaign_several": False,
                "top_keyword_several": False, "last_run_date": None,
                "lead_country_spellings": []}
    out = {f: sum(int(p.get(f) or 0) for p in parts) for f in LEAD_COUNT_FIELDS}
    verdicted = (out["confirmed_sqls"] + out["in_progress"]
                 + out["confirmed_junk"] + out["wrong_fit"])
    dates = [str(p["last_run_date"]) for p in parts if p.get("last_run_date")]

    def agreed(field):
        # The summary carries each spelling's most common campaign / keyword,
        # not the counts behind it, so the mode ACROSS spellings cannot be
        # recomputed. It is known only when every spelling names the same one
        # (a value that leads in each part leads in the whole). Otherwise it
        # is not shown — never the largest spelling's, which can be wrong.
        values = {p.get(field) for p in parts if p.get(field)}
        return next(iter(values)) if len(values) == 1 else None

    def several(field):
        # Two spellings naming different ones: said as "Several", not blank.
        return len({p.get(field) for p in parts if p.get(field)}) > 1

    out.update({
        "verdicted_leads": verdicted,
        "junk_rate_pct": (round(out["confirmed_junk"] / verdicted * 100, 2)
                          if verdicted > 0 else None),
        "top_campaign": agreed("top_campaign"),
        "top_keyword": agreed("top_keyword"),
        "top_campaign_several": several("top_campaign"),
        "top_keyword_several": several("top_keyword"),
        "last_run_date": max(dates) if dates else None,
        "lead_country_spellings": sorted({p["_raw"] for p in parts}),
    })
    return out


def build_country_evidence(*, days: int | None, window_key: str,
                           lead_rows: list | None = None,
                           leads_available: bool = True,
                           now: datetime | None = None) -> dict:
    """The Countries page payload: one row per country, spend and leads joined.

    ``lead_rows`` is the lead country summary for the same evidence window
    (``/api/leads/country-summary`` rows, one per raw country text);
    ``leads_available=False`` means it could not be read.

    Returns::

        {days, window, window_range: {from, through, ends_on},
         spend_source, dataset, reporting_currency,
         coverage: {status, covered_from, covered_through, reason,
                    requested_from, requested_through, missing_days,
                    missing_ranges, failed_chunks},
         spend_status: "published" | "unavailable",
         leads_status: "published" | "unavailable",
         owner_labelled_geo: {criteria, mismatches, note},
         rows: [{country_key, country, country_code, country_resolved,
                 spend_usd, spend_status, spend_reason,
                 country_provenance, owner_labelled_criteria,
                 <lead figures>}],
         total_spend_usd, countries_active, unmatched_lead_countries,
         dropped_metrics}

    A spend figure appears ONLY when ``coverage.status == "covered"``. Never
    raises for a data gap.
    """
    from db import revenue_repository as repo  # noqa: PLC0415
    from services import google_ads_geo_sync_service as geo  # noqa: PLC0415

    tz_name, tz = account_zone()
    start, end = window_bounds(days, now, tz)
    customer_id = geo.configured_customer_id()
    coverage = _coverage_verdict(start, end, customer_id, tz, now)
    covered = coverage["status"] == COVERAGE_COVERED

    # PR-ADS-167A: the geo reads below share one snapshot when covered.
    with contextlib.ExitStack() as geo_reads:
        spend_rows: list = []
        spend: dict = {}
        owner_geo = {"criteria": [], "mismatches": []}
        unresolved_criteria: list = []
        conflicting_criteria: list = []
        if covered:
            # The row read is bounded to the CERTIFIED range. For a fixed window
            # that is the requested range; for all_time it is the earliest
            # verified day — never "no lower bound".
            row_start = start or date.fromisoformat(coverage["covered_from"])
            # PR-ADS-167A: spend rows, identities and carriers from ONE snapshot
            # (``repo.geo_read_snapshot``), so a sync committing between them
            # cannot turn an "Unavailable" into a false $0.
            geo_reads.enter_context(repo.geo_read_snapshot())
            spend = repo.fetch_geo_daily_spend_by_country(row_start, end, customer_id)
            if not spend.get("available"):
                # The ledger says the days were fetched; the rows could not be
                # read back. That is unknown — not covered-and-empty.
                coverage = {**coverage, "status": COVERAGE_UNAVAILABLE,
                            "reason": geo.GEO_GAP_GEO_ROWS_UNREADABLE}
                covered = False
            else:
                spend_rows = spend.get("rows") or []
                identities = repo.fetch_geo_criterion_identities(
                    [r.get("country_criterion_id") for r in spend_rows])
                if not identities.get("available"):
                    # Which country each criterion is could not be read. Naming
                    # the rows from their own (possibly blank) codes instead would
                    # be the defect this read exists to close.
                    coverage = {**coverage, "status": COVERAGE_UNAVAILABLE,
                                "reason": geo.GEO_GAP_GEO_ROWS_UNREADABLE}
                    covered, spend_rows = False, []
                else:
                    spend_rows, unresolved_criteria, conflicting_criteria = (
                        identify_spend_rows(spend_rows, identities["identities"]))
                    owner_geo = _owner_label_disclosure(spend_rows)

        joined = _join(spend_rows, list(lead_rows or []) if leads_available else [])
        # Spend in this window that belongs to no named country. While there is
        # any, a country with NO spend row is not a measured zero: that spend may
        # be its own. (A country WITH a row is complete — a country's spend lives
        # under one criterion, and that criterion resolved.)
        loose = joined.get("spend:unresolved")
        unresolved_usd = None
        unresolved_spend_present = False
        if loose is not None and loose["_spend_parts"]:
            if loose["_spend_published"]:
                unresolved_usd = round(loose["_spend_usd"], 2)
                unresolved_spend_present = loose["_spend_usd"] != 0
            else:
                unresolved_spend_present = True      # amount unknown: not "none"
        # PR-ADS-167A — which spend-less countries that loose spend CANNOT be.
        # Google keys one country under one criterion. A location no stored row
        # names is therefore not a country whose code another criterion carries on
        # record. So a resolved country with no spend row is still a measured $0
        # while unresolved spend is in the window, if and only if:
        #   (a) the window is covered (checked above: `covered`);
        #   (b) every unresolved criterion in the window is code-less EVERYWHERE —
        #       none is a criterion whose rows disagree (that one IS some country);
        #   (c) the country's code is carried on record by exactly one criterion,
        #       and that criterion carries no other code. A country never seen in
        #       geo spend at all (leads, no Google spend ever) could be the loose
        #       location, so it stays unavailable while there is loose spend.
        # The evidence read failing is "carried by nothing": fail closed.
        carried: set = set()
        # PR-ADS-170A: a zero that stands because the country's ONE carrier is
        # an owner-labelled criterion stands on the owner's word; say so.
        carried_by_owner: dict = {}
        if covered and unresolved_spend_present and not conflicting_criteria:
            candidates = sorted({
                row["country_code"] for row in joined.values()
                if row["country_resolved"] and row["country_code"]
                and not row["_spend_parts"]})
            if candidates:
                found = repo.fetch_geo_code_carriers(candidates)
                if found.get("available"):
                    for code, holders in (found.get("carriers") or {}).items():
                        if len(holders) == 1 and holders[0].get("codes") == [code]:
                            carried.add(code)
                            if holders[0].get("provenance") == OWNER_LABELLED:
                                carried_by_owner[code] = holders[0].get("criterion_id")
    withheld_zero: list = []
    rows = []
    for row in joined.values():
        parts, usd = row.pop("_spend_parts"), row.pop("_spend_usd")
        fx_ok = row.pop("_spend_published")
        no_country = row.pop("_no_country")
        owner_criteria = set(row.pop("_owner_criteria"))
        leads = _lead_figures(row.pop("_lead_parts"))
        if not leads_available:
            leads = {k: (None if k != "lead_country_spellings" else [])
                     for k in leads}
        if not covered:
            spend_usd, status, reason = None, "unavailable", coverage["reason"]
        elif parts and not fx_ok:
            spend_usd, status, reason = None, "unavailable", R_FX_RATE_MISSING
        elif parts:
            spend_usd, status, reason = round(usd, 2), "published", None
        elif (row["country_resolved"] and unresolved_spend_present
              and row["country_code"] not in carried):
            spend_usd, status, reason = (None, "unavailable",
                                         R_SPEND_LOCATION_UNRESOLVED)
            withheld_zero.append(row["country"])
        elif row["country_resolved"]:
            # A resolved country, every day of the window settled, no spend row,
            # and no spend in the window that could be its own — either none
            # without a country, or only spend against locations that cannot
            # be this country (its code is another criterion's): a measured
            # zero.
            spend_usd, status, reason = 0.0, "published", None
            if row["country_code"] in carried_by_owner:
                owner_criteria.add(carried_by_owner[row["country_code"]])
        elif no_country:
            spend_usd, status, reason = None, "unavailable", R_NO_COUNTRY_RECORDED
        else:
            spend_usd, status, reason = None, "unavailable", R_COUNTRY_NOT_MATCHED
        rows.append({**row, "spend_usd": spend_usd, "spend_status": status,
                     "spend_reason": reason,
                     # PR-ADS-170A: "owner_labelled" when this row's spend (or
                     # the evidence its zero rests on) is under this country
                     # because the owner labelled the Google criterion.
                     "country_provenance": OWNER_LABELLED if owner_criteria else None,
                     "owner_labelled_criteria": sorted(c for c in owner_criteria if c),
                     **leads})
    rows.sort(key=lambda r: (r["spend_usd"] is None, -(r["spend_usd"] or 0.0),
                             -(r.get("total_leads") or 0), r["country"]))

    total = spend.get("total_spend_usd") if covered else None
    fx_complete = bool(spend.get("fx_complete")) if covered else False
    resolved_rows = [r for r in rows if r["country_resolved"]]
    countries_active = None
    if covered and fx_complete and leads_available:
        # Geographies, not spellings: resolved countries with spend or leads.
        countries_active = sum(
            1 for r in resolved_rows
            if (r["spend_usd"] or 0) > 0 or (r["total_leads"] or 0) > 0)
    return {
        "days": days,
        "window": window_key,
        "window_range": {"from": start.isoformat() if start else
                         coverage.get("covered_from"),
                         "through": end.isoformat(),
                         "ends_on": WINDOW_ENDS_ON},
        "spend_source": "google_ads_geo_daily_spend",
        "dataset": f"{CANONICAL_GEO_SOURCE}/{CANONICAL_GEO_DATASET}",
        "reporting_currency": (spend.get("reporting_currency") or "USD"),
        "account_time_zone": tz_name,
        "coverage": coverage,
        # Spend against Google locations no stored row names. One combined
        # row carries it; these say how many locations and how much.
        "unresolved_spend": {
            "criteria": len(unresolved_criteria),
            "conflicting_criteria": len(conflicting_criteria),
            "spend_usd": unresolved_usd if covered else None,
            # True when it withholds at least one country's zero. The names are
            # the countries it affects (PR-ADS-167A): only those never seen in
            # Google Ads geo spend, or every spend-less country when a criterion
            # in the window carries two codes.
            "blocks_zero": bool(covered and withheld_zero),
            "zero_withheld_countries": sorted(withheld_zero) if covered else [],
        },
        # PR-ADS-170A: the window's criteria whose country is the owner's label
        # (Google reports them without one), and any labelled criterion whose
        # stored Google code differs from its label — the stored code is used.
        "owner_labelled_geo": {**owner_geo, "note": OWNER_LABEL_NOTE},
        "spend_status": "published" if covered else "unavailable",
        "leads_status": "published" if leads_available else "unavailable",
        "leads_reason": None if leads_available else R_LEADS_UNREADABLE,
        "rows": rows,
        # None when any country's FX is incomplete: a total that leaves a
        # country out is a partial, and is not published as the total.
        "total_spend_usd": (round(float(total), 2)
                            if (covered and fx_complete and total is not None)
                            else None),
        "total_spend_reason": (None if (covered and fx_complete) else
                               (R_FX_RATE_MISSING if covered
                                else coverage["reason"])),
        "countries_active": countries_active,
        # Lead countries that name no single country. "No country recorded"
        # is not one of them: it is the absence of a country, not a mismatch.
        "unmatched_lead_countries": sum(
            1 for r in rows
            if not r["country_resolved"] and r["country_key"].startswith("lead:")
            and r["country_key"] != "lead:none"),
        "dropped_metrics": list(DROPPED_METRICS),
    }


__all__ = [
    "COVERAGE_COVERED", "COVERAGE_NOT_COVERED", "COVERAGE_UNAVAILABLE",
    "DROPPED_METRICS", "NO_COUNTRY_LABEL", "R_COUNTRY_NOT_MATCHED",
    "R_ACCOUNT_TZ_UNKNOWN", "R_DAY_NOT_REREAD", "R_NO_COUNTRY_RECORDED",
    "R_REREAD_FAILED", "R_SPEND_LOCATION_UNRESOLVED",
    "UNRESOLVED_COUNTRY_LABEL", "WINDOW_ENDS_ON", "account_zone",
    "build_country_evidence", "identify_spend_rows", "settled_chunks",
    "settled_through", "window_bounds",
]
