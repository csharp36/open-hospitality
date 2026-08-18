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
    await userEvent.type(screen.getByLabelText(/which pms/i), 'SkyTouch')
    await userEvent.selectOptions(screen.getByLabelText(/jurisdiction/i), 'US-CA')
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1')
    await userEvent.click(screen.getByRole('button', { name: /create workspace/i }))
    await waitFor(() => expect(completeSignup).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(completeSignup).mock.calls[0]![0]
    expect(payload).toMatchObject({ pms_source: 'other', pms_other_name: 'SkyTouch' })
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
