# Data Foundation Hardening Audit

**Document:** `docs/11_DATA_FOUNDATION_HARDENING_AUDIT.md`
**Roadmap ID:** PR-ADS-050
**Phase:** 1.5 — Data Foundation Hardening
**Owner:** Youssef Awwad
**Audit date:** 2026-05-07
**Status:** Audit-only. No code changed. No schema changed. No API changed.

Depends on: PR-ADS-049
Unblocks: PR-ADS-051 — Data Foundation Cleanup Patch

---

## 1. Executive Verdict

### Are PR-ADS-039 through PR-ADS-049 structurally safe?

**Yes, with low-to-medium risk items.** The data foundation added across these PRs is correctly designed, read-only doctrine is preserved throughout, and no blocking structural defects were found in schema, writers, schedulers, or API endpoints.

### Are there blocking issues?

**No blocking issues.** All 🔴 Blocking rows in the risk register below are absent — no blocking defects were found. The highest severity items are 🟠 High and are addressable in PR-ADS-051.

### Is the system ready for the next feature phase?

**Yes, conditionally.** The data foundation is safe to build on. The following items should be resolved in PR-ADS-051 before scaling search-term or attribution features:

- Duplicated cursor encode/decode helpers across endpoints (shared helper recommended)
- Loaded-page KPI labels are accurate on the Search Terms and GCLID Attribution pages but should be verified against the `summary` block from the API
- `write_search_terms` and `write_gclid_attribution` return `len(rows)` as attempted-upsert count, not confirmed-write count — callers must treat this as "attempted, not guaranteed"
- `BACKFILL_RUNBOOK.md` references `gclid_attribution` as "not yet available" but the table now exists (PR-ADS-044); the note is stale

### Which cleanup PR should happen next?

**PR-ADS-051** — Data Foundation Cleanup Patch. Surgical fixes only: shared cursor helper, standard loaded-page label convention, a small docs correction, and any missing index recommendations from this audit.

### Severity scale

| Symbol | Level | Meaning |
|--------|-------|---------|
| 🔴 | Blocking | Must fix before next feature PR merges |
| 🟠 | High | Should fix in PR-ADS-051 |
| 🟡 | Medium | Should fix within next 2 PRs |
| 🟢 | Low | Fix when convenient |

---

## 2. Scope Reviewed

| PR | Module | Description |
|----|--------|-------------|
| PR-ADS-039 | `db/schema.py`, `db/writers.py` | sync_batches + sync_state tables, writer helpers |
| PR-ADS-040 | `db/schema.py`, `db/writers.py` | search_terms table, write_search_terms() |
| PR-ADS-041 | `scripts/backfill.py`, `docs/BACKFILL_RUNBOOK.md` | Historical backfill CLI skeleton (dry-run only) |
| PR-ADS-042 | `scheduler/daily.py` | Daily incremental sync tracking for hubspot/contacts and windsor/search_terms |
| PR-ADS-043 | `static/app.js` | Search Terms forensics page (frontend) |
| PR-ADS-044 | `db/schema.py`, `db/writers.py` | gclid_attribution + gclid_coverage_snapshots tables, writer functions |
| PR-ADS-045 | `api/server.py`, `static/app.js` | /api/datasets/freshness endpoint, Dataset Freshness panel |
| PR-ADS-046 | `static/app.js` | GCLID Attribution UI page |
| PR-ADS-047 | `static/app.js`, `api/server.py` | Campaign attribution drilldown in campaign drawer |
| PR-ADS-048 | `api/server.py`, `static/app.js` | /api/attribution/quality endpoint, quality signals panel |
| PR-ADS-049 | `static/app.js` | Campaign attribution quality overlay in campaign drawer |

---

## 3. Database Schema Audit

### sync_batches (`db/schema.py` lines 206–225)

- `run_id` is nullable (`REFERENCES runs(id) ON DELETE SET NULL`) — intentional: allows batches started outside a formal run context, and preserves audit trail on run deletion ✅
- `source`, `dataset`, `status` are `NOT NULL` ✅
- `status` defaults to `'running'`; updated to `'success'` or `'failed'` via `finish_sync_batch()` ✅
- Indexes: `(source, dataset)`, `status`, `started_at`, `run_id` — supports freshness queries efficiently ✅
- Failed batches preserve `error_message` and `finished_at` — audit trail intact ✅
- No `UNIQUE` constraint on `(source, dataset)` — multiple batches per dataset are by design (one per sync run) ✅

### sync_state (`db/schema.py` lines 228–242)

- `UNIQUE(source, dataset)` present ✅
- On failure, `finish_sync_batch()` issues `DO UPDATE SET status = 'failed', error_message = ..., updated_at = NOW()` without touching `last_successful_sync_at` or `last_source_date` — watermark preserved ✅ (lines 933–946)
- `update_sync_state()` uses `COALESCE(EXCLUDED.last_successful_sync_at, sync_state.last_successful_sync_at)` — non-null values never overwritten with NULL ✅ (lines 1010–1013)
- `updated_at` is set to `NOW()` on every upsert, representing last write time (not last success time) — semantics are clear ✅
- Valid statuses: `unknown` (default), `running` (written by `start_sync_batch`), `success`, `failed` — all four documented in code ✅

