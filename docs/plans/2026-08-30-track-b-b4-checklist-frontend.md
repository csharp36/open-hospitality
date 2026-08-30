# Track B / B4 — open-items checklist (FRONTEND) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (fresh subagent per task, full red→green→commit TDD loop, review between tasks).
> Steps use `- [ ]` checkboxes.

**Goal:** Give the operator the three surfaces §7 of the design specifies — a
permanent `/setup` page, a sidebar entry with a count badge, and a dashboard
card that retires when setup is finished — over the shipped
`GET /api/checklist`, and resolve the demand-feed gap as **D-B4.8**.

**Architecture:** One `useChecklist()` hook owning the sole TanStack Query key
(`['checklist']`), consumed by all three surfaces so they are a single fetch
and cannot disagree. A `ChecklistPage` at `/setup` groups required and optional
items and carries the dismiss/restore controls (org-admin gated). `Layout`
gains a top-level nav entry with a badge; `DashboardPage` gains a compact card.
One small backend edit lands first: `where` becomes nullable and
`unavailable_reason` joins the wire (D-B4.8).

**Tech Stack:** React 19, TypeScript, Vite, TanStack Router + Query, Tailwind,
Vitest + Testing Library. Frontend dir: `frontend/`. Backend: Python 3.12,
FastAPI, pydantic.

Implements §7 of
[`docs/design/2026-08-30-track-b-b4-open-items-checklist-design.md`](../design/2026-08-30-track-b-b4-open-items-checklist-design.md).
The backend shipped as **PR #106**; its plan is
[`2026-08-30-track-b-b4-checklist-backend.md`](2026-08-30-track-b-b4-checklist-backend.md)
(read its banner — those code samples are deliberately not what shipped).
Branch: `feat/track-b-b4-checklist-frontend`.

---

## The two things this plan exists to get right

**1. `all_clear`, never `open_count > 0`.** They are identical in every normal
case and diverge in exactly one: a probe failure. `error_count` items are not
`open`, so a tenant whose probes all failed has `open_count == 0` — and a card
gated on `open_count > 0` would vanish at the precise moment nothing is known.
`all_clear` is false whenever anything is open **or** anything errored, and it
is the only predicate any surface may gate on. **Every visibility decision in
this plan reads `all_clear`.** Tasks 3, 5 and 6 each pin this with a test whose
fixture is `open_count: 0, error_count: 3, all_clear: false` — the shape a
`open_count`-gated implementation gets wrong and nothing else catches.

**2. `error_count > 0` must read as "we could not check these", never as
progress.** An errored item is not a done item and not an open one. It renders
with a danger tone and the words "could not check", and the badge shows `!`
rather than a count that would read as fewer things to do.

## Gates (run for EVERY task before committing)

Frontend tasks, from `frontend/`:

```bash
npm run lint          # oxlint
npm run test          # vitest run
npm run build         # tsc -b && vite build (type-check + bundle)
```

Task 1 is a backend task; from the repo root:

```bash
uv run pytest -q tests/test_checklist.py tests/test_checklist_api.py
uv run mypy src
uv run ruff check src tests
```

## Grounding facts (verified against the code — do not re-guess)

- **Wire contract, as shipped** (`src/usali/checklist_api.py`): `GET
  /api/checklist` → `{items: [{key, title, description, required, where,
  status, detail}], open_count, error_count, all_clear}`. `status ∈
  {"done","open","dismissed","error"}` (`checklist.py:37-40`). `PUT|DELETE
  /api/checklist/{key}/dismissal` → **204**, `org_admin` only; 422 on a
  required key, 404 on an unknown one.
- **`ItemModel` sets `extra="forbid"` and is built with
  `ItemModel(**vars(row))`** (`checklist_api.py:29,54`). The pydantic model is
  therefore coupled to `ItemStatus`'s field set: adding a field to the
  dataclass without adding it to `ItemModel` is a 500, not a type error. Task 1
  edits both.
- **`ITEMS` `where` values as shipped** (`checklist.py:171-207`): `/upload`,
  `/property-config`, `/property-config`, `/payroll`, `/qbo`, `/schedule`,
  `/employees`. Task 1 nulls the middle-three integration ones (D-B4.8).
- **`_probe_payroll` / `_probe_accounting` return `False` unconditionally**
  (D-B4.3) — they are not stubs, they are the correct answer until OH-17.
- **`SchedulePage.tsx:204`**: `const demand = demandQuery.data?.configured ?
  demandQuery.data : undefined` — when the org has no `crm_provider` the page
  renders **no** demand UI at all. This is the concrete fact behind D-B4.8.
