// Presentational Summary Operating Statement. Section order and labels echo
// src/usali/render.py's render_sos_text: operated departments, misc income,
// TOTAL OPERATING REVENUE, rooms segment split, taxes, settlements, other
// (nonempty sections only), statistics. Every financial line is a button that
// hands its SosLine to the drill-through handler.
//
// Layout is a full-width financial document: generous row heights, wide
// gutters, section headings on the sidebar's dot+hairline recipe, and one
// statement-defining moment (the TOTAL OPERATING REVENUE band).

import type { SosLine, SosReport } from '../api/types'
import { sumFixed } from '../lib/decimal'
import { fmtMoney, fmtStat, pct } from '../lib/format'
import { Badge } from './ui'

type StatementProps = {
  report: SosReport
  onLineClick: (line: SosLine) => void
}

const lineButtonClass =
  'text-left text-accent hover:underline focus-visible:underline cursor-pointer'

// Roomier local table recipe (the shared ui.tsx one is tuned for dense pages).
const tableCls = 'w-full text-[15px]'
const headCls =
  'py-2.5 pl-6 text-left text-[11px] font-semibold uppercase tracking-wide text-ink-faint'
const amountHeadCls =
  'py-2.5 pr-4 text-right text-[11px] font-semibold uppercase tracking-wide text-ink-faint whitespace-nowrap'
const amountCls = 'py-3 pr-4 text-right tabular-nums whitespace-nowrap'

function SectionHeading({ children }: { children: string }) {
  return (
    <h2 className="mb-3 mt-12 flex items-center gap-2.5 text-[11px] font-bold uppercase tracking-[0.14em] text-ink-muted">
      <span aria-hidden="true" className="inline-block size-1.5 rounded-full bg-accent/70" />
      {children}
      <span aria-hidden="true" className="h-px flex-1 bg-line" />
    </h2>
  )
}

// Labor headings carry an "estimate" badge: est_cost (meal premiums unpriced,
// superseded by Pillar C) and the FTE basis are both estimates, and the design
// requires that be visible wherever labor surfaces.
function EstimateHeading({ children }: { children: string }) {
  return (
    <div className="mb-3 mt-12 flex items-center gap-2.5">
      <span aria-hidden="true" className="inline-block size-1.5 rounded-full bg-accent/70" />
      <h2 className="text-[11px] font-bold uppercase tracking-[0.14em] text-ink-muted">
        {children}
      </h2>
      <Badge tone="warn">estimate</Badge>
      <span aria-hidden="true" className="h-px flex-1 bg-line" />
    </div>
  )
}

function LineRow({
  line,
  onLineClick,
  indent = false,
}: {
  line: SosLine
  onLineClick: (line: SosLine) => void
  indent?: boolean
}) {
  return (
    <tr className="border-b border-line last:border-0 hover:bg-surface-sunken">
      <td className={`py-3 ${indent ? 'pl-10' : 'pl-6'}`}>
        <button type="button" className={lineButtonClass} onClick={() => onLineClick(line)}>
          {line.line_item}
        </button>
      </td>
      <td className={amountCls}>{fmtMoney(line.total)}</td>
    </tr>
  )
}

function TotalRow({ label, total, indent = false }: { label: string; total: string; indent?: boolean }) {
  return (
    <tr className="border-t border-line-strong bg-surface-sunken/60 font-semibold">
      <td className={`py-3 ${indent ? 'pl-6' : 'pl-4'}`}>{label}</td>
      <td className={amountCls}>{fmtMoney(total)}</td>
    </tr>
  )
}

function LineSection({
  title,
  lines,
  totalLabel,
  total,
  onLineClick,
}: {
  title: string
  lines: SosLine[]
  totalLabel: string
  total: string
  onLineClick: (line: SosLine) => void
}) {
  return (
    <section>
      <SectionHeading>{title}</SectionHeading>
      <table className={tableCls}>
        <tbody>
          {lines.map((line) => (
            <LineRow
              key={`${line.major}|${line.sub_category}|${line.line_item}`}
              line={line}
              onLineClick={onLineClick}
            />
          ))}
          <TotalRow label={totalLabel} total={total} />
        </tbody>
      </table>
    </section>
  )
}

/** Signed money for variance cells: "+64.00" / "-12.00" — direction matters. */
function fmtVariance(s: string): string {
  const formatted = fmtMoney(s)
  return formatted.startsWith('-') ? formatted : `+${formatted}`
}

