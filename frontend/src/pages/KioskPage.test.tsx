import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createAppRouter } from '../router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getKioskRoster: vi.fn(),
  postPunch: vi.fn(),
  getKioskMyWeek: vi.fn(),
  getKioskConfig: vi.fn(),
  postKioskIdentify: vi.fn(),
  getKioskSearch: vi.fn(),
  getKioskPunchState: vi.fn(),
}))
import {
  getKioskConfig, getKioskMyWeek, getKioskPunchState, getKioskRoster,
  getKioskSearch, postKioskIdentify, postPunch,
} from '../api/client'
import { upcomingWeekMonday } from '../lib/week'

// The kiosk captures a photo via getUserMedia + canvas; both are stubbed here.
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

function renderKiosk() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/kiosk'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

// The SAME Monday the page will request — the arithmetic itself is unit-tested
// in lib/week.test.ts; here it pins the mocked shifts into the rendered week.
const weekStart = upcomingWeekMonday(new Date())

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  stubCamera()
  vi.mocked(getKioskRoster).mockResolvedValue([{ employee_id: 7, full_name: 'Hank H' }])
  vi.mocked(postPunch).mockResolvedValue({
    punch_id: 1, employee_id: 7, punch_type: 'clock_in', business_date: '2026-07-07',
  })
  vi.mocked(getKioskMyWeek).mockResolvedValue({
    employee_id: 7, week_start: weekStart, published: true, shifts: [],
  })
  // Default: matching dark — every pre-F5 test runs the roster flow unchanged.
  vi.mocked(getKioskConfig).mockResolvedValue({ matching_enabled: false })
  // Default: nobody is on the clock, so Clock In is the only legal punch.
  vi.mocked(getKioskPunchState).mockResolvedValue({
    employee_id: 7, state: 'out', allowed: ['clock_in'],
  })
  vi.mocked(postKioskIdentify).mockResolvedValue({
    state: 'matched', employee_id: 7, full_name: 'Hank H',
  })
  vi.mocked(getKioskSearch).mockResolvedValue([{ employee_id: 7, full_name: 'Hank H' }])
})

