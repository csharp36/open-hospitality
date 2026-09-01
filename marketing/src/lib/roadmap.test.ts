import { describe, expect, it } from 'vitest'
import { FEATURED, featuredEntries, readCatalogue, type RoadmapEntry } from './roadmap'

function catalogue(entries: Partial<RoadmapEntry>[]): Map<string, RoadmapEntry> {
  return new Map(
    entries.map((e) => [
      e.id!,
      { id: e.id!, title: e.title ?? 't', summary: e.summary ?? 's', status: e.status ?? 'planned' },
    ]),
  )
}

describe('featuredEntries', () => {
  it('returns the site copy joined to the catalogue status', () => {
    const out = featuredEntries(catalogue([{ id: 'OH-11', status: 'planned' }]), [
      { id: 'OH-11', heading: 'Portfolio roll-up', blurb: 'Every property side by side.' },
    ])
    expect(out).toEqual([
      { id: 'OH-11', heading: 'Portfolio roll-up', blurb: 'Every property side by side.', status: 'planned' },
    ])
  })

  it('throws when a featured id is absent from the catalogue', () => {
    expect(() =>
      featuredEntries(catalogue([{ id: 'OH-11' }]), [{ id: 'OH-99', heading: 'h', blurb: 'b' }]),
    ).toThrow(/OH-99/)
  })

  it('throws when a featured item has shipped, so the copy cannot outlive it', () => {
    expect(() =>
      featuredEntries(catalogue([{ id: 'OH-11', status: 'shipped' }]), [
        { id: 'OH-11', heading: 'h', blurb: 'b' },
      ]),
    ).toThrow(/shipped/)
  })
})

describe('the real catalogue', () => {
  // This is the guard itself: it runs against .github/roadmap.yml, so CI fails
  // when a featured item ships or is renamed out of the file.
  it('supports every entry the site features', () => {
    expect(() => featuredEntries(readCatalogue(), FEATURED)).not.toThrow()
  })
})
