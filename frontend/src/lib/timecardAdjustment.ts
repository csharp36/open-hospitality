// Shared pieces of a timecard correction, kept out of the component file so
// fast refresh keeps working (a component module exports only components).
import type { TimecardAdjustmentBody } from '../api/types'

/** The four event kinds the server accepts (`_PUNCH_TYPES` in timecard_api.py). */
export const PUNCH_TYPES: { value: TimecardAdjustmentBody['punch_type']; label: string }[] = [
  { value: 'clock_in', label: 'Clock in' },
  { value: 'lunch_start', label: 'Lunch start' },
  { value: 'lunch_end', label: 'Lunch end' },
  { value: 'clock_out', label: 'Clock out' },
]

/** The server's column width for `reason` (a longer one is a 422 at the door). */
export const REASON_MAX = 300

/** Local wall-clock day + "HH:MM" -> an AWARE ISO instant in the browser's
 * zone. Punches are aware and the server sorts both into one timeline; a
 * naive value would either break that sort or force a guessed zone. */
export function toAwareIso(day: string, time: string): string {
  return new Date(`${day}T${time}:00`).toISOString()
}
