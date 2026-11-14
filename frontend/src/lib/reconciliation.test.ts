import { describe, expect, it } from 'vitest'

import { formatRecon, formatReconDelta, occupancyExceedsCapacity } from './reconciliation'

describe('reconciliation formatting', () => {
  it('renders each side in the metric’s unit instead of the raw decimal string', () => {
    expect(formatRecon('occupancy', '0.847559296482412060301507537')).toBe('84.8%')
    expect(formatRecon('occupancy', '1.0285')).toBe('102.8%') // same toFixed(1) the KPI tile uses
    expect(formatRecon('adr', '191.2630632411067193675889328')).toBe('$191.26')
    expect(formatRecon('room_revenue', '96778.89')).toBe('$96,778.89')
    expect(formatRecon('rooms_available', '597.0000')).toBe('597')
    expect(formatRecon('revpar', null)).toBe('—')
    expect(formatRecon('occupancy', 'not a number')).toBe('—')
  })

  it('shows the gap, signed, in the same unit', () => {
    expect(formatReconDelta('occupancy', '1.0285', '0.8475')).toBe('+18.1 pts')
    expect(formatReconDelta('revpar', '196.7051', '162.1089')).toBe('+$34.60')
    expect(formatReconDelta('rooms_available', '492', '597.0000')).toBe('−105')
    expect(formatReconDelta('adr', '191.2626', '191.2626')).toBe('$0.00')
    expect(formatReconDelta('adr', null, '1')).toBeNull()
  })

  it('leaves an unknown metric as a plain number rather than guessing a unit', () => {
    expect(formatRecon('something_new', '12.3456789')).toBe('12.3457')
    expect(formatReconDelta('something_new', '2', '1')).toBe('+1')
  })

  it('flags occupancy over 100% as a capacity problem, not a result', () => {
    expect(occupancyExceedsCapacity('1.0285')).toBe(true)
    expect(occupancyExceedsCapacity('1.0')).toBe(false)
    expect(occupancyExceedsCapacity('0.9999')).toBe(false)
    expect(occupancyExceedsCapacity(null)).toBe(false)
  })
})
