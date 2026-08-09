import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Badge,
  Card,
  PageHeader,
  amountCellClass,
  amountHeadClass,
  cellClass,
  controlClass,
  headCellClass,
  tableClass,
} from '../components/ui'
import {
  ApiError,
  createScheduleWeek,
  createShift,
  createTemplate,
  deleteShift,
  deleteStandard,
  deleteTemplate,
  getAdherence,
  getDepartments,
  getEmployees,
  getForecast,
  getMe,
  getProjection,
  getDemand,
  getScheduleWeek,
  getStandards,
  getTargets,
  getTemplates,
  publishSchedule,
  putAvailabilityNote,
  saveForecast,
  updateShift,
  upsertStandard,
} from '../api/client'
import type {
  AdherenceException,
  DemandCapabilities,
  DemandDay,
  DemandSurface,
  Department,
  Employee,
  ForecastDay,
  LaborStandard,
  ScheduleProjectionWarning,
  ScheduleShift,
  ScheduleWeek,
  ShiftTemplate,
  StandardBasis,
  TargetDay,
} from '../api/types'
import { fmtMoney } from '../lib/format'
import Modal from '../components/Modal'
import { hasRole } from '../lib/roles'
import { useGlobalProperty } from '../lib/propertyContext'
import { errorMessage } from '../lib/errors'
import { addDays, dayName } from '../lib/week'

// Schedule week builder (Pillar D1): assemble next week's shifts from
// templates on the payroll Monday grid. The server is AUTHORITATIVE on that
// grid — the week picker accepts any date and an off-grid pick surfaces the
// API's 422 detail verbatim instead of duplicating the anchor client-side.
// A 404 on the week query is not an error: the week simply doesn't exist yet,
// so it renders as the "Create week" button.
//
// THE MONEY DISCIPLINE (B3/C2/C3, carried here): per-employee figures are
// HOURS ONLY — money appears solely as department aggregates (suppressed to
// null below two PRICED employees) in the projection panel. Because no
// per-employee money exists anywhere on this page, the print view cannot leak
// it either.
//
// Print: "Print week" is window.print() + Tailwind `print:` variants — every
// panel except the week grid carries print:hidden, so only the wall grid prints.

/** Monday of the current week — the payroll grid anchor is a Monday, so every
    Monday is on-grid and the server never 422s these. */
function currentMonday(): string {
  const d = new Date()
  const monday = new Date(d)
  monday.setDate(d.getDate() - ((d.getDay() + 6) % 7))
  const p2 = (n: number) => String(n).padStart(2, '0')
  return `${monday.getFullYear()}-${p2(monday.getMonth() + 1)}-${p2(monday.getDate())}`
}

/** ISO -> MM/DD/YYYY. Slashes read as a date at a glance; the ISO form reads as
 *  a database column. The wire stays ISO — this is display only. */
function usDate(isoDate: string): string {
  const [y = '', m = '', d = ''] = isoDate.split('-')
  return `${m}/${d}/${y}`
}

function weekLabel(weekStart: string): string {
  return `${usDate(weekStart)} – ${usDate(addDays(weekStart, 6))}`
}

