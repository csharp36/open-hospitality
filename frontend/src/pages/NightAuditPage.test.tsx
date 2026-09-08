// Night-audit page flow with the api client mocked: slots/pack uploads and
// their inline refusals, the verification rows and the AR adjust form, the
// segments reconciliation's exact-decimal Save gate, roll gating with visible
// blockers, and the property-switch isolation the T3 review flagged as
// Critical (typed state must never survive into another property).

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
  getNightAudit: vi.fn(),
  postNightAuditUpload: vi.fn(),
  postNightAuditAdjust: vi.fn(),
  postNightAuditRoll: vi.fn(),
  postNightAuditSegments: vi.fn(),
}))

import {
  ApiError,
  getMe,
  getNightAudit,
  getProperties,
  postNightAuditAdjust,
  postNightAuditRoll,
  postNightAuditSegments,
  postNightAuditUpload,
} from '../api/client'
import { createAppRouter } from '../router'
import {
  AUTHED_CONTEXT,
  HISJ_PROPERTY,
  SSSJ_PROPERTY,
  makeNightAuditCheck,
  makeNightAuditSegments,
  makeNightAuditState,
} from '../test/fixtures'
import { AuthContext } from '../auth/authContext'

// Returns the QueryClient so tests can pre-cache the other property's state —
// the cached-switch path where the page body swaps without an unmount.
function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/night-audit'] }))
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

beforeEach(() => {
  vi.clearAllMocks()
  // The Layout picker persists the chosen property (usali.property); without
  // this, a switch test leaves the NEXT test starting on SSSJ and its own
  // "switch" becomes a no-op that asserts nothing.
  localStorage.clear()
  vi.mocked(getProperties).mockResolvedValue([HISJ_PROPERTY, SSSJ_PROPERTY])
  vi.mocked(getMe).mockResolvedValue({ subject: 'u1', username: 'tester', roles: [] })
  vi.mocked(getNightAudit).mockResolvedValue(makeNightAuditState())
})

function pdf() {
  return new File(['%PDF-1.4 fake'], 'audit.pdf', { type: 'application/pdf' })
}

/**
 * The visible Upload button opens a native file picker jsdom cannot drive, so
 * tests land the change on the hidden input itself; its aria-label is only a
 * handle here, never the thing under test (jsdom renders display:none inputs
 * findable by label — a browser-visibility claim this suite does not make).
 */
function chooseFile(label: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { files: [pdf()] } })
}

// A failing AR check with an adjustable close — the shape the adjust form
// needs. Same name constant for every property on purpose: the isolation
// tests below rely on nothing but the key differing across properties.
const FAILING_AR = makeNightAuditCheck({
  status: 'fail',
  detail: 'AR close misses the roll-forward',
  delta: '-0.50',
  adjust: {
    business_date: '2026-07-06',
    ledger_code: 'AR',
    stored: '6281.40',
    suggested: '6281.90',
  },
})

describe('NightAuditPage state display', () => {
  it('renders the business date, closed-through, and roll window from the state', async () => {
    renderPage()
    expect(await screen.findByText('Current business date')).toBeInTheDocument()
    const dateCard = screen.getByRole('region', { name: 'business date' })
    expect(within(dateCard).getByText('2026-07-07')).toBeInTheDocument()
    expect(within(dateCard).getByText(/Closed through/)).toBeInTheDocument()
    expect(within(dateCard).getByText('2026-07-06')).toBeInTheDocument()
    const rollCard = screen.getByRole('region', { name: 'roll date' })
    expect(within(rollCard).getByText('00:00–05:00')).toBeInTheDocument()
    expect(within(rollCard).getByText(/America\/Costa_Rica/)).toBeInTheDocument()
    expect(within(rollCard).getByText('01:12')).toBeInTheDocument()
    expect(within(rollCard).getByText(/\(open\)/)).toBeInTheDocument()
  })

  it('renders the no-property empty state when no property exists', async () => {
    vi.mocked(getProperties).mockResolvedValue([])
    renderPage()
    expect(await screen.findByText('No property selected yet.')).toBeInTheDocument()
    expect(getNightAudit).not.toHaveBeenCalled()
  })

  it('renders a failed load loud, with no page body underneath', async () => {
    vi.mocked(getNightAudit).mockRejectedValue(new ApiError(503, 'upstream down'))
    renderPage()
    expect(await screen.findByText('Failed to load: upstream down')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'business date' })).not.toBeInTheDocument()
  })
})

