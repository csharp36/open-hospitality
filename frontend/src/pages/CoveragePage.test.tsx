// Coverage page with getCoverage mocked: per-source card renders the
// confidence/review-status grids, staged-vs-mapped with missing codes, the
// needs-review worklist, and the collapsible (informational) unmapped
// statistics labels; the edition selector defaults to 12 and refetches on
// change.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getCoverage: vi.fn(),
}))

import { getCoverage } from '../api/client'
import { createAppRouter } from '../router'
import { AUTHED_CONTEXT, makeCoverageReport } from '../test/fixtures'
import { AuthContext } from '../auth/authContext'

function renderPage(initialPath = '/coverage') {
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
  vi.mocked(getCoverage).mockResolvedValue(makeCoverageReport())
})

describe('CoveragePage', () => {
  it('fetches edition 12 by default and renders the per-source financial coverage', async () => {
    renderPage()

    const card = await screen.findByRole('region', { name: 'Coverage: OPERA' })
    expect(getCoverage).toHaveBeenCalledWith(12)

    // Staged-vs-mapped summary, missing codes, exception count.
    expect(within(card).getByText(/38 of 40 staged codes mapped/)).toBeInTheDocument()
    expect(within(card).getByText(/42 dictionary entries/)).toBeInTheDocument()
    expect(within(card).getByText(/3 exceptions/)).toBeInTheDocument()
    expect(within(card).getByText('Missing codes: 1234, 5678')).toBeInTheDocument()

    // GL mapping summary plus the red unmapped-codes list (same pattern as
    // missing_codes — these block the QBO push).
    expect(within(card).getByText(/GL mapping: 41\/42 entries carry a GL account/)).toBeInTheDocument()
    const glUnmapped = within(card).getByText('GL unmapped codes: 5007')
    expect(glUnmapped).toBeInTheDocument()
    expect(glUnmapped.className).toMatch(/red/)

    // Confidence and review-status count grids.
    expect(within(card).getByText('By confidence')).toBeInTheDocument()
    expect(within(card).getByText('HIGH')).toBeInTheDocument()
    expect(within(card).getByText('30')).toBeInTheDocument()
    expect(within(card).getByText('By review status')).toBeInTheDocument()
    expect(within(card).getByText('unreviewed')).toBeInTheDocument()
    expect(within(card).getByText('7')).toBeInTheDocument()
  })

  it('renders count grids in the backend semantic order, not alphabetically', async () => {
    renderPage()
    const card = await screen.findByRole('region', { name: 'Coverage: OPERA' })

    function gridKeys(title: string): string[] {
      const grid = within(card).getByText(title).parentElement as HTMLElement
      return Array.from(grid.querySelectorAll('dt')).map((dt) => dt.textContent ?? '')
    }
    expect(gridKeys('By confidence')).toEqual(['HIGH', 'MEDIUM', 'LOW'])
    expect(gridKeys('By review status')).toEqual(['reviewed', 'needs-review', 'unreviewed'])
  })

  it('renders the needs-review worklist rows for financial and segments', async () => {
    renderPage()

    const financial = await screen.findByRole('table', {
      name: 'Financial needs-review worklist',
    })
    const rows = within(financial).getAllByRole('row')
    expect(rows).toHaveLength(3) // header + 2 entries
    expect(within(financial).getByText('5105')).toBeInTheDocument()
    expect(within(financial).getByText('Parking')).toBeInTheDocument()
    expect(within(financial).getByText('Ambiguous mapping')).toBeInTheDocument()
    expect(within(financial).getByText('Misc Adjustments')).toBeInTheDocument()

    const segments = screen.getByRole('table', { name: 'Segment needs-review worklist' })
    expect(within(segments).getByText('GRP-X')).toBeInTheDocument()
    expect(screen.getByText('Unmapped codes: WALKIN')).toBeInTheDocument()
  })

  it('lists unmapped statistics labels in a collapsed, informational details element', async () => {
    renderPage()

    const summary = await screen.findByText('2 unmapped labels (informational)')
    const details = summary.closest('details')
    expect(details).not.toBeNull()
    expect(details).not.toHaveAttribute('open') // collapsed by default
    expect(screen.getByText('House Use Rooms')).toBeInTheDocument()
    expect(screen.getByText('Comp Rooms')).toBeInTheDocument()
    // Informational tone: no error styling on the summary.
    expect(summary.className).not.toMatch(/red/)
  })

  it('refetches when the edition selector changes and reads ?edition from the URL', async () => {
    renderPage()
    const select = await screen.findByLabelText('USALI edition')
    expect(select).toHaveValue('12')

    fireEvent.change(select, { target: { value: '11' } })
    expect(await screen.findByRole('region', { name: 'Coverage: OPERA' })).toBeInTheDocument()
    expect(getCoverage).toHaveBeenCalledWith(11)
  })

  it('initializes the edition from the URL search param', async () => {
    renderPage('/coverage?edition=11')
    expect(await screen.findByLabelText('USALI edition')).toHaveValue('11')
    expect(getCoverage).toHaveBeenCalledWith(11)
    expect(getCoverage).not.toHaveBeenCalledWith(12)
  })

  it('clamps an unknown ?edition to the default so the select never renders blank', async () => {
    renderPage('/coverage?edition=99')
    expect(await screen.findByLabelText('USALI edition')).toHaveValue('12')
    expect(getCoverage).toHaveBeenCalledWith(12)
  })
})
