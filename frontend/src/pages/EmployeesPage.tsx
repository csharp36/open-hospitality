// Workforce hub: header card, KPI strip, roster, inactive list, departments,
// and modal-based onboarding / editing. Property comes from the GLOBAL top-bar
// selector — pick it once, every page follows.
//
// TWO GATES LIVE ON THIS PAGE AND THEY ARE DIFFERENT.
//   * The payroll PII vault (SSN, bank, tax elections) is payroll_admin-only,
//     sealed client side and write-only — unchanged, and mirroring
//     require_payroll_admin exactly.
//   * The pay RATE is wider: payroll_admin, org_admin, or a property_gm within
//     its own property, mirroring require_rate_editor. Being allowed to say
//     what someone earns is not being allowed to read their SSN.
//   * HOURS go to every operator who can already see the employee — the
//     operational fact a department manager needs to run a schedule.
//   * EARNED COST rides the RATE gate, because cost over hours is the rate.
//     The server decides and sends null; this page renders an em dash and
//     withholds the Labor Cost tile with it. Never re-derive a total from the
//     rows to fill the gap — that is the leak wearing a different hat.

import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'

import { Badge, Card, controlClass, controlLargeClass } from '../components/ui'
import Modal from '../components/Modal'
import IconButton from '../components/IconButton'
import {
  AlertIcon,
  BanknoteIcon,
  ClockIcon,
  PauseIcon,
  PencilIcon,
  PeopleIcon,
  PlayIcon,
  UserIcon,
  TrendUpIcon,
  UserMinusIcon,
  WalletIcon,
} from '../components/icons'
import {
  createDepartment,
  deleteFaceTemplate,
  enrollFaceTemplate,
  getDepositAccounts,
  getFaceTemplate,
  getDepartments,
  getEmployeeWork,
  getEmployees,
  getMe,
  getPayRate,
  getPayrollProfile,
  getPayrollPublicKey,
  onboardEmployee,
  putDepositAccounts,
  putPayrollProfile,
  setEmployeeSuspended,
  setPayRate,
  terminateEmployee,
  updateEmployee,
} from '../api/client'
import type {
  AllocationType,
  DepositAccountBody,
  Department,
  Employee,
  EmployeeWork,
  SealedProfileBody,
} from '../api/types'
import { useGlobalProperty } from '../lib/propertyContext'
import { hasRole } from '../lib/roles'
import { depositAad, sealField } from '../lib/piiSeal'
import { errorMessage } from '../lib/errors'

// department_manager needs a department selector (follow-up) — omitted here so the
// UI never onboards a department_manager without a department (would be mis-scoped).
// The VALUE is the wire identifier the backend gates on and never changes; the
// label is only what a human reads in the picker.
const OPERATOR_ROLES: { value: string; label: string }[] = [
  { value: 'property_gm', label: 'Property GM' },
  { value: 'accountant', label: 'Accountant' },
  { value: 'payroll_admin', label: 'Payroll Admin' },
  { value: 'org_admin', label: 'Org Admin' },
]

/** Titles a wire value for display. `pay_type` travels as 'hourly' | 'salary'
 *  and is written back untouched — only the reading of it is capitalised. */
function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1)
}

// Every in-card label on this page: Title Case, not shouted. The shared
// `sectionHeadClass` still uppercases elsewhere in the app, which is why this
// page carries its own recipe rather than editing that one.
const cardLabelClass = 'text-xs font-semibold tracking-wide text-ink-muted'

const money = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})

function iso(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

type Window = { from: string; to: string }

/**
 * The three windows the KPIs read.
 *
 * `priorSpan` is the SAME NUMBER OF DAYS of the previous month, not the whole
 * of it: on the 2nd, month-to-date is two days, and holding that against a full
 * 31-day month would print a −93% that only measures how early in the month it
 * is. `priorFull` is carried alongside as context, so the whole-month figure is
 * still on the tile without pretending to be a comparison.
 *
 * `new Date(y, m, 0)` is the last day of the previous month, so 31 March
 * compares against 28 February rather than spilling into March.
 */
function monthWindows(now: Date): { current: Window; priorSpan: Window; priorFull: Window } {
  const y = now.getFullYear()
  const m = now.getMonth()
  const priorLastDay = new Date(y, m, 0).getDate()
  return {
    current: { from: iso(new Date(y, m, 1)), to: iso(now) },
    priorSpan: {
      from: iso(new Date(y, m - 1, 1)),
      to: iso(new Date(y, m - 1, Math.min(now.getDate(), priorLastDay))),
    },
    priorFull: { from: iso(new Date(y, m - 1, 1)), to: iso(new Date(y, m, 0)) },
  }
}

function monthName(d: Date, style: 'long' | 'short' = 'long'): string {
  return d.toLocaleString('en-US', { month: style })
}

/** "Jul 1–2" — the span a delta is actually measured against, spelled out so it
 *  is never read as the whole month. */
function spanLabel(w: Window): string {
  const from = new Date(`${w.from}T00:00:00`)
  const to = new Date(`${w.to}T00:00:00`)
  const month = monthName(from, 'short')
  return from.getDate() === to.getDate()
    ? `${month} ${from.getDate()}`
    : `${month} ${from.getDate()}–${to.getDate()}`
}

function fmtHours(value: number): string {
  return `${value.toLocaleString('en-US', { maximumFractionDigits: 1 })} h`
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .map((w) => w[0])
    .slice(0, 2)
    .join('')
    .toUpperCase()
}

// --- KPIs --------------------------------------------------------------------

/**
 * Change against the same span of the previous month.
 *
 * `good` says which direction is welcome, because it is not the same answer for
 * every measure: more hours worked is neither good nor bad on its own, more
 * labor cost usually is not, and more overtime rarely is. A tile that painted
 * every rise green would be telling the reader something untrue. `'none'` keeps
 * the neutral ink and lets the arrow carry the direction alone.
 *
 * The arrow is a second cue beside the colour, so the direction survives for a
 * colour-blind reader and in print.
 */
function Delta({
  current,
  prior,
  good,
  format,
}: {
  current: number
  prior: number
  good: 'up' | 'down' | 'none'
  format: (v: number) => string
}) {
  if (prior === 0) {
    // A percentage off zero is undefined, but "no comparison" would bury the
    // most newsworthy case there is — a measure that went from nothing to
    // something. Show the absolute rise and say what it rose from.
    if (current === 0) return <span className="text-xs text-ink-faint">nothing yet</span>
    return (
      <span
        className={`text-xs font-medium ${good === 'down' ? 'text-warn-amber' : 'text-ink-muted'}`}
      >
        <span aria-hidden="true">▲</span> {format(current)} from none
      </span>
    )
  }
  const change = current - prior
  const pct = (change / prior) * 100
  const rising = change > 0
  const flat = Math.abs(pct) < 0.05
  const welcome = good === 'none' ? null : (rising ? good === 'up' : good === 'down')
  const tone = flat || welcome === null
    ? 'text-ink-muted'
    : welcome
      ? 'text-ok-green'
      : 'text-warn-amber'
  return (
    <span className={`flex flex-wrap items-baseline gap-x-1.5 text-xs font-medium ${tone}`}>
      <span className="tabular-nums">
        <span aria-hidden="true">{flat ? '→' : rising ? '▲' : '▼'}</span>{' '}
        {flat ? 'level' : `${Math.abs(pct).toFixed(1)}%`}
      </span>
      <span className="text-ink-muted">
        ({rising ? '+' : '−'}
        {format(Math.abs(change))})
      </span>
    </span>
  )
}

function KpiTile({
  label,
  value,
  hint,
  delta,
  icon,
  tone,
}: {
  label: string
  value: string
  hint?: string
  delta?: React.ReactNode
  icon: React.ReactNode
  tone: string
}) {
  return (
    <Card className="flex items-start gap-3.5 p-5">
      <span className={`grid size-11 shrink-0 place-items-center rounded-xl ${tone}`}>{icon}</span>
      <div className="min-w-0">
        <p className={cardLabelClass}>{label}</p>
        <p className="mt-0.5 text-[26px] font-bold leading-tight tabular-nums tracking-tight text-ink">
          {value}
        </p>
        {delta !== undefined && <div className="mt-1">{delta}</div>}
        {hint !== undefined && <p className="mt-0.5 text-xs text-ink-faint">{hint}</p>}
      </div>
    </Card>
  )
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 flex-1 items-baseline gap-2 px-4 py-1">
      <span className="text-lg font-semibold tabular-nums text-ink">{value}</span>
      <span className="truncate text-xs font-medium tracking-wide text-ink-muted">{label}</span>
    </div>
  )
}

