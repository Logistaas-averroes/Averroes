# Negative Candidate Readiness Audit

**Document:** `docs/14_NEGATIVE_CANDIDATE_READINESS_AUDIT.md`
**Roadmap ID:** PR-ADS-058
**Phase:** 1.5 — Search-Term Intelligence / Negative Candidate Governance
**Owner:** Youssef Awwad
**Audit date:** 2026-05-08
**Status:** Audit-only. No code changed. No schema changed. No API changed.

Depends on: PR-ADS-057 / PR-ADS-057A
Unblocks: PR-ADS-059 — N-Gram Stopword Config OR Negative Candidate Review Architecture

---

## 1. Executive Verdict

### Is the system ready to generate negative keyword candidates?

**No.** The system has factual visibility into search-term patterns — stored `search_terms`, waste flags, n-gram evidence, and performance metrics — but factual evidence of repetition is not the same as a negative keyword decision. The current evidence layer lacks the safeguards, governance, and review workflow needed to translate pattern data into candidates.

### Should candidates be generated automatically?

**No.** Automatic candidate generation must not begin until evidence thresholds, false-positive protections, human review gates, and a no-push guarantee are all in place. None of those exist yet.

### Should candidates be pushed to Google Ads?

**No.** No Google Ads writes are permitted in Phase 1 or Phase 1.5, including any write that adds, modifies, or removes negative keywords at any level.

### What evidence is required before a term can become a candidate?

Before any term or n-gram is considered a negative candidate, the following must all be present:

- Waste pattern evidence (matched junk pattern, high flagged-waste share, or repeated irrelevant intent)
- Economic evidence (meaningful spend threshold, repeated clicks/impressions, multiple example rows)
- Conversion and lead quality evidence (no Google conversions after meaningful spend, or HubSpot junk/low-quality lead evidence)
- Scope evidence (campaign-level candidate at minimum; account-level requires much stronger evidence)

Full requirements are in Section 5.

### What should the next PR build?

Either:

- **PR-ADS-059** — N-Gram Stopword Config (move stopwords and protected tokens to configuration; no candidate generation)
- **PR-ADS-060** — Negative Candidate Architecture Spec (docs-only; no implementation)

Both options are read-only or audit-only and do not unblock any Google Ads writes.

### Summary verdict

| Question | Answer |
|---|---|
| Ready to generate candidates automatically? | **No** |
| Ready to push to Google Ads? | **No** |
| Ready to build review table? | **Not yet — architecture spec first** |
| Safe next step | Stopword config hardening or candidate architecture design only |

---

## 2. Current Evidence Sources

### 2.1 Search Terms

**Source:** `search_terms` table

**Available fields:**

| Field | Notes |
|---|---|
| `source_date` | Date of the search-term row |
| `campaign_name` | Stored lowercase (canonicalized) |
| `ad_group` | Ad group name |
| `keyword` | Triggering keyword |
| `match_type` | Match type of the keyword |
| `search_term` | Actual search query |
| `spend_usd` | Spend attributed to the term |
| `clicks` | Click count |
| `impressions` | Impression count |
| `conversions` | Google Ads conversion count |
| `is_flagged_waste` | Tri-state: `TRUE` = waste, `FALSE` = clean, `NULL` = unanalyzed |
| `junk_category` | Waste category label if flagged |
| `matched_pattern` | Pattern that triggered the waste flag |

**Important caveats:**

- `is_flagged_waste` is a tri-state field. `NULL` does not mean clean — it means unanalyzed. Any candidate logic must treat `NULL` rows separately.
- `conversions` reflects Google Ads platform conversions, not HubSpot sales quality.
- Campaign names are stored lowercase; case-sensitive comparisons will fail.

### 2.2 N-Grams

**Source:** `GET /api/search-terms/ngrams`

**Available fields:**

