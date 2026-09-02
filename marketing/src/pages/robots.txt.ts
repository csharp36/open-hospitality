import type { APIRoute } from 'astro'

// The canonical host is a deploy-time decision (astro.config.mjs's `site`,
// driven by SITE_URL) so the site can move off oh.mandati.ai without a
// rewrite. robots.txt has to carry an absolute sitemap URL, so it is
// generated here from `site` rather than shipped as a static public/ file —
// a hardcoded host would drift from the canonical tags and the sitemap
// itself the moment SITE_URL changes.
export const GET: APIRoute = ({ site }) => {
  const sitemapUrl = new URL('sitemap-index.xml', site)
  const body = `User-agent: *
Allow: /

Sitemap: ${sitemapUrl.href}
`
  return new Response(body, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  })
}
