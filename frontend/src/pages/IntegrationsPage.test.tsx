import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'
import { createAppRouter } from '../router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getIntegrations: vi.fn(),
  connectIntegration: vi.fn(),
  disconnectIntegration: vi.fn(),
  getAuthorizeUrl: vi.fn(),
  getMe: vi.fn(),
}))
import { ApiError, getIntegrations, getMe } from '../api/client'
import type { Integration } from '../api/types'

function gusto(overrides: Partial<Integration> = {}): Integration {
  return {
    integration: 'payroll',
    connected: false,
    provider: null,
    identifiers: {},
    connected_at: null,
    providers: [{
      provider: 'gusto',
      label: 'Gusto',
      oauth: false,
      fields: [
        { name: 'api_token', secret: true },
        { name: 'company_id', secret: false },
      ],
    }],
    ...overrides,
  }
}

function qbo(overrides: Partial<Integration> = {}): Integration {
  return {
    integration: 'accounting',
    connected: false,
    provider: null,
    identifiers: {},
    connected_at: null,
    providers: [{
      provider: 'qbo',
      label: 'QuickBooks Online',
      oauth: true,
      fields: [
        { name: 'refresh_token', secret: true },
        { name: 'realm_id', secret: false },
      ],
    }],
    ...overrides,
  }
}

function renderPage(entry = '/integrations') {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [entry] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
  // Returned so a test can drive a refetch via invalidateQueries rather than
  // re-rendering — see App.test.tsx's renderApp for the same pattern.
  return qc
}

describe('IntegrationsPage', () => {
  beforeEach(() => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
  })

  it('shows a connected integration with its identifier', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [qbo({
        connected: true,
        provider: 'qbo',
        identifiers: { realm_id: '4620816365' },
        connected_at: '2026-08-31T10:00:00',
      })],
    })
    renderPage()
    expect(await screen.findByText('QuickBooks Online')).toBeInTheDocument()
    expect(screen.getByText('4620816365')).toBeInTheDocument()
  })

  it('shows an unconnected integration as not connected', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [gusto()] })
    renderPage()
    expect(await screen.findByText('Payroll')).toBeInTheDocument()
    expect(screen.getByText('Not connected')).toBeInTheDocument()
    // The provider label belongs to the connect form, which a later task
    // adds — a disconnected card names the integration, not the product.
    expect(screen.queryByText('Gusto')).not.toBeInTheDocument()
  })

  it('refuses the whole page when a refetch finds a credential cannot be decrypted', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [qbo({
        connected: true,
        provider: 'qbo',
        identifiers: { realm_id: '4620816365' },
        connected_at: '2026-08-31T10:00:00',
      })],
    })
    const qc = renderPage()
    expect(await screen.findByText('QuickBooks Online')).toBeInTheDocument()

    vi.mocked(getIntegrations).mockRejectedValue(
      new ApiError(503, 'the accounting credential cannot be decrypted'),
    )
    await qc.invalidateQueries({ queryKey: ['integrations'] })

    expect(
      await screen.findByText(/cannot be decrypted/),
    ).toBeInTheDocument()
    // react-query keeps the stale QBO data around after a failed refetch;
    // the 503 branch in IntegrationsPage must render instead of the generic
    // error path, or the previously-visible card would render beside the
    // refusal message — the lie CredentialUnreadable exists to prevent.
    expect(screen.queryByText('QuickBooks Online')).not.toBeInTheDocument()
  })
})