describe('NightAuditPage uploads', () => {
  it('renders a 422 upload refusal beside its own slot, and only there', async () => {
    vi.mocked(postNightAuditUpload).mockRejectedValue(
      new ApiError(422, 'report is for business date 2026-07-08; the audit expects 2026-07-07'),
    )
    renderPage()
    await screen.findByText('Required reports — OPERA')
    chooseFile('Upload Manager Flash')

    const err = await screen.findByText(
      'report is for business date 2026-07-08; the audit expects 2026-07-07',
    )
    const row = screen.getByText('Manager Flash').closest('li')!
    expect(row).toContainElement(err)
    // Nothing claims success: the refused slot carries no uploaded mark, and
    // the other slot carries no error.
    expect(within(row).queryByText('✓ uploaded')).not.toBeInTheDocument()
    const otherRow = screen.getByText('Trial Balance').closest('li')!
    expect(within(otherRow).queryByText(/audit expects/)).not.toBeInTheDocument()
  })

  it('a landed upload adopts the returned state: the slot flips to uploaded', async () => {
    vi.mocked(postNightAuditUpload).mockResolvedValue({
      ...makeNightAuditState({
        slots: [
          { report_type: 'trial_balance', label: 'Trial Balance', landed: true },
          { report_type: 'manager_flash', label: 'Manager Flash', landed: true },
        ],
        all_reports_landed: true,
      }),
      sections: [],
    })
    renderPage()
    await screen.findByText('Required reports — OPERA')
    chooseFile('Upload Manager Flash')

    const row = screen.getByText('Manager Flash').closest('li')!
    expect(await within(row).findByText('✓ uploaded')).toBeInTheDocument()
    // Adopted from the mutation payload, not refetched.
    expect(getNightAudit).toHaveBeenCalledTimes(1)
  })

  it('pack mode renders the one-drop card instead of per-report slots', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({
        upload_mode: 'pack',
        pack_label: 'SkyTouch Night Audit Report Pack',
        pms_source: 'SKYTOUCH',
        slots: [
          { report_type: 'trial_balance', label: 'Trial Balance', landed: false },
          { report_type: 'manager_flash', label: 'Manager Flash', landed: false },
        ],
      }),
    )
    renderPage()
    const card = await screen.findByRole('region', { name: 'audit pack' })
    expect(within(card).getByText(/SkyTouch Night Audit Report Pack/)).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Upload audit pack (PDF)' })).toBeInTheDocument()
    // The pack replaces the slot rows — no per-report card renders.
    expect(screen.queryByRole('region', { name: 'required reports' })).not.toBeInTheDocument()
  })

  it('a pack upload renders its sections, the skipped one included', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({ upload_mode: 'pack', pack_label: 'SkyTouch pack' }),
    )
    vi.mocked(postNightAuditUpload).mockResolvedValue({
      ...makeNightAuditState({
        upload_mode: 'pack',
        pack_label: 'SkyTouch pack',
        slots: [
          { report_type: 'trial_balance', label: 'Trial Balance', landed: true },
          { report_type: 'manager_flash', label: 'Manager Flash', landed: true },
        ],
        all_reports_landed: true,
      }),
      sections: [
        { title: 'Trial Balance', report_type: 'trial_balance', staged: 12, mapped: 12, skipped: false },
        { title: 'Guest List', report_type: null, staged: 0, mapped: 0, skipped: true },
      ],
    })
    renderPage()
    const card = await screen.findByRole('region', { name: 'audit pack' })
    chooseFile('Upload audit pack')

    expect(await within(card).findByText('What the pack contained')).toBeInTheDocument()
    expect(within(card).getByText('12 rows staged · 12 mapped')).toBeInTheDocument()
    expect(
      within(card).getByText('Guest List — skipped (not part of the audit)'),
    ).toBeInTheDocument()
    // The adopted state also flips the slot list to landed.
    expect(within(card).getAllByText('uploaded')).toHaveLength(2)
  })

  it('a pack refusal renders beside the pack button with no sections claimed', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({ upload_mode: 'pack', pack_label: 'SkyTouch pack' }),
    )
    vi.mocked(postNightAuditUpload).mockRejectedValue(
      new ApiError(422, 'the pack contains no Trial Balance section'),
    )
    renderPage()
    const card = await screen.findByRole('region', { name: 'audit pack' })
    chooseFile('Upload audit pack')

    expect(
      await within(card).findByText('the pack contains no Trial Balance section'),
    ).toBeInTheDocument()
    expect(within(card).queryByText('What the pack contained')).not.toBeInTheDocument()
    expect(within(card).queryByText(/rows staged/)).not.toBeInTheDocument()
  })
})

