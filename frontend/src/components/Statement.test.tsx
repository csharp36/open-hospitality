import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

import Statement from './Statement'
import { makeSosReport } from '../test/fixtures'
import type { LaborVariance } from '../api/types'

describe('Statement', () => {
  it('renders sections in SOS order with formatted amounts', () => {
    render(<Statement report={makeSosReport()} onLineClick={() => {}} />)

    expect(screen.getByText('OPERATED DEPARTMENTS')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Transient Rooms Revenue' })).toBeInTheDocument()
    expect(screen.getByText('Total Rooms')).toBeInTheDocument()

    expect(screen.getByText('MISCELLANEOUS INCOME')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Parking' })).toBeInTheDocument()
    // Parking line amount and Total Miscellaneous Income are both 410.00.
    expect(screen.getAllByText('410.00')).toHaveLength(2)

    expect(screen.getByText('TOTAL OPERATING REVENUE')).toBeInTheDocument()
    expect(screen.getByText('10,866.37')).toBeInTheDocument()

    expect(screen.getByText('TAXES COLLECTED (PASS-THROUGH)')).toBeInTheDocument()
    expect(screen.getByText('SETTLEMENTS')).toBeInTheDocument()
    expect(screen.getByText('STATISTICS')).toBeInTheDocument()
  })

  it('formats negative settlements with a minus sign', () => {
    render(<Statement report={makeSosReport()} onLineClick={() => {}} />)
    // Line amount and section total are both -16.20.
    expect(screen.getAllByText('-16.20').length).toBeGreaterThanOrEqual(2)
  })

  it('computes rooms segment split percentages from the string decimals', () => {
    render(<Statement report={makeSosReport()} onLineClick={() => {}} />)
    expect(screen.getByText('ROOMS SEGMENT SPLIT')).toBeInTheDocument()
    expect(screen.getByText('75.0%')).toBeInTheDocument()
    expect(screen.getByText('25.0%')).toBeInTheDocument()
  })

  it('renders n/a percentages when segment revenue totals zero', () => {
    const report = makeSosReport({
      rooms_segments: [
        { segment: 'Transient', rooms: '0', room_revenue: '0.0000' },
        { segment: 'Group', rooms: '0', room_revenue: '0.0000' },
      ],
    })
    render(<Statement report={report} onLineClick={() => {}} />)
    expect(screen.getAllByText('n/a')).toHaveLength(2)
  })

  it('hides the OTHER section when empty and shows it when populated', () => {
    const { unmount } = render(<Statement report={makeSosReport()} onLineClick={() => {}} />)
    expect(screen.queryByText('OTHER (UNSCHEDULED)')).not.toBeInTheDocument()
    unmount()

    const withOther = makeSosReport({
      other: [
        {
          major: 'Other',
          sub_category: 'Unscheduled',
          line_item: 'Mystery Charge',
          total: '12.3400',
        },
      ],
      other_total: '12.3400',
    })
    render(<Statement report={withOther} onLineClick={() => {}} />)
    expect(screen.getByText('OTHER (UNSCHEDULED)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Mystery Charge' })).toBeInTheDocument()
  })

  it('shows prior-year statistics columns only when a prior value exists', () => {
    const { unmount } = render(<Statement report={makeSosReport()} onLineClick={() => {}} />)
    expect(screen.getByText('DAY PY')).toBeInTheDocument()
    expect(screen.getByText('MTD PY')).toBeInTheDocument()
    expect(screen.getByText('YTD PY')).toBeInTheDocument()
    unmount()

    const noPriors = makeSosReport({
      statistics: [
        {
          metric_code: 'ROOMS_OCCUPIED',
          day: '40.0000',
          mtd: '250.0000',
          ytd: '1200.0000',
          day_prior: null,
          mtd_prior: null,
          ytd_prior: null,
        },
      ],
    })
    render(<Statement report={noPriors} onLineClick={() => {}} />)
    expect(screen.getByText('DAY')).toBeInTheDocument()
    expect(screen.queryByText('DAY PY')).not.toBeInTheDocument()
  })

  it('invokes onLineClick with the clicked SosLine', () => {
    const onLineClick = vi.fn()
    render(<Statement report={makeSosReport()} onLineClick={onLineClick} />)
    fireEvent.click(screen.getByRole('button', { name: 'Parking' }))
    expect(onLineClick).toHaveBeenCalledWith({
      major: 'Operating Revenue',
      sub_category: 'Miscellaneous Income',
      line_item: 'Parking',
      total: '410.0000',
    })
  })
})

// C3: one alerted two-employee department + one suppressed solo department,
// mirroring the backend payoff numbers (est 640 vs actual 704 = +10% alert).
const VARIANCE_BLOCK: LaborVariance = {
  lines: [
    {
      department: 'Housekeeping',
      est_cost: '640.00',
      actual_gross: '704.00',
      employer_burden: '70.40',
      variance: '64.00',
      hours_actual: '32.00',
      alert: true,
    },
    {
      department: 'Front Desk',
      est_cost: null,
      actual_gross: null,
      employer_burden: null,
      variance: null,
      hours_actual: '16.00',
      alert: false,
    },
  ],
  periods: ['2026-07-06..2026-07-19'],
  est_total: '640.00',
  actual_total: '704.00',
  variance_total: '64.00',
  burden_total: '70.40',
  alert: true,
  suppressed_departments: 1,
  unpriced_hours: '0',
}

describe('Statement labor variance (C3)', () => {
  it('renders the variance section with period labels, alert badges, and signed totals', () => {
    render(
      <Statement
        report={makeSosReport({ labor_variance: VARIANCE_BLOCK })}
        onLineClick={() => {}}
      />,
    )

    expect(
      screen.getByText('Schedule 14 — Payroll: actual vs estimate'),
    ).toBeInTheDocument()
    expect(screen.getByText('Pay periods: 2026-07-06..2026-07-19')).toBeInTheDocument()

    // The alert badge sits on the alerted department row, next to the signed variance.
    const hkRow = screen.getByText('Housekeeping').closest('tr')!
    expect(within(hkRow).getByText('alert')).toBeInTheDocument()
    expect(within(hkRow).getByText('+64.00')).toBeInTheDocument()
    expect(within(hkRow).getByText('640.00')).toBeInTheDocument()
    expect(within(hkRow).getByText('704.00')).toBeInTheDocument()
    expect(within(hkRow).getByText('70.40')).toBeInTheDocument()

    // Totals row: bold complement of the visible lines, block-level alert badge.
    const totalsRow = screen.getByText('Total actual vs estimate').closest('tr')!
    expect(within(totalsRow).getByText('+64.00')).toBeInTheDocument()
    expect(within(totalsRow).getByText('alert')).toBeInTheDocument()
    expect(within(totalsRow).getByText('640.00')).toBeInTheDocument()
    expect(within(totalsRow).getByText('704.00')).toBeInTheDocument()
  })

  it('suppresses every money cell for a single-employee department — hours still carry', () => {
    render(
      <Statement
        report={makeSosReport({ labor_variance: VARIANCE_BLOCK })}
        onLineClick={() => {}}
      />,
    )

    const fdRow = screen.getByText('Front Desk').closest('tr')!
    // Em dashes, the inline hidden marker, no money figures at all — the hours
    // cell renders trimmed ("16"), so any dd.dd pattern would be a leak.
    expect(within(fdRow).getByText('hidden (single employee)')).toBeInTheDocument()
    expect(fdRow.textContent).not.toMatch(/\d+\.\d{2}/)
    expect(within(fdRow).getByText('16')).toBeInTheDocument()
    expect(within(fdRow).queryByText('alert')).not.toBeInTheDocument()

    // Honest muted note, reusing the B3 phrasing.
    expect(screen.getByText(/Cost hidden for 1 single-employee/)).toBeInTheDocument()
  })

  it('renders no variance section when labor_variance is null', () => {
    render(<Statement report={makeSosReport()} onLineClick={() => {}} />)
    expect(
      screen.queryByText('Schedule 14 — Payroll: actual vs estimate'),
    ).not.toBeInTheDocument()
    expect(screen.queryByText(/Pay periods:/)).not.toBeInTheDocument()
  })
})
