import { expect, test } from '@playwright/test'

const APP_ORIGIN = process.env.APP_ORIGIN ?? 'https://demo.mandati.ai'

test('the primary CTA sends the visitor to the preview', async ({ page }) => {
  await page.goto('/')
  const cta = page.getByRole('link', { name: 'See it on your own report' }).first()
  await expect(cta).toHaveAttribute('href', `${APP_ORIGIN}/try`)
})

test('every page reaches the others through the nav', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'Pricing' }).first().click()
  await expect(page).toHaveURL(/\/pricing\/?$/)
  await page.getByRole('link', { name: 'Your data' }).first().click()
  await expect(page).toHaveURL(/\/your-data\/?$/)
})
