// Shared modal: scrim + centered panel, closes on scrim click and Escape.
// Token-based like every primitive; the panel is a focus boundary in spirit —
// simple enough for the app's forms without pulling in a dialog library.

import { useEffect, type ReactNode } from 'react'

import { CloseIcon } from './icons'

// Named sizes rather than a `wide` boolean: a form with paired fields needs a
// middle width, and two booleans would be the next thing asked for.
const widthClass = {
  sm: 'max-w-md',
  md: 'max-w-xl',
  lg: 'max-w-3xl',
  // For content that is WIDE rather than long — a day timeline needs the
  // horizontal room or every punch piles into the same few pixels.
  xl: 'max-w-5xl',
} as const

export default function Modal({
  title,
  subtitle,
  onClose,
  children,
  size = 'sm',
}: {
  title: string
  subtitle?: string
  onClose: () => void
  children: ReactNode
  size?: keyof typeof widthClass
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 grid place-items-center p-4">
      <button
        type="button"
        aria-label="Close dialog"
        className="absolute inset-0 bg-scrim"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`relative flex max-h-[88vh] w-full flex-col overflow-hidden rounded-2xl border border-line bg-surface-raised shadow-overlay ${widthClass[size]}`}
      >
        {/* The header is a fixed frame: a long deposit chain scrolls under it
            rather than carrying the title off the top of the panel. */}
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-line px-7 py-5">
          <div className="min-w-0">
            <h2 className="text-xl font-bold tracking-tight text-ink">{title}</h2>
            {subtitle !== undefined && (
              <p className="mt-1 text-sm leading-relaxed text-ink-muted">{subtitle}</p>
            )}
          </div>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="-mr-1.5 shrink-0 rounded-lg p-1.5 text-ink-muted hover:bg-surface-sunken hover:text-ink"
          >
            <CloseIcon />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-7 py-6">{children}</div>
      </div>
    </div>
  )
}
