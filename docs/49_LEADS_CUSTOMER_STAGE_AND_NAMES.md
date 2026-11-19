# 49 — Leads page: the Customer stage, customers won, and names

**PR-ADS-166A.** The Leads page showed a count of HubSpot lifecycle stage
changes under the word "Customers". For Q3 2026 that was about 219 contacts —
206 with an OFFLINE original source, 182 created before the quarter — while 8
deals were closed-won. This PR names the count for what it is, shows customers
won beside it from the closed-won reader contract, and adds contact and
company names. It adds no customer rule, no won predicate and no attribution
rule.

| Owner | Role |
| --- | --- |
| `services/canonical_crm_funnel_service.py` | the stage counts (`build`), the contact rows (`contacts`), and `build_customers_won`, which republishes the reader contract |
| `services/canonical_revenue_service.py` | `load_won_deals` inside `single_snapshot` — the only source of customers won |
| `services/hubspot_name_backfill_service.py` | the two read-only name loads |
| `connectors/hubspot_pull.py` | `read_contact_names`, `read_company_names` (batch reads by id) |

## 1. Two figures, two metrics

| | Moved to Customer stage in HubSpot | Customers won |
| --- | --- | --- |
| Counts | contacts whose lifecycle stage became Customer | distinct HubSpot companies on closed-won deals |
| `metric_family` | `lifecycle_stage_events` | `closed_won_customers` |
| `window_basis` | `date_entered_customer` | `deal_close_date` |
| Dedup key | `contact_id` | `hubspot_company_id` |
| Served by | `GET /api/crm-funnel` | `GET /api/crm-funnel/customers-won` |
| Can be withheld | no (it is a count of rows) | yes — `{value, status, reason}` from the reader contract |

Rules:

* The stage count is never labelled "Customers", on any surface. Its one
  label is `analysis/crm_lifecycle.EVENT_LABELS["customer"]` = **"Moved to
  Customer stage"** (the Leads strip adds "in HubSpot"). The Overview's
  Lifecycle Activity and Lifecycle Cohort panels read the same label: the
  activity card says "Contacts moved to the Customer stage during this
  period", and a conversion into the stage names its target "Customer stage"
  — a stage move, never a customer conversion.
* Neither figure is added to, subtracted from or derived from the other.
  `build_customers_won` reads nothing from the funnel.
* Customers won is `null` with a status and a reason when it is not published.
  The page renders "Withheld" or "Unavailable" and the reason — never a dash,
  a 0 or the stage count.
* Customers won is refused, not approximated, when a Scope or Source filter is
  on (those classify contacts, not deals) and for a window that is not a
  business window.

## 2. The won-customers list

`won_customers` on the customers-won payload has one entry per won customer:
company id, company name, closed-won deal count, latest close date. It is
built from the reader's own population and identity map (`deals`,
`company_ids_by_deal`).

* **Withheld means absent.** The key exists only when the customer count is
  published. Otherwise there is no key and no partial list.
* If the entries do not number exactly the published count, the list is left
  out (`won_customers_omitted`) and the page says "The list of customers
  could not be built for this period."
* The Leads block is a registered consumer of the `customers` and
  `closed_won_deals` identities in `services/cross_page_parity_service.py`:
  it must show the Deals page's figures for the same window.
* The list names customers; it never decides who is one.

## 3. Names

### 3.1 Contacts

`hubspot_contact_funnel.first_name`, `last_name`, `names_read_at`,
`names_last_asked_at`.

| `contact_name_status` | Stored | The page says | Meaning |
| --- | --- | --- | --- |
| `recorded` | a displayable name | the name | |
| `not_displayable` | a value that is an email or has no letter | "Name not shown" | HubSpot holds a value in the name field that is not shown as a name |
| `not_recorded` | `names_read_at` set, no name | "Name not recorded" | HubSpot was asked and holds no name |
| `not_returned` | `names_read_at` NULL, `names_last_asked_at` set | "Not returned by HubSpot" | a read HubSpot answered asked for this contact and did not get it back |
| `not_loaded` | both stamps NULL | "Name not loaded yet" | the platform has never asked |

