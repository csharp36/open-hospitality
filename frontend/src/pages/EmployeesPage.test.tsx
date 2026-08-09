/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  Aes256Gcm,
  CipherSuite,
  DhkemP256HkdfSha256,
  HkdfSha256,
} from '@hpke/core'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT, makeEmployee } from '../test/fixtures'
import { createAppRouter } from '../router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getEmployees: vi.fn(),
  getEmployeeWork: vi.fn(),
  getDepartments: vi.fn(),
  onboardEmployee: vi.fn(),
  terminateEmployee: vi.fn(),
  updateEmployee: vi.fn(),
  setEmployeeSuspended: vi.fn(),
  getPayRate: vi.fn(),
  setPayRate: vi.fn(),
  getMe: vi.fn(),
  getProperties: vi.fn(),
  getPayrollPublicKey: vi.fn(),
  getPayrollProfile: vi.fn(),
  putPayrollProfile: vi.fn(),
  getDepositAccounts: vi.fn(),
  putDepositAccounts: vi.fn(),
  getFaceTemplate: vi.fn(),
  enrollFaceTemplate: vi.fn(),
  deleteFaceTemplate: vi.fn(),
}))
import {
  deleteFaceTemplate,
  enrollFaceTemplate,
  getDepartments,
  getDepositAccounts,
  getEmployeeWork,
  getEmployees,
  getFaceTemplate,
  getMe,
  getPayRate,
  getPayrollProfile,
  getPayrollPublicKey,
  getProperties,
  onboardEmployee,
  putDepositAccounts,
  putPayrollProfile,
  setEmployeeSuspended,
  setPayRate,
  terminateEmployee,
  updateEmployee,
} from '../api/client'

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/employees'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(getEmployees).mockResolvedValue([makeEmployee({ full_name: 'Gina M' })])
  vi.mocked(getEmployeeWork).mockResolvedValue([])
  vi.mocked(getDepartments).mockResolvedValue([
    { department_id: 3, property_id: 'HISJ', name: 'Housekeeping' },
  ])
  vi.mocked(getProperties).mockResolvedValue([
    { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2026-07-01', last_date: '2026-07-31', name: 'HOLIDAY INN & SUITES SAN JOSE' },
  ])
})

