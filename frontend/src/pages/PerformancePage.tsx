import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { Badge, Card, PageHeader, controlClass } from '../components/ui'
import { getPerformance } from '../api/client'
import type { CoreMetrics, PerformanceResponse } from '../api/types'
import { useGlobalProperty } from '../lib/propertyContext'
import { errorMessage } from '../lib/errors'

/**
 * Core performance statistics (issue #9): the property's occupancy / ADR /
 * RevPAR / TRevPAR over a window, each against its prior period and prior year,
 * with the PMS-KPI reconciliation surfaced when our computed figure and the
 * ingested statistic diverge. Reads consume the E1 `getPerformance` client and
 * are property-access gated server-side; the active property comes from the
 * GLOBAL top-bar selector, same as every other page.
 *
 * Decimals arrive as EXACT strings and a `null` figure is WITHHELD (e.g.
 * occupancy with no rooms available) — never zero. We render withheld figures
 * as "—" and format occupancy as a percentage, ADR/RevPAR/TRevPAR as currency.
 * Labor productivity (issue #9) rides on the same response: labor hours and
 * cost per occupied room, with cost withheld (a "cost withheld" note) when the
 * single-employee suppression fires (`labor.cost_suppressed`).
 */

// The four core KPIs, keyed by the metric name the delta/recon maps use.
const KPIS: { key: keyof CoreMetrics; label: string; kind: 'pct' | 'money' }[] = [
  { key: 'occupancy', label: 'Occupancy', kind: 'pct' },
  { key: 'adr', label: 'ADR', kind: 'money' },
  { key: 'revpar', label: 'RevPAR', kind: 'money' },
  { key: 'trevpar', label: 'TRevPAR', kind: 'money' },
]

function formatValue(value: string | null, kind: 'pct' | 'money'): string {
  if (value === null) return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return '—'
  return kind === 'pct' ? `${(n * 100).toFixed(1)}%` : `$${n.toFixed(2)}`
}

function formatHours(value: string | null): string {
  if (value === null) return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return '—'
  return n.toFixed(2)
}

function formatDelta(value: string | null | undefined): { text: string; positive: boolean | null } {
  if (value === null || value === undefined) return { text: '—', positive: null }
  const n = Number(value)
  if (Number.isNaN(n)) return { text: '—', positive: null }
  const sign = n > 0 ? '+' : ''
  return { text: `${sign}${n.toFixed(1)}%`, positive: n === 0 ? null : n > 0 }
}

export default function PerformancePage() {
  const { property, selected } = useGlobalProperty()
  // The window is seeded from the property's OWN data range rather than a
  // today-relative month-to-date: the demo (and any property whose feed has
  // lapsed) has no data in the current month, so a today-anchored default
  // lands on an empty window. Default to the last month of available data —
  // `to` = last_date, `from` = the first of that month clamped to first_date.
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  // Re-default only when the property changes, so a user's manual date edits
  // survive re-renders (react-query may hand back a fresh `selected` object
  // with the same id on refetch).
  const defaultedFor = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (selected === undefined || defaultedFor.current === selected.property_id) return
    defaultedFor.current = selected.property_id
    const monthStart = `${selected.last_date.slice(0, 7)}-01`
    setTo(selected.last_date)
    setFrom(selected.first_date > monthStart ? selected.first_date : monthStart)
  }, [selected])

  const perf = useQuery({
    queryKey: ['performance', property, from, to],
    queryFn: () => getPerformance(property!, { from, to }),
    enabled: property !== undefined && from !== '' && to !== '',
  })

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Performance"
        subtitle="Occupancy, ADR, RevPAR, and TRevPAR for the window — each against its prior period and prior year, with the PMS reconciliation."
      />

      <Card role="region" aria-label="performance window">
        <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => e.preventDefault()}>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">From</span>
            <input
              type="date"
              className={controlClass}
              value={from}
              aria-label="From"
              onChange={(e) => setFrom(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">To</span>
            <input
              type="date"
              className={controlClass}
              value={to}
              aria-label="To"
              onChange={(e) => setTo(e.target.value)}
            />
          </label>
          {selected !== undefined && (
            <p className="pb-1.5 text-xs text-ink-muted">
              Data available {selected.first_date} – {selected.last_date}
            </p>
          )}
        </form>
      </Card>

      {property === undefined && (
        <Card>
          <p className="text-sm text-ink-muted">No property selected yet.</p>
        </Card>
      )}

      {perf.isError && (
        <Card>
          <p className="text-sm text-danger-red">Failed to load: {errorMessage(perf.error)}</p>
        </Card>
      )}

      {property !== undefined && perf.isPending && (
        <Card>
          <p className="text-sm text-ink-muted">Loading …</p>
        </Card>
      )}

      {property !== undefined && perf.data && <PerformanceBody data={perf.data} />}
    </div>
  )
}

