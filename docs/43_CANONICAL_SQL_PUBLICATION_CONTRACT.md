# 43 — Canonical SQL Publication Contract

**PR-ADS-161A-1.** One production-facing verdict decides whether a complete SQL
total may be shown. It fails closed, and it is the only thing an executive
surface is allowed to ask.

> **No consumer is migrated in this PR.** This is the structural prerequisite
> PR-ADS-161A's brief sequences first. The executive cutover — Dashboard
> Overview, Revenue by Source, Campaigns, Countries, Revenue, Deals and the
> Revenue Decision Mart — is **PR-ADS-161A-2**, and it consumes this contract.

---

## 1. The defect this closes

`analysis/lifecycle_sql_coverage.py:206 window_coverage()` answers a
**necessary** question: given the undated population and the boundary, *could*
a complete total exist for this window? It sets `cpql_publishable` and
`complete_sql_total` from that answer alone. Its own docstring is explicit:

> The final word belongs to `audit_certification`, which alone can see reader
> reconciliation and source freshness, and which sets this back to False.

That final word lived **only** in `scripts/audit_lifecycle_sql_coverage.py:329`,
coupled to a CLI `Findings` object. Nothing a product surface could import. So
the sole publication flag reachable from production code was the intermediate
one — `True` for a window the audit refuses to certify.

Nothing read it yet, which is why this was a landmine and not an incident.
PR-ADS-161A-2 migrates executive surfaces onto canonical lifecycle truth, and
the first of them to reach for `window_coverage()` would have published a
total the audit withholds. The door is closed before anyone walks through it.

## 2. The contract

`analysis/sql_publication.py::publication_verdict` is the **only** function
permitted to decide a complete SQL total or a CPQL may be shown. It takes every
gate at once:

| | Gate | Source |
|---|---|---|
| window-local | membership resolved, historical **and** prospective | `window_coverage` |
| window-local | window lies at or after the boundary | `_certification` |
| window-local | no open post-boundary gap can belong to it | `incident_membership` |
| window-local | contact-funnel source proven fresh | `sql_coverage_freshness` |
| global | every canonical reader reconciles (44 combinations) | recorded evidence |
| global | boundary store was readable | `crm_funnel_repository` |
| global | post-boundary incident store was readable | `crm_funnel_repository` |

All of them, or `publishable` is `False` and `complete_sql_total` is `None`.

**Every gate fails closed.** `None` — could not be read — withholds exactly as
`False` does. An outage must never certify a window. This is "unknown is not
zero" applied to publication.

Three outcomes stay distinct all the way to the caller:

```
published    a complete, certified total exists
withheld     we looked, and the evidence does not support a total
unavailable  we could not look
```

`complete_sql_total` is `None` in the last two, **never `0`**. A withheld total
is not a measurement of zero.

`confirmed_sql_subset` is always present and always truthfully named: contacts
with a **proven** SQL-entry date in the window. A surface may show it while the
total is withheld, provided it says which one it is showing.

## 3. One implementation, two callers

`scripts/audit_lifecycle_sql_coverage.py::audit_certification` now **delegates**
to `publication_verdict`. It no longer carries its own copy of the decision.

If the two could drift, the audit would stop describing what production
publishes — and the audit is the thing we point at to claim production is
truthful.

### What changed, stated plainly

An earlier draft of this document claimed the refactor changed nothing and
offered the 141 green PR-ADS-160 cases as the evidence. **That was wrong on
both counts**, and the review of this PR caught it.

Two audit outputs change. Neither changes a *refusal* — `certified: False`,
`cpql_publishable: False` and `complete_sql_total: None` are identical in
every case, and `blocked_windows` keeps its shape — but the **reason string**
differs:

| Situation | Before | After |
|---|---|---|
| source not fresh | `not_certifiable_source_not_fresh` | the freshness reason (`source_stale`, `source_last_incremental_failed`, …) |
| contact store unreadable (window dict has no `certification_eligible`) | `None` | `coverage_verdict_absent` |

The first is an improvement — an operator's next step is the pipeline, not the
window — but it is a **change**, not a preserved behaviour. Before this PR the
freshness branch was unreachable from `run()`: a window is only locally
eligible when its freshness says fresh, and `run()` passes ONE freshness object
to both call sites, so the old code always reported the window's reason.

The 141 cases pass, and that is worth having — but they are **not** evidence
that behaviour is identical, because none of them exercises the stale-source
path with a shared freshness object (`_certify()` in the PR-ADS-160 suite
always passes `freshness=FRESH` regardless of how the window was built).
`test_26` and `test_27` in this PR's suite cover both changed paths directly.

### One explicit difference between the two callers

`require_full_scope_coverage` is `True` for production and `False` for the
audit, named at both call sites. Production must not publish a narrow scope on
a reconciliation record in which that scope was never compared; the audit's
`reconciliation_complete` is documented to mean "every combination reached a
proven outcome and every **comparable** one agreed", and a contract-unavailable
pair has never blocked it. Requiring it there would mean the audit could not
certify anything while Google Ads campaign identity is unavailable — a
different decision, and not this PR's to make.

