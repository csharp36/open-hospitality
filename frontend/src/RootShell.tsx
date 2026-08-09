// Root shell + OIDC callback page. Kept out of router.tsx so that module stays
// component-free (it exports createAppRouter + route types), clearing oxlint
// react(only-export-components) / React Fast Refresh. This file exports only
// components.

import { useEffect, useState } from 'react'
import { Outlet, useRouterState } from '@tanstack/react-router'

import Layout from './Layout'
import { RequireAuth } from './auth/AuthProvider'
import { login, userManager } from './auth/oidc'
import { rememberRoute } from './lib/lastRoute'

/**
 * OIDC redirect landing. Completes the Authorization Code exchange and bounces
 * to the app root, which restores the page you were on before the sign-in
 * redirect (see lib/lastRoute). It must render WITHOUT the auth guard —
 * RootShell renders a bare <Outlet/> for this path so the callback never
 * triggers RequireAuth's login-redirect loop.
 *
 * On a persistent exchange failure (clock skew, misconfig, cleared state) we do
 * NOT auto-bounce to '/', which would silently re-enter the login loop.
 * Instead we surface a manual retry, mirroring RequireAuth's error affordance.
 */
export function CallbackPage() {
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    userManager
      .signinRedirectCallback()
      .then(() => window.location.replace('/'))
      .catch((err: unknown) => {
        console.error('OIDC callback failed', err)
        setFailed(true)
      })
  }, [])
  if (failed)
    return (
      <div className="p-6 text-ink-muted">
        Sign-in failed —{' '}
        <button
          type="button"
          className="underline"
          onClick={() => {
            login().catch(() => {})
          }}
        >
          try again
        </button>
      </div>
    )
  return <div className="p-6 text-ink-muted">Completing sign in…</div>
}

/**
 * /callback and /kiosk render unguarded (bare Outlet); every other route is
 * wrapped in <RequireAuth><Layout/></RequireAuth> so all authenticated pages
 * require login. Guarding here (rather than via a nested layout route) keeps
 * every page a direct child of the root route, so their route ids ('/',
 * '/coverage', …) — which pages resolve via getRouteApi — stay unchanged.
 *
 * /callback and /kiosk render unguarded: the callback completes OIDC, and the
 * kiosk authenticates by DEVICE token (no operator session exists on an iPad).
 */
export function RootShell() {
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  // href, not pathname: the restore target keeps the search params, so a
  // reload returns to the same statement/month, not just the same page.
  const href = useRouterState({ select: (s) => s.location.href })
  useEffect(() => {
    rememberRoute(href)
  }, [href])
  if (pathname === '/callback' || pathname === '/kiosk') return <Outlet />
  return (
    <RequireAuth>
      <Layout />
    </RequireAuth>
  )
}
