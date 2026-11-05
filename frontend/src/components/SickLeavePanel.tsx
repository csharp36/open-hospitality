// Sick-leave panel (E4): the balance, this year's use against the cap, and the
// three audited writes — record usage, void a usage, adjust the balance.
//
// Every figure here is DERIVED on the server from the entry timeline; the
// panel never computes a balance itself, it only shows what the fold returns
// and refetches after each write. Each form carries its own error, next to
// its own button, so a refusal ("would exceed the calendar-year cap of …")
// lands where the admin is looking rather than at the bottom of the modal.
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError } from '../api/client'
import {
  adjustSickLeave,
  getSickLeave,
  recordSickLeaveUsage,
  voidSickLeaveUsage,
} from '../api/sickLeave'
import type { SickLeaveStatus } from '../api/types'
import { errorMessage } from '../lib/errors'
import Modal from './Modal'
import { Badge, controlClass } from './ui'

const hoursFmt = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

/** "12.5000" -> "12.50". Display only; the string goes back to the server untouched. */
function fmtHours(s: string): string {
  const n = Number(s)
  return Number.isFinite(n) ? hoursFmt.format(n) : s
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

/** A positive decimal with at most two places — the shape the server's
 * Decimal field accepts without rounding surprises. */
const HOURS_RE = /^\d+(\.\d{1,2})?$/
const SIGNED_HOURS_RE = /^-?\d+(\.\d{1,2})?$/

function windowLabel(window: string | null): string {
  if (window === null) return 'not recorded'
  return window.replace(/_/g, ' ')
}

export default function SickLeavePanel({
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
    queryKey: ['sick-leave', employeeId],
    queryFn: () => getSickLeave(employeeId),
    // A 403 ("insufficient role") or 404 will not change on retry; the app
    // default would hold this modal on "Loading…" for ~7s before saying so.
    retry: (count, err) =>
      !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 3,
  })

  return (
    <Modal title="Sick leave" subtitle={employeeName} size="md" onClose={onClose}>
      <div className="flex flex-col gap-5">
        {status.isPending && <p className="text-sm text-ink-muted">Loading balance…</p>}
        {status.isError && (
          <p role="alert" className="text-sm text-danger-red">
            Failed to load sick leave: {errorMessage(status.error)}
          </p>
        )}
        {status.data && (
          <>
            <Summary status={status.data} />
            <UsageForm
              employeeId={employeeId}
              onDone={() => void qc.invalidateQueries({ queryKey: ['sick-leave', employeeId] })}
            />
            <VoidForm
              employeeId={employeeId}
              onDone={() => void qc.invalidateQueries({ queryKey: ['sick-leave', employeeId] })}
            />
            <AdjustForm
              employeeId={employeeId}
              onDone={() => void qc.invalidateQueries({ queryKey: ['sick-leave', employeeId] })}
            />
          </>
        )}
      </div>
    </Modal>
  )
}

