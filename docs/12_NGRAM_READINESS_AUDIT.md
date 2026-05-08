# N-Gram Readiness Audit

**Document:** `docs/12_NGRAM_READINESS_AUDIT.md`
**Roadmap ID:** PR-ADS-054
**Phase:** 1.5 — Search-Term Intelligence / Audit
**Owner:** Youssef Awwad
**Audit date:** 2026-05-07
**Status:** Originally audit-only at time of PR-ADS-054: no code changed, no schema changed, no API changed. Amended post-audit to note that PR-ADS-055 introduces one new read-only endpoint: `GET /api/search-terms/ngrams`.

**Implementation note (PR-ADS-055):** PR-ADS-055 implements the first backend prototype described by this audit: a read-only factual n-gram metrics endpoint (`GET /api/search-terms/ngrams`). It does not implement scoring, UI, materialization, or negative keyword candidate generation.

**UI note (PR-ADS-056):** PR-ADS-056 adds the first read-only N-Gram UI page using the backend prototype endpoint. It does not add scoring, materialization, negative keyword candidates, or write actions.

**Config note (PR-ADS-060):** PR-ADS-060 moves n-gram stopwords and protected tokens into `config/ngram_stopwords.yaml`. This supports safer future candidate governance but does not generate candidates, scoring, recommendations, or write actions.

Depends on: PR-ADS-053
Unblocks: PR-ADS-055 — N-Gram Analysis Backend Prototype

---

## 1. Executive Verdict

### Is the system ready to build n-gram analysis?

**Yes, conditionally.** The `search_terms` table now provides a sufficient data foundation for read-only n-gram analysis. The grain, tri-state waste flag, and supporting metrics (spend, clicks, impressions, conversions) are all present. A backend prototype is viable now, provided implementation stays strictly read-only.

### Which existing table should power it?

**`search_terms`** — it is the authoritative source of raw search term strings, per-day, per-campaign, per-ad-group. No other table is suitable as a primary source.

### Should n-grams be computed live or materialized?

**Live in the first prototype, materialized later.** The first endpoint (PR-ADS-055) should compute dynamically over a filtered recent window (default 14 days, max 30–90 days). Materialization is deferred until after the prototype confirms value — see Section 10 and Section 12.

### Should n-grams create negative keyword candidates now?

**No.** N-gram analysis is strictly read-only intelligence at this phase. Negative keyword candidate generation requires additional evidence rules, CRM/HubSpot quality joins, false-positive review, match-type scoping, campaign-level scope rules, and an explicit human approval workflow. These are not present and should not be built until PR-ADS-058 at the earliest.

### What should PR-ADS-055 build first?

- A single read-only backend endpoint: `GET /api/search-terms/ngrams`
- Dynamic analysis over filtered recent `search_terms` rows
- No schema changes
- No UI
- No negative keyword candidates
- No writes of any kind
- Limited date window (14 days default)
- Unigrams, bigrams, trigrams only

---

## 2. Current Data Foundation

The `search_terms` table provides the following fields relevant to n-gram analysis:

| Field | N-Gram Relevance |
|---|---|
| `source_date` | Date-window filtering; avoids stale data dominating results |
| `campaign_name` | Group n-grams by campaign; detect campaign-level leakage |
| `ad_group` | Group n-grams by ad group; detect ad-group-level drift |
| `keyword` | Cross-reference the matched keyword to assess match-type behaviour |
| `match_type` | Broad-match rows are higher leakage risk than exact/phrase |
| `search_term` | **Primary input to n-gram tokenization** |
| `spend_usd` | Weight n-gram attention by cost |
| `clicks` | Secondary engagement metric |
| `impressions` | Reach metric; high impressions + zero conversions is a signal |
| `conversions` | Google platform conversions — see note below |
| `is_flagged_waste` | Tri-state filter and group-by dimension |
| `junk_category` | Optional filter; may cluster with certain n-grams |
| `matched_pattern` | Optional cross-reference to pattern engine |

### Tri-state meaning of `is_flagged_waste`

| Value | Meaning |
|---|---|
| `NULL` | Not yet analyzed — waste state is unknown |
| `TRUE` | Analyzed and flagged as waste |
| `FALSE` | Analyzed and not flagged as waste |