describe('EmployeesPage', () => {
  it('lists employees', async () => {
    renderPage()
    const table = await screen.findByRole('table', { name: 'Employees' })
    expect(within(table).getByText('Gina M')).toBeInTheDocument()
  })

  it('onboards from the form', async () => {
    vi.mocked(onboardEmployee).mockResolvedValue({
      employee_id: 2, keycloak_subject: 'kc-x', property_id: 'HISJ', full_name: 'New Op' })
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    // Onboarding lives in a modal now — open it first.
    await userEvent.click(screen.getByRole('button', { name: 'Add Employee' }))
    await userEvent.type(screen.getByLabelText('Full Name'), 'New Op')
    await userEvent.type(screen.getByLabelText('Email'), 'op@x.com')
    await userEvent.click(screen.getByRole('button', { name: 'Onboard' }))
    await waitFor(() =>
      expect(onboardEmployee).toHaveBeenCalledWith(
        expect.objectContaining({ full_name: 'New Op', email: 'op@x.com' }),
      ),
    )
  })

  it('shows hours and earned cost per person for the current month', async () => {
    vi.mocked(getEmployeeWork).mockResolvedValue([
      { employee_id: 1, hours: '124.00', ot_hours: '4.00', est_cost: '3120.0000' },
    ])
    renderPage()
    const table = await screen.findByRole('table', { name: 'Employees' })
    expect(await within(table).findByText('124 h')).toBeInTheDocument()
    expect(within(table).getByText('$3,120')).toBeInTheDocument()
  })

  // The server sends est_cost: null to anyone who may not see a pay rate,
  // because cost over hours IS the rate. Two things must NOT happen here: a
  // $0.00 anywhere (a withheld figure rendered as a real one), and a total the
  // page reconstitutes for itself out of the rows it was given.
  it('withholds earned cost without inventing a zero when the server sends null', async () => {
    vi.mocked(getEmployeeWork).mockResolvedValue([
      { employee_id: 1, hours: '124.00', ot_hours: '4.00', est_cost: null },
    ])
    renderPage()
    const table = await screen.findByRole('table', { name: 'Employees' })
    expect(await within(table).findByText('124 h')).toBeInTheDocument()
    expect(within(table).queryByText('$0')).not.toBeInTheDocument()
    expect(within(table).queryByText('$0.00')).not.toBeInTheDocument()

    // The KPI tile goes with it — a period-over-period CHANGE in labor cost is
    // the same money answered a slower way.
    expect(await screen.findByText('Requires payroll access')).toBeInTheDocument()
    expect(screen.queryByText(/^\$[\d,]/)).not.toBeInTheDocument()
  })

  // Month-to-date is compared against the SAME SPAN of last month, never the
  // whole of it: on the 10th, holding 10 days against 31 would print a fall
  // that only measures how early in the month it is.
  it('compares month-to-date against the same span of last month', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date(2026, 7, 10, 9, 0, 0)) // 10 August 2026
    try {
      vi.mocked(getEmployeeWork).mockImplementation(({ from, to }) => {
        if (from === '2026-08-01' && to === '2026-08-10')
          return Promise.resolve([
            { employee_id: 1, hours: '110.00', ot_hours: '6.00', est_cost: '2800.0000' },
          ])
        if (from === '2026-07-01' && to === '2026-07-10')
          return Promise.resolve([
            { employee_id: 1, hours: '100.00', ot_hours: '4.00', est_cost: '2500.0000' },
          ])
        if (from === '2026-07-01' && to === '2026-07-31')
          return Promise.resolve([
            { employee_id: 1, hours: '400.00', ot_hours: '20.00', est_cost: '10000.0000' },
          ])
        throw new Error(`unexpected window ${from}..${to}`)
      })
      renderPage()
      expect(await screen.findByText('Hours Worked \u00b7 August')).toBeInTheDocument()
      // The row is on the same window as the tile above it, which is the point:
      // with one employee the column and the KPI agree exactly.
      const table = await screen.findByRole('table', { name: 'Employees' })
      expect(await within(table).findByText('110 h')).toBeInTheDocument()
      // 110 against 100 is +10%, and the label names the span it is against.
      expect(await screen.findByText(/10\.0%/)).toBeInTheDocument()
      expect(
        screen.getByText('vs Jul 1\u201310 \u00b7 all July 400 h'),
      ).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  // The whole point of the inactive section: someone paused or let go must
  // leave the working roster rather than sit in it looking schedulable.
  it('splits suspended and terminated staff into their own list', async () => {
    vi.mocked(getEmployees).mockResolvedValue([
      makeEmployee({ employee_id: 1, full_name: 'Gina M' }),
      makeEmployee({ employee_id: 2, full_name: 'Paused Pat', employment_status: 'leave' }),
      makeEmployee({
        employee_id: 3, full_name: 'Gone Gary', employment_status: 'terminated',
      }),
    ])
    renderPage()
    const active = await screen.findByRole('table', { name: 'Employees' })
    expect(within(active).getByText('Gina M')).toBeInTheDocument()
    expect(within(active).queryByText('Paused Pat')).not.toBeInTheDocument()
    expect(within(active).queryByText('Gone Gary')).not.toBeInTheDocument()

    const inactive = screen.getByRole('table', { name: 'Inactive employees' })
    expect(within(inactive).getByText('Paused Pat')).toBeInTheDocument()
    expect(within(inactive).getByText('Gone Gary')).toBeInTheDocument()
    // Only a suspension is reversible; a termination offers no Reinstate.
    expect(screen.getByRole('button', { name: 'Reinstate Paused Pat' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reinstate Gone Gary' })).not.toBeInTheDocument()
  })

  it('suspends and reinstates from the row actions', async () => {
    vi.mocked(setEmployeeSuspended).mockResolvedValue({ employee_id: 1, status: 'leave' })
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    await userEvent.click(screen.getByRole('button', { name: 'Suspend Gina M' }))
    await waitFor(() => expect(setEmployeeSuspended).toHaveBeenCalledWith(1, true))
  })

  // Terminate disables a login and closes a placement, so an icon click alone
  // must not do it.
  it('asks for confirmation before terminating', async () => {
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    await userEvent.click(screen.getByRole('button', { name: 'Terminate Gina M' }))
    expect(terminateEmployee).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: 'Terminate Employee' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Terminate' }))
    await waitFor(() => expect(terminateEmployee).toHaveBeenCalledWith(1))
  })

  it('edits name and department through the modal', async () => {
    vi.mocked(updateEmployee).mockResolvedValue(makeEmployee({ full_name: 'Gina Marsh' }))
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    await userEvent.click(screen.getByRole('button', { name: 'Edit Gina M' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit Employee' })
    await userEvent.clear(within(dialog).getByLabelText('Full Name'))
    await userEvent.type(within(dialog).getByLabelText('Full Name'), 'Gina Marsh')
    await userEvent.selectOptions(within(dialog).getByLabelText('Department'), '3')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save Changes' }))
    await waitFor(() =>
      expect(updateEmployee).toHaveBeenCalledWith(1, {
        full_name: 'Gina Marsh',
        department_id: 3,
      }),
    )
  })

  // The rate gate is payroll_admin | org_admin | property_gm on the server; the
  // field must not look usable to a role whose save would 403.
  it('disables the pay rate field for a role that cannot set pay', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['accountant'] })
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    await userEvent.click(screen.getByRole('button', { name: 'Add Employee' }))
    expect(screen.getByLabelText('Pay Rate')).toBeDisabled()
    expect(setPayRate).not.toHaveBeenCalled()
  })

  // A GM hires, so a GM has to be able to say what the hire is paid.
  it('lets a property_gm set the pay rate', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    await userEvent.click(screen.getByRole('button', { name: 'Add Employee' }))
    expect(screen.getByLabelText('Pay Rate')).toBeEnabled()
  })

  it('sets the pay rate on onboarding when the caller may set pay', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['payroll_admin'] })
    vi.mocked(getPayRate).mockResolvedValue({ employee_id: 2, pay_rate: null })
    vi.mocked(setPayRate).mockResolvedValue({ employee_id: 2, pay_rate: '24.50' })
    vi.mocked(onboardEmployee).mockResolvedValue({
      employee_id: 2, keycloak_subject: null, property_id: 'HISJ', full_name: 'New Op' })
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    await userEvent.click(screen.getByRole('button', { name: 'Add Employee' }))
    await userEvent.type(screen.getByLabelText('Full Name'), 'New Op')
    await userEvent.type(screen.getByLabelText('Pay Rate'), '24.50')
    await userEvent.click(screen.getByRole('button', { name: 'Onboard' }))
    // '24.5', not '24.50': a number input normalises the trailing zero away.
    // Harmless — the server takes a Decimal and 2dp is a maximum, not a shape.
    await waitFor(() => expect(setPayRate).toHaveBeenCalledWith(2, { pay_rate: '24.5' }))
  })
})

