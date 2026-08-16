import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, postPreview } from './client'

beforeEach(() => vi.restoreAllMocks())

describe('postPreview (public front door)', () => {
  it('posts the raw PDF body with no Authorization header', async () => {
    const payload = {
      pms_source: 'opera',
      report_type: 'sos',
      business_date: '2026-08-14',
      pnl_lines: [],
      kpis: [],
      codes_recognized: 10,
      codes_mapped: 9,
      codes_needs_review: 1,
      net_total: '1000.00',
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok', payload }), { status: 200 }),
    )
    const file = new File(['%PDF-1.4 fake'], 'a.pdf', { type: 'application/pdf' })
    const result = await postPreview(file)

    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/preview')
    expect(init!.method).toBe('POST')
    expect(init!.body).toBe(file)
    const headers = new Headers(init!.headers)
    expect(headers.get('Content-Type')).toBe('application/pdf')
    expect(headers.get('Authorization')).toBeNull()

    expect(result).toEqual({ status: 'ok', payload })
  })

  it('throws ApiError on a 413 without navigating/redirecting to login', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'file too large' }), { status: 413 }),
    )
    const file = new File(['%PDF-1.4 fake'], 'a.pdf', { type: 'application/pdf' })

    await expect(postPreview(file)).rejects.toBeInstanceOf(ApiError)
  })
})
