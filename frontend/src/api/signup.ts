// Public signup API — Track B/B1 Part-2. Unlike src/api/client.ts these
// endpoints are UNAUTHENTICATED (an owner holding an invite token, no session),
// so we use bare fetch with only Content-Type: NO Authorization/X-Active-Org.
// Every failure surfaces as a SignupError carrying the HTTP status, so the page
// can branch (404 -> generic "invalid link", 403 -> wrong OTP, 429 -> back off).

export class SignupError extends Error {
  readonly status: number

  constructor(status: number) {
    super(`signup request failed: ${status}`)
    this.name = 'SignupError'
    this.status = status
  }
}

export interface CompletePayload {
  token: string
  otp: string
  workspace_name: string
  workspace_alias: string
  property_name: string
  pms_source: 'opera' | 'autoclerk' | 'other'
  pms_other_name?: string
  wage_jurisdiction: string
  timezone?: string
  cell: string
  password: string
}

export interface CompleteResult {
  org_alias: string
  pms_supported: boolean
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

export async function getInvite(token: string): Promise<string> {
  const res = await fetch(`/api/signup/invite/${encodeURIComponent(token)}`, {
    headers: JSON_HEADERS,
  })
  if (!res.ok) throw new SignupError(res.status)
  const body = (await res.json()) as { email: string }
  return body.email
}

export async function requestOtp(token: string, cell: string): Promise<void> {
  const res = await fetch('/api/signup/otp', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ token, cell }),
  })
  if (!res.ok) throw new SignupError(res.status)
}

export async function completeSignup(payload: CompletePayload): Promise<CompleteResult> {
  const res = await fetch('/api/signup/complete', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new SignupError(res.status)
  return (await res.json()) as CompleteResult
}
