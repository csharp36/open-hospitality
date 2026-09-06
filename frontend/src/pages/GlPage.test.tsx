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
import {
  closeGlPeriod,
  getGlPeriods,
  getJournalEntries,
  getTrialBalance,
  postGlRange,
  reopenGlPeriod,
} from '../api/gl'
import type { GlPostOutcome, JournalEntry } from '../api/types'
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

// Returns the QueryClient so absence assertions can anchor on the ['me']
// query having resolved (the App.test.tsx renderApp shape).
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
  return queryClient
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

    // Line cells: code, name, type, amounts via fmtMoney — scoped to the
    // trial balance card, since the balance sheet below repeats codes/names.
    const tbCard = screen.getByRole('region', { name: 'Trial balance 2026-P07' })
    expect(within(tbCard).getByText('1010')).toBeInTheDocument()
    expect(within(tbCard).getByText('Cash - Operating')).toBeInTheDocument()
    expect(within(tbCard).getAllByText('asset')).toHaveLength(2)
    expect(within(tbCard).getByText('10,866.37')).toBeInTheDocument() // 4100 credits
    // Totals row: both totals render 12,066.37 (line-cell 12,066.37 does not
    // exist in the fixture, so exactly two).
    expect(within(tbCard).getAllByText('12,066.37')).toHaveLength(2)
    // The period's date range from the response, not the chip.
    expect(within(tbCard).getByText(/2026-07-01 – 2026-07-31/)).toBeInTheDocument()
    expect(within(tbCard).getByText('balanced ✓')).toBeInTheDocument()
  })

  it('shows a danger badge, not balanced, when totals differ', async () => {
    vi.mocked(getTrialBalance).mockResolvedValue(
      makeTrialBalance({ total_debits: '12066.3700', total_credits: '12000.0000' }),
    )
    renderPage('/gl?period=2026-P07')
    expect(await screen.findByText('Rooms Revenue')).toBeInTheDocument()
    // Scoped to the trial balance card: the balance sheet below foots from
    // the lines and keeps its own badge (its describe block covers that).
    const tbCard = screen.getByRole('region', { name: 'Trial balance 2026-P07' })
    expect(within(tbCard).queryByText('balanced ✓')).not.toBeInTheDocument()
    expect(within(tbCard).getByText(/does not equal/)).toBeInTheDocument()
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

describe('GlPage balance sheet', () => {
  it('renders the net-change balance sheet from the same trial-balance response', async () => {
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Balance sheet 2026-P07' })
    // The title says what the numbers are — the period's net change, not a
    // statement of financial position (plan "things to get right" #1).
    expect(
      within(card).getByRole('heading', { name: 'Balance sheet — net change for 2026-P07' }),
    ).toBeInTheDocument()
    expect(
      within(card).getByText(/activity for this period, not a cumulative position/i),
    ).toBeInTheDocument()
    // Derived in place: one fetch serves the trial balance and this card.
    expect(getTrialBalance).toHaveBeenCalledTimes(1)
    // The synthetic equity line, and the fixture's cash net (500 - 1200).
    expect(within(card).getByText('Net income (this period)')).toBeInTheDocument()
    expect(within(card).getByText('-700.00')).toBeInTheDocument()
    // Its own balanced badge, from its own foot.
    expect(within(card).getByText('balanced ✓')).toBeInTheDocument()
  })

  it('foots from the lines, so its badge is independent of the totals fields', async () => {
    // Totals that disagree with each other while the lines still foot: the
    // trial balance card badges danger, the balance sheet stays balanced.
    vi.mocked(getTrialBalance).mockResolvedValue(
      makeTrialBalance({ total_debits: '12066.3700', total_credits: '12000.0000' }),
    )
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Balance sheet 2026-P07' })
    expect(within(card).getByText('balanced ✓')).toBeInTheDocument()
    const tbCard = screen.getByRole('region', { name: 'Trial balance 2026-P07' })
    expect(within(tbCard).getByText(/does not equal/)).toBeInTheDocument()
  })
})

describe('GlPage period detail', () => {
  // 2026-P07 open with both gap directions populated.
  const OPEN_WITH_GAPS = makeGlPeriod({
    unposted_dates: ['2026-07-02', '2026-07-03', '2026-07-05'],
    orphaned_dates: ['2026-07-04'],
  })

  const ORG_ADMIN = { subject: 'u1', username: 'admin', roles: ['org_admin'] }

  it('shows both gap lists with the PMS qualifier and the dates themselves', async () => {
    vi.mocked(getGlPeriods).mockResolvedValue([OPEN_WITH_GAPS])
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    // The count phrase says PMS — the unposted check is pms_daily-only, and
    // a bare "3 days unposted" would imply the payroll side was checked.
    expect(within(card).getByText('3 PMS days unposted')).toBeInTheDocument()
    expect(within(card).getByText(/payroll accruals are not checked here/i)).toBeInTheDocument()
    expect(within(card).getByText('1 posted entry lost its facts (orphaned)')).toBeInTheDocument()
    // The dates themselves, not just counts.
    expect(within(card).getByText(/2026-07-02, 2026-07-03, 2026-07-05/)).toBeInTheDocument()
    expect(within(card).getByText(/2026-07-04/)).toBeInTheDocument()
  })

  it('says no gaps without claiming all caught up, qualifier still present', async () => {
    vi.mocked(getGlPeriods).mockResolvedValue([makeGlPeriod()])
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    expect(
      within(card).getByText('No PMS-day gaps and no orphaned entries.'),
    ).toBeInTheDocument()
    // Zero unposted PMS days still says nothing about payroll accruals, so
    // the qualifier sentence stays.
    expect(within(card).getByText(/payroll accruals are not checked here/i)).toBeInTheDocument()
    expect(within(card).queryByText(/all caught up/i)).not.toBeInTheDocument()
  })

  it('renders no detail card when ?period= matches no period in the rail', async () => {
    // A stale deep link after a property switch: the rail loads, nothing is
    // selected in it, and no detail card renders.
    renderPage('/gl?period=2025-P01')
    await screen.findByRole('group', { name: 'Fiscal periods' })
    expect(screen.queryByRole('region', { name: 'Period 2025-P01' })).not.toBeInTheDocument()
  })

  it('close: the confirm gets focus and names the gaps, the card shows Closed, and the periods query refetches', async () => {
    const unposted = ['2026-07-02', '2026-07-03', '2026-07-05']
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(getGlPeriods)
      .mockResolvedValueOnce([makeGlPeriod({ unposted_dates: unposted })])
      .mockResolvedValue([makeGlPeriod({ state: 'closed', unposted_dates: unposted })])
    vi.mocked(closeGlPeriod).mockResolvedValue({
      period_key: '2026-P07',
      state: 'closed',
      unposted_dates: unposted,
      orphaned_dates: [],
    })
    renderPage('/gl?period=2026-P07')

    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    // No reopen on an open period.
    expect(within(card).queryByRole('button', { name: 'Reopen 2026-P07' })).not.toBeInTheDocument()
    fireEvent.click(within(card).getByRole('button', { name: 'Close 2026-P07' }))
    // The in-card confirm names the gaps being closed over — never a bare
    // "Are you sure?", never window.confirm.
    expect(
      within(card).getByText(/close 2026-P07 with 3 unposted PMS days\?/i),
    ).toBeInTheDocument()
    // Focus follows the swap: the button just pressed is gone, so the
    // confirm inherits it.
    const yesClose = within(card).getByRole('button', { name: 'Yes, close' })
    await waitFor(() => expect(yesClose).toHaveFocus())
    fireEvent.click(yesClose)

    await waitFor(() => expect(closeGlPeriod).toHaveBeenCalledWith('HISJ', '2026-P07'))
    expect(await within(card).findByText('Closed')).toBeInTheDocument()
    // The confirm is gone and the close control with it — the period is closed.
    expect(within(card).queryByRole('button', { name: 'Yes, close' })).not.toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Close 2026-P07' })).not.toBeInTheDocument()
    // The invalidated periods query refetched.
    expect(getGlPeriods).toHaveBeenCalledTimes(2)
  })

  it('close: cancel backs out without calling the endpoint', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(getGlPeriods).mockResolvedValue([OPEN_WITH_GAPS])
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    fireEvent.click(within(card).getByRole('button', { name: 'Close 2026-P07' }))
    fireEvent.click(within(card).getByRole('button', { name: 'Cancel' }))
    expect(within(card).getByRole('button', { name: 'Close 2026-P07' })).toBeInTheDocument()
    expect(closeGlPeriod).not.toHaveBeenCalled()
  })

  it('reopen: the reason is required, then sent verbatim', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(getGlPeriods).mockResolvedValue([makeGlPeriod({ state: 'closed' })])
    vi.mocked(reopenGlPeriod).mockResolvedValue(undefined)
    renderPage('/gl?period=2026-P07')

    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    // No close on a closed period.
    expect(within(card).queryByRole('button', { name: 'Close 2026-P07' })).not.toBeInTheDocument()
    const reopen = within(card).getByRole('button', { name: 'Reopen 2026-P07' })
    expect(reopen).toBeDisabled()
    // Whitespace is not a reason.
    fireEvent.change(within(card).getByLabelText('Reason for reopening'), {
      target: { value: '   ' },
    })
    expect(reopen).toBeDisabled()
    fireEvent.change(within(card).getByLabelText('Reason for reopening'), {
      target: { value: 'auditor request' },
    })
    expect(reopen).toBeEnabled()
    fireEvent.click(reopen)
    await waitFor(() =>
      expect(reopenGlPeriod).toHaveBeenCalledWith('HISJ', '2026-P07', 'auditor request'),
    )
  })

  it('property_gm sees state and gaps but no close/reopen controls', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 'u1', username: 'gm', roles: ['property_gm'] })
    vi.mocked(getGlPeriods).mockResolvedValue([OPEN_WITH_GAPS])
    const queryClient = renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    expect(within(card).getByText('3 PMS days unposted')).toBeInTheDocument()
    // Anchor on the role query itself before asserting absence — the card
    // renders before `me` settles, so an unanchored query would pass against
    // a still-pending role fetch (the App.test.tsx nav-test shape).
    await waitFor(() => expect(queryClient.getQueryData(['me'])).toBeDefined())
    expect(within(card).queryByRole('button', { name: 'Close 2026-P07' })).not.toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Reopen 2026-P07' })).not.toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Post 2026-P07' })).not.toBeInTheDocument()
  })

  it('surfaces a refused close inline via its detail', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(getGlPeriods).mockResolvedValue([OPEN_WITH_GAPS])
    vi.mocked(closeGlPeriod).mockRejectedValue(
      new ApiError(422, 'close refused: the journal disagrees with the SOS'),
    )
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    fireEvent.click(within(card).getByRole('button', { name: 'Close 2026-P07' }))
    fireEvent.click(within(card).getByRole('button', { name: 'Yes, close' }))
    expect(
      await within(card).findByText('close refused: the journal disagrees with the SOS'),
    ).toBeInTheDocument()
  })
})

