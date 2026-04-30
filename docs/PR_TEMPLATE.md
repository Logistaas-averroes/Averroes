# PR Template — Logistaas Ads Intelligence System

Every PR uses one of two templates below. Pick the one that matches your work.

- **Template A — Backend / Logic** — for `connectors/`, `analysis/`, `scheduler/`, `api/`, `config/`, `scripts/`
- **Template B — Frontend** — for `static/` (HTML, CSS, JS)

If a PR touches both backend and frontend, split it into two PRs.

---

## How to use this file

1. Copy the relevant template into the PR description.
2. Fill in every field. No `TBD` allowed at merge time.
3. Run the test commands locally before requesting review.
4. Update `docs/09_REPO_STATE.md` as the **last commit** of every PR.

---

# Template A — Backend / Logic PR

```
📐 Logistaas Ads Intelligence System
PR-ADS-XXX — [Short Title]
Type:    Backend / Logic
Doctrine: Avverros v1.0
Phase:   [1 — Read Only / 2 — OCT / 3 — Actions / 4 — Platform]
```

---

## PR Classification

```
PR Type:      [Feature / Hardening / Config / Fix / Docs]
Module:       [connectors/ / analysis/ / scheduler/ / api/ / config/ / scripts/]
Roadmap ID:   PR-ADS-XXX
Depends On:   [PR-ADS-XXX or Nothing]
Blocks:       [PR-ADS-XXX or Nothing]
PR Stage:     [stabilization / feature-expansion / phase-transition]
```

---

## 1. Problem

*One paragraph. What is missing or broken. Be specific. Reference live data evidence where possible.*

---

## 2. Implementation

### File: `path/to/file.py` — [BUILD NEW / HARDEN / UPDATE / DELETE]

**Responsibility:** One sentence describing what this module is responsible for.

**Changes made:**
- Added X
- Fixed Y
- Updated Z

**Forbidden in this file:**
- No analysis logic in connectors
- No external API calls in analysis modules
- No business logic in schedulers
- No write-back to Google Ads or HubSpot in Phase 1

*(Repeat the block above for each file changed.)*

---

## 3. Contract Impact

```
Data output changed:    None / Yes — describe new schema
Config changed:         None / Yes — list new keys in config/thresholds.yaml or junk_patterns.yaml
API endpoint changed:   None / Yes — describe new contract
Breaking change:        No / Yes — describe migration path
Phase 1 read-only:      Confirmed — no writes to Google Ads or HubSpot
```

---

## 4. Testing

```bash
# Local commands the reviewer can run
python -m connectors.example
python -m analysis.example

# Expected output (paste actual output, not "should print X")
"Pulled 312 contacts"
"GCLID coverage: 87.4%"

# Files that must exist after running
ls data/example.json     # exists, valid JSON
ls outputs/example.md    # exists, non-empty
```

---

## 5. Doctrine Checklist

- [ ] Connectors only fetch and save — no analysis inside
- [ ] Analysis modules only read `data/` — no external API calls
- [ ] Schedulers only orchestrate — no business logic
- [ ] No thresholds hardcoded — all values in `config/thresholds.yaml`
- [ ] No junk patterns hardcoded — all in `config/junk_patterns.yaml`
- [ ] No API keys in code — all from environment variables
- [ ] No `print()` debug statements left in production paths
- [ ] `DICARDED` spelled with one R in all MQL status references
- [ ] `mql___mdr_comments` written with three underscores
- [ ] `data/` and `outputs/` are gitignored — nothing committed there
- [ ] Brand and non-brand data never mixed in any analysis output
- [ ] Phase 1: zero write operations to Google Ads, HubSpot, or any external service

---

## 6. Operational Readiness

```
Logging added:                Yes / No
Error handling for API down:  Yes / No
Rate limit (429) handling:    Yes / No
Env vars documented in .env.example: Yes / No
Config keys documented:       Yes / No
```

---

## 7. Failure Modes

| Failure | Behaviour |
|---------|-----------|
| Windsor.ai 429 rate limit | Exponential backoff, max 3 retries |
| HubSpot API timeout | Logged, returns empty list, downstream proceeds |
| Required env var missing | Healthcheck fails before connector runs |

---

## 8. Post-Merge Verification

```bash
# Commands to verify after deploy
make healthcheck
make validate
python -m scheduler.daily   # exits 0
```

**Success criteria:** *Specific output that proves this works in production.*

**Owner:** Youssef Awwad
**Unblocks:** PR-ADS-XXX — [title]

---

## 9. State Update

The final commit in this PR must update `docs/09_REPO_STATE.md` to reflect:
- Files moved from "Built but Broken" to "Built and Verified"
- Files added under "Built and Verified"
- This PR added under "Current PR Index"

