// Month/date derivation from the /api/properties date window: the endpoint
// gives first/last DATES (`first_date`/`last_date`, ISO YYYY-MM-DD), and the
// Reports/QBO month pickers need months while the QBO push table needs the
// individual dates of the picked month. Integer arithmetic on the ISO string
// parts — no Date parsing of date-only strings, so no timezone drift.

/** Inclusive list of 'YYYY-MM' months spanning firstDate..lastDate. */
export function monthRange(firstDate: string, lastDate: string): string[] {
  let year = Number(firstDate.slice(0, 4))
  let month = Number(firstDate.slice(5, 7))
  const lastYear = Number(lastDate.slice(0, 4))
  const lastMonth = Number(lastDate.slice(5, 7))
  const months: string[] = []
  while (year < lastYear || (year === lastYear && month <= lastMonth)) {
    months.push(`${year}-${String(month).padStart(2, '0')}`)
    month += 1
    if (month === 13) {
      month = 1
      year += 1
    }
  }
  return months
}

/** Day count of 'YYYY-MM' (leap-aware via Date.UTC day-0 rollback). */
function daysInMonth(month: string): number {
  const y = Number(month.slice(0, 4))
  const m = Number(month.slice(5, 7))
  return new Date(Date.UTC(y, m, 0)).getUTCDate()
}

/**
 * Every 'YYYY-MM-DD' of `month`, clamped to the [firstDate, lastDate] window
 * (ISO date strings compare lexicographically). Empty when the month lies
 * outside the window entirely.
 */
export function datesInMonth(month: string, firstDate: string, lastDate: string): string[] {
  const dates: string[] = []
  for (let day = 1; day <= daysInMonth(month); day++) {
    const iso = `${month}-${String(day).padStart(2, '0')}`
    if (iso >= firstDate && iso <= lastDate) dates.push(iso)
  }
  return dates
}