### search_terms (`db/schema.py` lines 248–336)

- Grain: `(source_date, campaign_name, ad_group, keyword, match_type, search_term)` — unique natural key enforced via `CREATE UNIQUE INDEX idx_search_terms_unique_fact` with `COALESCE` on nullable columns ✅
- `search_term NOT NULL` enforced at DDL level; `PR-ADS-040A` migration purges and re-constrains existing nulls ✅
- `is_flagged_waste` tri-state correctly defined:
  - `NULL` = not analysed
  - `TRUE` = flagged waste
  - `FALSE` = analysed clean ✅
- The `write_search_terms()` upsert does **not** touch `is_flagged_waste`, `junk_category`, or `matched_pattern` on conflict — raw writer cannot override waste classifications ✅ (lines 697–722)
- Cursor index: `idx_search_terms_cursor ON search_terms(source_date DESC, id DESC)` — matches API ORDER BY ✅
- `q=` / `ILIKE '%term%'` filter: relies on sequential scan without `pg_trgm`. Schema comment documents this correctly (lines 312–318). No B-tree index added (correct — B-tree does not support `LIKE '%..%'`). 🟡 **Risk:** at large table sizes this becomes a performance concern.
- `idx_search_terms_flagged_waste` index on `is_flagged_waste` — supports efficient `waste_only=true` queries ✅

### gclid_attribution (`db/schema.py` lines 343–406)

- `attribution_key` is `UNIQUE NOT NULL` — stable SHA1 dedupe key (gclid|contact_id|deal_id-or-first_url|campaign|keyword|match_status) ✅
- When `deal_id` is absent, `first_url` is included in the key to preserve uniqueness — documented in schema comment and `_make_attribution_key()` ✅ (lines 1097–1119)
- Multiple deals for the same contact/GCLID are preserved as separate rows (distinct `deal_id` → distinct `attribution_key`) ✅
- On conflict, `run_id` and `sync_batch_id` are preserved via `COALESCE(existing, excluded)` — original run context retained for audit continuity ✅ (lines 1239–1240)
- `match_status` column: `matched | unmatched | url_fallback | unknown` — values are consistent between `_normalise_gclid_match_status()` and `_make_attribution_key()` ✅
- Cursor index: `idx_gclid_attr_cursor ON gclid_attribution(created_at DESC, id DESC)` — matches API ORDER BY ✅
- `idx_gclid_attr_created ON gclid_attribution(created_at DESC)` also exists — slight duplication but harmless ✅

### gclid_coverage_snapshots (`db/schema.py` lines 409–434)

- `idx_gclid_coverage_snapshot_date ON gclid_coverage_snapshots(snapshot_date DESC)` ✅
- `raw_summary JSONB` preserves full coverage dict for future analysis ✅
- `/api/gclid-coverage` uses `WHERE snapshot_date >= CURRENT_DATE - %s` (DATE arithmetic) — correct for a DATE column ✅ (line 3727)
- No `UNIQUE` constraint on `snapshot_date` — multiple snapshots per day are by design (one per run) ✅

### Schema Table Summary

| Table | Grain | Strengths | Risks | Recommended Fix |
|-------|-------|-----------|-------|-----------------|
| `sync_batches` | One row per sync operation | Nullable run_id intentional, audit trail preserved, good indexes | None | None |
| `sync_state` | One row per source+dataset | UNIQUE(source,dataset), watermark preserved on failure, COALESCE guards | None | None |
| `search_terms` | source_date + campaign + ad_group + keyword + match_type + search_term | Tri-state correct, cursor index matches ORDER BY, raw writer cannot overwrite waste flags | ILIKE without pg_trgm is a sequential scan at scale | Document trigram extension install in PR-ADS-051 |
| `gclid_attribution` | attribution_key (SHA1 dedupe) | Stable key, multi-deal preserved, run_id audit trail, cursor index | Slight redundancy between idx_gclid_attr_cursor and idx_gclid_attr_created | Drop idx_gclid_attr_created in PR-ADS-051 (low priority) |
| `gclid_coverage_snapshots` | snapshot_date per run | snapshot_date index, JSONB raw summary | No UNIQUE on snapshot_date (multiple per day) | Acceptable by design |

---

## 4. Writer Function Audit

### `start_sync_batch()` (`db/writers.py` lines 784–853)

- **Returns:** `int` batch_id on success, `0` on DB unavailable or invalid inputs ✅
- **Errors:** Caught and logged; never raises ✅
- **Validation:** source/dataset/sync_type stripped and lowercased; unknown values are warned (not failed) — extensible without deploy ✅
- **date_from/date_to:** coerced via `_to_date_or_none()`; invalid values return `0` early ✅
- **Risk:** Returns `0` both for "DB unavailable" and "invalid inputs" — callers cannot distinguish the two failure modes. 🟡 Medium.

### `finish_sync_batch()` (`db/writers.py` lines 856–955)

- **Returns:** `bool` — `True` on success, `False` on DB unavailable or invalid `batch_id` ✅
- **Status guard:** only accepts `'success'` or `'failed'`; logs warning and returns `False` otherwise ✅
- **Watermark safety:** on `'failed'`, `sync_state` update preserves `last_successful_sync_at` and `last_source_date` via selective DO UPDATE ✅
- **Errors:** Caught and logged; never raises ✅
- **Risk:** `last_source_date` resolution falls back to `batch_date_to` when `last_source_date` arg is None — if `date_to` was set at batch start, this is an approximation. 🟢 Low.

