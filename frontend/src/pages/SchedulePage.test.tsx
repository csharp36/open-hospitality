import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT, HISJ_PROPERTY, makeEmployee } from '../test/fixtures'
import { createAppRouter } from '../router'
import type {
  DemandSurface,
  ForecastDay,
  LaborStandard,
  ScheduleAdherence,
  ScheduleProjection,
  ScheduleShift,
  ScheduleTargets,
  ScheduleWeek,
  ShiftTemplate,
} from '../api/types'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getMe: vi.fn(),
  getProperties: vi.fn(),
  getEmployees: vi.fn(),
  getDepartments: vi.fn(),
  getTemplates: vi.fn(),
  createTemplate: vi.fn(),
  deleteTemplate: vi.fn(),
  getScheduleWeek: vi.fn(),
  createScheduleWeek: vi.fn(),
  createShift: vi.fn(),
  updateShift: vi.fn(),
  deleteShift: vi.fn(),
  getProjection: vi.fn(),
  getAdherence: vi.fn(),
  publishSchedule: vi.fn(),
  getStandards: vi.fn(),
  upsertStandard: vi.fn(),
  deleteStandard: vi.fn(),
  getForecast: vi.fn(),
  saveForecast: vi.fn(),
  getTargets: vi.fn(),
  putAvailabilityNote: vi.fn(),
  getDemand: vi.fn(),
}))
import {
  ApiError,
  createScheduleWeek,
  createShift,
  deleteShift,
  deleteStandard,
  getAdherence,
  getDemand,
  getDepartments,
  getEmployees,
  getForecast,
  getMe,
  getProjection,
  getProperties,
  getScheduleWeek,
  getStandards,
  getTargets,
  getTemplates,
  publishSchedule,
  putAvailabilityNote,
  saveForecast,
  updateShift,
  upsertStandard,
} from '../api/client'

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/schedule'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

// 2026-07-20 is a Monday on the HISJ payroll grid.
const FRONT_DESK_AM: ShiftTemplate = {
  template_id: 3, property_id: 'HISJ', department_id: 5,
  name: 'Front Desk AM', start_time: '07:00', end_time: '15:00',
  crosses_midnight: false,
}

const HANK_SHIFT: ScheduleShift = {
  shift_id: 1, schedule_id: 10, business_date: '2026-07-20', department_id: 5,
  start_time: '07:00', end_time: '15:00', crosses_midnight: false,
  employee_id: 11, template_id: 3,
}

const OPEN_SHIFT: ScheduleShift = {
  shift_id: 2, schedule_id: 10, business_date: '2026-07-21', department_id: 5,
  start_time: '15:00', end_time: '23:00', crosses_midnight: false,
  employee_id: null, template_id: null,
}

const WEEK: ScheduleWeek = {
  schedule_id: 10, property_id: 'HISJ', week_start: '2026-07-20',
  status: 'draft', version: 0, published_at: null,
  shifts: [HANK_SHIFT, OPEN_SHIFT],
}

// Rooms carries the ONLY money on the page (two PRICED employees, so not
// suppressed); Kitchen is single-employee → est_cost null; Spa is the
// 2-assigned/1-priced shape (its second worker is exempt/rate-less), so the
// backend suppresses it too — est_cost null, mirrored here. From these numbers
// a leak WOULD look like: rate 987.53/57.50 ≈ 17.17, Hank ≈ 712.75,
// Rita ≈ 274.78; and if Spa's solo priced cost leaked it would read 258.00
// (12.00h × a round 21.50) — the money-discipline test asserts none exist.
const PROJECTION: ScheduleProjection = {
  schedule_id: 10, week_start: '2026-07-20',
  merged_through: null, // not the current week — a pure plan-derived projection
  employees: [
    {
      employee_id: 11, full_name: 'Hank Housekeeper',
      total_hours: '41.50', regular_hours: '40.00', ot_hours: '1.50',
    },
    {
      employee_id: 12, full_name: 'Rita Roomer',
      total_hours: '16.00', regular_hours: '16.00', ot_hours: '0.00',
    },
  ],
  warnings: [
    {
      code: 'scheduled_overtime', employee_id: 11, full_name: 'Hank Housekeeper',
      business_date: '2026-07-24', hours: '1.50',
    },
    {
      code: 'clopening', employee_id: 11, full_name: 'Hank Housekeeper',
      business_date: '2026-07-21', hours: '0.00',
    },
  ],
  departments: [
    { department: 'Kitchen', hours: '8.00', est_cost: null },
    { department: 'Rooms', hours: '57.50', est_cost: '987.53' },
    { department: 'Spa', hours: '20.00', est_cost: null },
  ],
  total_est_cost: '987.53', suppressed_departments: 2, unpriced_hours: '4.00',
}

// --- D2 fixtures -------------------------------------------------------------

const FIXED_STANDARD: LaborStandard = {
  standard_id: 1, property_id: 'HISJ', department_id: 5,
  basis: 'fixed_hours_per_day', value: '16.00',
}