/** Today as ISO, in local time — the boundary a past day is measured against. */
function todayIso(): string {
  const d = new Date()
  const p2 = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`
}

/** The Monday on or before `isoDate`. The picker accepts any date and snaps,
 *  so a GM can jump to a week by picking any day inside it. */
function mondayOf(isoDate: string): string {
  const d = new Date(`${isoDate}T00:00:00`)
  return addDays(isoDate, -((d.getDay() + 6) % 7))
}

/** Raw span hours of a shift (no meal deduction — the projection owns that). */
function shiftHours(sh: { start_time: string; end_time: string; crosses_midnight: boolean }): number {
  const [sh1 = 0, sm = 0] = sh.start_time.split(':').map(Number)
  const [eh = 0, em = 0] = sh.end_time.split(':').map(Number)
  let dur = eh + em / 60 - (sh1 + sm / 60)
  if (sh.crosses_midnight) dur += 24
  return Math.max(0, dur)
}

function fmtHM(hours: number): string {
  const h = Math.floor(hours)
  const m = Math.round((hours - h) * 60)
  return `${h}:${String(m).padStart(2, '0')}`
}

function shiftLabel(s: ScheduleShift, nameOf: (id: number | null) => string): string {
  return `${s.start_time}–${s.end_time}${s.crosses_midnight ? ' (+1d)' : ''} ${nameOf(s.employee_id)}`
}

/** One number in the week bar. Small, quiet, and next to the week it counts. */
function WeekStat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5 text-xs">
      <span className={`text-sm font-semibold tabular-nums ${tone ?? 'text-ink'}`}>{value}</span>
      <span className="text-ink-muted">{label}</span>
    </span>
  )
}

export default function SchedulePage() {
  const qc = useQueryClient()
  const me = useQuery({ queryKey: ['me'], queryFn: getMe })
  const { property: globalProperty } = useGlobalProperty()
  const employees = useQuery({ queryKey: ['employees'], queryFn: () => getEmployees() })
  // Mirrors the backend's require_scheduler (org_admin | property_gm) — the
  // same gate as the Timecards nav link.
  const canSchedule = hasRole(me.data, 'org_admin', 'property_gm')

  const [weekStart, setWeekStart] = useState(currentMonday)
  const [selectedShiftId, setSelectedShiftId] = useState<number | null>(null)
  const [deptFilter, setDeptFilter] = useState('')

  const propertyId = globalProperty ?? ''

  const departments = useQuery({
    queryKey: ['departments', propertyId],
    queryFn: () => getDepartments(propertyId),
    enabled: canSchedule && propertyId !== '',
  })
  const deptNameOf = (id: number): string =>
    departments.data?.find((d) => d.department_id === id)?.name ?? `Dept ${id}`

  const templates = useQuery({
    queryKey: ['schedule-templates', propertyId],
    queryFn: () => getTemplates(propertyId),
    enabled: canSchedule && propertyId !== '',
  })

  const week = useQuery({
    queryKey: ['schedule-week', propertyId, weekStart],
    queryFn: () => getScheduleWeek(propertyId, weekStart),
    enabled: canSchedule && propertyId !== '' && weekStart !== '',
    // A 404 is the expected "no week yet" answer — retrying it three times
    // would only delay the Create week button.
    retry: false,
  })
  const weekMissing =
    week.isError && week.error instanceof ApiError && week.error.status === 404

  const createWeek = useMutation({
    mutationFn: () => createScheduleWeek({ property: propertyId, week_start: weekStart }),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: ['schedule-week', propertyId, weekStart] }),
  })

  // The Inn-Flow behavior: no "create week" ceremony. The week record is
  // created silently the first time something needs it (a shift add / drop).
  const ensureWeek = async (): Promise<number> => {
    if (week.data !== undefined) return week.data.schedule_id
    const created = await createWeek.mutateAsync()
    return created.schedule_id
  }

  // CRM demand hints (Pillar J). Decision-support only: configured:false
  // (feature off) and a failed fetch both render NO demand UI — a hint
  // surface degrading must never block scheduling.
  const demandQuery = useQuery({
    queryKey: ['crm-demand', propertyId, weekStart],
    queryFn: () => getDemand(propertyId, weekStart, addDays(weekStart, 6)),
    enabled: canSchedule && propertyId !== '' && weekStart !== '',
  })
  const demand = demandQuery.data?.configured ? demandQuery.data : undefined

  const publish = useMutation({
    mutationFn: async () => publishSchedule(await ensureWeek()),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['schedule-week'] }),
  })

  const roster = (employees.data ?? []).filter((e) => e.property_id === propertyId)
  const nameOf = (id: number | null): string => {
    if (id === null) return 'OPEN'
    return employees.data?.find((e) => e.employee_id === id)?.full_name ?? `#${id}`
  }

  // The selected shift always re-reads from the CURRENT week data, so a
  // refetch after an edit never leaves a stale copy in the editor.
  const selectedShift =
    week.data?.shifts.find((s) => s.shift_id === selectedShiftId) ?? null

  if (me.data && !canSchedule) {
    return (
      <div className="flex flex-col gap-5">
        <PageHeader title="Schedule" />
        <Card>
          <p className="text-sm text-ink-muted">
            Scheduling requires the org_admin or property_gm role.
          </p>
        </Card>
      </div>
    )
  }

  const shifts = week.data?.shifts ?? []
  const openShifts = shifts.filter((s) => s.employee_id === null).length
  const scheduledHours = shifts.reduce((acc, s) => acc + shiftHours(s), 0)
  const staffScheduled = new Set(
    shifts.filter((s) => s.employee_id !== null).map((s) => s.employee_id),
  ).size
  const goToWeek = (nextMonday: string) => {
    setWeekStart(nextMonday)
    setSelectedShiftId(null)
  }

  return (
    <div className="flex flex-col gap-5">
      {/* Header + week bar in ONE card: the title, the week it applies to, and
          the counts that describe that week belong to the same statement. */}
      <Card className="p-0 print:hidden">
        <div className="flex flex-wrap items-start justify-between gap-4 px-5 py-4">
          <div className="min-w-0">
            <h1 className="text-2xl font-bold tracking-tight text-ink">Schedule</h1>
            <p className="mt-1 text-sm text-ink-muted">
              Build the week from shift templates on the payroll Monday grid.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {week.data?.status === 'published' ? (
              <Badge tone="ok">
                {`v${week.data.version} published ${week.data.published_at?.slice(0, 10) ?? ''}`.trim()}
              </Badge>
            ) : (
              <Badge tone="neutral">draft</Badge>
            )}
            <select
              aria-label="Filter by department"
              value={deptFilter}
              onChange={(e) => setDeptFilter(e.target.value)}
              className={`${controlClass} w-40 py-1.5`}
            >
              <option value="">All departments</option>
              {(departments.data ?? []).map((d) => (
                <option key={d.department_id} value={String(d.department_id)}>{d.name}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => window.print()}
              className="rounded-control border border-line px-3 py-1.5 text-sm text-ink-muted hover:bg-surface-sunken"
            >Print</button>
            <button
              type="button"
              onClick={() => publish.mutate()}
              disabled={publish.isPending}
              className="rounded-control bg-accent px-4 py-1.5 text-sm font-semibold text-accent-contrast disabled:opacity-50"
            >
              {week.data?.status === 'published'
                ? `Republish (v${week.data.version + 1})`
                : 'Publish'}
            </button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line px-5 py-2.5">
          <div className="flex items-center gap-1">
            <button
              type="button"
              aria-label="Previous week"
              onClick={() => goToWeek(addDays(weekStart, -7))}
              className="grid size-8 place-items-center rounded-lg border border-line text-ink-muted hover:bg-surface-sunken"
            >‹</button>
            <button
              type="button"
              aria-label="Next week"
              onClick={() => goToWeek(addDays(weekStart, 7))}
              className="grid size-8 place-items-center rounded-lg border border-line text-ink-muted hover:bg-surface-sunken"
            >›</button>
          </div>
          <span className="text-sm font-semibold tabular-nums text-ink">
            {weekLabel(weekStart)}
          </span>
          {/* Any date is accepted and snapped back to its Monday, so jumping to
              a week never lands off the payroll grid the server enforces. */}
          <input
            type="date"
            aria-label="Jump to week"
            value={weekStart}
            onChange={(e) => {
              if (e.target.value !== '') goToWeek(mondayOf(e.target.value))
            }}
            className={`${controlClass} w-[10.5rem] py-1.5`}
          />
          <button
            type="button"
            onClick={() => goToWeek(currentMonday())}
            className="rounded-lg border border-line px-3 py-1.5 text-xs font-medium text-ink-muted hover:bg-surface-sunken"
          >This week</button>

          <span className="mx-1 hidden h-6 w-px bg-line sm:block" />

          <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1">
            <WeekStat label="shifts" value={String(shifts.length)} />
            <WeekStat label="scheduled" value={fmtHM(scheduledHours)} />
            <WeekStat label="staff on" value={String(staffScheduled)} />
            <WeekStat
              label="open"
              value={String(openShifts)}
              tone={openShifts > 0 ? 'text-danger-red' : undefined}
            />
          </div>
        </div>
        {weekMissing && (
          <p className="border-t border-line px-5 py-2 text-xs text-ink-faint">
            Empty week — it is created automatically with the first shift.
          </p>
        )}
        {(createWeek.isError || publish.isError) && (
          <p className="border-t border-line px-5 py-2 text-sm text-danger-red" role="alert">
            {createWeek.isError
              ? `Create week failed: ${errorMessage(createWeek.error)}`
              : `Publish failed: ${errorMessage(publish.error)}`}
          </p>
        )}
        {week.isError && !weekMissing && (
          <p className="border-t border-line px-5 py-2 text-sm text-danger-red">
            Failed to load: {errorMessage(week.error)}
          </p>
        )}
      </Card>

      <ScheduleBoard
        weekStart={weekStart}
        week={week.data ?? null}
        departments={(departments.data ?? []).filter(
          (d) => deptFilter === '' || String(d.department_id) === deptFilter,
        )}
        roster={roster}
        templates={templates.data ?? []}
        deptNameOf={deptNameOf}
        nameOf={nameOf}
        onSelectShift={setSelectedShiftId}
        ensureWeek={ensureWeek}
        propertyId={propertyId}
        demand={demand}
      />

      {week.data && (
        <>
          <ForecastPanel
            propertyId={propertyId}
            weekStart={week.data.week_start}
            scheduleId={week.data.schedule_id}
            demand={demand}
          />
          <TargetsPanel scheduleId={week.data.schedule_id} />
          {week.data.status === 'published' && (
            <AdherencePanel scheduleId={week.data.schedule_id} />
          )}
          <ProjectionPanel scheduleId={week.data.schedule_id} />
        </>
      )}

      {/* Setup, not daily work: templates and standards are configured once and
          then live at the bottom, out of the way of the board. */}
      <TemplatesPanel propertyId={propertyId} templates={templates.data ?? []} departments={departments.data ?? []} />
      <StandardsPanel propertyId={propertyId} departments={departments.data ?? []} />
      {selectedShift && (
        <ShiftEditor
          key={selectedShift.shift_id}
          shift={selectedShift}
          roster={roster}
          nameOf={nameOf}
          onClose={() => setSelectedShiftId(null)}
        />
      )}
    </div>
  )
}

// --- Templates panel ---------------------------------------------------------

function TemplatesPanel({
  propertyId,
  templates,
  departments,
}: {
  propertyId: string
  templates: ShiftTemplate[]
  departments: Department[]
}) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [departmentId, setDepartmentId] = useState('')
  const [startTime, setStartTime] = useState('')
  const [endTime, setEndTime] = useState('')
  const [crossesMidnight, setCrossesMidnight] = useState(false)

  const create = useMutation({
    mutationFn: () =>
      createTemplate({
        property: propertyId,
        department_id: Number(departmentId),
        name,
        start_time: startTime,
        end_time: endTime,
        crosses_midnight: crossesMidnight,
      }),
    onSuccess: () => {
      setName(''); setDepartmentId(''); setStartTime(''); setEndTime('')
      setCrossesMidnight(false)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['schedule-templates'] }),
  })

  const remove = useMutation({
    mutationFn: (id: number) => deleteTemplate(id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['schedule-templates'] }),
  })

  return (
    <Card role="region" aria-label="shift templates" className="print:hidden">
      <PageHeader level={2} title="Shift templates" />
      {templates.length === 0 ? (
        <p className="text-sm text-ink-muted">No templates yet for {propertyId}.</p>
      ) : (
        <table className={tableClass} aria-label="Shift templates">
          <thead>
            <tr className="border-b border-line">
              <th className={headCellClass}>Name</th>
              <th className={headCellClass}>Department</th>
              <th className={headCellClass}>Times</th>
              <th className={headCellClass}></th>
            </tr>
          </thead>
          <tbody>
            {templates.map((t) => (
              <tr key={t.template_id} className="border-b border-line last:border-0">
                <td className={cellClass}>{t.name}</td>
                <td className={cellClass}>{departments.find((d) => d.department_id === t.department_id)?.name ?? `Dept ${t.department_id}`}</td>
                <td className={cellClass}>
                  {t.start_time}–{t.end_time}{t.crosses_midnight ? ' (+1d)' : ''}
                </td>
                <td className={cellClass}>
                  <div className="flex justify-end">
                    <button
                      type="button"
                      onClick={() => remove.mutate(t.template_id)}
                      disabled={remove.isPending}
                      aria-label={`Delete template ${t.name}`}
                      className="rounded-control border border-line px-2 py-1 text-xs text-danger-red hover:bg-danger-red-soft"
                    >Delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {remove.isError && (
        <p className="mt-2 text-sm text-danger-red">
          Delete failed: {errorMessage(remove.error)}
        </p>
      )}
      <form
        className="mt-3 flex flex-wrap items-end gap-3"
        onSubmit={(e) => { e.preventDefault(); create.mutate() }}
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Name</span>
          <input className={controlClass} value={name} required
            onChange={(e) => setName(e.target.value)} aria-label="Template name" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Department</span>
          <select className={controlClass} value={departmentId} required
            onChange={(e) => setDepartmentId(e.target.value)} aria-label="Template department id">
            <option value="">Select…</option>
            {departments.map((d) => (
              <option key={d.department_id} value={d.department_id}>{d.name}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Start</span>
          <input className={controlClass} type="time" value={startTime} required
            onChange={(e) => setStartTime(e.target.value)} aria-label="Template start time" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">End</span>
          <input className={controlClass} type="time" value={endTime} required
            onChange={(e) => setEndTime(e.target.value)} aria-label="Template end time" />
        </label>
        <label className="flex items-center gap-2 pb-1.5 text-sm">
          <input type="checkbox" checked={crossesMidnight}
            onChange={(e) => setCrossesMidnight(e.target.checked)}
            aria-label="Template crosses midnight" />
          <span className="text-xs font-medium text-ink-muted">Crosses midnight</span>
        </label>
        <button
          type="submit"
          disabled={create.isPending || !name || !departmentId || !startTime || !endTime}
          className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >Add template</button>
      </form>
      {create.isError && (
        <p className="mt-2 text-sm text-danger-red">
          Create failed: {errorMessage(create.error)}
        </p>
      )}
    </Card>
  )
}

// --- Labor standards (D2) ----------------------------------------------------

// How a department's target hours derive from demand: fixed_hours_per_day is a
// constant daily target; minutes_per_occupied_room multiplies the GM's
// forecast. HOURS-only by design (THE MONEY RULE) — a standard never carries a
// rate or cost, so neither can anything derived from it.

const BASIS_LABELS: Record<string, string> = {
  fixed_hours_per_day: 'fixed hours per day',
  minutes_per_occupied_room: 'minutes per occupied room',
}

function StandardsPanel({
  propertyId,
  departments,
}: {
  propertyId: string
  departments: Department[]
}) {
  const deptName = (id: number): string =>
    departments.find((d) => d.department_id === id)?.name ?? `Dept ${id}`
  const qc = useQueryClient()
  const standards = useQuery({
    queryKey: ['schedule-standards', propertyId],
    queryFn: () => getStandards(propertyId),
    enabled: propertyId !== '',
  })

  const [departmentId, setDepartmentId] = useState('')
  const [basis, setBasis] = useState<StandardBasis>('fixed_hours_per_day')
  const [value, setValue] = useState('')

  function invalidate() {
    void qc.invalidateQueries({ queryKey: ['schedule-standards'] })
    // Targets derive from standards — the summary table must move with them.
    void qc.invalidateQueries({ queryKey: ['schedule-targets'] })
  }

  const upsert = useMutation({
    mutationFn: () =>
      upsertStandard({
        property: propertyId,
        department_id: Number(departmentId),
        basis,
        value: Number(value),
      }),
    onSuccess: () => { setDepartmentId(''); setValue('') },
    onSettled: invalidate,
  })

  const remove = useMutation({
    mutationFn: (id: number) => deleteStandard(id),
    onSettled: invalidate,
  })

  return (
    <Card role="region" aria-label="labor standards" className="print:hidden">
      <PageHeader
        level={2}
        title="Labor standards"
        subtitle="One standard per department — targets are hours-only by design."
      />
      {(standards.data ?? []).length === 0 ? (
        <p className="text-sm text-ink-muted">No standards yet for {propertyId}.</p>
      ) : (
        <table className={tableClass} aria-label="Labor standards">
          <thead>
            <tr className="border-b border-line">
              <th className={headCellClass}>Department</th>
              <th className={headCellClass}>Basis</th>
              <th className={amountHeadClass}>Value</th>
              <th className={headCellClass}></th>
            </tr>
          </thead>
          <tbody>
            {(standards.data ?? []).map((s: LaborStandard) => (
              <tr key={s.standard_id} className="border-b border-line last:border-0">
                <td className={cellClass}>{deptName(s.department_id)}</td>
                <td className={cellClass}>{BASIS_LABELS[s.basis] ?? s.basis}</td>
                <td className={amountCellClass}>{s.value}</td>
                <td className={cellClass}>
                  <div className="flex justify-end">
                    <button
                      type="button"
                      onClick={() => remove.mutate(s.standard_id)}
                      disabled={remove.isPending}
                      aria-label={`Delete standard for ${deptName(s.department_id)}`}
                      className="rounded-control border border-line px-2 py-1 text-xs text-danger-red hover:bg-danger-red-soft"
                    >Delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {remove.isError && (
        <p className="mt-2 text-sm text-danger-red">
          Delete failed: {errorMessage(remove.error)}
        </p>
      )}
      <form
        className="mt-3 flex flex-wrap items-end gap-3"
        onSubmit={(e) => { e.preventDefault(); upsert.mutate() }}
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Department</span>
          <select className={controlClass} value={departmentId} required
            onChange={(e) => setDepartmentId(e.target.value)}
            aria-label="Standard department">
            <option value="">Select…</option>
            {departments.map((d) => (
              <option key={d.department_id} value={d.department_id}>{d.name}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Basis</span>
          <select className={controlClass} value={basis}
            onChange={(e) => setBasis(e.target.value as StandardBasis)}
            aria-label="Standard basis">
            <option value="fixed_hours_per_day">fixed hours per day</option>
            <option value="minutes_per_occupied_room">minutes per occupied room</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Value</span>
          <input className={controlClass} type="number" min="0.01" step="0.01"
            value={value} required
            onChange={(e) => setValue(e.target.value)} aria-label="Standard value" />
        </label>
        <button
          type="submit"
          disabled={upsert.isPending || !departmentId || !value}
          className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >Save standard</button>
      </form>
      {upsert.isError && (
        <p className="mt-2 text-sm text-danger-red" role="alert">
          Save failed: {errorMessage(upsert.error)}
        </p>
      )}
    </Card>
  )
}

// --- Occupancy forecast (D2) -------------------------------------------------

// The GM's number is the forecast. Hints (same-day-last-week, trailing
// average of our own promoted ROOMS_OCCUPIED facts) render muted BESIDE each
// input and NEVER auto-fill it — inputs seed only from the SAVED forecast.

function hintText(d: ForecastDay): string | null {
  const parts: string[] = []
  if (d.hint_same_day_last_week !== null) parts.push(`last wk: ${d.hint_same_day_last_week}`)
  if (d.hint_trailing_avg !== null) parts.push(`avg: ${d.hint_trailing_avg}`)
  return parts.length > 0 ? parts.join(' · ') : null
}

// CRM demand chip text (Pillar J). A dimension the provider does not speak —
// capability false, or a day the provider stated no figure for — is ABSENT
// from the chip, never a 0: absence of a dimension is not zero demand (the
// D2 forecast-null rule, applied to the feed).
function demandFigures(d: DemandDay, caps: DemandCapabilities): string | null {
  const parts: string[] = []
  if (caps.rooms_on_books && d.rooms_on_books !== null) parts.push(`${d.rooms_on_books} on books`)
  if (caps.group_rooms && d.group_rooms !== null) parts.push(`${d.group_rooms} group`)
  if (caps.event_covers && d.event_covers !== null) parts.push(`${d.event_covers} covers`)
  return parts.length > 0 ? parts.join(' · ') : null
}

function demandByDate(demand: DemandSurface | undefined): Map<string, DemandDay> {
  return new Map((demand?.days ?? []).map((d) => [d.stay_date, d]))
}

function ForecastPanel({
  propertyId,
  weekStart,
  scheduleId,
  demand,
}: {
  propertyId: string
  weekStart: string
  scheduleId: number
  demand?: DemandSurface
}) {
  const forecast = useQuery({
    queryKey: ['schedule-forecast', propertyId, weekStart],
    queryFn: () => getForecast(propertyId, weekStart),
  })
  // Rows can legitimately span pulls (a past date's voice comes from an
  // older batch), so the caption must never stamp every row with one
  // row's as-of — a stale figure would display under a fresh claim.
  const stamps = [...new Set((demand?.days ?? []).map((d) => d.pulled_at.slice(0, 10)))].sort()
  const asOf = stamps.length > 1 ? `${stamps[0]} – ${stamps[stamps.length - 1]}` : stamps[0]

  return (
    <Card role="region" aria-label="occupancy forecast" className="print:hidden">
      <PageHeader
        level={2}
        title="Occupancy forecast"
        subtitle="Expected occupied rooms per day. Hints inform, never dictate — your number is the forecast."
      />
      {demand && asOf && (
        <p className="mb-2 text-xs text-ink-muted">
          CRM demand as of {asOf} ({demand.provider}) — group blocks and
          events inform, never fill.
        </p>
      )}
      {forecast.isError && (
        <p className="text-sm text-danger-red">
          Failed to load: {errorMessage(forecast.error)}
        </p>
      )}
      {forecast.data && (
        // Keyed so switching property/week re-seeds the inputs from the newly
        // loaded SAVED values (never from hints).
        <ForecastForm
          key={`${propertyId}-${weekStart}`}
          propertyId={propertyId}
          scheduleId={scheduleId}
          days={forecast.data}
          demand={demand}
        />
      )}
    </Card>
  )
}

function ForecastForm({
  propertyId,
  scheduleId,
  days,
  demand,
}: {
  propertyId: string
  scheduleId: number
  days: ForecastDay[]
  demand?: DemandSurface
}) {
  const qc = useQueryClient()
  // Seeded from the GM's SAVED numbers only — a day with hints but no saved
  // forecast starts EMPTY. Hints never auto-fill.
  const [values, setValues] = useState<string[]>(
    days.map((d) => (d.occupied_rooms === null ? '' : String(d.occupied_rooms))),
  )

  const save = useMutation({
    mutationFn: () =>
      saveForecast({
        property: propertyId,
        // Only entered days travel: occupied_rooms is a non-null int server-
        // side, and an empty input means "not forecast", not zero.
        days: days.flatMap((d, i) => {
          const v = values[i] ?? ''
          return v === ''
            ? []
            : [{ business_date: d.business_date, occupied_rooms: Number(v) }]
        }),
      }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['schedule-forecast'] })
      // Per-room targets multiply the forecast — the summary must follow.
      void qc.invalidateQueries({ queryKey: ['schedule-targets', scheduleId] })
    },
  })

  return (
    <>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => { e.preventDefault(); save.mutate() }}
      >
        {days.map((d, i) => {
          const hint = hintText(d)
          const dd = demand ? demandByDate(demand).get(d.business_date) : undefined
          const figures = dd && demand ? demandFigures(dd, demand.capabilities) : null
          return (
            <label key={d.business_date} className="flex flex-col gap-1 text-sm">
              <span className="text-xs font-medium text-ink-muted">
                {dayName(d.business_date)} {d.business_date.slice(5)}
              </span>
              <input
                className={controlClass}
                type="number"
                min="0"
                value={values[i] ?? ''}
                onChange={(e) =>
                  setValues(values.map((v, j) => (j === i ? e.target.value : v)))
                }
                aria-label={`Occupied rooms ${d.business_date}`}
              />
              <span className="text-xs text-ink-muted">{hint ?? ' '}</span>
              {/* Demand hints render BESIDE the input and never fill it —
                  the same rule as the history hints above. Labels (block/
                  event names) are scheduler-surface working data: this
                  page only, never the kiosk. */}
              {figures && (
                <span className="max-w-40 text-xs text-ink-muted">
                  {figures}
                  {dd?.labels ? ` — ${dd.labels}` : ''}
                </span>
              )}
            </label>
          )
        })}
        <button
          type="submit"
          disabled={save.isPending}
          className="mb-5 rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >Save forecast</button>
      </form>
      {save.isError && (
        <p className="mt-2 text-sm text-danger-red" role="alert">
          Save failed: {errorMessage(save.error)}
        </p>
      )}
    </>
  )
}

// --- Targets vs scheduled (D2) -----------------------------------------------

// Each cell reads "scheduled/target" in HOURS (never money — THE MONEY RULE).
// A per-room day without a forecast has target null, rendered as a muted "—":
// null, NOT zero, because absence of a forecast is not zero demand and a
// silent 0 would paint every schedule over-target. The week total sums the
// non-null days and the days_without_forecast note tells the truth.

function targetCell(day: TargetDay) {
  if (day.target_hours === null) {
    return <span className="text-ink-muted">{day.scheduled_hours}/—</span>
  }
  const over = Number(day.scheduled_hours) > Number(day.target_hours)
  return (
    <span className={over ? 'font-medium text-warn-amber' : undefined}>
      {day.scheduled_hours}/{day.target_hours}
    </span>
  )
}

function TargetsPanel({ scheduleId }: { scheduleId: number }) {
  const targets = useQuery({
    queryKey: ['schedule-targets', scheduleId],
    queryFn: () => getTargets(scheduleId),
  })
  // Local const so TS narrowing survives the JSX map callbacks below.
  const data = targets.data

  return (
    <Card role="region" aria-label="targets" className="print:hidden">
      <PageHeader
        level={2}
        title="Target vs scheduled hours"
        subtitle="scheduled/target per department per day — hours only, from the labor standards and the occupancy forecast."
      />
      {targets.isError && (
        <p className="text-sm text-danger-red">
          Failed to load: {errorMessage(targets.error)}
        </p>
      )}
      {data && (
        data.departments.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No shifts and no standards yet — nothing to compare.
          </p>
        ) : (
          <div className="flex flex-col gap-2">
            <table className={tableClass} aria-label="Target vs scheduled hours">
              <thead>
                <tr className="border-b border-line">
                  <th className={headCellClass}>Department</th>
                  {Array.from({ length: 7 }, (_, i) =>
                    addDays(data.week_start, i),
                  ).map((d) => (
                    <th key={d} className={amountHeadClass}>
                      {dayName(d)} {d.slice(5)}
                    </th>
                  ))}
                  <th className={amountHeadClass}>Week</th>
                </tr>
              </thead>
              <tbody>
                {data.departments.map((dept) => (
                  <tr key={dept.department} className="border-b border-line last:border-0">
                    <td className={cellClass}>{dept.department}</td>
                    {dept.days.map((day) => (
                      <td key={day.business_date} className={amountCellClass}>
                        {targetCell(day)}
                      </td>
                    ))}
                    <td className={amountCellClass}>
                      {targetCell({
                        business_date: 'total',
                        target_hours: dept.target_total,
                        scheduled_hours: dept.scheduled_total,
                      })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.departments
              .filter((d) => d.days_without_forecast > 0)
              .map((d) => (
                <p key={d.department} className="text-xs text-ink-muted">
                  {d.department}: {d.days_without_forecast} day(s) without forecast —
                  per-room targets need one (absence is not zero demand).
                </p>
              ))}
          </div>
        )
      )}
    </Card>
  )
}

// --- Adherence (D3) ----------------------------------------------------------

// Scheduled vs punched for the week's ELAPSED days (strictly before the
// server's as_of — the future has nothing to adhere to; today is mid-shift).
// Punched hours are B2's MERGED timeline, so a manager-corrected day is clean.
// HOURS ONLY everywhere (THE MONEY RULE): nothing money-shaped exists in this
// response, and nothing money-shaped may ever render here.
//
// Freshness: punches arrive server-side independently of anything this page
// mutates, so query invalidation alone can never keep this panel current —
// the Refresh button refetches on demand. Shift mutations still invalidate
// ['schedule-adherence', scheduleId] because scheduled hours are half the
// comparison.

const EXCEPTION_LABELS: Record<string, string> = {
  no_show: 'no show',
  unscheduled_punch: 'unscheduled punch',
  deviation: 'deviation',
}

/** One exception line, anchored on the day+employee, hours only:
 * "Tue 2026-07-07 — no show: Hank H (scheduled 7.50)" /
 * "… — deviation: Barb B (7.50 scheduled, 4.00 punched)" /
 * "… — unscheduled punch: Cara C (3.00 punched)". */
function exceptionLine(x: AdherenceException): string {
  const label = EXCEPTION_LABELS[x.code] ?? x.code
  const detail =
    x.code === 'no_show'
      ? `(scheduled ${x.scheduled_hours})`
      : x.code === 'unscheduled_punch'
        ? `(${x.punched_hours} punched)`
        : `(${x.scheduled_hours} scheduled, ${x.punched_hours} punched)`
  return `${dayName(x.business_date)} ${x.business_date} — ${label}: ${x.full_name} ${detail}`
}

/** punched/scheduled per dept-day. Warn tone when punched < scheduled — the
 * dept-day rows carry no per-employee threshold verdict (that truth lives in
 * the exceptions list, computed server-side against the configured deviation
 * minutes), so the cell heuristic is the honest comparison the data supports:
 * any under-delivered coverage shades amber. */
function adherenceCell(punched: string, scheduled: string) {
  const under = Number(punched) < Number(scheduled)
  return (
    <span className={under ? 'font-medium text-warn-amber' : undefined}>
      {punched}/{scheduled}
    </span>
  )
}

function AdherencePanel({ scheduleId }: { scheduleId: number }) {
  const adherence = useQuery({
    queryKey: ['schedule-adherence', scheduleId],
    queryFn: () => getAdherence(scheduleId),
  })
  // Local const so TS narrowing survives the JSX map callbacks below.
  const data = adherence.data
  // Every department carries the same elapsed-day spine — the first one is
  // the column set. Empty spine = no elapsed days (fully-future week).
  const elapsedDays = data?.departments[0]?.days.map((d) => d.business_date) ?? []

  return (
    <Card role="region" aria-label="adherence" className="print:hidden">
      <PageHeader
        level={2}
        title="Adherence"
        subtitle="punched/scheduled per department for the elapsed days — hours only, from the merged punch timeline."
        actions={
          <button
            type="button"
            onClick={() => void adherence.refetch()}
            disabled={adherence.isFetching}
            className="rounded-control border border-line px-3 py-1.5 text-sm text-ink-muted hover:bg-surface-sunken disabled:opacity-50"
          >Refresh</button>
        }
      />
      {adherence.isError && (
        <p className="text-sm text-danger-red">
          Failed to load: {errorMessage(adherence.error)}
        </p>
      )}
      {data && (
        data.departments.length === 0 ? (
          <p className="text-sm text-ink-muted">No elapsed days yet.</p>
        ) : (
          <div className="flex flex-col gap-3">
            <table className={tableClass} aria-label="Adherence by department">
              <thead>
                <tr className="border-b border-line">
                  <th className={headCellClass}>Department</th>
                  {elapsedDays.map((d) => (
                    <th key={d} className={amountHeadClass}>
                      {dayName(d)} {d.slice(5)}
                    </th>
                  ))}
                  <th className={amountHeadClass}>Week</th>
                </tr>
              </thead>
              <tbody>
                {data.departments.map((dept) => (
                  <tr key={dept.department} className="border-b border-line last:border-0">
                    <td className={cellClass}>{dept.department}</td>
                    {dept.days.map((day) => (
                      <td key={day.business_date} className={amountCellClass}>
                        {adherenceCell(day.punched_hours, day.scheduled_hours)}
                      </td>
                    ))}
                    <td className={amountCellClass}>
                      {adherenceCell(dept.punched_total, dept.scheduled_total)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.exceptions.length === 0 ? (
              <p className="text-sm text-ink-muted">No exceptions.</p>
            ) : (
              // Grouped by code — the GM triages no-shows, deviations, and
              // unscheduled punches as different conversations.
              (['no_show', 'deviation', 'unscheduled_punch'] as const)
                .map((code) => ({
                  code,
                  items: data.exceptions.filter((x) => x.code === code),
                }))
                .filter((g) => g.items.length > 0)
                .map((g) => (
                  <ul
                    key={g.code}
                    aria-label={`${EXCEPTION_LABELS[g.code]} exceptions`}
                    className="flex flex-col gap-1"
                  >
                    {g.items.map((x) => (
                      <li
                        key={`${x.employee_id}-${x.business_date}`}
                        className="text-sm text-warn-amber"
                      >{exceptionLine(x)}</li>
                    ))}
                  </ul>
                ))
            )}
          </div>
        )
      )}
    </Card>
  )
}

// --- Week grid ---------------------------------------------------------------

function ScheduleBoard({
  weekStart,
  week,
  departments,
  roster,
  templates,
  deptNameOf,
  nameOf,
  onSelectShift,
  ensureWeek,
  propertyId,
  demand,
}: {
  weekStart: string
  week: ScheduleWeek | null
  departments: Department[]
  roster: Employee[]
  templates: ShiftTemplate[]
  deptNameOf: (id: number) => string
  nameOf: (id: number | null) => string
  onSelectShift: (id: number) => void
  ensureWeek: () => Promise<number>
  propertyId: string
  demand?: DemandSurface
}) {
  const qc = useQueryClient()
  // Days come from the LOADED week when it exists (the server owns the grid);
  // the picker's Monday is only the fallback for a not-yet-created week.
  const effectiveStart = week?.week_start ?? weekStart
  const demandDays = demandByDate(demand)
  const days = Array.from({ length: 7 }, (_, i) => addDays(effectiveStart, i))
  const shifts = week?.shifts ?? []
  const [hoverCell, setHoverCell] = useState<string | null>(null)
  const [openDepts, setOpenDepts] = useState<Set<number>>(new Set())
  const [addTarget, setAddTarget] = useState<{
    date: string
    employeeId: number | null
    departmentId: number | null
  } | null>(null)

  const deptIds = [...new Set([
    ...shifts.map((sh) => sh.department_id),
    ...templates.map((t) => t.department_id),
    ...departments.map((d) => d.department_id),
  ])].sort((a, b) => a - b)
  const DEPT_TONES = [
    'bg-accent-soft text-accent-ink',
    'bg-info-blue-soft text-info-blue',
    'bg-ok-green-soft text-ok-green',
    'bg-warn-amber-soft text-warn-amber',
  ]
  const toneOf = (deptId: number) =>
    DEPT_TONES[Math.max(0, deptIds.indexOf(deptId)) % DEPT_TONES.length] ??
    'bg-accent-soft text-accent-ink'

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['schedule-week'] })
    void qc.invalidateQueries({ queryKey: ['schedule-projection'] })
    void qc.invalidateQueries({ queryKey: ['schedule-targets'] })
  }

  const addFromTemplate = useMutation({
    mutationFn: async ({ t, day, employeeId }: { t: ShiftTemplate; day: string; employeeId: number | null }) => {
      const sid = await ensureWeek()
      return createShift(sid, {
        business_date: day,
        department_id: t.department_id,
        start_time: t.start_time,
        end_time: t.end_time,
        crosses_midnight: t.crosses_midnight,
        employee_id: employeeId,
        template_id: t.template_id,
      })
    },
    onSettled: invalidate,
  })

  const moveShift = useMutation({
    mutationFn: ({ sh, day, employeeId }: { sh: ScheduleShift; day: string; employeeId: number | null }) =>
      updateShift(sh.shift_id, {
        business_date: day,
        department_id: sh.department_id,
        start_time: sh.start_time,
        end_time: sh.end_time,
        crosses_midnight: sh.crosses_midnight,
        employee_id: employeeId,
        template_id: sh.template_id,
      }),
    onSettled: invalidate,
  })

  function onDropCell(e: React.DragEvent, day: string, employeeId: number | null) {
    e.preventDefault()
    setHoverCell(null)
    const raw = e.dataTransfer.getData('text/plain')
    if (raw === '') return
    try {
      const payload = JSON.parse(raw) as { kind: string; id: number }
      if (payload.kind === 'template') {
        const t = templates.find((x) => x.template_id === payload.id)
        if (t !== undefined) addFromTemplate.mutate({ t, day, employeeId })
      } else if (payload.kind === 'shift') {
        const sh = shifts.find((x) => x.shift_id === payload.id)
        if (sh !== undefined && (sh.business_date !== day || sh.employee_id !== employeeId)) {
          moveShift.mutate({ sh, day, employeeId })
        }
      }
    } catch {
      /* foreign drag payload — ignore */
    }
  }

  // Grouping: employees under their home department; a "No department" group
  // for unplaced staff. Shifts render under the EMPLOYEE row whatever their
  // department is (the chip color carries the shift's department).
  const groups: { deptId: number | null; name: string; members: Employee[] }[] = [
    ...departments.map((d) => ({
      deptId: d.department_id as number | null,
      name: d.name,
      members: roster.filter((e) => e.department_id === d.department_id),
    })),
  ]
  const unplaced = roster.filter(
    (e) => e.department_id === null || !departments.some((d) => d.department_id === e.department_id),
  )
  if (unplaced.length > 0) groups.push({ deptId: null, name: 'No department', members: unplaced })

  // Rolled up over the group's PEOPLE, not over the shift's department: the
  // accordion groups employees by where they are placed, so a front-desk agent
  // covering a laundry shift still counts under front desk — which is the row
  // the manager is reading when they ask "how many hours am I giving my team".
  const groupDayHours = (members: Employee[], day: string): number => {
    const ids = new Set(members.map((m) => m.employee_id))
    return shifts
      .filter((sh) => sh.business_date === day && sh.employee_id !== null && ids.has(sh.employee_id))
      .reduce((acc, sh) => acc + shiftHours(sh), 0)
  }

  const employeeWeekHours = (employeeId: number): number =>
    shifts.filter((sh) => sh.employee_id === employeeId).reduce((a, sh) => a + shiftHours(sh), 0)

  const toggleDept = (key: number) =>
    setOpenDepts((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  const isOpen = (key: number) => !openDepts.has(key) // default OPEN; toggling closes

  // A day already over is READ-ONLY: no drop target, no +, and its chips go
  // grey. Editing the past would restate hours the timecards already own.
  const today = todayIso()
  const isPast = (day: string) => day < today

  const cellCls = (key: string, day: string) =>
    [
      'relative h-[3.25rem] border-b border-l border-line p-1.5 align-top transition-colors',
      isPast(day) ? 'bg-surface-sunken' : '',
      hoverCell === key ? 'bg-accent-soft ring-1 ring-inset ring-accent/40' : '',
    ].join(' ')

  /**
   * A shift chip. Solid fill, centred, one line — legible across a wall-mounted
   * week at a glance.
   *
   * The fill is a STATUS: green while it is still ahead, grey once the day has
   * passed, red when nobody is on it. Colour is never the only cue — the chip
   * always states its hours, and an open one says OPEN.
   */
  const renderShiftChips = (cellShifts: ScheduleShift[], day: string, showWho: boolean) =>
    cellShifts.map((sh) => {
      const open = sh.employee_id === null
      const fill = isPast(day)
        ? 'bg-shift-past'
        : open
          ? 'bg-shift-open'
          : 'bg-shift-on'
      return (
        <button
          key={sh.shift_id}
          type="button"
          draggable={!isPast(day)}
          onDragStart={(e) => {
            e.dataTransfer.setData('text/plain', JSON.stringify({ kind: 'shift', id: sh.shift_id }))
            e.dataTransfer.effectAllowed = 'move'
          }}
          onClick={() => onSelectShift(sh.shift_id)}
          // The visible chip is deliberately terse — times only, so a wall of
          // them stays readable. The accessible name carries what the eye gets
          // from the row and column the chip sits in.
          aria-label={`${shiftLabel(sh, nameOf)} · ${deptNameOf(sh.department_id)} · ${day}`}
          title={`${deptNameOf(sh.department_id)} · ${shiftLabel(sh, nameOf)}${
            isPast(day) ? ' — this day has passed' : ' — click to edit, drag to move'
          }`}
          className={`flex w-full items-center justify-center gap-1 rounded-lg px-2 py-1.5 text-center text-[11px] font-semibold leading-tight text-shift-ink shadow-sm transition-transform ${fill} ${
            isPast(day) ? 'cursor-default' : 'cursor-grab hover:brightness-110 active:cursor-grabbing'
          }`}
        >
          <span className="tabular-nums">
            {sh.start_time}–{sh.end_time}
            {sh.crosses_midnight ? ' +1' : ''}
          </span>
          {open && <span className="opacity-90">· OPEN</span>}
          {showWho && !open && (
            <span className="truncate opacity-90">· {nameOf(sh.employee_id)}</span>
          )}
        </button>
      )
    })

  /** Always rendered, never hover-only: a control you cannot see is a control
   *  most people never find. It simply disappears on a day already over. */
  const plusButton = (date: string, employeeId: number | null, departmentId: number | null, who: string) => {
    if (isPast(date)) return null
    return (
      <button
        type="button"
        aria-label={`Add shift for ${who} on ${date}`}
        onClick={() => setAddTarget({ date, employeeId, departmentId })}
        className="mx-auto grid h-7 w-full max-w-[6rem] place-items-center rounded-lg border border-dashed border-line text-sm font-medium text-ink-faint transition-colors hover:border-accent hover:bg-accent-soft hover:text-accent print:hidden"
      >+</button>
    )
  }

  const headCls =
    'sticky top-0 z-20 border-b border-line bg-surface-raised py-3 text-center text-[11px] font-semibold uppercase tracking-wide text-ink-faint'
  const stickyNameCls = 'sticky left-0 z-10 bg-surface-raised'

  return (
    <Card
      role="region"
      aria-label="week grid"
      // The board is a FRAME, not a page section: it is capped to the viewport
      // and scrolls internally, so all seven days stay on screen and opening a
      // department never pushes the week off the bottom.
      className="flex max-h-[calc(100vh-19rem)] flex-col p-0 print:max-h-none"
    >
      {(addFromTemplate.isError || moveShift.isError) && (
        <p className="border-b border-line px-5 py-2 text-sm text-danger-red print:hidden" role="alert">
          Shift change failed: {errorMessage(addFromTemplate.error ?? moveShift.error)}
        </p>
      )}

      {templates.length > 0 && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line px-5 py-3 print:hidden">
          <span className="text-xs font-semibold uppercase tracking-wide text-ink-faint">Templates</span>
          {templates.map((t) => (
            <span
              key={t.template_id}
              draggable
              onDragStart={(e) => {
                e.dataTransfer.setData('text/plain', JSON.stringify({ kind: 'template', id: t.template_id }))
                e.dataTransfer.effectAllowed = 'copy'
              }}
              title={`Drag onto the grid: ${t.name} ${t.start_time}–${t.end_time}`}
              className={`inline-flex cursor-grab items-center gap-1.5 rounded-full border border-line px-3 py-1 text-xs font-medium active:cursor-grabbing ${toneOf(t.department_id)}`}
            >
              <span aria-hidden="true">⠿</span>
              {t.name} · {t.start_time}–{t.end_time}
            </span>
          ))}
          <span className="text-xs text-ink-faint">drag onto a cell, or use a cell's +</span>
        </div>
      )}

      {/* The whole seven-day frame in one screen. The scroll lives INSIDE this
          box, with the day header and the staff column pinned, so opening a
          department scrolls the roster past a header that stays put instead of
          pushing the week off the page. */}
      <div className="min-h-0 flex-1 overflow-auto print:overflow-visible">
        <table className="w-full border-separate border-spacing-0 text-sm" aria-label="Week grid">
          <thead>
            <tr>
              <th className={`${headCls} left-0 z-30 w-[12rem] min-w-[12rem] pl-4 text-left`}>Staff</th>
              {days.map((d) => (
                <th
                  key={d}
                  className={`${headCls} min-w-[6.5rem] border-l ${
                    isPast(d) ? 'text-ink-faint/70' : d === today ? 'text-accent' : ''
                  }`}
                >
                  {dayName(d)}{' '}
                  <span className="tabular-nums">{usDate(d).slice(0, 5)}</span>
                  {d === today && <span className="ml-1 font-bold">•</span>}
                  {/* The demand chip: figures only (a dimension the provider
                      lacks is absent, never 0). It informs the GM assembling
                      the day — it moves no target. */}
                  {(() => {
                    const dd = demandDays.get(d)
                    const chip = dd && demand ? demandFigures(dd, demand.capabilities) : null
                    return chip === null ? null : (
                      <span className="mt-0.5 block text-[10px] font-medium normal-case tracking-normal text-ink-muted">
                        {chip}
                      </span>
                    )
                  })()}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {/* Unassigned (OPEN) shifts — planned coverage nobody owns yet */}
            <tr>
              <td className={`${stickyNameCls} border-b border-line py-3 pl-4 pr-3 align-middle`}>
                <span className="flex items-center gap-2">
                  <span aria-hidden="true" className="inline-block size-2.5 rounded-full bg-shift-open" />
                  <span className="text-xs font-bold uppercase tracking-wide text-ink-muted">
                    Unassigned
                  </span>
                </span>
              </td>
              {days.map((d) => {
                const key = `open|${d}`
                const cellShifts = shifts.filter((sh) => sh.business_date === d && sh.employee_id === null)
                return (
                  <td
                    key={d}
                    onDragOver={(e) => { if (!isPast(d)) { e.preventDefault(); setHoverCell(key) } }}
                    onDragLeave={() => setHoverCell((c) => (c === key ? null : c))}
                    onDrop={(e) => { if (!isPast(d)) onDropCell(e, d, null) }}
                    className={cellCls(key, d)}
                  >
                    <div className="flex flex-col gap-1.5">
                      {renderShiftChips(cellShifts, d, false)}
                      {plusButton(d, null, null, 'open shift')}
                    </div>
                  </td>
                )
              })}
            </tr>

            {groups.map((g) => {
              const groupKey = g.deptId ?? -1
              const open = isOpen(groupKey)
              const weekHrs = days.reduce((a, d) => a + groupDayHours(g.members, d), 0)
              return [
                <tr
                  key={`dept-${groupKey}`}
                  className="cursor-pointer bg-surface-sunken/70 hover:bg-surface-sunken"
                  onClick={() => toggleDept(groupKey)}
                >
                  <td className={`${stickyNameCls} border-y border-line py-3.5 pl-4 pr-3`}>
                    <span className="flex items-center gap-2.5">
                      <span
                        aria-hidden="true"
                        className={`text-[10px] text-ink-faint transition-transform ${open ? 'rotate-90' : ''}`}
                      >▶</span>
                      {g.deptId !== null && (
                        <span aria-hidden="true" className={`inline-block size-2.5 rounded-full ${toneOf(g.deptId).split(' ')[0]}`} />
                      )}
                      <span className="truncate font-semibold text-ink">{g.name}</span>
                      <span className="rounded-full bg-surface px-2 py-0.5 text-[11px] font-medium tabular-nums text-ink-muted">
                        {g.members.length}
                      </span>
                      <span className="ml-auto text-xs tabular-nums text-ink-muted">
                        {weekHrs > 0 ? fmtHM(weekHrs) : ''}
                      </span>
                    </span>
                  </td>
                  {days.map((d) => {
                    const hrs = groupDayHours(g.members, d)
                    return (
                      <td
                        key={d}
                        className={`border-y border-l border-line py-3.5 text-center text-xs font-medium tabular-nums ${
                          hrs > 0 ? 'text-ink' : 'text-ink-faint'
                        } ${isPast(d) ? 'bg-surface-sunken' : ''}`}
                      >
                        {hrs > 0 ? fmtHM(hrs) : '—'}
                      </td>
                    )
                  })}
                </tr>,
                ...(open
                  ? g.members.map((emp) => {
                      const empHrs = employeeWeekHours(emp.employee_id)
                      const over = empHrs > 40
                      return (
                        <tr key={`emp-${emp.employee_id}`} className="hover:bg-surface-sunken/40">
                          <td className={`${stickyNameCls} border-b border-line py-3 pl-10 pr-3`}>
                            <span className="block truncate text-[13px] font-medium text-ink">
                              {emp.full_name}
                            </span>
                            <span
                              className={`mt-0.5 block text-[11px] tabular-nums ${
                                over ? 'font-semibold text-warn-amber' : 'text-ink-faint'
                              }`}
                            >
                              {fmtHM(empHrs)} / 40:00{over ? ' · over' : ''}
                            </span>
                          </td>
                          {days.map((d) => {
                            const key = `${emp.employee_id}|${d}`
                            const cellShifts = shifts.filter(
                              (sh) => sh.business_date === d && sh.employee_id === emp.employee_id,
                            )
                            return (
                              <td
                                key={d}
                                onDragOver={(e) => { if (!isPast(d)) { e.preventDefault(); setHoverCell(key) } }}
                                onDragLeave={() => setHoverCell((c) => (c === key ? null : c))}
                                onDrop={(e) => { if (!isPast(d)) onDropCell(e, d, emp.employee_id) }}
                                className={cellCls(key, d)}
                              >
                                <div className="flex flex-col gap-1.5">
                                  {renderShiftChips(cellShifts, d, false)}
                                  {plusButton(d, emp.employee_id, g.deptId, emp.full_name)}
                                </div>
                              </td>
                            )
                          })}
                        </tr>
                      )
                    })
                  : []),
              ]
            })}
          </tbody>
        </table>
      </div>

      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-t border-line px-5 py-3 text-xs text-ink-muted">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block size-2.5 rounded-[4px] bg-shift-on" />
          scheduled
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block size-2.5 rounded-[4px] bg-shift-open" />
          open — nobody assigned
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block size-2.5 rounded-[4px] bg-shift-past" />
          past — locked
        </span>
        {deptIds.length > 0 && <span className="mx-1 hidden h-4 w-px bg-line sm:block" />}
        {deptIds.map((id) => (
          <span key={id} className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className={`inline-block size-2.5 rounded-full ${toneOf(id).split(' ')[0]}`} />
            {deptNameOf(id)}
          </span>
        ))}
      </div>

      {addTarget !== null && (
        <AddShiftModal
          target={addTarget}
          weekStart={effectiveStart}
          departments={departments}
          templates={templates}
          roster={roster}
          ensureWeek={ensureWeek}
          onClose={() => setAddTarget(null)}
          onDone={() => { setAddTarget(null); invalidate() }}
          propertyId={propertyId}
        />
      )}
    </Card>
  )
}

// --- Projection panel --------------------------------------------------------

// The "live" behavior lives in the QUERY KEY: every shift mutation on this page
// invalidates ['schedule-projection', scheduleId], so the numbers move as the
// GM assigns. Per-employee cells are HOURS ONLY; money is department-aggregate
// with the B3 suppression on the PRICED population (est_cost null below two
// priced employees → em-dash + note, the Statement recipe) and a
// complementary total.

const WARNING_LABELS: Record<string, string> = {
  scheduled_overtime: 'scheduled overtime',
  clopening: 'clopening',
  seventh_day: '7th consecutive day',
}

/** One warning line, anchored on the day+employee it concerns; OT carries its
 * hours (never money): "Fri 2026-07-24 — scheduled overtime: Hank H, 1.50h". */
function warningLine(w: ScheduleProjectionWarning): string {
  const label = WARNING_LABELS[w.code] ?? w.code
  const hours = w.code === 'scheduled_overtime' ? `, ${w.hours}h` : ''
  return `${dayName(w.business_date)} ${w.business_date} — ${label}: ${w.full_name}${hours}`
}

function ProjectionPanel({ scheduleId }: { scheduleId: number }) {
  const projection = useQuery({
    queryKey: ['schedule-projection', scheduleId],
    queryFn: () => getProjection(scheduleId),
  })

  return (
    <Card role="region" aria-label="projection" className="print:hidden">
      <PageHeader level={2} title="Projection" />
      {projection.isError && (
        <p className="text-sm text-danger-red">
          Failed to project: {errorMessage(projection.error)}
        </p>
      )}
      {projection.data && (
        <div className="flex flex-col gap-4">
          {/* D3: the current week's projection merges punched reality for
              elapsed days — the label says so ("merged actuals label itself"),
              and its absence means a pure plan-derived projection. */}
          {projection.data.merged_through !== null && (
            <p className="text-xs text-ink-muted">
              Includes actual hours through {projection.data.merged_through}
            </p>
          )}
          {projection.data.employees.length === 0 ? (
            <p className="text-sm text-ink-muted">No assigned shifts to project yet.</p>
          ) : (
            <table className={tableClass} aria-label="Projected hours">
              <thead>
                <tr className="border-b border-line">
                  <th className={headCellClass}>Employee</th>
                  <th className={amountHeadClass}>Total</th>
                  <th className={amountHeadClass}>Regular</th>
                  <th className={amountHeadClass}>OT</th>
                </tr>
              </thead>
              <tbody>
                {projection.data.employees.map((e) => (
                  <tr key={e.employee_id} className="border-b border-line last:border-0">
                    <td className={cellClass}>{e.full_name}</td>
                    <td className={amountCellClass}>{e.total_hours}</td>
                    <td className={amountCellClass}>{e.regular_hours}</td>
                    <td className={amountCellClass}>
                      {Number(e.ot_hours) > 0
                        ? <Badge tone="warn">{e.ot_hours}</Badge>
                        : e.ot_hours}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {projection.data.warnings.length > 0 && (
            <ul aria-label="Schedule warnings" className="flex flex-col gap-1">
              {projection.data.warnings.map((w, i) => (
                <li key={i} className="text-sm text-warn-amber">{warningLine(w)}</li>
              ))}
            </ul>
          )}

          {projection.data.departments.length > 0 && (
            <table className={tableClass} aria-label="Projected department cost">
              <thead>
                <tr className="border-b border-line">
                  <th className={headCellClass}>Department</th>
                  <th className={amountHeadClass}>Hours</th>
                  <th className={amountHeadClass}>Est cost</th>
                </tr>
              </thead>
              <tbody>
                {projection.data.departments.map((d) => (
                  <tr key={d.department} className="border-b border-line last:border-0">
                    <td className={cellClass}>{d.department}</td>
                    <td className={amountCellClass}>{d.hours}</td>
                    <td className={amountCellClass}>
                      {d.est_cost === null ? (
                        // The Statement's B3/C3 suppression recipe: hours still
                        // carry, the money cell hides.
                        <span className="text-ink-muted">
                          —{' '}
                          <span className="text-xs font-normal">
                            hidden (fewer than two priced employees)
                          </span>
                        </span>
                      ) : (
                        fmtMoney(d.est_cost)
                      )}
                    </td>
                  </tr>
                ))}
                <tr className="font-semibold">
                  <td className={cellClass}>Total est cost</td>
                  <td className={amountCellClass}></td>
                  <td className={amountCellClass}>{fmtMoney(projection.data.total_est_cost)}</td>
                </tr>
              </tbody>
            </table>
          )}

          <div className="flex flex-col gap-1">
            {projection.data.suppressed_departments > 0 && (
              <p className="text-xs text-ink-muted">
                Cost hidden for {projection.data.suppressed_departments} department
                {projection.data.suppressed_departments === 1 ? '' : 's'} with fewer than
                two priced employees (excluded from the total).
              </p>
            )}
            {Number(projection.data.unpriced_hours) > 0 && (
              <p className="text-xs text-ink-muted">
                {projection.data.unpriced_hours}h scheduled carry no est cost (exempt or
                unrated employees).
              </p>
            )}
            <p className="text-xs text-ink-muted">
              Shifts over 6h assume a 30-minute unpaid meal.
            </p>
          </div>
        </div>
      )}
    </Card>
  )
}

// --- Availability note (D2) --------------------------------------------------

// GM-maintained scheduling aid ("can't work Tuesdays") — operational, never
// money or medical detail. Rendered muted beside employee selection in the
// add-shift and reassign flows; the inline edit PUTs the note (scheduler-gated
// server-side) and refreshes the roster it travels on.

function AvailabilityNote({ employee }: { employee: Employee }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(employee.availability_note ?? '')

  const save = useMutation({
    mutationFn: () =>
      putAvailabilityNote(
        employee.employee_id,
        draft.trim() === '' ? null : draft.trim(),
      ),
    onSuccess: () => setEditing(false),
    // The note travels on the roster (getEmployees) — refresh it.
    onSettled: () => void qc.invalidateQueries({ queryKey: ['employees'] }),
  })

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      {editing ? (
        <>
          <span className="text-xs text-ink-muted">
            scheduling note (visible to managers)
          </span>
          <input
            className={controlClass}
            value={draft}
            maxLength={300}
            onChange={(e) => setDraft(e.target.value)}
            aria-label={`Availability note for ${employee.full_name}`}
          />
          <button
            type="button"
            onClick={() => save.mutate()}
            disabled={save.isPending}
            className="rounded-control bg-accent px-2 py-1 text-xs font-medium text-accent-contrast disabled:opacity-50"
          >Save note</button>
        </>
      ) : (
        <>
          <span className="text-xs text-ink-muted">
            {employee.availability_note !== null && employee.availability_note !== ''
              ? `note: ${employee.availability_note}`
              : 'no scheduling note'}
          </span>
          <button
            type="button"
            onClick={() => setEditing(true)}
            aria-label={`Edit availability note for ${employee.full_name}`}
            className="rounded-control border border-line px-2 py-1 text-xs text-ink-muted hover:bg-surface-sunken"
          >Edit note</button>
        </>
      )}
      {save.isError && (
        <span className="text-xs text-danger-red" role="alert">
          Save note failed: {errorMessage(save.error)}
        </span>
      )}
    </div>
  )
}

// --- Add shift ---------------------------------------------------------------

function AddShiftModal({
  target,
  weekStart,
  departments,
  templates,
  roster,
  ensureWeek,
  onClose,
  onDone,
  propertyId,
}: {
  target: { date: string; employeeId: number | null; departmentId: number | null }
  weekStart: string
  departments: Department[]
  templates: ShiftTemplate[]
  roster: Employee[]
  ensureWeek: () => Promise<number>
  onClose: () => void
  onDone: () => void
  propertyId: string
}) {
  const [templateId, setTemplateId] = useState('')
  const [departmentId, setDepartmentId] = useState(
    target.departmentId !== null ? String(target.departmentId) : '',
  )
  const [employeeId, setEmployeeId] = useState(
    target.employeeId !== null ? String(target.employeeId) : '',
  )
  const [date, setDate] = useState(target.date)
  const [startTime, setStartTime] = useState('')
  const [endTime, setEndTime] = useState('')
  const [crossesMidnight, setCrossesMidnight] = useState(false)

  // Picking a template pre-fills department + times; everything stays editable
  // (templates are provenance, not a straitjacket).
  function applyTemplate(id: string) {
    setTemplateId(id)
    const t = templates.find((x) => String(x.template_id) === id)
    if (t !== undefined) {
      setDepartmentId(String(t.department_id))
      setStartTime(t.start_time)
      setEndTime(t.end_time)
      setCrossesMidnight(t.crosses_midnight)
    }
  }

  const add = useMutation({
    mutationFn: async () => {
      const sid = await ensureWeek()
      return createShift(sid, {
        business_date: date,
        department_id: Number(departmentId),
        start_time: startTime,
        end_time: endTime,
        crosses_midnight: crossesMidnight,
        employee_id: employeeId === '' ? null : Number(employeeId),
        template_id: templateId === '' ? null : Number(templateId),
      })
    },
    onSuccess: onDone,
  })

  const days = Array.from({ length: 7 }, (_, i) => addDays(weekStart, i))

  return (
    <Modal
      title="Add shift"
      subtitle={`${propertyId} · week of ${weekStart}`}
      onClose={onClose}
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => { e.preventDefault(); add.mutate() }}
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Template</span>
          <select className={controlClass} value={templateId}
            onChange={(e) => applyTemplate(e.target.value)} aria-label="Shift template">
            <option value="">(pick a template)</option>
            {templates.map((t) => (
              <option key={t.template_id} value={t.template_id}>
                {t.name} · {t.start_time}–{t.end_time}
              </option>
            ))}
          </select>
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Department</span>
            <select className={controlClass} value={departmentId} required
              onChange={(e) => setDepartmentId(e.target.value)} aria-label="Shift department">
              <option value="">Select…</option>
              {departments.map((d) => (
                <option key={d.department_id} value={d.department_id}>{d.name}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Employee</span>
            <select className={controlClass} value={employeeId}
              onChange={(e) => setEmployeeId(e.target.value)} aria-label="Shift employee">
              <option value="">(open shift)</option>
              {roster.map((emp) => (
                <option key={emp.employee_id} value={emp.employee_id}>{emp.full_name}</option>
              ))}
            </select>
            {/* D2 scheduling aid — operator-visible, never money/medical. */}
            {employeeId !== '' && (
              <span className="text-xs text-ink-muted">
                {roster.find((e) => String(e.employee_id) === employeeId)?.availability_note ??
                  null
                  ? `note: ${roster.find((e) => String(e.employee_id) === employeeId)!.availability_note}`
                  : 'no scheduling note'}
              </span>
            )}
          </label>
        </div>
        <div className="grid grid-cols-3 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Day</span>
            <select className={controlClass} value={date}
              onChange={(e) => setDate(e.target.value)} aria-label="Shift day">
              {days.map((d) => (
                <option key={d} value={d}>{dayName(d)} {d.slice(5)}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Start</span>
            <input className={controlClass} type="time" value={startTime} required
              onChange={(e) => setStartTime(e.target.value)} aria-label="Shift start time" />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">End</span>
            <input className={controlClass} type="time" value={endTime} required
              onChange={(e) => setEndTime(e.target.value)} aria-label="Shift end time" />
          </label>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={crossesMidnight}
            onChange={(e) => setCrossesMidnight(e.target.checked)}
            aria-label="Shift crosses midnight" />
          <span className="text-xs font-medium text-ink-muted">Crosses midnight</span>
        </label>
        <div className="mt-1 flex items-center justify-end gap-2">
          <button type="button" onClick={onClose}
            className="rounded-control border border-line px-3 py-1.5 text-sm text-ink-muted hover:bg-surface-sunken">
            Cancel
          </button>
          <button
            type="submit"
            disabled={add.isPending || departmentId === '' || !startTime || !endTime}
            className="rounded-control bg-accent px-4 py-1.5 text-sm font-semibold text-accent-contrast disabled:opacity-50"
          >Add shift</button>
        </div>
      </form>
      {add.isError && (
        <p className="mt-2 text-sm text-danger-red" role="alert">
          Add failed: {errorMessage(add.error)}
        </p>
      )}
    </Modal>
  )
}

function ShiftEditor({
  shift,
  roster,
  nameOf,
  onClose,
}: {
  shift: ScheduleShift
  roster: Employee[]
  nameOf: (id: number | null) => string
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [employeeId, setEmployeeId] = useState(
    shift.employee_id === null ? '' : String(shift.employee_id),
  )

  function invalidate() {
    void qc.invalidateQueries({ queryKey: ['schedule-week'] })
    void qc.invalidateQueries({ queryKey: ['schedule-projection', shift.schedule_id] })
    void qc.invalidateQueries({ queryKey: ['schedule-targets', shift.schedule_id] })
    // D3: the scheduled side of adherence moves with the shifts.
    void qc.invalidateQueries({ queryKey: ['schedule-adherence', shift.schedule_id] })
  }

  const selectedEmployee =
    roster.find((e) => String(e.employee_id) === employeeId) ?? null

  const reassign = useMutation({
    // The PUT re-sends the shift's full shape (the API revalidates
    // everything); only the assignment changes here.
    mutationFn: () =>
      updateShift(shift.shift_id, {
        business_date: shift.business_date,
        department_id: shift.department_id,
        start_time: shift.start_time,
        end_time: shift.end_time,
        crosses_midnight: shift.crosses_midnight,
        employee_id: employeeId === '' ? null : Number(employeeId),
        template_id: shift.template_id,
      }),
    onSettled: invalidate,
  })

  const remove = useMutation({
    mutationFn: () => deleteShift(shift.shift_id),
    onSuccess: onClose,
    onSettled: invalidate,
  })

  return (
    <Card role="region" aria-label="shift detail" className="print:hidden">
      <PageHeader
        level={2}
        title={`Shift — ${shift.business_date} ${shift.start_time}–${shift.end_time}`}
        subtitle={`Dept ${shift.department_id} · currently ${nameOf(shift.employee_id)}`}
        actions={
          <button
            type="button"
            onClick={onClose}
            aria-label="Close shift detail"
            className="rounded-control border border-line px-2 py-1 text-xs text-ink-muted hover:bg-surface-sunken"
          >Close</button>
        }
      />
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Assign to</span>
          <select className={controlClass} value={employeeId}
            onChange={(e) => setEmployeeId(e.target.value)} aria-label="Reassign employee">
            <option value="">(open shift)</option>
            {roster.map((e) => (
              <option key={e.employee_id} value={e.employee_id}>{e.full_name}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => reassign.mutate()}
          disabled={reassign.isPending}
          className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >Save assignment</button>
        <button
          type="button"
          onClick={() => remove.mutate()}
          disabled={remove.isPending}
          aria-label="Delete shift"
          className="rounded-control border border-line px-2 py-1.5 text-sm text-danger-red hover:bg-danger-red-soft"
        >Delete shift</button>
      </div>
      {selectedEmployee && (
        <AvailabilityNote
          key={selectedEmployee.employee_id}
          employee={selectedEmployee}
        />
      )}
      {reassign.isError && (
        <p className="mt-2 text-sm text-danger-red" role="alert">
          Reassign failed: {errorMessage(reassign.error)}
        </p>
      )}
      {remove.isError && (
        <p className="mt-2 text-sm text-danger-red">
          Delete failed: {errorMessage(remove.error)}
        </p>
      )}
    </Card>
  )
}
