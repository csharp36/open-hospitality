import { describe, expect, it } from 'vitest'
import { addDays, dayName, upcomingWeekMonday } from './week'

describe('upcomingWeekMonday', () => {
  it('returns next Monday from a mid-week day', () => {
    // 2026-07-16 is a Thursday; the upcoming week starts 2026-07-20.
    expect(upcomingWeekMonday(new Date('2026-07-16T12:00:00'))).toBe('2026-07-20')
  })

  it('returns the NEXT Monday even when today IS a Monday', () => {
    // The kiosk answers "next week", never the week already in progress.
    expect(upcomingWeekMonday(new Date('2026-07-13T09:00:00'))).toBe('2026-07-20')
  })

  it('rolls a Sunday evening to the very next day', () => {
    expect(upcomingWeekMonday(new Date('2026-07-19T23:00:00'))).toBe('2026-07-20')
  })

  it('lands on the payroll anchor grid (anchor 2026-01-05 is a Monday)', () => {
    const monday = upcomingWeekMonday(new Date('2026-07-16T12:00:00'))
    const days = (Date.parse(monday) - Date.parse('2026-01-05')) / 86_400_000
    expect(days % 7).toBe(0)
  })
})

describe('addDays / dayName', () => {
  it('adds days without timezone slip and crosses month ends', () => {
    expect(addDays('2026-07-20', 6)).toBe('2026-07-26')
    expect(addDays('2026-07-31', 1)).toBe('2026-08-01')
  })

  it('names days Mon-first', () => {
    expect(dayName('2026-07-20')).toBe('Mon')
    expect(dayName('2026-07-26')).toBe('Sun')
  })
})
