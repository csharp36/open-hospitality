import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../router'

vi.mock('../api/signup', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/signup')>()),
  getInvite: vi.fn(),
  requestOtp: vi.fn(),
  completeSignup: vi.fn(),
}))
import { getInvite } from '../api/signup'

function renderSignup(token = 'tok-123') {
  const router = createAppRouter(
    createMemoryHistory({ initialEntries: [`/signup?token=${token}`] }),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

// restoreAllMocks resets spies; clearAllMocks wipes vi.fn() call history so the
// "getInvite not called" assertion sees only this test's calls, not leftovers.
beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('SignupPage — invite load', () => {
  it('shows the invited email when the token is valid', async () => {
    vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
    renderSignup()
    expect(await screen.findByText(/owner@hotel\.test/)).toBeInTheDocument()
    // The cell step is available (a mobile field), not a refusal.
    expect(screen.getByLabelText(/mobile/i)).toBeInTheDocument()
  })

  it('shows one generic refusal and NO form when the invite is invalid', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(getInvite).mockRejectedValue(new SignupError(404))
    renderSignup('bad')
    expect(await screen.findByText(/isn'?t valid or has expired/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/mobile/i)).not.toBeInTheDocument()
  })

  it('shows the refusal and NO form when the token is absent (query disabled)', async () => {
    renderSignup('')
    expect(await screen.findByText(/isn'?t valid or has expired/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/mobile/i)).not.toBeInTheDocument()
    expect(getInvite).not.toHaveBeenCalled()
  })
})