### `update_sync_state()` (`db/writers.py` lines 958–1042)

- **Returns:** `bool` ✅
- **NULL-safety:** all three watermark columns (`last_successful_sync_at`, `last_source_date`, `last_batch_id`) use `COALESCE(EXCLUDED.x, sync_state.x)` — non-null values never overwritten with NULL ✅
- **Errors:** Never raises ✅
- **Note:** This function is not called by the daily/weekly/monthly schedulers directly — they call `finish_sync_batch()` which handles `sync_state` internally. `update_sync_state()` exists for manual or future use.

### `write_search_terms()` (`db/writers.py` lines 607–740)

- **Returns:** `int` — count of attempted upserts (`len(rows)`), not confirmed DB writes ✅ (documented in docstring lines 618–619)
- **Tri-state safety:** `is_flagged_waste` is never set or touched by this writer ✅
- **Conflict resolution:** ON CONFLICT preserves `is_flagged_waste`, `junk_category`, `matched_pattern` by omitting them from DO UPDATE ✅
- **Empty input:** returns `0` safely ✅
- **Row-level date preservation:** `source_date` resolved from `row.get("date") or row.get("source_date")`, with fallback to today ✅
- **Bulk upsert:** uses `executemany()` ✅
- **Errors:** Caught and logged; returns `0` on failure ✅
- **Risk:** `executemany` with `ON CONFLICT DO UPDATE` makes `cur.rowcount` unreliable — the writer correctly uses `len(rows)` as the return value. Callers in `scheduler/daily.py` use `_persistence_succeeded()` which treats `> 0` as success — this is correct for non-empty input but means a single failed row among many successful ones is invisible. 🟡 Medium.

### `write_gclid_attribution()` (`db/writers.py` lines 1142–1272)

- **Returns:** `int` — attempted upsert count (`len(rows)`) ✅
- **Blank GCLID guard:** rows without a GCLID are skipped with a warning ✅
- **Upsert conflict clause:** preserves existing `run_id`, `sync_batch_id` (COALESCE existing over excluded) — audit trail stable ✅
- **Useful value preservation:** all optional fields use `COALESCE(EXCLUDED.x, gclid_attribution.x)` — no overwrite with NULL ✅
- **Multi-deal rows:** distinct attribution keys preserve multiple deal rows per contact/GCLID ✅
- **Errors:** Never raises; returns `0` on failure ✅
- **Risk:** Same `len(rows)` approximation as `write_search_terms` — see above. 🟡 Medium.

### `write_gclid_coverage_snapshot()` (`db/writers.py` lines 1275–1330)

- **Returns:** `1` on success, `0` on empty input or DB unavailable ✅
- **JSONB:** full coverage dict serialised; `json.dumps` failure caught and stored as `None` ✅
- **Errors:** Never raises ✅

### `write_leads()` (referenced at `scheduler/daily.py` line 109, `scheduler/weekly.py` line 69)

- Not modified by PR-ADS-039 through PR-ADS-049. Return semantics (`int` row count or `0`) unchanged ✅

### Writer Function Summary

| Writer | Good | Risk | Recommended Fix |
|--------|------|------|-----------------|
| `start_sync_batch()` | Returns 0 safely; validates inputs; never raises | Return value 0 conflates DB unavailable with invalid input | Add a sentinel or separate return code in PR-ADS-051 (low priority) |
| `finish_sync_batch()` | Preserves watermark on failure; validates status; never raises | last_source_date fallback to batch date_to is approximate | Acceptable; document clearly |
| `update_sync_state()` | COALESCE guards prevent null overwrites; never raises | Not called by schedulers directly — purpose is manual/future use | Add note in docstring |
| `write_search_terms()` | Tri-state preserved; bulk upsert; date-level grain; never raises | len(rows) ≠ confirmed writes; partial failures invisible | Document limitation; add per-row error logging in future PR |
| `write_gclid_attribution()` | Attribution key stable; multi-deal rows preserved; null preservation; never raises | Same len(rows) approximation | Document limitation |
| `write_gclid_coverage_snapshot()` | Returns 1/0 reliably; JSONB fallback; never raises | None | None |

---

## 5. Scheduler Audit

### `scheduler/daily.py`

**Reviewed:** lines 70–228

- `hubspot/contacts` freshness updated only after `_persistence_succeeded(contacts, contacts_written)` is confirmed ✅ (lines 110–122)
- `windsor/search_terms` freshness updated only after `_persistence_succeeded(search_terms, st_count)` is confirmed ✅ (lines 161–174)
- `_persistence_succeeded()` correctly distinguishes zero-row syncs (legitimate empty) from failed persistence (rows fetched, zero written) ✅ (lines 51–67)
- Explicit comment block at lines 184–192 documents datasets that are **not** tracked in sync_state: `windsor/campaigns`, `windsor/keywords`, `windsor/geo`, `hubspot/deals`, `gclid/matches` ✅
- `gclid/matches` has no sync tracking in daily — correct: GCLID match runs weekly/monthly only ✅
- No all-time daily fetch ✅
- No external writes (Google Ads / HubSpot) ✅
- No backfill trigger ✅
- No `sync_state` update for `windsor/campaigns` despite pulling `pull_campaign_performance(days_back=2)` — correct; campaigns are analysis output, not raw source facts ✅

