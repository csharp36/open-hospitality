// GL page flow with the gl module mocked: the period rail IS the picker
// (selection lives in ?period=), a chip click fetches the trial balance, and
// the 404-vs-failure split renders a quiet empty state, never a red line.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'

// importOriginal spread keeps the real ApiError class (lib/errors depends on
// it for instanceof checks) while stubbing the fetchers.
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getProperties: vi.fn(),
  getMe: vi.fn(),
}))

// Full mock is fine here: api/gl exports functions only, no classes.
vi.mock('../api/gl', () => ({
  getGlPeriods: vi.fn(),
  getTrialBalance: vi.fn(),
  getJournalEntries: vi.fn(),
  closeGlPeriod: vi.fn(),
  reopenGlPeriod: vi.fn(),
  postGlRange: vi.fn(),
}))

import { ApiError, getMe, getProperties } from '../api/client'
import { getGlPeriods, getTrialBalance } from '../api/gl'
import { createAppRouter } from '../router'
import {
  AUTHED_CONTEXT,
  HISJ_PROPERTY,
  SSSJ_PROPERTY,
  makeGlPeriod,
  makeTrialBalance,
} from '../test/fixtures'
import { AuthContext } from '../auth/authContext'

function renderPage(initialPath = '/gl') {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [initialPath] }))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

// The page's fiscal-year default is derived client-side; computing the same
// expression here keeps the assertion from expiring at the new year.
const THIS_YEAR = new Date().getFullYear()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getProperties).mockResolvedValue([HISJ_PROPERTY, SSSJ_PROPERTY])
  vi.mocked(getMe).mockResolvedValue({ subject: 'u1', username: 'tester', roles: [] })
  vi.mocked(getGlPeriods).mockResolvedValue([
    makeGlPeriod({
      period_key: '2026-P06',
      state: 'closed',
      date_from: '2026-06-01',
      date_to: '2026-06-30',
    }),
    makeGlPeriod(), // 2026-P07, open
  ])
  vi.mocked(getTrialBalance).mockResolvedValue(makeTrialBalance())
})

