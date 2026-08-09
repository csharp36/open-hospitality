// Pay-run round-trip e2e (Pillar C2): the full stack in one flow — browser →
// pay-run API → preflight → vault open (the seeded sealed profile, opened by
// the app's SoftwareOpener at send time) → mock Gusto on 9300 → fetch results
// → department-aggregated actuals rendered.
//
// scripts/e2e_backend.py seeds "E2E Payrollee" at HISJ with an APPROVED
// timecard holding one 8h shift on 2026-06-09 — period 2026-06-08..2026-06-21
// under the default payroll_period_anchor (2026-01-05) — a $20/h pay rate, a
// sealed payroll profile, and a biweekly PaySchedule (check offset 5 days →
// check date 2026-06-26). Expected dept gross: 8h × $20 = 160.00.
//
// storageState is the payroll_admin session (global-setup.ts, from
// `.e2e-payroll-token`): the pay-run routes are require_payroll_admin-gated.
import { expect, test } from '@playwright/test'

test.use({ storageState: 'e2e/.auth/payrollState.json' })

test('payroll admin submits a pay run and fetches processed actuals', async ({ page }) => {
  await page.goto('/payroll')
  await expect(page.getByRole('heading', { name: 'Pay runs' })).toBeVisible()

  // Create the run for the seeded period (any in-period date resolves to it).
  await page.getByLabel('Property').selectOption('HISJ')
  await page.getByLabel('In period').fill('2026-06-09')
  await page.getByRole('button', { name: 'Create pay run' }).click()

  // 201 → the new run is auto-selected; the detail card shows it submitted.
  const detail = page.getByRole('region', { name: 'pay run detail' })
  await expect(detail).toBeVisible()
  await expect(detail.getByText('2026-06-08 → 2026-06-21')).toBeVisible()
  await expect(detail.getByText('submitted', { exact: true })).toBeVisible()
  await expect(detail.getByText('check date 2026-06-26')).toBeVisible()

  // Pull the provider's result: processed + a non-zero department gross.
  await detail.getByRole('button', { name: 'Fetch results' }).click()
  await expect(detail.getByText('processed', { exact: true })).toBeVisible()
  const aggregates = detail.getByRole('table', { name: 'Department aggregates' })
  await expect(aggregates.getByRole('cell', { name: 'Housekeeping' })).toBeVisible()
  await expect(aggregates.getByRole('cell', { name: '160.00', exact: true })).toBeVisible()

  // C3: the per-employee detail loads ONLY behind the explicit click (the
  // server audits every read of /runs/{id}/lines). The seeded solo employee's
  // gross equals the dept aggregate: 8h × $20 = 160.00 — non-zero, decrypted.
  await expect(detail.getByRole('table', { name: 'Employee detail' })).toBeHidden()
  await detail.getByRole('button', { name: 'Show employee detail' }).click()
  const employees = detail.getByRole('table', { name: 'Employee detail' })
  await expect(employees.getByRole('cell', { name: 'E2E Payrollee' })).toBeVisible()
  await expect(employees.getByRole('cell', { name: '160.00', exact: true })).toBeVisible()
})