describe('KioskPage', () => {
  it('asks for a device token when the kiosk is not enrolled', async () => {
    renderKiosk()
    expect(await screen.findByLabelText('Device token')).toBeInTheDocument()
  })

  it('renders the roster and punches without any login', async () => {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Clock In' }))
    await waitFor(() =>
      expect(postPunch).toHaveBeenCalledWith('dev-tok', 7, 'clock_in', expect.any(Blob)),
    )
    // The toast names the employee — /Clocked in/i alone also matches a
    // roster tile's own "Not clocked in" caption.
    expect(await screen.findByText(/Clocked in — Hank H/)).toBeInTheDocument()
  })

  it('shows my week: OWN shifts from the published schedule, never another employee\'s', async () => {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    vi.mocked(getKioskRoster).mockResolvedValue([
      { employee_id: 7, full_name: 'Hank H' },
      { employee_id: 8, full_name: 'Rosa R' },
    ])
    // The server only ever returns the requested employee's shifts; the mock
    // mirrors that so the assertion below proves the UI renders exactly what
    // was asked for — id 7's week — and nothing of Rosa's.
    vi.mocked(getKioskMyWeek).mockImplementation(async (_tok, employeeId) =>
      employeeId === 7
        ? {
            employee_id: 7, week_start: weekStart, published: true,
            shifts: [
              { business_date: weekStart, department: 'Housekeeping',
                start_time: '09:00', end_time: '17:00', crosses_midnight: false },
              { business_date: weekStart, department: 'Front Desk',
                start_time: '22:00', end_time: '06:00', crosses_midnight: true },
            ],
          }
        : {
            employee_id: 8, week_start: weekStart, published: true,
            shifts: [
              { business_date: weekStart, department: 'Laundry',
                start_time: '10:00', end_time: '18:00', crosses_midnight: false },
            ],
          },
    )
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'My week' }))

    expect(await screen.findByText(/Housekeeping · 09:00–17:00/)).toBeInTheDocument()
    expect(screen.getByText(/Front Desk · 22:00–06:00 \(\+1d\)/)).toBeInTheDocument()
    expect(getKioskMyWeek).toHaveBeenCalledWith('dev-tok', 7, weekStart)
    // Another employee's shifts never render.
    expect(screen.queryByText(/Laundry/)).not.toBeInTheDocument()
    expect(screen.queryByText(/10:00–18:00/)).not.toBeInTheDocument()
  })

  it('shows "No published schedule yet" when the week is draft/unpublished', async () => {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    vi.mocked(getKioskMyWeek).mockResolvedValue({
      employee_id: 7, week_start: weekStart, published: false, shifts: [],
    })
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'My week' }))
    expect(await screen.findByText('No published schedule yet')).toBeInTheDocument()
  })

  // THE ORDER RULE, as the employee meets it. The server refuses an illegal
  // punch with a 409; the kiosk's job is to never offer one in the first place.
  // The wall answers "who is working right now" before anyone taps anything.
  it('marks each roster tile with its punch state', async () => {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    vi.mocked(getKioskRoster).mockResolvedValue([
      { employee_id: 7, full_name: 'Hank H', state: 'in' },
      { employee_id: 8, full_name: 'Rosa R', state: 'on_break' },
      { employee_id: 9, full_name: 'Cal C', state: 'out' },
    ])
    renderKiosk()

    // The state is TEXT as well as a ring colour: a colour-blind employee has
    // to find their own tile as fast as anyone else.
    expect(await screen.findByText('On the clock')).toBeInTheDocument()
    expect(screen.getByText('On lunch')).toBeInTheDocument()
    expect(screen.getByText('Not clocked in')).toBeInTheDocument()
    // Initials, not a photo — the kiosk never renders a face on the idle wall.
    expect(screen.getByText('HH')).toBeInTheDocument()
    // And the header counts who is on shift, lunch included.
    expect(screen.getByText('2 on the clock')).toBeInTheDocument()
  })

  it('offers only the punches that are legal from the current state', async () => {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    vi.mocked(getKioskPunchState).mockResolvedValue({
      employee_id: 7, state: 'on_break', allowed: ['lunch_end'],
    })
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))

    expect(await screen.findByRole('button', { name: 'End Lunch' })).toBeInTheDocument()
    // You cannot clock out while still on lunch: the open break is never
    // deducted, so the employee would be paid straight through it.
    expect(screen.queryByRole('button', { name: 'Clock Out' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clock In' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start Lunch' })).not.toBeInTheDocument()
    // And the screen says where they stand, so the missing buttons read as a
    // state rather than as a broken kiosk.
    expect(screen.getByText('On lunch')).toBeInTheDocument()
  })

  it('offers lunch and clock-out once on the clock', async () => {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    vi.mocked(getKioskPunchState).mockResolvedValue({
      employee_id: 7, state: 'in', allowed: ['lunch_start', 'clock_out'],
    })
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    expect(await screen.findByRole('button', { name: 'Start Lunch' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Clock Out' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clock In' })).not.toBeInTheDocument()
  })

  // The wage rule outranks the convenience: a kiosk that cannot read the state
  // must still let someone punch, because the server will refuse anything
  // genuinely illegal anyway.
  it('falls back to every punch when the state call fails', async () => {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    vi.mocked(getKioskPunchState).mockRejectedValue(new Error('down'))
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    expect(await screen.findByRole('button', { name: 'Clock In' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Clock Out' })).toBeInTheDocument()
  })

  it('Back returns from my week to the punch actions', async () => {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'My week' }))
    expect(await screen.findByText(/week of/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clock In' })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByRole('button', { name: 'Clock In' })).toBeInTheDocument()
  })

  const TWO_HOTELS = [
    { label: 'Holiday Inn', token: 'tok-hisj' },
    { label: 'SureStay', token: 'tok-sssj' },
  ]

  it('a shared tablet asks WHERE first, then rosters only that hotel', async () => {
    localStorage.setItem('usali.kiosk.enrollments', JSON.stringify(TWO_HOTELS))
    renderKiosk()
    expect(await screen.findByText('Where are you working today?')).toBeInTheDocument()
    // No roster call yet: neither hotel's names may show before the pick.
    expect(getKioskRoster).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'SureStay' }))
    await screen.findByRole('button', { name: /Hank H/ })
    expect(getKioskRoster).toHaveBeenCalledWith('tok-sssj')
    expect(getKioskRoster).not.toHaveBeenCalledWith('tok-hisj')
  })

  it('Switch location returns to the picker', async () => {
    localStorage.setItem('usali.kiosk.enrollments', JSON.stringify(TWO_HOTELS))
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: 'Holiday Inn' }))
    await screen.findByRole('button', { name: /Hank H/ })
    await userEvent.click(screen.getByRole('button', { name: 'Switch location' }))
    expect(await screen.findByText('Where are you working today?')).toBeInTheDocument()
  })

  it('a revoked token drops ONLY its own enrollment; the other hotel survives', async () => {
    localStorage.setItem('usali.kiosk.enrollments', JSON.stringify(TWO_HOTELS))
    const { ApiError } = await vi.importActual<typeof import('../api/client')>('../api/client')
    vi.mocked(getKioskRoster).mockImplementation(async (tok) => {
      if (tok === 'tok-sssj') throw new ApiError(403, 'revoked', 'revoked')
      return [{ employee_id: 7, full_name: 'Hank H' }]
    })
    renderKiosk()
    await userEvent.click(await screen.findByRole('button', { name: 'SureStay' }))
    // Dead token: back to the picker... which now has only the good hotel —
    // a single remaining enrollment auto-activates, so Hank's roster loads.
    await screen.findByRole('button', { name: /Hank H/ })
    expect(
      JSON.parse(localStorage.getItem('usali.kiosk.enrollments') ?? '[]'),
    ).toEqual([TWO_HOTELS[0]])
  })

  // --- F5: the photo-first flow (matching enabled) ---------------------------

  function enableMatching() {
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    vi.mocked(getKioskConfig).mockResolvedValue({ matching_enabled: true })
  }

  it('face-first: NO roster grid, capture -> "Hi {name}" -> one tap punches', async () => {
    enableMatching()
    renderKiosk()
    // The idle screen is the camera, not names.
    const start = await screen.findByRole('button', { name: /tap to clock in or out/i })
    expect(getKioskRoster).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /Hank H/ })).not.toBeInTheDocument()

    await userEvent.click(start)
    await waitFor(() =>
      expect(postKioskIdentify).toHaveBeenCalledWith('dev-tok', expect.any(Blob)),
    )
    // The confirm tap IS the punch action tap.
    expect(await screen.findByText(/Hi Hank H/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clock In' }))
    await waitFor(() =>
      expect(postPunch).toHaveBeenCalledWith('dev-tok', 7, 'clock_in', expect.any(Blob)),
    )
    // The toast names the employee — /Clocked in/i alone also matches a
    // roster tile's own "Not clocked in" caption.
    expect(await screen.findByText(/Clocked in — Hank H/)).toBeInTheDocument()
    // Back on the idle screen afterwards; the roster never loaded at all.
    expect(await screen.findByRole('button', { name: /tap to clock in or out/i }))
      .toBeInTheDocument()
    expect(getKioskRoster).not.toHaveBeenCalled()
  })

  it('a failed match falls back to SEARCH — never a browsable roster', async () => {
    enableMatching()
    vi.mocked(postKioskIdentify).mockResolvedValue({ state: 'no_match' })
    renderKiosk()
    await userEvent.click(
      await screen.findByRole('button', { name: /tap to clock in or out/i }),
    )
    const box = await screen.findByLabelText('Search your name')
    expect(getKioskRoster).not.toHaveBeenCalled()

    // Two characters: no search fires (the server would 422 anyway).
    await userEvent.type(box, 'ha')
    expect(getKioskSearch).not.toHaveBeenCalled()

    await userEvent.type(box, 'n')
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Clock In' }))
    await waitFor(() =>
      expect(postPunch).toHaveBeenCalledWith('dev-tok', 7, 'clock_in', expect.any(Blob)),
    )
    expect(getKioskSearch).toHaveBeenCalledWith('dev-tok', 'han')
  })

  it('"Not me" on the confirm card goes to search, not a roster', async () => {
    enableMatching()
    renderKiosk()
    await userEvent.click(
      await screen.findByRole('button', { name: /tap to clock in or out/i }),
    )
    expect(await screen.findByText(/Hi Hank H/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Not me' }))
    expect(await screen.findByLabelText('Search your name')).toBeInTheDocument()
    expect(getKioskRoster).not.toHaveBeenCalled()
  })

  it('identify being DOWN still lets the employee punch via search (wage rule)', async () => {
    enableMatching()
    const { ApiError } = await vi.importActual<typeof import('../api/client')>('../api/client')
    vi.mocked(postKioskIdentify).mockRejectedValue(
      new ApiError(503, 'face matching unavailable', 'down'),
    )
    renderKiosk()
    await userEvent.click(
      await screen.findByRole('button', { name: /tap to clock in or out/i }),
    )
    const box = await screen.findByLabelText('Search your name')
    await userEvent.type(box, 'hank')
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Clock In' }))
    await waitFor(() =>
      expect(postPunch).toHaveBeenCalledWith('dev-tok', 7, 'clock_in', expect.any(Blob)),
    )
  })

  it('config being DOWN still lets the employee punch via search (wage rule)', async () => {
    // Not a dead token — the server is just failing. Before F8 this dead-
    // ended the kiosk: no camera (matching unknown), no roster (config
    // gates it), no search, no message. The punch endpoints may be fine;
    // search must render.
    localStorage.setItem('usali.kiosk.token', 'dev-tok')
    const { ApiError } = await vi.importActual<typeof import('../api/client')>('../api/client')
    vi.mocked(getKioskConfig).mockRejectedValue(
      new ApiError(500, 'internal error', 'boom'),
    )
    renderKiosk()
    const box = await screen.findByLabelText('Search your name')
    await userEvent.type(box, 'hank')
    await userEvent.click(await screen.findByRole('button', { name: /Hank H/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Clock In' }))
    await waitFor(() =>
      expect(postPunch).toHaveBeenCalledWith('dev-tok', 7, 'clock_in', expect.any(Blob)),
    )
  })

  it('enrollment records a location name with the token (legacy key untouched)', async () => {
    renderKiosk()
    await userEvent.type(await screen.findByLabelText('Location name'), 'Holiday Inn')
    await userEvent.type(screen.getByLabelText('Device token'), 'tok-new')
    await userEvent.click(screen.getByRole('button', { name: 'Enroll this kiosk' }))
    await screen.findByRole('button', { name: /Hank H/ })
    expect(getKioskRoster).toHaveBeenCalledWith('tok-new')
    expect(
      JSON.parse(localStorage.getItem('usali.kiosk.enrollments') ?? '[]'),
    ).toEqual([{ label: 'Holiday Inn', token: 'tok-new' }])
  })
})
