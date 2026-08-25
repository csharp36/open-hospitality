// RequestAccess: the one live write on the anonymous front door.
//
// The confirmation copy is the thing worth pinning. The server answers 202 for
// any well-formed address whether or not it has ever been seen (no existence
// oracle), so a page that says "we've sent it to you" would be asserting
// something it was deliberately not told.

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/signup', async (orig) => ({
  ...(await orig<typeof import('../../api/signup')>()),
  requestInvite: vi.fn(),
}))

import { requestInvite, SignupError } from '../../api/signup'
import RequestAccess from './RequestAccess'

function renderForm() {
  render(<RequestAccess heading="Want this every morning?" blurb="A workspace keeps it." />)
}

async function submit(email: string) {
  await userEvent.type(screen.getByLabelText('Email address'), email)
  await userEvent.click(screen.getByRole('button', { name: /setup link/i }))
}

describe('RequestAccess', () => {
  // Block body, not a concise one: returning the MockInstance from the hook
  // makes vitest report the next test's handled rejection as an unhandled one.
  beforeEach(() => {
    vi.mocked(requestInvite).mockReset()
  })

  it('sends the address and confirms without claiming the account exists', async () => {
    vi.mocked(requestInvite).mockResolvedValue()
    renderForm()

    await submit('owner@hotel.test')

    await waitFor(() => expect(requestInvite).toHaveBeenCalledWith('owner@hotel.test'))
    const status = await screen.findByRole('status', { name: 'Setup link requested' })
    expect(status).toHaveTextContent('Check owner@hotel.test')
    // Conditional, because the server never told us whether it can receive mail.
    expect(status).toHaveTextContent(/if that address can receive mail/i)
  })

  it('catches a malformed address before spending a round-trip', async () => {
    // "owner@hotel" passes the browser's own type=email check but fails ours,
    // which wants a dot — the typo an owner actually makes.
    renderForm()
    await submit('owner@hotel')
    expect(requestInvite).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent(/doesn’t look right/i)
  })

  it.each([
    [429, /give it a minute/i],
    [502, /couldn’t send the email/i],
    [500, /on our end/i],
  ])('explains a %i instead of blaming the visitor’s details', async (status, expected) => {
    // mockImplementation, not mockRejectedValue: the latter builds the rejected
    // promise immediately, before anything awaits it, which vitest reports as an
    // unhandled rejection.
    vi.mocked(requestInvite).mockImplementation(() => Promise.reject(new SignupError(status)))
    renderForm()

    await submit('owner@hotel.test')

    expect(await screen.findByRole('alert')).toHaveTextContent(expected)
    // The form is still there to retry with — a failed send must not strand them
    // on a dead confirmation screen.
    expect(screen.getByLabelText('Email address')).toBeInTheDocument()
  })
})
