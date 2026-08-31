// Smoke tests on the nav shell: the router renders the layout with its nav
// links and the SOS page heading at `/sos`, plus the dark-mode toggle and the
// role gating on individual entries. Also covers the entry route `/`, which
// restores the last visited page and falls back to the dashboard on a first
// visit. The second describe covers the Setup entry and its checklist badge.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'

vi.mock('./api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api/client')>()),
  getMe: vi.fn(),
}))

vi.mock('./api/checklist', () => ({
  getChecklist: vi.fn(),
  dismissItem: vi.fn(),
  restoreItem: vi.fn(),
}))

import { getMe } from './api/client'
import { getChecklist } from './api/checklist'
import { createAppRouter } from './router'
import type { Checklist } from './api/types'
import { CHECKLIST_KEY } from './lib/useChecklist'
import { AuthContext, type AuthContextValue } from './auth/authContext'
import { AUTHED_CONTEXT } from './test/fixtures'

function renderApp(auth: AuthContextValue = AUTHED_CONTEXT, initialPath = '/sos') {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [initialPath] }))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
  // Returned so a test can anchor on the query's own state rather than on a
  // link that is already in the DOM, and can drive a refetch.
  return queryClient
}

// File-scoped rather than per-describe: the sidebar reads the checklist on
// every authenticated page, so every test in this file mounts that query and
// an unstubbed one would resolve undefined. `all_clear` by default so the
// badge stays out of the way of the tests that are not about it.
beforeEach(() => {
  vi.mocked(getMe).mockResolvedValue({ subject: '', username: '', roles: [] })
  vi.mocked(getChecklist).mockResolvedValue({
    items: [],
    open_count: 0,
    error_count: 0,
    all_clear: true,
  })
})

afterEach(() => {
  document.documentElement.classList.remove('dark')
  localStorage.clear()
})

