import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { createAppRouter } from '../router'

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
})
