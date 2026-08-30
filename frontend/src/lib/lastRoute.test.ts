import { beforeEach, describe, expect, it } from 'vitest'
import { lastRoute, rememberRoute } from './lastRoute'
import { isServedPath } from '../router'

const HOME = '/dashboard'

// The denylist tests are about `restorable`, not about route resolution, so
// they hand `lastRoute` a resolver that accepts everything: whatever they
// assert is then the denylist's doing and nothing else's.
const anyPath = () => true

beforeEach(() => localStorage.clear())

describe('lastRoute restore exclusions', () => {
  it('does not restore the one-shot /signup route (consumed invite token)', () => {
    rememberRoute('/signup?token=abc')
    expect(lastRoute(anyPath)).toBe(HOME)
  })

  it('does not restore the one-shot /callback OIDC route', () => {
    rememberRoute('/callback?code=xyz&state=1')
    expect(lastRoute(anyPath)).toBe(HOME)
  })

  it('does not restore the entry route itself', () => {
    rememberRoute('/')
    expect(lastRoute(anyPath)).toBe(HOME)
  })

  it('remembers and restores a normal route', () => {
    rememberRoute('/coverage?month=2026-08')
    expect(lastRoute(anyPath)).toBe('/coverage?month=2026-08')
  })

  it('returns HOME when nothing has been remembered', () => {
    expect(lastRoute(anyPath)).toBe(HOME)
  })
})

describe('lastRoute restores only routes the SPA serves', () => {
  it('restores a remembered route that resolves, search params and all', () => {
    rememberRoute('/qbo?property=HISJ&month=2026-07')
    expect(lastRoute(isServedPath)).toBe('/qbo?property=HISJ&month=2026-07')
  })

  it('falls back to HOME for a remembered route with no route to serve it', () => {
    // The live case, not a hypothetical: the checklist's three integration
    // items link to /integrations, whose page is a later plan. Without this
    // guard one click from /setup pins Not Found onto every bare-origin load
    // and every post-login return.
    rememberRoute('/integrations')
    expect(isServedPath('/integrations')).toBe(false)
    expect(lastRoute(isServedPath)).toBe(HOME)
  })

  it('falls back to HOME for a route that was removed after being remembered', () => {
    // What a denylist can never do: nothing had to predict this path.
    localStorage.setItem('usali.last-route', '/some-page-we-deleted?x=1')
    expect(lastRoute(isServedPath)).toBe(HOME)
  })

  it('treats a trailing slash as the same route', () => {
    expect(isServedPath('/setup/')).toBe(true)
  })
})