describe('app shell', () => {
  it('renders the top nav and the SOS page at /sos', async () => {
    renderApp()
    // The Open Hospitality wordmark rides the header — "Open" is the
    // accented span, "Hospitality" the trailing text node.
    expect(await screen.findByText('Open')).toBeInTheDocument()
    expect(screen.getAllByText(/Hospitality/).length).toBeGreaterThan(0)
    expect(await screen.findByRole('link', { name: 'SOS' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Coverage' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Upload' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Reports' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'QBO' })).toBeInTheDocument()
    expect(
      await screen.findByRole('heading', { name: 'Summary Operating Statement' }),
    ).toBeInTheDocument()
  })

  it('opens the dashboard at / on a first visit', async () => {
    localStorage.clear() // nothing remembered — a genuine first load
    renderApp(AUTHED_CONTEXT, '/')
    expect(await screen.findByRole('heading', { name: 'Hotel overview' })).toBeInTheDocument()
  })

  it('restores the last visited page at /', async () => {
    localStorage.setItem('usali.last-route', '/upload')
    renderApp(AUTHED_CONTEXT, '/')
    expect(await screen.findByRole('heading', { name: 'Upload Reports' })).toBeInTheDocument()
  })

  it('remembers the page you are on so the next / lands there', async () => {
    localStorage.clear()
    renderApp(AUTHED_CONTEXT, '/upload')
    await screen.findByRole('heading', { name: 'Upload Reports' })
    expect(localStorage.getItem('usali.last-route')).toBe('/upload')
  })

  it('dark-mode toggle flips the root class, aria-pressed, and back', async () => {
    renderApp()
    const toggle = await screen.findByRole('button', { name: 'Toggle dark mode' })
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(toggle).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(toggle)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(toggle).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(toggle)
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
  })

  it('Sign out button invokes logout', async () => {
    const logout = vi.fn()
    renderApp({ ...AUTHED_CONTEXT, logout })
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(logout).toHaveBeenCalledOnce()
  })

  it('shows Employees link for org_admin', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    renderApp()
    expect(await screen.findByRole('link', { name: 'Employees' })).toBeInTheDocument()
  })

  it('hides Employees link for a non-admin operator', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['accountant'] })
    const queryClient = renderApp()
    // Waiting on SOS alone is vacuous: it renders before the role query
    // settles, so the assertion below would pass against a still-pending
    // `me`. Accountant does not unlock any gated nav link, so there is no
    // link whose appearance would prove resolution the way Weekly Schedule
    // does for property_gm — anchor on the query itself instead.
    await waitFor(() => expect(queryClient.getQueryData(['me'])).toBeDefined())
    expect(screen.queryByRole('link', { name: 'Employees' })).not.toBeInTheDocument()
  })

  it('shows Weekly Schedule link for property_gm', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    renderApp()
    expect(await screen.findByRole('link', { name: 'Weekly Schedule' })).toBeInTheDocument()
  })

  it('hides Weekly Schedule link for a non-admin operator', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['accountant'] })
    const queryClient = renderApp()
    // Same problem as the Employees test above, and the same fix: accountant
    // holds no role that unlocks a gated link, so there is nothing in the DOM
    // whose appearance can stand in for "the role query resolved" — wait on
    // the query itself instead of a link.
    await waitFor(() => expect(queryClient.getQueryData(['me'])).toBeDefined())
    expect(screen.queryByRole('link', { name: 'Weekly Schedule' })).not.toBeInTheDocument()
  })

  it('shows Integrations link for org_admin', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    renderApp()
    expect(await screen.findByRole('link', { name: 'Integrations' })).toBeInTheDocument()
  })

  it('hides Integrations link from a property_gm', async () => {
    // The strongest non-admin the system has, and still not an org_admin:
    // connecting a tenant's payroll is not a GM's call, and a nav entry is a
    // promise about what this account can do.
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    renderApp()
    // Waiting on SOS alone is vacuous: it renders before the role query
    // settles, so the Integrations check below would pass even ungated.
    // Weekly Schedule only appears once /api/me has resolved, so waiting on
    // it first is what actually pins the assertion to post-resolution state.
    await screen.findByRole('link', { name: 'Weekly Schedule' })
    expect(screen.queryByRole('link', { name: 'Integrations' })).not.toBeInTheDocument()
  })

  // A coming-soon entry is still a promise about what this account can do.
  // Payroll & Compensation is payroll_admin work, so a GM must not see it
  // waiting for them.
  it('hides the payroll placeholder from a role that will never own it', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    renderApp()
    await screen.findByRole('link', { name: 'Weekly Schedule' })
    expect(screen.queryByText('Payroll & Compensation')).not.toBeInTheDocument()
  })

  it('shows the payroll placeholder to a payroll admin', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['payroll_admin'] })
    renderApp()
    expect(await screen.findByText('Payroll & Compensation')).toBeInTheDocument()
  })

  // The nav had a live /schedule route sitting beside a "Weekly Schedule"
  // placeholder for the same thing, and an Employee Profile placeholder with
  // nothing behind it. One entry per destination.
  it('carries no placeholder that duplicates a live route', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    renderApp()
    await screen.findByRole('link', { name: 'Weekly Schedule' })
    expect(screen.queryByText('Employee Profile')).not.toBeInTheDocument()
    // A single entry, and it is the real one — not a link plus a dead twin.
    expect(screen.getAllByText('Weekly Schedule')).toHaveLength(1)
  })
})

