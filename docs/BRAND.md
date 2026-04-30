# Logistaas Brand Reference
## Single source of truth for all visual design decisions

This file is the brand authority. Every frontend PR must comply with it.
Source: `Colors_Typography_Guide_2025.pdf` (official brand guide).

---

## 1. Colour Tokens

### Light Sky Blue Theme (default for the dashboard)
The primary theme. Used for the main app surface, cards, and most UI.

| Token | Hex | Use |
|-------|-----|-----|
| `--brand-sky-bg` | `#ecf4ff` | Light background fills, hero start, hover states |
| `--brand-sky-accent` | `#129ef5` | Primary buttons, links, active nav, brand accent |
| `--brand-sky-gradient` | `linear-gradient(135deg, #ecf4ff 0%, #129ef5 100%)` | Hero / login backgrounds only |

### Midnight Blue & Turquoise Theme (sidebar + dark surfaces)

| Token | Hex | Use |
|-------|-----|-----|
| `--brand-midnight` | `#011931` | Sidebar background, dark cards, headers on dark |
| `--brand-turquoise` | `#00ffa9` | Status indicators (online, success), accent on dark surfaces |
| `--brand-dark-gradient` | `linear-gradient(135deg, #00ffa9 0%, #011931 100%)` | Dark hero panels only |

### Functional / System Colours
Used for verdicts, alerts, and status badges. These are the **only** non-brand colours allowed.

| Token | Hex | Use |
|-------|-----|-----|
| `--c-success` | `#15803d` | SCALE verdict, success states |
| `--c-success-bg` | `#dcfce7` | Success badge fill |
| `--c-warning` | `#b45309` | FIX verdict, warning states |
| `--c-warning-bg` | `#fef3c7` | Warning badge fill |
| `--c-danger` | `#b91c1c` | CUT verdict, error states |
| `--c-danger-bg` | `#fee2e2` | Danger badge fill |
| `--c-neutral` | `#6e6e73` | HOLD verdict, muted text |
| `--c-neutral-bg` | `#f0f0f5` | Neutral badge fill |

### Surface / Text Tokens

| Token | Hex | Use |
|-------|-----|-----|
| `--surface-page` | `#f5f8ff` | Page background (slight sky tint) |
| `--surface-card` | `#ffffff` | Card surfaces |
| `--surface-alt` | `#fbfbfd` | Alt rows, table headers |
| `--border` | `#e0e6ef` | Default borders |
| `--border-strong` | `#cbd5e1` | Strong borders, hover states |
| `--text-primary` | `#011931` | Headings, primary text (uses brand midnight) |
| `--text-secondary` | `#4b5563` | Body text |
| `--text-muted` | `#94a3b8` | Captions, hints |

---

## 2. Forbidden Colours

Never use these in any new component:

- ❌ Pure black (`#000000`) — use `#011931` (brand midnight) instead
- ❌ Pure white on dark — use `#ecf4ff` for soft contrast
- ❌ Apple system blue (`#0071e3`, `#007aff`) — use `#129ef5`
- ❌ Generic Tailwind blues (`#3b82f6`, `#2563eb`)
- ❌ Purple gradients of any kind
- ❌ Neon / saturated greens other than `#00ffa9`

---

## 3. Typography

### Font Family
**Sora** — and only Sora. No fallbacks beyond a generic sans-serif.

```css
@import url('https://fonts.googleapis.com/css2?family=Sora:wght@300;400;500;600;700&display=swap');

:root {
  --font-sans: 'Sora', system-ui, -apple-system, sans-serif;
}

body { font-family: var(--font-sans); }
```

### Weights Used (per brand guide)

| Weight | CSS | When to use |
|--------|-----|-------------|
| Light (300) | `font-weight: 300` | Captions, meta text, subtle decoration |
| Regular (400) | `font-weight: 400` | Body text, default UI |
| Medium (500) | `font-weight: 500` | Emphasised body, sub-labels |
| Semibold (600) | `font-weight: 600` | Section headings, button labels |
| Bold (700) | `font-weight: 700` | Page titles, brand wordmark, headlines |

### Type Scale

| Element | Size | Weight | Line height |
|---------|------|--------|-------------|
| Page title (h1) | `28px` | 700 | 1.2 |
| Section heading (h2) | `20px` | 600 | 1.3 |
| Subsection (h3) | `16px` | 600 | 1.4 |
| Body text | `14px` | 400 | 1.6 |
| Small text / labels | `12px` | 500 | 1.5 |
| Micro / caps labels | `11px` | 600 | 1.4 (uppercase, letter-spacing 0.06em) |
| KPI / large metric | `30px` | 700 | 1 |

