// The payroll dashboard reads one endpoint and derives every figure from it.
// What is worth pinning is not the SVG geometry — it is the arithmetic a GM
// will act on, and the two places the page must tell the truth about what it
// cannot show.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getLaborAnalytics: vi.fn(),
  getMe: vi.fn(),
  getProperties: vi.fn(),
}))

import { getLaborAnalytics, getMe, getProperties } from '../api/client'
import type { LaborAnalytics } from '../api/types'
import { createAppRouter } from '../router'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/payroll-dashboard'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

// 100 hours, 10 of them overtime, $2,000 disclosed against $10,000 revenue and
// 200 rooms: 20% of revenue, $10.00 per room, 10% overtime. Round numbers on
// purpose — a wrong denominator is obvious rather than plausible.
const ANALYTICS: LaborAnalytics = {
  property_id: 'HISJ',
  date_from: '2026-08-01',
  date_to: '2026-08-02',
  days: [
    {
      business_date: '2026-08-01', hours: '60.00', ot_hours: '0.00',
      est_cost: '1200.0000', rooms_occupied: '120.0000', revenue: '6000.0000',
      department_hours: { Housekeeping: '50.00', 'Night Auditor': '10.00' },
    },
    {
      business_date: '2026-08-02', hours: '40.00', ot_hours: '10.00',
      est_cost: '800.0000', rooms_occupied: '80.0000', revenue: '4000.0000',
      // Housekeeping falls 50 -> 25 while Night Auditor RISES 10 -> 15. Both
      // still sum to the day totals above and to the window totals below.
      department_hours: { Housekeeping: '25.00', 'Night Auditor': '15.00' },
    },
  ],
  departments: [
    {
      department: 'Housekeeping', hours: '75.00', ot_hours: '10.00',
      est_cost: '2000.0000', target_hours: '60.00',
    },
    {
      department: 'Night Auditor', hours: '25.00', ot_hours: '0.00',
      est_cost: null, target_hours: null,
    },
  ],
  hours_total: '100.00', ot_hours_total: '10.00', cost_total: '2000.0000',
  revenue_total: '10000.0000', rooms_total: '200.0000', fte: '8.75',
  suppressed_departments: 1, unpriced_hours: '0.00',
}

const EMPTY: LaborAnalytics = {
  ...ANALYTICS, days: [], departments: [], hours_total: '0.00',
  ot_hours_total: '0.00', cost_total: '0.0000', revenue_total: '0.0000',
  rooms_total: '0.0000', fte: null, suppressed_departments: 0,
}

beforeEach(() => {
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
  vi.mocked(getProperties).mockResolvedValue([
    { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2026-07-01', last_date: '2026-08-02', name: 'HISJ' },
  ])
  // The current window resolves first, the comparison window second.
  vi.mocked(getLaborAnalytics)
    .mockResolvedValueOnce(ANALYTICS)
    .mockResolvedValue(EMPTY)
})

describe('PayrollDashboardPage', () => {
  it('derives the decision measures from cost, revenue and rooms', async () => {
    renderPage()
    // $2,000 shows twice on purpose — the KPI and Housekeeping's row, which is
    // the only disclosed department, so they agree.
    expect(await screen.findAllByText('$2,000')).toHaveLength(2)
    expect(screen.getByText('20%')).toBeInTheDocument()             // % of revenue
    expect(screen.getByText('$10.00')).toBeInTheDocument()          // cost per room
    // Overtime, on the KPI and again on Housekeeping's row — all 10 hours of
    // it are theirs.
    expect(screen.getAllByText('10 h').length).toBeGreaterThanOrEqual(2)
    // 100 hours over 200 rooms — the productivity yardstick, on the same tile.
    expect(screen.getByText(/0\.5 hours per room/)).toBeInTheDocument()
  })

  // The whole reason the money and the mix are drawn on different measures.
  it('states a suppressed department rather than showing it as zero', async () => {
    renderPage()
    const table = await screen.findByRole('table', { name: 'Department labor' })
    const row = within(table).getByText('Night Auditor').closest('tr')!
    expect(within(row).getByText('hidden')).toBeInTheDocument()
    expect(within(row).getByText('25 h')).toBeInTheDocument()  // hours still carry
    expect(within(row).queryByText('$0')).not.toBeInTheDocument()
    expect(
      screen.getByText(/Cost is hidden for 1 department/),
    ).toBeInTheDocument()
  })

  it('scores hours against the department standard', async () => {
    renderPage()
    const table = await screen.findByRole('table', { name: 'Department labor' })
    const row = within(table).getByText('Housekeeping').closest('tr')!
    expect(within(row).getByText('60 h')).toBeInTheDocument()   // target
    expect(within(row).getByText('+15 h')).toBeInTheDocument()  // 75 worked - 60 target
    // A department with no standard says so instead of implying a target of 0.
    const solo = within(table).getByText('Night Auditor').closest('tr')!
    expect(within(solo).getByText('no standard')).toBeInTheDocument()
  })

  // The ranked chart replaced a stacked column. Two properties matter: it is
  // ranked by hours, and each department's daily line is that department's OWN
  // series from the API — not the day total apportioned by its window share,
  // which drew every department with an identical shape.
  it('ranks departments by hours and draws each one its own daily shape', async () => {
    renderPage()
    const chart = await screen.findByRole('img', {
      name: "Departments ranked by hours worked, with each department's daily hours",
    })
    // Housekeeping's 75 h outranks Night Auditor's 25 h, so it is drawn first.
    const names = within(chart)
      .getAllByTitle(/Housekeeping|Night Auditor/)
      .map((el) => el.textContent)
    expect(names).toEqual(['Housekeeping', 'Night Auditor'])
    // The department that carries all the overtime says so; the other does not.
    expect(within(chart).getByText('10 h OT')).toBeInTheDocument()

    // Housekeeping falls 50 -> 25 while Night Auditor RISES 10 -> 15 — opposite
    // directions on the same two days. Apportioning the day total by each
    // department's window share could never produce that: every department
    // would move the same way, and after per-row normalisation the two paths
    // would be byte-identical.
    const hk = within(chart).getByRole('img', { name: 'Housekeeping daily hours' })
    const na = within(chart).getByRole('img', { name: 'Night Auditor daily hours' })
    const d = (el: HTMLElement) => el.querySelector('path')!.getAttribute('d')
    expect(d(hk)).not.toBe(d(na))
  })

  it('refetches on a period change', async () => {
    // The clock is FROZEN mid-month, and that is load-bearing rather than
    // tidiness. The page reads the real `new Date()`, and on the LAST day of a
    // month "this month"'s comparison window (the prior month, clamped to
    // today's day-of-month) is exactly "last month"'s primary window — so
    // React Query serves one of the two from cache and only one new fetch
    // fires. This test failed for real on 2026-08-31, on `main` as well: the
    // page was behaving correctly and the assertion was assuming a date.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-08-12T09:00:00Z'))
    try {
      renderPage()
      await screen.findAllByText('$2,000')
      vi.mocked(getLaborAnalytics).mockClear()
      await userEvent.click(screen.getByRole('button', { name: 'Last month' }))
      // Both windows move together: the reading and what it is compared against.
      expect(getLaborAnalytics).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
