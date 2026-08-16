# 35 — Canonical Revenue Ledger (PR-ADS-153E-A)

One deal ledger. One won predicate. One currency doctrine. One association rule.

Status: **shadow mode.** The ledger is populated and reconciled; no production
page reads it. Consumer migration is PR-ADS-153E-B.

---

## 1. Why this exists

PR-ADS-153A §9.2 found **three incompatible revenue lineages**:

| Chain | Keyed by | Feeds | Fatal flaw |
|---|---|---|---|
| `gclid_attribution` | SHA1 attribution hash | Dashboard, Deals, ROAS, Mart | Drops every deal without a GCLID; a relabel mints a new row |
| `deal_source_attribution` | `deal_id` | Revenue by Source, Channels | No lifecycle, no currency contract |
| Local JSON / Windsor | nothing | Unit Economics, deprecated routes | No dedup, no currency, no lineage |

Two pages could therefore report different customer and revenue totals for the
same window **by construction**. Revenue was also labelled USD with no
verification at all: HubSpot's raw `amount` was written straight into a column
called `deal_amount_usd` (§9.3).

---

## 2. Deal identity

    canonical revenue identity := deal_id

`deal_id` is HubSpot's own durable identity and the ledger's PRIMARY KEY. It is
not a hash, not a contact, not a GCLID.

This is what makes reprocessing safe. A changed campaign label, source
classification or GCLID updates the same row. The legacy ledger's
`attribution_key = SHA1(gclid|contact|deal|campaign|keyword|match)` minted a new
row on any relabel, so one deal accumulated several rows and its history could
silently migrate between campaigns.

### Revenue Customer

**One distinct HubSpot deal whose latest authoritative `hs_is_closed_won` is
true.** The grain is `deal_id` — not contact, not GCLID.

A **lifecycle customer** is a different concept owned by
`canonical_contact_outcome_service` (a contact reaching the `customer` lifecycle
stage). The two are never substituted for one another: a two-deal customer counts
twice as revenue customers and once as a lifecycle customer, and both are
correct for their own question.

---

## 3. The won predicate

    won := hs_is_closed_won IS TRUE

Nothing else. Defined once in `analysis/deal_truth.is_won`.

Stage labels are **display evidence**. They never decide whether a deal is won.

**Fails closed.** A missing `hs_is_closed_won` is stored as NULL and counted
separately as `unknown_won_deals` — absence of proof that a deal is won is not
proof that it is.

Forbidden, and asserted by test:

| Forbidden | Why it was dangerous |
|---|---|
| hardcoded stage id as the predicate | ties revenue truth to one portal's configuration |
| `deal_stage_label ILIKE '%won%'` | any label containing "won" became revenue |
| unknown stage → "Deal Won / Payment Received" | combined with the ILIKE rule, an unrecognised stage silently became revenue |
| missing boolean → true | asserts revenue we cannot prove |

The connector's unknown-stage default is removed: an unknown stage id is now
labelled `Unknown stage (<id>)` and counted in the audit.

---

## 4. Currency doctrine — fail closed

Six fields are persisted separately so a USD claim is always provable:

`amount_raw` · `deal_currency_code` · `amount_in_home_currency` ·
`home_currency_code` · `revenue_usd` · `currency_status` + `currency_reason`

`revenue_usd` is populated **only** when the currency is proven:

| Status | Meaning | Summable |
|---|---|---|
| `verified_usd` | the deal's own currency is USD, or the VERIFIED portal home currency is USD | ✅ |
| `converted` | non-USD, converted at the local FX rate for the deal's **close date** | ✅ |
| `unavailable` | see reasons below | ❌ |

`unavailable` reasons: `no_amount` · `unknown_currency` ·
`home_currency_unverified` · `no_close_date_for_fx` · `no_fx_rate_for_close_date`.

Rules:

* `amount_in_home_currency` is read as USD **only** after HubSpot positively
  confirms the portal home currency is USD. Without that confirmation it is an
  amount in an unknown currency.
