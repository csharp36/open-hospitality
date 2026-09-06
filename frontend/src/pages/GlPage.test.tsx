// GL page flow with the gl module mocked: the period rail IS the picker
// (selection lives in ?period=), a chip click fetches the trial balance, and
// the 404-vs-failure split renders a quiet empty state, never a red line.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
import { getGlPeriods, getJournalEntries, getTrialBalance } from '../api/gl'
import type { JournalEntry } from '../api/types'
import { createAppRouter } from '../router'
import {
  AUTHED_CONTEXT,
  HISJ_PROPERTY,
  SSSJ_PROPERTY,
  makeGlPeriod,
  makeJournalEntry,
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
  vi.mocked(getJournalEntries).mockResolvedValue(
    journalEnvelope([makeJournalEntry({ entry_id: 12 })]),
  )
})

function journalEnvelope(entries: JournalEntry[]) {
  return { property_id: 'HISJ', period_key: '2026-P07', account_code: '4100', entries }
}

/** Renders /gl with a period selected, drills 4100, returns the open panel. */
async function openDrill() {
  renderPage('/gl?period=2026-P07')
  fireEvent.click(await screen.findByRole('button', { name: 'Rooms Revenue' }))
  return await screen.findByRole('dialog', { name: 'Journal entries: 4100 — Rooms Revenue' })
}

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

describe('GlPage journal drill', () => {
  it('opens the entries panel from an account row and lays amounts under their posting', async () => {
    const panel = await openDrill()
    expect(getJournalEntries).toHaveBeenCalledWith('HISJ', '2026-P07', '4100')

    // Entry header: date, source, entry #, posted by.
    expect(await within(panel).findByText('2026-07-07')).toBeInTheDocument()
    expect(within(panel).getByText('pms_daily')).toBeInTheDocument()
    expect(within(panel).getByText('entry #12')).toBeInTheDocument()
    expect(within(panel).getByText('posted by dev-admin')).toBeInTheDocument()

    // Lines table: Debit and Credit columns, each amount under the column its
    // posting names — never a signed amount.
    expect(within(panel).getByRole('columnheader', { name: 'Debit' })).toBeInTheDocument()
    expect(within(panel).getByRole('columnheader', { name: 'Credit' })).toBeInTheDocument()
    const debitCells = within(
      within(panel).getByText('Guest Ledger').closest('tr')!,
    ).getAllByRole('cell')
    expect(debitCells[2]).toHaveTextContent('10,456.37')
    expect(debitCells[3]!.textContent).toBe('')
    const creditCells = within(
      within(panel).getByText('Rooms Revenue').closest('tr')!,
    ).getAllByRole('cell')
    expect(creditCells[2]!.textContent).toBe('')
    expect(creditCells[3]).toHaveTextContent('10,456.37')
  })

  it('badges a reversal entry with the entry it reverses', async () => {
    vi.mocked(getJournalEntries).mockResolvedValue(
      journalEnvelope([
        makeJournalEntry({ entry_id: 12 }),
        makeJournalEntry({ entry_id: 13, reversal_of: 12, memo: 'Reversal of entry 12' }),
      ]),
    )
    const panel = await openDrill()
    expect(await within(panel).findByText('entry #13')).toBeInTheDocument()
    expect(within(panel).getByText('reversal of entry #12')).toBeInTheDocument()
    // The original entry carries no badge — exactly one reversal marker.
    expect(within(panel).getAllByText(/reversal of entry/)).toHaveLength(1)
  })

  it('shows staged provenance on pms lines and nothing at all on null-provenance lines', async () => {
    const panel = await openDrill()
    // The credit line carries its staged transaction: txn code + source file.
    expect(await within(panel).findByText('1000')).toBeInTheDocument()
    expect(within(panel).getByText(/opera-2026-07-07\.pdf/)).toBeInTheDocument()
    // The null-provenance debit line shows its memo and NO transaction
    // placeholder: absent means "no source transaction by design", not
    // missing data.
    const debitRow = within(panel).getByText('Guest Ledger').closest('tr')!
    expect(within(debitRow).getByText('Daily revenue 2026-07-07')).toBeInTheDocument()
    expect(within(debitRow).getAllByRole('cell')[4]!.textContent).toBe('')
    expect(within(debitRow).queryByText('—')).not.toBeInTheDocument()
  })

  it('focuses Close on open and closes on Escape', async () => {
    const panel = await openDrill()
    expect(within(panel).getByRole('button', { name: 'Close' })).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders a failed entries fetch loud inside the panel', async () => {
    vi.mocked(getJournalEntries).mockRejectedValue(new ApiError(503, 'upstream down'))
    const panel = await openDrill()
    expect(
      await within(panel).findByText('Failed to load journal entries: upstream down'),
    ).toBeInTheDocument()
  })

  it('renders an empty entries list as a quiet line, not an error', async () => {
    vi.mocked(getJournalEntries).mockResolvedValue(journalEnvelope([]))
    const panel = await openDrill()
    expect(
      await within(panel).findByText('No entries for this account in this period.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Failed to load journal entries/)).not.toBeInTheDocument()
  })

  it('closes the panel when the property changes', async () => {
    await openDrill()
    fireEvent.change(screen.getByLabelText('Active property'), { target: { value: 'SSSJ' } })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('closes the panel when the period changes', async () => {
    await openDrill()
    fireEvent.click(screen.getByRole('button', { name: /2026-P06/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})