function PerformanceBody({ data }: { data: PerformanceResponse }) {
  const divergent = Object.entries(data.reconciliation).filter(
    ([, line]) => line.agrees === false,
  )

  return (
    <>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {KPIS.map((kpi) => (
          <KpiCard
            key={kpi.key}
            label={kpi.label}
            value={formatValue(data.current[kpi.key], kpi.kind)}
            priorPeriod={data.prior_period_delta_pct[kpi.key]}
            priorYear={data.prior_year_delta_pct[kpi.key]}
          />
        ))}
      </div>

      <Card role="region" aria-label="window details">
        <h2 className="mb-3 text-sm font-semibold text-ink">Window</h2>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-xs font-medium text-ink-muted">Range</dt>
            <dd className="text-ink">
              {data.start} – {data.end}
            </dd>
          </div>
          {data.period !== null && (
            <div>
              <dt className="text-xs font-medium text-ink-muted">Fiscal period</dt>
              <dd className="text-ink">{data.period}</dd>
            </div>
          )}
          <div>
            <dt className="text-xs font-medium text-ink-muted">ADR room basis</dt>
            <dd className="text-ink">{data.adr_room_basis}</dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-ink-muted">Days excluded</dt>
            <dd className="text-ink">{data.days_excluded}</dd>
          </div>
        </dl>
      </Card>

      <Card role="region" aria-label="labor productivity">
        <h2 className="mb-3 text-sm font-semibold text-ink">Labor productivity</h2>
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs font-medium text-ink-muted">Labor hours per occupied room</dt>
            <dd className="text-2xl font-semibold text-ink">
              {formatHours(data.labor.hours_per_occupied_room)}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-ink-muted">Labor cost per occupied room</dt>
            {data.labor.cost_suppressed ? (
              <dd className="flex items-center gap-2">
                <span className="text-2xl font-semibold text-ink-muted">—</span>
                <Badge tone="neutral">Cost withheld</Badge>
              </dd>
            ) : (
              <dd className="text-2xl font-semibold text-ink">
                {formatValue(data.labor.cost_per_occupied_room, 'money')}
              </dd>
            )}
          </div>
        </dl>
        {data.labor.cost_suppressed && (
          <p className="mt-3 text-xs text-ink-muted">
            Labor cost is withheld for this window: a contributing day had too few priced
            employees to show cost without disclosing an individual&apos;s pay rate. Hours are
            always shown.
          </p>
        )}
      </Card>

      <Card role="region" aria-label="reconciliation">
        <h2 className="mb-3 text-sm font-semibold text-ink">PMS reconciliation</h2>
        {divergent.length > 0 ? (
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Badge tone="danger">Divergence</Badge>
            <span className="text-sm text-ink-muted">
              Computed and ingested figures disagree on {divergent.map(([m]) => m).join(', ')}.
            </span>
          </div>
        ) : (
          <p className="mb-3 text-sm text-ink-muted">
            Computed figures agree with the ingested PMS statistics.
          </p>
        )}
        <ul className="flex flex-col gap-1 text-sm">
          {Object.entries(data.reconciliation).map(([metric, line]) => (
            <li key={metric} className="flex items-center gap-2">
              <span className="w-24 capitalize text-ink-muted">{metric}</span>
              <span className="text-ink">
                computed {line.computed ?? '—'} vs ingested {line.ingested ?? '—'}
              </span>
              {line.agrees === false && (
                <span className="text-xs font-semibold text-danger-red">does not reconcile</span>
              )}
            </li>
          ))}
        </ul>
      </Card>
    </>
  )
}

function KpiCard({
  label,
  value,
  priorPeriod,
  priorYear,
}: {
  label: string
  value: string
  priorPeriod: string | null | undefined
  priorYear: string | null | undefined
}) {
  const pp = formatDelta(priorPeriod)
  const py = formatDelta(priorYear)
  const deltaClass = (positive: boolean | null) =>
    positive === null
      ? 'text-ink-muted'
      : positive
        ? 'text-ok-green'
        : 'text-danger-red'

  return (
    <Card role="region" aria-label={`${label} KPI`}>
      <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">{label}</p>
      <p className="mt-1 text-2xl font-semibold text-ink">{value}</p>
      <dl className="mt-3 flex flex-col gap-1 text-xs">
        <div className="flex items-center justify-between">
          <dt className="text-ink-muted">vs prior period</dt>
          <dd className={deltaClass(pp.positive)}>{pp.text}</dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-ink-muted">vs prior year</dt>
          <dd className={deltaClass(py.positive)}>{py.text}</dd>
        </div>
      </dl>
    </Card>
  )
}
