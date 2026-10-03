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
| global | contact-funnel source proven fresh **as the service read it** | `publication_inputs` |

All of them, or `publishable` is `False` and `complete_sql_total` is `None`.

Freshness appears twice on purpose. The window-local row is the caller's copy,
carried inside the `coverage` dict it hands in. The global row is the read
`publication_inputs` performs itself. Round 3 of the audit found the second one
being *stamped on the verdict as a label* and never gated: a caller whose
coverage said fresh, over a service that had just read stale, published a
certified 42 while `audit_certification` refused the identical inputs. Both
signals must now say fresh.

### The order refusals are reported in

A verdict names one reason, so which gate fires first is part of the contract:

1. **window-local** — could this window ever be certified (boundary, membership,
   gaps)? A window that precedes the boundary is refused for that, permanently,
   whatever else is wrong today.
2. **stores readable** — could we look at all? `unavailable`, not `withheld`.
3. **reader reconciliation** — do the canonical readers agree, and do we have
   proof? An unproven or unreadable record is `unavailable`.
4. **source freshness** — we looked, everything was readable and agreed, and
   the source has stopped arriving. `withheld`.
5. **coverage internally consistent** — the window is marked certifiable while
   its undated membership is unresolved. The two cannot both be true, so it
   fails closed rather than being resolved in either direction. `unavailable`.
6. **counted population present** — every gate passed but the window carries no
   count. `unavailable`.

Step 1 has one exception in the other direction. `publication_inputs` sets
`boundary_observed_at = None` whenever the boundary store is unreadable, so a
caller building coverage from it gets the window-local `CERT_NO_BOUNDARY` —
and round 4 found that published as the affirmative claim *"no coverage
boundary exists"*, identical at every surface to a store that read fine and
genuinely holds no boundary. During an outage every SQL surface stated a
permanent, benign, nothing-to-do condition. `boundary_readable` was a
parameter that changed nothing on any input `publication_inputs` can produce.
An unreadable boundary store now yields `unavailable /
certification_inputs_unreadable`, and `test_40` asserts the two payloads
differ.

Freshness sits *after* the readability and reconciliation gates because those
answer "could we look", and a `withheld` verdict must not displace an
`unavailable` one. Round 3 caught the first version placing it first, so "we
could not read the boundary store" was reported as "the source is stale" —
which sends an operator to fix a pipeline that is not the problem.

**Round 3's reordering alone did not achieve that, and this document claimed
it did.** Freshness is gated in *two* places, and only the step-4 one moved.
`lifecycle_sql_coverage._certification` refuses an otherwise-perfect window
with `CERT_STALE_SOURCE`, which arrives here as a *window-local* reason — step
1. On every coherent production input a stale source therefore still won:

```
BEFORE the round-4 fix, source stale in every case:
  stale + reconciliation record absent  ->  not_certifiable_source_not_fresh
  stale + readers disagreed             ->  not_certifiable_source_not_fresh
  stale + boundary store unreadable     ->  not_certifiable_source_not_fresh
```

The step-4 gate fired only when the caller's copy and the service's read
disagreed — the one case fix #1 exists to catch, and nothing else. Because no
reconciliation record exists yet (§5), a stale sync would have sent an
operator to the pipeline while the refusal actually blocking every window went
unreported.

Step 1 now **defers** a reason in `FRESHNESS_REFUSALS` to the global gates
instead of returning, so the window's own specific reason survives but no
longer outranks them. The deferral is conditional on `source_fresh is not
True`, so a dict claiming a stale status beside a fresh source cannot fall
through to the publishing branch — that fail-open is `test_42`'s last
assertion.

`_certification` evaluates freshness **last** of its own eight branches, so a
window that is both stale and structurally refused still reports the
structural reason. The deferral does not change that.

For the same reason a freshness refusal never borrows the window's own
`certification_status`: on the only shape `window_coverage` emits with
`certification_eligible: True`, that status is literally `"eligible"`. The
reason is taken from the window only when the window's own status is itself
about freshness — the membership test is `sql_publication.FRESHNESS_REFUSALS`,
the single table the gate, the service and the audit all consult.

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

Round 4 found the previous version of this paragraph unreproducible: it gave
a delta count without defining the input space it was counted over. The space
is now stated, so the number can be checked.