| Field | Notes |
|---|---|
| `ngram` | Token or phrase |
| `n` | N-gram size |
| `row_count` | Number of search-term rows containing the n-gram |
| `unique_search_terms` | Distinct search terms containing the n-gram |
| `campaign_spread` | Number of distinct campaigns affected |
| `spend` | Aggregated spend across matching rows |
| `clicks` | Aggregated clicks |
| `impressions` | Aggregated impressions |
| `google_conversions` | Aggregated Google Ads conversions |
| `flagged` / `clean` / `unanalyzed` | Counts of waste-flag states in matching rows |
| `samples` | Example search terms |

**Important caveats:**

- The n-gram endpoint does not join HubSpot data.
- Google conversions in the n-gram response are platform conversions only; they do not reflect lead quality or deal creation.
- The endpoint is subject to a row cap (`_NGRAMS_SOURCE_ROW_CAP = 10_000`). At high data volumes, results may represent only a subset of available rows.
- An n-gram appearing across many rows may represent a core business term, not a waste signal.

### 2.3 GCLID / HubSpot Evidence

**Available separately:**

- GCLID attribution linking Google clicks to HubSpot contacts
- Attribution quality signals
- HubSpot contacts and deals
- Lead quality fields

**Important caveats:**

- The current n-gram endpoint does not join HubSpot.
- Google Ads conversions are platform conversions, not SQLs or deals.
- A term with no Google conversions may still produce qualified HubSpot leads.
- A term with Google conversions may still produce junk HubSpot contacts.
- HubSpot evidence must only be used when properly joined through GCLID attribution.

---

## 3. Why Negative Candidates Are Risky

Factual repetition of a term in search-term data does not make it safe to exclude. The following false-positive scenarios must be understood before any candidate system is designed.

### 3.1 Core Business Terms

Terms like:

- `freight`
- `forwarding`
- `logistics`
- `shipping`
- `cargo`
- `software`
- `customs`
- `warehouse`

appear frequently across campaigns, have high spend, and trigger many impressions. They are core to the product. High recurrence is expected and desirable. These terms must never become negative candidates on frequency evidence alone.

### 3.2 Research Intent Can Still Be B2B

Terms containing:

- `best`
- `compare`
- `alternatives`
- `pricing`
- `demo`
- `system`

may reflect commercial research by decision-makers evaluating logistics solutions. These are not waste by definition. Whether they convert depends on landing page quality, campaign targeting, and follow-up — not on the presence of the research modifier.

### 3.3 Competitor Terms Are Not Automatically Waste

A competitor-name term may be:

- High intent (the prospect is actively evaluating alternatives)
- Low quality (the prospect is committed to the competitor)
- Strategically valuable (conquesting campaigns)
- Expensive but worth it (CPL within acceptable range)

The decision depends entirely on campaign strategy. No automated system should classify competitor terms without human review and strategic context.

### 3.4 Language Terms Can Be Misleading

Spanish and Arabic search terms may reflect:

- Real target markets where the business actively operates
- Geographic campaigns targeting specific regions
- Legitimate bilingual customers

Language alone is not a waste signal. Excluding language terms without reviewing campaign intent could harm qualified demand in active markets.

### 3.5 Google Conversions Are Not Sales Truth

The relationship between Google Ads conversions and actual sales quality is unreliable in both directions:

- A term with zero Google conversions may still generate qualified HubSpot leads, MQLs, or closed deals through longer attribution paths.
- A term with multiple Google conversions may produce junk contacts, irrelevant form fills, or leads that never progress in HubSpot.

Using Google conversion counts as the sole quality signal will produce false negatives (blocking good terms) and false positives (sparing bad terms that look converted).

### 3.6 Broad-Match Leakage Needs Campaign Context

A search term that appears wasteful under one campaign's targeting settings may be acceptable under another campaign's audience, bid strategy, or match-type configuration. A term-level candidate without campaign context is incomplete and may block demand that another campaign legitimately needs.

---

