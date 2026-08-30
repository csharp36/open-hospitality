// Session continuity for the entry route.
//
// The app remembers the last page you were on so that landing on '/' — a
// reload of the bare origin, or the post-login bounce, which always returns to
// '/' — puts you back where you were. With nothing remembered (a genuine first
// visit) the entry route opens the dashboard.

const STORAGE_KEY = 'usali.last-route'
const HOME = '/dashboard'

/**
 * Paths that must never become the restore target: '/' is the entry route
 * itself (restoring it would loop); '/callback' and '/signup' are one-shot
 * URLs that are invalid the second time they are visited — '/callback' is the
 * OIDC code exchange, and '/signup' carries an invite token consumed on first
 * use, so restoring either dead-ends the newly-authenticated owner; and '/try'
 * is the public marketing route — remembering it would strand a freshly
 * authenticated user back on the anonymous preview page.
 */
function restorable(href: string): boolean {
  return (
    href !== '/' &&
    !href.startsWith('/callback') &&
    !href.startsWith('/signup') &&
    !href.startsWith('/try')
  )
}

/** Record the current location (pathname + search) as the restore target. */
export function rememberRoute(href: string): void {
  if (!restorable(href)) return
  try {
    localStorage.setItem(STORAGE_KEY, href)
  } catch {
    // Storage can be unavailable (private mode, blocked cookies). Losing
    // continuity is acceptable; throwing out of a render effect is not.
  }
}

/**
 * The page to open at '/': the last one visited, else the dashboard.
 *
 * `resolves` answers "does the SPA actually serve this href?" and is checked
 * at RESTORE time, not only when the route was remembered. It is a required
 * argument because forgetting it is the bug: a remembered href the router
 * cannot match makes '/' redirect to Not Found on EVERY bare-origin load and
 * every post-login return, and the operator cannot escape it without typing a
 * URL. That is not hypothetical — the checklist's three integration items
 * point at `/integrations`, a page whose frontend is a later plan, so one
 * click from `/setup` writes an unservable href into `localStorage`.
 *
 * It is a predicate rather than another entry in `restorable`'s list on
 * purpose: a denylist would have to name every route that does not exist, and
 * would go stale in the other direction the moment such a page ships. Asking
 * the router what it serves (`router.tsx`'s `isServedPath`) also covers
 * `localStorage` left behind by any future route REMOVAL, which no hand-kept
 * list ever will.
 */
export function lastRoute(resolves: (href: string) => boolean): string {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored !== null && stored !== '' && restorable(stored) && resolves(stored)
      ? stored
      : HOME
  } catch {
    return HOME
  }
}
