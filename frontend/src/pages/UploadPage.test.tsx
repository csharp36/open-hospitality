// Upload page with postIngest mocked: picking files fires one sequential
// POST /ingest per file; success cards show the parse summary, 422 failures
// show the API detail message on a red-bordered card, and a failure never
// stops the rest of the batch.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  postIngest: vi.fn(),
}))

import { ApiError, postIngest } from '../api/client'
import type { IngestResult } from '../api/types'
import { createAppRouter } from '../router'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'

const OPERA_RESULT: IngestResult = {
  pms_source: 'OPERA',
  report_type: 'trial_balance',
  property_id: 'HISJ',
  business_date: '2026-07-07',
  staged: 120,
  mapped: 118,
  unmapped: 2,
  skipped: 1,
}

function renderPage() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/upload'] }))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

function pdf(name: string): File {
  return new File(['%PDF-1.7'], name, { type: 'application/pdf' })
}

async function pickFiles(...files: File[]) {
  const input = await screen.findByLabelText('PDF files')
  fireEvent.change(input, { target: { files } })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('UploadPage', () => {
  it('accepts multiple .pdf files through the hidden picker input', async () => {
    renderPage()
    const input = await screen.findByLabelText('PDF files')
    expect(input).toHaveAttribute('accept', '.pdf')
    expect(input).toHaveAttribute('multiple')
    expect(input).toHaveAttribute('type', 'file')
  })

  it('uploads a picked file and renders every ingest summary field', async () => {
    vi.mocked(postIngest).mockResolvedValue(OPERA_RESULT)
    renderPage()

    await pickFiles(pdf('opera-2026-07-07.pdf'))

    const card = await screen.findByRole('region', {
      name: 'Upload result: opera-2026-07-07.pdf',
    })
    expect(postIngest).toHaveBeenCalledTimes(1)

    expect(await within(card).findByText('OPERA')).toBeInTheDocument()
    expect(within(card).getByText('trial_balance')).toBeInTheDocument()
    expect(within(card).getByText('HISJ')).toBeInTheDocument()
    expect(within(card).getByText('2026-07-07')).toBeInTheDocument()
    expect(within(card).getByText('120')).toBeInTheDocument() // staged
    expect(within(card).getByText('118')).toBeInTheDocument() // mapped
    expect(within(card).getByText('2')).toBeInTheDocument() // unmapped
    expect(within(card).getByText('1')).toBeInTheDocument() // skipped
    expect(card.className).not.toMatch(/border-danger-red/)
  })

  it('renders the 422 detail message on a red-bordered card', async () => {
    vi.mocked(postIngest).mockRejectedValue(
      new ApiError(422, 'Unrecognized report format: not a supported PMS export'),
    )
    renderPage()

    await pickFiles(pdf('mystery.pdf'))

    const card = await screen.findByRole('region', { name: 'Upload result: mystery.pdf' })
    expect(
      await within(card).findByText('Unrecognized report format: not a supported PMS export'),
    ).toBeInTheDocument()
    expect(card.className).toMatch(/border-danger-red/)
  })

  it('uploads sequentially and continues the batch after a failure', async () => {
    // The first upload stays pending behind a gate so we can observe that the
    // second file has NOT been posted yet — true one-at-a-time sequencing.
    let releaseFirst!: () => void
    const gate = new Promise<void>((resolve) => {
      releaseFirst = resolve
    })
    const order: string[] = []
    vi.mocked(postIngest).mockImplementation(async (file: File) => {
      order.push(file.name)
      if (file.name === 'bad.pdf') {
        await gate
        throw new ApiError(422, 'No business date found')
      }
      return OPERA_RESULT
    })
    renderPage()

    await pickFiles(pdf('bad.pdf'), pdf('good.pdf'))

    const badCard = await screen.findByRole('region', { name: 'Upload result: bad.pdf' })
    const goodCard = await screen.findByRole('region', { name: 'Upload result: good.pdf' })
    expect(within(goodCard).getByText('Waiting…')).toBeInTheDocument()
    expect(postIngest).toHaveBeenCalledTimes(1) // second waits for the first

    releaseFirst()

    expect(await within(badCard).findByText('No business date found')).toBeInTheDocument()
    expect(await within(goodCard).findByText('OPERA')).toBeInTheDocument()
    expect(postIngest).toHaveBeenCalledTimes(2)
    expect(order).toEqual(['bad.pdf', 'good.pdf'])
    expect(badCard.className).toMatch(/border-danger-red/)
    expect(goodCard.className).not.toMatch(/border-danger-red/)
  })

  it('uploads files dropped on the drop zone', async () => {
    vi.mocked(postIngest).mockResolvedValue(OPERA_RESULT)
    renderPage()

    const zone = await screen.findByText('Drag and drop PDF reports here')
    fireEvent.drop(zone, { dataTransfer: { files: [pdf('dropped.pdf')] } })

    const card = await screen.findByRole('region', { name: 'Upload result: dropped.pdf' })
    expect(await within(card).findByText('OPERA')).toBeInTheDocument()
    expect(postIngest).toHaveBeenCalledTimes(1)
  })

  it('shows a busy notice instead of silently discarding files added mid-batch', async () => {
    let releaseFirst!: () => void
    vi.mocked(postIngest).mockImplementation(
      () =>
        new Promise<IngestResult>((resolve) => {
          releaseFirst = () => resolve(OPERA_RESULT)
        }),
    )
    renderPage()

    await pickFiles(pdf('first.pdf'))
    await screen.findByRole('region', { name: 'Upload result: first.pdf' })

    // Mid-batch: no drop-here affordance, and a drop attempt gets a visible
    // notice instead of a silent discard.
    const zone = screen.getByText('Drag and drop PDF reports here')
    fireEvent.dragOver(zone)
    expect(zone.closest('div')?.className).not.toMatch(/border-accent/)

    fireEvent.drop(zone, { dataTransfer: { files: [pdf('late.pdf')] } })
    expect(
      await screen.findByText(
        'Please wait for the current batch to finish before adding more files.',
      ),
    ).toBeInTheDocument()
    expect(postIngest).toHaveBeenCalledTimes(1)
    expect(
      screen.queryByRole('region', { name: 'Upload result: late.pdf' }),
    ).not.toBeInTheDocument()

    releaseFirst()

    // Notice clears once the batch finishes.
    expect(await screen.findByText('OPERA')).toBeInTheDocument()
    expect(screen.queryByText(/wait for the current batch/)).not.toBeInTheDocument()
  })
})