**Critical interpretation rules:**

- `NULL` does **not** mean clean. Unanalyzed rows carry unknown waste risk.
- `FALSE` (not flagged) does **not** mean valuable. It means the pattern engine did not match — it has no opinion on commercial quality.
- Google `conversions` are platform-reported conversion events. They are **not** HubSpot SQL-quality leads. A row with `conversions > 0` is not confirmed revenue or a qualified pipeline lead.
- Any future join between n-gram signals and CRM quality must be explicitly designed and is out of scope for this phase.

---

## 3. N-Gram Use Cases

The following use cases are safe to build as read-only analysis in PR-ADS-055 and beyond.

### Search-term waste discovery

Find repeated tokens and phrases associated with non-commercial or non-target-persona intent:

- **Free intent:** terms containing "free", "gratis", "gratuito", "مجاني"
- **Job seeker intent:** terms containing "jobs", "career", "وظائف", "hiring"
- **Student / training intent:** terms containing "student", "training", "course", "learn", "certification"
- **Competitor research intent:** terms containing competitor brand names or product names used in information-gathering context
- **Unrelated software categories:** terms referencing ERP, HR, CRM, or other software categories unrelated to freight forwarding
- **Local services irrelevant to freight forwarding:** terms referencing physical services, moving companies, consumer courier services
- **Low B2B intent terms:** consumer-facing phrasing, how-to queries, price-check phrasing

### Broad-match drift detection

Identify repeated irrelevant thematic clusters appearing disproportionately in broad-match traffic. Cross-reference with `match_type = 'BROAD'` rows. Recurring n-grams with high spend, low conversions, and broad match are candidates for manual review.

### Campaign leakage review

Identify n-grams appearing across multiple campaigns where they logically should not co-appear. Example: a competitor-brand n-gram appearing in a non-competitor campaign indicates potential keyword or match-type boundary failure.

### Language-market mismatch

Identify Spanish-language terms appearing in English-market campaigns, Arabic-language terms appearing in campaigns targeting Latin markets, or mixed-language queries where the script does not match the expected campaign market. See Section 6 for language handling guidance.

### Search-term taxonomy

Group recurring intent patterns for future reporting and editorial annotation. This is informational only and should not drive automated actions.

---

## 4. Hard Boundaries / Prohibited Actions

This audit and all implementations derived from it must respect the following hard boundaries:

- No negative keyword candidate generation
- No negative keyword push to Google Ads
- No Google Ads write operation of any kind
- No HubSpot write operation of any kind
- No campaign pausing, bid change, or budget change
- No classification writes to `search_terms` rows
- No UI action buttons (Apply, Exclude, Block, Add Negative, Fix)
- No automated action queue items
- No AI chat integration
- No production n-gram engine in this audit PR
- No OCT upload

---

## 5. Tokenization Requirements

### Basic normalization

Before tokenization, each `search_term` string should be normalized as follows:

1. **Lowercase** all text
2. **Trim** leading and trailing whitespace
3. **Normalize** repeated internal whitespace to a single space
4. **Remove punctuation** where safe — commas, periods, exclamation marks, question marks, semicolons, colons
5. **Preserve meaningful symbols** only where they carry semantic weight: `+` (broad-match modifier historical usage), `#` (if appearing in software/dev terms), `/` (if appearing in path-like terms), `-` (if used as a compound joiner and not a list separator)
6. **Normalize Arabic diacritics** — remove tashkeel (harakat) if Arabic support is included; see Section 6 for full Arabic normalization rules
7. **Normalize accented Latin characters** — language detection (Section 6) must be performed **before** accent normalization; once the language is determined, accented characters may be normalized for tokenization (é → e, ñ → n); normalizing before detection would destroy Spanish script signals used for language inference

### Token length filtering

After splitting on whitespace:

- **Ignore single-character tokens** except in cases where the character carries established meaning (e.g., `+` as an operator)
- **Ignore numeric-only tokens** unless the number appears to carry industry-relevant meaning (e.g., HS codes, container sizes — low priority for first prototype)
- **Minimum token length:** `>= 2` characters for Latin scripts; `>= 2` characters for Arabic script (many meaningful Arabic words are 2–3 characters after normalization)
- **Maximum token length for filtering:** exclude tokens `> 30` characters in the first prototype; these are likely URLs, tracking fragments, or malformed strings

