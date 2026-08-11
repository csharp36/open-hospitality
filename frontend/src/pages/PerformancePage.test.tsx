import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
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
import { getMe, getPerformance, getProperties } from '../api/client'

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
})
