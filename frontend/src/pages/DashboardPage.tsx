// Hotel overview dashboard: gradient hero, KPI tiles with colored icon chips,
// and two charts (revenue by department, 14-day trend) for one property +
// business date. The property/date filters portal into the Layout top bar
// (#topbar-slot). Business date defaults to TODAY — a date with no facts shows
// an honest empty state plus a jump-to-latest shortcut, never fake zeros.
//
// Everything but the first-run setup card comes from GET /api/sos — a day
// call, a month-to-date range call, and one small day call per trend date (a
// missing day 404s and renders as a gap). The card reads the shared checklist
// query instead (design §7). Chart series colors are the chart-1/chart-2
// tokens (validated for CVD + contrast in both themes); the labor series is
// additionally dashed.

import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link } from '@tanstack/react-router'
import { useQueries, useQuery } from '@tanstack/react-query'

import { getSos } from '../api/client'
import type { MetricRow, PropertyInfo, SosReport } from '../api/types'
import { Badge, Card, controlClass, sectionHeadClass } from '../components/ui'
import BarGradients from '../components/BarGradients'
import { barFill, barRampCss, barWidth, topRoundedBar } from '../lib/chartBars'
import { BanknoteIcon, ClockIcon, ReportsIcon, UploadIcon } from '../components/icons'
import { useAuth } from '../auth/authContext'
import { useGlobalProperty } from '../lib/propertyContext'
import { badgeLabel, useChecklist } from '../lib/useChecklist'

// --- formatting --------------------------------------------------------------

const money = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})
const money2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
const num = new Intl.NumberFormat('en-US')

function fmtMoney(value: string | null | undefined, exact = false): string {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  return Number.isFinite(n) ? (exact ? money2 : money).format(n) : '—'
}

function metric(rows: MetricRow[] | undefined, code: string): string | null {
  const row = rows?.find((r) => r.metric_code === code)
  return row?.day ?? row?.mtd ?? null
}

function isoAddDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

function monthStart(iso: string): string {
  return `${iso.slice(0, 7)}-01`
}

