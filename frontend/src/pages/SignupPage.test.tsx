import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../router'

vi.mock('../api/signup', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/signup')>()),
  getInvite: vi.fn(),
  requestOtp: vi.fn(),
  completeSignup: vi.fn(),
}))
import { getInvite, requestOtp, completeSignup } from '../api/signup'

vi.mock('../auth/oidc', () => ({ login: vi.fn() }))
import { login } from '../auth/oidc'

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
    // findAll: the address is shown twice now — "Invited as ..." and the line
    // saying where the verification code is going.
    expect((await screen.findAllByText(/owner@hotel\.test/)).length).toBeGreaterThan(0)
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

describe('SignupPage — cell step', () => {
  it('sends the OTP and advances to the details step', async () => {
    vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
    vi.mocked(requestOtp).mockResolvedValue(undefined)
    renderSignup()
    const cell = await screen.findByLabelText(/mobile/i)
    await userEvent.type(cell, '+15550000000')
    await userEvent.click(screen.getByRole('button', { name: /send code/i }))
    await waitFor(() =>
      expect(requestOtp).toHaveBeenCalledWith('tok-123', '+15550000000'),
    )
    // Details step is now shown (a verification-code field appears).
    expect(await screen.findByLabelText(/verification code/i)).toBeInTheDocument()
  })

  it('shows a back-off message on a 429 and stays on the cell step', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
    vi.mocked(requestOtp).mockRejectedValue(new SignupError(429))
    renderSignup()
    await userEvent.type(await screen.findByLabelText(/mobile/i), '+15550000000')
    await userEvent.click(screen.getByRole('button', { name: /send code/i }))
    expect(await screen.findByText(/too many/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/verification code/i)).not.toBeInTheDocument()
  })

  it('shows the generic invalid-link error on a non-429 failure and stays on the cell step', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
    vi.mocked(requestOtp).mockRejectedValue(new SignupError(404))
    renderSignup()
    await userEvent.type(await screen.findByLabelText(/mobile/i), '+15550000000')
    await userEvent.click(screen.getByRole('button', { name: /send code/i }))
    expect(await screen.findByText(/isn'?t valid or has expired/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/verification code/i)).not.toBeInTheDocument()
  })
})

async function toDetails() {
  vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
  vi.mocked(requestOtp).mockResolvedValue(undefined)
  renderSignup()
  await userEvent.type(await screen.findByLabelText(/mobile/i), '+15550000000')
  await userEvent.click(screen.getByRole('button', { name: /send code/i }))
  await screen.findByLabelText(/verification code/i)
}