describe('NightAuditPage verification', () => {
  it('renders pass, fail, and skipped rows distinctly; Adjust only on the adjustable fail', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({
        verification: [
          makeNightAuditCheck({ name: 'guest_ledger', status: 'pass' }),
          FAILING_AR,
          makeNightAuditCheck({
            name: 'deposit_ledger',
            status: 'skipped',
            detail: 'no deposit block on the report',
            delta: '1.00',
          }),
        ],
      }),
    )
    renderPage()
    const card = await screen.findByRole('region', { name: 'verification' })

    const passRow = within(card).getByText('guest ledger').closest('li')!
    expect(within(passRow).getByText('✓')).toBeInTheDocument()
    const failRow = within(card).getByText('ar ledger rollforward').closest('li')!
    expect(within(failRow).getByText('✗')).toBeInTheDocument()
    expect(within(failRow).getByText('Δ -0.50')).toBeInTheDocument()
    const skippedRow = within(card).getByText('deposit ledger').closest('li')!
    expect(within(skippedRow).getByText('–')).toBeInTheDocument()
    // A skipped check shows no delta even when the payload carries one.
    expect(within(skippedRow).queryByText(/Δ/)).not.toBeInTheDocument()
    // Exactly one Adjust affordance, and it belongs to the adjustable fail.
    expect(within(card).getAllByRole('button', { name: 'Adjust…' })).toHaveLength(1)
    expect(within(failRow).getByRole('button', { name: 'Adjust…' })).toBeInTheDocument()
  })

  it('locks the correction Save until the reason has three real characters', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({ verification: [FAILING_AR] }),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Adjust…' }))
    const save = screen.getByRole('button', { name: 'Save correction' })
    expect(save).toBeDisabled()
    // Whitespace is not a reason — three spaces satisfy minLength, not the gate.
    fireEvent.change(screen.getByLabelText('Adjustment reason'), { target: { value: '   ' } })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Adjustment reason'), { target: { value: ' ok ' } })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Adjustment reason'), {
      target: { value: 'late transfer' },
    })
    expect(save).toBeEnabled()
  })

  it('submitting a correction posts amount + reason and adopts the returned state', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({ verification: [FAILING_AR] }),
    )
    vi.mocked(postNightAuditAdjust).mockResolvedValue(
      makeNightAuditState({
        verification: [makeNightAuditCheck({ detail: 'AR close ties after correction' })],
      }),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Adjust…' }))
    // The corrected close arrives prefilled with the server's suggestion.
    expect(screen.getByLabelText('Corrected close')).toHaveValue('6281.90')
    fireEvent.change(screen.getByLabelText('Adjustment reason'), {
      target: { value: 'late city-ledger transfer' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction' }))

    await waitFor(() =>
      expect(postNightAuditAdjust).toHaveBeenCalledWith('HISJ', {
        corrected_amount: '6281.90',
        reason: 'late city-ledger transfer',
      }),
    )
    // The row flips to pass from the adopted payload — no refetch round trip.
    const card = screen.getByRole('region', { name: 'verification' })
    expect(await within(card).findByText('AR close ties after correction')).toBeInTheDocument()
    expect(within(card).getByText('✓')).toBeInTheDocument()
    expect(screen.queryByLabelText('Adjustment reason')).not.toBeInTheDocument()
    expect(getNightAudit).toHaveBeenCalledTimes(1)
  })

  it('renders a refused correction beside its form, edits kept for a retry', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({ verification: [FAILING_AR] }),
    )
    vi.mocked(postNightAuditAdjust).mockRejectedValue(
      new ApiError(403, 'night_audit_api.require_auditor refused the role'),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Adjust…' }))
    fireEvent.change(screen.getByLabelText('Adjustment reason'), {
      target: { value: 'late transfer' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction' }))

    const err = await screen.findByText('night_audit_api.require_auditor refused the role')
    expect(err.closest('form')).toBe(screen.getByLabelText('Corrected close').closest('form'))
    expect(screen.getByLabelText('Adjustment reason')).toHaveValue('late transfer')
    expect(screen.getByRole('button', { name: 'Save correction' })).toBeEnabled()
  })
})

describe('NightAuditPage segments', () => {
  function failingSegmentsState() {
    return makeNightAuditState({ segments: makeNightAuditSegments() })
  }

  it('renders pass-status cells as plain text with no Save control', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({
        segments: makeNightAuditSegments({
          status: 'pass',
          detail: 'both sums tie',
          revenue_total: '10456.37',
          revenue_delta: '0',
          rows: [
            { code: 'TRAN', description: 'Transient', rooms: '30', room_revenue: '7842.28' },
            { code: 'GRP', description: 'Group', rooms: '10', room_revenue: '2614.09' },
          ],
        }),
      }),
    )
    renderPage()
    const card = await screen.findByRole('region', { name: 'market code reconciliation' })
    expect(within(card).getByText('2614.09')).toBeInTheDocument()
    expect(within(card).queryByRole('textbox')).not.toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Save matched values' })).not.toBeInTheDocument()
  })

  it('a typed correction moves the live sum exactly and unlocks Save at the tie', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(failingSegmentsState())
    renderPage()
    const card = await screen.findByRole('region', { name: 'market code reconciliation' })
    // Live sum before any edit: the fixture's own -4.09 miss.
    expect(within(card).getByText('10,452.28')).toBeInTheDocument()
    const save = within(card).getByRole('button', { name: 'Save matched values' })
    expect(save).toBeDisabled()
    expect(within(card).getByText(/Save unlocks when both sums match/)).toBeInTheDocument()

    fireEvent.change(within(card).getByLabelText('GRP room_revenue'), {
      target: { value: '2614.09' },
    })
    // 7842.28 + 2614.09 — exact string arithmetic, displayed re-formatted.
    expect(within(card).getByText('10,456.37')).toBeInTheDocument()
    expect(within(card).queryByText('10,452.28')).not.toBeInTheDocument()
    expect(save).toBeEnabled()
    expect(within(card).queryByText(/Save unlocks when both sums match/)).not.toBeInTheDocument()
  })

  it('unlocks at Δ exactly 0.01 — the server-parity edge — and locks at 0.02', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(failingSegmentsState())
    renderPage()
    const card = await screen.findByRole('region', { name: 'market code reconciliation' })
    const save = within(card).getByRole('button', { name: 'Save matched values' })

    fireEvent.change(within(card).getByLabelText('GRP room_revenue'), {
      target: { value: '2614.10' },
    })
    expect(save).toBeEnabled()
    fireEvent.change(within(card).getByLabelText('GRP room_revenue'), {
      target: { value: '2614.11' },
    })
    expect(save).toBeDisabled()
    expect(within(card).getByText(/Save unlocks when both sums match/)).toBeInTheDocument()
  })

  it('an unparseable cell locks Save with the fix-cells message and never counts as zero', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(failingSegmentsState())
    renderPage()
    const card = await screen.findByRole('region', { name: 'market code reconciliation' })

    const revenueCell = within(card).getByLabelText('GRP room_revenue')
    fireEvent.change(revenueCell, { target: { value: '1,234' } })
    expect(revenueCell).toHaveAttribute('aria-invalid', 'true')
    expect(within(card).getByRole('button', { name: 'Save matched values' })).toBeDisabled()
    expect(within(card).getByText(/Fix the marked cells first/)).toBeInTheDocument()
    // No sum renders at all — the junk cell did not fold in as zero
    // (7842.28 alone would show as a red 7,842.28 sum).
    expect(within(card).queryByText('7,842.28')).not.toBeInTheDocument()
    expect(within(card).getAllByText('—').length).toBeGreaterThan(0)

    // Rooms cells are stricter still: whole numbers only.
    fireEvent.change(revenueCell, { target: { value: '2614.09' } })
    fireEvent.change(within(card).getByLabelText('TRAN rooms'), { target: { value: '30.5' } })
    expect(within(card).getByRole('button', { name: 'Save matched values' })).toBeDisabled()
    expect(within(card).getByText(/Fix the marked cells first/)).toBeInTheDocument()
  })

  it('posts every row on Save; a refusal keeps the typed cells for a retry', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(failingSegmentsState())
    vi.mocked(postNightAuditSegments).mockRejectedValue(
      new ApiError(422, 'stage rows failed strict parse'),
    )
    renderPage()
    const card = await screen.findByRole('region', { name: 'market code reconciliation' })
    fireEvent.change(within(card).getByLabelText('GRP room_revenue'), {
      target: { value: '2614.09' },
    })
    fireEvent.click(within(card).getByRole('button', { name: 'Save matched values' }))

    await waitFor(() =>
      expect(postNightAuditSegments).toHaveBeenCalledWith('HISJ', [
        { code: 'TRAN', rooms: '30', room_revenue: '7842.28' },
        { code: 'GRP', rooms: '10', room_revenue: '2614.09' },
      ]),
    )
    expect(await within(card).findByText('stage rows failed strict parse')).toBeInTheDocument()
    // The failed save did not eat the edits.
    expect(within(card).getByLabelText('GRP room_revenue')).toHaveValue('2614.09')
    expect(within(card).getByRole('button', { name: 'Save matched values' })).toBeEnabled()
  })
})