These are different claims. `first_name IS NULL` cannot tell them apart and is
never used as the queue or the label.

Names arrive two ways, both read-only:

1. The modified-since contact sync asks for `firstname` / `lastname` and
   stamps `names_read_at` for every contact it reads.
2. The backfill step (`hubspot/contact_names` in the daily incremental sync)
   reads contacts by id, 100 per HubSpot call, at most
   `name_backfill.max_contacts_per_run` (3000) per run. Queue order: contacts
   never asked for, newest first; then contacts HubSpot did not return, asked
   again at most once every `name_backfill.not_returned_retry_days` (7). Ids
   are selected in SQL with `LIMIT`.

Rules for both:

* An id HubSpot returned is stamped `names_read_at`, whatever name came back.
* An id HubSpot did not return (deleted, merged) is **not** "no name": no
  name is written, `names_read_at` stays NULL, `names_last_asked_at` is
  stamped, and it is counted as `not_returned`. That includes a 207 (which
  the SDK returns as results beside errors) and a 404 that names the
  requested ids as not found: the batch was answered. Removing contacts belongs to PR-ADS-163C.
* A failed batch stamps nothing. Its ids are read again next run.
* A read is retried, with bounded backoff, on 429, any 5xx, a timeout or a
  transport error. Any other 4xx (a missing scope is 403) is not retried and
  no further batch is asked that run; the step's error text carries the HTTP
  status. The SDK raises a different `ApiException` class per package
  (`hubspot.crm.contacts`, `.companies`, `.deals`) with no common base:
  catch `HUBSPOT_API_EXCEPTIONS`, never one class, and make a fake raise the
  class of the API it stands in for.
* An empty answer never blanks a stored name (`COALESCE`). A different
  non-empty name replaces it.
* Email is never requested. A name field that holds an email (or no letter
  at all) is stored as HubSpot sent it and is not displayed; the row reads
  "Name not shown". The search box does not match such a value either.

While any stored contact has never been asked for, the Contact column header
says "Names are being loaded from HubSpot: N of M contacts read so far." from
counts in the database. It clears when every contact has been asked for, even
if HubSpot did not return some.

### 3.2 Companies

`hubspot_company_name (company_id, name, read_at)` — the name of a HubSpot
company that appears in `hubspot_deal_company_association`.

| Row | The list says |
| --- | --- |
| none | "Company name not loaded yet" |
| `read_at` NULL (asked, not returned) | "Company not returned by HubSpot" |
| read, `name` NULL | "Company has no name in HubSpot" |
| `name` set | the name |
| store unreadable | "Company name unavailable" |

The step `hubspot/company_names` reads never-asked ids first, then ids whose
last answer is more than `name_backfill.company_reread_days` (7) old. A failed
read writes nothing, so the last good name stands. A named company HubSpot
later stops returning keeps its name. An older read never overwrites a newer
one. Retries and refusals follow §3.1.

The **Company** column on contact rows is unchanged: it is the text typed on
the HubSpot contact, not a company record.

## 4. Step status

Both name steps report `success`, `partial` or `failed`:

* `success` — every requested batch was answered and written. No batch due is
  a success.
* `partial` — some batches were answered and some were not, or an answered
  batch could not be written.
* `failed` — the queue could not be read, or no batch at all was answered.

`partial` stays `partial` on the run and is an error with its reason. The
steps have no sync batch and no canonical freshness dataset: their state is
the stamp on each row.

## 5. Who sees names

`/api/crm-funnel/contacts` and `/api/crm-funnel/customers-won` require a
signed-in user of any role, as the contact rows always did. Names follow that
gate. Making them admin-only is an owner decision that has not been made.

## 6. Known gaps

See `docs/09_REPO_STATE.md`, section PR-ADS-166A.