// --- page --------------------------------------------------------------------

// `cost` is null when the server withheld earnings from this caller — the
// total has to be withheld too, not summed as zero. A footer reading $0.00
// under a column of em dashes would be a lie the user has no way to spot.
type WorkTotals = { hours: number; ot: number; cost: number | null }

function sumWork(rows: EmployeeWork[] | undefined): WorkTotals {
  const list = rows ?? []
  const priced = list.every((r) => r.est_cost !== null)
  return list.reduce<WorkTotals>(
    (acc, r) => ({
      hours: acc.hours + Number(r.hours),
      ot: acc.ot + Number(r.ot_hours),
      cost: acc.cost === null ? null : acc.cost + Number(r.est_cost),
    }),
    { hours: 0, ot: 0, cost: priced ? 0 : null },
  )
}

export default function EmployeesPage() {
  const qc = useQueryClient()
  const { property, selected } = useGlobalProperty()
  // include_inactive: the roster and the inactive list are one fetch, split in
  // the client — two queries would show a person twice mid-transition.
  const employees = useQuery({
    queryKey: ['employees', 'all'],
    queryFn: () => getEmployees(true),
  })
  const departments = useQuery({
    queryKey: ['departments', property],
    queryFn: () => getDepartments(property!),
    enabled: property !== undefined,
  })
  const me = useQuery({ queryKey: ['me'], queryFn: getMe })

  // Computed once per mount: three windows off the same clock reading, so the
  // current month and the span it is compared against can never straddle
  // midnight and disagree about what day it is.
  const [now] = useState(() => new Date())
  const windows = useMemo(() => monthWindows(now), [now])

  const workQueries = useQueries({
    queries: [windows.current, windows.priorSpan, windows.priorFull].map((w) => ({
      queryKey: ['employee-work', property, w.from, w.to],
      queryFn: () => getEmployeeWork({ from: w.from, to: w.to, property }),
      enabled: property !== undefined,
    })),
  })
  const work = workQueries[0]!
  const priorSpanWork = workQueries[1]!
  const priorFullWork = workQueries[2]!

  // Two gates, mirroring the backend exactly. An accountant or department
  // manager sees the roster and gets neither.
  const canPayroll = hasRole(me.data, 'payroll_admin')
  // Face enrollment mirrors the backend's require_onboarder gate exactly.
  const canFace = hasRole(me.data, 'org_admin', 'property_gm')
  const [selectedFace, setSelectedFace] = useState<number | null>(null)
  const canSetRate = hasRole(me.data, 'payroll_admin', 'org_admin', 'property_gm')
  const [selectedPayroll, setSelectedPayroll] = useState<number | null>(null)
  const [onboardOpen, setOnboardOpen] = useState(false)
  const [editing, setEditing] = useState<Employee | null>(null)
  const [confirming, setConfirming] = useState<Employee | null>(null)

  const terminate = useMutation({
    mutationFn: (id: number) => terminateEmployee(id),
    onSuccess: () => setConfirming(null),
    onSettled: () => qc.invalidateQueries({ queryKey: ['employees'] }),
  })
  const suspend = useMutation({
    mutationFn: (v: { id: number; suspended: boolean }) =>
      setEmployeeSuspended(v.id, v.suspended),
    onSettled: () => qc.invalidateQueries({ queryKey: ['employees'] }),
  })

  const inProperty = useMemo(
    () => (employees.data ?? []).filter((e) => property === undefined || e.property_id === property),
    [employees.data, property],
  )
  const roster = inProperty.filter((e) => e.employment_status === 'active')
  const inactive = inProperty.filter((e) => e.employment_status !== 'active')

  const workById = useMemo(() => {
    const map = new Map<number, EmployeeWork>()
    for (const row of work.data ?? []) map.set(row.employee_id, row)
    return map
  }, [work.data])

  const totals = sumWork(work.data)
  const priorSpanTotals = sumWork(priorSpanWork.data)
  const priorFullTotals = sumWork(priorFullWork.data)
  const comparedTo = spanLabel(windows.priorSpan)
  const priorMonth = monthName(new Date(`${windows.priorFull.from}T00:00:00`))
  const thisMonth = monthName(now)
  const hourly = roster.filter((e) => e.pay_type === 'hourly').length
  const deptName = (id: number | null): string => {
    if (id === null) return '—'
    return departments.data?.find((d) => d.department_id === id)?.name ?? `Dept ${id}`
  }
  const selectedEmployee = employees.data?.find((e) => e.employee_id === selectedPayroll)
  const otShare = totals.hours === 0 ? 0 : (totals.ot / totals.hours) * 100

  const actionsBusy = terminate.isPending || suspend.isPending
  const actionError = terminate.isError
    ? errorMessage(terminate.error)
    : suspend.isError
      ? errorMessage(suspend.error)
      : null

  return (
    <div className="flex flex-col gap-5">
      {/* header card — title, context, and the primary action in one frame */}
      <Card className="flex flex-wrap items-center justify-between gap-4 p-6">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold tracking-tight text-ink">Employees</h1>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-ink-muted">
            <span>Workforce Roster</span>
            {selected !== undefined && (
              <>
                <span aria-hidden="true" className="text-ink-faint">
                  ·
                </span>
                <span className="font-medium text-ink">{selected.property_id}</span>
              </>
            )}
            <span aria-hidden="true" className="text-ink-faint">
              ·
            </span>
            <span>
              {roster.length} active
              {inactive.length > 0 ? `, ${inactive.length} inactive` : ''}
            </span>
          </p>
        </div>
        <button
          type="button"
          onClick={() => setOnboardOpen(true)}
          className="flex items-center gap-2 rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-accent-contrast shadow-sm transition-opacity hover:opacity-90"
        >
          <PeopleIcon width={16} height={16} />
          Add Employee
        </button>
      </Card>

      {/* KPI strip */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiTile
          label="Total Staff"
          value={String(roster.length)}
          hint={`${hourly} hourly · ${roster.length - hourly} salaried`}
          icon={<PeopleIcon width={20} height={20} />}
          tone="bg-accent-soft text-accent-ink"
        />
        <KpiTile
          label={`Hours Worked · ${thisMonth}`}
          value={fmtHours(totals.hours)}
          delta={
            <Delta
              current={totals.hours}
              prior={priorSpanTotals.hours}
              good="none"
              format={fmtHours}
            />
          }
          hint={`vs ${comparedTo} · all ${priorMonth} ${fmtHours(priorFullTotals.hours)}`}
          icon={<ClockIcon width={20} height={20} />}
          tone="bg-info-blue-soft text-info-blue"
        />
        {/* Withheld earnings take the tile down with them: no value, and no
            delta either — a period-over-period CHANGE in labor cost is the
            same money answered a slower way. The hint names the reason,
            which is about the viewer's own role and discloses nothing about
            anybody on the roster. */}
        <KpiTile
          label={`Labor Cost · ${thisMonth}`}
          value={totals.cost === null ? '—' : money.format(totals.cost)}
          delta={
            totals.cost === null || priorSpanTotals.cost === null ? undefined : (
              <Delta
                current={totals.cost}
                prior={priorSpanTotals.cost}
                good="down"
                format={(v) => money.format(v)}
              />
            )
          }
          hint={
            totals.cost === null
              ? 'Requires payroll access'
              : `vs ${comparedTo} · all ${priorMonth} ${
                  priorFullTotals.cost === null ? '—' : money.format(priorFullTotals.cost)
                }`
          }
          icon={<BanknoteIcon width={20} height={20} />}
          tone="bg-ok-green-soft text-ok-green"
        />
        <KpiTile
          label={`Overtime · ${thisMonth}`}
          value={fmtHours(totals.ot)}
          delta={
            <Delta
              current={totals.ot}
              prior={priorSpanTotals.ot}
              good="down"
              format={fmtHours}
            />
          }
          hint={`${otShare.toFixed(1)}% of hours · all ${priorMonth} ${fmtHours(priorFullTotals.ot)}`}
          icon={totals.ot > 0 ? <AlertIcon width={20} height={20} /> : <TrendUpIcon width={20} height={20} />}
          tone="bg-warn-amber-soft text-warn-amber"
        />
      </div>

      {/* secondary stat bar */}
      <Card className="flex flex-wrap items-center gap-y-2 divide-x divide-line p-2">
        <MiniStat label="Departments" value={String(departments.data?.length ?? 0)} />
        <MiniStat label="Hourly" value={String(hourly)} />
        <MiniStat label="Salaried" value={String(roster.length - hourly)} />
        <MiniStat
          label={`Avg / Person · ${thisMonth}`}
          value={roster.length === 0 ? '—' : fmtHours(totals.hours / roster.length)}
        />
        <MiniStat label="Inactive" value={String(inactive.length)} />
      </Card>

      {actionError !== null && (
        <p className="text-sm text-danger-red">Action failed: {actionError}</p>
      )}

      {/* roster */}
      <Card role="region" aria-label="roster" className="p-0">
        {employees.isPending && <p className="p-5 text-sm text-ink-muted">Loading …</p>}
        {employees.isError && (
          <p className="p-5 text-sm text-danger-red">
            Failed to load: {errorMessage(employees.error)}
          </p>
        )}
        {employees.data !== undefined &&
          (roster.length === 0 ? (
            <p className="p-5 text-sm text-ink-muted">
              No employees yet{selected === undefined ? '' : ` for ${selected.property_id}`} — add
              the first one.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <RosterTable
                label="Employees"
                rows={roster}
                deptName={deptName}
                workById={workById}
                period={monthName(now, 'short')}
                actions={(e) => (
                  <>
                    <IconButton
                      label={`Edit ${e.full_name}`}
                      icon={<PencilIcon width={17} height={17} />}
                      onClick={() => setEditing(e)}
                    />
                    {canFace && (
                      <IconButton
                        label={`Face for ${e.full_name}`}
                        tone="accent"
                        icon={<UserIcon width={17} height={17} />}
                        onClick={() => setSelectedFace(e.employee_id)}
                      />
                    )}
                    {canPayroll && (
                      <IconButton
                        label={`Payroll for ${e.full_name}`}
                        tone="accent"
                        icon={<WalletIcon width={17} height={17} />}
                        onClick={() => setSelectedPayroll(e.employee_id)}
                      />
                    )}
                    <IconButton
                      label={`Suspend ${e.full_name}`}
                      tone="warn"
                      disabled={actionsBusy}
                      icon={<PauseIcon width={17} height={17} />}
                      onClick={() => suspend.mutate({ id: e.employee_id, suspended: true })}
                    />
                    <IconButton
                      label={`Terminate ${e.full_name}`}
                      tone="danger"
                      disabled={actionsBusy}
                      icon={<UserMinusIcon width={17} height={17} />}
                      onClick={() => setConfirming(e)}
                    />
                  </>
                )}
              />
            </div>
          ))}
      </Card>

      {/* inactive — below the divider, same shape, different actions */}
      {inactive.length > 0 && (
        <div className="border-t border-line pt-5">
          <div className="mb-3 flex flex-wrap items-baseline gap-x-2">
            <h2 className={`${cardLabelClass} text-sm`}>Not Active</h2>
            <span className="text-xs text-ink-faint">
              suspended and terminated — they schedule nothing and are paid nothing
            </span>
          </div>
          <Card role="region" aria-label="inactive employees" className="p-0">
            <div className="overflow-x-auto">
              <RosterTable
                label="Inactive employees"
                rows={inactive}
                deptName={deptName}
                workById={workById}
                period={monthName(now, 'short')}
                muted
                actions={(e) =>
                  e.employment_status === 'leave' ? (
                    <IconButton
                      label={`Reinstate ${e.full_name}`}
                      tone="ok"
                      disabled={actionsBusy}
                      icon={<PlayIcon width={17} height={17} />}
                      onClick={() => suspend.mutate({ id: e.employee_id, suspended: false })}
                    />
                  ) : (
                    <span className="whitespace-nowrap pr-1 text-xs text-ink-faint">
                      no longer placed
                    </span>
                  )
                }
              />
            </div>
          </Card>
        </div>
      )}

      <div className="xl:max-w-md">
        <DepartmentsCard propertyId={property} />
      </div>

      {onboardOpen && property !== undefined && (
        <OnboardModal
          propertyId={property}
          departments={departments.data ?? []}
          canSetRate={canSetRate}
          onClose={() => setOnboardOpen(false)}
        />
      )}

      {editing !== null && (
        <EditModal
          key={editing.employee_id}
          employee={editing}
          departments={departments.data ?? []}
          canSetRate={canSetRate}
          onClose={() => setEditing(null)}
        />
      )}

      {confirming !== null && (
        <Modal
          title="Terminate Employee"
          subtitle={`${confirming.full_name} will be removed from the roster, their login disabled, and their placement closed today. Hours already worked stay on the books.`}
          onClose={() => setConfirming(null)}
        >
          <div className="flex items-center justify-end gap-3">
            <button
              type="button"
              onClick={() => setConfirming(null)}
              className={cancelButtonClass}
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={terminate.isPending}
              onClick={() => terminate.mutate(confirming.employee_id)}
              className="h-10 rounded-control bg-danger-red px-5 text-sm font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              Terminate
            </button>
          </div>
        </Modal>
      )}

      {canPayroll && selectedPayroll !== null && (
        <PayrollSubForm
          key={selectedPayroll}
          employeeId={selectedPayroll}
          employeeName={selectedEmployee?.full_name ?? `#${selectedPayroll}`}
          onClose={() => setSelectedPayroll(null)}
        />
      )}

      {canFace && selectedFace !== null && (
        <FaceSubForm
          key={selectedFace}
          employeeId={selectedFace}
          employeeName={
            employees.data?.find((e) => e.employee_id === selectedFace)?.full_name
            ?? `#${selectedFace}`
          }
          onClose={() => setSelectedFace(null)}
        />
      )}
    </div>
  )
}


const headCls = 'px-5 py-3.5 text-left text-xs font-semibold tracking-wide text-ink-muted'
const numHeadCls = `${headCls} text-right`
const rowCellCls = 'px-5 py-4 align-middle'
const numCellCls = `${rowCellCls} text-right tabular-nums`

function RosterTable({
  label,
  rows,
  deptName,
  workById,
  period,
  actions,
  muted = false,
}: {
  label: string
  rows: Employee[]
  deptName: (id: number | null) => string
  workById: Map<number, EmployeeWork>
  /** Same window as the KPI tiles, so a column can be added up and reconciled
   *  against the number above it. */
  period: string
  actions: (e: Employee) => React.ReactNode
  muted?: boolean
}) {
  return (
    <table className="w-full min-w-[54rem] text-sm" aria-label={label}>
      <thead>
        <tr className="border-b border-line bg-surface-sunken/60">
          <th className={headCls}>Name</th>
          <th className={headCls}>Department</th>
          <th className={headCls}>Pay</th>
          <th className={numHeadCls}>Hours · {period}</th>
          <th className={numHeadCls}>Earned · {period}</th>
          <th className={`${headCls} text-right`}>Actions</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((e) => {
          const w = workById.get(e.employee_id)
          return (
            <tr
              key={e.employee_id}
              className="border-b border-line last:border-0 transition-colors hover:bg-surface-sunken"
            >
              <td className={rowCellCls}>
                <span className="flex items-center gap-3">
                  <span
                    aria-hidden="true"
                    className={`grid size-9 shrink-0 place-items-center rounded-full text-[12px] font-bold ${
                      muted ? 'bg-surface-sunken text-ink-muted' : 'bg-accent-soft text-accent-ink'
                    }`}
                  >
                    {initials(e.full_name)}
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate font-semibold text-ink">{e.full_name}</span>
                    {e.employment_status === 'leave' && (
                      <span className="text-xs font-medium text-warn-amber">Suspended</span>
                    )}
                    {e.employment_status === 'terminated' && (
                      <span className="text-xs font-medium text-ink-faint">Terminated</span>
                    )}
                    {e.employment_status === 'active' && e.availability_note !== null && (
                      <span className="block truncate text-xs text-ink-muted">
                        {e.availability_note}
                      </span>
                    )}
                  </span>
                </span>
              </td>
              <td className={`${rowCellCls} text-ink-muted`}>{deptName(e.department_id)}</td>
              <td className={rowCellCls}>
                <Badge tone={e.pay_type === 'hourly' ? 'warn' : 'ok'}>
                  {titleCase(e.pay_type)}
                </Badge>
              </td>
              <td className={numCellCls}>
                {w === undefined ? (
                  <span className="text-ink-faint">—</span>
                ) : (
                  fmtHours(Number(w.hours))
                )}
              </td>
              <td className={numCellCls}>
                {/* Two different em dashes, deliberately indistinguishable to
                    the eye: no facts for this window, and facts withheld from
                    this role. Spelling the second one out ("hidden") would
                    tell a department manager which colleagues carry cost. */}
                {w === undefined || w.est_cost === null ? (
                  <span className="text-ink-faint">—</span>
                ) : (
                  <span className="font-medium text-ink">{money.format(Number(w.est_cost))}</span>
                )}
              </td>
              <td className={`${rowCellCls} text-right`}>
                <span className="inline-flex items-center justify-end gap-1">{actions(e)}</span>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

// --- Departments manager -----------------------------------------------------



// --- Departments manager -----------------------------------------------------

// --- Departments manager -----------------------------------------------------

function DepartmentsCard({ propertyId }: { propertyId: string | undefined }) {
  const qc = useQueryClient()
  const departments = useQuery({
    queryKey: ['departments', propertyId],
    queryFn: () => getDepartments(propertyId!),
    enabled: propertyId !== undefined,
  })
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: () => createDepartment({ property_id: propertyId!, name }),
    onSuccess: () => setName(''),
    onSettled: () => qc.invalidateQueries({ queryKey: ['departments', propertyId] }),
  })
  return (
    <Card role="region" aria-label="departments">
      <h2 className={`${cardLabelClass} mb-3 text-sm`}>Departments</h2>
      {departments.data !== undefined && departments.data.length === 0 && (
        <p className="mb-2 text-sm text-ink-muted">
          No departments yet. Add Rooms, F&amp;B, Front Office… — shifts and standards hang off
          these.
        </p>
      )}
      <ul className="mb-3 flex flex-col gap-1.5">
        {(departments.data ?? []).map((d) => (
          <li
            key={d.department_id}
            className="flex items-center gap-2.5 rounded-lg border border-line bg-surface px-3 py-2 text-sm"
          >
            <span aria-hidden="true" className="inline-block size-1.5 rounded-full bg-accent/70" />
            <span className="min-w-0 flex-1 truncate font-medium text-ink">{d.name}</span>
            <span className="text-xs text-ink-faint">#{d.department_id}</span>
          </li>
        ))}
      </ul>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (name.trim() !== '') create.mutate()
        }}
      >
        <input
          className={`${controlClass} min-w-0 flex-1`}
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="Department Name"
          placeholder="e.g. Housekeeping"
        />
        <button
          type="submit"
          disabled={create.isPending || name.trim() === '' || propertyId === undefined}
          className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >
          Add
        </button>
      </form>
      {create.isError && (
        <p className="mt-2 text-sm text-danger-red">Add failed: {errorMessage(create.error)}</p>
      )}
    </Card>
  )
}

// --- Face enrollment sub-form (Pillar F) -------------------------------------
// The Employees-page end of the photo-first kiosk: capture (or upload) a
// reference photo; the server embeds it and keeps the template encrypted.
// The embedding has no read path, so this surface only ever shows STATUS —
// enrolled or not, which model, when. Server-side refusals (matching dark,
// unencoded jurisdiction, no face in frame) surface verbatim as errors.

function FaceSubForm({
  employeeId,
  employeeName,
  onClose,
}: {
  employeeId: number
  employeeName: string
  onClose: () => void
}) {
  const qc = useQueryClient()
  const status = useQuery({
    queryKey: ['face-template', employeeId],
    queryFn: () => getFaceTemplate(employeeId),
  })

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [camError, setCamError] = useState(false)
  useEffect(() => {
    let stream: MediaStream | null = null
    navigator.mediaDevices
      ?.getUserMedia({ video: true })
      .then((s) => {
        stream = s
        if (videoRef.current) videoRef.current.srcObject = s
      })
      .catch(() => setCamError(true))
    return () => stream?.getTracks().forEach((t) => t.stop())
  }, [])

  const enroll = useMutation({
    mutationFn: (photo: Blob) => enrollFaceTemplate(employeeId, photo),
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: ['face-template', employeeId] }),
  })
  const remove = useMutation({
    mutationFn: () => deleteFaceTemplate(employeeId),
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: ['face-template', employeeId] }),
  })

  async function captureAndEnroll() {
    const canvas = canvasRef.current
    const video = videoRef.current
    if (!canvas) return
    if (video) {
      canvas.width = video.videoWidth || 320
      canvas.height = video.videoHeight || 240
      canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
    }
    const blob = await new Promise<Blob>((resolve) => {
      canvas.toBlob((b) => resolve(b ?? new Blob()), 'image/jpeg', 0.9)
    })
    enroll.mutate(blob)
  }

  const s = status.data
  return (
    <Modal
      title="Face Enrollment"
      size="md"
      subtitle={`${employeeName} — the reference photo the kiosk matches against. Only enrollment status is shown; the template never leaves the server.`}
      onClose={onClose}
    >
      {/* The landmark stays on an inner element: the modal owns the dialog
          role, and the tests locate this surface by its region name. */}
      <div role="region" aria-label="face enrollment" className="flex flex-col gap-4">
        <div className="flex items-center gap-2 text-sm">
          {s?.enrolled
            ? <Badge tone="ok">enrolled</Badge>
            : <Badge tone="neutral">not enrolled</Badge>}
          {s?.enrolled && s.model_version && (
            <span className="text-xs text-ink-muted">
              {s.model_version}
              {s.created_at ? ` · ${s.created_at.slice(0, 10)}` : ''}
            </span>
          )}
        </div>
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className="h-56 w-full rounded-card bg-surface-sunken object-cover"
        />
        <canvas ref={canvasRef} className="hidden" />
        {camError && (
          <p className="text-xs text-ink-muted">Camera unavailable — upload a JPEG instead.</p>
        )}
        <div className="flex flex-wrap items-center gap-3 border-t border-line pt-5">
          <button
            type="button"
            onClick={() => void captureAndEnroll()}
            disabled={enroll.isPending || camError}
            className={submitButtonClass}
          >{s?.enrolled ? 'Retake & Replace' : 'Capture & Enroll'}</button>
          <label className={`${cancelButtonClass} flex cursor-pointer items-center`}>
            Upload a JPEG
            <input
              type="file"
              accept="image/jpeg"
              className="hidden"
              aria-label="Upload Face Photo"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) enroll.mutate(f)
                e.target.value = ''
              }}
            />
          </label>
          {s?.enrolled && (
            <button
              type="button"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
              className="ml-auto h-10 rounded-control border border-line px-4 text-sm font-medium text-danger-red hover:bg-danger-red-soft"
            >Remove</button>
          )}
        </div>
        {enroll.isSuccess && (
          <p className="text-sm text-ink-muted">
            Enrolled — the kiosk can now match {employeeName}.
          </p>
        )}
        {enroll.isError && (
          <p className="text-sm text-danger-red">Enroll failed: {errorMessage(enroll.error)}</p>
        )}
        {remove.isError && (
          <p className="text-sm text-danger-red">Remove failed: {errorMessage(remove.error)}</p>
        )}
      </div>
    </Modal>
  )
}

