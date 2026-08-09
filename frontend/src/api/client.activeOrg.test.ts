import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as oidc from '../auth/oidc'
import { setActiveOrg } from '../auth/activeOrg'
import { getProperties } from './client'

function makeToken(orgs: string[]): string {
  const b64 = btoa(JSON.stringify({ organization: orgs, sub: 'u' }))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  return `header.${b64}.sig`
}

function activeOrgHeader(fetchMock: ReturnType<typeof vi.spyOn>): string | null {
  const init = fetchMock.mock.calls[0]![1] as RequestInit
  return new Headers(init.headers).get('X-Active-Org')
}

beforeEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
})

describe('X-Active-Org injection at the header seam', () => {
  it('a single-org user carries their one alias automatically (no picker needed)', async () => {
    vi.spyOn(oidc, 'getAccessToken').mockResolvedValue(makeToken(['solo-org']))
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('[]', { status: 200 }))
    await getProperties()
    expect(activeOrgHeader(fetchMock)).toBe('solo-org')
  })

  it('a multi-org user with NO selection sends no header (server 400s ambiguity)', async () => {
    vi.spyOn(oidc, 'getAccessToken').mockResolvedValue(makeToken(['multi-a', 'multi-b']))
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('[]', { status: 200 }))
    await getProperties()
    expect(activeOrgHeader(fetchMock)).toBeNull()
  })

  it('a multi-org user carries their chosen alias once selected', async () => {
    vi.spyOn(oidc, 'getAccessToken').mockResolvedValue(makeToken(['multi-a', 'multi-b']))
    setActiveOrg('multi-b')
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('[]', { status: 200 }))
    await getProperties()
    expect(activeOrgHeader(fetchMock)).toBe('multi-b')
  })

  it('a pick survives a reload — the persisted alias still rides the request', async () => {
    // Persist a pick (setActiveOrg writes sessionStorage), then drop the module
    // registry to simulate a reload. A fresh client, reading the fresh
    // activeOrg module, must still send the persisted alias.
    setActiveOrg('multi-b')
    vi.resetModules()
    const freshOidc = await import('../auth/oidc')
    const { getProperties: freshGetProperties } = await import('./client')
    vi.spyOn(freshOidc, 'getAccessToken').mockResolvedValue(makeToken(['multi-a', 'multi-b']))
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('[]', { status: 200 }))
    await freshGetProperties()
    expect(activeOrgHeader(fetchMock)).toBe('multi-b')
  })

  it('no token → no active-org header (and no bearer)', async () => {
    vi.spyOn(oidc, 'getAccessToken').mockResolvedValue(null)
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('[]', { status: 200 }))
    await getProperties()
    expect(activeOrgHeader(fetchMock)).toBeNull()
    const init = fetchMock.mock.calls[0]![1] as RequestInit
    expect(new Headers(init.headers).get('Authorization')).toBeNull()
  })
})