// --- Payroll PII sub-form: the security property ----------------------------
// The whole point of C1 on the client: the raw SSN is sealed in the browser and
// must appear NOWHERE in the outgoing request. This test runs REAL @hpke/core
// (unmocked) against the committed interop keypair, so it also proves the
// envelope actually decrypts to the SSN under the reconstructed aad.

const here = dirname(fileURLToPath(import.meta.url))
const fixture = JSON.parse(
  readFileSync(join(here, '..', '..', '..', 'tests', 'fixtures', 'hpke_interop.json'), 'utf8'),
) as { private_key_raw_b64: string; public_key_sec1_b64: string }

const SUITE_LABEL = 'DHKEM-P256-HKDF-SHA256/HKDF-SHA256/AES-256-GCM'
const RAW_SSN = '123-45-6789'

const b64ToBytes = (b64: string): Uint8Array =>
  Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
const b64ToArrayBuffer = (b64: string): ArrayBuffer => {
  const bytes = b64ToBytes(b64)
  const buf = new ArrayBuffer(bytes.byteLength)
  new Uint8Array(buf).set(bytes)
  return buf
}
const utf8 = (s: string): Uint8Array => new TextEncoder().encode(s)

/** Open a sealed envelope with the fixture private key — the server's job in
 * C2, run here only to prove the client produced a genuine seal. */
