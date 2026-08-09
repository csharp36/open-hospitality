// Timecard review: filter, page, open one card, approve or reopen it.
//
// The card DETAIL is a modal, not a section appended under the list. With one
// row per employee per period the list is hundreds long, so a detail rendered
// below meant a click scrolled the thing you clicked off screen and read as
// "nothing happened".
//
// Every photo read is audited server-side as "this manager saw this face", so
// photos load only on an explicit click — never eagerly with the rows.

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Badge, Card, PageHeader, controlClass, tableClass } from '../components/ui'
import Modal from '../components/Modal'
import { approveTimecard, getPunchPhoto, getTimecard, getTimecards, reopenTimecard } from '../api/client'
import type { TimecardDayPunch, TimecardSummary } from '../api/types'
import { errorMessage } from '../lib/errors'
import { barRampCss } from '../lib/chartBars'

const PAGE_SIZE = 20

/** "8h" when the minutes are whole hours, "8h 30m" when they are not — a
 *  column of "0m" suffixes is noise the reader has to skip past. */
function hm(minutes: number): string {
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m === 0 ? `${h}h` : `${h}h ${m}m`
}

/** 2026-07-20 -> "July 20, 2026". Built from the PARTS, never
 *  `new Date('2026-07-20')`, which reads a bare ISO date as UTC and prints the
 *  19th anywhere west of Greenwich. */
function longDate(isoDate: string): string {
  const [y, m, d] = isoDate.split('-').map(Number)
  if (y === undefined || m === undefined || d === undefined) return isoDate
  return new Date(y, m - 1, d).toLocaleDateString('en-US', {
    day: 'numeric', month: 'long', year: 'numeric',
  })
}

/** "July 20 – August 2, 2026", dropping the repeated year on the left. */
function dateRange(from: string, to: string): string {
  const [fy] = from.split('-')
  const [ty] = to.split('-')
  const left = fy === ty ? longDate(from).replace(`, ${fy}`, '') : longDate(from)
  return `${left} – ${longDate(to)}`
}

/** Wire values are snake_case lower ('no_meal_break', 'approved'); this is the
 *  reading of them only — nothing here is ever sent back. */
function titleCase(value: string): string {
  return value.replace(/_/g, ' ').replace(/\b[a-z]/g, (c) => c.toUpperCase())
}

/** Tone per workflow state rather than "anything not approved is grey": a
 *  rejected card must not look like one nobody has opened yet. */
function statusTone(status: string): 'ok' | 'warn' | 'danger' | 'neutral' {
  if (status === 'approved') return 'ok'
  if (status === 'rejected' || status === 'denied') return 'danger'
  if (status === 'open') return 'warn'
  return 'neutral'
}

/**
 * F6: the match verdict per punch. Green = the face at the kiosk verified
 * against the on-file template; red = it did NOT (a human must look and
 * explicitly acknowledge before approval); grey = nobody enrolled yet.
 * A pre-matching punch (null) shows nothing — there is no verdict to show.
 */
function MatchBadge({ punch }: { punch: TimecardDayPunch }) {
  if (punch.match_state === 'verified') {
    return (
      <Badge tone="ok">
        {punch.match_score != null ? `verified ${punch.match_score.toFixed(2)}` : 'verified'}
      </Badge>
    )
  }
  if (punch.match_state === 'unverified') return <Badge tone="danger">unverified</Badge>
  if (punch.match_state === 'no_template') return <Badge tone="neutral">no template</Badge>
  return null
}

function punchLabel(punch: TimecardDayPunch): string {
  return titleCase(punch.punch_type.replace('_', ' '))
}

function punchTime(punch: TimecardDayPunch): string {
  return new Date(punch.punched_at).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit',
  })
}

/** Minutes as "7h 30m" / "45m" — for the length of a segment, where a bare
 *  "0h 45m" reads worse than the shape it is labelling. */
function span(minutes: number): string {
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  if (h === 0) return `${m}m`
  return m === 0 ? `${h}h` : `${h}h ${m}m`
}

