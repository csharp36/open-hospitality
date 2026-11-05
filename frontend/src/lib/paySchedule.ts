// Pure date math for a biweekly pay schedule. Everything works on ISO
// "YYYY-MM-DD" strings through UTC day numbers, so a DST change or the
// browser's zone can never shift a period boundary by a day.

const DAY_MS = 86_400_000
export const PERIOD_DAYS = 14

function toDayNumber(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number) as [number, number, number]
  return Math.floor(Date.UTC(y, m - 1, d) / DAY_MS)
}

function fromDayNumber(n: number): string {
  return new Date(n * DAY_MS).toISOString().slice(0, 10)
}

/** Add whole days to an ISO date. */
export function addDays(iso: string, days: number): string {
  return fromDayNumber(toDayNumber(iso) + days)
}

/** True when `candidate` is a whole number of periods from `anchor`. */
export function isAligned(anchor: string, candidate: string): boolean {
  const diff = toDayNumber(candidate) - toDayNumber(anchor)
  return ((diff % PERIOD_DAYS) + PERIOD_DAYS) % PERIOD_DAYS === 0
}

/** The two grid dates nearest to `chosen` (the one at or before, the one
 * after) — what to offer when the server refuses a misaligned anchor. */
export function nearestAligned(anchor: string, chosen: string): [string, string] {
  const a = toDayNumber(anchor)
  const c = toDayNumber(chosen)
  const diff = c - a
  const floor = a + Math.floor(diff / PERIOD_DAYS) * PERIOD_DAYS
  return [fromDayNumber(floor), fromDayNumber(floor + PERIOD_DAYS)]
}

export interface PayPeriod {
  start: string
  end: string
}

/** The period (inclusive, 14 days) that contains `on`, for a schedule
 * anchored at `anchor`. Works before the anchor too. */
export function periodContaining(anchor: string, on: string): PayPeriod {
  const a = toDayNumber(anchor)
  const o = toDayNumber(on)
  const start = a + Math.floor((o - a) / PERIOD_DAYS) * PERIOD_DAYS
  return { start: fromDayNumber(start), end: fromDayNumber(start + PERIOD_DAYS - 1) }
}

/** Pay day for a period: its last day plus the schedule's offset. */
export function checkDate(period: PayPeriod, offsetDays: number): string {
  return addDays(period.end, offsetDays)
}

export function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}
