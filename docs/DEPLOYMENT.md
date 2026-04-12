# Logistaas Ads Intelligence System — Deployment Guide

## Overview

This document covers the Render.com deployment for Phase 1 of the Logistaas Ads Intelligence System.

Phase 1 deploys **three cron jobs only**. No web service is included — FastAPI (`api/server.py`) is a Phase 4 deliverable.

---

## Architecture

```
Render.com
 ├── logistaas-daily-pulse    (cron: 0 6 * * *)   → python scheduler/daily.py
 ├── logistaas-weekly-report  (cron: 0 7 * * 1)   → python scheduler/weekly.py
 └── logistaas-monthly-strategy (cron: 0 7 1 * *) → python scheduler/monthly.py
```

All services are defined in `render.yaml` at the repo root.

---

## Required Environment Variables

Set each variable in the Render dashboard under **Service → Environment** for every cron service that requires it.

| Variable | Required by | Description |
|----------|-------------|-------------|
| `ANTHROPIC_API_KEY` | all schedulers | Claude API key for doctrine analysis |
| `HUBSPOT_API_KEY` | all schedulers | HubSpot private app token |
| `WINDSOR_API_KEY` | all schedulers | Windsor.ai API key |
| `WINDSOR_ACCOUNT_ID` | all schedulers | Windsor.ai account identifier |
| `SENDGRID_API_KEY` | weekly, monthly | SendGrid API key for email delivery |
| `REPORT_SENDER_EMAIL` | weekly, monthly | Verified SendGrid sender address |
| `REPORT_RECIPIENT_EMAIL` | weekly, monthly | Report delivery recipient |

> **Note:** `GOOGLE_ADS_*` variables are reserved for `connectors/oct_uploader.py` (not yet built). Do not configure them until that module is available.

---

## Deployment Steps

### 1. Connect repository to Render

1. Log into [render.com](https://render.com) → **New** → **Blueprint**.
2. Connect the `Logistaas-averroes/Averroes` GitHub repository.
3. Render will detect `render.yaml` and preview the three cron services.
4. Click **Apply**.

### 2. Set environment variables

For each of the three cron services (`logistaas-daily-pulse`, `logistaas-weekly-report`, `logistaas-monthly-strategy`):

1. Open the service → **Environment** tab.
2. Add each variable from the table above that applies to that service.
3. Click **Save Changes**.

Refer to `.env.example` for the full variable reference.

### 3. Trigger a manual run (optional verification)

In the Render dashboard, open any cron service and click **Trigger Run**. Watch the **Logs** tab for output.

Expected daily pulse output:
```
============================================================
LOGISTAAS DAILY PULSE — YYYY-MM-DD HH:MM UTC
============================================================
Step 1/5: Pulling Google Ads data (last 2 days)...
Step 2/5: Pulling HubSpot contacts (last 2 days)...
Step 3/5: Running anomaly detection...
Step 4/5: Checking for new junk search terms...
Step 5/5: Running doctrine analysis...
Report saved: outputs/daily_YYYY-MM-DD.json
Daily pulse complete. Status: ...
```

---

## Cron Schedule Reference

| Service | Schedule | Runs |
|---------|----------|------|
| `logistaas-daily-pulse` | `0 6 * * *` | Every day at 06:00 UTC |
| `logistaas-weekly-report` | `0 7 * * 1` | Every Monday at 07:00 UTC |
| `logistaas-monthly-strategy` | `0 7 1 * *` | 1st of each month at 07:00 UTC |

All times are UTC. Render cron jobs run in UTC by default.

---

## Post-Merge Verification Checklist

After deploying, confirm the following in the Render dashboard:

- [ ] **Daily job registered** — `logistaas-daily-pulse` visible under Cron Jobs with schedule `0 6 * * *`
- [ ] **Weekly job registered** — `logistaas-weekly-report` visible with schedule `0 7 * * 1`
- [ ] **Monthly job registered** — `logistaas-monthly-strategy` visible with schedule `0 7 1 * *`
- [ ] **First successful run confirmed** — trigger a manual run; logs show no errors and exit code 0
- [ ] **Report file created** — `outputs/daily_YYYY-MM-DD.json` (or weekly/monthly `.md`) visible in logs or attached storage
- [ ] **Delivery attempted** — for weekly/monthly, logs show `[DELIVERY SUCCESS]` or a documented failure reason

---

## Failure Handling

### Missing environment variables

| Condition | Behaviour |
|-----------|-----------|
| `ANTHROPIC_API_KEY` missing | `analysis/advisor.py` raises `AuthenticationError` on first Claude call; run exits with traceback |
| `HUBSPOT_API_KEY` missing | `connectors/hubspot_pull.py` returns empty contacts list; downstream steps proceed with no data |
| `WINDSOR_API_KEY` / `WINDSOR_ACCOUNT_ID` missing | `connectors/windsor_pull.py` returns empty campaigns list; downstream steps proceed with no data |
| `SENDGRID_API_KEY` missing | `scheduler/delivery.py` logs `[DELIVERY FAILURE] SENDGRID_API_KEY is not set` and returns `False`; scheduler still exits cleanly |
| `REPORT_SENDER_EMAIL` missing | Same as above — delivery skipped, run does not fail |
| `REPORT_RECIPIENT_EMAIL` missing | Same as above |

### API failures during cron run

| Condition | Behaviour |
|-----------|-----------|
| Windsor.ai API returns error / timeout | Monthly and weekly schedulers catch the exception, log `Step 1/6 FAILED`, and return `None` (run aborts cleanly) |
| HubSpot API returns 429 rate limit | `hubspot_pull.py` retries with exponential back-off; if retries exhausted, raises exception caught by monthly scheduler |
| HubSpot API returns other error | Same retry logic; ultimately caught by monthly scheduler |
| Claude API returns unexpected format | `analysis/advisor.py` returns `None`; monthly scheduler logs `Step 6/6 FAILED` |
| SendGrid delivery fails | `delivery.py` logs `[DELIVERY FAILURE]` with HTTP status; returns `False`; does not raise |

All failures are logged to stdout, which Render captures and displays in the **Logs** tab.

---

## Rollback

To roll back the deployment:

1. In the Render dashboard, navigate to a cron service → **Deploys**.
2. Select the previous successful deploy and click **Redeploy**.

`render.yaml` changes are reversible: reverting the commit and redeploying via Blueprint restores the previous configuration.

**Rollback risk level: Low** — cron jobs are stateless; reverting does not affect data already written to `data/` or `outputs/`.

---

## Local Validation

To validate schedulers locally before deploying:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env with real credentials

# 3. Run each scheduler manually
python scheduler/daily.py
python scheduler/weekly.py
python scheduler/monthly.py
```

To syntax-check without running:
```bash
python -m py_compile scheduler/daily.py
python -m py_compile scheduler/weekly.py
python -m py_compile scheduler/monthly.py
```

---

## Non-Goals (Phase 1)

- No FastAPI web service (`api/server.py` is Phase 4)
- No Slack delivery
- No retry queue
- No OCT uploads (requires `connectors/oct_uploader.py`)
- No dashboard
