import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'
import { createAppRouter } from '../router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getKioskDevices: vi.fn(),
  enrollKioskDevice: vi.fn(),
  revokeKioskDevice: vi.fn(),
  getProperties: vi.fn(),
  getMe: vi.fn(),
}))
import {
  enrollKioskDevice, getKioskDevices, getMe, getProperties, revokeKioskDevice,
} from '../api/client'

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/kiosk-devices'] }))
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
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
  vi.mocked(getProperties).mockResolvedValue([
    { property_id: 'HISJ', pms_source: 'OPERA', first_date: '2026-07-07', last_date: '2026-07-07', name: null },
    { property_id: 'SSSJ', pms_source: 'AUTOCLERK', first_date: '2026-07-07', last_date: '2026-07-07', name: null },
  ])
  vi.mocked(getKioskDevices).mockResolvedValue([
    { device_id: 1, property_id: 'HISJ', name: 'Front desk iPad', revoked: false },
    { device_id: 2, property_id: 'SSSJ', name: 'Old tablet', revoked: true },
  ])
  vi.mocked(enrollKioskDevice).mockResolvedValue({
    device_id: 3, property_id: 'HISJ', name: 'New iPad', token: 'tok-shown-once',
  })
  vi.mocked(revokeKioskDevice).mockResolvedValue({
    device_id: 1, property_id: 'HISJ', name: 'Front desk iPad', revoked: true,
  })
})

describe('KioskDevicesPage', () => {
  it('lists devices with revocation state', async () => {
    renderPage()
    const table = await screen.findByRole('table', { name: 'Kiosk devices' })
    expect(within(table).getByText('Front desk iPad')).toBeInTheDocument()
    expect(within(table).getByText('revoked')).toBeInTheDocument()
    // A revoked device offers no revoke button; the active one does.
    expect(within(table).getAllByRole('button', { name: 'Revoke' })).toHaveLength(1)
  })

  it('enrolls a device and shows the token exactly once', async () => {
    renderPage()
    await screen.findByRole('table', { name: 'Kiosk devices' })
    await userEvent.selectOptions(screen.getByLabelText('Property'), 'HISJ')
    await userEvent.type(screen.getByLabelText('Device name'), 'New iPad')
    await userEvent.click(screen.getByRole('button', { name: 'Enroll device' }))
    await waitFor(() => expect(enrollKioskDevice).toHaveBeenCalledWith('HISJ', 'New iPad'))
    expect(await screen.findByText('tok-shown-once')).toBeInTheDocument()
    // Dismiss is the only way it goes away — it is never fetchable again.
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('tok-shown-once')).not.toBeInTheDocument()
  })

  it('revokes a device', async () => {
    renderPage()
    const table = await screen.findByRole('table', { name: 'Kiosk devices' })
    await userEvent.click(within(table).getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(revokeKioskDevice).toHaveBeenCalledWith(1))
  })
})
