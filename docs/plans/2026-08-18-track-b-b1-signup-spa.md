# Track B / B1 Part-2 — signup SPA (FRONTEND) plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (fresh subagent per task, full red→green→commit TDD loop, review between tasks).
> Steps use `- [ ]` checkboxes.

**Goal:** A public `/signup` single-page flow — accept an invite, verify the cell
by SMS OTP, name the workspace + first property (or flag an unsupported PMS), set
a password — that drives the built signup API and hands the new owner off to OIDC
login.

**Architecture:** A new unauthenticated route (`/signup`, added to `RootShell`'s
existing bare-`<Outlet/>` allowlist alongside `/kiosk` and `/callback`). A
`SignupPage` state machine (`cell → details → done`) calls a new
`src/api/signup.ts` that uses **plain `fetch` with no auth header** (the endpoints
are public). Success starts OIDC login with the invited email as `login_hint`.

**Tech Stack:** React 19, TypeScript, Vite, TanStack Router + Query, Tailwind,
`oidc-client-ts`, Vitest + Testing Library. Frontend dir: `frontend/`.

Implements the FRONTEND half of
[`docs/design/2026-08-18-track-b-b1-signup-frontend-design.md`](../design/2026-08-18-track-b-b1-signup-frontend-design.md)
(§6). The backend (first-property + PMS-interest) shipped on this same branch
(`feat/track-b-b1-signup-frontend`, commits `47dbafe..c5e18a0`).

## Gates (run for EVERY task before committing — from `frontend/`)

```bash
npm run lint          # oxlint
npm run test          # vitest run
npm run build         # tsc -b && vite build (type-check + bundle)
```

## Grounding facts (verified against the code — do not re-guess)

- **Unguarded routes:** `frontend/src/RootShell.tsx` `RootShell()` does
  `if (pathname === '/callback' || pathname === '/kiosk') return <Outlet />`
  (bare, no `RequireAuth`). Add `'/signup'` to that condition.
- **Routing:** `frontend/src/router.tsx` builds routes with
  `createRoute({ getParentRoute: () => rootRoute, path, component })`, assembles
  them in `const routeTree = rootRoute.addChildren([...])` (~line 202), and
  exports `createAppRouter(history?) = createRouter({ routeTree, history })`.
  Search params use `validateSearch: (search) => ({...})` (see `SosSearch`).
- **OIDC:** `frontend/src/auth/oidc.ts` exports `login() { return
  userManager.signinRedirect() }` and `userManager` (an `oidc-client-ts`
  `UserManager`). `signinRedirect` accepts `{ login_hint?: string }`.
- **API style:** `frontend/src/api/client.ts` calls relative paths
  (`fetch('/api/...', { headers: await authHeaders() })`) and AUTO-ATTACHES a
  bearer + `X-Active-Org`. The signup module must NOT use `client.ts` — it uses
  bare `fetch('/api/signup/...')` with only `Content-Type`, so no auth is sent.
- **Component tests:** render pattern (see `src/pages/KioskDevicesPage.test.tsx`):
  `createAppRouter(createMemoryHistory({ initialEntries: ['/route'] }))` inside
  `<QueryClientProvider><RouterProvider/></QueryClientProvider>`; API mocked via
  `vi.mock('../api/...')` + `vi.mocked(fn).mockResolvedValue(...)`; interaction
  via `@testing-library/user-event`. `/signup` renders as a bare Outlet, so NO
  `AuthContext.Provider` is needed.
- **API-module tests:** mock `globalThis.fetch` with `vi.spyOn(globalThis,
  'fetch').mockResolvedValue(new Response(...))` and assert method/path/body/
  headers (see `src/api/client.auth.test.ts`).
- **Backend contract (built):** `GET /api/signup/invite/{token}` → `{email}` or
  404; `POST /api/signup/otp` `{token, cell}` → 204 / 404 / 429; `POST
  /api/signup/complete` `{token, otp, workspace_name, workspace_alias,
  property_name, pms_source, pms_other_name?, wage_jurisdiction, timezone?, cell,
  password}` → 201 `{org_alias, pms_supported}` / 403 (wrong OTP) / 422 / 404 /
  429. `pms_source ∈ {opera, autoclerk, other}`; `other` requires
  `pms_other_name`. Supported PMS creates a property; `other` captures demand.
  Jurisdictions: `US-CA`, `US-FL`. Alias regex: `^[a-z0-9][a-z0-9-]{1,62}$`.