// Five saved days; Sat 07-25 has HINTS but no saved forecast (the never-auto-
// fill case); Sun 07-26 has neither.
const FORECAST: ForecastDay[] = [
  { business_date: '2026-07-20', occupied_rooms: 42, hint_same_day_last_week: 38, hint_trailing_avg: 41 },
  { business_date: '2026-07-21', occupied_rooms: 40, hint_same_day_last_week: null, hint_trailing_avg: 41 },
  { business_date: '2026-07-22', occupied_rooms: 38, hint_same_day_last_week: 36, hint_trailing_avg: 41 },
  { business_date: '2026-07-23', occupied_rooms: 45, hint_same_day_last_week: 43, hint_trailing_avg: 41 },
  { business_date: '2026-07-24', occupied_rooms: 50, hint_same_day_last_week: 47, hint_trailing_avg: 41 },
  { business_date: '2026-07-25', occupied_rooms: null, hint_same_day_last_week: 44, hint_trailing_avg: 41 },
  { business_date: '2026-07-26', occupied_rooms: null, hint_same_day_last_week: null, hint_trailing_avg: null },
]

// Rooms: under on Mon (13.50/20.00), OVER on Tue (18.00/16.00 → warn), a
// no-forecast Wed (target null → "—", counted in days_without_forecast).
// Kitchen has NO standard — every target null, total null, counter 0 (its
// nulls are not about the forecast). HOURS ONLY by construction: the fixture
// contains no cost/rate/money-shaped key, and a test pins that.
const TARGETS: ScheduleTargets = {
  schedule_id: 10, week_start: '2026-07-20',
  departments: [
    {
      department: 'Rooms',
      days: [
        { business_date: '2026-07-20', target_hours: '20.00', scheduled_hours: '13.50' },
        { business_date: '2026-07-21', target_hours: '16.00', scheduled_hours: '18.00' },
        { business_date: '2026-07-22', target_hours: null, scheduled_hours: '4.00' },
        { business_date: '2026-07-23', target_hours: '19.00', scheduled_hours: '19.00' },
        { business_date: '2026-07-24', target_hours: '22.75', scheduled_hours: '8.00' },
        { business_date: '2026-07-25', target_hours: null, scheduled_hours: '0.00' },
        { business_date: '2026-07-26', target_hours: null, scheduled_hours: '0.00' },
      ],
      target_total: '77.75', scheduled_total: '62.50', days_without_forecast: 3,
    },
    {
      department: 'Kitchen',
      days: [
        { business_date: '2026-07-20', target_hours: null, scheduled_hours: '6.00' },
        { business_date: '2026-07-21', target_hours: null, scheduled_hours: '0.00' },
        { business_date: '2026-07-22', target_hours: null, scheduled_hours: '0.00' },
        { business_date: '2026-07-23', target_hours: null, scheduled_hours: '0.00' },
        { business_date: '2026-07-24', target_hours: null, scheduled_hours: '0.00' },
        { business_date: '2026-07-25', target_hours: null, scheduled_hours: '0.00' },
        { business_date: '2026-07-26', target_hours: null, scheduled_hours: '0.00' },
      ],
      target_total: null, scheduled_total: '6.00', days_without_forecast: 0,
    },
  ],
}

// --- D3 fixtures -------------------------------------------------------------

// Adherence only renders for PUBLISHED weeks — a draft has nothing the crew
// could have adhered to.
const PUBLISHED_WEEK: ScheduleWeek = {
  ...WEEK, status: 'published', version: 1,
  published_at: '2026-07-20T12:00:00+00:00',
}

// as_of Thu 2026-07-23 → elapsed Mon–Wed. Rooms: a no-show Monday
// (0.00/7.50), a short Tuesday (4.00/7.50 — a ≥60-min deviation), a clean
// Wednesday (7.50/7.50). Kitchen carries Cara's Wednesday UNSCHEDULED punch
// attributed to her home department (3.00 punched, nothing scheduled).
// HOURS ONLY by construction — no cost/rate/money-shaped key exists, and a
// test pins that.
const ADHERENCE: ScheduleAdherence = {
  schedule_id: 10, week_start: '2026-07-20', as_of: '2026-07-23',
  departments: [
    {
      department: 'Kitchen',
      days: [
        { business_date: '2026-07-20', scheduled_hours: '0.00', punched_hours: '0.00' },
        { business_date: '2026-07-21', scheduled_hours: '0.00', punched_hours: '0.00' },
        { business_date: '2026-07-22', scheduled_hours: '0.00', punched_hours: '3.00' },
      ],
      scheduled_total: '0.00', punched_total: '3.00',
    },
    {
      department: 'Rooms',
      days: [
        { business_date: '2026-07-20', scheduled_hours: '7.50', punched_hours: '0.00' },
        { business_date: '2026-07-21', scheduled_hours: '7.50', punched_hours: '4.00' },
        { business_date: '2026-07-22', scheduled_hours: '7.50', punched_hours: '7.50' },
      ],
      scheduled_total: '22.50', punched_total: '11.50',
    },
  ],
  exceptions: [
    {
      code: 'no_show', employee_id: 11, full_name: 'Hank Housekeeper',
      business_date: '2026-07-20', scheduled_hours: '7.50', punched_hours: '0.00',
    },
    {
      code: 'deviation', employee_id: 12, full_name: 'Rita Roomer',
      business_date: '2026-07-21', scheduled_hours: '7.50', punched_hours: '4.00',
    },
    {
      code: 'unscheduled_punch', employee_id: 13, full_name: 'Cara Cook',
      business_date: '2026-07-22', scheduled_hours: '0.00', punched_hours: '3.00',
    },
  ],
}