describe('SignupPage — details step', () => {
  it('auto-slugs the workspace alias from the name (editable)', async () => {
    await toDetails()
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'Sunset Group!!')
    expect((screen.getByLabelText(/workspace url/i) as HTMLInputElement).value).toBe('sunset-group')
  })

  it('reveals a PMS name field only when "Other" is chosen', async () => {
    await toDetails()
    expect(screen.queryByLabelText(/which pms/i)).not.toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'other')
    expect(screen.getByLabelText(/which pms/i)).toBeInTheDocument()
  })

  it('offers SkyTouch as a selectable source, with no "which PMS" follow-up', async () => {
    // Held out of the list while its Hotel Statistics adapter was un-registered;
    // both its reports parse now. Selecting it must NOT reveal the free-text
    // field, which is what marks a source as unsupported.
    await toDetails()
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'skytouch')
    expect(screen.queryByLabelText(/which pms/i)).not.toBeInTheDocument()
  })

  it('submits skytouch as the pms_source', async () => {
    vi.mocked(completeSignup).mockResolvedValue({ org_alias: 'redstone', pms_supported: true })
    await toDetails()
    await userEvent.type(screen.getByLabelText(/verification code/i), '123456')
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'Redstone Group')
    await userEvent.type(screen.getByLabelText(/property name/i), 'Redstone Inn')
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'skytouch')
    await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(completeSignup).mock.calls[0]![0]
    expect(payload).toMatchObject({ pms_source: 'skytouch' })
    expect(payload).not.toHaveProperty('pms_other_name')
    // The success screen must not apologise for an unsupported PMS.
    expect(await screen.findByText(/ready/i)).toBeInTheDocument()
    expect(screen.queryByText(/don.t support/i)).not.toBeInTheDocument()
  })

  it('submits the full payload and advances on 201', async () => {
    vi.mocked(completeSignup).mockResolvedValue({ org_alias: 'sunset-group', pms_supported: true })
    await toDetails()
    await userEvent.type(screen.getByLabelText(/verification code/i), '123456')
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'Sunset Group')
    await userEvent.type(screen.getByLabelText(/property name/i), 'Sunset Inn')
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'opera')
    await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(completeSignup).mock.calls[0]![0]
    expect(payload).toMatchObject({
      token: 'tok-123', otp: '123456', workspace_name: 'Sunset Group',
      workspace_alias: 'sunset-group', property_name: 'Sunset Inn',
      pms_source: 'opera', wage_jurisdiction: 'US-CA', cell: '+15550000000',
      password: 'passw0rd1',
    })
    expect(payload.timezone).toBeTruthy() // browser-detected
    expect(payload).not.toHaveProperty('pms_other_name')
    expect(await screen.findByText(/ready/i)).toBeInTheDocument()
  })

  it('includes pms_other_name in the payload when PMS is "Other"', async () => {
    vi.mocked(completeSignup).mockResolvedValue({ org_alias: 'x-group', pms_supported: false })
    await toDetails()
    await userEvent.type(screen.getByLabelText(/verification code/i), '123456')
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'X Group')
    await userEvent.type(screen.getByLabelText(/property name/i), 'X Inn')
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'other')
    await userEvent.type(screen.getByLabelText(/which pms/i), 'HotelKey')
    await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(completeSignup).mock.calls[0]![0]
    expect(payload).toMatchObject({ pms_source: 'other', pms_other_name: 'HotelKey' })
  })

  it('stops auto-slugging the workspace URL once the user edits it', async () => {
    await toDetails()
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'Sunset')
    const url = screen.getByLabelText(/workspace url/i) as HTMLInputElement
    await userEvent.clear(url)
    await userEvent.type(url, 'custom-alias')
    await userEvent.type(screen.getByLabelText(/workspace name/i), ' Group')
    expect(url.value).toBe('custom-alias')
  })

  it('submits the manually-edited workspace alias in the payload', async () => {
    vi.mocked(completeSignup).mockResolvedValue({ org_alias: 'custom-alias', pms_supported: true })
    await toDetails()
    await userEvent.type(screen.getByLabelText(/verification code/i), '123456')
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'Sunset Group')
    const url = screen.getByLabelText(/workspace url/i) as HTMLInputElement
    await userEvent.clear(url)
    await userEvent.type(url, 'custom-alias')
    await userEvent.type(screen.getByLabelText(/property name/i), 'Sunset Inn')
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'opera')
    await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    expect(vi.mocked(completeSignup).mock.calls[0]![0]).toMatchObject({
      workspace_alias: 'custom-alias',
    })
  })

  it('shows an inline retry on a wrong OTP (403) and stays on the details step', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(completeSignup).mockRejectedValue(new SignupError(403))
    await toDetails()
    await userEvent.type(screen.getByLabelText(/verification code/i), '000000')
    await userEvent.type(screen.getByLabelText(/workspace name/i), 'X Group')
    await userEvent.type(screen.getByLabelText(/property name/i), 'X Inn')
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'opera')
    await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    expect(await screen.findByText(/code is incorrect or expired/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/verification code/i)).toBeInTheDocument()
  })
})

async function completeTo(supported: boolean, otherName?: string) {
  vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
  vi.mocked(requestOtp).mockResolvedValue(undefined)
  vi.mocked(completeSignup).mockResolvedValue({ org_alias: 'sunset-group', pms_supported: supported })
  renderSignup()
  await userEvent.type(await screen.findByLabelText(/mobile/i), '+15550000000')
  await userEvent.click(screen.getByRole('button', { name: /send code/i }))
  await userEvent.type(await screen.findByLabelText(/verification code/i), '123456')
  await userEvent.type(screen.getByLabelText(/workspace name/i), 'Sunset Group')
  await userEvent.type(screen.getByLabelText(/property name/i), 'Sunset Inn')
  if (otherName) {
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'other')
    await userEvent.type(screen.getByLabelText(/which pms/i), otherName)
  } else {
    await userEvent.selectOptions(screen.getByLabelText(/pms/i), 'opera')
  }
  await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
  await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
  await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
}

