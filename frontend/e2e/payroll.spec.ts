// Sealed-PII vault e2e (Pillar C1): a payroll_admin enters an SSN, the browser
// seals it with real @hpke/core to the server's pinned recipient public key, and
// the server stores the opaque envelope. There is no read-back (the vault is
// blind overwrite), so success is the status query flipping to "on file" —
// proving the browser sealed and the backend's SoftwareOpener accepted it.
//
// storageState is overridden to the payroll_admin session that
// global-setup.ts seeds from `.e2e-payroll-token` (scripts/e2e_backend.py):
// require_payroll_admin gates the write/status routes, which neither the default
// accountant nor the org_admin token satisfies.
import { expect, test } from '@playwright/test'

test.use({ storageState: 'e2e/.auth/payrollState.json' })

test('payroll admin seals an SSN in the browser and sees it on file', async ({ page }) => {
  await page.goto('/employees')
  await expect(page.getByRole('table', { name: 'Employees' })).toBeVisible()

  // E2E Clocker is seeded at HISJ (scripts/e2e_backend.py _seed). Open its
  // payroll sub-form — the Payroll button only renders for a payroll principal.
  await page.getByRole('button', { name: 'Payroll for E2E Clocker' }).click()
  const region = page.getByRole('region', { name: 'payroll profile' })
  await expect(region).toBeVisible()

  // Nothing on file yet (exact match: "not on file" must not count).
  await expect(region.getByText('on file', { exact: true })).toHaveCount(0)

  await region.getByLabel('SSN').fill('123-45-6789')
  await region.getByRole('button', { name: 'Save' }).click()

  // The status query (booleans only) now reports the SSN on file.
  await expect(region.getByText('on file', { exact: true })).toBeVisible()
})
