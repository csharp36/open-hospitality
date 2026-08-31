import { describe, expect, it } from 'vitest'
import { isServedPath } from './router'

/**
 * The frontend half of the dead-link pair. Its counterpart is
 * `tests/test_checklist.py::test_every_item_route_is_pinned`, which pins the
 * same list in Python. Neither side can see the other's language, so both
 * pin it and a new checklist route fails the Python half first.
 *
 * These are the `where` values `usali.checklist.ITEMS` can return. A link to
 * a path the router does not serve renders as a dead link with no error —
 * which is how /integrations shipped on the checklist before it was a route.
 */
const CHECKLIST_ROUTES = [
  '/employees',
  '/integrations',
  '/property-config',
  '/upload',
]

describe('checklist routes', () => {
  it('are all served by the SPA', () => {
    for (const path of CHECKLIST_ROUTES) {
      expect(isServedPath(path), path).toBe(true)
    }
  })
})
