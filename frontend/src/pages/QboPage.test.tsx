// QBO page flow with the API client mocked: the status table derives its
// dates from the property window UNION the ledger rows, Preview renders the
// JE plan with totals, Push goes through a confirm dialog (postQboPush fires
// only after confirm) and refreshes the status query, the structured
// unmapped-GL 422 renders as a worklist, and stale/failed outcomes render as
// attention states — never success.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'

// importOriginal spread keeps the real ApiError class (the unmapped-worklist
// detection does instanceof + detailBody checks) while stubbing the fetchers.
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getProperties: vi.fn(),
  getQboStatus: vi.fn(),
  getQboPreview: vi.fn(),
  postQboPush: vi.fn(),
}))

import {
  ApiError,
  getProperties,
  getQboPreview,
  getQboStatus,
  postQboPush,
} from '../api/client'
import type { JePlan, PushResult } from '../api/types'
import { createAppRouter } from '../router'
import {
  AUTHED_CONTEXT,
  HISJ_PROPERTY,
  SSSJ_PROPERTY,
  UNMAPPED_GL_DETAIL,
  makeJePlan,
  makePushLedgerRow,
} from '../test/fixtures'
import { AuthContext } from '../auth/authContext'

function renderPage(initialPath = '/qbo?property=HISJ&month=2026-07') {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [initialPath] }))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getProperties).mockResolvedValue([HISJ_PROPERTY, SSSJ_PROPERTY])
  vi.mocked(getQboStatus).mockResolvedValue([makePushLedgerRow()])
  vi.mocked(getQboPreview).mockResolvedValue(makeJePlan())
  vi.mocked(postQboPush).mockResolvedValue({ status: 'pushed', qbo_je_id: '987', message: null })
})

describe('QboPage status table', () => {
  it('offers one row per date in the property window, with ledger info where it exists', async () => {
    renderPage()

    const table = await screen.findByRole('table', { name: 'QBO push status' })
    expect(getQboStatus).toHaveBeenCalledWith({ property: 'HISJ', month: '2026-07' })

    // HISJ window 2026-07-01..07 -> 7 candidate dates; only 07-07 has a ledger row.
    expect(within(table).getByText('2026-07-01')).toBeInTheDocument()
    expect(within(table).getByText('2026-07-07')).toBeInTheDocument()
    expect(within(table).getAllByText('not pushed')).toHaveLength(6)
    expect(within(table).getByText('pushed')).toBeInTheDocument()
    expect(within(table).getByText('987')).toBeInTheDocument() // QBO JE id
    expect(within(table).getAllByRole('button', { name: /^Push / })).toHaveLength(7)
  })

  it('renders stale and failed ledger rows as attention badges', async () => {
    vi.mocked(getQboStatus).mockResolvedValue([
      makePushLedgerRow({
        push_id: 2,
        business_date: '2026-07-06',
        status: 'stale',
        qbo_je_id: '900',
        message: 'facts changed since push',
      }),
      makePushLedgerRow({
        push_id: 3,
        business_date: '2026-07-07',
        status: 'failed',
        qbo_je_id: null,
        message: 'QBO rejected the JE',
      }),
    ])
    renderPage()

    const table = await screen.findByRole('table', { name: 'QBO push status' })
    expect(within(table).getByText('stale').className).toMatch(/amber/)
    expect(within(table).getByText('failed').className).toMatch(/red/)
    expect(within(table).getByText('QBO rejected the JE')).toBeInTheDocument()
  })
})