### N-gram lengths

First prototype should compute:

| Length | Name | Example |
|---|---|---|
| 1 | Unigram | "gratis" |
| 2 | Bigram | "freight software" |
| 3 | Trigram | "free freight software" |

**Do not go beyond trigram in the first prototype.**

Rationale:
- 4+ grams become sparse; each additional token dramatically reduces match frequency
- Trigrams are sufficient to capture the most actionable waste-intent phrases:
  - "free freight software"
  - "shipping software gratis"
  - "logistics jobs amman"
  - "freight forwarding training"
- Sparse 4-grams produce noise, not intelligence

---

## 6. Language Handling

The `search_terms` corpus is expected to contain queries in three primary scripts/languages based on the markets served:

### English

The dominant language. Most business-to-business freight-forwarding queries will be English. English stopwords should be filtered to avoid inflating n-gram counts with function words.

**English stopword examples** (not exhaustive):

`the`, `a`, `an`, `for`, `to`, `in`, `of`, `and`, `with`, `on`, `at`, `by`, `from`, `is`, `are`, `was`, `be`, `or`, `as`, `i`, `it`, `this`, `that`, `which`, `who`

> **Caution:** `software` may appear frequently in this corpus and **must not** be treated as a stopword. It is a core discriminating term — "freight forwarding software" vs. "freight forwarding service" carry different commercial meanings. See Section 7.

### Spanish

Present in Latin American and Iberian market queries. Spanish stopwords should be filtered, with important exceptions.

**Spanish stopword examples** (not exhaustive):

`de`, `para`, `el`, `la`, `los`, `las`, `en`, `con`, `por`, `que`, `un`, `una`, `es`, `al`, `del`, `se`, `su`, `lo`, `y`, `o`

> **Caution:** `gratis` is **not** a stopword. It is a high-signal waste indicator meaning "free". Removing it from analysis would neuter waste detection for Spanish-language traffic.

### Arabic

Present in Middle Eastern market queries (Jordan, Gulf region, North Africa). Arabic tokenization requires additional normalization before stopword filtering.

**Arabic stopword examples** (not exhaustive):

`من`, `في`, `على`, `الى`, `إلى`, `عن`, `مع`, `و`, `ال`, `هذا`, `هذه`, `التي`, `الذي`, `كان`, `كانت`, `أن`, `إن`

**Arabic normalization requirements:**

1. **Normalize alef forms:** treat `أ`, `إ`, `آ`, `ا` as equivalent (map all to `ا`) — alef variation is the most common source of tokenization fragmentation
2. **Normalize ya/alef maqsura:** evaluate whether treating `ي` and `ى` as equivalent is safe for the specific corpus; document the decision
3. **Remove tatweel:** strip the kashida/tatweel character `ـ` (U+0640) which is used for text stretching but carries no semantic meaning
4. **Remove diacritics (tashkeel):** strip harakat (fatha, damma, kasra, sukun, tanwin forms) — these are rarely present in typed web queries but may appear in some inputs
5. **Tokenization method:** whitespace tokenization is acceptable as a first version for Arabic but is documented as imperfect — Arabic morphology allows prefix/suffix attachment that whitespace splitting does not resolve; this limitation should be noted in the prototype's API response

### Language detection

Implementing a full language detection library is **not required** in the first prototype.

**Recommended first version — script and character-based inference:**

1. If the search term contains Arabic-script characters (Unicode block U+0600–U+06FF), classify as `arabic`
2. If the search term contains Spanish-specific characters (`ñ`, `¿`, `¡`) or common Spanish-only content words that are not language-neutral (`gratis`, `envios`, `logistica`, `flete`, `aduanas`), classify as `spanish` — **do not rely on stopwords for detection**, as this creates a circular dependency (you need the language to pick the stopword list, but you are using the stopword list to detect the language)
3. Otherwise, classify as `english` (default)

**Alternative acceptable approach:** Skip per-row language classification entirely in the first prototype and use a combined stopword list that includes English + Spanish + Arabic stopwords. Document this as a known simplification. Language classification can be added in PR-ADS-056 or PR-ADS-057.

---

