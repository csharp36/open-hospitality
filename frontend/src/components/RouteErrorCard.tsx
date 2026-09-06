// The card a page collapses to when its render throws. Wired as
// `defaultErrorComponent` in router.tsx; that the shell around the errored
// page survives the swap is pinned by GlPage.test.tsx "contains a render-time
// throw: the shell survives and the message shows".

import type { ErrorComponentProps } from '@tanstack/react-router'

import { errorMessage } from '../lib/errors'
import { Card } from './ui'

export default function RouteErrorCard({ error }: ErrorComponentProps) {
  return (
    <Card role="alert">
      <p className="text-sm font-medium text-ink">This page failed to render.</p>
      {/* The message verbatim: a deploy-skew throw ("unhandled account_type:
          …", "not a fixed-point decimal: …") is only debuggable by name. */}
      <p className="mt-1 text-sm text-danger-red">{errorMessage(error)}</p>
    </Card>
  )
}