// --- shared form bits --------------------------------------------------------

function Field({
  label,
  hint,
  children,
}: {
  label: string
  /** Sits under the control, not beside the label, so a long note never pushes
   *  the field it belongs to out of alignment with its neighbour. */
  hint?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      <span className="text-[13px] font-semibold text-ink">{label}</span>
      {children}
      {hint !== undefined && <span className="text-xs leading-relaxed text-ink-muted">{hint}</span>}
    </label>
  )
}

/** Right-aligned footer for every form in this page's modals. */
function ModalActions({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-2 flex items-center justify-end gap-3 border-t border-line pt-5">
      {children}
    </div>
  )
}

const cancelButtonClass =
  'h-10 rounded-control border border-line px-4 text-sm font-medium text-ink-muted ' +
  'hover:bg-surface-sunken'
const submitButtonClass =
  'h-10 rounded-control bg-accent px-5 text-sm font-semibold text-accent-contrast ' +
  'transition-opacity hover:opacity-90 disabled:opacity-50'

/** The pay-rate input. Rendered for everyone so the field is discoverable, but
 *  DISABLED for a role the backend would refuse — an accountant or department
 *  manager can read the roster and cannot set pay. A hidden field would just
 *  read as a missing feature; a disabled one says whose job it is. */
function RateField({
  value,
  onChange,
  enabled,
  payType,
}: {
  value: string
  onChange: (v: string) => void
  enabled: boolean
  payType: string
}) {
  return (
    <Field
      label={payType === 'salary' ? 'Hourly Equivalent' : 'Pay Rate'}
      hint={
        !enabled
          ? 'Your role cannot set a pay rate — a GM, org admin, or payroll admin can.'
          : payType === 'salary'
            ? 'Salaried staff are not costed hourly; this rate is stored but only prices hourly work.'
            : undefined
      }
    >
      <span className="relative flex items-center">
        <span aria-hidden="true" className="absolute left-3.5 text-sm text-ink-muted">
          $
        </span>
        {/* pr-16 reserves the suffix's own column: without it a five-figure
            rate runs underneath "/ hour". */}
        <input
          className={`${controlLargeClass} pl-8 pr-16`}
          type="number"
          min="0"
          step="0.01"
          value={value}
          disabled={!enabled}
          onChange={(e) => onChange(e.target.value)}
          aria-label="Pay Rate"
          placeholder={enabled ? '24.50' : 'set by a payroll admin'}
        />
        <span aria-hidden="true" className="absolute right-3.5 text-xs text-ink-muted">
          / hour
        </span>
      </span>
    </Field>
  )
}

