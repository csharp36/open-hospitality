// A property's pay schedule: what it is now (with the current period and its
// pay day derived from it), and the form to set it. The one rule that bites
// is the server's: the anchor must sit on the platform biweekly grid. When it
// refuses, the refusal names that grid, so the card turns the two nearest
// grid dates into buttons instead of leaving the admin to count fortnights.
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { getPaySchedule, platformAnchorFromError, putPaySchedule } from '../api/paySchedule'
import type { PaySchedule, PayScheduleBody } from '../api/types'
import { errorMessage } from '../lib/errors'
import { checkDate, nearestAligned, periodContaining, todayIso } from '../lib/paySchedule'
import { Badge, Card, controlClass } from './ui'

const OFFSET_MIN = 0
const OFFSET_MAX = 30

function longDate(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number) as [number, number, number]
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
  })
}

export default function PayScheduleCard({ propertyId }: { propertyId: string }) {
  const qc = useQueryClient()
  const schedule = useQuery({
    queryKey: ['pay-schedule', propertyId],
    queryFn: () => getPaySchedule(propertyId),
  })

  return (
    <Card>
      <section role="region" aria-label="pay schedule" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold text-ink">Pay schedule</h2>
          {schedule.data === null && <Badge tone="warn">not set</Badge>}
        </div>
        {schedule.isPending && <p className="text-sm text-ink-muted">Loading …</p>}
        {schedule.isError && (
          <p role="alert" className="text-sm text-danger-red">
            Failed to load the pay schedule: {errorMessage(schedule.error)}
          </p>
        )}
        {schedule.data !== undefined && !schedule.isError && (
          <>
            {schedule.data === null ? (
              <p className="text-sm text-ink-muted">
                No pay schedule yet for this property. Pay runs need one to know which fortnight
                they cover and when the check date falls.
              </p>
            ) : (
              <Current schedule={schedule.data} />
            )}
            <ScheduleForm
              key={propertyId}
              propertyId={propertyId}
              existing={schedule.data}
              onSaved={(saved) => qc.setQueryData(['pay-schedule', propertyId], saved)}
            />
          </>
        )}
      </section>
    </Card>
  )
}

function Current({ schedule }: { schedule: PaySchedule }) {
  const period = periodContaining(schedule.anchor, todayIso())
  const payDay = checkDate(period, schedule.check_date_offset_days)
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
      <div>
        <dt className="text-xs font-medium uppercase tracking-wide text-ink-muted">Frequency</dt>
        <dd className="text-ink">{schedule.frequency}</dd>
      </div>
      <div>
        <dt className="text-xs font-medium uppercase tracking-wide text-ink-muted">Anchor</dt>
        <dd className="font-mono tabular-nums text-ink">{schedule.anchor}</dd>
      </div>
      <div>
        <dt className="text-xs font-medium uppercase tracking-wide text-ink-muted">Current period</dt>
        <dd className="text-ink">{longDate(period.start)} – {longDate(period.end)}</dd>
      </div>
      <div>
        <dt className="text-xs font-medium uppercase tracking-wide text-ink-muted">Pay day</dt>
        <dd className="text-ink">
          {longDate(payDay)}
          <span className="text-ink-muted"> · {schedule.check_date_offset_days} days after the period</span>
        </dd>
      </div>
    </dl>
  )
}

function ScheduleForm({
  propertyId,
  existing,
  onSaved,
}: {
  propertyId: string
  existing: PaySchedule | null
  onSaved: (saved: PaySchedule) => void
}) {
  const [anchor, setAnchor] = useState(existing?.anchor ?? '')
  const [offset, setOffset] = useState(String(existing?.check_date_offset_days ?? 5))
  const [clientError, setClientError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  // The server's own grid anchor, learned from its first refusal; lets the
  // card offer aligned dates instead of a number to count from.
  const [platformAnchor, setPlatformAnchor] = useState<string | null>(null)

  const m = useMutation({
    mutationFn: (body: PayScheduleBody) => putPaySchedule(propertyId, body),
    onSuccess: (saved) => {
      setDone(`Saved. Periods run from ${saved.anchor}, pay day ${saved.check_date_offset_days} days after each.`)
      onSaved(saved)
    },
    onError: (err) => {
      // A refusal replaces any earlier success line.
      setDone(null)
      const a = platformAnchorFromError(err)
      if (a !== null) setPlatformAnchor(a)
    },
  })

  function submit(e: React.FormEvent) {
    e.preventDefault()
    setClientError(null)
    setDone(null)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(anchor)) {
      setClientError('Pick the first day of a pay period as the anchor.')
      return
    }
    const n = Number(offset)
    if (!Number.isInteger(n) || n < OFFSET_MIN || n > OFFSET_MAX) {
      setClientError(`Check date offset must be a whole number of days from ${OFFSET_MIN} to ${OFFSET_MAX}.`)
      return
    }
    m.mutate({ frequency: 'biweekly', anchor, check_date_offset_days: n })
  }

  const serverError = m.isError ? errorMessage(m.error) : null
  const error = clientError ?? serverError
  const suggestions =
    serverError !== null && platformAnchor !== null && anchor ? nearestAligned(platformAnchor, anchor) : null

  return (
    <form
      noValidate
      onSubmit={submit}
      aria-label={existing ? 'change pay schedule' : 'set pay schedule'}
      className="flex flex-col gap-2 border-t border-line pt-3"
    >
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor="ps-frequency">
          Frequency
          <select id="ps-frequency" className={controlClass} value="biweekly" disabled>
            <option value="biweekly">Biweekly</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor="ps-anchor">
          Period anchor (first day of a period)
          <input
            id="ps-anchor"
            type="date"
            className={controlClass}
            value={anchor}
            onChange={(e) => setAnchor(e.target.value)}
            aria-invalid={error !== null}
            aria-describedby={error !== null ? 'ps-error' : undefined}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-muted" htmlFor="ps-offset">
          Check date, days after period end
          <input
            id="ps-offset"
            type="number"
            inputMode="numeric"
            min={OFFSET_MIN}
            max={OFFSET_MAX}
            className={`${controlClass} w-24`}
            value={offset}
            onChange={(e) => setOffset(e.target.value)}
            aria-invalid={error !== null}
            aria-describedby={error !== null ? 'ps-error' : undefined}
          />
        </label>
        <button
          type="submit"
          disabled={m.isPending}
          className="h-10 rounded-control bg-accent px-4 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >
          {m.isPending ? 'Saving…' : existing ? 'Save changes' : 'Set schedule'}
        </button>
      </div>
      <p className="text-xs text-ink-muted">
        Only biweekly is offered: all period math runs on the platform grid, so the anchor must
        be a whole number of fortnights from it. Per-property grids are future work.
      </p>
      {error !== null && (
        <p id="ps-error" role="alert" className="text-sm text-danger-red">{error}</p>
      )}
      {suggestions !== null && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-ink-muted">Nearest dates on the grid:</span>
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => { setAnchor(s); m.reset(); setClientError(null) }}
              className="rounded-control border border-line px-3 py-1 font-mono text-sm text-accent hover:bg-surface-sunken"
            >
              Use {s}
            </button>
          ))}
        </div>
      )}
      {done !== null && error === null && (
        <p role="status" className="text-sm text-ok-green">{done}</p>
      )}
    </form>
  )
}
