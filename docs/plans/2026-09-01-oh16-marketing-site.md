# OH-16 marketing site — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the static marketing site at `marketing/` — three pages that
explain the platform to a hotel operator and send them to `/try` — per
[the design](../design/2026-09-01-oh16-marketing-front-door-design.md).

**Architecture:** An Astro package independent of `frontend/`, building to
static HTML with no client JavaScript. Two read-only couplings to the rest of
the repo: `shared/brand.css` (the design tokens, one file with two readers) and
`.github/roadmap.yml` (product status, read at build time by a guard that fails
the build when featured work ships).

**Tech Stack:** Astro 7.2, Tailwind CSS 4.3 via `@tailwindcss/vite`,
`@astrojs/sitemap` 3.7, `yaml` 2.9, Vitest 4 for the guard tests, Playwright for
one smoke test. Node ≥ 22.12 (this machine runs 22.21.1).

**Deploy is a separate plan:** `2026-09-01-oh16-marketing-deploy.md`. Everything
here runs and is testable locally with no Cloudflare account.

**Verified before writing this plan** (in a throwaway probe, not from memory):
Astro 7.2.10 accepts `output: 'static'` and `site`; Tailwind v4 merges a second
`@theme` block imported from outside the project root, emitting both the custom
property and the utility class; `@astrojs/sitemap` emits `sitemap-index.xml`;
and `yaml` parses `.github/roadmap.yml` into 21 entries.

---

## File structure

| File | Responsibility |
|---|---|
| `shared/brand.css` | The marketing design tokens. Imported by the app and the site. Sole definition. |
| `frontend/src/index.css` | Modified: the marketing token block is removed and imported instead. |
| `marketing/package.json` | The site's own package, scripts, and dependencies. |
| `marketing/astro.config.mjs` | Static output, `site` from env, sitemap, Tailwind. |
| `marketing/src/lib/roadmap.ts` | Reads the catalogue; throws when a featured id is missing or shipped. |
| `marketing/src/lib/roadmap.test.ts` | The guard's tests. |
| `marketing/src/lib/site.ts` | `APP_ORIGIN` and the CTA href. One place, so the host stays swappable. |
| `marketing/src/layouts/Base.astro` | `<head>` metadata, nav, footer. Every page's chrome. |
| `marketing/src/components/*.astro` | One component per homepage section. |
| `marketing/src/pages/index.astro` | Home. |
| `marketing/src/pages/pricing.astro` | Pricing philosophy. |
| `marketing/src/pages/your-data.astro` | Data posture. |
| `marketing/public/robots.txt` | Allows crawling, points at the sitemap. |
| `frontend/public/robots.txt` | New: keeps the app from competing with the site. |
| `scripts/gen_marketing_assets.py` | Renders the "before" WebP and draws the OG card. |
| `marketing/public/` | Generated images plus the favicon copied from the app. |
| `marketing/tests/build.test.ts` | Post-build assertions: links resolve, metadata present. |
| `marketing/e2e/cta.spec.ts` | One Playwright smoke test on the CTA. |

---

## Task 1: Extract the brand tokens into one shared file

The marketing tokens currently sit inside the `@theme` block of
`frontend/src/index.css` at lines 90–100, under a "Marketing front door"
comment. They become `shared/brand.css`, which both stylesheets import.

Tailwind v4 merges multiple `@theme` blocks, so the shared file carries its own
`@theme` wrapper. `@import` must precede other rules, so it goes directly after
the two imports already at the top of `index.css`.

**Files:**
- Create: `shared/brand.css`
- Modify: `frontend/src/index.css:1-5` (add import), `:90-100` (remove block)

- [ ] **Step 1: Create the shared token file**

```css
/* shared/brand.css
 *
 * The marketing skin: a warm hospitality identity, deliberately separate from
 * the app's indigo so the product palette is untouched. Serif display,
 * terracotta accent, monospace numbers. Resolved in Track A §7.
 *
 * Imported by BOTH frontend/src/index.css (for the public /try page) and
 * marketing/src/styles/global.css. One definition with two readers, so the
 * public front door and the marketing site cannot drift apart.
 *
 * A second @theme block is how Tailwind v4 merges tokens from another file;
 * `npm run build` in either package emits --color-brand-canvas and the
 * .bg-brand-canvas utility from this file.
 */
@theme {
  --font-display: Georgia, "Times New Roman", serif;
  --color-brand-canvas: oklch(96.6% 0.012 79);
  --color-brand-surface: oklch(98.6% 0.008 79);
  --color-brand-ink: oklch(31% 0.03 47);
  --color-brand-ink-muted: oklch(52% 0.03 60);
  --color-brand-line: oklch(89% 0.02 74);
  --color-brand-accent: oklch(58% 0.13 42);
  --color-brand-accent-soft: oklch(94% 0.03 55);
}
```

- [ ] **Step 2: Import it from the frontend stylesheet**

In `frontend/src/index.css`, the file currently begins:

```css
@import "tailwindcss";
@import "@fontsource-variable/inter";
```

Add a third import immediately after them:

```css
@import "tailwindcss";
@import "@fontsource-variable/inter";
@import "../../shared/brand.css";
```

- [ ] **Step 3: Delete the moved block**

Remove lines 90–100 of `frontend/src/index.css` — the comment beginning
`/* --- Marketing front door (public /try)` through
`--color-brand-accent-soft: oklch(94% 0.03 55);` inclusive. Leave the closing
`}` of the `@theme` block in place.

- [ ] **Step 4: Verify the tokens survive the move**

Run:

```bash
cd frontend && npm run build && \
  grep -c "color-brand-canvas" dist/assets/*.css
```

Expected: a count of 1 or more. A count of 0 means the import did not resolve —
check the relative path from `frontend/src/` to `shared/`.

- [ ] **Step 5: Verify the app still renders**

Run: `cd frontend && npm test`
Expected: PASS, no new failures. The suite is jsdom and does not compile CSS, so
this proves the move broke no component — Step 4 is what proves the CSS.

- [ ] **Step 6: Commit**

```bash
git add shared/brand.css frontend/src/index.css
git commit -m "refactor(oh16): brand tokens become one file with two readers

The marketing skin lived inside the app's stylesheet. The marketing site
needs the same tokens, and a copy would drift. shared/brand.css is now the
sole definition; frontend/src/index.css imports it."
```

---

## Task 2: Scaffold the marketing package

**Files:**
- Create: `marketing/package.json`, `marketing/astro.config.mjs`,
  `marketing/tsconfig.json`, `marketing/.gitignore`,
  `marketing/src/styles/global.css`, `marketing/src/pages/index.astro`

- [ ] **Step 1: Create `marketing/package.json`**

