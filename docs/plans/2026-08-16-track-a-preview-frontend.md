# Track A — Part 2: Front-door preview UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public, unauthenticated page where a hotel operator drops a PMS PDF and sees it mapped to a USALI P&L — consuming the Part 1 `POST /api/preview`, persisting nothing, in a warm-but-precise skin.

**Architecture:** A new public route `/try` (TanStack Router is code-defined in `router.tsx`; `/` is already a redirect entry route, so the front door lives at `/try` — promoting it to the true landing is a deferred follow-on). The route is allowlisted in `RootShell.tsx` so it renders **outside** `RequireAuth`/`Layout`. A public `postPreview` client posts the raw PDF body and **must not** attach the OIDC token or redirect to login. The page renders a discriminated result: P&L + coverage (happy), or a "notify me" / "see a sample" edge state.

**Tech Stack:** React 19, TanStack Router, Tailwind v4 (CSS `@theme`), Vitest + Testing Library, Playwright. Backend contract: Part 1 `/api/preview` returns `{status:"ok",payload}` | `{status:"unsupported",vendor,reason}` | `{status:"unreadable",hints[]}`; abuse-guard rejections are 413/415/429 with a `{detail}` body. Design spec: [`docs/design/2026-08-16-track-a-front-door-preview-design.md`](../design/2026-08-16-track-a-front-door-preview-design.md).

**Model to follow:** `frontend/src/pages/UploadPage.tsx` (the existing authenticated drag-drop page) — match its conventions: default-export function component, discriminated-union status state, semantic Tailwind tokens, `<input type="file" accept=".pdf" aria-label>`, result cards as `role="region" aria-label={...}`, Tailwind v4 important modifier as a trailing `!`.