describe('GlPage', () => {
  it('renders the no-property empty state when no property exists', async () => {
    vi.mocked(getProperties).mockResolvedValue([])
    renderPage()
    expect(await screen.findByRole('heading', { name: 'General Ledger' })).toBeInTheDocument()
    expect(await screen.findByText('No property selected yet.')).toBeInTheDocument()
    expect(getGlPeriods).not.toHaveBeenCalled()
  })

  it('renders a chip per period, closed ones carrying a Closed badge', async () => {
    renderPage()
    const rail = await screen.findByRole('group', { name: 'Fiscal periods' })
    const closedChip = within(rail).getByRole('button', { name: /2026-P06/ })
    const openChip = within(rail).getByRole('button', { name: /2026-P07/ })
    expect(within(closedChip).getByText('Closed')).toBeInTheDocument()
    expect(within(openChip).queryByText('Closed')).not.toBeInTheDocument()
    expect(getGlPeriods).toHaveBeenCalledWith('HISJ', THIS_YEAR)
  })

  it('refetches the rail when the fiscal year steps', async () => {
    renderPage()
    await screen.findByRole('group', { name: 'Fiscal periods' })
    fireEvent.change(screen.getByLabelText('Fiscal year'), { target: { value: '2025' } })
    expect(getGlPeriods).toHaveBeenCalledWith('HISJ', 2025)
  })

  it('ignores half-typed years instead of fetching them', async () => {
    renderPage()
    await screen.findByRole('group', { name: 'Fiscal periods' })
    // Retyping a year passes through junk states ("2", "20"); none may fetch.
    fireEvent.change(screen.getByLabelText('Fiscal year'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Fiscal year'), { target: { value: '20' } })
    expect(getGlPeriods).not.toHaveBeenCalledWith('HISJ', 2)
    expect(getGlPeriods).not.toHaveBeenCalledWith('HISJ', 20)
    expect(getGlPeriods).toHaveBeenCalledTimes(1)
  })

  it('seeds the rail year from a deep-linked period', async () => {
    renderPage('/gl?period=2025-P07')
    await screen.findByRole('group', { name: 'Fiscal periods' })
    // Rail and detail agree on arrival: the rail fetches the linked period's
    // year, and the trial balance fetches the linked period itself.
    expect(getGlPeriods).toHaveBeenCalledWith('HISJ', 2025)
    expect(getTrialBalance).toHaveBeenCalledWith('HISJ', '2025-P07')
    expect(screen.getByLabelText('Fiscal year')).toHaveValue(2025)
  })

  it('fetches and renders the trial balance when a chip is clicked', async () => {
    // Totals serialize at DIFFERENT scales: string equality would call this
    // unbalanced, and only exact numeric comparison (eqFixed) says balanced.
    vi.mocked(getTrialBalance).mockResolvedValue(
      makeTrialBalance({ total_debits: '12066.37', total_credits: '12066.3700' }),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /2026-P07/ }))

    expect(await screen.findByText('Rooms Revenue')).toBeInTheDocument()
    expect(getTrialBalance).toHaveBeenCalledWith('HISJ', '2026-P07')
    // The selected chip is marked accessibly.
    expect(screen.getByRole('button', { name: /2026-P07/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    // Line cells: code, name, type, amounts via fmtMoney.
    expect(screen.getByText('1010')).toBeInTheDocument()
    expect(screen.getByText('Cash - Operating')).toBeInTheDocument()
    expect(screen.getAllByText('asset')).toHaveLength(2)
    expect(screen.getByText('10,866.37')).toBeInTheDocument() // 4100 credits
    // Totals row: both totals render 12,066.37 (line-cell 12,066.37 does not
    // exist in the fixture, so exactly two).
    expect(screen.getAllByText('12,066.37')).toHaveLength(2)
    // The period's date range from the response, not the chip.
    expect(screen.getByText(/2026-07-01 – 2026-07-31/)).toBeInTheDocument()
    expect(screen.getByText('balanced ✓')).toBeInTheDocument()
  })

  it('shows a danger badge, not balanced, when totals differ', async () => {
    vi.mocked(getTrialBalance).mockResolvedValue(
      makeTrialBalance({ total_debits: '12066.3700', total_credits: '12000.0000' }),
    )
    renderPage('/gl?period=2026-P07')
    expect(await screen.findByText('Rooms Revenue')).toBeInTheDocument()
    expect(screen.queryByText('balanced ✓')).not.toBeInTheDocument()
    expect(screen.getByText(/does not equal/)).toBeInTheDocument()
  })

  it('renders a 404 as a quiet empty state, not a failure', async () => {
    vi.mocked(getTrialBalance).mockRejectedValue(
      new ApiError(404, 'no journal lines for property HISJ in 2026-P07'),
    )
    renderPage('/gl?period=2026-P07')
    expect(await screen.findByText('Nothing posted for this period yet.')).toBeInTheDocument()
    expect(screen.getByText('no journal lines for property HISJ in 2026-P07')).toBeInTheDocument()
    expect(screen.queryByText(/Failed to load trial balance/)).not.toBeInTheDocument()
  })

  it('renders a real failure loud', async () => {
    vi.mocked(getTrialBalance).mockRejectedValue(new ApiError(503, 'upstream down'))
    renderPage('/gl?period=2026-P07')
    expect(
      await screen.findByText('Failed to load trial balance: upstream down'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Nothing posted for this period yet.')).not.toBeInTheDocument()
  })

  it('preselects the chip named by ?period= in the initial URL', async () => {
    renderPage('/gl?period=2026-P07')
    expect(await screen.findByText('Rooms Revenue')).toBeInTheDocument()
    expect(getTrialBalance).toHaveBeenCalledWith('HISJ', '2026-P07')
    expect(screen.getByRole('button', { name: /2026-P07/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('clamps a malformed ?period= to no selection and fires no request', async () => {
    renderPage('/gl?period=garbage')
    const rail = await screen.findByRole('group', { name: 'Fiscal periods' })
    for (const chip of within(rail).getAllByRole('button')) {
      expect(chip).toHaveAttribute('aria-pressed', 'false')
    }
    expect(getTrialBalance).not.toHaveBeenCalled()
  })
})