## 7. Stopword Strategy

Stopword lists must be:

- **Configurable** — not hardcoded deep inside analysis functions
- **Stored in a config file** when built: recommended path `config/ngram_stopwords.yaml`
- **Language-aware** where practical — separate lists per language, merged at runtime with deduplication
- **Reviewed before finalization** — stopword lists for this corpus are not generic; they require domain awareness

### Words that must NOT automatically be stopwords

The following words carry commercial or waste-signal meaning in the freight-forwarding software context. Removing them from n-gram analysis would destroy the value of the analysis:

| Token | Why it must be kept |
|---|---|
| `freight` | Core business term; high discriminating value |
| `forwarding` | Core business term; differentiates from general logistics |
| `logistics` | Core business term; present in most meaningful queries |
| `shipping` | Core business term; also present in consumer shipping (ambiguous) |
| `software` | Critical discriminator — "logistics software" vs. "logistics service" |
| `system` | Often appears in "logistics system", "freight system" |
| `cargo` | Core business term |
| `customs` | Core business term; "customs software", "customs clearance" |
| `warehouse` | Core business term; may indicate adjacent market |
| `gratis` | High-value waste signal in Spanish queries |
| `free` | High-value waste signal in English queries |
| `job` | Waste signal — job-seeker intent |
| `jobs` | Waste signal — job-seeker intent |
| `student` | Waste signal — educational intent |
| `training` | Ambiguous — could be "staff training software" (B2B) or "training course" (education); must be reviewed, not auto-removed |

Removing any of these words as stopwords would produce a useless detector — it would fail to surface waste signals and fail to measure how frequently core business terms appear in waste traffic.

---

## 8. Metric Requirements

For each n-gram identified, the analysis should compute the following metrics:

| Metric | Description |
|---|---|
| `ngram` | The token or phrase string |
| `n` | Length: 1 (unigram), 2 (bigram), 3 (trigram) |
| `language` | Detected or inferred script/language: `english`, `spanish`, `arabic` |
| `row_count` | Number of `search_terms` rows containing this n-gram |
| `unique_search_terms` | Count of distinct `search_term` strings containing this n-gram |
| `campaigns_count` | Number of distinct campaigns where this n-gram appears |
| `ad_groups_count` | Number of distinct ad groups where this n-gram appears |
| `keywords_count` | Number of distinct matched keywords where this n-gram appears |
| `total_spend_usd` | Sum of `spend_usd` across all rows containing this n-gram |
| `total_clicks` | Sum of `clicks` |
| `total_impressions` | Sum of `impressions` |
| `google_conversions` | Sum of `conversions` (platform conversions — see note) |
| `avg_cpc_usd` | `total_spend_usd / total_clicks` where `total_clicks > 0` |
| `ctr_pct` | `(total_clicks / total_impressions) * 100` where impressions > 0 |
| `google_conversion_rate_pct` | `(google_conversions / total_clicks) * 100` where clicks > 0 |
| `flagged_waste_rows` | Count of rows where `is_flagged_waste = TRUE` |
| `clean_rows` | Count of rows where `is_flagged_waste = FALSE` |
| `unanalyzed_rows` | Count of rows where `is_flagged_waste IS NULL` |
| `flagged_waste_spend_usd` | Sum of `spend_usd` where `is_flagged_waste = TRUE` |
| `campaigns_sample` | Array of up to 5 distinct campaign names for display |
| `search_terms_sample` | Array of up to 5 distinct raw search term strings for context, selected by highest `spend_usd` descending |

> **Important:** `google_conversions` are Google Ads platform conversion events. They are not HubSpot SQL-quality leads. A non-zero conversion count does not indicate pipeline quality. Any future CRM quality join must be explicitly designed and is out of scope for this phase and the next.

---

## 9. Scoring / Severity Readiness

A read-only attention score may be computed for display purposes in a future UI. **Do not implement in PR-ADS-055.**

### Proposed future score formula (display-only)

```text
ngram_attention_score =
    (spend_weight)
    + (frequency_weight)
    + (waste_state_weight)
    + (conversion_absence_weight)
```

Where each component is a normalized contribution — exact weights are to be determined empirically after the prototype is deployed.

### Required guardrails on any scoring

