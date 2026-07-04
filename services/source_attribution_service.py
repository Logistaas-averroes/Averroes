"""
Revenue by Acquisition Source service (PR-ADS-117)

Explains pipeline and closed-won revenue across Google Ads, Other Paid, Organic,
Offline, and Unclassified / Needs Review — from one durable, auditable source of
truth. Google Ads is the ONLY group with connected spend, so the ONLY group that
may show ROAS; every other group is revenue-only (ROAS unavailable, never $0 /
0.00x). Deal revenue is never split across sources, and each deal is counted once.

Window doctrine (PR-ADS-116): Leads / SQLs use contact_created_at; Won Revenue
uses deal_close_date; both use the selected business window.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from analysis.business_windows import resolve_window
from analysis.source_classification import (
    GROUP_GOOGLE_ADS, GROUP_OTHER_PAID, GROUP_ORGANIC, GROUP_OFFLINE,
    GROUP_UNCLASSIFIED, GROUP_LABELS, GROUPS_WITH_SPEND, RULE_VERSION,
    CHANNEL_LABELS, PLATFORM_LABELS, CH_UNSPECIFIED, PF_UNSPECIFIED,
    classify_source, classify_source_taxonomy, attribute_deal,
)
from db import revenue_repository as repo

log = logging.getLogger(__name__)

# Display order of the page sections.
SECTION_ORDER = [GROUP_GOOGLE_ADS, GROUP_OTHER_PAID, GROUP_ORGANIC,
                 GROUP_OFFLINE, GROUP_UNCLASSIFIED]

# Per-platform status copy (PR-ADS-133). Only Google Ads is spend-connected /
# ROAS-eligible; every other source is revenue-only. Offline and Unclassified get
# their own honest labels — never a fabricated $0 / 0.00x ROAS.
STATUS_ROAS_AVAILABLE = "ROAS available"
STATUS_REVENUE_ONLY = "Revenue-only — no connected spend source"
STATUS_OFFLINE = "Imported CRM records — no reliable source attribution"
STATUS_NEEDS_REVIEW = "Needs review — source missing or unsafe"


def _platform_status(group: str) -> str:
    if group in GROUPS_WITH_SPEND:
        return STATUS_ROAS_AVAILABLE
    if group == GROUP_OFFLINE:
        return STATUS_OFFLINE
    if group == GROUP_UNCLASSIFIED:
        return STATUS_NEEDS_REVIEW
    return STATUS_REVENUE_ONLY


def _taxonomy_for_section(section: str, primary_raw, detail_raw) -> tuple[str, str, str, str]:
    """Resolve (channel, channel_label, platform, platform_label) for a row whose
    authoritative GROUP is ``section`` (from the durable acquisition_group).

    The channel/platform are derived from the raw HubSpot source fields, but only
    when the derived group agrees with the row's stored section. When the raw
    fields are missing or the deal's stored group is ambiguous/unclassified (so it
    folds into a different section than its primary contact's raw source), the row
    lands in the section's explicit "Unspecified" channel — never mis-attributed
    to a real platform.
    """
    tax = classify_source_taxonomy(primary_raw, detail_raw)
    if tax["source_group"] == section:
        return (tax["source_channel"], tax["source_channel_label"],
                tax["source_platform"], tax["source_platform_label"])
    return (CH_UNSPECIFIED, CHANNEL_LABELS[CH_UNSPECIFIED],
            PF_UNSPECIFIED, PLATFORM_LABELS[PF_UNSPECIFIED])


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _nullable_float(v):
    """Coerce to float, or None when missing/invalid — never fabricates a 0.0."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _window_bounds(window: str, now):
    resolved = resolve_window(window, now=now)
    start = date.fromisoformat(resolved["start_date"]) if resolved["start_date"] else None
    end = date.fromisoformat(resolved["end_date"])
    return resolved, start, end


def _section_bucket(acquisition_group: str) -> str:
    """Map a stored group to its display section. Ambiguous and unclassified deal
    revenue fold into the Unclassified / Needs Review section (counted once)."""
    if acquisition_group in (GROUP_GOOGLE_ADS, GROUP_OTHER_PAID, GROUP_ORGANIC, GROUP_OFFLINE):
        return acquisition_group
    return GROUP_UNCLASSIFIED


