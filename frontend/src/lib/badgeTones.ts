// The badge palette, in its own module rather than beside `Badge` in ui.tsx.
// Callers that need the colours without `Badge`'s fixed geometry (the sidebar's
// 10px setup pill) must not re-derive them, and a module exporting components
// cannot also export a plain object without breaking Fast Refresh for every
// component in it — `react/only-export-components`, whose `allowConstantExport`
// escape hatch covers literal strings but not a `Record`.
//
// Tone class strings deliberately contain the legacy colour words
// (green/amber/red/blue): className-regex assertions in the page tests depend
// on those words surviving a restyle.

export type BadgeTone = 'ok' | 'warn' | 'danger' | 'info' | 'neutral'

export const badgeToneClasses: Record<BadgeTone, string> = {
  ok: 'bg-ok-green-soft text-ok-green',
  warn: 'bg-warn-amber-soft text-warn-amber',
  danger: 'bg-danger-red-soft text-danger-red',
  info: 'bg-info-blue-soft text-info-blue',
  // text-ink (not ink-muted): muted ink on the sunken tint is ~4.3:1 at
  // text-xs in light mode — below AA.
  neutral: 'bg-surface-sunken text-ink',
}
