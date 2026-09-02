import { parse } from 'yaml'

import roadmapYaml from '../../../.github/roadmap.yml?raw'

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

/**
 * The catalogue the repo already keeps: .github/roadmap.yml, from
 * marketing/src/lib/. Defaults to the `?raw` import above, not a
 * `readFileSync` of a path built from `import.meta.url`. That distinction
 * matters: Vite resolves a `?raw` specifier against the *source* tree while
 * building the module graph, so the reference stays correct no matter where
 * the bundler later places the compiled chunk. A runtime path built from
 * `import.meta.url` has no such guarantee — `astro build` inlines this
 * module into `dist/.prerender/chunks/`, one directory level deeper than
 * `marketing/src/lib/`, so the same `../../../` climb lands on `marketing/`
 * instead of the repo root and the read throws ENOENT. That failure happens
 * before featuredEntries() ever reaches the shipped-status check below, so a
 * shipped feature would produce a path error instead of the message telling
 * someone to promote it — the guard cannot fire if this regresses.
 * Reproduce by swapping the import for
 * `readFileSync(new URL('../../../.github/roadmap.yml', import.meta.url), 'utf8')`
 * and running `cd marketing && npm run build` from the repo root.
 */
export function readCatalogue(source: string = roadmapYaml): Map<string, RoadmapEntry> {
  const doc = parse(source) as { roadmap?: RoadmapEntry[] }
  if (!Array.isArray(doc?.roadmap)) {
    throw new Error(
      '.github/roadmap.yml does not have a top-level "roadmap:" list — check the file is well-formed.',
    )
  }
  return new Map(doc.roadmap.map((entry) => [entry.id, entry]))
}

/**
 * Join the site's copy to the catalogue's status, refusing both ways it can lie.
 *
 * Throwing is deliberate rather than returning an error value: Forward.astro's
 * frontmatter is where this throw becomes a build failure; roadmap.test.ts's
 * "the real catalogue" case is where the catalogue is checked.
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
