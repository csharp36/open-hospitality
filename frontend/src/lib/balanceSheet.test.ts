// The balance sheet is pure arithmetic over trial-balance lines — no DOM
// here. subFixed rides along: balanceSheet is the caller it was added for.

import { describe, expect, it } from 'vitest'

import type { TrialBalanceLine } from '../api/types'
import { balanceSheet } from './balanceSheet'
import { eqFixed, subFixed } from './decimal'

function line(
  account_code: string,
  name: string,
  account_type: TrialBalanceLine['account_type'],
  debits: string,
  credits: string,
): TrialBalanceLine {
  return { account_code, name, account_type, debits, credits }
}

// Balanced by construction: total debits 1500.00 == total credits 1500.00.
const BALANCED: TrialBalanceLine[] = [
  line('1010', 'Cash - Operating', 'asset', '1000.00', '0.00'),
  line('1510', 'Accumulated Depreciation', 'contra_asset', '0.00', '100.00'),
  line('2010', 'Accounts Payable', 'liability', '0.00', '400.00'),
  line('3010', 'Owner Equity', 'equity', '0.00', '100.00'),
  line('4100', 'Rooms Revenue', 'income', '0.00', '900.00'),
  line('6100', 'Wages - Rooms', 'expense', '500.00', '0.00'),
]

describe('subFixed', () => {
  it('subtracts exactly, result at the wider scale (like addFixed)', () => {
    expect(subFixed('100.00', '30.0000')).toBe('70.0000')
    expect(eqFixed(subFixed('100.00', '30.0000'), '70')).toBe(true)
  })

  it('crosses zero without float drift', () => {
    // 0.1 - 0.3 !== -0.2 in binary floating point.
    expect(subFixed('0.1', '0.3')).toBe('-0.2')
    expect(subFixed('500.0000', '1200.0000')).toBe('-700.0000')
  })

  it('rejects non-decimal input', () => {
    expect(() => subFixed('abc', '1')).toThrow(/not a fixed-point decimal/)
  })
})

describe('balanceSheet', () => {
  it('groups by account_type, each side netted toward its section', () => {
    const bs = balanceSheet(BALANCED)
    // Assets net debits - credits; contra_asset sits inside assets and its
    // credit balance shows as a negative net, no special casing.
    expect(bs.assets).toEqual([
      { account_code: '1010', name: 'Cash - Operating', net: '1000.00' },
      { account_code: '1510', name: 'Accumulated Depreciation', net: '-100.00' },
    ])
    expect(bs.liabilities).toEqual([
      { account_code: '2010', name: 'Accounts Payable', net: '400.00' },
    ])
    // Income and expense fold into the synthetic equity line, last in the
    // section: 900.00 revenue - 500.00 wages.
    expect(bs.equity).toEqual([
      { account_code: '3010', name: 'Owner Equity', net: '100.00' },
      { account_code: '', name: 'Net income (this period)', net: '400.00' },
    ])
  })

  it('foots when the input is balanced', () => {
    const { foot } = balanceSheet(BALANCED)
    expect(foot.totalAssets).toBe('900.00')
    expect(eqFixed(foot.totalAssets, foot.totalLiabilitiesAndEquity)).toBe(true)
    expect(foot.balanced).toBe(true)
  })

  it('reports an unbalanced input instead of throwing', () => {
    // Without the liability, credits fall 400.00 short of debits.
    const bs = balanceSheet(BALANCED.filter((l) => l.account_type !== 'liability'))
    expect(bs.foot.balanced).toBe(false)
    expect(eqFixed(bs.foot.totalAssets, bs.foot.totalLiabilitiesAndEquity)).toBe(false)
  })

  it('drops zero-net accounts from the sections', () => {
    // A fully reversed account: a statement line of 0.00 is noise, and the
    // drill for the why lives on the trial balance above.
    const bs = balanceSheet([
      ...BALANCED,
      line('1300', 'Guest Deposits', 'asset', '250.0000', '250.0000'),
    ])
    expect(bs.assets.map((l) => l.account_code)).toEqual(['1010', '1510'])
    // Its zero also moves the foot by nothing.
    expect(eqFixed(bs.foot.totalAssets, '900')).toBe(true)
  })
})