* Conversion uses the existing local `fx_rates` contract — the same fail-closed
  posture spend already uses. **Services never fetch FX externally.**
* An amount is converted **only** at its own currency's rate. The resolver
  receives the whole `{currency: {date: rate}}` table and selects the amount
  source and its rate map together, so a missing GBP rate can never be filled in
  with the home currency's EUR rate. Missing rate → `unavailable`.
* An unknown currency **never becomes zero**. Zero is a claim the deal was worth
  nothing; NULL is the truth that we do not know.
* Currencies are never mixed inside one total. `amount_raw_total` is reported
  separately and explicitly labelled *not* a USD figure.

---

## 5. Association rule — one resolver

`analysis/deal_truth.resolve_deal_associations` is the ONLY deal→contact rule,
and it feeds GCLID, source, campaign and country attribution alike. Previously
ledger A took `results[0]` (the arbitrary first association) while ledger B took
all contacts and could return ambiguous — so the same deal bucketed differently
on two pages.

| Case | Primary | Status | Attribution |
|---|---|---|---|
| one contact | that contact | `resolved` | `attributed` |
| several, identical evidence | lowest stable contact id, **display identity only** | `resolved` | `attributed` |
| several, conflicting evidence | **none** | `ambiguous` | `ambiguous` |
| lookup FAILED | none | `lookup_failed` | `unavailable` |

Conflict is checked across `gclid`, `campaign_name_raw`, `country_raw` and
`acquisition_group`. A disagreement in **any** of them makes the deal ambiguous —
campaign and country drive different pages, so resolving one while ignoring
another is how two surfaces start disagreeing again.

Deterministic: input order cannot change the result.

An **ambiguous** deal stays in the ledger and contributes NO attribution evidence
to its row, while the bridge retains every candidate so the conflict is
explainable.

### Contact evidence is read by a dedicated connector function

`pull_contact_attribution_properties` returns
`{contact_id: {"id": …, "properties": {…}}}` and reads only the seven
attribution properties — no names, no email addresses. It is deliberately
separate from `pull_contacts_by_ids`, the PR-ADS-115 lead-date reader that
returns `{contact_id: createdate}`; the two contracts are not interchangeable.

A contact HubSpot does not return is **missing evidence**, not a contact with no
source: an incomplete batch is treated as a lookup failure.

### A failed lookup

Categorically distinct from a successful lookup that found nothing.
`lookup_failed` → `unavailable`, never `unclassified` (a conclusion we did not
reach). Losing attribution because an API call timed out would silently move
revenue between sources, so on an EXISTING row the write path preserves every
association-derived field:

`primary_contact_id` · `association_count` · `association_status` ·
`association_reason` · `gclid` · `campaign_name_raw` · `keyword_raw` ·
`country_raw` · `source_primary_raw` · `source_detail_raw` ·
`acquisition_group` · `attribution_status` · `attribution_reason`

Deal facts read successfully in the same run — stage, amount, currency,
last-modified — still update. The bridge is untouched. The failed attempt is
counted in `hubspot_deal_sync_state.association_failures` and reported by the
audit, so preservation never hides it.

A deal with **no previous row** has nothing to preserve, so `lookup_failed` /
`unavailable` is stored.

---

## 6. Attribution scopes

Attribution is **nullable evidence**. A deal belongs in the ledger whether or not
it has a GCLID, a campaign mapping, an associated contact, a country or a
classified source.

    all-source revenue      ⊇  Google Ads-source
                            ⊇  campaign-attributable
                            ⊇  GCLID-attributable

The legacy GCLID ledger held only the innermost ring and fed it to the Dashboard
as if it were the whole. `won_without_gclid` in the audit is exactly the revenue
that was invisible.

---

## 7. Stage handling

