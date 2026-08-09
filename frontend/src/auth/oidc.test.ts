import { describe, expect, it, vi } from 'vitest'
import { userManager, getAccessToken } from './oidc'

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
