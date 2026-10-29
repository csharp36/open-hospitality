import { describe, expect, it } from 'vitest'

import { addDays, checkDate, isAligned, nearestAligned, periodContaining } from './paySchedule'

// The platform grid in config.py: payroll_period_anchor = 2026-01-05 (a Monday).
const GRID = '2026-01-05'

describe('paySchedule math', () => {
  it('knows which dates sit on the 14-day grid, in both directions', () => {
    expect(isAligned(GRID, '2026-01-05')).toBe(true)
    expect(isAligned(GRID, '2026-01-19')).toBe(true)
    expect(isAligned(GRID, '2025-12-22')).toBe(true) // one period before the anchor
    expect(isAligned(GRID, '2026-01-12')).toBe(false) // a Monday, but the wrong week
    expect(isAligned(GRID, '2026-10-26')).toBe(true) // 21 periods later
  })

  it('offers the grid dates on either side of a misaligned choice', () => {
    expect(nearestAligned(GRID, '2026-10-29')).toEqual(['2026-10-26', '2026-11-09'])
    // Exactly on the grid: that date and the next period.
    expect(nearestAligned(GRID, '2026-10-26')).toEqual(['2026-10-26', '2026-11-09'])
    // Before the anchor works too.
    expect(nearestAligned(GRID, '2025-12-30')).toEqual(['2025-12-22', '2026-01-05'])
  })

  it('finds the period containing a date and its pay day', () => {
    const p = periodContaining(GRID, '2026-10-29')
    expect(p).toEqual({ start: '2026-10-26', end: '2026-11-08' })
    expect(checkDate(p, 5)).toBe('2026-11-13')
    // Last day of a period is still inside it; the next day starts the next.
    expect(periodContaining(GRID, '2026-11-08').start).toBe('2026-10-26')
    expect(periodContaining(GRID, '2026-11-09').start).toBe('2026-11-09')
  })

  it('does day arithmetic across month ends and a DST change without drift', () => {
    expect(addDays('2026-02-28', 1)).toBe('2026-03-01')
    expect(addDays('2026-03-07', 7)).toBe('2026-03-14') // US DST starts 2026-03-08
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
  })
})
