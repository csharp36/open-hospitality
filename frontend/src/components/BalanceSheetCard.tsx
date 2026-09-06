// The period's balance sheet, derived client-side from the trial-balance
// lines the page already holds (lib/balanceSheet.ts) — no fetch of its own.

import type { TrialBalance } from '../api/types'
import { balanceSheet, type BalanceSheetLine } from '../lib/balanceSheet'
import { subFixed } from '../lib/decimal'
import { fmtImbalance, fmtMoney } from '../lib/format'
import {
  amountCellClass,
  amountHeadClass,
  Badge,
  Card,
  cellClass,
  headCellClass,
  tableClass,
} from './ui'

// The heading names these as the period's net change — period-scoped numbers
// (the derivation's header comment in lib/balanceSheet.ts says why), and a
// bare "Balance sheet" over them would be a false statement of position.
export default function BalanceSheetCard({ tb }: { tb: TrialBalance }) {
  const bs = balanceSheet(tb.lines)

  return (
    <Card role="region" aria-label={`Balance sheet ${tb.period_key}`}>
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink">
          Balance sheet — net change for {tb.period_key}
        </h2>
        <p className="text-xs tabular-nums text-ink-muted">
          {tb.date_from} – {tb.date_to}
        </p>
      </div>
      <p className="mb-3 text-xs text-ink-muted">
        These figures are the journal's activity for this period, not a cumulative position.
      </p>

      <table className={tableClass}>
        <thead>
          <tr className="border-b border-line">
            <th className={headCellClass}>Account</th>
            <th className={headCellClass}>Name</th>
            <th className={amountHeadClass}>Net change</th>
          </tr>
        </thead>
        <tbody>
          <BalanceSheetSection title="Assets" lines={bs.assets} />
          <BalanceSheetSection title="Liabilities" lines={bs.liabilities} />
          <BalanceSheetSection title="Equity" lines={bs.equity} />
        </tbody>
        <tfoot>
          <tr className="border-t border-line-strong font-semibold">
            <td colSpan={2} className={cellClass}>
              Total assets
            </td>
            <td className={amountCellClass}>{fmtMoney(bs.foot.totalAssets)}</td>
          </tr>
          <tr className="font-semibold">
            <td colSpan={2} className={cellClass}>
              Total liabilities and equity
            </td>
            <td className={amountCellClass}>{fmtMoney(bs.foot.totalLiabilitiesAndEquity)}</td>
          </tr>
        </tfoot>
      </table>

      <p className="mt-4">
        {bs.foot.balanced ? (
          <Badge tone="ok">balanced ✓</Badge>
        ) : (
          <Badge tone="danger">
            {/* The delta stays full precision — fmtImbalance's doc says why. */}
            Assets {fmtMoney(bs.foot.totalAssets)} does not equal liabilities and equity{' '}
            {fmtMoney(bs.foot.totalLiabilitiesAndEquity)} —{' '}
            {fmtImbalance(
              subFixed(bs.foot.totalAssets, bs.foot.totalLiabilitiesAndEquity),
              'assets',
              'liabilities and equity',
            )}
          </Badge>
        )}
      </p>
    </Card>
  )
}

// A section header row followed by its lines; the synthetic net income line
// has no account code and renders an empty code cell.
function BalanceSheetSection({ title, lines }: { title: string; lines: BalanceSheetLine[] }) {
  return (
    <>
      <tr className="border-b border-line">
        <th colSpan={3} scope="colgroup" className={`${cellClass} text-left font-semibold`}>
          {title}
        </th>
      </tr>
      {lines.map((line) => (
        <tr key={line.account_code || line.name} className="border-b border-line">
          <td className={`${cellClass} tabular-nums`}>{line.account_code}</td>
          <td className={cellClass}>{line.name}</td>
          <td className={amountCellClass}>{fmtMoney(line.net)}</td>
        </tr>
      ))}
    </>
  )
}
