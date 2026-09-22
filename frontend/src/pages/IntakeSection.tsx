import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Card, cellClass, controlClass, headCellClass, tableClass } from '../components/ui'
import {
  ApiError, createIntakeAddress, getIntakeAddress, getIntakeEvents, rotateIntakeAddress,
  setIntakeSenderDomains,
} from '../api/client'
import type { IntakeAddress, IntakeEvent, IntakeOutcome } from '../api/types'
import { errorMessage } from '../lib/errors'

/**
 * The property's night-audit email address, and the log of what arrived at it
 * (OH-23) — a section of the property configuration page, in its own file
 * because it owns four writes and two queries of its own.
 */

const INTAKE_EVENT_LIMIT = 20
const primaryButtonClass =
  'rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50'
const quietButtonClass =
  'rounded-control border border-line px-2 py-1 text-xs text-ink hover:bg-surface-sunken disabled:opacity-50'
const dangerButtonClass =
  'rounded-control border border-line px-2 py-1 text-xs text-danger-red hover:bg-surface-sunken disabled:opacity-50'

const SENDER_DOMAINS_HELP_ID = 'intake-sender-domains-help'
const SENDER_DOMAINS_ERROR_ID = 'intake-sender-domains-error'
const ROTATE_ERROR_ID = 'intake-rotate-error'

/**
 * How each outcome is worded on screen.
 *
 * Typing this `Record<IntakeOutcome, string>` is what closes the set AT COMPILE
 * TIME: adding a member to `IntakeOutcome` without a label here fails
 * `npx tsc -b`. It says nothing about the wire — no runtime validation stands
 * between the API and this map — so `outcomeLabel` still has to answer for a
 * value it has never seen.
 */
const OUTCOME_LABELS: Record<IntakeOutcome, string> = {
  ingested: 'ingested',
  partial: 'partially ingested',
  duplicate: 'duplicate',
  no_attachment: 'no attachment',
  sender_rejected: 'sender rejected',
  wrong_property: 'wrong property',
  not_a_night_audit_report: 'not a night-audit report',
  unreadable: 'unreadable',
  failed: 'failed',
  revoked_address: 'revoked address',
}

/**
 * The wording for an outcome, or the raw value with its underscores opened out
 * when the map has none.
 *
 * The fallback is the load-bearing half: `IntakeOutcome` is a compile-time
 * claim about a JSON string, and nothing checks the response against it, so an
 * outcome a later release adds arrives here as a value this build has never
 * heard of. It renders as itself rather than as "undefined" — pinned by
 * IntakeSection.test.tsx::"renders an outcome this build has never heard of".
 */
function outcomeLabel(outcome: IntakeOutcome): string {
  const known: string | undefined = OUTCOME_LABELS[outcome]
  return known ?? String(outcome).replace(/_/g, ' ')
}

function parseSenderDomains(text: string): string[] {
  return text.split(',').map((entry) => entry.trim()).filter((entry) => entry !== '')
}

/**
 * What to show beside the sender-domain field when a save is refused. A 422
 * from the request BODY carries `detail` as a string naming the offending
 * entry, and that string is the whole message; a `detail` of any other shape
 * (FastAPI spells a bad query parameter as a list) is reported by status
 * instead of stringified at the operator.
 */
function senderDomainRefusal(error: unknown): string {
  if (error instanceof ApiError) {
    return typeof error.detailBody === 'string'
      ? error.detailBody
      : `Save failed (HTTP ${error.status}).`
  }
  return errorMessage(error)
}

/**
 * The address is minted ON DEMAND rather than at property creation, so a
 * property whose night audit is uploaded by hand carries no unused intake
 * capability. The address itself is served fully formed — the intake domain is
 * a server setting and is never assembled here.
 */
