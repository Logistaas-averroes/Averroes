"""
services/dataset_keys.py

PR-ADS-153F / PR-ADS-154 — the ONE registry of canonical (source, dataset)
machine keys, and the only place that decides whether a pair is registered.

Freshness, System Status and Revenue Health all look a dataset up by the
``(source, dataset)`` pair that its writer stamped on ``sync_batches``. When the
writer and the config spell that pair differently, the lookup silently matches
nothing: the dataset reports "never run" forever while its table fills up
normally. The PR-ADS-153A audit found exactly that on canonical campaign spend —
writers stamped ``source="google_ads"`` while the freshness config expected
``google_ads_api``, so the ROAS denominator had no freshness signal at all.

Spelling a key in two places is what allows them to disagree, so the keys live
HERE and every side imports them: the writers that stamp batches, the freshness
configuration that reads them, and the scheduler that orchestrates the datasets.

This module deliberately has no imports of its own. Anything may depend on it,
and it can never create an import cycle between a dataset's owner and the
configuration that describes it.

One canonical Google Ads source
-------------------------------
``google_ads_api`` is THE platform-evidence source key. ``google_ads`` was a
second spelling of the same thing, which is how the drift above happened.

It is not simply deleted, because production ``sync_batches`` and ``sync_state``
rows are already stamped with it — including the successful historical geo
bootstrap. Deleting the spelling would orphan that history and every affected
dataset would report "never run" again, which is the same defect wearing the
opposite mask. Instead the registry **normalizes** it: writers canonicalize
before stamping, so new rows carry one key, and readers canonicalize on the way
out, so the older rows keep resolving to the same dataset.
"""

from __future__ import annotations

# ── Sources ─────────────────────────────────────────────────────────────────

#: THE Google Ads platform-evidence source key. Campaign spend, geo spend,
#: search terms, keywords and keyword facts all stamp this.
PLATFORM_EVIDENCE_SOURCE = "google_ads_api"

#: Superseded spellings, mapped to the canonical key. A source in this map is
#: never written again; it is only recognised so history stays readable.
SOURCE_ALIASES: dict[str, str] = {
    "google_ads": PLATFORM_EVIDENCE_SOURCE,
}

#: Every source an active writer may stamp. `windsor` is retained ONLY so that
#: historical rows still resolve — no active scheduler path writes it any more
#: (PR-ADS-154 removed Windsor from orchestration). Retiring the historical rows
#: and the connector is a separate PR.
VALID_SYNC_SOURCES: frozenset[str] = frozenset({
    PLATFORM_EVIDENCE_SOURCE,
    "hubspot",
    "fx",
    "gclid",
    "mailchimp",
    "analysis",
    "windsor",          # historical only — not written by any active path
})

# ── Datasets ────────────────────────────────────────────────────────────────

#: Every dataset key an active writer may stamp. A key missing here produces an
#: "unknown dataset" warning and, more importantly, means some config somewhere
#: is describing a dataset by a name nothing writes.
VALID_SYNC_DATASETS: frozenset[str] = frozenset({
    # Platform evidence
    "campaigns", "keywords", "keyword_facts", "search_terms", "geo",
    "canonical_spend", "canonical_geo",
    # CRM
    "contacts", "deals", "matches", "contact_funnel", "lifecycle_events",
    "deal_ledger", "source_classification",
    # Currency
    "daily_rates",
    # Mailchimp (PR-ADS-151)
    "reports", "audiences", "attribution", "coverage_snapshots",
    # Derived analysis
    "waste_terms",
})

VALID_SYNC_TYPES: frozenset[str] = frozenset({
    "backfill", "daily", "weekly", "monthly", "manual",
})

VALID_SYNC_STATUSES: frozenset[str] = frozenset({
    "running", "success", "failed", "unknown",
})