- The score is **not** a recommendation
- The score is **not** a negative keyword decision
- The score means **"review priority"** — nothing more
- The score must **not** be labelled "bad score", "negative score", "block score", or any name implying action
- No automatic action may be triggered by score value
- The score is display-only; it must be accompanied by the evidence fields that compose it

### Recommended status labels

| Label | Meaning |
|---|---|
| `review` | Warrants human review; elevated spend, low conversions, waste-state mix |
| `watch` | Notable frequency or spend; no strong waste signal yet |
| `informational` | Present in corpus; no attention signal |

**Labels that must never be used:**

`block`, `exclude`, `pause`, `kill`, `negative_now`, `apply`, `fix`

---

## 10. Data Volume / Performance Audit

### How many rows can `search_terms` reach?

A production freight-forwarding advertiser with broad-match keywords active across multiple markets can accumulate thousands of unique search terms per week. At 90-day retention with multi-campaign coverage, the table could feasibly hold tens of thousands to hundreds of thousands of rows. N-gram tokenization of every row on every request would be expensive at scale.

### How expensive is tokenizing on every request?

Tokenizing and cross-joining n-gram extraction in PostgreSQL using string functions (`regexp_split_to_table`, `string_agg`) across a large unindexed text corpus is expensive. Running this live on full table scans at query time is not sustainable beyond the prototype phase.

### Recommendations

**First prototype (PR-ADS-055):**

| Parameter | Value |
|---|---|
| Default date window | 14 days |
| Maximum date window | 30 days (configurable to 90 for operator use) |
| Row limit | Apply a safety limit (e.g., 10,000 rows) if needed to avoid query timeout |
| Tokenization location | Python (backend), not PostgreSQL — avoids expensive DB text operations |
| Caching | No scheduled materialization yet; Python-level in-memory computation per request |
| Scheduled jobs | None in this PR |

**Later production (post-PR-ADS-057):**

| Component | Recommendation |
|---|---|
| Materialized table | `search_term_ngrams` — computed by scheduler, indexed by date/campaign/ngram/n |
| Scheduler integration | Weekly or on-demand recompute after each data sync |
| Indexes | On `ngram`, `n`, `source_date_from`, `campaign_name` |
| UI data source | Reads from materialized table, not live computation |

Do not build the materialized table or scheduler integration in this audit PR.

---

## 11. Proposed Future API

**Endpoint:** `GET /api/search-terms/ngrams`

### Query parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `days` | integer | 14 | Date window in days from today |
| `campaign` | string | (all) | Filter to specific campaign name |
| `match_type` | string | (all) | Filter to specific match type |
| `waste_state` | string | `all` | `flagged`, `clean`, `unanalyzed`, `all` (`waste` accepted as an alias for `flagged`) |
| `q` | string | (none) | Substring search filter on n-gram text |
| `min_spend` | decimal | 0 | Minimum total spend threshold |
| `n` | string | `1,2,3` | Comma-separated n-gram lengths to return |
| `limit` | integer | 100 | Maximum rows to return |

### Response shape

```json
{
  "days": 14,
  "filters": {
    "campaign": "global - competitors",
    "n": [1, 2, 3],
    "waste_state": "all"
  },
  "rows": [
    {
      "ngram": "gratis",
      "n": 1,
      "language": "spanish",
      "row_count": 18,
      "unique_search_terms": 12,
      "campaigns_count": 3,
      "ad_groups_count": 4,
      "keywords_count": 6,
      "total_spend_usd": 420.50,
      "total_clicks": 70,
      "total_impressions": 3100,
      "google_conversions": 0,
      "avg_cpc_usd": 6.01,
      "ctr_pct": 2.26,
      "google_conversion_rate_pct": 0.0,
      "flagged_waste_rows": 11,
      "clean_rows": 0,
      "unanalyzed_rows": 7,
      "flagged_waste_spend_usd": 310.00,
      "campaigns_sample": ["global - broad", "latam - broad"],
      "search_terms_sample": ["software de logistica gratis", "sistema de envios gratis"]
    }
  ],
  "data_quality": {
    "note": "N-gram analysis is read-only and does not create negative keyword actions.",
    "google_conversions_note": "Conversion counts are Google platform events, not HubSpot SQL-quality leads.",
    "unanalyzed_note": "Rows with is_flagged_waste IS NULL have not been analyzed by the pattern engine. NULL does not mean clean."
  }
}
```

