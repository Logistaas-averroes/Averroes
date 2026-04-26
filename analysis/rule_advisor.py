"""
analysis/rule_advisor.py

Generates deterministic weekly/monthly markdown reports from structured
analysis outputs.  Replaces Claude as the default report writer.

Reads:
  outputs/waste_report.json
  outputs/lead_quality.json
  outputs/campaign_truth.json
  config/thresholds.yaml
  config/junk_patterns.yaml  (optional — for waste category labels)

Outputs:
  outputs/weekly_report_YYYY-MM-DD.md
  outputs/monthly_report_YYYY-MM.md

Rules:
  - No Claude API calls.
  - No external API calls.
  - No HubSpot or Windsor calls.
  - No Google Ads writes.
  - No hidden scoring model.
  - Language: no "you must pause", no "automatically change".
    Use "warrants review" and "suggests attention".
  - CPQL shows N/A when confirmed SQL = 0.
  - Google Ads conversions are not presented as business performance.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any

import yaml

OUTPUT_DIR = "outputs"
CONFIG_DIR = "config"


# ── Data loading helpers ──────────────────────────────────────────────────────

def _load_json(path: str) -> Any:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_thresholds() -> dict:
    path = f"{CONFIG_DIR}/thresholds.yaml"
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_junk_patterns() -> dict:
    path = f"{CONFIG_DIR}/junk_patterns.yaml"
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_usd(value) -> str:
    """Format a dollar value.  Returns N/A if None or zero-SQL context."""
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1f}%"


def _fmt_int(value) -> str:
    if value is None:
        return "N/A"
    return str(int(value))


def _verdict_label(verdict: str) -> str:
    labels = {
        "SCALE": "✅ SCALE",
        "HOLD":  "⏸ HOLD",
        "FIX":   "⚠️ FIX",
        "CUT":   "🔴 CUT",
    }
    return labels.get(verdict, verdict or "—")


def _data_availability(waste, lead_quality, campaign_truth) -> list[str]:
    """Return list of data-availability notes."""
    notes = []
    if waste is None:
        notes.append("Waste detection data unavailable — `outputs/waste_report.json` not found.")
    elif waste.get("data_warning"):
        notes.append(f"Waste data warning: {waste['data_warning']}")

    if lead_quality is None:
        notes.append("Lead quality data unavailable — `outputs/lead_quality.json` not found.")

    if campaign_truth is None:
        notes.append("Campaign truth data unavailable — `outputs/campaign_truth.json` not found.")

    if not notes:
        notes.append("All three analysis outputs are present.")

    return notes


# ── Section builders ──────────────────────────────────────────────────────────

def _build_executive_summary(waste, lead_quality, campaign_truth, report_type: str) -> str:
    period = "this week" if report_type == "weekly" else "this month"
    lines = [f"## 1. Executive Summary\n"]

    if campaign_truth:
        s = campaign_truth.get("summary", {})
        total_spend = s.get("total_spend_usd", 0)
        total_sqls = s.get("total_confirmed_sqls", 0)
        n_fix = s.get("fix_count", 0)
        n_scale = s.get("scale_count", 0)
        n_hold = s.get("hold_count", 0)
        n_cut = s.get("cut_count", 0)
        n_campaigns = n_fix + n_scale + n_hold + n_cut

        lines.append(
            f"Analysis covers **{n_campaigns} campaign(s)** with total spend of "
            f"**{_fmt_usd(total_spend)}** {period}."
        )
        lines.append(
            f"Confirmed SQLs: **{total_sqls}**. "
            f"Verdict breakdown: {n_scale} SCALE · {n_fix} FIX · {n_hold} HOLD · {n_cut} CUT."
        )
    else:
        lines.append("Campaign spend and verdict data not yet available for this period.")

    if waste and waste.get("confirmed_waste_usd") is not None:
        cw = waste["confirmed_waste_usd"]
        sw = waste.get("suspected_waste_usd", 0) or 0
        lines.append(
            f"Confirmed waste identified: **{_fmt_usd(cw)}**. "
            f"Suspected additional waste: **{_fmt_usd(sw)}**."
        )
        if waste.get("data_source") == "keywords_fallback":
            lines.append(
                "> ⚠️ Waste figures are based on keyword-level data (search term data unavailable). "
                "Actual waste may be 20–40% higher."
            )

    if lead_quality:
        total_contacts = lead_quality.get("total_contacts_analysed", 0)
        lines.append(f"Lead quality analysis covers **{total_contacts}** paid contacts.")

    if report_type == "monthly":
        lines.append(
            "\nThis monthly report consolidates findings from the 30-day analysis window. "
            "Trends and persistent patterns are highlighted in each section."
        )

    return "\n".join(lines) + "\n"


def _build_campaign_truth_table(campaign_truth) -> str:
    lines = ["## 2. Campaign Truth Table\n"]

    if campaign_truth is None:
        lines.append("_Campaign truth data not available. Run analysis scripts first._\n")
        return "\n".join(lines) + "\n"

    campaigns = campaign_truth.get("campaigns", [])
    if not campaigns:
        lines.append("_No campaigns found in analysis output._\n")
        return "\n".join(lines) + "\n"

    lines.append(
        "| Campaign | Spend (30d) | Leads | Confirmed SQLs | Junk Rate | CPQL | Verdict | Reason |"
    )
    lines.append(
        "|----------|------------|-------|---------------|-----------|------|---------|--------|"
    )

    for c in campaigns:
        spend = _fmt_usd(c.get("spend_30d_usd"))
        leads = _fmt_int(c.get("total_leads"))
        sqls = _fmt_int(c.get("confirmed_sqls"))
        junk_rate = _fmt_pct(c.get("junk_rate_pct"))
        # CPQL must show N/A when SQL = 0
        cpql_raw = c.get("cpql_usd")
        cpql = _fmt_usd(cpql_raw) if (c.get("confirmed_sqls") or 0) > 0 and cpql_raw else "N/A"
        verdict = _verdict_label(c.get("verdict", ""))
        reason = c.get("verdict_reason", "—")
        campaign = c.get("campaign", "unknown")
        lines.append(f"| {campaign} | {spend} | {leads} | {sqls} | {junk_rate} | {cpql} | {verdict} | {reason} |")

    # Warnings
    warnings_found = [
        (c.get("campaign", ""), c.get("warnings", []))
        for c in campaigns
        if c.get("warnings")
    ]
    if warnings_found:
        lines.append("\n**Warnings:**")
        for campaign, warnings in warnings_found:
            for w in warnings:
                lines.append(f"- **{campaign}**: {w}")

    lines.append(
        "\n> Google Ads conversion counts are not used as indicators of business "
        "performance here. SQL counts are sourced from HubSpot MQL status only."
    )

    return "\n".join(lines) + "\n"


def _build_lead_quality_breakdown(lead_quality) -> str:
    lines = ["## 3. Lead Quality Breakdown\n"]

    if lead_quality is None:
        lines.append("_Lead quality data not available. Run analysis scripts first._\n")
        return "\n".join(lines) + "\n"

    campaigns = lead_quality.get("by_campaign", [])
    if not campaigns:
        lines.append("_No campaign lead data found._\n")
        return "\n".join(lines) + "\n"

    lines.append(
        "| Campaign | Total | Qualified (SQL) | In Progress | Junk | Wrong Fit | No Status | Junk Rate |"
    )
    lines.append(
        "|----------|-------|----------------|-------------|------|-----------|-----------|-----------|"
    )

    for c in campaigns:
        name = c.get("campaign", "unknown")
        total = _fmt_int(c.get("total"))
        qualified = _fmt_int(c.get("qualified"))
        in_progress = _fmt_int(c.get("in_progress"))
        junk = _fmt_int(c.get("junk"))
        wrong_fit = _fmt_int(c.get("wrong_fit"))
        no_status = _fmt_int(c.get("no_status"))
        junk_rate = _fmt_pct(c.get("junk_rate_pct"))
        lines.append(
            f"| {name} | {total} | {qualified} | {in_progress} | {junk} | {wrong_fit} | {no_status} | {junk_rate} |"
        )

    # Campaigns with small samples or no verdicts
    notes_found = False
    for c in campaigns:
        warnings = c.get("warnings", [])
        examples = c.get("qualified_examples", [])
        if warnings:
            if not notes_found:
                lines.append("\n**Data notes:**")
                notes_found = True
            for w in warnings:
                lines.append(f"- **{c['campaign']}**: {w}")

        if examples:
            keywords = [e.get("keyword", "") for e in examples if e.get("keyword")]
            if keywords:
                lines.append(
                    f"- **{c['campaign']}** — example SQL keywords: "
                    + ", ".join(f"`{k}`" for k in keywords[:3])
                )

    return "\n".join(lines) + "\n"


def _build_waste_detection_summary(waste, junk_patterns: dict) -> str:
    lines = ["## 4. Waste Detection Summary\n"]

    if waste is None:
        lines.append("_Waste detection data not available. Run analysis scripts first._\n")
        return "\n".join(lines) + "\n"

    data_source = waste.get("data_source", "unknown")
    total_spend = waste.get("total_spend_analysed", 0)
    confirmed_waste = waste.get("confirmed_waste_usd", 0)
    suspected_waste = waste.get("suspected_waste_usd", 0)

    lines.append(f"**Data source:** {data_source}")
    if waste.get("data_warning"):
        lines.append(f"\n> ⚠️ {waste['data_warning']}\n")

    lines.append(f"\n| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total spend analysed | {_fmt_usd(total_spend)} |")
    lines.append(f"| Confirmed waste | {_fmt_usd(confirmed_waste)} |")
    lines.append(f"| Suspected waste | {_fmt_usd(suspected_waste)} |")

    confirmed_items = waste.get("confirmed_waste_items", [])
    if confirmed_items:
        lines.append("\n**Top confirmed waste terms:**\n")
        lines.append("| Search Term | Campaign | Spend | Category | CRM Junk Count |")
        lines.append("|-------------|----------|-------|----------|----------------|")
        for item in confirmed_items[:10]:
            term = item.get("term", "—")
            campaign = item.get("campaign", "—")
            spend = _fmt_usd(item.get("spend_usd"))
            category = item.get("junk_category", "—")
            crm_junk = _fmt_int(item.get("crm_junk_confirmed"))
            # Look up category description from junk_patterns if available
            cat_desc = ""
            if category in junk_patterns and isinstance(junk_patterns[category], dict):
                cat_desc = junk_patterns[category].get("description", "")
            display_cat = f"{category}" + (f" ({cat_desc})" if cat_desc else "")
            lines.append(f"| `{term}` | {campaign} | {spend} | {display_cat} | {crm_junk} |")

    suspected_items = waste.get("suspected_waste_items", [])
    if suspected_items:
        lines.append(f"\n**Top suspected waste terms** ({len(suspected_items)} total shown):")
        lines.append("\n| Search Term | Spend | Category |")
        lines.append("|-------------|-------|----------|")
        for item in suspected_items[:5]:
            term = item.get("term", "—")
            spend = _fmt_usd(item.get("spend_usd"))
            category = item.get("junk_category", "—")
            lines.append(f"| `{term}` | {spend} | {category} |")

    return "\n".join(lines) + "\n"


def _build_data_gaps(waste, lead_quality, campaign_truth) -> str:
    lines = ["## 5. Data Gaps / Uncertainty\n"]

    gaps = []

    if waste is None:
        gaps.append("Waste detection output missing — `outputs/waste_report.json` not found.")
    elif waste.get("data_source") == "keywords_fallback":
        gaps.append(
            "Search term data unavailable from Windsor. Waste analysis ran on keyword-level "
            "data only. True waste figures may be 20–40% higher than shown."
        )

    if lead_quality is None:
        gaps.append("Lead quality output missing — `outputs/lead_quality.json` not found.")
    else:
        for c in lead_quality.get("by_campaign", []):
            for w in c.get("warnings", []):
                gaps.append(f"Campaign `{c['campaign']}`: {w}")

    if campaign_truth is None:
        gaps.append("Campaign truth output missing — `outputs/campaign_truth.json` not found.")
    else:
        for c in campaign_truth.get("campaigns", []):
            if (c.get("confirmed_sqls") or 0) == 0 and (c.get("spend_30d_usd") or 0) > 0:
                gaps.append(
                    f"Campaign `{c['campaign']}` has spend of "
                    f"{_fmt_usd(c.get('spend_30d_usd'))} with 0 confirmed SQLs — "
                    "outcome is uncertain until MDR verdicts are available."
                )

    if not gaps:
        gaps.append(
            "No significant data gaps detected. Analysis ran with full data availability."
        )

    for g in gaps:
        lines.append(f"- {g}")

    return "\n".join(lines) + "\n"


def _build_human_review_items(waste, lead_quality, campaign_truth) -> str:
    lines = ["## 6. Human Review Items\n"]
    items = []

    if campaign_truth:
        for c in campaign_truth.get("campaigns", []):
            verdict = c.get("verdict", "")
            campaign = c.get("campaign", "unknown")
            reason = c.get("verdict_reason", "")
            spend = c.get("spend_30d_usd", 0)

            if verdict == "FIX":
                items.append(
                    f"Campaign **{campaign}** warrants review — verdict FIX: {reason} "
                    f"(spend: {_fmt_usd(spend)})"
                )
            elif verdict == "CUT":
                items.append(
                    f"Campaign **{campaign}** suggests attention — verdict CUT: {reason} "
                    f"(spend: {_fmt_usd(spend)})"
                )

    if waste:
        confirmed = waste.get("confirmed_waste_items", [])
        if confirmed:
            total_confirmed = waste.get("confirmed_waste_usd", 0)
            if total_confirmed and total_confirmed > 50:
                items.append(
                    f"Confirmed waste of {_fmt_usd(total_confirmed)} warrants review. "
                    "Top waste terms listed in Section 4."
                )

    if lead_quality:
        for c in lead_quality.get("by_campaign", []):
            junk_rate = c.get("junk_rate_pct")
            if junk_rate is not None and junk_rate >= 25:
                items.append(
                    f"Campaign **{c['campaign']}** has a junk rate of {_fmt_pct(junk_rate)} — "
                    "warrants MDR review of lead qualification process."
                )

    if not items:
        lines.append(
            "No immediate human review items identified. "
            "All campaigns are within expected parameters."
        )
    else:
        for item in items:
            lines.append(f"- {item}")

    lines.append(
        "\n> All items above require human review before any action is taken. "
        "This system is read-only and does not take actions automatically."
    )

    return "\n".join(lines) + "\n"


def _build_phase1_reminder() -> str:
    return (
        "## 7. Phase 1 Read-Only Reminder\n\n"
        "This report was generated automatically from structured analysis data. "
        "**No actions have been taken automatically.**\n\n"
        "- No Google Ads campaigns have been paused, modified, or created.\n"
        "- No HubSpot contacts or deals have been modified.\n"
        "- No negative keywords have been added.\n"
        "- All findings above require human review and approval before any action.\n"
        "- Verdicts of FIX or CUT are suggestions for human consideration, "
        "not automatic instructions.\n\n"
        "> ⚖️ **Phase 1 Doctrine**: AI explains, humans decide. "
        "This system is in read-only validation mode.\n"
    )


# ── Main report generator ─────────────────────────────────────────────────────

def generate_deterministic_report(report_type: str = "weekly") -> str | None:
    """
    Generate a deterministic markdown report from structured analysis outputs.

    Args:
        report_type: "weekly" or "monthly"

    Returns:
        Path to the generated report file, or None if no analysis data found.
    """
    if report_type not in ("weekly", "monthly"):
        raise ValueError(f"report_type must be 'weekly' or 'monthly', got {report_type!r}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load analysis outputs
    waste = _load_json(f"{OUTPUT_DIR}/waste_report.json")
    lead_quality = _load_json(f"{OUTPUT_DIR}/lead_quality.json")
    campaign_truth = _load_json(f"{OUTPUT_DIR}/campaign_truth.json")

    if not any([waste, lead_quality, campaign_truth]):
        print("No analysis outputs found. Run analysis scripts first.")
        return None

    # Load config
    junk_patterns = _load_junk_patterns()

    # Determine output path
    now = datetime.now(tz=timezone.utc)
    if report_type == "weekly":
        date_str = now.strftime("%Y-%m-%d")
        report_path = f"{OUTPUT_DIR}/weekly_report_{date_str}.md"
        title = "Logistaas Weekly Ads Intelligence Report"
    else:
        month_str = now.strftime("%Y-%m")
        report_path = f"{OUTPUT_DIR}/monthly_report_{month_str}.md"
        title = "Logistaas Monthly Ads Intelligence Report"

    # Build report
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")
    data_notes = _data_availability(waste, lead_quality, campaign_truth)

    sections = [
        f"# {title}\n",
        f"**Generated:** {generated_at}  \n"
        f"**Report type:** {report_type}  \n"
        f"**Advisor mode:** deterministic (no external AI model)\n\n"
        "---\n",
        _build_executive_summary(waste, lead_quality, campaign_truth, report_type),
        "---\n",
        _build_campaign_truth_table(campaign_truth),
        "---\n",
        _build_lead_quality_breakdown(lead_quality),
        "---\n",
        _build_waste_detection_summary(waste, junk_patterns),
        "---\n",
        _build_data_gaps(waste, lead_quality, campaign_truth),
        "---\n",
        _build_human_review_items(waste, lead_quality, campaign_truth),
        "---\n",
        _build_phase1_reminder(),
    ]

    report_text = "\n".join(sections)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(f"Deterministic {report_type} report saved: {report_path}")
    for note in data_notes:
        print(f"  Data note: {note}")

    return report_path


if __name__ == "__main__":
    import sys
    rt = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    result = generate_deterministic_report(rt)
    if result:
        print(f"Report generated: {result}")
    else:
        print("No report generated — missing analysis data.")
        sys.exit(1)
