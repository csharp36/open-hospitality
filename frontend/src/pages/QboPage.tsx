// QBO push page: property + month picker (state in the URL search params), a
// per-date table over the push ledger, a JE preview panel, and Push — the
// portal's first QBO write — behind a confirm dialog that restates the plan
// totals. All data fetching lives here; the table/panel/dialog components
// below stay presentational.
//
// Which dates get Preview/Push? Every date of the picked month inside the
// property's first..last window (from /api/properties — the honest "has
// facts" source for the pilot), UNION any ledger-row dates, so a recorded
// push never disappears from the table even if the window shifts.
//
// Push outcomes: `stale` and `failed` arrive as HTTP 200 PushResults — the
// mutation branches on `status` and renders them as attention states
// (warn/danger), never as success. Only the unmapped-GL 422 (rendered as a
// curation worklist) and transport failures (502) reject.

import { useEffect, useRef, useState } from 'react'
import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'

import {
  ApiError,
  getQboPreview,
  getQboStatus,
  postQboPush,
} from '../api/client'
import type {
  JePlan,
  PushLedgerRow,
  PushResult,
  PushResultStatus,
  UnmappedGlLine,
} from '../api/types'
import MonthPickerBar from '../components/MonthPickerBar'
import { useGlobalProperty } from '../lib/propertyContext'
import {
  amountCellClass,
  amountHeadClass,
  Badge,
  type BadgeTone,
  Card,
  cellClass,
  headCellClass,
  PageHeader,
  sectionHeadClass,
  tableClass,
} from '../components/ui'
import { errorMessage } from '../lib/errors'
import { fmtMoney } from '../lib/format'
import { datesInMonth } from '../lib/months'
import type { PropertyMonthSearch } from '../router'

// getRouteApi avoids the router.tsx <-> QboPage.tsx circular value import.
const routeApi = getRouteApi('/qbo')

/** The structured `{"unmapped": [...]}` worklist from a QBO 422, or null. */
function unmappedWorklist(error: unknown): UnmappedGlLine[] | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null
  const body: unknown = error.detailBody
  if (body === null || typeof body !== 'object' || !('unmapped' in body)) return null
  const raw = (body as { unmapped: unknown }).unmapped
  // An empty list is not a worklist — fall through to the plain error render.
  if (!Array.isArray(raw) || raw.length === 0) return null
  const isLine = (v: unknown): v is UnmappedGlLine =>
    v !== null &&
    typeof v === 'object' &&
    typeof (v as UnmappedGlLine).major === 'string' &&
    typeof (v as UnmappedGlLine).sub_category === 'string' &&
    typeof (v as UnmappedGlLine).line_item === 'string'
  return raw.every(isLine) ? raw : null
}

