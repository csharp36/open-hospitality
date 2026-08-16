// PreviewResult unit tests: renders a successful `ok` preview payload — the
// "nothing saved" banner, an honest coverage line (never "ties out" — that
// signal isn't built yet), the USALI P&L lines, and a DEFERRED early-access
// CTA that must read as coming-soon, not a live action.

import { render, screen, within } from '@testing-library/react'
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
  net_total: '0.00',
}

describe('PreviewResult', () => {
  it('renders the source, P&L lines, honest coverage, nothing-saved banner, and a coming-soon CTA', () => {
    render(<PreviewResult payload={payload} />)

    const region = screen.getByRole('region', { name: 'Preview result' })

    expect(within(region).getByText(/OPERA · trial_balance/)).toBeInTheDocument()

    expect(within(region).getByText(/Room Revenue/)).toBeInTheDocument()
    expect(within(region).getByText(/Visa/)).toBeInTheDocument()
    expect(within(region).getByText('5487.00')).toBeInTheDocument()
    expect(within(region).getByText('-5487.00')).toBeInTheDocument()

    expect(within(region).getByText(/3 of 4 mapped/)).toBeInTheDocument()
    expect(within(region).getByText(/2 need review/)).toBeInTheDocument()

    expect(within(region).getByText(/nothing saved/i)).toBeInTheDocument()

    // Must never claim a "ties out" signal — that reconciliation isn't built yet.
    expect(within(region).queryByText(/ties out/i)).not.toBeInTheDocument()

    const cta = within(region).getByRole('button', { name: /early access/i })
    expect(cta).toBeInTheDocument()
    expect(cta).toHaveAttribute('aria-disabled', 'true')
  })
})
