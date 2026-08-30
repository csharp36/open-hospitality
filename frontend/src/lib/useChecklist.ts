// THE single TanStack Query key for the onboarding checklist (design §7). The
// /setup page, the sidebar badge and the dashboard card all read it through
// this hook, so they are one fetch and cannot disagree. Do not write
// `useQuery({ queryKey: ['checklist'] })` anywhere else.

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getChecklist } from '../api/checklist'
import type { Checklist, ChecklistItem } from '../api/types'
import type { BadgeTone } from '../components/ui'

export const CHECKLIST_KEY = ['checklist'] as const

export function useChecklist() {
  // staleTime because `Layout` mounts this on every authenticated page: the
  // checklist changes at human speed, so a refetch per navigation buys nothing.
  return useQuery({ queryKey: CHECKLIST_KEY, queryFn: getChecklist, staleTime: 60_000 })
}

export function useInvalidateChecklist() {
  const qc = useQueryClient()
  return () => void qc.invalidateQueries({ queryKey: CHECKLIST_KEY })
}

export interface ChecklistBadge {
  text: string
  tone: BadgeTone
  title: string
}

function plural(n: number): string {
  return n === 1 ? 'item' : 'items'
}

/**
 * The badge every surface shows, or null when there is nothing to show.
 *
 * Null iff `all_clear` — never on `open_count === 0`. An item whose probe
 * raised is `status: 'error'`, which `open_count` does not count, so a tenant
 * whose probes all failed has zero open items and knows nothing. Retiring the
 * badge there would report "finished" at the exact moment the operator most
 * needs to look.
 *
 * For the same reason the text is '!' rather than a numeral whenever anything
 * errored: a count that silently omits the unchecked items reads as progress.
 */
export function badgeLabel(data: Checklist): ChecklistBadge | null {
  if (data.all_clear) return null
  if (data.error_count > 0) {
    const checked = `Could not check ${data.error_count} ${plural(data.error_count)}`
    return {
      text: '!',
      tone: 'danger',
      title:
        data.open_count > 0
          ? `${checked}; ${data.open_count} more still to set up`
          : checked,
    }
  }
  return {
    text: String(data.open_count),
    tone: 'warn',
    title: `${data.open_count} ${plural(data.open_count)} still to set up`,
  }
}

/**
 * Split the items the way the page renders them. Registry order is deliberate
 * on the backend and is preserved within each group.
 */
export function groupItems(items: ChecklistItem[]): {
  required: ChecklistItem[]
  optional: ChecklistItem[]
} {
  return {
    required: items.filter((item) => item.required),
    optional: items.filter((item) => !item.required),
  }
}
