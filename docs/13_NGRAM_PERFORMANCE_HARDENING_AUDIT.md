# N-Gram Performance Hardening Audit

**Document:** `docs/13_NGRAM_PERFORMANCE_HARDENING_AUDIT.md`
**Roadmap ID:** PR-ADS-057
**Phase:** 1.5 — Search-Term Intelligence / Performance Hardening
**Owner:** Youssef Awwad
**Audit date:** 2026-05-08
**Status:** Audit-only. No code changed. No schema changed. No API changed.

Depends on: PR-ADS-056 / PR-ADS-056A
Unblocks: PR-ADS-058 — Negative Candidate Readiness Audit

---

## 1. Executive Verdict

### Is the current dynamic n-gram prototype safe to keep?

**Yes, for current data volumes.** The live-aggregation endpoint introduced in PR-ADS-055 is safe to keep as the primary implementation provided the `search_terms` table remains under ~10,000 rows per filtered request window. The existing `_NGRAMS_SOURCE_ROW_CAP = 10_000` enforces this limit at query time.

### At what data volume does it become risky?

At approximately **10,000 source rows per request** the Python aggregation loop becomes measurably slow for interactive use. At 100,000+ rows the live endpoint is not viable without scheduled materialization. The cap currently prevents hitting that ceiling, but as the table grows the cap itself may begin hiding significant portions of the data — at which point materialization should be evaluated.

### Should the next implementation use live endpoint, cache, scheduled materialized table, or hybrid?

**Keep the live endpoint only for now.** Do not materialize or cache until there is evidence of performance pain (response time consistently above 3 seconds, or cap routinely applied across all active customers). A cache layer is a reasonable intermediate step if the same filters are hit repeatedly; a scheduled materialized table is warranted only once the endpoint becomes operationally slow.

### Should PR-ADS-058 still be the negative-candidate audit, or should performance hardening come first?

**Proceed with PR-ADS-058 as the negative-candidate readiness audit.** Performance is acceptable at current volumes. Performance hardening (benchmark tooling, optional trigram index, or materialization planning) can proceed in parallel or as a follow-on track but should not block the negative-candidate audit gate.

### Recommended next build PR

**PR-ADS-058 — Negative Candidate Readiness Audit** (audit-only, no candidates yet).

---

## 2. Current N-Gram Architecture

### Source files

| Component | File |
|---|---|
| Aggregation logic | `analysis/ngrams.py` |
| HTTP endpoint | `api/server.py` — `GET /api/search-terms/ngrams` (line 3415) |
| UI page | `static/app.js` — `loadNgrams`, `renderNgramsKPIs`, `renderNgramsTable` |
| Data source table | `search_terms` (defined in `db/schema.py` lines 248–319) |

### Runtime constants (verified in `api/server.py` lines 3372–3376)

| Constant | Value |
|---|---|
| `_NGRAMS_DEFAULT_DAYS` | 14 |
| `_NGRAMS_MAX_DAYS` | 30 |
| `_NGRAMS_DEFAULT_LIMIT` | 100 |
| `_NGRAMS_MAX_LIMIT` | 250 |
| `_NGRAMS_SOURCE_ROW_CAP` | 10,000 |

### Supported n values

Unigrams (n=1), bigrams (n=2), and trigrams (n=3). Any other value is rejected at the API layer with HTTP 400. Default request is `n=1,2,3`.

### Sorting

Results are sorted by `total_spend_usd DESC → row_count DESC → ngram ASC` (Python sort in `aggregate_ngrams`). Source rows are fetched from the database `ORDER BY spend_usd DESC NULLS LAST, source_date DESC, id DESC` to ensure highest-spend rows are captured within the cap.

### Stopword logic

