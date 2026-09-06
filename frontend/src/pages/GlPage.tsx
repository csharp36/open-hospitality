// General ledger page: the period rail IS the period picker (selection lives
// in the ?period= search param so a view is linkable and survives reload),
// and the trial balance renders for the selected period. All fetching lives
// here (TanStack Query keyed on property + search params).

import { useEffect, useState } from 'react'
import { keepPreviousData, skipToken, useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'

import { ApiError } from '../api/client'
import { getGlPeriods, getJournalEntries, getTrialBalance } from '../api/gl'
import type { TrialBalance } from '../api/types'
import JournalDrillPanel from '../components/JournalDrillPanel'
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
import { balanceSheet, type BalanceSheetLine } from '../lib/balanceSheet'
import { eqFixed } from '../lib/decimal'
import { errorMessage } from '../lib/errors'
import { fmtMoney } from '../lib/format'
import { useGlobalProperty } from '../lib/propertyContext'

// getRouteApi avoids the router.tsx <-> GlPage.tsx circular value import.
const routeApi = getRouteApi('/gl')

// The stepper's sane window: wide enough for any books this app will meet,
// tight enough that a half-typed year ("2", "20") never commits to state and
// so never fires a fetch.
const FISCAL_YEAR_MIN = 2000
const FISCAL_YEAR_MAX = 2100

// Same string as Statement.tsx's local lineButtonClass — the drill affordance
// reads identically on both pages.
const lineButtonClass =
  'text-left text-accent hover:underline focus-visible:underline cursor-pointer'

/** The drilled account: its code keys the fetch, its name titles the panel. */
type DrillTarget = { code: string; name: string }

export default function GlPage() {
  const search = routeApi.useSearch()
  const navigate = routeApi.useNavigate()
  // Property comes from the GLOBAL top-bar selector — picked once, app-wide.
  const { property } = useGlobalProperty()
  // A deep-linked ?period= seeds the rail's year so rail and detail agree on
  // arrival; the first four characters are the year by construction — the
  // /gl route's validateSearch (PERIOD_RE in router.tsx) is where that shape
  // is enforced. Otherwise the rail starts on the calendar year containing
  // today, always passed explicitly so the query key names the year actually
  // fetched. (The backend's own default for an omitted fiscal_year lives in
  // gl_api.get_periods; on a non-calendar fiscal calendar the two can label
  // the year differently, and the stepper is the remedy either way.)
  const [fiscalYear, setFiscalYear] = useState(() =>
    search.period !== undefined
      ? parseInt(search.period.slice(0, 4), 10)
      : new Date().getFullYear(),
  )

  const period = search.period

  const [drill, setDrill] = useState<DrillTarget | null>(null)
  // Changing property or period invalidates any open drill window (the
  // SosPage precedent): the drilled entries are scoped to both, and a stale
  // panel over a new selection would show the old one's entries.
  useEffect(() => setDrill(null), [property, period])

  const periodsQuery = useQuery({
    queryKey: ['gl-periods', property, fiscalYear],
    // skipToken disables the query until a property exists.
    queryFn: property === undefined ? skipToken : () => getGlPeriods(property, fiscalYear),
    // Stepping the year keeps the old rail on screen until the new one lands
    // instead of unmounting to the loading line.
    placeholderData: keepPreviousData,
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

  const entriesQuery = useQuery({
    // One query per drill scope: property, period, account.
    queryKey: ['gl-entries', property, period, drill?.code],
    // skipToken until an account is drilled with property and period in hand.
    queryFn:
      property === undefined || period === undefined || drill === null
        ? skipToken
        : () => getJournalEntries(property, period, drill.code),
  })

  function selectPeriod(key: string) {
    // Cleared here, not just in the effect above: synchronous with the click,
    // no committed render pairs the new period with a stale drilled account
    // (SosPage's updateSearch does the same).
    setDrill(null)
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
                min={FISCAL_YEAR_MIN}
                max={FISCAL_YEAR_MAX}
                onChange={(e) => {
                  const year = parseInt(e.target.value, 10)
                  if (year >= FISCAL_YEAR_MIN && year <= FISCAL_YEAR_MAX) setFiscalYear(year)
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
              {tbQuery.data !== undefined && (
                <>
                  <TrialBalanceCard
                    tb={tbQuery.data}
                    onDrill={(code, name) => setDrill({ code, name })}
                  />
                  {/* Derived from the same response as the card above — no
                      second fetch, so the two can never disagree. */}
                  <BalanceSheetCard tb={tbQuery.data} />
                </>
              )}

              {drill !== null && (
                <JournalDrillPanel
                  property={property}
                  period={period}
                  account={drill.code}
                  accountName={drill.name}
                  entries={entriesQuery.data?.entries}
                  error={entriesQuery.isError ? errorMessage(entriesQuery.error) : null}
                  onClose={() => setDrill(null)}
                />
              )}
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

// Titled "net change", not "Balance sheet": these are period-scoped numbers
// (the derivation's own header comment in lib/balanceSheet.ts says why), and
// a bare "Balance sheet" over them would be a false statement of position.
function BalanceSheetCard({ tb }: { tb: TrialBalance }) {
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
            Assets {fmtMoney(bs.foot.totalAssets)} does not equal liabilities and equity{' '}
            {fmtMoney(bs.foot.totalLiabilitiesAndEquity)}
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

function TrialBalanceCard({
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
