# 34 — Search-Term Waste Consolidation (PR-ADS-153D)

Flagged Waste Terms is no longer a product. It is a **view** of Search Terms and
a **source of actions** in the Action Queue.

---

## 1. The rule

There is ONE canonical Google Ads search-term fact source. Waste is an
**annotation and decision layer** on top of it — never a second ledger.

    canonical search-term facts   (search_terms)
      + durable waste annotations (waste_terms)
      + durable local decisions   (search_term_review)

Two surfaces consume that: **Search Terms** answers *what happened and which
queries deserve review*, and the **Action Queue** answers *what should I do*.
No third surface may answer either question.

---

## 2. What was retired

| Thing | Status |
|---|---|
| Flagged Waste Terms sidebar item | **Removed** |
| `#page-waste` section markup | **Deleted** |
| `loadWaste` / `renderWasteTable` / `renderWasteKPIs` / `populateWasteFilters` / `getFilteredWasteTerms` / `applyWasteFilters` / `copyWasteTerms` / `downloadWasteCSV` | **Deleted** |
| `PAGE_EXPLANATIONS.waste`, `PAGE_HELP_CONTENT.waste`, `PAGE_DATASET_MAP.waste`, `DERIVED_DATASET_PAGES` entry, `EVIDENCE_PAGES` entry | **Deleted** |
| `#/waste` route | **Redirects** to `#/search-terms?tab=flagged` |
| `waste_terms` table | **Preserved** — annotation/history only |
| `GET /api/waste` | **Compatibility adapter**, rebuilt on canonical facts |

The route key survives in `PAGES` for one reason only: so `#/waste` resolves to
the redirect instead of 404-ing an existing bookmark. The page itself is gone —
keeping a hidden page alive purely for a URL is what §5 forbids.

---

## 3. The canonical durable identity

Defined once in `analysis/search_term_identity.py`:

    durable term identity := canonical campaign identity + normalized search term

`campaign_key` comes from the canonical campaign-identity contract — the Google
Ads `campaign_id` when the campaign is mapped, otherwise an explicitly-unmapped
label key. It is never a fuzzy name match where a stronger id exists, so the
same query text in two campaigns can never collide.

This is not a new key. It is the grain the canonical Search Terms evidence
service has always merged at, promoted out of a private helper into a shared,
documented module so the flagged view, the review record and the Action Queue
item provably describe one thing.

**Why ad group is evidence, not identity.** The canonical FACT key includes ad
group, keyword and match type (`idx_search_terms_unique_fact`) — that is the
grain of one ingested row, and it is what makes ingestion idempotent. The
durable REVIEW identity is deliberately coarser because it is what a human
reviews and acts on: §17 defines the action object as search term + campaign,
and §18/§44 require one durable term to produce one queue item. Keying by ad
group would split one human decision about one query into several. Ad groups,
keywords and match types are preserved as evidence inside the unit and shown in
the drawer.

Storage is a sha256 digest plus both readable components, because campaign
labels and user queries can contain any character (the in-memory key used
`\x00`, which PostgreSQL `TEXT` cannot store) and because a stored decision must
stay auditable without recomputing a hash.

---

## 4. The double-count defect, and the fix

`waste_terms` is **run-grained**: one row per waste term *per run*.

**Old `/api/waste`:** selected rows over a `run_date` window. A term seen by
five weekly runs appeared five times, and the row list's spend was five separate
snapshot rows of the same fact.

**Old Action Queue:** `SUM(spend_usd) ... GROUP BY search_term, campaign_name,
junk_category` over the same run window. That multiplied spend by the number of
runs *and* produced a second queue item whenever a term's junk category changed
between runs.

**The fix.** Both now read the canonical `search_terms` facts through
`build_flagged_search_terms`, aggregated at the durable identity. `search_terms`
carries a unique fact index over
`(source_date, campaign_name, campaign_id, ad_group, keyword, match_type,
search_term)`, so re-ingesting the same source-date fact **upserts** — it cannot
add spend, clicks, impressions or a term to the count.

`waste_terms` contributes classification annotations only: reason, matched rule,
CRM-junk confirmation, classification date. No number from it is ever summed.