function todayIso(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function greeting(): string {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning'
  if (h < 18) return 'Good afternoon'
  return 'Good evening'
}

/** Registry names are stored UPPERCASE; title-case them for display
    ("HOLIDAY INN & SUITES SAN JOSE" -> "Holiday Inn & Suites San Jose"). */
function propertyDisplayName(p: PropertyInfo | undefined): string | null {
  if (p === undefined) return null
  if (p.name === null) return p.property_id
  return p.name
    .toLowerCase()
    .replace(/(^|[\s(/-])[a-z]/g, (m) => m.toUpperCase())
}

// --- charts (inline SVG, token-colored) --------------------------------------

type TrendPoint = { date: string; revenue: number | null; labor: number | null }

function TrendChart({ points }: { points: TrendPoint[] }) {
  const W = 560
  const H = 210
  const PAD = { top: 12, right: 8, bottom: 26, left: 46 }
  const innerW = W - PAD.left - PAD.right
  const innerH = H - PAD.top - PAD.bottom
  const max = Math.max(1, ...points.map((p) => Math.max(p.revenue ?? 0, p.labor ?? 0)))
  const top = max * 1.15
  const step = innerW / points.length
  const barW = barWidth(step, 46)
  const y = (v: number) => PAD.top + innerH * (1 - v / top)
  const gridVals = [top / 3, (2 * top) / 3, top]
  const laborPath = points
    .map((p, i) =>
      p.labor === null ? null : `${PAD.left + i * step + step / 2},${y(p.labor)}`,
    )
    .filter((s): s is string => s !== null)
    .map((coord, i) => `${i === 0 ? 'M' : 'L'}${coord}`)
    .join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Daily revenue and estimated labor cost, last 14 days">
      <BarGradients prefix="trend" colors={['var(--color-chart-1)']} />
      {gridVals.map((v) => (
        <g key={v}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(v)} y2={y(v)} className="stroke-line" strokeWidth="1" />
          <text x={PAD.left - 6} y={y(v) + 3.5} textAnchor="end" className="fill-ink-faint text-[10px] tabular-nums">
            {v >= 1000 ? `${Math.round(v / 1000)}k` : Math.round(v)}
          </text>
        </g>
      ))}
      <line x1={PAD.left} x2={W - PAD.right} y1={PAD.top + innerH} y2={PAD.top + innerH} className="stroke-line-strong" strokeWidth="1" />
      {points.map((p, i) => {
        const cx = PAD.left + i * step + step / 2
        const label = p.date.slice(5).replace('-', '/')
        return (
          <g key={p.date}>
            {p.revenue !== null && (
              <path
                d={topRoundedBar(
                  cx - barW / 2,
                  y(p.revenue),
                  barW,
                  Math.max(2, PAD.top + innerH - y(p.revenue)),
                )}
                fill={barFill('trend', 'var(--color-chart-1)')}
              >
                <title>{`${p.date} · revenue ${money2.format(p.revenue)}`}</title>
              </path>
            )}
            {p.revenue === null && (
              <circle cx={cx} cy={PAD.top + innerH - 3} r="1.5" className="fill-ink-faint">
                <title>{`${p.date} · no data`}</title>
              </circle>
            )}
            {i % 2 === points.length % 2 && (
              <text x={cx} y={H - 8} textAnchor="middle" className="fill-ink-faint text-[9.5px] tabular-nums">
                {label}
              </text>
            )}
          </g>
        )
      })}
      {laborPath !== '' && (
        <path d={laborPath} fill="none" strokeWidth="2" strokeDasharray="5 4" strokeLinecap="round" className="stroke-chart-2" />
      )}
      {points.map(
        (p, i) =>
          p.labor !== null && (
            <circle
              key={`l-${p.date}`}
              cx={PAD.left + i * step + step / 2}
              cy={y(p.labor)}
              r="3"
              className="fill-chart-2"
            >
              <title>{`${p.date} · est labor ${money2.format(p.labor)}`}</title>
            </circle>
          ),
      )}
    </svg>
  )
}

/**
 * The hero's right-hand panel: one headline figure, the 14-day shape behind
 * it, and the three counts a GM looks at before anything else.
 *
 * The headline is MONTH-TO-DATE revenue on purpose — the KPI row below already
 * carries today's, and a hero that repeats the tile under it is decoration.
 * The three columns are the front-desk triad: who is coming, who is going, and
 * what is still on the shelf. None of them repeats a tile either.
 */
function HeroPulse({
  mtd, monthLabel, points, arrivals, departures, available, statementTo,
}: {
  mtd: string | null
  monthLabel: string
  points: TrendPoint[]
  arrivals: string | null
  departures: string | null
  available: string | null
  statementTo: { property?: string; date: string }
}) {
  const revenue = points.map((p) => p.revenue).filter((v): v is number => v !== null)
  return (
    <div
      role="region"
      aria-label="Today at a glance"
      className="w-full shrink-0 rounded-3xl border border-white/25 bg-white/[0.17] p-6 shadow-[0_20px_50px_-24px_rgb(0_0_0/0.7)] backdrop-blur-xl sm:max-w-sm"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-white/70">Revenue</p>
          <p className="text-xs text-white/65">{monthLabel} to date</p>
        </div>
        {/* The reference's "…" affordance, wired to something real rather than
            drawn: the statement this figure comes from. */}
        <Link
          to="/sos"
          search={statementTo}
          aria-label="Open the statement"
          className="rounded-full px-2 py-1 text-lg leading-none text-white/60 transition-colors hover:bg-white/15 hover:text-white"
        >
          ⋯
        </Link>
      </div>

      <p className="mt-2 text-[2.6rem] font-bold leading-none tracking-tight tabular-nums">
        {mtd === null ? '—' : fmtMoney(mtd)}
      </p>

      <HeroSpark values={revenue} />

      <div className="grid grid-cols-3 divide-x divide-dashed divide-white/25">
        <HeroStat label="Arrivals" value={arrivals} />
        <HeroStat label="Departures" value={departures} />
        <HeroStat label="Available" value={available} />
      </div>
    </div>
  )
}

