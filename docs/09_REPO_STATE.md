# Repository State — Single Source of Truth
## Logistaas Ads Intelligence System

**Last updated:** PR-ADS-018 — Modern UI Dashboard Foundation (April 2026)

> This document reflects the **actual state of the repository** — not what was planned or intended.
> Update this file in every PR that changes the state of any module listed below.
> AI agents must read this file at the start of each session before assuming what is built.

---

## Built and Verified (safe to call)

| File | Module | Notes |
|------|--------|-------|
| `connectors/hubspot_pull.py` | HubSpot CRM connector | Pulls contacts, deals, GCLID; retry logic included |
| `connectors/windsor_pull.py` | Windsor.ai connector | Pulls campaigns, search terms, keywords, geo |
| `connectors/gclid_match.py` | GCLID reconciliation | Joins Windsor + HubSpot via GCLID; falls back if `logistaas_config.yaml` missing |
| `analysis/core.py` | Waste detection + lead quality + campaign truth | All three functions in one file; `load_json` defined at line 471 |
| `analysis/advisor.py` | Claude API report generator | `generate_weekly_report()` and `generate_monthly_report()` |
| `scheduler/weekly.py` | Weekly report orchestrator | Full pipeline: pull → analyse → report → deliver |
| `scheduler/monthly.py` | Monthly report orchestrator | Full pipeline: pull → analyse → report → deliver; per-step error handling |
| `scheduler/delivery.py` | SendGrid email delivery | Delivers weekly and monthly report files; returns bool |
| `scheduler/run_history.py` | Persistent run log | Writes JSONL to `runtime_logs/run_history.jsonl` |
| `scripts/healthcheck.py` | Pre-flight environment check | Validates env vars, dirs, imports; exits non-zero on failure |
| `config/thresholds.yaml` | Decision thresholds | FIX/HOLD/SCALE/CUT rules; lead quality; waste detection |
| `config/junk_patterns.yaml` | Junk pattern library | Intent mismatch patterns; safe-terms whitelist |
| `render.yaml` | Render.com deployment | Web service (uvicorn) + 3 cron jobs: daily (6am), weekly (Mon 7am), monthly (1st 7am) |
| `Makefile` | Manual ops runner | `healthcheck`, `daily`, `weekly`, `monthly`, `validate`, `runs` targets |
| `scheduler/daily.py` | Daily pulse orchestrator | Step counter fixed; structured logging per step; result saved to `outputs/daily_YYYY-MM-DD.json` |
| `scripts/validate_phase1.py` | Phase 1 read-only validation | Syntax, YAML, docs, and stale-reference checks |
| `scripts/phase1_readiness.py` | Phase 1 production readiness audit | Env vars, files, Makefile targets, render.yaml, forbidden modules, delegates to healthcheck + validate |
| `docs/PHASE1_PRODUCTION_READINESS.md` | Go/no-go checklist | Official Phase 1 production readiness gate |
| `.env.example` | Environment variable reference | All required vars documented |
| `requirements.txt` | Python dependencies | All runtime deps present |
| `api/__init__.py` | API package declaration | Declares `api/` as a Python package |
| `api/server.py` | FastAPI web entry point | Phase 1 endpoints: `/health`, `/readiness`, `/runs/latest`, `/reports/latest`, `/reports/latest/raw`; protected run endpoints: `POST /run/daily`, `/run/weekly`, `/run/monthly`; `GET /` serves dashboard |
| `static/index.html` | Dashboard UI | System status cards, latest run/report panels, manual run controls, doctrine reminder |
| `static/app.js` | Dashboard frontend logic | Fetches all API endpoints; triggers runs with Bearer token; stores token in sessionStorage only |
| `static/styles.css` | Dashboard styles | Modern neutral SaaS style; responsive cards and panels |

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
| PR-ADS-018 | Modern UI Dashboard Foundation (`static/index.html`, `app.js`, `styles.css`, `GET /`) | ✅ This PR |
| **Next state** | **PR-ADS-019 — In-App Scheduler** | 🟢 Next |
| PR-ADS-005 | Config hardening — create `config/logistaas_config.yaml`, validate all YAML keys | ⬜ Post-validation |

---

## What Is Intentionally Not Built (Phase 2+)

> These items are **deferred by design**, not missing or broken.
> OCT and negative push require Phase 1 to be proven accurate before activation.

- `connectors/oct_uploader.py` — Phase 2 gate, requires Phase 1 validated
- `connectors/negative_pusher.py` — Phase 3
- Manual run API endpoints — built in PR-ADS-017 (requires `ADMIN_API_TOKEN`)
- Frontend dashboard — built in PR-ADS-018
- In-app scheduler — PR-ADS-019 (not yet built)
- Meta Ads connector — Phase 4
