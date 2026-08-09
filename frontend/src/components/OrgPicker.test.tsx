import { fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import OrgPicker from './OrgPicker'
import { AuthContext } from '../auth/authContext'

beforeEach(() => sessionStorage.clear())

function makeToken(orgs: string[]): string {
  const b64 = btoa(JSON.stringify({ organization: orgs, sub: 'u' }))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  return `header.${b64}.sig`
}

function withUser(orgs: string[], children: ReactNode, queryClient?: QueryClient) {
  const user = { access_token: makeToken(orgs), expired: false } as never
  const client = queryClient ?? new QueryClient()
  return (
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={{ user, loading: false, logout: () => {} }}>
        {children}
      </AuthContext.Provider>
    </QueryClientProvider>
  )
}

describe('OrgPicker', () => {
  it('renders NOTHING for a single-org user (single-org UX unchanged)', () => {
    render(withUser(['solo-org'], <OrgPicker />))
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Active organization')).not.toBeInTheDocument()
  })

  it('renders nothing when the token carries no organization claim', () => {
    render(withUser([], <OrgPicker />))
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('renders a picker with an option per org when the claim lists >1', () => {
    render(withUser(['org-a', 'org-b', 'org-c'], <OrgPicker />))
    const select = screen.getByLabelText('Active organization')
    expect(select).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'org-a' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'org-b' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'org-c' })).toBeInTheDocument()
  })

  it('selecting an org makes it the active value and invalidates the query cache', () => {
    const client = new QueryClient()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    render(withUser(['org-a', 'org-b'], <OrgPicker />, client))
    const select = screen.getByLabelText<HTMLSelectElement>('Active organization')
    fireEvent.change(select, { target: { value: 'org-b' } })
    expect(select.value).toBe('org-b')
    // The org switch must refetch under the newly-active org, not leave the
    // shell on the previous org's (or 400'd) cache.
    expect(invalidate).toHaveBeenCalled()
  })
})
