import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getKioskMyWeek, getKioskRoster, postPunch } from './client'

beforeEach(() => vi.restoreAllMocks())

describe('kiosk api', () => {
  it('sends the device token, NOT an OIDC bearer', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    )
    await getKioskRoster('dev-tok')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/kiosk/employees')
    const headers = new Headers(init!.headers)
    expect(headers.get('X-Kiosk-Token')).toBe('dev-tok')
    expect(headers.get('Authorization')).toBeNull()
  })

  it('posts a punch as multipart with the photo', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ punch_id: 1 }), { status: 201 }),
    )
    const blob = new Blob([new Uint8Array([1, 2, 3])], { type: 'image/jpeg' })
    await postPunch('dev-tok', 7, 'clock_in', blob)
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/kiosk/punch')
    expect(init!.method).toBe('POST')
    const body = init!.body as FormData
    expect(body.get('employee_id')).toBe('7')
    expect(body.get('punch_type')).toBe('clock_in')
    expect(body.get('photo')).toBeInstanceOf(Blob)
    expect(new Headers(init!.headers).get('X-Kiosk-Token')).toBe('dev-tok')
  })

  it('fetches my-week with the device token and both REQUIRED params', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ employee_id: 7, week_start: '2026-07-20', published: true, shifts: [] }),
        { status: 200 },
      ),
    )
    await getKioskMyWeek('dev-tok', 7, '2026-07-20')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/kiosk/my-week?employee_id=7&week_start=2026-07-20')
    const headers = new Headers(init!.headers)
    expect(headers.get('X-Kiosk-Token')).toBe('dev-tok')
    expect(headers.get('Authorization')).toBeNull()
  })
})
