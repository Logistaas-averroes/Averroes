# 51 — Campaign Evidence drawer sections: bounded memory (PR-ADS-172A)

## 1. What failed

The daily in-app certification (PR-ADS-161J, capped by PR-ADS-169A) runs six
audits, each in a child process under a resident budget with a 320 MB
address-space backstop. Five hold. The sixth,
`scripts.audit_campaign_evidence_certification`, did not:

| day | recorded |
| --- | --- |
| 2026-11-21 | killed by SIGKILL |
| 2026-11-22 | crashed — "reached its memory backstop (320 MB of address space)", `peak_rss_mb` 287, 114 s |

So the platform read NOT CERTIFIED for a memory reason on both days. The cap
did what it is for. **It is unchanged**: `audit_address_space_mb: 320`,
`safety_margin_mb: 64`, and the audit is still in `AUDITS`.

For each of six Evidence Windows that audit calls `build_campaign_evidence`,
then `build_campaign_keyword_preview` and `build_campaign_flagged_preview` for
one campaign. Those are the builders `/api/campaigns` and
`/api/campaign-detail` run inside the web server.

## 2. What was measured, and on what

**Everything below is measured on a synthetic PostgreSQL dataset, not on
production.** Its row counts are the live tables' on 2026-11-22
(`search_terms` 228,043; `keyword_daily_facts` 98,152; `leads` 27,823;
`hubspot_contact_funnel` 21,866; `contact_source_classification` 21,889;
campaign daily spend 4,810). Its content is invented: 27 campaigns, 62,706
distinct queries with a long tail over 180 source dates (102,990 term ×
campaign units), 9,563 fact rows flagged, 9,596 `waste_terms` annotations.
Production's term mix is not known from here, so production's absolute figures
will differ; the one production number available — 287 MB resident when the
backstop stopped the audit — is reproduced by this dataset at 294 MB.

Peak resident growth of ONE call in a warm process (`VmHWM` after − `VmRSS`
before), MB:

| builder | 7d | 14d | 30d | 60d | 180d | all_time |
| --- | --- | --- | --- | --- | --- | --- |
| `build_campaign_evidence` (unchanged) | 24 | 24 | 24 | 25 | 26 | 34 |
| `build_campaign_keyword_preview` — before | 23 | 24 | 27 | 39 | 117 | 118 |
| `build_campaign_keyword_preview` — after | 22 | 23 | 26 | 29 | 34 | 39 |
| `build_campaign_flagged_preview` — before | 52 | 103 | 207 | 369 | 836 | 836 |
| `build_campaign_flagged_preview` — after | 18 | 18 | 29 | 53 | 113 | 114 |

The whole audit, six windows in one process:

| | peak resident | peak address space | wall |
| --- | --- | --- | --- |
| before, uncapped | 902 MB | — | 52 s |
| before, under the real 320 MB cap | 294 MB | 320 MB (the cap) | — |
| after, under the real 320 MB cap | 175 MB | 208 MB | 19 s |

Before, under the cap, the audit's own `except Exception` handlers swallowed
the `MemoryError`s and it exited 0 reporting `certified: true`; the
certification's guard band (PR-ADS-169A) is what refused that report. That is
the production message.

**Top allocators before** (`tracemalloc`, flagged preview, `all_time`, 755 MB
traced):

| MB | where |
| --- | --- |
| 269 | `db/search_term_repository.py` `_rows_as_dicts` — every grouped `search_terms` row, then every (term, campaign, day) cost row, as a dict |
| ≈ 315 | `services/search_term_evidence_service.py` `_build_population` — one unit dict and ten sets per term × campaign |
| 34 | `db/search_term_repository.py` — `sorted(...)` of each row's array columns |

Stage by stage (resident): `fetch_search_term_aggregates` +255 MB (102,990
rows); units +250 MB; `fetch_search_term_daily_costs` +264 MB (227,952 rows).
For the keyword preview: `fetch_keyword_daily_costs` +116 MB (98,152 rows; 70 MB
of it `db/keyword_repository.py` `_rows_as_dicts`).

**Accumulation across windows: none measured.** After the fix the six-window
run peaks at 175 MB and the `all_time` window alone at 175 MB. Nothing was
changed in the audit's loop.

## 3. Why the sections were hungry

Neither section is a query for one campaign. Each composes its page's builder
with a campaign filter, and that builder loads the account:

* **Flagged.** `build_flagged_search_terms` built every term × campaign unit in
  the window — flagged or not — then kept the flagged rows of one campaign. A
  preview of ten rows cost the whole fact table, twice (grouped rows, then
  per-day cost rows).
* **Keyword.** The units are aggregated in SQL (1,800 rows here), but per-date
  FX needs each criterion's daily cost, and that arrived as one dict per
  (criterion, day), each carrying two provenance lists nothing reads.

## 4. The fix

### 4.1 Flagged: load only the units that can be flagged

`build_flagged_search_terms(population="flagged_candidates")`, which the drawer
section now passes. Default `"complete"`: the Flagged page is unchanged.

A unit is one **normalized** term × one canonical campaign, flagged only by
durable evidence — a fact row with `is_flagged_waste = true`, or a
`waste_terms` annotation stored against one of its raw spellings. So
`fetch_search_term_aggregates(flagged_candidates=True)`:

1. asks the database which raw terms in the canonical scope carry either kind
   of evidence:

   ```sql
   SELECT search_term FROM search_terms WHERE <canonical scope>
   GROUP BY search_term
   HAVING BOOL_OR(is_flagged_waste IS TRUE)
       OR search_term IN (SELECT search_term FROM waste_terms)
   ```