Every deal is synced, in every stage. `DEAL_STAGE_MAP` labels the nine known
stages — Proposal, In Trials, Pricing Acceptance, Invoice Agreement Sent,
Unresponsive, **Won**, Lost, **Downgrade**, **Churn** — but it is a **display
vocabulary, not a population filter**. Ingestion applies no stage filter at all;
a stage id the map does not know is stored and labelled `Unknown stage (<id>)`.
Gating the read on the map would make the ledger silently incomplete the moment
someone adds a pipeline stage in HubSpot.

Previously only Won was fetched, so open pipeline was invisible and churn could
never reverse a customer.

For this PR:

* current closed-won totals follow `hs_is_closed_won`;
* churn / downgrade / lost / open are stored and surfaced in audit diagnostics;
* **no negative revenue is invented** and no ACV is subtracted — there is no
  authoritative CRM reversal/refund field to justify it;
* historical stage evidence is never erased.

---

## 8. Failure states

| State | Meaning | Never |
|---|---|---|
| `currency_status = unavailable` | value unprovable | zeroed |
| `association_status = lookup_failed` | we learned nothing | recorded as unclassified, or allowed to erase prior evidence |
| `hs_is_closed_won IS NULL` | HubSpot did not say | treated as won |
| sync `partial` / `failed` | incomplete read | reported as a successful zero-row result |
| a ledger write that FAILED | nothing was persisted | counted as "zero rows written" and passed over |

**Persistence fails closed.** `upsert_deal` and `record_sync_state` return
`available: False` on a database error, the sync inspects every result, and a
run with any write failure reports `partial` — or `failed`, when nothing
persisted at all. Coverage that could not be recorded is coverage the run does
not get to claim. The scheduler marks the batch failed accordingly.

### Watermark

Advances on a fully successful run, or to a **clean prefix checkpoint**. Deals
are read ascending by `hs_lastmodifieddate`, so the last deal that was both
fully resolved and committed is a safe resume point; the prefix closes at the
first association failure or write failure. A failed run never advances.

That checkpoint is what makes `backfill_deals()` finishable: the association-
lookup cap ends a pass at its last committed deal and the next pass resumes
there, instead of paging through the same first 5,000 deals forever. Deals a
capped run never reached are not written at all — inventing `lookup_failed` rows
for them would manufacture evidence of a failure that never happened.

### Sync mode — declared, never inferred (PR-ADS-153E-A2)

Every `record_sync_state` call names its mode. The two write different columns:

| Column | `sync_mode="bootstrap"` | `sync_mode="incremental"` |
|---|---|---|
| `bootstrap_status` | `in_progress` → `complete` | untouched |
| `bootstrap_started_at` | `COALESCE(existing, NOW())` | untouched |
| `bootstrap_completed_at` | set once, only when proven | untouched |
| `last_incremental_at` | **untouched** | `NOW()` |
| `last_status` / `last_error` / counters | honest | honest |
| watermark | existing checkpoint rules | existing checkpoint rules |

`bootstrap_status` becomes `complete` only when the run **succeeded** *and*
`proved_complete` — the connector reached the end of the result set and the run
was not truncated by the lookup cap. "We stopped and nothing went wrong" is not
proof that there was nothing left to read.

Two monotonic guarantees:

* a completed bootstrap is never downgraded — coverage once proven stays proven;
* `bootstrap_completed_at` keeps its **first** value. Re-running the backfill
  re-proves the same coverage; restamping it would push completion past the
  incremental that already ran and silently revoke a passing gate until the next
  daily sync.

153E-A inferred the mode from `full_refresh and complete`, so an ordinary
incremental left `bootstrap_status = not_started` while reporting
`last_status = success`, and stamped `last_incremental_at` on bootstraps too —
which made "an incremental succeeded after the bootstrap" unprovable.

---

## 9. Synchronization

