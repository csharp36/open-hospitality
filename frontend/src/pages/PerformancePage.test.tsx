import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'
import { createAppRouter } from '../router'
import type { CoreMetrics, PerformanceResponse } from '../api/types'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getPerformance: vi.fn(),
  getProperties: vi.fn(),
  getMe: vi.fn(),
}))
import { ApiError, getMe, getPerformance, getProperties } from '../api/client'

function metrics(over: Partial<CoreMetrics> = {}): CoreMetrics {
  return {
    start: '2026-07-01',
    end: '2026-07-31',
    rooms_available: '3100',
    rooms_sold: '2480',
    adr_rooms_sold: '2480',
    room_revenue: '372000.0000',
    total_revenue: '418500.0000',
    occupancy: '0.8000',
    adr: '150.0000',
    revpar: '120.0000',
    trevpar: '135.0000',
    adr_room_basis: 'as_reported',
    ...over,
  }
}

function sampleResponse(): PerformanceResponse {
  return {
    property_id: 'HISJ',
    adr_room_basis: 'as_reported',
    period: null,
    start: '2026-07-01',
    end: '2026-07-31',
    current: metrics(),
    prior_period: metrics({ occupancy: '0.7500', adr: '140.0000' }),
    prior_year: metrics({ occupancy: '0.7000', adr: '130.0000' }),
    prior_period_delta_pct: { occupancy: '6.7', adr: '7.1', revpar: '9.0', trevpar: '8.0' },
    prior_year_delta_pct: { occupancy: '14.3', adr: '15.4', revpar: '16.0', trevpar: '17.0' },
    reconciliation: {
      occupancy: { computed: '0.8000', ingested: '0.7100', agrees: false },
      adr: { computed: '150.0000', ingested: '150.2000', agrees: true },
      revpar: { computed: '120.0000', ingested: '120.1000', agrees: true },
    },
    trends: {
      anchor: '2026-07-31',
      wow: {},
      mtd: {},
      rolling_30: {},
      dow: {},
    },
    labor: {
      labor_hours: '4960.00',
      rooms_sold: '2480.0000',
      hours_per_occupied_room: '2.0000',
      labor_cost: '124000.00',
      cost_per_occupied_room: '50.0000',
      cost_suppressed: false,
    },
    days_excluded: 0,
  }
}

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/performance'] }))
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
  vi.mocked(getPerformance).mockResolvedValue(sampleResponse())
})

describe('PerformancePage', () => {
  it('renders the four KPI values', async () => {
    renderPage()
    expect(await screen.findByText('80.0%')).toBeInTheDocument() // occupancy
    expect(screen.getByText('$150.00')).toBeInTheDocument() // ADR
    expect(screen.getByText('$120.00')).toBeInTheDocument() // RevPAR
    expect(screen.getByText('$135.00')).toBeInTheDocument() // TRevPAR
  })

  it('states the ADR room basis', async () => {
    renderPage()
    expect(await screen.findByText(/as_reported/i)).toBeInTheDocument()
  })

  it('flags a reconciliation divergence when a metric does not agree', async () => {
    renderPage()
    expect(await screen.findByText(/divergence/i)).toBeInTheDocument()
  })

  it('renders the labor productivity section with hours and cost per occupied room', async () => {
    renderPage()
    expect(await screen.findByText(/labor productivity/i)).toBeInTheDocument()
    expect(screen.getByText(/labor hours per occupied room/i)).toBeInTheDocument()
    expect(screen.getByText('2.00')).toBeInTheDocument() // hours per occupied room
    expect(screen.getByText('$50.00')).toBeInTheDocument() // cost per occupied room
  })

  it('shows the seeded data window so an empty range is not picked blind', async () => {
    vi.mocked(getProperties).mockResolvedValue([
      { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2025-08-01', last_date: '2026-07-31', name: null },
    ])
    renderPage()
    expect(await screen.findByText(/data available 2025-08-01 – 2026-07-31/i)).toBeInTheDocument()
  })

  it('defaults the window to the last month of available data, not month-to-date', async () => {
    // last_date is 2026-07-31; the default window is that month (from clamped
    // to first_date when first_date falls inside the month). A today-anchored
    // default would land on an empty current month.
    vi.mocked(getProperties).mockResolvedValue([
      { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2025-08-01', last_date: '2026-07-31', name: null },
    ])
    renderPage()
    await waitFor(() =>
      expect((screen.getByLabelText('To') as HTMLInputElement).value).toBe('2026-07-31'),
    )
    expect((screen.getByLabelText('From') as HTMLInputElement).value).toBe('2026-07-01')
  })

  it('clamps the default From up to first_date when the data starts mid-month', async () => {
    vi.mocked(getProperties).mockResolvedValue([
      { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2026-07-07', last_date: '2026-07-20', name: null },
    ])
    renderPage()
    await waitFor(() =>
      expect((screen.getByLabelText('From') as HTMLInputElement).value).toBe('2026-07-07'),
    )
    expect((screen.getByLabelText('To') as HTMLInputElement).value).toBe('2026-07-20')
  })

  it('shows a cost-withheld note when labor cost is suppressed', async () => {
    vi.mocked(getPerformance).mockResolvedValue({
      ...sampleResponse(),
      labor: {
        labor_hours: '4960.00',
        rooms_sold: '2480.0000',
        hours_per_occupied_room: '2.0000',
        labor_cost: null,
        cost_per_occupied_room: null,
        cost_suppressed: true,
      },
    })
    renderPage()
    expect(await screen.findByText(/cost withheld/i)).toBeInTheDocument()
    // hours are never withheld
    expect(screen.getByText('2.00')).toBeInTheDocument()
  })

  it('clamps the date pickers to the property data range', async () => {
    vi.mocked(getProperties).mockResolvedValue([
      { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2025-08-01', last_date: '2026-07-31', name: null },
    ])
    renderPage()
    await waitFor(() =>
      expect((screen.getByLabelText('To') as HTMLInputElement).value).toBe('2026-07-31'),
    )
    for (const label of ['From', 'To']) {
      const input = screen.getByLabelText(label) as HTMLInputElement
      expect(input).toHaveAttribute('min', '2025-08-01')
      expect(input).toHaveAttribute('max', '2026-07-31')
    }
  })

  it('shows a calm "no data for this window" note on a 409, not a red failure', async () => {
    vi.mocked(getPerformance).mockRejectedValue(
      new ApiError(409, 'HISJ is set to exclude comp/house-use from ADR, but over … refusing'),
    )
    renderPage()
    expect(await screen.findByText(/no performance data for this window/i)).toBeInTheDocument()
    expect(screen.getByText(/refusing/i)).toBeInTheDocument()
    expect(screen.queryByText(/failed to load/i)).not.toBeInTheDocument()
  })

  it('still shows a red failure for a real error (non-409)', async () => {
    vi.mocked(getPerformance).mockRejectedValue(new ApiError(500, 'boom'))
    renderPage()
    expect(await screen.findByText(/failed to load/i)).toBeInTheDocument()
    expect(screen.queryByText(/no performance data for this window/i)).not.toBeInTheDocument()
  })
})
