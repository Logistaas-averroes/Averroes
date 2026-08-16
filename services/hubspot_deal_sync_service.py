"""
services/hubspot_deal_sync_service.py

PR-ADS-153E-A — orchestration for the canonical deal ledger.

Pipeline, in order:

    connector read (all stages, read-only)
      → normalize deal state (won = hs_is_closed_won, stages preserved)
      → resolve currency (fail-closed, local FX only)
      → resolve associations + attribution (ONE shared resolver)
      → write ledger + association bridge (idempotent, monotonic)
      → record coverage / completeness / failures / watermark

Every business rule lives in a pure module (``analysis.deal_truth``,
``analysis.deal_currency``, ``analysis.source_classification``). This service
only sequences them and persists the result — schedulers, in turn, only call
this service and hold no revenue logic of their own.

Governance
----------
Reads HubSpot read-only. Writes ONLY local PostgreSQL. No Google Ads call of any
kind, no HubSpot mutation, no Mailchimp. Nothing here is a production consumer:
in 153E-A the ledger is populated and reconciled but read by no page.

Failure posture
---------------
A failed or partial sync is reported AS failed/partial and does not advance the
watermark. It must never look like a successful zero-row result — that is how a
silent revenue gap starts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from analysis.deal_currency import resolve_deal_currency
from analysis.deal_truth import (
    ASSOC_LOOKUP_FAILED,
    parse_hubspot_bool,
    primary_contact_evidence,
    resolve_deal_associations,
)

log = logging.getLogger(__name__)

# Re-read a safety margin behind the watermark. HubSpot's last-modified index is
# eventually consistent, so a strict ">= watermark" would skip a deal modified in
# the same instant the previous run finished.
WATERMARK_OVERLAP_MINUTES = 15

# Cap the association lookups a single run performs, so one enormous backfill
# page cannot hold the scheduler indefinitely. Reaching the cap yields a PARTIAL
# sync (disclosed), never a silently truncated success.
DEFAULT_MAX_ASSOCIATION_LOOKUPS = 5000


def _iso_or_none(value):
    if value in (None, ""):
        return None
    return str(value)


def _close_date_iso(value) -> str | None:
    """The FX date for a deal: the calendar day of its close date."""
    if not value:
        return None
    text = str(value)
    if "T" in text:
        return text.split("T")[0]
    return text[:10] if len(text) >= 10 else None


def _normalize_deal(raw: dict, stage_map: dict) -> dict:
    """Flatten one HubSpot deal into ledger shape (no decisions yet)."""
    props = raw.get("properties") or {}
    stage = props.get("dealstage") or None
    return {
        "deal_id": str(raw.get("id")) if raw.get("id") is not None else None,
        "deal_name": props.get("dealname"),
        "pipeline_id": props.get("pipeline"),
        "deal_stage_id": stage,
        # Unknown stages stay explicitly unknown — never defaulted to a won label.
        "deal_stage_label": (stage_map.get(stage)
                             or (f"Unknown stage ({stage})" if stage
                                 else "Unknown stage")),
        "hs_is_closed": parse_hubspot_bool(props.get("hs_is_closed")),
        "hs_is_closed_won": parse_hubspot_bool(props.get("hs_is_closed_won")),
        "deal_created_at": _iso_or_none(props.get("createdate")),
        "deal_close_date": _iso_or_none(props.get("closedate")),
        "hubspot_lastmodified_at": _iso_or_none(props.get("hs_lastmodifieddate")),
        "amount_raw": props.get("amount"),
        "deal_currency_code": props.get("deal_currency_code"),
        "amount_in_home_currency": props.get("amount_in_home_currency"),
    }


def _contact_evidence(contact_props: dict, association: dict) -> dict:
    """Attribution evidence for one associated contact.

    Mirrors the existing contact-source doctrine so the ledger cannot disagree
    with `deal_source_attribution` about what a contact's source is.
    """
    from analysis.source_classification import classify_source  # noqa: PLC0415
    from connectors.gclid_match import _extract_gclid_from_url  # noqa: PLC0415

    props = contact_props or {}
    direct = (props.get("hs_google_click_id") or "").strip()
    gclid = direct or _extract_gclid_from_url(props.get("hs_analytics_first_url") or "")
    primary = props.get("hs_analytics_source")
    detail = props.get("hs_analytics_source_data_1")
    return {
        "contact_id": str(association.get("contact_id")),
        "association_type_id": association.get("association_type_id"),
        "association_label": association.get("association_label"),
        "gclid": gclid or None,
        "campaign_name_raw": detail or None,
        "keyword_raw": props.get("hs_analytics_source_data_2") or None,
        # Same precedence the rest of the product uses: ip_country then country.
        "country_raw": props.get("ip_country") or props.get("country") or None,
        "source_primary_raw": primary,
        "source_detail_raw": detail,
        "acquisition_group": classify_source(primary, detail),
    }


def _fx_rates_for(currencies: set, start_iso: str | None, end_iso: str | None) -> dict:
    """Local close-date FX rates per currency. Never fetches externally."""
    from datetime import date as _date

    import db.revenue_repository as revenue_repo  # noqa: PLC0415

    out: dict = {}
    if not currencies or not end_iso:
        return out
    try:
        start = _date.fromisoformat(start_iso) if start_iso else None
        end = _date.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return out
    for ccy in currencies:
        if not ccy or ccy.upper() == "USD":
            continue
        try:
            res = revenue_repo.fetch_fx_rates(start, end, ccy.upper(), "USD")
            if res.get("available"):
                out[ccy.upper()] = res.get("rates") or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("FX rates unavailable for %s: %s", ccy, exc)
    return out


def sync_deals(*, modified_since=None, stages=None, batch_id=None,
               max_association_lookups: int = DEFAULT_MAX_ASSOCIATION_LOOKUPS,
               full_refresh: bool = False) -> dict:
    """Synchronize deals into the canonical ledger.

    Args:
        modified_since: ISO timestamp; ``None`` uses the stored watermark (minus
            the overlap). Ignored when ``full_refresh``.
        full_refresh: read every tracked deal regardless of watermark.

    Returns ``{available, status, deals_seen, written, skipped_stale,
    association_failures, pages, complete, watermark, error}`` where ``status``
    is ``success`` | ``partial`` | ``failed``.
    """
    import connectors.hubspot_pull as hubspot  # noqa: PLC0415
    import db.deal_ledger_repository as ledger_repo  # noqa: PLC0415

    result = {"available": True, "status": "failed", "deals_seen": 0,
              "written": 0, "skipped_stale": 0, "association_failures": 0,
              "pages": 0, "complete": False, "watermark": None, "error": None}

    # ── Resolve the incremental window ──────────────────────────────────────
    since_ms = None
    if not full_refresh:
        since = modified_since
        if since is None:
            state = ledger_repo.fetch_sync_state()
            since = ((state.get("row") or {}) or {}).get("last_modified_watermark")
        if since:
            try:
                parsed = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                parsed -= timedelta(minutes=WATERMARK_OVERLAP_MINUTES)
                since_ms = int(parsed.timestamp() * 1000)
            except (TypeError, ValueError):
                since_ms = None

    # ── Read ────────────────────────────────────────────────────────────────
    try:
        pull = hubspot.pull_deals_for_ledger(modified_since_ms=since_ms,
                                             stages=stages)
    except Exception as exc:  # noqa: BLE001
        log.error("[deal-sync] deal pull failed: %s", exc)
        result["error"] = f"pull_failed: {exc}"
        ledger_repo.record_sync_state(status="failed", error=result["error"],
                                      batch_id=batch_id)
        return result

    raw_deals = pull.get("deals") or []
    result["pages"] = pull.get("pages") or 0
    result["complete"] = bool(pull.get("complete"))
    result["deals_seen"] = len(raw_deals)
    if not pull.get("available"):
        result["error"] = pull.get("error") or "pull_unavailable"
        ledger_repo.record_sync_state(status="failed", error=result["error"],
                                      batch_id=batch_id)
        return result

    stage_map = getattr(hubspot, "DEAL_STAGE_MAP", {}) or {}
    home = {}
    try:
        home = hubspot.fetch_portal_home_currency() or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("[deal-sync] home currency lookup failed: %s", exc)

    normalized = [_normalize_deal(d, stage_map) for d in raw_deals]
    normalized = [d for d in normalized if d.get("deal_id")]

    # ── FX, fetched once per currency across the whole batch ────────────────
    close_dates = [_close_date_iso(d["deal_close_date"]) for d in normalized]
    close_dates = sorted({c for c in close_dates if c})
    currencies = {(d.get("deal_currency_code") or "").upper()
                  for d in normalized if d.get("deal_currency_code")}
    if home.get("verified") and home.get("home_currency_code"):
        currencies.add(str(home["home_currency_code"]).upper())
    fx_by_currency = _fx_rates_for(
        currencies,
        close_dates[0] if close_dates else None,
        close_dates[-1] if close_dates else None)

    lookups = 0
    association_failures = 0
    written = 0
    skipped_stale = 0
    watermark = None
    truncated = False

    for deal in normalized:
        deal_id = deal["deal_id"]

        # ── Associations ────────────────────────────────────────────────────
        lookup_failed = False
        contacts: list = []
        if lookups >= max_association_lookups:
            truncated = True
            lookup_failed = True
        else:
            lookups += 1
            try:
                assoc = hubspot.fetch_deal_associations(deal_id)
                raw_assocs = assoc.get("contacts") or []
                if raw_assocs:
                    contact_ids = [a["contact_id"] for a in raw_assocs]
                    props_by_id = {}
                    try:
                        props_by_id = hubspot.pull_contacts_by_ids(contact_ids) or {}
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[deal-sync] contact props failed for %s: %s",
                                    deal_id, exc)
                        lookup_failed = True
                    if not lookup_failed:
                        contacts = [
                            _contact_evidence(
                                (props_by_id.get(str(a["contact_id"])) or {}).get(
                                    "properties",
                                    props_by_id.get(str(a["contact_id"])) or {}),
                                a)
                            for a in raw_assocs
                        ]
            except Exception as exc:  # noqa: BLE001
                # Includes DealAssociationLookupError. A failure is NOT an empty
                # result — the ledger keeps its previous evidence.
                log.warning("[deal-sync] association lookup failed for %s: %s",
                            deal_id, exc)
                lookup_failed = True

        if lookup_failed:
            association_failures += 1

        resolution = resolve_deal_associations(contacts, lookup_failed=lookup_failed)
        evidence = primary_contact_evidence(contacts, resolution)

        # ── Currency ────────────────────────────────────────────────────────
        ccy = (deal.get("deal_currency_code") or "").upper() or None
        rates = fx_by_currency.get(ccy, {}) if ccy else {}
        if not rates and home.get("verified") and home.get("home_currency_code"):
            rates = fx_by_currency.get(str(home["home_currency_code"]).upper(), {})
        currency = resolve_deal_currency(
            amount_raw=deal.get("amount_raw"),
            deal_currency_code=deal.get("deal_currency_code"),
            amount_in_home_currency=deal.get("amount_in_home_currency"),
            home_currency_code=home.get("home_currency_code"),
            home_currency_verified=bool(home.get("verified")),
            close_date_iso=_close_date_iso(deal.get("deal_close_date")),
            fx_rates=rates,
        )

        row = {
            **deal,
            "home_currency_code": home.get("home_currency_code"),
            "revenue_usd": currency["revenue_usd"],
            "currency_status": currency["currency_status"],
            "currency_reason": currency["currency_reason"],
            "primary_contact_id": resolution["primary_contact_id"],
            "association_count": resolution["association_count"],
            "association_status": resolution["association_status"],
            "association_reason": resolution["association_reason"],
            **evidence,
            "attribution_status": resolution["attribution_status"],
            "attribution_reason": resolution["attribution_reason"],
            "sync_batch_id": batch_id,
            "source_fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        primary_id = resolution.get("primary_contact_id")
        for c in contacts:
            c["is_primary"] = str(c.get("contact_id")) == str(primary_id)
            c["primary_selection_reason"] = (
                resolution.get("association_reason") if c["is_primary"] else None)

        write = ledger_repo.upsert_deal(
            row, associations=contacts,
            # A failed lookup writes NO associations, preserving prior evidence.
            associations_observed=not lookup_failed)
        written += int(write.get("written") or 0)
        skipped_stale += int(write.get("skipped_stale") or 0)

        modified = deal.get("hubspot_lastmodified_at")
        if modified and (watermark is None or str(modified) > str(watermark)):
            watermark = modified

    complete = bool(pull.get("complete")) and not truncated
    status = "success" if complete and association_failures == 0 else "partial"
    if not pull.get("complete"):
        status = "partial"

    result.update({
        "status": status, "written": written, "skipped_stale": skipped_stale,
        "association_failures": association_failures, "complete": complete,
        "watermark": watermark,
        "error": pull.get("error") or ("association_lookup_cap_reached"
                                       if truncated else None),
    })

    ledger_repo.record_sync_state(
        status=status, watermark=watermark, deals_seen=len(normalized),
        pages_fetched=result["pages"], association_failures=association_failures,
        error=result["error"], batch_id=batch_id,
        bootstrap_status="complete" if (full_refresh and complete) else None)

    log.info("[deal-sync] status=%s seen=%d written=%d stale_skipped=%d "
             "assoc_failures=%d", status, len(normalized), written,
             skipped_stale, association_failures)
    return result


def backfill_deals(*, batch_id=None) -> dict:
    """Resumable historical backfill over every tracked deal state.

    Reads HubSpot read-only and writes only local PostgreSQL. Safe to re-run:
    the ledger upsert is idempotent by ``deal_id`` and monotonic, so a repeat
    pass cannot duplicate a deal or revert a newer state.
    """
    return sync_deals(full_refresh=True, batch_id=batch_id)


__all__ = [
    "WATERMARK_OVERLAP_MINUTES", "DEFAULT_MAX_ASSOCIATION_LOOKUPS",
    "sync_deals", "backfill_deals",
]
