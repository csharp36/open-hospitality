// The selected period's detail: state, both gap lists, and the post and
// close/reopen controls. The unposted count always says PMS in the visible copy — the
// check is pms_daily-only by design (PeriodModel.unposted_dates' docstring in
// gl_api.py is where that scope is set), and copy that totals up to "all
// caught up" would claim the payroll side was verified when it was never
// checked. Close confirms in the card itself, never window.confirm (the
// ConnectedActions precedent); reopen requires a reason. GlPage owns the
// periods query this card writes into and invalidates.

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { closeGlPeriod, postGlRange, reopenGlPeriod } from '../api/gl'
import type { GlPeriod, GlPostOutcome } from '../api/types'
import { errorMessage } from '../lib/errors'
import { Badge, Card, cellClass, controlClass, headCellClass, tableClass } from './ui'
import type { BadgeTone } from './ui'

// Total over the status union: a widened GlPostOutcome['status'] fails tsc
// here. `skipped` is neutral, not a warning — the backend uses it for the
// honest "no chart yet / nothing to post" answer (gl_api's
// test_post_without_a_chart_reports_honest_skips), and painting it amber
// would nag every tenant that has not seeded a chart.
const outcomeTone: Record<GlPostOutcome['status'], BadgeTone> = {
  posted: 'ok',
  reposted: 'ok',
  noop: 'neutral',
  reversed: 'warn',
  skipped: 'neutral',
  failed: 'danger',
}

