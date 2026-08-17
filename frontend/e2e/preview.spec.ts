import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

test.use({ storageState: { cookies: [], origins: [] } }) // anonymous — no seeded token

const SAMPLE = fileURLToPath(
  new URL('../../docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf', import.meta.url),
)

test('anonymous visitor drops a report and sees a P&L', async ({ page }) => {
  await page.goto('/try')
  await expect(page.getByText(/see your night audit/i)).toBeVisible()
  await page.getByLabel('PDF file').setInputFiles(SAMPLE)
  const region = page.getByRole('region', { name: 'Preview result' })
  await expect(region).toBeVisible()
  await expect(region.getByText(/mapped/)).toBeVisible()
})

test('a trailing-slash /try/ stays public (no Keycloak bounce)', async ({ page }) => {
  await page.goto('/try/')
  await expect(page.getByText(/see your night audit/i)).toBeVisible()
})

test('a non-PDF drop shows a validation message', async ({ page }) => {
  await page.goto('/try')
  await page.getByLabel('PDF file').setInputFiles({
    name: 'notes.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('hello'),
  })
  await expect(page.getByRole('alert')).toBeVisible()
})