- **Routing** (`src/router.tsx`): routes are
  `createRoute({ getParentRoute: () => rootRoute, path, component })`, collected
  in `const routeTree = rootRoute.addChildren([...])` (~line 227). No
  `validateSearch` needed for `/setup`.
- **Nav** (`src/Layout.tsx:74-113`): `SECTIONS: NavSection[]`, where the first
  section has `label: null` and holds only Dashboard. Items are
  `{ to, label, icon, exact?, soon?, show? }`; `show(me)` gates by role and a
  missing `me` is never privileged (`lib/roles.ts`). Rendering is at
  `Layout.tsx:196-260`; the link body is
  `<span className={chipBase}><Icon/></span><span className={labelClass}>{label}</span>`
  and `labelClass` is `'sr-only'` when collapsed.
- **`me`** is already fetched in `Layout` as `useQuery({ queryKey: ['me'],
  queryFn: getMe })`; `hasRole(me, 'org_admin')` (`lib/roles.ts`) is the gate
  helper.
- **UI primitives** (`src/components/ui.tsx`): `Card`, `PageHeader`, `Badge`
  (`tone: 'ok'|'warn'|'danger'|'info'|'neutral'`), `sectionHeadClass`. Badge
  tone class strings deliberately keep the color words — page tests assert on
  them.
- **Icons** (`src/components/icons.tsx`): all are `aria-hidden`, parents carry
  the name. `AlertIcon` and `GaugeIcon` exist; there is no checklist icon —
  Task 4 adds `ChecklistIcon` following the `Icon` wrapper convention
  (24px grid, stroke `currentColor`).
- **API client** (`src/api/client.ts`): `getJson<T>(path)` for reads;
  writes are a hand-rolled `fetch` + `authHeaders({'Content-Type': ...})` +
  `if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }` +
  `if (!res.ok) await raiseApiError(res)` (see `setFiscalCalendar`,
  `removeOoo` at `client.ts:750-771`). A 204 write returns `Promise<void>` and
  must **not** call `res.json()`.
- **Page-test convention** (`src/pages/PropertyConfigPage.test.tsx:1-45`):
  `vi.mock('../api/client', async (importOriginal) => ({...await
  importOriginal(), fn: vi.fn()}))`, render via
  `createAppRouter(createMemoryHistory({ initialEntries: ['/route'] }))` inside
  `<QueryClientProvider><AuthContext.Provider value={AUTHED_CONTEXT}>
  <RouterProvider/></AuthContext.Provider></QueryClientProvider>`, and
  `AUTHED_CONTEXT` comes from `src/test/fixtures`.
- **`errorMessage(err)`** (`src/lib/errors.ts`) renders an `ApiError`'s bare
  `detail`; use it for every failure string.

## File structure

```
MOD  src/usali/checklist.py                     # Task 1 — where: str|None, unavailable_reason
MOD  src/usali/checklist_api.py                 # Task 1 — ItemModel mirrors the dataclass
MOD  tests/test_checklist.py                    # Task 1 — the paired invariant
MOD  tests/test_checklist_api.py                # Task 1 — the field on the wire

NEW  frontend/src/api/checklist.ts              # Task 2 — getChecklist / dismissItem / restoreItem
NEW  frontend/src/api/checklist.test.ts         # Task 2
MOD  frontend/src/api/types.ts                  # Task 2 — ChecklistItem / Checklist
NEW  frontend/src/lib/useChecklist.ts           # Task 3 — THE query key, + derived helpers
NEW  frontend/src/lib/useChecklist.test.ts      # Task 3
NEW  frontend/src/pages/ChecklistPage.tsx       # Tasks 4-5
NEW  frontend/src/pages/ChecklistPage.test.tsx  # Tasks 4-5
MOD  frontend/src/router.tsx                    # Task 4 — /setup
MOD  frontend/src/components/icons.tsx          # Task 4 — ChecklistIcon
MOD  frontend/src/Layout.tsx                    # Task 6 — nav entry + badge
MOD  frontend/src/App.test.tsx                  # Task 6
MOD  frontend/src/pages/DashboardPage.tsx       # Task 7 — the card
MOD  frontend/src/pages/DashboardPage.test.tsx  # Task 7
```

---

## Task 1 — D-B4.8 on the wire: nullable `where` + `unavailable_reason`

**Files:** Modify `src/usali/checklist.py`, `src/usali/checklist_api.py`,
`tests/test_checklist.py`, `tests/test_checklist_api.py`.

Resolves the gap design §4 recorded. No migration: `where` is not persisted,
so the `org_checklist_override` CHECK mirror is untouched.

- [ ] **Step 1: failing tests** — add to `tests/test_checklist.py`:

```python
def test_where_and_unavailable_reason_are_paired():
    """D-B4.8: an item either routes somewhere real or says why it does not.
    Exactly one of the two, never both and never neither — an item with a
    reason AND a link would render a link the reason contradicts, and one
    with neither is the dead end this decision exists to remove."""
    for item in ITEMS:
        assert (item.where is None) == (item.unavailable_reason is not None), item.key


def test_the_integration_items_have_no_connect_surface_yet():
    """The three OH-17 items, named explicitly. This test is the tripwire that
    OH-17 must delete: when it supplies a connect surface it restores `where`
    and drops the reason, and this assertion fails loudly rather than leaving
    a stale "coming later" string on a working page."""
    by_key = {i.key: i for i in ITEMS}
    for key in ("payroll", "accounting", "demand_feed"):
        assert by_key[key].where is None
        assert "OH-17" in (by_key[key].unavailable_reason or "")
    # Everything else still routes.
    for key in ("first_report", "room_inventory", "fiscal_calendar", "team"):
        assert by_key[key].where is not None
```

  and to `tests/test_checklist_api.py`, inside the existing GET test's org:

```python
def test_get_carries_the_unavailable_reason(...):
    """ItemModel forbids extra fields and is built with **vars(ItemStatus), so
    a field added to the dataclass and NOT to the model is a 500. This asserts
    the pair travels, which is what that coupling can break."""
    body = client.get("/api/checklist").json()
    by_key = {i["key"]: i for i in body["items"]}
    assert by_key["first_report"]["where"] == "/upload"
    assert by_key["first_report"]["unavailable_reason"] is None
    assert by_key["payroll"]["where"] is None
    assert "OH-17" in by_key["payroll"]["unavailable_reason"]
```

> **Resolve at implementation:** copy the `_client(...)` factory and org
> fixture the file's existing GET test already uses; do not invent a new one.

- [ ] **Step 2: run RED** — `uv run pytest -q tests/test_checklist.py tests/test_checklist_api.py`.
  Expected: `TypeError`/`AttributeError` on `unavailable_reason`.

- [ ] **Step 3: implement.** In `checklist.py`:

  - `ChecklistItem`: `where: str | None` and a new
    `unavailable_reason: str | None = None`, with a docstring stating the
    paired invariant and pointing at D-B4.8.
  - `ItemStatus`: same two changes (`where: str | None`, and
    `unavailable_reason: str | None = None` after `detail`).
  - `_status(...)`: pass `unavailable_reason=item.unavailable_reason`
    **unconditionally** — it is a static property of the item, so an `error`
    row carries it too.
  - The three integration entries in `ITEMS`: `where=None` plus a reason. Use
    one sentence each, naming OH-17 so the tripwire test above binds:

    ```python
    _OH17_REASON = (
        "No connect surface yet — per-tenant integration setup arrives with "
        "OH-17. You can dismiss this if you do not plan to connect it."
    )
    ```

    A single shared constant, because the three items share one cause: three
    near-identical strings would drift, and D-B4.8's whole point is that this
    is one class and not three special cases.

  In `checklist_api.py`, `ItemModel`: `where: str | None` and
  `unavailable_reason: str | None = None`.

- [ ] **Step 4: run GREEN** — the two test files, then the full checklist-adjacent set.
- [ ] **Step 5: gates + commit**

```bash
uv run pytest -q tests/test_checklist.py tests/test_checklist_api.py tests/test_checklist_tenancy.py
uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(checklist): an item with no connect surface says why (D-B4.8)"
```

---

## Task 2 — `src/api/checklist.ts` + the response types

**Files:** Create `frontend/src/api/checklist.ts`,
`frontend/src/api/checklist.test.ts`. Modify `frontend/src/api/types.ts`.

A separate module rather than more weight on `client.ts` (1010 lines), the same
call the backend made for `checklist_api.py`. It still imports `authHeaders` /
`raiseApiError` / `redirectToLogin` from `client.ts` — these are authenticated
operator endpoints, **unlike** `api/signup.ts`, which is public and deliberately
bypasses them.

> **Resolve at implementation:** `authHeaders`, `raiseApiError` and
> `redirectToLogin` are module-private in `client.ts` today. Export them (a
> one-word change each, no behavior) rather than duplicating the 401 dance —
> duplicating it is how one copy stops redirecting on session expiry.

- [ ] **Step 1: failing tests** — `frontend/src/api/checklist.test.ts`

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { dismissItem, getChecklist, restoreItem } from './checklist'

vi.mock('../auth/oidc', () => ({ getAccessToken: vi.fn().mockResolvedValue('tok'), login: vi.fn() }))

function mockFetch(status: number, body: unknown = null) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(body === null ? null : JSON.stringify(body), {
      status,
      headers: body === null ? undefined : { 'Content-Type': 'application/json' },
    }),
  )
}