// --- J5 fixtures: CRM demand -------------------------------------------------

// The feature-off shape: configured false renders NO demand UI at all.
const DEMAND_OFF: DemandSurface = {
  configured: false, provider: null,
  capabilities: { rooms_on_books: false, group_rooms: false, event_covers: false },
  days: [],
}

// Delphi-ish: pace + blocks, NO covers capability. Thu 07-23 carries the fat
// block (labels are scheduler-surface working data — asserted visible HERE
// and nowhere else); Sat 07-25 has group rooms but no rooms-on-books figure
// (the series omitted it → null, must render absent, never 0). 07-25 also has
// NO saved forecast, pinning that demand never auto-fills an input.
const DEMAND: DemandSurface = {
  configured: true, provider: 'delphi',
  capabilities: { rooms_on_books: true, group_rooms: true, event_covers: false },
  days: [
    {
      stay_date: '2026-07-23', rooms_on_books: 132, group_rooms: 50,
      event_covers: null, labels: 'Acme Corp Annual, Delta Sigma Reunion',
      pulled_at: '2026-07-20T12:00:00+00:00',
    },
    {
      stay_date: '2026-07-25', rooms_on_books: null, group_rooms: 12,
      event_covers: null, labels: 'Coastal Runners Expo',
      pulled_at: '2026-07-20T12:00:00+00:00',
    },
  ],
}

beforeEach(() => {
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
  vi.mocked(getProperties).mockResolvedValue([HISJ_PROPERTY])
  vi.mocked(getEmployees).mockResolvedValue([
    makeEmployee({ employee_id: 11, full_name: 'Hank Housekeeper', department_id: 5 }),
    makeEmployee({ employee_id: 12, full_name: 'Rita Roomer', department_id: 5 }),
  ])
  vi.mocked(getTemplates).mockResolvedValue([FRONT_DESK_AM])
  vi.mocked(getScheduleWeek).mockResolvedValue(WEEK)
  vi.mocked(createShift).mockClear()
  vi.mocked(createShift).mockResolvedValue(HANK_SHIFT)
  vi.mocked(updateShift).mockResolvedValue(HANK_SHIFT)
  vi.mocked(deleteShift).mockResolvedValue(undefined)
  vi.mocked(getProjection).mockResolvedValue(PROJECTION)
  vi.mocked(getAdherence).mockClear()
  vi.mocked(getAdherence).mockResolvedValue(ADHERENCE)
  vi.mocked(publishSchedule).mockClear()
  vi.mocked(getStandards).mockResolvedValue([FIXED_STANDARD])
  vi.mocked(upsertStandard).mockClear()
  vi.mocked(upsertStandard).mockResolvedValue(FIXED_STANDARD)
  vi.mocked(deleteStandard).mockClear()
  vi.mocked(deleteStandard).mockResolvedValue(undefined)
  vi.mocked(getForecast).mockResolvedValue(FORECAST)
  vi.mocked(saveForecast).mockClear()
  vi.mocked(saveForecast).mockResolvedValue({ property_id: 'HISJ', saved: 7 })
  vi.mocked(getDepartments).mockResolvedValue([
    { department_id: 5, property_id: 'HISJ', name: 'Front Desk' },
  ])
  vi.mocked(getTargets).mockResolvedValue(TARGETS)
  vi.mocked(getDemand).mockResolvedValue(DEMAND_OFF)
  vi.mocked(putAvailabilityNote).mockClear()
  vi.mocked(putAvailabilityNote).mockResolvedValue({
    employee_id: 11, availability_note: 'prefers mornings',
  })
})

async function pickWeek() {
  // The page defaults to the current Monday and loads it immediately — the
  // mocked getScheduleWeek answers whatever week is asked for.
  await screen.findByRole('region', { name: 'week grid' })
}

