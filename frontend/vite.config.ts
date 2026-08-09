/// <reference types="vitest/config" />
import { configDefaults } from 'vitest/config'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev-mode proxy: `usali serve` runs the API on 127.0.0.1:8100; the Vite dev
// server forwards API and upload calls there so the SPA needs no CORS setup.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8100',
      '/ingest': 'http://127.0.0.1:8100',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
    // e2e/*.spec.ts are Playwright tests, not vitest tests.
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
})
