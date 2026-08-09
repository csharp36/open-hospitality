import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Badge,
  Card,
  PageHeader,
  amountCellClass,
  amountHeadClass,
  cellClass,
  controlClass,
  headCellClass,
  tableClass,
  type BadgeTone,
} from '../components/ui'
import {
  createPayRun,
  fetchPayRunResults,
  getMe,
  getPayRun,
  getPayRunLines,
  getPayRuns,
  getProperties,
} from '../api/client'
import { hasRole } from '../lib/roles'
import { errorMessage } from '../lib/errors'

// Pay runs (Pillar C2): create = preflight -> vault open at send -> provider
// submit. A 422 is the FEATURE — the preflight blocker list, rendered verbatim
// so the payroll admin fixes data problems instead of decoding a failed
// provider submit. Responses carry department AGGREGATES only; the C3
// per-employee detail loads ONLY behind the explicit "Show employee detail"
// click — the server audits every read, so each read must be a human who
// asked to see pay, not a navigation side effect.

function statusTone(status: string): BadgeTone {
  switch (status) {
    case 'processed':
      return 'ok'
    case 'submitted':
      return 'info'
    case 'failed':
      return 'danger'
    default:
      return 'neutral'
  }
}

export default function PayRunsPage() {
  const qc = useQueryClient()
  const me = useQuery({ queryKey: ['me'], queryFn: getMe })
  const properties = useQuery({ queryKey: ['properties'], queryFn: getProperties })
  // Mirrors the backend's require_payroll_admin EXACTLY (like the C1 vault
  // sub-form): showing this page to anyone else would only 403 on every call.
  const canPayroll = hasRole(me.data, 'payroll_admin')

  const [property, setProperty] = useState('')
  const [inPeriod, setInPeriod] = useState('')
  const [selected, setSelected] = useState<number | null>(null)
  // The audited-read gate: false until the payroll admin clicks "Show employee
  // detail" for the CURRENT run. selectRun resets it — switching runs must
  // never carry the reveal over (that would fire an un-asked-for audited read).
  const [showLines, setShowLines] = useState(false)

  function selectRun(id: number | null) {
    setSelected(id)
    setShowLines(false)
  }

  const propertyId = property || properties.data?.[0]?.property_id || ''

  const runs = useQuery({
    queryKey: ['pay-runs', propertyId],
    queryFn: () => getPayRuns(propertyId),
    enabled: canPayroll && propertyId !== '',
  })
  const detail = useQuery({
    queryKey: ['pay-run', selected],
    queryFn: () => getPayRun(selected as number),
    enabled: selected !== null,
  })
  // Runs ONLY after the explicit click (showLines), never on selection alone:
  // every server-side read_pay_run_lines audit row corresponds to a human who
  // asked to see pay. The audit contract also forbids BACKGROUND refetches —
  // TanStack Query's defaults (staleTime 0, refetch on window focus/reconnect)
  // would fire an audited read every time the tab regains focus, writing
  // read_pay_run_lines audit rows no human initiated.
  const lines = useQuery({
    queryKey: ['payrun-lines', selected],
    queryFn: () => getPayRunLines(selected as number),
    enabled: showLines && selected !== null,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: Infinity,
  })

  const create = useMutation({
    mutationFn: () => createPayRun({ property: propertyId, in_period: inPeriod }),
    onSuccess: (run) => selectRun(run.pay_run_id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['pay-runs'] }),
  })

  const fetchResults = useMutation({
    mutationFn: (id: number) => fetchPayRunResults(id),
    onSettled: (_data, _error, id) => {
      void qc.invalidateQueries({ queryKey: ['pay-runs'] })
      void qc.invalidateQueries({ queryKey: ['pay-run', id] })
    },
  })

  if (me.data && !canPayroll) {
    return (
      <div className="flex flex-col gap-5">
        <PageHeader title="Pay runs" />
        <Card>
          <p className="text-sm text-ink-muted">Pay runs require the payroll_admin role.</p>
        </Card>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Pay runs"
        subtitle="Run an approved pay period through the payroll provider and pull back actuals."
      />

      <Card>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => { e.preventDefault(); create.mutate() }}
        >
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">Property</span>
            <select className={controlClass} value={propertyId}
              onChange={(e) => { setProperty(e.target.value); selectRun(null) }}
              aria-label="Property">
              {(properties.data ?? []).map((p) => (
                <option key={p.property_id} value={p.property_id}>{p.property_id}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">In period</span>
            <input className={controlClass} type="date" value={inPeriod}
              onChange={(e) => setInPeriod(e.target.value)} required aria-label="In period" />
          </label>
          <button
            type="submit"
            disabled={create.isPending || !inPeriod || !propertyId}
            className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-50"
          >Create pay run</button>
        </form>
        {create.isError && (
          <div className="mt-2" role="alert" aria-label="pay run blockers">
            <p className="text-sm font-medium text-danger-red">Pay run not created:</p>
            {/* The service joins preflight blockers with "; " — split them back
                apart but render each blocker string verbatim. */}
            <ul className="mt-1 list-disc pl-5 text-sm text-danger-red">
              {errorMessage(create.error).split('; ').map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </div>
        )}
      </Card>

      <Card role="region" aria-label="pay runs">
        {runs.isLoading && <p className="text-sm text-ink-muted">Loading …</p>}
        {runs.isError && (
          <p className="text-sm text-danger-red">Failed to load: {errorMessage(runs.error)}</p>
        )}
        {runs.data && runs.data.length === 0 && (
          <p className="text-sm text-ink-muted">No pay runs yet for {propertyId}.</p>
        )}
        {runs.data && runs.data.length > 0 && (
          <table className={tableClass} aria-label="Pay runs">
            <thead>
              <tr className="border-b border-line">
                <th className={headCellClass}>Period</th>
                <th className={headCellClass}>Status</th>
                <th className={headCellClass}>Provider</th>
                <th className={headCellClass}>Check date</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.pay_run_id} className="border-b border-line last:border-0">
                  <td className={cellClass}>
                    <button
                      type="button"
                      onClick={() => selectRun(r.pay_run_id)}
                      className="rounded-control px-1 text-accent hover:bg-surface-sunken"
                    >{r.period_start} → {r.period_end}</button>
                  </td>
                  <td className={cellClass}>
                    <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                  </td>
                  <td className={cellClass}>{r.provider}</td>
                  <td className={cellClass}>{r.check_date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {selected !== null && (
        <Card role="region" aria-label="pay run detail">
          {detail.isPending && <p className="text-sm text-ink-muted">Loading …</p>}
          {detail.isError && (
            <p className="text-sm text-danger-red">Failed to load: {errorMessage(detail.error)}</p>
          )}
          {detail.data && (
            <>
              <PageHeader
                level={2}
                title={`Run #${detail.data.pay_run_id} — ${detail.data.period_start} → ${detail.data.period_end}`}
                subtitle={`${detail.data.provider} · check date ${detail.data.check_date}`}
                actions={
                  <>
                    <Badge tone={statusTone(detail.data.status)}>{detail.data.status}</Badge>
                    {detail.data.status === 'submitted' && (
                      <button
                        type="button"
                        onClick={() => fetchResults.mutate(detail.data.pay_run_id)}
                        disabled={fetchResults.isPending}
                        className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast disabled:opacity-50"
                      >Fetch results</button>
                    )}
                  </>
                }
              />
              {detail.data.failure_reason !== null && (
                <p className="mb-2 text-sm text-danger-red">
                  Failed: {detail.data.failure_reason}
                </p>
              )}
              {fetchResults.isError && (
                <p className="mb-2 text-sm text-danger-red">
                  Fetch failed: {errorMessage(fetchResults.error)}
                </p>
              )}
              {detail.data.department_aggregates.length === 0 ? (
                <p className="text-sm text-ink-muted">
                  No results yet — department aggregates appear after Fetch results.
                </p>
              ) : (
                <table className={tableClass} aria-label="Department aggregates">
                  <thead>
                    <tr className="border-b border-line">
                      <th className={headCellClass}>Department</th>
                      <th className={amountHeadClass}>Hours</th>
                      <th className={amountHeadClass}>Gross</th>
                      <th className={amountHeadClass}>Employer burden</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.department_aggregates.map((a) => (
                      <tr key={a.department} className="border-b border-line last:border-0">
                        <td className={cellClass}>{a.department}</td>
                        <td className={amountCellClass}>{a.hours}</td>
                        <td className={amountCellClass}>{a.gross}</td>
                        <td className={amountCellClass}>{a.employer_burden}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {!showLines ? (
                <div className="mt-3">
                  <button
                    type="button"
                    onClick={() => setShowLines(true)}
                    className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast"
                  >Show employee detail</button>
                  <p className="mt-1 text-xs text-ink-muted">
                    Per-employee pay — every view is recorded in the audit trail.
                  </p>
                </div>
              ) : (
                <div className="mt-3">
                  {lines.isPending && <p className="text-sm text-ink-muted">Loading …</p>}
                  {lines.isError && (
                    <p className="text-sm text-danger-red">
                      Failed to load: {errorMessage(lines.error)}
                    </p>
                  )}
                  {lines.data && (lines.data.lines.length === 0 ? (
                    <p className="text-sm text-ink-muted">No employee lines on this run.</p>
                  ) : (
                    <table className={tableClass} aria-label="Employee detail">
                      <thead>
                        <tr className="border-b border-line">
                          <th className={headCellClass}>Employee</th>
                          <th className={amountHeadClass}>Hours</th>
                          <th className={amountHeadClass}>Gross</th>
                          <th className={amountHeadClass}>Employee taxes</th>
                          <th className={amountHeadClass}>Employer taxes</th>
                          <th className={amountHeadClass}>Net</th>
                        </tr>
                      </thead>
                      <tbody>
                        {lines.data.lines.map((l) => (
                          <tr key={l.employee_id} className="border-b border-line last:border-0">
                            <td className={cellClass}>{l.employee_name}</td>
                            <td className={amountCellClass}>{l.hours}</td>
                            <td className={amountCellClass}>{l.gross}</td>
                            <td className={amountCellClass}>{l.employee_taxes}</td>
                            <td className={amountCellClass}>{l.employer_taxes}</td>
                            <td className={amountCellClass}>{l.net}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ))}
                </div>
              )}
            </>
          )}
        </Card>
      )}
    </div>
  )
}