## 4. Hard Boundaries

The following actions are explicitly prohibited in this PR and in any system built without completing the requirements in Sections 5 through 12:

**Prohibited actions:**

- No negative candidate generation
- No negative keyword creation at any level (exact, phrase, broad)
- No push to Google Ads
- No automatic exclusions
- No campaign pausing
- No bid changes
- No budget changes
- No labels of type "recommended negative", "block", or "exclude"
- No AI-generated action text recommending blocking or excluding terms
- No HubSpot writes

**Prohibited language in any UI, API response, or automated output:**

- "add negative"
- "push negative"
- "block this term"
- "exclude now"
- "apply"
- "kill"
- "recommended action: exclude"

**Permitted language for candidate review context:**

- evidence
- risk
- review-needed
- candidate-readiness
- human review required
- unanalyzed
- flagged pattern
- spend threshold

---

## 5. Candidate Evidence Requirements

The following evidence categories define the minimum bar that must be met before a term or n-gram can be considered a negative candidate. All three primary categories (A, B, C) must be represented. Category D (scope) must always be specified.

### A. Waste Pattern Evidence

At least one of:

- Matched a known junk pattern (e.g., `matched_pattern` is populated with a recognized waste pattern)
- High flagged-waste row share (e.g., ≥ 70% of rows for this term are `is_flagged_waste = TRUE`)
- Repeated free/job/student/training intent across multiple source dates and campaigns
- Clearly irrelevant service category (e.g., residential moving appearing in a B2B freight campaign)

### B. Economic Evidence

At least one of:

- Meaningful spend threshold (minimum threshold to be defined in config before candidate generation begins)
- Repeated clicks across multiple source dates
- Repeated impressions across multiple source dates
- Multiple distinct search-term examples supporting the same waste signal

### C. Conversion and Quality Evidence

At least one of:

- No Google conversions after meaningful spend (with the caveat that Google conversions are not final quality proof)
- HubSpot junk/low-quality lead evidence joined correctly through GCLID attribution
- Low MQL/SQL conversion quality from leads attributed to the term
- No linked deals for leads attributed to the term

**Caveats:**

- Google Ads conversions alone are insufficient to approve or reject a candidate.
- HubSpot data must only be used when joined correctly through verified GCLID attribution — not inferred or approximated.

### D. Scope Evidence

Every candidate must specify scope. Scope must be defined before a candidate is stored or reviewed:

- **Campaign-level candidate** — applies the negative within one named campaign. Default and safest scope.
- **Ad-group-level candidate** — applies the negative within one named ad group inside one campaign. More precise; appropriate when the term is wasteful in a specific ad group but not account-wide.
- **Account-level candidate** — applies the negative across all campaigns. Requires much stronger evidence, explicit justification, and should be blocked in early candidate system versions.

**Default scope:** campaign-level only.

Account-level candidates must not be generated until a separate governance gate is in place for account-level actions.

---

## 6. Candidate Types — Future Only

The following candidate categories are documented for future architecture design. None of these should be implemented in this PR or any PR before the requirements in Section 12 are satisfied.

### 6.1 Search Term Candidate

Based on an exact search query appearing repeatedly with waste evidence.

Example fields:

| Field | Description |
|---|---|
| `search_term` | Exact query text |
| `campaign_name` | Campaign context |
| `match_type` | Match type of the triggering keyword |
| `spend` | Aggregated spend |
| `clicks` | Aggregated clicks |
| `conversions` | Google Ads conversions |
| `waste_reason` | Why it was surfaced |

### 6.2 N-Gram Candidate

Based on a repeated token or phrase appearing across multiple search terms.

Example fields:

| Field | Description |
|---|---|
| `ngram` | Token or phrase |
| `n` | N-gram size |
| `affected_campaigns` | List of campaigns where it appears |
| `spend` | Aggregated spend |
| `examples` | Sample search terms containing the n-gram |
| `waste_pattern` | Why it was surfaced |

