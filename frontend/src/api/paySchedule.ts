// Typed client for a property's pay schedule (payroll_admin-only on the
// server, audited on write). "Not set yet" is a 404 from the read, which this
// module turns into `null` so the card can render a first-run form instead
// of an error.
import { ApiError, authHeaders, raiseApiError } from './client'
import type { PaySchedule, PayScheduleBody } from './types'

export async function getPaySchedule(propertyId: string): Promise<PaySchedule | null> {
  const res = await fetch(`/api/payroll/properties/${propertyId}/pay-schedule`, {
    headers: await authHeaders(),
  })
  if (res.status === 404) return null
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<PaySchedule>
}

/** Set (get-or-create) the schedule. A misaligned anchor is a 422 whose
 * detail names the platform anchor, e.g. "… (anchored at 2026-01-05, biweekly)". */
export async function putPaySchedule(
  propertyId: string,
  body: PayScheduleBody,
): Promise<PaySchedule> {
  const res = await fetch(`/api/payroll/properties/${propertyId}/pay-schedule`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<PaySchedule>
}

/** The platform anchor the server names in its misalignment refusal, if it did. */
export function platformAnchorFromError(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null
  const m = /anchored at (\d{4}-\d{2}-\d{2})/.exec(err.detail)
  return m ? m[1]! : null
}
