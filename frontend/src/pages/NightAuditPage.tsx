import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Card, PageHeader } from '../components/ui'
import { getNightAudit, postNightAuditAdjust, postNightAuditRoll, postNightAuditSegments, postNightAuditUpload } from '../api/client'
import type { NightAuditCheck, NightAuditPackSection, NightAuditSegments, NightAuditSlot } from '../api/types'
import { useGlobalProperty } from '../lib/propertyContext'
import { errorMessage } from '../lib/errors'

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

  const roll = useMutation({
    mutationFn: () => postNightAuditRoll(property!),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['night-audit', property] })
      void qc.invalidateQueries({ queryKey: ['properties'] })
    },
  })

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
            <PackDropCard
              propertyId={property}
              packLabel={audit.data.pack_label ?? 'Night-audit pack'}
              slots={audit.data.slots}
              onUploaded={() => {
                void qc.invalidateQueries({ queryKey: ['night-audit', property] })
                void qc.invalidateQueries({ queryKey: ['properties'] })
              }}
            />
          ) : (
            <Card role="region" aria-label="required reports">
              <h2 className="mb-3 text-sm font-semibold text-ink">
                Required reports — {audit.data.pms_source}
              </h2>
              <ol className="flex flex-col gap-2">
                {audit.data.slots.map((slot, i) => (
                  <SlotRow
                    key={slot.report_type}
                    index={i + 1}
                    slot={slot}
                    propertyId={property}
                    onUploaded={() => {
                      void qc.invalidateQueries({ queryKey: ['night-audit', property] })
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
              {audit.data.verification.map((c) => (
                <CheckRow
                  key={c.name}
                  check={c}
                  propertyId={property}
                  onAdjusted={() => {
                    void qc.invalidateQueries({ queryKey: ['night-audit', property] })
                  }}
                />
              ))}
            </ul>
          </Card>

          {audit.data.segments !== null && (
            <SegmentsCard
              propertyId={property}
              segments={audit.data.segments}
              onSaved={() => {
                void qc.invalidateQueries({ queryKey: ['night-audit', property] })
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
                title={audit.data.can_roll ? undefined : 'All reports must land, checks must pass, and the window must be open'}
              >
                Roll to next business date →
              </button>
            </div>
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
  onUploaded: () => void
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
  onAdjusted: () => void
}) {
  const [open, setOpen] = useState(false)
  const [corrected, setCorrected] = useState('')
  const [reason, setReason] = useState('')
  const adjust = useMutation({
    mutationFn: () =>
      postNightAuditAdjust(propertyId, { corrected_amount: corrected, reason }),
    onSuccess: () => {
      setOpen(false)
      setReason('')
      onAdjusted()
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
              disabled={adjust.isPending}
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
  onUploaded: () => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [sections, setSections] = useState<NightAuditPackSection[] | null>(null)
  const upload = useMutation({
    mutationFn: (file: File) => postNightAuditUpload(propertyId, file),
    onSuccess: (result) => {
      setSections(result.sections)
      onUploaded()
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
// mismatch every code's cells become editable — the auditor makes the numbers
// EXACTLY equal, saves, and the checks re-run live. No reason is asked; every
// changed cell is still recorded server-side (old → new, who, when).

function SegmentsCard({ propertyId, segments, onSaved }: {
  propertyId: string
  segments: NightAuditSegments
  onSaved: () => void
}) {
  const [edited, setEdited] = useState<Record<string, { rooms: string; room_revenue: string }>>({})
  const failing = segments.status === 'fail'

  const rows = segments.rows.map((r) => ({
    code: r.code,
    description: r.description,
    rooms: edited[r.code]?.rooms ?? r.rooms,
    room_revenue: edited[r.code]?.room_revenue ?? r.room_revenue,
  }))
  const liveRooms = rows.reduce((a, r) => a + (Number(r.rooms) || 0), 0)
  const liveRevenue = rows.reduce((a, r) => a + (Number(r.room_revenue) || 0), 0)
  const roomsRef = segments.rooms_ref === null ? null : Number(segments.rooms_ref)
  const revenueRef = segments.revenue_ref === null ? null : Number(segments.revenue_ref)
  const roomsTie = roomsRef !== null && Math.abs(liveRooms - roomsRef) < 0.005
  const revenueTie = revenueRef !== null && Math.abs(liveRevenue - revenueRef) < 0.005

  const save = useMutation({
    mutationFn: () => postNightAuditSegments(propertyId, rows),
    onSuccess: () => {
      setEdited({})
      onSaved()
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
                  {(['rooms', 'room_revenue'] as const).map((field) => (
                    <td key={field} className="py-1.5 text-right font-mono">
                      {failing ? (
                        <input
                          className="w-28 rounded-control border border-line px-2 py-1 text-right font-mono text-sm"
                          value={r[field]}
                          aria-label={`${r.code} ${field}`}
                          inputMode={field === 'rooms' ? 'numeric' : 'decimal'}
                          onChange={(e) => setCell(r.code, field, e.target.value)}
                        />
                      ) : (
                        r[field]
                      )}
                    </td>
                  ))}
                </tr>
              ))}
              <tr className="border-b border-line font-medium">
                <td className="py-1.5 text-ink">Sum</td>
                <td className={`py-1.5 text-right font-mono ${roomsTie ? 'text-ok-green' : 'text-danger-red'}`}>
                  {liveRooms.toFixed(0)}
                </td>
                <td className={`py-1.5 text-right font-mono ${revenueTie ? 'text-ok-green' : 'text-danger-red'}`}>
                  {liveRevenue.toFixed(2)}
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
                  {roomsRef === null ? '—' : (liveRooms - roomsRef).toFixed(0)}
                </td>
                <td className={`py-1 text-right font-mono ${revenueTie ? 'text-ok-green' : 'text-danger-red'}`}>
                  {revenueRef === null ? '—' : (liveRevenue - revenueRef).toFixed(2)}
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
            disabled={save.isPending || !roomsTie || !revenueTie}
            onClick={() => save.mutate()}
            className="rounded-control bg-accent px-4 py-2 text-sm font-medium text-accent-contrast disabled:opacity-40"
            title={roomsTie && revenueTie ? undefined : 'Edit the cells until both sums exactly match the references'}
          >
            Save matched values
          </button>
          {(!roomsTie || !revenueTie) && (
            <span className="text-xs text-ink-muted">
              Save unlocks when both sums exactly match the references.
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
