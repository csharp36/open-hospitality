import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Card, PageHeader } from '../components/ui'
import { getNightAudit, postNightAuditAdjust, postNightAuditRoll, postNightAuditSegments, postNightAuditUpload } from '../api/client'
import type { NightAuditCheck, NightAuditPackSection, NightAuditSegments, NightAuditSlot, NightAuditState } from '../api/types'
import { useGlobalProperty } from '../lib/propertyContext'
import { errorMessage } from '../lib/errors'
import { eqFixed, isFixed, subFixed, sumFixed } from '../lib/decimal'
import { fmtMoney, fmtStat } from '../lib/format'

/**
 * Night audit: the property's EXPLICIT current business date. The auditor
 * uploads the night's required reports (per-PMS set — becomes onboarding
 * config later, alongside XML/XLS formats), the ledger checks verify the close
 * against the last one, and the ROLL advances the date — enabled only inside
 * the property-local 00:00–05:00 window. Dashboards default to data through
 * `closed_through` (the last rolled day); today's data is still arriving here.
 */
export default function NightAuditPage() {
  const qc = useQueryClient()
  const { property } = useGlobalProperty()

  const audit = useQuery({
    queryKey: ['night-audit', property],
    queryFn: () => getNightAudit(property!),
    enabled: property !== undefined,
  })

  // Every night-audit mutation returns the full state payload; writing it
  // straight into the cache is the PeriodDetailCard close-mutation idiom —
  // the page re-renders from the freshest picture at once, with no refetch
  // round trip showing the pre-mutation numbers in the meantime. The cache is
  // keyed by the payload's OWN property_id, not the selector's current value,
  // so a mutation that resolves after a property switch can only ever update
  // the property it was posted against.
  const adopt = (state: NightAuditState) => {
    qc.setQueryData(['night-audit', state.property_id], state)
  }

  const roll = useMutation({
    mutationFn: () => postNightAuditRoll(property!),
    onSuccess: (state) => {
      adopt(state)
      void qc.invalidateQueries({ queryKey: ['properties'] })
    },
  })

  // Why the roll is refused, named in visible words beside the button — a
  // title-only reason never reaches keyboard or screen-reader users (the
  // segments Save reason span is the pattern).
  const rollBlockers: string[] = []
  if (audit.data !== undefined && !audit.data.can_roll) {
    const missing = audit.data.slots.filter((s) => !s.landed)
    if (missing.length > 0)
      rollBlockers.push(`reports still missing: ${missing.map((s) => s.label).join(', ')}`)
    if (
      audit.data.verification.some((c) => c.status === 'fail') ||
      audit.data.segments?.status === 'fail'
    )
      rollBlockers.push('checks above are failing')
    if (!audit.data.window.open) rollBlockers.push('the roll window is closed')
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Night audit"
        subtitle="Upload the night's reports, verify the close ties to the last one, then roll the property to the next business date."
      />

      {property === undefined && (
        <Card><p className="text-sm text-ink-muted">No property selected yet.</p></Card>
      )}
      {audit.isError && (
        <Card><p className="text-sm text-danger-red">Failed to load: {errorMessage(audit.error)}</p></Card>
      )}
      {property !== undefined && audit.isPending && (
        <Card><p className="text-sm text-ink-muted">Loading …</p></Card>
      )}

      {property !== undefined && audit.data && (
        <>
          <Card role="region" aria-label="business date">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <div className="text-xs font-medium uppercase tracking-wide text-ink-muted">
                  Current business date
                </div>
                <div className="text-2xl font-semibold text-ink">{audit.data.business_date}</div>
              </div>
              <p className="text-sm text-ink-muted">
                Closed through <b className="text-ink">{audit.data.closed_through}</b>
                {' '}· dashboards report through the last closed day
              </p>
            </div>
          </Card>

          {audit.data.upload_mode === 'pack' ? (
            /* key={property}: the card's sections/upload state belongs to one
               property and must die on a switch — GlPage.tsx's scope-keyed
               discipline. */
            <PackDropCard
              key={property}
              propertyId={property}
              packLabel={audit.data.pack_label ?? 'Night-audit pack'}
              slots={audit.data.slots}
              onUploaded={(state) => {
                adopt(state)
                void qc.invalidateQueries({ queryKey: ['properties'] })
              }}
            />
          ) : (
            <Card role="region" aria-label="required reports">
              <h2 className="mb-3 text-sm font-semibold text-ink">
                Required reports — {audit.data.pms_source}
              </h2>
              <ol className="flex flex-col gap-2">
                {/* Property in the key: report types recur across properties
                    (any two on the same PMS share the set), and a row's
                    upload-mutation state (a pending or failed label) must not
                    survive into another — GlPage.tsx's scope-keyed
                    discipline. */}
                {audit.data.slots.map((slot, i) => (
                  <SlotRow
                    key={`${property}:${slot.report_type}`}
                    index={i + 1}
                    slot={slot}
                    propertyId={property}
                    onUploaded={(state) => {
                      adopt(state)
                      void qc.invalidateQueries({ queryKey: ['properties'] })
                    }}
                  />
                ))}
              </ol>
            </Card>
          )}

          <Card role="region" aria-label="verification">
            <h2 className="mb-3 text-sm font-semibold text-ink">Verification — balances vs the last close</h2>
            <ul className="flex flex-col gap-1.5">
              {/* Property in the key: check names are the same constants for
                  every property, and an open adjust form (amount + reason
                  typed for one property) must not survive into another —
                  GlPage.tsx's scope-keyed discipline. */}
              {audit.data.verification.map((c) => (
                <CheckRow
                  key={`${property}:${c.name}`}
                  check={c}
                  propertyId={property}
                  onAdjusted={(state) => {
                    adopt(state)
                    // Prefix-wide drop: this page does not track which
                    // performance windows are cached, and none of them may
                    // keep rendering pre-correction numbers.
                    void qc.invalidateQueries({ queryKey: ['performance'] })
                  }}
                />
              ))}
            </ul>
          </Card>

          {audit.data.segments !== null && (
            /* key={property}: the `edited` cell map is keyed by market code —
               the same codes recur across properties — and must die on a
               switch, GlPage.tsx's scope-keyed discipline. */
            <SegmentsCard
              key={property}
              propertyId={property}
              segments={audit.data.segments}
              onSaved={(state) => {
                adopt(state)
                // The saved rows land in UsaliSegmentFact, the table
                // usali/performance.py reads — prefix-wide drop, since this
                // page does not track which windows are cached.
                void qc.invalidateQueries({ queryKey: ['performance'] })
              }}
            />
          )}

          <Card role="region" aria-label="roll date">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="text-sm text-ink-muted">
                Roll window: <b className="text-ink">{audit.data.window.hours}</b>{' '}
                {audit.data.window.timezone} — property time is now{' '}
                <b className="text-ink">{audit.data.window.local_time}</b>
                {audit.data.window.open ? ' (open)' : ' (closed)'}
              </div>
              <button
                type="button"
                disabled={!audit.data.can_roll || roll.isPending}
                onClick={() => roll.mutate()}
                className="rounded-control bg-accent px-4 py-2 text-sm font-medium text-accent-contrast disabled:opacity-40"
              >
                Roll to next business date →
              </button>
            </div>
            {!audit.data.can_roll && (
              <p className="mt-2 text-sm text-ink-muted">
                Roll unavailable —{' '}
                {rollBlockers.length > 0 ? rollBlockers.join('; ') : 'the server refuses the roll'}.
              </p>
            )}
            {roll.isError && (
              <p className="mt-2 text-sm text-danger-red">{errorMessage(roll.error)}</p>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

function SlotRow({ index, slot, propertyId, onUploaded }: {
  index: number
  slot: NightAuditSlot
  propertyId: string
  onUploaded: (state: NightAuditState) => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const upload = useMutation({
    mutationFn: (file: File) => postNightAuditUpload(propertyId, file),
    onSuccess: onUploaded,
  })
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 rounded-control border border-line px-3 py-2">
      <span className="text-sm text-ink">
        <span className="mr-2 font-mono text-xs text-ink-muted">{index}.</span>
        {slot.label}
        {slot.landed && <span className="ml-2 text-xs font-medium text-ok-green">✓ uploaded</span>}
      </span>
      <span className="flex items-center gap-2">
        {upload.isError && (
          <span className="text-xs text-danger-red">{errorMessage(upload.error)}</span>
        )}
        <input
          ref={fileRef}
          type="file"
          accept="application/pdf"
          className="hidden"
          aria-label={`Upload ${slot.label}`}
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) upload.mutate(f)
            e.target.value = ''
          }}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={upload.isPending}
          className="rounded-control border border-line px-3 py-1.5 text-sm text-ink disabled:opacity-50"
        >
          {upload.isPending ? 'Uploading…' : slot.landed ? 'Re-upload' : 'Upload PDF'}
        </button>
      </span>
    </li>
  )
}

function CheckRow({ check, propertyId, onAdjusted }: {
  check: NightAuditCheck
  propertyId: string
  onAdjusted: (state: NightAuditState) => void
}) {
  const [open, setOpen] = useState(false)
  const [corrected, setCorrected] = useState('')
  const [reason, setReason] = useState('')
  const adjust = useMutation({
    mutationFn: () =>
      postNightAuditAdjust(propertyId, { corrected_amount: corrected, reason }),
    onSuccess: (state) => {
      setOpen(false)
      setReason('')
      onAdjusted(state)
    },
  })
  const tone =
    check.status === 'pass' ? 'text-ok-green'
    : check.status === 'fail' ? 'text-danger-red'
    : 'text-ink-muted'
  const mark = check.status === 'pass' ? '✓' : check.status === 'fail' ? '✗' : '–'
  return (
    <li className="text-sm">
      <div className="flex flex-wrap items-center gap-x-2">
        <span className={`font-semibold ${tone}`}>{mark}</span>
        <span className="text-ink">{check.name.replace(/_/g, ' ')}</span>
        <span className="text-ink-muted">{check.detail}</span>
        {check.delta !== null && check.status !== 'skipped' && (
          <span className="font-mono text-xs text-ink-muted">Δ {check.delta}</span>
        )}
        {check.status === 'fail' && check.adjust !== null && !open && (
          <button
            type="button"
            className="rounded-control border border-line px-2 py-0.5 text-xs text-ink"
            onClick={() => {
              setCorrected(check.adjust!.suggested)
              setOpen(true)
            }}
          >
            Adjust…
          </button>
        )}
      </div>

      {open && check.adjust !== null && (
        <form
          className="mt-2 flex flex-wrap items-end gap-3 rounded-control border border-line p-3"
          onSubmit={(e) => {
            e.preventDefault()
            adjust.mutate()
          }}
        >
          <p className="w-full text-xs text-ink-muted">
            Correct the {check.adjust.business_date} AR close — the PMS export can't fix a
            prior-night figure. Stored: <span className="font-mono">{check.adjust.stored}</span>;
            the suggested value zeroes the residual. Every correction is recorded (old → new,
            reason, who).
          </p>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">Corrected close</span>
            <input
              className="rounded-control border border-line px-2 py-1.5 font-mono text-sm"
              value={corrected}
              aria-label="Corrected close"
              onChange={(e) => setCorrected(e.target.value)}
              required
            />
          </label>
          <label className="flex min-w-64 flex-1 flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">Reason (required)</span>
            <input
              className="rounded-control border border-line px-2 py-1.5 text-sm"
              value={reason}
              aria-label="Adjustment reason"
              placeholder="e.g. late city-ledger transfer posted after the 07-06 close"
              onChange={(e) => setReason(e.target.value)}
              minLength={3}
              required
            />
          </label>
          <div className="flex items-center gap-2">
            <button
              type="submit"
              // Strip-then-floor, the same rule night_audit_api.py's
              // AdjustBody._stripped_reason enforces: three spaces satisfy
              // minLength but are not a reason (the GlPage reopen gate is
              // the pattern).
              disabled={adjust.isPending || reason.trim().length < 3}
              className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-50"
            >
              {adjust.isPending ? 'Saving…' : 'Save correction'}
            </button>
            <button
              type="button"
              className="text-sm text-ink-muted underline"
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
          </div>
          {adjust.isError && (
            <p className="w-full text-sm text-danger-red">{errorMessage(adjust.error)}</p>
          )}
        </form>
      )}
    </li>
  )
}


// --- Pack mode (SkyTouch): ONE drop fills every slot the pack contains -------

function PackDropCard({ propertyId, packLabel, slots, onUploaded }: {
  propertyId: string
  packLabel: string
  slots: NightAuditSlot[]
  onUploaded: (state: NightAuditState) => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [sections, setSections] = useState<NightAuditPackSection[] | null>(null)
  const upload = useMutation({
    mutationFn: (file: File) => postNightAuditUpload(propertyId, file),
    onSuccess: (result) => {
      setSections(result.sections)
      onUploaded(result)
    },
  })
  const allLanded = slots.length > 0 && slots.every((s) => s.landed)
  return (
    <Card role="region" aria-label="audit pack">
      <h2 className="mb-1 text-sm font-semibold text-ink">Night-audit pack</h2>
      <p className="mb-3 text-sm text-ink-muted">
        {packLabel} — one upload; the pack is split report-by-report on the server.
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <input
          ref={fileRef}
          type="file"
          accept="application/pdf"
          className="hidden"
          aria-label="Upload audit pack"
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) upload.mutate(f)
            e.target.value = ''
          }}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={upload.isPending}
          className="rounded-control bg-accent px-4 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >
          {upload.isPending ? 'Splitting & ingesting…' : allLanded ? 'Re-upload pack' : 'Upload audit pack (PDF)'}
        </button>
        {upload.isError && (
          <span className="text-sm text-danger-red">{errorMessage(upload.error)}</span>
        )}
      </div>

      <h3 className="mb-2 mt-4 text-xs font-medium uppercase tracking-wide text-ink-muted">
        Fills these reports
      </h3>
      <ul className="flex flex-col gap-1.5">
        {slots.map((slot) => (
          <li key={slot.report_type} className="text-sm text-ink">
            <span className={`mr-2 font-semibold ${slot.landed ? 'text-ok-green' : 'text-ink-muted'}`}>
              {slot.landed ? '✓' : '○'}
            </span>
            {slot.label}
            {slot.landed && <span className="ml-2 text-xs text-ok-green">uploaded</span>}
          </li>
        ))}
      </ul>

      {sections !== null && (
        <div className="mt-4 rounded-control border border-line p-3">
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-muted">
            What the pack contained
          </h3>
          <ul className="flex flex-col gap-1">
            {sections.map((sec, i) => (
              <li key={`${sec.title}-${i}`} className="text-sm">
                {sec.skipped ? (
                  <>
                    <span className="mr-2 text-ink-muted">–</span>
                    <span className="text-ink-muted">{sec.title} — skipped (not part of the audit)</span>
                  </>
                ) : (
                  <>
                    <span className="mr-2 font-semibold text-ok-green">✓</span>
                    <span className="text-ink">{sec.title}</span>
                    <span className="ml-2 font-mono text-xs text-ink-muted">
                      {sec.staged} rows staged · {sec.mapped} mapped
                    </span>
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  )
}


// --- Rooms & revenue by market code -------------------------------------------
// The tabular reconciliation: Σ per-code rooms must hit the Manager Flash
// occupied total, Σ per-code revenue the Trial Balance Rooms line. On a
// mismatch every code's cells become editable — the auditor edits until both
// sums tie within the report's own tolerance, saves, and the checks re-run
// live. No reason is asked; every changed cell is still recorded server-side
// (old → new, who, when).

// Rooms are counts: a whole number, nothing else. (Revenue cells take any
// fixed-point decimal — isFixed in lib/decimal.ts is that gate.)
const WHOLE_RE = /^-?\d+$/

/**
 * |Δ| ≤ 0.01 — the same tie the segment check applies server-side
 * (night_audit.py's _TOL is where that tolerance is set). Decided on the
 * exact decimal strings: subFixed's sign carries the comparison, so no float
 * ever touches the amounts.
 */
function withinTol(delta: string): boolean {
  const abs = delta.startsWith('-') ? delta.slice(1) : delta
  const rem = subFixed(abs, '0.01')
  return rem.startsWith('-') || eqFixed(rem, '0')
}

function SegmentsCard({ propertyId, segments, onSaved }: {
  propertyId: string
  segments: NightAuditSegments
  onSaved: (state: NightAuditState) => void
}) {
  const [edited, setEdited] = useState<Record<string, { rooms: string; room_revenue: string }>>({})
  const failing = segments.status === 'fail'

  const rows = segments.rows.map((r) => {
    const rooms = edited[r.code]?.rooms ?? r.rooms
    const room_revenue = edited[r.code]?.room_revenue ?? r.room_revenue
    return {
      code: r.code,
      description: r.description,
      rooms,
      room_revenue,
      // An unparseable cell must never count as 0 toward the tie — it locks
      // Save and is flagged in place instead. A raw "1,234" posted to the
      // server would only come back as an opaque 422.
      roomsInvalid: !WHOLE_RE.test(rooms.trim()),
      revenueInvalid: !isFixed(room_revenue),
    }
  })
  const roomsValid = rows.every((r) => !r.roomsInvalid)
  const revenueValid = rows.every((r) => !r.revenueInvalid)
  const anyInvalid = !roomsValid || !revenueValid

  // Exact string arithmetic (lib/decimal.ts) end-to-end; sums exist only
  // while every cell in the column parses.
  const liveRooms = roomsValid ? sumFixed(rows.map((r) => r.rooms)) : null
  const liveRevenue = revenueValid ? sumFixed(rows.map((r) => r.room_revenue)) : null
  const roomsDelta =
    liveRooms !== null && segments.rooms_ref !== null ? subFixed(liveRooms, segments.rooms_ref) : null
  const revenueDelta =
    liveRevenue !== null && segments.revenue_ref !== null
      ? subFixed(liveRevenue, segments.revenue_ref)
      : null
  const roomsTie = roomsDelta !== null && withinTol(roomsDelta)
  const revenueTie = revenueDelta !== null && withinTol(revenueDelta)

  const save = useMutation({
    mutationFn: () =>
      postNightAuditSegments(
        propertyId,
        rows.map(({ code, rooms, room_revenue }) => ({ code, rooms, room_revenue })),
      ),
    onSuccess: (state) => {
      setEdited({})
      onSaved(state)
    },
  })

  function setCell(code: string, field: 'rooms' | 'room_revenue', value: string) {
    setEdited((prev) => {
      const row = rows.find((r) => r.code === code)!
      return { ...prev, [code]: { rooms: row.rooms, room_revenue: row.room_revenue, [field]: value } }
    })
  }

  const tone = segments.status === 'pass' ? 'text-ok-green'
    : segments.status === 'fail' ? 'text-danger-red' : 'text-ink-muted'
  const mark = segments.status === 'pass' ? '✓' : segments.status === 'fail' ? '✗' : '–'

  return (
    <Card role="region" aria-label="market code reconciliation">
      <h2 className="mb-1 text-sm font-semibold text-ink">
        <span className={`mr-2 ${tone}`}>{mark}</span>
        Rooms &amp; revenue by market code
      </h2>
      <p className="mb-3 text-sm text-ink-muted">{segments.detail}</p>

      {segments.status === 'skipped' ? null : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-muted">
                <th className="py-1.5">Market code</th>
                <th className="py-1.5 text-right">Rooms</th>
                <th className="py-1.5 text-right">Room revenue</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.code} className="border-b border-line">
                  <td className="py-1.5 text-ink">
                    <span className="font-mono">{r.code}</span>
                    <span className="ml-2 text-xs text-ink-muted">{r.description}</span>
                  </td>
                  {(['rooms', 'room_revenue'] as const).map((field) => {
                    const invalid = field === 'rooms' ? r.roomsInvalid : r.revenueInvalid
                    return (
                      <td key={field} className="py-1.5 text-right font-mono">
                        {failing ? (
                          <input
                            className={`w-28 rounded-control border px-2 py-1 text-right font-mono text-sm ${
                              invalid ? 'border-danger-red' : 'border-line'
                            }`}
                            value={r[field]}
                            aria-label={`${r.code} ${field}`}
                            aria-invalid={invalid}
                            inputMode={field === 'rooms' ? 'numeric' : 'decimal'}
                            onChange={(e) => setCell(r.code, field, e.target.value)}
                          />
                        ) : (
                          r[field]
                        )}
                      </td>
                    )
                  })}
                </tr>
              ))}
              <tr className="border-b border-line font-medium">
                <td className="py-1.5 text-ink">Sum</td>
                <td className={`py-1.5 text-right font-mono ${roomsTie ? 'text-ok-green' : 'text-danger-red'}`}>
                  {liveRooms === null ? '—' : fmtStat(liveRooms)}
                </td>
                <td className={`py-1.5 text-right font-mono ${revenueTie ? 'text-ok-green' : 'text-danger-red'}`}>
                  {liveRevenue === null ? '—' : fmtMoney(liveRevenue)}
                </td>
              </tr>
              {segments.report_total_rooms !== null && (
                <tr className="text-xs text-ink-muted">
                  <td className="py-1">Report's own TOTAL row</td>
                  <td className="py-1 text-right font-mono">{segments.report_total_rooms}</td>
                  <td className="py-1 text-right font-mono">{segments.report_total_revenue}</td>
                </tr>
              )}
              <tr className="text-ink-muted">
                <td className="py-1.5">Reference (Manager Flash · Trial Balance)</td>
                <td className="py-1.5 text-right font-mono">{segments.rooms_ref}</td>
                <td className="py-1.5 text-right font-mono">{segments.revenue_ref}</td>
              </tr>
              <tr className="text-xs text-ink-muted">
                <td className="py-1">Δ remaining</td>
                <td className={`py-1 text-right font-mono ${roomsTie ? 'text-ok-green' : 'text-danger-red'}`}>
                  {roomsDelta === null ? '—' : fmtStat(roomsDelta)}
                </td>
                <td className={`py-1 text-right font-mono ${revenueTie ? 'text-ok-green' : 'text-danger-red'}`}>
                  {revenueDelta === null ? '—' : fmtMoney(revenueDelta)}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {failing && (
        <div className="mt-3 flex items-center gap-3">
          <button
            type="button"
            disabled={save.isPending || anyInvalid || !roomsTie || !revenueTie}
            onClick={() => save.mutate()}
            className="rounded-control bg-accent px-4 py-2 text-sm font-medium text-accent-contrast disabled:opacity-40"
          >
            Save matched values
          </button>
          {(anyInvalid || !roomsTie || !revenueTie) && (
            <span className="text-xs text-ink-muted">
              {anyInvalid
                ? 'Fix the marked cells first — rooms take a whole number, revenue a plain decimal (no commas).'
                : 'Save unlocks when both sums match the references within the report’s tolerance (±0.01).'}
            </span>
          )}
          {save.isError && (
            <span className="text-sm text-danger-red">{errorMessage(save.error)}</span>
          )}
        </div>
      )}
    </Card>
  )
}
