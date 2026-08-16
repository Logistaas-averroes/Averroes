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
A failed or partial sync is reported AS failed/partial. It must never look like
a successful zero-row result — that is how a silent revenue gap starts. This
covers PERSISTENCE too: a database write that fails is a failed sync, not a
sync that happened to write nothing.

The watermark advances only over deals that were fully processed AND committed.
A capped run checkpoints at the end of that clean prefix so the next run resumes
after it, instead of reprocessing the first page forever.
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
# sync (disclosed), never a silently truncated success — and it CHECKPOINTS at
# the last fully committed deal so the next run resumes there. Deals arrive in
# ascending hs_lastmodifieddate order, which is what makes that prefix safe.
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
    """``{CURRENCY: {iso_date: rate}}`` from the LOCAL fx_rates table.

    Handed to ``resolve_deal_currency`` whole, so that module can look up the
    rate map for the currency an amount is actually in. The service deliberately
    does not pre-select a map: doing so is what let a GBP amount be converted at
    the home currency's rate when GBP rates were missing.

    Never fetches externally.
    """
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
               full_refresh: bool = False, bootstrap: bool = False) -> dict:
    """Synchronize deals into the canonical ledger.

    Args:
        modified_since: ISO timestamp; ``None`` uses the stored watermark (minus
            the overlap). Ignored when ``full_refresh``.
        full_refresh: read every tracked deal regardless of watermark.
        bootstrap: this run is a HISTORICAL BOOTSTRAP pass. Recorded as such in
            the coverage state, whether it succeeds, partials or fails —
            including on the early pull-failure paths, so a failed bootstrap can
            never be filed as a failed incremental (or vice versa).

    Returns ``{available, status, sync_mode, deals_seen, written, skipped_stale,
    association_failures, write_failures, pages, complete, watermark,
    watermark_is_checkpoint, error}`` where ``status`` is ``success`` |
    ``partial`` | ``failed``.
    """
    import connectors.hubspot_pull as hubspot  # noqa: PLC0415
    import db.deal_ledger_repository as ledger_repo  # noqa: PLC0415

    # Declared once, here, and passed to EVERY state write on every exit path.
    sync_mode = (ledger_repo.SYNC_MODE_BOOTSTRAP if bootstrap
                 else ledger_repo.SYNC_MODE_INCREMENTAL)

    result = {"available": True, "status": "failed", "sync_mode": sync_mode,
              "deals_seen": 0,
              "written": 0, "skipped_stale": 0, "association_failures": 0,
              "write_failures": 0, "pages": 0, "complete": False,
              "watermark": None, "watermark_is_checkpoint": False,
              "error": None}

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
        ledger_repo.record_sync_state(status="failed", sync_mode=sync_mode,
                                      error=result["error"], batch_id=batch_id)
        return result

    raw_deals = pull.get("deals") or []
    result["pages"] = pull.get("pages") or 0
    result["complete"] = bool(pull.get("complete"))
    result["deals_seen"] = len(raw_deals)
    if not pull.get("available"):
        result["error"] = pull.get("error") or "pull_unavailable"
        ledger_repo.record_sync_state(status="failed", sync_mode=sync_mode,
                                      error=result["error"], batch_id=batch_id)
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
    write_failures = 0
    written = 0
    skipped_stale = 0
    watermark = None
    # The end of the contiguous prefix of deals that were fully resolved AND
    # committed. Safe to resume from precisely because the read is sorted
    # ascending by hs_lastmodifieddate.
    checkpoint = None
    prefix_clean = True
    truncated = False
    first_write_error = None

    for deal in normalized:
        deal_id = deal["deal_id"]

        # The cap ends this run; it does not turn the remaining deals into
        # failed lookups. Writing `lookup_failed` rows for deals we never even
        # attempted would manufacture evidence of a failure that never happened.
        if lookups >= max_association_lookups:
            truncated = True
            break

        # ── Associations ────────────────────────────────────────────────────
        lookup_failed = False
        contacts: list = []
        lookups += 1
        try:
            assoc = hubspot.fetch_deal_associations(deal_id)
            raw_assocs = assoc.get("contacts") or []
            if raw_assocs:
                contact_ids = [str(a["contact_id"]) for a in raw_assocs]
                try:
                    # DEDICATED attribution reader. `pull_contacts_by_ids` is the
                    # PR-ADS-115 lead-date reader and returns createdate strings;
                    # its contract is not this one.
                    by_id = hubspot.pull_contact_attribution_properties(
                        contact_ids) or {}
                except Exception as exc:  # noqa: BLE001
                    log.warning("[deal-sync] contact attribution read failed "
                                "for %s: %s", deal_id, exc)
                    lookup_failed = True
                    by_id = {}
                missing = [c for c in contact_ids if c not in by_id]
                if missing and not lookup_failed:
                    # An incomplete batch is a lookup failure, not a set of
                    # contacts that happen to have no source.
                    log.warning("[deal-sync] contact attribution incomplete for "
                                "%s: %d of %d contact(s) missing", deal_id,
                                len(missing), len(contact_ids))
                    lookup_failed = True
                if not lookup_failed:
                    contacts = [
                        _contact_evidence(
                            (by_id.get(str(a["contact_id"])) or {}).get(
                                "properties") or {},
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
        # The WHOLE rate table is passed through. The currency module selects the
        # amount source and its matching rate map together, so a missing GBP rate
        # can never be substituted with the home currency's.
        currency = resolve_deal_currency(
            amount_raw=deal.get("amount_raw"),
            deal_currency_code=deal.get("deal_currency_code"),
            amount_in_home_currency=deal.get("amount_in_home_currency"),
            home_currency_code=home.get("home_currency_code"),
            home_currency_verified=bool(home.get("verified")),
            close_date_iso=_close_date_iso(deal.get("deal_close_date")),
            fx_rates_by_currency=fx_by_currency,
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

        # A persistence failure is a SYNC failure. Counting it as zero rows
        # written and moving on is how a run reports success while the ledger
        # silently loses a deal.
        write_ok = bool(write.get("available"))
        if not write_ok:
            write_failures += 1
            if first_write_error is None:
                first_write_error = (write.get("error") or write.get("reason")
                                     or "ledger_write_failed")
            log.error("[deal-sync] ledger write failed for %s: %s",
                      deal_id, first_write_error)
        written += int(write.get("written") or 0)
        skipped_stale += int(write.get("skipped_stale") or 0)

        modified = deal.get("hubspot_lastmodified_at")
        if modified and (watermark is None or str(modified) > str(watermark)):
            watermark = modified

        # The resume checkpoint only extends while EVERY deal so far was fully
        # resolved and committed. Once one is not, the prefix is closed: moving
        # past a deal whose associations we never read would strand it.
        if write_ok and not lookup_failed and prefix_clean:
            if modified and (checkpoint is None or str(modified) > str(checkpoint)):
                checkpoint = modified
        elif not (write_ok and not lookup_failed):
            prefix_clean = False

    complete = bool(pull.get("complete")) and not truncated
    status = "success" if (complete and association_failures == 0
                           and write_failures == 0) else "partial"
    if not pull.get("complete"):
        status = "partial"
    # Nothing persisted at all: the run learned nothing durable, so it is failed
    # rather than partially successful.
    if write_failures and written == 0 and skipped_stale == 0:
        status = "failed"

    errors = [e for e in (
        pull.get("error"),
        "association_lookup_cap_reached" if truncated else None,
        (f"ledger_write_failed:{first_write_error}" if write_failures else None),
    ) if e]

    # On success the watermark is the run's maximum. Otherwise it may only
    # advance to the clean prefix — and only when the run was cut short cleanly
    # rather than by a failure.
    if status == "success":
        state_watermark, is_checkpoint = watermark, False
    elif status == "partial" and checkpoint is not None:
        state_watermark, is_checkpoint = checkpoint, True
    else:
        state_watermark, is_checkpoint = None, False

    result.update({
        "status": status, "written": written, "skipped_stale": skipped_stale,
        "association_failures": association_failures,
        "write_failures": write_failures, "complete": complete,
        "watermark": state_watermark, "watermark_is_checkpoint": is_checkpoint,
        "error": "; ".join(errors) if errors else None,
    })

    state = ledger_repo.record_sync_state(
        status=status, sync_mode=sync_mode, watermark=state_watermark,
        watermark_is_checkpoint=is_checkpoint, deals_seen=len(normalized),
        pages_fetched=result["pages"], association_failures=association_failures,
        error=result["error"], batch_id=batch_id,
        # `complete` is the connector's proof that pagination reached the END of
        # the result set, AND that this run was not truncated by the lookup cap.
        # Only that may complete the bootstrap.
        proved_complete=complete)
    if not state.get("available"):
        # Coverage we could not record is coverage we cannot claim.
        result["status"] = "failed"
        result["error"] = "; ".join(
            errors + [f"sync_state_write_failed:{state.get('error') or state.get('reason')}"])

    log.info("[deal-sync] status=%s seen=%d written=%d stale_skipped=%d "
             "assoc_failures=%d write_failures=%d", result["status"],
             len(normalized), written, skipped_stale, association_failures,
             write_failures)
    return result


def backfill_deals(*, batch_id=None, restart: bool = False,
                   max_association_lookups: int = DEFAULT_MAX_ASSOCIATION_LOOKUPS
                   ) -> dict:
    """Resumable historical backfill over every deal state.

    RESUMES by default. A pass that hits the association cap checkpoints at its
    last committed deal, so the next call picks up from there instead of paging
    through the same first 5,000 deals on every attempt — which is what made the
    previous implementation unable to finish a large portal at all.

    ``restart=True`` ignores the checkpoint and re-reads everything. It is an
    explicit operator choice — slower and deliberate — and is never selected
    automatically, least of all in response to an error.

    ALWAYS records ``sync_mode="bootstrap"``, so a bootstrap pass never stamps
    ``last_incremental_at`` and can never be mistaken for the post-bootstrap
    incremental the cutover gate requires.

    Reads HubSpot read-only and writes only local PostgreSQL.
    """
    return sync_deals(full_refresh=restart, bootstrap=True, batch_id=batch_id,
                      max_association_lookups=max_association_lookups)


__all__ = [
    "WATERMARK_OVERLAP_MINUTES", "DEFAULT_MAX_ASSOCIATION_LOOKUPS",
    "sync_deals", "backfill_deals",
]
