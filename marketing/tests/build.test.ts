import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const DIST = new URL('../dist/', import.meta.url).pathname
const PAGES = ['index.html', 'pricing/index.html', 'your-data/index.html']

function html(page: string): string {
  const path = join(DIST, page)
  if (!existsSync(path)) throw new Error(`${page} not built — run \`npm run build\` first`)
  return readFileSync(path, 'utf8')
}

describe.each(PAGES)('%s', (page) => {
  const doc = () => html(page)

  it('has a title', () => expect(doc()).toMatch(/<title>[^<]{10,}<\/title>/))
  it('has a meta description', () =>
    expect(doc()).toMatch(/<meta name="description" content="[^"]{40,}"/))
  it('has a canonical link', () =>
    expect(doc()).toMatch(/<link rel="canonical" href="https?:\/\/[^"]+"/))
  it('has an OG image', () =>
    expect(doc()).toMatch(/<meta property="og:image" content="[^"]+"/))
})

describe('referenced assets exist', () => {
  // og:image lives in a `content` attribute, so the href/src scan below is blind
  // to it. A broken social card is invisible until someone shares a link.
  it('every og:image resolves to a file in the build', () => {
    const missing: string[] = []
    for (const page of PAGES) {
      const m = html(page).match(/<meta property="og:image" content="([^"]+)"/)
      if (!m) { missing.push(`${page}: no og:image`); continue }
      const path = new URL(m[1]).pathname
      if (!existsSync(join(DIST, path))) missing.push(`${page} -> ${m[1]}`)
    }
    expect(missing).toEqual([])
  })
})

describe('internal links', () => {
  it('every internal href resolves to something in the build', () => {
    const missing: string[] = []
    for (const page of PAGES) {
      for (const [, href] of doc_hrefs(html(page))) {
        if (!href.startsWith('/') || href.startsWith('//')) continue
        const path = href.split('#')[0].split('?')[0]
        if (path === '/' || path === '') continue
        const candidates = [
          join(DIST, path),
          join(DIST, path, 'index.html'),
          join(DIST, `${path}.html`),
        ]
        if (!candidates.some(existsSync)) missing.push(`${page} -> ${href}`)
      }
    }
    expect(missing).toEqual([])
  })
})

function* doc_hrefs(source: string): Generator<[string, string]> {
  for (const m of source.matchAll(/(href|src)="([^"]+)"/g)) yield [m[1], m[2]]
}