---
---

# Template B — Frontend PR

```
📐 Logistaas Ads Intelligence System
PR-ADS-XXX — [Short Title]
Type:    Frontend
Module:  static/
Phase:   [1 — Read Only / 4 — Platform]
```

---

## PR Classification

```
PR Type:      [Feature / Visual Refresh / Fix / Refactor]
Roadmap ID:   PR-ADS-XXX
Depends On:   [PR-ADS-XXX or Nothing]
Blocks:       Nothing
PR Stage:     [feature-expansion / stabilization]
```

---

## 1. Problem

*One paragraph. What's wrong with the current UI or what new capability is needed.*

---

## 2. Files Changed

| File | Action |
|------|--------|
| `static/index.html` | Replace / Update |
| `static/styles.css` | Replace / Update |
| `static/app.js` | Replace / Update |
| `static/assets/...` | Add / N/A |

---

## 3. Brand Compliance

All frontend PRs must follow `docs/BRAND.md`. Confirm:

- [ ] Sora font loaded from Google Fonts (Bold 700, Regular 400, Light 300)
- [ ] Primary accent: `#129ef5` (Light Sky Blue)
- [ ] Dark surface: `#011931` (Midnight Blue)
- [ ] Accent green: `#00ffa9` (Turquoise) — used only for status / success
- [ ] Light gradient available for hero/login: `#ecf4ff → #129ef5`
- [ ] No DM Sans, Inter, Roboto, system-ui fonts used
- [ ] No purple gradients or generic AI aesthetics
- [ ] Verdict badges follow doctrine colour mapping (SCALE=green, FIX=orange, HOLD=gray, CUT=red)

---

## 4. Pages / Components in Scope

List every page or component this PR adds or modifies.

| Page | Route (JS) | Role Visibility |
|------|-----------|----------------|
| Dashboard | `dashboard` | all |
| Campaigns | `campaigns` | all |
| Lead Quality | `leads` | all |
| Deals | `deals` | all |
| Opportunities | `opportunities` | all |
| Scheduler | `scheduler` | admin shows triggers |

---

## 5. API Endpoints Used

JS only calls existing endpoints. List them and confirm no new endpoints are needed.

```
GET  /auth/me
POST /auth/login
POST /auth/logout
GET  /runs/latest
GET  /reports/latest
GET  /reports/latest/raw
GET  /scheduler/status
GET  /health
GET  /readiness         (admin only)
POST /run/daily         (admin only)
POST /run/weekly        (admin only)
POST /run/monthly       (admin only)
```

---

## 6. What Is NOT Changing

Confirm these are untouched:

- [ ] `api/server.py` — no new endpoints, no changed contracts
- [ ] `api/auth.py` — no auth logic changes
- [ ] `api/scheduler.py` — no scheduler changes
- [ ] All Python modules — frontend PRs are static-only

---

## 7. Frontend Rules

- [ ] No build step — plain HTML, CSS, JS files served by FastAPI `StaticFiles`
- [ ] No npm, no React, no bundlers
- [ ] No external JS frameworks loaded from CDN beyond fonts
- [ ] No `localStorage` for sensitive data — session cookie is the only auth state
- [ ] Mobile-responsive: usable on iPad and laptop, acceptable on phone
- [ ] Accessibility: keyboard navigable, focus visible, semantic HTML

---

## 8. Testing

```bash
# Start the server locally
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000

# Open in browser
open http://localhost:8000

# Manual checklist
# [ ] Login screen renders with brand gradient background
# [ ] Login with valid credentials → dashboard appears
# [ ] All pages reachable via sidebar nav
# [ ] Admin role: run trigger buttons visible
# [ ] Viewer role: run trigger buttons hidden
# [ ] Logout returns to login screen
# [ ] Sora font loaded (check DevTools → Network)
# [ ] No console errors
```

---

## 9. Visual Evidence

Attach screenshots in the PR description:
- Login screen
- Dashboard (admin view)
- Each unique page added/changed
- Mobile breakpoint (~390px wide)

---

## 10. Post-Merge Verification

```bash
# After deploy on Render
curl https://<service>.onrender.com/health   # 200 OK
# Open https://<service>.onrender.com in browser
# Verify all pages render with brand styling
```

**Success criteria:** Every page renders with Sora font, brand colours, and no console errors. All API calls succeed for authenticated users.

**Owner:** Youssef Awwad

---

## 11. State Update

The final commit in this PR must update `docs/09_REPO_STATE.md`:
- Update `static/index.html`, `static/styles.css`, `static/app.js` rows with the PR number
- Add this PR under "Current PR Index"

---
