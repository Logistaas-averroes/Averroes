# PR-ADS-159 — Lifecycle SQL Timestamp Recovery, Coverage Bounds and Read Consistency

This PR does **not** migrate legacy SQL consumers. PR-ADS-158's inventory (25
active legacy consumers, 6 mixed/adapter, 16 affecting executive totals) is
unchanged and the doctrine-inventory audit continues to disclose it. What this
PR does is make the lifecycle SQL evidence underneath a migration truthful and
self-consistent, so that migrating a consumer stops meaning "swap one incomplete
number for another".

---

## §1 — Why the recovery dry run recovered zero

The production dry run examined 50 contacts and reported
`history_payload_missing` 50 times, 0 usable histories, 0 recovered timestamps.
Read literally, that says the connected portal holds no `lifecyclestage`
history at all.

It was not saying that. **The request never asked for history.**

`connectors/hubspot_pull.fetch_lifecycle_stage_history` called:

```python
client.crm.contacts.batch_api.read(
    batch_read_input_simple_public_object_id={
        "inputs": [...],
        "properties": ["lifecyclestage"],
        "properties_with_history": ["lifecyclestage"],   # a plain dict
    })
```

The SDK's `ApiClient.sanitize_for_serialization` documents its own behaviour:

> If obj is dict, return the dict.

`attribute_map` is applied **only to model instances**. A dict body is passed
through verbatim, so the wire body carried the snake_case key
`properties_with_history`. HubSpot's batch-read endpoint does not know that
field, ignored it, and answered with contacts carrying no history container.
Every contact then parsed as `HISTORY_PROPERTY_ABSENT`.

Proven, not argued:

```
dict body  -> ['inputs', 'properties', 'properties_with_history']
model body -> ['inputs', 'properties', 'propertiesWithHistory']
```

The old docstring claimed "the request model serializes
`properties_with_history` to `propertiesWithHistory`". That was true **of the
model** and irrelevant to the code, which never constructed one. A parameter
that exists on an object the call does not use is not a parameter that was sent.

### The fix

`_batch_history_body()` builds `BatchReadInputSimplePublicObjectId`, so
`attribute_map` applies and `propertiesWithHistory` goes on the wire.
`test_01` asserts the serialized key through the SDK's real serializer;
`test_02` is the negative control proving the dict form did not.

### The individual read

`basic_api.get_by_id(contact_id, properties_with_history=[...])` is wired
correctly by the SDK on its own — it appends `("propertiesWithHistory", …)` as a
**query** parameter. `fetch_lifecycle_stage_history_single` uses it as a bounded
fallback, and `diagnose_lifecycle_history_reads` runs both reads over one
bounded sample so that "we got nothing" can be told apart from "there is
nothing". Its verdicts:

| Verdict | Meaning |
| --- | --- |
| `both_paths_return_history` | the portal has history and both reads see it |
| `individual_only_batch_returns_no_history` | **our batch request is wrong**, not HubSpot's retention |
| `batch_only_individual_returns_no_history` | the individual read is the broken one |
| `neither_path_returns_history` | the portal genuinely holds none for this sample |

The diagnostic records endpoint category, HTTP outcome, requested/returned
counts, container presence, version counts and parser verdicts. It records no
token, email, name, company, full payload, or unrelated property value.

---

## §2 — Evidence vocabularies (corrected by R4)

The first cut declared ten states as one flat "evidence vocabulary" and claimed
they were mutually exclusive. They were not one thing at all — and two of the ten
were **unreachable**, emitted by nothing, while two states the code *did* emit
were absent from the list. The exhaustiveness test used a subset check, so it
passed on both counts. **A constant kept alive so a static test goes green is not
a contract.**

There are five denominators, so there are five vocabularies:

**A · per read attempt** — `history_request_ok`, `history_request_failed`,
`hubspot_authorization_failed`

**B · per contact asked** — `history_payload_present`, `history_payload_missing`,
`history_payload_empty`, `history_contact_not_returned`,
`history_individual_fallback_deferred`

**C · per (contact, SQL gap)** — `history_sql_timestamp_recovered`,
`history_present_no_sql_stage`, `history_sql_version_missing_timestamp`,
`history_sql_timestamp_invalid`, `unrecoverable_no_hubspot_evidence`,
`sql_recovery_deferred_by_budget`, plus the three payload reasons (for a gap on a
contact HubSpot answered with nothing, the payload state *is* the answer)

