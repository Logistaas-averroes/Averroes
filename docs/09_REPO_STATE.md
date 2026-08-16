## Repository State — Single Source of Truth
## Logistaas Ads Intelligence System

**Last updated:** PR-ADS-153E-A2 — Canonical Revenue Cutover Gate Hardening (August 2026)

> This document reflects the **actual state of the repository** — not what was planned or intended.
> Update this file in every PR that changes the state of any module listed below.
> AI agents must read this file at the start of each session before assuming what is built.

---

> ### ⚠️ Authoritative status (PR-ADS-153E-A, August 2026)
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
>   proven completion. Still shadow mode; still no consumer switched.
> * **153E-B: blocked** until the production evidence in
>   `docs/35_CANONICAL_REVENUE_LEDGER.md` §11 passes
>   (`--all-windows` aggregate `ok: true`). Then: revenue consumer cutover and
>   Unit Economics migration.
> * **153F and 153G: remain.** Geo synchronization; legacy table/route deletion.
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
| `scripts/backfill_canonical_deal_ledger.py` | Operator historical bootstrap | **NEW in PR-ADS-153E-A2** — drives the bounded, resumable bootstrap to a completion proven in the DURABLE state. Bounded by `--max-passes`; stops on first failure; `--restart` is opt-in. No HTTP endpoint, no startup trigger, no PII in output |
| `analysis/crm_lifecycle.py` | Canonical CRM lifecycle taxonomy | **NEW in PR-ADS-153B** — HubSpot Lifecycle Stage is the funnel spine. Funnel events (lead/mql/sql/opportunity/customer) each map to their own `hs_v2_date_entered_*` property. Pure, no I/O |
| `analysis/mql_status_taxonomy.py` | The ONE `mql_status` mapping | **NEW in PR-ADS-153B** — replaces four divergent copies. Maps every live value incl. previously-unmapped `CLOSED - Bad Contact` / `CLOSED - No Response` / `RESELLER`; distinguishes `no_verdict` from `unmapped`. Operational dimension only — NOT a funnel definition |
| `services/hubspot_contact_funnel_sync_service.py` | Canonical contact ingestion | **NEW in PR-ADS-153B** — SOLE writer of `hubspot_contact_funnel`. All-source, watermarked on `lastmodifieddate`, resumable, durable bootstrap state. Read-only vs HubSpot |
| `services/canonical_crm_funnel_service.py` | Canonical funnel contract | **NEW in PR-ADS-153B** — one definition of Lead/MQL/SQL/Opportunity/Lifecycle-Customer; named scopes (`keyword ≤ campaign ≤ google_ads_source ≤ all_source`); cohort-safe conversions; fail-closed (`unavailable ≠ zero`) |
| `services/crm_funnel_reconciliation_service.py` | Legacy vs lifecycle reconciliation | **NEW in PR-ADS-153B** — contact-by-contact mismatch classes + before/after SQL comparison split into DATE-SHIFT vs POPULATION causes. No emails returned |
| `db/crm_funnel_repository.py` | Canonical funnel reads | **NEW in PR-ADS-153B** — read-only; explicit `available: false` rather than an empty result |
| `scripts/audit_crm_funnel_truth.py` | Production funnel audit | **NEW in PR-ADS-153B** — read-only Render validation (coverage, mql_status mapping, legacy reconciliation, source split) |
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
