// Mapping-coverage worklist page: USALI edition selector (state in the URL
// search params), then one card per PMS source with financial, segment, and
// statistics coverage. Data fetching lives here (TanStack Query keyed on the
// edition); the card components below stay presentational. Financial/segment
// gaps are errors (danger-red); unmapped statistics labels are informational
// only.

import { Fragment } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'

import { getCoverage } from '../api/client'
import type {
  FinancialCoverage,
  NeedsReviewEntry,
  SegmentCoverage,
  SourceCoverage,
  StatisticsCoverage,
} from '../api/types'
import {
  Badge,
  Card,
  cellClass,
  controlClass,
  headCellClass,
  PageHeader,
  sectionHeadClass,
  tableClass,
} from '../components/ui'
import { DEFAULT_EDITION, EDITIONS } from '../lib/editions'
import { errorMessage } from '../lib/errors'

// getRouteApi avoids the router.tsx <-> CoveragePage.tsx circular value import.
const routeApi = getRouteApi('/coverage')

export default function CoveragePage() {
  const search = routeApi.useSearch()
  const navigate = routeApi.useNavigate()
  const edition = search.edition ?? DEFAULT_EDITION

  const coverageQuery = useQuery({
    queryKey: ['coverage', edition],
    queryFn: () => getCoverage(edition),
  })

  return (
    <div className="space-y-4">
      <PageHeader
        title="Mapping Coverage"
        actions={
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">USALI edition</span>
            <select
              className={controlClass}
              value={edition}
              onChange={(e) =>
                void navigate({ search: { edition: Number(e.target.value) }, replace: true })
              }
            >
              {EDITIONS.map((ed) => (
                <option key={ed} value={ed}>
                  {ed}th edition
                </option>
              ))}
            </select>
          </label>
        }
      />

      {coverageQuery.isPending && <p className="text-sm text-ink-muted">Loading coverage…</p>}
      {coverageQuery.isError && (
        <p className="text-sm text-danger-red">
          Failed to load coverage: {errorMessage(coverageQuery.error)}
        </p>
      )}
      {coverageQuery.data !== undefined &&
        (coverageQuery.data.sources.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No staged data yet — upload a report to see coverage.
          </p>
        ) : (
          coverageQuery.data.sources.map((source) => (
            <SourceCard key={source.pms_source} source={source} />
          ))
        ))}
    </div>
  )
}

// --- Presentational cards ----------------------------------------------------

function SourceCard({ source }: { source: SourceCoverage }) {
  // role="region" keeps the landmark the tests locate via
  // getByRole('region', { name: 'Coverage: <source>' }) — Card spreads rest
  // props onto its div, so the aria-label passes through.
  return (
    <Card role="region" aria-label={`Coverage: ${source.pms_source}`}>
      <PageHeader title={source.pms_source} level={2} />
      <div className="space-y-5">
        <FinancialSection financial={source.financial} />
        <SegmentsSection segments={source.segments} />
        <StatisticsSection statistics={source.statistics} />
      </div>
    </Card>
  )
}

function FinancialSection({ financial }: { financial: FinancialCoverage }) {
  return (
    <section className="space-y-3">
      <h3 className={sectionHeadClass}>Financial</h3>
      <p className="flex flex-wrap items-center gap-2 text-sm text-ink-muted">
        <span>
          {financial.mapped_codes} of {financial.staged_codes} staged codes mapped ·{' '}
          {financial.dictionary_entries} dictionary entries
        </span>
        <Badge tone={financial.exception_count > 0 ? 'danger' : 'neutral'}>
          {financial.exception_count} exception{financial.exception_count === 1 ? '' : 's'}
        </Badge>
      </p>
      {financial.missing_codes.length > 0 && (
        <p className="text-sm font-medium text-danger-red">
          Missing codes: {financial.missing_codes.join(', ')}
        </p>
      )}
      <p className="text-sm text-ink-muted">
        GL mapping: {financial.gl_mapped}/{financial.dictionary_entries} entries carry a GL
        account
      </p>
      {financial.gl_unmapped_codes.length > 0 && (
        <p className="text-sm font-medium text-danger-red">
          GL unmapped codes: {financial.gl_unmapped_codes.join(', ')}
        </p>
      )}
      <div className="flex flex-wrap gap-x-12 gap-y-3">
        <CountGrid title="By confidence" counts={financial.by_confidence} />
        <CountGrid title="By review status" counts={financial.by_review_status} />
      </div>
      <div>
        <h4 className="mb-1 text-xs font-medium text-ink-muted">
          Needs review ({financial.needs_review.length})
        </h4>
        <NeedsReviewTable
          entries={financial.needs_review}
          label="Financial needs-review worklist"
        />
      </div>
    </section>
  )
}