### `scheduler/weekly.py`

**Reviewed:** lines 24–229

- `gclid/matches` tracked only after `write_gclid_attribution()` succeeds and `row_count > 0` ✅ (lines 160–177)
- `if matched_rows and row_count == 0: raise RuntimeError(...)` guard prevents false-fresh state when attribution rows exist but writes fail ✅ (lines 160–163)
- `finish_sync_batch(status='failed')` called in exception handler — sync_state updated to failed correctly ✅ (lines 179–185)
- `write_gclid_coverage_snapshot()` called after attribution write — coverage snapshot is associated with the same batch ✅
- No sync_state tracking for `windsor/campaigns`, `windsor/keywords`, `windsor/geo`, `hubspot/contacts`, `hubspot/deals` — campaigns/geo/keywords are not raw source facts; contacts written without batch tracking in weekly (only daily tracks contacts freshness). 🟡 Medium risk: weekly contact writes do not update `hubspot/contacts` freshness state.
- Search terms written in weekly via `write_search_terms(run_id, search_terms)` without a sync batch — no `sync_state` update in weekly. Weekly search terms freshness not tracked. 🟡 Medium.
- No external writes ✅
- No backfill trigger ✅

### `scheduler/monthly.py`

**Reviewed:** lines 35–383

- Same GCLID match guard pattern as weekly ✅ (lines 263–266)
- Search terms written without sync batch tracking (same as weekly) — same 🟡 Medium risk
- No external writes ✅
- No backfill trigger ✅
- `pull_search_terms(days_back=30)` comment notes the 14d Windsor limitation and does not claim 30-day coverage ✅ (lines 126–127)

### Scheduler Summary

| Scheduler | Correctly Tracks Freshness | Risk | Recommended Fix |
|-----------|---------------------------|------|-----------------|
| `daily.py` | hubspot/contacts, windsor/search_terms | None | None |
| `weekly.py` | gclid/matches | Weekly hubspot/contacts writes do not update sync_state; search_terms freshness not tracked in weekly | Add batch tracking for search_terms in weekly/monthly in PR-ADS-051 or document the gap |
| `monthly.py` | gclid/matches | Same as weekly | Same |

---

## 6. API Contract Audit

### `/api/datasets/freshness` (`api/server.py` lines 2739–2842)

- **Auth:** `require_auth` ✅
- **Read-only:** yes; no writes, no sync execution ✅
- **Pagination:** N/A — returns full list of known datasets (max 7 rows) ✅
- **DB unavailable:** returns `{"datasets": [], "summary": {...}, "db_unavailable": true}` ✅
- **Unknown datasets:** datasets not yet in sync_state are returned as `status: "unknown"` — correctly distinct from `status: "failed"` ✅
- **Risk:** `db_unavailable` key is present in the safe empty response but absent in the success response — inconsistency with other endpoints that omit the key on success. 🟢 Low.

### `/api/search-terms` (`api/server.py` lines 2892–3058)

- **Auth:** `require_auth` ✅
- **Read-only:** yes ✅
- **Cursor pagination:** `(source_date DESC, id DESC)` — matches `idx_search_terms_cursor` ✅
- **Invalid cursor:** raises `HTTPException(status_code=400)` ✅
- **DB unavailable:** returns `_safe_empty` with `"db_unavailable": true` ✅
- **Query params:** `days`, `campaign`, `match_type`, `q`, `waste_only`, `min_spend`, `limit`, `cursor` — all documented in `API_CONTRACT.md`? **Risk:** API_CONTRACT.md does not yet document `/api/search-terms`. 🟠 High.
- **Loaded-page summary:** `pagination.has_more` and `pagination.next_cursor` correctly reflect page state ✅
- **Waste-only filter:** `is_flagged_waste IS TRUE` — correctly excludes NULL (unanalysed) and FALSE (clean) ✅

### `/api/gclid-attribution` (`api/server.py` lines 3100–3290)

- **Auth:** `require_auth` ✅
- **Read-only:** yes ✅
- **Cursor pagination:** `(created_at DESC, id DESC)` — matches `idx_gclid_attr_cursor` ✅
- **Invalid cursor:** raises `HTTPException(status_code=400)` ✅
- **DB unavailable:** returns `_safe_empty` with `"db_unavailable": true` ✅
- **Query params:** `days`, `campaign`, `gclid`, `contact_id`, `deal_id`, `match_status`, `limit`, `cursor`
- **Risk:** API_CONTRACT.md does not yet document `/api/gclid-attribution`. 🟠 High.
- **Summary block:** `loaded_rows`, `matched_rows`, `url_fallback_rows`, `unmatched_rows`, `total_deal_amount_usd_loaded` — all scoped to loaded page, not total table ✅
- **Labels note:** key `total_deal_amount_usd_loaded` name explicitly encodes "loaded" scope ✅

### `/api/gclid-coverage` (`api/server.py` lines 3690–3753)

