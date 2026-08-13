// Release notes drawer: the build's changes and recent history, opened from
// the footer build stamp. Content is baked in at build time
// (src/generated/releaseNotes.ts, generated from git) — no network call, so it
// works behind the app's strict same-origin posture and always matches the
// running bundle. The release whose SHA equals the running build is marked and
// listed first.

import Modal from './Modal'
import { Badge } from './ui'
import { releases as bakedReleases, type Release } from '../generated/releaseNotes'

const REPO_URL = 'https://github.com/csharp36/open-hospitality'

// Stable heading order; only groups with entries render.
const LABEL_ORDER = ['Features', 'Fixes', 'Performance', 'Refactors', 'Docs', 'Reverts']

function ChangeGroups({ changes }: { changes: Release['changes'] }) {
  const groups = LABEL_ORDER.map((label) => ({
    label,
    items: changes.filter((c) => c.label === label),
  })).filter((g) => g.items.length > 0)

  return (
    <div className="mt-2 flex flex-col gap-2">
      {groups.map((group) => (
        <div key={group.label}>
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
            {group.label}
          </p>
          <ul className="mt-1 flex list-disc flex-col gap-1 pl-5">
            {group.items.map((c, i) => (
              <li key={`${group.label}-${i}`} className="text-sm text-ink">
                {c.breaking && (
                  <>
                    <Badge tone="danger">Breaking</Badge>{' '}
                  </>
                )}
                {c.scope !== null && <span className="text-ink-muted">{c.scope}: </span>}
                {c.subject}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

export default function ReleaseNotes({
  onClose,
  buildSha,
  releases = bakedReleases,
}: {
  onClose: () => void
  buildSha: string
  releases?: Release[]
}) {
  return (
    <Modal title="Release notes" subtitle={`This build: ${buildSha}`} onClose={onClose} size="md">
      {releases.length === 0 ? (
        <p className="text-sm text-ink-muted">No release notes available for this build.</p>
      ) : (
        <ol className="flex flex-col gap-4">
          {releases.map((r) => {
            const current = r.sha === buildSha
            return (
              <li
                key={r.sha}
                className={`rounded-xl p-4 ${
                  current ? 'border-2 border-accent bg-surface' : 'border border-line bg-surface'
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <code className="rounded bg-surface-sunken px-1.5 py-0.5 text-xs text-ink">
                    {r.sha}
                  </code>
                  <span className="text-xs text-ink-muted">{r.date}</span>
                  {r.pr !== null && (
                    <a
                      href={`${REPO_URL}/pull/${r.pr}`}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs font-medium text-accent hover:underline"
                    >
                      #{r.pr}
                    </a>
                  )}
                  {current && <Badge tone="info">This build</Badge>}
                </div>
                <ChangeGroups changes={r.changes} />
              </li>
            )
          })}
        </ol>
      )}
    </Modal>
  )
}
