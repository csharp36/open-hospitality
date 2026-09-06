// The balance sheet as a pure derivation over trial-balance lines. It is the
// period's NET CHANGE, not a cumulative position: the only shipped read is
// the period-scoped trial balance (plan "things to get right" #1), and the
// page's title says so. All arithmetic is BigInt fixed-point (lib/decimal) —
// amounts stay exact decimal strings end to end.

import type { TrialBalanceLine } from '../api/types'
import { eqFixed, subFixed, sumFixed } from './decimal'

/** One statement line. The synthetic net income line has an empty code. */
export interface BalanceSheetLine {
  account_code: string
  name: string
  net: string
}

/** The statement's foot, as data — the page decides how to badge it. */
export interface BalanceSheetFoot {
  totalAssets: string
  totalLiabilitiesAndEquity: string
  balanced: boolean
}

export interface BalanceSheetReport {
  assets: BalanceSheetLine[]
  liabilities: BalanceSheetLine[]
  equity: BalanceSheetLine[]
  foot: BalanceSheetFoot
}

/**
 * Group trial-balance lines into balance-sheet sections and foot them.
 *
 * Assets net debits - credits (a contra_asset sits inside assets, its credit
 * balance showing as a negative net); liabilities and equity net the other
 * way. Income and expense fold into one synthetic equity line, "Net income
 * (this period)" — that articulation is what makes the foot visible:
 * total debits == total credits implies totalAssets == liabilities + equity.
 *
 * Zero-net accounts (a fully reversed account) are dropped: a statement line
 * of 0.00 is noise, and the drill for the why lives on the trial balance.
 *
 * An unbalanced input is reported (balanced: false), never thrown — the DB
 * trigger owns that wall; its refusal is pinned by
 * tests/test_gl_wall.py::test_an_unbalanced_entry_is_refused_at_commit.
 */
export function balanceSheet(lines: readonly TrialBalanceLine[]): BalanceSheetReport {
  const assets: BalanceSheetLine[] = []
  const liabilities: BalanceSheetLine[] = []
  const equity: BalanceSheetLine[] = []
  const incomeNets: string[] = []
  const expenseNets: string[] = []

  for (const line of lines) {
    const debitNet = subFixed(line.debits, line.credits)
    const creditNet = subFixed(line.credits, line.debits)
    const { account_code, name } = line
    switch (line.account_type) {
      case 'asset':
      case 'contra_asset':
        assets.push({ account_code, name, net: debitNet })
        break
      case 'liability':
        liabilities.push({ account_code, name, net: creditNet })
        break
      case 'equity':
        equity.push({ account_code, name, net: creditNet })
        break
      case 'income':
        incomeNets.push(creditNet)
        break
      case 'expense':
        expenseNets.push(debitNet)
        break
      default: {
        // Exhaustive on GlAccountType: a widened backend set becomes a type
        // error here, never a silently misfiled account.
        const unhandled: never = line.account_type
        throw new Error(`unhandled account_type: ${String(unhandled)}`)
      }
    }
  }

  equity.push({
    account_code: '',
    name: 'Net income (this period)',
    net: subFixed(sumFixed(incomeNets), sumFixed(expenseNets)),
  })

  // Foot before dropping zero-net lines; a dropped zero moves nothing.
  const totalAssets = sumFixed(assets.map((l) => l.net))
  const totalLiabilitiesAndEquity = sumFixed(
    [...liabilities, ...equity].map((l) => l.net),
  )

  const dropZero = (section: BalanceSheetLine[]) =>
    section.filter((l) => !eqFixed(l.net, '0'))
  return {
    assets: dropZero(assets),
    liabilities: dropZero(liabilities),
    equity: dropZero(equity),
    foot: {
      totalAssets,
      totalLiabilitiesAndEquity,
      balanced: eqFixed(totalAssets, totalLiabilitiesAndEquity),
    },
  }
}
