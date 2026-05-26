# Empty State & Page Explanation Model

**PR-ADS-070 — Empty State & Page Explanation Upgrade**

---

## 1. Purpose

Every page in the Logistaas Ads Intelligence app must answer five questions instantly:

1. **What am I looking at?** — purpose of this page
2. **Where does this data come from?** — data source
3. **When was it last updated?** — freshness context (via canonical freshness strip)
4. **Why is this page empty?** — specific, contextual empty-state explanation
5. **What should I check next?** — next action

No page should ever display a vague "No data found" without context.

---

## 2. Page Explanation Pattern

Each page shows a compact explanation panel containing:

| Field | Description |
|-------|-------------|
| **What this page shows** | One-sentence purpose |
| **What empty means** | Context-specific explanation of zero rows |
| **Next action** | Where to go or what to check |

Plus context chips showing:
- **Source** (e.g., Windsor / Google Ads)
- **Depends on** (upstream dataset)
- **Read-only** badge

The explanation panel uses the `PAGE_EXPLANATIONS` configuration object in `static/app.js`.

---

## 3. Empty State Taxonomy

Empty states are categorized by type:

| Type | Meaning | Severity |
|------|---------|----------|
| `loading` | Data is being fetched | Neutral |
| `no_rows` | Query returned zero rows | Info |
| `filtered_out` | User filters narrowed to zero | Info |
| `dependency_blocked` | Upstream dataset unavailable | Warning |
| `fresh_but_empty` | Sync succeeded but no rows in window | Warning |
| `stale_or_failed` | Pipeline stale or failed | Error |
| `db_unavailable` | Database offline | Error |
| `not_run` | Pipeline has never executed | Info |
| `unknown` | Cannot determine state | Info |

Severity drives visual treatment:
- **Info** = neutral explanation (grey)
- **Warning** = attention needed (amber)
- **Error** = pipeline broken (red)

---

## 4. Page Dependency Map

```javascript
const PAGE_DEPENDENCIES = {
  dashboard: ["campaigns", "leads", "deals", "waste_terms"],
  "action-queue": ["campaigns", "search_terms", "waste_terms", "leads"],
  reports: ["weekly_report"],
  campaigns: ["campaigns"],
  waste: ["waste_terms", "search_terms"],
  "search-terms": ["search_terms"],
  ngrams: ["ngrams", "search_terms"],
  geo: ["geo"],
  keywords: ["keywords"],
  leads: ["leads"],
  deals: ["deals"],
  "gclid-attribution": ["gclid_attribution", "gclid_coverage_snapshots"],
  opportunities: ["leads"],
  scheduler: ["runs"],
  health: ["system_status"],
  backfill: ["historical_backfill"],
  "historical-intelligence": ["historical_intelligence"]
};
```

Key dependencies:
- **Waste** depends on `search_terms` — if Search Term Universe is empty, waste cannot be trusted
- **N-Grams** depends on `search_terms` — pattern analysis requires search-term rows
- **Dashboard** depends on multiple datasets — partial data is expected

---

## 5. Message Rules

### Critical Rules

1. **Search Term Universe**: Zero rows does NOT mean the account is clean. The message must always state this explicitly.
2. **Flagged Waste Terms**: Must explain dependency on Search Term Universe. Zero waste is not proof of a clean account if search terms are unavailable.
3. **Search Pattern Analysis**: Must explain it is computed from Search Term Universe. If search terms are blocked, patterns cannot be trusted.
4. **Admin Backfill**: Must explain that dry-run is safe and does not write to the database, Google Ads, or HubSpot.
5. **System Status**: War Room shows blockers and pipeline health — it does not modify anything.

### General Rules

- A zero-row page should only look "scary" (warning/error) if canonical freshness says it is suspicious, blocked, stale, or failed.
- Use canonical freshness status from `/api/datasets/freshness` where available.
- If `dependency_blocked`, never say "No data found" — say "Blocked by [dependency]".
- No page should imply write capability to Google Ads or HubSpot.

---

## 6. Examples by Page

### Search Term Universe (empty)
> No search-term rows are stored for this window.
>
> This does not mean the account is clean. It means the Search Terms evidence pipeline has no data for the selected window.
>
> **Check:** System Status → Search Terms Pipeline.

### Flagged Waste Terms (empty)
> No flagged waste terms in this time range.
>
> This may mean no terms were flagged, or it may mean Search Term Universe is empty. Waste detection depends on Search Term Universe — if search-term data is unavailable, this page cannot confirm the account is clean.
>
> **Next:** Check System Status before assuming no waste exists.

### Search Pattern Analysis (empty)
> No n-grams found for the selected filters.
>
> Search Pattern Analysis is computed from Search Term Universe. If Search Terms has no usable rows, pattern analysis cannot produce trustworthy results.
>
> **Next:** Fix Search Term Universe first.

### Admin Backfill (no run selected)
> No backfill run selected.
>
> Run a dry-run first to preview the ingestion plan. Dry-run does not write to the database or modify Google Ads/HubSpot. Read-only.

### Campaigns (empty)
> No campaign performance rows found for this window.
>
> This may mean Windsor campaign data has not synced, the selected window has no stored rows, or the data pipeline is stale.
>
> **Next:** Check System Status → Windsor / Campaigns.

---

## 7. Read-Only Governance

All page explanations and empty states reinforce read-only governance:

- Context chips include a "Read-only" badge
- Admin Backfill explicitly states dry-run safety
- No page implies write capability
- No page suggests triggering scheduler jobs from empty states
- No page suggests pushing negatives or modifying Google Ads

---

## 8. Future Follow-Ups

| PR | Description |
|----|-------------|
| PR-ADS-071 | Page Help Panels — expandable help drawers per page |
| PR-ADS-072 | Waste & N-Grams Confidence Restoration — trusted/untrusted badges |
| PR-ADS-073 | Daily Incremental Sync — improves freshness for empty-state accuracy |
