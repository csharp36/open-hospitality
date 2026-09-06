// The selected period's trial balance: one row per account, the account name
// as the drill affordance, and the balanced badge from exact numeric
// comparison. Data and the drill handler come in as props — the page owns
// the fetch.

import type { TrialBalance } from '../api/types'
import { eqFixed } from '../lib/decimal'
import { fmtMoney } from '../lib/format'
import {
  amountCellClass,
  amountHeadClass,
  Badge,
  Card,
  cellClass,
  headCellClass,
  tableClass,
} from './ui'

// Same string as Statement.tsx's local lineButtonClass — the drill affordance
// reads identically on both pages.
const lineButtonClass =
  'text-left text-accent hover:underline focus-visible:underline cursor-pointer'

export default function TrialBalanceCard({
  tb,
  onDrill,
}: {
  tb: TrialBalance
  onDrill: (code: string, name: string) => void
}) {
  // Totals can serialize at different scales; only numeric comparison is
  // honest here (eqFixed's own contract).
  const balanced = eqFixed(tb.total_debits, tb.total_credits)

  return (
    <Card role="region" aria-label={`Trial balance ${tb.period_key}`}>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink">Trial balance — {tb.period_key}</h2>
        <p className="text-xs tabular-nums text-ink-muted">
          {tb.date_from} – {tb.date_to}
        </p>
      </div>

      <table className={tableClass}>
        <thead>
          <tr className="border-b border-line">
            <th className={headCellClass}>Account</th>
            <th className={headCellClass}>Name</th>
            <th className={headCellClass}>Type</th>
            <th className={amountHeadClass}>Debits</th>
            <th className={amountHeadClass}>Credits</th>
          </tr>
        </thead>
        <tbody>
          {tb.lines.map((line) => (
            <tr key={line.account_code} className="border-b border-line">
              <td className={`${cellClass} tabular-nums`}>{line.account_code}</td>
              <td className={cellClass}>
                {/* The account's name is the drill affordance — the Statement
                    line-button recipe, handing its account up to the page. */}
                <button
                  type="button"
                  className={lineButtonClass}
                  onClick={() => onDrill(line.account_code, line.name)}
                >
                  {line.name}
                </button>
              </td>
              <td className={`${cellClass} text-ink-muted`}>{line.account_type}</td>
              <td className={amountCellClass}>{fmtMoney(line.debits)}</td>
              <td className={amountCellClass}>{fmtMoney(line.credits)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-line-strong font-semibold">
            <td colSpan={3} className={cellClass}>
              Totals ({tb.lines.length} account{tb.lines.length === 1 ? '' : 's'})
            </td>
            <td className={amountCellClass}>{fmtMoney(tb.total_debits)}</td>
            <td className={amountCellClass}>{fmtMoney(tb.total_credits)}</td>
          </tr>
        </tfoot>
      </table>

      <p className="mt-4">
        {balanced ? (
          <Badge tone="ok">balanced ✓</Badge>
        ) : (
          <Badge tone="danger">
            Debits {fmtMoney(tb.total_debits)} does not equal credits {fmtMoney(tb.total_credits)}
          </Badge>
        )}
      </p>
    </Card>
  )
}
