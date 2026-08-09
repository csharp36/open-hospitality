// The `<defs>` block a bar chart needs: one fill ramp per distinct bar colour.
// The ramp maths lives in lib/chartBars so this file exports a component only.

import { barRampTop, gradientId } from '../lib/chartBars'

export default function BarGradients({
  prefix,
  colors,
}: {
  prefix: string
  colors: string[]
}) {
  return (
    <defs>
      {[...new Set(colors)].map((c) => (
        <linearGradient key={c} id={gradientId(prefix, c)} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={barRampTop(c)} />
          <stop offset="100%" stopColor={c} />
        </linearGradient>
      ))}
    </defs>
  )
}
