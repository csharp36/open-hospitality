import { describe, expect, it } from 'vitest'

import { fmtMoney, fmtStat, pct } from './format'

describe('fmtMoney', () => {
  it('formats 4dp storage strings to 2dp with thousands separators', () => {
    expect(fmtMoney('10866.3700')).toBe('10,866.37')
    expect(fmtMoney('410.0000')).toBe('410.00')
    expect(fmtMoney('1234567.8900')).toBe('1,234,567.89')
  })

  it('formats negatives with a minus sign', () => {
    expect(fmtMoney('-16.2000')).toBe('-16.20')
  })

  it('normalizes negative zero', () => {
    expect(fmtMoney('-0.0000')).toBe('0.00')
  })
})

describe('pct', () => {
  it('renders one decimal place', () => {
    expect(pct('7842.2775', '10456.3700')).toBe('75.0%')
    expect(pct('2614.0925', '10456.3700')).toBe('25.0%')
  })

  it('returns n/a on zero total', () => {
    expect(pct('0.0000', '0.0000')).toBe('n/a')
  })
})

describe('fmtStat', () => {
  it('trims trailing fractional zeros only', () => {
    expect(fmtStat('95.5000')).toBe('95.5')
    expect(fmtStat('11.0000')).toBe('11')
    expect(fmtStat('100')).toBe('100')
    expect(fmtStat('0.0000')).toBe('0')
  })

  it('renders null as empty', () => {
    expect(fmtStat(null)).toBe('')
  })
})
