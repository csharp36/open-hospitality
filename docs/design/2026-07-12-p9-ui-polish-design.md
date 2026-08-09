# USALI Engine — P9 Portal UI Polish Design

**Date:** 2026-07-12
**Status:** Approved for planning
**Depends on:** P0–P8 (merged; portal at `frontend/` with 5 pages, 73 vitest + 6 Playwright tests)

## Goal

Take the portal from "developer site" to a polished internal tool — a pure visual pass:
design tokens, typography, shared UI primitives, refined chrome, and dark mode. Zero
behavior changes, zero backend changes, zero new features.

## Decisions locked with the user (chosen via visual mockups)

1. **Visual direction: Modern SaaS** — Inter, indigo accent, soft shadows, rounded
   cards, pill badges (the Linear/Stripe internal-tool look). Rejected: classic-ledger
   serif and dense-terminal dark alternatives.
2. **Chrome: refined light top nav** — today's structure executed well (brand mark,
   active-tab pill, crisp border). Rejected: dark anchor bar, left sidebar.
3. **Dark mode: IN scope** — token-swap variant with a nav toggle.
4. **Print stylesheet: deferred** (noted for a later phase; the CPA-facing print/PDF
   pass is the natural next add-on).
5. **Tier-1 only:** no component library (shadcn stays a future option), no density
   modes, no redesign of flows.

## Architecture

```
frontend/src/index.css        @theme tokens (semantic CSS variables) + .dark remapping
frontend/src/components/ui.tsx NEW: Card, PageHeader, Badge, shared table style constants
frontend/src/Layout.tsx        refined top nav + dark-mode toggle
frontend/src/lib/theme.ts      NEW: dark-mode init/toggle (prefers-color-scheme + localStorage)
pages/* + components/*         restyled onto tokens/primitives (no logic changes)
```

### 1. Token foundation (`index.css`)

Tailwind v4 CSS-first theming: an `@theme` block defining **semantic** variables —
components never reference raw palette steps:

- Surfaces: `--color-surface` (page), `--color-surface-raised` (cards),
  `--color-surface-sunken` (wells/zebra), `--color-border`, `--color-border-strong`.
- Text: `--color-text`, `--color-text-muted`, `--color-text-faint`.
- Accent: `--color-accent` (indigo-600 family), `--color-accent-soft` (pill/hover bg).
- Status (badges/notices, used by QBO + coverage + upload): `--color-ok`, `--color-warn`,
  `--color-danger`, `--color-info` + matching `-soft` background variants.
- Typography: Inter via the `@fontsource-variable/inter` npm package (self-hosted, no
  CDN, offline-safe); `font-feature-settings`/`font-variant-numeric: tabular-nums` on
  all amount columns.
- Radii (`--radius-card`, `--radius-control`, pill) and two shadow levels
  (`--shadow-card`, `--shadow-overlay`).

Light values match the approved mockup (white cards on `#f8fafc`, `#0f172a` text,
indigo accents).

### 2. Dark mode

- `.dark` on `<html>` remaps the same semantic variables to a slate-900 palette
  (surfaces `#0f172a`/`#1e293b`, text `#e2e8f0`, indigo-400 accent); status colors get
  dark-tuned soft backgrounds so amber/red/green badges stay readable.
- `lib/theme.ts`: `initTheme()` (localStorage → `prefers-color-scheme` fallback) called
  from `main.tsx` before render (no flash); `toggleTheme()` flips the class + persists.
- Toggle button in the nav (right side, sun/moon glyph, `aria-label="Toggle dark mode"`).

### 3. Shared UI primitives (`components/ui.tsx`)

Consolidates what is currently copy-pasted across four pages plus DrillPanel:

- `Card` (raised surface, radius, shadow, padding) and `PageHeader` (title + optional
  subtitle/actions row).
- `Badge({ tone: 'ok' | 'warn' | 'danger' | 'info' | 'neutral' })` — the QBO status
  badges (pushed/stale/failed/already-pushed/not-pushed), coverage counts, and upload
  cards all route through it. Tone names appear in the class strings so the existing
  className-regex tests (`/red/`, `/amber/`) keep passing or are updated deliberately.
- Table style constants: `tableClass`, `headCellClass`, `cellClass`, `amountCellClass`
  (right-aligned, tabular-nums) — single source of truth.

### 4. Chrome (`Layout.tsx`)

Refined light top nav per the approved mockup: brand mark ("◆ USALI Portal" with the
accent on "Portal"), nav links with an active pill (TanStack Router active props),
bottom border, dark-mode toggle. Content container gets a consistent `max-w` and
horizontal padding; every page adopts the `PageHeader` pattern.

### 5. Page passes (restyle only — no logic, markup changes only where styling demands)

- **SOS/Statement**: financial typography — uppercase section labels in muted text,
  hairline rules between lines, heavier rules above totals, the emphasized dark
  TOTAL OPERATING REVENUE bar from the mockup, tabular-nums amounts, row hover on
  drillable lines (accent text like today).
- **DrillPanel + ConfirmPushDialog**: overlay dimming, `--shadow-overlay`, card radius;
  reconciliation/status badges via `Badge`.
- **Pickers/forms** (PickerBar, MonthPickerBar, Upload): consistent control styling
  (radius, border, focus ring in accent), styled drop zone (dashed border card, accent
  highlight on drag-over — same states as today).
- **Reports/QBO/Coverage**: card-per-section layout, shared table styles, `Badge`
  everywhere a status renders.

### 6. Testing & gates

- Existing suites must stay green: 73 vitest + 6 Playwright use role/label locators and
  are styling-agnostic; the few className-regex assertions survive because tone names
  keep semantic color words in class strings — any that still break are updated
  deliberately (never deleted, never weakened).
- New tests: `theme.ts` unit tests (init from localStorage / media query, toggle
  persists, class applied); one Layout test asserting the toggle button exists and
  flips the root class.
- Gates: vitest, `tsc --noEmit`, oxlint, build, full backend pytest (must be untouched),
  Playwright e2e cold.
- Human visual review: the portal runs against the seeded e2e backend
  (`scripts/e2e_backend.py` + `npm run dev`) for a final look at every page in both
  modes before merge.

## Out of scope (explicit)

- Print stylesheet (deferred — next candidate add-on).
- Component library adoption (shadcn/ui), density modes, landing page, charts.
- Any behavior, API, or backend change; any new page or feature.
- The e2e Playwright specs gain no new tests (existing ones must simply keep passing).

## Definition of done

All five pages + both dialogs restyled onto the token system; dark mode toggles and
persists with readable status colors in both modes; nav matches the approved mockup;
table/badge/card styles have a single source of truth (no duplicated style constants);
all gates green including Playwright cold; visual sign-off from the user on the running
portal in both modes.
