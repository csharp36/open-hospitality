// Summary Operating Statement page: property/date picker (state in the URL
// search params), statement view, and drill-through panel. All data fetching
// lives here (TanStack Query keyed on the search params); PickerBar,
// Statement, and DrillPanel stay presentational.

import { useEffect, useState } from 'react'
import { skipToken, useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'

import {
  getLineTransactions,
  getSos,
  type LineTransactionsParams,
  type SosParams,
} from '../api/client'
import type { SosLine } from '../api/types'
import DrillPanel from '../components/DrillPanel'
import PickerBar, { type DateMode } from '../components/PickerBar'
import Statement from '../components/Statement'
import { Card, PageHeader } from '../components/ui'
import { errorMessage } from '../lib/errors'
import type { SosSearch } from '../router'
import { useGlobalProperty } from '../lib/propertyContext'

// getRouteApi avoids the router.tsx <-> SosPage.tsx circular value import.
const routeApi = getRouteApi('/sos')

export default function SosPage() {
  const search = routeApi.useSearch()
  const navigate = routeApi.useNavigate()
  const [mode, setMode] = useState<DateMode>(
    search.from !== undefined || search.to !== undefined ? 'range' : 'single',
  )
  const [drillLine, setDrillLine] = useState<SosLine | null>(null)
  // Property comes from the GLOBAL top-bar selector — picked once, app-wide.
  const { property, selected } = useGlobalProperty()

  // Changing property invalidates any open drill window.
  useEffect(() => setDrillLine(null), [property])

  function updateSearch(patch: Partial<SosSearch>) {
    setDrillLine(null) // any picker change invalidates the open drill window
    void navigate({ search: (prev) => ({ ...prev, ...patch }), replace: true })
  }

  const sosParams: SosParams | null =
    property !== undefined && search.date !== undefined
      ? { property, date: search.date }
      : property !== undefined && search.from !== undefined && search.to !== undefined
        ? { property, from: search.from, to: search.to }
        : null

  const sosQuery = useQuery({
    queryKey: ['sos', property, search.date, search.from, search.to],
    // skipToken disables the query until the picker state is complete.
    queryFn: sosParams === null ? skipToken : () => getSos(sosParams),
  })

  // Drill window: single-date mode drills from=to=date.
  const dateWindow =
    search.date !== undefined
      ? { from: search.date, to: search.date }
      : search.from !== undefined && search.to !== undefined
        ? { from: search.from, to: search.to }
        : null

  const drillParams: LineTransactionsParams | null =
    drillLine !== null && property !== undefined && dateWindow !== null
      ? {
          property,
          major: drillLine.major,
          sub: drillLine.sub_category,
          line_item: drillLine.line_item,
          from: dateWindow.from,
          to: dateWindow.to,
        }
      : null

  const txnsQuery = useQuery({
    queryKey: [
      'line-transactions',
      property,
      drillLine?.major,
      drillLine?.sub_category,
      drillLine?.line_item,
      dateWindow?.from,
      dateWindow?.to,
    ],
    // skipToken disables the query until a line is drilled with a full window.
    queryFn: drillParams === null ? skipToken : () => getLineTransactions(drillParams),
  })

  return (
    <div className="space-y-4">
      <PageHeader title="Summary Operating Statement" />

      <Card>
        <PickerBar
          selected={selected}
          mode={mode}
          date={search.date}
          from={search.from}
          to={search.to}
          onModeChange={(next) => {
            setMode(next)
            updateSearch({ date: undefined, from: undefined, to: undefined })
          }}
          onDateChange={(date) =>
            updateSearch({ date: date === '' ? undefined : date, from: undefined, to: undefined })
          }
          onFromChange={(from) =>
            updateSearch({ from: from === '' ? undefined : from, date: undefined })
          }
          onToChange={(to) => updateSearch({ to: to === '' ? undefined : to, date: undefined })}
        />
      </Card>

      {sosParams === null ? (
        <p className="text-sm text-ink-muted">
          Pick {mode === 'single' ? 'a business date' : 'a date range'} to view the statement.
        </p>
      ) : (
        <>
          {sosQuery.isPending && <p className="text-sm text-ink-muted">Loading statement…</p>}
          {sosQuery.isError && (
            <p className="text-sm text-danger-red">
              Failed to load statement: {errorMessage(sosQuery.error)}
            </p>
          )}
          {sosQuery.data !== undefined && (
            <Card className="p-6 sm:p-10">
              <Statement report={sosQuery.data} onLineClick={setDrillLine} />
            </Card>
          )}
        </>
      )}

      {drillLine !== null && dateWindow !== null && (
        <DrillPanel
          line={drillLine}
          from={dateWindow.from}
          to={dateWindow.to}
          txns={txnsQuery.data}
          loading={txnsQuery.isPending}
          error={txnsQuery.isError ? errorMessage(txnsQuery.error) : null}
          onClose={() => setDrillLine(null)}
        />
      )}
    </div>
  )
}
