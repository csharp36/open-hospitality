// The app-wide react-query retry rule.
//
// A 4xx is the server's considered answer to this exact request — a 401
// session that expired, a 403 role it does not hold, a 404 day with no data,
// a 409 lock, a 422 the validator refused — and asking three more times will
// not change it. A 5xx or a network failure may be a blip, so it gets ONE
// retry (a second failure in a row is almost always persistent).
//
// Measured before this rule: Setup, General Ledger and Integrations each sat
// on "Loading…" for 7.7s before showing a deterministic 500, because the
// default three retries back off 1s + 2s + 4s. With one retry that is ~1s.
import { ApiError } from '../api/client'

export const MAX_RETRIES = 1

export function shouldRetry(failureCount: number, err: unknown): boolean {
  if (err instanceof ApiError && err.status >= 400 && err.status < 500) return false
  return failureCount < MAX_RETRIES
}
