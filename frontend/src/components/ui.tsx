// Shared presentational primitives for the portal (P9 UI polish).
// Everything here is stateless and token-based: components reference the
// semantic colors from index.css (surface/line/ink/accent/status tones), so
// dark mode needs no `dark:` variants anywhere.
//
// Badge tone class strings live in ../lib/badgeTones, which says why they are not
// in here.

import type { ComponentPropsWithoutRef, ReactNode } from 'react'

import { badgeToneClasses, type BadgeTone } from '../lib/badgeTones'

// --- Card ------------------------------------------------------------------

// Rest props spread onto the div so consumers can carry landmark semantics
// (e.g. role="region" aria-label="Sales") — the Reports/Coverage page tests
// locate sections via getByRole('region', { name }) and those locators must
// keep working when Tasks 4-5 restyle the sections onto Card.
export function Card({
  children,
  className,
  ...rest
}: ComponentPropsWithoutRef<'div'> & { children: ReactNode }) {
  const base = 'bg-surface-raised border border-line rounded-card shadow-card p-5'
  return (
    <div className={className ? `${base} ${className}` : base} {...rest}>
      {children}
    </div>
  )
}

// --- PageHeader --------------------------------------------------------------

// `level` keeps the document outline intact: the page title is the sole h1,
// while per-source sections (CoveragePage) keep their h2 rank.
export function PageHeader({
  title,
  subtitle,
  actions,
  level = 1,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
  level?: 1 | 2
}) {
  const Heading = level === 1 ? 'h1' : 'h2'
  return (
    <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <Heading className="text-xl font-semibold text-ink">{title}</Heading>
        {subtitle !== undefined && <p className="mt-0.5 text-sm text-ink-muted">{subtitle}</p>}
      </div>
      {actions !== undefined && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  )
}

// --- Badge -------------------------------------------------------------------

// Re-exported so the existing `import { type BadgeTone } from '../components/ui'`
// call sites keep working; the tones themselves live in ../lib/badgeTones, which
// explains why.
export type { BadgeTone }

export function Badge({ tone, children }: { tone: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${badgeToneClasses[tone]}`}
    >
      {children}
    </span>
  )
}

// --- Shared section-label recipe -------------------------------------------------

// Uppercase in-card section labels (Statement-style) shared by the Coverage,
// Reports, and QBO pages.
export const sectionHeadClass = 'text-xs font-semibold uppercase tracking-wide text-ink-muted'

// --- Shared table styles -------------------------------------------------------

export const tableClass = 'w-full text-sm'
export const headCellClass = 'py-1.5 pr-3 text-left text-xs font-semibold text-ink-muted'
// Separate right-aligned head recipe: `${headCellClass} text-right` would
// stack conflicting text-left/text-right utilities and the winner depends on
// stylesheet emission order.
export const amountHeadClass =
  'py-1.5 pr-2 text-right text-xs font-semibold text-ink-muted whitespace-nowrap'
export const cellClass = 'py-1.5 pr-3 align-top'
export const amountCellClass = 'py-1.5 pr-2 text-right tabular-nums whitespace-nowrap'

// --- Shared form-control recipe ------------------------------------------------

export const controlClass =
  'rounded-control border border-line bg-surface-raised px-2 py-1 text-sm text-ink ' +
  'focus:outline-none focus:ring-2 focus:ring-accent ' +
  'disabled:bg-surface-sunken disabled:text-ink-faint'

// The same control at form scale: one fixed height so an input, a select, and
// a prefixed money field sitting in one grid row line up on both edges. The
// compact `controlClass` stays as-is for toolbars and inline filters.
export const controlLargeClass =
  'h-11 w-full rounded-control border border-line bg-surface-raised px-3.5 text-sm text-ink ' +
  'focus:outline-none focus:ring-2 focus:ring-accent focus:border-accent ' +
  'disabled:bg-surface-sunken disabled:text-ink-faint'