```json
{
  "name": "marketing",
  "type": "module",
  "private": true,
  "version": "0.0.0",
  "engines": { "node": ">=22.12.0" },
  "scripts": {
    "dev": "astro dev",
    "build": "astro build",
    "preview": "astro preview",
    "test": "vitest run",
    "e2e": "playwright test"
  },
  "dependencies": {
    "@astrojs/sitemap": "^3.7.4",
    "@tailwindcss/vite": "^4.3.3",
    "astro": "^7.2.10",
    "tailwindcss": "^4.3.3",
    "yaml": "^2.9.0"
  },
  "devDependencies": {
    "@playwright/test": "^1.62.1",
    "vitest": "^4.1.11"
  }
}
```

- [ ] **Step 2: Create `marketing/astro.config.mjs`**

```js
// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

// SITE_URL drives canonical tags and the sitemap; APP_ORIGIN (see src/lib/site.ts)
// drives every CTA. Both are env with defaults so the site is host-agnostic —
// landing on a different domain is a config change, not a rewrite.
export default defineConfig({
  site: process.env.SITE_URL ?? 'https://oh.mandati.ai',
  output: 'static',
  integrations: [sitemap()],
  vite: { plugins: [tailwindcss()] },
});
```

- [ ] **Step 3: Create `marketing/tsconfig.json`**

```json
{
  "extends": "astro/tsconfigs/strict",
  "include": [".astro/types.d.ts", "**/*"],
  "exclude": ["dist"]
}
```

- [ ] **Step 4: Create `marketing/.gitignore`**

```
dist/
.astro/
node_modules/
test-results/
```

- [ ] **Step 5: Create `marketing/src/styles/global.css`**

```css
@import "tailwindcss";
@import "../../../shared/brand.css";
```

- [ ] **Step 6: Create a placeholder `marketing/src/pages/index.astro`**

Replaced in Task 6. It exists so the build has a route and Step 8 can prove the
toolchain works before any real content depends on it.

```astro
---
import '../styles/global.css';
---
<html lang="en">
  <head><meta charset="utf-8" /><title>Open Hospitality</title></head>
  <body class="bg-brand-canvas text-brand-ink font-display">
    <h1 class="text-brand-accent">Scaffold</h1>
  </body>
</html>
```

- [ ] **Step 7: Install**

Run: `cd marketing && npm install`
Expected: completes with no errors.

- [ ] **Step 8: Verify the toolchain end to end**

Run:

```bash
cd marketing && npm run build && \
  grep -o "bg-brand-canvas" dist/index.html && \
  grep -ro "\-\-color-brand-canvas:[^;]*;" dist/_astro/*.css && \
  ls dist/sitemap-index.xml
```

Expected: prints `bg-brand-canvas`, then the token definition, then the sitemap
path. All three must appear — together they prove Astro built, Tailwind read the
shared tokens across package boundaries, and the sitemap integration ran.

- [ ] **Step 9: Commit**

```bash
git add marketing/
git commit -m "build(oh16): scaffold the marketing Astro package

Static output, Tailwind v4 reading shared/brand.css across the package
boundary, sitemap integration. No content yet."
```

---

## Task 3: The roadmap guard

The forward band writes its own copy but never its own status. The guard makes
the one rot that matters — a feature ships and the site keeps calling it
upcoming — into a failed build.

**Files:**
- Create: `marketing/src/lib/roadmap.ts`, `marketing/src/lib/roadmap.test.ts`

- [ ] **Step 1: Write the failing tests**

`marketing/src/lib/roadmap.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { FEATURED, featuredEntries, readCatalogue, type RoadmapEntry } from './roadmap'

function catalogue(entries: Partial<RoadmapEntry>[]): Map<string, RoadmapEntry> {
  return new Map(
    entries.map((e) => [
      e.id!,
      { id: e.id!, title: e.title ?? 't', summary: e.summary ?? 's', status: e.status ?? 'planned' },
    ]),
  )
}

describe('featuredEntries', () => {
  it('returns the site copy joined to the catalogue status', () => {
    const out = featuredEntries(catalogue([{ id: 'OH-11', status: 'planned' }]), [
      { id: 'OH-11', heading: 'Portfolio roll-up', blurb: 'Every property side by side.' },
    ])
    expect(out).toEqual([
      { id: 'OH-11', heading: 'Portfolio roll-up', blurb: 'Every property side by side.', status: 'planned' },
    ])
  })

  it('throws when a featured id is absent from the catalogue', () => {
    expect(() =>
      featuredEntries(catalogue([{ id: 'OH-11' }]), [{ id: 'OH-99', heading: 'h', blurb: 'b' }]),
    ).toThrow(/OH-99/)
  })

  it('throws when a featured item has shipped, so the copy cannot outlive it', () => {
    expect(() =>
      featuredEntries(catalogue([{ id: 'OH-11', status: 'shipped' }]), [
        { id: 'OH-11', heading: 'h', blurb: 'b' },
      ]),
    ).toThrow(/shipped/)
  })
})

describe('the real catalogue', () => {
  // This is the guard itself: it runs against .github/roadmap.yml, so CI fails
  // when a featured item ships or is renamed out of the file.
  it('supports every entry the site features', () => {
    expect(() => featuredEntries(readCatalogue(), FEATURED)).not.toThrow()
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd marketing && npx vitest run src/lib/roadmap.test.ts`
Expected: FAIL — `Failed to resolve import "./roadmap"`.

- [ ] **Step 3: Implement the module**

`marketing/src/lib/roadmap.ts`:

