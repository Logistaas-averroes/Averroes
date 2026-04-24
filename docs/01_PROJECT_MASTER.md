# Logistaas Ads Intelligence System
## Project Master Document

**Version:** 3.0 | **Date:** April 2026 | **Owner:** Youssef Awwad (youssef.awwad@logistaas.com)

---

## What This Is

An automated Google Ads intelligence system for Logistaas — a B2B SaaS company selling Transportation Management System (TMS) software to freight forwarders across 80+ countries.

The system acts as a permanent, always-on analyst that reads data from Google Ads (via Windsor.ai) and HubSpot CRM, identifies where money is being wasted, and produces plain-language findings for human review and decision.

**This is not a reporting tool. It is a signal correction engine.**

Its purpose is to expose the gap between what Google Ads thinks is happening and what is actually happening in the business.

---

## The Core Problem

Google Ads counts a job seeker from Nigeria who fills a form as a "conversion" — identical in value to a freight forwarder from UAE who becomes a $21,870 closed deal. The algorithm then optimises toward finding more people like whoever converted recently. If junk leads dominate, it actively learns to generate more junk.

This is called Conversion Poisoning. It is the primary reason campaigns can look healthy on paper while pipeline quality deteriorates.

Logistaas has confirmed this pattern in live data:
- 28% of paid contacts are DICARDED (no viable lead action)
- 18% are confirmed bad product fit or wrong industry
- Job seekers, students, fraud patterns, and Spanish/Arabic free-intent queries are actively triggering campaigns
- The algorithm has no feedback from HubSpot — it only knows about form fills

**The system's job: expose this gap every week, in plain language, grounded in real data.**

---

## Infrastructure

| Tool | Purpose | Cost/month |
|------|---------|-----------|
| Claude Pro (Anthropic) | Analysis engine | $20 |
| GitHub | Version control | $10 |
| Windsor.ai Basic | Google Ads data | $23 |
| Render.com | Hosting + scheduler | $7 |
| **Total** | | **$60/month** |

---

## Account Details (Confirmed)

| Item | Value |
|------|-------|
| HubSpot Account ID | 142257138 |
| Google Ads Account ID | 3059734490 |
| HubSpot Owner | Youssef Awwad |
| Timezone | Asia/Amman (UTC+3) |
| Active paid contacts (3 weeks) | ~2,582 |
| Active pipeline deals | 557 |
| GCLID coverage | 100% |

---

## The Four Phases

### Phase 1 — Signal Intelligence (Current)
**Duration:** 4–8 weeks minimum before advancing
**What it does:** Reads data, finds waste, reports findings. Nothing more.
**Current State: Built, entering stabilization**
**Outputs:** Weekly report answering "where am I losing money?"
**No write-back to Google Ads. No automation. Human decides everything.**

> **Note:** All Phase 1 modules are written. The system is not yet validated end-to-end.
> No automation is active or permitted until validation is confirmed.
> See `docs/09_REPO_STATE.md` for current file-level status.

### Phase 2 — Signal Correction
**Prerequisite:** Phase 1 validated for 4+ weeks. At least 3 recommendations verified correct.
**What it adds:** OCT uploader — feeds confirmed deal values back to Google Ads so Smart Bidding learns from revenue, not form fills
**Still no automated changes. Human approves every OCT activation.**

### Phase 3 — Active Defence
**Prerequisite:** Phase 2 running cleanly for 4+ weeks
**What it adds:** Negative keyword push — system-generated negative lists pushed to Google Ads after human approval. Geo bid adjustment recommendations.
**Human approval gate on every push. Full audit trail.**

### Phase 4 — Intelligence Platform
**Prerequisite:** Phase 3 proven
**What it adds:** Frontend war room dashboard. Meta Ads integration. Gamma presentation reports. Full consultant-grade UI.
**The system becomes the primary interface for all paid media intelligence at Logistaas.**

---

## Logistaas Context

**Product:** Cloud-native TMS for freight forwarders. Not shippers, not retailers, not manufacturers.

**Why this distinction matters:** The Google Ads system is actively serving ads to shippers, job seekers, students, and wrong industries. Every one of these contacts is teaching the algorithm the wrong lesson.

**Sales cycle:** 3–12 months. Any bidding strategy optimising on form fills is mathematically wrong for this business.

**Confirmed won deals (April 2026):** Al-Ahmadi Logistics ($2,400), Hero Freight ($2,580), Offshore Freight ($8,932), Beyond3PL ($21,870), Akzent ($51,366), and others.

**The revenue is real. The signal reaching the algorithm is corrupted.**