/**
 * What the clock was doing between two punches.
 *
 * Driven by the punch that OPENS the interval, so it generalises past lunch:
 * any `*_start` opens a break, any `*_end` (and clock_in) resumes work, and
 * clock_out ends the day. A punch type nobody has invented yet lands on
 * 'work', which is the honest default — it is on the clock until something
 * says otherwise.
 */
function segmentKind(openedBy: string): 'work' | 'break' | 'off' {
  if (openedBy === 'clock_out') return 'off'
  if (openedBy.endsWith('_start') && openedBy !== 'clock_in') return 'break'
  return 'work'
}

/**
 * One day as a single horizontal timeline: clock-in to clock-out, with the
 * break drawn as its own gap rather than as two more rows of text.
 *
 * This replaced a stacked list of punches, which cost four lines per day and
 * still never showed the thing a reviewer is actually checking — whether the
 * shape of the day is right (turned up late, no break, went home early).
 *
 * Clicking a marker opens that punch and, when there is one, loads its photo.
 * That single click IS the explicit act the audit trail records: nothing is
 * fetched by rendering the day, only by asking for a punch by name.
 */
function DayTimeline({
  punches, acknowledgeable, acked, onAcknowledge,
}: {
  punches: TimecardDayPunch[]
  // True only on an OPEN card: renders the acknowledgment checkboxes the
  // approve call collects.
  acknowledgeable: boolean
  acked: ReadonlySet<number>
  onAcknowledge: (id: number, checked: boolean) => void
}) {
  const [open, setOpen] = useState<number | null>(null)

  const ordered = [...punches].sort(
    (a, b) => Date.parse(a.punched_at) - Date.parse(b.punched_at),
  )
  if (ordered.length === 0) {
    return <span className="text-xs text-ink-muted">No punches recorded</span>
  }

  const first = Date.parse(ordered[0]!.punched_at)
  const last = Date.parse(ordered.at(-1)!.punched_at)
  // Every punch at the same instant would divide by zero; one marker at the
  // start of an empty track is the honest rendering of that.
  const total = Math.max(1, last - first)
  const at = (p: TimecardDayPunch) => ((Date.parse(p.punched_at) - first) / total) * 100

  const segments = ordered.slice(0, -1).map((p, i) => {
    const next = ordered[i + 1]!
    return {
      key: p.punch_id,
      kind: segmentKind(p.punch_type),
      left: at(p),
      width: at(next) - at(p),
      minutes: (Date.parse(next.punched_at) - Date.parse(p.punched_at)) / 60000,
    }
  })

  const unverified = ordered.filter((p) => p.match_state === 'unverified')
  const selected = ordered.find((p) => p.punch_id === open)

  const breakMinutes = segments
    .filter((x) => x.kind === 'break')
    .reduce((total, x) => total + x.minutes, 0)

  return (
    <div className="flex flex-col gap-3">
      {/* px-2 so the end markers hang past the track instead of being clipped */}
      <div className="px-2">
        <div className="relative h-3.5">
          <div className="absolute inset-x-0 top-0.5 h-2.5 rounded-full bg-surface-sunken" />
          {segments.map((x) =>
            x.kind === 'off' ? null : (
              <div
                key={x.key}
                className={`absolute top-0.5 h-2.5 rounded-full ${
                  x.kind === 'break' ? 'bg-warn-amber-soft ring-1 ring-inset ring-warn-amber/40' : ''
                }`}
                style={{
                  left: `${x.left}%`,
                  width: `${x.width}%`,
                  ...(x.kind === 'work' ? { background: barRampCss('var(--color-accent)') } : {}),
                }}
                title={`${x.kind === 'work' ? 'Worked' : 'Break'} ${span(x.minutes)}`}
              />
            ),
          )}
          {ordered.map((p) => {
            const isOpen = p.punch_id === open
            // Colour repeats the match verdict, but the verdict is never colour
            // alone — the open panel names it, and unverified punches also get
            // their own checkbox row below.
            const tone =
              p.match_state === 'unverified'
                ? 'bg-danger-red'
                : p.match_state === 'verified'
                  ? 'bg-ok-green'
                  : p.match_state === 'no_template'
                    ? 'bg-ink-faint'
                    : 'bg-accent'
            return (
              <button
                key={p.punch_id}
                type="button"
                onClick={() => setOpen(isOpen ? null : p.punch_id)}
                aria-expanded={isOpen}
                // The accessible name is the whole punch: a marker with no text
                // is unusable without it.
                aria-label={`${punchLabel(p)} ${punchTime(p)}`}
                title={`${punchLabel(p)} ${punchTime(p)}`}
                className="absolute top-0 -translate-x-1/2 p-0"
                style={{ left: `${at(p)}%` }}
              >
                <span
                  className={`block size-3.5 rounded-full ring-2 ring-surface-raised transition-transform hover:scale-125 ${tone} ${
                    isOpen ? 'outline outline-2 outline-offset-2 outline-accent' : ''
                  }`}
                />
              </button>
            )
          })}
        </div>
      </div>

      {/* The times live here rather than under each marker: a 30-minute lunch
          is ~3% of an 8-hour day, so per-marker labels overlapped into
          "11:08AM11:38 AM". One line says start, end, and break total; a
          marker gives the exact punch on hover or click. */}
      <p className="flex flex-wrap items-center gap-x-2 text-xs tabular-nums text-ink-muted">
        <span className="font-medium text-ink">{punchTime(ordered[0]!)}</span>
        <span aria-hidden="true">→</span>
        <span className="font-medium text-ink">{punchTime(ordered.at(-1)!)}</span>
        {breakMinutes > 0 && (
          <>
            <span aria-hidden="true" className="text-ink-faint">·</span>
            <span>{span(breakMinutes)} break</span>
          </>
        )}
        {ordered.length === 1 && <span className="text-warn-amber">single punch</span>}
      </p>

      {selected && (
        <PunchDetail punch={selected} onClose={() => setOpen(null)} />
      )}

      {acknowledgeable && unverified.length > 0 && (
        // The gate is NOT hidden behind a marker click: a punch that blocks
        // approval states itself, and ticking it is one click from here.
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg bg-danger-red-soft px-3 py-2">
          {unverified.map((p) => (
            <label key={p.punch_id} className="flex items-center gap-1.5 text-xs text-ink">
              <input
                type="checkbox"
                checked={acked.has(p.punch_id)}
                onChange={(e) => onAcknowledge(p.punch_id, e.target.checked)}
                aria-label={`acknowledge unverified ${p.punch_type.replace('_', ' ')}`}
              />
              Reviewed — {punchLabel(p)} {punchTime(p)}
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * The opened punch: verdict, and its photo if one survives.
 *
 * Every successful photo read is audited server-side as "this manager saw this
 * face", so the fetch is tied to opening ONE punch by name and never to
 * rendering the day.
 */
function PunchDetail({ punch, onClose }: { punch: TimecardDayPunch; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!punch.has_photo) return
    let revoked = false
    let objectUrl: string | null = null
    setLoading(true)
    getPunchPhoto(punch.punch_id)
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob)
        if (!revoked) setUrl(objectUrl)
      })
      .catch((e: unknown) => setError(errorMessage(e)))
      .finally(() => setLoading(false))
    return () => {
      revoked = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [punch.punch_id, punch.has_photo])

  const label = punch.punch_type.replace('_', ' ')
  return (
    <div className="flex items-start gap-3 rounded-card border border-line bg-surface-sunken/50 p-3">
      <div className="grid size-20 shrink-0 place-items-center overflow-hidden rounded-card bg-surface">
        {url && <img src={url} alt={`${label} punch photo`} className="size-full object-cover" />}
        {!url && (
          <span className="px-1 text-center text-[10px] leading-tight text-ink-muted">
            {loading ? 'Loading …' : punch.has_photo ? 'No image' : 'No photo'}
          </span>
        )}
      </div>
      <div className="flex min-w-0 flex-col gap-1.5">
        <span className="text-sm font-semibold text-ink">
          {punchLabel(punch)} {punchTime(punch)}
        </span>
        <MatchBadge punch={punch} />
        {!punch.has_photo && (
          <span className="text-xs text-ink-muted">no photo (purged or not stored)</span>
        )}
        {error && <span className="text-xs text-danger-red">{error}</span>}
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label={`Close ${label} punch`}
        className="ml-auto rounded-lg px-2 py-1 text-xs text-ink-muted hover:bg-surface-sunken hover:text-ink"
      >
        Close
      </button>
    </div>
  )
}

const headCls = 'px-5 py-3.5 text-left text-xs font-semibold tracking-wide text-ink-muted'
const rowCellCls = 'px-5 py-4 align-middle'
const pagerButtonCls =
  'rounded-control border border-line px-3 py-1.5 text-sm text-ink-muted ' +
  'hover:bg-surface-sunken disabled:opacity-40'

export default function TimecardsPage() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<number | null>(null)
  // F6: red-punch ids the approver has ticked. Reset on card switch — an
  // acknowledgment is per card, never carried to the next one.
  const [acked, setAcked] = useState<ReadonlySet<number>>(new Set())

  const [name, setName] = useState('')
  const [status, setStatus] = useState('')
  const [period, setPeriod] = useState('')
  const [page, setPage] = useState(1)

  const timecards = useQuery({ queryKey: ['timecards'], queryFn: getTimecards })
  const detail = useQuery({
    queryKey: ['timecard', selected],
    queryFn: () => getTimecard(selected as number),
    enabled: selected !== null,
  })

  const unverifiedIds =
    detail.data?.days.flatMap((d) =>
      (d.punches ?? [])
        .filter((p) => p.match_state === 'unverified')
        .map((p) => p.punch_id),
    ) ?? []
  const unacknowledged = unverifiedIds.filter((id) => !acked.has(id))

  // One row per employee PER PERIOD is the data model; make that legible by
  // grouping periods together, newest first, alphabetical within.
  const sorted: TimecardSummary[] | undefined = useMemo(
    () =>
      timecards.data
        ? [...timecards.data].sort(
            (a, b) =>
              b.period_start.localeCompare(a.period_start) ||
              a.employee_name.localeCompare(b.employee_name),
          )
        : undefined,
    [timecards.data],
  )

  // Filter options come from the DATA, not a hardcoded list: a status the
  // backend adds later appears here instead of being silently unfilterable.
  const periods = useMemo(
    () => [...new Set((sorted ?? []).map((c) => c.period_start))],
    [sorted],
  )
  const statuses = useMemo(
    () => [...new Set((sorted ?? []).map((c) => c.status))].sort(),
    [sorted],
  )

  const filtered = useMemo(() => {
    const needle = name.trim().toLowerCase()
    return (sorted ?? []).filter(
      (c) =>
        (needle === '' || c.employee_name.toLowerCase().includes(needle)) &&
        (status === '' || c.status === status) &&
        (period === '' || c.period_start === period),
    )
  }, [sorted, name, status, period])

  // Clamped rather than stored: a filter that leaves fewer pages than the one
  // being viewed would otherwise show an empty table with rows on page 1.
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const current = Math.min(page, pageCount)
  const visible = filtered.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE)
  const filtering = name !== '' || status !== '' || period !== ''

  const approve = useMutation({
    mutationFn: (id: number) => approveTimecard(id, [...acked]),
    onSettled: (_data, _error, id) => {
      void qc.invalidateQueries({ queryKey: ['timecards'] })
      void qc.invalidateQueries({ queryKey: ['timecard', id] })
    },
  })

  // H3: reopen for a fresh review. Acknowledgments are cleared — the server
  // re-runs the F6 gate over every punch (relinked ones included) and nothing
  // carries over, so the UI must not pre-tick what nobody has re-reviewed.
  const reopen = useMutation({
    mutationFn: (id: number) => reopenTimecard(id),
    onSuccess: () => setAcked(new Set()),
    onSettled: (_data, _error, id) => {
      void qc.invalidateQueries({ queryKey: ['timecards'] })
      void qc.invalidateQueries({ queryKey: ['timecard', id] })
    },
  })

  function selectCard(id: number) {
    setSelected(id)
    setAcked(new Set())
  }

  return (
    <div className="flex flex-col gap-5">
      <Card className="flex flex-col gap-5 p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="[&>header]:mb-0">
            <PageHeader
              title="Timecards"
              subtitle="Review hours and warnings, then approve. Approving locks the card."
            />
          </div>
          {sorted !== undefined && (
            <p className="text-sm text-ink-muted">
              <span className="font-semibold text-ink">{filtered.length}</span>
              {filtering ? ` of ${sorted.length}` : ''} card{filtered.length === 1 ? '' : 's'}
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <label className="flex min-w-[14rem] flex-1 flex-col gap-1.5 text-sm">
            <span className="text-[13px] font-semibold text-ink">Employee</span>
            <input
              className={`${controlClass} h-10 w-full px-3`}
              value={name}
              onChange={(e) => { setName(e.target.value); setPage(1) }}
              aria-label="Filter By Employee"
              placeholder="Search by name"
            />
          </label>
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-[13px] font-semibold text-ink">Pay Period</span>
            <select
              className={`${controlClass} h-10 px-3`}
              value={period}
              onChange={(e) => { setPeriod(e.target.value); setPage(1) }}
              aria-label="Filter By Pay Period"
            >
              <option value="">All Periods</option>
              {periods.map((p) => (
                <option key={p} value={p}>{longDate(p)}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-[13px] font-semibold text-ink">Status</span>
            <select
              className={`${controlClass} h-10 px-3`}
              value={status}
              onChange={(e) => { setStatus(e.target.value); setPage(1) }}
              aria-label="Filter By Status"
            >
              <option value="">All Statuses</option>
              {statuses.map((s) => (
                <option key={s} value={s}>{titleCase(s)}</option>
              ))}
            </select>
          </label>
          {filtering && (
            <button
              type="button"
              onClick={() => { setName(''); setStatus(''); setPeriod(''); setPage(1) }}
              className="h-10 rounded-control border border-line px-4 text-sm font-medium text-ink-muted hover:bg-surface-sunken"
            >
              Clear Filters
            </button>
          )}
        </div>
      </Card>

      <Card role="region" aria-label="timecards" className="p-0">
        {timecards.isPending && <p className="p-5 text-sm text-ink-muted">Loading …</p>}
        {timecards.isError && (
          <p className="p-5 text-sm text-danger-red">
            Failed to load: {errorMessage(timecards.error)}
          </p>
        )}
        {sorted && filtered.length === 0 && (
          <p className="p-5 text-sm text-ink-muted">
            {sorted.length === 0 ? 'No timecards yet.' : 'No timecards match these filters.'}
          </p>
        )}
        {sorted && filtered.length > 0 && (
          <>
            <div className="overflow-x-auto">
              <table className={`${tableClass} min-w-[46rem]`} aria-label="Timecards">
                <thead>
                  <tr className="border-b border-line bg-surface-sunken/60">
                    <th className={headCls}>Employee</th>
                    <th className={headCls}>Pay Period</th>
                    <th className={`${headCls} text-right`}>Hours</th>
                    <th className={headCls}>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((c) => (
                    <tr
                      key={c.timecard_id}
                      className="border-b border-line transition-colors last:border-0 hover:bg-surface-sunken"
                    >
                      <td className={rowCellCls}>
                        <button
                          type="button"
                          onClick={() => selectCard(c.timecard_id)}
                          className="rounded-control text-left font-semibold text-accent hover:underline"
                        >{c.employee_name}</button>
                      </td>
                      <td className={`${rowCellCls} whitespace-nowrap text-ink-muted`}>
                        {longDate(c.period_start)}
                      </td>
                      <td className={`${rowCellCls} text-right font-medium tabular-nums text-ink`}>
                        {hm(c.total_minutes)}
                      </td>
                      <td className={rowCellCls}>
                        <Badge tone={statusTone(c.status)}>{titleCase(c.status)}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-5 py-3.5 text-sm">
              <p className="text-ink-muted">
                Showing{' '}
                <span className="font-medium text-ink">
                  {(current - 1) * PAGE_SIZE + 1}–{Math.min(current * PAGE_SIZE, filtered.length)}
                </span>{' '}
                of <span className="font-medium text-ink">{filtered.length}</span>
              </p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setPage(current - 1)}
                  disabled={current === 1}
                  className={pagerButtonCls}
                >Previous</button>
                <span className="px-1 text-ink-muted">
                  Page <span className="font-medium text-ink">{current}</span> of {pageCount}
                </span>
                <button
                  type="button"
                  onClick={() => setPage(current + 1)}
                  disabled={current === pageCount}
                  className={pagerButtonCls}
                >Next</button>
              </div>
            </div>
          </>
        )}
      </Card>

      {selected !== null && (
        <Modal
          title={detail.data?.employee_name ?? 'Timecard'}
          size="xl"
          subtitle={
            detail.data ? dateRange(detail.data.period_start, detail.data.period_end) : undefined
          }
          onClose={() => setSelected(null)}
        >
          {/* The landmark stays on an inner element: the modal owns the dialog
              role, and the review flow is located by this region name. */}
          <div role="region" aria-label="timecard detail" className="flex flex-col gap-4">
            {detail.isPending && <p className="text-sm text-ink-muted">Loading …</p>}
            {detail.isError && (
              <p className="text-sm text-danger-red">Failed to load: {errorMessage(detail.error)}</p>
            )}
            {detail.data && (
              <>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <span className="flex items-center gap-2 text-sm">
                    <Badge tone={statusTone(detail.data.status)}>
                      {titleCase(detail.data.status)}
                    </Badge>
                    <span className="text-ink-muted">
                      {hm(detail.data.total_minutes)} over {detail.data.days.length} day
                      {detail.data.days.length === 1 ? '' : 's'}
                    </span>
                  </span>
                  {detail.data.status === 'approved' ? (
                    <button
                      type="button"
                      onClick={() => reopen.mutate(detail.data.timecard_id)}
                      disabled={reopen.isPending}
                      className="h-10 rounded-control border border-line px-4 text-sm font-medium text-accent hover:bg-surface-sunken disabled:opacity-50"
                    >Reopen</button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => approve.mutate(detail.data.timecard_id)}
                      disabled={approve.isPending || unacknowledged.length > 0}
                      className="h-10 rounded-control bg-accent px-5 text-sm font-semibold text-accent-contrast transition-opacity hover:opacity-90 disabled:opacity-50"
                    >Approve</button>
                  )}
                </div>
                {approve.isError && (
                  <p className="text-sm text-danger-red">
                    Approve failed: {errorMessage(approve.error)}
                  </p>
                )}
                {reopen.isError && (
                  <p className="text-sm text-danger-red">
                    Reopen failed: {errorMessage(reopen.error)}
                  </p>
                )}
                {detail.data.status !== 'approved' && unacknowledged.length > 0 && (
                  <p className="rounded-lg bg-danger-red-soft px-4 py-3 text-sm text-danger-red">
                    {unacknowledged.length} unverified punch
                    {unacknowledged.length === 1 ? '' : 'es'} — the face did not match the record
                    on file. Review each photo and tick “reviewed” to approve anyway.
                  </p>
                )}
                <div className="overflow-x-auto rounded-card border border-line">
                  <table className={tableClass} aria-label="Days">
                    <thead>
                      <tr className="border-b border-line bg-surface-sunken/60">
                        <th className={headCls}>Date</th>
                        <th className={`${headCls} text-right`}>Hours</th>
                        <th className={headCls}>Warnings</th>
                        <th className={`${headCls} w-1/2`}>Day</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.data.days.map((d) => (
                        <tr key={d.business_date} className="border-b border-line last:border-0">
                          <td className={`${rowCellCls} whitespace-nowrap font-medium text-ink`}>
                            {longDate(d.business_date)}
                          </td>
                          <td className={`${rowCellCls} text-right tabular-nums`}>
                            {hm(d.worked_minutes)}
                          </td>
                          <td className={rowCellCls}>
                            <span className="flex flex-wrap gap-1">
                              {d.warnings.map((w) => <Badge key={w} tone="warn">{w}</Badge>)}
                            </span>
                          </td>
                          <td className={rowCellCls}>
                            <DayTimeline
                              punches={d.punches ?? []}
                              acknowledgeable={detail.data?.status !== 'approved'}
                              acked={acked}
                              onAcknowledge={(id, checked) => {
                                setAcked((prev) => {
                                  const next = new Set(prev)
                                  if (checked) next.add(id)
                                  else next.delete(id)
                                  return next
                                })
                              }}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        </Modal>
      )}
    </div>
  )
}
