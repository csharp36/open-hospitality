import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'
import { createAppRouter } from '../router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getPropertyConfig: vi.fn(),
  setInventory: vi.fn(),
  addOoo: vi.fn(),
  removeOoo: vi.fn(),
  setFiscalCalendar: vi.fn(),
  getFiscalPeriods: vi.fn(),
  getProperties: vi.fn(),
  getMe: vi.fn(),
  getIntakeAddress: vi.fn(),
  createIntakeAddress: vi.fn(),
  rotateIntakeAddress: vi.fn(),
  setIntakeSenderDomains: vi.fn(),
  getIntakeEvents: vi.fn(),
}))
import {
  ApiError, createIntakeAddress, getFiscalPeriods, getIntakeAddress, getIntakeEvents, getMe,
  getProperties, getPropertyConfig, rotateIntakeAddress, setIntakeSenderDomains,
} from '../api/client'
import type { IntakeAddress, IntakeEvent } from '../api/types'

const NO_ADDRESS: IntakeAddress = {
  address: null, local_part: null, created_at: null, sender_domains: null,
}
const ADDRESS: IntakeAddress = {
  address: 'na-abc@intake.example.test',
  local_part: 'na-abc',
  created_at: '2026-07-07T04:00:00Z',
  sender_domains: ['pms.test'],
}
const EVENTS: IntakeEvent[] = [
  {
    event_id: 2,
    received_at: '2026-07-08T04:05:00Z',
    envelope_from: 'newest@pms.test',
    subject: 'Night audit',
    outcome: 'no_attachment',
    message_id: '<two@pms.test>',
    attachments: [],
  },
  {
    event_id: 1,
    received_at: '2026-07-07T04:00:00Z',
    envelope_from: 'oldest@pms.test',
    subject: 'Night audit',
    outcome: 'ingested',
    message_id: '<one@pms.test>',
    attachments: [{ name: 'f.pdf', sha256: 'aa', bytes: 3, outcome: 'ingested', batch_id: 7 }],
  },
]

/**
 * Let anything already queued run before a "was not called" assertion.
 *
 * react-query's `mutate` fires its mutationFn off the synchronous path, so an
 * assertion made in the same tick as the click passes whether or not the
 * mutation was started — the "did not rotate / did not save" cases below are
 * vacuous without this.
 */
async function settle() {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
}

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/property-config'] }))
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
  // No `clearMocks` in vite.config.ts, so call counts would carry across tests
  // in this file — the rotate case below counts getIntakeAddress calls.
  vi.clearAllMocks()
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
  vi.mocked(getProperties).mockResolvedValue([
    { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2026-07-07', last_date: '2026-07-07', name: null },
  ])
  vi.mocked(getPropertyConfig).mockResolvedValue({
    property_id: 'HISJ', inventory: [], out_of_order: [], fiscal_calendar: null,
  })
  vi.mocked(getFiscalPeriods).mockResolvedValue({ periods: [] })
  vi.mocked(getIntakeAddress).mockResolvedValue(NO_ADDRESS)
  vi.mocked(getIntakeEvents).mockResolvedValue({ events: [] })
})

describe('PropertyConfigPage', () => {
  it('shows the week-start field only when 4-4-5 is chosen', async () => {
    renderPage()
    // default calendar_month -> no week-start select
    await screen.findByLabelText(/4-4-5/i)
    expect(screen.queryByLabelText(/week start/i)).toBeNull()
    fireEvent.click(screen.getByLabelText(/4-4-5/i))
    expect(screen.getByLabelText(/week start/i)).toBeInTheDocument()
  })

  it('offers all seven OOO reason codes including the DNR reasons', async () => {
    renderPage()
    const select = await screen.findByLabelText(/reason/i)
    const options = select.querySelectorAll('option')
    expect(options).toHaveLength(7)
    const values = Array.from(options).map((o) => (o as HTMLOptionElement).value)
    expect(values).toContain('do_not_rent')
    expect(values).toContain('owner_occupied')
  })

  it('shows the count in force today, not a future-dated row, as "current"', async () => {
    // Newest row is dated far in the future (a planned renovation); the count
    // in force today is the older 140-room row. inventory arrives newest-first.
    vi.mocked(getPropertyConfig).mockResolvedValue({
      property_id: 'HISJ',
      inventory: [
        { inventory_id: 2, effective_date: '2999-01-01', total_rooms: 99 },
        { inventory_id: 1, effective_date: '2020-01-01', total_rooms: 140 },
      ],
      out_of_order: [],
      fiscal_calendar: null,
    })
    renderPage()
    // "Current count" reflects the in-force 140, never the future 99.
    const current = await screen.findByText(/current count/i)
    expect(current).toHaveTextContent('140')
    expect(current).not.toHaveTextContent('99')
  })

  it('labels a future-only inventory as not yet in force', async () => {
    vi.mocked(getPropertyConfig).mockResolvedValue({
      property_id: 'HISJ',
      inventory: [{ inventory_id: 1, effective_date: '2999-01-01', total_rooms: 99 }],
      out_of_order: [],
      fiscal_calendar: null,
    })
    renderPage()
    await screen.findByText(/no count in force yet/i)
    expect(screen.queryByText(/current count/i)).toBeNull()
  })
})

