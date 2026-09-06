// Typed fetch wrappers over the general-ledger router (src/usali/gl_api.py).
// Its own module rather than more weight on `client.ts`, the same call the
// backend made for the router (and `checklist.ts` before it).
//
// These are authenticated operator endpoints, so they go through `client.ts`'s
// header seam and its `redirectToLogin` rather than re-implementing either —
// the `redirecting` latch that fires login exactly once lives in that module,
// and a second copy of it would redirect once per in-flight 401.

import { authHeaders, raiseApiError, redirectToLogin } from './client'
import type { GlCloseResponse, GlPeriod, GlPostOutcome, JournalEntries, TrialBalance } from './types'

// The GETs are spelled out rather than routed through client.ts's private
// `getJson` for checklist.ts's reason: exporting a helper to save a few lines
// would leave the reads and the writes in different shapes.

export async function getGlPeriods(property: string, fiscalYear?: number): Promise<GlPeriod[]> {
  // fiscal_year is omitted entirely when not passed — the backend defaults it,
  // and `?fiscal_year=undefined` would fail its int parse.
  const year = fiscalYear === undefined ? '' : `&fiscal_year=${fiscalYear}`
  const res = await fetch(`/api/gl/periods?property=${property}${year}`, {
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<GlPeriod[]>
}

export async function getTrialBalance(property: string, period: string): Promise<TrialBalance> {
  const res = await fetch(`/api/gl/trial-balance?property=${property}&period=${period}`, {
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<TrialBalance>
}

export async function getJournalEntries(
  property: string,
  period: string,
  account: string,
): Promise<JournalEntries> {
  const res = await fetch(
    `/api/gl/entries?property=${property}&period=${period}&account=${account}`,
    { headers: await authHeaders() },
  )
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<JournalEntries>
}

export async function closeGlPeriod(property: string, periodKey: string): Promise<GlCloseResponse> {
  const res = await fetch(`/api/gl/periods/${periodKey}/close?property=${property}`, {
    method: 'PUT',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<GlCloseResponse>
}

// Reopen answers 204 — res.json() on an empty body rejects, so it must not
// copy the trailing parse the close above carries.

export async function reopenGlPeriod(
  property: string,
  periodKey: string,
  reason: string,
): Promise<void> {
  const res = await fetch(`/api/gl/periods/${periodKey}/reopen?property=${property}`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ reason }),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
}

/** Post (or re-post) every source for each date in the range. Per-grain
 *  trouble is decided by `gl_posting.post_and_record` and comes back as an
 *  outcome status (`skipped`/`failed`), so a resolved promise is not success —
 *  callers must branch on `GlPostOutcome.status`. */
export async function postGlRange(body: {
  property_id: string
  date_from: string
  date_to: string
}): Promise<GlPostOutcome[]> {
  const res = await fetch('/api/gl/post', {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<GlPostOutcome[]>
}
