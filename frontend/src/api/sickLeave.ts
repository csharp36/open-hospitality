// Typed client for the sick-leave surface (E4): one balance read and three
// audited writes, all under /api/payroll and all payroll_admin-only on the
// server. Hours travel as DECIMAL STRINGS, never JS numbers — the server folds
// a whole timeline of entries, and a float that drifted a hundredth would be
// recorded as a real hour someone did or did not take.
import { authHeaders, raiseApiError } from './client'
import type { SickLeaveStatus } from './types'

export interface SickLeaveUsageBody {
  hours: string
  on: string
}

export interface SickLeaveAdjustmentBody extends SickLeaveUsageBody {
  note?: string
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<T>
}

export async function getSickLeave(employeeId: number): Promise<SickLeaveStatus> {
  const res = await fetch(`/api/payroll/employees/${employeeId}/sick-leave`, {
    headers: await authHeaders(),
  })
  return readJson(res)
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  return readJson(res)
}

/** Record sick hours taken on a date. The server refuses, by name, a day with
 * no primary placement, an overdraw anywhere on the timeline, the
 * calendar-year cap, and an identical duplicate (409). */
export function recordSickLeaveUsage(
  employeeId: number,
  body: SickLeaveUsageBody,
): Promise<SickLeaveStatus> {
  return post(`/api/payroll/employees/${employeeId}/sick-leave/usage`, body)
}

/** Reverse a mistaken usage: restores the balance AND the cap headroom it
 * consumed — which a plain adjustment would not. */
export function voidSickLeaveUsage(
  employeeId: number,
  body: SickLeaveUsageBody,
): Promise<SickLeaveStatus> {
  return post(`/api/payroll/employees/${employeeId}/sick-leave/usage-voids`, body)
}

/** The audited human path for balance corrections and opening balances.
 * Signed hours; the note is kept on the entry and never echoed into audit. */
export function adjustSickLeave(
  employeeId: number,
  body: SickLeaveAdjustmentBody,
): Promise<SickLeaveStatus> {
  return post(`/api/payroll/employees/${employeeId}/sick-leave/adjustments`, body)
}