Stopwords are loaded from `config/ngram_stopwords.yaml` via `get_stopword_tokens()`. They cover common English articles/prepositions/conjunctions, Spanish function words, and Arabic token-level stopwords (من, في, على, إلى, عن, مع, and others). Arabic text also continues to undergo character-level normalization (tatweel, diacritic, and alef-form removal) before tokenization. As of PR-ADS-060, Arabic stopword filtering operates at both the character level and the token level.

**Business/waste-signal tokens that are intentionally not stopworded** (verified in source):
`freight`, `forwarding`, `logistics`, `shipping`, `software`, `system`, `cargo`, `customs`, `warehouse`, `gratis`, `free`, `job`, `jobs`, `student`, `training`.

### Language detection logic

Detection runs per n-gram phrase via `detect_script_or_language(phrase)` called once per output row during the aggregation build step (`analysis/ngrams.py` line 363). It is a heuristic with three outcomes: `arabic`, `spanish`, `english_or_latin`. It does not run per source row; it runs on the already-normalized output phrase.

### Current data flow

```
search_terms table
  → GET /api/search-terms/ngrams DB query
      (WHERE source_date >= NOW() - INTERVAL + optional filters)
      (ORDER BY spend_usd DESC NULLS LAST, source_date DESC, id DESC)
      (LIMIT _NGRAMS_SOURCE_ROW_CAP + 1)
  → cap detection (fetch cap+1, detect overflow, trim to cap)
  → Python dict conversion
  → analysis/ngrams.aggregate_ngrams()
      → per-row: tokenize_search_term → build_ngrams (n=1,2,3)
      → per-ngram: accumulate metrics in defaultdict
      → build output list, sort by spend DESC
  → slice to limit
  → JSON response with summary + data_quality block
  → N-Gram UI page (static/app.js)
```

### Current constraints (by design)

- No materialized n-gram table
- No scheduled n-gram computation
- No negative keyword candidates
- No scoring or attention-status fields
- No writes to Google Ads or HubSpot
- Response includes `row_cap_applied` flag in `data_quality` when cap is hit

---

## 3. Current Query and Runtime Risk

### Query behavior (verified in `api/server.py` lines 3499–3546)

| Behavior | Confirmed |
|---|---|
| Filters by `source_date` using `NOW() - INTERVAL '1 day' * days` | ✅ Yes |
| Supports `campaign` filter (exact, canonicalized) | ✅ Yes |
| Supports `match_type` filter (`ILIKE '%...%'`) | ✅ Yes |
| Supports `waste_state` filter (flagged / clean / unanalyzed / all) | ✅ Yes |
| Supports `q` filter (`search_term ILIKE '%...%'`) | ✅ Yes |
| Supports `min_spend` filter | ✅ Yes |
| Orders by `spend_usd DESC NULLS LAST, source_date DESC, id DESC` | ✅ Yes |
| Caps source rows at `_NGRAMS_SOURCE_ROW_CAP` | ✅ Yes |
| Fetches `cap + 1` to detect truncation accurately | ✅ Yes |
| Avoids full-table scan by default (date filter applied first) | ✅ Yes — `source_date` has an index (`idx_search_terms_source_date`) |

### Runtime risk inventory

| Risk | Description |
|---|---|
| `q ILIKE '%term%'` is a sequential scan | The `search_terms` table has no trigram index. The schema comment (`db/schema.py` lines 312–318) documents this gap and states a `pg_trgm`-backed GIN index is required but not yet created. For small tables this is acceptable; for large tables it degrades to full-scan. |
| Broad date windows scan many rows | At `days=30` with no campaign filter, the query may scan the full 30-day window. The cap limits rows returned but not rows scanned. |
| Python tokenization cost grows with row count | For each of the N capped source rows, the system runs `normalize_search_term` + `split` + stopword filter + n-gram enumeration. At 10,000 rows with average 5 tokens and n=1,2,3, this produces up to ~130,000 n-gram accumulation operations per request. |
| n=1,2,3 triples output work | Requesting all three n lengths triples the tokenization pass relative to a single-n request. |
| Repeated UI Apply clicks re-run full aggregation | Each click triggers a full DB query + Python aggregation. There is no debounce on the Apply button and no response caching. |
| Multiple concurrent users amplify CPU | Each simultaneous request runs an independent Python aggregation in the server process. On Render free/starter tier (shared single-process), this can saturate the CPU. |
| Language detection runs on phrases, not source rows | `detect_script_or_language` is called once per unique output n-gram, not once per source row. This is efficient from a performance standpoint because the phrase set is usually smaller than the source row count. A future shift to row-level detection would be for better source-level attribution/accuracy propagation, not because phrase-level detection is currently a performance bottleneck. |

