// Public signup page (Track B/B1). Reached by an invited owner with no session,
// so it renders unguarded (RootShell bare Outlet). It reads ?token= from the
// URL, loads the invite on mount, and FAILS CLOSED: a missing or invalid token
// shows a single generic refusal with NO form — never a signup surface.

import { useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'
import { useState } from 'react'

import { getInvite, requestOtp, SignupError } from '../api/signup'
import { controlLargeClass } from '../components/ui'

// getRouteApi avoids the router.tsx <-> SignupPage.tsx circular value import.
const routeApi = getRouteApi('/signup')

export default function SignupPage() {
  const { token } = routeApi.useSearch()
  const invite = useQuery({
    queryKey: ['signup-invite', token],
    queryFn: () => getInvite(token as string),
    enabled: Boolean(token),
    retry: false,
  })

  // Fail closed: no token, or the invite lookup failed (404/expired/etc.) →
  // one generic refusal, no form. We never distinguish the failure reason.
  if (!token || invite.isError)
    return (
      <div className="mx-auto max-w-md p-8 text-center">
        <p className="text-ink-muted">
          This invite link isn&apos;t valid or has expired.
        </p>
      </div>
    )
  if (invite.isPending)
    return <div className="mx-auto max-w-md p-8 text-center text-ink-muted">Loading…</div>

  return <SignupFlow token={token} email={invite.data} />
}

function SignupFlow({ token, email }: { token: string; email: string }) {
  // Step machine — cell → details → done. Task 4 drives cell → details.
  const [step, setStep] = useState<'cell' | 'details' | 'done'>('cell')
  const [cell, setCell] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function sendCode() {
    setBusy(true)
    setError(null)
    try {
      await requestOtp(token, cell)
      setStep('details')
    } catch (e) {
      // 429 -> back off; anything else collapses to the generic invalid-link
      // copy so we never leak which failure occurred.
      setError(
        e instanceof SignupError && e.status === 429
          ? 'Too many requests — please wait a minute and try again.'
          : "This invite link isn't valid or has expired.",
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-md p-8">
      <h1 className="text-xl font-semibold">Create your workspace</h1>
      <p className="mt-1 text-sm text-ink-muted">Invited as {email}</p>
      {error && (
        <p role="alert" className="mt-4 text-sm text-danger-red">
          {error}
        </p>
      )}
      {step === 'cell' && (
        <form
          className="mt-6 space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            void sendCode()
          }}
        >
          <label className="block text-sm">
            <span className="text-xs font-medium text-ink-muted">Mobile number</span>
            <input
              aria-label="Mobile number"
              name="cell"
              type="tel"
              inputMode="tel"
              autoComplete="tel"
              value={cell}
              onChange={(e) => setCell(e.target.value)}
              className={`mt-1 ${controlLargeClass}`}
            />
          </label>
          <button
            type="submit"
            disabled={busy || !cell}
            className="w-full rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50"
          >
            Send code
          </button>
        </form>
      )}
      {step === 'details' && (
        <DetailsStep
          token={token}
          email={email}
          cell={cell}
          onDone={() => setStep('done')}
        />
      )}
    </div>
  )
}

// STUB — Task 5 fleshes out the details form (OTP + workspace fields + submit).
function DetailsStep(_: {
  token: string
  email: string
  cell: string
  onDone: () => void
}) {
  return (
    <form className="mt-6 space-y-4">
      <label className="block text-sm">
        <span className="text-xs font-medium text-ink-muted">Verification code</span>
        <input aria-label="Verification code" name="otp" className={`mt-1 ${controlLargeClass}`} />
      </label>
    </form>
  )
}