describe('SignupPage — success + handoff', () => {
  it('supported PMS: confirms the workspace is ready and hands off with login_hint', async () => {
    await completeTo(true)
    expect(await screen.findByText(/your workspace.*is ready/i)).toBeInTheDocument()
    // Supported-only clause — distinguishes this branch from the unsupported copy.
    expect(screen.getByText(/property is set up/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /go to your workspace/i }))
    expect(login).toHaveBeenCalledWith('owner@hotel.test')
  })

  it('unsupported PMS: says the PMS will be enabled later', async () => {
    await completeTo(false, 'SkyTouch')
    expect(await screen.findByText(/don'?t support skytouch yet/i)).toBeInTheDocument()
    // The handoff CTA fires on the unsupported branch too.
    await userEvent.click(screen.getByRole('button', { name: /go to your workspace/i }))
    expect(login).toHaveBeenCalledWith('owner@hotel.test')
  })
})

// Fill the details step with valid values, applying per-field overrides so a
// single test can drive exactly ONE constraint invalid. Assumes toDetails() ran.
// An empty-string override leaves that field blank (no typing). PMS is selected
// by role — /pms/i is ambiguous once "Other" reveals the "Which PMS" field.
async function fillValidDetails(
  opts: {
    otp?: string
    workspaceName?: string
    alias?: string
    propertyName?: string
    pms?: 'opera' | 'other'
    pmsOther?: string
    password?: string
  } = {},
) {
  const {
    otp = '123456',
    workspaceName = 'Sunset Group',
    propertyName = 'Sunset Inn',
    pms = 'opera',
    pmsOther,
    password = 'passw0rd1',
    alias,
  } = opts
  if (otp) await userEvent.type(screen.getByLabelText(/verification code/i), otp)
  if (workspaceName)
    await userEvent.type(screen.getByLabelText(/workspace name/i), workspaceName)
  if (alias !== undefined) {
    const url = screen.getByLabelText(/workspace url/i)
    await userEvent.clear(url)
    if (alias) await userEvent.type(url, alias)
  }
  if (propertyName)
    await userEvent.type(screen.getByLabelText(/property name/i), propertyName)
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'PMS' }), pms)
  if (pms === 'other' && pmsOther)
    await userEvent.type(screen.getByLabelText(/which pms/i), pmsOther)
  await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
  if (password) await userEvent.type(screen.getByLabelText(/password/i), password)
}

describe('SignupPage — client-side validation blocks the submit', () => {
  const submit = () =>
    userEvent.click(screen.getByRole('button', { name: /create workspace/i }))

  it('flags a password shorter than 8 characters and never calls the API', async () => {
    await toDetails()
    await fillValidDetails({ password: 'short1' })
    await submit()
    expect(await screen.findByText(/at least 8 characters/i)).toBeInTheDocument()
    expect(completeSignup).not.toHaveBeenCalled()
    // Stays on the details step (the code field is still present).
    expect(screen.getByLabelText(/verification code/i)).toBeInTheDocument()
  })

  it('flags an empty verification code', async () => {
    await toDetails()
    await fillValidDetails({ otp: '' })
    await submit()
    expect(await screen.findByText(/enter the verification code/i)).toBeInTheDocument()
    expect(completeSignup).not.toHaveBeenCalled()
  })

  it('flags an empty workspace name', async () => {
    await toDetails()
    await fillValidDetails({ workspaceName: '' })
    await submit()
    expect(await screen.findByText(/workspace name is required/i)).toBeInTheDocument()
    expect(completeSignup).not.toHaveBeenCalled()
  })

  it('flags a workspace URL with invalid characters', async () => {
    await toDetails()
    await fillValidDetails({ alias: 'Bad_Alias' })
    await submit()
    expect(await screen.findByText(/workspace url can only use/i)).toBeInTheDocument()
    expect(completeSignup).not.toHaveBeenCalled()
  })

  it('flags an empty property name', async () => {
    await toDetails()
    await fillValidDetails({ propertyName: '' })
    await submit()
    expect(await screen.findByText(/property name is required/i)).toBeInTheDocument()
    expect(completeSignup).not.toHaveBeenCalled()
  })

  it('flags a blank PMS name when "Other" is chosen', async () => {
    await toDetails()
    await fillValidDetails({ pms: 'other' })
    await submit()
    expect(await screen.findByText(/enter the name of your pms/i)).toBeInTheDocument()
    expect(completeSignup).not.toHaveBeenCalled()
  })
})

