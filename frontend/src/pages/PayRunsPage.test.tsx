import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT, HISJ_PROPERTY } from '../test/fixtures'
import { createAppRouter } from '../router'
import type { PayRunDetail, PayRunLines, PayRunSummary } from '../api/types'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getMe: vi.fn(),
  getProperties: vi.fn(),
  getPayRuns: vi.fn(),
  getPayRun: vi.fn(),
  getPayRunLines: vi.fn(),
  createPayRun: vi.fn(),
  fetchPayRunResults: vi.fn(),
}))
import {
  ApiError,
  createPayRun,
  fetchPayRunResults,
  getMe,
  getPayRun,
  getPayRunLines,
  getPayRuns,
  getProperties,
} from '../api/client'

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/payroll'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

const SUBMITTED: PayRunSummary = {
  pay_run_id: 1, property_id: 'HISJ',
  period_start: '2026-07-06', period_end: '2026-07-19', check_date: '2026-07-24',
  status: 'submitted', provider: 'gusto',
}

const SUBMITTED_DETAIL: PayRunDetail = {
  ...SUBMITTED, failure_reason: null, department_aggregates: [],
}

// Department AGGREGATE only: 640.00 is the summed dept gross. A per-employee
// gross (320.00 for either of the two employees behind it) must never appear.
const PROCESSED_DETAIL: PayRunDetail = {
  ...SUBMITTED, status: 'processed', failure_reason: null,
  department_aggregates: [
    { department: 'Housekeeping', hours: '32.00', gross: '640.00', employer_burden: '64.00' },
  ],
}

// The C3 per-employee detail behind the aggregate: two $320 gross lines.
const RUN_LINES: PayRunLines = {
  pay_run_id: 1, status: 'processed',
  lines: [
    {
      employee_id: 11, employee_name: 'Hank Housekeeper', hours: '16.00',
      gross: '320.00', employee_taxes: '48.00', employer_taxes: '32.00', net: '272.00',
    },
    {
      employee_id: 12, employee_name: 'Rita Roomer', hours: '16.00',
      gross: '320.00', employee_taxes: '48.00', employer_taxes: '32.00', net: '272.00',
    },
  ],
}

beforeEach(() => {
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['payroll_admin'] })
  vi.mocked(getProperties).mockResolvedValue([HISJ_PROPERTY])
  vi.mocked(getPayRuns).mockResolvedValue([])
  vi.mocked(getPayRun).mockResolvedValue(SUBMITTED_DETAIL)
  // The audited-read tests count calls — start each test at zero (no global
  // clearMocks in the vitest config).
  vi.mocked(getPayRunLines).mockClear()
  vi.mocked(getPayRunLines).mockResolvedValue(RUN_LINES)
  vi.mocked(createPayRun).mockResolvedValue(SUBMITTED)
  vi.mocked(fetchPayRunResults).mockResolvedValue({ status: 'processed', lines: 2 })
})

async function submitCreateForm() {
  fireEvent.change(await screen.findByLabelText('In period'), {
    target: { value: '2026-07-06' },
  })
  await userEvent.click(screen.getByRole('button', { name: 'Create pay run' }))
}

