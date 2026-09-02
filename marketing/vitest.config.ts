import { configDefaults, defineConfig } from 'vitest/config'

// Without this exclude, `npm test` also collects e2e/cta.spec.ts and fails
// with "Playwright Test did not expect test() to be called here" (reproduce
// by removing the 'e2e/**' entry below and rerunning). Excluded here the
// same way frontend/vite.config.ts excludes its e2e/ directory.
export default defineConfig({
  test: {
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
})
