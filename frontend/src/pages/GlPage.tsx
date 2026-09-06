// General ledger page: the period rail IS the period picker (selection lives
// in the ?period= search param so a view is linkable and survives reload),
// and the selected period gets a detail card — state, both gap lists, the
// org_admin close/reopen controls — above its trial balance. All fetching
// lives here (TanStack Query keyed on property + search params).

import { useEffect, useState } from 'react'
import {
  keepPreviousData,
  skipToken,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'

import { ApiError, getMe } from '../api/client'
import {
  closeGlPeriod,
  getGlPeriods,
  getJournalEntries,
  getTrialBalance,
  reopenGlPeriod,
} from '../api/gl'
import type { GlPeriod } from '../api/types'
import BalanceSheetCard from '../components/BalanceSheetCard'
import JournalDrillPanel from '../components/JournalDrillPanel'
import TrialBalanceCard from '../components/TrialBalanceCard'
import { Badge, Card, controlClass, PageHeader } from '../components/ui'
import { errorMessage } from '../lib/errors'
import { useGlobalProperty } from '../lib/propertyContext'
import { hasRole } from '../lib/roles'

// getRouteApi avoids the router.tsx <-> GlPage.tsx circular value import.
const routeApi = getRouteApi('/gl')

// The stepper's sane window: wide enough for any books this app will meet,
// tight enough that a half-typed year ("2", "20") never commits to state and
// so never fires a fetch.
const FISCAL_YEAR_MIN = 2000
const FISCAL_YEAR_MAX = 2100

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

  const me = useQuery({ queryKey: ['me'], queryFn: getMe })
  // Mirrors the endpoint's own gate: close and reopen sit behind
  // require_gl_admin = require_grants(ORG_ADMIN) in gl_api.py:29. Offering
  // the controls to anyone else would only produce a 403 on click.
  const canManage = hasRole(me.data, 'org_admin')

  // Derived once at page level: the rail's pressed chip and the detail card
  // both read this, so the two can never disagree about what is selected. A
  // stale ?period= (say, after a property switch) matches nothing here and
  // the detail card simply does not render.
  const selectedPeriod = periodsQuery.data?.find((p) => p.period_key === period)

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
                    aria-pressed={p.period_key === selectedPeriod?.period_key}
                    onClick={() => selectPeriod(p.period_key)}
                    className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium tabular-nums transition-colors ${
                      p.period_key === selectedPeriod?.period_key
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
              {selectedPeriod !== undefined && (
                <PeriodDetailCard
                  // Keyed on both scope halves: confirm/reason/mutation state
                  // (including a held close response) must not survive a
                  // switch to a different period OR to another property that
                  // happens to hold the same period key.
                  key={`${property}:${selectedPeriod.period_key}`}
                  property={property}
                  fiscalYear={fiscalYear}
                  period={selectedPeriod}
                  canManage={canManage}
                />
              )}
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

// The selected period's detail: state, both gap lists, and the close/reopen
// controls. The unposted count always says PMS in the visible copy — the
// check is pms_daily-only by design (PeriodModel.unposted_dates' docstring in
// gl_api.py is where that scope is set), and copy that totals up to "all
// caught up" would claim the payroll side was verified when it was never
// checked. Close confirms in the card itself, never window.confirm (the
// ConnectedActions precedent); reopen requires a reason.
function PeriodDetailCard({
  property,
  fiscalYear,
  period: p,
  canManage,
}: {
  property: string
  fiscalYear: number
  period: GlPeriod
  canManage: boolean
}) {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const [reason, setReason] = useState('')

  // Both verbs change the period's stored state, and the rail chips render
  // from the same query — one invalidation moves the chip and this card
  // together.
  const invalidatePeriods = () =>
    queryClient.invalidateQueries({ queryKey: ['gl-periods', property, fiscalYear] })
  const close = useMutation({
    mutationFn: () => closeGlPeriod(property, p.period_key),
    onSuccess: () => {
      setConfirming(false)
      void invalidatePeriods()
    },
  })
  const reopen = useMutation({
    mutationFn: () => reopenGlPeriod(property, p.period_key, reason),
    onSuccess: () => {
      setReason('')
      // The close response stops describing the period the moment it reopens;
      // dropping it here lets the props below take over again.
      close.reset()
      void invalidatePeriods()
    },
  })

  // The close response is the freshest picture of state and gaps — render it
  // while the invalidated periods query refetches.
  const state = close.data?.state ?? p.state
  const unposted = close.data?.unposted_dates ?? p.unposted_dates
  const orphaned = close.data?.orphaned_dates ?? p.orphaned_dates

  const gapPhrases: string[] = []
  if (unposted.length > 0)
    gapPhrases.push(`${unposted.length} unposted PMS day${unposted.length === 1 ? '' : 's'}`)
  if (orphaned.length > 0)
    gapPhrases.push(`${orphaned.length} orphaned entr${orphaned.length === 1 ? 'y' : 'ies'}`)
  // With no gaps the confirm degrades to the bare question — there is
  // nothing to warn about, only the state change itself.
  const confirmCopy =
    gapPhrases.length === 0
      ? `Close ${p.period_key}?`
      : `Close ${p.period_key} with ${gapPhrases.join(' and ')}?`

  return (
    <Card role="region" aria-label={`Period ${p.period_key}`} className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-ink">Period {p.period_key}</h2>
          {state === 'closed' ? (
            <Badge tone="neutral">Closed</Badge>
          ) : (
            <Badge tone="ok">Open</Badge>
          )}
        </div>
        <p className="text-xs tabular-nums text-ink-muted">
          {p.date_from} – {p.date_to}
        </p>
      </div>

      <div className="space-y-1">
        {unposted.length > 0 ? (
          <>
            <p className="text-sm font-medium text-ink">
              {unposted.length} PMS day{unposted.length === 1 ? '' : 's'} unposted
            </p>
            <p className="text-xs tabular-nums text-ink-muted">{unposted.join(', ')}</p>
          </>
        ) : (
          <p className="text-sm font-medium text-ink">
            {orphaned.length === 0
              ? 'No PMS-day gaps and no orphaned entries.'
              : 'No PMS-day gaps.'}
          </p>
        )}
        {/* Rendered at zero too: an empty unposted list still says nothing
            about payroll accruals. */}
        <p className="text-xs text-ink-muted">
          Days with promoted PMS facts and no posted journal entry. Payroll accruals are not
          checked here — a day with no accrual is the ordinary no-cost case.
        </p>
      </div>

      {orphaned.length > 0 && (
        <div className="space-y-1">
          <p className="text-sm font-medium text-ink">
            {orphaned.length} posted entr{orphaned.length === 1 ? 'y lost its' : 'ies lost their'}{' '}
            facts
          </p>
          <p className="text-xs tabular-nums text-ink-muted">{orphaned.join(', ')}</p>
          <p className="text-xs text-ink-muted">
            Days whose entry's source facts were re-transformed away — both sources. Re-posting
            reverses them.
          </p>
        </div>
      )}

      {canManage && state === 'open' && (
        confirming ? (
          <div className="space-y-2">
            <p className="text-sm text-ink">{confirmCopy}</p>
            <div className="flex gap-2">
              <button
                type="button"
                className={controlClass}
                disabled={close.isPending}
                onClick={() => close.mutate()}
              >
                Yes, close
              </button>
              <button
                type="button"
                className={controlClass}
                disabled={close.isPending}
                onClick={() => setConfirming(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button type="button" className={controlClass} onClick={() => setConfirming(true)}>
            Close {p.period_key}
          </button>
        )
      )}

      {canManage && state === 'closed' && (
        <div className="space-y-2">
          <label className="block text-xs font-medium text-ink-muted" htmlFor="gl-reopen-reason">
            Reason for reopening
          </label>
          <textarea
            id="gl-reopen-reason"
            className={`${controlClass} block w-full max-w-md`}
            rows={2}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button
            type="button"
            className={controlClass}
            disabled={reason.trim() === '' || reopen.isPending}
            onClick={() => reopen.mutate()}
          >
            Reopen {p.period_key}
          </button>
        </div>
      )}

      {close.error !== null && (
        <p className="text-sm text-danger-red">{errorMessage(close.error)}</p>
      )}
      {reopen.error !== null && (
        <p className="text-sm text-danger-red">{errorMessage(reopen.error)}</p>
      )}
    </Card>
  )
}
