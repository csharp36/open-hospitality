// Exact fixed-point arithmetic over the API's decimal strings (≤4dp storage
// scale). Amounts stay strings end-to-end; BigInt on scaled integers does the
// math, so reconciliation never touches floats.

type Scaled = { value: bigint; scale: number }

const FIXED_RE = /^(-?)(\d+)(?:\.(\d+))?$/

function parseScaled(s: string): Scaled {
  const m = FIXED_RE.exec(s.trim())
  if (!m) throw new Error(`not a fixed-point decimal: ${JSON.stringify(s)}`)
  const sign = m[1] === '-' ? -1n : 1n
  const int = m[2] ?? '0'
  const frac = m[3] ?? ''
  return { value: sign * BigInt(int + frac), scale: frac.length }
}

function align(a: Scaled, b: Scaled): [bigint, bigint, number] {
  const scale = Math.max(a.scale, b.scale)
  const av = a.value * 10n ** BigInt(scale - a.scale)
  const bv = b.value * 10n ** BigInt(scale - b.scale)
  return [av, bv, scale]
}

function formatScaled(value: bigint, scale: number): string {
  const neg = value < 0n
  const digits = (neg ? -value : value).toString().padStart(scale + 1, '0')
  const int = digits.slice(0, digits.length - scale)
  const frac = scale > 0 ? `.${digits.slice(digits.length - scale)}` : ''
  return `${neg ? '-' : ''}${int}${frac}`
}

/** Exact sum of two decimal strings; result carries the wider scale. */
export function addFixed(a: string, b: string): string {
  const [av, bv, scale] = align(parseScaled(a), parseScaled(b))
  return formatScaled(av + bv, scale)
}

/** Exact numeric equality across differing scales ("410.00" == "410.0000"). */
export function eqFixed(a: string, b: string): boolean {
  const [av, bv] = align(parseScaled(a), parseScaled(b))
  return av === bv
}

/** Exact sum of many decimal strings (empty list -> "0"). */
export function sumFixed(values: readonly string[]): string {
  return values.reduce(addFixed, '0')
}