export function IntakeSection({ propertyId }: { propertyId: string }) {
  const qc = useQueryClient()

  const address = useQuery({
    queryKey: ['intake-address', propertyId],
    queryFn: () => getIntakeAddress(propertyId),
  })
  const events = useQuery({
    queryKey: ['intake-events', propertyId],
    queryFn: () => getIntakeEvents(propertyId, INTAKE_EVENT_LIMIT),
  })

  const refetchAddress = () =>
    void qc.invalidateQueries({ queryKey: ['intake-address', propertyId] })

  /**
   * Seat the address a write just returned, then re-read.
   *
   * Every one of the four address routes answers with the full new address, so
   * seating it is what puts a rotated address on screen in the same tick the
   * rotation resolves; the invalidate that follows only confirms it against
   * the server. Without the seat the field shows the OLD address until the
   * refetch lands — pinned by IntakeSection.test.tsx::"shows the rotated
   * address before the refetch resolves".
   */
  const publishAddress = (fresh: IntakeAddress) => {
    qc.setQueryData(['intake-address', propertyId], fresh)
    refetchAddress()
  }

  const create = useMutation({
    mutationFn: () => createIntakeAddress(propertyId),
    onSuccess: publishAddress,
    // Re-read on failure too: a 409 means this property already has an address
    // (another session minted it), and that address is the answer to show.
    onSettled: refetchAddress,
  })

  const live = address.data

  return (
    <Card role="region" aria-label="night-audit email">
      <h2 className="mb-3 text-sm font-semibold text-ink">Night-audit email</h2>

      {address.isPending && <p className="text-sm text-ink-muted">Loading …</p>}
      {address.isError && (
        <p className="text-sm text-danger-red">Failed to load: {errorMessage(address.error)}</p>
      )}

      {live !== undefined && live.address === null && (
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm text-ink-muted">
            This property has no night-audit email address yet. Create one to have the PMS
            mail its night audit straight in.
          </p>
          <button
            type="button"
            disabled={create.isPending}
            onClick={() => create.mutate()}
            className={primaryButtonClass}
          >
            Create address
          </button>
          {/* Inside the no-address branch on purpose: a 409 means the address
              now exists, and the refetch that follows replaces this whole
              branch — a banner outside it would outlive the failure it
              describes and sit beside the address that answered it. */}
          {create.isError && (
            <p className="text-sm text-danger-red">Create failed: {errorMessage(create.error)}</p>
          )}
        </div>
      )}

      {live !== undefined && live.address !== null && (
        // Keyed by the address so a write that changes it gives the panel a
        // fresh mount: its draft sender-domain text belongs to the address it
        // was typed against.
        <IntakeAddressPanel
          key={live.address}
          propertyId={propertyId}
          address={live.address}
          createdAt={live.created_at}
          senderDomains={live.sender_domains}
          onAddressWritten={publishAddress}
          onAddressChanged={refetchAddress}
        />
      )}

      <div className="mt-4 border-t border-line pt-4">
        {events.isError && (
          <p className="text-sm text-danger-red">
            Failed to load the message log: {errorMessage(events.error)}
          </p>
        )}
        {events.data && <IntakeEventTable events={events.data.events} />}
      </div>
    </Card>
  )
}

