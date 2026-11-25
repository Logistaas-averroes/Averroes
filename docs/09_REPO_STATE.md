## Repository State — Single Source of Truth
## Logistaas Ads Intelligence System

**Last updated:** PR-ADS-161E — Production readers migrated to canonical customer and closed-won truth (November 2026)

> This document reflects the **actual state of the repository** — not what was planned or intended.
> Update this file in every PR that changes the state of any module listed below.
> AI agents must read this file at the start of each session before assuming what is built.

---

> ### ⚠️ Read the newest sections, not this header's successor below
>
> The block that follows was written at PR-ADS-153E-B and was never updated as
> the repository moved on. It is kept because its architectural content is
> still accurate and because several documents cite it — but its **status**
> claims stop at August 2026. The repository has since merged through
> **PR-ADS-161A-1**; read the dated sections at the end of this file, and
> `git log`, for what is actually true now.

> ### ⚠️ Historical status snapshot (PR-ADS-153E-B, August 2026)
>
> This document's phase/status narrative below predates the PR-ADS-153A–D
> sequence and is retained for its architectural content, not its status claims.
> The current authority is
> `docs/audits/PR-ADS-153A-MINIMUM-VIABLE-TRUTH-AUDIT.md` plus the merged PRs.
>
> * **153A–153D: merged.** Truth audit; canonical CRM funnel; canonical Leads
>   experience; search-term waste consolidation.
> * **153E-A: merged.** Canonical deal ledger built and reconciled in SHADOW
>   MODE — see `docs/35_CANONICAL_REVENUE_LEDGER.md`. No revenue consumer has
>   been switched.
> * **153E-A2: cutover gate hardening.** The 153E-A gate proved the ledger was
>   reconciled but never that it was COMPLETE — a portal with no historical
>   bootstrap passed it. The audit now requires proven bootstrap coverage plus a
>   successful incremental on top of it, and
>   `scripts/backfill_canonical_deal_ledger.py` drives that bootstrap to a
>   proven completion.
> * **153E-A3: merged.** Blank HubSpot monetary fields are normalized by one
>   shared parser before persistence, so currency resolution and the ledger's
>   `NUMERIC` columns can never disagree about whether a deal has an amount.
> * **153E-B: consumer cutover DONE.** The production gate passed
>   (`--all-windows` aggregate `ok: true`, 0 write failures, 0 association
>   failures), and every revenue consumer now reads the canonical deal ledger
>   through ONE shared contract (`services/canonical_revenue_service.py`) at an
>   explicit attribution scope
>   (`all_source ≥ google_ads_source ≥ campaign_attributable ≥ gclid_attributable`).
>   Unit Economics is off local JSON / Windsor and on business windows. Shadow
>   mode is over — see `docs/35_CANONICAL_REVENUE_LEDGER.md` §14–§23.
> * **153F and 153G: remain.** Geo synchronization; legacy table/route deletion.
>   No legacy table is dropped by 153E-B; they remain written and readable for
>   the observation period.
> * **Phase 2 / OCT: blocked.** Offline conversion uploads are not started and
>   are not authorized.
> * **Six-month read-only governance: ACTIVE.** No writes to Google Ads or
>   HubSpot. See `docs/15_SIX_MONTH_READ_ONLY_GOVERNANCE.md`.

---

## Built and Verified (safe to call)

| File | Module | Notes |
|------|--------|-------|
| `analysis/deal_truth.py` | Canonical won predicate + deal→contact resolver | **NEW in PR-ADS-153E-A** — `hs_is_closed_won` is the ONLY won rule (fails closed); one shared association resolver feeding GCLID/source/campaign/country, with `lookup_failed` distinct from `none`. Pure, no I/O |
| `analysis/deal_currency.py` | Fail-closed revenue currency doctrine | **NEW in PR-ADS-153E-A** — `revenue_usd` only when proven (`verified_usd` / `converted` at close-date local FX); unknown currency stays NULL, never 0, never assumed USD. Pure, no I/O |
| `db/deal_ledger_repository.py` | Canonical deal ledger persistence | **NEW in PR-ADS-153E-A** — idempotent by `deal_id`, monotonic on `hubspot_lastmodified_at`; a failed association lookup never destroys prior evidence. **153E-A2:** sync mode is DECLARED (`bootstrap` / `incremental`), the two write different columns, and only a run that proved end-of-results completes the bootstrap |
| `services/hubspot_deal_sync_service.py` | Deal ledger orchestration | **NEW in PR-ADS-153E-A** — SOLE writer of `hubspot_deal_ledger`. All stages, watermarked on `hs_lastmodifieddate` with overlap, resumable backfill. Read-only vs HubSpot |
| `services/revenue_reconciliation_service.py` | Shadow reconciliation + cutover gate | **NEW in PR-ADS-153E-A** — canonical vs `gclid_attribution` vs `deal_source_attribution` at DEAL GRAIN; every difference itemized by deal id + reason. No PII. **153E-A2:** `ok: true` also requires proven bootstrap coverage and a later successful incremental, with stable violation codes |
| `analysis/revenue_scope.py` | Attribution-scope lattice | **NEW in PR-ADS-153E-B** — `all_source ≥ google_ads_source ≥ campaign_attributable ≥ gclid_attributable`, nested BY CONSTRUCTION so a narrower scope can never exceed the population it subsets. Pure, no I/O |
| `services/canonical_revenue_service.py` | THE canonical revenue read contract | **NEW in PR-ADS-153E-B** — the ONLY module that reads canonical revenue. Owns the won predicate, the revenue event date, currency safety, business-window bounds, scope filtering, fail-closed readiness and the response metadata. No consumer may read `deal_ledger_repository` directly (CI-enforced) |
| `services/canonical_unit_economics_service.py` | Unit Economics on canonical sources | **NEW in PR-ADS-153E-B** — canonical Google Ads spend + canonical deal ledger, business windows, declared scope. LTV/CAC and payback are WITHHELD with a reason (recurring revenue is not canonical) rather than computed from the retired local-JSON chain |
| `scripts/backfill_canonical_deal_ledger.py` | Operator historical bootstrap | **NEW in PR-ADS-153E-A2** — drives the bounded, resumable bootstrap to a completion proven in the DURABLE state. Bounded by `--max-passes`; stops on first failure; `--restart` is opt-in. No HTTP endpoint, no startup trigger, no PII in output |
| `analysis/crm_lifecycle.py` | Canonical CRM lifecycle taxonomy | **NEW in PR-ADS-153B** — HubSpot Lifecycle Stage is the funnel spine. Funnel events (lead/mql/sql/opportunity/customer) each map to their own `hs_v2_date_entered_*` property. Pure, no I/O |
| `analysis/mql_status_taxonomy.py` | The ONE `mql_status` mapping | **NEW in PR-ADS-153B** — replaces four divergent copies. Maps every live value incl. previously-unmapped `CLOSED - Bad Contact` / `CLOSED - No Response` / `RESELLER`; distinguishes `no_verdict` from `unmapped`. Operational dimension only — NOT a funnel definition |
| `services/hubspot_contact_funnel_sync_service.py` | Canonical contact ingestion | **NEW in PR-ADS-153B** — SOLE writer of `hubspot_contact_funnel`. All-source, watermarked on `lastmodifieddate`, resumable, durable bootstrap state. Read-only vs HubSpot |
| `services/canonical_crm_funnel_service.py` | Canonical funnel contract | **NEW in PR-ADS-153B** — one definition of Lead/MQL/SQL/Opportunity/Lifecycle-Customer; named scopes (`keyword ≤ campaign ≤ google_ads_source ≤ all_source`); cohort-safe conversions; fail-closed (`unavailable ≠ zero`) |
| `services/crm_funnel_reconciliation_service.py` | Legacy vs lifecycle reconciliation | **NEW in PR-ADS-153B** — contact-by-contact mismatch classes + before/after SQL comparison split into DATE-SHIFT vs POPULATION causes. No emails returned |
| `db/crm_funnel_repository.py` | Canonical funnel reads | **NEW in PR-ADS-153B** — read-only; explicit `available: false` rather than an empty result |
| `scripts/audit_crm_funnel_truth.py` | Production funnel audit | **NEW in PR-ADS-153B** — read-only Render validation (coverage, mql_status mapping, legacy reconciliation, source split) |
| `analysis/sql_doctrine_audit.py` | SQL doctrine audit core | **NEW in PR-ADS-158** — pure: marker discovery, rule-based classification, durable-key population comparison, hidden reconciliation-cause detection, report assembly. No I/O beyond reading source files |
| `analysis/sql_doctrine_registry.py` | Reviewed SQL consumer registry | **NEW in PR-ADS-158** — every production SQL consumer with its definition, date field, dedup key, windows, scope and affects-flags; classification rules for every discovered occurrence; CPQL consumers; decision surfaces; known contract conflicts. Data only, imported by nothing in production |
| `scripts/audit_sql_doctrine_inventory.py` | System-wide SQL doctrine audit | **NEW in PR-ADS-158** — read-only CLI (`--json`, `--static-only`); compares the legacy `status_category = qualified` population against the canonical lifecycle SQL population for every Evidence and Business window; session read-only guard on every pooled connection; exit 0 complete / 1 incomplete / 2 source unavailable |
| `connectors/hubspot_pull.py` | HubSpot CRM connector | **PR-ADS-153B**: added `CONTACT_FUNNEL_PROPERTIES` (lifecyclestage + all five `hs_v2_date_entered_*` + `lastmodifieddate`), `iter_contacts_modified_since()` (all-source, watermarked, resumable, 10k-cap re-anchoring) and the pure `normalize_contact_funnel_row()`. `mql___mdr_comments` deliberately excluded from the canonical path; associations_api crash fixed (PR-ADS-027); now uses CRM v4 REST API for associations — version-agnostic |
| `connectors/windsor_pull.py` | Windsor.ai connector | search term query fixed (PR-ADS-027); removed segment=search_term (400 error), switched to date_preset; ✅ Search-term contract aligned to confirmed Windsor 60-day extraction path (PR-ADS-063); ✅ PR-ADS-065: Enhanced logging (row count, sample keys, search_term field presence), normalize_search_term_rows() added, loud warnings on empty/missing-field pulls |
| `connectors/gclid_match.py` | GCLID reconciliation | Joins Windsor + HubSpot via GCLID; falls back if `logistaas_config.yaml` missing |
| `analysis/core.py` | Waste detection + lead quality + campaign truth | All three functions in one file; `load_json` defined at line 471; PR-ADS-025F: Windsor spend + HubSpot SQLs merged into single row per campaign before write_campaigns() call; junk entries filtered pre-write; PR-ADS-025F-FIX: lq_by_campaign aggregates instead of overwrites; legacy keys emitted for backwards compat |
| `analysis/rule_advisor.py` | Deterministic report generator | **NEW in PR-ADS-021** — `generate_deterministic_report(report_type)` generates markdown from structured JSON outputs; no external API; replaces Claude as default |
| `analysis/advisor.py` | Report generation dispatcher | `generate_weekly_report()` and `generate_monthly_report()` — now defaults to `rule_advisor`; Claude optional via `ADVISOR_MODE=claude`; importing does not require `ANTHROPIC_API_KEY` |
| `scheduler/weekly.py` | Weekly report orchestrator | Full pipeline: pull → analyse → report → deliver; uses deterministic advisor by default; also writes to Postgres after each step (PR-ADS-024); ✅ Weekly search-term pull aligned to 60-day contract where supported (PR-ADS-063) |
| `scheduler/monthly.py` | Monthly report orchestrator | Full pipeline: pull → analyse → report → deliver; per-step error handling; uses deterministic advisor by default; also writes to Postgres after each step (PR-ADS-024) |
| `scheduler/delivery.py` | SendGrid email delivery | Delivers weekly and monthly report files; returns bool |
| `scheduler/run_history.py` | Persistent run log | Writes JSONL to `runtime_logs/run_history.jsonl` |
| `scripts/healthcheck.py` | Pre-flight environment check | Validates env vars, dirs, imports; `ANTHROPIC_API_KEY` optional unless `ADVISOR_MODE=claude`; `APP_SECRET_KEY` and `AUTH_USERS_JSON` required |
| `config/thresholds.yaml` | Decision thresholds | FIX/HOLD/SCALE/CUT rules; lead quality; waste detection |
| `config/junk_patterns.yaml` | Junk pattern library | Intent mismatch patterns; safe-terms whitelist |
| `render.yaml` | Render.com deployment | **Single web service** (uvicorn); Render cron jobs decommissioned by PR-ADS-019; in-app APScheduler handles all scheduled jobs |
| `Makefile` | Manual ops runner | `healthcheck`, `daily`, `weekly`, `monthly`, `validate`, `runs` targets |
| `scheduler/daily.py` | Daily pulse orchestrator | Step counter fixed; structured logging per step; result saved to `outputs/daily_YYYY-MM-DD.json`; also writes to Postgres after data pull (PR-ADS-024) |
| `scripts/validate_phase1.py` | Phase 1 read-only validation | Syntax, YAML, docs, and stale-reference checks |
| `scripts/phase1_readiness.py` | Phase 1 production readiness audit | Updated in PR-ADS-021: `ANTHROPIC_API_KEY` removed from required list; `APP_SECRET_KEY`, `AUTH_USERS_JSON` added; `api/server.py` removed from forbidden modules (was stale entry); deterministic advisor check added |
| `scripts/create_user_hash.py` | Password hash generator | **NEW in PR-ADS-021** — generates PBKDF2-SHA256 password hash for `AUTH_USERS_JSON`; never prints password |
| `docs/PHASE1_PRODUCTION_READINESS.md` | Go/no-go checklist | Official Phase 1 production readiness gate |
| `docs/05_DATA_REFERENCE.md` | Data reference | ✅ Windsor MCP search-term response shape documented (PR-ADS-063) |
| `.env.example` | Environment variable reference | Updated in PR-ADS-021: `ADVISOR_MODE`, `APP_SECRET_KEY`, `AUTH_USERS_JSON` added; Claude moved to optional |
| `requirements.txt` | Python dependencies | Added psycopg2-binary (PR-ADS-024) |
| `api/__init__.py` | API package declaration | Declares `api/` as a Python package |
| `api/auth.py` | Internal auth module | Updated in PR-ADS-021B: `authenticate_user()` added — supports both `password_hash` (PBKDF2) and `password` (plain-text fallback via `hmac.compare_digest`); passwords never logged or exposed in API responses |
| `api/server.py` | FastAPI web entry point | PR-ADS-025E-FIX: api_summary() uses MAX(run_id) join instead of MAX(run_date) — eliminates same-day double-count. PR-ADS-025E: api_summary() spend query fixed — reads latest run only (not cumulative SUM across all runs). Corrects $62k→~$10k overcount. Updated in PR-ADS-021B: `/auth/login` now uses `authenticate_user()` for dual-mode credential verification; Updated in PR-ADS-024: DB init in lifespan + 6 new `/api/*` endpoints with `?days=` param; Updated in PR-ADS-025A: `/api/campaigns` PERCENTILE_CONT removed, flat aggregate query, trend hardcoded to stable; exc_info=True added to all /api/* except blocks; Updated in PR-ADS-025C: `/api/leads` SELECT includes source_type; Updated in PR-ADS-025D: `/api/campaigns` GroupingError fixed — correlated subquery replaced with DISTINCT ON CTE |
| `api/scheduler.py` | In-app APScheduler | Schedules daily (06:00), weekly (Mon 07:00), monthly (1st 08:00) Phase 1 jobs in Asia/Amman timezone; exposes shared lock state and `get_scheduler_status()` |
| `db/__init__.py` | DB package | New in PR-ADS-024 |
| `db/schema.py` | PostgreSQL schema + init_db() | PR-ADS-025F-FIX: TRUNCATE guard uses INSERT ON CONFLICT DO NOTHING + IF FOUND — race-safe. PR-ADS-025F: migrations table added; one-time TRUNCATE campaigns guard (runs once via migration flag); DELETE junk campaign rows (idempotent). PR-ADS-025E-FIX: idx_leads_campaign_name added; authoritative-source comment on backfill UPDATEs. PR-ADS-025E: Backfill UPDATEs for 5 Windsor→canonical campaign name variants in both campaigns and leads tables. Idempotent. PR-ADS-025C: leads table gains source_type VARCHAR(30) column + index; ALTER TABLE IF NOT EXISTS ensures idempotent migration on startup. CREATE TABLE IF NOT EXISTS; idempotent; non-fatal. New in PR-ADS-024 |
| `db/connection.py` | Connection pool, non-fatal if unavailable | ThreadedConnectionPool max 10; DATABASE_URL from env; yields None if unavailable. New in PR-ADS-024 |
| `db/writers.py` | Write runs, campaigns, leads, waste, deals | PR-ADS-025F: write_campaigns() now receives pre-merged rows — one per campaign. PR-ADS-025E-FIX: str() guard + empty check in write_campaigns() name normalisation. PR-ADS-025E: _canonicalise_campaign_name() added — Windsor→canonical name map; write_campaigns() now computes and stores cpql_usd at write time. PR-ADS-025C: write_leads() fixed to unpack HubSpot properties dict; _clean_campaign_name() normalises to lowercase, filters pseudo-names; _map_source_type() maps hs_analytics_source to closed source_type enum; write_campaigns() normalises campaign_name to lowercase. New in PR-ADS-024 |
| `static/index.html` | Dashboard UI  | PR-ADS-025B: time range selector bar added to main content header |
| `static/app.js`     | Frontend logic | PR-ADS-025B: full rewrite — markdown parser removed, all pages read from /api/* DB endpoints, time range selector (7d/14d/30d/60d) added |
| `static/styles.css` | Dashboard styles | PR-ADS-025B: .time-range-bar, .time-range-btn styles appended |
| `scripts/verify_live_deployment.py` | Live deployment verifier | Updated in PR-ADS-021: checks `/health` is public; checks protected endpoints return 401 when unauthenticated; optional login test via `TEST_USERNAME`/`TEST_PASSWORD` |
| `docs/API_CONTRACT.md` | API endpoint contract | PR-ADS-025C: /api/leads response/example updated to include source_type. Valid source_type enum remains documented here in repo state: paid_search, organic_search, referral, direct, email, other. Single source of truth for every endpoint in api/server.py |
| `docs/23_PRODUCTION_REALITY_AND_UX_AUDIT.md` | Production reality map and UX navigation diagnosis | PR-ADS-064: Full audit of every sidebar page, endpoint, DB table, scheduler, and data pipeline. Search Terms pipeline investigation. UX restructure recommendation. |
| `scripts/audit_production_reality.py` | Read-only diagnostic script for production data trust | PR-ADS-064: Checks row counts, freshness, verdicts per dataset. Flags FRESH_BUT_EMPTY. No external API calls, no mutations. |
| `scripts/verify_search_terms_pipeline.py` | Search Terms pipeline verification script | PR-ADS-065: Focused verification of the full Search Terms chain (Windsor → file → DB → API). Supports --db-only, --pull-live, --api-url modes. Produces pipeline verdicts. |
| `tests/test_production_reality_audit.py` | Unit tests for audit script verdict logic | PR-ADS-064: Tests compute_verdict() and compute_pipeline_blockers() pure functions |
| `tests/test_search_terms_pipeline_verifier.py` | Unit tests for Search Terms verifier | PR-ADS-065: Tests compute_search_terms_verdict() pure function for all verdict cases |

**Phase 1 state:** Read-only. Deterministic advisor active. Internal auth active. Claude API optional.

---

## Current Architecture Snapshot

As of PR-ADS-063 audit, the active architecture is:

- `connectors/` fetches data only.
- `analysis/` performs local read-only analysis only.
- `scheduler/` orchestrates daily, weekly, and monthly runs.
- `api/` exposes FastAPI endpoints and admin-gated run triggers.
- `static/` renders the dashboard UI.
- `config/` owns thresholds, junk patterns, and N-Gram stopwords.
- `db/` stores local application data only.
- No Google Ads or HubSpot write path exists in Phase 1.

---

## Built but Broken (do not call until fixed)

All previously broken references in `scheduler/daily.py` were fixed in PR-ADS-013.
No files are currently in a broken state.

---

## Missing (referenced in code or docs, not yet built)

| Missing item | Referenced in | Notes |
|--------------|---------------|-------|
| `connectors/oct_uploader.py` | `README.md`, `.env.example`, `CLAUDE_CODE_BRIEFING.md`, `docs/` | Phase 2 — do not build until Phase 1 validated |
| `config/logistaas_config.yaml` | `connectors/gclid_match.py`, `CLAUDE_CODE_BRIEFING.md`, `docs/GITHUB_AGENT_BRIEFING.md` | `gclid_match.py` has a fallback (`min_gclid_coverage_pct` defaults to 70); tracked in PR-ADS-005 |
| `data/ads_campaigns_7d.json` | `scheduler/daily.py:detect_anomalies()` | Read for anomaly baseline; not created by any connector; anomaly detection silently skips if absent |

---

## Missing `__init__.py` files (package declarations)

| Directory | Effect |
|-----------|--------|
| `scheduler/` | No `__init__.py`; module imports work when run as `python -m scheduler.weekly` but may fail in some import contexts |
| `analysis/` | No `__init__.py`; same risk |

---

## Current PR Index (from PR-ADS-015)

| PR | Description | Status |
|----|-------------|--------|
| PR-ADS-012 | Repository reality sync + docs | ✅ Complete |
| PR-ADS-013 | Broken reference fix (`scheduler/daily.py`, `config/patterns.yaml`) | ✅ Complete |
| PR-ADS-014 | Phase 1 Operational Readiness Pack (healthcheck, validate, Makefile, daily hardening, docs) | ✅ Complete |
| PR-ADS-015 | Phase 1 Production Readiness Audit (readiness script, go/no-go checklist, Makefile target, docs) | ✅ Complete |
| PR-ADS-016 | Single Web Service Foundation (`api/server.py`, FastAPI, Render web service) | ✅ Complete |
| PR-ADS-017 | Protected Manual Run Endpoints (`POST /run/daily`, `/run/weekly`, `/run/monthly`) | ✅ Complete |
| PR-ADS-018 | Modern UI Dashboard Foundation (`static/index.html`, `app.js`, `styles.css`, `GET /`) | ✅ Complete |
| PR-ADS-019 | In-App Scheduler + Render Cron Decommission (`api/scheduler.py`, `GET /scheduler/status`, single-service `render.yaml`) | ✅ Complete |
| PR-ADS-021 | Deterministic Advisor + Internal User Permissions (PBKDF2 auth, roles, login UI) | ✅ Complete |
| PR-ADS-021B | Plain-text password fallback hotfix — `authenticate_user()` dual-mode, unblocks login | ✅ Complete |
| PR-ADS-022  | Premium Dashboard Visual Upgrade — CSS/HTML/JS polish, no backend changes | ✅ This PR |
| PR-ADS-023 | Brand-Aligned Dashboard Rebuild — 5-page SPA, Sora font, full API wiring | ✅ Complete |
| PR-ADS-027 | Fix HubSpot associations_api crash + Windsor search term 400 error | ✅ Complete |
| PR-ADS-024 | PostgreSQL Foundation — schema, writers, time-range API | ✅ Complete |
| PR-ADS-025A | Fix /api/campaigns query crash — PERCENTILE_CONT removed, exc_info=True logging | ✅ Complete |
| PR-ADS-025C | Data quality fix — campaign normalisation, lead property mapping, source_type tracking | ✅ Complete |
| PR-ADS-025D | Fix /api/campaigns GroupingError — DISTINCT ON CTE replaces correlated subquery | ✅ Complete |
| PR-ADS-025E | Writer integrity — CPQL write fix, summary spend deduplication, campaign name canonicalisation | 🔄 In Progress |
| PR-ADS-025E-FIX | Copilot fixes — run_id dedup, str() guard, leads index, canonical map comment | 🔄 In Progress |
| PR-ADS-025F | Campaign truth table merge — Windsor spend + HubSpot SQLs unified per run, junk filter, one-time DB cleanup | 🔄 In Progress |
| PR-ADS-025F-FIX | Copilot fixes — lq aggregate, legacy key shim, race-safe TRUNCATE | 🔄 In Progress |
| PR-ADS-025B | Dashboard live data — markdown parser removed, /api/* DB calls, time range selector | 🔄 In Progress |
| PR-ADS-064 | Full Production Reality Audit & UX Navigation Diagnosis — audit doc, diagnostic script, admin endpoint | 🔨 In Progress |
| PR-ADS-065 | Search Terms Pipeline Verification & Repair — verifier script, connector/writer/scheduler hardening | 🔨 In Progress |
| PR-ADS-066 | Search Terms Production Verdict Panel & Windsor Source-Parity Resolution — verdict endpoint, UI panel, MCP import | ✅ Complete |
| PR-ADS-067 | Canonical Freshness Semantics & Zero-Row Truth States — `services/freshness_service.py`, canonical endpoint enrichment, UI truth labels | 🔨 In Progress |
| PR-ADS-068 | System Status War Room & Pipeline Dependency Map — `services/system_status_service.py`, war room endpoint, UI war room section | 🔨 In Progress |
| **Next state** | **4-week Phase 1 live validation period** | 🟢 Next |
| PR-ADS-005 | Config hardening — create `config/logistaas_config.yaml`, validate all YAML keys | ⬜ Post-validation |

> **Phase 2 / OCT is blocked** until the 4-week Phase 1 validation period is complete and Youssef approves Phase 2.

---

## What Is Intentionally Not Built (Phase 2+)

> These items are **deferred by design**, not missing or broken.
> OCT and negative push require Phase 1 to be proven accurate before activation.

- `connectors/oct_uploader.py` — Phase 2 gate, requires Phase 1 validated
- `connectors/negative_pusher.py` — Phase 3
- Manual run API endpoints — built in PR-ADS-017 (requires `ADMIN_API_TOKEN`)
- Frontend dashboard — built in PR-ADS-018
- In-app scheduler — built in PR-ADS-019 (APScheduler, runs inside web service process)
- Meta Ads connector — Phase 4

- PR-ADS-069: Sidebar UX Grouping & Page Rename — four sidebar groups, 9 label renames, route stability rule, navigation doc ✅ Complete

---

## Follow-up — 2026-08-16 (PR-ADS-153E-A2, Canonical Revenue Cutover Gate Hardening)

Hardening only. No production consumer switched, no `static/` file touched, no
visible KPI changed, no legacy table dropped, no external write path added.

**The defect.** PR-ADS-153E-A's gate proved the canonical ledger was internally
consistent and reconciled against both legacy lineages. It never proved the
ledger was COMPLETE. `_check_invariants` accepted a successful `fetch_sync_state`
that returned `row=None`, never required `bootstrap_status == "complete"`, and
`bootstrap_started_at` / `bootstrap_completed_at` — columns that had existed
since 153E-A — were never written by `record_sync_state` at all. A portal whose
historical bootstrap had never run therefore passed: one nightly incremental
over the last 24 hours reports `success`, reconciles perfectly against the same
24 hours of legacy rows, and returns `ok: true`. That was the signal 153E-B was
going to read as permission to repoint the executive revenue and customer
totals at a ledger holding one day of history.

**What changed.**

* `db/deal_ledger_repository.py` — `record_sync_state` takes a required
  `sync_mode`; bootstrap and incremental runs write different columns; a
  completed bootstrap is never downgraded; the first start and completion
  timestamps survive retries; only a run that PROVED end-of-results completes
  the bootstrap.
* `services/hubspot_deal_sync_service.py` — every state write on every exit
  path names its mode, including the two early pull-failure returns.
* `services/revenue_reconciliation_service.py` — coverage is a hard invariant,
  and every violation carries a stable code.
* `scripts/backfill_canonical_deal_ledger.py` — new operator CLI.
* `scripts/audit_canonical_revenue_truth.py` — `--all-windows`.

**Two follow-up blockers, fixed in review:**

* the backfill CLI reported success whenever the durable row already said
  `complete`, even when the current run had failed. `bootstrap_status` is
  monotonic by design, so it answered "has a bootstrap ever worked?" rather than
  "did this one?". It now requires proof from the current execution AND agreement
  from the durable state;
* `last_status` and `last_error` are shared between sync modes, so a bootstrap
  rerun's `success` could validate a FAILED incremental's timestamp. A durable
  `last_sync_mode` column (additive, idempotent migration, NULL fails closed)
  now records which mode wrote them, and the audit requires the latest sync to
  have been an incremental.

**Status: shadow mode, unchanged.** 153E-B remains blocked until the production
procedure in `docs/35_CANONICAL_REVENUE_LEDGER.md` §11 returns aggregate
`ok: true`. Six-month read-only governance active. Phase 2 / OCT blocked.


---

## Follow-up — 2026-08-17 (PR-ADS-153E-B, Canonical Revenue Consumer Cutover)

**Status: shadow mode is over.** Every production revenue consumer now reads the
canonical deal ledger through one shared contract. No legacy table was dropped,
no external write path was added, and Phase 2 / OCT remains blocked.

**The defect.** Three revenue lineages each called their output "closed-won
revenue": `gclid_attribution` (GCLID-bearing deals only, keyed on an attribution
hash), `deal_source_attribution` (all closed-won deals, no currency contract) and
a local Windsor/JSON chain (Unit Economics). The same quarter produced different
customer counts and different revenue on different pages, and nothing in the
product said which population any number described. In production the size of
the error is concrete: 124 of 180 won deals have no GCLID, so a dashboard
sourcing "total revenue" from GCLID evidence was showing about a third of the
business.

**What changed.**

* `analysis/revenue_scope.py` — the explicit scope lattice. Membership is nested
  by construction, so the ordering is a property of the code rather than a
  convention callers are asked to respect.
* `services/canonical_revenue_service.py` — one read contract. Consumers no
  longer re-derive won status, deal identity, the revenue event date, the
  revenue value, currency safety, window bounds or the population.
* `services/revenue_reconciliation_service.check_sync_coverage` — extracted from
  `_check_invariants` so the merge audit and the production read path apply ONE
  implementation of the readiness rule. A page can no longer render revenue from
  a ledger the gate would have rejected.
* `db/deal_ledger_repository.fetch_won_deals` / `fetch_won_state_counts` — the
  single production SQL read (`hs_is_closed_won IS TRUE`, EXCLUSIVE upper bound)
  plus a separate count of deals whose won state is UNKNOWN.
* Eleven consumer modules migrated; Unit Economics moved to business windows and
  off Windsor entirely.
* Two honest renames: legacy `company` (the associated contact's employer)
  becomes `deal_name`, and `match_status`/`match_source` become
  `attribution_scope`.
* A CI contract guard (`tests/test_pr_ads_153e_b_consumer_cutover.py`) fails the
  build if a migrated module calls a retired legacy revenue provider or names a
  legacy revenue table in SQL. The guard is AST-based and is itself tested
  against a synthetic regression, in both directions.

**Fail-closed.** An unreadable or coverage-unproven ledger yields an explicit
unavailable response — reason, violation codes, scope, freshness — with counts
`null` rather than `0`. There is no fallback to any legacy lineage, because each
holds a different population and a fallback would silently redefine "revenue"
mid-incident.

Rollback is a code deployment rollback only. Canonical records, sync state,
reconciliation evidence and legacy comparison history are all preserved.

---

## PR-ADS-153F — Canonical country geography (August 2026)

Country truth had no owner. Nothing scheduled `run_google_ads_geo_sync` — its
only caller was an admin endpoint a human had to click — so canonical geo went
stale the moment the window advanced, and with no coverage ledger, no resume and
no freshness entry, that staleness was invisible on every health surface. The
ROAS by Country gate was blocking correctly for a reason nothing could report.

**Built**

* `analysis/country_identity.py` — THE country identity contract. One registry
  drives both name→code and code→label, so the eleven codes that resolved
  forward but had no label are unrepresentable. A two-letter token is no longer
  a country code. Unidentifiable geography resolves to an explicit residual with
  a status and reason, never to a guess and never to nothing.
* `services/dataset_keys.py` — the one `(source, dataset)` registry that writers
  and freshness configuration both import.
* `google_ads_geo_coverage` + `google_ads_geo_sync_state` — additive per-chunk
  coverage, per-range failure evidence, a resume checkpoint, and a durable
  cross-instance run lease.

**Changed**

* The daily incremental sync runs canonical spend → FX → geo → reconciliation,
  in that order, and the manual recovery trigger is preserved.
* A chunk is `verified` only after its rows land; a `failed` write never demotes
  a verified chunk; completeness is re-read from the ledger rather than inferred
  from a run's own counters, so a partial run cannot publish complete coverage or
  healthy freshness.
* Dashboard Countries, ROAS by Country, the country drilldown, the mart, geo
  reconciliation and Revenue Health all join on ONE canonical key. Blank, invalid
  and unresolved geography lands in one residual carrying BOTH the Google Ads
  spend shortfall and the unidentifiable revenue, so known rows plus residual
  reconcile on both sides.
* One `country_geo_ready` predicate replaces three readiness bars; the mart's
  page-difference audit no longer applies a stricter bar than the pages it
  audits.
* `canonical_spend`'s freshness key corrected to what its writer stamps (the
  PR-ADS-153A §1.7 finding). Three phantom freshness datasets removed (`ngrams`,
  `historical_intelligence`, `mailchimp_attribution` — no table, no writer); two
  real ones connected to real sync batches (`waste_terms`,
  `gclid_coverage_snapshots`); `api/server.py`'s hand-listed dataset registry is
  now derived.

**Unchanged, deliberately.** `SPEND_VARIANCE_TOLERANCE`, business-window
definitions, FX doctrine, revenue scope definitions, won-deal doctrine and the
PR-ADS-131 safe-residual eligibility rules. The gate was never the defect;
loosening it would have hidden the missing owner rather than fixing it.

**Not in this PR.** No legacy table or route deletion (PR-ADS-153G), no OCT, no
external mutation of any kind. Rollback is a code deployment rollback only —
geo rows, coverage state, failure history, checkpoints and reconciliation
evidence are all retained.

Full doctrine: `docs/36_CANONICAL_COUNTRY_GEOGRAPHY.md`.


## PR-ADS-160 — Prospective SQL coverage boundary (September 2026)

**The historical question is closed by exhaustion.** PR-ADS-159's production
validation read every candidate: 1,261 contacts whose lifecycle stage proves
they reached SQL, 728 with HubSpot's direct property, **0** recoverable from
lifecycle history, **533** with no provable timestamp. All 533 returned valid
HubSpot history and none of it held a transition into `salesqualifiedlead`.
Those dates are absent from HubSpot, not missing from us. **They are never
invented, here or anywhere.**

**What this PR adds instead.** An immutable, local, provenance-carrying
*boundary*: an upper bound recording that a contact had **already** reached SQL
by an observed instant. It can only disprove membership — an event known to be
over before a window opened cannot have happened inside it — and it is never a
date. It lives in its own tables under `known_reached_sql_by`, and a monitoring
gate fails if it is ever read as an event date or appears in a stage-entry
column.

**The defect it also fixes, proven against real PostgreSQL.** The contact-funnel
upsert refreshed every column from the incoming payload under a *staleness*
guard only. A later payload that omitted `hs_v2_date_entered_salesqualifiedlead`
therefore blanked a stored date and reported `{'ok': True, 'persisted': 1}` —
destroying the only evidence a contact entered SQL, and the evidence that
anything had been lost. Stage-entry columns are now refreshed only from a
present value; lifecycle stage and status deliberately keep latest-state
semantics, because for those a cleared value is a real fact.

**New in this PR.**

| Path | Purpose |
| --- | --- |
| `services/sql_coverage_boundary_service.py` | boundary creation and post-boundary gap detection; no HubSpot write path |
| `scripts/establish_sql_coverage_boundary.py` | dry-run-by-default CLI; local `--apply`; atomic; idempotent |
| `scripts/audit_sql_coverage_gate.py` | read-only gate on the six guarantees; 0 holds / 1 broken / 2 blind |
| `sql_coverage_boundary`, `sql_coverage_boundary_contact`, `sql_post_boundary_incident` | the boundary, its bounds, and prospective gaps |
| `hubspot/sql_coverage_gaps` | scheduler dataset; a new gap is an **error** on the run, not a log line |

**Unchanged, deliberately.** The SQL definition, the two permitted evidence
sources, the creation-time lower-bound rule, and every canonical read path. All
44 window/scope combinations still reconcile.

**Not in this PR.** No consumer migration — the 25 legacy SQL consumers remain
for **PR-ADS-161**. No production apply run. No HubSpot write. No complete
historical SQL total, and no CPQL where coverage is incomplete. All Time stays
incomplete permanently.

**Review corrections (§1–§7).** Seven truth-contract blockers were found and
fixed before merge:

* **the classifier was wrong.** Selecting prospective contacts on
  `created_at >= boundary OR effective_date >= boundary` missed the central
  failure mode — an old contact, below SQL when the snapshot was taken, promoted
  afterwards with no timestamp. Both predicates are false for it. The immutable
  snapshot is now the classifier, by anti-join; creation date classifies nothing.
* **the boundary instant was the operator's to choose.** `--observed-at` is gone.
  The instant is stamped database-side with `clock_timestamp()` inside the same
  transaction that reads the population, and strictly after that read.
* **the boundary was replaceable.** A partial unique index enforces exactly one
  completed boundary, and triggers make it and its bounded contacts immutable.
  An identical replay verifies; any other replay is refused unchanged.
* **writes went unchecked.** Every resolution result is inspected, counts come
  from the database rather than `len(requested)`, and an unreadable final
  verification read fails the run instead of returning `ok: true` with a null.
* **freshness was promised but not implemented.** One contract
  (`analysis/sql_coverage_freshness.py`) shared by the audit and the gate;
  stale, failed, incomplete-bootstrap and unavailable all block certification.
* **incidents blocked globally.** They are now resolved per window against the
  same two sound bounds — creation as a lower bound, detection as an upper one.
* **boundary exclusion was inclusive.** `known_reached_sql_by < window_start`,
  strictly: at equality the transition could have occurred at the window's
  inclusive first instant.

**Second review (§1–§5).** Five further blockers were found and fixed:

* **the scheduler dataset had no status.** `_overall_status()` treats an absent
  status as a failure — correctly — so every real sync reported `partial`,
  including a flawless prospective check. The wrapper now maps the service's
  outcome onto the scheduler's vocabulary: `skipped` (no boundary yet, with an
  explicit reason, non-voting), `success` (verified clean), `failed`
  (unverifiable, or gaps/incidents open). No path returns `success` beside a
  populated `errors` list.
* **an incomplete total could still publish.** Completeness was only the
  *historical* half, so a window with an open prospective gap published a total
  provably missing rows, and a CPQL from it. The halves are now separate
  (`historical_membership_complete`, `prospective_membership_complete`),
  `complete_sql_total` is null unless both hold, and `cpql_publishable`
  additionally requires certification — which, when it blocks, takes the total
  back rather than annotating it. The confirmed dated subset stays visible under
  `confirmed_sql_subset`, never as "the total".
* **a bootstrap could masquerade as a fresh feed.** The contact-funnel sync
  stamps `last_incremental_at` on bootstrap runs too, so a completed backfill
  read as a live incremental pipeline. Freshness now reads
  `last_successful_incremental_at` and `last_incremental_status` — which no
  bootstrap overwrites, so a later bootstrap cannot erase evidence that the
  required incremental failed. The migration is additive and legacy rows fail
  closed until one real incremental sync records the evidence.
* **six repository functions were defined twice**, the second silently
  overriding the first. Removed, with an AST guard (and its own negative
  control) over every module this PR touches.
* **boundary provenance was not bound to the snapshot.** It was read before the
  writer's transaction, so a sync landing in between made `source_run_id`
  describe an older state than the population snapshotted. It is now read inside
  that transaction, immediately after the population, and its absence **refuses**
  establishment rather than recording a best-effort NULL.

**Third review (§1–§3).** Three further blockers were found and fixed:

* **one transaction was not one snapshot.** Under the connection's default READ
  COMMITTED isolation PostgreSQL takes a fresh snapshot at the start of *every*
  statement, so a contact-funnel sync committing between the population SELECT
  and the provenance SELECT was invisible to the first and visible to the
  second — the mixed boundary the previous fix aimed at, one layer further down.
  The transaction now sets `REPEATABLE READ` before its first query. Proven with
  two concurrent connections, the establishing transaction paused at an exact
  statement boundary, and a READ COMMITTED negative control in which the mix
  duly appears.
* **a truncated contact-funnel run reported success.** `run_status` was computed
  correctly as `partial` and then discarded: both sync batches were finished as
  `success`, the returned dict hardcoded `"status": "success"`, and
  `last_source_date` advanced past data the scan never read. The run's own
  verdict is now what is written down and returned, the coverage watermark stays
  put, and the scheduler run goes non-green with an error that says why.
* **the audit summary ignored its own final gate.** `complete_sql_total_publishable`
  and `cpql_publishable` were `coverage_complete` — a membership verdict —
  published under two more names, so the summary could announce a publishable
  CPQL while `certification` beneath it reported zero certified windows. Both are
  now derived from what survived certification, `coverage_complete` remains as
  the separate membership-only question, and the audit raises its own violation
  if a summary ever publishes with nothing certified.

**Fourth review.** One blocker: **a partial run was still persisted as a
success.** The scheduler's summary and CLI exit code were already truthful, but
the write to the `runs` table read `"success" if overall_status in ("success",
"partial")` — and the `runs` table is what production reads. `/api/runs`, the
"Latest recorded run" banner, per-page run metadata and Data Runs all consume
it, so a truncated contact-funnel sync left every one of those surfaces showing
a clean run over an unfinished contact population. The exact status is now
persisted, and the three outcomes stay distinct all the way to the screen:
`api/monitoring.py` no longer lets a partial advance `last_success_at` (the
proven-complete coverage claim staleness is measured against) and no longer
reports green while the latest run is partial, while still not counting it as a
failure; the banner and the per-page strip render it as a warning reading
"Latest run partial — some datasets were incomplete" rather than as Fresh.

**Fifth review.** One blocker: **canonical dataset freshness still turned
`partial` green.** `compute_canonical_freshness` branched on `running` and on
`failed` but had no branch for `partial`, so a recent, populated dataset whose
sync stopped short fell through to the staleness test and reported
`fresh_with_data` at ok severity — the last surface left after the fourth
review. Two states close it, mirroring the two `failed` states:
`data_available_latest_sync_partial` (rows exist, population incomplete —
warning, in `HAS_DATA_STATES`, non-blocking) and `partial_no_data` (nothing
arrived and the window is **not proven empty** — error, blocking). An unmeasured
row count stays unmeasured, with the partial fact carried into the reason rather
than an emptiness nobody looked for.

**PR-ADS-160-F1 — Incremental Monitoring Identity & Partial-State Truth.** A
post-merge defect in PR-ADS-160: the monitoring severity path never saw the runs
it was written for. `compute_monitoring_status` grouped by matching the cadence
names (`daily`/`weekly`/`monthly`) against `runs.run_type`, while the scheduler
persists `daily_incremental_sync` — so every real incremental row was discarded
before any partial-run logic ran, and the daily bucket reported "No daily run
found in history" while daily runs were happening. The PR-ADS-160 test missed it
by rewriting `run_type` to `"daily"` before calling monitoring.

`api/monitoring.py` now carries one explicit `RUN_TYPE_CADENCE` table and a
`monitoring_cadence()` helper; the durable run type is unchanged and unknown run
types are not monitored rather than silently daily. The test passes the
persisted value through, with a control replaying the pre-fix grouping.

The same patch settled a precedence question PR-ADS-160 left open: a dataset
with its OWN failed or partial sync had that replaced by
`blocked_by_dependency` inherited from an upstream one. Direct evidence now
outranks inherited evidence — the dependency is named in the reason rather than
dropped — applied uniformly across all three configured dependency pairs.

Full doctrine: `docs/41_PROSPECTIVE_SQL_COVERAGE_BOUNDARY.md`.


## PR-ADS-160-F2 — Monitoring cadence identity and partial-state truth (September 2026)

Found by an independent readiness audit before production validation, not by
CI. Every fix here was shown failing against the pre-fix code.

**A cadence was a name two pipelines shared, and the healthier one won the
verdict.** PR-ADS-160-F1 correctly stopped `compute_monitoring_status`
discarding real `daily_incremental_sync` rows — then routed them into the same
cadence as the legacy 06:00 pulse. Monitoring computes one failure streak, one
`last_success_at` and one severity per cadence, so the 06:00 pulse broke the
09:00 incremental sync's failure streak every morning (red needs 2 consecutive;
it could never exceed 1) and advanced the clock its staleness is measured
against. Measured on the real function: five days of incremental-sync failures
behind a healthy pulse reported `severity: green, warnings: []`, and
`static/app.js` renders nothing on green. No test had ever placed a `daily` row
and a `daily_incremental_sync` row in the same list — the population production
emits was untested. A cadence is now one pipeline; `daily_incremental_sync` has
its own bucket, its own threshold in `config/thresholds.yaml`, and its own
verdict. `api/server.py:_load_monitoring_thresholds` iterates
`MONITORING_CADENCES` rather than a literal triple, and the unchosen 2-day
fallback is gone: a cadence with no configured threshold is warned about by
name instead of being measured against a number nobody picked.

**Direct evidence reported LESS than the inherited state it displaced.** The
PR-ADS-160-F1 §4 precedence rule promised the dependency would be named in the
reason rather than dropped. One of five returns in
`services/freshness_service.py` — partial sync with an unmeasured row count —
omitted `dependency_note` entirely and fell from `blocked_by_dependency` /
`error` to `unknown_row_count` / `neutral`, which is not in `BLOCKING_STATES`,
so the cascade was lost too. Reachable whenever the row-count `SELECT` raises,
which is exactly when a blocking error matters most. Both returns now carry the
dependency, and `_result` takes a severity override that floors the verdict at
`error` when it displaces a blocking one.

**`partial` was collapsed into `failed` on two canonical sync batches.**
`scheduler/incremental_sync.py` did this for the deal ledger and canonical geo
under a comment asserting `sync_batches` accepts success|failed only — a
premise PR-ADS-160 itself had made false by adding `partial` to
`VALID_SYNC_STATUSES` and to `finish_sync_batch`. `sync_state.status` is what
`compute_canonical_freshness` reads, so a truncated deal-ledger sync rendered
as "Latest sync failed" on the canonical **revenue** population: retry, when
the remedy is resume. Both sites pass the exact status through; the writer
already withholds `last_source_date` for anything but success.

**A daily pulse that failed during its pulls wrote no `runs` row at all.**
`scheduler/daily.py` inserted the record after both pulls, so the failure path
called `update_run(None, ...)`, which returns immediately. This is the likely
origin of the "No daily run found in history" symptom F1 diagnosed, and the
reason the masking above was dormant rather than active — it would have
switched on the day the pulse started succeeding. The insert now happens
immediately after `start_run`, before any connector import.

Not fixed here, and recorded so they are not lost: `last_completed_at` has no
UI reader (nor does any other `latest_runs` field — the banner renders
`warnings` only); `/api/monitoring/status` still falls back to
`runtime_logs/run_history.jsonl`, which never contains `daily_incremental_sync`,
when a reachable database returns zero runs in 90 days; and
`analysis/lifecycle_sql_coverage.py` still returns `cpql_publishable`
pre-certification, retracted only by the CLI audit — a landmine for
PR-ADS-161's consumer migration, though no product surface imports it today.

Full doctrine: `docs/42_MONITORING_CADENCE_AND_PARTIAL_TRUTH.md`.


## PR-ADS-161A-1 — Canonical SQL publication contract (September 2026)

**The structural prerequisite to the PR-ADS-161 consumer cutover. No consumer
is migrated here.**

`analysis/lifecycle_sql_coverage.py::window_coverage` answers a NECESSARY
question — could a complete total exist for this window — and sets
`cpql_publishable` and `complete_sql_total` from that alone. The final gate
(canonical reader reconciliation, boundary readable, incident store readable,
source proven fresh) lived only in `scripts/audit_lifecycle_sql_coverage.py`,
coupled to a CLI `Findings` object and unreachable from production. So the only
publication flag a product surface could import was the intermediate one, which
is TRUE for windows the audit refuses to certify. Nothing read it yet; the first
executive consumer to reach for `window_coverage()` would have published a
total the audit withholds.

`analysis/sql_publication.py` is now the single decision. It takes every gate
at once and fails closed on each — `None` withholds exactly as `False` does,
because an outage must never certify a window. Three outcomes stay distinct to
the caller: `published`, `withheld` (we looked and the evidence does not
support a total) and `unavailable` (we could not look).
`complete_sql_total` is `None` in the latter two, never `0`.
`confirmed_sql_subset` is always present under a name that cannot be mistaken
for a total. `scripts/audit_lifecycle_sql_coverage.py::audit_certification`
now DELEGATES to it rather than carrying a second copy — all 141 cases in the
PR-ADS-160 suite pass unchanged, including the PostgreSQL-backed ones, which is
the evidence the refactor changed no behaviour.

**Reader reconciliation is recorded, not recomputed per request.** Proving it
reads the entire funnel table across 44 window/scope combinations —
unaffordable on a dashboard request. `scripts/record_sql_reader_reconciliation.py
--apply` runs the audit's own comparison function and writes the outcome to the
new append-only `sql_reader_reconciliation` table; the contract reads the newest
row with a 36-hour maximum age. No row, a stale row, or a recorded disagreement
all withhold, and they are reported as three different reasons because the
remedy differs. The recorder is a separate command because the coverage audit
is read-only by contract and is run under a session-level `SET TRANSACTION READ
ONLY` guard during production validation.

**Nothing visible changes today.** No consumer reads the contract, and the
boundary is `2026-09-21 04:34:37`, so every product window currently straddles
or precedes it and would be withheld on the window-local gate alone. The 7d
window becomes fully post-boundary on 2026-09-28, which is when the recorded
reconciliation starts deciding anything — the recorder needs a scheduled home
before then, and this PR does not give it one.

**Corrected after review.** The first cut of this PR carried four defects that
its own truth audit found, recorded here because three of them were defects in
the GUARDS rather than the code: `publication_inputs` read `incidents` from a
repository that returns `rows`, so the post-boundary gap gate was dead on
arrival and one natural migration spelling would have turned a real open gap
into a certified zero; `test_15` re-implemented a weaker predicate instead of
calling the detector `test_14` uses, and both stayed green with that detector
deliberately gutted; the recorder's `bool()` coercion defeated the writer's
own guard and could write an unproven run as a recorded disagreement; and the
claim that the refactor changed no audit behaviour was false — two reason
strings change (stale source now reports the freshness reason, an unreadable
contact store now reports `coverage_verdict_absent`), and the 141 green
PR-ADS-160 cases were never evidence either way because none exercises those
paths. All four are fixed, each with the control that proves it.

Guarded by `tests/test_pr_ads_161a1_sql_publication_contract.py` (58 cases):
every gate refusing in isolation with a positive control that proves the gate
can publish; the named regression driven through the real `window_coverage`
where membership is complete, certification is false and publication stays
withheld, with its negative control; and an AST guard — not a substring search
— asserting no module under `services/`, `api/`, `db/`, `scheduler/`,
`connectors/` or `analysis/` imports `lifecycle_sql_coverage` or names
`cpql_publishable`, whose negative control calls THE SAME detector the guard calls,
over seven spellings including relative imports, `importlib` and `sys.modules`.

Doctrine inventory is unchanged and says so: 25 legacy / 6 mixed / 4 canonical,
`READY_FOR_ROADMAP`, `audit_complete: true`, 0 unclassified occurrences, 0
registry problems. A migration claim the scanner does not support is not made.

Full doctrine: `docs/43_CANONICAL_SQL_PUBLICATION_CONTRACT.md`.


## PR-ADS-161A-1-F2 — the production freshness gate, and controls that can fail (September 2026)

Round 2 of the truth audit on PR-ADS-161A-1-F1. One blocker and three majors,
all of them in the half of the work whose job is to prove the other half safe.

**Production published a certified total from a source that is not fresh.**
F1 added an independent freshness gate to `audit_certification` and not to
`publication_verdict`, so the audit became the better-defended caller — and the
audit is what we point at to describe what production publishes. Measured: a
coverage dict with `certification_eligible: True` and `source_fresh: False`
published a total of 42 in production while the audit refused the identical
dict. Reachable for the same reason the missing-count case is — `publication_for`
takes `coverage` from its CALLER. `publication_verdict` now gates on
`source_fresh` directly, and `test_29` asserts audit/production parity over the
same dict rather than testing each side alone.

**`test_23` asserted an expression against itself.** The fix for the
future-dated reconciliation row (a negative age read as "not older than the
limit" grants publication forever) was guarded by a test that computed
`not (0 <= age <= max_age)` in its own body and compared it to itself. It never
called `fetch_reader_reconciliation`. Reverting the production line left all
4,596 tests green. This is round 1's `test_15` defect committed a second time,
one round after it was found. The test now drives the real reader over a real
row shape across five ages and carries the verdict through to a refusal.

**`test_22` could not distinguish the naive-timestamp guard from its absence.**
It asserted only a return value, with no database — so `get_conn()` yielded
`None` and the writer returned False whether or not the guard existed. It now
intercepts the cursor and asserts no statement was executed, with a positive
control that a tz-aware instant IS written.

**`test_20` did not guard the incident key it was credited with.** It called
`window_coverage` directly and never touched `publication_inputs`. `test_19`
now spans repository shape → service → coverage → verdict in one case, so
reverting the key fails there too.

Also: `coverage_verdict_absent` and "no counted population" had one reason
constant between them despite needing different remedies; the `bool()`
coercion round 1 flagged in the recorder was still present, F1 having added a
different guard beside it; docs/43 said 34 cases for a 64-case suite and
described its reason-delta table as exhaustive when a 1,152-case differential
finds eight deltas, six unreachable from `run()`.

Every fix is shown failing against the pre-fix line by mutation, not by
reading: reverting the future-date fix, the naive-tz guard, the incident key
or the production freshness gate each turns exactly the intended test red.

Full doctrine: `docs/43_CANONICAL_SQL_PUBLICATION_CONTRACT.md`.

## PR-ADS-161A-1-F3 — the freshness gate that was still a label (September 2026)

Round 3 of the truth audit on PR-ADS-161A-1-F2. One blocker and four majors.
Every one of them is in the freshness gate F2 added — the fix landed at the
wrong layer, under the wrong reason, in the wrong order.

**The service read freshness from the database and never gated on it.**
`publication_inputs()` performs a real read of the contact-funnel sync state
and assesses it. `publication_for()` stamped the result onto the verdict as a
label and delegated to a gate that reads `coverage["source_fresh"]` — a value
the CALLER copied in. So a caller whose coverage said fresh, over a service
that had just read stale, published:

```
value: 42, available: True, certified: True,
explanation: "...and the contact-funnel source is fresh"
source_freshness_reason: "source_stale"        <- the same object
```

while `audit_certification` refused the identical inputs. That is F2's own
blocker with one level of indirection, and it made the database read a guard
whose absence changed nothing. `withheld_payload()` then dropped the field, so
the contradiction was not even visible at the API boundary. Both signals must
now say fresh, and the payload carries the fact it was judged on.

**A freshness refusal was served under the reason `"eligible"`.** The gate
reported `coverage["certification_status"]` as its reason. On the only shape
`window_coverage` can emit with `certification_eligible: True`, that status is
literally `not_certifiable_*`'s opposite — `"eligible"`. A consumer received a
withheld total under a reason meaning "every prerequisite is met". The window's
own status is now used only when it is itself about freshness.

**"We could not look" was reported as "the source is stale".** The gate sat
ahead of the store-readability and reader-reconciliation gates, so an
`unavailable` verdict was displaced by a `withheld` one whenever the source
also happened to be stale — sending an operator to fix a pipeline when the
boundary store was unreadable or the readers had never been compared.
Refusal order is now documented and tested: window-local, then readability,
then reconciliation, then freshness, then the counted population.

**The audit's relabel rewrote every other refusal.** Keyed on `source_fresh`
alone, a pre-boundary window, an unreadable store and a reader disagreement all
reported `source_stale`. Reachable from `run()` on ordinary windows. Only a
refusal that IS about freshness may be relabelled, and the membership test is
now one shared table — `sql_publication.FRESHNESS_REFUSALS` — that the gate,
the service and the audit all consult, with `test_38` guarding it against
drifting away from `CERT_STALE_SOURCE`.

**The first fix for the blocker repeated the relabel defect one layer up.**
Short-circuiting in the service and overriding `certification_status` meant a
pre-boundary window was reported as a stale source. The audit had a test for
exactly this (`test_35`) and the service did not. The freshness signal is now
folded into the coverage the gate reads rather than short-circuited ahead of
it, so the pure layer keeps deciding the order; `test_37` is the guard.

Suite: 78 cases, up from 64. Eight counterfactuals were run — each pre-fix line
restored, the module re-run, the named test confirmed failing. The three that
matter most: the service fold, the gate ordering and the service relabel were
all invisible to the 64 cases that existed before them.

Full doctrine: `docs/43_CANONICAL_SQL_PUBLICATION_CONTRACT.md`.

## PR-ADS-161A-1-F4 — the gates that were unreachable on real inputs (September 2026)

Round 4 of the truth audit, run against the F3 commit. Two blockers and three
further findings. **Round 4's finding was not another fabricated control** —
every F3 guard does go red under a targeted mutation. The defect was subtler
and worse: several of them prove their property only on input tuples
`publication_inputs()` and `window_coverage()` cannot jointly produce, and on
the tuples production *does* produce the property was false.

**An unread boundary store was published as the claim "no coverage boundary
exists".** `publication_inputs` sets `boundary_observed_at = None` whenever
the store is unreadable, so a caller building coverage from it — the only
caller shape the service supports — gets the window-local `CERT_NO_BOUNDARY`
back, and that fired before the readability gate was ever evaluated.
`withheld_payload` drops `boundary_id` and `boundary_observed_at`, so the two
states were byte-identical at every surface. During an outage every SQL
surface would state a permanent, benign, nothing-to-do condition and an
operator would wait it out. `boundary_readable` was a parameter that changed
nothing on any input the service can actually produce.

**F3's reordering was inert on every coherent production input.** Freshness is
gated in *two* places and only the second moved. `_certification` refuses an
otherwise-perfect window with `CERT_STALE_SOURCE`, which arrives as a
window-local reason at step 1 — ahead of everything F3 put in front of the
step-4 gate. Measured with the source stale: a missing reconciliation record,
a reader disagreement and an unreadable boundary store all still reported
`not_certifiable_source_not_fresh`. Because the recorder still has no
scheduled home, no reconciliation record exists, so a stale sync would have
sent an operator to the pipeline while the refusal blocking every window went
unreported. Step 1 now defers a freshness reason to the global gates instead
of returning — conditional on `source_fresh is not True`, so the deferral
cannot itself become a fail-open.

**An eligible window with unresolved membership published the subset as a
certified complete total** — `value: 42, available: True, certified: True,
coverage_complete: False` in one object, PR-ADS-160 §2's defect verbatim. Not
emittable by `window_coverage`; reachable only from a caller-built dict, which
is the seam `publication_for` exposes and the reason `test_24` exists for the
sibling missing-count case. F3 guarded "eligible with no count" (a blank) and
left "eligible with unresolved membership" (a wrong number) open.

**F3's own fold coerced `fresh: None` to `source_fresh: False`** — turning "we
could not read the sync state" into the claim "the pipeline is stale", the
exact distinction `sql_coverage_freshness.assess` documents. And it raised
`TypeError` on a truthy non-mapping coverage where the F2 service returned
`unavailable`; `test_25` proves the pure gate survives that input but routes
nothing through the service, so the regression was invisible to it.

Also: `docs/43`'s reason-delta claim gave a count without defining its input
space, so it was not reproducible — the space is now stated (1,152 cells) and
the measured result is 307 reason deltas in 6 classes with **0**
`windows_certified` deltas; the F3 repo-state section said "76 cases existed
before" when 64 did.

Suite: 96 cases, up from 78. Eight counterfactuals run for the new guards,
each confirmed failing against the pre-fix line. Every §7 test whose subject
is a production input builds its coverage by driving the real
`window_coverage()` from an `_inputs()` dict that mirrors
`publication_inputs()`'s output and enforces both of its couplings — an
unreadable boundary store cannot also hand back a boundary instant, and an
unreadable incident store yields `None`, never `[]`. `_inputs()` mirrors that
function rather than calling it (only `test_12` and `test_19` drive the real
one), and `test_39`, `test_43`, `test_45` and `test_42`'s closing assertion
deliberately bypass it, because a source scan, the caller seam's incoherent
dicts and malformed non-mappings are states production cannot produce.

**The recurring lesson, recorded because it has now cost four rounds:** a
fixture that cannot arise from the producing code proves nothing about the
consuming code.

**Round 5 (final audit, `11605ef0`): `MERGE ASSESSMENT: no blocker found`.**
Zero cells in 19,008 where F4 publishes something F3/`main` withheld —
1,512-cell production-shaped space and 17,496-cell caller-seam space, both
loading the pre-merge modules side by side; exactly one of the 1,512
production cells publishes at all. Every load-bearing guard red under an
independent 14-mutation sweep. The four defects it did find are all
pre-existing in `main` and byte-identical under PR #185, so they gate
PR-ADS-161A-2 rather than this PR: an unread boundary store still publishes
`coverage_complete: true` on the API shape beside a real open gap
(`lifecycle_sql_coverage.py:298` reads a null boundary instant as "no
prospective period"); the step-5 self-consistency guard checks
`window_total_complete` only, so a caller-built dict contradicting
`certification_status`, `window_after_boundary` or `open_post_boundary_gaps`
still publishes a certified total; `reconciliation_gate` raises
`AttributeError` on a truthy non-mapping, which F4 hardened for `coverage`
and not for `reconciliation`/`inputs`; and a withheld payload carries a blank
`explanation` when `coverage is None` and the source is not fresh. Two claims
were corrected in place here rather than left standing: `test_41`'s docstring
asserted a pre-fix measurement that was never true, and "every §7 test builds
its coverage from the real `publication_inputs()` output" overstated both the
scope and the mechanism. `_inputs()` now enforces the incident-store coupling
as well as the boundary one (an unreadable read is `None`, never `[]`), the
case is carried as `test_41`'s sixth arm with its true measured value, and
§6's `_service_inputs` — the last impossible tuple round 4 named — delegates
to `_inputs` instead of building its own. Suite 96. See §7 of the doctrine.

Full doctrine: `docs/43_CANONICAL_SQL_PUBLICATION_CONTRACT.md`.

---

## PR-ADS-161B — Canonical marketing outcome cohorts and the Campaign Evidence migration (October 2026)

Campaign Evidence now publishes **acquisition-cohort** outcomes: contacts
**created** in the window, with their SQL outcome read from canonical lifecycle
evidence as of the canonical contact-funnel watermark. The page label is
*"SQLs from contacts created during this period, measured as of the canonical
contact-funnel watermark"*. Closed-won deals were published in rounds 1–2 and
**removed in round 3** (below); this PR publishes SQLs and CPQL only.

**Why.** Production evidence supplied with the brief (against `main` `944af0c`;
not re-read here): 668 of the 1,531 contacts whose lifecycle stage proves SQL
had no exact SQL-entry timestamp. The page withheld SQL and CPQL entirely
— gated on PR-ADS-152's legacy `leads.status_category` reconciliation — so
those proven SQLs disappeared from every campaign decision. A cohort's window
is creation, which is known, so they are counted once, in the period they were
acquired, and disclosed as lifecycle-event gaps. **No SQL-entry date is
produced or substituted** — the cohort never asks for one.

**Built.**

* `services/marketing_outcome_cohort_service.py` — the cohort contract. SQL is
  proven by a direct SQL-entry date, a recovered lifecycle transition, or a
  current stage in `stages_implying_event(EVENT_SQL)` (the repository's one
  rule). Contacts deduplicated on `contact_id`. Every SQL lands in exactly one
  of `campaign` / `unattributed_google_ads` / `excluded_non_google`, placed by
  Campaign Evidence's own `_assign_lead`. One publication verdict,
  `sql_publication()`. *(Rounds 1–2 also placed closed-won deals through the
  ledger's display-only primary contact; removed in round 3.)*
* `db/crm_funnel_repository.py` — `fetch_acquisition_cohort_contacts`. Both
  SQL-entry precedence levels are selected separately from the same
  definitions `effective_date_sql` is built from.
* `services/campaign_evidence_service.py` — rows, KPIs, CPQL and outcome status
  from the cohort; `cohort` block with publication status, the API metric
  contract (`metric_family`, `window_basis`, `outcome_basis`, `dedup_key`,
  `as_of`, `source_freshness`, `attribution_status`, `mapped_count`,
  `unattributed_count`, `excluded_non_google_count`, `coverage_status`,
  `coverage_notes`), reconciliation and the lifecycle-event disclosure. Legacy
  SQL fields are still returned and declared in `legacy_sql`.
* `static/app.js` — one publication gate, `campaignSqlPublication(cohort)`,
  taking the response's cohort as an argument; KPI cards incl. Unattributed
  Google Ads SQLs; Leads acquired / Cohort SQLs columns; an evidence disclosure
  that always shows the lifecycle-event coverage the page does not publish.
* `scripts/audit_marketing_outcome_cohorts.py` — read-only audit over all six
  supported windows, re-deriving membership and SQL proof **in its own SQL**
  and checking the page against it. Exit 0 / 1 / 2. Incomplete event
  timestamps, correctly disclosed, are not a violation.

**CPQL** = window Google Ads spend ÷ **all** Google Ads cohort SQLs (campaign +
unattributed) from the same window, on account-local days for both halves.
Never blocked by a missing SQL date; withheld on a stale or unknown source;
`N/A` at zero.

**Not changed.** `audit_sql_coverage_gate`, the boundary, the 103 open
post-boundary incidents, canonical revenue, junk (still the lead-quality
classification), and every other page.

**Found and fixed on the way.** `_outcome_status` turned an unavailable SQL
count into *"Spend without SQL proof"*; it now says *"Data unavailable"*. The
Campaign drawer threw a temporal-dead-zone `ReferenceError` on open
(`drawerSqlPub` read before its `const`); declared before use.

**Retargeted — and what that loses.** Three PR-ADS-157 certification checks
(`check_frontend_gates`) pinned the legacy gate's strings. They now pin the
cohort contract at equal string strength, and `test_16` shows each red under a
mutation of `app.js`. PR-ADS-143/157 tests that pinned the old labels assert
the new ones, and every principle assertion around them is kept. What is
**not** equivalent: the legacy gate withheld at request time when the page
disagreed with an *independent* lifecycle population. The cohort's
`cohort_reconciliation_failed` compares buckets built in one pass, so it fires
only on a construction bug. The independent cross-check now lives in
`scripts/audit_marketing_outcome_cohorts.py`, which runs on demand, not per
request.

**Review round (truth auditor, three MAJORs, all fixed).**
(1) `assess` returns `fresh=False`, not `None`, for a missing sync state, an
incomplete bootstrap or a failed incremental. The first commit withheld only
on `None`, so it published cohort counts over a partial population, and the
CPQL said "stale". There is now one verdict, `sql_publication()`: only
`source_fresh` / `source_stale` publish, and CPQL inherits the verdict and its
reason. (2) Zero window spend published a `$0.00` CPQL; it is now `N/A`
(`zero_window_spend`). (3) Junk and Junk Rate sat unlabelled beside the cohort's
Leads acquired; every junk label now names its lead-status basis. Also from
that round: a blank original source is excluded under its own reason; the
audit re-derives the Paid Search split independently; deals placed by a
display-only primary contact, and the deal/contact attribution-basis
difference, are disclosed per response; the all-time lifecycle figures are
labelled all-time. Separately, CI's PostgreSQL step caught the audit spelling
`known_reached_sql_by` (a new reader to `audit_sql_coverage_gate`). It now
imports the gate's own `BOUND_COLUMN`; the gate's allow-list is unchanged.

**Doctrine inventory**: 23 legacy / 8 mixed / 5 canonical (was 25 / 6 / 4);
0 unclassified, 0 registry problems. Campaign Evidence and its drawer are
`mixed` — the drawer's Lead Quality / Country splits and Recent Leads, and the
legacy payload fields, still read `leads.status_category`.

A second auditor pass over the fixes found no blocker and two MINORs, both
fixed. The audit's SQL Paid Search normalisation missed tab / newline / NBSP
spellings `classify_source` accepts (a false alarm, failing closed), and the
mobile `data-label`s and drawer split headers still read a bare "Junk".

**Review round 3 (Copilot, 1 high / 7 medium / 3 low — all resolved).**

* *HIGH — raw SQL exposed while withheld.* Rows rendered the withheld count with
  a "not published" suffix. Withheld now means **absent**: the payload carries
  no SQL count, breakdown or CPQL anywhere when the verdict is not `published`
  (the audit's `withheld_exposures()` must be empty), and every surface renders
  *Withheld* / *Unavailable* in the cell itself. Proven by executing the whole
  production `app.js` in a node `vm` against an adversarial payload carrying a
  sentinel count, under seven verdicts, with eight guard-bypassing mutations
  each shown to expose it.
* *One verdict everywhere.* Row-only CPQL refusals ran before the page verdict
  (an unmatched row said `unmapped_label_has_no_campaign_spend` during a
  bootstrap); the verdict is now applied first, every row and the summary
  carry it, and `test_17` asserts one verdict on summary, rows, page CPQL, row
  CPQL, outcome status, cohort block and the detail endpoint across eleven
  states. The drawer gated on page-global state (stale, or absent when opened
  from the Action Queue); it now gates on its own `/api/campaign-detail`
  response's `cohort`, and the gate function takes the cohort as an argument.
* *Detail endpoint.* `/api/campaign-detail` rebuilt its card from a legacy
  whitelist and dropped every cohort field. It now copies `COHORT_ROW_FIELDS`
  (card == table row, field for field), returns the verdict, and no longer
  carries `confirmed_sqls` / `cpql_usd` — the legacy qualified count travels
  only as `legacy_lead_status.qualified` for the labelled split.
* *Fallback shape.* The last-resort response had `cohort.metadata: null` and
  missing blocks; it is now key-for-key the live shape with `null` values. The
  database-down response no longer pairs the real verdict with a summary saying
  `request_failed`, and now carries `metric_family`.
* *Closed-won deals — removed.* Not certifiable in this PR: no ledger sync
  coverage or own watermark, placement through a display-only primary contact,
  deal attribution status derived from contacts. No ledger read, field, card,
  column or drawer KPI remains; `cohort.closed_won_deals` declares them
  unpublished (`deferred_until_certified`). This resolves four findings.
* *Wording.* "as of now" / "today" / "customers" replaced: the cohort is
  measured as of the canonical contact-funnel watermark; SQL = distinct contact,
  closed-won deal = distinct deal, customer = not published.
* *CI.* The blocking job's only failure was `test_pr_ads_152`'s Q4 time-bomb
  (`current_quarter` from the wall clock against July fixtures; red on `main`
  too). The test now injects `now`; a regression runs it under several wall
  clocks and a counterfactual shows the unpinned call really depended on the
  date. Production window behaviour is unchanged.
* The PR-ADS-157 certification's frontend check is tightened, not loosened:
  a surface must call a gate WITH a cohort, no zero-argument call may exist,
  the gate may not read page state, and the drawer may not touch the page's.

**Round 3, third truth review (`3864421`): one MAJOR, two MINORs — fixed.**
The withheld disclosure still rendered the funnel-wide reached-SQL counts
(1,531 / 863 / 668) beside "no SQL count is published", and the exposure scan
never looked there. They are now withheld with the cohort (`counts_withheld`),
the scan covers them, and the UI renders them only under a published verdict
(mutation-tested). The incident count stays visible. The legacy lead-status
fields remain in the payload for other readers. That is now a stated exception
(docs/44 §7), enforced by a certification check that no Campaign Evidence
surface reads them. The fallback is now path-for-path the live shape,
including `sql_reconciliation` and the audit keys, and `test_17c` compares it
recursively. Also: a cohort-maturity note, the gate-call check accepting only
real cohort identifiers (never a literal), and a stale drawer comment fixed.

A fourth independent review of `fa24491` found no blocker and no unresolved
MAJOR. It left one MINOR: the legacy-field exception was guarded by a
hand-listed scan of eight functions. The guard is now proven by EXECUTION:
the whole page renders over a read-recording payload with zero legacy reads,
under every verdict. The certification scan now discovers its surfaces from
the source, and both catch a read added through a new helper.

The final independent review of `7dae351` found no blocker and no MAJOR. Its
one MINOR: the drawer's labelled legacy split was guarded by `test_15` but not
by a mutation or by the certification. Both now cover it. A wording regression
test (`test_18`) pins the watermark language and the "customers are not
published" definition.

Suite: `tests/test_pr_ads_161b_marketing_outcome_cohorts.py` (190), including
9 PostgreSQL end-to-end cases, in CI's PostgreSQL step and did-run list.

Full doctrine: `docs/44_MARKETING_OUTCOME_COHORTS.md`.

---

## PR-ADS-161C — Post-boundary SQL evidence forensics and prospective gap prevention (October 2026)

**Why.** Production (brief, against `dcf078c`): the coverage gate was red on
**113** open post-boundary incidents, all `post_boundary_history_has_no_sql_transition`,
and it called every one of them "our gap". Nothing had shown whose gap any of
them was. This PR does not resolve that by assertion. It makes the question
answerable per incident and fixes the code-owned losses the trace found. It does
not claim any of the 113 is fixed: their breakdown needs production, via the
commands below.

**Four defects, each reproduced on `main`'s production code first.**
1. The detector re-read `lifecyclestage` history for every undated contact on
   every run, and **never the direct `hs_v2_date_entered_salesqualifiedlead`**.
   A direct date HubSpot held but the watermark never re-selected stayed
   invisible forever. Reproduced: HubSpot holds `T`, two runs later
   `date_entered_sql = None`, incident open, reason "no SQL transition".
2. **Stranded incidents.** An incident whose contact left the detector's
   population (its direct date preceded the boundary, or its stage fell below
   SQL) was never looked at again. Reproduced: exact date stored, incident open.
3. **`resolve_post_boundary_incidents` closed any id it was handed**, evidence or
   not. Reproduced: no date anywhere, `persisted: 1`, `resolved`.
4. **A parse failure was reported as HubSpot's absence.** An SQL version with an
   unparseable timestamp was recorded as "no SQL transition".

**Built.**
* `connectors/hubspot_pull.py` — the history read carries the direct property
  as a current value on the same request (history still `lifecyclestage` only);
  `_direct_sql_entry` tells present / absent / unparseable / not_read apart;
  `compare_sql_entry_evidence` is the forensic read (stage + direct property,
  with history of both, and `lastmodifieddate` — no personal property).
* `services/sql_coverage_boundary_service.py` — direct dates refreshed and
  their incidents closed atomically; two new reasons (`…sql_timestamp_unparseable`,
  `…sql_version_undated`); every incident carries the history shape and
  HubSpot-recorded **bounds**; stranded incidents swept, closed only on stored
  evidence, the remainder counted as `open_incidents_outside_population`.
* `db/writers.py` — `apply_post_boundary_sql_evidence` (one transaction:
  fill-only direct date, history upsert, evidence-proven resolution);
  `resolve_post_boundary_incidents` now requires stored evidence;
  `record_post_boundary_incidents` stores the forensic fields and never lets a
  failed read erase a good one.
* `db/schema.py` — additive nullable columns on `sql_post_boundary_incident`,
  no defaults, no backfill, index on `(boundary_id, status)`.
* `analysis/post_boundary_sql_forensics.py` — pure classification: facts,
  one root cause per incident, owner (code / source / not determined).
* `services/post_boundary_sql_evidence_service.py`,
  `scripts/audit_post_boundary_sql_incidents.py` (read-only; READ ONLY
  transaction), `scripts/repair_post_boundary_sql_evidence.py` (dry run default).
* `scripts/audit_sql_coverage_gate.py` — verdict unchanged; the message no
  longer asserts every gap is ours, it points to the forensic audit.

**Not changed.** The boundary; the gate's checks and exit codes;
`analysis/sql_publication.py`; the PR-ADS-161A-1 guard; the acquisition
cohort's definition and verdict; the status vocabulary (`open` / `resolved`).
No new closing status was added, because the gate counts `open`. Observation
bounds are stored and published but **not** consumed by certification.

**Ownership.** `apply_post_boundary_sql_evidence` writes `date_entered_sql`
outside the contact sync, fill-only from NULL with HubSpot's own value, so the
evidence and the closure of its incident commit together. This is a narrow,
stated exception to PR-ADS-153B §30 (docs/45 §5.3).

**Suite.** `tests/test_pr_ads_161c_post_boundary_sql_evidence.py` — PG
end-to-end cases for §8.1–§8.10 and a test for each of §9's eleven mutations,
in CI's PostgreSQL step and did-run list. Not all eleven remove a 161C guard:
`test_60` injects a substitution and shows the invariant catches it, and
`test_61`, `test_65` and `test_66` mutate contracts that predate this PR.

**Production validation (not executed here).** See docs/45 §7. Expected before
any repair: cohort audit 0, campaign certification 0, SQL gate 1 while incidents
stay open. The next incremental sync records the direct-property state and
history shape for open incidents still in the detector's population (up to its
200-contact budget); run the local audit after it, or `--compare-hubspot`.

**Truth audit (`45fb8f5`): no blocker.** Its one MAJOR — the local audit
classified every pre-161C incident, all 113 included, as HubSpot's gap and
exited 0, with `test_08` pinning it — was fixed in `dfe043d` (with Copilot's
review). Its MINORs were fixed after: an out-of-range epoch raised inside the
history read and failed a whole 50-contact chunk; both resolvers now derive
`resolved_by` from stored evidence; `detected_by_run_id` is written once; the
repair counts a missing history payload as partial; the evidence writer is
registered canonical; and four doc overclaims were corrected (columns written,
which incidents a sync re-checks, how many reads the audit makes, what the
mutation tests remove).

Full doctrine: `docs/45_POST_BOUNDARY_SQL_EVIDENCE.md`.

---

## PR-ADS-161D — Canonical closed-won deals, customers and revenue truth (October 2026)

**Why.** Every reader that publishes "customers" counts won **deals**; revenue
readers disagree on population, window and gate; and nothing reported whether a
closed-won number was proven. This PR builds one read-only definition and an
audit that certifies it. **No production page reads it yet** — PR-ADS-161E
migrates readers.

**Built.**
* `analysis/closed_won_truth.py` — pure: won-definition, close-date, amount,
  currency and customer-identity states; half-open window membership; a
  six-bucket attribution partition that always sums to the all-source total;
  the acquisition cohort (contact creation) beside, never mixed with, the
  close-date cohort; a publication verdict per metric, NULL when not published.
* `services/canonical_customer_revenue_service.py` — evidence windows
  (7d…180d, all_time; Europe/London days) and business windows
  (`get_window_bounds`), freshness from sync coverage, campaign placement via
  Campaign Evidence's `_assign_lead` (never fuzzy).
* `canonical_revenue_service.load_closed_won_universe` →
  `deal_ledger_repository.fetch_closed_won_universe` — one
  `REPEATABLE READ, READ ONLY` transaction (won rows, flag/stage cross-check
  rows, deal→contact creation times, sync state).
* `scripts/audit_customer_closed_won_truth.py` — read-only, `--json`, exit
  0/1/2; re-derives every window from the snapshot's raw rows (shared with
  production only: business-window bounds and the Google Ads lattice
  predicate) and runs production's own `fetch_won_deals` inside the same
  snapshot as a cross-check.

**Decisions that depart from the brief.**
1. The predicate stays `hs_is_closed_won IS TRUE` (docs/35 §3 forbids a
   stage-id filter). Stage `326093516` is a **cross-check**: any disagreement
   touching a window withholds its count.
2. **Customers are withheld everywhere.** No deal→company association exists in
   the repository; identity is `association_unavailable`, reason
   `company_associations_not_ingested`, lower bound NULL. Unblocking needs a
   sync change plus a re-sync — out of scope here.
3. **No staleness threshold exists** for the deal ledger, so age is reported
   and not judged (`staleness_assessed: false`).

**Review (Copilot, truth auditor).** Fixed: closes later today admitted
to every window; a published count unchecked against membership; UTC instead of
London evidence bounds; a cross-check outside the snapshot; the acquisition
cohort published over won flag/stage conflicts; withheld counts recoverable
from coverage and bucket fields (now redacted — withheld means absent); unknown
won state undisclosed; per-campaign lower bounds published while Google Ads
deals were unplaced; negative amounts netted off revenue; an audit that re-ran
the service's own logic (now re-derived from raw rows, each shown by an
implementation mutation). Found, not changed: production's
`load_won_deals("all_time")` is bounded above, so it drops undated won deals
and admits closes later today — PR-ADS-161E.

**Not changed.** Every existing reader, route and `static/app.js`; the won
predicate in `analysis.deal_truth`; the coverage gate. No ROAS or CAC.
No HubSpot or Google Ads call; no database write.

**Suite.** `tests/test_pr_ads_161d_customer_closed_won_truth.py` — pure cases
for every state and bucket, audit counterfactuals (each mutation shown failing
the audit), PG end-to-end cases including the audit CLI and a no-writes
fingerprint; in CI's PostgreSQL step and did-run list.

Full doctrine: `docs/46_CANONICAL_CUSTOMER_CLOSED_WON_TRUTH.md`.

---

## PR-ADS-161D2 — Deal→company association ingestion and customer identity certification (October 2026)

**Why.** PR-ADS-161D defined customers as distinct HubSpot company ids and
withheld every customer total, because no deal→company association was stored
anywhere. This PR stores that evidence, keeps it current, and lets the truth
service publish an exact customer total where — and only where — it supports
one.

**Built.**
* `connectors/hubspot_pull.fetch_deal_company_associations` — v4 GET, every
  page, every association path, Retry-After honoured; raises
  `DealCompanyAssociationLookupError` on any failure, malformed row or repeated
  cursor. An empty list only after a complete successful read.
* Schema (additive, idempotent): `hubspot_deal_company_association` (one row per
  deal/company/path), `hubspot_deal_company_observation` (latest attempt and
  latest success per deal; `success_zero_companies` recorded, with an
  all-or-nothing CHECK on the success columns),
  `hubspot_deal_company_sync_state` (last run, last complete scan, backfill
  generation).
* `db/deal_company_association_repository.py` — a success replaces the set and
  records itself in one transaction; a failure records the attempt and
  preserves the last proven set; both ordered by observation START, so an older
  pass cannot overwrite a newer one.
* `services/deal_company_association_sync_service.py` — scans the WHOLE won
  population (never the GCLID subset); bounded; success / partial / failed.
* `scripts/backfill_deal_company_associations.py` — dry run by default (no
  writes), `--apply`, `--resume --apply`, deliberate `--restart`; stops at the
  first failure; completion proven from stored observations.
* Scheduler: `hubspot/deal_company_associations` registered and run right after
  `hubspot/deal_ledger`; no batch → abort before HubSpot.
* Truth service: `company_ids_by_deal` and stale evidence built from the same
  READ ONLY snapshot (under a SAVEPOINT, so unreadable company evidence
  withholds customers only). A deal absent from the map is now
  `association_unavailable`, never "no company".
* `scripts/audit_deal_company_association_coverage.py` — read-only coverage and
  storage certification; `audit_customer_closed_won_truth` now re-derives
  customers from the raw rows.

**Not changed.** Every production page and route; deal, revenue, attribution
and cohort contracts; the won predicate. No HubSpot write, no Google Ads call.

**Suite.** `tests/test_pr_ads_161d2_deal_company_associations.py` — connector,
PG storage (atomicity, preservation, ordering, rollback), identity through the
real service, sync/backfill/scheduler, audits and implementation mutations; in
CI's PostgreSQL step and did-run list.

**Truth audit (no blocker after fixes).** Its blocker — the all-success
fixture in `test_daily_incremental_sync.py` did not stub the new step — and its
MAJOR — customers kept publishing on weeks-old evidence after the pipeline
stopped, because a run that fails before reading a deal moves no per-deal row —
are fixed: a scan gate (latest full run successful and complete, last complete
scan within `customer_identity.association_scan_max_age_hours`) withholds
customers otherwise, a step that dies records a failed full run, and the
coverage audit fails on the same condition. Also fixed: both audits now
reconcile the customer status and identity distribution against the raw
evidence; the lower bound is NULL when nothing was observed; observations are
ordered by the database clock.

**Production.** Completeness depends on the backfill's evidence. Expected after
it: deal and revenue totals unchanged; customer totals published only in windows
whose won deals all resolve to one current company; the coverage audit lists the
CRM remediation queue (no-company and multi-company won deals).

Full doctrine: `docs/46_CANONICAL_CUSTOMER_CLOSED_WON_TRUTH.md` §5, §9.1.

### PR-ADS-161D2-F1 — Monotonic and atomic customer certification (October 2026, same PR)

**Why.** Review of PR-ADS-161D2 found four ways the system could claim current,
complete customer evidence it had not proven.

**Fixed.**
* **Run state is monotonic by run start.** Every `last_run_*` and
  `last_full_run_*` field applies only when its run started at or after the
  stored run (this landed with the Copilot review fixes). F1 adds tests for
  both directions and every commit order of three runs.
* **A complete scan names what it certified.** `last_complete_scan_deal_ids`
  stores the exact set; the population is its size. A completeness claim that
  names no deals proves nothing.
* **The coverage audit checks the CURRENT population, set for set.** The new
  blocking check `complete_scan_covers_current_won_population` requires every
  current won deal to have a success at or after the scan's start, the
  certified set to equal the current won set, and the recorded population to
  equal the set's size. A deal that won or stopped being won since the scan
  fails it until a later full scan succeeds and covers every won deal (a
  failed, truncated or 404-ing scan keeps it red).
* **Backfill completion is atomic.** `complete_backfill_generation` no longer
  takes a population. Inside its transaction it takes a SHARE lock on
  `hubspot_deal_ledger`, re-reads the won population, proves every deal was
  observed in the generation, and derives the certified set. Otherwise it
  refuses with the remaining count.
* **Counts are separate from samples.** CRM findings report
  `no_company_count` / `multiple_company_count` beside
  `*_deal_ids_sample` (≤ 100).
* **Runbook.** `--resume --apply` runs only after an `--apply` that stopped at a
  resumable checkpoint.

**Suite.** `tests/test_pr_ads_161d2_f1_monotonic_atomic_certification.py`, in
CI's PostgreSQL step and did-run list. It includes a real concurrent ledger
writer that completion must wait out.

### PR-ADS-161D2-F2 — Scan age from its start; reconciled population (October 2026, same PR)

From the truth audit of F1 and the PR re-review.

* **The scan is aged from its START** (the truth auditor's MAJOR, inherited
  from 161D2). A backfill generation resumed over days used to stamp
  `last_complete_scan_finished_at = now`, and age was measured from that, so
  nine-day-old evidence read as fresh for 48h. The service gate and both audits
  now age from `last_complete_scan_started_at`, which bounds the oldest
  observation the scan certified.
* **`complete_scan_population_reconciles`** is a separate blocking check: the
  recorded population must equal the certified set's size and the current won
  population. Recorded, certified and current counts are disclosed.
* **Migration:** `ALTER TABLE … ADD COLUMN IF NOT EXISTS
  last_complete_scan_deal_ids`, for a database that created the table first.
* **Bounded lock:** completion waits at most
  `COMPLETION_LOCK_TIMEOUT_MS` (30s) for the ledger lock. On timeout it writes
  nothing and reports unavailable; `--resume --apply` retries.
* **Tests:** completion is refused, then succeeds with the DB-derived
  population; an older complete scan committing late keeps the newer set; a
  held ledger lock times out without writing; the migration is exercised on a
  real table.

### PR-ADS-161D2-F3 — The publication gate requires the certified set (October 2026, same PR)

**Why.** After F2 the coverage audit required the scan's certified deal set to
equal the current won population, but the customer publication gate did not.
A deal that won after the scan and was then observed by a subset pass left
every member with usable evidence: the audit said "coverage contract broken"
while the service published. One system gave two answers.

**Fixed.** `association_scan_current` takes the current won-deal ids (from the
same snapshot as the evidence) as a required argument. It is current only when
`last_complete_scan_deal_ids` equals that set and
`last_complete_scan_population` equals both; otherwise customers are withheld
with `customer_identity_scan_not_current`, and the freshness block discloses
`won_not_certified` / `certified_no_longer_won`. Both audits' `scan_is_current`
restate the same rule independently, so their expected verdicts match the
service.

**Suite.** `test_50`–`test_53` in
`tests/test_pr_ads_161d2_f1_monotonic_atomic_certification.py`: a deal won
after the scan (observed by a subset pass) withholds until a full scan
certifies it; a deal leaving the won population withholds until rescanned; a
corrupted recorded population is rejected by the gate and the audit; the gate
cannot be called without the won set. All four fail on 03159b5.

## PR-ADS-191 — CRM Truth Exceptions workbench (October 2026)

**Why.** The canonical services correctly withheld customer and revenue totals
but never named the HubSpot won deals responsible. The system knew where the
problem was, and the CRM team could not see it.

**Built.**
* `services/crm_truth_exceptions_service.py` — one read-only read model over
  the canonical universe and truth service. Rows are keyed
  `(deal_id, family, reason)` in three families: customer identity, revenue
  proof and won-deal membership. Dataset-level blockers cover an unreadable
  ledger or evidence, unproven coverage, and a scan that is not current. Each
  window gets a summary that explains every withheld verdict and reconciles
  with the truth service's own counts.
* `GET /api/audit/crm-truth-exceptions` and `.csv` — admin-only,
  formula-injection-safe CSV, 400 on an unknown filter. Unavailable evidence is
  `rows: null` (JSON) or a 503 (CSV), never an empty list.
* System Status → **CRM Truth Exceptions** panel (admin-only).
* `scripts/audit_crm_truth_exceptions.py` — exit 0/1/2; CRM findings alone
  never fail it.
* Truth review before push. Fixes applied:
  - an undated won deal is named in every window that cannot include it, even
    where a won-definition conflict is the verdict's first reason;
  - undated and conflict verdicts reconcile with the truth service's counts;
  - the audit re-derives undated and conflict deals from the raw ledger;
  - an unreadable identity family makes the unfiltered list and totals
    `null`, and makes the CSV a 503, never a partial list;
  - the won population is labelled as the raw won-flag count the scan
    certifies, not the closed-won total;
  - future-dated findings are disclosed through `close_date_state`;
  - the workbench's own importers are fenced;
  - numeric-only HubSpot links, and whitespace-prefixed formula cells.
* PR review round (Copilot). Five findings, all fixed:
  - the audit's undated check reads only missing-close-date rows, so an
    undated conflict deal no longer fails a truthful workbench;
  - filter reconciliation reads every API page past `MAX_LIMIT`;
  - window identity counts are `null` while association evidence is
    unreadable, and the audit then reports unavailable, not a violation;
  - the won-definition snapshot rows carry `deal_name`;
  - the UI's filter options stay retryable after an unavailable response.
* Canonical-owner change, publication unchanged: `company_identity_inputs`
  returns `stale_reasons` (`stale` is exactly its keys), and
  `scan_certification_sets` is the one place the certified set is compared.
  `audit_customer_closed_won_truth` names the workbench as a certification
  consumer.

**Not changed.** No publication gate, no total, no customer or revenue
definition, no HubSpot or Google Ads call, no write.

**Suite.** `tests/test_pr_ads_191_crm_truth_exceptions.py`, in CI's PostgreSQL
step and did-run list.

Doctrine: `docs/47_CRM_TRUTH_EXCEPTIONS.md`.

---

## PR-ADS-161E — Production readers migrated to canonical customer and closed-won truth (November 2026)

**Why.** PR-ADS-161D/161D2 built one certified definition of closed-won deals,
customers and revenue, and PR-ADS-191 named the deals that block it. No
production page read it. Pages still called won deals "customers", divided CAC
by won deals, and gated canonical revenue on a legacy table's existence
(`revenue_integration_connected()` over `gclid_attribution`). They also mixed
two cohorts in `sql_to_customer_rate`. All Time dropped undated won deals and
admitted closes later today.

**Built.**
* Reader contract in `services/canonical_revenue_service.py`:
  * `load_won_deals` is now a reader of
    `canonical_customer_revenue_service.window_view`. It releases the
    population only when the canonical deal count publishes, and carries the
    canonical verdicts.
  * `subset_metrics` / `reader_row` produce a row's deals, customers, revenue
    and the two period ratios through `analysis.closed_won_truth.subset_outcomes`,
    `derive_period_revenue_per_ad_spend` and
    `derive_period_spend_per_customer_won`. Certified ROAS and CAC are
    always unavailable (decision 1, below).
  * `one_snapshot` / `single_snapshot` give each page one READ ONLY universe.
* Migrated:
  * the Revenue Decision Mart, revenue attribution (Campaign / Country Revenue &
    Spend, formerly "ROAS by Campaign / Country"),
    Revenue by Source, campaign mapping review and Unit Economics;
  * all six dashboard tabs and cross-page parity.
  * Every one now publishes `closed_won_deals` and canonical `customers` as
    two metrics, each with status and reason.
* Removed: `db/revenue_repository.revenue_integration_connected()`. Readiness
  reads `canonical_revenue_available` and `canonical_revenue_status`.
* Retired:
  * `sql_to_customer_rate`, `revenue_per_sql` and `lead_to_customer_rate` are
    unavailable, with the reason (two cohorts).
  * The Deals tab reads won-deal contacts from the canonical snapshot, not
    `gclid_attribution.has_closed_won_deal`.
* Withheld legacy routes: `/api/reports/roas/campaigns|countries`,
  `/api/reports/roas/snapshots*`, and the GCLID readiness won-deal count
  (`legacy_local_revenue_not_canonical`).
* UI (`static/app.js`):
  * `canonicalMetricHtml` / `rowMetricHtml` render the value, `0`,
    "Withheld" + reason, or "Unavailable" + reason, never a dash;
  * admin-only **View blockers** deep-links into the CRM Truth Exceptions
    workbench with the window and family preselected;
  * channel, campaign and country charts count won deals; customers are never
    summed across rows.
* Per-campaign placement republishes the canonical partition: the window is
  evaluated with Campaign Evidence's resolver. Per-campaign deals, revenue,
  customers and period ratios are withheld while a Google Ads won deal resolves to
  no campaign (`google_ads_deals_not_placed_on_a_campaign`). The
  campaign-attributable scope total is not gated.
* Country residual: the Google Ads-source canonical total minus the country
  rows. It used to subtract from the all-source total, which put non-Google
  deals in the "no country" bucket.
* Found by the new audit: the Mart's revenue metric carried the legacy reason
  code. In a window withheld for an undated deal, it also said "unavailable"
  where the canonical verdict is "withheld". It now republishes the canonical
  status and reason whenever either side withholds.
* `scripts/audit_customer_revenue_reader_cutover.py` — 18 checks, exit 0/1/2;
  CRM findings alone never fail it. `INVENTORY` is the reader matrix.

* **Truth review before push** (`averroes-truth-auditor`). It found 1 blocker
  and 5 major findings, all reproduced by executed probes, and all fixed:
  - **Blocker:** a withheld per-campaign deal count read as 0 became "waste"
    verdicts and "no won deals" cards. Rows with a withheld count are now
    `outcome_withheld` / "Closed-won outcome withheld" and are never judged.
  - The placement gate re-derived "unplaced". It now republishes the canonical
    partition, and the scope total is ungated.
  - Recoverable withheld values:
    - a proof-deal list stayed beside a withheld count;
    - `summarize_deals` netted negative amounts;
    - the GCLID readiness sibling counts re-added to the withheld total.
  - The spend ratios were published at `all_source` scope.
  - The audit's leak walk covered the Mart only, and it missed the Mailchimp
    audit reader.
  - Headline revenue said "Unavailable" with no reason.
  - Minors fixed:
    - wrong-reason cards ("unknown deal amount", "ledger could not be
      read", "integration disconnected");
    - an HTML sub-line rendered as escaped text;
    - deal-count links carried no window;
    - `reconciles: true` was reported without a money comparison.
  - Regressions: tests 28–33.

**Not changed.** The won predicate, customer identity, revenue proof, window
definitions and the canonical verdicts are unchanged. **Changed on purpose, as
directed by the owner in review:**
* **Decision 1 (corrected).** Attribution ROAS and CAC are **unavailable**
  everywhere: close-date outcomes and same-window spend are not one acquisition
  cohort in a long B2B sales cycle. Reasons:
  `roas_requires_revenue_and_spend_in_one_acquisition_cohort`,
  `cac_requires_spend_and_customers_in_one_acquisition_cohort`.
  The close-date calculation is kept as two non-cohort operational ratios:
  * `period_revenue_per_ad_spend` — "Period Revenue / Ad Spend";
  * `period_spend_per_customer_won` — "Spend per Customer Won This Period".

  Each carries `ratio_kind: non_cohort_operational_ratio` and the disclosure
  "Non-cohort operational ratio · Closed-won outcomes may originate from
  earlier advertising periods", which the UI shows beside it. Renamed to match:
  * every API row and KPI field (`roas`/`geo_roas`/`google_ads_roas` and
    `cac` are null; `roas_available` (rows, `spend_truth` and the source
    classification) is now `period_ratio_available`;
    `roas_status`/`roas_reason` are now `period_ratio_status`/`period_ratio_reason`;
    the Mart readiness `roas_ready` is now `period_ratio_ready`);
  * every label, page title ("ROAS by Campaign/Country" are now "Campaign/Country
    Revenue & Spend") and card.

  Kept on purpose: route ids (`#/roas-campaigns`) and the source-health
  booleans `campaign_roas_available`, `country_roas_available` and
  `country_roas_unblockable`. They are not ratio values.
  Audit check 18 (`no_period_ratio_named_roas_or_cac`) walks twelve page
  payloads (the ten builders plus the Mart's source and country views). It fails
  on any of these:
  * a figure, digit-bearing string or numeric descendant under any `roas`/`cac`
    key, in any case or camelCase, or a synonym;
  * a row naming ROAS/CAC by value;
  * any API string that calls something ROAS or CAC, outside the exact
    certified-unavailable phrasings;
  * a period ratio without its disclosure;
  * UI text naming ROAS/CAC outside the exact certified labels, or a certified
    label rendered beside a period value.

  Tests 38–45 are its counterfactuals. The UI shipped at `64b2ce3` has 97
  violations.
  The truth review of this round found live diagnostics text ("carries
  spend/ROAS", "Country ROAS is shown", "Geo ROAS is withheld") and lexical
  bypasses of the first check 18; both are fixed. Zero spend now makes Spend per
  Customer Won This Period unavailable, never "$0", and the dead
  `compute_roas` / `compute_cac` helpers are deleted.
  docs/46's 161D line ("neither") stands: no ROAS or CAC is published.
* **PR review round 2 (Copilot, on `4f0f199`).** Two findings, both fixed:
  * Unit Economics now republishes the canonical verdicts when the window
    releases no population. It used to return `business`/`advertising: null`,
    so the KPIs read a bare "Unavailable" with no reason and no View blockers.
  * Cross-page parity now classifies every canonical customer-identity reason.
    It used to report them as the catch-all `canonical_source_unavailable`. A
    CRM finding is `customer_total_unpublishable_identity_unresolved`. Stale,
    unobserved, unscanned or uningested evidence is
    `customer_association_coverage_unproven`.
  * Tests 46–47 cover both; each fails without its fix.
* **Decision 2 (approved).** Per-campaign figures follow the canonical
  placement verdict. Account and scope totals stay independent when their own
  evidence is complete. A withheld campaign count is never zero, "waste" or
  "no won deals".

No HubSpot or Google Ads call, no write. Marketing SQLs stay on the
acquisition cohort.

**Deferred** (docs/48 §7): `/api/attribution/confidence-summary` (tier counts
only), `/api/audit/revenue-truth` (admin shadow diagnostic), and the daily
local snapshot job (its routes withhold).

**Suite.** `tests/test_pr_ads_161e_reader_cutover.py`, in CI's PostgreSQL step
and did-run list, with a counterfactual on base `028effc`.

Doctrine: `docs/48_CUSTOMER_REVENUE_READER_CUTOVER.md`.

## PR-ADS-161E-F1 — Final review's minor findings closed (November 2026)

**Why.** PR-ADS-161E merged at `b00defb` with no blocker, major or medium
finding. The independent final review recorded four minors, left out so the
merged head would not move; this PR closes them.

**Fixed.**
* **Parity names no owner unless every consumer explains the absence.**
  `cross_page_parity_service._classify_unavailable` used to route a metric to a
  specific owner when one consumer declared a registered reason, even if
  another declared none or one the audit cannot classify. It now falls back to
  `canonical_source_unavailable` and names who said what. Every real consumer
  declares a reason (the PostgreSQL parity suites pass unchanged), so no
  production outcome moves; only the mixed case stops guessing.
* **Unit Economics' all-source `business` block** carried period ratios
  "withheld" or "spend unavailable". Google Ads spend can never have produced an
  all-source population, so both period ratios are now
  `numerator_and_denominator_scopes_differ` (docs/48 §4) on the published and
  the withheld path.
* **Unit Economics no longer reads a bare "Unavailable".** Three spots did:
  * average deal value (KPI and campaign rows) now carries the verdict of
    whichever input blocks it, revenue or won deals;
  * the window verdict shows the canonical word ("Withheld" for a CRM
    finding, with View blockers for admins; "Unavailable" for an outage) and
    the reason in words, never a raw code;
  * a withheld window's campaign table says the economics are withheld, not
    "No campaign-level economics data".

  The PR-ADS-161E note that "the KPIs read Withheld with the reason" now holds
  for every card.
* **Test 46 runs through the real producer.** It now uses a ledger with an
  undated won deal instead of a hand-built verdict. It renders the real
  `renderUnitEconomicsPage` in node for an admin and a viewer. 46b adds the
  outage case: "Unavailable", no View blockers link, even for an admin.

**Tests.** 46 (rewritten), 46b, 48, 49 and 50 in
`tests/test_pr_ads_161e_reader_cutover.py`; each fails on `53e5bc9` and passes
here.

**Not changed.** The audit check, the reader contract, the canonical verdicts
and the period-ratio doctrine. One observation is still open and outside this
PR: docs/48 §5 lists `company_associations_unreadable` in the
`customer_identity` View-blockers family, while also saying outages get no link.
The link still opens a dataset blocker in the workbench.

## PR-ADS-161F — Deal-ledger freshness gate (November 2026)

**Why.** The deal ledger's age was reported, not judged. If the daily HubSpot
deal sync simply stopped, every page kept showing its last deals, customers and
revenue as current. A sync that ran and failed already withheld; one that never
ran did not.

**Built.**
* `deal_ledger.max_staleness_hours: 36` in config/thresholds.yaml (owner-set;
  the sync is daily, so 36h tolerates one late run and catches a missed day). A
  missing config file falls back to the same 36.
* `canonical_customer_revenue_service.ledger_staleness` judges a PROVEN ledger.
  Over 36h, or with no successful-sync timestamp, it adds a
  `canonical_ledger_stale` finding. `closed_won_truth.evaluate_window` then makes
  deals, revenue and customers `unavailable` with that reason. Every reader
  inherits it through the one snapshot.
* The CRM Truth Exceptions workbench shows a stale-ledger dataset blocker
  instead of a blocker list that may name deals already fixed.
* Parity routes it to the sync owner.
* The UI reads "Unavailable — the deal sync is overdue", with no View blockers
  link (no CRM record to fix).
* `audit_customer_closed_won_truth` prints the threshold and the verdict.

**Fixtures.** `READY_SYNC_STATE` stamps a fixed June sync, which reads as months
stale to tests evaluated later. The default stubbed ledger is now current when
read (`fresh_sync_state`). Suites that pin an evaluation date use
`fresh_sync_state(NOW)`, and the PostgreSQL seeds use the database clock. Two
tests that age the association scan by 47–49h now keep the deal sync current.
That way they test the scan only.

`test_161d_51` pinned the old rule ("age is reported, not judged"). It now
asserts the new one: the same June ledger read in October withholds every
figure.

**Tests.** `tests/test_pr_ads_161f_deal_ledger_freshness.py`, 12 tests; all 12
fail on `c0de299`.

---

## PR-ADS-161G — one-command platform certification

**Why.** Six audits had to be run one by one in the Render shell, and their
exit codes combined by eye. One command now runs them and gives one verdict:

```bash
python -m scripts.audit_platform_certification          # human-readable
python -m scripts.audit_platform_certification --json   # machine-readable
```

**What it does.** It runs each existing audit unchanged, in its own
subprocess, with `--json`, and combines the results. There is no new gate
logic.

| Exit | Verdict | Meaning |
| --- | --- | --- |
| 0 | CERTIFIED | all six exited 0 with a consistent report |
| 1 | VIOLATION | at least one audit found a truth-contract violation |
| 2 | NOT CERTIFIED | no violation, but at least one audit did not finish |

**Fail closed.** An exit code alone is not enough evidence. Two audits let an
unexpected exception escape, and Python reports that as exit 1, which would
read as a violation. Any of these counts as `crashed`, and a crash never
certifies:

* an exception escaping the audit;
* exit 0 with no readable JSON report;
* an unknown exit code;
* a timeout;
* a report whose own `exit_code` disagrees with the process exit code.

**CRM data to fix.** Below the verdict, the command repeats the won deals that
keep figures withheld, from the CRM Truth Exceptions audit's own report. This
is the list for sales. If that report could not be read, the count prints as
`unknown`, never `0`. A CERTIFIED platform can still withhold figures: a
withheld figure is a correct answer.

**Tests.** `tests/test_pr_ads_161g_platform_certification.py` has 15 tests,
20 cases with parametrisation. The last test runs the six real audits with no
database and asserts that each prints a readable report and exits 2, not
`crashed`.

**Not changed.** Every audit, gate and verdict.

---

## PR-ADS-161H — attribution feasibility report (read-only, measure first)

**Why.** Attribution ROAS and CAC are unavailable (docs/48 §4): close-date
revenue over same-window spend is not one acquisition cohort in a long B2B
cycle. Before building cohort attribution, we measure whether the data could
support it:

```bash
python -m scripts.report_attribution_feasibility          # human-readable
python -m scripts.report_attribution_feasibility --json
```

**What it measures.**

1. **Campaign placement.** How many won Google Ads deals sit on ONE campaign
   (`campaign_attributable`) and how many no campaign can claim
   (`google_ads_unplaced`, with reasons). This is read from the canonical
   truth's own attribution partition, using the production campaign resolver.
   It is never recomputed.
2. **Acquisition date.** How many won deals have every contact's creation time
   on record. The report separates:
   * a lookup that found no contact;
   * a failed or missing association lookup ("unresolved");
   * a contact whose creation time is missing.
3. **Cycle length.** Days from a deal's first contact creation to its close:
   p25, p50, p75 and p90, and the share of deals closing within 30, 90, 180 and
   365 days. It is shown for all won deals and for Google Ads deals.
   * A contact created after the close gives no cycle length. These deals are
     counted separately, never treated as 0 days.
   * When no deal can be measured, every percentile is `None`, never `0`.

**What it does not do.** It sets no threshold and publishes nothing; whether
the numbers justify building is the owner's call.

* **Exit 0:** measured.
* **Exit 2:** not measured. This happens when the ledger is unreadable, its
  coverage is not proven (a missing verdict counts as unproven), or the truth
  reports any window's population unavailable (for example, a stale ledger).

**Registered as a diagnostic.** The guards caught the report as a new reader
on its first run, which is their job. It is now named in three places, beside
the audits it sits with:

* `OWNERS` in `audit_customer_revenue_reader_cutover`;
* `CERTIFICATION_CONSUMERS` in `audit_customer_closed_won_truth`;
* the SQL doctrine registry (`closedwon.feasibility`, `CLS_DIAGNOSTIC`), for
  its two `contact_created_at` reads.

It serves no page.

**Tests.** `tests/test_pr_ads_161h_attribution_feasibility.py` (11) drives the
real `get_closed_won_truth` over a stubbed ledger.

**Not changed.** Every reader, gate, verdict and page.

---

## PR-ADS-161I — the contact scan read ~30% of HubSpot and called it complete

**Found in production (2026-11-16).** HubSpot held **21,751** contacts and
`hubspot_contact_funnel` held **6,389**. A full rebuild from the epoch reported
`success` / `bootstrap_status = complete` after **2,000**. A raw search from the
Render shell paged in order past 2,400. The connector's own iterator jumped from
January 2025 to May 2026 by page 7 and emitted its completion sentinel at page
20. Won deals with a contact never synced: **41 of 67 closed in 2025**.

**Cause.** `iter_contacts_modified_since` re-sent its *advancing* watermark as
the `lastmodifieddate GTE` filter on every page, but kept the paging offset.
Each request was therefore the Nth page of a different, shrinking result set,
skipped ahead further every page, and ran out of "next" early. This had been
true since PR-ADS-153B. It hit the bootstrap and every incremental run with more
than one page of changes. The tests' fake HubSpot ignored the filter value, so
none of them could see it.

**Fixed.**
1. **Keyset paging, no offsets at all.** Every page is a new query,
   `lastmodifieddate >= watermark`, and rows already read at the watermark
   timestamp are de-duplicated. A group of contacts that shares ONE timestamp
   and fills a page is walked by keyset on the contact id instead:
   `lastmodifieddate EQ T AND hs_object_id GT last`, sorted by id. That query was
   verified against live HubSpot, which returns tied rows in an arbitrary order.
   After the group, the scan resumes with `lastmodifieddate GT T`. Any offset
   over HubSpot's *live* result set skips an unread contact whenever an
   already-read one is modified, merged or deleted mid-scan, and walking ties by
   offset skips when HubSpot reorders them. Both cases were reproduced by the
   truth audit: 29 and 99 contacts missing while still reporting complete.
2. **A scan must prove it is whole.** It must meet HubSpot's own `total` from its
   first response, within max(1%, 2) for contacts deleted before they were read.
   Zero read against a non-empty set never passes, and a response without `total`
   cannot prove anything. Otherwise `HubSpotSearchIncompleteError` is raised
   instead of the completion sentinel.
3. **An incomplete scan rewinds.** The per-page checkpoints had already moved the
   watermark past the gap. The failure now resets it to where the run began (NULL
   for an epoch scan). Without this, the next scheduled run resumed past the gap,
   checked only the tail, and marked the bootstrap complete (truth-auditor
   blocker).
4. **The false `complete` is withdrawn once** (migration
   `PR-ADS-161I-contact-funnel-rebuild-from-epoch`). It sets the state to
   `partial` with no watermark, so **the next scheduled run rebuilds from the
   epoch by itself**. Until then, every gate that needs a complete bootstrap
   withholds. No contact row is deleted.
5. Run results now carry `source_total` and `unique_contacts_read`
   (`contacts_seen` counts re-reads).
6. **An incremental never writes `bootstrap_status`**, so no run of the *new*
   code can restore the withdrawn `complete`. A run of the *old* process that is
   still syncing during the deploy can (see the residual risk below). A contact whose
   `lastmodifieddate` cannot be parsed is still read, and never moves the
   watermark. A rewind that fails to persist is logged and reported as
   `watermark_rewind_failed`.

**Not affected by this cause.** `pull_deals_for_ledger` keeps one filter for its
whole read. It does page by offset over a live set, so a deal modified during its
read could in principle shift a page. This is noted, not fixed here.
*(Corrected in PR-ADS-166B: the original text added "the deal population is
small and reconciled by its own audits". That was not true — no audit compares
the ledger with HubSpot; the deal certification audits forbid importing the
HubSpot connector and check the ledger against itself. The shift was real and executed in review:
an edited deal made the read skip an unread one and still say `complete`.
PR-ADS-166B moved the deal read to keyset paging.)*

**Tests.** `tests/test_pr_ads_161i_contact_scan_paging.py` (21). Its fake HubSpot:
* honours the GTE / GT / EQ and `hs_object_id GT` filters;
* sorts by the requested property;
* reports `total`;
* can **change its data during the scan** and **reorder ties between requests**.

What the tests prove:
* Against `main`'s connector the fake yields 600 of 2,500 contacts and reports
  complete.
* 08 (data changing mid-scan) and 11 (two runs through the real service) fail on
  this PR's first push.
* 02, 09, 10, 10a (reshuffled ties) and 10b (a tie member modified mid-walk) fail
  on its second push.
* 13 runs the migration on a live PostgreSQL cluster, once only. The module is
  in CI's PostgreSQL step, so it cannot be skipped silently.
* Two fakes in `test_pr_ads_153b_canonical_funnel.py` now report `total`, as
  HubSpot does; their assertions are unchanged.

**Residual risks.**
* A chain of `max_pages`-capped runs (API only) is checked per run, not per
  bootstrap.
* An *old* process still running a sync across the deploy could restore
  `complete` / a watermark after the migration.

* Two scan-loop guards: a full GT page that makes no progress, and an empty
  first id-walk page, both raise `HubSpotSearchStalledError`.

**After deploy, confirm the state once, before the next scheduled sync:**
`SELECT bootstrap_status, last_modified_watermark FROM hubspot_contact_funnel_sync_state WHERE scope = 'contacts'`.
It should read `partial` / `NULL` until the rebuild runs, then `complete`.

**If it reads anything else before the rebuild has run,** an old process
re-asserted its state. Re-apply the withdrawal:
`UPDATE hubspot_contact_funnel_sync_state SET bootstrap_status = 'partial',
bootstrap_completed_at = NULL, last_modified_watermark = NULL WHERE scope = 'contacts'`.
Then let the next scheduled run, or `POST /api/crm-funnel/sync?mode=bootstrap`,
rebuild from the epoch.

**After deploy.** Nothing to run by hand. The next scheduled sync (or
`POST /api/crm-funnel/sync?mode=bootstrap`) rebuilds from the epoch. Check that
`unique_contacts_read` is close to HubSpot's total. Lead and SQL counts for
older windows will rise: that is the correction.

---

## PR-ADS-161J — the platform certification runs daily inside the app

**Why.** The certification (PR-ADS-161G) only ran when someone pasted a command
into the Render shell.

**What.** A new scheduler job, `platform_certification`, runs at **10:30
Asia/Amman (07:30 UTC)**, 90 minutes after the 09:00 incremental sync, inside
the existing web service. It costs nothing extra on Render.
`services/platform_certification_service.run_and_record` calls
`scripts.audit_platform_certification.run()` unchanged and writes one row to the
new table `platform_certification_runs`:

* started / finished;
* exit code and verdict;
* a compact summary: each audit's outcome, violations and unavailable reasons,
  plus the CRM data to fix. The full audit reports are not stored.

**Fail closed.** If the certification cannot run, or returns an exit code it
does not recognise, the row records **NOT CERTIFIED** (exit 2) with the error.
It is never a pass and never a missing row. A failed write is logged and
returned as `recorded: false`. The scheduled wrapper never raises.

**Read it.**
`SELECT started_at, verdict, summary FROM platform_certification_runs ORDER BY started_at DESC LIMIT 1`.

**Not changed.** No audit or gate. The job writes only to the local database.
The audits it runs are read-only.

**Tests.** `tests/test_pr_ads_161j_daily_certification.py` (10, including a
live-PostgreSQL round trip). The module is added to CI's PostgreSQL step.

---

## PR-ADS-162A — Channels page: one acquisition cohort, every channel

**Why.** The owner wants the product to assess every marketing channel, not only
Google Ads. Production data (2026-11-18, 21,791 contacts) showed what the app was
not surfacing:

* Paid Social is the second paid channel: 2,461 contacts, split between LinkedIn
  (1,336) and Facebook (1,121).
* AI referrals (chatgpt.com) and organic search exist but appear nowhere as
  channels.
* Mailchimp is **not** connected in production: 0 rows, and no sync batch has
  ever run. An email page would have been empty.

**What.** A new sidebar page, **Channels** (`GET /api/channel-funnel`,
`services/channel_funnel_service.py`).

* **Cohort.** Contacts created in the window (30d / 60d / 180d / all time). It
  uses the same membership rule, account-local window days and freshness verdict
  as Campaign Evidence's acquisition cohort, over all sources.
* **Channel.** HubSpot's original source, unmodified. The original-source
  drill-down (linkedin, facebook, chatgpt.com, a campaign label) is shown
  underneath: the top 8 values, with the rest folded into one row.
* **Stages.** MQL, SQL, Opportunity, Customer stage. A contact has reached a stage
  on any of:
  * a direct stage-entry timestamp;
  * a recovered stage-entry timestamp;
  * a current lifecycle stage ranked at or beyond it.

  SQL uses the cohort service's own `sql_proof`, so there is one SQL rule. Nothing
  is inferred downward: opportunity evidence is not SQL evidence, so a later stage
  can exceed an earlier one for a channel.
* **One publication verdict** (`sql_publication`). Rows that do not sum to the
  total, an incomplete bootstrap, a failed incremental or an unknown watermark
  withhold the whole table. The payload then carries **no counts at all**.

**Not on this page.** Deals, revenue, customers (companies), spend and cost per
SQL have their own owners and gates. "Customer stage" is the HubSpot contact
lifecycle stage, counted in contacts and labelled as such.

**Tests.** `tests/test_pr_ads_162a_channel_funnel.py` (16 tests, 19 cases). It
includes the real query on live PostgreSQL and the real `renderChannelFunnel`
evaluated in node. The module is added to CI's PostgreSQL step. The service is
registered in the SQL doctrine registry (`_CHANNEL_FUNNEL`).

---

## PR-ADS-162B — retire endpoints nothing calls (slice 1)

**Why.** `api/server.py` had grown to 9,477 lines and 119 routes, and the UI never
calls 29 of the `/api/*` routes. Most of those are still wired into old tests and
guard registries, so they are being retired in slices, each with its tests and
registries updated. This is the first slice: the routes with no caller and no
dedicated test.

**Removed** (about 800 lines of `api/server.py`, 240 lines of `API_CONTRACT.md`):

| Route | What it was |
| --- | --- |
| `GET /api/summary` | legacy headline KPIs from the `campaigns` snapshot (third SQL definition) |
| `GET /api/dashboard/trends` | legacy period-over-period trends over the same snapshot, with its alert helpers |
| `GET /api/deals` | legacy `deals` table read |
| `GET /api/campaigns/{campaign_name}/detail` | path-segment duplicate of `/api/campaign-detail` |
| `GET /api/keyword-evidence/audit` | operator diagnostic; the script `audit_keyword_currency` remains |
| `GET /api/google-ads-spend-coverage-audit` | operator diagnostic; its service function and test remain |
| `GET /api/system/reality-audit` | operator diagnostic; the script `audit_production_reality` remains |

Also removed: the helpers only those routes used (`_compute_severity`,
`_build_trend_alerts`, `_trend_metric*`, the `_TREND_*` thresholds, the
reality-audit cache) and `_nullable_int`, which was already unreferenced.

**Registries.** `analysis/sql_doctrine_registry.py` no longer lists the summary
and trends consumers, their rules, or the `/api/summary` CPQL consumer. The
snapshot SQL definition now feeds only the action queue and historical
intelligence.

**Tests.** No test exercised these routes. The existing assertions that the UI
does not call `/api/summary`, `/api/dashboard/trends` or `/api/deals?days=` still
hold. No test was edited.

**Next slices.** `/api/leads`, `/api/search-terms` (+ `/summary`), `/api/waste`,
`/api/keywords`, the retired ROAS routes, `/api/revenue-by-source`, the hidden
historical-intelligence page, and the Windsor connector. Each has tests and guard
entries to update with it.

---

## PR-ADS-162C — one plain-language System page; technical admin pages folded away

**Why.** The Admin section listed six technical pages (Data Runs, GCLID
Attribution, System Status, Revenue Health, Admin Backfill, Churn Input) with
no plain statement of the three things an operator needs: can the numbers be
trusted, is the data up to date, and what has to be fixed. The daily
certification (PR-ADS-161J) recorded a verdict that no screen showed.

**What changed.**

- `GET /api/system/overview` (admin only) —
  `services/system_overview_service.py`. Three blocks, all read from the local
  database:
  1. **Certification**: the newest `platform_certification_runs` row as
     recorded. `certified` requires exit code `0`, a finish time within
     `CERTIFICATION_MAX_AGE_HOURS` (26h) AND a non-empty audit list in which
     every outcome is `holds`. No row, an old row, a future-dated row or a
     missing finish time is `not_checked`; an unrecognised exit code, or a `0`
     without every audit holding, is `not_certified`; an unreadable table is
     `unavailable`.
  2. **Sources**: one `status` per source; only `current` is healthy. The page
     owns no freshness rule — each status is the verdict the platform already
     publishes by:
     - HubSpot contacts: `cohort.read_freshness` (the canonical assessor);
     - HubSpot deals: `canonical_revenue_service.ledger_freshness` (new) —
       `check_sync_coverage` + `freshness_block` + `ledger_staleness`, the
       same three steps every closed-won figure is judged by;
     - Google Ads spend: `analyze_coverage` over the durable
       `google_ads_spend_coverage` ledger for the last 7 complete account days
       (`revenue_repository.fetch_spend_coverage`, plus
       `fetch_spend_coverage_recency`, new, for the newest verified chunk).
  3. **What needs fixing**: the `crm_data_to_fix` block the certification
     stored, passed through.
- **System page** (`#/system`, admin only, clean chrome): "Can you trust the
  numbers?", "Is the data up to date?", "What needs fixing" and a described
  list of the advanced tools. The fix card separates records the sales team
  corrects **in HubSpot** from blockers **on our side** (the sync's own state),
  names which periods are hidden, dates itself by the last check, and links
  into the CRM Truth Exceptions workbench.
- **Sidebar**: Admin is now `System` plus a collapsed `Advanced tools` group
  holding the six technical pages. Nothing was removed; a deep link to a folded
  page opens the group. Items hidden from non-admins stay hidden when it opens.

**What it does not do.** It decides no publication. The pass wording is scoped
to what the six audits check — won deals, customers, revenue and the Campaigns
page's lead and SQL counts — and says on the page that ad spend, the Channels
page, Leads and search terms are not re-checked, and that a record that never
reached HubSpot cannot be seen. The 26h limit decides only whether a recorded
verdict is still current; it is not a data-freshness rule.

**Known limits, stated.** (1) The verdict row is written when a run ends. If
today's run leaves no row (a failed write, a deploy mid-run, a misfire),
yesterday's pass stays the newest row until it is 26h old — up to about two
hours in which the page says the *latest* check passed, with its age shown.
(2) The row records no build identity, so a deploy after 07:30 is covered by
that morning's verdict until the next run. (3) The spend ledger marks a day
covered when it was fetched during that day, so "complete through yesterday"
before the morning sync includes a partially-fetched yesterday — the same
definition every revenue page uses.

**Independent truth review (before first push).** The `averroes-truth-auditor`
found two blockers in the first implementation, both reproduced through the
real code and both fixed here:

- *B1 — "Nothing to fix" when nobody looked.* An unavailable CRM Truth
  Exceptions audit stores `{deals_with_any_exception: null, by_family: {},
  dataset_blockers: [], windows: []}`; the card read the empty lists as an
  all-clear. Now "Nothing to fix" is said only on a counted zero with no
  blocker and every period recorded as shown; a NULL count is "Unknown".
- *B2 — deals read "Last update succeeded" for a ledger the platform treats
  as stale or unproven.* The first version judged the deal ledger by
  `last_status == "success"` alone: a second, weaker freshness rule.
  `last_incremental_at` is stamped on every attempt and `last_status` is
  shared between modes, so a week-old success, a failed incremental followed
  by a bootstrap re-run, and a bootstrap with no incremental all read healthy
  while every closed-won figure was withheld. The line now comes from the
  ledger's own gate.

Also fixed from that review: spend was read from the best-effort
`google_ads_account_daily_spend` side table (now the coverage ledger); the
pass wording promised spend and "every figure" (now scoped); the sync's own
blockers were listed under a HubSpot heading; "Other periods show their
figures" was printed when none did; an unreadable result said "no check has
been recorded"; a crashed run said "no wrong figure was found"; timestamps
were labelled UTC without being converted; the API doc named a header that
returns 401.

**Tests.** `tests/test_pr_ads_162c_system_page.py` (40 tests, 69 cases), on
production-shaped fixtures: the certification summary production stored on
2026-11-17; contact rows driven through the real `assess`; nine deal sync-state
shapes driven through the real coverage gate, plus a test that the page and
the reader contract reach the same verdict on each; spend through the real
`analyze_coverage`; the unavailable and stale-ledger fix blocks produced by
the real audit code; every reason code in `reason_vocabulary()` required to
have plain words and an owner; the real renderer evaluated in node; the
endpoint through a test client; and the real reads on a live PostgreSQL
(empty database, two runs where the newest decides, a refilled old month that
must not read as the newest fetch, another account's chunk that must not
count). Added to CI's PostgreSQL step. Eight mutations were each shown to fail
the suite: "nothing to fix" without a counted zero; deals ignoring staleness;
deals ignoring coverage; spend always current; newest-run ordering reversed;
exit 0 accepted without every audit holding; a sync reason filed under
HubSpot; an unknown source status rendered healthy.

---

## PR-ADS-162D — retire two pages nothing depended on

**Why.** The owner asked for fewer pages and a simpler product. A page
inventory (every page's loader, endpoints, tables, inbound links and tests)
plus production row counts found two pages with no reader:

| Page | Finding |
| --- | --- |
| Historical Trends (`#/historical-intelligence`) | No sidebar entry and no link from any page — reachable only by typing the URL. Its CPQL "improving / deteriorating" verdict read `COALESCE(confirmed_sqls, 0)` from the legacy `campaigns` snapshot: a missing value became a zero, and it was a third SQL definition. |
| Churn Input (`#/churn-input`) | Stored a monthly churn rate in a local YAML file. `canonical_unit_economics_service` never reads it — LTV, CAC and payback are withheld there for want of recurring revenue, whatever the rate. The Unit Economics help text said the opposite and pointed the reader here; that text is corrected. |

**Deleted, not hidden.**

- `static/app.js`, `static/index.html`, `static/styles.css`: both pages'
  routes, sections, loaders, state, wiring, help/explanation entries and
  styles. An old bookmark falls through `navigate()`'s unknown-page branch to
  the Dashboard.
- `api/server.py`: `GET /api/historical-intelligence`,
  `GET` and `POST /api/admin/churn-input`.
- `analysis/historical_intelligence.py` and its test file.
- `analysis/sql_doctrine_registry.py`: the Historical Intelligence consumer,
  module and row rules, UI rule, CPQL entry and verdict entry. One legacy SQL
  definition fewer.
- `docs/API_CONTRACT.md`: the churn-input sections.

**Kept on purpose.** `connectors/hubspot_churn.py` and
`config/churn_input.yaml` are still imported by the legacy snapshot chain
(`analysis/roas_calculator.py`, `services/unit_economics_service.py`,
`services/roas_snapshot_service.py`). They go with that chain, not with this
page. `analysis/rule_advisor._build_historical_intelligence_block` takes a
dict nobody passes; it is report code and out of scope here.

**Tests.** `tests/test_pr_ads_162d_page_retirement.py` (7 tests, 10 cases):
no route, section, sidebar entry, loader, style or endpoint remains (the
endpoints are checked on the running app: 404 and absent from the route
table); an old bookmark resolves to the Dashboard (the real `PAGES` list
evaluated in node); every remaining sidebar page still routes and no page
section is orphaned; the doctrine registry holds no entry for the deleted
surface and the inventory has nothing unclassified; no text still points at a
retired page. Nine of the ten cases fail on the pre-removal tree. Page-existence
assertions in `test_sidebar_navigation_structure.py`,
`test_help_drawer_and_snapshot_history.py` and
`test_empty_state_page_explanations.py` were turned into retirement
assertions, as PR-ADS-153D did for Flagged Waste Terms.

---

## PR-ADS-162E — delete the legacy local-JSON ROAS chain

**Why.** Since PR-ADS-161E this chain existed to withhold its own output. It
computed revenue, ROAS, LTV/CAC and SCALE/HOLD/FIX/CUT verdicts from local JSON
files; a daily job persisted them; and every route that served them replaced
each figure with "withheld — not canonical". The Reports page showed the result
as a table of dates beside the word "Withheld". One route still published
numbers computed from it: the attribution confidence summary's tier counts.

**Deleted.**

| What | Detail |
| --- | --- |
| Modules | `analysis/roas_calculator.py`, `analysis/unit_economics.py`, `services/unit_economics_service.py`, `services/roas_snapshot_service.py`, `connectors/hubspot_churn.py`, `config/churn_input.yaml` |
| Scripts | `scripts/smoke_test_roas.py`, `scripts/smoke_test_roas_snapshot.py` |
| Routes | `GET /api/reports/roas/campaigns`, `/api/reports/roas/countries`, `/api/reports/roas/snapshots`, `/api/reports/roas/snapshots/latest`, `/api/attribution/confidence-summary` |
| Job | the "daily ROAS snapshot" step of `scheduler/daily.py` (the job is now five steps) |
| UI | the Revenue Snapshot History block on Reports; the Attribution Confidence card on GCLID Attribution; their loaders and styles |
| Config | the `roas:` block of `config/thresholds.yaml` (its only reader was `roas_calculator`) |
| Server helpers | `_withhold_legacy_roas_snapshot`, `_LEGACY_SNAPSHOT_METADATA`, `_LEGACY_REVENUE_WITHHELD` |

**Kept.** `GET /api/reports/unit-economics` (canonical) is untouched.
`analysis/attribution_confidence.py` stays: revenue rows still carry a
confidence label. `analysis/attribution_matcher.py` stays: the revenue
attribution service reads it. `LEGACY_REVENUE_WITHHELD_REASON` stays for the
GCLID readiness diagnostic. The Reports page keeps its advisor-report viewer.

**The reader-cutover audit** (`scripts/audit_customer_revenue_reader_cutover.py`).
Four inventory rows for the deleted surfaces are removed, and `RETIRED_LEGACY`
no longer names files that do not exist. The guard is kept and tightened:

- `READER_ENTRY_POINTS` still names the chain's functions, so a module that
  calls one again is a closed-won reader the inventory must name.
- Check 16 used to read the two deprecated route bodies for a legacy
  computation. It now fails if `api/server.py` names any of the chain's
  functions or a `/api/reports/roas/` route anywhere.

**Tests.** `tests/test_pr_ads_162e_legacy_roas_chain_retired.py` (10 tests, 14
cases): the files are gone; no production module imports a deleted module
(several were imported lazily, inside a function, where a leftover import is a
crash waiting for its code path); each route is 404 on the running app and
absent from the route table; the canonical unit-economics route still
resolves; the daily job has no snapshot step; the UI has no loader, block or
fetch left; the audit's structural checks pass on this tree, and go red when a
legacy call is put back into the server text; the unread thresholds are gone.
Existence assertions for the deleted block in four older test files became
retirement assertions; `tests/test_roas_snapshot_service.py` is deleted with
the service it tested.

**Found by this slice's own test.** `test_08` ran the audit's wording check and
caught a sentence written in PR-ADS-162D: the corrected Unit Economics help
text named ROAS and CAC, which check 18 forbids in any UI text. It is reworded
in 162D.

---

## PR-ADS-162F — the sidebar says what each group holds

**Why.** The owner could not tell what the sections were for. "Command
Center", "Platform Evidence" and "CRM & Revenue" are internal names.

**What changed.** Labels and order only — no page added, removed or renamed,
and no route key changed.

| Group | Pages |
| --- | --- |
| Overview | Dashboard, Channels |
| Google Ads | Campaigns, Search Terms, Keyword Evidence, Countries, Action Queue, Reports |
| Sales Results | Leads, Deals, Revenue by Source, Campaign Revenue & Spend, Country Revenue & Spend, Unit Economics |
| Admin | System, Advanced tools (Data Runs, GCLID Attribution, System Status, Revenue Health, Admin Backfill) |

- Channels moves up beside the Dashboard: it is the all-channel view, not a
  HubSpot detail page.
- Action Queue and Reports move under Google Ads, because both are built on
  Google Ads data only. Whether to keep, rebuild on all-channel data or delete
  them is an open owner decision; placing them here makes their scope visible
  in the meantime.
- The group structure is ready for more ad platforms: a "Meta Ads" or
  "LinkedIn Ads" group would sit beside "Google Ads".

**Tests.** `tests/test_pr_ads_162f_sidebar_groups.py` pins the exact groups,
order and pages, that the old names are gone from the screen, that all twenty
sidebar pages still route, and that admin gating and the Advanced group are
unchanged. Three older tests that named the old groups
(`test_sidebar_navigation_structure.py`, `test_pr_ads_153c_leads_page.py`,
`test_pr_ads_153d_waste_consolidation.py`) now name the new ones; each keeps
its rule (Leads sits in the sales group; the four ad-platform pages sit
together; GCLID Attribution is not in the sales group). Verified in a browser:
seven pages opened from the regrouped menu with no script error.

---

## PR-ADS-162G — delete the Action Queue and Reports pages

**Why.** Owner decision ("delete action queue and reports"). 162F had left the
question open and parked both pages under Google Ads. Neither could be rebuilt
by relabelling:

- **Action Queue** ranked review items from the legacy `campaigns` snapshot
  (`confirmed_sqls` copied from an undated, undeduplicated JSON pull — the
  third SQL definition), from raw `leads` rows windowed on `run_date`, and from
  the legacy `keywords` snapshot. It was Google-Ads-only. Its one canonical
  input, flagged search terms, is the Search Terms → Flagged view.
- **Reports** displayed the latest generated markdown file: the weekly/monthly
  advisor report, whose SCALE/HOLD/FIX/CUT verdicts and CPQL come from that
  same third definition and are reconciled against no page.

**Deleted.**

| What | Detail |
| --- | --- |
| Pages | `#/action-queue` and `#/reports`: sidebar entries, page sections, `PAGES` keys, explanation / help / dataset / dependency map entries, page state, loaders, renderers, filter and button wiring |
| Routes | `GET /api/action-queue`, `GET /reports/latest`, `GET /reports/latest/raw` |
| Server code | `api_action_queue`, `_build_campaign_queue_items`, `_build_waste_queue_items`, `_build_geo_queue_items`, `_build_keyword_queue_items`, `_build_data_quality_items`, `_queue_severity_label`, `_queue_id`, the `_QUEUE_*` constants, `_evidence_window_for_days` (its only caller was the queue), `reports_latest`, `reports_latest_raw`, `_latest_report_path`, and three imports nothing else used |
| JS helper | `fetchText` (its only caller was the report viewer) |
| Styles | the Reports and Action Queue blocks of `static/styles.css` (every class checked unused first) |
| Registry | the two Action Queue consumers, rules `api.aq.campaign`, `api.aq.geo` and `ui.report`, and two decision surfaces in `analysis/sql_doctrine_registry.py` |
| Deploy check | the `/reports/latest` and `/reports/latest/raw` probes in `scripts/verify_live_deployment.py` |
| Docs | their sections and rows in `docs/API_CONTRACT.md`, `docs/24_UI_NAVIGATION_MODEL.md` and `docs/DEPLOYMENT.md` |

An old bookmark to either page lands on the Dashboard (the router sends any
key outside `PAGES` there). Neither key was kept as a redirect: no page is the
equivalent of either.

**Kept, and why.**

- **The report generation pipeline** — `scheduler/weekly.py`,
  `scheduler/monthly.py`, `analysis/core.py`, `analysis/rule_advisor.py`,
  `analysis/advisor.py`, `POST /run/weekly`, `POST /run/monthly`. Out of scope
  for this slice. The jobs still run, still write `outputs/*.md` and still
  email the report; only the in-app viewer is gone. See Follow-up.
- **The high-spend fallback.** `_QUEUE_HIGH_SPEND_USD_DEFAULT` lived in the
  Action Queue block but is read by `/api/waste` and
  `/api/search-term-evidence/flagged`. It stays, renamed
  `_FLAGGED_HIGH_SPEND_USD_DEFAULT`.
- **The focused-term deep link** (`#/search-terms?tab=flagged&term=…`). The
  queue item was the only thing in the app that generated it, but it is a URL:
  a saved or shared link still sets it, and two 153D tests pin its encoding.
  The mechanism is unchanged; its notice now reads "Filtered to … by the link
  that opened this view" instead of naming the Action Queue.

**Dashboard cards.** Two Overview decision cards opened the Action Queue.

| Card | Was | Now | Why |
| --- | --- | --- | --- |
| Investigate — "Paid spend without SQL proof" | `action-queue`, "Open Action Queue" | `roas-campaigns`, "Open Campaign Revenue & Spend" | The card restates the `biggest_waste` signal: campaigns with spend and no SQL or won deal. That signal already named Campaign Revenue & Spend as its evidence. |
| Investigate — "No unexplained paid spend" | `action-queue`, "Open Action Queue" | `roas-campaigns`, "Open Campaign Revenue & Spend" | The same campaign rows, with nothing to flag; the sibling card "Paid spend cannot be fully judged" already opened this page. |

No card content changed. The Dashboard help text no longer sends the reader to
the Action Queue, and three Search Terms strings that named it are reworded or
removed.

**The search-term waste audit** (`scripts/audit_search_term_waste_truth.py`).
Its second half rebuilt the queue's waste items through the deleted server
builder and reconciled them with the flagged view: queue/page spend and reason,
duplicate queue identities, and a review-store outage presenting as an empty
queue. That half is removed, with its output section. Left in place it would
have raised on import of the deleted builder, which `main()` reports as a
failed audit on every run. Every check about the flagged population itself is
unchanged: canonical fact and annotation availability, `truth_state`, complete
read, review-store availability, missing and duplicate identities over the
complete pre-dedup population, row / KPI / pagination reconciliation, and flag
history persistence.

**The doctrine registry.** `ui.report` was bound to `copyLatestReport`, but the
occurrence it classified was the Dashboard's delta-valence map
(`sqls: "up-good"`): a top-level literal the scanner attributes to the function
before it. Deleting that function left the literal unclassified. It is now
bound to the Dashboard Overview consumer as `ui.overview.delta_valence`, where
it belongs. The `_REPORT` consumer and its `analysis/core.py` /
`analysis/rule_advisor.py` rules are untouched.

**Tests.** `tests/test_pr_ads_162g_action_queue_reports_retired.py` (16 tests, 33 cases): no route key, section, sidebar entry, map entry, loader, state,
style or wiring remains for either page; the three endpoints are 404 on the
running app and absent from the route table; no UI text names the Action Queue
or links to either page; **every `target_page` any dashboard service can emit
is a real page** (read from the AST of all `services/dashboard_*_service.py`
against the `PAGES` list parsed from `app.js`, failing on a target it cannot
read, with a counterfactual that goes red on the old `action-queue` target);
an old bookmark resolves to the Dashboard; all 18 remaining sidebar pages
route; the doctrine inventory has nothing unclassified; the report pipeline and
its registry consumer are still there; the waste audit keeps every flagged
check; the reader-cutover audit's structural checks, including the UI wording
check, pass. Sixteen of the eighteen cases fail on the pre-change tree; the two
that pass there are the "kept" guards.

Older tests:

- `test_pr_ads_162f_sidebar_groups.py` — the exact sidebar is 18 pages, and
  Google Ads holds four.
- `test_sidebar_navigation_structure.py`, `test_empty_state_page_explanations.py`,
  `test_help_drawer_and_snapshot_history.py` — the two pages leave the required
  page lists.
- `test_dataset_freshness_ui_contract.py` — the two `action_queue` mapping
  tests become one retirement assertion, plus the same multi-dataset rule on
  Campaigns, which still has two datasets.
- `test_pr_ads_162e_legacy_roas_chain_retired.py` — the Reports loader it
  pinned present is pinned absent.
- `test_pr_ads_134_dashboard_overview.py` — the Investigate target and label.
- `test_pr_ads_153d_waste_consolidation.py` — eleven tests whose whole subject was
  the queue item are removed, each named in a comment where it stood, and
  replaced by one retirement test. Four tests that used the queue as a second
  surface keep every assertion about the page; the priority test reads the same
  reasons from the flagged row the queue copied them from.
- `test_pr_ads_153d_pg_integration.py` — two end-to-end tests keep their page
  half and replace the queue half with a retirement assertion or the page row's
  own `action_needed`. The seven audit gate tests are unchanged and still pass.
- `test_platform_evidence_google_ads_api_cutover.py` — the freshness map was
  required to name the legacy `google_ads_api/campaigns` dataset, which only
  the two deleted pages mapped to. It now requires the key the Campaigns page
  has used since PR-ADS-143, `google_ads_api/canonical_spend`.
- `test_pr_ads_156_evidence_freshness.py::test_28c` — it counts the readers of
  the legacy `keywords` snapshot in `api/server.py` to prove the scheduled
  legacy writes are still needed. One of the two it counted was the queue's
  keyword builder. The bound is now `>= 1`, pinned to the aggregated keyword
  endpoint; zero is still what re-opens the question.

**Follow-up.**

1. **Report generation still runs and still emails.** `scheduler/weekly.py`
   and `scheduler/monthly.py` build the advisor report
   (`run_lead_quality`, `run_campaign_truth`, `compute_ngram_findings`,
   `generate_*_report`) and `scheduler/delivery.py` sends it by SendGrid when
   its keys are set. No page shows it any more. It is the remaining owner of
   the third SQL definition. The owner has ordered Reports deleted; removing
   the generation and the email is the next slice.
2. **Those two jobs are NOT only report code.** They are the only writers of
   inputs live pages read, and these steps must be kept (or moved) when the
   report steps are cut:
   - `run_waste_detection` + `write_waste_terms` — the flagged state on
     Search Terms → Flagged;
   - `record_flag_history` (weekly only) — first/latest flagged dates, and the
     waste-truth audit fails without it;
   - `write_geo`, `write_keywords`, the GCLID coverage snapshot and GCLID
     match — the Countries page, Dashboard keyword themes and GCLID pages.
   Removing the jobs outright would also leave the `weekly` / `monthly`
   monitoring cadences warning forever.
3. **The legacy `campaigns` snapshot has no EVIDENCE reader left** (the
   queue's campaign builder was the last), but it is still a registered core
   freshness dataset: `services/freshness_service.py`, `/api/datasets/freshness`,
   the status war room and `system_status_service.CORE_DATASETS` count its
   rows. `db.writers.write_campaigns` must not be cut before the dataset is
   unregistered there, or System Status reports a stale core dataset.
4. The legacy `keywords` snapshot keeps one in-product reader: the Dashboard
   Campaigns keyword themes (`fetch_keyword_theme_snapshot`).
   `GET /api/keywords` also reads it and has no caller in the UI — an orphan
   endpoint for a later slice. An audit count and the freshness row count read
   it too.
5. Text that still describes the two pages and was left alone in this slice:
   comments in `scheduler/weekly.py` and the `search_term_review` DDL comment
   in `db/schema.py`; `analysis/legacy_source_guard.py`'s note on the
   `write_keywords` allowlist ("four live consumers" — one remains);
   `docs/38`, `docs/44`, `docs/PR_TEMPLATE.md` and `docs/DEPLOYMENT.md`
   passages; and the `view_reports` permission in `api/auth.py`, which nothing
   uses now. `docs/34` §16 is corrected in this slice.
6. The waste-truth audit reconciles counts only. Without the queue's second
   read it holds no spend assertion; summing row spend against the KPI is the
   cheap addition for a later slice.

**Independent truth review (before the first push).** The
`averroes-truth-auditor` found no blocker and ten corrections, applied here:
the follow-up list above was rewritten (the `campaigns` snapshot is still a
registered freshness dataset; the weekly and monthly jobs are the only writers
of live flagged-view inputs; the report is still emailed); `docs/34` §16
listed two deleted checks; the new registry rule for the Dashboard valence
literal is bound to its one pattern, with a test that it binds exactly one
occurrence; the "kept checks" test now runs the real `_validate` and breaks
one input per check (the string-presence version passed with every check
disabled); the Dashboard target test no longer waves through a computed local
named `target_page`; the legacy-keyword-consumer test asserts the Dashboard
call site and names `/api/keywords` as an orphan; and the audit's summary
sentence says it reconciles counts, not spend.

## PR-ADS-162H — remove report generation and its email; keep the jobs' data steps

**Why.** Owner decision ("delete action queue and reports"). 162G deleted the
Reports page and left the generator running: the weekly and monthly jobs still
built the advisor report and emailed it. Production, read before this slice:

- **Weekly job** (`scheduler/weekly.py`, Mondays 04:00 UTC). Killed by Render at
  the 512 MB memory limit about two minutes after it starts, every Monday since
  at least 2026-10-19. Its last recorded success was 2026-06-01. Its `geo` and
  `keywords` writes do land each Monday (latest `run_date` 2026-11-16), so the
  kill comes after those writes and before waste detection completes — in the
  keyword-facts / 60-day search-term sync / waste-detection region, not in the
  report steps.
- **Monthly job** (`scheduler/monthly.py`, 1st of the month 05:00 UTC).
  Completes. Its email has failed every month (`delivery_attempted = true`,
  `delivery_success = false`).
- `waste_terms` and `search_term_review` hold 0 rows. The `campaigns` snapshot's
  latest `run_date` is 2026-11-01, written by the monthly job.
- Nobody receives the report and no page shows it.

**What this does NOT fix.** Removing the report does not stop the weekly job
being killed. The kill happens in the data steps this slice keeps, before the
report steps were ever reached, so the weekly job will go on dying at the same
point. Flagged-term detection (`run_waste_detection` → `write_waste_terms` →
`record_flag_history`) stays unfed until the memory problem is solved — a
larger instance, or streaming the 60-day search-term sync and the waste
analysis instead of holding the rows in memory. That is an open owner
decision, not something this slice settles. A killed run also never reaches
`finish_run`, so its `runs` row keeps the `failed` it was inserted with; the
`weekly` cadence keeps reporting failures, correctly.

**Removed.**

| What | Detail |
| --- | --- |
| Job steps (both jobs) | `run_lead_quality` (wrote `outputs/lead_quality.json`), `run_campaign_truth` (wrote `outputs/campaign_truth.json`), the `write_campaigns` step with its `google_ads_api/campaigns` sync batch, `compute_ngram_findings`, `generate_weekly_report` / `generate_monthly_report`, `deliver_report` |
| Files | `scheduler/delivery.py`, `analysis/advisor.py`, `analysis/rule_advisor.py` |
| `analysis/core.py` | `run_lead_quality`, `run_campaign_truth`, `determine_verdict`, their helpers (`_clean_campaign_name`, `_is_real_campaign`, `_canonicalise_campaign_name`, `_CAMPAIGN_CANONICAL`, `_HUBSPOT_SOURCE_PSEUDONAMES`) and the four status lists only they used (`IN_PROGRESS`, `UNKNOWN`, `JUNK`, `WRONG_FIT`) |
| Monthly failure paths | "Advisor returned no report path" and "Report file not found": a run that makes no report cannot fail for want of one |
| Readiness | `analysis.advisor` and `analysis.rule_advisor` in `api/server.py` `_REQUIRED_MODULES`; the SendGrid, advisor-mode and advisor-file checks in `scripts/healthcheck.py` and `scripts/phase1_readiness.py` (the preflight failed without SendGrid keys) |
| Deploy config | `SENDGRID_API_KEY`, `REPORT_SENDER_EMAIL`, `REPORT_RECIPIENT_EMAIL`, `ANTHROPIC_API_KEY` in `render.yaml`; those and `ADVISOR_MODE` in `.env.example`; `sendgrid` in `requirements.txt` |
| UI | the "Report: outputs/…" text after a manual run on the Scheduler page |
| Registry | the report-pipeline consumer and the optional LLM report-writer consumer; rules `report.core.module`, `report.core`, `report.rule_advisor.module`, `report.rule_advisor`, `report.advisor` and `api.module`; two report decision surfaces; the `doctrine.third_snapshot_population` conflict (its producer and its publisher are both gone) |

Every deletion was preceded by a search for other importers. The search found
three that an import scan cannot see, because they name the modules as strings:
the readiness endpoint's module list and the two preflight scripts. All three
are corrected, and the new suite reads string constants for that reason.

**Kept, and why.**

| Step | Why |
| --- | --- |
| Google Ads pulls, HubSpot pulls | inputs of everything below |
| `write_leads`, `write_deals` | the legacy `leads` / `deals` tables and their `hubspot/contacts`, `hubspot/deals` freshness |
| `write_geo`, `write_keywords` | legacy snapshots live pages still read (PR-ADS-162G follow-up 2 and 4) |
| `sync_keyword_daily_facts`, `sync_recent_search_terms` | the durable keyword facts and the canonical search-term sync |
| `run_waste_detection` + `write_waste_terms` | the flagged state on Search Terms → Flagged |
| `record_flag_history` (weekly only) | first / latest flagged dates; the waste-truth audit fails without it |
| `run_gclid_match` + `write_gclid_attribution`, `write_gclid_coverage_snapshot` | the GCLID pages and their two freshness datasets |
| `start_run` / `finish_run`, the `runs` row, run types `weekly` / `monthly` | the two monitoring cadences |

Local JSON saves, each kept because something still reads the file:

| Save | File | Reader |
| --- | --- | --- |
| `google_ads_save(campaigns, None, keywords, geos)` | `data/ads_campaigns.json` | `scheduler/daily.py` anomaly baseline; `revenue_attribution_service` diagnostic fallback |
| | `data/ads_geos.json` | `revenue_attribution_service` diagnostic fallback |
| | `data/ads_keywords.json` | none found; written by the same call and left as it was |
| `google_ads_save(None, search_terms, None, None)` | `data/ads_search_terms.json` | `connectors/gclid_match.py` |
| `hubspot_save(contacts, deals, crm_summary)` | `data/crm_contacts.json` | `connectors/gclid_match.py`, `analysis.core.run_waste_detection`, `revenue_attribution_service` fallback |
| | `data/crm_deals.json` | `connectors/gclid_match.py` |
| | `data/crm_summary.json` | none found; written by the same call and left as it was |
| `gclid_save(gclid_result)` | match and coverage files | unchanged; not report plumbing |

`db.writers.write_campaigns` is **not** deleted: `scripts/backfill_windsor.py`
(the manual admin backfill) still calls it. `analysis.core.QUALIFIED` stays:
`services/revenue_attribution_service.py` imports it. `anthropic` stays in
`requirements.txt` although nothing imports it now — the test client's HTTP
dependency is not listed in that file, and this package's own dependencies are
the likeliest place CI gets it from. That was not verified against a clean
install, so the line is left rather than risk the blocking job.

Step labels are renumbered `1/4` … `4/4` in both jobs: Google Ads pull, HubSpot
pull and durable writes, waste detection, GCLID match.

**Run status — a behaviour change, stated plainly.** The jobs were recorded
`success` once the report step was reached. A data step that failed on the way
was logged inside a `try`/`except` and the run still ended `success`: a
`write_geo` that persisted nothing, a search-term sync that reported not-ok, a
GCLID match that raised. With the report gone the data steps are the job, so
the run record now states what they did:

| Status | When |
| --- | --- |
| `success` | every kept step **reported** landing (see "Known gaps" below) |
| `partial` | the run reached its end, its run row exists, and at least one kept step did not land; `failed_step` lists them, `error_message` says why |
| `failed` | the run could not continue: a Google Ads or HubSpot pull raised, waste detection raised, or (monthly) a connector snapshot is missing — unchanged; OR the run reached its end with no run row (usually an unreachable database), because a run with no durable record cannot show that anything landed |

A step "did not land" when its writer wrote nothing although rows were fetched (`persistence_succeeded`: 0 of N — the writers return `len(rows)` or 0), its sync returned `ok: false`, `record_flag_history`
returned `available: false`, the coverage snapshot wrote 0 rows, no run row
could be created, or it raised. `partial` here means what it means everywhere
else: monitoring shows the cadence yellow and does not advance
`last_success_at`. Expect this on the first run after deploy if any of those
steps has been failing unnoticed: the monthly job has been recording `success`
with no evidence about them either way.

One gap is left and named: `write_waste_terms` returns nothing and never
raises, so a failed insert there is still invisible to the job.

`report_path`, `delivery_attempted` and `delivery_success` stay as columns (no
schema change). Both jobs write `NULL`, `FALSE`, `NULL`. The daily pulse still
records the path of its own JSON result.

**The callers.** The jobs return their outcome
(`{run_type, status, failed_steps}`) instead of a report path.
`POST /run/weekly` and `POST /run/monthly` republish it; they used to answer
`success` for anything that did not raise, which for the monthly job — which
records a failed step and returns without raising — included failed runs. An
outcome the endpoint does not recognise is `failed`. The scheduled wrappers in
`api/scheduler.py` log the outcome the same way instead of "succeeded". The
Scheduler page shows a partial run as a warning naming the steps. Job ids and
schedules are unchanged; the display names are "Weekly Data Refresh" and
"Monthly Data Refresh".

**The `campaigns` snapshot is no longer a dataset.** With its writer
unscheduled the table gets no new rows, and a registered core dataset nothing
fills can only report stale. It is unregistered everywhere it was treated as
live:

| Where | Change |
| --- | --- |
| `services/freshness_service.py` | the `campaigns` entry removed from `DATASET_FRESHNESS_CONFIG`; `RETIRED_DATASETS` added, naming `(google_ads_api, campaigns)` and why |
| `/api/datasets/freshness` | the pair leaves the known list (derived from the config). The endpoint also lists every `sync_state` row it does not recognise, with a default 8-day threshold — and production holds that row — so a retired key is excluded from `datasets` and the counts and returned under `retired_datasets` with its last recorded sync and no status |
| status war room | row counts and statuses are config-driven, so the table is no longer counted; a retired row no longer dates its source's last sync |
| `services/system_status_service.py` | removed from `CORE_DATASETS` (the source of the "Campaigns data is stale" blocker), `PIPELINE_DEPENDENCIES` and the Google Ads source rollup; `PAGE_PIPELINE_IMPACT` for Dashboard and Campaigns now names `canonical_spend` |
| `static/app.js` | `PAGE_DEPENDENCIES` for Dashboard, Campaign Revenue & Spend and Unit Economics, the matching `dependsOn` chips, the dataset label and the dataset → page link now use `google_ads_api/canonical_spend` |
| `scripts/audit_production_reality.py` | the table is no longer audited against an 8-day threshold |

`services/dataset_keys.py` is unchanged: `campaigns` is still a valid sync
dataset key, stamped by Mailchimp and by the manual backfill. The table is not
dropped. The Campaigns page's dataset, `canonical_spend`
(`google_ads_campaign_daily_spend`), is untouched and still reported.

**The doctrine registry.** Besides the removals above, the
`campaigns snapshot writer` consumer and the `campaigns.cpql_usd` CPQL entry
are kept and rewritten, because the writer and the column still exist: both now
say the only caller is the manual backfill, whose rows carry no
`confirmed_sqls`, so it stores a NULL CPQL. The static inventory reports no
unclassified occurrence and no registry problem.

**Tests.** `tests/test_pr_ads_162h_report_generation_retired.py` (31 tests, 84 cases):

- the three files are gone and no production module imports, names as a
  string, or calls anything from them;
- neither job function calls a report function, `deliver_report` or
  `write_campaigns`, or stamps a `campaigns` batch — read from the AST of the
  job function, not from the file's text;
- both job functions still call every kept step exactly once, stamp exactly
  the seven sync batches that remain once `campaigns` is removed, keep every
  JSON save, and keep their run type;
- each job, run with every external call stubbed: all steps landing is
  `success` with the three report columns `NULL` / `FALSE` / `NULL`; each kept
  step broken one at a time (17 ways) is `partial` with the step named; each
  step the run cannot continue past is `failed`;
- the manual-run endpoints and the scheduled wrappers republish `success`,
  `partial` and `failed`, and treat a missing or unrecognised outcome as
  `failed`;
- the `campaigns` snapshot is not a freshness dataset, `canonical_spend` is,
  and Mailchimp's `campaigns` key is not swept up;
- the status war room, over a database holding a 40-day-old `campaigns` sync
  row and an empty `campaigns` table, raises no blocker and is `ok` — and the
  same database with the old registration restored is `error`, so the first
  result is not an accident;
- the doctrine inventory is clean and the registry points at no deleted code;
  the reader audit's structural checks, including the UI wording check, pass.

Seventy-four of the 82 cases fail on the pre-change tree (measured by copying
the file into a worktree of the parent commit). The eight that pass there are
guards on things this slice keeps: the JSON readers, the weekly job's three
`failed` paths, a raising run reported `failed` by both endpoints, the
monitoring cadences, and the reader audit's structural checks.

Older tests:

- `test_rule_advisor_doctrine.py`, `test_rule_advisor_ngram_block.py`,
  `test_core_campaign_truth.py` — removed. Each imported only deleted code.
- `test_core_lead_quality.py` — reduced to the cases about `QUALIFIED`, which
  survives; the cases about the four deleted status lists and a test-local copy
  of the deleted junk-rate formula are removed, and the removal is asserted.
- `test_pr_ads_162g_action_queue_reports_retired.py::test_11` — it pinned the
  generator as untouched; it now states what this slice removed and kept.
- `test_pr_ads_156_f1_freshness_truth.py::test_13b` — its weekly / monthly half
  asserted the n-gram hand-off to the advisor. It now asserts that no n-gram or
  report call remains and that waste detection still receives the availability
  flag. The daily half is unchanged.
- `test_pr_ads_156_f2_stale_analysis_and_durability.py::test_16` and the CI
  workflow — `test_rule_advisor_doctrine` leaves the blocking contract step;
  the new suite takes its place.
- `test_search_terms_followup_fixes.py` — the weekly baseline case loses its
  stubs of deleted functions. It still fails the documented way
  (`OSError: Missing required Google Ads env vars`).
- `test_system_status_truth.py`, `test_system_status_war_room.py`,
  `test_canonical_freshness.py`, `test_dataset_freshness_endpoint.py`,
  `test_dataset_freshness_ui_contract.py`, `test_empty_state_page_explanations.py`,
  `test_platform_evidence_google_ads_api_cutover.py` — fixtures and required
  keys that named the retired `campaigns` dataset use `canonical_spend`. Every
  assertion about the mechanism is kept; the freshness-endpoint file gains a
  case for the retired row.

**Left alone.**

1. `outputs/waste_report.json` is still written by `run_waste_detection` and
   now has no reader. The 156-F2 tests read it to prove what the function did.
2. `config/thresholds.yaml` keeps keys only the deleted code read:
   `campaign_verdicts.scale.*`, `campaign_verdicts.fix.zero_sqls_with_intent_mismatch`,
   `lead_quality.early_lead_grace_days`, `confirmed_cut_markets`.
3. `db.writers.write_campaigns`, the `campaigns` table and the admin backfill's
   `campaigns` dataset.
4. `docs/PHASE1_PRODUCTION_READINESS.md` carries a banner instead of a rewrite.
   Older audit documents still describe the report as it was.
5. The follow-ups 4, 5 and 6 of PR-ADS-162G are unchanged.

**Independent truth review (before the first push).** The
`averroes-truth-auditor` drove both real job functions against a throwaway
PostgreSQL with only the connectors stubbed and found **no blocker**. It
confirmed `partial` reaches every surface unchanged — the job's return, both
POST endpoints, the `runs` row, the run-history file, `/api/runs`, monitoring
(yellow, `last_success_at` not advanced), per-dataset sync batches, canonical
freshness and the Scheduler page — and that no kept step was removed,
reordered or given different arguments. One finding was fixed here:

- *`partial` when nothing landed.* With the database unreachable the weekly
  job reached its end and recorded `partial` with five failed steps and no
  run row. `run_outcome` now answers `failed` when the run row itself could
  not be created. Tests `09a`, `11` and `11b` cover it; removing the rule
  fails three cases.

Also from that review: a test now pins the kept steps' windows and batch types
(three literals could be changed with every test still green), and the wording
above was corrected ("reported landing"; "wrote nothing", not "fewer rows").

**Known gaps — `success` is only as true as what each step reports.** Not
introduced by this slice, and not fixed by it:

1. **A swallowed HubSpot error still ends `success`.**
   `connectors/hubspot_pull.pull_paid_search_contacts` logs a non-429 API error
   and returns `[]`; `persistence_succeeded([], 0)` is true. With an expired
   token the run is `success`, `hubspot/contacts` is stamped fresh,
   `data/crm_contacts.json` is overwritten with `[]` and a 0-of-0 coverage
   snapshot is stored. `pull_deals_with_gclid` swallows per-contact failures
   the same way. The Google Ads pulls raise and are not affected. Fix: make the
   connector raise. This is the most important follow-up.
   **Fixed in PR-ADS-163A (below):** the legacy pulls raise
   `HubSpotPullIncompleteError`, and both jobs record `failed` and store
   nothing. Gap 4 still applies to that failed run.
2. `db.writers.write_waste_terms` returns `None` and never raises, so a failed
   insert there is invisible; the waste batch records `len(items)`.
3. `start_sync_batch` returning 0, or `finish_sync_batch` returning False, is
   not noted: the run ends `success` with no freshness stamp for that dataset.
4. **`failed` does not reach the `runs` table when a pull raises.** The run
   row is first written after both pulls, so a run that fails in step 1 or 2
   is `failed` in the run-history file and the POST response but has no row;
   monitoring, which reads the table, keeps showing the previous outcome.
   Fix: insert the row at `start_run`.
5. If the leads/deals block raises after the row exists, the job drops its run
   id: the return and the history file say `partial` while the row keeps the
   `failed` it was inserted with and no `finished_at`. Low reachability (the
   writers catch database errors); documented in `test_09`.
6. The durable row for a partial run is asserted through a stubbed
   `update_run`. The reviewer's PostgreSQL probe showed it is correct; no test
   in the repository drives it to a real row yet.

**Found in passing, own PR.** `services/freshness_service.py` names
`deals.close_date` and `gclid_attribution.matched_at`, columns `db/schema.py`
does not define. On a schema built from that file the war room's row count
raises inside one transaction and every later statement fails silently:
"Scheduler Latest Runs" reads "no run records found" and `rows_in_window` is
empty for `deals` and every dataset after it. Present before this slice and
unchanged by it. Not checked against production's actual schema.

**Leftovers for a later slice,** by how much they can mislead an operator:
`hubspot/deals` dependency chips on pages that read the deal ledger;
`WINDSOR_API_KEY` / `WINDSOR_ACCOUNT_ID` still required by both preflights
for a retired provider; the monthly required-files check still demands
`ads_search_terms.json`; `outputs/waste_report.json`, `data/ads_keywords.json`
and `data/crm_summary.json` are written with no reader; dead
`config/thresholds.yaml` keys; `anthropic` in `requirements.txt` (kept: here
it is what pulls in the test client's HTTP dependency — replace it with an
explicit test dependency rather than deleting it); `write_campaigns` and the
`campaigns` table; the manual backfill stamps `windsor/campaigns`, which the
freshness endpoint still lists.

## PR-ADS-162I — the Channels page, redesigned

**Why.** Owner request: "revamp channels page ui and ux just like the dashboard
or make it better". The page was one table. It answered "how many", and left
the reader to work out which channel brings the SQLs and which converts.

**What changed — presentation only.** `/api/channel-funnel` is untouched apart
from the wording of two notes. Every figure on the page is a count that
endpoint published, or a share of two of them. The page still decides nothing
about publication: a withheld funnel shows the word and the reason and no
figure, ranking, control or table.

| Part | Detail |
| --- | --- |
| Window | Four buttons (30 / 60 / 180 days, all time) replace the select and the Refresh button. They are exactly the windows the API accepts |
| Totals strip | Contacts acquired, then each stage reached with its share of contacts, on the dashboard's `dash-outcomes` cells |
| Two rankings | For one chosen stage (SQL by default): "Where your SQLs come from" (count and share of all) and "Which channels convert best to SQL" (share of the channel's own contacts) |
| Small samples | A channel with fewer than 30 contacts keeps its rates, carries a "small sample" mark, and is ranked below the channels with enough contacts to compare. In production's 180-day window Email Marketing converts 5 of 9 (55.6%), the highest rate on the page; it no longer reads as the best channel |
| Table | Same columns and figures as before, with a bar per rate. Click a column to sort. A channel's sources are collapsed under it and open on click |
| Unknown counts | A count the payload does not carry renders as a dash. The old page rendered `NaN` |
| Failed load | Clears the cached funnel, so the previous window's figures cannot stay on screen under a new window's button |
| Notes | "canonical evidence" and "unmodified" replaced with plain wording in `STAGE_NOTE` / `CHANNEL_NOTE` |

**Not changed.** The cohort rule, the stages, the channel names, the
publication verdict, the freshness gate. No spend, deal, revenue or customer
figure is on this page; "Customer stage" is still contacts.

**Tests.** `tests/test_pr_ads_162i_channels_page_revamp.py` — 20 cases that
evaluate the real functions from `static/app.js` in `node`. The existing
`tests/test_pr_ads_162a_channel_funnel.py` passes unedited. Checked in a real
browser (Playwright): sorting, stage comparison, expanding sources, window
buttons, no page error, no horizontal scroll at phone width.

**Known gap, found while checking this page against HubSpot (2026-11-18).**
For contacts created in October 2026 the stored funnel and HubSpot agree
exactly on reached-SQL (70) and reached-Opportunity (46). The contact count
does not: 564 stored, 557 in HubSpot. The 7 extra no longer exist in HubSpot —
deleted or merged — and the sync, which reads modified contacts, never learns
of a removal. 30 such contacts are stored today. Removing them is the next
backend slice; until then "Contacts" on this page can run slightly high.

## PR-ADS-163B — one unreadable row count must not blank the rest

**Why.** The 162H review found `services/freshness_service.py` naming two
columns that `db/schema.py` does not create. Read against production before
this slice (2026-11-18):

- `deals.close_date` and `gclid_attribution.matched_at` do not exist
  (`information_schema.columns`).
- Every request to `/api/datasets/freshness` logged
  `row count query failed for dataset=deals … column "close_date" does not
  exist`, then `current transaction is aborted, commands ignored until end of
  transaction block` for `gclid_attribution`, `gclid_coverage_snapshots`,
  `mailchimp_campaigns`, `mailchimp_reports`, `mailchimp_audiences`, and
  finally for `latest sync_batch query`.

So six datasets reported "row count unavailable", and **no** dataset had its
latest sync batch read — the input that separates "the source returned zero
rows" from "the window is empty". One wrong column name cost every read
registered after it. The war room runs the same loop and failed the same way, without logging,
and its run-history reads sit last on that transaction: all four "latest run"
tiles were empty. `gather_dataset_window_counts` has the same loop. The
Mailchimp status reads only its own three tables, so there the defect was
latent — it would have needed one of those three entries to be wrong.

The cause is general, not a typo. PostgreSQL aborts a transaction on its first
error. A `try/except` around each query catches the exception and leaves the
connection unusable for the next query. The readers were written as if each
read were independent; on one cursor they are not.

**Why no test saw it.** `tests/test_dataset_freshness_endpoint.py` drives the
endpoint with a mock cursor whose failing table raises and whose next statement
succeeds. A real transaction does not do that.

**Changed.**

| What | Detail |
| --- | --- |
| `db/isolated_read.py` (new) | `execute_isolated(cur, query, params)`: runs the statement inside a savepoint, rolls back to it on error, re-raises. A failure costs only the read that raised it; the caller still records that read as unknown |
| Four readers | `/api/datasets/freshness`, `/api/system/status-war-room`, `system_status_service.gather_dataset_window_counts`, `mailchimp_status_service._dataset_freshness_map` use it for every row count, the latest-batch reads and the run-history reads. The war room now logs a failed read instead of dropping it |
| `deals` registry entry | `date_column` `close_date` → `run_date` (the date a snapshot row is written under, as for the other run-snapshot tables). Its `page` is now `None` |
| Page impact and core set | `PAGE_PIPELINE_IMPACT["deals"]` removed and `deals` dropped from `CORE_DATASETS`: both tied the Deals page to the legacy `deals` snapshot, which that page does not read. Without this, fixing the row count would have made the war room report the Deals page **blocked** on a table that feeds nothing on it |
| `last_batch_row_count` in the war room | A dataset with no batch on record returned `0`; it returns `None` |
| War room run tiles | A `partial` run had the same grey as "never ran"; it has its own warning style |
| `gclid_attribution` registry entry | `date_column` `matched_at` → `created_at`: when a match was first recorded, and the date the GCLID Attribution page windows on. Not `updated_at` — every re-confirmation stamps that, so a run that only re-touched old rows would read "fresh with data" beside a page showing none |
| War room "Incremental" tile | Read `run_type = 'incremental'`, a value no scheduler writes; the job persists `scheduler.incremental_sync.RUN_TYPE` (`daily_incremental_sync`). The tile was empty on every request while the job ran daily. It now reads the persisted run type and shows the recorded status, `partial` included |
| Old mock cursor | Now models the abort: after a failed statement every later one fails until `ROLLBACK TO SAVEPOINT`. Its assertions are unchanged |
| `_call_endpoint` in the same file | Imports `psycopg2.sql` and `api.server` before `patch.dict("sys.modules", …)`. The patch evicted modules first imported inside it; a re-imported `psycopg2.sql` made the next real-database test fail with `got Composed instead` |

**Tests.** `tests/test_pr_ads_163b_freshness_row_counts.py` — 11 cases, every
one on a real PostgreSQL cluster with the real schema, added to CI's PostgreSQL
step and to its did-run list. The first 9 fail on `main`; the pre-fix run
reproduces the production log line for line. Case 10 fails when the Deals page
mapping is put back. Case 04 pins the `created_at` basis with a match that was
recorded 200 days ago and re-confirmed today. Case 01 is the standing guard:
every registered `(table, date_column)` must exist in the schema `init_db()`
builds.

**What production will show after this ships** (computed from today's rows,
not yet observed):

- `gclid/matches`: 213 rows first recorded in the 60-day window → "fresh with
  data".
- `gclid/coverage_snapshots` and the Mailchimp datasets: a real count instead
  of "row count unavailable" (Mailchimp tables hold 0 rows).
- `hubspot/deals`: **"empty success"**, shown on the two technical status
  pages only. The war room's HubSpot source card will read "warning" for it.
  That is true, and it is a finding, not a regression — see below. No page is
  reported blocked by it.
- Every dataset now has its latest batch read, for the first time.

**Found, not fixed here.**

1. **The legacy `deals` table is dead.** The
   table has one writer (`db_writers.write_deals`) and no reader anywhere in
   the code. Its newest row is 2026-05-25. Since 2026-05-26 the `hubspot/deals`
   sync has recorded 179 daily and 6 monthly `success` batches with 0 rows
   each, and no weekly batch has written a row either. `static/app.js` still lists `hubspot/deals` as a
   dependency of the Dashboard, Deals, Campaign Revenue & Spend, Country
   Revenue & Spend and Unit Economics pages, though every figure on them comes
   from the canonical deal ledger (gated separately by
   `canonical_ledger_stale`). Next slice: retire the legacy deal pull
   (`pull_deals_with_gclid`, `write_deals`, its step in the daily,
   incremental, weekly and monthly jobs and the backfill) together with its
   registry entry and those dependency lists.
2. **The daily incremental sync has ended `partial` every day since
   2026-09-22** (60 runs), each time for `hubspot/sql_coverage_gaps`. The open
   post-boundary incident count went from 334 to 1,401 on 2026-11-17, the day
   the contact scan was rebuilt (PR-ADS-161I). The status is honest; a status
   that never changes is also one nobody reads.
3. The latest-batch read takes the newest batch of any status, so an abandoned
   `running` batch can stand in for the latest finished one. That read never
   succeeded in production before this slice, so this is reachable for the
   first time.
4. **Contacts deleted or merged in HubSpot stay in the funnel.** Checked
   against HubSpot on 2026-11-18: 30 stored contacts no longer exist there (7
   of the 564 created in October). The sync reads modified contacts and never
   learns of a removal. Reached-SQL (70) and reached-Opportunity (46) for
   October matched HubSpot exactly.

## PR-ADS-163A — legacy HubSpot pulls fail loudly instead of returning a truncated list

**Why.** PR-ADS-162H "Known gaps" item 1. Five legacy pulls in
`connectors/hubspot_pull.py` could finish without having read their result and
still hand back a list. With an expired HubSpot token, `scheduler/monthly.py`
recorded the run `success`, stamped `hubspot/contacts` fresh, overwrote
`data/crm_contacts.json` with `[]` and stored a 0-of-0 GCLID coverage snapshot.
An empty list from a failed first page is also what a month with no leads
returns; nothing downstream could tell them apart. (The incremental-sync half
was checked once against a throwaway PostgreSQL with the real writers: on the
parent commit `hubspot/contacts` and `hubspot/deals` ended `success` over zero
rows and their `sync_state` rows were stamped. That was a one-off local check.
It is not in the repository.)

**What this is, and is not.** A pull now raises `HubSpotPullIncompleteError`
on an API error, a transport error, an exhausted retry, or a response it
cannot read. It does **not** prove that a pull which returns has read every
record: these pulls page by offset and never read HubSpot's `total`, so a
record deleted or re-sorted while the scan runs can still shift a page — the
PR-ADS-161I failure. See "NOT covered" 1.

**What each pull did, and does now.** The connector imports
`hubspot.crm.contacts.ApiException`. The SDK generates a separate
`ApiException` for every API package with no common base, so that `except`
never caught an error from the deals API. That is why the five did not all
fail the same way.

| Pull | Before | Now |
| --- | --- | --- |
| `pull_paid_search_contacts` | non-429 API error: logged, `break`, returned the pages read (`[]` on page one). 429: slept 4 s and retried with no limit | raises `HubSpotPullIncompleteError`; 429 retried 3 times per page (2 s, 4 s, 8 s), then raises |
| `pull_paid_search_contacts_in_range` | the same | the same |
| `pull_all_contacts_in_range` | the same | the same |
| `pull_closed_won_deals_in_range` | a deals-search error was not caught: it propagated as the raw SDK exception, including a single 429 (no retry). The swallow was one level down: `_fetch_primary_contact_for_deal` caught every error and returned `None`, so a won deal whose contact lookup failed came back as a deal with `gclid=None` — "no click evidence" | a deals-search error raises `HubSpotPullIncompleteError`, 429 retried as above. A contact lookup is retried up to 3 attempts and then raises; `gclid=None` now only means the contact was read and has none. A deal whose lookup answered `results: []` is still returned unattributed |
| `pull_deals_with_gclid` | an association read that failed 3 times: logged, contact skipped, its deals missing from the list. A deal read that failed: raw SDK exception, no retry on 429 (the handler written for it never matched) | raises `HubSpotPullIncompleteError` when any GCLID contact's associations or deals cannot be read after 3 attempts. A 429 on a deal read is now retried; deals are collected per attempt, so the retry returns each deal once |

Four rules apply to all five, and to the sources pull below:

- **A 404 is an answer, not an outage.** It is raised without a retry and
  without the 2 s + 4 s sleeps. It still raises: returning `gclid=None` for a
  deal whose contact read answered 404 would claim "no click evidence".
- **A 200 that is not an association list is not "nothing associated".**
  `payload.get("results", [])` read a response with no `results` key as an
  empty one, and a reference with no object id was skipped; the list came back
  short with no error. Both raise, as does a response that announces a second
  page (these readers request one page; pagination is still the `PR-ADS-028`
  TODO). An explicit `results: []` is still a valid empty answer.
  `_association_object_ids` is the one parser.
- **A refused connection or a timeout is the same named error.**
  `MaxRetryError` and `ReadTimeoutError` escaped as raw urllib3 exceptions.
  They are caught (`HUBSPOT_SDK_READ_ERRORS`) and raised as
  `HubSpotPullIncompleteError` with the original as `cause`. On a search page
  they are not retried, like any other non-429 error. **A TLS failure is not
  covered:** SDK 13.0.0 turns a urllib3 `SSLError` into `ApiException(status=0)`
  and then fails in its own error handling with a raw `AttributeError`. The
  pull does not return, so the step still fails, but not under the named
  error; it is not caught, because catching `AttributeError` would hide real
  defects.
- **A search page with no `results` list raises.** For a 200 whose body has no
  `results` key the SDK returns `results=None`; the pulls died on a bare
  `TypeError`. `_legacy_search_page` raises the named error instead.
- **Every SDK read has a timeout.** The SDK applies none unless given one, so
  a HubSpot that stopped answering held the pull open without limit. Each
  `do_search` and `get_by_id` these pulls make passes
  `_request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS` (30 s, what the REST
  calls already used). The limit is per request, not per pull.

`HubSpotPullIncompleteError` carries `pull` (the function), `records_read`
(how many records it had read, which are discarded) and `cause`. It is not a
subclass of `HubSpotRetryableError`, so no existing per-deal handler can catch
a whole-pull failure. `HUBSPOT_API_EXCEPTIONS` names both SDK exception
classes. The unused `_retry_on_rate_limit` decorator is deleted: it wrapped
`pull_paid_search_contacts`, whose inner loop caught every `ApiException`
first, so it never ran.

On a run where every read answers, each pull returns the same records in the
same order as before; the clean multi-page cases in the new suite pass on the
parent commit too. The requests differ in two ways: the SDK calls carry a
timeout, and the sources pull makes fewer of them (next paragraph).

**The sources pull, `pull_closed_won_deals_with_sources_in_range`.** Two
defects of the same class, found by the independent review of the first
version of this slice (`0ed399f`):

- It read its deals through `pull_closed_won_deals_in_range`, which resolves
  every deal's primary contact for click evidence the sources pull never
  used: a second association read and a full contact read per deal, thrown
  away. Once that lookup raised instead of returning `None`, a failure in a
  lookup nobody read failed `hubspot/source_classification` and a
  source-backfill chunk. The won-deal search is now one generator,
  `_iter_closed_won_deal_pages`, shared by both pulls; the sources pull reads
  deal id, close date and amount from it and makes no primary-contact read.
- It returns a deal whose source lookup failed as `lookup_failed: True` with
  no contacts. `source_attribution_service` skips such a deal.
  `scheduler/incremental_sync._sync_source_classification` never read the
  flag: it attributed the deal from its empty contact list, upserted it
  `unclassified / no_classified_contact` over the stored attribution, and
  ended `success`. The step now fails when any pulled deal carries the flag,
  before a deal row is prepared or written.

**The callers.** Every caller was found by a search for the names, including
imports inside functions. Two needed a code change.

| Caller | On a pull that raises |
| --- | --- |
| `scheduler/monthly.py:141-142` | the existing step-2 handler (`:331`) records the run `failed` in the run history and returns `{status: failed}`. `hubspot_save` is never reached, so `data/crm_contacts.json` keeps its previous content. No `hubspot/contacts` or `hubspot/deals` batch is opened, `write_leads` is not called, and the run stops before the GCLID match, so no coverage snapshot is written. No code change |
| `scheduler/weekly.py:89-90` | the existing outer handler (`:518`) records `failed` and re-raises. The same four things do not happen. No code change |
| `scheduler/daily.py:51` | as for a failing Google Ads pull: the outer handler (`:206`) updates the run row it inserted to `failed` and re-raises. No contacts batch, no `write_leads`, no saved result. No code change |
| `scheduler/incremental_sync.py:768` `hubspot/contacts` | the step's own handler finishes its batch `failed` (so `sync_state` keeps its previous watermark — `finish_sync_batch` only moves it on `success`), appends to `errors`, and returns `{status: failed}`; `write_leads` is not called. The run is `partial` or `failed` by `_overall_status`, and the `runs` row gets that exact word. No code change |
| `scheduler/incremental_sync.py:830-833` `hubspot/deals` | the same, for either pull. `write_deals` is not called |
| `scheduler/incremental_sync.py:894` `gclid/matches` | the same. `write_gclid_attribution` is not called |
| `scheduler/incremental_sync.py:1090-1110` `hubspot/source_classification` | **changed.** A contact pull or a deal page that raises was already handled as above. What was not: a deal returned with `lookup_failed`. The step now raises on it (`:1104`), so the batch is `failed`, the error names the deals, and `upsert_deal_source_attribution` is not called for that pull. The contact classifications are written before the deal pull and stay written — see "NOT covered" 4 |
| `scripts/backfill_hubspot.py:40,46` | the existing `pull_error` path: the chunk's batch is finished `failed`, the error is listed, the chunk is not counted complete, the result is `failed` (no chunk completed) or `partial`, and the backfill run row is `failed`. No code change |
| `services/revenue_recovery_service.py:250,272` | **changed.** The error was already listed and the run `partial`, but the chunk was added to `completed_chunks` whatever happened, so a resume skipped it for good. A chunk is now recorded complete only when both phases finished, the rule `source_attribution_service` already applies |
| `services/source_attribution_service.py:893,913` | existing: `contacts_ok` / `deals_ok` false, chunk not completed, error listed; a `lookup_failed` deal is skipped and keeps the chunk incomplete. No code change |
| `services/mailchimp_audit_service.py:334` | existing handler: identity bridge unavailable, outcomes withheld. It used to build the bridge from a truncated list and call it available. No code change |
| `db/writers.py` | names `pull_deals_with_gclid` only in the `write_deals` docstring; it does not call a pull |

No HubSpot or Google Ads write is added. The test portal has no write API and
replaces every `requests` verb except GET with a stub that fails.

**Expect this after deploy.**

- PR-ADS-154C recorded 429 responses in the legacy `hubspot/deals` association
  scan in production. A contact that is still rate limited on its third
  attempt (after 2 s and 4 s) used to be skipped; it now fails the
  `hubspot/deals` step, and the incremental run is `partial`. That dataset is
  registered non-authoritative and does not move truth readiness, but the run
  status will say `partial` on such a day.
- **In the weekly and monthly jobs the same exhausted 429 on one contact
  aborts the whole run.** `pull_deals_with_gclid` is in step 2, and a raise
  there ends the job: the geo, keyword, search-term, waste and GCLID steps are
  skipped, no `runs` row is written (162H gap 4), and the Google Ads JSON
  snapshots from step 1 are already saved. Before this slice the run went on
  with that contact's deals missing and ended `success`. This is a known
  consequence, not an oversight: the alternative is a `deals` table that is
  silently short. The legacy deal pull is scheduled for removal — nothing
  reads the `deals` table, and its newest production row is dated 2026-05-25 —
  which removes the step rather than softening the error.
- A lookup that answers 404 for one object (a deal's contact, a contact's
  associations) fails its dataset on every run until that object is gone from
  the window or readable again. It used to be recorded as "no click evidence"
  or "no deals".
- `hubspot/source_classification` will end `failed` on a day when one deal's
  source lookup cannot be read. It used to end `success` and overwrite that
  deal's stored attribution with `unclassified`.

**Tests.** `tests/test_pr_ads_163a_hubspot_pull_fails_loudly.py` — 49 tests,
108 cases. Every case drives the real connector function over a fake HubSpot
portal that honours the filters each pull sends, pages by `after`, raises the
SDK's own exception class for the API that was called, and fails a chosen
request once, a few times, or for good — with a status, a transport error, or
a 200 that is not an association list. No pull is stubbed. The portal does
not change during a scan, so none of this exercises offset paging over moving
data.

- the four search pulls: 401 on page one raises and returns nothing; 500 on
  page three raises with `records_read == 200`; a persistent 429 raises after
  exactly `MAX_RETRIES + 1` attempts with the 2 / 4 / 8 s schedule; a transient
  429 is retried; the 429 budget is per page; a clean three-page pull returns
  exactly the expected records; a transport error raises the named error with
  the urllib3 exception as `cause`;
- `pull_deals_with_gclid`: all reads succeeding returns every deal; a failed
  association read (401, 500, 429, connection error) raises after 3 attempts;
  a 404 raises after one; a deal that cannot be fetched raises; a transient
  failure loses nothing and duplicates nothing; an unread second page raises;
  four shapes of unusable 200 raise; `results: []` is a valid empty answer;
- `pull_closed_won_deals_in_range`: a deal whose contact cannot be read raises
  instead of returning `gclid=None` (429, 500, 404 without a retry, a
  transport error after 3 attempts, four shapes of unusable 200); a transient
  lookup failure keeps the GCLID; a deal with no contact is still returned;
- the sources pull: it makes no primary-contact read and one association read
  per deal; a failed page raises under its own name; a failed source read, a
  404, a transport error, an unusable 200 and a second page each mark that
  deal `lookup_failed` and no other;
- every SDK read made by these pulls carries the 30 s timeout;
- no `except` handler in these functions contains `break` or `return`, and
  none retries inside a `while` loop (read from the AST);
- the real weekly and monthly job functions (the 162H harness, with the two
  pulls, the lead summary and the JSON save put back as the real functions)
  over an expired token, a 500 on page three, and one unreadable contact: the
  run is `failed`, `write_leads` and `hubspot_save` are never called, the
  snapshot file is byte-for-byte what it was, no batch finishes at all, and
  no coverage snapshot is written. A control runs both jobs over a healthy
  portal to `success`;
- the real daily pulse: raises, the inserted run row is updated `failed`, no
  batch, no leads, no saved result;
- the real incremental sync (the `_patch_all_datasets_success` fixture with the
  HubSpot pulls put back): with an expired token the run is `partial`, the run
  type written is `daily_incremental_sync`, and each of the four legacy
  datasets is `failed` with its batch finished `failed` and no source date;
  a mid-scan 500 persists none of the pages before it; one unreadable contact
  fails only `hubspot/deals`; one unread source lookup fails
  `hubspot/source_classification` with no deal attribution written and leaves
  `gclid/matches` `success`; one failed click-evidence read fails
  `gclid/matches` and leaves `hubspot/source_classification` `success`; a
  healthy control is `success`;
- the backfill script, revenue recovery (including the resume that now
  re-reads the failed chunk) and the Mailchimp identity bridge.

Measured by copying the file into a worktree of each commit:

- **On the parent commit (`c3d04a1`): 81 cases fail, 23 pass.** Of the 23,
  thirteen are controls (clean pulls, healthy runs, the valid empty answer,
  the connector's `lookup_failed` contract). The other ten are transient
  failures the old code already recovered from: a 429 on a contact search was
  retried (without limit, which is what the persistent-429 cases fail on),
  and a transient association failure was retried up to 3 attempts.
- **On the first version of this slice (`0ed399f`): 30 cases fail** — the
  cases for the review findings above: the unread source lookup written as
  `unclassified`; 211 primary-contact reads by the sources pull and a failed
  click-evidence read failing the source step; a 404 attempted 3 times; an
  unusable 200 returning 108 or 109 of 110 deals, a won deal with
  `gclid=None`, or a source lookup marked successful and empty; a raw urllib3
  exception; SDK calls with no timeout.

Older tests changed. No assertion about behaviour was removed or relaxed.

- `test_pr_ads_114_revenue_recovery.py::test_connector_normalise_prefers_direct_then_url_gclid`
  and `test_pr_ads_117_revenue_by_source.py::test_connector_association_failure_via_node_not_needed`
  replaced connector functions by plain assignment and never restored them
  (`_fetch_primary_contact_for_deal`, `pull_closed_won_deals_in_range`), so the
  connector kept a lambda for the rest of the session and no later test could
  reach the real won-deal pull. Both use `monkeypatch` now. The new suite
  checks, before every test, that the functions it drives are the real ones.
- The same `test_pr_ads_117` test stubbed `pull_closed_won_deals_in_range` as
  the sources pull's source of deals. That seam moved: it stubs
  `_iter_closed_won_deal_pages`, returning pages of raw deals, and also
  asserts the deal id.
- `test_pr_ads_114_revenue_recovery.py::test_connector_pulls_closed_won_by_closedate`
  read the dealstage / closedate filter out of the first 2,500 characters of
  `pull_closed_won_deals_in_range`. The filter lives in
  `_iter_closed_won_deal_pages` now; the same four assertions are made there,
  plus two that the pull reads its deals from that function and runs no
  search of its own.

No existing test asserted the old behaviour (a failed pull returning `[]` or a
partial list with the caller carrying on).

**NOT covered.**

1. **Offset paging over moving data.** The four search pulls page by `after`
   offset and never read HubSpot's `total`. A record deleted, created or
   re-sorted while the scan runs can shift a page, and a pull that returns
   normally can still have skipped a record — exactly the PR-ADS-161I
   landmine. Nothing here detects it. The fix there was keyset paging plus a
   `total` check; these pulls have neither.
2. **A failed weekly or monthly run still has no `runs` row** (162H gap 4).
   The row is first written after both pulls, so the run this slice now fails
   is `failed` in the run-history file and the POST response, and monitoring,
   which reads the table, keeps showing the previous outcome. This slice makes
   that gap easier to reach. Fix: insert the row at `start_run`, as the daily
   pulse does.
3. `iter_contacts_modified_since._search` still retries a 429 without limit
   and passes no timeout. It raises on every other API error, and was out of
   scope here. `pull_deals_for_ledger` catches the contacts `ApiException`
   around a deals search: with the installed SDK that handler never runs, so
   its 429 retry and its `complete: False` answer are unreachable for an API
   error, which propagates instead and fails the ledger step. Loud, but not
   what the code says it does. Neither was touched.
4. **`_sync_source_classification` writes the contact classifications before
   it pulls the deals.** When the deal pull raises or carries a
   `lookup_failed` deal, the batch is `failed` over contact rows that landed.
   The rows are correct; the batch understates what was stored.
5. **A failed revenue-recovery phase leaves zeros in the chunk detail.** The
   chunk is not completed and the error is listed, but `chunks[n]` still says
   `contacts_read: 0` or `deals_found: 0` with no flag that the figure was
   never measured. `revenue_recovery_service` and `source_attribution_service`
   also answer `partial` when every chunk failed (`failed` needs zero chunks
   attempted).
6. A 5xx or a transport error on a search page is not retried; it fails the
   step until the next run. Only 429 has a retry budget there. The 30 s
   timeout bounds one request, not a pull.
7. The batch and watermark assertions read recorded `finish_sync_batch` and
   `update_run` calls. No test here drives a real `sync_state` or `runs` row.
8. Association pagination is not implemented; a second page raises instead of
   being dropped. Retry budgets differ by call: a search page gets 4 attempts
   (2 / 4 / 8 s), a per-contact or per-deal lookup 3 (2 / 4 s).
9. 162H gaps 2, 3, 5 and 6 are unchanged.

## PR-ADS-164A — the Deals and Revenue by Source pages say what they count, and never read "unavailable" as "none"

**Why.** The owner reported that the Deals page and Revenue by Source "show
different data". A read-only audit found they do not: both read the same
canonical won-deal population (`canonical_revenue_service.load_won_deals`,
all-source scope, same business window, same `deal_amount_usd`). They looked
different because of how each presented it:

- **Deals called an all-source list "attributed to Google Ads"** (subtitle and
  footnote), and showed a Google Ads spend card above it.
- **Two dead columns.** "Company" rendered the server's hard-coded `None`
  (the ledger stores the deal's name, not an employer) — a dash on every row.
  "Attribution" read `match_source`, which the canonical row has not carried
  since PR-ADS-153E-B — "Needs Review" on every row.
- **"Exact GCLID Matches"** labelled the count of deals in the
  `gclid_attributable` scope, which is a subset of the list, not a match rate.
- **Withheld revenue was a dash** on Deals and "Withheld" with its reason on
  Revenue by Source.
- **A stale or unreadable ledger read as "there are none"** — the important
  one. Deals recognised only `deal_ledger_status == "database_unavailable"`
  and defaulted every other value to "available"; since 153E-B the server
  sends the canonical reason instead, so that branch was unreachable and
  `canonical_ledger_stale` rendered "No closed-won deals found … the ledger is
  available". Revenue by Source rendered its empty `groups` as "No attributed
  source activity", and its drawer as "No attributed closed-won client detail
  found".

**What changed.** Presentation and labels, plus one pass-through key. No deal,
customer or revenue figure is published or gated differently.

| Where | Change |
| --- | --- |
| Server — `build_revenue_deals`, `build_revenue_by_source`, `build_source_platform_detail`, and the mart's deal and source views | Carry `closed_won_population`: the canonical `{value, status, reason}` of the won-deal count behind the rows, taken from `reader_row` / `subset_metrics` and passed through unchanged. The drawer's is for its own platform's deals. It is `None` on the campaign and country views, and in a drawer whose contact evidence could not be read. |
| UI — one rule for all three surfaces (`closedWonPopulationState`) | **rows**: published, and exactly that many deals arrived. **empty**: published with value 0 — the only state that may say "No closed-won deals closed in this period". **blocked**: withheld, unavailable, an unrecognised status, no verdict, or a published count the rows contradict — the word and the reason through `canonicalMetricHtml`, no row, no figure, no "none". |
| Deals page | Subtitle and footnote say every source, by close date. Columns are Deal, Closed, Country, Campaign, Source, Won Revenue — `deal_name` and the stored `acquisition_group` (the grouping Revenue by Source uses), with the dead two removed. The tile is "GCLID-attributable Deals". The count, revenue and average go through `rowMetricHtml` / `canonicalValueHtml`; a deal with no amount reads "Unavailable — no proven USD amount". The spend card is gone. `martDealLedgerSummary` no longer re-adds the rows client-side (it counted an unpriced deal as zero). |
| Revenue by Source | The blocked state replaces the whole body, spend card included. The drawer treats a `source_health.status` other than `ready` as unreadable, not as empty. |

**Not changed.** Which deals are members, how the count, revenue and customers
are gated, the reader contract, `summary.metrics`, the mart's top-line, the
Dashboard's own Deals tab, the campaign and country drawers, any page outside
these two. No customers figure was added to the Deals page. The API still
sends `exact_gclid_count` (an alias of `gclid_attributable_deals`) and
`company: None` on each deal row; the UI reads neither.

**Tests.** `tests/test_pr_ads_164a_sales_results_states.py`, 44 cases. The UI
cases load the whole real `static/app.js` in a node `vm`, run the real loaders
and render from payloads produced by the real builders over a stubbed ledger:
current with deals, current with no deal in the window, stale, unreadable,
coverage unproven, an undated won deal, and one deal with no amount. Against
the parent commit 43 fail and 1 passes (the audit check 18 guard, which holds
on `main` and is there to keep it holding). `test_20` bypasses the guard in a
mutated `app.js` and shows the stale ledger reading as "none" again.

Existing tests edited, each because it asserted what this slice removes: the
old subtitle, the two dead columns and the deleted badge, the old card wording
(113); the spend card on the Deals page (126, 128); the
`"database_unavailable"` branch and the client-side re-aggregation (127). Two
fixed character spans were widened where a renderer grew (113, 133).

**Known gaps.** Not introduced here, not fixed here:

1. **The leads half of Revenue by Source has no verdict.**
   `build_revenue_by_source` reads `fetch_source_leads(...)["rows"]` and
   ignores `available`, so an unreadable classification table renders as 0
   leads. And when the ledger is withheld the page shows no leads at all
   (`groups: []`), although `_unavailable_revenue_by_source` says the leads
   structure "is still shown where it is readable".
2. **`sourceGaSqlsChip` escapes HTML it built itself.** When the Google
   Ads-source SQL count is null the chip prints the literal text
   `<span class="dash-unavailable">Unavailable</span>`. It is an SQL surface
   in the doctrine registry, so it was left for its own slice.
3. The Deals page prints the close date as the stored ISO timestamp.
4. The Deals page still shows the mart's diagnostics stack, whose messages are
   about Google Ads spend and FX coverage.
5. The fail-closed rule lives in `static/app.js` and its tests; no
   `scripts/audit_*` gate enforces it in production.

## PR-ADS-163C — contacts deleted or merged in HubSpot leave the funnel

**Why.** The canonical contact table `hubspot_contact_funnel` is filled by a
modified-since sync (`hubspot_contact_funnel_sync_service`,
`iter_contacts_modified_since`). A contact that is deleted in HubSpot, or
merged into another, is never "modified": it stops being returned, and nothing
tells the sync so. Its row stayed and was counted by every funnel and cohort
read. Measured against production and HubSpot on 2026-11-18:

- contacts created in October 2026: 564 in the table, 557 in HubSpot. The 7
  extra ids return nothing from a HubSpot search on `hs_object_id`;
- table-wide, 30 stored contacts no longer exist in HubSpot (all 30 confirmed
  absent by a search on their ids). 26 were created in the last 180 days; one
  carries SQL evidence;
- reached-SQL and reached-Opportunity for that October cohort otherwise match
  HubSpot exactly (70 and 46), so the error was the population, not the stages;
- the same 30 contacts have 30 rows in `contact_source_classification` (the
  "Leads" counts on Revenue by Source) and 98 rows in the legacy `leads` table.

**What runs now.** One step in the daily incremental sync,
`hubspot/contact_reconciliation`, right after the contact sync and before the
post-boundary gap detection (the first step that reads the funnel). In one
line: search → two strikes → direct read confirmation → backlog and
trailing-window limits → move.

**The residual, stated plainly.** A contact is removed only when HubSpot's
search omitted it on two checks at least 20 hours apart — the second in a run
where HubSpot answered every batch — AND a
direct read of that contact by id returned 404 (or returned its merge
survivor naming it). What remains is HubSpot answering 404 for a contact that
exists.

1. **Ask.** `connectors.hubspot_pull.check_contacts_exist` reads every stored
   contact id and asks HubSpot which still exist: one CRM search per batch of
   at most 100 ids, one filter (`hs_object_id IN <batch>`), `limit` 100, only
   `hs_object_id` requested, 0.25 s between calls, the 30 s request timeout and
   `_legacy_search_page`'s bounded 429 retry. Read-only: no other endpoint.
2. **Three answers, not two.** An id HubSpot returned is *existing*. An id in a
   batch HubSpot answered and did not return is *missing*. An id whose batch
   had no usable answer is *unknown* and is left exactly as it was. A batch is
   usable only when the response has a `results` list, HubSpot's `total`
   equals the number of results and is no larger than the batch, there is no
   further page, and every result is an id that was asked for, once. Ids are
   compared as numbers. A stored id that is not canonical (not all digits, or
   digits with leading zeros) is never sent and is unknown on its own. After
   three unanswered batches in a row the remaining batches are not sent.
3. **Strike.** Each missing id gets a row in `hubspot_contact_absence`
   (`first_missing_at`, `last_missing_at`, `checks`, `last_run_id`), unless
   its funnel row was ingested after this run started. Each returned id has
   its row deleted: it came back.
4. **Confirm.** Every contact this run could remove is read directly by id
   (`confirm_contacts_absent`: `basic_api.get_by_id`, read-only, 30 s
   timeout, 429 retried three times, 0.25 s apart, at most the per-run limit
   of 50 reads). The search index can leave a live contact out of a
   well-formed answer on every run; the object read does not use it.
   - a 404 confirms the contact is gone;
   - the contact itself returned means it EXISTS: the search was wrong. Its
     strike is deleted, the run counts it in `search_disagreements` and is
     `partial` with that reason;
   - a different record returned confirms a merge only if that record's
     `hs_merged_object_ids` names the id asked for; otherwise it is unknown;
   - an error, a timeout or an exhausted 429 is unknown. An unconfirmed
     contact stays, keeps its strikes, makes the run `partial` and does not
     block the others.

   If more contacts are eligible than one run may remove, none is read: the
   valve refuses that run whatever HubSpot says.
5. **Move.** A contact leaves only when ALL of these hold, decided by the
   database in one statement:
   - it was struck by the current run and on a check at least 20 hours
     earlier;
   - its funnel row was last ingested before this run started. A row the
     contact sync wrote while the check was running is newer evidence than
     the check's answer;
   - it has no open row in `sql_post_boundary_incident` (see "Held" below);
   - every batch of this run was answered. A run with an unanswered batch
     records strikes for the answered ones, reports `would_remove`, removes
     nothing and ends `partial`;
   - the direct read confirmed it (the confirmed ids are a parameter of the
     statement; everything else is still decided in SQL);
   - both limits pass.

   The row is moved into `hubspot_contact_funnel_removed` (every funnel
   column, plus `removed_at`, `removal_reason = 'absent_from_hubspot'`, the
   run and the strike history). Its `contact_source_classification` rows move
   to `contact_source_classification_removed`, its strike is deleted and the
   counts are written onto the run row, all in the same transaction.
6. **Refuse, per run.** The valve counts the whole standing backlog: every
   stored contact with two strikes 20 hours apart and no open incident,
   whichever run struck it (`backlog_qualified`). If that is more than
   `contact_reconciliation.max_removals_per_run` (50), or more than
   `max_removal_fraction` (1%) of the table, nothing moves and the run is
   `refused`. The strikes stay. It is not a count of what this run would
   remove: counted that way, a degraded HubSpot answered a few batches a day
   was drained in slices that each fit under the limit.
7. **Refuse, over a trailing window.** The removals already archived in the
   last `removal_window_days` (7) plus what this run would move must not
   exceed `max_removals_per_window` (100) nor `max_removal_window_fraction`
   (2%) of the table; over either, nothing moves and the run is `refused`.
   The per-run limit alone lets a small persistent loss through every other
   day without end.
8. **Warn on day one.** A run that strikes more contacts for the first time
   than the per-run limits allow is `partial` with the reason. Nothing is
   removed on a first strike; the warning is not left for the refusal a day
   later.

`would_remove` is the run's eligible contacts after the direct read cleared
the ones it found; `confirmed_absent` in the run result is how many of them
the read confirmed.

**Held.** A contact HubSpot no longer returns that has an OPEN
post-boundary SQL incident is not removed. Both incident resolvers
(`apply_post_boundary_sql_evidence`, `resolve_post_boundary_incidents`) read
the contact's funnel row; with the row gone the incident could never close and
`audit_sql_coverage_gate`'s `no_open_post_boundary_gaps` would stay broken for
ever. No closing status was added (a new status would silently stop
blocking). The consequence is stated, not hidden: **a held contact stays in
the funnel and stays counted by every funnel and cohort read until its
incident is resolved**, it keeps its strikes, the run reports
`held_open_incident` and is `partial` on every run while any contact is held.
Held contacts are NOT part of `backlog_qualified`: they cannot be removed, so
they are not weighed by the valve. The consequence: if eight contacts vanish
and four are held, the valve sees four, and with a limit of five the other
four are removed although eight disappeared at once; if the four incidents
are later closed by hand, those four go too. The first-strike warning (step
8) does count all eight on day one.
A deleted contact's incident cannot resolve by itself, because the exact SQL
date it waits for can no longer arrive from HubSpot; closing it is a manual
decision this slice does not make.

Every run is a row in `hubspot_contact_reconciliation_runs` (`started_at`,
`finished_at`, `status`, `contacts_stored`, `contacts_checked`, `batches`,
`batches_unknown`, `missing`, `newly_missing`, `returned`, `would_remove`,
`removed`, `classification_rows_removed`, `backlog_qualified`,
`held_open_incident`, `search_disagreements`, `error`). The row is opened before HubSpot is called; if
it cannot be written, HubSpot is not called. The removal counts are written by
the move's own transaction, so a removal that committed is on its run row even
if the write that closes the run is lost (the row then stays `running`).

| Status | Meaning | On the incremental run |
| --- | --- | --- |
| `success` | every batch answered; strikes and removals recorded | green |
| `partial` | strikes recorded, but not clean: a batch had no usable answer (then nothing is removed), a contact is held on an open incident, the direct read found a contact the search omitted or could not confirm one, or more first strikes than the limits | error, run `partial` |
| `refused` | a limit stopped the removal: the backlog of qualifying contacts is over the per-run limit, or the trailing window's removals would pass its limit | error, run `partial` |
| `failed` | no run record, unreadable ids, no batch answered, or a write failed | error, run `partial` |

A run still `running` in the table is one that did not finish.

**Why a move and not a flag.** About thirty queries in some twenty files read
`hubspot_contact_funnel`. A `removed` flag would have to be honoured by every
one of them, and a reader that missed it would keep counting the contact with
nothing to say so. Moving the row needs no reader to change. Nothing is
dropped: the archive holds the row as it was.

**Why two strikes.** One search answer is one observation of a search index.
A batch that comes back well-formed and empty (`results: []`, `total: 0`) is
indistinguishable from 100 real deletions. Two checks at least 20 hours apart,
with the second in the current run, means one odd answer strikes a contact
and the next good one clears it; it never removes it. A contact whose batch
was not answered in the current run is not removed on old strikes either.

**Why the database decides.** Who qualifies, the valve and the move are one
SQL statement (`db.writers.move_absent_contacts`): a `gate` CTE counts the
backlog and the table, and the `DELETE … RETURNING` feeding the archive
`INSERT` is conditional on it. There is no count read by Python and acted on
later, and the counts reported are the statement's own. Strike instants are
the database's `clock_timestamp()`, taken after the HubSpot read; the
ingest-time guard compares two database clocks (`last_ingested_at` and the run
row's `started_at`).

**A contact that comes back.** The archive has no unique key on `contact_id`
and nothing references it. A contact restored in HubSpot and seen by the
modified-since sync is ingested as an ordinary new funnel row. It carries no
strike (the strike was deleted with the removal), so it would have to be
found missing twice more to be removed again; the archive then holds both
removals.

**System page.** `/api/system/overview` carries `contact_reconciliation`: the
latest run's status and its own reason text (why it is `partial` or
`refused`), start, finish, contacts checked, unknown batches, `would_remove`,
removed, `backlog_qualified`, `held_open_incident`, `search_disagreements`,
plus the number of pending strikes. Facts only; the page does not render them
yet and no figure page changed.

**Calls and time.** For about 22,000 stored contacts a daily run makes 220
search calls and sleeps 219 × 0.25 s ≈ 55 s, plus request time, and at most
50 direct reads (one per contact it is about to remove; about 12 s of pacing
at the limit, normally a handful). A 429 adds at most 14 s of backoff per
batch or read, and three failures in a row end the calling.

**Verified against HubSpot, read-only, on 2026-11-19** (by the coordinator,
not by this code's tests): a search with `hs_object_id IN` and 100 values is
accepted and returned `total: 100`; no stored contact id is non-canonical;
none of the 30 absent contacts has an open post-boundary incident or is a
member of the coverage boundary.

**Tests.** `tests/test_pr_ads_163c_removed_contacts.py`, 64 cases, 41 of them
on a real PostgreSQL cluster with the real schema, funnel writer, sync
service, keyset iterator and readers. The fake HubSpot serves both the sync's
`lastmodifieddate` scan and the `hs_object_id IN` check from one set of
contacts that the tests change between runs; it honours the filters, matches
ids as numbers, reports `total`, answers a read by id from the objects (not
from the search index), and records and refuses any call that is neither. The module is in CI's PostgreSQL step and its did-run list.

- **Before the fix.** On the parent commit `test_30` — two real
  `run_daily_incremental_sync` runs a day apart after HubSpot deletes three of
  300 contacts, then the real channel-funnel read — fails with
  `assert 300 == 297`. It uses only code that existed before.
- **Guards.** Fifty-five mutations, each removing or weakening one guard,
  were applied one at a time to a copy of the tree and the module re-run;
  each failed at least one case. Three of the first review's mutations
  survived their first attempt (the move's own ingest-time guard, the 1% arm
  of the first-strike warning, and held contacts counting against the
  limit); `test_52b`, `test_12` and `test_53b` were added for them.
- **Time.** The service takes the instant of the check as an argument so the
  20 hours are testable; production passes none and the database stamps
  `clock_timestamp()` (`test_21`). The scheduler takes no clock, so the
  scheduler cases (`test_30`, `test_31`, `test_33`) move the recorded strikes
  21 hours back between two runs.

**Independent review of the first version.** The first commit of this slice
was reviewed with executable probes and corrected before merge. What was
wrong, and the case that now fails without the fix:

| Finding | What the first version did | Now | Case |
| --- | --- | --- | --- |
| F1 | the valve counted only contacts struck by the current run, and a run with unanswered batches removed the answered share: a degraded index answered one batch a day lost live contacts in slices under the limit | the valve counts the whole backlog; a run with an unanswered batch removes nothing | `test_50`, `test_51`, `test_13`, `test_33` |
| F2 | a contact restored and re-ingested by the sync between the check's answer and the move was removed | a row ingested since the run started is neither struck nor moved | `test_52`, `test_52b` |
| F3 | a contact with an open post-boundary incident was removed, leaving the incident unresolvable | held, counted, run `partial` | `test_53` |
| F4 | 100 first strikes in one run was `success` | `partial` over the same limits | `test_12`, `test_12b` |
| F5 | a stored id with leading zeros was sent, HubSpot's canonical answer was "not asked for", and its whole batch was unknown on every run | ids compared as numbers; the non-canonical id alone is unknown | `test_08`, `test_09` |
| F7 | a lost finishing write left a committed removal on a `running` row with NULL counts | the move writes its counts onto the run row in its own transaction | `test_54` |

**Second review.** The corrected version was reviewed again.

| Finding | What that version did | Now | Case |
| --- | --- | --- | --- |
| N1 | a search index omitting the same few live contacts on every fully answered run lost them every other day as `success`, with no cumulative limit; a refused backlog that shrank under the limit was removed | direct read confirmation before any removal; trailing-window cap | `test_61`, `test_62`, `test_63`, `test_64`, `test_66`, `test_60`, `test_67` |
| C2 | a contact the sync saw during the check but could not write (rejected as stale) was removed | the direct read finds it | `test_65` |
| N3 | a `refused` or `partial` run did not say why on the overview payload | reason text and the four counts carried | `test_42` |

`test_33[partial]` and `test_13` asserted in the first version that a partial
run removes the contacts of its answered batches. Those assertions encoded F1
and were changed to assert the opposite.

Existing tests touched, both by adding the new step to a fixture that lists
every step of the incremental sync — the edit PR-ADS-156, 160 and 161D2 made
in the same places. No assertion was removed or loosened.

- `tests/test_daily_incremental_sync.py`: `_patch_all_datasets_success` stubs
  the check at its service seam like every other step, and
  `test_datasets_keys_present` expects `hubspot/contact_reconciliation`.
- `tests/test_pr_ads_154_incremental_sync_hotfix.py`: the two healthy-run
  step lists name `_sync_contact_reconciliation`.

Without those edits seven tests failed, all the same way: a healthy-run
fixture met a step it did not describe, which with no database records
`failed` ("run record could not be created") and makes the run `partial`.
Two of the seven (`test_pr_ads_160` 93 and 95) stub steps by the `_sync_`
prefix; the step is named to match and they needed no edit. One
(`test_pr_ads_163a` 28) uses the shared helper above.

**NOT covered.**

1. **The legacy `leads` table.** The 98 rows of the removed contacts stay and
   keep feeding the pre-153C pages that read `leads`. An admin
   `POST /api/audit/sql-truth/repair-classification?apply=true` re-derives the
   classification cache from `leads` and would re-insert the removed
   contacts' classification rows.
2. **Lifecycle history, recovery, incident and boundary tables.**
   `hubspot_lifecycle_stage_history`, the recovery state,
   `sql_post_boundary_incident` and the immutable `sql_coverage_boundary*`
   tables are not written. A contact with an OPEN incident is held in the
   funnel (see "Held"). A removed contact that is a member of the immutable
   coverage boundary stays in `sql_coverage_boundary_contact`; what the
   boundary's membership checks make of that was not traced.
3. **Deals and companies.** Out of scope. A won deal whose stored association
   (`hubspot_deal_contact_association`) still names a removed contact loses
   that contact's row in the closed-won acquisition read
   (`funnel_row_present = false`), which makes the deal's acquisition state
   unresolved rather than wrong. Not measured for the 30.
4. **No freshness dataset.** The check is not registered in
   `DATASET_FRESHNESS_CONFIG` or the canonical freshness statuses (a
   nine-place change). A check that stops running is visible only as a stale
   `started_at` on the System overview payload and as an error on the
   incremental run; nothing withholds a figure because of it.
5. **A merged contact keeps only the survivor's row.** The merged-away id is
   removed; whatever HubSpot folded into the survivor arrives through the
   survivor's own modification. Nothing here links the two ids. Whether a
   search on a merged-away id always returns nothing was observed for these
   ids, not established as HubSpot behaviour; a response that returns a
   different id than was asked makes the batch unknown.
6. **A restored contact that HubSpot does not mark modified** is not seen by
   the sync and stays out of the funnel until something modifies it. Its
   classification row stays in the archive unless it was created in the last
   14 days (the daily classification window).
7. **A legitimate removal over the limit** (a CRM clean-up of more than 50
   contacts, or of more than 1% of the table) is refused every day until the
   limit in `config/thresholds.yaml` is raised. There is no one-off approval,
   and no page shows a `refused` run: it is an error on the incremental run
   and a row in the runs table. Because the valve counts the backlog, strikes
   on contacts whose batch is never answered again also count against it
   until those contacts are answered.
8. **A table under 100 contacts** can never remove one: one contact is over
   1%. A stored id that is not a canonical object id is never sent to HubSpot
   and makes every run `partial`, so nothing is removed while one exists.
9. Only the incremental sync runs the check. The manual
   `POST /api/crm-funnel/sync` and the other schedulers do not.
10. The System page does not render the new block.

**Not reached by the review or by these tests (unverified).**

- HubSpot's real behaviour beyond the three facts above: the search rate
  limit, whether a search on a merged-away id can return the survivor,
  whether restoring a contact changes `lastmodifieddate`, how long a search
  index can lag or be partially lost, and — the one the removal now rests on
  — what a read by id returns for a MERGED-AWAY id: that it returns the
  survivor, and that the survivor's `hs_merged_object_ids` lists the merged
  id in a `;`-separated form, is assumed, not observed. If it does not, a
  merged contact is never confirmed, is never removed, and every run that
  meets it is `partial`: wrong in the safe direction. Everything here was run
  against a fake.
- The other certification audits. Only the SQL coverage gate's incident
  check was examined against a removal; `audit_platform_certification`'s
  other audits were not run over a database with removed contacts.
- The `leads` / `repair-classification` path (item 1): that an applied repair
  re-inserts a removed contact's classification row was read from the code,
  not executed.

**Verified against HubSpot, read-only, 2026-11-19.** A direct read of two of
the absent ids returned a DIFFERENT, surviving contact whose
`hs_merged_object_ids` listed the asked id, `;`-separated (for example
`865430158531;91109773542`): they were merged, not deleted. A direct read of a
live contact returned it under its own id. So the merge rule matches the real
API, and some of the 30 absent contacts are merges.

---

## PR-ADS-165A — retire the weekly job and the Unit Economics page

**Owner decisions (2026-11-19).** "remove the weekly job, this is killing
render each week", and "unit economics is not needed".

**Production facts behind the first.** The weekly data refresh
(`scheduler/weekly.py`, Mondays 07:00 Asia/Amman / 04:00 UTC) was killed by
Render at the 512 MB memory limit every Monday from 2026-10-19 at the latest.
Its last recorded success was 2026-06-01. Its newest row in `runs` is `failed`.
The monthly job (`scheduler/monthly.py`) completes.

### A. The weekly job

**The audit.** Every step of `run_weekly_report`, what it wrote, who else
writes the same thing, and who reads it. "Monthly" is `run_monthly_report`;
"incremental" is the 09:00 daily incremental sync; "daily" is the 06:00 pulse.

| # | Weekly step | What it wrote | Other scheduled writers | Readers | Decision |
| --- | --- | --- | --- | --- | --- |
| 1 | Google Ads pull, 30 days: campaigns, keywords, geo | `data/ads_campaigns.json`, `ads_keywords.json`, `ads_geo.json` | Monthly (same 30 days) | Daily pulse's spend-anomaly baseline reads `ads_campaigns.json`; `revenue_attribution_service` names it as a diagnostic fallback | Deleted. Monthly is the writer |
| 2 | HubSpot pull, 30 days: paid-search contacts, their deals, summary | `data/crm_contacts.json`, `crm_deals.json`, `crm_summary.json` | Monthly | GCLID match and waste detection, both inside the monthly job | Deleted |
| 3 | `start_run("weekly")` / `write_run` | one `runs` row, `run_type = 'weekly'` | none — a retired run type | `/api/runs`, monitoring, the war room | No new rows. Old rows kept as history (below) |
| 4 | `write_leads` | `leads`; batch `hubspot/contacts` | Daily, incremental, monthly | Leads page, Dashboard, CRM funnel | Deleted |
| 5 | `write_deals` | `deals` (legacy GCLID snapshot); batch `hubspot/deals` | Incremental, monthly | No page (163B) | Deleted |
| 6 | `write_geo` | `geo` (legacy snapshot); batch `google_ads_api/geo` | **Monthly only** | Was `/api/geo` — the Google Ads → Countries page (`#/geo`) — which is **cut over to canonical daily spend in this PR** (section C). Left: `revenue_attribution_service` (probe, reconciliation fallback, degraded-mode diagnostic), the admin spend audit, the admin pollution report | Deleted from weekly. Kept in monthly; no page reads it |
| 7 | `write_keywords` | `keywords` (legacy snapshot); batch `google_ads_api/keywords` | **Monthly only** | `/api/keywords` (no page calls it), the Dashboard Campaigns keyword-theme snapshot (latest snapshot, dated), the keyword legacy audit | Deleted from weekly. Kept in monthly (section C) |
| 8 | `sync_keyword_daily_facts`, 30 days | `keyword_daily_facts`; batch `google_ads_api/keyword_facts` | Daily, incremental (30-day window), monthly | Keywords page | Deleted |
| 9 | `sync_recent_search_terms("weekly", days=60)` | `search_terms`; batch `google_ads_api/search_terms` | Daily and incremental (14-day rolling window), monthly (30 days) | Search Terms page, n-grams | Deleted. The 60-day re-read is gone (gap 3) |
| 10 | search-term JSON snapshot | `data/ads_search_terms.json` | Monthly | GCLID match | Deleted |
| 11 | `run_waste_detection` + `write_waste_terms` | `waste_terms`; batch `analysis/waste_terms` | **Monthly only** | Search Terms → Flagged, campaign drawer flag preview | Deleted. Now monthly — threshold changed |
| 12 | `record_flag_history` | `search_term_review.first_flagged_at` / `latest_flagged_at` / reasons | **None.** Weekly was the only scheduled caller | Search Terms → Flagged ("first flagged", "last flagged") | **Moved** to the monthly job, after `write_waste_terms` |
| 13 | `run_gclid_match` + `write_gclid_attribution` | `gclid_attribution`; batch `gclid/matches`; `outputs/` JSON | Incremental, monthly | GCLID Attribution page, revenue reconciliation | Deleted |
| 14 | `write_gclid_coverage_snapshot` | `gclid_coverage_snapshots`; batch `gclid/coverage_snapshots` | **Monthly only** | `/api/gclid-coverage`, `/api/attribution/quality` | Deleted. Now monthly — threshold changed |

So: **no dataset lost its only writer**, and nothing was added to
`RETIRED_DATASETS`. One step lost its only caller (12) and was moved.

**Removed.**

- `scheduler/weekly.py`.
- The scheduler registration, its wrapper `_run_weekly_scheduled`, its
  `_job_state` slot and schedule description (`api/scheduler.py`). `weekly` is
  recorded in `RETIRED_JOB_IDS`.
- `POST /run/weekly`. The Data Runs page builds one trigger button per job
  `/scheduler/status` lists, so the button went with the job id.
- `make weekly`; the module from the readiness and healthcheck module lists
  (`api/server.py::_REQUIRED_MODULES`, `scripts/healthcheck.py`,
  `scripts/phase1_readiness.py`).
- The `weekly` monitoring cadence: `MONITORING_CADENCES`, `RUN_TYPE_CADENCE`,
  `STALE_DAYS_DEFAULT`, `CADENCE_LABELS`, and the `weekly: 8` line in
  `config/thresholds.yaml`.
- The war room's Weekly tile: the `runs` query for it (`api/server.py`),
  `latest_weekly` in `compute_scheduler_summary`, and the tile in
  `static/app.js`.
- The weekly allowlist entry in `analysis/legacy_source_guard.py`.

**Kept, and why.**

- **The flag-history step**, in the monthly job. It is the only writer of the
  flag history the Flagged tab shows. It is local-database only and reads the
  flagged population 200 rows a page — the same read the tab itself does on a
  GET. It runs once, after `write_waste_terms`, and a failure makes the run
  `partial`, exactly as it did in the weekly job.
- **Search Terms → Flagged.** The monthly job writes the same `waste_terms`
  annotations, so the tab is not one that can only ever be empty. It now says
  how fresh a flag can be: waste detection runs once a month, on the 1st, over
  the previous 30 days, so a flag can be up to a month old and a term first
  seen since the last monthly run has not been checked. Spend, clicks and
  dates beside a flag are still from the daily search-term sync.
- **Every dataset.** The four the monthly job alone now writes — `waste_terms`,
  the legacy `keywords` and `geo` snapshots, `gclid_coverage_snapshots` — had
  the weekly threshold of 8 days. Left alone they would read `stale_with_data`
  from the 9th of every month to the end of it. They take
  `MONTHLY_REFRESH_STALE_DAYS` (35, the monthly cadence's own monitoring
  threshold) and carry `refresh_cadence: "monthly"`, republished by
  `/api/datasets/freshness` and by the war room's pipeline rows, and printed
  as "refreshed monthly" in the System Status table. A missed monthly run
  still turns them stale at day 36.

  "Fresh" for these four means "refreshed on schedule", not "recent". That is
  only honest because **none of them is a core dataset or the dataset of a
  page's primary figure**: the legacy `keywords` and `geo` snapshots were
  removed from `CORE_DATASETS` (Keyword Evidence reads `keyword_facts`,
  Countries reads `canonical_geo`), so System Status no longer reads "ok" on
  the strength of a wide threshold over a month-old snapshot.
- **`VALID_SYNC_TYPES` still contains `weekly`.** Old `sync_batches` rows carry
  it.

**Old `weekly` rows in `runs`.** They stay. They are history, never a health
signal:

- `weekly` is in `api.monitoring.RETIRED_RUN_TYPES`. It is not a cadence: no
  failure streak, no `last_success_at`, no staleness, no warning, no severity.
- It is not folded into another cadence. A cadence is one pipeline's verdict
  (PR-ADS-160-F2); folded into `monthly`, old weekly failures would redden a
  healthy job and an old weekly success would hide a broken one.
- `/api/monitoring/status` reports them under `retired_runs`:
  `{retired, reason, last_run_at, last_status, runs_in_history}`. No surface
  renders that block; it is there so the API never leaves "where did weekly
  go" to be guessed.
- `/api/runs` still lists them with their own `run_type`.
- The search-term pipeline verdict asked for "the latest weekly run" to decide
  whether the pipeline had ever run. It now asks for the latest run of a job
  that syncs search terms (`SEARCH_TERM_RUN_TYPES`: daily, incremental,
  monthly).

**What production shows after this ships.**

- Data Runs: four jobs — daily, monthly, daily_incremental_sync,
  platform_certification. No weekly card, no weekly button.
- The monitoring banner: no "Weekly run has failed N times in a row". With the
  three live jobs healthy the severity is green.
- System Status: three run tiles (Daily, Monthly, Incremental). `waste_terms`,
  `keywords`, `geo` and `gclid_coverage_snapshots` read fresh for 35 days after
  a monthly run, each marked "refreshed monthly".
- A monthly run that fails turns the monitoring banner yellow the same day
  ("Monthly run failed on its latest attempt…"), not 35 days later.
- Google Ads → Countries: spend from the daily sync through yesterday, one
  row per country whatever the lead spelling, available from the morning sync
  onward — and "Unavailable" with the reason before it. Never `$0` for a
  window with no rows or an unmatched country.
- No Monday 04:00 UTC memory kill.

**A failed monthly run is never green.** `compute_monitoring_status` turned red
at two failures in a row and stale after 35 days. A monthly run that failed
yesterday after a success 31 days ago has a streak of 1 and is not stale, so
it read `green` with no warnings — while the monthly job is now the sole
writer of four datasets and does not run again for a month. For the cadences
in `LATEST_RUN_MUST_SUCCEED` (`monthly` only) a latest run that is not
`success` is at least yellow, with a plain warning; an unrecognised status
counts (fail closed). The daily cadences are unchanged: one failed morning,
retried tomorrow, is still under the streak.

### B. The Unit Economics page

Deleted, following PR-ADS-162D / 162G: the sidebar entry, the page section,
`loadUnitEconomics` / `renderUnitEconomicsPage` and their state, the two CSS
rules, `GET /api/reports/unit-economics`, and
`services/canonical_unit_economics_service.py`. The page key is gone from
`PAGES`, so `#/unit-economics` lands on the Dashboard through the router's
existing guard. The help text of Deals and Campaign Revenue & Spend no longer
points at it.

It was a registered reader of closed-won figures. Its row is gone from
`INVENTORY` and from `_page_builders` in
`scripts/audit_customer_revenue_reader_cutover.py`; `build_unit_economics` was
added to `READER_ENTRY_POINTS`, so a module that brings it back is a reader
that must be inventoried. `scripts/audit_customer_closed_won_truth.py` never
named it and is unchanged. The SQL doctrine registry held no entry for it.

No other page changed what it publishes. The helpers the page shared
(`rowMetricHtml`, `canonicalMetricHtml`, `canonicalValueHtml`,
`fmtPeriodRatio`, the business-window handler) are still used by the pages
that remain. Average deal value is still published by the Dashboard's Revenue
tab (`dashboard_revenue_service`) and the Deals page
(`revenue_attribution_service`). The all-source "business" block beside an
advertising scope was this page's alone.

That block was the only caller that ever passed `period_ratio_scope_ok=False`
to `reader_row` (period ratios unavailable for a population Google Ads spend
cannot have produced). With the page gone no production code could turn the
rule on, so the flag, `closed_won_truth`'s `scope_compatible` parameter and the
reason `numerator_and_denominator_scopes_differ` were removed rather than kept
with a test as their only caller. Each remaining reader passes spend only
beside a Google Ads-scoped population, as before the flag existed (docs/48 §4
is updated).

### C. The Google Ads → Countries page reads daily canonical spend

**The defect this section fixes, which the first version of this PR would have
caused.** The page read the legacy `geo` snapshot: `/api/geo` ran
`SUM(spend_usd) … WHERE run_date >= NOW() - N days`, and `loadGeo` filled a
country with no spend row as `spend_usd: 0`. In production the newest `geo`
and `keywords` `run_date` is 2026-11-16: the weekly job wrote those two tables
before it was killed each Monday. Retiring it makes the monthly job their only
writer. Monthly run on Dec 1, viewed on Dec 12 with the 7-day window:
`/api/geo` returns nothing, the leads endpoint still returns countries, every
country shows Spend `$0`, "Total Geo Spend $0", under "Fresh with data". On
Dec 20 the 30-day window shows twelve days of spend as the 30-day figure.

**What it reads now.** `GET /api/geo` is served by
`services/country_evidence_service.build_country_evidence` and returns one row
per country with spend and lead figures already joined:

- per-country spend from `google_ads_geo_daily_spend`, which the daily
  incremental sync writes (it replaces each re-fetched range, so an
  overlapping re-sync never adds to a day); converted to USD at each day's own
  FX rate (`fetch_geo_daily_spend_by_country`, now scopable to one account);
- a coverage verdict for the window —
  `{status, covered_from, covered_through, reason, requested_from,
  requested_through, missing_days, missing_ranges, failed_chunks}`;
- the lead figures of `/api/leads/country-summary` for the same evidence
  window, read by the handler and joined in the service.

A spend figure appears only when `coverage.status` is `covered`.

**The gate, exactly.** Every day of the window must lie in a `verified` chunk
of the `google_ads_geo_coverage` ledger (for the configured account) that was
**fetched at or after the end of that account-local day**, and the rows must
be readable.

The second half is the third review's finding. The daily sync fetches a range
ending today and marks the chunk `verified` at about 06:00 UTC, so the ledger
says "verified" for a day with six hours of spend in it. On chunk status alone
yesterday read "covered" from account-midnight until this morning's sync —
holding only what yesterday morning's sync saw — and all day if the sync
fails: `$775` published where the week's spend was `$875`. Now each verified
chunk counts only for the days that had ended when it was fetched
(`settled_through`); otherwise the window is `not_covered`
with reason `spend_not_reread_since_day_ended` and the page says "yesterday's
spend has not been re-read since the day ended". When a sync covering those
days has run and failed, the reason is `spend_reread_failed` and the page says
so instead of pointing at a morning sync that already ran.

The account's time zone is read where the geo sync reads it
(`repo.fetch_account_time_zone`: the zone Google reported with the account's
spend), not from the `Europe/London` constant. With no zone on record the
window is `unavailable` / `account_time_zone_unknown`. (The sync itself falls
back to UTC in that case; this page does not.)

**So the page is not always available, and says so:** spend is published from
the morning sync onward and reads "Unavailable" between account-midnight and
that sync, and all day when the sync fails.

The day arithmetic is the shared `analyze_coverage`, fed the ledger's chunks
clipped to the days each settles; the gap codes are the geo service's own.
This is **not Country Revenue & Spend's gate**. That page divides revenue by
this spend, so it also requires complete campaign-spend coverage, FX coverage
and a geo↔campaign reconciliation within tolerance; this page divides by
nothing and requires geo coverage only, so a window whose geo total does not
reconcile is published here and blocked there. And that page judges coverage
on chunk status alone: **the same part-day hole exists in the shared
`analyze_geo_coverage` / `analyze_coverage` it uses** (and in the campaign
spend ledger read the same way). Not changed here; see Known gaps.

**The window ends yesterday** in the account time zone, not today: today is a
day still in progress. Yesterday is complete only once it was re-read after it
ended, which is what the gate checks — the bound alone does not make it so.
The page prints "Spend is for <from> through <through>, re-read after that day
ended. Today is not included." `all_time` has no requested start: coverage
starts at the earliest verified chunk and the row read is bounded to that same
certified range.

**The join is on the server, on an ISO country code.** The page used to join
spend to leads in the browser on the trimmed country text: leads spelled `UK`
beside spend for "United Kingdom" were two rows and the lead row read `$0` in
a covered window. The first server-side join then resolved both sides through
`country_identity`'s supported-MARKET registry (96 codes) — and production
spend falls in 162 ISO codes, with lead countries stored lower-case
("nigeria", "libyan arab jamahiriya"), so most of the page would have read
"(unresolved location)" or "not matched". Now:

- a spend row is keyed by the ISO code of its Google criterion and shown under
  Google's name. The code is the one any stored row of that criterion carries,
  read over the whole table (`repo.fetch_geo_criterion_identities`). This is
  the fourth review's finding: the sync stores a row with **no** code when its
  country lookup fails that day (and overwrites a code already stored), the
  chunk is still `verified` and the run `success` — so the UAE's £50 moved to
  "(unresolved location)" and the page read "United Arab Emirates $0". The
  criterion id is Google's own id for the location, so a code on another row
  of the same criterion is stored evidence, not inference. Exactly one code on
  record resolves; none, or two that disagree, do not;
- spend whose criterion no stored row names is one "(unresolved location)"
  row, in the total. The payload counts the criteria (`unresolved_spend`).
  Production holds two such criteria, `2192` and `2364` (25 rows, about £11,
  none ever named): a standing state, not only a failed lookup.
  *(PR-ADS-170A: the owner labelled these two — `2192` Cuba, `2364` Iran — in
  `config/geo_owner_labels.yaml`. They read under those countries, marked
  "set by owner". An unlisted code-less criterion is still unresolved.)*;
- lead text resolves through the complete ISO 3166-1 table —
  `country_identity.resolve_iso_country_text` over `analysis/iso_countries.py`
  (249 codes as repo data, no new dependency; ISO's inverted names, former
  names such as "Libyan Arab Jamahiriya", common spellings). Exact matches
  only: bare "Congo", "California", "Dubai" and two-letter text ("CA", "IN")
  match nothing;
- a lead country that names no single country is its own row, "Unavailable —
  country not matched", never `$0`;
- a resolved country with **no spend row** is `$0` only when the window holds
  no unresolved spend. While it does, that spend may be this country's: the
  row reads "Unavailable — some spend in this window has no country"
  (`spend_location_unresolved`) and a notice says how much and that the
  figures shown are not affected. A country **with** a spend row is complete,
  not a lower bound, on the assumption that Google keys one country under
  one criterion. Production on 2026-11-19: 212 coded criteria, 212 codes, no
  code under two criteria and no criterion with two codes. The code does not
  enforce that assumption;
- blanks and placeholders ("Other", "N/A", "(unknown)") are one "No country
  recorded" row, not counted as unmatched countries;
- a merged row shows a top campaign / keyword only when every spelling agrees
  on it — the summary does not carry the counts to recompute it — and
  "Several" when they differ;
- Kosovo: Google reports it under `XK`, which ISO has not assigned. It is held
  apart from the ISO table (`USER_ASSIGNED_COUNTRY_CODES`) and the lead text
  "Kosovo" resolves to it.

`country_identity`'s existing functions and `SUPPORTED_COUNTRIES` are
unchanged: the complete resolver is a separate block of new names, called only
by the Countries service. No revenue page's country grouping moved.

**What the page shows.**

| State | Spend column | Total Geo Spend | Countries Active |
| --- | --- | --- | --- |
| Window covered | the figure; "<$1" for an amount under a dollar. `$0` only for a country that resolved, with no spend row, in a window holding no unresolved spend — otherwise "Unavailable — some spend in this window has no country". "Unavailable — country not matched" for lead text that names no single country; "Unavailable — no country recorded" for leads with none | the total | resolved countries with spend or leads |
| Covered, one country missing an FX rate | "Unavailable — FX rate missing" for that country | "Unavailable" | "Unavailable" |
| Not every day re-read after it ended (each morning before the sync, or the sync failed), a gap, coverage / rows / criterion identities unreadable, no account configured, or no account time zone on record | "Unavailable" for every country | "Unavailable" | "Unavailable" |

In the third state a notice above the map says which days are complete, in
words, and why that is not the window. No spend figure appears anywhere,
including the map. The lead figures are the same as before, summed per
country. A count the server sent is printed as the number it is, `0`
included. When the lead summary cannot be read every lead column — counts,
junk rate, top campaign, top keyword, leads-as-of — reads "Unavailable" in the
table and in the map hover, the map carries no value (it drew 0), and an empty
table says "Lead figures could not be read." rather than "No leads". "Countries
with SQLs" and "High Junk Countries" count resolved countries only. One line
under the notice says lead counts cover a different period from spend —
through today, by lead run date — and the help text no longer invites
comparing them.

"SQLs" on this page is the legacy lead-summary count
(`status_category = 'qualified'`), not the canonical lifecycle or cohort SQL
figure. The column and the card say only "SQLs". Not relabelled here; it
belongs to the page-unification slice.

**Dropped from the page**, because the canonical table holds cost only: the
Clicks and Conv. columns, and "top campaign by spend" (Top Campaign is the
HubSpot lead evidence it already fell back to). They are not filled in from
the monthly snapshot — that would put two periods under one heading. "Last
Run" is renamed "Leads as of": it is the lead run date.

**Gone:** the empty-state text "No Google Ads API geo data has been synced
yet. Run the monthly scheduler". No surface tells an operator to run the
memory-heavy job to refresh a page.

**Registries.** The page's freshness strip and dependency maps key on
`google_ads_api/canonical_geo` (`PAGE_DATASET_MAP`, `PAGE_DEPENDENCIES`,
`datasetRelatedPage`, `PAGE_PIPELINE_IMPACT["geo"]`). Country Revenue & Spend's
JS dependency named the legacy dataset too, though it has read canonical geo
since PR-ADS-124; corrected. In the SQL doctrine registry the "Geo
Intelligence country summary" consumer now says `/api/geo` republishes its
counts per country, and the join (`country_evidence_service._lead_figures`)
is bound to it: it sums the summary's counts across spellings of one country
and defines no SQL of its own.

**Legacy `geo` — kept, not retired.** No page reads it now, but three readers
remain, so it is still written by the monthly job and still registered
(`page: None`, "Geo (legacy monthly snapshot)", not core):

1. `revenue_attribution_service._build_from_db` calls
   `fetch_campaign_country_spend` (a business-window sum over it) as its
   database-reachable probe; as a fallback geo total used only to *test*
   reconciliation when canonical geo has no rows — country spend itself still
   requires canonical rows and complete coverage, so this cannot publish; and,
   only when the canonical campaign-spend table cannot be read, as a
   diagnostic spend column (`spend_source: geo_fallback`, every ratio
   unavailable). **That last use is the same window-sum-over-a-monthly-table
   defect, in a degraded path.** Not fixed here: it sits in the middle of the
   revenue contract, and dozens of tests stub that function.
2. `build_google_ads_spend_audit` (admin) prints it as `legacy_geo_total`.
3. `fetch_campaign_pollution_report` (admin diagnostic) lists campaigns with
   leads and no `geo` row in the window — which will over-report mid-month.

Retiring the table needs those three moved first; that is the next slice.

**Legacy `keywords` — kept.** Readers: `GET /api/keywords` (a day-window sum
over `run_date`, the same defect — but no page calls it; `static/app.js` has
no reference); the Dashboard Campaigns keyword-theme snapshot
(`fetch_keyword_theme_snapshot`, the single latest snapshot with its date, not
a window sum); and the keyword legacy audit. `/api/keywords` was left as it
is: deleting or repointing an endpoint is its own change. Next slice.

### Tests

`tests/test_pr_ads_165a_weekly_job_and_unit_economics_retired.py`, 27 cases,
`tests/test_pr_ads_165a_countries_pg_integration.py`, 32 cases, and
`tests/test_pr_ads_165a_country_identity_complete.py`, 162 cases (section C).
Monitoring is driven over the history production holds — `daily`,
`daily_incremental_sync`, `monthly`, plus old `weekly` rows ending in `failed`
— under the run types the schedulers persist. The war room and the freshness
endpoint are driven through their routes over a database stub in the same
state. Against the parent commit 24 fail and 3 pass: the closed-won audit
(unchanged by this slice), and two counterfactuals that restore the old
registration, or fold `weekly` into `monthly`, and show the alarm or the
masking return.

The Countries cases run on real PostgreSQL: the real tables seeded through the
real writers (the restatement through `replace_geo_daily_spend_chunk`, the
writer the sync uses), the real `api_geo` handler, and its response rendered
by the real `loadGeo` in node. They cover:

- a covered window with an overlapping re-sync — exact sums, no double count;
- three kinds of uncovered window (sync behind, a gap, a failed chunk) —
  "Unavailable", no `$0`, no partial sum;
- a covered window where a known country has leads and no spend, and no
  spend is without a country — `$0`;
- a missing FX rate; a database holding only the legacy monthly snapshot;
- `UK` / `united kingdom` / `UAE` leads against "United Kingdom" / "United
  Arab Emirates" spend — one row each, leads summed; an unmatched lead country
  — "Unavailable — country not matched", and not counted as a geography;
- yesterday fetched during yesterday (through the real
  `run_google_ads_geo_sync`, ledger re-stamped to yesterday 06:05 UTC) —
  "Unavailable", not `$775`; after this morning's real sync — `$875`;
- `all_time` with an uncertified row twenty days before the certified range;
- all 162 production spend codes each as its own row, the 40 production lead
  spellings joined to them; Kazakhstan / Georgia / no-code spend with
  `Congo`, `California`, `Dubai`, `IN`, `CA` leads — totals conserved, no
  wrong join; `XK`; a merged country's top campaign; unreadable leads on the
  table and the map;
- a failed country lookup in this morning's real sync: with an older row
  naming the criterion the UAE keeps its `$63`; with none, the UAE reads
  "Unavailable — some spend in this window has no country", not `$0`;
- production's criterion `2192`, never named: a window holding some of it
  withholds a spend-less lead country and leaves the others published; a
  window holding none publishes that country's `$0`;
- one criterion stored under two codes — given neither; the identity read
  failing — unavailable;
- 39p never renders `$0`; unreadable leads are not "No leads"; a failed
  re-read is not "wait for the morning sync"; the country cards count
  countries; Kosovo; no account time zone on record;
- mutations removing the server gate and the page gate.

The identity cases hold the complete table (249 codes, round trip), every
production lead spelling and spend code, the names that must not match, and
that the market registry and its callers are exactly as they were.

Every Countries case failed against one or more of the three earlier versions
of this PR (`4651e2e`, `0ed6326`, `c23df68`). Against `c23df68`: `$775` for
"Unavailable" (08), "Unavailable — country not matched" for Kazakhstan's `$63`
(11), `camp-A` shown for a merged country (12), `0` on the map for unreadable
leads (13). Against `14542fd`: "United Arab Emirates `$0`" for `$63` (14) and
for "Unavailable" (14b), `$0` for 39p (17), "No leads in the selected window."
for unreadable leads (18), "The morning sync re-reads it" after that sync
failed (19), "Countries with SQLs 3" for 1 (20), Kosovo "not matched" (21).

Existing tests edited or deleted are listed in the commit message. None was
skipped or weakened: cases parametrised over both jobs now run for the job
that exists; cases that used `weekly` as their example cadence run on
`monthly`; cases that read the deleted page, service or job, and nothing
else, were deleted with a note in their place.

One of the two documented baseline deselects,
`test_weekly_empty_search_terms_marks_success_not_success_empty`, drove the
deleted job and is deleted. The baseline is now one deselect, in `CLAUDE.md`
and in `.github/workflows/pr-ads-153d-checks.yml`.

### Known gaps

1. **The monthly job is now the only scheduled writer of four datasets** —
   `waste_terms`, the legacy `keywords` and `geo` snapshots and
   `gclid_coverage_snapshots` — and of the search-term flag history. It runs
   the same pulls the weekly job did, and **its peak memory is UNMEASURED**.
   If it is ever killed at the memory limit as the weekly job was, those four
   stop refreshing and nothing else writes them. The failed-monthly warning
   (yellow on the first failure) is how that shows; they then go stale at day
   36. The flag-history step added to it calls `build_flagged_search_terms`,
   which builds the whole 30-day search-term population in memory for each
   page of flagged rows — its cost does not depend on how many terms are
   flagged.
2. **A monthly run that is still `running` reads as "failed on its latest
   attempt".** `LATEST_RUN_MUST_SUCCEED` treats every latest status other than
   `success` / `partial` as not succeeded, which fails closed for an unknown
   status but is wrong wording for a run in flight. Follow-up.
3. **Two more readers sum a monthly table over a window.** The degraded-mode
   spend column in `revenue_attribution_service` (only when canonical campaign
   spend is unreadable) and the admin pollution report read legacy `geo`;
   `GET /api/keywords` reads legacy `keywords` and has no page caller. See
   section C. Next slice: move them, then retire both snapshots and drop their
   pulls from the monthly job.
4. **Country Revenue & Spend and Campaign Evidence count a part-day as
   covered.** Both end their windows today, and both judge coverage on chunk
   status alone (`analyze_geo_coverage`, `analyze_coverage` over the campaign
   spend ledger): today — and, before the morning sync, yesterday — counts as
   covered while holding a few hours of spend. The fetched-after-the-day-ended
   rule is applied on the Countries page only. Follow-up: move the rule into
   the shared coverage function.
5. **On Countries the spend window and the lead window differ by a day.**
   Spend ends yesterday; the lead summary still windows
   on `run_date >= now - N days`. They are different facts about different
   entities, shown side by side, and no ratio is computed between them.

   Four more on the same page:

   - **The geo sync blanks a stored country code when its lookup fails.**
     `_resolve_country_metadata` swallows the failure and
     `replace_geo_daily_spend_chunk` deletes the re-read range and inserts the
     new rows, so a code already stored for those days is replaced by NULL, the chunk is `verified`
     and the run `success`. The Countries page now reads each criterion's
     code from any stored row, which covers every criterion that has a row
     outside the re-read range. It does **not** cover a criterion whose only
     rows were all inside it: nothing on record names that location any more,
     its spend sits in "(unresolved location)", and its lead row reads
     "Unavailable" (not `$0`) until a later sync restores the code. Follow-up
     in the sync: keep the last known code per criterion, and do not report a
     failed lookup as a clean run. Country Revenue & Spend reads the same
     rows through its own reader and is not changed here.
   - **Two production locations have no country on any row** — criteria
     `2192` and `2364`. By Google's convention a country criterion id is
     2000 + the ISO numeric code; that convention is not in the stored data
     and is not applied. Whether to name them that way is the owner's call.
     *Closed by PR-ADS-170A (owner decision 2026-11-22): `2192` is Cuba and
     `2364` is Iran, as two explicit entries in `config/geo_owner_labels.yaml`.
     The convention is still not applied: only a listed id is labelled.*
   - **No settle margin.** A day counts once a verified fetch was made at or
     after account-midnight. A manual sync minutes after midnight settles
     yesterday on whatever Google had attributed by then, until the 06:00 UTC
     sync re-reads it. `fetched_at` is stamped when the chunk is recorded,
     after the fetch.
   - **The page depends on the account time zone being on record** (the
     newest `google_ads_account_daily_spend.account_time_zone`). Not checked
     against production in this PR. If it is absent the page reads
     "Unavailable" and says why.
6. **The docs/48 scope rule is convention only.** With `period_ratio_scope_ok`
   removed, nothing checks that a reader passes spend only beside a Google
   Ads-scoped population. A check in `audit_customer_revenue_reader_cutover`
   belongs in a later slice.
7. **Flagged holds no rows in production, and this does not change that.**
   `waste_terms` and `search_term_review` are empty. A term is "confirmed
   waste" only when it matches a junk pattern, spent at least $5, and a
   HubSpot contact whose keyword equals the term exactly carries a junk
   status. The monthly job applies the same rule over 30 days.
8. **The 60-day search-term re-read is gone.** Search terms are re-read over
   14 days daily and 30 days monthly. A Google Ads restatement older than 30
   days is no longer picked up by any scheduled job.
9. **Other monthly data.** The waste flags, the GCLID coverage snapshot, the
   Dashboard's keyword-theme snapshot and the local JSON snapshots — the daily
   pulse's spend-anomaly baseline among them — are up to a month old.
10. **`retired_runs` is rendered nowhere.** It is in the monitoring API only.
11. **Older documents were not rewritten.** `docs/DEPLOYMENT.md` and
    `docs/48` §4 are corrected. `docs/API_CONTRACT.md`, `docs/03`, `docs/04`,
    `docs/07`, `docs/24`, `docs/31` and earlier sections of this file still
    describe `POST /run/weekly`, the weekly job, the Unit Economics page and
    the old `/api/geo` response.

---

## PR-ADS-166A — Leads page: contact names, customer names, and what "Customers" means

**Owner report (2026-11-19).** "The Leads page is amazing; maybe add customer
name and contact name to it; something is wrong, it says 208 customers for
last quarter."

**Production facts.** For Q3 2026 about 219 contacts entered the HubSpot
Customer lifecycle stage. 206 of them have an OFFLINE original source and 182
were created before the quarter: the team updated old records in bulk. In the
same quarter 8 deals were closed-won. The page's number was a correct count of
stage changes. The word above it, "Customers", was wrong.

Doctrine: `docs/49_LEADS_CUSTOMER_STAGE_AND_NAMES.md`.

### A. The number

- The stage count is now called **"Moved to Customer stage in HubSpot"** on
  the funnel strip, the tab ("Moved to Customer stage") and the table. Its
  help line: "Counts contacts whose HubSpot lifecycle stage changed to
  Customer in this period, including old contacts updated by the team. It is
  not new customers."
- The same event is renamed at its source, `analysis/crm_lifecycle.EVENT_LABELS`
  ("Lifecycle Customers" → **"Moved to Customer stage"**), so the Overview
  page's Lifecycle Activity and Lifecycle Cohort panels and the API say it
  too. The activity card reads "Contacts moved to the Customer stage during
  this period", and a conversion into the stage names its target "Customer
  stage".
- Beside it the page shows **"Customers won"**: distinct HubSpot companies
  with a closed-won deal closed in the period, with the closed-won deal count
  as a second line. Both come from the closed-won reader contract
  (`canonical_revenue_service.load_won_deals`, inside one snapshot) through
  the new `build_customers_won` in
  `services/canonical_crm_funnel_service.py` and
  `GET /api/crm-funnel/customers-won`. It republishes
  `{value, status, reason}` and decides nothing.
- The two figures are never added, and neither is worked out from the other.
  Each says what it is: the stage count carries
  `metric_family: lifecycle_stage_events`, `window_basis:
  date_entered_customer` (every event block of `/api/crm-funnel` now carries
  both keys); customers won carries `metric_family: closed_won_customers`,
  `window_basis: deal_close_date`, `dedup_key: hubspot_company_id`.
- When customers cannot be published the page shows "Withheld" or
  "Unavailable" and the reason, never a dash, a 0 or the stage count. An
  admin also gets "View blockers".
- **Refused rather than approximated.** Customers won is "Unavailable" when a
  Scope or Source filter is on (those filters classify contacts, not deals),
  and for any window that is not a business window. The page only sends
  business windows today.
- **The list of won customers.** When the customer count is published, the
  block has "Show the N customers won": company name, closed-won deals,
  latest close date. When the count is withheld or unavailable the payload
  has no list at all. If the list would not have exactly the published number
  of entries it is left out and the page says "The list of customers could
  not be built for this period."
- The Leads block is registered in `services/cross_page_parity_service.py`
  as a consumer of the `customers` and `closed_won_deals` identities, so the
  parity audit compares it with the Deals page, the Overview and the mart.
- The reader is in `INVENTORY` of
  `scripts/audit_customer_revenue_reader_cutover.py`, and the new route and
  that inventory row are registered in `analysis/sql_doctrine_registry.py`.
  The reader sits in the funnel service file, not a file of its own, because
  that audit's base-revision test reads every inventoried file from the
  PR-ADS-161E tree. It reads nothing from the funnel.

### B. Names

**What was stored before this PR.**

| Item | Stored? | Where |
| --- | --- | --- |
| Contact first / last name | No | nowhere |
| Company typed on the contact | Yes | `hubspot_contact_funnel.company`, written by the contact funnel sync |
| Name of the HubSpot company on a won deal | No | `hubspot_deal_company_association` holds company ids only |

**Contact names.** Stored in `hubspot_contact_funnel.first_name`,
`last_name`, `names_read_at` and `names_last_asked_at` (also on the archive
table `hubspot_contact_funnel_removed`). All reads are read-only. Email is
never requested; a name field that holds an email is stored as HubSpot sent
it and is not displayed.

- The daily contact sync now also asks HubSpot for `firstname` and
  `lastname`. That covers new and changed contacts.
- A new step in the daily incremental sync, `hubspot/contact_names`, reads
  the names of contacts already stored, by id, newest contact first, 100 per
  HubSpot call, at most 3000 contacts per run
  (`name_backfill.max_contacts_per_run` in `config/thresholds.yaml`). About
  21,700 stored contacts take about eight daily runs.
- `names_read_at` is stamped only for a contact HubSpot returned. A contact
  HubSpot was asked for and did not return gets `names_last_asked_at` instead
  and is asked again at most once every 7 days. A contact in a batch that
  failed gets no stamp and is asked again next run.
- A read is retried on 429, 5xx, a timeout or a transport error. Any other
  4xx (403 for a missing scope) is not retried, stops the step, and puts the
  HTTP status in the step's error text. The HubSpot SDK has a separate
  exception class per object type; contacts and companies are both caught.
- A later answer with no name does not blank a stored name. A different name
  replaces it.

**What the page says.**

| Case | Text |
| --- | --- |
| Name stored | the name |
| Names read, HubSpot has none | "Name not recorded" |
| The name field holds an email or no letters | "Name not shown" |
| HubSpot was asked and did not return the contact | "Not returned by HubSpot" |
| Never asked | "Name not loaded yet" |
| Any stored contact never asked for | under the Contact header: "Names are being loaded from HubSpot: N of M contacts read so far." |

The company, the email and the contact id are never shown in the name's
place. A missing company reads "Company not recorded". The search box matches
company or a displayed name; it does not match a name-field value that is not
shown.

**Company names.** A new table, `hubspot_company_name`, holds the HubSpot
`name` of each company that appears on a deal-to-company association. A new
step, `hubspot/company_names`, reads them by id: never-read companies first,
then those last read more than 7 days ago. A failed read writes nothing, so
the last good name stays. In the won-customers list an unnamed entry reads
"Company name not loaded yet", "Company not returned by HubSpot", "Company
has no name in HubSpot" or "Company name unavailable".

The **Company** column on contact rows did not change: it is the text typed
on the HubSpot contact.

**Step status.** Each name step is `success` when every batch it asked for
was answered, `partial` when some were not, and `failed` when the queue could
not be read or no batch was answered. `partial` is an error on the daily run
and is not turned into either neighbour.

**HubSpot calls.** While contacts are loading: up to 30 extra calls a day for
about eight days. After that the contact step makes no call on a normal day;
contacts HubSpot does not return are asked again once a week, one call per
100. The company step makes one call per 100 companies about once a week. The contact sync
makes the same number of calls as before.

**Who can see names.** `GET /api/crm-funnel/contacts` and
`GET /api/crm-funnel/customers-won` require a signed-in user of any role
(`require_auth`), as the contact rows did for company, country, source and
status. The app has two roles, `admin` and `viewer`.

### Tests

`tests/test_pr_ads_166a_leads_page_names_and_customers.py` — 32 cases, 11 on
a real PostgreSQL cluster (in CI's PostgreSQL step and its did-run list). The
page's own JavaScript functions are run in `node`, the Overview's two
lifecycle panels included, from a real overview payload. The fake HubSpot for
the name loads answers some batches, fails one, leaves one id out and returns
an empty name for another.

Existing tests changed:

- `test_leads_page_consumes_only_the_canonical_crm_funnel_family`: the
  endpoint allow-list gained `/api/crm-funnel/customers-won`.
- The tests that list the daily sync's datasets gained the two name steps,
  and `tests/conftest.py`'s eager-import list gained the new db module.
- `test_pr_ads_155_f2…::test_1`: the fifth activity label changed from
  "Lifecycle Customers entered during this period" to "Contacts moved to the
  Customer stage during this period", because the label itself changed.
- `test_pr_ads_154c_cross_page_parity.py`: the production-shaped fixture and
  the expected consumer set for `customers` gained the Leads block.

### Known gaps

1. **Names appear over about a week.** Until the load finishes, older
   contacts read "Name not loaded yet". The header says how far it has got.
2. **A contact HubSpot does not return** reads "Not returned by HubSpot" and
   is asked again weekly until PR-ADS-163C's check removes it. Whether the
   live batch endpoint leaves such ids out of its answer (a 207, which the
   SDK returns) or answers 404 naming them was not checked against HubSpot;
   both shapes are handled.
3. **A name cleared in HubSpot stays in the platform** until a new one is
   typed, for contacts and companies. That is the price of not letting an
   empty answer blank a name.
4. **A company renamed in HubSpot shows its old name for up to 7 days.**
5. **"Company" on a contact row is the text typed on the contact**, not a
   HubSpot company record. Contact rows are not linked to company records.
6. **Customers won does not follow the Scope and Source filters.** It says so
   and shows no number or list.
7. **The two figures use each family's own window edges for the same named
   period.** Customers won leaves out a deal closing later today. They were
   not reconciled to the instant.
8. **Whether names should be admin-only is the owner's call.** They follow
   the existing gate for contact rows.
9. **A non-ASCII name-field value made only of punctuation** is not displayed
   but is still matched by the search box (PostgreSQL's letter test here is
   locale-independent and coarser than Python's).
   The search also matches the stored text, so a name stored with two
   spaces inside it is found by typing two spaces, while the page shows one.
10. **`hubspot_company_name` has no `ALTER` path.** Its columns changed during
    review. That is safe only because no earlier version of this PR was ever
    deployed: a database that somehow had the first version of the table
    would keep it.
11. **Older documents still say "Lifecycle Customer".** `docs/33` §5 carries a
    note; `docs/05` and `docs/37` were not rewritten.
12. **The name steps are not on the System page** and have no freshness
   status. Their outcome is in the daily run's dataset report and errors.
13. **Not checked against production.** No production database or HubSpot
    call was made from this PR. The Q3 figures are from the owner's read-only
    checks. Whether Q3 customers publish in production, how many companies
    are on won deals, and whether the HubSpot token may read company names
    are not known here.

## PR-ADS-166B — HubSpot SDK errors caught per package; the deal ledger read pages by keyset (November 2026)

### 1. What was wrong

**Errors from the wrong package.** The HubSpot SDK makes one `ApiException`
class for each API package (contacts, deals, companies …), and the classes
share no parent. Catching the contacts class does not catch a deals error.

- `pull_deals_for_ledger` searched the **deals** API but caught the
  **contacts** `ApiException`. A deals 429 was never retried, and a 5xx, a 403
  or a timeout escaped the connector. The deal sync recorded `failed`, with the
  SDK's raw text (HTTP response headers and body) as the error.
- `pull_contact_attribution_properties` caught `requests` errors around an SDK
  batch read. The SDK never raises those; its transport failures are urllib3
  errors, so a timeout there was not retried.
- Neither call had a timeout.

**The deal read could skip deals and still say "complete"** (found in review,
executed against the real connector). `pull_deals_for_ledger` paged HubSpot's
search by its `after` offset over `hs_lastmodifieddate >= since`, stopped when
a response had no next page, never read HubSpot's `total`, and never removed
repeated ids.

- A deal already read and then edited during the read moves to the end of the
  result set, and every later row moves up one slot. The next offset then
  skips one unread deal. With 250 deals: 250 records, 249 unique, a closed-won
  deal never read, `complete: True`, and the watermark moved past it, so the
  next incremental never read it either.
- HubSpot does not keep the same order among deals with the same timestamp
  between queries (a bulk edit gives many the same one). With 150 tied deals,
  33 were never read; `complete: True`.
- A 200 response with no `results` key was read as an empty page:
  `complete: True`, no deals.
- No audit catches this. The deal certification audits forbid importing the
  HubSpot connector and check the ledger against itself. docs/09 §161I said
  the deal read was "reconciled by its own audits"; that was false and is now
  corrected there.

Production today, from the coordinator's read-only check of HubSpot and of the
Render database: HubSpot holds 578 deals (190 closed-won) and the ledger holds
578 / 190, so nothing is missing now. A daily
incremental reads about 28 deals on one page, where the offset problem cannot
arise. A bootstrap or restart reads 6 pages, where it can.

### 2. What changed

| Where | Change |
| --- | --- |
| `connectors/hubspot_pull.py` `_keyset_scan` (new) | The keyset loop PR-ADS-161I wrote for contacts, moved out so contacts and deals share it. Every page is a new query from offset 0 (`>= watermark`), never an offset, so HubSpot's 10,000-result paging limit never applies. Repeated rows at the watermark are dropped by id. A full page that never leaves its timestamp is a tie group, walked by `EQ T AND hs_object_id GT last` sorted by id, then `GT T`. A first response with no `total`, or a read ending short of it, raises `HubSpotSearchIncompleteError` (a proven gap). A page with no `results` list raises `HubSpotSearchPageUnreadableError`, which is deliberately not a proven gap: the pages before it are a keyset-safe prefix. The read ends only after `_assert_scan_complete` against the first query's `total`. Extra filters (the stage `IN`) ride on every query. |
| `_bounded_search` (new) | One request with a bounded retry. Catches `HUBSPOT_SDK_READ_ERRORS`. A 429, a 5xx or a transport error is retried 3 times (4 attempts); anything else is raised at once. |
| `_assert_scan_complete` | Wording names the record kind. New `tolerant=False`: every counted record must be read. The deal read uses it (a deal deleted mid-read costs one `partial` run; a tolerance could let a missed won deal pass). Contacts keep 1% / 2. |
| `pull_deals_for_ledger` | Rebuilt on `_keyset_scan` with a 30 s timeout. Returns one record per deal id, its newest version, in ascending timestamp order. **Complete** only after the `total` check. **Partial** (`available` True, the deals read so far): a transient error that outlasted its retries, a page with no `results`, a stalled tie walk, `page_limit`. **Partial with `gap_proven`**: a shortfall against `total`, or no `total` to check. **Unavailable** (`available` False, no deals): any other API error, with the HTTP status in the error (`deal read refused: HubSpot API error HTTP 403 (Forbidden)`). `pages` counts only pages actually read. |
| `iter_contacts_modified_since` | Now a wrapper over `_keyset_scan` and `_bounded_search`. **Behaviour change:** a 429 is retried 3 times and then raises `HubSpotRetryableError` (it used to retry without limit); a 5xx or a timeout is retried the same way (they used to fail at once, or escape raw); a page with no `results` raises `HubSpotSearchPageUnreadableError` instead of reading as empty. That is not a `HubSpotSearchIncompleteError`, so the contact sync keeps its checkpoint instead of rewinding a bootstrap to its start. The error text keeps `status=<code>`, which the lifecycle recovery reads to stop on 401/403. No timeout was added: the existing fakes of this search take no extra arguments. |
| `pull_contact_attribution_properties` | Catches `HUBSPOT_SDK_READ_ERRORS` as well as `requests` errors, and has a 30 s timeout. |
| `services/hubspot_deal_sync_service.sync_deals` | An incomplete read that read **no deal** (a 429 that never cleared on the first request, a first response with no `results` or no `total`) is `failed`, not `partial`. A read with `gap_proven` is `partial` and moves **no** watermark. |
| `db/deal_ledger_repository.record_sync_state` docstring | The "checkpoint skips nothing" claim now says why it holds (keyset), that it did not hold under offset paging, and that it does not hold after a shortfall, when nothing advances. |

**Why a partial read may still move the watermark to its clean prefix.**
Every query asks for `hs_lastmodifieddate >= the newest timestamp read so
far`, and results come back oldest first. So when a read stops, every deal it
did not reach has a timestamp at or after the newest one it did. The sync
checkpoints at the end of its clean, committed prefix, which is at or before
that point, and the next run re-reads from the checkpoint minus 15 minutes.
No unread deal lies behind the checkpoint. Under offset paging an unread deal
could, and a test now shows the old code checkpointing past one.

**Except after a shortfall.** That argument assumes HubSpot honoured every
query. A read that ends short of HubSpot's own `total` proves a deal was
missed but not where — it can sit behind the prefix. Review round 2 executed
it: with the deals search honouring `hs_object_id GT` but not the id sort, a
250-deal tie group lost 61 deals, including a won deal; the round-1 code
checkpointed about 477 hours past the oldest of them, and the next
incremental recovered none. So a shortfall (`gap_proven`) moves no watermark
at all: the next run re-reads the whole window. An unread deal deleted
mid-read also lands here; it costs one more full window read.

### 3. What the sync records for each outcome

Traced through `sync_deals`, `record_sync_state`, `check_sync_coverage`,
`freshness_block` and `ledger_staleness`, and executed in tests:

| Read outcome | Run status | Watermark |
| --- | --- | --- |
| complete | `success` | the newest deal read |
| transient error, stall, or a page with no `results`, after one or more deals were read | `partial` | the end of the clean, committed prefix |
| fewer unique deals than `total` (`gap_proven`) | `partial` | unchanged |
| first response with no `total` (`gap_proven`, nothing read) | `failed` | unchanged |
| transient error on the first page; first page with no `results` | `failed` | unchanged |
| refused (401, 403, 400 …) | `failed`, error carries `HTTP 403` | unchanged |

**When figures are withheld.** The first version of this section said deals,
customers and revenue are withheld "once 36 hours pass" without a successful
incremental. That was wrong. Executed: a ledger whose last incremental was
`partial` or `failed` fails the coverage gate at once
(`last_sync_not_successful`), so `coverage_proven` is false and every
closed-won figure withholds **from that run on**, for that reason.
`ledger_staleness` then returns `stale: None`: the 36-hour
`canonical_ledger_stale` limit applies only while the last run was a success,
and catches a scheduler that stopped running.

### 4. Tests

- `tests/test_pr_ads_166b_sdk_exception_classes.py`, 27 cases. Runtime cases
  drive the **real** SDK client and replace only `urllib3.PoolManager.request`,
  so each exception is the one the SDK itself raises for that status and
  package (deals `ApiException` 429, `ServiceException` 503,
  `ForbiddenException` 403, urllib3 `ReadTimeoutError` / `MaxRetryError`).
  Sync cases run the real `sync_deals` and the real `record_sync_state` over a
  recording connection. The structural guard parses the connector and fails
  on an `except` around a `client.crm.<package>` use that does not catch that
  package's class, or that catches `requests` errors without urllib3's. It now
  also follows `crm = client.crm`, a local alias of an API object, a nested
  def judged where it is called, an `except` naming the class by module alias
  or full dotted path, and counts `HUBSPOT_API_EXCEPTIONS` only for the
  packages its definition lists (an associations call under it is flagged).
  Each of those shapes is shown flagged, and shown passing when correct.
- `tests/test_pr_ads_166b_deal_keyset_paging.py`, 16 cases. Its fake HubSpot
  honours every filter the read sends, both sort orders, the `after` offset
  (so the old code can run against it) and the 10,000-result limit, reports
  `total`, changes its order among tied rows on every query, and can edit a
  deal between queries. It reproduces the review probes: an edit after page 1
  (every one of 250 ids read once, the won deal written, success watermark),
  the same edit followed by a 429 (the checkpoint is behind no unread deal),
  150 tied deals in a shifting order, ties plus an edit during the tie walk,
  a `total` one higher than the deals that exist (`partial`, no watermark
  advance), a first page with no `results` (`failed`), no `total` (`failed`),
  a page with no `results` mid-read (`partial`, checkpoint kept and behind
  every unread deal), the round-2 S4 case over two runs (id sort ignored in
  the tie walk: run 1 `partial` with no advance, run 2 reads every deal run 1
  missed), 10,050 deals (no offset ever sent), and a stage filter on every
  query. Two cases execute the coverage gate for the withholding claim
  above. One case runs the contact sync over the PR-ADS-161I fake: a page
  with no `results` mid-bootstrap fails the run and keeps the checkpoint
  instead of rewinding to the epoch.
- Against the commit before the review fix (`5e8f2a2`): 14 of the 40 cases
  fail — 11 of the 13 keyset cases (the two that pass execute the coverage
  gate, which this PR does not change) and 3 of the 27 others (the zero-page
  `failed` rule, and two whose fakes now model keyset paging). Against
  `d979638`, the first version of the exception-class module failed 14 of
  its 20. Against round 1 (`ab8058d`): 4 of the 43 fail — the shortfall
  advance, the no-`total` `failed` rule, the S4 two-run case and the contact
  checkpoint case. The mid-read unreadable-page deal case passes on both:
  round 1 already kept that checkpoint.
- Existing tests: none changed. `tests/test_pr_ads_161i_contact_scan_paging.py`
  and the contact-sync suites pass unchanged on the shared loop.
- No PostgreSQL case was added: the watermark decision (`advance`, the
  checkpoint) is made in Python before the SQL runs, and the SQL's use of it
  is covered by `test_watermark_advances_only_on_success` and
  `test_sync_state_checkpoint_advances_the_watermark_on_a_capped_run`
  (PR-ADS-153E-A PostgreSQL suite).

### 5. Known gaps

1. **Legacy searches still page by offset** and never read `total`: the
   offset-skip shown above can happen in them.
   `pull_paid_search_contacts` (`connectors/hubspot_pull.py:328`),
   `pull_paid_search_contacts_in_range` (`:384`),
   `_iter_closed_won_deal_pages` (`:592`, used by
   `pull_closed_won_deals_in_range` and
   `pull_closed_won_deals_with_sources_in_range`) and
   `pull_all_contacts_in_range` (`:836`). They feed the legacy, non-canonical
   datasets, not the deal ledger.
2. **The AST guard is not a type checker.** It does not follow an API object
   passed between module functions other than as a direct argument or lambda
   to a module helper, an alias assigned in one function and used in another,
   `getattr(client.crm, name)`, or an alias of a module-level `HUBSPOT_*`
   tuple. Each shape the review found is covered.
3. `iter_contacts_modified_since` still passes no request timeout.
4. The contact history and comparison reads (`fetch_lifecycle_stage_history`,
   `fetch_lifecycle_stage_history_single`, `compare_sql_entry_evidence`)
   retry only a 429. A timeout escapes as a raw urllib3 error; every caller
   catches it and records a failed read — loud, not retried.
5. `pull_contacts_by_ids` has no retry and no timeout. Its caller treats any
   error as "unresolved, retry later".
6. `pull_contact_attribution_properties` still retries a 403 three times.
7. A deal deleted or merged in HubSpot during a read now makes that run
   `partial` with no watermark advance (`tolerant=False`). The next run
   re-reads the window and repairs it.
8. **The deal tie walk relies on HubSpot honouring `hs_object_id GT` and the
   `hs_object_id` sort on the deals search.** Both were verified live for
   contacts in PR-ADS-161I, not for deals. If the sort is ignored the walk
   skips tied deals; the `total` check then fails the read closed
   (`partial`, no advance) and every later run would do the same until a run
   reads the whole window — loud, but not self-healing.
9. **AST guard residual blind spots** besides those in gap 2: an alias kept
   on `self` and used in another method (`self.api = client.crm.deals…`), and
   a tuple-unpacking alias (`a, b = client.crm.deals, …`).
10. Not checked against production HubSpot.

---

## PR-ADS-167A — Countries: zeros that are measured, codes the sync keeps, one settle rule (November 2026)

Scope: the PR-ADS-165A known gaps for the Countries page and the surfaces that
share its geo data. No change to the SQLs column (legacy, separate slice).
Criteria `2192` and `2364` are not labelled (owner decision pending).
*(Decided 2026-11-22 and implemented in PR-ADS-170A: owner-labelled Cuba and
Iran, `config/geo_owner_labels.yaml`. The zero rule below is unchanged; those
two criteria now count as carriers of `CU` / `IR` and no longer as unresolved.)*

**Production facts (read-only, 2026-11-19).** `google_ads_geo_daily_spend`:
35,297 rows with a country code over 212 criteria carrying 212 codes, strictly
one-to-one; 25 code-less rows over two criteria, `2192` and `2364`, with no
coded row anywhere (about £11; newest code-less day 2026-11-06). Coverage
ledger: 122 chunks, all verified, every `fetched_at` set; the daily chunk
[D-6..D] is fetched about 06:02 UTC. One Google Ads account, time zone
Europe/London.

### A. The zero rule no longer over-withholds (165A gap 1)

Before: a resolved lead country with no spend row read "Unavailable — some
spend in this window has no country" whenever any spend in the window had no
country. With `2192` spending, that was every spend-less lead country.

Google keys one country under one criterion. Spend on a criterion no stored
row names is therefore not the spend of a country whose code another criterion
carries on record. A resolved country with no spend row is now `$0`
(published) when:

1. the window is covered (unchanged);
2. every unresolved criterion in the window is code-less everywhere — none is
   a criterion whose rows carry two codes;
3. the country's code is carried on record by exactly one criterion, and that
   criterion carries no other code (`repo.fetch_geo_code_carriers`).

Otherwise it is "Unavailable" with the same reason as before. Rule 3 is
stricter than "carried by at least one criterion": if the stored data shows a
code under two criteria, "one country, one criterion" is false for that
country and its zero is not measured. Production has no such code. A failed
carrier read counts as "carried by nothing".

A country never seen in Google Ads geo spend (Pakistan: leads, no Google
spend) could be the code-less location: "Unavailable" while code-less spend
is in the window, `$0` when none is.

The payload's `unresolved_spend` gains `zero_withheld_countries`, and
`blocks_zero` now means "withholds at least one country's zero". The notice
says which countries are affected. Production-shaped case (UK spends, `2192`
spent £3 in the window, Jordan's only rows are older, Pakistan has never
spent):

- before: notice "…A country with no spend of its own reads "Unavailable"
  rather than zero, because that spend may be its own. Countries that show a
  figure are not affected."; Jordan "Unavailable — some spend in this window
  has no country"; Pakistan the same.
- after: notice "$4 of this window's spend was reported by Google Ads against
  one location with no country on record. It is in the total and in the
  "(unresolved location)" row. It cannot be the spend of a country Google Ads
  reports under a location of its own, so those countries' figures, zeros
  included, stand. It could be the spend of a country never seen in Google
  Ads spend: Pakistan reads "Unavailable" rather than zero."; Jordan `$0`;
  Pakistan "Unavailable — some spend in this window has no country".

When code-less spend is in the window and no country is affected the notice
still shows, and ends "No country's figure is affected." When a criterion in
the window carries two codes it says so and lists every spend-less country.

### B. The geo sync keeps known codes and reports a failed lookup (165A gap 2)

Before: `_resolve_country_metadata` swallowed a failed `geo_target_constant`
lookup; every row of the re-read range was written with no code (the range is
deleted first, so stored codes for those days were lost), the chunk was
`verified` and the run `success`.

Now:

- One statement of the identity rule, `db/geo_identity_sql.py` (exactly one
  code on record over the whole table resolves; none or two do not). Every
  reader uses it: `fetch_geo_criterion_identities`,
  `fetch_geo_daily_spend_by_country` and `fetch_geo_code_carriers`.
- The sync names each criterion from the codes already stored for it, and asks
  Google only about the criteria storage cannot name.
- The writer never writes NULL over a stored code: `replace_geo_daily_spend_chunk`
  reads the identity of code-less incoming criteria inside its own
  transaction, before deleting the range (it reports `codes_preserved`);
  `upsert_geo_daily_spend` keeps a stored code when the payload has none.
- A lookup that raises still writes the rows (the spend is real) and the chunk
  stays `verified` — coverage is a statement about spend — with the failure in
  the chunk's `error_message`. The run is `partial`, reason
  `country_lookup_failed`. The daily incremental step passes `partial` to the
  sync batch and `sync_state` unchanged, adds the error to the run's error
  list, and does not advance the watermark or the geo checkpoint.
- A lookup that answers with no country for a criterion (production's
  `2192`) is Google's answer, not a failure: the run stays `success`.

### C. One settle rule, and the consumers that publish country spend (165A gaps 3/4)

The "settled" rule — a verified chunk covers only the days that had ended, in
the account time zone, when it was fetched — moved from
`country_evidence_service` into `google_ads_spend_service`
(`settled_through`, `settled_chunks`, `analyze_settled_coverage`). The
Countries page calls the shared one; the old names are re-exported. The
reasons are shared codes: `spend_not_reread_since_day_ended`,
`spend_reread_failed`, `account_time_zone_unknown`, and new
`window_includes_day_in_progress`.

`analyze_published_geo_coverage` applies it to the geo ledger (reading the
account time zone where the sync does). `analyze_geo_coverage` keeps its old
meaning, "was every day fetched?", because the sync's own completeness check
and the backfill script ask that; with the settle rule every daily sync would
be `partial`.

**Owner decision (2026-11-20): the country consumers' open windows end
yesterday.** Today is never settled, so a window that includes today can never
hold complete country spend. For Country Revenue & Spend and Dashboard
Countries (and the mart country view and reconciliation panel that feed them)
a window that includes today ENDS AT YESTERDAY in the account time zone. Its
deals (and so revenue and customers) are read over the same account-local days
as its settled spend: from account-local midnight of the first day to
account-local midnight of today, as UTC instants. Leads keep the mart's
date-grain read over the same start and end dates. Closed windows (Last
Quarter) are unchanged: their deals keep the business windows' UTC-midnight
bounds, so under BST a closed window's deals and its London spend days are an
hour apart at each end (see Known gaps). Every other revenue page
keeps its window, so these pages may differ from them by today's deals, and
say so.

One rule, `services/canonical_contract.py`:

- `end_at_settled_day(resolved)` — an open window keeps its start and ends
  yesterday; a closed one is unchanged. It adds `through_reason`
  (`today_not_settled`), `account_today` and `window_range` = `{start,
  through (inclusive), through_reason, empty, disclosure}`. The disclosure is
  "Through 19 Nov 2026 — today is not included until tomorrow morning's sync.
  Other revenue pages, such as Deals, already count deals closed today." On a
  quarter's or year's first day the open window has no settled day (`empty`,
  "No completed day in this window yet — …").
- `resolve_settled_window(window, now)` = that over `resolve_canonical_window`.
- `settled_revenue_bounds(resolved)` — `(start, end)`: account-local midnight
  of `start_date` (None for All Time) and of `account_today` (exclusive), as
  UTC instants; `end == start` on an empty window; None for a closed window.
  Every settled consumer, the parity reference and the reader cutover audit
  read deals through it, so they compare the same instants. Review round 1
  found the first cut resolving the window KEY on the UTC day and only
  narrowing its end: at 23:30 UTC on 30 Sep (00:30 BST, 1 Oct) the empty Q4
  window published Q3's 2 deals / $8,000 beside £0 spend, and a deal at 00:20
  BST on 16 Jul was counted in "Through 15 Jul".
- An empty window (a quarter's or year's first day) measures nothing: every
  metric on these consumers is unavailable with `no_settled_day_in_window`
  (mart summary, spend truth and readiness; Dashboard Countries KPIs, rows,
  residual and metric contracts; the panel); the UI shows the disclosure
  instead of the cards, and the range chip shows no reversed range.

Mechanism, no forked reader:

- `canonical_revenue_service.load_won_deals(window, ..., bounds=None)` — a
  new keyword: `(start, end)` from `settled_revenue_bounds` that replaces the
  named window's bounds; the population is evaluated over `[start, end)` by
  the same canonical membership as any explicit bounds (`window_view`), the
  window block keeps the key and label. `None` (every other caller) changes
  nothing. No new reader file, so the cutover audit's `INVENTORY` is
  unchanged.
- `revenue_attribution_service.build_revenue_attribution(window, now, *,
  settled_days=False)` — True resolves the settled window for spend, leads and
  geo coverage and passes `bounds` to the reader contract. The default
  (`/api/revenue-attribution`, Campaign Revenue & Spend) is unchanged.
- `revenue_decision_mart.SETTLED_DAY_VIEWS = {"country"}`: the country view is
  built over the settled-day core; its `window` block carries
  `through_reason` / `account_today` / `window_range`. The campaign, source and
  deal views are unchanged. Its `sql_reconciliation` is asked at the last
  instant of yesterday and published only when that resolves to exactly the
  view's days; otherwise (Last 6 Months, whose start would move) it is
  `unavailable` with `settled_window_not_reconcilable`.
- `dashboard_countries_service._assemble` reads the mart country view and the
  geo spend over its window, and the deals proving each country with
  `load_won_deals(..., bounds=settled_revenue_bounds(window_block))`; the
  mart's `window.start_utc` / `end_utc_exclusive` publish those instants.
  When geo is not ready (any gate gap, e.g. before the morning sync) each
  country row and the residual withhold the same figures as the KPIs (spend,
  revenue, deals and proof list, customers, SQLs, ratio; native spend only
  where the KPI keeps it), with `withheld_reason` and the status "Country
  figures withheld" — rows that add back up to a withheld total would publish
  it (review round 1: before the fix the US row showed spend 8000 / revenue
  29000 / deals 2 / SQLs 20 beside withheld KPIs). The previous period is
  judged by the same rule. `analysis/sql_doctrine_registry.py` classifies the
  new symbol (`_withhold_when_geo_not_ready`) under the existing Dashboard
  Countries legacy-SQL record.
- `build_geo_reconciliation` applies `end_at_settled_day` to its account-day
  window and publishes `through_reason` / `window_range`.
- The mart's page audit (`build_revenue_performance_audit`) compares Country
  Revenue & Spend's spend with the mart's spend over the SAME settled days and
  reports that window on the row.

Final review (round 3):

- `load_won_deals(bounds=)` refuses bounds that do not sit inside the named
  window (`_settled_bounds_problem`): start after end, end after now, start
  before account-local midnight of the window's first day, end after its UTC
  end, or no start for a window that has one → `unavailable` with
  `unknown_business_window`. Before: `bounds=(2020-01-01, 2030-01-01)` on
  Current Quarter returned 2024 and 2026 deals labelled "Current Quarter".
- The parity audit compares a settled consumer's published instants
  (`window.start_utc` / `end_utc_exclusive`) with `settled_revenue_bounds` for
  the window it resolves itself, not only the dates: right days read on the
  UTC calendar is `consumer_windows_differ`.
- Country Revenue & Spend's own rows (the mart country view) withhold, when
  the geo gate is not ready, every row's spend, ratio and country-attributed
  outcome (leads, SQLs, deals, customers, revenue, `metrics`) with
  `withheld_reason` (`_withhold_country_rows_when_geo_not_ready`); spend
  survives as native only when FX is the only gap. Before: with the gate at
  `spend_not_reread_since_day_ended`, the GB row carried spend 6000, revenue
  9000, 1 deal and SQLs.

| Consumer | Window end | Verdict |
| --- | --- | --- |
| Countries (`/api/geo`) | yesterday | unchanged; uses the shared rule |
| Country Revenue & Spend (mart country view), Dashboard Countries, geo reconciliation panel | yesterday for open windows; Last Quarter unchanged | after the morning sync (~06:02 UTC) every figure publishes. Between account midnight and that sync yesterday was last read in progress: country spend and ratios withheld with `spend_not_reread_since_day_ended` ("…The morning sync re-reads it."); revenue for the same days still publishes |
| `/api/revenue-attribution` (default contract), Campaign Revenue & Spend, every other revenue page | today | unchanged; its country section withholds open windows with `window_includes_day_in_progress` |
| Geo sync final check, backfill script | — | unchanged ("was every day fetched") |

The Countries page (`/api/geo`) now reads its spend rows, criterion
identities and code carriers in ONE REPEATABLE READ, READ ONLY transaction
(`repo.geo_read_snapshot()`), so a sync naming a criterion between them cannot
produce a false $0.

Rendered (`static/app.js` `renderWindowRange` → `windowThroughNote`, verbatim
from `window.window_range.disclosure`), open window after the morning sync:
"Current Quarter: 2026-10-01 → 2026-11-19 · Through 19 Nov 2026 — today is not
included until tomorrow morning's sync. Other revenue pages, such as Deals,
already count deals closed today." on Country Revenue & Spend's range chip and
on the Dashboard's range chip for Countries; the Verified Geo Spend card shows
the native/USD figure. Before the morning sync the same window line shows, and
the card reads "Country spend withheld — yesterday's spend has not been
re-read since the day ended" (`ctrySpendWithheldText`, from the page's metric
contract reason code). "Country spend withheld for this window" is gone. The
Country Revenue & Spend subtitle adds "A window that includes today ends
yesterday, for revenue and spend alike: today's country spend is complete only
after tomorrow morning's sync." On a window with no completed day the range
chip reads "Current Quarter: No completed day in this window yet — today (1 Oct
2026) is not included until tomorrow morning's sync. Other revenue pages, such
as Deals, already count deals closed today." and both pages show a "No
completed day yet" card with that sentence instead of cards and tables.

**Cross-page parity** (`cross_page_parity_service`): a consumer whose window
block declares `through_reason: today_not_settled` is not compared with the
others' window; it is compared with the settled window the audit resolves
itself (`settled_window` in the result) and a declaration it cannot reproduce
is `consumer_windows_differ`. An UNdeclared shorter window is still compared
with the others and still differs. A settled reference consumer,
`revenue_decision_mart/country` (the mart country view; it certifies no
identity), supplies the coverage proof for the country identities when their
consumer declares the settled window, and the total for the
`campaign_attributable_sqls = country + residual` conservation rule
(`total_source` in the result) — used only while its window is the parts'
window, otherwise not evaluable. Equality is not loosened. The reader cutover
audit judges `revenue_decision_mart_country` and `dashboard_countries` against
the canonical publication for the settled days (`settled_publication`).

**Dashboard Countries now withholds what it declared not ready.** With geo
not ready, the page's own metric contract said `not_ready` for
country-attributed spend, revenue, the closed-won deal count and the SQL
split, and published them anyway (`kpis.verified_spend_usd`,
`won_revenue_usd`, `closed_won_deals`, `sqls`, `residual.sqls`). That was
latent for any non-ready geo window (a mismatch); the contract test
`test_f3_15` and the cross-page parity audit caught it while open windows were
not ready (before the owner decision above). It still applies before the
morning sync and to any other geo gap. Those figures are now absent (`null`) when geo is not ready, the
withheld spend and SQLs name the gate's reason in `unavailable` and in their
metric contract, and native spend is withheld too unless the only gap is FX
(the gate's own rule: "native spend is shown instead"). The KPI card reads
"Unavailable" with "Country spend withheld for this window".

The shared gate `resolve_country_spend_status` gained one required,
keyword-only argument, `geo_coverage_settle_gap`; it names a fully fetched but
unsettled window instead of "never synced — run the geo backfill". New
operator sentences in `GEO_GAP_MESSAGES`.

Country Revenue & Spend and Dashboard Countries read
`fetch_geo_daily_spend_by_country`, which named a criterion by
`MAX(country_code)` of the window's rows. It now carries the criterion's
identity on record, read in the same statement as the rows
(`identity_codes` lists every code so "none" and "two" differ). A criterion
whose window rows were written while the lookup failed no longer drops out of
its country.

### Tests

New, all on real PostgreSQL, in CI's PostgreSQL step and did-run list:

- `tests/test_pr_ads_167a_countries_zero_rule_pg_integration.py` (7): the
  production shape through the real handler and `loadGeo` — Jordan `$0`,
  Pakistan "Unavailable" with `2192` spend in the window and `$0` without; a
  two-code criterion in the window; a code under two criteria; the carrier
  read failing; an uncovered window; one identity statement.
- `tests/test_pr_ads_167a_geo_sync_country_codes_pg_integration.py` (4): the
  real sync with a fake Google whose lookup raises — stored codes kept
  (including a criterion whose only rows were in the re-read range), the new
  criterion NULL, Google asked only about it, run `partial`, ledger chunk
  verified with the error, checkpoint not advanced; the next run resolves it
  and is `success`; a lookup that names no country is not a failure; the
  daily step records a `partial` batch and `sync_state`; the writer guard.
- `tests/test_pr_ads_167a_settled_coverage_consumers_pg_integration.py` (12):
  the shared rule's reasons; one rule; the reconciliation panel over Last 6
  Months ends yesterday, excludes today's spend from both totals and is ready
  after the morning sync, not ready before it (`spend_not_reread_since_day_ended`),
  Last Quarter unchanged; a closed window not re-read after its last day; the
  settled revenue attribution contract and the mart country view (ready,
  declared) beside the unchanged default contract and campaign view; identity
  in the country spend reader and Dashboard Countries' map; the sync's check
  is not the publication rule; and end to end, a deal closed TODAY is on the
  Deals page and the campaign view and not on the mart country view or
  Dashboard Countries (whose spend, deals, revenue and per-country ratio all
  cover the same days), with the disclosure, Last Quarter unchanged and
  spend withheld before the morning sync. Review round 1 added: the first hour
  of Q4 under BST (23:30 UTC 30 Sep) and noon on 1 Oct publish nothing on the
  mart, Dashboard Countries or the panel (`no_settled_day_in_window`), and at
  noon on 16 Jul the 00:30 BST 1 Jul deal is in and the 00:20 BST 16 Jul deal
  is out, with the window's UTC instants on the account calendar.

Final review added: PG `test_12` (one geo snapshot, behavioural: a concurrent
commit naming criterion 2192 is invisible to identities and carriers inside
`geo_read_snapshot`, visible after; an error inside makes the readers
unavailable; a write inside is refused), and pure 06d (bounds never widen),
12 (UTC-calendar instants are a window mismatch) and 13 (Country Revenue &
Spend rows withhold) — all three fail on a05ade7. The 158 privacy test also
checks the national format ("07700 900123").

Not PostgreSQL (full suite): `tests/test_pr_ads_167a_settled_day_windows.py`
(16) — the rule (open, closed, all time, a quarter's first day, the account
day at 23:30 UTC under BST), `load_won_deals(bounds=)` over account-local
instants (the reviewer's probe cases: 00:20 BST today out, 00:30 BST on the
first day in, the first hour of a quarter empty), the Countries page's one
geo snapshot (structural), and the parity audit: the country consumers declare a window it
reproduces, a wrong declaration and an undeclared shorter window are both
`consumer_windows_differ`, conservation uses the settled reference only over
the same days.

Against origin/main 17 of 18 fail; the one that passes is the uncovered-window
guard (rule 1, unchanged behaviour). Against the first 167A cut (open windows
withheld, before the owner decision) every new settled-window case fails (13
of 18 in the two settled modules; the 5 that pass are unchanged rules): the
panel's `date_to` was today, `load_won_deals` took no `through`, the rule did
not exist. Review round 1's new cases (06, 06b, 06c, PG 09, 10, 11 and 138's
extended `test_167a_a`) all fail on 8ef9cac: Q3 deals published under an
empty Q4 window (3 deals), "verified" zeros on 1 Oct, the window starting at
UTC midnight, the 00:20 BST deal counted, and the US row's spend 6000
published beside withheld KPIs. Behavioural failures among them: Jordan
"Unavailable" for `$0`; the sync `success` after a failed lookup, and the
daily batch `success`; the writer storing NULL over "AE"; the panel
`geo_ready: true` for the current quarter; revenue attribution
`geo_coverage_status: complete` for the current quarter; the UAE named
`(None, None)` by the country spend reader. A mutation dropping rules 2 and 3
fails the two-code cases.

Existing tests changed (expectations of the old behaviour, or fakes that did
not model production):

- `test_pr_ads_165a_countries_pg_integration.py`: `unresolved_spend` dicts
  gained `zero_withheld_countries`; the notice wording; the structural check
  now finds the settle rule in the spend service; `_failed_lookup_sync` no
  longer asserts `success` — test 14 asserts `success` (storage named every
  criterion, Google was not asked) and that no row is blanked; test 14b's
  location is now first seen on the failing morning (a sync can no longer
  blank a known one) and asserts `partial`.
- `test_pr_ads_153f_country_geo_truth.py`, `test_pr_ads_154b_truth_readiness.py`:
  the gate's proven-evidence sets and caller checks include the new required
  argument; the reconciliation fixture's chunk carries `fetched_at` and runs a
  closed window, and a new case asserts YTD is `window_includes_day_in_progress`.
- Geo ledger fakes carry `fetched_at` (production stamps every chunk), and
  the account time zone where it was not patched: 124, 125 (126/129/130/131
  through it), 129, 130, 138, 153f, `test_revenue_attribution_service.py`.
- After the owner decision the first cut's moves to Last Quarter are
  REVERTED: 126, 131, 154c parity and 154c_f3 are origin/main's files again
  (154c_f3 keeps one change, below); 129/130/153f run YTD again; 125's country
  cases run YTD again; 138 is back at 22 June / YTD.
- Changed against origin/main because the behaviour changed:
  125 `test_all_views_use_the_same_window_resolver` (campaign/source/deal end
  22 June; the country view ends 21 June and declares it, disclosure text
  asserted), `test_country_cannot_invent_spend_beyond_mart` (compares with the
  settled contract), `test_country_reconciliation_status_is_shared_across_views`
  (all four verified on Last Quarter; on YTD the country view is verified
  through yesterday and the others say `window_includes_day_in_progress`),
  `test_audit_compares_every_page_against_the_mart` (the country row reports
  its window); 154c_f3 `test_f3f1_13` mutates the settled reference, the
  evidence the country proof now reads; `test_pr_ads_154c_parity_pg` excludes
  declared rows from the one-range check and asserts they equal
  `settled_window`; 153f `test_f1_6` (YTD ends yesterday; a ledger fetched two
  days ago is `spend_not_reread_since_day_ended`); 138 `test_167a_a/b/c` (YTD
  through yesterday publishes; before the morning sync withheld with the
  reason, and no row / residual / regional figure the KPIs withhold; Last
  Quarter unchanged; the card's reason text); 124 and
  `test_revenue_attribution_service.py` test the settled contract
  (`settled_days=True`) for availability and still assert the default
  contract withholds an open window.
- `test_pr_ads_158_sql_doctrine_audit_pg_integration.py::test_no_email_or_phone_in_runtime_output`
  (fixture identity, not a weakening): it asserted the fragment "7946" of the
  seeded phone never appears, and the audit lists source locations — app.js
  line 17946 is a classified occurrence. The fixture now seeds a distinctive
  phone and email and the test looks for each whole value and the phone's
  digits-only form; a probe printing either from the audit fails it.
- `test_pr_ads_124_roas_country_geo_recovery.py`: the dry-run sync case's fake
  Google answers the country lookup (unpatched, the real connector cannot load
  and that is now — correctly — a `partial` run).

### Known gaps

1. **Closed windows keep the UTC-midnight deal bounds** (owner: Last Quarter
   unchanged), while their spend is London days: under BST a closed window's
   deals and spend are an hour apart at each end (a deal at 00:30 BST on the
   quarter's first day is in the previous quarter's revenue). Open windows have
   no offset. Last 6 Months' SQL reconciliation is withheld on the country
   view; `/api/revenue-attribution` (default) keeps today and withholds its
   country section for open windows.
2. **Campaign spend coverage still judges chunk status.** Campaign ROAS, the
   Dashboard's spend and Campaign Revenue & Spend use `analyze_coverage` over
   the campaign spend ledger; they count today's part-day as covered. Applying
   the rule there withholds the Dashboard's spend for every open window — not
   done here.
3. **Campaign Evidence (`/api/campaigns`) has no coverage gate at all.** It
   sums `fetch_canonical_campaign_spend` over a window ending today, whatever
   the ledger says. 165A described it as judging chunk status; it does not
   judge coverage. Not changed.
4. **Fixed (review round 1):** the Countries page's spend, identity and
   carrier reads share one snapshot. Its coverage-ledger read before them is
   still a separate statement.
5. **Rows already blanked in production stay blank** until a sync re-reads
   them (the daily sync re-reads 7 days). Readers name them through the
   criterion's identity; nothing rewrites history.
6. **No settle margin**, as in 165A: a day settles on any verified fetch made
   after account-midnight.
7. **Not checked against production**: the new statements' cost on 35k rows,
   and whether production's lookup ever raises (the 165A defect assumed it).
8. **Fixed (review round 1):** Dashboard Countries' rows and residual
   withhold what its KPIs withhold.
9. **Parity for open windows** compares the country consumers with the
   settled reference, one extra mart build per audited window; its cost was
   not measured against production.

## PR-ADS-168A — Channels: closed-won outcomes per channel (November 2026)

Scope: the Channels page gains the deals that CLOSED in the same days as its
acquisition-cohort funnel, per HubSpot original source, through the reader
contract. Revenue by Source is unchanged (its removal is PR-ADS-168B). Full
design: `docs/50_CHANNEL_OUTCOMES.md`.

**Production facts (read-only, 2026-11-20).** Won deals by placement class:
all time — 155 resolved, 20 contacts disagree, 14 no associated contact, 1
contact not in the funnel; last 180 days — 15 resolved, nothing unresolved;
last 60 days — 8 resolved.

### What changed

1. **Deal → channel.** A won deal takes channel X only when every associated
   contact's `hubspot_contact_funnel.hs_analytics_source` (through
   `channel_funnel_service.source_key`) is X; otherwise it is on the
   outcome-only row `__deal_source_unresolved__` ("Source not resolved") with
   one of six classes (`association_lookup_failed`, `no_associated_contact`,
   `associated_contacts_not_stored`, `contact_not_in_funnel`,
   `contact_has_no_source`, `contacts_disagree`).
   `fetch_closed_won_universe` selects `f.hs_analytics_source` in its
   REPEATABLE READ snapshot. The ledger's `source_primary_raw` is not read.
2. **Reader contract.** `load_won_deals(window_type="evidence")` reads an
   evidence key through `ccr.window_view(window_type=evidence)`; `bounds=`
   (PR-ADS-167A) applies to it. `source_attribution_service.build_channel_outcomes`
   (`@single_snapshot`) builds one `reader_row` per channel; totals are the
   window's `canonical_metrics`; the rows must reconcile to the published
   deal count (same ids) or every row withholds with
   `channel_partition_does_not_reconcile`; `channels` / `partition` are null
   when the deal count is not published.
3. **Settled days.** `canonical_contract.resolve_settled_evidence_window`
   ends the evidence window at yesterday through `end_at_settled_day`; deals
   are read over `settled_revenue_bounds`; the window block carries the
   disclosure. The funnel keeps its own cohort through today.
4. **Spend.** `revenue_attribution_service.canonical_campaign_spend_block`
   is extracted unchanged from `_build_from_db` (which now calls it);
   `revenue_spend_truth_service.build_google_ads_spend_truth_for_days(start,
   end, settled=)` runs it and `derive_google_ads_spend_truth`; `settled=True`
   adds `analyze_settled_coverage` over the campaign-spend chunks
   (`fetch_spend_coverage` now returns `fetched_at`). Google Ads spend sits on
   Paid Search only; every other row is `channel_spend_not_connected`
   (new `cwt.R_CHANNEL_SPEND_NOT_CONNECTED`), the unresolved row
   `deal_source_not_resolved`, the totals `all_channel_spend_not_connected`.
5. **API.** `/api/channel-funnel` = `{**build_channel_funnel(window, now=now),
   "outcomes": build_channel_outcomes(window, now=now)}`, one `now`; an
   outcomes failure leaves the funnel and reports `channel_outcomes_failed`.
6. **UI.** `renderChannelFunnel`: an outcome strip (Closed-won deals,
   Customers won, Revenue, Google Ads spend); a grouped header (Funnel ·
   contacts acquired | Outcomes · deals closed | Paid media · same days);
   cells through `canonicalMetricHtml` / `rowMetricHtml` with the evidence
   window for View blockers; the unresolved row and its class breakdown; each
   half renders when the other is withheld; the "not on this page yet" bullet
   replaced by the cohort, attribution, original-source, customers, spend and
   ratio notes. The page subtitle says it.
7. **Audits.** Reader cutover: INVENTORY row updated; an evidence-window pass
   (`report["evidence_windows"]`) for 30d/60d/180d/all_time with an
   independent evidence-window restatement and `channel_outcomes` through
   checks 8/9/18. Parity: `EVIDENCE_WINDOW_CONSUMERS` (reported by
   `audit_all_windows`). SQL doctrine registry: no new SQL occurrence (the
   scanner finds none in the new code). CI: the new PG module is in the PG
   step and the did-run list.
8. **Feasibility report.** Section 4 counts won deals per placement class for
   evidence all_time / 180d / 60d / 30d.

### Tests

New: `tests/test_pr_ads_168a_channel_outcomes.py` (41, pure + node) and
`tests/test_pr_ads_168a_channel_outcomes_pg_integration.py` (9, PostgreSQL).
On 1a889b6 38 of the 39 new cases fail (test_20, which asserts the funnel
service stays free of reader terms, holds there by design), and so does the
changed 162i test_13: 39 failures.

Changed: `test_pr_ads_162i_channels_page_revamp.py::test_13` — a contract
change, not a weakening. It banned the word "customers" from the Channels
code because the page then showed contacts only; the page now shows
customers won. It now asserts the stronger properties: distinct headers
("Customer stage (contacts)" vs "Customers won (distinct companies)"), every
"customers" in the code is the won-companies figure, no percentage in the
outcome cell renderer, and the total and strip read the window's metric
(no `reduce(`). `tests/canonical_ledger_fixtures.py`: the fake universe takes
production's `acquisition_contacts` rows and its default rows carry
`hs_analytics_source`, as production's read now does.

### Review round 1

* **BLOCKER — Paid Search ratio was a proxy.** The Paid Search row divided
  HubSpot's Paid Search deals by Google Ads spend. Probe (g1 mapped campaign
  $10k, b1 label approved not Google Ads $50k, d1 GCLID recorded as Direct
  $20k, spend $4,000): published $60,000 / 15.0x; the truth's
  campaign_attributable was $30k. Now the Paid Search row keeps deals,
  revenue and customers and refuses both ratios
  (`channel_population_is_not_spend_population`); a separate line "Google
  Ads (canonical evidence)" (`outcomes.google_ads_line`) reads the truth's
  campaign_attributable + google_ads_unplaced members
  (`canonical_revenue_service.google_ads_evidence_members`, proven against
  the truth's partition counts now carried on `load_won_deals` as
  `attribution_partition`) over the same settled Google Ads spend: $30,000 /
  $4,000 = 7.5x. It overlaps the channel rows and is never added to them.
* **MINOR — failure payload** rendered "Deals closed in all time, to
  undefined": the context line now needs `outcomes.window`.
* **MINOR — funnel source outside the label table** read "the server sent no
  outcome row": the server passes the funnel's sources
  (`build_channel_outcomes(channel_sources=)`) and each gets a measured row.
* **Observation — one calendar.** Deal bounds use `ACCOUNT_TZ`, the settle
  rule the configured zone. The evidence windows, the funnel cohort and
  `ccr.window_bounds` all use `ACCOUNT_TZ`; moving them to the configured
  zone changes Campaign Evidence and the truth's evidence windows too, so it
  is not done here. Instead the spend is unavailable
  (`account_time_zone_differs_from_evidence_days`) whenever the configured
  zone is known and differs. Production: both Europe/London.

### Review round 2

* **MAJOR — the line was described as removing other search engines.** It
  does not: `cwt.attribution_bucket` admits `google_ads_unplaced` on a GCLID
  OR `acquisition_group = google_ads`, which the deal sync sets from a Paid
  Search contact. Probe: g1 GCLID $10k + b2 Paid Search, unmapped, no GCLID
  $50k → the line is 2 deals, $60,000, 15.0x. Decision kept: the platform's
  doctrine stands (only an approved not-Google-Ads label removes a Paid
  Search deal). The page, docs/50, CLAUDE.md and this log now say exactly
  that, and the line carries `evidence_split` (with a GCLID vs Paid Search
  classification only, same snapshot, null unless it sums to the line),
  shown as "N deals: X with a Google click ID, Y by HubSpot source only".
  PG test_07's p1 is written as the deal sync writes it (`classify_source`):
  the line is 3 deals, $7,000.
* **MINOR — ratio gate status.** The Paid Search ratios are now
  `cwt.incompatible_rate(...)` (unavailable), the convention for a
  population mismatch.
* **MINOR — reconcile guard tested** (`_evidence_counts_match`): a tampered
  partition makes the line unavailable; with the guard removed it publishes.
* **MINOR — Paid Search spend cell** is unavailable
  (`spend_shown_on_google_ads_line`); spend stays on the line and the strip.
* **Observation — gated spend block**: when any gate makes the spend
  unavailable, `google_ads_state` reads `unavailable` and `native_spend` is
  null.

### Known gaps

1. Paid Search revenue is HubSpot's original source (labels approved as not
   Google Ads; GCLID deals recorded elsewhere are on other rows); its ratios
   and spend cell are unavailable. The Google Ads line carries the
   comparison over the platform's Google Ads evidence, which still counts
   Paid Search deals from other engines unless their label is approved as
   not Google Ads; its split shows how much rests on a click ID.
2. A deal whose contacts were acquired before the funnel window sits on a
   channel row the funnel shows with 0 contacts.
3. The campaign-spend settle rule applies to Channels only (167A gap 2
   stands for the other pages).
4. Not run against production: the extra column in the universe read, and
   the evidence-window audit pass's cost.

## PR-ADS-169A — an audit starts only within the free headroom and is killed if it exceeds it

**Production evidence (Render, read-only, 2026-11-21).** One 512Mi instance
runs `uvicorn api.server:app` and, inside it, the scheduler; idle memory is
≈ 297 MB. The `platform_certification` job (07:30 UTC, PR-ADS-161J) runs the
six audits of `scripts.audit_platform_certification`.

- `server_failed`, `oomKilled` (512Mi) at **2026-11-19T07:39:19Z** and
  **2026-11-20T07:44:30Z**: the whole web server restarted mid-certification
  and **no `platform_certification_runs` row was written** for either day —
  contrary to "a run that cannot execute is recorded as NOT CERTIFIED".
- 2026-11-21: memory 297 → 369 MB (07:32) → 384 MB (07:38) → 490 MB (07:40);
  `campaign_evidence` then ended with exit −9 after 106.8 s ("unrecognised
  exit code -9", crashed); verdict NOT CERTIFIED. `reader_cutover` took
  529.6 s (447 s on 11-18; PR-ADS-168A added an evidence-window pass).
- Production sizes: `hubspot_contact_funnel` 21,847; `hubspot_deal_ledger`
  578 (190 won); `google_ads_geo_daily_spend` ≈ 35k; campaign daily spend of
  the same order; ≈ 1,410 open SQL coverage incidents.

**Memory model (before).** The audits already ran sequentially, each in its
own `subprocess.run([python, -m, audit, --json])` — a fresh interpreter
(`vfork` + `exec`: the server's pages are never copied), with stdout captured
in the parent. So the instance held the server (≈ 297 MB) **plus one audit
child at a time**, and nothing bounded the child. The cgroup limit is the
instance's, not the child's: when the child grew past ≈ 215 MB the kernel
OOM-killed either the child (11-21, exit −9) or the whole container (11-19,
11-20). The row was written only after all six audits finished, so a dead
container left nothing.

**Measured** on a production-sized synthetic PostgreSQL dataset (21,847
contacts with 24,944 history rows and 21,847 leads and classifications;
36,040 campaign and 35,000 geo spend rows over 900/350 days; 578 deals, 190
won, with companies and a complete scan; 1,410 open incidents). Every audit
holds on it. Peak resident memory (`VmHWM`), peak address space (`VmPeak`)
and wall time, each audit alone:

| audit | RSS before | RSS after | VmPeak before | VmPeak after | wall after |
| --- | --- | --- | --- | --- | --- |
| reader_cutover | 133.3 MB | 97.6 MB | 165 MB | 128 MB | 55 s |
| closed_won_truth | 36.5 MB | 36.6 MB | 68 MB | 68 MB | 0.8 s |
| deal_company_associations | 28.4 MB | 28.3 MB | 61 MB | 61 MB | 0.1 s |
| crm_truth_exceptions | 41.2 MB | 41.3 MB | 73 MB | 73 MB | 1.4 s |
| marketing_outcome_cohorts | 100.4 MB | 69.9 MB | 132 MB | 102 MB | 2.0 s |
| campaign_evidence | 130.4 MB | 97.1 MB | 161 MB | 131 MB | 4.5 s |

The whole run's parent (the CLI) peaked at 15 MB; in the app the parent is
the server, which only reads each audit's report (≤ 110 kB). Production's
child was larger than these figures (≥ 190 MB at 07:40 on 11-21, against
130 MB here before the fix): real text is longer and the `leads` history is
deeper. The ratio is not measurable from here.

**Hotspots found** (main-thread stack sampled at each RSS high-water mark):

1. `canonical_contact_outcome_service.page_reconciliation` — called by every
   page builder an audit exercises, for every window — ran the full `build`:
   `fetch_canonical_inputs` (all latest `leads` rows and all 21,847
   `contact_source_classification` rows as dicts, ≈ 43 MB of Python heap)
   plus a 21k-dict `contacts` population (≈ 24 MB), only to read three
   counts. Callers hold their own data while it runs, so it stacked.
2. psycopg2 builds a new `str` per text cell: 21,847 copies of each status,
   source, source type, campaign name and run date per read, and the whole
   tuple list existed beside the dicts until the conversion finished.
3. `audit_campaign_evidence_certification._function_code` parsed the 7,600-
   line `api/server.py` whole once per structural check (five per run).
4. Not changed: each window and each page builder re-reads its universe
   (no cross-call cache); caching would change what each reader observes.

**The fix.**

a. *An audit is started only within the headroom that is free, and is
   killed if it exceeds it.* This bounds the audit, not the server: requests
   served in-process are not bounded.
   `scripts/certification_child.py` (new): each audit runs as `python -m
   scripts.certification_child --memory-mb A <audit> --json`, a fresh
   interpreter that sets its own `RLIMIT_AS` to A before importing the audit
   and reports `VmPeak`/`VmHWM` in a marker line. Not `preexec_fn`: that
   forces `fork()` of the multi-threaded server and runs Python in the child
   before `exec` (documented unsafe with threads). Measured with a 310 MB
   threaded parent: `Popen` returns in 5.8 ms with `preexec_fn` (fork) and
   0.25 ms without (vfork); both exec a fresh interpreter.
   * **Budget, derived when each audit starts** (`derive_budget`): instance
     limit (cgroup v2 `memory.max` when numeric, else cgroup v1
     `memory.limit_in_bytes` when below the v1 "unlimited" value, else
     `instance_memory_mb: 512`) − instance usage now (the same cgroup's
     `memory.current` / `memory.usage_in_bytes`, else the parent's own
     `VmRSS`) − `safety_margin_mb: 64`. Its inputs are stored per audit
     (`summary.audits[].memory`). In production that is ≈ 512 − 297 − 64 =
     **151 MB resident**; on this machine (cgroup v1, unlimited → config,
     parent 26 MB) it derived **421 MB**. With usage injected at 297 MB the
     six audits on the production-sized dataset all hold under 151 MB (peak
     97.6 MB, 35% headroom).
   * **Floor** `min_audit_budget_mb: 96`: below it (or with an unreadable
     usage) the audit is not started and is crashed "insufficient memory
     headroom (N MB available …): not started" — never skipped.
   * **Watchdog**: the wait loop reads the child's `/proc/<pid>/status`
     `VmRSS` every 0.25 s and SIGKILLs it past the budget: crashed "exceeded
     its memory budget (N MB resident): M MB resident when the watchdog
     stopped it". A child whose own reported peak exceeded the budget between
     two reads is crashed the same way.
   * **Backstop** `audit_address_space_mb: 320` (`RLIMIT_AS`): only for a
     spike faster than the poll. `RLIMIT_AS` counts reserved, untouched
     mappings (VmPeak − VmHWM ≈ 30–35 MB here; `campaign_evidence` is ≈ 97 MB
     of address space after its imports alone), which is why it is not the
     budget. A `MemoryError` that reached the top → crashed "exceeded its
     memory backstop"; a peak within 16 MB of the backstop → crashed, "an
     allocation may have failed inside the audit and its report is not
     trusted"; a missing marker → crashed; −9 → "killed by SIGKILL (signal 9)
     — out of memory".
   * Sequential, never parallel; output to temp files; each audit's budget,
     inputs, `peak_rss_mb` and `vm_peak_mb` are stored and printed.
b. *A row exists from the start.* Schema: `status` (`running` | `finished` |
   `interrupted`, default `finished` for existing rows), `heartbeat_at`,
   `finished_at` nullable. `run_and_record` sweeps, opens the row
   (`running`, exit 2, NOT CERTIFIED, `current_audit`), beats every 60 s
   and at each audit start, and finalizes the same row (a late finish
   replaces an "interrupted" guess with the real verdict).
   `close_interrupted_runs` closes a `running` row whose heartbeat is older
   than `interrupted_after_minutes` (5): NOT CERTIFIED, "heartbeat stopped;
   presumed interrupted" (the cause is not observed). It runs at startup (`start_scheduler`), again after
   the grace (a restarted server is back inside it), and before every run.
   The System page: a live `running` row is the new state `running` ("Not
   confirmed yet — the daily check is running"), never yesterday's verdict
   and never a pass; a stale `running` row or an `interrupted` row is
   `not_certified` with `interrupted`/`could_not_run`; an unknown status is
   not certified. While a run is running or after one was interrupted, the
   "What needs fixing" card shows the last FINISHED run's CRM data to fix,
   labelled with that run's date.
c. *Hotspots reduced, no verdict logic changed.* `db/row_compaction.py`
   (new): `rows_as_dicts` returns the same dicts with equal `str` cells
   sharing one object, reading a real psycopg2 cursor in `fetchmany` chunks
   of 2,000 (test doubles still via `fetchall`); used by
   `revenue_repository._rows_as_dicts`, `crm_funnel_repository._rows_as_dicts`
   and `fetch_canonical_inputs` (shared across its two reads, dates
   included). `crm_funnel_repository._normalised_rows` shares equal `date`
   objects. `build_populations(keep_contacts=False)` accumulates the same
   scope counts without keeping contacts (`contacts`/`excluded` are None,
   not empty); `page_reconciliation` uses it. `build` narrows classification
   rows to the four fields the population reads. `_function_code` parses
   each file once and keeps only positions. Equivalence: the six reports on
   the synthetic dataset are identical before and after (clock fields
   aside); the existing fixture suites (152, 157, 157-F1, 158, 161B, 161E,
   161G, 161J, 162C, monitoring, system status) pass unchanged; new pure
   and PG equivalence cases (test_10–13, 20–22).
d. *The 04:02Z / 05:15Z OOMs.* 2026-11-16 04:02Z was a Monday: the weekly
   job (Mondays 04:00 UTC), killed at the limit about two minutes in every
   Monday (PR-ADS-162H) — retired by PR-ADS-165A on 2026-11-19, so nothing
   runs then now. 2026-11-17 05:15Z (a Tuesday) matches no scheduled job:
   the daily pulse is 03:00 UTC, the incremental sync 06:00 UTC, the monthly
   refresh only on the 1st, and the certification was not yet deployed.
   That is the day the contact scan was rebuilt (PR-ADS-161I; incidents
   334 → 1,401), so a manual `/run/incremental-sync` or a long daily pulse is
   the likely candidate, unproven from the repository. **Next fix:** read the
   Render log around 05:15Z on 11-17 for the job that started, then move it
   onto the same capped-subprocess pattern.

**Tests.** `tests/test_pr_ads_169a_certification_memory.py` (38 cases; in
CI's PostgreSQL step and did-run list), with the stand-in audit
`tests/pr_ads_169a_fake_audit.py`: the watchdog kills a fake audit that makes
pages resident past a 64 MB budget (the parent lives; with room it holds);
the budget shrinks as the reported usage grows and is re-derived at each
audit's start, with its inputs recorded; below the floor an audit is crashed,
not started, not skipped; the backstop still stops a fast reservation; a
swallowed `MemoryError` within the guard band is not a pass; SIGKILL is
named; audits never overlap; no `preexec_fn`; the settings are configured;
classify reads memory evidence before the exit code; the cgroup readers
prefer v2, then v1, then config; the periodic heartbeat beats ≥ 5 times in a
1.5 s audit at 0.2 s; the row exists while running and is finalized in place;
the heartbeat names the audit; a stale row is closed by the startup sweep and
a live one is not; the next run sweeps first; the System page reads
running/interrupted honestly, stays `running` throughout a 2 s audit with a
1.2 s staleness limit, and shows the last finished CRM data; the end-to-end
over-budget run is recorded NOT CERTIFIED; the startup sweep runs now and
after the grace; the equivalence cases. On 7273b73 36 of 38 fail (the two
that pass are equivalence cases); on the first 169A commit (df89cb2) 18 fail
— every budget, watchdog, floor, backstop-wording, cgroup-reader, summary,
message and CRM-card case. With the periodic beat replaced by `pass`,
test_09d and test_24 fail. 161G's test_15 (the six real audits through the
real subprocesses) now states its memory environment as it states its
database one — production's 512 MB with 297 MB used — and asserts the six fit
that 151 MB budget: run late in a full suite without a cgroup limit, the
derivation counted the ~380 MB pytest process as the instance and refused
every audit at the floor, which is the rule working, not a fit.
Through main's runner the allocating and the
swallowing fake audits each certify (`CERTIFIED holds`).

**Known gaps.**

1. Production peaks are not measurable from here. The first production run
   should show, per audit in `summary->'audits'`, `memory.limit_source`
   `cgroup_v2` (or v1), `memory_budget_mb` ≈ 150, and `peak_rss_mb` under it;
   instance memory should peak near usage + the largest `peak_rss_mb`. An
   audit that now shows `crashed` "exceeded its memory budget (N MB
   resident)" or "insufficient memory headroom" is the production-size gap
   made visible, not a regression: the audit is stopped and the day reads
   NOT CERTIFIED; the next step is that audit's own reads.
1a. cgroup `memory.current` counts page cache, so where it is read the
   budget is conservative (smaller than the truly free memory); a day with
   much cache may refuse an audit at the floor.
1c. Where no cgroup limit is readable, the parent's own VmRSS stands for the
   instance's usage. On Render the server is the parent; elsewhere (a CLI
   run inside a large process) the budget is that process's view.
1b. Only the audit is bounded. Requests the server serves in-process during
   the run are not, and the budget is fixed at each audit's start: memory
   the server takes later is not taken back from the running audit.
2. A `MemoryError` that an audit swallows from a single allocation larger
   than the 16 MB guard band is not detected by the band; that audit then
   reports whatever its own exception handler reports (unavailable on the
   read paths, as for any failed read).
3. `reader_cutover` still takes ≈ 530 s in production: its statement count
   (6,400 on an empty database) times network latency. Not addressed here.
4. Other in-app jobs (daily pulse, incremental sync, monthly refresh) still
   run in the server's own process, uncapped.
5. A guard-band (or over-budget) hit on an audit that exited 1 (VIOLATION)
   is reported `crashed`, so the run reads NOT CERTIFIED rather than
   VIOLATION: an unproven report is not used, even to report a violation.
6. Follow-up: `audit_campaign_evidence_certification.py:348`
   (`check_section_contracts_are_complete`, the ownership check) reads
   `_function_code(_API_SERVER, name) or ""`, so a function it cannot find
   reads as one that hand-builds no section dictionary and passes — fail
   open for that structural check. Unchanged here.

## PR-ADS-170A — the owner labels two Google Ads locations that Google reports without a country (November 2026)

Scope: the open item PR-ADS-165A and 167A both recorded — "criteria `2192` /
`2364` stay unlabelled; owner decision".

**Production facts (read-only).** `google_ads_geo_daily_spend` holds two
criteria with no country code on any row: `2192` (17 rows, about £7.64) and
`2364` (8 rows, about £3.34). Google's geo_target_constant lookup answers for
them with no country. They read as "(unresolved location)", and while their
spend was in a window a lead country never seen in Google Ads spend read
"Unavailable — some spend in this window has no country".

**Owner decision, 2026-11-22.** `2192` is Cuba (`CU`); `2364` is Iran (`IR`).

**What changed.**

- `config/geo_owner_labels.yaml` — `owner_labelled_geo_criteria`, one entry
  per criterion: `code`, `approved`, `by`. Only a listed id is labelled.
  Google's "2000 + ISO numeric" id convention is still not applied anywhere:
  an unlisted code-less criterion (`2368`, say) resolves to nothing.
- `analysis/geo_owner_labels.py` loads and validates it. An entry is ignored,
  with a logged warning, unless its key is a criterion id, `code` is in
  `analysis/iso_countries.ISO_COUNTRY_NAMES`, `approved` is a date that is not
  in the future and `by` is non-empty. A missing, empty or unparseable file
  means no labels. **A key repeated anywhere in the file means no labels at
  all** (see F1 below): the file is read with a loader that refuses duplicate
  keys, because YAML's default keeps the last one silently. Loading never
  raises.
- `db/geo_identity_sql.criterion_identity` is the one rule, used by the
  readers that NAME a country (`fetch_geo_daily_spend_by_country`,
  `fetch_geo_criterion_identities`, `fetch_geo_code_carriers`,
  `country_evidence_service.identify_spend_rows`). One geo reader does not use
  it and is not changed: `fetch_geo_reconciliation_breakdown` (the geo
  reconciliation panel's `unmapped_geo_native` / `null_country_rows`) counts
  rows whose STORED `country_code` is NULL, so it still reports the two
  criteria's rows as unknown-country. That is a true statement about storage —
  Google sent no country and the label is never written — but it is not what
  the country pages show for them. The rule:
  stored codes are judged first, exactly as before (one resolves; two that
  disagree resolve to nothing); only a criterion with NO stored code takes its
  owner label, with `provenance: "owner_labelled"`.
- **A stored Google code always wins.** If a labelled criterion has a stored
  code that is not the label's, the stored code is used, the label is not
  applied, and the mismatch is logged (`[geo-identity] … NOT applied`) and
  published as `owner_label_mismatch` on the row and in
  `owner_labelled_geo.mismatches`. Nothing is merged.
- **The label is never written.** The range writer
  (`replace_geo_daily_spend_range`) and the sync's
  `_resolve_country_metadata` still use `resolved_code` (stored codes only), so
  the table stays what Google sent, Google is still asked about a labelled
  criterion on every sync, and an answer it gives later is stored and wins.
  The sync's chunk report names the code-less criteria the readers will label
  (`country_identity.owner_labelled`); `unresolved` still lists every
  criterion stored with no code.
- Countries (`/api/geo`): the two criteria are rows "Cuba" and "Iran" with
  `country_provenance: "owner_labelled"` and `owner_labelled_criteria`; lead
  text resolving to `CU` / `IR` joins them. The payload carries
  `owner_labelled_geo: {criteria, mismatches, note}`. With nothing else
  unresolved, the "(unresolved location)" row and the notice are gone.
- The zero rule (167A) is unchanged; its inputs changed. An owner-labelled
  criterion is not unresolved spend, and it is a carrier of its code
  (`fetch_geo_code_carriers` returns it with `provenance: "owner_labelled"`
  when it has rows on record and none carries a code). A zero that stands on
  such a carrier is marked `owner_labelled` on the row.
- Country Revenue & Spend and Dashboard Countries read the same repo function,
  so the two criteria arrive named. A market row whose geo spend rests on a
  label carries `spend_country_provenance: "owner_labelled"` and
  `owner_labelled_criteria`; `spend_truth.owner_labelled_geo` (mart) and
  `owner_labelled_geo` (dashboard) list the criteria and any mismatch.
- UI: `ownerLabelledCountryNote` prints "set by owner" beside the country
  name on all three pages, with the title "Country set by owner — Google Ads
  reports this location without a country".

**To label another criterion:** add an entry to
`config/geo_owner_labels.yaml` with the owner's approval date and approver,
and a line here. Do not add an id the owner has not approved, and do not add a
rule that derives a code from the id.

**Existing tests changed, and why.** Five 165A / 167A cases used `2192` /
`2364` as "a criterion no stored row names". By the owner's decision those two
are no longer that. The fixtures now use ids that are not in the owner table
(`2997`, `2998`); every assertion about the rule is unchanged. One assertion
gained a field: a carrier now states its `provenance`
(`test_12_the_countries_geo_reads_share_one_snapshot`).

**Tests.** `tests/test_pr_ads_170a_owner_labelled_geo_pg_integration.py` (20
cases; in CI's PostgreSQL step and did-run list): the table holds exactly the
two approved entries; nothing is inferred from an id; a stored code beats the
label and the mismatch is logged and returned; malformed entries and
unreadable files are ignored without raising; the production shape through
the real handler and `loadGeo` (Cuba `$10`, Iran `$4`, both "set by owner",
no "(unresolved location)", no notice, Pakistan `$0`, the Countries total
identical before and after — on that page the label only moves spend between
rows; on the two revenue pages the country rows RISE, see F1 — stored
rows still NULL); an unlisted code-less criterion still unresolved and still
withholding; the owner-labelled criterion as the carrier of its code; Google
later reporting `2192` as Mexico; a malformed config through the page; the
real sync storing NULL, still asking Google, and Google's later answer
winning; neither writer reading the owner table; the mart country view and
Dashboard Countries; the UI helper evaluated in node.

**Known gaps.**

1. Country Revenue & Spend and Dashboard Countries group on the 96-market
   registry (`country_identity.SUPPORTED_COUNTRIES`, unchanged). Iran is in it
   and is a market row on both. Cuba is not, and nor is any other Google
   country outside the registry:
   - **Country Revenue & Spend** — its spend is on the one residual row
     ("Unknown / Unattributed country"), as a named component
     (`spend_components.outside_listed_markets`, with the country names), and
     the Spend cell says so. Since F1 the geo shortfall is ADDED to it; before
     F1 a shortfall overwrote it and that spend was on no row.
   - **Dashboard Countries** — it is on no row and is not in the residual
     tile (which stays the campaign-to-geo shortfall). The "Verified Geo
     Spend" card is the listed markets' spend only; the payload says so
     (`kpis.verified_spend_scope: "listed_markets"`) and states the amount
     outside them (`kpis.spend_outside_listed_markets`), and the card prints
     it. The page is not restructured: there is still no row for such a
     country there.
   Only the Countries page shows a Cuba row.
2. A market row is marked by its country code. If a code were carried by both
   an owner-labelled criterion and a Google-named one, the row's note would
   cover spend of both. Production has no such code.
3. The labels are read once per process: a config change takes effect on the
   next deploy / restart.
4. Not run against production. The row counts and amounts above are the
   read-only figures recorded for this task; the first production read should
   show Cuba and Iran rows on the Countries page for a window holding their
   spend, and `owner_labelled_geo.mismatches` empty.

### F1 — review corrections (same PR)

An independent review of the first commit (`2a42a7d`) found no blocker and
five things to correct. All are in the second commit.

1. **Spend dropped on Country Revenue & Spend (pre-existing; exposed here).**
   `_build_db_rows` puts the spend of a country outside the listed markets on
   the residual row. `_apply_geo_spend_residual` then set that row's `spend`
   to the campaign-to-geo shortfall — written OVER it, not added. With a
   shortfall (`reconciled_with_residual`) the rows summed to less than the
   campaign total: campaign £113, geo UK £100 + Iran £4 + Cuba £3 + Fiji £2 →
   rows summed to £108. The first commit's text "never dropped / every pound on
   exactly one row" was true only with no shortfall. Now the shortfall is
   added; the row carries `spend_components: {outside_listed_markets,
   country_unidentified, geo_unassigned}` (its `spend` is their sum),
   `outside_listed_market_countries`, `spend_geo_unassigned_native/_usd` (the
   shortfall alone) and `spend_native/_usd` of the whole row; an unknown part
   makes the sum `None`. The row's notes no longer say "country could not be
   identified" beside spend whose country is known: that component reads
   "known but … not one of the listed markets". `spend_truth.
   country_residual_native` is unchanged (the shortfall).
   **So "identical totals before and after" holds for the Countries page
   only.** On Country Revenue & Spend the rows' sum rises (to the campaign
   total), and on Dashboard Countries the spend card rises by Iran's spend —
   both because that spend was previously on no row.
2. **A duplicated YAML key failed open.** `yaml.safe_load` keeps the last of
   two equal keys: `"2192": CU` then `"2192": MX` labelled it MX. The file is
   now read with a SafeLoader subclass that raises on a repeated key at any
   level (`"2192"` and `2192` are one key); such a file labels nothing and
   logs why. Also rejected, per entry: an `approved` date in the future, and a
   key that is not ASCII digits.
3. **A mismatch was only in the payload and one log line.** It is now a
   visible notice on the Countries page (`#geo-owner-label-notice`), on
   Country Revenue & Spend and on Dashboard Countries
   (`ownerLabelMismatchNotice`), naming the location, the owner's code and the
   Google code in use.
4. **Provenance wherever the spend is shown.** The marker is also on the
   Countries map hover, the Dashboard Countries bubble tooltip and the row
   drawer. A published `$0` that stands only on the owner's labels says so in
   both of its forms — the country's own carrier is owner-labelled, or
   labelled spend in the window would otherwise have no country — through one
   field, `zero_rests_on_owner_labels`, and one marker ("owner label"). A
   country Google keys under a criterion of its own is not marked: its zero
   would stand without any label. `country_provenance` stays the statement
   "this row's COUNTRY was set by the owner" and is not put on a row such as
   Pakistan.
5. This section's wording (above) on duplicates, on which readers use the
   rule, on totals, and known gap 1.

**F1 known gaps.**

- **No audit reports a mismatch.** The certification audits
  (`scripts/audit_platform_certification.AUDITS`) cover the revenue/customer
  reader contract, closed-won truth, deal-company associations, CRM truth
  exceptions, marketing cohorts and Campaign Evidence. None reads geo spend or
  country identity, and `audit_production_reality` is a legacy-table freshness
  diagnostic. No audit was invented for this: a geo/country truth audit is
  its own piece of work. Until one exists a mismatch is visible on the three
  pages and in the log, and certification is silent about it.
- The Dashboard Countries regional mix is a per-region aggregate with no
  country names; it carries no per-country marker.
- The zero marker needs the carrier read; if that read fails the zero is
  marked as resting on the labels (not proven independent), which can over-
  disclose.
- F1 tests: 9 more cases in the same module (29 in all).

---

## PR-ADS-171A — Meta Ads spend: a read-only connector, per account (November 2026)

**What this adds.** Averroes reads daily campaign spend from Meta (Facebook /
Instagram) Ads, stores it, and can say for any range of days whether that
spend may be published and what it is. GET only; nothing is written to Meta.
**No page shows Meta spend yet** — the Channels page is PR-ADS-171B. The
System page gains one line. Doctrine: `docs/52_META_ADS_SPEND_CONNECTOR.md`.

**Production facts (owner's Render shell, read-only, 2026-11-22).**

- An unversioned Graph call: HTTP 400 `(#2635) You are calling a deprecated
  version of the Ads API. Please upgrade to the latest version: v27.0.` The
  configured version is `v27.0`; code 2635 is its own never-retried failure.
- With `v27.0` and the token in the `Authorization: Bearer` header only, the
  account node and Insights both answer 200.
- Two ad accounts, in two Business portfolios, so two tokens:
  `4730305973961806` "Logistaas Limited", GBP, America/Los_Angeles, status 1,
  no delivery (`{'data': []}`) — the new account; and `944370805110952`
  "Logistaas", USD, Asia/Amman, status 1, `last_30d` spend `'1909.34'`
  (2026-10-23 to 2026-11-21) — the old account, to be suspended in about a
  month, whose history must be kept. The owner intends to move the new account
  to UK time.

**So the connector is multi-account and multi-credential from the start.**

| Piece | Where |
|---|---|
| Connector (GET only; credential sets; paging; retries; typed errors) | `connectors/meta_ads.py` |
| Sync of one account's range; every account; backfill | `services/meta_ads_spend_service.py` |
| Truth for a range of days, summed over accounts | `services/meta_ads_spend_truth_service.py` |
| Reads | `db/meta_ads_repository.py` |
| Writers (account, atomic range replace, failed ledger row, sync state) | `db/writers.py` (end of file) |
| Five tables | `db/schema.py` (end of `_DDL`) |
| Daily step, right after FX | `scheduler/incremental_sync.py::_sync_meta_ads_spend` |
| System page line | `services/system_overview_service._meta_spend_source`; `static/app.js::systemSourceLine` |
| Backfill; smoke test | `scripts/backfill_meta_ads_spend.py`; `scripts/meta_ads_smoke_test.py` |

**Rules that hold (each has a test that fails without it — see below).**

1. *The token.* Sent only in the `Authorization` header. `paging.next` (a URL
   with the token in it) is never requested, stored or logged. No error text
   carries the request URL or the text of a `requests` exception; Meta's own
   `message` is included after secrets are scrubbed from it. An account is called
   only with its own credential set's token.
2. *Replace, not merge.* A re-read range is replaced in one transaction; a
   campaign-day Meta stops reporting stops counting; a failure leaves the
   range as it was.
3. *Verified means reconciled.* A range is verified only when paging was
   exhausted and each day's campaign rows add up to the account-level row
   within one minor unit. Otherwise nothing is stored.
4. *Unknown is not zero.* An empty verified response is a verified zero; a
   range never fetched, or fetched and failed, is unknown.
5. *Each account's own days.* The window ends on the account's own yesterday;
   the settle rule runs in the account's own zone. At 06:00 UTC the Amman
   account's yesterday has ended and the Los Angeles account's has not.
6. *Each account's own currency.* USD converts at 1; GBP through the existing
   daily GBP→USD rates; a currency the provider does not price is never
   converted (`fx_currency_not_supported`).
7. *One account never blocks or damages another.* The step is `success` only
   when every account is verified and convertible, `failed` only when none is
   verified, `partial` otherwise — recorded as `partial`, never folded.
8. *An unreadable account keeps its history.* Stored rows and verified ledger
   rows stay, and ranges past its last verified day are withheld — whether
   it is still configured, sits in a broken credential set, or was dropped
   from the configuration. The only exemption is the owner's explicit
   declaration (`META_AD_ACCOUNT_RETIRED`), honoured only when the ledger
   bears it out and disclosed on every total that rests on it. (Corrected in
   review round 1, below: the first version treated "absent from the
   configuration" as "stopped spending".)
9. *Identity.* A different currency or zone for an account that has stored
   spend is refused. With no stored spend it is accepted, and on a zone change
   the account's ledger rows are dropped so its days are re-read in the new
   zone.

**Registration.** Dataset pair `(meta_ads_api, meta_campaign_spend)` in
`services/dataset_keys.py`; `ACTIVE_SYNC_PAIRS`; freshness registry key
`meta_ads_spend` (no page; not a core dataset); System Status source
`meta_ads_api`. No new run type and no monitoring cadence
(`RUN_TYPE_CADENCE` unchanged; the backfill writes no `sync_batches` row —
review round 1, below). No
new freshness status: while Meta is not configured the dataset reads
`not_run`, which is neutral. `not_connected` exists only on the System page's
Meta line, where it is rendered neutral. `connectors.meta_ads` is a forbidden
import for reader-contract modules (reader-cutover audit, check 16). CI: the
new PostgreSQL module is in the PG step and its did-run list.

**Tests changed outside this PR's own files, and why.**

- `tests/test_pr_ads_154_incremental_sync_hotfix.py::test_5b` — its
  name→value table gained the two Meta key names, because the scheduler now
  stamps that pair by those names. The assertion (declared == stamped) is
  unchanged.
- `tests/test_pr_ads_162c_system_page.py::test_21` — the System page has a
  fourth source; the expected list gained it, and the case stubs it like the
  other three. `::test_40` — on its empty database with no Meta credentials
  the fourth source is `not_connected`; that is asserted on its own, and the
  assertion over the original three sources is unchanged.
- `tests/test_daily_incremental_sync.py::test_datasets_keys_present` — the
  run report has one more dataset key, `meta_ads_api/meta_campaign_spend`.
- `tests/conftest.py` — `db.meta_ads_repository` binds `get_conn` by value, so
  it joined the eager-import list (the PR-ADS-157 guard `test_66` requires it).

**Each guard, shown failing.** Twelve one-line changes to the implementation,
each run against the two new test modules and then reverted:

| Change made to the code | Tests that then fail (first few) |
|---|---|
| reconciliation gate removed | unit `test_39[…reconciliation_mismatch]`, `test_40`, `test_56`; PG `test_12` |
| token also sent as `access_token` in the query | 65 unit cases — the fake refuses every request |
| merge-only writer (no DELETE of the range; upsert) | PG `test_04`, `test_07`, `test_08`, `test_10` |
| window on the UTC day instead of the account's | unit `test_33`, `test_33b`, `test_35`, `test_38` … (41 cases) |
| settle rule in one fixed zone for every account | unit `test_60`–`test_63` (10 cases) |
| every account called with set 1's token | unit `test_33`, `test_47a`, `test_47e`, `test_47f`, `test_47j` … (17 cases) |
| `str(exc)` of the transport error in the message | unit `test_23`, `test_39[…retries_exhausted]`, `test_47j` |
| a failed fetch may overwrite a verified ledger row | PG `test_12`, `test_15` |
| paging stops at a short page instead of at no-`next` | unit `test_13`, `test_14`, `test_16`, `test_17`, `test_39[…page_overrun]` |
| identity change accepted with stored spend | PG `test_15` |
| a removed account ignored by the truth read | unit `test_63e`, `test_63f`, `test_63h`; PG `test_13`, `test_27` |
| a configured unreadable account does not withhold later days | unit `test_63b`, `test_63e`; PG `test_01`, `test_02`, `test_12`, `test_13` |

**Counts (build environment, PostgreSQL available, Python 3.11).**
`tests/test_pr_ads_171a_meta_ads_connector.py`: 187 passed.
`tests/test_pr_ads_171a_meta_ads_pg_integration.py`: 36 passed, none skipped.
Full suite with the one baseline deselect: **6175 passed, 1 skipped,
1 deselected** (37m38s). `compileall`, `node --check static/app.js` and
`git diff --check` clean. A first full run found two contracts this PR had to
meet — the conftest eager-import list and the System page's empty-database
case — both listed above; the run reported here is after them.

**Owner steps** (in full, with the smoke test's expected output:
`docs/52_META_ADS_SPEND_CONNECTOR.md` §10).

1. Deploy. The two credential sets are already in Render
   (`META_ADS_ACCESS_TOKEN` + `META_AD_ACCOUNT_ID=4730305973961806`;
   `META_ADS_ACCESS_TOKEN_2` + `META_AD_ACCOUNT_ID_2=944370805110952`).
2. `python -m scripts.meta_ads_smoke_test` — per account: name, currency, time
   zone, status, active campaigns, last-30-day spend, and yesterday's campaign
   sum against the account total. Exit 0 and `=== 2 of 2 account(s) OK ===`.
3. `python -m scripts.backfill_meta_ads_spend --from <first day> --dry-run`,
   then without `--dry-run`. Both accounts, from the same first day.
4. System page: "Meta Ads spend — Complete through <date>", one line per
   account on its own date.
5. Move the new account to UK time **before** its first ad, then backfill it
   again (`--account 4730305973961806`).
6. Keep the old account configured for as long as Meta answers for it. Only
   when Meta refuses it for good: set
   `META_AD_ACCOUNT_RETIRED=944370805110952:<last spend day>`, check the
   System page, and only then remove `META_ADS_ACCESS_TOKEN_2` and
   `META_AD_ACCOUNT_ID_2` (`docs/52` §10 step 7). Before any backfill, run
   `python -m scripts.meta_ads_smoke_test --month YYYY-MM` for a month with an
   archived campaign.

**VERIFY — facts about the live API not checkable from the build
environment** (`docs/52` §9, in full): daily rows from `time_range` +
`time_increment=1` at both levels; the campaign-level fields; whether
`level=campaign` includes archived and deleted campaigns; that each day's
campaign rows add up to the account row within one minor unit; that a row
always carries `spend`; `limit=500`; `paging.cursors.after` beside
`paging.next`; the `effective_status` filter (smoke test only); the throttle
codes and usage headers; what a suspended account returns; whether an
account's zone can be changed in place; `account_status` meanings;
`appsecret_proof`; how far back Insights serves daily data. Confirmed and
removed from the list: the API version, and the Bearer header on the account
node and on Insights.

**Decisions to review** (`docs/52` §11). The owner's retirement declaration
is the one value not taken from Meta. Any account not verified for a day
withholds the total for that day. A zone change on an account with no spend
drops that account's ledger rows.

**Not in this PR.** The Channels page (171B). Meta campaign, ad set or ad
breakdowns beyond campaign-day spend. Any outcome joined to Meta spend. An
admin status route (the System page line carries the same facts).

### PR-ADS-171A — review round 1 (independent review; second commit)

Two blockers and four majors, each a surface that could state a number the
evidence does not support. Each fix has a regression test that fails against
the first commit (`3cce252`).

| # | Defect | Fix | Regression (fails on `3cce252`) |
|---|---|---|---|
| 1 | **A broken credential set read as "removed".** With `META_ADS_ACCESS_TOKEN_2` lost and `META_AD_ACCOUNT_ID_2` still set, the old account dropped out of the configured list, was treated as retired, and a range past its last verified day published `verified` without its spend. | Configured = every id LISTED in any set, complete or broken (`config_status.listed_account_ids`); all are required through the last day asked for. An account list with an entry that is not an id → `source_unavailable`. | unit `test_63f2`; PG `test_13b` |
| 2 | **"Absent from the configuration" published as "stopped spending".** | Rule deleted. A stored account that is not configured is required like any other. The only exemption is `META_AD_ACCOUNT_RETIRED=<id>:<last spend day>`, honoured only when verified coverage reaches that day and no stored spend is dated after it. A total that rests on it carries top-level `owner_declared_retired` and `owner_declared_retired_disclosure`. | unit `test_63e2`–`test_63e7`, `test_63f`, `test_63h`; PG `test_13`, `test_27` |
| 3 | **Configured but never read was `not_connected`.** | Any listed account is relevant: `coverage_incomplete`. | unit `test_63g`; PG `test_17` |
| 4 | **An empty 200 at both levels deleted stored spend** (nothing reconciles with nothing). | A re-read that would take a day with verified, non-zero spend to zero is refused inside the replace transaction (`meta_restatement_implausible`); rows and ledger untouched. Amount changes and a campaign-day going while its day keeps spend still pass. Override only by the backfill's `--accept-zero-restatement`. | unit `test_78`, `test_56`; PG `test_10`, `test_10b` |
| 5 | **The System line read "late" about 22 hours a day for the Los Angeles account after a clean sync** (judged at page-view time). | Each account is judged against the last day its most recent DUE sync could settle in its own zone (`last_due_sync`, three hours' grace). `misconfigured` is shown whatever other accounts say. `usd_unavailable` instead of "Complete through" while spend has no US-dollar value. | unit `test_79`, `test_80`; PG `test_27b`, `test_27c`, `test_26`, `test_03` |
| 6 | **A backfill overwrote the daily feed's freshness** (a failed daily run, then `backfill --account A`, read fresh; a partial backfill could move the proven-through date). | The Meta backfill writes no `sync_batches` row — as the Google Ads spend and geo backfills never did. Only the daily step moves `sync_state`. | unit `test_56`; PG `test_28`, `test_28b` (through `/api/datasets/freshness`) |

Also: `error.code` / `error_subcode` are used only as integers (unit `test_76`,
`test_77`); the "no error text contains a URL" claim is corrected to "the
request URL" — Meta's own message is included after scrubbing; a test for the
cursor that does not advance (unit `test_75`; the check already existed, so
this one passes on `3cce252`); `first_verified_days` at the top level of the
truth payload (unit `test_63h2`); `scripts/meta_ads_smoke_test.py --month
YYYY-MM` compares campaign rows with the account total for a whole month, to
be run for a month with an archived campaign BEFORE any backfill (unit
`test_81`). No filter parameter for archived campaigns is sent: none could be
verified.

**Tests whose pinned behaviour this round reversed, and why.** Only where the
review named the behaviour as the defect:

- unit `test_63g` (last assertion) and PG `test_17`: asserted `not_connected`
  for a configured account that was never read — item 3.
- unit `test_63e` / `test_63h` and PG `test_13` / `test_27`: asserted that an
  account dropped from the configuration keeps publishing later ranges —
  item 2.
- PG `test_10` (was "an empty response empties a range that held spend") —
  item 4; it is now the regression for the refusal. PG `test_07` and `test_08`
  build their own replacement rows and were adjusted so they no longer zero a
  verified day (`test_08` passes the explicit flag for its last step).
- unit `test_56` and PG `test_28`: asserted `sync_type='backfill'` batches —
  item 6.
- PG `test_03`: the "late" case moved from page-view time to after the missed
  sync was due — item 5.

No assertion was loosened to make an implementation pass.

**Counts after this round.** Unit module 214 passed; PostgreSQL module 41
passed, none skipped. Full suite with the one baseline deselect: **6207
passed, 1 skipped, 1 deselected** (39m18s). Against `3cce252` the same two
modules: 45 failed, 210 passed.

### PR-ADS-171A — review round 2 (third commit): the retirement declaration

Round 2's verdict was ship, with six points on `META_AD_ACCOUNT_RETIRED`
closed first. One pure function now holds the rule
(`meta_ads_spend_truth_service.declaration_verdict`).

| # | Point | Regression (fails on `7be6b23`) |
|---|---|---|
| 1 | A declaration is honoured only for an account that is NOT being read (in no credential set, or `not_readable_since` set). A configured, readable, declared account stays required; the declaration is reported `retired_declaration_account_still_read` in the truth result and on the System line. | unit `test_82`; PG `test_27a` |
| 2 | A declared day past the last verified day withholds only ranges that touch the unread days in between; ranges up to the last verified day publish as measured, ranges wholly after the declared day publish with the disclosure. The owner never has to declare a false day. | unit `test_83`, `test_84`; PG `test_13` |
| 3 | `owner_declared_retired` entries carry `relied_from`, the first day the figure rests on the declaration; the disclosure string states it. | unit `test_63e3`; PG `test_13` |
| 4 | Every account validly declared, none configured, range after all of them: `verified_zero_spend` WITH the marker and disclosure — not an unmarked `not_connected`. | unit `test_63h`; PG `test_27` |
| 5 | The text of an entry that failed to parse is never echoed: `config_status`, the run error, the batch, the logs and the System line carry a count and 1-based positions only (`retired_invalid_positions`, `unparsed_account_entry_count`). The backfill script no longer echoes a bad `--account` value. | unit `test_85`, `test_63e6`, `test_63e7`; PG `test_27a` |
| 6 | `act_` is stripped case-insensitively wherever an id is parsed, so `id:…,ACT_id:…` with two days is one id declared twice: neither is honoured. | unit `test_63e6` |

Assertions reversed by these points, each pinning what the point changes:
unit `test_63h` and PG `test_27` (`not_connected` → the marked zero, item 4);
unit `test_63e3` and PG `test_13` (marker shape and disclosure text, item 3;
and in PG `test_13` the System line for a declared day past the ledger, which
is now "declared retired" with the unread days named, item 2); unit
`test_63e6` / `test_63e7` (raw entries → positions, item 5).

`docs/52` §6 carries the rule as a table and a note for PR-ADS-171B: any
surface that shows a figure with `owner_declared_retired` non-empty must show
the disclosure, and 171B must add an audit/AST guard for it.

**Counts after this round.** Unit module 233 passed; PostgreSQL module 42
passed, none skipped. Full suite with the one baseline deselect: **6227
passed, 1 skipped, 1 deselected** (37m49s). Against `7be6b23` the same two
modules: 28 failed, 247 passed.

## PR-ADS-172A — a campaign drawer section no longer builds the account's search terms

**Production evidence.** The daily certification's sixth audit,
`campaign_evidence`, was SIGKILLed on 2026-11-21 and on 2026-11-22 was crashed
"reached its memory backstop (320 MB of address space)" at `peak_rss_mb` 287
after 114 s; the other five held at 36–127 MB. NOT CERTIFIED both days. The cap
(PR-ADS-169A) worked as designed and is unchanged: backstop 320 MB, safety
margin 64 MB, the audit still in `AUDITS`.

**Measured on a production-scale SYNTHETIC PostgreSQL dataset** (live row
counts — `search_terms` 228,043, `keyword_daily_facts` 98,152, `leads` 27,823,
contact funnel 21,866 — with invented content: 62,706 distinct queries, 102,990
term × campaign units, 9,563 flagged fact rows, 9,596 annotations). These are
not production figures. Peak resident growth of one call, `all_time`:
`build_campaign_flagged_preview` 836 → 114 MB; `build_campaign_keyword_preview`
118 → 39 MB; `build_campaign_evidence` 34 MB, unchanged. The six-window audit:
902 MB uncapped before; under the real cap 294 MB resident at the 320 MB limit
before and 175 MB resident / 208 MB of
address space after, in 19 s instead of 52 s. No accumulation across windows
was measured (six windows 175 MB, `all_time` alone 175 MB), so the audit loop
is untouched. Full table: `docs/51_CAMPAIGN_EVIDENCE_DRAWER_MEMORY.md`.

**Cause.** Each drawer section composes its page builder with a campaign
filter, and the builder loaded the account. The flagged section built every
term × campaign unit in the window — `fetch_search_term_aggregates` (+255 MB,
102,990 dict rows), the units (+250 MB), `fetch_search_term_daily_costs`
(+264 MB, 227,952 dict rows) — to publish ten flagged rows of one campaign. The
keyword section read 98,152 (criterion, day) cost rows as dicts (+116 MB).

**The fix.**

a. `build_flagged_search_terms(population="flagged_candidates")`, passed by
   `build_campaign_flagged_preview`; default `complete`, so the Flagged page is
   unchanged. `fetch_search_term_aggregates(flagged_candidates=True)` asks the
   database which raw terms carry durable evidence (`BOOL_OR(is_flagged_waste
   IS TRUE) OR search_term IN (SELECT search_term FROM waste_terms)`), adds
   every other spelling that normalizes to the same term (decided by
   `normalize_search_term` over a server-side cursor, never re-written in SQL),
   narrows both of its statements with `search_term = ANY(%s)` and returns the
   list; `fetch_search_term_daily_costs(search_terms=, folded=True)` takes the
   same list. A flagged unit, its metrics, `total_count`, the KPIs, the facets,
   the annotation join and the truth state are computed by the same code over
   units that are complete in every spelling and every campaign. If the CRM
   ever carries exact user queries (`population_has_text`), attribution depends
   on unflagged units too and the complete population is built instead. A
   narrowed response declares `population_scope`.
b. `fetch_keyword_daily_costs(folded=True)`: one `{iso_date: micros}` mapping
   per criterion from a server-side cursor instead of one dict per (criterion,
   day). `keyword_evidence_service._build_population` always asks for it, so
   the Keyword Evidence page shrinks too (123 → 43 MB at `all_time`).
c. `_build_population` (search terms) lets go of the grouped rows and the cost
   rows as soon as they are merged.

No published number or payload shape changes: account scope is still applied in
SQL before aggregation, a preview limit is still never the total, and every
existing test passes unmodified.

d. *Review round — a section that was not built is not a section that held.*
   `MemoryError` is re-raised ahead of every broad handler in the two
   repositories, the two services and the audit's `_audit_window` (it used to
   become "unavailable", so under the cap the pre-fix audit could report a
   false pass or a false violation depending on which allocation failed). The
   audit classifies every section reason code
   (`classify_section_reason`): a declared withholding passes, a failed build
   or an unrecognised reason is `unavailable_now` (exit 2), a scope breach is
   a violation. `summarize` keeps `audits[].drawer_sections` so the stored run
   row proves both sections were built per window. The narrowed read is one
   `REPEATABLE READ, READ ONLY` snapshot. A failed per-date cost read makes
   the population unavailable instead of publishing `fx_incomplete` in an
   available response (pages included).

e. *Second review round.* `google_ads_customer_not_configured` is a FAILURE
   for the audit (exit 2): with the headline built and no account, no section
   was read, and that run used to exit 0. A failed FX rate read makes the
   population unavailable (`FxRatesUnavailable`); a rate missing for a day is
   still `fx_incomplete`. The `MemoryError` guard extends to `fetch_fx_rates`,
   `fetch_canonical_campaign_spend`, `fetch_campaign_identity`,
   `fetch_sql_contacts` and `campaign_evidence_service`. A quarantined flagged
   population still passes but is stored as `audits[].drawer_warnings`.

**Production observation (read-only, 2026-11-22).** 66,455 distinct terms,
101,852 term × campaign units, zero flagged rows, `waste_terms` empty. The
narrowed read therefore loads no unit in production today; the section is
available with a true zero total and its memory should be near the floor —
conditional on that count, and growing with it.

**Tests.** `tests/test_pr_ads_172a_campaign_evidence_memory.py` (49 cases; in
CI's PostgreSQL step and did-run list). Of the first 20, 17 fail on `main`,
three of them for the memory reason (test_21, test_22, test_24) and 14 on the
missing API; of the review round's 22 selected cases 20 fail on the first 172A
commit. Pure: the folds equal the per-day rows;
the builders ask for the narrow reads; an unknown population is refused; a CRM
with exact queries gets the complete population. Real PostgreSQL, adversarial
fixture: narrowed == complete as whole dictionaries for 6 windows × 5 campaign
filters × 3 sorts; the narrowed population omits what cannot be flagged and
holds complete units; the preview total is the campaign's whole flagged count;
an unexplained flag quarantines both; an unconfigured account is unavailable;
an empty term list selects nothing. Production-scale synthetic data: flagged
growth ≤ 170 MB, keyword ≤ 70 MB, narrowed == complete at scale, and the real
audit through `_run_capped` + `classify` holds at ≤ 75% of a 293 MB budget with
both sections available in every window. The three of the first 20 that pass
on `main` are the dataset's row counts, the preview total (already right) and
the complete view declaring no narrowed population.

**Known gaps.**

1. The Search Terms pages still build the complete population inside the web
   server: `/api/search-term-evidence`, `/flagged` (the page), `/patterns`,
   `/patterns/detail`, `/term`, `/export` and `/api/waste` — 702 MB of growth
   at `all_time` on this dataset (839 before; only (c) applies), 170 MB at 30d.
   On a 512 MB instance that is an OOM risk a user can trigger from the window
   selector. Terms and Patterns publish every unit and need pagination or
   aggregation in SQL; the Flagged page could take the narrowed population
   once its audit block's "every source row" claim is restated.
2. The flagged section's cost now follows the account's flagged and annotated
   terms (13,387 candidate units ≈ 110 MB here), because `truth_state` and
   `annotation_join` are account-wide by contract.
3. Production's term mix is unknown from here; the first production run should
   show `campaign_evidence` `holds` with `peak_rss_mb` well under its budget.

---

## PR-ADS-171C — Meta ad sets: daily spend per ad set, and Meta-reported lead actions (November 2026)

**What this adds.** Each Meta ad set has its own budget, targeting and
countries, so the owner judges ad sets separately. Averroes now reads one row
per ad account, ad set and day — spend, impressions, clicks and Meta's own
lead-related action counts — and can say for any range of days whether that
detail may be published. GET only; the existing `ads_read` token is
sufficient; nothing is written to Meta. **No page and no API route shows it
yet** (PR-ADS-171B / 171D). Doctrine: `docs/53_META_ADS_ADSET_DETAIL.md`.
Depends on PR-ADS-171A (`docs/52`), whose rules are unchanged.

**Production context (owner, 2026-11-22).** Account `944370805110952` (USD,
Asia/Amman) has $5,927.77 stored since 2026-04-20 in two campaigns; account
`4730305973961806` (GBP, America/Los_Angeles) is a verified zero. The ads use
Meta native lead forms.

| Piece | Where |
|---|---|
| The ad-set read (`level=adset`; `actions` → lead-related types; current names) | `connectors/meta_ads.py::iter_adset_daily`, `meta_reported_lead_actions` |
| Sync of one range; the daily pass; reconciliation to campaign rows | `services/meta_ads_adset_service.py` |
| The read function for the future page | `services/meta_ads_adset_truth_service.py::build_meta_adset_breakdown_for_days` |
| Reads (ad-set ledger; campaign base; aggregated rows + read-time reconciliation) | `db/meta_ads_repository.py` |
| The atomic range replace; the ad-set failure recorder | `db/writers.py::replace_meta_adset_chunk`, `record_meta_adset_coverage_failure` |
| Tables `meta_ads_adset_daily_spend`, `meta_ads_adset_coverage` | `db/schema.py` |
| The sibling daily step and its batch | `scheduler/incremental_sync.py::_sync_meta_ads_adsets` |
| `--level adset`; `--adsets` | `scripts/backfill_meta_ads_spend.py`, `scripts/meta_ads_smoke_test.py` |

**The rules that matter.**

1. **The campaign level stays the published base.** An ad-set chunk is
   `verified` only when every day of it is inside a verified campaign chunk
   and, per day and campaign, the ad-set rows sum to the STORED campaign row
   within one minor unit. A mismatch fails the chunk and stores nothing.
2. **The ad-set level cannot demote campaign truth.** Its own two tables, its
   own ledger, its own dataset key (`meta_ads_api/meta_adset_spend`) and
   `sync_batches` row, written by a sibling step that starts after the
   campaign batch is closed. No ad-set writer names a campaign table, the
   campaign ledger or `meta_ads_account_sync_state`. A second ledger rather
   than a `level` column on the existing one, so the guarantee is structural
   rather than a filter every reader has to remember.
3. **Run status.** Campaign verified + ad sets failed = the campaign dataset
   `success`, the ad-set dataset `failed` (or `partial` when only some
   accounts failed), and the run `partial` — never `success`, never `failed`.
   No new run type; `RUN_TYPE_CADENCE` unchanged; no new freshness status.
4. **The read function re-checks reconciliation in the database.** The
   campaign level is re-read every morning; if the ad-set read then fails,
   the ad-set rows describe yesterday's campaign figures while their ledger
   row still says `verified`. `fetch_meta_adset_spend` compares the two
   tables per day and campaign in one REPEATABLE READ snapshot and the read
   function withholds on any difference.
5. **One account short withholds every account.** Which accounts a range
   needs, and through which day, is the campaign truth's decision
   (`build_meta_ads_spend_truth_for_days`), called, not re-implemented.
6. **Leads are Meta's own counts and are named so.** `meta_reported_leads` /
   `meta_reported_lead_actions`; never contacts, CRM leads, MQLs or SQLs;
   never in arithmetic with a HubSpot count.
7. **The lead action types overlap and are never summed.** Each lead-related
   `action_type` (name contains `lead`) is stored with its own count in
   `meta_reported_lead_actions` (JSONB).
8. **Unknown is not zero.** A row with no `actions` key stores SQL `NULL`; a
   row whose `actions` holds no lead type stores `{}`. One row without
   `actions` makes the range's lead figures unavailable. The same for a
   missing `impressions` or `clicks`.
9. **Names are labels.** `adset_id` and `campaign_id` are the identity.
   Insights returns the CURRENT name on every row; a stored name is "what
   Meta called it when this row was last fetched". Nothing joins on a name.

**What Meta's documentation says about lead action types, and the decision.**
The Insights reference describes `actions` only as "The total number of
actions Accounts Center accounts took that are attributed to your ads" and
lists no action types; the word "lead" does not occur on the page. The
Breakdowns page says the entries "are hierarchical and include detailed
actions not counted". The `AdsActionStats` reference page could not be
retrieved from the build environment. So the documentation does not make the
choice unambiguous: **no named native-lead column is stored**, the raw
per-type counts are, and `meta_reported_leads` is `unavailable`
(`meta_lead_action_type_unverified`) until
`meta_ads.native_lead_action_type` names ONE type. The owner's
`meta_ads_smoke_test --adsets` output is what will decide it.

**Clicks** are Meta's `clicks` ("The number of clicks on your ads"), the
figure the campaign rows already hold — not `inline_link_clicks`, which Meta
reports on a fixed 1-day-click window.

**Tests.** `tests/test_pr_ads_171c_meta_adsets.py` (47 cases) and
`tests/test_pr_ads_171c_meta_adsets_pg_integration.py` (14 cases, in CI's
PostgreSQL step and its did-run list). The fake Graph API
(`tests/meta_ads_fakes.py`) gained ad sets: rows that roll up into their
campaign's row, the current name on every row, `actions` only where a row
has some.

**Guards shown failing** (each mutation applied to the final tree, the two
171C modules and the 171A PostgreSQL module run, then reverted):

| Mutation | Fails |
|---|---|
| Reconciliation removed (`sync_adset_chunk` ignores the verdict) | unit `test_13`, `test_14`, `test_26`, `test_28`; PG `test_03` |
| An ad-set failure marks the ACCOUNT failed in `meta_ads_account_sync_state` | unit `test_13`, `test_16`, `test_28`; PG `test_03`, `test_04`, `test_12` |
| An ad-set failure recorded in the CAMPAIGN ledger | unit `test_28`; PG `test_03`, `test_04`, `test_12` |
| A missing `actions` key read as "no leads" (`{}` instead of unknown) | unit `test_06`, `test_12`, `test_35`; PG `test_01`, `test_02` |
| Overlapping lead types summed into `meta_reported_leads` | unit `test_07`, `test_08`; PG `test_02` |
| `partial` collapsed to `failed` | unit `test_13`, `test_16`, `test_18`; PG `test_03`, `test_04`, `test_05`, `test_09` |
| `partial` collapsed to `success` | unit `test_13`, `test_16`, `test_18`, `test_28`; PG `test_03`, `test_04`, `test_05`, `test_09` |
| The read-time reconciliation check removed | PG `test_05` |
| The "campaign base verified" check removed | unit `test_19`, `test_34`; PG `test_12` |
| The ad-set coverage gate removed from the read function | PG `test_03`, `test_04`, `test_09`, `test_11` |
| A failed row may replace a verified one (the shared recorder's guard) | PG `test_04`, `test_05`; 171A PG `test_10`, `test_10b`, `test_12`, `test_15` |

**Existing tests changed** — three, each because a registry legitimately grew
by the ad-set pair; no assertion was loosened:

* `tests/test_pr_ads_171a_meta_ads_connector.py::test_51` — the datasets of
  source `meta_ads_api` are now exactly `["meta_ads_spend", "meta_ads_adsets"]`
  (was `["meta_ads_spend"]`).
* `tests/test_daily_incremental_sync.py` — the daily run's dataset labels gain
  `meta_ads_api/meta_adset_spend`.
* `tests/test_pr_ads_154_incremental_sync_hotfix.py` — the name→value table
  of the declared-equals-stamped check gains `META_ADS_ADSET_DATASET`.

**Not verified against live Meta** — `docs/53` §9: the shape of a
`level=adset` row; whether Meta omits `actions` for a row with no action;
whether archived ad sets are included; rounding across many ad sets; which
action type is the native-form lead count; that
`use_unified_attribution_setting` is accepted with `ads_read`; whether
`impressions`/`clicks` are always present; the default `action_report_time`.

**Owner steps** (`docs/53` §10): deploy;
`python -m scripts.meta_ads_smoke_test --adsets` and send back the output;
then `python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --level adset`
(the SAME `--from` as the campaign-level backfill, which ran from 2025-01-01).

**Counts.** Unit module 47 passed; PostgreSQL module 14 passed, none
skipped. The 171A modules: 233 and 42 passed, unchanged in number.

**Review round 1.** An independent review found no blocker; fixed in place:
* a configured lead type could publish a confident 0 on every ad set. A type
  the sync never stores (no `lead` in it) is now refused
  (`meta_lead_action_type_not_stored`, a fixed message in `config_problems`,
  the string not echoed), and 0 is published only when that exact type
  appears in that account's stored ad-set rows on some day, otherwise
  `meta_lead_action_type_not_observed` — per account
  (`db/meta_ads_repository.fetch_meta_adset_spend(observed_action_type=)`,
  same snapshot). `test_08` had pinned the defect (0 for `{}` counts with no
  evidence the type is ever emitted); its assertion is corrected. New:
  unit `test_36`, PostgreSQL `test_15`–`test_17`, shown failing on c0a8077
  except `test_16` (a type seen on day 1 is still 0 on day 2 — preserved).
* `docs/53`: ad-set backfill uses the campaign backfill's `--from`
  (2025-01-01), or all-time ad-set detail stays withheld (§10); the smoke test
  does not exercise VERIFY 3 — the backfill fails loudly instead (§9); one
  malformed `actions` entry fails the whole chunk, spend included (VERIFY 9);
  the custom-conversion claim is marked unproven (§11); the System page's
  "Meta Ads API" source card and worst-of verdict now reflect ad-set health
  (§5).

Counts after round 1: unit module 50 passed; PostgreSQL module 18 passed,
none skipped; the 171A modules 233 and 42, unchanged.

## PR-ADS-173A — the Search Terms pages no longer build the account's window as Python dicts

**The exposure.** Production is one web process on a 512 MB instance (idle
≈ 120 MB). `/api/search-term-evidence` (Terms), `/flagged`, `/patterns`,
`/patterns/detail`, `/term`, `/export` and the `/api/waste` adapter each built
every term × campaign unit of the selected window in Python on every request,
with no cache and no limit — the item PR-ADS-172A listed first under "Not
fixed". A signed-in user choosing "All time" and opening Search Terms could get
the server killed.

**Measured on a production-scale SYNTHETIC PostgreSQL dataset** (PR-ADS-172A's:
the live `search_terms` row count, 228,043, with invented content — 62,706
distinct queries, 102,990 term × campaign units, 9,563 flagged fact rows).
These are not production figures. Peak resident growth of one request, MB,
`all_time` (the dataset holds 180 dates, so `180d` is the same population):

| route | before | after |
| --- | --- | --- |
| Terms | 673 | 75 |
| Flagged page / `/api/waste` | 677 / 678 | 79 / 79 |
| Patterns | 680 | 107 |
| Pattern drawer | 679 | 78 |
| Term drawer | 681 | 1.6 |
| CSV export (20 MB file) | 673 | 117 |

At `30d`: 144–159 before, 0.1–46 after. Full table, allocators and method:
`docs/54_SEARCH_TERMS_PAGES_MEMORY.md`.

**Cause.** 622 MB traced at the end of `_build_population`: ≈ 222 MB the ten
`set` objects of each unit, 91 MB the 25-key unit dicts, 62 MB one dict per
(term, campaign, day) cost row, 41 MB the per-unit per-date dicts, 19 MB a
stored provenance dict per unit; the grouped rows were another ≈ 255 MB while
being merged. Pages added more on top (49 MB of per-unit attribution state on
Terms, 80 MB of per-term and per-pattern dicts on Patterns).

**The fix.** Normalization and campaign identity are decided in Python and are
NOT re-expressed in SQL; the units still exist, they are just no longer
expensive.

a. `fetch_search_term_aggregates(sink=)` and
   `fetch_search_term_daily_costs(sink=)` stream their rows from server-side
   cursors into the units. A reader offering only the row-list form (the
   two-argument stand-ins existing tests install) is asked for rows and gets
   the same merge (`reader_accepts`). The grouped statement is now ordered
   (`search_term, campaign_name, campaign_id`, byte order), so a few details
   that followed PostgreSQL's unordered output — the order of two same-named
   campaigns in a facet, of two flagged rows with an equal sort key, of a term
   drawer's campaign list — are deterministic. No value changes.
b. `_Unit`: the same fields under the same keys, read through the same
   `unit["key"]` / `.get()` protocol, stored in slots with shared frozensets, a
   flat per-date tuple and a derived `_provenance`. The FX arithmetic is the
   unchanged `_fx_convert_population`.
c. Only the returned page is serialized: Terms selects it with a heap
   (`_sorted_page`); the Flagged view keeps an index of the fields its filters /
   sorts / KPIs / facets read (`_FlaggedIndexRow`, which raises on any other
   key) and builds full rows for the page; Patterns uses slotted records and
   releases the units first; the pattern drawer decides membership before
   unifying; the term drawer loads the units of its one normalized term
   (`normalized_terms=`), or the complete population if the CRM carries exact
   queries; the CSV export writes one row dict at a time into a complete
   body, releases the population and returns it with its Content-Length —
   never a stream, so a failure on any row is a 503 and not a short
   `…_complete.csv` under HTTP 200 (review fix F1).
d. With no contact carrying a query, a unit's attribution state is asked for
   per mapping status (`unit_state_without_text_evidence`, the rule
   `_attribute` uses) instead of once per unit.
e. `population_snapshot()`: every `search_terms` / `waste_terms` statement of a
   build runs on one `REPEATABLE READ, READ ONLY` connection. Before, a sync
   committing between the grouped rows and the per-date costs produced units
   whose total and per-date amounts disagreed in a response that reconciled.
f. `_search_term_evidence_call`, `/api/waste` and the export route let
   `MemoryError` through instead of answering it as "database unavailable";
   so does `fetch_reviews_for_identities`, which answered it as "review state
   unavailable" (review fix). The other readers a build calls already did.

**Proof.** `tests/test_pr_ads_173a_search_terms_pages_memory.py`, registered in
CI's PostgreSQL step and did-run list: 364 requests on a real server publish
payloads equal **as whole documents** to the ones captured from the parent
commit `6c1cb04` (`tests/pr_ads_173a_scenarios.py`, `tests/fixtures/`); every
route is bounded under 150 MB at `180d` and `all_time` on the scale dataset; a
failed cost read and a failed FX read are unavailable on every route; a stream
that breaks midway publishes nothing; out of memory is raised. Against the
parent commit the measured cases fail at ≈ 675 MB and the snapshot case fails.
Every existing test passes unmodified.

**No refusal guard was added** — nothing measured needs one.

**Not fixed.**

1. If `leads` ever carries exact user queries, per-unit attribution adds
   ≈ 80 MB (Terms ≈ 157 MB at `all_time`, simulated). Above target, unguarded;
   bound `_attribute` before turning `LEADS_HAVE_SEARCH_TERM` on.
2. Concurrent requests add up; nothing serialises population builds.
3. A build holds two of the pool's ten connections at once, halving the
   concurrency (≈ 5 builds) at which `getconn` raises and the read without a
   connection reports unavailable.
4. The snapshot covers `search_terms` and `waste_terms`, not canonical spend,
   identity, FX rates, reviews or SQL contacts.
5. The Flagged page still builds the complete population (`docs/51` §6.1).
6. Production peaks are not measurable from here.

## PR-ADS-171B — Meta spend on the Channels page and the Dashboard (November 2026)

**What this adds.** A separate "Meta Ads (spend)" line on the Channels page,
beside the "Google Ads (canonical evidence)" line, and the same line as a panel
on the Dashboard's Channels tab. It republishes
`meta_ads_spend_truth_service.build_meta_ads_spend_truth_for_days` — the only
reader of Meta spend totals — over the page's settled dates, and nothing else:
USD spend, the native total when the truth gives one, each account's own
figures when the total is published, each account's zone and verified-through
day, and the owner's retirement disclosure whenever the figure rests on it.
HubSpot's Paid Social contacts are split by drill-down 1 into Meta (facebook +
instagram), LinkedIn, other and unknown. **No ratio** joins Meta spend to a
HubSpot count. Doctrine: `docs/55_META_SPEND_ON_CHANNELS.md`. Depends on
PR-ADS-171A (`docs/52`) and PR-ADS-168A (`docs/50`); `docs/50` §5–6 and
`docs/52` (header, §6) cross-reference it.

| Piece | Where |
|---|---|
| The line (pure `line_from_truth`; `build_meta_ads_spend_line`; `paid_social_spend`) | `services/meta_ads_spend_line_service.py` |
| Channels: `outcomes.meta_ads_line`, the Paid Social row's spend/ratios, `spend_sources` | `services/source_attribution_service.py::build_channel_outcomes` |
| Paid Social platform split (`platform_split`, `platform_key`, reconcile) | `services/channel_funnel_service.py` |
| Dashboard: `meta_ads_line`, `truth_status.meta_ads_spend`, Paid Social status | `services/dashboard_channels_service.py` |
| Reasons `spend_shown_on_meta_ads_line`, `channel_population_is_not_meta_spend_population` | `analysis/closed_won_truth.py` |
| Rendering (`metaLineSpendMetric` gate, Meta row, strip cell, platform sub-rows, Dashboard panel, footer chip) | `static/app.js`, `static/styles.css` |
| Checks 19 `meta_spend_disclosure_carried`, 20 `no_rate_over_meta_spend` | `scripts/audit_customer_revenue_reader_cutover.py` |

**Days.** The line passes the page's settled dates (Channels: the evidence
window ended at London's yesterday; Dashboard: the business window ended at
yesterday when open) to the truth with `settled=True`; the truth reads and
settles each ad account on its own calendar. No second date rule. Disclosed
on the line. Consequence today: the Los Angeles account is always verified one
date behind London's yesterday at the 06:00 UTC sync, so every such window is
**withheld** ("account 4730305973961806 is not verified for every day through
… (verified through …)") until the owner moves it to UK time and re-runs its
backfill.

**Drill-down 1** (`hs_analytics_source_data_1`) was already stored in
`hubspot_contact_funnel` and selected by `fetch_channel_funnel_contacts`: no
sync or schema change. Matched whole (no substring); no value is `unknown`,
never Meta.

**Cost per lead** is not shown (171C's `meta_reported_leads` has its own
unverified gate; omitted in this PR).

**Shown failing first.** The new modules fail 38/38 on e07cf45. Mutations
(each run, each caught): per-account figures exposed under a withheld total
(`test_03` ×3, `test_03d`, `test_06c`, `test_09b`); the Paid Social row left at
`channel_spend_not_connected` with Meta set up (`test_06`); a missing
drill-down guessed as Meta (`test_05c`); a cost-per-contact key, a HubSpot
count, a published comparison on the line (`test_06b`); the page gate ignoring
the declaration (`test_07c` counterfactual, `test_10b`); a renderer reading
`.spend` around the gate, the Dashboard panel bypassing it, a "per contact"
label (`test_10b`); an unregistered module reading the truth (`test_10c`); a
reader dropping the disclosure (`test_10d`).

**Existing tests changed** — one module, no assertion loosened:
`tests/test_pr_ads_168a_channel_outcomes.py` gains an autouse fixture stating
the Meta state its tests assume (no Meta variable, nothing stored →
`not_connected`). Without it the Meta store is unreadable in those tests,
which the line correctly reads as "could not tell" and the Paid Social row as
"shown on the Meta line".

**Tests.** `tests/test_pr_ads_171b_meta_on_channels.py` (38);
`tests/test_pr_ads_171b_meta_on_channels_pg_integration.py` (4, registered in
the PostgreSQL step and its did-run list).

**Known gaps** (`docs/55` §7): withheld while the LA account is on LA time;
the Dashboard platform matrix's substring classifier can disagree with the
exact Channels split; no join between Meta campaigns and HubSpot contacts.

**Full suite.** 6407 passed, 1 skipped, 1 deselected (the documented
baseline), 8 failed on the first run: all eight were the SQL doctrine
inventory reading two regex literals in the new audit check as SQL field
references. Reworded (`sq[l]s`); those eight modules, the 158 PostgreSQL
module and 161E re-run green (167 + 82 passed).

**Review round 1** (independent review, no blocker; one new commit). The
Dashboard read the Meta line's `connected` None (could not tell — an
unreadable store, or the first day of a quarter with no settled day) as "not
connected"; it is now three-way (set up / not set up / could not tell) in the
Paid Social statuses, the decision card and the unavailable list, and the
footer dot for `not_connected` differs from `unavailable`. The Channels
strip's Meta cell now says each account is read on its own calendar days.
Check 20 also catches singular and abbreviated labels (`/ lead`, `/ SQL`,
`CPL`, `cost per`). `test_10c` no longer writes a probe module into the
source tree. `CLAUDE.md` (Channels landmine) and `docs/50` name the Paid
Social reasons and the strip's Meta cell. New tests `test_14`–`test_16`
(11 cases); 8 of them fail on 4d1e929 (the strip zone text, both "could not
tell" cases, the footer dot, four singular/abbreviated labels; three labels
the old pattern already caught pass there).

---

## PR-ADS-168B — remove the Revenue by Source page; Channels is the one channel page

**Why.** The owner wants fewer pages; the six-item menu plan merges Revenue by
Source into Channels. PR-ADS-168A already publishes per-channel closed-won
deals, customers and revenue on Channels through the reader contract, so the
page was a duplicate with a second taxonomy. Full write-up:
`docs/56_REVENUE_BY_SOURCE_REMOVED.md`.

**Removed.** The sidebar entry, `#page-revenue-by-source`, its loader,
renderers, platform drawer and CSS; `GET /api/revenue-by-source` and
`GET /api/revenue-performance/source-platform-detail` (deleted → 404, the
PR-ADS-162B/162D convention); `build_source_platform_detail`,
`_source_detail_row`, `_source_contact_row` and
`revenue_repository.fetch_source_contact_details`, which only the drawer used;
the mart `source` view (`?view=source` → 400), its diagnostic, the
`source_spend_truth` response key, and the Revenue by Source row of the mart
page-parity audit. `build_revenue_by_source` stays — the Dashboard Overview,
Revenue, Deals and Channels tabs read it.

**Redirect.** `#/revenue-by-source` → Channels via `RETIRED_PAGE_REDIRECTS`
(the only place the key survives in `static/`).

**Moved.** The admin Source Attribution Backfill panel → Revenue Health
(`#source-backfill-panel`, rendered by `loadRevenueHealth`). Everything else on
the page was redundant with Channels; `docs/56` §4 lists each item with its
Channels equivalent.

**Links.** Every Dashboard `target_page: "revenue-by-source"` ("Open Revenue by
Source") is now `"channels"` ("Open Channels"), and the Revenue tab's source
breakdown links to Channels.

**Registries.** Reader-cutover `INVENTORY` keeps the
`source_attribution_service.py` row (still a reader) with its surface
corrected, and its page pass drops `revenue_decision_mart_source` while still
building `build_revenue_by_source`. The SQL doctrine registry drops the deleted
symbols, route and renderers; the static inventory finds no unclassified
occurrence.

**Tests.** `tests/test_pr_ads_168b_revenue_by_source_removed.py` (14 cases; 11
fail against the parent commit). Tests whose subject was deleted were deleted
(listed in `docs/56` and the PR description); lists that named the page were
updated to the remaining pages.

**Known gaps.** Channels has no per-deal list behind a channel and no
PR-ADS-133 platform split; the Dashboard Revenue tab still titles a panel
"Revenue by Source" (`docs/56` §7).

**Review round 1.** Dashboard links to Channels promised evidence Channels
cannot reproduce (business windows by source group vs evidence windows by
original source): every one now reads "Open Channels (different windows and
grouping)" (`canonical_contract.CHANNELS_LINK_LABEL`), the Overview signal
tiles print that label, and the Revenue tab panel is retitled "Closed-won by
source group · this window". The Revenue Health parity banner names the pages
it compares ("Campaign, Country and Deals pages agree with the mart"). The
uncalled `revenue_repository.fetch_source_deal_details` is deleted (its name
stays banned in the audit lists). `docs/56` §7.2 notes PR-ADS-171B's Paid
Social contact split.

## PR-ADS-174A — the six-item menu

**What.** Navigation only (`docs/57`). The sidebar's 11 entries, four group
labels and the "Advanced tools" fold are replaced by the owner's six items:
Overview (`dashboard`), Channels, Campaigns (campaigns, Campaign Revenue &
Spend, Keyword Evidence, Search Terms), Countries (Countries, Country Revenue &
Spend), Pipeline (Leads, Deals) and System (System, Data Runs, System Status,
Revenue Health, Admin Backfill, GCLID Attribution). An item with more than one
page shows them as a tab strip at the top of the page, styled as the
Dashboard's `.dashboard-tab`. The strip is the only sub-page pattern.

**Unchanged.** Every page id, `PAGES`, `RETIRED_PAGE_REDIRECTS` (an old
`#/revenue-by-source` bookmark still lands on Channels, which is now
highlighted), hash aliases, loaders, refresh wiring, window selectors, page
content and every Dashboard `target_page`. The Dashboard page keeps its
`<h1>Dashboard</h1>`; only the menu label is "Overview".

**Gating.** `showApp()` hides the same four admin-only entries by the same ids
(`nav-system-item`, `nav-health-item`, `nav-revenue-health-item`,
`nav-backfill-item`). They are now System tabs. `navigate()` checks are
unchanged. An item opens the first page the viewer may see, and an item with
none is hidden. A non-admin's System item opens Data Runs and shows Data Runs
and GCLID Attribution, the same two pages a non-admin could open before.

**Code.** `NAV_ITEMS`, `navItemForPage`, `navVisiblePages`, `applyNavGating`
and `renderNavActive` are in `static/app.js`. `ADVANCED_ADMIN_PAGES`,
`setAdvancedNavOpen`, the toggle and the `.nav-advanced` / `.nav-item--admin`
CSS are deleted.

**Tests.** `tests/test_pr_ads_174a_six_item_menu.py` has 17 cases. The menu
table, the lookup and the gating code are run in `node` against a stand-in DOM
built from `index.html`. Eleven existing cases that pinned the old menu shape
were updated; each is listed with its old and new expectation in `docs/57` §7.

**Not verified.** Browser rendering.

## PR-ADS-175A — a row HubSpot returns out of its sort position moves nothing (November 2026)

**Found in production (2026-11-23 06:00 UTC).** `runs` 342 (`partial`),
`sync_batches` 2744 `hubspot/contact_funnel` `failed` in 8 s: "contact scan
ended after 813 unique contacts but HubSpot counted 965 at its start". The
PR-ADS-161I `total` guard was right; the paging was wrong.

**Cause.** HubSpot's search filters and sorts on its search index, then
returns each record's CURRENT properties. Contact 935470478561 was modified at
2026-11-23T05:57:02.287Z (an email event; property history), 2 min 58 s
before the scan. At 06:00:07 the index still held its previous value,
2026-11-22T21:42:49.482Z, so the ninth query (`lastmodifieddate >=
2026-11-22T21:17:12.511Z`) returned it as row 16 of 100 carrying
`05:57:02.287`. `_keyset_scan` moved the watermark to that value on that one
row, treated rows 17–100 as already read (their timestamps were now below the
watermark) and sent `>= 05:57:02.287`: four rows, a short page, the end. The
stored rows of batch 2744 show it page by page: page 9 holds 15 contacts from
21:18–21:28 and then 935470478561; page 10 holds four from 05:58–05:59. 152
contacts modified between 22 Nov 21:42 and 23 Nov 05:57 were never read. Not
a tie group: the 22 Nov bulk update (~600 contacts, 08:06–08:16) is spread
over distinct milliseconds and was read in full. Not `total` counting
something filtered out: the same filter, read now, places the contact at its
05:57 position.

**What happened next.** The contact service returned `failed` (the `runs` row
is `partial`, the scheduler's roll-up of its jobs). The rewind worked:
`last_modified_watermark` went back to the run's `since`
(2026-11-22T05:41:08.617Z), `last_incremental_status = failed`, so cohort
freshness reports `sync_failed` and Campaign Evidence
withholds cohort SQLs/CPQL until a run succeeds. The next run starts 15 min
earlier again (overlap re-applied). It is not deterministic: it fails whenever
a contact modified in the minutes before 06:00 (or during the scan) lands in
the middle of a page before the index catches up.

**Fix.** `connectors/hubspot_pull.py::_keyset_scan` (shared with the deal
ledger):

* every returned row is read unless this scan already read that id at that
  timestamp (`read_at: id -> timestamp`), replacing the set keyed to the
  current watermark that discarded every row below it;
* after a range page the watermark becomes the OLDER of the page's last two
  parseable timestamps (never lower than before) — one out-of-place row
  cannot carry it past the unread rows behind it;
* a tie walk never moves the watermark;
* `meta["watermark_ms"]` is that position. `services/hubspot_contact_funnel_sync_service.py::_page_watermark`
  now checkpoints it instead of the newest `last_modified_at` on the page —
  which, for a killed worker or a capped run, would have resumed past unread
  contacts with a `total` (counted from the later start) unable to see it.

`_assert_scan_complete` is unchanged. **Remaining gap:** if BOTH of a page's
last two rows are out of place, the scan can still skip. The guard refuses
that (`failed`, rewind, retry — `test_05`) only when the shortfall exceeds
its slack and nothing masks it: the contact scan (`tolerant=True`) completes
with up to max(2, 1%) records unread, and any record that enters the result
set mid-scan counts toward the unique total and can hide a skipped one, for
deals too. Excluding rows stamped at or after the scan's start from that
count was considered and not done: a record counted in `total` and modified
before it was read looks identical, so a deal edited mid-scan would turn a
clean read into a false `gap_proven`, and it would compare HubSpot's clock
with ours.
When the read STOPS EARLY (429, timeout, unreadable page, cap, stall) the
guard never runs: two out-of-place rows ending an earlier page can then put
the stored keyset position past unread records, and the next run's own
`total` cannot see them (review round 2: 300 deals, 5098/5099 lagged at the
end of page 1, a 429 from request 3 → run 2 `success` with 51 deals never
written). It needs two adjacent lagged rows and a later stop in the same
read; at c903091 one lagged row and a stop were enough.

**Tests.** `tests/test_pr_ads_175a_contact_scan_index_lag.py` (6). Its fake
keeps the index value (filters, sort, `total`) apart from the returned value,
honours the filter and `total`, reshuffles ties, and modifies contacts during
the scan with a lagging index. On origin/main `test_01` (the production
replay: bulk update, a 250-row one-millisecond group, one contact modified
three minutes before the scan) fails with "contact scan ended after 990
unique contacts but HubSpot counted 1019"; `test_02` (a lagged row ending a
page), `test_03` (a lagged member inside a tie walk), `test_04` (mid-scan
modifications, index two requests behind) and `test_06` (the checkpoint
landed at 05:57:02 past unread contacts) fail too; `test_05` (the residual
fails closed) passes on both.

**Existing test changed** — one assertion, still an equality:
`tests/test_pr_ads_166b_deal_keyset_paging.py::test_6_a` pinned the kept
bootstrap checkpoint to page 1's newest row; it is now page 1's keyset
position (the older of its last two timestamps), which is the rule this PR
changes.

**Review round 1 — blocker: the deal ledger's partial checkpoint.** The fixed
scan completes deal reads that used to end short and be `gap_proven`, and
those reached `sync_deals`' checkpoint, which assumed the read was
ascending: the newest `hs_lastmodifieddate` of the clean prefix. One
out-of-place deal in a capped run's first 50 moved the checkpoint 30 hours
ahead; the next run was `success`/`complete` with 250 deals never written,
and a backfill pass completed the bootstrap over them. Now
`services/hubspot_deal_sync_service.py::_resume_checkpoint` stores the
smallest of the clean prefix's newest timestamp, every returned deal from
the first unclean or unprocessed one onward, and — when the read stopped
early — the scan's keyset position, which `pull_deals_for_ledger` now
returns as `scan_watermark_ms` (unknown position → no checkpoint).
Timestamps are compared as instants. Docstrings that claimed the read was
ascending (`pull_deals_for_ledger`, `record_sync_state`, `sync_deals`) are
corrected. New tests in the same module: `test_07` (cap 50, out-of-place
deal inside it: checkpoint at or before deal 5050, run 2 writes all 300),
`test_08` (out-of-place deal on page 1, a 429 from page 3: checkpoint at or
before the first unread deal), `test_09` (the backfill CLI never completes
the bootstrap with a deal unwritten). On bf48b04 all three fail:
"checkpoint 2026-11-23T04:26:40.000Z is past deal 5050", "… past unread
deals", "the bootstrap was completed with 250 deals never written".
`test_05` now drives the service and asserts the rewind. One more existing
assertion moves, still an equality:
`tests/test_pr_ads_166b_sdk_exception_classes.py::test_3_c_a` (a 429 after
one full page) pinned the checkpoint to that page's newest deal
(`00:01:39Z`); it is now the read's keyset position after the page, the
older of its last two timestamps (`00:01:38Z`, deal 1098) — the bound this
round adds for a read that stopped early. The audit label
`latest modification held` (`scripts/audit_crm_funnel_truth.py`) reads
`MAX(last_modified_at)` from the contact table, not the sync state, so it is
unchanged.

## PR-ADS-171D — the ad-set backfill works on a live account (November 2026)

**Why.** The owner's first production ad-set backfill (2026-11-23, Render
shell, account 944370805110952, USD, Asia/Amman) verified Jan 2025 – Oct 2026
and then failed twice on November:

1. `--level adset --from 2025-01-01` at 04:2x UTC:
   `meta_adset_campaign_base_unverified`. The backfill ended at the account's
   local yesterday (22 Nov); the campaign level was verified only through
   21 Nov because the 06:00 UTC daily sync had not run. The whole month was
   discarded.
2. Rerun with `--to 2026-11-21`: `meta_adset_reconciliation_mismatch` —
   21 Nov, campaign 6951621703494, fresh ad-set rows 61,320,000 micros against
   the STORED campaign row 61,300,000 micros (read by the previous morning's
   sync). Meta had restated the day by $0.02 between the two reads. The
   backfill compared a live ad-set read with an older stored campaign read, so
   any restatement inside Meta's adjustment window made a recent range
   unloadable.

**What changed.**

* **A — live-to-live.** `services/meta_ads_spend_service.py::_backfill_adset_chunk`:
  each ad-set month first re-reads the campaign level of the same account and
  days through the existing `sync_chunk` (account-total reconciliation, the
  atomic `replace_meta_campaign_spend_chunk`, its `MetaRestatementImplausible`
  refusal — never with `--accept-zero-restatement`), then calls the unchanged
  `meta_ads_adset_service.sync_adset_chunk`, which reconciles the ad sets to
  the rows just stored. Tolerance unchanged (one minor unit). A re-read that
  is not verified fails the ad-set month with the re-read's reason, requests
  no ad set, stores no ad-set row, records a `failed` ad-set ledger row. The
  re-read lives in the backfill, not in the ad-set service: that module and
  the daily ad-set step still cannot write a campaign table (171C `test_28`).
  Because the ad-set level now writes campaign rows, it handles the account
  like the campaign backfill: stored through `refresh_account` (the identity
  guard), and a failed account read recorded in `meta_ads_account_sync_state`.
* **B — report the cut.** The end was already capped at the account's local
  yesterday (the last day the campaign level can be verified); the first
  production failure was the stale STORED campaign level, fixed by A. What B
  adds: at `--level adset` an end later than that day is reported per
  account where it is cut: `requested_to`,
  `campaign_verifiable_through`, `stopped_at`, `stop_reason`
  (`meta_adset_end_after_last_verifiable_day`), `stop_detail`. Days before it
  are stored; no later day is read. Without `--level` nothing changed.
* **C — lead type and attribution.** `config/thresholds.yaml`
  `meta_ads.native_lead_action_type: "onsite_conversion.lead_grouped"`, on the
  evidence recorded in `docs/53` §4 (the owner's statement that the ads use
  native lead forms only, and five lead types with identical counts on every
  row and month; Meta's definition of the type was not retrieved). The 171C
  rules hold: published only for an account whose
  stored rows carry that exact key; never summed. The read function now
  returns `meta_reported_leads_attribution` (`{settings, rows_without_setting,
  status, detail}`, from the stored `attribution_setting` column; `not_stored`
  says so, nothing is inferred; `None` when withheld) and each row's
  `attribution_setting_missing_rows`. Production observation recorded in
  `docs/53`: `1d_view_7d_click` (2026-11-23).
* **D — owner steps.** `docs/53` §10: no `--to`, `--resume` with the same
  `--from`; a recent-day mismatch can no longer come from the script's timing,
  and §10 lists exactly when a month can still fail (a restatement in the
  seconds between the two reads; VERIFY 3/4; a failed campaign re-read).

**The daily step was not affected, and is unchanged**: it reads the campaign
level, then the ad sets of the same window, in one run
(`test_08` asserts the call order and that a 2-cent restatement reconciles).

**Shown failing first.** On c903091 (origin/main) the new modules fail 12 of
13: the two production scenarios reproduce the production errors verbatim —
`meta_adset_campaign_base_unverified` for 2026-11-01..2026-11-22 (`test_01`,
`test_02`) and `meta_adset_reconciliation_mismatch` "first 2026-11-21 …
ad-set rows 61320000 micros, campaign row 61300000 micros" (`test_03`).
`test_08` (the daily step) passes there by design: it verifies the step was
never affected. Mutations, each caught: the campaign re-read given the
override (`test_05`); the ad-set read made after a failed re-read (`test_04`).

**Existing tests changed** (contracts this PR deliberately reverses, nothing
loosened): 171C `test_33` comment (the ad-set backfill now stores the account);
171C `test_34` (a month without a stored campaign base now loads; the month
stops where its campaign re-read fails, with that reason, nothing stored);
171C PG `test_12` (the ad-set backfill writes campaign tables only as a
campaign backfill of the same months would — asserted equal — and still no
batch, sync_state or run); 171C `test_08` and PG `test_02` state the
unconfigured lead type explicitly instead of relying on the shipped config.

**Tests.** `tests/test_pr_ads_171d_meta_adset_backfill_pg_integration.py` (9,
registered in the PostgreSQL step and its did-run list);
`tests/test_pr_ads_171d_meta_adset_backfill.py` (4).

**Full suite** (with the one documented baseline deselect): 6521 passed,
1 skipped, 1 deselected, 0 failed. The 171A/171B/171C/171D modules: 411 passed.

**Review round 1** (independent review: ship, no blocker; one commit on top).

* m1 — the FX-partial step of `_backfill_adset_chunk` is now tested end to end
  (`test_09`: GBP account, rates unavailable → chunk `partial`
  `fx_rates_missing`, run `partial`, script exit 3, both ledgers `verified`).
  Replacing the step with `if False:` fails it.
* m2 — `docs/53` §6: the ad-set backfill advances the System page's "Meta Ads
  spend" line (it reads the campaign coverage ledger), as the campaign
  backfill already did; `sync_batches` / `sync_state` / `runs` do not move.
* m3 — tripwire: for an account whose stored rows carry the configured type,
  any stored row on which it differs from `lead` (or appears without it) makes
  `meta_reported_leads` unavailable for that account
  (`meta_lead_action_types_diverged`), adds a `config_problems` entry naming
  the account, rows and first day, and sets
  `accounts[].meta_reported_lead_types_diverged`
  (`db/meta_ads_repository.py::fetch_meta_adset_spend`, same snapshot;
  `services/meta_ads_adset_truth_service.py`). `test_10` (lead=5,
  onsite_conversion.lead_grouped=3) fails on 8c90950. `docs/53` §4 and the
  config comment now say the choice rests on the owner's statement and the
  five identical counts only; Meta's definition was not retrieved.
* m4 — stale text: the `--level` help; the §10 smoke example shows
  `1d_view_7d_click`; "stop, don't fail" corrected everywhere — the end was
  already capped at the account's local yesterday, 171D adds the reporting,
  and the production failure was fixed by the campaign re-read.
* m5 — `test_11`: a failed account read during an ad-set backfill is recorded
  in `meta_ads_account_sync_state` exactly as the campaign backfill records
  it (fails against c903091's account handling).

Round 1 full suite (one baseline deselect): 6524 passed, 1 skipped,
1 deselected, 0 failed.

## PR-ADS-176A — one rule per figure in the shared country builder

**What.** Backend only (`docs/58`). Navigation and page layout are unchanged.
The country builders behind Countries, Country Revenue & Spend and Dashboard →
Countries now state only what the evidence supports, and each figure follows
one rule.

* **Zero rule.** `services/country_spend_zero_rule.py` holds the PR-ADS-167A
  rule. It moved from `country_evidence_service` unchanged, and Countries, the
  mart country rows (`_build_db_rows`) and the Dashboard all call it. A
  spend-less market whose `$0` the rule cannot prove reads `None` with
  `spend_location_unresolved` on every surface. Before, the mart and the
  Dashboard printed `$0`. The Dashboard's listed-markets spend total is
  withheld with that reason.
* **Code-less criteria.** A criterion that names no country is bucketed into
  the residual's `country_unidentified` component instead of being dropped. Σ
  rows + residual = campaign total.
* **Outside-market countries** are itemised as residual sub-items
  (`outside_listed_market_items`) on the mart and the Dashboard. The items are
  a breakdown, never additional. Their outcomes are `"in_residual"`.
* **Drawers** list their row's own population. Country Revenue & Spend's
  drawer uses settled-day bounds and the "not Google Ads" exclusions. The
  Dashboard drawer lists the mart row's `deal_ids` at `google_ads_source`.
* **Account scope.** The Dashboard's and the mart's per-country geo reads are
  scoped to the configured customer (PR-ADS-154B).
* **Statuses.** "Customer proven" is now "Won deal, revenue not proven".
  "Spend without customer proof" is now "Spend without a won deal". The
  `app.js` status maps are re-keyed to match.
* **Blocked card.** `spend_truth.country_gap_message` is the cause. The next
  step comes from `country_gap_codes`. The settle gap no longer says "run the
  Geo Sync".

**Truth audit round 1** (`docs/58` §2, §6):

* The Dashboard no longer blames FX when the zero rule withholds spend.
* The residual row publishes no period ratio (`residual_not_a_market`).
* Regional Mix and `countries_with_spend` state no zero over unknowns.
* The blocked card's cause, next step and run buttons follow one primary gap.
* No deal denominators are published while the gate withholds.
* The spend metric names `spend_location_unresolved`.
* The drawer's exclusions are read for the row's account.
* `deal_ids` is None beside a withheld count, and the audit checks it.
* Withheld spend renders "Unavailable" with its reason.

**Truth audit round 2** (`docs/58` §2, §6):

* Unreadable canonical campaign spend now reads as `campaign_spend_unreadable`.
  The gate omits "not like for like" when there is no baseline to compare.
* Run buttons are the union over all gap codes, and the time-zone gap offers
  the Spend Backfill.
* A gap with no primary is reviewed, never "run the Geo Sync".
* The zero-rule copy says "$0 is not proven".
* A withheld population carries `deals: None`, and the audit flags `deals: []`.

**Owner decisions 2026-11-23** for 176B: business windows with Current Quarter
as the default, a strict spend gate, and no lead-quality columns. These are
recorded in `docs/58` §3.

**Tests.** `tests/test_pr_ads_176a_country_builder_pg_integration.py` has 24
cases and is registered in CI. Nine of the first eleven fail on origin/main;
case 02 is the counterfactual. Case 10 checks that the geo snapshot fails
closed. Every truth-audit case fails on `98e0b5e`, except the guards 15 and
16, which fail under mutation. Existing tests changed:

* ten geo-reader fakes now accept the account-scope argument;
* the structural snapshot test in 167a `test_11` follows the carriers read
  into the shared rule;
* a `tests/conftest.py` autouse fixture restores `db.connection._pool` after
  every test, so PG fixtures no longer leak a pool over a stopped cluster;
* 124 / 127 now assert the gap-driven CTA call
  `roasCountryCtaButtons(guide.run)`;
* two 138 cases now assert `deals is None` beside a withheld count
  (round 2).

Details are in `docs/58` §6.

**Next.** 176B merges the two pages. 176C retires `/api/geo`.

## PR-ADS-176B — one Countries page (November 2026)

**What.** The owner decisions of 2026-11-23 (`docs/58` §3, §7). Countries is
one page, `roas-countries`, reading the mart's country view on business
windows, in the Dashboard's visual language.

* **Navigation.** `NAV_ITEMS.countries = ["roas-countries"]`; the Countries tab
  strip is deleted (a single-page item has none). `#/geo` is a retired key
  (`RETIRED_PAGE_REDIRECTS.geo → roas-countries`, the PR-ADS-168B pattern), and
  `#/countries` aliases to `roas-countries`.
* **Deleted.** The Countries evidence page: its section, KPI strip, notices,
  Plotly map, search box, table, lead-quality columns (decision 3), loader and
  renderers, CSS, and its registry keys. The UI no longer reads `/api/geo`.
* **The page.** Dashboard command bar with the five business windows (Current
  Quarter default); the 176A gap-driven blocked card for every non-ready gate
  status, FX-only included (decision 2: never a number with a warning); KPI
  cards over the mart's `country_totals`; the Dashboard country table with the
  residual pinned and its outside-market countries as "In residual" sub-rows;
  the Dashboard drawer (row click / Enter / Space) with the row's deal list;
  Regional Mix and the residual panel; a truth footer. The campaign-scope spend
  card, summary strip and diagnostics are no longer on it.
* **Mart.** The country view gains row `region` (`analysis/country_regions.py`,
  the Dashboard's table moved unchanged), `country_totals` and `regional_mix`
  (`_country_page_blocks`): listed markets only, an unknown row makes its total
  unknown with its reason, no total under an unready gate or an empty window,
  the window's withheld deal / revenue verdict wins, no customer total.
* **Scope declaration.** The mart's country `country_truth.revenue_scope` and the
  Dashboard's `revenue_scope` / `country_truth.revenue_scope` say
  `google_ads_source` — what the rows count (`docs/58` §5).
* **Wording.** Leads / SQLs cells read "Unavailable", not "—"; sub-dollar spend
  reads "<$1"; country reason codes get words (`countryReasonLabel`). Links that
  named "Country Revenue & Spend" say "Open Countries".

**Tests.** `tests/test_pr_ads_176b_countries_one_page.py` (13) and
`tests/test_pr_ads_176b_countries_pg_integration.py` (5, registered in CI). All
18 fail on `2eda9ac`; case 06's gate half is proved by mutation. 22 existing
test files changed, each because a page, key or layout it pinned was deleted or
merged; the table, with reasons, is in `docs/58` §7.9. The `#/geo` PG suites
(165A, 167A, 170A) keep every `/api/geo` payload assertion; their page
assertions are restated on the rows, and the page rules that still apply are
guarded on the one Countries page — except the "$0 rests on an owner label"
disclosure, which this PR dropped and F1 restores (below).

**Next.** 176C retires `/api/geo` and `services/country_evidence_service.py`.

### PR-ADS-176B-F1 — truth audit round 1

* **M1** A row whose spend (zero rule) or revenue (won deals, no proven amount)
  is unknown is not judged: verdict `spend_unknown` / `revenue_unknown` with
  `verdict_reason`, badge "Not judged — …", outside the four decision filters.
  It read "Watch" / "Learning" on `spend or 0`.
* **M2** `zero_rests_on_owner_labels` on mart country rows (the shared zero
  rule), and "$0 owner label" on the Countries page — lost with `#/geo`.
* **M3** The `country_totals` gate is tested: an FX-only gap publishes no total
  (native GBP on rows was publishable as USD under the mutation).
* **Minor** reconciliation footer label and the blocking market's name (m4);
  cents in the residual (m5); "listed markets" on Won Revenue (m6); the
  response's window for links and the drawer, and a cleared deal-list cache on
  reload (m7); `lead_scope` declared and labelled (m8); `None` mix and market
  count with no settled day (m9).

Tests: 11 new cases (one-page 14–20, PG 06–07, 170A 10b / 12b), all failing on
`4d0921e` (case 15 under the named mutation). Details in `docs/58` §7.10.

### PR-ADS-176B-F2 — truth audit round 2 (merge, four small items)

* The `carried_by_google` exclusion and the in-flight deal-list drop are now
  guarded (176B PG case 08, one-page case 21), each failing under its mutation.
* Dashboard → Countries: a `$0` resting on owner labels carries
  `zero_rests_on_owner_labels` (the shared zero rule) and says "owner label"
  (176B PG case 09).
* M1 reaches Campaign Revenue & Spend too: won deals with withheld revenue read
  "Not judged — revenue unknown" (`docs/58` §7.10, `161e` case 05). The unused
  `NOT_JUDGED_VERDICTS` set is deleted.

---

## PR-ADS-176C — the legacy country feed is retired

**Why.** After PR-ADS-176B nothing in `static/` read `GET /api/geo`
(`services/country_evidence_service`) or `GET /api/leads/country-summary`, but
both were live, and the service kept its own copy of the owner-label zero
disclosure. A second builder of a country figure is a second rule waiting to
drift. They are deleted. Their truth properties were first re-asserted on the
surfaces that are left. Full record: `docs/58` §8.

**Port before delete.** Every property the 165A / 167A / 170A PostgreSQL suites
asserted on the `/api/geo` payload now runs, on real PostgreSQL, against the
mart's country view (the one Countries page) and Dashboard → Countries. The
properties are:

* exact sums, with restated days counted once;
* uncovered windows, the settle rule, failed re-reads, the account time zone;
* the zero rule and its carriers;
* owner labels and mismatches;
* FX gaps, the legacy snapshot and all_time;
* the lead join, unmatched text and unreadable leads;
* the 162-code production shape.

The ports are a new module,
`tests/test_pr_ads_176c_country_feed_retired_pg_integration.py` (26 cases),
plus in-place rewrites of 167A zero-rule 01–06 and 170A 10–14 / 24. 165A
case 12 (merged top campaign / keyword) is not ported: lead-quality columns
were dropped by owner decision 3. The port table is `docs/58` §8.3.

**Deleted.**

* Both routes, now 404.
* `country_evidence_service.py`.
* `geo_owner_labels.OWNER_LABEL_NOTE`.
* `PAGE_PIPELINE_IMPACT["geo"]`.
* The doctrine-registry consumer and rules for the Geo country summary.
  `ui.index` is rebound to the Countries consumer.
* The two `API_CONTRACT.md` sections.

`country_spend_zero_rule` is now the only implementation of the zero verdict
and the owner-label disclosure.

**Parity.** `cross_page_parity_service` never compared `/api/geo`. It listed
`/api/leads/country-summary` as a service of the uncertified
`lead_intelligence` page. That list is now `["/api/leads"]`. No identity or
check changed.

**Findings, fixed in PR-ADS-176C-F1.** The port found four properties
`/api/geo` held that the live surfaces did not. The first commit pinned them
"as observed"; the follow-up commit fixes each in the code and asserts the
correct behaviour. Each case fails on `2b056a4` and on `5a9b5e9`. Full record:
`docs/58` §8.4 / §8.7.

* **F1.** A listed market's lead text in an ISO or legacy spelling
  ("libyan arab jamahiriya", "Iran, Islamic Republic of", "Türkiye",
  "Jordan.") now joins its market. `country_identity.resolve` falls back to
  the exact complete-ISO lookup, for listed markets only. There is no fuzzy
  matching, and conservation holds.
* **F2.** Dashboard → Countries keeps native spend only for an FX-only gap.
  This is one rule, `geo_gate.native_country_spend_publishable`, shared with
  the mart. Two accounts summed, `totals_differ` and unreadable campaign spend
  now withhold.
* **F3.** Open-start (all_time) spend reads count each account's rows only from
  its earliest verified coverage chunk (`revenue_repository._certified_from`).
  The six reads are campaign spend, the daily series, the geo total, geo by
  country, the reconciliation breakdown and FX coverage. They reach the mart
  and every page on it, Dashboard Countries / Campaigns / Overview, Channels,
  Campaign Evidence, the mapping review, the spend audits and the geo panel.
  The deal resolver's all-time campaign-name read is exempt as identity
  evidence.
* **F5.** The mart no longer builds country rows, `top_campaign` or
  `country_geo_total` from the legacy `geo` snapshot. With no canonical geo
  rows, the geo total is unknown and the source is `unavailable`.

### PR-ADS-176C-F2 — truth audit round 1 (merge, no blocker; fixed anyway)

* **M1.** With canonical campaign spend unreadable, the mart campaign view no
  longer builds rows from the legacy `geo` snapshot (`campaign_spend_rows = []`).
  `fetch_campaign_country_spend` remains only as `_build_from_db`'s
  reachability probe and presence flags.
* **m1.** The FX bound is guarded (176C 25).
* **m2.** The deal resolver's all-time campaign names are bounded like the
  leads side's: one identity population (176C 27).
* **m3.** The Overview spend trend is scoped to the configured account
  (176C 26). This round also made an account with no verified chunk
  contribute nothing to open-start reads (`'infinity'`). That was a
  regression; F3 reverted it (below).
* **m4.** No Google Ads account configured: the mart country view and
  Dashboard → Countries withhold country spend with
  `google_ads_customer_id_not_configured`, as the PR-ADS-165A Countries
  contract says. Other views keep the PR-ADS-154B default; this is noted as a
  follow-up.
* **m5.** CLAUDE.md now names the live ledger judge; two production spellings
  are pinned; `REASON_ISO_NAME_MATCH` is exported.

Details and mutation evidence: `docs/58` §8.9.

### PR-ADS-176C-F3 — truth audit round 2 ("fix first")

* **R2-1 (regression from F2).** The `'infinity'` bound turned a no-ledger
  account's spend into 0.0 with `available: True`. The Overview claimed "No
  unexplained paid spend", and the spend audit's reconciliations read
  verified / reconciled. The fixes:
  * the bound is restored to 56adcfc's (unbounded without a chunk);
  * configured-account figures stay clean by scope: the daily series, and now
    both spend audits;
  * the Overview card requires verified spend (that gap predated 176C).

  Regression test 176C 28 (the no-ledger probe), plus 26 (scoped) and 29
  (card gate). The 173A matrix runs with and without a verified ledger and is
  byte-identical to its golden both ways.
* **R2-2.** `/api/revenue-attribution` no longer reads the legacy snapshot for
  `source_health.coverage_start` / `coverage_end` or `campaign_spend_status`
  (176C 30).
* **R2-3.** `docs/58` §8.9 corrected: 71 payloads, not 38; §8.10 added.

**Guard.** `tests/test_pr_ads_176c_country_feed_retired.py` (6 cases) covers:

* the routes are 404 and absent from the app;
* no handler is defined for them;
* the service is absent and imported nowhere, by any spelling;
* the UI reads neither route;
* the zero rule has one implementation;
* no registry or audit names a deleted reader.

Cases 01–03, 05 and 06 fail on `5a9b5e9`.

**Next.** None from this slice.

---

## PR-ADS-177A — One SQL count: the acquisition cohort everywhere

**Owner decision 2026-11-24.** Every page shows the same SQLs — the stricter
acquisition cohort Campaign Evidence already publishes — accepting that they
are withheld when the contact sync is incomplete. This PR switches the
Revenue Decision Mart and every page that republishes it; PR-ADS-177B builds
the one Campaigns page, PR-ADS-177C retires the old ones. Doc: `docs/59`.

* **The mart** (`revenue_attribution_service._build_from_db`) no longer reads
  `fetch_lead_quality` (`leads.status_category = 'qualified'`). It builds the
  window's acquisition cohort once (`_read_cohort` →
  `marketing_outcome_cohort_service.build_window_outcomes`, freshness from the
  real `assess`) and places each Google Ads contact by the mart's OWN campaign
  identity rule (`_mart_label_resolver`, the key its revenue rows use) and by
  the contact's own HubSpot country (`contact_country` →
  `country_identity.country_key`; `build_cohort(group_key=…)` partitions and
  `reconcile_cohort` re-checks it). Every row and total's `leads` / `sqls`,
  `metrics.leads` / `metrics.sqls`, `sqls_status` / `sqls_reason`, and a
  `cohort` block (verdict, `metric_family`, `window_basis`, metadata, totals,
  `campaign_residual`) on every view and every Dashboard payload.
* **Conservation.** Campaign rows + `cohort.campaign_residual` = the total;
  country rows + the residual row = the total; Dashboard Countries
  `kpis.sqls` + `residual.sqls` = the total (the residual is the mart
  residual row's, no longer a subtraction from a legacy campaign total).
* **Withheld means absent.** A non-published verdict leaves no lead or SQL
  count anywhere in the mart or Dashboard payloads; leads are withheld with the
  SQLs. A withheld cohort blocks no page (`revenue_decision_ready` no longer
  waits on the leads).
* **Verdicts.** `sqls_unknown` (mart rows) when the verdict depends on a
  withheld count; "SQLs withheld" on Dashboard Campaigns / Countries / Deals
  and the regional mix. `sqls_for_verdict = 0` is gone.
* **Labels.** "SQLs · contacts acquired in window" / "Leads · contacts
  acquired in window"; every mart / Dashboard SQL cell goes through
  `cohortCountHtml(value, cohort, row)` with the response's own cohort.
  Dashboard Deals' legacy backlog and source breakdown are relabelled
  "Qualified (lead status)". Dashboard Campaigns' `revenue_scope` now says
  `campaign_attributable` (it said `all_source`).
* **Audits.** `audit_marketing_outcome_cohorts` scans the mart's two views and
  the five Dashboard payloads for every business window
  (`mart_withheld_exposures`, `mart_conservation_problems`).
  `cross_page_parity_service`: identities `google_ads_cohort_sqls` /
  `google_ads_cohort_leads`, the conservation rule over the cohort, and
  `cohort_sql_parity` (Countries page = Dashboard Countries = Campaign Evidence
  over the same settled days; Campaign Evidence gains `bounds=`). SQL-doctrine
  inventory: 10 canonical / 11 legacy / 9 mixed, 0 unknown.
* **Tests.** `tests/test_pr_ads_177a_one_sql_count.py` (41) and
  `tests/test_pr_ads_177a_one_sql_count_pg_integration.py` (13, PG step and
  did-run list). Mock-based mart tests feed the same contacts through the
  canonical funnel (`tests/cohort_fixture.py`); each changed test is listed
  in `docs/59` §9.2.

**Next.** PR-ADS-177B (the one Campaigns page). Remaining legacy SQL readers,
all labelled and none mart-derived, are listed in `docs/59` §8.

## PR-ADS-177A-F1 — truth audit round 1 on the one SQL count

Fixes on top of PR-ADS-177A; `docs/59` §10 has each finding, fix and the
fail-before / mutation evidence.

* **Stale cohort (MAJOR 1).** The mart's `cohort` block declares `stale`,
  `as_of` and `window`. Every published cohort count prints "measured as of …"
  over a `source_stale` funnel, via Campaign Evidence's `campaignCohortAsOf`
  inside the one gate `cohortCountHtml`. `audit_marketing_outcome_cohorts`
  fails a stale published payload without them (`stale_disclosed`).
* **Two "SQLs" (MAJOR 2).** Dashboard Deals: the legacy backlog is out of the
  funnel (`lead_status_backlog`, `kpis.lead_status_qualified_no_won_deal`,
  was `sqls_not_closed_won`); its cards, statuses and headers say
  "Qualified (lead status)"; the funnel's SQL stage under a withheld cohort
  says "Withheld" and the reason. Dashboard Channels' lead-status counts are
  labelled "Qualified (lead status)" everywhere
  (`kpis.total_sqls_metric_family`).
* **Windows (MINOR 1).** Each cohort label says its last day; §5's "same
  total" corrected (campaign / deal views through today, country views
  through yesterday); `cohort_sql_parity.campaign_view` compares the campaign
  view's days with Campaign Evidence like for like.
* **Delta (MINOR 2).** No SQL delta on an open window (`cohortDeltaChip`).
* **Names (MINOR 4).** Overview `kpis.google_ads_cohort_sqls` /
  `campaign_row_cohort_sqls` (were the names of two other scopes).
* **Audit endpoint (MINOR 5).** `build_revenue_attribution_audit` judges the
  lead window on the cohort verdict; the legacy table's grain is reported only.
* **Flake (MINOR 6).** The 154c / 154c_f3 CLI cases run at a fixed instant;
  both pass at 2026-10-01 08:00Z.
* **Tests.** `tests/test_pr_ads_177a_f1_truth_audit.py` (14) and four PG cases
  (`test_08`–`test_10`) in the 177A PG module. `cohort_sql_parity` still runs
  only from the manual CLI — a follow-up.

## PR-ADS-177A-F2 — truth audit round 2 on the one SQL count

`docs/59` §11 has the findings, the fixes, the evidence and the measurements.

* **Revenue Health (MAJOR).** F1's MINOR 5 fix made a withheld cohort an UNSAFE
  revenue verdict ("Revenue is not ready", "Run Lead Event-Date
  Reconciliation"). The revenue audit now reports the cohort in its own
  `lead_cohort` field (never a blocker); Revenue Health shows a separate
  "Leads / SQLs (acquisition cohort)" chip with the reason and the
  contact-funnel sync as its remedy; its window comparison's leads go through
  the gate.
* **Gate / labels (MINOR 1).** The Revenue tab rate card, three Dashboard
  sub-lines and the Revenue Health comparison print cohort counts through
  `cohortCountHtml`; the Channels Momentum toggle says "Qualified (lead
  status)". A source guard fails any bare-formatted cohort count.
* **Parity.** The campaign-view violation's wiring is tested (MINOR 2). A
  window's first day — `no_settled_day_in_window` from every settled-day
  consumer — is `not_evaluable_no_settled_day`, not six violations (MINOR 3);
  Channels reads its ledger at the page's `now`, not the wall clock.
* **Certification cost.** The extended cohort audit: 11.5 s / 61 MB resident
  (was 0.5 s / 46 MB) on a 21,750-contact fixture — within the memory budget;
  the shared-core memoisation that halves it is deferred (single-snapshot
  reader contract).
* **Tests.** `tests/test_pr_ads_177a_f2_truth_audit.py` (12).

## PR-ADS-177A-F3 — truth audit round 3 (verdict: merge)

`docs/59` §12.

* **Revenue audit.** Pseudo / email rows in the legacy `leads` table are
  reported (`legacy_table_findings`), never a revenue blocker — no figure reads
  that table. The audit's verdict has no legacy-table input left.
* **Parity.** `not_evaluable_no_settled_day` requires the audit's own settled
  range to be empty, not only the consumers' declaration
  (`_no_settled_day_exempt`); a false declaration on a settled day is a
  violation.
* **UI.** The mapping workbench labels its legacy counts; the Revenue Health
  cohort chip states the selected window's last day (every window) and its
  as-of watermark when stale.
* **Tests.** `tests/test_pr_ads_177a_f3_truth_audit.py` (12).

---

## PR-ADS-178A — CI runs each test once, on pull requests only

Owner decision, 25 Nov 2026: cut CI time; this is a small platform.

* **Before.** A PR run took 63–75 min. The PostgreSQL step (~26 min) and the
  contract step (~3 min) ran their modules, and then the full suite (~33 min)
  ran those same modules a second time. Every merge then started another full
  run on the push to `main`, re-proving a tree its PR had already run green.
* **After.**
  * The contract step writes `contract-results.xml`.
  * The full-suite step reads both junit files and passes `--ignore` for each
    module that contributed cases there. A module that contributed nothing
    (skipped or failed to collect) still runs in the full suite.
  * The PostgreSQL did-run assertion is unchanged.
  * The workflow triggers on `pull_request` only, with `concurrency` cancelling
    a superseded run on the same PR.
* **Coverage.** Unchanged: every test still runs exactly once per job. Locally,
  collection drops from 6690 to 6539 when two modules are ignored, which proves
  the ignore list removes exactly those modules. The ten tests that pin the
  workflow's structure (`-k "ci or workflow or deselect or gate or
  registered"`, 92 cases) pass.
* **Expected time.** About 40 min per PR instead of 63–75, and no second run
  after each merge.
* **Risk.** `main` is no longer re-tested after a merge. That is safe only while
  PRs merge one at a time onto the base they were tested on, which is the
  practice here (squash with `expectedHeadSha`). If two PRs ever merge back to
  back, re-run CI on the second.

## PR-ADS-177B — one Campaigns page, Meta ad sets beside it

`docs/60`. Depends on PR-ADS-177A (`b1cc9e9`). Unblocks 177C (retire Campaign
Evidence's endpoint and renderers) and 177D (Keywords & Search Terms).

* **One page.** `campaigns` ("Campaigns") in the Dashboard design: business
  windows ending yesterday, KPI cards from the mart's `campaign_totals`, the
  campaign table (Spend, Leads, SQLs, Won Deals, Customers, Won Revenue, Period
  Revenue / Ad Spend, Status), a "No campaign label" residual row (cohort leads /
  SQLs only; other cells "Not campaign-attributable"), a drawer that fails
  closed unless its deal ids equal the row's `deal_ids`, and a truth footer.
  Strict spend gate as Countries. `#/roas-campaigns` redirects to `campaigns`;
  the menu item is `[campaigns, keywords, search-terms]`; "Open Campaign Revenue
  & Spend" links are now "Open Campaigns".
* **Settled campaign view (option B).** `build_revenue_decision_mart(view=
  "campaign", settled=True)` / `?settled=true`: settled window, the spend settle
  rule (`canonical_campaign_spend_block(settled=True)`), ledger and cohort over
  the same days, rows with `deal_ids`. Read only by the Campaigns page and
  Dashboard → Campaigns; Overview, Revenue, Deals, Channels and Closed-Won Deals
  keep through-today. A deal closed today is on Overview / Deals, not on
  Campaigns, whose range chip says why. First day of a window: the 167A card and
  `no_settled_day_in_window` on every contract.
* **Dashboard → Campaigns** reads the settled view. Deleted: `_period_ratio`
  (rows carry the reader-contract ratio; a zero revenue is a published `0.00x`),
  `_group_deals_by_campaign` (the drawer's proof is the row's `deal_ids`), the
  all-source "Unattributed / Needs Review" row, and the keyword-theme panel (it
  read the non-windowed `keywords` snapshot, which now has no in-product reader).
* **Meta ad sets.** `GET /api/meta-ads/adsets` →
  `services/meta_ads_adset_table_service.py` over the 171C truth service: ad-set
  rows by id, own totals (never summed with Google), HubSpot outcomes
  "Not linked yet", per-account time zones and the settle rule, withheld (rows
  empty, totals null) with the reason. Registered in the reader-cutover audit
  (`META_ADSET_PAGES`, JS gate `metaAdsetTableGate`) and the SQL-doctrine
  inventory.
* **Parity / audits.** The Campaigns page and Dashboard → Campaigns join the
  settled `cohort_sql_parity` group; five `*__settled_days` identities compare
  Dashboard → Campaigns with `revenue_decision_mart/campaign_settled`. The
  cohort audit scans `campaign_totals` / `campaign_residual_row` and checks the
  residual's conservation.
* **Tests.** `tests/test_pr_ads_177b_campaigns_one_page.py` (14),
  `tests/test_pr_ads_177b_settled_campaign_view.py` (6),
  `tests/test_pr_ads_177b_campaigns_pg_integration.py` (5, PG),
  `tests/test_pr_ads_177b_meta_adsets_pg_integration.py` (7, PG) — both PG
  modules are in the CI PostgreSQL step and its did-run list. Existing-test
  changes, each with its reason: `docs/60` §7.2.
* **Not done.** CLAUDE.md's "dashboard rows return None, not 0, for zero
  revenue" sentence no longer holds for Dashboard → Campaigns (`docs/60` §8);
  CLAUDE.md is not edited here.

## PR-ADS-178B — one Pipeline page: leads and deals together

`docs/61`. Depends on PR-ADS-177B (`d543dcc`). Unblocks 178C (retire the old
routes after porting what parity and the audits read from them).

* **One page.** `pipeline` ("Pipeline") in the Dashboard design replaces the
  Leads page and the Closed-Won Deals page: command bar (business windows
  through today; Scope / Source as LEAD filters), five KPI cards (SQLs = the
  acquisition cohort through `cohortCountHtml` on the response's cohort;
  closed-won deals, customers, won revenue = the mart deal view's summary
  metrics; average deal value = its `ledger_summary`), the lifecycle funnel
  "Entered stage in window" (stage buttons open the leads list; "Moved to
  Customer stage" keeps its help text; the working-status breakdown stays as
  its own button), the leads list (`/api/crm-funnel/contacts`, names, no
  emails, the existing drawer), the closed-won deal list (Source column + a
  filter on the deal's own source group; shown only when its length equals the
  published count), customers won (a list only when it numbers the published
  count), one truth-footer line.
* **One builder.** `services/pipeline_service.build_pipeline`
  (`@single_snapshot`) at `GET /api/pipeline?window=&scope=&acquisition_group=`.
  A lead filter blanks no deal figure; under one, customers won is unavailable
  with `customers_won_not_split_by_contact_source`.
* **Dashboard → Deals** republishes `build_pipeline` (+ a previous-period
  comparison from the same builder) and renders the Pipeline renderers.
  Deleted: its second composition (three closed-won counts, `build_revenue_by_source`
  source groups with a lead-status column, the through-today campaign
  breakdown), the legacy "Qualified (lead status)" backlog and
  `db.revenue_repository.fetch_sql_lead_details` (owner decision 4), the
  always-Unavailable cards, the aging panel and the decision cards.
* **Navigation.** `NAV_ITEMS.pipeline = ["pipeline"]`; `leads`, `deals`,
  `opportunities` are `RETIRED_PAGE_REDIRECTS` carrying their intent (section
  anchor; `open_working`). Links relabelled "Open Pipeline" (Countries drawers,
  Dashboard → Revenue cards).
* **Registries / audits.** Reader-cutover INVENTORY (`pipeline_service` row;
  `dashboard_deals_service` via `build_pipeline`), `_page_builders()` +
  `pipeline_checks` (windows_reconcile, status/reason, list = members);
  parity: a `pipeline` consumer on `closed_won_revenue_usd`,
  `closed_won_deals`, `customers`, `google_ads_cohort_sqls`; the cohort audit
  builds and scans the pipeline payload; SQL-doctrine registry 12 canonical /
  11 legacy / 8 mixed, 0 unknown.
* **Kept for 178C.** `/api/revenue-deals`, `/api/crm-funnel`,
  `/api/crm-funnel/customers-won`, `/api/crm-funnel/operational-status`,
  `/api/dashboard/deals` (still used by the tab).
* **Tests.** `tests/test_pr_ads_178b_pipeline_one_page.py` (12),
  `tests/test_pr_ads_178b_pipeline_pg_integration.py` (11, PG; in the CI
  PostgreSQL step and its did-run list). Existing-test changes with a reason
  each: `docs/61` §9.2.

## PR-ADS-178B-F1 — truth audit round 1 on the Pipeline page

Same branch, a second commit. One Major, no blockers (`docs/61` §11).

* **MAJOR-1.** The "Disqualified / Other" working-status breakdown called
  `leadsCount`, which 178B deleted: the panel hung on "Loading…" with an
  unhandled rejection, and `loadPipeline` threw on a window or filter change
  with that view selected. `leadsCount` is restored (null/undefined is the word
  "Unavailable", never 0); a node test awaits the view's real renderer, and a
  static test checks that every function `app.js` calls is defined.
* **m1.** Customers won is the window's all-source won population whatever
  the lead filters; no longer withheld under one.
* **m3.** The average deal value compares its divisor (`deal_count`) with the
  published deals; a mismatch is `average_deal_value_population_mismatch`.
* **m4 / m5 / o1.** Funnel reasons in words, HubSpot property names in titles;
  the SQL card says the lead filters do not narrow it; a stage count under
  partial evidence is "at least N" with the reason.
* **m2.** Counterfactuals for three guards that survived mutation
  (`pipeline_checks` KPI half, `period_change` previous window, the average's
  deal-count agreement).
* **Tests.** `tests/test_pr_ads_178b_f1_truth_audit_round1.py` (18, non-PG).
  Carried: m6, m7, m8 (`docs/61` §11).

### PR-ADS-178B-F2 — funnel conversion % beside a lower bound

In truth audit round 2, a conversion % next to an "at least N" stage was
still shown as an exact number. Now an undated TO stage gives
"at least X%", and an undated FROM stage gives "—" with the reason
(`pipelineFunnelHtml`). Test: `tests/test_pr_ads_178b_f1_truth_audit_round1.py`
`test_09_a`/`test_09_b`; `test_09_a` fails on 2904d7f. The full note is in
docs/61 §12.

## PR-ADS-177C — the old campaign feed retired; one Keywords & Search Terms page

`docs/60` §9. Depends on PR-ADS-177B (`d543dcc`); rebased onto PR-ADS-178B (§9.13). Read-only: no Google Ads or
HubSpot write added.

* **Retired (404):** `/api/campaigns`, `/api/campaign-detail`, `/api/keywords`,
  `/api/search-terms`, `/api/search-terms/summary`, `/api/search-terms/ngrams`,
  `/api/revenue-attribution` (`/audit` stays). `campaign_evidence_service`, the
  drawer previews, the Campaign Evidence JS, its certification / screenshot /
  benchmark scripts and four test modules go with them. Every truth property
  their tests held is ported first (§9.3 port table). Guard:
  `tests/test_pr_ads_177c_campaign_feed_retired.py` + certification S1.
* **Moved:** `evidence_window_bounds` (`analysis.evidence_windows`),
  `account_today` / `ACCOUNT_TZ` (`analysis.account_time`), the campaign-ID
  resolver (`services/campaign_label_resolver.py`).
* **Parity:** `campaign_id_cohort` (the canonical funnel through the campaign-ID
  resolver, configured account) replaces Campaign Evidence as the independent
  leg of both `cohort_sql_parity` groups; `canonical/campaign_settled` gives
  the five `*__settled_days` identities an independent reading (M8).
* **Certification:** `scripts/audit_campaigns_page_certification.py` (key
  `campaigns`) replaces the Campaign Evidence gate in `AUDITS`; it exits 2 on an
  unreachable database instead of certifying empty payloads.
* **Findings fixed:** two Google Ads campaign ids under one name were merged on
  the settled view and overwritten on Dashboard → Campaigns (now flagged
  `campaign_name_shared_by_google_ads_campaigns`, ids listed, mapping review);
  Dashboard → Campaigns read every account's spend (M3); the live search-term
  routes dropped the "no account configured" reason.
* **Keywords & Search Terms:** one `search-terms` page, Dashboard design, tabs
  Terms / Flagged / Patterns / Keywords; `#/keywords` redirects to its tab; the
  Campaigns item is `[campaigns, search-terms]`.
* **Legacy `keywords` snapshot:** no reader left, so the monthly job no longer
  writes it (our own table); it is a retired dataset. `keyword_daily_facts`
  still syncs monthly.
* **177B minors:** M1–M8 and O1 fixed (§9.7); CLAUDE.md's zero-revenue rule
  rewritten.
* **Tests:** `test_pr_ads_177c_campaign_feed_retired.py` (16),
  `test_pr_ads_177c_campaign_feed_port_pg_integration.py` (10, PG; in the CI
  PostgreSQL step and did-run list). All 26 fail on `d543dcc`. Existing-test
  changes with reasons: `docs/60` §9.9.

## PR-ADS-177C-F1 — truth audit round 1

`docs/60` §9.11. On top of `2cfd095`. Read-only.

* **M-1 (major): a name several Google Ads campaign ids share is labelled
  everywhere it is printed** (production: `low cpc - europe`, two ids). Label,
  don't withhold: the Campaigns table, its drawer and Dashboard → Campaigns say
  "N Google Ads campaigns share this name — figures combined" beside the name
  and the period ratio; drawers list the ids; the badge says "Not judged —
  shared name". Overview's best-campaign and waste signals rank distinct
  campaigns only. Certification S4 / L5 enforce it.
* **Minors:** the settled identities' independent reading applies the
  account's approved "not Google Ads" exclusions (no false mismatch); L6 reads
  the drawers once per window, records drawers probed in the run row and exits
  2 when canonical revenue cannot be read; the Windsor backfill's swallowed
  `write_keywords` step is removed; CLAUDE.md, the API-contract index, a
  docstring and a thresholds comment corrected; docs/60's 157 §4–8 overclaim
  corrected.
* **Runtime:** the Campaigns certification, all five windows, 21.8k contacts /
  401 deals / 42 campaign rows: 26–28 s, 60 MB resident (177C: 39–47 s).
* **Tests:** `tests/test_pr_ads_177c_f1_truth_audit.py` (8) and PG cases 11–13
  in the 177C port module; all fail on `2cfd095`. Carried: L7 coverage, M4
  FX-only rows (`docs/60` §9.11).

## PR-ADS-177C-F2 — truth audit round 2

`docs/60` §9.12. On top of `8876c0f`. Read-only.

* **Major (introduced in F1), fixed.** Overview counts shared-name rows again
  in every total and count: the waste signal's value and count, and the
  Scale card's producers. A shared row is passed over only when ONE row is
  named. When it is the only candidate, it is named with its label, "N Google
  Ads campaigns share this name — figures combined". The Scale, Watch and
  Investigate cards and the signal lines carry the label.
* **Certification L5** now checks Overview's totals and decision cards
  against the through-today campaign view.
* **Dashboard → Deals** labels shared rows. This is minimal because 178B
  rewrites that file: two pass-through fields and one helper call.
* **CLAUDE.md**: the stale `/api/campaign-detail` drawer reference is
  corrected.
* **Production fact:** `low cpc - europe` is two back-to-back ids in account
  3059734490 (a relaunch). Its per-campaign revenue is withheld until its
  label is approved onto one id.
* **Tests:** `test_pr_ads_177c_f1_truth_audit.py` gains 05b–05f, and 05 is
  rewritten. All 6 fail on `8876c0f`.

## PR-ADS-177C on PR-ADS-178B — rebase, and the round-3 carried minors

`docs/60` §9.13. The three 177C commits are cherry-picked onto `bffe604`
(main `07be622` + 178B), with the conflicts resolved so both PRs hold:
`NAV_ITEMS` campaigns `[campaigns, search-terms]` and pipeline `[pipeline]`;
`RETIRED_PAGE_REDIRECTS` keeps roas-campaigns, keywords, leads, deals and
opportunities; CI runs both PRs' new PG modules; the reader-cutover audit
keeps 178B's `pipeline` row, builder and `pipeline_checks` beside 177C's
settled-view and Meta builders. 177C-F2's Dashboard → Deals label is dropped,
because 178B deleted that table. The Campaigns drawer's "Open Deals" becomes
"Open Pipeline". Round-3 minors: the Scale card names a bigger shared row it
passed over; headlines count names when one is shared; L5 matches the exact
label.

## PR-ADS-178C — the legacy Leads / Deals feeds retired

`docs/61` §13. On main `ea6a7ae` (178B + 177C). Read-only: no Google Ads or
HubSpot write added or removed.

* **Retired (404):** `GET /api/revenue-deals` (no caller) and
  `GET /api/crm-funnel/customers-won`. **Kept:** `GET /api/dashboard/deals`
  (Dashboard → Deals fetches it). `GET /api/crm-funnel` (no page caller) was
  not in the brief and is left.
* **Ported first:** the customers-won reading (`load_won_deals`' own
  customers / closed-won-deals verdicts, window, provenance) moved from the
  funnel service into `build_pipeline`'s `customers_won` block; the
  cross-page parity audit reads it there as `pipeline/customers_won` — still
  the one consumer of those identities that does not republish the mart —
  and a new PG case shows it failing on either side. The funnel service
  reads no closed-won deal now; its reader-inventory row is gone.
* **Enforced:** check 21 `retired_readers_absent` of the reader-cutover audit
  (in the production certification) is a static scan. It fails if either
  route is registered again (as a decorator or `add_api_route`, positional or
  `path=`, a literal or a constant) or requested again from `app.js`, or if
  `build_customers_won` returns. CI's live `app.routes` test is the complete
  guard.
* **Dead code removed:** `renderMartDiagnostics`, four retired "Customers
  won" reason labels, the Deals-tab / Leads-page / diagnostics CSS,
  `PAGE_PIPELINE_IMPACT` `leads` / `opportunities`. System Status now maps
  `pipeline` to the canonical contact funnel (`contact_funnel`,
  `lifecycle_events`, which name `page: "pipeline"`); the legacy `leads`
  snapshot names no page (the Pipeline page does not read it).
* **178B minors:** m6 — the deal-source filter is per surface (Pipeline /
  Dashboard → Deals), resets on a window change, and its counts are the
  payload's `source_breakdown`; m7 — the 166A cases run the real Pipeline
  builder (no adapter); m8 — `docs/61` §9.1 / §11 say which cases a mutation
  proves and which only failed for a missing feature; round-2 observation —
  the funnel counts undated stage reaches per attribution scope, so a narrow
  Scope marks only its own lower bounds.
* **Tests:** `tests/test_pr_ads_178c_leads_deals_feeds_retired.py` (9) and
  `tests/test_pr_ads_178c_leads_deals_port_pg_integration.py` (6, in CI's
  PostgreSQL step and did-run list); 15 of 15 fail on `ea6a7ae`, each guard
  also under a named mutation (`docs/61` §13.7). SQL-doctrine inventory: 0
  unknown.

## PR-ADS-179A — certification audits build each window once, in one snapshot

`docs/62`. On top of `ea6a7ae`. Read-only. No check, window or payload
removed.

* **Why.** On 25 Nov 07:30 UTC the daily certification took 16.5 min:
  `reader_cutover` 477 s and `marketing_outcome_cohorts` 430 s of a 900 s
  per-audit limit. A timeout is recorded as crashed, which makes the day NOT
  CERTIFIED with nothing wrong. Each window's pages rebuilt the same inputs:
  per window, 16 mart cores came from 5 distinct inputs and 30
  `load_won_deals` calls from 3.
* **Pinned read snapshot** (`db/connection.py`). `pinned_read_snapshot()`
  makes every `get_conn()` in the block one SAVEPOINT on one `REPEATABLE
  READ, READ ONLY` transaction. A reader's own REPEATABLE READ / READ ONLY
  request is absorbed. Any other transaction mode raises
  `PinnedSnapshotError` (fail closed), and writes are refused by PostgreSQL.
  Outside the block nothing changes.
* **Snapshot memo** (`services/snapshot_memo.py`).
  `certification_window()` opens one pinned snapshot, one closed-won
  universe and one memo. `@memoised` applies to
  `revenue_attribution_service._build_from_db`,
  `canonical_revenue_service.load_won_deals`, `canonical_crm_funnel_service.build`
  and `canonical_contact_outcome_service.build`. A value is cached only
  inside a window, only while its own snapshot is active, and only when `now`
  is explicit. Each caller gets a deep copy. Web requests never open a
  window, so nothing is cached for them.
* **Audits.** The three audits run each window (and each `reader_cutover`
  evidence window) inside its own `certification_window()`. The cohort
  audit's independent SQL now reads at the same instant as the payload it
  reconciles. The campaigns audit now takes one `now` for the whole run.
* **Measured** on a production-sized fixture: 22k contacts, 400 won deals,
  40 campaigns, 19 months of spend and geo, both Meta accounts. Runs used
  `certification_child --memory-mb 320`:

  | Audit | Wall time | Peak RSS |
  | --- | --- | --- |
  | `reader_cutover` | 84–88 → 31–33 s | 109 → 114 MB |
  | `marketing_outcome_cohorts` | 63–69 → 23–24 s | 104 → 108 MB |
  | `campaigns` | 28–37 → 15–16 s | 100 → 105 MB |

  The mart cores per window went from 16 to 6, `load_won_deals` from 30 to
  2–4 and the funnel from 5 to 2.
* **Equivalence.** Each audit's full report is identical before and after,
  ignoring `generated_at`. Every payload each audit built is byte-identical
  by (window, builder, arguments): 90, 212 and 277 payloads, 114 MB in all.
  `test_06` keeps both comparisons, reports and every page payload, in CI.
* **Tests.** `tests/test_pr_ads_179a_certification_runtime.py` has 11 cases
  (10 PostgreSQL), and is added to CI's PG step and its did-run list. Every
  case fails on `ea6a7ae`. Each guard also fails under a named mutation
  (`docs/62` §6): a memo shared across windows, a READ COMMITTED pin, no
  copy, caching outside a window, caching wall-clock calls, a key that drops
  arguments, a dropped payload (Pipeline / the Meta table / Overview), one
  snapshot for the whole audit, any SET TRANSACTION accepted, a universe
  shared without a snapshot, and a key that ignores the window.

## PR-ADS-179A-F1 — truth audit round 1

`docs/62` §3.2, §3.3 and §6.2. On top of `91290f7`. The verdict was merge with
minors: no cross-window leak, and reports identical between base and head.

* **A window deadlocked with `init_db` on deploy (fixed).** A window holds
  AccessShareLock on every table it has read; `init_db`'s startup DDL needs
  AccessExclusiveLock, all in one transaction. Mid-window, PostgreSQL broke
  the cycle by aborting one side. Either the audit's read failed (NOT
  CERTIFIED) or `init_db` logged "init_db failed" and the release ran
  without its migration.
  * Both now take the advisory lock `db.connection.SCHEMA_LOCK_KEY` before
    any table lock. Windows take it shared, at session level, before their
    snapshot, bounded at 60 s, and run unpinned past that. `init_db` takes it
    exclusive as its first statement, bounded at 180 s. Neither waits while
    holding a table lock, so no cycle is possible.
  * Past its bound, `init_db` raises `SchemaMigrationBlocked`, logged at
    ERROR. The lifespan hook fails, the deploy fails and the old release
    stays. It never runs without its schema.
* **A reader that swallows its own failed statement no longer poisons the
  window.** A clean exit in the aborted state rolls back to the reader's
  savepoint, as `get_conn`'s commit does outside a window.
* **Fail closed.** Every `PinnedSnapshotError` is recorded on the pin as it
  is raised. Each audit reports a window with any refusal as exit 2: cannot
  check, unavailable, or a page not run. This holds even if a reader
  swallowed the error; on `91290f7` such a window certified with exit 0.
  * The mode check also covers `sql.Composed`, `bytes` and `executemany`.
  * A statement that ends the transaction is caught when it runs, and every
    later read in that window is refused.
  * A refusal raised outside any `try` crashes the audit, which is NOT
    CERTIFIED.
* **Docs.** Certification checks a snapshot-consistent build of each page.
  Web requests still read per statement under READ COMMITTED, so a torn read
  inside one request is invisible to certification by construction.
  `reader_cutover`'s closed-won universe is read outside every pin.
* **Tests.** `test_pr_ads_179a_certification_runtime.py` gains 08_a–10_b, 17
  cases in all. Each new case fails on `91290f7`; 08_a fails with the real
  `DeadlockDetected`. Each also fails under a named mutation (M14–M17).

## PR-ADS-179B — certification runtime: the re-audit's minors

`docs/62` §8. On top of `9a09c6d` (PR-ADS-179A). Read-only. No check, gate,
window or exit code changed.

* **`SchemaMigrationBlocked` names both holders.** The schema lock also
  serialises two `init_db` calls. For example,
  `scripts/backfill_meta_ads_spend.py` running during a deploy holds it just
  as a window does. The message now names certification window(s) or another
  migration.
* **The Meta backfill CLI exits 2.** On `SchemaMigrationBlocked` it now exits
  2 with `schema_migration_blocked` and never reaches Meta. Before, it exited
  1 with a traceback.
* **`init_db` documents its real guarantee.** Only the lock-wait bound
  raises. Every other error, including a `QueryCanceled` from a role-level
  `statement_timeout` during the advisory wait, is still logged and
  swallowed, unchanged. The docstring and `docs/62` overclaimed.
* **No statement runs after a reader ends the pinned transaction.** 179A
  checked only for an IDLE connection, which three escapes got past:
  * `SELECT 1; COMMIT; BEGIN ISOLATION LEVEL READ COMMITTED`;
  * `ROLLBACK AND CHAIN`, which has the same modes but a new snapshot;
  * a commit through `cursor.connection`.

  Each let the next statement in the block read outside the snapshot. Now:
  * the pinned transaction carries a transaction-local marker;
  * after any statement that names a transaction-ending verb, in its
    template or in the text psycopg2 sent, and after every `executemany`,
    the session must still show that marker, REPEATABLE READ and read only;
  * before every statement, an IDLE connection is refused;
  * `ROLLBACK AND CHAIN`, `ABORT` and `PREPARE TRANSACTION` are refused
    before they are sent.

  Asking the session was chosen over a verb blacklist on the statement text:
  `END` closes every `CASE`, so no text rule is both safe and exact.
* **An unpinned window is disclosed.** A window that runs unpinned (the
  schema lock was held past its bound) now reports `snapshot: {pinned:
  false, reason, detail}` in all three audits. Each report also carries a
  `snapshot_pinning` summary whose note says that what was certified there
  is not a snapshot-consistent build. The exit code is unchanged.
* **The shared lock is released before pooling.** A new case reads
  `pg_locks` on the window connection's own backend pid. The shared lock is
  gone once the window closes, and the same connection is then reused from
  the pool.
* **Tests.** `tests/test_pr_ads_179b_certification_runtime_followups.py` has
  10 PostgreSQL cases and is added to CI's PG step and did-run list. Two
  cases in the 179a module changed:
  * `test_06` now compares the reports without the pinning keys, which
    differ by design. It asserts all windows pinned with the windows, and
    all `not_recorded` without them.
  * The `10_a` docstring no longer claims more than it tests.

  Every new guard fails on `9a09c6d`, or under a named mutation (M18–M25)
  where 179A already held.
