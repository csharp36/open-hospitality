// Kiosk clock-in e2e: proves the punch flow needs NO operator session at all —
// the kiosk is authenticated purely by its device token (scripts/e2e_backend.py
// writes `.e2e-kiosk-token` at the repo root after enrolling a HISJ kiosk +
// employee in _seed). storageState is overridden to empty here specifically
// to demonstrate that no OIDC user is present when the punch succeeds.
import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'

// The kiosk is device-authenticated: no OIDC session at all.
test.use({ storageState: { cookies: [], origins: [] } })

test('an employee clocks in on the kiosk with no login', async ({ page }) => {
  const token = readFileSync('../.e2e-kiosk-token', 'utf8').trim()
  await page.addInitScript(
    (t) => window.localStorage.setItem('usali.kiosk.token', t),
    token,
  )
  await page.goto('/kiosk')

  await expect(page.getByRole('heading', { name: 'Time Clock' })).toBeVisible()
  await page.getByRole('button', { name: 'E2E Clocker' }).click()
  await page.getByRole('button', { name: 'Clock In' }).click()
  await expect(page.getByText(/Clocked in/i)).toBeVisible()
})

// My week (D2): scripts/e2e_backend.py seeds a PUBLISHED schedule for the
// UPCOMING week (the same "this week's Monday + 7 days" rule the UI computes
// in lib/week.ts) with one 09:00–17:00 Housekeeping shift for E2E Clocker.
test('an employee sees their published week on the kiosk — still no login', async ({ page }) => {
  const token = readFileSync('../.e2e-kiosk-token', 'utf8').trim()
  await page.addInitScript(
    (t) => window.localStorage.setItem('usali.kiosk.token', t),
    token,
  )
  await page.goto('/kiosk')

  await page.getByRole('button', { name: 'E2E Clocker' }).click()
  await page.getByRole('button', { name: 'My week' }).click()
  await expect(page.getByText(/week of \d{4}-\d{2}-\d{2}/)).toBeVisible()
  await expect(page.getByText('Housekeeping · 09:00–17:00')).toBeVisible()

  // Back returns to the punch actions.
  await page.getByRole('button', { name: 'Back' }).click()
  await expect(page.getByRole('button', { name: 'Clock In' })).toBeVisible()
})
