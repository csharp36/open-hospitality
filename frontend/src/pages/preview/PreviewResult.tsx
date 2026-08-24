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

import { useState } from 'react'

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

// The API sends amounts as plain decimal strings. Group them and put negatives
// in parentheses, which is how they read on the report the visitor just dropped.
function money(amount: string): string {
  const value = Number(amount)
  if (!Number.isFinite(value)) return amount
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
          {payload.pnl_lines.map((l, i) => (
            <tr key={`${l.major}-${l.sub}-${l.line_item}-${i}`} className="border-b border-brand-line">
              <td className="py-1.5 text-brand-ink">
                {l.line_item}
                <span className="text-brand-ink-muted"> · {l.major}</span>
              </td>
              <td className="py-1.5 text-right font-mono text-brand-ink">{money(l.amount)}</td>
            </tr>
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