- **Auth:** `require_auth` ✅
- **Read-only:** yes ✅
- **Pagination:** none (returns all rows in the date window) — acceptable for a time-series snapshot endpoint ✅
- **DB unavailable:** returns `{"days": days, "rows": [], "db_unavailable": true}` ✅
- **DATE predicate:** `WHERE snapshot_date >= CURRENT_DATE - %s` — correct for DATE column ✅
- **Risk:** API_CONTRACT.md does not document `/api/gclid-coverage`. 🟠 High.

### `/api/attribution/quality` (`api/server.py` lines 3475–3687)

- **Auth:** `require_auth` ✅
- **Read-only:** yes ✅
- **Pagination:** N/A — aggregate result ✅
- **DB unavailable:** returns `{"days": days, "summary": {}, "rates": {}, "signals": [], "db_unavailable": true}` ✅
- **OCT language:** none detected; explicit comment at line 3309: "Forbidden language: OCT ready, upload, push, fix, guaranteed, qualified revenue" ✅
- **Signal labels:** "Local warehouse is fresh" / "Local warehouse may be stale" — honest; does not claim external API freshness ✅
- **Risk:** API_CONTRACT.md does not document `/api/attribution/quality`. 🟠 High.

### `/api/campaign-detail` (existing, referenced by PR-ADS-047/049)

- **Auth:** `require_auth` ✅
- **Read-only:** yes ✅
- **Attribution preview:** added as part of drawer — reads from `gclid_attribution` for the given campaign ✅
- **Risk:** API_CONTRACT.md coverage of campaign-detail should be verified after PR-ADS-047 additions.

### Existing endpoints `/api/waste`, `/api/keywords`, `/api/geo`

- No changes in PR-ADS-039 through PR-ADS-049 ✅
- Documented in API_CONTRACT.md ✅

### API Endpoint Summary

| Endpoint | Pagination | DB Unavailable | Auth | Risk | Recommended Fix |
|----------|------------|----------------|------|------|-----------------|
| `/api/datasets/freshness` | N/A | ✅ db_unavailable | ✅ | db_unavailable absent on success | Minor: omit key consistently |
| `/api/search-terms` | ✅ cursor (source_date, id) | ✅ db_unavailable | ✅ | Not documented in API_CONTRACT.md | Add to API_CONTRACT.md in PR-ADS-051 |
| `/api/gclid-attribution` | ✅ cursor (created_at, id) | ✅ db_unavailable | ✅ | Not documented in API_CONTRACT.md | Add to API_CONTRACT.md in PR-ADS-051 |
| `/api/gclid-coverage` | None (time-series) | ✅ db_unavailable | ✅ | Not documented in API_CONTRACT.md | Add to API_CONTRACT.md in PR-ADS-051 |
| `/api/attribution/quality` | N/A | ✅ db_unavailable | ✅ | Not documented in API_CONTRACT.md | Add to API_CONTRACT.md in PR-ADS-051 |
| `/api/waste` | None | ✅ db_unavailable | ✅ | None | None |
| `/api/keywords` | None | ✅ db_unavailable | ✅ | None | None |
| `/api/geo` | None | ✅ db_unavailable | ✅ | None | None |

---

## 7. Cursor Pagination Audit

### `/api/search-terms` cursor helpers (`api/server.py` lines 2859–2889)

```
Encode: base64url(json({"source_date": str, "id": int})).rstrip("=")
Decode: pad with "=" * (-len(token) % 4); validate source_date as ISO date; validate id > 0
```

- Padding: standard `(-len(token) % 4)` formula ✅
- Invalid cursor: raises `ValueError` → caught and re-raised as `HTTPException(400)` ✅
- Cursor fields: `source_date`, `id` — match `ORDER BY source_date DESC, id DESC` ✅
- Composite index `idx_search_terms_cursor` covers `(source_date DESC, id DESC)` ✅
- No offset pagination ✅
- Tie-breaker: `id` as secondary sort — no duplicate rows between pages ✅
- Stable sort: `id` is a serial PK, always unique ✅

### `/api/gclid-attribution` cursor helpers (`api/server.py` lines 3070–3097)

```
Encode: base64url(json({"created_at": datetime.isoformat(), "id": int})).rstrip("=")
Decode: pad with "=" * (-len(token) % 4); validate created_at as ISO datetime; validate id > 0
```

- Padding: same formula ✅
- Invalid cursor: raises `ValueError` → `HTTPException(400)` ✅
- Cursor fields: `created_at`, `id` — match `ORDER BY created_at DESC, id DESC` ✅
- Composite index `idx_gclid_attr_cursor` covers `(created_at DESC, id DESC)` ✅
- No offset pagination ✅
- Stable sort: `id` as tie-breaker ✅

### Duplication Assessment

The two cursor implementations are structurally identical (same base64url encoding formula, same padding strategy, same validation pattern) but implemented separately. The only difference is the cursor field names (`source_date`/`id` vs `created_at`/`id`) and their types (`date` vs `datetime`).

**Risk:** 🟠 High — code duplication means a future bug fix or enhancement must be applied in two places. A shared cursor encode/decode helper with field-name and type parameters would reduce drift risk.

