// Payroll & labor analytics — the staffing decision view.
//
// WHAT THIS PAGE IS FOR. The Summary Operating Statement answers "what did
// labor cost". This answers "was that the right amount, and where do I change
// it next week". So every number is expressed against DEMAND (rooms sold) or
// against REVENUE, not in isolation: a $95k month means nothing until you know
// it moved 2,000 rooms and 27% of the top line.
//
// THE MONEY RULE, CARRIED. Everything here is a department aggregate with the
// statement's own suppression (reporting._discloses). Two consequences the UI
// must state rather than hide:
//   * A department with fewer than two priced employees on a day that carried
//     cost shows hours but no money — the table says "hidden", never 0.
//   * The disclosed daily total is therefore <= the true total. The KPI says
//     "disclosed", and the shortfall is named under the department table.
// Hours are never suppressed, which is why the department chart is ranked and
// drawn on hours: it is the one view where nobody is missing.
//
// NO DUAL AXES. Labor cost and revenue live on different scales, so they are
// never plotted together — the chart is their RATIO, which is the number a GM
// is actually judged on.

import { useMemo, useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { Badge, Card, sectionHeadClass } from '../components/ui'
import BarGradients from '../components/BarGradients'
import { barFill, barRampCss, barWidth, topRoundedBar } from '../lib/chartBars'
import { getLaborAnalytics } from '../api/client'
import type { LaborAnalytics, LaborDay, LaborDepartment } from '../api/types'
import { useGlobalProperty } from '../lib/propertyContext'
import { errorMessage } from '../lib/errors'
import {
  AlertIcon,
  BanknoteIcon,
  ClockIcon,
  PeopleIcon,
  TrendUpIcon,
} from '../components/icons'

// --- formatting ---------------------------------------------------------------

const money0 = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 0,
})
const money2 = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
})
const num1 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 })

const n = (v: string | null | undefined): number => (v === null || v === undefined ? 0 : Number(v))
const hrs = (v: number): string => `${num1.format(v)} h`
const pct = (v: number): string => `${num1.format(v)}%`

