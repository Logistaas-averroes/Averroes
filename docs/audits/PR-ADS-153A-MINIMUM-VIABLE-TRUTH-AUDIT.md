# PR-ADS-153A — Minimum Viable Truth & Product Simplification Audit

**Repository:** `Logistaas-averroes/Averroes` — `main` @ `cc269aa` (after PR-ADS-152)
**Mode:** AUDIT ONLY. No pages deleted, no production data changed, no Google Ads / HubSpot / Mailchimp mutation.
**Date:** 2026-08-12

This audit answers one question: *can every visible number in Averroes be traced to one canonical source, one business meaning, one dedup key, one event date, one named attribution scope, a freshness state, and fail-closed behaviour?* Today the answer is **no**, for reasons that are specific, provable in code, and fixable in a small number of coherent PRs.

All file:line references are against `main` @ `cc269aa`.

---

## 1. Executive findings

### 1.1 The funnel is not HubSpot's funnel

The entire CRM outcome doctrine of the product is **five lines of set membership** over one custom property, `mql_status`, at `db/writers.py:35-52`:

```python
_QUALIFIED    = {"CLOSED - Sales Qualified", "CLOSED - Deal Created"}
_IN_PROGRESS  = {"OPEN - Meeting Booked", "OPEN - Pending Meeting"}
_JUNK         = {"CLOSED - Job Seeker", "DICARDED"}
_WRONG_FIT    = {"CLOSED - Bad Product Fit", "CLOSED - Sales Disqualified"}
# everything else, including NULL → "unknown"
```

- **`status_category = 'qualified'` is NOT `lifecyclestage = salesqualifiedlead`.** It is the older MQL-status doctrine (`CLOSED - Sales Qualified` OR `CLOSED - Deal Created`). Nothing in the codebase ever reads `lifecyclestage` for classification. The two definitions can diverge arbitrarily and nothing reconciles them. `CLOSED - Deal Created` arguably corresponds to HubSpot `opportunity`, not SQL.
- **`lifecyclestage` is fetched and thrown away.** It is in `CONTACT_PROPERTIES` (`connectors/hubspot_pull.py:42`) but is never written to any table. `services/source_attribution_service.py:405,432` hardcode `"lifecycle_stage": None  # HubSpot lifecyclestage is not persisted` and the UI renders it "Unavailable" (`static/app.js:7823,7846`).
- **MQLs and Opportunities are not modelled anywhere.** Zero occurrences of `marketingqualifiedlead` outside docs/tests; no MQL count, no Opportunity count on any page. The UI funnel is `Leads → SQLs → Customers` (`static/app.js:2740`).
- **`hs_v2_date_entered_*` stage-entry timestamps are never fetched** (`grep -rn "hs_v2"` → zero hits) even though all five exist on the live portal. The SQL "event date" is the contact's **creation date** (`SQL_DATE_FIELD = "contact_created_at"`, `services/canonical_contact_outcome_service.py:65`; `db/revenue_repository.py:342` literally aliases `d.contact_created_at AS sql_date`). Every windowed SQL count is an **acquisition-cohort count, not a qualification-event count** — a contact created in January and qualified in June is a January SQL.
- **Three live `mql_status` values are silently unmapped**: `CLOSED - Bad Contact`, `CLOSED - No Response`, `RESELLER` all collapse to `unknown`, indistinguishable from "no verdict yet", and vanish from every junk-rate denominator (`api/server.py:1558, 2755`).
- **Free-text MDR comments can be written into `mql_status`**: `db/writers.py:357` — `mql_status = props.get("mql_status") or props.get("mql___mdr_comments")` — poisoning the raw-value audit trail (always maps to `unknown`).
- **The waste detector disagrees with the funnel on wrong-fit**: `analysis/core.py:102` counts `CLOSED - Bad Product Fit` as junk while `db/writers.py:38` and `analysis/core.py:188` classify it wrong-fit.

### 1.2 Customer and revenue truth is split across two disjoint ledgers

There is no "customer" entity. Every page defines **customer = one closed-won deal row** — but from **two non-overlapping tables**:

- **`gclid_attribution`** (GCLID-only — deals with no GCLID are dropped at `services/revenue_recovery_service.py:84-85` and `db/writers.py:1470-1472`) feeds Dashboard Overview/Revenue/Campaigns/Countries/Deals, the Revenue Decision Mart, and both ROAS pages.
- **`deal_source_attribution`** (every closed-won deal, all sources) feeds Revenue by Source and Dashboard Channels.

A won deal from Organic/Email/Referral **exists on Revenue by Source but is structurally absent from the Dashboard's customer count, revenue total, and deal ledger**. The two customer/revenue numbers cannot agree by construction.

Additional revenue-truth defects:

- **"Won" is decided by hardcoded stage ID `326093516` plus an `ILIKE '%won%'` label fallback** (`db/revenue_repository.py:32-33`). `hs_is_closed_won` appears nowhere in the repo. An unknown stage ID even **defaults to the won label** (`connectors/hubspot_pull.py:461`).
- **Churn Deal and Downgrade Deal stages are permanently invisible.** The pull filters `dealstage = won`; writers are upsert-only; no delete path exists. A deal that moves Won → Churn keeps counting as a customer forever. Meanwhile LTV uses a hand-edited YAML churn constant (`config/churn_input.yaml`, default 3%/month) with no relationship to the Churn Deal stage.
- **Revenue is never FX-converted while spend is rigorously FX-converted.** `props["amount"]` (whatever currency the HubSpot deal is in) is written straight into a column named `deal_amount_usd` (`connectors/hubspot_pull.py:459`). `deal_currency_code` and `amount_in_home_currency` are fetched only by the dead JSON connector and discarded. ROAS = unconverted deal-currency revenue ÷ FX-correct USD spend.
- **`gclid_attribution` is not keyed on the deal** — its unique key is a SHA1 including `campaign_name` and `match_status` (`db/writers.py:1404-1427`), so a re-labelled or re-matched deal mints a second row; reads survive only via `DISTINCT ON (deal_id) ORDER BY created_at DESC`, which means a deal's campaign/country can silently move between windows with no audit trail. Raw readers (`/api/gclid-attribution`) have no such guard.
- **Unit Economics runs on a third, dead lineage**: local JSON files + Windsor spend + substring campaign matching, no dedup, no FX, `hs_acv OR amount`, and deliberately **includes deals with missing `closedate`** (`analysis/roas_calculator.py:268-300`). It is the most divergent live page in the product.

### 1.3 PR-ADS-152's canonical SQL truth exists but is not consistently consumed

The canonical service (`services/canonical_contact_outcome_service.py`) and its named scopes are sound. But:

- Of **34 SQL-displaying surfaces**, only **4** consume the canonical population with scope-labelled UI (Dashboard Overview KPI, Revenue by Source group chip, Keyword Evidence, Search Terms "Attributed SQLs").
- **11 surfaces show a naked `SQLs` label over a narrower scope** — including the Dashboard Overview funnel, which renders a different number than the canonical KPI *on the same screen* (`static/app.js:2707` vs `:2417`).
- **`sql_reconciliation` mismatch metadata is computed and then ignored by the UI** everywhere except Keyword Evidence (`grep sql_reconciliation static/app.js` → 2 hits). The doctrine "a mismatch must never render as a normal count" (`canonical_contact_outcome_service.py:479-481`) is violated at the render layer on the ROAS pages and Lead Quality.
- **The mart's "campaign-attributable" claim is drifted**: mart `summary.sqls` = `source_type='paid_search'` ∧ non-pseudo campaign *label* (`db/revenue_repository.py:151-165`), while canonical `campaign_attributable` requires a *resolved* Google Ads campaign identity. The mart labels its number `SCOPE_CAMPAIGN_ATTRIBUTABLE` anyway (`revenue_decision_mart.py:436`).
- **`SCOPE_ALL_SOURCE` is misnamed**: it reads `leads`, which only ever receives PAID_SEARCH contacts (every `write_leads` caller passes `pull_paid_search_contacts*`). Non-paid SQLs exist only in `contact_source_classification`, which the scope never reads.

### 1.4 Country truth is blocked by a missing owner, not by a wrong gate

The ROAS by Country blocking behaviour is **correct and must not be weakened**. The reason it blocks is upstream:

- **Nothing schedules `run_google_ads_geo_sync`.** Its only non-test caller is the admin endpoint (`api/server.py:7658`). Canonical geo spend goes stale the moment the window advances past the last manual run → `missing_geo_dates` → residual unblock refuses (correctly) → page blocked until a human clicks a button on Revenue Health.
- **Geo has no coverage ledger, no freshness entry, no resume** (`services/google_ads_geo_sync_service.py:222, 397-408`), so the staleness is invisible on every health surface.
- Even when reconciled, `geographic_view` structurally omits unlocatable spend, so **geo total ≤ campaign total by design**; the PR-ADS-131 residual path handles this correctly.
- **Three different country-join rules coexist**: ROAS by Country joins spend↔revenue on raw lowercased strings (`revenue_attribution_service.py:199-201`); Dashboard Countries joins ISO-code-first (`dashboard_countries_service.py:150-164`); the drilldown uses code-then-normalized-name (`db/revenue_repository.py:490-575`). Blank-country revenue is **silently dropped** from ROAS by Country rows (`revenue_attribution_service.py:723-724`) but preserved as a residual on Dashboard Countries — the two pages report different totals for the same window.
- `services/country_codes.py` `_CODE_TO_NAME` is missing 11 codes that `_COUNTRY_CODES` contains (SG, MY, ID, TH, VN, PH, AU, NZ, LK, ZA, NG), and any 2-letter token is accepted as a "valid" ISO code (`:155-157`).

### 1.5 Windows: four vocabularies, two resolvers for the same label

- **Four window vocabularies** coexist (evidence `7d…all_time`; business `current_quarter…all_time`; legacy day buttons `7/14/30/60`; legacy ROAS `Nd` strings incl. `90d/365d`). Labels `7d/14d/30d/60d` exist in two incompatible sets — each 400s on the other's extra values.
- **The same evidence-window key has two implementations**: account-timezone inclusive-calendar-dates (Europe/London) used by Campaign/Keyword/Search-Term evidence, vs a Postgres `NOW() - INTERVAL` rolling lookback used by `/api/leads`, `/api/waste`, `/api/geo`, `/api/leads/country-summary`. "30 days" means different populations on different pages.
- **Five pages still window by `run_date`** (sync date, not business event date): Lead Quality, In Progress Leads, Flagged Waste Terms, Countries (evidence), Action Queue — the exact defect class PR-ADS-152 eliminated for the canonical layer.

### 1.6 The Lead Intelligence section is retire-ready

- **Lead Quality**: no unique metric — every figure exists on Campaign Evidence with *better* semantics (event-date window, exclusions applied, canonical spend + CPQL). Windows by `run_date`; no filters/drilldown/export; its own footnote (`static/index.html:653`) claims a 7-day grace period that the SQL path does not implement.
- **In Progress Leads**: has no backend at all — it re-calls `/api/leads` and filters client-side, using a **third** definition of "in progress" (includes `OPEN - Connecting`, contradicting `db/writers.py:36` and the doctrine), over a 1000-row truncated payload while ignoring the complete aggregates in the same response.
- **Flagged Waste Terms**: the Search Terms page's `flagged` state + junk-category filter + server-side full-set CSV export is a strict functional superset — minus one gap (bulk copy-all-filtered-terms). The page also has a real bug: it **sums raw `waste_terms` rows across runs** while deduping term counts, so its "Flagged Waste Spend" KPI is inflated by the number of runs in the window — the only consumer of `waste_terms` that does this.
- **Durable flag evidence caveat**: `search_terms.is_flagged_waste` is **never written by anything** (no `UPDATE … SET is_flagged_waste` exists). The weekly/monthly `waste_terms` snapshots (top-20 per run, from JSON inputs) are the **only** flag source in the product. Retiring the page is safe; retiring the `waste_terms` *writes* would blind the Search Terms flagged state entirely.

### 1.7 Platform data: canonical spend is healthy; everything around it leaks