**D · per (contact, stage gap)** in the all-stage run — the generic
PR-ADS-155-F1 names plus the SQL ones, because the SQL vocabulary is keyed on the
**event**, not on the run mode

**E · per diagnosis run** — `both_paths_return_history`,
`individual_only_batch_returns_no_history`,
`batch_only_individual_returns_no_history`, `neither_path_returns_history`,
`batch_request_failed`, `individual_request_failed`, `both_requests_failed`,
`history_parameter_dropped_or_unsupported`

Every report names the vocabulary each count block belongs to, so two numbers
with different denominators can never be added by a reader who assumed they
described the same thing.

### The two states that were unreachable

* **`history_parameter_dropped_or_unsupported`** is a statement about the
  **request** — something one contact's payload can never make. Declaring it a
  per-contact state is precisely what made it unreachable. It now lives only in
  vocabulary E, and the diagnostic emits it structurally whenever the serialized
  batch body will not carry `propertiesWithHistory`.
* **`unrecoverable_no_hubspot_evidence`** now carries a proof. It is emitted only
  when **both** supported paths completed and the surviving answer is an
  affirmative absence (an EMPTY history). A *missing payload* could still be our
  request; a *contact not returned* is an identity question. Neither proves the
  transition was never recorded, and neither earns the word "unrecoverable".

`matching_stage_version_recovered` was being added to the summary
unconditionally, so an SQL run whose rows all said
`history_sql_timestamp_recovered` was summarised under a name that appeared
nowhere in its own output. The summary is now built from each row's own
`evidence_state`.

Retained from the first cut: `history_contact_not_returned` no longer folds into
`history_payload_missing`; `history_sql_timestamp_invalid` is separate from
`history_sql_version_missing_timestamp` (both arrive as `timestamp: None`, so the
connector carries `timestamp_raw`). An unrecognised state is reported **as
itself**.

### Diagnosis must not overclaim

A failed batch request is no longer reported as `neither_path_returns_history` —
the strongest possible claim about the portal, drawn from a request that never
got an answer. Request failure has its own verdicts, both paths' outcomes are
preserved even when they disagree, and `paths_agree` records whether they did.

## §3 — SQL-specific candidates

A candidate is exactly:

* the contact's current lifecycle stage implies it entered SQL
  (`analysis.crm_lifecycle.stages_implying_event("sql")` — one doctrine source,
  applied in Python and passed to SQL as an array), **and**
* it has no **effective** SQL-entry timestamp: no direct property *and* no
  previously recovered history date.

The second half needs the recovery join, unlike the all-stage read. Without it
every run re-asks HubSpot about contacts it already answered.
### Remaining work is exact (R3)

`more_candidates_remain` was `len(rows) >= limit`, which cannot tell "exactly the
last page" from "another page exists" — so it reported more work forever, and an
operator reading a false "no more work" would stop early on a real gap. The
command now fetches `limit + 1`, processes at most `limit`, and sets the flag
from what it actually saw. Budget-deferred candidates count as remaining work. A
pass that could not finish reports `None` — an aborted run proves nothing about
what is left, not even that there is more.

`--sql-only` selects this population; the report carries `candidate_mode`,
`more_candidates_remain`, the per-state outcome counts, the next cursor, and the
individual-read accounting. No PII.

---

## §4 — Bounded fallback and safety

| Control | Behaviour |
| --- | --- |
| request budget | `--individual-budget` (default 200) is a hard ceiling for the **whole run**; contacts beyond it are reported as *unattempted*, never as unrecoverable |
| transient failures | 429 retried with exponential backoff |
| permanent failures | 401/403 **stop the run** under `hubspot_authorization_failed` — never counted as N contacts without history, which is the §1 false conclusion arriving through another door |
| 404 | `history_contact_not_returned` — HubSpot answered |
| resumability | durable cursor, advanced only on completed `--apply` runs |
| writes | `--apply` writes only `hubspot_lifecycle_stage_history`; **never** HubSpot |

### The cursor never passes an unadjudicated contact (R2)

