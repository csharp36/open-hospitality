// Presentational property + month picker shared by the Reports and QBO pages.
// Selection state lives in each route's search params; the page owns
// navigation and passes values down. Month options derive from the selected
// property's available date window — /api/properties gives first/last DATES,
// monthRange spans the months between them.

import type { PropertyInfo } from '../api/types'
import { monthRange } from '../lib/months'
import { controlClass } from './ui'

type MonthPickerBarProps = {
  /** The globally selected property (top bar) — drives the month options. */
  selected: PropertyInfo | undefined
  month: string | undefined
  onMonthChange: (month: string) => void
}

const fieldLabelClass = 'flex flex-col gap-1 text-sm'
const fieldNameClass = 'text-xs font-medium text-ink-muted'

export default function MonthPickerBar({
  selected,
  month,
  onMonthChange,
}: MonthPickerBarProps) {
  const months = selected !== undefined ? monthRange(selected.first_date, selected.last_date) : []

  return (
    // Card chrome (surface, border, radius, shadow) comes from the Card the
    // page wraps this bar in — the bar itself only lays out its fields (same
    // contract as PickerBar).
    <div className="flex flex-wrap items-end gap-4">
      <label className={fieldLabelClass}>
        <span className={fieldNameClass}>Month</span>
        <select
          className={controlClass}
          value={month ?? ''}
          disabled={selected === undefined}
          onChange={(e) => onMonthChange(e.target.value)}
        >
          <option value="" disabled>
            Select month…
          </option>
          {months.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </label>

      {selected !== undefined && (
        <p className="pb-1.5 text-xs text-ink-muted">
          Data available {selected.first_date} – {selected.last_date}
        </p>
      )}
    </div>
  )
}
