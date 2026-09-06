// General ledger page: the period rail IS the period picker (selection lives
// in the ?period= search param so a view is linkable and survives reload),
// and the selected period gets a detail card — state, both gap lists, the
// org_admin close/reopen controls — above its trial balance. All fetching
// lives here (TanStack Query keyed on property + search params).

import { useEffect, useState } from 'react'
import { keepPreviousData, skipToken, useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'

import { ApiError, getMe } from '../api/client'
import { getGlPeriods, getJournalEntries, getTrialBalance } from '../api/gl'
import BalanceSheetCard from '../components/BalanceSheetCard'
import JournalDrillPanel from '../components/JournalDrillPanel'
import PeriodDetailCard from '../components/PeriodDetailCard'
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
  // The input's own text, so every keystroke stays visible: a half-typed "2"
  // lands here unconditionally, and only a draft that round-trips to an
  // in-range year is promoted to fiscalYear (the query key never sees junk).
  const [yearDraft, setYearDraft] = useState(() => String(fiscalYear))

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
                value={yearDraft}
                min={FISCAL_YEAR_MIN}
                max={FISCAL_YEAR_MAX}
                onChange={(e) => {
                  const draft = e.target.value
                  setYearDraft(draft)
                  const year = parseInt(draft, 10)
                  if (String(year) === draft && year >= FISCAL_YEAR_MIN && year <= FISCAL_YEAR_MAX)
                    setFiscalYear(year)
                }}
                // A draft abandoned invalid or half-typed snaps back to the
                // year the rail is actually showing.
                onBlur={() => setYearDraft(String(fiscalYear))}
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
