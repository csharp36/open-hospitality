// PreviewResult unit tests: the "nothing saved" banner, an honest coverage
// sentence (never "ties out" — that signal isn't built yet), the P&L lines, a
// real disclosure behind "Not what you expected?", and a LIVE setup-link CTA.

import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { PreviewPayload } from '../../api/types'
import PreviewResult from './PreviewResult'

const payload: PreviewPayload = {
  pms_source: 'OPERA',
  report_type: 'trial_balance',
  business_date: '2026-08-13',
  pnl_lines: [
    { major: 'Operated Departments', sub: 'Rooms', line_item: 'Room Revenue', amount: '5487.00' },
    { major: 'Settlements', sub: 'Credit Card', line_item: 'Visa', amount: '-5487.00' },
  ],
  kpis: [],
  codes_recognized: 4,
  codes_mapped: 3,
  codes_needs_review: 2,
}

describe('PreviewResult', () => {
  it('renders the P&L lines and the nothing-saved banner', () => {
    render(<PreviewResult payload={payload} />)
    const region = screen.getByRole('region', { name: 'Preview result' })

    expect(within(region).getByText(/Room Revenue/)).toBeInTheDocument()
    expect(within(region).getByText(/Visa/)).toBeInTheDocument()
    expect(within(region).getByText('5,487.00')).toBeInTheDocument()
    // A negative reads as it does on the report the visitor just dropped.
    expect(within(region).getByText('(5,487.00)')).toBeInTheDocument()

    expect(within(region).getByText(/nothing saved/i)).toBeInTheDocument()
    // Must never claim a "ties out" signal — that reconciliation isn't built yet.
    expect(within(region).queryByText(/ties out/i)).not.toBeInTheDocument()
  })

  it('names the source and report in words an owner would use, not pipeline identifiers', () => {
    render(<PreviewResult payload={payload} />)
    const region = screen.getByRole('region', { name: 'Preview result' })
    expect(within(region).getByText(/Opera trial balance/)).toBeInTheDocument()
    expect(within(region).queryByText(/OPERA · trial_balance/)).not.toBeInTheDocument()
  })

  it('says how many codes still need a person, not just a ratio', () => {
    render(<PreviewResult payload={payload} />)
    const region = screen.getByRole('region', { name: 'Preview result' })
    expect(within(region).getByText(/3 of your 4 charge codes/)).toBeInTheDocument()
    expect(within(region).getByText(/2 still need a human/)).toBeInTheDocument()
  })

  it('does not invent a needs-review warning when everything mapped', () => {
    render(
      <PreviewResult
        payload={{ ...payload, codes_mapped: 4, codes_needs_review: 0 }}
      />,
    )
    const region = screen.getByRole('region', { name: 'Preview result' })
    expect(within(region).getByText(/placed all 4 charge codes/)).toBeInTheDocument()
    expect(within(region).queryByText(/need a human/)).not.toBeInTheDocument()
  })

  it('"Not what you expected?" actually discloses something', () => {
    // It shipped as a <button> with no onClick at all — an affordance that
    // looked live and did nothing, on the page we send to strangers.
    render(<PreviewResult payload={payload} />)
    const region = screen.getByRole('region', { name: 'Preview result' })

    const toggle = within(region).getByRole('button', { name: /not what you expected/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within(region).queryByText(/report’s own title/)).not.toBeInTheDocument()

    fireEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(within(region).getByText(/report’s own title/)).toBeInTheDocument()
  })

  it('offers a LIVE setup-link CTA, never a disabled coming-soon button', () => {
    render(<PreviewResult payload={payload} />)
    const region = screen.getByRole('region', { name: 'Preview result' })

    const cta = within(region).getByRole('button', { name: /setup link/i })
    expect(cta).toBeInTheDocument()
    expect(within(region).getByLabelText('Email address')).toBeInTheDocument()
    // The CTA is only disabled for want of an address, which is not the same as
    // "coming soon".
    expect(within(region).queryByText(/coming soon/i)).not.toBeInTheDocument()
  })
})