def _finalize_channels(group: str, channels: dict, group_spend, group_roas) -> list:
    """Shape the nested channel/platform dicts into sorted list rows (PR-ADS-133).

    Only the Google Ads group carries spend/ROAS; it has exactly one real
    channel/platform (Paid Search → Google Ads) so the group spend/ROAS attaches
    to it. Every other platform is revenue-only: spend/ROAS stay None (never a
    fabricated $0 / 0.00x) and get an honest status label.
    """
    has_spend = group in GROUPS_WITH_SPEND
    out = []
    for ch_key, ch in channels.items():
        platforms = []
        for pf_key, pf in ch["platforms"].items():
            platforms.append({
                "platform": pf_key,
                "label": pf["label"],
                "leads": pf["leads"],
                "sqls": pf["sqls"],
                "customers": pf["customers"],
                "won_revenue": round(pf["won_revenue"], 2),
                "spend": group_spend if has_spend else None,
                "roas": group_roas if has_spend else None,
                "status": _platform_status(group),
            })
        platforms.sort(key=lambda p: (p["won_revenue"], p["leads"]), reverse=True)
        out.append({
            "channel": ch_key,
            "label": ch["label"],
            "leads": ch["leads"],
            "sqls": ch["sqls"],
            "customers": ch["customers"],
            "won_revenue": round(ch["won_revenue"], 2),
            "spend": group_spend if has_spend else None,
            "roas": group_roas if has_spend else None,
            "platforms": platforms,
        })
    out.sort(key=lambda c: (c["won_revenue"], c["leads"]), reverse=True)
    return out


