# Attribution Confidence Model

## Overview

The Logistaas Ads Intelligence System uses a three-tier attribution model to match revenue (HubSpot closed-won deals) to ad spend (Windsor / Google Ads campaigns).

Each ROAS row carries an `attribution_confidence` field indicating how the revenue was attributed to a campaign or country.

---

## Tiers

### Tier 1 — Exact GCLID (`tier_1_gclid`)

- **Trust level:** Exact
- **Method:** The deal (or its associated contact) has a GCLID that matches a Windsor click-level row.
- **Confidence:** High — this is click-level attribution evidence.
- **When this appears:** Only when both HubSpot and Windsor expose GCLID data, and an exact match exists.

### Tier 2 — Source Tag (`tier_2_source_tag`)

- **Trust level:** Directional
- **Method:** The deal's `hs_analytics_source` is `PAID_SEARCH` and `hs_analytics_source_data_1` matches a known campaign tag.
- **Confidence:** Medium — revenue is attributed to the correct campaign family, but not to a specific click.
- **When this appears:** When HubSpot records the paid-search source but GCLID matching is not available.

### Tier 3 — Spend-Weighted Estimate (`tier_3_spend_weighted`)

- **Trust level:** Estimated
- **Method:** Revenue is allocated proportionally based on campaign spend (fallback).
- **Confidence:** Low — this is a directional estimate, not a proven attribution path.
- **When this appears:** When neither GCLID nor source tag matching succeeds.
- **Important:** Tier 3 rows should never be used for final budget decisions. They are directional only.

---

## Overall Confidence

The system computes an overall confidence level for the entire ROAS dataset:

| Level   | Rule                                    |
|---------|-----------------------------------------|
| HIGH    | tier_1_share >= 70%                     |
| MEDIUM  | tier_1_share + tier_2_share >= 70%      |
| LOW     | tier_3_share > 50%                      |
| UNKNOWN | No ROAS rows available                  |

---

## Country-Level ROAS

Country-level ROAS remains **estimated** until GCLID attribution is fully wired. This is because:

1. Country is derived from the contact's IP or profile data, not from the ad click.
2. The spend-to-country mapping uses Windsor geo data, which is campaign-level aggregated spend.
3. No click-level country-to-deal path exists without GCLID.

The country ROAS page always displays an estimate warning banner.

---

## GCLID Readiness

The GCLID Readiness Audit measures whether the system is ready to move from Tier 2/3 attribution to Tier 1 (exact GCLID matching).

### Readiness Statuses

| Status    | Meaning                                                                 |
|-----------|-------------------------------------------------------------------------|
| READY     | Tier 1 matches exist. Both HubSpot and Windsor expose GCLID data.      |
| PARTIAL   | HubSpot has some GCLID data, but Windsor/click matching is incomplete.  |
| NOT_READY | No reliable Tier 1 GCLID match path exists.                            |
| UNKNOWN   | Required source files are missing or empty.                             |

### Readiness Score (0–100)

| Component                          | Points |
|------------------------------------|--------|
| HubSpot deals/contact GCLID exists | +30    |
| Windsor/click GCLID exists         | +30    |
| Exact GCLID matches found          | +25    |
| Source-tag fallback coverage exists | +10    |
| Country-level estimate warning on  | +5     |

The score is an operational readiness indicator, not a scientific measurement.

---

## Non-Goals

This model does NOT:

- Upload offline conversions (OCT)
- Capture GCLIDs from website forms
- Modify HubSpot forms or workflows
- Write to Google Ads
- Write to HubSpot
- Change bids or budgets
- Push negative keywords
- Rewrite ROAS calculations

---

## Related PRs

- PR-ADS-080A — Revenue Truth Layer (backend ROAS)
- PR-ADS-080B — Revenue-First Menu (frontend ROAS)
- PR-ADS-080C — Daily ROAS Snapshots
- PR-ADS-081 — GCLID Bridge Readiness Audit
- PR-ADS-082 — ROAS Confidence & Attribution Badges
