# PR-ADS-157 — Campaign Evidence Certification

Base: `2040853` (PR-ADS-156-F4, merged and production-validated).

## §0 — Verified live dependency map (before-state)

Established by reading the current repository, not the roadmap.

### Campaign Evidence readers

| Consumer | Source | Grain | Window | Identity |
|---|---|---|---|---|
| `/api/campaigns` → `campaign_evidence_service.build_campaign_evidence` | canonical Google Ads daily spend + durable HubSpot event-date outcomes | campaign | **selected window** | `campaign_key` / `campaign_id` + approved aliases |
| drawer headline / lead-quality / countries / recent leads → `build_campaign_drawer_evidence` | same service as the table | campaign | **selected window** | `campaign_key` + alias set |
| drawer **keywords** (`api/server.py:1727-1754`) | legacy **`keywords`** snapshot | `DISTINCT ON (keyword, match_type)` latest `run_date` | **none — latest snapshot** | `lower(btrim(campaign_name)) = ANY(label_set)` — **display name** |
| drawer **waste terms** (`api/server.py:1761-1784`) | legacy **`waste_terms`** snapshot | `DISTINCT ON (search_term, junk_category, matched_pattern)` latest `run_date` | **none — latest snapshot** | `lower(btrim(campaign_name)) = ANY(label_set)` — **display name** |

The first two rows are the working foundation this PR preserves. The last two
are the certification gap.

### The three defects in the drawer previews

1. **Name-keyed identity.** Both queries match on the lowercased display-name
   set. Two campaigns sharing a display name share each other's keyword and
   flagged-term rows, and a name-only annotation crosses between campaign IDs.
2. **Snapshot, not window.** Both take the latest scheduler snapshot. The rows
   have no relationship to the Evidence Window the user selected. The keyword
   section discloses this (`keywords_note`); the waste section does not.
3. **Retired source.** `keywords` and `waste_terms` are the retired snapshot
   tables. `waste_terms.spend_usd` is read as a metric, which PR-ADS-153D
   established it is not.

### SQL reconciliation — produced, then dropped

`campaign_evidence_service` builds the full contract at line 395 via
`canonical_contact_outcome_service.page_reconciliation(WINDOW_EVIDENCE, window,
SCOPE_CAMPAIGN_ATTRIBUTABLE, consumer_count=summary.mapped_sqls)` and returns it
on `/api/campaigns` as `sql_reconciliation`.

`static/app.js` references `sql_reconciliation` exactly twice — line 8445
(source attribution) and line 11977 (Keyword Evidence). **Neither is the
Campaign page.** `loadCampaignEvidence()` reads `campaigns`, `summary`, `audit`,
`window`, `spend_currency` and `reporting_currency`, and never reads
`sql_reconciliation`.

Consequently the Campaign page publishes:

* a KPI labelled **"Confirmed SQLs"** (`summary.confirmed_sqls_total`) with the
  subline "Mapped Google Ads, this window";
* **"Overall CPQL"** derived from it;
* filters `has_sql` / `no_sql` and sorts `sqls` / `cpql`;

with no reconciliation gate on any of them. The scope word
"campaign-attributable" appears nowhere in the UI, and a `mismatch`,
`partial` or `unavailable` reconciliation renders exactly like a reconciled one.

### Reconciliation vocabulary (existing, unchanged by this PR)

`reconciled` · `partial` · `mismatch` · `unavailable` —
`canonical_contact_outcome_service` lines 76-79. On `unavailable` every count is
already `None` rather than `0` (line 489).

### What already satisfies §3 and §4 by composition

* `keyword_evidence_service.build_keyword_evidence(window, campaign=<campaign_key>)`
  — canonical `keyword_daily_facts`, filtered on `campaign_key` (line 562), over
  the selected window.
* `search_term_evidence_service.build_flagged_search_terms(window, campaign=<campaign_key>)`
  — canonical `search_terms` metrics, `waste_terms` declared
  `"classification annotation only"`, with a truth-state quarantine that returns
  no decision metrics on mismatch.

So both replacements are compositions of existing canonical services. No second
classification doctrine and no new aggregation logic is introduced.