---

## 4. Data Volume Thresholds

The following tiers are **initial operational targets, not hard guarantees.** They are based on estimated Python tokenization cost at 5 tokens/row and n=1,2,3 on a single-core server process. Actual numbers will vary by server tier, token density, and n-gram cardinality.

### Source rows analyzed vs. risk

| Source Rows Analyzed | Risk | Recommendation |
|---|---|---|
| 0 – 2,500 | Low | Live endpoint fine. No action needed. |
| 2,500 – 10,000 | Medium | Live endpoint acceptable with cap and warnings. Consider showing cap warning in UI. |
| 10,000+ | High | Cap is always applied. Consider cache or materialized table if this is routine. |
| 100,000+ | Very High | Scheduled materialization strongly recommended. Live endpoint should not be the primary path. |

### Response time targets

| Response Time | Meaning |
|---|---|
| < 1s | Good — interactive experience |
| 1 – 3s | Acceptable — slight lag, tolerable |
| 3 – 8s | Warning — user may abandon; investigate |
| > 8s | Not acceptable for interactive UI |

---

## 5. Measurement Plan

Before changing the architecture, measure actual endpoint performance at real data volumes. The following is a proposed future benchmark command — **not implemented in this PR.**

```bash
# Set BASE_URL and TOKEN in your shell before running
time curl -s "$BASE_URL/api/search-terms/ngrams?days=14&n=1,2,3&limit=100" \
  -H "Cookie: ads_session=$TOKEN" > /tmp/ngrams_14d.json

# Inspect summary
python3 -c "import json,sys; d=json.load(open('/tmp/ngrams_14d.json')); print(d['summary'], d.get('data_quality', {}))"
```

### Test scenarios to cover

| Scenario | Parameters |
|---|---|
| Default 14d all campaigns | `days=14` |
| 30d all campaigns | `days=30` |
| 14d one high-volume campaign | `days=14&campaign=<name>` |
| 14d broad match only | `days=14&match_type=broad` |
| 14d with q filter | `days=14&q=freight` |
| 14d waste_state=unanalyzed | `days=14&waste_state=unanalyzed` |
| limit=250 | `days=14&limit=250` |

### Data to capture per scenario

- End-to-end response time (wall clock)
- `summary.source_rows_analyzed`
- `data_quality.row_cap_applied` (true/false)
- DB query time if server-side logging is present
- Python aggregation time if measurable (requires instrumentation)
- JSON payload size (`wc -c /tmp/ngrams_*.json`)
- Server CPU/memory if Render metrics are available

**Do not implement benchmark tooling in this audit PR.**

---

## 6. Materialization Options

### Option A — Keep live endpoint only (current state)

**Pros:**
- Simple — no schema, no scheduler
- Always shows freshest data
- Low maintenance overhead
- Already implemented and working

**Cons:**
- Repeated computation on every request
- May become slow at scale (10k+ rows per filter)
- Cannot show historical trend over time
- CPU load grows linearly with concurrent users

**Recommended if:** Source rows per request stay under 10,000 and response time stays under 3 seconds.

---

### Option B — In-memory / short TTL cache

**Pros:**
- Avoids repeated computation for identical filter combinations
- No schema changes required
- Straightforward to implement with a dict keyed on query params