beforeEach(() => vi.restoreAllMocks())

describe('checklist API', () => {
  it('GETs /api/checklist and returns the parsed body', async () => {
    const f = mockFetch(200, { items: [], open_count: 0, error_count: 0, all_clear: true })
    const out = await getChecklist()
    expect(out.all_clear).toBe(true)
    expect(f.mock.calls[0]![0]).toBe('/api/checklist')
  })

  it('dismissItem PUTs the dismissal and tolerates a 204 with no body', async () => {
    const f = mockFetch(204)
    await expect(dismissItem('payroll')).resolves.toBeUndefined()
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/checklist/payroll/dismissal')
    expect((init as RequestInit).method).toBe('PUT')
  })

  it('restoreItem DELETEs the dismissal', async () => {
    const f = mockFetch(204)
    await restoreItem('payroll')
    expect((f.mock.calls[0]![1] as RequestInit).method).toBe('DELETE')
  })

  it('surfaces the 422 detail when a required item is dismissed', async () => {
    mockFetch(422, { detail: 'first_report is required and cannot be dismissed' })
    await expect(dismissItem('first_report')).rejects.toThrow(/cannot be dismissed/)
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- checklist`

- [ ] **Step 3: implement.** Add to `src/api/types.ts` (near the other feature
  blocks, with the mirror comment the file uses elsewhere):

```ts
// --- Onboarding open-items checklist (Track B/B4) ----------------------------
// Mirrors ItemModel / ChecklistModel in src/usali/checklist_api.py.

export type ChecklistStatus = 'done' | 'open' | 'dismissed' | 'error'

export interface ChecklistItem {
  key: string
  title: string
  description: string
  required: boolean
  /** The SPA route that closes this item, or null when there is none yet —
   *  in which case `unavailable_reason` says why (D-B4.8). Exactly one of the
   *  two is set. */
  where: string | null
  unavailable_reason: string | null
  status: ChecklistStatus
  /** Populated only for `status: 'error'` — the probe's exception type. */
  detail: string | null
}

export interface Checklist {
  items: ChecklistItem[]
  open_count: number
  error_count: number
  /** Zero open AND zero errors. The ONLY predicate any surface gates on: an
   *  item we could not check is not a finished item, and `open_count` alone
   *  goes to zero on a total probe failure. */
  all_clear: boolean
}
```

  Then `src/api/checklist.ts` with `getChecklist()`, `dismissItem(key)` and
  `restoreItem(key)` following the `setFiscalCalendar`/`removeOoo` shape. The
  two writes return `Promise<void>` and never touch `res.json()` — the
  endpoints are 204.

- [ ] **Step 4: run GREEN** — `npm run test -- checklist`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(checklist-spa): typed client for the checklist endpoints"
```

---

## Task 3 — `useChecklist()`: one query key, one set of derived facts

**Files:** Create `frontend/src/lib/useChecklist.ts`,
`frontend/src/lib/useChecklist.test.ts`.

Design §7's "all three surfaces share one TanStack Query key" is only true if
the key literal exists once. This hook is that once. A surface that calls
`useQuery({queryKey: ['checklist']})` by hand is a bug — the review checklist
greps for it.

- [ ] **Step 1: failing tests** — `frontend/src/lib/useChecklist.test.ts`

```ts
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

vi.mock('../api/checklist', () => ({ getChecklist: vi.fn() }))
import { getChecklist } from '../api/checklist'
import { CHECKLIST_KEY, badgeLabel, useChecklist } from './useChecklist'

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

beforeEach(() => vi.clearAllMocks())

describe('useChecklist', () => {
  it('two consumers of the hook cause exactly ONE fetch', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 2, error_count: 0, all_clear: false,
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const w = wrapper(qc)
    renderHook(() => useChecklist(), { wrapper: w })
    renderHook(() => useChecklist(), { wrapper: w })
    await waitFor(() => expect(getChecklist).toHaveBeenCalledTimes(1))
    expect(CHECKLIST_KEY).toEqual(['checklist'])
  })
})

describe('badgeLabel', () => {
  it('is null when all_clear — the badge retires at zero', () => {
    expect(badgeLabel({ items: [], open_count: 0, error_count: 0, all_clear: true })).toBeNull()
  })

  it('counts the open items', () => {
    expect(badgeLabel({ items: [], open_count: 3, error_count: 0, all_clear: false }))
      .toEqual({ text: '3', tone: 'warn', title: '3 items still to set up' })
  })

  // THE divergence case. A total probe failure leaves open_count at 0 while
  // nothing at all is known. A badge reading "0" — or no badge — would say
  // "finished" at the exact moment the operator most needs to look.
  it('shows "!" and never "0" when every probe failed', () => {
    const badge = badgeLabel({ items: [], open_count: 0, error_count: 3, all_clear: false })
    expect(badge).not.toBeNull()
    expect(badge!.text).toBe('!')
    expect(badge!.tone).toBe('danger')
    expect(badge!.title).toMatch(/could not check/i)
  })

  it('leads with the errors when items are both open and unchecked', () => {
    const badge = badgeLabel({ items: [], open_count: 2, error_count: 1, all_clear: false })
    expect(badge!.tone).toBe('danger')
    expect(badge!.title).toMatch(/could not check/i)
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- useChecklist`

- [ ] **Step 3: implement** — `src/lib/useChecklist.ts`:

```ts
// THE single TanStack Query key for the onboarding checklist (design §7). The
// /setup page, the sidebar badge and the dashboard card all read it through
// this hook, so they are one fetch and cannot disagree. Do not write
// `useQuery({ queryKey: ['checklist'] })` anywhere else.

export const CHECKLIST_KEY = ['checklist'] as const

export function useChecklist() {
  return useQuery({ queryKey: CHECKLIST_KEY, queryFn: getChecklist, staleTime: 60_000 })
}

export function useInvalidateChecklist() {
  const qc = useQueryClient()
  return () => void qc.invalidateQueries({ queryKey: CHECKLIST_KEY })
}
```

  plus the pure derivations, each exported for direct unit test:

  - `badgeLabel(data): { text, tone, title } | null` — **null iff
    `all_clear`**, never on `open_count === 0`. When `error_count > 0` the text
    is `'!'` and the tone `'danger'`, because a numeral that omits the
    unchecked items reads as progress.
  - `groupItems(items)` → `{ required, optional }`, preserving registry order
    within each group.

  `staleTime: 60_000` because `Layout` mounts this on every authenticated page:
  the checklist changes at human speed, and a refetch on every navigation buys
  nothing.

- [ ] **Step 4: run GREEN** — `npm run test -- useChecklist`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(checklist-spa): useChecklist owns the single query key"
```

---

## Task 4 — `/setup`: the page, its route, and item rendering

**Files:** Create `frontend/src/pages/ChecklistPage.tsx`,
`frontend/src/pages/ChecklistPage.test.tsx`. Modify `frontend/src/router.tsx`,
`frontend/src/components/icons.tsx`.

Read-only in this task; the dismiss/restore controls are Task 5.

- [ ] **Step 1: failing tests** — `frontend/src/pages/ChecklistPage.test.tsx`

```tsx
vi.mock('../api/checklist', () => ({
  getChecklist: vi.fn(), dismissItem: vi.fn(), restoreItem: vi.fn(),
}))
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getMe: vi.fn(), getProperties: vi.fn(),
}))

function item(over: Partial<ChecklistItem> = {}): ChecklistItem {
  return {
    key: 'first_report', title: 'Upload your first PMS report',
    description: 'Drop a night-audit export.', required: true,
    where: '/upload', unavailable_reason: null, status: 'open', detail: null,
    ...over,
  }
}

function renderSetup() { /* memory history at '/setup', per the house pattern */ }

describe('ChecklistPage', () => {
  it('groups required and optional items under their own headings', async () => { … })

  it('links an item that has a `where`', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item()], open_count: 1, error_count: 0, all_clear: false,
    })
    renderSetup()
    const link = await screen.findByRole('link', { name: /upload your first pms report/i })
    expect(link).toHaveAttribute('href', '/upload')
  })

  // D-B4.8. The item is un-closeable today, and says so — it is never a link
  // to a page where the feature is invisible.
  it('renders an item with no `where` as a non-link carrying its reason', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({
        key: 'payroll', title: 'Connect payroll', required: false, where: null,
        unavailable_reason: 'No connect surface yet — per-tenant integration setup arrives with OH-17.',
      })],
      open_count: 1, error_count: 0, all_clear: false,
    })
    renderSetup()
    expect(await screen.findByText(/connect payroll/i)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /connect payroll/i })).toBeNull()
    expect(screen.getByText(/arrives with OH-17/i)).toBeInTheDocument()
  })

  it('renders an errored item as unchecked, never as progress', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({ status: 'error', detail: 'OperationalError' })],
      open_count: 0, error_count: 1, all_clear: false,
    })
    renderSetup()
    expect(await screen.findByText(/could not check/i)).toBeInTheDocument()
    expect(screen.queryByText(/^done$/i)).toBeNull()
  })

  it('says setup is finished when all_clear', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({ status: 'done' })], open_count: 0, error_count: 0, all_clear: true,
    })
    renderSetup()
    expect(await screen.findByText(/nothing left to set up/i)).toBeInTheDocument()
  })

  it('shows a loud failure when the checklist itself cannot be fetched', async () => {
    vi.mocked(getChecklist).mockRejectedValue(new ApiError(503, 'upstream down'))
    renderSetup()
    expect(await screen.findByText(/upstream down/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- ChecklistPage`

- [ ] **Step 3: implement.**

  `ChecklistPage.tsx` — `PageHeader` ("Setup" / "What is left to configure.
  Nothing here blocks reporting."), then a `Card` per group with an `h2`, then
  one `ItemRow` per item. `ItemRow` renders, in order: a status glyph, the
  title (a `<Link to={where}>` when `where !== null`, otherwise plain text),
  the description, and then — when `where === null` — the
  `unavailable_reason` on its own line in `text-ink-muted`.

  Status → `Badge` tone and word:

  | status | tone | word |
  |---|---|---|
  | `done` | `ok` | Done |
  | `open` | `warn` | To do |
  | `dismissed` | `neutral` | Dismissed |
  | `error` | `danger` | Could not check |

  `error` says **"Could not check"**, not "Failed" and never a checkmark — the
  operator must be able to tell "4 things to do" from "4 things we could not
  check". Show `detail` beside it when present.

  Unlike every other page in the app this one takes **no property** — the
  checklist is org-scoped, so there is no `useGlobalProperty()` and no
  "No property selected yet" state.

  `router.tsx`: a `checklistRoute` at `/setup` rendering `ChecklistPage`,
  added to `rootRoute.addChildren([...])`.

  `icons.tsx`: `ChecklistIcon` in the file's `Icon` wrapper — a clipboard
  outline with a tick.

- [ ] **Step 4: run GREEN** — `npm run test -- ChecklistPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(checklist-spa): the /setup page, with an honest un-closeable item"
```

---

## Task 5 — dismiss and restore, org-admin gated

**Files:** Modify `frontend/src/pages/ChecklistPage.tsx`,
`frontend/src/pages/ChecklistPage.test.tsx`.

Dismissal is the one action available on the three OH-17 items, so it is what
keeps them from being dead ends and what keeps `all_clear` reachable today.
Restore matters just as much: a dismissal the UI cannot undo is a trap.

- [ ] **Step 1: failing tests** — add to `ChecklistPage.test.tsx`

```tsx
describe('ChecklistPage — dismissal', () => {
  it('an org admin can dismiss an optional item, and the list refetches', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    vi.mocked(dismissItem).mockResolvedValue(undefined)
    // getChecklist: open first, dismissed on the refetch.
    renderSetup()
    await userEvent.click(await screen.findByRole('button', { name: /dismiss connect payroll/i }))
    expect(dismissItem).toHaveBeenCalledWith('payroll')
    expect(await screen.findByRole('button', { name: /restore connect payroll/i })).toBeInTheDocument()
  })

  it('offers no dismiss control on a REQUIRED item', async () => { … })

  it('offers no dismiss control to a non-admin', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    …
    expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull()
  })

  it('surfaces a refused dismissal instead of silently doing nothing', async () => {
    vi.mocked(dismissItem).mockRejectedValue(new ApiError(422, 'first_report is required'))
    …
    expect(await screen.findByText(/first_report is required/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- ChecklistPage`

- [ ] **Step 3: implement.** A `useMutation` per verb, both
  `onSuccess: invalidate` from `useInvalidateChecklist()` (Task 3) — that
  single invalidation is what updates the page, the sidebar badge and the
  dashboard card at once, and is the payoff of the shared key.

  The control renders when **all** of: `me` holds `org_admin`
  (`hasRole(me, 'org_admin')`), and `!item.required`. Its accessible name
  includes the item title (`aria-label={`Dismiss ${item.title}`}`) so a page of
  seven rows has seven distinguishable buttons. A `dismissed` item gets
  **Restore** instead.

  Required items never render the control — the server's 422 is the wall, and
  offering a button that can only fail is the dishonesty this feature exists to
  avoid. The 422 branch is still handled: the endpoint is reachable, and a
  registry edit could put a key on the wrong side.

  Errors render inline under the row via `errorMessage(mutation.error)`.

> **Deferred, deliberately:** the endpoint accepts an optional `note` (≤200
> chars) and the column stores it, but this slice sends none — a note turns a
> one-click button into a form, and §7 does not ask for one. Adding it later is
> a pure frontend change against an unchanged contract. Worth doing when there
> is a second admin to explain the decision to.

- [ ] **Step 4: run GREEN** — `npm run test -- ChecklistPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(checklist-spa): dismiss and restore an optional item"
```

---

## Task 6 — the sidebar entry and its badge

**Files:** Modify `frontend/src/Layout.tsx`, `frontend/src/App.test.tsx`.

Design §7: a top-level entry **above** the Accounting section. It is the
permanent home and survives to zero open items, because OH-17 will need
somewhere to manage connected integrations — so the **entry never hides**; only
the badge retires.

- [ ] **Step 1: failing tests** — add to `frontend/src/App.test.tsx`

```tsx
// App.test.tsx already mocks ./api/client; add ./api/checklist alongside it.
vi.mock('./api/checklist', () => ({ getChecklist: vi.fn(), dismissItem: vi.fn(), restoreItem: vi.fn() }))

describe('app shell — setup nav', () => {
  it('shows the Setup entry with an open-item count', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 3, error_count: 0, all_clear: false,
    })
    renderApp()
    expect(await screen.findByRole('link', { name: /setup/i })).toBeInTheDocument()
    expect(await screen.findByText('3')).toBeInTheDocument()
  })

  it('keeps the entry but drops the badge at all_clear', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 0, error_count: 0, all_clear: true,
    })
    renderApp()
    expect(await screen.findByRole('link', { name: /setup/i })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('setup-badge')).toBeNull())
  })

  // THE divergence case, at the badge.
  it('badges "!" — not "0", not nothing — when every probe failed', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 0, error_count: 4, all_clear: false,
    })
    renderApp()
    expect(await screen.findByTestId('setup-badge')).toHaveTextContent('!')
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- App`

- [ ] **Step 3: implement.** Add a section to `SECTIONS` between the Dashboard
  section and `Employee Management`:

```ts
{ label: null, items: [{ to: '/setup', label: 'Setup', icon: ChecklistIcon }] },
```

  §7 says "above the Accounting section"; placing it directly under Dashboard
  puts it above **both** grouped sections, which is the same requirement met
  more strongly — it belongs to neither group, and burying it under Employee
  Management would hide the first-run surface behind eleven links.

  Rendering: `SidebarContent` calls `useChecklist()` once and renders the badge
  inside the `/setup` link, after `<span className={labelClass}>`:

```tsx
{badge !== null && (
  <span
    data-testid="setup-badge"
    title={badge.title}
    className={`ml-auto rounded-full px-1.5 py-0.5 text-[10px] font-bold ${
      badge.tone === 'danger' ? 'bg-danger-red-soft text-danger-red' : 'bg-warn-amber-soft text-warn-amber'
    }`}
  >
    {badge.text}
  </span>
)}
```

  **Collapsed mode:** the label goes `sr-only` but the badge must NOT — the
  count is the whole reason a collapsed sidebar still points at setup. Keep it
  visible, and keep `title={badge.title}` so the "could not check" wording
  survives when the numeral is all that is left.

  No `show` gate: reading the checklist needs only the router's operator gate,
  so every operator who can see the sidebar can see the entry. The dismiss
  controls inside are separately gated (Task 5).

  While the query is pending or has failed, render **no badge** — the badge is
  an ambient pointer and cannot honestly report a number it does not have. The
  loud failure belongs on `/setup`, which is where the operator went to find
  out (Task 4).

  `SidebarContent` is rendered **twice** (desktop `<aside>` + mobile drawer).
  Both mount `useChecklist()`; TanStack dedupes on the shared key, so this is
  one fetch. Do not lift it into `Layout` and prop-drill.

- [ ] **Step 4: run GREEN** — `npm run test -- App`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(checklist-spa): Setup nav entry with a badge that retires at all_clear"
```

---

## Task 7 — the dashboard card, retiring at `all_clear`

**Files:** Modify `frontend/src/pages/DashboardPage.tsx`,
`frontend/src/pages/DashboardPage.test.tsx`.

A first-run pointer to `/setup`, not a second checklist. It renders **while
`all_clear` is false** and disappears for good when setup is finished, leaving
the dashboard to pure operations.

- [ ] **Step 1: failing tests** — add to `DashboardPage.test.tsx`

```tsx
vi.mock('../api/checklist', () => ({ getChecklist: vi.fn(), dismissItem: vi.fn(), restoreItem: vi.fn() }))

describe('DashboardPage — setup card', () => {
  it('points at /setup while items are open', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 2, error_count: 0, all_clear: false,
    })
    renderDashboard()
    const link = await screen.findByRole('link', { name: /finish setting up/i })
    expect(link).toHaveAttribute('href', '/setup')
    expect(screen.getByText(/2 things left/i)).toBeInTheDocument()
  })

  it('retires at all_clear', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 0, error_count: 0, all_clear: true,
    })
    renderDashboard()
    await waitFor(() => expect(screen.queryByRole('link', { name: /finish setting up/i })).toBeNull())
  })

  // THE divergence case, and the reason the card gates on all_clear. An
  // open_count-gated card would vanish here — at the one moment the operator
  // needs to know something is wrong.
  it('stays, and says nothing could be checked, when every probe failed', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 0, error_count: 5, all_clear: false,
    })
    renderDashboard()
    expect(await screen.findByRole('link', { name: /finish setting up/i })).toBeInTheDocument()
    expect(screen.getByText(/could not check/i)).toBeInTheDocument()
    expect(screen.queryByText(/0 things left/i)).toBeNull()
  })
})
```

- [ ] **Step 2: run RED** — `npm run test -- DashboardPage`

- [ ] **Step 3: implement.** A `SetupCard` component in `DashboardPage.tsx`,
  rendered directly under the hero and above the KPI grid — the first-run
  operator has no KPIs yet, so a card below an empty grid is a card below the
  fold.

```tsx
function SetupCard() {
  const { data } = useChecklist()
  // all_clear, NOT open_count > 0. They differ only on a probe failure, where
  // open_count is 0 while nothing is known — gating on it would retire this
  // card at exactly the moment it matters. (design §7)
  if (data === undefined || data.all_clear) return null
  …
}
```

  Copy: the headline is always **"Finish setting up"** (the link's accessible
  name, stable across both branches so one locator finds it). Beneath it:

  - `error_count === 0` → `{open_count} things left before your setup is complete.`
  - `error_count > 0` → `We could not check {error_count} setup items.` plus
    the open count when there is one. Never a total that folds the two
    together, and never a `0 things left` — the numeral must not stand in for
    "we don't know".

  Tone: `warn` normally, `danger` when `error_count > 0`. `data === undefined`
  covers both pending and error: the card is ambient, and a dashboard is not
  where a fetch failure gets announced.

- [ ] **Step 4: run GREEN** — `npm run test -- DashboardPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(checklist-spa): dashboard setup card, gated on all_clear"
```

---

## Task 8 — ROADMAP status

**Files:** Modify `docs/ROADMAP.md`.

- [ ] Flip **OH-18** (line ~316) from `in-progress — backend shipped, frontend
  pending` to `shipped`, and reword §2.2 in the past tense.
- [ ] In §2.1 (OH-17), add one line: the three integration items now carry
  `where: null` + an `unavailable_reason` naming OH-17, and OH-17's frontend
  work includes restoring their `where` — `test_the_integration_items_have_no_connect_surface_yet`
  (Task 1) is the tripwire that fails when it does not.
- [ ] Gates: `npm run lint && npm run test && npm run build` from `frontend/`
  (docs-only, but keep the branch green), then commit.

---

## Self-review checklist

- [ ] **Nothing gates on `open_count`.** `grep -rn "open_count" frontend/src`
  returns only `badgeLabel`, the card's copy, and tests. Every *visibility*
  decision — badge, card — reads `all_clear`.
- [ ] Three tests use the `open_count: 0, error_count: N, all_clear: false`
  fixture (Tasks 3, 6, 7) and each asserts the surface **stays**.
- [ ] No surface renders a `0`, a checkmark, or the word "done" for an errored
  item; "could not check" appears on the page, the badge title, and the card.
- [ ] `grep -rn "queryKey: \['checklist'\]" frontend/src` matches only
  `lib/useChecklist.ts`.
- [ ] Every item with `where === null` renders as a non-link **and** shows its
  `unavailable_reason`; the paired invariant is pinned backend-side (Task 1).
- [ ] The three OH-17 items are dismissible, so `all_clear` is reachable today:
  three required items done + three integrations dismissed.
- [ ] Dismiss/restore appear only for `org_admin` on optional items, carry the
  item title in their accessible name, and surface a refusal inline.
- [ ] The `/setup` nav entry survives `all_clear`; only the badge retires.
- [ ] The badge stays visible (not `sr-only`) in the collapsed sidebar.
- [ ] `/setup` reports a fetch failure loudly; badge and card stay silent.
- [ ] Every task ran `npm run lint && npm run test && npm run build` green
  (Task 1: `pytest` + `mypy src` + `ruff`) before committing.

## Deferred / follow-ups

- **The dismissal `note`.** The endpoint and column take one (≤200 chars); no
  UI sends it (Task 5). A pure frontend change when it earns a form.
- **Confetti at zero open items** — design §10, out of scope, but `all_clear`
  is the predicate it will hang on.
- **e2e.** The Playwright harness authenticates via `global-setup` +
  `scripts/e2e_backend.py`; a `/setup` spec needs that fixture to seed an org
  with a known checklist shape. The vitest tests cover the render logic and the
  backend tests cover the contract; note it, do not force it.
- **OH-17 restores the three `where` values** and deletes both the
  `_OH17_REASON` constant and the tripwire test from Task 1.