describe('PropertyConfigPage night-audit email section', () => {
  it('offers "Create address" and no address field when the property has none', async () => {
    renderPage()
    expect(await screen.findByRole('button', { name: 'Create address' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Night-audit email address')).toBeNull()
  })

  it('shows the address and the event log newest first', async () => {
    vi.mocked(getIntakeAddress).mockResolvedValue(ADDRESS)
    vi.mocked(getIntakeEvents).mockResolvedValue({ events: EVENTS })
    renderPage()

    const field = await screen.findByLabelText('Night-audit email address')
    expect(field).toHaveValue('na-abc@intake.example.test')
    expect(field).toHaveAttribute('readonly')
    expect(screen.queryByRole('button', { name: 'Create address' })).toBeNull()

    const rows = screen.getAllByRole('row').filter((r) => r.closest('table')?.getAttribute('aria-label') === 'Night-audit intake events')
    // [0] is the header; the newest message leads the body.
    expect(rows[1]).toHaveTextContent('newest@pms.test')
    expect(rows[1]).toHaveTextContent('no attachment')
    expect(rows[2]).toHaveTextContent('oldest@pms.test')
    expect(rows[2]).toHaveTextContent('ingested')
  })

  it('creates the address on demand', async () => {
    vi.mocked(getIntakeAddress).mockResolvedValueOnce(NO_ADDRESS).mockResolvedValue(ADDRESS)
    vi.mocked(createIntakeAddress).mockResolvedValue(ADDRESS)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Create address' }))
    await waitFor(() => { expect(createIntakeAddress).toHaveBeenCalledWith('HISJ') })
    expect(await screen.findByLabelText('Night-audit email address'))
      .toHaveValue('na-abc@intake.example.test')
  })

  it('rotates only after a second, confirming click, then refetches the address', async () => {
    const rotated: IntakeAddress = {
      address: 'na-new@intake.example.test', local_part: 'na-new',
      created_at: '2026-07-09T04:00:00Z', sender_domains: null,
    }
    vi.mocked(getIntakeAddress).mockResolvedValueOnce(ADDRESS).mockResolvedValue(rotated)
    vi.mocked(rotateIntakeAddress).mockResolvedValue(rotated)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Rotate address' }))
    await settle()
    expect(rotateIntakeAddress).not.toHaveBeenCalled()
    expect(getIntakeAddress).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Confirm rotation' }))
    await waitFor(() => { expect(rotateIntakeAddress).toHaveBeenCalledWith('HISJ') })
    await waitFor(() => { expect(vi.mocked(getIntakeAddress).mock.calls.length).toBeGreaterThan(1) })
    expect(await screen.findByLabelText('Night-audit email address'))
      .toHaveValue('na-new@intake.example.test')
  })

  it('cancels a pending rotation', async () => {
    vi.mocked(getIntakeAddress).mockResolvedValue(ADDRESS)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Rotate address' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await settle()
    expect(screen.queryByRole('button', { name: 'Confirm rotation' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Rotate address' })).toBeInTheDocument()
    expect(rotateIntakeAddress).not.toHaveBeenCalled()
  })

  it('saves the sender domains on blur, trimmed and as typed', async () => {
    vi.mocked(getIntakeAddress).mockResolvedValue(ADDRESS)
    vi.mocked(setIntakeSenderDomains).mockResolvedValue({ ...ADDRESS, sender_domains: ['pms.test', 'other.test'] })
    renderPage()

    const field = await screen.findByLabelText('Allowed sender domains')
    expect(field).toHaveValue('pms.test')
    fireEvent.change(field, { target: { value: 'PMS.test, other.test, ' } })
    fireEvent.blur(field)
    await waitFor(() => {
      expect(setIntakeSenderDomains).toHaveBeenCalledWith('HISJ', ['PMS.test', 'other.test'])
    })
  })

  it('does not write when a blur leaves the sender domains unchanged', async () => {
    vi.mocked(getIntakeAddress).mockResolvedValue(ADDRESS)
    renderPage()

    const field = await screen.findByLabelText('Allowed sender domains')
    fireEvent.blur(field)
    await settle()
    expect(setIntakeSenderDomains).not.toHaveBeenCalled()
    // Case alone is not a change either: the allowlist is stored lowercased.
    fireEvent.change(field, { target: { value: ' PMS.test ' } })
    fireEvent.blur(field)
    await settle()
    expect(setIntakeSenderDomains).not.toHaveBeenCalled()
  })

  it('shows a 422 refusal beside the sender-domain field', async () => {
    vi.mocked(getIntakeAddress).mockResolvedValue(ADDRESS)
    vi.mocked(setIntakeSenderDomains).mockRejectedValue(
      new ApiError(422, "not a sender domain: 'nope'", "not a sender domain: 'nope'"),
    )
    renderPage()

    const field = await screen.findByLabelText('Allowed sender domains')
    fireEvent.change(field, { target: { value: 'nope' } })
    fireEvent.blur(field)
    expect(await screen.findByText(/not a sender domain: 'nope'/)).toBeInTheDocument()
  })

  it('copies the address to the clipboard', async () => {
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    vi.mocked(getIntakeAddress).mockResolvedValue(ADDRESS)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith('na-abc@intake.example.test')
    expect(await screen.findByText('Copied')).toBeInTheDocument()
  })

  it('renders an empty event log as "No messages yet"', async () => {
    vi.mocked(getIntakeAddress).mockResolvedValue(ADDRESS)
    renderPage()
    expect(await screen.findByText('No messages yet.')).toBeInTheDocument()
  })
})
