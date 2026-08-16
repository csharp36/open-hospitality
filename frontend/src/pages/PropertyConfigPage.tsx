import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Card, PageHeader, cellClass, controlClass, headCellClass, tableClass } from '../components/ui'
import {
  addOoo, getFiscalPeriods, getPropertyConfig, removeOoo, setFiscalCalendar, setInventory,
} from '../api/client'
import { OOO_REASONS, type FiscalConfig } from '../api/types'
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
        subtitle="Sellable-room inventory, out-of-order blocks, and the fiscal calendar this property runs on."
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
