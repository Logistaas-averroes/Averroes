"""
analysis/advisor.py

Takes the three structured JSON outputs.
Calls Claude API with DOCTRINE.md as system prompt.
Returns plain-language weekly report.

Claude receives: structured findings only.
Claude does NOT: invent data, extrapolate, recommend actions not in the data.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from anthropic import Anthropic

client = Anthropic()
DOCTRINE_PATH = Path("docs/DOCTRINE.md")
MODEL = "claude-sonnet-4-6"


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def build_data_summary(waste, lead_quality, campaign_truth):
    """
    Assembles a clean summary of findings for Claude.
    Only includes what the data actually shows.
    """
    summary = {}

    # Waste findings
    if waste:
        summary["waste_detection"] = {
            "data_source": waste.get("data_source"),
            "data_warning": waste.get("data_warning"),
            "confirmed_waste_usd_estimate": waste.get("confirmed_waste_usd"),
            "top_waste_terms": waste.get("confirmed_waste_items", [])[:10],
        }
    else:
        summary["waste_detection"] = {"error": "Waste data unavailable"}

    # Lead quality
    if lead_quality:
        summary["lead_quality"] = {
            "total_contacts_analysed": lead_quality.get("total_contacts_analysed"),
            "by_campaign": [
                {
                    "campaign": c["campaign"],
                    "total": c["total"],
                    "qualified": c["qualified"],
                    "in_progress": c["in_progress"],
                    "junk": c["junk"],
                    "wrong_fit": c["wrong_fit"],
                    "unknown": c["unknown"],
                    "junk_rate_pct": c["junk_rate_pct"],
                    "warnings": c["warnings"],
                    "qualified_examples": c.get("qualified_examples", []),
                    "junk_examples": c.get("junk_examples", []),
                }
                for c in lead_quality.get("by_campaign", [])
            ]
        }
    else:
        summary["lead_quality"] = {"error": "Lead quality data unavailable"}

    # Campaign truth
    if campaign_truth:
        summary["campaign_truth"] = {
            "summary": campaign_truth.get("summary"),
            "campaigns": campaign_truth.get("campaigns", []),
        }
    else:
        summary["campaign_truth"] = {"error": "Campaign truth data unavailable"}

    return summary


def generate_weekly_report():
    os.makedirs("outputs", exist_ok=True)

    # Load all analysis outputs
    waste = load_json("outputs/waste_report.json")
    lead_quality = load_json("outputs/lead_quality.json")
    campaign_truth = load_json("outputs/campaign_truth.json")

    if not any([waste, lead_quality, campaign_truth]):
        print("No analysis outputs found. Run analysis scripts first.")
        return None

    # Load doctrine
    system_prompt = DOCTRINE_PATH.read_text()

    # Build clean data summary
    data_summary = build_data_summary(waste, lead_quality, campaign_truth)

    # Call Claude
    user_message = f"""
Here are the findings from this week's data analysis for Logistaas Google Ads.

DATA:
{json.dumps(data_summary, indent=2)}

Please produce the weekly report following the structure in your instructions:
1. Summary (what the data shows this week overall)
2. Waste this week
3. Lead quality by campaign
4. Campaign verdicts
5. What needs attention

Base everything on the data provided. If data is missing or uncertain, say so clearly.
Do not recommend actions not directly supported by the numbers.
"""

    print("Calling Claude API for weekly report...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        temperature=0,  # Deterministic — same data should produce same report
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    report_text = response.content[0].text
    date_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Save report
    report_path = f"outputs/weekly_report_{date_str}.md"
    with open(report_path, "w") as f:
        f.write(f"# Logistaas Weekly Ads Intelligence Report\n")
        f.write(f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write(report_text)

    print(f"Weekly report saved: {report_path}")
    print(f"Tokens used: {response.usage.input_tokens} in, {response.usage.output_tokens} out")

    return report_path


def generate_monthly_report():
    os.makedirs("outputs", exist_ok=True)

    # Load all analysis outputs
    waste = load_json("outputs/waste_report.json")
    lead_quality = load_json("outputs/lead_quality.json")
    campaign_truth = load_json("outputs/campaign_truth.json")

    if not any([waste, lead_quality, campaign_truth]):
        print("No analysis outputs found. Run analysis scripts first.")
        return None

    # Load doctrine
    system_prompt = DOCTRINE_PATH.read_text()

    # Build clean data summary
    data_summary = build_data_summary(waste, lead_quality, campaign_truth)

    # Call Claude
    user_message = f"""
Here are the findings from this month's data analysis for Logistaas Google Ads.

DATA:
{json.dumps(data_summary, indent=2)}

Please produce the monthly strategic report following the structure in your instructions:
1. Monthly Summary (what the data shows this month overall)
2. Waste this month
3. Lead quality by campaign
4. Campaign verdicts
5. Strategic recommendations for next month

Base everything on the data provided. If data is missing or uncertain, say so clearly.
Do not recommend actions not directly supported by the numbers.
"""

    print("Calling Claude API for monthly report...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        temperature=0,  # Deterministic — same data should produce same report
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    report_text = response.content[0].text
    month_str = datetime.utcnow().strftime("%Y-%m")

    # Save report
    report_path = f"outputs/monthly_report_{month_str}.md"
    with open(report_path, "w") as f:
        f.write(f"# Logistaas Monthly Ads Intelligence Report\n")
        f.write(f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write(report_text)

    print(f"Monthly report saved: {report_path}")
    print(f"Tokens used: {response.usage.input_tokens} in, {response.usage.output_tokens} out")

    return report_path


if __name__ == "__main__":
    generate_weekly_report()