- Canonical campaign spend (`google_ads_campaign_daily_spend` + coverage ledger + FX gating) is the best-governed dataset in the repo. But its **freshness key is broken** — writers stamp `source="google_ads"`, config expects `google_ads_api` (`incremental_sync.py:691` vs `freshness_service.py:126`), so the ROAS denominator has **no working freshness signal**. Same class of bug for `fx`, `gclid_matches`, `source_classification`.
- **The daily 9 AM incremental sync still pulls campaigns/keywords/geo from Windsor** (`incremental_sync.py:300,350,399`) — the only remaining Windsor dependency in scheduled paths; failures are swallowed per-dataset.
- The legacy `campaigns` table is **structurally corrupted**: three writers with different grains, all stamped `run_date = _today()`, plain INSERT with no conflict clause (`db/writers.py:276, 311-320`) — a naive `SUM(spend_usd)` over-counts ~14×. It still feeds Action Queue and Historical Intelligence.
- Weekly/monthly write **`country = NULL` into legacy `geo`** (`connectors/google_ads_source.py:265` hardcodes it), and `GET /api/geo` sums across overlapping run_ids without dedup (`api/server.py:1376-1391`).
- `metrics.conversions_value` is fetched and discarded; `search_terms.conversions` coerces NULL→0 while `keyword_daily_facts` correctly preserves NULL — two doctrines for the same fact.
- Scheduler concurrency guards are **process-local** (`api/scheduler.py:51-52`); with >1 Render instance every job runs twice, and the append-only legacy writers duplicate rows.

### 1.8 Product surface vs product need

- **107 API routes; ~24 orphans (22%)**, including two self-declared DEPRECATED ROAS routes, a 343-line dead trends handler, and two whole backend-only families (Mailchimp ×5, SQL-truth audit ×2).
- **6 different code paths** can answer "how did campaign X perform"; 7 overlapping health/freshness surfaces; two parallel search-term implementations and two n-gram engines both live.
- 29 DB tables, of which 7 hold campaign spend in some form, 3 hold `deal_amount_usd` at different grains, and 5 are Windsor-era run snapshots still being written.

**Conclusion:** the product's canonical layers (PR-ADS-144/146/152 era) are largely sound. The incoherence comes from (a) the pre-canonical pages and tables still being alive and rendered, (b) the funnel doctrine never having been reconciled to HubSpot lifecycle truth, (c) two disjoint revenue ledgers, and (d) missing ownership for geo. Minimum Viable Truth is achievable by consolidation and retirement, not by new construction.

---

## 2. Current architecture map

```
                     EXTERNAL TRUTH OWNERS
  Google Ads API           HubSpot CRM                Mailchimp     ECB FX
  (spend/keywords/geo)     (contacts, deals)          (read-only)   (frankfurter)
        │                        │                        │            │
        ▼                        ▼                        ▼            ▼
  connectors/google_ads_direct   connectors/hubspot_pull  mailchimp_   fx_rates
  connectors/google_ads_source   connectors/hubspot_deals pull         connector
  connectors/windsor_pull ⚠still (legacy JSON path)
  in daily incremental path      connectors/gclid_match ⚠dead file path
        │                        │
        ▼                        ▼
  ┌─ DURABLE CANONICAL ─────────────────────────────────────────────────────┐
  │ google_ads_campaign_daily_spend  + google_ads_spend_coverage + fx_rates │
  │ google_ads_account_daily_spend   google_ads_geo_daily_spend ⚠no owner   │
  │ keyword_daily_facts              search_terms                           │
  │ leads (snapshot ledger)          lead_truth_exclusions                  │
  │ gclid_attribution (rev ledger A) deal_source_attribution (rev ledger B) │
  │ contact_source_classification (cache, drift-repairable)                 │
  │ google_ads_campaign_identity (human-approved bridge)                    │
  └─────────────────────────────────────────────────────────────────────────┘
  ┌─ LEGACY RUN SNAPSHOTS (still written) ──────────────────────────────────┐
  │ campaigns ⚠corrupted grain   geo ⚠NULL countries   keywords   deals ⚠orphan
  │ waste_terms (only real waste-flag source)   gclid_coverage_snapshots    │
  └─────────────────────────────────────────────────────────────────────────┘
        │                                          │
        ▼                                          ▼
  CANONICAL SERVICES                         LEGACY INLINE SQL (api/server.py)
  canonical_contact_outcome_service          /api/leads /api/waste /api/geo
  (scopes: keyword ≤ campaign ≤              /api/leads/country-summary
   google_ads_source ≤ all_source)           /api/action-queue  — all run_date
  revenue_decision_mart (view=campaign/
   country/deal/source)                      analysis/core.py JSON pipeline
  dashboard_*_service ×6                     → outputs/*.json → rule_advisor
  campaign/keyword/search_term evidence      → Reports page (markdown)
  source_attribution_service                 analysis/roas_calculator (JSON)
  revenue_attribution_service                → Unit Economics ⚠live
        │                                          │
        ▼                                          ▼
  UI: Dashboard (6 tabs), ROAS pages,        UI: Lead Quality, In Progress,
  Revenue by Source, Deals, Campaigns,       Flagged Waste, Countries(geo),
  Search Terms, Keywords                     Action Queue, Reports, GCLID page
```

Two parallel generations are live simultaneously. Every "two pages disagree" report traces to a page on the left column being compared with a page on the right column, or to the two revenue ledgers.

---

## 3. Current page inventory

Navigation: 5 sections, 21 sidebar items, 6 Dashboard inner tabs, Search Terms inner tab pair, plus 1 unlinked page. Router registry `static/app.js:21`; sidebar `static/index.html:63-260`.

For each page: the user question, feeding APIs → tables, external truth owner, window/date, dedup, definitions, and overlap. (Full API↔page mapping is in §14; window detail in §12.)

### 3.1 Command Center

| Page | Question answered | APIs | Tables | Window / event date | Notes |
|---|---|---|---|---|---|
| **Dashboard → Overview** | Spend, revenue, SQLs, customers, ROAS + movement for a business window | `/api/dashboard/overview` | canonical spend, `leads`, `gclid_attribution`, fx | Business; spend `spend_date`, revenue `deal_close_date`, SQLs `contact_created_at` | Canonical GA-source SQL KPI ✔; funnel `SQLs` = narrower mart subset on same screen ✘ |
| **Dashboard → Revenue** | Revenue, customers, deal size, SQL→customer efficiency | `/api/dashboard/revenue` | `gclid_attribution` (ledger A) | Business; `deal_close_date` | Customers = GCLID-only deals; naked `SQLs` labels |
| **Dashboard → Channels** | Which source produced revenue | `/api/dashboard/channels` | `deal_source_attribution` (ledger B), `contact_source_classification`, canonical spend | Business; `deal_close_date`, `contact_created_at` | Hero SQL total mixes canonical + classification counts; trend chart re-derives from stale cache path |
| **Dashboard → Campaigns & Keywords** | Which campaigns/keywords produced SQLs and revenue | `/api/dashboard/campaigns` | mart (ledger A), `keywords` (legacy theme snapshot), `search_terms` | Business | Keyword outcome attribution does not exist — themes only |
| **Dashboard → Countries** | Which markets produced revenue | `/api/dashboard/countries` | canonical geo, mart (ledger A) | Business | ISO-code join; never blocks, withholds ROAS only; residual bucket is arithmetic |
| **Dashboard → Deals** | Deal/pipeline movement | `/api/dashboard/deals` | ledger A + `leads` | Business; `deal_close_date` / `contact_created_at` | `sql_to_customer_rate` divides deal-grain by contact-grain |
| **Action Queue** | What needs human review | `/api/action-queue` | **legacy** `campaigns`, `waste_terms`, `leads`, `geo`, `keywords` | Legacy days; **`run_date`** | Runs entirely on the legacy sync-date pipeline; not comparable with Dashboard numbers |
| **Reports** | What did the last advisor report say | `/reports/latest`, `/reports/latest/raw`, `/api/reports/roas/snapshots*` | `outputs/*.md` files (from `analysis/core.py` JSON pipeline), ROAS snapshot JSON files | Latest run | Different truth source from Dashboard; markdown viewer, no duplication of UI but no reconciliation either |

### 3.2 Platform Evidence

| Page | Question | APIs | Tables | Window / event date | Notes |
|---|---|---|---|---|---|
| **Campaigns** | Per-campaign spend + lead/SQL/junk/CPQL outcome | `/api/campaigns`; drawer `/api/campaign-detail` (+3) | canonical spend, `leads`, fx | Evidence (account-tz); spend `spend_date`, leads `contact_created_at` | Canonical-era page ✔; drawer waste/keyword panels are windowless all-time snapshots labelled "selected window" ✘ |
| **Search Terms** (+ Patterns tab) | Which queries ran; waste evidence; recurring patterns | `/api/search-term-evidence*` ×5 | `search_terms`, `waste_terms` (flag source), `platform_sql_attribution` | Evidence (account-tz); `source_date` | Canonical-era ✔; scope-labelled SQLs ✔ |
| **Keyword Evidence** | Per-criterion spend, quality, attributed SQLs | `/api/keyword-evidence*` ×4 | `keyword_daily_facts`, platform SQL attribution | Evidence (account-tz); `source_date` | The reference implementation: only page that renders reconciliation mismatch ✔ |
| **Countries (geo)** | Which countries consume spend / produce junk | `/api/geo` + `/api/leads/country-summary` | **legacy** `geo` (NULL-country rows, no run dedup), `leads` | Evidence label but **PG rolling `run_date`** | Windsor-era pipeline; disagrees with both Dashboard Countries and ROAS by Country by construction |

### 3.3 Lead Intelligence

| Page | Question | APIs | Tables | Window / event date | Notes |
|---|---|---|---|---|---|
| **Lead Quality** | Lead quality by campaign, junk rate | `/api/leads` | `leads` | Evidence label, **`run_date`** rolling | All-source, no exclusions, no filters/export; every metric duplicated better on Campaign Evidence; false grace-period footnote |
| **In Progress Leads** | Which leads are being worked | `/api/leads` (same call) | `leads` | same | No backend; client filter incl. `OPEN - Connecting` (3rd definition of in-progress); filters a truncated 1000-row page |
| **Flagged Waste Terms** | Which terms are flagged waste | `/api/waste` | `waste_terms` | Evidence label, **`run_date`** | Sums raw rows across runs (inflated spend KPI); superseded by Search Terms `flagged` state + Action Queue |

### 3.4 Revenue & Attribution

| Page | Question | APIs | Tables | Window / event date | Notes |
|---|---|---|---|---|---|
| **Deals** | Which closed-won deals produced revenue | `/api/revenue-performance?view=deal` | ledger A | Business; `deal_close_date` | GCLID-only ledger — non-GCLID won deals absent |
| **ROAS by Campaign** | Which campaigns are profitable | `/api/revenue-performance?view=campaign` (+detail, spend-proof) | canonical spend + identity + ledger A + fx | Business | Naked `SQLs`; ignores its own `sql_reconciliation` |
| **ROAS by Country** | Which countries are profitable | `/api/revenue-performance?view=country` (+detail, geo-reconcile) | canonical spend + canonical geo + ledger A + fx | Business | Blocks correctly; raw-string country join; drops blank-country revenue |
| **Revenue by Source** | Which source produced revenue | `/api/revenue-performance?view=source` (+platform detail) | **ledger B** + `contact_source_classification` + canonical spend | Business; `deal_close_date` / `contact_created_at` | Canonical GA-source SQL chip ✔; table SQL-rate mixes populations |
| **GCLID Attribution** | Can clicks be traced to leads/deals (forensics) | `/api/gclid-attribution`, `/api/attribution/quality`, `/api/gclid-coverage`, `/api/attribution/gclid-readiness`, `/api/attribution/confidence-summary` | `gclid_attribution`, `gclid_coverage_snapshots` | **Legacy day buttons** (only Revenue section page on them) | Diagnostics page in everyday nav; page-scoped KPI counts presented as window totals |
| **Unit Economics** | LTV, CAC, payback | `/api/reports/unit-economics` | **`data/hubspot_won_deals.json` local file** + Windsor JSON + churn YAML | Legacy `Nd`, `utcnow` | Dead lineage: no dedup, no FX, includes missing-closedate deals; most divergent live page |

### 3.5 Admin