def canonical_source(source: str | None) -> str:
    """Normalize a source key to its canonical spelling.

    Lower-cased, trimmed, and mapped through :data:`SOURCE_ALIASES`. Applied on
    the way IN (so one key is written) and on the way OUT (so history written
    under a superseded spelling still resolves to the same dataset).
    """
    key = (source or "").strip().lower()
    return SOURCE_ALIASES.get(key, key)


def is_registered_source(source: str | None) -> bool:
    """Whether ``source`` resolves to a registered source key."""
    return canonical_source(source) in VALID_SYNC_SOURCES


def is_registered_dataset(dataset: str | None) -> bool:
    """Whether ``dataset`` is a registered dataset key."""
    return (dataset or "").strip().lower() in VALID_SYNC_DATASETS


def is_registered_pair(source: str | None, dataset: str | None) -> bool:
    """Whether both halves of a ``(source, dataset)`` pair are registered.

    The contract test enumerates every pair the active scheduler stamps and
    asserts this, so a new dataset cannot ship with a key nothing recognises —
    the failure mode where a run logs "unknown dataset" and its freshness row
    quietly never appears.
    """
    return is_registered_source(source) and is_registered_dataset(dataset)


# ── Canonical dataset keys, by owner ────────────────────────────────────────

# Canonical Google Ads campaign-daily spend — the ROAS denominator.
CANONICAL_SPEND_SOURCE = PLATFORM_EVIDENCE_SOURCE
CANONICAL_SPEND_DATASET = "canonical_spend"

# Canonical Google Ads per-country (geo) daily spend — the Country ROAS
# denominator, reconciled against canonical campaign spend.
CANONICAL_GEO_SOURCE = PLATFORM_EVIDENCE_SOURCE
CANONICAL_GEO_DATASET = "canonical_geo"

# The durable scope key for canonical geo run/checkpoint state
# (google_ads_geo_sync_state.scope). Not a sync_batches key — named here so a
# second geo dataset cannot quietly reuse the same state row.
CANONICAL_GEO_SCOPE = "geo_daily_spend"

# Daily reference FX rates (GBP→USD), required before any USD figure is safe.
FX_SOURCE = "fx"
FX_DAILY_RATES_DATASET = "daily_rates"

# Canonical HubSpot deal ledger — the ONE revenue population.
DEAL_LEDGER_SOURCE = "hubspot"
DEAL_LEDGER_DATASET = "deal_ledger"

# Acquisition-source classification / deal attribution.
SOURCE_CLASSIFICATION_SOURCE = "hubspot"
SOURCE_CLASSIFICATION_DATASET = "source_classification"

# GCLID click↔deal attribution rows. PR-ADS-154: the incremental scheduler was
# stamping `(hubspot, gclid_matches)` while the freshness config read
# `(gclid, matches)`, so this dataset reported "never run" forever while
# `gclid_attribution` filled up normally — the same drift class as
# canonical_spend, found by the same audit.
GCLID_SOURCE = "gclid"
GCLID_MATCHES_DATASET = "matches"
GCLID_COVERAGE_SNAPSHOTS_DATASET = "coverage_snapshots"

__all__ = [
    "PLATFORM_EVIDENCE_SOURCE", "SOURCE_ALIASES",
    "VALID_SYNC_SOURCES", "VALID_SYNC_DATASETS",
    "VALID_SYNC_TYPES", "VALID_SYNC_STATUSES",
    "canonical_source", "is_registered_source", "is_registered_dataset",
    "is_registered_pair",
    "CANONICAL_SPEND_SOURCE", "CANONICAL_SPEND_DATASET",
    "CANONICAL_GEO_SOURCE", "CANONICAL_GEO_DATASET", "CANONICAL_GEO_SCOPE",
    "FX_SOURCE", "FX_DAILY_RATES_DATASET",
    "DEAL_LEDGER_SOURCE", "DEAL_LEDGER_DATASET",
    "SOURCE_CLASSIFICATION_SOURCE", "SOURCE_CLASSIFICATION_DATASET",
    "GCLID_SOURCE", "GCLID_MATCHES_DATASET", "GCLID_COVERAGE_SNAPSHOTS_DATASET",
]