```ts
import { parse } from 'yaml'

import roadmapYaml from '../../../.github/roadmap.yml?raw'

export type RoadmapEntry = {
  id: string
  title: string
  summary: string
  status: string
}

export type FeaturedCopy = {
  id: string
  heading: string
  blurb: string
}

/**
 * What the forward band shows, and the words it uses. Status is deliberately
 * absent — it comes from the catalogue, so this list and the product cannot
 * disagree. featuredEntries() below is where that is enforced.
 */
export const FEATURED: FeaturedCopy[] = [
  {
    id: 'OH-11',
    heading: 'Portfolio roll-up',
    blurb: 'Every property side by side, ranked, with the portfolio total on top.',
  },
  {
    id: 'OH-15',
    heading: 'Performance alerts',
    blurb: "A daily and weekly flash when a number moves in a way you'd want to know about.",
  },
  {
    id: 'OH-8',
    heading: 'Budget vs. actual',
    blurb: 'Import the budget, see variance by department, every month.',
  },
  {
    id: 'OH-22',
    heading: 'Direct PMS integrations',
    blurb: 'Connect your property management system directly, with no file to send.',
  },
]

/**
 * The catalogue the repo already keeps: .github/roadmap.yml.
 *
 * Imported as text via Vite's `?raw` rather than read from disk. A path
 * resolved at RUNTIME from `import.meta.url` breaks here: `astro build`
 * bundles this module into dist/.prerender/chunks/, a different depth than
 * the source, so the relative climb misses the repo root and throws ENOENT
 * before featuredEntries() reaches its shipped-status check -- which made the
 * guard unable to fire at all. `?raw` is resolved by Vite against the SOURCE
 * location while building the module graph, so relocation cannot break it,
 * and the YAML is inlined with no runtime filesystem access.
 */
export function readCatalogue(source: string = roadmapYaml): Map<string, RoadmapEntry> {
  const doc = parse(source) as { roadmap?: RoadmapEntry[] }
  if (!Array.isArray(doc?.roadmap)) {
    throw new Error(
      '.github/roadmap.yml does not have a top-level "roadmap:" list — check the file is well-formed.',
    )
  }
  return new Map(doc.roadmap.map((entry) => [entry.id, entry]))
}

/**
 * Join the site's copy to the catalogue's status, refusing both ways it can lie.
 *
 * Throwing here fails `astro build`, because the forward band imports this in
 * page frontmatter. That is the intent: a shipped feature must be promoted out
 * of "shipping next" before the site can build again.
 */
export function featuredEntries(
  catalogue: Map<string, RoadmapEntry>,
  featured: FeaturedCopy[] = FEATURED,
): (FeaturedCopy & { status: string })[] {
  return featured.map((copy) => {
    const entry = catalogue.get(copy.id)
    if (!entry) {
      throw new Error(
        `${copy.id} is featured on the marketing site but absent from .github/roadmap.yml. ` +
          `Add the entry, or remove it from FEATURED in marketing/src/lib/roadmap.ts.`,
      )
    }
    if (entry.status === 'shipped') {
      throw new Error(
        `${copy.id} ("${entry.title}") has shipped, but the marketing site still lists it ` +
          `under "shipping next". Move it into the page proper and remove it from FEATURED.`,
      )
    }
    return { ...copy, status: entry.status }
  })
}
```

- [ ] **Step 4: Run the tests**

Run: `cd marketing && npx vitest run src/lib/roadmap.test.ts`
Expected: three tests PASS, and `the real catalogue` FAILS with
`OH-22 is featured on the marketing site but absent from .github/roadmap.yml`.

That failure is correct — Task 4 adds the entry. Do not weaken the guard to make
it pass.

**Why `?raw` and not `readFileSync`:** the first version of this used
`new URL('../../../.github/roadmap.yml', import.meta.url)`. Vitest runs the
source module, so all four tests passed. `astro build` runs a bundled copy at a
different depth, so it threw ENOENT — and because that throw happened before the
status check, the guard produced a path error instead of the message it exists to
produce. **Passing unit tests said nothing about the behavior that mattered.**
Task 6's deliberate break-the-build step is what exposed it; keep that step.

- [ ] **Step 5: Commit**

```bash
git add marketing/src/lib/roadmap.ts marketing/src/lib/roadmap.test.ts
git commit -m "feat(oh16): roadmap guard for the forward band

The site writes its own copy but never its own status. A featured item that
ships, or leaves the catalogue, now fails the build instead of leaving the
marketing page advertising something already delivered.

The real-catalogue test fails until OH-22 exists; that is the guard working."
```

---

## Task 4: Open OH-22, and resync the OH-17 drift

**Files:**
- Modify: `.github/roadmap.yml`, `docs/ROADMAP.md:411`

- [ ] **Step 1: Add OH-22 to the catalogue**

Append to `.github/roadmap.yml`, after the `OH-21` entry, matching the
surrounding two-space indentation:

```yaml
  - id: OH-22
    title: Direct PMS integrations
    summary: >
      Pull data directly from a property management system rather than
      ingesting an exported report. Distinct from OH-2, which extends the
      existing file-based path to more report shapes.
    status: planned
    tags: [ingestion, pms, integrations]
```

- [ ] **Step 2: Correct the OH-17 status drift**

`docs/ROADMAP.md:411` reads:

```
| **OH-17** | Connect your own accounting and payroll accounts | `planned` | §2.1 |
```

`.github/roadmap.yml` says `shipped`, and the integrations page merged in #113.
Change `planned` to `shipped`:

```
| **OH-17** | Connect your own accounting and payroll accounts | `shipped` | §2.1 |
```

- [ ] **Step 3: Run the guard**

Run: `cd marketing && npx vitest run src/lib/roadmap.test.ts`
Expected: all four tests PASS, including `the real catalogue`.

- [ ] **Step 4: Commit**

```bash
git add .github/roadmap.yml docs/ROADMAP.md
git commit -m "docs(oh16): open OH-22, resync the OH-17 status

OH-22 (direct PMS integrations) is what the marketing site's forward band
promises; it existed nowhere in the catalogue, and OH-2 is scoped to file
shapes rather than connectors.

OH-17 read 'planned' in ROADMAP.md and 'shipped' in roadmap.yml — the same
drift that document already records for OH-18."
```

---

## Task 5: The base layout and its metadata

**Files:**
- Create: `marketing/src/lib/site.ts`, `marketing/src/layouts/Base.astro`

- [ ] **Step 1: Create `marketing/src/lib/site.ts`**

```ts
/**
 * The app the site sends visitors to. One constant, so moving the app host or
 * the marketing host is a config change rather than a search-and-replace.
 */
export const APP_ORIGIN = import.meta.env.APP_ORIGIN ?? 'https://demo.mandati.ai'

/** The single CTA target. Every "try it" link on the site resolves here. */
export const TRY_URL = `${APP_ORIGIN}/try`

/** The login link in the nav. */
export const LOGIN_URL = APP_ORIGIN
```

- [ ] **Step 2: Create `marketing/src/layouts/Base.astro`**