async function openWithFixtureKey(envelopeJson: string, aad: string): Promise<string> {
  const env = JSON.parse(envelopeJson) as { enc: string; ct: string }
  const suite = new CipherSuite({
    kem: new DhkemP256HkdfSha256(),
    kdf: new HkdfSha256(),
    aead: new Aes256Gcm(),
  })
  const recipientKey = await suite.kem.importKey(
    'raw',
    b64ToArrayBuffer(fixture.private_key_raw_b64),
    false,
  )
  const recip = await suite.createRecipientContext({
    recipientKey,
    enc: b64ToBytes(env.enc),
    info: utf8('usali-payroll-pii/v1'),
  })
  const pt = await recip.open(b64ToBytes(env.ct), utf8(aad))
  return new TextDecoder().decode(pt)
}

describe('EmployeesPage payroll sub-form', () => {
  // The deposit destination left this surface in E5 — the chain has its own
  // endpoint, mocked empty here.
  const notOnFile = {
    employee_id: 1, ssn_on_file: false, tax_elections_on_file: false,
  }

  beforeEach(() => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['payroll_admin'] })
    vi.mocked(getPayrollPublicKey).mockResolvedValue({
      key_id: 'interop', suite: SUITE_LABEL, public_key: fixture.public_key_sec1_b64,
    })
    vi.mocked(getPayrollProfile).mockResolvedValue(notOnFile)
    vi.mocked(putPayrollProfile).mockResolvedValue({ ...notOnFile, ssn_on_file: true })
    vi.mocked(getDepositAccounts).mockResolvedValue({ employee_id: 1, accounts: [] })
    vi.mocked(putDepositAccounts).mockResolvedValue({ employee_id: 1, accounts: [] })
  })

  it('seals every deposit-account field under its per-ordinal slot aad', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Payroll for Gina M' }))
    const region = await screen.findByRole('region', { name: 'deposit accounts' })

    await userEvent.type(within(region).getByLabelText('Bank Account 1'), '000123456')
    await userEvent.type(within(region).getByLabelText('Bank Routing 1'), '021000021')
    await userEvent.click(within(region).getByRole('button', { name: 'Replace Chain' }))

    await waitFor(() => expect(putDepositAccounts).toHaveBeenCalledTimes(1))
    const [empId, body] = vi.mocked(putDepositAccounts).mock.calls[0]!
    expect(empId).toBe(1)
    // THE security property: no plaintext account/routing in the call args.
    const serialized = JSON.stringify([empId, body])
    expect(serialized.includes('000123456')).toBe(false)
    expect(serialized.includes('021000021')).toBe(false)
    // One remainder account, sealed under the slot the backend derives —
    // `${employeeId}:deposit:${ordinal}:{field}` — proven by round-tripping
    // with the interop fixture key under exactly that aad.
    const [acct] = body.accounts
    expect(acct!.allocation_type).toBe('remainder')
    expect(acct!.allocation_value).toBeNull()
    expect(await openWithFixtureKey(acct!.sealed_account, '1:deposit:1:account')).toBe('000123456')
    expect(await openWithFixtureKey(acct!.sealed_routing, '1:deposit:1:routing')).toBe('021000021')
  })

  it('seals the SSN client-side — the raw value never leaves the browser', async () => {
    // The status query returns not-on-file first, then on-file after the write.
    vi.mocked(getPayrollProfile)
      .mockResolvedValueOnce(notOnFile)
      .mockResolvedValue({ ...notOnFile, ssn_on_file: true })
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: 'Payroll for Gina M' }))
    const region = await screen.findByRole('region', { name: 'payroll profile' })
    // Nothing on file before we save.
    expect(within(region).queryByText('on file')).not.toBeInTheDocument()

    await userEvent.type(within(region).getByLabelText('SSN'), RAW_SSN)
    await userEvent.click(within(region).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(putPayrollProfile).toHaveBeenCalledTimes(1))
    const [empId, body] = vi.mocked(putPayrollProfile).mock.calls[0]!

    // THE security property: the plaintext SSN is nowhere in the call arguments.
    expect(JSON.stringify([empId, body]).includes(RAW_SSN)).toBe(false)

    // A REAL sealed envelope was sent: correct shape, 65-byte SEC1 enc point.
    expect(empId).toBe(1)
    const envelope = JSON.parse(body.ssn!) as {
      version: number; suite: string; key_id: string; enc: string; ct: string
    }
    expect(envelope.version).toBe(1)
    expect(envelope.suite).toBe(SUITE_LABEL)
    expect(envelope.key_id).toBe('interop')
    expect(b64ToBytes(envelope.enc).byteLength).toBe(65)
    expect(b64ToBytes(envelope.enc)[0]).toBe(0x04)
    expect(envelope.ct.length).toBeGreaterThan(0)
    // Only the field that was typed is sent (blind overwrite of provided fields).
    expect(body.tax_elections).toBeUndefined()

    // And it genuinely decrypts to the SSN under aad "1:ssn" (employee_id:field).
    expect(await openWithFixtureKey(body.ssn!, '1:ssn')).toBe(RAW_SSN)

    // The on-file badge reflects the status query after the write.
    expect(await within(region).findByText('on file')).toBeInTheDocument()
  })

  it('hides the Payroll button when the principal lacks the role', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['accountant'] })
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    expect(screen.queryByRole('button', { name: 'Payroll for Gina M' })).not.toBeInTheDocument()
  })

  it('hides the Payroll button from org_admin — the vault is payroll_admin-only', async () => {
    // The backend gate is require_payroll_admin only; showing the sub-form to
    // org_admin would just 403 on save, so the frontend gate must match exactly.
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    expect(screen.queryByRole('button', { name: 'Payroll for Gina M' })).not.toBeInTheDocument()
  })
})