## File structure

```
NEW  frontend/src/api/signup.ts            # getInvite / requestOtp / completeSignup (plain fetch)
NEW  frontend/src/api/signup.test.ts       # Task 1
MOD  frontend/src/auth/oidc.ts             # login(loginHint?)
MOD  frontend/src/auth/oidc.test.ts        # Task 2 (create if absent)
MOD  frontend/src/RootShell.tsx            # /signup in the unguarded allowlist
MOD  frontend/src/router.tsx               # signupRoute + validateSearch(token) + addChildren
NEW  frontend/src/pages/SignupPage.tsx     # the wizard state machine (Tasks 3-6)
NEW  frontend/src/pages/SignupPage.test.tsx # Tasks 3-6
```

`SignupPage` is one focused file (a `step` state machine like other single-file
pages). If the details-step form grows unwieldy, extracting a `SignupDetailsForm`
child is reasonable — note it, don't force it.

---

## Task 1 — `src/api/signup.ts` (public API client)

**Files:** Create `frontend/src/api/signup.ts`, `frontend/src/api/signup.test.ts`.

- [ ] **Step 1: failing tests** — `frontend/src/api/signup.test.ts`

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { completeSignup, getInvite, requestOtp, SignupError } from './signup'

function mockFetch(status: number, body: unknown = null) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(body === null ? null : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
}

beforeEach(() => vi.restoreAllMocks())

describe('signup API (public — no auth header)', () => {
  it('getInvite returns the email on 200 and sends no Authorization', async () => {
    const f = mockFetch(200, { email: 'owner@hotel.test' })
    const email = await getInvite('tok-123')
    expect(email).toBe('owner@hotel.test')
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/signup/invite/tok-123')
    expect(new Headers((init as RequestInit).headers).get('Authorization')).toBeNull()
  })

  it('getInvite throws SignupError with status 404 on a missing invite', async () => {
    mockFetch(404, { detail: 'not found' })
    await expect(getInvite('nope')).rejects.toMatchObject({ status: 404 })
  })

  it('requestOtp POSTs token+cell and resolves on 204', async () => {
    const f = mockFetch(204)
    await requestOtp('tok-123', '+15550000000')
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/signup/otp')
    expect((init as RequestInit).method).toBe('POST')
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      token: 'tok-123', cell: '+15550000000',
    })
  })

  it('requestOtp throws SignupError{status:429} when rate-limited', async () => {
    mockFetch(429, { detail: 'too many requests' })
    await expect(requestOtp('t', '+15550000000')).rejects.toMatchObject({ status: 429 })
  })

  it('completeSignup returns {org_alias, pms_supported} on 201', async () => {
    mockFetch(201, { org_alias: 'sky-group', pms_supported: true })
    const res = await completeSignup({
      token: 't', otp: '123456', workspace_name: 'Sky', workspace_alias: 'sky-group',
      property_name: 'Sky Hotel', pms_source: 'opera', wage_jurisdiction: 'US-CA',
      timezone: 'America/New_York', cell: '+15550000000', password: 'passw0rd',
    })
    expect(res).toEqual({ org_alias: 'sky-group', pms_supported: true })
  })

  it('completeSignup throws SignupError{status:403} on a wrong OTP', async () => {
    mockFetch(403, { detail: 'verification failed' })
    await expect(
      completeSignup({
        token: 't', otp: '000000', workspace_name: 'W', workspace_alias: 'w-x',
        property_name: 'P', pms_source: 'opera', wage_jurisdiction: 'US-CA',
        cell: '+15550000000', password: 'passw0rd',
      }),
    ).rejects.toMatchObject({ status: 403 })
  })

  it('SignupError exposes the status for the UI to branch on', () => {
    expect(new SignupError(404).status).toBe(404)
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- signup` → FAIL (module missing).

- [ ] **Step 3: implement** — `frontend/src/api/signup.ts`

```ts
// Public signup API — Track B/B1 Part-2. Unlike src/api/client.ts these
// endpoints are UNAUTHENTICATED (an owner holding an invite token, no session),
// so we use bare fetch with only Content-Type: NO Authorization/X-Active-Org.
// Every failure surfaces as a SignupError carrying the HTTP status, so the page
// can branch (404 -> generic "invalid link", 403 -> wrong OTP, 429 -> back off).

export class SignupError extends Error {
  constructor(readonly status: number) {
    super(`signup request failed: ${status}`)
    this.name = 'SignupError'
  }
}

export interface CompletePayload {
  token: string
  otp: string
  workspace_name: string
  workspace_alias: string
  property_name: string
  pms_source: 'opera' | 'autoclerk' | 'other'
  pms_other_name?: string
  wage_jurisdiction: string
  timezone?: string
  cell: string
  password: string
}

export interface CompleteResult {
  org_alias: string
  pms_supported: boolean
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

export async function getInvite(token: string): Promise<string> {
  const res = await fetch(`/api/signup/invite/${encodeURIComponent(token)}`, {
    headers: JSON_HEADERS,
  })
  if (!res.ok) throw new SignupError(res.status)
  const body = (await res.json()) as { email: string }
  return body.email
}

export async function requestOtp(token: string, cell: string): Promise<void> {
  const res = await fetch('/api/signup/otp', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ token, cell }),
  })
  if (!res.ok) throw new SignupError(res.status)
}

