import type { Me } from '../api/types'

/**
 * True when the current principal holds ANY of `roles`. Central helper so the
 * nav gate (Layout) and the payroll sub-form gate (EmployeesPage) agree on how
 * roles are read from the `/api/me` response — a missing/loading `me` is never
 * privileged.
 */
export function hasRole(me: Me | undefined, ...roles: string[]): boolean {
  const held = me?.roles ?? []
  return roles.some((r) => held.includes(r))
}