### 6.3 Pattern Candidate

Based on a named waste pattern type rather than a specific term.

Examples:

- Free intent (queries containing "free", "gratis", etc.)
- Job seeker intent (queries containing "jobs", "hiring", "vacancy", etc.)
- Student/training intent (queries containing "course", "certification", "training", etc.)
- Irrelevant category (residential, personal, unrelated industry)

**These are future concepts only. Do not implement.**

---

## 7. Match Type / Negative Type Guardrails

Negative keywords in Google Ads can be applied as negative exact, negative phrase, or negative broad. The choice of negative type has a major impact on how much traffic is excluded.

### 7.1 Recommended First-Phase Constraints

When a candidate review system is eventually built, it should support only:

- **Negative exact** — for exact search terms with strong waste evidence
- **Negative phrase** — for carefully reviewed n-grams with strong waste evidence across multiple terms

### 7.2 What to Avoid Initially

- **Negative broad** — broad negatives can block a wide range of queries, including relevant ones. Broad negatives should not be permitted in early candidate review versions.

### 7.3 Scope Constraints

- **Campaign-level negatives** — default and required for early versions
- **Account-level negatives** — must be blocked until a separate governance gate is defined, with explicit evidence requirements for account-level actions and explicit admin approval

### 7.4 Rationale

Broad negatives and account-level negatives are the highest-risk combination in any negative keyword system. Restricting to exact/phrase and campaign-level significantly reduces the blast radius of any error in candidate generation or human review.

---

## 8. Human Review Workflow Requirements

Before any future candidate can become actionable — even locally — a human review workflow must exist. The following requirements define what that workflow must include.

### 8.1 Candidate Evidence Card

Every candidate surfaced for review must present:

- Candidate text (exact term or n-gram)
- Candidate type (search term, n-gram, or pattern)
- Suggested scope (campaign, ad group)
- Suggested negative type (exact or phrase)
- Why it surfaced (waste pattern, spend threshold, flag share)
- Example search terms
- Affected campaign(s)
- Aggregated spend
- Aggregated clicks
- Aggregated impressions
- Google Ads conversion count (labeled as platform conversions, not sales quality)
- HubSpot quality evidence, if available and correctly joined

### 8.2 Review Workflow Requirements

The review workflow must support:

- Reviewer decision (approve for later / reject / archive)
- Reviewer notes field
- Full audit trail (who reviewed, when, what decision)
- Ability to reject a candidate without it being re-surfaced automatically
- Ability to archive a candidate with a note
- No automatic push of any decision to Google Ads

### 8.3 Required Decision States

| State | Meaning |
|---|---|
| `needs_review` | Candidate has been generated; no human decision yet |
| `approved_for_later` | Reviewed and considered worth keeping for future action; no push |
| `rejected` | Reviewed and rejected; should not resurface without new evidence |
| `archived` | Closed without decision; retained for audit trail |

**Prohibited states:**

- `auto_apply`
- `ready_to_push`
- `must_block`

No state should trigger an automatic write to Google Ads.

### 8.4 No-Push Guarantee

The review workflow must enforce a no-push guarantee at both the application and UI layer:

- No API endpoint may write negative keywords to Google Ads
- No UI action may trigger a Google Ads write
- The review table is a local decision table, not a push queue

---

## 9. Required Future Schema — Design Only

The following schema is provided for architecture planning purposes. **Do not implement this table in this PR.** No migration should be created. No ORM model should be added.

