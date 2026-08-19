"""
services/dataset_keys.py

PR-ADS-153F — the ONE registry of canonical (source, dataset) machine keys.

Freshness, System Status and Revenue Health all look a dataset up by the
``(source, dataset)`` pair that its writer stamped on ``sync_batches``. When the
writer and the config spell that pair differently, the lookup silently matches
nothing: the dataset reports "never run" forever while its table fills up
normally. The PR-ADS-153A audit found exactly that on canonical campaign spend —
writers stamped ``source="google_ads"`` while the freshness config expected
``google_ads_api``, so the ROAS denominator had no freshness signal at all.

Spelling a key in two places is what allows them to disagree, so the keys live
HERE and both sides import them. This module deliberately has no imports of its
own: anything may depend on it, and it can never create an import cycle between
a dataset's owner and the freshness configuration that describes it.
"""

from __future__ import annotations

# Canonical Google Ads campaign-daily spend — the ROAS denominator.
CANONICAL_SPEND_SOURCE = "google_ads"
CANONICAL_SPEND_DATASET = "canonical_spend"

# Canonical Google Ads per-country (geo) daily spend — the Country ROAS
# denominator, reconciled against canonical campaign spend.
CANONICAL_GEO_SOURCE = "google_ads"
CANONICAL_GEO_DATASET = "canonical_geo"

# The durable scope key for canonical geo run/checkpoint state
# (google_ads_geo_sync_state.scope). Not a sync_batches key — named here so a
# second geo dataset cannot quietly reuse the same state row.
CANONICAL_GEO_SCOPE = "geo_daily_spend"

__all__ = [
    "CANONICAL_SPEND_SOURCE", "CANONICAL_SPEND_DATASET",
    "CANONICAL_GEO_SOURCE", "CANONICAL_GEO_DATASET", "CANONICAL_GEO_SCOPE",
]
