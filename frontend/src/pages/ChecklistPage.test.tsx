import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'
import { createAppRouter } from '../router'
import { ApiError } from '../api/client'
import type { ChecklistItem } from '../api/types'

vi.mock('../api/checklist', () => ({
  getChecklist: vi.fn(), dismissItem: vi.fn(), restoreItem: vi.fn(),
}))
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getMe: vi.fn(), getProperties: vi.fn(),
}))
import { dismissItem, getChecklist, restoreItem } from '../api/checklist'
import { getMe, getProperties } from '../api/client'

function item(over: Partial<ChecklistItem> = {}): ChecklistItem {
  return {
    key: 'first_report', title: 'Upload your first PMS report',
    description: 'Drop a night-audit export.', required: true,
    where: '/upload', unavailable_reason: null, status: 'open', detail: null,
    ...over,
  }
}

function renderSetup() {
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/setup'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
  vi.mocked(getProperties).mockResolvedValue([])
})

describe('ChecklistPage', () => {
  it('groups required and optional items under their own headings', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [
        item(),
        item({ key: 'team', title: 'Invite your team', required: false, where: '/employees' }),
      ],
      open_count: 2, error_count: 0, all_clear: false,
    })
    renderSetup()

    const required = await screen.findByRole('region', { name: /required/i })
    const optional = screen.getByRole('region', { name: /optional/i })
    expect(within(required).getByText(/upload your first pms report/i)).toBeInTheDocument()
    expect(within(required).queryByText(/invite your team/i)).toBeNull()
    expect(within(optional).getByText(/invite your team/i)).toBeInTheDocument()
    expect(within(optional).queryByText(/upload your first pms report/i)).toBeNull()
  })

  it('links an item that has a `where`', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({ where: '/upload' })], open_count: 1, error_count: 0, all_clear: false,
    })
    renderSetup()
    const link = await screen.findByRole('link', { name: /upload your first pms report/i })
    expect(link).toHaveAttribute('href', '/upload')
  })

  // D-B4.8. The item is un-closeable today, and says so — it is never a link
  // to a page where the feature is invisible.
  it('renders an item with no `where` as a non-link carrying its reason', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({
        key: 'payroll', title: 'Connect payroll', required: false, where: null,
        unavailable_reason: 'No connect surface yet — per-tenant integration setup arrives with OH-17.',
      })],
      open_count: 1, error_count: 0, all_clear: false,
    })
    renderSetup()
    expect(await screen.findByText(/connect payroll/i)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /connect payroll/i })).toBeNull()
    expect(screen.getByText(/arrives with OH-17/i)).toBeInTheDocument()
  })

  // The reason is deliberately status-neutral: a demand_feed a tenant was
  // provisioned with probes `done` while still having had no way to connect
  // it, so Done and the reason legitimately appear together.
  it('keeps the reason on a done item that still has no connect surface', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({
        key: 'demand_feed', title: 'Connect a demand feed', required: false,
        where: null, unavailable_reason: 'No connect surface yet — arrives with OH-17.',
        status: 'done',
      })],
      open_count: 0, error_count: 0, all_clear: true,
    })
    renderSetup()
    expect(await screen.findByText(/^done$/i)).toBeInTheDocument()
    expect(screen.getByText(/arrives with OH-17/i)).toBeInTheDocument()
  })

  it('renders an errored item as unchecked, never as progress', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({ status: 'error', detail: 'OperationalError' })],
      open_count: 0, error_count: 1, all_clear: false,
    })
    renderSetup()
    // The word alone is not the invariant — a green tick beside "Could not
    // check" would still read as progress, which design §8 forbids
    // absolutely. Pin the tone and the glyph, not just the wording.
    const badge = await screen.findByText('Could not check')
    expect(badge.className).toMatch(/red/)
    expect(badge.className).not.toMatch(/green/)
    // Scoped to the row: the sidebar badge also renders '!' on an errored
    // checklist, and the glyph under test is the one beside the item.
    const row = screen.getByRole('region', { name: /required/i })
    expect(within(row).getByText('!').className).toMatch(/red/)
    expect(screen.queryByText('✓')).toBeNull()
    expect(screen.queryByText(/^done$/i)).toBeNull()
    expect(screen.getByText(/OperationalError/)).toBeInTheDocument()
  })

  // The only status with no other fixture, and Task 5 puts its Restore
  // control on exactly these rows.
  it('renders a dismissed item in the neutral tone', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({
        key: 'team', title: 'Invite your team', required: false,
        where: '/employees', status: 'dismissed',
      })],
      open_count: 0, error_count: 0, all_clear: true,
    })
    renderSetup()
    const badge = await screen.findByText('Dismissed')
    expect(badge.className).toMatch(/surface-sunken/)
    expect(badge.className).not.toMatch(/green|amber|red|blue/)
  })

  // The load-bearing gate: zero open items with a failed probe is NOT
  // finished. Gating on open_count === 0 would say setup is complete at the
  // exact moment nothing whatsoever is known.
  it('does not claim setup is finished when every probe failed', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({ status: 'error', detail: 'OperationalError' })],
      open_count: 0, error_count: 1, all_clear: false,
    })
    renderSetup()
    expect(await screen.findByText('Could not check')).toBeInTheDocument()
    expect(screen.queryByText(/nothing left to set up/i)).toBeNull()
  })

  // §6 puts error_count on the wire so a client can tell "4 things to do"
  // from "4 things we could not check" AND SAY SO. This is where it says so.
  it('summarises the counts, keeping could-not-check apart from to-do', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [
        item(),
        item({ key: 'team', title: 'Invite your team', required: false, status: 'error', detail: 'OperationalError' }),
      ],
      open_count: 1, error_count: 1, all_clear: false,
    })
    renderSetup()
    // Both halves in one sentence. The exact wording is badgeLabel's own and
    // is pinned in useChecklist.test.tsx; what this pins is that /setup shows
    // it at all, and that the errored item is not folded into the to-do count.
    const summary = await screen.findByText(/could not check 1 item;/i)
    expect(summary.textContent).toMatch(/1 .*still to set up/i)
  })

  it('says setup is finished when all_clear', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item({ status: 'done' })], open_count: 0, error_count: 0, all_clear: true,
    })
    renderSetup()
    expect(await screen.findByText(/nothing left to set up/i)).toBeInTheDocument()
    expect(screen.queryByText(/still to set up/i)).toBeNull()
  })

  it('shows a loud failure when the checklist itself cannot be fetched', async () => {
    vi.mocked(getChecklist).mockRejectedValue(new ApiError(503, 'upstream down'))
    renderSetup()
    expect(await screen.findByText(/upstream down/i)).toBeInTheDocument()
  })
})