**Cons:**
- Not durable — Render instance restart clears the cache
- Cache invalidation becomes complex if filters are diverse
- Risky if multiple Render instances are deployed (cache is not shared)
- Does not help if every request uses different filter combinations

**Recommended if:** The same filter combinations (e.g. default 14d, all campaigns) are requested frequently, data volume is moderate, and historical trend views are not needed.

---

### Option C — Scheduled materialized table

**Pros:**
- Fast UI — reads from pre-computed table, no Python aggregation per request
- Supports historical trending (run-by-run comparison)
- Stable reporting — UI is unaffected by live table growth
- Supports larger date windows without query-time cost
- Eliminates per-request Python computation entirely

**Cons:**
- Requires schema change (new table + indexes)
- Requires scheduler change (new daily/weekly job)
- Data is stale between runs (typically 24h lag)
- Must define grain carefully — date range, campaign scope, n-value scope
- More complex to maintain and debug

**Recommended if:** The live endpoint becomes consistently slow, n-grams become an operational reporting page, users need historical trend views, or `search_terms` table grows materially (>100k rows).

---

### Option D — Hybrid (live + materialized)

- Live endpoint for narrow/filtered requests (specific campaign, short window)
- Materialized table for broad/default views (all campaigns, 14–30d)

**Pros:** Best of both — freshness for targeted queries, speed for default views.
**Cons:** Most complex to build and maintain. Two code paths, two data sources, cache invalidation logic.

**Likely the long-term best option, but not the first build.** Defer until Option A or B is proven insufficient.

---

## 7. Proposed Materialized Table — If Needed Later

**This table must not be created in PR-ADS-057.** The following is architecture planning only.

```sql
CREATE TABLE IF NOT EXISTS search_term_ngrams (
  id SERIAL PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  sync_batch_id INTEGER REFERENCES sync_batches(id) ON DELETE SET NULL,

  source_date_from DATE NOT NULL,
  source_date_to DATE NOT NULL,

  campaign_name TEXT,
  match_type TEXT,
  waste_state TEXT DEFAULT 'all',

  ngram TEXT NOT NULL,
  n INTEGER NOT NULL,
  language TEXT,

  row_count INTEGER DEFAULT 0,
  unique_search_terms INTEGER DEFAULT 0,
  campaigns_count INTEGER DEFAULT 0,
  ad_groups_count INTEGER DEFAULT 0,
  keywords_count INTEGER DEFAULT 0,

  total_spend_usd NUMERIC(10,2) DEFAULT 0,
  total_clicks INTEGER DEFAULT 0,
  total_impressions INTEGER DEFAULT 0,
  google_conversions NUMERIC(8,2) DEFAULT 0,

  flagged_waste_rows INTEGER DEFAULT 0,
  clean_rows INTEGER DEFAULT 0,
  unanalyzed_rows INTEGER DEFAULT 0,
  flagged_waste_spend_usd NUMERIC(10,2) DEFAULT 0,

  campaigns_sample JSONB,
  search_terms_sample JSONB,

  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Recommended indexes (if table is built)

```sql
CREATE INDEX IF NOT EXISTS idx_search_term_ngrams_window
  ON search_term_ngrams(source_date_from, source_date_to);

CREATE INDEX IF NOT EXISTS idx_search_term_ngrams_ngram
  ON search_term_ngrams(ngram);

CREATE INDEX IF NOT EXISTS idx_search_term_ngrams_campaign
  ON search_term_ngrams(campaign_name);

CREATE INDEX IF NOT EXISTS idx_search_term_ngrams_n
  ON search_term_ngrams(n);

CREATE INDEX IF NOT EXISTS idx_search_term_ngrams_spend
  ON search_term_ngrams(total_spend_usd DESC);
