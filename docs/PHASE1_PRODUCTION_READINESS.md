# Phase 1 Production Readiness — Go / No-Go Checklist
## Logistaas Ads Intelligence System

**Doctrine:** Avverros v1.0 | **Phase:** 1 — Read Only
**Status:** Entering 4-week validation period

> This document is the official gate before the system is declared production-ready for
> unattended scheduled runs.  Every item must be checked manually before the system is
> considered "live."

---

## 1. Architecture Confirmation

Phase 1 is strictly read-only.  Confirm each constraint before proceeding.

| Constraint | Status |
|-----------|--------|
| `connectors/` — fetch data only, no writes | ⬜ Confirmed |
| `analysis/` — analyse data only, no writes | ⬜ Confirmed |
| `scheduler/` — orchestration only, no writes | ⬜ Confirmed |
| No Google Ads write-back (no OCT, no negatives) | ⬜ Confirmed |
| No HubSpot write-back | ⬜ Confirmed |

**Deferred (Phase 2+):**
- `connectors/oct_uploader.py` — not built, not active
- `connectors/negative_pusher.py` — not built, not active

---

## 2. Required Render Services

Three cron jobs must be registered and active in Render.com before unattended runs.

| Service | Schedule | Status |
|---------|----------|--------|
| Daily cron (`python -m scheduler.daily`) | 06:00 UTC daily | ⬜ Active |
| Weekly cron (`python -m scheduler.weekly`) | 07:00 UTC every Monday | ⬜ Active |
| Monthly cron (`python -m scheduler.monthly`) | 07:00 UTC 1st of month | ⬜ Active |

Verify in the Render dashboard that each service shows **Active** and the schedule
matches the values in `render.yaml`.

---

## 3. Required Environment Variables

All of the following must be set in the Render environment (and in `.env` for local
testing).  Use `.env.example` as the reference template.

| Variable | Purpose | Status |
|----------|---------|--------|
| `ANTHROPIC_API_KEY` | Claude API — weekly/monthly reports | ⬜ Set |
| `HUBSPOT_API_KEY` | HubSpot CRM connector | ⬜ Set |
| `WINDSOR_API_KEY` | Windsor.ai connector | ⬜ Set |
| `WINDSOR_ACCOUNT_ID` | Windsor.ai account identifier | ⬜ Set |
| `SENDGRID_API_KEY` | SendGrid delivery | ⬜ Set |
| `REPORT_SENDER_EMAIL` | Verified SendGrid sender address | ⬜ Set |
| `REPORT_RECIPIENT_EMAIL` | Report delivery address | ⬜ Set |

Run `make healthcheck` to verify all required vars are present.

---

## 4. Required Local Commands

The following commands must run without error before the system is declared ready.

```bash
make healthcheck   # validate env vars + runtime dependencies
make validate      # Phase 1 read-only validation (syntax, YAML, docs, stale refs)
make readiness     # Phase 1 production readiness audit
make daily         # run the daily pulse scheduler manually
make weekly        # run the weekly report scheduler manually
make monthly       # run the monthly report scheduler manually
```

Each command must exit with code 0.

---

## 5. Required Render Manual Tests

Before enabling unattended scheduled runs, trigger each cron job once manually and
confirm the following for each run.

### Daily cron manual test
- [ ] Trigger daily cron manually in Render dashboard
- [ ] Confirm exit code 0
- [ ] Confirm logs show each pipeline step completing
- [ ] Confirm `outputs/daily_YYYY-MM-DD.json` is written

### Weekly cron manual test
- [ ] Trigger weekly cron manually in Render dashboard
- [ ] Confirm exit code 0
- [ ] Confirm logs show report generation step
- [ ] Confirm delivery was attempted (SendGrid call logged)

### Monthly cron manual test
- [ ] Trigger monthly cron manually in Render dashboard
- [ ] Confirm exit code 0
- [ ] Confirm logs show report generation step
- [ ] Confirm delivery was attempted (SendGrid call logged)

---

## 6. SendGrid Readiness

| Check | Status |
|-------|--------|
| Sender identity verified in SendGrid dashboard | ⬜ Verified |
| `REPORT_SENDER_EMAIL` matches the verified sender | ⬜ Confirmed |
| `REPORT_RECIPIENT_EMAIL` is correct and reachable | ⬜ Confirmed |
| Failed delivery logs are visible in SendGrid activity feed | ⬜ Tested |

If delivery fails, check SendGrid activity for bounce or spam block events.  Do not
proceed with unattended runs until at least one successful delivery is confirmed.

---

## 7. Success Criteria

All of the following must be true for the system to be declared Phase 1 production-ready.

| Criterion | Status |
|-----------|--------|
| Daily run completes without critical error | ⬜ Pass |
| Weekly report generated and delivered | ⬜ Pass |
| Monthly report generated and delivered | ⬜ Pass |
| Delivery attempted (SendGrid) with no authentication errors | ⬜ Pass |
| Run history updated (`runtime_logs/run_history.jsonl`) | ⬜ Pass |
| No missing env var / config / import errors in any run | ⬜ Pass |
| `make readiness` exits 0 | ⬜ Pass |
| `make healthcheck` exits 0 | ⬜ Pass |
| `make validate` exits 0 | ⬜ Pass |

---

## 8. Go / No-Go Decision Table

| Outcome | Condition |
|---------|-----------|
| **GO** ✅ | All items in sections 1–7 are checked and passing |
| **NO-GO** 🚫 | Any critical item in sections 1–7 is failing or unchecked |

**Critical items** (NO-GO if any one fails):
- Any required environment variable is missing
- Any required Render cron service is not active
- Any manual Render run exits non-zero
- SendGrid delivery authentication fails
- `make readiness` or `make healthcheck` or `make validate` exits non-zero

**Non-blocking warnings** (GO proceeds with awareness):
- `data/ads_campaigns_7d.json` absent — anomaly detection silently skips, not critical
- `config/logistaas_config.yaml` absent — `gclid_match.py` falls back to defaults (70%)

---

## 9. Post-Go Actions

Once GO is declared:

1. Enable all three Render cron jobs for unattended scheduling.
2. Record the go-live date as the start of the **4-week validation period**.
3. During validation, check each weekly report manually against actual account performance.
4. Apply at least 3 specific recommendations by hand and verify the outcome.
5. At the end of the 4-week period, Youssef reviews and explicitly approves or defers Phase 2.

**Phase 2 does not begin until all four advancement criteria are met** (see
`docs/04_PHASE_ROADMAP.md` — Phase 1 Advancement Criteria).
