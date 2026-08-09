// Portal e2e: one flow against the real stack (Testcontainers Postgres seeded
// with the six sample PDFs, FastAPI on 8100, Vite dev server on 5173).
// Mirrors the spec's DoD: upload result card, HISJ/SSSJ statements with and
// without prior-year columns, Parking drill-through with the reconciliation
// badge, and the coverage worklists.
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

const SAMPLE_PDF = fileURLToPath(
  new URL(
    '../../docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf',
    import.meta.url,
  ),
)

test('upload: re-ingesting a sample PDF renders a result card', async ({ page }) => {
  await page.goto('/upload')

  await page.getByLabel('PDF files').setInputFiles(SAMPLE_PDF)

  // The backend was seeded with this exact file, so this hits the
  // duplicate-hash path — assert the card renders its fields, not numbers.
  const card = page.getByRole('region', {
    name: 'Upload result: Trial Balance 07.07.2026 - Opera.pdf',
  })
  await expect(card).toBeVisible()
  await expect(card.getByText('OPERA', { exact: true })).toBeVisible()
  await expect(card.getByText('PMS source')).toBeVisible()
  await expect(card.getByText('Business date')).toBeVisible()
  await expect(card.getByText('Staged')).toBeVisible()
  await expect(card.getByText('Skipped')).toBeVisible()
})

test('SOS: HISJ statement, Parking drill-through, then SSSJ without PY columns', async ({
  page,
}) => {
  await page.goto('/')

  // HISJ on 2026-07-07: TOR 10,866.37 with prior-year statistics columns.
  await page.getByLabel('Property').selectOption('HISJ')
  await page.getByLabel('Date', { exact: true }).fill('2026-07-07')

  await expect(page.getByText('TOTAL OPERATING REVENUE')).toBeVisible()
  await expect(page.getByText('10,866.37')).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'DAY PY' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'YTD PY' })).toBeVisible()

  // Drill through the Parking line: trx code 5105, 410.00, reconciled badge.
  await page.getByRole('button', { name: 'Parking', exact: true }).click()
  const panel = page.getByRole('dialog', { name: 'Transactions: Parking' })
  await expect(panel).toBeVisible()
  await expect(panel.getByText('5105').first()).toBeVisible()
  await expect(panel.getByText('410.00').first()).toBeVisible()
  await expect(panel.getByText(/reconciled/)).toBeVisible()
  await panel.getByRole('button', { name: 'Close' }).click()
  await expect(panel).not.toBeVisible()

  // SSSJ (Autoclerk): TOR 6,359.03, no prior-year statistics columns.
  await page.getByLabel('Property').selectOption('SSSJ')
  await page.getByLabel('Date', { exact: true }).fill('2026-07-07')

  await expect(page.getByText('TOTAL OPERATING REVENUE')).toBeVisible()
  await expect(page.getByText('6,359.03')).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'DAY', exact: true })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'DAY PY' })).not.toBeVisible()
})

test('coverage: OPERA + AUTOCLERK cards with the 5105/Parking worklist row', async ({
  page,
}) => {
  await page.goto('/coverage')

  const opera = page.getByRole('region', { name: 'Coverage: OPERA' })
  await expect(opera).toBeVisible()
  await expect(page.getByRole('region', { name: 'Coverage: AUTOCLERK' })).toBeVisible()

  const worklist = opera.getByRole('table', { name: 'Financial needs-review worklist' })
  const row = worklist.getByRole('row').filter({ hasText: '5105' })
  await expect(row).toBeVisible()
  await expect(row.getByRole('cell', { name: 'Parking', exact: true })).toBeVisible()
})
