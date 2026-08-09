// Playwright e2e config: boots the real backend (throwaway Testcontainers
// Postgres seeded with the six sample PDFs — scripts/e2e_backend.py) and the
// Vite dev server (which proxies /api + /ingest to 8100); tests hit 5173.
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  // One worker: the suite is a single flow against one shared backend.
  workers: 1,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'blob' : 'list',
  // Runs after the webServers are ready (so scripts/e2e_backend.py has written
  // .e2e-token): seeds the oidc store into e2e/.auth/storageState.json.
  globalSetup: './e2e/global-setup.ts',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
    // Every context starts with the seeded oidc-client-ts User in localStorage,
    // so the gated API (require_operator) sees a valid operator bearer and the
    // SPA renders authenticated instead of redirecting to Keycloak.
    storageState: './e2e/.auth/storageState.json',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        browserName: 'chromium',
        // The kiosk e2e (kiosk.spec.ts) drives a live camera preview via
        // getUserMedia — headless chromium needs a fake device + auto-granted
        // permission or the promise never resolves.
        permissions: ['camera'],
        launchOptions: {
          args: [
            '--use-fake-ui-for-media-stream',
            '--use-fake-device-for-media-stream',
          ],
        },
      },
    },
  ],
  webServer: [
    {
      // Repo venv python: the script needs testcontainers + the usali package.
      command: '../.venv/bin/python ../scripts/e2e_backend.py',
      // /api/properties only answers once migrate/seed/process has finished.
      url: 'http://127.0.0.1:8100/api/properties',
      // Never reuse: `usali serve` (the dev workflow) also listens on 8100,
      // and reusing it would write through the dev's real /ingest and assert
      // against their real DB. Fail fast with "port 8100 is already used".
      reuseExistingServer: false,
      // Generous: container pull + schema + seeding six PDFs.
      timeout: 180_000,
      // SIGTERM (instead of the default SIGKILL) lets uvicorn shut down and
      // return, so the script's context managers unwind and remove the
      // postgres container; the script's startup sweep stays as a fallback.
      gracefulShutdown: { signal: 'SIGTERM', timeout: 15_000 },
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      // --strictPort: if 5173 is occupied by something that isn't a reusable
      // dev server, fail immediately instead of a misleading startup timeout.
      command: 'npm run dev -- --strictPort',
      url: 'http://localhost:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
})