```astro
---
import '../styles/global.css'
import { LOGIN_URL, TRY_URL } from '../lib/site'

interface Props {
  title: string
  description: string
}

const { title, description } = Astro.props
const canonical = new URL(Astro.url.pathname, Astro.site).href
const ogImage = new URL('/og.png', Astro.site).href
---
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <meta name="description" content={description} />
    <link rel="canonical" href={canonical} />
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />

    <meta property="og:type" content="website" />
    <meta property="og:title" content={title} />
    <meta property="og:description" content={description} />
    <meta property="og:url" content={canonical} />
    <meta property="og:image" content={ogImage} />
    <meta name="twitter:card" content="summary_large_image" />

    <script type="application/ld+json" set:html={JSON.stringify({
      '@context': 'https://schema.org',
      '@type': 'SoftwareApplication',
      name: 'Open Hospitality',
      applicationCategory: 'BusinessApplication',
      description,
      url: Astro.site?.href,
    })} />
  </head>
  <body class="bg-brand-canvas text-brand-ink font-sans antialiased">
    <header class="flex items-center justify-between px-6 py-4 border-b border-brand-line">
      <a href="/" class="font-display text-lg">Open Hospitality</a>
      <nav class="flex items-center gap-5 text-sm text-brand-ink-muted">
        <a href="/#platform" class="hover:text-brand-ink">Platform</a>
        <a href="/pricing" class="hover:text-brand-ink">Pricing</a>
        <a href="/your-data" class="hover:text-brand-ink">Your data</a>
        <a href={LOGIN_URL} class="rounded-lg border border-brand-line px-3 py-1.5 text-brand-ink hover:bg-brand-surface">
          Log in
        </a>
      </nav>
    </header>

    <main><slot /></main>

    <footer class="mt-20 border-t border-brand-line px-6 py-10 text-sm text-brand-ink-muted">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <span>Open Hospitality — the open financial and labor engine for hotels.</span>
        <div class="flex gap-5">
          <a href="/pricing" class="hover:text-brand-ink">Pricing</a>
          <a href="/your-data" class="hover:text-brand-ink">Your data</a>
          <a href="https://github.com/csharp36/open-hospitality" class="hover:text-brand-ink">GitHub</a>
          <a href={TRY_URL} class="hover:text-brand-ink">Try it</a>
        </div>
      </div>
      <p class="mt-6 text-xs">Apache-2.0 licensed and self-hostable.</p>
    </footer>
  </body>
</html>
```

**On radius classes:** the app defines `--radius-control` and `--radius-card` in
`frontend/src/index.css`, and Task 1 deliberately does *not* move them —
they are app tokens, not marketing ones. So the marketing site uses Tailwind's
stock `rounded-lg` throughout. Writing `rounded-control` here would silently
produce no radius at all, since the token does not exist in this package.

- [ ] **Step 3: Build**

Run: `cd marketing && npm run build`
Expected: completes. The placeholder `index.astro` does not yet use the layout,
so this only proves the layout compiles.

- [ ] **Step 4: Commit**

```bash
git add marketing/src/lib/site.ts marketing/src/layouts/Base.astro
git commit -m "feat(oh16): base layout, head metadata, and the app-origin constant"
```

---

## Task 6: The homepage

Six sections, in the order the design fixes: claim the platform, prove it,
show the surface area, make the multi-property claim concrete, promise forward,
close.

**Files:**
- Create: `marketing/src/components/Hero.astro`,
  `Transformation.astro`, `Pillars.astro`, `Portfolio.astro`,
  `Forward.astro`, `Close.astro`
- Modify: `marketing/src/pages/index.astro`

- [ ] **Step 1: `marketing/src/components/Hero.astro`**

```astro
---
import { TRY_URL } from '../lib/site'
---
<section class="px-6 py-20 text-center">
  <p class="text-xs font-semibold uppercase tracking-[0.14em] text-brand-accent">
    One property or one hundred
  </p>
  <h1 class="mx-auto mt-4 max-w-[20ch] font-display text-4xl leading-tight tracking-tight sm:text-5xl">
    Run the money and the labor for every property you operate.
  </h1>
  <p class="mx-auto mt-5 max-w-[60ch] text-brand-ink-muted">
    Operating statements, department labor cost, schedules, and time clocks — for one
    hotel or a whole portfolio, in one account. It starts with data your PMS already
    produces.
  </p>
  <div class="mt-8 flex flex-wrap items-center justify-center gap-3">
    <a href={TRY_URL} class="rounded-lg bg-brand-accent px-5 py-3 font-medium text-white">
      See it on your own report
    </a>
    <a href={TRY_URL} class="rounded-lg border border-brand-line px-5 py-3">
      Run a sample instead
    </a>
  </div>
</section>
```

- [ ] **Step 2: `marketing/src/components/Transformation.astro`**

The "before" image comes from Task 8. Until then the `<img>` 404s in dev, which
is harmless; Task 10's link check runs after Task 8.

```astro
---
const rows = [
  ['Rooms revenue', '142,880'],
  ['Food & beverage', '18,204'],
  ['Other operated', '4,116'],
  ['Total revenue', '165,200'],
  ['Rooms expense', '(38,410)'],
  ['Departmental profit', '117,550'],
]
const bold = new Set(['Total revenue', 'Departmental profit'])
---
<section class="border-t border-brand-line px-6 py-16">
  <h2 class="text-center font-display text-3xl">This becomes this.</h2>
  <p class="mx-auto mt-3 max-w-[52ch] text-center text-brand-ink-muted">
    The report your property management system produces every night, sorted into a real
    operating statement. No setup, no integration, no account.
  </p>

  <div class="mx-auto mt-10 grid max-w-4xl items-center gap-4 sm:grid-cols-[1fr_auto_1fr]">
    <figure class="rounded-lg border border-brand-line bg-brand-surface p-4">
      <figcaption class="mb-3 text-[10px] uppercase tracking-widest text-brand-ink-muted">
        Opera · Trial Balance
      </figcaption>
      <img src="/before-trial-balance.webp" alt="An unsorted property management system trial balance"
           width="480" height="320" class="w-full rounded" loading="lazy" />
    </figure>

    <div aria-hidden="true" class="text-center text-2xl text-brand-accent">→</div>

    <div class="rounded-lg border border-brand-line bg-brand-surface p-4">
      <p class="mb-3 text-[10px] uppercase tracking-widest text-brand-ink-muted">
        Summary Operating Statement
      </p>
      <dl>
        {rows.map(([label, value]) => (
          <div class={`flex justify-between border-b border-brand-line py-1.5 text-sm last:border-0 ${bold.has(label) ? 'font-semibold' : ''}`}>
            <dt>{label}</dt>
            <dd class="font-mono tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  </div>
</section>
```

- [ ] **Step 3: `marketing/src/components/Pillars.astro`**

Every claim here is shipped today. Nothing in this section is forward-looking.

