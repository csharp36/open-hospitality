import { describe, expect, it } from 'vitest'

import { addFixed, eqFixed, sumFixed } from './decimal'

describe('addFixed', () => {
  it('adds same-scale storage strings exactly', () => {
    expect(addFixed('250.0000', '160.0000')).toBe('410.0000')
  })

  it('aligns differing scales to the wider one', () => {
    expect(addFixed('410.00', '0.0000')).toBe('410.0000')
    expect(addFixed('1', '0.5')).toBe('1.5')
  })

  it('handles negatives and sign crossings', () => {
    expect(addFixed('-16.2000', '16.2000')).toBe('0.0000')
    expect(addFixed('-10.50', '-0.25')).toBe('-10.75')
    expect(addFixed('5.0000', '-16.2000')).toBe('-11.2000')
  })

  it('is exact where floats are not', () => {
    // 0.1 + 0.2 !== 0.3 in binary floating point.
    expect(addFixed('0.1', '0.2')).toBe('0.3')
    expect(addFixed('9007199254740993.0001', '0.0001')).toBe('9007199254740993.0002')
  })

  it('rejects non-decimal input', () => {
    expect(() => addFixed('abc', '1')).toThrow(/not a fixed-point decimal/)
    expect(() => addFixed('1e3', '1')).toThrow(/not a fixed-point decimal/)
  })
})

describe('eqFixed', () => {
  it('compares across scales', () => {
    expect(eqFixed('410.00', '410.0000')).toBe(true)
    expect(eqFixed('410', '410.0000')).toBe(true)
    expect(eqFixed('-16.2', '-16.2000')).toBe(true)
  })

  it('detects real differences', () => {
    expect(eqFixed('410.01', '410.0000')).toBe(false)
    expect(eqFixed('-410.00', '410.0000')).toBe(false)
  })

  it('treats 0 and -0 forms as equal', () => {
    expect(eqFixed('0.0000', '-0.00')).toBe(true)
  })
})

describe('sumFixed', () => {
  it('sums a list and returns 0 for empty', () => {
    expect(sumFixed(['250.0000', '160.0000'])).toBe('410.0000')
    expect(sumFixed([])).toBe('0')
  })
})
