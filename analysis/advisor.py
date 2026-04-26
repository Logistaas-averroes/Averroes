"""
analysis/advisor.py

Dispatches report generation to the appropriate advisor based on ADVISOR_MODE.

Default:   ADVISOR_MODE=deterministic  — uses analysis/rule_advisor.py
Optional:  ADVISOR_MODE=claude         — calls Claude API (requires ANTHROPIC_API_KEY)

Environment variables:
  ADVISOR_MODE         — "deterministic" (default) or "claude"
  ANTHROPIC_API_KEY    — required only when ADVISOR_MODE=claude

Backward compatibility:
  generate_weekly_report() and generate_monthly_report() continue to work
  for all existing scheduler calls.
"""

import json
import os
from datetime import datetime
from pathlib import Path

# Deterministic advisor — imported at module level so it is always available
# without requiring ANTHROPIC_API_KEY.
from analysis.rule_advisor import generate_deterministic_report


def _get_advisor_mode() -> str:
    return os.getenv("ADVISOR_MODE", "deterministic").lower().strip()


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


# ── Claude helpers (only used when ADVISOR_MODE=claude) ──────────────────────

def _build_data_summary(waste, lead_quality, campaign_truth):
    """Assembles a structured findings dict for the Claude prompt."""
    summary = {}

    if waste:
        summary["waste_detection"] = {
            "data_source": waste.get("data_source"),
            "data_warning": waste.get("data_warning"),
            "confirmed_waste_usd_estimate": waste.get("confirmed_waste_usd"),
            "top_waste_terms": waste.get("confirmed_waste_items", [])[:10],
        }
    else:
        summary["waste_detection"] = {"error": "Waste data unavailable"}

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

    if campaign_truth:
        summary["campaign_truth"] = {
            "summary": campaign_truth.get("summary"),
            "campaigns": campaign_truth.get("campaigns", []),
        }
    else:
        summary["campaign_truth"] = {"error": "Campaign truth data unavailable"}

    return summary


def _claude_report(report_type: str) -> str | None:
    """
    Generate a report via Claude API.
    Only called when ADVISOR_MODE=claude.

    Raises RuntimeError if ANTHROPIC_API_KEY is not set.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ADVISOR_MODE=claude requires ANTHROPIC_API_KEY to be set. "
            "Set ADVISOR_MODE=deterministic to use the built-in deterministic advisor instead."
        )

    from anthropic import Anthropic  # noqa: PLC0415

    client = Anthropic()
    doctrine_path = Path("docs/DOCTRINE.md")

    os.makedirs("outputs", exist_ok=True)
    waste = load_json("outputs/waste_report.json")
    lead_quality = load_json("outputs/lead_quality.json")
    campaign_truth = load_json("outputs/campaign_truth.json")

    if not any([waste, lead_quality, campaign_truth]):
        print("No analysis outputs found. Run analysis scripts first.")
        return None

    system_prompt = doctrine_path.read_text()
    data_summary = _build_data_summary(waste, lead_quality, campaign_truth)

    if report_type == "weekly":
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
        title = "Logistaas Weekly Ads Intelligence Report"
        report_path = f"outputs/weekly_report_{datetime.utcnow().strftime('%Y-%m-%d')}.md"
    else:
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
        title = "Logistaas Monthly Ads Intelligence Report"
        report_path = f"outputs/monthly_report_{datetime.utcnow().strftime('%Y-%m')}.md"

    print(f"Calling Claude API for {report_type} report...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    report_text = response.content[0].text

    with open(report_path, "w") as f:
        f.write(f"# {title}\n")
        f.write(f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write(report_text)

    print(f"{report_type.capitalize()} report saved: {report_path}")
    print(f"Tokens used: {response.usage.input_tokens} in, {response.usage.output_tokens} out")
    return report_path


# ── Public API ────────────────────────────────────────────────────────────────

def generate_weekly_report() -> str | None:
    """
    Generate the weekly report.

    Uses deterministic mode by default (ADVISOR_MODE=deterministic or unset).
    Falls back to Claude API only when ADVISOR_MODE=claude.
    """
    mode = _get_advisor_mode()
    if mode == "claude":
        return _claude_report("weekly")
    return generate_deterministic_report("weekly")


def generate_monthly_report() -> str | None:
    """
    Generate the monthly report.

    Uses deterministic mode by default (ADVISOR_MODE=deterministic or unset).
    Falls back to Claude API only when ADVISOR_MODE=claude.

    Returns the report file path on success, or None if analysis data is missing.
    """
    mode = _get_advisor_mode()
    if mode == "claude":
        return _claude_report("monthly")
    return generate_deterministic_report("monthly")


if __name__ == "__main__":
    generate_weekly_report()