// Three of the seven items have no connect surface until OH-17 (D-B4.8), so
// dismissal is the only action an operator can take on them: without it those
// rows are dead ends and `all_clear` is unreachable for every tenant.
describe('ChecklistPage — dismissal', () => {
  function payroll(over: Partial<ChecklistItem> = {}): ChecklistItem {
    return item({
      key: 'payroll', title: 'Connect payroll', required: false, where: null,
      unavailable_reason: 'No connect surface yet — arrives with OH-17.',
      ...over,
    })
  }

  it('an org admin can dismiss an optional item, and the list refetches', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    vi.mocked(dismissItem).mockResolvedValue(undefined)
    // Open on the first read, dismissed on the refetch. Nothing but the shared
    // key's invalidation can turn Dismiss into Restore, so the second
    // assertion is what pins the refetch rather than just the write.
    vi.mocked(getChecklist)
      .mockResolvedValueOnce({
        items: [payroll()], open_count: 1, error_count: 0, all_clear: false,
      })
      .mockResolvedValue({
        items: [payroll({ status: 'dismissed' })], open_count: 0, error_count: 0, all_clear: true,
      })
    renderSetup()

    await userEvent.click(await screen.findByRole('button', { name: /dismiss connect payroll/i }))
    expect(dismissItem).toHaveBeenCalledWith('payroll')
    expect(
      await screen.findByRole('button', { name: /restore connect payroll/i }),
    ).toBeInTheDocument()
  })

  // D-B4.4 makes a dismissal a two-way decision, so a UI that could only
  // dismiss would be a one-way door over it. This is also the one place a
  // copy-pasted mutation pair would silently call `dismissItem` twice.
  it('an org admin can restore a dismissed item, and the list refetches', async () => {
    vi.mocked(restoreItem).mockResolvedValue(undefined)
    vi.mocked(getChecklist)
      .mockResolvedValueOnce({
        items: [payroll({ status: 'dismissed' })], open_count: 0, error_count: 0, all_clear: true,
      })
      .mockResolvedValue({
        items: [payroll()], open_count: 1, error_count: 0, all_clear: false,
      })
    renderSetup()

    await userEvent.click(await screen.findByRole('button', { name: /restore connect payroll/i }))
    expect(restoreItem).toHaveBeenCalledWith('payroll')
    expect(dismissItem).not.toHaveBeenCalled()
    expect(
      await screen.findByRole('button', { name: /dismiss connect payroll/i }),
    ).toBeInTheDocument()
  })

  // The server answers 422 here (design §6), so offering the control would be
  // offering a button that can only fail.
  it('offers no dismiss control on a REQUIRED item', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [item()], open_count: 1, error_count: 0, all_clear: false,
    })
    renderSetup()

    const required = await screen.findByRole('region', { name: /required/i })
    expect(within(required).getByText(/upload your first pms report/i)).toBeInTheDocument()
    expect(within(required).queryByRole('button', { name: /dismiss/i })).toBeNull()
  })

  // The write would succeed and the row would persist, but `done` outranks a
  // dismissal (D-B4.4) — so the row does not move and nothing is said. A
  // button whose effect is invisible is the silent no-op ADR-010 refuses; the
  // server's 422 on a required item at least says something.
  it('offers no dismiss control on an item that is already done', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [payroll({ status: 'done' })], open_count: 0, error_count: 0, all_clear: true,
    })
    renderSetup()

    const optional = await screen.findByRole('region', { name: /optional/i })
    expect(within(optional).getByText(/connect payroll/i)).toBeInTheDocument()
    expect(within(optional).queryByRole('button', { name: /dismiss/i })).toBeNull()
  })

  // The asymmetry with `done` is deliberate, and this pins it against a "just
  // hide it unless open" tidy-up. The probe raised before the override was
  // consulted, so a dismissal here masks nothing — the row goes on reading
  // "Could not check" and the stored decision takes effect only once the probe
  // recovers. Withholding the one available action during an outage would
  // strand the operator on exactly the dead end D-B4.8 exists to kill.
  it('keeps the dismiss control on an item whose probe failed', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [payroll({ status: 'error', detail: 'OperationalError' })],
      open_count: 0, error_count: 1, all_clear: false,
    })
    renderSetup()

    expect(
      await screen.findByRole('button', { name: /dismiss connect payroll/i }),
    ).toBeInTheDocument()
  })

  // "We don't use payroll" is a standing commitment about the tenant, not a
  // per-user preference, so the endpoint requires ORG_ADMIN (design §6) and the
  // page must not offer what the server would refuse.
  it('offers no dismiss control to a non-admin', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    vi.mocked(getChecklist).mockResolvedValue({
      items: [payroll()], open_count: 1, error_count: 0, all_clear: false,
    })
    renderSetup()

    // Awaited first: queryByRole against a page that has not rendered yet
    // would pass for the wrong reason.
    const optional = await screen.findByRole('region', { name: /optional/i })
    expect(within(optional).getByText(/connect payroll/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull()
  })

  // ADR-010: a refusal is never a silent no-op. The control is withheld from
  // required items, but the branch stays handled — the endpoint is reachable
  // and a registry edit could move a key across the required line.
  it('surfaces a refused dismissal instead of silently doing nothing', async () => {
    vi.mocked(getChecklist).mockResolvedValue({
      items: [payroll()], open_count: 1, error_count: 0, all_clear: false,
    })
    vi.mocked(dismissItem).mockRejectedValue(new ApiError(422, 'first_report is required'))
    renderSetup()

    await userEvent.click(await screen.findByRole('button', { name: /dismiss connect payroll/i }))
    // Exact, not a loose regex: `ApiError.message` is "HTTP 422: first_report
    // is required", so an unanchored match would pass just as happily against
    // `error.message` and leave the house convention — an ApiError renders its
    // bare `detail` — unpinned at the one place this page relies on it.
    expect(await screen.findByText('first_report is required')).toBeInTheDocument()
  })

  // D-B4.5 makes the endpoint idempotent, so a double-click costs a redundant
  // PUT rather than corruption — but the disabled-while-pending guard was an
  // explicit constraint, and without a test it can be deleted silently.
  it('does not fire a second write while the first is still in flight', async () => {
    let settle!: () => void
    vi.mocked(dismissItem).mockReturnValue(
      new Promise<void>((resolve) => {
        settle = resolve
      }),
    )
    vi.mocked(getChecklist).mockResolvedValue({
      items: [payroll()], open_count: 1, error_count: 0, all_clear: false,
    })
    renderSetup()

    const button = await screen.findByRole('button', { name: /dismiss connect payroll/i })
    await userEvent.click(button)
    expect(button).toBeDisabled()
    await userEvent.click(button)
    expect(dismissItem).toHaveBeenCalledTimes(1)

    settle()
    await waitFor(() => expect(button).toBeEnabled())
  })
})
