import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import type { SickLeaveStatus } from '../api/types'
import SickLeavePanel from './SickLeavePanel'

vi.mock('../api/sickLeave', () => ({
  getSickLeave: vi.fn(),
  recordSickLeaveUsage: vi.fn(),
  voidSickLeaveUsage: vi.fn(),
  adjustSickLeave: vi.fn(),
}))
import {
  adjustSickLeave,
  getSickLeave,
  recordSickLeaveUsage,
  voidSickLeaveUsage,
} from '../api/sickLeave'

const STATUS: SickLeaveStatus = {
  employee_id: 7,
  balance_hours: '12.5000',
  usage_cap_hours: '40.0000',
  accrual_cap_hours: '80.0000',
  usage_cap_window: 'calendar_year',
  used_this_year_hours: '24.0000',
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onClose = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <SickLeavePanel employeeId={7} employeeName="Gina M" onClose={onClose} />
    </QueryClientProvider>,
  )
  return { onClose }
}

beforeEach(() => {
  vi.mocked(getSickLeave).mockResolvedValue(STATUS)
  vi.mocked(recordSickLeaveUsage).mockResolvedValue(STATUS)
  vi.mocked(voidSickLeaveUsage).mockResolvedValue(STATUS)
  vi.mocked(adjustSickLeave).mockResolvedValue(STATUS)
})

describe('SickLeavePanel', () => {
  it('shows the derived balance, this year’s use against the cap, and the window', async () => {
    renderPanel()
    const summary = await screen.findByRole('region', { name: 'sick leave balance' })
    expect(within(summary).getByText('12.50 h')).toBeInTheDocument()
    expect(within(summary).getByText('24.00 / 40.00 h')).toBeInTheDocument()
    expect(within(summary).getByText('80.00 h')).toBeInTheDocument()
    expect(within(summary).getByText('calendar year')).toBeInTheDocument()
    expect(within(summary).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '60')
    expect(within(summary).getByText('16.00 h of headroom')).toBeInTheDocument()
  })

  it('shows no cap and says why when the server returns null caps', async () => {
    vi.mocked(getSickLeave).mockResolvedValue({
      ...STATUS, usage_cap_hours: null, accrual_cap_hours: null, usage_cap_window: null,
    })
    renderPanel()
    const summary = await screen.findByRole('region', { name: 'sick leave balance' })
    expect(within(summary).getByText('24.00 h')).toBeInTheDocument()
    expect(within(summary).queryByRole('progressbar')).not.toBeInTheDocument()
    expect(within(summary).getByText(/no recorded day length/i)).toBeInTheDocument()
  })

  it('records usage with hours as a decimal STRING and refetches the balance', async () => {
    renderPanel()
    const form = await screen.findByRole('region', { name: 'record sick leave' })
    await userEvent.type(within(form).getByLabelText('Hours taken'), '8')
    fireEvent.change(within(form).getByLabelText('Date'), { target: { value: '2026-10-28' } })
    await userEvent.click(within(form).getByRole('button', { name: 'Record usage' }))

    await waitFor(() => expect(recordSickLeaveUsage).toHaveBeenCalledTimes(1))
    const [empId, body] = vi.mocked(recordSickLeaveUsage).mock.calls[0]!
    expect(empId).toBe(7)
    expect(body).toEqual({ hours: '8', on: '2026-10-28' })
    expect(typeof body.hours).toBe('string')
    // The balance is re-read from the server, never computed here.
    await waitFor(() => expect(getSickLeave).toHaveBeenCalledTimes(2))
    expect(await within(form).findByRole('status')).toHaveTextContent('Record usage: 8 h on 2026-10-28')
  })

  it('surfaces a server refusal next to the form that caused it, verbatim', async () => {
    vi.mocked(recordSickLeaveUsage).mockRejectedValue(
      new ApiError(422, 'usage would exceed the calendar-year cap of 40.00 hours'),
    )
    renderPanel()
    const form = await screen.findByRole('region', { name: 'record sick leave' })
    await userEvent.type(within(form).getByLabelText('Hours taken'), '20')
    await userEvent.click(within(form).getByRole('button', { name: 'Record usage' }))

    const alert = await within(form).findByRole('alert')
    expect(alert).toHaveTextContent('usage would exceed the calendar-year cap of 40.00 hours')
    expect(within(form).getByLabelText('Hours taken')).toHaveAttribute('aria-invalid', 'true')
    // The refusal belongs to this form only.
    const voidForm = screen.getByRole('region', { name: 'void a usage' })
    expect(within(voidForm).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('refuses a zero adjustment and malformed hours client-side, without calling the server', async () => {
    renderPanel()
    const form = await screen.findByRole('region', { name: 'adjust the balance' })
    await userEvent.type(within(form).getByLabelText('Hours (±)'), '0')
    await userEvent.click(within(form).getByRole('button', { name: 'Apply adjustment' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('An adjustment must be nonzero.')

    await userEvent.clear(within(form).getByLabelText('Hours (±)'))
    await userEvent.type(within(form).getByLabelText('Hours (±)'), '1.234')
    await userEvent.click(within(form).getByRole('button', { name: 'Apply adjustment' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent(/up to two decimals/)
    expect(adjustSickLeave).not.toHaveBeenCalled()
  })

  it('sends a signed adjustment with its note, and voids through the void endpoint', async () => {
    renderPanel()
    const adjust = await screen.findByRole('region', { name: 'adjust the balance' })
    await userEvent.type(within(adjust).getByLabelText('Hours (±)'), '-1.5')
    fireEvent.change(within(adjust).getByLabelText('Date'), { target: { value: '2026-10-01' } })
    await userEvent.type(within(adjust).getByLabelText(/Note/), 'opening balance correction')
    await userEvent.click(within(adjust).getByRole('button', { name: 'Apply adjustment' }))
    await waitFor(() => expect(adjustSickLeave).toHaveBeenCalledWith(7, {
      hours: '-1.5', on: '2026-10-01', note: 'opening balance correction',
    }))

    const voidForm = screen.getByRole('region', { name: 'void a usage' })
    await userEvent.type(within(voidForm).getByLabelText('Hours to void'), '4')
    fireEvent.change(within(voidForm).getByLabelText('Date'), { target: { value: '2026-10-20' } })
    await userEvent.click(within(voidForm).getByRole('button', { name: 'Void usage' }))
    await waitFor(() => expect(voidSickLeaveUsage).toHaveBeenCalledWith(7, { hours: '4', on: '2026-10-20' }))
    expect(recordSickLeaveUsage).not.toHaveBeenCalled()
  })
})
