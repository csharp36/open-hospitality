import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import type { PaySchedule } from '../api/types'
import PayScheduleCard from './PayScheduleCard'

vi.mock('../api/paySchedule', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/paySchedule')>()),
  getPaySchedule: vi.fn(),
  putPaySchedule: vi.fn(),
}))
import { getPaySchedule, putPaySchedule } from '../api/paySchedule'

const SCHEDULE: PaySchedule = {
  property_id: 'HISJ', frequency: 'biweekly', anchor: '2026-01-05', check_date_offset_days: 5,
}

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <PayScheduleCard propertyId="HISJ" />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-10-29T12:00:00Z') })
  vi.mocked(getPaySchedule).mockResolvedValue(SCHEDULE)
  vi.mocked(putPaySchedule).mockResolvedValue(SCHEDULE)
})

describe('PayScheduleCard', () => {
  it('shows the schedule with the current period and pay day derived from its anchor', async () => {
    renderCard()
    const region = await screen.findByRole('region', { name: 'pay schedule' })
    expect(await within(region).findByText('2026-01-05')).toBeInTheDocument()
    expect(within(region).getByText('Oct 26, 2026 – Nov 8, 2026')).toBeInTheDocument()
    expect(within(region).getByText('Nov 13, 2026')).toBeInTheDocument()
    expect(within(region).getByRole('button', { name: 'Save changes' })).toBeInTheDocument()
  })

  it('renders a first-run form when the server has no schedule (404 -> null)', async () => {
    vi.mocked(getPaySchedule).mockResolvedValue(null)
    renderCard()
    const region = await screen.findByRole('region', { name: 'pay schedule' })
    expect(await within(region).findByText('not set')).toBeInTheDocument()
    expect(within(region).getByText(/No pay schedule yet/)).toBeInTheDocument()
    expect(await within(region).findByRole('button', { name: 'Set schedule' })).toBeInTheDocument()
    expect(within(region).getByLabelText(/Check date/)).toHaveValue(5)
  })

  it('PUTs the anchor and offset and replaces the schedule from the response', async () => {
    vi.mocked(getPaySchedule).mockResolvedValue(null)
    vi.mocked(putPaySchedule).mockResolvedValue({ ...SCHEDULE, anchor: '2026-10-26', check_date_offset_days: 3 })
    renderCard()
    const region = await screen.findByRole('region', { name: 'pay schedule' })
    fireEvent.change((await within(region).findByLabelText(/Period anchor/)), { target: { value: '2026-10-26' } })
    await userEvent.clear(within(region).getByLabelText(/Check date/))
    await userEvent.type(within(region).getByLabelText(/Check date/), '3')
    await userEvent.click(await within(region).findByRole('button', { name: 'Set schedule' }))

    await waitFor(() => expect(putPaySchedule).toHaveBeenCalledWith('HISJ', {
      frequency: 'biweekly', anchor: '2026-10-26', check_date_offset_days: 3,
    }))
    // The saved schedule drives the summary; the "not set" badge is gone.
    expect(await within(region).findByText('2026-10-26')).toBeInTheDocument()
    expect(within(region).queryByText('not set')).not.toBeInTheDocument()
    expect(within(region).getByRole('status')).toHaveTextContent('pay day 3 days after each')
  })

  it('turns a misaligned-anchor refusal into the two nearest grid dates, and one click fixes it', async () => {
    vi.mocked(putPaySchedule)
      .mockRejectedValueOnce(new ApiError(422,
        'anchor 2026-10-29 does not align with the platform payroll grid (anchored at 2026-01-05, biweekly); per-property grids are future work'))
      .mockResolvedValue({ ...SCHEDULE, anchor: '2026-10-26' })
    renderCard()
    const region = await screen.findByRole('region', { name: 'pay schedule' })
    fireEvent.change((await within(region).findByLabelText(/Period anchor/)), { target: { value: '2026-10-29' } })
    await userEvent.click(await within(region).findByRole('button', { name: 'Save changes' }))

    const alert = await within(region).findByRole('alert')
    expect(alert).toHaveTextContent('does not align with the platform payroll grid')
    expect((await within(region).findByLabelText(/Period anchor/))).toHaveAttribute('aria-invalid', 'true')
    await userEvent.click(within(region).getByRole('button', { name: 'Use 2026-10-26' }))
    expect((await within(region).findByLabelText(/Period anchor/))).toHaveValue('2026-10-26')
    expect(within(region).queryByRole('alert')).not.toBeInTheDocument()

    await userEvent.click(within(region).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(putPaySchedule).toHaveBeenLastCalledWith('HISJ', expect.objectContaining({ anchor: '2026-10-26' })))
  })

  it('refuses an empty anchor and an out-of-range offset client-side', async () => {
    vi.mocked(getPaySchedule).mockResolvedValue(null)
    renderCard()
    const region = await screen.findByRole('region', { name: 'pay schedule' })
    await userEvent.click(await within(region).findByRole('button', { name: 'Set schedule' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent(/first day of a pay period/)
    fireEvent.change((await within(region).findByLabelText(/Period anchor/)), { target: { value: '2026-10-26' } })
    await userEvent.clear(within(region).getByLabelText(/Check date/))
    await userEvent.type(within(region).getByLabelText(/Check date/), '45')
    await userEvent.click(await within(region).findByRole('button', { name: 'Set schedule' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent(/0 to 30/)
    expect(putPaySchedule).not.toHaveBeenCalled()
  })
})