function iso(d: Date): string {
  const p = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

type Window = { from: string; to: string; label: string }

export type RangeKey = 'mtd' | 'last-month' | 'd30' | 'd90'

const RANGES: { key: RangeKey; label: string }[] = [
  { key: 'mtd', label: 'This month' },
  { key: 'last-month', label: 'Last month' },
  { key: 'd30', label: '30 days' },
  { key: 'd90', label: '90 days' },
]

function addDays(d: Date, days: number): Date {
  const out = new Date(d)
  out.setDate(out.getDate() + days)
  return out
}

/**
 * The window being read, and the one it is compared against.
 *
 * The prior window is always the SAME LENGTH, immediately before — for
 * month-to-date that means the same span of the previous month, never the whole
 * of it. Holding two days against a full month prints a fall that measures how
 * early in the month it is and nothing else.
 */
function windows(now: Date, range: RangeKey): { current: Window; prior: Window } {
  const y = now.getFullYear()
  const m = now.getMonth()
  const monthName = (d: Date) => d.toLocaleString('en-US', { month: 'long' })
  const span = (a: Date, b: Date) =>
    `${a.toLocaleString('en-US', { month: 'short', day: 'numeric' })}–${b.getDate()}`

  if (range === 'mtd') {
    const priorLast = new Date(y, m, 0).getDate()
    const priorTo = new Date(y, m - 1, Math.min(now.getDate(), priorLast))
    const priorFrom = new Date(y, m - 1, 1)
    return {
      current: { from: iso(new Date(y, m, 1)), to: iso(now), label: monthName(now) },
      prior: { from: iso(priorFrom), to: iso(priorTo), label: span(priorFrom, priorTo) },
    }
  }
  if (range === 'last-month') {
    const from = new Date(y, m - 1, 1)
    const to = new Date(y, m, 0)
    const pFrom = new Date(y, m - 2, 1)
    const pTo = new Date(y, m - 1, 0)
    return {
      current: { from: iso(from), to: iso(to), label: monthName(from) },
      prior: { from: iso(pFrom), to: iso(pTo), label: monthName(pFrom) },
    }
  }
  const length = range === 'd30' ? 30 : 90
  const from = addDays(now, -(length - 1))
  const pTo = addDays(from, -1)
  const pFrom = addDays(pTo, -(length - 1))
  return {
    current: { from: iso(from), to: iso(now), label: `last ${length} days` },
    prior: { from: iso(pFrom), to: iso(pTo), label: `the ${length} before` },
  }
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function weekdayIndex(isoDate: string): number {
  // Monday-first, so the week reads the way a schedule does.
  return (new Date(`${isoDate}T00:00:00`).getDay() + 6) % 7
}

// --- derived measures ---------------------------------------------------------

type Measures = {
  hours: number
  ot: number
  cost: number
  revenue: number
  rooms: number
  /** Labor cost as a share of operating revenue — THE number management reads. */
  costPctRevenue: number
  /** Cost per occupied room: the productivity figure that survives a soft month. */
  cpor: number
  /** Hours per occupied room. Hotel labor's oldest and best yardstick. */
  hpor: number
  otShare: number
}

function measure(a: LaborAnalytics | undefined): Measures {
  const days = a?.days ?? []
  const hours = days.reduce((s, d) => s + n(d.hours), 0)
  const ot = days.reduce((s, d) => s + n(d.ot_hours), 0)
  const cost = days.reduce((s, d) => s + n(d.est_cost), 0)
  const revenue = days.reduce((s, d) => s + n(d.revenue), 0)
  const rooms = days.reduce((s, d) => s + n(d.rooms_occupied), 0)
  return {
    hours, ot, cost, revenue, rooms,
    costPctRevenue: revenue === 0 ? 0 : (cost / revenue) * 100,
    cpor: rooms === 0 ? 0 : cost / rooms,
    hpor: rooms === 0 ? 0 : hours / rooms,
    otShare: hours === 0 ? 0 : (ot / hours) * 100,
  }
}

// --- chart primitives ---------------------------------------------------------

const CAT = [
  'var(--color-cat-1)', 'var(--color-cat-2)', 'var(--color-cat-3)',
  'var(--color-cat-4)', 'var(--color-cat-5)', 'var(--color-cat-6)',
]
const CAT_OTHER = 'var(--color-cat-other)'

/** Assigned in fixed order and never cycled: a seventh department folds into
 *  "Other" rather than reusing slot 1, so a colour always means one thing. */
function seriesColor(index: number): string {
  return CAT[index] ?? CAT_OTHER
}

function ChartFrame({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  return (
    <Card className="flex flex-col">
      <div className="mb-1">
        <h2 className={sectionHeadClass}>{title}</h2>
        <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p>
      </div>
      <div className="mt-2">{children}</div>
    </Card>
  )
}

/**
 * Daily bars for one measure, with a dashed reference line at the window
 * average. One series, so no legend — the title names it. Every bar carries a
 * <title> so hovering reads the exact figure; there is no separate tooltip
 * layer to get stuck open.
 */
function DailyBars({
  points,
  format,
  average,
  averageLabel,
  colorOf,
  ariaLabel,
  gradientPrefix,
}: {
  points: { date: string; value: number }[]
  format: (v: number) => string
  average: number
  averageLabel: string
  colorOf?: (p: { date: string; value: number }) => string
  ariaLabel: string
  /** Namespaces this chart's gradient ids — see BarGradients. */
  gradientPrefix: string
}) {
  const W = 720
  const H = 200
  const PAD = { top: 12, right: 10, bottom: 24, left: 52 }
  const innerW = W - PAD.left - PAD.right
  const innerH = H - PAD.top - PAD.bottom
  const max = Math.max(1, ...points.map((p) => p.value), average)
  const top = max * 1.15
  const step = points.length === 0 ? innerW : innerW / points.length
  const barW = barWidth(step)
  const y = (v: number) => PAD.top + innerH * (1 - v / top)
  const colorAt = (p: { date: string; value: number }) => colorOf?.(p) ?? 'var(--color-cat-1)'

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={ariaLabel}>
      <BarGradients prefix={gradientPrefix} colors={points.map(colorAt)} />
      {[top / 2, top].map((v) => (
        <g key={v}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(v)} y2={y(v)} className="stroke-line" strokeWidth="1" />
          <text x={PAD.left - 6} y={y(v) + 3.5} textAnchor="end" className="fill-ink-faint text-[10px] tabular-nums">
            {format(v)}
          </text>
        </g>
      ))}
      <line x1={PAD.left} x2={W - PAD.right} y1={PAD.top + innerH} y2={PAD.top + innerH} className="stroke-line-strong" strokeWidth="1" />
      {average > 0 && (
        <>
          <line
            x1={PAD.left} x2={W - PAD.right} y1={y(average)} y2={y(average)}
            className="stroke-ink-faint" strokeWidth="1.5" strokeDasharray="5 4"
          />
          <text x={W - PAD.right} y={y(average) - 5} textAnchor="end" className="fill-ink-muted text-[10px]">
            {averageLabel}
          </text>
        </>
      )}
      {points.map((p, i) => {
        const cx = PAD.left + i * step + step / 2
        const h = Math.max(0, PAD.top + innerH - y(p.value))
        return (
          <g key={p.date}>
            <path
              d={topRoundedBar(cx - barW / 2, y(p.value), barW, h)}
              fill={barFill(gradientPrefix, colorAt(p))}
            >
              <title>{`${p.date}: ${format(p.value)}`}</title>
            </path>
            {(i === 0 || i === points.length - 1 || i % 7 === 0) && (
              <text x={cx} y={H - 8} textAnchor="middle" className="fill-ink-faint text-[9px] tabular-nums">
                {p.date.slice(5)}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

/**
 * Departments ranked by hours, each with the daily shape beside it.
 *
 * This replaced a stacked column, which was the wrong form twice over: only
 * the bottom series sat on a common baseline, so no other department's trend
 * could be read at all, and the question a GM actually asks — who is consuming
 * the hours, and is it growing — needs a RANKING, which a stack never gives.
 *
 * The sparkline is real per-day department hours from the API. The previous
 * chart apportioned each day's total by the department's share of the window,
 * which drew every department with the same shape and called it a trend.
 */
function RankedDepartments({
  rows,
  ariaLabel,
}: {
  rows: {
    name: string
    color: string
    hours: number
    ot: number
    /** Hours on each day of the window, in date order. */
    daily: number[]
  }[]
  ariaLabel: string
}) {
  const max = Math.max(1, ...rows.map((r) => r.hours))
  return (
    <div role="img" aria-label={ariaLabel} className="flex flex-col">
      {rows.map((r) => (
        <div
          key={r.name}
          className="grid grid-cols-[9.5rem_minmax(0,1fr)_5rem_5.5rem_4.5rem] items-center gap-3 border-b border-line py-2.5 text-xs last:border-0"
        >
          <span className="flex min-w-0 items-center gap-2">
            <span
              aria-hidden="true"
              className="inline-block size-2.5 shrink-0 rounded-[3px]"
              style={{ background: r.color }}
            />
            <span className="truncate font-medium text-ink" title={r.name}>{r.name}</span>
          </span>
          <span className="h-7 min-w-0 overflow-hidden rounded-lg bg-surface-sunken">
            <span
              className="block h-full rounded-lg"
              style={{ width: `${Math.max(1.5, (r.hours / max) * 100)}%`, background: barRampCss(r.color) }}
              title={hrs(r.hours)}
            />
          </span>
          <span className="text-right font-semibold tabular-nums text-ink">{hrs(r.hours)}</span>
          <Sparkline values={r.daily} color={r.color} label={`${r.name} daily hours`} />
          <span
            className={`text-right tabular-nums ${r.ot > 0 ? 'text-warn-amber' : 'text-ink-faint'}`}
          >
            {r.ot > 0 ? `${hrs(r.ot)} OT` : '—'}
          </span>
        </div>
      ))}
    </div>
  )
}

/**
 * The daily shape of one department, on its OWN scale.
 *
 * Per-row scaling is the right call here because the bar beside it already
 * carries magnitude — the line only has to answer "steady, climbing, or
 * spiky". A shared scale would flatten every small department into a
 * straight line and say nothing. The row's aria-label names it as a shape.
 */
function Sparkline({ values, color, label }: { values: number[]; color: string; label: string }) {
  const W = 72
  const H = 22
  const max = Math.max(1, ...values)
  if (values.length < 2) return <span className="text-ink-faint">—</span>
  const step = W / (values.length - 1)
  const path = values
    .map((v, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(1)},${(H - 2 - (v / max) * (H - 4)).toFixed(1)}`)
    .join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={label}>
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

/** Horizontal bars — used for the weekday overtime profile, where the label
 *  matters more than precise length comparison. */
function RowBars({
  rows,
  format,
  ariaLabel,
}: {
  rows: { label: string; value: number; tone?: string }[]
  format: (v: number) => string
  ariaLabel: string
}) {
  const max = Math.max(1, ...rows.map((r) => r.value))
  return (
    <div role="img" aria-label={ariaLabel} className="flex flex-col gap-1.5">
      {rows.map((r) => (
        <div key={r.label} className="flex items-center gap-3 text-xs">
          <span className="w-16 shrink-0 text-ink-muted">{r.label}</span>
          <span className="h-7 min-w-0 flex-1 overflow-hidden rounded-lg bg-surface-sunken">
            <span
              className="block h-full rounded-lg"
              style={{
                width: `${(r.value / max) * 100}%`,
                background: barRampCss(r.tone ?? 'var(--color-cat-1)'),
              }}
            />
          </span>
          <span className="w-16 shrink-0 text-right tabular-nums text-ink">{format(r.value)}</span>
        </div>
      ))}
    </div>
  )
}

// --- KPI ----------------------------------------------------------------------

function Kpi({
  label,
  value,
  hint,
  current,
  prior,
  good,
  format,
  icon,
  tone,
}: {
  label: string
  value: string
  hint: string
  current: number
  prior: number
  good: 'up' | 'down' | 'none'
  format: (v: number) => string
  icon: React.ReactNode
  tone: string
}) {
  const change = current - prior
  const rel = prior === 0 ? null : (change / prior) * 100
  const rising = change > 0
  const flat = rel !== null && Math.abs(rel) < 0.05
  const welcome = good === 'none' ? null : rising ? good === 'up' : good === 'down'
  const deltaTone = flat || welcome === null
    ? 'text-ink-muted'
    : welcome ? 'text-ok-green' : 'text-warn-amber'
  return (
    <Card className="flex items-start gap-3.5">
      <span className={`grid size-11 shrink-0 place-items-center rounded-xl ${tone}`}>{icon}</span>
      <div className="min-w-0">
        <p className={sectionHeadClass}>{label}</p>
        <p className="mt-0.5 text-[26px] font-bold leading-tight tabular-nums tracking-tight text-ink">
          {value}
        </p>
        <p className={`mt-1 text-xs font-medium ${deltaTone}`}>
          {prior === 0 ? (
            current === 0 ? 'nothing yet' : <><span aria-hidden="true">▲</span> {format(current)} from none</>
          ) : (
            <>
              <span aria-hidden="true">{flat ? '→' : rising ? '▲' : '▼'}</span>{' '}
              {flat ? 'level' : `${num1.format(Math.abs(rel ?? 0))}%`}{' '}
              <span className="text-ink-muted">
                ({rising ? '+' : '−'}{format(Math.abs(change))})
              </span>
            </>
          )}
        </p>
        <p className="mt-0.5 text-xs text-ink-faint">{hint}</p>
      </div>
    </Card>
  )
}

// --- page ---------------------------------------------------------------------

export default function PayrollDashboardPage() {
  const { property, selected } = useGlobalProperty()
  const [now] = useState(() => new Date())
  const [range, setRange] = useState<RangeKey>('mtd')
  const w = useMemo(() => windows(now, range), [now, range])

  const queries = useQueries({
    queries: [w.current, w.prior].map((win) => ({
      queryKey: ['labor-analytics', property, win.from, win.to],
      queryFn: () => getLaborAnalytics({ property: property!, from: win.from, to: win.to }),
      enabled: property !== undefined,
    })),
  })
  const current = queries[0]!
  const prior = queries[1]!

  const a = current.data
  const cur = measure(a)
  const pri = measure(prior.data)

  // Memoised off `a` so the derived arrays are referentially stable — the
  // stacked-series useMemo below depends on them.
  const days: LaborDay[] = useMemo(() => a?.days ?? [], [a])
  const departments: LaborDepartment[] = useMemo(() => a?.departments ?? [], [a])

  // Top six by hours keep their own colour; the tail folds into Other so a hue
  // never has to mean two things.
  const { ranked, named, tail } = useMemo(() => {
    const r = [...departments].sort((x, y) => n(y.hours) - n(x.hours))
    return { ranked: r, named: r.slice(0, 6), tail: r.slice(6) }
  }, [departments])

  // One row per department, ranked by hours, each carrying its REAL daily
  // series from `day.department_hours`. A department absent from a day worked
  // no hours that day, which is a zero in the shape — not a gap.
  const rankedDepartments = useMemo(() => {
    const dailyFor = (pick: (d: LaborDay) => number) => days.map(pick)
    const rows = named.map((dept, i) => ({
      name: dept.department,
      color: seriesColor(i),
      hours: n(dept.hours),
      ot: n(dept.ot_hours),
      // Optional read: during a rollout a new bundle can meet an API that
      // predates the field, and a missing daily series must flatten the line,
      // not blank the page.
      daily: dailyFor((d) => n(d.department_hours?.[dept.department])),
    }))
    if (tail.length === 0) return rows
    // The tail folds into one row rather than reusing a hue, exactly as the
    // stack did: a colour must never mean two things.
    const tailNames = tail.map((d) => d.department)
    return [
      ...rows,
      {
        name: `Other (${tail.length})`,
        color: CAT_OTHER,
        hours: tail.reduce((s, d) => s + n(d.hours), 0),
        ot: tail.reduce((s, d) => s + n(d.ot_hours), 0),
        daily: dailyFor((d) =>
          tailNames.reduce((s, name) => s + n(d.department_hours?.[name]), 0),
        ),
      },
    ]
  }, [days, named, tail])

  const otByWeekday = useMemo(() => {
    const sums = new Array<number>(7).fill(0)
    for (const d of days) sums[weekdayIndex(d.business_date)]! += n(d.ot_hours)
    const worst = Math.max(...sums, 0)
    return WEEKDAYS.map((label, i) => ({
      label,
      value: sums[i] ?? 0,
      tone: (sums[i] ?? 0) === worst && worst > 0 ? 'var(--color-cat-2)' : 'var(--color-cat-1)',
    }))
  }, [days])

  const costPctPoints = days.map((d) => ({
    date: d.business_date,
    value: n(d.revenue) === 0 ? 0 : (n(d.est_cost) / n(d.revenue)) * 100,
  }))
  const cporPoints = days.map((d) => ({
    date: d.business_date,
    value: n(d.rooms_occupied) === 0 ? 0 : n(d.est_cost) / n(d.rooms_occupied),
  }))

  const hoursTotal = departments.reduce((s, d) => s + n(d.hours), 0)
  const hiddenCost = departments.filter((d) => d.est_cost === null).length

  if (property === undefined) {
    return (
      <div className="flex flex-col gap-5">
        <Header window={w.current} propertyName={null} range={range} onRange={setRange} />
        <Card>
          <p className="text-sm text-ink-muted">Pick a property in the top bar to see its labor analytics.</p>
        </Card>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-5">
      <Header
        window={w.current}
        propertyName={selected?.property_id ?? property}
        range={range}
        onRange={setRange}
      />

      {current.isError && (
        <Card><p className="text-sm text-danger-red">Failed to load: {errorMessage(current.error)}</p></Card>
      )}
      {current.isPending && <Card><p className="text-sm text-ink-muted">Loading labor analytics…</p></Card>}

      {a !== undefined && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Kpi
              label={`Labor cost · ${w.current.label}`}
              value={money0.format(cur.cost)}
              hint={`disclosed · vs ${w.prior.label}`}
              current={cur.cost} prior={pri.cost} good="down"
              format={(v) => money0.format(v)}
              icon={<BanknoteIcon width={20} height={20} />}
              tone="bg-ok-green-soft text-ok-green"
            />
            <Kpi
              label="Labor % of revenue"
              value={pct(cur.costPctRevenue)}
              hint={`${money0.format(cur.revenue)} operating revenue`}
              current={cur.costPctRevenue} prior={pri.costPctRevenue} good="down"
              format={(v) => `${num1.format(v)} pts`}
              icon={<TrendUpIcon width={20} height={20} />}
              tone="bg-accent-soft text-accent-ink"
            />
            <Kpi
              label="Cost per occupied room"
              value={money2.format(cur.cpor)}
              hint={`${num1.format(cur.hpor)} hours per room · ${num1.format(cur.rooms)} rooms sold`}
              current={cur.cpor} prior={pri.cpor} good="down"
              format={(v) => money2.format(v)}
              icon={<PeopleIcon width={20} height={20} />}
              tone="bg-info-blue-soft text-info-blue"
            />
            <Kpi
              label="Overtime"
              value={hrs(cur.ot)}
              hint={`${pct(cur.otShare)} of all hours · FTE ${a.fte ?? '—'}`}
              current={cur.ot} prior={pri.ot} good="down"
              format={hrs}
              icon={cur.ot > 0 ? <AlertIcon width={20} height={20} /> : <ClockIcon width={20} height={20} />}
              tone="bg-warn-amber-soft text-warn-amber"
            />
          </div>

          <div className="grid gap-5 xl:grid-cols-2">
            <ChartFrame
              title={`Labor as % of revenue · ${w.current.label}`}
              subtitle="The number management is judged on. Bars above the dashed month average are the days that moved it."
            >
              <DailyBars
                points={costPctPoints}
                format={(v) => `${num1.format(v)}%`}
                average={cur.costPctRevenue}
                averageLabel={`month ${pct(cur.costPctRevenue)}`}
                // Over the line is hotter, under is the calm brand indigo —
                // one family, two intensities. The dashed reference line and
                // its label carry the threshold too, so the split is never
                // colour alone.
                colorOf={(p) => (p.value > cur.costPctRevenue ? 'var(--color-cat-2)' : 'var(--color-cat-1)')}
                ariaLabel="Daily labor cost as a percentage of operating revenue"
                gradientPrefix="costpct"
              />
            </ChartFrame>

            <ChartFrame
              title={`Cost per occupied room · ${w.current.label}`}
              subtitle="Productivity that survives a soft month: a quiet day should cost less, not the same."
            >
              <DailyBars
                points={cporPoints}
                format={(v) => money0.format(v)}
                average={cur.cpor}
                averageLabel={`month ${money2.format(cur.cpor)}`}
                ariaLabel="Daily labor cost per occupied room"
                gradientPrefix="cpor"
              />
            </ChartFrame>

            <ChartFrame
              title="Hours by department"
              subtitle="Ranked by hours worked — never cost, which is suppressed for small departments. The line is that department's own daily shape."
            >
              <RankedDepartments
                rows={rankedDepartments}
                ariaLabel="Departments ranked by hours worked, with each department's daily hours"
              />
            </ChartFrame>

            <ChartFrame
              title="Where overtime happens"
              subtitle="Overtime hours by day of week. A spike on one weekday is a rota problem, not a payroll one."
            >
              <RowBars rows={otByWeekday} format={hrs} ariaLabel="Overtime hours by day of week" />
              <p className="mt-3 text-xs text-ink-muted">
                California pays overtime past 8 hours in a day, past 40 in a week, and on a
                seventh consecutive day — so a cluster late in the week usually means someone is
                working six or seven days, not long ones.
              </p>
            </ChartFrame>
          </div>

          {/* The table view. Required relief for the chart palette, and the
              place where a suppressed department is stated rather than skipped. */}
          <Card role="region" aria-label="department labor" className="p-0">
            <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line px-5 py-3">
              <h2 className={sectionHeadClass}>Department detail · {w.current.label}</h2>
              <span className="text-xs text-ink-muted">
                target hours come from each department&apos;s labor standard, applied to rooms
                actually sold
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[46rem] text-sm" aria-label="Department labor">
                <thead>
                  <tr className="border-b border-line bg-surface-sunken/60">
                    <th className="px-5 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wide text-ink-muted">Department</th>
                    <th className="px-5 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wide text-ink-muted">Hours</th>
                    <th className="px-5 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wide text-ink-muted">Share</th>
                    <th className="px-5 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wide text-ink-muted">OT</th>
                    <th className="px-5 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wide text-ink-muted">Target</th>
                    <th className="px-5 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wide text-ink-muted">Variance</th>
                    <th className="px-5 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wide text-ink-muted">Est cost</th>
                  </tr>
                </thead>
                <tbody>
                  {ranked.map((d) => {
                    const h = n(d.hours)
                    const target = d.target_hours === null ? null : n(d.target_hours)
                    const variance = target === null ? null : h - target
                    const colorIdx = named.findIndex((x) => x.department === d.department)
                    return (
                      <tr key={d.department} className="border-b border-line last:border-0 hover:bg-surface-sunken/50">
                        <td className="px-5 py-3">
                          <span className="flex items-center gap-2.5">
                            <span
                              aria-hidden="true"
                              className="inline-block size-2.5 shrink-0 rounded-[3px]"
                              style={{ background: colorIdx >= 0 ? seriesColor(colorIdx) : CAT_OTHER }}
                            />
                            <span className="font-medium text-ink">{d.department}</span>
                          </span>
                        </td>
                        <td className="px-5 py-3 text-right tabular-nums">{hrs(h)}</td>
                        <td className="px-5 py-3 text-right tabular-nums text-ink-muted">
                          {hoursTotal === 0 ? '—' : pct((h / hoursTotal) * 100)}
                        </td>
                        <td className={`px-5 py-3 text-right tabular-nums ${n(d.ot_hours) > 0 ? 'text-warn-amber' : 'text-ink-faint'}`}>
                          {n(d.ot_hours) > 0 ? hrs(n(d.ot_hours)) : '—'}
                        </td>
                        <td className="px-5 py-3 text-right tabular-nums text-ink-muted">
                          {target === null ? <span className="text-ink-faint">no standard</span> : hrs(target)}
                        </td>
                        <td className="px-5 py-3 text-right tabular-nums">
                          {variance === null ? (
                            <span className="text-ink-faint">—</span>
                          ) : (
                            <span className={variance > 0 ? 'font-medium text-warn-amber' : 'font-medium text-ok-green'}>
                              {variance > 0 ? '+' : '−'}{hrs(Math.abs(variance))}
                            </span>
                          )}
                        </td>
                        <td className="px-5 py-3 text-right tabular-nums">
                          {d.est_cost === null ? (
                            <Badge tone="neutral">hidden</Badge>
                          ) : (
                            <span className="font-medium text-ink">{money0.format(n(d.est_cost))}</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <div className="flex flex-col gap-1 border-t border-line px-5 py-3 text-xs text-ink-muted">
              {hiddenCost > 0 && (
                <p>
                  Cost is hidden for {hiddenCost} department{hiddenCost === 1 ? '' : 's'} that had
                  fewer than two priced employees on a day carrying cost — cost divided by hours
                  would re-derive one person&apos;s pay rate. Their hours are shown in full, and
                  the headline figure excludes their money, so it reads low.
                </p>
              )}
              {n(a.unpriced_hours) > 0 && (
                <p>
                  {hrs(n(a.unpriced_hours))} were worked with no rate on file or by salaried staff,
                  so they carry hours but no estimated cost.
                </p>
              )}
              <p>
                Estimates, from approved timecards — the same facts behind Schedule 14 on the{' '}
                <Link to="/sos" className="text-accent underline">statement</Link>. Actual payroll
                lands on the Pay runs page once a run is processed.
              </p>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}

function Header({
  window: win,
  propertyName,
  range,
  onRange,
}: {
  window: Window
  propertyName: string | null
  range: RangeKey
  onRange: (r: RangeKey) => void
}) {
  return (
    <Card className="flex flex-wrap items-center justify-between gap-4 p-6">
      <div className="min-w-0">
        <h1 className="text-2xl font-bold tracking-tight text-ink">Payroll Dashboard</h1>
        <p className="mt-1 flex flex-wrap items-center gap-x-2 text-sm text-ink-muted">
          <span>Labor cost and productivity</span>
          {propertyName !== null && (
            <>
              <span aria-hidden="true" className="text-ink-faint">·</span>
              <span className="font-medium text-ink">{propertyName}</span>
            </>
          )}
          <span aria-hidden="true" className="text-ink-faint">·</span>
          <span className="tabular-nums">
            {win.from} – {win.to}
          </span>
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Reporting period" className="flex rounded-full border border-line p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              aria-pressed={range === r.key}
              onClick={() => onRange(r.key)}
              className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
                range === r.key
                  ? 'bg-accent text-accent-contrast'
                  : 'text-ink-muted hover:bg-surface-sunken hover:text-ink'
              }`}
            >{r.label}</button>
          ))}
        </div>
        <Badge tone="warn">estimate</Badge>
      </div>
    </Card>
  )
}
