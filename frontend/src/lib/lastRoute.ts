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
 * itself (restoring it would loop), and '/callback' and '/signup' are one-shot
 * URLs that are invalid the second time they are visited — '/callback' is the
 * OIDC code exchange, and '/signup' carries an invite token consumed on first
 * use, so restoring either dead-ends the newly-authenticated owner.
 */
function restorable(href: string): boolean {
  return href !== '/' && !href.startsWith('/callback') && !href.startsWith('/signup')
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

/** The page to open at '/': the last one visited, else the dashboard. */
export function lastRoute(): string {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored !== null && stored !== '' && restorable(stored) ? stored : HOME
  } catch {
    return HOME
  }
}
