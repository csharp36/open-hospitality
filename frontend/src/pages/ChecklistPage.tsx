// The onboarding checklist's permanent home (Track B/B4, design §7): what is
// still unconfigured, probed on read by the backend rather than read from a
// stored status that can outlive the thing it describes (D-B4.1).
//
// Unlike every other page this one takes NO property — the checklist is
// org-scoped, so there is no `useGlobalProperty()` and no "no property
// selected yet" state to render.

import { useMutation, useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { Badge, Card, PageHeader, sectionHeadClass, type BadgeTone } from '../components/ui'
import type { ChecklistItem, ChecklistStatus } from '../api/types'
import { dismissItem, restoreItem } from '../api/checklist'
import { getMe } from '../api/client'
import { badgeLabel, groupItems, useChecklist, useInvalidateChecklist } from '../lib/useChecklist'
import { errorMessage } from '../lib/errors'
import { hasRole } from '../lib/roles'

// `error` reads "Could not check" — not "Failed", and never a tick — because
// the operator must be able to tell "4 things to do" from "4 things we could
// not check" (design §6, §8). A failed probe never renders as progress.
const STATUS: Record<ChecklistStatus, { tone: BadgeTone; word: string; glyph: string }> = {
  done: { tone: 'ok', word: 'Done', glyph: '✓' },
  open: { tone: 'warn', word: 'To do', glyph: '○' },
  dismissed: { tone: 'neutral', word: 'Dismissed', glyph: '–' },
  error: { tone: 'danger', word: 'Could not check', glyph: '!' },
}

const glyphTone: Record<ChecklistStatus, string> = {
  done: 'text-ok-green',
  open: 'text-warn-amber',
  dismissed: 'text-ink-faint',
  error: 'text-danger-red',
}

export default function ChecklistPage() {
  const checklist = useChecklist()
  const me = useQuery({ queryKey: ['me'], queryFn: getMe })
  // Mirrors the endpoint's own gate (design §6): "we don't use payroll" is a
  // standing commitment about the tenant, not a per-user preference. Offering
  // the control to anyone else would only produce a 403 on click.
  const canDismiss = hasRole(me.data, 'org_admin')
  const groups = checklist.data !== undefined ? groupItems(checklist.data.items) : null
  // Reuses the sidebar badge's own sentence rather than composing a fourth
  // count string: /setup is the page that badge sends you to, and §6 puts
  // `error_count` on the wire so a client can tell "4 things to do" from
  // "4 things we could not check" and say so. This is where it says so.
  const summary = checklist.data !== undefined ? badgeLabel(checklist.data) : null

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Setup"
        subtitle="What is left to configure. The checklist never blocks you — the required items are what reporting needs."
      />

      {summary !== null && <p className="text-sm text-ink-muted">{summary.title}</p>}

      {checklist.isError && (
        <Card>
          <p className="text-sm text-danger-red">Failed to load: {errorMessage(checklist.error)}</p>
        </Card>
      )}

      {checklist.isPending && (
        <Card>
          <p className="text-sm text-ink-muted">Loading …</p>
        </Card>
      )}

      {/* `all_clear`, never `open_count === 0` — see the invariant on
          `Checklist.all_clear` for why the two differ. */}
      {checklist.data?.all_clear === true && (
        <Card>
          <p className="text-sm text-ink">Nothing left to set up.</p>
        </Card>
      )}

      {groups !== null && groups.required.length > 0 && (
        <ItemGroup label="Required" items={groups.required} canDismiss={canDismiss} />
      )}
      {groups !== null && groups.optional.length > 0 && (
        <ItemGroup label="Optional" items={groups.optional} canDismiss={canDismiss} />
      )}
    </div>
  )
}

function ItemGroup({
  label,
  items,
  canDismiss,
}: {
  label: string
  items: ChecklistItem[]
  canDismiss: boolean
}) {
  return (
    <Card role="region" aria-label={`${label} setup`}>
      <h2 className={sectionHeadClass}>{label}</h2>
      {/* No `display:flex` on the <ul>: it drops the list role in WebKit/
          VoiceOver, and "how many, and which" is this page's whole job. */}
      <ul className="mt-1">
        {items.map((item) => (
          <ItemRow key={item.key} item={item} canDismiss={canDismiss} />
        ))}
      </ul>
    </Card>
  )
}

function ItemRow({ item, canDismiss }: { item: ChecklistItem; canDismiss: boolean }) {
  const status = STATUS[item.status]
  const invalidate = useInvalidateChecklist()
  // Invalidating the one shared key is what moves this row, the sidebar badge
  // and the dashboard card together — they read the same query, so they cannot
  // end up disagreeing about what is still open.
  const dismiss = useMutation({ mutationFn: () => dismissItem(item.key), onSuccess: invalidate })
  const restore = useMutation({ mutationFn: () => restoreItem(item.key), onSuccess: invalidate })
  // Required items never get the control: the server answers 422 (design §6),
  // and a button whose only outcome is a refusal is the dishonesty this
  // feature exists to avoid. The 422 branch below stays handled anyway — the
  // endpoint is reachable, and a registry edit could move a key across the
  // required line without this file changing.
  const action =
    canDismiss && !item.required ? (item.status === 'dismissed' ? restore : dismiss) : null
  const verb = item.status === 'dismissed' ? 'Restore' : 'Dismiss'
  return (
    <li className="flex flex-col gap-0.5 border-b border-line py-3 last:border-0">
      {/* justify-between reserves the right edge as a stable action slot: an
          inline control after `detail` would be moved around by flex-wrap. */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span
            aria-hidden="true"
            className={`w-3 shrink-0 text-center text-sm font-semibold ${glyphTone[item.status]}`}
          >
            {status.glyph}
          </span>
          {item.where !== null ? (
            <Link to={item.where} className="text-sm font-medium text-accent hover:underline">
              {item.title}
            </Link>
          ) : (
            <span className="text-sm font-medium text-ink">{item.title}</span>
          )}
          <Badge tone={status.tone}>{status.word}</Badge>
          {item.detail !== null && <span className="text-xs text-ink-muted">{item.detail}</span>}
        </div>
        {action !== null && (
          // The title is in the accessible name because a full checklist is
          // seven rows of the same word, and "Dismiss" alone would name them
          // all identically to a screen reader and to a test.
          <button
            type="button"
            aria-label={`${verb} ${item.title}`}
            disabled={action.isPending}
            onClick={() => action.mutate()}
            className="shrink-0 rounded-control border border-line px-2 py-1 text-xs text-ink-muted hover:bg-surface-sunken disabled:opacity-50"
          >
            {verb}
          </button>
        )}
      </div>
      <p className="pl-5 text-sm text-ink-muted">{item.description}</p>
      {/* Deliberately not conditioned on status: a demand feed the tenant was
          provisioned with probes `done` and still had no surface to connect it
          on, so Done and the reason legitimately appear together (D-B4.8).
          The `unavailable_reason` half of the test is a guard against a backend
          that broke D-B4.8's paired invariant — an un-closeable item with no
          reason renders as plain text with no explanation, which is wrong but
          less wrong than crashing the page. */}
      {item.where === null && item.unavailable_reason !== null && (
        <p className="pl-5 text-sm text-ink-muted">{item.unavailable_reason}</p>
      )}
      {/* Beside the row it belongs to, not in a page-level banner: on a
          seven-row page the operator has to know WHICH item refused. */}
      {action?.isError === true && (
        <p className="pl-5 text-sm text-danger-red">{errorMessage(action.error)}</p>
      )}
    </li>
  )
}