### Forbidden Typography

- ❌ DM Sans, Inter, Roboto, Arial, system-ui as primary font
- ❌ Italic styles (Sora is never used in italic per brand guide)
- ❌ ALL CAPS for sentence content (only for micro labels with letter-spacing)
- ❌ Font weights 100, 200 (too thin for the design)
- ❌ Underlined links (use colour only)

---

## 4. Spacing & Layout

| Token | Value | Use |
|-------|-------|-----|
| `--space-1` | `4px` | Icon-text gap, tight inline |
| `--space-2` | `8px` | Default small gap |
| `--space-3` | `12px` | Card padding (compact) |
| `--space-4` | `16px` | Standard spacing |
| `--space-5` | `20px` | Card padding (default) |
| `--space-6` | `24px` | Page padding, large gaps |
| `--space-8` | `32px` | Section spacing |

| Radius | Value | Use |
|--------|-------|-----|
| `--radius-sm` | `6px` | Badges, chips, small buttons |
| `--radius-md` | `10px` | Buttons, inputs |
| `--radius-lg` | `14px` | Cards, modals |
| `--radius-xl` | `20px` | Hero panels |

| Shadow | Value | Use |
|--------|-------|-----|
| `--shadow-sm` | `0 1px 2px rgba(1, 25, 49, 0.06)` | Default card |
| `--shadow-md` | `0 4px 12px rgba(1, 25, 49, 0.08)` | Hover, modals |
| `--shadow-lg` | `0 12px 32px rgba(1, 25, 49, 0.12)` | Floating elements |

---

## 5. Component Standards

### Sidebar
- Background: `var(--brand-midnight)` (#011931)
- Width: 240px on desktop, collapsed icon-only on mobile
- Active nav item: `var(--brand-sky-accent)` left border (3px) + soft `rgba(18, 158, 245, 0.12)` background
- Inactive text: `rgba(236, 244, 255, 0.65)`
- Active text: `#ffffff`

### Buttons

```css
/* Primary */
background: var(--brand-sky-accent);
color: #ffffff;
font-weight: 600;
padding: 10px 20px;
border-radius: var(--radius-md);

/* Secondary */
background: transparent;
color: var(--brand-sky-accent);
border: 1px solid var(--brand-sky-accent);

/* Ghost */
background: transparent;
color: var(--text-secondary);
```

### Cards
```css
background: var(--surface-card);
border: 1px solid var(--border);
border-radius: var(--radius-lg);
padding: var(--space-5);
box-shadow: var(--shadow-sm);
```

### Verdict Badges (mandatory mapping)

| Verdict | Background | Text | Border |
|---------|-----------|------|--------|
| `SCALE` | `--c-success-bg` | `--c-success` | `1px solid rgba(21, 128, 61, 0.2)` |
| `HOLD` | `--c-neutral-bg` | `--c-neutral` | `1px solid rgba(110, 110, 115, 0.15)` |
| `FIX` | `--c-warning-bg` | `--c-warning` | `1px solid rgba(180, 83, 9, 0.2)` |
| `CUT` | `--c-danger-bg` | `--c-danger` | `1px solid rgba(185, 28, 28, 0.2)` |

---

## 6. Logo Usage

The Logistaas wordmark uses Sora Bold. The mark sits next to the wordmark with 12px gap.
On dark surfaces (sidebar, login hero), use the white version.
On light surfaces, use the midnight (`#011931`) version.

Never:
- Tint the logo with non-brand colours
- Use the logo smaller than 24px tall
- Place the logo on a busy or low-contrast background

---

## 7. Iconography

Use inline SVG icons only. No emoji in the UI chrome.

- Stroke width: 1.6px
- Stroke linecap: round
- Stroke linejoin: round
- Default size: 18px (nav, buttons), 20px (section headers), 14px (inline)
- Colour: `currentColor` so icons inherit text colour

Source recommendation: Lucide icon set (CC0). Copy SVG markup directly — do not load runtime libraries.

---

## 8. Compliance Check

Every frontend PR must answer "yes" to all of these:

- [ ] Only Sora font is loaded
- [ ] Only brand colours + functional system colours are used
- [ ] No purple gradients, no DM Sans, no Inter, no system-ui
- [ ] Verdict badges use the mandatory colour mapping
- [ ] Sidebar uses midnight blue, accent buttons use sky blue
- [ ] Logo follows usage rules
- [ ] All icons are inline SVG with stroke 1.6px

If any box is unchecked, the PR is not brand-compliant and must be revised.
