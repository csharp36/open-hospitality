// Presentational drill-through slide-over: the journal entries behind one
// trial-balance account. SlideOverShell owns the dialog chrome and its
// keyboard contract; the page owns the fetch; this renders.

import type { JournalEntry, JournalLine } from '../api/types'
import { fmtMoney } from '../lib/format'
import SlideOverShell from './SlideOverShell'
import { amountCellClass, amountHeadClass, Badge, cellClass, headCellClass, tableClass } from './ui'

type JournalDrillPanelProps = {
  property: string
  period: string
  account: string
  accountName: string
  entries: JournalEntry[] | undefined
  error: string | null
  onClose: () => void
}

export default function JournalDrillPanel({
  property,
  period,
  account,
  accountName,
  entries,
  error,
  onClose,
}: JournalDrillPanelProps) {
  return (
    <SlideOverShell
      label={`Journal entries: ${account} — ${accountName}`}
      title={
        <>
          <span className="tabular-nums">{account}</span> — {accountName}
        </>
      }
      onClose={onClose}
    >
      <p className="mb-4 text-sm text-ink-muted">
        {property} · {period}
      </p>

      {error !== null && (
        <p className="text-sm text-danger-red">Failed to load journal entries: {error}</p>
      )}
      {entries === undefined && error === null && (
        <p className="text-sm text-ink-muted">Loading journal entries…</p>
      )}
      {/* An empty list while open is quiet, not an error: the account can
          drop out of the journal between the trial-balance fetch and this
          one (a mid-session repost). */}
      {entries !== undefined && entries.length === 0 && (
        <p className="text-sm text-ink-muted">No entries for this account in this period.</p>
      )}
      {entries !== undefined &&
        entries.map((entry) => <EntryBlock key={entry.entry_id} entry={entry} />)}
    </SlideOverShell>
  )
}

// No per-entry totals row: balance is enforced where entries are written
// (ck_journal_entry_balanced — test_an_unbalanced_entry_is_refused_at_commit
// in tests/test_gl_wall.py), so a sum row would only restate it.
function EntryBlock({ entry }: { entry: JournalEntry }) {
  return (
    <section className="mb-6">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-semibold tabular-nums text-ink">{entry.business_date}</span>
        <span className="text-sm text-ink-muted">{entry.source_type}</span>
        <span className="text-sm tabular-nums text-ink-muted">entry #{entry.entry_id}</span>
        <span className="text-sm text-ink-muted">posted by {entry.posted_by}</span>
        {entry.reversal_of !== null && (
          <Badge tone="info">reversal of entry #{entry.reversal_of}</Badge>
        )}
      </div>
      {entry.memo !== null && <p className="mt-0.5 text-sm text-ink-muted">{entry.memo}</p>}

      <table className={tableClass}>
        <thead>
          <tr className="border-b border-line">
            <th className={headCellClass}>Account</th>
            <th className={headCellClass}>Memo</th>
            <th className={amountHeadClass}>Debit</th>
            <th className={amountHeadClass}>Credit</th>
            <th className={headCellClass}>Source</th>
          </tr>
        </thead>
        <tbody>
          {entry.lines.map((line) => (
            <LineRow key={line.line_id} line={line} />
          ))}
        </tbody>
      </table>
    </section>
  )
}

function LineRow({ line }: { line: JournalLine }) {
  return (
    <tr className="border-b border-line">
      <td className={cellClass}>
        <span className="tabular-nums">{line.account_code}</span>{' '}
        <span>{line.account_name}</span>
      </td>
      <td className={cellClass}>{line.memo ?? ''}</td>
      {/* The wire keeps direction in `posting`, never in a sign; the amount
          lands under the column its posting names and the opposite cell stays
          empty. */}
      <td className={amountCellClass}>{line.posting === 'Debit' ? fmtMoney(line.amount) : ''}</td>
      <td className={amountCellClass}>{line.posting === 'Credit' ? fmtMoney(line.amount) : ''}</td>
      {/* Null provenance renders as nothing, not a dash: lines without a
          staged transaction (multi-fact groups, payroll accruals) have no
          source transaction by design — the memo is their provenance. */}
      <td className={`${cellClass} break-all text-ink-muted`}>
        {line.pms_trx_code !== null && (
          <>
            <span className="tabular-nums">{line.pms_trx_code}</span> · {line.source_file}
          </>
        )}
      </td>
    </tr>
  )
}
