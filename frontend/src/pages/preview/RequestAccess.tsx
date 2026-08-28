// The one live action on the anonymous front door: ask for a workspace setup
// link. It replaces two disabled "coming soon" buttons that looked like
// controls and did nothing — the worst thing a page shown to strangers can do,
// because a dead control reads as a broken product rather than an unbuilt one.
//
// The confirmation deliberately does NOT say "we've sent it to you". The server
// answers identically for an address that already has a workspace and one it
// has never seen (no existence oracle), so the page cannot honestly claim
// delivery — only that a link is on its way if that address can receive mail.

import { useState } from 'react'

import { requestInvite, SignupError } from '../../api/signup'

// A shape check, matching the server's: catch the obvious typo before a
// round-trip. Whether the address is real is settled by whether mail arrives.
const EMAIL_RE = /^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/

export default function RequestAccess({
  heading,
  blurb,
  cta = 'Email me a setup link',
}: {
  heading: string
  blurb: string
  cta?: string
}) {
  const [email, setEmail] = useState('')
  const [state, setState] = useState<'idle' | 'sending' | 'sent'>('idle')
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    if (!EMAIL_RE.test(email.trim())) {
      setError('That address doesn’t look right — check it and try again.')
      return
    }
    setError(null)
    setState('sending')
    try {
      await requestInvite(email.trim())
      setState('sent')
    } catch (e) {
      // Each of these tells the visitor something they can act on. The
      // catch-all deliberately does not blame their details — by this point the
      // address has passed the same check the server applies, so a failure here
      // is ours.
      setError(
        e instanceof SignupError && e.status === 429
          ? 'That’s a few requests in a row — give it a minute and try again.'
          : e instanceof SignupError && e.status === 422
            ? 'That address doesn’t look right — check it and try again.'
            : e instanceof SignupError && e.status === 502
              ? 'We couldn’t send the email just now. Please try again in a moment.'
              : 'Something went wrong on our end. Please try again in a moment.',
      )
      setState('idle')
    }
  }

  if (state === 'sent')
    return (
      <section
        role="status"
        aria-label="Setup link requested"
        className="rounded-card border border-brand-line bg-brand-surface p-4"
      >
        <p className="text-brand-ink">Check {email.trim()}.</p>
        <p className="mt-1 text-sm text-brand-ink-muted">
          If that address can receive mail, a setup link is on its way. It works once and
          expires in seven days. Nothing has been created yet — you finish setup from the
          link.
        </p>
      </section>
    )

  return (
    <section aria-label={heading} className="rounded-card border border-brand-line bg-brand-surface p-4">
      <h2 className="font-display text-lg text-brand-ink">{heading}</h2>
      <p className="mt-1 text-sm text-brand-ink-muted">{blurb}</p>
      <form
        className="mt-3 flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
        }}
      >
        <input
          aria-label="Email address"
          type="email"
          autoComplete="email"
          placeholder="you@yourhotel.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="min-w-0 flex-1 rounded-control border border-brand-line bg-brand-canvas px-3 py-2 text-brand-ink placeholder:text-brand-ink-muted"
        />
        <button
          type="submit"
          disabled={state === 'sending' || !email}
          className="rounded-control bg-brand-accent px-4 py-2 text-white disabled:opacity-60"
        >
          {state === 'sending' ? 'Sending…' : cta}
        </button>
      </form>
      {error && (
        <p role="alert" className="mt-2 text-sm text-danger-red">
          {error}
        </p>
      )}
    </section>
  )
}
