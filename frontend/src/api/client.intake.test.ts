import { describe, expect, it, vi, beforeEach } from 'vitest'
import * as oidc from '../auth/oidc'
import {
  createIntakeAddress,
  getIntakeAddress,
  getIntakeEvents,
  rotateIntakeAddress,
  setIntakeSenderDomains,
} from './client'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(oidc, 'getAccessToken').mockResolvedValue('tok')
})

const ADDRESS = {
  address: 'na-abc@intake.example.test',
  local_part: 'na-abc',
  created_at: '2026-07-07T04:00:00Z',
  sender_domains: ['pms.test'],
}

describe('night-audit intake client', () => {
  it('getIntakeAddress GETs /api/properties/{id}/intake-address with bearer', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(ADDRESS), { status: 200 }),
    )
    const addr = await getIntakeAddress('HISJ')
    expect(addr.address).toBe('na-abc@intake.example.test')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/intake-address')
    expect(new Headers(init!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('getIntakeAddress carries the all-null no-address shape through', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ address: null, local_part: null, created_at: null, sender_domains: null }),
        { status: 200 },
      ),
    )
    const addr = await getIntakeAddress('HISJ')
    expect(addr.address).toBeNull()
    expect(addr.sender_domains).toBeNull()
  })

  it('createIntakeAddress POSTs the address route with no body', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(ADDRESS), { status: 201 }),
    )
    const addr = await createIntakeAddress('HISJ')
    expect(addr.local_part).toBe('na-abc')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/intake-address')
    expect(init!.method).toBe('POST')
    expect(init!.body).toBeUndefined()
    expect(new Headers(init!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('rotateIntakeAddress POSTs the rotate route', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ ...ADDRESS, address: 'na-new@intake.example.test', local_part: 'na-new', sender_domains: null }),
        { status: 201 },
      ),
    )
    const addr = await rotateIntakeAddress('HISJ')
    expect(addr.local_part).toBe('na-new')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/intake-address/rotate')
    expect(init!.method).toBe('POST')
    expect(new Headers(init!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('setIntakeSenderDomains PUTs {sender_domains} as JSON', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(ADDRESS), { status: 200 }),
    )
    const addr = await setIntakeSenderDomains('HISJ', ['pms.test'])
    expect(addr.sender_domains).toEqual(['pms.test'])
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/intake-address')
    expect(init!.method).toBe('PUT')
    expect(JSON.parse(init!.body as string)).toEqual({ sender_domains: ['pms.test'] })
    expect(new Headers(init!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('setIntakeSenderDomains sends an empty list as [], the "any sender" spelling', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ...ADDRESS, sender_domains: null }), { status: 200 }),
    )
    await setIntakeSenderDomains('HISJ', [])
    const init = fetchMock.mock.calls[0]![1]!
    expect(JSON.parse(init.body as string)).toEqual({ sender_domains: [] })
  })

  it('getIntakeEvents GETs the event log with the limit query param', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          events: [{
            event_id: 1,
            received_at: '2026-07-07T04:00:00Z',
            envelope_from: 'a@b.test',
            subject: 'Night audit',
            outcome: 'ingested',
            message_id: '<one@pms.test>',
            attachments: [
              { name: 'f.pdf', sha256: 'aa', bytes: 3, outcome: 'ingested', batch_id: 7 },
            ],
          }],
        }),
        { status: 200 },
      ),
    )
    const result = await getIntakeEvents('HISJ', 20)
    expect(result.events).toHaveLength(1)
    expect(result.events[0]!.attachments[0]!.batch_id).toBe(7)
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/properties/HISJ/intake-events?limit=20')
    expect(new Headers(init!.headers).get('Authorization')).toBe('Bearer tok')
  })
})