describe('SchedulePage', () => {
  it('renders the week grid with assigned and OPEN shifts from the API', async () => {
    renderPage()
    await pickWeek()

    const grid = await screen.findByRole('region', { name: 'week grid' })
    expect(getScheduleWeek).toHaveBeenCalledWith('HISJ', expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/))
    // The chip shows times only; who and where live in its accessible name,
    // because the row and column already say them on screen.
    expect(
      await within(grid).findByRole('button', { name: /07:00–15:00 Hank Housekeeper/ }),
    ).toBeInTheDocument()
    expect(within(grid).getByRole('button', { name: /15:00–23:00 OPEN/ })).toBeInTheDocument()
    // Departments render by NAME now (the accordion header + legend).
    expect(within(grid).getAllByText('Front Desk').length).toBeGreaterThan(0)
    // Publish state moved to the page header card, beside the week it applies to.
    expect(screen.getByText('draft')).toBeInTheDocument()
  })

  // A day already over is read-only: its hours belong to the timecards now, and
  // "correcting" a past schedule would restate them.
  it('locks days that have already passed and leaves the rest editable', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    // Wednesday of the fixture week: Mon/Tue are behind, Wed onward ahead.
    vi.setSystemTime(new Date(2026, 6, 22, 9, 0, 0))
    try {
      renderPage()
      const grid = await screen.findByRole('region', { name: 'week grid' })
      // Wait for the week to actually land: the queries below are `query*`,
      // which would pass vacuously against a grid that had not loaded yet.
      const pastChip = await within(grid).findByRole('button', {
        name: /07:00–15:00 Hank Housekeeper/,
      })

      // No way in on a day that is over — the + is absent, not merely hidden.
      expect(
        within(grid).queryByLabelText(/Add shift for .* on 2026-07-20/),
      ).not.toBeInTheDocument()
      expect(
        within(grid).queryByLabelText(/Add shift for .* on 2026-07-21/),
      ).not.toBeInTheDocument()
      // ...and every future day of the same week still offers one.
      expect(
        within(grid).getAllByLabelText(/Add shift for .* on 2026-07-22/).length,
      ).toBeGreaterThan(0)

      // A past shift is not draggable either, so it cannot be moved out of the
      // day the timecards recorded it in.
      expect(pastChip).toHaveAttribute('draggable', 'false')
    } finally {
      vi.useRealTimers()
    }
  })

  it('adds a shift with the times pre-filled from the template', async () => {
    renderPage()
    await pickWeek()

    // Every cell carries a hover "+" that opens the add-shift modal prefilled.
    fireEvent.click(screen.getAllByLabelText(/^Add shift for/)[0]!)
    const form = await screen.findByRole('dialog', { name: 'Add shift' })
    await userEvent.selectOptions(within(form).getByLabelText('Shift template'), '3')
    await userEvent.selectOptions(within(form).getByLabelText('Shift employee'), '12')
    await userEvent.click(within(form).getByRole('button', { name: 'Add shift' }))

    await waitFor(() => expect(createShift).toHaveBeenCalledWith(10, expect.objectContaining({
      department_id: 5,
      start_time: '07:00',
      end_time: '15:00',
      crosses_midnight: false,
      employee_id: 12,
      template_id: 3,
    })))
  })

  it('renders a conflict 422 detail verbatim', async () => {
    vi.mocked(createShift).mockRejectedValue(
      new ApiError(422, 'overlaps an existing shift for this employee'),
    )
    renderPage()
    await pickWeek()

    fireEvent.click(screen.getAllByLabelText(/^Add shift for/)[0]!)
    const form = await screen.findByRole('dialog', { name: 'Add shift' })
    await userEvent.selectOptions(within(form).getByLabelText('Shift template'), '3')
    await userEvent.click(within(form).getByRole('button', { name: 'Add shift' }))

    expect(await within(form).findByRole('alert')).toHaveTextContent(
      'overlaps an existing shift for this employee',
    )
  })

  it('auto-creates the missing week on the first shift add and surfaces its error', async () => {
    vi.mocked(getScheduleWeek).mockRejectedValue(new ApiError(404, 'schedule week not found'))
    vi.mocked(createScheduleWeek).mockRejectedValue(
      new ApiError(422, 'week_start is not on the payroll Monday grid'),
    )
    renderPage()
    // The 404 is "no week yet", not an error banner — the grid still renders
    // and the week is created silently with the first shift.
    expect(
      await screen.findByText(/created automatically with the first shift/),
    ).toBeInTheDocument()
    fireEvent.click(screen.getAllByLabelText(/^Add shift for/)[0]!)
    const form = await screen.findByRole('dialog', { name: 'Add shift' })
    await userEvent.selectOptions(within(form).getByLabelText('Shift template'), '3')
    await userEvent.click(within(form).getByRole('button', { name: 'Add shift' }))

    expect(await within(form).findByRole('alert')).toHaveTextContent(
      'week_start is not on the payroll Monday grid',
    )
    expect(createScheduleWeek).toHaveBeenCalledWith(
      expect.objectContaining({ property: 'HISJ' }),
    )
  })

  it('reassigns a shift from the editor via updateShift with the full shape', async () => {
    renderPage()
    await pickWeek()

    const grid = await screen.findByRole('region', { name: 'week grid' })
    await userEvent.click(
      await within(grid).findByRole('button', { name: /15:00–23:00 OPEN/ }),
    )
    const editor = await screen.findByRole('region', { name: 'shift detail' })
    await userEvent.selectOptions(within(editor).getByLabelText('Reassign employee'), '12')
    await userEvent.click(within(editor).getByRole('button', { name: 'Save assignment' }))

    await waitFor(() => expect(updateShift).toHaveBeenCalledWith(2, {
      business_date: '2026-07-21',
      department_id: 5,
      start_time: '15:00',
      end_time: '23:00',
      crosses_midnight: false,
      employee_id: 12,
      template_id: null,
    }))
  })

  it('deletes a shift from the editor', async () => {
    renderPage()
    await pickWeek()

    const grid = await screen.findByRole('region', { name: 'week grid' })
    await userEvent.click(
      await within(grid).findByRole('button', { name: /07:00–15:00 Hank Housekeeper/ }),
    )
    const editor = await screen.findByRole('region', { name: 'shift detail' })
    await userEvent.click(within(editor).getByRole('button', { name: 'Delete shift' }))

    await waitFor(() => expect(deleteShift).toHaveBeenCalledWith(1))
  })

  it('renders projected hours with a warn badge on OT and the warning lines', async () => {
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'projection' })
    expect(getProjection).toHaveBeenCalledWith(10)

    const hours = await within(panel).findByRole('table', { name: 'Projected hours' })
    const hankRow = within(hours).getByText('Hank Housekeeper').closest('tr')!
    expect(within(hankRow).getByText('41.50')).toBeInTheDocument()
    expect(within(hankRow).getByText('40.00')).toBeInTheDocument()
    // OT > 0 renders as a warn Badge (tone classes carry the amber color word).
    expect(within(hankRow).getByText('1.50').className).toMatch(/amber/)
    // Rita has no OT — a plain cell, no badge.
    const ritaRow = within(hours).getByText('Rita Roomer').closest('tr')!
    expect(within(ritaRow).getByText('0.00').className).not.toMatch(/amber/)

    // Warnings anchor on the day+employee they concern; OT speaks in hours.
    expect(within(panel).getByText(
      'Fri 2026-07-24 — scheduled overtime: Hank Housekeeper, 1.50h',
    )).toBeInTheDocument()
    expect(within(panel).getByText(
      'Tue 2026-07-21 — clopening: Hank Housekeeper',
    )).toBeInTheDocument()

    // The meal assumption is stated, not hidden.
    expect(within(panel).getByText(
      'Shifts over 6h assume a 30-minute unpaid meal.',
    )).toBeInTheDocument()

    // merged_through null → NO merged-actuals label (D3): the label's absence
    // means a pure plan-derived projection.
    expect(within(panel).queryByText(/Includes actual hours/)).not.toBeInTheDocument()
  })

  it('labels the projection with the merged-actuals line when merged_through is set', async () => {
    vi.mocked(getProjection).mockResolvedValue({
      ...PROJECTION, merged_through: '2026-07-22',
    })
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'projection' })
    expect(await within(panel).findByText(
      'Includes actual hours through 2026-07-22',
    )).toBeInTheDocument()
  })

  it('suppresses department cost below two priced employees and notes it', async () => {
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'projection' })
    const costs = await within(panel).findByRole('table', {
      name: 'Projected department cost',
    })
    const kitchenRow = within(costs).getByText('Kitchen').closest('tr')!
    // Hours still carry; the money cell is the em-dash + note, no number.
    expect(within(kitchenRow).getByText('8.00')).toBeInTheDocument()
    expect(within(kitchenRow).getByText('—')).toBeInTheDocument()
    expect(
      within(kitchenRow).getByText('hidden (fewer than two priced employees)'),
    ).toBeInTheDocument()
    expect(kitchenRow.textContent).not.toMatch(/\d+\.\d{2}\s*$/)

    // Spa is 2-assigned/1-priced — suppressed exactly like the solo Kitchen.
    const spaRow = within(costs).getByText('Spa').closest('tr')!
    expect(within(spaRow).getByText('20.00')).toBeInTheDocument()
    expect(within(spaRow).getByText('—')).toBeInTheDocument()
    expect(spaRow.textContent).not.toMatch(/\d+\.\d{2}\s*$/)

    const roomsRow = within(costs).getByText('Rooms').closest('tr')!
    expect(within(roomsRow).getByText('987.53')).toBeInTheDocument()
    const totalRow = within(costs).getByText('Total est cost').closest('tr')!
    expect(within(totalRow).getByText('987.53')).toBeInTheDocument()

    expect(within(panel).getByText(
      'Cost hidden for 2 departments with fewer than two priced employees ' +
        '(excluded from the total).',
    )).toBeInTheDocument()
    expect(within(panel).getByText(
      '4.00h scheduled carry no est cost (exempt or unrated employees).',
    )).toBeInTheDocument()
  })

  it('publishes the week and re-renders the bumped version', async () => {
    const publishedWeek: ScheduleWeek = {
      ...WEEK, status: 'published', version: 1,
      published_at: '2026-07-16T21:00:00+00:00',
    }
    vi.mocked(publishSchedule).mockResolvedValue(publishedWeek)
    renderPage()
    await pickWeek()

    await screen.findByRole('region', { name: 'week grid' })
    // Publish and its state live in the page header card now, next to the week
    // they describe — not buried in the grid.
    expect(screen.getByText('draft')).toBeInTheDocument()

    // The publish invalidates the week query; the refetch returns the
    // published week.
    vi.mocked(getScheduleWeek).mockResolvedValue(publishedWeek)
    await userEvent.click(screen.getByRole('button', { name: 'Publish' }))

    await waitFor(() => expect(publishSchedule).toHaveBeenCalledWith(10))
    expect(await screen.findByText('v1 published 2026-07-16')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Republish (v2)' })).toBeInTheDocument()
  })

  it('Print week calls window.print', async () => {
    const print = vi.fn()
    vi.stubGlobal('print', print)
    try {
      renderPage()
      await pickWeek()

      await screen.findByRole('region', { name: 'week grid' })
      await userEvent.click(screen.getByRole('button', { name: 'Print' }))
      expect(print).toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('never renders per-employee money anywhere in the DOM', async () => {
    renderPage()
    await pickWeek()
    await screen.findByRole('region', { name: 'week grid' })
    // Wait for the projection DATA (both tables), not just the panel shell.
    await screen.findByRole('table', { name: 'Projected hours' })
    await screen.findByRole('table', { name: 'Projected department cost' })

    const text = document.body.textContent ?? ''
    // The department aggregate is the ONLY money on the page…
    expect(text).toContain('987.53')
    // …and nothing derivable per employee exists: no currency marker, no
    // rate (987.53 / 57.50 h ≈ 17.17), no per-employee cost split
    // (Hank 41.50 h ≈ 712.75, Rita 16.00 h ≈ 274.78), and no would-be cost
    // for the 2-assigned/1-priced Spa (12.00 h × 21.50 = 258.00 — the value
    // an assigned-population suppression counter would have leaked, from
    // which rate = 258.00 / 12.00 = 21.50 falls straight out).
    expect(text).not.toContain('$')
    for (const leak of ['17.17', '712.75', '274.78', '258.00', '21.50']) {
      expect(text).not.toContain(leak)
    }
    // The per-employee table itself carries hours only — the department
    // aggregate never bleeds into it.
    const hours = screen.getByRole('table', { name: 'Projected hours' })
    expect(hours.textContent).not.toContain('987')
  })

  it('lists labor standards and upserts/deletes per department', async () => {
    renderPage()

    const panel = await screen.findByRole('region', { name: 'labor standards' })
    const table = await within(panel).findByRole('table', { name: 'Labor standards' })
    const row = within(table).getByText('Front Desk').closest('tr')!
    expect(within(row).getByText('fixed hours per day')).toBeInTheDocument()
    expect(within(row).getByText('16.00')).toBeInTheDocument()

    // A dropdown of real departments, not a database key typed by hand.
    await userEvent.selectOptions(within(panel).getByLabelText('Standard department'), '5')
    await userEvent.selectOptions(
      within(panel).getByLabelText('Standard basis'), 'minutes_per_occupied_room',
    )
    fireEvent.change(within(panel).getByLabelText('Standard value'), {
      target: { value: '30' },
    })
    await userEvent.click(within(panel).getByRole('button', { name: 'Save standard' }))
    await waitFor(() => expect(upsertStandard).toHaveBeenCalledWith({
      property: 'HISJ', department_id: 5,
      basis: 'minutes_per_occupied_room', value: 30,
    }))

    await userEvent.click(
      within(panel).getByRole('button', { name: 'Delete standard for Front Desk' }),
    )
    await waitFor(() => expect(deleteStandard).toHaveBeenCalledWith(1))
  })

  it('renders forecast inputs with hints beside them and saves the 7-day payload', async () => {
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'occupancy forecast' })
    // Hints render muted beside each day; a null hint is simply omitted
    // ('avg: 41' alone on Tue, whose last-wk fact is absent).
    expect(await within(panel).findByText('last wk: 38 · avg: 41')).toBeInTheDocument()
    expect(within(panel).getByText('avg: 41')).toBeInTheDocument()

    // The GM's SAVED numbers seed the inputs; hints NEVER auto-fill: Sat has
    // hints (last wk 44) but no saved value, so its input stays empty.
    expect(within(panel).getByLabelText('Occupied rooms 2026-07-20')).toHaveValue(42)
    expect(within(panel).getByText('last wk: 44 · avg: 41')).toBeInTheDocument()
    expect(within(panel).getByLabelText('Occupied rooms 2026-07-25')).toHaveValue(null)

    fireEvent.change(within(panel).getByLabelText('Occupied rooms 2026-07-25'), {
      target: { value: '48' },
    })
    fireEvent.change(within(panel).getByLabelText('Occupied rooms 2026-07-26'), {
      target: { value: '55' },
    })
    await userEvent.click(within(panel).getByRole('button', { name: 'Save forecast' }))

    await waitFor(() => expect(saveForecast).toHaveBeenCalledWith({
      property: 'HISJ',
      days: [
        { business_date: '2026-07-20', occupied_rooms: 42 },
        { business_date: '2026-07-21', occupied_rooms: 40 },
        { business_date: '2026-07-22', occupied_rooms: 38 },
        { business_date: '2026-07-23', occupied_rooms: 45 },
        { business_date: '2026-07-24', occupied_rooms: 50 },
        { business_date: '2026-07-25', occupied_rooms: 48 },
        { business_date: '2026-07-26', occupied_rooms: 55 },
      ],
    }))
  })

  it('renders target-vs-scheduled cells with over/under tones and the forecast-gap note', async () => {
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'targets' })
    const table = await within(panel).findByRole('table', {
      name: 'Target vs scheduled hours',
    })

    // Under target: a plain scheduled/target cell.
    expect(within(table).getByText('13.50/20.00').className).not.toMatch(/amber/)
    // Over target (18.00 scheduled vs 16.00 target): the warn tone.
    expect(within(table).getByText('18.00/16.00').className).toMatch(/amber/)
    // A per-room day with NO forecast: muted "—", never a silent zero.
    expect(within(table).getByText('4.00/—').className).toMatch(/ink-muted/)
    // Week totals column: scheduled_total/target_total.
    expect(within(table).getByText('62.50/77.75')).toBeInTheDocument()

    // Kitchen has NO standard — every day and the total render the muted "—".
    const kitchenRow = within(table).getByText('Kitchen').closest('tr')!
    expect(within(kitchenRow).getAllByText(/\/—$/)).toHaveLength(8) // 7 days + week
    // Both the Mon cell and the week total read 6.00/— (all-null target_total)
    // and both carry the muted tone.
    for (const cell of within(kitchenRow).getAllByText('6.00/—')) {
      expect(cell.className).toMatch(/ink-muted/)
    }

    // The gap is named, per department; Kitchen's nulls are not about the
    // forecast, so it gets no note.
    expect(within(panel).getByText(
      'Rooms: 3 day(s) without forecast — per-room targets need one ' +
        '(absence is not zero demand).',
    )).toBeInTheDocument()
    expect(within(panel).queryByText(/Kitchen: \d+ day/)).not.toBeInTheDocument()

    // Hours-only by construction: the targets response shape carries no
    // money-shaped key or value anywhere.
    expect(JSON.stringify(TARGETS)).not.toMatch(/cost|rate|price|\$/i)
  })

  it('renders the availability note on selection and saves an inline edit', async () => {
    vi.mocked(getEmployees).mockResolvedValue([
      makeEmployee({
        employee_id: 11, full_name: 'Hank Housekeeper', department_id: 5,
        availability_note: "can't work Tuesdays",
      }),
      makeEmployee({ employee_id: 12, full_name: 'Rita Roomer', department_id: 5 }),
    ])
    renderPage()
    await pickWeek()

    // Add-shift flow: the note appears when the employee is picked…
    fireEvent.click(screen.getAllByLabelText(/^Add shift for/)[0]!)
    const form = await screen.findByRole('dialog', { name: 'Add shift' })
    await userEvent.selectOptions(within(form).getByLabelText('Shift employee'), '11')
    expect(within(form).getByText("note: can't work Tuesdays")).toBeInTheDocument()
    // …and the note-less employee shows the muted placeholder instead.
    await userEvent.selectOptions(within(form).getByLabelText('Shift employee'), '12')
    expect(within(form).getByText('no scheduling note')).toBeInTheDocument()

    // Reassign flow: the editor shows the selected employee's note too.
    await userEvent.click(within(form).getByRole('button', { name: 'Cancel' }))
    const grid = screen.getByRole('region', { name: 'week grid' })
    await userEvent.click(
      await within(grid).findByRole('button', { name: /07:00–15:00 Hank Housekeeper/ }),
    )
    const editor = await screen.findByRole('region', { name: 'shift detail' })
    expect(within(editor).getByText("note: can't work Tuesdays")).toBeInTheDocument()

    // Inline edit PUTs the note (scheduler-gated server-side).
    await userEvent.click(within(editor).getByRole('button', {
      name: 'Edit availability note for Hank Housekeeper',
    }))
    const input = within(editor).getByLabelText(
      'Availability note for Hank Housekeeper',
    )
    await userEvent.clear(input)
    await userEvent.type(input, 'prefers mornings')
    await userEvent.click(within(editor).getByRole('button', { name: 'Save note' }))
    await waitFor(() =>
      expect(putAvailabilityNote).toHaveBeenCalledWith(11, 'prefers mornings'),
    )
  })

  it('renders the adherence table and grouped exceptions for a published week', async () => {
    vi.mocked(getScheduleWeek).mockResolvedValue(PUBLISHED_WEEK)
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'adherence' })
    expect(getAdherence).toHaveBeenCalledWith(10)

    const table = await within(panel).findByRole('table', {
      name: 'Adherence by department',
    })
    const roomsRow = within(table).getByText('Rooms').closest('tr')!
    // Under-delivered coverage shades amber: the no-show Monday and the short
    // Tuesday; the clean Wednesday stays plain. The week total is under too.
    expect(within(roomsRow).getByText('0.00/7.50').className).toMatch(/amber/)
    expect(within(roomsRow).getByText('4.00/7.50').className).toMatch(/amber/)
    expect(within(roomsRow).getByText('7.50/7.50').className).not.toMatch(/amber/)
    expect(within(roomsRow).getByText('11.50/22.50').className).toMatch(/amber/)
    // Kitchen's unscheduled punch OVER-delivers — no warn tone anywhere
    // (both the Wednesday cell and the week total read 3.00/0.00).
    const kitchenRow = within(table).getByText('Kitchen').closest('tr')!
    for (const cell of within(kitchenRow).getAllByText('3.00/0.00')) {
      expect(cell.className).not.toMatch(/amber/)
    }

    // All three exception codes render, grouped by code, hours only.
    expect(within(panel).getByText(
      'Mon 2026-07-20 — no show: Hank Housekeeper (scheduled 7.50)',
    )).toBeInTheDocument()
    expect(within(panel).getByText(
      'Tue 2026-07-21 — deviation: Rita Roomer (7.50 scheduled, 4.00 punched)',
    )).toBeInTheDocument()
    expect(within(panel).getByText(
      'Wed 2026-07-22 — unscheduled punch: Cara Cook (3.00 punched)',
    )).toBeInTheDocument()
    expect(within(panel).getByRole('list', { name: 'no show exceptions' }))
      .toBeInTheDocument()
    expect(within(panel).getByRole('list', { name: 'deviation exceptions' }))
      .toBeInTheDocument()
    expect(within(panel).getByRole('list', { name: 'unscheduled punch exceptions' }))
      .toBeInTheDocument()

    // Punches change server-side independently of this page's mutations —
    // Refresh refetches on demand.
    await userEvent.click(within(panel).getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(getAdherence).toHaveBeenCalledTimes(2))
  })

  it('shows the adherence empty state on a fully-future published week', async () => {
    vi.mocked(getScheduleWeek).mockResolvedValue(PUBLISHED_WEEK)
    vi.mocked(getAdherence).mockResolvedValue({
      schedule_id: 10, week_start: '2026-07-20', as_of: '2026-07-18',
      departments: [], exceptions: [],
    })
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'adherence' })
    expect(await within(panel).findByText('No elapsed days yet.')).toBeInTheDocument()
  })

  it('hides the adherence panel for draft weeks', async () => {
    renderPage()
    await pickWeek()

    await screen.findByRole('region', { name: 'week grid' })
    expect(screen.queryByRole('region', { name: 'adherence' })).not.toBeInTheDocument()
    expect(getAdherence).not.toHaveBeenCalled()
  })

  it('adherence carries no money — hours only, in the data and the DOM', async () => {
    vi.mocked(getScheduleWeek).mockResolvedValue(PUBLISHED_WEEK)
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'adherence' })
    await within(panel).findByRole('table', { name: 'Adherence by department' })

    // The response shape is hours-only by construction: no money-shaped key
    // or value anywhere (the mock mirrors the backend field-for-field)…
    expect(JSON.stringify(ADHERENCE)).not.toMatch(/cost|rate|price|\$/i)
    // …and the rendered panel never invents one.
    expect(panel.textContent).not.toMatch(/\$/)
    expect(panel.textContent).not.toMatch(/cost|rate|price/i)
  })

  it('gates the page on org_admin/property_gm', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['accountant'] })
    renderPage()
    expect(
      await screen.findByText('Scheduling requires the org_admin or property_gm role.'),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText('Previous week')).not.toBeInTheDocument()
  })
})