Regression tests: `test_pr_ads_153d_waste_consolidation.py` (pure) and
`test_pr_ads_153d_pg_integration.py` (real PostgreSQL double-ingest).

---

## 5. The join contract (§24)

`waste_terms` rows carry `campaign_name` but **no** `campaign_id`. An annotation
is attached to a canonical unit only when its label identifies exactly one
campaign, both locally and globally:

1. only `mapped` units are eligible — an unmatched or non-Google unit has no
   confirmed identity to bind to;
2. the label must uniquely identify that unit among all units for the same term;
3. the label must not be globally ambiguous (shared by more than one canonical
   campaign id).

Ambiguous either way ⇒ no attachment, and the unit stays *Needs review*.

Annotations that no safe join could place are counted and reported as
`annotation_join.legacy_unresolved` on the flagged payload, and disclosed in the
UI. They are never guessed onto a term — and never silently dropped either,
because a reviewer needs to know historical evidence exists that current
identifiers cannot place.

---

## 6. Metric semantics (§11)

| KPI | Definition |
|---|---|
| **Flagged Terms** | Unique durable term identities currently matching the flagged contract |
| **Flagged Spend** | Canonical Google Ads spend for those terms in the selected evidence window (FX-verified subtotal only) |
| **Search-term-attributable SQLs** | Lifecycle SQLs safely attributed through the approved search-term attribution contract |
| **Review Needed** | Flagged terms with no finished local review decision |

No other KPI is published. There is no vanity metric and no AI score.

### What "flagged" means

A term is flagged because DURABLE evidence says so:

1. `search_terms.is_flagged_waste = true`, or
2. safely campaign-scoped `waste_terms` classification evidence.

It is **never** derived from `spend > 0 AND sqls = 0`. That rule would brand
every term whose attribution is merely unavailable as waste, which is exactly
the confusion §13 exists to prevent.

### SQL truth (§12)

The canonical SQL event is unchanged: **HubSpot Sales Qualified Lead lifecycle
entry**, dated by `hs_v2_date_entered_salesqualifiedlead` (PR-ADS-153B). Search
Terms only *attributes* that event; it does not define it.

The count is always labelled **Search-term-attributable SQLs**, never a naked
"SQLs", because it is a strict subset:

    search-term attributable ≤ campaign attributable ≤ Google Ads-source ≤ all source

### Attribution unavailable is not zero (§13, §33)

| Status | Renders as |
|---|---|
| `attributed` | the count |
| `known_zero` | `0` — attribution was available and found nothing |
| `unavailable` / `mapping_review` / `partial_attribution` | `—` |

A term with $500 spend and unavailable attribution is not equivalent to a term
with $500 spend and a proven zero, and unavailable attribution never contributes
to review priority.

---

## 7. Truth states (§32)

| State | Meaning |
|---|---|
| `reconciled` | Canonical facts present and SQL attribution resolvable |
| `partial` | Facts present but CRM attribution incomplete, or annotations could not be safely joined |
| `mismatch` | A flagged term carries no reason evidence — counts are withheld, not rendered as normal |
| `unavailable` | The canonical fact source could not be read |

None of these is ever converted into zero.

---

## 8. Waste reason taxonomy (§14)

Centralised in `analysis/waste_reason_taxonomy.py`. Raw `junk_category` values
come from `config/junk_patterns.yaml`; the mapping is justified inline by that
file's own `description` field.

| Raw category | Canonical reason |
|---|---|
| `job_seeker` | Job seeker |
| `student` | Consumer / B2C intent |
| `free_intent_english` / `_spanish` / `_arabic` | Low commercial intent |
| `shipper_intent` | Wrong product / service |
| `fraud_indicators` | Irrelevant intent |
| `informational`, `informational_industry` | Low commercial intent |
| `manual`, `manual_review` | Manual review flag |
| `other` | Other |
| *anything else* | **Unmapped — needs taxonomy review** |

Raw evidence is preserved alongside the mapped category on every row.

