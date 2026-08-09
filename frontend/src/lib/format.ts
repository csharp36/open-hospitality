// Display-only formatting of the API's decimal strings. `Number()` is allowed
// here (and only here): formatting and the % display are the sanctioned float
// uses — all arithmetic that must be exact lives in lib/decimal.ts.

const moneyFmt = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

/** "10866.3700" -> "10,866.37"; "-16.2000" -> "-16.20". */
export function fmtMoney(s: string): string {
  const n = Number(s)
  return moneyFmt.format(n === 0 ? 0 : n) // normalize -0
}

/** Percent of `total` to one decimal place; "n/a" when total is zero. */
export function pct(part: string, total: string): string {
  const t = Number(total)
  if (t === 0) return 'n/a'
  return `${((Number(part) / t) * 100).toFixed(1)}%`
}

/** Statistics cell: trim trailing fractional zeros ("95.5000" -> "95.5"). */
export function fmtStat(s: string | null): string {
  if (s === null) return ''
  return s.includes('.') ? s.replace(/\.?0+$/, '') : s
}
