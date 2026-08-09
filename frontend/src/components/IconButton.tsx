// An icon-only action with a hover/focus tooltip.
//
// The label is the button's accessible name AND the tooltip text, from one
// prop — so the two can never drift, and the control is never a bare glyph to a
// screen reader. The tooltip is CSS-only (group-hover / group-focus-within), so
// there is no timer, no portal, and no state to get stuck open.
//
// `title` is deliberately NOT used: the native tooltip is slow, unstyleable,
// and would double up with this one.

import type { ComponentPropsWithoutRef, ReactNode } from 'react'

const TONES = {
  neutral: 'text-ink-muted hover:bg-surface-sunken hover:text-ink',
  accent: 'text-accent hover:bg-accent-soft',
  warn: 'text-warn-amber hover:bg-warn-amber-soft',
  danger: 'text-danger-red hover:bg-danger-red-soft',
  ok: 'text-ok-green hover:bg-ok-green-soft',
} as const

export type IconButtonTone = keyof typeof TONES

export default function IconButton({
  label,
  icon,
  tone = 'neutral',
  className,
  ...rest
}: Omit<ComponentPropsWithoutRef<'button'>, 'aria-label' | 'title'> & {
  label: string
  icon: ReactNode
  tone?: IconButtonTone
}) {
  return (
    <span className="group relative inline-flex">
      <button
        type="button"
        aria-label={label}
        className={[
          'grid size-8 place-items-center rounded-lg border border-transparent',
          'transition-colors disabled:cursor-not-allowed disabled:opacity-40',
          TONES[tone],
          className ?? '',
        ].join(' ')}
        {...rest}
      >
        {icon}
      </button>
      {/* aria-hidden: the same string is already the button's accessible name,
          so exposing it twice would just make the control read itself back. */}
      <span
        aria-hidden="true"
        className={[
          'pointer-events-none absolute -top-1 left-1/2 z-20 -translate-x-1/2 -translate-y-full',
          'whitespace-nowrap rounded-md bg-ink px-2 py-1 text-xs font-medium text-surface',
          'opacity-0 shadow-lg transition-opacity',
          'group-hover:opacity-100 group-focus-within:opacity-100',
        ].join(' ')}
      >
        {label}
      </span>
    </span>
  )
}
