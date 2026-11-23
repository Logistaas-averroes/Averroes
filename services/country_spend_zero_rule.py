"""
services/country_spend_zero_rule.py

PR-ADS-176A — ONE rule for "this country spent $0", shared by every builder
that publishes per-country Google Ads spend: the Countries page
(``services/country_evidence_service.py``), the revenue attribution country
rows the mart's country view serves (Country Revenue & Spend), and Dashboard →
Countries (``services/dashboard_countries_service.py``).

Why it exists
-------------
Before this module only the Countries page applied the PR-ADS-167A zero rule.
The other two started every country at ``0.0`` and called any country with no
geo row a measured zero whenever the geo read succeeded. So with spend in the
window that Google reported against a location no stored row names (a
code-less criterion such as ``2997``), Pakistan — leads, no Google spend ever —
read ``$0`` on the Dashboard and on Country Revenue & Spend and "Unavailable"
on Countries, over the same evidence. One figure, two rules.

The rule (moved here from ``country_evidence_service``, unchanged)
-----------------------------------------------------------------
A country WITH a spend row in the window is unaffected. A country with NO spend
row is a measured ``$0`` when no spend in the window can be its own:

* there is no unresolved spend in the window (every spend row names a
  country), or
* every unresolved criterion in the window is code-less EVERYWHERE (none
  carries two codes — that one IS some country), AND the country's code is
  carried on record by exactly one criterion that carries no other code
  (``repo.fetch_geo_code_carriers``). Google keys one country under one
  criterion, so a location no stored row names cannot be a country another
  criterion carries.

Otherwise its spend is unavailable with ``spend_location_unresolved``. A
country never seen in Google Ads geo spend (leads or deals, never a geo row)
is never carried, so it stays unavailable while there is unresolved spend.
The carrier read failing is "carried by nothing": fail closed.

One snapshot
------------
The spend rows (whose identities are resolved in the same statement), any
separate identity read, and the carriers read must answer from ONE snapshot,
or a sync naming a code-less criterion between them turns an "Unavailable"
into a false ``$0`` (PR-ADS-167A). :func:`assess` therefore must be called
inside the ``repo.geo_read_snapshot()`` the spend rows were read in;
:func:`read_country_spend` does both, scoped to one Google Ads account
(PR-ADS-154B).

Read-only. No Google Ads or HubSpot call.
"""
from __future__ import annotations

import logging

from analysis.geo_owner_labels import PROVENANCE_OWNER_LABELLED as OWNER_LABELLED

log = logging.getLogger(__name__)

#: A country with no spend row, in a window that holds spend Google reported
#: against a location no stored row names: that spend may be this country's.
R_SPEND_LOCATION_UNRESOLVED = "spend_location_unresolved"
#: The geo rows (or the evidence the rule needs) could not be read.
R_ZERO_EVIDENCE_UNREADABLE = "geo_rows_unreadable"


def _code(value) -> str:
    return str(value or "").strip().upper()


def _row_spend_unknown_or_nonzero(row: dict) -> bool:
    """Whether a spend row holds spend (or an amount nobody can state)."""
    micros = row.get("cost_micros")
    if micros is not None:
        try:
            return int(micros) != 0
        except (TypeError, ValueError):
            return True
    native = row.get("spend")
    if native is None:
        return True
    try:
        return float(native) != 0.0
    except (TypeError, ValueError):
        return True


def assess(spend_rows: list, candidate_codes, *,
           owner_labelled_criteria=()) -> dict:
    """The zero-rule evidence for one window's spend rows.

    MUST be called inside the ``repo.geo_read_snapshot()`` the spend rows were
    read in: it reads the carriers of ``candidate_codes``.

    ``spend_rows`` are the window's rows with their criterion identity
    applied: ``country_code`` (None when the criterion names no single
    country), ``identity_codes`` (every stored code of the criterion) and
    ``country_criterion_id``. ``candidate_codes`` are the codes of the
    countries the caller would publish; codes with a spend row in the window
    are ignored (their spend is a measurement). ``owner_labelled_criteria``
    are the window's criteria whose country is the owner's label
    (PR-ADS-170A), used only for the Countries page's disclosure.

    Returns ``{unresolved_spend_present, unresolved_criteria,
    conflicting_criteria, spend_codes, carried, carried_by_owner,
    carried_by_google, owner_in_window, carriers_available}``.
    """
    from db import revenue_repository as repo  # noqa: PLC0415

    spend_codes: set = set()
    unresolved: set = set()
    conflicting: set = set()
    present = False
    for r in spend_rows or []:
        code = _code(r.get("country_code"))
        if code:
            spend_codes.add(code)
            continue
        cid = str(r.get("country_criterion_id") or "").strip()
        unresolved.add(cid)
        if len(set(r.get("identity_codes") or [])) > 1:
            conflicting.add(cid)
        if _row_spend_unknown_or_nonzero(r):
            present = True
    owner_in_window = sorted({str(c) for c in owner_labelled_criteria or () if c})
    candidates = sorted({_code(c) for c in candidate_codes or () if _code(c)}
                        - spend_codes)

    carried: set = set()
    carried_by_owner: dict = {}
    carried_by_google: set = set()
    carriers_available = None
    if (present or owner_in_window) and not conflicting and candidates:
        found = repo.fetch_geo_code_carriers(candidates)
        carriers_available = bool(found.get("available"))
        if carriers_available:
            for code, holders in (found.get("carriers") or {}).items():
                if len(holders) == 1 and holders[0].get("codes") == [code]:
                    carried.add(code)
                    if holders[0].get("provenance") == OWNER_LABELLED:
                        carried_by_owner[code] = holders[0].get("criterion_id")
                    else:
                        carried_by_google.add(code)
        else:
            log.error("[country-zero-rule] carrier read failed; no zero is proven")
    return {
        "unresolved_spend_present": present,
        "unresolved_criteria": sorted(unresolved),
        "conflicting_criteria": sorted(conflicting),
        "spend_codes": spend_codes,
        "carried": carried,
        "carried_by_owner": carried_by_owner,
        "carried_by_google": carried_by_google,
        "owner_in_window": owner_in_window,
        "carriers_available": carriers_available,
    }


