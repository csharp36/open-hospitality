// Auth context + hook, kept in a NON-component module so AuthProvider.tsx can
// export only components (clears oxlint react(only-export-components)).
//
// The <AuthProvider> is the SINGLE source of truth for auth state: it is the one
// place that calls getUser() and subscribes to userManager events. Consumers
// (including the RequireAuth guard) read state through useAuth() — so nesting
// <RequireAuth> inside <AuthProvider> yields exactly one auth-state machine.
import { createContext, useContext } from 'react'
import type { User } from 'oidc-client-ts'
import { logout } from './oidc'

export type AuthState = { user: User | null; loading: boolean }
export type AuthContextValue = AuthState & { logout: () => void }

export const AuthContext = createContext<AuthContextValue>({
  user: null,
  loading: true,
  // Fire-and-forget, but never let a rejected sign-out become an unhandled
  // rejection. The real provider supplies its own wrapped logout.
  logout: () => {
    void logout().catch(() => {})
  },
})

export function useAuth(): AuthContextValue {
  return useContext(AuthContext)
}
