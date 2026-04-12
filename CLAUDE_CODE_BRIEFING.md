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
- `analysis/advisor.py` — Claude API integration (✅ complete)
- `analysis/core.py` — waste detection, lead quality, campaign truth (✅ complete)
- `scheduler/daily.py` — daily pulse orchestrator (✅ complete)
- `scheduler/weekly.py` — weekly report orchestrator (✅ complete)
- `scheduler/monthly.py` — monthly report orchestrator (✅ complete)
- `scheduler/delivery.py` — SendGrid email delivery (✅ complete)
- `scheduler/run_history.py` — persistent JSONL run history (✅ complete — PR-ADS-012)
- `scripts/healthcheck.py` — preflight env + import validation (✅ complete — PR-ADS-013)
- `Makefile` — manual ops runner (✅ complete — PR-ADS-015)

---

## WHAT NEEDS TO BE BUILT (in order)

### Phase 1 — Remaining
- [ ] Config + pattern hardening (PR-ADS-005)
- [ ] End-to-end test on live environment + first real report (PR-ADS-006)
- [ ] Render deployment — register all 3 cron jobs (PR-ADS-007)
- [ ] `connectors/oct_uploader.py` dry-run (PR-ADS-008, Phase 2 gate)

### Phase 2
- [ ] OCT live activation (PR-ADS-009)

### Phase 3
- [ ] `connectors/negative_pusher.py` — human-approved negative keyword push (PR-ADS-010)

### Phase 4
- [ ] `api/server.py` — FastAPI for on-demand triggers (PR-ADS-011)
- [ ] Frontend dashboard (PR-ADS-016)
- [ ] Meta Ads integration (PR-ADS-017)

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