| Page | Question | APIs | Notes |
|---|---|---|---|
| **Data Runs** | Did the schedulers run | `/scheduler/status`, `/api/runs`, `/runs/latest` | Not actually role-gated client-side (no `<li>` id, no `navigate()` check) — viewers can reach it |
| **System Status** | What is blocked, how fresh is each dataset | `/readiness`, `/api/datasets/freshness`, `/api/system/status-war-room`, `/api/system/search-terms-verdict`, `/api/gclid-coverage` | Freshness config references 3 nonexistent tables/columns (§15); canonical spend key mismatch means the ROAS denominator freshness is invisible here |
| **Revenue Health** | Can the revenue numbers be trusted; run repairs | 16 endpoints, 7 mutating (spend/FX/geo backfills, recovery, reconciliation, mapping) | The de-facto truth-operations console; parity panel hardcodes its own 0.02 tolerance copy (`revenue_decision_mart.py:568`) |
| **Admin Backfill** | Backfill legacy datasets | `/api/backfill/run`, `/api/backfill/status` | Synchronous in-request, in-memory state, not resumable |
| **Churn Input** | Override monthly churn for LTV | `/api/admin/churn-input` GET/POST | Writes local YAML only; disconnected from the HubSpot Churn Deal stage |

### 3.6 Hidden / unlinked

- **Historical Trends** (`historical-intelligence`): full page + loader + endpoint, no sidebar entry — reachable only by hash. Reads corrupted legacy `campaigns` + `geo` snapshots by `run_date`.
- **`ngrams`**: route alias → Search Terms Patterns tab. Empty stub section remains in `index.html:544`.
- **Mailchimp**: 5 API routes + 5 tables + 2 services, zero UI (paused per governance — correct).
- **SQL Truth Audit** (PR-ADS-152): `GET /api/audit/sql-truth` + repair POST, admin-only, no UI.

---

## 4. Keep / Consolidate / Retire matrix

| Surface | Verdict | Rationale / destination |
|---|---|---|
| Dashboard (6 tabs) | **KEEP** | The daily command surface. Fix SQL/customer scope labelling; inner tabs do *not* duplicate full pages (Deals tab is a summary; Deals page is the ledger) — but Channels vs Revenue by Source and Countries vs ROAS by Country each need one owner of "the number" (§16). |
| Action Queue | **KEEP (rebuild source)** | Unique decision value (ranked human review). Must be re-pointed from legacy `run_date` tables to canonical services; waste items' `primary_link` must point to Search Terms. |
| Reports | **KEEP (decide later)** | Unique artifact (durable narrative + ROAS snapshot history). Long-term: regenerate from canonical services instead of `analysis/core.py` JSON pipeline. |
| Campaigns | **KEEP** | Canonical-era evidence page. |
| Search Terms (+Patterns) | **KEEP** | Canonical-era; absorbs Flagged Waste Terms. |
| Keyword Evidence | **KEEP** | Reference implementation for scope labelling + mismatch handling. |
| Countries (geo evidence) | **CONSOLIDATE → rebuild on canonical geo** | Windsor-era pipeline (`geo` table, `run_date`, case-sensitive country strings). Either rebuild on `google_ads_geo_daily_spend` + canonical leads, or retire in favour of Dashboard Countries + ROAS by Country. NEEDS DECISION on which. |
| Lead Quality | **CONSOLIDATE → Leads** | See §17. |
| In Progress Leads | **RETIRE (absorb as view/filter)** | No backend of its own; becomes an "MQL working queue" view of Leads. |
| Flagged Waste Terms | **RETIRE (absorb)** | → Search Terms `flagged` state + Action Queue. Keep `waste_terms` writes. Add bulk copy-all-filtered to Search Terms. |
| Deals | **KEEP** | Becomes the canonical deal ledger page — after ledger unification (§9). |
| ROAS by Campaign | **KEEP** | Render its reconciliation metadata. |
| ROAS by Country | **KEEP** | Do not weaken blocking; fix upstream ownership. |
| Revenue by Source | **KEEP** | Canonical source page after ledger unification. |
| GCLID Attribution | **ADMIN ONLY** | Self-described forensics; no everyday decision. Move under Admin (or into Revenue Health as a panel). |
| Unit Economics | **KEEP page, REBUILD lineage** | The question (LTV/CAC/payback) is real; the current JSON lineage must be replaced with mart inputs. |
| Data Runs | **ADMIN ONLY (fix gating)** | Add the missing client-side gate. |
| System Status | **KEEP (admin)** | Merge candidates: absorb `/readiness`, reality-audit, search-terms-verdict into the war room. |
| Revenue Health | **KEEP (admin)** | Remains separate from System Status: System Status = pipeline freshness; Revenue Health = money-truth operations. Distinct questions, distinct audiences. |
| Admin Backfill | **KEEP (admin)** | Make jobs durable/resumable (§15). |
| Churn Input | **ADMIN ONLY (keep)** | Needed until Churn Deal stage is modelled; then re-evaluate. Not navigation-worthy for operators. |
| Historical Trends (unlinked) | **RETIRE** | Unlinked, reads corrupted `campaigns` snapshots; Dashboard previous-period deltas answer the question. |
| Legacy `ngrams` stub | **RETIRE** | Dead markup. |
| Mailchimp routes | **KEEP (dormant)** | Per non-goals; no UI until foundation is stable. |
| SQL Truth Audit routes | **KEEP (admin)** | Give it a small panel on Revenue Health instead of being invisible. |

**NEEDS DECISION (Youssef):**
1. Countries evidence page: rebuild on canonical geo vs retire into the two country pages that already exist.
2. Reports: keep legacy `analysis/core.py` narrative pipeline until 153G, or freeze it now.
3. Whether `OPEN - Connecting` belongs in the Leads "working queue" view (the In Progress page thought yes; the doctrine says it is "no verdict").

---

## 5. HubSpot funnel truth map

### 5.1 Live HubSpot ground truth (verified against the portal schema this session)

- **`mql_status`** (enumeration, 12 internal values): `OPEN - Connecting`, `OPEN - Pending Meeting`, `OPEN - Meeting Booked`, `CLOSED - Job Seeker`, `CLOSED - Bad Contact`, `CLOSED - Bad Product Fit`, `CLOSED - No Response`, `CLOSED - Sales Qualified`, `CLOSED - Sales Disqualified`, `CLOSED - Deal Created`, `DICARDED` (internal value one-R; label "DISCARDED"), `RESELLER`.
- **`lifecyclestage`**: `subscriber`, `lead`, `marketingqualifiedlead`, `salesqualifiedlead`, `opportunity`, `customer`, `evangelist`, `other`, plus custom `370543605` ("Discarded Contact") and `377714653` ("Reseller").
- **All five `hs_v2_date_entered_*`** datetime properties exist (lead / marketingqualifiedlead / salesqualifiedlead / opportunity / customer).

### 5.2 What Averroes ingests

`CONTACT_PROPERTIES` (`connectors/hubspot_pull.py:34-56`) fetches `mql_status`, `lifecyclestage`, `hs_lead_status`, analytics source fields, `createdate` — and **not** `hs_v2_date_entered_*`, **not** `lastmodifieddate`. Only `PAID_SEARCH` contacts reach the `leads` table (every `write_leads` caller); the all-source pull feeds only `contact_source_classification`.

Persisted per contact: `contact_id, campaign_name, keyword, country, mql_status, status_category, gclid, source_type, company, contact_created_at, hs_analytics_source, run_date` (`db/writers.py:389-393`). **Not persisted anywhere:** `lifecyclestage`, `hs_lead_status`, `hs_v2_date_entered_*`, email, `hs_latest_source*`.

### 5.3 Lineage chain (condensed)

```
HubSpot mql_status ─→ hubspot_pull (createdate-windowed, PAID_SEARCH filter)
  ─→ db/writers.write_leads
       :357 mql_status = mql_status OR mql___mdr_comments   ← pollution bug
       :375 _map_status_category (the 5-line doctrine)
       lifecyclestage discarded here
  ─→ leads (snapshot per contact per run)
  ─→ canonical_contact_outcome_repository (DISTINCT ON contact_key, latest run first,
       minus lead_truth_exclusions, join contact_source_classification)
  ─→ canonical_contact_outcome_service
       SQL_DEFINITION = "latest status_category = qualified"
       SQL_DATE_FIELD = "contact_created_at"           ← createdate proxy
       scopes: keyword ≤ campaign ≤ google_ads_source ≤ all_source
  ─→ consumers (revenue_repository, platform_sql_attribution, dashboards, mart)
```

### 5.4 Mismatch table (Averroes state → HubSpot truth)

| # | Averroes state | HubSpot source it should represent | What code actually uses | Verdict | Action |
|---|---|---|---|---|---|
| 1 | `status_category='qualified'` = "SQL" | `lifecyclestage = salesqualifiedlead` | `mql_status ∈ {CLOSED - Sales Qualified, CLOSED - Deal Created}` (`db/writers.py:35,44-45`) | **Divergent doctrine** — defensible sales-ops proxy, never reconciled to lifecycle; `CLOSED - Deal Created` is arguably Opportunity | 153B: decide canonical SQL = lifecycle `salesqualifiedlead` (recommended) with `mql_status` retained as diagnostic; add reconciliation |
| 2 | SQL event date | `hs_v2_date_entered_salesqualifiedlead` | `createdate` (`canonical…service.py:65`; `revenue_repository.py:342` `contact_created_at AS sql_date`) | **Wrong** — back-dates SQLs into acquisition cohort | 153B: fetch + persist stage-entry dates; window SQLs by entry date |
| 3 | "Customers" | `lifecyclestage=customer` / `hs_v2_date_entered_customer` | closed-won deal rows, stage `326093516`, dated `closedate` | **Questionable grain** — 2-deal customer counts twice; customer-stage contact with no won deal counts zero | 153E: define Customer explicitly (recommended: closed-won deal for revenue; contact-lifecycle for funnel counts; label both) |
| 4 | "Leads" | `lifecyclestage=lead` | every deduped **paid-search** `leads` row, any status, by `createdate` | **Misnamed** — it is "paid-search contacts created" | 153B: name it that, or count lifecycle Leads from all-source data |
| 5 | MQL count | `lifecyclestage=marketingqualifiedlead` | **absent** | **Gap** | 153B/153C: model MQLs (lifecycle + `mql_status` OPEN values as working queue) |
| 6 | Opportunity count | `lifecyclestage=opportunity` and/or deal creation | **absent** | **Gap** | 153B: decide canonical (recommended: lifecycle=opportunity for contacts; deal-created for pipeline objects) |
| 7 | `lifecyclestage` | — | fetched, never persisted, UI shows "Unavailable" | Wasted fetch | 153B: persist |
| 8-10 | `CLOSED - Bad Contact`, `CLOSED - No Response`, `RESELLER` | live enum values | unmapped → `unknown` | **Wrong** — silently drop from verdicted denominator | 153B: map (Bad Contact→junk, No Response→needs decision, RESELLER→own bucket) |
| 11 | `DICARDED` one-R | internal value | `_JUNK` includes it, guard test exists | **Correct** | none |
| 12 | comments fallback | — | `mql_status or mql___mdr_comments` (`db/writers.py:357`) | **Wrong** — free text into typed column | 153B: remove fallback |
| 13 | `OPEN - Connecting` | "no verdict yet" | collapsed into `unknown` with NULL + unmapped | **Provenance loss** (math survives) | 153B: distinct `no_verdict` value |
| 14 | Bad Product Fit | wrong_fit | `analysis/core.py:102` counts it junk for waste detection | **Internally inconsistent** | 153D: single classification import |
| 15 | `SCOPE_ALL_SOURCE` | all-source SQLs | reads paid-search-only `leads` | **Misnamed / undercounts** | 153B: all-source snapshot or rename scope |
| 16 | legacy endpoints window | business event date | `run_date` (`api/server.py:973,1522,2706`) | **Wrong grain** | 153C: move to event dates |
| 17 | change detection | `lastmodifieddate` | never fetched; re-window by `createdate` | **Stale-risk** — a contact re-qualified outside the sync window never updates | 153B: lastmodified-based incremental |

### 5.5 Doctrine verdict

**HubSpot should own funnel truth; today a custom property owns it.** The `mql_status` doctrine is not "wrong" — it encodes MDR verdicts that lifecycle stages may lag — but it is **not what the product claims to display** ("SQLs"), it has no reconciliation against lifecycle, and its event date is fabricated from `createdate`. PR-ADS-153B must make lifecycle the canonical funnel spine (stages + entry dates), keep `mql_status` as the MDR working-status dimension, and publish the mapping between them as data, not code comments.

---

## 6. Canonical metric registry

Doctrine per metric: one canonical source, table, dedup key, event date, scope. **Status** = does today's code comply?

### Advertising

