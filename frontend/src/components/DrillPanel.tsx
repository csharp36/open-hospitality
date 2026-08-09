// Presentational drill-through slide-over: the staged PMS transactions behind
// one statement line, with a BigInt-exact footer sum and a reconciliation
// badge comparing that sum to the line total (scales differ — 2dp report
// totals vs 4dp staged amounts — so comparison is numeric, never raw-string).

import { useEffect, useRef } from 'react'

import type { SosLine, StagedTxn } from '../api/types'
import { eqFixed, sumFixed } from '../lib/decimal'
import { fmtMoney } from '../lib/format'
import { amountCellClass, amountHeadClass, Badge, cellClass, headCellClass, tableClass } from './ui'

type DrillPanelProps = {
  line: SosLine
  from: string
  to: string
  txns: StagedTxn[] | undefined
  loading: boolean
  error: string | null
  onClose: () => void
}

export default function DrillPanel({
  line,
  from,
  to,
  txns,
  loading,
  error,
  onClose,
}: DrillPanelProps) {
  const closeRef = useRef<HTMLButtonElement>(null)

  // Modal a11y: focus lands on the Close button when the panel opens, and
  // Escape closes it from anywhere.
  useEffect(() => {
    closeRef.current?.focus()
  }, [])
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-10">
      {/* bg-scrim stays dark in BOTH modes (token has no .dark remap) — a
          scrim dims the page behind the overlay; bg-ink/40 would invert to a
          light veil in dark mode. */}
      <div className="absolute inset-0 bg-scrim" onClick={onClose} aria-hidden="true" />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`Transactions: ${line.line_item}`}
        className="absolute inset-y-0 right-0 w-full max-w-2xl overflow-y-auto rounded-l-card border-l border-line bg-surface-raised p-6 shadow-overlay"
      >
        <div className="flex items-start justify-between">
          <h2 className="text-lg font-semibold text-ink">{line.line_item}</h2>
          <button
            ref={closeRef}
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="rounded-control px-2 text-xl leading-none text-ink-muted hover:bg-surface-sunken hover:text-ink"
          >
            ×
          </button>
        </div>
        <p className="mb-4 text-sm text-ink-muted">
          {line.major} · {line.sub_category} · {from === to ? from : `${from} – ${to}`}
        </p>

        {loading && <p className="text-sm text-ink-muted">Loading transactions…</p>}
        {error !== null && (
          <p className="text-sm text-danger-red">Failed to load transactions: {error}</p>
        )}
        {txns !== undefined && <TxnTable txns={txns} lineTotal={line.total} />}
      </aside>
    </div>
  )
}

function TxnTable({ txns, lineTotal }: { txns: StagedTxn[]; lineTotal: string }) {
  const sum = sumFixed(txns.map((t) => t.amount))
  const reconciled = eqFixed(sum, lineTotal)

  return (
    <>
      <table className={tableClass}>
        <thead>
          <tr className="border-b border-line">
            <th className={headCellClass}>Code</th>
            <th className={headCellClass}>Description</th>
            <th className={headCellClass}>Date</th>
            <th className={amountHeadClass}>Amount</th>
            <th className={headCellClass}>Source file</th>
          </tr>
        </thead>
        <tbody>
          {txns.map((t) => (
            <tr key={t.stage_id} className="border-b border-line">
              <td className={`${cellClass} tabular-nums`}>{t.pms_trx_code}</td>
              <td className={cellClass}>{t.pms_trx_desc ?? ''}</td>
              <td className={`${cellClass} whitespace-nowrap`}>{t.business_date}</td>
              <td className={amountCellClass}>{fmtMoney(t.amount)}</td>
              <td className={`${cellClass} break-all text-ink-muted`}>{t.source_file}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-line-strong font-semibold">
            <td colSpan={3} className={cellClass}>
              Sum ({txns.length} transaction{txns.length === 1 ? '' : 's'})
            </td>
            <td className={amountCellClass}>{fmtMoney(sum)}</td>
            <td />
          </tr>
        </tfoot>
      </table>

      <p className="mt-4">
        {reconciled ? (
          <Badge tone="ok">reconciled ✓</Badge>
        ) : (
          <Badge tone="danger">
            Transaction sum {fmtMoney(sum)} does not match line total {fmtMoney(lineTotal)}
          </Badge>
        )}
      </p>
    </>
  )
}
