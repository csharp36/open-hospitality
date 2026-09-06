import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getAccessToken } from '../auth/oidc'
import { makeGlPeriod, makeJournalEntry, makeTrialBalance } from '../test/fixtures'
import {
  closeGlPeriod,
  getGlPeriods,
  getJournalEntries,
  getTrialBalance,
  postGlRange,
  reopenGlPeriod,
} from './gl'

vi.mock('../auth/oidc', () => ({ getAccessToken: vi.fn(), login: vi.fn() }))

function mockFetch(status: number, body: unknown = null) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(body === null ? null : JSON.stringify(body), {
      status,
      headers: body === null ? undefined : { 'Content-Type': 'application/json' },
    }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(getAccessToken).mockResolvedValue('tok')
})

describe('gl API', () => {
  it('getGlPeriods GETs /api/gl/periods for the property', async () => {
    const f = mockFetch(200, [makeGlPeriod()])
    const out = await getGlPeriods('HISJ')
    expect(out[0]!.period_key).toBe('2026-P07')
    expect(f.mock.calls[0]![0]).toBe('/api/gl/periods?property=HISJ')
    expect(new Headers(f.mock.calls[0]![1]!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('getGlPeriods appends fiscal_year only when one is passed', async () => {
    const f = mockFetch(200, [])
    await getGlPeriods('HISJ', 2026)
    expect(f.mock.calls[0]![0]).toBe('/api/gl/periods?property=HISJ&fiscal_year=2026')
  })

  it('getTrialBalance GETs /api/gl/trial-balance and returns the parsed body', async () => {
    const f = mockFetch(200, makeTrialBalance())
    const out = await getTrialBalance('HISJ', '2026-P07')
    expect(out.total_debits).toBe(out.total_credits)
    expect(out.lines.length).toBeGreaterThan(0)
    expect(f.mock.calls[0]![0]).toBe('/api/gl/trial-balance?property=HISJ&period=2026-P07')
  })

  it('getJournalEntries GETs /api/gl/entries with property, period, and account', async () => {
    const f = mockFetch(200, {
      property_id: 'HISJ',
      period_key: '2026-P07',
      account_code: '4100',
      entries: [makeJournalEntry()],
    })
    const out = await getJournalEntries('HISJ', '2026-P07', '4100')
    expect(out.entries[0]!.lines.length).toBe(2)
    expect(f.mock.calls[0]![0]).toBe('/api/gl/entries?property=HISJ&period=2026-P07&account=4100')
  })

  it('closeGlPeriod PUTs the close and returns the CloseResponse body', async () => {
    const f = mockFetch(200, {
      period_key: '2026-P07',
      state: 'closed',
      unposted_dates: ['2026-07-03'],
      orphaned_dates: [],
    })
    const out = await closeGlPeriod('HISJ', '2026-P07')
    expect(out.state).toBe('closed')
    expect(out.unposted_dates).toEqual(['2026-07-03'])
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/gl/periods/2026-P07/close?property=HISJ')
    expect((init as RequestInit).method).toBe('PUT')
    expect(new Headers((init as RequestInit).headers).get('Authorization')).toBe('Bearer tok')
  })

  it('reopenGlPeriod PUTs the reason and tolerates a 204 with no body', async () => {
    const f = mockFetch(204)
    await expect(reopenGlPeriod('HISJ', '2026-P07', 'because')).resolves.toBeUndefined()
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/gl/periods/2026-P07/reopen?property=HISJ')
    expect((init as RequestInit).method).toBe('PUT')
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ reason: 'because' })
  })

  it('postGlRange POSTs /api/gl/post and returns the outcomes array', async () => {
    const outcomes = [
      {
        business_date: '2026-07-01',
        source_type: 'pms_daily',
        status: 'posted',
        entry_id: 7,
        message: null,
      },
      {
        business_date: '2026-07-01',
        source_type: 'payroll_accrual',
        status: 'skipped',
        entry_id: null,
        message: 'no labor chart',
      },
    ]
    const f = mockFetch(200, outcomes)
    const body = { property_id: 'HISJ', date_from: '2026-07-01', date_to: '2026-07-01' }
    const out = await postGlRange(body)
    expect(out).toEqual(outcomes)
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/gl/post')
    expect((init as RequestInit).method).toBe('POST')
    expect(JSON.parse((init as RequestInit).body as string)).toEqual(body)
  })

  it('surfaces the 422 detail when a close is refused', async () => {
    mockFetch(422, { detail: 'unknown period key 2026-P99' })
    await expect(closeGlPeriod('HISJ', '2026-P99')).rejects.toThrow('unknown period key 2026-P99')
  })
})
