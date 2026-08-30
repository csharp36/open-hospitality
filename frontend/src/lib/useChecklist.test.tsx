// The shared spine of the three checklist surfaces: one query key (so two
// consumers are one fetch), and the derivations they must agree on.
//
// The load-bearing case is `all_clear` vs `open_count`. They diverge only when
// a probe raised: `status: 'error'` is not counted in `open_count`, so a tenant
// whose probes all failed reads `open_count === 0` while nothing is known.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import type { Checklist, ChecklistItem } from '../api/types'

vi.mock('../api/checklist', () => ({ getChecklist: vi.fn() }))
import { getChecklist } from '../api/checklist'
import { CHECKLIST_KEY, badgeLabel, groupItems, useChecklist } from './useChecklist'

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

function makeItem(over: Partial<ChecklistItem> = {}): ChecklistItem {
  return {
    key: 'connect_pms',
    title: 'Connect your PMS',
    description: 'Upload a nightly export.',
    required: true,
    where: '/uploads',
    unavailable_reason: null,
    status: 'open',
    detail: null,
    ...over,
  }
}

function makeChecklist(over: Partial<Checklist> = {}): Checklist {
  return { items: [], open_count: 0, error_count: 0, all_clear: true, ...over }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useChecklist', () => {
  it('two consumers of the hook cause exactly ONE fetch', async () => {
    vi.mocked(getChecklist).mockResolvedValue(
      makeChecklist({ open_count: 2, all_clear: false }),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const w = wrapper(qc)
    renderHook(() => useChecklist(), { wrapper: w })
    renderHook(() => useChecklist(), { wrapper: w })
    await waitFor(() => expect(getChecklist).toHaveBeenCalledTimes(1))
    expect(CHECKLIST_KEY).toEqual(['checklist'])
  })
})

describe('badgeLabel', () => {
  it('is null when all_clear — the badge retires at zero', () => {
    expect(badgeLabel(makeChecklist())).toBeNull()
  })

  it('counts the open items', () => {
    expect(badgeLabel(makeChecklist({ open_count: 3, all_clear: false }))).toEqual({
      text: '3',
      tone: 'warn',
      title: '3 items still to set up',
    })
  })

  // THE divergence case. A total probe failure leaves open_count at 0 while
  // nothing at all is known. A badge reading "0" — or no badge — would say
  // "finished" at the exact moment the operator most needs to look.
  it('shows "!" and never "0" when every probe failed', () => {
    const badge = badgeLabel(makeChecklist({ error_count: 3, all_clear: false }))
    expect(badge).not.toBeNull()
    expect(badge!.text).toBe('!')
    expect(badge!.tone).toBe('danger')
    expect(badge!.title).toMatch(/could not check/i)
  })

  it('leads with the errors when items are both open and unchecked', () => {
    const badge = badgeLabel(makeChecklist({ open_count: 2, error_count: 1, all_clear: false }))
    expect(badge!.tone).toBe('danger')
    expect(badge!.title).toMatch(/could not check/i)
    // The open items are still named — the operator is not told the errors are
    // all that is left.
    expect(badge!.title).toMatch(/2/)
  })

  it('says "item" once and "items" otherwise', () => {
    expect(badgeLabel(makeChecklist({ open_count: 1, all_clear: false }))!.title).toBe(
      '1 item still to set up',
    )
    expect(badgeLabel(makeChecklist({ error_count: 1, all_clear: false }))!.title).toBe(
      'Could not check 1 item',
    )
  })
})

describe('groupItems', () => {
  it('splits on `required`', () => {
    const items = [
      makeItem({ key: 'a', required: true }),
      makeItem({ key: 'b', required: false }),
      makeItem({ key: 'c', required: true }),
    ]
    const { required, optional } = groupItems(items)
    expect(required.map((i) => i.key)).toEqual(['a', 'c'])
    expect(optional.map((i) => i.key)).toEqual(['b'])
  })

  // The backend registry is deliberately ordered and the page renders it as-is:
  // grouping must not sort, dedupe or otherwise reshuffle.
  it('preserves registry order within each group', () => {
    const items = [
      makeItem({ key: 'z', required: true }),
      makeItem({ key: 'y', required: false }),
      makeItem({ key: 'a', required: true }),
      makeItem({ key: 'b', required: false }),
    ]
    const { required, optional } = groupItems(items)
    expect(required.map((i) => i.key)).toEqual(['z', 'a'])
    expect(optional.map((i) => i.key)).toEqual(['y', 'b'])
  })

  it('returns both groups empty rather than undefined for no items', () => {
    expect(groupItems([])).toEqual({ required: [], optional: [] })
  })
})
