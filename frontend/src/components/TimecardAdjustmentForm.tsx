// A timecard correction (approver path). Punches stay immutable; this ADDS a
// synthetic event — a missed clock-out, a lunch that was never punched — and
// the server merges it into the day's timeline and returns the updated card.
//
// The reason is the whole audit value of a correction, so it is required here
// and bounded to the server's column width. The time is sent as an AWARE
// datetime from the browser's own zone, which the form says out loud: a GM
// correcting their own property's card is in that property's zone, and a
// silently guessed one would move someone's paid hours.
import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { addTimecardAdjustment } from '../api/client'
import type { Timecard, TimecardAdjustmentBody } from '../api/types'
import { errorMessage } from '../lib/errors'
import { PUNCH_TYPES, REASON_MAX, toAwareIso } from '../lib/timecardAdjustment'
import { controlClass } from './ui'

export default function TimecardAdjustmentForm({
  timecard,
  onSaved,
}: {
  timecard: Timecard
  onSaved: (card: Timecard) => void
}) {
  // Default to the latest day that has punches — the one most likely being
  // corrected — and fall back to the period start for an empty card.
  const [day, setDay] = useState(
    timecard.days.length > 0
      ? timecard.days.reduce((a, d) => (d.business_date > a ? d.business_date : a), timecard.days[0]!.business_date)
      : timecard.period_start,
  )
  const [punchType, setPunchType] = useState<TimecardAdjustmentBody['punch_type']>('clock_in')
  const [time, setTime] = useState('')
  const [reason, setReason] = useState('')
  const [clientError, setClientError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone

  const m = useMutation({
    mutationFn: (body: TimecardAdjustmentBody) => addTimecardAdjustment(timecard.timecard_id, body),
    onSuccess: (card) => {
      setDone(`Added ${PUNCH_TYPES.find((p) => p.value === punchType)?.label.toLowerCase()} at ${time} on ${day}.`)
      setTime('')
      setReason('')
      onSaved(card)
    },
  })

  if (timecard.status === 'approved') {
    return (
      <p className="text-sm text-ink-muted">
        This card is approved and locked. Reopen it to add a correction.
      </p>
    )
  }

  function submit(e: React.FormEvent) {
    e.preventDefault()
    setDone(null)
    setClientError(null)
    if (day < timecard.period_start || day > timecard.period_end) {
      setClientError(`Pick a day inside this period (${timecard.period_start} to ${timecard.period_end}).`)
      return
    }
    if (!/^\d{2}:\d{2}$/.test(time)) {
      setClientError('Enter the time of the punch.')
      return
    }
    const why = reason.trim()
    if (why.length === 0) {
      setClientError('A correction needs a reason — it is the audit record.')
      return
    }
    if (why.length > REASON_MAX) {
      setClientError(`Keep the reason under ${REASON_MAX} characters.`)
      return
    }
    m.mutate({ punch_type: punchType, adjusted_at: toAwareIso(day, time), business_date: day, reason: why })
  }

  const error = clientError ?? (m.isError ? errorMessage(m.error) : null)
  const describedBy = error !== null ? 'tc-adj-error' : undefined

  return (
    <form
      role="region"
      aria-label="add a correction"
      // One message style for every refusal (ours and the server's), rather
      // than a native tooltip for the date bounds and a red line for the rest.
      noValidate
      onSubmit={submit}
      className="flex flex-col gap-2 rounded-card border border-line p-3"
    >
      <div>
        <h3 className="text-sm font-semibold text-ink">Add a correction</h3>
        <p className="text-xs text-ink-muted">
          Adds a punch the kiosk missed. Existing punches are never changed. Times are in your
          time zone ({zone}).
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor="tc-adj-day">
          Day
          <input
            id="tc-adj-day"
            type="date"
            className={controlClass}
            value={day}
            min={timecard.period_start}
            max={timecard.period_end}
            onChange={(e) => setDay(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor="tc-adj-type">
          Punch
          <select
            id="tc-adj-type"
            className={controlClass}
            value={punchType}
            onChange={(e) => setPunchType(e.target.value as TimecardAdjustmentBody['punch_type'])}
          >
            {PUNCH_TYPES.map((p) => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor="tc-adj-time">
          Time
          <input
            id="tc-adj-time"
            type="time"
            className={controlClass}
            value={time}
            onChange={(e) => setTime(e.target.value)}
            aria-invalid={error !== null}
            aria-describedby={describedBy}
          />
        </label>
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor="tc-adj-reason">
          Reason
          <input
            id="tc-adj-reason"
            className={controlClass}
            value={reason}
            maxLength={REASON_MAX}
            placeholder="e.g. forgot to clock out, left at 5:00 per supervisor"
            onChange={(e) => setReason(e.target.value)}
            aria-invalid={error !== null}
            aria-describedby={describedBy}
          />
        </label>
        <button
          type="submit"
          disabled={m.isPending}
          className="h-10 rounded-control bg-accent px-4 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >
          {m.isPending ? 'Saving…' : 'Add correction'}
        </button>
      </div>
      {error !== null && (
        <p id="tc-adj-error" role="alert" className="text-sm text-danger-red">{error}</p>
      )}
      {done !== null && error === null && (
        <p role="status" className="text-sm text-ok-green">{done}</p>
      )}
    </form>
  )
}
