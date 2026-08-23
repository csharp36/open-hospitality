import { beforeEach, describe, expect, it } from 'vitest'
import { lastRoute, rememberRoute } from './lastRoute'

const HOME = '/dashboard'

beforeEach(() => localStorage.clear())

describe('lastRoute restore exclusions', () => {
  it('does not restore the one-shot /signup route (consumed invite token)', () => {
    rememberRoute('/signup?token=abc')
    expect(lastRoute()).toBe(HOME)
  })

  it('does not restore the one-shot /callback OIDC route', () => {
    rememberRoute('/callback?code=xyz&state=1')
    expect(lastRoute()).toBe(HOME)
  })

  it('does not restore the entry route itself', () => {
    rememberRoute('/')
    expect(lastRoute()).toBe(HOME)
  })

  it('remembers and restores a normal route', () => {
    rememberRoute('/coverage?month=2026-08')
    expect(lastRoute()).toBe('/coverage?month=2026-08')
  })

  it('returns HOME when nothing has been remembered', () => {
    expect(lastRoute()).toBe(HOME)
  })
})
