// The onboarding checklist's permanent home (Track B/B4, design §7): what is
// still unconfigured, probed on read by the backend rather than read from a
// stored status that can outlive the thing it describes (D-B4.1).
//
// Unlike every other page this one takes NO property — the checklist is
// org-scoped, so there is no `useGlobalProperty()` and no "no property
// selected yet" state to render.

import { Link } from '@tanstack/react-router'

import { Badge, Card, PageHeader, sectionHeadClass, type BadgeTone } from '../components/ui'
import type { ChecklistItem, ChecklistStatus } from '../api/types'
import { badgeLabel, groupItems, useChecklist } from '../lib/useChecklist'
import { errorMessage } from '../lib/errors'

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
        <ItemGroup label="Required" items={groups.required} />
      )}
      {groups !== null && groups.optional.length > 0 && (
        <ItemGroup label="Optional" items={groups.optional} />
      )}
    </div>
  )
}

function ItemGroup({ label, items }: { label: string; items: ChecklistItem[] }) {
  return (
    <Card role="region" aria-label={`${label} setup`}>
      <h2 className={sectionHeadClass}>{label}</h2>
      {/* No `display:flex` on the <ul>: it drops the list role in WebKit/
          VoiceOver, and "how many, and which" is this page's whole job. */}
      <ul className="mt-1">
        {items.map((item) => (
          <ItemRow key={item.key} item={item} />
        ))}
      </ul>
    </Card>
  )
}

function ItemRow({ item }: { item: ChecklistItem }) {
  const status = STATUS[item.status]
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
    </li>
  )
}
