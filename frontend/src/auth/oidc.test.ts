import { describe, expect, it, vi } from 'vitest'
import { login, userManager, getAccessToken } from './oidc'

describe('login()', () => {
  it('forwards an email as login_hint so the OIDC screen is prefilled', async () => {
    const spy = vi.spyOn(userManager, 'signinRedirect').mockResolvedValue(undefined as never)
    await login('owner@hotel.test')
    expect(spy).toHaveBeenCalledWith({ login_hint: 'owner@hotel.test' })
  })

  it('with no hint calls signinRedirect with no args (unchanged behavior)', async () => {
    const spy = vi.spyOn(userManager, 'signinRedirect').mockResolvedValue(undefined as never)
    await login()
    expect(spy).toHaveBeenCalledWith()
  })
})

describe('getAccessToken', () => {
  it('returns the token for a live user', async () => {
    vi.spyOn(userManager, 'getUser').mockResolvedValue({
      access_token: 'tok-123', expired: false,
    } as never)
    expect(await getAccessToken()).toBe('tok-123')
  })

  it('returns null when the user is expired', async () => {
    vi.spyOn(userManager, 'getUser').mockResolvedValue({
      access_token: 'tok-123', expired: true,
    } as never)
    expect(await getAccessToken()).toBeNull()
  })

  it('returns null with no user', async () => {
    vi.spyOn(userManager, 'getUser').mockResolvedValue(null)
    expect(await getAccessToken()).toBeNull()
  })
})
