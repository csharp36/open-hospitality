// Presentational property + date-window picker. Selection state lives in the
// SOS route's search params; the page owns navigation and passes values down.

import type { PropertyInfo } from '../api/types'

import { controlClass } from './ui'

export type DateMode = 'single' | 'range'

type PickerBarProps = {
  /** The globally selected property (top bar) — drives the date bounds. */
  selected: PropertyInfo | undefined
  mode: DateMode
  date: string | undefined
  from: string | undefined
  to: string | undefined
  onModeChange: (mode: DateMode) => void
  onDateChange: (date: string) => void
  onFromChange: (from: string) => void
  onToChange: (to: string) => void
}

const fieldLabelClass = 'flex flex-col gap-1 text-sm'
const fieldNameClass = 'text-xs font-medium text-ink-muted'

export default function PickerBar({
  selected,
  mode,
  date,
  from,
  to,
  onModeChange,
  onDateChange,
  onFromChange,
  onToChange,
}: PickerBarProps) {
  return (
    // Card chrome (surface, border, radius, shadow) comes from the Card the
    // page wraps this bar in — the bar itself only lays out its fields.
    <div className="flex flex-wrap items-end gap-4">
      <fieldset className="flex items-center gap-3 pb-1 text-sm">
        <legend className="sr-only">Date mode</legend>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="date-mode"
            className="accent-accent"
            checked={mode === 'single'}
            onChange={() => onModeChange('single')}
          />
          Single date
        </label>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="date-mode"
            className="accent-accent"
            checked={mode === 'range'}
            onChange={() => onModeChange('range')}
          />
          Range
        </label>
      </fieldset>

      {mode === 'single' ? (
        <label className={fieldLabelClass}>
          <span className={fieldNameClass}>Date</span>
          <input
            type="date"
            className={controlClass}
            value={date ?? ''}
            min={selected?.first_date}
            max={selected?.last_date}
            disabled={selected === undefined}
            onChange={(e) => onDateChange(e.target.value)}
          />
        </label>
      ) : (
        <>
          <label className={fieldLabelClass}>
            <span className={fieldNameClass}>From</span>
            <input
              type="date"
              className={controlClass}
              value={from ?? ''}
              min={selected?.first_date}
              max={to ?? selected?.last_date}
              disabled={selected === undefined}
              onChange={(e) => onFromChange(e.target.value)}
            />
          </label>
          <label className={fieldLabelClass}>
            <span className={fieldNameClass}>To</span>
            <input
              type="date"
              className={controlClass}
              value={to ?? ''}
              min={from ?? selected?.first_date}
              max={selected?.last_date}
              disabled={selected === undefined}
              onChange={(e) => onToChange(e.target.value)}
            />
          </label>
        </>
      )}

      {selected !== undefined && (
        <p className="pb-1.5 text-xs text-ink-muted">
          Data available {selected.first_date} – {selected.last_date}
        </p>
      )}
    </div>
  )
}