**Recommendation for PR-ADS-051:** Extract `_encode_cursor(fields: dict) -> str` and `_decode_cursor(token: str, field_specs: dict) -> dict` shared helpers. Retain the existing per-endpoint wrappers as thin adapters.

---

## 8. Frontend UI Audit

### Dataset Freshness Panel (`static/app.js`)

- **No action/write buttons** — display-only ✅
- **No OCT language** ✅
- **unknown ≠ failed** — `status: "unknown"` rendered distinctly from `status: "failed"` in freshness panel ✅
- **stale display-only** — freshness age shown as informational; no action triggered ✅

### Search Terms Page (`static/app.js` lines 2040–2250)

- **No action/write buttons** ✅
- **KPI labels:** `"Loaded Terms"`, `"Loaded Spend"`, `"Flagged Waste"`, `"Unanalyzed"`, `"Avg CPC"` — all clearly scoped to loaded page ✅ (lines 2156–2172)
- **unanalysed ≠ clean** — `is_flagged_waste === null` shown as `"Unanalyzed"` badge, not "clean" ✅
- **cursor_error state handled** — `searchTermsStatus = "cursor_error"` state exists; `Load More` hidden on cursor errors ✅ (lines 2129, 2133–2136)
- **filters reset cursor** — `loadSearchTerms({ reset: true })` resets `searchTermsNextCursor` on filter change ✅ (line 412)
- **Risk:** The `renderSearchTermsKPIs()` function computes `waste_only` and `unanalysed` counts from the **already-filtered visible rows** (client-side subset), not from the full backend dataset. A user with `waste_only=true` filter active will see KPIs reflecting only loaded flagged rows — counts may appear lower than actual totals. 🟡 Medium.

### GCLID Attribution Page (`static/app.js` lines 2296–2600)

- **No action/write buttons** ✅
- **KPI labels:** `"Loaded Rows"`, `"Matched Rows"`, `"URL Fallback Rows"`, `"Unmatched Rows"`, `"Loaded Deal Amount"` — all explicitly loaded-page scope ✅ (lines 2339–2356)
- **cursor_error state handled** — `gclidStatus = "cursor_error"` state exists; Load More hidden on cursor error ✅ (lines 2428, 2502–2512)
- **Campaign prefill from drawer:** `gclidAttributionPrefill` set before navigation, consumed and cleared immediately on page entry ✅ (lines 418–421) — no stale state risk ✅

### Campaign Drawer Attribution Preview (PR-ADS-047)

- **Read-only** — drawer shows attribution evidence only, no write controls ✅
- **Campaign prefill consumed once** — `gclidAttributionPrefill = null` after use ✅

### Campaign Drawer Attribution Quality Overlay (PR-ADS-049)

- **No OCT language** — quality signal cards say "strong/moderate/weak match coverage", "local warehouse freshness" — no OCT readiness claims ✅
- **Freshness honest** — "Local warehouse is fresh" / "Local warehouse may be stale" — not "Google Ads data is fresh" ✅
- **unknown ≠ failed** — `status: "unknown"` rendered as unknown, not error ✅

### GCLID Attribution Quality Panel (PR-ADS-048)

- **No action/write buttons** ✅
- **Signal detail texts verified** — all use hedged language: "loaded attribution rows", "local warehouse freshness only" ✅

### UI Surface Summary

| UI Surface | Good | Risk | Recommended Fix |
|------------|------|------|-----------------|
| Dataset Freshness panel | No actions; unknown/failed distinguished; stale is display-only | None | None |
| Search Terms page | KPIs labelled as loaded-page; unanalysed ≠ clean; cursor_error handled | KPI counts computed from client-side visible rows; waste_only filter counts may differ from total | Label KPIs with "(loaded)" suffix or add a backend total count endpoint in PR-ADS-052 |
| GCLID Attribution page | KPIs explicitly "Loaded …"; cursor_error handled; no action buttons | None | None |
| Campaign drawer attribution preview | Read-only; prefill consumed and cleared immediately | None | None |
| Campaign drawer quality overlay | No OCT language; honest freshness language; unknown ≠ failed | None | None |
| GCLID Attribution quality panel | No action buttons; hedged signal language | None | None |

---

## 9. Read-Only Doctrine Audit

A search was performed across all files changed in PR-ADS-039 through PR-ADS-049 for forbidden action/control language.

**Files searched:**
- `db/schema.py`
- `db/writers.py`
- `api/server.py`
- `scheduler/daily.py`
- `scheduler/weekly.py`
- `scheduler/monthly.py`
- `static/app.js`
- `docs/API_CONTRACT.md`
- `docs/BACKFILL_RUNBOOK.md`

**Forbidden phrases (as action controls):**
- upload conversion ❌ Not found
- push (as action) ❌ Not found
- sync to Google Ads ❌ Not found
- send to HubSpot ❌ Not found
- retry attribution ❌ Not found
- fix attribution ❌ Not found
- mark matched ❌ Not found
- mark unmatched ❌ Not found
- apply negative ❌ Not found
- pause campaign ❌ Not found
- increase budget ❌ Not found
- update CRM ❌ Not found
- OCT ready ❌ Not found

