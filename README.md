# Logistaas Ads Intelligence System

An automated, doctrine-driven Google Ads advisory engine for Logistaas — a TMS SaaS platform operating across 80+ countries.

This system connects Windsor.ai (Google Ads data), HubSpot CRM (pipeline data), and the Anthropic Claude API (doctrine analysis) to produce daily, weekly, and monthly revenue-focused recommendations.

**Core principle:** Signal Integrity > Scale. Revenue Attribution > Platform Metrics.

---

## Stack

| Component | Tool | Cost |
|-----------|------|------|
| AI engine | Claude Sonnet (Anthropic API) | ~$20/mo |
| Version control | GitHub | $10/mo |
| Google Ads data | Windsor.ai Basic | $23/mo |
| Hosting + scheduler | Render.com | $7/mo |
| **Total** | | **$60/mo** |

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/logistaas-ads-intelligence.git
cd logistaas-ads-intelligence
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set environment variables

Copy `.env.example` to `.env` and fill in all values:

```bash
cp .env.example .env
```

Required variables:
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `HUBSPOT_API_KEY` — from HubSpot Settings > Integrations > API Key
- `WINDSOR_API_KEY` — from Windsor.ai account settings
- `GOOGLE_ADS_DEVELOPER_TOKEN` — from Google Ads API Center
- `GOOGLE_ADS_CLIENT_ID` — OAuth client ID
- `GOOGLE_ADS_CLIENT_SECRET` — OAuth client secret
- `GOOGLE_ADS_REFRESH_TOKEN` — from OAuth flow
- `GOOGLE_ADS_CUSTOMER_ID` — your Google Ads account ID (no dashes)
- `REPORT_EMAIL` — email address for report delivery
- `SLACK_WEBHOOK_URL` — (optional) Slack webhook for alerts

### 4. Run your first daily pulse

```bash
python -m scheduler.daily
```

---

## Project Structure

```txt
logistaas-ads-intelligence/
├── api/                         # FastAPI app, auth, read-only endpoints, admin-gated run triggers
├── analysis/                    # Read-only analysis logic: waste, lead quality, campaign truth, N-Grams, advisor output
├── connectors/                  # Read-only data pulls from Windsor.ai and HubSpot; no analysis or decisions
├── config/                      # Thresholds, junk patterns, N-Gram stopwords
├── db/                          # Local persistence schema and writers
├── docs/                        # Doctrine, roadmap, governance, audits, PR workflow
├── scheduler/                   # Daily, weekly, monthly orchestration
├── scripts/                     # Healthcheck, validation, readiness, benchmarks
├── static/                      # Static dashboard UI
├── tests/                       # Pytest suite
├── outputs/                     # Generated reports; runtime only
└── runtime_logs/                # Run history logs; runtime only
```

Core architecture rule:

```
connectors/ → fetch only
analysis/   → analyze only
scheduler/  → orchestrate only
api/        → expose read-only data and admin-gated run triggers
static/     → display only
config/     → decision thresholds and patterns
```

> **Phase 1 remains read-only.** The system does not write to Google Ads, does not write
> to HubSpot, does not push negative keywords, does not upload offline conversions, and
> does not change bids or budgets.

---

## Deployment on Render

1. Push this repo to GitHub
2. Go to render.com → New → Blueprint → Connect your repo
3. Render will detect `render.yaml` and preview **one web service**
4. Set all environment variables in the Render dashboard
5. Render auto-deploys on every push to `main`

The Phase 1 deployment is a **single Render web service**. Render cron jobs are not
required — scheduled daily, weekly, and monthly jobs run inside the FastAPI process
via in-app APScheduler (introduced in PR-ADS-019). See `docs/DEPLOYMENT.md` for full
deployment topology and environment variable reference.

---

## Doctrine

All recommendations are governed by the **Avverros Ads Specialist Doctrine** defined in `docs/DOCTRINE.md`.

The system will never:
- Recommend Broad Match without a negative keyword architecture
- Mix Brand and Non-Brand signals
- Scale during learning phase
- Optimise for CPL alone — always CPQL (Cost Per Qualified Lead)
- Assume conversions = revenue without CRM verification

---

## Mailchimp (read-only email-marketing evidence)

PR-ADS-151 adds a **pull-only** connection to the Logistaas Mailchimp account. The
connector (`connectors/mailchimp_pull.py`) issues **GET requests exclusively** — it
never creates, edits, sends, schedules, or deletes anything in Mailchimp, and there
is no mutation route anywhere in the API. Credentials are server-side only and are
never exposed through the API or frontend.

Configure via environment variables (see `.env.example`):

| Variable | Purpose |
|----------|---------|
| `MAILCHIMP_API_KEY` | Marketing API key (its `-usXX` suffix derives the data centre) |
| `MAILCHIMP_SERVER_PREFIX` | Optional explicit data-centre prefix (e.g. `us21`) |
| `MAILCHIMP_ENABLED` | `true` to enable live pulls; otherwise the connector reports "not configured" and never touches the network |

Durable, additive tables (`mailchimp_campaigns`, `mailchimp_campaign_reports`,
`mailchimp_campaign_links`, `mailchimp_audience_snapshots`, `mailchimp_sync_state`)
store campaign/report/link/audience evidence keyed on immutable Mailchimp IDs, so
repeated syncs upsert in place and never duplicate campaigns or metrics. Read-only
endpoints: `GET /api/mailchimp/{status,audit,campaigns,campaign-detail}`.

## HubSpot Fields Used

| Field | Purpose |
|-------|---------|
| `hs_google_click_id` | GCLID — links ad click to contact |
| `mql_status` | Lead qualification status |
| `hs_lead_status` | Sales outreach status |
| `hs_analytics_source_data_1` | Campaign name (UTM) |
| `hs_analytics_source_data_2` | Keyword (UTM) |
| `ip_country` | Lead geography |
| `lifecyclestage` | Funnel position |
