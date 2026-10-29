// Session continuity for the entry route.
//
// The app remembers the last page you were on so that landing on '/' — a
// reload of the bare origin, or the post-login bounce, which always returns to
// '/' — puts you back where you were. With nothing remembered (a genuine first
// visit) the entry route opens the dashboard.
//
// The memory is PER SUBJECT (issue #103). It used to be per browser: nothing
// cleared it on logout or signup, so a freshly provisioned owner inherited the
// previous person's page, and on a shared machine the next login learned where
// the last one had been. Storing the authenticated subject beside the route and
// restoring only on a match covers logout, user-switching and signup with one
// rule, while the intended case — the same person reloading — still works.

const STORAGE_KEY = 'usali.last-route'
const HOME = '/dashboard'

interface Remembered {
  href: string
  subject: string
}

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

/**
 * Record the current location (pathname + search) as the restore target for
 * `subject`. With no subject (not signed in yet, or on an unguarded page) there
 * is nothing to attribute the route to, so nothing is written — an anonymous
 * visit must not overwrite a signed-in user's place, and must never be
 * restored for whoever signs in next.
 */
export function rememberRoute(href: string, subject: string | null | undefined): void {
  if (!subject || !restorable(href)) return
  const entry: Remembered = { href, subject }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entry))
  } catch {
    // Storage can be unavailable (private mode, blocked cookies). Losing
    // continuity is acceptable; throwing out of a render effect is not.
  }
}

function read(): Remembered | null {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (raw === null || raw === '') return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (
      parsed !== null && typeof parsed === 'object' &&
      typeof (parsed as Remembered).href === 'string' &&
      typeof (parsed as Remembered).subject === 'string'
    ) {
      return parsed as Remembered
    }
  } catch {
    // The pre-#103 format was a bare href string with no owner. It cannot be
    // attributed to anyone, so it is treated as nothing remembered.
  }
  return null
}

/**
 * The page to open at '/' for `subject`: the last one THAT SUBJECT visited,
 * else the dashboard. A route remembered by someone else is never returned.
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
export function lastRoute(
  resolves: (href: string) => boolean,
  subject: string | null | undefined,
): string {
  if (!subject) return HOME
  try {
    const stored = read()
    return stored !== null && stored.subject === subject && restorable(stored.href) && resolves(stored.href)
      ? stored.href
      : HOME
  } catch {
    return HOME
  }
}
