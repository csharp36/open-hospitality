import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://localhost:4321' },
  webServer: {
    command: 'npm run preview -- --port 4321',
    url: 'http://localhost:4321',
    reuseExistingServer: !process.env.CI,
    // Astro's `preview` command detects an AI-agent parent process (via
    // am-i-vibing) and detaches into the background, which makes the
    // process Playwright spawned exit immediately — Playwright then reports
    // "Process from config.webServer exited early" even though a server is
    // still running. ASTRO_PREVIEW_BACKGROUND is Astro's own documented
    // escape hatch to keep it in the foreground: see
    // node_modules/astro/dist/cli/preview/index.js, `agentDetected`.
    env: { ...process.env, ASTRO_PREVIEW_BACKGROUND: '1' },
  },
})
