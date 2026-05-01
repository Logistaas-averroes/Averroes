"""
analysis/waste_detection.py

Identifies search terms spending money with zero qualified outcome.
Reads from: data/ads_search_terms.json, data/ads_keywords.json, data/crm_contacts.json
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


def run_waste_detection():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    patterns = load_patterns()

    # Load search terms (primary)
    search_terms = load_json(f"{DATA_DIR}/ads_search_terms.json") or []
    keywords = load_json(f"{DATA_DIR}/ads_keywords.json") or []
    contacts = load_json(f"{DATA_DIR}/crm_contacts.json") or []

    # Build contact lookup by keyword (for CRM cross-reference)
    contacts_by_keyword = {}
    for contact in contacts:
        props = contact.get("properties", {})
        keyword = (props.get("hs_analytics_source_data_2") or "").lower()
        if keyword:
            contacts_by_keyword.setdefault(keyword, []).append(props.get("mql_status"))

    # Determine data completeness
    using_fallback = len(search_terms) == 0
    data_source = "search_terms" if not using_fallback else "keywords_fallback"

    data_to_analyse = search_terms if not using_fallback else keywords
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
        "data_warning": (
            "Search term data unavailable. Analysis uses keyword-level data only. "
            "True waste may be 20-40% higher than shown here."
            if using_fallback else None
        ),
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
    print(f"  Data source: {data_source}")
    if using_fallback:
        print(f"  WARNING: Using keyword fallback — search terms unavailable")
    print(f"  Confirmed waste: ${total_confirmed_waste:.2f}")
    print(f"  Suspected waste: ${total_suspected_waste:.2f}")
    print(f"  Top waste item: {confirmed_waste[0]['term'] if confirmed_waste else 'none'}")

    return output


if __name__ == "__main__":
    run_waste_detection()


# ─────────────────────────────────────────────────────────────────────────────


"""
analysis/lead_quality.py

Groups paid contacts by campaign and counts MQL status outcomes.
No scoring. No inference. Direct HubSpot MQL status aggregation only.
Reads from: data/crm_contacts.json
Outputs to: outputs/lead_quality.json
"""

import json
import os
import yaml
from datetime import datetime, timedelta, timezone


QUALIFIED = ["CLOSED - Sales Qualified", "CLOSED - Deal Created"]
IN_PROGRESS = ["OPEN - Meeting Booked", "OPEN - Pending Meeting"]
UNKNOWN = ["OPEN - Connecting"]
JUNK = ["CLOSED - Job Seeker", "DICARDED"]
WRONG_FIT = ["CLOSED - Bad Product Fit", "CLOSED - Sales Disqualified"]


def run_lead_quality():
    os.makedirs("outputs", exist_ok=True)

    contacts = load_json("data/crm_contacts.json") or []
    if not contacts:
        print("No contact data found. Run hubspot_pull.py first.")
        return {}

    with open("config/thresholds.yaml") as f:
        thresholds = yaml.safe_load(f)

    grace_days = thresholds["lead_quality"]["early_lead_grace_days"]
    min_sample = thresholds["lead_quality"]["small_sample_warning_threshold"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)

    by_campaign = {}

    for contact in contacts:
        props = contact.get("properties", {})
        campaign = props.get("hs_analytics_source_data_1") or "unknown"
        status = props.get("mql_status") or None
        country = props.get("ip_country") or "unknown"
        keyword = props.get("hs_analytics_source_data_2") or "unknown"
        created_str = props.get("createdate") or ""

        # Parse creation date for grace period
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            is_recent = created > cutoff
        except (ValueError, AttributeError):
            is_recent = False

        entry = by_campaign.setdefault(campaign, {
            "campaign": campaign,
            "total": 0,
            "qualified": 0,
            "in_progress": 0,
            "unknown": 0,
            "junk": 0,
            "wrong_fit": 0,
            "no_status": 0,
            "qualified_examples": [],
            "junk_examples": [],
            "warnings": [],
        })

        entry["total"] += 1

        if status in QUALIFIED:
            entry["qualified"] += 1
            entry["qualified_examples"].append({
                "contact_id": contact.get("id"),
                "country": country,
                "keyword": keyword,
                "status": status,
            })
        elif status in IN_PROGRESS:
            entry["in_progress"] += 1
        elif status in UNKNOWN:
            entry["unknown"] += 1
        elif status in JUNK:
            if is_recent:
                entry["unknown"] += 1  # Too recent to classify as junk definitively
            else:
                entry["junk"] += 1
                entry["junk_examples"].append({
                    "contact_id": contact.get("id"),
                    "country": country,
                    "keyword": keyword,
                    "status": status,
                })
        elif status in WRONG_FIT:
            entry["wrong_fit"] += 1
        else:
            entry["no_status"] += 1

    # Calculate junk rate (excluding unknown/connecting)
    results = []
    for campaign, data in by_campaign.items():
        verdicted = data["qualified"] + data["in_progress"] + data["junk"] + data["wrong_fit"]
        junk_rate = round(data["junk"] / verdicted * 100, 1) if verdicted > 0 else None

        data["verdicted_contacts"] = verdicted
        data["junk_rate_pct"] = junk_rate
        data["cpql"] = None  # Calculated in campaign_truth.py with spend data

        if data["total"] < min_sample:
            data["warnings"].append(f"Insufficient sample ({data['total']} contacts) — results not statistically meaningful")

        if verdicted == 0:
            data["warnings"].append("No MDR verdicts yet — all contacts in connecting state")

        # Trim examples to top 3
        data["qualified_examples"] = data["qualified_examples"][:3]
        data["junk_examples"] = data["junk_examples"][:3]

        results.append(data)

    results.sort(key=lambda x: x["qualified"], reverse=True)

    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "total_contacts_analysed": len(contacts),
        "by_campaign": results,
    }

    with open("outputs/lead_quality.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Lead quality analysis complete. {len(contacts)} contacts, {len(results)} campaigns.")
    for r in results:
        print(f"  {r['campaign']}: {r['qualified']} SQL, {r['junk']} junk, {r['junk_rate_pct']}% junk rate")

    return output


if __name__ == "__main__":
    run_lead_quality()


# ─────────────────────────────────────────────────────────────────────────────


"""
analysis/campaign_truth.py