The first cut set `last_contact_id` on **every row it looked at**, including
rows whose required individual read the budget could not fund. It then reported
those contacts as "not attempted" — and persisted a cursor past them. On the
next resumed run they were below the cursor and **skipped permanently**. A
bounded command that loses candidates is worse than one that does less work.

The pass now **stops** at the first contact that needs an individual read it
cannot fund. That contact and everything after it stay eligible; the checkpoint
advances only to the last **fully adjudicated** contact. Four states are kept
apart in the report: batch attempted, individual fallback attempted, fallback
deferred by budget, and fully adjudicated. A deferred contact is reported as
`sql_recovery_deferred_by_budget` — never as `unrecoverable_no_hubspot_evidence`.

### Independent checkpoints per candidate mode (R1)

The general and SQL-only runs shared the scope `lifecycle_stage_history`, so an
SQL-only run resumed from whatever cursor the last all-stage run left behind.
The all-stage population is a **superset ordered by the same key**, so its
cursor is normally far ahead — and every SQL candidate below it was skipped
silently while the report said the run completed.

| Mode | Checkpoint scope |
| --- | --- |
| all-stage (default) | `lifecycle_stage_history` |
| `--sql-only` | `lifecycle_stage_history:sql` |
| any other event | `lifecycle_stage_history:<event>` |

The general scope keeps its original name, so the existing production checkpoint
resumes exactly where it is. `--restart` ignores the cursor of the **current**
mode only. An unrecognised mode gets its own scope rather than inheriting the
general one — inheriting is the defect.

Modes: dry run by default, `--apply`, `--sql-only`, `--limit`, `--restart`,
`--no-individual-fallback`, `--individual-budget`, `--diagnose`, `--json`.

---

## §5 — One effective SQL-entry expression

**The drift.** `fetch_funnel_contacts` / `fetch_all_funnel_contacts` filtered on
the coalesced expression. `fetch_funnel_contact_page` and
`fetch_operational_status_counts` filtered on the **bare column**. So a contact
whose SQL date came from recovered history was counted in the headline and
absent from the detail page and the operational counts that are supposed to
explain it — two published numbers that disagree by construction.

It was invisible until a timestamp was actually recovered. With zero recovered
rows the bare column and the coalesced expression return identical results, and
the reads agree by accident.

**The doctrine**, defined once in `db/crm_funnel_repository.py`:

```python
direct_date_sql(event)     # 1. f.date_entered_<event>          (HubSpot property)
recovered_date_sql(event)  # 2. h.recovered_date_entered_<event> (lifecycle history)
effective_date_sql(event)  # COALESCE(1, 2) — NULL when neither exists
```

The direct property always wins; recovery fills a gap and can never override a
fact. Every canonical read now uses it: headline counts, scoped counts, contact
pagination, operational status counts, recovery candidates, coverage population,
and the unresolved-bounds read. The contact page also *projects* the coalesced
date and flags it (`date_entered_sql_from_history`), so a recovered row shows
its date rather than a blank beside a row just proved to be in window.

Never a substitute for a stage-entry date: contact creation, latest status
update, latest classification timestamp, deal creation, current stage alone,
inferred stage ordering, campaign observation date, application run date.

A drift-prevention test parametrises every canonical read; the audit repeats the
check statically and names each certified reader.

### Reconciled across every window and scope (R5)

The first cut compared the three reads for `all_time` with **no attribution
scope** — one of forty-four combinations — and the PR then claimed they
reconcile everywhere. All 44 now run: 11 resolved lifecycle windows × 4 canonical
scopes (`all_source`, `google_ads_source`, `campaign_attributable`,
`keyword_attributable`).

The allow-lists come from the canonical service's own
`resolve_population_filters` and `_build_campaign_resolver`. The audit does not
re-implement attribution classification: a second copy that agreed would prove
nothing, and one that disagreed would report the audit's bug as the product's.

Each pair is reported separately with its window, window type, scope, the three
counts and the effective-date basis. A pair the audit cannot check is
**unavailable with a reason** — `campaign_identity_unavailable`,
`scope_membership_unavailable`, `scoped_reader_unavailable`,
`contact_read_unavailable` — with all three counts `None`, never zero. A
disagreement between available readers is exit 1; unreadable inputs are exit 2;
`--strict` keeps its separate exit 3 for incomplete evidence coverage.

