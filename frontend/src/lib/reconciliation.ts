// Display formatting for the PMS-KPI reconciliation on the Performance page.
//
// The API sends both sides of each line as full-precision decimal strings
// ("0.847559296482412060301507537688…"). An operator deciding whether 84.8%
// and 102.9% reconcile does not need 30 digits — they need both figures in
// the metric's own unit and the gap between them. The raw strings stay
// available (the page puts them in a tooltip) for anyone who does.
//
// Number() is display-only here, the sanctioned use (see lib/format.ts); the
// agree/disagree verdict itself comes from the server and is never recomputed.

export type ReconKind = 'pct' | 'money' | 'count'

/** Unit per reconciliation metric. Anything the server adds later and this
 * map does not know renders as a plain number rather than a wrong unit. */
export const RECON_KINDS: Record<string, ReconKind> = {
  occupancy: 'pct',
  adr: 'money',
  revpar: 'money',
  trevpar: 'money',
  room_revenue: 'money',
  rooms_available: 'count',
  rooms_sold: 'count',
}

const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
const count = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })
const plain = new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 })

export function kindFor(metric: string): ReconKind | null {
  return RECON_KINDS[metric] ?? null
}

/** A figure in its metric's unit; "—" for null or unparseable. */
export function formatRecon(metric: string, value: string | null): string {
  if (value === null) return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  switch (kindFor(metric)) {
    case 'pct':
      return `${(n * 100).toFixed(1)}%`
    case 'money':
      return money.format(n)
    case 'count':
      return count.format(n)
    default:
      return plain.format(n)
  }
}

/** computed − ingested, signed, in the metric's unit: "+2.6 pts", "−$3.50",
 * "+105 rooms". Null when either side is missing. */
export function formatReconDelta(
  metric: string,
  computed: string | null,
  ingested: string | null,
): string | null {
  if (computed === null || ingested === null) return null
  const c = Number(computed)
  const i = Number(ingested)
  if (!Number.isFinite(c) || !Number.isFinite(i)) return null
  const d = c - i
  const sign = d > 0 ? '+' : d < 0 ? '−' : ''
  const abs = Math.abs(d)
  switch (kindFor(metric)) {
    case 'pct':
      return `${sign}${(abs * 100).toFixed(1)} pts`
    case 'money':
      return `${sign}${money.format(abs)}`
    case 'count':
      return `${sign}${count.format(abs)}`
    default:
      return `${sign}${plain.format(abs)}`
  }
}

/** Occupancy above 100% is arithmetically possible — rooms sold over rooms
 * available — and always means the denominator is wrong, almost always the
 * property's room inventory. Flag it rather than print it as a fact. */
export function occupancyExceedsCapacity(value: string | null): boolean {
  if (value === null) return false
  const n = Number(value)
  return Number.isFinite(n) && n > 1
}