describe('SignupPage — server 422 is not the opaque error', () => {
  it('maps a 422 to a check-your-entries message, not "Something didn’t go through"', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(completeSignup).mockRejectedValue(new SignupError(422))
    await toDetails()
    await fillValidDetails() // all valid → clears client checks, reaches the server
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/some details weren.t accepted/i)).toBeInTheDocument()
    expect(screen.queryByText(/something didn.t go through/i)).not.toBeInTheDocument()
  })
})

describe('SignupPage — where the code actually goes', () => {
  it('tells the owner the code is emailed, not texted', async () => {
    // The step collects a mobile number but the OTP goes to the invited email
    // (there is no SMS vendor, and a caller-supplied number is not a channel we
    // can trust). A page that asks for a phone and then says "we sent your
    // code" points someone at the wrong device.
    vi.mocked(getInvite).mockResolvedValue('owner@hotel.test')
    renderSignup()
    expect(await screen.findByText(/email your verification code to owner@hotel\.test/i))
      .toBeInTheDocument()
    expect(screen.getByText(/won’t text you a code|won't text you a code/i)).toBeInTheDocument()
  })
})

describe('SignupPage — a server fault is not the owner’s fault', () => {
  // Three distinct backend faults reached a real owner wearing the identical
  // "Check your details and try again" copy: a Keycloak admin read timeout, a
  // duplicate-key collision on the organization id sequence, and a Keycloak
  // 409 on a duplicate email. The details were correct every time, and the
  // message sent them hunting a problem that was never in the form.
  it('maps a 500 to an our-end message that does not blame the details', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(completeSignup).mockRejectedValue(new SignupError(500))
    await toDetails()
    await fillValidDetails()
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/went wrong on our end/i)).toBeInTheDocument()
    expect(screen.getByText(/no workspace was created/i)).toBeInTheDocument()
    expect(screen.queryByText(/check your details/i)).not.toBeInTheDocument()
  })

  it('distinguishes a dead invite (404) from a server fault', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(completeSignup).mockRejectedValue(new SignupError(404))
    await toDetails()
    await fillValidDetails()
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/already been used or has expired/i)).toBeInTheDocument()
    // A fresh code cannot revive a consumed invite, so it must NOT be offered.
    expect(screen.queryByRole('button', { name: /send me a new code/i })).not.toBeInTheDocument()
  })
})

describe('SignupPage — a burned code is replaceable in place', () => {
  // `otp.verify` DELETES the challenge the moment it matches, so any refusal
  // raised after that leaves the owner holding a spent code. Resubmitting the
  // same form can then only ever return 403, however correct their details.
  it('offers a new code after a server fault and keeps the typed details', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(completeSignup).mockRejectedValue(new SignupError(500))
    await toDetails()
    await fillValidDetails({ workspaceName: 'Harbour View' })
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))

    const resend = screen.getByRole('button', { name: /send me a new code/i })
    vi.mocked(requestOtp).mockClear()
    await userEvent.click(resend)
    await waitFor(() => expect(requestOtp).toHaveBeenCalledTimes(1))

    // Still on the details step with the work intact — the whole point of
    // resending in place rather than returning to the first step.
    expect(screen.getByDisplayValue('Harbour View')).toBeInTheDocument()
    expect(screen.getByText(/new code is on its way/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send me a new code/i })).not.toBeInTheDocument()
  })

  it('does not offer a new code on a 403 — that code is still live', async () => {
    const { SignupError } = await import('../api/signup')
    vi.mocked(completeSignup).mockRejectedValue(new SignupError(403))
    await toDetails()
    await fillValidDetails()
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/code is incorrect or expired/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send me a new code/i })).not.toBeInTheDocument()
  })
})