**Constraints on response design:**

- No `action` fields
- No `push` fields
- No `create_negative` fields
- No `recommended_negative` fields
- No write-command fields of any kind

> **Note:** `attention_status`, `review_status`, or evidence-label fields should be deferred until a later scoring/readiness PR. PR-ADS-055 should return factual n-gram metrics only.

---

## 12. Proposed Future Table — Optional Later

The following materialized table is documented for future reference. **Do not build in this audit PR or in PR-ADS-055.**

```sql
CREATE TABLE IF NOT EXISTS search_term_ngrams (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    source_date_from    DATE,
    source_date_to      DATE,
    campaign_name       TEXT,
    match_type          TEXT,
    ngram               TEXT        NOT NULL,
    n                   INTEGER     NOT NULL,
    language            TEXT,
    row_count           INTEGER     DEFAULT 0,
    unique_search_terms INTEGER     DEFAULT 0,
    total_spend_usd     NUMERIC(10, 2) DEFAULT 0,
    total_clicks        INTEGER     DEFAULT 0,
    total_impressions   INTEGER     DEFAULT 0,
    google_conversions  NUMERIC(8, 2) DEFAULT 0,
    flagged_waste_rows  INTEGER     DEFAULT 0,
    clean_rows          INTEGER     DEFAULT 0,
    unanalyzed_rows     INTEGER     DEFAULT 0,
    attention_status    TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
```

### Recommendation

Do not build this table until after a prototype confirms value. The first implementation in PR-ADS-055 should use dynamic backend analysis or a temporary analysis module. If the prototype proves useful, PR-ADS-057 can introduce materialization.

> **Note:** The numeric types above intentionally mirror current `search_terms` conventions unless a later volume/performance audit justifies wider precision.

---

## 13. UI Readiness

Future n-gram UI (PR-ADS-056) should display:

- N-gram text
- Language / script
- Row count
- Unique search terms
- Total spend
- Total clicks
- Total impressions
- Google conversions (with prominent label: "platform events, not SQL leads")
- Flagged / clean / unanalyzed row breakdown
- Top campaign names (sample)
- Sample search term strings
- Status label: `review` / `watch` / `informational`
- Filter controls: days, campaign, match type, waste state, n-gram length, min spend

**UI must not show:**

- "Add negative" button
- "Push negative" button
- "Apply" button
- "Exclude" button
- "Block now" button
- "Fix" button
- Any button that implies or enables an action

**One safe optional call-to-action:**

> **View matching search terms** — navigates to the Search Terms Forensics page pre-filtered to the selected n-gram. No write operation.

---

## 14. Negative Keyword Candidate Boundary

This section documents a hard architectural boundary.

**N-grams are not negative keyword candidates.**

N-gram frequency and spend signals are informational. They indicate that a word or phrase appears repeatedly in the search term corpus. They do not indicate:

- That the word/phrase is irrelevant to the target persona
- That the word/phrase caused budget waste (correlation is not causation without CRM join)
- That a negative keyword would not block valuable traffic
- That a negative keyword would be correctly scoped to the right campaign and match type

### Prerequisites for future negative candidate generation

Before any system generates negative keyword candidates from n-gram data, all of the following must be in place:

| Prerequisite | Status |
|---|---|
| Stronger evidence rules (not just frequency) | Not built |
| CRM / HubSpot quality join (SQL pipeline data) | Not built |
| False-positive review workflow | Not built |
| Match-type suggestion rules | Not built |
| Campaign-level scope rules | Not built |
| Human approval workflow | Not built |
| No-push guarantee (candidate ≠ push) | Not built |

### Future candidate review PR sequence

```
PR-ADS-058 — Negative Candidate Readiness Audit (audit only, no candidate generation)
PR-ADS-059 — Negative Candidate Review Queue (read-only queue, no push)
Phase 3 only — push to Google Ads, only after explicit human approval workflow is complete
```

Do not build any of this now. Do not build any of this in PR-ADS-055.

---

## 15. Architecture Recommendation

Recommended implementation sequence:

### PR-ADS-055 — N-Gram Backend Prototype

