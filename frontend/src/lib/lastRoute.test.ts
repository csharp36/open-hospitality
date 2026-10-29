import { beforeEach, describe, expect, it } from 'vitest'
import { lastRoute, rememberRoute } from './lastRoute'
import { isServedPath } from '../router'

const HOME = '/dashboard'
const SUBJECT = 'kc-user-a'

// The denylist tests are about `restorable`, not about route resolution, so
// they hand `lastRoute` a resolver that accepts everything: whatever they
// assert is then the denylist's doing and nothing else's.
const anyPath = () => true

beforeEach(() => localStorage.clear())

describe('lastRoute restore exclusions', () => {
  it('does not restore the one-shot /signup route (consumed invite token)', () => {
    rememberRoute('/signup?token=abc', SUBJECT)
    expect(lastRoute(anyPath, SUBJECT)).toBe(HOME)
  })

  it('does not restore the one-shot /callback OIDC route', () => {
    rememberRoute('/callback?code=xyz&state=1', SUBJECT)
    expect(lastRoute(anyPath, SUBJECT)).toBe(HOME)
  })

  it('does not restore the entry route itself', () => {
    rememberRoute('/', SUBJECT)
    expect(lastRoute(anyPath, SUBJECT)).toBe(HOME)
  })

  it('remembers and restores a normal route', () => {
    rememberRoute('/coverage?month=2026-08', SUBJECT)
    expect(lastRoute(anyPath, SUBJECT)).toBe('/coverage?month=2026-08')
  })

  it('returns HOME when nothing has been remembered', () => {
    expect(lastRoute(anyPath, SUBJECT)).toBe(HOME)
  })
})

describe('lastRoute restores only routes the SPA serves', () => {
  it('restores a remembered route that resolves, search params and all', () => {
    rememberRoute('/qbo?property=HISJ&month=2026-07', SUBJECT)
    expect(lastRoute(isServedPath, SUBJECT)).toBe('/qbo?property=HISJ&month=2026-07')
  })

  it('falls back to HOME for a remembered route with no route to serve it', () => {
    // Without this guard, a remembered route that stops resolving (removed,
    // renamed, or never registered) pins Not Found onto every bare-origin
    // load and every post-login return.
    rememberRoute('/not-a-real-route', SUBJECT)
    expect(isServedPath('/not-a-real-route')).toBe(false)
    expect(lastRoute(isServedPath, SUBJECT)).toBe(HOME)
  })

  it('falls back to HOME for a route that was removed after being remembered', () => {
    // What a denylist can never do: nothing had to predict this path.
    localStorage.setItem('usali.last-route', '/some-page-we-deleted?x=1')
    expect(lastRoute(isServedPath, SUBJECT)).toBe(HOME)
  })

  it('treats a trailing slash as the same route', () => {
    expect(isServedPath('/setup/')).toBe(true)
  })
})

// Issue #103: the memory belongs to a subject, not to the browser.
describe('lastRoute is per subject', () => {
  it('does not restore a route remembered under another subject — the next person gets HOME', () => {
    rememberRoute('/payroll-dashboard', SUBJECT)
    expect(lastRoute(anyPath, 'kc-user-b')).toBe(HOME)
  })

  it('restores for the same subject that remembered it', () => {
    rememberRoute('/coverage?month=2026-08', SUBJECT)
    expect(lastRoute(anyPath, SUBJECT)).toBe('/coverage?month=2026-08')
  })

  it('remembers nothing for an anonymous visit, and never overwrites a signed-in user\'s place', () => {
    rememberRoute('/coverage', SUBJECT)
    rememberRoute('/try', null)
    rememberRoute('/sos', undefined)
    expect(lastRoute(anyPath, SUBJECT)).toBe('/coverage')
  })

  it('returns HOME with no subject to restore for', () => {
    rememberRoute('/coverage', SUBJECT)
    expect(lastRoute(anyPath, null)).toBe(HOME)
    expect(lastRoute(anyPath, undefined)).toBe(HOME)
  })

  it('treats the pre-#103 bare-string format as nothing remembered', () => {
    localStorage.setItem('usali.last-route', '/payroll-dashboard')
    expect(lastRoute(anyPath, SUBJECT)).toBe(HOME)
  })

  it('survives garbage in storage', () => {
    localStorage.setItem('usali.last-route', '{not json')
    expect(lastRoute(anyPath, SUBJECT)).toBe(HOME)
    localStorage.setItem('usali.last-route', JSON.stringify({ href: 42 }))
    expect(lastRoute(anyPath, SUBJECT)).toBe(HOME)
  })
})