**Space (1,152 cells).** Every window built through the real `window_coverage`:
window position (after / straddles / before / open-start) × boundary (present
/ absent) × incident store (empty / one open gap / unreadable) × membership
(resolved / unresolved) × freshness (fresh / stale / unreadable) ×
`boundary.available` (T/F) × `post_boundary_incidents_available` (T/F) ×
`reconciliation_complete` (T/F) = 4·2·3·2·3·2·2·2.

**Measured, pre-PR-ADS-161A-1 `audit_certification` vs current:**

| | Count |
|---|---|
| `windows_certified` deltas | **0** — the decision is preserved in every cell |
| reason-string deltas | 307, in 6 classes |

```
x288  not_certifiable_no_boundary_established -> certification_inputs_unreadable
 x12  not_certifiable_source_not_fresh        -> certification_inputs_unreadable
  x3  canonical_readers_did_not_reconcile     -> certification_inputs_unreadable
  x2  not_certifiable_source_not_fresh        -> canonical_readers_did_not_reconcile
  x1  not_certifiable_source_not_fresh        -> source_stale
  x1  not_certifiable_source_not_fresh        -> source_freshness_unreadable
```

The reason deltas are large **on purpose**: the 288 is the round-4 boundary
fix (an unread store stops claiming no boundary exists), the 12 and 3 are the
same fix reached through other paths, and the 2 is the freshness deferral
letting the reconciliation refusal through. Each moves a reason toward the
action an operator must actually take. No cell changes whether a window
certifies.

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

`tests/test_pr_ads_161a1_sql_publication_contract.py` — 95 cases.

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

* **§6** what round 3 found, each with the control that proves the guard can
  fail: the service reading freshness and only labelling it (`test_31`, with
  `test_32` over every non-`True` shape), a freshness refusal reported under
  the reason `"eligible"` (`test_33`), the freshness gate placed ahead of the
  "could we look at all" gates (`test_34`), the audit's relabel keyed on
  `source_fresh` alone so that a pre-boundary window reported `source_stale`
  (`test_35`), the recorder coercing an unproven `None` to a recorded
  disagreement (`test_36`), the same relabel defect repeated one layer up in
  the service (`test_37`), and the shared `FRESHNESS_REFUSALS` table drifting
  away from the coverage constant it spells out (`test_38`).

  Every one of these was run as a **counterfactual**: the pre-fix code was
  restored, the module re-run, and the named test confirmed failing. A guard
  whose absence changes nothing is not a guard, and §6 exists because round 3
  found two that were not.

* **§7** what round 4 found. Round 4's finding was *not* a fabricated control —
  every §6 guard does go red under a targeted mutation. The defect was subtler
  and worse: several of them prove their property only on input tuples
  `publication_inputs()` and `window_coverage()` **cannot jointly produce**
  (`boundary_readable: False` beside a `boundary_observed_at`;
  `certification_eligible: True` beside `source_fresh: False`), and on the
  tuples production *does* produce the property was false.

  Every §7 test whose subject is a *production* input therefore builds its
  coverage by driving the real `window_coverage()` (`_coverage_from_inputs`)
  from an inputs dict that `_inputs()` shapes to `publication_inputs()`'s
  output. `_inputs()` enforces both couplings the real function enforces: an
  unreadable boundary store cannot also hand back a boundary instant, and an
  unreadable incident store yields `None`, never `[]` — the unknown, not the
  affirmative claim "no open gaps". It is a **mirror** of
  `publication_inputs()`, not a call to it; only `test_12` and `test_19` drive
  the real function.

  `test_39`, `test_43`, `test_45` and `test_42`'s closing assertion
  deliberately bypass that path, because their subject is a state production
  cannot produce: a scan of the module's own source, the incoherent dicts the
  `publication_for` **caller seam** exposes, and malformed non-mappings. A
  fixture must be production-shaped when it stands in for production; those
  stand in for a buggy caller, which is the thing they exist to catch.

  `test_39` guards the three copied
  `CERT_*` literals, `test_40` the unread boundary store, `test_41`/`test_42`
  the freshness deferral and its fail-open, `test_43` the eligible-but-
  incomplete contradiction, `test_44` `None`-vs-`False` freshness, `test_45`
  the malformed-coverage regression the service had and the gate did not.

  **Round 5 correction.** Round 5 measured `test_41`'s own docstring against
  the pre-fix gate and found one of its four listed cases was never true:
  `stale + incident store unreadable` reported `unavailable /
  certification_unavailable` before the fix and reports it now, because an
  unreadable incident store makes `publication_inputs` emit
  `open_incidents = None`, which `_certification` turns into
  `CERT_UNAVAILABLE` several branches before the stale-source branch is
  reached. It was also the one case `_inputs()` could not shape honestly, so
  no §7 test exercised it at all. The docstring now states the measured
  value, `_inputs()` enforces the `None` coupling, and the case is carried as
  `test_41`'s sixth arm — a negative control proving the deferral leaves that
  refusal exactly where it was. Both additions were shown load-bearing:
  removing the coupling, or widening `FRESHNESS_REFUSALS` to swallow
  `certification_unavailable`, each turns `test_41` red.

  Round 5 also removed the last impossible tuple round 4 had named but left
  standing: §6's `_service_inputs` built its own dict, so
  `_service_inputs(boundary_readable=False)` handed back a boundary instant.
  It now delegates to `_inputs`, and every §6 assertion holds unchanged on the
  corrected shape — they were true, they were simply not being proven on
  anything production emits.

  **The lesson, recorded because it recurred four rounds running:** a fixture
  that cannot arise from the producing code proves nothing about the consuming
  code. Prefer driving the real producer over hand-building its output.

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

