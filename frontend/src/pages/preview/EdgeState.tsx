// The two ways a preview can end without a P&L. Both used to end in a dead
// control: "Notify me when it's ready" was `disabled` with a coming-soon title,
// which is the last thing to show someone whose file just didn't work.
//
// Now both end somewhere real. Unsupported vendors get the setup-link form with
// copy that says plainly what naming their PMS during setup actually does —
// signup records it (`pms_interest`) and routes it to us. That is the whole of
// the mechanism, so it is what the page promises: no "we'll email you when it's
// live" from a system that has no such trigger.

import type { PreviewResponse } from '../../api/types'
import RequestAccess from './RequestAccess'

export default function EdgeState({ res, onRetry }: {
  res: Extract<PreviewResponse, { status: 'unsupported' | 'unreadable' }>
  onRetry: () => void
}) {
  if (res.status === 'unsupported') {
    return (
      <section role="region" aria-label="Unsupported PMS" className="space-y-3">
        <p className="text-brand-ink">
          This looks like a <b>{res.vendor}</b> report, and we can’t read {res.vendor} yet.
        </p>
        <p className="text-sm text-brand-ink-muted">
          Today we read Opera and AutoClerk night-audit reports. Everything else is a
          question of demand, and the way demand reaches us is through setup: start a
          workspace, choose “Other” when it asks which PMS you use, and name {res.vendor}.
          That goes on the list we work from.
        </p>
        <RequestAccess
          heading={`Put ${res.vendor} on the list`}
          blurb="We’ll email you a setup link. Name your PMS during setup and it reaches us."
          cta="Email me a setup link"
        />
        <button
          type="button"
          onClick={onRetry}
          className="rounded-control border border-brand-line px-4 py-1.5 text-brand-ink"
        >
          Try another file
        </button>
      </section>
    )
  }
  return (
    <section role="region" aria-label="Unreadable file" className="space-y-3">
      <p className="text-brand-ink">We couldn’t read that file.</p>
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
