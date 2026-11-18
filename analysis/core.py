"""
analysis/core.py — search-term waste detection.

Identifies search terms spending money with zero qualified outcome.
Input: the canonical search-term rows the caller passes in (never a file), and
data/crm_contacts.json for the CRM cross-reference.
Outputs to: outputs/waste_report.json

No scoring models. No inference. Pattern matching + CRM cross-reference only.
"""

import json
import os
import yaml
from datetime import datetime

DATA_DIR = "data"
OUTPUT_DIR = "outputs"
CONFIG_DIR = "config"


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_patterns():
    with open(f"{CONFIG_DIR}/junk_patterns.yaml") as f:
        return yaml.safe_load(f)


def is_junk_term(term, patterns):
    """
    Returns (True, category, reason) if term matches a junk pattern.
    Returns (False, None, None) if no match.
    """
    term_lower = term.lower()
    safe_terms = [t.lower() for t in patterns.get("safe_terms", {}).get("terms", [])]

    # Check safe list first
    for safe in safe_terms:
        if safe in term_lower:
            return False, None, None

    # Check junk patterns
    for category, data in patterns.items():
        if category == "safe_terms":
            continue
        if not isinstance(data, dict) or "terms" not in data:
            continue
        for pattern in data["terms"]:
            if pattern.lower() in term_lower:
                return True, category, pattern

    return False, None, None


#: Sentinel distinguishing "the caller supplied no rows" from "the caller
#: supplied an empty population". `None` cannot carry that distinction, and the
#: distinction is the whole point of PR-ADS-156-F2 §1.
_UNSPECIFIED = object()

#: Why the current run has no search-term population to analyse.
WASTE_SEARCH_TERMS_UNAVAILABLE = "canonical_search_term_evidence_unavailable"


