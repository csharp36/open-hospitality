import { useCallback, useEffect, useState, type ReactNode } from 'react'
import type { User } from 'oidc-client-ts'
import { getUser, login, logout, userManager } from './oidc'
import { AuthContext, useAuth, type AuthContextValue, type AuthState } from './authContext'

// AuthProvider is the single owner of auth state: it calls getUser() once on
// mount and subscribes to userManager's userLoaded events. Everything else
// (including RequireAuth) reads through useAuth(), so there is exactly one
// auth-state machine even when the guard is nested inside the provider.
export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ user: null, loading: true })

  useEffect(() => {
    let active = true
    getUser()
      .then((user) => {
        if (active) setState({ user, loading: false })
      })
      .catch(() => {
        if (active) setState({ user: null, loading: false })
      })
    const onLoaded = (user: User) => setState({ user, loading: false })
    userManager.events.addUserLoaded(onLoaded)
    return () => {
      active = false
      userManager.events.removeUserLoaded(onLoaded)
    }
  }, [])

  const value: AuthContextValue = {
    ...state,
    logout: () => {
      void logout().catch(() => {})
    },
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// Guard: renders children only when authenticated; otherwise kicks off login.
// Reads user/loading from the provider context (single source of truth). login()
// is fire-and-forget navigation, but a rejected redirect (e.g. OIDC discovery
// failure) must not be swallowed — it surfaces a retry affordance instead of
// leaving the user stuck on "Redirecting…".
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const [error, setError] = useState(false)

  const startLogin = useCallback(() => {
    setError(false)
    login().catch(() => setError(true))
  }, [])

  useEffect(() => {
    if (!loading && !user) startLogin()
  }, [loading, user, startLogin])

  if (loading) return <div className="p-6 text-ink-muted">Loading…</div>
  if (error)
    return (
      <div className="p-6 text-ink-muted">
        Sign-in failed —{' '}
        <button type="button" className="underline" onClick={startLogin}>
          retry
        </button>
      </div>
    )
  if (!user) return <div className="p-6 text-ink-muted">Redirecting to sign in…</div>
  return <>{children}</>
}
