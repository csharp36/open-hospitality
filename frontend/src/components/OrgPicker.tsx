// The active-org picker (Pillar L decision 6). Shown ONLY when the token's
// `organization` claim lists more than one org — a single-org user sees NOTHING
// new (their sole org is active automatically, and its alias still rides every
// request as X-Active-Org). Selecting an alias makes it active through the
// shared activeOrg store, which client.ts reads at the header seam.

import { useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { useAuth } from '../auth/authContext'
import {
  getSelectedOrg,
  orgAliasesFromToken,
  resolveActiveOrg,
  setActiveOrg,
  subscribeActiveOrg,
} from '../auth/activeOrg'
import { controlClass } from './ui'

export default function OrgPicker() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  // Re-render on selection changes (the store is the single source of truth,
  // shared with client.ts's header injection).
  useSyncExternalStore(subscribeActiveOrg, getSelectedOrg)

  const orgs = orgAliasesFromToken(user?.access_token ?? null)
  // Single-org (or org-less) users get no UI at all — the invariant pinned in
  // the vitest suite.
  if (orgs.length <= 1) return null

  const active = resolveActiveOrg(orgs)
  const onSwitch = (alias: string) => {
    setActiveOrg(alias)
    // Every cached query was fetched under the PREVIOUS active org (or under
    // no header at all, having 400'd). Invalidate the whole cache so the shell
    // refetches under the newly-active org instead of showing a stale error /
    // another org's data.
    void queryClient.invalidateQueries()
  }
  return (
    <label className="flex items-center gap-1.5 text-sm">
      <span className="text-xs font-medium text-ink-muted">Org</span>
      <select
        aria-label="Active organization"
        className={controlClass}
        value={active ?? ''}
        onChange={(e) => onSwitch(e.target.value)}
      >
        {/* Placeholder only while nothing is active yet (ambiguous multi-org,
            no selection): forces a deliberate choice rather than guessing. */}
        {active === null && (
          <option value="" disabled>
            Select org…
          </option>
        )}
        {orgs.map((alias) => (
          <option key={alias} value={alias}>
            {alias}
          </option>
        ))}
      </select>
    </label>
  )
}