describe('app shell — setup nav', () => {
  it('shows the Setup entry with an open-item count', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [],
      open_count: 3,
      error_count: 0,
      all_clear: false,
    })
    renderApp()
    // The accessible name is the user-facing contract, and the count belongs
    // in it: a pill whose text lands in the name turns this into "Setup3" and
    // makes every exact-name lookup in this file miss.
    expect(
      await screen.findByRole('link', { name: 'Setup: 3 items still to set up' }),
    ).toBeInTheDocument()
    // Scoped to the badge: '3' is a bare numeral in a whole app shell.
    expect(within(screen.getByTestId('setup-badge')).getByText('3')).toBeInTheDocument()
  })

  // Two states in one test so neither is vacuous: `findByRole` alone resolves
  // as soon as the nav paints, which cannot tell "retired because all_clear"
  // from "the fetch has not landed yet".
  it('renders no badge while the checklist is in flight, and none once it clears', async () => {
    let settle!: (c: Checklist) => void
    vi.mocked(getChecklist).mockReturnValue(
      new Promise((resolve) => {
        settle = resolve
      }),
    )
    const queryClient = renderApp()
    await screen.findByRole('link', { name: 'Setup' })
    expect(screen.queryByTestId('setup-badge')).toBeNull()

    settle({ items: [], open_count: 0, error_count: 0, all_clear: true })
    // Anchored on the query, not on `findByRole`: the link is already in the
    // DOM, so a role lookup resolves at once and would leave the assertion
    // below passing against a still-pending fetch.
    await waitFor(() => expect(queryClient.getQueryData(CHECKLIST_KEY)).toBeDefined())
    expect(screen.queryByTestId('setup-badge')).toBeNull()
  })

  // A sidebar query that 500s must not take the shell down with it.
  it('renders no badge and keeps the shell when the checklist read fails', async () => {
    vi.mocked(getChecklist).mockRejectedValue(new Error('boom'))
    renderApp()
    expect(
      await screen.findByRole('heading', { name: 'Summary Operating Statement' }),
    ).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Setup' })).toBeInTheDocument()
    expect(screen.queryByTestId('setup-badge')).toBeNull()
  })

  // Pins what Layout's comment promises: a failure *after* a good read keeps
  // the last-known count, because an ambient pointer that blinks out on a
  // transient hiccup is worse than a slightly stale numeral.
  it('keeps the last-known count when a background refetch fails', async () => {
    vi.mocked(getChecklist)
      .mockResolvedValueOnce({ items: [], open_count: 3, error_count: 0, all_clear: false })
      .mockRejectedValue(new Error('boom'))
    const queryClient = renderApp()
    expect(await screen.findByTestId('setup-badge')).toHaveTextContent('3')

    void queryClient.invalidateQueries({ queryKey: CHECKLIST_KEY })
    await waitFor(() => expect(queryClient.getQueryState(CHECKLIST_KEY)?.error).toBeTruthy())
    expect(screen.getByTestId('setup-badge')).toHaveTextContent('3')
  })

  // THE divergence case, at the badge.
  it('badges "!" — not "0", not nothing — when every probe failed', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [],
      open_count: 0,
      error_count: 4,
      all_clear: false,
    })
    renderApp()
    expect(await screen.findByTestId('setup-badge')).toHaveTextContent('!')
    // '!' announces as nothing at default verbosity, so the divergence has to
    // survive into the name as words.
    expect(
      screen.getByRole('link', { name: 'Setup: Could not check 4 items' }),
    ).toBeInTheDocument()
  })

  // The count is the whole reason a collapsed sidebar still points at setup,
  // so the pill must not ride along when the label goes sr-only. It has to be
  // a structural check: `sr-only` is position/clip, not display:none, so
  // `toBeVisible()` would pass on an sr-only element even with the real
  // stylesheet loaded.
  it('keeps the badge out of sr-only when the sidebar is collapsed', async () => {
    localStorage.setItem('usali.sidebar-collapsed', '1')
    vi.mocked(getChecklist).mockResolvedValue({
      items: [],
      open_count: 2,
      error_count: 0,
      all_clear: false,
    })
    renderApp()
    // `closest` starts at the element itself, so this covers the pill too.
    expect((await screen.findByTestId('setup-badge')).closest('.sr-only')).toBeNull()
  })
})
