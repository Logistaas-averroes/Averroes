# 33 — Canonical CRM Funnel Doctrine

> PR-ADS-153B — HubSpot Lifecycle Stage is the Averroes funnel.

---

## 1. The rule

**HubSpot owns CRM lifecycle truth.** Averroes may ingest it, persist it,
deduplicate it, attribute it, window it, reconcile it and report it.

Averroes must **never** invent a parallel lifecycle.

The canonical funnel is:

```
Lead → Marketing Qualified Lead → Sales Qualified Lead → Opportunity → Customer
```

---

## 2. Canonical definitions

Every funnel metric is a **stage-entry EVENT**, proven by a HubSpot timestamp.

| Metric | Definition | Canonical event date | Durable column |
|---|---|---|---|
| **Lead** | contact entered `lead` | `hs_v2_date_entered_lead` | `date_entered_lead` |
| **MQL** | contact entered `marketingqualifiedlead` | `hs_v2_date_entered_marketingqualifiedlead` | `date_entered_mql` |
| **SQL** | contact entered `salesqualifiedlead` | `hs_v2_date_entered_salesqualifiedlead` | `date_entered_sql` |
| **Opportunity** | contact entered `opportunity` | `hs_v2_date_entered_opportunity` | `date_entered_opportunity` |
| **Lifecycle Customer** | contact entered `customer` | `hs_v2_date_entered_customer` | `date_entered_customer` |

Canonical source: `hubspot_lifecycle` · Table: `hubspot_contact_funnel` ·
Dedup key: the durable HubSpot **contact id** · Rule version: `v1`
(`analysis/crm_lifecycle.py`).

### 2.1 What changed from the legacy doctrine

| | Legacy (pre-PR-ADS-153B) | Canonical |
|---|---|---|
| SQL definition | `status_category = 'qualified'` ⟸ `mql_status ∈ {CLOSED - Sales Qualified, CLOSED - Deal Created}` | entered lifecycle stage `salesqualifiedlead` |
| SQL event date | **contact creation date** | Sales-Qualified **entry** timestamp |
| MQL | not modelled | lifecycle entry event |
| Opportunity | not modelled | lifecycle entry event |
| Customer | closed-won deal row | lifecycle entry event (revenue customer is separate — §5) |
| Population | paid-search contacts only | **all sources** |

A contact created in January and qualified in August is a **January Lead** and an
**August SQL**. This is required behaviour, not a rounding difference.

### 2.2 Absence is never a date

A missing `hs_v2_date_entered_*` is a **coverage gap**. The contact is not counted
in any bounded window and the gap is reported. `createdate` is **never**
substituted for a missing funnel event date.

### 2.3 Counts are not mutually exclusive

A contact currently at `customer` **still** entered `salesqualifiedlead` on some
date and remains in that historical SQL cohort. Funnel populations are never made
mutually exclusive by current lifecycle stage.

---

## 3. `mql_status` — preserved, but demoted

`mql_status` is an **operational workflow dimension inside the MQL process**. It
is *not* the definition of any funnel stage.

The single mapping lives in `analysis/mql_status_taxonomy.py`. It is imported
everywhere; it is never duplicated in writers, analysis, UI or reports.

| Raw HubSpot value | Operational category |
|---|---|
| `Open`, `OPEN - Connecting`, `OPEN - Pending Meeting`, `OPEN - Meeting Booked` | `open_working` |
| `CLOSED - Sales Qualified` | `sales_qualified_signal` |
| `CLOSED - Deal Created` | `deal_created_signal` |
| `CLOSED - Sales Disqualified` | `disqualified` |
| `CLOSED - Bad Product Fit` | `bad_fit` |
| `CLOSED - Job Seeker`, `CLOSED - Bad Contact` | `contact_quality` |
| `Closed`, `CLOSED - No Response` | `no_response` |
| `DICARDED` *(one R — the real HubSpot internal value; label is DISCARDED)* | `discarded` |
| `RESELLER` | `reseller` |
| `Other` | `unmapped` *(carries no operational meaning — must surface in the audit)* |

### 3.1 Two absences, never merged