export default function PeriodDetailCard({
  property,
  fiscalYear,
  period: p,
  canManage,
}: {
  property: string
  fiscalYear: number
  period: GlPeriod
  canManage: boolean
}) {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const [reason, setReason] = useState('')
  // Modal-lite a11y: focus lands on the confirm button when the button row
  // swaps to the confirm pair (the JournalDrillPanel focus-on-open shape) —
  // the click target the operator just pressed is gone from under them.
  const confirmRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (confirming) confirmRef.current?.focus()
  }, [confirming])

  // Both verbs change the period's stored state, and the rail chips render
  // from the same query — one invalidation moves the chip and this card
  // together.
  const invalidatePeriods = () =>
    queryClient.invalidateQueries({ queryKey: ['gl-periods', property, fiscalYear] })
  const close = useMutation({
    mutationFn: () => closeGlPeriod(property, p.period_key),
    onSuccess: (resp) => {
      setConfirming(false)
      // The close response is the freshest picture of state and gaps, so it
      // is written straight into the periods cache — the card and the rail
      // chip re-render from it at once, and the invalidation's refetch can
      // only ever replace it with something newer. Holding the response in
      // mutation state instead would shadow every later refetch.
      queryClient.setQueryData<GlPeriod[]>(['gl-periods', property, fiscalYear], (old) =>
        old?.map((q) =>
          q.period_key === p.period_key
            ? {
                ...q,
                state: resp.state,
                unposted_dates: resp.unposted_dates,
                orphaned_dates: resp.orphaned_dates,
              }
            : q,
        ),
      )
      void invalidatePeriods()
    },
  })
  const reopen = useMutation({
    mutationFn: () => reopenGlPeriod(property, p.period_key, reason),
    onSuccess: () => {
      setReason('')
      void invalidatePeriods()
    },
  })
  // The period's own bounds are the range — at most ~31 days. Prefix
  // invalidation on the trial-balance and entries families: this card does
  // not track which scopes of those queries are cached, so it drops them all
  // rather than guess. Outcomes live in mutation state and clear when the
  // card remounts — GlPage's key on this card is where a scope switch forces
  // that remount.
  const post = useMutation({
    mutationFn: () =>
      postGlRange({ property_id: property, date_from: p.date_from, date_to: p.date_to }),
    onSuccess: () => {
      void invalidatePeriods()
      void queryClient.invalidateQueries({ queryKey: ['gl-trial-balance'] })
      void queryClient.invalidateQueries({ queryKey: ['gl-entries'] })
    },
  })

  const { state, unposted_dates: unposted, orphaned_dates: orphaned } = p

  // A confirm is armed only from the open-state button row, so it belongs to
  // the open spell it was armed in. State changes reach this card in place —
  // GlPage's key on this card is where only a scope switch forces a remount —
  // so when the state moves under a primed confirm (a close from elsewhere
  // landing via refetch, then a reopen), the confirm is discarded here,
  // during render, rather than re-offered in a later open spell that never
  // asked for it. Render-time adjustment, not an effect: an effect would
  // commit the stale confirm to the screen first.
  const [prevState, setPrevState] = useState(state)
  if (state !== prevState) {
    setPrevState(state)
    if (confirming) setConfirming(false)
    // Same rule for the red lines: a refusal answers an attempt made in the
    // spell that just ended, and must not resurface under the next spell's
    // fresh controls.
    if (close.error !== null) close.reset()
    if (reopen.error !== null) reopen.reset()
  }

  const gapPhrases: string[] = []
  if (unposted.length > 0)
    gapPhrases.push(`${unposted.length} unposted PMS day${unposted.length === 1 ? '' : 's'}`)
  if (orphaned.length > 0)
    gapPhrases.push(`${orphaned.length} orphaned entr${orphaned.length === 1 ? 'y' : 'ies'}`)
  // With no gaps the confirm degrades to the bare question — there is
  // nothing to warn about, only the state change itself.
  const confirmCopy =
    gapPhrases.length === 0
      ? `Close ${p.period_key}?`
      : `Close ${p.period_key} with ${gapPhrases.join(' and ')}?`

  return (
    <Card role="region" aria-label={`Period ${p.period_key}`} className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-ink">Period {p.period_key}</h2>
          {state === 'closed' ? (
            <Badge tone="neutral">Closed</Badge>
          ) : (
            <Badge tone="ok">Open</Badge>
          )}
        </div>
        <p className="text-xs tabular-nums text-ink-muted">
          {p.date_from} – {p.date_to}
        </p>
      </div>

      <div className="space-y-1">
        {unposted.length > 0 ? (
          <>
            <p className="text-sm font-medium text-ink">
              {unposted.length} PMS day{unposted.length === 1 ? '' : 's'} unposted
            </p>
            <p className="text-xs tabular-nums text-ink-muted">{unposted.join(', ')}</p>
          </>
        ) : (
          <p className="text-sm font-medium text-ink">
            {orphaned.length === 0
              ? 'No PMS-day gaps and no orphaned entries.'
              : 'No PMS-day gaps.'}
          </p>
        )}
        {/* Rendered at zero too: an empty unposted list still says nothing
            about payroll accruals. */}
        <p className="text-xs text-ink-muted">
          Days with promoted PMS facts and no posted journal entry. Payroll accruals are not
          checked here — a day with no accrual is the ordinary no-cost case.
        </p>
      </div>

      {orphaned.length > 0 && (
        <div className="space-y-1">
          {/* "(orphaned)" is introduced here, on the list itself, so the
              close confirm's "{n} orphaned entries" is a term the operator
              has already met — never first encountered inside a destructive
              confirm. */}
          <p className="text-sm font-medium text-ink">
            {orphaned.length} posted entr{orphaned.length === 1 ? 'y lost its' : 'ies lost their'}{' '}
            facts (orphaned)
          </p>
          <p className="text-xs tabular-nums text-ink-muted">{orphaned.join(', ')}</p>
          <p className="text-xs text-ink-muted">
            Days whose entry's source facts were re-transformed away — both sources. Re-posting
            reverses them.
          </p>
        </div>
      )}

      {canManage && state === 'open' && (
        confirming ? (
          <div className="space-y-2">
            <p className="text-sm text-ink">{confirmCopy}</p>
            <div className="flex gap-2">
              <button
                ref={confirmRef}
                type="button"
                className={controlClass}
                disabled={close.isPending}
                onClick={() => close.mutate()}
              >
                Yes, close
              </button>
              <button
                type="button"
                className={controlClass}
                disabled={close.isPending}
                onClick={() => {
                  // A failed attempt's red line must not outlive the confirm
                  // it belonged to.
                  close.reset()
                  setConfirming(false)
                }}
              >
                Cancel
              </button>
            </div>
            {/* In the confirm's own block, beside the pair it answers: a
                refused close must not land at the card foot, below a month
                of outcome rows. */}
            {close.error !== null && (
              <p role="alert" className="text-sm text-danger-red">
                {errorMessage(close.error)}
              </p>
            )}
          </div>
        ) : (
          // Post before Close: Post is the remedy for the gaps named above,
          // Close is the commitment made once they are dealt with.
          <div className="space-y-2">
            <div className="flex gap-2">
              <button
                type="button"
                className={controlClass}
                disabled={post.isPending}
                onClick={() => post.mutate()}
              >
                Post {p.period_key}
              </button>
              <button
                type="button"
                className={controlClass}
                disabled={post.isPending}
                onClick={() => setConfirming(true)}
              >
                Close {p.period_key}
              </button>
            </div>
            {/* A post failure's red line dies with its button — it must not
                sit unlabeled under the reopen form of a since-closed
                period. */}
            {post.error !== null && (
              <p role="alert" className="text-sm text-danger-red">
                {errorMessage(post.error)}
              </p>
            )}
          </div>
        )
      )}

      {canManage && state === 'closed' && (
        <div className="space-y-2">
          <label className="block text-xs font-medium text-ink-muted" htmlFor="gl-reopen-reason">
            Reason for reopening
          </label>
          <textarea
            id="gl-reopen-reason"
            className={`${controlClass} block w-full max-w-md`}
            rows={2}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button
            type="button"
            className={controlClass}
            disabled={reason.trim() === '' || reopen.isPending}
            onClick={() => reopen.mutate()}
          >
            Reopen {p.period_key}
          </button>
          {/* In the form's own block, beside the button it answers: a
              refused reopen must not land at the card foot, below a month
              of outcome rows. */}
          {reopen.error !== null && (
            <p role="alert" className="text-sm text-danger-red">
              {errorMessage(reopen.error)}
            </p>
          )}
        </div>
      )}

      {post.data !== undefined && (
        <div className="space-y-1">
          {/* The table outlives the open-state controls (the outcomes are the
              operator's record of what they just did), so it names itself. */}
          <p className="text-sm font-medium text-ink">Post outcomes</p>
          <div className="overflow-x-auto">
            <table className={tableClass}>
              <thead>
                <tr className="border-b border-line">
                  <th className={headCellClass}>Date</th>
                  <th className={headCellClass}>Source</th>
                  <th className={headCellClass}>Status</th>
                  <th className={headCellClass}>Entry</th>
                  <th className={headCellClass}>Message</th>
                </tr>
              </thead>
              <tbody>
                {post.data.map((o) => (
                  <tr
                    key={`${o.business_date}:${o.source_type}`}
                    className="border-b border-line last:border-0"
                  >
                    <td className={`${cellClass} tabular-nums`}>{o.business_date}</td>
                    <td className={cellClass}>{o.source_type}</td>
                    <td className={cellClass}>
                      <Badge tone={outcomeTone[o.status]}>{o.status}</Badge>
                    </td>
                    {/* Blank over an em-dash placeholder: an outcome that names
                        no entry (a noop, a skip) is the ordinary case, not
                        missing data. Same for a message. */}
                    <td className={`${cellClass} tabular-nums`}>
                      {o.entry_id !== null ? `entry #${o.entry_id}` : ''}
                    </td>
                    <td className={cellClass}>{o.message ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Card>
  )
}