```

**Decision gate:** Only build this table if the live endpoint is confirmed slow at production data volumes. Do not speculate — measure first (see Section 5).

---

## 8. Stopword Config Strategy

### Current state (as of PR-ADS-060)

As of PR-ADS-060, stopwords and protected tokens are loaded from `config/ngram_stopwords.yaml`. The analyzer falls back to safe defaults if the config is unavailable. Arabic token-level stopwords are now represented in config (من, في, على, إلى, عن, مع, and others). The hardcoded `_STOPWORDS` frozenset has been removed; all token filtering is now config-driven via `get_stopword_tokens()` and `get_protected_tokens()`.

### Recommended future config

The config file `config/ngram_stopwords.yaml` is now the authoritative source for stopwords and protected tokens. Tuning stopwords no longer requires code changes:

```yaml
# config/ngram_stopwords.yaml
english:
  - the
  - a
  - an
  - for
  - to
  - in
  - of
spanish:
  - de
  - para
  - el
  - la
  - los
  - las
arabic:
  - من
  - في
  - على
  - إلى
  - عن
```

### Protected tokens — must never be stopworded

The following tokens carry waste-signal value and must remain in the vocabulary regardless of stopword configuration changes:

```
freight      forwarding   logistics    shipping
software     system       cargo        customs
warehouse    gratis       free         job
jobs         student      training
```

These are explicitly documented in `analysis/ngrams.py` (lines 35–37 and 177–179) and must be preserved in any future config-based implementation.

### Recommendation

PR-ADS-060 has migrated stopwords to `config/ngram_stopwords.yaml`. Protected tokens are now enforced in config and override stopwords at tokenization time.

---

## 9. Language Handling Performance

### Current implementation (verified in `analysis/ngrams.py`)

| Step | Where it runs |
|---|---|
| Arabic normalization (tatweel, diacritics, alef variants) | `normalize_search_term()` — per source row string |
| Latin accent stripping (NFKD) | `normalize_search_term()` — per source row string |
| Language detection (`detect_script_or_language`) | `aggregate_ngrams()` line 363 — **per output n-gram phrase**, not per source row |

### Audit finding

Language detection runs on output n-gram phrases, not on source rows. This is already efficient because:
- The output phrase set is much smaller than the source row set (many rows produce identical n-grams that are aggregated)
- Each unique `(phrase, n)` pair is detected once

However, the detection runs on the normalized phrase rather than propagating a row-level language label. For Arabic phrases this is correct (Arabic script characters survive normalization). For Spanish, a Spanish source term like "transporte de carga" may produce individual n-gram tokens that don't retain Spanish markers after accent stripping — though the `_SPANISH_DOMAIN_TERMS` set provides a fallback.

### Recommendations

1. **Do language detection once per source row** and propagate the detected language to all n-grams generated from that row. This would give a more accurate label for mixed-language terms and reduce redundant regex searches on output phrases.
2. **Propagate row language to generated n-grams** in `aggregate_ngrams` — pass the source-row language down to the per-phrase accumulator bucket.
3. **Avoid re-detecting per n-gram** unless the phrase set is independently useful (e.g., a bigram from a Spanish source row that loses its Spanish markers post-normalization).

These recommendations are for a future PR. Do not implement in PR-ADS-057.

---

## 10. Search-Term `q` Filter Performance

### Current implementation (verified in `api/server.py` line 3514–3516)

```python
if q:
    conditions.append("search_term ILIKE %s")
    params.append(f"%{q.strip()}%")
