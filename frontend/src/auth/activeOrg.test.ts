import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getSelectedOrg,
  orgAliasesFromToken,
  resolveActiveOrg,
  setActiveOrg,
} from './activeOrg'

// The selection now persists to sessionStorage, so reset it between tests to
// keep each pin independent (test hygiene for the module store).
beforeEach(() => sessionStorage.clear())

// A fake JWT: header.payload.signature, payload base64url-encoded. Only the
// payload matters to the decoder (signature is never verified client-side).
function makeToken(claims: Record<string, unknown>): string {
  const b64 = btoa(JSON.stringify(claims))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  return `header.${b64}.sig`
}

describe('orgAliasesFromToken', () => {
  it('reads the organization claim as an array of aliases', () => {
    const tok = makeToken({ organization: ['org-a', 'org-b'], sub: 'u' })
    expect(orgAliasesFromToken(tok)).toEqual(['org-a', 'org-b'])
  })

  it('returns [] for a null token, a claim-less token, or a malformed claim', () => {
    expect(orgAliasesFromToken(null)).toEqual([])
    expect(orgAliasesFromToken(makeToken({ sub: 'u' }))).toEqual([])
    // The server refuses a dict-shaped claim (_malformed); the SPA declines to
    // guess too — no crash, just no orgs.
    expect(orgAliasesFromToken(makeToken({ organization: { id: 1 } }))).toEqual([])
    expect(orgAliasesFromToken('not-a-jwt')).toEqual([])
  })
})

describe('resolveActiveOrg', () => {
  it('defaults to the sole org with no selection (single-org UX unchanged)', () => {
    expect(resolveActiveOrg(['only-one'])).toBe('only-one')
  })

  it('is null when multiple orgs and nothing chosen (ambiguity, no guess)', () => {
    expect(resolveActiveOrg(['amb-x', 'amb-y'])).toBeNull()
  })

  it('honors an explicit selection, but only while it is still a member', () => {
    setActiveOrg('pick-b')
    expect(resolveActiveOrg(['pick-a', 'pick-b'])).toBe('pick-b')
    // A selection the token no longer lists is inert — falls back, never sticks.
    expect(resolveActiveOrg(['pick-a', 'pick-c'])).toBeNull()
    expect(resolveActiveOrg(['pick-a'])).toBe('pick-a')
  })
})

describe('persistence across reload', () => {
  it('a pick survives a module reset (reload) via sessionStorage', async () => {
    setActiveOrg('pick-b')
    expect(getSelectedOrg()).toBe('pick-b')
    // Simulate a browser reload: drop the module registry and re-import. The
    // fresh module has no in-memory state; it must read the pick back from
    // sessionStorage (which survives a reload within the tab).
    vi.resetModules()
    const fresh = await import('./activeOrg')
    expect(fresh.getSelectedOrg()).toBe('pick-b')
    expect(fresh.resolveActiveOrg(['pick-a', 'pick-b'])).toBe('pick-b')
  })
})
