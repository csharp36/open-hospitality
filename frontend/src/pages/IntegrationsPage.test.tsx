import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
import {
  ApiError, connectIntegration, disconnectIntegration, getAuthorizeUrl, getIntegrations, getMe,
} from '../api/client'
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
        { name: 'api_token', secret: true, label: 'API token' },
        { name: 'company_id', secret: false, label: 'Company ID' },
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
        { name: 'refresh_token', secret: true, label: 'Refresh token' },
        { name: 'realm_id', secret: false, label: 'Realm ID (QuickBooks company)' },
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
  // qc is returned so a test can drive a refetch via invalidateQueries
  // rather than re-rendering — see App.test.tsx's renderApp for the same
  // pattern. router is returned so a test can assert on its own location:
  // this is a memory history, so window.location never moves.
  return { qc, router }
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
    expect(screen.getByText(/^realm_id:/)).toBeInTheDocument()
  })

  it('still reads as connected when its provider is not in the spec list', async () => {
    // A provider retired from PROVIDERS while a tenant's row still names it.
    // The label degrades to the raw key; the card must not claim the tenant
    // has nothing connected.
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [qbo({
        connected: true,
        provider: 'some_retired_provider',
        identifiers: { realm_id: '4620816365' },
        connected_at: '2026-08-31T10:00:00',
      })],
    })
    renderPage()
    expect(await screen.findByText('some_retired_provider')).toBeInTheDocument()
    expect(screen.queryByText('Not connected')).not.toBeInTheDocument()
    // Disconnect only needs item.integration, not the spec, so a retired
    // provider must not also strand the operator with a live credential they
    // cannot remove.
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Replace credentials' }))
      .not.toBeInTheDocument()
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
    const { qc } = renderPage()
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

  it('renders an input per spec field and sends what it collected', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [gusto()] })
    vi.mocked(connectIntegration).mockResolvedValue(undefined)
    renderPage()

    const token = await screen.findByLabelText('API token')
    // Secret fields are password inputs and start empty even when connected.
    // Two Python tests are what make that safe rather than optimistic, both
    // in tests/test_integrations_api.py: test_no_secret_is_ever_on_the_wire
    // (no value ever comes back) and
    // test_connect_nulls_the_previous_providers_fields (the write is total).
    expect(token).toHaveAttribute('type', 'password')
    expect(token).toHaveValue('')

    fireEvent.change(token, { target: { value: 'tok-1' } })
    fireEvent.change(screen.getByLabelText('Company ID'), {
      target: { value: 'c-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Connect Gusto' }))

    await waitFor(() => {
      expect(connectIntegration).toHaveBeenCalledWith('payroll', {
        provider: 'gusto', api_token: 'tok-1', company_id: 'c-1',
      })
    })
  })

  it('shows the backend refusal verbatim', async () => {
    // The demand feed's crm_ref rule lives in verify_credentials and is NOT
    // restated here: this asserts the page relays it, not that it knows it.
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [gusto({ integration: 'demand_feed', providers: [{
        provider: 'delphi', label: 'Delphi', oauth: false,
        fields: [{ name: 'subscription_key', secret: true, label: 'Subscription key' }],
      }] })],
    })
    vi.mocked(connectIntegration).mockRejectedValue(new ApiError(
      422, 'no property in this workspace has a crm_ref, so the demand feed cannot be verified',
    ))
    renderPage()

    fireEvent.change(await screen.findByLabelText('Subscription key'), {
      target: { value: 'k-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Connect Delphi' }))

    expect(await screen.findByText(/has a crm_ref/)).toBeInTheDocument()
  })

  it('sends an oauth provider to the consent URL and renders no inputs', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [qbo()] })
    vi.mocked(getAuthorizeUrl).mockResolvedValue({ url: 'https://intuit.test/consent' })
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    renderPage()

    expect(await screen.findByRole('button', { name: 'Connect QuickBooks Online' }))
      .toBeInTheDocument()
    // No credential inputs: the tokens come back from Intuit, not the operator.
    expect(screen.queryByLabelText('Refresh token')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Connect QuickBooks Online' }))
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith('https://intuit.test/consent')
    })
    vi.unstubAllGlobals()
  })

  it('announces a completed grant and clears the param', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [qbo()] })
    const { router } = renderPage('/integrations?connected=accounting')
    expect(await screen.findByText(/QuickBooks Online is connected/)).toBeInTheDocument()
    // renderPage uses a memory history, so window.location.search never
    // reflects the router's state — asserting on it would test nothing.
    // The router's own location is what the app actually navigates, so
    // that is what proves the param was cleared.
    await waitFor(() => {
      expect(router.state.location.search).not.toHaveProperty('connected')
    })
  })

  it('renders a failed grant on the accounting card', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [qbo()] })
    renderPage('/integrations?error=QuickBooks+refused+the+grant%3A+access_denied')
    expect(await screen.findByText(/access_denied/)).toBeInTheDocument()
  })

  it('disconnects only after the confirm names what is going', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [qbo({
        connected: true, provider: 'qbo',
        identifiers: { realm_id: '4620816365' },
        connected_at: '2026-08-31T10:00:00',
      })],
    })
    vi.mocked(disconnectIntegration).mockResolvedValue(undefined)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(disconnectIntegration).not.toHaveBeenCalled()
    // The confirm restates the identifier, so an operator with two QuickBooks
    // companies can tell which one they are about to drop. Scoped to the
    // confirm's own sentence (not a bare /4620816365/) because the connected
    // card already renders the identifier above the button, in its own
    // "realm_id: 4620816365" line — a loose match would pass even if the
    // confirm text never mentioned it.
    expect(
      screen.getByText('Disconnect QuickBooks Online (4620816365)?'),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Yes, disconnect' }))
    await waitFor(() => {
      expect(disconnectIntegration).toHaveBeenCalledWith('accounting')
    })
  })

  it('closes the replace form once the new credential is accepted', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [gusto({
        connected: true, provider: 'gusto',
        identifiers: { company_id: 'c-1' },
        connected_at: '2026-08-31T10:00:00',
      })],
    })
    vi.mocked(connectIntegration).mockResolvedValue(undefined)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Replace credentials' }))
    fireEvent.change(screen.getByLabelText('API token'), { target: { value: 'tok-2' } })
    fireEvent.change(screen.getByLabelText('Company ID'), { target: { value: 'c-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect Gusto' }))

    await waitFor(() => {
      expect(connectIntegration).toHaveBeenCalledWith('payroll', {
        provider: 'gusto', api_token: 'tok-2', company_id: 'c-1',
      })
    })
    // The form closes on success: leaving it open leaves the submitted secret
    // rendered in the input and invites a second, accidental resubmit.
    await waitFor(() => {
      expect(screen.queryByLabelText('API token')).not.toBeInTheDocument()
    })
  })

  it('the label an operator sees is what names the input', async () => {
    // getByLabelText alone cannot catch the original defect: an aria-label
    // with nothing drawn on screen satisfies it just as well as a visible
    // <label>. This asserts the visible half directly — a <label> element
    // with the field's text exists and its htmlFor resolves to the input —
    // which is what an aria-label-only render cannot produce.
    vi.mocked(getIntegrations).mockResolvedValue({ items: [gusto()] })
    renderPage()

    const input = await screen.findByLabelText('API token')
    const label = screen.getByText('API token').closest('label')
    expect(label).not.toBeNull()
    expect(label?.getAttribute('for')).toBe(input.id)
  })

  it('never renders two forms with the same input id', async () => {
    // The payroll card offers both Gusto and ADP while nothing is connected
    // — the real defect this fixes. Two ProviderForms on one card must not
    // collide on id even though both happen to have a plain identifier
    // field, which is what makes the id include the provider and not just
    // the field name.
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [gusto({
        providers: [
          {
            provider: 'gusto', label: 'Gusto', oauth: false,
            fields: [
              { name: 'api_token', secret: true, label: 'API token' },
              { name: 'company_id', secret: false, label: 'Company ID' },
            ],
          },
          {
            provider: 'adp', label: 'ADP', oauth: false,
            fields: [
              { name: 'client_secret', secret: true, label: 'Client secret' },
              { name: 'client_id', secret: false, label: 'Client ID' },
            ],
          },
        ],
      })],
    })
    renderPage()
    await screen.findByRole('button', { name: 'Connect Gusto' })
    await screen.findByRole('button', { name: 'Connect ADP' })

    const ids = Array.from(document.querySelectorAll('[id]'))
      .map((el) => el.id)
      .filter((id) => id !== '')
    expect(new Set(ids).size).toBe(ids.length)
  })
})
