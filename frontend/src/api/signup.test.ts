import { beforeEach, describe, expect, it, vi } from 'vitest'

import { completeSignup, getInvite, requestOtp, SignupError } from './signup'

function mockFetch(status: number, body: unknown = null) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(body === null ? null : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
}

beforeEach(() => vi.restoreAllMocks())

describe('signup API (public — no auth header)', () => {
  it('getInvite returns the email on 200 and sends no Authorization', async () => {
    const f = mockFetch(200, { email: 'owner@hotel.test' })
    const email = await getInvite('tok-123')
    expect(email).toBe('owner@hotel.test')
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/signup/invite/tok-123')
    expect(new Headers((init as RequestInit).headers).get('Authorization')).toBeNull()
  })

  it('getInvite throws SignupError with status 404 on a missing invite', async () => {
    mockFetch(404, { detail: 'not found' })
    await expect(getInvite('nope')).rejects.toMatchObject({ status: 404 })
  })

  it('requestOtp POSTs token+cell and resolves on 204', async () => {
    const f = mockFetch(204)
    await requestOtp('tok-123', '+15550000000')
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/signup/otp')
    expect((init as RequestInit).method).toBe('POST')
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      token: 'tok-123', cell: '+15550000000',
    })
    expect(new Headers((init as RequestInit).headers).get('Authorization')).toBeNull()
  })

  it('requestOtp throws SignupError{status:429} when rate-limited', async () => {
    mockFetch(429, { detail: 'too many requests' })
    await expect(requestOtp('t', '+15550000000')).rejects.toMatchObject({ status: 429 })
  })

  it('completeSignup returns {org_alias, pms_supported} on 201', async () => {
    const f = mockFetch(201, { org_alias: 'sky-group', pms_supported: true })
    const res = await completeSignup({
      token: 't', otp: '123456', workspace_name: 'Sky', workspace_alias: 'sky-group',
      property_name: 'Sky Hotel', pms_source: 'opera', wage_jurisdiction: 'US-CA',
      timezone: 'America/New_York', cell: '+15550000000', password: 'passw0rd',
    })
    expect(res).toEqual({ org_alias: 'sky-group', pms_supported: true })
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/signup/complete')
    expect((init as RequestInit).method).toBe('POST')
    expect(new Headers((init as RequestInit).headers).get('Authorization')).toBeNull()
  })

  it('completeSignup throws SignupError{status:403} on a wrong OTP', async () => {
    mockFetch(403, { detail: 'verification failed' })
    await expect(
      completeSignup({
        token: 't', otp: '000000', workspace_name: 'W', workspace_alias: 'w-x',
        property_name: 'P', pms_source: 'opera', wage_jurisdiction: 'US-CA',
        cell: '+15550000000', password: 'passw0rd',
      }),
    ).rejects.toMatchObject({ status: 403 })
  })

  it('SignupError exposes the status for the UI to branch on', () => {
    expect(new SignupError(404).status).toBe(404)
  })
})
