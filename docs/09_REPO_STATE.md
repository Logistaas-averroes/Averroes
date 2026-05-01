## Repository State — Single Source of Truth
## Logistaas Ads Intelligence System

**Last updated:** PR-ADS-024 — PostgreSQL Foundation: Schema, Writers, and Time-Range API (May 2026)

> This document reflects the **actual state of the repository** — not what was planned or intended.
> Update this file in every PR that changes the state of any module listed below.
> AI agents must read this file at the start of each session before assuming what is built.

---

## Built and Verified (safe to call)

| File | Module | Notes |
|------|--------|-------|
| `connectors/hubspot_pull.py` | HubSpot CRM connector | associations_api crash fixed (PR-ADS-027); now uses CRM v4 REST API for associations — version-agnostic |
| `connectors/windsor_pull.py` | Windsor.ai connector | search term query fixed (PR-ADS-027); removed segment=search_term (400 error), switched to date_preset |
| `connectors/gclid_match.py` | GCLID reconciliation | Joins Windsor + HubSpot via GCLID; falls back if `logistaas_config.yaml` missing |
| `analysis/core.py` | Waste detection + lead quality + campaign truth | All three functions in one file; `load_json` defined at line 471 |
| `analysis/rule_advisor.py` | Deterministic report generator | **NEW in PR-ADS-021** — `generate_deterministic_report(report_type)` generates markdown from structured JSON outputs; no external API; replaces Claude as default |
| `analysis/advisor.py` | Report generation dispatcher | `generate_weekly_report()` and `generate_monthly_report()` — now defaults to `rule_advisor`; Claude optional via `ADVISOR_MODE=claude`; importing does not require `ANTHROPIC_API_KEY` |
| `scheduler/weekly.py` | Weekly report orchestrator | Full pipeline: pull → analyse → report → deliver; uses deterministic advisor by default; also writes to Postgres after each step (PR-ADS-024) |
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
| `.env.example` | Environment variable reference | Updated in PR-ADS-021: `ADVISOR_MODE`, `APP_SECRET_KEY`, `AUTH_USERS_JSON` added; Claude moved to optional |
| `requirements.txt` | Python dependencies | Added psycopg2-binary (PR-ADS-024) |
| `api/__init__.py` | API package declaration | Declares `api/` as a Python package |
| `api/auth.py` | Internal auth module | Updated in PR-ADS-021B: `authenticate_user()` added — supports both `password_hash` (PBKDF2) and `password` (plain-text fallback via `hmac.compare_digest`); passwords never logged or exposed in API responses |
| `api/server.py` | FastAPI web entry point | Updated in PR-ADS-021B: `/auth/login` now uses `authenticate_user()` for dual-mode credential verification; Updated in PR-ADS-024: DB init in lifespan + 6 new `/api/*` endpoints with `?days=` param |
| `api/scheduler.py` | In-app APScheduler | Schedules daily (06:00), weekly (Mon 07:00), monthly (1st 08:00) Phase 1 jobs in Asia/Amman timezone; exposes shared lock state and `get_scheduler_status()` |
| `db/__init__.py` | DB package | New in PR-ADS-024 |
| `db/schema.py` | PostgreSQL schema + init_db() | CREATE TABLE IF NOT EXISTS; idempotent; non-fatal. New in PR-ADS-024 |
| `db/connection.py` | Connection pool, non-fatal if unavailable | ThreadedConnectionPool max 10; DATABASE_URL from env; yields None if unavailable. New in PR-ADS-024 |
| `db/writers.py` | Write runs, campaigns, leads, waste, deals | All functions non-fatal; MQL status mapped to status_category. New in PR-ADS-024 |
| `static/index.html` | Dashboard UI  | Updated in PR-ADS-023: 5-page brand-aligned SPA — login screen, sidebar nav, 7 page containers |
| `static/app.js`     | Frontend logic | Updated in PR-ADS-023: full SPA routing, role-based UI, markdown report parser, API wiring for all 7 pages |
| `static/styles.css` | Dashboard styles | Updated in PR-ADS-023: Sora font, full Logistaas brand token system, sidebar layout, all components |
| `scripts/verify_live_deployment.py` | Live deployment verifier | Updated in PR-ADS-021: checks `/health` is public; checks protected endpoints return 401 when unauthenticated; optional login test via `TEST_USERNAME`/`TEST_PASSWORD` |
| `docs/LIVE_VALIDATION_LOG.md` | Phase 1 validation log | Official 4-week validation tracking template |

**Phase 1 state:** Read-only. Deterministic advisor active. Internal auth active. Claude API optional.

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