function SegmentsSection({ segments }: { segments: SegmentCoverage }) {
  return (
    <section className="space-y-3">
      <h3 className={sectionHeadClass}>Segments</h3>
      <p className="text-sm text-ink-muted">
        {segments.mapped_codes} of {segments.staged_codes} staged codes mapped ·{' '}
        {segments.dictionary_entries} dictionary entries
      </p>
      {segments.unmapped_codes.length > 0 && (
        <p className="text-sm font-medium text-danger-red">
          Unmapped codes: {segments.unmapped_codes.join(', ')}
        </p>
      )}
      <div>
        <h4 className="mb-1 text-xs font-medium text-ink-muted">
          Needs review ({segments.needs_review.length})
        </h4>
        <NeedsReviewTable entries={segments.needs_review} label="Segment needs-review worklist" />
      </div>
    </section>
  )
}

function StatisticsSection({ statistics }: { statistics: StatisticsCoverage }) {
  return (
    <section className="space-y-2">
      <h3 className={sectionHeadClass}>Statistics</h3>
      <p className="text-sm text-ink-muted">
        {statistics.mapped_labels} of {statistics.staged_labels} staged labels mapped ·{' '}
        {statistics.dictionary_entries} dictionary entries
      </p>
      {statistics.unmapped_labels.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer select-none text-ink-muted">
            {statistics.unmapped_labels.length} unmapped label
            {statistics.unmapped_labels.length === 1 ? '' : 's'} (informational)
          </summary>
          <ul className="mt-1 list-disc pl-5 text-ink-muted">
            {statistics.unmapped_labels.map((label) => (
              <li key={label}>{label}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

function CountGrid({ title, counts }: { title: string; counts: Record<string, number> }) {
  // Object.entries gives well-typed [string, number] pairs (no
  // noUncheckedIndexedAccess friction). The backend emits counts in semantic
  // order (HIGH/MEDIUM/LOW, reviewed/needs-review/unreviewed — see
  // reporting.py's _ordered_counts) and JSON key order survives res.json(),
  // so render in insertion order; do not sort.
  const entries = Object.entries(counts)
  return (
    <div>
      <h4 className="text-xs font-medium text-ink-muted">{title}</h4>
      {entries.length === 0 ? (
        <p className="text-sm text-ink-muted">none</p>
      ) : (
        <dl className="mt-1 grid w-fit grid-cols-[auto_auto] gap-x-6 gap-y-0.5 text-sm">
          {entries.map(([key, count]) => (
            <Fragment key={key}>
              <dt className="text-ink-muted">{key}</dt>
              <dd className="text-right font-medium tabular-nums text-ink">{count}</dd>
            </Fragment>
          ))}
        </dl>
      )}
    </div>
  )
}

function NeedsReviewTable({ entries, label }: { entries: NeedsReviewEntry[]; label: string }) {
  if (entries.length === 0) {
    return <p className="text-sm text-ink-muted">No entries need review.</p>
  }
  return (
    <table className={tableClass} aria-label={label}>
      <thead>
        <tr className="border-b border-line">
          <th className={headCellClass}>Code</th>
          <th className={headCellClass}>Line item</th>
          <th className={headCellClass}>Notes</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          // Composite key: the segment YAML gives no code-uniqueness guarantee.
          <tr key={`${entry.code}:${entry.line_item}`} className="border-b border-line">
            <td className={`${cellClass} tabular-nums`}>{entry.code}</td>
            <td className={cellClass}>{entry.line_item}</td>
            <td className={`${cellClass} text-ink-muted`}>{entry.notes ?? ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