export async function completeSignup(payload: CompletePayload): Promise<CompleteResult> {
  const res = await fetch('/api/signup/complete', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new SignupError(res.status)
  return (await res.json()) as CompleteResult
}
```

- [ ] **Step 4: run GREEN** — `npm run test -- signup`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(signup-spa): public signup API client (no auth header)"
```

---

## Task 2 — `login(loginHint?)` passes `login_hint`

**Files:** Modify `frontend/src/auth/oidc.ts`; Test `frontend/src/auth/oidc.test.ts` (create if absent).

- [ ] **Step 1: failing test** — add to (or create) `frontend/src/auth/oidc.test.ts`

```ts
import { describe, expect, it, vi } from 'vitest'

import { login, userManager } from './oidc'

describe('login()', () => {
  it('forwards an email as login_hint so the OIDC screen is prefilled', async () => {
    const spy = vi.spyOn(userManager, 'signinRedirect').mockResolvedValue(undefined)
    await login('owner@hotel.test')
    expect(spy).toHaveBeenCalledWith({ login_hint: 'owner@hotel.test' })
  })

  it('with no hint calls signinRedirect with no args (unchanged behavior)', async () => {
    const spy = vi.spyOn(userManager, 'signinRedirect').mockResolvedValue(undefined)
    await login()
    expect(spy).toHaveBeenCalledWith()
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- oidc` → FAIL (login takes no arg).

- [ ] **Step 3: implement** — in `frontend/src/auth/oidc.ts`, change `login`:

```ts
export function login(loginHint?: string): Promise<void> {
  return loginHint
    ? userManager.signinRedirect({ login_hint: loginHint })
    : userManager.signinRedirect()
}
```

> **Resolve at implementation:** existing callers do `login()` (no arg) — the
> optional param keeps them working. If `signinRedirect`'s TS type rejects
> `{ login_hint }`, use `{ extraQueryParams: { login_hint: loginHint } }`
> instead (both reach the IdP as the `login_hint` query param); adjust the test
> assertion to match whichever the types accept.

- [ ] **Step 4: run GREEN** — `npm run test -- oidc`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(signup-spa): login() forwards login_hint for the post-signup handoff"
```

---

## Task 3 — `/signup` route + `SignupPage` invite-load skeleton

**Files:** Modify `frontend/src/RootShell.tsx`, `frontend/src/router.tsx`; Create
`frontend/src/pages/SignupPage.tsx`, `frontend/src/pages/SignupPage.test.tsx`.

The page reads `?token=`, loads the invite on mount, and shows the invited email
(valid) or a single generic refusal with NO form (invalid — fail-closed).

- [ ] **Step 1: failing test** — `frontend/src/pages/SignupPage.test.tsx`

```tsx
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../router'

vi.mock('../api/signup', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/signup')>()),
  getInvite: vi.fn(),
  requestOtp: vi.fn(),
  completeSignup: vi.fn(),
}))
import { getInvite } from '../api/signup'

