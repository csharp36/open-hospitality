// P8 e2e: CPA monthly pack (/reports) and the QBO push flow (/qbo) against the
// real stack — the e2e backend also runs a mock QuickBooks Online on 9200 with
// a bootstrapped refresh token, so Push posts a real journal entry.
//
// The reports test is independent, like the P7 suite. The /qbo tests are a
// `test.describe.serial` group — unlike P7 (whose tests were read-only and
// order-free), the re-push test only means what it claims if the first push
// test has already recorded a `pushed` ledger row for the day, so a first-test
// failure must skip (not cascade into a misleading failure of) the second.
// workers:1 already runs them in file order; serial adds the skip semantics.
import { expect, test } from '@playwright/test'

// HISJ 2026-07-07 balanced JE total (debits == credits) — see the P8 plan's
// golden numbers. The confirm-dialog regex derives from it (`.` escaped).
const JE_TOTAL = '12,439.66'
const CONFIRM_TOTALS = new RegExp(
  `debits ${JE_TOTAL} = credits ${JE_TOTAL}`.replaceAll('.', '\\.'),
)

test('reports: HISJ July 2026 pack — sales total and A/R first-reported balance', async ({
  page,
}) => {
  await page.goto('/reports')

  await page.getByLabel('Property').selectOption('HISJ')
  await page.getByLabel('Month').selectOption('2026-07')

  // Sales: TOR for the month == the single seeded day, 10,866.37.
  const sales = page.getByRole('region', { name: 'Sales' })
  await expect(sales.getByText('TOTAL OPERATING REVENUE')).toBeVisible()
  await expect(sales.getByText('10,866.37')).toBeVisible()

  // A/R: guest ledger 11,627.58 under the "First reported" opening label
  // (one seeded day, so first-reported == closing — the value appears twice).
  const ar = page.getByRole('region', { name: 'Accounts receivable' })
  await expect(ar.getByRole('columnheader', { name: /First reported/ })).toBeVisible()
  await expect(ar.getByText('11,627.58').first()).toBeVisible()
})

test.describe.serial('QBO push', () => {
  // Never retry: these tests mutate the push ledger, and the e2e backend is
  // NOT reset between retries within a run — after a post-push flake, a retry
  // would fail deterministically on the "not pushed" precondition instead of
  // re-testing anything. State-mutating tests against a non-resettable
  // backend must not retry.
  test.describe.configure({ retries: 0 })

  // Shared flow: pick HISJ July 2026 and return the 2026-07-07 status row.
  async function openHisjJuly(page: import('@playwright/test').Page) {
    await page.goto('/qbo')
    await page.getByLabel('Property').selectOption('HISJ')
    await page.getByLabel('Month').selectOption('2026-07')
    const table = page.getByRole('table', { name: 'QBO push status' })
    await expect(table).toBeVisible()
    return table.getByRole('row').filter({ hasText: '2026-07-07' })
  }

  test('preview then push 2026-07-07: balanced totals, confirm dialog, pushed badge + JE id', async ({
    page,
  }) => {
    const row = await openHisjJuly(page)
    await expect(row.getByText('not pushed', { exact: true })).toBeVisible()

    // Preview: the JE plan panel shows balanced totals (JE_TOTAL appears
    // exactly twice — the debit and credit totals; no line carries it).
    await row.getByRole('button', { name: 'Preview 2026-07-07' }).click()
    const panel = page.getByRole('region', { name: 'JE preview: HISJ 2026-07-07' })
    await expect(panel).toBeVisible()
    await expect(panel.getByText(JE_TOTAL)).toHaveCount(2)

    // Push: the confirm dialog restates the totals; Confirm only enables once
    // the fresh plan has loaded, which the totals assertion waits out.
    await row.getByRole('button', { name: 'Push 2026-07-07' }).click()
    const dialog = page.getByRole('dialog', { name: 'Confirm QBO push' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText(CONFIRM_TOTALS)).toBeVisible()
    await dialog.getByRole('button', { name: 'Confirm push' }).click()

    // Outcome notice with the JE id, then the refreshed ledger row. Locate
    // the notice by its text, not role=status — the transient "Pushing …"
    // banner also carries role=status.
    await expect(page.getByText(/Pushed 2026-07-07 to QBO — JE \d+\./)).toBeVisible()
    await expect(row.getByText('pushed', { exact: true })).toBeVisible()
    // The JE id cell: located by content (an all-digits cell only ever holds
    // the QBO JE id in this table), not by column position.
    await expect(row.getByRole('cell', { name: /^\d+$/ })).toBeVisible()
  })

  test('re-push 2026-07-07: already-pushed no-op, still exactly one pushed row', async ({
    page,
  }) => {
    const row = await openHisjJuly(page)
    // Depends on the previous test's push (hence the serial group).
    await expect(row.getByText('pushed', { exact: true })).toBeVisible()

    await row.getByRole('button', { name: 'Push 2026-07-07' }).click()
    const dialog = page.getByRole('dialog', { name: 'Confirm QBO push' })
    await expect(dialog.getByText(CONFIRM_TOTALS)).toBeVisible()
    await dialog.getByRole('button', { name: 'Confirm push' }).click()

    // Idempotent no-op: informational notice, badge still `pushed` (the
    // ledger keeps one row per date — nothing new was posted), and exactly
    // one pushed row in the whole table.
    await expect(
      page.getByText(/2026-07-07 was already pushed with identical content/),
    ).toBeVisible()
    const table = page.getByRole('table', { name: 'QBO push status' })
    await expect(table.getByText('pushed', { exact: true })).toHaveCount(1)
    await expect(table.getByText('already-pushed', { exact: true })).not.toBeVisible()
  })
})