function Summary({ status }: { status: SickLeaveStatus }) {
  const used = Number(status.used_this_year_hours)
  const cap = status.usage_cap_hours === null ? null : Number(status.usage_cap_hours)
  const pct = cap !== null && cap > 0 ? Math.min(100, (used / cap) * 100) : null
  const atCap = cap !== null && used >= cap
  return (
    <section role="region" aria-label="sick leave balance" className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Balance" value={`${fmtHours(status.balance_hours)} h`} big />
        <Stat
          label="Used this year"
          value={
            cap === null
              ? `${fmtHours(status.used_this_year_hours)} h`
              : `${fmtHours(status.used_this_year_hours)} / ${fmtHours(status.usage_cap_hours!)} h`
          }
        />
        <Stat
          label="Accrual cap"
          value={status.accrual_cap_hours === null ? '—' : `${fmtHours(status.accrual_cap_hours)} h`}
        />
        <Stat label="Cap window" value={windowLabel(status.usage_cap_window)} />
      </div>
      {pct !== null && (
        <div className="flex items-center gap-3">
          <div
            className="h-2 flex-1 overflow-hidden rounded-full bg-brand-surface"
            role="progressbar"
            aria-label="Usage against the calendar-year cap"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(pct)}
          >
            <div
              className={`h-full ${atCap ? 'bg-danger-red' : 'bg-accent'}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          {atCap ? (
            <Badge tone="danger">at cap</Badge>
          ) : (
            <span className="text-xs text-ink-muted">
              {fmtHours(String(cap! - used))} h of headroom
            </span>
          )}
        </div>
      )}
      {cap === null && (
        <p className="text-xs text-ink-muted">
          No usage cap is shown because this employee has no recorded day length yet; the server
          does not enforce the cap until one is on file.
        </p>
      )}
    </section>
  )
}

function Stat({ label, value, big = false }: { label: string; value: string; big?: boolean }) {
  return (
    <div className="rounded-control border border-line px-3 py-2">
      <div className="text-xs font-medium uppercase tracking-wide text-ink-muted">{label}</div>
      <div className={`font-mono tabular-nums ${big ? 'text-2xl font-semibold' : 'text-base'}`}>
        {value}
      </div>
    </div>
  )
}

/** One write form: hours + date (+ optional note), its own busy/error/done
 * state, and a client-side shape check so the obvious typo never reaches
 * the server. Server refusals are shown verbatim — they are written to be
 * read by exactly this person. */
function WriteForm({
  idPrefix,
  title,
  description,
  submitLabel,
  hoursLabel,
  signed,
  withNote,
  mutate,
  onDone,
}: {
  idPrefix: string
  title: string
  description: string
  submitLabel: string
  hoursLabel: string
  signed: boolean
  withNote: boolean
  mutate: (body: { hours: string; on: string; note?: string }) => Promise<unknown>
  onDone: () => void
}) {
  const [hours, setHours] = useState('')
  const [on, setOn] = useState(todayIso)
  const [note, setNote] = useState('')
  const [clientError, setClientError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const m = useMutation({
    mutationFn: mutate,
    onSuccess: () => {
      setDone(`${submitLabel}: ${hours} h on ${on}`)
      setHours('')
      setNote('')
      onDone()
    },
  })

  function submit(e: React.FormEvent) {
    e.preventDefault()
    setDone(null)
    setClientError(null)
    const trimmed = hours.trim()
    if (!(signed ? SIGNED_HOURS_RE : HOURS_RE).test(trimmed)) {
      setClientError(
        signed
          ? 'Enter hours as a number with up to two decimals, e.g. -1.5 or 8.'
          : 'Enter hours as a positive number with up to two decimals, e.g. 8 or 4.5.',
      )
      return
    }
    const n = Number(trimmed)
    if (signed ? n === 0 : n <= 0) {
      setClientError(signed ? 'An adjustment must be nonzero.' : 'Hours must be greater than zero.')
      return
    }
    if (!on) {
      setClientError('Pick the date.')
      return
    }
    const body: { hours: string; on: string; note?: string } = { hours: trimmed, on }
    if (withNote && note.trim()) body.note = note.trim()
    m.mutate(body)
  }

  const error = clientError ?? (m.isError ? errorMessage(m.error) : null)

  return (
    <form
      role="region"
      aria-label={title.toLowerCase()}
      onSubmit={submit}
      className="flex flex-col gap-2 rounded-control border border-line p-3"
    >
      <div>
        <h3 className="text-sm font-semibold">{title}</h3>
        <p className="text-xs text-ink-muted">{description}</p>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor={`${idPrefix}-hours`}>
          {hoursLabel}
          <input
            id={`${idPrefix}-hours`}
            className={`${controlClass} w-28 font-mono`}
            inputMode="decimal"
            value={hours}
            onChange={(e) => setHours(e.target.value)}
            aria-invalid={error !== null}
            aria-describedby={error !== null ? `${idPrefix}-error` : undefined}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor={`${idPrefix}-on`}>
          Date
          <input
            id={`${idPrefix}-on`}
            type="date"
            className={controlClass}
            value={on}
            onChange={(e) => setOn(e.target.value)}
          />
        </label>
        {withNote && (
          <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor={`${idPrefix}-note`}>
            Note
            <input
              id={`${idPrefix}-note`}
              className={controlClass}
              value={note}
              maxLength={300}
              placeholder="kept on the entry, e.g. opening balance from prior system"
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
        )}
        <button
          type="submit"
          disabled={m.isPending}
          className="rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-40"
        >
          {m.isPending ? 'Saving…' : submitLabel}
        </button>
      </div>
      {error !== null && (
        <p id={`${idPrefix}-error`} role="alert" className="text-sm text-danger-red">
          {error}
        </p>
      )}
      {done !== null && error === null && (
        <p role="status" className="text-sm text-ok-green">
          {done}
        </p>
      )}
    </form>
  )
}

function UsageForm({ employeeId, onDone }: { employeeId: number; onDone: () => void }) {
  return (
    <WriteForm
      idPrefix="sl-usage"
      title="Record sick leave"
      description="Hours taken on a day the employee had a placement. Refused if it would overdraw the balance or exceed the calendar-year cap."
      submitLabel="Record usage"
      hoursLabel="Hours taken"
      signed={false}
      withNote={false}
      mutate={(b) => recordSickLeaveUsage(employeeId, b)}
      onDone={onDone}
    />
  )
}

function VoidForm({ employeeId, onDone }: { employeeId: number; onDone: () => void }) {
  return (
    <WriteForm
      idPrefix="sl-void"
      title="Void a usage"
      description="Reverse hours recorded by mistake. Restores the balance and the cap headroom it used — an adjustment would restore only the balance."
      submitLabel="Void usage"
      hoursLabel="Hours to void"
      signed={false}
      withNote={false}
      mutate={(b) => voidSickLeaveUsage(employeeId, b)}
      onDone={onDone}
    />
  )
}

function AdjustForm({ employeeId, onDone }: { employeeId: number; onDone: () => void }) {
  return (
    <WriteForm
      idPrefix="sl-adj"
      title="Adjust the balance"
      description="Opening balances and corrections. Signed hours: positive adds, negative removes. Audited either way."
      submitLabel="Apply adjustment"
      hoursLabel="Hours (±)"
      signed
      withNote
      mutate={(b) => adjustSickLeave(employeeId, b)}
      onDone={onDone}
    />
  )
}