```

This is a leading-wildcard `ILIKE` pattern. PostgreSQL cannot use a standard B-tree index for `LIKE '%term%'` queries. Without a trigram index, this falls back to a sequential scan of all rows matching the date filter.

### Current indexing state (verified in `db/schema.py` lines 312–318)

The schema file documents this gap explicitly:

```sql
-- NOTE: Trigram index for /api/search-terms?q= (full-text contains search) requires
-- the pg_trgm extension. If you have DBA access, enable it once with:
--   CREATE EXTENSION IF NOT EXISTS pg_trgm;
--   CREATE INDEX IF NOT EXISTS idx_search_terms_search_term_trgm
--     ON search_terms USING gin (search_term gin_trgm_ops);
-- Until enabled, ?q= filtering is supported but uses a sequential scan.
-- Do NOT add a plain B-tree index — it does not support LIKE '%term%' queries.
```

### Risk assessment

| Table size | Risk |
|---|---|
| < 50,000 rows | Low — sequential scan is fast enough |
| 50,000 – 200,000 rows | Medium — noticeable latency on `q` requests |
| > 200,000 rows | High — `q` queries will be slow without trigram index |

### Recommendation

- Do not enable `pg_trgm` blindly in this audit PR.
- A future PR can add an optional migration if the deployment environment supports extension creation.
- Until the index exists, avoid heavy use of the `q` filter across large date windows.
- Communicate this constraint to operators via documentation.

**Potential future schema (do not apply in PR-ADS-057):**

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_search_terms_search_term_trgm
  ON search_terms USING gin (search_term gin_trgm_ops);
```

---

## 11. UI Performance Considerations

### Current N-Gram UI (verified in `static/app.js`)

| Behavior | Confirmed |
|---|---|
| No auto-refresh | ✅ Refresh and Apply are explicit button clicks only |
| No infinite scroll | ✅ Table renders the full `limit` rows at once |
| Default limit = 100 | ✅ `_NGRAMS_DEFAULT_LIMIT = 100` |
| Filters require explicit Apply | ✅ `ngrams-apply-btn` click calls `loadNgrams()` |
| Table has horizontal scroll | ✅ Wrapped in `ngrams-table-scroll` div |
| No heavy client-side aggregation | ✅ All aggregation is server-side; UI only renders |
| Enter key triggers load in text inputs | ✅ `keydown` handler on `ngrams-query`, `ngrams-campaign`, `ngrams-min-spend` |

### Potential risks

- User clicks Apply or Refresh repeatedly in quick succession — each click fires a new independent request with no debounce guard
- Filters can be changed and applied in rapid succession (e.g., stepping through campaigns) — each change triggers a full aggregation run
- Row cap warning is not shown prominently in the UI — users may not realize results are truncated

### Future recommendations (not implemented in this audit PR)

- Disable Apply and Refresh buttons while a request is in flight
- Add debounce to Enter key handler if live-search is ever added
- Show a prominent row cap warning when `data_quality.row_cap_applied` is true
- Show a "prototype analysis" note if cap is applied

---

## 12. Read-Only / Action-Language Guardrails

### Reconfirmed forbidden terms and concepts

The n-gram system at this phase must not produce, display, or imply any of the following:

| Forbidden | Reason |
|---|---|
| `negative_candidate` | Requires additional evidence, CRM joins, and human approval workflow |
| `suggested_negative` | Same as above |
| `add negative` / `push` / `apply` | Write action — blocked for Phase 1 |
| `exclude` / `block` / `pause` | Keyword management action — blocked |
| `recommendation` | Implies actionable output — not permitted without review workflow |
| `score` / `priority_score` | Scoring model not implemented |
| `attention_status` / `review_status` | Status classification not implemented |

### Allowed factual outputs (confirmed in current implementation)

| Allowed | Source |
|---|---|
| `ngram` — the phrase itself | `aggregate_ngrams` output |
| `n` — gram length | `aggregate_ngrams` output |
| `language` — heuristic label | `detect_script_or_language` |
| `row_count` — source rows containing this n-gram | `aggregate_ngrams` output |
| `unique_search_terms` — distinct search terms | `aggregate_ngrams` output |
| `total_spend_usd` — factual spend total | `aggregate_ngrams` output |
| `total_clicks` / `total_impressions` — factual engagement | `aggregate_ngrams` output |
| `flagged_waste_rows` / `clean_rows` / `unanalyzed_rows` — waste mix | `aggregate_ngrams` output |
| `source_rows_analyzed` — rows fed into aggregation | endpoint `summary` block |
| `row_cap_applied` — truncation flag | endpoint `data_quality` block |
| `campaigns_sample` / `search_terms_sample` — informational samples | `aggregate_ngrams` output |

