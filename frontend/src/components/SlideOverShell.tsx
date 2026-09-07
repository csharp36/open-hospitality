// Shared slide-over dialog shell: scrim, right-edge panel, header row with
// the Close button, and the whole modal keyboard contract — focus lands on
// Close at open, Escape (unless already consumed) and the scrim close, Tab
// wraps inside the panel, and focus goes back to whatever held it at open.
// DrillPanel and JournalDrillPanel supply only their content as children.

import { useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'

// The trap's notion of tabbable: a query over the panel, recomputed on each
// Tab press so content that arrives after open (a loaded table's links, say)
// joins the cycle without bookkeeping.
const TABBABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

type SlideOverShellProps = {
  /** Accessible name for the dialog. */
  label: string
  /** Header content, rendered as the panel's h2 beside the Close button. */
  title: ReactNode
  onClose: () => void
  children: ReactNode
}

export default function SlideOverShell({ label, title, onClose, children }: SlideOverShellProps) {
  const asideRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  // Both ends of the focus contract in one effect: Close takes focus when the
  // panel opens, and the element that held focus at open gets it back when
  // the panel unmounts — one path for Escape, scrim click, the Close button,
  // and a page-driven close alike. The isConnected guard covers openers that
  // unmounted while the panel was up (a refetched table rebuilt its rows).
  useEffect(() => {
    const opener = document.activeElement
    closeRef.current?.focus()
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      // defaultPrevented: something closer to the key already consumed this
      // Escape; closing on top of that would double-act one keypress. And
      // claim the keypress before closing — the check on this same line is
      // what any other handler in the protocol consults.
      if (e.key === 'Escape' && !e.defaultPrevented) {
        e.preventDefault()
        onClose()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  // The trap wraps only at the edges and moves focus itself; interior Tabs
  // fall through untouched.
  function trapTab(e: ReactKeyboardEvent) {
    if (e.key !== 'Tab' || asideRef.current === null) return
    const tabbables = Array.from(asideRef.current.querySelectorAll<HTMLElement>(TABBABLE))
    if (tabbables.length === 0) return
    const first = tabbables[0]!
    const last = tabbables[tabbables.length - 1]!
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  return (
    <div className="fixed inset-0 z-10">
      {/* bg-scrim stays dark in BOTH modes (token has no .dark remap) — a
          scrim dims the page behind the overlay; bg-ink/40 would invert to a
          light veil in dark mode. */}
      <div className="absolute inset-0 bg-scrim" onClick={onClose} aria-hidden="true" />
      <aside
        ref={asideRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        onKeyDown={trapTab}
        className="absolute inset-y-0 right-0 w-full max-w-2xl overflow-y-auto rounded-l-card border-l border-line bg-surface-raised p-6 shadow-overlay"
      >
        <div className="flex items-start justify-between">
          <h2 className="text-lg font-semibold text-ink">{title}</h2>
          <button
            ref={closeRef}
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="rounded-control px-2 text-xl leading-none text-ink-muted hover:bg-surface-sunken hover:text-ink"
          >
            ×
          </button>
        </div>
        {children}
      </aside>
    </div>
  )
}
