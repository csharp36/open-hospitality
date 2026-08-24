import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createAppRouter } from '../router'
import type { PreviewPayload } from '../api/types'

vi.mock('../api/client', async (orig) => ({
  ...(await orig<typeof import('../api/client')>()),
  postPreview: vi.fn(),
}))

import { postPreview } from '../api/client'

const okPayload: PreviewPayload = {
  pms_source: 'OPERA',
  report_type: 'trial_balance',
  business_date: '2026-08-13',
  pnl_lines: [],
  kpis: [],
  codes_recognized: 4,
  codes_mapped: 3,
  codes_needs_review: 2,
}

function renderPreviewPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/try'] }))
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

async function dropFile() {
  const input = await screen.findByLabelText('PDF file')
  fireEvent.change(input, {
    target: { files: [new File(['x'], 'a.pdf', { type: 'application/pdf' })] },
  })
}

afterEach(() => vi.unstubAllGlobals())

describe('PreviewPage (public)', () => {
  it('renders the front door without authentication', async () => {
    renderPreviewPage()
    expect(await screen.findByText(/see last night’s numbers/i)).toBeInTheDocument()
  })

  it('shows the preview result when postPreview resolves ok', async () => {
    vi.mocked(postPreview).mockResolvedValue({ status: 'ok', payload: okPayload })
    renderPreviewPage()

    await dropFile()

    expect(await screen.findByRole('region', { name: 'Preview result' })).toBeInTheDocument()
  })

  it('shows the unreadable edge state with its hints', async () => {
    vi.mocked(postPreview).mockResolvedValue({
      status: 'unreadable',
      hints: ['try the original PDF'],
    })
    renderPreviewPage()

    await dropFile()

    expect(await screen.findByRole('region', { name: 'Unreadable file' })).toBeInTheDocument()
    expect(await screen.findByText('try the original PDF')).toBeInTheDocument()
  })

  it('offers an unsupported vendor a live setup-link form, not a disabled notify-me', async () => {
    vi.mocked(postPreview).mockResolvedValue({
      status: 'unsupported',
      vendor: 'HotelKey',
      reason: 'vendor_not_supported',
    })
    renderPreviewPage()

    await dropFile()

    const region = await screen.findByRole('region', { name: 'Unsupported PMS' })
    expect(within(region).getAllByText(/HotelKey/).length).toBeGreaterThan(0)
    expect(within(region).getByLabelText('Email address')).toBeInTheDocument()
    expect(within(region).queryByText(/coming soon/i)).not.toBeInTheDocument()
  })

  it('runs a sample through the SAME upload path as a dropped file', async () => {
    // Nothing is pre-baked: the sample is fetched from the SPA origin and posted
    // to /api/preview like any other file, so a visitor who clicks it sees what
    // the parser really does.
    const blob = new Blob(['%PDF-1.4'], { type: 'application/pdf' })
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) })
    vi.stubGlobal('fetch', fetchMock)
    vi.mocked(postPreview).mockResolvedValue({ status: 'ok', payload: okPayload })
    renderPreviewPage()

    fireEvent.click(await screen.findByRole('button', { name: /opera trial balance/i }))

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith('/samples/opera-trial-balance-sample.pdf'),
    )
    await waitFor(() => expect(postPreview).toHaveBeenCalled())
    expect(await screen.findByRole('region', { name: 'Preview result' })).toBeInTheDocument()
  })

  it('reports a sample that fails to load instead of hanging on "Reading…"', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    renderPreviewPage()

    fireEvent.click(await screen.findByRole('button', { name: /autoclerk transaction summary/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/sample didn’t load/i)
  })

  it('says on the page that it cannot read a SkyTouch pack here', async () => {
    // _PREVIEW_ADAPTERS holds only Opera trial_balance and AutoClerk
    // transaction_summary, so a SkyTouch pack dropped here comes back
    // "unreadable" — even though signup now offers SkyTouch as a source. The
    // front door has to say so before someone tries it.
    renderPreviewPage()
    const section = await screen.findByRole('region', { name: 'How this works' })
    expect(within(section).getByText(/SkyTouch/)).toBeInTheDocument()
  })

  it('labels the header login for who it is actually for', async () => {
    // Plain "Log in" sent first-time visitors to a Keycloak screen they have no
    // account for and no way past.
    renderPreviewPage()
    expect(await screen.findByRole('button', { name: /customer log in/i })).toBeInTheDocument()
  })
})
