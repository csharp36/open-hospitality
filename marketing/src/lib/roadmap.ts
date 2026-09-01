import { readFileSync } from 'node:fs'
import { parse } from 'yaml'

export type RoadmapEntry = {
  id: string
  title: string
  summary: string
  status: string
}

export type FeaturedCopy = {
  id: string
  heading: string
  blurb: string
}

/**
 * What the forward band shows, and the words it uses. Status is deliberately
 * absent — it comes from the catalogue, so this list and the product cannot
 * disagree. featuredEntries() below is where that is enforced.
 */
export const FEATURED: FeaturedCopy[] = [
  {
    id: 'OH-11',
    heading: 'Portfolio roll-up',
    blurb: 'Every property side by side, ranked, with the portfolio total on top.',
  },
  {
    id: 'OH-15',
    heading: 'Performance alerts',
    blurb: "A daily and weekly flash when a number moves in a way you'd want to know about.",
  },
  {
    id: 'OH-8',
    heading: 'Budget vs. actual',
    blurb: 'Import the budget, see variance by department, every month.',
  },
  {
    id: 'OH-22',
    heading: 'Direct PMS integrations',
    blurb: 'Connect your property management system directly, with no file to send.',
  },
]

/** The catalogue the repo already keeps: .github/roadmap.yml, from marketing/src/lib/. */
export function readCatalogue(
  path = new URL('../../../.github/roadmap.yml', import.meta.url),
): Map<string, RoadmapEntry> {
  const doc = parse(readFileSync(path, 'utf8')) as { roadmap?: RoadmapEntry[] }
  if (!Array.isArray(doc?.roadmap)) {
    throw new Error(`${path} does not have a top-level "roadmap:" list — check the file is well-formed.`)
  }
  return new Map(doc.roadmap.map((entry) => [entry.id, entry]))
}

/**
 * Join the site's copy to the catalogue's status, refusing both ways it can lie.
 *
 * Throwing is deliberate rather than returning an error value: a caller that
 * invokes this at build time (page frontmatter) fails the build, which is the
 * only way to stop a shipped feature from sitting in "shipping next".
 */
export function featuredEntries(
  catalogue: Map<string, RoadmapEntry>,
  featured: FeaturedCopy[] = FEATURED,
): (FeaturedCopy & { status: string })[] {
  return featured.map((copy) => {
    const entry = catalogue.get(copy.id)
    if (!entry) {
      throw new Error(
        `${copy.id} is featured on the marketing site but absent from .github/roadmap.yml. ` +
          `Add the entry, or remove it from FEATURED in marketing/src/lib/roadmap.ts.`,
      )
    }
    if (entry.status === 'shipped') {
      throw new Error(
        `${copy.id} ("${entry.title}") has shipped, but the marketing site still lists it ` +
          `under "shipping next". Move it into the page proper and remove it from FEATURED ` +
          `in marketing/src/lib/roadmap.ts.`,
      )
    }
    return { ...copy, status: entry.status }
  })
}