describe('NightAuditPage roll', () => {
  it('a blocked roll disables the button and names the missing report in visible words', async () => {
    renderPage() // default state: manager_flash missing, can_roll false
    const card = await screen.findByRole('region', { name: 'roll date' })
    expect(
      within(card).getByRole('button', { name: 'Roll to next business date →' }),
    ).toBeDisabled()
    expect(
      within(card).getByText(/reports still missing: Manager Flash/),
    ).toBeInTheDocument()
  })

  it('names failing checks and a closed window as blockers too', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({
        slots: [{ report_type: 'trial_balance', label: 'Trial Balance', landed: true }],
        verification: [FAILING_AR],
        window: { open: false, hours: '00:00–05:00', timezone: 'America/Costa_Rica', local_time: '14:30' },
      }),
    )
    renderPage()
    const card = await screen.findByRole('region', { name: 'roll date' })
    expect(await within(card).findByText(/checks above are failing/)).toBeInTheDocument()
    expect(within(card).getByText(/the roll window is closed/)).toBeInTheDocument()
    expect(within(card).queryByText(/reports still missing/)).not.toBeInTheDocument()
  })

  it('an enabled roll adopts the advanced date from the payload without a refetch', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(
      makeNightAuditState({
        slots: [
          { report_type: 'trial_balance', label: 'Trial Balance', landed: true },
          { report_type: 'manager_flash', label: 'Manager Flash', landed: true },
        ],
        all_reports_landed: true,
        can_roll: true,
      }),
    )
    vi.mocked(postNightAuditRoll).mockResolvedValue(
      makeNightAuditState({
        business_date: '2026-07-08',
        closed_through: '2026-07-07',
        last_rolled_at: '2026-07-08T07:12:00Z',
      }),
    )
    renderPage()
    const card = await screen.findByRole('region', { name: 'roll date' })
    const button = within(card).getByRole('button', { name: 'Roll to next business date →' })
    expect(button).toBeEnabled()
    fireEvent.click(button)

    await waitFor(() => expect(postNightAuditRoll).toHaveBeenCalledWith('HISJ'))
    const dateCard = screen.getByRole('region', { name: 'business date' })
    expect(await within(dateCard).findByText('2026-07-08')).toBeInTheDocument()
    expect(within(dateCard).getByText('2026-07-07')).toBeInTheDocument() // closed through
    // The displayed date came from the mutation payload — no extra GET.
    expect(getNightAudit).toHaveBeenCalledTimes(1)
  })

  it('renders a refused roll beside the button', async () => {
    vi.mocked(getNightAudit).mockResolvedValue(makeNightAuditState({ can_roll: true }))
    vi.mocked(postNightAuditRoll).mockRejectedValue(
      new ApiError(422, 'the roll window closed at 05:00'),
    )
    renderPage()
    const card = await screen.findByRole('region', { name: 'roll date' })
    fireEvent.click(within(card).getByRole('button', { name: 'Roll to next business date →' }))
    expect(await within(card).findByText('the roll window closed at 05:00')).toBeInTheDocument()
  })
})

