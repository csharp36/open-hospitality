// SOS page flow with the API client mocked: picker populates from
// /api/properties, picking a property + date fires getSos, clicking a
// financial line opens the drill panel with transactions + reconciliation.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'

// importOriginal spread keeps the real ApiError class (lib/errors depends on
// it for instanceof checks) while stubbing the fetchers.
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getProperties: vi.fn(),
  getSos: vi.fn(),
  getLineTransactions: vi.fn(),
}))

import { getLineTransactions, getProperties, getSos } from '../api/client'
import { createAppRouter } from '../router'
import DrillPanel from '../components/DrillPanel'
import {
  AUTHED_CONTEXT,
  HISJ_PROPERTY,
  PARKING_TXNS,
  SSSJ_PROPERTY,
  makeSosReport,
} from '../test/fixtures'
import { AuthContext } from '../auth/authContext'

function renderPage(initialPath = '/sos') {
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

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getProperties).mockResolvedValue([HISJ_PROPERTY, SSSJ_PROPERTY])
  vi.mocked(getSos).mockResolvedValue(makeSosReport())
  vi.mocked(getLineTransactions).mockResolvedValue(PARKING_TXNS)
})

describe('SosPage', () => {
  it('populates the property picker from /api/properties', async () => {
    renderPage()
    expect(await screen.findByRole('option', { name: 'HISJ — OPERA' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'SSSJ — AUTOCLERK' })).toBeInTheDocument()
  })

  it('constrains the date input to the property date span and fetches the SOS', async () => {
    renderPage()
    fireEvent.change(await screen.findByLabelText('Active property'), { target: { value: 'HISJ' } })

    const dateInput = await screen.findByLabelText('Date')
    expect(dateInput).toHaveAttribute('min', '2026-07-01')
    expect(dateInput).toHaveAttribute('max', '2026-07-07')

    fireEvent.change(dateInput, { target: { value: '2026-07-07' } })
    expect(await screen.findByText('10,866.37')).toBeInTheDocument()
    expect(getSos).toHaveBeenCalledWith({ property: 'HISJ', date: '2026-07-07' })
  })

  it('opens the drill panel on line click and reconciles the transaction sum', async () => {
    renderPage()
    fireEvent.change(await screen.findByLabelText('Active property'), { target: { value: 'HISJ' } })
    fireEvent.change(await screen.findByLabelText('Date'), { target: { value: '2026-07-07' } })

    fireEvent.click(await screen.findByRole('button', { name: 'Parking' }))

    const panel = await screen.findByRole('dialog', { name: 'Transactions: Parking' })
    expect(getLineTransactions).toHaveBeenCalledWith({
      property: 'HISJ',
      major: 'Operating Revenue',
      sub: 'Miscellaneous Income',
      line_item: 'Parking',
      from: '2026-07-07',
      to: '2026-07-07',
    })
    expect(await within(panel).findAllByText('5105')).toHaveLength(2)
    expect(within(panel).getByText('PARKING SELF')).toBeInTheDocument()
    expect(within(panel).getByText('250.00')).toBeInTheDocument()
    expect(within(panel).getByText('410.00')).toBeInTheDocument() // footer sum
    expect(within(panel).getByText(/reconciled/)).toBeInTheDocument()

    fireEvent.click(within(panel).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('reads the initial selection from the URL search params', async () => {
    renderPage('/sos?property=HISJ&date=2026-07-07')
    expect(await screen.findByText('TOTAL OPERATING REVENUE')).toBeInTheDocument()
    expect(getSos).toHaveBeenCalledWith({ property: 'HISJ', date: '2026-07-07' })
  })

  it('renders Schedule 14/15 labor sections labeled estimate when labor is present', async () => {
    vi.mocked(getSos).mockResolvedValue(
      makeSosReport({
        // TWO-employee department: cost SHOWS (not suppressed). 2 × 8h × $20 = $320.
        payroll_expense: [
          { department: 'Housekeeping', hours: '16.00', ot_hours: '0.00', est_cost: '320.0000' },
        ],
        payroll_expense_total: '320.0000',
        labor_hours_total: '16.00',
        labor_ot_hours_total: '0.00',
        labor_fte: '1.00',
      }),
    )
    renderPage('/sos?property=HISJ&date=2026-07-07')

    // Schedule 14: department line + estimated cost (dept row + total row).
    expect(
      await screen.findByText('Schedule 14 — Payroll Related Expenses'),
    ).toBeInTheDocument()
    expect(screen.getByText('Housekeeping')).toBeInTheDocument()
    expect(screen.getAllByText('320.00')).toHaveLength(2)

    // Schedule 15: hours reporting.
    expect(screen.getByText('Schedule 15 — Payroll / FTE')).toBeInTheDocument()
    expect(screen.getByText('Total Hours')).toBeInTheDocument()
    expect(screen.getByText('Overtime Hours')).toBeInTheDocument()

    // Cost is labeled an estimate wherever it surfaces (one badge per section).
    expect(screen.getAllByText('estimate')).toHaveLength(2)
  })

  it('hides cost for a single-employee department and notes suppressed + unpriced hours', async () => {
    vi.mocked(getSos).mockResolvedValue(
      makeSosReport({
        // Solo department: est_cost null -> cost cell is an em dash, excluded from
        // the total; unpriced hours booked at rate 0 are flagged separately.
        payroll_expense: [
          { department: 'Housekeeping', hours: '8.00', ot_hours: '0.00', est_cost: null },
        ],
        payroll_expense_total: '0',
        labor_hours_total: '8.00',
        labor_ot_hours_total: '0.00',
        labor_fte: '1.00',
        labor_suppressed_departments: 1,
        labor_unpriced_hours: '8.00',
      }),
    )
    renderPage('/sos?property=HISJ&date=2026-07-07')

    // The suppressed cost cell renders an em dash + inline "hidden" marker.
    const marker = await screen.findByText('hidden (single employee)')
    expect(marker.parentElement).toHaveTextContent('—')
    // Hours still show for the solo department.
    expect(screen.getByText('Housekeeping')).toBeInTheDocument()

    // Honest muted notes under Schedule 14.
    expect(screen.getByText(/Cost hidden for 1 single-employee/)).toBeInTheDocument()
    expect(screen.getByText(/Excludes 8 unpriced hours \(no rate on file\)/)).toBeInTheDocument()
  })

  it('range mode fetches with from/to and the drill window spans the range', async () => {
    renderPage()
    fireEvent.change(await screen.findByLabelText('Active property'), { target: { value: 'HISJ' } })
    fireEvent.click(screen.getByRole('radio', { name: 'Range' }))
    fireEvent.change(await screen.findByLabelText('From'), { target: { value: '2026-07-01' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-07-07' } })

    expect(await screen.findByText('TOTAL OPERATING REVENUE')).toBeInTheDocument()
    expect(getSos).toHaveBeenCalledWith({ property: 'HISJ', from: '2026-07-01', to: '2026-07-07' })

    fireEvent.click(await screen.findByRole('button', { name: 'Parking' }))
    await screen.findByRole('dialog', { name: 'Transactions: Parking' })
    expect(getLineTransactions).toHaveBeenCalledWith({
      property: 'HISJ',
      major: 'Operating Revenue',
      sub: 'Miscellaneous Income',
      line_item: 'Parking',
      from: '2026-07-01', // from !== to: drill window spans the whole range
      to: '2026-07-07',
    })
  })
})

const PARKING_LINE = {
  major: 'Operating Revenue',
  sub_category: 'Miscellaneous Income',
  line_item: 'Parking',
  total: '999.9900',
}

describe('DrillPanel reconciliation badge', () => {
  it('warns in red when the transaction sum does not match the line total', () => {
    render(
      <DrillPanel
        line={PARKING_LINE}
        from="2026-07-07"
        to="2026-07-07"
        txns={PARKING_TXNS}
        loading={false}
        error={null}
        onClose={() => {}}
      />,
    )
    expect(screen.queryByText(/reconciled/)).not.toBeInTheDocument()
    expect(screen.getByText(/does not match/)).toBeInTheDocument()
  })
})

describe('DrillPanel modal a11y', () => {
  it('focuses the Close button on open and closes on Escape', () => {
    const onClose = vi.fn()
    render(
      <DrillPanel
        line={PARKING_LINE}
        from="2026-07-07"
        to="2026-07-07"
        txns={PARKING_TXNS}
        loading={false}
        error={null}
        onClose={onClose}
      />,
    )
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
