import { describe, expect, it, vi, beforeEach } from 'vitest'
import * as oidc from '../auth/oidc'
import { getProperties } from './client'

beforeEach(() => vi.restoreAllMocks())

describe('API bearer injection', () => {
  it('attaches Authorization from the current access token', async () => {
    vi.spyOn(oidc, 'getAccessToken').mockResolvedValue('tok-xyz')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    )
    await getProperties()
    const init = fetchMock.mock.calls[0]![1] as RequestInit
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer tok-xyz')
  })

  it('redirects to login on 401', async () => {
    vi.spyOn(oidc, 'getAccessToken').mockResolvedValue('stale')
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 401 }))
    const loginSpy = vi.spyOn(oidc, 'login').mockResolvedValue()
    await expect(getProperties()).rejects.toBeTruthy()
    expect(loginSpy).toHaveBeenCalled()
  })
})