| Metric | Canonical source | Table | Dedup key | Event date | Scope | Consumer pages | Status |
|---|---|---|---|---|---|---|---|
| Spend | Google Ads API | `google_ads_campaign_daily_spend` (`cost_micros` + FX at spend-date) | `(customer_id, campaign_id, spend_date)` | `spend_date` (account tz) | Google Ads only | Dashboard, Campaigns, ROAS pages, Rev by Source | ✔ canonical; ✘ legacy `campaigns/geo/keywords.spend_usd` still written & rendered (Action Queue, Countries, Historical) |
| Impressions / Clicks / CTR / CPC | Google Ads API | same + `keyword_daily_facts`, `search_terms` | as per table | `spend_date`/`source_date` | Google Ads | evidence pages | ✔ |
| Platform conversions | Google Ads API | `keyword_daily_facts.conversions` (NULL≠0) / `search_terms.conversions` (⚠NULL→0) | fact key | `source_date` | platform events, never SQLs | Keyword/Search-term drawers | ✘ inconsistent NULL doctrine; `conversions_value` discarded; no campaign-grain conversions table |
| Keyword spend | Google Ads API | `keyword_daily_facts` | `(source_date, customer, campaign, ad_group, criterion)` | `source_date` | Google Ads | Keyword Evidence | ✔ |
| Search-term spend | Google Ads API | `search_terms` (`cost_micros`) | 7-col fact key | `source_date` | Google Ads | Search Terms | ✔ (legacy `spend_usd` col is native-currency mislabel) |
| Country geo spend | Google Ads `geographic_view` | `google_ads_geo_daily_spend` | `(customer, country_criterion, campaign, spend_date)` | `spend_date` | Google Ads; residual explicit | Dashboard Countries, ROAS by Country | ✘ no scheduled writer, no coverage ledger, no freshness |

### Funnel (target doctrine — much of this does not exist yet)

| Metric | Canonical source | Table (target) | Dedup key | Event date (target) | Scope | Status today |
|---|---|---|---|---|---|---|
| Lead | HubSpot `lifecyclestage` history | latest canonical contact | contact_key = `COALESCE(NULLIF(contact_id,''),'id:'||id)` | `hs_v2_date_entered_lead` (proxy `createdate` acceptable if disclosed) | all-source; paid-search as named subset | ✘ "Leads" = paid-search contacts by createdate |
| MQL | HubSpot `lifecyclestage=marketingqualifiedlead`; `mql_status` OPEN values as working queue | latest canonical contact | contact_key | `hs_v2_date_entered_marketingqualifiedlead` | all-source + named subsets | ✘ not modelled |
| SQL | HubSpot `lifecyclestage=salesqualifiedlead` (decision needed: vs `mql_status` CLOSED-Sales-Qualified doctrine — see §5.5) | latest canonical contact | contact_key | `hs_v2_date_entered_salesqualifiedlead` | 4 named scopes (152) | ✘ MQL-status proxy, createdate-dated |
| Opportunity | HubSpot lifecycle `opportunity` (contacts) / deal createdate (pipeline) | contact + deal | contact_key / deal_id | `hs_v2_date_entered_opportunity` / deal `createdate` | named | ✘ not modelled |
| Customer | funnel: lifecycle `customer`; revenue: closed-won deal | contact + deal | contact_key / deal_id | `hs_v2_date_entered_customer` / `closedate` | named | ✘ deal-grain only, two disjoint ledgers |

### Revenue

| Metric | Canonical source | Table | Dedup key | Event date | Scope | Status today |
|---|---|---|---|---|---|---|
| Closed-won deal | HubSpot deal with `hs_is_closed_won=true` (target) | one unified deal ledger (target) | `deal_id` | `closedate` | all-source; attribution subsets named | ✘ stage-ID + ILIKE '%won%'; two ledgers; `hs_is_closed_won` unused |
| Closed-won revenue | deal `amount` + explicit currency doctrine (`deal_currency_code`, `amount_in_home_currency`) | same | `deal_id` | `closedate` | as above | ✘ raw `amount` labelled USD, no FX |
| Open pipeline | non-closed deals in Sales Pipeline | not synced today | `deal_id` | stage-entry | — | ✘ invisible (only won deals pulled); Dashboard Deals discloses gap |
| Campaign ROAS | canonical spend ÷ canonical revenue via identity map | mart | campaign_id | spend_date / closedate | campaign-attributable, labelled | ~✔ mechanics; ✘ numerator currency + GCLID-only ledger |
| Country ROAS | canonical geo spend ÷ country revenue | mart | country code (target: single join rule) | spend_date / closedate | country-attributed + residual | ✘ three join rules, blocked by missing geo owner |
| Source ROAS | canonical spend ÷ ledger-B revenue | mart view=source | deal_id | closedate | google_ads group only | ~✔ shape; ✘ ledger split |
| Unit economics | mart revenue/spend + churn model | (target: mart) | — | closedate windows | portfolio | ✘ dead JSON lineage |

**Rule for all of the above:** pages must consume the registry's service, never re-derive. Today Lead Quality, Countries(geo), Action Queue, Historical Trends, Unit Economics and the legacy ROAS routes all re-derive.

---

## 7. SQL consistency audit (post PR-ADS-152)

Canonical contract (`services/canonical_contact_outcome_service.py`): SQL = latest `status_category='qualified'`; date `contact_created_at`; dedup `contact_key`; scopes `keyword_attributable ≤ campaign_attributable ≤ google_ads_source ≤ all_source`; statuses `reconciled/partial/unavailable/mismatch` with "mismatch must never render as a normal count" (`:479-481`).

### 7.1 Consumption status

**34 SQL-displaying surfaces inventoried.** Full table with file:line references retained from the audit pass; summarized:

| Consumption class | Surfaces | Examples |
|---|---|---|
| Canonical, scope-labelled, mismatch-handled | 4 | Dashboard Overview KPI (`app.js:2417`), Revenue-by-Source group chip (`:7746`), Keyword Evidence (`:10789-10791`, withholds on mismatch `:10773`), Search Terms Attributed SQLs |
| Mart campaign-labelled subset under a naked `SQLs` label | 14 | Overview funnel (`:2707`), Revenue tab, Campaigns tab ("SQLs from Google Ads" `:4434`), Countries tab, Deals tab, both ROAS pages (`:14470,15529`), Campaign Evidence table column |
| Classification-cache mixed populations | 6 | Channels hero + trend (`dashboard_channels_service.py:344, 440-470` — the trend re-derives from the exact stale-cache path PR-152 diagnosed), Rev-by-Source table rate, Deals By-Source |
| Legacy `run_date` populations | 6 | Lead Quality, Countries(geo), Action Queue, `/api/summary`, trends, Historical |
| Snapshot-table verdicts | 4 | System Status badges, war room, waste page |

### 7.2 Named-scope invariant

The invariant `keyword ≤ campaign ≤ google_ads_source ≤ all_source` is enforced in the canonical service (`:369-375, 442-463`) and holds for its consumers. **However:**

