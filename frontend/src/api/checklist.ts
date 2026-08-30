// Typed fetch wrappers over the onboarding checklist router
// (src/usali/checklist_api.py). Its own module rather than more weight on
// `client.ts`, the same call the backend made for the router.
//
// These are authenticated operator endpoints, so they go through `client.ts`'s
// header seam and its 401 handling rather than repeating either — a second copy
// of the redirect dance is how one of them stops redirecting on session expiry.
// (Contrast `signup.ts`, which is public and bypasses the seam deliberately.)

import { authHeaders, raiseApiError, redirectToLogin } from './client'
import type { Checklist } from './types'

export async function getChecklist(): Promise<Checklist> {
  const res = await fetch('/api/checklist', { headers: await authHeaders() })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<Checklist>
}

// Both writes are 204s: never touch res.json(), there is no body to parse.
// The endpoint accepts an optional `{note}`; sending one is deferred.

export async function dismissItem(key: string): Promise<void> {
  const res = await fetch(`/api/checklist/${key}/dismissal`, {
    method: 'PUT',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
}

export async function restoreItem(key: string): Promise<void> {
  const res = await fetch(`/api/checklist/${key}/dismissal`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
}
