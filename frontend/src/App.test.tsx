// Smoke test on the nav shell: the router renders the layout with all five
// route links and the SOS page heading at `/sos`, plus the dark-mode toggle.
// Also covers the entry route `/`, which restores the last visited page and
// falls back to the dashboard on a first visit.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'

vi.mock('./api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api/client')>()),
  getMe: vi.fn(),
}))

import { getMe } from './api/client'
import { createAppRouter } from './router'
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
}

describe('app shell', () => {
  beforeEach(() => {
    vi.mocked(getMe).mockResolvedValue({ subject: '', username: '', roles: [] })
  })

  afterEach(() => {
    document.documentElement.classList.remove('dark')
    localStorage.clear()
  })

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
    renderApp()
    await screen.findByRole('link', { name: 'SOS' }) // nav rendered
    expect(screen.queryByRole('link', { name: 'Employees' })).not.toBeInTheDocument()
  })

  it('shows Weekly Schedule link for property_gm', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    renderApp()
    expect(await screen.findByRole('link', { name: 'Weekly Schedule' })).toBeInTheDocument()
  })

  it('hides Weekly Schedule link for a non-admin operator', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['accountant'] })
    renderApp()
    await screen.findByRole('link', { name: 'SOS' }) // nav rendered
    expect(screen.queryByRole('link', { name: 'Weekly Schedule' })).not.toBeInTheDocument()
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
