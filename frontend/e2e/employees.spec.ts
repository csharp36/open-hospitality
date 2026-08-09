// Admin onboarding e2e: overrides the default operator (accountant)
// storageState with the org_admin one global-setup.ts writes from
// `.e2e-admin-token` (scripts/e2e_backend.py), so require_onboarder
// (org_admin/property_gm — see workforce.py) allows the onboard POST and the
// Employees nav link/page render instead of being gated out.
import { expect, test } from '@playwright/test'

test.use({ storageState: 'e2e/.auth/adminState.json' })

test('admin can open Employees and onboard', async ({ page }) => {
  await page.goto('/employees')
  await expect(page.getByRole('table', { name: 'Employees' })).toBeVisible()

  // Onboarding lives in a modal — open it before the fields exist.
  await page.getByRole('button', { name: 'Add Employee' }).click()
  await page.getByLabel('Full Name').fill('E2E Operator')
  await page.getByLabel('Email').fill('e2e-op@example.com')
  await page.getByLabel('Role').selectOption('property_gm')
  await page.getByRole('button', { name: 'Onboard' }).click()

  await expect(page.getByRole('cell', { name: 'E2E Operator', exact: true })).toBeVisible()
})