- The mart's `summary.sqls` claims `SCOPE_CAMPAIGN_ATTRIBUTABLE` (`revenue_decision_mart.py:436`) while actually being "paid-search with a non-pseudo campaign **label**" (`db/revenue_repository.py:151-165`) — not "resolved Google Ads campaign identity". The two differ in both directions; the resulting `mismatch` status is computed and **not rendered**.
- `SCOPE_ALL_SOURCE` reads paid-search-only `leads` (§5.4 #15) — the top of the inequality chain is mislabelled product-wide.
- Two pre-152 SQL populations survive and are declared canonical-adjacent without being so: `db/platform_sql_attribution_repository.py:69-97` (PR-146C) and `db/revenue_repository.py:130` `fetch_lead_quality` — both keyed on `source_type='paid_search'` instead of the canonical acquisition group.

### 7.3 Naked-label flags (must be fixed at render layer)

1. Dashboard Overview funnel `SQLs` — different number than the canonical KPI on the same screen; backend ships `sqls_scope` (`dashboard_overview_service.py:930`), UI ignores it.
2. ROAS by Campaign / Country `SQLs` — `sql_reconciliation` in payload, never read (`grep sql_reconciliation static/app.js` → 2 hits, both other pages).
3. Dashboard Campaigns `"SQLs from Google Ads"` — value is the campaign-labelled subset, systematically ≤ the GA-source KPI one tab over.
4. Dashboard Countries / Deals / Revenue naked `SQLs`, incl. `revenue_per_sql_usd` dividing all-ledger revenue by a narrow SQL subset, and Deals' `sql_to_customer_rate` dividing across two populations.
5. Revenue by Source table `SQL Rate` — canonical numerator over classification-cache denominator.
6. Channels hero `SQLs` — sums one canonical population + seven classification populations; trend chart under the same label uses a third population.
7. Lead Quality `Confirmed SQLs` and Geo `SQLs` — all-source-ish but `run_date`-windowed; disclosure only in JSON payload.

### 7.4 Google Ads conversions vs CRM SQLs

**No numeric conflation found** — platform conversions are consistently separated and disclaimed (`platform_sql_attribution_service.py:12`; `keyword_evidence_service.py:106`; `app.js:10794, 11269`). Two adjacency risks: the Countries(geo) table renders `Conv.` beside `SQLs` with no disclaimer, and "SQL producer" vs "Spend without platform conversion" badges look alike but are different evidence classes.

---

## 8. Customer consistency audit

Full trace in §1.2/§9; per-page findings:

| Page | Customer definition | Table | Dedup | Event date | Consistent? |
|---|---|---|---|---|---|
| Dashboard Overview/Revenue/Campaigns/Countries/Deals, Mart, both ROAS pages | closed-won deal row | `gclid_attribution` (ledger A, GCLID-only) | read-time `DISTINCT ON (deal_id)` latest `created_at` | `deal_close_date` | Internally yes; excludes all non-GCLID customers |
| Revenue by Source, Dashboard Channels | closed-won deal row | `deal_source_attribution` (ledger B, all deals) | `UNIQUE(deal_id)` | `deal_close_date` | Internally yes; ≥ ledger A by construction |
| Dashboard Deals "SQLs not yet closed-won" | **contact**-grain | `leads` ⋈ ledger A | contact_key | `contact_created_at` | Cross-grain with the same page's customer KPI |
| Dashboard Countries residual | arithmetic gap (window total − Σ rows) | derived | n/a | n/a | Inherits ledger-A ceiling; silently understates unattributed |
| Unit Economics / legacy ROAS routes | `len(deals)` from local JSON | `data/hubspot_won_deals.json` | **none** | rolling `closedate` (missing dates included) | Not consistent with anything |
| Mailchimp audit | contact-grain distinct associated contacts | ledger B | contact_id | `deal_close_date` | Different grain again (documented) |

**Reconciliation requirements (deal/contact-level):**
1. One deal ledger keyed `UNIQUE(deal_id)` holding every closed-won deal (superset = today's ledger B) with attribution columns (gclid, campaign, country, source group) as nullable evidence — ledger A becomes an attribution view, not a population filter.
2. A contact↔deal association bridge with a deterministic primary-contact rule (today: `results[0]`, arbitrary — `connectors/hubspot_pull.py:487-489`; ledger B takes all associations; the same deal can land in different countries in A vs source groups in B).
3. Customer-as-contact (lifecycle) vs customer-as-deal (revenue) explicitly named on every surface.
4. Churn/Downgrade stages synced so a customer can stop being one (§9).

---

## 9. Revenue consistency audit

### 9.1 How "won" is decided

- **`hs_is_closed_won` and `hs_closed_amount` appear nowhere in the repo.**
- Ingest: HubSpot Search filter `dealstage IN ["326093516"]` (`connectors/hubspot_pull.py:59-73, 383-384`; legacy `hubspot_deals.py:226-229`).
- Read: `deal_stage = '326093516' OR deal_stage_label ILIKE '%won%'` (`db/revenue_repository.py:32-33`, applied at `:349-350, 396, 410, 464, 527, 616, 816`).
- **Unsafe default:** unknown stage IDs get labelled `"Deal Won / Payment Received"` (`hubspot_pull.py:461`) — combined with the ILIKE fallback, an unrecognized stage that slipped past the filter would count as revenue.
- Of the 9 real pipeline stages, **only Won is ever synced.** `ACTIVE_DEAL_STAGES` / `LOST_DEAL_STAGES` are declared and referenced nowhere (`hubspot_pull.py:71-73`). Proposal/Trials/Pricing/Invoice/Unresponsive/Lost/**Downgrade**/**Churn** are invisible. No open-pipeline metric can exist; churn never reverses a customer.

### 9.2 Three live revenue engines

| Chain | Path | Feeds | Key facts |
|---|---|---|---|
| **A — GCLID ledger** | `pull_closed_won_deals_in_range` → `revenue_recovery_service` (drops `gclid=None` deals, `:84-85`) → `write_gclid_attribution` (SHA1 `attribution_key`, `UNIQUE(attribution_key)` not deal_id) → `fetch_won_revenue` etc. (`DISTINCT ON (deal_id)` + won-predicate) | Dashboard, Mart, ROAS pages, Deals | first-associated-contact only; raw `amount` → `deal_amount_usd`; campaign from contact's `hs_analytics_source_data_1`; country from contact `ip_country`/`country` |
| **B — Source ledger** | `pull_closed_won_deals_with_sources_in_range` (same pull, keeps non-GCLID, all contacts) → `attribute_deal_row` (one group or `ambiguous`; never split) → `upsert_deal_source_attribution` (`UNIQUE(deal_id)`) → `fetch_source_revenue` (**no won-predicate at all** — safe only because writer feeds only won deals) | Revenue by Source, Channels | superset of A |
| **C — Legacy JSON** | `hubspot_deals.pull_won_deals` → `data/hubspot_won_deals.json` → `attribution_matcher` (hardcoded 13-tag substring matching) → `roas_calculator` | Unit Economics (live), 2 DEPRECATED ROAS routes | fetches `amount_in_home_currency`/`deal_currency_code`/MRR/ARR/ACV — then discards them into a file no production page trusts |

### 9.3 Currency doctrine

**Spend:** GBP native → USD via per-spend-date ECB rate; missing rate ⇒ `spend_usd=None` ⇒ ROAS withheld — exemplary fail-closed (`services/fx_service.py:69-107`).
**Revenue:** none. `props["amount"]` → `deal_amount_usd` with zero conversion or currency check (`hubspot_pull.py:459`; grep confirms no FX call in `revenue_attribution_service` / `revenue_decision_mart`). The one field that would verify the USD assumption (`deal_currency_code`) is fetched only by chain C and dropped.

### 9.4 Dedup & association

- Ledger A key fragility: `attribution_key = SHA1(gclid|contact_id|deal_id-or-first_url|campaign|keyword|match_status)` (`db/writers.py:1404-1427`) — relabel/rematch mints new rows; latest-write wins on read; deal history can silently migrate between campaigns/countries. Weekly `run_gclid_match()` output pollutes the same table with deal-less rows (`weekly.py:323`; excluded from revenue by `deal_id IS NOT NULL` but inflating `/api/gclid-attribution`).
- Deal→contact: A takes `results[0]`; B takes all + `ambiguous`. Same deal can bucket differently in A vs B.
- Deal→country: contact's `ip_country` else `country` (except `attribution_matcher` which inverts the precedence — `analysis/attribution_matcher.py:108-113`).

### 9.5 Revenue findings ranked

1. **[P0] Two disjoint ledgers** → Dashboard vs Revenue-by-Source customer/revenue disagreement by construction.
2. **[P0] Won-detection**: adopt `hs_is_closed_won`; remove ILIKE fallback and won-label default.
3. **[P0] Currency**: fetch `deal_currency_code` + `amount_in_home_currency`; convert or fail closed; rename/alias `deal_amount_usd` honestly.
4. **[P1] Churn/Downgrade invisibility** + YAML churn constant disconnected from CRM truth.
5. **[P1] Ledger A dedup key**; add `UNIQUE(deal_id)` semantics or move revenue reads to a deal-keyed ledger.
6. **[P1] `fetch_source_revenue` lacks a won-predicate** — future-proofing hazard.
7. **[P2] Chain C still live** behind Unit Economics; kill after 153E.
8. **[P2] Cross-grain `sql_to_customer_rate`**; two `compute_cac` implementations with different null semantics.

---

## 10. Google Ads dataset audit

| Dataset | Canonical table | Dedup key | Date col | Writer(s) | Coverage ledger | Freshness | Auto backfill | Verdict |
|---|---|---|---|---|---|---|---|---|
| Campaign daily spend | `google_ads_campaign_daily_spend` | (customer, campaign, spend_date) | `spend_date` | daily incremental (7-day lookback) + manual backfill | ✔ `google_ads_spend_coverage` ("missing ≠ zero") | ✘ **key mismatch** (`google_ads` vs `google_ads_api`) → no working signal | manual only | Healthy data, broken observability |
| Account daily spend | `google_ads_account_daily_spend` | (customer, spend_date) | `spend_date` | same, **best-effort try/except** (swallowed failures) | shares spend coverage | ✘ none | — | Reconciliation counterpart can silently stale |
| Geo daily spend | `google_ads_geo_daily_spend` | (customer, criterion, campaign, spend_date) | `spend_date` | **admin endpoint only** | ✘ none | ✘ none | ✘ no resume | **No owner** — root cause of Country ROAS blocking |
| Keyword facts | `keyword_daily_facts` | 5-col strict NOT NULL key | `source_date` | daily 30d + weekly/monthly + startup bootstrap + manual — all through one path | ✔ month-key ledger | ✔ | ✔ startup bootstrap | **Best-governed dataset** |
| Search terms | `search_terms` | 7-col COALESCE key | `source_date` | daily 2d + weekly 60d + monthly 30d | ✘ | ✔ | ✘; **skipped by incremental sync on stale Windsor rationale** (`incremental_sync.py:175-179`) — >2-day outage between Mondays = permanent hole | OK with a known gap class |
| Campaign identity | `google_ads_campaign_identity` | (customer, external_label) | audit only | human-only via admin UI; `auto_link_exact_matches` computes but never persists | n/a | ✘ | n/a | New campaigns stay unmapped indefinitely |
| FX rates | `fx_rates` | (rate_date, base, quote) | `rate_date` | daily **7-day window** + manual backfill | on-demand compute | ✘ (invalid `fx` source key) | ✘ | Historical windows need manual backfill forever |
| Platform conversions | **no table** | — | — | columns only; `conversions_value` fetched & discarded | — | — | — | Platform ROAS cross-check impossible |
| Legacy `campaigns`/`geo`/`keywords`/`waste_terms`/`deals` | run snapshots | none / run-scoped | `run_date` | weekly + monthly + daily-incremental (Windsor) | ✘ | partial | ✘ | Corrupted grains, NULL countries, still rendered |

**Earliest/latest dates and missing ranges cannot be proven from the repo** — production commands in §21 produce them.

Cross-cutting: no retry on any Google Ads read path (`google_ads_direct.py:154-167`); `fetch_geo_performance` swallows errors into "success, 0 rows" (`:469` + `sync_utils.py:41-42`); the daily incremental still pulls campaigns/keywords/geo from **Windsor** (`incremental_sync.py:300,350,399`) with env vars still provisioned (`render.yaml:42,44`).

---

## 11. Country / geo audit

### 11.1 Pipeline and blocking (current behaviour is correct)

Reconciliation gates (all native GBP, tolerance 2% `SPEND_VARIANCE_TOLERANCE`, `google_ads_spend_service.py:52`): campaign coverage complete ∧ FX complete ∧ geo-total within tolerance of canonical total (`google_ads_geo_sync_service.py:83-242`). Gap causes classified in priority order: `missing_geo_dates` → `campaign_spend_without_geo` → `geo_report_does_not_reconcile_by_design` → `totals_differ` (`:245-359`). The PR-ADS-131 residual unblock applies **only** to the by-design case with no missing dates/campaigns (`:362-394`) and appends an explicit "Unattributed / No Country" row so Σ(countries)+residual = campaign spend. `country_spend_trusted` gate at `revenue_attribution_service.py:1090-1102`. **Do not weaken any of this.**

### 11.2 Why it blocks in production

Causal chain: **no scheduled writer** for `run_google_ads_geo_sync` (only caller `api/server.py:7658`) → geo stale as soon as the window advances → `missing_geo_dates` populates → residual refuses (correctly) → blocked until manual run. Because geo has **no coverage ledger, no freshness entry, no failure ledger** (`google_ads_geo_sync_service.py:222`), no health surface explains why. Exact missing ranges are a production query (§21 Q6).

Structural residual: `geographic_view` omits unlocatable spend, so geo ≤ campaign **by design** — sync-defect vs by-design is exactly what the reason classifier distinguishes; the fix is ownership + ledger, not tolerance.

Note the stricter mart consumer: Revenue Decision Mart requires `verified` (residual not accepted) for `roas_by_country` (`revenue_decision_mart.py:498`) while Dashboard Countries accepts residual (`dashboard_countries_service.py:671,786`) — two different bars for the same page family.

### 11.3 Country identity defects

- Three join rules (raw string / ISO-first / code-then-name) across ROAS by Country, Dashboard Countries, drilldown (§1.4).
- Blank-country revenue dropped from ROAS rows (`revenue_attribution_service.py:723-724`) vs preserved as residual on Dashboard.
- `_CODE_TO_NAME` missing 11 codes; any 2-letter token accepted as ISO (`country_codes.py:155-187`).
- Contact country = `ip_country` else `country` — deal country is the **first associated contact's IP geography**, a disclosed estimate (confidence model already says country ROAS is estimate-grade until GCLID complete).
- Legacy Countries(geo) page: separate Windsor pipeline, `run_date`, case-sensitive strings, NULL-country rows from weekly/monthly, un-deduped SUM in `/api/geo`.

---

## 12. Window / date audit

### 12.1 Inventory

| # | Vocabulary | Values | Resolver | TZ | Users |
|---|---|---|---|---|---|
| 1 | Evidence | 7d/14d/30d/60d/180d/all_time | (a) `campaign_evidence_service._window_bounds` — account-tz inclusive calendar dates (Europe/London); (b) `api/server.py:828` `_evidence_date_clause` — Postgres `NOW()-INTERVAL` rolling | London vs UTC | (a) Campaigns, Keywords, Search Terms, canonical reconciliation; (b) Lead Quality, In Progress, Waste, Countries(geo) |
| 2 | Business | current_quarter/last_quarter/last_6_months/ytd/all_time | `analysis/business_windows.py:99-192` | UTC | Dashboard, ROAS pages, Deals, Rev by Source, Revenue Health |
| 3 | Legacy day buttons | 7/14/30/60 (valid set adds 90/365) | `_clamp_days` | PG UTC | Action Queue, Reports, GCLID page, Data Runs, admin pages |
| 4 | Legacy `Nd` | 7d…365d (no 180d/all_time) | `_parse_window` (`api/server.py:6567`) | `utcnow` | Unit Economics, deprecated ROAS routes, confidence summary |

Plus hardcoded `days=60` on System Status, ad-hoc 1–180 inputs on Historical Trends, and the diagnostics script's own 6-value set.

### 12.2 Same-label different-semantics flags

1. "30 days" Campaign Evidence (event-date, London, exclusions) vs Lead Quality (`run_date`, rolling UTC, no exclusions) — irreconcilable; the API even ships a runtime footnote saying so (`api/server.py:1063-1075`).
2. "30 days" Search Terms (`source_date`) vs Flagged Waste (`waste_terms.run_date` = analysis-run date).
3. "30" on Flagged Waste vs Action Queue waste items — different sessionStorage selectors, same table, one dedupes and one doesn't.
4. "All Time": evidence = no bound; business = no bound **plus** completeness caveat (`ALL_TIME_NOTE`).
5. "60d" exists in three vocabularies with three resolvers.
6. Campaign drawer waste panel says "in selected window" over a windowless all-time query (`api/server.py:1688-1732` vs `app.js:13083`).
7. `fetch_waste_evidence_for_terms` is unbounded — a 7d search-term row can be flagged by a months-old waste run.
8. Default drift: evidence 30d; day-buttons init 30 but fall back to 60; search terms default 14; business current_quarter.

### 12.3 Recommendation — two window systems (as intended), one resolver each

- **Evidence windows** (platform pages): keep vocabulary; make implementation (a) the only resolver — migrate `/api/leads`, `/api/waste`, `/api/geo`, `/api/leads/country-summary`, `/api/action-queue` off `run_date`/`NOW()-INTERVAL` onto account-tz bounds + business event dates.
- **Business windows** (CRM/revenue pages): keep as-is; move GCLID page and Unit Economics onto it (retiring vocabularies 3 and 4 from operator pages; day buttons may survive on admin diagnostics only).
- One shared `resolve_window_contract` (already exists at `canonical_contact_outcome_service.py:562-597`) becomes the single entry point; every payload stamps `date_field` (several already do).

---

## 13. Database table inventory

29 tables, all created in one hand-rolled idempotent DDL (`db/schema.py:26-1049`); no migration framework. Classification below; full column/reader/writer detail was verified per-table.

### Canonical truth
| Table | Key | Notes |
|---|---|---|
| `google_ads_campaign_daily_spend` (:769) | (customer,campaign,date) | Spend denominator for everything. Dangerous to delete. |
| `google_ads_account_daily_spend` (:815) | (customer,date) | Deliberate second measurement for reconciliation. |
| `google_ads_geo_daily_spend` (:840) | (customer,criterion,campaign,date) | Canonical geo; unowned writer (§10). |
| `keyword_daily_facts` (:428) | strict 5-col | NULL conversions ≠ 0 doctrine. |
| `search_terms` (:260) | 7-col COALESCE key | Carries dead `is_flagged_waste` (no writer) + mislabelled legacy `spend_usd`. |
| `fx_rates` (:870) | (date,base,quote) | Gap blocks ROAS (by design). |
| `lead_truth_exclusions` (:705) | `lead_id` | Decision ledger; deleting re-admits bad leads everywhere. |

### Raw evidence / de-facto canonical
| Table | Key | Notes |
|---|---|---|
| `leads` (:62) | none (read-time `DISTINCT ON contact_key`) | Sole substrate of canonical contact population; paid-search only; pre-109 rows have NULL `contact_created_at`. Maximum blast radius. |

### Attribution bridges
| Table | Key | Notes |
|---|---|---|
| `gclid_attribution` (:518) | `attribution_key` SHA1 (⚠ not deal_id) | Doing double duty as revenue ledger A; row-mint fragility; polluted by weekly gclid-match rows. |
| `deal_source_attribution` (:744) | `deal_id` UNIQUE | Ledger B; correct key shape; no won-predicate on reads. |
| `google_ads_campaign_identity` (:892) | (customer,label) | Human-approved decisions; not reproducible; third campaign-canonicalisation mechanism alongside `_CAMPAIGN_CANONICAL` dict + schema UPDATE pairs. |
| `contact_source_classification` (:722) | `contact_key` UNIQUE | Derived cache; known drift (dedicated repair service + endpoint exist). |

### Snapshot history / derived caches
`waste_terms` (:79 — only real waste-flag source; top-20 per run, append-only), `gclid_coverage_snapshots` (:584 — no unique key on snapshot_date, same-day duplicates), `mailchimp_campaign_reports` (current-state by design), `mailchimp_campaign_links`, `mailchimp_audience_snapshots` (correct dated-snapshot upsert pattern).

### Operational state
`runs` (:28 — ⚠ `ON DELETE CASCADE` from 6 fact tables), `migrations` (:162 — ⚠ truncating re-arms `TRUNCATE campaigns` and search-terms deletes; highest blast-radius-per-row table), `sync_batches` (:218), `sync_state` (:240), `revenue_recovery_jobs` (:645 — single-runner indexes exist for only 2 of 7 job types), `mailchimp_sync_state` (:1034 — duplicates generic sync_state), `google_ads_spend_coverage` (:793), `mailchimp_campaigns`/`mailchimp_sync_state`.

### Legacy — candidate retirement (after consumer migration)
| Table | Still written by | Still read by | Blocker to retirement |
|---|---|---|---|
| `campaigns` (:42) | weekly/monthly truth-tables + daily-incremental Windsor raw rows (corrupted mix) | `/api/summary`+trends (orphans), Action Queue, Historical Trends | 153G after Action Queue re-point |
| `geo` (:175) | weekly/monthly (NULL countries) + daily Windsor | `/api/geo` (Countries page), spend-reconcile fallback, Action Queue | Countries page decision |
| `keywords` (:194) | weekly/monthly/incremental | `fetch_keyword_theme_snapshot` → **live Dashboard Campaigns tab**, legacy audit | Migrate theme snapshot to `keyword_daily_facts` first |
| `deals` (:93) | weekly/monthly/incremental | **only** orphan `/api/deals` | Strongest single retirement candidate |
| `waste_terms` (:79) | weekly/monthly | Search Terms flag source, Action Queue, waste page | **Keep writes** until a durable flag writer exists |

### Duplicate-interpretation shortlist
Campaign spend ×7 tables; geo spend ×2; keyword facts ×2; `deal_amount_usd` ×3 (three grains, three keys); waste verdict ×2 (one writerless); contact state ×3 + computed population; campaign-name canonicalisation ×3 mechanisms; sync lifecycle ×3 (`runs`/`sync_batches`/JSONL) + `mailchimp_sync_state`.

### Incidental schema defects
Freshness config references nonexistent `historical_intelligence` table, `deals.close_date`, `gclid_attribution.matched_at` (`freshness_service.py:197,206,224`) → permanent `UNKNOWN_ROW_COUNT` on the freshness API; `db/schema.py:626-640` still carries the one-time `TRUNCATE TABLE campaigns` block marked "REMOVE THIS BLOCK".

---

## 14. API inventory

**107 routes** in `api/server.py`. Full route→consumer mapping verified against `static/app.js`; condensed inventory:

| Family | Routes | Consumer | Verdict |
|---|---|---|---|
| Infra/auth (`/`, `/health`, `/auth/*`, `/readiness`) | 6 | login/shell | KEEP |
| Scheduler (`/scheduler/status`, `/run/*` ×4, `/runs/latest`) | 6 | Data Runs | KEEP (admin) |
| Reports (`/reports/latest*`, `/api/reports/roas/snapshots*`) | 4 | Reports | KEEP |
| Dashboard contracts (`/api/dashboard/{overview,revenue,channels,campaigns,countries,deals}`) | 6 | 6 tabs | KEEP — the model shape |
| Revenue mart (`/api/revenue-performance` +3 details +audit) | 5 | 4 pages + Revenue Health | KEEP |
| Evidence families (`/api/campaigns`+detail, `/api/search-term-evidence*` ×5, `/api/keyword-evidence*` ×5(+refresh)) | ~13 | evidence pages | KEEP |
| Repair/job endpoints (recovery, lead-reconciliation, source-attribution, spend/geo/fx backfills, campaign-mapping ×3) | 15 | Revenue Health / Rev-by-Source admin panel | KEEP (admin) |
| Health surfaces (`/api/datasets/freshness`, war-room, search-terms-verdict, monitoring, reality-audit, window-semantics) | 6 | System Status | CONSOLIDATE → war room |
| Legacy raw-table reads (`/api/leads`, `/api/waste`, `/api/geo`, `/api/leads/country-summary`, `/api/action-queue`) | 5 | Lead Intelligence trio, Countries, Action Queue | REBUILD or RETIRE with their pages |
| GCLID family (5 read routes) | 5 | GCLID page + drawers | MOVE (admin) |
| Churn (`/api/admin/churn-input` ×2) | 2 | Churn Input | KEEP (admin) |
| Mailchimp ×5 | 5 | none | KEEP dormant |
| SQL truth audit ×2 | 2 | none | KEEP (admin; surface on Revenue Health) |

### Orphans (no frontend consumer) — 24+ routes, retirement candidates after page consolidation

Superseded: `/api/deals`, `/api/summary` (120 lines), `/api/keywords`, `/api/campaigns/{name}/detail`, `/api/dashboard/trends` (**343-line dead handler**), `/api/search-terms`, `/api/search-terms/summary`, `/api/revenue-attribution`, `/api/revenue-deals` (frontend explicitly forbids fallback to it), `/api/reports/roas/campaigns|countries` (**self-declared DEPRECATED, still reachable**).
Operator/diagnostic-only: currency audits ×2, `/api/keyword-evidence/audit`, reality-audit, window-semantics, `/api/fx-coverage`, `/api/source-attribution-health`, `/api/google-ads-spend-coverage-audit` (zero references anywhere).
Broken pairing: `/api/fx-backfill/status` orphaned — the FX backfill POST is fire-and-forget in the UI.

### Duplicate-answer families (§16 details)
Campaign performance ×6 code paths; country ×6; deals ×4; source ×4; search terms ×2 full implementations; n-grams ×2 engines; health ×7; run history ×3.

### Gating anomalies
Data Runs page not client-gated; `/api/backfill/status` auth while sibling job statuses are admin; `/api/campaign-mapping-review` readable by viewers; mutating `POST /api/audit/sql-truth/repair-classification` invisible in the product. 18 mutating routes total; none write to Google Ads/HubSpot (Phase-1 compliant).

---

## 15. Scheduler & sync ownership inventory

APScheduler in-process, tz Asia/Amman (`api/scheduler.py:168-208`): `daily` 06:00 → `run_daily_pulse`; `weekly` Mon 07:00; `monthly` 1st 08:00; `daily_incremental_sync` 09:00. Startup recovery: keyword bootstrap + Mailchimp backfill threads only (`api/server.py:132-157`).

### Writer-ownership map

| Dataset | Writers | Ownership verdict |
|---|---|---|
| Canonical campaign/account spend | daily incremental (7-day) + manual backfill — one upsert path | **Clean** (observability broken — key mismatch) |
| Canonical geo spend | admin endpoint only | **No owner** (P0) |
| `fx_rates` | daily 7-day + manual | Clean; history manual-only |
| `keyword_daily_facts` | 5 entry points, one sync path | **Model dataset** |
| `search_terms` | daily 2d / weekly 60d / monthly 30d (+MCP import) | OK; daily-incremental skip = gap class |
| `leads` / `contact_source_classification` / ledgers A,B | all four schedulers + repair/backfill jobs | OK paths, but `_sync_hubspot_deals` only discovers deals via GCLID contacts created in a 30-day window (`incremental_sync.py:518-521`) — old-contact closings missed while reporting success |
| Legacy `campaigns`/`geo`/`keywords` | weekly + monthly (Google Ads adapter) **+ daily incremental (Windsor)** | **Ownership conflict**, mixed grains, NULL countries |
| `waste_terms` | weekly + monthly | Sole flag producer — protect |
| `google_ads_campaign_identity` | human only | By design; needs new-campaign nudge |

### Failure-mode inventory
Process-local concurrency guards (multi-instance double-runs); no retry on Google Ads reads; geo/`fetch_geo_performance` "success with 0 rows"; account-spend best-effort swallow; `api_backfill_run` synchronous + in-memory; FX backfill progress in-memory; stuck `running` recovery jobs block relaunch via 409; invalid sync-source keys (`google_ads`, `fx`, `gclid_matches`, `source_classification` not in `VALID_SYNC_SOURCES`, `db/writers.py:1054-1058`) logged-and-proceeded.

**Goal state:** one ingestion path per canonical dataset (spend ✔, keywords ✔, search terms ✔-ish, geo ✘, FX ✔) + durable job state + a freshness registry whose keys are validated against writers at test time.

---

## 16. Duplicate / legacy systems (consolidated)

1. **Two page generations**: canonical services (mart, evidence, dashboards) vs legacy inline-SQL pages (`run_date`) — §3.
2. **Two revenue ledgers** + one dead JSON chain — §9.
3. **Two search-term implementations** (`/api/search-terms*` legacy vs `search-term-evidence` family) and **two n-gram engines** (`analysis/ngrams.py` via campaign drawer vs patterns service).
4. **Two lead-quality engines**: SQL path (`/api/leads`) vs JSON path (`analysis/core.py` → Reports); the JSON path has the grace period the UI footnote advertises, the SQL path doesn't.
5. **Two evidence-window resolvers**; four window vocabularies — §12.
6. **Three campaign-name canonicalisation mechanisms** (writers dict / schema UPDATEs / identity table).
7. **Three run-history stores** (`runs`, `sync_batches`, JSONL) + Mailchimp's private sync_state.
8. **Two waste-flag stores**, one writerless — §1.6.
9. **Windsor connector still live** in daily incremental + backfill scripts, with legacy tables and provisioned env vars.
10. **Legacy Countries page pipeline** vs canonical country pages.
11. **Deprecated ROAS routes + attribution_matcher + gclid_match + backfill_gclid stub** — dead code paths that read like live engines.

---

## 17. Data-risk findings ranked

### P0 — actively producing wrong or contradictory user-visible numbers
| # | Finding | Evidence |
|---|---|---|
| P0-1 | Funnel doctrine ≠ HubSpot lifecycle; SQL = MQL-status proxy dated by `createdate`; MQL/Opportunity unmodelled; `hs_v2_*` unused | §5 |
| P0-2 | Three live `mql_status` values unmapped → silent `unknown`; MDR-comment fallback pollutes `mql_status` | `db/writers.py:35-57` |
| P0-3 | Two disjoint revenue/customer ledgers (GCLID-only vs all-deals) | §9.2 |
| P0-4 | Won = stage-ID + `ILIKE '%won%'` + won-label default; `hs_is_closed_won` unused; churn/downgrade invisible | §9.1 |
| P0-5 | Revenue currency never converted; `deal_amount_usd` mislabel | §9.3 |
| P0-6 | Canonical geo spend unowned → Country ROAS self-blocks; no geo coverage/freshness | §11.2 |
| P0-7 | Naked/narrower `SQLs` labels on 11 surfaces; mismatch metadata computed but unrendered; mart scope claim drifted; `SCOPE_ALL_SOURCE` misnamed | §7 |
| P0-8 | Five pages window by `run_date` under evidence-window labels; two resolvers for the same vocabulary | §12 |

### P1 — integrity/observability hazards that will produce P0s
| # | Finding |
|---|---|
| P1-1 | Freshness key mismatches (canonical spend, fx, gclid, classification) + freshness config referencing nonexistent tables/columns |
| P1-2 | `gclid_attribution` SHA1 key row-minting; weekly match-row pollution; latest-write-wins attribution migration |
| P1-3 | Legacy `campaigns` corrupted grain (3 writers, `run_date=today`, ~14× over-count) feeding Action Queue + Historical |
| P1-4 | Legacy `geo` NULL countries + `/api/geo` un-deduped SUM |
| P1-5 | Windsor still in daily incremental; silent per-dataset failure |
| P1-6 | Process-local scheduler locks; non-durable backfills; stuck-job 409s; no Google Ads retry; geo "success 0 rows" |
| P1-7 | Country identity: 3 join rules, dropped blank-country revenue, `_CODE_TO_NAME` gaps, loose 2-letter ISO acceptance |
| P1-8 | `is_flagged_waste` writerless; waste truth = top-20-per-run snapshots from JSON inputs; waste page double-counts spend |
| P1-9 | `contact_source_classification` drift (mitigated by repair service, but repair is manual + invisible) |
| P1-10 | Incremental deals discovery misses closings on old contacts while reporting success |
| P1-11 | Unit Economics dead lineage live in production |
| P1-12 | `fetch_source_revenue` no won-predicate; `sql_to_customer_rate` cross-grain |

### P2 — hygiene / debt
Orphan routes (24+) incl. deprecated-but-reachable ROAS; dead handlers (343-line trends); `deals` table + `/api/deals` orphan pair; dead attribution scripts (`backfill_gclid.py` stub, `gclid_match`, `attribution_matcher`); `search_terms.conversions` NULL→0 vs keyword doctrine; `conversions_value` discarded; GCLID page page-scoped KPIs; tolerance constant copy in mart parity panel; mailchimp/sync_state duplication; `migrations` re-arm hazard + leftover TRUNCATE block; gating anomalies (§14); Data Runs page gate; default-window drift; `runs` CASCADE deletes.

---

## 18. Navigation simplification recommendation

Adopting the §16-target with adjustments justified by decision value / canonical data / workflow necessity:

```
COMMAND CENTER          GOOGLE ADS              CRM & REVENUE           ADMIN
  Dashboard               Campaigns               Leads                   Data Runs
  Action Queue            Search Terms            Deals                   System Status
  Reports                 Keywords                Revenue by Source       Revenue Health
                          Countries               ROAS by Campaign        Backfill
                                                  ROAS by Country         (Churn Input)
                                                  Unit Economics          (GCLID forensics)
```

- **GCLID Attribution → Admin** (forensics; no everyday decision; readiness/confidence panels can live inside Revenue Health). ✔ per hypothesis.
- **Churn Input stays out of operator nav** (admin-only config; revisit once Churn Deal stage is modelled). ✔.
- **Revenue Health and System Status remain separate** — different question (money-truth operations vs pipeline freshness), different blast radius, both admin. ✔, but consolidate the 7 health APIs behind the war room.
- **Dashboard inner tabs do not duplicate full pages** except by data-source accident: Channels vs Revenue by Source and Countries tab vs ROAS by Country must consume the same service so the tab is a summary of the page, not a competitor. Deals tab (summary) vs Deals page (ledger) is a healthy pattern.
- **Reports does not duplicate Dashboard** (markdown narrative from a different pipeline) — keep, but its generator must eventually read canonical services or it will keep publishing a third set of numbers.
- **Countries under GOOGLE ADS** = evidence page **rebuilt on canonical geo** (or dropped — NEEDS DECISION §4).
- 21 sidebar items → **15** (17 counting parenthesized admin utilities).

## 19. Proposed target architecture

**One truth spine:**

```
Google Ads API ─→ canonical facts (spend/keyword/search-term/geo + coverage + FX)
HubSpot ─→ raw contact store (lifecycle + mql_status + stage-entry dates, latest-state + history)
        ─→ canonical contact outcome service (single funnel definitions + named scopes)
HubSpot deals ─→ ONE deal ledger (UNIQUE deal_id, hs_is_closed_won, currency-resolved,
                 all stages incl. churn) ─→ attribution views (gclid/campaign/country/source)
        ─→ revenue decision mart (only revenue reader) ─→ dashboards + pages
Two window systems, one resolver each; every payload stamps scope + date_field + freshness;
fail-closed everywhere (null/blocked, never fake zero); coverage gaps rendered, not implied.
```

Enforcement of the ten MVT rules maps: Rule 1-2 → canonical contact service is the only lead/SQL reader; Rule 3 → lifecycle spine (153B); Rule 4 → canonical Google Ads facts only (153G removes Windsor); Rule 5 → unified closed-won ledger (153E); Rules 6-7 → attribution = views over the ledger with mandatory scope labels (render-layer contract, 153B/E); Rule 8-9 → existing coverage/FX gating extended to geo + freshness registry fixed (153F/B); Rule 10 → §18 nav (153C/D/G).

---

## 20. Implementation roadmap

Six PRs, dependency-ordered. (The suggested shape survives the audit with one change: window/date unification is folded into 153C, where its consumers are being rebuilt anyway, rather than a separate PR.)

### PR-ADS-153B — Canonical CRM Funnel Truth
**Objective:** HubSpot lifecycle becomes the funnel spine. Fetch + persist `lifecyclestage`, `hs_lead_status`, all `hs_v2_date_entered_*`, `lastmodifieddate`; add a raw latest-state contact store (all sources, not just paid search); map the three unmapped `mql_status` values; remove the MDR-comments fallback; split `unknown` into `no_verdict` vs `unmapped`; define Lead/MQL/SQL/Opportunity/Customer against lifecycle with stage-entry event dates; publish the lifecycle↔mql_status mapping as data; fix `SCOPE_ALL_SOURCE`; extend the canonical service to true all-source; incremental sync keyed on `lastmodifieddate`.
**Dependencies:** none. **Risk:** SQL counts will visibly move (dates shift from createdate to entry-date; unmapped statuses reclassify) — ship with a before/after reconciliation report per window. **Affected pages:** every SQL/lead surface. **Acceptance:** mismatch table §5.4 rows 1-17 all resolved or explicitly waived; `/api/audit/sql-truth` green across pages; production check §21-Q1/Q2 returns zero unmapped/polluted rows. **Production validation:** §21 Q1,Q2,Q9,Q10 before and after.

### PR-ADS-153C — Leads Consolidation + Lead Intelligence Retirement
**Objective:** one **Leads** page (views: Overview / Leads / MQLs / SQLs / Opportunities / Customers / Disqualified-Wrong-Fit) built on the 153B canonical service; retire Lead Quality + In Progress Leads (nav + routes `/api/leads` after re-point); migrate `/api/leads/*`, `/api/waste`, `/api/geo`, `/api/action-queue` off `run_date` onto account-tz event-date windows (single evidence resolver); render scope labels + reconciliation states on the Dashboard funnel and mart pages (closing §7.3).
**Dependencies:** 153B. **Risk:** medium — UI consolidation; keep route aliases. **Affected:** Lead Intelligence section, Dashboard funnel, Action Queue. **Acceptance:** no surface windows by `run_date`; no naked `SQLs` label (scope suffix or tooltip everywhere); In-Progress semantics decided and documented; Leads page views reuse `canonical_contact_outcome_service` + existing evidence UI components. **Validation:** §21 Q1 re-run; window-semantics diagnostic clean.

### PR-ADS-153D — Search-Term Waste Consolidation + Navigation Cleanup
**Objective:** retire Flagged Waste Terms; Search Terms owns waste evidence (add bulk copy-all-filtered-terms; fix drawer "selected window" mislabel; bound `fetch_waste_evidence_for_terms` or disclose age); Action Queue owns actions (re-point `primary_link` `api/server.py:2679`, `app.js:13641-13649`); add a real writer/workflow for durable flags (either write `is_flagged_waste` from review actions or formally bless `waste_terms` as the flag ledger and remove the dead column); single junk-classification import (fix `analysis/core.py:102` Bad-Product-Fit inconsistency); retire legacy search-terms routes + ngrams stub; move GCLID Attribution to Admin; hide Historical Trends.
**Dependencies:** none (can parallel 153B). **Risk:** low. **Acceptance:** dependency checklist in §Part-3 all re-pointed; `waste_terms` writes preserved; waste-spend KPI dedup fixed or page gone. **Validation:** none needed beyond tests.

### PR-ADS-153E — Customer / Revenue Canonical Reconciliation
**Objective:** one deal ledger (UNIQUE deal_id; every closed-won deal incl. non-GCLID; attribution as nullable evidence columns/views); won = `hs_is_closed_won` (remove ILIKE + won-label default); fetch `deal_currency_code` + `amount_in_home_currency`, define currency doctrine (fail closed on unknown currency); sync all pipeline stages (open pipeline visible; churn/downgrade reverse customers or are explicitly surfaced); deterministic deal→contact rule; migrate Unit Economics onto the mart; delete chain C from live routing; deprecate `deals` snapshot table + orphan revenue routes.
**Dependencies:** 153B (contact keys), independent of 153C/D. **Risk:** high — revenue numbers move (non-GCLID deals join the Dashboard; currency correction may change totals). Ship with deal-level reconciliation export. **Affected:** all Dashboard tabs, Deals, both ROAS pages, Revenue by Source, Unit Economics. **Acceptance:** Dashboard customers == Revenue-by-Source customers for any window; §21 Q3/Q4/Q5 clean; parity panel green with shared tolerance constant. **Validation:** §21 Q3-Q5 before/after.

### PR-ADS-153F — Country Geo Truth Repair
**Objective:** schedule `run_google_ads_geo_sync` (daily incremental step) with coverage ledger + freshness entry + resume; fix sync-source key mismatches (spend/fx/gclid/classification) and the three phantom freshness configs; one country-join rule (ISO-code-first) across mart/ROAS/drilldown; stop dropping blank-country revenue (route to residual); complete `_CODE_TO_NAME`; validate 2-letter codes; align mart vs dashboard blocking bars (`verified` vs residual-accepted). Do **not** change tolerance or gate logic.
**Dependencies:** none; before or parallel to 153E (both touch `revenue_attribution_service` — coordinate). **Risk:** low-medium. **Acceptance:** ROAS by Country populates without manual sync after a fresh window; missing-date query §21 Q6 empty going forward; the same country row set on Dashboard Countries and ROAS by Country. **Validation:** §21 Q6, Q7.

### PR-ADS-153G — Final Product Simplification / Legacy Retirement
**Objective:** §18 navigation; retire orphan routes (superseded list §14) + dead handlers + deprecated ROAS routes; Countries page decision executed; retire `deals` table; migrate keyword theme snapshot off legacy `keywords`, then stop writing legacy `campaigns`/`geo`/`keywords`; remove Windsor from incremental sync + scripts + env; Reports generator onto canonical services (or explicit freeze); scheduler hardening (durable locks/jobs, retries, startup recovery, remove TRUNCATE block); System Status health-API consolidation; gating fixes.
**Dependencies:** 153C, 153D, 153E, 153F. **Risk:** medium (deletion PR — everything behind dependency maps in this audit). **Acceptance:** every remaining route has a consumer; every table has one writer; System Status shows canonical spend/geo freshness truthfully; Windsor env vars removable. **Validation:** §21 Q8 zero rows after legacy write stop; smoke of every nav page.

---

## 21. Production checks (read-only, for Youssef on Render)

Repository analysis cannot prove production coverage/counts. Run this single read-only psql block (`psql $DATABASE_URL -f mvt_audit_checks.sql` or paste whole); every statement is a SELECT.

```sql
-- ==== Q1: mql_status values present vs mapping (expect: no unexpected raw values;
--          rows with category 'unknown' and a non-enum raw value = pollution/unmapped)
SELECT mql_status, status_category, COUNT(*) AS contacts
FROM (SELECT DISTINCT ON (COALESCE(NULLIF(contact_id,''),'id:'||id::text))
        mql_status, status_category
      FROM leads ORDER BY COALESCE(NULLIF(contact_id,''),'id:'||id::text), run_date DESC, id DESC) t
GROUP BY 1,2 ORDER BY 3 DESC;

-- ==== Q2: MDR-comment pollution (expect 0 rows; any row = free text in mql_status)
SELECT COUNT(*) AS polluted FROM leads
WHERE mql_status IS NOT NULL AND mql_status NOT IN
 ('OPEN - Connecting','OPEN - Pending Meeting','OPEN - Meeting Booked',
  'CLOSED - Job Seeker','CLOSED - Bad Contact','CLOSED - Bad Product Fit',
  'CLOSED - No Response','CLOSED - Sales Qualified','CLOSED - Sales Disqualified',
  'CLOSED - Deal Created','DICARDED','RESELLER');

-- ==== Q3: two-ledger reconciliation (expect ledger B ≥ ledger A; the gap = customers
--          missing from Dashboard)
SELECT 'ledger_A_gclid' src, COUNT(DISTINCT deal_id) deals, SUM(deal_amount_usd) amt
FROM (SELECT DISTINCT ON (deal_id) deal_id, deal_amount_usd FROM gclid_attribution
      WHERE deal_id IS NOT NULL AND deal_close_date IS NOT NULL
        AND (deal_stage='326093516' OR deal_stage_label ILIKE '%won%')
      ORDER BY deal_id, created_at DESC) a
UNION ALL
SELECT 'ledger_B_source', COUNT(DISTINCT deal_id), SUM(deal_amount_usd)
FROM deal_source_attribution;

-- ==== Q4: ledger-A duplicate rows per deal (expect few; each = attribution rewrite)
SELECT deal_id, COUNT(*) rows FROM gclid_attribution
WHERE deal_id IS NOT NULL GROUP BY 1 HAVING COUNT(*)>1 ORDER BY 2 DESC LIMIT 20;

-- ==== Q5: deal stages actually stored (expect only 326093516 / won label;
--          anything else = ILIKE or default-label leakage)
SELECT deal_stage, deal_stage_label, COUNT(*) FROM gclid_attribution
WHERE deal_id IS NOT NULL GROUP BY 1,2 ORDER BY 3 DESC;

-- ==== Q6: geo coverage gap — campaign-spend dates with no geo rows
--          (expect: recent dates listed = the stale-geo root cause; empty = healthy)
SELECT c.spend_date, SUM(c.cost_micros)/1e6 AS campaign_spend
FROM google_ads_campaign_daily_spend c
LEFT JOIN google_ads_geo_daily_spend g ON g.spend_date=c.spend_date
WHERE g.spend_date IS NULL AND c.cost_micros>0
GROUP BY 1 ORDER BY 1 DESC LIMIT 40;

-- ==== Q7: dataset date coverage (earliest/latest per canonical table)
SELECT 'campaign_spend' t, MIN(spend_date)::text, MAX(spend_date)::text, COUNT(*) FROM google_ads_campaign_daily_spend
UNION ALL SELECT 'geo_spend', MIN(spend_date)::text, MAX(spend_date)::text, COUNT(*) FROM google_ads_geo_daily_spend
UNION ALL SELECT 'fx_rates', MIN(rate_date)::text, MAX(rate_date)::text, COUNT(*) FROM fx_rates
UNION ALL SELECT 'keyword_facts', MIN(source_date)::text, MAX(source_date)::text, COUNT(*) FROM keyword_daily_facts
UNION ALL SELECT 'search_terms', MIN(source_date)::text, MAX(source_date)::text, COUNT(*) FROM search_terms
UNION ALL SELECT 'leads', MIN(run_date)::text, MAX(run_date)::text, COUNT(*) FROM leads;

-- ==== Q8: legacy campaigns-table corruption scale (rows per run_date;
--          large same-day counts = mixed-grain writes)
SELECT run_date, COUNT(*) rows, COUNT(DISTINCT campaign_name) campaigns,
       ROUND(SUM(spend_usd)::numeric,2) summed_spend
FROM campaigns GROUP BY 1 ORDER BY 1 DESC LIMIT 15;

-- ==== Q9: classification-cache drift (expect ~0; >0 = stale Revenue-by-Source rows)
SELECT COUNT(*) AS stale FROM contact_source_classification c
JOIN (SELECT DISTINCT ON (COALESCE(NULLIF(contact_id,''),'id:'||id::text))
        COALESCE(NULLIF(contact_id,''),'id:'||id::text) k, status_category
      FROM leads ORDER BY 1, run_date DESC, id DESC) l ON l.k=c.contact_key
WHERE l.status_category IS DISTINCT FROM c.status_category;

-- ==== Q10: leads missing business event date (withheld from canonical windows)
SELECT COUNT(*) FILTER (WHERE contact_created_at IS NULL) AS null_event_date,
       COUNT(*) AS total_rows FROM leads;
```

HubSpot-side cross-check (no DB): in HubSpot, filter contacts `lifecyclestage = salesqualifiedlead` for a quarter and compare with the Dashboard GA-source SQL count for the same business window — the delta is the lifecycle-vs-mql_status divergence that 153B must explain.

---

## 22. Completion-standard answers

1. **What is a Lead?** Today: any deduped paid-search contact row (any status), dated `createdate`. Target: HubSpot lifecycle Lead, all-source, entry-dated; "paid-search contacts" becomes a named subset.
2. **What is an MQL?** Today: not modelled. Target: lifecycle `marketingqualifiedlead` (entry-dated), with `mql_status` OPEN values as the working queue dimension.
3. **What is an SQL?** Today: latest `status_category='qualified'` ⟸ `mql_status ∈ {CLOSED - Sales Qualified, CLOSED - Deal Created}`, dated `createdate`, four named scopes. Target: lifecycle `salesqualifiedlead`, entry-dated, same scope algebra, mql_status kept as diagnostic.
4. **What is an Opportunity?** Today: not modelled. Target decision (153B): contact-side = lifecycle `opportunity`; pipeline-side = deal created; both named, never conflated.
5. **What is a Customer?** Today: a closed-won deal row (two disjoint ledgers). Target: revenue-Customer = closed-won deal (unified ledger); funnel-Customer = lifecycle `customer`; labels always disambiguate.
6. **What is Closed-Won Revenue?** Today: `amount` of stage-326093516 deals, unconverted, GCLID-ledger for most pages. Target: `hs_is_closed_won` deals, one ledger, deal_id-deduped, `closedate`-dated, currency-resolved.
7. **Google Ads-source attribution?** Contact's `hs_analytics_source = PAID_SEARCH` → `google_ads` group (engine-blind; documented).
8. **Campaign attribution?** Contact `hs_analytics_source_data_1` label → canonical identity resolution (exact-normalized / approved manual); unmapped = withheld, never $0.
9. **Keyword attribution?** `hs_analytics_source_data_2` text only; **no criterion-level join exists** — keyword SQLs are the narrowest named scope and platform keyword facts are evidence, not outcomes.
10. **Country attribution?** Spend: `geographic_view` criterion → ISO code (+ explicit residual). Revenue: first-associated-contact `ip_country`→`country` fallback — estimate-grade, disclosed. One join rule after 153F.
11. **Which table owns each fact?** §6 registry + §13 inventory (spend → `google_ads_campaign_daily_spend`; contacts → `leads`+canonical service (153B: raw contact store); revenue → unified deal ledger (153E); geo → `google_ads_geo_daily_spend`; keywords → `keyword_daily_facts`; terms → `search_terms`; FX → `fx_rates`; exclusions → `lead_truth_exclusions`; identity → `google_ads_campaign_identity`).
12. **Which page owns each decision?** §4/§18: Dashboard = daily posture; Action Queue = human actions; Leads = funnel truth; Deals = revenue ledger; ROAS pages = budget decisions; Search Terms = waste evidence; Keywords = keyword evidence; Revenue Health = truth operations.
13. **Which pages can disappear?** Lead Quality, In Progress Leads, Flagged Waste Terms, GCLID Attribution (as everyday nav), Historical Trends, ngrams stub — plus the Countries evidence page pending decision.
14. **Which legacy services/tables may eventually be retired?** Tables: `deals`, `campaigns`, `geo`, `keywords` (after theme-snapshot migration), `gclid_coverage_snapshots` (fold into freshness). Code: Windsor connector + backfill scripts, `analysis/roas_calculator` chain C, `attribution_matcher`, `gclid_match.run_gclid_match`, `backfill_gclid.py`, legacy search-terms routes, `/api/dashboard/trends`, `/api/summary`, deprecated ROAS routes.
15. **Why can no two pages silently disagree again?** Because after 153B–153G: every metric resolves through one registry service (Rules 1–5); attribution is a named-scope view, never a redefinition (Rules 6–7); labels must carry scope and payloads carry `date_field` + reconciliation status, and a mismatch renders as withheld, not as a number (Rule 8); coverage/freshness gaps are first-class UI states with working keys (Rule 9); and the legacy tables/routes that made silent disagreement possible no longer exist (Rule 10 + §16 retirement). The `/api/audit/sql-truth` reconciliation (extended to customers/revenue in 153E) runs as a test-time and admin-visible invariant, so a future divergence is a red audit, not a support ticket.

---

*End of PR-ADS-153A audit. No implementation was performed; no production data, Google Ads, HubSpot, or Mailchimp state was modified.*

---

## Follow-up status (appended — the audit above is historical and unmodified)

**As of PR-ADS-153E-A, August 2026.**

| Roadmap item | Status |
|---|---|
| PR-ADS-153A — Minimum Viable Truth audit | **Merged** (this document) |
| PR-ADS-153B — Canonical CRM Funnel Truth | **Merged** |
| PR-ADS-153C — Canonical Leads Experience | **Merged** |
| PR-ADS-153D — Search-Term Waste Consolidation | **Merged** |
| PR-ADS-153E-A — Canonical Deal Ledger Foundation | **Completed after merge** — shadow mode |
| PR-ADS-153E-B — Revenue Consumer Cutover & Unit Economics Migration | **Next** |
| PR-ADS-153F — Geo synchronization | Remains |
| PR-ADS-153G — Legacy table / route deletion | Remains |
| Phase 2 / OCT | **Blocked** — not started, not authorized |
| Six-month read-only governance | **ACTIVE** |

### §9 revenue findings — disposition

| Finding | Status after 153E-A |
|---|---|
| [P0] Two disjoint ledgers | Canonical `hubspot_deal_ledger` built and reconciled at deal grain. **Consumers not yet switched** — that is 153E-B. |
| [P0] Won-detection (`ILIKE`, won-label default) | **Fixed** in the canonical layer: `hs_is_closed_won` only, fails closed; the connector's unknown-stage→won default is removed. Legacy readers still carry the old predicate until 153E-B. |
| [P0] Currency | **Fixed** in the canonical layer: `deal_currency_code` + `amount_in_home_currency` fetched, verified home currency required, close-date local FX, fail closed. |
| [P1] Churn/Downgrade invisibility | **Fixed** — all nine stages synced and surfaced. No negative revenue invented. |
| [P1] Ledger A dedup key | Canonical ledger is `deal_id`-keyed. `gclid_attribution` is unchanged and retained as a comparison source. |
| [P1] `fetch_source_revenue` lacks a won-predicate | Unchanged — legacy reader, addressed at cutover. |
| [P2] Chain C (local JSON) still live | Unchanged — Unit Economics migrates in 153E-B. |
| [P2] Cross-grain `sql_to_customer_rate` | Unchanged — 153E-B. |

Production checks Q3–Q5 are superseded for the canonical layer by
`python -m scripts.audit_canonical_revenue_truth`, which reports the same
two-ledger reconciliation, duplicate-row and stage-leakage evidence and exits
non-zero on violation. The original queries remain valid for the legacy tables.

Doctrine: `docs/35_CANONICAL_REVENUE_LEDGER.md`.