describe('GlPage post the period', () => {
  const ORG_ADMIN = { subject: 'u1', username: 'admin', roles: ['org_admin'] }

  function makeOutcome(overrides: Partial<GlPostOutcome> = {}): GlPostOutcome {
    return {
      business_date: '2026-07-01',
      source_type: 'pms_daily',
      status: 'posted',
      entry_id: null,
      message: null,
      ...overrides,
    }
  }

  it('offers Post to an org_admin on an open period', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    expect(await within(card).findByRole('button', { name: 'Post 2026-P07' })).toBeInTheDocument()
  })

  it('offers no Post on a closed period — the button could only 422', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(getGlPeriods).mockResolvedValue([makeGlPeriod({ state: 'closed' })])
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    // Reopen appearing proves `me` resolved and canManage is true — only
    // then is the Post absence meaningful.
    expect(await within(card).findByRole('button', { name: 'Reopen 2026-P07' })).toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Post 2026-P07' })).not.toBeInTheDocument()
  })

  it('posts the period bounds, disables while pending, and renders per-grain outcomes', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    let resolvePost!: (outcomes: GlPostOutcome[]) => void
    vi.mocked(postGlRange).mockImplementation(
      () => new Promise((resolve) => (resolvePost = resolve)),
    )
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    const postButton = await within(card).findByRole('button', { name: 'Post 2026-P07' })
    fireEvent.click(postButton)

    await waitFor(() =>
      expect(postGlRange).toHaveBeenCalledWith({
        property_id: 'HISJ',
        date_from: '2026-07-01',
        date_to: '2026-07-31',
      }),
    )
    // One engine run per (date, source) grain — gl_posting.POSTING_SOURCES
    // is where the grains are enumerated; no double-fire.
    expect(postButton).toBeDisabled()

    resolvePost([
      makeOutcome({ status: 'posted', entry_id: 41 }),
      makeOutcome({ source_type: 'payroll_daily', status: 'noop' }),
      makeOutcome({
        business_date: '2026-07-02',
        status: 'failed',
        message: 'unmapped transaction code ABC',
      }),
    ])

    // One row per (date, source): status word, entry #N when set, and the
    // failed row's message visible without any interaction.
    expect(await within(card).findByText('entry #41')).toBeInTheDocument()
    const postedRow = within(card).getByText('entry #41').closest('tr')!
    expect(within(postedRow).getByText('2026-07-01')).toBeInTheDocument()
    expect(within(postedRow).getByText('pms_daily')).toBeInTheDocument()
    expect(within(postedRow).getByText('posted')).toBeInTheDocument()
    expect(within(card).getByText('noop')).toBeInTheDocument()
    expect(within(card).getByText('payroll_daily')).toBeInTheDocument()
    const failedRow = within(card).getByText('failed').closest('tr')!
    expect(within(failedRow).getByText('2026-07-02')).toBeInTheDocument()
    expect(within(failedRow).getByText('unmapped transaction code ABC')).toBeInTheDocument()
    expect(postButton).toBeEnabled()
  })

  it('tones the six statuses: ok / neutral / warn / neutral / danger', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(postGlRange).mockResolvedValue([
      makeOutcome({ business_date: '2026-07-01', status: 'posted' }),
      makeOutcome({ business_date: '2026-07-02', status: 'reposted' }),
      makeOutcome({ business_date: '2026-07-03', status: 'noop' }),
      makeOutcome({ business_date: '2026-07-04', status: 'reversed' }),
      makeOutcome({ business_date: '2026-07-05', status: 'skipped' }),
      makeOutcome({ business_date: '2026-07-06', status: 'failed', message: 'boom' }),
    ])
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    fireEvent.click(await within(card).findByRole('button', { name: 'Post 2026-P07' }))

    expect(await within(card).findByText('posted')).toBeInTheDocument()
    expect(within(card).getByText('posted').className).toMatch(/green/)
    expect(within(card).getByText('reposted').className).toMatch(/green/)
    expect(within(card).getByText('reversed').className).toMatch(/amber/)
    expect(within(card).getByText('failed').className).toMatch(/red/)
    // noop and skipped are neutral — skipped is the backend's honest "no
    // chart yet / nothing to post" answer, not a warning.
    expect(within(card).getByText('noop').className).not.toMatch(/green|amber|red/)
    expect(within(card).getByText('skipped').className).not.toMatch(/green|amber|red/)
  })

  it('a successful post refetches the periods and the trial balance', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(postGlRange).mockResolvedValue([makeOutcome({ entry_id: 41 })])
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    // Both queries settled once before the post.
    await screen.findByRole('region', { name: 'Trial balance 2026-P07' })
    expect(getGlPeriods).toHaveBeenCalledTimes(1)
    expect(getTrialBalance).toHaveBeenCalledTimes(1)

    fireEvent.click(await within(card).findByRole('button', { name: 'Post 2026-P07' }))
    await within(card).findByText('entry #41')
    await waitFor(() => expect(getGlPeriods).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(getTrialBalance).toHaveBeenCalledTimes(2))
  })

  it('surfaces a transport failure inline', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(postGlRange).mockRejectedValue(new ApiError(503, 'upstream down'))
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    fireEvent.click(await within(card).findByRole('button', { name: 'Post 2026-P07' }))
    expect(await within(card).findByText('upstream down')).toBeInTheDocument()
  })

  it('a post failure dies with its button: closing the period removes the red line', async () => {
    vi.mocked(getMe).mockResolvedValue(ORG_ADMIN)
    vi.mocked(getGlPeriods)
      .mockResolvedValueOnce([makeGlPeriod()])
      .mockResolvedValue([makeGlPeriod({ state: 'closed' })])
    vi.mocked(postGlRange).mockRejectedValue(new ApiError(503, 'upstream down'))
    vi.mocked(closeGlPeriod).mockResolvedValue({
      period_key: '2026-P07',
      state: 'closed',
      unposted_dates: [],
      orphaned_dates: [],
    })
    renderPage('/gl?period=2026-P07')
    const card = await screen.findByRole('region', { name: 'Period 2026-P07' })
    fireEvent.click(await within(card).findByRole('button', { name: 'Post 2026-P07' }))
    expect(await within(card).findByText('upstream down')).toBeInTheDocument()

    fireEvent.click(within(card).getByRole('button', { name: 'Close 2026-P07' }))
    fireEvent.click(within(card).getByRole('button', { name: 'Yes, close' }))
    expect(await within(card).findByText('Closed')).toBeInTheDocument()
    // The Post button is gone with the closed state, and its failure line
    // with it — no unlabeled red line under the reopen form.
    expect(within(card).queryByText('upstream down')).not.toBeInTheDocument()
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
