// The bar treatment shared by the dashboard and the payroll report: thick
// bars, rounded caps, and a vertical fill ramp that is lighter at the top and
// full-strength at the baseline.
//
// The ramp is derived from ONE colour with color-mix rather than stored as a
// second token per series. That matters for identity: a series keeps a single
// hue in the legend, the swatch, and the bar, and adding a department can never
// desynchronise a hand-picked pair of stops.

/**
 * Bar thickness for a band. Bars fill most of their band with a small gutter,
 * so a two-day window reads as two solid columns rather than two hairlines —
 * capped so a single day does not become a wall.
 */
export function barWidth(step: number, max = 68): number {
  return Math.min(max, Math.max(3, step * 0.72))
}

/**
 * A bar with rounded top corners and a square foot.
 *
 * The baseline is a real edge — the axis — so rounding it would lift the bar
 * off its own zero and make short bars read as floating. Only the free end
 * gets a radius.
 */
export function topRoundedBar(x: number, y: number, w: number, h: number, r?: number): string {
  // The radius follows the bar's own width. A fixed one is either invisible on
  // a 90-day chart or turns a thin bar into a dome, and a stacked column full
  // of domes reads as beads on a string rather than one measure.
  const rad = Math.max(0, Math.min(r ?? Math.min(8, Math.max(2, w * 0.16)), w / 2, h))
  return (
    `M${x},${y + h} V${y + rad} A${rad},${rad} 0 0 1 ${x + rad},${y} ` +
    `H${x + w - rad} A${rad},${rad} 0 0 1 ${x + w},${y + rad} V${y + h} Z`
  )
}

// Gradient ids are document-global, so every chart namespaces its own: two
// charts on one page both using cat-1 would otherwise collide and the second
// would silently paint with the first one's stops.
export function gradientId(prefix: string, color: string): string {
  return `${prefix}-${color.replace(/[^a-zA-Z0-9]/g, '')}`
}

/** `fill` for a bar in the chart that rendered `<BarGradients>` with `prefix`. */
export function barFill(prefix: string, color: string): string {
  return `url(#${gradientId(prefix, color)})`
}

/** The light end of the ramp — also the top stop for CSS-drawn (HTML) bars. */
export function barRampTop(color: string): string {
  return `color-mix(in oklab, ${color} 72%, white)`
}

/** The same ramp for a bar drawn in HTML rather than SVG. */
export function barRampCss(color: string): string {
  return `linear-gradient(180deg, ${barRampTop(color)}, ${color})`
}