**Correctly negated (documentation-only context):**
- `api/server.py` line 3117: "Does not upload offline conversions. Does not write to Google Ads or HubSpot." ✅
- `api/server.py` line 3309: "Forbidden language: OCT ready, upload, push, fix, guaranteed, qualified revenue." (docstring enforcement note) ✅
- `docs/BACKFILL_RUNBOOK.md`: "It never writes to Google Ads or HubSpot." ✅

**Verdict:** 🟢 No read-only doctrine violations found.

---

## 10. Documentation Consistency Audit

### `docs/API_CONTRACT.md`

- **Endpoints documented:** `/api/campaigns`, `/api/leads`, `/api/deals`, `/api/waste`, `/api/runs`, `/api/geo`, `/api/keywords`, `/api/leads/country-summary`, `/api/campaign-detail` ✅
- **Missing:** `/api/datasets/freshness`, `/api/search-terms`, `/api/gclid-attribution`, `/api/gclid-coverage`, `/api/attribution/quality` — **all four new endpoints from PR-ADS-039 through PR-ADS-048 are not yet documented in API_CONTRACT.md**. 🟠 High.
- `/api/campaign-detail` — verify it reflects PR-ADS-047 attribution preview additions.

### `docs/BACKFILL_RUNBOOK.md`

- Section 5 (`Dataset Notes`) under `GCLID Attribution` states: "gclid_attribution table planned for PR-ADS-044". This is now stale — the table was created in PR-ADS-044. 🟡 Medium (docs drift).
- `## 8. Daily Sync vs Historical Backfill` section references PR-ADS-042 correctly ✅
- `## Dataset Freshness UI (PR-ADS-045)` section at bottom correctly describes the freshness panel as display-only ✅
- Execute mode caveat ("not yet implemented") remains accurate as of this audit ✅

### `docs/10_DATA_WAREHOUSE_READINESS_AUDIT.md`

- Update notes for PR-ADS-041, PR-ADS-042, PR-ADS-044 are present ✅
- No update notes for PR-ADS-040, PR-ADS-043, PR-ADS-045 through PR-ADS-049 — this is expected for a previous-phase audit document; the current audit (PR-ADS-050) supersedes it for the new scope ✅
- The "What data is still temporary or discarded?" table lists "GCLID matched records" and "GCLID coverage stats" as not persisted — stale since PR-ADS-044 implemented persistence. 🟡 Medium (noted for awareness; PR-ADS-050 supersedes).

### Attribution Key Documentation

- Schema comment at `db/schema.py` line 340: "SHA1 of gclid|contact_id|(deal_id or first_url)|campaign_name|keyword|match_status" — matches `_make_attribution_key()` implementation ✅
- `first_url` fallback is documented in both schema comment and `_make_attribution_key()` docstring ✅

### Tri-State Flag Documentation

- Schema comment at lines 246–247 and 263–268 documents the tri-state correctly ✅
- `docs/API_CONTRACT.md` does not mention `is_flagged_waste` because `/api/search-terms` is not yet documented — another reason to add the endpoint to the contract. 🟠 High (same item as above).

### Freshness: unknown vs failed

- `api/server.py` `/api/datasets/freshness` returns `status: "unknown"` for datasets not yet in sync_state, correctly distinct from `"failed"` ✅
- Attribution quality signals (`_compute_attribution_quality_signals`) handle `fst == "unknown"` vs `fst == "failed"` distinctly ✅

### Backfill Dry-Run / Execute Documentation

- Runbook clearly states execute mode is not implemented ✅
- Dry-run examples are safe (no API calls, no writes) ✅

---

## 11. Risk Register

| Risk | Severity | Evidence | Recommended PR |
|------|----------|----------|----------------|
| New endpoints not documented in API_CONTRACT.md (`/api/search-terms`, `/api/gclid-attribution`, `/api/gclid-coverage`, `/api/attribution/quality`) | 🟠 High | API_CONTRACT.md reviewed; none of the four new endpoints are present | PR-ADS-051 |
| Duplicated cursor helper logic between `/api/search-terms` and `/api/gclid-attribution` | 🟠 High | `_encode_cursor` / `_decode_cursor` and `_encode_gclid_cursor` / `_decode_gclid_cursor` are structurally identical with different field names | PR-ADS-051 |
| `write_search_terms` / `write_gclid_attribution` return attempted-upsert count, not confirmed-write count | 🟡 Medium | `executemany` with `ON CONFLICT` makes `cur.rowcount` unreliable; writers use `len(rows)` — documented in docstrings but not explicit in scheduler error-checking | PR-ADS-051 (add doc note) |
| Search term ILIKE without pg_trgm extension is a sequential scan | 🟡 Medium | Schema comment at lines 312–318 acknowledges this; no trigram index created | PR-ADS-054 or document as known limitation in PR-ADS-051 |
| Frontend Search Terms KPI counts computed from client-side visible rows (may not reflect full table) | 🟡 Medium | `renderSearchTermsKPIs()` called with `getVisibleSearchTermRows()` — counts reflect loaded/filtered rows only | PR-ADS-052 (backend filter adds total counts) |
| Weekly/monthly search_terms writes do not update hubspot/contacts or windsor/search_terms freshness in sync_state | 🟡 Medium | Only daily.py calls `finish_sync_batch` for these datasets; weekly writes search_terms without batch tracking | PR-ADS-051 (add tracking or document the gap) |
| `BACKFILL_RUNBOOK.md` section 5 says gclid_attribution table is "not yet available" (stale since PR-ADS-044) | 🟡 Medium | `docs/BACKFILL_RUNBOOK.md` line 145 | PR-ADS-051 |
| `docs/10_DATA_WAREHOUSE_READINESS_AUDIT.md` "temporary" table still lists GCLID matched records as not persisted | 🟡 Medium | Stale since PR-ADS-044 | Informational; PR-ADS-050 supersedes for this scope |
| `db_unavailable` key present in `/api/datasets/freshness` safe-empty response but absent on success | 🟢 Low | Inconsistency with other endpoints that return `db_unavailable: true` only in the error path | PR-ADS-051 (minor) |
| `idx_gclid_attr_created` and `idx_gclid_attr_cursor` partially overlap (both cover `created_at DESC`) | 🟢 Low | `db/schema.py` lines 395–400; cursor index is composite and supersedes the single-column index for pagination | PR-ADS-051 (drop `idx_gclid_attr_created` if confirmed unused) |
| `start_sync_batch()` returns `0` for both "DB unavailable" and "invalid inputs" (conflated failure modes) | 🟢 Low | `db/writers.py` lines 800–853 | Future PR (low priority) |