Blocking every scope when any scope is uncomparable is conservative: it
withholds `all_source` too, which *was* comparable. The precise fix is to
record per-scope outcomes and require the requested scope. That belongs with
the consumers that will request them.

## 4. Why reconciliation is read, not computed

Publication requires that the headline, detail and operational reads of the
lifecycle population agree, for **every** window and scope. `audit_read_
reconciliation` proves that across all 44 combinations — by calling
`repo.fetch_all_funnel_contacts()`, i.e. reading the entire funnel table.

That is an audit's cost model. On a dashboard request it is not affordable.

So the proof is **recorded** and the contract reads it:

```
scripts/record_sql_reader_reconciliation.py --apply
   → runs the audit's OWN audit_read_reconciliation (not a second copy)
   → db.writers.record_reader_reconciliation
   → sql_reader_reconciliation  (append-only, observed_at = when it RAN)

services.canonical_sql_publication_service.publication_inputs()
   → db.crm_funnel_repository.fetch_reader_reconciliation(max_age_hours=36)
```

Three refusals, kept apart because the remedy differs:

* **no row** — the comparison was never recorded. Absence of a check is not
  evidence of agreement.
* **stale row** — it ran, but the population has moved since.
* **`reconciliation_complete = false`** — they were compared and disagreed.

The recorder is a separate command on purpose. `audit_lifecycle_sql_coverage.py`
is read-only by contract — it reports `external_writes_performed: false` and is
run under a session-level `SET TRANSACTION READ ONLY` guard during production
validation. Teaching it to write would break both.

**The consequence is deliberate: with no recorded proof, every window is
withheld.** That is the correct state, not a degraded one.

## 5. What this changes today

Nothing visible. No consumer reads the contract yet, and the boundary
(`boundary_sqlbound_e9ffa89de603`, observed `2026-09-21 04:34:37.772120+00:00`)
is today — so every window in the product currently straddles or precedes it
and would be withheld on the window-local gate alone. The 7d window becomes
fully post-boundary on **2026-09-28**, which is when the reconciliation record
starts deciding anything.

Before then, `scripts/record_sql_reader_reconciliation.py --apply` needs a
scheduled home. It is not wired into the scheduler in this PR.

## 6. Guards

`tests/test_pr_ads_161a1_sql_publication_contract.py` — 34 cases.

* **§1** every gate refuses in isolation, each with the full set of other
  inputs satisfied, plus `test_01` as the positive control proving the gate can
  publish at all.
* **§2** `test_07` is the case the brief names: driven through the **real**
  `window_coverage`, a window whose membership genuinely resolves but which
  straddles the boundary — `cpql_publishable` is the permissive intermediate
  `True`, and production publication is still withheld. `test_08` is its
  negative control: move the same window past the boundary and it publishes.
* **§3** the reconciliation record, including that a missing one and a stale
  one both refuse, and that the repository reader fails closed with no database.
* **§4** an **AST** guard — not a substring search — asserting no module under
  `services/`, `api/`, `db/`, `scheduler/`, `connectors/` or `analysis/`
  reaches `lifecycle_sql_coverage` or names `cpql_publishable` **or**
  `complete_sql_total` (both are set from membership alone), outside an
  explicit allow-list. The detector covers plain imports, `from`-imports,
  package imports, **relative** imports, `importlib.import_module` and
  `sys.modules[...]`.

  `test_15` is its negative control and calls **the same function** the guard
  calls — `_import_offenders`. The first version of this PR re-implemented a
  weaker predicate there instead, and the review proved both tests stayed
  green with the real detector deliberately gutted. One function, two callers,
  is the fix; `test_15b` is the positive control proving the detector does not
  simply flag everything.

* **§5** the defects the review of this PR found, each with its control:
  the incident gate reading a key the repository never returns (`test_19`,
  `test_20`), the recorder writing an unproven run as a disagreement
  (`test_21`), a naive or future-dated `observed_at` (`test_22`, `test_23`), a
  published verdict with no count (`test_24`), a malformed coverage input
  (`test_25`), and freshness as an independent audit gate (`test_26`).

## 7. Not in this PR

* **No consumer migrated.** Doctrine inventory counts are unchanged:
  25 legacy / 6 mixed / 4 canonical. The scanner reports
  `READY_FOR_ROADMAP`, `audit_complete: true`, 0 unclassified occurrences,
  0 registry problems — a migration claim the scanner does not support is not
  made.
* **The recorder is not scheduled.** It runs on demand. Wiring it into the
  incremental sync belongs with the consumers that depend on it.
* **No frontend change.** `withheld_payload()` defines the API shape a
  consumer will serialise; no endpoint emits it yet.
* **No external writes.** The only write introduced is one append-only row in
  our own database, and only from the explicit `--apply` recorder.
