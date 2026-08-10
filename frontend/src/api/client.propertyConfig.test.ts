import { describe, expect, it, vi, beforeEach } from 'vitest'
import * as oidc from '../auth/oidc'
import {
  addOoo,
  getFiscalPeriods,
  getPropertyConfig,
  removeOoo,
  setFiscalCalendar,
  setInventory,
} from './client'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(oidc, 'getAccessToken').mockResolvedValue('tok')
})

describe('property config client', () => {
  it('getPropertyConfig GETs /api/properties/{id}/config with bearer', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ property_id: 'HISJ', inventory: [], out_of_order: [], fiscal_calendar: null }),
        { status: 200 },
      ),
    )
    const cfg = await getPropertyConfig('HISJ')
    expect(cfg.property_id).toBe('HISJ')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/config')
    expect(new Headers(init!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('setInventory POSTs the body as JSON', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ inventory_id: 1, effective_date: '2026-01-01', total_rooms: 140 }),
        { status: 201 },
      ),
    )
    const row = await setInventory('HISJ', { effective_date: '2026-01-01', total_rooms: 140 })
    expect(row.total_rooms).toBe(140)
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/inventory')
    expect(init!.method).toBe('POST')
    expect(JSON.parse(init!.body as string)).toEqual({ effective_date: '2026-01-01', total_rooms: 140 })
  })

  it('addOoo POSTs the body as JSON', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          ooo_id: 1, start_date: '2026-02-01', end_date: '2026-02-07',
          room_count: 3, reason_code: 'renovation', note: null,
        }),
        { status: 201 },
      ),
    )
    const row = await addOoo('HISJ', {
      start_date: '2026-02-01', end_date: '2026-02-07',
      room_count: 3, reason_code: 'renovation', note: null,
    })
    expect(row.reason_code).toBe('renovation')
    const init = fetchMock.mock.calls[0]![1]!
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string).reason_code).toBe('renovation')
  })

  it('removeOoo DELETEs the block', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }))
    await removeOoo('HISJ', 7)
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/out-of-order/7')
    expect(init!.method).toBe('DELETE')
  })

  it('setFiscalCalendar PUTs the body as JSON', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ calendar_type: '445', fiscal_year_start_month: 1, week_start_weekday: 6 }),
        { status: 200 },
      ),
    )
    const cfg = await setFiscalCalendar('HISJ', {
      calendar_type: '445', fiscal_year_start_month: 1, week_start_weekday: 6,
    })
    expect(cfg.calendar_type).toBe('445')
    const init = fetchMock.mock.calls[0]![1]!
    expect(init.method).toBe('PUT')
  })

  it('getFiscalPeriods GETs with the fiscal_year query param', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ periods: [{ key: '2026-P01', start: '2026-01-01', end: '2026-01-31' }] }), {
        status: 200,
      }),
    )
    const result = await getFiscalPeriods('HISJ', 2026)
    expect(result.periods).toHaveLength(1)
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/properties/HISJ/fiscal-periods?fiscal_year=2026')
  })
})