```astro
---
const pillars = [
  {
    name: 'Financials',
    body: 'A USALI operating statement with drill-through to the transaction row behind any line. QuickBooks and CPA pack export.',
  },
  {
    name: 'Labor',
    body: 'Schedule 14/15 cost and hours by department, target hours against rooms actually sold, overtime and productivity.',
  },
  {
    name: 'Workforce',
    body: 'Employees, scheduling, and an iPad time clock with server-enforced punch order. Pay runs when the period closes.',
  },
  {
    name: 'Connections',
    body: 'Opera, AutoClerk and SkyTouch in. Your own accounting and payroll accounts connected out.',
  },
]
---
<section id="platform" class="border-t border-brand-line px-6 py-16">
  <h2 class="text-center font-display text-3xl">What it runs</h2>
  <div class="mx-auto mt-10 grid max-w-5xl gap-4 sm:grid-cols-2 lg:grid-cols-4">
    {pillars.map((p) => (
      <div class="rounded-lg border border-brand-line bg-brand-surface p-5">
        <h3 class="font-medium">{p.name}</h3>
        <p class="mt-2 text-sm text-brand-ink-muted">{p.body}</p>
      </div>
    ))}
  </div>
</section>
```

- [ ] **Step 4: `marketing/src/components/Portfolio.astro`**

```astro
---
const properties = [
  ['Harborview Inn', '78.4% · $164'],
  ['Cedar Street Hotel', '71.2% · $148'],
  ['The Marlow', '83.9% · $211'],
]
---
<section class="border-t border-brand-line px-6 py-16">
  <div class="mx-auto grid max-w-4xl items-center gap-10 sm:grid-cols-2">
    <div>
      <h2 class="font-display text-3xl leading-snug">
        Every property in one account, walled off from every other.
      </h2>
      <p class="mt-4 text-brand-ink-muted">
        Add properties as you take them on. Isolation is enforced by the database itself,
        not by application code that has to remember — so one property can never read
        another's rows.
      </p>
    </div>
    <div class="rounded-lg border border-brand-line bg-brand-surface p-5">
      <dl>
        {properties.map(([name, stat]) => (
          <div class="flex justify-between border-b border-brand-line py-1.5 text-sm">
            <dt>{name}</dt>
            <dd class="font-mono tabular-nums">{stat}</dd>
          </div>
        ))}
        <div class="flex justify-between py-1.5 text-sm text-brand-ink-muted">
          <dt>+ 9 more</dt><dd></dd>
        </div>
      </dl>
      <p class="mt-4 text-xs text-brand-accent">
        Side-by-side roll-up
        <span class="ml-1 rounded border border-brand-accent-soft px-1.5 py-0.5 uppercase tracking-wider">
          shipping next
        </span>
      </p>
    </div>
  </div>
</section>
```

- [ ] **Step 5: `marketing/src/components/Forward.astro`**

This is where the guard runs. `featuredEntries` throws during `astro build` if
the catalogue disagrees with the copy.

```astro
---
import { FEATURED, featuredEntries, readCatalogue } from '../lib/roadmap'

const entries = featuredEntries(readCatalogue(), FEATURED)
---
<section class="border-t border-brand-line px-6 py-16">
  <h2 class="text-center font-display text-3xl">Shipping next</h2>
  <p class="mx-auto mt-3 max-w-[56ch] text-center text-brand-ink-muted">
    What we are building now. This list is generated from the project's own roadmap, so
    it cannot drift from what is actually happening.
  </p>
  <div class="mx-auto mt-10 grid max-w-5xl gap-4 sm:grid-cols-2 lg:grid-cols-4">
    {entries.map((e) => (
      <div class="rounded-lg border border-brand-line bg-brand-surface p-5">
        <h3 class="font-medium">{e.heading}</h3>
        <p class="mt-2 text-sm text-brand-ink-muted">{e.blurb}</p>
        <p class="mt-3 text-[10px] uppercase tracking-widest text-brand-accent">{e.status}</p>
      </div>
    ))}
  </div>
</section>
```

- [ ] **Step 6: `marketing/src/components/Close.astro`**

```astro
---
import { TRY_URL } from '../lib/site'
---
<section class="border-t border-brand-line px-6 py-16 text-center">
  <h2 class="font-display text-3xl">Start with this morning's report.</h2>
  <a href={TRY_URL} class="mt-6 inline-block rounded-lg bg-brand-accent px-5 py-3 font-medium text-white">
    See it on your own report
  </a>
  <p class="mt-4 text-xs text-brand-ink-muted">
    Nothing is saved. No account needed. Apache-2.0 and self-hostable.
  </p>
</section>
```

- [ ] **Step 7: Replace `marketing/src/pages/index.astro`**

```astro
---
import Base from '../layouts/Base.astro'
import Hero from '../components/Hero.astro'
import Transformation from '../components/Transformation.astro'
import Pillars from '../components/Pillars.astro'
import Portfolio from '../components/Portfolio.astro'
import Forward from '../components/Forward.astro'
import Close from '../components/Close.astro'
---
<Base
  title="Open Hospitality — run the money and the labor for every property"
  description="Operating statements, department labor cost, schedules, and time clocks for one hotel or a whole portfolio. Starts with data your PMS already produces."
>
  <Hero />
  <Transformation />
  <Pillars />
  <Portfolio />
  <Forward />
  <Close />
</Base>
```

- [ ] **Step 8: Build and check the guard ran**

Run: `cd marketing && npm run build && grep -c "Shipping next" dist/index.html`
Expected: build completes, count is 1.

- [ ] **Step 9: Verify the guard actually fails the build**

Temporarily set OH-11's status to `shipped` in `.github/roadmap.yml`, then:

Run: `cd marketing && npm run build`
Expected: FAIL with `OH-11 ("Portfolio roll-up across properties") has shipped`.

**Revert the roadmap.yml change before continuing.** This step exists because a
guard that has never been seen to fire is not known to work.

- [ ] **Step 10: Commit**

```bash
git add marketing/src/components marketing/src/pages/index.astro
git commit -m "feat(oh16): the homepage

Six sections: claim the platform, prove it with the before/after, show the
shipped surface area, make the multi-property claim concrete, promise
forward under the roadmap guard, close."
```

---

## Task 7: The pricing and data pages

**Files:**
- Create: `marketing/src/pages/pricing.astro`, `marketing/src/pages/your-data.astro`

- [ ] **Step 1: `marketing/src/pages/pricing.astro`**

