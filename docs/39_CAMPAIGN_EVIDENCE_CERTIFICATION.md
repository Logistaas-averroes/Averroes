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

---

## After-state — what each surface now reads

### Drawer sections

| Section | Source | Grain | Identity | Window | Account scope |
|---|---|---|---|---|---|
| headline card | canonical Google Ads daily spend + durable HubSpot event-date outcomes | campaign | `campaign_key` + approved aliases | selected | canonical spend read is customer-scoped |
| lead quality / countries / recent leads | same service as the table | campaign | `campaign_key` + alias set | selected | — |
| **keywords** | `keyword_daily_facts` via `keyword_evidence_service.build_campaign_keyword_preview` | criterion (`campaign_id` + `ad_group_id` + `criterion_id`) | `campaign_key` — **never a display name** | selected | **verified from returned rows** (see below) |
| **flagged search terms** | `search_terms` via `search_term_evidence_service.build_campaign_flagged_preview`; `waste_terms` annotation only | `search_term` × canonical campaign identity | `campaign_key` | selected | **enforced in SQL** by `canonical_scope()` |

### The account-scope asymmetry, and why it is declared rather than assumed

`db/search_term_repository.py` applies `canonical_scope(start, end)` — an account
+ provenance predicate in SQL. `db/keyword_repository.py:60` does not:

```
_WINDOW = "(%s::date IS NULL OR source_date >= %s) AND source_date <= %s"
```

That is a date filter with no `customer_id` clause. The first version of the
keyword adapter nevertheless declared

```
"scope": "account + campaign_id, selected evidence window"
```

which was false, and false in exactly the way this PR exists to remove — a claim
about a predicate that does not exist.

Two honest options were available: add account scoping to the shared keyword
query, which would change the Keyword Evidence page globally and is outside this
PR's remit; or prove the account instead of asserting it. The second was chosen.
`_unit_row` now emits `customer_id` (additive — existing consumers ignore it),
and the preview compares every returned row against the configured account's
exact spellings. A row from another account, or a row with no account recorded
at all, is a **scope violation**: the rows are withheld entirely rather than
shown with a caveat, and the section declares `account_scope: "verified from
returned rows (read is date-scoped)"`.

Treating "no account recorded" as "our account" is the assumption that produced
16,100 account-less twins in PR-ADS-156-F3. It is not made here.

### SQL publication policy

One decision function in `static/app.js`:

```js
campaignSqlPublication() -> { publish, state, reason, canonical }
```

`publish` is true only for `reconciliation_status === "reconciled"`. A missing
block is unproven, not permission.

| State | Aggregate SQL | Aggregate CPQL | Row SQL | Row CPQL | SQL-dependent status | `has_sql`/`no_sql`, `sqls`/`cpql` sorts |
|---|---|---|---|---|---|---|
| `reconciled` | published | published | published | published | published | enabled |
| `mismatch` | Reconciliation required | Reconciliation required | raw, labelled unreconciled | withheld | Reconciliation required | disabled + coerced to neutral |
| `partial` | withheld, reason stated | withheld | raw, labelled unreconciled | withheld | Reconciliation required | disabled |
| `unavailable` | withheld, never `0` | withheld | raw, labelled unreconciled | withheld | Reconciliation required | disabled |

Spend, leads, confirmed junk and wrong-fit are independent of the SQL scope and
stay published in every state. Withholding them would punish the operator for a
defect in an unrelated population — and remove the evidence they need to
investigate it.

Row-level SQL evidence stays visible on purpose. §2 requires it to be labelled
raw/unreconciled and to drive nothing; withholding it as well would destroy the
per-campaign detail that makes a mismatch diagnosable.

### The five populations, kept apart

1. `total_all_source_sqls` — every HubSpot-confirmed qualified contact
2. `google_ads_source_sqls` — those attributable to Google Ads
3. **`campaign_attributable_sqls`** — those with a canonical campaign identity · **this page**
4. `keyword_attributable_sqls` — uniquely keyword-attributable · Keyword Evidence
5. Google Ads **platform conversions** — reported by Google Ads, not HubSpot, on a
   different date basis, deduplicated differently

Each is a subset of the one above it, except (5), which is not in that lattice at
all. The UI labels are `Campaign-attributable SQLs`, `Attributed SQLs` where
column width is tight, and `Google Ads platform conversions` for (5).

### Legacy readers removed

Both direct queries in `_build_campaign_detail` are gone, along with the database
connection they used. `api/server.py` now holds no keyword or search-term
aggregation. Five readers of `keywords` / `waste_terms` remain elsewhere in that
file (lines ~1329, ~1491, ~2279, ~2290, ~2996); they are other consumers that
PR-ADS-157 explicitly defers, and no legacy table is dropped.
