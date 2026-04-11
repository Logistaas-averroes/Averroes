# Claude Code Briefing — Logistaas Ads Intelligence System

## WHAT THIS IS

You are continuing development of the Logistaas Ads Intelligence System — an automated, doctrine-driven Google Ads advisory engine for a B2B SaaS company selling TMS (Transportation Management System) software to freight forwarders across 80+ countries.

This briefing is the handoff from the strategy phase (Claude.ai) to the build phase (Claude Code). Read it fully before touching any code.

---

## KEY CONTEXT

**Company:** Logistaas — freight forwarder TMS, 80+ countries, long B2B sales cycle (3–12 months)

**Core problem we're solving:**
Google Ads is counting job seekers and wrong-industry companies as "conversions". The algorithm is learning to generate more junk. This system stops that feedback loop and replaces it with qualified pipeline data from HubSpot.

**Live audit findings (April 2026):**
- GCLID (`hs_google_click_id`) IS already captured in HubSpot — confirmed populated
- HubSpot account ID: 142257138
- Google Ads account ID: 3059734490
- MQL statuses confirmed: "OPEN - Connecting", "CLOSED - Job Seeker", etc.
- Deal stages confirmed with real IDs (see config/logistaas_config.yaml)

**Doctrine:** All recommendations governed by Avverros Ads Specialist Doctrine in docs/DOCTRINE.md. Read it.

---

## WHAT'S ALREADY BUILT

- `README.md` — full setup guide
- `docs/DOCTRINE.md` — Claude system prompt (doctrine rules)
- `config/logistaas_config.yaml` — geo tiers, OCT values, thresholds
- `config/patterns.yaml` — intent mismatch pattern library
- `.env.example` — all required environment variables
- `requirements.txt` — all Python dependencies
- `render.yaml` — Render.com deployment with 3 cron jobs
- `.gitignore`
- `connectors/hubspot_pull.py` — HubSpot connector (✅ hardened — retry, 429 handling, logging)
- `connectors/windsor_pull.py` — Windsor.ai connector (✅ hardened — retry, 429/401 handling, logging)
- `connectors/gclid_match.py` — GCLID match engine (✅ built — PR-ADS-001)
- `doctrine/advisor.py` — Claude API integration (complete)
- `scheduler/daily.py` — daily pulse orchestrator (complete)

---

## WHAT NEEDS TO BE BUILT (in order)

### Phase 1 (complete first)
- [x] `connectors/gclid_match.py` — join Windsor clicks + HubSpot contacts via GCLID
- [ ] `connectors/oct_uploader.py` — push HubSpot deal stage changes to Google Ads OCT
- [x] Harden `connectors/hubspot_pull.py` — retry logic, 429 handling, structured logging
- [x] Harden `connectors/windsor_pull.py` — retry logic, 429/401 handling, structured logging

### Phase 2
- [ ] `engine/signal_check.py` — brand vs non-brand split, Ads vs CRM delta
- [ ] `engine/ngram_analysis.py` — decompose search terms, score waste
- [ ] `engine/campaign_classifier.py` — FIX/HOLD/SCALE/CUT classification
- [ ] `engine/lead_quality.py` — MQL vs SQL vs junk scoring

### Phase 3
- [ ] `scheduler/weekly.py` — orchestrate full weekly report
- [ ] `scheduler/monthly.py` — orchestrate monthly strategy
- [ ] Report delivery (email via SendGrid or Slack webhook)

### Phase 4
- [ ] `api/server.py` — FastAPI for on-demand triggers
- [ ] Deploy to Render.com

---

## IMPORTANT FIELD NAMES (confirmed from live HubSpot)

```python
# Contact fields
"hs_google_click_id"          # GCLID — key linking field
"mql_status"                  # "OPEN - Connecting", "CLOSED - Job Seeker" etc.
"hs_analytics_source_data_1"  # Campaign name
"hs_analytics_source_data_2"  # Keyword (e.g. "cargowise", "logisys")
"ip_country"                  # Lead geography
"mql___mdr_comments"          # MDR comments (contains junk signals)

# Deal stage IDs
"qualifiedtobuy"   → Proposal
"334269159"        → In Trials
"326093513"        → Pricing Acceptance
"326093515"        → Invoice Agreement Sent
"326093516"        → Deal Won (primary OCT signal)
"379124201"        → Lost Deal
```

---

## HOW TO GET STARTED

```bash
git clone https://github.com/YOUR_USERNAME/logistaas-ads-intelligence.git
cd logistaas-ads-intelligence
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with real API keys
python connectors/hubspot_pull.py   # Test connection first
```

---

## DOCTRINE RULES (non-negotiable)

1. Never mix Brand and Non-Brand data
2. Never scale during learning phase
3. Never optimise for CPL alone — always CPQL
4. Never assume conversions = revenue without CRM verification
5. FIX/HOLD/SCALE/CUT — every campaign must be classified

Full doctrine: `docs/DOCTRINE.md`
