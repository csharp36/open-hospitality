import { describe, expect, it, vi, beforeEach } from 'vitest'
import * as oidc from '../auth/oidc'
import { getPerformance, setStatConfig } from './client'
import type { PerformanceResponse } from './types'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(oidc, 'getAccessToken').mockResolvedValue('tok')
})

// A minimal but field-faithful PerformanceResponse body for the GET assertions.
const perfBody: PerformanceResponse = {
  property_id: 'HISJ',
  adr_room_basis: 'as_reported',
  period: null,
  start: '2026-01-01',
  end: '2026-01-31',
  current: {
    start: '2026-01-01',
    end: '2026-01-31',
    rooms_available: '4340',
    rooms_sold: '3100',
    adr_rooms_sold: '3100',
    room_revenue: '372000.00',
    total_revenue: '500000.00',
    occupancy: '0.7143',
    adr: '120.00',
    revpar: '85.71',
    trevpar: '115.21',
    adr_room_basis: 'as_reported',
  },
  prior_period: null,
  prior_year: null,
  prior_period_delta_pct: {},
  prior_year_delta_pct: {},
  reconciliation: {},
  trends: { anchor: '2026-01-31', wow: {}, mtd: {}, rolling_30: {}, dow: {} },
  labor: {
    labor_hours: null,
    rooms_sold: null,
    hours_per_occupied_room: null,
    labor_cost: null,
    cost_per_occupied_room: null,
    cost_suppressed: false,
  },
  days_excluded: 0,
}

describe('performance client', () => {
  it('getPerformance with {from,to} GETs /api/performance with the range params + bearer', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(perfBody), { status: 200 }),
    )
    const perf = await getPerformance('HISJ', { from: '2026-01-01', to: '2026-01-31' })
    expect(perf.property_id).toBe('HISJ')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/performance?property=HISJ&from=2026-01-01&to=2026-01-31')
    expect(new Headers(init!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('getPerformance with {period} GETs /api/performance with the period param', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ...perfBody, period: '2026-P01' }), { status: 200 }),
    )
    const perf = await getPerformance('HISJ', { period: '2026-P01' })
    expect(perf.period).toBe('2026-P01')
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/performance?property=HISJ&period=2026-P01')
  })

  it('setStatConfig PUTs /api/properties/{id}/stat-config with the {adr_room_basis} body', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ adr_room_basis: 'exclude_comp_house' }), { status: 200 }),
    )
    const result = await setStatConfig('HISJ', 'exclude_comp_house')
    expect(result.adr_room_basis).toBe('exclude_comp_house')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/stat-config')
    expect(init!.method).toBe('PUT')
    expect(JSON.parse(init!.body as string)).toEqual({ adr_room_basis: 'exclude_comp_house' })
  })
})