### Inherited by PR-ADS-161A-2 — four defects round 5 measured and did not fix

All four are **pre-existing in `main`** and byte-identical under PR #185, so
none of them is a regression of this work. All four are latent only because
nothing renders the contract yet — 161A-2 is the PR that supplies the human
to mislead, so all four should close before a consumer is migrated.

1. **An unread boundary store still publishes `coverage_complete: true` on the
   API shape, beside a real open gap.** `lifecycle_sql_coverage.py:298` reads
   a null boundary instant as "no prospective period", and the service
   overloads `boundary_observed_at = None` to mean both "no boundary" and
   "boundary unknown". Measured: `status: unavailable`,
   `withheld_reason: certification_inputs_unreadable`, `coverage_complete:
   True`, `open_post_boundary_gaps: 1` — in one object. This is BLOCKER 1's
   collapse, fixed at the status axis and left intact one level down.
   *Smallest fix:* give `window_coverage` a `boundary_readable` argument
   (default `True`) and make the vacuity exception conditional on it; or set
   `coverage_complete: None` on the `WITHHELD_INPUTS_UNREADABLE` return.
2. **The step-5 self-consistency guard checks one field of four.** With
   `certification_eligible: True` and a coherent count, a caller-built dict
   whose `certification_status` refuses, or whose `window_after_boundary` is
   `False`, or which carries open gaps, still yields `publishable=True
   total=42 certified=True`. `test_43`'s own justification — "`window_coverage`
   cannot emit that pair, so it arises only from a caller-built dict, which is
   exactly the seam `publication_for` exposes" — is equally true of the other
   three. *Smallest fix:* extend the guard at `sql_publication.py:360` to
   `certification_status`, `window_after_boundary` and
   `open_post_boundary_gaps`, adding `"eligible"` to the tabled literals.
3. **`reconciliation_gate` raises `AttributeError` on a truthy non-mapping.**
   `sql_publication.py:140-142` does `if not reconciliation:` then
   `.get(...)`. F4 hardened `coverage` against exactly this shape and not
   `reconciliation` or `inputs`. A crash, not a false claim — but the module's
   contract is that every gate fails closed.
4. **A withheld payload carries a blank `explanation`** when `coverage is
   None` and the service's source is not fresh:
   `canonical_sql_publication_service.py:190-191` manufactures a non-empty
   `{"source_fresh": …}`, so the pure gate's absent-coverage branch — the one
   carrying the real explanation — never fires.

Round 5 also recorded three non-defects worth knowing: `incidents_readable`
is inert on every production-shaped input (harmlessly — the resulting claim
is truthful, but its reason code is the generic `certification_unavailable`,
which a consumer cannot tell apart from an unreadable contact population);
the freshness deferral can downgrade `unavailable` to `withheld` on tuples
requiring `certification_eligible: None`, which no producer emits; and
`source_freshness_unreadable` is a reason code this service invents outside
`sql_coverage_freshness.FRESHNESS_REASONS` and outside `docs/41`'s table.
