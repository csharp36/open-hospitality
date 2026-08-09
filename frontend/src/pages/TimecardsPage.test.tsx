import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'
import { createAppRouter } from '../router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getTimecards: vi.fn(),
  getTimecard: vi.fn(),
  approveTimecard: vi.fn(),
  reopenTimecard: vi.fn(),
  getPunchPhoto: vi.fn(),
  getMe: vi.fn(),
}))
import { approveTimecard, reopenTimecard, getPunchPhoto, getTimecard, getTimecards, getMe } from '../api/client'

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/timecards'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

const CARD = {
  timecard_id: 1, employee_id: 7, employee_name: 'Hank H',
  period_start: '2026-07-06', period_end: '2026-07-19', status: 'open',
  total_minutes: 480,
  days: [{
    business_date: '2026-07-07', worked_minutes: 480, warnings: ['no_meal_break'],
    punches: [
      { punch_id: 11, punch_type: 'clock_in', punched_at: '2026-07-07T09:00:00+00:00', has_photo: true, match_state: null, match_score: null },
      { punch_id: 12, punch_type: 'clock_out', punched_at: '2026-07-07T17:00:00+00:00', has_photo: false, match_state: null, match_score: null },
    ],
  }],
}

// F6: one verified, one unverified (red, gates approval), one cold-start grey.
const MATCHED_CARD = {
  ...CARD,
  days: [{
    business_date: '2026-07-07', worked_minutes: 480, warnings: [],
    punches: [
      { punch_id: 21, punch_type: 'clock_in', punched_at: '2026-07-07T09:00:00+00:00', has_photo: true, match_state: 'verified' as const, match_score: 0.94 },
      { punch_id: 22, punch_type: 'lunch_start', punched_at: '2026-07-07T12:00:00+00:00', has_photo: true, match_state: 'no_template' as const, match_score: null },
      { punch_id: 23, punch_type: 'clock_out', punched_at: '2026-07-07T17:00:00+00:00', has_photo: true, match_state: 'unverified' as const, match_score: 0.31 },
    ],
  }],
}

beforeEach(() => {
  // Call history, not just implementations: two tests assert getPunchPhoto was
  // NOT called, which an earlier test's click would otherwise falsify.
  vi.clearAllMocks()
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
  vi.mocked(getTimecards).mockResolvedValue([CARD])
  vi.mocked(getTimecard).mockResolvedValue(CARD)
  vi.mocked(approveTimecard).mockResolvedValue({ ...CARD, status: 'approved' })
  vi.mocked(reopenTimecard).mockResolvedValue(CARD)
  vi.mocked(getPunchPhoto).mockResolvedValue(new Blob(['x'], { type: 'image/jpeg' }))
  // jsdom has no object-URL support; the component only needs a stable string.
  URL.createObjectURL = vi.fn(() => 'blob:punch-photo')
  URL.revokeObjectURL = vi.fn()
})

