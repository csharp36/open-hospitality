// Renders a successful `ok` preview payload for the anonymous front door:
// the "nothing saved" banner, an honest coverage line (no "ties out" claim —
// that reconciliation signal isn't built yet), the USALI P&L lines, and a
// DEFERRED early-access CTA (visual only — marked coming-soon, not a live
// action).

import type { PreviewPayload } from '../../api/types'

export default function PreviewResult({ payload }: { payload: PreviewPayload }) {
  return (
    <section role="region" aria-label="Preview result" className="space-y-4">
      <p className="rounded-control bg-brand-surface px-3 py-2 text-sm text-brand-ink-muted">
        🔒 Nothing saved — this preview lives in your browser session only.
      </p>

      <header className="flex items-baseline justify-between">
        <span className="text-sm text-brand-ink-muted">
          Recognized:{' '}
          <b className="text-brand-ink">
            {payload.pms_source} · {payload.report_type}
          </b>{' '}
          <button type="button" className="underline">
            not right?
          </button>
        </span>
        <span className="font-mono text-xs text-brand-ink-muted">
          {payload.codes_mapped} of {payload.codes_recognized} mapped · {payload.codes_needs_review}{' '}
          need review
        </span>
      </header>

      <table className="w-full text-sm">
        <tbody>
          {payload.pnl_lines.map((l, i) => (
            <tr key={`${l.major}-${l.sub}-${l.line_item}-${i}`} className="border-b border-brand-line">
              <td className="py-1.5 text-brand-ink">
                {l.major} — {l.line_item}
              </td>
              <td className="py-1.5 text-right font-mono text-brand-ink">{l.amount}</td>
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

      <button
        type="button"
        disabled
        title="Coming soon"
        className="rounded-control bg-brand-accent px-4 py-2 text-white opacity-70"
      >
        Save &amp; automate → Get early access (coming soon)
      </button>
    </section>
  )
}