**Deferred (NOT in Part 2):** lead capture — the "Save & automate → Get early access" and "Notify me" CTAs are **visual, non-wired placeholders** (a "coming soon" affordance) until the `/api/leads` plan lands (it needs the non-tenant-rows-under-RLS decision). The **KPI/pulse zone** stays empty until the stats family ships (Part 1's payload has `kpis: []` for a trial balance). Promoting `/try` → `/` as the marketing landing (with the authed-user redirect) is a follow-on.

---

## File structure

- `frontend/src/index.css` — MODIFY: add `--color-brand-*` + `--font-display` marketing tokens to `@theme` (+ `.dark` remaps). Separate from the app's indigo identity.
- `frontend/src/api/types.ts` — MODIFY: add `PreviewResponse` + `PreviewPayload` types.
- `frontend/src/api/client.ts` — MODIFY: add public `postPreview(file)` (raw body, no auth, no login redirect).
- `frontend/src/pages/PreviewPage.tsx` — CREATE: the public front door (header + hero + DropZone + result/edge).
- `frontend/src/pages/preview/DropZone.tsx` — CREATE: drag/drop + file-pick surface.
- `frontend/src/pages/preview/PreviewResult.tsx` — CREATE: P&L + coverage + nothing-saved banner + confirm + deferred CTA.
- `frontend/src/pages/preview/EdgeState.tsx` — CREATE: unsupported / unreadable states.
- `frontend/src/router.tsx` — MODIFY: register the `/try` route.
- `frontend/src/RootShell.tsx` — MODIFY: allowlist `/try` as a bare `<Outlet/>` (public).
- Tests: `frontend/src/pages/PreviewPage.test.tsx` (+ component tests), `frontend/e2e/preview.spec.ts` (unauthenticated).

Commands (from `frontend/`): `npm run test` (vitest), `npm run e2e` (playwright), `npm run lint` (oxlint), `npm run build` (`tsc -b && vite build`).

---

## Task 1: Marketing skin tokens

**Files:** Modify `frontend/src/index.css`. Test: none (visual); `npm run build` must stay green.

- [ ] **Step 1:** In the `@theme { ... }` block (after the existing tokens), add the marketing brand tokens — kept separate from the app's indigo identity because only the unguarded `/try` page uses them:

```css
  /* --- Marketing front door (public /try): a warm hospitality identity,
     deliberately separate from the app's indigo so the product palette is
     untouched. Serif display + terracotta accent + monospace numbers. --- */
  --font-display: Georgia, "Times New Roman", serif;
  --color-brand-canvas: oklch(96.6% 0.012 79);
  --color-brand-surface: oklch(98.6% 0.008 79);
  --color-brand-ink: oklch(31% 0.03 47);
  --color-brand-ink-muted: oklch(52% 0.03 60);
  --color-brand-line: oklch(89% 0.02 74);
  --color-brand-accent: oklch(58% 0.13 42);
  --color-brand-accent-soft: oklch(94% 0.03 55);
```

- [ ] **Step 2:** In the `.dark { ... }` block, add warm-dark remaps so the page respects dark mode:

```css
  --color-brand-canvas: oklch(26% 0.02 60);
  --color-brand-surface: oklch(30% 0.02 60);
  --color-brand-ink: oklch(93% 0.012 79);
  --color-brand-ink-muted: oklch(74% 0.02 74);
  --color-brand-line: oklch(40% 0.02 60);
  --color-brand-accent: oklch(70% 0.12 45);
  --color-brand-accent-soft: oklch(34% 0.05 45 / 0.6);
```

- [ ] **Step 3:** `npm run build` (Tailwind generates `bg-brand-canvas`, `text-brand-ink`, `font-display`, etc.). Commit: `git commit -am "feat(ui): warm marketing brand tokens for the front door"`

> Values are a calibrated starting point; fine-tune during visual review. Monospace numbers use the built-in `font-mono` utility.

---

## Task 2: Public `postPreview` client + types

**Files:** Modify `frontend/src/api/types.ts`, `frontend/src/api/client.ts`. Test: `frontend/src/api/client.test.ts` (or the existing client test file — match what's there).

- [ ] **Step 1:** Add to `frontend/src/api/types.ts`:

```ts
export interface PreviewPnlLine {
  major: string
  sub: string
  line_item: string
  amount: string
}

export interface PreviewPayload {
  pms_source: string
  report_type: string
  business_date: string
  pnl_lines: PreviewPnlLine[]
  kpis: { label: string; value: string }[]
  codes_recognized: number
  codes_mapped: number
  codes_needs_review: number
  net_total: string
}

export type PreviewResponse =
  | { status: 'ok'; payload: PreviewPayload }
  | { status: 'unsupported'; vendor: string; reason: string }
  | { status: 'unreadable'; hints: string[] }
```

- [ ] **Step 2:** Write the failing client test (mock `fetch`), asserting `postPreview` posts raw bytes with `Content-Type: application/pdf`, sends **no** `Authorization` header, and does **not** redirect to login on a 4xx (it throws `ApiError` instead). Match the existing client-test style.

- [ ] **Step 3:** Add to `frontend/src/api/client.ts` (reuse the existing `raiseApiError`/`ApiError`; do NOT use `authHeaders` or `redirectToLogin`):

```ts
import type { PreviewResponse } from './types'

/**
 * PUBLIC preview — the anonymous front door. Posts the raw PDF body to the
 * Part 1 endpoint. Deliberately does NOT attach the OIDC token (authHeaders)
 * and does NOT redirect to login on a 4xx: an anonymous visitor must never be
 * bounced to Keycloak. 413/415/429 surface as ApiError for a friendly message.
 */
export async function postPreview(file: File): Promise<PreviewResponse> {
  const res = await fetch('/api/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/pdf' },
    body: file, // a File is a Blob -> sent as the raw request body
  })
  if (!res.ok) await raiseApiError(res)
  return (await res.json()) as PreviewResponse
}
```

- [ ] **Step 4:** `npm run test` (client test passes), `npm run lint`, `npm run build`. Commit: `git commit -am "feat(api): public postPreview client (raw body, no auth)"`

---

## Task 3: Public route wiring (`/try`) + a minimal PreviewPage

**Files:** Modify `frontend/src/router.tsx`, `frontend/src/RootShell.tsx`; create a minimal `frontend/src/pages/PreviewPage.tsx`. Test: `frontend/src/pages/PreviewPage.test.tsx`.

- [ ] **Step 1:** Create a minimal `PreviewPage` (fleshed out in Task 6) so the route resolves:

```tsx
// frontend/src/pages/PreviewPage.tsx
export default function PreviewPage() {
  return (
    <main className="min-h-screen bg-brand-canvas text-brand-ink" data-testid="front-door">
      <h1 className="font-display">See your night audit as a real P&L.</h1>
    </main>
  )
}
```

- [ ] **Step 2:** Register the route in `frontend/src/router.tsx` — import `PreviewPage`, add a route, and include it in `rootRoute.addChildren([...])`:

```ts
import PreviewPage from './pages/PreviewPage'

const tryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/try',
  component: PreviewPage,
})
// ...add `tryRoute` to the addChildren([...]) array.
```

- [ ] **Step 3:** Allowlist `/try` as public in `frontend/src/RootShell.tsx` — it must render OUTSIDE `RequireAuth` (an anonymous visitor must not be bounced to login):

```ts
  if (pathname === '/callback' || pathname === '/kiosk' || pathname === '/try')
    return <Outlet />
```

Update the surrounding comment to note `/try` is the public marketing/preview front door (no operator session).

- [ ] **Step 4:** Write `frontend/src/pages/PreviewPage.test.tsx` proving the page renders **unauthenticated** — render the real router at `/try` WITHOUT the `AuthContext.Provider` wrapper (a public route needs no auth context), and assert the hero text appears and no login redirect fires:

```tsx
import { createMemoryHistory } from '@tanstack/react-router'
import { RouterProvider } from '@tanstack/react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { createAppRouter } from '../router'

function renderAt(path: string) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }))
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('PreviewPage (public)', () => {
  it('renders the front door without authentication', async () => {
    renderAt('/try')
    expect(await screen.findByText(/see your night audit/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 5:** `npm run test`, `npm run lint`, `npm run build`. Commit: `git commit -am "feat(ui): public /try route for the anonymous front door"`

---

## Task 4: DropZone

**Files:** Create `frontend/src/pages/preview/DropZone.tsx`. Test: `frontend/src/pages/preview/DropZone.test.tsx`.

Model on `UploadPage.tsx`'s drag/drop handling. DropZone is presentational input: it validates type/size client-side and calls `onFile(file)`; the page owns the async call.

- [ ] **Step 1:** Write the failing test: rendering DropZone, a `fireEvent.change` on the file input (`findByLabelText('PDF file')`) with a `new File(['x'], 'a.pdf', { type: 'application/pdf' })` calls `onFile`; a `.txt` file does NOT call `onFile` and surfaces a validation message; a >10MB file surfaces "too large".

- [ ] **Step 2:** Implement:

```tsx
// frontend/src/pages/preview/DropZone.tsx
import { useRef, useState } from 'react'

const MAX_BYTES = 10 * 1024 * 1024

export default function DropZone({ onFile }: { onFile: (file: File) => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [over, setOver] = useState(false)

  function accept(file: File | undefined) {
    if (!file) return
    if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
      setError('Please choose a PDF — the report your PMS emails you.')
      return
    }
    if (file.size > MAX_BYTES) {
      setError('That file is over 10 MB. A night-audit PDF is usually much smaller.')
      return
    }
    setError(null)
    onFile(file)
  }

  return (
    <div>
      <div
        onDragOver={(e) => { e.preventDefault(); setOver(true) }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); accept(e.dataTransfer.files[0]) }}
        className={`rounded-card border p-8 text-center ${over ? 'border-brand-accent bg-brand-accent-soft' : 'border-brand-line bg-brand-surface'}`}
      >
        <p className="text-brand-ink">Drop your PMS report here</p>
        <p className="mt-1 text-sm text-brand-ink-muted">Opera · AutoClerk — PDF, nothing saved</p>
        <button
          type="button"
          className="mt-3 rounded-control border border-brand-accent px-4 py-1.5 text-brand-accent"
          onClick={() => inputRef.current?.click()}
        >
          Choose a file
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".pdf"
          aria-label="PDF file"
          className="hidden"
          onChange={(e) => accept(e.target.files?.[0])}
        />
      </div>
      {error && <p role="alert" className="mt-2 text-sm text-danger-red">{error}</p>}
    </div>
  )
}
```

- [ ] **Step 3:** `npm run test`, `npm run lint`, `npm run build`. Commit: `git commit -am "feat(ui): DropZone for the preview front door"`

---

## Task 5: PreviewResult

**Files:** Create `frontend/src/pages/preview/PreviewResult.tsx`. Test: `frontend/src/pages/preview/PreviewResult.test.tsx`.

Renders an `ok` payload: the "🔒 nothing saved" banner, the **coverage** line (Part 1's honest signal — no "ties out" yet), the USALI P&L lines (monospace amounts), a "Recognized: X — not right?" confirm, and the **deferred** early-access CTA (visual only). Render `kpis` only if non-empty (empty for a trial balance today).

- [ ] **Step 1:** Write the failing test: given a payload with two `pnl_lines` and `codes_recognized: 4, codes_mapped: 3, codes_needs_review: 2`, the region `role="region" aria-label="Preview result"` shows the source, both line items, the amounts, and a coverage string like "3 of 4 mapped · 2 need review". Assert the "nothing saved" banner is present. Assert the CTA is rendered but marked as coming-soon (e.g. `disabled` or `aria-disabled`).

- [ ] **Step 2:** Implement:

```tsx
// frontend/src/pages/preview/PreviewResult.tsx
import type { PreviewPayload } from '../../api/types'