describe('QboPage preview', () => {
  it('opens the JE plan panel with lines and totals', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Preview 2026-07-07' }))

    const panel = await screen.findByRole('region', { name: 'JE preview: HISJ 2026-07-07' })
    expect(getQboPreview).toHaveBeenCalledWith('HISJ', '2026-07-07')
    expect(await within(panel).findByText('Guest Ledger')).toBeInTheDocument()
    expect(within(panel).getByText('Rooms Revenue')).toBeInTheDocument()
    expect(within(panel).getByText('10,456.37')).toBeInTheDocument()
    // Debit total and credit total agree (the debit line shows the same amount).
    expect(within(panel).getAllByText('12,439.66').length).toBeGreaterThanOrEqual(2)

    fireEvent.click(within(panel).getByRole('button', { name: 'Close preview' }))
    expect(screen.queryByRole('region', { name: /JE preview/ })).not.toBeInTheDocument()
  })

  it('renders the structured unmapped-GL 422 as a curation worklist', async () => {
    vi.mocked(getQboPreview).mockRejectedValue(
      new ApiError(422, JSON.stringify(UNMAPPED_GL_DETAIL), UNMAPPED_GL_DETAIL),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Preview 2026-07-07' }))

    const worklist = await screen.findByRole('region', { name: 'Unmapped GL worklist' })
    expect(within(worklist).getByText(/GL curation needed/)).toBeInTheDocument()
    expect(within(worklist).getByText('Parking')).toBeInTheDocument()
    expect(within(worklist).getByText('Vending')).toBeInTheDocument()
  })

  it('renders a plain 422 detail as an error message, not a worklist', async () => {
    vi.mocked(getQboPreview).mockRejectedValue(new ApiError(422, 'multi-source property'))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Preview 2026-07-07' }))

    expect(await screen.findByText(/multi-source property/)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Unmapped GL worklist' })).not.toBeInTheDocument()
  })

  it('treats an empty unmapped list as a plain error, not a worklist', async () => {
    const emptyDetail = { unmapped: [] }
    vi.mocked(getQboPreview).mockRejectedValue(
      new ApiError(422, JSON.stringify(emptyDetail), emptyDetail),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Preview 2026-07-07' }))

    expect(await screen.findByText(/Failed to build the JE plan/)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Unmapped GL worklist' })).not.toBeInTheDocument()
  })
})

describe('QboPage push flow', () => {
  /** Open the confirm dialog for `date`, wait for the plan totals (Confirm is
   * disabled until the plan loads), then confirm. */
  async function pushViaDialog(date: string) {
    fireEvent.click(await screen.findByRole('button', { name: `Push ${date}` }))
    const dialog = await screen.findByRole('dialog', { name: 'Confirm QBO push' })
    await within(dialog).findByText(/debits/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm push' }))
  }

  it('pushes only after the confirm dialog restating the plan totals', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Push 2026-07-07' }))

    const dialog = await screen.findByRole('dialog', { name: 'Confirm QBO push' })
    expect(
      await within(dialog).findByText(
        /Post JE for HISJ 2026-07-07 — debits 12,439\.66 = credits 12,439\.66\?/,
      ),
    ).toBeInTheDocument()
    expect(postQboPush).not.toHaveBeenCalled() // confirm gates the write

    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm push' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    // mutate() runs the mutationFn through a microtask — assert via waitFor.
    await waitFor(() => expect(postQboPush).toHaveBeenCalledWith('HISJ', '2026-07-07'))

    const notice = await screen.findByText(/Pushed 2026-07-07 to QBO — JE 987/)
    expect(notice).toHaveRole('status')
    expect(notice.className).toMatch(/green/)

    // The mutation invalidates the status query -> the ledger refetches.
    await waitFor(() => expect(getQboStatus).toHaveBeenCalledTimes(2))
  })

  it('refetches the plan when the confirm dialog opens (no stale cached totals)', async () => {
    renderPage()
    // Prime the preview cache for the date first…
    fireEvent.click(await screen.findByRole('button', { name: 'Preview 2026-07-07' }))
    await screen.findByText('Guest Ledger')
    expect(getQboPreview).toHaveBeenCalledTimes(1)

    // …then opening the confirm dialog must NOT trust it: the plan refetches
    // so the dialog can only restate fresh totals.
    fireEvent.click(screen.getByRole('button', { name: 'Push 2026-07-07' }))
    await waitFor(() => expect(getQboPreview).toHaveBeenCalledTimes(2))
  })

  it('keeps Confirm disabled until the plan finishes fetching', async () => {
    let resolvePreview!: (plan: JePlan) => void
    vi.mocked(getQboPreview).mockImplementation(
      () =>
        new Promise<JePlan>((resolve) => {
          resolvePreview = resolve
        }),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Push 2026-07-07' }))

    const dialog = await screen.findByRole('dialog', { name: 'Confirm QBO push' })
    expect(within(dialog).getByText('Loading JE plan…')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Confirm push' })).toBeDisabled()

    await act(async () => resolvePreview(makeJePlan()))
    expect(await within(dialog).findByText(/debits 12,439\.66/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Confirm push' })).toBeEnabled()
  })

  it('shows an in-flight state and disables all actions while the push runs', async () => {
    let resolvePush!: (result: PushResult) => void
    vi.mocked(postQboPush).mockImplementation(
      () =>
        new Promise<PushResult>((resolve) => {
          resolvePush = resolve
        }),
    )
    renderPage()
    await pushViaDialog('2026-07-07')

    const pending = await screen.findByRole('status')
    expect(pending.textContent).toMatch(/Pushing 2026-07-07…/)
    // Every Preview/Push button disables — no second dialog can race the outcome.
    for (const button of screen.getAllByRole('button', { name: /^(Preview|Push) / })) {
      expect(button).toBeDisabled()
    }

    await act(async () => resolvePush({ status: 'pushed', qbo_je_id: '987', message: null }))
    expect(await screen.findByText(/Pushed 2026-07-07 to QBO/)).toBeInTheDocument()
    expect(screen.queryByText(/Pushing 2026-07-07…/)).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Push 2026-07-07' })).toBeEnabled()
  })

  it('Escape closes the confirm dialog without pushing', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Push 2026-07-07' }))
    await screen.findByRole('dialog', { name: 'Confirm QBO push' })

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(postQboPush).not.toHaveBeenCalled()
  })

  it('a picker change closes the panel and clears the push notice', async () => {
    renderPage()
    await pushViaDialog('2026-07-07')
    await screen.findByText(/Pushed 2026-07-07 to QBO/)
    expect(screen.getByRole('region', { name: /JE preview/ })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Active property'), { target: { value: 'SSSJ' } })
    expect(screen.queryByRole('region', { name: /JE preview/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/Pushed 2026-07-07 to QBO/)).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('cancel closes the dialog without pushing', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Push 2026-07-07' }))

    const dialog = await screen.findByRole('dialog', { name: 'Confirm QBO push' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(postQboPush).not.toHaveBeenCalled()
  })

  it('renders a stale outcome (HTTP 200) as an amber attention notice, not success', async () => {
    vi.mocked(postQboPush).mockResolvedValue({
      status: 'stale',
      qbo_je_id: null,
      message: 'facts changed since last push',
    })
    renderPage()
    await pushViaDialog('2026-07-07')

    // findByText, not findByRole('status'): the transient "Pushing…" notice
    // also carries role=status and must not be the element under assertion.
    const notice = await screen.findByText(/2026-07-07 is stale/)
    expect(notice).toHaveRole('status')
    expect(notice.textContent).toMatch(/Nothing was posted/)
    expect(notice.className).toMatch(/amber/)
    expect(notice.className).not.toMatch(/green/)
  })

  it('renders a failed outcome (HTTP 200) as a red attention notice, not success', async () => {
    vi.mocked(postQboPush).mockResolvedValue({
      status: 'failed',
      qbo_je_id: null,
      message: 'QBO rejected the journal entry: account 4100 inactive',
    })
    renderPage()
    await pushViaDialog('2026-07-07')

    const notice = await screen.findByText(/failed: QBO rejected the journal entry/)
    expect(notice).toHaveRole('status')
    expect(notice.className).toMatch(/red/)
    expect(notice.className).not.toMatch(/green/)
  })

  it('renders already-pushed as an informational notice', async () => {
    vi.mocked(postQboPush).mockResolvedValue({
      status: 'already-pushed',
      qbo_je_id: '987',
      message: null,
    })
    renderPage()
    await pushViaDialog('2026-07-07')

    const notice = await screen.findByText(/already pushed with identical content/)
    expect(notice).toHaveRole('status')
    expect(notice.className).not.toMatch(/red|amber|green/)
  })

  it('renders a transport failure (502 rejection) as an alert', async () => {
    vi.mocked(postQboPush).mockRejectedValue(
      new ApiError(502, 'cannot reach QBO: connection refused'),
    )
    renderPage()
    await pushViaDialog('2026-07-07')

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/cannot reach QBO/)
    expect(alert.className).toMatch(/red/)
  })
})
