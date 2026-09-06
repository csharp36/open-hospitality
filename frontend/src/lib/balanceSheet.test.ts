// The balance sheet is pure arithmetic over trial-balance lines — no DOM
// here. subFixed's own tests live beside its siblings in decimal.test.ts.

import { describe, expect, it } from 'vitest'

import type { TrialBalanceLine } from '../api/types'
import { balanceSheet } from './balanceSheet'
import { eqFixed } from './decimal'

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

  it('drops a zero net income line under the same zero-net rule', () => {
    // Income fully offset by expense: the synthetic line nets to zero and is
    // dropped like any zero-net account line — the uniform rule, a decision,
    // not an accident.
    const bs = balanceSheet([
      line('4100', 'Rooms Revenue', 'income', '0.00', '500.00'),
      line('6100', 'Wages - Rooms', 'expense', '500.00', '0.00'),
    ])
    expect(bs.equity).toEqual([])
    expect(bs.foot.balanced).toBe(true)
  })

  it('returns empty sections and a balanced foot for no lines at all', () => {
    expect(balanceSheet([])).toEqual({
      assets: [],
      liabilities: [],
      equity: [],
      foot: { totalAssets: '0', totalLiabilitiesAndEquity: '0', balanced: true },
    })
  })
})