describe('PayRunsPage', () => {
  it('renders the preflight blocker list verbatim on a 422', async () => {
    vi.mocked(createPayRun).mockRejectedValue(new ApiError(
      422,
      'employee 7 (Hank H): no pay rate on file; ' +
        'no pay schedule configured for property HISJ',
    ))
    renderPage()
    await submitCreateForm()

    const alert = await screen.findByRole('alert', { name: 'pay run blockers' })
    const blockers = within(alert).getAllByRole('listitem').map((li) => li.textContent)
    expect(blockers).toEqual([
      'employee 7 (Hank H): no pay rate on file',
      'no pay schedule configured for property HISJ',
    ])
  })

  it('fetches results for a submitted run and renders the processed aggregates', async () => {
    vi.mocked(getPayRuns).mockResolvedValue([SUBMITTED])
    vi.mocked(getPayRun)
      .mockResolvedValueOnce(SUBMITTED_DETAIL)
      .mockResolvedValue(PROCESSED_DETAIL)
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: /2026-07-06 → 2026-07-19/ }))
    const detail = await screen.findByRole('region', { name: 'pay run detail' })
    await userEvent.click(await within(detail).findByRole('button', { name: 'Fetch results' }))
    await waitFor(() => expect(fetchPayRunResults).toHaveBeenCalledWith(1))

    // The invalidated detail query refetches as processed with the aggregates.
    expect(await within(detail).findByText('processed')).toBeInTheDocument()
    const aggregates = within(detail).getByRole('table', { name: 'Department aggregates' })
    expect(within(aggregates).getByText('Housekeeping')).toBeInTheDocument()
    expect(within(aggregates).getByText('640.00')).toBeInTheDocument()
    expect(within(aggregates).getByText('64.00')).toBeInTheDocument()
  })

  it('never renders per-employee money — aggregates only', async () => {
    vi.mocked(getPayRuns).mockResolvedValue([{ ...SUBMITTED, status: 'processed' }])
    vi.mocked(getPayRun).mockResolvedValue(PROCESSED_DETAIL)
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: /2026-07-06 → 2026-07-19/ }))
    expect(await screen.findByText('640.00')).toBeInTheDocument()
    // The per-employee gross behind the aggregate must not exist anywhere.
    expect(screen.queryByText(/320\.00/)).not.toBeInTheDocument()
    expect(document.body.textContent).not.toContain('320.00')
  })

  it('does NOT fetch employee lines on run selection — the audited read needs a click', async () => {
    vi.mocked(getPayRuns).mockResolvedValue([{ ...SUBMITTED, status: 'processed' }])
    vi.mocked(getPayRun).mockResolvedValue(PROCESSED_DETAIL)
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: /2026-07-06 → 2026-07-19/ }))
    const detail = await screen.findByRole('region', { name: 'pay run detail' })
    // The detail card (aggregates) is fully loaded …
    expect(await within(detail).findByText('640.00')).toBeInTheDocument()
    // … yet the audited per-employee endpoint was never touched.
    expect(getPayRunLines).not.toHaveBeenCalled()
    expect(within(detail).queryByRole('table', { name: 'Employee detail' })).not.toBeInTheDocument()
  })

  it('fetches employee lines once on the explicit click and renders the money', async () => {
    vi.mocked(getPayRuns).mockResolvedValue([{ ...SUBMITTED, status: 'processed' }])
    vi.mocked(getPayRun).mockResolvedValue(PROCESSED_DETAIL)
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: /2026-07-06 → 2026-07-19/ }))
    const detail = await screen.findByRole('region', { name: 'pay run detail' })
    await userEvent.click(
      await within(detail).findByRole('button', { name: 'Show employee detail' }),
    )

    const table = await within(detail).findByRole('table', { name: 'Employee detail' })
    expect(getPayRunLines).toHaveBeenCalledTimes(1)
    expect(getPayRunLines).toHaveBeenCalledWith(1)
    expect(within(table).getByText('Hank Housekeeper')).toBeInTheDocument()
    expect(within(table).getAllByText('320.00')).toHaveLength(2)
    expect(within(table).getAllByText('272.00')).toHaveLength(2)
  })

  it('does NOT refetch employee lines on window focus — every audited read needs a click', async () => {
    vi.mocked(getPayRuns).mockResolvedValue([{ ...SUBMITTED, status: 'processed' }])
    vi.mocked(getPayRun).mockResolvedValue(PROCESSED_DETAIL)
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: /2026-07-06 → 2026-07-19/ }))
    const detail = await screen.findByRole('region', { name: 'pay run detail' })
    await userEvent.click(
      await within(detail).findByRole('button', { name: 'Show employee detail' }),
    )
    await within(detail).findByRole('table', { name: 'Employee detail' })
    expect(getPayRunLines).toHaveBeenCalledTimes(1)

    // Simulate the user tabbing away and back: TanStack Query v5's
    // focusManager listens for window 'visibilitychange' (plus 'focus' in
    // older setups) and refetches every stale query by default.
    const detailCallsBefore = vi.mocked(getPayRun).mock.calls.length
    await act(async () => {
      window.dispatchEvent(new Event('visibilitychange'))
      window.dispatchEvent(new Event('focus'))
      await Promise.resolve()
    })
    // Prove the focus-refetch mechanism actually fired in this environment:
    // the (un-audited) detail query DID refetch …
    await waitFor(() =>
      expect(vi.mocked(getPayRun).mock.calls.length).toBeGreaterThan(detailCallsBefore),
    )
    // … while the audited lines query stayed at exactly the one human click.
    expect(getPayRunLines).toHaveBeenCalledTimes(1)
  })

  it('switching runs hides the employee table and does not auto-fetch — the reveal resets', async () => {
    const RUN2: PayRunSummary = {
      ...SUBMITTED, pay_run_id: 2, status: 'processed',
      period_start: '2026-07-20', period_end: '2026-08-02', check_date: '2026-08-07',
    }
    vi.mocked(getPayRuns).mockResolvedValue([{ ...SUBMITTED, status: 'processed' }, RUN2])
    vi.mocked(getPayRun).mockImplementation(async (id) =>
      id === 1 ? PROCESSED_DETAIL : { ...RUN2, failure_reason: null, department_aggregates: [] },
    )
    renderPage()

    // Reveal run 1's employee detail.
    await userEvent.click(await screen.findByRole('button', { name: /2026-07-06 → 2026-07-19/ }))
    const detail = await screen.findByRole('region', { name: 'pay run detail' })
    await userEvent.click(
      await within(detail).findByRole('button', { name: 'Show employee detail' }),
    )
    await within(detail).findByRole('table', { name: 'Employee detail' })
    expect(getPayRunLines).toHaveBeenCalledTimes(1)

    // Switch to run 2: the table is gone, the button is back, NO new fetch.
    await userEvent.click(screen.getByRole('button', { name: /2026-07-20 → 2026-08-02/ }))
    await within(detail).findByText(/Run #2/)
    expect(within(detail).queryByRole('table', { name: 'Employee detail' })).not.toBeInTheDocument()
    expect(
      within(detail).getByRole('button', { name: 'Show employee detail' }),
    ).toBeInTheDocument()
    expect(getPayRunLines).toHaveBeenCalledTimes(1) // still only run 1's explicit click
  })

  it('gates the page on payroll_admin', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    renderPage()
    expect(
      await screen.findByText('Pay runs require the payroll_admin role.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create pay run' })).not.toBeInTheDocument()
  })
})
