import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
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
  net_total: '0.00',
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

describe('PreviewPage (public)', () => {
  it('renders the front door without authentication', async () => {
    const router = createAppRouter(createMemoryHistory({ initialEntries: ['/try'] }))
    render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/see your night audit/i)).toBeInTheDocument()
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

  it('shows the unsupported vendor edge state with a notify-me CTA', async () => {
    vi.mocked(postPreview).mockResolvedValue({
      status: 'unsupported',
      vendor: 'HotelKey',
      reason: 'vendor_not_supported',
    })
    renderPreviewPage()

    await dropFile()

    const region = await screen.findByRole('region', { name: 'Unsupported PMS' })
    expect(within(region).getAllByText(/HotelKey/).length).toBeGreaterThan(0)
    expect(within(region).getByText(/notify me/i)).toBeInTheDocument()
  })
})