describe('NightAuditPage property switch', () => {
  // Both properties get IDENTICAL page shapes — same check name, same market
  // codes, same slots — so nothing but the property in the child keys can
  // separate them. The other property's state is pre-cached before the
  // switch: the body swaps in place with no unmount-through-loading pass,
  // which is exactly the path the T3 review's Critical named.
  function stateFor(propertyId: string) {
    return makeNightAuditState({
      property_id: propertyId,
      business_date: propertyId === 'HISJ' ? '2026-07-07' : '2026-07-05',
      verification: [FAILING_AR],
      segments: makeNightAuditSegments(),
    })
  }

  it('an open adjust form and typed segment cells die on a cached switch', async () => {
    vi.mocked(getNightAudit).mockImplementation((pid) => Promise.resolve(stateFor(pid)))
    const queryClient = renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Adjust…' }))
    fireEvent.change(screen.getByLabelText('Corrected close'), { target: { value: '9999.99' } })
    fireEvent.change(screen.getByLabelText('Adjustment reason'), {
      target: { value: 'typed for HISJ' },
    })
    fireEvent.change(screen.getByLabelText('GRP room_revenue'), { target: { value: '2614.09' } })

    queryClient.setQueryData(['night-audit', 'SSSJ'], stateFor('SSSJ'))
    fireEvent.change(screen.getByLabelText('Active property'), { target: { value: 'SSSJ' } })
    expect(await screen.findByText('2026-07-05')).toBeInTheDocument()

    // The adjust form is gone — closed, not retargeted at SSSJ's books.
    expect(screen.queryByLabelText('Adjustment reason')).not.toBeInTheDocument()
    expect(screen.queryByDisplayValue('9999.99')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Adjust…' })).toBeInTheDocument()
    // The segment cell is SSSJ's own value again, not HISJ's edit.
    expect(screen.getByLabelText('GRP room_revenue')).toHaveValue('2610.00')
  })

  it('a failed upload label dies with its property on a cached switch', async () => {
    vi.mocked(getNightAudit).mockImplementation((pid) => Promise.resolve(stateFor(pid)))
    vi.mocked(postNightAuditUpload).mockRejectedValue(
      new ApiError(422, 'report is for business date 2026-07-08; the audit expects 2026-07-07'),
    )
    const queryClient = renderPage()
    await screen.findByText('Required reports — OPERA')
    chooseFile('Upload Manager Flash')
    await screen.findByText(/the audit expects 2026-07-07/)

    queryClient.setQueryData(['night-audit', 'SSSJ'], stateFor('SSSJ'))
    fireEvent.change(screen.getByLabelText('Active property'), { target: { value: 'SSSJ' } })
    expect(await screen.findByText('2026-07-05')).toBeInTheDocument()

    // SSSJ's identical slot row carries no leftover refusal from HISJ.
    expect(screen.queryByText(/the audit expects 2026-07-07/)).not.toBeInTheDocument()
  })
})