---

## §6 — Global gaps versus per-window gaps

The PR-ADS-158 audit reported the same 525 undated SQL contacts against **all
eleven** windows. That is one finding printed eleven times.

Those 525 are a **global** population. One temporal fact is known about each:
when the contact record was created. That yields exactly one sound implication:

> a contact created **after** a window ended cannot have entered SQL inside it.

An SQL transition cannot precede the contact's own existence, so creation is a
lower bound on the event. It is used to **disprove** membership and for nothing
else — never as the event date, never to include a contact in a window.

Deliberately still unknown:

* creation *before* a window's end proves nothing either way;
* a contact with no creation timestamp cannot be ruled out of **any** window,
  and is reported separately as `unresolved_without_created_at` because it
  bounds what this method can ever achieve;
* **no monotonic-ordering inference.** "It reached opportunity on D, so it
  reached SQL before D" holds only in a portal that never skips stages and never
  back-fills, and this repository has no tested evidence that this portal is
  such a portal. `test_38` asserts no neighbouring stage date and no
  `STAGE_RANK` appears in the coverage module's executable code.

Reported per window, from `analysis/lifecycle_sql_coverage.py`:

| Field | Meaning |
| --- | --- |
| `global_missing_sql_entry_date` | the whole undated population — the same number every window, stated once |
| `window_membership_unresolved` | undated contacts this window **cannot** rule out |
| `window_membership_proven_outside` | undated contacts proven not to be in it |
| `window_membership_recovered` | contacts in the window on a recovered date |
| `window_total_complete` | `True` only when nothing is unresolved |

The window end is **inclusive** (matching `< end + INTERVAL '1 day'`), asserted
at the boundary day: an off-by-one here would rule a contact out of a window it
might genuinely belong to, which is the one direction this must never err in.

Every window states *why* completeness is or is not proven, in
`explanation`.

---

## §7 — Publication stays fail-closed

While a window's completeness is unproven:

* the **confirmed subset** may be published, labelled as a subset
  (`confirmed_sqls`);
* the **complete total** is `None` — not zero, and not the subset relabelled;
* **CPQL** is `None`;
* `reason` and `explanation` carry the missing coverage;
* an unreadable population reports `None`, never zero missing dates.

A window is publishable only via `window_total_complete`. A proven-empty window
publishes `0` — that is a measured zero, distinct from a withheld one.

---

## §8 — The coverage audit

```
python -m scripts.audit_lifecycle_sql_coverage [--json] [--strict]
```

Two questions, never merged:

* **`audit_complete`** — the audit ran and every check returned an answer.
* **`coverage_complete`** — every window's population is fully dated.

An audit that runs perfectly over incomplete data is a *successful* audit; it is
doing its job by saying the data is incomplete. Requiring zero unresolved
contacts before producing a report would mean no report exists precisely while
the gap is being worked.

| Exit | Meaning |
| --- | --- |
| 0 | the audit ran and its contract checks passed (coverage may still be incomplete) |
| 1 | a contract check **failed** — the code contradicts its own doctrine |
| 2 | the audit could not run — database or population unavailable |
| 3 | `--strict` only: the audit passed, but SQL coverage is incomplete |

Reports: the global candidate population split into direct / recovered /
unresolved; unresolved evidence states; per-window possible-membership gaps with
reasons; effective-date consistency and precedence; headline vs detail vs
operational reconciliation; whether a complete total and CPQL are publishable;
and `external_writes_performed` / `hubspot_calls_performed`, both always false.

Read-only, local database only. `test_47` asserts the audit cannot reach
`hubspot_pull`, `get_client`, Google Ads or Mailchimp. Contacts appear only as
counts.

---

## What is still open

* **Consumer migration is not done.** 25 legacy consumers remain; the
  doctrine-inventory audit still says so.
* **525 unresolved SQL timestamps.** With the batch request fixed, a real dry
  run can now say for the first time how many HubSpot actually holds evidence
  for. Until it does, that number is unknown — not zero.
* **`--strict` is not yet a merge gate.** It cannot be while coverage is
  incomplete by design; wiring it in belongs with the migration that closes the
  gap.