describe('TimecardsPage', () => {
  it('lists cards and shows hours + warnings on open', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const detail = await screen.findByRole('region', { name: 'timecard detail' })
    // "8h", not "8h 0m": whole hours drop the zero-minute suffix.
    expect(within(detail).getAllByText(/8h/).length).toBeGreaterThan(0)
    expect(within(detail).getByText(/no_meal_break/)).toBeInTheDocument()
  })

  it('approves a card (no acknowledgments needed)', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(approveTimecard).toHaveBeenCalledWith(1, []))
  })

  // The verdict now rides on the timeline marker; opening one states it.
  it('shows match badges: green verified with score, grey no-template, red unverified', async () => {
    vi.mocked(getTimecard).mockResolvedValue(MATCHED_CARD)
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const detail = await screen.findByRole('region', { name: 'timecard detail' })

    await userEvent.click(within(detail).getByRole('button', { name: /^Clock In/ }))
    expect(within(detail).getByText('verified 0.94')).toBeInTheDocument()
    await userEvent.click(within(detail).getByRole('button', { name: /^Lunch Start/ }))
    expect(within(detail).getByText('no template')).toBeInTheDocument()
    await userEvent.click(within(detail).getByRole('button', { name: /^Clock Out/ }))
    expect(within(detail).getByText('unverified')).toBeInTheDocument()
  })

  it('an unverified punch blocks Approve until its checkbox is ticked', async () => {
    vi.mocked(getTimecard).mockResolvedValue(MATCHED_CARD)
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const detail = await screen.findByRole('region', { name: 'timecard detail' })

    const approveBtn = within(detail).getByRole('button', { name: 'Approve' })
    expect(approveBtn).toBeDisabled()
    expect(within(detail).getByText(/1 unverified punch/)).toBeInTheDocument()
    // Only the RED punch gets a checkbox — grey/green never gate.
    const boxes = within(detail).getAllByRole('checkbox')
    expect(boxes).toHaveLength(1)

    await userEvent.click(boxes[0]!)
    expect(approveBtn).toBeEnabled()
    await userEvent.click(approveBtn)
    await waitFor(() => expect(approveTimecard).toHaveBeenCalledWith(1, [23]))
  })

  it('an approved card shows badges but no acknowledgment checkboxes', async () => {
    vi.mocked(getTimecard).mockResolvedValue({ ...MATCHED_CARD, status: 'approved' })
    vi.mocked(getTimecards).mockResolvedValue([{ ...CARD, status: 'approved' }])
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const detail = await screen.findByRole('region', { name: 'timecard detail' })
    await userEvent.click(within(detail).getByRole('button', { name: /^Clock Out/ }))
    expect(within(detail).getByText('unverified')).toBeInTheDocument()
    expect(within(detail).queryAllByRole('checkbox')).toHaveLength(0)
  })

  it('an approved card offers Reopen, which calls the endpoint (H3)', async () => {
    vi.mocked(getTimecards).mockResolvedValue([{ ...CARD, status: 'approved' }])
    vi.mocked(getTimecard).mockResolvedValue({ ...CARD, status: 'approved' })
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const detail = await screen.findByRole('region', { name: 'timecard detail' })
    // Approved means locked: no Approve button — Reopen is the only act left.
    expect(within(detail).queryByRole('button', { name: 'Approve' })).toBeNull()
    await userEvent.click(within(detail).getByRole('button', { name: 'Reopen' }))
    await waitFor(() => expect(reopenTimecard).toHaveBeenCalledWith(1))
  })

  it('an open card shows no Reopen button', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const detail = await screen.findByRole('region', { name: 'timecard detail' })
    expect(within(detail).queryByRole('button', { name: 'Reopen' })).toBeNull()
    expect(within(detail).getByRole('button', { name: 'Approve' })).toBeInTheDocument()
  })

  // The list is one row per employee PER PERIOD, so it is long by construction.
  it('filters by name, period and status, and pages the rest', async () => {
    // 25 cards: enough to need a second page at 20 per page.
    const many = Array.from({ length: 25 }, (_, i) => ({
      ...CARD,
      timecard_id: 100 + i,
      employee_name: `Person ${String(i).padStart(2, '0')}`,
      // One card sits in an older period and is already approved, so each
      // filter has something to exclude.
      period_start: i === 0 ? '2026-06-22' : '2026-07-06',
      status: i === 0 ? 'approved' : 'open',
    }))
    vi.mocked(getTimecards).mockResolvedValue(many)
    renderPage()

    const table = await screen.findByRole('table', { name: 'Timecards' })
    // Page 1 holds PAGE_SIZE rows, not all 25 — plus the header row.
    expect(within(table).getAllByRole('row')).toHaveLength(21)
    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(within(screen.getByRole('table', { name: 'Timecards' })).getAllByRole('row'))
      .toHaveLength(6)

    // A name filter narrows to one AND returns to page 1 — otherwise the
    // matches would sit on a page nobody is looking at.
    await userEvent.type(screen.getByLabelText('Filter By Employee'), 'Person 03')
    expect(screen.getByRole('button', { name: 'Person 03' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Person 04' })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Clear Filters' }))
    await userEvent.selectOptions(screen.getByLabelText('Filter By Status'), 'approved')
    // Person 00 is the only approved card.
    expect(screen.getByRole('button', { name: 'Person 00' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Person 01' })).not.toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('Filter By Pay Period'), '2026-07-06')
    // approved + the July period is nobody: an empty result says so rather
    // than silently showing page 1 of the unfiltered list.
    expect(screen.getByText('No timecards match these filters.')).toBeInTheDocument()
  })

  it('reads dates as month, day and year rather than an ISO string', async () => {
    renderPage()
    // Twice over: the row's Pay Period cell and the filter's option for it.
    expect(await screen.findAllByText('July 6, 2026')).toHaveLength(2)
    expect(screen.queryByText('2026-07-06')).not.toBeInTheDocument()
  })

  it('opens the detail in a dialog, not appended under the list', async () => {
    renderPage()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('region', { name: 'timecard detail' })).toBeInTheDocument()
    // And Escape closes it — the list is still there underneath.
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Timecards' })).toBeInTheDocument()
  })

  it('loads a punch photo only on opening that punch (the audited read)', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const detail = await screen.findByRole('region', { name: 'timecard detail' })
    // Rendering the day must NOT fetch any photo — each read is audited
    // server-side as "this manager saw this face", so the fetch is tied to
    // asking for ONE punch by name.
    expect(getPunchPhoto).not.toHaveBeenCalled()
    await userEvent.click(within(detail).getByRole('button', { name: /^Clock In/ }))
    await waitFor(() => expect(getPunchPhoto).toHaveBeenCalledWith(11))
    expect(await within(detail).findByAltText('clock in punch photo')).toBeInTheDocument()
  })

  it('says so for a purged punch, and fetches nothing for it', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    const detail = await screen.findByRole('region', { name: 'timecard detail' })
    // clock_out carries has_photo: false — opening it must not call the
    // endpoint at all, and must say why there is nothing to look at.
    await userEvent.click(within(detail).getByRole('button', { name: /^Clock Out/ }))
    expect(within(detail).getByText(/no photo/)).toBeInTheDocument()
    expect(getPunchPhoto).not.toHaveBeenCalled()
  })
})