// --- Face enrollment sub-form (Pillar F) -------------------------------------
// The Employees-page end of the photo-first kiosk: an onboarder captures a
// reference photo; the server embeds and keeps the template. The vector never
// crosses the wire — the UI only ever sees the enrollment STATUS.

function stubCamera() {
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [] }) },
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (
    cb: BlobCallback,
  ) {
    cb(new Blob([new Uint8Array([1])], { type: 'image/jpeg' }))
  } as never)
}

describe('EmployeesPage face enrollment', () => {
  it('captures from the camera and enrolls (onboarder role)', async () => {
    stubCamera()
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    vi.mocked(getFaceTemplate).mockResolvedValue({ employee_id: 1, enrolled: false })
    vi.mocked(enrollFaceTemplate).mockResolvedValue({
      employee_id: 1, model_version: 'buffalo_l-w600k_r50', notice_version: null,
    })
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Face for Gina M' }))
    const region = await screen.findByRole('region', { name: 'face enrollment' })
    expect(within(region).getByText('not enrolled')).toBeInTheDocument()
    await userEvent.click(within(region).getByRole('button', { name: 'Capture & Enroll' }))
    await waitFor(() =>
      expect(enrollFaceTemplate).toHaveBeenCalledWith(1, expect.any(Blob)),
    )
  })

  it('uploads a JPEG as the fallback when there is no camera', async () => {
    stubCamera()
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    vi.mocked(getFaceTemplate).mockResolvedValue({ employee_id: 1, enrolled: false })
    vi.mocked(enrollFaceTemplate).mockResolvedValue({
      employee_id: 1, model_version: 'buffalo_l-w600k_r50', notice_version: null,
    })
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Face for Gina M' }))
    const region = await screen.findByRole('region', { name: 'face enrollment' })
    const file = new File([new Uint8Array([0xff, 0xd8, 0xff])], 'me.jpg', { type: 'image/jpeg' })
    await userEvent.upload(within(region).getByLabelText('Upload Face Photo'), file)
    await waitFor(() =>
      expect(enrollFaceTemplate).toHaveBeenCalledWith(1, expect.any(Blob)),
    )
  })

  it('shows enrolled status and removes the template on request', async () => {
    stubCamera()
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    vi.mocked(getFaceTemplate).mockResolvedValue({
      employee_id: 1, enrolled: true, model_version: 'buffalo_l-w600k_r50',
      notice_version: null, created_at: '2026-07-22T10:00:00+00:00',
    })
    vi.mocked(deleteFaceTemplate).mockResolvedValue(undefined)
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Face for Gina M' }))
    const region = await screen.findByRole('region', { name: 'face enrollment' })
    expect(within(region).getByText('enrolled')).toBeInTheDocument()
    await userEvent.click(within(region).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(deleteFaceTemplate).toHaveBeenCalledWith(1))
  })

  it('hides the Face button from non-onboarder roles', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['accountant'] })
    renderPage()
    await screen.findByRole('table', { name: 'Employees' })
    expect(screen.queryByRole('button', { name: 'Face for Gina M' })).not.toBeInTheDocument()
  })
})
