// Public signup page (Track B/B1). Reached by an invited owner with no session,
// so it renders unguarded (RootShell bare Outlet). It reads ?token= from the
// URL, loads the invite on mount, and FAILS CLOSED: a missing or invalid token
// shows a single generic refusal with NO form — never a signup surface.

import { useQuery } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'
import { useState } from 'react'

import {
  completeSignup,
  getInvite,
  requestOtp,
  SignupError,
  type CompletePayload,
} from '../api/signup'
import { controlLargeClass } from '../components/ui'

// getRouteApi avoids the router.tsx <-> SignupPage.tsx circular value import.
const routeApi = getRouteApi('/signup')

const SUPPORTED_PMS = [
  { value: 'opera', label: 'Opera' },
  { value: 'autoclerk', label: 'AutoClerk' },
  { value: 'other', label: 'Other — my PMS isn’t listed' },
] as const
const JURISDICTIONS = ['US-CA', 'US-FL'] as const

// Derive a URL-safe workspace alias from the workspace name: lowercase, collapse
// non-alphanumerics to single hyphens, trim edge hyphens, cap at 63 chars. The
// trailing-hyphen trim runs AFTER the length cap so a hyphen that lands at char
// 63 isn't reintroduced.
function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+/, '')
    .slice(0, 63)
    .replace(/-+$/, '')
}

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
  // Stashed for Task 6 (real success screen + OIDC handoff).
  const [result, setResult] = useState<{ alias: string; supported: boolean } | null>(null)

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
          cell={cell}
          onDone={(r) => {
            setResult(r)
            setStep('done')
          }}
        />
      )}
      {step === 'done' && (
        // Minimal placeholder — Task 6 replaces this with the real success
        // screen + OIDC handoff. `result` is stashed for that step.
        <p className="mt-6 text-sm text-ink">
          Your workspace {result?.alias} is ready.
        </p>
      )}
    </div>
  )
}

// The details form: OTP + workspace/property fields, PMS select with an
// "Other" reveal, jurisdiction, password, and the completing submit.
function DetailsStep({
  token,
  cell,
  onDone,
}: {
  token: string
  cell: string
  onDone: (r: { alias: string; supported: boolean }) => void
}) {
  const [otp, setOtp] = useState('')
  const [workspaceName, setWorkspaceName] = useState('')
  const [alias, setAlias] = useState('')
  const [aliasEdited, setAliasEdited] = useState(false)
  const [propertyName, setPropertyName] = useState('')
  const [pms, setPms] = useState<'opera' | 'autoclerk' | 'other'>('opera')
  const [pmsOther, setPmsOther] = useState('')
  const [jurisdiction, setJurisdiction] = useState<string>('US-CA')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // The alias tracks the workspace name until the user edits the URL field.
  const effectiveAlias = aliasEdited ? alias : slugify(workspaceName)

  async function submit() {
    setBusy(true)
    setError(null)
    const payload: CompletePayload = {
      token,
      otp,
      workspace_name: workspaceName,
      workspace_alias: effectiveAlias,
      property_name: propertyName,
      pms_source: pms,
      wage_jurisdiction: jurisdiction,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      cell,
      password,
      ...(pms === 'other' ? { pms_other_name: pmsOther } : {}),
    }
    try {
      const res = await completeSignup(payload)
      onDone({ alias: res.org_alias, supported: res.pms_supported })
    } catch (e) {
      setError(
        e instanceof SignupError && e.status === 403
          ? 'That code is incorrect or expired.'
          : e instanceof SignupError && e.status === 429
            ? 'Too many requests — please wait and try again.'
            : 'Something didn’t go through. Check your details and try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className="mt-6 space-y-4"
      onSubmit={(e) => {
        e.preventDefault()
        void submit()
      }}
    >
      {error && (
        <p role="alert" className="text-sm text-danger-red">
          {error}
        </p>
      )}
      <Field
        label="Verification code"
        value={otp}
        onChange={setOtp}
        inputMode="numeric"
        autoComplete="one-time-code"
      />
      <Field label="Workspace name" value={workspaceName} onChange={setWorkspaceName} />
      <Field
        label="Workspace URL"
        value={effectiveAlias}
        onChange={(v) => {
          setAliasEdited(true)
          setAlias(v)
        }}
      />
      <Field label="Property name" value={propertyName} onChange={setPropertyName} />
      <label className="block text-sm">
        <span className="text-xs font-medium text-ink-muted">PMS</span>
        {/* NOTE for test authors: /pms/i also matches the "Which PMS do you use?"
            reveal field, so once "Other" is selected, query this select by role
            (getByRole('combobox', { name: 'PMS' })) to avoid an ambiguous match. */}
        <select
          aria-label="PMS"
          value={pms}
          onChange={(e) => setPms(e.target.value as typeof pms)}
          className={`mt-1 ${controlLargeClass}`}
        >
          {SUPPORTED_PMS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      {pms === 'other' && (
        <Field label="Which PMS do you use?" value={pmsOther} onChange={setPmsOther} />
      )}
      <label className="block text-sm">
        <span className="text-xs font-medium text-ink-muted">Jurisdiction</span>
        <select
          aria-label="Jurisdiction"
          value={jurisdiction}
          onChange={(e) => setJurisdiction(e.target.value)}
          className={`mt-1 ${controlLargeClass}`}
        >
          {JURISDICTIONS.map((j) => (
            <option key={j} value={j}>
              {j}
            </option>
          ))}
        </select>
      </label>
      <label className="block text-sm">
        <span className="text-xs font-medium text-ink-muted">Password</span>
        <input
          aria-label="Password"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className={`mt-1 ${controlLargeClass}`}
        />
      </label>
      <button
        type="submit"
        disabled={busy}
        className="w-full rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50"
      >
        Create workspace
      </button>
    </form>
  )
}

function Field({
  label,
  value,
  onChange,
  type,
  inputMode,
  autoComplete,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  inputMode?: 'text' | 'numeric'
  autoComplete?: string
}) {
  return (
    <label className="block text-sm">
      <span className="text-xs font-medium text-ink-muted">{label}</span>
      <input
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        type={type}
        inputMode={inputMode}
        autoComplete={autoComplete}
        className={`mt-1 ${controlLargeClass}`}
      />
    </label>
  )
}
