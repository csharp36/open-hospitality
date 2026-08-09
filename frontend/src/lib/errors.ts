// Single display convention for caught errors across pages: an ApiError
// renders its bare FastAPI `detail` (no "HTTP 422:" prefix); anything else
// falls back to the Error message.

import { ApiError } from '../api/client'

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail
  return error instanceof Error ? error.message : String(error)
}