---

## 12. Recommended Cleanup PR Sequence

### PR-ADS-051 — Data Foundation Cleanup Patch

Surgical fixes from this audit only. No schema changes. No new features.

**Candidate fixes:**
1. **Shared cursor helper** — extract `_encode_keyset_cursor(fields: dict) -> str` and `_decode_keyset_cursor(token: str, field_specs: dict) -> dict` to eliminate the duplicate implementations in `/api/search-terms` and `/api/gclid-attribution`.
2. **API_CONTRACT.md additions** — document `/api/datasets/freshness`, `/api/search-terms`, `/api/gclid-attribution`, `/api/gclid-coverage`, `/api/attribution/quality` with full request/response shapes, pagination notes, and DB-unavailable shapes.
3. **BACKFILL_RUNBOOK.md correction** — update stale "gclid_attribution table not yet available" note in section 5 to reflect PR-ADS-044 implementation.
4. **Weekly/monthly search_terms sync tracking** — add `start_sync_batch` / `finish_sync_batch` around `write_search_terms` calls in weekly and monthly schedulers, consistent with daily pattern. (Or document the intentional gap.)
5. **Minor index note** — add a comment recommending `DROP INDEX idx_gclid_attr_created` if the composite cursor index renders it redundant (confirm with EXPLAIN ANALYZE first).
6. **pg_trgm documentation** — add a `docs/` note or runbook entry explaining the trigram extension install path for operators who want to enable fast `?q=` search on large search_terms tables.

---

### PR-ADS-052 — Search Terms Backend State Filter

**Scope:** Add backend support for waste state filtering:

```
GET /api/search-terms?waste_state=flagged|clean|unanalysed|all
```

**Reason:** Current `waste_only=true` filter returns only flagged rows; clean (`FALSE`) and unanalysed (`NULL`) filtering is performed client-side on the already-loaded page. For large tables, this means users cannot get an accurate count of unanalysed terms without loading all rows. Backend filtering with `waste_state=` maps directly to `is_flagged_waste IS TRUE` / `IS FALSE` / `IS NULL` predicates and can return accurate server-side counts.

**Non-goals:** No schema change. No new endpoint. The existing `waste_only` parameter should be retained for backwards compatibility.

---

### PR-ADS-053 — Dataset Freshness Page Upgrade

**Scope:** If warranted, add a sync batch history view to the Dataset Freshness panel.

**Still read-only.** The view would show recent rows from `sync_batches` for each dataset — useful for debugging sync failures.

**Prerequisite:** PR-ADS-051 must add API_CONTRACT.md entry for `/api/datasets/freshness` first.

---

### PR-ADS-054 — N-Gram Readiness Audit

**Scope:** Audit-only PR before building any n-gram or text classification engine on top of the `search_terms` table.

**Key questions to answer:**
- Is the pg_trgm extension available in the production Postgres instance?
- What is the current row count of `search_terms`?
- What is the current query time for `?q=` without trigram?
- What classification patterns are used in `config/junk_patterns.yaml`?
- Are there overlapping pattern definitions that would produce false positives in an automated classifier?

---

## 13. Non-Goals

This audit document explicitly does not:

- Change any code
- Change any database schema
- Change any API endpoint
- Change any UI component
- Change any scheduler
- Change any connector
- Fetch live data
- Write to Google Ads
- Write to HubSpot
- Upload offline conversions (OCT)
- Push negative keywords
- Make bid, budget, or campaign changes
- Add AI chat or generative features

---

## 14. Phase 1 Read-Only Checklist

- [x] Audit-only
- [x] No code changed
- [x] No schema changed
- [x] No API changed
- [x] No UI changed
- [x] No scheduler changed
- [x] No connector changed
- [x] No live data fetched
- [x] No Google Ads writes
- [x] No HubSpot writes
- [x] No OCT upload
- [x] No negative keyword push
- [x] No bid/budget/campaign changes