function IntakeAddressPanel({
  propertyId,
  address,
  createdAt,
  senderDomains,
  onAddressWritten,
  onAddressChanged,
}: {
  propertyId: string
  address: string
  createdAt: string | null
  senderDomains: string[] | null
  onAddressWritten: (fresh: IntakeAddress) => void
  onAddressChanged: () => void
}) {
  const [confirmingRotate, setConfirmingRotate] = useState(false)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  // Both initialize from the props once: IntakeSection keys this panel by the
  // address, so a rotation mounts a new one rather than leaving a stale draft.
  // `saved` then tracks what the server last confirmed, which is what a blur
  // compares against — the `senderDomains` prop lags a save by one refetch.
  const [domainsDraft, setDomainsDraft] = useState(senderDomains?.join(', ') ?? '')
  const [saved, setSaved] = useState<string[]>(senderDomains ?? [])

  // Clear the "Copied" flash on a timer, and cancel it on unmount so the timer
  // does not outlive the panel.
  useEffect(() => {
    if (copyState !== 'copied') return
    const timer = setTimeout(() => setCopyState('idle'), 2000)
    return () => clearTimeout(timer)
  }, [copyState])

  // A 404 from either write means only "no active address" — it was revoked
  // elsewhere — so re-read rather than leave a form up that writes nowhere.
  // A property the caller may not touch answers 403, not 404; the client's
  // night-audit-intake block in api/client.ts names the tests for both.
  const resyncIfRevoked = (error: unknown) => {
    if (error instanceof ApiError && error.status === 404) onAddressChanged()
  }

  const rotate = useMutation({
    mutationFn: () => rotateIntakeAddress(propertyId),
    onSuccess: (fresh) => {
      setConfirmingRotate(false)
      onAddressWritten(fresh)
    },
    onError: resyncIfRevoked,
  })

  const saveDomains = useMutation({
    mutationFn: (entries: string[]) => setIntakeSenderDomains(propertyId, entries),
    onSuccess: (fresh) => {
      const stored = fresh.sender_domains ?? []
      setSaved(stored)
      setDomainsDraft(stored.join(', '))
      onAddressWritten(fresh)
    },
    onError: resyncIfRevoked,
  })

  const copyAddress = () => {
    setConfirmingRotate(false)
    const clipboard: Clipboard | undefined = navigator.clipboard
    if (clipboard === undefined) {
      setCopyState('failed')
      return
    }
    void Promise.resolve(clipboard.writeText(address)).then(
      () => setCopyState('copied'),
      () => setCopyState('failed'),
    )
  }

  const submitDomains = () => {
    const entries = parseSenderDomains(domainsDraft)
    // Entries are stored lowercased (see setIntakeSenderDomains in api/client.ts
    // for the test that holds it), so the comparison that decides "nothing
    // changed" is case-insensitive. Skipping an unchanged save matters: each
    // one writes an audit row, and a blur fires whether or not anything moved.
    const unchanged =
      entries.length === saved.length &&
      entries.every((entry, i) => entry.toLowerCase() === saved[i])
    if (unchanged || saveDomains.isPending) return
    saveDomains.mutate(entries)
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <label htmlFor="intake-address" className="text-xs font-medium text-ink-muted">
          Night-audit email address
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <input
            id="intake-address"
            readOnly
            value={address}
            className={`${controlClass} w-80 max-w-full`}
          />
          <button type="button" onClick={copyAddress} className={quietButtonClass}>
            Copy
          </button>
          {/* Always rendered, so a screen reader is watching this node before
              the copy happens — a region that appears only once there is
              something to say is announced unreliably. */}
          <span role="status" className="text-xs">
            {copyState === 'copied' && <span className="text-ink-muted">Copied</span>}
            {copyState === 'failed' && (
              <span className="text-danger-red">Could not copy — select the address instead.</span>
            )}
          </span>
        </div>
        {createdAt !== null && (
          <p className="text-xs text-ink-muted">
            In use since {new Date(createdAt).toLocaleDateString()}.
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {confirmingRotate ? (
          <>
            <button
              type="button"
              disabled={rotate.isPending}
              onClick={() => rotate.mutate()}
              className={dangerButtonClass}
              aria-invalid={rotate.isError || undefined}
              aria-describedby={rotate.isError ? ROTATE_ERROR_ID : undefined}
            >
              Confirm rotation
            </button>
            <button
              type="button"
              onClick={() => setConfirmingRotate(false)}
              className={quietButtonClass}
            >
              Cancel
            </button>
            <span className="text-xs text-ink-muted">
              The current address stops accepting mail, and the new one starts with no
              sender allowlist.
            </span>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingRotate(true)}
            className={quietButtonClass}
            aria-invalid={rotate.isError || undefined}
            aria-describedby={rotate.isError ? ROTATE_ERROR_ID : undefined}
          >
            Rotate address
          </button>
        )}
      </div>
      {rotate.isError && (
        <p id={ROTATE_ERROR_ID} className="text-sm text-danger-red">
          Rotate failed: {errorMessage(rotate.error)}
        </p>
      )}

      <div className="flex flex-col gap-1">
        <label htmlFor="intake-sender-domains" className="text-xs font-medium text-ink-muted">
          Allowed sender domains
        </label>
        <input
          id="intake-sender-domains"
          value={domainsDraft}
          className={`${controlClass} w-80 max-w-full`}
          placeholder="pms.example.com, reports.example.com"
          aria-invalid={saveDomains.isError || undefined}
          aria-describedby={
            saveDomains.isError
              ? `${SENDER_DOMAINS_ERROR_ID} ${SENDER_DOMAINS_HELP_ID}`
              : SENDER_DOMAINS_HELP_ID
          }
          onFocus={() => setConfirmingRotate(false)}
          onChange={(e) => setDomainsDraft(e.target.value)}
          onBlur={submitDomains}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submitDomains()
            }
          }}
        />
        <p id={SENDER_DOMAINS_HELP_ID} className="text-xs text-ink-muted">
          Comma-separated. Leave it empty to accept any authenticated sender. Saved when you
          leave the field or press Enter.
        </p>
        {saveDomains.isError && (
          <p id={SENDER_DOMAINS_ERROR_ID} className="text-sm text-danger-red">
            {senderDomainRefusal(saveDomains.error)}
          </p>
        )}
      </div>
    </div>
  )
}

function IntakeEventTable({ events }: { events: IntakeEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-ink-muted">No messages yet.</p>
  }
  return (
    // The caption both heads the log on screen and names the table for a
    // screen reader — one visible string doing both jobs, so there is no
    // aria-label here to drift away from what is drawn.
    <table className={tableClass}>
      <caption className="mb-2 text-left text-xs font-semibold text-ink-muted">
        Last {INTAKE_EVENT_LIMIT} messages
      </caption>
      <thead>
        <tr className="border-b border-line">
          <th className={headCellClass}>Received</th>
          <th className={headCellClass}>From</th>
          <th className={headCellClass}>Subject</th>
          <th className={headCellClass}>Outcome</th>
          <th className={headCellClass}>Attachments</th>
        </tr>
      </thead>
      <tbody>
        {events.map((event) => (
          <tr key={event.event_id} className="border-b border-line last:border-0">
            <td className={cellClass}>{new Date(event.received_at).toLocaleString()}</td>
            <td className={cellClass}>{event.envelope_from}</td>
            {/* The subject is how an operator recognizes a message; a PMS that
                sends none still has to be tellable from one that did. */}
            <td className={cellClass}>
              {event.subject ?? <span className="text-ink-muted">(no subject)</span>}
            </td>
            <td className={cellClass}>{outcomeLabel(event.outcome)}</td>
            <td className={cellClass}>
              {event.attachments.length === 0 ? (
                '—'
              ) : (
                <ul className="flex flex-col gap-0.5">
                  {event.attachments.map((attachment, i) => (
                    <li key={`${attachment.sha256}-${i}`}>
                      {attachment.name} — {outcomeLabel(attachment.outcome)}
                    </li>
                  ))}
                </ul>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
