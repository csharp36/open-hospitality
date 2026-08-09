import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import * as oidc from './oidc'
import { AuthProvider, RequireAuth } from './AuthProvider'
import { useAuth } from './authContext'

// RequireAuth reads state from the provider context, so it is exercised through
// the real composition: <RequireAuth> nested inside <AuthProvider>, both driven
// by the same mocked oidc.getUser / oidc.login.
describe('RequireAuth', () => {
  it('renders children when a user is present', async () => {
    vi.spyOn(oidc, 'getUser').mockResolvedValue({ expired: false } as never)
    render(
      <AuthProvider>
        <RequireAuth><p>secret</p></RequireAuth>
      </AuthProvider>,
    )
    await waitFor(() => expect(screen.getByText('secret')).toBeInTheDocument())
  })

  it('triggers login when unauthenticated', async () => {
    vi.spyOn(oidc, 'getUser').mockResolvedValue(null)
    const spy = vi.spyOn(oidc, 'login').mockResolvedValue()
    render(
      <AuthProvider>
        <RequireAuth><p>secret</p></RequireAuth>
      </AuthProvider>,
    )
    await waitFor(() => expect(spy).toHaveBeenCalled())
    expect(screen.queryByText('secret')).not.toBeInTheDocument()
  })

  it('shows Loading… before getUser resolves', () => {
    vi.spyOn(oidc, 'getUser').mockReturnValue(new Promise(() => {}) as never)
    render(
      <AuthProvider>
        <RequireAuth><p>secret</p></RequireAuth>
      </AuthProvider>,
    )
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByText('secret')).not.toBeInTheDocument()
  })

  it('surfaces a retry affordance when login rejects', async () => {
    vi.spyOn(oidc, 'getUser').mockResolvedValue(null)
    vi.spyOn(oidc, 'login').mockRejectedValue(new Error('discovery failed'))
    render(
      <AuthProvider>
        <RequireAuth><p>secret</p></RequireAuth>
      </AuthProvider>,
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument(),
    )
    expect(screen.queryByText('secret')).not.toBeInTheDocument()
  })
})

describe('AuthProvider', () => {
  it('exposes the resolved user through useAuth', async () => {
    vi.spyOn(oidc, 'getUser').mockResolvedValue({ expired: false } as never)
    function Probe() {
      const { user, loading } = useAuth()
      if (loading) return <p>pending</p>
      return <p>authed:{user ? 'yes' : 'no'}</p>
    }
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    await waitFor(() => expect(screen.getByText('authed:yes')).toBeInTheDocument())
  })

  it('subscribes to userLoaded on mount and cleans up on unmount', async () => {
    vi.spyOn(oidc, 'getUser').mockResolvedValue(null)
    const add = vi.spyOn(oidc.userManager.events, 'addUserLoaded')
    const remove = vi.spyOn(oidc.userManager.events, 'removeUserLoaded')
    const { unmount } = render(
      <AuthProvider>
        <p>app</p>
      </AuthProvider>,
    )
    await waitFor(() => expect(add).toHaveBeenCalled())
    unmount()
    expect(remove).toHaveBeenCalled()
  })
})
