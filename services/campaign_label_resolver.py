"""
The campaign-ID label resolver (PR-ADS-143, moved here by PR-ADS-177C).

A HubSpot / UTM campaign label is placed on a Google Ads campaign by its
stable ``campaign_id``, never by display name:

1. an APPROVED durable identity mapping (``google_ads_campaign_identity``)
   decides first — ``manual`` / ``exact_normalized`` put the label on its
   campaign id, ``not_google_ads`` excludes it;
2. only when no durable mapping exists, an exact-normalised match against the
   canonical spend campaign names places it — and only when exactly ONE
   campaign id carries that normalised name. Two campaign ids that share a
   display name are never merged: the label stays unmatched.
3. Never fuzzy.

Until PR-ADS-177C this lived in ``services.campaign_evidence_service``
(``_spend_by_campaign_id`` / ``_identity_index`` / ``_assign_lead``). That page
is retired; two readers still need this rule, unchanged:

* ``canonical_customer_revenue_service.campaign_resolver`` — the closed-won
  truth audit's deal placement;
* :func:`google_ads_cohort_sqls_by_campaign_id` below — the cross-page parity
  audit's INDEPENDENT reading of the Google Ads acquisition-cohort SQLs. The
  live campaign surfaces (the Revenue Decision Mart's campaign views and every
  Dashboard tab) place contacts with the mart's own NAME-keyed rule
  (``revenue_attribution_service._mart_label_resolver``). Reading the same
  canonical contact funnel through this campaign-id rule is what keeps parity a
  comparison between two implementations rather than a page compared with a
  republication of itself (``docs/60`` §9).

Read-only. No writes to Google Ads, HubSpot or the local database.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from services.campaign_identity_service import normalize_campaign_name

logger = logging.getLogger(__name__)


def spend_by_campaign_id(spend_result: dict) -> tuple[dict, dict]:
    """Canonical spend keyed by ``campaign_id`` plus a normalised-name ->
    ``{campaign_id}`` index for the exact-normalised fallback.

    Two campaigns whose display names normalise to the same text keep SEPARATE
    ids (the index maps the shared name to a SET, so the fallback stays
    ambiguous and never merges them)."""
    by_id: dict = {}
    norm_to_ids: dict = {}
    for r in (spend_result.get("rows") or []):
        cid = r.get("campaign_id")
        if cid is None:
            continue
        cid = str(cid)
        by_id[cid] = {
            "campaign_id": cid,
            "campaign_name": r.get("campaign_name"),
            "native": r.get("spend"),
            "usd": r.get("spend_usd"),
            "fx_complete": bool(r.get("fx_complete")),
        }
        norm = normalize_campaign_name(r.get("campaign_name"))
        if norm:
            norm_to_ids.setdefault(norm, set()).add(cid)
    return by_id, norm_to_ids


def identity_index(identity_result: dict) -> tuple[dict, dict]:
    """Approved durable mappings -> ``{norm(external_label): mapping}`` plus
    aliases by campaign id. Only approved rows are present
    (``fetch_campaign_identity`` filters); never fuzzy."""
    by_label: dict = {}
    aliases_by_id: dict = {}
    for m in (identity_result.get("mappings") or []):
        label = m.get("external_campaign_label")
        norm = normalize_campaign_name(label)
        if norm:
            by_label.setdefault(norm, m)
        cid = m.get("campaign_id")
        if cid is not None and label:
            aliases_by_id.setdefault(str(cid), set()).add(label)
    return by_label, aliases_by_id


def assign_label(label, identity_by_label: dict, spend_norm_to_ids: dict) -> tuple[str, Any]:
    """Map a HubSpot / external label to a Google Ads campaign id, or a bucket.

    Returns ``(kind, key)``:

    * ``("google_ads", campaign_id)`` — mapped to a Google Ads campaign;
    * ``("not_google_ads", None)`` — approved as a non-Google-Ads label;
    * ``("unmatched", norm)`` — no durable / exact mapping (Mapping Review).

    Durable APPROVED identity mapping first; the exact-normalised fallback
    (against canonical spend names) only when no durable mapping exists; never
    fuzzy; never merge two campaign ids sharing a normalised display name.
    """
    norm = normalize_campaign_name(label)
    if not norm:
        return ("unmatched", "")
    m = identity_by_label.get(norm)
    if m is not None:
        method = (m.get("match_method") or "").strip().lower()
        cid = m.get("campaign_id")
        if method == "not_google_ads":
            return ("not_google_ads", None)
        if cid is not None and method in ("manual", "exact_normalized"):
            return ("google_ads", str(cid))
        return ("unmatched", norm)      # unmatched method / no campaign_id
    ids = spend_norm_to_ids.get(norm)
    if ids and len(ids) == 1:            # exactly one spend campaign -> safe fallback
        return ("google_ads", next(iter(ids)))
    return ("unmatched", norm)           # ambiguous (>1 id) or no spend match


def build_resolver(spend_result: dict, identity_result: dict):
    """``label -> (kind, key)`` bound to one spend read and one identity read."""
    _, norm_to_ids = spend_by_campaign_id(spend_result)
    by_label, _ = identity_index(identity_result)
    return lambda label: assign_label(label, by_label, norm_to_ids)


def google_ads_cohort_sqls_by_campaign_id(start: date | None, end: date, *,
                                          now: datetime | None = None) -> dict:
    """The window's Google Ads acquisition-cohort SQLs, placed by campaign ID.

    An independent reading for the cross-page parity audit (PR-ADS-177C). It
    reads the canonical contact funnel through the cohort service's one
    population rule (``build_window_outcomes``: contacts CREATED in the
    inclusive account-local days ``start..end``, SQL proven by canonical
    lifecycle evidence, the same freshness verdict), and resolves campaign
    labels with the campaign-ID rule above, bound to THIS window's canonical
    spend of the configured account and that account's approved identity
    mappings — the rule the retired Campaign Evidence page applied.

    Returns ``{"available", "sql_status", "sql_reason", "google_ads_sqls",
    "campaign_sqls", "unattributed_sqls", "excluded_non_google_sqls",
    "by_campaign_id"}``. Every count is None unless the cohort's verdict is
    ``published`` — a withheld count is absent, never 0. Never raises.
    """
    from db import revenue_repository as repo  # noqa: PLC0415
    from services import marketing_outcome_cohort_service as cohort_svc  # noqa: PLC0415

    empty = {"available": False, "sql_status": cohort_svc.STATUS_UNAVAILABLE,
             "sql_reason": None, "google_ads_sqls": None, "campaign_sqls": None,
             "unattributed_sqls": None, "excluded_non_google_sqls": None,
             "by_campaign_id": None}
    try:
        from services.google_ads_spend_service import configured_customer_id  # noqa: PLC0415
        # The configured account's campaigns — the account the live campaign
        # surfaces publish (the retired page read every account present).
        spend = repo.fetch_canonical_campaign_spend(
            start, end, customer_id=configured_customer_id())
        identity = repo.fetch_campaign_identity(spend.get("customer_id"))
        resolver = build_resolver(spend, identity)
        outcomes = cohort_svc.build_window_outcomes(start, end, resolve_label=resolver,
                                                    now=now)
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unavailable reading, never a crash
        logger.warning("[campaign_label_resolver] cohort reading failed: %s", exc)
        return {**empty, "sql_reason": f"{type(exc).__name__}"}
    cohort = outcomes.get("cohort")
    status, reason = tuple(outcomes.get("sql_publication")
                           or (cohort_svc.STATUS_UNAVAILABLE, None))
    published = cohort is not None and status == cohort_svc.STATUS_PUBLISHED
    out = {**empty, "available": cohort is not None, "sql_status": status,
           "sql_reason": reason}
    if not published:
        return out
    by_campaign = {str(k): int(v.get("sqls") or 0)
                   for k, v in (cohort.get("by_campaign") or {}).items()}
    out.update({
        "google_ads_sqls": int(cohort["google_ads"]["sqls"]),
        "campaign_sqls": sum(by_campaign.values()),
        "unattributed_sqls": int(cohort["unattributed"]["sqls"]),
        "excluded_non_google_sqls": int(cohort["excluded_non_google"]["sqls"]),
        "by_campaign_id": by_campaign,
    })
    return out
