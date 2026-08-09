import { describe, expect, it } from 'vitest'

import { datesInMonth, monthRange } from './months'

describe('monthRange', () => {
  it('returns the single month when first and last share it', () => {
    expect(monthRange('2026-07-01', '2026-07-07')).toEqual(['2026-07'])
  })

  it('spans a year boundary inclusively', () => {
    expect(monthRange('2025-11-15', '2026-02-03')).toEqual([
      '2025-11',
      '2025-12',
      '2026-01',
      '2026-02',
    ])
  })
})

describe('datesInMonth', () => {
  it('clamps to the property window inside the month', () => {
    expect(datesInMonth('2026-07', '2026-07-05', '2026-07-07')).toEqual([
      '2026-07-05',
      '2026-07-06',
      '2026-07-07',
    ])
  })

  it('covers the full month when the window is wider', () => {
    const dates = datesInMonth('2026-06', '2026-01-01', '2026-12-31')
    expect(dates).toHaveLength(30)
    expect(dates[0]).toBe('2026-06-01')
    expect(dates[29]).toBe('2026-06-30')
  })

  it('is leap-aware', () => {
    expect(datesInMonth('2028-02', '2028-01-01', '2028-12-31')).toHaveLength(29)
  })

  it('is empty when the month lies outside the window', () => {
    expect(datesInMonth('2026-08', '2026-07-01', '2026-07-07')).toEqual([])
  })
})