// --- CRM demand hints (Pillar J5) --------------------------------------------

describe('SchedulePage CRM demand', () => {
  it('renders demand beside the forecast and as a grid chip, from the latest snapshot', async () => {
    vi.mocked(getDemand).mockResolvedValue(DEMAND)
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'occupancy forecast' })
    await waitFor(() =>
      expect(panel.textContent).toContain(
        '132 on books · 50 group — Acme Corp Annual, Delta Sigma Reunion',
      ),
    )
    // The as-of line says WHEN and from WHICH provider.
    expect(panel.textContent).toContain('CRM demand as of 2026-07-20 (delphi)')
    expect(getDemand).toHaveBeenCalledWith('HISJ', '2026-07-20', '2026-07-26')

    const grid = await screen.findByRole('region', { name: 'week grid' })
    expect(within(grid).getByText('132 on books · 50 group')).toBeInTheDocument()
    // The grid chip carries FIGURES only — labels live in the forecast panel.
    expect(within(grid).queryByText(/Acme Corp Annual/)).not.toBeInTheDocument()
  })

  it('renders an absent dimension as absent — never 0', async () => {
    vi.mocked(getDemand).mockResolvedValue(DEMAND)
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'occupancy forecast' })
    await waitFor(() => expect(panel.textContent).toContain('132 on books'))
    // Delphi has no covers capability: the word never appears, 0 never renders.
    expect(panel.textContent).not.toContain('covers')
    // Sat 07-25: the series stated no rooms figure — group renders alone.
    expect(panel.textContent).toContain('12 group — Coastal Runners Expo')
    expect(panel.textContent).not.toContain('0 on books')
    const grid = await screen.findByRole('region', { name: 'week grid' })
    expect(within(grid).getByText('12 group')).toBeInTheDocument()
  })

  it("shows an as-of range when rows span pulls, never one row's stamp", async () => {
    // Rows can legitimately come from different batches (a past date's
    // voice is an older pull). Stamping them all with days[0]'s pulled_at
    // would display a stale figure under a fresh claim (J7 review).
    vi.mocked(getDemand).mockResolvedValue({
      ...DEMAND,
      days: DEMAND.days.map((d) =>
        d.stay_date === '2026-07-25'
          ? { ...d, pulled_at: '2026-07-14T12:00:00+00:00' }
          : d,
      ),
    })
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'occupancy forecast' })
    await waitFor(() =>
      expect(panel.textContent).toContain(
        'CRM demand as of 2026-07-14 – 2026-07-20 (delphi)',
      ),
    )
  })

  it('never auto-fills a forecast input from demand', async () => {
    vi.mocked(getDemand).mockResolvedValue(DEMAND)
    renderPage()
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'occupancy forecast' })
    await waitFor(() => expect(panel.textContent).toContain('12 group'))
    // Sat 07-25 has demand but NO saved forecast: the input stays empty.
    expect(within(panel).getByLabelText('Occupied rooms 2026-07-25')).toHaveValue(null)
  })

  it('renders no demand UI when the feature is off', async () => {
    renderPage() // beforeEach default: DEMAND_OFF
    await pickWeek()

    const panel = await screen.findByRole('region', { name: 'occupancy forecast' })
    await within(panel).findByLabelText('Occupied rooms 2026-07-20')
    expect(panel.textContent).not.toContain('on books')
    expect(panel.textContent).not.toContain('CRM demand')
  })
})
