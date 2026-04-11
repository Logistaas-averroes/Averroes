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
python scheduler/daily.py
```

---

## Project Structure

```
logistaas-ads-intelligence/
├── connectors/
│   ├── windsor_pull.py       # Google Ads data via Windsor.ai
│   ├── hubspot_pull.py       # CRM contacts, deals, GCLIDs
│   ├── gclid_match.py        # Match clicks to pipeline
│   └── oct_uploader.py       # Feed deal data back to Google Ads
├── engine/
│   ├── signal_check.py       # Brand vs non-brand, integrity audit
│   ├── ngram_analysis.py     # Search term forensics
│   ├── campaign_classifier.py # FIX / HOLD / SCALE / CUT
│   └── lead_quality.py       # MQL vs SQL vs junk scoring
├── doctrine/
│   └── advisor.py            # Claude API doctrine engine
├── scheduler/
│   ├── daily.py              # 6am GMT daily pulse
│   ├── weekly.py             # Monday 7am weekly report
│   └── monthly.py            # 1st of month strategy
├── api/
│   └── server.py             # FastAPI — on-demand triggers
├── config/
│   ├── logistaas_config.yaml # Markets, tiers, thresholds
│   └── patterns.yaml         # Intent mismatch pattern library
├── docs/
│   └── DOCTRINE.md           # Avverros doctrine rules (system prompt source)
├── data/                     # Runtime data (gitignored)
├── outputs/                  # Generated reports (gitignored)
├── .env.example
├── requirements.txt
└── render.yaml               # Render.com deployment config
```

---

## Deployment on Render

1. Push this repo to GitHub
2. Go to render.com → New → Web Service → Connect your repo
3. Set all environment variables in the Render dashboard
4. Render auto-deploys on every push to `main`
5. Cron jobs are defined in `render.yaml` — no manual setup needed

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

Deal stages mapped to OCT values — see `config/logistaas_config.yaml`.
