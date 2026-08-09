// Week-grid date helpers. The payroll/schedule grid is Monday-anchored
// (payroll_period_anchor 2026-01-05 is a Monday), so EVERY Monday is on the
// grid — the server remains authoritative and 422s anything off-grid.
// All arithmetic runs on UTC calendar dates so an ISO date never slips a
// timezone at local midnight.

const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/**
 * The Monday of the UPCOMING week: this week's Monday (Mon-first) + 7 days.
 * Deliberately always the NEXT week — even when `today` IS a Monday — because
 * the kiosk answers "when do I work next week?", mirroring the builder which
 * plans the week ahead.
 */
export function upcomingWeekMonday(today: Date): string {
  const d = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()))
  const monFirst = (d.getUTCDay() + 6) % 7 // Mon=0 … Sun=6
  d.setUTCDate(d.getUTCDate() - monFirst + 7)
  return d.toISOString().slice(0, 10)
}

/** ISO date + n days, in UTC so the ISO date never slips a timezone. */
export function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + n)
  return d.toISOString().slice(0, 10)
}

/** Mon-first day name for an ISO date, in UTC like addDays. */
export function dayName(iso: string): string {
  return DAY_NAMES[(new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7] ?? ''
}