def build_revenue_by_source(window: str, now: datetime | None = None) -> dict:
    """Build the Revenue by Source contract for a business window.

    Each group additionally carries a ``channels`` breakdown (PR-ADS-133), and
    each channel a ``platforms`` breakdown, derived at read time from the durable
    raw HubSpot source fields. Group totals stay authoritative (from
    acquisition_group) and every nested level sums back to them. Only Google Ads
    is spend-connected / ROAS-eligible.

    Raises ValueError for an unsupported window.
    """
    resolved, start, end = _window_bounds(window, now)

    leads = repo.fetch_source_leads(start, end)
    revenue = repo.fetch_source_revenue(start, end)
    spend = repo.fetch_campaign_country_spend(start, end)

    buckets = {g: {"leads": 0, "sqls": 0, "customers": 0, "won_revenue": 0.0}
               for g in SECTION_ORDER}
    # Nested channel → platform sub-buckets per group (PR-ADS-133). Group totals
    # remain authoritative from acquisition_group; these only ADD a breakdown and
    # always sum back to the group total by construction.
    nested = {g: {} for g in SECTION_ORDER}

    def _platform_bucket(section, primary_raw, detail_raw):
        ch, ch_label, pf, pf_label = _taxonomy_for_section(section, primary_raw, detail_raw)
        channel = nested[section].setdefault(
            ch, {"label": ch_label, "leads": 0, "sqls": 0, "customers": 0,
                 "won_revenue": 0.0, "platforms": {}})
        platform = channel["platforms"].setdefault(
            pf, {"label": pf_label, "leads": 0, "sqls": 0, "customers": 0, "won_revenue": 0.0})
        return channel, platform

    for row in (leads.get("rows") or []):
        g = _section_bucket(row.get("acquisition_group") or GROUP_UNCLASSIFIED)
        qualified = row.get("status_category") == "qualified"
        buckets[g]["leads"] += 1
        if qualified:
            buckets[g]["sqls"] += 1
        channel, platform = _platform_bucket(
            g, row.get("source_primary_raw"), row.get("source_detail_raw"))
        channel["leads"] += 1
        platform["leads"] += 1
        if qualified:
            channel["sqls"] += 1
            platform["sqls"] += 1

    for row in (revenue.get("rows") or []):
        g = _section_bucket(row.get("acquisition_group") or GROUP_UNCLASSIFIED)
        amount = _safe_float(row.get("deal_amount_usd"))
        buckets[g]["customers"] += 1
        buckets[g]["won_revenue"] += amount
        channel, platform = _platform_bucket(
            g, row.get("source_primary_raw"), row.get("source_detail_raw"))
        channel["customers"] += 1
        channel["won_revenue"] += amount
        platform["customers"] += 1
        platform["won_revenue"] += amount

    # Only Google Ads has a connected spend source.
    google_spend = round(sum(_safe_float(r.get("spend")) for r in (spend.get("rows") or [])), 2)

    groups = []
    for g in SECTION_ORDER:
        b = buckets[g]
        has_spend = g in GROUPS_WITH_SPEND
        won = round(b["won_revenue"], 2)
        group_spend = google_spend if has_spend else None
        # ROAS is available ONLY for Google Ads, and only with real spend.
        roas = (round(won / group_spend, 2) if has_spend and group_spend and group_spend > 0 else None)
        groups.append({
            "group": g,
            "label": GROUP_LABELS[g],
            "has_spend": has_spend,
            "spend": group_spend,
            "leads": b["leads"],
            "sqls": b["sqls"],
            "customers": b["customers"],
            "won_revenue": won,
            "roas": roas,
            # Non-Google groups have no connected spend source — ROAS unavailable.
            "roas_status": "available" if has_spend else "unavailable_no_spend_source",
            "channels": _finalize_channels(g, nested[g], group_spend, roas),
        })

    summary = source_attribution_health_counts()

    return {
        "window": resolved,
        "groups": groups,
        "summary": summary,
        "source_truth": "hubspot_original_source_classification",
        "google_ads_conversion_value_used": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _source_detail_row(r: dict, group: str, channel_label: str, platform_label: str) -> dict:
    """One 'client / deal behind this source platform' drawer row (PR-ADS-133).

    Only durably-stored fields are populated. Contact name, company id, deal name
    and HubSpot lifecycle are NOT persisted locally, so they are explicit None →
    the UI renders "Unavailable", never a fabricated value. A missing amount stays
    None (never a fake $0).
    """
    amount = _nullable_float(r.get("deal_amount_usd"))
    return {
        "company": r.get("company") or None,
        "company_id": None,                 # company record id is not stored
        "main_contact": None,               # contact name is not stored
        "contact_id": r.get("associated_contact_id") or None,
        "lifecycle_stage": None,            # HubSpot lifecyclestage is not persisted
        "status_category": r.get("status_category") or None,
        "deal": None,                       # deal name is not stored
        "deal_id": r.get("deal_id") or None,
        "amount": round(amount, 2) if amount is not None else None,
        "close_date": r.get("deal_close_date"),
        "source": channel_label,
        "source_drilldown_1": platform_label,
        "source_drilldown_2": None,         # hs_analytics_source_data_2 not persisted
        "campaign_source_label": r.get("campaign_name") or r.get("source_detail_raw") or None,
        "attribution_status": r.get("attribution_status") or None,
    }


def build_source_platform_detail(window: str, source_group: str, source_channel: str,
                                 source_platform: str, now: datetime | None = None) -> dict:
    """Closed-won client/deal detail rows behind ONE source platform (PR-ADS-133).

    Read-only lazy drilldown for the Revenue by Source drawer. Fetches closed-won
    deals in the window (deal_source_attribution, windowed by deal_close_date),
    derives each deal's group/channel/platform with the SAME taxonomy the page
    rows use, and returns the deals whose (group, channel, platform) match the
    request. Never writes anything; never fabricates ids, names or amounts.

    Raises ValueError for an unsupported window.
    """
    resolved, start, end = _window_bounds(window, now)
    generated_at = datetime.now(timezone.utc).isoformat()

    fetched = repo.fetch_source_deal_details(start, end)
    if not fetched.get("available"):
        return {
            "window": resolved, "source_group": source_group,
            "source_channel": source_channel, "source_platform": source_platform,
            "rows": [], "source_health": {"status": "database_unavailable"},
            "generated_at": generated_at,
        }

    rows = []
    for r in (fetched.get("rows") or []):
        section = _section_bucket(r.get("acquisition_group") or GROUP_UNCLASSIFIED)
        ch, ch_label, pf, pf_label = _taxonomy_for_section(
            section, r.get("source_primary_raw"), r.get("source_detail_raw"))
        if section == source_group and ch == source_channel and pf == source_platform:
            rows.append(_source_detail_row(r, section, ch_label, pf_label))

    return {
        "window": resolved,
        "source_group": source_group,
        "source_channel": source_channel,
        "source_platform": source_platform,
        "rows": rows,
        "source_health": {"status": "ready"},
        "generated_at": generated_at,
    }


def source_attribution_health_counts() -> dict:
    """Durable classification/attribution counts for the source-health summary."""
    import db.writers as db_writers  # noqa: PLC0415
    return db_writers.source_attribution_health_counts()


def build_source_attribution_health() -> dict:
    return {
        "summary": source_attribution_health_counts(),
        "classification_rule_version": RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Classification helpers (pure, shared with the backfill) ──────────────────


def classify_contact_row(contact: dict) -> dict:
    """Classify one HubSpot contact dict into a durable classification row.

    primary = Original Source (hs_analytics_source); detail = Original Source
    Drill-Down (hs_analytics_source_data_1). Raw values are preserved.
    """
    props = contact.get("properties") or contact
    primary = props.get("hs_analytics_source")
    detail = props.get("hs_analytics_source_data_1")
    contact_id = contact.get("id") or contact.get("contact_id")
    key = (str(contact_id).strip() if contact_id else "") or f"id:{contact.get('row_id', '')}"
    from db.writers import _map_status_category  # noqa: PLC0415
    return {
        "contact_key": key,
        "contact_id": str(contact_id) if contact_id else None,
        "source_primary_raw": primary,
        "source_detail_raw": detail,
        "acquisition_group": classify_source(primary, detail),
        "classification_rule_version": RULE_VERSION,
        "contact_created_at": props.get("createdate"),
        "status_category": _map_status_category(props.get("mql_status")),
    }


def attribute_deal_row(deal: dict) -> dict:
    """Build a durable deal-source-attribution row from a deal with its contacts.

    deal: {deal_id, deal_close_date, deal_amount_usd,
           contacts:[{contact_id, source_primary, source_detail}]}.
    Revenue is never split; the deal maps to one group / ambiguous / unclassified.
    """
    contacts = deal.get("contacts") or []
    groups = [classify_source(c.get("source_primary"), c.get("source_detail")) for c in contacts]
    decision = attribute_deal(groups)
    primary_contact = contacts[0] if contacts else {}
    return {
        "deal_id": deal.get("deal_id"),
        "associated_contact_id": primary_contact.get("contact_id"),
        "acquisition_group": decision["acquisition_group"],
        "source_primary_raw": primary_contact.get("source_primary"),
        "source_detail_raw": primary_contact.get("source_detail"),
        "attribution_status": decision["attribution_status"],
        "attribution_reason": decision["attribution_reason"],
        "deal_close_date": deal.get("deal_close_date"),
        "deal_amount_usd": deal.get("deal_amount_usd"),
        "classification_rule_version": RULE_VERSION,
    }


# ── Durable, resumable historical backfill (reuses PR-ADS-114 job pattern) ────


def run_source_attribution_backfill(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    dry_run: bool = True,
    chunk_months: int = 1,
    resume: bool = False,
    job_id: str | None = None,
    progress: dict | None = None,
    checkpoint=None,
    load_completed=None,
) -> dict:
    """Classify all historical contacts and attribute all closed-won deals.

    Reads HubSpot read-only; writes ONLY the local classification tables (when
    not dry_run). Resumable via durable checkpoints. NEVER writes to HubSpot or
    Google Ads, and NEVER overwrites raw HubSpot data.
    """
    from connectors.hubspot_pull import (  # noqa: PLC0415
        pull_all_contacts_in_range,
        pull_closed_won_deals_with_sources_in_range,
    )
    import db.writers as db_writers  # noqa: PLC0415
    from dateutil.relativedelta import relativedelta  # noqa: PLC0415

    # Default range: All Time floor through today.
    end = date.fromisoformat(date_to) if date_to else datetime.now(tz=timezone.utc).date()
    start = date.fromisoformat(date_from) if date_from else date(2018, 1, 1)
    if start > end:
        raise ValueError("date_from must be before or equal to date_to")

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = progress if progress is not None else {}
    state.update({"running": True, "dry_run": dry_run, "phase": "starting",
                  "job_id": job_id, "started_at": started_at})

    if resume and load_completed is not None:
        try:
            completed = set(load_completed() or [])
        except Exception:  # noqa: BLE001
            completed = set()
    else:
        completed = set()

    summary = {"contacts_classified": 0, "deals_attributed": 0,
               "ambiguous_deals": 0, "unclassified_deals": 0, "failed": 0}
    chunks_detail: list = []
    errors: list = []

    def _emit(status):
        if checkpoint is None:
            return
        try:
            checkpoint(job_id, {
                "status": status, "phase": state.get("phase"),
                "current_chunk": state.get("current_chunk"),
                "completed_chunks": list(completed),
                "summary": dict(summary), "chunks": list(chunks_detail), "errors": list(errors),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("[source_backfill] checkpoint failed: %s", exc)

    cursor = start
    step = relativedelta(months=max(1, chunk_months))
    while cursor <= end:
        chunk_to = min(cursor + step - relativedelta(days=1), end)
        chunk_key = f"{cursor.isoformat()}:{chunk_to.isoformat()}"
        if resume and chunk_key in completed:
            cursor = chunk_to + relativedelta(days=1)
            continue
        state["current_chunk"] = chunk_key
        chunk = {"chunk": chunk_key, "contacts": 0, "deals": 0, "deal_lookups_failed": 0}

        # Contacts → classify. (Re-upserting on a retried chunk is idempotent.)
        state["phase"] = "contacts"
        contacts_ok = True
        try:
            contacts = pull_all_contacts_in_range(
                date_from=cursor.isoformat(), date_to=chunk_to.isoformat())
            rows = [classify_contact_row(c) for c in contacts]
            chunk["contacts"] = len(rows)
            summary["contacts_classified"] += len(rows)
            if not dry_run and rows:
                db_writers.upsert_contact_source_classification(rows)
        except Exception as exc:  # noqa: BLE001
            contacts_ok = False
            errors.append(f"contacts {chunk_key}: {exc}")
            summary["failed"] += 1

        # Closed-won deals → attribute. A deal whose HubSpot association/source
        # lookup failed is NOT turned into an Unclassified row — it is skipped
        # (existing durable attribution untouched), counted failed, and keeps the
        # chunk incomplete so resume retries it. Only a SUCCESSFUL zero-contact
        # lookup becomes Unclassified.
        state["phase"] = "deals"
        deals_ok = True
        try:
            deals = pull_closed_won_deals_with_sources_in_range(
                date_from=cursor.isoformat(), date_to=chunk_to.isoformat())
            drows = []
            failed_lookups = 0
            for d in deals:
                if d.get("lookup_failed"):
                    failed_lookups += 1
                    continue
                drows.append(attribute_deal_row(d))
            chunk["deals"] = len(drows)
            chunk["deal_lookups_failed"] = failed_lookups
            for dr in drows:
                st = dr["attribution_status"]
                if st == "attributed":
                    summary["deals_attributed"] += 1
                elif st == "ambiguous":
                    summary["ambiguous_deals"] += 1
                else:
                    summary["unclassified_deals"] += 1
            if not dry_run and drows:
                db_writers.upsert_deal_source_attribution(drows)
            if failed_lookups:
                deals_ok = False
                summary["failed"] += failed_lookups
                errors.append(
                    f"deals {chunk_key}: {failed_lookups} deal source lookup(s) failed (retryable)")
        except Exception as exc:  # noqa: BLE001
            deals_ok = False
            errors.append(f"deals {chunk_key}: {exc}")
            summary["failed"] += 1

        chunks_detail.append(chunk)
        # Only a fully-successful chunk is recorded complete; an incomplete chunk
        # is retried on resume.
        if contacts_ok and deals_ok:
            completed.add(chunk_key)
        state["completed_chunks"] = list(completed)
        _emit("running")
        cursor = chunk_to + relativedelta(days=1)

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = "failed" if (errors and not chunks_detail) else ("partial" if errors else "success")
    result = {
        "status": status, "dry_run": dry_run,
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "chunk_months": chunk_months, "started_at": started_at, "finished_at": finished_at,
        "summary": summary, "chunks": chunks_detail, "errors": errors,
    }
    state.update({"running": False, "phase": "done", "finished_at": finished_at, "latest": result})
    if checkpoint is not None:
        state["phase"] = "done"
        checkpoint(job_id, {
            "status": status, "phase": "done", "current_chunk": None,
            "completed_chunks": list(completed), "summary": dict(summary),
            "chunks": list(chunks_detail), "errors": list(errors), "finished_at": finished_at,
        })
    return result
