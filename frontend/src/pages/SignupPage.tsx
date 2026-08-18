// Public signup page (Track B/B1). Reached by an invited owner with no session,
// so it renders unguarded (RootShell bare Outlet). It reads ?token= from the
// URL, loads the invite on mount, and FAILS CLOSED: a missing or invalid token
// shows a single generic refusal with NO form — never a signup surface.

import { useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'
import { useState } from 'react'

import { getInvite } from '../api/signup'

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
  // Step machine shell — Task 4 drives the transitions (cell → details → done).
  const [step] = useState<'cell' | 'details' | 'done'>('cell')
  void token // consumed by later steps (OTP request / complete)
  return (
    <div className="mx-auto max-w-md p-8">
      <h1 className="text-xl font-semibold">Create your workspace</h1>
      <p className="mt-1 text-sm text-ink-muted">Invited as {email}</p>
      {step === 'cell' && <CellStep />}
    </div>
  )
}

function CellStep() {
  // Skeleton stub: only the mobile-number field. Task 4 wires OTP sending.
  return (
    <form className="mt-6 space-y-4">
      <label className="block text-sm">
        Mobile number
        <input
          aria-label="Mobile number"
          name="cell"
          className="mt-1 w-full rounded border px-3 py-2"
        />
      </label>
    </form>
  )
}
