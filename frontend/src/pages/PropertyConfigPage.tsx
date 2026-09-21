import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Card, PageHeader, cellClass, controlClass, headCellClass, tableClass } from '../components/ui'
import {
  ApiError, addOoo, createIntakeAddress, getFiscalPeriods, getIntakeAddress, getIntakeEvents,
  getPropertyConfig, removeOoo, rotateIntakeAddress, setFiscalCalendar, setIntakeSenderDomains,
  setInventory,
} from '../api/client'
import {
  OOO_REASONS, type FiscalConfig, type IntakeEvent, type IntakeOutcome,
} from '../api/types'
import { useGlobalProperty } from '../lib/propertyContext'
import { errorMessage } from '../lib/errors'

/**
 * Property configuration: the authoritative, effective-dated record of a
 * property's sellable-room inventory, out-of-order rooms, and fiscal calendar
 * (issue #8) — the hard dependency of #9 (core performance statistics).
 *
 * Property comes from the GLOBAL top-bar selector, same as EmployeesPage —
 * pick it once, every page follows. Writes go through org_admin |
 * property_gm (confined to assigned properties) on the server; reads gate on
 * readability. Every write is audited server-side.
 */
export default function PropertyConfigPage() {
  const qc = useQueryClient()
  const { property, selected } = useGlobalProperty()

  const config = useQuery({
    queryKey: ['property-config', property],
    queryFn: () => getPropertyConfig(property!),
    enabled: property !== undefined,
  })

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Property configuration"
        subtitle="Sellable-room inventory, out-of-order blocks, the fiscal calendar this property runs on, and the address its night audit can be emailed to."
      />

      {property === undefined && (
        <Card>
          <p className="text-sm text-ink-muted">No property selected yet.</p>
        </Card>
      )}

      {config.isError && (
        <Card>
          <p className="text-sm text-danger-red">Failed to load: {errorMessage(config.error)}</p>
        </Card>
      )}

      {property !== undefined && config.isPending && (
        <Card>
          <p className="text-sm text-ink-muted">Loading …</p>
        </Card>
      )}

      {property !== undefined && config.data && (
        <>
          <InventorySection
            propertyId={property}
            propertyLabel={selected?.property_id}
            inventory={config.data.inventory}
            onSaved={() => void qc.invalidateQueries({ queryKey: ['property-config', property] })}
          />
          <OutOfOrderSection
            propertyId={property}
            blocks={config.data.out_of_order}
            onChanged={() => void qc.invalidateQueries({ queryKey: ['property-config', property] })}
          />
          <FiscalCalendarSection
            propertyId={property}
            fiscalCalendar={config.data.fiscal_calendar}
            onSaved={() => void qc.invalidateQueries({ queryKey: ['property-config', property] })}
          />
        </>
      )}

      {property !== undefined && <IntakeSection propertyId={property} />}
    </div>
  )
}

// --- Room inventory -----------------------------------------------------------