```sql
CREATE TABLE IF NOT EXISTS negative_candidate_reviews (
  id SERIAL PRIMARY KEY,

  candidate_type TEXT NOT NULL,         -- search_term | ngram | pattern
  candidate_text TEXT NOT NULL,

  source_scope TEXT NOT NULL DEFAULT 'campaign',
  campaign_name TEXT,
  ad_group TEXT,

  proposed_negative_type TEXT,          -- exact | phrase
  evidence_summary JSONB,
  examples JSONB,

  spend_usd NUMERIC(10,2) DEFAULT 0,
  clicks INTEGER DEFAULT 0,
  impressions INTEGER DEFAULT 0,
  google_conversions NUMERIC(8,2) DEFAULT 0,

  hubspot_quality_summary JSONB,

  review_status TEXT NOT NULL DEFAULT 'needs_review',
  reviewer_note TEXT,
  reviewed_by TEXT,
  reviewed_at TIMESTAMPTZ,

  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Important:**

- This table is future-only. Do not create it now.
- The review table is not a push queue. No row in this table should ever trigger a Google Ads write.
- `review_status` must be constrained to the approved states from Section 8.3.
- `source_scope` defaults to `campaign`. Account-level scope should be blocked in the application layer.

---

## 10. Future API Design — Design Only

The following endpoint shapes are provided for architecture planning purposes. **Do not implement these endpoints in this PR.**

### 10.1 Candidate Listing

```
GET /api/negative-candidates
```

Read-only. Returns a paginated list of candidates with their evidence and review status.

No response field should imply an action. No response field should contain "push", "apply", or "add negative".

### 10.2 Candidate Review

```
POST /api/negative-candidates/:id/review
```

Updates the local `review_status` for a candidate. This is a local database write only.

**This endpoint must never write to Google Ads.** It updates only the `review_status`, `reviewer_note`, `reviewed_by`, and `reviewed_at` fields on the local candidate record.

**No endpoint should push to Google Ads in Phase 1.5 or Phase 2 without explicit separate governance.**

---

## 11. UI Requirements — Future Only

The following UI requirements apply to any future candidate review interface. **Do not implement this UI in this PR.**

### 11.1 Required Display Fields

The candidate review UI must display:

- Candidate text
- Candidate type (search term, n-gram, pattern)
- Suggested scope (campaign, ad group)
- Suggested negative type (exact, phrase)
- Why it surfaced (evidence summary)
- Example search terms
- Affected campaign(s)
- Aggregated spend, clicks, impressions
- Google Ads conversion count (labeled: platform conversions only)
- HubSpot quality evidence if available and correctly joined
- Reviewer decision controls (local only)

### 11.2 Forbidden UI Actions

The following actions must not appear in any candidate review UI:

- Push to Google Ads
- Apply now
- Add negative
- Sync negative
- Auto-fix
- Enable blocking
- Exclude now

### 11.3 Permitted Future Local Actions

When a review table exists and the workflow is built:

- Mark reviewed
- Reject
- Archive
- Add internal note

**Even these local review actions should not be built until the review table and workflow requirements from Section 8 are implemented.**

---

## 12. Required Safeguards Before Candidate Generation

Candidate generation must not begin until all of the following are satisfied:

- [ ] N-gram endpoint performance is acceptable or hardened for the expected data volume
- [ ] Stopwords are configurable and managed outside application code
- [ ] Protected business tokens (e.g., `freight`, `logistics`, `forwarding`) are documented in config and excluded from candidate surfacing
- [ ] Search-term exact candidates are designed separately from n-gram candidates
- [ ] Campaign-level scope is enforced as the default
- [ ] Google Ads conversion counts are clearly separated from HubSpot lead quality in all evidence summaries
- [ ] Human review workflow exists (Section 8)
- [ ] No-push guarantee is enforced in both application code and UI layer
- [ ] Candidate surfacing reasons are explainable and auditable
- [ ] False-positive examples (Section 3) are tested against the candidate logic before any candidate is generated

---

## 13. Recommended Next PR Sequence

The recommended path from this audit is:

### PR-ADS-059 — N-Gram Stopword Config

- Move stopwords and protected tokens to a config file
- No negative candidates
- No UI changes unless docs only
- Unblocks: cleaner n-gram signal for any future candidate work

### PR-ADS-060 — Negative Candidate Architecture Spec

- Docs-only
- Define candidate tables, APIs, UI, and review workflow in detail
- No implementation
- Unblocks: PR-ADS-061

### PR-ADS-061 — Negative Candidate Read-Only Prototype

- Generate local candidate evidence only (no push)
- No approval actions
- No Google Ads writes
- Review table may be created for local storage only

### PR-ADS-062 — Candidate Review UI

- Local review only (mark reviewed, reject, archive)
- No Google Ads writes
- Requires review table from PR-ADS-061

### Phase 3+ — Google Ads Push (Explicit Approval Only)

- Separate gated feature requiring explicit admin approval
- Irreversible-action warnings required at every step
- Full audit log required
- Admin-only access
- Cannot proceed without all preceding gates

### Alternative path (if performance is a blocker)

If n-gram endpoint performance degrades before PR-ADS-059 is complete:

- Delay candidate architecture
- Build benchmark and caching infrastructure first (see PR-ADS-057 recommendations)
- Resume candidate sequence once performance is stable

---

## 14. Risk Register

| Risk | Severity | Why It Matters | Guardrail |
|---|---|---|---|
| Blocking relevant B2B demand | Critical | Core logistics terms appear frequently; excluding them would suppress qualified traffic | Protected token config; human review required before any action |
| Over-pruning broad match | High | Broad negatives can block a wide range of queries unexpectedly | Restrict first candidate system to exact and phrase negatives only |
| Spanish/Arabic false positives | High | Language terms may reflect active target markets, not irrelevant traffic | Campaign context required; language alone is not a waste signal |
| Competitor strategy misclassification | High | Competitor terms may be high-intent conquesting, not waste | Human review required; no automated competitor exclusions |
| Google conversion ≠ sales quality | High | Platform conversions do not reflect HubSpot lead quality or deal creation | Separate Google and HubSpot evidence; never use conversions as sole qualifier |
| Account-level negative overreach | Critical | Account-level negatives affect all campaigns; one error has wide blast radius | Block account-level candidates until separate governance gate exists |
| Accidental push language in UI | High | Labels like "add negative" or "apply" create implicit user expectations of push capability | Explicit forbidden-language list enforced in UI and API response contracts |
| AI-hallucinated recommendations | High | LLM-generated action text may suggest blocking terms without evidence | No AI-generated action text in candidate UI or API responses |
| Insufficient audit trail | Medium | Without reviewer identity and timestamp, decisions cannot be reviewed or reversed | Audit fields required on every candidate record (`reviewed_by`, `reviewed_at`, `reviewer_note`) |
| No rollback mechanism | High | Negative keywords applied to Google Ads cannot be automatically reversed | No Google Ads writes until rollback mechanism is defined and tested |

---

## 15. Non-Goals

This PR explicitly does not:

- Change any code
- Change any schema
- Change any API endpoint
- Change any UI
- Change any scheduler
- Change any connector
- Generate negative candidates
- Create a review table
- Create a review queue
- Add scoring logic
- Add recommendation labels
- Write to Google Ads
- Write to HubSpot
- Add AI chat or AI-generated recommendations

---

## 16. Phase 1 Read-Only Checklist

- [x] Audit-only
- [x] No runtime behavior change
- [x] No code changed
- [x] No schema changed
- [x] No API changed
- [x] No UI changed
- [x] No negative candidates generated
- [x] No recommendations
- [x] No scoring
- [x] No Google Ads writes
- [x] No HubSpot writes
- [x] No AI chat

---

## Contract Impact

| Area | Changed? |
|---|---|
| Data output | No |
| DB schema | No |
| API endpoints | No |
| Config | No |
| Breaking change | No |
| Documentation | Adds `docs/14_NEGATIVE_CANDIDATE_READINESS_AUDIT.md` |

Phase 1 read-only: **Confirmed. Audit only.**
