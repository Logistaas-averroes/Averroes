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

A **failed lookup** is categorically distinct from a successful lookup that found
nothing. `lookup_failed` → `unavailable`, never `unclassified` (a conclusion we
did not reach), and the write path leaves the previous successful associations
completely untouched. Losing attribution because an API call timed out would
silently move revenue between sources.

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

All nine pipeline stages are synced: Proposal, In Trials, Pricing Acceptance,
Invoice Agreement Sent, Unresponsive, **Won**, Lost, **Downgrade**, **Churn**.
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

The sync watermark advances **only** on a fully successful run, so a partial run
re-reads its range rather than skipping deals it never saw.

---

## 9. Synchronization

| Concern | Contract |
|---|---|
| Incremental driver | `hs_lastmodifieddate` with a 15-minute overlap — never creation recency. A deal created two years ago and closed today must be re-read today. |
| Ordering | ascending by last-modified, so a retried page cannot skip records |
| Replay safety | monotonic: an older observation cannot overwrite newer state |
| Backfill | `backfill_deals()` — resumable, idempotent, all stages |
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
python -m scripts.audit_canonical_revenue_truth --window current_quarter
```

Exits non-zero on: duplicated `deal_id`; a row counted as won without
`hs_is_closed_won = true`; an unknown currency contributing to USD revenue; a
failed lookup represented as unclassified; ledger rows disagreeing with the
ledger summary; a required sync range incomplete; currency completeness falsely
reported; a deal present in a legacy ledger but missing from canonical.

Also exposed read-only at `GET /api/audit/revenue-truth` (admin).

Every legacy-versus-canonical difference is itemized **by deal id with a
reason**. "Totals differ" is not an acceptable output — the cutover must be able
to explain each deal that moves.

Expected differences (not failures):

* `non_gclid_deal_excluded_by_legacy_ledger` — the defect this PR exists to fix;
* `canonical_currency_*` — canonical withholds where legacy assumed USD.

Never expected: `legacy_only`. That means the canonical sync missed a deal.

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
