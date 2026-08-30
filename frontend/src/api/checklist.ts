// Typed fetch wrappers over the onboarding checklist router
// (src/usali/checklist_api.py). Its own module rather than more weight on
// `client.ts`, the same call the backend made for the router.
//
// These are authenticated operator endpoints, so they go through `client.ts`'s
// header seam and its `redirectToLogin` rather than re-implementing either —
// the `redirecting` latch that fires login exactly once lives in that module,
// and a second copy of it would redirect once per in-flight 401.
// (Contrast `signup.ts`, which is public and bypasses the seam deliberately.)

import { authHeaders, raiseApiError, redirectToLogin } from './client'
import type { Checklist } from './types'

// Spelled out rather than over client.ts's `getJson`: this module has exactly
// one read, and exporting a fourth helper to save five lines would leave the
// GET and the two writes in different shapes.
export async function getChecklist(): Promise<Checklist> {
  const res = await fetch('/api/checklist', { headers: await authHeaders() })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<Checklist>
}

// Both endpoints answer 204 — res.json() on an empty body rejects, so these
// must not copy the trailing parse the writes in client.ts all carry.
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