```astro
---
import Base from '../layouts/Base.astro'
import { TRY_URL } from '../lib/site'
---
<Base
  title="Pricing — Open Hospitality"
  description="The open core is free and self-hostable. The hosted product is what costs money. We are in pilot, and pricing lands at general availability."
>
  <section class="mx-auto max-w-3xl px-6 py-20">
    <h1 class="font-display text-4xl">What this costs</h1>
    <p class="mt-6 text-brand-ink-muted">
      We are in pilot, and we have not set a price yet. Rather than invent one, here is
      the shape of the answer — which is decided, and will not change when the number is.
    </p>

    <div class="mt-12 space-y-8">
      <div class="rounded-lg border border-brand-line bg-brand-surface p-6">
        <h2 class="font-display text-2xl">The open core is free</h2>
        <p class="mt-3 text-brand-ink-muted">
          The engine is Apache-2.0. Ingestion, the USALI mapping, the operating statement,
          labor cost, scheduling and the time clock are open source, and you can run the
          whole thing on your own hardware forever without paying us anything.
        </p>
      </div>

      <div class="rounded-lg border border-brand-line bg-brand-surface p-6">
        <h2 class="font-display text-2xl">The hosted product is what you pay for</h2>
        <p class="mt-3 text-brand-ink-muted">
          If you would rather not run a database, apply migrations, or manage identity, we
          run it for you — with backups, upgrades, and support. That is the paid product.
        </p>
      </div>

      <div class="rounded-lg border border-brand-line bg-brand-surface p-6">
        <h2 class="font-display text-2xl">Pricing lands at general availability</h2>
        <p class="mt-3 text-brand-ink-muted">
          Pilot properties help us work out what a fair basis actually is. When we publish
          a number, pilot participants will know well before it applies to them.
        </p>
      </div>
    </div>

    <p class="mt-12 text-brand-ink-muted">
      The question behind the question is usually whether you will be squeezed once you
      depend on this. The license is the answer: the core is open, so leaving is always
      possible, and that constrains us more than any promise would.
    </p>

    <a href={TRY_URL} class="mt-10 inline-block rounded-lg bg-brand-accent px-5 py-3 font-medium text-white">
      See it on your own report
    </a>
  </section>
</Base>
```

- [ ] **Step 2: `marketing/src/pages/your-data.astro`**

Every claim on this page is a property the system already has. Do not add one
that isn't.

```astro
---
import Base from '../layouts/Base.astro'
import { TRY_URL } from '../lib/site'

const facts = [
  {
    heading: 'The preview stores nothing',
    body: 'When you drop a report into the try-it page, it is parsed in memory and the result is returned to your browser. No account, no database row, nothing written to disk.',
  },
  {
    heading: 'Properties are isolated at the database',
    body: 'Separation between tenants is enforced by row-level security in Postgres, not by application code remembering to filter. One property cannot read another’s rows even if the application asks it to.',
  },
  {
    heading: 'Sensitive employee data is sealed before it reaches us',
    body: 'Social security numbers, bank details and tax elections are encrypted in your browser. The server stores them sealed and never holds them in plaintext at rest.',
  },
  {
    heading: 'The exit is real',
    body: 'The engine is Apache-2.0 and self-hostable. If you stop trusting us, you can run the same software yourself.',
  },
]
---
<Base
  title="Your data — Open Hospitality"
  description="What happens to your numbers: the preview stores nothing, tenants are isolated by row-level security, and sensitive employee data is sealed client-side."
>
  <section class="mx-auto max-w-3xl px-6 py-20">
    <h1 class="font-display text-4xl">What happens to your numbers</h1>
    <p class="mt-6 text-brand-ink-muted">
      You are being asked to hand your property's financials to a company you have not
      heard of. That is a reasonable thing to hesitate over, so here is exactly what the
      software does.
    </p>

    <div class="mt-12 space-y-8">
      {facts.map((f) => (
        <div class="rounded-lg border border-brand-line bg-brand-surface p-6">
          <h2 class="font-display text-2xl">{f.heading}</h2>
          <p class="mt-3 text-brand-ink-muted">{f.body}</p>
        </div>
      ))}
    </div>

    <a href={TRY_URL} class="mt-10 inline-block rounded-lg bg-brand-accent px-5 py-3 font-medium text-white">
      Try it — nothing is saved
    </a>
  </section>
</Base>
```

- [ ] **Step 3: Build**

Run: `cd marketing && npm run build && ls dist/pricing/index.html dist/your-data/index.html`
Expected: both paths exist.

- [ ] **Step 4: Commit**

```bash
git add marketing/src/pages/pricing.astro marketing/src/pages/your-data.astro
git commit -m "feat(oh16): pricing philosophy and data posture pages"
```

---

## Task 8: Site images — the "before" panel, the OG card, the favicon

Three assets, all generated or copied rather than hand-drawn, so none of them
drifts from the brand tokens.

The real exports under `~/Desktop/Sample Hotel` carry production figures and
must never enter the repo. The synthetic sample `/try` already demonstrates on
is committed and safe, and `tests/test_preview_samples.py` is where it is pinned
to keep parsing.

**Files:**
- Create: `scripts/gen_marketing_assets.py`
- Output: `marketing/public/before-trial-balance.webp`, `marketing/public/og.png`
- Copy: `marketing/public/favicon.svg`

- [ ] **Step 1: Check the rendering dependency**

Run: `cd /Users/csharpl/Desktop/open-hospitality && uv run python -c "import pypdfium2; print(pypdfium2.__version__)"`

If this fails with `ModuleNotFoundError`, add it first:
`uv add --dev pypdfium2` and re-run.

- [ ] **Step 2: Write the script**

`scripts/gen_marketing_assets.py`:

```python
"""Render the synthetic Opera sample's first page for the marketing site.

The marketing homepage shows an unreadable trial balance turning into an
operating statement. The "before" image is the SAME synthetic report the /try
preview runs its own sample against -- generated by gen_preview_samples.py and
committed at frontend/public/samples/. Real exports carry production figures and
are never committed, so this path is the only one that can produce the asset.

Run: uv run python scripts/gen_marketing_assets.py
"""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "frontend/public/samples/opera-trial-balance-sample.pdf"
TARGET = REPO / "marketing/public/before-trial-balance.webp"
SCALE = 2.0


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(
            f"{SOURCE} is missing. Run: uv run python scripts/gen_preview_samples.py"
        )

    pdf = pdfium.PdfDocument(SOURCE)
    image = pdf[0].render(scale=SCALE).to_pil()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    image.save(TARGET, "WEBP", quality=80, method=6)
    print(f"wrote {TARGET.relative_to(REPO)} ({TARGET.stat().st_size // 1024} KB)")


def og_card() -> None:
    """The social card, drawn in code so it can be regenerated rather than
    re-exported from a design tool by hand.

    The three colors below are hand-converted sRGB approximations of the oklch
    values in shared/brand.css -- PIL cannot read CSS, so they ARE a second
    copy and they CAN drift. Nothing detects that: the card is a PNG, and no
    test compares its pixels to the stylesheet. If the brand colors change,
    someone has to remember to re-run this. That is the known cost of having a
    raster social card at all.
    """
    canvas = (247, 242, 234)   # --color-brand-canvas
    ink = (51, 41, 31)         # --color-brand-ink
    accent = (189, 91, 61)     # --color-brand-accent

    image = Image.new("RGB", (1200, 630), canvas)
    draw = ImageDraw.Draw(image)

    title = ImageFont.truetype("/System/Library/Fonts/Supplemental/Georgia.ttf", 68)
    body = ImageFont.truetype("/System/Library/Fonts/Supplemental/Georgia.ttf", 30)

    draw.text((80, 150), "Run the money and the labor", font=title, fill=ink)
    draw.text((80, 232), "for every property you operate.", font=title, fill=ink)
    draw.text((80, 360), "One property or one hundred.", font=body, fill=accent)
    draw.rectangle((80, 470, 200, 474), fill=accent)
    draw.text((80, 500), "Open Hospitality", font=body, fill=ink)

    target = REPO / "marketing/public/og.png"
    image.save(target, "PNG")
    print(f"wrote {target.relative_to(REPO)} ({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
    og_card()
```

