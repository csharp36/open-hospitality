import { describe, expect, it } from 'vitest'

import { ApiError } from '../api/client'
import { MAX_RETRIES, shouldRetry } from './queryRetry'

describe('shouldRetry', () => {
  it('never retries a client error — the answer will not change', () => {
    for (const status of [400, 401, 403, 404, 409, 422, 429]) {
      expect(shouldRetry(0, new ApiError(status, 'no'))).toBe(false)
    }
  })

  it('retries a server error or a network failure exactly once', () => {
    expect(MAX_RETRIES).toBe(1)
    expect(shouldRetry(0, new ApiError(500, 'boom'))).toBe(true)
    expect(shouldRetry(1, new ApiError(500, 'boom'))).toBe(false)
    expect(shouldRetry(0, new ApiError(503, 'later'))).toBe(true)
    expect(shouldRetry(0, new TypeError('Failed to fetch'))).toBe(true)
    expect(shouldRetry(1, new TypeError('Failed to fetch'))).toBe(false)
  })
})
