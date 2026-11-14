import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import type { Timecard } from '../api/types'
import { toAwareIso } from '../lib/timecardAdjustment'
import TimecardAdjustmentForm from './TimecardAdjustmentForm'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  addTimecardAdjustment: vi.fn(),
}))
import { addTimecardAdjustment } from '../api/client'

const CARD: Timecard = {
  timecard_id: 1, employee_id: 7, employee_name: 'Hank H',
  period_start: '2026-07-06', period_end: '2026-07-19', status: 'open', total_minutes: 480,
  days: [{ business_date: '2026-07-07', worked_minutes: 480, warnings: [], punches: [] }],
}

function renderForm(card: Timecard = CARD) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onSaved = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <TimecardAdjustmentForm timecard={card} onSaved={onSaved} />
    </QueryClientProvider>,
  )
  return { onSaved }
}

beforeEach(() => {
  vi.mocked(addTimecardAdjustment).mockResolvedValue({ ...CARD, total_minutes: 540 })
})

describe('TimecardAdjustmentForm', () => {
  it('posts an AWARE instant for the chosen day and time, with the reason, and hands back the card', async () => {
    const { onSaved } = renderForm()
    const form = screen.getByRole('region', { name: 'add a correction' })
    fireEvent.change(within(form).getByLabelText('Day'), { target: { value: '2026-07-07' } })
    await userEvent.selectOptions(within(form).getByLabelText('Punch'), 'clock_out')
    fireEvent.change(within(form).getByLabelText('Time'), { target: { value: '17:00' } })
    await userEvent.type(within(form).getByLabelText('Reason'), 'forgot to clock out')
    await userEvent.click(within(form).getByRole('button', { name: 'Add correction' }))

    await waitFor(() => expect(addTimecardAdjustment).toHaveBeenCalledTimes(1))
    const [id, body] = vi.mocked(addTimecardAdjustment).mock.calls[0]!
    expect(id).toBe(1)
    expect(body).toEqual({
      punch_type: 'clock_out',
      business_date: '2026-07-07',
      adjusted_at: toAwareIso('2026-07-07', '17:00'),
      reason: 'forgot to clock out',
    })
    // Aware: an ISO instant with an explicit offset, never a naive wall clock.
    expect(body.adjusted_at).toMatch(/Z$|[+-]\d{2}:\d{2}$/)
    expect(new Date(body.adjusted_at).getTime()).toBe(new Date('2026-07-07T17:00:00').getTime())
    // The updated card goes to the caller; the form never recomputes hours.
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ ...CARD, total_minutes: 540 }))
    expect(await within(form).findByRole('status')).toHaveTextContent('Added clock out at 17:00 on 2026-07-07.')
  })

  it('requires a reason and a time before calling the server', async () => {
    renderForm()
    const form = screen.getByRole('region', { name: 'add a correction' })
    await userEvent.click(within(form).getByRole('button', { name: 'Add correction' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Enter the time of the punch.')

    fireEvent.change(within(form).getByLabelText('Time'), { target: { value: '09:00' } })
    await userEvent.click(within(form).getByRole('button', { name: 'Add correction' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent(/needs a reason/)
    expect(addTimecardAdjustment).not.toHaveBeenCalled()
  })

  it('refuses a day outside the period client-side', async () => {
    renderForm()
    const form = screen.getByRole('region', { name: 'add a correction' })
    fireEvent.change(within(form).getByLabelText('Day'), { target: { value: '2026-07-25' } })
    fireEvent.change(within(form).getByLabelText('Time'), { target: { value: '09:00' } })
    await userEvent.type(within(form).getByLabelText('Reason'), 'x')
    await userEvent.click(within(form).getByRole('button', { name: 'Add correction' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('2026-07-06 to 2026-07-19')
    expect(addTimecardAdjustment).not.toHaveBeenCalled()
  })

  it('shows the server refusal verbatim, tied to the fields', async () => {
    vi.mocked(addTimecardAdjustment).mockRejectedValue(
      new ApiError(422, 'employee is placed at two properties on 2026-07-07; specify property_id'),
    )
    renderForm()
    const form = screen.getByRole('region', { name: 'add a correction' })
    fireEvent.change(within(form).getByLabelText('Time'), { target: { value: '09:00' } })
    await userEvent.type(within(form).getByLabelText('Reason'), 'late start approved')
    await userEvent.click(within(form).getByRole('button', { name: 'Add correction' }))
    const alert = await within(form).findByRole('alert')
    expect(alert).toHaveTextContent('two properties on 2026-07-07')
    expect(within(form).getByLabelText('Reason')).toHaveAttribute('aria-invalid', 'true')
  })

  it('offers no form on an approved card — the server would 409', () => {
    renderForm({ ...CARD, status: 'approved' })
    expect(screen.queryByRole('region', { name: 'add a correction' })).not.toBeInTheDocument()
    expect(screen.getByText(/approved and locked/)).toBeInTheDocument()
  })
})

describe('TimecardAdjustmentForm default day', () => {
  it('starts on the latest punched day, not the period start', () => {
    renderForm({ ...CARD, days: [
      { business_date: '2026-07-07', worked_minutes: 480, warnings: [], punches: [] },
      { business_date: '2026-07-09', worked_minutes: 240, warnings: [], punches: [] },
    ] })
    expect(screen.getByLabelText('Day')).toHaveValue('2026-07-09')
  })
  it('falls back to the period start for an empty card', () => {
    renderForm({ ...CARD, days: [] })
    expect(screen.getByLabelText('Day')).toHaveValue('2026-07-06')
  })
})