function InventorySection({
  propertyId,
  propertyLabel,
  inventory,
  onSaved,
}: {
  propertyId: string
  propertyLabel: string | undefined
  inventory: { inventory_id: number; effective_date: string; total_rooms: number }[]
  onSaved: () => void
}) {
  const [effectiveDate, setEffectiveDate] = useState('')
  const [totalRooms, setTotalRooms] = useState('')

  // inventory arrives newest-first (server orders by effective_date desc). The
  // count IN FORCE today is the newest row whose effective_date is on or before
  // today — the same greatest-effective_date-<=-today rule the backend uses; a
  // future-dated row (e.g. a renovation) is NOT the current count.
  const todayIso = new Date().toLocaleDateString('en-CA') // YYYY-MM-DD, local
  const current = inventory.find((row) => row.effective_date <= todayIso)
  const earliest = inventory[inventory.length - 1]

  const add = useMutation({
    mutationFn: () =>
      setInventory(propertyId, { effective_date: effectiveDate, total_rooms: Number(totalRooms) }),
    onSuccess: () => {
      setEffectiveDate('')
      setTotalRooms('')
      onSaved()
    },
  })

  return (
    <Card role="region" aria-label="room inventory">
      <h2 className="mb-3 text-sm font-semibold text-ink">
        Room inventory{propertyLabel !== undefined ? ` — ${propertyLabel}` : ''}
      </h2>
      {current !== undefined ? (
        <p className="mb-3 text-sm text-ink">
          Current count: <span className="font-semibold">{current.total_rooms}</span> rooms,
          effective {current.effective_date}
        </p>
      ) : earliest !== undefined ? (
        <p className="mb-3 text-sm text-ink-muted">
          No count in force yet — first count of{' '}
          <span className="font-semibold">{earliest.total_rooms}</span> rooms takes effect{' '}
          {earliest.effective_date}.
        </p>
      ) : (
        <p className="mb-3 text-sm text-ink-muted">No room count on file yet — add one below.</p>
      )}

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          add.mutate()
        }}
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Effective date</span>
          <input
            type="date"
            className={controlClass}
            value={effectiveDate}
            aria-label="Effective date"
            onChange={(e) => setEffectiveDate(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Total rooms</span>
          <input
            type="number"
            min={1}
            className={controlClass}
            value={totalRooms}
            aria-label="Total rooms"
            onChange={(e) => setTotalRooms(e.target.value)}
            required
          />
        </label>
        <button
          type="submit"
          disabled={add.isPending}
          className="rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >
          Add a count
        </button>
      </form>
      {add.isError && (
        <p className="mt-2 text-sm text-danger-red">Save failed: {errorMessage(add.error)}</p>
      )}

      {inventory.length > 0 && (
        <ul className="mt-4 flex flex-col gap-1 text-sm text-ink-muted">
          {inventory.map((row) => (
            <li key={row.inventory_id}>
              {row.effective_date} — {row.total_rooms} rooms
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

// --- Out-of-order rooms ---------------------------------------------------------

function OutOfOrderSection({
  propertyId,
  blocks,
  onChanged,
}: {
  propertyId: string
  blocks: {
    ooo_id: number
    start_date: string
    end_date: string
    room_count: number
    reason_code: string
    note: string | null
  }[]
  onChanged: () => void
}) {
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [roomCount, setRoomCount] = useState('')
  const [reason, setReason] = useState<(typeof OOO_REASONS)[number]>(OOO_REASONS[0])
  const [note, setNote] = useState('')

  const add = useMutation({
    mutationFn: () =>
      addOoo(propertyId, {
        start_date: startDate,
        end_date: endDate,
        room_count: Number(roomCount),
        reason_code: reason,
        note: note.trim() === '' ? null : note.trim(),
      }),
    onSuccess: () => {
      setStartDate('')
      setEndDate('')
      setRoomCount('')
      setReason(OOO_REASONS[0])
      setNote('')
      onChanged()
    },
  })

  const remove = useMutation({
    mutationFn: (oooId: number) => removeOoo(propertyId, oooId),
    onSettled: onChanged,
  })

  return (
    <Card role="region" aria-label="out of order rooms">
      <h2 className="mb-3 text-sm font-semibold text-ink">Out-of-order rooms</h2>

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          add.mutate()
        }}
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Start date</span>
          <input
            type="date"
            className={controlClass}
            value={startDate}
            aria-label="Start date"
            onChange={(e) => setStartDate(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">End date</span>
          <input
            type="date"
            className={controlClass}
            value={endDate}
            aria-label="End date"
            onChange={(e) => setEndDate(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Room count</span>
          <input
            type="number"
            min={1}
            className={controlClass}
            value={roomCount}
            aria-label="Room count"
            onChange={(e) => setRoomCount(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Reason</span>
          <select
            className={controlClass}
            value={reason}
            aria-label="Reason"
            onChange={(e) => setReason(e.target.value as (typeof OOO_REASONS)[number])}
          >
            {OOO_REASONS.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Note</span>
          <input
            className={controlClass}
            value={note}
            aria-label="Note"
            onChange={(e) => setNote(e.target.value)}
            placeholder="optional"
          />
        </label>
        <button
          type="submit"
          disabled={add.isPending}
          className="rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50"
        >
          Add block
        </button>
      </form>
      {add.isError && (
        <p className="mt-2 text-sm text-danger-red">Add failed: {errorMessage(add.error)}</p>
      )}
      {remove.isError && (
        <p className="mt-2 text-sm text-danger-red">Remove failed: {errorMessage(remove.error)}</p>
      )}

      {blocks.length > 0 && (
        <table className={`${tableClass} mt-4`} aria-label="Out of order blocks">
          <thead>
            <tr className="border-b border-line">
              <th className={headCellClass}>Start</th>
              <th className={headCellClass}>End</th>
              <th className={headCellClass}>Rooms</th>
              <th className={headCellClass}>Reason</th>
              <th className={headCellClass}>Note</th>
              <th className={headCellClass}></th>
            </tr>
          </thead>
          <tbody>
            {blocks.map((b) => (
              <tr key={b.ooo_id} className="border-b border-line last:border-0">
                <td className={cellClass}>{b.start_date}</td>
                <td className={cellClass}>{b.end_date}</td>
                <td className={cellClass}>{b.room_count}</td>
                <td className={cellClass}>{b.reason_code}</td>
                <td className={cellClass}>{b.note ?? '—'}</td>
                <td className={cellClass}>
                  <button
                    type="button"
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(b.ooo_id)}
                    className="rounded-control border border-line px-2 py-1 text-xs text-danger-red hover:bg-surface-sunken disabled:opacity-50"
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}

// --- Fiscal calendar -----------------------------------------------------------

const FISCAL_MONTHS = Array.from({ length: 12 }, (_, i) => i + 1)
const WEEKDAYS: { value: number; label: string }[] = [
  { value: 0, label: 'Monday' },
  { value: 1, label: 'Tuesday' },
  { value: 2, label: 'Wednesday' },
  { value: 3, label: 'Thursday' },
  { value: 4, label: 'Friday' },
  { value: 5, label: 'Saturday' },
  { value: 6, label: 'Sunday' },
]

function FiscalCalendarSection({
  propertyId,
  fiscalCalendar,
  onSaved,
}: {
  propertyId: string
  fiscalCalendar: FiscalConfig | null
  onSaved: () => void
}) {
  const [calendarType, setCalendarType] = useState<'calendar_month' | '445'>('calendar_month')
  const [fyStartMonth, setFyStartMonth] = useState(1)
  const [weekStartWeekday, setWeekStartWeekday] = useState(6)
  const [initialized, setInitialized] = useState(false)
  const [previewYear, setPreviewYear] = useState(() => new Date().getFullYear())

  // Sync local form state from the loaded config exactly once — after that
  // the form is the user's draft, not a mirror of the server.
  useEffect(() => {
    if (initialized) return
    if (fiscalCalendar !== null) {
      setCalendarType(fiscalCalendar.calendar_type)
      setFyStartMonth(fiscalCalendar.fiscal_year_start_month)
      setWeekStartWeekday(fiscalCalendar.week_start_weekday ?? 6)
    }
    setInitialized(true)
  }, [fiscalCalendar, initialized])

  const save = useMutation({
    mutationFn: () =>
      setFiscalCalendar(propertyId, {
        calendar_type: calendarType,
        fiscal_year_start_month: fyStartMonth,
        week_start_weekday: calendarType === '445' ? weekStartWeekday : null,
      }),
    onSuccess: onSaved,
  })

  const periods = useQuery({
    queryKey: ['fiscal-periods', propertyId, previewYear],
    queryFn: () => getFiscalPeriods(propertyId, previewYear),
    enabled: fiscalCalendar !== null,
  })

  return (
    <Card role="region" aria-label="fiscal calendar">
      <h2 className="mb-3 text-sm font-semibold text-ink">Fiscal calendar</h2>

      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        <fieldset className="flex flex-wrap items-center gap-4">
          <legend className="mb-1 text-xs font-medium text-ink-muted">Calendar type</legend>
          <label className="flex items-center gap-1.5 text-sm text-ink">
            <input
              type="radio"
              name="calendar_type"
              value="calendar_month"
              aria-label="Calendar month"
              checked={calendarType === 'calendar_month'}
              onChange={() => setCalendarType('calendar_month')}
            />
            Calendar month
          </label>
          <label className="flex items-center gap-1.5 text-sm text-ink">
            <input
              type="radio"
              name="calendar_type"
              value="445"
              aria-label="4-4-5"
              checked={calendarType === '445'}
              onChange={() => setCalendarType('445')}
            />
            4-4-5
          </label>
        </fieldset>

        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">Fiscal year starts</span>
            <select
              className={controlClass}
              value={fyStartMonth}
              aria-label="Fiscal year start month"
              onChange={(e) => setFyStartMonth(Number(e.target.value))}
            >
              {FISCAL_MONTHS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </label>

          {calendarType === '445' && (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-xs font-medium text-ink-muted">Week start weekday</span>
              <select
                className={controlClass}
                value={weekStartWeekday}
                aria-label="Week start weekday"
                onChange={(e) => setWeekStartWeekday(Number(e.target.value))}
              >
                {WEEKDAYS.map((w) => (
                  <option key={w.value} value={w.value}>{w.label}</option>
                ))}
              </select>
            </label>
          )}

          <button
            type="submit"
            disabled={save.isPending}
            className="rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50"
          >
            Save fiscal calendar
          </button>
        </div>
      </form>
      {save.isError && (
        <p className="mt-2 text-sm text-danger-red">Save failed: {errorMessage(save.error)}</p>
      )}

      {fiscalCalendar !== null && (
        <div className="mt-4 border-t border-line pt-4">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">Preview fiscal year</span>
            <input
              type="number"
              className={`${controlClass} w-28`}
              value={previewYear}
              aria-label="Preview fiscal year"
              onChange={(e) => setPreviewYear(Number(e.target.value))}
            />
          </label>
          {periods.data && (
            <ul className="mt-2 flex flex-col gap-1 text-xs text-ink-muted">
              {periods.data.periods.map((p) => (
                <li key={p.key}>
                  {p.key}: {p.start} – {p.end}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  )
}

// --- Night-audit email (OH-23) --------------------------------------------

const INTAKE_EVENT_LIMIT = 20
const primaryButtonClass =
  'rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50'
const quietButtonClass =
  'rounded-control border border-line px-2 py-1 text-xs text-ink hover:bg-surface-sunken disabled:opacity-50'
const dangerButtonClass =
  'rounded-control border border-line px-2 py-1 text-xs text-danger-red hover:bg-surface-sunken disabled:opacity-50'

/** The raw outcome as the API spells it, underscores opened out. Taking
 * `IntakeOutcome` rather than a string is what keeps the set closed here: an
 * outcome the API grows that api/types.ts has not been told about stops at
 * this signature instead of being rendered as whatever text arrived. */
function outcomeLabel(outcome: IntakeOutcome): string {
  return outcome.replace(/_/g, ' ')
}

function parseSenderDomains(text: string): string[] {
  return text.split(',').map((entry) => entry.trim()).filter((entry) => entry !== '')
}

/**
 * What to show beside the sender-domain field when a save is refused. A 422
 * from the request BODY carries `detail` as a string naming the offending
 * entry, and that string is the whole message; a `detail` of any other shape
 * (FastAPI spells a bad query parameter as a list) is reported by status
 * instead of stringified at the operator.
 */
function senderDomainRefusal(error: unknown): string {
  if (error instanceof ApiError) {
    return typeof error.detailBody === 'string'
      ? error.detailBody
      : `Save failed (HTTP ${error.status}).`
  }
  return errorMessage(error)
}

/**
 * The property's night-audit email address, and the log of what arrived at it.
 *
 * The address is minted ON DEMAND rather than at property creation, so a
 * property whose night audit is uploaded by hand carries no unused intake
 * capability. The address itself is served fully formed — the intake domain is
 * a server setting and is never assembled here.
 */
function IntakeSection({ propertyId }: { propertyId: string }) {
  const qc = useQueryClient()

  const address = useQuery({
    queryKey: ['intake-address', propertyId],
    queryFn: () => getIntakeAddress(propertyId),
  })
  const events = useQuery({
    queryKey: ['intake-events', propertyId],
    queryFn: () => getIntakeEvents(propertyId, INTAKE_EVENT_LIMIT),
  })

  const refetchAddress = () =>
    void qc.invalidateQueries({ queryKey: ['intake-address', propertyId] })

  const create = useMutation({
    mutationFn: () => createIntakeAddress(propertyId),
    // Re-read on failure too: a 409 means this property already has an address
    // (another session minted it), and that address is the answer to show.
    onSettled: refetchAddress,
  })

  const live = address.data

  return (
    <Card role="region" aria-label="night-audit email">
      <h2 className="mb-3 text-sm font-semibold text-ink">Night-audit email</h2>

      {address.isPending && <p className="text-sm text-ink-muted">Loading …</p>}
      {address.isError && (
        <p className="text-sm text-danger-red">Failed to load: {errorMessage(address.error)}</p>
      )}

      {live !== undefined && live.address === null && (
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm text-ink-muted">
            This property has no night-audit email address yet. Create one to have the PMS
            mail its night audit straight in.
          </p>
          <button
            type="button"
            disabled={create.isPending}
            onClick={() => create.mutate()}
            className={primaryButtonClass}
          >
            Create address
          </button>
          {/* Inside the no-address branch on purpose: a 409 means the address
              now exists, and the refetch that follows replaces this whole
              branch — a banner outside it would outlive the failure it
              describes and sit beside the address that answered it. */}
          {create.isError && (
            <p className="text-sm text-danger-red">Create failed: {errorMessage(create.error)}</p>
          )}
        </div>
      )}

      {live !== undefined && live.address !== null && (
        // Keyed by the address so a rotation remounts the panel: its draft
        // sender-domain text belongs to the address it was typed against.
        <IntakeAddressPanel
          key={live.address}
          propertyId={propertyId}
          address={live.address}
          createdAt={live.created_at}
          senderDomains={live.sender_domains}
          onAddressChanged={refetchAddress}
        />
      )}

      <div className="mt-4 border-t border-line pt-4">
        {events.isError && (
          <p className="text-sm text-danger-red">
            Failed to load the message log: {errorMessage(events.error)}
          </p>
        )}
        {events.data && <IntakeEventTable events={events.data.events} />}
      </div>
    </Card>
  )
}

function IntakeAddressPanel({
  propertyId,
  address,
  createdAt,
  senderDomains,
  onAddressChanged,
}: {
  propertyId: string
  address: string
  createdAt: string | null
  senderDomains: string[] | null
  onAddressChanged: () => void
}) {
  const [confirmingRotate, setConfirmingRotate] = useState(false)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  // Both initialize from the props once: IntakeSection keys this panel by the
  // address, so a rotation remounts it rather than leaving a stale draft.
  // `saved` then tracks what the server last confirmed, which is what a blur
  // compares against — the `senderDomains` prop lags a save by one refetch.
  const [domainsDraft, setDomainsDraft] = useState(senderDomains?.join(', ') ?? '')
  const [saved, setSaved] = useState<string[]>(senderDomains ?? [])

  // Clear the "Copied" flash on a timer, and cancel that timer on unmount —
  // otherwise the flash outlives the panel a rotation replaces.
  useEffect(() => {
    if (copyState !== 'copied') return
    const timer = setTimeout(() => setCopyState('idle'), 2000)
    return () => clearTimeout(timer)
  }, [copyState])

  // A 404 from either write means only "no active address" — it was revoked
  // elsewhere — so re-read rather than leave a form up that writes nowhere.
  // A property the caller may not touch answers 403, not 404; the client's
  // night-audit-intake block in api/client.ts names the tests for both.
  const resyncIfRevoked = (error: unknown) => {
    if (error instanceof ApiError && error.status === 404) onAddressChanged()
  }

  const rotate = useMutation({
    mutationFn: () => rotateIntakeAddress(propertyId),
    onSuccess: () => {
      setConfirmingRotate(false)
      onAddressChanged()
    },
    onError: resyncIfRevoked,
  })

  const saveDomains = useMutation({
    mutationFn: (entries: string[]) => setIntakeSenderDomains(propertyId, entries),
    onSuccess: (updated) => {
      const stored = updated.sender_domains ?? []
      setSaved(stored)
      setDomainsDraft(stored.join(', '))
      onAddressChanged()
    },
    onError: resyncIfRevoked,
  })

  const copyAddress = () => {
    setConfirmingRotate(false)
    const clipboard: Clipboard | undefined = navigator.clipboard
    if (clipboard === undefined) {
      setCopyState('failed')
      return
    }
    void Promise.resolve(clipboard.writeText(address)).then(
      () => setCopyState('copied'),
      () => setCopyState('failed'),
    )
  }

  const submitDomains = () => {
    const entries = parseSenderDomains(domainsDraft)
    // Entries are stored lowercased (see setIntakeSenderDomains in api/client.ts
    // for the test that holds it), so the comparison that decides "nothing
    // changed" is case-insensitive. Skipping an unchanged save matters: each
    // one writes an audit row, and a blur fires whether or not anything moved.
    const unchanged =
      entries.length === saved.length &&
      entries.every((entry, i) => entry.toLowerCase() === saved[i])
    if (unchanged || saveDomains.isPending) return
    saveDomains.mutate(entries)
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <label htmlFor="intake-address" className="text-xs font-medium text-ink-muted">
          Night-audit email address
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <input
            id="intake-address"
            readOnly
            value={address}
            className={`${controlClass} w-80 max-w-full`}
          />
          <button type="button" onClick={copyAddress} className={quietButtonClass}>
            Copy
          </button>
          {copyState === 'copied' && <span className="text-xs text-ink-muted">Copied</span>}
          {copyState === 'failed' && (
            <span className="text-xs text-danger-red">Could not copy — select the address instead.</span>
          )}
        </div>
        {createdAt !== null && (
          <p className="text-xs text-ink-muted">
            In use since {new Date(createdAt).toLocaleDateString()}.
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {confirmingRotate ? (
          <>
            <button
              type="button"
              disabled={rotate.isPending}
              onClick={() => rotate.mutate()}
              className={dangerButtonClass}
            >
              Confirm rotation
            </button>
            <button
              type="button"
              onClick={() => setConfirmingRotate(false)}
              className={quietButtonClass}
            >
              Cancel
            </button>
            <span className="text-xs text-ink-muted">
              The current address stops accepting mail, and the new one starts with no
              sender allowlist.
            </span>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingRotate(true)}
            className={quietButtonClass}
          >
            Rotate address
          </button>
        )}
      </div>
      {rotate.isError && (
        <p className="text-sm text-danger-red">Rotate failed: {errorMessage(rotate.error)}</p>
      )}

      <div className="flex flex-col gap-1">
        <label htmlFor="intake-sender-domains" className="text-xs font-medium text-ink-muted">
          Allowed sender domains
        </label>
        <input
          id="intake-sender-domains"
          value={domainsDraft}
          className={`${controlClass} w-80 max-w-full`}
          placeholder="pms.example.com, reports.example.com"
          onFocus={() => setConfirmingRotate(false)}
          onChange={(e) => setDomainsDraft(e.target.value)}
          onBlur={submitDomains}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submitDomains()
            }
          }}
        />
        <p className="text-xs text-ink-muted">
          Comma-separated. Leave it empty to accept any authenticated sender. Saved when you
          leave the field or press Enter.
        </p>
        {saveDomains.isError && (
          <p className="text-sm text-danger-red">{senderDomainRefusal(saveDomains.error)}</p>
        )}
      </div>
    </div>
  )
}

function IntakeEventTable({ events }: { events: IntakeEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-ink-muted">No messages yet.</p>
  }
  return (
    // The caption both heads the log on screen and names the table for a
    // screen reader — one visible string doing both jobs, so there is no
    // aria-label here to drift away from what is drawn.
    <table className={tableClass}>
      <caption className="mb-2 text-left text-xs font-semibold text-ink-muted">
        Last {INTAKE_EVENT_LIMIT} messages
      </caption>
      <thead>
        <tr className="border-b border-line">
          <th className={headCellClass}>Received</th>
          <th className={headCellClass}>From</th>
          <th className={headCellClass}>Subject</th>
          <th className={headCellClass}>Outcome</th>
          <th className={headCellClass}>Attachments</th>
        </tr>
      </thead>
      <tbody>
        {events.map((event) => (
          <tr key={event.event_id} className="border-b border-line last:border-0">
            <td className={cellClass}>{new Date(event.received_at).toLocaleString()}</td>
            <td className={cellClass}>{event.envelope_from}</td>
            {/* The subject is how an operator recognizes a message; a PMS that
                sends none still has to be tellable from one that did. */}
            <td className={cellClass}>
              {event.subject ?? <span className="text-ink-muted">(no subject)</span>}
            </td>
            <td className={cellClass}>{outcomeLabel(event.outcome)}</td>
            <td className={cellClass}>
              {event.attachments.length === 0 ? (
                '—'
              ) : (
                <ul className="flex flex-col gap-0.5">
                  {event.attachments.map((attachment, i) => (
                    <li key={`${attachment.sha256}-${i}`}>
                      {attachment.name} — {outcomeLabel(attachment.outcome)}
                    </li>
                  ))}
                </ul>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
