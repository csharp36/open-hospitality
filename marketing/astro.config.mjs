// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

// SITE_URL drives canonical tags and the sitemap; APP_ORIGIN (see src/lib/site.ts)
// drives every CTA. Both are env with defaults so the site is host-agnostic —
// landing on a different domain is a config change, not a rewrite.
export default defineConfig({
  site: process.env.SITE_URL ?? 'https://oh.mandati.ai',
  output: 'static',
  integrations: [sitemap()],
  vite: { plugins: [tailwindcss()] },
});