The font path is macOS-specific. On a machine without Georgia, substitute any
serif the system has — `fc-list | grep -i serif` finds one on Linux. The card is
generated once and committed, so CI never runs this.

- [ ] **Step 3: Run it**

Run: `uv run python scripts/gen_marketing_assets.py`
Expected: two `wrote ...` lines — the WebP and `og.png`.

- [ ] **Step 4: Copy the favicon**

`Base.astro` links `/favicon.svg`, and the app already has one. Same product,
same mark:

```bash
cp frontend/public/favicon.svg marketing/public/favicon.svg
```

- [ ] **Step 5: Look at all three**

Open each file. The WebP must read as a dense, unsorted financial report at the
size the homepage shows it — if it is illegible mush or obviously blank, raise
`SCALE` and re-run. The OG card must have no clipped text at 1200×630.

This step is a human looking at images. Do not skip it: every automated check in
this plan can confirm a file *exists* and none can confirm it looks right.

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_marketing_assets.py marketing/public/
git commit -m "feat(oh16): the site's three images

The 'before' panel is the same synthetic report /try demonstrates on, so no
real export is needed and none can leak. The OG card is drawn from the brand
colors rather than exported by hand. The favicon is the app's."
```

---

## Task 9: robots.txt on both hosts

**Files:**
- Create: `marketing/public/robots.txt`, `frontend/public/robots.txt`

- [ ] **Step 1: `marketing/public/robots.txt`**

```
User-agent: *
Allow: /

Sitemap: https://oh.mandati.ai/sitemap-index.xml
```

- [ ] **Step 2: `frontend/public/robots.txt`**

```
# The app host. The marketing site is the surface meant to rank; the app's
# routes are behind a login and have nothing to offer a crawler. /try is the
# exception -- it is the public preview and should be findable.
User-agent: *
Disallow: /
Allow: /try
```

- [ ] **Step 3: Verify both land in their builds**

Run:

```bash
cd marketing && npm run build && cat dist/robots.txt
cd ../frontend && npm run build && cat dist/robots.txt
```

Expected: each prints the file written above.

- [ ] **Step 4: Commit**

```bash
git add marketing/public/robots.txt frontend/public/robots.txt
git commit -m "feat(oh16): robots.txt for both hosts

The app host served none, so the SPA was crawlable and would compete with
the marketing site on the same terms."
```

---

## Task 10: Build-time checks

A static site fails by broken links and missing metadata. These assert against
the real build output.

**Files:**
- Create: `marketing/tests/build.test.ts`

- [ ] **Step 1: Write the tests**

```ts
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const DIST = new URL('../dist/', import.meta.url).pathname
const PAGES = ['index.html', 'pricing/index.html', 'your-data/index.html']

function html(page: string): string {
  const path = join(DIST, page)
  if (!existsSync(path)) throw new Error(`${page} not built — run \`npm run build\` first`)
  return readFileSync(path, 'utf8')
}