| Concern | Contract |
|---|---|
| Incremental driver | `hs_lastmodifieddate` with a 15-minute overlap — never creation recency. A deal created two years ago and closed today must be re-read today. |
| Ordering | ascending by last-modified, so a retried page cannot skip records |
| Replay safety | monotonic: an older observation cannot overwrite newer state, and an UNKNOWN incoming timestamp cannot overwrite a known one |
| Atomicity | the association bridge is replaced only when the ledger update was actually applied — a stale replay changes neither |
| Backfill | `backfill_deals()` — resumable via the checkpoint, idempotent, every stage |
| Scheduler role | orchestration only; **no** revenue, currency, won-state or attribution logic |
| Flag of failure | a failed sync is recorded `failed`, added to `errors`, and its batch marked failed |

Production write path: `scheduler/incremental_sync._sync_deal_ledger` →
`services.hubspot_deal_sync_service.sync_deals`.

---

## 10. Shadow-mode rollout

**This PR builds and proves the ledger. It switches nothing.**

`gclid_attribution`, `deal_source_attribution` and the legacy `deals` snapshot
are intact and still feed every production page. No page's visible totals change.

The gate before PR-ADS-153E-B may begin:

```
python -m scripts.audit_canonical_revenue_truth --all-windows --json
```

`--all-windows` runs `current_quarter`, `last_quarter`, `last_6_months`, `ytd`
and `all_time`. **One failing or unavailable window fails the whole command** —
a green `current_quarter` is not permission to migrate a page that renders
"all time". `--window` still runs exactly one; the two are mutually exclusive.

### Coverage is part of the gate (PR-ADS-153E-A2)

Reconciling what the ledger **holds** says nothing about what it is
**missing**. A portal whose historical bootstrap had never run — one nightly
incremental over the last 24 hours, reporting `success` — reconciled perfectly
against the same 24 hours of legacy rows and returned `ok: true`. That was the
signal 153E-B was going to read as permission to repoint the executive revenue
and customer totals at a ledger holding one day of history.

`ok: true` therefore also requires:

| Requirement | Violation code |
|---|---|
| the sync-state read succeeded | `sync_state_unavailable` |
| a sync-state row exists | `sync_state_missing` |
| `bootstrap_status = complete` | `bootstrap_not_complete` |
| both bootstrap timestamps exist | `bootstrap_timestamp_missing` |
| completion is not before start | `bootstrap_timestamp_invalid` |
| an incremental ran **after** completion | `post_bootstrap_incremental_missing` |
| the last sync succeeded | `last_sync_not_successful` |
| success was not recorded with an error | `last_sync_reported_success_with_error` |
| stage coverage is readable | `stage_breakdown_unavailable` |

Every violation carries a stable `code` beside its human message, exposed as
`violation_codes` and `violation_details`, so a runbook keys off the reason
rather than parsing English. An unreadable stage breakdown reports NULL, never
`[]` — "could not read" is not "there is nothing there".

### Operator bootstrap command

```
python -m scripts.backfill_canonical_deal_ledger              # resume (default)
python -m scripts.backfill_canonical_deal_ledger --json --max-passes 40
python -m scripts.backfill_canonical_deal_ledger --restart    # deliberate
```

Drives the bounded, resumable bootstrap to a **proven** completion. Exit 0
requires both a pass reporting `status=success` and `complete=true`, *and* a
re-read of the durable state showing `bootstrap_status = complete` — the first
is the process's opinion, the second is what the gate will read tomorrow.

Stops immediately on any pull, association, persistence or state-recording
failure; bounded by `--max-passes`, so it can never loop indefinitely. It never
escalates to `--restart` on its own: re-reading an entire portal is an
operator's decision, not a program's reaction to a bad night. Counts, statuses
and timestamps only — no deal names, contact names, emails or full GCLIDs,
because this output goes into operator logs and PR evidence.

There is no HTTP endpoint for it, and nothing triggers it on application
startup.

Exits non-zero on: duplicated `deal_id`; a row counted as won without
`hs_is_closed_won = true`; an unknown currency contributing to USD revenue; a
failed lookup represented as unclassified; ledger rows disagreeing with the
ledger summary; a required sync range incomplete; currency completeness falsely
reported; a deal present in a legacy ledger but missing from canonical.

