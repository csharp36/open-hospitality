import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, fireEvent } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'
import { createAppRouter } from '../router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getPropertyConfig: vi.fn(),
  setInventory: vi.fn(),
  addOoo: vi.fn(),
  removeOoo: vi.fn(),
  setFiscalCalendar: vi.fn(),
  getFiscalPeriods: vi.fn(),
  getProperties: vi.fn(),
  getMe: vi.fn(),
}))
import {
  getFiscalPeriods, getMe, getProperties, getPropertyConfig,
} from '../api/client'

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/property-config'] }))
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
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
  vi.mocked(getProperties).mockResolvedValue([
    { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2026-07-07', last_date: '2026-07-07', name: null },
  ])
  vi.mocked(getPropertyConfig).mockResolvedValue({
    property_id: 'HISJ', inventory: [], out_of_order: [], fiscal_calendar: null,
  })
  vi.mocked(getFiscalPeriods).mockResolvedValue({ periods: [] })
})

describe('PropertyConfigPage', () => {
  it('shows the week-start field only when 4-4-5 is chosen', async () => {
    renderPage()
    // default calendar_month -> no week-start select
    await screen.findByLabelText(/4-4-5/i)
    expect(screen.queryByLabelText(/week start/i)).toBeNull()
    fireEvent.click(screen.getByLabelText(/4-4-5/i))
    expect(screen.getByLabelText(/week start/i)).toBeInTheDocument()
  })

  it('offers all seven OOO reason codes including the DNR reasons', async () => {
    renderPage()
    const select = await screen.findByLabelText(/reason/i)
    const options = select.querySelectorAll('option')
    expect(options).toHaveLength(7)
    const values = Array.from(options).map((o) => (o as HTMLOptionElement).value)
    expect(values).toContain('do_not_rent')
    expect(values).toContain('owner_occupied')
  })

  it('shows the count in force today, not a future-dated row, as "current"', async () => {
    // Newest row is dated far in the future (a planned renovation); the count
    // in force today is the older 140-room row. inventory arrives newest-first.
    vi.mocked(getPropertyConfig).mockResolvedValue({
      property_id: 'HISJ',
      inventory: [
        { inventory_id: 2, effective_date: '2999-01-01', total_rooms: 99 },
        { inventory_id: 1, effective_date: '2020-01-01', total_rooms: 140 },
      ],
      out_of_order: [],
      fiscal_calendar: null,
    })
    renderPage()
    // "Current count" reflects the in-force 140, never the future 99.
    const current = await screen.findByText(/current count/i)
    expect(current).toHaveTextContent('140')
    expect(current).not.toHaveTextContent('99')
  })

  it('labels a future-only inventory as not yet in force', async () => {
    vi.mocked(getPropertyConfig).mockResolvedValue({
      property_id: 'HISJ',
      inventory: [{ inventory_id: 1, effective_date: '2999-01-01', total_rooms: 99 }],
      out_of_order: [],
      fiscal_calendar: null,
    })
    renderPage()
    await screen.findByText(/no count in force yet/i)
    expect(screen.queryByText(/current count/i)).toBeNull()
  })
})
