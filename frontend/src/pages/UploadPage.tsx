// Drag-and-drop PDF upload page. Each dropped/picked file POSTs to /ingest
// sequentially (one at a time — the backend stages synchronously) and gets a
// result card: the parse summary on success, or the API `detail` message on a
// danger-bordered card on failure. A failed file never blocks the rest of the
// batch.

import { Fragment, useRef, useState } from 'react'

import { postIngest } from '../api/client'
import type { IngestResult } from '../api/types'
import { Card, PageHeader } from '../components/ui'
import { errorMessage } from '../lib/errors'

type UploadStatus =
  | { kind: 'pending' }
  | { kind: 'uploading' }
  | { kind: 'done'; result: IngestResult }
  | { kind: 'failed'; detail: string }

type UploadItem = { id: number; name: string; status: UploadStatus }

export default function UploadPage() {
  const [items, setItems] = useState<UploadItem[]>([])
  const [busy, setBusy] = useState(false)
  const [busyNotice, setBusyNotice] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const nextIdRef = useRef(0)

  function patchItem(id: number, status: UploadStatus) {
    setItems((prev) => prev.map((item) => (item.id === id ? { ...item, status } : item)))
  }

  async function uploadFiles(files: File[]) {
    if (files.length === 0) return
    if (busy) {
      // Late arrivals aren't queued — say so instead of silently dropping them.
      setBusyNotice(true)
      return
    }
    setBusy(true)
    setBusyNotice(false)
    const batch = files.map((file) => ({ file, id: nextIdRef.current++ }))
    setItems((prev) => [
      ...prev,
      ...batch.map(({ file, id }) => ({ id, name: file.name, status: { kind: 'pending' } as const })),
    ])
    for (const { file, id } of batch) {
      patchItem(id, { kind: 'uploading' })
      try {
        const result = await postIngest(file)
        patchItem(id, { kind: 'done', result })
      } catch (error) {
        // errorMessage renders a 422 ApiError's bare FastAPI detail
        // (unrecognized report, bad parse); anything else falls back to the
        // Error message. Either way the loop continues with the next file.
        patchItem(id, { kind: 'failed', detail: errorMessage(error) })
      }
    }
    setBusy(false)
    setBusyNotice(false)
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Upload Reports" />

      <div
        onDragOver={(e) => {
          e.preventDefault()
          if (!busy) setDragOver(true) // no drop-here affordance mid-batch
        }}
        onDragLeave={(e) => {
          // Only a real exit clears the highlight — dragging over child
          // elements fires dragleave too and would make the border flicker.
          if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false)
        }}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          void uploadFiles(Array.from(e.dataTransfer.files))
        }}
        className={`flex flex-col items-center gap-2 rounded-card border-2 border-dashed p-10 ${
          dragOver ? 'border-accent bg-accent-soft' : 'border-line bg-surface-raised'
        }`}
      >
        {/* ink-muted fails AA on the drag-over accent-soft tint — swap to
            accent-ink while the highlight is active. */}
        <p className={`text-sm ${dragOver ? 'text-accent-ink' : 'text-ink-muted'}`}>
          Drag and drop PDF reports here
        </p>
        <p className="text-xs text-ink-faint">or</p>
        <button
          type="button"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
          className="rounded-control border border-line bg-surface-raised px-3 py-1.5 text-sm font-medium text-ink hover:bg-surface-sunken disabled:bg-surface-sunken disabled:text-ink-faint"
        >
          Choose files
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".pdf"
          multiple
          aria-label="PDF files"
          className="hidden"
          onChange={(e) => {
            void uploadFiles(Array.from(e.target.files ?? []))
            e.target.value = '' // allow re-picking the same file
          }}
        />
        {busy && <p className="text-sm text-ink-muted">Uploading…</p>}
        {busyNotice && (
          <p className="rounded-control bg-warn-amber-soft px-3 py-1.5 text-sm font-medium text-warn-amber">
            Please wait for the current batch to finish before adding more files.
          </p>
        )}
      </div>

      {items.map((item) => (
        <ResultCard key={item.id} item={item} />
      ))}
    </div>
  )
}

// --- Presentational cards ----------------------------------------------------

function ResultCard({ item }: { item: UploadItem }) {
  // Card bakes in `border-line`; `border-danger-red` alone would stack a
  // conflicting border-color utility whose winner depends on stylesheet
  // emission order. The trailing `!` (Tailwind v4 important modifier) makes
  // the danger border win deterministically. role="region" keeps the landmark
  // the tests locate via getByRole('region', { name: 'Upload result: …' }).
  return (
    <Card
      role="region"
      aria-label={`Upload result: ${item.name}`}
      className={item.status.kind === 'failed' ? 'border-danger-red!' : undefined}
    >
      <h2 className="text-sm font-semibold break-all text-ink">{item.name}</h2>
      {item.status.kind === 'pending' && <p className="mt-1 text-sm text-ink-muted">Waiting…</p>}
      {item.status.kind === 'uploading' && (
        <p className="mt-1 text-sm text-ink-muted">Uploading…</p>
      )}
      {item.status.kind === 'failed' && (
        <p className="mt-1 text-sm text-danger-red">{item.status.detail}</p>
      )}
      {item.status.kind === 'done' && <IngestSummary result={item.status.result} />}
    </Card>
  )
}

function IngestSummary({ result }: { result: IngestResult }) {
  const fields: [string, string | number][] = [
    ['PMS source', result.pms_source],
    ['Report type', result.report_type],
    ['Property', result.property_id],
    ['Business date', result.business_date],
    ['Staged', result.staged],
    ['Mapped', result.mapped],
    ['Unmapped', result.unmapped],
    ['Skipped', result.skipped],
  ]
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-sm sm:grid-cols-[auto_1fr_auto_1fr]">
      {fields.map(([label, value]) => (
        <Fragment key={label}>
          <dt className="self-center text-xs font-medium text-ink-muted">{label}</dt>
          <dd className="tabular-nums">{value}</dd>
        </Fragment>
      ))}
    </dl>
  )
}