Also exposed read-only at `GET /api/audit/revenue-truth` (admin).

Every legacy-versus-canonical difference is itemized **by deal id with a
reason**, and carries `expected`. "Totals differ" is not an acceptable output —
the cutover must be able to explain each deal that moves, so **the gate fails on
every difference that is not expected.**

Five categories, split by what an operator would actually have to do about them:

| Category | Meaning | Reason | Expected |
|---|---|---|---|
| `canonical_only` | canonical won, legacy has no row | `non_gclid_deal_excluded_by_legacy_ledger` | ✅ the defect being fixed |
| | | `gclid_won_deal_missing_from_legacy_ledger` | ❌ the GCLID ledger *can* hold it and does not |
| | | `canonical_won_deal_missing_from_legacy_ledger` | ❌ `deal_source_attribution` is deal-keyed and has no excuse |
| `legacy_only` | canonical has **no row at all**, in any state | `missing_from_canonical_ledger` | ❌ the sync missed a deal |
| `won_disagreement` | canonical **holds** the deal, the ledgers classify it differently | `legacy_predicate_counted_non_won_deal` | ✅ the legacy `ILIKE '%won%'` false positive |
| | | `canonical_won_state_unknown` | ❌ |
| | | `canonical_close_date_outside_window` | ❌ |
| `amount_disagreement` | same deal, different money | `canonical_currency_*`, `currency_resolution_differs:*` | ✅ the currency doctrine |
| | | `both_ledgers_claim_a_proven_usd_amount` | ❌ |
| | legacy holds the deal with **no** amount | `legacy_amount_unavailable` | ❌ |
| `duplicate_legacy_rows` | one deal, several `gclid_attribution` rows | — | the legacy SHA1-key defect, reported |

The two directions of a missing amount are deliberately different findings.
Canonical NULL against a legacy figure is explained by the currency doctrine —
canonical withholding what legacy asserted without proof. Canonical **proven**
against a legacy NULL is not explained by anything: the cutover is about to
publish a figure the outgoing ledger never carried, and it must be named.

### An unreadable legacy lineage fails outright

If `gclid_attribution` or `deal_source_attribution` cannot be read, the gate
fails before any comparison is attempted, `legacy_deal_count` stays NULL rather
than 0, and no difference is itemized at all — comparing against a ledger we
could not read would report every canonical deal as absent from it, which is
pure artefact.

Unavailable is never an empty ledger. Against an empty canonical won population
the two are indistinguishable — zero deals, zero differences, apparently
reconciled — so a broken read would otherwise wave the cutover through on a
reconciliation that never happened.

Separating `legacy_only` from `won_disagreement` is why the gate reads canonical
identity across **all** deal states (`fetch_deal_states`), unbounded by window
and won-state. "The sync missed this deal" and "the two ledgers classify this
deal differently" have completely different remediations; collapsing them left
the gate unable to say which had happened.

The duplicate-row scan is bounded by the same window as the rest of the
reconciliation — an unbounded `GROUP BY` over the whole legacy table both scans
without limit and reports deals outside the window being reconciled.

---

## 11. Consumer cutover (PR-ADS-153E-B)

| Consumer | Current source | Target |
|---|---|---|
| Dashboard Overview / Revenue / Campaigns / Countries / Deals | `gclid_attribution` via Mart | canonical ledger |
| Dashboard Channels, Revenue by Source | `deal_source_attribution` | canonical ledger |
| Deals page | `gclid_attribution` | canonical ledger |
| ROAS by Campaign / Country | `gclid_attribution` | canonical ledger |
| Unit Economics | local JSON / Windsor | canonical ledger |
| Revenue Decision Mart | `gclid_attribution` | canonical ledger |

