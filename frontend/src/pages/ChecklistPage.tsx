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
import { groupItems, useChecklist } from '../lib/useChecklist'
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

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Setup"
        subtitle="What is left to configure. Nothing here blocks reporting."
      />

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

      {/* `all_clear`, never `open_count === 0`: an item whose probe raised is
          not counted as open, so a tenant whose probes all failed has zero
          open items while nothing at all is known. */}
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
      <ul className="mt-1 flex flex-col">
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
      <div className="flex flex-wrap items-center gap-2">
        <span aria-hidden="true" className={`w-3 text-sm font-semibold ${glyphTone[item.status]}`}>
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
      <p className="pl-5 text-sm text-ink-muted">{item.description}</p>
      {/* Deliberately not conditioned on status: a demand feed the tenant was
          provisioned with probes `done` and still had no surface to connect it
          on, so Done and the reason legitimately appear together (D-B4.8). */}
      {item.where === null && item.unavailable_reason !== null && (
        <p className="pl-5 text-sm text-ink-muted">{item.unavailable_reason}</p>
      )}
    </li>
  )
}