An unrecognised value maps to `unmapped`, a first-class VISIBLE state — never
silently to `other`. "We have never seen this reason" and "the rule said other"
are different facts, and collapsing them would hide a rules-file change from the
people reviewing its output. The flagged table badges an unmapped reason and
shows the raw value on hover.

This also fixed a live defect: `_QUEUE_FRAUD_CATEGORIES` tested for `"fraud"`, a
value the rules file never emits (it emits `fraud_indicators`), so the Action
Queue's fraud escalation had silently never fired.

---

## 9. Review state (§15, §16)

One durable local vocabulary in `analysis/search_term_review_state.py`, stored in
`search_term_review`, read and written by both Search Terms and the Action
Queue:

    unreviewed · keep · monitor · exclude_candidate · resolved

`exclude_candidate` is a **local recommendation**. It records that a human thinks
this query should be excluded. It is **not** evidence that a Google Ads negative
keyword exists — this system has no write path to Google Ads and cannot know
whether anyone acted on the recommendation.

Forbidden wording, enforced by test rather than by review:

> "Excluded" · "Negative keyword added" · "Removed from Google Ads"

Every review payload carries `applied_to_google_ads: false` explicitly.

**History survives decisions.** `first_flagged_at` / `latest_flagged_at` are
append-only and monotonic (`LEAST` / `GREATEST`), and observing a flag again
never touches `review_state`. A term that a human resolved stays auditable as
historically flagged, and a repeated sync cannot reopen it (§25, §44).

---

## 10. Action Queue integration (§17–§19)

One durable flagged term → one queue item, id `waste-review-<identity[:24]>`,
stable until the term is resolved. Terms whose review state is a finished
decision (`keep` / `resolved`) carry no remaining action and are excluded.

The item carries search term, campaign, spend, reason, evidence state, review
state, priority and a deep link to `#/search-terms?tab=flagged&term=<term>` — the
queue is an action surface, so the full investigation stays in Search Terms.

### Priority is explainable, never a score (§19)

| Component | Points | Rule |
|---|---|---|
| `high_spend` | 40 | Spend ≥ the configured review threshold |
| `spend_magnitude` | 0–20 | Proportional below the threshold, capped |
| `clear_disqualifying_intent` | 25 | Reason is irrelevant / job seeker / consumer / wrong product |
| `proven_zero_qualified_outcome` | 15 | Attribution **available** and found no qualified SQL |
| `repeated_occurrences` | 0–10 | Number of canonical fact rows in the window |
| `never_reviewed` | 10 | No human decision recorded |

Capped at 100; band is `high ≥ 60`, `medium ≥ 30`, else `low`. Every applied
component is echoed in `priority_reasons` and shown on hover.

**Deliberately not a component: unavailable attribution.** "We could not check
whether this term produced qualified outcomes" is not evidence of waste, and
letting it raise priority would launder an unknown into a signal.

---

## 11. Windows (§21)

Search Terms — including the Flagged tab — is a **Platform Evidence** surface on
the shared **evidence** window vocabulary (`7d/14d/30d/60d/180d/all_time`),
resolved by `analysis/evidence_windows.py`. It never moves onto CRM/revenue
business windows.

The retired page left no second window implementation behind: `waste` is gone
from `EVIDENCE_PAGES`, and the Flagged tab uses the Search Terms page's own
window. The Action Queue snaps its `days` lookback to the nearest canonical
evidence window that is not narrower than the request, so the queue and the page
describe the same window rather than the queue inventing its own.

---

## 12. Database audit (§38)

| Table | Grain | Writer | Status | Active consumers | 153G |
|---|---|---|---|---|---|
| `search_terms` | source_date × campaign_name × campaign_id × ad_group × keyword × match_type × search_term | `db/writers.upsert_search_terms` (Google Ads connector) | **Canonical fact** | Search Terms (all tabs), Action Queue, Campaign Evidence, Dashboard | Keep |
| `waste_terms` | run_id × run_date × search_term × campaign_name | weekly waste-detection run (`scheduler/weekly.py`) | **Annotation** | Search Terms flagged view + drawer (classification evidence only) | Review — the run-grained table is the double-count hazard; a term-grained annotation table is the successor |
| `search_term_review` | durable term identity | `db/search_term_review_repository` (local UI writes) | **Decision** (new in 153D) | Search Terms flagged view, Action Queue | Keep |

