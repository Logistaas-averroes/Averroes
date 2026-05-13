# Historical Intelligence

## Purpose

Historical Intelligence analyzes local historical data only. It does not modify
Google Ads, HubSpot, campaigns, bids, budgets, contacts, deals, or negative keywords.

The goal is to turn locally-stored historical data into trend evidence that can be
reviewed by a human before any action is considered:

```
local historical data → trend analysis → dashboard/report insight → human review
```

Not:

```
historical data → automatic Google Ads changes
```

---

## Data Sources

Historical Intelligence reads exclusively from the local PostgreSQL database.
It does **not** call Windsor, HubSpot, Google Ads, or any external service.

Tables used:

| Table        | Usage                                                   |
|--------------|---------------------------------------------------------|
| `campaigns`  | Spend, confirmed SQLs, junk rate, CPQL per run date     |
| `geo`        | Geo spend per country/campaign per run date             |

Future phases may extend to `leads`, `deals`, and `gclid_attribution` tables.

---

## Current vs Previous Period Logic

A trend is computed by comparing two non-overlapping windows of equal length:

- **Current period**: the most recent `current_days` days (default: 30)
- **Previous period**: the `previous_days` days immediately before the current period (default: 30)

Example with defaults:

```
Today
|-- current 30 days --|-- previous 30 days --|
      [d-0 to d-30]        [d-30 to d-60]
```

Metrics are aggregated per entity (campaign or geo) across all runs in each window.
The windows are compared to produce movement labels and a trend status.

---

## Trend Labels

| Label                | Meaning                                                                      |
|----------------------|------------------------------------------------------------------------------|
| `improving`          | Quality metrics moved in a positive direction                                |
| `deteriorating`      | Quality metrics moved in a negative direction                                |
| `stable`             | All metrics within ±5% of the previous period                               |
| `insufficient_data`  | One or both periods have no usable data                                      |
| `new_activity`       | Current period has data but previous period has none                         |
| `no_recent_activity` | Previous period has data but current period has none                         |

### Deterioration signal logic

A campaign is classified as **deteriorating** when two or more of the following
signals are present:

1. Spend increased **and** confirmed SQLs decreased (combined signal, weight 2)
2. Junk rate increased (weight 1)
3. CPQL worsened (weight 1)

### Improvement signal logic

A campaign is classified as **improving** when improve signals outweigh degrade signals:

1. Confirmed SQLs increased (weight 1)
2. Junk rate decreased (weight 1)
3. CPQL improved (weight 1)

Otherwise the campaign is classified as **stable**.

### Forbidden action labels

The following labels **must not** appear in any trend output:

- `scale`, `cut`, `pause`, `apply`, `push`, `block`, `send`, `upload`
- `change bid`, `change budget`

These belong to existing campaign verdict logic and are not used here.

---

## CPQL Safety

CPQL (Cost Per Qualified Lead) is computed as:

```
CPQL = total_spend / confirmed_sqls
```

**When `confirmed_sqls = 0`, CPQL is returned as `None` (N/A).**

The system never divides by zero. The API and dashboard will display `N/A` in
all contexts where CPQL cannot be computed.

---

## Read-Only Governance

Historical Intelligence is bound by Phase 1 doctrine:

- ✅ Read local DB
- ✅ Compute trend signals
- ✅ Display advisory output
- ❌ No Google Ads writes
- ❌ No HubSpot writes
- ❌ No negative keyword push
- ❌ No OCT upload
- ❌ No campaign pause
- ❌ No bid changes
- ❌ No budget changes
- ❌ No auto-recommendations that imply execution

All trend notes use advisory language:

> Warrants review · quality deteriorated · quality improved · watchlist candidate ·
> historical risk signal · human review required

Forbidden language:

> ~~pause this campaign~~ · ~~cut this budget~~ · ~~apply negatives~~ ·
> ~~block this term~~ · ~~increase bid~~ · ~~push change~~

---

## Dashboard Usage

The **Historical Intelligence** page in the dashboard shows:

1. **KPI cards** — counts of improving / deteriorating / stable / insufficient data campaigns
2. **Trend table** — per-entity breakdown with:
   - Trend badge (color-coded)
   - Spend movement (↑ / ↓)
   - SQL movement (↑ / ↓)
   - Junk rate movement (↑ / ↓)
   - CPQL movement (better / worse)
   - Human review note

Users can filter by entity (campaigns | geo) and adjust the comparison window length.

**No action buttons appear on this page.** The page is read-only.

---

## API Contract

### `GET /api/historical-intelligence`

**Auth required.** Read-only. No external calls. No mutations.

#### Query parameters

| Parameter      | Type    | Default      | Description                           |
|----------------|---------|--------------|---------------------------------------|
| `entity`       | string  | `campaigns`  | Entity to analyse: `campaigns` or `geo` |
| `current_days` | integer | `30`         | Current window in days (1–180)        |
| `previous_days`| integer | `30`         | Previous window in days (1–180)       |
| `limit`        | integer | `25`         | Max rows returned (1–100)             |

#### Success response (status: ok)

```json
{
  "entity": "campaigns",
  "current_days": 30,
  "previous_days": 30,
  "status": "ok",
  "summary": {
    "improving": 3,
    "deteriorating": 5,
    "stable": 7,
    "insufficient_data": 10,
    "new_activity": 0,
    "no_recent_activity": 2
  },
  "rows": [
    {
      "campaign_name": "example campaign",
      "current": {
        "spend": 1200.00,
        "confirmed_sqls": 3,
        "junk_rate": 24.0,
        "cpql": 400.00,
        "lead_count": 20
      },
      "previous": {
        "spend": 900.00,
        "confirmed_sqls": 5,
        "junk_rate": 12.0,
        "cpql": 180.00,
        "lead_count": 15
      },
      "movement": {
        "spend": "up",
        "sqls": "down",
        "junk_rate": "up",
        "cpql": "worse"
      },
      "trend_status": "deteriorating",
      "human_review_note": "Spend increased, confirmed SQLs decreased, junk rate rose. Warrants human review."
    }
  ]
}
```

#### Insufficient data response

```json
{
  "entity": "campaigns",
  "current_days": 30,
  "previous_days": 30,
  "status": "insufficient_data",
  "message": "Historical intelligence requires at least two comparable periods.",
  "summary": { "improving": 0, "deteriorating": 0, "stable": 0, ... },
  "rows": []
}
```

---

## Known Limitations

1. **Campaign table only**: the current implementation reads from the `campaigns` table,
   which is populated by the weekly/monthly scheduler. Daily incremental sync does not
   write campaign snapshots. This means fewer data points are available for entities
   that only appear in weekly/monthly runs.

2. **Aggregation method**: metrics are summed across all run snapshots in the window.
   For junk rate, the average across runs is used. This can be affected by unequal
   numbers of runs in each window.

3. **Minimum history**: at least one run in each window is required for a meaningful
   trend. Campaigns with fewer than two total runs will be labeled `insufficient_data`.

4. **No confidence intervals**: the current implementation does not compute statistical
   confidence. Treat all trend signals as directional evidence, not statistical proof.

5. **Geo lacks SQL/junk data**: the `geo` table stores spend and clicks only. Geo trends
   are therefore spend-only and cannot be classified as improving/deteriorating based on
   quality signals.
