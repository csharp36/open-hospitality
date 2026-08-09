// Reports page flow with the API client mocked: picker populates from
// /api/properties, picking a property + month (derived from the property's
// date window) fires getCpaPack, all three pack sections render with honest
// A/R labeling, and "Download JSON" produces a blob URL of the API payload.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'

// importOriginal spread keeps the real ApiError class (lib/errors depends on
// it for instanceof checks) while stubbing the fetchers.
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getProperties: vi.fn(),
  getCpaPack: vi.fn(),
}))

import { getCpaPack, getProperties } from '../api/client'
import { createAppRouter } from '../router'
import { AUTHED_CONTEXT, HISJ_PROPERTY, SSSJ_PROPERTY, makeCpaPack } from '../test/fixtures'
import { AuthContext } from '../auth/authContext'

function renderPage(initialPath = '/reports') {
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
  vi.mocked(getCpaPack).mockResolvedValue(makeCpaPack())
})

describe('ReportsPage', () => {
  it('derives month options from the property date window and fetches the pack', async () => {
    renderPage()
    fireEvent.change(await screen.findByLabelText('Active property'), { target: { value: 'HISJ' } })

    // HISJ spans 2026-07-01..07 -> exactly one month option besides the
    // placeholder. findByRole: the search-param navigation flushes async.
    const monthSelect = screen.getByLabelText('Month')
    expect(await within(monthSelect).findByRole('option', { name: '2026-07' })).toBeInTheDocument()
    expect(within(monthSelect).getAllByRole('option')).toHaveLength(2)

    fireEvent.change(monthSelect, { target: { value: '2026-07' } })
    expect(await screen.findByText('TOTAL OPERATING REVENUE')).toBeInTheDocument()
    expect(getCpaPack).toHaveBeenCalledWith('HISJ', '2026-07')
  })

  it('renders the Sales, Taxes, and A/R sections from the pack', async () => {
    renderPage('/reports?property=HISJ&month=2026-07')

    // Sales: lines + emphasized TOR.
    const sales = await screen.findByRole('region', { name: 'Sales' })
    expect(within(sales).getByText('Transient Rooms Revenue')).toBeInTheDocument()
    expect(within(sales).getByText('10,456.37')).toBeInTheDocument()
    expect(within(sales).getByText('TOTAL OPERATING REVENUE')).toBeInTheDocument()
    expect(within(sales).getByText('10,866.37')).toBeInTheDocument()

    // Taxes: line, GL account, total, room revenue base.
    const taxes = screen.getByRole('region', { name: 'Taxes' })
    expect(within(taxes).getByText('Occupancy Tax')).toBeInTheDocument()
    expect(within(taxes).getByText('2210')).toBeInTheDocument()
    expect(within(taxes).getAllByText('1,254.76')).toHaveLength(2) // line + total
    expect(within(taxes).getByText(/Room revenue base/)).toBeInTheDocument()

    // A/R: balances with first-reported/closing/movement.
    const ar = screen.getByRole('region', { name: 'Accounts receivable' })
    expect(within(ar).getByText('City Ledger')).toBeInTheDocument()
    expect(within(ar).getByText('5,000.00')).toBeInTheDocument()
    expect(within(ar).getByText('6,200.00')).toBeInTheDocument()
    expect(within(ar).getByText('1,200.00')).toBeInTheDocument()
  })

  it('labels the A/R opening column honestly as first-reported', async () => {
    renderPage('/reports?property=HISJ&month=2026-07')
    const ar = await screen.findByRole('region', { name: 'Accounts receivable' })

    // The column header must NOT read as a bare "Opening" — the value is the
    // first balance reported IN the month, not the prior month's close.
    expect(within(ar).getByText(/First reported balance/)).toBeInTheDocument()
    expect(within(ar).queryByText(/^Opening/)).not.toBeInTheDocument()
    expect(within(ar).getByText(/NOT the prior month's close/)).toBeInTheDocument()
  })

  it('Download JSON produces a blob URL of the pack payload', async () => {
    // jsdom implements neither createObjectURL nor revokeObjectURL — install
    // mocks so the click handler runs end-to-end.
    const createObjectURL = vi.fn(() => 'blob:cpa-pack')
    const revokeObjectURL = vi.fn()
    URL.createObjectURL = createObjectURL
    URL.revokeObjectURL = revokeObjectURL
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined)

    renderPage('/reports?property=HISJ&month=2026-07')
    fireEvent.click(await screen.findByRole('button', { name: 'Download JSON' }))

    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob))
    expect(click).toHaveBeenCalledTimes(1)
    // Revocation is deferred (setTimeout) so the download can start first.
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:cpa-pack'))

    click.mockRestore()
  })

  it('notes that CSV export stays a CLI concern', async () => {
    renderPage()
    expect(await screen.findByText(/CSV stays a CLI concern/)).toBeInTheDocument()
    expect(screen.getByText(/usali cpa-pack --format csv/)).toBeInTheDocument()
  })

  it('renders the API detail on pack fetch failure', async () => {
    const { ApiError } = await vi.importActual<typeof import('../api/client')>('../api/client')
    vi.mocked(getCpaPack).mockRejectedValue(new ApiError(404, 'unknown property: NOPE'))
    renderPage('/reports?property=HISJ&month=2026-07')
    expect(await screen.findByText(/unknown property: NOPE/)).toBeInTheDocument()
  })
})
