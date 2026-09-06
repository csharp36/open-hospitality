// General ledger page: the period rail IS the period picker (selection lives
// in the ?period= search param so a view is linkable and survives reload),
// and the trial balance renders for the selected period. All fetching lives
// here (TanStack Query keyed on property + search params).

import { useState } from 'react'
import { skipToken, useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'

import { ApiError } from '../api/client'
import { getGlPeriods, getTrialBalance } from '../api/gl'
import type { TrialBalance } from '../api/types'
import {
  amountCellClass,
  amountHeadClass,
  Badge,
  Card,
  cellClass,
  controlClass,
  headCellClass,
  PageHeader,
  tableClass,
} from '../components/ui'
import { eqFixed } from '../lib/decimal'
import { errorMessage } from '../lib/errors'
import { fmtMoney } from '../lib/format'
import { useGlobalProperty } from '../lib/propertyContext'

// getRouteApi avoids the router.tsx <-> GlPage.tsx circular value import.
const routeApi = getRouteApi('/gl')

export default function GlPage() {
  const search = routeApi.useSearch()
  const navigate = routeApi.useNavigate()
  // Property comes from the GLOBAL top-bar selector — picked once, app-wide.
  const { property } = useGlobalProperty()
  // The rail starts on the calendar year containing today and always passes
  // it, so the query key names the year actually fetched. (The backend's own
  // default for an omitted fiscal_year lives in gl_api.get_periods; on a
  // non-calendar fiscal calendar the two can label the year differently, and
  // the stepper is the remedy either way.)
  const [fiscalYear, setFiscalYear] = useState(() => new Date().getFullYear())

  const period = search.period

  const periodsQuery = useQuery({
    queryKey: ['gl-periods', property, fiscalYear],
    // skipToken disables the query until a property exists.
    queryFn: property === undefined ? skipToken : () => getGlPeriods(property, fiscalYear),
  })

  const tbQuery = useQuery({
    queryKey: ['gl-trial-balance', property, period],
    // skipToken until both halves of the request exist; a malformed ?period=
    // never reaches here — the route validator clamps it to undefined.
    queryFn:
      property === undefined || period === undefined
        ? skipToken
        : () => getTrialBalance(property, period),
  })

  function selectPeriod(key: string) {
    void navigate({ search: (prev) => ({ ...prev, period: key }), replace: true })
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="General Ledger"
        subtitle="The trial balance and the books' periods, from the journal."
      />

      {property === undefined ? (
        <Card>
          <p className="text-sm text-ink-muted">No property selected yet.</p>
        </Card>
      ) : (
        <>
          <Card className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <label className="text-xs font-medium text-ink-muted" htmlFor="gl-fiscal-year">
                Fiscal year
              </label>
              <input
                id="gl-fiscal-year"
                type="number"
                className={`${controlClass} w-24`}
                value={fiscalYear}
                onChange={(e) => {
                  const year = parseInt(e.target.value, 10)
                  if (!Number.isNaN(year)) setFiscalYear(year)
                }}
              />
            </div>

            {periodsQuery.isPending && <p className="text-sm text-ink-muted">Loading periods…</p>}
            {periodsQuery.isError && (
              <p className="text-sm text-danger-red">
                Failed to load periods: {errorMessage(periodsQuery.error)}
              </p>
            )}
            {periodsQuery.data !== undefined && (
              <div
                role="group"
                aria-label="Fiscal periods"
                className="flex flex-wrap gap-0.5 rounded-full border border-line p-0.5 self-start w-fit"
              >
                {periodsQuery.data.map((p) => (
                  <button
                    key={p.period_key}
                    type="button"
                    aria-pressed={p.period_key === period}
                    onClick={() => selectPeriod(p.period_key)}
                    className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium tabular-nums transition-colors ${
                      p.period_key === period
                        ? 'bg-accent text-accent-contrast'
                        : 'text-ink-muted hover:bg-surface-sunken hover:text-ink'
                    }`}
                  >
                    <span>{p.period_key}</span>
                    {p.state === 'closed' && <Badge tone="neutral">Closed</Badge>}
                  </button>
                ))}
              </div>
            )}
          </Card>

          {period === undefined ? (
            <p className="text-sm text-ink-muted">Pick a period to view its trial balance.</p>
          ) : (
            <>
              {tbQuery.isPending && (
                <p className="text-sm text-ink-muted">Loading trial balance…</p>
              )}
              {tbQuery.isError && <TrialBalanceError error={tbQuery.error} />}
              {tbQuery.data !== undefined && <TrialBalanceCard tb={tbQuery.data} />}
            </>
          )}
        </>
      )}
    </div>
  )
}

// 404 = "nothing posted here" (gl_api._run maps trial_balance's NoFactsError
// to it) — the state every new tenant starts in, rendered calmly. Anything
// else is a real problem and stays red.
function TrialBalanceError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.status === 404) {
    return (
      <Card>
        <p className="text-sm font-medium text-ink">Nothing posted for this period yet.</p>
        <p className="mt-1 text-sm text-ink-muted">{error.detail}</p>
      </Card>
    )
  }
  return (
    <p className="text-sm text-danger-red">
      Failed to load trial balance: {errorMessage(error)}
    </p>
  )
}

function TrialBalanceCard({ tb }: { tb: TrialBalance }) {
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
              <td className={cellClass}>{line.name}</td>
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