def run_waste_detection(search_terms=_UNSPECIFIED, *,
                        search_term_evidence_available=None):
    """Identify search terms spending money with no qualified outcome.

    PR-ADS-156-F2 §1 — the canonical input is PASSED IN, not re-read.

    This function used to reload ``data/ads_search_terms.json`` itself. F1 had
    already stopped the weekly and monthly schedulers overwriting that file when
    the canonical sync failed, precisely so an outage could not masquerade as a
    quiet week — but this function then loaded the *preserved* file and
    published findings from it stamped with the current run's timestamp. The
    snapshot was protected from being destroyed and immediately reused as
    though it were current, which is the same falsehood from the other side.

    Worse, an empty search-term population silently fell back to keyword-level
    rows. A verified-empty interval — asked, answered, nothing there — is a
    genuine measurement, and substituting a different population for it turns
    "no wasteful searches this week" into a keyword report wearing a search-term
    label.

    Three explicit inputs, three explicit behaviours:

    * ``search_term_evidence_available=False``, or no rows supplied at all — the
      sync failed, persisted only partially, or was never run. No search-term
      analysis is produced: the result is marked unavailable and carries no
      items.
    * ``search_terms=[…]``, available — exactly those rows are analysed. They
      are the rows the canonical sync actually persisted, so the report and the
      database describe the same observation by construction.
    * ``search_terms=[]``, available — a verified-empty population. Analysed as
      empty: zero findings, and no substitution.

    The keyword fallback is gone with the snapshot read. An empty search-term
    population used to silently become a keyword-level analysis, which answers a
    different question under the same heading: a verified-empty interval —
    asked, answered, nothing there — is a genuine measurement of search terms,
    not a gap to be filled with a different population.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    patterns = load_patterns()

    supplied = search_terms is not _UNSPECIFIED
    if search_term_evidence_available is None:
        # Not stated: available exactly when rows were supplied. A caller that
        # supplies nothing has given this function no evidence to work from, and
        # reaching for a file on disk to fill the gap is the defect above.
        search_term_evidence_available = supplied

    if not search_term_evidence_available:
        return _unavailable_waste_report()

    search_terms = list(search_terms or [])
    contacts = load_json(f"{DATA_DIR}/crm_contacts.json") or []

    # Build contact lookup by keyword (for CRM cross-reference)
    contacts_by_keyword = {}
    for contact in contacts:
        props = contact.get("properties", {})
        keyword = (props.get("hs_analytics_source_data_2") or "").lower()
        if keyword:
            contacts_by_keyword.setdefault(keyword, []).append(props.get("mql_status"))

    # One population, named. There is no fallback branch left to take.
    data_source = "canonical_search_terms"
    data_to_analyse = search_terms
    total_spend = sum(float(r.get("spend", 0) or 0) for r in data_to_analyse)

    confirmed_waste = []
    suspected_waste = []

    for row in data_to_analyse:
        term = row.get("search_term") or row.get("keyword") or ""
        if not term:
            continue

        spend = float(row.get("spend", 0) or 0)
        if spend < 5:  # Below minimum threshold
            continue

        is_junk, category, pattern = is_junk_term(term, patterns)
        if not is_junk:
            continue

        # CRM cross-reference
        term_lower = term.lower()
        crm_statuses = contacts_by_keyword.get(term_lower, [])
        junk_statuses = ["CLOSED - Job Seeker", "DICARDED", "CLOSED - Bad Product Fit"]
        crm_junk_count = sum(1 for s in crm_statuses if s in junk_statuses)
        crm_qualified_count = sum(
            1 for s in crm_statuses
            if s in ["CLOSED - Sales Qualified", "CLOSED - Deal Created"]
        )

        entry = {
            "term": term,
            "campaign": row.get("campaign", "unknown"),
            "spend_usd": round(spend, 2),
            "junk_category": category,
            "matched_pattern": pattern,
            "crm_contacts_found": len(crm_statuses),
            "crm_junk_confirmed": crm_junk_count,
            "crm_qualified": crm_qualified_count,
        }

        if crm_junk_count > 0 or (crm_contacts_found := len(crm_statuses)) == 0:
            confirmed_waste.append(entry) if crm_junk_count > 0 else suspected_waste.append(entry)
        else:
            suspected_waste.append(entry)

    # Sort by spend descending
    confirmed_waste.sort(key=lambda x: x["spend_usd"], reverse=True)
    suspected_waste.sort(key=lambda x: x["spend_usd"], reverse=True)

    total_confirmed_waste = sum(e["spend_usd"] for e in confirmed_waste)
    total_suspected_waste = sum(e["spend_usd"] for e in suspected_waste)

    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "data_source": data_source,
        # PR-ADS-156-F2 §1: stated, so a reader is never left inferring whether
        # an empty findings list means "clean week" or "no evidence".
        "search_term_evidence_available": True,
        "search_term_population": len(search_terms),
        "unavailable_reason": None,
        "data_warning": None,
        "total_spend_analysed": round(total_spend, 2),
        "confirmed_waste_usd": round(total_confirmed_waste, 2),
        "suspected_waste_usd": round(total_suspected_waste, 2),
        "confirmed_waste_items": confirmed_waste[:20],  # Top 20
        "suspected_waste_items": suspected_waste[:20],
    }

    out_path = f"{OUTPUT_DIR}/waste_report.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Waste detection complete.")
    print(f"  Data source: {data_source} ({len(search_terms)} row(s))")
    print(f"  Confirmed waste: ${total_confirmed_waste:.2f}")
    print(f"  Suspected waste: ${total_suspected_waste:.2f}")
    print(f"  Top waste item: {confirmed_waste[0]['term'] if confirmed_waste else 'none'}")

    return output


def _unavailable_waste_report() -> dict:
    """The report for a run whose canonical search-term evidence never arrived.

    PR-ADS-156-F2 §1. It is WRITTEN rather than skipped, and it carries no
    items. Leaving the previous ``waste_report.json`` untouched would be worse
    than either alternative: the file's own ``generated_at`` is what every
    reader uses to decide how current it is, so a preserved report from last
    week reads as this week's findings. An explicitly empty, explicitly
    unavailable report cannot be mistaken for either a clean week or a stale
    one. The durable canonical evidence is untouched — this is a derived
    analysis output, regenerated every run.
    """
    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "data_source": None,
        "search_term_evidence_available": False,
        "search_term_population": None,
        "unavailable_reason": WASTE_SEARCH_TERMS_UNAVAILABLE,
        "data_warning": (
            "Canonical search-term evidence was not persisted for this run, so "
            "no search-term waste analysis was performed. This is NOT a finding "
            "of zero waste, and no previous snapshot was reused."
        ),
        "total_spend_analysed": None,
        "confirmed_waste_usd": None,
        "suspected_waste_usd": None,
        "confirmed_waste_items": [],
        "suspected_waste_items": [],
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(f"{OUTPUT_DIR}/waste_report.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print("Waste detection SKIPPED — canonical search-term evidence unavailable.")
    print("  No previous snapshot was analysed, and no keyword fallback was used.")
    return output


# ─────────────────────────────────────────────────────────────────────────────
# PR-ADS-162H — what used to follow here is deleted.
#
# This module also held `run_lead_quality` (wrote outputs/lead_quality.json),
# `run_campaign_truth` (wrote outputs/campaign_truth.json, and fed the legacy
# `campaigns` snapshot) and `determine_verdict` (SCALE / HOLD / FIX / CUT). All
# three existed for the weekly / monthly advisor report, which is no longer
# generated: they counted SQLs from an undated, undeduplicated JSON contact
# pull — a third SQL definition no page reconciled against.
#
# One name survives because a live module imports it.

#: HubSpot `mql_status` values that mean a contact is sales-qualified. Imported
#: by services/revenue_attribution_service.py for its diagnostic JSON fallback.
QUALIFIED = ["CLOSED - Sales Qualified", "CLOSED - Deal Created"]


if __name__ == "__main__":
    run_waste_detection()