function renderSignup(token = 'tok-123') {
  const router = createAppRouter(
    createMemoryHistory({ initialEntries: [`/signup?token=${token}`] }),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

beforeEach(() => vi.restoreAllMocks())

describe('SignupPage — invite load', () => {
  it('shows the invited email when the token is valid', async () => {
    vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
    renderSignup()
    expect(await screen.findByText(/owner@hotel\.test/)).toBeInTheDocument()
    // The cell step is available (a mobile field), not a refusal.
    expect(screen.getByLabelText(/mobile/i)).toBeInTheDocument()
  })

  it('shows one generic refusal and NO form when the invite is invalid', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(getInvite).mockRejectedValue(new SignupError(404))
    renderSignup('bad')
    expect(await screen.findByText(/isn'?t valid or has expired/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/mobile/i)).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- SignupPage` → FAIL (route/page missing).

- [ ] **Step 3a: RootShell** — in `frontend/src/RootShell.tsx`, add `/signup`:

```tsx
  if (pathname === '/callback' || pathname === '/kiosk' || pathname === '/signup')
    return <Outlet />
```

- [ ] **Step 3b: router** — in `frontend/src/router.tsx`, import the page, add a
  route with a `token` search param, and register it in `addChildren`:

```tsx
import SignupPage from './pages/SignupPage'

export type SignupSearch = { token?: string }

const signupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/signup',
  component: SignupPage,
  validateSearch: (search: Record<string, unknown>): SignupSearch => ({
    token: typeof search.token === 'string' ? search.token : undefined,
  }),
})
```
  Add `signupRoute` to the `rootRoute.addChildren([...])` array.

- [ ] **Step 3c: page skeleton** — `frontend/src/pages/SignupPage.tsx`

```tsx
import { useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'
import { useState } from 'react'

import { getInvite } from '../api/signup'

const route = getRouteApi('/signup')

export default function SignupPage() {
  const { token } = route.useSearch()
  const invite = useQuery({
    queryKey: ['signup-invite', token],
    queryFn: () => getInvite(token as string),
    enabled: Boolean(token),
    retry: false,
  })

  if (!token || invite.isError)
    return (
      <div className="mx-auto max-w-md p-8 text-center">
        <p className="text-ink-muted">
          This invite link isn&rsquo;t valid or has expired.
        </p>
      </div>
    )
  if (invite.isPending)
    return <div className="mx-auto max-w-md p-8 text-center text-ink-muted">Loading…</div>

  return <SignupFlow token={token} email={invite.data} />
}

function SignupFlow({ token, email }: { token: string; email: string }) {
  const [step] = useState<'cell' | 'details' | 'done'>('cell')
  return (
    <div className="mx-auto max-w-md p-8">
      <h1 className="text-xl font-semibold">Create your workspace</h1>
      <p className="mt-1 text-sm text-ink-muted">Invited as {email}</p>
      {step === 'cell' && <CellStep />}
    </div>
  )
}

function CellStep() {
  // Task 4 fills this in.
  return (
    <form className="mt-6 space-y-4">
      <label className="block text-sm">
        Mobile number
        <input aria-label="Mobile number" name="cell" className="mt-1 w-full rounded border px-3 py-2" />
      </label>
    </form>
  )
}
```

> **Resolve at implementation:** confirm `getRouteApi('/signup')` is the right
> id (TanStack derives the id from the path — match how other pages read search,
> e.g. `SosPage`). Match the app's real token class names (`text-ink-muted` etc.
> are illustrative — use the tokens the app actually defines in `index.css`/other
> pages). The `<CellStep/>` here is a stub the label-only test needs; Task 4
> replaces it.

- [ ] **Step 4: run GREEN** — `npm run test -- SignupPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(signup-spa): /signup route + invite-load skeleton (fail-closed)"
```

---

## Task 4 — Cell step → request OTP → advance

**Files:** Modify `frontend/src/pages/SignupPage.tsx`, `SignupPage.test.tsx`.

- [ ] **Step 1: failing test** — add to `SignupPage.test.tsx`

```tsx
import userEvent from '@testing-library/user-event'
import { requestOtp } from '../api/signup'
// (getInvite already imported/mocked)

describe('SignupPage — cell step', () => {
  it('sends the OTP and advances to the details step', async () => {
    vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
    vi.mocked(requestOtp).mockResolvedValue(undefined)
    renderSignup()
    const cell = await screen.findByLabelText(/mobile/i)
    await userEvent.type(cell, '+15550000000')
    await userEvent.click(screen.getByRole('button', { name: /send code/i }))
    await waitFor(() =>
      expect(requestOtp).toHaveBeenCalledWith('tok-123', '+15550000000'),
    )
    // Details step is now shown (a verification-code field appears).
    expect(await screen.findByLabelText(/verification code/i)).toBeInTheDocument()
  })

  it('shows a back-off message on a 429 and stays on the cell step', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
    vi.mocked(requestOtp).mockRejectedValue(new SignupError(429))
    renderSignup()
    await userEvent.type(await screen.findByLabelText(/mobile/i), '+15550000000')
    await userEvent.click(screen.getByRole('button', { name: /send code/i }))
    expect(await screen.findByText(/too many/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/verification code/i)).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- SignupPage` → FAIL (no Send code button / no advance).

- [ ] **Step 3: implement** — lift `step`, `cell`, and an error into `SignupFlow`
  state and flesh out `CellStep`:

```tsx
function SignupFlow({ token, email }: { token: string; email: string }) {
  const [step, setStep] = useState<'cell' | 'details' | 'done'>('cell')
  const [cell, setCell] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function sendCode() {
    setBusy(true)
    setError(null)
    try {
      await requestOtp(token, cell)
      setStep('details')
    } catch (e) {
      setError(
        e instanceof SignupError && e.status === 429
          ? 'Too many requests — please wait a minute and try again.'
          : 'This invite link isn’t valid or has expired.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-md p-8">
      <h1 className="text-xl font-semibold">Create your workspace</h1>
      <p className="mt-1 text-sm text-ink-muted">Invited as {email}</p>
      {error && <p role="alert" className="mt-4 text-sm text-red-600">{error}</p>}
      {step === 'cell' && (
        <form
          className="mt-6 space-y-4"
          onSubmit={(e) => { e.preventDefault(); void sendCode() }}
        >
          <label className="block text-sm">
            Mobile number
            <input
              aria-label="Mobile number" name="cell" value={cell}
              onChange={(e) => setCell(e.target.value)}
              className="mt-1 w-full rounded border px-3 py-2"
            />
          </label>
          <button type="submit" disabled={busy || !cell}
                  className="w-full rounded bg-brand px-4 py-2 text-white">
            Send code
          </button>
        </form>
      )}
      {step === 'details' && (
        <DetailsStep token={token} email={email} cell={cell} onDone={() => setStep('done')} />
      )}
    </div>
  )
}
```
  Add imports: `import { getInvite, requestOtp, SignupError } from '../api/signup'`.
  Add a minimal `DetailsStep` stub that renders a `verification code` field (Task
  5 fleshes it out):

```tsx
function DetailsStep(_: { token: string; email: string; cell: string; onDone: () => void }) {
  return (
    <form className="mt-6 space-y-4">
      <label className="block text-sm">
        Verification code
        <input aria-label="Verification code" name="otp"
               className="mt-1 w-full rounded border px-3 py-2" />
      </label>
    </form>
  )
}
```

> **Resolve at implementation:** use the app's real primary-button class (the
> `bg-brand`/`text-white` here is illustrative — match an existing button, e.g.
> in `KioskDevicesPage`). Keep the label text so the tests' `getByLabelText`
> queries resolve (`/mobile/i`, `/verification code/i`).

- [ ] **Step 4: run GREEN** — `npm run test -- SignupPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(signup-spa): cell step sends OTP and advances"
```

---

## Task 5 — Details step: fields, PMS "other" reveal, auto-slug, submit

**Files:** Modify `frontend/src/pages/SignupPage.tsx`, `SignupPage.test.tsx`.

- [ ] **Step 1: failing tests** — add to `SignupPage.test.tsx`

```tsx
import { completeSignup } from '../api/signup'

async function toDetails() {
  vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
  vi.mocked(requestOtp).mockResolvedValue(undefined)
  renderSignup()
  await userEvent.type(await screen.findByLabelText(/mobile/i), '+15550000000')
  await userEvent.click(screen.getByRole('button', { name: /send code/i }))
  await screen.findByLabelText(/verification code/i)
}

describe('SignupPage — details step', () => {
  it('auto-slugs the workspace alias from the name (editable)', async () => {
    await toDetails()
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'Sunset Group!!')
    expect((screen.getByLabelText(/workspace url/i) as HTMLInputElement).value).toBe('sunset-group')
  })

  it('reveals a PMS name field only when "Other" is chosen', async () => {
    await toDetails()
    expect(screen.queryByLabelText(/which pms/i)).not.toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'other')
    expect(screen.getByLabelText(/which pms/i)).toBeInTheDocument()
  })

  it('submits the full payload and advances on 201', async () => {
    vi.mocked(completeSignup).mockResolvedValue({ org_alias: 'sunset-group', pms_supported: true })
    await toDetails()
    await userEvent.type(screen.getByLabelText(/verification code/i), '123456')
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'Sunset Group')
    await userEvent.type(screen.getByLabelText(/property name/i), 'Sunset Inn')
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'opera')
    await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(completeSignup).mock.calls[0]![0]
    expect(payload).toMatchObject({
      token: 'tok-123', otp: '123456', workspace_name: 'Sunset Group',
      workspace_alias: 'sunset-group', property_name: 'Sunset Inn',
      pms_source: 'opera', wage_jurisdiction: 'US-CA', cell: '+15550000000',
      password: 'passw0rd1',
    })
    expect(payload.timezone).toBeTruthy() // browser-detected
    expect(await screen.findByText(/ready/i)).toBeInTheDocument()
  })

  it('shows an inline retry on a wrong OTP (403) and stays on the details step', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(completeSignup).mockRejectedValue(new SignupError(403))
    await toDetails()
    await userEvent.type(screen.getByLabelText(/verification code/i), '000000')
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'X Group')
    await userEvent.type(screen.getByLabelText(/property name/i), 'X Inn')
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'opera')
    await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    expect(await screen.findByText(/code is incorrect or expired/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/verification code/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- SignupPage`

- [ ] **Step 3: implement** — replace the `DetailsStep` stub with the full form.
  Add a module-level slugify + constants and the browser timezone:

```tsx
import { completeSignup, getInvite, requestOtp, SignupError, type CompletePayload } from '../api/signup'

const SUPPORTED_PMS = [
  { value: 'opera', label: 'Opera' },
  { value: 'autoclerk', label: 'AutoClerk' },
  { value: 'other', label: 'Other — my PMS isn’t listed' },
] as const
const JURISDICTIONS = ['US-CA', 'US-FL'] as const

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 63)
}

function DetailsStep({
  token, cell, onDone,
}: { token: string; email: string; cell: string; onDone: (r: { alias: string; supported: boolean }) => void }) {
  const [otp, setOtp] = useState('')
  const [workspaceName, setWorkspaceName] = useState('')
  const [alias, setAlias] = useState('')
  const [aliasEdited, setAliasEdited] = useState(false)
  const [propertyName, setPropertyName] = useState('')
  const [pms, setPms] = useState<'opera' | 'autoclerk' | 'other'>('opera')
  const [pmsOther, setPmsOther] = useState('')
  const [jurisdiction, setJurisdiction] = useState<string>('US-CA')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const effectiveAlias = aliasEdited ? alias : slugify(workspaceName)

  async function submit() {
    setBusy(true); setError(null)
    const payload: CompletePayload = {
      token, otp, workspace_name: workspaceName, workspace_alias: effectiveAlias,
      property_name: propertyName, pms_source: pms, wage_jurisdiction: jurisdiction,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      cell, password,
      ...(pms === 'other' ? { pms_other_name: pmsOther } : {}),
    }
    try {
      const res = await completeSignup(payload)
      onDone({ alias: res.org_alias, supported: res.pms_supported })
    } catch (e) {
      setError(
        e instanceof SignupError && e.status === 403
          ? 'That code is incorrect or expired.'
          : e instanceof SignupError && e.status === 429
            ? 'Too many requests — please wait and try again.'
            : 'Something didn’t go through. Check your details and try again.',
      )
    } finally { setBusy(false) }
  }

  return (
    <form className="mt-6 space-y-4"
          onSubmit={(e) => { e.preventDefault(); void submit() }}>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <Field label="Verification code" value={otp} onChange={setOtp} />
      <Field label="Workspace name" value={workspaceName} onChange={setWorkspaceName} />
      <Field label="Workspace URL" value={effectiveAlias}
             onChange={(v) => { setAliasEdited(true); setAlias(v) }} />
      <Field label="Property name" value={propertyName} onChange={setPropertyName} />
      <label className="block text-sm">PMS
        <select aria-label="PMS" value={pms}
                onChange={(e) => setPms(e.target.value as typeof pms)}
                className="mt-1 w-full rounded border px-3 py-2">
          {SUPPORTED_PMS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </label>
      {pms === 'other' && (
        <Field label="Which PMS do you use?" value={pmsOther} onChange={setPmsOther} />
      )}
      <label className="block text-sm">Jurisdiction
        <select aria-label="Jurisdiction" value={jurisdiction}
                onChange={(e) => setJurisdiction(e.target.value)}
                className="mt-1 w-full rounded border px-3 py-2">
          {JURISDICTIONS.map((j) => <option key={j} value={j}>{j}</option>)}
        </select>
      </label>
      <label className="block text-sm">Password
        <input aria-label="Password" type="password" value={password}
               onChange={(e) => setPassword(e.target.value)}
               className="mt-1 w-full rounded border px-3 py-2" />
      </label>
      <button type="submit" disabled={busy}
              className="w-full rounded bg-brand px-4 py-2 text-white">
        Create workspace
      </button>
    </form>
  )
}

function Field({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="block text-sm">{label}
      <input aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
             className="mt-1 w-full rounded border px-3 py-2" />
    </label>
  )
}
```
  Update `SignupFlow` to store the completion result and pass a typed `onDone`
  that sets `step='done'` and stashes `{ alias, supported }` for Task 6.

> **Resolve at implementation:** the "Workspace URL" label maps to the alias
> field; keep labels matching the tests' `getByLabelText` regexes
> (`/workspace name/i`, `/workspace url/i`, `/property name/i`, `/pms/i`,
> `/jurisdiction/i`, `/password/i`, `/which pms/i`). `/pms/i` must match the PMS
> `<select>` but not "Which PMS" — give the select `aria-label="PMS"` and the
> other field the longer label so the regex resolves unambiguously (Testing
> Library throws on multiple matches — verify). Client-side password length /
> alias-format validation mirroring the server can be added, but the server
> remains the source of truth; keep it minimal.

- [ ] **Step 4: run GREEN** — `npm run test -- SignupPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(signup-spa): details step (fields, PMS-other, auto-slug, submit)"
```

---

## Task 6 — Success step + OIDC handoff

**Files:** Modify `frontend/src/pages/SignupPage.tsx`, `SignupPage.test.tsx`.

- [ ] **Step 1: failing tests** — add to `SignupPage.test.tsx`

```tsx
vi.mock('../auth/oidc', () => ({ login: vi.fn() }))
import { login } from '../auth/oidc'

async function completeTo(supported: boolean, otherName?: string) {
  vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
  vi.mocked(requestOtp).mockResolvedValue(undefined)
  vi.mocked(completeSignup).mockResolvedValue({ org_alias: 'sunset-group', pms_supported: supported })
  renderSignup()
  await userEvent.type(await screen.findByLabelText(/mobile/i), '+15550000000')
  await userEvent.click(screen.getByRole('button', { name: /send code/i }))
  await userEvent.type(await screen.findByLabelText(/verification code/i), '123456')
  await userEvent.type(screen.getByLabelText(/workspace name/i), 'Sunset Group')
  await userEvent.type(screen.getByLabelText(/property name/i), 'Sunset Inn')
  if (otherName) {
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'other')
    await userEvent.type(screen.getByLabelText(/which pms/i), otherName)
  } else {
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'opera')
  }
  await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
  await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
  await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
}

describe('SignupPage — success + handoff', () => {
  it('supported PMS: confirms the workspace is ready and hands off with login_hint', async () => {
    await completeTo(true)
    expect(await screen.findByText(/your workspace.*is ready/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /go to your workspace/i }))
    expect(login).toHaveBeenCalledWith('owner@hotel.test')
  })

  it('unsupported PMS: says the PMS will be enabled later', async () => {
    await completeTo(false, 'SkyTouch')
    expect(await screen.findByText(/don'?t support skytouch yet/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- SignupPage`

- [ ] **Step 3: implement** — add a `DoneStep` and render it when `step==='done'`.
  `SignupFlow` holds `result: { alias; supported } | null` and the entered PMS
  name for the copy:

```tsx
import { login } from '../auth/oidc'

function DoneStep({ email, supported, pmsName }: { email: string; supported: boolean; pmsName: string }) {
  return (
    <div className="mt-6 space-y-4 text-center">
      {supported ? (
        <p>Your workspace and property are ready.</p>
      ) : (
        <p>
          Your workspace is ready. We don’t support {pmsName} yet — we’ve logged
          it and will email {email} when it’s live.
        </p>
      )}
      <button type="button" onClick={() => void login(email)}
              className="w-full rounded bg-brand px-4 py-2 text-white">
        Go to your workspace
      </button>
    </div>
  )
}
```
  Wire `SignupFlow`: on `DetailsStep`'s `onDone({alias, supported})`, store the
  result + the chosen PMS display name and set `step='done'`, then render
  `<DoneStep email={email} supported={result.supported} pmsName={pmsName} />`.
  (Lift `pms`/`pmsOther` display name up, or pass it through `onDone`.)

> **Resolve at implementation:** pass the unsupported PMS display name to
> `DoneStep` (either lift the PMS name into `SignupFlow` or include it in the
> `onDone` payload). Keep the supported-copy matching `/your workspace.*is ready/i`
> and the unsupported-copy matching `/don't support {name} yet/i`.

- [ ] **Step 4: run GREEN** — `npm run test -- SignupPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(signup-spa): success step + OIDC handoff with login_hint"
```

---

## Self-review checklist

- [ ] `src/api/signup.ts` uses bare `fetch` (no `authHeaders`) — no bearer/X-Active-Org
  leaks on the public endpoints (Task 1 asserts the absent Authorization header).
- [ ] `/signup` renders unguarded (RootShell allowlist) and reads `?token=`.
- [ ] Fail-closed: an invalid invite shows ONE generic message and NO form; wrong
  OTP → inline retry staying on the details step; 429 → back-off copy.
- [ ] `pms_source` "other" reveals + requires a name; supported PMS omits it.
- [ ] Alias auto-slugs from the workspace name and stays editable; timezone is
  browser-detected and sent.
- [ ] Success copy branches on `pms_supported`; the CTA calls `login(email)`.
- [ ] Every task ran `npm run lint && npm run test && npm run build` green before commit.

## Deferred / follow-ups

- **e2e (deferred, with a concrete blocker):** the Playwright harness exists but
  every spec authenticates via `global-setup` + `scripts/e2e_backend.py`, and the
  signup flow is unauthenticated AND its OTP code is delivered via the notifier
  with no retrieval API. A real e2e needs `scripts/e2e_backend.py` to mint+expose
  an invite token and surface the OTP (e.g. a file-writing notifier), plus a
  `test.use({ storageState: { cookies: [], origins: [] } })` opt-out. Tracked as
  a follow-up; the vitest component tests + the backend integration tests cover
  the flow in the meantime.
- Warm Track-A "front door" skin once Track A merges; client-side field
  validation parity; promoting `/signup` styling.
