import type { PreviewResponse } from '../../api/types'

export default function EdgeState({ res, onRetry }: {
  res: Extract<PreviewResponse, { status: 'unsupported' | 'unreadable' }>
  onRetry: () => void
}) {
  if (res.status === 'unsupported') {
    return (
      <section role="region" aria-label="Unsupported PMS" className="space-y-3">
        <p className="text-brand-ink">🔎 This looks like a <b>{res.vendor}</b> report.</p>
        <p className="text-sm text-brand-ink-muted">We don't fully support {res.vendor} yet.</p>
        <button type="button" disabled title="Coming soon"
          className="rounded-control border border-brand-accent px-4 py-1.5 text-brand-accent opacity-70">
          Notify me when it's ready (coming soon)
        </button>
      </section>
    )
  }
  return (
    <section role="region" aria-label="Unreadable file" className="space-y-3">
      <p className="text-brand-ink">🤔 We couldn't read that file.</p>
      <ul className="list-disc pl-5 text-sm text-brand-ink-muted">
        {res.hints.map((h) => <li key={h}>{h}</li>)}
      </ul>
      <button type="button" onClick={onRetry}
        className="rounded-control border border-brand-line px-4 py-1.5 text-brand-ink">
        Try another file
      </button>
    </section>
  )
}