// C3 suppression: a single-employee department (on either side) hides
// est/actual/burden/variance — hours still carry. Same em-dash + inline note
// recipe as the B3 est_cost cell; the note appears once per row (Est cell).
function SuppressedCell({ note = false }: { note?: boolean }) {
  return (
    <span className="text-ink-muted">
      —
      {note && (
        <>
          {' '}
          <span className="text-xs font-normal">hidden (single employee)</span>
        </>
      )}
    </span>
  )
}

function periodHeading(report: SosReport): string {
  if (report.business_date !== null) return `Business date ${report.business_date}`
  return `Period ${report.date_from ?? '?'} – ${report.date_to ?? '?'}`
}

function MetaChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5 rounded-full border border-line bg-surface px-3 py-1 text-xs">
      <span className="font-semibold uppercase tracking-wide text-ink-faint">{label}</span>
      <span className="font-semibold text-ink">{value}</span>
    </span>
  )
}

export default function Statement({ report, onLineClick }: StatementProps) {
  const statistics = report.statistics
  const hasPrior = statistics.some(
    (row) => row.day_prior !== null || row.mtd_prior !== null || row.ytd_prior !== null,
  )
  const segmentRevenueTotal = sumFixed(report.rooms_segments.map((s) => s.room_revenue))
  // Labor is unioned in outside the revenue reconciliation; show it only when a
  // timecard has been promoted (lines present, or any non-zero total).
  const hasLabor =
    report.payroll_expense.length > 0 ||
    Number(report.payroll_expense_total) !== 0 ||
    Number(report.labor_hours_total) !== 0

  return (
    <div>
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-line pb-5">
        <h2 className="text-lg font-bold tracking-tight text-ink">SUMMARY OPERATING STATEMENT</h2>
        <div className="flex flex-wrap gap-2">
          <MetaChip label="Property" value={report.property_id} />
          <MetaChip label="Source" value={report.pms_source} />
          <MetaChip
            label={report.business_date !== null ? 'Date' : 'Period'}
            value={
              report.business_date !== null
                ? report.business_date
                : `${report.date_from ?? '?'} – ${report.date_to ?? '?'}`
            }
          />
        </div>
        <span className="sr-only">
          Property: {report.property_id} · PMS source: {report.pms_source} ·{' '}
          {periodHeading(report)}
        </span>
      </header>

      <section>
        <SectionHeading>OPERATED DEPARTMENTS</SectionHeading>
        {report.operated_departments.map((dept) => (
          <table key={dept.sub_category} className={tableCls}>
            <tbody>
              <tr>
                <td colSpan={2} className="pb-1 pl-4 pt-5 text-[15px] font-semibold text-ink">
                  {dept.sub_category}
                </td>
              </tr>
              {dept.lines.map((line) => (
                <LineRow
                  key={`${line.major}|${line.sub_category}|${line.line_item}`}
                  line={line}
                  onLineClick={onLineClick}
                  indent
                />
              ))}
              <TotalRow label={`Total ${dept.sub_category}`} total={dept.total} indent />
            </tbody>
          </table>
        ))}
      </section>

      <LineSection
        title="MISCELLANEOUS INCOME"
        lines={report.misc_income}
        totalLabel="Total Miscellaneous Income"
        total={report.misc_income_total}
        onLineClick={onLineClick}
      />

      <div className="mt-12 flex items-center justify-between rounded-xl bg-ink px-6 py-4 text-surface-raised shadow-card">
        <span className="text-base font-bold tracking-wide">TOTAL OPERATING REVENUE</span>
        <span className="text-xl font-bold tabular-nums">
          {fmtMoney(report.total_operating_revenue)}
        </span>
      </div>

      {report.rooms_segments.length > 0 && (
        <section>
          <SectionHeading>ROOMS SEGMENT SPLIT</SectionHeading>
          <table className={tableCls}>
            <thead>
              <tr className="border-b border-line-strong">
                <th className={headCls}>Segment</th>
                <th className={amountHeadCls}>Rooms</th>
                <th className={amountHeadCls}>Revenue</th>
                <th className={amountHeadCls}>% Revenue</th>
              </tr>
            </thead>
            <tbody>
              {report.rooms_segments.map((seg) => (
                <tr key={seg.segment} className="border-b border-line last:border-0 hover:bg-surface-sunken">
                  <td className="py-3 pl-6">{seg.segment}</td>
                  <td className={amountCls}>{fmtStat(seg.rooms)}</td>
                  <td className={amountCls}>{fmtMoney(seg.room_revenue)}</td>
                  <td className={amountCls}>{pct(seg.room_revenue, segmentRevenueTotal)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {report.taxes.length > 0 && (
        <LineSection
          title="TAXES COLLECTED (PASS-THROUGH)"
          lines={report.taxes}
          totalLabel="Total Taxes Collected"
          total={report.taxes_total}
          onLineClick={onLineClick}
        />
      )}
      {report.settlements.length > 0 && (
        <LineSection
          title="SETTLEMENTS"
          lines={report.settlements}
          totalLabel="Total Settlements"
          total={report.settlements_total}
          onLineClick={onLineClick}
        />
      )}
      {report.other.length > 0 && (
        <LineSection
          title="OTHER (UNSCHEDULED)"
          lines={report.other}
          totalLabel="Total Other"
          total={report.other_total}
          onLineClick={onLineClick}
        />
      )}

      {statistics.length > 0 && (
        <section>
          <SectionHeading>STATISTICS</SectionHeading>
          <div className="overflow-x-auto">
            <table className={tableCls}>
              <thead>
                <tr className="border-b border-line-strong">
                  <th className={headCls}>Metric</th>
                  <th className={amountHeadCls}>DAY</th>
                  <th className={amountHeadCls}>MTD</th>
                  <th className={amountHeadCls}>YTD</th>
                  {hasPrior && (
                    <>
                      <th className={amountHeadCls}>DAY PY</th>
                      <th className={amountHeadCls}>MTD PY</th>
                      <th className={amountHeadCls}>YTD PY</th>
                    </>
                  )}
                </tr>
              </thead>
              <tbody>
                {statistics.map((row) => (
                  <tr key={row.metric_code} className="border-b border-line last:border-0 hover:bg-surface-sunken">
                    <td className="py-3 pl-6">{row.metric_code}</td>
                    <td className={amountCls}>{fmtStat(row.day)}</td>
                    <td className={amountCls}>{fmtStat(row.mtd)}</td>
                    <td className={amountCls}>{fmtStat(row.ytd)}</td>
                    {hasPrior && (
                      <>
                        <td className={amountCls}>{fmtStat(row.day_prior)}</td>
                        <td className={amountCls}>{fmtStat(row.mtd_prior)}</td>
                        <td className={amountCls}>{fmtStat(row.ytd_prior)}</td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {hasLabor && (
        <section>
          <EstimateHeading>Schedule 14 — Payroll Related Expenses</EstimateHeading>
          <table className={tableCls}>
            <thead>
              <tr className="border-b border-line-strong">
                <th className={headCls}>Department</th>
                <th className={amountHeadCls}>Hours</th>
                <th className={amountHeadCls}>OT Hours</th>
                <th className={amountHeadCls}>Est Cost</th>
              </tr>
            </thead>
            <tbody>
              {report.payroll_expense.map((line) => (
                <tr key={line.department} className="border-b border-line last:border-0 hover:bg-surface-sunken">
                  <td className="py-3 pl-6">{line.department}</td>
                  <td className={amountCls}>{fmtStat(line.hours)}</td>
                  <td className={amountCls}>{fmtStat(line.ot_hours)}</td>
                  <td className={amountCls}>
                    {line.est_cost === null ? (
                      <span className="text-ink-muted">
                        —{' '}
                        <span className="text-xs font-normal">hidden (single employee)</span>
                      </span>
                    ) : (
                      fmtMoney(line.est_cost)
                    )}
                  </td>
                </tr>
              ))}
              <tr className="border-t border-line-strong bg-surface-sunken/60 font-semibold">
                <td className="py-3 pl-4">Total Payroll Related Expenses</td>
                <td className={amountCls}>{fmtStat(report.labor_hours_total)}</td>
                <td className={amountCls}>{fmtStat(report.labor_ot_hours_total)}</td>
                <td className={amountCls}>{fmtMoney(report.payroll_expense_total)}</td>
              </tr>
            </tbody>
          </table>
          {report.labor_suppressed_departments > 0 && (
            <p className="mt-2 pl-6 text-xs text-ink-muted">
              Cost hidden for {report.labor_suppressed_departments} single-employee
              department(s)
            </p>
          )}
          {report.labor_unpriced_hours !== '0' && (
            <p className="mt-2 pl-6 text-xs text-ink-muted">
              Excludes {fmtStat(report.labor_unpriced_hours)} unpriced hours (no rate on file)
            </p>
          )}
        </section>
      )}

      {hasLabor && (
        <section>
          <EstimateHeading>Schedule 15 — Payroll / FTE</EstimateHeading>
          <table className={tableCls}>
            <tbody>
              <tr className="border-b border-line">
                <td className="py-3 pl-6">Total Hours</td>
                <td className={amountCls}>{fmtStat(report.labor_hours_total)}</td>
              </tr>
              <tr className="border-b border-line last:border-0">
                <td className="py-3 pl-6">Overtime Hours</td>
                <td className={amountCls}>{fmtStat(report.labor_ot_hours_total)}</td>
              </tr>
              {report.labor_fte !== null && (
                <tr className="border-b border-line last:border-0">
                  <td className="py-3 pl-6">FTE (estimate)</td>
                  <td className={amountCls}>{fmtStat(report.labor_fte)}</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {/* Pillar C3: estimate vs provider-actual for the processed pay periods
          intersecting the window. Full-period semantics — the block will not
          visually tie to the window-clipped estimate above; the period labels
          are the honest explanation. Absent when no processed run touches. */}
      {report.labor_variance !== null && (
        <section>
          <EstimateHeading>Schedule 14 — Payroll: actual vs estimate</EstimateHeading>
          <p className="mb-2 pl-6 text-xs text-ink-muted">
            Pay periods: {report.labor_variance.periods.join(', ')}
          </p>
          <div className="overflow-x-auto">
            <table className={tableCls}>
              <thead>
                <tr className="border-b border-line-strong">
                  <th className={headCls}>Department</th>
                  <th className={amountHeadCls}>Est</th>
                  <th className={amountHeadCls}>Actual</th>
                  <th className={amountHeadCls}>Variance</th>
                  <th className={amountHeadCls}>Employer burden</th>
                  <th className={amountHeadCls}>Hours</th>
                </tr>
              </thead>
              <tbody>
                {report.labor_variance.lines.map((line) => (
                  <tr key={line.department} className="border-b border-line last:border-0 hover:bg-surface-sunken">
                    <td className="py-3 pl-6">{line.department}</td>
                    <td className={amountCls}>
                      {line.est_cost === null ? <SuppressedCell note /> : fmtMoney(line.est_cost)}
                    </td>
                    <td className={amountCls}>
                      {line.actual_gross === null ? (
                        <SuppressedCell />
                      ) : (
                        fmtMoney(line.actual_gross)
                      )}
                    </td>
                    <td className={amountCls}>
                      {line.variance === null ? (
                        <SuppressedCell />
                      ) : (
                        <span className="inline-flex items-center gap-1.5">
                          {line.alert && <Badge tone="warn">alert</Badge>}
                          {fmtVariance(line.variance)}
                        </span>
                      )}
                    </td>
                    <td className={amountCls}>
                      {line.employer_burden === null ? (
                        <SuppressedCell />
                      ) : (
                        fmtMoney(line.employer_burden)
                      )}
                    </td>
                    <td className={amountCls}>{fmtStat(line.hours_actual)}</td>
                  </tr>
                ))}
                <tr className="border-t border-line-strong bg-surface-sunken/60 font-semibold">
                  <td className="py-3 pl-4">Total actual vs estimate</td>
                  <td className={amountCls}>{fmtMoney(report.labor_variance.est_total)}</td>
                  <td className={amountCls}>
                    {fmtMoney(report.labor_variance.actual_total)}
                  </td>
                  <td className={amountCls}>
                    <span className="inline-flex items-center gap-1.5">
                      {report.labor_variance.alert && <Badge tone="warn">alert</Badge>}
                      {fmtVariance(report.labor_variance.variance_total)}
                    </span>
                  </td>
                  <td className={amountCls}>
                    {fmtMoney(report.labor_variance.burden_total)}
                  </td>
                  <td className={amountCls} />
                </tr>
              </tbody>
            </table>
          </div>
          {report.labor_variance.suppressed_departments > 0 && (
            <p className="mt-2 pl-6 text-xs text-ink-muted">
              Cost hidden for {report.labor_variance.suppressed_departments} single-employee
              department(s)
            </p>
          )}
          {report.labor_variance.unpriced_hours !== '0' && (
            <p className="mt-2 pl-6 text-xs text-ink-muted">
              Excludes {fmtStat(report.labor_variance.unpriced_hours)} unpriced hours (no
              rate on file)
            </p>
          )}
        </section>
      )}
    </div>
  )
}