---

## 13. Recommended Next PR Sequence

### Branch A — If performance is acceptable (recommended based on current volume)

| PR | Title | Type |
|---|---|---|
| PR-ADS-058 | Negative Candidate Readiness Audit | Audit-only |
| PR-ADS-059 | N-Gram Stopword Config | Move stopwords to YAML; no behavior changes |
| PR-ADS-060 | N-Gram UI Polish | Row cap warning; filter UX cleanup |

### Branch B — If performance risk is confirmed high (defer based on measurement)

| PR | Title | Type |
|---|---|---|
| PR-ADS-058 | N-Gram Benchmark Instrumentation | Add timing logs; no schema change |
| PR-ADS-059 | Search Terms Trigram Index | Optional `pg_trgm` migration |
| PR-ADS-060 | N-Gram Materialization Design PR | Schema + scheduler plan |

### Decision rule

Choose Branch A or B based on actual endpoint response time and whether `row_cap_applied` is routinely true in production. If 14d default requests return in under 3 seconds and the cap is rarely hit, proceed with Branch A.

---

## 14. Risk Register

| Risk | Severity | Evidence | Recommended Guardrail |
|---|---|---|---|
| Dynamic endpoint CPU load at scale | Medium | Python aggregation runs per request; no caching | Cap at 10,000 rows; measure response time; add cache if same filters repeat |
| `q ILIKE '%term%'` sequential scan | Medium | No trigram index; documented in `db/schema.py` lines 312–318 | Future: add `pg_trgm` GIN index if deployment supports it |
| Row cap hiding long-tail patterns | Medium | Cap = 10,000; enforced by `_NGRAMS_SOURCE_ROW_CAP` | Show `row_cap_applied` warning in UI; lower default window if cap is routinely hit |
| Stopword config drift | Low | Stopwords are hardcoded; no external override | Move to YAML config in PR-ADS-059; document protected tokens |
| Accidental scoring language in UI or API | Low | No scoring fields exist; endpoint docstring forbids them | Code review gate; language guardrail in Section 12 |
| Negative-candidate creep | Low | No candidate generation path exists | Maintain explicit non-goals checklist in each PR; review PR-ADS-058 gate |
| Arabic tokenization quality | Low | Heuristic normalization (no full morphological analyzer) | Acceptable for current use; document limitations; revisit if Arabic-language clients require precision |
| Spanish accent normalization drift | Low | NFKD strips accents, which may collapse distinct Spanish words | Acceptable for current use; domain terms bypass this via `_SPANISH_DOMAIN_TERMS` set |
| Table payload size | Low | Default limit=100; max=250; each row is a small JSON object | No action needed at current limit; revisit if limit is raised significantly |
| Multiple concurrent users amplifying CPU | Medium | No request queue; no concurrency limit | Render instance autoscaling; or add per-user rate limiting in a future PR |

---

## 15. Non-Goals

This PR is audit-only. The following are explicitly out of scope:

- No code changes
- No schema changes
- No API changes
- No UI changes
- No scheduler changes
- No connector changes
- No materialized n-gram table
- No cache implementation
- No benchmark tooling
- No negative keyword candidates
- No negative keyword push
- No writes to Google Ads
- No writes to HubSpot
- No AI chat integration
- No scoring fields
- No recommendation fields

---

## 16. Phase 1 Read-Only Checklist

- [x] Audit-only
- [x] No runtime behavior change
- [x] No code changed
- [x] No schema changed
- [x] No API changed
- [x] No UI changed
- [x] No external writes
- [x] No recommendations
- [x] No scoring
- [x] No negative candidates
- [x] No negative keyword push