export default function PreviewResult({ payload }: { payload: PreviewPayload }) {
  return (
    <section role="region" aria-label="Preview result" className="space-y-4">
      <p className="rounded-control bg-brand-surface px-3 py-2 text-sm text-brand-ink-muted">
        🔒 Nothing saved — this preview lives in your browser session only.
      </p>

      <header className="flex items-baseline justify-between">
        <span className="text-sm text-brand-ink-muted">
          Recognized: <b className="text-brand-ink">{payload.pms_source} · {payload.report_type}</b>{' '}
          <button type="button" className="underline">not right?</button>
        </span>
        <span className="font-mono text-xs text-brand-ink-muted">
          {payload.codes_mapped} of {payload.codes_recognized} mapped · {payload.codes_needs_review} need review
        </span>
      </header>

      <table className="w-full text-sm">
        <tbody>
          {payload.pnl_lines.map((l, i) => (
            <tr key={i} className="border-b border-brand-line">
              <td className="py-1.5 text-brand-ink">{l.major} — {l.line_item}</td>
              <td className="py-1.5 text-right font-mono text-brand-ink">{l.amount}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {payload.kpis.length > 0 && (
        <div className="flex gap-6 font-mono">
          {payload.kpis.map((k, i) => (
            <div key={i}><div className="text-lg">{k.value}</div><div className="text-xs text-brand-ink-muted">{k.label}</div></div>
          ))}
        </div>
      )}

      <button
        type="button"
        aria-disabled="true"
        title="Coming soon"
        className="rounded-control bg-brand-accent px-4 py-2 text-white opacity-70"
      >
        Save &amp; automate → Get early access (coming soon)
      </button>
    </section>
  )
}
```

- [ ] **Step 3:** `npm run test`, `npm run lint`, `npm run build`. Commit: `git commit -am "feat(ui): PreviewResult renders the USALI P&L + coverage"`

---

## Task 6: EdgeState + assemble PreviewPage

**Files:** Create `frontend/src/pages/preview/EdgeState.tsx`; flesh out `frontend/src/pages/PreviewPage.tsx`. Test: extend `frontend/src/pages/PreviewPage.test.tsx`.

- [ ] **Step 1:** Implement `EdgeState`:

```tsx
// frontend/src/pages/preview/EdgeState.tsx
import type { PreviewResponse } from '../../api/types'

export default function EdgeState({ res, onRetry }: {
  res: Extract<PreviewResponse, { status: 'unsupported' | 'unreadable' }>
  onRetry: () => void
}) {
  if (res.status === 'unsupported') {
    return (
      <section role="region" aria-label="Unsupported PMS" className="space-y-3">
        <p className="text-brand-ink">🔎 This looks like a <b>{res.vendor}</b> report.</p>
        <p className="text-sm text-brand-ink-muted">We don't fully support {res.vendor} yet.</p>
        <button type="button" aria-disabled="true" title="Coming soon"
          className="rounded-control border border-brand-accent px-4 py-1.5 text-brand-accent opacity-70">
          Notify me when it's ready (coming soon)
        </button>
      </section>
    )
  }
  return (
    <section role="region" aria-label="Unreadable file" className="space-y-3">
      <p className="text-brand-ink">🤔 We couldn't read that file.</p>
      <ul className="list-disc pl-5 text-sm text-brand-ink-muted">
        {res.hints.map((h, i) => <li key={i}>{h}</li>)}
      </ul>
      <button type="button" onClick={onRetry}
        className="rounded-control border border-brand-line px-4 py-1.5 text-brand-ink">
        Try another file
      </button>
    </section>
  )
}
```

- [ ] **Step 2:** Flesh out `PreviewPage` — header (logo + `Log in` upper-right calling `login()` from `./auth/oidc`), hero, and the status machine:

```tsx
// frontend/src/pages/PreviewPage.tsx
import { useState } from 'react'
import { ApiError, postPreview } from '../api/client'
import type { PreviewResponse } from '../api/types'
import { login } from '../auth/oidc'
import DropZone from './preview/DropZone'
import EdgeState from './preview/EdgeState'
import PreviewResult from './preview/PreviewResult'

type State =
  | { kind: 'idle' }
  | { kind: 'working' }
  | { kind: 'result'; res: PreviewResponse }
  | { kind: 'error'; message: string }

export default function PreviewPage() {
  const [state, setState] = useState<State>({ kind: 'idle' })

  async function run(file: File) {
    setState({ kind: 'working' })
    try {
      setState({ kind: 'result', res: await postPreview(file) })
    } catch (e) {
      const message =
        e instanceof ApiError && e.status === 413 ? 'That file is too large (max 10 MB).'
        : e instanceof ApiError && e.status === 429 ? 'A lot of previews right now — try again shortly.'
        : e instanceof ApiError && e.status === 415 ? 'Please upload a PDF.'
        : 'Something went wrong reading that file. Please try again.'
      setState({ kind: 'error', message })
    }
  }

  return (
    <main className="min-h-screen bg-brand-canvas text-brand-ink font-sans">
      <header className="flex items-center justify-between px-6 py-4 border-b border-brand-line">
        <span className="font-display text-lg">Open Hospitality</span>
        <button type="button" onClick={() => { login().catch(() => {}) }}
          className="rounded-control border border-brand-accent px-3 py-1 text-sm text-brand-accent">
          Log in
        </button>
      </header>

      <div className="mx-auto max-w-2xl px-6 py-12 space-y-6">
        <div>
          <h1 className="font-display text-3xl leading-tight">See your night audit as a real P&amp;L.</h1>
          <p className="mt-2 text-brand-ink-muted">
            Drop the report your PMS already emails you. Mapped and shown back in seconds —
            <span className="text-brand-ink"> no account, nothing saved.</span>
          </p>
        </div>

        {(state.kind === 'idle' || state.kind === 'error') && <DropZone onFile={run} />}
        {state.kind === 'error' && <p role="alert" className="text-sm text-danger-red">{state.message}</p>}
        {state.kind === 'working' && <p className="text-brand-ink-muted">Reading your report…</p>}
        {state.kind === 'result' && state.res.status === 'ok' && <PreviewResult payload={state.res.payload} />}
        {state.kind === 'result' && state.res.status !== 'ok' &&
          <EdgeState res={state.res} onRetry={() => setState({ kind: 'idle' })} />}
      </div>
    </main>
  )
}
```

- [ ] **Step 3:** Extend `PreviewPage.test.tsx` (mock `../api/client`'s `postPreview`): a mocked `ok` response renders the "Preview result" region; a mocked `unreadable` response renders the "Unreadable file" region with hints; a mocked `unsupported` renders the vendor + "Notify me". Use `findByLabelText('PDF file')` + `fireEvent.change` with a `new File([...], 'a.pdf', {type:'application/pdf'})` to drive it.

- [ ] **Step 4:** `npm run test`, `npm run lint`, `npm run build`. Commit: `git commit -am "feat(ui): assemble the front-door preview page + edge states"`

---

## Task 7: Unauthenticated Playwright e2e

**Files:** Create `frontend/e2e/preview.spec.ts`.

- [ ] **Step 1:** Write the e2e. CRITICAL: the Playwright config's global `storageState` pre-authenticates every test — a genuinely-anonymous test MUST opt out per-file:

```ts
// frontend/e2e/preview.spec.ts
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

test.use({ storageState: { cookies: [], origins: [] } }) // anonymous — no seeded token

const SAMPLE = fileURLToPath(
  new URL('../../docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf', import.meta.url),
)

test('anonymous visitor drops a report and sees a P&L', async ({ page }) => {
  await page.goto('/try')
  await expect(page.getByText(/see your night audit/i)).toBeVisible()
  await page.getByLabel('PDF file').setInputFiles(SAMPLE)
  const region = page.getByRole('region', { name: 'Preview result' })
  await expect(region).toBeVisible()
  await expect(region.getByText(/mapped/)).toBeVisible()
})

test('a non-PDF drop shows an unreadable/validation message', async ({ page }) => {
  await page.goto('/try')
  await page.getByLabel('PDF file').setInputFiles({
    name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello'),
  })
  await expect(page.getByRole('alert')).toBeVisible()
})
```

- [ ] **Step 2:** Run: `npm run e2e -- preview.spec.ts` (boots the real backend + dev server per `playwright.config.ts`). Both pass. Bind the exact synthetic sample filename by listing `docs/reference/samples/` if it differs.

- [ ] **Step 3:** Full gates: `npm run test && npm run lint && npm run build && npm run e2e`. Commit: `git commit -am "test(ui): unauthenticated e2e for the preview front door"`

---

## Deferred to later plans

- **Lead capture** wires the two "coming soon" CTAs to `/api/leads` (needs the non-tenant-rows-under-RLS decision).
- **KPI/pulse zone** fills once the stats family ships a payload with `kpis`.
- **Promote `/try` → `/`** as the real marketing landing (with the authed-user redirect that `entryRoute` does today) once the experience is validated.

## Self-review checklist (done)

- Spec coverage: front-door shell, anonymous preview render, both edge states, warm-precise skin, and the deferred-CTA placeholders all have tasks. KPIs/leads/landing-promotion explicitly deferred.
- No placeholders: complete code for tokens, `postPreview`, route/RootShell wiring, and all four components; the one bind-at-implementation value (the sample PDF filename, already used by Part 1's tests) is called out.
- Type consistency: `PreviewResponse`/`PreviewPayload` in `types.ts` match Part 1's `_payload_json` shape (`net_total` string, `pnl_lines[].amount` string, `kpis: []`, coverage counts) and are used identically across `postPreview`, `PreviewResult`, `EdgeState`, `PreviewPage`.
- Public-route safety: `postPreview` bypasses `authHeaders`/`redirectToLogin`; `/try` is allowlisted in `RootShell`; the e2e opts out of the global authenticated `storageState`.
```