// --- Onboarding modal --------------------------------------------------------

function OnboardModal({
  propertyId,
  departments,
  canSetRate,
  onClose,
}: {
  propertyId: string
  departments: Department[]
  canSetRate: boolean
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [payType, setPayType] = useState('hourly')
  const [rate, setRate] = useState('')
  const [role, setRole] = useState('')
  const [departmentId, setDepartmentId] = useState('')

  const onboard = useMutation({
    mutationFn: async () => {
      const created = await onboardEmployee({
        full_name: fullName,
        email: email || null,
        property: propertyId,
        pay_type: payType,
        role: role || null,
        department_id: departmentId === '' ? null : Number(departmentId),
      })
      // The rate is a SECOND call on purpose: onboarding is open to a GM, the
      // rate is payroll_admin-only. Keeping them separate means a GM can still
      // hire someone; the rate simply arrives later, from someone allowed to
      // set it. A failure here must not read as "the hire failed" — the person
      // exists either way, so it surfaces as its own message.
      if (canSetRate && rate !== '') {
        await setPayRate(created.employee_id, { pay_rate: rate })
      }
      return created
    },
    onSuccess: () => onClose(),
    onSettled: () => qc.invalidateQueries({ queryKey: ['employees'] }),
  })

  return (
    <Modal
      title="Add Employee"
      size="md"
      subtitle={`Onboarding into ${propertyId}. An operator role provisions a login and requires an email.`}
      onClose={onClose}
    >
      <form
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault()
          onboard.mutate()
        }}
      >
        <div className="grid gap-x-5 gap-y-5 sm:grid-cols-2">
          <Field label="Full Name">
            <input
              className={controlLargeClass}
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              required
              autoFocus
              aria-label="Full Name"
              placeholder="e.g. Maria Lopez"
            />
          </Field>
          <Field label="Email">
            <input
              className={controlLargeClass}
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              aria-label="Email"
              placeholder="required for operator roles"
            />
          </Field>
          <Field label="Role">
            <select
              className={controlLargeClass}
              value={role}
              onChange={(e) => setRole(e.target.value)}
              aria-label="Role"
            >
              <option value="">No Login (Hourly Staff)</option>
              {OPERATOR_ROLES.map((r) => (
                <option key={r.value} value={r.value}>{r.label}</option>
              ))}
            </select>
          </Field>
          <Field label="Department">
            <select
              className={controlLargeClass}
              value={departmentId}
              onChange={(e) => setDepartmentId(e.target.value)}
              aria-label="Department"
            >
              <option value="">No Department Yet</option>
              {departments.map((d) => (
                <option key={d.department_id} value={d.department_id}>{d.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Pay Type">
            <select
              className={controlLargeClass}
              value={payType}
              onChange={(e) => setPayType(e.target.value)}
              aria-label="Pay Type"
            >
              <option value="hourly">Hourly</option>
              <option value="salary">Salary</option>
            </select>
          </Field>
          <RateField value={rate} onChange={setRate} enabled={canSetRate} payType={payType} />
        </div>
        <ModalActions>
          <button type="button" onClick={onClose} className={cancelButtonClass}>
            Cancel
          </button>
          <button
            type="submit"
            disabled={onboard.isPending || fullName === ''}
            className={submitButtonClass}
          >
            Onboard
          </button>
        </ModalActions>
      </form>
      {onboard.isError && (
        <p className="mt-3 text-sm text-danger-red">Onboard failed: {errorMessage(onboard.error)}</p>
      )}
    </Modal>
  )
}

// --- Edit modal --------------------------------------------------------------

function EditModal({
  employee,
  departments,
  canSetRate,
  onClose,
}: {
  employee: Employee
  departments: Department[]
  canSetRate: boolean
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [fullName, setFullName] = useState(employee.full_name)
  const [payType, setPayType] = useState(employee.pay_type)
  const [departmentId, setDepartmentId] = useState(
    employee.department_id === null ? '' : String(employee.department_id),
  )
  const [rate, setRate] = useState('')

  // Reading a rate writes an audit row server-side, so it is fetched only when
  // the caller may actually see one and only while this modal is open.
  const current = useQuery({
    queryKey: ['pay-rate', employee.employee_id],
    queryFn: () => getPayRate(employee.employee_id),
    enabled: canSetRate,
  })
  const storedRate = current.data?.pay_rate ?? null
  const rateValue = rate === '' && storedRate !== null ? storedRate : rate

  const save = useMutation({
    mutationFn: async () => {
      const patch: Record<string, string | number> = {}
      if (fullName !== employee.full_name) patch.full_name = fullName
      if (payType !== employee.pay_type) patch.pay_type = payType
      const nextDept = departmentId === '' ? null : Number(departmentId)
      if (nextDept !== null && nextDept !== employee.department_id) patch.department_id = nextDept
      if (Object.keys(patch).length > 0) await updateEmployee(employee.employee_id, patch)
      if (canSetRate && rateValue !== '' && rateValue !== storedRate) {
        await setPayRate(employee.employee_id, { pay_rate: rateValue })
      }
    },
    onSuccess: () => onClose(),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['employees'] })
      void qc.invalidateQueries({ queryKey: ['pay-rate', employee.employee_id] })
    },
  })

  const movingDepartment =
    departmentId !== '' && Number(departmentId) !== employee.department_id

  return (
    <Modal title="Edit Employee" size="md" subtitle={employee.full_name} onClose={onClose}>
      <form
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        <div className="grid gap-x-5 gap-y-5 sm:grid-cols-2">
          <Field label="Full Name">
            <input
              className={controlLargeClass}
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              required
              autoFocus
              aria-label="Full Name"
            />
          </Field>
          <Field label="Pay Type">
            <select
              className={controlLargeClass}
              value={payType}
              onChange={(e) => setPayType(e.target.value)}
              aria-label="Pay Type"
            >
              <option value="hourly">Hourly</option>
              <option value="salary">Salary</option>
            </select>
          </Field>
          <Field label="Department">
            <select
              className={controlLargeClass}
              value={departmentId}
              onChange={(e) => setDepartmentId(e.target.value)}
              aria-label="Department"
            >
              <option value="">No Department</option>
              {departments.map((d) => (
                <option key={d.department_id} value={d.department_id}>{d.name}</option>
              ))}
            </select>
          </Field>
          <RateField
            value={rateValue}
            onChange={setRate}
            enabled={canSetRate}
            payType={payType}
          />
        </div>
        {movingDepartment && (
          <p className="rounded-lg border border-line bg-surface-sunken px-4 py-3 text-xs leading-relaxed text-ink-muted">
            A department change takes effect today and opens a new placement. Hours already worked
            stay costed against the old department, so the statement does not move.
          </p>
        )}
        <ModalActions>
          <button type="button" onClick={onClose} className={cancelButtonClass}>
            Cancel
          </button>
          <button
            type="submit"
            disabled={save.isPending || fullName.trim() === ''}
            className={submitButtonClass}
          >
            Save Changes
          </button>
        </ModalActions>
      </form>
      {save.isError && (
        <p className="mt-3 text-sm text-danger-red">Save failed: {errorMessage(save.error)}</p>
      )}
    </Modal>
  )
}

// --- Payroll PII sub-form (blind overwrite) ---------------------------------
// Sealed client-side and PUT as opaque envelopes: a raw value is NEVER sent and
// NEVER shown. Inputs start empty and reset on save; a stored field surfaces only
// as an "on file" badge driven by the status query (the vault has no read path).

function OnFileBadge({ present }: { present: boolean }) {
  return present
    ? <Badge tone="ok">on file</Badge>
    : <Badge tone="neutral">not on file</Badge>
}

function PayrollSubForm({
  employeeId,
  employeeName,
  onClose,
}: {
  employeeId: number
  employeeName: string
  onClose: () => void
}) {
  const qc = useQueryClient()
  // The pinned recipient key — cached across employees so switching rows does
  // not re-fetch. Substituting it would silently defeat the sealing, so it is
  // served on the authenticated channel and pinned here.
  const pubkey = useQuery({ queryKey: ['payroll-pubkey'], queryFn: getPayrollPublicKey })
  const status = useQuery({
    queryKey: ['payroll-profile', employeeId],
    queryFn: () => getPayrollProfile(employeeId),
  })

  const chain = useQuery({
    queryKey: ['deposit-accounts', employeeId],
    queryFn: () => getDepositAccounts(employeeId),
  })

  const [ssn, setSsn] = useState('')
  const [taxElections, setTaxElections] = useState('')
  // Draft deposit chain — full re-entry: saving REPLACES the whole chain
  // (the C1 blind-overwrite contract generalized; ordinals come from array
  // position server-side). The last row is the remainder by construction.
  const emptyDraft = { account: '', routing: '', accountType: 'checking',
                       allocationType: 'remainder', allocationValue: '' }
  const [drafts, setDrafts] = useState([{ ...emptyDraft }])

  const save = useMutation({
    mutationFn: async () => {
      const key = pubkey.data
      if (!key) throw new Error('recipient public key not loaded')
      // Seal each PROVIDED field client-side; aad binds it to employee_id:field.
      const body: SealedProfileBody = {}
      if (ssn) body.ssn = await sealField(key.public_key, key.key_id, ssn, `${employeeId}:ssn`)
      if (taxElections)
        body.tax_elections = await sealField(key.public_key, key.key_id, taxElections, `${employeeId}:tax_elections`)
      return putPayrollProfile(employeeId, body)
    },
    onSuccess: () => {
      setSsn(''); setTaxElections('')
      void qc.invalidateQueries({ queryKey: ['payroll-profile', employeeId] })
    },
  })

  const saveChain = useMutation({
    mutationFn: async () => {
      const key = pubkey.data
      if (!key) throw new Error('recipient public key not loaded')
      // Every row seals fresh under its per-ordinal slot aad — the ordinal in
      // the aad is what makes two accounts' ciphertexts non-interchangeable.
      const accounts: DepositAccountBody[] = []
      for (const [i, d] of drafts.entries()) {
        const ordinal = i + 1
        accounts.push({
          allocation_type: d.allocationType as AllocationType,
          allocation_value: d.allocationType === 'remainder' ? null : d.allocationValue,
          account_type: d.accountType as 'checking' | 'savings',
          sealed_account: await sealField(
            key.public_key, key.key_id, d.account, depositAad(employeeId, ordinal, 'account')),
          sealed_routing: await sealField(
            key.public_key, key.key_id, d.routing, depositAad(employeeId, ordinal, 'routing')),
        })
      }
      return putDepositAccounts(employeeId, { accounts })
    },
    onSuccess: () => {
      setDrafts([{ ...emptyDraft }])
      void qc.invalidateQueries({ queryKey: ['deposit-accounts', employeeId] })
    },
  })

  const s = status.data
  const disabled = save.isPending || pubkey.isPending

  return (
    <Modal
      title="Payroll Details"
      size="md"
      subtitle={`${employeeName} — enter a value to seal and store it. Values are write-only and never displayed.`}
      onClose={onClose}
    >
      <div role="region" aria-label="payroll profile" className="flex flex-col gap-5">
        {pubkey.isError && (
          <p className="text-sm text-danger-red">
            Could not load the sealing key: {errorMessage(pubkey.error)}
          </p>
        )}
        <form
          className="flex flex-col gap-5"
          onSubmit={(e) => { e.preventDefault(); save.mutate() }}
        >
          <div className="grid gap-x-5 gap-y-5 sm:grid-cols-2">
            <Field
              label="SSN"
              hint={<OnFileBadge present={s?.ssn_on_file ?? false} />}
            >
              <input className={controlLargeClass} value={ssn} autoComplete="off"
                onChange={(e) => setSsn(e.target.value)} aria-label="SSN"
                placeholder="•••-••-••••" />
            </Field>
            <Field
              label="Tax Elections"
              hint={<OnFileBadge present={s?.tax_elections_on_file ?? false} />}
            >
              <input className={controlLargeClass} value={taxElections} autoComplete="off"
                onChange={(e) => setTaxElections(e.target.value)} aria-label="Tax Elections" />
            </Field>
          </div>
          <ModalActions>
            <button type="submit" disabled={disabled} className={submitButtonClass}>Save</button>
          </ModalActions>
        </form>
        {save.isError && (
          <p className="text-sm text-danger-red">Save failed: {errorMessage(save.error)}</p>
        )}

        <div role="region" aria-label="deposit accounts" className="border-t border-line pt-5">
          <h3 className="text-sm font-semibold text-ink">Deposit Accounts</h3>
          <p className="mb-4 mt-1 text-xs leading-relaxed text-ink-muted">
            Net pay routes through the chain in order; the last account receives
            the remainder. Saving replaces the whole chain — enter every account.
          </p>
          {chain.data && chain.data.accounts.length > 0 && (
            <ul className="mb-4 flex flex-col gap-1.5 text-xs text-ink-muted">
              {chain.data.accounts.map((a) => (
                <li key={a.ordinal}>
                  #{a.ordinal} {a.allocation_type}
                  {a.allocation_value ? ` ${a.allocation_value}` : ''} — {a.account_type} —{' '}
                  account <OnFileBadge present={a.account_on_file} />{' '}
                  routing <OnFileBadge present={a.routing_on_file} />
                </li>
              ))}
            </ul>
          )}
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => { e.preventDefault(); saveChain.mutate() }}
          >
            {drafts.map((d, i) => {
              const last = i === drafts.length - 1
              const set = (patch: Partial<typeof d>) =>
                setDrafts(drafts.map((row, j) => (j === i ? { ...row, ...patch } : row)))
              return (
                <fieldset
                  key={i}
                  className="grid gap-x-5 gap-y-4 rounded-card border border-line bg-surface-sunken/40 p-5 sm:grid-cols-2"
                >
                  <legend className="px-1.5 text-xs font-semibold text-ink-muted">
                    Account {i + 1}{last ? ' (Remainder)' : ''}
                  </legend>
                  {!last && (
                    <>
                      <Field label="Allocation Type">
                        <select className={controlLargeClass} value={d.allocationType}
                          onChange={(e) => set({ allocationType: e.target.value })}
                          aria-label={`Allocation Type ${i + 1}`}>
                          <option value="amount">Amount ($)</option>
                          <option value="percent">Percent (%)</option>
                        </select>
                      </Field>
                      <Field label="Allocation Value">
                        <input className={controlLargeClass} value={d.allocationValue}
                          onChange={(e) => set({ allocationValue: e.target.value })}
                          aria-label={`Allocation Value ${i + 1}`} placeholder="50.00" />
                      </Field>
                    </>
                  )}
                  <Field label="Account Type">
                    <select className={controlLargeClass} value={d.accountType}
                      onChange={(e) => set({ accountType: e.target.value })}
                      aria-label={`Account Type ${i + 1}`}>
                      <option value="checking">Checking</option>
                      <option value="savings">Savings</option>
                    </select>
                  </Field>
                  <Field label="Bank Account">
                    <input className={controlLargeClass} value={d.account} autoComplete="off"
                      onChange={(e) => set({ account: e.target.value })}
                      aria-label={`Bank Account ${i + 1}`} />
                  </Field>
                  <Field label="Bank Routing">
                    <input className={controlLargeClass} value={d.routing} autoComplete="off"
                      onChange={(e) => set({ routing: e.target.value })}
                      aria-label={`Bank Routing ${i + 1}`} />
                  </Field>
                </fieldset>
              )
            })}
            <div className="flex flex-wrap items-center gap-3 border-t border-line pt-5">
              <button type="button"
                onClick={() => setDrafts([
                  { ...emptyDraft, allocationType: 'amount' },
                  ...drafts.slice(0, -1).map((d) => ({ ...d })),
                  { ...drafts[drafts.length - 1]! },
                ])}
                className={cancelButtonClass}
              >Add Account</button>
              {drafts.length > 1 && (
                <button type="button"
                  onClick={() => setDrafts(drafts.filter((_, j) => j !== 0))}
                  className={cancelButtonClass}
                >Remove First</button>
              )}
              <button
                type="submit"
                disabled={saveChain.isPending || pubkey.isPending
                  || drafts.some((d) => !d.account || !d.routing)}
                className={`${submitButtonClass} ml-auto`}
              >Replace Chain</button>
            </div>
          </form>
          {saveChain.isError && (
            <p className="mt-3 text-sm text-danger-red">
              Save failed: {errorMessage(saveChain.error)}
            </p>
          )}
        </div>
      </div>
    </Modal>
  )
}
