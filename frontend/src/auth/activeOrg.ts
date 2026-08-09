// Active-org mechanics (Pillar L decision 6). The MINIMAL SPA surface for
// multi-tenancy: read the `organization` claim (an array of KC org aliases) off
// the access token, remember which one is active, and let client.ts inject it
// as `X-Active-Org`. Single-org users see nothing new — the sole org is active
// automatically and no picker renders.
//
// This is a plain module store, NOT React state, because the header seam
// (client.ts `authHeaders()`) is a plain async function outside the component
// tree. The picker (a component) reads/writes the same store through
// `useSyncExternalStore`, so the selection and the injected header are ONE
// source of truth.
//
// The selection is persisted to sessionStorage (tab-scoped, cleared on tab
// close — the org context should not outlive the browsing session), so a
// RELOAD keeps the pick instead of resetting to null and firing header-less,
// 400-ing queries. sessionStorage is the source of truth; there is no cached
// module variable to fall out of sync with it.

const STORAGE_KEY = 'usali.activeOrg'
const listeners = new Set<() => void>()

function readSelected(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY)
  } catch {
    // Storage unavailable (private mode / SSR): fail closed to "no selection".
    return null
  }
}

/**
 * Decode the `organization` claim from a JWT access token: the same shape the
 * server pins (a JSON array of alias strings — auth.py `_principal_from_claims`
 * / test_oidc_realm_contract). Any malformed/absent claim yields [] — the SPA
 * refuses to guess an org exactly as the server does; a bad token simply shows
 * no picker and sends no header.
 */
export function orgAliasesFromToken(token: string | null): string[] {
  if (!token) return []
  const parts = token.split('.')
  if (parts.length < 2) return []
  try {
    // base64url -> JSON. atob wants standard base64; restore padding/chars.
    const b64 = parts[1]!.replace(/-/g, '+').replace(/_/g, '/')
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
    const claims: unknown = JSON.parse(decodeURIComponent(escape(atob(padded))))
    if (!claims || typeof claims !== 'object') return []
    const orgs = (claims as { organization?: unknown }).organization
    if (!Array.isArray(orgs)) return []
    return orgs.filter((a): a is string => typeof a === 'string' && a.length > 0)
  } catch {
    return []
  }
}

/**
 * The effective active alias for `orgs`: the explicit selection if it is still
 * one of the token's orgs; otherwise the sole org (the single-org default, no
 * UI); otherwise null (ambiguous — the picker must choose before any request
 * carries the header). Returning the sole org WITHOUT a prior selection is what
 * makes single-org UX unchanged.
 */
export function resolveActiveOrg(orgs: string[]): string | null {
  const selected = readSelected()
  if (selected !== null && orgs.includes(selected)) return selected
  if (orgs.length === 1) return orgs[0]!
  return null
}

export function getSelectedOrg(): string | null {
  return readSelected()
}

export function setActiveOrg(alias: string): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, alias)
  } catch {
    // Best effort: without storage the pick lives only until reload, which is
    // no worse than the pre-persistence behavior.
  }
  listeners.forEach((l) => l())
}

/** Subscribe to selection changes (for useSyncExternalStore in the picker). */
export function subscribeActiveOrg(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