export default function QboPage() {
  const search = routeApi.useSearch()
  const navigate = routeApi.useNavigate()
  const queryClient = useQueryClient()

  const [previewDate, setPreviewDate] = useState<string | null>(null)
  const [confirmDate, setConfirmDate] = useState<string | null>(null)
  const [lastPush, setLastPush] = useState<{ date: string; result: PushResult } | null>(null)

  function updateSearch(patch: Partial<PropertyMonthSearch>) {
    // Any picker change invalidates the open panel/dialog/notice.
    setPreviewDate(null)
    setConfirmDate(null)
    setLastPush(null)
    void navigate({ search: (prev) => ({ ...prev, ...patch }), replace: true })
  }

  // Property is GLOBAL (top-bar selector); only the month is page state.
  const { property, selected } = useGlobalProperty()

  // A property switch invalidates everything the panel was showing: the open
  // preview, a pending confirm, and the last-push notice all belonged to the
  // previous property.
  useEffect(() => {
    setPreviewDate(null)
    setConfirmDate(null)
    setLastPush(null)
  }, [property])

  const statusParams =
    property !== undefined && search.month !== undefined
      ? { property, month: search.month }
      : null

  const statusQuery = useQuery({
    queryKey: ['qbo-status', property, search.month],
    // skipToken disables the query until the picker state is complete.
    queryFn: statusParams === null ? skipToken : () => getQboStatus(statusParams),
  })

  const previewProperty = property
  const previewQuery = useQuery({
    queryKey: ['qbo-preview', previewProperty, previewDate],
    queryFn:
      previewProperty === undefined || previewDate === null
        ? skipToken
        : () => getQboPreview(previewProperty, previewDate),
    retry: false, // the 422 worklist and 404 are definitive, not transient
  })

  const pushMutation = useMutation({
    mutationFn: ({ property, date }: { property: string; date: string }) =>
      postQboPush(property, date),
    onSuccess: (result, { date }) => setLastPush({ date, result }),
    // Refresh the ledger whatever the outcome — even a failed attempt writes
    // a row — and drop every cached plan: after a push (especially a `stale`
    // refusal) a previewed plan may no longer describe the day.
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['qbo-status'] }),
        queryClient.invalidateQueries({ queryKey: ['qbo-preview'] }),
      ]),
  })

  // Latest ledger row per date (the ledger keeps history; push_id is monotonic).
  const latestByDate = new Map<string, PushLedgerRow>()
  for (const row of statusQuery.data ?? []) {
    const prev = latestByDate.get(row.business_date)
    if (prev === undefined || row.push_id > prev.push_id) latestByDate.set(row.business_date, row)
  }

  const windowDates =
    selected !== undefined && search.month !== undefined
      ? datesInMonth(search.month, selected.first_date, selected.last_date)
      : []
  const dates = [...new Set([...windowDates, ...latestByDate.keys()])].sort()

  function openPreview(date: string) {
    setPreviewDate(date)
    setConfirmDate(null)
  }

  function openPushConfirm(date: string) {
    // The confirm dialog restates the plan totals, so pushing rides on the
    // same preview query; the panel opens underneath as supporting detail.
    // A cached plan may predate fact changes — invalidate so the dialog only
    // ever restates a FRESH plan (its ready gate also waits out the refetch).
    void queryClient.invalidateQueries({ queryKey: ['qbo-preview', property, date] })
    setPreviewDate(date)
    setConfirmDate(date)
    setLastPush(null)
    pushMutation.reset()
  }

  function confirmPush() {
    if (property !== undefined && confirmDate !== null) {
      pushMutation.mutate({ property, date: confirmDate })
    }
    setConfirmDate(null)
  }

  return (
    <div className="space-y-4">
      <PageHeader title="QBO Push" />

      <Card>
        <MonthPickerBar
          selected={selected}
          month={search.month}
          onMonthChange={(month) => updateSearch({ month: month === '' ? undefined : month })}
        />
      </Card>

      {statusParams === null ? (
        <p className="text-sm text-ink-muted">Pick a month to view push status.</p>
      ) : (
        <>
          {statusQuery.isPending && (
            <p className="text-sm text-ink-muted">Loading push status…</p>
          )}
          {statusQuery.isError && (
            <p className="text-sm text-danger-red">
              Failed to load push status: {errorMessage(statusQuery.error)}
            </p>
          )}
          {statusQuery.data !== undefined && (
            <StatusTable
              dates={dates}
              latestByDate={latestByDate}
              busy={pushMutation.isPending}
              onPreview={openPreview}
              onPush={openPushConfirm}
            />
          )}
        </>
      )}

      {pushMutation.isPending && pushMutation.variables !== undefined && (
        <p
          role="status"
          className="rounded-control bg-info-blue-soft px-3 py-2 text-sm font-medium text-info-blue"
        >
          Pushing {pushMutation.variables.date}…
        </p>
      )}
      {lastPush !== null && <PushNotice date={lastPush.date} result={lastPush.result} />}
      {pushMutation.isError && <PushErrorNotice error={pushMutation.error} />}

      {previewDate !== null && property !== undefined && (
        <PreviewPanel
          property={property}
          date={previewDate}
          plan={previewQuery.data}
          loading={previewQuery.isPending}
          error={previewQuery.isError ? previewQuery.error : null}
          onClose={() => {
            setPreviewDate(null)
            setConfirmDate(null)
          }}
        />
      )}

      {confirmDate !== null && property !== undefined && (
        <ConfirmPushDialog
          property={property}
          date={confirmDate}
          plan={previewQuery.data}
          planFetching={previewQuery.isFetching}
          planFailed={previewQuery.isError}
          onConfirm={confirmPush}
          onCancel={() => setConfirmDate(null)}
        />
      )}
    </div>
  )
}

