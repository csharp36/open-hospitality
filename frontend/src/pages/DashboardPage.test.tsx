// The dashboard hero panel. What is worth pinning is not the gradient — it is
// that the three counts come from the statistics feed and that a date with no
// statistics says so instead of printing zeros.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getSos: vi.fn(),
  getProperties: vi.fn(),
  getMe: vi.fn(),
}))

vi.mock('../api/checklist', () => ({
  getChecklist: vi.fn(),
  dismissItem: vi.fn(),
  restoreItem: vi.fn(),
}))

import { getMe, getProperties, getSos } from '../api/client'
import { getChecklist } from '../api/checklist'
import { createAppRouter } from '../router'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT, HISJ_PROPERTY, makeSosReport } from '../test/fixtures'

function stat(metric_code: string, day: string | null) {
  return {
    metric_code, day, mtd: null, ytd: null,
    day_prior: null, mtd_prior: null, ytd_prior: null,
  }
}

function renderDashboard() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/dashboard'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
  vi.mocked(getProperties).mockResolvedValue([HISJ_PROPERTY])
  // All clear by default so the setup card stays out of every other test in
  // this file.
  vi.mocked(getChecklist).mockResolvedValue({
    items: [], open_count: 0, error_count: 0, all_clear: true,
  })
  vi.mocked(getSos).mockResolvedValue(
    makeSosReport({
      total_operating_revenue: '28786.0000',
      statistics: [
        stat('ARRIVALS', '22.0000'),
        stat('DEPARTURES', '23.0000'),
        // "still vacant tonight" — NOT the denominator occupancy is computed
        // against, which is TOTAL_ROOMS.
        stat('ROOMS_AVAILABLE', '18.0000'),
      ],
    }),
  )
})

describe('DashboardPage hero panel', () => {
  it('carries the front-desk triad off the statistics feed', async () => {
    renderDashboard()
    // Wait on a VALUE, not on the panel: the panel renders before its query
    // settles, so awaiting the region alone races the data it is asserting.
    expect(await screen.findByText('22')).toBeInTheDocument()

    const panel = screen.getByRole('region', { name: 'Today at a glance' })
    expect(within(panel).getByText('Arrivals')).toBeInTheDocument()
    expect(within(panel).getByText('Departures')).toBeInTheDocument()
    expect(within(panel).getByText('23')).toBeInTheDocument()
    expect(within(panel).getByText('Available')).toBeInTheDocument()
    expect(within(panel).getByText('18')).toBeInTheDocument()
  })

  // The headline is month-to-date on purpose: the KPI row below already
  // carries today's revenue, and a hero that repeats the tile under it is
  // decoration rather than information.
  it('headlines month-to-date revenue, not today’s', async () => {
    renderDashboard()
    expect(await screen.findByText('$28,786')).toBeInTheDocument()
    expect(screen.getByText(/to date/)).toBeInTheDocument()
  })

  // A date the PMS never delivered must not print zeros — a zero arrival count
  // reads as "nobody is checking in", which is a different fact from "we do
  // not know yet".
  it('shows an em dash, not zero, when the day has no statistics', async () => {
    vi.mocked(getSos).mockResolvedValue(
      makeSosReport({ statistics: [], total_operating_revenue: '28786.0000' }),
    )
    renderDashboard()
    // The revenue headline proves the query SETTLED, so the dashes below are
    // "no statistics", not "not loaded yet".
    await screen.findByText('$28,786')

    const panel = screen.getByRole('region', { name: 'Today at a glance' })
    expect(within(panel).queryByText('0')).not.toBeInTheDocument()
    expect(within(panel).getAllByText('—')).toHaveLength(3)
  })
})

// A first-run pointer to /setup, not a second checklist: it says how much is
// left and gets out of the way for good once setup is finished.
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
    // Anchor on settled dashboard data first: asserting absence on the very
    // first tick would pass against a card that has simply not rendered yet.
    await screen.findByText('$28,786')
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

  // Errors AND open items are two different facts. Two sentences, two
  // numerals — never the 5 that folds "we could not check these" into "these
  // are still to do".
  it('keeps the unchecked count apart from the open count when both exist', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 3, error_count: 2, all_clear: false,
    })
    renderDashboard()
    expect(await screen.findByText(/could not check 2 setup items/i)).toBeInTheDocument()
    expect(screen.getByText(/3 other things still to set up/i)).toBeInTheDocument()
    expect(screen.queryByText(/5 things/i)).toBeNull()
  })

  it('counts one open item in the singular', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [], open_count: 1, error_count: 0, all_clear: false,
    })
    renderDashboard()
    expect(await screen.findByText('1 thing left before your setup is complete.')).toBeInTheDocument()
  })
})