describe.each(PAGES)('%s', (page) => {
  const doc = () => html(page)

  it('has a title', () => expect(doc()).toMatch(/<title>[^<]{10,}<\/title>/))
  it('has a meta description', () =>
    expect(doc()).toMatch(/<meta name="description" content="[^"]{40,}"/))
  it('has a canonical link', () =>
    expect(doc()).toMatch(/<link rel="canonical" href="https?:\/\/[^"]+"/))
  it('has an OG image', () =>
    expect(doc()).toMatch(/<meta property="og:image" content="[^"]+"/))
})

describe('referenced assets exist', () => {
  // og:image lives in a `content` attribute, so the href/src scan below is blind
  // to it. A broken social card is invisible until someone shares a link.
  it('every og:image resolves to a file in the build', () => {
    const missing: string[] = []
    for (const page of PAGES) {
      const m = html(page).match(/<meta property="og:image" content="([^"]+)"/)
      if (!m) { missing.push(`${page}: no og:image`); continue }
      const path = new URL(m[1]).pathname
      if (!existsSync(join(DIST, path))) missing.push(`${page} -> ${m[1]}`)
    }
    expect(missing).toEqual([])
  })
})

describe('internal links', () => {
  it('every internal href resolves to something in the build', () => {
    const missing: string[] = []
    for (const page of PAGES) {
      for (const [, href] of doc_hrefs(html(page))) {
        if (!href.startsWith('/') || href.startsWith('//')) continue
        const path = href.split('#')[0].split('?')[0]
        if (path === '/' || path === '') continue
        const candidates = [
          join(DIST, path),
          join(DIST, path, 'index.html'),
          join(DIST, `${path}.html`),
        ]
        if (!candidates.some(existsSync)) missing.push(`${page} -> ${href}`)
      }
    }
    expect(missing).toEqual([])
  })
})

function* doc_hrefs(source: string): Generator<[string, string]> {
  for (const m of source.matchAll(/(href|src)="([^"]+)"/g)) yield [m[1], m[2]]
}
```

- [ ] **Step 2: Build, then run**

Run: `cd marketing && npm run build && npx vitest run tests/build.test.ts`
Expected: all assertions PASS. A failure naming `before-trial-balance.webp`
means Task 8 was skipped.

- [ ] **Step 3: Commit**

```bash
git add marketing/tests/build.test.ts
git commit -m "test(oh16): assert metadata and link integrity on the built site"
```

---

## Task 11: Retire "the report your PMS already emails you"

The phrasing hard-codes today's transport into the product's self-description at
the moment OH-22 opens a second one. The marketing site already says "produces";
this brings `/try` into line so the two surfaces a visitor sees in sequence do
not contradict each other.

There is exactly one live occurrence in user-facing copy. Two other matches turn
up in a naive grep and **must not be changed**:

- `README.md:36` — "one PDF auto-emailed after the nightly audit" describes what
  *SkyTouch* does. That is a fact about a vendor, not a claim about our
  ingestion, and it stays true regardless of OH-22.
- `docs/design/*.md` — historical records of what was decided when. Rewriting a
  shipped design doc to match later copy destroys the record.

**Files:**
- Modify: `frontend/src/pages/preview/DropZone.tsx:17`

- [ ] **Step 1: Confirm the scope before editing**

Run: `grep -rn "PMS emails you" --include="*.tsx" frontend/src`
Expected: exactly one hit, `frontend/src/pages/preview/DropZone.tsx:17`. If more
appear, the file has changed since this plan was written — handle each on the
same principle: describe what the data *is*, not how it arrived.

- [ ] **Step 2: Rewrite the line**

`frontend/src/pages/preview/DropZone.tsx:17` currently reads:

```tsx
      setError('Please choose a PDF — the report your PMS emails you.')
```

Replace with:

```tsx
      setError('Please choose a PDF — the nightly report your PMS produces.')
```

"Produces" survives OH-22; "emails you" does not. The sentence stays concrete
about what to drop, because dropping a file is still exactly what this control
does.

- [ ] **Step 3: Run the frontend tests**

Run: `cd frontend && npm test`
Expected: PASS. If a test asserts on the old string, update the assertion — the
copy is the thing being changed deliberately.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/preview/DropZone.tsx
git commit -m "fix(oh16): stop describing ingestion by its current transport

'The report your PMS emails you' bakes today's file-based path into
user-facing copy, right as OH-22 opens a second one. 'The nightly report
your PMS produces' stays true whichever way it arrives.

README's SkyTouch line is untouched: that one describes what SkyTouch does,
not how we ingest."
```

---

## Task 12: The CTA smoke test

**Files:**
- Create: `marketing/playwright.config.ts`, `marketing/e2e/cta.spec.ts`,
  `marketing/vitest.config.ts`
- Modify: `marketing/.gitignore`

**Two things this task needs that are easy to miss.**

*Vitest will collect the Playwright spec and fail.* Vitest's default include
glob is `**/*.{test,spec}.*`, so `e2e/cta.spec.ts` gets picked up and dies with
`Playwright Test did not expect test() to be called here`. Adding the e2e file
therefore BREAKS `npm test` unless vitest is told to skip that directory.
`frontend/vite.config.ts` already carries this exclusion for the identical
reason — mirror it:

```ts
// marketing/vitest.config.ts
import { configDefaults, defineConfig } from 'vitest/config'

// e2e/ holds Playwright specs. Vitest's default glob matches *.spec.* and would
// collect them, where Playwright's test() cannot run. frontend/vite.config.ts
// excludes its own e2e directory for the same reason.
export default defineConfig({
  test: { exclude: [...configDefaults.exclude, 'e2e/**'] },
})
```

*`astro preview` may detach itself and break Playwright's `webServer`.* Astro
detects an agent-like parent process and forces background mode, so the process
Playwright spawns exits immediately and it reports `Process from
config.webServer exited early`. Set Astro's own escape hatch in the `webServer`
block: `env: { ...process.env, ASTRO_PREVIEW_BACKGROUND: '1' }`. This does not
fire in CI, and CI does not run Playwright — but without it nobody can run these
tests from an agent session.

- [ ] **Step 1: `marketing/playwright.config.ts`**

```ts
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://localhost:4321' },
  webServer: {
    command: 'npm run preview -- --port 4321',
    url: 'http://localhost:4321',
    reuseExistingServer: !process.env.CI,
  },
})
```

- [ ] **Step 2: `marketing/e2e/cta.spec.ts`**

```ts
import { expect, test } from '@playwright/test'

const APP_ORIGIN = process.env.APP_ORIGIN ?? 'https://demo.mandati.ai'

test('the primary CTA sends the visitor to the preview', async ({ page }) => {
  await page.goto('/')
  const cta = page.getByRole('link', { name: 'See it on your own report' }).first()
  await expect(cta).toHaveAttribute('href', `${APP_ORIGIN}/try`)
})

test('every page reaches the others through the nav', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'Pricing' }).first().click()
  await expect(page).toHaveURL(/\/pricing\/?$/)
  await page.getByRole('link', { name: 'Your data' }).first().click()
  await expect(page).toHaveURL(/\/your-data\/?$/)
})
```

- [ ] **Step 3: Close the Playwright artifact gap in .gitignore**

`marketing/.gitignore` currently lists `dist/`, `.astro/`, `node_modules/`, and
`test-results/`. Playwright also writes `playwright-report/` and `blob-report/`,
which the root `.gitignore` already excludes for the sibling `frontend/` package
but which nothing excludes here. Append both:

```
playwright-report/
blob-report/
```

Nothing was untracked before this task because no Playwright config existed; it
does now.

- [ ] **Step 4: Build and run**

Run: `cd marketing && npm run build && npx playwright test`
Expected: 2 passed. If Playwright's browsers are not installed, run
`npx playwright install chromium` first.

- [ ] **Step 5: Commit**

```bash
git add marketing/playwright.config.ts marketing/e2e/cta.spec.ts marketing/.gitignore
git commit -m "test(oh16): smoke test the CTA target and cross-page nav"
```

---

## Task 13: Wire the site into CI

`.github/workflows/ci.yml` does not know `marketing/` exists. Read it first and
follow its existing conventions rather than inventing new ones.

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Read the existing workflow**

Run: `cat .github/workflows/ci.yml`

Note how the frontend job is defined: its `runs-on`, Node setup action and
version, working-directory convention, and cache settings. The new job mirrors
them.

- [ ] **Step 2: Add a marketing job**

Add a job alongside the frontend one, matching the file's existing style:

```yaml
  marketing:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: marketing
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: npm
          cache-dependency-path: marketing/package-lock.json
      - run: npm ci
      # Build first: the roadmap guard runs during the build, and the metadata
      # and link assertions read dist/.
      - run: npm run build
      - run: npm test
```

Adjust action versions and the Node version to match whatever the frontend job
already uses — consistency with the file beats the versions written here.

- [ ] **Step 3: Verify the workflow parses**

Run: `cd marketing && npm ci && npm run build && npm test`
Expected: all pass locally, which is what the job runs.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml marketing/package-lock.json
git commit -m "ci(oh16): build and test the marketing site

The build runs the roadmap guard, so a featured item shipping turns CI red
until the site is updated."
```

---

## Done

The site builds, is tested, and runs locally with `cd marketing && npm run dev`.

Deployment — the Cloudflare Pages project, the custom domain, analytics, and the
deploy workflow — is `2026-09-01-oh16-marketing-deploy.md`, which is gated on an
API token only a human can mint.