No table is dropped in this PR.

`waste_terms` must never become an alternative source of canonical Google Ads
spend, clicks or impressions. That is asserted by test, not left to convention.

---

## 13. API retirement manifest (§39)

| Route | Current consumer | Replacement | Delete in 153G? |
|---|---|---|---|
| `GET /api/search-term-evidence` | Search Terms → All Terms | — (canonical active) | No |
| `GET /api/search-term-evidence/flagged` | Search Terms → Flagged, Action Queue, `/api/waste` adapter | — (canonical active, new) | No |
| `POST /api/search-term-evidence/review` | Search Terms → Flagged drawer | — (canonical active, new) | No |
| `GET /api/search-term-evidence/patterns` | Search Terms → Patterns | — (canonical active) | No |
| `GET /api/search-term-evidence/term` | Search Terms drawer | — (canonical active) | No |
| `GET /api/search-term-evidence/export` | Search Terms export | — (canonical active) | No |
| `GET /api/waste` | **None in-product** — retained for external/bookmarked clients | `GET /api/search-term-evidence/flagged` | **Yes** |
| `GET /api/search-terms` | None in-product (superseded by `/api/search-term-evidence`) | `GET /api/search-term-evidence` | **Yes** |
| `GET /api/search-terms/summary` | None in-product | `GET /api/search-term-evidence` KPIs | **Yes** |
| `GET /api/search-terms/ngrams` | None in-product (superseded by `/patterns`) | `GET /api/search-term-evidence/patterns` | **Yes** |

Frontend / service code now unused or partially unused:

| Thing | Status | 153G |
|---|---|---|
| `loadWaste` and the whole standalone waste module | Deleted in this PR | Done |
| `wastePrefill` global | Deleted in this PR | Done |
| `_build_waste_queue_items` old `waste_terms` SQL | Replaced by the canonical service | Done |
| `analysis/core.py` waste-detection writer | Still the producer of `waste_terms` annotations | Keep until the annotation table is re-grained |
| `analysis/advisor.py` `outputs/waste_report.json` reader | File-based advisor path, no UI consumer | Review |

`/api/waste` exists after this PR **only** as a compatibility adapter for
external/bookmarked API clients. It has no first-party consumer. It is rebuilt on
canonical facts so that, even while it exists, it cannot double-count.

---

## 14. System Status ownership (§35)

System Status must present these as what they are:

* **Google Ads / Search Terms** — canonical source-fact freshness
  (`google_ads_api/search_terms`);
* **Search-term review annotations** — local analysis output
  (`analysis/waste_terms`) and local decisions (`search_term_review`).

The annotation table is not another external data feed and must not be presented
as one.

---

## 15. Reports and Dashboard (§36, §37)

No report consumed the old waste contract directly; the only in-product consumer
was the retired page's own loader. Any future export must read the canonical
flagged contract.

Dashboard gains no waste KPI. Dashboard is executive truth; actionable
search-term waste belongs in the Action Queue.

---

## 16. Governance (§3, §16, §46)

Read-only against every external platform. Allowed: Google Ads GET/read through
existing ingestion, local PostgreSQL reads/writes, and local review-state
updates. Prohibited and asserted by test: negative-keyword mutations, campaign /
keyword edits, bid or budget changes, offline conversion uploads, HubSpot
mutations, Mailchimp work.

The one write this PR introduces is a local review decision. It touches
`search_term_review` and nothing else.

No HubSpot email address is exposed anywhere in the consolidated page or the
queue.

---

## Related

- `docs/33_CANONICAL_CRM_FUNNEL.md` — the canonical lifecycle SQL event this page attributes
- `docs/24_UI_NAVIGATION_MODEL.md` — navigation model
- `docs/audits/PR-ADS-153A-MINIMUM-VIABLE-TRUTH-AUDIT.md` — the audit that found the overlap and the double-count
- PR-ADS-153G — legacy table / API deletion, driven by the manifest in §13 above