| Category | Meaning |
|---|---|
| `no_verdict` | the property is null/blank — the MDR has recorded nothing yet |
| `unmapped` | a **non-null** value Averroes does not know — a NEW production value that must surface as an audit warning |

A new HubSpot value appearing in production is never silently classified as
normal.

### 3.2 Free text can no longer reach the property

The legacy `mql_status = mql_status OR mql___mdr_comments` fallback is **removed**
from the canonical path: `mql___mdr_comments` is not even fetched
(`CONTACT_FUNNEL_PROPERTIES`). Historical polluted rows are **detected and
counted**, never rewritten or deleted.

---

## 4. Named scopes

The PR-ADS-152 scope algebra applies to **every** funnel event:

```
keyword_attributable ≤ campaign_attributable ≤ google_ads_source ≤ all_source
```

Attribution creates **subsets**; it never redefines the underlying event.

`all_source` is genuinely all-source: the canonical store ingests every HubSpot
contact, not only paid-search ones. (The legacy `SCOPE_ALL_SOURCE` read a
paid-search-only table and so could never exceed the Google Ads scope.)

`keyword_attributable` here means a campaign-attributable contact that also
carries a HubSpot keyword label. It is **not** a Google Ads criterion-level join —
no such join exists (see the PR-ADS-153A audit).

### 4.1 When campaign identity cannot be consulted

The two narrow scopes depend on the Google Ads campaign-identity contract. If it
cannot be consulted, they are **unavailable**, never zero:

| Scope | Behaviour |
|---|---|
| `all_source` | fully available — a CRM funnel count does not depend on ad attribution |
| `google_ads_source` | fully available — decided by the contact's own HubSpot source |
| `campaign_attributable` | **`null`** (unavailable) |
| `keyword_attributable` | **`null`** (unavailable) |
| reconciliation status | `partial`, reason `campaign_identity_unavailable` — never `reconciled` |

Rendering these as `0` would state "no campaign-attributable contacts" when the
truth is "we could not check". A **successfully consulted** contract that maps
nothing is a different thing entirely, and `campaign_attributable: 0` is then a
real, provable zero.

---

## 5. Lifecycle Customer ≠ Revenue Customer

Two distinct concepts, always named explicitly:

| Concept | Definition | Owner |
|---|---|---|
| **Lifecycle Customer** | contact entered lifecycle stage `customer` | this PR |
| **Revenue Customer** | canonical closed-won deal truth | **PR-ADS-153E** |

The same applies to Opportunity: the CRM funnel `lifecycle_opportunity` (a
contact stage transition) is distinct from a HubSpot **Deal** existing.
PR-ADS-153E unifies deal/revenue truth.

---

## 6. Cohort-safe conversions

A conversion rate is only ever published on a **cohort** basis:

- **Denominator** — contacts that entered stage *X* **inside** the window.
- **Numerator** — the subset of *that same cohort* which also entered stage *Y*,
  whenever that happened.

Dividing two independent event-period totals compares different cohorts and is
never done. An empty cohort yields `rate_pct: null` with basis `unavailable` — a
funnel rate is never fabricated.

---

## 7. Ingestion

One service owns the canonical contact store:
`services/hubspot_contact_funnel_sync_service.py`.

| Property | Behaviour |
|---|---|
| Watermark | `lastmodifieddate` — **never** contact-creation recency. A contact created two years ago and qualified today is refreshed today. |
| Scope | all sources; no `hs_analytics_source` filter |
| Resumability | the watermark is persisted after **every page**; completion state never lives in process memory |
| Bootstrap | same code path as incremental, differing only in the starting watermark. Status is explicit (`not_started` / `running` / `partial` / `complete` / `failed`) |
| Idempotency | upsert on `contact_id`, guarded by `last_modified_at` so a replayed older read can never resurrect a superseded stage |
| Failure | a partial read is recorded as a failed batch with the watermark left at the last **proven** checkpoint — never reported as complete |

Completeness is never claimed merely because recent rows exist.

### 7.1 Bootstrap resumes — it does not rescan

An interrupted historical bootstrap resumes from its **durable watermark**, using
the same safe overlap incremental sync uses. It does **not** restart from the
epoch, which would make a large backlog impossible to finish:

| State | Starting point |
|---|---|
| No durable watermark (first-ever bootstrap) | epoch |
| Durable watermark present | watermark − overlap |
| `restart_from_epoch=true` | epoch — operator-only escape hatch for a deliberate full rebuild, never normal behaviour |

An unfinished bootstrap keeps bootstrapping (`get_bootstrap_mode`) until it
completes; only then does the scheduler switch to incremental.

### 7.2 Fail closed at every durable boundary

A canonical sync never reports success for writes it cannot prove:

1. **Before calling HubSpot** — if the sync batches or the bootstrap-running
   state cannot be persisted, the run refuses to read at all. A failure that
   leaves no record is worse than no run.
2. **Per page** — normalise → persist → **verify the write was proven** → only
   then advance the durable watermark. A failed persist raises; the watermark
   stays where it was, so the next run re-reads that page.
3. **Per checkpoint** — `update_contact_funnel_sync_state` returns a boolean. If
   the checkpoint cannot be written the run stops rather than reading pages whose
   progress can never be recorded.
4. **At finalisation** — the final durable state write must succeed before the
   run may report success or mark a bootstrap complete.

**`persisted < attempted` is NOT a failure.** The writer returns
`{ok, attempted, persisted, error}` precisely so the caller can tell an
idempotent stale-row no-op (the latest-state guard doing its job) apart from a
persistence failure.

### 7.3 Completion is proven, never assumed

The iterator signals the true end of the HubSpot result set with an **explicit
empty sentinel page** (`{"complete": true}`). A bootstrap may be marked complete
only when that sentinel was observed.

Running out of pages proves nothing:

| Condition | Outcome |
|---|---|
| Sentinel observed | `complete` |
| `max_pages` cap reached | `partial` (truncated) |
| Stall at HubSpot's 10,000-result boundary | `HubSpotSearchStalledError` → run **failed**, bootstrap stays `partial` |

The stall case is when more than one full page shares an identical
`lastmodifieddate` at the paging cap, so re-anchoring cannot advance and an
unknown number of contacts is unreachable. Returning normally there would let a
bootstrap be marked complete on an incomplete scan.

---

## 8. Freshness

| Dataset | Table | Date column | Meaning |
|---|---|---|---|
| `hubspot/contact_funnel` | `hubspot_contact_funnel` | `last_modified_at` | contact spine ingestion recency |
| `hubspot/lifecycle_events` | `hubspot_contact_funnel` | `latest_stage_entry_at` | newest lifecycle transition held |

The writer keys and the `DATASET_FRESHNESS_CONFIG` keys are asserted equal by a
test, so the `google_ads` vs `google_ads_api` mismatch class (which left canonical
spend with no freshness signal — PR-ADS-153A finding P1-1) cannot recur.

---

## 9. Legacy `status_category` — compatibility only

`status_category` is **not deleted** in this PR. Too many surfaces still depend on
it, and PR-ADS-153C migrates them.

Until then it is explicitly **compatibility-only** and must not be treated as
canonical funnel truth. `GET /api/crm-funnel/audit` reconciles the two doctrines
contact by contact.

---

## 10. Reconciliation and the before/after contract

Because SQL truth materially changes, the audit splits every delta into its two
independent causes:

| Cause | Meaning |
|---|---|
| **Date shift** | the contact is an SQL under *both* doctrines, but the event date moved from acquisition to qualification, so it lands in a different window |
| **Population** | the contact qualifies under one doctrine only (e.g. legacy-qualified but HubSpot never marked it Sales Qualified) |

Mismatch classes reported per contact (`services/crm_funnel_reconciliation_service.py`):
lifecycle SQL vs legacy not qualified · legacy qualified never entered SQL ·
lifecycle Opportunity vs legacy in-progress · lifecycle Customer with no customer
date · `CLOSED - Deal Created` without Opportunity · `CLOSED - Sales Qualified`
without SQL · unmapped status · no verdict · legacy row without HubSpot identity ·
free-text pollution.

No email address is ever returned — contact id and company only.

---

## 11. Windows

No new window vocabulary. The canonical funnel accepts the **existing** shared
resolvers (`resolve_window_contract`):

