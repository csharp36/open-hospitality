// Renders a successful `ok` preview payload for the anonymous front door: the
// "nothing saved" banner, an honest coverage line (no "ties out" claim — that
// reconciliation signal isn't built yet), the P&L lines, and a LIVE CTA that
// emails a workspace setup link.
//
// Two things here are deliberately not what they look like they should be.
//
// The coverage line is written out in words rather than shown as a ratio,
// because "13 of 14" reads as a score to an owner who has never seen the
// mapping, and the number that matters to them is the one they have to do
// something about.
//
// "Not what you expected?" is a real disclosure, not decoration. It used to be
// a <button> with no onClick at all — an affordance that looked live and did
// nothing, on the page we send to strangers.

import { Fragment, useMemo, useState } from 'react'

import type { PreviewPayload } from '../../api/types'
import RequestAccess from './RequestAccess'

// The API returns the PMS/report identifiers as the pipeline names them
// ("OPERA", "trial_balance"). Nobody outside this repo calls a report that.
const SOURCE_LABELS: Record<string, string> = {
  OPERA: 'Opera',
  AUTOCLERK: 'AutoClerk',
  SKYTOUCH: 'SkyTouch',
}
const REPORT_LABELS: Record<string, string> = {
  trial_balance: 'trial balance',
  transaction_summary: 'transaction summary',
}

function label(map: Record<string, string>, key: string): string {
  return map[key] ?? key.replace(/_/g, ' ')
}

// The order the server returns is alphabetical by (major, sub, line_item) —
// which puts Parking above Room Revenue and reads like a database dump, not a
// P&L. A statement leads with what the hotel sold, then the taxes it is only
// collecting, then how any of it was paid for. Sorting is a PRESENTATION
// decision, so it lives here rather than changing what the API returns.
const MAJOR_ORDER = [
  'Operated Departments',
  'Miscellaneous Income',
  'Non-Operating',
  'Taxes (Pass-Through)',
  'Settlements',
]

function majorRank(major: string): number {
  const i = MAJOR_ORDER.indexOf(major)
  // An unrecognised major sorts after the known ones rather than silently
  // taking Operated Departments' place at the top.
  return i === -1 ? MAJOR_ORDER.length : i
}

// Within a department, rooms come first — it is the one line every owner looks
// for. Everything else keeps the server's alphabetical order, which a stable
// sort preserves.
function subRank(sub: string): number {
  return sub === 'Rooms' ? 0 : 1
}

// The API sends amounts as plain decimal strings. Group them and put negatives
// in parentheses, which is how they read on the report the visitor just dropped.
function money(amount: string | number): string {
  const value = Number(amount)
  if (!Number.isFinite(value)) return String(amount)
  const shown = Math.abs(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  return value < 0 ? `(${shown})` : shown
}

export default function PreviewResult({ payload }: { payload: PreviewPayload }) {
  const [explain, setExplain] = useState(false)
  const source = label(SOURCE_LABELS, payload.pms_source)
  const report = label(REPORT_LABELS, payload.report_type)

  // Group under the major headings a statement uses, so the department name is
  // said once instead of trailing every row. The subtotal is arithmetic on the
  // lines above it — it is NOT a reconciliation signal, and nothing here claims
  // the report ties out.
  const groups = useMemo(() => {
    const ordered = [...payload.pnl_lines].sort(
      (a, b) => majorRank(a.major) - majorRank(b.major) || subRank(a.sub) - subRank(b.sub),
    )
    const out: { major: string; lines: typeof ordered; total: number }[] = []
    for (const line of ordered) {
      const last = out[out.length - 1]
      if (last && last.major === line.major) last.lines.push(line)
      else out.push({ major: line.major, lines: [line], total: 0 })
    }
    for (const group of out)
      group.total = group.lines.reduce((sum, l) => sum + (Number(l.amount) || 0), 0)
    return out
  }, [payload.pnl_lines])

  return (
    <section role="region" aria-label="Preview result" className="space-y-4">
      <p className="rounded-control bg-brand-surface px-3 py-2 text-sm text-brand-ink-muted">
        🔒 Nothing saved — this preview lives in your browser session only.
      </p>

      <div>
        <p className="text-brand-ink">
          Read as a <b>{source} {report}</b> for {payload.business_date}.{' '}
          <button
            type="button"
            aria-expanded={explain}
            onClick={() => setExplain((v) => !v)}
            className="underline"
          >
            Not what you expected?
          </button>
        </p>
        {explain && (
          <p className="mt-2 rounded-control bg-brand-surface px-3 py-2 text-sm text-brand-ink-muted">
            We match on the report’s own title, so this is what the PDF calls itself. If
            it’s wrong, it’s usually a different report from the same system — try the
            trial balance or transaction summary your PMS prints at the end of the night
            audit, rather than a folio, a forecast, or a scan.
          </p>
        )}
      </div>

      <table className="w-full text-sm">
        <tbody>
          {groups.map((group) => (
            <Fragment key={group.major}>
              <tr>
                <th
                  colSpan={2}
                  scope="colgroup"
                  className="pt-4 pb-1 text-left font-display text-base font-normal text-brand-ink"
                >
                  {group.major}
                </th>
              </tr>
              {group.lines.map((l, i) => (
                <tr key={`${l.sub}-${l.line_item}-${i}`} className="border-b border-brand-line">
                  <td className="py-1.5 pl-4 text-brand-ink">
                    {l.line_item}
                    {l.sub && l.sub !== l.line_item && (
                      <span className="text-brand-ink-muted"> · {l.sub}</span>
                    )}
                  </td>
                  <td className="py-1.5 text-right font-mono text-brand-ink">{money(l.amount)}</td>
                </tr>
              ))}
              {group.lines.length > 1 && (
                <tr className="border-b-2 border-brand-line">
                  <td className="py-1.5 pl-4 text-brand-ink-muted">Total {group.major}</td>
                  <td className="py-1.5 text-right font-mono text-brand-ink">
                    {money(group.total)}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>

      {payload.kpis.length > 0 && (
        <div className="flex gap-6 font-mono">
          {payload.kpis.map((k, i) => (
            <div key={`${k.label}-${i}`}>
              <div className="text-lg">{k.value}</div>
              <div className="text-xs text-brand-ink-muted">{k.label}</div>
            </div>
          ))}
        </div>
      )}

      <p className="text-sm text-brand-ink-muted">
        {payload.codes_needs_review > 0 ? (
          <>
            We placed {payload.codes_mapped} of your {payload.codes_recognized} charge codes.{' '}
            <b className="text-brand-ink">{payload.codes_needs_review} still need a human</b> —
            house codes differ at every property, and guessing at one is how a number ends up
            in the wrong place all year.
          </>
        ) : (
          <>We placed all {payload.codes_recognized} charge codes on this report.</>
        )}
      </p>

      <RequestAccess
        heading="Want this every morning?"
        blurb={
          'A workspace keeps the history, does this automatically for each business date, ' +
          'and lets you fix the codes above once instead of every night.'
        }
      />
    </section>
  )
}
