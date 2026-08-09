# USALI Portal (frontend)

React 19 + TypeScript SPA over the USALI Engine portal API (Vite, TanStack
Query/Router, Tailwind v4). See the repo README's "Portal (dev run)" section.

```bash
npm install
npm run dev     # Vite dev server on 5173; proxies /api and /ingest to 127.0.0.1:8100
npm run build   # production build to dist/ — `usali serve` then serves it at /
npm test        # vitest
npm run lint    # oxlint (Vite's default linter)
```