Every field those pages need already exists on the ledger, so the cutover needs
no schema change. 153E-B additionally: makes Dashboard and Revenue-by-Source
customer/revenue totals reconcile; turns `gclid_attribution` into an attribution
**view** rather than a population; quarantines mismatches instead of rendering
normal values; and deprecates legacy revenue routes without dropping historical
evidence.

**153E-B remains blocked** until the production evidence below passes. The gate
is mechanical, not a judgement call:

1. deploy the merge commit;
2. `python -m scripts.backfill_canonical_deal_ledger` until it exits `0`
   (re-run to resume; it is bounded and resumable);
3. run one normal daily incremental sync **after** the bootstrap completes;
4. `python -m scripts.audit_canonical_revenue_truth --all-windows --json`;
5. require aggregate `ok: true` and exit code `0`;
6. confirm the admin `GET /api/audit/revenue-truth` agrees for every window.

Record only non-sensitive proof: deployed commit SHA, timestamps, bootstrap
status, last incremental status, each window's pass/fail, aggregate counts and
exit codes. Deal-level production identifiers do not go in a public PR.

Expect the first production run to exit `1`. The gate is strict by design, and
genuine backlog in the legacy ledgers will surface as itemized differences to
triage rather than a clean pass.

---

## 12. Tables

| Table | Grain | Writer | Status | 153G |
|---|---|---|---|---|
| `hubspot_deal_ledger` | `deal_id` | `db/deal_ledger_repository` | **Canonical** (new) | Keep |
| `hubspot_deal_contact_association` | `(deal_id, contact_id)` | same | Evidence bridge (new) | Keep |
| `hubspot_deal_sync_state` | `scope` | same | Coverage (new) | Keep |
| `gclid_attribution` | SHA1 attribution key | `db/writers.write_gclid_attribution` | Legacy — comparison source | Retire after 153E-B |
| `deal_source_attribution` | `deal_id` | `services/source_attribution_service` | Legacy — comparison source | Retire after 153E-B |
| `deals` | run snapshot | `db/writers.write_deals` | Legacy snapshot | Retire after 153E-B |

**No table is dropped in this PR.**

---

## 13. Governance

Read-only against every external platform. Allowed: HubSpot GET reads, local
PostgreSQL writes. Prohibited and asserted by test: HubSpot mutations, Google Ads
mutations of any kind, negative keywords, bid/budget/campaign/keyword changes,
offline conversion uploads, Mailchimp work.

No contact names or email addresses are stored on the ledger or printed by the
audit; a GCLID is reported only as present/absent, because reconciliation output
goes into CI logs.

Six-month read-only governance (`docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md`)
remains active. Phase 2 / OCT remains blocked.

---

## Related

- `docs/audits/PR-ADS-153A-MINIMUM-VIABLE-TRUTH-AUDIT.md` §9, §20, Q3–Q5
- `docs/33_CANONICAL_CRM_FUNNEL.md` — lifecycle customers (a different grain)
- `docs/34_SEARCH_TERM_WASTE_CONSOLIDATION.md` — the same doctrine applied to search terms

---

## Follow-up — 2026-08-16 (PR-ADS-153E-A2)

**Status: unchanged — shadow mode.** This hardening PR is a safety interlock; it
switches no consumer, changes no visible total, drops no legacy table and adds
no external write path.

What it changed, and why the original gate was not sufficient:

* the 153E-A gate proved the ledger was *self-consistent and reconciled*, but
  never that it was *complete*. A portal with no historical bootstrap passed;
* sync mode is now declared rather than inferred, and bootstrap and incremental
  runs write different columns (§9);
* `bootstrap_started_at` / `bootstrap_completed_at` are populated for the first
  time — the columns existed since 153E-A and were never written;
* the audit requires complete, ordered bootstrap coverage plus a successful
  incremental on top of it, with stable violation codes (§10);
* `scripts/backfill_canonical_deal_ledger.py` drives the bounded bootstrap to a
  proven completion;
* `--all-windows` makes one failing window fail the whole gate.

Read-only against every external platform. Phase 2 / OCT remains blocked.