- Read-only `GET /api/search-terms/ngrams` endpoint
- Dynamic Python analysis from `search_terms` table
- No schema changes
- No UI
- Limited date window (14–30 days)
- Unigrams, bigrams, trigrams
- No negative keyword candidates
- No writes

### PR-ADS-056 — N-Gram UI Page

- Read-only table view
- Filter controls (days, campaign, match type, waste state, n-gram length, min spend)
- Sample search terms per n-gram
- Link to Search Terms Forensics page (filtered)
- No action buttons

### PR-ADS-057 — N-Gram Performance Hardening

- Decide: caching vs. materialized table
- Optional `search_term_ngrams` table if prototype confirms value
- Optional `config/ngram_stopwords.yaml` configurable stopword list
- Scheduler integration if materialization is chosen

### PR-ADS-058 — Negative Candidate Readiness Audit

- Audit-only document
- No candidate generation
- Review prerequisites listed in Section 14

---

## 16. Risk Register

| Risk | Severity | Why It Matters | Guardrail |
|---|---|---|---|
| False positives from repeated business terms | 🔴 High | Words like "freight", "logistics", "software" appear in both valuable and waste queries — removing or flagging them incorrectly destroys the detector | Configurable stopword list must exclude business-critical terms; see Section 7 |
| Language tokenization mistakes | 🟠 Medium | Incorrect splitting or normalization produces phantom n-grams that do not correspond to real phrases | Test tokenization against representative corpus samples before production |
| Arabic normalization errors | 🟠 Medium | Alef variants and tatweel cause the same word to appear as multiple distinct tokens, inflating counts and hiding real patterns | Apply alef normalization and tatweel stripping before tokenization; see Section 6 |
| Treating Google platform conversions as HubSpot SQLs | 🔴 High | A row with `conversions > 0` may have zero CRM pipeline value; using Google conversions as a quality signal overestimates traffic quality | Display label must read "Google platform conversions" — not "leads" or "SQL conversions"; CRM join is explicitly deferred |
| High query cost from live tokenization | 🟠 Medium | Tokenizing large text corpora in PostgreSQL or Python without a row limit can cause timeout or resource exhaustion | Apply date window default (14 days) and row safety limit in first prototype |
| Accidental action language in UI | 🔴 High | Any button or label implying an action (Block, Exclude, Apply) creates pressure for premature negative keyword creation | UI must not contain action buttons; see Section 13 |
| Over-trusting unanalyzed rows | 🟠 Medium | `is_flagged_waste IS NULL` rows have no waste opinion; treating them as clean understates risk | All metric outputs must break down flagged / clean / unanalyzed separately; NULL must never be grouped with FALSE |
| Broad-match over-pruning risk | 🔴 High | Aggressively creating negatives from broad-match n-grams may block valuable traffic using natural language variation | Negative candidate generation is blocked until PR-ADS-058; broad-match rows are flagged as higher risk in analysis outputs |
| Sparse 4+ gram noise | 🟢 Low | Long n-grams produce very low match counts and misleading specificity | First prototype is limited to unigrams, bigrams, trigrams; 4+ grams not implemented |
| Pagination contamination | 🟠 Medium | If n-grams are computed from paginated UI data rather than full DB result sets, counts will be artificially low and unreliable | N-gram analysis must always run over full filtered DB result sets, never over paginated UI data |

---

## 17. Non-Goals

This audit PR introduces no changes to runtime code, data, or interfaces:

- No code changes
- No schema changes
- No API endpoint changes
- No UI changes
- No scheduler changes
- No connector changes
- No n-gram analysis engine
- No n-gram computation of any kind
- No negative keyword candidates
- No negative keyword push
- No Google Ads writes
- No HubSpot writes
- No AI chat integration
- No action queue items
- No OCT upload

---

## 18. Phase 1 Read-Only Checklist

- [x] Audit-only document
- [x] No code changed
- [x] No schema changed
- [x] No API changed
- [x] No UI changed
- [x] No scheduler changed
- [x] No connector changed
- [x] No n-gram analysis executed
- [x] No negative keyword candidates created
- [x] No Google Ads writes
- [x] No HubSpot writes
- [x] No OCT upload
- [x] No bid / budget / campaign changes