def zero_spend_verdict(evidence: dict | None, code) -> tuple:
    """``(publishable, reason)`` for a RESOLVED country with no spend row in a
    window whose geo spend was read.

    ``publishable`` True means its spend is a measured ``0``. With no evidence
    (the geo read failed) nothing is proven.
    """
    if evidence is None:
        return False, R_ZERO_EVIDENCE_UNREADABLE
    if not evidence.get("unresolved_spend_present"):
        return True, None
    if _code(code) in (evidence.get("carried") or set()):
        return True, None
    return False, R_SPEND_LOCATION_UNRESOLVED


def zero_rests_on_owner_labels(evidence: dict | None, code) -> list:
    """The owner-labelled criteria a PUBLISHED ``$0`` for ``code`` rests on.

    PR-ADS-170A-F1, shared since PR-ADS-176B-F1 (it lived only in the deleted
    Countries evidence page): the country's own carrier when the owner
    labelled it, and every owner-labelled criterion in the window unless a
    Google-named criterion carries the code (labelled spend would otherwise
    have no country and withhold the zero). ``[]`` when the zero stands
    without any label. Only meaningful where ``zero_spend_verdict`` publishes.
    """
    ev = evidence or {}
    c = _code(code)
    rests: set = set()
    owner_carrier = (ev.get("carried_by_owner") or {}).get(c)
    if owner_carrier:
        rests.add(str(owner_carrier))
    if c not in (ev.get("carried_by_google") or set()):
        rests.update(str(x) for x in ev.get("owner_in_window") or () if x)
    return sorted(rests)


def read_country_spend(start, end, customer_id, candidate_codes) -> dict:
    """The window's per-country geo spend AND its zero-rule evidence, from ONE
    snapshot, scoped to ``customer_id`` (the configured account; None keeps
    the account-wide read).

    Returns ``{"spend": <fetch_geo_daily_spend_by_country result>,
    "zero_rule": <assess() result, or None when the rows were unreadable>}``.
    """
    from db import revenue_repository as repo  # noqa: PLC0415

    # PR-ADS-154B: scoped to the configured account. Passed only when one is
    # configured, so an unconfigured deployment keeps its account-wide read.
    scope = {"customer_id": customer_id} if customer_id else {}
    try:
        with repo.geo_read_snapshot():
            spend = repo.fetch_geo_daily_spend_by_country(start, end, **scope)
            if not spend.get("available"):
                return {"spend": spend, "zero_rule": None}
            rows = spend.get("rows") or []
            owner = [r.get("country_criterion_id") for r in rows
                     if r.get("identity_provenance") == OWNER_LABELLED]
            evidence = assess(rows, candidate_codes, owner_labelled_criteria=owner)
    except Exception as exc:  # noqa: BLE001 — the snapshot could not be opened
        # Unknown, never "no spend": the rows are unreadable, as when the
        # reader itself fails, and no zero is proven.
        log.error("[country-zero-rule] geo read snapshot failed: %s", exc)
        return {"spend": {"available": False, "has_rows": False, "rows": []},
                "zero_rule": None}
    return {"spend": spend, "zero_rule": evidence}


__all__ = [
    "R_SPEND_LOCATION_UNRESOLVED", "R_ZERO_EVIDENCE_UNREADABLE", "assess",
    "read_country_spend", "zero_rests_on_owner_labels", "zero_spend_verdict",
]
