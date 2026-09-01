// Presentational drag/drop + file-pick control for the public preview front
// door. Validates type/size client-side and calls onFile(file); the page
// that renders this owns the async upload/analysis call.

import { useRef, useState } from 'react'

const MAX_BYTES = 10 * 1024 * 1024

export default function DropZone({ onFile }: { onFile: (file: File) => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [over, setOver] = useState(false)

  function accept(file: File | undefined) {
    if (!file) return
    if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
      setError('Please choose a PDF — the nightly report your PMS produces.')
      return
    }
    if (file.size > MAX_BYTES) {
      setError('That file is over 10 MB. A night-audit PDF is usually much smaller.')
      return
    }
    setError(null)
    onFile(file)
  }

  return (
    <div>
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setOver(false)
          accept(e.dataTransfer.files[0])
        }}
        className={`rounded-card border p-8 text-center ${over ? 'border-brand-accent bg-brand-accent-soft' : 'border-brand-line bg-brand-surface'}`}
      >
        <p className="text-brand-ink">Drop your PMS report here</p>
        <p className="mt-1 text-sm text-brand-ink-muted">Opera · AutoClerk — PDF, nothing saved</p>
        <button
          type="button"
          className="mt-3 rounded-control border border-brand-accent px-4 py-1.5 text-brand-accent"
          onClick={() => inputRef.current?.click()}
        >
          Choose a file
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".pdf"
          aria-label="PDF file"
          className="hidden"
          onChange={(e) => accept(e.target.files?.[0])}
        />
      </div>
      {error && (
        <p role="alert" className="mt-2 text-sm text-danger-red">
          {error}
        </p>
      )}
    </div>
  )
}