Joins Windsor spend data with HubSpot MQL counts.
Calculates CPQL where confirmed SQLs exist.
Applies FIX/HOLD/SCALE/CUT verdict based on threshold rules from config.
Reads from: data/ads_campaigns.json, outputs/lead_quality.json
Outputs to: outputs/campaign_truth.json
"""

import json
import os
import re
import yaml
from datetime import datetime

# Canonical campaign name map: Windsor variant → HubSpot UTM convention.
# All keys are lowercase (matched after .lower() on input).
# Mirrors _CAMPAIGN_CANONICAL in db/writers.py — keep both in sync when adding entries.
# "mexico, chile, colombia" → "mexico,chile": HubSpot UTM tracks this campaign without
# Colombia in the name; both Windsor and HubSpot refer to the same single campaign.
_CAMPAIGN_CANONICAL = {
    "mexico, chile, colombia":  "mexico,chile",
    "compliance markets":       "compliance - markets",
    "emerging markets":         "emerging - markets",
    "mature markets":           "mature - markets",
    "europe low-cpc-2026":      "europe low cpc-new",
}

# HubSpot traffic source values that appear as campaign_name — not real campaigns
_HUBSPOT_SOURCE_PSEUDONAMES = {
    "(referral)", "(organic)", "(direct)", "(not set)",
    "(cross-network)", "(none)", "(content)", "(social)",
}

_EMAIL_CAMPAIGN_RE = re.compile(r"email_campaign", re.IGNORECASE)


def _canonicalise_campaign_name(name):
    """Apply canonical name mapping (Windsor variant → HubSpot UTM convention)."""
    if name is None:
        return None
    return _CAMPAIGN_CANONICAL.get(name, name)


def _clean_campaign_name(name):
    """Normalise and validate a campaign name from HubSpot source data.

    Returns None for pseudo-names, email campaign IDs, and empty values.
    Applies canonical name mapping after lowercasing.
    """
    if not name:
        return None
    stripped = name.strip()
    if not stripped:
        return None
    if stripped.lower() in _HUBSPOT_SOURCE_PSEUDONAMES:
        return None
    if _EMAIL_CAMPAIGN_RE.search(stripped):
        return None
    return _canonicalise_campaign_name(stripped.lower())


def _is_real_campaign(name):
    """Return True if the campaign name represents a real Google Ads campaign."""
    if not name:
        return False
    if name in _HUBSPOT_SOURCE_PSEUDONAMES:
        return False
    if _EMAIL_CAMPAIGN_RE.search(name):
        return False
    return True


def run_campaign_truth():
    os.makedirs("outputs", exist_ok=True)

    ads_data = load_json("data/ads_campaigns.json") or []
    lead_quality = load_json("outputs/lead_quality.json") or {}

    if not ads_data:
        print("No Windsor campaign data. Run windsor_pull.py first.")
        return {}

    with open("config/thresholds.yaml") as f:
        thresholds = yaml.safe_load(f)

    scale_t = thresholds["campaign_verdicts"]["scale"]
    fix_t = thresholds["campaign_verdicts"]["fix"]
    confirmed_cut = set(thresholds.get("confirmed_cut_markets", []))

    # Build spend by campaign (Windsor source — apply canonical name mapping)
    spend_by_campaign = {}
    for row in ads_data:
        raw_name = (row.get("campaign") or "").strip().lower()
        if not raw_name:
            continue
        canonical = _canonicalise_campaign_name(raw_name)
        spend = float(row.get("spend", 0) or 0)
        spend_by_campaign[canonical] = spend_by_campaign.get(canonical, 0) + spend

    # Build lead quality by campaign (HubSpot source — filter pseudo-names and canonicalise)
    lq_by_campaign = {}
    for lq in lead_quality.get("by_campaign", []):
        raw_name = lq.get("campaign", "")
        canonical = _clean_campaign_name(raw_name)
        if canonical:
            lq_by_campaign[canonical] = lq

    # Build truth table — one merged row per canonical campaign name
    rows = []
    all_campaigns = set(spend_by_campaign.keys()) | set(lq_by_campaign.keys())

    for campaign in all_campaigns:
        if not _is_real_campaign(campaign):
            continue

        spend = round(spend_by_campaign.get(campaign, 0), 2)
        lq = lq_by_campaign.get(campaign, {})

        total_leads = lq.get("total", 0)
        confirmed_sqls = lq.get("qualified", 0)
        junk_count = lq.get("junk", 0)
        junk_rate = lq.get("junk_rate_pct")
        warnings = lq.get("warnings", [])

        # CPQL
        cpql = None
        if confirmed_sqls > 0 and spend > 0:
            cpql = round(spend / confirmed_sqls, 2)

        # Verdict
        verdict = determine_verdict(
            spend=spend,
            confirmed_sqls=confirmed_sqls,
            junk_rate=junk_rate,
            campaign=campaign,
            confirmed_cut_markets=confirmed_cut,
            scale_thresholds=scale_t,
            fix_thresholds=fix_t,
        )

        rows.append({
            "campaign_name": campaign,
            "spend_usd": spend,
            "total_leads": total_leads,
            "confirmed_sqls": confirmed_sqls,
            "junk_count": junk_count,
            "junk_rate_pct": junk_rate,
            "cpql_usd": cpql,
            "verdict": verdict["state"],
            "verdict_reason": verdict["reason"],
            "warnings": warnings,
        })

    # Sort by verdict priority then spend
    verdict_order = {"FIX": 0, "CUT": 1, "SCALE": 2, "HOLD": 3}
    rows.sort(key=lambda x: (verdict_order.get(x["verdict"], 99), -x["spend_usd"]))

    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "campaigns": rows,
        "summary": {
            "fix_count": sum(1 for r in rows if r["verdict"] == "FIX"),
            "cut_count": sum(1 for r in rows if r["verdict"] == "CUT"),
            "scale_count": sum(1 for r in rows if r["verdict"] == "SCALE"),
            "hold_count": sum(1 for r in rows if r["verdict"] == "HOLD"),
            "total_spend_usd": round(sum(r["spend_usd"] for r in rows), 2),
            "total_confirmed_sqls": sum(r["confirmed_sqls"] for r in rows),
        },
    }

    with open("outputs/campaign_truth.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Campaign truth table complete. {len(rows)} campaigns.")
    summary = output["summary"]
    print(f"  FIX: {summary['fix_count']} | SCALE: {summary['scale_count']} | HOLD: {summary['hold_count']} | CUT: {summary['cut_count']}")
    print(f"  Total spend: ${summary['total_spend_usd']:,.2f} | Total SQLs: {summary['total_confirmed_sqls']}")

    return output


def determine_verdict(spend, confirmed_sqls, junk_rate, campaign,
                      confirmed_cut_markets, scale_thresholds, fix_thresholds):
    """
    Applies FIX/HOLD/SCALE/CUT verdict.
    Returns dict with state and reason.
    All rules sourced from thresholds.yaml.
    """
    # CUT — must be explicitly confirmed
    if campaign.lower().replace(" ", "_") in {m.replace(" ", "_") for m in confirmed_cut_markets}:
        return {"state": "CUT", "reason": "Confirmed wrong market — no qualified pipeline possible"}

    # SCALE — requires confirmed SQLs AND clean junk rate
    if (confirmed_sqls >= scale_thresholds["min_confirmed_sqls_30d"] and
            junk_rate is not None and
            junk_rate <= scale_thresholds["max_junk_pct"]):
        return {
            "state": "SCALE",
            "reason": f"{confirmed_sqls} confirmed SQL(s) | {junk_rate}% junk rate"
        }

    # FIX — high junk rate
    if junk_rate is not None and junk_rate >= fix_thresholds["min_junk_pct"]:
        return {
            "state": "FIX",
            "reason": f"{junk_rate}% junk rate exceeds {fix_thresholds['min_junk_pct']}% threshold"
        }

    # FIX — zero SQLs with meaningful spend (implies waste without signal)
    if confirmed_sqls == 0 and spend > 200 and fix_thresholds.get("zero_sqls_with_intent_mismatch"):
        return {
            "state": "FIX",
            "reason": f"${spend:.0f} spend, 0 confirmed SQLs — review intent and targeting"
        }

    # HOLD — default
    return {
        "state": "HOLD",
        "reason": "Insufficient data to classify or no clear signal"
    }


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    run_campaign_truth()
