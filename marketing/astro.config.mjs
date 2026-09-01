// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

// SITE_URL drives canonical tags and the sitemap. It is env with a default so
// the site is host-agnostic — landing on a different domain is a config change,
// not a rewrite. The CTA target is deliberately not configured here: it belongs
// with the links that use it.
export default defineConfig({
  site: process.env.SITE_URL ?? 'https://oh.mandati.ai',
  output: 'static',
  integrations: [sitemap()],
  vite: { plugins: [tailwindcss()] },
});