- **Business** — Current Quarter, Last Quarter, Last 6 Months, YTD, All Time.
- **Evidence** — retained for Platform Evidence surfaces.

Window semantics are applied to the relevant **event date** for the requested
event. There is no separate rolling-Postgres implementation of a visible label.

---

## 12. Governance

Read-only relative to external systems. This PR adds no HubSpot contact/lifecycle
/deal writes, no Google Ads mutation, no offline-conversion upload, no Mailchimp
work, and deletes no historical evidence.

---

## 13. Consuming surfaces (PR-ADS-153C)

| Surface | What it reads |
|---|---|
| **Leads page** | `GET /api/crm-funnel` (funnel strip + definitions), `GET /api/crm-funnel/contacts` (server-side paginated rows), `GET /api/crm-funnel/operational-status` (working statuses). Default scope **All Sources** — it answers a CRM question, not an attribution one. |
| **Dashboard funnel** | Canonical lifecycle Leads / MQLs / SQLs via `kpis.lifecycle_*`, with cohort-safe conversions. Customers and Closed-Won Revenue deliberately REMAIN on the revenue contract until PR-ADS-153E. |

The Leads page never renders a narrower scope under a generic label, never
divides unrelated period totals, and renders `—` for anything unavailable.

### 13.1 One selector, one population

A visible filter must move every number it sits beside. `acquisition_group` is
therefore part of the **aggregate** contract, not only the row contract: the
funnel strip, the cohort-safe conversions, the coverage block, the contact rows
and the working-status breakdown all receive the same value. "Source = Organic"
above an all-source funnel headline is a lie about which population is being
reported, and is prevented by contract rather than by convention.

The same rule governs the controls themselves: a filter the active view cannot
honour is hidden, not shown and ignored. The Disqualified / Other view *is* the
working-status breakdown, so the working-status and company filters are removed
there while window, scope and source — which do filter it — remain.

### 13.2 Source classification uses the FULL contract

Acquisition group comes from `analysis/source_classification.py`, the same module
Revenue by Source and the durable `contact_source_classification` writer use, and
with the same evidence pair:

    primary = hs_analytics_source          (HubSpot Original Source)
    detail  = hs_analytics_source_data_1   (HubSpot Original Source Drill-Down)

Passing the detail is mandatory. `Offline Sources` is ambiguous on its own — only
the drill-down routes SalesNash / Events to Other Paid, and reseller / referral /
direct email to Organic. Dropping it made the Leads page classify the same
contact differently from Revenue by Source. Because classification reads both
fields, the server-side allow-list is over `(source, drill-down)` **pairs**;
collapsing it to the primary would re-lose the same evidence.

Unlike the legacy `leads` table — which stores `campaign_name`, not the
drill-down, which is why PR-ADS-152 had to pass `None` — the canonical contact
store persists the real HubSpot field, so the full contract is available. Campaign
name is still never used to infer a source.

### 13.3 Campaign and keyword are Google-Ads-only claims

`hs_analytics_source_data_1` / `_2` do **not** universally mean "Google Ads
campaign / keyword". They carry those semantics only for Paid Search contacts;
for Organic, Paid Social, Email Marketing, Referral, Offline and Event contacts
they hold entirely different drill-down text.

A campaign or keyword label is therefore published only when all three hold: the
contact's acquisition group is Google Ads, the Google Ads campaign-identity
contract was consulted, and the label resolved to a real campaign identity.
Otherwise both are `null` and the row states which condition failed
(`not_google_ads_source`, `campaign_identity_unavailable`,
`campaign_identity_unresolved`).

Non-Google contacts are not left blank: they carry the canonical source taxonomy
— acquisition group, channel, platform, attribution quality — plus the neutral
raw drill-down as *Source detail*. The evidence is preserved; only the false
label is refused.

---

## Related

- `docs/audits/PR-ADS-153A-MINIMUM-VIABLE-TRUTH-AUDIT.md` — the audit that
  established these findings
- `docs/24_UI_NAVIGATION_MODEL.md` — navigation (PR-ADS-153C/D)
- PR-ADS-153C — Leads consolidation + Lead Intelligence retirement
- PR-ADS-153E — Customer / Revenue canonical reconciliation