2. adds every other raw spelling in the scope that normalizes to the same
   term. This is decided by `normalize_search_term` itself (NFKC, casefold,
   whitespace) over `SELECT DISTINCT search_term`, read through a server-side
   cursor — not by a copy of the rule written in SQL, which could disagree
   with it;
3. runs the two existing statements (grouped rows; source totals) with
   `AND search_term = ANY(%s)`, and returns the list so
   `fetch_search_term_daily_costs(search_terms=…, folded=True)` reads the same
   population.

An empty list selects nothing. An unconfigured account is unavailable, as
before. The merge in `_build_population` is the same code for both
populations.

**Why the numbers do not change.** Everything the flagged view publishes is a
function of flagged units, or of the annotation join:

* a flagged unit's terms are all loaded, in every spelling, so its summed
  clicks, cost, per-date FX, first/last seen and variants are the complete
  unit's;
* an annotation attaches only where its campaign label identifies one unit
  among **all units of the same normalized term** — those are loaded, in every
  campaign — and is not globally ambiguous (decided from spend and identity
  rows, not from units);
* `annotation_rows` counts annotations whose term is in the population: every
  such term is evidenced, so it is loaded;
* `truth_state` reads the flagged units, the annotation join and whether SQL
  attribution was available;
* `total_count`, the KPIs and the facets are computed over all flagged rows of
  the account before the page slice. **A preview limit is still never the
  total.**

One thing does depend on units that are not flagged: search-term SQL
attribution, once the CRM carries an exact user query (a contact attaches to a
unit only if exactly one unit matches, and the attributed share is counted over
all units). `leads` persists no query today (`LEADS_HAVE_SEARCH_TERM = False`),
so every unit's attribution is decided by its own mapping status. If that ever
changes, `_flagged_population` sees `population_has_text`, builds the complete
population instead, and the response carries no `population_scope`.

A narrowed response says so: `population_scope: "flagged_candidates"`. Its
`audit` block reconciles the narrowed units against source totals narrowed by
the same predicate.

### 4.2 Keyword: fold the per-day costs

`fetch_keyword_daily_costs(folded=True)` reads the same grouped statement
(minus the two unread array columns) through a server-side cursor and returns
`{(customer_id, campaign_id, ad_group_id, criterion_id): {iso_date: micros}}`.
`_build_population` always asks for it, so the Keyword Evidence page gets the
same reduction. The amounts are integers summed under the same keys; the FX
arithmetic that follows is untouched. Account scope is still applied in SQL by
`_scope(customer_ids)`.

### 4.3 Not held longer than used

`_build_population` (search terms) no longer keeps the grouped rows and the
cost rows referenced until it returns.

## 5. Proof

`tests/test_pr_ads_172a_campaign_evidence_memory.py` (20 cases; in CI's
PostgreSQL step and did-run list):

* **Equivalence, real PostgreSQL.** On a fixture built from the cases where
  narrowing could go wrong — a unit of three spellings with one flagged, the
  same term in two campaigns, an annotation whose campaign name belongs to two
  ids, an annotation with no campaign, a flag outside the short windows, an
  annotation for a term the window lacks — the narrowed and complete responses
  are **equal as whole dictionaries** for 6 windows × 5 campaign filters × 3
  sorts. Repeated at scale (30d, two campaigns) in a child process.
* **Measured, production-scale synthetic data.** Flagged preview growth
  ≤ 170 MB (114 measured), keyword ≤ 70 MB (39 measured), `all_time`, each in
  a fresh interpreter. The real audit, started by
  `audit_platform_certification._run_capped` with the configured backstop and
  classified by `classify`, holds with resident peak ≤ 75% of a 293 MB budget
  and with both sections available in every window.
* On `main` 17 of the 20 fail: "the flagged preview added 839 MB", "the keyword
  preview added 123 MB", and "reached its memory backstop (320 MB of address
  space): peak 320.0 MB is within 16 MB of it".

Every existing test passes unmodified.

## 6. Not fixed

1. **The Search Terms pages still build the complete population in the web
   server.** `/api/search-term-evidence` (Terms), `/flagged` (the page itself),
   `/patterns`, `/patterns/detail`, `/term`, `/export` and the `/api/waste`
   adapter: 839 → 702 MB of growth at `all_time` on this dataset (§4.3 only),
   211 → 170 MB at 30d. Terms and Patterns publish every unit, so narrowing
   does not apply; they need server-side pagination or aggregation of their
   own. The Flagged page could take `flagged_candidates` once its audit block's
   "every source row" claim is restated for a narrowed population — a contract
   decision, not made here.
2. **The flagged section now scales with the account's flagged and annotated
   terms**, not with the table: 13,387 candidate units here cost ≈ 110 MB,
   ≈ 30 MB of it full row dictionaries for flagged terms of other campaigns
   (built for the facets and the identity invariant). `truth_state` and
   `annotation_join` are account-wide by contract, which is why the read is not
   narrowed to the one campaign.
3. `build_campaign_evidence` (24–34 MB) is unchanged; most of it is the
   canonical contact reconciliation PR-ADS-169A already reduced.
4. Production peaks are not measurable from here. The first production run
   after deploy should show `campaign_evidence` `holds` with `peak_rss_mb` well
   under its budget in `platform_certification_runs.summary`.