function HeroStat({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="px-3 first:pl-0 last:pr-0">
      <p className="text-xs font-medium text-white/70">{label}</p>
      <p className="mt-1 text-2xl font-bold tabular-nums">
        {value === null ? '—' : num.format(Number(value))}
      </p>
    </div>
  )
}

/**
 * The 14-day revenue shape. ONE series, named by the caption — the reference
 * card draws three anonymous waves, which is decoration; a line on a dashboard
 * is read as data whether or not it was meant to be.
 *
 * Scaled between its own min and max rather than from zero: this is a shape,
 * not a magnitude, and the headline above already carries the magnitude.
 */
function HeroSpark({ values }: { values: number[] }) {
  const W = 300
  const H = 54
  if (values.length < 2) return <div className="my-5 h-[54px]" />
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const step = W / (values.length - 1)
  const y = (v: number) => H - 4 - ((v - min) / span) * (H - 10)
  const line = values.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`)
  return (
    <div className="my-5">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img"
           aria-label="Daily revenue, last 14 days">
        <path d={`M0,${H} L${line.join(' L')} L${W},${H} Z`} fill="rgb(255 255 255 / 0.18)" />
        <path d={`M${line.join(' L')}`} fill="none" stroke="white" strokeWidth="2"
              strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <p className="mt-1.5 text-[11px] text-white/65">Daily revenue · last 14 days</p>
    </div>
  )
}

function DeptBars({ rows }: { rows: { label: string; total: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => r.total))
  return (
    <div className="flex flex-col gap-3">
      {rows.map((r) => (
        <div key={r.label} className="grid grid-cols-[10rem_1fr_5.5rem] items-center gap-3 text-sm">
          <span className="truncate text-ink-muted" title={r.label}>
            {r.label}
          </span>
          <div className="h-7 overflow-hidden rounded-lg bg-surface-sunken">
            <div
              className="h-full rounded-lg"
              style={{
                width: `${Math.max(1.5, (r.total / max) * 100)}%`,
                background: barRampCss('var(--color-chart-1)'),
              }}
              title={money2.format(r.total)}
            />
          </div>
          <span className="text-right tabular-nums text-ink">{fmtMoney(String(r.total))}</span>
        </div>
      ))}
    </div>
  )
}

/** Small occupancy donut, stroke = chart-1 over the line token. */
function OccRing({ pct }: { pct: number }) {
  const R = 17
  const C = 2 * Math.PI * R
  const filled = Math.max(0, Math.min(100, pct)) / 100
  return (
    <svg viewBox="0 0 44 44" width="44" height="44" aria-hidden="true" className="-rotate-90">
      <circle cx="22" cy="22" r={R} fill="none" strokeWidth="5" className="stroke-line" />
      <circle
        cx="22"
        cy="22"
        r={R}
        fill="none"
        strokeWidth="5"
        strokeLinecap="round"
        strokeDasharray={`${C * filled} ${C}`}
        className="stroke-chart-1"
      />
    </svg>
  )
}

// --- KPI tile ---------------------------------------------------------------

function Kpi({
  label,
  value,
  sub,
  badge,
  visual,
}: {
  label: string
  value: string
  sub?: string
  badge?: string
  visual: React.ReactNode
}) {
  return (
    <Card className="flex items-center gap-4">
      {visual}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex items-center justify-between gap-2">
          <span className={sectionHeadClass}>{label}</span>
          {badge !== undefined && <Badge tone="info">{badge}</Badge>}
        </div>
        <span className="truncate text-[26px] font-bold leading-tight tabular-nums tracking-tight text-ink">
          {value}
        </span>
        {sub !== undefined && <span className="truncate text-xs text-ink-muted">{sub}</span>}
      </div>
    </Card>
  )
}

function IconChip({ tone, children }: { tone: string; children: React.ReactNode }) {
  return (
    <span className={`grid size-11 shrink-0 place-items-center rounded-xl ${tone}`}>{children}</span>
  )
}

// --- first-run setup card ----------------------------------------------------

/**
 * A pointer to /setup, not a second checklist — the destination carries the
 * items, this only says how much is left and then gets out of the way.
 */
function SetupCard() {
  const { data } = useChecklist()
  // all_clear, NOT open_count > 0. They differ only on a probe failure, where
  // open_count is 0 while nothing is known — gating on it would retire this
  // card at exactly the moment it matters. (design §7)
  //
  // `undefined` covers pending and error alike: the card is ambient, and a
  // dashboard is not where a checklist fetch failure gets announced.
  if (data === undefined || data.all_clear) return null

  const failed = data.error_count > 0
  // badgeLabel's sentence, not a second count string: this card, the sidebar
  // badge and /setup must not word one state three ways. It already keeps the
  // two counts apart — an item we could not check is not an item still to do,
  // and no sum may stand in for "we do not know".
  //
  // Non-null: badgeLabel is null only on all_clear, which the gate above took.
  const line = badgeLabel(data)!.title

  return (
    <Card className="flex items-center gap-3">
      {/* The same two glyphs the /setup rows use for these two states, so the
          card and its destination read as one feature. */}
      <span
        aria-hidden="true"
        className={`text-sm font-semibold ${failed ? 'text-danger-red' : 'text-warn-amber'}`}
      >
        {failed ? '!' : '○'}
      </span>
      <div className="flex min-w-0 flex-col gap-0.5">
        {/* Headline wording is fixed across both branches: one accessible name
            for one card, whatever it goes on to say beneath. */}
        <Link to="/setup" className="text-sm font-semibold text-accent hover:underline">
          Finish setting up
        </Link>
        <p className="text-sm text-ink-muted">{line}</p>
      </div>
    </Card>
  )
}

// --- page -------------------------------------------------------------------

const TREND_DAYS = 14

export default function DashboardPage() {
  const { user } = useAuth()
  const username = user?.profile.preferred_username
  // Property is GLOBAL (top-bar selector); only the date is page-local.
  const { property, selected } = useGlobalProperty()
  const [pickedDate, setPickedDate] = useState<string | undefined>(undefined)
  const [slot, setSlot] = useState<HTMLElement | null>(null)

  useEffect(() => {
    setSlot(document.getElementById('topbar-slot'))
  }, [])

  const date = pickedDate ?? todayIso()

  const ready = property !== undefined

  const dayQuery = useQuery({
    queryKey: ['sos', property, date],
    queryFn: () => getSos({ property: property!, date }),
    enabled: ready,
    retry: false,
  })

  const mtdQuery = useQuery({
    queryKey: ['sos', property, monthStart(date), date],
    queryFn: () => getSos({ property: property!, from: monthStart(date), to: date }),
    enabled: ready,
    retry: false,
  })

  const trendDates = useMemo(
    () => Array.from({ length: TREND_DAYS }, (_, i) => isoAddDays(date, i - (TREND_DAYS - 1))),
    [date],
  )

  const trendQueries = useQueries({
    queries: trendDates.map((d) => ({
      queryKey: ['sos', property, d],
      queryFn: () => getSos({ property: property!, date: d }),
      enabled: ready,
      retry: false,
      staleTime: 5 * 60_000,
    })),
  })

  const trendPoints: TrendPoint[] = trendDates.map((d, i) => {
    const q = trendQueries[i]
    const report: SosReport | undefined = q?.data
    return {
      date: d,
      revenue: report === undefined ? null : Number(report.total_operating_revenue),
      labor:
        report === undefined || Number(report.payroll_expense_total) === 0
          ? null
          : Number(report.payroll_expense_total),
    }
  })

  const day = dayQuery.data
  const stats = day?.statistics
  const occ = metric(stats, 'OCCUPANCY_PCT')
  const adr = metric(stats, 'ADR')
  const revpar = metric(stats, 'REVPAR')
  const roomsOcc = metric(stats, 'ROOMS_OCCUPIED')
  // TOTAL_ROOMS is the house size; ROOMS_AVAILABLE in the PMS feed means
  // "still vacant", which is not the denominator occupancy is computed against.
  const roomsTotal = metric(stats, 'TOTAL_ROOMS')
  const arrivals = metric(stats, 'ARRIVALS')
  const departures = metric(stats, 'DEPARTURES')
  // ROOMS_AVAILABLE in this feed means "still vacant tonight" — see the note
  // on roomsTotal above. That is exactly the number a GM can still act on.
  const roomsFree = metric(stats, 'ROOMS_AVAILABLE')

  const deptRows =
    day === undefined
      ? []
      : [
          ...day.operated_departments.map((s) => ({
            label: s.sub_category,
            total: Number(s.total),
          })),
          { label: 'Miscellaneous income', total: Number(day.misc_income_total) },
        ].filter((r) => r.total !== 0)

  const laborHours = day === undefined ? null : Number(day.labor_hours_total)

  const filters = (
    <input
      type="date"
      aria-label="Business date"
      title={
        selected === undefined
          ? undefined
          : `Data available ${selected.first_date} – ${selected.last_date}`
      }
      className={controlClass}
      value={date}
      onChange={(e) => setPickedDate(e.target.value || undefined)}
    />
  )

  return (
    <div className="space-y-4">
      {slot !== null && createPortal(filters, slot)}

      {/* hero */}
      <div className="dash-hero flex flex-wrap items-start justify-between gap-8 rounded-2xl px-8 py-10 text-white shadow-card">
        <div className="min-w-0 flex-1">
        <span className="inline-flex items-center gap-2 rounded-full border border-white/25 bg-white/15 px-4 py-1.5 text-sm font-semibold backdrop-blur">
          <span aria-hidden="true">👋</span>
          {greeting()}
          {username === undefined ? '' : `, ${username}`}
        </span>
        <h1 className="mt-4 text-3xl font-bold tracking-tight">
          {propertyDisplayName(selected) ?? 'Hotel overview'}
        </h1>
        <p className="mt-2 max-w-2xl text-[15px] text-white/85">
          Business date {date}. Revenue, occupancy, and labor at a glance, with the full statement
          one click away.
        </p>
        <div className="mt-6 flex flex-wrap gap-2.5">
          <Link
            to="/sos"
            search={{ property, date }}
            className="rounded-full bg-white px-5 py-2.5 text-sm font-semibold text-indigo-700 shadow-sm transition-colors hover:bg-indigo-50"
          >
            View statement
          </Link>
          <Link
            to="/upload"
            className="flex items-center gap-2 rounded-full border border-white/40 bg-white/10 px-5 py-2.5 text-sm font-semibold text-white backdrop-blur transition-colors hover:bg-white/20"
          >
            <UploadIcon width={15} height={15} />
            Upload report
          </Link>
        </div>
        </div>

        <HeroPulse
          mtd={mtdQuery.data?.total_operating_revenue ?? null}
          monthLabel={new Date(`${date}T00:00:00`).toLocaleString('en-US', { month: 'long' })}
          points={trendPoints}
          arrivals={arrivals}
          departures={departures}
          available={roomsFree}
          statementTo={{ property, date }}
        />
      </div>

      {/* Directly under the hero, above the KPI grid: a first-run operator has
          no KPIs yet, so a card below an empty grid is a card below the fold. */}
      <SetupCard />

      {ready && dayQuery.isError && (
        <Card className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-ink-muted">
            No data for <span className="font-semibold text-ink">{date}</span>
            {selected === undefined
              ? '.'
              : ` — data available ${selected.first_date} to ${selected.last_date}.`}
          </p>
          {selected !== undefined && (
            <button
              type="button"
              className="rounded-lg bg-accent px-3 py-1.5 text-sm font-semibold text-accent-contrast hover:opacity-90"
              onClick={() => setPickedDate(selected.last_date)}
            >
              Jump to latest data
            </button>
          )}
        </Card>
      )}
      {ready && dayQuery.isPending && <p className="text-sm text-ink-muted">Loading dashboard…</p>}

      {day !== undefined && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Kpi
              label="Total revenue"
              value={fmtMoney(day.total_operating_revenue, true)}
              sub={
                mtdQuery.data === undefined
                  ? 'day · operating revenue'
                  : `MTD ${fmtMoney(mtdQuery.data.total_operating_revenue)}`
              }
              visual={
                <IconChip tone="bg-accent-soft text-accent-ink">
                  <BanknoteIcon />
                </IconChip>
              }
            />
            <Kpi
              label="Occupancy"
              value={occ === null ? '—' : `${Number(occ).toFixed(1)}%`}
              sub={
                roomsOcc === null || roomsTotal === null
                  ? 'no statistics for this date'
                  : `${num.format(Number(roomsOcc))} of ${num.format(Number(roomsTotal))} rooms · ${arrivals === null ? '—' : num.format(Number(arrivals))} arrivals`
              }
              visual={<OccRing pct={occ === null ? 0 : Number(occ)} />}
            />
            <Kpi
              label="ADR"
              value={adr === null ? '—' : fmtMoney(adr, true)}
              sub={revpar === null ? undefined : `RevPAR ${fmtMoney(revpar, true)}`}
              visual={
                <IconChip tone="bg-ok-green-soft text-ok-green">
                  <ReportsIcon />
                </IconChip>
              }
            />
            <Kpi
              label="Labor cost"
              value={fmtMoney(day.payroll_expense_total, true)}
              badge="Estimate"
              sub={
                laborHours === null || laborHours === 0
                  ? 'no approved hours for this date'
                  : `${num.format(laborHours)} h${day.labor_fte === null ? '' : ` · FTE ${day.labor_fte}`}`
              }
              visual={
                <IconChip tone="bg-warn-amber-soft text-warn-amber">
                  <ClockIcon />
                </IconChip>
              }
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <h2 className={sectionHeadClass}>Revenue by department</h2>
              <p className="mb-4 mt-0.5 text-xs text-ink-muted">{date} · operating revenue</p>
              {deptRows.length === 0 ? (
                <p className="text-sm text-ink-muted">No revenue lines for this date.</p>
              ) : (
                <DeptBars rows={deptRows} />
              )}
            </Card>
            <Card>
              <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <h2 className={sectionHeadClass}>Last {TREND_DAYS} days</h2>
                  <p className="mt-0.5 text-xs text-ink-muted">
                    daily totals ending {date} · missing days show as gaps
                  </p>
                </div>
                <div className="flex items-center gap-4 text-xs text-ink-muted">
                  <span className="flex items-center gap-1.5">
                    <span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-[3px] bg-chart-1" />
                    Revenue
                  </span>
                  <span className="flex items-center gap-1.5">
                    <svg width="18" height="6" aria-hidden="true">
                      <line x1="0" y1="3" x2="18" y2="3" strokeWidth="2" strokeDasharray="5 4" className="stroke-chart-2" />
                    </svg>
                    Labor (est)
                  </span>
                </div>
              </div>
              <TrendChart points={trendPoints} />
            </Card>
          </div>

          {day.labor_variance !== null && (
            <Card>
              <div className="flex flex-wrap items-center gap-3">
                <h2 className={sectionHeadClass}>Payroll actual vs estimate</h2>
                {day.labor_variance.alert && <Badge tone="warn">Variance alert</Badge>}
              </div>
              <p className="mt-2 text-sm text-ink-muted">
                Estimate {fmtMoney(day.labor_variance.est_total, true)} · Actual{' '}
                {fmtMoney(day.labor_variance.actual_total, true)} · Variance{' '}
                <span className="tabular-nums text-ink">{fmtMoney(day.labor_variance.variance_total, true)}</span>{' '}
                across {day.labor_variance.periods.join(', ')}
              </p>
            </Card>
          )}
        </>
      )}
    </div>
  )
}