// --- Presentational pieces -----------------------------------------------------

const actionButtonClass =
  'rounded-control border border-line bg-surface-raised px-2 py-0.5 text-xs font-medium text-ink ' +
  'hover:bg-surface-sunken disabled:bg-surface-sunken disabled:text-ink-faint'

// Attention states per the API handoff: stale/failed are warn/danger, never
// success-styled. `already-pushed` is informational (nothing new was posted);
// anything unknown (incl. `not pushed`) stays neutral. Badge tone classes
// contain the legacy color words (ok-green/warn-amber/danger-red) the test
// regexes assert on.
const badgeTones: Record<string, BadgeTone> = {
  pushed: 'ok',
  'already-pushed': 'info',
  stale: 'warn',
  failed: 'danger',
}

function StatusBadge({ status }: { status: string }) {
  return <Badge tone={badgeTones[status] ?? 'neutral'}>{status}</Badge>
}

function StatusTable({
  dates,
  latestByDate,
  busy,
  onPreview,
  onPush,
}: {
  dates: string[]
  latestByDate: Map<string, PushLedgerRow>
  /** True while a push is in flight — all actions disable so a second dialog
   * cannot race the outcome. */
  busy: boolean
  onPreview: (date: string) => void
  onPush: (date: string) => void
}) {
  if (dates.length === 0) {
    return <p className="text-sm text-ink-muted">No dates with data in this month.</p>
  }
  return (
    <Card>
      <table className={tableClass} aria-label="QBO push status">
        <thead>
          <tr className="border-b border-line">
            <th className={headCellClass}>Date</th>
            <th className={headCellClass}>Status</th>
            <th className={headCellClass}>QBO JE</th>
            <th className={headCellClass}>Message</th>
            <th className={headCellClass}>Pushed at</th>
            <th className={headCellClass}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {dates.map((date) => {
            const row = latestByDate.get(date)
            return (
              <tr key={date} className="border-b border-line last:border-0">
                <td className={`${cellClass} whitespace-nowrap tabular-nums`}>{date}</td>
                <td className={cellClass}>
                  <StatusBadge status={row?.status ?? 'not pushed'} />
                </td>
                <td className={`${cellClass} tabular-nums`}>{row?.qbo_je_id ?? ''}</td>
                <td className={`${cellClass} text-ink-muted`}>{row?.message ?? ''}</td>
                <td className={`${cellClass} whitespace-nowrap text-ink-muted`}>
                  {row !== undefined ? new Date(row.pushed_at).toLocaleString() : ''}
                </td>
                <td className={`${cellClass} whitespace-nowrap`}>
                  <button
                    type="button"
                    aria-label={`Preview ${date}`}
                    className={actionButtonClass}
                    disabled={busy}
                    onClick={() => onPreview(date)}
                  >
                    Preview
                  </button>{' '}
                  <button
                    type="button"
                    aria-label={`Push ${date}`}
                    className={actionButtonClass}
                    disabled={busy}
                    onClick={() => onPush(date)}
                  >
                    Push
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </Card>
  )
}

function PushNotice({ date, result }: { date: string; result: PushResult }) {
  // Soft status panels: tone text on the matching -soft tint (the AA-checked
  // Badge pairing) — never ink-muted on a tinted background.
  const styles: Record<PushResultStatus, string> = {
    pushed: 'bg-ok-green-soft text-ok-green',
    'already-pushed': 'bg-info-blue-soft text-info-blue',
    stale: 'bg-warn-amber-soft text-warn-amber',
    failed: 'bg-danger-red-soft text-danger-red',
  }
  const texts: Record<PushResultStatus, string> = {
    pushed: `Pushed ${date} to QBO — JE ${result.qbo_je_id ?? '?'}.`,
    'already-pushed': `${date} was already pushed with identical content (JE ${
      result.qbo_je_id ?? '?'
    }). Nothing was posted.`,
    stale: `${date} is stale: the facts changed since the last push. Nothing was posted — review the day before pushing again.`,
    failed: `Push for ${date} failed: ${result.message ?? 'QBO rejected the journal entry.'}`,
  }
  return (
    <p
      role="status"
      className={`rounded-control px-3 py-2 text-sm font-medium ${styles[result.status]}`}
    >
      {texts[result.status]}
    </p>
  )
}

function PushErrorNotice({ error }: { error: unknown }) {
  const worklist = unmappedWorklist(error)
  if (worklist !== null) return <UnmappedWorklist lines={worklist} />
  return (
    <p
      role="alert"
      className="rounded-control bg-danger-red-soft px-3 py-2 text-sm font-medium text-danger-red"
    >
      Push failed: {errorMessage(error)}
    </p>
  )
}

function UnmappedWorklist({ lines }: { lines: UnmappedGlLine[] }) {
  return (
    <section
      aria-label="Unmapped GL worklist"
      aria-live="polite"
      className="rounded-card border border-warn-amber/40 bg-warn-amber-soft p-4"
    >
      <h3 className="text-sm font-semibold text-warn-amber">
        GL curation needed — {lines.length} line{lines.length === 1 ? '' : 's'} without a GL
        account
      </h3>
      {/* text-ink (not ink-muted): muted ink fails AA on the amber tint. */}
      <p className="mt-1 text-sm text-ink">
        This day cannot post until every line item carries a GL account. Curate these in the
        mapping dictionary, then preview again.
      </p>
      <table className={`mt-2 ${tableClass}`}>
        <thead>
          <tr className="border-b border-warn-amber/40 text-left text-xs font-semibold text-warn-amber">
            <th className={cellClass}>Major</th>
            <th className={cellClass}>Sub-category</th>
            <th className={cellClass}>Line item</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr
              key={`${line.major}|${line.sub_category}|${line.line_item}`}
              className="border-b border-warn-amber/20 last:border-0"
            >
              <td className={cellClass}>{line.major}</td>
              <td className={cellClass}>{line.sub_category}</td>
              <td className={cellClass}>{line.line_item}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function PreviewPanel({
  property,
  date,
  plan,
  loading,
  error,
  onClose,
}: {
  property: string
  date: string
  plan: JePlan | undefined
  loading: boolean
  error: unknown | null
  onClose: () => void
}) {
  const worklist = error !== null ? unmappedWorklist(error) : null
  return (
    // Card spreads rest props onto its div, so role="region" + aria-label keep
    // the landmark the tests locate via getByRole('region', { name: /JE preview/ }).
    <Card role="region" aria-label={`JE preview: ${property} ${date}`}>
      <div className="flex items-start justify-between">
        <h2 className={sectionHeadClass}>
          JOURNAL ENTRY PREVIEW — {property} {date}
        </h2>
        <button
          type="button"
          aria-label="Close preview"
          onClick={onClose}
          className="rounded-control px-2 text-xl leading-none text-ink-muted hover:bg-surface-sunken hover:text-ink"
        >
          ×
        </button>
      </div>

      {loading && <p className="mt-2 text-sm text-ink-muted">Loading JE plan…</p>}
      {error !== null &&
        (worklist !== null ? (
          <div className="mt-2">
            <UnmappedWorklist lines={worklist} />
          </div>
        ) : (
          <p className="mt-2 text-sm text-danger-red">
            Failed to build the JE plan: {errorMessage(error)}
          </p>
        ))}
      {plan !== undefined && <PlanTable plan={plan} />}
    </Card>
  )
}

function PlanTable({ plan }: { plan: JePlan }) {
  return (
    <table className={`mt-2 ${tableClass}`}>
      <thead>
        <tr className="border-b border-line">
          <th className={headCellClass}>GL account</th>
          <th className={headCellClass}>Account</th>
          <th className={amountHeadClass}>Debit</th>
          <th className={amountHeadClass}>Credit</th>
          <th className={headCellClass}>Memo</th>
        </tr>
      </thead>
      <tbody>
        {plan.lines.map((line) => (
          <tr
            key={`${line.gl_account_code}|${line.posting}|${line.memo}`}
            className="border-b border-line last:border-0"
          >
            <td className={`${cellClass} tabular-nums`}>{line.gl_account_code}</td>
            <td className={cellClass}>{line.account_name}</td>
            <td className={amountCellClass}>
              {line.posting === 'Debit' ? fmtMoney(line.amount) : ''}
            </td>
            <td className={amountCellClass}>
              {line.posting === 'Credit' ? fmtMoney(line.amount) : ''}
            </td>
            <td className={`${cellClass} text-ink-muted`}>{line.memo}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr className="border-t border-line-strong font-semibold">
          <td colSpan={2} className={cellClass}>
            Totals
          </td>
          <td className={amountCellClass}>{fmtMoney(plan.total_debits)}</td>
          <td className={amountCellClass}>{fmtMoney(plan.total_credits)}</td>
          <td />
        </tr>
      </tfoot>
    </table>
  )
}

function ConfirmPushDialog({
  property,
  date,
  plan,
  planFetching,
  planFailed,
  onConfirm,
  onCancel,
}: {
  property: string
  date: string
  plan: JePlan | undefined
  /** True while the plan is (re)fetching — a cached plan must not be
   * confirmable until the fresh one lands. */
  planFetching: boolean
  planFailed: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  // Modal a11y: focus lands on Cancel (the safe action) when the dialog
  // opens, and Escape cancels from anywhere.
  useEffect(() => {
    cancelRef.current?.focus()
  }, [])
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onCancel])

  // The plan matches the previewed date by query key; only a loaded, FRESH
  // plan for this exact date may be confirmed — while the refetch triggered by
  // opening this dialog is in flight, cached totals stay unconfirmable.
  const ready = plan !== undefined && plan.business_date === date && !planFetching

  return (
    <div className="fixed inset-0 z-10">
      {/* bg-scrim stays dark in BOTH modes (token has no .dark remap) — a
          scrim dims the page behind the overlay; bg-ink/40 would invert to a
          light veil in dark mode. */}
      <div className="absolute inset-0 bg-scrim" onClick={onCancel} aria-hidden="true" />
      {/* Centered overlay — free on all four edges, so full card rounding
          (unlike DrillPanel, which is edge-anchored and rounds only its free
          edge). */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Confirm QBO push"
        className="absolute top-1/3 left-1/2 w-full max-w-md -translate-x-1/2 rounded-card border border-line bg-surface-raised p-6 shadow-overlay"
      >
        <h2 className="text-lg font-semibold text-ink">Confirm push</h2>
        {ready ? (
          <p className="mt-2 text-sm text-ink">
            Post JE for {property} {date} — debits {fmtMoney(plan.total_debits)} = credits{' '}
            {fmtMoney(plan.total_credits)}?
          </p>
        ) : planFailed ? (
          <p className="mt-2 text-sm text-danger-red">
            The JE plan could not be built — see the preview panel for the reason. Nothing can be
            pushed.
          </p>
        ) : (
          <p className="mt-2 text-sm text-ink-muted">Loading JE plan…</p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-control border border-line bg-surface-raised px-3 py-1.5 text-sm font-medium text-ink hover:bg-surface-sunken"
          >
            Cancel
          </button>
          {/* Primary action. text-accent-contrast, NOT text-white: the
              dark-mode accent is indigo-400, where white text fails AA — the
              contrast token flips to near-black there. */}
          <button
            type="button"
            disabled={!ready}
            onClick={onConfirm}
            className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast hover:opacity-90 disabled:bg-surface-sunken disabled:text-ink-faint"
          >
            Confirm push
          </button>
        </div>
      </div>
    </div>
  )
}
