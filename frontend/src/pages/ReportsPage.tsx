// CPA monthly pack page: property + month picker (state in the URL search
// params), then the three pack sections — Sales (TOR emphasized), Taxes
// (with the room-revenue base), and A/R balances. The A/R "opening" column is
// labeled "First reported": it is the earliest balance reported IN the month,
// not the prior month's close (see ArLine in api/types.ts). "Download JSON"
// saves the exact API payload; CSV export deliberately stays a CLI concern
// (`usali cpa-pack --format csv`) — noted in the page footer.

import { skipToken, useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'

import { getCpaPack } from '../api/client'
import type { ArReport, CpaPack, SalesReport, TaxReport } from '../api/types'
import MonthPickerBar from '../components/MonthPickerBar'
import {
  amountCellClass,
  amountHeadClass,
  Card,
  cellClass,
  headCellClass,
  PageHeader,
  sectionHeadClass,
  tableClass,
} from '../components/ui'
import { errorMessage } from '../lib/errors'
import { useGlobalProperty } from '../lib/propertyContext'
import { fmtMoney } from '../lib/format'
import type { PropertyMonthSearch } from '../router'

// getRouteApi avoids the router.tsx <-> ReportsPage.tsx circular value import.
const routeApi = getRouteApi('/reports')

export default function ReportsPage() {
  const search = routeApi.useSearch()
  const navigate = routeApi.useNavigate()

  function updateSearch(patch: Partial<PropertyMonthSearch>) {
    void navigate({ search: (prev) => ({ ...prev, ...patch }), replace: true })
  }

  // Property is GLOBAL (top-bar selector); only the month is page state.
  const { property, selected } = useGlobalProperty()

  const params =
    property !== undefined && search.month !== undefined
      ? { property, month: search.month }
      : null

  const packQuery = useQuery({
    queryKey: ['cpa-pack', property, search.month],
    // skipToken disables the query until the picker state is complete.
    queryFn: params === null ? skipToken : () => getCpaPack(params.property, params.month),
  })

  return (
    <div className="space-y-4">
      <PageHeader
        title="CPA Monthly Pack"
        actions={packQuery.data !== undefined ? <DownloadJsonButton pack={packQuery.data} /> : undefined}
      />

      <Card>
        <MonthPickerBar
          selected={selected}
          month={search.month}
          onMonthChange={(month) => updateSearch({ month: month === '' ? undefined : month })}
        />
      </Card>

      {params === null ? (
        <p className="text-sm text-ink-muted">Pick a month to view the pack.</p>
      ) : (
        <>
          {packQuery.isPending && <p className="text-sm text-ink-muted">Loading pack…</p>}
          {packQuery.isError && (
            <p className="text-sm text-danger-red">
              Failed to load pack: {errorMessage(packQuery.error)}
            </p>
          )}
          {packQuery.data !== undefined && <PackView pack={packQuery.data} />}
        </>
      )}

      <p className="text-xs text-ink-muted">
        Need CSV? Export from the CLI: <code>usali cpa-pack --format csv --out DIR</code>. CSV
        stays a CLI concern by design — this page serves the on-screen review and the JSON
        download.
      </p>
    </div>
  )
}

function DownloadJsonButton({ pack }: { pack: CpaPack }) {
  function download() {
    const blob = new Blob([JSON.stringify(pack, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `cpa-pack-${pack.property_id}-${pack.month}.json`
    // In the DOM for the click (some browsers ignore detached-anchor clicks);
    // revocation deferred so the download can start before the URL dies.
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  return (
    // Primary action. text-accent-contrast, NOT text-white: the dark-mode
    // accent is indigo-400, where white text fails AA — the contrast token is
    // white in light mode and flips to near-black in dark mode.
    <button
      type="button"
      onClick={download}
      className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast hover:opacity-90"
    >
      Download JSON
    </button>
  )
}

// --- Presentational sections ---------------------------------------------------

function PackView({ pack }: { pack: CpaPack }) {
  return (
    <div className="max-w-4xl space-y-4">
      <p className="text-sm text-ink-muted">
        Property: {pack.property_id} · PMS source: {pack.pms_source} · Month: {pack.month}
      </p>
      <SalesSection sales={pack.sales} />
      <TaxSection taxes={pack.taxes} />
      <ArSection ar={pack.ar} />
    </div>
  )
}

function SalesSection({ sales }: { sales: SalesReport }) {
  // Card spreads rest props onto its div, so role="region" + aria-label keep
  // the landmark the tests locate via getByRole('region', { name: 'Sales' }).
  return (
    <Card role="region" aria-label="Sales">
      <h2 className={sectionHeadClass}>SALES</h2>
      {sales.lines.length === 0 ? (
        <p className="mt-2 text-sm text-ink-muted">No sales lines this month.</p>
      ) : (
        <table className={`mt-2 ${tableClass}`}>
          <thead>
            <tr className="border-b border-line">
              <th className={headCellClass}>Sub-category</th>
              <th className={headCellClass}>Line item</th>
              <th className={amountHeadClass}>MTD amount</th>
              <th className={amountHeadClass}>Days</th>
            </tr>
          </thead>
          <tbody>
            {sales.lines.map((line) => (
              <tr
                key={`${line.major}|${line.sub_category}|${line.line_item}`}
                className="border-b border-line last:border-0"
              >
                <td className={cellClass}>{line.sub_category}</td>
                <td className={cellClass}>{line.line_item}</td>
                <td className={amountCellClass}>{fmtMoney(line.mtd_amount)}</td>
                <td className={amountCellClass}>{line.day_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {/* Same emphasized TOR bar as Statement — bg-ink/text-surface-raised
          inverts naturally in dark mode via the token remap. */}
      <div className="mt-3 flex items-center justify-between rounded-control bg-ink px-3 py-2 text-base font-bold text-surface-raised">
        <span>TOTAL OPERATING REVENUE</span>
        <span className="tabular-nums">{fmtMoney(sales.total_operating_revenue)}</span>
      </div>
    </Card>
  )
}

function TaxSection({ taxes }: { taxes: TaxReport }) {
  return (
    <Card role="region" aria-label="Taxes">
      <h2 className={sectionHeadClass}>TAXES COLLECTED (PASS-THROUGH)</h2>
      {taxes.lines.length === 0 ? (
        <p className="mt-2 text-sm text-ink-muted">No tax lines this month.</p>
      ) : (
        <table className={`mt-2 ${tableClass}`}>
          <thead>
            <tr className="border-b border-line">
              <th className={headCellClass}>Line item</th>
              <th className={headCellClass}>GL account</th>
              <th className={amountHeadClass}>MTD amount</th>
            </tr>
          </thead>
          <tbody>
            {taxes.lines.map((line) => (
              <tr key={line.line_item} className="border-b border-line last:border-0">
                <td className={cellClass}>{line.line_item}</td>
                <td className={`${cellClass} tabular-nums`}>{line.gl_account_code ?? '—'}</td>
                <td className={amountCellClass}>{fmtMoney(line.mtd_amount)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-line-strong font-semibold">
              <td colSpan={2} className={cellClass}>
                Total taxes collected
              </td>
              <td className={amountCellClass}>{fmtMoney(taxes.taxes_total)}</td>
            </tr>
          </tfoot>
        </table>
      )}
      <p className="mt-2 text-sm text-ink-muted">
        Room revenue base:{' '}
        <span className="tabular-nums text-ink">{fmtMoney(taxes.room_revenue_base)}</span>
      </p>
    </Card>
  )
}

function ArSection({ ar }: { ar: ArReport }) {
  return (
    <Card role="region" aria-label="Accounts receivable">
      <h2 className={sectionHeadClass}>A/R LEDGER BALANCES</h2>
      {ar.balances.length === 0 ? (
        <p className="mt-2 text-sm text-ink-muted">No A/R balances this month.</p>
      ) : (
        <table className={`mt-2 ${tableClass}`}>
          <thead>
            <tr className="border-b border-line">
              <th className={headCellClass}>Ledger</th>
              <th className={headCellClass}>Name</th>
              <th className={amountHeadClass}>First reported balance *</th>
              <th className={amountHeadClass}>Closing balance</th>
              <th className={amountHeadClass}>Movement</th>
            </tr>
          </thead>
          <tbody>
            {ar.balances.map((line) => (
              <tr key={line.ledger_code} className="border-b border-line last:border-0">
                <td className={`${cellClass} tabular-nums`}>{line.ledger_code}</td>
                <td className={cellClass}>{line.ledger_name}</td>
                <td className={amountCellClass}>{fmtMoney(line.opening_balance)}</td>
                <td className={amountCellClass}>{fmtMoney(line.closing_balance)}</td>
                <td className={amountCellClass}>{fmtMoney(line.movement)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="mt-2 text-xs text-ink-muted">
        * “First reported” is the earliest balance reported within the month — after an ingestion
        gap it is NOT the prior month's close.
      </p>
    </Card>
  )
}
